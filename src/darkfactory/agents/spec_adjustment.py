"""Spec Adjustment — SDK-driven correction role.

Reads the most recent verify failure (test_results + findings) plus the
current spec and decides between two corrective branches:

1. **patch_code** — the code is wrong; emit a unified diff for the
   responsible worker.
2. **update_spec** — the spec is wrong; emit a mutated `SpecSlice`.

No tools, no MCP servers; reasoning-only role with structured output.
The workflow code in `runtime/workflow.py` consumes the structured
output directly to drive routing and state mutations.
"""
from __future__ import annotations

from typing import Literal, Optional

from claude_agent_sdk import ClaudeSDKClient, HookMatcher
from pydantic import BaseModel

from darkfactory.agents._sdk_common import load_prompt, run_to_completion
from darkfactory.agents.architect import SpecSliceModel
from darkfactory.hooks.call_cap import make_call_cap
from darkfactory.hooks.goal_pin import make_goal_pin
from darkfactory.hooks.loop_breaker import make_loop_breaker
from darkfactory.hooks.otel_emit import make_otel_emit
from darkfactory.llm_factory import build_options

WorkerName = Literal["backend", "database", "unit_test", "frontend"]
Decision = Literal["patch_code", "update_spec"]


class SpecAdjustmentOutput(BaseModel):
    """Spec Adjustment decision — exactly one of two corrective branches."""

    decision: Decision
    rationale: str = ""

    target_worker: Optional[WorkerName] = None
    slice_id: Optional[str] = None
    path: Optional[str] = None
    diff: Optional[str] = None

    updated_slice: Optional[SpecSliceModel] = None


def _user_message(state_slice: dict) -> str:
    lines: list[str] = []
    for tr in state_slice.get("test_results") or []:
        if tr.get("returncode", 0) != 0 or tr.get("failed", 0) > 0:
            errs = "\n".join((tr.get("errors") or [])[:5])
            lines.append(
                f"[{tr.get('runner')}] rc={tr.get('returncode')} "
                f"passed={tr.get('passed')} failed={tr.get('failed')}\n{errs}"
            )
    for f in (state_slice.get("findings") or [])[:10]:
        if f.get("severity") in ("error", "critical"):
            lines.append(
                f"[{f.get('tool')}] {f.get('severity')} {f.get('file')}:"
                f"{f.get('line')} {f.get('rule')} — {f.get('message')}"
            )
    spec = state_slice.get("spec") or []
    cur = state_slice.get("current_slice") or (spec[0]["story_id"] if spec else "")
    return (
        f"current_slice={cur}\n"
        f"spec={spec}\n\n"
        "Verify failures:\n" + ("\n".join(lines) or "(no detail captured)")
    )


def make_spec_adjustment_client(state_slice: dict) -> ClaudeSDKClient:
    user_request = state_slice.get("user_request", "") or ""
    otel_pre, otel_post = make_otel_emit("spec_adjustment")
    options = build_options(
        "spec_adjustment",
        system_prompt=load_prompt("spec_adjustment"),
        allowed_tools=[],
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[make_loop_breaker(), make_call_cap(), otel_pre]),
            ],
            "PostToolUse": [
                HookMatcher(hooks=[otel_post]),
            ],
            "UserPromptSubmit": [
                HookMatcher(hooks=[make_goal_pin(user_request)]),
            ],
        },
        mcp_servers={},
    )
    return ClaudeSDKClient(options=options)


async def run_spec_adjustment(state_slice: dict) -> SpecAdjustmentOutput:
    async with make_spec_adjustment_client(state_slice) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=SpecAdjustmentOutput)
        assert isinstance(result, SpecAdjustmentOutput)
        return result
