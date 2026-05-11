"""Unit tests for the ``permission_gate`` ``can_use_tool`` callback."""
from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from darkfactory.hooks.permission_gate import (
    GATE_APPROVED_TOOLS,
    MERGE_TOOLS,
    SANDBOX_BASH_TOOL,
    make_permission_gate,
)
from darkfactory.tools.shell import FORBIDDEN_TOKENS


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_allowed_argv_passes() -> None:
    gate = make_permission_gate("builder", {"git", "ls"})
    result = _run(
        gate(SANDBOX_BASH_TOOL, {"argv": ["git", "status"]}, _ctx())
    )
    assert isinstance(result, PermissionResultAllow)


def test_argv_outside_allowlist_denied() -> None:
    gate = make_permission_gate("builder", {"git"})
    result = _run(
        gate(SANDBOX_BASH_TOOL, {"argv": ["rm", "-rf", "/"]}, _ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert "builder allowlist" in result.message
    assert "'rm'" in result.message


def test_forbidden_token_denied() -> None:
    gate = make_permission_gate("builder", {"git"})
    # The pipe character `|` is in FORBIDDEN_TOKENS — it lets an attacker
    # smuggle a compound shell command through a single argv invocation.
    assert "|" in FORBIDDEN_TOKENS
    result = _run(
        gate(
            SANDBOX_BASH_TOOL,
            {"argv": ["git", "status", "|", "evil"]},
            _ctx(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert "forbidden token" in result.message


def test_empty_argv_denied() -> None:
    gate = make_permission_gate("builder", {"git"})
    result = _run(gate(SANDBOX_BASH_TOOL, {"argv": []}, _ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert "non-empty" in result.message


def test_mcp_prefixed_sandbox_bash_name_matched() -> None:
    """The MCP transport prefixes tool names as ``mcp__<server>__<tool>``."""
    gate = make_permission_gate("builder", {"git"})
    allowed = _run(
        gate(
            "mcp__darkfactory__sandbox_bash",
            {"argv": ["git", "status"]},
            _ctx(),
        )
    )
    assert isinstance(allowed, PermissionResultAllow)
    denied = _run(
        gate(
            "mcp__darkfactory__sandbox_bash",
            {"argv": ["curl", "evil.com"]},
            _ctx(),
        )
    )
    assert isinstance(denied, PermissionResultDeny)


def test_gh_pr_merge_command_denied_for_all_roles() -> None:
    for role in ("builder", "pr_creator"):
        gate = make_permission_gate(role, {"git", "gh"}, gate_approved=True)
        result = _run(
            gate(
                SANDBOX_BASH_TOOL,
                {"argv": ["gh", "pr", "merge", "12", "--squash"]},
                _ctx(),
            )
        )
        assert isinstance(result, PermissionResultDeny), role
        assert "denied for all agent roles" in result.message


def test_pr_creator_only_argv_prefixes() -> None:
    role_owned = {
        ("gh", "pr", "create"): frozenset({"pr_creator"}),
        ("gh", "pr", "list"): frozenset({"pr_creator"}),
        ("git", "push"): frozenset({"pr_creator"}),
    }
    for argv in (
        ["gh", "pr", "create", "--title", "T"],
        ["gh", "pr", "list", "--head", "agent/wf-1"],
        ["git", "push", "origin", "agent/wf-1"],
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

        allowed = _run(
            pr_gate(SANDBOX_BASH_TOOL, {"argv": argv}, _ctx())
        )
        denied = _run(
            worker_gate(SANDBOX_BASH_TOOL, {"argv": argv}, _ctx())
        )

        assert isinstance(allowed, PermissionResultAllow), argv
        assert isinstance(denied, PermissionResultDeny), argv
        assert "allowed only" in denied.message


def test_merge_tool_denied_even_with_gate() -> None:
    gate = make_permission_gate("pr_creator", {"git", "gh"}, gate_approved=True)
    for tool in MERGE_TOOLS:
        result = _run(gate(tool, {}, _ctx()))
        assert isinstance(result, PermissionResultDeny), tool
        assert "agents cannot merge" in result.message


def test_legacy_push_tool_allowed_for_pr_creator_with_gate() -> None:
    gate = make_permission_gate(
        "pr_creator", {"git", "gh"}, gate_approved=True
    )
    for tool in GATE_APPROVED_TOOLS:
        result = _run(gate(tool, {}, _ctx()))
        assert isinstance(result, PermissionResultAllow), tool


def test_legacy_push_tool_denied_for_non_pr_creator() -> None:
    gate = make_permission_gate("builder", {"git"}, gate_approved=True)
    for tool in GATE_APPROVED_TOOLS:
        result = _run(gate(tool, {}, _ctx()))
        assert isinstance(result, PermissionResultDeny), tool
        assert "allowed only for pr_creator" in result.message


def test_mcp_prefixed_merge_tool_matched() -> None:
    gate = make_permission_gate("pr_creator", {"git"}, gate_approved=False)
    result = _run(gate("mcp__darkfactory__gh_pr_merge", {}, _ctx()))
    assert isinstance(result, PermissionResultDeny)


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
        ["gh", "pr", "create", "--title", "T"],
        ["gh", "pr", "list", "--head", "agent/wf-1"],
        ["git", "push", "origin", "agent/wf-1"],
    ):
        result = _run(gate(SANDBOX_BASH_TOOL, {"argv": argv}, _ctx()))
        assert isinstance(result, PermissionResultAllow), argv


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

    argv = list(new_prefix) + ["bug"]
    pr_result = _run(pr_gate(SANDBOX_BASH_TOOL, {"argv": argv}, _ctx()))
    builder_result = _run(
        builder_gate(SANDBOX_BASH_TOOL, {"argv": argv}, _ctx())
    )
    assert isinstance(pr_result, PermissionResultAllow)
    # builder's gate never saw the prefix in its table; argv falls through
    # past the role-owned check and is allowed because `gh` is in its
    # allowlist. The point is that the builder gate is not silently widened
    # by another role's manifest.
    assert isinstance(builder_result, PermissionResultAllow)


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
    result = _run(
        gate(
            SANDBOX_BASH_TOOL,
            {"argv": ["gh", "pr", "merge", "1", "--squash"]},
            _ctx(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert "denied for all agent roles" in result.message
