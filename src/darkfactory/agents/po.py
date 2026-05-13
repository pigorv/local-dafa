"""Product Owner — SDK-driven discovery role.

Reasoning-only role that translates a user request plus repo context into
a dict of brief-intent fields and user stories. The output shape is
defined by ``schemas/po_output.json`` (canonical, hand-edited) and
enforced by the SDK's ``output_format``; this module does not declare a
Pydantic schema.

Two transforms are applied to the structured output:

- ``_normalize_legacy_aliases`` accepts pre-v2 keys (``acceptance_criteria``,
  ``risks``, ``assumptions``) and maps them onto the v2 field names. The
  shim stays until every legacy caller is migrated (per CLAUDE.md).
- ``_derive_expected_behavior_from_stories`` populates ``expected_behavior``
  from ``stories[].acceptance_criteria`` when the model left it empty.

``_ensure_defaults`` backfills optional fields so downstream consumers
always see the full ProductRequest shape.
"""
from __future__ import annotations

from typing import Any

from darkfactory.agents._sdk_common import (
    ParseError,
    _drain,
    render_role_user_message,
    repo_summary,
)
from darkfactory.agents.compose import ComposeState, compose

_LEGACY_ALIASES: dict[str, str] = {
    "acceptance_criteria": "expected_behavior",
    "risks": "compatibility_risks",
    "assumptions": "open_assumptions",
}


def _normalize_legacy_aliases(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for legacy_key, v2_key in _LEGACY_ALIASES.items():
        if legacy_key in out and v2_key not in out:
            out[v2_key] = out[legacy_key]
    return out


def _derive_expected_behavior_from_stories(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("expected_behavior"):
        return data
    out = dict(data)
    derived: list[str] = []
    for story in out.get("stories") or []:
        if isinstance(story, dict):
            derived.extend(story.get("acceptance_criteria") or [])
    out["expected_behavior"] = derived
    return out


def _ensure_defaults(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    out.setdefault("problem", "")
    out.setdefault("expected_behavior", [])
    out.setdefault("compatibility_risks", [])
    out.setdefault("open_assumptions", [])
    out.setdefault("stories", [])
    return out


def normalize_po_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Public entry-point for legacy-alias + derive transforms.

    Exposed so tests can exercise the shim layer without driving an SDK
    client.
    """
    return _ensure_defaults(
        _derive_expected_behavior_from_stories(_normalize_legacy_aliases(raw))
    )


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
        "po",
        user_request=state_slice.get("user_request", "") or "",
        repo_context=repo_summary(state_slice.get("repo_context")),
        planning_feedback=_planning_feedback_text(state_slice),
    )


async def run_po(state_slice: dict) -> dict[str, Any]:
    compose_state = ComposeState.from_mapping(state_slice)
    rendered = _render_user_prompt(state_slice)
    async with compose(
        "po",
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(rendered)
        _text, structured, _result = await _drain(client)
    if structured is None:
        raise ParseError("PO emitted no structured output")
    return normalize_po_output(structured)
