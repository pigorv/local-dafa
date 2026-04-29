"""UserPromptSubmit hook: re-inject the user request and a one-line spec
summary every ``every_n`` prompts.

Long tool-using sessions tend to drift off the original goal. This hook
counts the role's UserPromptSubmit events and, every Nth call, returns
``additionalContext`` carrying the original ``user_request`` and a short
``spec_summary`` so the model is reminded of the target.
"""
from __future__ import annotations

from claude_agent_sdk.types import (
    HookContext,
    HookJSONOutput,
    UserPromptSubmitHookInput,
)

GOAL_PIN_EVERY_N = 5


def _format(user_request: str, spec_summary: str) -> str:
    body = f"GOAL REMINDER\nUser request: {user_request}"
    if spec_summary:
        body += f"\nSpec summary: {spec_summary}"
    return body


def make_goal_pin(
    user_request: str,
    spec_summary: str = "",
    every_n: int = GOAL_PIN_EVERY_N,
):
    """Return a UserPromptSubmit hook callback with a private turn counter.

    Triggers on the Nth, 2Nth, 3Nth ... prompt (i.e. ``counter % every_n == 0``).
    The very first prompt is not re-pinned because the user's request is
    already part of it; the hook exists to fight drift later in the session.
    """
    if every_n < 1:
        raise ValueError("every_n must be >= 1")
    counter = 0

    async def goal_pin_hook(
        input_data: UserPromptSubmitHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        nonlocal counter
        counter += 1
        if counter % every_n != 0:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _format(user_request, spec_summary),
            }
        }

    return goal_pin_hook
