from __future__ import annotations

from functools import wraps
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from typing import Any, Awaitable, Callable, TypeVar

import docker
from docker.errors import NotFound
from opentelemetry import trace
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from darkfactory.bootstrap import set_current_run_id
from darkfactory.runtime.approval import (
    ApprovalSignal,
    is_authorized,
    parse_command,
)
from darkfactory.runtime.comment_templates import render as render_comment_template
from darkfactory.runtime.issue_comments import filter_dark_factory_marker_comments
from darkfactory.runtime.phase_comment import end_marker_for, marker_for
from darkfactory.runtime.tracing import coalesced_trace_id, phase_span
from darkfactory.tools.sandbox import RepoSandbox
from darkfactory.tools.shell import get_sandbox, register_sandbox
from darkfactory.state import IssueComment, IssueRef, IssueRunRequest

DEFAULT_WORKER_IMAGE = "darkfactory-worker:polyglot"
WORKER_IMAGE_ENV = "DARKFACTORY_WORKER_IMAGE"
WORKER_NETWORK = "darkfactory-net"
WORKER_TRANSCRIPTS_VOLUME = os.environ.get(
    "DARKFACTORY_TRANSCRIPTS_VOLUME", "darkfactory_raw-claude"
)
WORKER_CLAUDE_HOME = "/home/agent/.claude"
DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
DEFAULT_OTLP_ENDPOINT = "http://otel-collector:4317"
DEFAULT_TEMPORAL_NAMESPACE = "default"
ISSUE_COMMENT_TIMEOUT_S = 60
ISSUE_LIST_TIMEOUT_S = 60
ISSUE_QUARANTINE_TIMEOUT_S = 60
ISSUE_LIST_JSON_FIELDS = "number,title,body,labels,updatedAt,comments"
ISSUE_QUARANTINE_VIEW_FIELDS = "comments,labels"
DF_READY_LABEL = "df:ready"
DF_TRIAGING_LABEL = "df:triaging"
DF_NEEDS_CLARIFICATION_LABEL = "df:needs-clarification"
DF_DESIGNING_LABEL = "df:designing"
DF_AWAITING_APPROVAL_LABEL = "df:awaiting-approval"
DF_APPROVED_LABEL = "df:approved"
DF_BUILDING_LABEL = "df:building"
DF_VERIFYING_LABEL = "df:verifying"
DF_REVIEWING_LABEL = "df:reviewing"
DF_AWAITING_MERGE_LABEL = "df:awaiting-merge"
DF_FIXING_LABEL = "df:fixing"
DF_IN_PROGRESS_LABEL = "df:in-progress"
DF_DONE_LABEL = "df:done"
DF_NEEDS_HUMAN_LABEL = "df:needs-human"
DF_CANCEL_LABEL = "df:cancel"
DF_CANCELED_LABEL = "df:canceled"
DF_FAILED_LABEL = "df:failed"
DEFAULT_ISSUE_LIST_LIMIT = 100
DEFAULT_WATCH_MAX_CONCURRENT = 3
ISSUE_WORKFLOW_ID_PREFIX = "df-issue-"
ISSUE_WORKFLOW_RUN_INFIX = "-run-"
AGENT_TASK_QUEUE_PREFIX = "agent-tq-"
SUPERVISOR_TASK_QUEUE = "supervisor-tq"
WORKER_CONTAINER_NAME_LIMIT = 200
RESOLVE_LATEST_RUN_PAGE_CAP = 50
DF_POLL_LABELS = (
    DF_READY_LABEL,
    DF_TRIAGING_LABEL,
    DF_NEEDS_CLARIFICATION_LABEL,
    DF_DESIGNING_LABEL,
    DF_AWAITING_APPROVAL_LABEL,
    DF_APPROVED_LABEL,
    DF_BUILDING_LABEL,
    DF_VERIFYING_LABEL,
    DF_REVIEWING_LABEL,
    DF_AWAITING_MERGE_LABEL,
    DF_FIXING_LABEL,
    DF_IN_PROGRESS_LABEL,
    DF_CANCEL_LABEL,
)


def _worker_image() -> str:
    """Return the per-workflow worker image, allowing deploy-time overrides."""
    configured = os.environ.get(WORKER_IMAGE_ENV, "").strip()
    return configured or DEFAULT_WORKER_IMAGE


DF_STATE_LABELS = (
    DF_READY_LABEL,
    DF_TRIAGING_LABEL,
    DF_NEEDS_CLARIFICATION_LABEL,
    DF_DESIGNING_LABEL,
    DF_AWAITING_APPROVAL_LABEL,
    DF_BUILDING_LABEL,
    DF_VERIFYING_LABEL,
    DF_REVIEWING_LABEL,
    DF_AWAITING_MERGE_LABEL,
    DF_FIXING_LABEL,
    DF_IN_PROGRESS_LABEL,
    DF_DONE_LABEL,
    DF_NEEDS_HUMAN_LABEL,
    DF_CANCELED_LABEL,
    DF_FAILED_LABEL,
)

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[dict]])


def _truncate_with_hash(value: str, limit: int) -> str:
    """Return value unchanged if within limit, else a stable truncation.

    Long workflow IDs (per-attempt format includes owner / repo / issue
    number / run number) can produce container names and branch names that
    exceed Docker / Git practical limits. Keep the leading prefix readable
    and append an 8-char SHA1 of the original value so the truncation is
    deterministic and collision-resistant.
    """
    if len(value) <= limit:
        return value
    suffix = "-" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    head_len = max(1, limit - len(suffix))
    return value[:head_len] + suffix


def _worker_container_name(wf_id: str) -> str:
    return _truncate_with_hash(
        f"darkfactory-worker-{wf_id}", WORKER_CONTAINER_NAME_LIMIT
    )


def _worker_temporal_address() -> str:
    address = os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)
    if address.startswith("localhost:"):
        return address.replace("localhost:", "host.docker.internal:", 1)
    if address.startswith("127.0.0.1:"):
        return address.replace("127.0.0.1:", "host.docker.internal:", 1)
    return address


def _temporal_namespace() -> str:
    if activity.in_activity():
        info = activity.info()
        if info.workflow_namespace:
            return info.workflow_namespace
    return os.environ.get("TEMPORAL_NAMESPACE", DEFAULT_TEMPORAL_NAMESPACE)


async def _connect_temporal_client() -> Client:
    return await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS),
        namespace=_temporal_namespace(),
        data_converter=pydantic_data_converter,
    )


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
    info = activity.info()
    # Bind the run id for this activity's async context BEFORE the span
    # recording check, so non-recording-parent child spans still get it.
    set_current_run_id(info.workflow_run_id)
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return
    span.set_attribute("langfuse.session.id", info.workflow_id)
    span.set_attribute("session.id", info.workflow_id)
    span.set_attribute("temporal.workflow.id", info.workflow_id)
    span.set_attribute("temporal.workflow.run_id", info.workflow_run_id)
    span.set_attribute("temporal.workflow.type", info.workflow_type)
    span.set_attribute("temporal.task_queue", info.task_queue)
    span.set_attribute("temporal.activity.type", info.activity_type)
    span.set_attribute("temporal.activity.attempt", info.attempt)


def _coalesced_trace_id(workflow_id: str, workflow_run_id: str | None = None) -> str:
    return coalesced_trace_id(workflow_id, workflow_run_id)


def _langfuse_client():
    """Langfuse v4 client, or None when scoring is disabled/unconfigured.

    Scoring is observational: missing creds or an unreachable Langfuse must
    never break the workflow. v4 `get_client()` is itself a process singleton
    and reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST from env.
    """
    if os.environ.get("LANGFUSE_SCORES_ENABLED", "true").lower() == "false":
        return None
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import get_client

        return get_client()
    except Exception:
        log.warning("langfuse client init failed; scoring disabled", exc_info=True)
        return None


def _emit_langfuse_scores(
    stage: str,
    scores: dict[str, float | int],
    comment: str | None = None,
) -> None:
    """Attach numeric scores to this workflow's coalesced Langfuse trace.

    No-op outside an activity body or when Langfuse is disabled/unreachable.
    Never raises - scoring must not change workflow outcomes. Flushes once at
    the end because the worker container is torn down in the workflow `finally`,
    so batched scores would otherwise be lost.
    """
    if not activity.in_activity():
        return
    client = _langfuse_client()
    if client is None:
        return
    info = activity.info()
    trace_id = _coalesced_trace_id(info.workflow_id, info.workflow_run_id)
    for name, value in scores.items():
        try:
            client.create_score(
                name=f"{stage}.{name}",
                value=float(value),
                trace_id=trace_id,
                data_type="NUMERIC",
                comment=comment,
            )
        except Exception:
            log.warning("langfuse score emit failed for %s.%s", stage, name, exc_info=True)
    try:
        client.flush()
    except Exception:
        log.warning("langfuse flush failed after %s scores", stage, exc_info=True)


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


def _state_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _github_cli_env(config_dir: str) -> dict[str, str]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "list_ready_issues_activity requires GH_TOKEN or GITHUB_TOKEN in "
            "the orchestrator runtime"
        )
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    env["GH_CONFIG_DIR"] = config_dir
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def _gh_issue_list_argv(repo: str, label: str, limit: int) -> list[str]:
    if not repo or "/" not in repo:
        raise ValueError("list_ready_issues_activity requires repo as owner/name")
    if not label:
        raise ValueError("list_ready_issues_activity requires a label")
    if limit < 1:
        raise ValueError("list_ready_issues_activity requires limit >= 1")
    return [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--label",
        label,
        "--limit",
        str(limit),
        "--json",
        ISSUE_LIST_JSON_FIELDS,
    ]


