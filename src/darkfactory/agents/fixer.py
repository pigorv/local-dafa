"""Fixer — SDK-driven bounded repair role.

Repairs failing verifier diagnostics with bounded code edits, then emits a
structured decision (``fixed`` / ``needs_brief_change`` / ``cannot_fix``).
The output shape is defined by ``schemas/fixer_output.json`` and enforced
by the SDK's ``output_format``; this module does not declare a Pydantic
schema (matches the PO / Architect / Tester pattern).

PR C: the Fixer declares its edits in the ``edits`` field of its
structured output. Repair patches themselves are computed deterministically
by the activity from ``git diff`` after the Fixer's turn ends — the agent
does not declare patches directly, and no hook injects them. The activity
(``runtime/activities.py:_fixer_delta``) is the single consumer and
translates this dict into a workflow-mergeable state delta.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from opentelemetry import trace as _otel_trace

from darkfactory.agents._sdk_common import (
    _drain,
    render_role_user_message,
    repo_summary,
)
from darkfactory.agents.compose import ComposeState, compose

log = logging.getLogger(__name__)

ROLE = "fixer"


def _jsonable(value: Any) -> Any:
    """Recursively coerce Pydantic models / dicts / lists to JSON-serializable shapes."""
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _read_field(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _wp_id(value: Any) -> str:
    return str(_read_field(value, "id", None) or _read_field(value, "story_id", ""))


def _failed_mechanical_diagnostics(state_slice: dict) -> dict[str, list[Any]]:
    test_results = [
        result
        for result in (state_slice.get("test_results") or [])
        if _read_field(result, "returncode", 0) != 0
        or _read_field(result, "failed", 0) > 0
        or bool(_read_field(result, "errors", []))
    ]
    findings = [
        finding
        for finding in (state_slice.get("findings") or [])
        if _read_field(finding, "severity", "") in ("error", "critical")
    ]
    return {
        "test_results": _jsonable(test_results),
        "findings": _jsonable(findings),
    }


def _semantic_failures(state_slice: dict) -> list[dict[str, Any]]:
    summary = state_slice.get("verify_summary") or {}
    coverage = _read_field(summary, "predicate_coverage", []) or []
    return [
        _jsonable(item)
        for item in coverage
        if str(_read_field(item, "status", "")) != "covered"
    ]


_BLOCKING_RECONCILIATION_KINDS = frozenset(
    {
        "builder_blocked",
        "builder_no_action",
        "claimed_edits_not_applied",
        "tester_parse_failure",
        "fixer_blocked",
    }
)


def _target_wp_ids(state_slice: dict) -> list[str]:
    """Failing WP ids derived from v2 sources only.

    Order: verifier ``predicate_coverage`` entries that aren't ``covered``,
    then blocking tester findings, then blocking reconciliation findings,
    then the active ``current_slice`` as a last resort. The legacy
    ``state["spec"]`` fallback was removed during v1 → v2 cleanup; the
    brief's ``work_packages`` is the authoritative source.
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    for item in _semantic_failures(state_slice):
        add(_read_field(item, "wp_id", ""))
    for finding in state_slice.get("tester_findings") or []:
        add(_read_field(finding, "wp_id", ""))
    for finding in state_slice.get("reconciliation_findings") or []:
        if (
            _read_field(finding, "kind", "")
            in _BLOCKING_RECONCILIATION_KINDS
        ):
            add(_read_field(finding, "wp_id", ""))
    add(state_slice.get("current_slice"))
    return out


def _target_predicates(state_slice: dict) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in _semantic_failures(state_slice):
        predicate = str(_read_field(item, "predicate", "")).strip()
        if predicate and predicate not in seen:
            seen.add(predicate)
            out.append(predicate)
    return out


def _failing_work_package(state_slice: dict, target_wp: str) -> dict[str, Any]:
    """Resolve the failing WP from the approved brief.

    Reads ``state.implementation_brief.work_packages`` (v2). Returns ``{}``
    when the brief has no work packages or none match the target id.
    """
    brief = state_slice.get("implementation_brief")
    work_packages = _read_field(brief, "work_packages", []) or []
    for wp in work_packages:
        if _wp_id(wp) == target_wp or _read_field(wp, "story_id", "") == target_wp:
            return _jsonable(wp) or {}
    return {}


