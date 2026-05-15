"""Verify-stage nodes: language-agnostic, plan-driven.

The verifier no longer knows about Maven, Gradle, npm, or any specific
toolchain. On the first iteration of a workflow it asks the
``verify_planner`` role to discover the target repo's canonical
test / compile / lint commands; the resulting ``VerificationPlan`` is
cached on ``PipelineState.verification_plan`` and reused for every
subsequent verify iteration.

Each step in the plan is executed once via ``RepoSandbox.exec``. When the
plan declares ``report_paths`` and a ``report_kind``, the corresponding
structured-report reader in ``tools/reports.py`` produces the ground
truth (test counts or Findings). When it doesn't, the verifier falls
back to exit-code gating: rc=0 is a pass, rc≠0 surfaces a synthetic
Finding so the aggregator can see it. Stdout regex parsing was removed
in this refactor — it broke whenever a build added a log-suppression
flag (e.g. ``mvn -q``).

State deltas:
- ``ensure_plan_node`` → sets ``verification_plan`` / ``verification_plan_rev`` /
  ``findings`` (a discovery-failure finding when the planner emits an
  empty plan).
- ``run_plan_node``    → appends one TestResult per ``test`` step plus
  Findings for compile / lint steps.
- ``run_semantic_coverage_node`` → adds predicate coverage and final
  pass/fail to ``verify_summary``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

from langgraph.graph import END, START, StateGraph

from darkfactory.agents._sdk_common import ParseError
from darkfactory.agents.verifier_semantic import run_verifier_semantic
from darkfactory.agents.verify_planner import run_verify_planner
from darkfactory.runtime.tracing import phase_span
from darkfactory.state import (
    Finding,
    PipelineState,
    RunContext,
    TestResult,
    VerifySummary,
)
from darkfactory.tools.reports import (
    read_checkstyle_xml,
    read_junit_xml,
    read_sarif,
)
from darkfactory.tools.sandbox import RepoSandbox
from darkfactory.tools.shell import get_sandbox, register_sandbox


def _ensure_sandbox(ctx: Any):
    sb = get_sandbox(ctx.task_id)
    if sb is None:
        register_sandbox(ctx.task_id, RepoSandbox(repo_path=ctx.repo_path))
        sb = get_sandbox(ctx.task_id)
    return sb


# --- plan discovery -------------------------------------------------------


def _plan_is_empty(plan: Mapping[str, Any] | None) -> bool:
    """A plan with no executable steps is treated as a discovery failure."""
    if not plan:
        return True
    test = plan.get("test")
    compile_step = plan.get("compile")
    lint = plan.get("lint") or []
    has_test = isinstance(test, Mapping) and test.get("argv")
    has_compile = isinstance(compile_step, Mapping) and compile_step.get("argv")
    has_lint = any(
        isinstance(item, Mapping) and item.get("argv") for item in lint
    )
    return not (has_test or has_compile or has_lint)


async def ensure_plan_node(state: PipelineState, runtime=None) -> dict:
    """Populate ``verification_plan`` on first verify; no-op when cached.

    On planner failure (no structured output, empty plan) we emit a
    single ``error`` Finding so the aggregator's hard-findings count
    fails the gate and the workflow escalates without burning Fixer
    budget on a problem the Fixer cannot solve.
    """
    cached = state.get("verification_plan")
    if cached and not _plan_is_empty(cached):
        return {}

    try:
        with phase_span("node.verify_planner"):
            plan = await run_verify_planner(dict(state))
    except ParseError as exc:
        return {
            "findings": [
                Finding(
                    tool="verify_planner",
                    severity="error",
                    file="(verify_planner)",
                    line=0,
                    rule="discovery_failed",
                    message=str(exc),
                )
            ],
        }

    if _plan_is_empty(plan):
        notes = ""
        if isinstance(plan, Mapping):
            notes = str(plan.get("notes") or "").strip()
        message = (
            "verify_planner emitted no executable steps"
            + (f"; notes: {notes}" if notes else "")
        )
        return {
            "verification_plan": plan,
            "verification_plan_rev": int(state.get("verification_plan_rev") or 0) + 1,
            "findings": [
                Finding(
                    tool="verify_planner",
                    severity="error",
                    file="(verify_planner)",
                    line=0,
                    rule="empty_plan",
                    message=message,
                )
            ],
        }

    return {
        "verification_plan": plan,
        "verification_plan_rev": int(state.get("verification_plan_rev") or 0) + 1,
    }


# --- plan execution -------------------------------------------------------


def _stderr_tail(result: Mapping[str, Any], limit: int = 20) -> str:
    err = str(result.get("stderr") or "")
    out = str(result.get("stdout") or "")
    tail_source = err.strip() or out.strip()
    lines = tail_source.splitlines()[-limit:]
    return "\n".join(lines)


def _parse_step_findings(
    step: Mapping[str, Any],
    repo_root: Path,
) -> tuple[list[Finding], bool]:
    """Parse declared report files for one step.

    Returns ``(findings, parsed_any)`` — ``parsed_any`` is True when
    ``report_paths`` was declared and resolved to at least one file, so
    the verifier knows whether to trust the report or fall back to
    exit-code gating.
    """
    report_paths = list(step.get("report_paths") or [])
    if not report_paths:
        return [], False
    kind = step.get("report_kind")
    if not kind:
        return [], False
    tool = str(step.get("name") or kind)
    if kind == "checkstyle-xml":
        findings = read_checkstyle_xml(report_paths, repo_root, tool=tool)
        # ``read_checkstyle_xml`` only inspects file presence implicitly;
        # treat any matched file as "parsed" so a clean run with zero
        # findings still suppresses the exit-code fallback.
        return findings, _any_report_file_exists(report_paths, repo_root)
    if kind == "sarif":
        findings = read_sarif(report_paths, repo_root, tool=tool)
        return findings, _any_report_file_exists(report_paths, repo_root)
    # JUnit-XML is parsed by the test branch; treat as no findings here.
    return [], _any_report_file_exists(report_paths, repo_root)


def _any_report_file_exists(report_paths: list[str], repo_root: Path) -> bool:
    import glob as _glob

    for pattern in report_paths:
        if not pattern:
            continue
        base = Path(pattern)
        if base.is_absolute():
            matches = _glob.glob(str(base), recursive=True)
        else:
            matches = _glob.glob(str(repo_root / pattern), recursive=True)
        if any(Path(m).is_file() for m in matches):
            return True
    return False


def _execute_step(
    sandbox: Any,
    step: Mapping[str, Any],
    repo_root: Path,
) -> tuple[dict[str, Any], float]:
    argv = list(step.get("argv") or [])
    timeout = int(step.get("timeout_s") or 600)
    started = time.monotonic()
    result = sandbox.exec(argv, timeout=timeout)
    duration = time.monotonic() - started
    return dict(result), duration


def _run_test_step(
    sandbox: Any,
    step: Mapping[str, Any],
    repo_root: Path,
) -> TestResult:
    """Execute a ``test`` step and return one ``TestResult``.

    Counts come from the declared JUnit-XML report when present;
    otherwise they fall back to ``(0, 0)`` with exit-code-derived
    errors. The synthetic error tail lets the aggregator count the step
    as a failure without inventing fake passed/failed numbers.
    """
    result, duration = _execute_step(sandbox, step, repo_root)
    rc = int(result.get("returncode", -1))
    errors: list[str] = []
    if result.get("timed_out"):
        errors.append("timed_out")

    report_kind = step.get("report_kind")
    report_paths = list(step.get("report_paths") or [])
    name = str(step.get("name") or "test")

    if report_kind == "junit-xml" and report_paths:
        summary = read_junit_xml(report_paths, repo_root)
        executed = summary.executed_tests
        if not summary.parsed_files and rc != 0:
            tail = _stderr_tail(result)
            if tail:
                errors.append(tail)
            errors.append(
                "junit-xml report files declared but none were emitted by the test step"
            )
        if summary.parse_errors:
            errors.extend(summary.parse_errors)
        return TestResult(
            runner=name,
            returncode=rc,
            passed=summary.passed,
            failed=summary.failed,
            errors=errors,
            duration_s=duration,
            executed_tests=executed,
        )

    # No report — exit-code fallback. We don't fabricate test counts.
    if rc != 0:
        tail = _stderr_tail(result)
        if tail:
            errors.append(tail)
        else:
            errors.append(f"test step exited non-zero ({rc})")
    return TestResult(
        runner=name,
        returncode=rc,
        passed=0,
        failed=0,
        errors=errors,
        duration_s=duration,
    )


def _run_finding_step(
    sandbox: Any,
    step: Mapping[str, Any],
    repo_root: Path,
    *,
    default_rule: str,
) -> list[Finding]:
    """Execute a compile / lint step and return parsed Findings.

    Behaviour:
    - Reports declared and parsed → emit those Findings verbatim. A
      clean run (zero entries in the report) emits zero Findings, even
      if rc≠0 — the report is the source of truth.
    - No reports declared, rc=0 → emit nothing. Step passed.
    - No reports declared, rc≠0, ``required=True`` → emit one synthetic
      error Finding so the aggregator sees a hard failure.
    - No reports declared, rc≠0, ``required=False`` → emit one warn
      Finding so the trace surfaces it but the gate doesn't block.
    """
    result, _ = _execute_step(sandbox, step, repo_root)
    rc = int(result.get("returncode", -1))
    name = str(step.get("name") or default_rule)
    required = bool(step.get("required", True))

    findings, parsed_any = _parse_step_findings(step, repo_root)
    if parsed_any:
        return findings

    if rc == 0 and not result.get("timed_out"):
        return []

    tail = _stderr_tail(result)
    message = tail or f"{name} exited non-zero ({rc})"
    if result.get("timed_out"):
        message = f"{name}: timed_out\n{message}".rstrip()
    severity = "error" if required else "warn"
    return [
        Finding(
            tool=name,
            severity=severity,  # type: ignore[arg-type]
            file=f"({name})",
            line=0,
            rule=default_rule,
            message=message,
        )
    ]


async def run_plan_node(state: PipelineState, runtime=None) -> dict:
    """Execute every step in the cached VerificationPlan.

    The plan is required to be present (set by ``ensure_plan_node``). If
    it's empty for any reason — e.g. the planner failed and emitted a
    discovery-failure Finding — we still return cleanly so the aggregate
    node can compute a summary based on existing state.
    """
    ctx = getattr(runtime, "context", None) if runtime is not None else None
    if ctx is None:
        return {}

    plan = state.get("verification_plan") or {}
    if _plan_is_empty(plan):
        return {}

    sandbox = _ensure_sandbox(ctx)
    if sandbox is None:
        return {}

    repo_root = Path(ctx.repo_path)
    test_results: list[TestResult] = []
    findings: list[Finding] = []

    test_step = plan.get("test")
    if isinstance(test_step, Mapping) and test_step.get("argv"):
        test_results.append(_run_test_step(sandbox, test_step, repo_root))

    compile_step = plan.get("compile")
    if isinstance(compile_step, Mapping) and compile_step.get("argv"):
        findings.extend(
            _run_finding_step(sandbox, compile_step, repo_root, default_rule="compile")
        )

    for lint_step in plan.get("lint") or []:
        if not (isinstance(lint_step, Mapping) and lint_step.get("argv")):
            continue
        findings.extend(
            _run_finding_step(sandbox, lint_step, repo_root, default_rule="lint")
        )

    delta: dict[str, Any] = {}
    if test_results:
        delta["test_results"] = test_results
    if findings:
        delta["findings"] = findings
    return delta


# --- helpers preserved from the old subgraph -----------------------------


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
    """Ask the Semantic Verifier to map WP predicates to evidence."""
    if not _has_verification_predicates(state):
        return {}

    with phase_span("node.verifier_semantic"):
        result = await run_verifier_semantic(dict(state))
    predicate_coverage = [
        _coverage_to_dict(item)
        for item in (result.get("predicate_coverage") or [])
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


_BLOCKING_RECONCILIATION_KINDS = frozenset(
    {
        "builder_blocked",
        "builder_no_action",
        "claimed_edits_not_applied",
        "tester_parse_failure",
        "fixer_blocked",
    }
)


def _blocking_failures(state: PipelineState) -> int:
    tester = len(state.get("tester_findings") or [])
    recon = sum(
        1
        for finding in (state.get("reconciliation_findings") or [])
        if (
            isinstance(finding, dict)
            and finding.get("kind") in _BLOCKING_RECONCILIATION_KINDS
        )
    )
    return tester + recon


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
    blocking_failures = _blocking_failures(state)
    passed = (
        failed_tests == 0
        and hard_findings == 0
        and blocking_failures == 0
    )
    summary = VerifySummary(
        passed=passed,
        failed_tests=failed_tests,
        hard_findings=hard_findings,
    )
    if blocking_failures:
        summary["blocking_failures"] = blocking_failures

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
    """Reduce mechanical evidence (tests + findings) to a verdict."""
    summary = _aggregate_verify_summary(state)
    delta: dict[str, Any] = {"verify_summary": summary}
    if not summary["passed"]:
        delta["verify_retries"] = (state.get("verify_retries") or 0) + 1
    return delta


def verify_subgraph() -> Any:
    """Verify subgraph: discover plan → run plan → aggregate → semantic.

    The four-way fan-out from the previous design is gone; the plan
    discovery + execution path is sequential because each downstream
    node depends on the prior node's writes (the planner populates
    ``verification_plan``, ``run_plan_node`` populates ``test_results``
    and ``findings``, and only then can the aggregator and semantic
    coverage step compute a summary).
    """
    g = StateGraph(PipelineState, context_schema=RunContext)
    g.add_node("ensure_plan", ensure_plan_node)
    g.add_node("run_plan", run_plan_node)
    g.add_node("aggregate", aggregate)
    g.add_node("run_semantic_coverage", run_semantic_coverage_node)

    g.add_edge(START, "ensure_plan")
    g.add_edge("ensure_plan", "run_plan")
    g.add_edge("run_plan", "aggregate")
    g.add_edge("aggregate", "run_semantic_coverage")
    g.add_edge("run_semantic_coverage", END)
    return g.compile()
