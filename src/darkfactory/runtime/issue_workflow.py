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
from darkfactory.runtime.workflow import (
    PLANNING_MAX_ATTEMPTS,
    SUPERVISOR_TASK_QUEUE,
    _fixer_budget_exhaustion,
    _fixer_decision_escalation,
    _fixer_escalation_delta,
    _planning_approved,
    _planning_attempt_log_entry,
    _planning_feedback_from_decision,
    _planning_feedback_from_human_revise,
    _record_fixer_attempt_delta,
    _reset_planning_artifacts,
)
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
DF_REVIEWING = "df:reviewing"
DF_AWAITING_MERGE = "df:awaiting-merge"
DF_FIXING = "df:fixing"
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


def _phase_key(
    phase: str,
    rev: int | None = None,
    attempt: int | None = None,
) -> str:
    if phase == "design" and rev is not None:
        return f"{phase}:{rev}"
    if phase == "review" and attempt is not None:
        return f"{phase}:{attempt}"
    return phase


_SENTINEL_PATCH_PATHS = frozenset({"(worker-completion)", "(worker-error)"})


def _real_patches(patches: Any) -> list[Any]:
    """Filter out the build-subgraph synthetic completion/error sentinels.

    Tester / Frontend still emit ``(worker-completion)`` when they make
    no edits (the supervisor uses it to advance ``build_order``);
    Builder stopped emitting it in PR B in favour of ``builder_outputs``.
    For phase metrics we only care about real file changes.
    """
    out: list[Any] = []
    for patch in patches or []:
        path = str(_state_value(patch, "path", "") or "")
        if path in _SENTINEL_PATCH_PATHS:
            continue
        out.append(patch)
    return out


