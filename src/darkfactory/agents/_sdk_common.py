"""Common helpers for SDK-driven role agents.

`run_to_completion` drives a `ClaudeSDKClient` turn until the assistant emits
its final message, then (optionally) extracts a Pydantic-typed structured
output from that message. On `ValidationError` it sends one re-prompt asking
for a clean JSON object matching the schema; if the second attempt still
fails it raises `ParseError`.

`load_prompt` reads a system-prompt markdown file from `darkfactory/prompts/`.

`BuilderOutput` / `BuilderEdit` mirror the JSON schema enforced on the
Builder's structured output. Patches themselves are computed by the
build subgraph from `git diff`, not by parsing the assistant's
response — workers commit code through `Edit` / `Write` / `Bash`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, TypeVar

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)
from claude_agent_sdk.types import ToolUseBlock
from pydantic import BaseModel, Field, ValidationError

STRUCTURED_OUTPUT_TOOL_NAME = "StructuredOutput"

T = TypeVar("T", bound=BaseModel)


class BuilderEdit(BaseModel):
    """One file edit declared by the Builder.

    Mirrors a single entry in ``schemas/builder_output.json#/properties/edits``.
    The Builder reports the path, the kind of edit, and a one-sentence
    intent so reviewers and the reconciliation step can trace each edit
    back to the brief or a verification predicate.
    """

    path: str
    operation: Literal["create", "modify", "delete"]
    intent: str


class BuilderOutput(BaseModel):
    """Structured Builder report for one Work Package turn.

    Mirrors ``schemas/builder_output.json``. The Builder agent emits this
    directly via the SDK's ``output_format``; the build subgraph reads
    ``status`` to route the turn (done / no_changes_needed / blocked)
    and reconciles ``edits`` against the ground-truth ``git diff`` to
    detect claimed-but-not-applied or undeclared changes.
    """

    wp_id: str
    status: Literal["done", "no_changes_needed", "blocked"]
    edits: list[BuilderEdit] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    summary: str = ""


class PRCreatorOutput(BaseModel):
    """Structured PR Creator report.

    Mirrors ``schemas/pr_creator_output.json``. ``status`` distinguishes a
    freshly opened PR from one the agent discovered already existed; the
    activity records both shapes through the same ``pr_url`` channel.
    """

    status: Literal["created", "existing"]
    pr_url: str
    summary: str = ""

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class ParseError(Exception):
    """Raised when a role's structured output cannot be validated after one retry."""


def load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


_REPO_SUMMARY_SECTIONS: tuple[str, ...] = (
    "agents_md",
    "repo_map",
    "style_configs",
    "git_log",
)


def repo_summary(
    repo_context: dict | None,
    *,
    include: tuple[str, ...] | None = None,
) -> str:
    """Summarise hydrator output for inclusion in a role's user prompt.

    ``include`` selects which sections to render; defaults to all four
    (``agents_md``, ``repo_map``, ``style_configs``, ``git_log``). Builder
    drops ``agents_md`` and ``git_log`` since the model can ``Read`` those
    on demand and the genuine value lives in the synthesized ``repo_map``
    plus ``style_configs``.
    """
    if not repo_context:
        return "(no repo context)"
    sections = tuple(include) if include is not None else _REPO_SUMMARY_SECTIONS
    parts: list[str] = []
    if "agents_md" in sections:
        agents_md = repo_context.get("agents_md") or ""
        if agents_md:
            parts.append(f"AGENTS.md:\n{agents_md[:2000]}")
    if "repo_map" in sections:
        repo_map = repo_context.get("repo_map") or ""
        if repo_map:
            parts.append(f"Repo map:\n{repo_map[:2000]}")
    if "style_configs" in sections:
        style_configs = repo_context.get("style_configs") or []
        if style_configs:
            rendered: list[str] = ["Style / lint configs (match these rules in new files):"]
            for entry in style_configs:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("path") or ""
                content = entry.get("content") or ""
                if path:
                    rendered.append(f"--- {path} ---")
                if content:
                    rendered.append(content)
            parts.append("\n".join(rendered))
    if "git_log" in sections:
        git_log = repo_context.get("git_log") or []
        if git_log:
            parts.append("Recent commits:\n" + "\n".join(git_log[:10]))
    return "\n\n".join(parts) if parts else "(empty repo)"


