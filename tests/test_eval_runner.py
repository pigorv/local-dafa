from pathlib import Path
from types import SimpleNamespace

import pytest

from darkfactory.eval import runner
from darkfactory.eval.runner import (
    BenchmarkConfigError,
    WorkflowRun,
    _configure_langfuse_eval_env,
    _evaluate,
    _write_langfuse_result,
    coalesced_trace_id,
    load_dataset,
)
from darkfactory.state import RunResult


def _case(**expected_overrides):
    expected = {
        "pr_created": True,
        "verify_passed": True,
        "predicate_coverage_min_pct": 0.8,
        "reviewer_severity_max": "medium",
        "reviewer_recommendation_approve": True,
        "planning_attempts_max": 2,
        "fixer_attempts_max": 1,
        "must_not_touch_files": [".github/", "package-lock.json"],
        "must_touch_files": ["src/foo.py"],
    }
    expected.update(expected_overrides)
    return {
        "id": "case-1",
        "description": "demo",
        "repo_url": "/tmp/repo",
        "repo_sha": "abc123",
        "issue": {"title": "Fix it", "body": "Broken"},
        "expected": expected,
        "tags": ["bugfix"],
    }


def _run(state_overrides=None):
    state = {
        "pr_url": "https://github.example/acme/repo/pull/1",
        "verify_summary": {
            "passed": True,
            "predicate_coverage": [
                {"status": "covered"},
                {"status": "covered"},
                {"status": "weakly_covered"},
            ],
        },
        "planning_attempts": 1,
        "fixer_attempts_by_wp": {},
        "review_decision": {"severity": "low", "recommendation": "approve"},
        "patches": [{"path": "src/foo.py"}],
    }
    if state_overrides:
        state.update(state_overrides)
    return WorkflowRun(
        workflow_id="wf-1",
        workflow_run_id="run-1",
        result=RunResult(status="merged", state=state),
    )


def test_evaluate_passing_case():
    result = _evaluate(_case(), _run())
    assert result.passed is True
    assert result.misses == []
    assert result.trace_id == coalesced_trace_id("wf-1", "run-1")


def test_evaluate_uses_review_decision_not_reviewer_summary():
    result = _evaluate(
        _case(),
        _run(
            {
                "review_decision": {
                    "severity": "high",
                    "recommendation": "request_changes",
                }
            }
        ),
    )
    assert result.passed is False
    assert any("reviewer.severity" in miss for miss in result.misses)


def test_forbidden_directory_touch_fails():
    result = _evaluate(
        _case(),
        _run({"patches": [{"path": ".github/workflows/test.yml"}]}),
    )
    assert result.passed is False
    assert any("forbidden" in miss for miss in result.misses)


def test_worker_sentinel_paths_are_ignored():
    result = _evaluate(
        _case(),
        _run({"patches": [{"path": "src/foo.py"}, {"path": "(worker-error)"}]}),
    )
    assert result.passed is True


def test_fixer_attempts_count_invocations_not_target_buckets():
    result = _evaluate(
        _case(fixer_attempts_max=1),
        _run(
            {
                "fixer_attempts_by_wp": {"WP-2": 1, "WP-4": 1},
                "attempt_log": [
                    {
                        "source": "fixer_attempt",
                        "attempt": 1,
                        "target_wps": ["WP-2", "WP-4"],
                    },
                    {
                        "source": "fixer_escalation",
                        "reason": "needs_brief_change",
                    },
                ],
            }
        ),
    )

    assert result.actual["fixer_attempts"] == 1
    assert result.actual["fixer_target_attempts"] == 2
    assert not any("fixer_attempts" in miss for miss in result.misses)


def test_load_dataset_rejects_empty_cases(tmp_path: Path):
    path = tmp_path / "benchmark.yaml"
    path.write_text("version: 1\ncases: []\n")
    with pytest.raises(BenchmarkConfigError):
        load_dataset(path)


def test_eval_langfuse_env_uses_local_defaults(monkeypatch):
    for key in (
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runner, "_running_in_container", lambda: False)

    assert _configure_langfuse_eval_env() is True
    assert runner.os.environ["LANGFUSE_HOST"] == "http://localhost:3000"
    assert runner.os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-lf-local"
    assert runner.os.environ["LANGFUSE_SECRET_KEY"] == "sk-lf-local"


def test_eval_langfuse_env_maps_compose_host_for_host_cli(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse-web:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    monkeypatch.setattr(runner, "_running_in_container", lambda: False)

    assert _configure_langfuse_eval_env() is True
    assert runner.os.environ["LANGFUSE_HOST"] == "http://localhost:3000"
    assert runner.os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-lf-local"
    assert runner.os.environ["LANGFUSE_SECRET_KEY"] == "sk-lf-local"


def test_eval_langfuse_env_requires_keys_for_remote_host(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(runner, "_running_in_container", lambda: False)

    assert _configure_langfuse_eval_env() is False


def test_write_langfuse_scores_target_dataset_run_only():
    class FakeDatasetRunItems:
        def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(dataset_run_id="dataset-run-1")

    class FakeClient:
        def __init__(self):
            self.api = SimpleNamespace(dataset_run_items=FakeDatasetRunItems())
            self.scores = []
            self.flushed = False

        def create_dataset(self, **kwargs):
            self.dataset = kwargs

        def create_dataset_item(self, **kwargs):
            self.item = kwargs
            return SimpleNamespace(id="dataset-item-1")

        def create_score(self, **kwargs):
            self.scores.append(kwargs)

        def flush(self):
            self.flushed = True

    client = FakeClient()
    result = _evaluate(_case(), _run())

    _write_langfuse_result(
        client=client,
        dataset_name="benchmark-prod",
        run_name="unit-run",
        case=_case(),
        result=result,
    )

    assert client.api.dataset_run_items.kwargs["trace_id"] == result.trace_id
    assert client.scores
    assert all(score["dataset_run_id"] == "dataset-run-1" for score in client.scores)
    assert all("trace_id" not in score for score in client.scores)
    assert all(score["metadata"]["trace_id"] == result.trace_id for score in client.scores)
    assert client.flushed is True
