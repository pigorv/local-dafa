from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from darkfactory.runtime.comment_templates import compact_value, render


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


_MERGE_GATE_OPTIONS: list[dict[str, str]] = [
    {"key": "approve", "command": "`/df approve`", "description": "merge the PR"},
    {"key": "fix", "command": "`/df fix <focus>`", "description": "re-run Fixer and Verifier"},
    {
        "key": "rebuild",
        "command": "`/df rebuild <focus>`",
        "description": "re-run Builder, Tester and Verifier",
    },
    {
        "key": "reject",
        "command": "`/df reject <reason>`",
        "description": "close the issue and quarantine",
    },
]


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
    stories: Iterable[dict[str, Any]] | None = None,
    spec: Iterable[dict[str, Any]] | None = None,
    review_decision: Any = None,
) -> str:
    story_list = [
        {
            "id": str(story.get("id") or story.get("story_id") or "story"),
            "title": str(story.get("title") or "").strip(),
            "acceptance_criteria": story.get("acceptance_criteria") or [],
        }
        for story in (stories or [])
    ]

    work_packages = [_work_package_view(item) for item in (spec or [])]

    deferred_notes: list[Any] = []
    review_decision_compact = ""
    if review_decision:
        notes = _decision_field(review_decision, "notes") or []
        if isinstance(notes, list):
            deferred_notes = notes
        review_decision_compact = compact_value(review_decision)

    empty = (
        not story_list
        and not work_packages
        and not deferred_notes
        and not review_decision_compact
    )

    rendered = render(
        "spec_markdown.md.j2",
        stories=story_list,
        work_packages=work_packages,
        deferred_notes=deferred_notes,
        review_decision_compact=review_decision_compact,
        empty=empty,
    )
    return rendered.rstrip()


def approval_instructions() -> str:
    return render("approval_instructions.md.j2").rstrip()


def merge_gate_instructions(recommendation: str = "") -> str:
    return render(
        "merge_gate_instructions.md.j2",
        suggested=_suggested_action(recommendation),
        options=_MERGE_GATE_OPTIONS,
    ).rstrip()


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
    return "\n".join(f"{key}: {compact_value(value)}" for key, value in fields.items())


def _render_triage(status: str, fields: dict[str, Any]) -> str:
    return render(
        "phase_triage.md.j2",
        status=status,
        outcome=str(fields.get("outcome") or "").strip(),
        round=fields.get("round"),
        max_rounds=fields.get("max_rounds"),
        clarify_url=str(fields.get("clarify_url") or "").strip(),
        derived_request=str(fields.get("derived_request") or "").strip(),
        confidence=str(fields.get("confidence") or "").strip(),
        rationale=str(fields.get("rationale") or "").strip(),
    ).rstrip()


def _render_design(status: str, fields: dict[str, Any]) -> str:
    include_instructions = fields.get("include_approval_instructions", True)
    approval_block = approval_instructions() if include_instructions else ""
    return render(
        "phase_design.md.j2",
        status=status,
        revision_note=str(fields.get("revision_note") or "").strip(),
        feedback=str(fields.get("feedback") or "").strip(),
        spec_markdown=str(fields.get("spec_markdown") or "").strip(),
        approval_note=str(fields.get("approval_note") or "").strip(),
        approval_block=approval_block,
    ).strip()


def _render_build(fields: dict[str, Any]) -> str:
    wp_outputs = _merge_wp_outputs(
        fields.get("builder_outputs") or [],
        fields.get("tester_outputs") or [],
    )
    findings_block = (
        render("build_findings.md.j2", wp_outputs=wp_outputs).rstrip()
        if wp_outputs
        else ""
    )
    return render(
        "phase_build.md.j2",
        commit_count=fields.get("commit_count"),
        files_changed=fields.get("files_changed"),
        head_sha=fields.get("head_sha"),
        branch=fields.get("branch"),
        attempts=fields.get("attempts") or [],
        findings_block=findings_block,
    ).rstrip()


