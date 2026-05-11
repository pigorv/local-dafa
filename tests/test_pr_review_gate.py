from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.runtime.approval import ApprovalSignal
from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow
from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import GateDecision, IssueRef, IssueRunRequest, RunRequest
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
    assert state["brief_gate_approved"] is True
    assert state["gate_approved"] is False
    return {"build_order": [], "current_slice": "slice-1", "patches": []}


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
    assert state["pr_url"] == "https://github.example/acme/repo/pull/6"
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
    assert state["gate_approved"] is False
    return {"pr_url": "https://github.example/acme/repo/pull/6"}


@activity.defn(name="merge_branch")
async def stub_merge_branch(state: dict) -> dict:
    _CALLS["merge"] += 1
    assert state["gate_approved"] is True
    assert state["merge_gate_approved"] is True
    assert state["pr_url"] == "https://github.example/acme/repo/pull/6"
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


def test_manual_workflow_waits_at_brief_and_merge_gates() -> None:
    asyncio.run(_run_two_gate_check())


async def _run_two_gate_check() -> None:
    _reset_calls()
    wf_id = "test-pr-review-gate-manual"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="wait at both manual gates",
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

            summary = await _wait_until_pending_gate(handle, "brief")
            assert summary["brief_gate_pending"] is True
            assert summary["merge_gate_pending"] is False
            assert _CALLS["discovery"] == 1
            assert _CALLS["build"] == 0
            assert _CALLS["pr_creator"] == 0

            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="brief approved"),
            )

            summary = await _wait_until_pending_gate(handle, "merge")
            assert summary["brief_gate_approved"] is True
            assert summary["merge_gate_pending"] is True
            assert summary["gate_approved"] is False
            assert summary["pr_url"] == "https://github.example/acme/repo/pull/6"
            assert _CALLS["build"] == 1
            assert _CALLS["verify"] == 1
            assert _CALLS["pr_creator"] == 1
            assert _CALLS["reviewer"] == 1
            assert _CALLS["merge"] == 0

            await handle.execute_update(
                DarkFactoryWorkflow.approve_gate,
                GateDecision(approved=True, reason="merge approved"),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert result.reason is None
    assert result.state["brief_gate_approved"] is True
    assert result.state["merge_gate_approved"] is True
    assert result.state["gate_approved"] is True
    assert result.state["merged"] is True
    assert _CALLS["merge"] == 1
    assert _CALLS["teardown"] == 1


async def _wait_until_pending_gate(handle, gate: str) -> dict:
    for _ in range(80):
        summary = await handle.query(DarkFactoryWorkflow.current_state_summary)
        if summary["pending_gate"] == gate and summary["gate_pending"] is True:
            return summary
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow did not reach the {gate} gate")


# ---------------------------------------------------------------------------
# Issue workflow merge gate tests (Task 6.3).
# ---------------------------------------------------------------------------


_ISSUE_CALLS: dict[str, int] = {
    "build": 0,
    "verify": 0,
    "fixer": 0,
    "reviewer": 0,
    "merge": 0,
    "quarantine": 0,
    "mark_done": 0,
}


def _reset_issue_calls() -> None:
    for key in _ISSUE_CALLS:
        _ISSUE_CALLS[key] = 0


def _issue_run_request() -> IssueRunRequest:
    return IssueRunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        issue=IssueRef(
            repo="octo-org/octo-repo",
            number=99,
            url="https://github.com/octo-org/octo-repo/issues/99",
            title="Issue merge gate",
            body="Drive Dark Factory through the merge gate.",
            labels=["df:ready"],
        ),
    )


@activity.defn(name="setup_worker_activity")
async def issue_setup_worker(wf_id: str, repo_url: str) -> str:  # noqa: ARG001
    return f"worker-{wf_id}"


@activity.defn(name="teardown_worker_activity")
async def issue_teardown_worker(wf_id: str) -> None:  # noqa: ARG001
    return None


@activity.defn(name="hydrate_stage")
async def issue_hydrate(state: dict) -> dict:
    return {"repo_context": {"repo_root": state.get("repo_path")}}


@activity.defn(name="triage_stage")
async def issue_triage(state: dict) -> dict:  # noqa: ARG001
    return {
        "ready_to_build": True,
        "clarification_questions": [],
        "derived_user_request": "Drive merge gate",
        "confidence": "high",
        "rationale": "Detail is sufficient.",
    }


@activity.defn(name="upsert_phase_comment_activity")
async def issue_upsert_phase(*_args: Any, **_kwargs: Any) -> int:
    return 1


