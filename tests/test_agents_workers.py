"""Build-stage worker role tests after the v2 Builder/Tester migration.

Builder, Tester, and PR Creator agents are composed via
``compose(role, ComposeState.from_mapping(...), task_id=...)``. The tests
below assert:

1. Option shape: ``setting_sources=["project"]`` (target-repo CLAUDE.md
   and .claude/skills/ are loaded but the host's ``~/.claude/`` is not);
   the canonical worker
   ``allowed_tools`` list (no built-in ``Bash``); the in-process
   ``darkfactory`` MCP server attached; a ``can_use_tool`` callback wired;
   all the expected hook events populated.
2. The role's argv allowlist matches ARCHITECTURE.md §5.5 (the four
   build-relevant binaries plus ``cat``/``ls``).
3. The ``can_use_tool`` callback honours the allowlist (allow on ``mvn``,
   deny on out-of-list ``argv[0]``).
4. ``run_<role>`` drives a fake SDK client and returns the agent's
   declared structured output.

No real Anthropic API calls and no real Docker. PR C removed the
``diff_capture`` hook — patches now come from ``git diff`` in the build
subgraph or Fixer activity, so the tests below no longer need to
simulate the hook's PostToolUse side effects.
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
    ToolUseBlock,
)

from darkfactory.agents import builder as builder_mod
from darkfactory.agents import pr_creator as pr_creator_mod
from darkfactory.agents import tester as tester_mod
from darkfactory.agents.builder import run_builder
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.pr_creator import run_pr_creator
from darkfactory.agents.registry import get_default_registry
from darkfactory.agents.tester import run_tester


def _compose_client(role: str, state_slice: dict) -> ClaudeSDKClient:
    state = ComposeState.from_mapping(state_slice)
    return compose(role, state, task_id=state.task_id)


def _builder_client(state_slice: dict) -> ClaudeSDKClient:
    return _compose_client("builder", state_slice)


def _tester_client(state_slice: dict) -> ClaudeSDKClient:
    return _compose_client("tester", state_slice)


def _pr_creator_client(state_slice: dict) -> ClaudeSDKClient:
    return _compose_client("pr_creator", state_slice)


BUILDER_TOOLS: list[str] = [
    "Read", "Write", "Edit", "Grep", "Glob", "Bash", "mcp__*",
]
TESTER_TOOLS: list[str] = [
    "Read", "Write", "Edit", "Grep", "Glob", "Bash", "mcp__*",
]
PR_CREATOR_TOOLS: list[str] = [
    "Read", "Grep", "Glob", "Bash", "mcp__*",
]
PR_CREATOR_DENYLIST: tuple[tuple[str, ...], ...] = (("gh", "issue"),)
WORKER_DENYLIST: tuple[tuple[str, ...], ...] = (("git", "push"),)


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


BUILDER_DENYLIST: tuple[tuple[str, ...], ...] = (("git", "push"),)
TESTER_DENYLIST: tuple[tuple[str, ...], ...] = (("git", "push"),)


def test_builder_client_options_are_hermetic_and_sdk_native() -> None:
    client = _builder_client(_state_slice())
    opts = client.options
    assert opts is not None

    # Builder runs the built-in ``Bash`` directly (no ``sandbox_bash``,
    # no ``darkfactory`` MCP server). The permission gate runs in pure
    # denylist mode: empty argv_allowlist, ``git push`` in argv_denylist.
    assert opts.setting_sources == ["project"]
    assert opts.skills == "all"
    assert opts.allowed_tools == BUILDER_TOOLS
    assert "Bash" in opts.allowed_tools
    assert "sandbox_bash" not in opts.allowed_tools
    assert opts.cwd == "/workspace"
    assert "darkfactory" not in opts.mcp_servers

    # Bash + non-empty argv_denylist → gate is installed even though the
    # allowlist is empty.
    assert callable(opts.can_use_tool)

    assert opts.model == "claude-sonnet-4-5-20250929"

    # PreToolUse / PostToolUse / Stop are populated; UserPromptSubmit
    # is intentionally absent (goal_pin was unreachable for this role).
    for event in ("PreToolUse", "PostToolUse", "Stop"):
        assert event in opts.hooks, event
        assert isinstance(opts.hooks[event][0], HookMatcher)
    assert "UserPromptSubmit" not in opts.hooks

    manifest_tools = get_default_registry().get("builder").tools
    assert manifest_tools.argv_allowlist == []
    assert tuple(manifest_tools.argv_denylist) == BUILDER_DENYLIST


def test_tester_client_options_are_hermetic_and_sdk_native() -> None:
    client = _tester_client(_state_slice())
    opts = client.options
    assert opts is not None

    # Tester mirrors Builder's shell pattern: built-in ``Bash`` with a
    # pure denylist (no ``sandbox_bash``, no ``darkfactory`` MCP server).
    assert opts.setting_sources == ["project"]
    assert opts.allowed_tools == TESTER_TOOLS
    assert "Bash" in opts.allowed_tools
    assert "sandbox_bash" not in opts.allowed_tools
    assert opts.cwd == "/workspace"
    assert "darkfactory" not in opts.mcp_servers

    # Bash + non-empty argv_denylist → gate is installed even though the
    # allowlist is empty.
    assert callable(opts.can_use_tool)

    assert opts.model == "claude-sonnet-4-5-20250929"

    # PreToolUse / PostToolUse / Stop are populated; UserPromptSubmit
    # is intentionally absent (goal_pin was unreachable for this role).
    for event in ("PreToolUse", "PostToolUse", "Stop"):
        assert event in opts.hooks, event
        assert isinstance(opts.hooks[event][0], HookMatcher)
    assert "UserPromptSubmit" not in opts.hooks

    manifest_tools = get_default_registry().get("tester").tools
    assert manifest_tools.argv_allowlist == []
    assert tuple(manifest_tools.argv_denylist) == TESTER_DENYLIST


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


@pytest.mark.parametrize("factory", [_builder_client, _tester_client])
def test_each_worker_has_loop_breaker_and_call_cap_on_pretool(factory: Any) -> None:
    client = factory(_state_slice())
    pre_hook_names = [hook.__name__ for hook in _flat_event_hooks(client, "PreToolUse")]
    # path_guard + loop_breaker + call_cap + heartbeat.
    assert "path_guard_hook" in pre_hook_names
    assert "loop_breaker_hook" in pre_hook_names
    assert "call_cap_hook" in pre_hook_names
    assert "heartbeat_hook" in pre_hook_names


def test_each_worker_has_injection_guard_on_posttool() -> None:
    """PR C removed the diff_capture hook; PostToolUse still carries the
    prompt-injection guard, structured-output hint (where wired), and
    heartbeat instrumentation.
    """
    client = _tester_client(_state_slice())
    post_hook_names = [hook.__name__ for hook in _flat_event_hooks(client, "PostToolUse")]
    assert "diff_capture_hook" not in post_hook_names
    assert "prompt_injection_guard_hook" in post_hook_names
    assert "heartbeat_hook" in post_hook_names


@pytest.mark.parametrize("factory", [_builder_client, _tester_client])
def test_each_worker_has_heartbeat_on_stop(factory: Any) -> None:
    client = factory(_state_slice())
    stop_hook_names = [hook.__name__ for hook in _flat_event_hooks(client, "Stop")]
    assert "heartbeat_hook" in stop_hook_names


def test_pr_creator_client_options_use_builtin_bash_with_tight_allowlist() -> None:
    client = _pr_creator_client(_pr_state_slice())
    opts = client.options
    assert opts is not None

    assert opts.setting_sources == ["project"]
    assert opts.allowed_tools == PR_CREATOR_TOOLS
    # pr_creator runs git/gh through the built-in Bash (no sandbox_bash,
    # no in-process darkfactory MCP server), but keeps a tight allowlist
    # rather than the Builder/Tester pure-denylist.
    assert "Bash" in opts.allowed_tools
    assert "sandbox_bash" not in opts.allowed_tools
    assert "Write" not in opts.allowed_tools
    assert "Edit" not in opts.allowed_tools
    assert opts.cwd == "/workspace"
    assert "darkfactory" not in opts.mcp_servers
    assert opts.mcp_servers == {}
    assert callable(opts.can_use_tool)
    assert opts.model == "claude-haiku-4-5-20251001"
    assert opts.thinking is not None
    assert opts.thinking["type"] == "disabled"

    manifest = get_default_registry().get("pr_creator")
    assert tuple(manifest.tools.argv_allowlist) == ("git", "gh")
    assert tuple(manifest.tools.argv_denylist) == PR_CREATOR_DENYLIST
    assert manifest.mcp == []

    # PreToolUse / PostToolUse / Stop are populated; UserPromptSubmit is
    # intentionally absent — pr_creator is single-turn so goal_pin would
    # never fire, and the manifest no longer attaches it.
    for event in ("PreToolUse", "PostToolUse", "Stop"):
        assert event in opts.hooks, event
        assert isinstance(opts.hooks[event][0], HookMatcher)
    assert "UserPromptSubmit" not in opts.hooks


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
            "Bash",
            {"command": "mvn -q compile"},
            ToolPermissionContext(),
        )

    result = asyncio.run(call())
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.parametrize(
    "factory",
    [_builder_client, _tester_client],
)
def test_permission_gate_denies_git_push_via_denylist(factory: Any) -> None:
    client = factory(_state_slice())
    gate = client.options.can_use_tool

    async def call() -> Any:
        return await gate(
            "Bash",
            {"command": "git push origin agent/x"},
            ToolPermissionContext(),
        )

    result = asyncio.run(call())
    assert isinstance(result, PermissionResultDeny)
    assert "git push" in result.message


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


def _pr_gate_call(client: ClaudeSDKClient, command: str) -> Any:
    gate = client.options.can_use_tool

    async def call() -> Any:
        return await gate("Bash", {"command": command}, ToolPermissionContext())

    return asyncio.run(call())


def test_pr_creator_permission_gate_allows_role_owned_gh_and_git() -> None:
    client = _pr_creator_client(_pr_state_slice())
    for command in (
        "gh pr list --head agent/wf-pr-123",
        "gh pr create --title T --body B",
        "git push origin agent/wf-pr-123",
    ):
        result = _pr_gate_call(client, command)
        assert isinstance(result, PermissionResultAllow), command


def test_pr_creator_permission_gate_denies_off_allowlist_argv() -> None:
    client = _pr_creator_client(_pr_state_slice())
    result = _pr_gate_call(client, "curl https://example.com")
    assert isinstance(result, PermissionResultDeny)
    assert "pr_creator allowlist" in result.message


def test_pr_creator_permission_gate_denies_gh_issue_and_merge() -> None:
    # The workflow owns the GitHub issue label lifecycle and no agent may
    # merge; both are gate-enforced for pr_creator, not honor-system.
    client = _pr_creator_client(_pr_state_slice())

    issue_result = _pr_gate_call(client, "gh issue edit 5 --add-label df:done")
    assert isinstance(issue_result, PermissionResultDeny)
    assert "denied for role 'pr_creator'" in issue_result.message

    merge_result = _pr_gate_call(client, "gh pr merge 5 --squash")
    assert isinstance(merge_result, PermissionResultDeny)
    assert "denied for all agent roles" in merge_result.message


# ---------- run_<role>: structured-output dict shape ----------


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
    stream. PR C removed the ``diff_capture`` sink, so this fake no
    longer needs to seed a patches list — patches are computed by the
    build subgraph / Fixer activity from ``git diff``, not declared by
    ``run_<role>``.
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


def test_run_builder_returns_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "wp_id": "US-1",
        "status": "done",
        "edits": [
            {
                "path": "src/main/java/app/UserController.java",
                "operation": "modify",
                "intent": "Add cursor parameter to the list endpoint.",
            }
        ],
        "blockers": [],
        "summary": "Edited UserController to accept cursor parameter.",
    }

    def _compose(role: str, compose_state: Any, **_kwargs: Any) -> _FakeClient:
        return _FakeClient(
            responses=[[_structured_assistant(payload), _result_msg()]],
        )

    monkeypatch.setattr(builder_mod, "compose", _compose)

    out = asyncio.run(run_builder(_state_slice()))

    assert out["wp_id"] == "US-1"
    assert out["status"] == "done"
    assert out["edits"] == payload["edits"]
    assert out["blockers"] == []
    assert out["summary"] == payload["summary"]
    # Patches are computed by the build subgraph, not the agent.
    assert "patches" not in out


def test_run_builder_synthesises_blocked_on_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No StructuredOutput tool call → fall back to a status=blocked payload.

    The build subgraph will route this through reconciliation_findings
    (kind builder_blocked) rather than into tester_findings.
    """

    def _compose(role: str, compose_state: Any, **_kwargs: Any) -> _FakeClient:
        return _FakeClient(
            responses=[
                [_assistant("free-form, no tool call"), _result_msg()],
                # The run_to_completion retry will see another non-structured
                # turn and raise ParseError.
                [_assistant("still free-form"), _result_msg()],
            ],
        )

    monkeypatch.setattr(builder_mod, "compose", _compose)

    out = asyncio.run(run_builder(_state_slice()))

    assert out["wp_id"] == "US-1"
    assert out["status"] == "blocked"
    assert out["blockers"] and "parseable" in out["blockers"][0]


