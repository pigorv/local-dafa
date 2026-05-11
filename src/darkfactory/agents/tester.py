"""Tester Worker — single generalist test-writing role.

Owns test code; reads the Builder's diff for shapes only and derives
assertions from the WP's ``verification`` predicate (the diff-blindness rule).

Production-code edits are restricted to the ``tester_mechanical``
category — rename / import / signature alignment. Anything semantic
returns a structured finding instead of touching production code; the
strict Fixer handles those.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from darkfactory.agents._sdk_common import run_to_completion
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.state import Patch

ROLE = "tester"


class CoverageEntryModel(BaseModel):
    wp_id: str
    predicate: str
    test_names: list[str] = Field(default_factory=list)


class TesterFinding(BaseModel):
    kind: Literal[
        "behavior_mismatch",
        "naming_mismatch",
        "unclear_predicate",
        "infeasible_predicate",
    ]
    wp_id: str
    detail: str = ""


class TesterOutput(BaseModel):
    """Tester structured output: tests + coverage map + findings."""

    # `Test*`-named classes are otherwise picked up by pytest's default class
    # collection rule and emit a noisy PytestCollectionWarning.
    __test__ = False

    summary: str = ""
    coverage: list[CoverageEntryModel] = Field(default_factory=list)
    findings: list[TesterFinding] = Field(default_factory=list)
    patches: list[dict[str, Any]] = Field(default_factory=list)


def _builder_signal(state_slice: dict) -> str:
    """Pluck the Builder's patches and summary for *this* WP into a Tester briefing.

    The Tester is told to use these to learn shapes only — it must not
    derive assertions from this content.
    """
    slice_id = state_slice.get("current_slice") or ""
    builder_patches = [
        p
        for p in (state_slice.get("patches") or [])
        if isinstance(p, dict)
        and p.get("slice_id") == slice_id
        and p.get("author_agent") == "builder"
    ]
    summary = state_slice.get("builder_summary") or ""
    return json.dumps(
        {"builder_patches": builder_patches, "builder_summary": summary},
        indent=2,
    )


def _user_message(state_slice: dict) -> str:
    slice_id = state_slice.get("current_slice") or ""
    slice_obj: dict = {}
    for s in state_slice.get("spec") or []:
        if isinstance(s, dict) and s.get("story_id") == slice_id:
            slice_obj = s
            break
    return (
        f"Work Package (JSON):\n{json.dumps(slice_obj, indent=2)}\n\n"
        f"Builder output for this WP (read for shape, NOT for assertions):\n"
        f"{_builder_signal(state_slice)}\n\n"
        "Write tests against the WP's `verification` predicate. End with a "
        "TesterOutput tool call carrying the coverage map and any findings."
    )


async def run_tester(state_slice: dict) -> TesterOutput:
    sink: list[Patch] = []
    compose_state = ComposeState.from_mapping(state_slice, patches_sink=sink)
    async with compose(
        ROLE,
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(_user_message(state_slice))
        try:
            result = await run_to_completion(client, expect=TesterOutput)
        except Exception:
            # Fall back to an empty TesterOutput so the build subgraph keeps
            # progressing; the Verifier semantic pass will mark predicates
            # uncovered and the build will fail into Fixer with a clear
            # signal rather than crashing the workflow.
            return TesterOutput(patches=[dict(p) for p in sink])
    assert isinstance(result, TesterOutput)
    # Merge captured test patches into the structured output so callers see
    # both the diff-capture sink and the Tester's self-reported coverage.
    result.patches = [dict(p) for p in sink]
    return result
