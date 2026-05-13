from __future__ import annotations

import asyncio
from typing import Callable

from darkfactory.runtime import activities as activities_mod
from darkfactory.runtime.activities import mark_issue_done_activity, merge_branch


class _RecordingSandbox:
    def __init__(
        self,
        responder: Callable[[list[str]], dict] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._responder = responder or (
            lambda _argv: {"returncode": 0, "stdout": "", "stderr": ""}
        )

    def exec(self, argv, timeout=120):  # noqa: ARG002 — match RepoSandbox.exec
        argv_list = list(argv)
        self.calls.append(argv_list)
        return self._responder(argv_list)


_PR_URL = "https://github.com/acme/demo/pull/42"
_STATE_VIEW = [
    "gh",
    "pr",
    "view",
    _PR_URL,
    "--json",
    "state",
    "--jq",
    ".state",
]
_GH_MERGE = [
    "gh",
    "pr",
    "merge",
    _PR_URL,
    "--squash",
    "--delete-branch",
]


def _merge_state(**overrides):
    base = {
        "wf_id": "wf-merge-1",
        "task_id": "wf-merge-1",
        "feature_branch": "agent/wf-merge-1",
        "repo_path": "/workspace",
        "pr_url": _PR_URL,
    }
    base.update(overrides)
    return base


def test_merge_branch_runs_gh_pr_merge_through_sandbox(monkeypatch):
    def respond(argv: list[str]) -> dict:
        if argv == _STATE_VIEW:
            return {"returncode": 0, "stdout": "OPEN\n", "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    sandbox = _RecordingSandbox(respond)
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    delta = asyncio.run(merge_branch(_merge_state()))

    assert delta == {"merged": True}
    assert sandbox.calls == [
        ["git", "checkout", "agent/wf-merge-1"],
        _STATE_VIEW,
        _GH_MERGE,
    ]


def test_merge_branch_is_idempotent_when_pr_already_merged(monkeypatch):
    def respond(argv: list[str]) -> dict:
        if argv == _STATE_VIEW:
            return {"returncode": 0, "stdout": "MERGED\n", "stderr": ""}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    sandbox = _RecordingSandbox(respond)
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    delta = asyncio.run(merge_branch(_merge_state()))

    assert delta == {"merged": True}
    # The pre-check short-circuits before gh pr merge is invoked.
    assert sandbox.calls == [
        ["git", "checkout", "agent/wf-merge-1"],
        _STATE_VIEW,
    ]


def test_merge_branch_treats_local_cleanup_failure_as_success_when_pr_merged(
    monkeypatch,
):
    """gh pr merge can succeed on GitHub but fail locally on --delete-branch."""
    state_responses = iter(["OPEN\n", "MERGED\n"])

    def respond(argv: list[str]) -> dict:
        if argv == _STATE_VIEW:
            return {
                "returncode": 0,
                "stdout": next(state_responses),
                "stderr": "",
            }
        if argv == _GH_MERGE:
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": (
                    "error: Your local changes to the following files would be "
                    "overwritten by checkout:\n\tsrc/test/java/X.java\nAborting"
                ),
            }
        return {"returncode": 0, "stdout": "", "stderr": ""}

    sandbox = _RecordingSandbox(respond)
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    delta = asyncio.run(merge_branch(_merge_state()))

    assert delta == {"merged": True}
    assert sandbox.calls == [
        ["git", "checkout", "agent/wf-merge-1"],
        _STATE_VIEW,
        _GH_MERGE,
        _STATE_VIEW,
    ]


def test_merge_branch_raises_when_gh_merge_fails_and_pr_not_merged(monkeypatch):
    def respond(argv: list[str]) -> dict:
        if argv == _STATE_VIEW:
            return {"returncode": 0, "stdout": "OPEN\n", "stderr": ""}
        if argv == _GH_MERGE:
            return {"returncode": 1, "stdout": "", "stderr": "boom"}
        return {"returncode": 0, "stdout": "", "stderr": ""}

    sandbox = _RecordingSandbox(respond)
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    try:
        asyncio.run(merge_branch(_merge_state()))
    except RuntimeError as exc:
        assert "gh pr merge failed" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when gh pr merge fails")


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