def _poll_labels(label: str) -> tuple[str, ...]:
    if label == DF_READY_LABEL:
        return DF_POLL_LABELS
    return (label,)


def _normalise_label_names(labels: Any) -> list[str]:
    names: list[str] = []
    for label in labels or []:
        if isinstance(label, str):
            name = label
        elif isinstance(label, dict):
            name = label.get("name") or ""
        else:
            name = getattr(label, "name", "")
        if name:
            names.append(str(name))
    return names


def _comment_author_name(author: Any) -> str:
    if isinstance(author, str):
        return author
    if isinstance(author, dict):
        return str(author.get("login") or author.get("name") or "")
    return str(getattr(author, "login", None) or getattr(author, "name", "") or "")


_COMMENT_URL_ID_RE = re.compile(r"#issuecomment-(\d+)\b")


def _comment_int_id(comment: Any) -> int:
    raw = _state_value(comment, "databaseId", None) or _state_value(comment, "id", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        # `gh issue list --json comments` returns the GraphQL global id (e.g.
        # `IC_kwDOSVokaM8…`), which can't be coerced. Recover the numeric id
        # from the comment URL when available.
        match = _COMMENT_URL_ID_RE.search(str(_state_value(comment, "url", "") or ""))
        if match:
            return int(match.group(1))
        return 0


def _normalise_polled_comment(comment: Any) -> dict[str, Any]:
    return {
        "id": _comment_int_id(comment),
        "author": _comment_author_name(_state_value(comment, "author", "")),
        "body": str(_state_value(comment, "body", "") or ""),
        "created_at": str(
            _state_value(comment, "created_at", None)
            or _state_value(comment, "createdAt", "")
            or ""
        ),
    }


def _issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _repo_clone_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _watch_max_concurrent() -> int:
    raw = os.environ.get("DF_WATCH_MAX_CONCURRENT", str(DEFAULT_WATCH_MAX_CONCURRENT))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("DF_WATCH_MAX_CONCURRENT must be an integer") from exc
    if value < 1:
        raise ValueError("DF_WATCH_MAX_CONCURRENT must be >= 1")
    return value


def _closed_workflow_reason(status: WorkflowExecutionStatus | None) -> str:
    return f"closed:{status.name.lower() if status else 'unknown'}"


def _normalise_ready_issue(repo: str, issue: dict[str, Any]) -> dict[str, Any]:
    number = int(issue.get("number") or 0)
    raw_comments = issue.get("comments") or []
    comments = raw_comments if isinstance(raw_comments, list) else []
    return {
        "issue": {
            "repo": repo,
            "number": number,
            "url": issue.get("url") or _issue_url(repo, number),
            "title": issue.get("title") or "",
            "body": issue.get("body") or "",
            "labels": _normalise_label_names(issue.get("labels")),
        },
        "updatedAt": issue.get("updatedAt") or "",
        "comments": [
            _normalise_polled_comment(comment)
            for comment in comments
        ],
        "comments_count": (
            int(raw_comments)
            if isinstance(raw_comments, int)
            else len(comments)
        ),
    }


def _issue_comment_target(issue: Any) -> tuple[str, int]:
    repo = _state_value(issue, "repo")
    number = _state_value(issue, "number")
    if not repo:
        raise ValueError("post_issue_comment_activity requires issue.repo")
    try:
        issue_number = int(number)
    except (TypeError, ValueError) as exc:
        raise ValueError("post_issue_comment_activity requires issue.number") from exc
    return str(repo), issue_number


def _issue_ref_from_poll_record(repo: str, issue: Any) -> IssueRef:
    raw_number = _state_value(issue, "number", 0)
    try:
        number = int(raw_number)
    except (TypeError, ValueError) as exc:
        raise ValueError("polled issue requires issue.number") from exc
    if number < 1:
        raise ValueError("polled issue requires issue.number >= 1")
    issue_repo = str(_state_value(issue, "repo", "") or repo)
    return IssueRef(
        repo=issue_repo,
        number=number,
        url=str(_state_value(issue, "url", "") or _issue_url(issue_repo, number)),
        title=str(_state_value(issue, "title", "") or ""),
        body=str(_state_value(issue, "body", "") or ""),
        labels=_normalise_label_names(_state_value(issue, "labels", [])),
    )


def _issue_run_request_from_poll_record(repo: str, issue: Any) -> IssueRunRequest:
    issue_ref = _issue_ref_from_poll_record(repo, issue)
    return IssueRunRequest(
        repo_url=str(_state_value(issue, "repo_url", "") or _repo_clone_url(repo)),
        repo_path=str(_state_value(issue, "repo_path", "") or "/workspace"),
        issue=issue_ref,
        model_profile=os.environ.get("LLM_MODEL_PROFILE"),
    )


def _issue_comments_from_poll_record(comments: Any) -> list[IssueComment]:
    if not isinstance(comments, list):
        return []
    normalised: list[IssueComment] = []
    for comment in filter_dark_factory_marker_comments(comments):
        try:
            comment_id = int(_state_value(comment, "id", 0) or 0)
        except (TypeError, ValueError):
            comment_id = 0
        normalised.append(
            IssueComment(
                id=comment_id,
                author=_comment_author_name(_state_value(comment, "author", "")),
                body=str(_state_value(comment, "body", "") or ""),
                created_at=str(
                    _state_value(comment, "created_at", None)
                    or _state_value(comment, "createdAt", "")
                    or ""
                ),
            )
        )
    return normalised


def _last_seen_comment_id(summary: Any) -> int:
    raw = _state_value(summary, "last_seen_comment_id", 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _comments_since_last_seen(
    comments: list[IssueComment],
    last_seen_comment_id: int,
) -> list[IssueComment]:
    return [
        comment
        for comment in comments
        if comment.id > last_seen_comment_id
    ]


def _clarification_questions(value: list[str] | str) -> list[str]:
    raw_questions = [value] if isinstance(value, str) else list(value or [])
    questions = [str(question).strip() for question in raw_questions]
    return [question for question in questions if question]


def render_clarification_comment_body(
    issue: Any,
    clarification_questions: list[str] | str,
    workflow_id: str,
    clarification_round: int = 1,
) -> str:
    """Render the GitHub issue comment used for triage clarification."""
    _, issue_number = _issue_comment_target(issue)
    questions = _clarification_questions(clarification_questions)
    if not questions:
        raise ValueError(
            "post_issue_comment_activity requires at least one clarification question"
        )
    if not workflow_id:
        raise ValueError("post_issue_comment_activity requires workflow_id")
    try:
        round_number = int(clarification_round)
    except (TypeError, ValueError) as exc:
        raise ValueError("clarification_round must be an integer") from exc
    if round_number < 1:
        raise ValueError("clarification_round must be >= 1")

    return render_comment_template(
        "clarification.md.j2",
        marker=f"<!-- df-clarify:{workflow_id}:{round_number} -->",
        issue_number=issue_number,
        questions=questions,
        workflow_id=workflow_id,
    )


def render_needs_human_comment_body(
    issue: Any,
    workflow_id: str,
    clarification_round: int,
    clarification_questions: list[str] | str | None = None,
) -> str:
    """Render the terminal clarification-cap handoff comment."""
    _, issue_number = _issue_comment_target(issue)
    if not workflow_id:
        raise ValueError("post_issue_comment_activity requires workflow_id")
    try:
        round_number = int(clarification_round)
    except (TypeError, ValueError) as exc:
        raise ValueError("clarification_round must be an integer") from exc
    if round_number < 1:
        raise ValueError("clarification_round must be >= 1")

    return render_comment_template(
        "needs_human.md.j2",
        marker=f"<!-- df-clarify:{workflow_id}:{round_number} -->",
        issue_number=issue_number,
        questions=_clarification_questions(clarification_questions or []),
        workflow_id=workflow_id,
    )


def _activity_task_id(explicit_task_id: str | None = None) -> str | None:
    if explicit_task_id:
        return explicit_task_id
    env_task_id = os.environ.get("DARKFACTORY_WF_ID")
    if env_task_id:
        return env_task_id
    if not activity.in_activity():
        return None
    info = activity.info()
    if info.task_queue.startswith(AGENT_TASK_QUEUE_PREFIX):
        return info.task_queue[len(AGENT_TASK_QUEUE_PREFIX):]
    return info.workflow_id


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


def _post_issue_comment_argv(issue: Any) -> list[str]:
    repo, number = _issue_comment_target(issue)
    return [
        "gh",
        "issue",
        "comment",
        str(number),
        "--repo",
        repo,
        "--body-file",
        "-",
    ]


def _add_needs_human_label_argv(issue: Any) -> list[str]:
    repo, number = _issue_comment_target(issue)
    return [
        "gh",
        "issue",
        "edit",
        str(number),
        "--repo",
        repo,
        "--add-label",
        DF_NEEDS_HUMAN_LABEL,
    ]


def _add_done_label_argv(issue: Any) -> list[str]:
    repo, number = _issue_comment_target(issue)
    return [
        "gh",
        "issue",
        "edit",
        str(number),
        "--repo",
        repo,
        "--add-label",
        DF_DONE_LABEL,
    ]


def _quarantine_marker(workflow_id: str) -> str:
    return f"<!-- df-quarantine:{workflow_id} -->"


def _split_repo(repo: str) -> tuple[str, str]:
    parts = (repo or "").strip().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("repo must be owner/name")
    return parts[0], parts[1]


def _issue_workflow_id_prefix(repo: str, issue_number: int) -> str:
    """Return the prefix shared by all per-attempt workflow IDs for an issue."""
    owner, name = _split_repo(repo)
    if int(issue_number) < 1:
        raise ValueError("issue_number must be >= 1")
    return f"{ISSUE_WORKFLOW_ID_PREFIX}{owner}-{name}-{int(issue_number)}{ISSUE_WORKFLOW_RUN_INFIX}"


def _issue_workflow_id(repo: str, issue_number: int, run: int) -> str:
    if int(run) < 1:
        raise ValueError("run must be >= 1")
    return f"{_issue_workflow_id_prefix(repo, issue_number)}{int(run)}"


def _legacy_issue_workflow_id(repo: str, issue_number: int) -> str:
    """Single-attempt format used before per-run IDs were introduced.

    Kept around so we can detect and sync to a still-running legacy workflow
    one last time. Removable once no legacy workflows remain in flight.
    """
    owner, name = _split_repo(repo)
    if int(issue_number) < 1:
        raise ValueError("issue_number must be >= 1")
    return f"{ISSUE_WORKFLOW_ID_PREFIX}{owner}-{name}-{int(issue_number)}"


def _parse_run_suffix(workflow_id: str, prefix: str) -> int | None:
    if not workflow_id.startswith(prefix):
        return None
    suffix = workflow_id[len(prefix):]
    try:
        n = int(suffix)
    except ValueError:
        return None
    return n if n >= 1 else None


def _comment_body_str(comment: Any) -> str:
    return str(_state_value(comment, "body", "") or "")


def _marker_in_comments(comments: Any, workflow_id: str) -> bool:
    """True iff the quarantine marker for the given workflow_id appears in comments."""
    if not workflow_id or not comments:
        return False
    marker = _quarantine_marker(workflow_id)
    iterable = comments if isinstance(comments, list) else []
    return any(marker in _comment_body_str(c) for c in iterable)


async def _resolve_latest_run(
    client: Any,
    repo: str,
    issue_number: int,
) -> tuple[str | None, Any | None, int]:
    """Look up the highest-N attempt of an issue from Temporal visibility.

    Returns (workflow_id, status, n) for the largest n found across all
    workflows whose ID matches `df-issue-{owner}-{name}-{number}-run-{n}`.
    Returns (None, None, 0) when no per-attempt workflow exists.

    Malformed suffixes are skipped. Visibility is paginated; iteration is
    capped at RESOLVE_LATEST_RUN_PAGE_CAP * page_size to bound activity time.
    """
    prefix = _issue_workflow_id_prefix(repo, issue_number)
    query = f"WorkflowId STARTS_WITH '{prefix}'"

    best_n = 0
    best_id: str | None = None
    best_status: Any | None = None
    seen = 0
    page_size = 1000
    async for execution in client.list_workflows(query=query, page_size=page_size):
        n = _parse_run_suffix(execution.id, prefix)
        if n is None:
            continue
        if n > best_n:
            best_n = n
            best_id = execution.id
            best_status = execution.status
        seen += 1
        if seen >= RESOLVE_LATEST_RUN_PAGE_CAP * page_size:
            break

    return best_id, best_status, best_n


def _quarantine_label_for(closure_status: str) -> str | None:
    """Map a closed Temporal status to the label that replaces df:ready.

    COMPLETED runs already have df:done from mark_issue_done_activity, so we
    just drop df:ready. Everything else (canceled / failed / terminated /
    timed_out) gets a status label so a maintainer can see why the run ended
    without opening Temporal.
    """
    name = (closure_status or "").lower()
    if name == "completed":
        return None
    if name == "canceled":
        return DF_CANCELED_LABEL
    return DF_FAILED_LABEL


def render_quarantine_comment_body(
    issue_number: int,
    workflow_id: str,
    closure_status: str,
) -> str:
    return render_comment_template(
        "quarantine.md.j2",
        marker=_quarantine_marker(workflow_id),
        workflow_id=workflow_id,
        issue_number=issue_number,
        status_word=(closure_status or "closed").lower(),
        label=_quarantine_label_for(closure_status),
        ready_label=DF_READY_LABEL,
    ).rstrip()


def _gh_issue_view_argv(repo: str, number: int, fields: str) -> list[str]:
    if not repo or "/" not in repo:
        raise ValueError("gh issue view requires repo as owner/name")
    if number < 1:
        raise ValueError("gh issue view requires issue number >= 1")
    return [
        "gh",
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        fields,
    ]


def _gh_issue_label_swap_argv(
    repo: str,
    number: int,
    *,
    remove: list[str] | None = None,
    add: list[str] | None = None,
) -> list[str]:
    argv = ["gh", "issue", "edit", str(number), "--repo", repo]
    for label in remove or []:
        argv += ["--remove-label", label]
    for label in add or []:
        argv += ["--add-label", label]
    return argv


def _gh_issue_comment_argv(repo: str, number: int) -> list[str]:
    return [
        "gh",
        "issue",
        "comment",
        str(number),
        "--repo",
        repo,
        "--body-file",
        "-",
    ]


def _gh_issue_comments_api_argv(repo: str, number: int) -> list[str]:
    if number < 1:
        raise ValueError("gh issue comments requires issue number >= 1")
    owner, name = _split_repo(repo)
    return [
        "gh",
        "api",
        "--paginate",
        f"/repos/{owner}/{name}/issues/{number}/comments",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
    ]


def _gh_issue_comment_patch_argv(repo: str, comment_id: int) -> list[str]:
    if comment_id < 1:
        raise ValueError("gh issue comment patch requires comment_id >= 1")
    owner, name = _split_repo(repo)
    return [
        "gh",
        "api",
        "-X",
        "PATCH",
        f"/repos/{owner}/{name}/issues/comments/{comment_id}",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        "--input",
        "-",
    ]


def _find_comment_by_marker(comments: Any, marker: str) -> dict[str, Any] | None:
    if not marker:
        return None
    iterable = comments if isinstance(comments, list) else []
    for comment in iterable:
        body = str(_state_value(comment, "body", "") or "")
        if marker in body:
            return {
                "id": _comment_int_id(comment),
                "body": body,
            }
    return None


def _append_once(body: str, note: str) -> str:
    if not note or note in body:
        return body
    return body.rstrip() + "\n\n" + note.strip() + "\n"


def _preserve_manual_tail(existing_body: str, marker: str, rendered_body: str) -> str:
    end_marker = end_marker_for(marker)
    if end_marker not in existing_body:
        return rendered_body
    tail = existing_body.split(end_marker, 1)[1].strip()
    if not tail:
        return rendered_body
    return rendered_body.rstrip() + "\n\n" + tail + "\n"


def _run_orchestrator_gh(
    argv: list[str],
    *,
    timeout: int,
    description: str,
    stdin: str | None = None,
) -> str:
    """Run a `gh` command from the orchestrator with token-bearing env.

    Mirrors the temp-config-dir + `_github_cli_env` pattern used by
    list_ready_issues_activity so the GH CLI cannot fall back to ambient
    `gh auth` state on the host.
    """
    with tempfile.TemporaryDirectory(prefix="darkfactory-gh-") as config_dir:
        try:
            completed = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                check=False,
                env=_github_cli_env(config_dir),
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"{description} requires the gh CLI in the orchestrator runtime"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{description} timed out "
                f"(stdout={exc.stdout or ''!r}, stderr={exc.stderr or ''!r})"
            ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"{description} failed "
            f"(rc={completed.returncode}, "
            f"stdout={completed.stdout!r}, stderr={completed.stderr!r})"
        )
    return completed.stdout or ""


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


def _ensure_transcripts_subpath(client: Any, wf_id: str) -> None:
    # Docker's VolumeOptions.Subpath fails the worker start if the per-workflow
    # subdirectory doesn't already exist on the named volume. Pre-create it via
    # a throwaway alpine container, and chown it to uid 1000 so the worker (which
    # runs as agent without CAP_CHOWN) can write transcripts there.
    client.containers.run(
        image="alpine:3",
        command=[
            "sh",
            "-c",
            f"mkdir -p /v/{wf_id} && chown -R 1000:1000 /v/{wf_id}",
        ],
        mounts=[
            docker.types.Mount(
                target="/v",
                source=WORKER_TRANSCRIPTS_VOLUME,
                type="volume",
            )
        ],
        remove=True,
    )


def _is_repo_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "git@", "ssh://", "git://"))


