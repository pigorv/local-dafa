from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.runtime.approval import ApprovalSignal
from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow
from darkfactory.runtime.workflow import DarkFactoryWorkflow, PLANNING_MAX_ATTEMPTS
from darkfactory.state import GateDecision, IssueRef, IssueRunRequest, RunRequest
from tests.temporal_testing import start_time_skipping_env


_CALLS: dict[str, int] = {
    "setup": 0,
    "teardown": 0,
    "hydrate": 0,
    "triage": 0,
    "phase": 0,
    "label": 0,
    "discovery": 0,
    "build": 0,
    "verify": 0,
    "reviewer": 0,
    "pr_creator": 0,
    "merge": 0,
    "mark_done": 0,
}
_DISCOVERY_RESPONSES: list[dict[str, Any]] = []
_DISCOVERY_INPUTS: list[dict[str, Any]] = []
_LABELS: list[dict[str, Any]] = []


def _reset() -> None:
    for key in _CALLS:
        _CALLS[key] = 0
    _DISCOVERY_RESPONSES.clear()
    _DISCOVERY_INPUTS.clear()
    _LABELS.clear()


def _state_value(obj: object, key: str, default: object = None) -> object:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _review_response(*, approved: bool, reason: str = "") -> dict[str, Any]:
    attempt = len(_DISCOVERY_RESPONSES) + 1
    return {
        "stories": [
            {
                "id": f"story-{attempt}",
                "title": f"Planning attempt {attempt}",
                "as_a": "maintainer",
                "i_want": "planning to be reviewed",
                "so_that": "weak briefs do not reach build",
                "acceptance_criteria": ["Plan Critic can request a revision."],
            }
        ],
        "spec": [
            {
                "story_id": f"story-{attempt}",
                "approach": "Revise the implementation brief.",
                "affected_files": [],
                "new_files": [],
                "test_files": [],
                "risks": [],
                "depends_on": [],
            }
        ],
        "review_decision": {
            "approved": approved,
            "reason": reason,
            "edits": {} if approved else {"story-1": {"verification": ["Add coverage."]}},
        },
    }


def _issue_request() -> IssueRunRequest:
    return IssueRunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        issue=IssueRef(
            repo="octo-org/octo-repo",
            number=42,
            url="https://github.com/octo-org/octo-repo/issues/42",
            title="Enforce planning quality",
            body="Retry planning when the critic rejects the brief.",
            labels=["df:ready"],
        ),
    )


@activity.defn(name="setup_worker_activity")
async def setup_worker(wf_id: str, repo_url: str) -> str:  # noqa: ARG001
    _CALLS["setup"] += 1
    return f"darkfactory-worker-{wf_id}"


@activity.defn(name="teardown_worker_activity")
async def teardown_worker(wf_id: str) -> None:  # noqa: ARG001
    _CALLS["teardown"] += 1


@activity.defn(name="hydrate_stage")
async def hydrate_stage(state: dict) -> dict:
    _CALLS["hydrate"] += 1
    return {"repo_context": {"repo_root": state.get("repo_path", "/workspace")}}


@activity.defn(name="triage_stage")
async def triage_stage(state: dict) -> dict:  # noqa: ARG001
    _CALLS["triage"] += 1
    return {
        "ready_to_build": True,
        "clarification_questions": [],
        "derived_user_request": "Build after planning approval.",
        "confidence": "high",
        "rationale": "The issue has enough detail.",
    }


@activity.defn(name="upsert_phase_comment_activity")
async def upsert_phase(
    issue: Any,  # noqa: ARG001
    marker: str,  # noqa: ARG001
    body: str,  # noqa: ARG001
    task_id: str | None = None,  # noqa: ARG001
    repo_path: str = "/workspace",  # noqa: ARG001
) -> int:
    _CALLS["phase"] += 1
    return _CALLS["phase"]


@activity.defn(name="swap_state_label_activity")
async def swap_label(
    issue: Any,
    remove: str | list[str] | None,
    add: str | list[str] | None,
    task_id: str | None = None,  # noqa: ARG001
    repo_path: str = "/workspace",  # noqa: ARG001
) -> dict[str, Any]:
    _CALLS["label"] += 1
    assert _state_value(issue, "number") == 42
    _LABELS.append({"remove": remove, "add": add})
    return {"labels_removed": [], "labels_added": []}


