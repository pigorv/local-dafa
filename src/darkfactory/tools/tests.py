"""run_tests tool: detect build system, execute test suite in the sandbox.

Detection is host-side (from RunContext.repo_path) because the sandbox
mounts that directory at /workspace. Execution happens inside the sandbox
via the per-task RepoSandbox registry.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from langgraph.runtime import get_runtime

from darkfactory.state import RunContext
from darkfactory.tools.shell import get_sandbox


def detect_project(repo_path: str | Path) -> dict[str, Any]:
    """Return {kind, cmd} or {error}. Kinds: maven, gradle, gradle-wrapper, npm."""
    root = Path(repo_path)
    if (root / "pom.xml").exists():
        return {"kind": "maven", "cmd": ["mvn", "-q", "-B", "test"]}
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        if (root / "gradlew").exists():
            return {"kind": "gradle-wrapper", "cmd": ["./gradlew", "--console=plain", "test"]}
        return {"kind": "gradle", "cmd": ["gradle", "--console=plain", "test"]}
    if (root / "package.json").exists():
        return {"kind": "npm", "cmd": ["npm", "test", "--silent"]}
    return {"error": "cannot detect project type"}


_MAVEN_SUMMARY_RE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
)
_GRADLE_SUMMARY_RE = re.compile(
    r"(\d+)\s+tests? completed(?:,\s*(\d+)\s+failed)?(?:,\s*(\d+)\s+skipped)?",
    re.IGNORECASE,
)


def parse_maven(stdout: str, stderr: str) -> dict[str, int] | None:
    """Sum the final Surefire summary lines, if present."""
    text = stdout + "\n" + stderr
    matches = list(_MAVEN_SUMMARY_RE.finditer(text))
    if not matches:
        return None
    run = failures = errors = skipped = 0
    for m in matches:
        r, f, e, s = (int(x) for x in m.groups())
        run += r
        failures += f
        errors += e
        skipped += s
    return {
        "passed": run - failures - errors - skipped,
        "failed": failures + errors,
        "skipped": skipped,
        "total": run,
    }


def parse_gradle(stdout: str, stderr: str) -> dict[str, int] | None:
    text = stdout + "\n" + stderr
    m = _GRADLE_SUMMARY_RE.search(text)
    if not m:
        return None
    total = int(m.group(1))
    failed = int(m.group(2) or 0)
    skipped = int(m.group(3) or 0)
    return {
        "passed": total - failed - skipped,
        "failed": failed,
        "skipped": skipped,
        "total": total,
    }


@tool
def run_tests(timeout: int = 600) -> dict[str, Any]:
    """Detect the project's build system and run its test suite in the sandbox.

    Returns a dict with returncode, kind, cmd, and (when parseable)
    passed/failed/skipped/total counts.
    """
    rt = get_runtime(RunContext)
    ctx = rt.context
    detected = detect_project(ctx.repo_path)
    if "error" in detected:
        return detected

    sb = get_sandbox(ctx.task_id)
    if sb is None:
        raise RuntimeError(
            f"no sandbox registered for task_id={ctx.task_id!r}; "
            "the activity must register a RepoSandbox before run_tests"
        )

    result = sb.exec(detected["cmd"], timeout=timeout)
    out = result.get("stdout", "")
    err = result.get("stderr", "")

    summary: dict[str, int] | None = None
    kind = detected["kind"]
    if kind == "maven":
        summary = parse_maven(out, err)
    elif kind in ("gradle", "gradle-wrapper"):
        summary = parse_gradle(out, err)

    payload: dict[str, Any] = {
        "kind": kind,
        "cmd": detected["cmd"],
        "returncode": result["returncode"],
        "timed_out": result.get("timed_out", False),
        "stdout": out,
        "stderr": err,
    }
    if summary is not None:
        payload.update(summary)
    return payload