def _clone_repo_into_workspace(container: Any, repo_url: str) -> None:
    """Clone a remote repo into /workspace inside the worker container.

    Used when no host-side checkout exists (issue-triggered runs). The worker
    image already has `gh`, `git`, and the orchestrator-injected GITHUB_TOKEN.
    """
    # `gh repo clone` shells out to `git clone`, which needs a credential
    # helper to consume the gh token — otherwise git prompts for a username
    # on stdin and the clone aborts with "could not read Username".
    setup = container.exec_run(
        ["gh", "auth", "setup-git"],
        workdir="/",
        user="agent",
    )
    setup_rc = _container_exec_returncode(setup)
    if setup_rc != 0:
        output = getattr(setup, "output", b"") or b""
        raise RuntimeError(
            f"gh auth setup-git failed (rc={setup_rc}, output={output!r})"
        )
    result = container.exec_run(
        ["gh", "repo", "clone", repo_url, "/workspace"],
        workdir="/",
        user="agent",
    )
    rc = _container_exec_returncode(result)
    if rc != 0:
        output = getattr(result, "output", b"") or b""
        raise RuntimeError(
            f"gh repo clone failed (rc={rc}, repo_url={repo_url!r}, output={output!r})"
        )


@activity.defn
async def ping_activity(msg: str) -> str:
    _stamp_temporal_activity_attrs()
    return msg


