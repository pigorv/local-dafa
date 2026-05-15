"""Builder Worker — single generalist build-stage role.

Implements one Work Package end-to-end (Java sources, Flyway/SQL migrations,
fixtures — whatever the WP requires). Tests are the Tester's job; this role
does not edit anything under ``src/test/...``.

File ops route through SDK built-ins (``Read`` / ``Write`` / ``Edit``
/ ``Grep`` / ``Glob``); shell commands route through the built-in
``Bash`` tool, argv-gated by ``hooks.permission_gate``. The worker
container is the isolation boundary.

The prompt file is rendered as a ``string.Template`` and sent as the
first user message (``prompt_as_user_message: true`` in the manifest);
this matches the PO/Architect discovery pattern so the model receives
the brief (rendered as Markdown), a trimmed repo summary, and the
active Work Package at the point of injection rather than implied by
an "Inputs" section.

The Builder emits a structured ``BuilderOutput`` (schema:
``schemas/builder_output.json``) declaring its status, edits, and
blockers. The build subgraph reconciles the declared edits against the
ground-truth ``git diff`` and routes the turn via the declared
``status`` rather than parsing free-form text.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from darkfactory.agents._sdk_common import (
    BuilderOutput,
    ParseError,
    render_role_user_message,
    repo_summary,
    role_turn_span,
    run_to_completion,
)
from darkfactory.agents.compose import ComposeState, compose

log = logging.getLogger(__name__)

ROLE = "builder"

_BUILDER_REPO_SUMMARY_SECTIONS: tuple[str, ...] = ("repo_map", "style_configs")


def _resolve_work_package(state_slice: dict) -> dict:
    slice_id = state_slice.get("current_slice") or ""
    for s in state_slice.get("spec") or []:
        if isinstance(s, dict) and s.get("story_id") == slice_id:
            return s
    return {}


def _bullets(items: list[str] | None, *, empty: str = "(none)") -> str:
    cleaned = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not cleaned:
        return empty
    return "\n".join(f"- {item}" for item in cleaned)


def _contract_changes_md(contract: dict | None) -> str:
    contract = contract or {}
    out: list[str] = []
    for label, key in (("API", "api"), ("Data", "data"), ("Events", "events")):
        out.append(f"- {label}: {_bullets(contract.get(key), empty='(none)').replace(chr(10), chr(10) + '  ')}")
    return "\n".join(out)


def _work_package_headers_md(work_packages: list[dict] | None) -> str:
    if not work_packages:
        return "(none)"
    rows: list[str] = []
    for wp in work_packages:
        if not isinstance(wp, dict):
            continue
        wp_id = wp.get("id") or wp.get("story_id") or "?"
        story_id = wp.get("story_id") or "?"
        title = wp.get("title") or ""
        intent = wp.get("intent") or wp.get("approach") or ""
        rows.append(f"- **{wp_id}** ({story_id}) — {title}: {intent}")
    return "\n".join(rows) if rows else "(none)"


def _brief_as_markdown(brief: dict | None) -> str:
    """Render an ``ImplementationBrief`` dict as compact Markdown.

    Token-cheaper and easier to scan than ``json.dumps(brief, indent=2)``.
    The active Work Package gets its own dedicated ``$work_package`` slot
    in the prompt — here we only list WP headers for context.
    """
    if not brief:
        return "(none)"
    sections: list[str] = []
    problem = (brief.get("problem") or "").strip()
    sections.append(f"## Problem\n{problem or '(none)'}")
    sections.append(
        "## Expected behavior\n" + _bullets(brief.get("expected_behavior"))
    )
    current = (brief.get("current_understanding") or "").strip()
    if current:
        sections.append(f"## Current understanding\n{current}")
    design = (brief.get("proposed_design") or "").strip()
    if design:
        sections.append(f"## Proposed design\n{design}")
    sections.append(
        "## Contract changes\n" + _contract_changes_md(brief.get("contract_changes"))
    )
    risks = brief.get("compatibility_risks")
    if risks:
        sections.append("## Compatibility risks\n" + _bullets(risks))
    test_strategy = (brief.get("test_strategy") or "").strip()
    if test_strategy:
        sections.append(f"## Test strategy\n{test_strategy}")
    sections.append(
        "## Work packages\n" + _work_package_headers_md(brief.get("work_packages"))
    )
    return "\n\n".join(sections)


def _render_user_prompt(state_slice: dict) -> str:
    return render_role_user_message(
        ROLE,
        user_request=state_slice.get("user_request", "") or "",
        repo_context=repo_summary(
            state_slice.get("repo_context"),
            include=_BUILDER_REPO_SUMMARY_SECTIONS,
        ),
        implementation_brief=_brief_as_markdown(
            state_slice.get("implementation_brief")
        ),
        work_package=json.dumps(_resolve_work_package(state_slice), indent=2),
    )


async def run_builder(state_slice: dict) -> dict[str, Any]:
    """Run the Builder for the active Work Package and return a result dict.

    The returned dict is the Builder's structured output (``wp_id``,
    ``status``, ``edits``, ``blockers``, ``summary``). The build subgraph
    reads ``status`` for routing, reconciles ``edits`` against the actual
    ``git diff`` it computes itself, and folds the rest into the
    pipeline state channels.

    On structured-output parse failure we synthesise a ``status=blocked``
    BuilderOutput whose blockers explain the parse error; downstream this
    flows through the same ``reconciliation_findings`` path as an agent
    that explicitly declared itself blocked.
    """
    compose_state = ComposeState.from_mapping(state_slice)
    rendered = _render_user_prompt(state_slice)
    slice_id = state_slice.get("current_slice") or ""
    async with role_turn_span(ROLE, wp_id=slice_id or None):
        async with compose(
            ROLE,
            compose_state,
            task_id=compose_state.task_id,
        ) as client:
            await client.query(rendered)
            try:
                output = await run_to_completion(client, expect=BuilderOutput)
            except ParseError as exc:
                log.warning(
                    "builder: structured output parse failure for slice %r: %s",
                    slice_id,
                    exc,
                )
                output = BuilderOutput(
                    wp_id=slice_id,
                    status="blocked",
                    blockers=[f"Builder produced no parseable structured output: {exc}"],
                    summary="",
                )
    payload = output.model_dump()
    # The build subgraph already knows which slice it dispatched; pin the
    # wp_id to the dispatched slice so a hallucinated wp_id in the agent
    # output cannot break downstream reconciliation.
    payload["wp_id"] = slice_id or payload.get("wp_id") or ""
    return payload
