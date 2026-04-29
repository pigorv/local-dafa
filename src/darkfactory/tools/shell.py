"""Per-task RepoSandbox registry and shell token deny-list."""
from __future__ import annotations

import threading

from darkfactory.tools.sandbox import RepoSandbox

# Shell metacharacters that must never appear in any argv element; their
# presence means the agent is trying to smuggle a compound command through
# what is supposed to be a single argv invocation.
FORBIDDEN_TOKENS: tuple[str, ...] = ("&&", "||", ";", "|", "$(", "`", ">", "<")


_REGISTRY_LOCK = threading.Lock()
_SANDBOXES: dict[str, RepoSandbox] = {}


def register_sandbox(task_id: str, sandbox: RepoSandbox) -> None:
    with _REGISTRY_LOCK:
        _SANDBOXES[task_id] = sandbox


def get_sandbox(task_id: str) -> RepoSandbox | None:
    with _REGISTRY_LOCK:
        return _SANDBOXES.get(task_id)


def close_sandbox(task_id: str) -> None:
    with _REGISTRY_LOCK:
        sb = _SANDBOXES.pop(task_id, None)
    if sb is not None:
        sb.close()
