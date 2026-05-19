import hashlib

import pytest
from unittest.mock import MagicMock

from darkfactory.runtime import activities


class _RecordingLangfuse:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.flushed = 0

    def create_score(self, *, name, value, trace_id, data_type=None, comment=None) -> None:
        self.calls.append({"name": name, "value": value, "trace_id": trace_id, "comment": comment})

    def flush(self) -> None:
        self.flushed += 1


@pytest.fixture
def fake_langfuse(monkeypatch):
    fake = _RecordingLangfuse()
    monkeypatch.setattr(activities, "_langfuse_client", lambda: fake)
    return fake


@pytest.fixture
def in_activity(monkeypatch):
    info = MagicMock(workflow_id="df-issue-acme-repo-42")
    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(activities.activity, "info", lambda: info)


def test_emit_scores_outside_activity_is_noop(fake_langfuse, monkeypatch):
    monkeypatch.setattr(activities.activity, "in_activity", lambda: False)
    activities._emit_langfuse_scores("verify", {"passed": 1.0})
    assert fake_langfuse.calls == []


def test_emit_scores_targets_coalesced_trace_id(fake_langfuse, in_activity):
    activities._emit_langfuse_scores("verify", {"passed": 1.0, "predicate_coverage_pct": 0.75})
    assert len(fake_langfuse.calls) == 2
    expected = activities._coalesced_trace_id("df-issue-acme-repo-42")
    assert all(c["trace_id"] == expected for c in fake_langfuse.calls)
    assert {c["name"] for c in fake_langfuse.calls} == {
        "verify.passed",
        "verify.predicate_coverage_pct",
    }
    assert fake_langfuse.flushed == 1


def test_emit_scores_swallows_client_errors(monkeypatch, in_activity, caplog):
    broken = MagicMock()
    broken.create_score.side_effect = RuntimeError("langfuse down")
    monkeypatch.setattr(activities, "_langfuse_client", lambda: broken)
    activities._emit_langfuse_scores("verify", {"passed": 0.0})  # must not raise
    assert "score emit failed" in caplog.text


def test_coalesced_trace_id_matches_collector_formula():
    wf_id = "df-issue-acme-repo-42"
    assert activities._coalesced_trace_id(wf_id) == hashlib.sha256(wf_id.encode()).hexdigest()[:32]
