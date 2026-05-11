"""Unit tests for the Reviewer SDK role (M4-1)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from darkfactory.agents import reviewer as reviewer_mod
from darkfactory.agents._sdk_common import load_prompt
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.reviewer import run_reviewer
from darkfactory.state import ReviewerSummary


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
    """Replace the live ``compose`` call so ``run_reviewer`` opens our fake."""

    def _compose(*_args: Any, **_kwargs: Any) -> _FakeClient:
        return fake

    monkeypatch.setattr(reviewer_mod, "compose", _compose)


def _state_slice() -> dict:
    return {
        "user_request": "Add cursor pagination",
        "pr_url": "https://github.com/acme/demo/pull/42",
        "implementation_brief": {
            "problem": "User listing cannot be paged reliably.",
            "work_packages": [
                {
                    "id": "WP-1",
                    "intent": "Add cursor pagination to the users API.",
                    "verification": ["cursor pagination returns a next cursor"],
                }
            ],
        },
        "patches": [
            {
                "path": "src/main/java/app/UserController.java",
                "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
                "author_agent": "builder",
                "slice_id": "US-1",
                "justification": "WP-1 cursor response shape.",
            }
        ],
        "verify_summary": {
            "passed": True,
            "failed_tests": 0,
            "hard_findings": 0,
            "predicate_coverage": [
                {
                    "wp_id": "WP-1",
                    "predicate": "cursor pagination returns a next cursor",
                    "status": "covered",
                    "evidence": "UserControllerTest",
                }
            ],
        },
        "test_results": [],
        "findings": [],
        "audit_log": [{"path": "src/main/java/app/UserController.java"}],
        "attempt_log": [{"source": "fixer_attempt", "target_wp": "WP-1"}],
    }


def test_reviewer_client_options_are_hermetic_and_no_tool() -> None:
    state = ComposeState.from_mapping({"user_request": "x"})
    client = compose("reviewer", state, task_id=state.task_id)
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


def test_reviewer_prompt_uses_trace_based_scope_creep() -> None:
    prompt = load_prompt("reviewer")
    lower = prompt.lower()

    assert "scope creep" in lower
    assert "traceability failure" in lower
    assert "approved brief" in lower
    assert "wp intent" in lower
    assert "verification predicate" in lower
    assert "reviewer finding" in lower
    assert "verifier failure" in lower
    assert "flag an edit as scope creep when" in lower
    assert "lacks a justification" in lower
    assert "do not flag an edit solely because its path is absent" in lower
    assert "absent from `candidate_files`" in lower
    assert "navigation hints, not permission boundaries" in lower
    assert "must be in planner-provided file" not in lower
    assert "allowed files" not in lower


def test_run_reviewer_returns_structured_summary(
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

    out = asyncio.run(run_reviewer(_state_slice()))

    assert isinstance(out, ReviewerSummary)
    assert out.severity == "low"
    assert out.issues == []
    assert out.recommendation == "approve"
    assert len(fake.queries) == 1
    assert "Review the implementation for merge readiness." in fake.queries[0]
    assert "https://github.com/acme/demo/pull/42" in fake.queries[0]
    assert "Implementation brief:" in fake.queries[0]
    assert "Predicate coverage:" in fake.queries[0]
    assert "Audit log:" in fake.queries[0]
    assert "Attempt log:" in fake.queries[0]
    assert "UserController.java" in fake.queries[0]
