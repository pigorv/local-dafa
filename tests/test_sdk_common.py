"""M2-3: `run_to_completion` happy / parse-retry / exhausted-retry paths."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import ProxyTracerProvider
from pydantic import BaseModel

from darkfactory.agents._sdk_common import ParseError, run_to_completion


class Greeting(BaseModel):
    hello: str
    n: int


_exporter = InMemorySpanExporter()
_processor_attached = False


def _ensure_in_memory_exporter() -> None:
    global _processor_attached
    if _processor_attached:
        return

    provider = trace.get_tracer_provider()
    if isinstance(provider, ProxyTracerProvider):
        new_provider = TracerProvider()
        new_provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(new_provider)
    elif isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
    _processor_attached = True


@pytest.fixture(autouse=True)
def _clean_spans() -> None:
    _ensure_in_memory_exporter()
    _exporter.clear()
    yield


def _assistant(
    text: str,
    *,
    usage: dict | None = None,
    model: str = "fake-model",
) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model=model,
        usage=usage,
        message_id="msg-1",
        session_id="session-1",
        stop_reason="end_turn",
    )


def _result(
    *,
    usage: dict | None = None,
    total_cost_usd: float | None = None,
    model_usage: dict | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason="end_turn",
        usage=usage,
        total_cost_usd=total_cost_usd,
        model_usage=model_usage,
        uuid="result-1",
    )


class FakeClient:
    """Stub that emits a scripted sequence of `receive_response()` runs.

    Each call to `receive_response()` consumes the next batch from `responses`.
    `query()` records prompts so tests can assert the retry prompt fired.
    """

    def __init__(self, responses: list[list[object]]) -> None:
        self._responses = list(responses)
        self.queries: list[str] = []

    async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
        self.queries.append(prompt)

    def receive_response(self) -> AsyncIterator[object]:
        if not self._responses:
            raise AssertionError("FakeClient: no more scripted responses")
        batch = self._responses.pop(0)

        async def _gen() -> AsyncIterator[object]:
            for msg in batch:
                yield msg

        return _gen()


def test_run_to_completion_happy_path_returns_typed_output() -> None:
    payload = '{"hello": "world", "n": 3}'
    client = FakeClient([[_assistant(payload), _result()]])

    out = asyncio.run(run_to_completion(client, expect=Greeting))  # type: ignore[arg-type]

    assert isinstance(out, Greeting)
    assert out.hello == "world"
    assert out.n == 3
    assert client.queries == []  # no retry needed


def test_run_to_completion_extracts_fenced_json() -> None:
    text = "Here is the answer:\n```json\n{\"hello\": \"fence\", \"n\": 1}\n```\n"
    client = FakeClient([[_assistant(text), _result()]])

    out = asyncio.run(run_to_completion(client, expect=Greeting))  # type: ignore[arg-type]
    assert isinstance(out, Greeting) and out.hello == "fence"


def test_run_to_completion_retries_once_on_validation_error() -> None:
    bad = '{"hello": "world"}'  # missing required `n`
    good = '{"hello": "world", "n": 7}'
    client = FakeClient(
        [
            [_assistant(bad), _result()],
            [_assistant(good), _result()],
        ]
    )

    out = asyncio.run(run_to_completion(client, expect=Greeting))  # type: ignore[arg-type]

    assert isinstance(out, Greeting) and out.n == 7
    assert len(client.queries) == 1
    assert "JSON Schema" in client.queries[0]


def test_run_to_completion_raises_parse_error_after_one_retry() -> None:
    bad1 = "no json here at all"
    bad2 = '{"hello": "world"}'  # still invalid (missing n)
    client = FakeClient(
        [
            [_assistant(bad1), _result()],
            [_assistant(bad2), _result()],
        ]
    )

    with pytest.raises(ParseError):
        asyncio.run(run_to_completion(client, expect=Greeting))  # type: ignore[arg-type]
    assert len(client.queries) == 1


def test_run_to_completion_without_schema_returns_text_and_result() -> None:
    client = FakeClient([[_assistant("free-form answer"), _result()]])

    out = asyncio.run(run_to_completion(client))  # type: ignore[arg-type]

    assert isinstance(out, dict)
    assert out["text"] == "free-form answer"
    assert isinstance(out["result"], ResultMessage)


def test_run_to_completion_emits_langfuse_generation_usage_and_cost_span() -> None:
    client = FakeClient(
        [
            [
                _assistant(
                    "free-form answer",
                    usage={"input_tokens": 11, "output_tokens": 7},
                    model="claude-test-model",
                ),
                _result(total_cost_usd=0.0012),
            ]
        ]
    )

    out = asyncio.run(run_to_completion(client))  # type: ignore[arg-type]

    assert isinstance(out, dict)
    spans = [
        span for span in _exporter.get_finished_spans() if span.name == "gen_ai.claude_code"
    ]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["langfuse.observation.type"] == "generation"
    assert attrs["langfuse.observation.model.name"] == "claude-test-model"
    assert attrs["gen_ai.request.model"] == "claude-test-model"
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs["gen_ai.usage.cost"] == 0.0012
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {
        "input": 11,
        "output": 7,
        "total": 18,
    }
    assert json.loads(attrs["langfuse.observation.cost_details"]) == {
        "total": 0.0012
    }
