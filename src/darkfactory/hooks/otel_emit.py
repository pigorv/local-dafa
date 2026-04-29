"""PreToolUse + PostToolUse hooks: emit one OTel span per tool call.

Per ARCHITECTURE.md §5.6 / §5.7, every SDK client opens an OTel span when
a tool is about to run and closes it once the tool returns. The span is
named ``tool.<tool_name>``; pre records the originating role and the
truncated argv; post records the exit code (when the response carries
one) and the output size in bytes. Spans inherit whatever span is active
at the moment ``otel_pre`` fires — typically the OpenInference
``AnthropicInstrumentor`` generation span — so Langfuse renders them
nested under the LLM call that triggered them.

Pre and post are separate hook callbacks with no shared context object;
``HookContext`` only carries a ``signal`` field. We correlate the two
via ``tool_use_id``, which is present on both ``PreToolUseHookInput``
and ``PostToolUseHookInput``. The ``make_otel_emit`` factory closes
over a per-client dict keyed by ``tool_use_id`` and returns the
``(pre, post)`` pair that share it.

Notes on edge cases:

- If a downstream PreToolUse hook (``loop_breaker``, ``call_cap``, or the
  ``can_use_tool`` permission gate) denies the tool *after* ``otel_pre``
  has run, the tool never executes and ``PostToolUse`` does not fire,
  so the span stays open. Per-client lifetime is short (one stage
  activity) and call counts are capped at 25 (`call_cap`), so the leak
  is bounded; we do not actively drain the dict on session close.
- ``trace.get_tracer`` returns a no-op tracer when no ``TracerProvider``
  has been configured, so the hooks are safe to attach during unit tests
  that don't initialise observability.
"""
from __future__ import annotations

import json as _json
import shlex
from typing import Any

from claude_agent_sdk.types import (
    HookContext,
    HookJSONOutput,
    PostToolUseHookInput,
    PreToolUseHookInput,
)
from opentelemetry import trace
from opentelemetry.trace import Span

ARGV_TRUNCATE_LIMIT = 256
TRACER_NAME = "darkfactory.hooks.otel_emit"


def _argv_repr(tool_input: dict[str, Any] | None) -> str:
    """Render ``tool_input`` to a short string suitable for a span attribute.

    Returns ``""`` for missing input. ``sandbox_bash`` carries an ``argv``
    list which is rendered with ``shlex.join``; everything else falls back
    to ``repr(tool_input)`` so non-shell tools (``Read``, ``Edit``, ...)
    still record something useful. The result is truncated to
    ``ARGV_TRUNCATE_LIMIT`` characters with an ellipsis suffix.
    """
    if not tool_input:
        return ""
    argv = tool_input.get("argv")
    if isinstance(argv, list) and all(isinstance(p, str) for p in argv):
        rendered = shlex.join(argv)
    else:
        rendered = repr(tool_input)
    if len(rendered) > ARGV_TRUNCATE_LIMIT:
        rendered = rendered[:ARGV_TRUNCATE_LIMIT] + "...[truncated]"
    return rendered


def _output_bytes(tool_response: Any) -> int:
    """Approximate the byte size of ``tool_response`` for an OTel attribute."""
    if tool_response is None:
        return 0
    if isinstance(tool_response, (bytes, bytearray)):
        return len(tool_response)
    if isinstance(tool_response, str):
        return len(tool_response.encode("utf-8", errors="replace"))
    try:
        return len(_json.dumps(tool_response, default=str).encode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return len(repr(tool_response).encode("utf-8", errors="replace"))


def _exit_code(tool_response: Any) -> int | None:
    """Pull the ``returncode`` from a ``sandbox_bash``-shaped response."""
    if isinstance(tool_response, dict):
        rc = tool_response.get("returncode")
        if isinstance(rc, int):
            return rc
    return None


def make_otel_emit(role: str) -> tuple:
    """Return a ``(otel_pre, otel_post)`` hook pair scoped to ``role``.

    The pair shares a private dict keyed by ``tool_use_id`` so the post
    hook can recover the span opened by the pre hook for the same tool
    invocation. Each ``make_<role>_client`` calls this once and attaches
    the pre to ``PreToolUse`` and the post to ``PostToolUse``.
    """
    spans: dict[str, Span] = {}
    tracer = trace.get_tracer(TRACER_NAME)

    async def otel_pre(
        input_data: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        tool_name = input_data.get("tool_name", "") or ""
        span = tracer.start_span(f"tool.{tool_name}")
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("agent.role", role)
        argv = _argv_repr(input_data.get("tool_input"))
        if argv:
            span.set_attribute("tool.input.argv", argv)
        if tool_use_id:
            spans[tool_use_id] = span
        else:
            # No correlation key — close immediately so we don't leak.
            span.end()
        return {}

    async def otel_post(
        input_data: PostToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        if not tool_use_id:
            return {}
        span = spans.pop(tool_use_id, None)
        if span is None:
            return {}
        rc = _exit_code(input_data.get("tool_response"))
        if rc is not None:
            span.set_attribute("tool.exit_code", rc)
        span.set_attribute(
            "tool.output_bytes", _output_bytes(input_data.get("tool_response"))
        )
        span.end()
        return {}

    return otel_pre, otel_post
