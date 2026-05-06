from __future__ import annotations

import asyncio
import json
from typing import Any

from darkfactory.runtime import activities as activities_mod
from darkfactory.runtime.activities import upsert_phase_comment_activity
from darkfactory.runtime.phase_comment import (
    end_marker_for,
    marker_for,
    render_phase_comment,
)


def test_marker_for_uses_per_run_markers_and_design_revisions() -> None:
    wf_id = "df-issue-octo-demo-42-run-1"

    assert marker_for(wf_id, "triage") == f"<!-- df-phase:{wf_id}:triage -->"
    assert marker_for(wf_id, "build") == f"<!-- df-phase:{wf_id}:build -->"
    assert marker_for(wf_id, "design", rev=1) == (
        f"<!-- df-phase:{wf_id}:design:1 -->"
    )
    assert marker_for(wf_id, "design", rev=2) != marker_for(
        wf_id,
        "design",
        rev=1,
    )


def test_render_phase_comment_includes_status_fields_and_workflow() -> None:
    body = render_phase_comment(
        "triage",
        "done",
        {
            "outcome": "ready",
            "derived_request": "Build the thing.",
            "confidence": "high",
            "next": "design",
        },
        wf_id="wf-1",
        started_at="2026-05-06T10:00:00Z",
        ended_at="2026-05-06T10:01:00Z",
    )

    assert body.startswith("<!-- df-phase:wf-1:triage -->")
    assert "**Dark Factory" in body
    assert "Outcome: ready" in body
    assert "Derived request: Build the thing." in body
    assert "Next: design" in body
    assert "Workflow: `wf-1`" in body


class _Sandbox:
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self.comments = comments
        self.calls: list[dict[str, Any]] = []

    def exec(self, argv, timeout=120, stdin=None):  # noqa: ARG002
        self.calls.append({"argv": list(argv), "stdin": stdin})
        if argv[:3] == ["gh", "api", "--paginate"]:
            return {
                "returncode": 0,
                "stdout": json.dumps(self.comments),
                "stderr": "",
            }
        if argv[:3] == ["gh", "issue", "view"]:
            return {"returncode": 0, "stdout": json.dumps({"comments": self.comments}), "stderr": ""}
        if argv[:3] == ["gh", "issue", "comment"]:
            self.comments.append({"id": 101, "body": stdin})
            return {"returncode": 0, "stdout": "", "stderr": ""}
        if argv[:4] == ["gh", "api", "-X", "PATCH"]:
            comment_id = int(argv[4].rsplit("/", 1)[1])
            body = json.loads(stdin)["body"]
            for comment in self.comments:
                if int(comment.get("id") or 0) == comment_id:
                    comment["body"] = body
            return {"returncode": 0, "stdout": "", "stderr": ""}
        raise AssertionError(argv)


def test_upsert_phase_comment_edits_existing_marker(monkeypatch) -> None:
    marker = "<!-- df-phase:wf-1:build -->"
    sandbox = _Sandbox([{"id": 77, "body": marker + "\nold"}])
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    comment_id = asyncio.run(
        upsert_phase_comment_activity(
            {"repo": "octo/demo", "number": 42},
            marker,
            marker + "\nnew",
            task_id="wf-1",
            repo_path="/workspace",
        )
    )

    assert comment_id == 77
    assert sandbox.comments[0]["body"] == marker + "\nnew"
    assert sandbox.calls[1]["argv"][:4] == ["gh", "api", "-X", "PATCH"]


def test_upsert_phase_comment_handles_graphql_node_id_regression(monkeypatch) -> None:
    marker = "<!-- df-phase:wf-1:build -->"
    sandbox = _Sandbox([{"id": 77, "node_id": "IC_kwDOExample", "body": marker}])
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    comment_id = asyncio.run(
        upsert_phase_comment_activity(
            {"repo": "octo/demo", "number": 42},
            marker,
            marker + "\nnew",
            task_id="wf-1",
            repo_path="/workspace",
        )
    )

    assert comment_id == 77
    assert sandbox.calls[0]["argv"][:4] == [
        "gh",
        "api",
        "--paginate",
        "/repos/octo/demo/issues/42/comments",
    ]


def test_upsert_phase_comment_preserves_manual_tail_after_end_marker(monkeypatch) -> None:
    marker = "<!-- df-phase:wf-1:build -->"
    existing = marker + "\nold\n" + end_marker_for(marker) + "\n\nmanual note"
    sandbox = _Sandbox([{"id": 78, "body": existing}])
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    asyncio.run(
        upsert_phase_comment_activity(
            {"repo": "octo/demo", "number": 42},
            marker,
            marker + "\nnew\n" + end_marker_for(marker) + "\n",
            task_id="wf-1",
            repo_path="/workspace",
        )
    )

    assert sandbox.comments[0]["body"].endswith("\n\nmanual note\n")


def test_upsert_phase_comment_creates_when_marker_missing(monkeypatch) -> None:
    marker = "<!-- df-phase:wf-1:verify -->"
    sandbox = _Sandbox([])
    monkeypatch.setattr(activities_mod, "get_sandbox", lambda _task_id: sandbox)

    comment_id = asyncio.run(
        upsert_phase_comment_activity(
            {"repo": "octo/demo", "number": 42},
            marker,
            marker + "\nrunning",
            task_id="wf-1",
            repo_path="/workspace",
        )
    )

    assert comment_id == 101
    assert sandbox.comments[0]["body"] == marker + "\nrunning"
