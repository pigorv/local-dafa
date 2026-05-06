from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv
from opentelemetry import trace
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter

from darkfactory.bootstrap import init_observability
from darkfactory.runtime import schedule_admin
from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import RunRequest


DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
SUPERVISOR_TASK_QUEUE = "supervisor-tq"
DEFAULT_WATCH_LABEL = "df:ready"


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

    schedule_parser = subparsers.add_parser("schedule")
    schedule_subparsers = schedule_parser.add_subparsers(
        dest="schedule_command",
        required=True,
    )

    install_parser = schedule_subparsers.add_parser("install")
    install_parser.add_argument("--repo", default=None)
    install_parser.add_argument("--label", default=DEFAULT_WATCH_LABEL)
    install_parser.add_argument(
        "--interval",
        type=_parse_interval,
        default=schedule_admin.DEFAULT_WATCH_INTERVAL,
    )
    install_parser.add_argument("--limit", type=int, default=100)
    install_parser.add_argument("--schedule-id", default=None)
    install_parser.add_argument("--workflow-id", default=None)

    for command in ("pause", "resume", "uninstall"):
        command_parser = schedule_subparsers.add_parser(command)
        target = command_parser.add_mutually_exclusive_group(required=False)
        target.add_argument("--repo")
        target.add_argument("--schedule-id")
        if command in {"pause", "resume"}:
            command_parser.add_argument("--note", default=None)

    list_parser = schedule_subparsers.add_parser("list")
    list_parser.add_argument("--query", default=None)
    list_parser.add_argument(
        "--all",
        action="store_true",
        help="include schedules outside the df-watch-* namespace",
    )
    list_parser.add_argument("--page-size", type=int, default=1000)

    return parser


def _parse_interval(raw: str) -> timedelta:
    value = raw.strip().lower()
    if not value:
        raise argparse.ArgumentTypeError("interval must not be empty")

    suffix = value[-1]
    multiplier = {"s": 1, "m": 60, "h": 60 * 60}.get(suffix)
    number = value[:-1] if multiplier else value

    try:
        amount = float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "interval must be seconds or use s, m, or h suffix"
        ) from exc

    seconds = amount * (multiplier or 1)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("interval must be greater than zero")
    return timedelta(seconds=seconds)


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


async def schedule_command(args: argparse.Namespace) -> int:
    init_observability("darkfactory-cli")
    tracer = trace.get_tracer("darkfactory.cli")

    with tracer.start_as_current_span("darkfactory.cli.schedule") as span:
        span.set_attribute("schedule.command", args.schedule_command)
        client = await _connect_client()

        if args.schedule_command == "install":
            repo = _resolve_watch_repo(args.repo)
            span.set_attribute("schedule.repo", repo)
            result = await schedule_admin.install_watch_schedule(
                client,
                repo=repo,
                label=args.label,
                interval=args.interval,
                limit=args.limit,
                schedule_id=args.schedule_id,
                workflow_id=args.workflow_id,
            )
            _print_schedule_result(result)
            _flush_traces()
            return 0

        if args.schedule_command == "pause":
            schedule_id = _schedule_target_id(args)
            result = await schedule_admin.pause(
                client,
                schedule_id=schedule_id,
                note=args.note,
            )
            _print_schedule_result(result)
            _flush_traces()
            return 0

        if args.schedule_command == "resume":
            schedule_id = _schedule_target_id(args)
            result = await schedule_admin.resume(
                client,
                schedule_id=schedule_id,
                note=args.note,
            )
            _print_schedule_result(result)
            _flush_traces()
            return 0

        if args.schedule_command == "uninstall":
            schedule_id = _schedule_target_id(args)
            result = await schedule_admin.uninstall(
                client,
                schedule_id=schedule_id,
            )
            _print_schedule_result(result)
            _flush_traces()
            return 0

        if args.schedule_command == "list":
            summaries = await schedule_admin.list(
                client,
                query=args.query,
                id_prefix=None if args.all else schedule_admin.WATCH_SCHEDULE_ID_PREFIX,
                page_size=args.page_size,
            )
            _print_schedule_list(summaries)
            _flush_traces()
            return 0

    raise SystemExit(f"unknown schedule command: {args.schedule_command}")


def _schedule_target_id(args: argparse.Namespace) -> str:
    if args.schedule_id:
        return args.schedule_id
    return schedule_admin.watch_schedule_id(_resolve_watch_repo(args.repo))


def _resolve_watch_repo(cli_value: str | None) -> str:
    repo = cli_value or os.environ.get("DF_WATCH_REPO")
    if not repo:
        raise SystemExit(
            "darkfactory schedule requires --repo or DF_WATCH_REPO in the environment"
        )
    return repo


def _print_schedule_result(result: schedule_admin.ScheduleAdminResult) -> None:
    print(f"schedule_id={result.schedule_id}")
    print(f"action={result.action}")


def _print_schedule_list(
    summaries: Sequence[schedule_admin.WatchScheduleSummary],
) -> None:
    if not summaries:
        print("no schedules")
        return

    for summary in summaries:
        paused = "true" if summary.paused else "false"
        interval = (
            "-"
            if summary.interval_s is None
            else f"{summary.interval_s:g}s"
        )
        next_times = (
            ", ".join(dt.isoformat() for dt in summary.next_action_times)
            or "-"
        )
        note = summary.note or "-"
        print(
            f"{summary.schedule_id}\tpaused={paused}\t"
            f"interval={interval}\tnext={next_times}\tnote={note}"
        )


def _flush_traces() -> None:
    """Flush BatchSpanProcessor before the CLI exits so spans aren't lost."""
    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if callable(flush):
        flush()


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return asyncio.run(run_command(args))
    if args.command == "schedule":
        return asyncio.run(schedule_command(args))

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
