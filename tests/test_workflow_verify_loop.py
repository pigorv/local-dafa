"""Regression test: verify loop must hand each cycle a clean mechanical slate.

Without the per-cycle reset, `test_results` and `findings` accumulate across
verify cycles (they use the `add` reducer in PipelineState). A single failure
in cycle N then keeps `_aggregate_verify_summary`'s `failed_tests` count > 0
forever, so `summary["passed"]` stays False even when the latest cycle's
tests pass — and the fixer keeps running against a stale verdict. This test
exercises that exact scenario through the real Temporal workflow.
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
    "fixer": 0,
    "reviewer": 0,
    "pr_creator": 0,
    "merge": 0,
    "teardown": 0,
}

# Snapshot of what each verify call saw on its `state["test_results"]` input.
# Lets the assertions distinguish "workflow cleared the channel" from
# "workflow handed the previous cycle's results back to verify".
_VERIFY_INPUT_TEST_RESULTS: list[list[dict]] = []


def _reset_calls() -> None:
    for key in _CALLS:
        _CALLS[key] = 0
    _VERIFY_INPUT_TEST_RESULTS.clear()


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
    """Production-shaped verify stub.

    Reads whatever `test_results` the workflow handed in, appends this
    cycle's fresh result, then aggregates pass/fail across the combined
    list — exactly what `_aggregate_verify_summary` does in production.
    On cycle 1 the fresh result is a failure; on cycle 2+ it is a pass.

    With the fix in place, each cycle sees an empty incoming list, so
    cycle 2's combined view is `[pass2]` → passed=True → loop exits.
    Without the fix, cycle 2 sees `[fail1]` incoming, combines to
    `[fail1, pass2]` → passed=False → fixer runs again.
    """
    _CALLS["verify"] += 1
    _VERIFY_INPUT_TEST_RESULTS.append(list(state.get("test_results") or []))

    if _CALLS["verify"] == 1:
        fresh = {
            "runner": "maven",
            "returncode": 1,
            "passed": 0,
            "failed": 1,
            "errors": [],
            "duration_s": 0.1,
        }
    else:
        fresh = {
            "runner": "maven",
            "returncode": 0,
            "passed": 1,
            "failed": 0,
            "errors": [],
            "duration_s": 0.1,
        }

    combined = list(state.get("test_results") or []) + [fresh]
    failed_tests = sum(
        1
        for r in combined
        if r.get("failed", 0) > 0
        or r.get("returncode", 0) != 0
        or bool(r.get("errors"))
    )
    return {
        "test_results": [fresh],
        "findings": [],
        "verify_summary": {
            "passed": failed_tests == 0,
            "failed_tests": failed_tests,
            "hard_findings": 0,
        },
    }


@activity.defn(name="fixer_stage")
async def stub_fixer_stage(state: dict) -> dict:
    _CALLS["fixer"] += 1
    return {"fixer_decision": {"decision": "fixed", "target_wp": "slice-1"}}


@activity.defn(name="reviewer_stage")
async def stub_reviewer_stage(state: dict) -> dict:
    _CALLS["reviewer"] += 1
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
    return {"pr_url": "https://github.example/acme/repo/pull/1"}


@activity.defn(name="merge_branch")
async def stub_merge_branch(state: dict) -> dict:
    _CALLS["merge"] += 1
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


def test_verify_loop_resets_mechanical_state_between_cycles() -> None:
    asyncio.run(_run_verify_loop_reset_check())


async def _run_verify_loop_reset_check() -> None:
    _reset_calls()
    wf_id = "test-wf-verify-loop-reset"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="verify cycle 1 fails, cycle 2 passes — workflow should exit",
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

            await _wait_until_gate_pending(handle, "brief")
            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="brief approved"),
            )

            await _wait_until_gate_pending(handle, "merge")
            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="merge approved"),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert result.state["verify_summary"]["passed"] is True

    # The whole point of the fix: cycle 2 must succeed, so verify is called
    # exactly twice and fixer exactly once. Pre-fix this would tick up to the
    # FIXER_MAX_ATTEMPTS cap because cycle 2 would inherit cycle 1's failure
    # and re-fail.
    assert _CALLS["verify"] == 2, (
        f"verify should run exactly 2x (1 fail + 1 pass), saw {_CALLS['verify']}"
    )
    assert _CALLS["fixer"] == 1, (
        f"fixer should run exactly once between the two verify cycles, "
        f"saw {_CALLS['fixer']}"
    )

    # Each verify invocation must have been handed a clean test_results list.
    # Pre-fix, the second entry would be `[fail1_dict]`, not `[]`.
    assert _VERIFY_INPUT_TEST_RESULTS == [[], []], (
        f"verify must see an empty test_results on every cycle, "
        f"saw {_VERIFY_INPUT_TEST_RESULTS}"
    )

    assert _CALLS["pr_creator"] == 1
    assert _CALLS["reviewer"] == 1
    assert _CALLS["merge"] == 1


async def _wait_until_gate_pending(handle, gate: str) -> dict:
    for _ in range(80):
        summary = await handle.query(DarkFactoryWorkflow.current_state_summary)
        if summary["pending_gate"] == gate and summary["gate_pending"] is True:
            return summary
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow did not reach the {gate} HITL gate")
