from __future__ import annotations

from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph.message import add_messages

from darkfactory.state import (
    IssueRef,
    IssueRunRequest,
    SpecSlice,
    init_state_from_issue,
    merge,
    merge_specs,
    overwrite,
)


def _slice(story_id: str, approach: str = "x") -> SpecSlice:
    return {
        "story_id": story_id,
        "approach": approach,
        "affected_files": [],
        "new_files": [],
        "test_files": [],
        "risks": [],
        "depends_on": [],
    }


class TestMergeSpecs:
    def test_appends_new_slice(self):
        left = [_slice("s1")]
        right = [_slice("s2")]
        result = merge_specs(left, right)
        ids = sorted(s["story_id"] for s in result)
        assert ids == ["s1", "s2"]

    def test_upsert_by_story_id_last_write_wins(self):
        left = [_slice("s1", approach="old")]
        right = [_slice("s1", approach="new")]
        result = merge_specs(left, right)
        assert len(result) == 1
        assert result[0]["approach"] == "new"

    def test_handles_none_inputs(self):
        assert merge_specs(None, None) == []
        assert merge_specs(None, [_slice("s1")])[0]["story_id"] == "s1"
        assert merge_specs([_slice("s1")], None)[0]["story_id"] == "s1"


class TestOverwrite:
    def test_returns_new_value(self):
        assert overwrite("old", "new") == "new"

    def test_ignores_old_value(self):
        assert overwrite({"a": 1}, {"b": 2}) == {"b": 2}

    def test_none_new_replaces(self):
        assert overwrite("old", None) is None


class TestAddMessages:
    def test_appends_new_messages(self):
        left = [HumanMessage(content="hi", id="1")]
        right = [AIMessage(content="hello", id="2")]
        merged = add_messages(left, right)
        assert len(merged) == 2
        assert merged[0].content == "hi"
        assert merged[1].content == "hello"

    def test_updates_by_id(self):
        left = [HumanMessage(content="hi", id="1")]
        right = [HumanMessage(content="edited", id="1")]
        merged = add_messages(left, right)
        assert len(merged) == 1
        assert merged[0].content == "edited"


class TestIssueState:
    def test_init_state_from_issue_round_trips_issue_through_merge(self):
        issue = IssueRef(
            repo="octo-org/octo-repo",
            number=42,
            url="https://github.com/octo-org/octo-repo/issues/42",
            title="Add issue-driven workflow",
            body="Dark Factory should pick this up from an issue.",
            labels=["df:ready", "enhancement"],
        )
        req = IssueRunRequest(
            repo_url="https://github.com/octo-org/octo-repo.git",
            repo_path="/workspace",
            issue=issue,
        )

        initial = init_state_from_issue(req)
        merged = merge({}, initial)

        assert merged["issue"] == issue
        assert merged["issue"].model_dump() == issue.model_dump()
        assert merged["issue_comments"] == []
        assert merged["phase_comment_ids"] == {}
        assert merged["latest_spec_rev"] == 1
        assert merged["approval_record"] is None
        assert merged["last_seen_comment_id"] == 0

    def test_issue_state_phase_fields_are_overwritten(self):
        state = {
            "phase_comment_ids": {"triage": 1},
            "latest_spec_rev": 1,
            "approval_record": None,
            "last_seen_comment_id": 10,
        }
        merged = merge(
            state,
            {
                "phase_comment_ids": {"design:1": 2},
                "latest_spec_rev": 2,
                "approval_record": {"author": "octocat", "spec_rev": 2},
                "last_seen_comment_id": 99,
            },
        )

        assert merged["phase_comment_ids"] == {"design:1": 2}
        assert merged["latest_spec_rev"] == 2
        assert merged["approval_record"]["author"] == "octocat"
        assert merged["last_seen_comment_id"] == 99