@activity.defn(name="post_issue_comment_activity")
async def post_issue_comment(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"issue_comment_posted": True}


@activity.defn(name="discovery_stage")
async def discovery_stage(state: dict) -> dict:
    _CALLS["discovery"] += 1
    _DISCOVERY_INPUTS.append(
        {
            "planning_attempts": state.get("planning_attempts"),
            "planning_feedback": list(state.get("planning_feedback") or []),
            "planning_attempt_log": list(state.get("planning_attempt_log") or []),
            "latest_spec_rev": state.get("latest_spec_rev"),
            "stories": list(state.get("stories") or []),
            "spec": list(state.get("spec") or []),
        }
    )
    if _DISCOVERY_RESPONSES:
        return _DISCOVERY_RESPONSES.pop(0)
    return _review_response(approved=True)


@activity.defn(name="build_stage")
async def build_stage(state: dict) -> dict:  # noqa: ARG001
    _CALLS["build"] += 1
    return {"build_order": ["story-1"], "current_slice": "story-1", "patches": []}


@activity.defn(name="verify_stage")
async def verify_stage(state: dict) -> dict:  # noqa: ARG001
    _CALLS["verify"] += 1
    return {
        "test_results": [],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
    }


@activity.defn(name="fixer_stage")
async def fixer_stage(state: dict) -> dict:  # noqa: ARG001
    return {}


@activity.defn(name="reviewer_stage")
async def reviewer_stage(state: dict) -> dict:  # noqa: ARG001
    _CALLS["reviewer"] += 1
    return {
        "review_decision": {
            "severity": "low",
            "issues": [],
            "recommendation": "approve",
        }
    }


@activity.defn(name="pr_creator_stage")
async def pr_creator_stage(state: dict) -> dict:  # noqa: ARG001
    _CALLS["pr_creator"] += 1
    return {"pr_url": "https://github.example/octo-org/octo-repo/pull/7"}


@activity.defn(name="merge_branch")
async def merge_branch(state: dict) -> dict:  # noqa: ARG001
    _CALLS["merge"] += 1
    return {"merged": True}


@activity.defn(name="mark_issue_done_activity")
async def mark_done(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    _CALLS["mark_done"] += 1
    return {"done_label_added": True}


_SUPERVISOR_ACTIVITIES = (setup_worker, teardown_worker)
_AGENT_ACTIVITIES = (
    hydrate_stage,
    triage_stage,
    upsert_phase,
    swap_label,
    post_issue_comment,
    discovery_stage,
    build_stage,
    verify_stage,
    fixer_stage,
    reviewer_stage,
    pr_creator_stage,
    merge_branch,
    mark_done,
)


def test_manual_workflow_replans_with_critic_feedback_then_builds() -> None:
    asyncio.run(_run_manual_replan_success())


async def _run_manual_replan_success() -> None:
    _reset()
    _DISCOVERY_RESPONSES.extend(
        [
            _review_response(
                approved=False,
                reason="Verification predicates are too vague.",
            ),
            _review_response(approved=True),
        ]
    )
    wf_id = "test-planning-loop-manual-replan"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="retry planning before build",
    )

    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryWorkflow],
            activities=list(_SUPERVISOR_ACTIVITIES),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_AGENT_ACTIVITIES),
        ):
            handle = await env.client.start_workflow(
                DarkFactoryWorkflow.run,
                req,
                id=wf_id,
                task_queue="supervisor-tq",
            )
            await _wait_for_manual_gate(handle, "brief")

            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="brief approved"),
            )
            await _wait_for_manual_gate(handle, "merge")
            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="merge approved"),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert _CALLS["discovery"] == 2
    assert _CALLS["build"] == 1
    assert _DISCOVERY_INPUTS[0]["planning_attempts"] == 1
    assert _DISCOVERY_INPUTS[0]["planning_feedback"] == []
    assert _DISCOVERY_INPUTS[1]["planning_attempts"] == 2
    assert "Verification predicates are too vague." in _DISCOVERY_INPUTS[1][
        "planning_feedback"
    ][0]
    assert _DISCOVERY_INPUTS[1]["stories"] == []
    assert _DISCOVERY_INPUTS[1]["spec"] == []
    assert result.state["planning_attempts"] == 2
    assert len(result.state["planning_feedback"]) == 1


def test_issue_workflow_planning_budget_exhaustion_needs_human() -> None:
    asyncio.run(_run_issue_planning_budget_exhaustion())


