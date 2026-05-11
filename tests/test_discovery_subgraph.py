from __future__ import annotations

import asyncio

from darkfactory.stages import discovery as discovery_mod
from darkfactory.stages.discovery import discovery_subgraph


def test_discovery_subgraph_produces_valid_spec(monkeypatch):
    po_out: dict = {
        "stories": [
            {
                "id": "US-1",
                "title": "Cursor pagination",
                "as_a": "API consumer",
                "i_want": "to page users with a cursor",
                "so_that": "I can scroll large result sets",
                "acceptance_criteria": ["GET /api/users?cursor=… returns next page"],
            }
        ],
        "problem": "",
        "expected_behavior": [],
        "compatibility_risks": [],
        "open_assumptions": [],
    }
    architect_out = {
        "current_understanding": "The existing API accepts limit and offset.",
        "proposed_design": "Add cursor parsing near the user lookup flow.",
        "contract_changes": {"api": [], "data": [], "events": []},
        "test_strategy": "Cover first and final cursor pages.",
        "work_packages": [
            {
                "id": "WP-1",
                "story_id": "US-1",
                "title": "Add cursor pagination",
                "intent": "Add cursor param to UserController; extend UserService.",
                "verification": [
                    "GET /api/users?cursor=… returns next page"
                ],
                "repo_areas": ["Backend user lookup flow"],
                "candidate_files": ["src/main/java/app/UserController.java"],
                "dependencies": [],
                "estimated_scope": "small",
                "notes": ["Risk: backward-compat of existing page param"],
            }
        ],
    }
    reviewer_out: dict = {
        "approved": True,
        "reason": "looks good",
        "edits": {},
        "notes": [],
    }

    seen: dict[str, dict] = {}

    async def fake_po(state):
        seen["po"] = dict(state)
        return po_out

    async def fake_architect(state):
        seen["architect"] = dict(state)
        return architect_out

    async def fake_critic(state):
        seen["critic"] = dict(state)
        return reviewer_out

    monkeypatch.setattr(discovery_mod, "run_po", fake_po)
    monkeypatch.setattr(discovery_mod, "run_architect", fake_architect)
    monkeypatch.setattr(discovery_mod, "run_plan_critic", fake_critic)

    graph = discovery_subgraph()

    out = asyncio.run(
        graph.ainvoke(
            {
                "user_request": "Add cursor-based pagination to /api/users.",
                "repo_context": {
                    "agents_md": "Spring Boot demo repo.",
                    "repo_map": "UserController.java\n  public class UserController",
                    "git_log": ["abc1234 initial"],
                },
            }
        )
    )

    stories = out.get("stories", [])
    assert len(stories) == 1
    assert stories[0]["id"] == "US-1"

    spec = out.get("spec", [])
    assert len(spec) == 1
    slice_ = spec[0]
    # Legacy spec is derived from work_packages: spec[*].story_id is the WP id
    # (the build-node id), not the user-story id. The user-story id rides on
    # work_packages[*].story_id below.
    assert slice_["story_id"] == "WP-1"
    assert "UserController.java" in slice_["affected_files"][0]
    assert slice_["depends_on"] == []

    decision = out.get("review_decision")
    assert decision is not None
    assert decision["approved"] is True

    # Architect node also persists v2 work_packages alongside the legacy spec.
    work_packages = out.get("work_packages") or []
    assert work_packages and work_packages[0]["story_id"] == "US-1"
    assert work_packages[0]["id"].startswith("WP-") or work_packages[0]["id"] == "US-1"

    # Architect saw the stories that PO produced.
    assert seen["architect"].get("stories") and seen["architect"]["stories"][0]["id"] == "US-1"
    # Plan Critic saw both stories and the v2 work_packages (story_id preserved).
    assert seen["critic"].get("stories") and seen["critic"]["stories"][0]["id"] == "US-1"
    critic_wps = seen["critic"].get("work_packages") or []
    assert critic_wps and critic_wps[0]["story_id"] == "US-1"
