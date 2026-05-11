from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.runtime.approval import ApprovalSignal
from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow
from darkfactory.state import IssueRef, IssueRunRequest
from tests.temporal_testing import start_time_skipping_env


_CALLS: dict[str, int] = {
    "setup": 0,
    "hydrate": 0,
    "triage": 0,
    "discovery": 0,
    "build": 0,
    "verify": 0,
    "reviewer": 0,
    "pr_creator": 0,
    "merge": 0,
    "mark_done": 0,
    "quarantine": 0,
    "teardown": 0,
}
_PHASES: list[dict[str, Any]] = []
_LABELS: list[dict[str, Any]] = []
_REVIEW_PR_URLS: list[str | None] = []
_QUALITY_APPROVES = True


def _reset(*, quality_approves: bool = True) -> None:
    global _QUALITY_APPROVES
    _QUALITY_APPROVES = quality_approves
    for key in _CALLS:
        _CALLS[key] = 0
    _PHASES.clear()
    _LABELS.clear()
    _REVIEW_PR_URLS.clear()


def _req() -> IssueRunRequest:
    return IssueRunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        issue=IssueRef(
            repo="octo-org/octo-repo",
            number=42,
            url="https://github.com/octo-org/octo-repo/issues/42",
            title="Phase visibility",
            body="Build a visible issue workflow.",
            labels=["df:ready"],
        ),
    )


@activity.defn(name="setup_worker_activity")
async def setup_worker(wf_id: str, repo_url: str) -> str:  # noqa: ARG001
    _CALLS["setup"] += 1
    return f"worker-{wf_id}"


@activity.defn(name="teardown_worker_activity")
async def teardown_worker(wf_id: str) -> None:  # noqa: ARG001
    _CALLS["teardown"] += 1


@activity.defn(name="hydrate_stage")
async def hydrate(state: dict) -> dict:
    _CALLS["hydrate"] += 1
    return {"repo_context": {"repo_root": state.get("repo_path")}}


@activity.defn(name="triage_stage")
async def triage(state: dict) -> dict:  # noqa: ARG001
    _CALLS["triage"] += 1
    return {
        "ready_to_build": True,
        "clarification_questions": [],
        "derived_user_request": "Implement visible phase updates.",
        "confidence": "high",
        "rationale": "Enough detail.",
    }


@activity.defn(name="upsert_phase_comment_activity")
async def upsert_phase(
    issue: Any,  # noqa: ARG001
    marker: str,
    body: str,
    task_id: str | None = None,  # noqa: ARG001
    repo_path: str = "/workspace",  # noqa: ARG001
) -> int:
    _PHASES.append({"marker": marker, "body": body})
    return len(_PHASES)


@activity.defn(name="swap_state_label_activity")
async def swap_label(
    issue: Any,  # noqa: ARG001
    remove: str | list[str] | None,
    add: str | list[str] | None,
    task_id: str | None = None,  # noqa: ARG001
    repo_path: str = "/workspace",  # noqa: ARG001
) -> dict[str, Any]:
    _LABELS.append({"remove": remove, "add": add})
    return {"labels_removed": [], "labels_added": []}


@activity.defn(name="post_issue_comment_activity")
async def post_issue_comment(*_args, **_kwargs) -> dict:
    return {"issue_comment_posted": True}


@activity.defn(name="discovery_stage")
async def discovery(state: dict) -> dict:
    _CALLS["discovery"] += 1
    rev = state.get("latest_spec_rev") or _CALLS["discovery"]
    return {
        "stories": [
            {
                "id": f"story-{rev}",
                "title": f"Visible phase rev {rev}",
                "acceptance_criteria": ["phase comment appears"],
            }
        ],
        "spec": [
            {
                "story_id": f"story-{rev}",
                "approach": "Render and upsert comments.",
                "affected_files": ["src/darkfactory/runtime/issue_workflow.py"],
                "new_files": [],
                "test_files": ["tests/test_issue_workflow_phases.py"],
                "verification": ["design comment presents file paths as hints"],
                "risks": [],
                "depends_on": [],
            }
        ],
        "review_decision": None,
    }


@activity.defn(name="build_stage")
async def build(state: dict) -> dict:  # noqa: ARG001
    _CALLS["build"] += 1
    return {
        "build_order": ["story-1"],
        "current_slice": "story-1",
        "patches": [{"path": "src/demo.py", "diff": "", "author_agent": "builder", "slice_id": "story-1"}],
    }


@activity.defn(name="verify_stage")
async def verify(state: dict) -> dict:  # noqa: ARG001
    _CALLS["verify"] += 1
    return {
        "test_results": [],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
    }


@activity.defn(name="fixer_stage")
async def fixer(state: dict) -> dict:  # noqa: ARG001
    return {}


