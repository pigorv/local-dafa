"""M4-6: workflow durability across a worker restart at the HITL gate.

This exercises R13 hermetically with Temporal's test harness. The workflow
advances to the durable gate, both test workers are stopped to model a crash,
new workers are started on the same task queues, and the approval update then
drives merge of the already reviewed PR. Activity stubs keep the test
independent of Docker, GitHub, the Anthropic API, and real stage subgraphs.
"""
from __future__ import annotations

import asyncio

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import GateDecision, RunRequest
from tests.temporal_testing import start_time_skipping_env


_CALLS: dict[str, int] = {
    "setup": 0,
    "hydrate": 0,
    "discovery": 0,
    "build": 0,
    "verify": 0,
    "reviewer": 0,
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
    return {"build_order": [], "current_slice": "slice-durable", "patches": []}


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
    return {}


@activity.defn(name="reviewer_stage")
async def stub_reviewer_stage(state: dict) -> dict:
    _CALLS["reviewer"] += 1
    assert state["verify_summary"]["passed"] is True
    assert state["pr_url"] == "https://github.example/acme/repo/pull/13"
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
    return {"pr_url": "https://github.example/acme/repo/pull/13"}


@activity.defn(name="merge_branch")
async def stub_merge_branch(state: dict) -> dict:
    _CALLS["merge"] += 1
    assert state["gate_approved"] is True
    assert state["pr_url"] == "https://github.example/acme/repo/pull/13"
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
    stub_fixer_stage,
    stub_reviewer_stage,
    stub_pr_creator_stage,
    stub_merge_branch,
)


def test_workflow_resumes_after_worker_restart_at_gate() -> None:
    asyncio.run(_run_durability_check())


async def _run_durability_check() -> None:
    _reset_calls()
    wf_id = "test-wf-durability"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="prove the gate survives a worker restart",
    )

    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        first_workers = await _start_workers(env.client, wf_id)
        try:
            handle = await env.client.start_workflow(
                DarkFactoryWorkflow.run,
                req,
                id=wf_id,
                task_queue="supervisor-tq",
            )

            summary = await _wait_until_gate_pending(handle, "brief")
            assert summary["gate_pending"] is True
            assert summary["brief_gate_pending"] is True
            assert summary["gate_approved"] is False
            assert _CALLS["build"] == 0
            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="brief approved before restart"),
            )

            summary = await _wait_until_gate_pending(handle, "merge")
            assert summary["gate_pending"] is True
            assert summary["merge_gate_pending"] is True
            assert summary["brief_gate_approved"] is True
            assert summary["gate_approved"] is False
            assert _CALLS["reviewer"] == 1
            assert _CALLS["pr_creator"] == 1
            assert _CALLS["merge"] == 0
        finally:
            await _stop_workers(*first_workers)

        assert _CALLS == {
            "setup": 1,
            "hydrate": 1,
            "discovery": 1,
            "build": 1,
            "verify": 1,
            "reviewer": 1,
            "pr_creator": 1,
            "merge": 0,
            "teardown": 0,
        }

        restarted_workers = await _start_workers(env.client, wf_id)
        try:
            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="approved after restart"),
            )
            result = await handle.result()
        finally:
            await _stop_workers(*restarted_workers)

    assert result.status == "merged"
    assert result.reason is None
    assert result.state["brief_gate_approved"] is True
    assert result.state["merge_gate_approved"] is True
    assert result.state["gate_approved"] is True
    assert result.state["pr_url"] == "https://github.example/acme/repo/pull/13"
    assert result.state["merged"] is True
    assert _CALLS == {
        "setup": 1,
        "hydrate": 1,
        "discovery": 1,
        "build": 1,
        "verify": 1,
        "reviewer": 1,
        "pr_creator": 1,
        "merge": 1,
        "teardown": 1,
    }


async def _start_workers(client, wf_id: str) -> tuple[Worker, Worker]:
    supervisor = Worker(
        client,
        task_queue="supervisor-tq",
        workflows=[DarkFactoryWorkflow],
        activities=list(_SUPERVISOR_ACTIVITIES),
        max_cached_workflows=0,
    )
    agent = Worker(
        client,
        task_queue=f"agent-tq-{wf_id}",
        activities=list(_AGENT_ACTIVITIES),
    )
    await supervisor.__aenter__()
    await agent.__aenter__()
    return supervisor, agent


async def _stop_workers(*workers: Worker) -> None:
    for worker in reversed(workers):
        await worker.__aexit__(None, None, None)


async def _wait_until_gate_pending(handle, gate: str) -> dict:
    for _ in range(80):
        summary = await handle.query(DarkFactoryWorkflow.current_state_summary)
        if summary["pending_gate"] == gate and summary["gate_pending"] is True:
            return summary
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow did not reach the durable {gate} HITL gate")
