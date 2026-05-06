from __future__ import annotations

import asyncio

import pytest

from darkfactory.agents.triage import make_triage_client
from darkfactory.state import IssueComment, IssueRef
from darkfactory.stages import triage as triage_mod
from darkfactory.stages.triage import CLARIFY_EDGE, READY_EDGE, triage_subgraph


_AUTH_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def _clear_auth_env(monkeypatch) -> None:
    for var in _AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_make_triage_client_uses_anthropic_api_key_when_set(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client = make_triage_client()
    assert client.api_key == "sk-test"
    assert client.auth_token is None


def test_make_triage_client_falls_back_to_claude_code_oauth_token(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test-token")
    client = make_triage_client()
    assert client.api_key is None
    assert client.auth_token == "oauth-test-token"


def test_make_triage_client_raises_when_no_auth_present(monkeypatch):
    _clear_auth_env(monkeypatch)
    with pytest.raises(RuntimeError, match="Triage agent requires"):
        make_triage_client()


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
