"""Discovery role tests after the SDK migration (M2-9).

The three discovery roles (`po`, `architect`, `plan_critic`) are composed
via ``compose(role, ComposeState.from_mapping(...), task_id=...)``. The
tests below replace `ClaudeSDKClient` with a scripted fake so they stay
hermetic — no Anthropic API calls, no claude CLI, no Ollama.

Each test asserts:
1. ``compose(role, ...)`` produces options with the expected hermetic
   shape (model from defaults, `setting_sources=[]`, no allowed tools, no
   MCP servers, hooks attached for PreToolUse + UserPromptSubmit).
2. `run_<role>` drives the (faked) client and returns the expected
   structured Pydantic output, identical in shape to what the old
   LangChain factory used to produce.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from darkfactory.agents import architect as architect_mod
from darkfactory.agents import plan_critic as plan_critic_mod
from darkfactory.agents import po as po_mod
from darkfactory.agents.architect import (
    ArchitectOutput,
    WorkPackagePlanModel,
    run_architect,
)
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.plan_critic import ReviewDecisionModel, run_plan_critic
from darkfactory.agents.po import normalize_po_output, run_po


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
    """Replace the role's live ``compose`` import so ``run_<role>`` opens our fake."""

    def _compose(*_args: Any, **_kwargs: Any) -> _FakeClient:
        return fake

    monkeypatch.setattr(module, "compose", _compose)


def _hook_callbacks(matcher: HookMatcher) -> list[Any]:
    return list(matcher.hooks)


def _flat_event_hooks(opts: Any, event: str) -> list[Any]:
    """Flatten every HookMatcher's callbacks for ``event``.

    ``compose`` emits one HookMatcher per manifest attachment, while the
    pre-Task-6.1 imperative path bundled them into a single matcher. Both
    shapes fire the same hooks at runtime — these structural tests flatten
    across matchers so the assertion is on hook identity, not packing.
    """
    out: list[Any] = []
    for matcher in opts.hooks.get(event) or []:
        out.extend(list(matcher.hooks or []))
    return out


# ---------- option-shape assertions (hermetic, no SDK calls) ----------


def test_po_client_options_are_hermetic_and_no_tool() -> None:
    state = ComposeState.from_mapping({"user_request": "x"})
    client = compose("po", state, task_id=state.task_id)
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == []
    assert opts.model == "claude-haiku-4-5-20251001"  # po default per ARCH §9
    assert opts.system_prompt == ""  # prompt is rendered as the user message
    assert opts.output_format is not None
    assert opts.output_format["type"] == "json_schema"
    assert "PreToolUse" in opts.hooks
    assert "UserPromptSubmit" in opts.hooks
    pre_hook_names = [
        hook.__name__ for hook in _flat_event_hooks(opts, "PreToolUse")
    ]
    assert "loop_breaker_hook" in pre_hook_names
    assert "call_cap_hook" in pre_hook_names
    post_hook_names = [
        hook.__name__ for hook in _flat_event_hooks(opts, "PostToolUse")
    ]
    assert "prompt_injection_guard_hook" in post_hook_names


def test_architect_client_options_are_hermetic_and_no_tool() -> None:
    state = ComposeState.from_mapping({"user_request": "x", "stories": []})
    client = compose("architect", state, task_id=state.task_id)
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == []
    assert opts.model == "claude-sonnet-4-5-20250929"  # architect default per ARCH §9
    assert "PreToolUse" in opts.hooks
    assert "UserPromptSubmit" in opts.hooks


def test_plan_critic_client_options_are_hermetic_and_no_tool() -> None:
    state = ComposeState.from_mapping({"stories": [], "spec": []})
    client = compose("plan_critic", state, task_id=state.task_id)
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == []
    assert opts.model == "claude-sonnet-4-5-20250929"  # plan_critic default per ARCH §9


# ---------- run_<role>: structured-output round-trips ----------


def _structured_assistant(payload: dict) -> AssistantMessage:
    """An assistant turn that emits the SDK's synthetic StructuredOutput tool."""
    return AssistantMessage(
        content=[
            ToolUseBlock(
                id="toolu_po_1",
                name="StructuredOutput",
                input=payload,
            )
        ],
        model="fake-model",
    )


