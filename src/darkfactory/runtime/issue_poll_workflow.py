from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow

from darkfactory.runtime.workflow import SUPERVISOR_TASK_QUEUE
from darkfactory.state import IssueComment, IssuePollRequest


def _state_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _polled_issue_record(polled_issue: Any) -> dict[str, Any]:
    issue = _state_value(polled_issue, "issue", {})
    if not isinstance(issue, dict):
        issue = dict(issue)
    if not issue.get("repo"):
        issue["repo"] = _state_value(polled_issue, "repo", "")
    try:
        issue["number"] = int(issue.get("number") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("polled issue requires numeric issue.number") from exc
    if issue["number"] < 1:
        raise ValueError("polled issue requires issue.number >= 1")
    return issue


def _comment_author_name(author: Any) -> str:
    if isinstance(author, str):
        return author
    if isinstance(author, dict):
        return str(author.get("login") or author.get("name") or "")
    return str(getattr(author, "login", None) or getattr(author, "name", "") or "")


def _polled_issue_comments(polled_issue: Any) -> list[dict[str, Any]]:
    """Normalize the raw gh-issue comment list without filtering bot markers.

    The activity inspects markers (e.g. df-quarantine) to detect retry
    signals, so it needs the raw bodies. Downstream forwarding paths
    (`post_new_comments` update and fresh-run fanout) re-filter via
    `filter_dark_factory_marker_comments` so bot markers never reach the
    issue workflow's context.
    """
    comments = _state_value(polled_issue, "comments", [])
    if not isinstance(comments, list):
        return []

    normalised: list[IssueComment] = []
    for comment in comments:
        try:
            comment_id = int(_state_value(comment, "id", 0) or 0)
        except (TypeError, ValueError):
            comment_id = 0
        normalised.append(
            IssueComment(
                id=comment_id,
                author=_comment_author_name(_state_value(comment, "author", "")),
                body=str(_state_value(comment, "body", "") or ""),
                created_at=str(_state_value(comment, "created_at", "") or ""),
            )
        )
    return [comment.model_dump() for comment in normalised]


@workflow.defn
class IssuePollWorkflow:
    @workflow.run
    async def run(self, req: IssuePollRequest) -> dict:
        issues = await workflow.execute_activity(
            "list_ready_issues_activity",
            args=[req.repo, req.label, req.limit],
            task_queue=SUPERVISOR_TASK_QUEUE,
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )
        capacity = await workflow.execute_activity(
            "issue_workflow_capacity_activity",
            args=[],
            task_queue=SUPERVISOR_TASK_QUEUE,
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )
        starts_remaining = int(capacity.get("available", 0) or 0)
        issue_workflows = []
        started = 0
        updated = 0
        ignored = 0
        throttled = 0
        quarantined = 0
        approval_signaled = 0
        for polled_issue in issues:
            issue = _polled_issue_record(polled_issue)
            repo = str(issue.get("repo") or req.repo)
            issue["repo"] = repo
            comments = _polled_issue_comments(polled_issue)
            allow_start = starts_remaining > 0
            sync_result = await workflow.execute_activity(
                "start_or_update_issue_workflow_activity",
                args=[repo, issue["number"], issue, comments, allow_start],
                task_queue=SUPERVISOR_TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=2),
                heartbeat_timeout=timedelta(seconds=30),
            )
            action = sync_result.get("action")
            workflow_id = str(sync_result.get("workflow_id") or "")
            if action == "started":
                started += 1
                starts_remaining = max(0, starts_remaining - 1)
            elif action == "updated":
                updated += 1
            elif action == "ignored":
                ignored += 1
            elif action == "throttled":
                throttled += 1

            quarantine_result = None
            reason = sync_result.get("reason")
            if (
                action == "ignored"
                and workflow_id
                and isinstance(reason, str)
                and reason.startswith("closed:")
            ):
                closure_status = reason.split(":", 1)[1]
                quarantine_result = await workflow.execute_activity(
                    "quarantine_closed_issue_activity",
                    args=[repo, issue["number"], workflow_id, closure_status],
                    task_queue=SUPERVISOR_TASK_QUEUE,
                    start_to_close_timeout=timedelta(minutes=2),
                    heartbeat_timeout=timedelta(seconds=30),
                )
                quarantined += 1

            approval_result = None
            if workflow_id and _can_detect_approval(sync_result):
                signal = await workflow.execute_activity(
                    "detect_approval_signal_activity",
                    args=[
                        issue,
                        int(sync_result.get("last_seen_comment_id") or 0),
                        workflow_id,
                        int(sync_result.get("latest_spec_rev") or 1),
                    ],
                    task_queue=SUPERVISOR_TASK_QUEUE,
                    start_to_close_timeout=timedelta(minutes=2),
                    heartbeat_timeout=timedelta(seconds=30),
                )
                if signal:
                    approval_result = await workflow.execute_activity(
                        "signal_issue_workflow_activity",
                        args=[workflow_id, signal],
                        task_queue=SUPERVISOR_TASK_QUEUE,
                        start_to_close_timeout=timedelta(minutes=2),
                        heartbeat_timeout=timedelta(seconds=30),
                    )
                    approval_signaled += 1

            entry = {
                "issue": issue,
                "comments_seen": len(comments),
                **sync_result,
            }
            if quarantine_result is not None:
                entry["quarantine"] = quarantine_result
            if approval_result is not None:
                entry["approval"] = approval_result
            issue_workflows.append(entry)

        return {
            "repo": req.repo,
            "label": req.label,
            "limit": req.limit,
            "issues_seen": len(issues),
            "active_issue_workflows": capacity.get("active", 0),
            "max_concurrent": capacity.get("max_concurrent", 0),
            "starts_available": capacity.get("available", 0),
            "issue_workflows": issue_workflows,
            "started": started,
            "updated": updated,
            "ignored": ignored,
            "throttled": throttled,
            "quarantined": quarantined,
            "approval_signaled": approval_signaled,
        }


def _can_detect_approval(sync_result: dict[str, Any]) -> bool:
    action = sync_result.get("action")
    reason = sync_result.get("reason")
    if action == "updated":
        return True
    return action == "ignored" and reason == "no_new_comments"
