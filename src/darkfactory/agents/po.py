"""Product Owner — SDK-driven discovery role.

Translates a user request plus repo context into a list of user stories.
No tools, no MCP servers; reasoning-only role with structured output.
"""
from __future__ import annotations

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


class UserStoryModel(BaseModel):
    id: str = Field(description="Stable id like US-1.")
    title: str
    as_a: str
    i_want: str
    so_that: str
    acceptance_criteria: list[str] = Field(default_factory=list)


class POOutput(BaseModel):
    """Product Owner output: a list of user stories."""

    stories: list[UserStoryModel] = Field(default_factory=list)


def _user_message(state_slice: dict) -> str:
    user_request = state_slice.get("user_request", "") or ""
    ctx_blob = repo_summary(state_slice.get("repo_context"))
    return (
        f"User request:\n{user_request}\n\n"
        f"Repo context:\n{ctx_blob}\n\n"
        "Produce user stories."
    )


def make_po_client(state_slice: dict) -> ClaudeSDKClient:
    user_request = state_slice.get("user_request", "") or ""
    options = build_options(
        "po",
        system_prompt=load_prompt("po"),
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


async def run_po(state_slice: dict) -> POOutput:
    async with make_po_client(state_slice) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=POOutput)
        assert isinstance(result, POOutput)
        return result
