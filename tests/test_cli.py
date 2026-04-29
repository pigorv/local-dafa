"""G-1: `darkfactory run "<prompt>" --repo PATH` submits the real workflow.

The CLI used to raise NotImplementedError on the non-hello path. These
tests pin the actual submission shape: workflow type, RunRequest payload,
task queue, --workflow-id override, and --wait/--no-wait semantics. They
mock the Temporal client so they run without a live Temporal server.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from darkfactory.cli import build_parser, run_command
from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import RunRequest, RunResult


def _fake_client(result: RunResult | None = None) -> tuple[MagicMock, MagicMock]:
    handle = MagicMock()
    handle.result = AsyncMock(
        return_value=result or RunResult(status="merged", state={})
    )
    client = MagicMock()
    client.start_workflow = AsyncMock(return_value=handle)
    return client, handle


def _parse(argv: list[str]):
    return build_parser().parse_args(argv)


def test_run_submits_workflow_and_waits(tmp_path: Path, capsys):
    repo = tmp_path
    args = _parse(["run", "do the thing", "--repo", str(repo)])
    client, handle = _fake_client(RunResult(status="merged", state={"x": 1}))

    with patch(
        "darkfactory.cli._connect_client",
        AsyncMock(return_value=client),
    ):
        rc = asyncio.run(run_command(args))

    assert rc == 0
    client.start_workflow.assert_awaited_once()
    handle.result.assert_awaited_once()

    call = client.start_workflow.await_args
    assert call.args[0] is DarkFactoryWorkflow.run
    request = call.args[1]
    assert isinstance(request, RunRequest)
    assert request.user_request == "do the thing"
    assert request.repo_url == str(repo.resolve())
    assert request.repo_path == "/workspace"
    assert call.kwargs["task_queue"] == "supervisor-tq"
    assert call.kwargs["id"].startswith("darkfactory-")

    out = capsys.readouterr().out
    assert "workflow_id=" in out
    assert "merged" in out


def test_run_no_wait_skips_result(tmp_path: Path, capsys):
    args = _parse(["run", "ship it", "--repo", str(tmp_path), "--no-wait"])
    client, handle = _fake_client()

    with patch(
        "darkfactory.cli._connect_client",
        AsyncMock(return_value=client),
    ):
        rc = asyncio.run(run_command(args))

    assert rc == 0
    client.start_workflow.assert_awaited_once()
    handle.result.assert_not_awaited()

    out = capsys.readouterr().out
    assert "workflow_id=" in out


def test_run_workflow_id_override(tmp_path: Path):
    args = _parse(
        [
            "run",
            "ship it",
            "--repo",
            str(tmp_path),
            "--workflow-id",
            "darkfactory-demo-001",
            "--no-wait",
        ]
    )
    client, _ = _fake_client()

    with patch(
        "darkfactory.cli._connect_client",
        AsyncMock(return_value=client),
    ):
        asyncio.run(run_command(args))

    assert client.start_workflow.await_args.kwargs["id"] == "darkfactory-demo-001"


def test_run_propagates_model_profile_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_PROFILE", "claude")
    args = _parse(["run", "ship it", "--repo", str(tmp_path), "--no-wait"])
    client, _ = _fake_client()

    with patch(
        "darkfactory.cli._connect_client",
        AsyncMock(return_value=client),
    ):
        asyncio.run(run_command(args))

    request = client.start_workflow.await_args.args[1]
    assert request.model_profile == "claude"


def test_run_requires_prompt():
    args = _parse(["run"])
    with pytest.raises(SystemExit):
        asyncio.run(run_command(args))
