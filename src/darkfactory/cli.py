from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from pathlib import Path
from typing import Sequence

from opentelemetry import trace
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter

from darkfactory.bootstrap import init_observability
from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import RunRequest


DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
SUPERVISOR_TASK_QUEUE = "supervisor-tq"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="darkfactory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("prompt", nargs="?")
    run_parser.add_argument("--repo", type=Path, default=Path.cwd())
    run_parser.add_argument("--hello-worker", action="store_true")
    run_parser.add_argument("--workflow-id", default=None)
    run_parser.add_argument(
        "--wait", dest="wait", action="store_true", default=True
    )
    run_parser.add_argument("--no-wait", dest="wait", action="store_false")

    return parser


async def _connect_client() -> Client:
    address = os.environ.get("TEMPORAL_ADDRESS", DEFAULT_TEMPORAL_ADDRESS)
    return await Client.connect(
        address,
        data_converter=pydantic_data_converter,
        interceptors=[TracingInterceptor()],
    )


async def run_command(args: argparse.Namespace) -> int:
    # Init observability so the CLI process has a TracerProvider; the
    # TracingInterceptor on the client then has an active span context to
    # inject into the workflow start headers, and the supervisor's
    # RunWorkflow span becomes a child of this CLI span.
    init_observability("darkfactory-cli")
    tracer = trace.get_tracer("darkfactory.cli")

    if args.hello_worker:
        workflow_id = args.workflow_id or f"darkfactory-hello-worker-{uuid.uuid4().hex}"
        repo = args.repo.resolve()
        request = RunRequest(
            repo_url=str(repo),
            repo_path="/workspace",
            user_request="hello from darkfactory worker",
            model_profile=None,
        )
        with tracer.start_as_current_span("darkfactory.cli.run") as span:
            span.set_attribute("langfuse.session.id", workflow_id)
            span.set_attribute("session.id", workflow_id)
            span.set_attribute("workflow.id", workflow_id)
            client = await _connect_client()
            result = await client.execute_workflow(
                DarkFactoryWorkflow.run,
                request,
                id=workflow_id,
                task_queue=SUPERVISOR_TASK_QUEUE,
            )
        _flush_traces()
        print(f"workflow_id={workflow_id}")
        print(result)
        return 0

    if not args.prompt:
        raise SystemExit("darkfactory run requires a prompt unless --hello-worker is set")

    workflow_id = args.workflow_id or f"darkfactory-{uuid.uuid4().hex}"
    repo = args.repo.resolve()
    request = RunRequest(
        repo_url=str(repo),
        repo_path="/workspace",
        user_request=args.prompt,
        model_profile=os.environ.get("LLM_MODEL_PROFILE"),
    )
    with tracer.start_as_current_span("darkfactory.cli.run") as span:
        span.set_attribute("langfuse.session.id", workflow_id)
        span.set_attribute("session.id", workflow_id)
        span.set_attribute("workflow.id", workflow_id)
        client = await _connect_client()
        handle = await client.start_workflow(
            DarkFactoryWorkflow.run,
            request,
            id=workflow_id,
            task_queue=SUPERVISOR_TASK_QUEUE,
        )
        print(f"workflow_id={workflow_id}")

        if not args.wait:
            _flush_traces()
            return 0

        result = await handle.result()
    _flush_traces()
    print(result)
    return 0


def _flush_traces() -> None:
    """Flush BatchSpanProcessor before the CLI exits so spans aren't lost."""
    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if callable(flush):
        flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return asyncio.run(run_command(args))

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
