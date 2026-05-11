from __future__ import annotations

from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph.message import add_messages

from darkfactory.state import (
    IssueRef,
    IssueRunRequest,
    _bounded_add,
    init_state_from_issue,
    merge,
    merge_work_packages,
    overwrite,
    WorkPackageDict,
)


def _slice(story_id: str, approach: str = "x") -> WorkPackageDict:
    return {
        "story_id": story_id,
        "approach": approach,
        "affected_files": [],
        "new_files": [],
        "test_files": [],
        "risks": [],
        "depends_on": [],
    }


class TestMergeWorkPackages:
    def test_appends_new_work_package(self):
        left = [_slice("s1")]
        right = [_slice("s2")]
        result = merge_work_packages(left, right)
        ids = sorted(s["story_id"] for s in result)
        assert ids == ["s1", "s2"]

    def test_upsert_by_story_id_last_write_wins(self):
        left = [_slice("s1", approach="old")]
        right = [_slice("s1", approach="new")]
        result = merge_work_packages(left, right)
        assert len(result) == 1
        assert result[0]["approach"] == "new"

    def test_handles_none_inputs(self):
        assert merge_work_packages(None, None) == []
        assert merge_work_packages(None, [_slice("s1")])[0]["story_id"] == "s1"
        assert merge_work_packages([_slice("s1")], None)[0]["story_id"] == "s1"


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


class TestCoverageEntries:
    def test_coverage_entries_append_across_build_deltas(self):
        state = {
            "coverage_entries": [
                {
                    "wp_id": "WP-1",
                    "predicate": "cursor pagination is covered",
                    "test_names": ["test_cursor_page"],
                }
            ]
        }

        merged = merge(
            state,
            {
                "coverage_entries": [
                    {
                        "wp_id": "WP-2",
                        "predicate": "empty cursor is covered",
                        "test_names": ["test_empty_cursor"],
                    }
                ]
            },
        )

        assert [entry["wp_id"] for entry in merged["coverage_entries"]] == [
            "WP-1",
            "WP-2",
        ]


class TestTesterFindings:
    def test_tester_findings_append_across_build_deltas(self):
        state = {
            "tester_findings": [
                {
                    "kind": "unclear_predicate",
                    "wp_id": "WP-1",
                    "detail": "Predicate does not name the observable behavior.",
                }
            ]
        }

        merged = merge(
            state,
            {
                "tester_findings": [
                    {
                        "kind": "behavior_mismatch",
                        "wp_id": "WP-2",
                        "detail": "Response body lacks the expected cursor.",
                    }
                ]
            },
        )

        assert [finding["wp_id"] for finding in merged["tester_findings"]] == [
            "WP-1",
            "WP-2",
        ]


class TestBoundedAdd:
    def test_concat_under_cap_returns_full_list(self):
        reducer = _bounded_add("DARKFACTORY_TEST_LOG_MAX", 5)
        merged = reducer([{"i": 1}, {"i": 2}], [{"i": 3}])
        assert [e["i"] for e in merged] == [1, 2, 3]

    def test_concat_over_cap_drops_oldest(self):
        reducer = _bounded_add("DARKFACTORY_TEST_LOG_MAX", 3)
        merged = reducer(
            [{"i": 1}, {"i": 2}, {"i": 3}],
            [{"i": 4}, {"i": 5}],
        )
        assert [e["i"] for e in merged] == [3, 4, 5]

    def test_default_cap_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("DARKFACTORY_TEST_LOG_MAX", raising=False)
        reducer = _bounded_add("DARKFACTORY_TEST_LOG_MAX", 4)
        merged = reducer([{"i": i} for i in range(10)], [])
        assert [e["i"] for e in merged] == [6, 7, 8, 9]

    def test_env_var_override_takes_effect(self, monkeypatch):
        monkeypatch.setenv("DARKFACTORY_TEST_LOG_MAX", "2")
        reducer = _bounded_add("DARKFACTORY_TEST_LOG_MAX", 50)
        merged = reducer([{"i": 1}, {"i": 2}, {"i": 3}], [{"i": 4}])
        assert [e["i"] for e in merged] == [3, 4]

    def test_invalid_env_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DARKFACTORY_TEST_LOG_MAX", "not-a-number")
        reducer = _bounded_add("DARKFACTORY_TEST_LOG_MAX", 2)
        merged = reducer([{"i": 1}, {"i": 2}], [{"i": 3}])
        assert [e["i"] for e in merged] == [2, 3]

    def test_zero_or_negative_env_value_clamps_to_one(self, monkeypatch):
        monkeypatch.setenv("DARKFACTORY_TEST_LOG_MAX", "0")
        reducer = _bounded_add("DARKFACTORY_TEST_LOG_MAX", 5)
        merged = reducer([{"i": 1}, {"i": 2}], [{"i": 3}])
        assert merged == [{"i": 3}]

    def test_handles_none_inputs(self):
        reducer = _bounded_add("DARKFACTORY_TEST_LOG_MAX", 5)
        assert reducer(None, None) == []
        assert reducer(None, [{"i": 1}]) == [{"i": 1}]
        assert reducer([{"i": 1}], None) == [{"i": 1}]


class TestAttemptLogChannelCap:
    def _reload_state(self):
        import importlib
        import darkfactory.state as state_mod

        return importlib.reload(state_mod)

    def test_attempt_log_caps_via_merge(self, monkeypatch):
        monkeypatch.setenv("DARKFACTORY_ATTEMPT_LOG_MAX", "3")
        try:
            state_mod = self._reload_state()
            state: dict = {"attempt_log": []}
            for i in range(7):
                state = state_mod.merge(
                    state,
                    {"attempt_log": [{"source": "fixer_attempt", "attempt": i}]},
                )
            attempts = [entry["attempt"] for entry in state["attempt_log"]]
            assert attempts == [4, 5, 6]
        finally:
            monkeypatch.delenv("DARKFACTORY_ATTEMPT_LOG_MAX", raising=False)
            self._reload_state()

    def test_planning_attempt_log_caps_via_merge(self, monkeypatch):
        monkeypatch.setenv("DARKFACTORY_PLANNING_ATTEMPT_LOG_MAX", "2")
        try:
            state_mod = self._reload_state()
            state: dict = {"planning_attempt_log": []}
            for i in range(5):
                state = state_mod.merge(
                    state,
                    {"planning_attempt_log": [{"source": "plan_critic_reject", "attempt": i}]},
                )
            attempts = [entry["attempt"] for entry in state["planning_attempt_log"]]
            assert attempts == [3, 4]
        finally:
            monkeypatch.delenv("DARKFACTORY_PLANNING_ATTEMPT_LOG_MAX", raising=False)
            self._reload_state()


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
