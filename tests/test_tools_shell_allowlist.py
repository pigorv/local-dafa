"""Unit tests for permission_gate argv allowlist handling."""
from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from darkfactory.agents.backend import BACKEND_ALLOWLIST
from darkfactory.hooks.permission_gate import make_permission_gate
from darkfactory.tools.shell import FORBIDDEN_TOKENS


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_worker_allowlist_contains_architecture_binaries():
    assert BACKEND_ALLOWLIST == frozenset(
        {"mvn", "gradle", "./gradlew", "git", "cat", "ls"}
    )


def test_forbidden_tokens_preserved():
    for tok in ("&&", "||", ";", "|", "$(", "`", ">", "<"):
        assert tok in FORBIDDEN_TOKENS


def test_mvn_test_q_passes_permission_gate():
    gate = make_permission_gate("backend", BACKEND_ALLOWLIST)
    result = _run(
        gate(
            "sandbox_bash",
            {"argv": ["mvn", "test", "-q"]},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultAllow)


def test_mvn_chain_with_rm_rejected():
    gate = make_permission_gate("backend", BACKEND_ALLOWLIST)
    result = _run(
        gate(
            "sandbox_bash",
            {"argv": ["mvn", "test", "&&", "rm", "-rf", "/"]},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert "&&" in result.message


def test_binary_outside_allowlist_rejected():
    gate = make_permission_gate("backend", BACKEND_ALLOWLIST)
    result = _run(
        gate(
            "sandbox_bash",
            {"argv": ["rm", "-rf", "/"]},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert "allowlist" in result.message


def test_empty_argv_rejected():
    gate = make_permission_gate("backend", BACKEND_ALLOWLIST)
    result = _run(
        gate("sandbox_bash", {"argv": []}, ToolPermissionContext())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "argv must be non-empty"
