"""Assert workflow + activity span attributes that drive Langfuse trace shape.

Companion to `test_observability_smoke.py` — that test asserts the *shape*
of the trace (which spans exist), this test asserts the *attribute content*
that Langfuse uses for trace naming, session grouping, and metadata
filtering. Specifically:

  * `_stamp_temporal_activity_attrs()` writes all six `temporal.*` attrs
    plus `langfuse.session.id` on the active activity span.
  * `SessionStampingSpanProcessor` lifts the workflow type onto
    `langfuse.trace.name` for any span named `RunWorkflow:<type>`.

Both behaviours are observed via an `InMemorySpanExporter`, no Langfuse
round-trip and no Anthropic API.
"""
from __future__ import annotations

import asyncio
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from temporalio import activity, workflow
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from darkfactory.agents._sdk_common import role_turn_span
from darkfactory.bootstrap import SessionStampingSpanProcessor
from darkfactory.runtime.activities import _stamp_temporal_activity_attrs
from darkfactory.runtime.tracing import phase_span
from tests.temporal_testing import start_time_skipping_env


def _attach_in_memory_exporter() -> InMemorySpanExporter:
    """Bolt an `InMemorySpanExporter` (plus the session/trace-name stamper)
    onto whatever provider is global.

    The OTel global provider can only be set once per process; an earlier
    test (or `init_observability` in production) may have installed one
    already. We always add both the `SessionStampingSpanProcessor` and the
    in-memory exporter — adding the stamper twice is safe because
    `set_attribute` is idempotent, but skipping it when an unrelated test
    set the provider first leaves `langfuse.trace.name` unstamped.
    """
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not hasattr(provider, "add_span_processor"):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(
            resource=Resource.create({"service.name": "darkfactory-test"})
        )
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SessionStampingSpanProcessor(None, "test"))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@activity.defn(name="attr_probe_activity")
async def attr_probe_activity() -> dict[str, Any]:
    """Activity that returns a dump of attributes the helper just stamped.

    Reads the live attribute dict off the current span, post-stamp.
    Asserting from inside the activity (rather than from the exported
    span) avoids depending on which `SpanProcessor` order the runtime
    chose.
    """
    _stamp_temporal_activity_attrs()
    span = trace.get_current_span()
    return dict(getattr(span, "attributes", {}) or {})


@workflow.defn(name="AttrProbeWorkflow", sandboxed=False)
class AttrProbeWorkflow:
    @workflow.run
    async def run(self) -> dict[str, Any]:
        from datetime import timedelta

        return await workflow.execute_activity(
            "attr_probe_activity",
            start_to_close_timeout=timedelta(seconds=10),
        )


def test_stamp_temporal_activity_attrs_sets_all_required_attributes() -> None:
    """Direct unit test against `ActivityEnvironment` — no workflow needed."""
    asyncio.run(_run_activity_env_probe())


async def _run_activity_env_probe() -> None:
    _attach_in_memory_exporter()
    env = ActivityEnvironment()
    # `ActivityEnvironment` doesn't run Temporal's `TracingInterceptor`, so
    # we have to provide a recording span for the helper to stamp. The
    # invocation runs in the same coroutine, so an enclosing
    # `start_as_current_span` is the live span when the helper runs.
    tracer = trace.get_tracer("test_tracing_attrs")
    with tracer.start_as_current_span("test-activity-probe"):
        attrs = await env.run(attr_probe_activity)
    # `langfuse.session.id` (= workflow_id) plus the six temporal.* attrs.
    expected_keys = {
        "langfuse.session.id",
        "session.id",
        "temporal.workflow.id",
        "temporal.workflow.run_id",
        "temporal.workflow.type",
        "temporal.task_queue",
        "temporal.activity.type",
        "temporal.activity.attempt",
    }
    missing = expected_keys - set(attrs.keys())
    assert not missing, f"missing attrs {missing}; got {sorted(attrs.keys())}"
    # Cross-field consistency: session.id and temporal.workflow.id are both
    # the workflow_id and must match.
    assert attrs["langfuse.session.id"] == attrs["temporal.workflow.id"]


