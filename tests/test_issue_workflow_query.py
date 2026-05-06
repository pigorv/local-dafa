from __future__ import annotations

from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow
from darkfactory.state import IssueComment, IssueRef


def test_current_state_summary_includes_issue_fields_and_comment_cursor() -> None:
    workflow = DarkFactoryIssueWorkflow()
    workflow._state = {
        "issue": IssueRef(
            repo="octo-org/octo-repo",
            number=42,
            url="https://github.com/octo-org/octo-repo/issues/42",
            title="Add issue-driven workflow",
            body="Dark Factory should pick this up from an issue.",
            labels=["df:ready", "enhancement"],
        ),
        "issue_comments": [
            IssueComment(id=100, author="alice", body="Initial context", created_at="t1"),
            {"id": "bad", "author": "bot", "body": "Ignore bad cursor", "created_at": "t2"},
        ],
        "verify_retries": 1,
        "ready_to_build": False,
        "clarification_questions": ["Which API should expose this?"],
        "current_df_label": "df:awaiting-approval",
        "latest_spec_rev": 2,
    }
    workflow._new_comments = [
        IssueComment(id=105, author="bob", body="Use /v1/issues.", created_at="t3")
    ]

    summary = workflow.current_state_summary()

    assert summary["verify_retries"] == 1
    assert summary["gate_pending"] is True
    assert summary["approval_waiting"] is True
    assert summary["latest_spec_rev"] == 2
    assert summary["ready_to_build"] is False
    assert summary["clarification_questions"] == ["Which API should expose this?"]
    assert summary["issue"] == {
        "repo": "octo-org/octo-repo",
        "number": 42,
        "url": "https://github.com/octo-org/octo-repo/issues/42",
        "title": "Add issue-driven workflow",
        "labels": ["df:ready", "enhancement"],
    }
    assert summary["issue_comment_count"] == 2
    assert summary["pending_comment_count"] == 1
    assert summary["last_seen_comment_id"] == 105
