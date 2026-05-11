"""Unit tests for permission_gate argv allowlist handling."""
from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from darkfactory.agents.pr_creator import PR_CREATOR_ALLOWLIST
from darkfactory.agents.registry import get_default_registry
from darkfactory.hooks.permission_gate import make_permission_gate
from darkfactory.tools.shell import FORBIDDEN_TOKENS


BUILDER_ALLOWLIST: frozenset[str] = frozenset(
    get_default_registry().get("builder").tools.argv_allowlist
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_worker_allowlist_contains_architecture_binaries():
    assert BUILDER_ALLOWLIST == frozenset(
        {"mvn", "gradle", "./gradlew", "git", "cat", "ls"}
    )


def test_forbidden_tokens_preserved():
    for tok in ("&&", "||", ";", "|", "$(", "`", ">", "<"):
        assert tok in FORBIDDEN_TOKENS


def test_mvn_test_q_passes_permission_gate():
    gate = make_permission_gate("builder", BUILDER_ALLOWLIST)
    result = _run(
        gate(
            "sandbox_bash",
            {"argv": ["mvn", "test", "-q"]},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultAllow)


def test_mvn_chain_with_rm_rejected():
    gate = make_permission_gate("builder", BUILDER_ALLOWLIST)
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
    gate = make_permission_gate("builder", BUILDER_ALLOWLIST)
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
    gate = make_permission_gate("builder", BUILDER_ALLOWLIST)
    result = _run(
        gate("sandbox_bash", {"argv": []}, ToolPermissionContext())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "argv must be non-empty"


def test_role_command_policy_for_push_and_pr_create():
    role_owned = get_default_registry().role_owned_argv_table()
    worker_gate = make_permission_gate(
        "builder",
        BUILDER_ALLOWLIST | {"gh"},
        role_owned_argv_prefixes=role_owned,
    )
    pr_gate = make_permission_gate(
        "pr_creator",
        PR_CREATOR_ALLOWLIST,
        role_owned_argv_prefixes=role_owned,
    )

    worker_push = _run(
        worker_gate(
            "sandbox_bash",
            {"argv": ["git", "push", "origin", "agent/wf-1"]},
            ToolPermissionContext(),
        )
    )
    pr_create = _run(
        pr_gate(
            "sandbox_bash",
            {"argv": ["gh", "pr", "create", "--title", "T"]},
            ToolPermissionContext(),
        )
    )

    assert isinstance(worker_push, PermissionResultDeny)
    assert "allowed only" in worker_push.message
    assert isinstance(pr_create, PermissionResultAllow)
