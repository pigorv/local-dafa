from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from darkfactory.agents.builder import run_builder
from darkfactory.agents.builder_supervisor import (
    SUPERVISOR_NAME,
    builder_supervisor_node,
)
from darkfactory.agents.frontend import run_frontend
from darkfactory.agents.tester import run_tester
from darkfactory.state import PipelineState
from darkfactory.tools.git_diff import (
    compute_wp_diff,
    reconcile_paths,
    snapshot_head,
)
from darkfactory.tools.sandbox import RepoSandbox
from darkfactory.tools.shell import get_sandbox, register_sandbox

log = logging.getLogger(__name__)

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
    """Extract any patches the result dict carried (test-only fallback).

    Real runs source patches from ``compute_wp_diff`` against the
    sandbox; this fallback exists so unit-test fixtures can drive patch
    shape via the result dict without standing up a git tree.
    """
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


def _builder_reconciliation_findings(
    *,
    wp_id: str,
    status: str,
    edits: list[dict[str, Any]],
    blockers: list[str],
    claimed_paths: list[str],
    actual_paths: list[str],
) -> list[dict[str, Any]]:
    """Derive reconciliation_findings entries from a Builder turn.

    PR B reconciliation: compares the Builder's declared edit paths
    against the ground-truth ``git diff`` paths. Status-driven failures
    (``blocked`` / no work) and path-set discrepancies
    (``claimed_edits_not_applied`` / ``undeclared_edits``) both surface
    here under explicit kinds so the Verifier can attribute them.
    """
    out: list[dict[str, Any]] = []
    if status == "blocked":
        detail = (
            "; ".join(b for b in blockers if b)
            if blockers
            else "Builder reported status=blocked with no reason"
        )
        out.append(
            {
                "kind": "builder_blocked",
                "wp_id": wp_id,
                "producer": "build_subgraph",
                "detail": detail,
                "blockers": list(blockers),
            }
        )
        return out
    if status == "no_changes_needed":
        log.info(
            "builder reported no_changes_needed for slice %r", wp_id,
        )
        return out
    # status == "done": reconcile declared edits against ground-truth paths.
    if not actual_paths and not edits:
        out.append(
            {
                "kind": "builder_no_action",
                "wp_id": wp_id,
                "producer": "build_subgraph",
                "detail": (
                    "Builder reported status=done but declared no edits and "
                    "no files changed in the working tree"
                ),
            }
        )
        return out
    paths_recon = reconcile_paths(claimed_paths, actual_paths)
    missing = paths_recon["claimed_not_applied"]
    extra = paths_recon["undeclared"]
    if missing:
        out.append(
            {
                "kind": "claimed_edits_not_applied",
                "wp_id": wp_id,
                "producer": "build_subgraph",
                "detail": (
                    f"Builder declared {len(missing)} edit(s) that were not "
                    "applied to the working tree"
                ),
                "claimed_paths": missing,
                "actual_paths": list(actual_paths),
            }
        )
    if extra:
        out.append(
            {
                "kind": "undeclared_edits",
                "wp_id": wp_id,
                "producer": "build_subgraph",
                "detail": (
                    f"Builder applied {len(extra)} edit(s) it did not "
                    "declare in its structured output"
                ),
                "claimed_paths": list(claimed_paths),
                "actual_paths": extra,
            }
        )
    return out


def _resolve_task_id(state: PipelineState) -> str:
    return str(
        state.get("task_id")
        or state.get("wf_id")
        or state.get("workflow_id")
        or ""
    )


