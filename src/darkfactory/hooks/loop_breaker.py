"""PreToolUse hook: deny when the recent tool-call window contains a repeating 3-gram.

Each PreToolUse event is hashed as `tool_name|sorted-json(tool_input)` and
appended to a per-client ring buffer of size ``window``. If any 3-gram in the
window appears more than once, the hook returns a deny verdict with a
reconsider message; otherwise it allows the call. Supports R7 (inner-loop
hard caps).
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk.types import HookContext, HookJSONOutput, PreToolUseHookInput

RECONSIDER_TEXT = (
    "You appear to be repeating the same tool-call pattern. "
    "Stop, reconsider your approach, and try a different strategy."
)


def hash_tool_call(tool_name: str, tool_input: dict[str, Any] | None) -> str:
    args = tool_input or {}
    try:
        args_key = json.dumps(args, sort_keys=True, default=str)
    except TypeError:
        args_key = repr(sorted(args.items())) if isinstance(args, dict) else repr(args)
    return f"{tool_name}|{args_key}"


def detect_repeating_triplet(hashes: list[str]) -> bool:
    if len(hashes) < 3:
        return False
    seen: set[tuple[str, str, str]] = set()
    for i in range(len(hashes) - 2):
        tri = (hashes[i], hashes[i + 1], hashes[i + 2])
        if tri in seen:
            return True
        seen.add(tri)
    return False


def make_loop_breaker(window: int = 5):
    """Return a PreToolUse hook callback with private window state.

    The returned callable conforms to ``claude_agent_sdk.HookCallback``: it
    takes (input, tool_use_id, context) and returns a ``HookJSONOutput``.
    """
    history: list[str] = []

    async def loop_breaker_hook(
        input_data: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        history.append(hash_tool_call(input_data["tool_name"], input_data.get("tool_input")))
        if len(history) > window:
            del history[: len(history) - window]
        if not detect_repeating_triplet(history):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": RECONSIDER_TEXT,
            }
        }

    return loop_breaker_hook