def test_workflow_root_span_carries_langfuse_trace_name() -> None:
    """End-to-end: run a tiny workflow + activity and inspect exported spans."""
    asyncio.run(_run_workflow_probe())


async def _run_workflow_probe() -> None:
    exporter = _attach_in_memory_exporter()
    exporter.clear()

    interceptor = TracingInterceptor()
    wf_id = "test-wf-attr-probe"

    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter,
        interceptors=[interceptor],
    ) as env:
        async with Worker(
            env.client,
            task_queue="attr-probe-tq",
            workflows=[AttrProbeWorkflow],
            activities=[attr_probe_activity],
            interceptors=[interceptor],
        ):
            attrs = await env.client.execute_workflow(
                AttrProbeWorkflow.run,
                id=wf_id,
                task_queue="attr-probe-tq",
            )

    trace.get_tracer_provider().force_flush()  # type: ignore[attr-defined]

    # 1. Activity-level: `temporal.workflow.id` matches the workflow id we
    #    started, proving propagation through Temporal headers.
    assert attrs["temporal.workflow.id"] == wf_id, (
        f"activity span saw wrong workflow id: {attrs['temporal.workflow.id']!r}"
    )
    assert attrs["temporal.workflow.type"] == "AttrProbeWorkflow"
    assert attrs["temporal.activity.type"] == "attr_probe_activity"
    assert attrs["temporal.activity.attempt"] == 1

    # 2. Workflow-root-level: span named `RunWorkflow:AttrProbeWorkflow`
    #    must have `langfuse.trace.name` stamped by
    #    `SessionStampingSpanProcessor.on_start`.
    spans = list(exporter.get_finished_spans())
    root_spans = [s for s in spans if s.name == "RunWorkflow:AttrProbeWorkflow"]
    assert root_spans, (
        "missing RunWorkflow:AttrProbeWorkflow root span; "
        f"saw {[s.name for s in spans]}"
    )
    root = root_spans[0]
    root_attrs = dict(root.attributes or {})
    assert root_attrs.get("langfuse.trace.name") == "AttrProbeWorkflow", (
        f"root span missing langfuse.trace.name; got {root_attrs}"
    )
    assert root_attrs.get("temporal.workflow.type") == "AttrProbeWorkflow"


@activity.defn(name="phase_probe_activity")
async def phase_probe_activity() -> dict[str, Any]:
    """Activity that opens phase + agent.turn spans and returns their context.

    The activity exercises the full structural chain the production
    pipeline produces: activity → phase → agent.turn. We return the OTel
    span ids so the test can assert parent-child links from the exported
    span tree.
    """
    _stamp_temporal_activity_attrs()
    activity_ctx = trace.get_current_span().get_span_context()
    with phase_span("probe", attempt=1) as ps:
        phase_ctx = ps.get_span_context()
        async with role_turn_span("probe_role", wp_id="A1") as ts:
            turn_ctx = ts.get_span_context()
    return {
        "activity_span_id": format(activity_ctx.span_id, "016x"),
        "phase_span_id": format(phase_ctx.span_id, "016x"),
        "turn_span_id": format(turn_ctx.span_id, "016x"),
    }


@workflow.defn(name="PhaseProbeWorkflow", sandboxed=False)
class PhaseProbeWorkflow:
    @workflow.run
    async def run(self) -> dict[str, Any]:
        from datetime import timedelta

        return await workflow.execute_activity(
            "phase_probe_activity",
            start_to_close_timeout=timedelta(seconds=10),
        )


def test_all_spans_for_one_workflow_share_session_and_structure() -> None:
    """Every span emitted for one workflow run shares one session id, and
    the activity → phase → agent.turn chain forms a proper parent tree.

    This locks in the coalescing invariant — Langfuse groups by OTel
    trace_id (rewritten by the collector from `langfuse.session.id`), so
    if any span in the chain is missing the session attribute, that span
    lands in a different Langfuse trace.
    """
    asyncio.run(_run_phase_probe())


