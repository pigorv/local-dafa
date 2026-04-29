"""Code Quality - SDK-driven review role.

Reviews the patches produced by Build plus the Verify summary, then emits a
small structured summary for the human gate. It is a reasoning-only Haiku
role: no built-in tools, no MCP server, no shell access.
"""
from __future__ import annotations

import json

from claude_agent_sdk import ClaudeSDKClient, HookMatcher

from darkfactory.agents._sdk_common import load_prompt, run_to_completion
from darkfactory.hooks.call_cap import make_call_cap
from darkfactory.hooks.goal_pin import make_goal_pin
from darkfactory.hooks.loop_breaker import make_loop_breaker
from darkfactory.hooks.otel_emit import make_otel_emit
from darkfactory.llm_factory import build_options
from darkfactory.state import CodeQualitySummary

ROLE = "code_quality"


def _user_message(state_slice: dict) -> str:
    patches = state_slice.get("patches") or []
    verify_summary = state_slice.get("verify_summary") or {}
    findings = state_slice.get("findings") or []
    test_results = state_slice.get("test_results") or []
    return (
        "Review the implementation for merge readiness.\n\n"
        f"User request:\n{state_slice.get('user_request', '') or ''}\n\n"
        f"Patches:\n{json.dumps(patches, indent=2)}\n\n"
        f"Verify summary:\n{json.dumps(verify_summary, indent=2)}\n\n"
        f"Test results:\n{json.dumps(test_results, indent=2)}\n\n"
        f"Findings:\n{json.dumps(findings, indent=2)}"
    )


def make_code_quality_client(state_slice: dict) -> ClaudeSDKClient:
    user_request = state_slice.get("user_request", "") or ""
    otel_pre, otel_post = make_otel_emit(ROLE)
    options = build_options(
        ROLE,
        system_prompt=load_prompt(ROLE),
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


async def run_code_quality(state_slice: dict) -> CodeQualitySummary:
    async with make_code_quality_client(state_slice) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=CodeQualitySummary)
        assert isinstance(result, CodeQualitySummary)
        return result
