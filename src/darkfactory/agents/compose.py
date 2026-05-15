from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient, HookMatcher
from claude_agent_sdk.types import (
    ThinkingConfigDisabled,
    ThinkingConfigEnabled,
)
from opentelemetry import trace as _otel_trace

import darkfactory.hooks as hook_exports
from darkfactory.agents.manifest import HookAttachment, RoleManifest
from darkfactory.agents.registry import (
    Registry,
    get_default_registry,
    resolve_prompt_path,
)
from darkfactory.hooks.permission_gate import make_permission_gate
from darkfactory.llm_factory import build_options
from darkfactory.tools.server import build_mcp_server


@dataclass(slots=True)
class ComposeState:
    """Runtime seams that cannot live in a static role manifest.

    ``slice_id`` tags the active Work Package for compose-time use (some
    roles override it, e.g. Fixer pins it to the failing WP).
    ``task_id`` selects the per-task RepoSandbox and MCP server instance.
    ``gate_approved`` closes over the human gate state for PR publication.
    ``dependency_changes_authorized`` feeds path_guard's lockfile allowance.
    ``user_request`` and ``spec_summary`` feed goal_pin reminders.

    PR C removed the ``patches_sink``, ``slice_intent``, and
    ``patch_justification`` seams that fed the legacy ``diff_capture``
    hook — patches are now computed deterministically from ``git diff``
    by the build subgraph and the Fixer activity.
    """

    slice_id: str = ""
    task_id: str = ""
    gate_approved: bool = False
    dependency_changes_authorized: bool = False
    user_request: str = ""
    spec_summary: str = ""

    @classmethod
    def task_only(cls, task_id: str) -> "ComposeState":
        """Minimal ``ComposeState`` for roles with no compose-time seams.

        Use when the role consumes no gate flag or ``user_request`` /
        ``spec_summary`` reminders. Triage is the canonical example:
        zero tools, one query per run, so every seam but ``task_id`` is
        dead. Going through ``from_mapping`` for such a role would pull
        keys that are guaranteed not to matter and obscure the actual
        contract.
        """
        return cls(task_id=str(task_id or ""))

    @classmethod
    def from_mapping(cls, state: Mapping[str, Any]) -> "ComposeState":
        slice_id = str(state.get("slice_id") or state.get("current_slice") or "")
        return cls(
            slice_id=slice_id,
            task_id=str(
                state.get("task_id")
                or state.get("wf_id")
                or state.get("workflow_id")
                or ""
            ),
            gate_approved=bool(state.get("gate_approved", False)),
            dependency_changes_authorized=bool(
                state.get("dependency_changes_authorized", False)
                or state.get("allow_dependency_changes", False)
            ),
            user_request=str(state.get("user_request") or ""),
            spec_summary=str(state.get("spec_summary") or ""),
        )


@dataclass(frozen=True, slots=True)
class ComposeOverrides:
    """In-process overrides for tests and activity-local experiments."""

    registry: Registry | None = None
    model: str | None = None
    thinking: bool | None = None
    thinking_budget_tokens: int | None = None
    cwd: str | None = None


