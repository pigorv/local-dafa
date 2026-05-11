"""Plan Critic — SDK-driven discovery role.

Reviews the spec produced by the Architect against the original stories
and either approves it or returns targeted edits keyed by story id.
No tools, no MCP servers; reasoning-only role with structured output.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from darkfactory.agents._sdk_common import run_to_completion
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.state import WorkPackage, work_package_from_dict


class ReviewDecisionModel(BaseModel):
    """Plan Critic decision — approves the brief or returns targeted edits."""

    approved: bool
    reason: str = ""
    edits: dict = Field(default_factory=dict)


def _resolve_work_packages(state_slice: dict) -> list[dict]:
    """Return v2 work packages as dicts.

    Prefers `state["work_packages"]` (written by the v2 architect_node).
    Falls back to converting legacy `state["spec"]` slices via
    `work_package_from_dict` so older fixtures still feed the critic.
    """
    work_packages = state_slice.get("work_packages") or []
    if work_packages:
        return [
            WorkPackage.model_validate(wp).model_dump()
            for wp in work_packages
        ]
    spec = state_slice.get("spec") or []
    return [work_package_from_dict(slice_).model_dump() for slice_ in spec]


def _user_message(state_slice: dict) -> str:
    stories = state_slice.get("stories", []) or []
    work_packages = _resolve_work_packages(state_slice)
    attempt = int(state_slice.get("planning_attempts") or 1)
    prior_feedback = [
        str(item) for item in (state_slice.get("planning_feedback") or []) if item
    ]
    prior_block = (
        f"\n\nPrior plan-critic rejections in this run (attempt → feedback):\n"
        + "\n".join(f"- attempt {i + 1}: {fb}" for i, fb in enumerate(prior_feedback))
        if prior_feedback
        else ""
    )
    return (
        f"Attempt: {attempt} (this is rejection #{len(prior_feedback) + 1} if you "
        f"reject again).{prior_block}\n\n"
        f"User stories (JSON):\n{json.dumps(stories, indent=2)}\n\n"
        f"Work packages (JSON):\n{json.dumps(work_packages, indent=2)}\n\n"
        "Review the work packages against the stories; approve or return targeted edits."
    )


async def run_plan_critic(state_slice: dict) -> ReviewDecisionModel:
    compose_state = ComposeState.from_mapping(state_slice)
    async with compose(
        "plan_critic",
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(_user_message(state_slice))
        result = await run_to_completion(client, expect=ReviewDecisionModel)
        assert isinstance(result, ReviewDecisionModel)
        return result
