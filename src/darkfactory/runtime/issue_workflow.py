from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from darkfactory.runtime.approval import ApprovalSignal
from darkfactory.runtime.issue_comments import filter_dark_factory_marker_comments
from darkfactory.runtime.phase_comment import (
    marker_for,
    render_phase_comment,
    render_spec_markdown,
)
from darkfactory.runtime.workflow import SUPERVISOR_TASK_QUEUE, VERIFY_RETRY_CAP
from darkfactory.state import (
    GateDecision,
    IssueComment,
    IssueRunRequest,
    RunResult,
    init_state_from_issue,
    merge,
)


DF_READY = "df:ready"
DF_TRIAGING = "df:triaging"
DF_NEEDS_CLARIFICATION = "df:needs-clarification"
DF_DESIGNING = "df:designing"
DF_AWAITING_APPROVAL = "df:awaiting-approval"
DF_APPROVED = "df:approved"
DF_BUILDING = "df:building"
DF_VERIFYING = "df:verifying"
DF_IN_PROGRESS = "df:in-progress"
DF_DONE = "df:done"
DF_NEEDS_HUMAN = "df:needs-human"
DF_CANCEL = "df:cancel"
DF_CANCELED = "df:canceled"

MAX_CLARIFICATION_ROUNDS = 3


def _state_value(obj: object, key: str, default: object = None) -> object:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _issue_summary(issue: object | None) -> dict | None:
    if not issue:
        return None
    return {
        "repo": _state_value(issue, "repo", ""),
        "number": _state_value(issue, "number", 0),
        "url": _state_value(issue, "url", ""),
        "title": _state_value(issue, "title", ""),
        "labels": list(_state_value(issue, "labels", []) or []),
    }


def _comment_id(comment: object) -> int | None:
    raw = _state_value(comment, "id")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _last_comment_id(comments: list[object]) -> int | None:
    ids = [
        comment_id
        for comment in comments
        if (comment_id := _comment_id(comment)) is not None
    ]
    return max(ids) if ids else None


def _phase_key(phase: str, rev: int | None = None) -> str:
    return f"{phase}:{rev}" if phase == "design" and rev is not None else phase


def _quality_approved(decision: Any) -> bool:
    if not decision:
        return True
    recommendation = str(_state_value(decision, "recommendation", "") or "").lower()
    if recommendation:
        return recommendation == "approve"
    approved = _state_value(decision, "approved", None)
    if approved is not None:
        return bool(approved)
    return True


def _patch_paths(patches: Any) -> list[str]:
    paths: list[str] = []
    for patch in patches or []:
        path = str(_state_value(patch, "path", "") or "")
        if path and path not in paths:
            paths.append(path)
    return paths