def _worker_node_factory(name: str):
    runner = WORKER_RUNNERS[name]

    async def worker_node(state: PipelineState) -> dict:
        slice_id = state.get("current_slice") or ""

        # Snapshot HEAD before the Builder / Tester runs so we can compute
        # the ground-truth diff afterwards. Frontend stays on the legacy
        # sentinel path (it's a no-op stub and not worth its own channel).
        task_id = _resolve_task_id(state)
        sandbox = get_sandbox(task_id) if task_id else None
        pre_sha = (
            snapshot_head(sandbox) if name in ("builder", "tester") else ""
        )

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

        # Builder / Tester patches come from `git diff` in PR B/C. In tests
        # that run without a registered sandbox (no pre_sha, no sandbox),
        # fall back to the result-dict shape so fixtures can drive patch
        # contents directly without standing up a real git tree.
        if name in ("builder", "tester") and pre_sha and sandbox is not None:
            patches = compute_wp_diff(
                sandbox, pre_sha, role=name, slice_id=slice_id
            )
        else:
            patches = _patches_from_result(result)

        coverage_entries = _coverage_entries_from_result(result)
        delta: dict[str, Any] = {}

        # Sentinel-completion is per-worker. Builder + Tester no longer
        # emit the ``(worker-completion)`` patch — the supervisor advances
        # on ``builder_outputs`` / ``tester_outputs`` entries instead.
        # Frontend keeps the sentinel since it doesn't have its own channel.
        if patches:
            delta["patches"] = patches
        elif name == "frontend":
            delta["patches"] = [
                {
                    "path": "(worker-completion)",
                    "diff": "",
                    "author_agent": name,
                    "slice_id": slice_id,
                }
            ]

        # Builder-only plumbing: record the structured BuilderOutput on its
        # own state channel, expose the summary so the Tester can read it,
        # and route any status-driven failures through reconciliation_findings
        # (never through tester_findings — that channel belongs to the
        # Tester agent's own declarations).
        if name == "builder" and isinstance(result, dict):
            wp_id = str(result.get("wp_id") or slice_id)
            status = str(result.get("status") or "blocked")
            edits = [
                dict(edit) for edit in (result.get("edits") or [])
                if isinstance(edit, dict)
            ]
            blockers = [
                str(reason) for reason in (result.get("blockers") or [])
            ]
            summary = str(result.get("summary") or "").strip()
            if summary:
                delta["builder_summary"] = summary
            delta["builder_outputs"] = [
                {
                    "wp_id": wp_id,
                    "status": status,
                    "edits": edits,
                    "blockers": blockers,
                    "summary": summary,
                }
            ]
            claimed_paths = [
                str(edit.get("path") or "") for edit in edits
                if edit.get("path")
            ]
            actual_paths = [
                str(p.get("path") or "") for p in patches if p.get("path")
            ]
            recon = _builder_reconciliation_findings(
                wp_id=wp_id,
                status=status,
                edits=edits,
                blockers=blockers,
                claimed_paths=claimed_paths,
                actual_paths=actual_paths,
            )
            if recon:
                delta["reconciliation_findings"] = recon

        # Tester-only plumbing: record the TesterOutput on its own channel,
        # fold the Tester's declared coverage and findings into their
        # respective channels (these are exclusively Tester-produced now),
        # and route parse failure through reconciliation_findings rather
        # than masquerading as a Tester finding.
        if name == "tester" and isinstance(result, dict):
            tester_findings = list(_tester_findings_from_result(result))
            tester_summary = str(result.get("summary") or "").strip()
            parse_failure = bool(result.get("parse_failure"))
            delta["tester_outputs"] = [
                {
                    "wp_id": slice_id,
                    "summary": tester_summary,
                    "coverage": list(coverage_entries),
                    "findings": list(tester_findings),
                    "parse_failure": parse_failure,
                }
            ]
            if tester_findings:
                delta["tester_findings"] = tester_findings
            if parse_failure:
                delta.setdefault("reconciliation_findings", []).append(
                    {
                        "kind": "tester_parse_failure",
                        "wp_id": slice_id,
                        "producer": "build_subgraph",
                        "detail": (
                            "Tester produced no structured output for this "
                            "Work Package; treat predicates as uncovered."
                        ),
                    }
                )

        if coverage_entries:
            delta["coverage_entries"] = coverage_entries
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
