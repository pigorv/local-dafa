from __future__ import annotations

from typing import Literal

from langgraph.graph import END
from langgraph.types import Command

from darkfactory.state import PipelineState, WorkPackageDict

WorkerName = Literal["builder", "tester"]

SUPERVISOR_NAME = "builder_supervisor"


def topo_sort(spec: list[WorkPackageDict]) -> list[str]:
    """Return slice story_ids in dependency order (Kahn's algorithm).

    Slices not in `spec` referenced via `depends_on` are treated as already-met.
    Ties broken by spec input order to keep runs deterministic.
    """
    ids = [s["story_id"] for s in spec]
    id_set = set(ids)
    pending: dict[str, set[str]] = {
        s["story_id"]: {d for d in (s.get("depends_on") or []) if d in id_set}
        for s in spec
    }
    order: list[str] = []
    remaining = list(ids)
    while remaining:
        ready = [sid for sid in remaining if not pending[sid]]
        if not ready:
            # Cycle: append the rest in declared order so the run still terminates.
            order.extend(remaining)
            break
        order.extend(ready)
        ready_set = set(ready)
        remaining = [sid for sid in remaining if sid not in ready_set]
        for sid in remaining:
            pending[sid] -= ready_set
    return order


def _slice_has_builder_run(state: PipelineState, slice_id: str) -> bool:
    """Did the Builder produce a structured output for this slice?

    PR B: the Builder no longer emits a ``(worker-completion)`` sentinel
    patch when it makes no edits, so the supervisor advances on the
    Builder's declared structured output (any status — ``done``,
    ``no_changes_needed``, ``blocked``) rather than on patch presence.
    """
    return any(
        out.get("wp_id") == slice_id
        for out in (state.get("builder_outputs") or [])
    )


def _slice_has_tester_run(state: PipelineState, slice_id: str) -> bool:
    """Did the Tester produce a structured output for this slice?

    PR C: same migration as Builder — Tester no longer emits a sentinel
    patch, so supervisor advancement reads ``tester_outputs`` instead.
    """
    return any(
        out.get("wp_id") == slice_id
        for out in (state.get("tester_outputs") or [])
    )


def _next_worker_for_slice(
    state: PipelineState,
    slice_: WorkPackageDict,
) -> WorkerName | None:
    """Return the next v2 build-stage worker needed for one slice."""
    slice_id = slice_["story_id"]
    if not _slice_has_builder_run(state, slice_id):
        return "builder"
    if not _slice_has_tester_run(state, slice_id):
        return "tester"
    return None


def builder_supervisor_node(state: PipelineState) -> Command:
    """Topo-sort the spec, dispatch the next un-built slice, or finish.

    Completion is detected by matching Builder/Tester completion patches
    against each `build_order` item.
    Returns `Command(goto=<worker>)` with `current_slice` pinned, or
    `Command(goto=END)` when every planned slice has its required worker
    completion patches.
    """
    spec = list(state.get("spec") or [])
    if not spec:
        return Command(goto=END)

    by_id = {s["story_id"]: s for s in spec}
    build_order = state.get("build_order") or topo_sort(spec)
    for slice_id in build_order:
        worker = _next_worker_for_slice(state, by_id[slice_id])
        if worker is not None:
            return Command(
                goto=worker,
                update={"build_order": build_order, "current_slice": slice_id},
            )

    return Command(goto=END, update={"build_order": build_order})
