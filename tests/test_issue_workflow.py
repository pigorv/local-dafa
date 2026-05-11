"""Happy-path coverage for the issue-driven Temporal workflow."""
from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.runtime.approval import ApprovalSignal
from darkfactory.runtime.issue_workflow import (
    MAX_CLARIFICATION_ROUNDS,
    DarkFactoryIssueWorkflow,
)
from darkfactory.state import IssueComment, IssueRef, IssueRunRequest
from tests.temporal_testing import start_time_skipping_env


_CALLS: dict[str, int] = {
    "setup": 0,
    "hydrate": 0,
    "triage": 0,
    "discovery": 0,
    "build": 0,
    "verify": 0,
    "fixer": 0,
    "reviewer": 0,
    "pr_creator": 0,
    "merge": 0,
    "mark_done": 0,
    "teardown": 0,
}

_POSTED_COMMENTS: list[dict[str, Any]] = []
_PHASE_COMMENTS: list[dict[str, Any]] = []
_LABEL_SWAPS: list[dict[str, Any]] = []
_TRIAGE_COMMENT_COUNTS: list[int] = []
_TRIAGE_RESPONSES: list[dict[str, Any]] = []
_DOWNSTREAM_USER_REQUESTS = {
    "Implement the fully specified issue.",
    "Implement after the clarification reply.",
}


def _reset_calls() -> None:
    for key in _CALLS:
        _CALLS[key] = 0
    _POSTED_COMMENTS.clear()
    _PHASE_COMMENTS.clear()
    _LABEL_SWAPS.clear()
    _TRIAGE_COMMENT_COUNTS.clear()
    _TRIAGE_RESPONSES.clear()


def _state_value(obj: object, key: str, default: object = None) -> object:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _ready_triage_output(user_request: str) -> dict[str, Any]:
    return {
        "ready_to_build": True,
        "clarification_questions": [],
        "derived_user_request": user_request,
        "confidence": "high",
        "rationale": "The issue includes enough detail to build.",
    }


def _clarifying_triage_output(round_number: int) -> dict[str, Any]:
    return {
        "ready_to_build": False,
        "clarification_questions": [f"Clarification question {round_number}?"],
        "derived_user_request": "",
        "confidence": "low",
        "rationale": f"Round {round_number} still needs user context.",
    }


def _issue_run_request() -> IssueRunRequest:
    return IssueRunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        issue=IssueRef(
            repo="octo-org/octo-repo",
            number=42,
            url="https://github.com/octo-org/octo-repo/issues/42",
            title="Add issue-driven workflow",
            body="Build the happy path from ready issue to merge.",
            labels=["df:ready"],
        ),
    )


@activity.defn(name="setup_worker_activity")
async def stub_setup_worker_activity(wf_id: str, repo_url: str) -> str:
    _CALLS["setup"] += 1
    assert repo_url == "/tmp/fake-repo"
    return f"darkfactory-worker-{wf_id}"


@activity.defn(name="teardown_worker_activity")
async def stub_teardown_worker_activity(wf_id: str) -> None:
    _CALLS["teardown"] += 1


@activity.defn(name="hydrate_stage")
async def stub_hydrate_stage(state: dict) -> dict:
    _CALLS["hydrate"] += 1
    issue = state["issue"]
    assert issue["number"] == 42
    return {
        "repo_context": {"repo_root": state.get("repo_path"), "files": ["README.md"]},
        "issue_comments": [],
    }


@activity.defn(name="triage_stage")
async def stub_triage_stage(state: dict) -> dict:
    _CALLS["triage"] += 1
    assert state["issue"]["title"] == "Add issue-driven workflow"
    assert "user_request" not in state
    _TRIAGE_COMMENT_COUNTS.append(len(state.get("issue_comments") or []))
    if _TRIAGE_RESPONSES:
        return _TRIAGE_RESPONSES.pop(0)
    return _ready_triage_output("Implement the fully specified issue.")


@activity.defn(name="post_issue_comment_activity")
async def stub_post_issue_comment_activity(
    issue: Any,
    clarification_questions: list[str] | str,
    task_id: str | None = None,
    repo_path: str = "/workspace",
    clarification_round: int = 1,
    mark_needs_human: bool = False,
) -> dict:
    assert _state_value(issue, "number") == 42
    assert repo_path == "/tmp/fake-repo"
    _POSTED_COMMENTS.append(
        {
            "questions": clarification_questions,
            "task_id": task_id,
            "clarification_round": clarification_round,
            "mark_needs_human": mark_needs_human,
        }
    )
    result = {"issue_comment_posted": True}
    if mark_needs_human:
        result["needs_human_label_added"] = True
    return result


