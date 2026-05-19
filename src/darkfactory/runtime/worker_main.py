from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from opentelemetry import trace
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from darkfactory.agents.registry import (
    DEFAULT_MANIFESTS_DIR,
    Registry,
    load_registry,
    set_default_registry,
)
from darkfactory.bootstrap import init_observability
from darkfactory.sdk_diagnostics import install_argv_logging
from darkfactory.runtime.activities import (
    STAGE_ACTIVITIES,
    mark_issue_done_activity,
    ping_activity,
    post_issue_comment_activity,
    swap_state_label_activity,
    upsert_phase_comment_activity,
)


DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
TEMPORAL_TASK_QUEUE_ENV = "TEMPORAL_TASK_QUEUE"
AGENT_TASK_QUEUE_PREFIX = "agent-tq-"
log = logging.getLogger(__name__)


def _task_queue_from_env() -> str:
    task_queue = os.environ.get(TEMPORAL_TASK_QUEUE_ENV)
    if not task_queue:
        raise RuntimeError(f"{TEMPORAL_TASK_QUEUE_ENV} is required")
    return task_queue


def _load_manifest_registry(
    manifests_dir: Path = DEFAULT_MANIFESTS_DIR,
) -> Registry:
    registry = load_registry(manifests_dir)
    set_default_registry(registry)
    hooks = ", ".join(registry.hook_names) or "none"
    log.info("registry: %s roles loaded; hooks: %s", len(registry), hooks)
    return registry


async def main() -> None:
    task_queue = _task_queue_from_env()
    if task_queue.startswith(AGENT_TASK_QUEUE_PREFIX):
        os.environ.setdefault("DARKFACTORY_WF_ID", task_queue[len(AGENT_TASK_QUEUE_PREFIX):])
    init_observability("darkfactory-worker")
    install_argv_logging()  # no-op unless DARKFACTORY_LOG_SDK_ARGV is set
    _load_manifest_registry()
    address = os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)
    tracing_interceptor = TracingInterceptor()
    client = await Client.connect(
        address,
        data_converter=pydantic_data_converter,
        interceptors=[tracing_interceptor],
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        activities=[
            ping_activity,
            upsert_phase_comment_activity,
            swap_state_label_activity,
            post_issue_comment_activity,
            mark_issue_done_activity,
            *STAGE_ACTIVITIES,
        ],
        interceptors=[tracing_interceptor],
    )
    try:
        await worker.run()
    finally:
        # Flush any spans buffered in BatchSpanProcessor before container teardown
        # by teardown_worker_activity, otherwise late activity spans may be lost.
        provider = trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if callable(flush):
            flush()
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()


if __name__ == "__main__":
    asyncio.run(main())