def _merge_wp_outputs(
    builder_records: list[Any],
    tester_records: list[Any],
) -> list[dict[str, Any]]:
    """Keep the latest builder + tester record per WP, in first-seen order."""
    latest_builder: dict[str, dict[str, Any]] = {}
    latest_tester: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _note(wp_id: str) -> None:
        if wp_id not in order:
            order.append(wp_id)

    for rec in builder_records:
        wp_id = str(_decision_field(rec, "wp_id") or "").strip() or "unassigned"
        latest_builder[wp_id] = {
            "status": str(_decision_field(rec, "status") or "").strip(),
            "summary": str(_decision_field(rec, "summary") or "").strip(),
            "edits": list(_decision_field(rec, "edits") or []),
            "blockers": list(_decision_field(rec, "blockers") or []),
        }
        _note(wp_id)
    for rec in tester_records:
        wp_id = str(_decision_field(rec, "wp_id") or "").strip() or "unassigned"
        latest_tester[wp_id] = {
            "summary": str(_decision_field(rec, "summary") or "").strip(),
            "findings": list(_decision_field(rec, "findings") or []),
        }
        _note(wp_id)

    return [
        {
            "wp_id": wp_id,
            "builder": latest_builder.get(wp_id),
            "tester": latest_tester.get(wp_id),
        }
        for wp_id in order
    ]


def _render_verify(fields: dict[str, Any]) -> str:
    return render(
        "phase_verify.md.j2",
        verify_block=_format_verify_summary(
            fields.get("summary") or fields.get("verify_summary")
        ),
        tests=fields.get("tests"),
        lint=fields.get("lint"),
        types=fields.get("types"),
        quality=fields.get("quality"),
        attempts=fields.get("attempts") or [],
    ).rstrip()


_PREDICATE_STATUS_ICONS = {
    "covered": "✓",
    "weakly_covered": "~",
    "uncovered": "✗",
}


def _format_verify_summary(summary: Any) -> str:
    """Render a VerifySummary dict as a multi-line markdown block.

    Empty / falsy input returns an empty string so callers can ``if summary``.
    A plain string is passed through verbatim, since some callers (legacy
    paths) hand the verifier's raw text in instead of the structured dict.
    """
    if not summary:
        return ""
    if isinstance(summary, str):
        return summary.strip()
    if not isinstance(summary, dict):
        return compact_value(summary)

    passed = summary.get("passed")
    failed_tests = summary.get("failed_tests")
    hard_findings = summary.get("hard_findings")
    uncovered = summary.get("uncovered_predicates")
    blocking = summary.get("blocking_failures")

    status_bits: list[str] = []
    if passed is True:
        status_bits.append("Passed: ✓")
    elif passed is False:
        status_bits.append("Passed: ✗")
    if failed_tests:
        status_bits.append(f"{failed_tests} failed test{'' if int(failed_tests) == 1 else 's'}")
    if hard_findings:
        status_bits.append(
            f"{hard_findings} hard finding{'' if int(hard_findings) == 1 else 's'}"
        )
    if uncovered:
        status_bits.append(f"{uncovered} uncovered predicates")
    if blocking:
        status_bits.append(f"{blocking} blocking failures")
    status_line = " · ".join(status_bits)

    coverage = summary.get("predicate_coverage") or []
    groups = _group_predicate_coverage(coverage)

    return render(
        "verify_summary.md.j2",
        status_line=status_line,
        predicate_coverage_groups=groups,
    ).rstrip()


