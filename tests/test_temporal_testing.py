"""Tests for shared Temporal workflow test helpers."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests import temporal_testing


def test_start_time_skipping_env_uses_explicit_binary(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = object()

    async def fake_start_time_skipping(**kwargs: Any) -> object:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setenv("TEMPORAL_TEST_SERVER_PATH", "/opt/temporal-test-server")
    monkeypatch.setattr(
        temporal_testing.WorkflowEnvironment,
        "start_time_skipping",
        staticmethod(fake_start_time_skipping),
    )

    result = asyncio.run(temporal_testing.start_time_skipping_env(foo="bar"))

    assert result is sentinel
    assert calls == [
        {
            "foo": "bar",
            "test_server_existing_path": "/opt/temporal-test-server",
        }
    ]


def test_start_time_skipping_env_defaults_to_repo_cache(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = object()

    async def fake_start_time_skipping(**kwargs: Any) -> object:
        calls.append(kwargs)
        return sentinel

    monkeypatch.delenv("TEMPORAL_TEST_SERVER_PATH", raising=False)
    monkeypatch.delenv("TEMPORAL_TEST_SERVER_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(
        temporal_testing.WorkflowEnvironment,
        "start_time_skipping",
        staticmethod(fake_start_time_skipping),
    )

    result = asyncio.run(temporal_testing.start_time_skipping_env())

    assert result is sentinel
    assert calls == [
        {
            "download_dest_dir": str(
                temporal_testing._REPO_ROOT / ".cache" / "temporal-test-server"
            )
        }
    ]


def test_start_time_skipping_env_skips_offline_download_failures(
    monkeypatch,
) -> None:
    async def fake_start_time_skipping(**kwargs: Any) -> object:
        raise RuntimeError(
            "Failed starting test server: error sending request for url "
            "(https://temporal.download/temporal-test-server/default)"
        )

    monkeypatch.delenv("TEMPORAL_TEST_SERVER_PATH", raising=False)
    monkeypatch.delenv("TEMPORAL_TEST_SERVER_REQUIRED", raising=False)
    monkeypatch.setattr(
        temporal_testing.WorkflowEnvironment,
        "start_time_skipping",
        staticmethod(fake_start_time_skipping),
    )

    with pytest.raises(pytest.skip.Exception):
        asyncio.run(temporal_testing.start_time_skipping_env())


def test_start_time_skipping_env_required_mode_fails_download_errors(
    monkeypatch,
) -> None:
    async def fake_start_time_skipping(**kwargs: Any) -> object:
        raise RuntimeError(
            "Failed starting test server: error sending request for url "
            "(https://temporal.download/temporal-test-server/default)"
        )

    monkeypatch.delenv("TEMPORAL_TEST_SERVER_PATH", raising=False)
    monkeypatch.setenv("TEMPORAL_TEST_SERVER_REQUIRED", "1")
    monkeypatch.setattr(
        temporal_testing.WorkflowEnvironment,
        "start_time_skipping",
        staticmethod(fake_start_time_skipping),
    )

    with pytest.raises(RuntimeError, match="Unable to start Temporal"):
        asyncio.run(temporal_testing.start_time_skipping_env())
