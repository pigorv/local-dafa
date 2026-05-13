from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from darkfactory.state import (
    GateDecision,
    RunRequest,
    RunResult,
    init_state,
    merge,
)


PLANNING_MAX_ATTEMPTS = 5
FIXER_MAX_ATTEMPTS = 2
# Compatibility name for older tests and callers. The v2 workflow no longer
# uses a global build/verify retry loop; a stable failure still produces one
# initial verify plus two focused Fixer attempts.
VERIFY_RETRY_CAP = FIXER_MAX_ATTEMPTS + 1
SUPERVISOR_TASK_QUEUE = "supervisor-tq"
UNKNOWN_FIXER_TARGET = "__unknown__"

# Build-stage activity scales its start_to_close_timeout by Work Package
# count: 5 min base + 4 min per WP, capped at 60 min. A flat 15 min was
# tight on multi-WP briefs.
BUILD_STAGE_BASE_MINUTES = 5
BUILD_STAGE_PER_WP_MINUTES = 4
BUILD_STAGE_MAX_MINUTES = 60


def _build_stage_timeout(state: Any) -> timedelta:
    """Compute start_to_close timeout for the build stage from WP count.

    Reads ``build_order`` first (populated by the supervisor when set) and
    falls back to ``spec`` length. Guarantees at least one WP's worth of
    budget so a missing/empty channel never collapses to the base alone.
    """
    build_order = _state_value(state, "build_order", None) or []
    spec = _state_value(state, "spec", None) or []
    wp_count = max(1, len(build_order) or len(spec))
    minutes = min(
        BUILD_STAGE_MAX_MINUTES,
        BUILD_STAGE_BASE_MINUTES + BUILD_STAGE_PER_WP_MINUTES * wp_count,
    )
    return timedelta(minutes=minutes)


def _state_value(obj: object, key: str, default: object = None) -> object:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _planning_approved(decision: Any) -> bool:
    if not decision:
        return True
    approved = _state_value(decision, "approved", None)
    if approved is None:
        return True
    return bool(approved)


def _planning_feedback_from_decision(decision: Any) -> str:
    reason = str(_state_value(decision, "reason", "") or "").strip()
    edits = _state_value(decision, "edits", {}) or {}
    if edits:
        try:
            edits_text = json.dumps(edits, sort_keys=True)
        except TypeError:
            edits_text = str(edits)
        if reason:
            return f"Plan Critic rejected: {reason} Requested edits: {edits_text}"
        return f"Plan Critic rejected: Requested edits: {edits_text}"
    fallback = reason or "revise and try again."
    return f"Plan Critic rejected: {fallback}"


def _planning_feedback_from_human_revise(signal: Any) -> str:
    author = str(_state_value(signal, "author", "") or "").strip().lstrip("@")
    text = str(_state_value(signal, "text", "") or "").strip()
    prefix = f"Human revise by @{author}" if author else "Human revise"
    return f"{prefix}: {text}" if text else prefix


def _planning_attempt_log_entry(
    *,
    source: str,
    attempt: int,
    feedback: str = "",
    rev: int | None = None,
    next_rev: int | None = None,
    author: str = "",
    comment_id: int = 0,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "source": source,
        "attempt": attempt,
    }
    if feedback:
        entry["feedback"] = feedback
    if rev is not None:
        entry["rev"] = rev
    if next_rev is not None:
        entry["next_rev"] = next_rev
    if author:
        entry["author"] = author
    if comment_id:
        entry["comment_id"] = comment_id
    return entry


def _reset_planning_artifacts(state: dict) -> dict:
    fresh = dict(state)
    fresh["stories"] = []
    fresh["spec"] = []
    fresh["work_packages"] = []
    fresh["implementation_brief"] = None
    fresh["review_decision"] = None
    return fresh


