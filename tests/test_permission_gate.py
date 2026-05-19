"""Unit tests for the ``permission_gate`` ``can_use_tool`` callback.

The gate now guards a single surface — the built-in ``Bash`` tool, whose
shell line arrives as ``tool_input["command"]``. The legacy in-process
``sandbox_bash`` MCP tool and the gate-approved ``git_push_agent_branch``
tool were removed; PR publication is controlled by role-owned argv
prefixes instead.
"""
from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from darkfactory.hooks.permission_gate import (
    BUILTIN_BASH_TOOL,
    MERGE_TOOLS,
    make_permission_gate,
)
from darkfactory.tools.shell import FORBIDDEN_TOKENS


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _bash(gate: Any, command: str) -> Any:
    """Invoke ``gate`` through the built-in Bash surface."""
    return _run(gate(BUILTIN_BASH_TOOL, {"command": command}, _ctx()))


def test_allowed_argv_passes() -> None:
    gate = make_permission_gate("builder", {"git", "ls"})
    assert isinstance(_bash(gate, "git status"), PermissionResultAllow)


def test_argv_outside_allowlist_denied() -> None:
    gate = make_permission_gate("builder", {"git"})
    result = _bash(gate, "rm -rf /")
    assert isinstance(result, PermissionResultDeny)
    assert "builder allowlist" in result.message
    assert "'rm'" in result.message


def test_forbidden_token_denied() -> None:
    gate = make_permission_gate("builder", {"git"})
    # The pipe character `|` is in FORBIDDEN_TOKENS — it lets an attacker
    # smuggle a compound shell command through a single Bash invocation.
    assert "|" in FORBIDDEN_TOKENS
    result = _bash(gate, "git status | evil")
    assert isinstance(result, PermissionResultDeny)
    assert "forbidden token" in result.message


def test_empty_command_denied() -> None:
    gate = make_permission_gate("builder", {"git"})
    result = _bash(gate, "   ")
    assert isinstance(result, PermissionResultDeny)
    assert "empty or non-string" in result.message


