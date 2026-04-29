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
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from darkfactory.bootstrap import SessionStampingSpanProcessor
from darkfactory.runtime.activities import _stamp_temporal_activity_attrs


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

    async with await WorkflowEnvironment.start_time_skipping(
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
