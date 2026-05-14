"""PR Creator - SDK-driven PR publication role.

The activity wrapper deterministically checks for an existing PR before
this role runs, so the agent only ever needs to push the feature branch
and open a new PR. The role can read/search the repo and can run a
narrow set of git/gh commands through ``sandbox_bash``; it cannot edit
files, merge, or use the built-in Bash tool.

Output is a structured ``PRCreatorOutput`` enforced by the SDK's
``output_format``; the activity translates that into the ``pr_url``
state channel.
"""
from __future__ import annotations

import json
from typing import Any

from darkfactory.agents._sdk_common import (
    PRCreatorOutput,
    render_role_user_message,
    run_to_completion,
)
from darkfactory.agents.compose import ComposeState, compose

ROLE = "pr_creator"


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


def _approval_line(state_slice: dict) -> str:
    rev = state_slice.get("approved_spec_rev")
    record = state_slice.get("approval_record") or {}
    if not rev or not record:
        return ""
    author = record.get("author", "") if isinstance(record, dict) else getattr(record, "author", "")
    approved_at = (
        record.get("approved_at", "") if isinstance(record, dict) else getattr(record, "approved_at", "")
    )
    return f"Spec rev {rev} approved by @{author} at {approved_at}"


def _closes_line(state_slice: dict) -> str:
    issue = state_slice.get("issue")
    if not issue:
        return ""
    number = issue.get("number") if isinstance(issue, dict) else getattr(issue, "number", None)
    if not number:
        return ""
    return f"Closes #{int(number)}"


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _render_user_prompt(state_slice: dict) -> str:
    return render_role_user_message(
        ROLE,
        user_request=str(state_slice.get("user_request") or ""),
        workflow_id=_wf_id(state_slice),
        feature_branch=_feature_branch(state_slice),
        approved_spec_rev=str(state_slice.get("approved_spec_rev") or ""),
        approval_line=_approval_line(state_slice),
        closes_line=_closes_line(state_slice),
        approved_spec_markdown=str(state_slice.get("approved_spec_markdown") or ""),
        spec=_json(state_slice.get("spec") or []),
        verify_summary=_json(state_slice.get("verify_summary") or {}),
    )


async def run_pr_creator(state_slice: dict) -> dict[str, Any]:
    """Run the PR Creator and return its structured output as a dict.

    Raises ``ParseError`` if the model cannot produce a valid
    ``PRCreatorOutput`` after one retry. The workflow's
    ``non_retryable_error_types=["ParseError"]`` ensures Temporal does
    not loop on parse failures.
    """
    compose_state = ComposeState.from_mapping(state_slice)
    rendered = _render_user_prompt(state_slice)
    async with compose(
        ROLE,
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(rendered)
        output = await run_to_completion(client, expect=PRCreatorOutput)
    return output.model_dump()
