"""M4-4: HITL gate behavior in the Temporal workflow.

The real acceptance path uses Temporal Web UI updates and GitHub. These tests
exercise the same durable workflow update boundary hermetically: stage
activities are stubs, the workflow pauses after code quality, and approval or
rejection decides whether PR creation and merge activities run.
"""
from __future__ import annotations

import asyncio

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import GateDecision, RunRequest


_CALLS: dict[str, int] = {
    "setup": 0,
    "hydrate": 0,
    "discovery": 0,
    "build": 0,
    "verify": 0,
    "code_quality": 0,
    "pr_creator": 0,
    "merge": 0,
    "teardown": 0,
}


def _reset_calls() -> None:
    for key in _CALLS:
        _CALLS[key] = 0


@activity.defn(name="setup_worker_activity")
async def stub_setup_worker_activity(wf_id: str, repo_url: str) -> str:
    _CALLS["setup"] += 1
    return f"darkfactory-worker-{wf_id}"


@activity.defn(name="teardown_worker_activity")
async def stub_teardown_worker_activity(wf_id: str) -> None:
    _CALLS["teardown"] += 1


@activity.defn(name="hydrate_stage")
async def stub_hydrate_stage(state: dict) -> dict:
    _CALLS["hydrate"] += 1
    return {"repo_context": {"repo_root": state.get("repo_path", "/workspace")}}


@activity.defn(name="discovery_stage")
async def stub_discovery_stage(state: dict) -> dict:
    _CALLS["discovery"] += 1
    return {"stories": [], "spec": [], "review_decision": None}


@activity.defn(name="build_stage")
async def stub_build_stage(state: dict) -> dict:
    _CALLS["build"] += 1
    return {"build_order": [], "current_slice": "slice-1", "patches": []}


@activity.defn(name="verify_stage")
async def stub_verify_stage(state: dict) -> dict:
    _CALLS["verify"] += 1
    return {
        "test_results": [],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
    }


@activity.defn(name="spec_adjustment_stage")
async def stub_spec_adjustment_stage(state: dict) -> dict:
    return {}


@activity.defn(name="code_quality_stage")
async def stub_code_quality_stage(state: dict) -> dict:
    _CALLS["code_quality"] += 1
    assert state["verify_summary"]["passed"] is True
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
    return {"pr_url": "https://github.example/acme/repo/pull/1"}


@activity.defn(name="merge_branch")
async def stub_merge_branch(state: dict) -> dict:
    _CALLS["merge"] += 1
    assert state["gate_approved"] is True
    assert state["pr_url"] == "https://github.example/acme/repo/pull/1"
    return {"merged": True}


_SUPERVISOR_ACTIVITIES = (
    stub_setup_worker_activity,
    stub_teardown_worker_activity,
)

_AGENT_ACTIVITIES = (
    stub_hydrate_stage,
    stub_discovery_stage,
    stub_build_stage,
    stub_verify_stage,
    stub_spec_adjustment_stage,
    stub_code_quality_stage,
    stub_pr_creator_stage,
    stub_merge_branch,
)


def test_workflow_waits_at_gate_then_merges_on_approval() -> None:
    asyncio.run(_run_gate_decision_check(approved=True))


def test_workflow_rejection_skips_pr_creation_and_merge() -> None:
    asyncio.run(_run_gate_decision_check(approved=False))


async def _run_gate_decision_check(*, approved: bool) -> None:
    _reset_calls()
    wf_id = f"test-wf-gate-{'approve' if approved else 'reject'}"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="ship only after human approval",
    )

    async with await WorkflowEnvironment.start_time_skipping(
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

            summary = await _wait_until_gate_pending(handle)
            assert summary["gate_pending"] is True
            assert summary["gate_approved"] is False
            assert _CALLS["code_quality"] == 1
            assert _CALLS["pr_creator"] == 0
            assert _CALLS["merge"] == 0

            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=approved, reason="human decision"),
            )
            result = await handle.result()

    if approved:
        assert result.status == "merged"
        assert result.state["gate_approved"] is True
        assert result.state["pr_url"] == "https://github.example/acme/repo/pull/1"
        assert result.state["merged"] is True
        assert result.reason is None
        assert _CALLS["pr_creator"] == 1
        assert _CALLS["merge"] == 1
    else:
        assert result.status == "rejected"
        assert result.reason == "human decision"
        assert result.state["gate_approved"] is False
        assert "pr_url" not in result.state
        assert "merged" not in result.state
        assert _CALLS["pr_creator"] == 0
        assert _CALLS["merge"] == 0

    assert _CALLS["setup"] == 1
    assert _CALLS["hydrate"] == 1
    assert _CALLS["discovery"] == 1
    assert _CALLS["build"] == 1
    assert _CALLS["verify"] == 1
    assert _CALLS["teardown"] == 1


async def _wait_until_gate_pending(handle) -> dict:
    for _ in range(80):
        summary = await handle.query(DarkFactoryWorkflow.current_state_summary)
        if _CALLS["code_quality"] == 1 and summary["gate_pending"] is True:
            return summary
        await asyncio.sleep(0.05)
    raise AssertionError("workflow did not reach the HITL gate")
