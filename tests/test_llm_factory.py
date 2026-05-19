from __future__ import annotations

import pytest

from claude_agent_sdk import ClaudeAgentOptions

from darkfactory.llm_factory import build_options


_BASE_KWARGS = dict(
    model="claude-sonnet-4-5-20250929",
    thinking=False,
    system_prompt="you are a test agent",
    allowed_tools=["Read"],
    hooks={},
    mcp_servers={},
    can_use_tool=None,
)


def test_build_options_passes_through_caller_supplied_defaults() -> None:
    opts = build_options("po", **_BASE_KWARGS)
    assert isinstance(opts, ClaudeAgentOptions)
    assert opts.model == "claude-sonnet-4-5-20250929"
    assert opts.setting_sources == ["project"]
    # ``skills`` is None unless the caller (compose) passes it explicitly.
    # The SDK loads no project skills without it.
    assert opts.skills is None
    assert opts.thinking is not None and opts.thinking["type"] == "disabled"


def test_build_options_passes_skills_through() -> None:
    opts_all = build_options("po", skills="all", **_BASE_KWARGS)
    assert opts_all.skills == "all"
    opts_list = build_options("po", skills=["pdf", "docx"], **_BASE_KWARGS)
    assert opts_list.skills == ["pdf", "docx"]


def test_build_options_thinking_enabled_writes_budget() -> None:
    opts = build_options(
        "architect",
        **{**_BASE_KWARGS, "thinking": True, "thinking_budget_tokens": 8192},
    )
    assert opts.thinking is not None
    assert opts.thinking["type"] == "enabled"
    assert opts.thinking["budget_tokens"] == 8192


def test_build_options_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BUILDER_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("LLM_BUILDER_THINKING", "on")
    opts = build_options("builder", **_BASE_KWARGS)
    assert opts.model == "claude-haiku-4-5-20251001"
    assert opts.thinking is not None and opts.thinking["type"] == "enabled"


def test_build_options_default_cwd_is_workspace() -> None:
    opts = build_options("po", **_BASE_KWARGS)
    assert opts.cwd == "/workspace"


def test_build_options_disables_interactive_permission_ui() -> None:
    """In non-interactive worker containers the CLI's permission UI hangs
    (`claude_code.tool.blocked_on_user`). `bypassPermissions` skips that
    UI; `can_use_tool` (the per-role permission gate) is still consulted.
    """
    opts = build_options("po", **_BASE_KWARGS)
    assert opts.permission_mode == "bypassPermissions"


def test_build_options_leaves_disallowed_tools_empty() -> None:
    """Built-in ``Bash`` is intentionally available to build/test/fixer
    roles via their ``allowed_tools``; the global disallow list is empty so
    per-role tool selection is authoritative.
    """
    opts = build_options("po", **_BASE_KWARGS)
    assert opts.disallowed_tools == []


def test_build_options_force_path_guard_installs_with_empty_allowlist() -> None:
    """``force_path_guard=True`` installs the guard even when no Edit/Write
    tool is named — the ``allowed: "all"`` (pure-yolo) case where the
    resolved allowlist is empty but Edit/Write are still reachable.
    """
    kwargs = {**_BASE_KWARGS, "allowed_tools": []}
    opts = build_options("builder", force_path_guard=True, **kwargs)

    pre_hooks = list(opts.hooks["PreToolUse"][0].hooks)
    assert pre_hooks[0].__name__ == "path_guard_hook"

    # Without the flag an empty allowlist installs no guard.
    opts_off = build_options("builder", **kwargs)
    assert not (opts_off.hooks or {}).get("PreToolUse")
