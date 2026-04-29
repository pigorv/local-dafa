"""Unit tests for tools/linters.py: build detection + parser output.

The acceptance criterion for tracker 3.1 is: on a repo with a known lint
violation, run_linters returns the violation as a Finding. We exercise it
two ways:

1. Parser unit tests on realistic Maven/Gradle output → Finding[].
2. run_linters with a fake sandbox returning a known-violation stdout →
   findings list contains the violation.
"""
from __future__ import annotations

import types

import pytest

from darkfactory.state import RunContext
from darkfactory.tools import linters as linters_mod
from darkfactory.tools.linters import (
    detect_build,
    parse_checkstyle,
    parse_compile,
    parse_spotless,
    run_linters,
)


# --- detect_build ---

def test_detect_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    d = detect_build(tmp_path)
    assert d["kind"] == "maven"
    assert d["checkstyle"][:2] == ["mvn", "-q"]
    assert "checkstyle:check" in d["checkstyle"]
    assert "spotless:check" in d["spotless"]
    assert "compile" in d["compile"]


def test_detect_gradle_wrapper(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    d = detect_build(tmp_path)
    assert d["kind"] == "gradle-wrapper"
    assert d["checkstyle"][0] == "./gradlew"
    assert "checkstyleMain" in d["checkstyle"]


def test_detect_unknown(tmp_path):
    assert "error" in detect_build(tmp_path)


# --- parsers ---

def test_parse_checkstyle_maven_known_violation():
    stdout = (
        "[INFO] Starting audit...\n"
        "[ERROR] src/main/java/com/example/UserService.java:[42,5] "
        "(whitespace) FileTabCharacter: Line contains a tab character.\n"
        "[WARN] src/main/java/com/example/UserService.java:[7] "
        "(imports) UnusedImports: Unused import - java.util.List.\n"
        "[INFO] Audit done.\n"
    )
    fs = parse_checkstyle(stdout, "")
    assert len(fs) == 2
    f0 = fs[0]
    assert f0["tool"] == "checkstyle"
    assert f0["severity"] == "error"
    assert f0["file"] == "src/main/java/com/example/UserService.java"
    assert f0["line"] == 42
    assert f0["rule"] == "FileTabCharacter"
    assert "tab" in f0["message"].lower()
    assert fs[1]["severity"] == "warn"
    assert fs[1]["rule"] == "UnusedImports"


def test_parse_checkstyle_gradle_known_violation():
    stdout = (
        "src/main/java/com/example/Foo.java:12:5: warning: "
        "Line is longer than 100 characters [LineLength]\n"
    )
    fs = parse_checkstyle(stdout, "")
    assert len(fs) == 1
    assert fs[0]["tool"] == "checkstyle"
    assert fs[0]["rule"] == "LineLength"
    assert fs[0]["line"] == 12
    assert fs[0]["severity"] == "warn"


def test_parse_compile_maven_known_error():
    stderr = (
        "[ERROR] /workspace/src/main/java/com/example/Foo.java:[15,9] "
        "cannot find symbol\n"
        "[ERROR]   symbol:   variable bar\n"
    )
    fs = parse_compile("", stderr)
    assert len(fs) == 1
    assert fs[0]["tool"] == "javac"
    assert fs[0]["severity"] == "error"
    assert fs[0]["line"] == 15
    assert "cannot find symbol" in fs[0]["message"]


def test_parse_compile_gradle_known_error():
    stderr = (
        "/workspace/src/main/java/com/example/Foo.java:15: error: "
        "cannot find symbol\n"
    )
    fs = parse_compile("", stderr)
    assert len(fs) == 1
    assert fs[0]["tool"] == "javac"
    assert fs[0]["severity"] == "error"
    assert fs[0]["line"] == 15


def test_parse_spotless_known_violation():
    stdout = (
        "[ERROR] The following files had format violations:\n"
        "[ERROR]     src/main/java/com/example/Foo.java\n"
        "[ERROR]     src/main/java/com/example/Bar.java\n"
        "\n"
        "[ERROR] Run spotless:apply to fix.\n"
    )
    fs = parse_spotless(stdout, "")
    assert {f["file"] for f in fs} == {
        "src/main/java/com/example/Foo.java",
        "src/main/java/com/example/Bar.java",
    }
    assert all(f["tool"] == "spotless" for f in fs)
    assert all(f["rule"] == "format" for f in fs)


def test_parse_clean_output_yields_no_findings():
    assert parse_checkstyle("BUILD SUCCESS\n", "") == []
    assert parse_compile("BUILD SUCCESS\n", "") == []
    assert parse_spotless("BUILD SUCCESS\n", "") == []


# --- run_linters ---

def test_run_linters_unknown_project(tmp_path, monkeypatch):
    ctx = RunContext(task_id="t-lint-unknown", repo_path=str(tmp_path))
    runtime = types.SimpleNamespace(context=ctx)
    monkeypatch.setattr(linters_mod, "get_runtime", lambda _c: runtime)
    out = run_linters.invoke({})
    assert out == {"error": "cannot detect project type"}


def test_run_linters_returns_finding_on_known_violation(tmp_path, monkeypatch):
    """Acceptance: known violation → Finding[] surfaced from run_linters."""
    (tmp_path / "pom.xml").write_text("<project/>")
    ctx = RunContext(task_id="t-lint-known", repo_path=str(tmp_path))
    runtime = types.SimpleNamespace(context=ctx)
    monkeypatch.setattr(linters_mod, "get_runtime", lambda _c: runtime)

    cs_stdout = (
        "[ERROR] src/main/java/com/example/UserService.java:[42,5] "
        "(whitespace) FileTabCharacter: Line contains a tab character.\n"
    )
    spotless_stdout = (
        "[ERROR] The following files had format violations:\n"
        "[ERROR]     src/main/java/com/example/UserService.java\n"
    )
    compile_stderr = (
        "[ERROR] /workspace/src/main/java/com/example/UserService.java:[15,9] "
        "cannot find symbol\n"
    )

    class FakeSandbox:
        def __init__(self):
            self.calls: list[list[str]] = []

        def exec(self, argv, timeout):
            self.calls.append(argv)
            if "checkstyle:check" in argv:
                return {"returncode": 1, "stdout": cs_stdout, "stderr": "",
                        "timed_out": False}
            if "spotless:check" in argv:
                return {"returncode": 1, "stdout": spotless_stdout, "stderr": "",
                        "timed_out": False}
            if "compile" in argv:
                return {"returncode": 1, "stdout": "", "stderr": compile_stderr,
                        "timed_out": False}
            return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}

    fake = FakeSandbox()
    monkeypatch.setattr(linters_mod, "get_sandbox", lambda _tid: fake)

    out = run_linters.invoke({"timeout": 30})
    assert out["kind"] == "maven"
    findings = out["findings"]
    by_tool = {f["tool"] for f in findings}
    assert by_tool == {"checkstyle", "spotless", "javac"}

    cs = next(f for f in findings if f["tool"] == "checkstyle")
    assert cs["rule"] == "FileTabCharacter"
    assert cs["severity"] == "error"
    assert cs["line"] == 42
    assert cs["file"].endswith("UserService.java")

    sp = next(f for f in findings if f["tool"] == "spotless")
    assert sp["file"].endswith("UserService.java")

    jc = next(f for f in findings if f["tool"] == "javac")
    assert jc["severity"] == "error"
    assert jc["line"] == 15

    # Each checker invoked exactly once.
    assert len(fake.calls) == 3


def test_run_linters_raises_without_sandbox(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("<project/>")
    ctx = RunContext(task_id="t-lint-nobox", repo_path=str(tmp_path))
    runtime = types.SimpleNamespace(context=ctx)
    monkeypatch.setattr(linters_mod, "get_runtime", lambda _c: runtime)
    monkeypatch.setattr(linters_mod, "get_sandbox", lambda _tid: None)
    with pytest.raises(RuntimeError, match="no sandbox registered"):
        run_linters.invoke({})