def test_non_string_command_denied() -> None:
    gate = make_permission_gate("builder", {"git"})
    result = _run(gate(BUILTIN_BASH_TOOL, {"command": None}, _ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert "empty or non-string" in result.message


def test_unparseable_command_denied() -> None:
    gate = make_permission_gate("builder", {"git"})
    result = _bash(gate, 'git commit -m "unterminated')
    assert isinstance(result, PermissionResultDeny)
    assert "unparseable" in result.message


def test_gh_pr_merge_command_denied_for_all_roles() -> None:
    for role in ("builder", "pr_creator"):
        gate = make_permission_gate(role, {"git", "gh"})
        result = _bash(gate, "gh pr merge 12 --squash")
        assert isinstance(result, PermissionResultDeny), role
        assert "denied for all agent roles" in result.message


def test_pr_creator_gh_issue_denied_via_manifest_denylist() -> None:
    """pr_creator forbids the issue lifecycle; the manifest argv_denylist
    hard-blocks every ``gh issue ...`` subcommand while leaving the
    role-owned ``gh pr create`` / ``git push`` prefixes reachable."""
    role_owned = {
        ("gh", "pr", "create"): frozenset({"pr_creator"}),
        ("gh", "pr", "list"): frozenset({"pr_creator"}),
        ("git", "push"): frozenset({"pr_creator"}),
    }
    gate = make_permission_gate(
        "pr_creator",
        {"git", "gh"},
        argv_denylist=[("gh", "issue")],
        role_owned_argv_prefixes=role_owned,
    )
    for argv in ("gh issue edit 5 --add-label x", "gh issue close 5"):
        denied = _bash(gate, argv)
        assert isinstance(denied, PermissionResultDeny), argv
        assert "denied for role 'pr_creator'" in denied.message
    for argv in (
        "gh pr create --title T --body B",
        "gh pr list --head agent/wf-1",
        "git push origin agent/wf-1",
    ):
        assert isinstance(_bash(gate, argv), PermissionResultAllow), argv


def test_pr_creator_only_argv_prefixes() -> None:
    role_owned = {
        ("gh", "pr", "create"): frozenset({"pr_creator"}),
        ("gh", "pr", "list"): frozenset({"pr_creator"}),
        ("git", "push"): frozenset({"pr_creator"}),
    }
    for argv in (
        "gh pr create --title T",
        "gh pr list --head agent/wf-1",
        "git push origin agent/wf-1",
    ):
        pr_gate = make_permission_gate(
            "pr_creator",
            {"git", "gh"},
            role_owned_argv_prefixes=role_owned,
        )
        worker_gate = make_permission_gate(
            "builder",
            {"git", "gh"},
            role_owned_argv_prefixes=role_owned,
        )
        assert isinstance(_bash(pr_gate, argv), PermissionResultAllow), argv
        denied = _bash(worker_gate, argv)
        assert isinstance(denied, PermissionResultDeny), argv
        assert "allowed only" in denied.message


def test_merge_tool_name_denied() -> None:
    gate = make_permission_gate("pr_creator", {"git", "gh"})
    for tool in MERGE_TOOLS:
        result = _run(gate(tool, {}, _ctx()))
        assert isinstance(result, PermissionResultDeny), tool
        assert "agents cannot merge" in result.message


def test_mcp_prefixed_merge_tool_matched() -> None:
    """``_match_tool`` matches an ``mcp__<server>__<tool>`` form so the
    merge deny stays correct even if a project .mcp.json ever exposes
    such a tool."""
    gate = make_permission_gate("pr_creator", {"git"})
    result = _run(gate("mcp__someserver__gh_pr_merge", {}, _ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert "agents cannot merge" in result.message


def test_gate_approved_kwarg_still_accepted() -> None:
    """``gate_approved`` is a deprecated no-op kept for call/test
    signature compatibility — passing it must not change behaviour."""
    gate = make_permission_gate("pr_creator", {"git", "gh"}, gate_approved=True)
    assert isinstance(_bash(gate, "git status"), PermissionResultAllow)


def test_unrelated_tool_passes() -> None:
    gate = make_permission_gate("builder", {"git"})
    for name in ("Read", "Edit", "Grep", "Glob", "Write"):
        result = _run(gate(name, {"path": "src/main.py"}, _ctx()))
        assert isinstance(result, PermissionResultAllow), name


def test_manifest_prefixes_grant_current_role_on_existing_entries() -> None:
    """The composer feeds make_permission_gate the registry-derived
    role-owned table; when the calling role appears in the allowed-roles
    set, the prefix is permitted end-to-end."""
    gate = make_permission_gate(
        "pr_creator",
        {"git", "gh"},
        role_owned_argv_prefixes={
            ("gh", "pr", "create"): frozenset({"pr_creator"}),
            ("gh", "pr", "list"): frozenset({"pr_creator"}),
            ("git", "push"): frozenset({"pr_creator"}),
        },
    )
    for argv in (
        "gh pr create --title T",
        "gh pr list --head agent/wf-1",
        "git push origin agent/wf-1",
    ):
        assert isinstance(_bash(gate, argv), PermissionResultAllow), argv


def test_manifest_prefixes_isolated_per_gate() -> None:
    """Each gate is constructed with the registry-derived table at compose
    time; a prefix that no manifest declares is not in the table at all,
    so it falls through the role-owned check and is allowed iff argv[0]
    is in the per-role allowlist."""
    new_prefix = ("gh", "label", "create")
    pr_gate = make_permission_gate(
        "pr_creator",
        {"gh"},
        role_owned_argv_prefixes={new_prefix: frozenset({"pr_creator"})},
    )
    # builder's gate was built without that prefix in its table (mirrors
    # builder.yaml declaring no role-owned prefixes today).
    builder_gate = make_permission_gate("builder", {"gh"})

    argv = "gh label create bug"
    assert isinstance(_bash(pr_gate, argv), PermissionResultAllow)
    # builder's gate never saw the prefix in its table; argv falls through
    # past the role-owned check and is allowed because `gh` is in its
    # allowlist. The point is that the builder gate is not silently widened
    # by another role's manifest.
    assert isinstance(_bash(builder_gate, argv), PermissionResultAllow)


def test_manifest_prefixes_cannot_bypass_denied_argv_prefixes() -> None:
    """Defense in depth: even if a manifest somehow declared a globally-denied
    prefix (registry-load-time validation refuses this — see
    tests/test_registry.py), the gate would still deny because
    DENIED_ARGV_PREFIXES is checked first and the code-declared denylist is
    unremovable."""
    gate = make_permission_gate(
        "pr_creator",
        {"gh"},
        role_owned_argv_prefixes={
            ("gh", "pr", "merge"): frozenset({"pr_creator"}),
        },
    )
    result = _bash(gate, "gh pr merge 1 --squash")
    assert isinstance(result, PermissionResultDeny)
    assert "denied for all agent roles" in result.message
