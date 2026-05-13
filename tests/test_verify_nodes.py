"""Unit tests for stages/verify.py (plan-driven topology).

The verifier discovers commands via the verify_planner role, caches the
plan on PipelineState.verification_plan, and consumes structured report
files via tools/reports.py. These tests monkeypatch the planner and the
sandbox; the readers in tools/reports.py have their own coverage in
tests/test_reports.py.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from darkfactory.agents._sdk_common import ParseError
from darkfactory.stages import verify as verify_mod
from darkfactory.stages.verify import (
    aggregate,
    ensure_plan_node,
    run_plan_node,
    run_semantic_coverage_node,
    verify_subgraph,
)


def _runtime(tmp_path: Path, task_id: str = "t-verify") -> types.SimpleNamespace:
    ctx = types.SimpleNamespace(
        repo_path=str(tmp_path),
        task_id=task_id,
    )
    return types.SimpleNamespace(context=ctx)


class FakeSandbox:
    """Records calls and returns queued replies keyed by argv prefix."""

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


SUREFIRE_PASS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.OrderControllerTest" tests="9" failures="0" errors="0" skipped="0" time="0.5">
  <testcase classname="com.example.OrderControllerTest" name="listReturnsFirstPage"/>
  <testcase classname="com.example.OrderControllerTest" name="listReturnsSecondPage"/>
  <testcase classname="com.example.OrderControllerTest" name="cursorPaginationReturnsFirstPage"/>
  <testcase classname="com.example.OrderControllerTest" name="cursorPaginationContinuesWithCursor"/>
  <testcase classname="com.example.OrderControllerTest" name="cursorPaginationDetectsEndOfResults"/>
  <testcase classname="com.example.OrderControllerTest" name="cursorPaginationReturnsEmptyWhenNoMoreResults"/>
  <testcase classname="com.example.OrderControllerTest" name="cursorPaginationHandlesCustomLimit"/>
  <testcase classname="com.example.OrderControllerTest" name="cursorPaginationHandlesInvalidCursor"/>
  <testcase classname="com.example.OrderControllerTest" name="cursorPaginationMultiPageScenario"/>
</testsuite>
"""

CHECKSTYLE_VIOLATION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<checkstyle version="10.3.4">
  <file name="src/main/java/com/example/Order.java">
    <error line="14" severity="error" message="'50' is a magic number."
           source="com.puppycrawl.tools.checkstyle.checks.coding.MagicNumberCheck"/>
  </file>
