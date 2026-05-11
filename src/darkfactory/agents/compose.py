from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
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
from darkfactory.state import Patch
from darkfactory.tools.server import build_mcp_server


@dataclass(slots=True)
class ComposeState:
    """Runtime seams that cannot live in a static role manifest.

    ``slice_id`` tags diff-capture patches with the active Work Package.
    ``task_id`` selects the per-task RepoSandbox and MCP server instance.
    ``patches_sink`` is the mutable list populated by diff_capture.
    ``gate_approved`` closes over the human gate state for PR publication.
    ``dependency_changes_authorized`` feeds path_guard's lockfile allowance.
    ``user_request`` and ``spec_summary`` feed goal_pin reminders.
    ``slice_intent`` is the active Work Package's intent string, used by
    diff_capture's ``justification_template`` so per-edit justifications
    remain WP-scoped without each role re-implementing the lookup.
    ``patch_justification`` is a caller-precomputed seam for roles whose
    justification text cannot be expressed as a static ``{slice_id}/
    {slice_intent}`` template (e.g. Fixer derives WP ids + predicates from
    ``verify_summary``). Manifests opt in via
    ``justification_template: "{patch_justification}"``.
    """

    slice_id: str = ""
    task_id: str = ""
    patches_sink: list[Patch] = field(default_factory=list)
    gate_approved: bool = False
    dependency_changes_authorized: bool = False
    user_request: str = ""
    spec_summary: str = ""
    slice_intent: str = ""
    patch_justification: str = ""

    @classmethod
    def from_mapping(
        cls,
        state: Mapping[str, Any],
        *,
        patches_sink: list[Patch] | None = None,
    ) -> "ComposeState":
        slice_id = str(state.get("slice_id") or state.get("current_slice") or "")
        return cls(
            slice_id=slice_id,
            task_id=str(
                state.get("task_id")
                or state.get("wf_id")
                or state.get("workflow_id")
                or ""
            ),
            patches_sink=patches_sink if patches_sink is not None else [],
            gate_approved=bool(state.get("gate_approved", False)),
            dependency_changes_authorized=bool(
                state.get("dependency_changes_authorized", False)
                or state.get("allow_dependency_changes", False)
            ),
            user_request=str(state.get("user_request") or ""),
            spec_summary=str(state.get("spec_summary") or ""),
            slice_intent=_lookup_slice_intent(state.get("spec"), slice_id),
            patch_justification=str(state.get("patch_justification") or ""),
        )


def _lookup_slice_intent(spec: Any, slice_id: str) -> str:
    if not slice_id or not isinstance(spec, list):
        return ""
    for entry in spec:
        if isinstance(entry, Mapping) and entry.get("story_id") == slice_id:
            return str(entry.get("intent") or "")
    return ""


@dataclass(frozen=True, slots=True)
class ComposeOverrides:
    """In-process overrides for tests and activity-local experiments."""

    registry: Registry | None = None
    model: str | None = None
    temperature: float | None = None
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
        "" if manifest.llm.prompt_as_user_message
        else prompt_path.read_text(encoding="utf-8")
    )
    hooks = _materialize_hooks(role, manifest, state_slice, runtime_task_id)
    mcp_servers = _materialize_mcp_servers(manifest.mcp, runtime_task_id)
    can_use_tool = _materialize_permission_gate(role, manifest, state_slice, registry)
    output_format = _load_output_format(manifest.llm.structured_output)

    options = build_options(
        role,
        model=manifest.llm.model,
        temperature=manifest.llm.temperature,
        thinking=manifest.llm.thinking.enabled,
        thinking_budget_tokens=manifest.llm.thinking.budget_tokens or 4096,
        system_prompt=system_prompt,
        allowed_tools=list(manifest.tools.allowed),
        hooks=hooks,
        mcp_servers=mcp_servers,
        can_use_tool=can_use_tool,
        path_guard_state=_path_guard_state(state_slice),
        cwd=overrides.cwd or "/workspace",
        output_format=output_format,
    )

    options.disallowed_tools = list(manifest.tools.disallowed)
    options.patches_sink = state_slice.patches_sink
    _apply_in_process_overrides(options, manifest, overrides)
    _stamp_manifest_attrs(registry, role, manifest, prompt_path)
    return ClaudeSDKClient(options=options)


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
    if not (
        manifest.tools.argv_allowlist
        or "sandbox_bash" in manifest.tools.allowed
        or manifest.mcp
    ):
        return None
    return make_permission_gate(
        role,
        manifest.tools.argv_allowlist,
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
    if attachment.name == "diff_capture":
        template = params.pop("justification_template", None)
        if template and "justification" not in params:
            params["justification"] = _format_justification(template, state_slice)
        return factory(
            params.pop("role", role),
            params.pop("slice_id", state_slice.slice_id),
            params.pop("task_id", task_id),
            state_slice.patches_sink,
            **params,
        )
    if attachment.name == "goal_pin":
        return factory(
            params.pop("user_request", state_slice.user_request),
            params.pop("spec_summary", state_slice.spec_summary),
            **params,
        )
    return factory(**params)


def _format_justification(template: str, state_slice: ComposeState) -> str:
    """Render a diff_capture ``justification_template`` from a ComposeState.

    Mirrors the imperative builder/tester pattern:
    ``f"<prefix> {slice_intent}" if slice_intent else "<prefix>"``. When the
    Work Package has no recorded intent, any trailing colon left after
    substituting an empty ``{slice_intent}`` is stripped so reviewers don't
    see dangling punctuation. Other placeholders go through ``str.format``
    untouched.
    """
    rendered = template.format(
        slice_id=state_slice.slice_id,
        slice_intent=state_slice.slice_intent,
        patch_justification=state_slice.patch_justification,
    ).rstrip()
    if not state_slice.slice_intent and rendered.endswith(":"):
        rendered = rendered[:-1].rstrip()
    return rendered


def _apply_in_process_overrides(
    options: Any,
    manifest: RoleManifest,
    overrides: ComposeOverrides,
) -> None:
    if overrides.model is not None:
        options.model = overrides.model
    if overrides.temperature is not None:
        options.temperature = overrides.temperature

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