def _reviewer_findings(state_slice: dict) -> Any:
    review = state_slice.get("review_decision")
    if review is None:
        return []
    if isinstance(review, dict) and "issues" in review:
        return _jsonable(review.get("issues") or [])
    return _jsonable(review)


def _render_user_prompt(state_slice: dict, target_wp: str) -> str:
    return render_role_user_message(
        ROLE,
        user_request=state_slice.get("user_request", "") or "",
        repo_context=repo_summary(state_slice.get("repo_context")),
        implementation_brief=json.dumps(
            _jsonable(state_slice.get("implementation_brief")) or {}, indent=2
        ),
        failing_work_package=json.dumps(
            _failing_work_package(state_slice, target_wp), indent=2
        ),
        mechanical_diagnostics=json.dumps(
            _failed_mechanical_diagnostics(state_slice), indent=2
        ),
        semantic_failures=json.dumps(_semantic_failures(state_slice), indent=2),
        tester_findings=json.dumps(
            _jsonable(state_slice.get("tester_findings") or []), indent=2
        ),
        reconciliation_findings=json.dumps(
            _jsonable(state_slice.get("reconciliation_findings") or []),
            indent=2,
        ),
        reviewer_findings=json.dumps(_reviewer_findings(state_slice), indent=2),
        prior_patches=json.dumps(
            _jsonable(state_slice.get("patches") or []), indent=2
        ),
    )


def _synthesize_parse_failure(
    state_slice: dict, target_wp: str
) -> dict[str, Any]:
    """Fail-soft response when the SDK returns no structured output.

    Emits a ``cannot_fix`` decision so the workflow's
    ``_fixer_decision_escalation`` routes the run to a human gate (rather
    than letting the activity raise and bubble up as an opaque failure).
    Tags the active OTel span so the failure is observable.
    """
    span = _otel_trace.get_current_span()
    span.set_attribute("darkfactory.fixer.parse_failure", True)
    if target_wp:
        span.set_attribute("darkfactory.fixer.target_wp", target_wp)
    log.warning(
        "fixer: no structured output for target_wp=%r; synthesizing cannot_fix decision",
        target_wp,
    )
    return {
        "decision": "cannot_fix",
        "target_wp": target_wp,
        "target_predicates": _target_predicates(state_slice),
        "edits": [],
        "summary": "Fixer produced no structured output; treating as cannot_fix.",
        "reason": "no structured output emitted",
        "parse_failure": True,
    }


async def run_fixer(state_slice: dict) -> dict[str, Any]:
    """Run the Fixer for the highest-priority failing WP.

    Returns the structured Fixer decision plus the declared ``edits``
    list. Patches are computed by the activity from ``git diff`` after
    this function returns — the agent does not declare patches and no
    hook injects them.
    """
    compose_state = ComposeState.from_mapping(state_slice)
    # Fixer-local seams: the slice_id is the failing WP (which may differ
    # from ``current_slice``). The slice_intent / patch_justification
    # seams used by the legacy diff_capture template are gone in PR C;
    # we still pin slice_id so any downstream code reading the active
    # WP from compose state sees the failing target rather than the
    # last-built slice.
    target_wp = (_target_wp_ids(state_slice) or [""])[0]
    compose_state.slice_id = target_wp

    rendered = _render_user_prompt(state_slice, target_wp)

    async with compose(
        ROLE,
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(rendered)
        _text, structured, _result = await _drain(client)

    if structured is None:
        return _synthesize_parse_failure(state_slice, target_wp)

    return {
        "decision": str(structured.get("decision") or "cannot_fix"),
        "target_wp": str(structured.get("target_wp") or target_wp),
        "target_predicates": list(structured.get("target_predicates") or []),
        "edits": list(structured.get("edits") or []),
        "summary": str(structured.get("summary") or ""),
        "reason": str(structured.get("reason") or ""),
        "parse_failure": False,
    }