@activity.defn(name="swap_state_label_activity")
async def issue_swap_label(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"labels_removed": [], "labels_added": []}


@activity.defn(name="post_issue_comment_activity")
async def issue_post_comment(*_args: Any, **_kwargs: Any) -> dict:
    return {"issue_comment_posted": True}


@activity.defn(name="discovery_stage")
async def issue_discovery(state: dict) -> dict:  # noqa: ARG001
    return {
        "stories": [{"id": "story-1", "title": "Drive merge gate"}],
        "spec": [
            {
                "story_id": "story-1",
                "approach": "Drive merge gate",
                "verification": ["merge gate routes correctly"],
            }
        ],
        "review_decision": None,
    }


@activity.defn(name="build_stage")
async def issue_build(state: dict) -> dict:  # noqa: ARG001
    _ISSUE_CALLS["build"] += 1
    return {
        "build_order": ["story-1"],
        "current_slice": "story-1",
        "patches": [
            {
                "path": "src/demo.py",
                "diff": "",
                "author_agent": "builder",
                "slice_id": "story-1",
            }
        ],
    }


@activity.defn(name="verify_stage")
async def issue_verify(state: dict) -> dict:  # noqa: ARG001
    _ISSUE_CALLS["verify"] += 1
    return {
        "test_results": [],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
    }


@activity.defn(name="fixer_stage")
async def issue_fixer(state: dict) -> dict:  # noqa: ARG001
    _ISSUE_CALLS["fixer"] += 1
    return {}


@activity.defn(name="reviewer_stage")
async def issue_reviewer(state: dict) -> dict:  # noqa: ARG001
    _ISSUE_CALLS["reviewer"] += 1
    return {
        "review_decision": {
            "severity": "low",
            "issues": [],
            "recommendation": "approve",
        }
    }


@activity.defn(name="pr_creator_stage")
async def issue_pr_creator(state: dict) -> dict:  # noqa: ARG001
    return {"pr_url": "https://github.example/octo-org/octo-repo/pull/42"}


@activity.defn(name="merge_branch")
async def issue_merge_branch(state: dict) -> dict:
    _ISSUE_CALLS["merge"] += 1
    assert state["merge_gate_approved"] is True
    return {"merged": True}


@activity.defn(name="mark_issue_done_activity")
async def issue_mark_done(*_args: Any, **_kwargs: Any) -> dict:
    _ISSUE_CALLS["mark_done"] += 1
    return {"done_label_added": True}


@activity.defn(name="quarantine_closed_issue_activity")
async def issue_quarantine(*_args: Any, **_kwargs: Any) -> dict:
    _ISSUE_CALLS["quarantine"] += 1
    return {"comment_posted": True}


_ISSUE_SUPERVISOR = (
    issue_setup_worker,
    issue_teardown_worker,
    issue_quarantine,
)
_ISSUE_AGENT = (
    issue_hydrate,
    issue_triage,
    issue_upsert_phase,
    issue_swap_label,
    issue_post_comment,
    issue_discovery,
    issue_build,
    issue_verify,
    issue_fixer,
    issue_reviewer,
    issue_pr_creator,
    issue_merge_branch,
    issue_mark_done,
)


async def _wait_until_issue_design_gate(handle) -> None:
    for _ in range(160):
        summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
        if summary.get("pending_gate") == "design":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("issue workflow did not reach the design gate")


async def _wait_until_issue_merge_gate(handle) -> None:
    for _ in range(160):
        summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
        if summary.get("pending_gate") == "merge":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("issue workflow did not reach the merge gate")


async def _send(handle, signal: ApprovalSignal) -> None:
    await handle.execute_update(
        DarkFactoryIssueWorkflow.signal_approval,
        signal,
    )


async def _start_issue_workflow(env, wf_id: str):
    return await env.client.start_workflow(
        DarkFactoryIssueWorkflow.run,
        _issue_run_request(),
        id=wf_id,
        task_queue="supervisor-tq",
    )


def test_issue_workflow_waits_at_merge_gate_then_merges() -> None:
    asyncio.run(_run_issue_merge_approve())


