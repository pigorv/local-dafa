from __future__ import annotations

import builtins
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleDescription,
    ScheduleInfo,
    ScheduleIntervalSpec,
    ScheduleListDescription,
    ScheduleListInfo,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
)
from temporalio.service import RPCError, RPCStatusCode

from darkfactory.runtime.issue_poll_workflow import IssuePollWorkflow
from darkfactory.runtime.workflow import SUPERVISOR_TASK_QUEUE
from darkfactory.state import IssuePollRequest


DEFAULT_WATCH_INTERVAL = timedelta(seconds=60)
WATCH_SCHEDULE_ID_PREFIX = "df-watch-"
POLL_WORKFLOW_ID_PREFIX = "df-poll-"

ScheduleAction = Literal[
    "created",
    "updated",
    "paused",
    "resumed",
    "deleted",
    "noop",
]


@dataclass(frozen=True)
class ScheduleAdminResult:
    schedule_id: str
    action: ScheduleAction


@dataclass(frozen=True)
class WatchScheduleSummary:
    schedule_id: str
    paused: bool
    note: str | None
    interval_s: float | None
    next_action_times: tuple[datetime, ...]


def watch_schedule_id(repo: str) -> str:
    owner, name = _repo_parts(repo)
    return f"{WATCH_SCHEDULE_ID_PREFIX}{owner}-{name}"


def poll_workflow_id(repo: str) -> str:
    owner, name = _repo_parts(repo)
    return f"{POLL_WORKFLOW_ID_PREFIX}{owner}-{name}"


async def install_watch_schedule(
    client: Client,
    *,
    repo: str,
    label: str,
    schedule_id: str | None = None,
    interval: timedelta = DEFAULT_WATCH_INTERVAL,
    workflow_id: str | None = None,
    task_queue: str = SUPERVISOR_TASK_QUEUE,
    limit: int = 100,
) -> ScheduleAdminResult:
    """Create or update the Temporal Schedule that fires IssuePollWorkflow."""
    schedule_id = schedule_id or watch_schedule_id(repo)
    workflow_id = workflow_id or poll_workflow_id(repo)
    schedule = _watch_schedule(
        repo=repo,
        label=label,
        limit=limit,
        interval=interval,
        workflow_id=workflow_id,
        task_queue=task_queue,
    )

    try:
        await client.create_schedule(id=schedule_id, schedule=schedule)
        return ScheduleAdminResult(schedule_id=schedule_id, action="created")
    except ScheduleAlreadyRunningError:
        pass
    except RPCError as exc:
        if not _is_rpc_status(exc, RPCStatusCode.ALREADY_EXISTS):
            raise

    handle = client.get_schedule_handle(schedule_id)

    def updater(input) -> ScheduleUpdate:
        current_state = input.description.schedule.state
        return ScheduleUpdate(schedule=replace(schedule, state=current_state))

    await handle.update(updater)
    return ScheduleAdminResult(schedule_id=schedule_id, action="updated")


async def pause(
    client: Client,
    *,
    schedule_id: str,
    note: str | None = None,
) -> ScheduleAdminResult:
    handle = client.get_schedule_handle(schedule_id)
    description = await _describe_or_none(handle)
    if description is None:
        return ScheduleAdminResult(schedule_id=schedule_id, action="noop")
    if description.schedule.state.paused:
        return ScheduleAdminResult(schedule_id=schedule_id, action="noop")

    await handle.pause(note=note)
    return ScheduleAdminResult(schedule_id=schedule_id, action="paused")


async def resume(
    client: Client,
    *,
    schedule_id: str,
    note: str | None = None,
) -> ScheduleAdminResult:
    handle = client.get_schedule_handle(schedule_id)
    description = await _describe_or_none(handle)
    if description is None:
        return ScheduleAdminResult(schedule_id=schedule_id, action="noop")
    if not description.schedule.state.paused:
        return ScheduleAdminResult(schedule_id=schedule_id, action="noop")

    await handle.unpause(note=note)
    return ScheduleAdminResult(schedule_id=schedule_id, action="resumed")


async def uninstall(
    client: Client,
    *,
    schedule_id: str,
) -> ScheduleAdminResult:
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.delete()
    except RPCError as exc:
        if not _is_rpc_status(exc, RPCStatusCode.NOT_FOUND):
            raise
        return ScheduleAdminResult(schedule_id=schedule_id, action="noop")
    return ScheduleAdminResult(schedule_id=schedule_id, action="deleted")


async def list_watch_schedules(
    client: Client,
    *,
    query: str | None = None,
    id_prefix: str | None = None,
    page_size: int = 1000,
) -> builtins.list[WatchScheduleSummary]:
    schedules: builtins.list[WatchScheduleSummary] = []
    async for description in client.list_schedules(query=query, page_size=page_size):
        if id_prefix and not description.id.startswith(id_prefix):
            continue
        schedules.append(_list_summary(description))
    return schedules


async def list(
    client: Client,
    *,
    query: str | None = None,
    id_prefix: str | None = None,
    page_size: int = 1000,
) -> builtins.list[WatchScheduleSummary]:
    return await list_watch_schedules(
        client,
        query=query,
        id_prefix=id_prefix,
        page_size=page_size,
    )


def _watch_schedule(
    *,
    repo: str,
    label: str,
    limit: int,
    interval: timedelta,
    workflow_id: str,
    task_queue: str,
) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            IssuePollWorkflow.run,
            IssuePollRequest(repo=repo, label=label, limit=limit),
            id=workflow_id,
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=interval)]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def _describe_or_none(handle) -> ScheduleDescription | None:
    try:
        return await handle.describe()
    except RPCError as exc:
        if _is_rpc_status(exc, RPCStatusCode.NOT_FOUND):
            return None
        raise


def _list_summary(description: ScheduleListDescription) -> WatchScheduleSummary:
    schedule = description.schedule
    info = description.info
    return WatchScheduleSummary(
        schedule_id=description.id,
        paused=bool(schedule and schedule.state.paused),
        note=schedule.state.note if schedule else None,
        interval_s=_interval_seconds(schedule.spec.intervals) if schedule else None,
        next_action_times=_next_action_times(info),
    )


def _interval_seconds(intervals: Sequence[ScheduleIntervalSpec]) -> float | None:
    if not intervals:
        return None
    return intervals[0].every.total_seconds()


def _next_action_times(info: ScheduleInfo | ScheduleListInfo | None) -> tuple[datetime, ...]:
    if info is None:
        return ()
    return tuple(info.next_action_times)


def _repo_parts(repo: str) -> tuple[str, str]:
    parts = repo.strip().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("watch schedule repo must be owner/name")
    return parts[0], parts[1]


def _is_rpc_status(exc: RPCError, status: RPCStatusCode) -> bool:
    return getattr(exc, "status", None) == status


__all__ = [
    "DEFAULT_WATCH_INTERVAL",
    "WATCH_SCHEDULE_ID_PREFIX",
    "ScheduleAdminResult",
    "WatchScheduleSummary",
    "install_watch_schedule",
    "watch_schedule_id",
    "poll_workflow_id",
    "pause",
    "resume",
    "uninstall",
    "list",
    "list_watch_schedules",
]