@activity.defn(name="upsert_phase_comment_activity")
async def stub_upsert_phase_comment_activity(
    issue: Any,
    marker: str,
    body: str,
    task_id: str | None = None,
    repo_path: str = "/workspace",
) -> int:
    assert _state_value(issue, "number") == 42
    assert marker in body
    assert task_id is not None
    assert repo_path == "/tmp/fake-repo"
    _PHASE_COMMENTS.append({"marker": marker, "body": body})
    return len(_PHASE_COMMENTS)


@activity.defn(name="swap_state_label_activity")
async def stub_swap_state_label_activity(
    issue: Any,
    remove: str | list[str] | None,
    add: str | list[str] | None,
    task_id: str | None = None,
    repo_path: str = "/workspace",
) -> dict[str, Any]:
    assert _state_value(issue, "number") == 42
    assert task_id is not None
    assert repo_path == "/tmp/fake-repo"
    _LABEL_SWAPS.append({"remove": remove, "add": add})
    return {"labels_removed": [remove] if isinstance(remove, str) else remove or [], "labels_added": [add] if isinstance(add, str) else add or []}


@activity.defn(name="discovery_stage")
async def stub_discovery_stage(state: dict) -> dict:
    _CALLS["discovery"] += 1
    assert state["ready_to_build"] is True
    assert state["user_request"] in _DOWNSTREAM_USER_REQUESTS
    return {
        "stories": [
            {
                "id": "story-1",
                "title": "Issue workflow",
                "as_a": "maintainer",
                "i_want": "issues to drive workflow runs",
                "so_that": "ready issues can be built automatically",
                "acceptance_criteria": ["ready issue reaches merge"],
            }
        ],
        "spec": [
            {
                "story_id": "story-1",
                "approach": "Stubbed happy path",
                "affected_files": [],
                "new_files": [],
                "test_files": [],
                "risks": [],
                "depends_on": [],
            }
        ],
        "review_decision": None,
    }


@activity.defn(name="build_stage")
async def stub_build_stage(state: dict) -> dict:
    _CALLS["build"] += 1
    assert state["user_request"] in _DOWNSTREAM_USER_REQUESTS
    return {"build_order": ["story-1"], "current_slice": "story-1", "patches": []}


@activity.defn(name="verify_stage")
async def stub_verify_stage(state: dict) -> dict:
    _CALLS["verify"] += 1
    return {
        "test_results": [],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
    }


@activity.defn(name="fixer_stage")
async def stub_fixer_stage(state: dict) -> dict:
    _CALLS["fixer"] += 1
    return {}


@activity.defn(name="reviewer_stage")
async def stub_reviewer_stage(state: dict) -> dict:
    _CALLS["reviewer"] += 1
    assert state["verify_summary"]["passed"] is True
    assert state["pr_url"] == "https://github.example/octo-org/octo-repo/pull/7"
    return {
        "review_decision": {
            "severity": "low",
            "issues": [],
            "recommendation": "approve",
        }
    }


@activity.defn(name="pr_creator_stage")
async def stub_pr_creator_stage(state: dict) -> dict:
    _CALLS["pr_creator"] += 1
    assert state["gate_approved"] is True
    assert state.get("merge_gate_approved", False) is False
    assert state["issue"]["number"] == 42
    return {"pr_url": "https://github.example/octo-org/octo-repo/pull/7"}


@activity.defn(name="merge_branch")
async def stub_merge_branch(state: dict) -> dict:
    _CALLS["merge"] += 1
    assert state["pr_url"] == "https://github.example/octo-org/octo-repo/pull/7"
    return {"merged": True}


@activity.defn(name="mark_issue_done_activity")
async def stub_mark_issue_done_activity(
    issue: Any,
    task_id: str | None = None,
    repo_path: str = "/workspace",
) -> dict:
    _CALLS["mark_done"] += 1
    assert _state_value(issue, "number") == 42
    assert task_id is not None
    assert repo_path == "/tmp/fake-repo"
    return {"done_label_added": True}


_SUPERVISOR_ACTIVITIES = (
    stub_setup_worker_activity,
    stub_teardown_worker_activity,
)

_AGENT_ACTIVITIES = (
    stub_hydrate_stage,
    stub_triage_stage,
    stub_upsert_phase_comment_activity,
    stub_swap_state_label_activity,
    stub_post_issue_comment_activity,
    stub_discovery_stage,
    stub_build_stage,
    stub_verify_stage,
    stub_fixer_stage,
    stub_reviewer_stage,
    stub_pr_creator_stage,
    stub_merge_branch,
    stub_mark_issue_done_activity,
)


