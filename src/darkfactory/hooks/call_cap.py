"""PreToolUse hook: deny once a per-client tool-call counter exceeds ``cap``.

Replaces the LangChain ``ModelCallLimitMiddleware``. Each invocation
increments the counter; once strictly greater than ``cap`` the hook returns
a deny verdict, asking the model to stop and summarise. Supports R7.
"""
from __future__ import annotations

from claude_agent_sdk.types import HookContext, HookJSONOutput, PreToolUseHookInput

CALL_CAP_DEFAULT = 80


def make_call_cap(cap: int = CALL_CAP_DEFAULT):
    """Return a PreToolUse hook callback with a private call counter."""
    counter = 0

    async def call_cap_hook(
        input_data: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        nonlocal counter
        counter += 1
        if counter <= cap:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Tool-call cap of {cap} exceeded for this agent. "
                    "Stop, summarise progress and outstanding work, then "
                    "produce your final structured response."
                ),
            }
        }

    return call_cap_hook
