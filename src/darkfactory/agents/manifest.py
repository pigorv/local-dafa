from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestIdentity(_StrictModel):
    role: str
    description: str
    when_to_use: str


class ThinkingPolicy(_StrictModel):
    enabled: bool
    budget_tokens: int | None = Field(default=None, ge=1)


class LLMPolicy(_StrictModel):
    model: str
    thinking: ThinkingPolicy
    prompt_path: str
    structured_output: str | None = None
    prompt_as_user_message: bool = False


class ToolPolicy(_StrictModel):
    allowed: list[str]
    disallowed: list[str]
    argv_allowlist: list[str]
    argv_denylist: list[tuple[str, ...]] = Field(default_factory=list)
    role_owned_argv_prefixes: list[tuple[str, ...]]
    edit_path_allowlist: list[str]
    # Opt this role into MCP servers declared by the target repo (loaded
    # via setting_sources=["project"] from /workspace/.mcp.json or
    # /workspace/.claude/settings.json). Each entry is either a server
    # name (expanded to ``mcp__<name>__*`` and appended to allowed_tools)
    # or the wildcard ``"*"`` (expanded to ``mcp__*``). Default is
    # ``["*"]`` — every project-loaded MCP tool is callable from this
    # role. Set to ``[]`` in the manifest to disable. Roles whose
    # ``allowed`` list is empty (plan_critic, verifier_semantic) are
    # zero-tool by design and ignore this field. Override per-role at
    # runtime with ``LLM_<ROLE>_PROJECT_MCP="*" | "name1,name2" | ""``.
    project_mcp_allowed: list[str] = Field(default_factory=lambda: ["*"])
    # Skills discovered from the target repo's ``.claude/skills/``
    # directory (loaded via setting_sources=["project"]). ``"all"``
    # enables every discovered skill; a list restricts to those names
    # (e.g. ``["pdf", "docx"]``); ``[]`` disables. Default is ``"all"``.
    # Roles whose ``allowed`` list is empty (plan_critic,
    # verifier_semantic) are zero-tool by design and never load skills.
    # Override per-role at runtime with
    # ``LLM_<ROLE>_SKILLS="all" | "name1,name2" | ""``.
    skills: Literal["all"] | list[str] = "all"


class HookAttachment(_StrictModel):
    event: str
    name: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class BudgetPolicy(_StrictModel):
    timeout: int | None = Field(default=None, ge=1)
    heartbeat: int | None = Field(default=None, ge=1)
    retry_caps: dict[str, int] = Field(default_factory=dict)


class RoleManifest(_StrictModel):
    identity: ManifestIdentity
    llm: LLMPolicy
    tools: ToolPolicy
    mcp: list[str]
    hooks: list[HookAttachment]
    budgets: BudgetPolicy
