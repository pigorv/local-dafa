"""Verify-stage nodes: thin wrappers that run tests, linters, and compile.

Each node is a plain graph node (not a `create_agent`). They reuse the
helpers in `tools/tests.py` and `tools/linters.py` directly, sidestepping
the `@tool` runtime so they can be invoked deterministically as part of
the parallel `verify_fanout` (Phase 3 task 3.3) and aggregated by a
`deferred=True` collector (task 3.4).

State deltas:
- `run_tests_node`     → appends one TestResult to `test_results`.
- `run_linters_node`   → appends Findings (Checkstyle + Spotless) to `findings`.
- `run_compile_node`   → appends Findings (javac errors) to `findings`.
- `run_happy_path_node`→ optional smoke test; currently a no-op stub.
"""
from __future__ import annotations

import time
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from darkfactory.state import (
    Finding,
    PipelineState,
    RunContext,
    TestResult,
    VerifySummary,
)
from darkfactory.tools.linters import (
    detect_build,
    parse_checkstyle,
    parse_compile,
    parse_spotless,
)
from darkfactory.tools.sandbox import RepoSandbox
from darkfactory.tools.shell import get_sandbox, register_sandbox
from darkfactory.tools.tests import detect_project, parse_gradle, parse_maven


def _ensure_sandbox(ctx: Any):
    sb = get_sandbox(ctx.task_id)
    if sb is None:
        register_sandbox(ctx.task_id, RepoSandbox(repo_path=ctx.repo_path))
        sb = get_sandbox(ctx.task_id)
    return sb


def run_tests_node(state: PipelineState, runtime=None) -> dict:
    """Run the project's test suite once and append a TestResult."""
    ctx = getattr(runtime, "context", None) if runtime is not None else None
    if ctx is None:
        return {}

    detected = detect_project(ctx.repo_path)
    if "error" in detected:
        return {
            "test_results": [
                TestResult(
                    runner="unknown",
                    returncode=-1,
                    passed=0,
                    failed=0,
                    errors=[detected["error"]],
                    duration_s=0.0,
                )
            ]
        }

    sb = _ensure_sandbox(ctx)
    if sb is None:
        return {}

    started = time.monotonic()
    result = sb.exec(detected["cmd"], timeout=600)
    duration = time.monotonic() - started

    out = result.get("stdout", "")
    err = result.get("stderr", "")
    kind = detected["kind"]
    summary: dict[str, int] | None = None
    if kind == "maven":
        summary = parse_maven(out, err)
    elif kind in ("gradle", "gradle-wrapper"):
        summary = parse_gradle(out, err)

    rc = int(result.get("returncode", -1))
    passed = (summary or {}).get("passed", 0)
    failed = (summary or {}).get("failed", 0)
    errors: list[str] = []
    if result.get("timed_out"):
        errors.append("timed_out")
    if rc != 0 and summary is None:
        # No parseable summary — surface a tail of stderr so the aggregator/
        # spec-adjustment agent has something to chew on.
        tail = (err or out).strip().splitlines()[-20:]
        if tail:
            errors.append("\n".join(tail))

    return {
        "test_results": [
            TestResult(
                runner=kind,
                returncode=rc,
                passed=passed,
                failed=failed,
                errors=errors,
                duration_s=duration,
            )
        ]
    }


def _run_linter(sb, argv: list[str], parser) -> tuple[int, list[Finding]]:
    result = sb.exec(argv, timeout=600)
    findings = parser(result.get("stdout", ""), result.get("stderr", ""))
    return int(result.get("returncode", -1)), findings


def run_linters_node(state: PipelineState, runtime=None) -> dict:
    """Run Checkstyle + Spotless and append parsed Findings."""
    ctx = getattr(runtime, "context", None) if runtime is not None else None
    if ctx is None:
        return {}

    detected = detect_build(ctx.repo_path)
    if "error" in detected:
        return {}

    sb = _ensure_sandbox(ctx)
    if sb is None:
        return {}

    findings: list[Finding] = []
    _, cs = _run_linter(sb, detected["checkstyle"], parse_checkstyle)
    findings.extend(cs)
    _, sp = _run_linter(sb, detected["spotless"], parse_spotless)
    findings.extend(sp)
    return {"findings": findings}


