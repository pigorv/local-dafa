from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any


PHASE_MARKERS: dict[str, str] = {
    "triage": "<!-- df-phase:{wf}:triage -->",
    "design": "<!-- df-phase:{wf}:design:{rev} -->",
    "build": "<!-- df-phase:{wf}:build -->",
    "verify": "<!-- df-phase:{wf}:verify -->",
    "pr": "<!-- df-phase:{wf}:pr -->",
    "review": "<!-- df-phase:{wf}:review:{iteration} -->",
    "merge": "<!-- df-phase:{wf}:merge -->",
}

PHASE_TITLES: dict[str, str] = {
    "triage": "Triage",
    "design": "Design",
    "build": "Build",
    "verify": "Verify",
    "pr": "PR",
    "review": "PR Review",
    "merge": "Merge",
}


def marker_for(wf_id: str, phase: str, **kwargs: Any) -> str:
    if not wf_id:
        raise ValueError("marker_for requires wf_id")
    if phase not in PHASE_MARKERS:
        raise ValueError(f"unknown Dark Factory phase: {phase!r}")
    if phase == "design":
        rev = int(kwargs.get("rev") or 0)
        if rev < 1:
            raise ValueError("design phase marker requires rev >= 1")
        return PHASE_MARKERS[phase].format(wf=wf_id, rev=rev)
    if phase == "review":
        iteration = int(
            kwargs.get("iteration") or kwargs.get("attempt") or 0
        )
        if iteration < 1:
            raise ValueError("review phase marker requires iteration >= 1")
        return PHASE_MARKERS[phase].format(wf=wf_id, iteration=iteration)
    return PHASE_MARKERS[phase].format(wf=wf_id)


def render_phase_comment(
    phase: str,
    status: str,
    fields: dict[str, Any] | None,
    *,
    wf_id: str,
    trace_url: str | None = None,
    rev: int | None = None,
    attempt: int | None = None,
    started_at: str | datetime | None = None,
    ended_at: str | datetime | None = None,
) -> str:
    """Render a single GitHub-visible phase comment body.

    The body always starts with the phase marker so comment upsert can find it
    across activity retries and workflow replays.
    """
    fields = dict(fields or {})
    marker = marker_for(wf_id, phase, rev=rev, attempt=attempt)
    title = PHASE_TITLES.get(phase, phase.title())
    suffix_parts: list[str] = []
    if rev is not None:
        suffix_parts.append(f"rev {int(rev)}")
    if attempt is not None:
        suffix_parts.append(f"attempt {int(attempt)}")
    suffix = f" ({' | '.join(suffix_parts)})" if suffix_parts and status == "running" else ""

    lines = [
        marker,
        f"**Dark Factory — {title}{suffix}**",
        "",
        _status_line(status, started_at=started_at, ended_at=ended_at),
    ]
    rendered = _render_phase_fields(phase, status, fields)
    if rendered:
        lines.extend(["", rendered])
    next_phase = str(fields.get("next") or "").strip()
    if next_phase and status != "running":
        lines.extend(["", f"Next: {next_phase}"])
    lines.extend(["", _workflow_line(wf_id, trace_url)])
    lines.extend(["", end_marker_for(marker)])
    return "\n".join(lines).rstrip() + "\n"


