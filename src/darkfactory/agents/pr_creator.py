"""PR Creator - SDK-driven PR publication role.

Finds or creates the workflow pull request before the final Reviewer pass and
human merge gate. The role can read/search the repo and can run a narrow set
of git/gh commands through ``sandbox_bash``; it cannot edit files, merge, or
use the built-in Bash tool.
"""
from __future__ import annotations

import json
import re
from typing import Any

from darkfactory.agents._sdk_common import ParseError, run_to_completion
from darkfactory.agents.compose import ComposeState, compose

ROLE = "pr_creator"

# argv[0] allowlist still consumed by tests/test_tools_shell_allowlist.py and
# tests/test_agents_workers.py; kept as a code-declared invariant alongside
# the manifest-declared per-role policies.
PR_CREATOR_ALLOWLIST: frozenset[str] = frozenset({"git", "gh", "cat", "ls"})

_PR_URL_RE = re.compile(r"https://github\.com/[^\s\"'<>]+/pull/\d+")


def _wf_id(state_slice: dict) -> str:
    return str(
        state_slice.get("wf_id")
        or state_slice.get("task_id")
        or state_slice.get("workflow_id")
        or ""
    )


def _feature_branch(state_slice: dict) -> str:
    if state_slice.get("feature_branch"):
        return str(state_slice["feature_branch"])
    wf_id = _wf_id(state_slice)
    return f"agent/{wf_id}" if wf_id else "agent/unknown"


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _user_message(state_slice: dict) -> str:
    branch = _feature_branch(state_slice)
    wf_id = _wf_id(state_slice)
    spec = state_slice.get("spec") or []
    approved_spec_markdown = state_slice.get("approved_spec_markdown") or ""
    approved_spec_rev = state_slice.get("approved_spec_rev")
    approval_record = state_slice.get("approval_record") or {}
    verify_summary = state_slice.get("verify_summary") or {}
    approval_line = ""
    if approved_spec_rev and approval_record:
        approval_line = (
            f"Spec rev {approved_spec_rev} approved by "
            f"@{approval_record.get('author', '')} at "
            f"{approval_record.get('approved_at', '')}"
        )

    return (
        "Create or find the pull request for this approved workflow.\n\n"
        f"Workflow ID: {wf_id}\n"
        f"Feature branch: {branch}\n"
        f"User request:\n{state_slice.get('user_request', '') or ''}\n\n"
        "Required idempotency step:\n"
        f"- Run `gh pr list --head {branch}` before creating anything.\n"
        "- If that command shows an existing PR, return its URL and stop.\n"
        f"- Otherwise run `git push origin {branch}` and then create a PR "
        "with `gh pr create`.\n\n"
        "Use this material for the PR title and body:\n"
        f"Approved spec rev: {approved_spec_rev or ''}\n"
        f"Approval line:\n{approval_line}\n\n"
        f"Approved spec markdown:\n{approved_spec_markdown}\n\n"
        f"Spec:\n{_json(spec)}\n\n"
        f"Verify summary:\n{_json(verify_summary)}\n\n"
        "Return exactly the pull request URL as plain text. No markdown, "
        "no JSON, no commentary."
    )


def _extract_pr_url(text: str) -> str:
    match = _PR_URL_RE.search(text.strip())
    if match is None:
        raise ParseError("PR Creator did not return a GitHub pull request URL")
    return match.group(0)


async def run_pr_creator(state_slice: dict) -> str:
    compose_state = ComposeState.from_mapping(state_slice)
    async with compose(
        ROLE,
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client)
        text = result.get("text", "") if isinstance(result, dict) else ""
        return _extract_pr_url(text)
