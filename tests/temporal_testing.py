"""Shared Temporal test environment helpers.

The Python SDK's time-skipping workflow environment runs a local
``temporal-test-server`` binary. Pointing every workflow test through this
helper lets local runs use an explicit binary or a stable repo-local cache
instead of repeatedly relying on the system temp directory.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import WorkflowEnvironment

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DOWNLOAD_DIR = _REPO_ROOT / ".cache" / "temporal-test-server"
_REQUIRED_ENV = "TEMPORAL_TEST_SERVER_REQUIRED"


def _is_download_failure(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "temporal.download" in message or "error sending request" in message


async def start_time_skipping_env(**kwargs: Any) -> WorkflowEnvironment:
    """Start Temporal's local time-skipping test server.

    Set ``TEMPORAL_TEST_SERVER_PATH`` to force an existing binary and avoid all
    download attempts. Otherwise the SDK downloads/caches the test server under
    ``TEMPORAL_TEST_SERVER_DOWNLOAD_DIR`` or ``.cache/temporal-test-server``.
    If the SDK cannot download the binary in an offline environment, tests skip
    unless ``TEMPORAL_TEST_SERVER_REQUIRED=1`` is set.
    """

    existing_path = os.getenv("TEMPORAL_TEST_SERVER_PATH")
    if existing_path:
        kwargs.setdefault(
            "test_server_existing_path",
            str(Path(existing_path).expanduser().resolve()),
        )
    else:
        download_dir = os.getenv(
            "TEMPORAL_TEST_SERVER_DOWNLOAD_DIR",
            str(_DEFAULT_DOWNLOAD_DIR),
        )
        kwargs.setdefault(
            "download_dest_dir",
            str(Path(download_dir).expanduser().resolve()),
        )

    try:
        return await WorkflowEnvironment.start_time_skipping(**kwargs)
    except RuntimeError as exc:
        if (
            not existing_path
            and os.getenv(_REQUIRED_ENV) != "1"
            and _is_download_failure(exc)
        ):
            pytest.skip(
                "Temporal time-skipping test server is unavailable. "
                "Run `scripts/bootstrap_temporal_test_server.py` with network "
                "access, set TEMPORAL_TEST_SERVER_PATH, or set "
                f"{_REQUIRED_ENV}=1 to fail instead of skipping."
            )
        raise RuntimeError(
            "Unable to start Temporal's local time-skipping test server. "
            "Run `scripts/bootstrap_temporal_test_server.py` once with network "
            "access, or set TEMPORAL_TEST_SERVER_PATH to an existing "
            "temporal-test-server binary."
        ) from exc
