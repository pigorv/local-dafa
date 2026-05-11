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
    render_spec_markdown,
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


def test_marker_for_review_requires_iteration_per_pass() -> None:
    wf_id = "df-issue-octo-demo-42-run-1"

    assert marker_for(wf_id, "review", attempt=1) == (
        f"<!-- df-phase:{wf_id}:review:1 -->"
    )
    assert marker_for(wf_id, "review", attempt=2) != marker_for(
        wf_id,
        "review",
        attempt=1,
    )

    try:
        marker_for(wf_id, "review")
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("review marker without iteration should raise")


def test_render_review_recommends_action_based_on_verdict() -> None:
    body = render_phase_comment(
        "review",
        "done",
        {
            "pr_url": "https://example/pr/1",
            "review_decision": {
                "recommendation": "request_changes",
                "severity": "medium",
                "issues": ["lint regression"],
            },
            "include_merge_instructions": True,
        },
        wf_id="wf-1",
        attempt=1,
    )

    assert body.startswith("<!-- df-phase:wf-1:review:1 -->")
    assert "### Recommended next actions" in body
    assert "Reviewer recommends: **fix**." in body
    assert "`/df fix <focus>` — re-run Fixer and Verifier ← recommended" in body
    assert "`/df approve` — merge the PR" in body


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


def test_render_spec_markdown_frames_work_package_files_as_hints() -> None:
    body = render_spec_markdown(
        user_request="Expose cursor pagination.",
        stories=[
            {
                "id": "US-1",
                "title": "Cursor pagination",
                "acceptance_criteria": ["clients can request the next page"],
            }
        ],
        spec=[
            {
                "id": "WP-1",
                "story_id": "US-1",
                "title": "Cursor token flow",
                "intent": "Return and consume stable cursor tokens.",
                "repo_areas": ["API user lookup flow"],
                "candidate_files": ["src/users/api.py", "tests/test_users_api.py"],
                "verification": [
                    "first page returns a next cursor",
                    {"predicate": "second page starts after the cursor"},
                ],
                "dependencies": [],
            }
        ],
    )

    assert "### Work Packages" in body
    assert "- **WP-1**: Cursor token flow" in body
    assert "Candidate files (hints): src/users/api.py, tests/test_users_api.py" in body
    assert "  - Verification predicates:" in body
    assert "    - first page returns a next cursor" in body
    assert "    - second page starts after the cursor" in body
    assert "allowed files" not in body.lower()
    assert "must edit" not in body.lower()
    assert "Affected files" not in body


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
