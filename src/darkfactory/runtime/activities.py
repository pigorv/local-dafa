from __future__ import annotations

from functools import wraps
import os
from typing import Any, Awaitable, Callable, TypeVar

import docker
from docker.errors import NotFound
from opentelemetry import trace
from temporalio import activity

from darkfactory.tools.sandbox import RepoSandbox
from darkfactory.tools.shell import get_sandbox, register_sandbox

WORKER_IMAGE = "darkfactory-worker:latest"
WORKER_NETWORK = "darkfactory-net"
DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
DEFAULT_OTLP_ENDPOINT = "http://otel-collector:4317"

F = TypeVar("F", bound=Callable[..., Awaitable[dict]])


def _worker_container_name(wf_id: str) -> str:
    return f"darkfactory-worker-{wf_id}"


def _worker_temporal_address() -> str:
    address = os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)
    if address.startswith("localhost:"):
        return address.replace("localhost:", "host.docker.internal:", 1)
    if address.startswith("127.0.0.1:"):
        return address.replace("127.0.0.1:", "host.docker.internal:", 1)
    return address


def _heartbeat(detail: str) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


def _stamp_temporal_activity_attrs() -> None:
    """Stamp Temporal + Langfuse attributes on the current activity span.

    Reads `temporalio.activity.info()` — only valid inside an `@activity.defn`
    body. Sets `langfuse.session.id` (so all spans from one workflow group as
    one Langfuse trace) plus the standard `temporal.*` attributes that make
    activity spans searchable in Langfuse and roll up under the workflow root.
    """
    if not activity.in_activity():
        return
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return
    info = activity.info()
    span.set_attribute("langfuse.session.id", info.workflow_id)
    span.set_attribute("session.id", info.workflow_id)
    span.set_attribute("temporal.workflow.id", info.workflow_id)
    span.set_attribute("temporal.workflow.run_id", info.workflow_run_id)
    span.set_attribute("temporal.workflow.type", info.workflow_type)
    span.set_attribute("temporal.task_queue", info.task_queue)
    span.set_attribute("temporal.activity.type", info.activity_type)
    span.set_attribute("temporal.activity.attempt", info.attempt)


def _runctx_from_state(state: dict) -> Any:
    """Reconstruct a `RunContext` from fields the workflow embeds in the state slice.

    Stage subgraphs (`build_subgraph`, `verify_subgraph`) read sandbox / repo
    fields off `runtime.context`. Temporal serialises plain dicts between
    workflow and worker, so the workflow embeds these fields into the state
    slice and the activity body re-hydrates them here. Defaults match the
    `setup_worker_activity` bind-mount layout (`/workspace`).
    """
    from darkfactory.state import RunContext

    return RunContext(
        repo_path=state.get("repo_path") or "/workspace",
        repo_url=state.get("repo_url"),
        base_branch=state.get("base_branch") or "main",
        feature_branch=state.get("feature_branch") or "",
        task_id=state.get("task_id") or state.get("wf_id") or "darkfactory",
        allow_auto_merge=bool(state.get("allow_auto_merge", False)),
        model_profile=state.get("model_profile") or "claude",
    )


def _branch_from_state(state: dict, expected_branch: str) -> str:
    if state.get("feature_branch"):
        return state["feature_branch"]
    if state.get("wf_id"):
        return f"agent/{state['wf_id']}"
    if state.get("task_id"):
        return f"agent/{state['task_id']}"
    return expected_branch.format(**state)


def _repo_task_id(state: dict) -> str | None:
    return state.get("task_id") or state.get("wf_id")


def _ensure_repo_sandbox(state: dict):
    task_id = _repo_task_id(state)
    repo_path = state.get("repo_path") or "/workspace"
    if task_id is None:
        return None

    sb = get_sandbox(task_id)
    if sb is None:
        register_sandbox(task_id, RepoSandbox(repo_path=repo_path))
        sb = get_sandbox(task_id)
    return sb


def _checkout_or_create_branch(sb: Any, branch: str) -> None:
    result = sb.exec(["git", "checkout", branch])
    if int(result.get("returncode", 1)) != 0:
        sb.exec(["git", "checkout", "-b", branch])


