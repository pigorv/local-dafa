from __future__ import annotations

from typing import Any, Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from darkfactory.agents._sdk_common import WorkerOutput
from darkfactory.agents.builder import run_builder
from darkfactory.agents.builder_supervisor import (
    SUPERVISOR_NAME,
    builder_supervisor_node,
)
from darkfactory.agents.frontend import run_frontend
from darkfactory.agents.tester import run_tester
from darkfactory.state import PipelineState
from darkfactory.tools.sandbox import RepoSandbox
from darkfactory.tools.shell import get_sandbox, register_sandbox

WORKER_NAMES = ("builder", "tester", "frontend")
BRANCH_INIT_NAME = "branch_init"

WORKER_RUNNERS: dict[str, Callable[[dict], Awaitable[Any]]] = {
    "builder": run_builder,
    "tester": run_tester,
    "frontend": run_frontend,
}


def _branch_init_node(state: PipelineState, runtime=None) -> dict:
    """Ensure the build runs on a fresh `agent/<task_id>` branch.

    Plain nodes do not run through a Temporal activity setup hook, so this
    node registers the sandbox itself (idempotent by task_id) and checks out
    the agent branch via the sandbox shell. Also seeds a minimal git identity
    so worker `git_commit` calls succeed inside the container.
    """
    ctx = getattr(runtime, "context", None) if runtime is not None else None
    if ctx is None:
        return {}

    if get_sandbox(ctx.task_id) is None:
        register_sandbox(ctx.task_id, RepoSandbox(repo_path=ctx.repo_path))
    sb = get_sandbox(ctx.task_id)
    if sb is None:
        return {}

    sb.exec(["git", "config", "user.name", "darkfactory-agent"])
    sb.exec(["git", "config", "user.email", "agent@darkfactory.local"])

    cur = sb.exec(["git", "branch", "--show-current"])
    branch = (cur.get("stdout") or "").strip()
    desired = ctx.feature_branch or f"agent/{ctx.task_id}"
    if branch == desired or branch.startswith("agent/"):
        return {}

    exists = sb.exec(["git", "rev-parse", "--verify", desired])
    if exists.get("returncode") == 0:
        sb.exec(["git", "checkout", desired])
    else:
        sb.exec(["git", "checkout", "-b", desired])
    return {}


def _patches_from_result(result: Any) -> list[dict]:
    if isinstance(result, WorkerOutput):
        return [dict(p) for p in result.patches]
    if isinstance(result, dict):
        return list(result.get("patches") or [])
    patches = getattr(result, "patches", None)
    if patches is not None:
        return [dict(p) for p in patches]
    return []


def _coverage_entries_from_result(result: Any) -> list[dict]:
    if isinstance(result, dict):
        entries = result.get("coverage_entries")
        if entries is None:
            entries = result.get("coverage")
    else:
        entries = getattr(result, "coverage_entries", None)
        if entries is None:
            entries = getattr(result, "coverage", None)

    out: list[dict] = []
    for entry in entries or []:
        if hasattr(entry, "model_dump"):
            out.append(entry.model_dump())
        elif isinstance(entry, dict):
            out.append(dict(entry))
    return out


def _tester_findings_from_result(result: Any) -> list[dict]:
    if isinstance(result, dict):
        findings = result.get("tester_findings")
        if findings is None:
            findings = result.get("findings")
    else:
        findings = getattr(result, "tester_findings", None)
        if findings is None:
            findings = getattr(result, "findings", None)

    out: list[dict] = []
    for finding in findings or []:
        if hasattr(finding, "model_dump"):
            out.append(finding.model_dump())
        elif isinstance(finding, dict):
            out.append(dict(finding))
    return out


def _worker_node_factory(name: str):
    runner = WORKER_RUNNERS[name]

    async def worker_node(state: PipelineState) -> dict:
        slice_id = state.get("current_slice") or ""
        try:
            result = await runner(state)
        except Exception as exc:  # keep the graph progressing on worker failure
            return {
                "patches": [
                    {
                        "path": "(worker-error)",
                        "diff": f"error: {exc}",
                        "author_agent": name,
                        "slice_id": slice_id,
                    }
                ]
            }

        patches = _patches_from_result(result)
        coverage_entries = _coverage_entries_from_result(result)
        tester_findings = _tester_findings_from_result(result)
        delta: dict[str, Any] = {}
        if patches:
            delta["patches"] = patches
        else:
            # Synthesize a completion marker so the supervisor advances build_order.
            delta["patches"] = [
                {
                    "path": "(worker-completion)",
                    "diff": "",
                    "author_agent": name,
                    "slice_id": slice_id,
                }
            ]
        if coverage_entries:
            delta["coverage_entries"] = coverage_entries
        if tester_findings:
            delta["tester_findings"] = tester_findings
        return delta

    return worker_node


def build_subgraph() -> Any:
    """Build subgraph: supervisor dispatches slices to Builder/Tester.

    Structure: START → branch_init → supervisor → (worker ↔ supervisor)* → END.
    The supervisor returns `Command(goto=<worker>|END)`; each worker edges
    unconditionally back to the supervisor so the next slice gets dispatched
    or the run terminates. Roles are resolved by name and executed as SDK
    clients inside the worker node bodies.
    """
    g = StateGraph(PipelineState)
    g.add_node(BRANCH_INIT_NAME, _branch_init_node)
    g.add_node(
        SUPERVISOR_NAME,
        builder_supervisor_node,
        destinations=(*WORKER_NAMES, END),
    )
    for name in WORKER_NAMES:
        g.add_node(name, _worker_node_factory(name))
        g.add_edge(name, SUPERVISOR_NAME)
    g.add_edge(START, BRANCH_INIT_NAME)
    g.add_edge(BRANCH_INIT_NAME, SUPERVISOR_NAME)
    return g.compile()