def run_compile_node(state: PipelineState, runtime=None) -> dict:
    """Compile main + test sources; emit javac errors as Findings.

    Compile errors are the Java analogue of mypy/tsc failures, so they
    show up in the same `findings` channel as linter output.
    """
    ctx = getattr(runtime, "context", None) if runtime is not None else None
    if ctx is None:
        return {}

    detected = detect_build(ctx.repo_path)
    if "error" in detected:
        return {}

    sb = _ensure_sandbox(ctx)
    if sb is None:
        return {}

    rc, findings = _run_linter(sb, detected["compile"], parse_compile)
    if rc != 0 and not findings:
        # Compile failed but the parser found nothing recognisable. Emit a
        # synthetic finding so the aggregator still sees a failure signal.
        findings.append(
            Finding(
                tool="javac",
                severity="error",
                file="(unknown)",
                line=0,
                rule="compile",
                message=f"compile returned non-zero ({rc}) with no parseable diagnostics",
            )
        )
    return {"findings": findings}


def run_happy_path_node(state: PipelineState, runtime=None) -> dict:
    """Optional smoke test: boot the app, hit a health endpoint, kill it.

    Stubbed for now (per PLAN.md §6 Phase 3 task 2: skip if over budget).
    Wired so the verify fan-out has a fourth slot ready when we implement
    it; currently a no-op that records nothing.
    """
    return {}


VERIFY_TARGETS = ("run_tests", "run_linters", "run_compile", "run_happy_path")


def verify_fanout(state: PipelineState) -> list[Send]:
    """Conditional-edge router: emit one `Send` per verify node.

    Returning a list of `Send` from a conditional edge is the LangGraph
    pattern that makes the four verify nodes execute in the same superstep
    — Studio renders them pulsing simultaneously (R11). The deferred
    aggregator that consumes their writes lands in task 3.4.
    """
    return [Send(target, state) for target in VERIFY_TARGETS]


def _aggregate_verify_summary(state: PipelineState) -> VerifySummary:
    """Reduce the current verify snapshot to a pass/fail verdict."""
    failed_tests = sum(
        1
        for result in (state.get("test_results") or [])
        if result.get("failed", 0) > 0
        or result.get("returncode", 0) != 0
        or bool(result.get("errors"))
    )
    hard_findings = sum(
        1
        for finding in (state.get("findings") or [])
        if finding.get("severity") in ("error", "critical")
    )
    return VerifySummary(
        passed=(failed_tests == 0 and hard_findings == 0),
        failed_tests=failed_tests,
        hard_findings=hard_findings,
    )


def aggregate(state: PipelineState, runtime=None) -> dict:
    """Join point for the four parallel verify branches.

    Collapses the fan-out outputs into one structured verdict and bumps
    `verify_retries` whenever the verdict is *not* a pass. The retry-cap
    routing (task 3.6) lives in the top-level graph and reads this counter
    to decide between another Build pass or a hard stop.
    """
    summary = _aggregate_verify_summary(state)
    delta: dict[str, Any] = {"verify_summary": summary}
    if not summary["passed"]:
        delta["verify_retries"] = (state.get("verify_retries") or 0) + 1
    return delta


def verify_subgraph() -> Any:
    """Verify subgraph: START -[fanout]-> {tests, linters, compile, happy} -> aggregate -> END.

    Compiled with `RunContext` so the inner nodes see `runtime.context`
    (sandbox, repo_path) when invoked from the top-level graph.
    """
    g = StateGraph(PipelineState, context_schema=RunContext)
    g.add_node("run_tests", run_tests_node)
    g.add_node("run_linters", run_linters_node)
    g.add_node("run_compile", run_compile_node)
    g.add_node("run_happy_path", run_happy_path_node)
    g.add_node("aggregate", aggregate, defer=True)

    g.add_conditional_edges(START, verify_fanout, list(VERIFY_TARGETS))
    for target in VERIFY_TARGETS:
        g.add_edge(target, "aggregate")
    g.add_edge("aggregate", END)
    return g.compile()
