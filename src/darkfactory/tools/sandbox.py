"""RepoSandbox: per-task subprocess wrapper rooted at the repo path.

Runs commands directly inside the worker container's filesystem. The worker
container itself is the isolation boundary (cap_drop, no-new-privileges,
pids_limit, mem_limit, network); there is no second inner container. The
shared `{returncode, stdout, stderr, timed_out}` shape and the per-task
registry in `tools/shell.py` are consumed deterministically by the build /
verify subgraphs and repo-touching activities (e.g. the PR-existence
check); agent shell access goes through the built-in `Bash` tool instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


MAX_STDOUT = 200_000
MAX_STDERR = 50_000


class RepoSandbox:
    def __init__(self, repo_path: str):
        self.repo_path = str(Path(repo_path).resolve())

    def exec(
        self,
        argv: list[str],
        timeout: int = 120,
        stdin: str | bytes | None = None,
    ) -> dict[str, Any]:
        if not argv:
            raise ValueError("argv must be non-empty")
        input_bytes = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        try:
            completed = subprocess.run(
                argv,
                cwd=self.repo_path,
                capture_output=True,
                input=input_bytes,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "returncode": 124,
                "stdout": _decode(exc.stdout)[:MAX_STDOUT],
                "stderr": _decode(exc.stderr)[:MAX_STDERR],
                "timed_out": True,
            }
        except FileNotFoundError as exc:
            return {
                "returncode": 127,
                "stdout": "",
                "stderr": f"{argv[0]}: {exc.strerror}",
                "timed_out": False,
            }
        return {
            "returncode": completed.returncode,
            "stdout": _decode(completed.stdout)[:MAX_STDOUT],
            "stderr": _decode(completed.stderr)[:MAX_STDERR],
            "timed_out": False,
        }

    def close(self) -> None:
        return None

    def __enter__(self) -> "RepoSandbox":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _decode(buf: bytes | None) -> str:
    if not buf:
        return ""
    return buf.decode("utf-8", "replace")
