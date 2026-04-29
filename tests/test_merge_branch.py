from __future__ import annotations

import asyncio

from darkfactory.runtime import activities as activities_mod
from darkfactory.runtime.activities import merge_branch


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