def with_repo_state(expected_branch: str) -> Callable[[F], F]:
    """Ensure a repo-touching activity runs on the workflow feature branch."""

    def decorator(fn: F) -> F:
        @wraps(fn)
        async def wrapper(state: dict, *args: Any, **kwargs: Any) -> dict:
            sb = _ensure_repo_sandbox(state)
            if sb is not None:
                _checkout_or_create_branch(sb, _branch_from_state(state, expected_branch))
            return await fn(state, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def _container_exec_returncode(result: Any) -> int:
    if hasattr(result, "exit_code"):
        return int(result.exit_code)
    return int(result[0])


def _init_worker_branch(container: Any, wf_id: str) -> None:
    branch = f"agent/{wf_id}"
    checkout = container.exec_run(["git", "checkout", branch], workdir="/workspace")
    if _container_exec_returncode(checkout) != 0:
        container.exec_run(["git", "checkout", "-b", branch], workdir="/workspace")


@activity.defn
async def ping_activity(msg: str) -> str:
    _stamp_temporal_activity_attrs()
    return msg


@activity.defn
async def setup_worker_activity(wf_id: str, repo_url: str) -> str:
    _stamp_temporal_activity_attrs()
    client = docker.from_env()
    name = _worker_container_name(wf_id)
    try:
        container = client.containers.get(name)
        _init_worker_branch(container, wf_id)
        return name
    except NotFound:
        pass

    container = client.containers.run(
        image=WORKER_IMAGE,
        name=name,
        detach=True,
        network=WORKER_NETWORK,
        environment={
            "TEMPORAL_ADDRESS": _worker_temporal_address(),
            "TEMPORAL_TASK_QUEUE": f"agent-tq-{wf_id}",
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
            "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get(
                "OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT
            ),
        },
        volumes={repo_url: {"bind": "/workspace", "mode": "rw"}},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        user="1000:1000",
        mem_limit="2g",
        pids_limit=256,
        tmpfs={"/tmp": "size=512m"},
    )
    _init_worker_branch(container, wf_id)
    return name


@activity.defn
async def teardown_worker_activity(wf_id: str) -> None:
    _stamp_temporal_activity_attrs()
    client = docker.from_env()
    name = _worker_container_name(wf_id)
    try:
        container = client.containers.get(name)
    except NotFound:
        return
    container.remove(force=True)


@activity.defn
@with_repo_state("agent/{wf_id}")
async def hydrate_stage(state: dict) -> dict:
    """Run the hydrator on the bind-mounted repo and return `repo_context`.

    Pure-Python (no LLM, no Docker exec); cheap enough to run inline rather
    than via a one-node subgraph.
    """
    _stamp_temporal_activity_attrs()
    _heartbeat("hydrate: starting")
    from darkfactory.stages.hydrator import hydrate

    repo_path = state.get("repo_path")
    if not repo_path:
        existing = state.get("repo_context") or {}
        if isinstance(existing, dict):
            repo_path = existing.get("repo_root")
    if not repo_path:
        repo_path = "/workspace"
    return {"repo_context": hydrate(repo_path)}


@activity.defn
async def discovery_stage(state: dict) -> dict:
    """Run the Discovery subgraph (PO → Architect → SpecReviewer)."""
    _stamp_temporal_activity_attrs()
    _heartbeat("discovery: starting subgraph")
    from darkfactory.stages.discovery import discovery_subgraph

    sg = discovery_subgraph()
    result = await sg.ainvoke(state)
    return {
        "stories": result.get("stories", []),
        "spec": result.get("spec", []),
        "review_decision": result.get("review_decision"),
    }


@activity.defn
@with_repo_state("agent/{wf_id}")
async def build_stage(state: dict) -> dict:
    """Run the Build subgraph (Builder Supervisor + worker fan-out)."""
    _stamp_temporal_activity_attrs()
    _heartbeat("build: starting subgraph")
    from darkfactory.stages.build import build_subgraph

    ctx = _runctx_from_state(state)
    sg = build_subgraph()
    result = await sg.ainvoke(state, context=ctx)
    return {
        "build_order": result.get("build_order"),
        "current_slice": result.get("current_slice"),
        "patches": result.get("patches", []),
    }


