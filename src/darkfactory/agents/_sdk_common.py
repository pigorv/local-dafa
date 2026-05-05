"""Common helpers for SDK-driven role agents.

`run_to_completion` drives a `ClaudeSDKClient` turn until the assistant emits
its final message, then (optionally) extracts a Pydantic-typed structured
output from that message. On `ValidationError` it sends one re-prompt asking
for a clean JSON object matching the schema; if the second attempt still
fails it raises `ParseError`.

`load_prompt` reads a system-prompt markdown file from `darkfactory/prompts/`.

`WorkerOutput` is the shared return type of `run_<role>` for the build-stage
workers (backend, database, unit_test). The patches list is populated by the
`diff_capture` PostToolUse hook the role attaches to its SDK client, not by
parsing the assistant's final message — workers commit code through `Edit` /
`Write`, not by emitting a structured JSON blob.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, TypeVar

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)
from pydantic import BaseModel, Field, ValidationError

T = TypeVar("T", bound=BaseModel)


class WorkerOutput(BaseModel):
    """Result of running a build-stage worker (backend / database / unit_test).

    ``patches`` is the list of ``Patch`` TypedDicts captured by the
    ``diff_capture`` hook over the role's SDK loop. ``summary`` is the final
    assistant text (typically the worker's one-paragraph summary). The build
    subgraph node folds ``patches`` straight into the ``patches`` channel of
    the pipeline state via the ``add`` reducer in ``state.py``.
    """

    patches: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class ParseError(Exception):
    """Raised when a role's structured output cannot be validated after one retry."""


def load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def repo_summary(repo_context: dict | None) -> str:
    """Summarise hydrator output for inclusion in a discovery role's user prompt."""
    if not repo_context:
        return "(no repo context)"
    parts: list[str] = []
    agents_md = repo_context.get("agents_md") or ""
    if agents_md:
        parts.append(f"AGENTS.md:\n{agents_md[:2000]}")
    repo_map = repo_context.get("repo_map") or ""
    if repo_map:
        parts.append(f"Repo map:\n{repo_map[:2000]}")
    git_log = repo_context.get("git_log") or []
    if git_log:
        parts.append("Recent commits:\n" + "\n".join(git_log[:10]))
    return "\n\n".join(parts) if parts else "(empty repo)"


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def _extract_json(text: str) -> str | None:
    """Return the first JSON object found in `text`, or None.

    Looks for a fenced ```json ... ``` block first, then scans for a balanced
    top-level `{...}` substring (string-aware, escape-aware).
    """
    fenced = _FENCED_JSON_RE.search(text)
    if fenced:
        return fenced.group(1).strip()

    depth = 0
    start: int | None = None
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    return text[start : i + 1]
    return None


async def _drain(client: ClaudeSDKClient) -> tuple[str, ResultMessage | None]:
    """Iterate `receive_response()` to exhaustion; return (last assistant text, ResultMessage)."""
    last_text_chunks: list[str] = []
    final: ResultMessage | None = None
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            last_text_chunks = [
                block.text for block in msg.content if isinstance(block, TextBlock)
            ]
        elif isinstance(msg, ResultMessage):
            final = msg
    return "".join(last_text_chunks), final


async def run_to_completion(
    client: ClaudeSDKClient,
    *,
    expect: type[T] | None = None,
) -> dict | T:
    """Drive the SDK client to completion; parse + validate against `expect` if given.

    Without ``expect``: returns ``{"text": <last assistant text>, "result": <ResultMessage|None>}``.

    With ``expect``: extracts a JSON block from the final assistant message and
    validates it against the Pydantic model. If parsing or validation fails, the
    function sends a single re-prompt requesting a JSON object that matches the
    model's schema and tries once more. On a second failure it raises `ParseError`.
    """
    text, result = await _drain(client)

    if expect is None:
        return {"text": text, "result": result}

    raw = _extract_json(text)
    if raw is not None:
        try:
            return expect.model_validate_json(raw)
        except ValidationError:
            pass

    schema = json.dumps(expect.model_json_schema())
    await client.query(
        "Your previous response could not be parsed as the expected structured "
        "output. Reply with a SINGLE JSON object that validates against this "
        "JSON Schema. Do not include code fences, commentary, or preamble — "
        "only the raw JSON object.\n\nSchema:\n" + schema
    )
    text, _ = await _drain(client)
    raw = _extract_json(text) or text.strip()
    try:
        return expect.model_validate_json(raw)
    except ValidationError as exc:
        raise ParseError(
            f"Could not validate {expect.__name__} after one retry: {exc}"
        ) from exc


def _resolve_slice(state_slice: dict) -> dict:
    """Return the SpecSlice for ``current_slice`` or an empty dict.

    Build-stage workers expect the activity to thread both the spec list and
    the active slice id through ``state_slice``. We look up by ``story_id``;
    if no match is found (e.g. an ad-hoc test with only ``current_slice``),
    return an empty dict so the user message is well-formed.
    """
    slice_id = state_slice.get("current_slice") or ""
    for s in state_slice.get("spec") or []:
        if isinstance(s, dict) and s.get("story_id") == slice_id:
            return s
    return {}


def worker_user_message(state_slice: dict) -> str:
    """Format the per-turn user message for a build-stage worker.

    The role-specific instructions live in the system prompt; the user
    message just hands the worker the SpecSlice it should execute and a
    short reminder of what "done" looks like.
    """
    slice_ = _resolve_slice(state_slice)
    return (
        f"SpecSlice (JSON):\n{json.dumps(slice_, indent=2)}\n\n"
        "Execute this slice end-to-end. Read the affected files first, make "
        "the minimal change, run the relevant build / test command via "
        "sandbox_bash to verify, then commit via sandbox_bash. Stay inside "
        "the listed paths."
    )
