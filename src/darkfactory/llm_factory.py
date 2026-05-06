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
from opentelemetry import trace as _otel_trace

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

# Built-in CLI tools we never want a role to invoke. The Claude Code CLI's
# permission UI runs *before* `can_use_tool` for tools not in `allowed_tools`,
# and in non-interactive worker containers that UI hangs (telemetry shows up
# as `claude_code.tool.blocked_on_user`). Listing the tool here causes the
# SDK to reject the call without the UI roundtrip. `Bash` is the standard
# escape hatch the model otherwise reaches for; `ToolSearch` is the deferred-
# tool discovery mechanism we don't use (all our MCP tools are exposed
# eagerly via `mcp_servers=`).
_DISALLOWED_BUILTIN_TOOLS: tuple[str, ...] = ("Bash", "ToolSearch")

CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]


def _env(role: str, key: str) -> str | None:
    return os.getenv(f"LLM_{role.upper()}_{key}")


def _otel_resource_attributes_with_parent_span() -> str | None:
    """Augment OTEL_RESOURCE_ATTRIBUTES with the active span's id as
    `darkfactory.cli_parent_span_id` (16-char hex).

    The bundled `claude` CLI emits most of its native spans
    (`claude_code.tool`, `claude_code.llm_request`) as roots — TRACEPARENT
    only adopts the *first* span (`claude_code.interaction`) it creates.
    Stamping the active Python span_id as a resource attribute lets the
    otel-collector's `transform/coalesce_trace_id` set `parent_span_id` on
    those orphan top-level CLI spans so they nest under whatever Python span
    spawned the SDK subprocess (LangGraph node for build roles, the activity
    span for non-LangGraph stages).

    Returns None when there's no active OTel span; in that case we don't
    override the env and the subprocess inherits whatever the parent process
    has.
    """
    span = _otel_trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid or ctx.span_id == 0:
        return None
    parent_span_hex = format(ctx.span_id, "016x")
    existing = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    parts = [existing] if existing else []
    parts.append(f"darkfactory.cli_parent_span_id={parent_span_hex}")
    return ",".join(parts)


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

    # W3C TRACEPARENT/TRACESTATE injection is handled by the SDK itself in
    # claude_agent_sdk._internal.transport.subprocess_cli.connect(); it reads
    # the active OTel context at subprocess spawn and writes the env vars the
    # CLI extracts to parent claude_code.interaction under the active span.
    #
    # We additionally pass `darkfactory.cli_parent_span_id` as a resource
    # attribute so the otel-collector can rewrite `parent_span_id` on orphan
    # top-level claude_code.* spans (most of them — the CLI emits llm_request
    # and tool as roots, TRACEPARENT adopts only the first span).
    sdk_env: dict[str, str] = {}
    augmented_attrs = _otel_resource_attributes_with_parent_span()
    if augmented_attrs is not None:
        sdk_env["OTEL_RESOURCE_ATTRIBUTES"] = augmented_attrs

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=list(_DISALLOWED_BUILTIN_TOOLS),
        mcp_servers=mcp_servers or {},
        can_use_tool=can_use_tool,
        hooks=hooks,
        cwd=cwd,
        setting_sources=[],
        thinking=thinking_cfg,
        env=sdk_env,
        permission_mode="bypassPermissions",
    )
    options.temperature = temperature
    return options
