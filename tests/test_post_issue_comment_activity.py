from __future__ import annotations

import asyncio
from typing import Any

from darkfactory.runtime import activities as activities_mod
from darkfactory.runtime.activities import (
    ISSUE_COMMENT_TIMEOUT_S,
    post_issue_comment_activity,
    render_clarification_comment_body,
)
from darkfactory.state import IssueRef


class _RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def exec(
        self,
        argv: list[str],
        timeout: int = 120,
        stdin: str | bytes | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"argv": list(argv), "timeout": timeout, "stdin": stdin})
        return {"returncode": 0, "stdout": "", "stderr": ""}


def _issue() -> IssueRef:
    return IssueRef(
        repo="acme/demo",
        number=17,
        url="https://github.com/acme/demo/issues/17",
        title="Export reports",
        body="Add CSV export.",
        labels=["df:ready"],
    )


def test_render_clarification_comment_body_includes_hidden_marker_and_questions():
    body = render_clarification_comment_body(
        _issue(),
        ["Which export format should be default?", "Who can access the export?"],
        workflow_id="df-issue-acme-demo-17",
        clarification_round=2,
    )

    assert body.startswith("<!-- df-clarify:df-issue-acme-demo-17:2 -->")
    assert "Dark Factory needs a bit more context for issue #17." in body
    assert "- Which export format should be default?" in body
    assert "- Who can access the export?" in body
    assert "workflow_id: `df-issue-acme-demo-17`" in body


def test_post_issue_comment_activity_uses_allowlisted_gh_comment_subset(monkeypatch):
    sandbox = _RecordingSandbox()
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    result = asyncio.run(
        post_issue_comment_activity(
            _issue(),
            ["Which export format should be default?"],
            task_id="df-issue-acme-demo-17",
            clarification_round=1,
        )
    )

    assert result == {"issue_comment_posted": True}
    assert sandbox.calls == [
        {
            "argv": [
                "gh",
                "issue",
                "comment",
                "17",
                "--repo",
                "acme/demo",
                "--body-file",
                "-",
            ],
            "timeout": ISSUE_COMMENT_TIMEOUT_S,
            "stdin": (
                "<!-- df-clarify:df-issue-acme-demo-17:1 -->\n"
                "Dark Factory needs a bit more context for issue #17.\n"
                "\n"
                "- Which export format should be default?\n"
                "\n"
                "Reply on this issue when you have the details.\n"
                "workflow_id: `df-issue-acme-demo-17`"
            ),
        }
    ]


def test_post_issue_comment_activity_uses_allowlisted_needs_human_label_subset(
    monkeypatch,
):
    sandbox = _RecordingSandbox()
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    result = asyncio.run(
        post_issue_comment_activity(
            _issue(),
            ["Which export format should be default?"],
            task_id="df-issue-acme-demo-17",
            clarification_round=3,
            mark_needs_human=True,
        )
    )

    assert result == {
        "issue_comment_posted": True,
        "needs_human_label_added": True,
    }
    assert [call["argv"] for call in sandbox.calls] == [
        [
            "gh",
            "issue",
            "comment",
            "17",
            "--repo",
            "acme/demo",
            "--body-file",
            "-",
        ],
        [
            "gh",
            "issue",
            "edit",
            "17",
            "--repo",
            "acme/demo",
            "--add-label",
            "df:needs-human",
        ],
    ]
    assert sandbox.calls[0]["timeout"] == ISSUE_COMMENT_TIMEOUT_S
    assert sandbox.calls[1]["timeout"] == ISSUE_COMMENT_TIMEOUT_S
    assert sandbox.calls[0]["stdin"].startswith(
        "<!-- df-clarify:df-issue-acme-demo-17:3 -->"
    )
