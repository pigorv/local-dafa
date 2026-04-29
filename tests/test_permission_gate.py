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
    gate = make_permission_gate("backend", {"git", "ls"})
    result = _run(
        gate(SANDBOX_BASH_TOOL, {"argv": ["git", "status"]}, _ctx())
    )
    assert isinstance(result, PermissionResultAllow)


def test_argv_outside_allowlist_denied() -> None:
    gate = make_permission_gate("backend", {"git"})
    result = _run(
        gate(SANDBOX_BASH_TOOL, {"argv": ["rm", "-rf", "/"]}, _ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert "backend allowlist" in result.message
    assert "'rm'" in result.message


def test_forbidden_token_denied() -> None:
    gate = make_permission_gate("backend", {"git"})
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
    gate = make_permission_gate("backend", {"git"})
    result = _run(gate(SANDBOX_BASH_TOOL, {"argv": []}, _ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert "non-empty" in result.message


def test_mcp_prefixed_sandbox_bash_name_matched() -> None:
    """The MCP transport prefixes tool names as ``mcp__<server>__<tool>``."""
    gate = make_permission_gate("backend", {"git"})
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


def test_merge_tool_denied_without_gate() -> None:
    gate = make_permission_gate("pr_creator", {"git", "gh"})
    for tool in MERGE_TOOLS:
        result = _run(gate(tool, {}, _ctx()))
        assert isinstance(result, PermissionResultDeny), tool
        assert "merge gate not approved" in result.message


def test_merge_tool_allowed_with_gate() -> None:
    gate = make_permission_gate(
        "pr_creator", {"git", "gh"}, gate_approved=True
    )
    for tool in MERGE_TOOLS:
        result = _run(gate(tool, {}, _ctx()))
        assert isinstance(result, PermissionResultAllow), tool


def test_mcp_prefixed_merge_tool_matched() -> None:
    gate = make_permission_gate("pr_creator", {"git"}, gate_approved=False)
    result = _run(gate("mcp__darkfactory__gh_pr_merge", {}, _ctx()))
    assert isinstance(result, PermissionResultDeny)


def test_unrelated_tool_passes() -> None:
    gate = make_permission_gate("backend", {"git"})
    for name in ("Read", "Edit", "Grep", "Glob", "Write"):
        result = _run(gate(name, {"path": "src/main.py"}, _ctx()))
        assert isinstance(result, PermissionResultAllow), name