@activity.defn(name="reviewer_stage")
async def reviewer(state: dict) -> dict:
    _CALLS["reviewer"] += 1
    _REVIEW_PR_URLS.append(state.get("pr_url"))
    return {
        "review_decision": {
            "severity": "low" if _QUALITY_APPROVES else "high",
            "issues": [] if _QUALITY_APPROVES else ["lint regression"],
            "recommendation": "approve" if _QUALITY_APPROVES else "request_changes",
        }
    }


@activity.defn(name="pr_creator_stage")
async def pr_creator(state: dict) -> dict:
    _CALLS["pr_creator"] += 1
    assert state["approval_record"]["author"] == "octocat"
    return {"pr_url": "https://github.example/octo-org/octo-repo/pull/9"}


@activity.defn(name="merge_branch")
async def merge_branch(state: dict) -> dict:  # noqa: ARG001
    _CALLS["merge"] += 1
    return {"merged": True}


@activity.defn(name="mark_issue_done_activity")
async def mark_done(*_args, **_kwargs) -> dict:
    _CALLS["mark_done"] += 1
    return {"done_label_added": True}


@activity.defn(name="quarantine_closed_issue_activity")
async def quarantine(*_args, **_kwargs) -> dict:
    _CALLS["quarantine"] += 1
    return {"comment_posted": True}


_SUPERVISOR = (setup_worker, teardown_worker, quarantine)
_AGENT = (
    hydrate,
    triage,
    upsert_phase,
    swap_label,
    post_issue_comment,
    discovery,
    build,
    verify,
    fixer,
    reviewer,
    pr_creator,
    merge_branch,
    mark_done,
)


def test_issue_workflow_phase_comments_and_labels_happy_path() -> None:
    asyncio.run(_run_happy())


async def _run_happy() -> None:
    _reset()
    result = await _run_until_terminal(
        "test-issue-phases-happy",
        [
            ApprovalSignal(kind="Approve", author="octocat", comment_id=201),
            ApprovalSignal(kind="Approve", author="octocat", comment_id=251),
        ],
    )

    assert result.status == "merged"
    assert _REVIEW_PR_URLS == ["https://github.example/octo-org/octo-repo/pull/9"]
    assert [item["add"] for item in _LABELS] == [
        "df:triaging",
        "df:designing",
        "df:awaiting-approval",
        "df:building",
        "df:verifying",
        "df:reviewing",
        "df:awaiting-merge",
        "df:in-progress",
        "df:done",
    ]
    markers = [item["marker"] for item in _PHASES]
    assert any(":triage" in marker for marker in markers)
    assert any(":design:1" in marker for marker in markers)
    assert any(":build" in marker for marker in markers)
    assert any(":verify" in marker for marker in markers)
    assert any(":pr" in marker for marker in markers)
    assert any(":review:1" in marker for marker in markers)
    assert any(":merge" in marker for marker in markers)
    review_bodies = [
        item["body"] for item in _PHASES if ":review" in item["marker"]
    ]
    review_done = next(
        body for body in reversed(review_bodies) if "Recommendation:" in body
    )
    assert "PR: https://github.example/octo-org/octo-repo/pull/9" in review_done
    assert "Recommendation: approve" in review_done
    assert "### Recommended next actions" in review_done
    assert "Reviewer recommends: **approve**." in review_done
    assert "`/df approve`" in review_done
    assert "`/df fix <focus>`" in review_done
    assert "`/df rebuild <focus>`" in review_done
    design_body = next(
        item["body"]
        for item in _PHASES
        if ":design:1" in item["marker"] and "### Work Packages" in item["body"]
    )
    assert "### Work Packages" in design_body
    assert "Candidate files (hints): src/darkfactory/runtime/issue_workflow.py" in design_body
    assert "  - Verification predicates:" in design_body
    assert "    - design comment presents file paths as hints" in design_body
    assert "allowed files" not in design_body.lower()
    assert "must edit" not in design_body.lower()


def test_issue_workflow_revise_posts_new_design_revision() -> None:
    asyncio.run(_run_revise())


async def _run_revise() -> None:
    _reset()
    result = await _run_until_terminal(
        "test-issue-phases-revise",
        [
            ApprovalSignal(
                kind="Revise",
                author="octocat",
                comment_id=301,
                text="Add an edge case.",
            ),
            ApprovalSignal(kind="Approve", author="octocat", comment_id=302),
            ApprovalSignal(kind="Approve", author="octocat", comment_id=352),
        ],
    )

    assert result.status == "merged"
    assert _CALLS["discovery"] == 2
    markers = [item["marker"] for item in _PHASES]
    assert any(":design:1" in marker for marker in markers)
    assert any(":design:2" in marker for marker in markers)


