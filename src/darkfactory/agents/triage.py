"""Triage role for issue-driven workflows.

The triage agent is a reasoning-only role: it does not get tools, and it
returns the structured decision consumed by the issue workflow.
"""
from __future__ import annotations

import json
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator

from darkfactory.agents._sdk_common import (
    repo_summary,
    run_to_completion,
)
from darkfactory.agents.compose import ComposeState, compose


class TriageOutput(TypedDict):
    ready_to_build: bool
    clarification_questions: list[str]
    derived_user_request: str
    confidence: Literal["low", "medium", "high"]
    rationale: str


class _TriageOutputModel(BaseModel):
    """Pydantic shape used by ``run_to_completion`` to validate the LLM's JSON.

    Mirrors the public ``TriageOutput`` TypedDict; ``run_triage`` converts an
    instance back to a TypedDict at the function boundary so callers see the
    same dict-shaped value the pre-migration implementation returned.
    """

    ready_to_build: bool
    clarification_questions: list[str] = Field(default_factory=list)
    derived_user_request: str = ""
    confidence: Literal["low", "medium", "high"]
    rationale: str = ""

    @model_validator(mode="after")
    def _check_invariants(self) -> "_TriageOutputModel":
        if len(self.clarification_questions) > 3:
            raise ValueError(
                "clarification_questions must contain at most 3 items"
            )
        if self.ready_to_build and self.clarification_questions:
            raise ValueError(
                "must not ask questions when ready_to_build=true"
            )
        return self


def _state_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _comment_record(comment: Any) -> dict[str, str] | None:
    if isinstance(comment, str):
        body = comment.strip()
        return {"author": "", "created_at": "", "body": body} if body else None
    body = str(_state_value(comment, "body", "") or "").strip()
    if not body:
        return None
    return {
        "author": str(_state_value(comment, "author", "") or ""),
        "created_at": str(
            _state_value(comment, "created_at", "")
            or _state_value(comment, "createdAt", "")
            or ""
        ),
        "body": body,
    }


def _user_message(state_slice: dict) -> str:
    issue = state_slice.get("issue") or {}
    title = state_slice.get("issue_title") or _state_value(issue, "title", "")
    body = state_slice.get("issue_body") or _state_value(issue, "body", "")
    comments = [
        rec
        for rec in (_comment_record(c) for c in state_slice.get("issue_comments") or [])
        if rec is not None
    ]
    ctx_blob = repo_summary(state_slice.get("repo_context"))
    return (
        f"issue_title:\n{title}\n\n"
        f"issue_body:\n{body}\n\n"
        f"issue_comments (JSON, chronological — each item has author, "
        f"created_at, body):\n{json.dumps(comments, indent=2)}\n\n"
        f"repo_context:\n{ctx_blob}\n\n"
        "Decide whether this issue is ready for Dark Factory to build."
    )


async def run_triage(state_slice: dict) -> TriageOutput:
    compose_state = ComposeState.from_mapping(state_slice)
    async with compose(
        "triage",
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=_TriageOutputModel)
        assert isinstance(result, _TriageOutputModel)
        return TriageOutput(
            ready_to_build=result.ready_to_build,
            clarification_questions=list(result.clarification_questions),
            derived_user_request=result.derived_user_request,
            confidence=result.confidence,
            rationale=result.rationale,
        )
