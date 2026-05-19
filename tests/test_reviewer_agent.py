"""Unit tests for the Reviewer SDK role (M4-1)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import AssistantMessage, ResultMessage, ToolUseBlock

from darkfactory.agents import reviewer as reviewer_mod
from darkfactory.agents._sdk_common import load_prompt
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.reviewer import normalize_reviewer_output, run_reviewer
from darkfactory.state import ReviewerSummary


def _structured_assistant(payload: dict) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolUseBlock(
                id="toolu_reviewer_1",
                name="StructuredOutput",
                input=payload,
            )
        ],
        model="fake-model",
    )


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
            }
        ],
        "builder_outputs": [
            {
                "wp_id": "WP-1",
                "status": "done",
                "edits": [
                    {
                        "path": "src/main/java/app/UserController.java",
                        "operation": "modify",
                        "intent": "WP-1 cursor response shape.",
                    }
                ],
                "blockers": [],
                "summary": "Added cursor response wiring.",
            }
        ],
        "tester_outputs": [],
        "tester_findings": [],
        "reconciliation_findings": [],
        "coverage_entries": [],
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
        "fixer_decision": {},
        "attempt_log": [{"source": "fixer_attempt", "target_wp": "WP-1"}],
    }


def _flat_event_hooks(opts: Any, event: str) -> list[Any]:
    out: list[Any] = []
    for matcher in opts.hooks.get(event) or []:
        out.extend(list(matcher.hooks or []))
    return out


def test_reviewer_client_options_are_hermetic_and_read_only() -> None:
    state = ComposeState.from_mapping({"user_request": "x"})
    client = compose("reviewer", state, task_id=state.task_id)
    opts = client.options
    assert opts is not None
    assert set(opts.allowed_tools) == {"Read", "Grep", "Glob", "mcp__*"}
    assert opts.skills == "all"
    assert set(opts.disallowed_tools) == {"Bash", "Edit", "Write"}
    assert opts.mcp_servers == {}
    assert opts.can_use_tool is None
    assert opts.setting_sources == ["project"]
    assert opts.model == "claude-sonnet-4-5-20250929"
    assert opts.thinking is not None
    assert opts.thinking["type"] == "disabled"
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert opts.output_format is not None
    assert opts.output_format["type"] == "json_schema"
    assert opts.output_format["schema"]["title"] == "ReviewerSummary"
    for event in ("PreToolUse", "PostToolUse", "Stop"):
        assert event in opts.hooks, event
        assert isinstance(opts.hooks[event][0], HookMatcher)
    assert "UserPromptSubmit" not in opts.hooks
    pre_hook_names = [hook.__name__ for hook in _flat_event_hooks(opts, "PreToolUse")]
    assert "loop_breaker_hook" in pre_hook_names
    assert "call_cap_hook" in pre_hook_names
    post_hook_names = [hook.__name__ for hook in _flat_event_hooks(opts, "PostToolUse")]
    assert "prompt_injection_guard_hook" in post_hook_names
    assert "structured_output_hint_hook" in post_hook_names


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
    assert "do not flag a change solely because its path is absent" in lower
    assert "`candidate_files`" in lower
    assert "navigation hints" in lower
    assert "permission" in lower
    assert "boundaries" in lower
    assert "builder structured outputs" in lower
    assert "reconciliation findings" in lower
    assert "must be in planner-provided file" not in lower
    assert "allowed files" not in lower


def test_run_reviewer_returns_structured_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "severity": "low",
        "issues": [],
        "recommendation": "approve",
        "findings": [],
    }
    fake = _FakeClient([[_structured_assistant(payload), _result()]])
    _patch_client(monkeypatch, fake)

    out = asyncio.run(run_reviewer(_state_slice()))

    assert isinstance(out, ReviewerSummary)
    assert out.severity == "low"
    assert out.issues == []
    assert out.recommendation == "approve"
    assert out.findings == []
    assert len(fake.queries) == 1
    assert "You review the produced pull request for merge readiness." in fake.queries[0]
    assert "https://github.com/acme/demo/pull/42" in fake.queries[0]
    assert "Implementation Brief" in fake.queries[0]
    assert "Builder structured outputs" in fake.queries[0]
    assert "Reconciliation findings" in fake.queries[0]
    assert "Attempt log" in fake.queries[0]
    assert "UserController.java" in fake.queries[0]


def test_reviewer_normalizer_forces_request_changes_for_high_finding() -> None:
    out = normalize_reviewer_output(
        {
            "severity": "medium",
            "issues": [],
            "recommendation": "approve",
            "findings": [
                {
                    "path": "src/app.py",
                    "line": 12,
                    "severity": "high",
                    "message": "Cursor token accepts stale tenant data.",
                }
            ],
        }
    )

    assert out.recommendation == "request_changes"
    assert out.issues == ["Cursor token accepts stale tenant data."]
