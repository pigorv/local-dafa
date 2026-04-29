"""Unit tests for stages/verify.py thin verify nodes."""
from __future__ import annotations

import types

import pytest

from darkfactory.stages import verify as verify_mod
from darkfactory.stages.verify import (
    run_compile_node,
    run_happy_path_node,
    run_linters_node,
    run_tests_node,
)


def _runtime(tmp_path, task_id="t-verify") -> types.SimpleNamespace:
    ctx = types.SimpleNamespace(
        repo_path=str(tmp_path),
        task_id=task_id,
    )
    return types.SimpleNamespace(context=ctx)


class FakeSandbox:
    """Records calls; returns a queued reply per argv prefix."""

    def __init__(self, replies: dict | None = None, default=None):
        self.replies = replies or {}
        self.default = default or {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }
        self.calls: list[list[str]] = []

    def exec(self, argv, timeout=None):  # noqa: ARG002 — match RepoSandbox.exec
        self.calls.append(list(argv))
        for prefix, reply in self.replies.items():
            if argv[: len(prefix)] == list(prefix):
                return reply
        return self.default


# --- run_tests_node ---

def test_run_tests_node_unknown_project(tmp_path):
    out = run_tests_node({}, _runtime(tmp_path))
    assert out["test_results"][0]["runner"] == "unknown"
    assert out["test_results"][0]["returncode"] == -1
    assert "cannot detect" in out["test_results"][0]["errors"][0]


