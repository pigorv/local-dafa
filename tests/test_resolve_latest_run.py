"""Unit tests for the per-attempt workflow ID helpers and `_resolve_latest_run`."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.client import WorkflowExecutionStatus

from darkfactory.runtime.activities import (
    ISSUE_WORKFLOW_ID_PREFIX,
    ISSUE_WORKFLOW_RUN_INFIX,
    WORKER_CONTAINER_NAME_LIMIT,
    _issue_workflow_id,
    _issue_workflow_id_prefix,
    _legacy_issue_workflow_id,
    _marker_in_comments,
    _parse_run_suffix,
    _quarantine_marker,
    _resolve_latest_run,
    _truncate_with_hash,
    _worker_container_name,
)


REPO = "octo-org/octo-repo"
ISSUE_NUMBER = 42


def _execution(workflow_id: str, status: WorkflowExecutionStatus) -> SimpleNamespace:
    return SimpleNamespace(id=workflow_id, status=status)


class _FakeClient:
    def __init__(self, executions: list[SimpleNamespace]) -> None:
        self.executions = executions
        self.queries: list[str] = []
        self.page_sizes: list[int] = []

    def list_workflows(self, query: str, page_size: int) -> "_FakeAsyncIter":
        self.queries.append(query)
        self.page_sizes.append(page_size)
        return _FakeAsyncIter(self.executions)


class _FakeAsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_FakeAsyncIter":
        return self

    async def __anext__(self) -> Any:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


# ---------- ID format helpers ----------


def test_issue_workflow_id_prefix_format():
    assert (
        _issue_workflow_id_prefix(REPO, ISSUE_NUMBER)
        == f"{ISSUE_WORKFLOW_ID_PREFIX}octo-org-octo-repo-42{ISSUE_WORKFLOW_RUN_INFIX}"
    )


def test_issue_workflow_id_full_format():
    assert (
        _issue_workflow_id(REPO, ISSUE_NUMBER, 7)
        == "df-issue-octo-org-octo-repo-42-run-7"
    )


@pytest.mark.parametrize(
    ("repo", "number"),
    [
        ("", 1),
        ("invalid", 1),
        ("/missing-owner", 1),
        ("missing-name/", 1),
        (REPO, 0),
        (REPO, -1),
    ],
)
def test_issue_workflow_id_prefix_rejects_invalid(repo, number):
    with pytest.raises(ValueError):
        _issue_workflow_id_prefix(repo, number)


def test_issue_workflow_id_rejects_run_below_one():
    with pytest.raises(ValueError):
        _issue_workflow_id(REPO, ISSUE_NUMBER, 0)


def test_legacy_issue_workflow_id_uses_old_format():
    assert (
        _legacy_issue_workflow_id(REPO, ISSUE_NUMBER)
        == "df-issue-octo-org-octo-repo-42"
    )


# ---------- run-suffix parsing ----------


def test_parse_run_suffix_returns_int():
    prefix = _issue_workflow_id_prefix(REPO, ISSUE_NUMBER)
    assert _parse_run_suffix(prefix + "5", prefix) == 5
    assert _parse_run_suffix(prefix + "12", prefix) == 12


def test_parse_run_suffix_rejects_bad_inputs():
    prefix = _issue_workflow_id_prefix(REPO, ISSUE_NUMBER)
    assert _parse_run_suffix("unrelated-id", prefix) is None
    assert _parse_run_suffix(prefix + "abc", prefix) is None
    assert _parse_run_suffix(prefix + "0", prefix) is None
    assert _parse_run_suffix(prefix + "-1", prefix) is None


def test_parse_run_suffix_distinguishes_run_2_from_run_12():
    """Prefix-collision safety: run-12 must not be classified as run-1 or run-2."""
    prefix = _issue_workflow_id_prefix(REPO, ISSUE_NUMBER)
    assert _parse_run_suffix(prefix + "12", prefix) == 12
    assert _parse_run_suffix(prefix + "2", prefix) == 2


# ---------- marker-in-comments ----------


def test_marker_in_comments_finds_exact_marker():
    workflow_id = _issue_workflow_id(REPO, ISSUE_NUMBER, 1)
    marker = _quarantine_marker(workflow_id)
    comments = [
        {"body": "Some unrelated reply"},
        {"body": f"{marker}\nDark Factory note..."},
    ]
    assert _marker_in_comments(comments, workflow_id) is True


def test_marker_in_comments_does_not_match_different_run():
    run_1 = _issue_workflow_id(REPO, ISSUE_NUMBER, 1)
    run_2 = _issue_workflow_id(REPO, ISSUE_NUMBER, 2)
    comments = [{"body": _quarantine_marker(run_1)}]
    assert _marker_in_comments(comments, run_1) is True
    assert _marker_in_comments(comments, run_2) is False


def test_marker_in_comments_handles_empty_inputs():
    workflow_id = _issue_workflow_id(REPO, ISSUE_NUMBER, 1)
    assert _marker_in_comments([], workflow_id) is False
    assert _marker_in_comments(None, workflow_id) is False
    assert _marker_in_comments([{"body": ""}], workflow_id) is False


# ---------- _resolve_latest_run ----------


def test_resolve_latest_run_empty_returns_none():
    client = _FakeClient(executions=[])
    result = asyncio.run(_resolve_latest_run(client, REPO, ISSUE_NUMBER))
    assert result == (None, None, 0)
    expected_query = (
        f"WorkflowId STARTS_WITH '{_issue_workflow_id_prefix(REPO, ISSUE_NUMBER)}'"
    )
    assert client.queries == [expected_query]


def test_resolve_latest_run_picks_max_n():
    prefix = _issue_workflow_id_prefix(REPO, ISSUE_NUMBER)
    executions = [
        _execution(prefix + "1", WorkflowExecutionStatus.COMPLETED),
        _execution(prefix + "3", WorkflowExecutionStatus.RUNNING),
        _execution(prefix + "2", WorkflowExecutionStatus.CANCELED),
    ]
    client = _FakeClient(executions=executions)
    workflow_id, status, n = asyncio.run(
        _resolve_latest_run(client, REPO, ISSUE_NUMBER)
    )
    assert workflow_id == prefix + "3"
    assert status == WorkflowExecutionStatus.RUNNING
    assert n == 3


def test_resolve_latest_run_skips_malformed_suffixes():
    prefix = _issue_workflow_id_prefix(REPO, ISSUE_NUMBER)
    executions = [
        _execution(prefix + "abc", WorkflowExecutionStatus.RUNNING),
        _execution(prefix + "0", WorkflowExecutionStatus.RUNNING),
        _execution(prefix + "5", WorkflowExecutionStatus.FAILED),
        # An unrelated id that happens to start with the prefix base but missing -run-
        _execution(
            ISSUE_WORKFLOW_ID_PREFIX + "octo-org-octo-repo-42",
            WorkflowExecutionStatus.RUNNING,
        ),
    ]
    client = _FakeClient(executions=executions)
    workflow_id, status, n = asyncio.run(
        _resolve_latest_run(client, REPO, ISSUE_NUMBER)
    )
    assert workflow_id == prefix + "5"
    assert status == WorkflowExecutionStatus.FAILED
    assert n == 5


def test_resolve_latest_run_distinguishes_run_2_from_run_12():
    prefix = _issue_workflow_id_prefix(REPO, ISSUE_NUMBER)
    executions = [
        _execution(prefix + "12", WorkflowExecutionStatus.RUNNING),
        _execution(prefix + "2", WorkflowExecutionStatus.COMPLETED),
    ]
    client = _FakeClient(executions=executions)
    workflow_id, status, n = asyncio.run(
        _resolve_latest_run(client, REPO, ISSUE_NUMBER)
    )
    assert workflow_id == prefix + "12"
    assert status == WorkflowExecutionStatus.RUNNING
    assert n == 12


# ---------- length-aware container name ----------


def test_truncate_with_hash_passes_short_inputs_through():
    assert _truncate_with_hash("short", 100) == "short"


def test_truncate_with_hash_appends_stable_hash_when_too_long():
    long_value = "x" * 300
    truncated = _truncate_with_hash(long_value, 64)
    assert len(truncated) == 64
    # Deterministic
    assert truncated == _truncate_with_hash(long_value, 64)
    # Different inputs → different truncations
    other = _truncate_with_hash("y" * 300, 64)
    assert truncated != other


def test_worker_container_name_short_id_unchanged():
    name = _worker_container_name("df-issue-octo-org-octo-repo-42-run-1")
    assert name == "darkfactory-worker-df-issue-octo-org-octo-repo-42-run-1"
    assert len(name) <= WORKER_CONTAINER_NAME_LIMIT


def test_worker_container_name_pathological_id_truncated():
    huge_id = "df-issue-" + ("a" * 300) + "-run-1"
    name = _worker_container_name(huge_id)
    assert len(name) <= WORKER_CONTAINER_NAME_LIMIT
    assert name.startswith("darkfactory-worker-")
