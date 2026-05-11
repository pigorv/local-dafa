"""Triage role for issue-driven workflows.

Reasoning-only role that decides whether a GitHub issue is ready for
Dark Factory to build. The structured-output shape is defined by
``schemas/triage_output.json`` (canonical, hand-edited) and enforced by
the SDK's ``output_format``; for parse-failure tolerance the runtime
drives the SDK via ``run_to_completion`` against a Pydantic mirror
(``_TriageParsedOutput``), so a single transient empty structured
response is retried once before raising ``ParseError`` — keeping triage
aligned with the other SDK-backed roles.

Two transforms are applied on top of the structured output:

- ``_ensure_defaults`` backfills optional fields so downstream consumers
  always see the full ``TriageOutput`` shape (also covered by the
  Pydantic defaults; this is a belt-and-suspenders pass for raw dicts).
- ``_enforce_ready_invariant`` resolves the cross-field invariants JSON
  Schema can't express. ``ready_to_build=true ∧ questions≠[]`` is
  reconciled by demoting ``ready_to_build``; the degenerate
  ``ready_to_build=false ∧ questions=[]`` state is filled with a generic
  clarification ask so the workflow's clarify path always has something
  to post back.

Untrusted-input handling for ``issue_body`` / ``issue_comments`` /
``repo_context`` is intentionally prompt-honor-system only: the prompt
labels these inputs as data not instructions and there is no runtime
content scanner.
"""
from __future__ import annotations

import json
from string import Template
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from darkfactory.agents._sdk_common import (
    ParseError,
    repo_summary,
    run_to_completion,
)
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.registry import get_default_registry, resolve_prompt_path


class TriageOutput(TypedDict):
    ready_to_build: bool
    clarification_questions: list[str]
    derived_user_request: str
    confidence: Literal["low", "medium", "high"]
    rationale: str


class _TriageParsedOutput(BaseModel):
    """Pydantic mirror of ``schemas/triage_output.json``.

    Exists so ``run_to_completion`` can drive the one-retry parse loop
    other SDK-backed roles use. Field defaults match
    ``_ensure_defaults`` — they only fire when the structured output is
    partial, which is rare since the schema is enforced server-side.
    Extra fields are ignored rather than rejected so the runtime stays
    forward-compatible with schema extensions.
    """

    model_config = ConfigDict(extra="ignore")

    ready_to_build: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    derived_user_request: str = ""
    confidence: Literal["low", "medium", "high"] = "low"
    rationale: str = ""


_DEFAULT_CLARIFICATION_QUESTION = (
    "Could you confirm the exact behaviour you want and any constraints "
    "(target surface, expected outputs, edge cases) we should respect?"
)


def _ensure_defaults(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    out.setdefault("ready_to_build", False)
    out.setdefault("clarification_questions", [])
    out.setdefault("derived_user_request", "")
    out.setdefault("confidence", "low")
    out.setdefault("rationale", "")
    return out


def _enforce_ready_invariant(data: dict[str, Any]) -> dict[str, Any]:
    """Resolve the cross-field invariants JSON Schema can't express.

    - ``ready_to_build=true ∧ clarification_questions≠[]`` → demote
      ``ready_to_build`` to ``false`` (the safer reading).
    - ``ready_to_build=false ∧ clarification_questions=[]`` → backfill a
      generic clarification question. The clarify path in the issue
      workflow posts the questions list to GitHub, so an empty list
      there would emit an empty comment and stall the loop.
    """
    out = dict(data)
    questions = list(out.get("clarification_questions") or [])
    if out.get("ready_to_build") and questions:
        out["ready_to_build"] = False
    elif not out.get("ready_to_build") and not questions:
        out["clarification_questions"] = [_DEFAULT_CLARIFICATION_QUESTION]
    return out


def normalize_triage_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Public entry-point for the defaults + invariant transforms.

    Exposed so tests can exercise the shim layer without driving an SDK
    client.
    """
    return _enforce_ready_invariant(_ensure_defaults(raw))


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


def _format_issue_comments(comments: Any) -> str:
    records = [
        rec
        for rec in (_comment_record(c) for c in comments or [])
        if rec is not None
    ]
    return json.dumps(records, indent=2)


def _render_user_prompt(state_slice: dict) -> str:
    manifest = get_default_registry().get("triage")
    template_text = resolve_prompt_path(manifest.llm.prompt_path).read_text(
        encoding="utf-8"
    )
    issue = state_slice.get("issue") or {}
    title = state_slice.get("issue_title") or _state_value(issue, "title", "") or ""
    body = state_slice.get("issue_body") or _state_value(issue, "body", "") or ""
    return Template(template_text).safe_substitute(
        issue_title=str(title),
        issue_body=str(body),
        issue_comments=_format_issue_comments(state_slice.get("issue_comments")),
        repo_context=repo_summary(state_slice.get("repo_context")),
    )


def _resolve_task_id(state_slice: dict) -> str:
    return str(
        state_slice.get("task_id")
        or state_slice.get("wf_id")
        or state_slice.get("workflow_id")
        or ""
    )


async def run_triage(state_slice: dict) -> TriageOutput:
    compose_state = ComposeState.task_only(_resolve_task_id(state_slice))
    rendered = _render_user_prompt(state_slice)
    async with compose(
        "triage",
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(rendered)
        parsed = await run_to_completion(client, expect=_TriageParsedOutput)
    if not isinstance(parsed, _TriageParsedOutput):
        raise ParseError("Triage returned unexpected output shape")
    normalized = normalize_triage_output(parsed.model_dump())
    return TriageOutput(
        ready_to_build=bool(normalized["ready_to_build"]),
        clarification_questions=list(normalized["clarification_questions"]),
        derived_user_request=str(normalized["derived_user_request"]),
        confidence=normalized["confidence"],
        rationale=str(normalized["rationale"]),
    )