def render_spec_markdown(
    *,
    user_request: str = "",
    stories: Iterable[dict[str, Any]] | None = None,
    spec: Iterable[dict[str, Any]] | None = None,
    review_decision: Any = None,
) -> str:
    lines = ["## Approved Contract"]
    if user_request:
        lines.extend(["", "### Request", user_request.strip()])

    story_list = list(stories or [])
    if story_list:
        lines.extend(["", "### Stories"])
        for story in story_list:
            story_id = str(story.get("id") or story.get("story_id") or "story")
            title = str(story.get("title") or "").strip()
            lines.append(f"- **{story_id}**: {title or '(untitled)'}")
            criteria = story.get("acceptance_criteria") or []
            for criterion in criteria:
                lines.append(f"  - {criterion}")

    spec_list = list(spec or [])
    if spec_list:
        lines.extend(["", "### Work Packages"])
        for item in spec_list:
            wp_id = str(item.get("id") or item.get("story_id") or "work-package")
            title = str(item.get("title") or "").strip()
            suffix = f": {title}" if title and title != wp_id else ""
            lines.append(f"- **{wp_id}**{suffix}")
            for key, label in (
                ("intent", "Intent"),
                ("approach", "Approach"),
                ("repo_areas", "Repo areas"),
            ):
                rendered = _compact_value(item.get(key))
                if rendered:
                    lines.append(f"  - {label}: {rendered}")

            candidate_files = _candidate_files_for(item)
            rendered_candidates = _compact_value(candidate_files)
            if rendered_candidates:
                lines.append(f"  - Candidate files (hints): {rendered_candidates}")

            verification = _verification_predicates_for(item)
            if verification:
                lines.append("  - Verification predicates:")
                lines.extend(f"    - {predicate}" for predicate in verification)

            for key, label in (("test_files", "Tests"), ("risks", "Risks")):
                rendered = _compact_value(item.get(key))
                if rendered:
                    lines.append(f"  - {label}: {rendered}")
            dependencies = _first_present(item, "dependencies", "depends_on")
            rendered_dependencies = _compact_value(dependencies)
            if rendered_dependencies:
                lines.append(f"  - Depends on: {rendered_dependencies}")

    if review_decision:
        lines.extend(["", "### Spec Review", _compact_value(review_decision)])

    if len(lines) == 1:
        lines.append("_No spec content was produced._")
    return "\n".join(lines).rstrip()


def approval_instructions() -> str:
    return "\n".join(
        [
            "### Approval",
            "Reply with one of:",
            "- `/df approve`",
            "- `/df revise <feedback>`",
            "- `/df reject <reason>`",
            "",
            "Allowed approvers: repo collaborators with write access.",
            "Use `/df revise` to change the spec; inline edits are ignored.",
        ]
    )


def _status_line(
    status: str,
    *,
    started_at: str | datetime | None,
    ended_at: str | datetime | None,
) -> str:
    started = _format_ts(started_at)
    ended = _format_ts(ended_at)
    clean_status = (status or "running").strip()
    if clean_status == "running":
        return f"Status: running · started {started or 'unknown'}"
    if started and ended:
        duration = _duration(started_at, ended_at)
        suffix = f" ({duration})" if duration else ""
        return f"Status: {clean_status} · {started} → {ended}{suffix}"
    if ended:
        return f"Status: {clean_status} · ended {ended}"
    return f"Status: {clean_status}"


def _workflow_line(wf_id: str, trace_url: str | None) -> str:
    if trace_url:
        return f"Workflow: `{wf_id}` · [trace]({trace_url})"
    return f"Workflow: `{wf_id}`"


def end_marker_for(marker: str) -> str:
    if not marker.startswith("<!-- "):
        return "<!-- /df-phase -->"
    return marker.replace("<!-- ", "<!-- /", 1)


def _render_phase_fields(phase: str, status: str, fields: dict[str, Any]) -> str:
    if phase == "triage":
        return _render_triage(status, fields)
    if phase == "design":
        return _render_design(status, fields)
    if phase == "build":
        return _render_build(fields)
    if phase == "verify":
        return _render_verify(fields)
    if phase == "pr":
        return _render_pr(fields)
    if phase == "review":
        return _render_review(status, fields)
    if phase == "merge":
        return _render_merge(fields)
    return "\n".join(f"{key}: {_compact_value(value)}" for key, value in fields.items())


