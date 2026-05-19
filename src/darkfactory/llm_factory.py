from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Awaitable, Callable, Literal

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    SystemPromptPreset,
    ThinkingConfigDisabled,
    ThinkingConfigEnabled,
    ToolPermissionContext,
)
from opentelemetry import trace as _otel_trace

from darkfactory.hooks.path_guard import make_path_guard

_DEFAULT_THINKING_BUDGET_TOKENS = 4096

# Built-in CLI tools the SDK should never invoke autonomously. Empty by
# default: shell-using roles get the built-in ``Bash`` tool in their
# allowed_tools, argv-gated by ``hooks/permission_gate.py`` (per-role
# allowlist/denylist). Permission mode is still ``bypassPermissions``,
# so the SDK won't prompt the (non-interactive) CLI UI for any tool
# already listed in a role's ``allowed_tools``.
_DISALLOWED_BUILTIN_TOOLS: tuple[str, ...] = ()
_FILE_MUTATION_TOOLS: frozenset[str] = frozenset({"Edit", "Write"})

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
    # The bundled `claude` CLI subprocess does not speak Temporal, so its
    # native claude_code.* spans have no `temporalRunID`. Pass the active
    # run id as a resource attribute so the otel-collector keys them into
    # the same per-run trace as the Python-side spans (one trace per run,
    # not per workflow id — critical when re-running the same wf id while
    # testing).
    try:
        from temporalio import activity as _activity

        if _activity.in_activity():
            run_id = _activity.info().workflow_run_id
            if run_id:
                parts.append(f"darkfactory.workflow_run_id={run_id}")
    except Exception:
        pass
    return ",".join(parts)


def _uses_file_mutation_tools(allowed_tools: list[str]) -> bool:
    return any(tool in _FILE_MUTATION_TOOLS for tool in allowed_tools)


def _with_path_guard(
    hooks: dict[str, Any],
    allowed_tools: list[str],
    state: Mapping[str, Any] | None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Attach the path guard to edit-capable SDK clients.

    ``force=True`` installs the guard even when ``allowed_tools`` does not
    name Edit/Write — used for ``allowed: "all"`` (pure-yolo) roles, whose
    resolved ``allowed_tools`` is empty yet still reach Edit/Write under
    ``bypassPermissions``.
    """
    out = {event: list(matchers) for event, matchers in hooks.items()}
    if not (force or _uses_file_mutation_tools(allowed_tools)):
        return out

    guard = make_path_guard(state)
    pre_tool = list(out.get("PreToolUse") or [])
    if not pre_tool:
        out["PreToolUse"] = [HookMatcher(hooks=[guard])]
        return out

    first = pre_tool[0]
    pre_tool[0] = HookMatcher(
        matcher=first.matcher,
        hooks=[guard, *list(first.hooks)],
        timeout=first.timeout,
    )
    out["PreToolUse"] = pre_tool
    return out


def build_options(
    role: str,
    *,
    model: str,
    thinking: bool,
    thinking_budget_tokens: int = _DEFAULT_THINKING_BUDGET_TOKENS,
    system_prompt: str | None,
    allowed_tools: list[str],
    hooks: dict[str, Any],
    mcp_servers: dict[str, Any] | None = None,
    can_use_tool: CanUseTool | None = None,
    path_guard_state: Mapping[str, Any] | None = None,
    cwd: str = "/workspace",
    output_format: dict[str, Any] | None = None,
    skills: Literal["all"] | list[str] | None = None,
    force_path_guard: bool = False,
) -> ClaudeAgentOptions:
    """Per-role ClaudeAgentOptions with env overrides and path-guard wiring.

    Caller supplies the manifest-derived ``model``/``thinking`` knobs
    explicitly; this function only layers ``LLM_<ROLE>_<KEY>`` env-var
    overrides on top, attaches the path guard for edit-capable roles, and
    stamps the OTel resource attributes the otel-collector needs to coalesce
    orphan ``claude_code.*`` spans. Always sets
    ``setting_sources=["project"]`` so the target repo's ``CLAUDE.md``,
    ``.claude/skills/``, and ``.claude/settings.json`` (rooted at ``cwd``)
    are loaded into every spawned session; host-level ``~/.claude/`` is
    intentionally excluded so the worker container stays hermetic.
    ``skills`` controls which discovered project skills are actually
    enabled — ``"all"`` for every one, a list of skill names to restrict,
    ``None``/``[]`` to disable. The Agent SDK defaults to ``None`` even
    when skills exist on disk, so callers must pass the resolved value.
    ``force_path_guard=True`` installs the Edit/Write path guard even when
    ``allowed_tools`` does not name Edit/Write — the composer passes this
    for ``allowed: "all"`` roles, whose resolved allowlist is empty but
    which still reach Edit/Write under ``bypassPermissions``.
    """
    raw_model = _env(role, "MODEL")
    if raw_model:
        model = raw_model
    raw_thinking = _env(role, "THINKING")
    if raw_thinking is not None:
        thinking = raw_thinking.lower() in ("on", "true", "1", "yes")
    thinking_cfg = (
        ThinkingConfigEnabled(
            type="enabled", budget_tokens=thinking_budget_tokens
        )
        if thinking
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
        allowed_tools=allowed_tools,
        disallowed_tools=list(_DISALLOWED_BUILTIN_TOOLS),
        mcp_servers=mcp_servers or {},
        can_use_tool=can_use_tool,
        hooks=_with_path_guard(
            hooks, allowed_tools, path_guard_state, force=force_path_guard
        ),
        cwd=cwd,
        setting_sources=["project"],
        thinking=thinking_cfg,
        env=sdk_env,
        permission_mode="bypassPermissions",
        output_format=output_format,
        skills=skills,
    )
    # The Agent SDK lowers ``system_prompt=None`` to an explicit
    # ``--system-prompt ""`` CLI arg (see claude_agent_sdk/_internal/
    # transport/subprocess_cli.py:_build_command), which *replaces* the
    # built-in Claude Code system prompt with an empty string. That default
    # prompt is the scaffolding that injects the target repo's CLAUDE.md and
    # surfaces installed project skills to the model, so an empty system
    # prompt silently disables both. Passing the ``claude_code`` preset with
    # no ``append`` makes the SDK emit no ``--system-prompt`` flag at all, so
    # the CLI keeps its default prompt. A caller-supplied prompt is layered on
    # via the preset's ``append`` so it adds to — rather than wipes — the
    # Claude Code base (and thus CLAUDE.md / skills survive for those roles
    # too). Roles using ``prompt_as_user_message: true`` pass
    # ``system_prompt=None`` and get the bare preset; their instructions ride
    # in as the first user message.
    preset: SystemPromptPreset = {"type": "preset", "preset": "claude_code"}
    if system_prompt:
        preset = {
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        }
    options.system_prompt = preset
    return options
