"""Triage role for issue-driven workflows.

The triage agent is a reasoning-only role: it does not get tools, and it
returns the structured decision consumed by the issue workflow.
"""
from __future__ import annotations

import json
import os
from typing import Any, Literal, TypedDict, cast

from anthropic import AsyncAnthropic
from pydantic import TypeAdapter, ValidationError

from darkfactory.agents._sdk_common import (
    ParseError,
    _extract_json,
    load_prompt,
    repo_summary,
)


class TriageOutput(TypedDict):
    ready_to_build: bool
    clarification_questions: list[str]
    derived_user_request: str
    confidence: Literal["low", "medium", "high"]
    rationale: str


_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_MAX_TOKENS = 1200
_TRIAGE_OUTPUT_ADAPTER = TypeAdapter(TriageOutput)


def _state_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _comment_body(comment: Any) -> str:
    if isinstance(comment, str):
        return comment
    return str(_state_value(comment, "body", "") or "")


def _user_message(state_slice: dict) -> str:
    issue = state_slice.get("issue") or {}
    title = state_slice.get("issue_title") or _state_value(issue, "title", "")
    body = state_slice.get("issue_body") or _state_value(issue, "body", "")
    comments = [
        body
        for body in (_comment_body(c) for c in state_slice.get("issue_comments") or [])
        if body
    ]
    ctx_blob = repo_summary(state_slice.get("repo_context"))
    return (
        f"issue_title:\n{title}\n\n"
        f"issue_body:\n{body}\n\n"
        f"issue_comments (JSON):\n{json.dumps(comments, indent=2)}\n\n"
        f"repo_context:\n{ctx_blob}\n\n"
        "Decide whether this issue is ready for Dark Factory to build."
    )


def _model() -> str:
    return os.getenv("LLM_TRIAGE_MODEL") or _DEFAULT_MODEL


def _temperature() -> float:
    raw = os.getenv("LLM_TRIAGE_TEMPERATURE")
    return float(raw) if raw is not None else _DEFAULT_TEMPERATURE


def _max_tokens() -> int:
    raw = os.getenv("LLM_TRIAGE_MAX_TOKENS")
    return int(raw) if raw is not None else _DEFAULT_MAX_TOKENS


def make_triage_client(state_slice: dict | None = None) -> AsyncAnthropic:  # noqa: ARG001
    """Build the Anthropic client for the triage agent.

    Worker containers receive `CLAUDE_CODE_OAUTH_TOKEN` (the same OAuth token
    Claude Code SDK uses for the other roles) but not `ANTHROPIC_API_KEY`.
    The Anthropic SDK only auto-reads `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`,
    so we wire the OAuth token in as `auth_token` (Bearer) when neither of
    those is set.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return AsyncAnthropic()
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        return AsyncAnthropic(auth_token=oauth_token)
    raise RuntimeError(
        "Triage agent requires ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or "
        "CLAUDE_CODE_OAUTH_TOKEN in the worker environment"
    )


def _response_text(message: Any) -> str:
    content = _state_value(message, "content", [])
    if isinstance(content, str):
        return content

    chunks: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            chunks.append(block)
            continue
        text = _state_value(block, "text")
        if text is not None:
            chunks.append(str(text))
    return "".join(chunks)


def _parse_triage_output(text: str) -> TriageOutput:
    raw = _extract_json(text) or text.strip()
    try:
        out = cast(TriageOutput, _TRIAGE_OUTPUT_ADAPTER.validate_json(raw))
    except ValidationError as exc:
        raise ParseError(f"Could not validate TriageOutput: {exc}") from exc
    if len(out["clarification_questions"]) > 3:
        raise ParseError("TriageOutput.clarification_questions must contain at most 3 items")
    if out["ready_to_build"] and out["clarification_questions"]:
        raise ParseError("TriageOutput must not ask questions when ready_to_build=true")
    return out


async def _create_message(client: AsyncAnthropic, messages: list[dict[str, str]]) -> Any:
    return await client.messages.create(
        model=_model(),
        max_tokens=_max_tokens(),
        temperature=_temperature(),
        system=load_prompt("triage"),
        messages=messages,
    )


async def run_triage(state_slice: dict) -> TriageOutput:
    client = make_triage_client(state_slice)
    messages = [{"role": "user", "content": _user_message(state_slice)}]
    response = await _create_message(client, messages)
    text = _response_text(response)
    try:
        return _parse_triage_output(text)
    except ParseError:
        schema = json.dumps(_TRIAGE_OUTPUT_ADAPTER.json_schema())
        messages.extend(
            [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "Your previous response could not be parsed as the expected "
                        "structured output. Reply with a SINGLE JSON object that "
                        "validates against this JSON Schema. Do not include code "
                        "fences, commentary, or preamble; only the raw JSON object.\n\n"
                        f"Schema:\n{schema}"
                    ),
                },
            ]
        )
        response = await _create_message(client, messages)
        return _parse_triage_output(_response_text(response))