def test_issue_workflow_happy_path_merges_ready_issue() -> None:
    asyncio.run(_run_issue_workflow_happy_path())


async def _run_issue_workflow_happy_path() -> None:
    _reset_calls()
    wf_id = "test-issue-workflow-happy-path"
    req = _issue_run_request()

    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_SUPERVISOR_ACTIVITIES),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_AGENT_ACTIVITIES),
        ):
            handle = await env.client.start_workflow(
                DarkFactoryIssueWorkflow.run,
                req,
                id=wf_id,
                task_queue="supervisor-tq",
            )

            summary = await _wait_until_spec_gate_pending(handle)
            assert summary["gate_pending"] is True
            assert summary["ready_to_build"] is True
            assert summary["issue"]["number"] == 42
            assert summary["issue_comment_count"] == 0
            assert _CALLS["triage"] == 1
            assert _CALLS["discovery"] == 1
            assert _CALLS["build"] == 0
            assert _CALLS["reviewer"] == 0
            assert _CALLS["pr_creator"] == 0
            assert _CALLS["merge"] == 0

            await handle.execute_update(
                DarkFactoryIssueWorkflow.signal_approval,
                ApprovalSignal(
                    kind="Approve",
                    author="octocat",
                    comment_id=900,
                    text="happy path accepted",
                ),
            )

            await _wait_until_merge_gate_pending(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.signal_approval,
                ApprovalSignal(
                    kind="Approve",
                    author="octocat",
                    comment_id=950,
                    text="merge approved",
                ),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert result.reason is None
    assert result.state["ready_to_build"] is True
    assert result.state["user_request"] == "Implement the fully specified issue."
    assert result.state["gate_approved"] is True
    assert result.state["merge_gate_approved"] is True
    assert result.state["approval_record"]["author"] == "octocat"
    assert result.state["approved_spec_rev"] == 1
    assert result.state["pr_url"] == "https://github.example/octo-org/octo-repo/pull/7"
    assert result.state["merged"] is True
    assert result.state["done_label_added"] is True
    assert result.state["issue"]["number"] == 42
    assert _CALLS == {
        "setup": 1,
        "hydrate": 1,
        "triage": 1,
        "discovery": 1,
        "build": 1,
        "verify": 1,
        "fixer": 0,
        "reviewer": 1,
        "pr_creator": 1,
        "merge": 1,
        "mark_done": 1,
        "teardown": 1,
    }
    assert _POSTED_COMMENTS == []
    assert _LABEL_SWAPS[0] == {"remove": "df:ready", "add": "df:triaging"}
    assert {
        "remove": ["df:awaiting-approval", "df:approved"],
        "add": "df:building",
    } in _LABEL_SWAPS
    assert any("df-phase" in call["marker"] for call in _PHASE_COMMENTS)
    assert _TRIAGE_COMMENT_COUNTS == [0]


def test_issue_workflow_comment_update_releases_wait_and_reruns_triage() -> None:
    asyncio.run(_run_issue_workflow_comment_update())


async def _run_issue_workflow_comment_update() -> None:
    _reset_calls()
    wf_id = "test-issue-workflow-comment-update"
    req = _issue_run_request()
    _TRIAGE_RESPONSES.extend(
        [
            _clarifying_triage_output(1),
            _ready_triage_output("Implement after the clarification reply."),
        ]
    )

    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_SUPERVISOR_ACTIVITIES),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_AGENT_ACTIVITIES),
        ):
            handle = await env.client.start_workflow(
                DarkFactoryIssueWorkflow.run,
                req,
                id=wf_id,
                task_queue="supervisor-tq",
            )

            await _wait_until_posted_comments(1)
            summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
            assert summary["ready_to_build"] is False
            assert summary["clarification_questions"] == [
                "Clarification question 1?"
            ]
            assert summary["issue_comment_count"] == 0
            assert summary["pending_comment_count"] == 0
            assert _POSTED_COMMENTS == [
                {
                    "questions": ["Clarification question 1?"],
                    "task_id": wf_id,
                    "clarification_round": 1,
                    "mark_needs_human": False,
                }
            ]

            await handle.execute_update(
                DarkFactoryIssueWorkflow.post_new_comments,
                [
                    IssueComment(
                        id=500,
                        author="darkfactory",
                        body=(
                            f"<!-- df-clarify:{wf_id}:1 -->\n"
                            "Dark Factory needs a bit more context."
                        ),
                        created_at="2026-05-05T09:59:00Z",
                    ),
                    IssueComment(
                        id=501,
                        author="octocat",
                        body="Please build it for the settings page.",
                        created_at="2026-05-05T10:00:00Z",
                    )
                ],
            )
            summary = await _wait_until_spec_gate_pending(handle)
            assert _CALLS["triage"] == 2
            assert _TRIAGE_COMMENT_COUNTS == [0, 1]
            assert summary["ready_to_build"] is True
            assert summary["issue_comment_count"] == 1
            assert summary["pending_comment_count"] == 0
            assert summary["last_seen_comment_id"] == 501

            await handle.execute_update(
                DarkFactoryIssueWorkflow.signal_approval,
                ApprovalSignal(
                    kind="Approve",
                    author="octocat",
                    comment_id=901,
                    text="clarified path accepted",
                ),
            )
            await _wait_until_merge_gate_pending(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.signal_approval,
                ApprovalSignal(
                    kind="Approve",
                    author="octocat",
                    comment_id=951,
                    text="merge approved",
                ),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert result.reason is None
    assert result.state["user_request"] == "Implement after the clarification reply."
    assert len(result.state["issue_comments"]) == 1
    assert result.state["issue_comments"][0]["id"] == 501
    assert _CALLS["discovery"] == 1
    assert _CALLS["pr_creator"] == 1
    assert _CALLS["merge"] == 1
    assert _CALLS["mark_done"] == 1
    assert _CALLS["teardown"] == 1


def test_issue_workflow_abandons_after_clarification_cap() -> None:
    asyncio.run(_run_issue_workflow_clarification_cap())


async def _run_issue_workflow_clarification_cap() -> None:
    _reset_calls()
    wf_id = "test-issue-workflow-clarification-cap"
    req = _issue_run_request()
    _TRIAGE_RESPONSES.extend(
        _clarifying_triage_output(round_number)
        for round_number in range(1, MAX_CLARIFICATION_ROUNDS + 2)
    )

    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_SUPERVISOR_ACTIVITIES),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_AGENT_ACTIVITIES),
        ):
            handle = await env.client.start_workflow(
                DarkFactoryIssueWorkflow.run,
                req,
                id=wf_id,
                task_queue="supervisor-tq",
            )

            for round_number in range(1, MAX_CLARIFICATION_ROUNDS + 1):
                await _wait_until_posted_comments(round_number)
                assert _POSTED_COMMENTS[-1]["clarification_round"] == round_number
                assert _POSTED_COMMENTS[-1]["mark_needs_human"] is False
                await handle.execute_update(
                    DarkFactoryIssueWorkflow.post_new_comments,
                    [
                        IssueComment(
                            id=700 + round_number,
                            author="octocat",
                            body=f"Reply for round {round_number}.",
                            created_at=f"2026-05-05T10:0{round_number}:00Z",
                        )
                    ],
                )

            result = await handle.result()

    assert result.status == "abandoned"
    assert result.reason == "max_clarification_rounds"
    assert result.state["clarification_rounds"] == MAX_CLARIFICATION_ROUNDS
    assert result.state["abandoned_reason"] == "max_clarification_rounds"
    assert result.state["ready_to_build"] is False
    assert len(result.state["issue_comments"]) == MAX_CLARIFICATION_ROUNDS
    assert _CALLS["triage"] == MAX_CLARIFICATION_ROUNDS + 1
    assert _TRIAGE_COMMENT_COUNTS == [0, 1, 2, 3]
    assert [call["clarification_round"] for call in _POSTED_COMMENTS] == [
        1,
        2,
        3,
        4,
    ]
    assert [call["mark_needs_human"] for call in _POSTED_COMMENTS] == [
        False,
        False,
        False,
        True,
    ]
    assert _CALLS["discovery"] == 0
    assert _CALLS["build"] == 0
    assert _CALLS["verify"] == 0
    assert _CALLS["reviewer"] == 0
    assert _CALLS["pr_creator"] == 0
    assert _CALLS["merge"] == 0
    assert _CALLS["mark_done"] == 0
    assert _CALLS["teardown"] == 1


async def _wait_until_spec_gate_pending(handle) -> dict:
    for _ in range(80):
        summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
        if _CALLS["discovery"] == 1 and summary["gate_pending"] is True:
            return summary
        await asyncio.sleep(0.05)
    raise AssertionError("issue workflow did not reach the spec approval gate")


async def _wait_until_merge_gate_pending(handle) -> dict:
    for _ in range(80):
        summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
        if summary.get("pending_gate") == "merge":
            return summary
        await asyncio.sleep(0.05)
    raise AssertionError("issue workflow did not reach the merge approval gate")


async def _wait_until_posted_comments(count: int) -> None:
    for _ in range(80):
        if len(_POSTED_COMMENTS) >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"issue workflow did not post {count} clarification comments")