@activity.defn
async def list_ready_issues_activity(
    repo: str,
    label: str = "df:ready",
    limit: int = DEFAULT_ISSUE_LIST_LIMIT,
) -> list[dict[str, Any]]:
    """List open GitHub issues that are ready for Dark Factory polling.

    This supervisor-queue activity intentionally requires token auth from the
    orchestrator environment so it cannot silently fall back to local `gh auth`.
    """
    _stamp_temporal_activity_attrs()
    _heartbeat("issue_poll: listing ready issues")
    issues_by_number: dict[int, dict[str, Any]] = {}
    for watch_label in _poll_labels(label):
        argv = _gh_issue_list_argv(repo, watch_label, limit)

        with tempfile.TemporaryDirectory(prefix="darkfactory-gh-") as config_dir:
            try:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    check=False,
                    env=_github_cli_env(config_dir),
                    text=True,
                    timeout=ISSUE_LIST_TIMEOUT_S,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "list_ready_issues_activity requires the gh CLI in the "
                    "orchestrator runtime"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "gh issue list timed out "
                    f"(stdout={exc.stdout or ''!r}, stderr={exc.stderr or ''!r})"
                ) from exc

        if completed.returncode != 0:
            raise RuntimeError(
                "gh issue list failed "
                f"(rc={completed.returncode}, label={watch_label!r}, "
                f"stdout={completed.stdout!r}, stderr={completed.stderr!r})"
            )

        try:
            issues = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "gh issue list returned invalid JSON "
                f"(stdout={completed.stdout!r})"
            ) from exc
        if not isinstance(issues, list):
            raise RuntimeError(
                "gh issue list returned unexpected JSON "
                f"(type={type(issues).__name__})"
            )
        for issue in issues:
            normalised = _normalise_ready_issue(repo, issue)
            number = int(normalised["issue"]["number"])
            issues_by_number.setdefault(number, normalised)
    return list(issues_by_number.values())


@activity.defn
async def issue_workflow_capacity_activity() -> dict[str, int]:
    """Return current issue-workflow capacity from Temporal visibility."""
    _stamp_temporal_activity_attrs()
    _heartbeat("issue_poll: counting active issue workflows")

    max_concurrent = _watch_max_concurrent()
    query = (
        f"WorkflowId STARTS_WITH '{ISSUE_WORKFLOW_ID_PREFIX}' "
        "AND ExecutionStatus = 'Running'"
    )
    client = await _connect_temporal_client()
    active = 0
    async for execution in client.list_workflows(query=query, page_size=1000):
        if execution.id.startswith(ISSUE_WORKFLOW_ID_PREFIX):
            active += 1

    return {
        "active": active,
        "max_concurrent": max_concurrent,
        "available": max(0, max_concurrent - active),
    }


async def _sync_existing_issue_workflow(
    handle: Any,
    workflow_id: str,
    comments: list[Any] | None,
    *,
    missing_action: str = "ignored",
) -> dict[str, Any]:
    from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow

    try:
        description = await handle.describe()
    except RPCError as exc:
        if getattr(exc, "status", None) != RPCStatusCode.NOT_FOUND:
            raise
        reason = "max_concurrent" if missing_action == "throttled" else "not_found"
        return {
            "action": missing_action,
            "workflow_id": workflow_id,
            "reason": reason,
            "comments_forwarded": 0,
        }

    status = description.status
    if status != WorkflowExecutionStatus.RUNNING:
        return {
            "action": "ignored",
            "workflow_id": workflow_id,
            "reason": _closed_workflow_reason(status),
            "comments_forwarded": 0,
        }

    summary = await handle.query(DarkFactoryIssueWorkflow.current_state_summary)
    last_seen = _last_seen_comment_id(summary)
    new_comments = _comments_since_last_seen(
        _issue_comments_from_poll_record(comments or []),
        last_seen,
    )
    if not new_comments:
        return {
            "action": "ignored",
            "workflow_id": workflow_id,
            "reason": "no_new_comments",
            "last_seen_comment_id": last_seen,
            "latest_spec_rev": _state_value(summary, "latest_spec_rev", 1),
            "approval_waiting": bool(_state_value(summary, "approval_waiting", False)),
            "comments_forwarded": 0,
        }

    await handle.execute_update(
        DarkFactoryIssueWorkflow.post_new_comments,
        new_comments,
    )
    return {
        "action": "updated",
        "workflow_id": workflow_id,
        "last_seen_comment_id": last_seen,
        "latest_spec_rev": _state_value(summary, "latest_spec_rev", 1),
        "approval_waiting": bool(_state_value(summary, "approval_waiting", False)),
        "comments_forwarded": len(new_comments),
    }


