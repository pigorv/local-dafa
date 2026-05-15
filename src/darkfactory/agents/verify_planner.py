"""Verify Planner — SDK-driven discovery role.

Reads the target repository and emits a VerificationPlan (see
``state.VerificationPlan``) describing the project's canonical test /
compile / lint commands plus report-file globs the deterministic verifier
should parse. The plan is cached on ``PipelineState.verification_plan``
and reused for every verify iteration; this role is only invoked once
per workflow (or on explicit cache invalidation).

The output shape is defined by ``schemas/verify_planner_output.json`` and
enforced by the SDK's ``output_format``; this module does not declare a
Pydantic schema. The verify subgraph (``stages/verify.py``) is the single
consumer.
"""
from __future__ import annotations

from typing import Any

from darkfactory.agents._sdk_common import (
    ParseError,
    _drain,
    render_role_user_message,
    repo_summary,
    role_turn_span,
    stamp_turn_usage,
)
from darkfactory.agents.compose import ComposeState, compose
from opentelemetry import trace


def _planning_feedback_text(state_slice: dict) -> str:
    feedback = [
        str(item)
        for item in state_slice.get("planning_feedback") or []
        if item
    ]
    if not feedback:
        return "(none)"
    return "\n".join(f"- {item}" for item in feedback)


def _render_user_prompt(state_slice: dict) -> str:
    return render_role_user_message(
        "verify_planner",
        repo_context=repo_summary(state_slice.get("repo_context")),
        planning_feedback=_planning_feedback_text(state_slice),
    )


async def run_verify_planner(state_slice: dict) -> dict[str, Any]:
    """Drive the verify_planner SDK turn and return the structured plan.

    Raises ``ParseError`` when the SDK turn produces no structured
    output; callers (the verify subgraph) treat that as a discovery
    failure and surface it as a synthetic finding rather than crashing
    the activity.
    """
    compose_state = ComposeState.from_mapping(state_slice)
    rendered = _render_user_prompt(state_slice)
    async with role_turn_span("verify_planner"):
        async with compose(
            "verify_planner",
            compose_state,
            task_id=compose_state.task_id,
        ) as client:
            await client.query(rendered)
            _text, structured, _result = await _drain(client)
            stamp_turn_usage(trace.get_current_span(), _result)
    if structured is None:
        raise ParseError("verify_planner emitted no structured output")
    return structured
