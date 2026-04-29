from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.bootstrap import init_observability
from darkfactory.runtime.activities import STAGE_ACTIVITIES, ping_activity


DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
TEMPORAL_TASK_QUEUE_ENV = "TEMPORAL_TASK_QUEUE"
AGENT_TASK_QUEUE_PREFIX = "agent-tq-"


def _task_queue_from_env() -> str:
    task_queue = os.environ.get(TEMPORAL_TASK_QUEUE_ENV)
    if not task_queue:
        raise RuntimeError(f"{TEMPORAL_TASK_QUEUE_ENV} is required")
    return task_queue


async def main() -> None:
    task_queue = _task_queue_from_env()
    if task_queue.startswith(AGENT_TASK_QUEUE_PREFIX):
        os.environ.setdefault("DARKFACTORY_WF_ID", task_queue[len(AGENT_TASK_QUEUE_PREFIX):])
    init_observability("darkfactory-worker")
    address = os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)
    # See comment in `orchestrator_main.py`. The agent worker doesn't host a
    # workflow definition, but we mirror the flag for symmetry — and so any
    # local-only workflow that ever runs on this worker (none today) inherits
    # the same behaviour.
    tracing_interceptor = TracingInterceptor(always_create_workflow_spans=True)
    client = await Client.connect(
        address,
        data_converter=pydantic_data_converter,
        interceptors=[tracing_interceptor],
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        activities=[ping_activity, *STAGE_ACTIVITIES],
        interceptors=[tracing_interceptor],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
