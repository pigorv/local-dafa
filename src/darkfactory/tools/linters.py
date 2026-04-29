"""run_linters tool: Checkstyle, Spotless, and compile-as-type-check.

Build-system detection mirrors tools/tests.py. Each linter executes inside
the per-run sandbox via the per-task RepoSandbox registry. Outputs are
parsed into Finding records (state.Finding). Compile errors are surfaced
as findings with severity="error" — in Java this is the type-check
analogue to mypy/tsc.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from langchain_core.tools import tool
from langgraph.runtime import get_runtime

from darkfactory.state import Finding, RunContext
from darkfactory.tools.shell import get_sandbox


def detect_build(repo_path: str | Path) -> dict[str, Any]:
    """Return per-checker argv for the detected build system, or {error}."""
    root = Path(repo_path)
    if (root / "pom.xml").exists():
        return {
            "kind": "maven",
            "checkstyle": ["mvn", "-q", "-B", "checkstyle:check"],
            "spotless": ["mvn", "-q", "-B", "spotless:check"],
            "compile": ["mvn", "-q", "-B", "compile", "test-compile"],
        }
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        prefix = ["./gradlew"] if (root / "gradlew").exists() else ["gradle"]
        common = ["--console=plain"]
        return {
            "kind": "gradle-wrapper" if (root / "gradlew").exists() else "gradle",
            "checkstyle": prefix + common + ["checkstyleMain", "checkstyleTest"],
            "spotless": prefix + common + ["spotlessCheck"],
            "compile": prefix + common + ["compileJava", "compileTestJava"],
        }
    return {"error": "cannot detect project type"}


_CS_MAVEN_RE = re.compile(
    r"\[(ERROR|WARN)\]\s+(\S+\.java):\[(\d+)(?:,\d+)?\]\s+"
    r"(?:\(([^)]+)\)\s+)?([\w.$]+):\s*(.+?)\s*$",
    re.MULTILINE,
)

_CS_GRADLE_RE = re.compile(
    r"^(\S+\.java):(\d+)(?::\d+)?:\s*(warning|error):\s*(.*?)"
    r"(?:\s*\[([\w.$]+)\])?\s*$",
    re.MULTILINE,
)

_COMPILE_MAVEN_RE = re.compile(
    r"\[ERROR\]\s+(\S+\.java):\[(\d+),\d+\]\s+(.+?)\s*$",
    re.MULTILINE,
)

_COMPILE_GRADLE_RE = re.compile(
    r"^(\S+\.java):(\d+):\s*(error|warning):\s*(.+?)\s*$",
    re.MULTILINE,
)


def _sev(label: str) -> str:
    low = label.lower()
    if low == "error":
        return "error"
    if low in ("warn", "warning"):
        return "warn"
    return "info"


def parse_checkstyle(stdout: str, stderr: str) -> list[Finding]:
    text = stdout + "\n" + stderr
    findings: list[Finding] = []
    for m in _CS_MAVEN_RE.finditer(text):
        sev, path, line, _cat, rule, msg = m.groups()
        findings.append(Finding(
            tool="checkstyle",
            severity=cast(Any, _sev(sev)),
            file=path,
            line=int(line),
            rule=rule,
            message=msg.strip(),
        ))
    if findings:
        return findings
    for m in _CS_GRADLE_RE.finditer(text):
        path, line, sev, msg, rule = m.groups()
        # Skip lines parse_compile would also match — those go through the
        # compile parser. Checkstyle output normally carries a [Rule] tag.
        if rule is None:
            continue
        findings.append(Finding(
            tool="checkstyle",
            severity=cast(Any, _sev(sev)),
            file=path,
            line=int(line),
            rule=rule,
            message=msg.strip(),
        ))
    return findings


def parse_compile(stdout: str, stderr: str) -> list[Finding]:
    text = stdout + "\n" + stderr
    findings: list[Finding] = []
    for m in _COMPILE_MAVEN_RE.finditer(text):
        path, line, msg = m.groups()
        findings.append(Finding(
            tool="javac",
            severity="error",
            file=path,
            line=int(line),
            rule="compile",
            message=msg.strip(),
        ))
    if findings:
        return findings
    for m in _COMPILE_GRADLE_RE.finditer(text):
        path, line, sev, msg = m.groups()
        findings.append(Finding(
            tool="javac",
            severity=cast(Any, _sev(sev)),
            file=path,
            line=int(line),
            rule="compile",
            message=msg.strip(),
        ))
    return findings


def parse_spotless(stdout: str, stderr: str) -> list[Finding]:
    text = stdout + "\n" + stderr
    findings: list[Finding] = []
    seen: set[str] = set()
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            in_block = False
            continue
        low = line.lower()
        if "format violation" in low or "spotless found" in low:
            in_block = True
            continue
        if in_block:
            m = re.match(r"(?:\[ERROR\]\s+)?(\S+\.java)\s*$", line)
            if m:
                path = m.group(1)
                if path not in seen:
                    seen.add(path)
                    findings.append(Finding(
                        tool="spotless",
                        severity="warn",
                        file=path,
                        line=1,
                        rule="format",
                        message="File has Spotless format violations",
                    ))
    return findings


@tool
def run_linters(timeout: int = 600) -> dict[str, Any]:
    """Run Checkstyle, Spotless, and compile (type-check) inside the sandbox.

    Returns {kind, findings, raw}. `findings` is a flat list[Finding]
    aggregated across all three checkers. `raw` keeps each checker's
    returncode/stdout/stderr for downstream debugging.
    """
    rt = get_runtime(RunContext)
    ctx = rt.context
    detected = detect_build(ctx.repo_path)
    if "error" in detected:
        return detected

    sb = get_sandbox(ctx.task_id)
    if sb is None:
        raise RuntimeError(
            f"no sandbox registered for task_id={ctx.task_id!r}; "
            "the activity must register a RepoSandbox before run_linters"
        )

    raw: dict[str, Any] = {}
    findings: list[Finding] = []
    for name, parser in (
        ("checkstyle", parse_checkstyle),
        ("spotless", parse_spotless),
        ("compile", parse_compile),
    ):
        argv = detected[name]
        result = sb.exec(argv, timeout=timeout)
        raw[name] = {
            "cmd": argv,
            "returncode": result["returncode"],
            "timed_out": result.get("timed_out", False),
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
        }
        findings.extend(parser(result.get("stdout", ""), result.get("stderr", "")))

    return {"kind": detected["kind"], "findings": findings, "raw": raw}