def _render_triage(status: str, fields: dict[str, Any]) -> str:
    lines: list[str] = []
    outcome = str(fields.get("outcome") or "").strip()
    if outcome:
        line = f"Outcome: {outcome}"
        round_number = fields.get("round")
        max_rounds = fields.get("max_rounds")
        if outcome == "needs-clarification" and round_number and max_rounds:
            line += f" (round {round_number}/{max_rounds})"
        clarify_url = str(fields.get("clarify_url") or "").strip()
        if clarify_url:
            line += f" — see {clarify_url}"
        lines.append(line)
    derived = str(fields.get("derived_request") or "").strip()
    if derived:
        lines.append(f"Derived request: {derived}")
    confidence = str(fields.get("confidence") or "").strip()
    if confidence:
        lines.append(f"Confidence: {confidence}")
    rationale = str(fields.get("rationale") or "").strip()
    if rationale and status != "running":
        lines.append(f"Rationale: {rationale}")
    return "\n".join(lines)


def _render_design(status: str, fields: dict[str, Any]) -> str:
    lines: list[str] = []
    revision_note = str(fields.get("revision_note") or "").strip()
    if revision_note:
        lines.append(revision_note)
    if status == "running":
        feedback = str(fields.get("feedback") or "").strip()
        if feedback:
            lines.append(f"Revision feedback: {feedback}")
        return "\n".join(lines)
    spec_markdown = str(fields.get("spec_markdown") or "").strip()
    if spec_markdown:
        lines.append(spec_markdown)
    approval_note = str(fields.get("approval_note") or "").strip()
    if approval_note:
        lines.extend(["", approval_note])
    if fields.get("include_approval_instructions", True):
        lines.extend(["", approval_instructions()])
    return "\n".join(line for line in lines if line is not None).strip()


def _render_build(fields: dict[str, Any]) -> str:
    lines = []
    for key, label in (
        ("commit_count", "Commits"),
        ("files_changed", "Files changed"),
        ("head_sha", "Head SHA"),
        ("branch", "Branch"),
    ):
        value = fields.get(key)
        if value not in (None, "", []):
            lines.append(f"{label}: {_compact_value(value)}")
    attempts = fields.get("attempts") or []
    if attempts:
        lines.extend(["", "Attempts:"])
        lines.extend(f"- {_compact_value(attempt)}" for attempt in attempts)
    return "\n".join(lines)


def _render_verify(fields: dict[str, Any]) -> str:
    lines = []
    summary = fields.get("summary") or fields.get("verify_summary")
    if summary:
        lines.append(f"Summary: {_compact_value(summary)}")
    for key, label in (
        ("tests", "Tests"),
        ("lint", "Lint"),
        ("types", "Types"),
        ("quality", "Quality"),
    ):
        value = fields.get(key)
        if value not in (None, "", []):
            lines.append(f"{label}: {_compact_value(value)}")
    attempts = fields.get("attempts") or []
    if attempts:
        lines.extend(["", "Attempts:"])
        lines.extend(f"- {_compact_value(attempt)}" for attempt in attempts)
    return "\n".join(lines)


def _render_pr(fields: dict[str, Any]) -> str:
    lines = []
    pr_url = str(fields.get("pr_url") or "").strip()
    if pr_url:
        lines.append(f"PR: {pr_url}")
    diffstat = str(fields.get("diffstat") or "").strip()
    if diffstat:
        lines.append(f"Diffstat: {diffstat}")
    pr_body_url = str(fields.get("pr_body_url") or "").strip()
    if pr_body_url:
        lines.append(f"Description: {pr_body_url}")
    return "\n".join(lines)


def merge_gate_instructions(recommendation: str = "") -> str:
    suggested = _suggested_action(recommendation)
    lines = ["### Recommended next actions"]
    if suggested:
        lines.append(f"Reviewer recommends: **{suggested}**.")
    else:
        lines.append("Reply with one of:")
    options: list[tuple[str, str, str]] = [
        ("approve", "`/df approve`", "merge the PR"),
        ("fix", "`/df fix <focus>`", "re-run Fixer and Verifier"),
        ("rebuild", "`/df rebuild <focus>`", "re-run Builder, Tester and Verifier"),
        ("reject", "`/df reject <reason>`", "close the issue and quarantine"),
    ]
    for key, command, description in options:
        marker = " ← recommended" if key == suggested else ""
        lines.append(f"- {command} — {description}{marker}")
    lines.extend(
        [
            "",
            "Allowed approvers: repo collaborators with write access.",
        ]
    )
    return "\n".join(lines)


