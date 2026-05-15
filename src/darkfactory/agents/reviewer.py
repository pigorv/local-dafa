"""Reviewer - SDK-driven read-only review role.

Reviews the produced PR, build traceability, patches, and Verify summary,
then emits a structured summary for the human gate. The output shape is
defined by ``schemas/reviewer_output.json`` and enforced by the SDK's
``output_format``; this module validates and normalizes cross-field
invariants before returning the state payload.
"""
from __future__ import annotations

import json

from typing import Any

from pydantic import ValidationError

from darkfactory.agents._sdk_common import (
    ParseError,
    _drain,
    render_role_user_message,
    repo_summary,
    role_turn_span,
    stamp_turn_usage,
)
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.state import ReviewerSummary
from opentelemetry import trace

ROLE = "reviewer"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _json_block(value: Any) -> str:
    return json.dumps(_jsonable(value), indent=2)


def _render_user_prompt(state_slice: dict) -> str:
    return render_role_user_message(
        ROLE,
        user_request=state_slice.get("user_request", "") or "",
        pr_url=state_slice.get("pr_url", "") or "",
        repo_context=repo_summary(state_slice.get("repo_context")),
        implementation_brief=_json_block(
            state_slice.get("implementation_brief") or {}
        ),
        approved_spec_markdown=state_slice.get("approved_spec_markdown", "") or "",
        patches=_json_block(state_slice.get("patches") or []),
        builder_outputs=_json_block(state_slice.get("builder_outputs") or []),
        tester_outputs=_json_block(state_slice.get("tester_outputs") or []),
        tester_findings=_json_block(state_slice.get("tester_findings") or []),
        reconciliation_findings=_json_block(
            state_slice.get("reconciliation_findings") or []
        ),
        coverage_entries=_json_block(state_slice.get("coverage_entries") or []),
        verify_summary=_json_block(state_slice.get("verify_summary") or {}),
        test_results=_json_block(state_slice.get("test_results") or []),
        findings=_json_block(state_slice.get("findings") or []),
        fixer_decision=_json_block(state_slice.get("fixer_decision") or {}),
        attempt_log=_json_block(state_slice.get("attempt_log") or []),
    )


def normalize_reviewer_output(raw: dict[str, Any]) -> ReviewerSummary:
    """Validate and enforce Reviewer invariants not expressible in JSON Schema."""
    try:
        summary = ReviewerSummary.model_validate(raw)
    except ValidationError as exc:
        raise ParseError(f"Reviewer emitted invalid structured output: {exc}") from exc

    issues = [str(issue).strip() for issue in summary.issues if str(issue).strip()]
    if not issues:
        issues = [
            finding.message.strip()
            for finding in summary.findings
            if finding.message.strip()
        ]

    high_finding = any(finding.severity == "high" for finding in summary.findings)
    recommendation = summary.recommendation
    if summary.severity == "high" or high_finding:
        recommendation = "request_changes"

    return summary.model_copy(
        update={
            "issues": issues,
            "recommendation": recommendation,
        }
    )


async def run_reviewer(state_slice: dict) -> ReviewerSummary:
    compose_state = ComposeState.from_mapping(state_slice)
    rendered = _render_user_prompt(state_slice)
    async with role_turn_span(ROLE):
        async with compose(
            ROLE,
            compose_state,
            task_id=compose_state.task_id,
        ) as client:
            await client.query(rendered)
            _text, structured, _result = await _drain(client)
            stamp_turn_usage(trace.get_current_span(), _result)
    if structured is None:
        raise ParseError("Reviewer emitted no structured output")
    return normalize_reviewer_output(structured)
