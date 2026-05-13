"""Unit tests for permission_gate argv allowlist handling.

These tests exercise ``make_permission_gate`` behaviour, not a specific
role's manifest. Builder and Tester both run in pure-denylist mode
(empty argv_allowlist, ``git push`` in argv_denylist); the synthetic
``WORKER_ALLOWLIST`` here is what Fixer / PR Creator use and lets the
allowlist branch still be exercised in isolation.
"""
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


WORKER_ALLOWLIST: frozenset[str] = frozenset(
    {"mvn", "gradle", "./gradlew", "git", "cat", "ls"}
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_builder_and_tester_manifests_use_pure_denylist():
    registry = get_default_registry()
    for role in ("builder", "tester"):
        tools = registry.get(role).tools
        assert tools.argv_allowlist == [], role
        assert tuple(tools.argv_denylist) == (("git", "push"),), role


def test_forbidden_tokens_preserved():
    for tok in ("&&", "||", ";", "|", "$(", "`", ">", "<"):
        assert tok in FORBIDDEN_TOKENS


def test_mvn_test_q_passes_permission_gate():
    gate = make_permission_gate("builder", WORKER_ALLOWLIST)
    result = _run(
        gate(
            "sandbox_bash",
            {"argv": ["mvn", "test", "-q"]},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultAllow)


def test_mvn_chain_with_rm_rejected():
    gate = make_permission_gate("builder", WORKER_ALLOWLIST)
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
    gate = make_permission_gate("builder", WORKER_ALLOWLIST)
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
    gate = make_permission_gate("builder", WORKER_ALLOWLIST)
    result = _run(
        gate("sandbox_bash", {"argv": []}, ToolPermissionContext())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "argv must be non-empty"


# ---------- built-in Bash enforcement ----------


def test_bash_allows_mvn_compile():
    gate = make_permission_gate("builder", WORKER_ALLOWLIST)
    result = _run(
        gate("Bash", {"command": "mvn -q compile"}, ToolPermissionContext())
    )
    assert isinstance(result, PermissionResultAllow)


def test_bash_denies_off_allowlist():
    gate = make_permission_gate("builder", WORKER_ALLOWLIST)
    result = _run(
        gate("Bash", {"command": "rm -rf /"}, ToolPermissionContext())
    )
    assert isinstance(result, PermissionResultDeny)
    assert "allowlist" in result.message


def test_bash_denies_forbidden_tokens():
    gate = make_permission_gate("builder", WORKER_ALLOWLIST)
    result = _run(
        gate(
            "Bash",
            {"command": "mvn test && rm -rf /"},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert "&&" in result.message


def test_bash_denies_merge_prefix():
    gate = make_permission_gate("builder", WORKER_ALLOWLIST | {"gh"})
    result = _run(
        gate("Bash", {"command": "gh pr merge 42"}, ToolPermissionContext())
    )
    assert isinstance(result, PermissionResultDeny)
    assert "gh pr merge" in result.message


def test_bash_denies_role_owned_prefix_for_wrong_role():
    role_owned = get_default_registry().role_owned_argv_table()
    gate = make_permission_gate(
        "builder",
        WORKER_ALLOWLIST | {"gh"},
        role_owned_argv_prefixes=role_owned,
    )
    result = _run(
        gate(
            "Bash",
            {"command": "git push origin agent/wf-1"},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert "allowed only" in result.message


def test_bash_denies_empty_command():
    gate = make_permission_gate("builder", WORKER_ALLOWLIST)
    for command in ("", "   ", None):
        result = _run(
            gate("Bash", {"command": command}, ToolPermissionContext())
        )
        assert isinstance(result, PermissionResultDeny)


def test_bash_denies_unparseable_command():
    gate = make_permission_gate("builder", WORKER_ALLOWLIST)
    # Unterminated quote → shlex.split raises ValueError.
    result = _run(
        gate("Bash", {"command": 'mvn "test'}, ToolPermissionContext())
    )
    assert isinstance(result, PermissionResultDeny)
    assert "unparseable" in result.message


# ---------- per-role argv_denylist ----------


def test_denylist_blocks_matching_prefix():
    gate = make_permission_gate(
        "builder",
        argv_allowlist=(),
        argv_denylist=(("git", "push"),),
    )
    result = _run(
        gate(
            "Bash",
            {"command": "git push origin agent/wf-1"},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert "git push" in result.message
    assert "builder" in result.message


def test_denylist_lets_other_git_subcommands_through():
    gate = make_permission_gate(
        "builder",
        argv_allowlist=(),
        argv_denylist=(("git", "push"),),
    )
    result = _run(
        gate(
            "Bash",
            {"command": "git commit -m 'WP-1: cursor pagination'"},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultAllow)


def test_empty_allowlist_allows_any_binary():
    # Pure denylist mode — the per-role allowlist check is disabled.
    gate = make_permission_gate(
        "builder",
        argv_allowlist=(),
        argv_denylist=(("git", "push"),),
    )
    result = _run(
        gate("Bash", {"command": "make build"}, ToolPermissionContext())
    )
    assert isinstance(result, PermissionResultAllow)


def test_denylist_does_not_widen_global_denies():
    gate = make_permission_gate(
        "builder",
        argv_allowlist=(),
        argv_denylist=(),
    )
    # gh pr merge is still globally denied even with no per-role denylist.
    result = _run(
        gate(
            "Bash",
            {"command": "gh pr merge 42"},
            ToolPermissionContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert "gh pr merge" in result.message


def test_role_command_policy_for_push_and_pr_create():
    role_owned = get_default_registry().role_owned_argv_table()
    worker_gate = make_permission_gate(
        "builder",
        WORKER_ALLOWLIST | {"gh"},
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
