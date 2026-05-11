"""M1-5 + M2-14: setup_worker_activity is idempotent across re-runs of the same wf_id.

Drives `DarkFactoryWorkflow` twice under one workflow id (force-replay
scenario) inside Temporal's time-skipping test harness and asserts the
orchestrator's `setup_worker_activity` reuses an existing worker container
rather than spawning a duplicate. `docker.from_env` is mocked and every
agent-tq stage activity is replaced with a hermetic stub so the test runs
without Docker, the Anthropic API, or live LangGraph subgraphs.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from docker.errors import NotFound
from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.runtime.activities import (
    setup_worker_activity,
    teardown_worker_activity,
    with_repo_state,
)
from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import GateDecision, RunRequest
from tests.temporal_testing import start_time_skipping_env


def _build_fake_docker_client() -> tuple[MagicMock, dict[str, int]]:
    alive: set[str] = set()
    containers: dict[str, MagicMock] = {}
    branches: dict[str, set[str]] = {}
    counters = {
        "spawn": 0,
        "lookup": 0,
        "remove": 0,
        "branch_checkout": 0,
        "branch_create": 0,
    }

    def _container(name: str) -> MagicMock:
        if name in containers:
            return containers[name]

        branches[name] = {"main"}

        def fake_exec_run(argv, **_kwargs):
            cmd = list(argv)
            if cmd[:2] == ["git", "checkout"] and len(cmd) == 3:
                counters["branch_checkout"] += 1
                branch = cmd[2]
                if branch in branches[name]:
                    return (0, b"")
                return (1, b"missing branch")
            if cmd[:3] == ["git", "checkout", "-b"] and len(cmd) == 4:
                counters["branch_create"] += 1
                branch = cmd[3]
                branches[name].add(branch)
                return (0, b"")
            return (0, b"")

        container = MagicMock()
        container.exec_run.side_effect = fake_exec_run
        container.remove = MagicMock(
            side_effect=lambda *_a, **_kw: counters.__setitem__(
                "remove", counters["remove"] + 1
            )
        )
        containers[name] = container
        return container

    def fake_run(**kwargs):
        # The orchestrator now spawns a throwaway alpine init container before
        # each worker to pre-create the transcripts-volume subpath. Those calls
        # have no `name=` and don't represent worker spawns.
        name = kwargs.get("name")
        if name is None:
            return MagicMock()
        counters["spawn"] += 1
        alive.add(name)
        return _container(name)

    def fake_get(name):
        counters["lookup"] += 1
        if name in alive:
            # remove() is a no-op so the container persists across the
            # first run's teardown, modeling a leaked container that the
            # second run's setup must reuse instead of double-spawning.
            return _container(name)
        raise NotFound(f"no such container {name}")

    fake_client = MagicMock()
    fake_client.containers.run.side_effect = fake_run
    fake_client.containers.get.side_effect = fake_get
    return fake_client, counters


@activity.defn(name="hydrate_stage")
async def stub_hydrate_stage(state: dict) -> dict:
    return {"repo_context": {"repo_root": state.get("repo_path", "/workspace")}}


@activity.defn(name="discovery_stage")
async def stub_discovery_stage(state: dict) -> dict:
    return {"stories": [], "spec": [], "review_decision": None}


@activity.defn(name="build_stage")
async def stub_build_stage(state: dict) -> dict:
    return {"build_order": [], "current_slice": "", "patches": []}


@activity.defn(name="verify_stage")
async def stub_verify_stage(state: dict) -> dict:
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
    return {}


@activity.defn(name="pr_creator_stage")
async def stub_pr_creator_stage(state: dict) -> dict:
    return {}


@activity.defn(name="merge_branch")
async def stub_merge_branch(state: dict) -> dict:
    return {}


_AGENT_STAGE_STUBS = (
    stub_hydrate_stage,
    stub_discovery_stage,
    stub_build_stage,
    stub_verify_stage,
    stub_fixer_stage,
    stub_reviewer_stage,
    stub_pr_creator_stage,
    stub_merge_branch,
)


def test_setup_worker_activity_is_idempotent_across_runs():
    asyncio.run(_run_idempotency_check())


async def _run_idempotency_check() -> None:
    wf_id = "test-wf-idempotent"
    fake_client, counters = _build_fake_docker_client()

    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="ping-hello",
    )

    with patch(
        "darkfactory.runtime.activities.docker.from_env",
        return_value=fake_client,
    ):
        async with await start_time_skipping_env(
            data_converter=pydantic_data_converter
        ) as env:
            agent_tq = f"agent-tq-{wf_id}"
            async with Worker(
                env.client,
                task_queue="supervisor-tq",
                workflows=[DarkFactoryWorkflow],
                activities=[setup_worker_activity, teardown_worker_activity],
            ), Worker(
                env.client,
                task_queue=agent_tq,
                activities=list(_AGENT_STAGE_STUBS),
            ):
                first = await _run_and_approve(env.client, req, wf_id)
                second = await _run_and_approve(
                    env.client,
                    req,
                    wf_id,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                )

    assert first.status == "merged", f"expected merged, got {first.status}"
    assert second.status == "merged", f"expected merged, got {second.status}"
    assert counters["spawn"] == 1, (
        f"expected one container spawn across both runs, got {counters['spawn']}"
    )
    assert counters["branch_checkout"] == 2, (
        "expected setup to checkout the workflow branch on both initial run and retry"
    )
    assert counters["branch_create"] == 1, (
        "expected setup to create the workflow branch only once"
    )


def test_with_repo_state_reuses_existing_branch_on_activity_retry():
    asyncio.run(_run_branch_reuse_check())


async def _run_branch_reuse_check() -> None:
    calls: list[list[str]] = []

    class FakeSandbox:
        def __init__(self):
            self.branches = {"main"}

        def exec(self, argv, timeout=120):  # noqa: ARG002 — match RepoSandbox.exec
            calls.append(list(argv))
            if argv[:2] == ["git", "checkout"] and len(argv) == 3:
                if argv[2] in self.branches:
                    return {"returncode": 0, "stdout": "", "stderr": ""}
                return {"returncode": 1, "stdout": "", "stderr": "missing"}
            if argv[:3] == ["git", "checkout", "-b"] and len(argv) == 4:
                self.branches.add(argv[3])
                return {"returncode": 0, "stdout": "", "stderr": ""}
            return {"returncode": 0, "stdout": "", "stderr": ""}

    sandbox = FakeSandbox()
    state = {
        "wf_id": "wf-branch-retry",
        "task_id": "wf-branch-retry",
        "repo_path": "/workspace",
        "feature_branch": "agent/wf-branch-retry",
    }

    @with_repo_state("agent/{wf_id}")
    async def repo_activity(state):
        return {"seen_branch": state["feature_branch"]}

    with patch(
        "darkfactory.runtime.activities.get_sandbox",
        return_value=sandbox,
    ), patch("darkfactory.runtime.activities.register_sandbox") as register:
        first = await repo_activity(state)
        second = await repo_activity(state)

    assert first == {"seen_branch": "agent/wf-branch-retry"}
    assert second == {"seen_branch": "agent/wf-branch-retry"}
    assert register.call_count == 0
    assert calls == [
        ["git", "checkout", "agent/wf-branch-retry"],
        ["git", "checkout", "-b", "agent/wf-branch-retry"],
        ["git", "checkout", "agent/wf-branch-retry"],
    ]


async def _run_and_approve(client, req: RunRequest, wf_id: str, **kwargs):
    handle = await client.start_workflow(
        DarkFactoryWorkflow.run,
        req,
        id=wf_id,
        task_queue="supervisor-tq",
        **kwargs,
    )
    await handle.execute_update(
        DarkFactoryWorkflow.approve_gate,
        GateDecision(approved=True, reason="test approves brief"),
    )
    await handle.execute_update(
        DarkFactoryWorkflow.approve_gate,
        GateDecision(approved=True, reason="test approves merge"),
    )
    return await handle.result()
