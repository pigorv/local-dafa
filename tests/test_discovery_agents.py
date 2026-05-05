"""Discovery role tests after the SDK migration (M2-9).

The three discovery roles (`po`, `architect`, `spec_reviewer`) are now
SDK-driven: each exposes `make_<role>_client(state_slice)` and a sibling
`async run_<role>(state_slice) -> Pydantic`. The tests below replace
`ClaudeSDKClient` with a scripted fake so they stay hermetic — no
Anthropic API calls, no claude CLI, no Ollama.

Each test asserts:
1. `make_<role>_client` produces options with the expected hermetic shape
   (model from defaults, `setting_sources=[]`, no allowed tools, no MCP
   servers, hooks attached for PreToolUse + UserPromptSubmit).
2. `run_<role>` drives the (faked) client and returns the expected
   structured Pydantic output, identical in shape to what the old
   LangChain factory used to produce.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from darkfactory.agents import architect as architect_mod
from darkfactory.agents import po as po_mod
from darkfactory.agents import spec_reviewer as spec_reviewer_mod
from darkfactory.agents.architect import (
    ArchitectOutput,
    SpecSliceModel,
    make_architect_client,
    run_architect,
)
from darkfactory.agents.po import (
    POOutput,
    UserStoryModel,
    make_po_client,
    run_po,
)
from darkfactory.agents.spec_reviewer import (
    ReviewDecisionModel,
    make_spec_reviewer_client,
    run_spec_reviewer,
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
    """Stub ClaudeSDKClient that scripts `receive_response()` runs.

    Used as an async-context-manager replacement for `ClaudeSDKClient`.
    `query()` records prompts so tests can assert what was sent.
    """

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


def _patch_client(monkeypatch: pytest.MonkeyPatch, module: Any, fake: _FakeClient) -> None:
    """Replace the role module's `make_<role>_client` so `run_<role>` opens our fake."""
    name = next(
        attr for attr in ("make_po_client", "make_architect_client", "make_spec_reviewer_client")
        if hasattr(module, attr)
    )

    @asynccontextmanager
    async def _factory(_state_slice: dict) -> AsyncIterator[_FakeClient]:
        async with fake as client:
            yield client

    # `run_<role>` does `async with make_<role>_client(state_slice) as client`,
    # which works for any async-context-manager-returning callable.
    def _make(_state_slice: dict) -> _FakeClient:
        return fake

    monkeypatch.setattr(module, name, _make)


def _hook_callbacks(matcher: HookMatcher) -> list[Any]:
    return list(matcher.hooks)


# ---------- option-shape assertions (hermetic, no SDK calls) ----------


def test_po_client_options_are_hermetic_and_no_tool() -> None:
    client = make_po_client({"user_request": "x"})
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == []
    assert opts.model == "claude-haiku-4-5-20251001"  # po default per ARCH §9
    assert "PreToolUse" in opts.hooks
    assert "UserPromptSubmit" in opts.hooks
    pre_hooks = _hook_callbacks(opts.hooks["PreToolUse"][0])
    assert len(pre_hooks) == 2  # loop_breaker + call_cap
    assert "PostToolUse" not in opts.hooks  # no PostToolUse hooks for reasoning-only roles


def test_architect_client_options_are_hermetic_and_no_tool() -> None:
    client = make_architect_client({"user_request": "x", "stories": []})
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == []
    assert opts.model == "claude-sonnet-4-5-20250929"  # architect default per ARCH §9
    assert "PreToolUse" in opts.hooks
    assert "UserPromptSubmit" in opts.hooks


def test_spec_reviewer_client_options_are_hermetic_and_no_tool() -> None:
    client = make_spec_reviewer_client({"stories": [], "spec": []})
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == []
    assert opts.model == "claude-sonnet-4-5-20250929"  # spec_reviewer default per ARCH §9


# ---------- run_<role>: structured-output round-trips ----------


def test_run_po_returns_user_stories(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "stories": [
                {
                    "id": "US-1",
                    "title": "Cursor pagination",
                    "as_a": "API consumer",
                    "i_want": "to page users with a cursor",
                    "so_that": "I can scroll large result sets",
                    "acceptance_criteria": [
                        "GET /api/users?cursor=… returns next page"
                    ],
                }
            ]
        }
    )
    fake = _FakeClient([[_assistant(payload), _result()]])
    _patch_client(monkeypatch, po_mod, fake)

    out = asyncio.run(
        run_po(
            {
                "user_request": "Add cursor pagination to /api/users.",
                "repo_context": {},
            }
        )
    )

    assert isinstance(out, POOutput)
    assert len(out.stories) == 1
    story = out.stories[0]
    assert isinstance(story, UserStoryModel)
    assert story.id == "US-1"
    assert story.acceptance_criteria == [
        "GET /api/users?cursor=… returns next page"
    ]
    # The user message threads the request and the production-of-stories instruction.
    assert len(fake.queries) == 1
    assert "Add cursor pagination" in fake.queries[0]
    assert "Produce user stories." in fake.queries[0]


def test_run_architect_returns_topo_sortable_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "spec": [
                {
                    "story_id": "US-1",
                    "approach": "Add cursor param to UserController; extend UserService query.",
                    "affected_files": ["src/main/java/app/UserController.java"],
                    "new_files": [],
                    "test_files": ["src/test/java/app/UserControllerTest.java"],
                    "risks": ["backward-compat of existing page param"],
                    "depends_on": [],
                }
            ]
        }
    )
    fake = _FakeClient([[_assistant(payload), _result()]])
    _patch_client(monkeypatch, architect_mod, fake)

    out = asyncio.run(
        run_architect(
            {
                "user_request": "Add cursor pagination",
                "repo_context": {},
                "stories": [{"id": "US-1", "title": "Cursor pagination"}],
            }
        )
    )

    assert isinstance(out, ArchitectOutput)
    assert len(out.spec) == 1
    slice_ = out.spec[0]
    assert isinstance(slice_, SpecSliceModel)
    assert slice_.story_id == "US-1"
    assert slice_.depends_on == []
    assert "UserController.java" in slice_.affected_files[0]
    assert "Plan" not in fake.queries[0]  # message asks for SpecSlices, not "Plan it"
    assert "User stories (JSON):" in fake.queries[0]


def test_run_spec_reviewer_returns_review_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "approved": False,
            "reason": "US-1 has no test file wired to its acceptance criteria.",
            "edits": {
                "US-1": {
                    "test_files": ["src/test/java/app/UserControllerTest.java"]
                }
            },
        }
    )
    fake = _FakeClient([[_assistant(payload), _result()]])
    _patch_client(monkeypatch, spec_reviewer_mod, fake)

    out = asyncio.run(
        run_spec_reviewer(
            {
                "stories": [{"id": "US-1"}],
                "spec": [{"story_id": "US-1", "test_files": []}],
            }
        )
    )

    assert isinstance(out, ReviewDecisionModel)
    assert out.approved is False
    assert "US-1" in out.edits
    assert "test_files" in out.edits["US-1"]
    assert "Spec slices (JSON):" in fake.queries[0]