@activity.defn
async def start_or_update_issue_workflow_activity(
    repo: str,
    issue_number: int,
    issue: Any,
    comments: list[Any] | None = None,
    allow_start: bool = True,
) -> dict[str, Any]:
    """Resolve and act on the latest per-attempt run for an issue.

    Decision matrix (see the per-attempt-workflow-id design doc):

    - No prior run: start `run-1` (or fall through to a still-running legacy
      single-attempt workflow, if one exists).
    - Latest run is RUNNING: forward new comments via `post_new_comments`.
    - Latest run is closed AND its quarantine marker is present in the
      issue comments: human re-added df:ready as a retry signal — start
      `run-(latest_n + 1)` and fan historical comments into the fresh
      workflow.
    - Latest run is closed AND no marker yet: return ignored so the poll
      workflow can call `quarantine_closed_issue_activity` and write the
      marker.
    - allow_start is False (concurrency cap reached) and no running
      attempt: throttled.

    The resolved `workflow_id` is always returned in the result so the
    poll workflow can target the right run for follow-up activities (e.g.
    quarantine).
    """
    _stamp_temporal_activity_attrs()
    _heartbeat("issue_poll: start/update issue workflow")

    from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow

    issue_number = int(issue_number)
    if issue_number < 1:
        raise ValueError(
            "start_or_update_issue_workflow_activity requires issue_number >= 1"
        )

    req = _issue_run_request_from_poll_record(repo, issue)
    client = await _connect_temporal_client()

    latest_id, latest_status, latest_n = await _resolve_latest_run(
        client, repo, issue_number
    )

    # Latest run is RUNNING → just forward new comments.
    if latest_id is not None and latest_status == WorkflowExecutionStatus.RUNNING:
        handle = client.get_workflow_handle(latest_id)
        return await _sync_existing_issue_workflow(handle, latest_id, comments)

    # No per-attempt run yet — probe the legacy single-attempt ID once. If
    # one is still running (mid-flight from before the migration), sync to
    # it instead of starting a duplicate run-1.
    if latest_id is None:
        legacy_id = _legacy_issue_workflow_id(repo, issue_number)
        legacy_handle = client.get_workflow_handle(legacy_id)
        try:
            legacy_desc = await legacy_handle.describe()
        except RPCError as exc:
            if getattr(exc, "status", None) != RPCStatusCode.NOT_FOUND:
                raise
            legacy_desc = None
        if (
            legacy_desc is not None
            and legacy_desc.status == WorkflowExecutionStatus.RUNNING
        ):
            return await _sync_existing_issue_workflow(
                legacy_handle, legacy_id, comments
            )

    # At this point: either no prior run, or latest is closed.
    if latest_id is None:
        next_run = 1
    else:
        # Closed run. Retry only if the human re-acknowledged via df:ready
        # re-add (= the quarantine marker for this run is already in the
        # issue's comments). Otherwise hand back to the poll workflow so it
        # can write the marker via `quarantine_closed_issue_activity`.
        if not _marker_in_comments(comments, latest_id):
            return {
                "action": "ignored",
                "workflow_id": latest_id,
                "reason": _closed_workflow_reason(latest_status),
                "comments_forwarded": 0,
            }
        next_run = latest_n + 1

    new_workflow_id = _issue_workflow_id(repo, issue_number, next_run)

    if not allow_start:
        return {
            "action": "throttled",
            "workflow_id": new_workflow_id,
            "reason": "max_concurrent",
            "comments_forwarded": 0,
        }

    try:
        await client.start_workflow(
            DarkFactoryIssueWorkflow.run,
            req,
            id=new_workflow_id,
            task_queue=SUPERVISOR_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError as exc:
        # Two ticks raced; sync to the run another tick just started.
        handle = client.get_workflow_handle(new_workflow_id, run_id=exc.run_id)
        return await _sync_existing_issue_workflow(
            handle, new_workflow_id, comments
        )

    forwarded = await _forward_history_to_fresh_run(
        client, new_workflow_id, comments
    )

    return {
        "action": "started",
        "workflow_id": new_workflow_id,
        "run_number": next_run,
        "comments_forwarded": forwarded,
    }


async def _forward_history_to_fresh_run(
    client: Any,
    workflow_id: str,
    comments: list[Any] | None,
) -> int:
    """Replay non-marker historical comments into a freshly-started run.

    `IssueRunRequest` doesn't carry comments, so a fresh run starts with no
    issue conversation context. We close the gap by sending the filtered
    history via the `post_new_comments` update right after start. The
    update is best-effort — if the workflow has already completed by the
    time we call it (extremely fast happy-path), `NOT_FOUND` is swallowed.
    """
    from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow

    history = _issue_comments_from_poll_record(comments or [])
    if not history:
        return 0
    handle = client.get_workflow_handle(workflow_id)
    try:
        await handle.execute_update(
            DarkFactoryIssueWorkflow.post_new_comments,
            history,
        )
    except RPCError as exc:
        if getattr(exc, "status", None) != RPCStatusCode.NOT_FOUND:
            raise
        return 0
    return len(history)


@activity.defn
async def quarantine_closed_issue_activity(
    repo: str,
    issue_number: int,
    workflow_id: str,
    closure_status: str,
) -> dict[str, Any]:
    """Drop df:ready and post a status comment when a polled issue's workflow has closed.

    Idempotent across activity retries: a marker comment guards against
    duplicate posts and label edits are themselves idempotent. Once df:ready
    is removed, the next IssuePollWorkflow tick will not return this issue,
    so this activity normally runs at most once per issue.
    """
    _stamp_temporal_activity_attrs()
    _heartbeat("issue_poll: quarantining closed issue workflow")

    if not repo or "/" not in repo:
        raise ValueError(
            "quarantine_closed_issue_activity requires repo as owner/name"
        )
    if issue_number < 1:
        raise ValueError(
            "quarantine_closed_issue_activity requires issue_number >= 1"
        )
    if not workflow_id:
        raise ValueError(
            "quarantine_closed_issue_activity requires workflow_id"
        )

    view_stdout = _run_orchestrator_gh(
        _gh_issue_view_argv(repo, issue_number, ISSUE_QUARANTINE_VIEW_FIELDS),
        timeout=ISSUE_QUARANTINE_TIMEOUT_S,
        description="gh issue view",
    )
    try:
        view = json.loads(view_stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh issue view returned invalid JSON (stdout={view_stdout!r})"
        ) from exc

    marker = _quarantine_marker(workflow_id)
    existing_comments = view.get("comments") or []
    comment_already_posted = any(
        marker in str(_state_value(comment, "body", "") or "")
        for comment in existing_comments
    )

    existing_labels = set(_normalise_label_names(view.get("labels")))
    status_label = _quarantine_label_for(closure_status)
    keep_labels = {status_label} if status_label else {DF_DONE_LABEL}
    remove_labels = [
        label
        for label in DF_STATE_LABELS
        if label in existing_labels and label not in keep_labels
    ]
    add_labels = (
        [status_label]
        if status_label and status_label not in existing_labels
        else []
    )

    if remove_labels or add_labels:
        _run_orchestrator_gh(
            _gh_issue_label_swap_argv(
                repo,
                issue_number,
                remove=remove_labels,
                add=add_labels,
            ),
            timeout=ISSUE_QUARANTINE_TIMEOUT_S,
            description="gh issue edit",
        )

    if not comment_already_posted:
        _run_orchestrator_gh(
            _gh_issue_comment_argv(repo, issue_number),
            timeout=ISSUE_QUARANTINE_TIMEOUT_S,
            description="gh issue comment",
            stdin=render_quarantine_comment_body(
                issue_number, workflow_id, closure_status
            ),
        )

    return {
        "workflow_id": workflow_id,
        "repo": repo,
        "issue_number": issue_number,
        "closure_status": (closure_status or "").lower(),
        "label_removed": (
            remove_labels[0]
            if len(remove_labels) == 1
            else (remove_labels if remove_labels else None)
        ),
        "label_added": status_label if add_labels else None,
        "comment_posted": not comment_already_posted,
    }


@activity.defn
async def detect_approval_signal_activity(
    issue: Any,
    since_id: int = 0,
    workflow_id: str = "",
    latest_spec_rev: int = 1,
) -> dict[str, Any] | None:
    """Detect the first authorized `/df` command or label fallback."""
    _stamp_temporal_activity_attrs()
    _heartbeat("issue_poll: detecting approval signal")

    repo, number = _issue_comment_target(issue)
    since_id = int(since_id or 0)
    view_stdout = _run_orchestrator_gh(
        _gh_issue_view_argv(repo, number, "comments,labels"),
        timeout=ISSUE_LIST_TIMEOUT_S,
        description="gh issue view",
    )
    try:
        view = json.loads(view_stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh issue view returned invalid JSON (stdout={view_stdout!r})"
        ) from exc
    comments_stdout = _run_orchestrator_gh(
        _gh_issue_comments_api_argv(repo, number),
        timeout=ISSUE_LIST_TIMEOUT_S,
        description="gh issue comments",
    )
    try:
        comments = json.loads(comments_stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh issue comments returned invalid JSON (stdout={comments_stdout!r})"
        ) from exc

    labels = set(_normalise_label_names(view.get("labels")))
    if DF_CANCEL_LABEL in labels:
        return ApprovalSignal(
            kind="Cancel",
            author="label:df:cancel",
            text="canceled via label",
        ).to_dict()
    if DF_APPROVED_LABEL in labels:
        return ApprovalSignal(
            kind="Approve",
            author="label:df:approved",
            text="approved via label",
        ).to_dict()

    for comment in _new_comments_from_view(comments, since_id):
        signal = parse_command(
            str(_state_value(comment, "body", "") or ""),
            author=_comment_author_name(
                _state_value(comment, "author", None)
                or _state_value(comment, "user", "")
            ),
            comment_id=_comment_int_id(comment),
            created_at=str(
                _state_value(comment, "created_at", None)
                or _state_value(comment, "createdAt", "")
                or ""
            ),
        )
        if signal is None:
            continue
        if not is_authorized(signal.author, repo, runner=_approval_gh_runner):
            _append_unauthorized_note(
                repo=repo,
                comments=comments,
                workflow_id=workflow_id,
                latest_spec_rev=latest_spec_rev,
                signal=signal,
            )
            continue
        return signal.to_dict()
    return None


def _new_comments_from_view(comments: Any, since_id: int) -> list[Any]:
    iterable = comments if isinstance(comments, list) else []
    return [
        comment
        for comment in iterable
        if _comment_int_id(comment) > since_id
    ]


def _approval_gh_runner(argv: list[str]) -> str:
    return _run_orchestrator_gh(
        argv,
        timeout=ISSUE_LIST_TIMEOUT_S,
        description="gh api collaborator permission",
    )


def _append_unauthorized_note(
    *,
    repo: str,
    comments: Any,
    workflow_id: str,
    latest_spec_rev: int,
    signal: ApprovalSignal,
) -> None:
    if not workflow_id:
        return
    try:
        marker = marker_for(workflow_id, "design", rev=max(1, int(latest_spec_rev)))
    except ValueError:
        return
    existing = _find_comment_by_marker(comments, marker)
    if not existing:
        return
    note = (
        f"Ignored `/df {signal.kind.lower()}` from @{signal.author} - "
        "insufficient permissions."
    )
    new_body = _append_once(str(existing["body"]), note)
    if new_body == existing["body"]:
        return
    _run_orchestrator_gh(
        _gh_issue_comment_patch_argv(repo, int(existing["id"])),
        timeout=ISSUE_COMMENT_TIMEOUT_S,
        description="gh issue comment edit",
        stdin=json.dumps({"body": new_body}),
    )


@activity.defn
async def signal_issue_workflow_activity(
    workflow_id: str,
    signal: Any,
) -> dict[str, Any]:
    """Forward an approval signal into a running issue workflow."""
    _stamp_temporal_activity_attrs()
    _heartbeat("issue_poll: forwarding approval signal")

    if not workflow_id:
        raise ValueError("signal_issue_workflow_activity requires workflow_id")
    approval = ApprovalSignal.from_any(signal)
    from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow

    client = await _connect_temporal_client()
    handle = client.get_workflow_handle(workflow_id)
    await handle.execute_update(
        DarkFactoryIssueWorkflow.signal_approval,
        approval,
    )
    return {
        "workflow_id": workflow_id,
        "approval_signal": approval.to_dict(),
    }


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

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)
    environment = os.environ.get("DARKFACTORY_ENVIRONMENT", "local")
    # Propagate the parent workflow's type as a resource attribute so every span
    # emitted from the worker container — including the bundled `claude` CLI's
    # native spans, which inherit OTEL_RESOURCE_ATTRIBUTES but otherwise have no
    # Temporal awareness — gets the right `langfuse.trace.name` after the
    # collector's coalesce_trace_id transform runs.
    workflow_type = activity.info().workflow_type if activity.in_activity() else ""
    repo_is_url = _is_repo_url(repo_url)
    volumes = (
        {} if repo_is_url else {repo_url: {"bind": "/workspace", "mode": "rw"}}
    )
    _ensure_transcripts_subpath(client, wf_id)
    transcripts_mount = docker.types.Mount(
        target=WORKER_CLAUDE_HOME,
        source=WORKER_TRANSCRIPTS_VOLUME,
        type="volume",
    )
    transcripts_mount["VolumeOptions"] = {"Subpath": wf_id}
    container = client.containers.run(
        image=_worker_image(),
        name=name,
        detach=True,
        network=WORKER_NETWORK,
        environment={
            "TEMPORAL_ADDRESS": _worker_temporal_address(),
            "TEMPORAL_TASK_QUEUE": f"agent-tq-{wf_id}",
            "DARKFACTORY_WF_ID": wf_id,
            "DARKFACTORY_ENVIRONMENT": environment,
            # Opt-in SDK argv/options diagnostics (sdk_diagnostics.py).
            # Forwarded from the orchestrator so it can be flipped without a
            # worker rebuild; empty (off) unless explicitly set.
            "DARKFACTORY_LOG_SDK_ARGV": os.environ.get(
                "DARKFACTORY_LOG_SDK_ARGV", ""
            ),
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
            "LANGFUSE_PUBLIC_KEY": os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
            "LANGFUSE_SECRET_KEY": os.environ.get("LANGFUSE_SECRET_KEY", ""),
            "LANGFUSE_HOST": os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000"),
            "LANGFUSE_PROMPTS_ENABLED": os.environ.get(
                "LANGFUSE_PROMPTS_ENABLED", "true"
            ),
            "LANGFUSE_PROMPT_LABEL": os.environ.get(
                "LANGFUSE_PROMPT_LABEL", "production"
            ),
            # Native Claude Code telemetry — see https://code.claude.com/docs/en/monitoring-usage.
            # Each role activity opens its own ClaudeSDKClient (one CLI subprocess per
            # activity), so traces propagate from the Temporal activity span via
            # TRACEPARENT injected in llm_factory.build_options().
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_endpoint,
            "OTEL_TRACES_EXPORT_INTERVAL": "1000",
            "OTEL_LOGS_EXPORT_INTERVAL": "1000",
            # Local-only content gates: prompts, tool args, tool I/O, and full API bodies
            # are captured. Strip these on any non-local deployment.
            "OTEL_LOG_USER_PROMPTS": "1",
            "OTEL_LOG_TOOL_DETAILS": "1",
            "OTEL_LOG_TOOL_CONTENT": "1",
            "OTEL_LOG_RAW_API_BODIES": "file:/var/log/claude-bodies",
            "OTEL_RESOURCE_ATTRIBUTES": (
                f"darkfactory.workflow_id={wf_id},"
                + (f"darkfactory.workflow_type={workflow_type}," if workflow_type else "")
                + f"darkfactory.environment={environment},"
                + "service.namespace=darkfactory"
            ),
        },
        volumes=volumes,
        mounts=[transcripts_mount],
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        user="1000:1000",
        mem_limit="2g",
        pids_limit=256,
        tmpfs={"/tmp": "size=512m"},
    )
    if repo_is_url:
        _clone_repo_into_workspace(container, repo_url)
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
    """Run the hydrator on the bind-mounted repo and return its state delta.

    Pure-Python (no LLM, no Docker exec); cheap enough to run inline rather
    than via a one-node subgraph.
    """
    _stamp_temporal_activity_attrs()
    with phase_span("hydrate"):
        _heartbeat("hydrate: starting")
        from darkfactory.stages.hydrator import hydrate_state

        repo_path = state.get("repo_path")
        if not repo_path:
            existing = state.get("repo_context") or {}
            if isinstance(existing, dict):
                repo_path = existing.get("repo_root")
        if not repo_path:
            repo_path = "/workspace"
        return hydrate_state(state, repo_path)


