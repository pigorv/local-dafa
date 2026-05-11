"""Verify-stage nodes: thin wrappers that run tests, linters, and compile.

Each node is a plain graph node (not a `create_agent`). They reuse the
helpers in `tools/tests.py` and `tools/linters.py` directly, sidestepping
the `@tool` runtime so they can be invoked deterministically as part of
the parallel `verify_fanout` (Phase 3 task 3.3) and aggregated by a
`deferred=True` collector (task 3.4).

State deltas:
- `run_tests_node`             → appends one TestResult to `test_results`.
- `run_linters_node`           → appends Findings (Checkstyle + Spotless) to `findings`.
- `run_compile_node`           → appends Findings (javac errors) to `findings`.
- `run_happy_path_node`        → optional smoke test; currently a no-op stub.
- `run_semantic_coverage_node` → adds predicate coverage and final pass/fail to
  `verify_summary`.
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
from darkfactory.agents.verifier_semantic import run_verifier_semantic
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


def _text_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_text_present(item) for item in value)
    root = getattr(value, "root", None)
    if root is not None:
        return _text_present(root)
    return bool(value)


def _read_field(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _predicate_text(value: Any) -> str:
    root = getattr(value, "root", None)
    if root is not None:
        return str(root).strip()
    if isinstance(value, dict):
        for key in ("root", "predicate", "value"):
            if key in value:
                return str(value[key]).strip()
    return str(value).strip()


def _predicate_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [
            text
            for text in (_predicate_text(item) for item in value)
            if text
        ]
    text = _predicate_text(value)
    return [text] if text else []


def _expected_predicates(state: PipelineState) -> list[tuple[str, str]]:
    expected: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(wp_id: str, predicates: Any) -> None:
        for predicate in _predicate_values(predicates):
            key = (wp_id, predicate)
            if key in seen:
                continue
            seen.add(key)
            expected.append(key)

    brief = state.get("implementation_brief")
    for wp in _read_field(brief, "work_packages", []) or []:
        wp_id = str(
            _read_field(wp, "id", None)
            or _read_field(wp, "story_id", "")
        )
        add(wp_id, _read_field(wp, "verification", []))

    for spec_slice in state.get("spec") or []:
        wp_id = str(
            _read_field(spec_slice, "id", None)
            or _read_field(spec_slice, "story_id", "")
        )
        add(wp_id, _read_field(spec_slice, "verification", None))

    return expected


def _has_verification_predicates(state: PipelineState) -> bool:
    if _expected_predicates(state):
        return True
    for entry in state.get("coverage_entries") or []:
        if _text_present(_read_field(entry, "predicate", None)):
            return True

    return False


def _coverage_to_dict(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    return dict(item)


async def run_semantic_coverage_node(state: PipelineState, runtime=None) -> dict:
    """Ask the Semantic Verifier to map WP predicates to evidence.

    The first aggregate node owns mechanical pass/fail. This node attaches the
    semantic coverage map and then recomputes the final verifier verdict across
    mechanical checks, predicate coverage, and blocking Tester findings.
    """
    if not _has_verification_predicates(state):
        return {}

    result = await run_verifier_semantic(dict(state))
    predicate_coverage = [
        _coverage_to_dict(item)
        for item in (getattr(result, "predicate_coverage", None) or [])
    ]
    summary = _aggregate_verify_summary(
        state,
        predicate_coverage=predicate_coverage,
        include_semantic=True,
    )
    delta: dict[str, Any] = {"verify_summary": summary}
    previous_summary = state.get("verify_summary") or {}
    if not summary["passed"] and previous_summary.get("passed", True):
        delta["verify_retries"] = (state.get("verify_retries") or 0) + 1
    return delta


VERIFY_TARGETS = ("run_tests", "run_linters", "run_compile", "run_happy_path")


def verify_fanout(state: PipelineState) -> list[Send]:
    """Conditional-edge router: emit one `Send` per verify node.

    Returning a list of `Send` from a conditional edge is the LangGraph
    pattern that makes the four verify nodes execute in the same superstep
    — Studio renders them pulsing simultaneously (R11). The deferred
    aggregator that consumes their writes lands in task 3.4.
    """
    return [Send(target, state) for target in VERIFY_TARGETS]


def _blocking_tester_findings(state: PipelineState) -> int:
    count = 0
    for finding in state.get("tester_findings") or []:
        if _read_field(finding, "resolved", False):
            continue
        if _read_field(finding, "blocking", True):
            count += 1
    return count


def _uncovered_predicates(
    expected: list[tuple[str, str]],
    predicate_coverage: list[dict[str, Any]],
) -> int:
    coverage_by_key: dict[tuple[str, str], str] = {}
    for item in predicate_coverage:
        wp_id = str(_read_field(item, "wp_id", ""))
        predicate = str(_read_field(item, "predicate", "")).strip()
        if not predicate:
            continue
        coverage_by_key[(wp_id, predicate)] = str(_read_field(item, "status", ""))

    if expected:
        return sum(
            1
            for key in expected
            if coverage_by_key.get(key) != "covered"
        )

    return sum(
        1
        for item in predicate_coverage
        if str(_read_field(item, "status", "")) != "covered"
    )


def _aggregate_verify_summary(
    state: PipelineState,
    *,
    predicate_coverage: list[dict[str, Any]] | None = None,
    include_semantic: bool = False,
) -> VerifySummary:
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
    blocking_tester_findings = _blocking_tester_findings(state)
    passed = (
        failed_tests == 0
        and hard_findings == 0
        and blocking_tester_findings == 0
    )
    summary = VerifySummary(
        passed=passed,
        failed_tests=failed_tests,
        hard_findings=hard_findings,
    )
    if blocking_tester_findings:
        summary["blocking_tester_findings"] = blocking_tester_findings

    if include_semantic:
        coverage = predicate_coverage or []
        uncovered_predicates = _uncovered_predicates(
            _expected_predicates(state),
            coverage,
        )
        summary["predicate_coverage"] = coverage
        summary["uncovered_predicates"] = uncovered_predicates
        summary["passed"] = passed and uncovered_predicates == 0

    return summary


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
    """Verify subgraph: fan out mechanical checks, aggregate, then cover predicates.

    Compiled with `RunContext` so the inner nodes see `runtime.context`
    (sandbox, repo_path) when invoked from the top-level graph.
    """
    g = StateGraph(PipelineState, context_schema=RunContext)
    g.add_node("run_tests", run_tests_node)
    g.add_node("run_linters", run_linters_node)
    g.add_node("run_compile", run_compile_node)
    g.add_node("run_happy_path", run_happy_path_node)
    g.add_node("aggregate", aggregate, defer=True)
    g.add_node("run_semantic_coverage", run_semantic_coverage_node)

    g.add_conditional_edges(START, verify_fanout, list(VERIFY_TARGETS))
    for target in VERIFY_TARGETS:
        g.add_edge(target, "aggregate")
    g.add_edge("aggregate", "run_semantic_coverage")
    g.add_edge("run_semantic_coverage", END)
    return g.compile()
