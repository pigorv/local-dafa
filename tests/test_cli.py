"""G-1: `darkfactory run "<prompt>" --repo PATH` submits the real workflow.

The CLI used to raise NotImplementedError on the non-hello path. These
tests pin the actual submission shape: workflow type, RunRequest payload,
task queue, --workflow-id override, and --wait/--no-wait semantics. They
mock the Temporal client so they run without a live Temporal server.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from darkfactory.cli import build_parser, roles_command, run_command, schedule_command
from darkfactory.runtime import schedule_admin
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


def test_schedule_install_invokes_schedule_admin(capsys):
    args = _parse(
        [
            "schedule",
            "install",
            "--repo",
            "acme/widgets",
            "--label",
            "df:ready",
            "--interval",
            "30s",
        ]
    )
    client = MagicMock()
    result = schedule_admin.ScheduleAdminResult(
        schedule_id="df-watch-acme-widgets",
        action="created",
    )

    with patch(
        "darkfactory.cli._connect_client",
        AsyncMock(return_value=client),
    ), patch(
        "darkfactory.cli.schedule_admin.install_watch_schedule",
        AsyncMock(return_value=result),
    ) as install:
        rc = asyncio.run(schedule_command(args))

    assert rc == 0
    install.assert_awaited_once()
    assert install.await_args.args[0] is client
    assert install.await_args.kwargs["repo"] == "acme/widgets"
    assert install.await_args.kwargs["label"] == "df:ready"
    assert install.await_args.kwargs["interval"] == timedelta(seconds=30)

    out = capsys.readouterr().out
    assert "schedule_id=df-watch-acme-widgets" in out
    assert "action=created" in out


def test_schedule_install_falls_back_to_df_watch_repo_env(monkeypatch, capsys):
    monkeypatch.setenv("DF_WATCH_REPO", "acme/widgets")
    args = _parse(["schedule", "install"])
    client = MagicMock()
    result = schedule_admin.ScheduleAdminResult(
        schedule_id="df-watch-acme-widgets",
        action="created",
    )

    with patch(
        "darkfactory.cli._connect_client",
        AsyncMock(return_value=client),
    ), patch(
        "darkfactory.cli.schedule_admin.install_watch_schedule",
        AsyncMock(return_value=result),
    ) as install:
        rc = asyncio.run(schedule_command(args))

    assert rc == 0
    assert install.await_args.kwargs["repo"] == "acme/widgets"


def test_schedule_install_errors_when_repo_missing(monkeypatch):
    monkeypatch.delenv("DF_WATCH_REPO", raising=False)
    args = _parse(["schedule", "install"])
    client = MagicMock()

    with patch(
        "darkfactory.cli._connect_client",
        AsyncMock(return_value=client),
    ), patch(
        "darkfactory.cli.schedule_admin.install_watch_schedule",
        AsyncMock(),
    ) as install, pytest.raises(SystemExit):
        asyncio.run(schedule_command(args))

    install.assert_not_awaited()


def _write_minimal_manifest(
    manifests_dir: Path,
    *,
    role: str = "noop",
    hooks: list[dict] | None = None,
    mcp: list[str] | None = None,
    allowed: list[str] | None = None,
) -> tuple[Path, Path]:
    prompt = manifests_dir / f"{role}.md"
    prompt.write_text(f"{role} system prompt", encoding="utf-8")
    payload = {
        "identity": {
            "role": role,
            "description": f"{role} role for CLI tests.",
            "when_to_use": "CLI fixture only.",
        },
        "llm": {
            "model": "claude-sonnet-4-5-20250929",
            "thinking": {"enabled": False},
            "prompt_path": str(prompt),
        },
        "tools": {
            "allowed": allowed or [],
            "disallowed": [],
            "argv_allowlist": [],
            "role_owned_argv_prefixes": [],
            "edit_path_allowlist": [],
        },
        "mcp": mcp or [],
        "hooks": hooks or [],
        "budgets": {
            "timeout": None,
            "heartbeat": None,
            "retry_caps": {},
        },
    }
    manifest_path = manifests_dir / f"{role}.yaml"
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return manifest_path, prompt


def test_roles_list_with_zero_manifests_prints_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    monkeypatch.setattr("darkfactory.cli.DEFAULT_MANIFESTS_DIR", tmp_path)
    args = _parse(["roles", "list"])

    rc = roles_command(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() == "0 roles registered (migration not started)"


def test_roles_list_prints_summary_fields_for_each_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    manifest_path, prompt_path = _write_minimal_manifest(
        tmp_path,
        role="architect",
        hooks=[
            {"event": "PreToolUse", "name": "call_cap", "parameters": {"cap": 1}},
            {"event": "PreToolUse", "name": "loop_breaker", "parameters": {}},
        ],
        mcp=["darkfactory"],
        allowed=["Read", "Edit", "Write"],
    )
    monkeypatch.setattr("darkfactory.cli.DEFAULT_MANIFESTS_DIR", tmp_path)
    args = _parse(["roles", "list"])

    rc = roles_command(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "role: architect" in out
    assert "model: claude-sonnet-4-5-20250929" in out
    assert f"prompt: {prompt_path}" in out
    assert "allowed_tools: 3" in out
    assert "hooks: call_cap, loop_breaker" in out
    assert "mcp: darkfactory" in out
    assert (
        f"manifest_sha: {sha256(manifest_path.read_bytes()).hexdigest()}"
        in out
    )
    assert (
        f"prompt_sha: {sha256(prompt_path.read_bytes()).hexdigest()}"
        in out
    )


def test_roles_list_orders_roles_alphabetically_and_separates_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    _write_minimal_manifest(tmp_path, role="zeta")
    _write_minimal_manifest(tmp_path, role="alpha")
    monkeypatch.setattr("darkfactory.cli.DEFAULT_MANIFESTS_DIR", tmp_path)
    args = _parse(["roles", "list"])

    rc = roles_command(args)

    assert rc == 0
    out = capsys.readouterr().out
    alpha_idx = out.index("role: alpha")
    zeta_idx = out.index("role: zeta")
    assert alpha_idx < zeta_idx
    # blank line between the two blocks
    assert "\n\nrole: zeta" in out


def test_schedule_pause_derives_schedule_id_from_repo():
    args = _parse(["schedule", "pause", "--repo", "acme/widgets"])
    client = MagicMock()
    result = schedule_admin.ScheduleAdminResult(
        schedule_id="df-watch-acme-widgets",
        action="paused",
    )

    with patch(
        "darkfactory.cli._connect_client",
        AsyncMock(return_value=client),
    ), patch(
        "darkfactory.cli.schedule_admin.pause",
        AsyncMock(return_value=result),
    ) as pause:
        rc = asyncio.run(schedule_command(args))

    assert rc == 0
    pause.assert_awaited_once()
    assert pause.await_args.args[0] is client
    assert pause.await_args.kwargs["schedule_id"] == "df-watch-acme-widgets"