@activity.defn
@with_repo_state("agent/{wf_id}")
async def verify_stage(state: dict) -> dict:
    """Run the Verify subgraph (parallel tests + linters + compile via Send fan-out).

    The subgraph itself runs `mvn test` / linters / compile through
    `RepoSandbox.exec` and parses with `tools/tests.py` + `tools/linters.py`;
    no LangChain agent wraps the verify stage. The deferred aggregator
    inside the subgraph collapses the fan-out into a single `VerifySummary`.
    """
    _stamp_temporal_activity_attrs()
    _heartbeat("verify: starting subgraph (Send fan-out)")
    from darkfactory.stages.verify import verify_subgraph

    ctx = _runctx_from_state(state)
    sg = verify_subgraph()
    result = await sg.ainvoke(state, context=ctx)
    delta: dict[str, Any] = {
        "test_results": result.get("test_results", []),
        "findings": result.get("findings", []),
        "verify_summary": result.get("verify_summary"),
    }
    if "verify_retries" in result:
        delta["verify_retries"] = result["verify_retries"]
    return delta


@activity.defn
async def spec_adjustment_stage(state: dict) -> dict:
    """Decide patch_code vs update_spec on a verify failure (R12)."""
    _stamp_temporal_activity_attrs()
    _heartbeat("spec_adjustment: starting")
    from darkfactory.agents.spec_adjustment import run_spec_adjustment

    out = await run_spec_adjustment(state)
    return _spec_adjustment_delta(out)


def _spec_adjustment_delta(out: Any) -> dict:
    """Translate a `SpecAdjustmentOutput` into a workflow-mergeable state delta."""
    from darkfactory.state import Patch

    if out.decision == "patch_code":
        if not (out.target_worker and out.slice_id and out.path and out.diff):
            raise ValueError(
                "patch_code decision missing required fields "
                "(target_worker, slice_id, path, diff)"
            )
        patch = Patch(
            path=out.path,
            diff=out.diff,
            author_agent="spec_adjustment",
            slice_id=out.slice_id,
        )
        return {"patches": [patch], "current_slice": out.slice_id}

    if out.decision == "update_spec":
        if out.updated_slice is None:
            raise ValueError("update_spec decision missing updated_slice")
        slice_dict = out.updated_slice.model_dump()
        return {
            "spec": [slice_dict],
            "current_slice": slice_dict["story_id"],
        }

    raise ValueError(f"unknown decision: {out.decision!r}")


@activity.defn
async def code_quality_stage(state: dict) -> dict:
    """Run the Code Quality reviewer and surface its gate summary."""
    _stamp_temporal_activity_attrs()
    _heartbeat("code_quality: starting")
    from darkfactory.agents.code_quality import run_code_quality

    result = await run_code_quality(state)
    return {"review_decision": result.model_dump()}


@activity.defn
@with_repo_state("agent/{wf_id}")
async def pr_creator_stage(state: dict) -> dict:
    """Run the PR Creator role and return the workflow's `pr_url` channel."""
    _stamp_temporal_activity_attrs()
    _heartbeat("pr_creator: starting")
    from darkfactory.agents.pr_creator import run_pr_creator

    return {"pr_url": await run_pr_creator(state)}


@activity.defn
@with_repo_state("agent/{wf_id}")
async def merge_branch(state: dict) -> dict:
    """Merge the approved pull request without involving an LLM."""
    _stamp_temporal_activity_attrs()
    _heartbeat("merge_branch: starting")
    pr_url = state.get("pr_url")
    if not pr_url:
        raise ValueError("merge_branch requires state['pr_url']")

    sb = _ensure_repo_sandbox(state)
    if sb is None:
        raise ValueError("merge_branch requires state['task_id'] or state['wf_id']")

    result = sb.exec(["gh", "pr", "merge", pr_url, "--squash", "--delete-branch"])
    if int(result.get("returncode", 1)) != 0:
        raise RuntimeError(
            "gh pr merge failed "
            f"(rc={result.get('returncode')}, "
            f"stdout={result.get('stdout', '')!r}, "
            f"stderr={result.get('stderr', '')!r})"
        )
    return {"merged": True}


STAGE_ACTIVITIES: tuple = (
    hydrate_stage,
    discovery_stage,
    build_stage,
    verify_stage,
    spec_adjustment_stage,
    code_quality_stage,
    pr_creator_stage,
    merge_branch,
)
