"""M2-3: `run_to_completion` happy / parse-retry / exhausted-retry paths."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from pydantic import BaseModel

from darkfactory.agents._sdk_common import (
    ParseError,
    _unwrap_singleton,
    run_to_completion,
)


class Greeting(BaseModel):
    hello: str
    n: int


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


def _structured_assistant(payload: dict) -> AssistantMessage:
    """An assistant turn that emits the SDK's synthetic StructuredOutput tool."""
    return AssistantMessage(
        content=[
            ToolUseBlock(
                id="toolu_struct_1",
                name="StructuredOutput",
                input=payload,
            )
        ],
        model="fake-model",
        usage=None,
        message_id="msg-struct-1",
        session_id="session-1",
        stop_reason="end_turn",
    )


def test_run_to_completion_reads_structured_output_tool_block() -> None:
    payload = {"hello": "structured", "n": 9}
    client = FakeClient([[_structured_assistant(payload), _result()]])

    out = asyncio.run(run_to_completion(client, expect=Greeting))  # type: ignore[arg-type]

    assert isinstance(out, Greeting)
    assert out.hello == "structured"
    assert out.n == 9
    assert client.queries == []  # no retry needed


def test_run_to_completion_prefers_structured_block_over_text() -> None:
    """If both arrive, the tool-block payload wins — text is the model's
    narration, the tool input is the actual structured output."""
    text_payload = '{"hello": "narration", "n": 1}'
    struct_payload = {"hello": "structured", "n": 2}
    msg = AssistantMessage(
        content=[
            TextBlock(text=text_payload),
            ToolUseBlock(
                id="toolu_struct_2",
                name="StructuredOutput",
                input=struct_payload,
            ),
        ],
        model="fake-model",
        usage=None,
        message_id="msg-mixed",
        session_id="session-1",
        stop_reason="end_turn",
    )
    client = FakeClient([[msg, _result()]])

    out = asyncio.run(run_to_completion(client, expect=Greeting))  # type: ignore[arg-type]
    assert isinstance(out, Greeting)
    assert out.hello == "structured"


def test_run_to_completion_without_schema_returns_text_and_result() -> None:
    client = FakeClient([[_assistant("free-form answer"), _result()]])

    out = asyncio.run(run_to_completion(client))  # type: ignore[arg-type]

    assert isinstance(out, dict)
    assert out["text"] == "free-form answer"
    assert isinstance(out["result"], ResultMessage)


# ---------- _unwrap_singleton ----------


def test_unwrap_singleton_drops_outer_key() -> None:
    wrapped = {"output": {"hello": "world", "n": 1}}
    assert _unwrap_singleton(wrapped) == {"hello": "world", "n": 1}


def test_unwrap_singleton_drops_arbitrary_single_outer_key() -> None:
    wrapped = {"payload": {"approved": True, "reason": ""}}
    assert _unwrap_singleton(wrapped) == {"approved": True, "reason": ""}


def test_unwrap_singleton_preserves_multi_key_dict() -> None:
    direct = {"hello": "world", "n": 1}
    assert _unwrap_singleton(direct) == direct


def test_unwrap_singleton_preserves_singleton_with_scalar_value() -> None:
    # Real schemas can have one-required-field shapes (e.g. predicate_coverage);
    # only unwrap when the inner value is itself a dict.
    direct = {"predicate_coverage": [{"id": "p1"}]}
    assert _unwrap_singleton(direct) == direct


def test_unwrap_singleton_handles_none_and_empty() -> None:
    assert _unwrap_singleton(None) is None
    assert _unwrap_singleton({}) == {}


def test_drain_unwraps_wrapped_structured_output() -> None:
    from darkfactory.agents._sdk_common import _drain

    wrapped_input = {"output": {"hello": "world", "n": 1}}
    tool_use = ToolUseBlock(
        id="t1", name="StructuredOutput", input=wrapped_input
    )
    msg = AssistantMessage(
        content=[tool_use],
        model="fake-model",
        usage=None,
        message_id="msg-1",
        session_id="session-1",
        stop_reason="end_turn",
    )
    client = FakeClient([[msg, _result()]])

    text, structured, result = asyncio.run(_drain(client))  # type: ignore[arg-type]

    assert structured == {"hello": "world", "n": 1}
    assert isinstance(result, ResultMessage)
    assert text == ""