def compose(
    role: str,
    state_slice: ComposeState,
    *,
    task_id: str,
    overrides: ComposeOverrides | None = None,
) -> ClaudeSDKClient:
    """Materialize a role manifest into a configured Claude SDK client."""
    overrides = overrides or ComposeOverrides()
    registry = overrides.registry or get_default_registry()
    manifest = registry.get(role)
    runtime_task_id = state_slice.task_id or task_id

    prompt_path = resolve_prompt_path(manifest.llm.prompt_path)
    system_prompt = (
        None if manifest.llm.prompt_as_user_message
        else prompt_path.read_text(encoding="utf-8")
    )
    hooks = _materialize_hooks(role, manifest, state_slice, runtime_task_id)
    mcp_servers = _materialize_mcp_servers(manifest.mcp, runtime_task_id)
    can_use_tool = _materialize_permission_gate(role, manifest, state_slice, registry)
    output_format = _load_output_format(manifest.llm.structured_output)

    options = build_options(
        role,
        model=manifest.llm.model,
        thinking=manifest.llm.thinking.enabled,
        thinking_budget_tokens=manifest.llm.thinking.budget_tokens or 4096,
        system_prompt=system_prompt,
        allowed_tools=_resolve_allowed_tools(role, manifest),
        hooks=hooks,
        mcp_servers=mcp_servers,
        can_use_tool=can_use_tool,
        path_guard_state=_path_guard_state(state_slice),
        cwd=overrides.cwd or "/workspace",
        output_format=output_format,
        skills=_resolve_skills(role, manifest),
    )

    options.disallowed_tools = list(manifest.tools.disallowed)
    _apply_in_process_overrides(options, manifest, overrides)
    _stamp_manifest_attrs(registry, role, manifest, prompt_path)
    return ClaudeSDKClient(options=options)


def _resolve_allowed_tools(role: str, manifest: RoleManifest) -> list[str]:
    """Assemble the role's effective ``allowed_tools`` list.

    Starts from ``manifest.tools.allowed``. Then appends project-MCP
    allowlist patterns sourced from ``manifest.tools.project_mcp_allowed``
    (default ``["*"]``) — overridable per role at runtime with
    ``LLM_<ROLE>_PROJECT_MCP`` (``"*"`` for all servers, ``"name1,name2"``
    for an explicit list, empty string to disable). Server names expand
    to ``mcp__<name>__*``; the literal ``"*"`` entry expands to
    ``mcp__*``. Duplicates are dropped. Roles that declare
    ``tools.allowed: []`` (plan_critic, verifier_semantic) are zero-tool
    by design and short-circuit before expansion — an empty manifest
    allowlist is treated as a deliberate "no tools" statement, not
    "expand from nothing". Project skills are gated separately via
    :func:`_resolve_skills` and the SDK's ``skills`` option; they do not
    appear in ``allowed_tools``.
    """
    allowed = list(manifest.tools.allowed)
    if not allowed:
        return allowed
    patterns = _expand_project_mcp_patterns(_project_mcp_entries(role, manifest))
    for pattern in patterns:
        if pattern not in allowed:
            allowed.append(pattern)
    return allowed


def _project_mcp_entries(role: str, manifest: RoleManifest) -> list[str]:
    raw = os.getenv(f"LLM_{role.upper()}_PROJECT_MCP")
    if raw is None:
        return list(manifest.tools.project_mcp_allowed)
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def _expand_project_mcp_patterns(entries: list[str]) -> list[str]:
    out: list[str] = []
    for entry in entries:
        if entry == "*":
            pattern = "mcp__*"
        else:
            pattern = f"mcp__{entry}__*"
        if pattern not in out:
            out.append(pattern)
    return out


def _resolve_skills(role: str, manifest: RoleManifest) -> str | list[str] | None:
    """Resolve which project skills should be enabled for the role.

    Returns ``"all"`` (every discovered skill), a list of skill names
    (restrict to those), or ``None`` (no skills). Zero-tool roles always
    get ``None``. ``LLM_<ROLE>_SKILLS`` overrides the manifest:
    ``"all"`` → all, ``"name1,name2"`` → list, empty string → disabled.
    """
    if not manifest.tools.allowed:
        return None
    raw = os.getenv(f"LLM_{role.upper()}_SKILLS")
    if raw is None:
        value = manifest.tools.skills
    elif raw.strip().lower() == "all":
        value = "all"
    else:
        value = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if isinstance(value, list) and not value:
        return None
    return value


def _load_output_format(structured_output: str | None) -> dict[str, Any] | None:
    if not structured_output:
        return None
    schema_path = resolve_prompt_path(structured_output)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return {"type": "json_schema", "schema": schema}


def _path_guard_state(state_slice: ComposeState) -> Mapping[str, bool]:
    return {
        "dependency_changes_authorized": state_slice.dependency_changes_authorized,
    }