async def _run_issue_planning_budget_exhaustion() -> None:
    _reset()
    _DISCOVERY_RESPONSES.extend(
        [
            _review_response(approved=False, reason=f"Critic reject {attempt}.")
            for attempt in range(1, PLANNING_MAX_ATTEMPTS + 1)
        ]
    )
    wf_id = "test-planning-loop-issue-budget"

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
            result = await env.client.execute_workflow(
                DarkFactoryIssueWorkflow.run,
                _issue_request(),
                id=wf_id,
                task_queue="supervisor-tq",
            )

    assert result.status == "needs_human"
    assert result.reason == "planning_retry_cap"
    assert _CALLS["discovery"] == PLANNING_MAX_ATTEMPTS
    assert _CALLS["build"] == 0
    assert _DISCOVERY_INPUTS[0]["planning_feedback"] == []
    assert len(_DISCOVERY_INPUTS[1]["planning_feedback"]) == 1
    assert len(_DISCOVERY_INPUTS[2]["planning_feedback"]) == 2
    assert result.state["planning_attempts"] == PLANNING_MAX_ATTEMPTS
    assert len(result.state["planning_feedback"]) == PLANNING_MAX_ATTEMPTS
    assert {
        item["source"]
        for item in result.state["planning_attempt_log"]
    } == {"plan_critic_reject"}
    assert _LABELS[-1] == {"remove": "df:designing", "add": "df:needs-human"}


def test_issue_workflow_human_revise_replans_with_human_feedback() -> None:
    asyncio.run(_run_issue_human_revise_replans())


async def _run_issue_human_revise_replans() -> None:
    _reset()
    _DISCOVERY_RESPONSES.extend(
        [
            _review_response(approved=True),
            _review_response(approved=True),
        ]
    )
    wf_id = "test-planning-loop-issue-human-revise"

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
                _issue_request(),
                id=wf_id,
                task_queue="supervisor-tq",
            )
            await _wait_for_issue_gate(handle, latest_spec_rev=1)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.signal_approval,
                ApprovalSignal(
                    kind="Revise",
                    author="octocat",
                    comment_id=501,
                    text="Add the export edge case.",
                ),
            )
            await _wait_for_issue_gate(handle, latest_spec_rev=2)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.signal_approval,
                ApprovalSignal(kind="Approve", author="octocat", comment_id=502),
            )
            await _wait_for_issue_merge_gate(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.signal_approval,
                ApprovalSignal(kind="Approve", author="octocat", comment_id=503),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert _CALLS["discovery"] == 2
    assert _DISCOVERY_INPUTS[0]["planning_attempts"] == 1
    assert _DISCOVERY_INPUTS[0]["planning_feedback"] == []
    assert _DISCOVERY_INPUTS[1]["latest_spec_rev"] == 2
    assert _DISCOVERY_INPUTS[1]["planning_attempts"] == 1
    assert _DISCOVERY_INPUTS[1]["planning_feedback"] == [
        "Human revise by @octocat: Add the export edge case."
    ]
    assert _DISCOVERY_INPUTS[1]["stories"] == []
    assert _DISCOVERY_INPUTS[1]["spec"] == []
    assert result.state["planning_attempts"] == 1
    assert result.state["planning_feedback"] == [
        "Human revise by @octocat: Add the export edge case."
    ]
    assert result.state["planning_attempt_log"] == [
        {
            "source": "human_revise",
            "attempt": 1,
            "feedback": "Human revise by @octocat: Add the export edge case.",
            "rev": 1,
            "next_rev": 2,
            "author": "octocat",
            "comment_id": 501,
        }
    ]


async def _wait_for_issue_gate(handle, *, latest_spec_rev: int) -> None:
    for _ in range(80):
        summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
        if (
            summary.get("pending_gate") == "design"
            and summary["latest_spec_rev"] == latest_spec_rev
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow did not reach spec approval gate rev {latest_spec_rev}")


async def _wait_for_issue_merge_gate(handle) -> None:
    for _ in range(160):
        summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
        if summary.get("pending_gate") == "merge":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("workflow did not reach merge approval gate")


async def _wait_for_manual_gate(handle, gate: str) -> None:
    for _ in range(80):
        summary = await handle.query(DarkFactoryWorkflow.current_state_summary)
        if summary["pending_gate"] == gate and summary["gate_pending"]:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow did not reach the {gate} human gate")
