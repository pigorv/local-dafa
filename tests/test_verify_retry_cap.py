"""Verify retry cap lives in the Temporal workflow, not the Studio graph."""
from __future__ import annotations

import asyncio

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from darkfactory.runtime.workflow import DarkFactoryWorkflow, VERIFY_RETRY_CAP
from darkfactory.stages.verify import aggregate
from darkfactory.state import PipelineState, RunRequest

_COUNTERS: dict[str, int] = {
    "setup": 0,
    "build": 0,
    "verify": 0,
    "spec_adjustment": 0,
    "code_quality": 0,
    "teardown": 0,
}


def _reset_counters() -> None:
    for key in _COUNTERS:
        _COUNTERS[key] = 0


def test_aggregate_increments_retries_on_failure():
    state: PipelineState = {
        "test_results": [
            {
                "runner": "maven",
                "returncode": 1,
                "passed": 0,
                "failed": 1,
                "errors": [],
                "duration_s": 0.1,
            }
        ],
        "findings": [],
        "verify_retries": 0,
    }
    delta = aggregate(state)
    assert delta["verify_summary"]["passed"] is False
    assert delta["verify_retries"] == 1


def test_aggregate_does_not_increment_on_pass():
    state: PipelineState = {
        "test_results": [
            {
                "runner": "maven",
                "returncode": 0,
                "passed": 5,
                "failed": 0,
                "errors": [],
                "duration_s": 0.1,
            }
        ],
        "findings": [],
        "verify_retries": 2,
    }
    delta = aggregate(state)
    assert delta["verify_summary"]["passed"] is True
    assert "verify_retries" not in delta


@activity.defn(name="setup_worker_activity")
async def stub_setup_worker_activity(wf_id: str, repo_url: str) -> str:
    _COUNTERS["setup"] += 1
    return f"darkfactory-worker-{wf_id}"


@activity.defn(name="teardown_worker_activity")
async def stub_teardown_worker_activity(wf_id: str) -> None:
    _COUNTERS["teardown"] += 1


@activity.defn(name="hydrate_stage")
async def stub_hydrate_stage(state: dict) -> dict:
    return {"repo_context": {"repo_root": state.get("repo_path", "/workspace")}}


@activity.defn(name="discovery_stage")
async def stub_discovery_stage(state: dict) -> dict:
    return {"stories": [], "spec": [], "review_decision": None}


@activity.defn(name="build_stage")
async def stub_build_stage(state: dict) -> dict:
    _COUNTERS["build"] += 1
    return {"build_order": [], "current_slice": "", "patches": []}


@activity.defn(name="verify_stage")
async def stub_verify_stage(state: dict) -> dict:
    _COUNTERS["verify"] += 1
    return {
        "test_results": [],
        "findings": [],
        "verify_summary": {
            "passed": False,
            "failed_tests": 1,
            "hard_findings": 0,
        },
        "verify_retries": (state.get("verify_retries") or 0) + 1,
    }


@activity.defn(name="spec_adjustment_stage")
async def stub_spec_adjustment_stage(state: dict) -> dict:
    _COUNTERS["spec_adjustment"] += 1
    return {}


@activity.defn(name="code_quality_stage")
async def stub_code_quality_stage(state: dict) -> dict:
    _COUNTERS["code_quality"] += 1
    return {}


@activity.defn(name="pr_creator_stage")
async def stub_pr_creator_stage(state: dict) -> dict:
    return {}


@activity.defn(name="merge_branch")
async def stub_merge_branch(state: dict) -> dict:
    return {}


def test_workflow_exhausts_after_verify_retry_cap():
    asyncio.run(_run_workflow_retry_cap_check())


async def _run_workflow_retry_cap_check() -> None:
    _reset_counters()
    wf_id = "test-wf-retry-cap"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="make verify fail until the cap",
    )

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryWorkflow],
            activities=[
                stub_setup_worker_activity,
                stub_teardown_worker_activity,
            ],
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=[
                stub_hydrate_stage,
                stub_discovery_stage,
                stub_build_stage,
                stub_verify_stage,
                stub_spec_adjustment_stage,
                stub_code_quality_stage,
                stub_pr_creator_stage,
                stub_merge_branch,
            ],
        ):
            result = await env.client.execute_workflow(
                DarkFactoryWorkflow.run,
                req,
                id=wf_id,
                task_queue="supervisor-tq",
            )

    assert result.status == "exhausted_retries"
    assert result.state["verify_summary"]["passed"] is False
    assert result.state["verify_retries"] == VERIFY_RETRY_CAP
    assert _COUNTERS["setup"] == 1
    assert _COUNTERS["build"] == VERIFY_RETRY_CAP
    assert _COUNTERS["verify"] == VERIFY_RETRY_CAP
    assert _COUNTERS["spec_adjustment"] == VERIFY_RETRY_CAP
    assert _COUNTERS["code_quality"] == 0
    assert _COUNTERS["teardown"] == 1
