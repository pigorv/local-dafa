"""Architect - SDK-driven discovery role.

Turns a list of user stories into a topo-sortable list of work packages.
No tools, no MCP servers; reasoning-only role with structured output.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from darkfactory.agents._sdk_common import (
    repo_summary,
    run_to_completion,
)
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.state import (
    ContractChanges,
    WorkPackage,
    work_package_dict_from_model,
    work_package_from_dict,
)


class WorkPackagePlanModel(BaseModel):
    story_id: str
    approach: str
    affected_files: list[str] = Field(default_factory=list)
    new_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    # v2 fields carried alongside the legacy aliases so they survive
    # the model_validator round-trip and reach downstream stages that
    # still read state["spec"].
    id: str = ""
    title: str = ""
    intent: str = ""
    verification: list[str] = Field(default_factory=list)
    repo_areas: list[str] = Field(default_factory=list)
    candidate_files: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    estimated_scope: str = ""
    notes: list[str] = Field(default_factory=list)

    @field_validator("verification", mode="before")
    @classmethod
    def _coerce_verification(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        return list(value)


def _empty_contract_changes() -> ContractChanges:
    return ContractChanges(api=[], data=[], events=[])


class ArchitectOutput(BaseModel):
    """Architect output with v2 planning fields and legacy spec aliases."""

    current_understanding: str = ""
    proposed_design: str = ""
    contract_changes: ContractChanges = Field(default_factory=_empty_contract_changes)
    test_strategy: str = ""
    work_packages: list[WorkPackage] = Field(default_factory=list)
    spec: list[WorkPackagePlanModel] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _sync_legacy_and_v2_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        if "work_packages" not in normalized and "spec" in normalized:
            normalized["work_packages"] = [
                work_package_from_dict(
                    WorkPackagePlanModel.model_validate(slice_).model_dump()
                )
                for slice_ in normalized.get("spec") or []
            ]
        if "spec" not in normalized and "work_packages" in normalized:
            normalized["spec"] = [
                work_package_dict_from_model(
                    WorkPackage.model_validate(work_package)
                )
                for work_package in normalized.get("work_packages") or []
            ]
        return normalized


def _planning_feedback_section(state_slice: dict) -> str:
    feedback = [
        str(item)
        for item in state_slice.get("planning_feedback") or []
        if item
    ]
    if not feedback:
        return ""
    lines = "\n".join(f"- {item}" for item in feedback)
    return f"\n\nPlanning feedback from prior attempt:\n{lines}"


def _user_message(state_slice: dict) -> str:
    user_request = state_slice.get("user_request", "") or ""
    stories = state_slice.get("stories", []) or []
    ctx_blob = repo_summary(state_slice.get("repo_context"))
    return (
        f"User request:\n{user_request}\n\n"
        f"Repo context:\n{ctx_blob}\n\n"
        f"User stories (JSON):\n{json.dumps(stories, indent=2)}\n\n"
        f"{_planning_feedback_section(state_slice)}\n\n"
        "Produce ArchitectOutput with work_packages and dependencies."
    )


async def run_architect(state_slice: dict) -> ArchitectOutput:
    compose_state = ComposeState.from_mapping(state_slice)
    async with compose(
        "architect",
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=ArchitectOutput)
        assert isinstance(result, ArchitectOutput)
        return result
