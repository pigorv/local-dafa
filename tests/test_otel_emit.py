"""Unit tests for the otel_emit Pre/PostToolUse hook pair.

Drives ``make_otel_emit`` directly against an in-memory OTel exporter so
the tests are hermetic — no collector, no Langfuse, no SDK process. The
broader observability smoke check (one root span per workflow, full
nesting) is covered by ``tests/test_observability_smoke.py`` in M3-4.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import ProxyTracerProvider

from darkfactory.hooks.otel_emit import (
    ARGV_TRUNCATE_LIMIT,
    _argv_repr,
    _exit_code,
    _output_bytes,
    make_otel_emit,
)


_exporter = InMemorySpanExporter()
_processor_attached = False


def _ensure_in_memory_exporter() -> None:
    """Ensure ``_exporter`` receives every span emitted in this process.

    The OTel global TracerProvider can only be set once; if a prior test
    (or ``bootstrap.init_observability``) already installed an SDK
    ``TracerProvider``, we attach our exporter as an additional processor
    on it. Otherwise we create a fresh provider and own it.
    """
    global _processor_attached
    if _processor_attached:
        return

    provider = trace.get_tracer_provider()
    if isinstance(provider, ProxyTracerProvider):
        new_provider = TracerProvider()
        new_provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(new_provider)
    elif isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
    _processor_attached = True


@pytest.fixture(autouse=True)
def _clean_spans() -> None:
    _ensure_in_memory_exporter()
    _exporter.clear()
    yield


def _pre(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": "test",
        "transcript_path": "/tmp/transcript",
        "cwd": "/workspace",
        "agent_id": "agent-test",
        "agent_type": "backend",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "tu-1",
    }


def _post(tool_name: str, tool_response: Any) -> dict[str, Any]:
    return {
        "session_id": "test",
        "transcript_path": "/tmp/transcript",
        "cwd": "/workspace",
        "agent_id": "agent-test",
        "agent_type": "backend",
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {},
        "tool_response": tool_response,
        "tool_use_id": "tu-1",
    }


def _ctx() -> dict[str, Any]:
    return {"signal": None}


# ---------------------------------------------------------------------------
# argv / output / exit-code helpers
# ---------------------------------------------------------------------------


def test_argv_repr_renders_argv_list_with_shlex() -> None:
    out = _argv_repr({"argv": ["mvn", "-q", "compile"]})
    assert out == "mvn -q compile"


def test_argv_repr_quotes_shell_special_chars() -> None:
    out = _argv_repr({"argv": ["echo", "hello world"]})
    assert "'hello world'" in out


def test_argv_repr_falls_back_to_repr_for_non_argv() -> None:
    out = _argv_repr({"file_path": "src/Foo.java"})
    assert "file_path" in out
    assert "Foo.java" in out


def test_argv_repr_empty_input_returns_empty() -> None:
    assert _argv_repr(None) == ""
    assert _argv_repr({}) == ""


def test_argv_repr_truncates_long_input() -> None:
    long_argv = ["echo", "x" * (ARGV_TRUNCATE_LIMIT * 2)]
    out = _argv_repr({"argv": long_argv})
    assert len(out) == ARGV_TRUNCATE_LIMIT + len("...[truncated]")
    assert out.endswith("...[truncated]")


def test_output_bytes_handles_common_shapes() -> None:
    assert _output_bytes(None) == 0
    assert _output_bytes("hello") == 5
    assert _output_bytes(b"\x00\x01\x02") == 3
    # dict gets JSON-serialised for size
    size = _output_bytes({"stdout": "abc", "returncode": 0})
    assert size > 0


def test_exit_code_extracts_returncode_from_dict() -> None:
    assert _exit_code({"returncode": 0}) == 0
    assert _exit_code({"returncode": 1, "stdout": "x"}) == 1
    assert _exit_code({"returncode": "not-int"}) is None
    assert _exit_code({"stdout": "no rc"}) is None
    assert _exit_code("string response") is None
    assert _exit_code(None) is None


# ---------------------------------------------------------------------------
# end-to-end pre + post pair
# ---------------------------------------------------------------------------


def test_pre_then_post_emits_one_closed_span() -> None:
    pre, post = make_otel_emit("backend")

    async def drive() -> None:
        await pre(_pre("sandbox_bash", {"argv": ["mvn", "compile"]}), "tu-1", _ctx())
        await post(
            _post("sandbox_bash", {"returncode": 0, "stdout": "BUILD SUCCESS"}),
            "tu-1",
            _ctx(),
        )

    asyncio.run(drive())
    spans = _exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "tool.sandbox_bash"
    attrs = dict(span.attributes)
    assert attrs["tool.name"] == "sandbox_bash"
    assert attrs["agent.role"] == "backend"
    assert attrs["tool.input.argv"] == "mvn compile"
    assert attrs["tool.exit_code"] == 0
    assert attrs["tool.output_bytes"] > 0


def test_pre_records_role_attribute_per_factory() -> None:
    pre_a, post_a = make_otel_emit("backend")
    pre_b, post_b = make_otel_emit("database")

    async def drive() -> None:
        await pre_a(_pre("Edit", {"file_path": "x.java"}), "a", _ctx())
        await post_a(_post("Edit", "ok"), "a", _ctx())
        await pre_b(_pre("Edit", {"file_path": "y.sql"}), "b", _ctx())
        await post_b(_post("Edit", "ok"), "b", _ctx())

    asyncio.run(drive())
    spans = _exporter.get_finished_spans()
    by_role = {dict(s.attributes)["agent.role"]: s for s in spans}
    assert set(by_role) == {"backend", "database"}


def test_post_without_pre_is_noop() -> None:
    _, post = make_otel_emit("backend")
    asyncio.run(post(_post("Read", "x"), "tu-stray", _ctx()))
    assert _exporter.get_finished_spans() == ()


def test_pre_without_post_keeps_span_open() -> None:
    pre, _ = make_otel_emit("backend")
    asyncio.run(pre(_pre("Read", {"file_path": "f"}), "tu-leak", _ctx()))
    # Span never ended -> not exported.
    assert _exporter.get_finished_spans() == ()


def test_each_factory_isolates_span_state() -> None:
    """Two clients sharing the same tool_use_id don't trip over each other.

    In production every SDK client has its own ``make_otel_emit`` pair,
    so a tool_use_id collision between concurrent clients is fine: each
    pair only sees its own pre/post.
    """
    pre_a, post_a = make_otel_emit("backend")
    pre_b, post_b = make_otel_emit("unit_test")

    async def drive() -> None:
        await pre_a(_pre("Edit", {"file_path": "a.java"}), "shared", _ctx())
        await pre_b(_pre("Edit", {"file_path": "b.java"}), "shared", _ctx())
        await post_a(_post("Edit", "okA"), "shared", _ctx())
        await post_b(_post("Edit", "okB"), "shared", _ctx())

    asyncio.run(drive())
    spans = _exporter.get_finished_spans()
    roles = sorted(dict(s.attributes)["agent.role"] for s in spans)
    assert roles == ["backend", "unit_test"]


def test_missing_tool_use_id_pre_closes_span_immediately() -> None:
    pre, _ = make_otel_emit("po")
    asyncio.run(pre(_pre("Read", {"file_path": "x"}), None, _ctx()))
    spans = _exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "tool.Read"


def test_missing_tool_use_id_post_is_noop() -> None:
    _, post = make_otel_emit("po")
    asyncio.run(post(_post("Read", "x"), None, _ctx()))
    assert _exporter.get_finished_spans() == ()


def test_span_nests_under_active_span() -> None:
    """When an outer span is active, the tool span hangs off it.

    OpenInference's AnthropicInstrumentor opens a generation span around
    each LLM call; the SDK fires PreToolUse hooks inside that span. Our
    tool spans should pick up that parent so Langfuse renders them under
    the LLM call.
    """
    tracer = trace.get_tracer("test_otel_emit_parent")
    pre, post = make_otel_emit("backend")

    async def drive() -> None:
        with tracer.start_as_current_span("llm.generation") as parent:
            await pre(_pre("Read", {"file_path": "f"}), "tu-1", _ctx())
            await post(_post("Read", "ok"), "tu-1", _ctx())
            parent.set_attribute("phase", "test")

    asyncio.run(drive())
    spans = _exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    assert {"tool.Read", "llm.generation"}.issubset(by_name)
    tool_span = by_name["tool.Read"]
    parent_span = by_name["llm.generation"]
    assert tool_span.parent is not None
    assert tool_span.parent.span_id == parent_span.context.span_id


def test_pre_tolerates_missing_tool_input() -> None:
    pre, post = make_otel_emit("backend")

    async def drive() -> None:
        ev = _pre("Read", {})
        del ev["tool_input"]
        await pre(ev, "tu-1", _ctx())
        await post(_post("Read", "ok"), "tu-1", _ctx())

    asyncio.run(drive())
    spans = _exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    # No argv attribute when input is empty/missing.
    assert "tool.input.argv" not in attrs


def test_post_without_returncode_omits_exit_code() -> None:
    pre, post = make_otel_emit("backend")

    async def drive() -> None:
        await pre(_pre("Read", {"file_path": "f"}), "tu-1", _ctx())
        await post(_post("Read", "plain string output"), "tu-1", _ctx())

    asyncio.run(drive())
    spans = _exporter.get_finished_spans()
    attrs = dict(spans[0].attributes)
    assert "tool.exit_code" not in attrs
    assert attrs["tool.output_bytes"] == len("plain string output")
