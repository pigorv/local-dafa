"""Build-stage worker role tests after the v2 Builder/Tester migration.

Builder, Tester, and PR Creator agents are composed via
``compose(role, ComposeState.from_mapping(...), task_id=...)``. The tests
below assert:

1. Option shape: hermetic ``setting_sources=[]``; the canonical worker
   ``allowed_tools`` list (no built-in ``Bash``); the in-process
   ``darkfactory`` MCP server attached; a ``can_use_tool`` callback wired;
   all the expected hook events populated.
2. The role's argv allowlist matches ARCHITECTURE.md §5.5 (the four
   build-relevant binaries plus ``cat``/``ls``).
3. The ``can_use_tool`` callback honours the allowlist (allow on ``mvn``,
   deny on out-of-list ``argv[0]``).
4. ``run_<role>`` drives a fake SDK client and folds the diff_capture sink
   into the returned output — confirming the ``options.patches_sink``
   round-trip.

No real Anthropic API calls and no real Docker. The lone integration touchpoint
is the per-task sandbox registry in ``tools/shell.py`` which the
``diff_capture`` hook would consult — for these tests we never trigger
``Edit`` / ``Write`` on the fake client, so no sandbox is needed.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import ClaudeSDKClient, HookMatcher
from claude_agent_sdk.types import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from darkfactory.agents import builder as builder_mod
from darkfactory.agents import pr_creator as pr_creator_mod
from darkfactory.agents import tester as tester_mod
from darkfactory.agents._sdk_common import WorkerOutput
from darkfactory.agents.builder import run_builder
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.pr_creator import PR_CREATOR_ALLOWLIST, run_pr_creator
from darkfactory.agents.registry import get_default_registry
from darkfactory.agents.tester import TesterOutput, run_tester


def _compose_client(
    role: str,
    state_slice: dict,
    *,
    patches_sink: list[Any] | None = None,
) -> ClaudeSDKClient:
    state = ComposeState.from_mapping(state_slice, patches_sink=patches_sink)
    return compose(role, state, task_id=state.task_id)


def _builder_client(
    state_slice: dict, *, patches_sink: list[Any] | None = None
) -> ClaudeSDKClient:
    return _compose_client("builder", state_slice, patches_sink=patches_sink)


def _tester_client(
    state_slice: dict, *, patches_sink: list[Any] | None = None
) -> ClaudeSDKClient:
    return _compose_client("tester", state_slice, patches_sink=patches_sink)


def _pr_creator_client(state_slice: dict) -> ClaudeSDKClient:
    return _compose_client("pr_creator", state_slice)


CANONICAL_ALLOWLIST: frozenset[str] = frozenset(
    {"mvn", "gradle", "./gradlew", "git", "cat", "ls"}
)
CANONICAL_TOOLS: list[str] = ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "sandbox_bash"]
PR_CREATOR_TOOLS: list[str] = ["Read", "Grep", "Glob", "sandbox_bash"]


def _state_slice() -> dict:
    return {
        "user_request": "Add cursor pagination to /api/users",
        "task_id": "task-123",
        "current_slice": "US-1",
        "spec": [
            {
                "story_id": "US-1",
                "approach": "Add cursor param to UserController.",
                "affected_files": ["src/main/java/app/UserController.java"],
                "new_files": [],
                "test_files": [],
                "risks": [],
                "depends_on": [],
            }
        ],
    }


def _pr_state_slice() -> dict:
    state = _state_slice()
    state.update(
        {
            "wf_id": "wf-pr-123",
            "task_id": "wf-pr-123",
            "feature_branch": "agent/wf-pr-123",
            "gate_approved": True,
            "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
            "review_decision": {
                "severity": "low",
                "issues": [],
                "recommendation": "approve",
            },
        }
    )
    return state


# ---------- option-shape assertions (hermetic, no SDK calls) ----------


@pytest.mark.parametrize(
    "factory, allowlist_source",
    [
        (
            _builder_client,
            lambda: frozenset(
                get_default_registry().get("builder").tools.argv_allowlist
            ),
        ),
        (
            _tester_client,
            lambda: frozenset(
                get_default_registry().get("tester").tools.argv_allowlist
            ),
        ),
    ],
)
def test_worker_client_options_are_hermetic_and_sdk_native(
    factory: Any, allowlist_source: Any
) -> None:
    client = factory(_state_slice())
    opts = client.options
    assert opts is not None

    # Hermetic / SDK-native shape per ARCHITECTURE.md §15.4 + §5.5.
    assert opts.setting_sources == []
    assert opts.allowed_tools == CANONICAL_TOOLS
    # Built-in ``Bash`` is intentionally enabled alongside ``sandbox_bash``
    # so agents can introspect projects without the per-role argv allowlist.
    assert "Bash" in opts.allowed_tools
    assert opts.cwd == "/workspace"

    # MCP server attached under the canonical "darkfactory" key.
    assert "darkfactory" in opts.mcp_servers

    # Per-role can_use_tool callback set.
    assert callable(opts.can_use_tool)

    # Worker model defaults from ARCHITECTURE.md §9.
    assert opts.model == "claude-sonnet-4-5-20250929"
    assert getattr(opts, "temperature", None) == 0.1

    # All four hook events populated with at least one HookMatcher each.
    for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"):
        assert event in opts.hooks, event
        assert isinstance(opts.hooks[event][0], HookMatcher)

    # The role's argv allowlist must match the canonical six-binary set.
    assert allowlist_source() == CANONICAL_ALLOWLIST


def _flat_event_hooks(client: Any, event: str) -> list[Any]:
    """Flatten every HookMatcher's callbacks for ``event``.

    ``compose`` emits one HookMatcher per manifest attachment, while the
    pre-Task-6.1 imperative path bundled them into a single matcher. Both
    shapes fire the same hooks at runtime — these structural tests flatten
    across matchers so the assertion is on hook identity, not packing.
    """
    out: list[Any] = []
    for matcher in client.options.hooks.get(event) or []:
        out.extend(list(matcher.hooks or []))
    return out


def test_each_worker_has_loop_breaker_and_call_cap_on_pretool() -> None:
    client = _builder_client(_state_slice())
    pre_hook_names = [hook.__name__ for hook in _flat_event_hooks(client, "PreToolUse")]
    # path_guard + loop_breaker + call_cap + heartbeat.
    assert "path_guard_hook" in pre_hook_names
    assert "loop_breaker_hook" in pre_hook_names
    assert "call_cap_hook" in pre_hook_names
    assert "heartbeat_hook" in pre_hook_names


def test_each_worker_has_diff_capture_and_injection_guard_on_posttool() -> None:
    client = _tester_client(_state_slice())
    post_hook_names = [hook.__name__ for hook in _flat_event_hooks(client, "PostToolUse")]
    # diff_capture (M2-7) + prompt_injection_guard (M2-8) + heartbeat.
    assert "diff_capture_hook" in post_hook_names
    assert "prompt_injection_guard_hook" in post_hook_names
    assert "heartbeat_hook" in post_hook_names


def test_each_worker_has_goal_pin_on_user_prompt_submit() -> None:
    client = _tester_client(_state_slice())
    submit_hook_names = [
        hook.__name__ for hook in _flat_event_hooks(client, "UserPromptSubmit")
    ]
    assert "goal_pin_hook" in submit_hook_names


def test_each_worker_has_heartbeat_on_stop() -> None:
    client = _builder_client(_state_slice())
    stop_hook_names = [hook.__name__ for hook in _flat_event_hooks(client, "Stop")]
    assert "heartbeat_hook" in stop_hook_names


def test_patches_sink_is_stashed_on_options() -> None:
    sink: list[dict] = []
    client = _builder_client(_state_slice(), patches_sink=sink)
    # The same list object is exposed so `run_<role>` can fold it into the
    # WorkerOutput after the SDK loop ends.
    assert client.options.patches_sink is sink


def test_pr_creator_client_options_are_pre_review_and_read_only() -> None:
    client = _pr_creator_client(_pr_state_slice())
    opts = client.options
    assert opts is not None

    assert opts.setting_sources == []
    assert opts.allowed_tools == PR_CREATOR_TOOLS
    assert "Bash" not in opts.allowed_tools
    assert "Write" not in opts.allowed_tools
    assert "Edit" not in opts.allowed_tools
    assert opts.cwd == "/workspace"
    assert "darkfactory" in opts.mcp_servers
    assert callable(opts.can_use_tool)
    assert opts.model == "claude-haiku-4-5-20251001"
    assert opts.temperature == 0.1
    assert opts.thinking is not None
    assert opts.thinking["type"] == "disabled"
    assert PR_CREATOR_ALLOWLIST == frozenset({"git", "gh", "cat", "ls"})
    for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"):
        assert event in opts.hooks, event
        assert isinstance(opts.hooks[event][0], HookMatcher)


# ---------- argv allowlist behaviour ----------


@pytest.mark.parametrize(
    "factory",
    [_builder_client, _tester_client],
)
def test_permission_gate_allows_mvn_compile(factory: Any) -> None:
    client = factory(_state_slice())
    gate = client.options.can_use_tool

    async def call() -> Any:
        return await gate(
            "sandbox_bash",
            {"argv": ["mvn", "-q", "compile"]},
            ToolPermissionContext(),
        )

    result = asyncio.run(call())
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.parametrize(
    "factory",
    [_builder_client, _tester_client],
)
def test_permission_gate_denies_off_allowlist_argv(factory: Any) -> None:
    client = factory(_state_slice())
    gate = client.options.can_use_tool

    async def call() -> Any:
        return await gate(
            "sandbox_bash",
            {"argv": ["rm", "-rf", "/"]},
            ToolPermissionContext(),
        )

    result = asyncio.run(call())
    assert isinstance(result, PermissionResultDeny)
    assert "allowlist" in result.message


@pytest.mark.parametrize(
    "factory",
    [_builder_client, _tester_client],
)
def test_permission_gate_denies_merge_tools_pre_gate(factory: Any) -> None:
    """Agents may never merge, even if a future caller passes a gate flag."""
    client = factory(_state_slice())
    gate = client.options.can_use_tool

    async def call() -> Any:
        return await gate("gh_pr_merge", {}, ToolPermissionContext())

    result = asyncio.run(call())
    assert isinstance(result, PermissionResultDeny)
    assert "agents cannot merge" in result.message


def test_pr_creator_permission_gate_allows_gh_after_gate() -> None:
    client = _pr_creator_client(_pr_state_slice())
    gate = client.options.can_use_tool

    async def call() -> Any:
        return await gate(
            "sandbox_bash",
            {"argv": ["gh", "pr", "list", "--head", "agent/wf-pr-123"]},
            ToolPermissionContext(),
        )

    result = asyncio.run(call())
    assert isinstance(result, PermissionResultAllow)


def test_pr_creator_permission_gate_denies_off_allowlist_argv() -> None:
    client = _pr_creator_client(_pr_state_slice())
    gate = client.options.can_use_tool

    async def call() -> Any:
        return await gate(
            "sandbox_bash",
            {"argv": ["curl", "https://example.com"]},
            ToolPermissionContext(),
        )

    result = asyncio.run(call())
    assert isinstance(result, PermissionResultDeny)
    assert "pr_creator allowlist" in result.message


def test_pr_creator_merge_tools_respect_gate() -> None:
    denied_client = _pr_creator_client({**_pr_state_slice(), "gate_approved": False})
    allowed_client = _pr_creator_client(_pr_state_slice())

    async def denied() -> Any:
        return await denied_client.options.can_use_tool(
            "git_push_agent_branch", {}, ToolPermissionContext()
        )

    async def allowed() -> Any:
        return await allowed_client.options.can_use_tool(
            "git_push_agent_branch", {}, ToolPermissionContext()
        )

    assert isinstance(asyncio.run(denied()), PermissionResultDeny)
    assert isinstance(asyncio.run(allowed()), PermissionResultAllow)


# ---------- run_<role>: WorkerOutput round-trips the diff_capture sink ----------


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="fake-model")


def _result_msg() -> ResultMessage:
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
    """Async-context-manager replacement for ``ClaudeSDKClient``.

    Records ``query`` calls and serves a scripted ``receive_response``
    stream. The tests below seed the list bound to the worker's
    ``patches_sink`` *before* the SDK loop runs, mirroring what the real
    diff_capture hook would do — that lets us assert the
    ``run_<role>`` plumbing without firing real PostToolUse hooks.
    """

    def __init__(
        self,
        responses: list[list[Any]],
        seeded_sink: list[dict[str, Any]],
        patches_sink: list[dict[str, Any]],
    ) -> None:
        self._responses = list(responses)
        self._seeded_sink = list(seeded_sink)
        self._patches_sink = patches_sink
        self.queries: list[str] = []

    async def __aenter__(self) -> "_FakeClient":
        # Simulate diff_capture appending to the sink during the SDK loop.
        self._patches_sink.extend(self._seeded_sink)
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


class _FakePRClient:
    def __init__(self, responses: list[list[Any]]) -> None:
        self._responses = list(responses)
        self.queries: list[str] = []

    async def __aenter__(self) -> "_FakePRClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
        self.queries.append(prompt)

    def receive_response(self) -> AsyncIterator[Any]:
        if not self._responses:
            raise AssertionError("FakePRClient: no more scripted responses")
        batch = self._responses.pop(0)

        async def _gen() -> AsyncIterator[Any]:
            for msg in batch:
                yield msg

        return _gen()


def test_run_builder_returns_worker_output_with_captured_patches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded_patch = {
        "path": "src/main/java/app/UserController.java",
        "diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
        "author_agent": builder_mod.ROLE,
        "slice_id": "US-1",
        "sha": "deadbeef",
    }
    summary = "Edited UserController to accept cursor parameter."

    def _compose(role: str, compose_state: Any, **_kwargs: Any) -> _FakeClient:
        return _FakeClient(
            responses=[[_assistant(summary), _result_msg()]],
            seeded_sink=[seeded_patch],
            patches_sink=compose_state.patches_sink,
        )

    monkeypatch.setattr(builder_mod, "compose", _compose)

    out = asyncio.run(run_builder(_state_slice()))

    assert isinstance(out, WorkerOutput)
    assert out.patches == [seeded_patch]
    assert out.summary == summary


def test_run_tester_returns_structured_output_with_captured_patches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded_patch = {
        "path": "src/test/java/app/UserControllerTest.java",
        "diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
        "author_agent": tester_mod.ROLE,
        "slice_id": "US-1",
        "sha": "feedface",
    }
    payload = (
        '{"summary":"Added cursor pagination coverage",'
        '"coverage":[{"wp_id":"US-1","predicate":"cursor returns next page",'
        '"test_names":["UserControllerTest.cursor"]}],'
        '"findings":[]}'
    )

    def _compose(role: str, compose_state: Any, **_kwargs: Any) -> _FakeClient:
        return _FakeClient(
            responses=[[_assistant(payload), _result_msg()]],
            seeded_sink=[seeded_patch],
            patches_sink=compose_state.patches_sink,
        )

    monkeypatch.setattr(tester_mod, "compose", _compose)

    out = asyncio.run(run_tester(_state_slice()))

    assert isinstance(out, TesterOutput)
    assert out.patches == [seeded_patch]
    assert out.coverage[0].wp_id == "US-1"
    assert out.findings == []


def test_run_pr_creator_returns_url_and_prompts_for_idempotent_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakePRClient(
        [[_assistant("https://github.com/acme/demo/pull/42"), _result_msg()]]
    )

    def _compose(*_args: Any, **_kwargs: Any) -> _FakePRClient:
        return fake

    monkeypatch.setattr(pr_creator_mod, "compose", _compose)

    out = asyncio.run(run_pr_creator(_pr_state_slice()))

    assert out == "https://github.com/acme/demo/pull/42"
    assert len(fake.queries) == 1
    assert "gh pr list --head agent/wf-pr-123" in fake.queries[0]
    assert "git push origin agent/wf-pr-123" in fake.queries[0]
