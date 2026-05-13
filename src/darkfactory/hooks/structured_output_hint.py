"""PostToolUse hook: hint the model when ``StructuredOutput`` was wrapped.

The SDK's ``StructuredOutput`` synthetic tool rejects an input that does not
match the declared JSON Schema. Its error message (``must have required
property X … must NOT have additional properties``) is verbatim ajv output
and does not tell the model *why* both clauses can fire together. In the
field this has produced retry storms where the model keeps re-sending
``{"output": {...}}`` and never connects the dots.

This hook detects a single-outer-key wrap on a failed ``StructuredOutput``
call and appends an ``additionalContext`` hint explaining the diagnosis,
so the model recovers on the next turn instead of looping.

Note: ``_unwrap_singleton`` in ``agents/_sdk_common.py`` silently fixes the
common case before the payload reaches role code. This hook only fires when
the wrap shape would otherwise be visible to the model (i.e., the SDK's own
validator rejected the call). It is intentionally additive.
"""
from __future__ import annotations

import json as _json
from typing import Any

from claude_agent_sdk.types import (
    HookContext,
    HookJSONOutput,
    PostToolUseHookInput,
)

STRUCTURED_OUTPUT_TOOL_NAME = "StructuredOutput"

HINT_TEMPLATE = (
    "The previous {tool} call wrapped the payload in a single outer key "
    "(e.g. `{{\"output\": {{...}}}}`). The SDK requires the schema's "
    "fields directly as the tool input — unwrap that outer key and resend."
)


def _is_wrapped_singleton(tool_input: Any) -> bool:
    if not isinstance(tool_input, dict) or len(tool_input) != 1:
        return False
    only_value = next(iter(tool_input.values()))
    return isinstance(only_value, dict)


def _looks_like_validation_error(tool_response: Any) -> bool:
    """Heuristic: tool_response indicates the SDK rejected the input."""
    if isinstance(tool_response, dict):
        if tool_response.get("is_error") is True:
            return True
        try:
            text = _json.dumps(tool_response, default=str)
        except (TypeError, ValueError):
            text = repr(tool_response)
    elif isinstance(tool_response, (list, tuple)):
        text = "\n".join(
            _json.dumps(part, default=str) if isinstance(part, dict) else str(part)
            for part in tool_response
        )
    elif isinstance(tool_response, str):
        text = tool_response
    else:
        text = str(tool_response or "")
    lowered = text.lower()
    return (
        "must have required property" in lowered
        or "must not have additional properties" in lowered
        or "does not match required schema" in lowered
    )


def make_structured_output_hint():
    """Return a PostToolUse hook that hints at outer-key wrap failures."""

    async def structured_output_hint_hook(
        input_data: PostToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        if input_data.get("tool_name") != STRUCTURED_OUTPUT_TOOL_NAME:
            return {}
        if not _is_wrapped_singleton(input_data.get("tool_input")):
            return {}
        if not _looks_like_validation_error(input_data.get("tool_response")):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": HINT_TEMPLATE.format(
                    tool=STRUCTURED_OUTPUT_TOOL_NAME
                ),
            }
        }

    return structured_output_hint_hook
