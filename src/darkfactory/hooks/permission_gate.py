"""``can_use_tool`` callback enforcing R5 (narrow tools) and R9 (HITL gate).

Per ARCHITECTURE.md §5.5, every SDK client wired with ``can_use_tool`` routes
its tool requests through this gate before the tool actually runs:

* For ``sandbox_bash``: deny if any argv element contains a shell metachar
  from ``tools/shell.py:FORBIDDEN_TOKENS``, or if ``argv[0]`` is not in the
  per-role argv allowlist.
* For the destructive tools ``gh_pr_merge`` and ``git_push_agent_branch``:
  deny unless the workflow has flipped ``state.gate_approved`` to ``True``
  (the human approval step described in ARCHITECTURE.md §5.1 R9 row).
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
from typing import Any, Iterable

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from darkfactory.tools.shell import FORBIDDEN_TOKENS

SANDBOX_BASH_TOOL = "sandbox_bash"
MERGE_TOOLS: frozenset[str] = frozenset({"gh_pr_merge", "git_push_agent_branch"})


def _match_tool(tool_name: str, target: str) -> bool:
    return tool_name == target or tool_name.endswith(f"__{target}")


def _is_merge_tool(tool_name: str) -> bool:
    return any(_match_tool(tool_name, t) for t in MERGE_TOOLS)


def make_permission_gate(
    role: str,
    argv_allowlist: Iterable[str],
    *,
    gate_approved: bool = False,
):
    """Return a ``can_use_tool`` callback for one SDK client.

    The role and its argv allowlist are baked into the closure at client
    construction time; ``gate_approved`` reflects the workflow's view of
    ``state.gate_approved`` at the moment the activity invokes the SDK
    client. For pre-gate roles (backend, database, unit_test, code_quality)
    the destructive merge tools are never reachable from the system prompt,
    so leaving ``gate_approved=False`` is the correct default. For the
    ``pr_creator`` role the workflow only schedules its activity after the
    gate is approved, at which point callers pass ``gate_approved=True``.
    """
    allowlist = frozenset(argv_allowlist)

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
            if argv[0] not in allowlist:
                return PermissionResultDeny(
                    message=f"argv[0]={argv[0]!r} not in {role} allowlist"
                )
            return PermissionResultAllow()

        if _is_merge_tool(tool_name):
            if not gate_approved:
                return PermissionResultDeny(
                    message=f"{tool_name} blocked: merge gate not approved"
                )
            return PermissionResultAllow()

        return PermissionResultAllow()

    return permission_gate
