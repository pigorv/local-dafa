from __future__ import annotations

import logging
import os
from typing import Optional

import darkfactory
from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import ProxyTracerProvider


# Default to localhost:4317 because the CLI typically runs on the host where
# docker-compose's port mapping (0.0.0.0:4317->4317) makes the collector
# reachable. The orchestrator and worker containers override this with the
# Docker DNS name `otel-collector:4317` via docker-compose.yml.
DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"

_WF_ID_ENV = "DARKFACTORY_WF_ID"
_ENVIRONMENT_ENV = "DARKFACTORY_ENVIRONMENT"
_DEFAULT_ENVIRONMENT = "local"

_SESSION_ATTR = "langfuse.session.id"
_SESSION_ATTR_ALT = "session.id"
_ENV_ATTR = "langfuse.environment"
_ENV_ATTR_ALT = "deployment.environment"
_TEMPORAL_WF_ATTR = "temporalWorkflowID"


class SessionStampingSpanProcessor(SpanProcessor):
    """Stamp langfuse.session.id (plus environment) on every span at start.

    Resolution order for the session id:
      1. Process default (env DARKFACTORY_WF_ID; set in worker_main from
         TEMPORAL_TASK_QUEUE, since each per-workflow worker container hosts
         exactly one wf_id).
      2. Parent span's langfuse.session.id — already-stamped ancestor.
      3. Parent span's temporalWorkflowID — added by
         temporalio.contrib.opentelemetry.TracingInterceptor on RunActivity
         spans, lets Python-side child spans (LangGraph, etc.) inherit the
         workflow id even in the multi-workflow orchestrator.

    Cross-trace coalescing into a single Langfuse trace is handled by the
    otel-collector's `transform/coalesce_trace_id` processor, which derives
    a deterministic OTel `trace_id` from `temporalWorkflowID` /
    `langfuse.session.id` / `resource.attributes["darkfactory.workflow_id"]`
    via `Substring(SHA256(...), 0, 32)`. We do not stamp the unified id from
    Python because the orchestrator hosts spans whose parent is cross-process
    (the cli.run span via TraceContextPropagator) — at on_start the parent
    is a NonRecordingSpan with no readable attributes — and Temporal's
    interceptor sets `temporalWorkflowID` only after on_start. The collector
    sees the final attribute state, so it's the right place to do the rewrite.
    """

    def __init__(self, default_wf_id: Optional[str], environment: Optional[str]) -> None:
        self._default_wf_id = default_wf_id
        self._environment = environment

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        wf_id = self._default_wf_id
        if not wf_id:
            parent = trace.get_current_span(parent_context)
            if parent is not None and parent.is_recording():
                attrs = getattr(parent, "attributes", None) or {}
                wf_id = attrs.get(_SESSION_ATTR) or attrs.get(_TEMPORAL_WF_ATTR)
        if wf_id:
            span.set_attribute(_SESSION_ATTR, wf_id)
            span.set_attribute(_SESSION_ATTR_ALT, wf_id)
        if self._environment:
            span.set_attribute(_ENV_ATTR, self._environment)
            span.set_attribute(_ENV_ATTR_ALT, self._environment)
        # Temporal's TracingInterceptor names the workflow root span
        # `RunWorkflow:<WorkflowType>`. Lift the type onto langfuse.trace.name
        # so each workflow execution shows up in Langfuse with the workflow's
        # class name rather than the raw OTel span name.
        if span.name.startswith("RunWorkflow:"):
            workflow_type = span.name.split(":", 1)[1]
            span.set_attribute("langfuse.trace.name", workflow_type)
            span.set_attribute("temporal.workflow.type", workflow_type)

    def on_end(self, span: ReadableSpan) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def init_observability(
    service_name: str,
    *,
    span_processor: Optional[SpanProcessor] = None,
    exporter: Optional[SpanExporter] = None,
) -> None:
    """Install the global OTel TracerProvider and OpenInference instrumentations.

    `span_processor` and `exporter` are test hooks — production callers pass
    neither and get the default OTLP gRPC exporter wrapped in a
    `BatchSpanProcessor`. Tests typically pass a `SimpleSpanProcessor` wrapping
    an `InMemorySpanExporter` to assert span attributes deterministically.

    Set `OTEL_SDK_DISABLED=true` to skip OTel setup entirely (standard OTel
    escape hatch — useful when the collector is unreachable, e.g. running
    the CLI without `docker compose up`).
    """
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() in ("1", "true", "yes"):
        return
    if not isinstance(trace.get_tracer_provider(), ProxyTracerProvider):
        return

    # Suppress per-retry WARNING spam from BatchSpanProcessor's exporter when the
    # collector is briefly unreachable; the final ERROR-level "Failed to export"
    # still surfaces. Set DARKFACTORY_OTEL_VERBOSE=1 to keep retry logs.
    if not os.environ.get("DARKFACTORY_OTEL_VERBOSE"):
        logging.getLogger("opentelemetry.exporter.otlp.proto.grpc.exporter").setLevel(
            logging.ERROR
        )

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.version": darkfactory.__version__,
            }
        )
    )
    provider.add_span_processor(
        SessionStampingSpanProcessor(
            default_wf_id=os.environ.get(_WF_ID_ENV) or None,
            environment=os.environ.get(_ENVIRONMENT_ENV) or _DEFAULT_ENVIRONMENT,
        )
    )
    if span_processor is not None:
        provider.add_span_processor(span_processor)
    else:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)
        active_exporter = exporter or OTLPSpanExporter(
            endpoint=endpoint, insecure=endpoint.startswith("http://")
        )
        provider.add_span_processor(BatchSpanProcessor(active_exporter))
    trace.set_tracer_provider(provider)

    # Claude Agent SDK telemetry rides the bundled `claude` CLI's native
    # exporters (CLAUDE_CODE_ENABLE_TELEMETRY=1 + OTEL_TRACES_EXPORTER=otlp set
    # on the worker container). The Python SDK never goes through the public
    # `anthropic` client, so AnthropicInstrumentor is intentionally not wired.
    # The SDK itself injects W3C TRACEPARENT into the CLI subprocess env from
    # the active OTel span (claude_agent_sdk's subprocess transport, not us),
    # so the CLI's `claude_code.interaction` span and its children adopt the
    # activity trace_id without anything extra on our side.
    LangChainInstrumentor().instrument()