async def _run_issue_merge_approve() -> None:
    _reset_issue_calls()
    wf_id = "test-issue-merge-approve"
    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_ISSUE_SUPERVISOR),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_ISSUE_AGENT),
        ):
            handle = await _start_issue_workflow(env, wf_id)
            await _wait_until_issue_design_gate(handle)
            await _send(
                handle,
                ApprovalSignal(kind="Approve", author="octocat", comment_id=10),
            )
            await _wait_until_issue_merge_gate(handle)
            assert _ISSUE_CALLS["build"] == 1
            assert _ISSUE_CALLS["verify"] == 1
            assert _ISSUE_CALLS["reviewer"] == 1
            assert _ISSUE_CALLS["merge"] == 0
            await _send(
                handle,
                ApprovalSignal(
                    kind="Approve",
                    author="octocat",
                    comment_id=20,
                    text="merge approved",
                ),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert _ISSUE_CALLS["merge"] == 1
    assert _ISSUE_CALLS["mark_done"] == 1
    assert result.state["merge_gate_approved"] is True
    assert result.state["gate_approved"] is True


def test_issue_workflow_merge_gate_fix_routes_to_fixer_then_re_review() -> None:
    asyncio.run(_run_issue_merge_fix())


async def _run_issue_merge_fix() -> None:
    _reset_issue_calls()
    wf_id = "test-issue-merge-fix"
    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_ISSUE_SUPERVISOR),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_ISSUE_AGENT),
        ):
            handle = await _start_issue_workflow(env, wf_id)
            await _wait_until_issue_design_gate(handle)
            await _send(
                handle,
                ApprovalSignal(kind="Approve", author="octocat", comment_id=10),
            )
            await _wait_until_issue_merge_gate(handle)
            assert _ISSUE_CALLS["fixer"] == 0
            await _send(
                handle,
                ApprovalSignal(
                    kind="Fix",
                    author="octocat",
                    comment_id=20,
                    text="please tighten error path",
                ),
            )
            await _wait_until_issue_merge_gate(handle)
            assert _ISSUE_CALLS["fixer"] == 1
            assert _ISSUE_CALLS["verify"] == 2
            assert _ISSUE_CALLS["reviewer"] == 2
            assert _ISSUE_CALLS["build"] == 1
            await _send(
                handle,
                ApprovalSignal(kind="Approve", author="octocat", comment_id=30),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert _ISSUE_CALLS["merge"] == 1
    assert result.state["human_fix_focus"] == "please tighten error path"


def test_issue_workflow_merge_gate_rebuild_routes_to_builder_then_re_review() -> None:
    asyncio.run(_run_issue_merge_rebuild())


async def _run_issue_merge_rebuild() -> None:
    _reset_issue_calls()
    wf_id = "test-issue-merge-rebuild"
    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_ISSUE_SUPERVISOR),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_ISSUE_AGENT),
        ):
            handle = await _start_issue_workflow(env, wf_id)
            await _wait_until_issue_design_gate(handle)
            await _send(
                handle,
                ApprovalSignal(kind="Approve", author="octocat", comment_id=10),
            )
            await _wait_until_issue_merge_gate(handle)
            await _send(
                handle,
                ApprovalSignal(
                    kind="Rebuild",
                    author="octocat",
                    comment_id=20,
                    text="rerun on WP-2",
                ),
            )
            await _wait_until_issue_merge_gate(handle)
            assert _ISSUE_CALLS["build"] == 2
            assert _ISSUE_CALLS["verify"] == 2
            assert _ISSUE_CALLS["reviewer"] == 2
            assert _ISSUE_CALLS["fixer"] == 0
            await _send(
                handle,
                ApprovalSignal(kind="Approve", author="octocat", comment_id=30),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert _ISSUE_CALLS["merge"] == 1
    assert result.state["human_rebuild_focus"] == "rerun on WP-2"


def test_issue_workflow_merge_gate_reject_quarantines_without_merge() -> None:
    asyncio.run(_run_issue_merge_reject())


async def _run_issue_merge_reject() -> None:
    _reset_issue_calls()
    wf_id = "test-issue-merge-reject"
    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_ISSUE_SUPERVISOR),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_ISSUE_AGENT),
        ):
            handle = await _start_issue_workflow(env, wf_id)
            await _wait_until_issue_design_gate(handle)
            await _send(
                handle,
                ApprovalSignal(kind="Approve", author="octocat", comment_id=10),
            )
            await _wait_until_issue_merge_gate(handle)
            await _send(
                handle,
                ApprovalSignal(
                    kind="Reject",
                    author="octocat",
                    comment_id=20,
                    text="not ready",
                ),
            )
            result = await handle.result()

    assert result.status == "rejected"
    assert result.reason == "not ready"
    assert _ISSUE_CALLS["merge"] == 0
    assert _ISSUE_CALLS["quarantine"] == 1


