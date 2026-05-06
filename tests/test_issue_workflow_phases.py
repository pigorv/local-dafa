from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from darkfactory.runtime.approval import ApprovalSignal
from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow
from darkfactory.state import IssueRef, IssueRunRequest


_CALLS: dict[str, int] = {
    "setup": 0,
    "hydrate": 0,
    "triage": 0,
    "discovery": 0,
    "build": 0,
    "verify": 0,
    "code_quality": 0,
    "pr_creator": 0,
    "merge": 0,
    "mark_done": 0,
    "quarantine": 0,
    "teardown": 0,
}
_PHASES: list[dict[str, Any]] = []
_LABELS: list[dict[str, Any]] = []
_QUALITY_APPROVES = True


def _reset(*, quality_approves: bool = True) -> None:
    global _QUALITY_APPROVES
    _QUALITY_APPROVES = quality_approves
    for key in _CALLS:
        _CALLS[key] = 0
    _PHASES.clear()
    _LABELS.clear()


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
        "patches": [{"path": "src/demo.py", "diff": "", "author_agent": "backend", "slice_id": "story-1"}],
    }


@activity.defn(name="verify_stage")
async def verify(state: dict) -> dict:  # noqa: ARG001
    _CALLS["verify"] += 1
    return {
        "test_results": [],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
    }


@activity.defn(name="spec_adjustment_stage")
async def spec_adjustment(state: dict) -> dict:  # noqa: ARG001
    return {}


@activity.defn(name="code_quality_stage")
async def code_quality(state: dict) -> dict:  # noqa: ARG001
    _CALLS["code_quality"] += 1
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
    spec_adjustment,
    code_quality,
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
        [ApprovalSignal(kind="Approve", author="octocat", comment_id=201)],
    )

    assert result.status == "merged"
    assert [item["add"] for item in _LABELS] == [
        "df:triaging",
        "df:designing",
        "df:awaiting-approval",
        "df:building",
        "df:verifying",
        "df:in-progress",
        "df:done",
    ]
    markers = [item["marker"] for item in _PHASES]
    assert any(":triage" in marker for marker in markers)
    assert any(":design:1" in marker for marker in markers)
    assert any(":build" in marker for marker in markers)
    assert any(":verify" in marker for marker in markers)
    assert any(":pr" in marker for marker in markers)
    assert any(":merge" in marker for marker in markers)


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


def test_issue_workflow_quality_failure_escalates_to_needs_human() -> None:
    asyncio.run(_run_quality_failure())


async def _run_quality_failure() -> None:
    _reset(quality_approves=False)
    result = await _run_until_terminal(
        "test-issue-phases-quality",
        [ApprovalSignal(kind="Approve", author="octocat", comment_id=501)],
    )

    assert result.status == "needs_human"
    assert result.reason == "code_quality_failed"
    assert _CALLS["pr_creator"] == 0
    assert _LABELS[-1] == {"remove": "df:verifying", "add": "df:needs-human"}


async def _run_until_terminal(
    wf_id: str,
    signals: list[ApprovalSignal],
):
    async with await WorkflowEnvironment.start_time_skipping(
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
    for _ in range(80):
        summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
        if summary["gate_pending"] is True:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("workflow did not reach spec approval gate")
