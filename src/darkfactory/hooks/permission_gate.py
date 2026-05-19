"""``can_use_tool`` callback enforcing R5 (narrow tools) and R9 (HITL gate).

Per ARCHITECTURE.md §5.5, every SDK client wired with ``can_use_tool`` routes
its tool requests through this gate before the tool actually runs. The gate
guards the built-in ``Bash`` tool: the shell line in ``command`` is parsed
with ``shlex.split`` and run through the argv-check chain.

The gate denies argv containing shell metachars from
``tools/shell.py:FORBIDDEN_TOKENS``; denies globally forbidden argv
prefixes such as ``gh pr merge``; denies per-role manifest denylist
prefixes; denies role-owned argv prefixes such as ``git push`` outside
their owning role; or denies if ``argv[0]`` is not in the per-role argv
allowlist (an empty allowlist opts the role into a pure denylist
policy). A defensive deny also fires for any tool whose name matches the
merge-tool set. All other tool requests pass through.

The Claude Agent SDK's ``ToolPermissionContext`` does not carry the role or
the workflow state; instead, this module exposes ``make_permission_gate``
which closes over those values, mirroring the factory pattern already used
by the loop-breaker / call-cap / goal-pin hooks. ``_match_tool`` matches
either a bare tool name or an ``mcp__<server>__<tool>`` form so the merge
deny stays correct even if a project ``.mcp.json`` ever exposes such a
tool.
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

BUILTIN_BASH_TOOL = "Bash"
MERGE_TOOLS: frozenset[str] = frozenset({"gh_pr_merge"})

DENIED_ARGV_PREFIXES: tuple[tuple[str, ...], ...] = (("gh", "pr", "merge"),)


def _match_tool(tool_name: str, target: str) -> bool:
    return tool_name == target or tool_name.endswith(f"__{target}")


def _is_merge_tool(tool_name: str) -> bool:
    return any(_match_tool(tool_name, t) for t in MERGE_TOOLS)


def _argv_has_prefix(argv: list[str], prefix: tuple[str, ...]) -> bool:
    return len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix


def _format_prefix(prefix: tuple[str, ...]) -> str:
    return " ".join(prefix)


def _check_argv(
    role: str,
    argv: list[str],
    joined: str,
    allowlist: frozenset[str],
    denylist: tuple[tuple[str, ...], ...],
    effective_owned: Mapping[tuple[str, ...], frozenset[str]],
) -> PermissionResultAllow | PermissionResultDeny:
    """Run the argv-policy chain for the built-in Bash tool.

    Order is deny-first: ``FORBIDDEN_TOKENS`` → global ``DENIED_ARGV_PREFIXES``
    → per-role manifest denylist → role-owned prefixes (ownership table).
    Only after all denies have a chance to fire does the per-role allowlist
    decide ``argv[0]``; an empty allowlist disables that check so a role
    can opt into a pure denylist policy.
    """
    if not argv:
        return PermissionResultDeny(message="argv must be non-empty")
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
    for prefix in denylist:
        if _argv_has_prefix(argv, prefix):
            return PermissionResultDeny(
                message=(
                    f"command prefix {_format_prefix(prefix)!r} "
                    f"denied for role {role!r}"
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
    # Empty allowlist = "no allowlist check" — the role is opting into a
    # pure denylist policy (denies above + globals do the work).
    if allowlist and argv[0] not in allowlist:
        return PermissionResultDeny(
            message=f"argv[0]={argv[0]!r} not in {role} allowlist"
        )
    return PermissionResultAllow()


def make_permission_gate(
    role: str,
    argv_allowlist: Iterable[str],
    *,
    argv_denylist: Iterable[Sequence[str]] = (),
    gate_approved: bool = False,
    role_owned_argv_prefixes: Mapping[Sequence[str], Iterable[str]] | None = None,
):
    """Return a ``can_use_tool`` callback for one SDK client.

    The role and its argv allowlist are baked into the closure at client
    construction time. ``gate_approved`` is a deprecated no-op kept only
    for call/test signature compatibility — the legacy gate-approved MCP
    push tool it guarded was removed; PR publication is controlled by the
    role-owned ``git push`` / ``gh pr create`` argv prefixes instead.

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
    denylist: tuple[tuple[str, ...], ...] = tuple(
        tuple(prefix) for prefix in argv_denylist if prefix
    )
    effective_owned: dict[tuple[str, ...], frozenset[str]] = {
        tuple(prefix): frozenset(allowed_roles)
        for prefix, allowed_roles in (role_owned_argv_prefixes or {}).items()
    }

    async def permission_gate(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        if _match_tool(tool_name, BUILTIN_BASH_TOOL):
            command = tool_input.get("command")
            if not isinstance(command, str) or not command.strip():
                return PermissionResultDeny(
                    message="Bash blocked: empty or non-string command"
                )
            try:
                argv = shlex.split(command)
            except ValueError as exc:
                return PermissionResultDeny(
                    message=f"Bash blocked: unparseable command ({exc})"
                )
            # FORBIDDEN_TOKENS scan runs on the raw command so shell
            # metacharacters that shlex would silently swallow (quoted
            # ``;``, backticks, redirects in argv quoting) still trip the
            # deny. We don't normalise via shlex.join here.
            return _check_argv(
                role, argv, command, allowlist, denylist, effective_owned
            )

        if _is_merge_tool(tool_name):
            return PermissionResultDeny(
                message=f"{tool_name} blocked: agents cannot merge"
            )

        return PermissionResultAllow()

    return permission_gate
