"""Populate the local Temporal SDK test-server cache.

Run this once on a machine with access to Temporal's test-server download.
Afterward, workflow tests use the repo-local cache via
``tests.temporal_testing.start_time_skipping_env``.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from temporalio.testing import WorkflowEnvironment


def _default_cache_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".cache" / "temporal-test-server"


async def _bootstrap(
    *,
    download_dir: Path,
    existing_path: Path | None,
) -> None:
    if existing_path is not None:
        async with await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=str(existing_path)
        ):
            pass
        return

    download_dir.mkdir(parents=True, exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping(
        download_dest_dir=str(download_dir)
    ):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download/cache Temporal's local time-skipping test server."
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=_default_cache_dir(),
        help="Directory where the SDK should cache temporal-test-server.",
    )
    parser.add_argument(
        "--existing-path",
        type=Path,
        help=(
            "Path to an already-downloaded temporal-test-server binary. "
            "This validates the binary without attempting a download."
        ),
    )
    args = parser.parse_args()
    download_dir = args.download_dir.expanduser().resolve()
    existing_path = (
        args.existing_path.expanduser().resolve() if args.existing_path else None
    )
    try:
        asyncio.run(_bootstrap(download_dir=download_dir, existing_path=existing_path))
    except RuntimeError as exc:
        raise SystemExit(
            "Unable to start Temporal's time-skipping test server. "
            "If this machine cannot reach temporal.download, provide a local "
            "binary with --existing-path or set TEMPORAL_TEST_SERVER_PATH."
        ) from exc

    if existing_path is not None:
        print(f"Temporal test server binary is usable: {existing_path}")
    else:
        print(f"Temporal test server cache is ready: {download_dir}")


if __name__ == "__main__":
    main()
