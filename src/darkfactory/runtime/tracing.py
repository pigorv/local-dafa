from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from temporalio import activity


_PHASE_TRACER = trace.get_tracer("darkfactory.phase")


def coalesced_trace_id(workflow_id: str, workflow_run_id: str | None = None) -> str:
    """Mirror otel-collector-config.yaml:47-54.

    When a Temporal run id is available, one Langfuse trace equals one Temporal
    run. The workflow-id-only hash is the collector fallback for spans that lack
    a run id.
    """
    if workflow_run_id:
        source = f"{workflow_id}/{workflow_run_id}"
    else:
        source = workflow_id
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


@contextmanager
def phase_span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    """Open a `darkfactory.phase.<name>` span as a child of the active span.

    Stamps `langfuse.session.id` / `session.id` from the current activity's
    `workflow_id` when called inside a `@activity.defn` body, so even if the
    parent span lookup fails the collector can still coalesce by session id.
    Outside an activity (e.g. unit tests, LangGraph nodes invoked directly)
    the helper still opens a span — it just skips the session stamping.

    Extra keyword attributes are stamped on the span verbatim under the
    `darkfactory.<key>` namespace; `None` values are dropped so callers can
    pass optional state without guarding each one.
    """
    span_attrs: dict[str, Any] = {"darkfactory.phase": name}
    if activity.in_activity():
        info = activity.info()
        span_attrs["langfuse.session.id"] = info.workflow_id
        span_attrs["session.id"] = info.workflow_id
    for key, value in attrs.items():
        if value is None:
            continue
        span_attrs[f"darkfactory.{key}"] = value
    with _PHASE_TRACER.start_as_current_span(
        f"darkfactory.phase.{name}", attributes=span_attrs
    ) as span:
        yield span
