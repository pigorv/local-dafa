"""Coverage for the issue-poll dispatch workflow with per-attempt run IDs."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from darkfactory.runtime.activities import (
    _issue_workflow_id,
    _legacy_issue_workflow_id,
    _quarantine_marker,
    start_or_update_issue_workflow_activity,
)
from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow
from darkfactory.runtime.issue_poll_workflow import IssuePollWorkflow
from darkfactory.state import IssuePollRequest
from tests.temporal_testing import start_time_skipping_env


REPO = "octo-org/octo-repo"


_LISTED_ISSUES: list[dict[str, Any]] = []
_CAPACITY: dict[str, int] = {"active": 0, "max_concurrent": 3, "available": 3}
_QUARANTINE_CALLS: list[dict[str, Any]] = []
_APPROVAL_DETECTIONS: list[dict[str, Any]] = []
_APPROVAL_FORWARDS: list[dict[str, Any]] = []
_DETECTED_SIGNAL: dict[str, Any] | None = None


@activity.defn(name="list_ready_issues_activity")
async def stub_list_ready_issues_activity(
    repo: str,
    label: str,
    limit: int,
) -> list[dict[str, Any]]:
    assert repo == REPO
    assert label == "df:ready"
    assert limit == 10
    return _LISTED_ISSUES


@activity.defn(name="issue_workflow_capacity_activity")
async def stub_issue_workflow_capacity_activity() -> dict[str, int]:
    return dict(_CAPACITY)


@activity.defn(name="quarantine_closed_issue_activity")
async def stub_quarantine_closed_issue_activity(
    repo: str,
    issue_number: int,
    workflow_id: str,
    closure_status: str,
) -> dict[str, Any]:
    call = {
        "repo": repo,
        "issue_number": issue_number,
        "workflow_id": workflow_id,
        "closure_status": closure_status,
    }
    _QUARANTINE_CALLS.append(call)
    return {
        **call,
        "label_removed": "df:ready",
        "label_added": None if closure_status == "completed" else "df:failed",
        "comment_posted": True,
    }


@activity.defn(name="detect_approval_signal_activity")
async def stub_detect_approval_signal_activity(
    issue: Any,
    since_id: int = 0,
    workflow_id: str = "",
    latest_spec_rev: int = 1,
) -> dict[str, Any] | None:
    call = {
        "issue": issue,
        "since_id": since_id,
        "workflow_id": workflow_id,
        "latest_spec_rev": latest_spec_rev,
    }
    _APPROVAL_DETECTIONS.append(call)
    return _DETECTED_SIGNAL


@activity.defn(name="signal_issue_workflow_activity")
async def stub_signal_issue_workflow_activity(
    workflow_id: str,
    signal: Any,
) -> dict[str, Any]:
    call = {"workflow_id": workflow_id, "signal": signal}
    _APPROVAL_FORWARDS.append(call)
    return call


class _FakeIssueWorkflowHandle:
    def __init__(
        self,
        workflow_id: str,
        status: WorkflowExecutionStatus,
        *,
        last_seen_comment_id: int = 0,
    ) -> None:
        self.workflow_id = workflow_id
        self.status = status
        self.last_seen_comment_id = last_seen_comment_id
        self.queries: list[Any] = []
        self.updates: list[tuple[Any, list[Any]]] = []

    async def describe(self) -> SimpleNamespace:
        return SimpleNamespace(status=self.status)

    async def query(self, query: Any) -> dict[str, int]:
        self.queries.append(query)
        return {"last_seen_comment_id": self.last_seen_comment_id}

    async def execute_update(self, update: Any, comments: list[Any]) -> None:
        self.updates.append((update, comments))


class _MissingHandle:
    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id

    async def describe(self) -> SimpleNamespace:
        raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def query(self, query: Any) -> Any:
        raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def execute_update(self, update: Any, args: Any) -> None:
        raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")


class _FakeAsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_FakeAsyncIter":
        return self

    async def __anext__(self) -> Any:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeTemporalClient:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.start_attempts: list[dict[str, Any]] = []
        self.handles: dict[str, _FakeIssueWorkflowHandle] = {}
        self.handle_requests: list[tuple[str, str | None]] = []
        self.list_workflow_queries: list[str] = []

    def add_handle(
        self,
        workflow_id: str,
        status: WorkflowExecutionStatus,
        *,
        last_seen_comment_id: int = 0,
    ) -> _FakeIssueWorkflowHandle:
        self.handles[workflow_id] = _FakeIssueWorkflowHandle(
            workflow_id,
            status,
            last_seen_comment_id=last_seen_comment_id,
        )
        return self.handles[workflow_id]

    def list_workflows(self, query: str, page_size: int) -> _FakeAsyncIter:
        self.list_workflow_queries.append(query)
        prefix = _extract_prefix(query)
        matched = [
            SimpleNamespace(id=h.workflow_id, status=h.status)
            for h in self.handles.values()
            if h.workflow_id.startswith(prefix)
        ]
        return _FakeAsyncIter(matched)

    async def start_workflow(self, workflow: Any, req: Any, **kwargs: Any) -> None:
        workflow_id = kwargs["id"]
        call = {
            "workflow": workflow,
            "req": req,
            "id": workflow_id,
            "task_queue": kwargs.get("task_queue"),
            "id_reuse_policy": kwargs.get("id_reuse_policy"),
        }
        self.start_attempts.append(call)
        if workflow_id in self.handles:
            raise WorkflowAlreadyStartedError(
                workflow_id,
                "DarkFactoryIssueWorkflow",
                run_id=f"run-{workflow_id}",
            )
        # Newly started: register a RUNNING handle so subsequent get_workflow_handle
        # finds it (used by the comment-fanout step in the activity).
        self.add_handle(workflow_id, WorkflowExecutionStatus.RUNNING)
        self.started.append(call)

    def get_workflow_handle(
        self,
        workflow_id: str,
        *,
        run_id: str | None = None,
    ) -> Any:
        self.handle_requests.append((workflow_id, run_id))
        if workflow_id in self.handles:
            return self.handles[workflow_id]
        return _MissingHandle(workflow_id)


def _extract_prefix(query: str) -> str:
    """Pull the prefix out of `WorkflowId STARTS_WITH '<prefix>'`."""
    needle = "STARTS_WITH '"
    start = query.index(needle) + len(needle)
    end = query.index("'", start)
    return query[start:end]


def test_issue_poll_workflow_decision_matrix() -> None:
    asyncio.run(_run_issue_poll_workflow_check())


async def _run_issue_poll_workflow_check() -> None:
    _LISTED_ISSUES[:] = _fixture_issues()
    _CAPACITY.update({"active": 1, "max_concurrent": 3, "available": 2})
    _QUARANTINE_CALLS.clear()
    _APPROVAL_DETECTIONS.clear()
    _APPROVAL_FORWARDS.clear()
    global _DETECTED_SIGNAL
    _DETECTED_SIGNAL = None

    fake_client = _FakeTemporalClient()
    # Issue 2: latest run-1 RUNNING (we'll forward new comments to it)
    issue2_run1 = _issue_workflow_id(REPO, 2, 1)
    fake_client.add_handle(
        issue2_run1,
        WorkflowExecutionStatus.RUNNING,
        last_seen_comment_id=20,
    )
    # Issue 3: latest run-1 COMPLETED, no quarantine marker → ignored + quarantine
    issue3_run1 = _issue_workflow_id(REPO, 3, 1)
    fake_client.add_handle(issue3_run1, WorkflowExecutionStatus.COMPLETED)
    # Issue 4: latest run-1 CANCELED + quarantine marker present in comments → start run-2
    issue4_run1 = _issue_workflow_id(REPO, 4, 1)
    fake_client.add_handle(issue4_run1, WorkflowExecutionStatus.CANCELED)

    async def fake_connect_temporal_client() -> _FakeTemporalClient:
        return fake_client

    with patch(
        "darkfactory.runtime.activities._connect_temporal_client",
        new=fake_connect_temporal_client,
    ):
        async with await start_time_skipping_env(
            data_converter=pydantic_data_converter
        ) as env:
            async with Worker(
                env.client,
                task_queue="supervisor-tq",
                workflows=[IssuePollWorkflow],
                activities=[
                    stub_list_ready_issues_activity,
                    stub_issue_workflow_capacity_activity,
                    start_or_update_issue_workflow_activity,
                    stub_detect_approval_signal_activity,
                    stub_signal_issue_workflow_activity,
                    stub_quarantine_closed_issue_activity,
                ],
            ):
                result = await env.client.execute_workflow(
                    IssuePollWorkflow.run,
                    IssuePollRequest(
                        repo=REPO,
                        label="df:ready",
                        limit=10,
                    ),
                    id="test-issue-poll-workflow",
                    task_queue="supervisor-tq",
                )

    assert result["issues_seen"] == 4
    assert result["started"] == 2  # issue 1 (run-1) + issue 4 (run-2)
    assert result["updated"] == 1  # issue 2
    assert result["ignored"] == 1  # issue 3
    assert result["throttled"] == 0
    assert result["quarantined"] == 1  # issue 3
    assert result["approval_signaled"] == 0

    # Quarantine fired only for the closed-no-marker case
    assert _QUARANTINE_CALLS == [
        {
            "repo": REPO,
            "issue_number": 3,
            "workflow_id": issue3_run1,
            "closure_status": "completed",
        }
    ]

    issue1_run1 = _issue_workflow_id(REPO, 1, 1)
    issue4_run2 = _issue_workflow_id(REPO, 4, 2)

    started_ids = [call["id"] for call in fake_client.started]
    assert started_ids == [issue1_run1, issue4_run2]

    # Each start used REJECT_DUPLICATE
    for call in fake_client.started:
        assert call["task_queue"] == "supervisor-tq"
        assert call["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
        assert call["workflow"] == DarkFactoryIssueWorkflow.run

    # Issue 2 (RUNNING): comments forwarded
    issue2_handle = fake_client.handles[issue2_run1]
    assert issue2_handle.queries == [DarkFactoryIssueWorkflow.current_state_summary]
    assert len(issue2_handle.updates) == 1
    update_fn, forwarded = issue2_handle.updates[0]
    assert update_fn == DarkFactoryIssueWorkflow.post_new_comments
    assert [c.id for c in forwarded] == [22]
    assert forwarded[0].body == "Fresh clarification reply."

    # Issue 4 fresh run-2: history fanout — non-marker comments seeded into the run
    issue4_run2_handle = fake_client.handles[issue4_run2]
    assert len(issue4_run2_handle.updates) == 1
    update_fn, history = issue4_run2_handle.updates[0]
    assert update_fn == DarkFactoryIssueWorkflow.post_new_comments
    history_bodies = [c.body for c in history]
    # The quarantine marker comment is filtered out; the user's narrative
    # answer is preserved.
    assert all("df-quarantine" not in body for body in history_bodies)
    assert "Please retry — I rebased main." in history_bodies

    # Issue 3 (closed COMPLETED): no execute_update issued (no retry signal)
    issue3_handle = fake_client.handles[issue3_run1]
    assert issue3_handle.updates == []

    # Per-issue result entries
    by_workflow_id = {
        item["workflow_id"]: item for item in result["issue_workflows"]
    }
    assert by_workflow_id[issue1_run1]["action"] == "started"
    assert by_workflow_id[issue1_run1]["run_number"] == 1
    assert by_workflow_id[issue2_run1]["action"] == "updated"
    assert by_workflow_id[issue2_run1]["comments_forwarded"] == 1
    assert by_workflow_id[issue3_run1]["action"] == "ignored"
    assert by_workflow_id[issue3_run1]["reason"] == "closed:completed"
    assert by_workflow_id[issue3_run1]["quarantine"]["closure_status"] == "completed"
    assert by_workflow_id[issue4_run2]["action"] == "started"
    assert by_workflow_id[issue4_run2]["run_number"] == 2
    assert by_workflow_id[issue4_run2]["comments_forwarded"] >= 1
    assert [call["workflow_id"] for call in _APPROVAL_DETECTIONS] == [issue2_run1]
    assert _APPROVAL_FORWARDS == []


def test_legacy_running_workflow_is_synced_when_no_per_attempt_run_exists() -> None:
    """If a single-attempt legacy workflow is still RUNNING for an issue and
    no per-attempt -run-N workflow exists yet, the activity should sync to it
    instead of starting a fresh run-1 (which would orphan the legacy run)."""

    async def _run() -> None:
        legacy_id = _legacy_issue_workflow_id(REPO, 99)
        fake_client = _FakeTemporalClient()
        legacy_handle = fake_client.add_handle(
            legacy_id,
            WorkflowExecutionStatus.RUNNING,
            last_seen_comment_id=0,
        )

        async def fake_connect_temporal_client() -> _FakeTemporalClient:
            return fake_client

        issue = {
            "repo": REPO,
            "number": 99,
            "url": f"https://github.com/{REPO}/issues/99",
            "title": "Legacy in-flight",
            "body": "Predates per-attempt IDs.",
            "labels": ["df:ready"],
        }
        comments = [
            {
                "id": 990,
                "author": {"login": "octocat"},
                "body": "Mid-conversation.",
                "created_at": "2026-05-05T10:00:00Z",
            }
        ]

        with patch(
            "darkfactory.runtime.activities._connect_temporal_client",
            new=fake_connect_temporal_client,
        ):
            result = await start_or_update_issue_workflow_activity(
                REPO, 99, issue, comments, True
            )

        assert result["action"] == "updated"
        assert result["workflow_id"] == legacy_id
        # No new -run-N workflow was started.
        assert fake_client.start_attempts == []
        # The legacy handle received the comment fanout.
        assert len(legacy_handle.updates) == 1
        update_fn, forwarded = legacy_handle.updates[0]
        assert update_fn == DarkFactoryIssueWorkflow.post_new_comments
        assert [c.id for c in forwarded] == [990]

    asyncio.run(_run())


def test_issue_poll_workflow_forwards_detected_approval_signal() -> None:
    asyncio.run(_run_issue_poll_approval_forwarding())


async def _run_issue_poll_approval_forwarding() -> None:
    global _DETECTED_SIGNAL
    run_id = _issue_workflow_id(REPO, 2, 1)
    _LISTED_ISSUES[:] = [
        {
            "issue": {
                "repo": REPO,
                "number": 2,
                "url": f"https://github.com/{REPO}/issues/2",
                "title": "Awaiting approval",
                "body": "Spec is ready.",
                "labels": ["df:awaiting-approval"],
            },
            "comments": [
                {
                    "id": 101,
                    "author": {"login": "octocat"},
                    "body": "/df approve",
                    "created_at": "2026-05-06T10:00:00Z",
                }
            ],
        }
    ]
    _CAPACITY.update({"active": 1, "max_concurrent": 3, "available": 2})
    _QUARANTINE_CALLS.clear()
    _APPROVAL_DETECTIONS.clear()
    _APPROVAL_FORWARDS.clear()
    _DETECTED_SIGNAL = {
        "kind": "Approve",
        "author": "octocat",
        "comment_id": 101,
        "text": "",
        "created_at": "2026-05-06T10:00:00Z",
    }

    fake_client = _FakeTemporalClient()
    fake_client.add_handle(
        run_id,
        WorkflowExecutionStatus.RUNNING,
        last_seen_comment_id=100,
    )

    async def fake_connect_temporal_client() -> _FakeTemporalClient:
        return fake_client

    with patch(
        "darkfactory.runtime.activities._connect_temporal_client",
        new=fake_connect_temporal_client,
    ):
        async with await start_time_skipping_env(
            data_converter=pydantic_data_converter
        ) as env:
            async with Worker(
                env.client,
                task_queue="supervisor-tq",
                workflows=[IssuePollWorkflow],
                activities=[
                    stub_list_ready_issues_activity,
                    stub_issue_workflow_capacity_activity,
                    start_or_update_issue_workflow_activity,
                    stub_detect_approval_signal_activity,
                    stub_signal_issue_workflow_activity,
                    stub_quarantine_closed_issue_activity,
                ],
            ):
                result = await env.client.execute_workflow(
                    IssuePollWorkflow.run,
                    IssuePollRequest(repo=REPO, label="df:ready", limit=10),
                    id="test-issue-poll-approval-forward",
                    task_queue="supervisor-tq",
                )

    assert result["approval_signaled"] == 1
    assert _APPROVAL_DETECTIONS == [
        {
            "issue": _LISTED_ISSUES[0]["issue"],
            "since_id": 100,
            "workflow_id": run_id,
            "latest_spec_rev": 1,
        }
    ]
    assert _APPROVAL_FORWARDS == [
        {"workflow_id": run_id, "signal": _DETECTED_SIGNAL}
    ]
    _DETECTED_SIGNAL = None


def _fixture_issues() -> list[dict[str, Any]]:
    issue4_run1_id = _issue_workflow_id(REPO, 4, 1)
    return [
        # Issue 1: never run before → start run-1
        {
            "issue": {
                "repo": REPO,
                "number": 1,
                "url": f"https://github.com/{REPO}/issues/1",
                "title": "Fresh issue",
                "body": "Ready to start.",
                "labels": ["df:ready"],
            },
            "comments": [],
        },
        # Issue 2: run-1 RUNNING → forward new comments
        {
            "issue": {
                "repo": REPO,
                "number": 2,
                "url": f"https://github.com/{REPO}/issues/2",
                "title": "Running issue",
                "body": "Awaiting clarification.",
                "labels": ["df:ready"],
            },
            "comments": [
                {
                    "id": 20,
                    "author": {"login": "octocat"},
                    "body": "Older context.",
                    "created_at": "2026-05-05T10:00:00Z",
                },
                {
                    "id": 21,
                    "author": {"login": "darkfactory"},
                    "body": (
                        f"<!-- df-clarify:{_issue_workflow_id(REPO, 2, 1)}:1 -->"
                    ),
                    "created_at": "2026-05-05T10:01:00Z",
                },
                {
                    "id": 22,
                    "author": {"login": "octocat"},
                    "body": "Fresh clarification reply.",
                    "created_at": "2026-05-05T10:02:00Z",
                },
            ],
        },
        # Issue 3: run-1 COMPLETED, no marker → ignored + quarantine
        {
            "issue": {
                "repo": REPO,
                "number": 3,
                "url": f"https://github.com/{REPO}/issues/3",
                "title": "Already handled issue",
                "body": "This child workflow is closed.",
                "labels": ["df:ready"],
            },
            "comments": [
                {
                    "id": 30,
                    "author": {"login": "octocat"},
                    "body": "Do not forward to a closed workflow.",
                    "created_at": "2026-05-05T10:03:00Z",
                }
            ],
        },
        # Issue 4: run-1 CANCELED + quarantine marker for run-1 present
        # in comments (= human re-added df:ready) → start run-2 with
        # comment fanout
        {
            "issue": {
                "repo": REPO,
                "number": 4,
                "url": f"https://github.com/{REPO}/issues/4",
                "title": "Cancelled, retry requested",
                "body": "Retry me.",
                "labels": ["df:ready"],
            },
            "comments": [
                {
                    "id": 40,
                    "author": {"login": "darkfactory"},
                    "body": (
                        f"{_quarantine_marker(issue4_run1_id)}\n"
                        "Dark Factory workflow ended in state `canceled`."
                    ),
                    "created_at": "2026-05-05T10:04:00Z",
                },
                {
                    "id": 41,
                    "author": {"login": "octocat"},
                    "body": "Please retry — I rebased main.",
                    "created_at": "2026-05-05T10:05:00Z",
                },
            ],
        },
    ]