def test_run_po_returns_user_stories(monkeypatch: pytest.MonkeyPatch) -> None:
    story = {
        "id": "US-1",
        "title": "Cursor pagination",
        "as_a": "API consumer",
        "i_want": "to page users with a cursor",
        "so_that": "I can scroll large result sets",
        "acceptance_criteria": [
            "GET /api/users?cursor=… returns next page"
        ],
    }
    fake = _FakeClient([[_structured_assistant({"stories": [story]}), _result()]])
    _patch_client(monkeypatch, po_mod, fake)

    out = asyncio.run(
        run_po(
            {
                "user_request": "Add cursor pagination to /api/users.",
                "repo_context": {},
            }
        )
    )

    assert isinstance(out, dict)
    assert len(out["stories"]) == 1
    assert out["stories"][0]["id"] == "US-1"
    assert out["stories"][0]["acceptance_criteria"] == [
        "GET /api/users?cursor=… returns next page"
    ]
    # `expected_behavior` is derived from stories[].acceptance_criteria
    # when the model leaves it empty.
    assert out["expected_behavior"] == [
        "GET /api/users?cursor=… returns next page"
    ]
    # The rendered template threads the original request and the PO role framing.
    assert len(fake.queries) == 1
    assert "Add cursor pagination" in fake.queries[0]
    assert "Product Owner agent" in fake.queries[0]


def test_normalize_po_output_accepts_v2_and_legacy_aliases() -> None:
    out = normalize_po_output(
        {
            "problem": "Large user lists need stable pagination.",
            "expected_behavior": ["Clients can request the next page with a cursor."],
            "compatibility_risks": ["Existing offset clients must keep working."],
            "open_assumptions": ["Cursor ordering can reuse created_at."],
            "stories": [],
        }
    )

    assert out["problem"] == "Large user lists need stable pagination."
    assert out["expected_behavior"] == [
        "Clients can request the next page with a cursor."
    ]
    assert out["compatibility_risks"] == ["Existing offset clients must keep working."]
    assert out["open_assumptions"] == ["Cursor ordering can reuse created_at."]

    legacy = normalize_po_output(
        {
            "acceptance_criteria": ["GET /api/users?cursor=abc returns a page."],
            "risks": ["Pagination metadata shape may be client-visible."],
            "assumptions": ["The API can keep its current page size default."],
        }
    )

    assert legacy["expected_behavior"] == [
        "GET /api/users?cursor=abc returns a page."
    ]
    assert legacy["compatibility_risks"] == [
        "Pagination metadata shape may be client-visible."
    ]
    assert legacy["open_assumptions"] == [
        "The API can keep its current page size default."
    ]


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
    assert isinstance(slice_, WorkPackagePlanModel)
    assert slice_.story_id == "US-1"
    assert slice_.depends_on == []
    assert "UserController.java" in slice_.affected_files[0]
    assert "Plan" not in fake.queries[0]  # message asks for work packages, not "Plan it"
    assert "User stories (JSON):" in fake.queries[0]


def test_architect_output_accepts_work_packages_and_legacy_spec_alias() -> None:
    out = ArchitectOutput.model_validate(
        {
            "current_understanding": "The existing API accepts limit and offset.",
            "proposed_design": "Add cursor parsing near the user lookup flow.",
            "contract_changes": {
                "api": ["Add optional cursor query parameter."],
                "data": [],
                "events": [],
            },
            "test_strategy": "Cover first and final cursor pages.",
            "work_packages": [
                {
                    "id": "WP-1",
                    "story_id": "US-1",
                    "title": "Add cursor pagination",
                    "intent": "Return stable cursor pages for active users.",
                    "verification": [
                        "First page includes a next cursor when more active users exist."
                    ],
                    "repo_areas": ["Backend user lookup flow"],
                    "candidate_files": [
                        "src/main/java/app/UserController.java",
                        "src/test/java/app/UserControllerTest.java",
                    ],
                    "dependencies": [],
                    "estimated_scope": "small",
                    "notes": ["Keep offset pagination fields for compatibility."],
                }
            ],
        }
    )

    assert out.work_packages[0].id == "WP-1"
    assert out.spec[0].story_id == "WP-1"
    assert out.spec[0].approach == "Return stable cursor pages for active users."
    assert out.spec[0].affected_files == [
        "src/main/java/app/UserController.java",
        "src/test/java/app/UserControllerTest.java",
    ]

    legacy = ArchitectOutput.model_validate(
        {
            "spec": [
                {
                    "story_id": "US-1",
                    "approach": "Add cursor param to UserController.",
                    "affected_files": ["src/main/java/app/UserController.java"],
                    "new_files": ["src/main/java/app/CursorToken.java"],
                    "test_files": ["src/test/java/app/UserControllerTest.java"],
                    "risks": ["Response metadata compatibility."],
                    "depends_on": ["US-0"],
                }
            ]
        }
    )

    assert legacy.work_packages[0].id == "US-1"
    assert legacy.work_packages[0].candidate_files == [
        "src/main/java/app/UserController.java",
        "src/main/java/app/CursorToken.java",
    ]
    assert legacy.work_packages[0].dependencies == ["US-0"]


def test_run_plan_critic_returns_review_decision(
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
    _patch_client(monkeypatch, plan_critic_mod, fake)

    out = asyncio.run(
        run_plan_critic(
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
    assert "Work packages (JSON):" in fake.queries[0]