def _materialize_permission_gate(
    role: str,
    manifest: RoleManifest,
    state_slice: ComposeState,
    registry: Registry,
):
    # The gate is installed whenever the role can reach a shell — either
    # the built-in ``Bash`` tool, the ``sandbox_bash`` MCP tool, or any
    # MCP server (since they all expose tool surfaces that can route
    # through ``can_use_tool``). A non-empty ``argv_allowlist`` or
    # ``argv_denylist`` also forces installation. Reasoning-only roles
    # (PO, Architect) with none of these short-circuit to ``None``.
    if not (
        manifest.tools.argv_allowlist
        or manifest.tools.argv_denylist
        or "Bash" in manifest.tools.allowed
        or "sandbox_bash" in manifest.tools.allowed
        or manifest.mcp
    ):
        return None
    return make_permission_gate(
        role,
        manifest.tools.argv_allowlist,
        argv_denylist=manifest.tools.argv_denylist,
        gate_approved=state_slice.gate_approved,
        role_owned_argv_prefixes=registry.role_owned_argv_table(),
    )


def _materialize_mcp_servers(names: list[str], task_id: str) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for name in names:
        if name != "darkfactory":
            raise ValueError(f"unknown MCP server declared in manifest: {name!r}")
        servers[name] = build_mcp_server(task_id)
    return servers


def _materialize_hooks(
    role: str,
    manifest: RoleManifest,
    state_slice: ComposeState,
    task_id: str,
) -> dict[str, list[HookMatcher]]:
    hooks: dict[str, list[HookMatcher]] = {}
    for attachment in manifest.hooks:
        callback = _materialize_hook(role, attachment, state_slice, task_id)
        if callback is None:
            continue
        hooks.setdefault(attachment.event, []).append(HookMatcher(hooks=[callback]))
    return hooks


def _materialize_hook(
    role: str,
    attachment: HookAttachment,
    state_slice: ComposeState,
    task_id: str,
) -> Any | None:
    if attachment.name == "path_guard":
        # build_options owns path_guard insertion so Edit/Write safety remains
        # code-declared and cannot be accidentally disabled by manifest shape.
        return None

    factory = hook_exports.MANIFEST_HOOKS[attachment.name]
    params = dict(attachment.parameters)
    if attachment.name == "goal_pin":
        return factory(
            params.pop("user_request", state_slice.user_request),
            params.pop("spec_summary", state_slice.spec_summary),
            **params,
        )
    return factory(**params)


def _apply_in_process_overrides(
    options: Any,
    manifest: RoleManifest,
    overrides: ComposeOverrides,
) -> None:
    if overrides.model is not None:
        options.model = overrides.model

    thinking_enabled = overrides.thinking
    if thinking_enabled is None:
        thinking = options.thinking or {}
        thinking_enabled = thinking.get("type") == "enabled"

    if thinking_enabled:
        current = options.thinking or {}
        budget = (
            overrides.thinking_budget_tokens
            or manifest.llm.thinking.budget_tokens
            or current.get("budget_tokens")
            or 4096
        )
        options.thinking = ThinkingConfigEnabled(
            type="enabled",
            budget_tokens=int(budget),
        )
    elif overrides.thinking is not None:
        options.thinking = ThinkingConfigDisabled(type="disabled")


def _stamp_manifest_attrs(
    registry: Registry,
    role: str,
    manifest: RoleManifest,
    prompt_path: Path,
) -> None:
    span = _otel_trace.get_current_span()
    span.set_attribute(
        "darkfactory.manifest_sha",
        _manifest_sha(registry, role, manifest),
    )
    span.set_attribute("darkfactory.prompt_sha", _file_sha(prompt_path))


def _manifest_sha(registry: Registry, role: str, manifest: RoleManifest) -> str:
    try:
        return _file_sha(registry.source_path(role))
    except KeyError:
        payload = manifest.model_dump_json().encode("utf-8")
        return sha256(payload).hexdigest()


def _file_sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()
