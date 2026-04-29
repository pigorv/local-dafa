from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ThinkingConfigDisabled,
    ThinkingConfigEnabled,
    ToolPermissionContext,
)

Role = Literal[
    "po",
    "architect",
    "spec_reviewer",
    "backend",
    "database",
    "unit_test",
    "spec_adjustment",
    "code_quality",
    "pr_creator",
]


@dataclass(frozen=True)
class _RoleDefaults:
    model: str
    temperature: float
    thinking: bool


# Per-role defaults from ARCHITECTURE.md §9. Builder Supervisor has no LLM
# (pure topo-sort) and is intentionally absent. Frontend is a no-op stub
# (see ARCHITECTURE.md §10) so it does not call this builder either.
_ROLE_DEFAULTS: dict[str, _RoleDefaults] = {
    "po": _RoleDefaults("claude-haiku-4-5-20251001", 0.3, False),
    "architect": _RoleDefaults("claude-sonnet-4-5-20250929", 0.3, False),
    "spec_reviewer": _RoleDefaults("claude-sonnet-4-5-20250929", 0.3, False),
    "backend": _RoleDefaults("claude-sonnet-4-5-20250929", 0.1, False),
    "database": _RoleDefaults("claude-sonnet-4-5-20250929", 0.1, False),
    "unit_test": _RoleDefaults("claude-sonnet-4-5-20250929", 0.1, False),
    "spec_adjustment": _RoleDefaults("claude-sonnet-4-5-20250929", 0.5, True),
    "code_quality": _RoleDefaults("claude-haiku-4-5-20251001", 0.2, False),
    "pr_creator": _RoleDefaults("claude-haiku-4-5-20251001", 0.1, False),
}

_THINKING_BUDGET_TOKENS = 4096

CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]


def _env(role: str, key: str) -> str | None:
    return os.getenv(f"LLM_{role.upper()}_{key}")


def build_options(
    role: str,
    *,
    system_prompt: str,
    allowed_tools: list[str],
    hooks: dict[str, Any],
    mcp_servers: dict[str, Any] | None = None,
    can_use_tool: CanUseTool | None = None,
    cwd: str = "/workspace",
) -> ClaudeAgentOptions:
    """Per-role ClaudeAgentOptions with env overrides.

    Resolves model, temperature, and thinking from `LLM_<ROLE>_<KEY>` env vars,
    falling back to per-role defaults from ARCHITECTURE.md §9. Always sets
    `setting_sources=[]` for hermetic runs (per ARCHITECTURE.md §15.4).

    Temperature is attached to the returned options as a `temperature`
    attribute. The Claude Agent SDK does not expose a temperature parameter
    on `ClaudeAgentOptions`, but ARCHITECTURE.md §9 treats it as a per-role
    knob; keep it accessible so callers and tests can inspect or forward it.
    """
    if role not in _ROLE_DEFAULTS:
        raise ValueError(
            f"Unknown role {role!r}. Known roles: {sorted(_ROLE_DEFAULTS)}"
        )
    defaults = _ROLE_DEFAULTS[role]

    model = _env(role, "MODEL") or defaults.model
    raw_temp = _env(role, "TEMPERATURE")
    temperature = float(raw_temp) if raw_temp is not None else defaults.temperature
    raw_thinking = _env(role, "THINKING")
    thinking_on = (
        raw_thinking.lower() in ("on", "true", "1", "yes")
        if raw_thinking is not None
        else defaults.thinking
    )
    thinking_cfg = (
        ThinkingConfigEnabled(type="enabled", budget_tokens=_THINKING_BUDGET_TOKENS)
        if thinking_on
        else ThinkingConfigDisabled(type="disabled")
    )

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        mcp_servers=mcp_servers or {},
        can_use_tool=can_use_tool,
        hooks=hooks,
        cwd=cwd,
        setting_sources=[],
        thinking=thinking_cfg,
    )
    options.temperature = temperature
    return options