def _patch_paths(patches: Any) -> list[str]:
    paths: list[str] = []
    for patch in _real_patches(patches):
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
        self._pending_gate: str | None = None

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
                    "next": "review",
                },
            )

            review_result = await self._run_review_and_merge_gate(agent_tq)
            if review_result is not None:
                return review_result

            await self._swap_label(agent_tq, DF_AWAITING_MERGE, DF_IN_PROGRESS)
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

            # Absorb any forwarded comments queued before this loop began
            # (e.g. the fresh-run history fanout from
            # `start_or_update_issue_workflow_activity`). Without this drain
            # the wait_condition below would unblock immediately on the first
            # clarify and burn a round before any user actually replied.
            if self._new_comments:
                pre_existing = list(self._new_comments)
                self._new_comments = []
                self._state = merge(
                    self._state,
                    {"issue_comments": pre_existing},
                )

            triage = await workflow.execute_activity(
                "triage_stage",
                self._state,
                task_queue=agent_tq,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    # Outer activity retry covers account-tier quota windows
                    # that exceed the Claude Agent SDK's per-call backoff.
                    maximum_attempts=5,
                    initial_interval=timedelta(seconds=15),
                    maximum_interval=timedelta(minutes=2),
                    backoff_coefficient=2.0,
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
            critic_approved, spec_markdown = await self._run_planning_loop(
                agent_tq,
                rev,
            )
            if not critic_approved:
                await self._phase(
                    agent_tq,
                    "design",
                    "failed",
                    {
                        "spec_markdown": spec_markdown,
                        "approval_note": "Plan Critic rejected the brief after the retry budget.",
                        "include_approval_instructions": False,
                        "next": "human",
                    },
                    rev=rev,
                )
                await self._swap_label(agent_tq, DF_DESIGNING, DF_NEEDS_HUMAN)
                return RunResult(
                    status="needs_human",
                    state=self._state,
                    reason="planning_retry_cap",
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
                human_feedback = _planning_feedback_from_human_revise(signal)
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
                        "planning_attempts": 0,
                        "planning_feedback": [human_feedback],
                        "planning_attempt_log": [
                            _planning_attempt_log_entry(
                                source="human_revise",
                                attempt=int(self._state.get("planning_attempts") or 0),
                                rev=rev,
                                next_rev=rev + 1,
                                feedback=human_feedback,
                                author=signal.author,
                                comment_id=signal.comment_id,
                            )
                        ],
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

    async def _run_planning_loop(
        self,
        agent_tq: str,
        rev: int,
    ) -> tuple[bool, str]:
        feedback = list(self._state.get("planning_feedback") or [])
        spec_markdown = ""
        for attempt in range(PLANNING_MAX_ATTEMPTS):
            attempt_number = attempt + 1
            visible_feedback = "\n".join(str(item) for item in feedback if item)
            await self._phase(
                agent_tq,
                "design",
                "running",
                {"feedback": visible_feedback},
                rev=rev,
                attempt=attempt_number,
            )
            self._state = merge(
                self._state,
                {
                    "planning_attempts": attempt_number,
                    "planning_max_attempts": PLANNING_MAX_ATTEMPTS,
                    "planning_feedback": feedback,
                },
            )
            self._state = _reset_planning_artifacts(self._state)
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "discovery_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=8),
                    heartbeat_timeout=timedelta(minutes=5),
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
            decision = self._state.get("review_decision")
            if _planning_approved(decision):
                return True, spec_markdown

            feedback_item = _planning_feedback_from_decision(decision)
            feedback = [*feedback, feedback_item]
            self._state = merge(
                self._state,
                {
                    "planning_feedback": feedback,
                    "planning_attempt_log": [
                        _planning_attempt_log_entry(
                            source="plan_critic_reject",
                            attempt=attempt_number,
                            rev=rev,
                            feedback=feedback_item,
                        )
                    ],
                },
            )

        return False, spec_markdown

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
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=2,
                    non_retryable_error_types=["ParseError"],
                ),
            ),
        )
        paths = _patch_paths(self._state.get("patches"))
        build_attempts.append(f"attempt 1: {len(paths)} files changed")
        await self._phase(
            agent_tq,
            "build",
            "done",
            {
                "commit_count": len(
                    _real_patches(self._state.get("patches"))
                ),
                "files_changed": len(paths),
                "branch": self._state.get("feature_branch"),
                "attempts": build_attempts,
                "next": "verify",
            },
        )
        await self._swap_label(agent_tq, DF_BUILDING, DF_VERIFYING)

        attempt_number = 0
        while True:
            attempt_number += 1
            await self._phase(
                agent_tq,
                "verify",
                "running",
                {"attempts": verify_attempts},
                attempt=attempt_number,
            )
            # See workflow.py: clear verify-owned channels so each cycle is
            # evaluated against fresh test/lint evidence rather than the
            # accumulated history that `add` reducers would otherwise keep.
            self._state["test_results"] = []
            self._state["findings"] = []
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "verify_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(
                        maximum_attempts=2,
                        non_retryable_error_types=["ParseError"],
                    ),
                ),
            )
            summary = self._state.get("verify_summary") or {}
            passed = bool(_state_value(summary, "passed", False))
            verify_attempts.append(
                f"attempt {attempt_number}: {'passed' if passed else 'failed'}"
            )
            if passed:
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

            escalation = _fixer_budget_exhaustion(self._state)
            if escalation is not None:
                self._state = merge(
                    self._state,
                    _fixer_escalation_delta(escalation),
                )
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
                    reason=str(escalation["reason"]),
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
                _record_fixer_attempt_delta(self._state),
            )
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "fixer_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(
                        maximum_attempts=2,
                        non_retryable_error_types=["ParseError"],
                    ),
                ),
            )
            escalation = _fixer_decision_escalation(self._state)
            if escalation is not None:
                self._state = merge(
                    self._state,
                    _fixer_escalation_delta(escalation),
                )
                await self._phase(
                    agent_tq,
                    "verify",
                    "failed",
                    {
                        "summary": self._state.get("verify_summary") or {},
                        "attempts": verify_attempts,
                        "next": "human",
                    },
                )
                await self._swap_label(agent_tq, DF_VERIFYING, DF_NEEDS_HUMAN)
                return RunResult(
                    status="needs_human",
                    state=self._state,
                    reason=str(escalation["reason"]),
                )

    async def _run_review_and_merge_gate(self, agent_tq: str) -> RunResult | None:
        iteration = 0
        while True:
            iteration += 1
            await self._swap_label(
                agent_tq,
                [DF_VERIFYING, DF_FIXING, DF_BUILDING, DF_AWAITING_MERGE],
                DF_REVIEWING,
            )
            await self._phase(
                agent_tq,
                "review",
                "running",
                {"pr_url": self._state.get("pr_url")},
                attempt=iteration,
            )
            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "reviewer_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=5),
                ),
            )
            await self._phase(
                agent_tq,
                "review",
                "done",
                {
                    "pr_url": self._state.get("pr_url"),
                    "review_decision": self._state.get("review_decision"),
                    "verify_summary": self._state.get("verify_summary"),
                    "include_merge_instructions": True,
                    "next": "human",
                },
                attempt=iteration,
            )
            await self._swap_label(agent_tq, DF_REVIEWING, DF_AWAITING_MERGE)

            self._approval_signal = None
            self._pending_gate = "merge"
            await workflow.wait_condition(lambda: self._approval_signal is not None)
            signal = self._approval_signal
            self._pending_gate = None
            if signal is None:
                continue
            self._record_last_seen(signal.comment_id)

            if signal.kind in {"Reject", "Cancel"}:
                closure = "rejected" if signal.kind == "Reject" else "canceled"
                await self._swap_label(
                    agent_tq,
                    [DF_AWAITING_MERGE, DF_CANCEL],
                    DF_CANCELED,
                )
                await self._quarantine(closure)
                return RunResult(
                    status="canceled" if signal.kind == "Cancel" else "rejected",
                    state=self._state,
                    reason=signal.text or signal.kind,
                )

            if signal.kind == "Approve":
                self._state = merge(
                    self._state,
                    {
                        "merge_gate_approved": True,
                        "merge_gate_reason": signal.text,
                        "merge_gate_author": signal.author,
                        "gate_approved": True,
                    },
                )
                return None

            if signal.kind == "Fix":
                await self._swap_label(agent_tq, DF_AWAITING_MERGE, DF_FIXING)
                self._state = merge(
                    self._state,
                    {
                        "human_fix_focus": signal.text,
                        "human_fix_author": signal.author,
                    },
                )
                self._state = merge(
                    self._state,
                    _record_fixer_attempt_delta(self._state),
                )
                self._state = merge(
                    self._state,
                    await workflow.execute_activity(
                        "fixer_stage",
                        self._state,
                        task_queue=agent_tq,
                        start_to_close_timeout=timedelta(minutes=5),
                        heartbeat_timeout=timedelta(minutes=5),
                        retry_policy=RetryPolicy(
                            maximum_attempts=2,
                            non_retryable_error_types=["ParseError"],
                        ),
                    ),
                )
                self._state = merge(
                    self._state,
                    await workflow.execute_activity(
                        "verify_stage",
                        self._state,
                        task_queue=agent_tq,
                        start_to_close_timeout=timedelta(minutes=10),
                        heartbeat_timeout=timedelta(minutes=5),
                        retry_policy=RetryPolicy(
                            maximum_attempts=2,
                            non_retryable_error_types=["ParseError"],
                        ),
                    ),
                )
                continue

            if signal.kind == "Rebuild":
                await self._swap_label(agent_tq, DF_AWAITING_MERGE, DF_BUILDING)
                self._state = merge(
                    self._state,
                    {
                        "human_rebuild_focus": signal.text,
                        "human_rebuild_author": signal.author,
                    },
                )
                self._state = merge(
                    self._state,
                    await workflow.execute_activity(
                        "build_stage",
                        self._state,
                        task_queue=agent_tq,
                        start_to_close_timeout=timedelta(minutes=15),
                        heartbeat_timeout=timedelta(minutes=5),
                        retry_policy=RetryPolicy(
                            maximum_attempts=2,
                            non_retryable_error_types=["ParseError"],
                        ),
                    ),
                )
                self._state = merge(
                    self._state,
                    await workflow.execute_activity(
                        "verify_stage",
                        self._state,
                        task_queue=agent_tq,
                        start_to_close_timeout=timedelta(minutes=10),
                        heartbeat_timeout=timedelta(minutes=5),
                        retry_policy=RetryPolicy(
                            maximum_attempts=2,
                            non_retryable_error_types=["ParseError"],
                        ),
                    ),
                )
                continue

            # Revise or unknown kind: ignore and re-render the verdict.
            continue

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
        key = _phase_key(phase, rev=rev, attempt=attempt)
        started_at = self._phase_started_at.get(key)
        if started_at is None or status == "running" and key not in self._phase_started_at:
            started_at = workflow.now()
            self._phase_started_at[key] = started_at
        ended_at = workflow.now() if status != "running" else None
        marker = marker_for(
            str(self._state.get("wf_id") or ""),
            phase,
            rev=rev,
            attempt=attempt,
        )
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
        # Backwards-compatible alias. Prefer the dedicated `approve_brief`,
        # `approve_merge`, or `reject_*` update methods for new callers.
        self._approval_signal = ApprovalSignal(
            kind="Approve" if decision.approved else "Reject",
            author="temporal",
            text=decision.reason,
        )

    def _signal_from_gate(
        self,
        kind: str,
        decision: GateDecision,
    ) -> ApprovalSignal:
        return ApprovalSignal(
            kind=kind,
            author="temporal",
            text=str(decision.reason or ""),
        )

    @workflow.update
    def approve_brief(self, decision: GateDecision) -> None:
        approval = self._signal_from_gate("Approve", decision)
        self._approval_signal = approval
        self._record_last_seen(approval.comment_id)

    @workflow.update
    def revise_brief(self, decision: GateDecision) -> None:
        approval = self._signal_from_gate("Revise", decision)
        self._approval_signal = approval
        self._record_last_seen(approval.comment_id)

    @workflow.update
    def reject_brief(self, decision: GateDecision) -> None:
        approval = self._signal_from_gate("Reject", decision)
        self._approval_signal = approval
        self._record_last_seen(approval.comment_id)

    @workflow.update
    def approve_merge(self, decision: GateDecision) -> None:
        approval = self._signal_from_gate("Approve", decision)
        self._approval_signal = approval
        self._record_last_seen(approval.comment_id)

    @workflow.update
    def reject_merge(self, decision: GateDecision) -> None:
        approval = self._signal_from_gate("Reject", decision)
        self._approval_signal = approval
        self._record_last_seen(approval.comment_id)

    @workflow.update
    def trigger_fix(self, decision: GateDecision) -> None:
        approval = self._signal_from_gate("Fix", decision)
        self._approval_signal = approval
        self._record_last_seen(approval.comment_id)

    @workflow.update
    def trigger_rebuild(self, decision: GateDecision) -> None:
        approval = self._signal_from_gate("Rebuild", decision)
        self._approval_signal = approval
        self._record_last_seen(approval.comment_id)

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
        current_label = self._state.get("current_df_label")
        approval_waiting = (
            current_label == DF_AWAITING_APPROVAL
            and self._approval_signal is None
        )
        merge_gate_waiting = (
            current_label == DF_AWAITING_MERGE
            and self._approval_signal is None
        )
        pending_gate: str | None = None
        if approval_waiting:
            pending_gate = "design"
        elif merge_gate_waiting:
            pending_gate = "merge"
        gate_pending = approval_waiting or merge_gate_waiting
        return {
            "planning_attempts": self._state.get("planning_attempts", 0),
            "planning_feedback": self._state.get("planning_feedback", []),
            "planning_attempt_log": self._state.get("planning_attempt_log", []),
            "verify_retries": self._state.get("verify_retries", 0),
            "verify_summary": self._state.get("verify_summary"),
            "fixer_attempts_by_predicate": self._state.get(
                "fixer_attempts_by_predicate", {}
            ),
            "fixer_attempts_by_wp": self._state.get("fixer_attempts_by_wp", {}),
            "attempt_log": self._state.get("attempt_log", []),
            "gate_approved": self._state.get("gate_approved", False),
            "merge_gate_approved": self._state.get("merge_gate_approved", False),
            "gate_pending": gate_pending,
            "pending_gate": pending_gate,
            "design_gate_pending": approval_waiting,
            "merge_gate_pending": merge_gate_waiting,
            "approval_waiting": approval_waiting,
            "approval_signal_pending": self._approval_signal is not None,
            "latest_spec_rev": self._state.get("latest_spec_rev", 1),
            "approval_record": self._state.get("approval_record"),
            "current_df_label": current_label,
            "current_slice": self._state.get("current_slice"),
            "pr_url": self._state.get("pr_url"),
            "review_decision": self._state.get("review_decision"),
            "ready_to_build": self._state.get("ready_to_build"),
            "clarification_questions": self._state.get("clarification_questions", []),
            "issue": _issue_summary(self._state.get("issue")),
            "issue_comment_count": len(issue_comments),
            "pending_comment_count": len(pending_comments),
            "last_seen_comment_id": last_seen,
        }
