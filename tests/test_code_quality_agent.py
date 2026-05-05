"""Unit tests for the Code Quality SDK role (M4-1)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from darkfactory.agents import code_quality as code_quality_mod
from darkfactory.agents.code_quality import (
    make_code_quality_client,
    run_code_quality,
)
from darkfactory.state import CodeQualitySummary


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="fake-model")


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason="end_turn",
    )


class _FakeClient:
    def __init__(self, responses: list[list[Any]]) -> None:
        self._responses = list(responses)
        self.queries: list[str] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
        self.queries.append(prompt)

    def receive_response(self) -> AsyncIterator[Any]:
        if not self._responses:
            raise AssertionError("FakeClient: no more scripted responses")
        batch = self._responses.pop(0)

        async def _gen() -> AsyncIterator[Any]:
            for msg in batch:
                yield msg

        return _gen()


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    def _make(_state_slice: dict) -> _FakeClient:
        return fake

    monkeypatch.setattr(code_quality_mod, "make_code_quality_client", _make)


def _state_slice() -> dict:
    return {
        "user_request": "Add cursor pagination",
        "patches": [
            {
                "path": "src/main/java/app/UserController.java",
                "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
                "author_agent": "backend",
                "slice_id": "US-1",
            }
        ],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
        "test_results": [],
        "findings": [],
    }


def test_code_quality_client_options_are_hermetic_and_no_tool() -> None:
    client = make_code_quality_client({"user_request": "x"})
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == []
    assert opts.model == "claude-haiku-4-5-20251001"
    assert opts.temperature == 0.2
    assert opts.thinking is not None
    assert opts.thinking["type"] == "disabled"
    assert "PreToolUse" in opts.hooks
    assert "PostToolUse" not in opts.hooks  # reasoning-only role: no post-tool hooks
    assert "UserPromptSubmit" in opts.hooks


def test_run_code_quality_returns_structured_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "severity": "low",
            "issues": [],
            "recommendation": "approve",
        }
    )
    fake = _FakeClient([[_assistant(payload), _result()]])
    _patch_client(monkeypatch, fake)

    out = asyncio.run(run_code_quality(_state_slice()))

    assert isinstance(out, CodeQualitySummary)
    assert out.severity == "low"
    assert out.issues == []
    assert out.recommendation == "approve"
    assert len(fake.queries) == 1
    assert "Review the implementation for merge readiness." in fake.queries[0]
    assert "UserController.java" in fake.queries[0]