def test_run_tests_node_maven_pass(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    sb = FakeSandbox(default={
        "returncode": 0,
        "stdout": "Tests run: 5, Failures: 0, Errors: 0, Skipped: 1\n",
        "stderr": "",
        "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sb)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    out = run_tests_node({}, _runtime(tmp_path))
    tr = out["test_results"][0]
    assert tr["runner"] == "maven"
    assert tr["returncode"] == 0
    assert tr["passed"] == 4
    assert tr["failed"] == 0
    assert tr["errors"] == []
    assert sb.calls[0] == ["mvn", "-q", "-B", "test"]


def test_run_tests_node_maven_fail_surfaces_errors(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    sb = FakeSandbox(default={
        "returncode": 1,
        "stdout": "Tests run: 2, Failures: 1, Errors: 0, Skipped: 0\n",
        "stderr": "",
        "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sb)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    out = run_tests_node({}, _runtime(tmp_path))
    tr = out["test_results"][0]
    assert tr["returncode"] == 1
    assert tr["failed"] == 1
    assert tr["passed"] == 1


def test_run_tests_node_no_runtime():
    assert run_tests_node({}, None) == {}


# --- run_linters_node ---

def test_run_linters_node_emits_findings(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    cs_stdout = (
        "[ERROR] /workspace/src/main/java/Foo.java:[12,4] (sizes) "
        "LineLength: Line is longer than 100 characters."
    )
    sb = FakeSandbox(replies={
        ("mvn", "-q", "-B", "checkstyle:check"): {
            "returncode": 1, "stdout": cs_stdout, "stderr": "", "timed_out": False,
        },
        ("mvn", "-q", "-B", "spotless:check"): {
            "returncode": 0, "stdout": "", "stderr": "", "timed_out": False,
        },
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sb)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    out = run_linters_node({}, _runtime(tmp_path))
    findings = out["findings"]
    assert len(findings) == 1
    assert findings[0]["tool"] == "checkstyle"
    assert findings[0]["rule"] == "LineLength"
    assert findings[0]["line"] == 12


def test_run_linters_node_unknown_build(tmp_path):
    assert run_linters_node({}, _runtime(tmp_path)) == {}


# --- run_compile_node ---

def test_run_compile_node_parses_javac_errors(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    compile_out = (
        "[ERROR] /workspace/src/main/java/Bar.java:[7,15] cannot find symbol\n"
    )
    sb = FakeSandbox(default={
        "returncode": 1, "stdout": compile_out, "stderr": "", "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sb)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    out = run_compile_node({}, _runtime(tmp_path))
    findings = out["findings"]
    assert len(findings) == 1
    assert findings[0]["tool"] == "javac"
    assert findings[0]["severity"] == "error"
    assert findings[0]["line"] == 7


def test_run_compile_node_synthesises_finding_on_unparseable_failure(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    sb = FakeSandbox(default={
        "returncode": 2, "stdout": "boom\n", "stderr": "", "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sb)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    out = run_compile_node({}, _runtime(tmp_path))
    assert len(out["findings"]) == 1
    assert out["findings"][0]["rule"] == "compile"


def test_run_compile_node_clean_returns_no_findings(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    sb = FakeSandbox(default={
        "returncode": 0, "stdout": "", "stderr": "", "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sb)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    out = run_compile_node({}, _runtime(tmp_path))
    assert out["findings"] == []


# --- run_happy_path_node ---

def test_run_happy_path_node_is_noop_stub(tmp_path):
    assert run_happy_path_node({}, _runtime(tmp_path)) == {}


# --- verify_fanout / verify_subgraph ---

def test_verify_fanout_emits_one_send_per_target():
    from langgraph.types import Send

    from darkfactory.stages.verify import VERIFY_TARGETS, verify_fanout

    sends = verify_fanout({"user_request": "x"})
    assert len(sends) == len(VERIFY_TARGETS)
    assert all(isinstance(s, Send) for s in sends)
    assert {s.node for s in sends} == set(VERIFY_TARGETS)


def test_verify_subgraph_defers_aggregate_until_all_fanout_nodes_finish(monkeypatch):
    """The deferred aggregate node should see all branch writes before it runs."""
    from darkfactory.stages import verify as verify_mod
    from darkfactory.stages.verify import verify_subgraph

    calls: list[str] = []

    def _stub(name):
        def node(state, runtime=None):  # noqa: ARG001
            calls.append(name)
            return {}
        return node

    def _tests(state, runtime=None):  # noqa: ARG001
        calls.append("tests")
        return {
            "test_results": [{
                "runner": "maven",
                "returncode": 0,
                "passed": 3,
                "failed": 0,
                "errors": [],
                "duration_s": 0.1,
            }]
        }

    def _linters(state, runtime=None):  # noqa: ARG001
        calls.append("linters")
        return {
            "findings": [{
                "tool": "checkstyle",
                "severity": "warn",
                "file": "src/main/java/Foo.java",
                "line": 12,
                "rule": "LineLength",
                "message": "line too long",
            }]
        }

    def _compile(state, runtime=None):  # noqa: ARG001
        calls.append("compile")
        return {
            "findings": [{
                "tool": "javac",
                "severity": "info",
                "file": "src/main/java/Foo.java",
                "line": 7,
                "rule": "compile",
                "message": "clean compile",
            }]
        }

    seen: dict[str, int] = {}
    real_aggregate = verify_mod.aggregate

    def _aggregate(state, runtime=None):
        calls.append("aggregate")
        seen["test_results"] = len(state.get("test_results", []))
        seen["findings"] = len(state.get("findings", []))
        return real_aggregate(state, runtime)

    monkeypatch.setattr(verify_mod, "run_tests_node", _tests)
    monkeypatch.setattr(verify_mod, "run_linters_node", _linters)
    monkeypatch.setattr(verify_mod, "run_compile_node", _compile)
    monkeypatch.setattr(verify_mod, "run_happy_path_node", _stub("happy"))
    monkeypatch.setattr(verify_mod, "aggregate", _aggregate)

    sg = verify_subgraph()
    out = sg.invoke({"user_request": "x"})

    assert set(calls[:-1]) == {"compile", "happy", "linters", "tests"}
    assert calls[-1] == "aggregate"
    assert seen == {"test_results": 1, "findings": 2}
    assert out["verify_summary"] == {
        "passed": True,
        "failed_tests": 0,
        "hard_findings": 0,
    }
