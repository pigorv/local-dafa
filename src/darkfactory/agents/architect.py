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
from string import Template
from typing import Any

from darkfactory.agents._sdk_common import ParseError, _drain, repo_summary
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.registry import get_default_registry, resolve_prompt_path


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
    manifest = get_default_registry().get("architect")
    template_text = resolve_prompt_path(manifest.llm.prompt_path).read_text(
        encoding="utf-8"
    )
    return Template(template_text).safe_substitute(
        user_request=state_slice.get("user_request", "") or "",
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