@activity.defn
async def discovery_stage(state: dict) -> dict:
    """Run the Discovery subgraph (PO → Architect → Plan Critic)."""
    _stamp_temporal_activity_attrs()
    with phase_span("discovery", attempt=state.get("planning_attempts")):
        _heartbeat("discovery: starting subgraph")
        from darkfactory.stages.discovery import discovery_subgraph

        sg = discovery_subgraph()
        result = await sg.ainvoke(state)
        _decision = result.get("review_decision") or {}
        _emit_langfuse_scores(
            stage="plan_critic",
            scores={
                "approved": 1.0 if _decision.get("approved") else 0.0,
                "attempts": float(state.get("planning_attempts", 0) + 1),
            },
            comment=(_decision.get("reason") or "")[:200] or None,
        )
        return {
            "stories": result.get("stories", []),
            "spec": result.get("spec", []),
            "work_packages": result.get("work_packages", []),
            "implementation_brief": result.get("implementation_brief"),
            "review_decision": result.get("review_decision"),
        }


@activity.defn
async def triage_stage(state: dict) -> dict:
    """Run the issue triage subgraph and return its decision channels."""
    _stamp_temporal_activity_attrs()
    with phase_span("triage", round=state.get("triage_round")):
        _heartbeat("triage: starting subgraph")
        from darkfactory.stages.triage import triage_subgraph

        sg = triage_subgraph()
        result = await sg.ainvoke(state)
        return {
            "ready_to_build": result.get("ready_to_build"),
            "clarification_questions": result.get("clarification_questions", []),
            "derived_user_request": result.get("derived_user_request", ""),
            "confidence": result.get("confidence"),
            "rationale": result.get("rationale", ""),
        }


