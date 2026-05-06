from __future__ import annotations

import asyncio

from darkfactory.runtime import activities as activities_mod
from darkfactory.runtime.activities import mark_issue_done_activity, merge_branch


class _RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def exec(self, argv, timeout=120):  # noqa: ARG002 — match RepoSandbox.exec
        self.calls.append(list(argv))
        return {"returncode": 0, "stdout": "", "stderr": ""}


def test_merge_branch_runs_gh_pr_merge_through_sandbox(monkeypatch):
    sandbox = _RecordingSandbox()
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    delta = asyncio.run(
        merge_branch(
            {
                "wf_id": "wf-merge-1",
                "task_id": "wf-merge-1",
                "feature_branch": "agent/wf-merge-1",
                "repo_path": "/workspace",
                "pr_url": "https://github.com/acme/demo/pull/42",
            }
        )
    )

    assert delta == {"merged": True}
    assert sandbox.calls == [
        ["git", "checkout", "agent/wf-merge-1"],
        [
            "gh",
            "pr",
            "merge",
            "https://github.com/acme/demo/pull/42",
            "--squash",
            "--delete-branch",
        ],
    ]


def test_mark_issue_done_activity_adds_done_label(monkeypatch):
    sandbox = _RecordingSandbox()
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    delta = asyncio.run(
        mark_issue_done_activity(
            {
                "repo": "acme/demo",
                "number": 17,
                "url": "https://github.com/acme/demo/issues/17",
                "title": "Export reports",
                "body": "Add CSV export.",
                "labels": ["df:in-progress"],
            },
            task_id="wf-merge-issue",
            repo_path="/workspace",
        )
    )

    assert delta == {"done_label_added": True}
    assert sandbox.calls == [
        [
            "gh",
            "issue",
            "edit",
            "17",
            "--repo",
            "acme/demo",
            "--add-label",
            "df:done",
        ],
    ]