async def _run_phase_probe() -> None:
    exporter = _attach_in_memory_exporter()
    exporter.clear()

    interceptor = TracingInterceptor()
    wf_id = "test-wf-phase-probe"

    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter,
        interceptors=[interceptor],
    ) as env:
        async with Worker(
            env.client,
            task_queue="phase-probe-tq",
            workflows=[PhaseProbeWorkflow],
            activities=[phase_probe_activity],
            interceptors=[interceptor],
        ):
            span_ids = await env.client.execute_workflow(
                PhaseProbeWorkflow.run,
                id=wf_id,
                task_queue="phase-probe-tq",
            )

    trace.get_tracer_provider().force_flush()  # type: ignore[attr-defined]

    spans = list(exporter.get_finished_spans())
    # Temporal emits a RunActivity:* span on both the orchestrator-side
    # scheduler and the worker-side executor — same name, different
    # processes simulated by the same in-memory test exporter. Look up by
    # span_id rather than by name so we assert against the span the
    # activity body actually ran on.
    spans_by_id = {format(s.context.span_id, "016x"): s for s in spans}

    activity_span = spans_by_id.get(span_ids["activity_span_id"])
    phase = spans_by_id.get(span_ids["phase_span_id"])
    turn = spans_by_id.get(span_ids["turn_span_id"])
    assert activity_span is not None, (
        f"worker-side activity span {span_ids['activity_span_id']} not exported"
    )
    assert phase is not None and turn is not None

    # 1. Spans created in-process from the activity body downward must carry
    #    `langfuse.session.id == wf_id` so the collector groups them by session.
    #    The workflow-root span is a known exception: temporalio sets
    #    `temporalWorkflowID` AFTER `on_start`, so the Python processor can't
    #    stamp session.id there — the collector reads `temporalWorkflowID`
    #    instead, which is what produces the unified trace at export time.
    for label, span in (
        ("activity", activity_span),
        ("phase", phase),
        ("agent.turn", turn),
    ):
        attrs = dict(span.attributes or {})
        assert attrs.get("langfuse.session.id") == wf_id, (
            f"{label} span has wrong langfuse.session.id: "
            f"{attrs.get('langfuse.session.id')!r} (expected {wf_id!r})"
        )
        assert attrs.get("session.id") == wf_id, (
            f"{label} span missing session.id"
        )
    root_spans = [s for s in spans if s.name == "RunWorkflow:PhaseProbeWorkflow"]
    assert root_spans, "missing RunWorkflow:PhaseProbeWorkflow span"
    root_attrs = dict(root_spans[0].attributes or {})
    assert root_attrs.get("temporalWorkflowID") == wf_id, (
        "RunWorkflow span missing temporalWorkflowID — collector cannot "
        "coalesce trace_id without it"
    )

    # 2. Parent-child chain: turn → phase → activity.
    assert turn.parent is not None and format(turn.parent.span_id, "016x") == span_ids["phase_span_id"], (
        f"agent.turn parent is not phase span; got {turn.parent}"
    )
    assert phase.parent is not None and format(phase.parent.span_id, "016x") == span_ids["activity_span_id"], (
        f"phase parent is not activity span; got {phase.parent}"
    )

    # 3. Phase span carries the iteration attribute we passed.
    phase_attrs = dict(phase.attributes or {})
    assert phase_attrs.get("darkfactory.phase") == "probe"
    assert phase_attrs.get("darkfactory.attempt") == 1

    # 4. Agent.turn span carries the role + wp_id we passed.
    turn_attrs = dict(turn.attributes or {})
    assert turn_attrs.get("darkfactory.role") == "probe_role"
    assert turn_attrs.get("darkfactory.wp_id") == "A1"