</checkstyle>
"""


# --- ensure_plan_node ---


def test_ensure_plan_node_caches_existing_plan(monkeypatch):
    """A populated cache short-circuits the planner call."""

    async def _unexpected(_state):
        raise AssertionError("verify_planner should not run when a plan is cached")

    monkeypatch.setattr(verify_mod, "run_verify_planner", _unexpected)
    state = {
        "verification_plan": {
            "test": {"name": "test", "argv": ["pytest"]},
        },
        "verification_plan_rev": 1,
    }

    out = asyncio.run(ensure_plan_node(state))

    assert out == {}


def test_ensure_plan_node_persists_planner_output(monkeypatch):
    async def _planner(_state):
        return {
            "test": {
                "name": "test",
                "argv": ["mvn", "-B", "test"],
                "report_paths": ["target/surefire-reports/TEST-*.xml"],
                "report_kind": "junit-xml",
            }
        }

    monkeypatch.setattr(verify_mod, "run_verify_planner", _planner)

    out = asyncio.run(ensure_plan_node({}))

    assert out["verification_plan"]["test"]["argv"] == ["mvn", "-B", "test"]
    assert out["verification_plan_rev"] == 1


def test_ensure_plan_node_flags_empty_plan_as_discovery_failure(monkeypatch):
    async def _planner(_state):
        return {"notes": "no test command available"}

    monkeypatch.setattr(verify_mod, "run_verify_planner", _planner)

    out = asyncio.run(ensure_plan_node({}))

    assert out["verification_plan"] == {"notes": "no test command available"}
    findings = out["findings"]
    assert len(findings) == 1
    assert findings[0]["tool"] == "verify_planner"
    assert findings[0]["rule"] == "empty_plan"
    assert findings[0]["severity"] == "error"
    assert "no test command available" in findings[0]["message"]


def test_ensure_plan_node_handles_parse_error(monkeypatch):
    async def _planner(_state):
        raise ParseError("verify_planner emitted no structured output")

    monkeypatch.setattr(verify_mod, "run_verify_planner", _planner)

    out = asyncio.run(ensure_plan_node({}))

    assert "verification_plan" not in out
    findings = out["findings"]
    assert len(findings) == 1
    assert findings[0]["rule"] == "discovery_failed"


# --- run_plan_node ---


def test_run_plan_node_test_step_with_junit_xml(tmp_path, monkeypatch):
    report_path = tmp_path / "target/surefire-reports/TEST-OrderControllerTest.xml"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(SUREFIRE_PASS_XML, encoding="utf-8")

    sandbox = FakeSandbox()
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sandbox)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    state = {
        "verification_plan": {
            "test": {
                "name": "test",
                "argv": ["mvn", "-B", "test"],
                "report_paths": ["target/surefire-reports/TEST-*.xml"],
                "report_kind": "junit-xml",
            }
        }
    }

    out = asyncio.run(run_plan_node(state, _runtime(tmp_path)))

    assert sandbox.calls == [["mvn", "-B", "test"]]
    tr = out["test_results"][0]
    assert tr["runner"] == "test"
    assert tr["passed"] == 9
    assert tr["failed"] == 0
    assert tr["returncode"] == 0
    assert len(tr["executed_tests"]) == 9
    assert tr["errors"] == []


def test_run_plan_node_test_step_without_report_falls_back_to_exit_code(tmp_path, monkeypatch):
    sandbox = FakeSandbox(default={
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sandbox)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    state = {
        "verification_plan": {
            "test": {
                "name": "test",
                "argv": ["pytest"],
            }
        }
    }

    out = asyncio.run(run_plan_node(state, _runtime(tmp_path)))

    tr = out["test_results"][0]
    assert tr["passed"] == 0
    assert tr["failed"] == 0
    assert tr["returncode"] == 0
    assert tr["errors"] == []


def test_run_plan_node_test_step_failing_exit_code_records_errors(tmp_path, monkeypatch):
    sandbox = FakeSandbox(default={
        "returncode": 1,
        "stdout": "",
        "stderr": "AssertionError: nope\n",
        "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sandbox)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    state = {
        "verification_plan": {
            "test": {"name": "test", "argv": ["pytest"]}
        }
    }

    out = asyncio.run(run_plan_node(state, _runtime(tmp_path)))

    tr = out["test_results"][0]
    assert tr["returncode"] == 1
    assert "AssertionError" in "\n".join(tr["errors"])


def test_run_plan_node_lint_step_with_checkstyle_xml(tmp_path, monkeypatch):
    report_path = tmp_path / "target/checkstyle-result.xml"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(CHECKSTYLE_VIOLATION_XML, encoding="utf-8")

    sandbox = FakeSandbox(default={
        "returncode": 1,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sandbox)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    state = {
        "verification_plan": {
            "lint": [
                {
                    "name": "checkstyle",
                    "argv": ["mvn", "-B", "checkstyle:checkstyle"],
                    "report_paths": ["target/checkstyle-result.xml"],
                    "report_kind": "checkstyle-xml",
                }
            ]
        }
    }

    out = asyncio.run(run_plan_node(state, _runtime(tmp_path)))

    findings = out["findings"]
    assert len(findings) == 1
    assert findings[0]["tool"] == "checkstyle"
    assert findings[0]["severity"] == "error"
    assert findings[0]["rule"] == "MagicNumberCheck"
    assert findings[0]["line"] == 14


def test_run_plan_node_compile_step_exit_code_fallback(tmp_path, monkeypatch):
    sandbox = FakeSandbox(default={
        "returncode": 2,
        "stdout": "",
        "stderr": "Boom\nERROR: type error in Foo.java:14\n",
        "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sandbox)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    state = {
        "verification_plan": {
            "compile": {"name": "compile", "argv": ["mvn", "-B", "compile"]}
        }
    }

    out = asyncio.run(run_plan_node(state, _runtime(tmp_path)))

    findings = out["findings"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "error"
    assert findings[0]["rule"] == "compile"
    assert "type error in Foo.java" in findings[0]["message"]


def test_run_plan_node_advisory_lint_emits_warn_finding(tmp_path, monkeypatch):
    sandbox = FakeSandbox(default={
        "returncode": 1,
        "stdout": "",
        "stderr": "style nag\n",
        "timed_out": False,
    })
    monkeypatch.setattr(verify_mod, "get_sandbox", lambda _t: sandbox)
    monkeypatch.setattr(verify_mod, "register_sandbox", lambda *a, **k: None)

    state = {
        "verification_plan": {
            "lint": [
                {
                    "name": "spotless",
                    "argv": ["mvn", "-B", "spotless:check"],
                    "required": False,
                }
            ]
        }
    }

    out = asyncio.run(run_plan_node(state, _runtime(tmp_path)))

    findings = out["findings"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "warn"


def test_run_plan_node_empty_plan_returns_no_writes(tmp_path):
    out = asyncio.run(run_plan_node({"verification_plan": {}}, _runtime(tmp_path)))
    assert out == {}


def test_run_plan_node_no_runtime_returns_no_writes():
    out = asyncio.run(run_plan_node({"verification_plan": {"test": {"argv": ["x"]}}}, None))
    assert out == {}


# --- aggregate (preserved from the old subgraph) ---


def test_aggregate_fails_on_blocking_tester_finding():
    state = {
        "test_results": [
            {
                "runner": "test",
                "returncode": 0,
                "passed": 1,
                "failed": 0,
                "errors": [],
                "duration_s": 0.1,
            }
        ],
        "findings": [],
        "tester_findings": [
            {
                "kind": "behavior_mismatch",
                "wp_id": "WP-1",
                "detail": "Implementation returns offset pagination.",
            }
        ],
        "verify_retries": 0,
    }

    out = aggregate(state)

    assert out["verify_summary"]["passed"] is False
    assert out["verify_summary"]["blocking_failures"] == 1
    assert out["verify_retries"] == 1


# --- run_semantic_coverage_node (preserved) ---


def test_run_semantic_coverage_node_skips_without_predicates(monkeypatch):
    async def _unexpected(_state):
        raise AssertionError("semantic verifier should not run without predicates")

    monkeypatch.setattr(verify_mod, "run_verifier_semantic", _unexpected)

    out = asyncio.run(
        run_semantic_coverage_node(
            {"verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0}}
        )
    )

    assert out == {}


def test_run_semantic_coverage_node_merges_predicate_coverage(monkeypatch):
    async def _fake_semantic(_state):
        return {
            "predicate_coverage": [
                {
                    "wp_id": "WP-1",
                    "predicate": "GET /customers/{unknown_id} returns 404",
                    "status": "covered",
                    "evidence": "CustomerControllerTest.missingCustomerReturns404",
                }
            ]
        }

    monkeypatch.setattr(verify_mod, "run_verifier_semantic", _fake_semantic)

    state = {
        "implementation_brief": {
            "work_packages": [
                {
                    "id": "WP-1",
                    "verification": ["GET /customers/{unknown_id} returns 404"],
                }
            ]
        },
        "test_results": [
            {
                "runner": "test",
                "returncode": 0,
                "passed": 1,
                "failed": 0,
                "errors": [],
                "duration_s": 0.1,
            }
        ],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
    }

    out = asyncio.run(run_semantic_coverage_node(state))

    assert out["verify_summary"]["passed"] is True
    assert out["verify_summary"]["uncovered_predicates"] == 0


def test_run_semantic_coverage_node_fails_on_uncovered_predicate(monkeypatch):
    async def _fake_semantic(_state):
        return {
            "predicate_coverage": [
                {
                    "wp_id": "WP-1",
                    "predicate": "GET /customers/{unknown_id} returns 404",
                    "status": "uncovered",
                    "evidence": "",
                }
            ]
        }

    monkeypatch.setattr(verify_mod, "run_verifier_semantic", _fake_semantic)

    state = {
        "implementation_brief": {
            "work_packages": [
                {
                    "id": "WP-1",
                    "verification": ["GET /customers/{unknown_id} returns 404"],
                }
            ]
        },
        "test_results": [
            {
                "runner": "test",
                "returncode": 0,
                "passed": 1,
                "failed": 0,
                "errors": [],
                "duration_s": 0.1,
            }
        ],
        "findings": [],
        "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0},
        "verify_retries": 0,
    }

    out = asyncio.run(run_semantic_coverage_node(state))

    assert out["verify_summary"]["passed"] is False
    assert out["verify_summary"]["uncovered_predicates"] == 1
    assert out["verify_retries"] == 1


# --- verify_subgraph topology ---


def test_verify_subgraph_runs_ensure_plan_then_run_plan_then_aggregate(monkeypatch):
    """ensure_plan → run_plan → aggregate → run_semantic_coverage."""
    calls: list[str] = []

    async def _ensure(state, runtime=None):  # noqa: ARG001
        calls.append("ensure_plan")
        return {
            "verification_plan": {"test": {"name": "test", "argv": ["x"]}},
            "verification_plan_rev": 1,
        }

    async def _run_plan(state, runtime=None):  # noqa: ARG001
        calls.append("run_plan")
        assert state["verification_plan"]["test"]["argv"] == ["x"]
        return {
            "test_results": [
                {
                    "runner": "test",
                    "returncode": 0,
                    "passed": 3,
                    "failed": 0,
                    "errors": [],
                    "duration_s": 0.1,
                }
            ]
        }

    def _aggregate(state, runtime=None):  # noqa: ARG001
        calls.append("aggregate")
        return {
            "verify_summary": {"passed": True, "failed_tests": 0, "hard_findings": 0}
        }

    async def _semantic(state, runtime=None):  # noqa: ARG001
        calls.append("semantic")
        return {}

    monkeypatch.setattr(verify_mod, "ensure_plan_node", _ensure)
    monkeypatch.setattr(verify_mod, "run_plan_node", _run_plan)
    monkeypatch.setattr(verify_mod, "aggregate", _aggregate)
    monkeypatch.setattr(verify_mod, "run_semantic_coverage_node", _semantic)

    sg = verify_subgraph()
    out = asyncio.run(sg.ainvoke({"user_request": "x"}))

    assert calls == ["ensure_plan", "run_plan", "aggregate", "semantic"]
    assert out["verify_summary"]["passed"] is True