def test_issue_workflow_reject_cancels_before_build() -> None:
    asyncio.run(_run_reject())


async def _run_reject() -> None:
    _reset()
    result = await _run_until_terminal(
        "test-issue-phases-reject",
        [
            ApprovalSignal(
                kind="Reject",
                author="octocat",
                comment_id=401,
                text="Wrong scope.",
            )
        ],
    )

    assert result.status == "canceled"
    assert result.reason == "Wrong scope."
    assert _CALLS["build"] == 0
    assert _CALLS["quarantine"] == 1
    assert _LABELS[-1] == {
        "remove": ["df:awaiting-approval", "df:cancel"],
        "add": "df:canceled",
    }


def test_issue_workflow_quality_failure_waits_for_human_at_merge_gate() -> None:
    asyncio.run(_run_quality_failure())


async def _run_quality_failure() -> None:
    _reset(quality_approves=False)
    result = await _run_until_terminal(
        "test-issue-phases-quality",
        [
            ApprovalSignal(kind="Approve", author="octocat", comment_id=501),
            ApprovalSignal(
                kind="Reject",
                author="octocat",
                comment_id=551,
                text="Reviewer flagged a regression.",
            ),
        ],
    )

    assert result.status == "rejected"
    assert result.reason == "Reviewer flagged a regression."
    assert _CALLS["pr_creator"] == 1
    assert _REVIEW_PR_URLS == ["https://github.example/octo-org/octo-repo/pull/9"]
    assert _CALLS["merge"] == 0
    assert _CALLS["quarantine"] == 1
    review_bodies = [
        item["body"] for item in _PHASES if ":review" in item["marker"]
    ]
    review_done = next(
        body for body in reversed(review_bodies) if "Recommendation:" in body
    )
    assert "Recommendation: request_changes" in review_done
    assert "lint regression" in review_done
    assert _LABELS[-1] == {
        "remove": ["df:awaiting-merge", "df:cancel"],
        "add": "df:canceled",
    }


def test_issue_workflow_fix_then_approve_emits_distinct_review_passes() -> None:
    asyncio.run(_run_fix_then_approve())


async def _run_fix_then_approve() -> None:
    _reset()
    result = await _run_until_terminal(
        "test-issue-phases-fix",
        [
            ApprovalSignal(kind="Approve", author="octocat", comment_id=600),
            ApprovalSignal(
                kind="Fix",
                author="octocat",
                comment_id=601,
                text="address lint regression",
            ),
            ApprovalSignal(kind="Approve", author="octocat", comment_id=602),
        ],
    )

    assert result.status == "merged"
    assert _CALLS["reviewer"] == 2
    assert _REVIEW_PR_URLS == [
        "https://github.example/octo-org/octo-repo/pull/9",
        "https://github.example/octo-org/octo-repo/pull/9",
    ]

    review_markers = sorted(
        {item["marker"] for item in _PHASES if ":review:" in item["marker"]}
    )
    assert any(marker.endswith(":review:1 -->") for marker in review_markers)
    assert any(marker.endswith(":review:2 -->") for marker in review_markers)
    # idempotent within a pass — exactly one comment id per pass.
    review_done_per_pass: dict[str, int] = {}
    for item in _PHASES:
        marker = item["marker"]
        if ":review:" not in marker:
            continue
        if "Recommendation:" in item["body"]:
            review_done_per_pass[marker] = (
                review_done_per_pass.get(marker, 0) + 1
            )
    assert all(count == 1 for count in review_done_per_pass.values())

    label_adds = [item["add"] for item in _LABELS]
    assert "df:reviewing" in label_adds
    assert "df:awaiting-merge" in label_adds
    assert "df:fixing" in label_adds


async def _run_until_terminal(
    wf_id: str,
    signals: list[ApprovalSignal],
):
    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_SUPERVISOR),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_AGENT),
        ):
            handle = await env.client.start_workflow(
                DarkFactoryIssueWorkflow.run,
                _req(),
                id=wf_id,
                task_queue="supervisor-tq",
            )
            for signal in signals:
                await _wait_until_gate(handle, signal.comment_id)
                await handle.execute_update(
                    DarkFactoryIssueWorkflow.signal_approval,
                    signal,
                )
            return await handle.result()


async def _wait_until_gate(handle, comment_id: int) -> None:  # noqa: ARG001
    # First, wait for any in-flight signal to be consumed by the workflow.
    for _ in range(160):
        summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
        if summary.get("approval_signal_pending") is False:
            break
        await asyncio.sleep(0.05)
    # Then, wait for the workflow to open the next gate.
    for _ in range(160):
        summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
        if (
            summary["gate_pending"] is True
            and summary.get("approval_signal_pending") is False
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("workflow did not reach the next approval gate")