def original_user_request(state_slice: dict) -> str:
    """Return the verbatim user request.

    For issue-driven runs, returns ``"Title: <title>\n\n<body>"`` from
    ``state["issue"]`` — the raw text triage saw, before it summarized.
    For CLI runs (no ``issue`` field), falls back to
    ``state["user_request"]``, which is already the verbatim CLI prompt
    since no triage stage overwrites it.
    """
    issue = state_slice.get("issue")
    if issue is not None:
        title = getattr(issue, "title", None)
        body = getattr(issue, "body", None)
        if title is None and isinstance(issue, dict):
            title = issue.get("title")
            body = issue.get("body")
        title = (title or "").strip()
        body = (body or "").strip()
        if title and body:
            return f"Title: {title}\n\n{body}"
        return title or body
    return str(state_slice.get("user_request") or "")


def render_role_user_message(role: str, **substitutions: object) -> str:
    """Render a role's prompt file as a ``string.Template`` user message.

    Looks up the role in the default registry, resolves the prompt path,
    reads the file, and runs ``Template.safe_substitute`` over it. Shared
    by every role that uses ``prompt_as_user_message: true`` (PO,
    Architect, Builder) so the contract — and any future changes to it —
    live in one place.
    """
    from darkfactory.agents.registry import get_default_registry, resolve_prompt_path

    manifest = get_default_registry().get(role)
    template_text = resolve_prompt_path(manifest.llm.prompt_path).read_text(
        encoding="utf-8"
    )
    from string import Template

    return Template(template_text).safe_substitute(**substitutions)


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


def _unwrap_singleton(structured: dict[str, Any] | None) -> dict[str, Any] | None:
    """If ``structured`` has exactly one key and that value is a dict, return the inner dict.

    Handles the recurring SDK structured-output failure mode where the model
    wraps the payload in ``{"output": {...}}`` (or any other single outer
    key). Returns the original value in every other case, including ``None``.
    """
    if not isinstance(structured, dict) or len(structured) != 1:
        return structured
    only_value = next(iter(structured.values()))
    return only_value if isinstance(only_value, dict) else structured


async def _drain(
    client: ClaudeSDKClient,
) -> tuple[str, dict[str, Any] | None, ResultMessage | None]:
    """Iterate `receive_response()` to exhaustion.

    Returns ``(last_assistant_text, structured_output, ResultMessage)``.

    ``structured_output`` is the ``input`` of the most recent
    ``ToolUseBlock`` named ``StructuredOutput`` — the SDK implements
    ``ClaudeAgentOptions.output_format`` as a synthetic tool, so when the
    caller declared a JSON Schema the model's structured response arrives
    here rather than in a ``TextBlock``.
    """
    last_text_chunks: list[str] = []
    structured: dict[str, Any] | None = None
    final: ResultMessage | None = None
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            text_chunks: list[str] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_chunks.append(block.text)
                elif (
                    isinstance(block, ToolUseBlock)
                    and block.name == STRUCTURED_OUTPUT_TOOL_NAME
                    and isinstance(block.input, dict)
                ):
                    structured = _unwrap_singleton(block.input)
            if text_chunks:
                last_text_chunks = text_chunks
        elif isinstance(msg, ResultMessage):
            final = msg
    return "".join(last_text_chunks), structured, final


async def run_to_completion(
    client: ClaudeSDKClient,
    *,
    expect: type[T] | None = None,
) -> dict | T:
    """Drive the SDK client to completion; parse + validate against `expect` if given.

    Without ``expect``: returns ``{"text": <last assistant text>, "result": <ResultMessage|None>}``.

    With ``expect``: returns the validated Pydantic model. The structured
    output is preferred from a ``StructuredOutput`` tool-use block (emitted
    when ``output_format`` is set on the SDK options) and falls back to a
    JSON object scraped out of the assistant text. On parse / validation
    failure the function sends a single re-prompt requesting a JSON object
    that matches the model's schema and tries once more. On a second
    failure it raises ``ParseError``.
    """
    text, structured, result = await _drain(client)

    if expect is None:
        return {"text": text, "result": result}

    if structured is not None:
        try:
            return expect.model_validate(structured)
        except ValidationError:
            pass

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
    text, structured, _ = await _drain(client)
    if structured is not None:
        try:
            return expect.model_validate(structured)
        except ValidationError:
            pass
    raw = _extract_json(text) or text.strip()
    try:
        return expect.model_validate_json(raw)
    except ValidationError as exc:
        raise ParseError(
            f"Could not validate {expect.__name__} after one retry: {exc}"
        ) from exc
