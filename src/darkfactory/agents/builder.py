"""Builder Worker — single generalist build-stage role.

Implements one Work Package end-to-end (Java sources, Flyway/SQL migrations,
fixtures — whatever the WP requires). Tests are the Tester's job; this role
does not edit anything under ``src/test/...``.

All file ops route through SDK built-ins (``Read`` / ``Write`` / ``Edit``
/ ``Grep`` / ``Glob``); shell commands route through the lone custom MCP
tool ``sandbox_bash``. Built-in ``Bash`` is deliberately omitted from
``allowed_tools`` so process execution always goes through the per-task
``RepoSandbox`` and the ``permission_gate`` argv allowlist.
"""
from __future__ import annotations

from darkfactory.agents._sdk_common import (
    WorkerOutput,
    run_to_completion,
    worker_user_message,
)
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.state import Patch

ROLE = "builder"


async def run_builder(state_slice: dict) -> WorkerOutput:
    sink: list[Patch] = []
    compose_state = ComposeState.from_mapping(state_slice, patches_sink=sink)
    summary_text = ""
    async with compose(
        ROLE,
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(worker_user_message(state_slice))
        result = await run_to_completion(client)
        if isinstance(result, dict):
            summary_text = result.get("text", "") or ""
    return WorkerOutput(patches=list(sink), summary=summary_text)
