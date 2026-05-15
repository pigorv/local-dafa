"""Semantic Verifier — reasoning-only predicate coverage role.

The output shape is defined by ``schemas/verifier_semantic_output.json``
and enforced by the SDK's ``output_format``; this module does not declare
a Pydantic schema (matches the PO / Architect / Plan Critic / Tester
pattern). The runtime drains the SDK loop and returns the structured
payload as a dict.
"""
from __future__ import annotations

import json
from typing import Any

from darkfactory.agents._sdk_common import (
    _drain,
    render_role_user_message,
    role_turn_span,
    stamp_turn_usage,
)
from darkfactory.agents.compose import ComposeState, compose
from opentelemetry import trace

ROLE = "verifier_semantic"


def _test_files_from_spec(spec: list[Any]) -> list[str]:
    files: list[str] = []
    for item in spec:
        if not isinstance(item, dict):
            continue
        for path in item.get("test_files") or []:
            if path not in files:
                files.append(path)
    return files


def _resolve_task_id(state_slice: dict) -> str:
    return str(
        state_slice.get("task_id")
        or state_slice.get("wf_id")
        or state_slice.get("workflow_id")
        or ""
    )


def _jsonable(value: Any) -> Any:
    """Coerce Pydantic / dataclass-like values into JSON-dumpable shape."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _render_user_prompt(state_slice: dict) -> str:
    spec = state_slice.get("spec") or []
    test_files = state_slice.get("test_files") or _test_files_from_spec(spec)
    return render_role_user_message(
        ROLE,
        user_request=state_slice.get("user_request", "") or "",
        implementation_brief=json.dumps(
            _jsonable(state_slice.get("implementation_brief") or {}), indent=2
        ),
        spec=json.dumps(_jsonable(spec), indent=2),
        coverage_entries=json.dumps(
            _jsonable(state_slice.get("coverage_entries") or []), indent=2
        ),
        test_files=json.dumps(_jsonable(test_files), indent=2),
        test_results=json.dumps(
            _jsonable(state_slice.get("test_results") or []), indent=2
        ),
        findings=json.dumps(_jsonable(state_slice.get("findings") or []), indent=2),
        tester_findings=json.dumps(
            _jsonable(state_slice.get("tester_findings") or []), indent=2
        ),
        builder_outputs=json.dumps(
            _jsonable(state_slice.get("builder_outputs") or []), indent=2
        ),
        reconciliation_findings=json.dumps(
            _jsonable(state_slice.get("reconciliation_findings") or []), indent=2
        ),
    )


async def run_verifier_semantic(state_slice: dict) -> dict[str, Any]:
    compose_state = ComposeState.task_only(_resolve_task_id(state_slice))
    rendered = _render_user_prompt(state_slice)
    async with role_turn_span(ROLE):
        async with compose(
            ROLE,
            compose_state,
            task_id=compose_state.task_id,
        ) as client:
            await client.query(rendered)
            _text, structured, _result = await _drain(client)
            stamp_turn_usage(trace.get_current_span(), _result)
    if structured is None:
        return {"predicate_coverage": []}
    return {"predicate_coverage": list(structured.get("predicate_coverage") or [])}
