from __future__ import annotations

from typing import Iterable, Literal

from langgraph.graph import END
from langgraph.types import Command

from darkfactory.state import PipelineState, SpecSlice

WorkerName = Literal["backend", "database", "unit_test", "frontend"]

SUPERVISOR_NAME = "builder_supervisor"


def topo_sort(spec: list[SpecSlice]) -> list[str]:
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


def _has_ext(paths: Iterable[str], exts: tuple[str, ...]) -> bool:
    return any(p.lower().endswith(exts) for p in paths)


def _has_path_fragment(paths: Iterable[str], fragments: tuple[str, ...]) -> bool:
    lowered = [p.lower() for p in paths]
    return any(frag in p for p in lowered for frag in fragments)


def route_slice(slice_: SpecSlice) -> WorkerName:
    """Pick a worker for a slice based on the files it touches.

    Rules (first match wins):
      1. Any SQL file or Flyway migration path → database.
      2. Frontend file extensions → frontend.
      3. Slice touches only test files → unit_test.
      4. Otherwise → backend.
    """
    affected = list(slice_.get("affected_files") or [])
    new_files = list(slice_.get("new_files") or [])
    test_files = list(slice_.get("test_files") or [])
    all_paths = affected + new_files + test_files
    source_paths = affected + new_files

    if _has_ext(all_paths, (".sql",)) or _has_path_fragment(all_paths, ("db/migration",)):
        return "database"
    if _has_ext(all_paths, (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte", ".css", ".html")):
        return "frontend"
    if test_files and not source_paths:
        return "unit_test"
    return "backend"


def builder_supervisor_node(state: PipelineState) -> Command:
    """Topo-sort the spec, dispatch the next un-built slice, or finish.

    Completion is detected by matching `patches[*].slice_id` against `build_order`.
    Returns `Command(goto=<worker>)` with `current_slice` pinned, or `Command(goto=END)`
    when every planned slice has at least one patch.
    """
    spec = list(state.get("spec") or [])
    if not spec:
        return Command(goto=END)

    by_id = {s["story_id"]: s for s in spec}
    build_order = state.get("build_order") or topo_sort(spec)
    done = {p.get("slice_id") for p in (state.get("patches") or [])}

    next_slice_id = next((sid for sid in build_order if sid not in done), None)
    if next_slice_id is None:
        return Command(goto=END, update={"build_order": build_order})

    worker = route_slice(by_id[next_slice_id])
    return Command(
        goto=worker,
        update={"build_order": build_order, "current_slice": next_slice_id},
    )
