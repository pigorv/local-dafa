"""Unit coverage for Temporal Schedule admin helpers."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from temporalio.client import (
    Schedule,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    ScheduleUpdate,
)
from temporalio.service import RPCError, RPCStatusCode

from darkfactory.runtime import schedule_admin


class _FakeScheduleHandle:
    def __init__(self, client: _FakeScheduleClient, schedule_id: str) -> None:
        self.client = client
        self.schedule_id = schedule_id

    async def describe(self) -> SimpleNamespace:
        schedule = self.client.schedules.get(self.schedule_id)
        if schedule is None:
            raise RPCError("schedule not found", RPCStatusCode.NOT_FOUND, b"")
        return SimpleNamespace(
            id=self.schedule_id,
            schedule=schedule,
            info=SimpleNamespace(next_action_times=()),
        )

    async def update(self, updater: Any) -> None:
        description = await self.describe()
        update = updater(SimpleNamespace(description=description))
        assert isinstance(update, ScheduleUpdate)
        self.client.schedules[self.schedule_id] = update.schedule
        self.client.update_count += 1

    async def pause(self, *, note: str | None = None) -> None:
        schedule = self.client.schedules[self.schedule_id]
        self.client.schedules[self.schedule_id] = replace(
            schedule,
            state=replace(schedule.state, paused=True, note=note),
        )

    async def unpause(self, *, note: str | None = None) -> None:
        schedule = self.client.schedules[self.schedule_id]
        self.client.schedules[self.schedule_id] = replace(
            schedule,
            state=replace(schedule.state, paused=False, note=note),
        )

    async def delete(self) -> None:
        if self.schedule_id not in self.client.schedules:
            raise RPCError("schedule not found", RPCStatusCode.NOT_FOUND, b"")
        del self.client.schedules[self.schedule_id]


class _FakeScheduleClient:
    def __init__(self) -> None:
        self.schedules: dict[str, Schedule] = {}
        self.create_count = 0
        self.update_count = 0

    async def create_schedule(self, *, id: str, schedule: Schedule) -> None:
        self.create_count += 1
        if id in self.schedules:
            raise ScheduleAlreadyRunningError()
        self.schedules[id] = schedule

    def get_schedule_handle(self, schedule_id: str) -> _FakeScheduleHandle:
        return _FakeScheduleHandle(self, schedule_id)


def test_install_watch_schedule_is_idempotent_and_preserves_state() -> None:
    asyncio.run(_run_install_watch_schedule_idempotency_check())


async def _run_install_watch_schedule_idempotency_check() -> None:
    client = _FakeScheduleClient()
    schedule_id = "df-watch-octo-org-octo-repo"

    created = await schedule_admin.install_watch_schedule(
        client,
        repo="octo-org/octo-repo",
        label="df:ready",
        schedule_id=schedule_id,
        interval=timedelta(seconds=45),
        limit=7,
    )

    assert created.action == "created"
    assert created.schedule_id == schedule_id
    assert client.create_count == 1
    assert client.update_count == 0
    assert len(client.schedules) == 1

    await schedule_admin.pause(
        client,
        schedule_id=schedule_id,
        note="operator pause",
    )

    updated = await schedule_admin.install_watch_schedule(
        client,
        repo="octo-org/octo-repo",
        label="df:triaged",
        schedule_id=schedule_id,
        interval=timedelta(seconds=90),
        workflow_id="custom-poll-workflow",
        task_queue="custom-supervisor-tq",
        limit=13,
    )

    assert updated.action == "updated"
    assert updated.schedule_id == schedule_id
    assert client.create_count == 2
    assert client.update_count == 1
    assert len(client.schedules) == 1

    description = await client.get_schedule_handle(schedule_id).describe()
    schedule = description.schedule
    request = schedule.action.args[0]
    assert schedule.state.paused is True
    assert schedule.state.note == "operator pause"
    assert schedule.spec.intervals[0].every == timedelta(seconds=90)
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.action.id == "custom-poll-workflow"
    assert schedule.action.task_queue == "custom-supervisor-tq"
    assert request.repo == "octo-org/octo-repo"
    assert request.label == "df:triaged"
    assert request.limit == 13


def test_pause_resume_uninstall_toggle_schedule_state() -> None:
    asyncio.run(_run_pause_resume_uninstall_state_check())


async def _run_pause_resume_uninstall_state_check() -> None:
    client = _FakeScheduleClient()
    schedule_id = schedule_admin.watch_schedule_id("octo-org/octo-repo")
    await schedule_admin.install_watch_schedule(
        client,
        repo="octo-org/octo-repo",
        label="df:ready",
        schedule_id=schedule_id,
    )
    handle = client.get_schedule_handle(schedule_id)

    paused = await schedule_admin.pause(
        client,
        schedule_id=schedule_id,
        note="maintenance",
    )
    assert paused.action == "paused"
    description = await handle.describe()
    assert description.schedule.state.paused is True
    assert description.schedule.state.note == "maintenance"

    paused_again = await schedule_admin.pause(client, schedule_id=schedule_id)
    assert paused_again.action == "noop"
    description = await handle.describe()
    assert description.schedule.state.paused is True
    assert description.schedule.state.note == "maintenance"

    resumed = await schedule_admin.resume(
        client,
        schedule_id=schedule_id,
        note="watch restored",
    )
    assert resumed.action == "resumed"
    description = await handle.describe()
    assert description.schedule.state.paused is False
    assert description.schedule.state.note == "watch restored"

    resumed_again = await schedule_admin.resume(client, schedule_id=schedule_id)
    assert resumed_again.action == "noop"
    description = await handle.describe()
    assert description.schedule.state.paused is False
    assert description.schedule.state.note == "watch restored"

    deleted = await schedule_admin.uninstall(client, schedule_id=schedule_id)
    assert deleted.action == "deleted"
    assert schedule_id not in client.schedules

    deleted_again = await schedule_admin.uninstall(client, schedule_id=schedule_id)
    assert deleted_again.action == "noop"
