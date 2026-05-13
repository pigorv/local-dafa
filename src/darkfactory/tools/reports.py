"""Structured-report readers for the plan-driven verifier.

Replaces the stdout regex parsers in ``tools/tests.py`` and
``tools/linters.py``. The verifier's plan declares ``report_paths`` (a list
of globs relative to the repo root) and a ``report_kind``; this module
loads those files and turns them into the ``TestResult``-style summary or
``Finding`` records the rest of the pipeline already consumes.

Three readers, one per ``report_kind``:

- ``read_junit_xml`` — Surefire / Gradle / pytest / jest-junit / gotestsum
  all emit the same ``<testsuite tests="N" failures="F" errors="E"
  skipped="S">`` shape (sometimes wrapped in a ``<testsuites>`` root). We
  parse counts off the suite element and per-test names off ``<testcase>``
  children.
- ``read_checkstyle_xml`` — Checkstyle's ``<checkstyle><file><error/>``
  schema. Severities map cleanly onto the ``Finding`` severity enum.
- ``read_sarif`` — SARIF 2.1.0. We pull ``ruleId``, ``level``,
  ``message.text`` and the first physical location.

All readers are pure: they take resolved paths and return data structures,
with no I/O outside ``open`` / ``glob``. The verify node owns sandboxing
and globbing; this module just parses.
"""
from __future__ import annotations

import glob
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, cast

from darkfactory.state import Finding


_SEVERITY_FROM_CHECKSTYLE: dict[str, str] = {
    "error": "error",
    "warning": "warn",
    "warn": "warn",
    "info": "info",
    "ignore": "info",
}

_SEVERITY_FROM_SARIF: dict[str, str] = {
    "error": "error",
    "warning": "warn",
    "note": "info",
    "none": "info",
}


@dataclass(slots=True)
class JUnitSummary:
    """Aggregated counts and per-test names across one or more JUnit-XML files.

    Counts are *summed* across every parsed ``<testsuite>`` so a
    multi-module Maven build (one file per class) reports the same total
    Surefire would print. ``executed_tests`` lists every ``<testcase>`` as
    ``"<classname>.<name>"`` when both are present, otherwise just
    ``<name>``. The list lets ``coverage_node`` finally match predicates to
    *actual* test execution evidence rather than the names the tester
    *declared*. ``parse_errors`` collects per-file XML errors so the verify
    node can surface them without failing the whole stage.
    """

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    executed_tests: list[str] = field(default_factory=list)
    parsed_files: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


def _resolve_glob(repo_root: Path, pattern: str) -> list[Path]:
    """Expand ``pattern`` against ``repo_root`` and return existing files."""
    # Absolute patterns are passed through unchanged so a planner that
    # declares an absolute path (rare but legal) still works. Relative
    # patterns are anchored on the repo root, not the process cwd, because
    # the activity runs from somewhere else inside the worker container.
    base = Path(pattern)
    if base.is_absolute():
        matched = glob.glob(str(base), recursive=True)
    else:
        matched = glob.glob(str(repo_root / pattern), recursive=True)
    return [Path(m) for m in sorted(matched) if Path(m).is_file()]


