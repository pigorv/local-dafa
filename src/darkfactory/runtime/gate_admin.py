"""CLI-side helpers for driving the prompt-run gate approval flow.

The prompt-driven ``DarkFactoryWorkflow`` blocks indefinitely at the brief gate
and the merge gate. The GitHub issue path resolves those gates from issue
comments; the prompt path has no such surface. This module gives the
``darkfactory gate`` CLI subcommands a thin, testable layer over the workflow's
``current_state_summary`` query and its gate update methods.

It is intentionally scoped to ``DarkFactoryWorkflow`` only — the issue workflow
(``DarkFactoryIssueWorkflow``) keeps its own comment-driven approval flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio.client import Client

from darkfactory.runtime.phase_comment import (
    render_phase_comment,
    render_spec_markdown,
)
from darkfactory.runtime.workflow import DarkFactoryWorkflow


# Human actions, keyed by the gate they apply to. Values are the workflow
# update methods passed straight to ``handle.execute_update``.
_BRIEF_ACTIONS = {
    "approve": DarkFactoryWorkflow.approve_brief,
    "reject": DarkFactoryWorkflow.reject_brief,
    "revise": DarkFactoryWorkflow.revise_brief,
}
_MERGE_ACTIONS = {
    "approve": DarkFactoryWorkflow.approve_merge,
    "reject": DarkFactoryWorkflow.reject_merge,
    "fix": DarkFactoryWorkflow.trigger_fix,
    "rebuild": DarkFactoryWorkflow.trigger_rebuild,
}


@dataclass(frozen=True)
class GateStatus:
    """Snapshot of a prompt-run workflow's gate state plus design artifacts."""

    workflow_id: str
    pending_gate: str | None
    brief_gate_pending: bool
    merge_gate_pending: bool
    pr_url: str | None
    verify_summary: Any
    implementation_brief: dict[str, Any] | None
    spec: list[Any]
    stories: list[Any]
    review_decision: dict[str, Any] | None
    user_request: str | None


async def describe_gate(client: Client, *, workflow_id: str) -> GateStatus:
    """Query ``current_state_summary`` and project it into a ``GateStatus``."""
    handle = client.get_workflow_handle(workflow_id)
    summary = await handle.query(DarkFactoryWorkflow.current_state_summary)
    return GateStatus(
        workflow_id=workflow_id,
        pending_gate=summary.get("pending_gate"),
        brief_gate_pending=bool(summary.get("brief_gate_pending")),
        merge_gate_pending=bool(summary.get("merge_gate_pending")),
        pr_url=summary.get("pr_url"),
        verify_summary=summary.get("verify_summary"),
        implementation_brief=summary.get("implementation_brief"),
        spec=list(summary.get("spec") or []),
        stories=list(summary.get("stories") or []),
        review_decision=summary.get("review_decision"),
        user_request=summary.get("user_request"),
    )


async def submit_gate_decision(
    client: Client,
    *,
    workflow_id: str,
    update: Any,
    decision: Any,
) -> None:
    """Send a gate decision update to the running workflow."""
    handle = client.get_workflow_handle(workflow_id)
    await handle.execute_update(update, decision)


def route_action(pending_gate: str | None, action: str) -> Any:
    """Map a (pending gate, action) pair to the workflow update method.

    Raises ``ValueError`` when no gate is pending or the action does not apply
    to the gate that is pending, so the CLI can fail before sending an update.
    """
    if pending_gate == "brief":
        update = _BRIEF_ACTIONS.get(action)
        if update is not None:
            return update
        raise ValueError(
            f"action {action!r} is not valid at the brief gate "
            "(fix/rebuild apply only at the merge gate)"
        )
    if pending_gate == "merge":
        update = _MERGE_ACTIONS.get(action)
        if update is not None:
            return update
        raise ValueError(
            f"action {action!r} is not valid at the merge gate "
            "(revise applies only at the brief gate)"
        )
    raise ValueError(
        "no gate is currently pending for this workflow "
        "(it may still be running, or already finished)"
    )


def render_brief_markdown(status: GateStatus) -> str:
    """Render the implementation brief as a markdown document for review."""
    lines: list[str] = [
        "# Dark Factory — Brief gate",
        "",
        f"Workflow: `{status.workflow_id}`",
    ]
    if status.user_request:
        lines += ["", "## Original request", "", status.user_request.strip()]

    brief = status.implementation_brief
    if brief:
        lines += _render_brief_prose(brief)
        spec = brief.get("work_packages") or status.spec
    else:
        lines += [
            "",
            "_No implementation brief available; showing work packages only._",
        ]
        spec = status.spec

    spec_md = render_spec_markdown(stories=status.stories, spec=spec)
    if spec_md:
        lines += ["", "## Work packages", "", spec_md]
    return "\n".join(lines).rstrip() + "\n"


def render_merge_markdown(status: GateStatus) -> str:
    """Render the PR + reviewer findings for merge-gate review."""
    body = render_phase_comment(
        "review",
        "completed",
        {
            "pr_url": status.pr_url or "",
            "review_decision": status.review_decision or {},
            "verify_summary": status.verify_summary,
            # The CLI prints its own next-step hints; suppress the GitHub
            # `/df ...` merge-gate command block.
            "include_merge_instructions": False,
        },
        wf_id=status.workflow_id,
        attempt=1,
    )
    return body


def _render_brief_prose(brief: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    rev = brief.get("rev")
    if rev:
        lines += ["", f"_Brief rev {rev}_"]
    _section(lines, "Problem", brief.get("problem"))
    _bullets(lines, "Expected behavior", brief.get("expected_behavior"))
    _section(lines, "Current understanding", brief.get("current_understanding"))
    _section(lines, "Proposed design", brief.get("proposed_design"))

    contract = brief.get("contract_changes") or {}
    contract_lines: list[str] = []
    for key, label in (("api", "API"), ("data", "Data"), ("events", "Events")):
        for entry in contract.get(key) or []:
            contract_lines.append(f"- {label}: {entry}")
    if contract_lines:
        lines += ["", "## Contract changes", "", *contract_lines]

    _bullets(lines, "Compatibility risks", brief.get("compatibility_risks"))
    _bullets(lines, "Open assumptions", brief.get("open_assumptions"))
    _section(lines, "Test strategy", brief.get("test_strategy"))
    return lines


def _section(lines: list[str], heading: str, value: Any) -> None:
    text = str(value or "").strip()
    if text:
        lines += ["", f"## {heading}", "", text]


def _bullets(lines: list[str], heading: str, values: Any) -> None:
    items = [str(item).strip() for item in (values or []) if str(item).strip()]
    if items:
        lines += ["", f"## {heading}", "", *(f"- {item}" for item in items)]


__all__ = [
    "GateStatus",
    "describe_gate",
    "submit_gate_decision",
    "route_action",
    "render_brief_markdown",
    "render_merge_markdown",
]
