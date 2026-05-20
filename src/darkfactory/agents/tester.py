"""Tester Worker — single generalist test-writing role.

Owns test code; reads the Builder's diff for shapes only and derives
assertions from the WP's ``verification`` predicate (the diff-blindness rule).

The output shape is defined by ``schemas/tester_output.json`` and enforced
by the SDK's ``output_format``; this module does not declare a Pydantic
schema (matches the PO/Architect pattern). The Tester does not declare
test patches — the build subgraph computes them from ``git diff`` after
the Tester's turn, exactly like it does for Builder (PR C).

Production-code edits are restricted to mechanical rename / import /
signature alignment. Anything semantic returns a ``behavior_mismatch``
finding; the strict Fixer handles those.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from darkfactory.agents._sdk_common import (
    _drain,
    render_role_user_message,
    repo_summary,
    role_turn_span,
    stamp_turn_usage,
)
from darkfactory.agents.compose import ComposeState, compose
from opentelemetry import trace

log = logging.getLogger(__name__)

ROLE = "tester"


def _resolve_work_package(state_slice: dict) -> dict:
    slice_id = state_slice.get("current_slice") or ""
    for s in state_slice.get("spec") or []:
        if isinstance(s, dict) and s.get("story_id") == slice_id:
            return s
    return {}


def _builder_signal(state_slice: dict) -> str:
    """Render the Builder's patches and summary for *this* WP as a JSON block.

    The Tester reads this to learn shapes only — it must not derive
    assertions from the content.
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


def _render_user_prompt(state_slice: dict) -> str:
    return render_role_user_message(
        ROLE,
        user_request=state_slice.get("user_request", "") or "",
        repo_context=repo_summary(state_slice.get("repo_context")),
        implementation_brief=json.dumps(
            state_slice.get("implementation_brief") or {}, indent=2
        ),
        work_package=json.dumps(_resolve_work_package(state_slice), indent=2),
        builder_signal=_builder_signal(state_slice),
    )


async def run_tester(state_slice: dict) -> dict[str, Any]:
    """Run the Tester for the active Work Package and return a result dict.

    Returns ``{summary, coverage, findings, parse_failure}``. The
    ``parse_failure`` flag is ``True`` when the SDK loop emitted no
    structured output (the agent did not respond through the
    ``StructuredOutput`` tool) — the build subgraph translates that into
    a ``reconciliation_findings`` entry of kind ``tester_parse_failure``
    so the channel attribution stays clean (the Tester's ``findings``
    array only ever holds Tester-declared findings).
    """
    compose_state = ComposeState.from_mapping(state_slice)
    slice_id = state_slice.get("current_slice") or ""
    async with role_turn_span(ROLE, wp_id=slice_id or None):
        rendered = _render_user_prompt(state_slice)
        async with compose(
            ROLE,
            compose_state,
            task_id=compose_state.task_id,
        ) as client:
            await client.query(rendered)
            _text, structured, _result = await _drain(client)
            stamp_turn_usage(trace.get_current_span(), _result)

    if structured is None:
        return {
            "summary": "",
            "coverage": [],
            "findings": [],
            "parse_failure": True,
        }
    return {
        "summary": structured.get("summary", "") or "",
        "coverage": list(structured.get("coverage") or []),
        "findings": list(structured.get("findings") or []),
        "parse_failure": False,
    }
