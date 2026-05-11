"""Semantic Verifier — reasoning-only predicate coverage role."""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from darkfactory.agents._sdk_common import run_to_completion
from darkfactory.agents.compose import ComposeState, compose

ROLE = "verifier_semantic"

CoverageStatus = Literal["covered", "uncovered", "weakly_covered"]


class PredicateCoverageModel(BaseModel):
    wp_id: str
    predicate: str
    status: CoverageStatus
    evidence: str = ""


class SemanticVerifierOutput(BaseModel):
    """Semantic coverage map for every Work Package predicate."""

    predicate_coverage: list[PredicateCoverageModel] = Field(default_factory=list)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _test_files_from_spec(spec: list[Any]) -> list[str]:
    files: list[str] = []
    for item in spec:
        if not isinstance(item, dict):
            continue
        for path in item.get("test_files") or []:
            if path not in files:
                files.append(path)
    return files


def _user_message(state_slice: dict) -> str:
    brief = _jsonable(state_slice.get("implementation_brief") or {})
    spec = _jsonable(state_slice.get("spec") or [])
    coverage_entries = _jsonable(state_slice.get("coverage_entries") or [])
    test_results = _jsonable(state_slice.get("test_results") or [])
    findings = _jsonable(state_slice.get("findings") or [])
    tester_findings = _jsonable(state_slice.get("tester_findings") or [])
    test_files = _jsonable(
        state_slice.get("test_files") or _test_files_from_spec(state_slice.get("spec") or [])
    )

    return (
        "Assess semantic predicate coverage for the verified build.\n\n"
        f"Implementation Brief (JSON):\n{json.dumps(brief, indent=2)}\n\n"
        f"Work Packages / legacy spec data (JSON):\n{json.dumps(spec, indent=2)}\n\n"
        f"Tester coverage entries (JSON):\n{json.dumps(coverage_entries, indent=2)}\n\n"
        f"Known test files (JSON):\n{json.dumps(test_files, indent=2)}\n\n"
        f"Verify test results (JSON):\n{json.dumps(test_results, indent=2)}\n\n"
        f"Mechanical findings (JSON):\n{json.dumps(findings, indent=2)}\n\n"
        f"Tester findings (JSON):\n{json.dumps(tester_findings, indent=2)}\n\n"
        "Return a SemanticVerifierOutput JSON object with one predicate_coverage "
        "entry for every Work Package verification predicate."
    )


async def run_verifier_semantic(state_slice: dict) -> SemanticVerifierOutput:
    compose_state = ComposeState.from_mapping(state_slice)
    async with compose(
        ROLE,
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=SemanticVerifierOutput)
        assert isinstance(result, SemanticVerifierOutput)
        return result
