"""Architect — SDK-driven discovery role.

Turns a list of user stories into a topo-sortable list of spec slices.
No tools, no MCP servers; reasoning-only role with structured output.
"""
from __future__ import annotations

import json

from claude_agent_sdk import ClaudeSDKClient, HookMatcher
from pydantic import BaseModel, Field

from darkfactory.agents._sdk_common import (
    load_prompt,
    repo_summary,
    run_to_completion,
)
from darkfactory.hooks.call_cap import make_call_cap
from darkfactory.hooks.goal_pin import make_goal_pin
from darkfactory.hooks.loop_breaker import make_loop_breaker
from darkfactory.llm_factory import build_options


class SpecSliceModel(BaseModel):
    story_id: str
    approach: str
    affected_files: list[str] = Field(default_factory=list)
    new_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class ArchitectOutput(BaseModel):
    """Architect output: a topo-sortable list of spec slices."""

    spec: list[SpecSliceModel] = Field(default_factory=list)


def _user_message(state_slice: dict) -> str:
    user_request = state_slice.get("user_request", "") or ""
    stories = state_slice.get("stories", []) or []
    ctx_blob = repo_summary(state_slice.get("repo_context"))
    return (
        f"User request:\n{user_request}\n\n"
        f"Repo context:\n{ctx_blob}\n\n"
        f"User stories (JSON):\n{json.dumps(stories, indent=2)}\n\n"
        "Produce a topo-sortable list of SpecSlices with depends_on."
    )


def make_architect_client(state_slice: dict) -> ClaudeSDKClient:
    user_request = state_slice.get("user_request", "") or ""
    options = build_options(
        "architect",
        system_prompt=load_prompt("architect"),
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


async def run_architect(state_slice: dict) -> ArchitectOutput:
    async with make_architect_client(state_slice) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=ArchitectOutput)
        assert isinstance(result, ArchitectOutput)
        return result
