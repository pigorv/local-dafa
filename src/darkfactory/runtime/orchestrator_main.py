from __future__ import annotations

import asyncio
import os

from opentelemetry import trace
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.bootstrap import init_observability
from darkfactory.runtime.activities import (
    detect_approval_signal_activity,
    issue_workflow_capacity_activity,
    list_ready_issues_activity,
    quarantine_closed_issue_activity,
    signal_issue_workflow_activity,
    setup_worker_activity,
    start_or_update_issue_workflow_activity,
    teardown_worker_activity,
)
from darkfactory.runtime.issue_poll_workflow import IssuePollWorkflow
from darkfactory.runtime.issue_workflow import DarkFactoryIssueWorkflow
from darkfactory.runtime.workflow import DarkFactoryWorkflow


DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
SUPERVISOR_TASK_QUEUE = "supervisor-tq"


async def main() -> None:
    init_observability("darkfactory-orchestrator")
    address = os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)
    # Default `always_create_workflow_spans=False`: the contrib interceptor
    # also creates workflow / activity spans during workflow REPLAY when this
    # is True, which produces N copies of every span (~6× the loop body in
    # observed traces). The `darkfactory` CLI initialises OTel via
    # `init_observability("darkfactory-cli")` and starts a parent span before
    # `start_workflow`, so RunWorkflow always inherits a real parent context;
    # the original justification ("workflow started without a tracer") no
    # longer applies. Schedules / Temporal UI starts are not used.
    tracing_interceptor = TracingInterceptor()
    client = await Client.connect(
        address,
        data_converter=pydantic_data_converter,
        interceptors=[tracing_interceptor],
    )
    worker = Worker(
        client,
        task_queue=SUPERVISOR_TASK_QUEUE,
        workflows=[DarkFactoryWorkflow, DarkFactoryIssueWorkflow, IssuePollWorkflow],
        activities=[
            setup_worker_activity,
            teardown_worker_activity,
            list_ready_issues_activity,
            issue_workflow_capacity_activity,
            start_or_update_issue_workflow_activity,
            detect_approval_signal_activity,
            signal_issue_workflow_activity,
            quarantine_closed_issue_activity,
        ],
        interceptors=[tracing_interceptor],
    )
    try:
        await worker.run()
    finally:
        provider = trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if callable(flush):
            flush()
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()


if __name__ == "__main__":
    asyncio.run(main())
