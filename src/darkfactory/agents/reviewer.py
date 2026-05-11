"""Reviewer - SDK-driven review role.

Reviews the patches produced by Build plus the Verify summary, then emits a
small structured summary for the human gate. It is a reasoning-only Haiku
role: no built-in tools, no MCP server, no shell access.
"""
from __future__ import annotations

import json

from typing import Any

from darkfactory.agents._sdk_common import run_to_completion
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.state import ReviewerSummary

ROLE = "reviewer"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _user_message(state_slice: dict) -> str:
    patches = state_slice.get("patches") or []
    verify_summary = state_slice.get("verify_summary") or {}
    predicate_coverage = _field(verify_summary, "predicate_coverage", []) or []
    findings = state_slice.get("findings") or []
    test_results = state_slice.get("test_results") or []
    audit_log = state_slice.get("audit_log") or []
    attempt_log = state_slice.get("attempt_log") or []
    return (
        "Review the implementation for merge readiness.\n\n"
        f"User request:\n{state_slice.get('user_request', '') or ''}\n\n"
        f"Pull request URL:\n{state_slice.get('pr_url', '') or ''}\n\n"
        "Implementation brief:\n"
        f"{json.dumps(_jsonable(state_slice.get('implementation_brief') or {}), indent=2)}\n\n"
        "Approved spec markdown:\n"
        f"{state_slice.get('approved_spec_markdown', '') or ''}\n\n"
        f"Patches:\n{json.dumps(_jsonable(patches), indent=2)}\n\n"
        f"Verify summary:\n{json.dumps(_jsonable(verify_summary), indent=2)}\n\n"
        "Predicate coverage:\n"
        f"{json.dumps(_jsonable(predicate_coverage), indent=2)}\n\n"
        f"Test results:\n{json.dumps(_jsonable(test_results), indent=2)}\n\n"
        f"Findings:\n{json.dumps(_jsonable(findings), indent=2)}\n\n"
        f"Audit log:\n{json.dumps(_jsonable(audit_log), indent=2)}\n\n"
        f"Attempt log:\n{json.dumps(_jsonable(attempt_log), indent=2)}"
    )


async def run_reviewer(state_slice: dict) -> ReviewerSummary:
    compose_state = ComposeState.from_mapping(state_slice)
    async with compose(
        ROLE,
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=ReviewerSummary)
        assert isinstance(result, ReviewerSummary)
        return result