def _append_unique(values: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


def _first_brief_wp_id(state: dict) -> str:
    brief = state.get("implementation_brief") or {}
    for wp in _state_value(brief, "work_packages", []) or []:
        wp_id = _state_value(wp, "id", None) or _state_value(wp, "story_id", "")
        if wp_id:
            return str(wp_id)
    for slice_ in state.get("spec") or []:
        wp_id = _state_value(slice_, "id", None) or _state_value(
            slice_, "story_id", ""
        )
        if wp_id:
            return str(wp_id)
    return UNKNOWN_FIXER_TARGET


def _fixer_failure_targets(state: dict) -> dict[str, list[str]]:
    """Return verifier failure targets that count against the Fixer budget."""

    summary = state.get("verify_summary") or {}
    target_wps: list[str] = []
    target_predicates: list[str] = []

    for item in _state_value(summary, "predicate_coverage", []) or []:
        if str(_state_value(item, "status", "")) == "covered":
            continue
        _append_unique(target_wps, _state_value(item, "wp_id", ""))
        _append_unique(target_predicates, _state_value(item, "predicate", ""))

    for finding in state.get("tester_findings") or []:
        _append_unique(target_wps, _state_value(finding, "wp_id", ""))

    # PR C: build-stage discrepancies (Builder blocked, claimed_edits_not_applied,
    # tester_parse_failure, fixer_blocked) also drive Fixer targeting.
    for finding in state.get("reconciliation_findings") or []:
        if (
            str(_state_value(finding, "kind", ""))
            in {
                "builder_blocked",
                "builder_no_action",
                "claimed_edits_not_applied",
                "tester_parse_failure",
                "fixer_blocked",
            }
        ):
            _append_unique(target_wps, _state_value(finding, "wp_id", ""))

    if not target_wps:
        _append_unique(target_wps, state.get("current_slice"))
    if not target_wps:
        _append_unique(target_wps, _first_brief_wp_id(state))

    return {
        "wps": target_wps or [UNKNOWN_FIXER_TARGET],
        "predicates": target_predicates,
    }


def _fixer_budget_exhaustion(state: dict) -> dict[str, Any] | None:
    targets = _fixer_failure_targets(state)
    by_wp = state.get("fixer_attempts_by_wp") or {}
    by_predicate = state.get("fixer_attempts_by_predicate") or {}

    exhausted_wps = [
        wp for wp in targets["wps"] if int(by_wp.get(wp, 0)) >= FIXER_MAX_ATTEMPTS
    ]
    exhausted_predicates = [
        predicate
        for predicate in targets["predicates"]
        if int(by_predicate.get(predicate, 0)) >= FIXER_MAX_ATTEMPTS
    ]
    if not exhausted_wps and not exhausted_predicates:
        return None
    return {
        "reason": "fixer_budget_exhausted",
        "target_wps": exhausted_wps,
        "target_predicates": exhausted_predicates,
    }


def _record_fixer_attempt_delta(state: dict) -> dict[str, Any]:
    targets = _fixer_failure_targets(state)
    by_wp = {
        str(key): int(value)
        for key, value in (state.get("fixer_attempts_by_wp") or {}).items()
    }
    by_predicate = {
        str(key): int(value)
        for key, value in (state.get("fixer_attempts_by_predicate") or {}).items()
    }

    attempt_numbers: list[int] = []
    for wp in targets["wps"]:
        by_wp[wp] = by_wp.get(wp, 0) + 1
        attempt_numbers.append(by_wp[wp])
    for predicate in targets["predicates"]:
        by_predicate[predicate] = by_predicate.get(predicate, 0) + 1
        attempt_numbers.append(by_predicate[predicate])

    return {
        "fixer_attempts_by_wp": by_wp,
        "fixer_attempts_by_predicate": by_predicate,
        "attempt_log": [
            {
                "source": "fixer_attempt",
                "attempt": max(attempt_numbers or [1]),
                "target_wps": targets["wps"],
                "target_predicates": targets["predicates"],
            }
        ],
    }


def _infeasible_predicate_escalation(state: dict) -> dict[str, Any] | None:
    """Short-circuit to `needs_brief_change` when the Tester flagged an
    `infeasible_predicate` finding — the predicate can't be satisfied
    inside the brief's stated constraints, so running the Fixer would
    only burn budget against a planning error.
    """
    findings = state.get("tester_findings") or []
    infeasible = [
        finding
        for finding in findings
        if str(_state_value(finding, "kind", "")) == "infeasible_predicate"
    ]
    if not infeasible:
        return None
    target_wps: list[str] = []
    details: list[str] = []
    for finding in infeasible:
        _append_unique(target_wps, _state_value(finding, "wp_id", ""))
        detail = str(_state_value(finding, "detail", "") or "").strip()
        if detail and detail not in details:
            details.append(detail)
    return {
        "reason": "needs_brief_change",
        "decision": "infeasible_predicate",
        "target_wp": target_wps[0] if target_wps else UNKNOWN_FIXER_TARGET,
        "target_wps": target_wps or [UNKNOWN_FIXER_TARGET],
        "target_predicates": [],
        "summary": (
            "Tester reported infeasible_predicate; brief revision required "
            "before Build can proceed."
        ),
        "details": details,
    }


def _fixer_decision_escalation(state: dict) -> dict[str, Any] | None:
    decision = state.get("fixer_decision") or {}
    decision_value = str(_state_value(decision, "decision", "") or "")
    if decision_value == "needs_brief_change":
        return {
            "reason": "needs_brief_change",
            "decision": decision_value,
            "target_wp": _state_value(decision, "target_wp", ""),
            "target_predicates": list(
                _state_value(decision, "target_predicates", []) or []
            ),
        }
    if decision_value == "cannot_fix":
        return {
            "reason": "fixer_cannot_fix",
            "decision": decision_value,
            "target_wp": _state_value(decision, "target_wp", ""),
            "target_predicates": list(
                _state_value(decision, "target_predicates", []) or []
            ),
        }
    return None


def _fixer_escalation_delta(escalation: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_log": [
            {
                "source": "fixer_escalation",
                **escalation,
            }
        ]
    }


