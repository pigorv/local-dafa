"""Unit tests for the prompt-run gate admin helpers.

These are pure: no Temporal server, no network. They pin `route_action`'s
gate/action routing and the markdown renderers used by `darkfactory gate show`.
"""
from __future__ import annotations

import pytest

from darkfactory.runtime import gate_admin
from darkfactory.runtime.gate_admin import GateStatus
from darkfactory.runtime.workflow import DarkFactoryWorkflow


def _status(**overrides) -> GateStatus:
    base = dict(
        workflow_id="darkfactory-test",
        pending_gate=None,
        brief_gate_pending=False,
        merge_gate_pending=False,
        pr_url=None,
        verify_summary=None,
        implementation_brief=None,
        spec=[],
        stories=[],
        review_decision=None,
        user_request=None,
    )
    base.update(overrides)
    return GateStatus(**base)


_SAMPLE_BRIEF = {
    "rev": 2,
    "problem": "Prompt runs cannot approve gates from the CLI.",
    "expected_behavior": ["A human can approve the brief", "A human can reject it"],
    "current_understanding": "Gate machinery exists but is unexposed.",
    "proposed_design": "Add a gate subcommand group.",
    "contract_changes": {
        "api": ["new darkfactory gate command"],
        "data": [],
        "events": [],
    },
    "compatibility_risks": ["none expected"],
    "open_assumptions": ["Temporal reachable from the CLI host"],
    "test_strategy": "Unit tests for routing plus CLI tests.",
    "work_packages": [
        {
            "id": "wp-1",
            "story_id": "story-1",
            "title": "Add gate_admin",
            "intent": "Expose gate state and decisions.",
            "verification": ["route_action raises on a mismatched gate"],
            "repo_areas": ["src/darkfactory/runtime"],
            "candidate_files": ["src/darkfactory/runtime/gate_admin.py"],
            "dependencies": [],
            "estimated_scope": "small",
            "notes": [],
        }
    ],
}

_SAMPLE_REVIEW = {
    "recommendation": "approve",
    "severity": "low",
    "issues": [],
    "findings": [
        {
            "path": "src/darkfactory/cli.py",
            "line": 10,
            "end_line": 10,
            "severity": "low",
            "message": "consider a docstring",
        }
    ],
}


# ---------- route_action ----------


@pytest.mark.parametrize(
    "action,expected",
    [
        ("approve", DarkFactoryWorkflow.approve_brief),
        ("reject", DarkFactoryWorkflow.reject_brief),
        ("revise", DarkFactoryWorkflow.revise_brief),
    ],
)
def test_route_action_brief_gate(action, expected):
    assert gate_admin.route_action("brief", action) is expected


@pytest.mark.parametrize(
    "action,expected",
    [
        ("approve", DarkFactoryWorkflow.approve_merge),
        ("reject", DarkFactoryWorkflow.reject_merge),
        ("fix", DarkFactoryWorkflow.trigger_fix),
        ("rebuild", DarkFactoryWorkflow.trigger_rebuild),
    ],
)
def test_route_action_merge_gate(action, expected):
    assert gate_admin.route_action("merge", action) is expected


@pytest.mark.parametrize("action", ["fix", "rebuild"])
def test_route_action_fix_rebuild_invalid_at_brief_gate(action):
    with pytest.raises(ValueError, match="brief gate"):
        gate_admin.route_action("brief", action)


def test_route_action_revise_invalid_at_merge_gate():
    with pytest.raises(ValueError, match="merge gate"):
        gate_admin.route_action("merge", "revise")


def test_route_action_no_pending_gate():
    with pytest.raises(ValueError, match="no gate is currently pending"):
        gate_admin.route_action(None, "approve")


# ---------- render_brief_markdown ----------


def test_render_brief_markdown_includes_prose_and_work_packages():
    status = _status(
        pending_gate="brief",
        brief_gate_pending=True,
        implementation_brief=_SAMPLE_BRIEF,
        stories=[{"id": "story-1", "title": "CLI approval"}],
        user_request="let me approve gates from the CLI",
    )
    body = gate_admin.render_brief_markdown(status)

    assert "Brief gate" in body
    assert "darkfactory-test" in body
    assert "let me approve gates from the CLI" in body
    assert "Prompt runs cannot approve gates from the CLI." in body
    assert "A human can approve the brief" in body
    assert "new darkfactory gate command" in body
    assert "Add gate_admin" in body
    assert "Brief rev 2" in body


def test_render_brief_markdown_falls_back_when_no_brief():
    status = _status(
        pending_gate="brief",
        brief_gate_pending=True,
        implementation_brief=None,
        spec=[{"id": "wp-9", "title": "Legacy slice", "intent": "do work"}],
    )
    body = gate_admin.render_brief_markdown(status)

    assert "No implementation brief available" in body
    assert "Legacy slice" in body


# ---------- render_merge_markdown ----------


def test_render_merge_markdown_renders_review_findings():
    status = _status(
        pending_gate="merge",
        merge_gate_pending=True,
        pr_url="https://github.com/acme/widgets/pull/7",
        review_decision=_SAMPLE_REVIEW,
        verify_summary={"passed": True},
    )
    body = gate_admin.render_merge_markdown(status)

    assert "https://github.com/acme/widgets/pull/7" in body
    assert "consider a docstring" in body
    assert "src/darkfactory/cli.py:10" in body
    # The GitHub `/df ...` merge-gate command block must be suppressed.
    assert "/df approve" not in body
