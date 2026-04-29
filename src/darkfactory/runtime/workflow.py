from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from darkfactory.state import (
    GateDecision,
    RunRequest,
    RunResult,
    init_state,
    merge,
)


VERIFY_RETRY_CAP = 3
SUPERVISOR_TASK_QUEUE = "supervisor-tq"


@workflow.defn
class DarkFactoryWorkflow:
    def __init__(self) -> None:
        self._gate: GateDecision | None = None
        self._state: dict = {}

    @workflow.run
    async def run(self, req: RunRequest) -> RunResult:
        # Long-running by design: the verify retry loop can iterate up to
        # VERIFY_RETRY_CAP times and `wait_condition(self._gate is not None)`
        # blocks indefinitely on a human-in-the-loop signal, so a single
        # workflow execution can comfortably exceed one hour. We intentionally
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

            for attempt in range(VERIFY_RETRY_CAP):
                self._state = merge(self._state, {"verify_retries": attempt})
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
                if summary.get("passed"):
                    break
                self._state = merge(
                    self._state,
                    await workflow.execute_activity(
                        "spec_adjustment_stage",
                        self._state,
                        task_queue=agent_tq,
                        start_to_close_timeout=timedelta(minutes=5),
                    ),
                )
            else:
                return RunResult(status="exhausted_retries", state=self._state)

            self._state = merge(
                self._state,
                await workflow.execute_activity(
                    "code_quality_stage",
                    self._state,
                    task_queue=agent_tq,
                    start_to_close_timeout=timedelta(minutes=5),
                ),
            )

            await workflow.wait_condition(lambda: self._gate is not None)

            if self._gate.approved:
                self._state = merge(self._state, {"gate_approved": True})
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
                        "merge_branch",
                        self._state,
                        task_queue=agent_tq,
                        start_to_close_timeout=timedelta(minutes=2),
                    ),
                )
                return RunResult(status="merged", state=self._state)

            return RunResult(
                status="rejected",
                state=self._state,
                reason=self._gate.reason,
            )
        finally:
            await workflow.execute_activity(
                "teardown_worker_activity",
                args=[wf_id],
                task_queue=SUPERVISOR_TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=1),
            )

    @workflow.update
    def approve_gate(self, decision: GateDecision) -> None:
        self._gate = decision

    @workflow.query
    def current_state_summary(self) -> dict:
        return {
            "verify_retries": self._state.get("verify_retries", 0),
            "verify_summary": self._state.get("verify_summary"),
            "gate_approved": self._state.get("gate_approved", False),
            "gate_pending": self._gate is None,
            "current_slice": self._state.get("current_slice"),
            "pr_url": self._state.get("pr_url"),
        }
