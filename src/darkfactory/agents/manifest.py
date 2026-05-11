from __future__ import annotations

from typing import Any

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
    temperature: float
    thinking: ThinkingPolicy
    prompt_path: str
    structured_output: str | None = None
    prompt_as_user_message: bool = False


class ToolPolicy(_StrictModel):
    allowed: list[str]
    disallowed: list[str]
    argv_allowlist: list[str]
    role_owned_argv_prefixes: list[tuple[str, ...]]
    edit_path_allowlist: list[str]


class HookAttachment(_StrictModel):
    event: str
    name: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class BudgetPolicy(_StrictModel):
    timeout: int | None = Field(default=None, ge=1)
    heartbeat: int | None = Field(default=None, ge=1)
    retry_caps: dict[str, int] = Field(default_factory=dict)


class IOContract(_StrictModel):
    reads: list[str]
    writes: list[str]


class RoleManifest(_StrictModel):
    identity: ManifestIdentity
    llm: LLMPolicy
    tools: ToolPolicy
    mcp: list[str]
    hooks: list[HookAttachment]
    budgets: BudgetPolicy
    io_contract: IOContract
