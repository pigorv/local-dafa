"""Unit tests for the Semantic Verifier SDK role."""
from __future__ import annotations

import asyncio
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

from darkfactory.agents import verifier_semantic as verifier_semantic_mod
from darkfactory.agents._sdk_common import load_prompt
from darkfactory.agents.compose import ComposeState, compose
from darkfactory.agents.verifier_semantic import run_verifier_semantic
from darkfactory.state import (
    ContractChanges,
    ImplementationBrief,
    VerificationPredicate,
    WorkPackage,
)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="fake-model")


def _structured_assistant(payload: dict) -> AssistantMessage:
    """An assistant turn that emits the SDK's synthetic StructuredOutput tool."""
    return AssistantMessage(
        content=[
            ToolUseBlock(
                id="toolu_verifier_1",
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
    """Replace the live ``compose`` call so ``run_verifier_semantic`` opens our fake."""

    def _compose(*_args: Any, **_kwargs: Any) -> _FakeClient:
        return fake

    monkeypatch.setattr(verifier_semantic_mod, "compose", _compose)


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
    return {
        "user_request": "Make missing customers return 404",
        "implementation_brief": _brief(),
        "coverage_entries": [
            {
                "wp_id": "WP-1",
                "predicate": "GET /customers/{unknown_id} returns 404 and an error body",
                "test_names": ["CustomerControllerTest.missingCustomerReturns404"],
            }
        ],
        "test_results": [
            {
                "runner": "maven",
                "returncode": 0,
                "passed": 1,
                "failed": 0,
                "errors": [],
                "duration_s": 1.0,
            }
        ],
        "findings": [],
        "tester_findings": [],
        "spec": [
            {
                "story_id": "WP-1",
                "test_files": ["src/test/java/app/CustomerControllerTest.java"],
            }
        ],
    }


def test_verifier_semantic_client_options_match_plan_critic_pattern() -> None:
    state = ComposeState.task_only("task-1")
    client = compose("verifier_semantic", state, task_id=state.task_id)
    opts = client.options
    assert opts is not None
    assert opts.allowed_tools == []
    assert opts.mcp_servers == {}
    assert opts.setting_sources == ["project"]
    assert opts.model == "claude-sonnet-4-5-20250929"
    assert opts.thinking is not None
    assert opts.thinking["type"] == "disabled"
    # Stop heartbeat plus the StructuredOutput hint hook — no unreachable
    # PreToolUse / UserPromptSubmit hooks (zero-tool, single-prompt role).
    assert "PreToolUse" not in opts.hooks
    assert "UserPromptSubmit" not in opts.hooks
    assert "Stop" in opts.hooks
    assert isinstance(opts.hooks["Stop"][0], HookMatcher)
    assert "PostToolUse" in opts.hooks
    post_hook_names = [
        hook.__name__
        for matcher in opts.hooks["PostToolUse"]
        for hook in matcher.hooks
    ]
    assert "structured_output_hint_hook" in post_hook_names
    # SDK output_format is wired from the manifest's structured_output schema.
    assert opts.output_format is not None
    assert opts.output_format["type"] == "json_schema"
    assert opts.output_format["schema"]["title"] == "PredicateCoverageReport"


def test_verifier_semantic_prompt_names_predicate_statuses_and_placeholders() -> None:
    prompt = load_prompt("verifier_semantic")
    assert "predicate_coverage" in prompt
    assert "covered" in prompt
    assert "uncovered" in prompt
    assert "weakly_covered" in prompt
    assert "tautological" in prompt
    # User-message template placeholders must be present so
    # render_role_user_message can substitute them.
    for placeholder in (
        "$user_request",
        "$implementation_brief",
        "$spec",
        "$coverage_entries",
        "$test_files",
        "$test_results",
        "$findings",
        "$tester_findings",
    ):
        assert placeholder in prompt, placeholder


def test_run_verifier_semantic_returns_structured_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "predicate_coverage": [
            {
                "wp_id": "WP-1",
                "predicate": "GET /customers/{unknown_id} returns 404 and an error body",
                "status": "covered",
                "evidence": "CustomerControllerTest.missingCustomerReturns404 asserts 404 and body",
            }
        ]
    }
    fake = _FakeClient([[_structured_assistant(payload), _result()]])
    _patch_client(monkeypatch, fake)

    out = asyncio.run(run_verifier_semantic(_state_slice()))

    assert out == payload
    assert len(fake.queries) == 1
    # The rendered user message must carry the substituted inputs.
    assert "Implementation Brief" in fake.queries[0]
    assert "Tester coverage entries" in fake.queries[0]
    assert "CustomerControllerTest.java" in fake.queries[0]


def test_run_verifier_semantic_falls_back_on_missing_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No StructuredOutput tool-use block → _drain returns structured=None;
    # runtime returns an empty predicate_coverage list so the verify
    # subgraph still produces a verdict (every predicate ends up uncovered).
    fake = _FakeClient([[_assistant("free-form text, no tool call"), _result()]])
    _patch_client(monkeypatch, fake)

    out = asyncio.run(run_verifier_semantic(_state_slice()))

    assert out == {"predicate_coverage": []}
