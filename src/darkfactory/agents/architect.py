"""Architect — SDK-driven discovery role.

Turns a list of user stories into a topo-sortable list of work packages.
No tools, no MCP servers; reasoning-only role with structured output.

The output shape is defined by ``schemas/architect_output.json`` and
enforced by the SDK's ``output_format``; this module does not declare a
Pydantic schema. The discovery subgraph (``stages/discovery.py``) is the
single consumer and is responsible for shaping the brief and deriving the
legacy ``spec`` channel.
"""
from __future__ import annotations

import json
from typing import Any

from darkfactory.agents._sdk_common import (
    ParseError,
    _drain,
    original_user_request,
    render_role_user_message,
    repo_summary,
)
from darkfactory.agents.compose import ComposeState, compose


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
        "architect",
        user_request=state_slice.get("user_request", "") or "",
        original_user_request=original_user_request(state_slice),
        repo_context=repo_summary(state_slice.get("repo_context")),
        stories=json.dumps(state_slice.get("stories") or [], indent=2),
        planning_feedback=_planning_feedback_text(state_slice),
    )


async def run_architect(state_slice: dict) -> dict[str, Any]:
    compose_state = ComposeState.from_mapping(state_slice)
    rendered = _render_user_prompt(state_slice)
    async with compose(
        "architect",
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(rendered)
        _text, structured, _result = await _drain(client)
    if structured is None:
        raise ParseError("Architect emitted no structured output")
    return structured
