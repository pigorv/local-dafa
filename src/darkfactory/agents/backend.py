"""Backend Worker — SDK-driven build-stage role.

Reads a SpecSlice describing one Java change, edits sources under
``src/main/java``, runs ``mvn -q compile`` via ``sandbox_bash`` to catch
type errors, and commits via git. All file ops go through SDK built-ins
(``Read`` / ``Write`` / ``Edit`` / ``Grep`` / ``Glob``); every shell command
goes through the lone custom MCP tool ``sandbox_bash``. Built-in ``Bash`` is
deliberately omitted from ``allowed_tools`` so process execution always
routes through the per-task ``RepoSandbox`` and the ``permission_gate``
argv allowlist.
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
from darkfactory.hooks.permission_gate import make_permission_gate
from darkfactory.hooks.prompt_injection_guard import make_prompt_injection_guard
from darkfactory.llm_factory import build_options
from darkfactory.state import Patch
from darkfactory.tools.server import build_mcp_server

ROLE = "backend"

# argv[0] allowlist enforced by permission_gate; per ARCHITECTURE.md §5.5.
BACKEND_ALLOWLIST: frozenset[str] = frozenset(
    {"mvn", "gradle", "./gradlew", "git", "cat", "ls"}
)

ALLOWED_TOOLS: list[str] = ["Read", "Write", "Edit", "Grep", "Glob", "sandbox_bash"]


def make_backend_client(
    state_slice: dict,
    *,
    patches_sink: list[Patch] | None = None,
) -> ClaudeSDKClient:
    """Build an SDK client for the Backend Worker.

    ``patches_sink`` is the list the ``diff_capture`` PostToolUse hook
    appends to whenever the worker uses ``Edit`` or ``Write``. Callers
    typically pass an empty list and read it back after the SDK loop ends;
    when omitted, an internal sink is created and stashed on
    ``client.options.patches_sink`` so ``run_backend`` can recover it.
    """
    if patches_sink is None:
        patches_sink = []

    user_request = state_slice.get("user_request", "") or ""
    task_id = state_slice.get("task_id", "") or ""
    slice_id = state_slice.get("current_slice", "") or ""

    options = build_options(
        ROLE,
        system_prompt=load_prompt(ROLE),
        allowed_tools=ALLOWED_TOOLS,
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[make_loop_breaker(), make_call_cap()]),
            ],
            "PostToolUse": [
                HookMatcher(
                    hooks=[
                        make_diff_capture(ROLE, slice_id, task_id, patches_sink),
                        make_prompt_injection_guard(),
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
        can_use_tool=make_permission_gate(ROLE, BACKEND_ALLOWLIST),
    )
    options.patches_sink = patches_sink
    return ClaudeSDKClient(options=options)


async def run_backend(state_slice: dict) -> WorkerOutput:
    sink: list[Patch] = []
    summary_text = ""
    async with make_backend_client(state_slice, patches_sink=sink) as client:
        await client.query(worker_user_message(state_slice))
        result = await run_to_completion(client)
        if isinstance(result, dict):
            summary_text = result.get("text", "") or ""
    return WorkerOutput(patches=list(sink), summary=summary_text)
