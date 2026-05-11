"""Unit tests for the Fixer SDK role."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)
from pydantic import ValidationError

from claude_agent_sdk import ClaudeSDKClient

from darkfactory.agents import fixer as fixer_mod
from darkfactory.agents._sdk_common import load_prompt
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.fixer import FixerOutput, run_fixer
from darkfactory.agents.registry import get_default_registry


# Matches the canonical worker allowlist; kept inline so the test fails loud
# if the manifest's allowed-tools list drifts from ARCHITECTURE.md §5.5.
ALLOWED_TOOLS: list[str] = ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "sandbox_bash"]


def _fixer_client(state_slice: dict) -> ClaudeSDKClient:
    state = ComposeState.from_mapping(state_slice)
    return compose("fixer", state, task_id=state.task_id)
from darkfactory.runtime.workflow import (
    FIXER_MAX_ATTEMPTS,
    _fixer_budget_exhaustion,
    _fixer_decision_escalation,
    _infeasible_predicate_escalation,
    _record_fixer_attempt_delta,
)
from darkfactory.state import (
    ContractChanges,
    ImplementationBrief,
    VerificationPredicate,
    WorkPackage,
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
    def __init__(
        self,
        responses: list[list[Any]],
        seeded_sink: list[dict[str, Any]] | None = None,
        patches_sink: list[dict[str, Any]] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._seeded_sink = list(seeded_sink or [])
        self._patches_sink = patches_sink
        self.queries: list[str] = []

    async def __aenter__(self) -> "_FakeClient":
        if self._patches_sink is not None:
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


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    def _compose(role: str, compose_state: Any, **_kwargs: Any) -> _FakeClient:
        fake._patches_sink = compose_state.patches_sink
        return fake

    monkeypatch.setattr(fixer_mod, "compose", _compose)


def _brief() -> ImplementationBrief:
    return ImplementationBrief(
        problem="Customers need missing lookups to fail clearly.",
        expected_behavior=["Unknown customers return 404 with a stable body."],
        current_understanding="The controller currently returns a generic error.",
        proposed_design="Map missing customers to the API error handler.",
        contract_changes=ContractChanges(api=[], data=[], events=[]),
        compatibility_risks=[],
        open_assumptions=[],
        test_strategy="Exercise the missing-customer HTTP path.",
        work_packages=[
            WorkPackage(
                id="WP-1",
                story_id="US-1",
                title="Missing customer",
                intent="Return a clear 404 for unknown customers.",
                verification=[
                    VerificationPredicate.model_validate(
                        "GET /customers/{unknown_id} returns 404 and an error body"
                    )
                ],
                repo_areas=["Customer API"],
                candidate_files=["src/main/java/app/CustomerController.java"],
                dependencies=[],
                estimated_scope="small",
                notes=[],
            )
        ],
    )


def _state_slice() -> dict:
    predicate = "GET /customers/{unknown_id} returns 404 and an error body"
    return {
        "user_request": "Make missing customers return 404",
        "task_id": "task-123",
        "current_slice": "WP-1",
        "implementation_brief": _brief(),
        "spec": [
            {
                "story_id": "WP-1",
                "approach": "Map missing customers to 404.",
                "affected_files": ["src/main/java/app/CustomerController.java"],
                "verification": predicate,
            }
        ],
        "test_results": [
            {
                "runner": "maven",
                "returncode": 1,
                "passed": 0,
                "failed": 1,
                "errors": ["expected 404 but was 500"],
                "duration_s": 1.0,
            }
        ],
        "findings": [
            {
                "tool": "javac",
                "severity": "error",
                "file": "src/main/java/app/CustomerController.java",
                "line": 42,
                "rule": "compile",
                "message": "cannot find symbol MissingCustomerException",
            }
        ],
        "tester_findings": [
            {
                "kind": "behavior_mismatch",
                "wp_id": "WP-1",
                "detail": "Implementation returns 500 for missing customers.",
            }
        ],
        "review_decision": {
            "severity": "high",
            "issues": ["Missing customer path does not trace to WP-1."],
            "recommendation": "request_changes",
        },
        "verify_summary": {
            "passed": False,
            "failed_tests": 1,
            "hard_findings": 1,
            "predicate_coverage": [
                {
                    "wp_id": "WP-1",
                    "predicate": predicate,
                    "status": "uncovered",
                    "evidence": "CustomerControllerTest failed before asserting body.",
                }
            ],
        },
        "patches": [
            {
                "path": "src/main/java/app/CustomerController.java",
                "diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
                "author_agent": "builder",
                "slice_id": "WP-1",
            }
        ],
    }


def test_fixer_client_options_are_tool_using_and_hermetic() -> None:
    client = _fixer_client(_state_slice())
    opts = client.options
    assert opts is not None

    assert opts.setting_sources == []
    assert opts.allowed_tools == ALLOWED_TOOLS
    assert "Bash" in opts.allowed_tools  # built-in Bash enabled for the Fixer
    assert opts.cwd == "/workspace"
    assert "darkfactory" in opts.mcp_servers
    assert callable(opts.can_use_tool)
    assert opts.model == "claude-sonnet-4-5-20250929"
    assert opts.temperature == 0.1
    assert opts.thinking is not None
    assert opts.thinking["type"] == "disabled"
    manifest_allowlist = frozenset(
        get_default_registry().get("fixer").tools.argv_allowlist
    )
    assert manifest_allowlist == frozenset(
        {"mvn", "gradle", "./gradlew", "git", "cat", "ls"}
    )

    for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"):
        assert event in opts.hooks, event
        assert isinstance(opts.hooks[event][0], HookMatcher)

    pre_hook_names = [
        hook.__name__
        for matcher in opts.hooks["PreToolUse"] or []
        for hook in matcher.hooks or []
    ]
    # ``path_guard`` is prepended onto the first matcher by ``build_options``
    # regardless of how the rest of the PreToolUse hooks are packed across
    # matchers (compose emits one matcher per attachment; the imperative path
    # bundled them into one).
    assert opts.hooks["PreToolUse"][0].hooks[0].__name__ == "path_guard_hook"
    assert "loop_breaker_hook" in pre_hook_names
    assert "call_cap_hook" in pre_hook_names
    assert "heartbeat_hook" in pre_hook_names

    post_hook_names = [
        hook.__name__
        for matcher in opts.hooks["PostToolUse"] or []
        for hook in matcher.hooks or []
    ]
    assert "diff_capture_hook" in post_hook_names
    assert "prompt_injection_guard_hook" in post_hook_names
    assert "heartbeat_hook" in post_hook_names


def test_fixer_permission_gate_allows_focused_maven_command() -> None:
    gate = _fixer_client(_state_slice()).options.can_use_tool

    async def call() -> Any:
        return await gate(
            "sandbox_bash",
            {"argv": ["mvn", "-q", "test"]},
            ToolPermissionContext(),
        )

    assert isinstance(asyncio.run(call()), PermissionResultAllow)


def test_fixer_permission_gate_denies_merge_command() -> None:
    gate = _fixer_client(_state_slice()).options.can_use_tool

    async def call() -> Any:
        return await gate(
            "sandbox_bash",
            {"argv": ["gh", "pr", "merge", "42"]},
            ToolPermissionContext(),
        )

    result = asyncio.run(call())
    assert isinstance(result, PermissionResultDeny)
    assert "denied for all agent roles" in result.message


def test_fixer_prompt_names_decisions_and_diff_capture() -> None:
    prompt = load_prompt("fixer")
    assert "fixed" in prompt
    assert "needs_brief_change" in prompt
    assert "cannot_fix" in prompt
    assert 'edit_kind="fixer"' in prompt
    assert "Do not alter the brief" in prompt
    assert "No prose, no markdown fences" in prompt


def test_fixer_output_validates_decision_values() -> None:
    out = FixerOutput.model_validate(
        {
            "decision": "fixed",
            "target_wp": "WP-1",
            "target_predicates": ["GET /customers/{unknown_id} returns 404"],
            "summary": "Mapped missing customers to 404.",
            "reason": "The behavior is required by WP-1.",
        }
    )
    assert out.decision == "fixed"
    assert out.patches == []

    with pytest.raises(ValidationError):
        FixerOutput.model_validate(
            {
                "decision": "patch_code",
                "target_wp": "WP-1",
                "target_predicates": [],
            }
        )


def test_fixer_budget_counts_by_predicate_and_wp() -> None:
    state: dict[str, Any] = {
        "verify_summary": {
            "passed": False,
            "predicate_coverage": [
                {
                    "wp_id": "WP-1",
                    "predicate": "GET /customers/{unknown_id} returns 404",
                    "status": "uncovered",
                    "evidence": "",
                }
            ],
        },
        "fixer_attempts_by_predicate": {},
        "fixer_attempts_by_wp": {},
    }

    first = _record_fixer_attempt_delta(state)
    state.update(first)
    assert state["fixer_attempts_by_wp"] == {"WP-1": 1}
    assert state["fixer_attempts_by_predicate"] == {
        "GET /customers/{unknown_id} returns 404": 1
    }
    assert _fixer_budget_exhaustion(state) is None

    second = _record_fixer_attempt_delta(state)
    state.update(second)
    assert state["fixer_attempts_by_wp"]["WP-1"] == FIXER_MAX_ATTEMPTS
    assert (
        state["fixer_attempts_by_predicate"][
            "GET /customers/{unknown_id} returns 404"
        ]
        == FIXER_MAX_ATTEMPTS
    )

    exhausted = _fixer_budget_exhaustion(state)
    assert exhausted == {
        "reason": "fixer_budget_exhausted",
        "target_wps": ["WP-1"],
        "target_predicates": ["GET /customers/{unknown_id} returns 404"],
    }


def test_infeasible_predicate_finding_short_circuits_before_fixer() -> None:
    state: dict[str, Any] = {
        "tester_findings": [
            {
                "kind": "infeasible_predicate",
                "wp_id": "WP-4",
                "detail": (
                    "Predicate requires @SpringBootTest and MockMvc but the brief "
                    "forbids pom.xml changes; no spring-boot-starter-test in repo."
                ),
            }
        ],
        "fixer_attempts_by_wp": {},
        "fixer_attempts_by_predicate": {},
    }

    escalation = _infeasible_predicate_escalation(state)
    assert escalation is not None
    assert escalation["reason"] == "needs_brief_change"
    assert escalation["decision"] == "infeasible_predicate"
    assert escalation["target_wp"] == "WP-4"
    assert escalation["target_wps"] == ["WP-4"]
    assert "MockMvc" in escalation["details"][0]


def test_infeasible_predicate_ignored_when_resolved() -> None:
    state: dict[str, Any] = {
        "tester_findings": [
            {
                "kind": "infeasible_predicate",
                "wp_id": "WP-4",
                "detail": "stale",
                "resolved": True,
            }
        ],
    }
    assert _infeasible_predicate_escalation(state) is None


def test_behavior_mismatch_does_not_trigger_infeasible_escalation() -> None:
    state: dict[str, Any] = {
        "tester_findings": [
            {
                "kind": "behavior_mismatch",
                "wp_id": "WP-3",
                "detail": "negative offset normalised to 0 instead of empty",
            }
        ],
    }
    assert _infeasible_predicate_escalation(state) is None


def test_fixer_needs_brief_change_escalates_without_budget_retry() -> None:
    escalation = _fixer_decision_escalation(
        {
            "fixer_decision": {
                "decision": "needs_brief_change",
                "target_wp": "WP-1",
                "target_predicates": ["accepted behavior is contradictory"],
            }
        }
    )

    assert escalation == {
        "reason": "needs_brief_change",
        "decision": "needs_brief_change",
        "target_wp": "WP-1",
        "target_predicates": ["accepted behavior is contradictory"],
    }


def test_run_fixer_returns_decision_with_captured_patches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded_patch = {
        "path": "src/main/java/app/CustomerController.java",
        "diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
        "author_agent": "fixer",
        "slice_id": "WP-1",
        "sha": "deadbeef",
        "edit_kind": "fixer",
    }
    payload = json.dumps(
        {
            "decision": "fixed",
            "target_wp": "WP-1",
            "target_predicates": [
                "GET /customers/{unknown_id} returns 404 and an error body"
            ],
            "summary": "Mapped missing customers to 404.",
            "reason": "The failing predicate is within the approved brief.",
        }
    )
    fake = _FakeClient([[_assistant(payload), _result()]], seeded_sink=[seeded_patch])
    _patch_client(monkeypatch, fake)

    out = asyncio.run(run_fixer(_state_slice()))

    assert isinstance(out, FixerOutput)
    assert out.decision == "fixed"
    assert out.patches == [seeded_patch]
    assert len(fake.queries) == 1
    assert "Failing Work Package context" in fake.queries[0]
    assert "Failed mechanical diagnostics" in fake.queries[0]
    assert "Semantic coverage failures" in fake.queries[0]
    assert "Tester findings" in fake.queries[0]
    assert "Reviewer findings" in fake.queries[0]
    assert "expected 404 but was 500" in fake.queries[0]


def test_run_fixer_can_return_needs_brief_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "decision": "needs_brief_change",
            "target_wp": "WP-1",
            "target_predicates": [
                "GET /customers/{unknown_id} returns 404 and an error body"
            ],
            "summary": "The predicate conflicts with the accepted API contract.",
            "reason": "The approved behavior must change before code repair.",
        }
    )
    fake = _FakeClient([[_assistant(payload), _result()]])
    _patch_client(monkeypatch, fake)

    out = asyncio.run(run_fixer(_state_slice()))

    assert out.decision == "needs_brief_change"
    assert out.patches == []
