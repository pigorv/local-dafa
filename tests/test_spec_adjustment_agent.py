"""Unit tests for the Spec Adjustment SDK role (M2-12).

The role is now SDK-driven: ``make_spec_adjustment_client(state_slice)`` +
``async run_spec_adjustment(state_slice) -> SpecAdjustmentOutput``. As with
the discovery roles, these tests replace ``ClaudeSDKClient`` with a scripted
fake so they stay hermetic — no Anthropic API calls, no claude CLI.

Both decision branches (``patch_code`` and ``update_spec``) are exercised
end-to-end through the JSON parser in ``_sdk_common.run_to_completion``.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from darkfactory.agents import spec_adjustment as spec_adjustment_mod
from darkfactory.agents.spec_adjustment import (
    SpecAdjustmentOutput,
    make_spec_adjustment_client,
    run_spec_adjustment,
)


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

    monkeypatch.setattr(spec_adjustment_mod, "make_spec_adjustment_client", _make)


_FAILING_TEST_LOG = (
    "[ERROR] Tests run: 4, Failures: 1, Errors: 0, Skipped: 0\n"
    "[ERROR] UserControllerTest.listUsers_returnsCursorPage:42 "
    "expected: <next-cursor> but was: <null>"
)


def _state_slice() -> dict:
    return {
        "user_request": "Add cursor pagination",
        "current_slice": "US-1",
        "spec": [
            {
                "story_id": "US-1",
                "approach": "Add cursor param to UserController",
                "affected_files": ["src/main/java/app/UserController.java"],
                "new_files": [],
                "test_files": ["src/test/java/app/UserControllerTest.java"],
                "risks": [],
                "depends_on": [],
            }
        ],
        "test_results": [
            {
                "runner": "maven",
                "returncode": 1,
                "passed": 3,
                "failed": 1,
                "errors": [_FAILING_TEST_LOG],
                "duration_s": 0.1,
            }
        ],
        "findings": [],
    }


def test_spec_adjustment_client_options_are_hermetic_and_no_tool() -> None:
    client = make_spec_adjustment_client({"user_request": "x"})
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == []
    assert opts.model == "claude-sonnet-4-5-20250929"  # spec_adjustment default per ARCH §9
    # spec_adjustment defaults to thinking ON.
    assert opts.thinking is not None
    assert opts.thinking["type"] == "enabled"
    assert "PreToolUse" in opts.hooks
    assert "UserPromptSubmit" in opts.hooks


def test_run_spec_adjustment_patch_code_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "decision": "patch_code",
            "rationale": "Test asserts a contract the spec already includes; fix code.",
            "target_worker": "backend",
            "slice_id": "US-1",
            "path": "src/main/java/app/UserController.java",
            "diff": (
                "--- a/src/main/java/app/UserController.java\n"
                "+++ b/src/main/java/app/UserController.java\n"
                "@@ -10,3 +10,3 @@\n"
                "-    return page;\n"
                "+    return page.withCursor(nextCursor);\n"
            ),
        }
    )
    fake = _FakeClient([[_assistant(payload), _result()]])
    _patch_client(monkeypatch, fake)

    out = asyncio.run(run_spec_adjustment(_state_slice()))

    assert isinstance(out, SpecAdjustmentOutput)
    assert out.decision == "patch_code"
    assert out.target_worker == "backend"
    assert out.slice_id == "US-1"
    assert out.diff is not None
    assert out.diff.startswith("--- a/src/main/java/app/UserController.java")
    # The user message threads the failure summary the workflow will consume.
    assert len(fake.queries) == 1
    assert "current_slice=US-1" in fake.queries[0]
    assert "Verify failures:" in fake.queries[0]


def test_run_spec_adjustment_update_spec_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "decision": "update_spec",
            "rationale": "Acceptance missed the test file; widen affected/test paths.",
            "updated_slice": {
                "story_id": "US-1",
                "approach": "Add cursor param to UserController; extend UserService query.",
                "affected_files": [
                    "src/main/java/app/UserController.java",
                    "src/main/java/app/UserService.java",
                ],
                "new_files": [],
                "test_files": ["src/test/java/app/UserControllerTest.java"],
                "risks": ["backward-compat of existing page param"],
                "depends_on": [],
            },
        }
    )
    fake = _FakeClient([[_assistant(payload), _result()]])
    _patch_client(monkeypatch, fake)

    out = asyncio.run(run_spec_adjustment(_state_slice()))

    assert isinstance(out, SpecAdjustmentOutput)
    assert out.decision == "update_spec"
    assert out.updated_slice is not None
    assert out.updated_slice.story_id == "US-1"
    assert any("UserService.java" in p for p in out.updated_slice.affected_files)