@activity.defn
async def upsert_phase_comment_activity(
    issue: Any,
    marker: str,
    body: str,
    task_id: str | None = None,
    repo_path: str = "/workspace",
) -> int:
    """Create or edit the Dark Factory phase comment identified by marker."""
    _stamp_temporal_activity_attrs()
    _heartbeat("phase_comment: upserting comment")

    if not marker:
        raise ValueError("upsert_phase_comment_activity requires marker")
    if marker not in body:
        raise ValueError("upsert_phase_comment_activity body must include marker")

    repo, number = _issue_comment_target(issue)
    resolved_task_id = _activity_task_id(task_id)
    if not resolved_task_id:
        raise ValueError(
            "upsert_phase_comment_activity requires task_id, DARKFACTORY_WF_ID, "
            "or a Temporal activity context"
        )
    sb = _ensure_repo_sandbox({"task_id": resolved_task_id, "repo_path": repo_path})
    if sb is None:
        raise ValueError("upsert_phase_comment_activity could not resolve RepoSandbox")

    comments_result = sb.exec(
        _gh_issue_comments_api_argv(repo, number),
        timeout=ISSUE_COMMENT_TIMEOUT_S,
    )
    if int(comments_result.get("returncode", 1)) != 0:
        raise RuntimeError(
            "gh issue comments failed "
            f"(rc={comments_result.get('returncode')}, "
            f"stdout={comments_result.get('stdout', '')!r}, "
            f"stderr={comments_result.get('stderr', '')!r})"
        )
    try:
        comments = json.loads(comments_result.get("stdout") or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh issue comments returned invalid JSON") from exc

    existing = _find_comment_by_marker(comments, marker)
    if existing is None:
        create_result = sb.exec(
            _gh_issue_comment_argv(repo, number),
            timeout=ISSUE_COMMENT_TIMEOUT_S,
            stdin=body,
        )
        if int(create_result.get("returncode", 1)) != 0:
            raise RuntimeError(
                "gh issue comment failed "
                f"(rc={create_result.get('returncode')}, "
                f"stdout={create_result.get('stdout', '')!r}, "
                f"stderr={create_result.get('stderr', '')!r})"
            )
        created_id = _extract_created_comment_id(create_result.get("stdout"), marker)
        if created_id:
            return created_id
        refetch_result = sb.exec(
            _gh_issue_comments_api_argv(repo, number),
            timeout=ISSUE_COMMENT_TIMEOUT_S,
        )
        if int(refetch_result.get("returncode", 1)) != 0:
            return 0
        try:
            refetched = json.loads(refetch_result.get("stdout") or "[]")
        except json.JSONDecodeError:
            return 0
        created = _find_comment_by_marker(refetched, marker)
        return int(created["id"]) if created else 0

    comment_id = int(existing["id"])
    patched_body = _preserve_manual_tail(str(existing["body"]), marker, body)
    patch_result = sb.exec(
        _gh_issue_comment_patch_argv(repo, comment_id),
        timeout=ISSUE_COMMENT_TIMEOUT_S,
        stdin=json.dumps({"body": patched_body}),
    )
    if int(patch_result.get("returncode", 1)) != 0:
        raise RuntimeError(
            "gh issue comment edit failed "
            f"(rc={patch_result.get('returncode')}, "
            f"stdout={patch_result.get('stdout', '')!r}, "
            f"stderr={patch_result.get('stderr', '')!r})"
        )
    return comment_id


def _extract_created_comment_id(stdout: Any, marker: str) -> int:
    text = str(stdout or "")
    if not text:
        return 0
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return 0
    if isinstance(payload, dict):
        return _comment_int_id(payload)
    if isinstance(payload, list):
        match = _find_comment_by_marker(payload, marker)
        return int(match["id"]) if match else 0
    return 0


@activity.defn
async def swap_state_label_activity(
    issue: Any,
    remove: str | list[str] | None,
    add: str | list[str] | None,
    task_id: str | None = None,
    repo_path: str = "/workspace",
) -> dict[str, Any]:
    """Transition GitHub issue state labels with an idempotent edit."""
    _stamp_temporal_activity_attrs()
    _heartbeat("issue_label: swapping state label")

    repo, number = _issue_comment_target(issue)
    resolved_task_id = _activity_task_id(task_id)
    if not resolved_task_id:
        raise ValueError(
            "swap_state_label_activity requires task_id, DARKFACTORY_WF_ID, "
            "or a Temporal activity context"
        )
    sb = _ensure_repo_sandbox({"task_id": resolved_task_id, "repo_path": repo_path})
    if sb is None:
        raise ValueError("swap_state_label_activity could not resolve RepoSandbox")

    view_result = sb.exec(
        _gh_issue_view_argv(repo, number, "labels"),
        timeout=ISSUE_COMMENT_TIMEOUT_S,
    )
    if int(view_result.get("returncode", 1)) != 0:
        raise RuntimeError(
            "gh issue view failed "
            f"(rc={view_result.get('returncode')}, "
            f"stdout={view_result.get('stdout', '')!r}, "
            f"stderr={view_result.get('stderr', '')!r})"
        )
    try:
        view = json.loads(view_result.get("stdout") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh issue view returned invalid JSON") from exc

    existing = set(_normalise_label_names(view.get("labels")))
    remove_labels = [label for label in _label_list(remove) if label in existing]
    add_labels = [label for label in _label_list(add) if label not in existing]

    if remove_labels or add_labels:
        edit_result = sb.exec(
            _gh_issue_label_swap_argv(
                repo,
                number,
                remove=remove_labels,
                add=add_labels,
            ),
            timeout=ISSUE_COMMENT_TIMEOUT_S,
        )
        if int(edit_result.get("returncode", 1)) != 0:
            raise RuntimeError(
                "gh issue edit failed "
                f"(rc={edit_result.get('returncode')}, "
                f"stdout={edit_result.get('stdout', '')!r}, "
                f"stderr={edit_result.get('stderr', '')!r})"
            )

    return {
        "labels_removed": remove_labels,
        "labels_added": add_labels,
    }


def _label_list(labels: str | list[str] | None) -> list[str]:
    if labels is None:
        return []
    raw = [labels] if isinstance(labels, str) else list(labels)
    return [str(label).strip() for label in raw if str(label).strip()]


@activity.defn
async def post_issue_comment_activity(
    issue: Any,
    clarification_questions: list[str] | str,
    task_id: str | None = None,
    repo_path: str = "/workspace",
    clarification_round: int = 1,
    mark_needs_human: bool = False,
) -> dict:
    """Post a rendered clarification comment to the issue via `gh`."""
    _stamp_temporal_activity_attrs()
    _heartbeat("issue_comment: posting comment")

    resolved_task_id = _activity_task_id(task_id)
    if not resolved_task_id:
        raise ValueError(
            "post_issue_comment_activity requires task_id, DARKFACTORY_WF_ID, "
            "or a Temporal activity context"
        )

    sb = _ensure_repo_sandbox({"task_id": resolved_task_id, "repo_path": repo_path})
    if sb is None:
        raise ValueError("post_issue_comment_activity could not resolve RepoSandbox")

    argv = _post_issue_comment_argv(issue)
    if mark_needs_human:
        body = render_needs_human_comment_body(
            issue,
            workflow_id=resolved_task_id,
            clarification_round=clarification_round,
            clarification_questions=clarification_questions,
        )
    else:
        body = render_clarification_comment_body(
            issue,
            clarification_questions,
            workflow_id=resolved_task_id,
            clarification_round=clarification_round,
        )
    result = sb.exec(argv, timeout=ISSUE_COMMENT_TIMEOUT_S, stdin=body)
    if int(result.get("returncode", 1)) != 0:
        raise RuntimeError(
            "gh issue comment failed "
            f"(rc={result.get('returncode')}, "
            f"stdout={result.get('stdout', '')!r}, "
            f"stderr={result.get('stderr', '')!r})"
        )
    if not mark_needs_human:
        return {"issue_comment_posted": True}

    label_result = sb.exec(
        _add_needs_human_label_argv(issue),
        timeout=ISSUE_COMMENT_TIMEOUT_S,
    )
    if int(label_result.get("returncode", 1)) != 0:
        raise RuntimeError(
            "gh issue edit failed "
            f"(rc={label_result.get('returncode')}, "
            f"stdout={label_result.get('stdout', '')!r}, "
            f"stderr={label_result.get('stderr', '')!r})"
        )
    return {"issue_comment_posted": True, "needs_human_label_added": True}


@activity.defn
async def mark_issue_done_activity(
    issue: Any,
    task_id: str | None = None,
    repo_path: str = "/workspace",
) -> dict:
    """Mark an issue-driven run as done after the PR merge succeeds."""
    _stamp_temporal_activity_attrs()
    _heartbeat("issue_label: marking done")

    resolved_task_id = _activity_task_id(task_id)
    if not resolved_task_id:
        raise ValueError(
            "mark_issue_done_activity requires task_id, DARKFACTORY_WF_ID, "
            "or a Temporal activity context"
        )

    sb = _ensure_repo_sandbox({"task_id": resolved_task_id, "repo_path": repo_path})
    if sb is None:
        raise ValueError("mark_issue_done_activity could not resolve RepoSandbox")

    result = sb.exec(_add_done_label_argv(issue), timeout=ISSUE_COMMENT_TIMEOUT_S)
    if int(result.get("returncode", 1)) != 0:
        raise RuntimeError(
            "gh issue edit failed "
            f"(rc={result.get('returncode')}, "
            f"stdout={result.get('stdout', '')!r}, "
            f"stderr={result.get('stderr', '')!r})"
        )
    return {"done_label_added": True}


@activity.defn
@with_repo_state("agent/{wf_id}")
async def build_stage(state: dict) -> dict:
    """Run the Build subgraph (Builder Supervisor + Builder/Tester workers)."""
    _stamp_temporal_activity_attrs()
    with phase_span("build"):
        _heartbeat("build: starting subgraph")
        from darkfactory.stages.build import build_subgraph

        ctx = _runctx_from_state(state)
        sg = build_subgraph()
        result = await sg.ainvoke(state, context=ctx)
        return {
            "build_order": result.get("build_order"),
            "current_slice": result.get("current_slice"),
            "coverage_entries": result.get("coverage_entries", []),
            "tester_findings": result.get("tester_findings", []),
            "patches": result.get("patches", []),
        }


@activity.defn
@with_repo_state("agent/{wf_id}")
async def verify_stage(state: dict) -> dict:
    """Run the plan-driven Verify subgraph.

    The subgraph asks ``verify_planner`` to discover the target repo's
    canonical test / compile / lint commands (cached after the first
    iteration), executes each step via ``RepoSandbox.exec``, and parses
    JUnit-XML / Checkstyle-XML / SARIF report files declared by the plan
    via ``tools/reports.py``. Steps without declared reports fall back to
    exit-code gating. No language-specific code lives in the verifier
    itself.
    """
    _stamp_temporal_activity_attrs()
    verify_cycle = sum(
        1
        for entry in (state.get("attempt_log") or [])
        if isinstance(entry, dict) and entry.get("kind") == "verify"
    ) + 1
    with phase_span("verify", verify_cycle=verify_cycle):
        _heartbeat("verify: starting plan-driven subgraph")
        from darkfactory.stages.verify import verify_subgraph

        ctx = _runctx_from_state(state)
        sg = verify_subgraph()
        result = await sg.ainvoke(state, context=ctx)
        _vs = result.get("verify_summary") or {}
        _cov = _vs.get("predicate_coverage") or []
        _covered = sum(1 for c in _cov if c.get("status") == "covered")
        _weak = sum(1 for c in _cov if c.get("status") == "weakly_covered")
        _emit_langfuse_scores(
            stage="verify",
            scores={
                "passed": 1.0 if _vs.get("passed") else 0.0,
                "predicate_coverage_pct": (
                    (_covered + 0.5 * _weak) / max(1, len(_cov))
                ),
                "uncovered_predicates": float(_vs.get("uncovered_predicates", 0) or 0),
                "fixer_attempts_used": float(
                    sum((state.get("fixer_attempts_by_wp") or {}).values())
                ),
            },
        )
        delta: dict[str, Any] = {
            "test_results": result.get("test_results", []),
            "findings": result.get("findings", []),
            "verify_summary": result.get("verify_summary"),
        }
        if "verification_plan" in result:
            delta["verification_plan"] = result["verification_plan"]
        if "verification_plan_rev" in result:
            delta["verification_plan_rev"] = result["verification_plan_rev"]
        if "verify_retries" in result:
            delta["verify_retries"] = result["verify_retries"]
        return delta


@activity.defn
@with_repo_state("agent/{wf_id}")
async def fixer_stage(state: dict) -> dict:
    """Run Fixer on failing verifier diagnostics and return captured patches."""
    _stamp_temporal_activity_attrs()
    fixer_attempts = state.get("fixer_attempts_by_wp") or {}
    target_wp: str | None = None
    target_attempt: int | None = None
    if isinstance(fixer_attempts, dict) and fixer_attempts:
        target_wp, target_attempt = max(
            fixer_attempts.items(), key=lambda kv: kv[1]
        )
    with phase_span("fixer", wp_id=target_wp, attempt=target_attempt):
        _heartbeat("fixer: starting")
        return await _run_fixer_stage(state)


async def _run_fixer_stage(state: dict) -> dict:
    """Snapshot HEAD, run the Fixer, compute ground-truth patches, reconcile.

    PR C: the Fixer no longer collects patches through a ``diff_capture``
    hook. The activity captures the pre-Fixer ``HEAD`` sha, runs the
    Fixer (which declares its intended edits in the structured output),
    and asks ``git diff`` what actually changed. Discrepancies between
    the declared ``edits`` and the observed paths surface as
    ``reconciliation_findings`` entries the same way they do for
    Builder.
    """
    from darkfactory.agents.fixer import run_fixer
    from darkfactory.tools.git_diff import (
        compute_wp_diff,
        reconcile_paths,
        snapshot_head,
    )

    sandbox = _ensure_repo_sandbox(state)
    pre_sha = snapshot_head(sandbox) if sandbox is not None else ""

    out = await run_fixer(state)
    target_wp = str(out.get("target_wp") or "")

    patches: list[Any] = []
    reconciliation: list[dict] = []
    if sandbox is not None and pre_sha:
        patches = compute_wp_diff(
            sandbox, pre_sha, role="fixer", slice_id=target_wp
        )
        edits = list(out.get("edits") or [])
        claimed = [
            str(edit.get("path") or "")
            for edit in edits
            if isinstance(edit, dict) and edit.get("path")
        ]
        actual = [
            str(p.get("path") or "")
            for p in patches
            if p.get("path")
        ]
        recon = reconcile_paths(claimed, actual)
        if out.get("parse_failure"):
            reconciliation.append(
                {
                    "kind": "fixer_blocked",
                    "wp_id": target_wp,
                    "producer": "fixer_stage",
                    "detail": (
                        "Fixer produced no structured output; treating "
                        "as cannot_fix."
                    ),
                }
            )
        if recon["claimed_not_applied"]:
            reconciliation.append(
                {
                    "kind": "claimed_edits_not_applied",
                    "wp_id": target_wp,
                    "producer": "fixer_stage",
                    "detail": (
                        f"Fixer declared "
                        f"{len(recon['claimed_not_applied'])} edit(s) "
                        "that were not applied to the working tree"
                    ),
                    "claimed_paths": recon["claimed_not_applied"],
                    "actual_paths": actual,
                }
            )
        if recon["undeclared"]:
            reconciliation.append(
                {
                    "kind": "undeclared_edits",
                    "wp_id": target_wp,
                    "producer": "fixer_stage",
                    "detail": (
                        f"Fixer applied {len(recon['undeclared'])} "
                        "edit(s) it did not declare in its structured output"
                    ),
                    "claimed_paths": claimed,
                    "actual_paths": recon["undeclared"],
                }
            )

    return _fixer_delta(out, patches, reconciliation)


def _fixer_delta(
    out: dict[str, Any],
    patches: list[Any] | None = None,
    reconciliation: list[dict[str, Any]] | None = None,
) -> dict:
    """Translate a Fixer output dict into a workflow-mergeable state delta.

    ``out`` is the plain dict produced by ``agents.fixer.run_fixer``:
    ``{decision, target_wp, target_predicates, edits, summary, reason}``.
    ``patches`` and ``reconciliation`` come from ``_run_fixer_stage`` and
    carry the ground-truth diff plus any agent-vs-disk discrepancies.
    """
    from darkfactory.state import Patch

    target_wp = str(out.get("target_wp") or "")
    decision_payload = {
        k: v
        for k, v in out.items()
        if k not in ("parse_failure",)
    }
    delta: dict[str, Any] = {
        "fixer_decision": decision_payload,
        "current_slice": target_wp,
    }

    coerced: list[Patch] = []
    for raw_patch in patches or []:
        patch = dict(raw_patch)
        if not (patch.get("path") and patch.get("diff")):
            raise ValueError("fixer patch missing required fields (path, diff)")
        patch["author_agent"] = "fixer"
        patch["slice_id"] = str(patch.get("slice_id") or target_wp)
        coerced.append(Patch(**patch))
    if coerced:
        delta["patches"] = coerced
    if reconciliation:
        delta["reconciliation_findings"] = list(reconciliation)
    return delta


@activity.defn
async def reviewer_stage(state: dict) -> dict:
    """Run the Reviewer and surface its gate summary."""
    _stamp_temporal_activity_attrs()
    with phase_span("reviewer"):
        _heartbeat("reviewer: starting")
        from darkfactory.agents.reviewer import run_reviewer

        result = await run_reviewer(state)
        rv = result.model_dump()
        _severity = {"low": 1.0, "medium": 2.0, "high": 3.0}
        _emit_langfuse_scores(
            stage="reviewer",
            scores={
                "severity_numeric": _severity.get(rv.get("severity"), 0.0),
                "recommendation_approve": 1.0
                if rv.get("recommendation") == "approve"
                else 0.0,
                "issue_count": float(len(rv.get("issues") or [])),
                "findings_count": float(len(rv.get("findings") or [])),
            },
        )
        return {"review_decision": rv}


@activity.defn(name="code_quality_stage")
async def code_quality_stage(state: dict) -> dict:
    """Compatibility alias for historical Temporal activity name."""
    return await reviewer_stage(state)


def _existing_pr_url(sb: Any, branch: str) -> str:
    """Return an open PR URL for ``branch`` if one exists, else empty string.

    Run before invoking the PR Creator role so the activity is idempotent
    under Temporal retry: if a previous attempt already created the PR,
    we return its URL without re-prompting the model.
    """
    if sb is None or not branch:
        return ""
    result = sb.exec(
        ["gh", "pr", "list", "--head", branch, "--json", "url", "--jq", ".[0].url"]
    )
    if int(result.get("returncode", 1)) != 0:
        return ""
    return str(result.get("stdout", "") or "").strip()


@activity.defn
@with_repo_state("agent/{wf_id}")
async def pr_creator_stage(state: dict) -> dict:
    """Run the PR Creator role and return the workflow's `pr_url` channel."""
    _stamp_temporal_activity_attrs()
    with phase_span("pr_creator"):
        _heartbeat("pr_creator: starting")

        sb = _ensure_repo_sandbox(state)
        branch = _branch_from_state(state, "agent/{wf_id}")
        existing = _existing_pr_url(sb, branch)
        if existing:
            return {"pr_url": existing}

        from darkfactory.agents.pr_creator import run_pr_creator

        output = await run_pr_creator(state)
        return {"pr_url": output["pr_url"]}


def _gh_pr_state(sb: Any, pr_url: str) -> str:
    """Return the GitHub state of a PR (e.g. ``MERGED``/``OPEN``/``CLOSED``).

    Returns an empty string when `gh pr view` itself fails — callers treat that
    as "unknown" and fall back to the original error path.
    """
    result = sb.exec(["gh", "pr", "view", pr_url, "--json", "state", "--jq", ".state"])
    if int(result.get("returncode", 1)) != 0:
        return ""
    return str(result.get("stdout", "") or "").strip().upper()


@activity.defn
@with_repo_state("agent/{wf_id}")
async def merge_branch(state: dict) -> dict:
    """Merge the approved pull request without involving an LLM.

    Idempotent: if the PR is already MERGED on GitHub, returns success without
    re-invoking `gh pr merge`. If the merge command itself fails but the PR
    ends up MERGED (e.g. GitHub-side merge succeeded but `--delete-branch`
    tripped on a dirty local working tree), the failure is treated as success
    — the worker container is torn down in the workflow's `finally` block, so
    local branch cleanup is not load-bearing.
    """
    _stamp_temporal_activity_attrs()
    with phase_span("merge"):
        _heartbeat("merge_branch: starting")
        pr_url = state.get("pr_url")
        if not pr_url:
            raise ValueError("merge_branch requires state['pr_url']")

        sb = _ensure_repo_sandbox(state)
        if sb is None:
            raise ValueError("merge_branch requires state['task_id'] or state['wf_id']")

        if _gh_pr_state(sb, pr_url) == "MERGED":
            log.info("merge_branch: PR %s already merged, skipping gh pr merge", pr_url)
            return {"merged": True}

        result = sb.exec(["gh", "pr", "merge", pr_url, "--squash", "--delete-branch"])
        if int(result.get("returncode", 1)) != 0:
            if _gh_pr_state(sb, pr_url) == "MERGED":
                log.warning(
                    "merge_branch: gh pr merge returned rc=%s but PR %s is MERGED; "
                    "treating as success (stderr=%r)",
                    result.get("returncode"),
                    pr_url,
                    result.get("stderr", ""),
                )
                return {"merged": True}
            raise RuntimeError(
                "gh pr merge failed "
                f"(rc={result.get('returncode')}, "
                f"stdout={result.get('stdout', '')!r}, "
                f"stderr={result.get('stderr', '')!r})"
            )

        return {"merged": True}


STAGE_ACTIVITIES: tuple = (
    hydrate_stage,
    triage_stage,
    discovery_stage,
    build_stage,
    verify_stage,
    fixer_stage,
    reviewer_stage,
    code_quality_stage,
    pr_creator_stage,
    merge_branch,
)
