"""Coverage for quarantine_closed_issue_activity (the closed-workflow cleanup)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from darkfactory.runtime import activities as activities_mod
from darkfactory.runtime.activities import (
    DF_CANCELED_LABEL,
    DF_FAILED_LABEL,
    DF_READY_LABEL,
    quarantine_closed_issue_activity,
    render_quarantine_comment_body,
)


REPO = "octo-org/octo-repo"
ISSUE_NUMBER = 42
WORKFLOW_ID = "df-issue-octo-org-octo-repo-42"


class _GhRecorder:
    """Stand-in for `_run_orchestrator_gh` that returns scripted view output."""

    def __init__(self, view_payload: dict[str, Any]) -> None:
        self.view_payload = view_payload
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        timeout: int,
        description: str,
        stdin: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "argv": list(argv),
                "timeout": timeout,
                "description": description,
                "stdin": stdin,
            }
        )
        if argv[:3] == ["gh", "issue", "view"]:
            return json.dumps(self.view_payload)
        return ""


def _patch_gh(monkeypatch, recorder: _GhRecorder) -> None:
    monkeypatch.setattr(activities_mod, "_run_orchestrator_gh", recorder)


def _argvs(recorder: _GhRecorder) -> list[list[str]]:
    return [call["argv"] for call in recorder.calls]


def test_render_quarantine_comment_body_for_canceled_includes_marker_and_label():
    body = render_quarantine_comment_body(ISSUE_NUMBER, WORKFLOW_ID, "canceled")

    assert body.startswith(f"<!-- df-quarantine:{WORKFLOW_ID} -->")
    assert f"issue #{ISSUE_NUMBER}" in body
    assert "`canceled`" in body
    assert f"`{DF_READY_LABEL}`" in body
    assert f"`{DF_CANCELED_LABEL}`" in body


def test_render_quarantine_comment_body_for_completed_omits_status_label():
    body = render_quarantine_comment_body(ISSUE_NUMBER, WORKFLOW_ID, "completed")

    assert "`completed`" in body
    assert f"`{DF_READY_LABEL}`" in body
    assert "`df:failed`" not in body
    assert "`df:canceled`" not in body


def test_quarantine_canceled_workflow_swaps_label_and_posts_comment(monkeypatch):
    recorder = _GhRecorder(
        view_payload={
            "comments": [
                {"author": {"login": "octocat"}, "body": "Original report."}
            ],
            "labels": [{"name": DF_READY_LABEL}, {"name": "bug"}],
        }
    )
    _patch_gh(monkeypatch, recorder)

    result = asyncio.run(
        quarantine_closed_issue_activity(
            REPO, ISSUE_NUMBER, WORKFLOW_ID, "canceled"
        )
    )

    assert result == {
        "workflow_id": WORKFLOW_ID,
        "repo": REPO,
        "issue_number": ISSUE_NUMBER,
        "closure_status": "canceled",
        "label_removed": DF_READY_LABEL,
        "label_added": DF_CANCELED_LABEL,
        "comment_posted": True,
    }
    assert _argvs(recorder) == [
        [
            "gh", "issue", "view", str(ISSUE_NUMBER),
            "--repo", REPO,
            "--json", "comments,labels",
        ],
        [
            "gh", "issue", "edit", str(ISSUE_NUMBER),
            "--repo", REPO,
            "--remove-label", DF_READY_LABEL,
            "--add-label", DF_CANCELED_LABEL,
        ],
        [
            "gh", "issue", "comment", str(ISSUE_NUMBER),
            "--repo", REPO,
            "--body-file", "-",
        ],
    ]
    comment_call = recorder.calls[2]
    assert comment_call["stdin"] is not None
    assert comment_call["stdin"].startswith(f"<!-- df-quarantine:{WORKFLOW_ID} -->")


def test_quarantine_failed_workflow_uses_failed_label(monkeypatch):
    recorder = _GhRecorder(
        view_payload={
            "comments": [],
            "labels": [{"name": DF_READY_LABEL}],
        }
    )
    _patch_gh(monkeypatch, recorder)

    result = asyncio.run(
        quarantine_closed_issue_activity(
            REPO, ISSUE_NUMBER, WORKFLOW_ID, "failed"
        )
    )

    assert result["label_removed"] == DF_READY_LABEL
    assert result["label_added"] == DF_FAILED_LABEL
    edit_call = recorder.calls[1]
    assert "--add-label" in edit_call["argv"]
    assert DF_FAILED_LABEL in edit_call["argv"]
    assert DF_CANCELED_LABEL not in edit_call["argv"]


def test_quarantine_completed_workflow_only_removes_df_ready(monkeypatch):
    recorder = _GhRecorder(
        view_payload={
            "comments": [],
            "labels": [{"name": DF_READY_LABEL}, {"name": "df:done"}],
        }
    )
    _patch_gh(monkeypatch, recorder)

    result = asyncio.run(
        quarantine_closed_issue_activity(
            REPO, ISSUE_NUMBER, WORKFLOW_ID, "completed"
        )
    )

    assert result["label_removed"] == DF_READY_LABEL
    assert result["label_added"] is None
    assert result["comment_posted"] is True
    edit_call = recorder.calls[1]
    assert edit_call["argv"] == [
        "gh", "issue", "edit", str(ISSUE_NUMBER),
        "--repo", REPO,
        "--remove-label", DF_READY_LABEL,
    ]


def test_quarantine_skips_comment_when_marker_already_present(monkeypatch):
    recorder = _GhRecorder(
        view_payload={
            "comments": [
                {
                    "author": {"login": "darkfactory"},
                    "body": (
                        f"<!-- df-quarantine:{WORKFLOW_ID} -->\n"
                        "previous quarantine comment"
                    ),
                }
            ],
            "labels": [{"name": DF_READY_LABEL}],
        }
    )
    _patch_gh(monkeypatch, recorder)

    result = asyncio.run(
        quarantine_closed_issue_activity(
            REPO, ISSUE_NUMBER, WORKFLOW_ID, "canceled"
        )
    )

    assert result["comment_posted"] is False
    assert _argvs(recorder) == [
        [
            "gh", "issue", "view", str(ISSUE_NUMBER),
            "--repo", REPO,
            "--json", "comments,labels",
        ],
        [
            "gh", "issue", "edit", str(ISSUE_NUMBER),
            "--repo", REPO,
            "--remove-label", DF_READY_LABEL,
            "--add-label", DF_CANCELED_LABEL,
        ],
    ]


def test_quarantine_does_not_treat_older_run_marker_as_current(monkeypatch):
    """Markers from a previous attempt's quarantine must not short-circuit
    quarantine of a newer attempt — comment is posted for the new workflow_id
    even when an older run's marker is in the comment thread."""
    older_workflow_id = "df-issue-octo-org-octo-repo-42-run-1"
    current_workflow_id = "df-issue-octo-org-octo-repo-42-run-2"
    recorder = _GhRecorder(
        view_payload={
            "comments": [
                {
                    "author": {"login": "darkfactory"},
                    "body": (
                        f"<!-- df-quarantine:{older_workflow_id} -->\n"
                        "Previous run's quarantine note."
                    ),
                }
            ],
            "labels": [{"name": DF_READY_LABEL}],
        }
    )
    _patch_gh(monkeypatch, recorder)

    result = asyncio.run(
        quarantine_closed_issue_activity(
            REPO, ISSUE_NUMBER, current_workflow_id, "failed"
        )
    )

    assert result["comment_posted"] is True
    assert result["workflow_id"] == current_workflow_id
    # Three calls: view, label edit, comment for the current run.
    assert len(recorder.calls) == 3
    comment_call = recorder.calls[2]
    assert f"df-quarantine:{current_workflow_id}" in comment_call["stdin"]


