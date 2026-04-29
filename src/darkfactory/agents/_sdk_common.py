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

from dataclasses import dataclass
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
from opentelemetry import trace
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
_GENERATION_TRACER = "darkfactory.sdk.generation"


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


@dataclass(frozen=True)
class _GenerationObservation:
    model: str | None
    usage: dict[str, Any] | None
    message_id: str | None
    session_id: str | None
    stop_reason: str | None


def _int_usage(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _normalise_usage_key(key: str) -> str:
    aliases = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "cacheReadInputTokens": "cache_read_input_tokens",
        "cacheCreationInputTokens": "cache_creation_input_tokens",
    }
    return aliases.get(key, key)


def _usage_details(usage: dict[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}

    details: dict[str, int] = {}
    for raw_key, value in usage.items():
        key = _normalise_usage_key(raw_key)
        numeric = _int_usage(value)
        if numeric is None:
            continue
        if key == "costUSD":
            continue
        if key.endswith("_tokens"):
            details[key[: -len("_tokens")]] = numeric
        else:
            details[key] = numeric

    if "total" not in details:
        total = sum(value for key, value in details.items() if key != "total")
        if total:
            details["total"] = total
    return details


def _usage_token(usage: dict[str, Any] | None, key: str) -> int | None:
    if not usage:
        return None
    value = usage.get(key)
    if value is None:
        camel_aliases = {
            "input_tokens": "inputTokens",
            "output_tokens": "outputTokens",
        }
        value = usage.get(camel_aliases.get(key, key))
    return _int_usage(value)


def _usage_cost(usage: dict[str, Any] | None) -> float | None:
    if not usage:
        return None
    value = usage.get("costUSD")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _model_usage_observations(final: ResultMessage) -> list[_GenerationObservation]:
    if not final.model_usage:
        return []

    observations: list[_GenerationObservation] = []
    for model, usage in final.model_usage.items():
        if not isinstance(usage, dict):
            continue
        observations.append(
            _GenerationObservation(
                model=str(model),
                usage=usage,
                message_id=final.uuid,
                session_id=final.session_id,
                stop_reason=final.stop_reason,
            )
        )
    return observations


def _emit_generation_span(
    observation: _GenerationObservation,
    *,
    cost_usd: float | None,
    index: int,
) -> None:
    usage_details = _usage_details(observation.usage)
    if not (observation.model or usage_details or cost_usd is not None):
        return

    tracer = trace.get_tracer(_GENERATION_TRACER)
    with tracer.start_as_current_span("gen_ai.claude_code") as span:
        span.set_attribute("langfuse.observation.type", "generation")
        span.set_attribute("gen_ai.system", "anthropic")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("darkfactory.generation.index", index)
        if observation.model:
            span.set_attribute("gen_ai.request.model", observation.model)
            span.set_attribute("gen_ai.response.model", observation.model)
            span.set_attribute("langfuse.observation.model.name", observation.model)
        if observation.message_id:
            span.set_attribute("gen_ai.response.id", observation.message_id)
        if observation.session_id:
            span.set_attribute(
                "langfuse.observation.metadata.claude_session_id",
                observation.session_id,
            )
        if observation.stop_reason:
            span.set_attribute("gen_ai.response.finish_reasons", observation.stop_reason)

        input_tokens = _usage_token(observation.usage, "input_tokens")
        output_tokens = _usage_token(observation.usage, "output_tokens")
        if input_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        if output_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        if usage_details:
            span.set_attribute(
                "langfuse.observation.usage_details",
                json.dumps(usage_details, sort_keys=True),
            )
        if cost_usd is not None:
            span.set_attribute("gen_ai.usage.cost", cost_usd)
            span.set_attribute(
                "langfuse.observation.cost_details",
                json.dumps({"total": cost_usd}, sort_keys=True),
            )


def _emit_generation_spans(
    observations: list[_GenerationObservation],
    final: ResultMessage | None,
) -> None:
    if final is None:
        for index, observation in enumerate(observations):
            _emit_generation_span(observation, cost_usd=None, index=index)
        return

    final_observations = observations or _model_usage_observations(final)
    if not final_observations and (final.usage or final.total_cost_usd is not None):
        final_observations = [
            _GenerationObservation(
                model=None,
                usage=final.usage,
                message_id=final.uuid,
                session_id=final.session_id,
                stop_reason=final.stop_reason,
            )
        ]

    last_index = len(final_observations) - 1
    for index, observation in enumerate(final_observations):
        usage = observation.usage
        if usage is None and index == last_index:
            usage = final.usage
        enriched = _GenerationObservation(
            model=observation.model,
            usage=usage,
            message_id=observation.message_id or final.uuid,
            session_id=observation.session_id or final.session_id,
            stop_reason=observation.stop_reason or final.stop_reason,
        )
        cost = (
            final.total_cost_usd
            if index == last_index and final.total_cost_usd is not None
            else _usage_cost(enriched.usage)
        )
        _emit_generation_span(enriched, cost_usd=cost, index=index)


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
    observations: list[_GenerationObservation] = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            last_text_chunks = [
                block.text for block in msg.content if isinstance(block, TextBlock)
            ]
            observations.append(
                _GenerationObservation(
                    model=msg.model,
                    usage=msg.usage,
                    message_id=msg.message_id or msg.uuid,
                    session_id=msg.session_id,
                    stop_reason=msg.stop_reason,
                )
            )
        elif isinstance(msg, ResultMessage):
            final = msg
    _emit_generation_spans(observations, final)
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
