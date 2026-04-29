"""M3-4: in-memory observability smoke test for the full pipeline.

Runs `DarkFactoryWorkflow` end-to-end against Temporal's time-skipping
test environment with `TracingInterceptor` wired on the client and both
workers, captures every emitted OTel span via an `InMemorySpanExporter`,
and asserts the trace shape from ARCHITECTURE §5.7:

  * one workflow root span scoped to the run (TracingInterceptor names it
    `RunWorkflow:DarkFactoryWorkflow` on the worker side; ARCHITECTURE's
    "DarkFactoryWorkflow.run" phrasing is descriptive, not the literal
    span name)
  * one `RunActivity:<stage>` span per stage activity that executed
  * for any stage that opens an SDK client, at least one `gen_ai.*`
    generation span — `stub_build_stage` simulates the AnthropicInstrumentor
    output so the assertion holds without an Anthropic API call
  * for any tool call inside a generation, a `tool.<name>` span — the stub
    drives the real `make_otel_emit` pre/post pair to produce one

No network, no Anthropic API, no Docker.
"""
from __future__ import annotations

import asyncio
from typing import Any

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
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from darkfactory.hooks.otel_emit import make_otel_emit
from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import GateDecision, RunRequest


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


def _hook_event(tool_name: str, **extra: Any) -> dict[str, Any]:
    base = {
        "session_id": "smoke",
        "transcript_path": "/tmp/transcript",
        "cwd": "/workspace",
        "agent_id": "agent-smoke",
        "agent_type": "backend",
        "tool_name": tool_name,
    }
    base.update(extra)
    return base


@activity.defn(name="build_stage")
async def stub_build_stage(state: dict) -> dict:
    """Simulate one Anthropic generation that issues one tool call.

    The `gen_ai.system="anthropic"` attribute is what
    `openinference.instrumentation.anthropic.AnthropicInstrumentor` would
    set on a real LLM-call span. The tool span comes from driving the real
    `otel_emit` hook pair, so we exercise production code rather than
    fake-emitting another span manually.
    """
    tracer = trace.get_tracer("test_observability_smoke")
    pre, post = make_otel_emit("backend")
    with tracer.start_as_current_span("anthropic.chat.completions") as gen:
        gen.set_attribute("gen_ai.system", "anthropic")
        gen.set_attribute("gen_ai.request.model", "claude-sonnet-4-5-20250929")
        gen.set_attribute("gen_ai.usage.input_tokens", 100)
        gen.set_attribute("gen_ai.usage.output_tokens", 20)
        await pre(
            _hook_event(
                "sandbox_bash",
                hook_event_name="PreToolUse",
                tool_input={"argv": ["mvn", "compile"]},
                tool_use_id="tu-smoke",
            ),
            "tu-smoke",
            {"signal": None},
        )
        await post(
            _hook_event(
                "sandbox_bash",
                hook_event_name="PostToolUse",
                tool_input={},
                tool_response={"returncode": 0, "stdout": "BUILD SUCCESS"},
                tool_use_id="tu-smoke",
            ),
            "tu-smoke",
            {"signal": None},
        )
    return {"build_order": [], "current_slice": "", "patches": []}


@activity.defn(name="verify_stage")
async def stub_verify_stage(state: dict) -> dict:
    return {
        "test_results": [],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
    }


@activity.defn(name="spec_adjustment_stage")
async def stub_spec_adjustment_stage(state: dict) -> dict:
    return {}


@activity.defn(name="code_quality_stage")
async def stub_code_quality_stage(state: dict) -> dict:
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
    stub_spec_adjustment_stage,
    stub_code_quality_stage,
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

    async with await WorkflowEnvironment.start_time_skipping(
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
                GateDecision(approved=True, reason="smoke approves"),
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
        "code_quality_stage",
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

    gen_ai_spans = [
        s for s in spans
        if any(k.startswith("gen_ai.") for k in dict(s.attributes or {}).keys())
    ]
    assert gen_ai_spans, (
        "expected at least one gen_ai.* generation span (simulating an SDK call); "
        f"saw {span_names}"
    )

    tool_spans = [s for s in spans if s.name.startswith("tool.")]
    assert tool_spans, f"expected at least one tool.<name> span; saw {span_names}"
    assert any(
        s.name == "tool.sandbox_bash" for s in tool_spans
    ), f"expected tool.sandbox_bash span; saw tool spans {[s.name for s in tool_spans]}"
