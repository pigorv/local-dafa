"""Discovery role tests after the SDK migration (M2-9).

The three discovery roles (`po`, `architect`, `plan_critic`) are composed
via ``compose(role, ComposeState.from_mapping(...), task_id=...)``. The
tests below replace `ClaudeSDKClient` with a scripted fake so they stay
hermetic — no Anthropic API calls, no claude CLI, no Ollama.

Each test asserts:
1. ``compose(role, ...)`` produces options with the expected shape
   (model from defaults, ``setting_sources=["project"]`` so target-repo
   CLAUDE.md and ``.claude/`` skills load, read-only tools, no MCP
   servers, hooks attached for PreToolUse + UserPromptSubmit).
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
from darkfactory.agents.architect import run_architect
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.plan_critic import run_plan_critic
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


def test_po_client_options_allow_read_only_tools() -> None:
    state = ComposeState.from_mapping({"user_request": "x"})
    client = compose("po", state, task_id=state.task_id)
    opts = client.options
    assert opts is not None
    assert set(opts.allowed_tools) == {"Read", "Grep", "Glob", "Skill"}
    assert set(opts.disallowed_tools) == {"Bash", "Edit", "Write"}
    assert opts.mcp_servers == {}
    assert opts.setting_sources == ["project"]
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


def test_architect_client_options_allow_read_only_tools() -> None:
    state = ComposeState.from_mapping({"user_request": "x", "stories": []})
    client = compose("architect", state, task_id=state.task_id)
    opts = client.options
    assert opts is not None
    assert set(opts.allowed_tools) == {"Read", "Grep", "Glob", "Skill"}
    assert set(opts.disallowed_tools) == {"Bash", "Edit", "Write"}
    assert opts.mcp_servers == {}
    # No MCP and no sandbox_bash in allowed_tools → no permission gate.
    # The architect's prompt explicitly limits it to Read / Grep / Glob,
    # so exposing a shell channel would be dead surface.
    assert opts.can_use_tool is None
    assert opts.setting_sources == ["project"]
    assert opts.model == "claude-sonnet-4-5-20250929"  # architect default per ARCH §9
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


def test_plan_critic_client_options_are_hermetic_and_no_tool() -> None:
    state = ComposeState.task_only(task_id="t")
    client = compose("plan_critic", state, task_id=state.task_id)
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == ["project"]
    assert opts.model == "claude-sonnet-4-5-20250929"  # plan_critic default per ARCH §9
    assert opts.system_prompt == ""  # prompt is rendered as the user message
    assert opts.output_format is not None
    assert opts.output_format["type"] == "json_schema"
    # Plan critic is single-turn with zero tools: PreToolUse / per-N-prompt
    # hooks could never fire. PostToolUse fires only for the synthetic
    # StructuredOutput tool, and Stop fires once at the turn boundary.
    assert "PreToolUse" not in opts.hooks
    assert "UserPromptSubmit" not in opts.hooks
    post_hook_names = [
        hook.__name__
        for matcher in opts.hooks.get("PostToolUse") or []
        for hook in matcher.hooks
    ]
    assert post_hook_names == ["structured_output_hint_hook"]
    assert "Stop" in opts.hooks


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


def test_run_architect_returns_work_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
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
                "candidate_files": ["src/main/java/app/UserController.java"],
                "dependencies": [],
                "estimated_scope": "small",
                "notes": [],
            }
        ],
    }
    fake = _FakeClient([[_structured_assistant(payload), _result()]])
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

    assert isinstance(out, dict)
    work_packages = out["work_packages"]
    assert len(work_packages) == 1
    assert work_packages[0]["id"] == "WP-1"
    assert work_packages[0]["story_id"] == "US-1"
    assert work_packages[0]["dependencies"] == []
    # The rendered template threads the request, stories, and Architect framing.
    assert len(fake.queries) == 1
    assert "Add cursor pagination" in fake.queries[0]
    assert "Architect agent" in fake.queries[0]
    assert "US-1" in fake.queries[0]


def test_run_plan_critic_returns_review_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "approved": False,
        "reason": (
            "Predicate observability: WP-1 verification pins @SpringBootTest "
            "instead of describing the observable behavior."
        ),
        "edits": {
            "WP-1": {
                "verification": [
                    "GET /api/users?cursor=… returns next page",
                ],
            },
        },
        "notes": [],
    }
    fake = _FakeClient([[_structured_assistant(payload), _result()]])
    _patch_client(monkeypatch, plan_critic_mod, fake)

    out = asyncio.run(
        run_plan_critic(
            {
                "user_request": "Add cursor pagination to /api/users.",
                "stories": [{"id": "US-1"}],
                "work_packages": [
                    {
                        "id": "WP-1",
                        "story_id": "US-1",
                        "title": "Add cursor pagination",
                        "intent": "Wire cursor parsing into UserController.",
                        "verification": [
                            "GET /api/users?cursor=… returns next page"
                        ],
                        "repo_areas": ["Backend user lookup flow"],
                        "candidate_files": [
                            "src/main/java/app/UserController.java"
                        ],
                        "dependencies": [],
                        "estimated_scope": "small",
                        "notes": [],
                    }
                ],
                "planning_attempts": 1,
                "planning_max_attempts": 5,
            }
        )
    )

    assert isinstance(out, dict)
    assert out["approved"] is False
    assert "WP-1" in out["edits"]
    assert out["edits"]["WP-1"]["verification"][0].startswith("GET /api/users")
    # The rendered template threads the user request, work packages, and
    # the explicit attempt-of-max marker the prompt's final-attempt rule
    # keys off.
    assert "Add cursor pagination" in fake.queries[0]
    assert "Plan Critic agent" in fake.queries[0]
    assert "Work packages (JSON):" in fake.queries[0]
    assert "Attempt 1 of 5" in fake.queries[0]


def test_po_prompt_includes_verbatim_issue_when_present() -> None:
    """Issue-driven runs surface the raw issue body alongside the derived prompt.

    For issue workflows, ``state["user_request"]`` is overwritten with the
    triage-derived restatement before discovery runs. ``state["issue"]``
    still carries the verbatim title + body. The PO prompt must thread
    both: the original (ground truth) and the derived (cleaner summary).
    """
    state = {
        "user_request": "Add filter bar to Session List",  # triage-derived
        "issue": {
            "repo": "owner/name",
            "number": 1,
            "url": "https://example.invalid/1",
            "title": "Refactor session controls into a single filter bar",
            "body": (
                "Replace the two-row controls with a single horizontal "
                "filter bar. Note: must preserve `cm.sessionList.chipFilter` "
                "localStorage key exactly — do not migrate it."
            ),
            "labels": ["df:ready"],
        },
        "repo_context": {},
    }

    rendered = po_mod._render_user_prompt(state)

    assert "Original user request" in rendered
    assert "Triage-derived request" in rendered
    # Both halves are present, distinct, and verbatim.
    assert "Refactor session controls into a single filter bar" in rendered
    assert "cm.sessionList.chipFilter" in rendered  # detail triage might drop
    assert "Add filter bar to Session List" in rendered  # derived restatement


def test_architect_prompt_includes_verbatim_issue_when_present() -> None:
    state = {
        "user_request": "Add filter bar to Session List",
        "issue": {
            "repo": "owner/name",
            "number": 1,
            "url": "https://example.invalid/1",
            "title": "Refactor session controls into a single filter bar",
            "body": (
                "Constraint: do not introduce new npm dependencies. "
                "Sort dropdown must exclude `compaction_count`."
            ),
            "labels": ["df:ready"],
        },
        "repo_context": {},
        "stories": [{"id": "US-1", "title": "Filter bar"}],
    }

    rendered = architect_mod._render_user_prompt(state)

    assert "Original user request" in rendered
    assert "Triage-derived request" in rendered
    assert "do not introduce new npm dependencies" in rendered
    assert "compaction_count" in rendered
    assert "Add filter bar to Session List" in rendered


def test_po_prompt_falls_back_to_user_request_without_issue() -> None:
    """CLI runs have no ``state['issue']``; original falls back to user_request."""
    state = {
        "user_request": "Add cursor pagination to /api/users.",
        "repo_context": {},
    }

    rendered = po_mod._render_user_prompt(state)

    assert "Original user request" in rendered
    assert "Triage-derived request" in rendered
    # Same text under both labels — CLI path has no triage to diverge from.
    assert rendered.count("Add cursor pagination to /api/users.") == 2


def test_normalize_plan_critic_output_enforces_invariants() -> None:
    from darkfactory.agents.plan_critic import normalize_plan_critic_output

    # approved=True drops stray edits the model attached.
    approved = normalize_plan_critic_output(
        {
            "approved": True,
            "reason": "All checks pass.",
            "edits": {"WP-1": {"notes": ["nit"]}},
            "notes": [],
        }
    )
    assert approved["approved"] is True
    assert approved["edits"] == {}
    assert approved["notes"] == []

    # approved=False with empty reason → backfilled from edits keys, and
    # stray notes are dropped (notes are for approval-with-notes only).
    derived = normalize_plan_critic_output(
        {
            "approved": False,
            "reason": "",
            "edits": {"WP-2": {"verification": ["…"]}, "WP-1": {"notes": []}},
            "notes": ["spurious"],
        }
    )
    assert derived["approved"] is False
    # Sorted keys so the message is deterministic for downstream logs.
    assert "WP-1" in derived["reason"] and "WP-2" in derived["reason"]
    assert derived["reason"].index("WP-1") < derived["reason"].index("WP-2")
    assert derived["notes"] == []

    # approved=False with no reason and no edits → explicit sentinel so
    # the workflow surfaces the unactionable rejection instead of silently
    # forwarding it to the architect.
    empty = normalize_plan_critic_output(
        {"approved": False, "reason": "", "edits": {}, "notes": []}
    )
    assert empty["approved"] is False
    assert "empty rejection" in empty["reason"]

    # Approval-with-notes path: deferred concerns survive as a separate
    # channel so the brief gate can surface them.
    with_notes = normalize_plan_critic_output(
        {
            "approved": True,
            "reason": "Final-attempt allowance — soft blockers below.",
            "edits": {},
            "notes": [
                "WP-1 estimated_scope still 'large'; consider splitting.",
                "WP-2 dependencies graph is a chain — re-check ordering.",
            ],
        }
    )
    assert with_notes["approved"] is True
    assert len(with_notes["notes"]) == 2
