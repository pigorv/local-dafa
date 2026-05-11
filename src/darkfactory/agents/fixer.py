"""Fixer - SDK-driven bounded repair role."""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from darkfactory.agents._sdk_common import run_to_completion
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.state import Patch

ROLE = "fixer"

FixerDecision = Literal["fixed", "needs_brief_change", "cannot_fix"]


class FixerOutput(BaseModel):
    """Fixer repair decision plus patches captured by diff_capture."""

    decision: FixerDecision
    target_wp: str
    target_predicates: list[str] = Field(default_factory=list)
    summary: str = ""
    reason: str = ""
    patches: list[dict[str, Any]] = Field(default_factory=list)


def _jsonable(value: Any) -> Any:
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


def _target_wp_ids(state_slice: dict) -> list[str]:
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
    add(state_slice.get("current_slice"))

    if not out:
        for spec_slice in state_slice.get("spec") or []:
            add(_wp_id(spec_slice))
            break
    return out


def _target_work_package_context(state_slice: dict) -> dict[str, Any]:
    target_ids = _target_wp_ids(state_slice)
    target_set = set(target_ids)
    brief = state_slice.get("implementation_brief")
    brief_wps = [
        _jsonable(wp)
        for wp in (_read_field(brief, "work_packages", []) or [])
        if _wp_id(wp) in target_set or _read_field(wp, "story_id", "") in target_set
    ]
    spec_slices = [
        _jsonable(slice_)
        for slice_ in (state_slice.get("spec") or [])
        if _wp_id(slice_) in target_set or _read_field(slice_, "story_id", "") in target_set
    ]
    return {
        "target_wp_ids": target_ids,
        "work_packages": brief_wps,
        "legacy_spec_slices": spec_slices,
    }


def _target_predicates(state_slice: dict) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in _semantic_failures(state_slice):
        predicate = str(_read_field(item, "predicate", "")).strip()
        if predicate and predicate not in seen:
            seen.add(predicate)
            out.append(predicate)
    return out


def _reviewer_findings(state_slice: dict) -> Any:
    review = state_slice.get("review_decision")
    if review is None:
        return []
    if isinstance(review, dict) and "issues" in review:
        return _jsonable(review.get("issues") or [])
    return _jsonable(review)


def _patch_justification(state_slice: dict) -> str:
    wp_ids = _target_wp_ids(state_slice)
    predicates = _target_predicates(state_slice)
    if predicates:
        return f"Fixer for {', '.join(wp_ids)}: " + "; ".join(predicates[:3])
    if wp_ids:
        return f"Fixer for {', '.join(wp_ids)}: verifier failure"
    return "Fixer: verifier failure"


def _user_message(state_slice: dict) -> str:
    brief = _jsonable(state_slice.get("implementation_brief") or {})
    target_context = _target_work_package_context(state_slice)
    mechanical = _failed_mechanical_diagnostics(state_slice)
    semantic = _semantic_failures(state_slice)
    tester_findings = _jsonable(state_slice.get("tester_findings") or [])
    reviewer_findings = _reviewer_findings(state_slice)
    patches = _jsonable(state_slice.get("patches") or [])
    repo_context = _jsonable(state_slice.get("repo_context") or {})

    return (
        "Repair only the failing diagnostics below. If repair would require "
        "changing the accepted brief or adding new scope, do not edit files.\n\n"
        f"Approved Implementation Brief (JSON):\n{json.dumps(brief, indent=2)}\n\n"
        f"Failing Work Package context (JSON):\n"
        f"{json.dumps(target_context, indent=2)}\n\n"
        f"Failed mechanical diagnostics (JSON):\n"
        f"{json.dumps(mechanical, indent=2)}\n\n"
        f"Semantic coverage failures (JSON):\n{json.dumps(semantic, indent=2)}\n\n"
        f"Tester findings (JSON):\n{json.dumps(tester_findings, indent=2)}\n\n"
        f"Reviewer findings (JSON):\n{json.dumps(reviewer_findings, indent=2)}\n\n"
        f"Prior patches (JSON):\n{json.dumps(patches, indent=2)}\n\n"
        f"Repo context (untrusted JSON):\n{json.dumps(repo_context, indent=2)}\n\n"
        "Return a FixerOutput JSON object with decision, target_wp, "
        "target_predicates, summary, and reason."
    )


async def run_fixer(state_slice: dict) -> FixerOutput:
    sink: list[Patch] = []
    compose_state = ComposeState.from_mapping(state_slice, patches_sink=sink)
    # Fixer-local seams: the diff_capture slice_id is the failing WP (which
    # may differ from ``current_slice``), and the justification text is
    # derived from verifier coverage + tester findings. Both are precomputed
    # here so the manifest's ``justification_template: "{patch_justification}"``
    # interpolates the same text the imperative path emits.
    compose_state.slice_id = (_target_wp_ids(state_slice) or [""])[0]
    compose_state.patch_justification = _patch_justification(state_slice)
    async with compose(
        ROLE,
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=FixerOutput)
    assert isinstance(result, FixerOutput)
    result.patches = [dict(p) for p in sink]
    return result
