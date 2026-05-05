"""Unit Test Worker — SDK-driven build-stage role.

Reads a SpecSlice describing the change that needs test coverage, finds an
existing test file via ``Grep`` / ``Glob`` to learn the repo's conventions,
writes JUnit 5 tests under ``src/test/java/...``, runs ``mvn -q test`` via
``sandbox_bash`` to confirm the new tests execute, and commits. Pass or fail
of the test run is fine here — the Verify stage decides whether the build
is green. Built-in ``Bash`` is deliberately omitted from ``allowed_tools``;
every shell command routes through ``sandbox_bash`` and the
``permission_gate`` argv allowlist.
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

ROLE = "unit_test"

# argv[0] allowlist enforced by permission_gate; per ARCHITECTURE.md §5.5.
UNIT_TEST_ALLOWLIST: frozenset[str] = frozenset(
    {"mvn", "gradle", "./gradlew", "git", "cat", "ls"}
)

ALLOWED_TOOLS: list[str] = ["Read", "Write", "Edit", "Grep", "Glob", "sandbox_bash"]


def make_unit_test_client(
    state_slice: dict,
    *,
    patches_sink: list[Patch] | None = None,
) -> ClaudeSDKClient:
    """Build an SDK client for the Unit Test Worker.

    ``patches_sink`` is the list the ``diff_capture`` hook appends to on
    every ``Edit`` / ``Write`` of a test file; ``run_unit_test`` reads it
    back after the SDK loop ends.
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
        can_use_tool=make_permission_gate(ROLE, UNIT_TEST_ALLOWLIST),
    )
    options.patches_sink = patches_sink
    return ClaudeSDKClient(options=options)


async def run_unit_test(state_slice: dict) -> WorkerOutput:
    sink: list[Patch] = []
    summary_text = ""
    async with make_unit_test_client(state_slice, patches_sink=sink) as client:
        await client.query(worker_user_message(state_slice))
        result = await run_to_completion(client)
        if isinstance(result, dict):
            summary_text = result.get("text", "") or ""
    return WorkerOutput(patches=list(sink), summary=summary_text)