@workflow.defn
class DarkFactoryIssueWorkflow:
    def __init__(self) -> None:
        self._approval_signal: ApprovalSignal | None = None
        self._new_comments: list[IssueComment] = []
        self._state: dict = {}
        self._phase_started_at: dict[str, datetime] = {}

    @workflow.run
    async def run(self, req: IssueRunRequest) -> RunResult:
        wf_id = workflow.info().workflow_id
        agent_tq = f"agent-tq-{wf_id}"
        self._state = init_state_from_issue(req)
        self._state["wf_id"] = wf_id
        self._state["task_id"] = wf_id
        self._state["feature_branch"] = f"agent/{wf_id}"
        self._state["current_df_label"] = DF_READY

        await workflow.execute_activity(
            "setup_worker_activity",
            args=[wf_id, req.repo_url],
            task_queue=SUPERVISOR_TASK_QUEUE,
            start_to_close_timeout=timedelta(minutes=2),
        )
        try:
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "hydrate_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=3),
                    heartbeat_timeout=timedelta(seconds=30),
                ),
            )

            await self._swap_label(agent_tq, DF_READY, DF_TRIAGING)
            await self._phase(
                agent_tq,
                "triage",
                "running",
                {"outcome": "running"},
            )

            triage_result = await self._run_triage(agent_tq, wf_id)
            if triage_result is not None:
                return triage_result

            await self._swap_label(agent_tq, DF_TRIAGING, DF_DESIGNING)

            design_result = await self._run_design_gate(agent_tq)
            if design_result is not None:
                return design_result

            await self._swap_label(
                agent_tq,
                [DF_AWAITING_APPROVAL, DF_APPROVED],
                DF_BUILDING,
            )

            build_verify_result = await self._run_build_verify(agent_tq)
            if build_verify_result is not None:
                return build_verify_result

            await self._phase(agent_tq, "pr", "running", {})
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "pr_creator_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=5),
                ),
            )
            await self._phase(
                agent_tq,
                "pr",
                "done",
                {
                    "pr_url": self._state.get("pr_url"),
                    "next": "merge",
                },
            )
            await self._swap_label(agent_tq, DF_VERIFYING, DF_IN_PROGRESS)

            cancel_result = await self._cancel_if_requested(agent_tq, DF_IN_PROGRESS)
            if cancel_result is not None:
                return cancel_result

            await self._phase(agent_tq, "merge", "running", {})
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "merge_branch",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=2),
                ),
            )
            await self._phase(
                agent_tq,
                "merge",
                "done",
                {
                    "branch_deleted": True,
                    "issue_closes": f"issue auto-closes via Closes #{self._issue_number()}",
                    "next": "done",
                },
            )
            if self._state.get("issue"):
                self._state = merge(
                    self._state,
                    await workflow.execute_activity(
                        "mark_issue_done_activity",
                        args=[
                            self._state["issue"],
                            wf_id,
                            self._state.get("repo_path", "/workspace"),
                        ],
                        task_queue=agent_tq,
                        start_to_close_timeout=timedelta(minutes=1),
                        heartbeat_timeout=timedelta(seconds=30),
                    ),
                )
            await self._swap_label(agent_tq, DF_IN_PROGRESS, DF_DONE)
            return RunResult(status="merged", state=self._state)
        finally:
            await workflow.execute_activity(
                "teardown_worker_activity",
                args=[wf_id],
                task_queue=SUPERVISOR_TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=1),
            )

    async def _run_triage(
        self,
        agent_tq: str,
        wf_id: str,
    ) -> RunResult | None:
        clarification_round = 0
        while True:
            cancel_result = await self._cancel_if_requested(agent_tq, DF_TRIAGING)
            if cancel_result is not None:
                return cancel_result

            triage = await workflow.execute_activity(
                "triage_stage",
                self._state,
                task_queue=agent_tq,
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(
                    maximum_attempts=2,
                    non_retryable_error_types=["ParseError"],
                ),
            )
            self._state = merge(self._state, triage)
            if triage.get("ready_to_build"):
                self._state = merge(
                    self._state,
                    {"user_request": triage.get("derived_user_request", "")},
                )
                await self._phase(
                    agent_tq,
                    "triage",
                    "done",
                    {
                        "outcome": "ready",
                        "derived_request": triage.get("derived_user_request", ""),
                        "confidence": triage.get("confidence"),
                        "rationale": triage.get("rationale", ""),
                        "next": "design",
                    },
                )
                return None

            if clarification_round >= MAX_CLARIFICATION_ROUNDS:
                self._state = merge(
                    self._state,
                    {
                        "clarification_rounds": clarification_round,
                        "abandoned_reason": "max_clarification_rounds",
                    },
                )
                await self._phase(
                    agent_tq,
                    "triage",
                    "done",
                    {
                        "outcome": "abandoned",
                        "round": clarification_round,
                        "max_rounds": MAX_CLARIFICATION_ROUNDS,
                        "rationale": "max clarification rounds reached",
                        "next": "human",
                    },
                )
                await workflow.execute_activity(
                    "post_issue_comment_activity",
                    args=[
                        self._state["issue"],
                        triage.get("clarification_questions", []),
                        wf_id,
                        self._state.get("repo_path", "/workspace"),
                        clarification_round + 1,
                        True,
                    ],
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=1),
                    heartbeat_timeout=timedelta(seconds=30),
                )
                await self._swap_label(agent_tq, DF_TRIAGING, DF_NEEDS_HUMAN)
                return RunResult(
                    status="abandoned",
                    state=self._state,
                    reason="max_clarification_rounds",
                )

            clarification_round += 1
            await self._phase(
                agent_tq,
                "triage",
                "running",
                {
                    "outcome": "needs-clarification",
                    "round": clarification_round,
                    "max_rounds": MAX_CLARIFICATION_ROUNDS,
                },
            )
            await workflow.execute_activity(
                "post_issue_comment_activity",
                args=[
                    self._state["issue"],
                    triage.get("clarification_questions", []),
                    wf_id,
                    self._state.get("repo_path", "/workspace"),
                    clarification_round,
                ],
                task_queue=agent_tq,
                start_to_close_timeout=timedelta(minutes=1),
                heartbeat_timeout=timedelta(seconds=30),
            )
            await self._swap_label(agent_tq, DF_TRIAGING, DF_NEEDS_CLARIFICATION)
            await workflow.wait_condition(
                lambda: bool(self._new_comments) or self._cancel_signal() is not None
            )
            cancel_result = await self._cancel_if_requested(
                agent_tq,
                DF_NEEDS_CLARIFICATION,
            )
            if cancel_result is not None:
                return cancel_result
            new_comments = list(self._new_comments)
            self._new_comments = []
            self._state = merge(self._state, {"issue_comments": new_comments})
            await self._swap_label(agent_tq, DF_NEEDS_CLARIFICATION, DF_TRIAGING)
            await self._phase(
                agent_tq,
                "triage",
                "running",
                {
                    "outcome": "running",
                    "round": clarification_round + 1,
                    "max_rounds": MAX_CLARIFICATION_ROUNDS,
                },
            )

    async def _run_design_gate(self, agent_tq: str) -> RunResult | None:
        while True:
            rev = int(self._state.get("latest_spec_rev") or 1)
            await self._phase(
                agent_tq,
                "design",
                "running",
                {"feedback": self._state.get("revision_feedback", "")},
                rev=rev,
            )
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "discovery_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=8),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(
                        maximum_attempts=2,
                        non_retryable_error_types=["ParseError"],
                    ),
                ),
            )
            spec_markdown = render_spec_markdown(
                user_request=str(self._state.get("user_request") or ""),
                stories=self._state.get("stories") or [],
                spec=self._state.get("spec") or [],
                review_decision=self._state.get("review_decision"),
            )
            self._state = merge(
                self._state,
                {
                    "latest_spec_rev": rev,
                    "latest_spec_markdown": spec_markdown,
                },
            )
            await self._phase(
                agent_tq,
                "design",
                "done",
                {
                    "spec_markdown": spec_markdown,
                    "include_approval_instructions": True,
                },
                rev=rev,
            )
            await self._swap_label(agent_tq, DF_DESIGNING, DF_AWAITING_APPROVAL)
            self._approval_signal = None
            await workflow.wait_condition(lambda: self._approval_signal is not None)
            signal = self._approval_signal
            if signal is None:
                continue
            self._record_last_seen(signal.comment_id)

            if signal.kind == "Revise":
                await self._phase(
                    agent_tq,
                    "design",
                    "done",
                    {
                        "spec_markdown": spec_markdown,
                        "revision_note": (
                            f"Revision requested by @{signal.author}: {signal.text}"
                        ),
                        "include_approval_instructions": False,
                        "next": "design rev " + str(rev + 1),
                    },
                    rev=rev,
                )
                self._state = merge(
                    self._state,
                    {
                        "latest_spec_rev": rev + 1,
                        "revision_feedback": signal.text,
                    },
                )
                await self._swap_label(agent_tq, DF_AWAITING_APPROVAL, DF_DESIGNING)
                self._approval_signal = None
                continue

            if signal.kind in {"Reject", "Cancel"}:
                await self._phase(
                    agent_tq,
                    "design",
                    "done",
                    {
                        "spec_markdown": spec_markdown,
                        "approval_note": (
                            f"Canceled by @{signal.author}: {signal.text or signal.kind}"
                        ),
                        "include_approval_instructions": False,
                        "next": "canceled",
                    },
                    rev=rev,
                )
                await self._swap_label(
                    agent_tq,
                    [DF_AWAITING_APPROVAL, DF_CANCEL],
                    DF_CANCELED,
                )
                await self._quarantine("canceled")
                return RunResult(
                    status="canceled",
                    state=self._state,
                    reason=signal.text or signal.kind,
                )

            if signal.kind == "Approve":
                approved_at = workflow.now().isoformat()
                record = {
                    "author": signal.author,
                    "approved_at": approved_at,
                    "spec_rev": rev,
                    "comment_id": signal.comment_id,
                    "text": signal.text,
                }
                self._state = merge(
                    self._state,
                    {
                        "gate_approved": True,
                        "approval_record": record,
                        "approved_spec_rev": rev,
                        "approved_spec_markdown": spec_markdown,
                    },
                )
                await self._phase(
                    agent_tq,
                    "design",
                    "done",
                    {
                        "spec_markdown": spec_markdown,
                        "approval_note": (
                            f"Spec rev {rev} approved by @{signal.author} at {approved_at}."
                        ),
                        "include_approval_instructions": False,
                        "next": "build",
                    },
                    rev=rev,
                )
                return None

    async def _run_build_verify(self, agent_tq: str) -> RunResult | None:
        build_attempts: list[str] = []
        verify_attempts: list[str] = []
        await self._phase(
            agent_tq,
            "build",
            "running",
            {"branch": self._state.get("feature_branch")},
            attempt=1,
        )
        for attempt in range(VERIFY_RETRY_CAP):
            attempt_number = attempt + 1
            self._state = merge(self._state, {"verify_retries": attempt})
            if attempt > 0:
                await self._swap_label(agent_tq, DF_VERIFYING, DF_BUILDING)
                await self._phase(
                    agent_tq,
                    "build",
                    "running",
                    {
                        "branch": self._state.get("feature_branch"),
                        "attempts": build_attempts,
                    },
                    attempt=attempt_number,
                )

            cancel_result = await self._cancel_if_requested(agent_tq, DF_BUILDING)
            if cancel_result is not None:
                return cancel_result

            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "build_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(minutes=2),
                ),
            )
            paths = _patch_paths(self._state.get("patches"))
            build_attempts.append(
                f"attempt {attempt_number}: {len(paths)} files changed"
            )
            await self._phase(
                agent_tq,
                "build",
                "done",
                {
                    "commit_count": len(self._state.get("patches") or []),
                    "files_changed": len(paths),
                    "branch": self._state.get("feature_branch"),
                    "attempts": build_attempts,
                    "next": "verify",
                },
            )
            await self._swap_label(agent_tq, DF_BUILDING, DF_VERIFYING)
            await self._phase(
                agent_tq,
                "verify",
                "running",
                {"attempts": verify_attempts},
                attempt=attempt_number,
            )
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "verify_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(minutes=2),
                ),
            )
            summary = self._state.get("verify_summary") or {}
            passed = bool(_state_value(summary, "passed", False))
            verify_attempts.append(f"attempt {attempt_number}: {'passed' if passed else 'failed'}")
            if passed:
                self._state = merge(
                    self._state,
                    await workflow.execute_activity(
                        "code_quality_stage",
                        self._state,
                        task_queue=agent_tq,
                        start_to_close_timeout=timedelta(minutes=5),
                    ),
                )
                if not _quality_approved(self._state.get("review_decision")):
                    await self._phase(
                        agent_tq,
                        "verify",
                        "failed",
                        {
                            "summary": summary,
                            "quality": self._state.get("review_decision"),
                            "attempts": verify_attempts,
                            "next": "human",
                        },
                    )
                    await self._swap_label(agent_tq, DF_VERIFYING, DF_NEEDS_HUMAN)
                    return RunResult(
                        status="needs_human",
                        state=self._state,
                        reason="code_quality_failed",
                    )
                await self._phase(
                    agent_tq,
                    "verify",
                    "done",
                    {
                        "summary": summary,
                        "quality": self._state.get("review_decision"),
                        "attempts": verify_attempts,
                        "next": "pr",
                    },
                )
                return None

            if attempt == VERIFY_RETRY_CAP - 1:
                await self._phase(
                    agent_tq,
                    "verify",
                    "failed",
                    {
                        "summary": summary,
                        "attempts": verify_attempts,
                        "next": "human",
                    },
                )
                await self._swap_label(agent_tq, DF_VERIFYING, DF_NEEDS_HUMAN)
                return RunResult(
                    status="needs_human",
                    state=self._state,
                    reason="verify_retry_cap",
                )

            await self._phase(
                agent_tq,
                "verify",
                "running",
                {
                    "summary": summary,
                    "attempts": verify_attempts,
                },
                attempt=attempt_number,
            )
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "spec_adjustment_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=5),
                ),
            )
        return RunResult(status="needs_human", state=self._state, reason="verify_retry_cap")

    async def _phase(
        self,
        agent_tq: str,
        phase: str,
        status: str,
        fields: dict[str, Any],
        *,
        rev: int | None = None,
        attempt: int | None = None,
    ) -> None:
        key = _phase_key(phase, rev)
        started_at = self._phase_started_at.get(key)
        if started_at is None or status == "running" and key not in self._phase_started_at:
            started_at = workflow.now()
            self._phase_started_at[key] = started_at
        ended_at = workflow.now() if status != "running" else None
        marker = marker_for(str(self._state.get("wf_id") or ""), phase, rev=rev)
        body = render_phase_comment(
            phase,
            status,
            fields,
            wf_id=str(self._state.get("wf_id") or ""),
            trace_url=str(self._state.get("trace_url") or ""),
            rev=rev,
            attempt=attempt,
            started_at=started_at,
            ended_at=ended_at,
        )
        comment_id = await workflow.execute_activity(
            "upsert_phase_comment_activity",
            args=[
                self._state["issue"],
                marker,
                body,
                self._state.get("wf_id"),
                self._state.get("repo_path", "/workspace"),
            ],
            task_queue=agent_tq,
            start_to_close_timeout=timedelta(minutes=1),
            heartbeat_timeout=timedelta(seconds=30),
        )
        if comment_id:
            ids = dict(self._state.get("phase_comment_ids") or {})
            ids[key] = int(comment_id)
            self._state = merge(self._state, {"phase_comment_ids": ids})

    async def _swap_label(
        self,
        agent_tq: str,
        remove: str | list[str] | None,
        add: str | list[str] | None,
    ) -> None:
        await workflow.execute_activity(
            "swap_state_label_activity",
            args=[
                self._state["issue"],
                remove,
                add,
                self._state.get("wf_id"),
                self._state.get("repo_path", "/workspace"),
            ],
            task_queue=agent_tq,
            start_to_close_timeout=timedelta(minutes=1),
            heartbeat_timeout=timedelta(seconds=30),
        )
        if isinstance(add, str) and add:
            self._state = merge(self._state, {"current_df_label": add})

    async def _cancel_if_requested(
        self,
        agent_tq: str,
        current_label: str,
    ) -> RunResult | None:
        signal = self._cancel_signal()
        if signal is None:
            return None
        self._record_last_seen(signal.comment_id)
        await self._swap_label(agent_tq, [current_label, DF_CANCEL], DF_CANCELED)
        await self._quarantine("canceled")
        return RunResult(
            status="canceled",
            state=self._state,
            reason=signal.text or "canceled",
        )

    def _cancel_signal(self) -> ApprovalSignal | None:
        signal = self._approval_signal
        if signal is not None and signal.kind == "Cancel":
            return signal
        return None

    async def _quarantine(self, closure_status: str) -> None:
        repo = str(_state_value(self._state.get("issue"), "repo", "") or "")
        number = self._issue_number()
        if not repo or number < 1:
            return
        await workflow.execute_activity(
            "quarantine_closed_issue_activity",
            args=[repo, number, self._state.get("wf_id"), closure_status],
            task_queue=SUPERVISOR_TASK_QUEUE,
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
        )

    def _issue_number(self) -> int:
        try:
            return int(_state_value(self._state.get("issue"), "number", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _record_last_seen(self, comment_id: int | None) -> None:
        if not comment_id:
            return
        current = int(self._state.get("last_seen_comment_id") or 0)
        self._state = merge(
            self._state,
            {"last_seen_comment_id": max(current, int(comment_id))},
        )

    @workflow.update
    def signal_approval(self, signal: ApprovalSignal) -> None:
        approval = ApprovalSignal.from_any(signal)
        self._approval_signal = approval
        self._record_last_seen(approval.comment_id)

    @workflow.update
    def approve_gate(self, decision: GateDecision) -> None:
        self._approval_signal = ApprovalSignal(
            kind="Approve" if decision.approved else "Reject",
            author="temporal",
            text=decision.reason,
        )

    @workflow.update
    def post_new_comments(self, comments: list[IssueComment]) -> None:
        latest = _last_comment_id(list(comments or []))
        self._record_last_seen(latest)
        self._new_comments.extend(filter_dark_factory_marker_comments(comments))

    @workflow.query
    def current_state_summary(self) -> dict:
        issue_comments = list(self._state.get("issue_comments") or [])
        pending_comments = list(self._new_comments)
        last_seen = max(
            int(self._state.get("last_seen_comment_id") or 0),
            _last_comment_id([*issue_comments, *pending_comments]) or 0,
        )
        approval_waiting = (
            self._state.get("current_df_label") == DF_AWAITING_APPROVAL
            and self._approval_signal is None
        )
        return {
            "verify_retries": self._state.get("verify_retries", 0),
            "verify_summary": self._state.get("verify_summary"),
            "gate_approved": self._state.get("gate_approved", False),
            "gate_pending": approval_waiting,
            "approval_waiting": approval_waiting,
            "approval_signal_pending": self._approval_signal is not None,
            "latest_spec_rev": self._state.get("latest_spec_rev", 1),
            "approval_record": self._state.get("approval_record"),
            "current_df_label": self._state.get("current_df_label"),
            "current_slice": self._state.get("current_slice"),
            "pr_url": self._state.get("pr_url"),
            "ready_to_build": self._state.get("ready_to_build"),
            "clarification_questions": self._state.get("clarification_questions", []),
            "issue": _issue_summary(self._state.get("issue")),
            "issue_comment_count": len(issue_comments),
            "pending_comment_count": len(pending_comments),
            "last_seen_comment_id": last_seen,
        }
