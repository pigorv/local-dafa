from __future__ import annotations

import asyncio

import pytest

from claude_agent_sdk import ClaudeSDKClient

from darkfactory.agents.compose import ComposeState, compose
from darkfactory.state import IssueComment, IssueRef
from darkfactory.stages import triage as triage_mod
from darkfactory.stages.triage import CLARIFY_EDGE, READY_EDGE, triage_subgraph


def _triage_client() -> ClaudeSDKClient:
    state = ComposeState.from_mapping({})
    return compose("triage", state, task_id=state.task_id)


def test_triage_client_returns_sdk_client_with_triage_prompt():
    client = _triage_client()
    assert isinstance(client, ClaudeSDKClient)
    options = client.options
    assert options.allowed_tools == ["Read", "Grep", "Glob", "mcp__*"]
    assert options.skills == "all"
    assert options.mcp_servers == {}
    # The prompt is rendered as the user message; the SDK enforces the
    # output shape via the StructuredOutput synthetic tool. system_prompt is
    # the claude_code preset so the SDK keeps the default Claude Code system
    # prompt (None would force --system-prompt "" and drop CLAUDE.md/skills).
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert options.output_format is not None
    assert options.output_format["type"] == "json_schema"


def test_triage_client_respects_env_model_override(monkeypatch):
    monkeypatch.setenv("LLM_TRIAGE_MODEL", "claude-haiku-override")
    client = _triage_client()
    assert client.options.model == "claude-haiku-override"


@pytest.mark.parametrize(
    ("triage_output", "expected_edge"),
    [
        (
            {
                "ready_to_build": True,
                "clarification_questions": [],
                "derived_user_request": "Add cursor pagination to GET /api/users.",
                "confidence": "high",
                "rationale": "The issue names the endpoint and expected behaviour.",
            },
            READY_EDGE,
        ),
        (
            {
                "ready_to_build": False,
                "clarification_questions": [
                    "Which endpoint should get cursor pagination?",
                    "What response shape should include the next cursor?",
                ],
                "derived_user_request": "",
                "confidence": "low",
                "rationale": "The issue asks for pagination but does not name an endpoint.",
            },
            CLARIFY_EDGE,
        ),
    ],
)
def test_triage_subgraph_merges_delta_and_selects_edge(
    monkeypatch, triage_output, expected_edge
):
    seen: dict[str, dict] = {}
    selected_edges: list[str] = []

    async def fake_triage(state):
        seen["triage"] = dict(state)
        return triage_output

    real_edge = triage_mod.triage_edge

    def record_edge(state):
        edge = real_edge(state)
        selected_edges.append(edge)
        return edge

    monkeypatch.setattr(triage_mod, "run_triage", fake_triage)
    monkeypatch.setattr(triage_mod, "triage_edge", record_edge)

    graph = triage_subgraph()
    initial_state = {
        "issue": IssueRef(
            repo="acme/widgets",
            number=42,
            url="https://github.com/acme/widgets/issues/42",
            title="Add pagination",
            body="The users endpoint needs pagination.",
            labels=["df:ready"],
        ),
        "issue_comments": [
            IssueComment(
                id=1001,
                author="octocat",
                body="Cursor pagination would be best.",
                created_at="2026-05-05T10:00:00Z",
            )
        ],
        "repo_context": {"repo_map": "src/api/users.py"},
    }

    out = asyncio.run(graph.ainvoke(initial_state))

    assert seen["triage"]["issue"].number == 42
    assert seen["triage"]["repo_context"]["repo_map"] == "src/api/users.py"
    assert out["ready_to_build"] is triage_output["ready_to_build"]
    assert out["clarification_questions"] == triage_output["clarification_questions"]
    assert out["derived_user_request"] == triage_output["derived_user_request"]
    assert out["confidence"] == triage_output["confidence"]
    assert out["rationale"] == triage_output["rationale"]
    assert selected_edges == [expected_edge]
