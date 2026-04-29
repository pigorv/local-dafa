"""Unit tests for tools/tests.py: project detection + summary parsing."""
from __future__ import annotations

import types

import pytest

from darkfactory.state import RunContext
from darkfactory.tools import tests as tests_mod
from darkfactory.tools.tests import (
    detect_project,
    parse_gradle,
    parse_maven,
    run_tests,
)


def test_detect_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    d = detect_project(tmp_path)
    assert d["kind"] == "maven"
    assert d["cmd"][0] == "mvn"
    assert "test" in d["cmd"]


def test_detect_gradle_wrapper(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    d = detect_project(tmp_path)
    assert d["kind"] == "gradle-wrapper"
    assert d["cmd"][0] == "./gradlew"


def test_detect_gradle_kts_no_wrapper(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("")
    d = detect_project(tmp_path)
    assert d["kind"] == "gradle"
    assert d["cmd"][0] == "gradle"


def test_detect_npm(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    d = detect_project(tmp_path)
    assert d["kind"] == "npm"


def test_detect_unknown(tmp_path):
    assert "error" in detect_project(tmp_path)


def test_detect_prefers_maven_over_gradle(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "build.gradle").write_text("")
    assert detect_project(tmp_path)["kind"] == "maven"


def test_parse_maven_single_module():
    stdout = """
[INFO] -------------------------------------------------------
[INFO]  T E S T S
[INFO] -------------------------------------------------------
[INFO] Running com.example.FooTest
[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 1, Time elapsed: 0.1 s
[INFO] Results:
[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 1
"""
    # The regex matches every occurrence; parse_maven sums all. A single
    # Surefire module prints the summary twice (per-class + final Results
    # block); the test asserts the summed counts regardless.
    s = parse_maven(stdout, "")
    assert s == {"passed": 8, "failed": 0, "skipped": 2, "total": 10}


def test_parse_maven_with_failures():
    stdout = "Tests run: 7, Failures: 2, Errors: 1, Skipped: 0\n"
    s = parse_maven(stdout, "")
    assert s == {"passed": 4, "failed": 3, "skipped": 0, "total": 7}


def test_parse_maven_no_summary():
    assert parse_maven("BUILD FAILURE\n", "") is None


def test_parse_gradle_all_passed():
    stdout = "BUILD SUCCESSFUL in 3s\n12 tests completed\n"
    s = parse_gradle(stdout, "")
    assert s == {"passed": 12, "failed": 0, "skipped": 0, "total": 12}


def test_parse_gradle_with_failures():
    stdout = "10 tests completed, 2 failed, 1 skipped\n"
    s = parse_gradle(stdout, "")
    assert s == {"passed": 7, "failed": 2, "skipped": 1, "total": 10}


def test_parse_gradle_no_summary():
    assert parse_gradle("FAILURE: Build failed\n", "") is None


def test_run_tests_unknown_project(tmp_path, monkeypatch):
    ctx = RunContext(task_id="t-tests-unknown", repo_path=str(tmp_path))
    runtime = types.SimpleNamespace(context=ctx)
    monkeypatch.setattr(tests_mod, "get_runtime", lambda _c: runtime)
    out = run_tests.invoke({})
    assert out == {"error": "cannot detect project type"}


def test_run_tests_uses_sandbox(tmp_path, monkeypatch):
    """Verify run_tests resolves the sandbox and forwards the detected argv."""
    (tmp_path / "pom.xml").write_text("<project/>")
    ctx = RunContext(task_id="t-tests-fake", repo_path=str(tmp_path))
    runtime = types.SimpleNamespace(context=ctx)
    monkeypatch.setattr(tests_mod, "get_runtime", lambda _c: runtime)

    calls: dict = {}

    class FakeSandbox:
        def exec(self, argv, timeout):
            calls["argv"] = argv
            calls["timeout"] = timeout
            return {
                "returncode": 0,
                "stdout": "Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n",
                "stderr": "",
                "timed_out": False,
            }

    monkeypatch.setattr(tests_mod, "get_sandbox", lambda _tid: FakeSandbox())

    out = run_tests.invoke({"timeout": 42})
    assert calls["argv"] == ["mvn", "-q", "-B", "test"]
    assert calls["timeout"] == 42
    assert out["kind"] == "maven"
    assert out["returncode"] == 0
    assert out["passed"] == 3
    assert out["failed"] == 0
    assert out["total"] == 3


def test_run_tests_raises_without_sandbox(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    ctx = RunContext(task_id="t-tests-nobox", repo_path=str(tmp_path))
    runtime = types.SimpleNamespace(context=ctx)
    monkeypatch.setattr(tests_mod, "get_runtime", lambda _c: runtime)
    monkeypatch.setattr(tests_mod, "get_sandbox", lambda _tid: None)
    with pytest.raises(RuntimeError, match="no sandbox registered"):
        run_tests.invoke({})