def _snapshot_findings_counts(state: dict) -> tuple[int, int]:
    """Return (tester, reconciliation) finding counts prior to a producer call."""
    return (
        len(state.get("tester_findings") or []),
        len(state.get("reconciliation_findings") or []),
    )


def _drop_stale_findings(state: dict, pre: tuple[int, int]) -> None:
    """Drop pre-existing tester/reconciliation findings after a producer activity.

    `tester_findings` and `reconciliation_findings` use the `add` reducer
    (state.py), so entries produced before the activity persist after merge.
    Once build_stage/fixer_stage has run to address them, those old entries
    are stale by definition — keeping them would re-trigger
    `_blocking_failures` in the next verify cycle (root cause of the
    "verify keeps failing after a successful fixer pass" bug). Slice off
    the pre-existing portion while preserving anything the producer itself
    emitted (e.g., fixer's `undeclared_edits` / `fixer_blocked`,
    build_stage tester reruns). Direct assignment bypasses merge() and is
    replay-deterministic.
    """
    pre_tester, pre_recon = pre
    state["tester_findings"] = (state.get("tester_findings") or [])[pre_tester:]
    state["reconciliation_findings"] = (
        state.get("reconciliation_findings") or []
    )[pre_recon:]


def _human_revise_feedback(decision: Any) -> str:
    """Format a brief-gate revise note as planner feedback."""

    text = str(_state_value(decision, "reason", "") or "").strip()
    return _planning_feedback_from_human_revise(
        type(
            "_HumanReviseSignal",
            (),
            {"author": "human", "text": text},
        )()
    )


