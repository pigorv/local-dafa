from __future__ import annotations

import pytest

from claude_agent_sdk import ClaudeAgentOptions

from darkfactory.llm_factory import _ROLE_DEFAULTS, build_options


_BASE_KWARGS = dict(
    system_prompt="you are a test agent",
    allowed_tools=["Read"],
    hooks={},
    mcp_servers={},
    can_use_tool=None,
)


@pytest.mark.parametrize("role", sorted(_ROLE_DEFAULTS))
def test_build_options_per_role_defaults(role: str) -> None:
    opts = build_options(role, **_BASE_KWARGS)
    defaults = _ROLE_DEFAULTS[role]
    assert isinstance(opts, ClaudeAgentOptions)
    assert opts.model == defaults.model
    assert opts.temperature == defaults.temperature
    assert opts.setting_sources == []
    expected_thinking = "enabled" if defaults.thinking else "disabled"
    assert opts.thinking is not None and opts.thinking["type"] == expected_thinking


def test_build_options_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BACKEND_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("LLM_BACKEND_TEMPERATURE", "0.7")
    monkeypatch.setenv("LLM_BACKEND_THINKING", "on")
    opts = build_options("backend", **_BASE_KWARGS)
    assert opts.model == "claude-haiku-4-5-20251001"
    assert opts.temperature == 0.7
    assert opts.thinking is not None and opts.thinking["type"] == "enabled"


def test_build_options_unknown_role_raises() -> None:
    with pytest.raises(ValueError, match="Unknown role"):
        build_options("builder_supervisor", **_BASE_KWARGS)


def test_build_options_default_cwd_is_workspace() -> None:
    opts = build_options("po", **_BASE_KWARGS)
    assert opts.cwd == "/workspace"
