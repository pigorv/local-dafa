"""``can_use_tool`` callback enforcing R5 (narrow tools) and R9 (HITL gate).

Per ARCHITECTURE.md §5.5, every SDK client wired with ``can_use_tool`` routes
its tool requests through this gate before the tool actually runs:

* For ``sandbox_bash``: deny if any argv element contains a shell metachar
  from ``tools/shell.py:FORBIDDEN_TOKENS``; deny globally forbidden argv
  prefixes such as ``gh pr merge``; deny role-owned argv prefixes such as
  ``git push`` outside their owning role; or deny if ``argv[0]`` is not in
  the per-role argv allowlist.
* For the destructive legacy tools, deny merge outright and allow branch
  push only for the PR Creator role when a caller explicitly marks that legacy
  tool path as gate-approved.
* All other tool requests pass through.

The Claude Agent SDK's ``ToolPermissionContext`` does not carry the role or
the workflow state; instead, this module exposes ``make_permission_gate``
which closes over those values, mirroring the factory pattern already used
by the loop-breaker / call-cap / goal-pin hooks. Tool names arriving from
MCP servers are prefixed (``mcp__<server>__<tool>``); the helpers here
match either the bare or the prefixed form so the gate stays correct
regardless of how a particular role wires up its MCP server.
"""
from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from typing import Any, Iterable

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from darkfactory.tools.shell import FORBIDDEN_TOKENS

SANDBOX_BASH_TOOL = "sandbox_bash"
PR_CREATOR_ROLE = "pr_creator"
MERGE_TOOLS: frozenset[str] = frozenset({"gh_pr_merge"})
GATE_APPROVED_TOOLS: frozenset[str] = frozenset({"git_push_agent_branch"})

DENIED_ARGV_PREFIXES: tuple[tuple[str, ...], ...] = (("gh", "pr", "merge"),)


def _match_tool(tool_name: str, target: str) -> bool:
    return tool_name == target or tool_name.endswith(f"__{target}")


def _is_merge_tool(tool_name: str) -> bool:
    return any(_match_tool(tool_name, t) for t in MERGE_TOOLS)


def _is_gate_approved_tool(tool_name: str) -> bool:
    return any(_match_tool(tool_name, t) for t in GATE_APPROVED_TOOLS)


def _argv_has_prefix(argv: list[str], prefix: tuple[str, ...]) -> bool:
    return len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix


def _format_prefix(prefix: tuple[str, ...]) -> str:
    return " ".join(prefix)


def make_permission_gate(
    role: str,
    argv_allowlist: Iterable[str],
    *,
    gate_approved: bool = False,
    role_owned_argv_prefixes: Mapping[Sequence[str], Iterable[str]] | None = None,
):
    """Return a ``can_use_tool`` callback for one SDK client.

    The role and its argv allowlist are baked into the closure at client
    construction time. ``gate_approved`` is retained for legacy MCP tools that
    mutate branches directly; modern PR Creator flows use ``sandbox_bash`` and
    are controlled by role-owned argv prefixes instead.

    ``role_owned_argv_prefixes`` is the registry-derived aggregation of every
    manifest's ``tools.role_owned_argv_prefixes`` — a mapping from each
    role-owned prefix to the set of roles permitted to invoke it. The
    composer builds it from the registry; tests construct it directly. A
    prefix present in this mapping is denied for any role not listed.
    ``DENIED_ARGV_PREFIXES`` is checked *before* this table, so a manifest
    can never widen the global denylist; registry-load-time validation
    refuses such manifests up front, and this defense-in-depth check keeps
    the gate honest if that validation is ever bypassed.
    """
    allowlist = frozenset(argv_allowlist)
    effective_owned: dict[tuple[str, ...], frozenset[str]] = {
        tuple(prefix): frozenset(allowed_roles)
        for prefix, allowed_roles in (role_owned_argv_prefixes or {}).items()
    }

    async def permission_gate(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        if _match_tool(tool_name, SANDBOX_BASH_TOOL):
            argv = list(tool_input.get("argv") or [])
            if not argv:
                return PermissionResultDeny(message="argv must be non-empty")
            joined = shlex.join(argv)
            for tok in FORBIDDEN_TOKENS:
                if tok in joined:
                    return PermissionResultDeny(
                        message=f"forbidden token {tok!r} in argv"
                    )
            for prefix in DENIED_ARGV_PREFIXES:
                if _argv_has_prefix(argv, prefix):
                    return PermissionResultDeny(
                        message=(
                            f"command prefix {_format_prefix(prefix)!r} "
                            "denied for all agent roles"
                        )
                    )
            for prefix, allowed_roles in effective_owned.items():
                if _argv_has_prefix(argv, prefix) and role not in allowed_roles:
                    roles = ", ".join(sorted(allowed_roles))
                    return PermissionResultDeny(
                        message=(
                            f"command prefix {_format_prefix(prefix)!r} "
                            f"allowed only for role(s): {roles}"
                        )
                    )
            if argv[0] not in allowlist:
                return PermissionResultDeny(
                    message=f"argv[0]={argv[0]!r} not in {role} allowlist"
                )
            return PermissionResultAllow()

        if _is_merge_tool(tool_name):
            return PermissionResultDeny(
                message=f"{tool_name} blocked: agents cannot merge"
            )

        if _is_gate_approved_tool(tool_name):
            if role != PR_CREATOR_ROLE:
                return PermissionResultDeny(
                    message=(
                        f"{tool_name} blocked: allowed only for "
                        f"{PR_CREATOR_ROLE}"
                    )
                )
            if not gate_approved:
                return PermissionResultDeny(
                    message=f"{tool_name} blocked: merge gate not approved"
                )
            return PermissionResultAllow()

        return PermissionResultAllow()

    return permission_gate
