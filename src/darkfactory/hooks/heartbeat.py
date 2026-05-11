"""Stop hook: emit a Temporal activity heartbeat at each SDK turn boundary.

Per ARCHITECTURE.md §5.6, long-running stage activities drive an SDK loop
whose individual LLM calls can each take minutes. Temporal's
``heartbeat_timeout`` would otherwise mark the activity dead while the
agent is still busy. The Claude Agent SDK fires its ``Stop`` hook once
per assistant turn boundary, which is the natural cadence to reassure
Temporal that the activity is still alive.

The hook is a no-op when not running inside a Temporal activity worker
(unit tests, ad-hoc CLI runs from the orchestrator), so it can be
attached unconditionally to every ``ClaudeSDKClient`` produced by
``compose(role, ...)`` without breaking standalone usage.
"""
from __future__ import annotations

from claude_agent_sdk.types import HookContext, HookJSONOutput, StopHookInput
from temporalio import activity

DEFAULT_DETAIL = "sdk: turn boundary"


def make_heartbeat(detail: str = DEFAULT_DETAIL):
    """Return a Stop hook callback that calls ``activity.heartbeat()``.

    Parameters
    ----------
    detail:
        String passed as the heartbeat payload. Surfaces in Temporal Web
        UI's activity heartbeat panel; useful for distinguishing roles
        when several SDK clients run inside the same activity.
    """

    async def heartbeat_hook(
        input_data: StopHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        if activity.in_activity():
            activity.heartbeat(detail)
        return {}

    return heartbeat_hook
