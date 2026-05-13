"""Unit tests for the path_guard PreToolUse hook."""
from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import HookMatcher

from darkfactory.hooks.path_guard import is_path_allowed, make_path_guard
from darkfactory.llm_factory import build_options


def _pre(tool_name: str, **tool_input: Any) -> dict[str, Any]:
    return {
        "session_id": "test",
        "transcript_path": "/tmp/transcript",
        "cwd": "/workspace",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": f"{tool_name}-1",
    }


def _ctx() -> dict[str, Any]:
    return {"signal": None}


def _run(hook: Any, item: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(hook(item, item["tool_use_id"], _ctx()))


def test_edit_on_github_workflow_is_denied() -> None:
    hook = make_path_guard()

    out = _run(hook, _pre("Edit", file_path=".github/workflows/ci.yml"))

    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    assert "GitHub workflow" in spec["permissionDecisionReason"]


def test_workflow_path_denied_after_normalization() -> None:
    hook = make_path_guard()

    out = _run(hook, _pre("Edit", file_path="src/../.github/workflows/ci.yml"))

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_edit_on_ordinary_source_path_is_allowed() -> None:
    hook = make_path_guard()

    out = _run(hook, _pre("Edit", file_path="src/foo.py"))

    assert out == {}


def test_prefixed_write_tool_on_env_file_is_denied() -> None:
    hook = make_path_guard()

    out = _run(hook, _pre("mcp__sdk__Write", file_path="/workspace/.env.local"))

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "environment files" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_lockfile_denied_without_dependency_authorization() -> None:
    hook = make_path_guard()

    out = _run(hook, _pre("Write", file_path="package-lock.json"))

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "dependency changes" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_lockfile_allowed_with_dependency_authorization() -> None:
    hook = make_path_guard({"dependency_changes": True})

    out = _run(hook, _pre("Write", file_path="package-lock.json"))

    assert out == {}


def test_contract_changes_can_authorize_lockfile() -> None:
    state = {
        "implementation_brief": {
            "contract_changes": {
                "dependency_changes": {"allowed": True},
            }
        }
    }

    assert is_path_allowed("uv.lock", state)


def test_path_guard_is_registered_for_edit_capable_sdk_options() -> None:
    options = build_options(
        "builder",
        model="claude-sonnet-4-5-20250929",
        thinking=False,
        system_prompt="x",
        allowed_tools=["Read", "Edit"],
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[]),
            ]
        },
    )

    pre_hooks = list(options.hooks["PreToolUse"][0].hooks)
    assert pre_hooks[0].__name__ == "path_guard_hook"


def test_path_guard_is_not_registered_for_read_only_sdk_options() -> None:
    options = build_options(
        "pr_creator",
        model="claude-haiku-4-5-20251001",
        thinking=False,
        system_prompt="x",
        allowed_tools=["Read", "Grep", "Glob"],
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[]),
            ]
        },
    )

    assert list(options.hooks["PreToolUse"][0].hooks) == []