def _suggested_action(recommendation: str) -> str:
    value = (recommendation or "").strip().lower()
    if not value:
        return ""
    if value == "approve":
        return "approve"
    if value in {"reject", "block"}:
        return "reject"
    if value in {"request_changes", "needs_changes", "changes_requested"}:
        return "fix"
    if value in {"rebuild", "needs_rebuild"}:
        return "rebuild"
    return ""


def _render_review(status: str, fields: dict[str, Any]) -> str:
    lines: list[str] = []
    pr_url = str(fields.get("pr_url") or "").strip()
    if pr_url:
        lines.append(f"PR: {pr_url}")

    decision = fields.get("review_decision") or {}
    recommendation = str(_decision_field(decision, "recommendation") or "").strip()
    severity = str(_decision_field(decision, "severity") or "").strip()
    if recommendation:
        lines.append(f"Recommendation: {recommendation}")
    if severity:
        lines.append(f"Severity: {severity}")
    issues = _decision_field(decision, "issues") or []
    if issues:
        lines.extend(["", "Issues:"])
        for issue in issues:
            lines.append(f"- {_compact_value(issue)}")

    summary = fields.get("verify_summary")
    if summary:
        lines.append("")
        lines.append(f"Verify summary: {_compact_value(summary)}")

    decision_note = str(fields.get("decision_note") or "").strip()
    if decision_note:
        lines.extend(["", decision_note])

    if status != "running" and fields.get("include_merge_instructions", True):
        lines.extend(["", merge_gate_instructions(recommendation)])
    return "\n".join(line for line in lines if line is not None).strip()


def _decision_field(decision: Any, key: str) -> Any:
    if isinstance(decision, dict):
        return decision.get(key)
    return getattr(decision, key, None)


def _render_merge(fields: dict[str, Any]) -> str:
    lines = []
    for key, label in (
        ("merge_commit_sha", "Merge commit"),
        ("branch_deleted", "Branch deleted"),
        ("issue_closes", "Issue close"),
    ):
        value = fields.get(key)
        if value not in (None, "", []):
            lines.append(f"{label}: {_compact_value(value)}")
    return "\n".join(lines)


def _compact_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, dict):
        parts = [f"{key}={_compact_value(val)}" for key, val in value.items()]
        return ", ".join(part for part in parts if not part.endswith("="))
    if isinstance(value, Iterable):
        items = [_compact_value(item) for item in value]
        return ", ".join(item for item in items if item)
    return str(value)


def _candidate_files_for(item: dict[str, Any]) -> list[str]:
    explicit = _list_value(item.get("candidate_files"))
    if explicit:
        return explicit
    return _dedupe(
        _list_value(item.get("affected_files")) + _list_value(item.get("new_files"))
    )


def _verification_predicates_for(item: dict[str, Any]) -> list[str]:
    predicates: list[str] = []
    for value in _list_value(item.get("verification")):
        predicates.extend(
            line.strip()
            for line in _predicate_text(value).splitlines()
            if line.strip()
        )
    return predicates


def _predicate_text(value: Any) -> str:
    if isinstance(value, dict):
        return _compact_value(value.get("root") or value.get("predicate") or value)
    return _compact_value(value)


def _list_value(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _first_present(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return None


def _dedupe(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    deduped: list[Any] = []
    for value in values:
        key = _compact_value(value)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _format_ts(value: str | datetime | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _duration(
    started_at: str | datetime | None,
    ended_at: str | datetime | None,
) -> str:
    if not isinstance(started_at, datetime) or not isinstance(ended_at, datetime):
        return ""
    seconds = max(0, int((ended_at - started_at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rem}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"
