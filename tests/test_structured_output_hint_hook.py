"""Tests for the structured_output_hint PostToolUse hook."""
from __future__ import annotations

import asyncio

from darkfactory.hooks.structured_output_hint import (
    STRUCTURED_OUTPUT_TOOL_NAME,
    make_structured_output_hint,
)


def _run(hook, input_data):
    return asyncio.run(hook(input_data, None, None))


def _wrapped_payload() -> dict:
    return {"output": {"current_understanding": "x", "proposed_design": "y"}}


def _error_response() -> dict:
    return {
        "is_error": True,
        "content": (
            "Output does not match required schema: root: must have required "
            "property 'current_understanding', root: must NOT have additional "
            "properties"
        ),
    }


def test_hook_emits_hint_on_wrapped_singleton_validation_error() -> None:
    hook = make_structured_output_hint()
    out = _run(
        hook,
        {
            "tool_name": STRUCTURED_OUTPUT_TOOL_NAME,
            "tool_input": _wrapped_payload(),
            "tool_response": _error_response(),
        },
    )
    assert "hookSpecificOutput" in out
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "unwrap" in ctx.lower()
    assert STRUCTURED_OUTPUT_TOOL_NAME in ctx


def test_hook_emits_hint_when_error_is_a_plain_string() -> None:
    hook = make_structured_output_hint()
    out = _run(
        hook,
        {
            "tool_name": STRUCTURED_OUTPUT_TOOL_NAME,
            "tool_input": _wrapped_payload(),
            "tool_response": (
                "Output does not match required schema: root: must have "
                "required property 'foo'"
            ),
        },
    )
    assert "hookSpecificOutput" in out


def test_hook_silent_for_non_structured_output_tool() -> None:
    hook = make_structured_output_hint()
    out = _run(
        hook,
        {
            "tool_name": "Read",
            "tool_input": _wrapped_payload(),
            "tool_response": _error_response(),
        },
    )
    assert out == {}


def test_hook_silent_when_response_is_not_an_error() -> None:
    hook = make_structured_output_hint()
    out = _run(
        hook,
        {
            "tool_name": STRUCTURED_OUTPUT_TOOL_NAME,
            "tool_input": _wrapped_payload(),
            "tool_response": {"is_error": False, "content": "ok"},
        },
    )
    assert out == {}


def test_hook_silent_when_input_is_not_a_singleton_wrap() -> None:
    hook = make_structured_output_hint()
    out = _run(
        hook,
        {
            "tool_name": STRUCTURED_OUTPUT_TOOL_NAME,
            "tool_input": {
                "current_understanding": "x",
                "proposed_design": "y",
            },
            "tool_response": _error_response(),
        },
    )
    assert out == {}


def test_hook_silent_when_singleton_value_is_not_a_dict() -> None:
    # Real schemas can have one-required-field shapes where the value is a
    # list (e.g. predicate_coverage). The hook must not fire for those.
    hook = make_structured_output_hint()
    out = _run(
        hook,
        {
            "tool_name": STRUCTURED_OUTPUT_TOOL_NAME,
            "tool_input": {"predicate_coverage": [{"id": "p1"}]},
            "tool_response": _error_response(),
        },
    )
    assert out == {}
