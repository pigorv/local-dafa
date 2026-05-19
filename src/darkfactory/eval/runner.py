"""Benchmark evaluation runner."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from darkfactory.runtime.tracing import coalesced_trace_id
from darkfactory.state import RunResult

log = logging.getLogger(__name__)

DEFAULT_LOCAL_LANGFUSE_HOST = "http://localhost:3000"
DEFAULT_DOCKER_LANGFUSE_HOST = "http://langfuse-web:3000"
DEFAULT_LOCAL_LANGFUSE_PUBLIC_KEY = "pk-lf-local"
DEFAULT_LOCAL_LANGFUSE_SECRET_KEY = "sk-lf-local"


class BenchmarkConfigError(ValueError):
    """The benchmark YAML is malformed."""


class CaseSetupError(RuntimeError):
    """A case could not be prepared before workflow execution."""


@dataclass(frozen=True)
class WorkflowRun:
    workflow_id: str
    workflow_run_id: str | None
    result: RunResult | None = None

    @property
    def trace_id(self) -> str:
        return coalesced_trace_id(self.workflow_id, self.workflow_run_id)


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    actual: dict[str, Any]
    expected: dict[str, Any]
    misses: list[str]
    workflow_id: str = ""
    workflow_run_id: str | None = None
    trace_id: str = ""


def load_dataset(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise BenchmarkConfigError("benchmark.yaml must be a mapping")
    if data.get("version") != 1:
        raise BenchmarkConfigError("benchmark.yaml must set version: 1")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BenchmarkConfigError("benchmark.yaml must contain a non-empty cases list")
    for index, case in enumerate(cases):
        _validate_case(index, case)
    return cases


def _validate_case(index: int, case: Any) -> None:
    if not isinstance(case, dict):
        raise BenchmarkConfigError(f"case #{index + 1} must be a mapping")
    required = ["id", "description", "repo_url", "repo_sha", "issue", "expected"]
    missing = [key for key in required if key not in case]
    if missing:
        raise BenchmarkConfigError(f"case #{index + 1} missing required keys: {missing}")
    issue = case["issue"]
    if not isinstance(issue, dict) or not issue.get("title") or not issue.get("body"):
        raise BenchmarkConfigError(f"case {case['id']} issue must include title and body")
    expected = case["expected"]
    if not isinstance(expected, dict):
        raise BenchmarkConfigError(f"case {case['id']} expected must be a mapping")
    tags = case.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise BenchmarkConfigError(f"case {case['id']} tags must be a list of strings")


def _clone_at_sha(repo_url: str, sha: str) -> Path:
    workdir = Path(tempfile.mkdtemp(prefix="df-eval-"))
    try:
        subprocess.run(
            ["git", "clone", "--no-tags", repo_url, str(workdir)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(workdir), "checkout", "--detach", sha],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise CaseSetupError(exc.stderr.strip() or str(exc)) from exc
    return workdir


def _coverage_pct(state: dict[str, Any]) -> float:
    coverage = (state.get("verify_summary") or {}).get("predicate_coverage") or []
    if not coverage:
        return 0.0
    covered = sum(1 for item in coverage if item.get("status") == "covered")
    weak = sum(1 for item in coverage if item.get("status") == "weakly_covered")
    return (covered + 0.5 * weak) / len(coverage)


def _touched_files(state: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for patch in state.get("patches") or []:
        path = str(patch.get("path") or "").strip()
        if not path or path.startswith("("):
            continue
        paths.append(path)
    return sorted(set(paths))


def _forbidden_matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return path == pattern


def _review_decision(state: dict[str, Any]) -> dict[str, Any]:
    decision = state.get("review_decision") or {}
    return decision if isinstance(decision, dict) else {}


def _fixer_invocation_count(state: dict[str, Any]) -> int:
    attempt_log = state.get("attempt_log")
    if isinstance(attempt_log, list):
        count = sum(
            1
            for entry in attempt_log
            if isinstance(entry, dict) and entry.get("source") == "fixer_attempt"
        )
        if count or attempt_log:
            return count
    return _fixer_target_attempt_count(state)


def _fixer_target_attempt_count(state: dict[str, Any]) -> int:
    return sum(
        int(value)
        for value in (state.get("fixer_attempts_by_wp") or {}).values()
    )


def _evaluate(case: dict[str, Any], run: WorkflowRun) -> CaseResult:
    if run.result is None:
        raise ValueError("cannot evaluate a workflow that was started with wait=False")
    state = run.result.state
    expected = case["expected"]
    review = _review_decision(state)
    fixer_attempts = _fixer_invocation_count(state)
    touched = _touched_files(state)

    actual = {
        "status": run.result.status,
        "reason": run.result.reason,
        "pr_url": state.get("pr_url"),
        "pr_created": bool(state.get("pr_url")),
        "verify_passed": bool((state.get("verify_summary") or {}).get("passed")),
        "predicate_coverage_pct": _coverage_pct(state),
        "planning_attempts": int(state.get("planning_attempts") or 0),
        "fixer_attempts": int(fixer_attempts),
        "fixer_target_attempts": _fixer_target_attempt_count(state),
        "reviewer_severity": review.get("severity"),
        "reviewer_recommendation_approve": review.get("recommendation") == "approve",
        "touched_files": touched,
    }

    misses: list[str] = []
    if expected.get("pr_created") and not actual["pr_created"]:
        misses.append("expected pr_created=true, got false")
    if expected.get("verify_passed") and not actual["verify_passed"]:
        misses.append("expected verify_passed=true, got false")
    threshold = float(expected.get("predicate_coverage_min_pct", 0.0))
    if actual["predicate_coverage_pct"] < threshold:
        misses.append(
            f"predicate_coverage_pct {actual['predicate_coverage_pct']:.2f} "
            f"< {threshold:.2f}"
        )
    planning_max = expected.get("planning_attempts_max")
    if planning_max is not None and actual["planning_attempts"] > int(planning_max):
        misses.append(f"planning_attempts {actual['planning_attempts']} > {planning_max}")
    fixer_max = expected.get("fixer_attempts_max")
    if fixer_max is not None and actual["fixer_attempts"] > int(fixer_max):
        misses.append(f"fixer_attempts {actual['fixer_attempts']} > {fixer_max}")
    severity_order = {None: 0, "low": 1, "medium": 2, "high": 3}
    max_severity = expected.get("reviewer_severity_max")
    if (
        max_severity
        and severity_order.get(actual["reviewer_severity"], 0)
        > severity_order[max_severity]
    ):
        misses.append(f"reviewer.severity={actual['reviewer_severity']} > {max_severity}")
    if (
        expected.get("reviewer_recommendation_approve")
        and not actual["reviewer_recommendation_approve"]
    ):
        misses.append("expected reviewer recommendation approve")

    forbidden = expected.get("must_not_touch_files") or []
    bad_touches = [
        path
        for path in touched
        if any(_forbidden_matches(path, pattern) for pattern in forbidden)
    ]
    if bad_touches:
        misses.append(f"touched forbidden files: {bad_touches}")

    return CaseResult(
        case_id=case["id"],
        passed=not misses,
        actual=actual,
        expected=expected,
        misses=misses,
        workflow_id=run.workflow_id,
        workflow_run_id=run.workflow_run_id,
        trace_id=run.trace_id,
    )


def _langfuse_client():
    _load_dotenv_for_eval()
    if os.environ.get("LANGFUSE_EVAL_ENABLED", "true").lower() == "false":
        return None
    if not _configure_langfuse_eval_env():
        return None
    try:
        from langfuse import Langfuse

        return Langfuse()
    except Exception:
        log.warning("langfuse init failed; benchmark will still run", exc_info=True)
        return None


def _load_dotenv_for_eval() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        log.debug("dotenv load failed for eval runner", exc_info=True)


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists()


def _langfuse_host_for_eval() -> str:
    configured = (os.environ.get("LANGFUSE_HOST") or "").strip()
    if not configured:
        if _running_in_container():
            return DEFAULT_DOCKER_LANGFUSE_HOST
        return DEFAULT_LOCAL_LANGFUSE_HOST
    if (
        not _running_in_container()
        and configured.rstrip("/") == DEFAULT_DOCKER_LANGFUSE_HOST
    ):
        return DEFAULT_LOCAL_LANGFUSE_HOST
    return configured


def _is_local_langfuse_host(host: str) -> bool:
    return any(
        marker in host
        for marker in (
            "localhost",
            "127.0.0.1",
            "langfuse-web",
        )
    )


def _configure_langfuse_eval_env() -> bool:
    host = _langfuse_host_for_eval()
    public_key = (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    secret_key = (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()
    if _is_local_langfuse_host(host):
        public_key = public_key or DEFAULT_LOCAL_LANGFUSE_PUBLIC_KEY
        secret_key = secret_key or DEFAULT_LOCAL_LANGFUSE_SECRET_KEY
    if not (public_key and secret_key):
        return False
    os.environ["LANGFUSE_HOST"] = host
    os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
    os.environ["LANGFUSE_SECRET_KEY"] = secret_key
    return True


def _dataset_item_id(dataset_name: str, case_id: str) -> str:
    import hashlib

    return hashlib.sha256(f"{dataset_name}:{case_id}".encode("utf-8")).hexdigest()[
        :32
    ]


def _upsert_dataset_item(client: Any, dataset_name: str, case: dict[str, Any]) -> Any:
    try:
        client.create_dataset(
            name=dataset_name,
            description="Dark Factory held-out benchmark cases",
        )
    except Exception:
        pass
    return client.create_dataset_item(
        dataset_name=dataset_name,
        id=_dataset_item_id(dataset_name, case["id"]),
        input=case["issue"],
        expected_output=case["expected"],
        metadata={
            "case_id": case["id"],
            "description": case.get("description"),
            "repo_url": case.get("repo_url"),
            "repo_sha": case.get("repo_sha"),
            "tags": case.get("tags") or [],
            "behavioral_predicates": case.get("expected", {}).get(
                "behavioral_predicates"
            )
            or [],
        },
    )


def _score_values(result: CaseResult) -> dict[str, float]:
    expected_touches = set(result.expected.get("must_touch_files") or [])
    touched = set(result.actual.get("touched_files") or [])
    overlap = len(expected_touches & touched) / max(1, len(expected_touches))
    return {
        "passed": 1.0 if result.passed else 0.0,
        "pr_created": 1.0 if result.actual.get("pr_created") else 0.0,
        "verify_passed": 1.0 if result.actual.get("verify_passed") else 0.0,
        "predicate_coverage_pct": float(
            result.actual.get("predicate_coverage_pct") or 0.0
        ),
        "planning_attempts": float(result.actual.get("planning_attempts") or 0),
        "fixer_attempts": float(result.actual.get("fixer_attempts") or 0),
        "must_touch_overlap_pct": float(overlap),
    }


def _write_langfuse_result(
    *,
    client: Any,
    dataset_name: str,
    run_name: str,
    case: dict[str, Any],
    result: CaseResult,
) -> None:
    item = _upsert_dataset_item(client, dataset_name, case)
    run_item = client.api.dataset_run_items.create(
        run_name=run_name,
        run_description="Dark Factory benchmark run",
        dataset_item_id=item.id,
        trace_id=result.trace_id,
        metadata={
            "case_id": result.case_id,
            "passed": result.passed,
            "misses": result.misses,
            "workflow_id": result.workflow_id,
            "workflow_run_id": result.workflow_run_id,
            "actual": result.actual,
        },
    )
    for name, value in _score_values(result).items():
        client.create_score(
            name=f"benchmark.{name}",
            value=value,
            dataset_run_id=run_item.dataset_run_id,
            data_type="NUMERIC",
            comment="; ".join(result.misses[:3]) or None,
            metadata={
                "case_id": result.case_id,
                "trace_id": result.trace_id,
                "workflow_id": result.workflow_id,
                "workflow_run_id": result.workflow_run_id,
            },
        )
    client.flush()


async def run(
    benchmark_path: Path,
    *,
    dataset_name: str = "benchmark-prod",
    tag_filter: list[str] | None = None,
    run_name: str | None = None,
    write_langfuse: bool = True,
    close_prs: bool = True,
) -> int:
    from darkfactory.cli import _start_workflow_and_wait

    cases = load_dataset(benchmark_path)
    if tag_filter:
        wanted = set(tag_filter)
        cases = [case for case in cases if wanted.intersection(case.get("tags") or [])]
    if not cases:
        raise BenchmarkConfigError("no benchmark cases selected")

    run_name = run_name or (
        f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{_short_sha()}"
    )
    langfuse = _langfuse_client() if write_langfuse else None

    results: list[CaseResult] = []
    for case in cases:
        repo_path: Path | None = None
        try:
            repo_path = _clone_at_sha(case["repo_url"], case["repo_sha"])
            prompt = _case_prompt(case)
            workflow = await _start_workflow_and_wait(
                prompt=prompt,
                repo=repo_path,
                workflow_id=f"darkfactory-eval-{case['id']}-{uuid.uuid4().hex[:8]}",
                auto_eval_gates=True,
            )
            assert workflow.result is not None
            result = _evaluate(case, workflow)
        except Exception as exc:
            result = CaseResult(
                case_id=case.get("id", "<unknown>"),
                passed=False,
                actual={"error": type(exc).__name__, "message": str(exc)},
                expected=case.get("expected") or {},
                misses=[f"{type(exc).__name__}: {exc}"],
            )
        finally:
            if repo_path is not None:
                shutil.rmtree(repo_path, ignore_errors=True)

        results.append(result)
        if langfuse is not None and result.trace_id:
            try:
                _write_langfuse_result(
                    client=langfuse,
                    dataset_name=dataset_name,
                    run_name=run_name,
                    case=case,
                    result=result,
                )
            except Exception:
                log.warning("failed to write Langfuse dataset result", exc_info=True)
        if close_prs and result.actual.get("pr_url"):
            _close_pr(str(result.actual["pr_url"]))

    _print_table(results, run_name=run_name)
    return 0 if all(result.passed for result in results) else 1


def _case_prompt(case: dict[str, Any]) -> str:
    issue = case["issue"]
    return f"{issue['title']}\n\n{issue['body']}"


def _short_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
        ).strip()
    except Exception:
        return "nogit"


def _close_pr(pr_url: str) -> None:
    subprocess.run(
        [
            "gh",
            "pr",
            "close",
            pr_url,
            "--delete-branch",
            "--comment",
            "Closed after Dark Factory benchmark run.",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ascii(value: Any) -> str:
    return str(value).encode("ascii", "replace").decode("ascii")


def _print_table(results: list[CaseResult], *, run_name: str) -> None:
    print(f"RUN {_ascii(run_name)}")
    print(f"{'CASE':<28} {'PASS':<5} {'COVERAGE':<9} {'PLAN':<5} {'FIX':<4} TRACE")
    for result in results:
        coverage = float(result.actual.get("predicate_coverage_pct") or 0.0)
        planning_attempts = int(result.actual.get("planning_attempts") or 0)
        fixer_attempts = int(result.actual.get("fixer_attempts") or 0)
        trace_id = _ascii(result.trace_id)
        if len(trace_id) > 8:
            trace_id = f"{trace_id[:8]}..."
        print(
            f"{_ascii(result.case_id)[:28]:<28} "
            f"{'yes' if result.passed else 'no':<5} "
            f"{coverage:<9.2f} "
            f"{planning_attempts:<5} "
            f"{fixer_attempts:<4} "
            f"{trace_id}"
        )
