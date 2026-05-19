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

from darkfactory.agents.registry import (
    DEFAULT_MANIFESTS_DIR,
    load_registry,
    role_summaries,
)
from darkfactory.bootstrap import init_observability
from darkfactory.eval.runner import WorkflowRun
from darkfactory.runtime import schedule_admin
from darkfactory.runtime.workflow import DarkFactoryWorkflow
from darkfactory.state import GateDecision, RunRequest, RunResult


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

    eval_parser = subparsers.add_parser("eval")
    eval_parser.add_argument("benchmark", type=Path)
    eval_parser.add_argument("--dataset-name", default="benchmark-prod")
    eval_parser.add_argument("--tag", action="append", default=[])
    eval_parser.add_argument("--run-name", default=None)
    eval_parser.add_argument("--dry-run", action="store_true")
    eval_parser.add_argument("--no-langfuse", action="store_true")
    eval_parser.add_argument("--keep-prs", action="store_true")

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

    roles_parser = subparsers.add_parser("roles")
    roles_subparsers = roles_parser.add_subparsers(
        dest="roles_command",
        required=True,
    )
    roles_subparsers.add_parser("list")

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


async def _wait_for_result_with_eval_gates(handle) -> RunResult:
    result_task = asyncio.create_task(handle.result())
    approved_brief = False
    rejected_merge = False
    while not result_task.done():
        summary = await handle.query(DarkFactoryWorkflow.current_state_summary)
        pending_gate = summary.get("pending_gate")
        if pending_gate == "brief" and not approved_brief:
            await handle.execute_update(
                DarkFactoryWorkflow.approve_brief,
                GateDecision(approved=True, reason="benchmark auto-approved brief"),
            )
            approved_brief = True
        elif pending_gate == "merge" and not rejected_merge:
            await handle.execute_update(
                DarkFactoryWorkflow.reject_merge,
                GateDecision(
                    approved=False,
                    reason="benchmark stopped before merge",
                ),
            )
            rejected_merge = True
        await asyncio.sleep(2)
    return await result_task


async def _start_workflow_and_wait(
    *,
    prompt: str,
    repo: Path,
    workflow_id: str | None = None,
    wait: bool = True,
    auto_eval_gates: bool = False,
) -> WorkflowRun:
    workflow_id = workflow_id or f"darkfactory-{uuid.uuid4().hex}"
    request = RunRequest(
        repo_url=str(repo.resolve()),
        repo_path="/workspace",
        user_request=prompt,
        model_profile=os.environ.get("LLM_MODEL_PROFILE"),
    )
    tracer = trace.get_tracer("darkfactory.cli")
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
        workflow_run_id = handle.first_execution_run_id
        if workflow_run_id:
            span.set_attribute("temporalRunID", workflow_run_id)
        if not wait:
            return WorkflowRun(
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
            )
        if auto_eval_gates:
            result = await _wait_for_result_with_eval_gates(handle)
        else:
            result = await handle.result()
    return WorkflowRun(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        result=result,
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
    run = await _start_workflow_and_wait(
        prompt=args.prompt,
        repo=args.repo,
        workflow_id=workflow_id,
        wait=args.wait,
    )
    _flush_traces()
    print(f"workflow_id={run.workflow_id}")
    if args.wait and run.result is not None:
        print(run.result)
    return 0


async def eval_command(args: argparse.Namespace) -> int:
    from darkfactory.eval.runner import load_dataset, run as run_eval

    cases = load_dataset(args.benchmark)
    selected = cases
    if args.tag:
        wanted = set(args.tag)
        selected = [
            case for case in cases if wanted.intersection(case.get("tags") or [])
        ]
    if args.dry_run:
        print(f"loaded {len(selected)} cases; schema OK")
        return 0
    return await run_eval(
        args.benchmark,
        dataset_name=args.dataset_name,
        tag_filter=args.tag,
        run_name=args.run_name,
        write_langfuse=not args.no_langfuse,
        close_prs=not args.keep_prs,
    )


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


def roles_command(args: argparse.Namespace) -> int:
    if args.roles_command == "list":
        return _roles_list()
    raise SystemExit(f"unknown roles command: {args.roles_command}")


def _roles_list() -> int:
    registry = load_registry(DEFAULT_MANIFESTS_DIR)
    if len(registry) == 0:
        print("0 roles registered (migration not started)")
        return 0
    summaries = role_summaries(registry)
    for index, summary in enumerate(summaries):
        if index:
            print()
        hooks = ", ".join(summary.hook_names) or "-"
        mcp = ", ".join(summary.mcp_servers) or "-"
        print(f"role: {summary.role}")
        print(f"model: {summary.model}")
        print(f"prompt: {summary.prompt_path}")
        allowed_tools_display = (
            "all" if summary.allowed_tool_count < 0
            else summary.allowed_tool_count
        )
        print(f"allowed_tools: {allowed_tools_display}")
        print(f"hooks: {hooks}")
        print(f"mcp: {mcp}")
        print(f"manifest_sha: {summary.manifest_sha}")
        print(f"prompt_sha: {summary.prompt_sha}")
    return 0


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
    if args.command == "eval":
        return asyncio.run(eval_command(args))
    if args.command == "schedule":
        return asyncio.run(schedule_command(args))
    if args.command == "roles":
        return roles_command(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