def _iter_report_files(
    paths: Iterable[str],
    repo_root: str | Path,
) -> list[Path]:
    root = Path(repo_root)
    out: list[Path] = []
    seen: set[str] = set()
    for pattern in paths or []:
        if not pattern:
            continue
        for file_path in _resolve_glob(root, pattern):
            key = str(file_path.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(file_path)
    return out


def _iter_testsuite_elements(root: ET.Element) -> Iterable[ET.Element]:
    """Yield every ``<testsuite>`` under ``root`` regardless of wrapping.

    Surefire emits one file per test class with a ``<testsuite>`` root.
    Gradle, pytest, and jest-junit wrap multiple suites in a
    ``<testsuites>`` root. Some pytest configurations emit ``<testsuites>``
    with nested ``<testsuite>`` even for a single suite. Accept all three
    shapes by yielding the root itself when its tag is ``testsuite``, plus
    every descendant tagged ``testsuite``.
    """
    if root.tag == "testsuite":
        yield root
    yield from root.iter("testsuite")


def _testcase_label(case: ET.Element) -> str:
    classname = (case.get("classname") or "").strip()
    name = (case.get("name") or "").strip()
    if classname and name:
        return f"{classname}.{name}"
    return name or classname


def read_junit_xml(
    paths: Iterable[str],
    repo_root: str | Path,
) -> JUnitSummary:
    """Parse every JUnit-XML file matched by ``paths`` and aggregate counts.

    Files that fail to parse are recorded under ``parse_errors`` rather
    than raising — a malformed report from one module shouldn't blank the
    counts from a sibling module.
    """
    summary = JUnitSummary()
    files = _iter_report_files(paths, repo_root)
    for file_path in files:
        summary.parsed_files.append(str(file_path))
        try:
            tree = ET.parse(file_path)
        except ET.ParseError as exc:
            summary.parse_errors.append(f"{file_path}: {exc}")
            continue
        root = tree.getroot()
        # Surefire files write the per-class summary on the <testsuite> root;
        # iterating descendants would double-count nested suites that share
        # the same parent's counts (no known emitter does this today, but
        # guard with a dedup key on element identity).
        seen_suites: set[int] = set()
        for suite in _iter_testsuite_elements(root):
            sid = id(suite)
            if sid in seen_suites:
                continue
            seen_suites.add(sid)
            try:
                tests = int(suite.get("tests") or 0)
                failures = int(suite.get("failures") or 0)
                errors = int(suite.get("errors") or 0)
                skipped = int(suite.get("skipped") or 0)
            except (TypeError, ValueError):
                continue
            summary.total += tests
            summary.failed += failures + errors
            summary.skipped += skipped
            summary.passed += max(0, tests - failures - errors - skipped)
            for case in suite.findall("testcase"):
                label = _testcase_label(case)
                if label:
                    summary.executed_tests.append(label)
    return summary


def _checkstyle_severity(raw: str | None) -> str:
    if not raw:
        return "warn"
    return _SEVERITY_FROM_CHECKSTYLE.get(raw.strip().lower(), "warn")


def _strip_repo_root(path: str, repo_root: Path) -> str:
    """Trim ``repo_root`` from an absolute Checkstyle / SARIF path."""
    if not path:
        return path
    candidate = Path(path)
    if not candidate.is_absolute():
        return path
    try:
        return str(candidate.relative_to(repo_root))
    except ValueError:
        return path


def read_checkstyle_xml(
    paths: Iterable[str],
    repo_root: str | Path,
    *,
    tool: str = "checkstyle",
) -> list[Finding]:
    """Parse Checkstyle XML reports and emit ``Finding`` records.

    ``tool`` defaults to ``"checkstyle"`` but the same XML schema is reused
    by other Maven/Gradle reporters that piggy-back on Checkstyle's writer
    (PMD's XML writer is close enough that callers can override the label).
    """
    root_path = Path(repo_root)
    findings: list[Finding] = []
    for file_path in _iter_report_files(paths, repo_root):
        try:
            tree = ET.parse(file_path)
        except ET.ParseError:
            continue
        report = tree.getroot()
        for file_el in report.iter("file"):
            file_name = _strip_repo_root(file_el.get("name") or "", root_path)
            for error in file_el.findall("error"):
                try:
                    line = int(error.get("line") or 0)
                except (TypeError, ValueError):
                    line = 0
                rule = (error.get("source") or "").rsplit(".", 1)[-1] or "checkstyle"
                findings.append(
                    Finding(
                        tool=tool,
                        severity=cast(Any, _checkstyle_severity(error.get("severity"))),
                        file=file_name,
                        line=line,
                        rule=rule,
                        message=(error.get("message") or "").strip(),
                    )
                )
    return findings


def _sarif_severity(level: str | None) -> str:
    if not level:
        return "warn"
    return _SEVERITY_FROM_SARIF.get(level.strip().lower(), "warn")


def _sarif_location(result: dict[str, Any]) -> tuple[str, int]:
    for location in result.get("locations") or []:
        if not isinstance(location, dict):
            continue
        physical = location.get("physicalLocation") or {}
        if not isinstance(physical, dict):
            continue
        artifact = physical.get("artifactLocation") or {}
        if not isinstance(artifact, dict):
            continue
        uri = str(artifact.get("uri") or "")
        region = physical.get("region") or {}
        try:
            line = int(region.get("startLine") or 0) if isinstance(region, dict) else 0
        except (TypeError, ValueError):
            line = 0
        if uri:
            return uri, line
    return "", 0


def _sarif_tool_name(run: dict[str, Any], default: str) -> str:
    tool = run.get("tool") or {}
    driver = tool.get("driver") if isinstance(tool, dict) else None
    if isinstance(driver, dict):
        name = driver.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return default


def read_sarif(
    paths: Iterable[str],
    repo_root: str | Path,
    *,
    tool: str = "sarif",
) -> list[Finding]:
    """Parse SARIF 2.1.0 reports and emit ``Finding`` records.

    The driver name from each ``run`` overrides the default ``tool`` label
    so a single SARIF file produced by a multi-rule runner (mypy, ruff,
    eslint, …) still attributes findings to the right tool.
    """
    root_path = Path(repo_root)
    findings: list[Finding] = []
    for file_path in _iter_report_files(paths, repo_root):
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for run in payload.get("runs") or []:
            if not isinstance(run, dict):
                continue
            tool_name = _sarif_tool_name(run, tool)
            for result in run.get("results") or []:
                if not isinstance(result, dict):
                    continue
                uri, line = _sarif_location(result)
                message = ""
                msg_obj = result.get("message") or {}
                if isinstance(msg_obj, dict):
                    message = str(msg_obj.get("text") or "").strip()
                elif isinstance(msg_obj, str):
                    message = msg_obj.strip()
                findings.append(
                    Finding(
                        tool=tool_name,
                        severity=cast(Any, _sarif_severity(result.get("level"))),
                        file=_strip_repo_root(uri, root_path),
                        line=line,
                        rule=str(result.get("ruleId") or "").strip() or tool_name,
                        message=message,
                    )
                )
    return findings
