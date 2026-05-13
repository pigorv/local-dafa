"""In-process MCP server exposing the lone custom tool ``sandbox_bash``.

The only custom MCP tool that survives the SDK migration is ``sandbox_bash``.
Every other file/search/edit/git concern is handled by SDK built-ins
(``Read``/``Write``/``Edit``/``Grep``/``Glob``). Roles that need shell
access either allow the built-in ``Bash`` (e.g. builder) or route through
this tool when they want the per-task ``RepoSandbox``'s argv allowlist +
deny-list + timeout + stdout truncation chokepoint (e.g. tester, fixer,
pr_creator). The worker container itself is the isolation boundary;
there is no second inner container.

The tool body itself does *not* enforce the per-role argv allowlist or the
``FORBIDDEN_TOKENS`` deny-list — those checks live in
``hooks/permission_gate.py``. Here we only resolve the active
``RepoSandbox`` from ``tools/shell.py``'s registry and dispatch the call.
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from darkfactory.tools.sandbox import MAX_STDERR, MAX_STDOUT
from darkfactory.tools.shell import get_sandbox

DEFAULT_TIMEOUT_S = 120

SANDBOX_BASH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": (
                "argv vector. argv[0] is the binary; remaining elements are "
                "passed as separate arguments. No shell metacharacters."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 1,
            "maximum": 600,
            "default": DEFAULT_TIMEOUT_S,
            "description": "Max seconds before SIGKILL.",
        },
    },
    "required": ["argv"],
    "additionalProperties": False,
}


def build_mcp_server(
    task_id: str,
    *,
    name: str = "darkfactory",
    version: str = "1.0.0",
) -> McpSdkServerConfig:
    """Build the in-process MCP server for a given task.

    Holds a closure over ``task_id`` so ``sandbox_bash`` can locate the
    correct ``RepoSandbox`` from the per-task registry at call time. One
    server instance per SDK client; instances are not shared between roles
    because the registry binding is per-task.
    """

    @tool(
        "sandbox_bash",
        (
            "Execute argv inside the per-task RepoSandbox. "
            "Returns JSON: {returncode, stdout, stderr, timed_out}."
        ),
        SANDBOX_BASH_INPUT_SCHEMA,
    )
    async def sandbox_bash(args: dict[str, Any]) -> dict[str, Any]:
        argv = args.get("argv") or []
        timeout = int(args.get("timeout", DEFAULT_TIMEOUT_S))

        sb = get_sandbox(task_id)
        if sb is None:
            payload = {
                "returncode": -1,
                "stdout": "",
                "stderr": f"no sandbox registered for task_id={task_id!r}",
                "timed_out": False,
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        result = sb.exec(argv, timeout=timeout)
        result["stdout"] = (result.get("stdout") or "")[:MAX_STDOUT]
        result["stderr"] = (result.get("stderr") or "")[:MAX_STDERR]
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return create_sdk_mcp_server(name=name, version=version, tools=[sandbox_bash])