def _group_predicate_coverage(coverage: list[Any]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for item in coverage:
        wp_id = str(_decision_field(item, "wp_id") or "").strip() or "unassigned"
        if wp_id not in groups:
            groups[wp_id] = []
            order.append(wp_id)
        status = str(_decision_field(item, "status") or "").strip()
        groups[wp_id].append(
            {
                "predicate": str(_decision_field(item, "predicate") or "").strip(),
                "status_icon": _PREDICATE_STATUS_ICONS.get(status, status or "·"),
                "evidence": str(_decision_field(item, "evidence") or "").strip(),
            }
        )
    return [{"wp_id": wp_id, "predicates": groups[wp_id]} for wp_id in order]


def _render_pr(fields: dict[str, Any]) -> str:
    return render(
        "phase_pr.md.j2",
        pr_url=str(fields.get("pr_url") or "").strip(),
        diffstat=str(fields.get("diffstat") or "").strip(),
        pr_body_url=str(fields.get("pr_body_url") or "").strip(),
    ).rstrip()


def _render_review(status: str, fields: dict[str, Any]) -> str:
    decision = fields.get("review_decision") or {}
    recommendation = str(_decision_field(decision, "recommendation") or "").strip()
    severity = str(_decision_field(decision, "severity") or "").strip()
    issues = _decision_field(decision, "issues") or []
    findings = [
        _reviewer_finding_view(finding)
        for finding in _decision_field(decision, "findings") or []
    ]

    merge_block = ""
    if status != "running" and fields.get("include_merge_instructions", True):
        merge_block = merge_gate_instructions(recommendation)

    return render(
        "phase_review.md.j2",
        pr_url=str(fields.get("pr_url") or "").strip(),
        recommendation=recommendation,
        severity=severity,
        issues=issues,
        findings=findings,
        verify_summary=_format_verify_summary(fields.get("verify_summary")),
        decision_note=str(fields.get("decision_note") or "").strip(),
        merge_block=merge_block,
    ).strip()


_SEVERITY_BADGES = {"low": "[LOW]", "medium": "[MED]", "high": "[HIGH]"}


def _reviewer_finding_view(finding: Any) -> dict[str, str]:
    """Render one reviewer finding as fields a template can format."""
    path = str(_decision_field(finding, "path") or "").strip()
    line = _decision_field(finding, "line")
    end_line = _decision_field(finding, "end_line")
    severity = str(_decision_field(finding, "severity") or "").strip().lower()
    message = str(_decision_field(finding, "message") or "").strip()

    if path and line and end_line and int(line) != int(end_line):
        location = f"{path}:{int(line)}-{int(end_line)}"
    elif path and line:
        location = f"{path}:{int(line)}"
    elif path:
        location = path
    else:
        location = "(no path)"

    badge = _SEVERITY_BADGES.get(severity, f"[{severity.upper()}]" if severity else "")
    return {
        "severity_badge": badge,
        "location": location,
        "message": message,
    }


def _render_merge(fields: dict[str, Any]) -> str:
    return render(
        "phase_merge.md.j2",
        merge_commit_sha=fields.get("merge_commit_sha"),
        branch_deleted=fields.get("branch_deleted"),
        issue_closes=fields.get("issue_closes"),
    ).rstrip()


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


def _decision_field(decision: Any, key: str) -> Any:
    if isinstance(decision, dict):
        return decision.get(key)
    return getattr(decision, key, None)


def _work_package_view(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a work package / spec slice for the spec_markdown template."""
    wp_id = str(item.get("id") or item.get("story_id") or "work-package")
    title = str(item.get("title") or "").strip()
    return {
        "id": wp_id,
        "title": title if title and title != wp_id else "",
        "intent": str(item.get("intent") or "").strip(),
        "approach": item.get("approach"),
        "repo_areas": item.get("repo_areas"),
        "candidate_files": _candidate_files_for(item),
        "verification_predicates": _verification_predicates_for(item),
        "test_files": item.get("test_files"),
        "risks": item.get("risks"),
        "dependencies": _first_present(item, "dependencies", "depends_on"),
    }


def _candidate_files_for(item: dict[str, Any]) -> list[Any]:
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
        return compact_value(value.get("root") or value.get("predicate") or value)
    return compact_value(value)


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
        key = compact_value(value)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


# Backwards-compatible alias for the previous module-private helper name.
_compact_value = compact_value


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