def test_quarantine_skips_label_edit_when_already_in_target_state(monkeypatch):
    recorder = _GhRecorder(
        view_payload={
            "comments": [
                {
                    "author": {"login": "darkfactory"},
                    "body": f"<!-- df-quarantine:{WORKFLOW_ID} -->",
                }
            ],
            "labels": [{"name": DF_CANCELED_LABEL}, {"name": "bug"}],
        }
    )
    _patch_gh(monkeypatch, recorder)

    result = asyncio.run(
        quarantine_closed_issue_activity(
            REPO, ISSUE_NUMBER, WORKFLOW_ID, "canceled"
        )
    )

    assert result == {
        "workflow_id": WORKFLOW_ID,
        "repo": REPO,
        "issue_number": ISSUE_NUMBER,
        "closure_status": "canceled",
        "label_removed": None,
        "label_added": None,
        "comment_posted": False,
    }
    assert _argvs(recorder) == [
        [
            "gh", "issue", "view", str(ISSUE_NUMBER),
            "--repo", REPO,
            "--json", "comments,labels",
        ]
    ]


@pytest.mark.parametrize(
    "repo, number, workflow_id",
    [
        ("", ISSUE_NUMBER, WORKFLOW_ID),
        ("no-slash", ISSUE_NUMBER, WORKFLOW_ID),
        (REPO, 0, WORKFLOW_ID),
        (REPO, ISSUE_NUMBER, ""),
    ],
)
def test_quarantine_rejects_invalid_inputs(monkeypatch, repo, number, workflow_id):
    recorder = _GhRecorder(view_payload={})
    _patch_gh(monkeypatch, recorder)

    with pytest.raises(ValueError):
        asyncio.run(
            quarantine_closed_issue_activity(repo, number, workflow_id, "canceled")
        )
    assert recorder.calls == []
