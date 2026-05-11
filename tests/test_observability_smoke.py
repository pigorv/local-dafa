"""M3-4: in-memory observability smoke test for the full pipeline.

Runs `DarkFactoryWorkflow` end-to-end against Temporal's time-skipping
test environment with `TracingInterceptor` wired on the client and both
workers, captures every emitted OTel span via an `InMemorySpanExporter`,
and asserts the Python-side trace shape from ARCHITECTURE §5.7:

  * one workflow root span scoped to the run (TracingInterceptor names it
    `RunWorkflow:DarkFactoryWorkflow` on the worker side; ARCHITECTURE's
    "DarkFactoryWorkflow.run" phrasing is descriptive, not the literal
    span name)
  * one `RunActivity:<stage>` span per stage activity that executed

The native Claude Code spans (`claude_code.interaction`, `claude_code.llm_request`,
`claude_code.tool*`) are emitted by the bundled `claude` CLI subprocess and
cannot be exercised without a real subprocess + OTel collector. They are
verified end-to-end by running a real workflow and inspecting Langfuse.

No network, no Anthropic API, no Docker.
"""
from __future__ import annotations

import asyncio

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import ProxyTracerProvider

from temporalio import activity
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import GateDecision, RunRequest
from tests.temporal_testing import start_time_skipping_env


_exporter = InMemorySpanExporter()
_processor_attached = False


def _ensure_in_memory_exporter() -> None:
    """Attach `_exporter` to whatever TracerProvider is global, exactly once.

    The OTel global TracerProvider can only be set once. If a prior test
    already installed an SDK provider, we bolt on an additional processor;
    otherwise we set our own provider. Either way the test ends up reading
    spans out of the same `_exporter`.
    """
    global _processor_attached
    if _processor_attached:
        return
    provider = trace.get_tracer_provider()
    if isinstance(provider, ProxyTracerProvider):
        new_provider = TracerProvider(
            resource=Resource.create({"service.name": "darkfactory-test"})
        )
        new_provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(new_provider)
    elif isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
    _processor_attached = True


@activity.defn(name="setup_worker_activity")
async def stub_setup_worker(wf_id: str, repo_url: str) -> str:
    return f"darkfactory-worker-{wf_id}"


@activity.defn(name="teardown_worker_activity")
async def stub_teardown_worker(wf_id: str) -> None:
    return None


@activity.defn(name="hydrate_stage")
async def stub_hydrate_stage(state: dict) -> dict:
    return {"repo_context": {"repo_root": state.get("repo_path", "/workspace")}}


@activity.defn(name="discovery_stage")
async def stub_discovery_stage(state: dict) -> dict:
    return {"stories": [], "spec": [], "review_decision": None}


@activity.defn(name="build_stage")
async def stub_build_stage(state: dict) -> dict:
    return {"build_order": [], "current_slice": "", "patches": []}


@activity.defn(name="verify_stage")
async def stub_verify_stage(state: dict) -> dict:
    return {
        "test_results": [],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
    }


@activity.defn(name="fixer_stage")
async def stub_fixer_stage(state: dict) -> dict:
    return {}


@activity.defn(name="reviewer_stage")
async def stub_reviewer_stage(state: dict) -> dict:
    return {}


@activity.defn(name="pr_creator_stage")
async def stub_pr_creator_stage(state: dict) -> dict:
    return {}


@activity.defn(name="merge_branch")
async def stub_merge_branch(state: dict) -> dict:
    return {}


_STAGE_STUBS = (
    stub_hydrate_stage,
    stub_discovery_stage,
    stub_build_stage,
    stub_verify_stage,
    stub_fixer_stage,
    stub_reviewer_stage,
    stub_pr_creator_stage,
    stub_merge_branch,
)


def test_observability_smoke_full_pipeline_emits_expected_spans() -> None:
    asyncio.run(_run_smoke())


async def _run_smoke() -> None:
    _ensure_in_memory_exporter()
    _exporter.clear()

    wf_id = "test-wf-observability-smoke"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="observability smoke",
    )

    interceptor = TracingInterceptor()

    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter,
        interceptors=[interceptor],
    ) as env:
        agent_tq = f"agent-tq-{wf_id}"
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryWorkflow],
            activities=[stub_setup_worker, stub_teardown_worker],
            interceptors=[interceptor],
        ), Worker(
            env.client,
            task_queue=agent_tq,
            activities=list(_STAGE_STUBS),
            interceptors=[interceptor],
        ):
            handle = await env.client.start_workflow(
                DarkFactoryWorkflow.run,
                req,
                id=wf_id,
                task_queue="supervisor-tq",
            )
            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="smoke approves brief"),
            )
            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="smoke approves merge"),
            )
            result = await handle.result()

    # Force the SimpleSpanProcessor to flush any pending exports before we
    # read; SimpleSpanProcessor exports synchronously, but a flush keeps the
    # assertion robust against future changes to the global provider.
    trace.get_tracer_provider().force_flush()  # type: ignore[attr-defined]

    assert result.status == "merged", f"workflow ended in {result.status!r}"

    spans = list(_exporter.get_finished_spans())
    span_names = [s.name for s in spans]

    workflow_root_spans = [
        s for s in spans if s.name == "RunWorkflow:DarkFactoryWorkflow"
    ]
    assert workflow_root_spans, (
        f"missing RunWorkflow:DarkFactoryWorkflow root span; saw {span_names}"
    )

    expected_stage_activities = {
        "hydrate_stage",
        "discovery_stage",
        "build_stage",
        "verify_stage",
        "reviewer_stage",
        "pr_creator_stage",
        "merge_branch",
    }
    activity_stage_names = {
        s.name.split(":", 1)[1]
        for s in spans
        if s.name.startswith("RunActivity:")
    }
    missing = expected_stage_activities - activity_stage_names
    assert not missing, (
        f"missing activity spans for stages {missing}; saw {sorted(activity_stage_names)}"
    )