def _structured_assistant(payload: dict) -> AssistantMessage:
    """An assistant turn that emits the SDK's synthetic StructuredOutput tool."""
    return AssistantMessage(
        content=[
            ToolUseBlock(
                id="toolu_tester_1",
                name="StructuredOutput",
                input=payload,
            )
        ],
        model="fake-model",
    )


def test_run_tester_returns_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "summary": "Added cursor pagination coverage",
        "coverage": [
            {
                "wp_id": "US-1",
                "predicate": "cursor returns next page",
                "test_names": ["UserControllerTest.cursor"],
            }
        ],
        "findings": [],
    }

    def _compose(role: str, compose_state: Any, **_kwargs: Any) -> _FakeClient:
        return _FakeClient(
            responses=[[_structured_assistant(payload), _result_msg()]],
        )

    monkeypatch.setattr(tester_mod, "compose", _compose)

    out = asyncio.run(run_tester(_state_slice()))

    assert out["coverage"][0]["wp_id"] == "US-1"
    assert out["findings"] == []
    assert out["summary"] == "Added cursor pagination coverage"
    assert out["parse_failure"] is False
    # PR C: the Tester no longer declares patches; the build subgraph
    # computes them from git diff after the run.
    assert "patches" not in out


def test_run_tester_signals_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No StructuredOutput tool call → return ``parse_failure=True``.

    PR C: the Tester no longer synthesizes its own ``unclear_predicate``
    finding — the build subgraph emits a ``reconciliation_findings``
    entry (kind ``tester_parse_failure``) instead, so the Tester's
    ``findings`` channel stays exclusively Tester-declared.
    """

    def _compose(role: str, compose_state: Any, **_kwargs: Any) -> _FakeClient:
        return _FakeClient(
            responses=[[_assistant("free-form text, no tool call"), _result_msg()]],
        )

    monkeypatch.setattr(tester_mod, "compose", _compose)

    out = asyncio.run(run_tester(_state_slice()))

    assert out["coverage"] == []
    assert out["findings"] == []
    assert out["parse_failure"] is True


def test_run_pr_creator_returns_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "status": "created",
        "pr_url": "https://github.com/acme/demo/pull/42",
        "summary": "Opened PR for agent/wf-pr-123.",
    }
    fake = _FakePRClient([[_structured_assistant(payload), _result_msg()]])

    def _compose(*_args: Any, **_kwargs: Any) -> _FakePRClient:
        return fake

    monkeypatch.setattr(pr_creator_mod, "compose", _compose)

    out = asyncio.run(run_pr_creator(_pr_state_slice()))

    assert out == payload
    assert len(fake.queries) == 1
    # The rendered user message should carry the feature branch the
    # agent is expected to push, and the "untrusted data" framing.
    assert "agent/wf-pr-123" in fake.queries[0]
    assert "git push origin" in fake.queries[0]
    assert "untrusted" in fake.queries[0].lower()
