"""Database Worker — SDK-driven build-stage role.

Reads a SpecSlice describing one schema change, writes a new Flyway
``V{n}__{slug}.sql`` migration under ``src/main/resources/db/migration/``
(and optionally edits JPA entities / repositories), runs
``mvn -q compile`` via ``sandbox_bash`` to catch entity / migration drift,
and commits. Built-in ``Bash`` is deliberately omitted from
``allowed_tools``; every shell command routes through ``sandbox_bash`` and
the ``permission_gate`` argv allowlist.
"""
from __future__ import annotations

from claude_agent_sdk import ClaudeSDKClient, HookMatcher

from darkfactory.agents._sdk_common import (
    WorkerOutput,
    load_prompt,
    run_to_completion,
    worker_user_message,
)
from darkfactory.hooks.call_cap import make_call_cap
from darkfactory.hooks.diff_capture import make_diff_capture
from darkfactory.hooks.goal_pin import make_goal_pin
from darkfactory.hooks.heartbeat import make_heartbeat
from darkfactory.hooks.loop_breaker import make_loop_breaker
from darkfactory.hooks.otel_emit import make_otel_emit
from darkfactory.hooks.permission_gate import make_permission_gate
from darkfactory.hooks.prompt_injection_guard import make_prompt_injection_guard
from darkfactory.llm_factory import build_options
from darkfactory.state import Patch
from darkfactory.tools.server import build_mcp_server

ROLE = "database"

# argv[0] allowlist enforced by permission_gate; per ARCHITECTURE.md §5.5.
DATABASE_ALLOWLIST: frozenset[str] = frozenset(
    {"mvn", "gradle", "./gradlew", "git", "cat", "ls"}
)

ALLOWED_TOOLS: list[str] = ["Read", "Write", "Edit", "Grep", "Glob", "sandbox_bash"]


def make_database_client(
    state_slice: dict,
    *,
    patches_sink: list[Patch] | None = None,
) -> ClaudeSDKClient:
    """Build an SDK client for the Database Worker.

    ``patches_sink`` mirrors the backend pattern: the ``diff_capture`` hook
    appends to it on every ``Edit`` / ``Write`` (including new migration
    files), and ``run_database`` reads it back after the SDK loop.
    """
    if patches_sink is None:
        patches_sink = []

    user_request = state_slice.get("user_request", "") or ""
    task_id = state_slice.get("task_id", "") or ""
    slice_id = state_slice.get("current_slice", "") or ""

    otel_pre, otel_post = make_otel_emit(ROLE)
    options = build_options(
        ROLE,
        system_prompt=load_prompt(ROLE),
        allowed_tools=ALLOWED_TOOLS,
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[make_loop_breaker(), make_call_cap(), otel_pre]),
            ],
            "PostToolUse": [
                HookMatcher(
                    hooks=[
                        make_diff_capture(ROLE, slice_id, task_id, patches_sink),
                        make_prompt_injection_guard(),
                        otel_post,
                    ]
                ),
            ],
            "UserPromptSubmit": [
                HookMatcher(hooks=[make_goal_pin(user_request)]),
            ],
            "Stop": [
                HookMatcher(hooks=[make_heartbeat(f"{ROLE}: turn boundary")]),
            ],
        },
        mcp_servers={"darkfactory": build_mcp_server(task_id)},
        can_use_tool=make_permission_gate(ROLE, DATABASE_ALLOWLIST),
    )
    options.patches_sink = patches_sink
    return ClaudeSDKClient(options=options)


async def run_database(state_slice: dict) -> WorkerOutput:
    sink: list[Patch] = []
    summary_text = ""
    async with make_database_client(state_slice, patches_sink=sink) as client:
        await client.query(worker_user_message(state_slice))
        result = await run_to_completion(client)
        if isinstance(result, dict):
            summary_text = result.get("text", "") or ""
    return WorkerOutput(patches=list(sink), summary=summary_text)
