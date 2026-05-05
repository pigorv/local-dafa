"""Spec Reviewer — SDK-driven discovery role.

Reviews the spec produced by the Architect against the original stories
and either approves it or returns targeted edits keyed by story id.
No tools, no MCP servers; reasoning-only role with structured output.
"""
from __future__ import annotations

import json

from claude_agent_sdk import ClaudeSDKClient, HookMatcher
from pydantic import BaseModel, Field

from darkfactory.agents._sdk_common import load_prompt, run_to_completion
from darkfactory.hooks.call_cap import make_call_cap
from darkfactory.hooks.goal_pin import make_goal_pin
from darkfactory.hooks.loop_breaker import make_loop_breaker
from darkfactory.llm_factory import build_options


class ReviewDecisionModel(BaseModel):
    """Spec Reviewer decision — approves the spec or returns targeted edits."""

    approved: bool
    reason: str = ""
    edits: dict = Field(default_factory=dict)


def _user_message(state_slice: dict) -> str:
    stories = state_slice.get("stories", []) or []
    spec = state_slice.get("spec", []) or []
    return (
        f"User stories (JSON):\n{json.dumps(stories, indent=2)}\n\n"
        f"Spec slices (JSON):\n{json.dumps(spec, indent=2)}\n\n"
        "Review the spec against the stories; approve or return targeted edits."
    )


def make_spec_reviewer_client(state_slice: dict) -> ClaudeSDKClient:
    user_request = state_slice.get("user_request", "") or ""
    options = build_options(
        "spec_reviewer",
        system_prompt=load_prompt("spec_reviewer"),
        allowed_tools=[],
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[make_loop_breaker(), make_call_cap()]),
            ],
            "UserPromptSubmit": [
                HookMatcher(hooks=[make_goal_pin(user_request)]),
            ],
        },
        mcp_servers={},
    )
    return ClaudeSDKClient(options=options)


async def run_spec_reviewer(state_slice: dict) -> ReviewDecisionModel:
    async with make_spec_reviewer_client(state_slice) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=ReviewDecisionModel)
        assert isinstance(result, ReviewDecisionModel)
        return result