# ---------------------------------------------------------------------------
# Task 6.4 — gate update methods (named workflow signals).
# ---------------------------------------------------------------------------


def test_manual_workflow_named_gate_updates_drive_two_gate_flow() -> None:
    asyncio.run(_run_manual_named_gate_updates())


async def _run_manual_named_gate_updates() -> None:
    _reset_calls()
    wf_id = "test-manual-named-gates"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="exercise approve_brief and approve_merge",
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

            await _wait_until_pending_gate(handle, "brief")
            await handle.execute_update(
                DarkFactoryWorkflow.approve_brief,
                GateDecision(approved=True, reason="brief approved by name"),
            )

            summary = await _wait_until_pending_gate(handle, "merge")
            assert summary["brief_gate_approved"] is True
            assert _CALLS["build"] == 1
            assert _CALLS["pr_creator"] == 1
            assert _CALLS["reviewer"] == 1
            assert _CALLS["merge"] == 0

            await handle.execute_update(
                DarkFactoryWorkflow.approve_merge,
                GateDecision(approved=True, reason="merge approved by name"),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert result.state["brief_gate_approved"] is True
    assert result.state["merge_gate_approved"] is True
    assert result.state["gate_approved"] is True
    assert _CALLS["merge"] == 1


def test_manual_workflow_reject_brief_short_circuits_before_build() -> None:
    asyncio.run(_run_manual_reject_brief())


async def _run_manual_reject_brief() -> None:
    _reset_calls()
    wf_id = "test-manual-reject-brief"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="reject_brief should skip build",
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
            await _wait_until_pending_gate(handle, "brief")
            await handle.execute_update(
                DarkFactoryWorkflow.reject_brief,
                GateDecision(approved=False, reason="not enough detail"),
            )
            result = await handle.result()

    assert result.status == "rejected"
    assert result.reason == "not enough detail"
    assert _CALLS["build"] == 0
    assert _CALLS["pr_creator"] == 0


def test_manual_workflow_reject_merge_short_circuits_before_merge() -> None:
    asyncio.run(_run_manual_reject_merge())


async def _run_manual_reject_merge() -> None:
    _reset_calls()
    wf_id = "test-manual-reject-merge"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="reject_merge should skip merge",
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
            await _wait_until_pending_gate(handle, "brief")
            await handle.execute_update(
                DarkFactoryWorkflow.approve_brief,
                GateDecision(approved=True, reason="brief approved"),
            )
            await _wait_until_pending_gate(handle, "merge")
            await handle.execute_update(
                DarkFactoryWorkflow.reject_merge,
                GateDecision(approved=False, reason="post-PR concern"),
            )
            result = await handle.result()

    assert result.status == "rejected"
    assert result.reason == "post-PR concern"
    assert _CALLS["pr_creator"] == 1
    assert _CALLS["reviewer"] == 1
    assert _CALLS["merge"] == 0


def test_manual_workflow_trigger_fix_routes_to_fixer_then_re_review() -> None:
    asyncio.run(_run_manual_trigger_fix())


async def _run_manual_trigger_fix() -> None:
    _reset_calls()
    wf_id = "test-manual-trigger-fix"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="trigger_fix should run fixer + verify + reviewer",
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
            await _wait_until_pending_gate(handle, "brief")
            await handle.execute_update(
                DarkFactoryWorkflow.approve_brief,
                GateDecision(approved=True, reason="brief approved"),
            )
            await _wait_until_pending_gate(handle, "merge")
            assert _CALLS["fixer"] == 0
            await handle.execute_update(
                DarkFactoryWorkflow.trigger_fix,
                GateDecision(approved=False, reason="please tighten error path"),
            )
            await _wait_until_pending_gate(handle, "merge")
            assert _CALLS["fixer"] == 1
            assert _CALLS["verify"] == 2
            assert _CALLS["reviewer"] == 2
            assert _CALLS["build"] == 1

            await handle.execute_update(
                DarkFactoryWorkflow.approve_merge,
                GateDecision(approved=True, reason="merge approved"),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert result.state["human_fix_focus"] == "please tighten error path"
    assert result.state["human_fix_author"] == "human"
    assert _CALLS["merge"] == 1


def test_manual_workflow_trigger_rebuild_routes_to_builder_then_re_review() -> None:
    asyncio.run(_run_manual_trigger_rebuild())


async def _run_manual_trigger_rebuild() -> None:
    _reset_calls()
    wf_id = "test-manual-trigger-rebuild"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="trigger_rebuild should rerun builder + verify + reviewer",
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
            await _wait_until_pending_gate(handle, "brief")
            await handle.execute_update(
                DarkFactoryWorkflow.approve_brief,
                GateDecision(approved=True, reason="brief approved"),
            )
            await _wait_until_pending_gate(handle, "merge")
            await handle.execute_update(
                DarkFactoryWorkflow.trigger_rebuild,
                GateDecision(approved=False, reason="rerun on WP-2"),
            )
            await _wait_until_pending_gate(handle, "merge")
            assert _CALLS["build"] == 2
            assert _CALLS["verify"] == 2
            assert _CALLS["reviewer"] == 2
            assert _CALLS["fixer"] == 0

            await handle.execute_update(
                DarkFactoryWorkflow.approve_merge,
                GateDecision(approved=True, reason="merge approved"),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert result.state["human_rebuild_focus"] == "rerun on WP-2"
    assert _CALLS["merge"] == 1


_REVISE_DISCOVERY_RESPONSES: list[dict[str, Any]] = []


@activity.defn(name="discovery_stage")
async def stub_discovery_stage_revise(state: dict) -> dict:  # noqa: ARG001
    if _REVISE_DISCOVERY_RESPONSES:
        return _REVISE_DISCOVERY_RESPONSES.pop(0)
    return {"stories": [], "spec": [], "review_decision": None}


def test_manual_workflow_revise_brief_loops_back_to_planning() -> None:
    asyncio.run(_run_manual_revise_brief())


async def _run_manual_revise_brief() -> None:
    _reset_calls()
    _REVISE_DISCOVERY_RESPONSES.clear()
    _REVISE_DISCOVERY_RESPONSES.extend(
        [
            {
                "stories": [{"id": "story-1", "title": "first attempt"}],
                "spec": [
                    {
                        "story_id": "story-1",
                        "approach": "first try",
                        "verification": ["does the thing"],
                    }
                ],
                "review_decision": {
                    "approved": True,
                    "reason": "looks ok",
                    "edits": {},
                },
            },
            {
                "stories": [{"id": "story-2", "title": "second attempt"}],
                "spec": [
                    {
                        "story_id": "story-2",
                        "approach": "second try with revision",
                        "verification": ["covers the export edge case"],
                    }
                ],
                "review_decision": {
                    "approved": True,
                    "reason": "ok",
                    "edits": {},
                },
            },
        ]
    )

    wf_id = "test-manual-revise-brief"
    req = RunRequest(
        repo_url="/tmp/fake-repo",
        repo_path="/tmp/fake-repo",
        user_request="revise_brief should rerun the planning loop",
    )

    revise_agent_activities = (
        stub_hydrate_stage,
        stub_discovery_stage_revise,
        stub_build_stage,
        stub_verify_stage,
        stub_fixer_stage,
        stub_reviewer_stage,
        stub_pr_creator_stage,
        stub_merge_branch,
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
            activities=list(revise_agent_activities),
        ):
            handle = await env.client.start_workflow(
                DarkFactoryWorkflow.run,
                req,
                id=wf_id,
                task_queue="supervisor-tq",
            )
            await _wait_until_pending_gate(handle, "brief")
            assert _CALLS["build"] == 0

            await handle.execute_update(
                DarkFactoryWorkflow.revise_brief,
                GateDecision(
                    approved=False,
                    reason="Add the export edge case.",
                ),
            )

            # Wait for the workflow to loop back to a fresh brief gate after
            # the revise-driven replan.
            for _ in range(160):
                summary = await handle.query(
                    DarkFactoryWorkflow.current_state_summary
                )
                discovery_calls = summary["planning_attempts"]
                if (
                    summary["pending_gate"] == "brief"
                    and summary["gate_pending"] is True
                    and discovery_calls >= 1
                    and len(summary["planning_feedback"]) >= 1
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("workflow did not replan after revise_brief")

            summary = await handle.query(
                DarkFactoryWorkflow.current_state_summary
            )
            assert any(
                item.get("source") == "human_revise"
                for item in summary["planning_attempt_log"]
            )
            assert "Add the export edge case." in summary["planning_feedback"][0]

            await handle.execute_update(
                DarkFactoryWorkflow.approve_brief,
                GateDecision(approved=True, reason="now approved"),
            )
            await _wait_until_pending_gate(handle, "merge")
            await handle.execute_update(
                DarkFactoryWorkflow.approve_merge,
                GateDecision(approved=True, reason="merge"),
            )
            result = await handle.result()

    assert result.status == "merged"
    # Two discovery calls — one before revise, one after.
    assert any(
        item.get("source") == "human_revise"
        for item in result.state["planning_attempt_log"]
    )
    assert "Add the export edge case." in result.state["planning_feedback"][0]


# ---------------------------------------------------------------------------
# Issue workflow named gate updates.
# ---------------------------------------------------------------------------


def test_issue_workflow_named_brief_and_merge_updates_drive_flow() -> None:
    asyncio.run(_run_issue_named_gate_updates())


async def _run_issue_named_gate_updates() -> None:
    _reset_issue_calls()
    wf_id = "test-issue-named-gates"
    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_ISSUE_SUPERVISOR),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_ISSUE_AGENT),
        ):
            handle = await _start_issue_workflow(env, wf_id)
            await _wait_until_issue_design_gate(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.approve_brief,
                GateDecision(approved=True, reason="brief approved"),
            )
            await _wait_until_issue_merge_gate(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.approve_merge,
                GateDecision(approved=True, reason="merge approved"),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert _ISSUE_CALLS["merge"] == 1


def test_issue_workflow_trigger_fix_named_update_routes_to_fixer() -> None:
    asyncio.run(_run_issue_trigger_fix_named())


async def _run_issue_trigger_fix_named() -> None:
    _reset_issue_calls()
    wf_id = "test-issue-trigger-fix-named"
    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_ISSUE_SUPERVISOR),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_ISSUE_AGENT),
        ):
            handle = await _start_issue_workflow(env, wf_id)
            await _wait_until_issue_design_gate(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.approve_brief,
                GateDecision(approved=True, reason="brief approved"),
            )
            await _wait_until_issue_merge_gate(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.trigger_fix,
                GateDecision(
                    approved=False,
                    reason="tighten the error path",
                ),
            )
            await _wait_until_issue_merge_gate(handle)
            assert _ISSUE_CALLS["fixer"] == 1
            await handle.execute_update(
                DarkFactoryIssueWorkflow.approve_merge,
                GateDecision(approved=True, reason="merge approved"),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert result.state["human_fix_focus"] == "tighten the error path"


def test_issue_workflow_trigger_rebuild_named_update_routes_to_builder() -> None:
    asyncio.run(_run_issue_trigger_rebuild_named())


async def _run_issue_trigger_rebuild_named() -> None:
    _reset_issue_calls()
    wf_id = "test-issue-trigger-rebuild-named"
    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_ISSUE_SUPERVISOR),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_ISSUE_AGENT),
        ):
            handle = await _start_issue_workflow(env, wf_id)
            await _wait_until_issue_design_gate(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.approve_brief,
                GateDecision(approved=True, reason="brief approved"),
            )
            await _wait_until_issue_merge_gate(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.trigger_rebuild,
                GateDecision(approved=False, reason="rerun on WP-2"),
            )
            await _wait_until_issue_merge_gate(handle)
            assert _ISSUE_CALLS["build"] == 2
            await handle.execute_update(
                DarkFactoryIssueWorkflow.approve_merge,
                GateDecision(approved=True, reason="merge approved"),
            )
            result = await handle.result()

    assert result.status == "merged"
    assert result.state["human_rebuild_focus"] == "rerun on WP-2"


def test_issue_workflow_reject_merge_named_update_quarantines() -> None:
    asyncio.run(_run_issue_reject_merge_named())


async def _run_issue_reject_merge_named() -> None:
    _reset_issue_calls()
    wf_id = "test-issue-reject-merge-named"
    async with await start_time_skipping_env(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="supervisor-tq",
            workflows=[DarkFactoryIssueWorkflow],
            activities=list(_ISSUE_SUPERVISOR),
        ), Worker(
            env.client,
            task_queue=f"agent-tq-{wf_id}",
            activities=list(_ISSUE_AGENT),
        ):
            handle = await _start_issue_workflow(env, wf_id)
            await _wait_until_issue_design_gate(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.approve_brief,
                GateDecision(approved=True, reason="brief approved"),
            )
            await _wait_until_issue_merge_gate(handle)
            await handle.execute_update(
                DarkFactoryIssueWorkflow.reject_merge,
                GateDecision(approved=False, reason="not ready"),
            )
            result = await handle.result()

    assert result.status == "rejected"
    assert _ISSUE_CALLS["merge"] == 0
    assert _ISSUE_CALLS["quarantine"] == 1