@workflow.defn
class DarkFactoryWorkflow:
    def __init__(self) -> None:
        self._brief_gate: GateDecision | None = None
        self._brief_revise: GateDecision | None = None
        self._merge_gate: GateDecision | None = None
        self._merge_action: str | None = None
        self._merge_action_payload: GateDecision | None = None
        self._pending_gate: str | None = None
        self._state: dict = {}

    @workflow.run
    async def run(self, req: RunRequest) -> RunResult:
        # Long-running by design: the verify/Fixer loop can iterate until
        # per-WP or per-predicate repair budgets are exhausted, and
        # `wait_condition(...)` blocks indefinitely on each human-in-the-loop
        # signal, so a single workflow execution can
        # comfortably exceed one hour. We intentionally
        # produce ONE Langfuse trace per execution, grouped by
        # `langfuse.session.id` (= workflow_id) — see README §Tracing. Per
        # task §6, NO custom OTel spans are created in this workflow body;
        # all custom instrumentation lives inside `@activity.defn` functions
        # so it does not have to survive workflow replay.
        wf_id = workflow.info().workflow_id
        agent_tq = f"agent-tq-{wf_id}"
        self._state = init_state(req)
        self._state["wf_id"] = wf_id
        self._state["task_id"] = wf_id
        self._state["feature_branch"] = f"agent/{wf_id}"

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

            planning_feedback = list(self._state.get("planning_feedback") or [])
            while True:
                for attempt in range(PLANNING_MAX_ATTEMPTS):
                    planning_attempt = attempt + 1
                    self._state = merge(
                        self._state,
                        {
                            "planning_attempts": planning_attempt,
                            "planning_max_attempts": PLANNING_MAX_ATTEMPTS,
                            "planning_feedback": planning_feedback,
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
                    decision = self._state.get("review_decision")
                    if _planning_approved(decision):
                        break

                    feedback_item = _planning_feedback_from_decision(decision)
                    planning_feedback = [*planning_feedback, feedback_item]
                    self._state = merge(
                        self._state,
                        {
                            "planning_feedback": planning_feedback,
                            "planning_attempt_log": [
                                _planning_attempt_log_entry(
                                    source="plan_critic_reject",
                                    attempt=planning_attempt,
                                    feedback=feedback_item,
                                )
                            ],
                        },
                    )
                    if planning_attempt >= PLANNING_MAX_ATTEMPTS:
                        return RunResult(
                            status="needs_human",
                            state=self._state,
                            reason="planning_retry_cap",
                        )

                self._pending_gate = "brief"
                await workflow.wait_condition(
                    lambda: self._brief_gate is not None
                    or self._brief_revise is not None
                )
                self._pending_gate = None

                if self._brief_revise is not None:
                    revise_decision = self._brief_revise
                    self._brief_revise = None
                    self._brief_gate = None
                    feedback_item = _human_revise_feedback(revise_decision)
                    planning_feedback = [feedback_item]
                    self._state = merge(
                        self._state,
                        {
                            "planning_attempts": 0,
                            "planning_feedback": planning_feedback,
                            "planning_attempt_log": [
                                _planning_attempt_log_entry(
                                    source="human_revise",
                                    attempt=int(
                                        self._state.get("planning_attempts") or 0
                                    ),
                                    feedback=feedback_item,
                                )
                            ],
                        },
                    )
                    continue

                brief_gate = self._brief_gate
                if brief_gate is None or not brief_gate.approved:
                    return RunResult(
                        status="rejected",
                        state=self._state,
                        reason=brief_gate.reason
                        if brief_gate
                        else "brief gate rejected",
                    )

                self._state = merge(
                    self._state,
                    {
                        "brief_gate_approved": True,
                        "brief_gate_reason": brief_gate.reason,
                    },
                )
                break

            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "build_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=_build_stage_timeout(self._state),
                    heartbeat_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(
                        maximum_attempts=2,
                        non_retryable_error_types=["ParseError"],
                    ),
                ),
            )
            while True:
                # Each verify cycle must evaluate fresh evidence. The
                # `test_results` / `findings` channels use the `add` reducer
                # (state.py), so leaving prior cycles in place would let one
                # stale failure keep `summary.passed=False` forever and feed
                # the verifier_semantic prompt a growing pile of contradictory
                # history. Direct assignment bypasses merge() and is
                # replay-deterministic. `tester_findings` and
                # `reconciliation_findings` are NOT cleared here — they're
                # produced by build/fixer and the FIRST verify must see them.
                # The stale-entry hazard is handled by post-fixer/post-build
                # slicing below.
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
                if summary.get("passed"):
                    break

                escalation = _infeasible_predicate_escalation(self._state)
                if escalation is not None:
                    self._state = merge(
                        self._state,
                        _fixer_escalation_delta(escalation),
                    )
                    return RunResult(
                        status="needs_human",
                        state=self._state,
                        reason=str(escalation["reason"]),
                    )

                escalation = _fixer_budget_exhaustion(self._state)
                if escalation is not None:
                    self._state = merge(
                        self._state,
                        _fixer_escalation_delta(escalation),
                    )
                    return RunResult(
                        status="needs_human",
                        state=self._state,
                        reason=str(escalation["reason"]),
                    )

                self._state = merge(
                    self._state,
                    _record_fixer_attempt_delta(self._state),
                )
                _pre = _snapshot_findings_counts(self._state)
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
                _drop_stale_findings(self._state, _pre)
                escalation = _fixer_decision_escalation(self._state)
                if escalation is not None:
                    self._state = merge(
                        self._state,
                        _fixer_escalation_delta(escalation),
                    )
                    return RunResult(
                        status="needs_human",
                        state=self._state,
                        reason=str(escalation["reason"]),
                    )

            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "pr_creator_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=5),
                ),
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

            while True:
                self._pending_gate = "merge"
                await workflow.wait_condition(
                    lambda: self._merge_gate is not None
                    or self._merge_action is not None
                )
                self._pending_gate = None

                if self._merge_action == "fix":
                    fix_payload = self._merge_action_payload
                    self._merge_action = None
                    self._merge_action_payload = None
                    self._merge_gate = None
                    self._state = merge(
                        self._state,
                        {
                            "human_fix_focus": str(
                                _state_value(fix_payload, "reason", "") or ""
                            ),
                            "human_fix_author": "human",
                        },
                    )
                    self._state = merge(
                        self._state,
                        _record_fixer_attempt_delta(self._state),
                    )
                    _pre = _snapshot_findings_counts(self._state)
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
                    _drop_stale_findings(self._state, _pre)
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
                    self._state = merge(
                        self._state,
                        await workflow.execute_activity(
                            "reviewer_stage",
                            self._state,
                            task_queue=agent_tq,
                            start_to_close_timeout=timedelta(minutes=5),
                        ),
                    )
                    continue

                if self._merge_action == "rebuild":
                    rebuild_payload = self._merge_action_payload
                    self._merge_action = None
                    self._merge_action_payload = None
                    self._merge_gate = None
                    self._state = merge(
                        self._state,
                        {
                            "human_rebuild_focus": str(
                                _state_value(rebuild_payload, "reason", "") or ""
                            ),
                            "human_rebuild_author": "human",
                        },
                    )
                    _pre = _snapshot_findings_counts(self._state)
                    self._state = merge(
                        self._state,
                        await workflow.execute_activity(
                            "build_stage",
                            self._state,
                            task_queue=agent_tq,
                            start_to_close_timeout=_build_stage_timeout(self._state),
                            heartbeat_timeout=timedelta(minutes=5),
                            retry_policy=RetryPolicy(
                                maximum_attempts=2,
                                non_retryable_error_types=["ParseError"],
                            ),
                        ),
                    )
                    _drop_stale_findings(self._state, _pre)
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
                    self._state = merge(
                        self._state,
                        await workflow.execute_activity(
                            "reviewer_stage",
                            self._state,
                            task_queue=agent_tq,
                            start_to_close_timeout=timedelta(minutes=5),
                        ),
                    )
                    continue

                merge_gate = self._merge_gate
                if merge_gate and merge_gate.approved:
                    self._state = merge(
                        self._state,
                        {
                            "gate_approved": True,
                            "merge_gate_approved": True,
                            "merge_gate_reason": merge_gate.reason,
                        },
                    )
                    self._state = merge(
                        self._state,
                        await workflow.execute_activity(
                            "merge_branch",
                            self._state,
                            task_queue=agent_tq,
                            start_to_close_timeout=timedelta(minutes=2),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        ),
                    )
                    return RunResult(status="merged", state=self._state)

                return RunResult(
                    status="rejected",
                    state=self._state,
                    reason=merge_gate.reason
                    if merge_gate
                    else "merge gate rejected",
                )
        finally:
            await workflow.execute_activity(
                "teardown_worker_activity",
                args=[wf_id],
                task_queue=SUPERVISOR_TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=1),
            )

    @workflow.update
    def approve_brief(self, decision: GateDecision) -> None:
        self._brief_gate = GateDecision(
            approved=True,
            reason=decision.reason,
            edits=decision.edits,
        )

    @workflow.update
    def revise_brief(self, decision: GateDecision) -> None:
        self._brief_revise = decision

    @workflow.update
    def reject_brief(self, decision: GateDecision) -> None:
        self._brief_gate = GateDecision(
            approved=False,
            reason=decision.reason,
            edits=decision.edits,
        )

    @workflow.update
    def approve_merge(self, decision: GateDecision) -> None:
        self._merge_gate = GateDecision(
            approved=True,
            reason=decision.reason,
            edits=decision.edits,
        )

    @workflow.update
    def reject_merge(self, decision: GateDecision) -> None:
        self._merge_gate = GateDecision(
            approved=False,
            reason=decision.reason,
            edits=decision.edits,
        )

    @workflow.update
    def trigger_fix(self, decision: GateDecision) -> None:
        self._merge_action = "fix"
        self._merge_action_payload = decision

    @workflow.update
    def trigger_rebuild(self, decision: GateDecision) -> None:
        self._merge_action = "rebuild"
        self._merge_action_payload = decision

    @workflow.update
    def approve_gate(self, decision: GateDecision) -> None:
        # Backwards-compatible alias. Routes the decision to whichever gate is
        # currently waiting; the dedicated `approve_brief` / `approve_merge` /
        # `reject_*` update methods are preferred for new callers.
        if self._pending_gate == "merge":
            self._merge_gate = decision
        elif self._pending_gate == "brief":
            self._brief_gate = decision
        elif self._brief_gate is None:
            self._brief_gate = decision
        else:
            self._merge_gate = decision

    @workflow.query
    def current_state_summary(self) -> dict:
        pending_gate = self._pending_gate
        brief_gate_pending = (
            pending_gate == "brief"
            and self._brief_gate is None
            and self._brief_revise is None
        )
        merge_gate_pending = (
            pending_gate == "merge"
            and self._merge_gate is None
            and self._merge_action is None
        )
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
            "brief_gate_approved": self._state.get("brief_gate_approved", False),
            "merge_gate_approved": self._state.get("merge_gate_approved", False),
            "gate_approved": self._state.get("gate_approved", False),
            "gate_pending": brief_gate_pending or merge_gate_pending,
            "pending_gate": (
                pending_gate if brief_gate_pending or merge_gate_pending else None
            ),
            "brief_gate_pending": brief_gate_pending,
            "merge_gate_pending": merge_gate_pending,
            "current_slice": self._state.get("current_slice"),
            "pr_url": self._state.get("pr_url"),
        }
