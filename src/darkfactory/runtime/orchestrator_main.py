from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.bootstrap import init_observability
from darkfactory.runtime.activities import (
    setup_worker_activity,
    teardown_worker_activity,
)
from darkfactory.runtime.workflow import DarkFactoryWorkflow


DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
SUPERVISOR_TASK_QUEUE = "supervisor-tq"


async def main() -> None:
    init_observability("darkfactory-orchestrator")
    address = os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)
    # `always_create_workflow_spans=True` is required when the workflow is
    # started by a process that has no OTel TracerProvider configured (the
    # `darkfactory` CLI, Temporal schedules, the Temporal UI, etc.). Without
    # it, the workflow inbound interceptor sees no inbound parent context
    # and silently skips the `RunWorkflow:<type>` root span — every
    # `workflow.execute_activity` then becomes its own trace root and
    # Langfuse shows N disconnected traces per run instead of one.
    tracing_interceptor = TracingInterceptor(always_create_workflow_spans=True)
    client = await Client.connect(
        address,
        data_converter=pydantic_data_converter,
        interceptors=[tracing_interceptor],
    )
    worker = Worker(
        client,
        task_queue=SUPERVISOR_TASK_QUEUE,
        workflows=[DarkFactoryWorkflow],
        activities=[setup_worker_activity, teardown_worker_activity],
        interceptors=[tracing_interceptor],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
