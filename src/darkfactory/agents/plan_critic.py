"""Plan Critic — SDK-driven discovery role.

Reviews the work packages produced by the Architect against the original
stories and either approves the brief or returns targeted edits keyed by
WorkPackage id. No tools, no MCP servers; reasoning-only role with
structured output.

The output shape is defined by ``schemas/plan_critic_output.json`` and
enforced by the SDK's ``output_format``. ``normalize_plan_critic_output``
applies the cross-field invariants JSON Schema can't express:
``approved=True`` forces ``edits={}`` (stray edits would mislead the next
planning loop), and ``approved=False`` with an empty ``reason`` is
backfilled from the edits keys (an empty rejection is not actionable
downstream).
"""
from __future__ import annotations

import json
from string import Template
from typing import Any

from darkfactory.agents._sdk_common import ParseError, _drain
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.registry import get_default_registry, resolve_prompt_path

DEFAULT_PLANNING_MAX_ATTEMPTS = 5


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
    manifest = get_default_registry().get("plan_critic")
    template_text = resolve_prompt_path(manifest.llm.prompt_path).read_text(
        encoding="utf-8"
    )
    brief = state_slice.get("implementation_brief") or {}
    if not isinstance(brief, dict):
        brief = {}
    contract_changes = brief.get("contract_changes") or {
        "api": [],
        "data": [],
        "events": [],
    }
    attempt = int(state_slice.get("planning_attempts") or 1)
    max_attempts = int(
        state_slice.get("planning_max_attempts") or DEFAULT_PLANNING_MAX_ATTEMPTS
    )
    return Template(template_text).safe_substitute(
        user_request=state_slice.get("user_request", "") or "",
        current_understanding=str(brief.get("current_understanding") or ""),
        contract_changes=json.dumps(contract_changes, indent=2),
        stories=json.dumps(state_slice.get("stories") or [], indent=2),
        work_packages=json.dumps(state_slice.get("work_packages") or [], indent=2),
        attempt=str(attempt),
        max_attempts=str(max_attempts),
        planning_feedback=_planning_feedback_text(state_slice),
    )


def _enforce_decision_invariants(data: dict[str, Any]) -> dict[str, Any]:
    """Resolve the cross-field invariants JSON Schema can't express.

    - ``approved=True`` → drop any ``edits`` the model may have attached.
      Downstream only forks on ``approved``; stray edits would mislead the
      next planning loop.
    - ``approved=False`` → ``notes`` is meaningless (concerns must escalate
      to ``reason`` + ``edits`` on a rejection), so clear it.
    - ``approved=False`` with an empty ``reason`` → derive a reason from
      the ``edits`` keys when possible, otherwise mark the decision
      explicitly unactionable so the workflow surfaces it instead of
      forwarding "revise and try again." to the architect.
    """
    out = dict(data)
    approved = bool(out.get("approved"))
    reason = str(out.get("reason") or "").strip()
    edits = dict(out.get("edits") or {})
    notes = [str(n).strip() for n in (out.get("notes") or []) if str(n).strip()]

    if approved:
        edits = {}
    else:
        notes = []
        if not reason:
            if edits:
                reason = f"Edits requested for: {', '.join(sorted(edits))}."
            else:
                reason = (
                    "Plan Critic returned an empty rejection — "
                    "no reason and no edits."
                )

    out["approved"] = approved
    out["reason"] = reason
    out["edits"] = edits
    out["notes"] = notes
    return out


def normalize_plan_critic_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Public entry-point for the invariant transform.

    Exposed so tests can exercise the normaliser without driving an SDK
    client.
    """
    return _enforce_decision_invariants(raw)


def _resolve_task_id(state_slice: dict) -> str:
    return str(
        state_slice.get("task_id")
        or state_slice.get("wf_id")
        or state_slice.get("workflow_id")
        or ""
    )


async def run_plan_critic(state_slice: dict) -> dict[str, Any]:
    compose_state = ComposeState.task_only(_resolve_task_id(state_slice))
    rendered = _render_user_prompt(state_slice)
    async with compose(
        "plan_critic",
        compose_state,
        task_id=compose_state.task_id,
    ) as client:
        await client.query(rendered)
        _text, structured, _result = await _drain(client)
    if structured is None:
        raise ParseError("Plan Critic emitted no structured output")
    return normalize_plan_critic_output(structured)
