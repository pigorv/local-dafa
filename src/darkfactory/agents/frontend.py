"""Frontend Worker — SDK-native no-op stub.

The target app is Java backend only (per ARCHITECTURE.md §5.3 and §10). The
stub exists so the Builder Supervisor's routing code can dispatch to a
``frontend`` slice without a special case; ``run_frontend`` short-circuits
without opening an SDK client and produces no patches.

Function shape mirrors the build-stage workers (``make_<role>_client`` +
``async run_<role>``) so the build subgraph dispatcher can call it
uniformly. ``make_frontend_client`` raises ``NotImplementedError`` because
no frontend role is provisioned; the SDK loop is never expected to run.
"""
from __future__ import annotations

from claude_agent_sdk import ClaudeSDKClient

from darkfactory.state import Patch

ROLE = "frontend"

NO_FRONTEND_NOTE = "no frontend work"


def make_frontend_client(
    state_slice: dict,
    *,
    patches_sink: list[Patch] | None = None,
) -> ClaudeSDKClient:
    raise NotImplementedError(
        "frontend role is a no-op stub; call run_frontend(state_slice) instead"
    )


async def run_frontend(state_slice: dict) -> dict:
    return {"patches": [], "note": NO_FRONTEND_NOTE}
