"""Unit tests for the loop_breaker PreToolUse hook."""
from __future__ import annotations

import asyncio
from typing import Any

from darkfactory.hooks.loop_breaker import (
    RECONSIDER_TEXT,
    detect_repeating_triplet,
    hash_tool_call,
    make_loop_breaker,
)


def _pre(tool_name: str, **tool_input: Any) -> dict[str, Any]:
    """Build a minimal PreToolUseHookInput payload."""
    return {
        "session_id": "test",
        "transcript_path": "/tmp/transcript",
        "cwd": "/workspace",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": f"{tool_name}-{hash(repr(tool_input)) & 0xffff}",
    }


def _ctx() -> dict[str, Any]:
    return {"signal": None}


def test_triplet_detector_flags_repeat() -> None:
    # A B A B A -> triplets (A,B,A), (B,A,B), (A,B,A) — repeat.
    assert detect_repeating_triplet(["A", "B", "A", "B", "A"], min_repeats=2)


def test_triplet_detector_ignores_non_repeat() -> None:
    assert not detect_repeating_triplet(["A", "B", "C", "D", "E"], min_repeats=2)
    assert not detect_repeating_triplet(["A", "B"], min_repeats=2)  # too short


def test_triplet_detector_min_repeats_three_needs_third_occurrence() -> None:
    # (A,B,A) appears twice in "A B A B A" — not enough for min_repeats=3.
    assert not detect_repeating_triplet(["A", "B", "A", "B", "A"], min_repeats=3)
    # Adding one more cycle makes (A,B,A) appear three times.
    assert detect_repeating_triplet(["A", "B", "A", "B", "A", "B", "A"], min_repeats=3)


def test_hash_is_stable_for_same_call() -> None:
    a = hash_tool_call("Read", {"path": "x.py"})
    b = hash_tool_call("Read", {"path": "x.py"})
    assert a == b


def test_hash_differs_on_args() -> None:
    a = hash_tool_call("Read", {"path": "x.py"})
    b = hash_tool_call("Read", {"path": "y.py"})
    assert a != b


def test_hook_denies_on_rigged_sequence() -> None:
    # Read(x) -> Write(y) -> Read(x) -> Write(y) -> Read(x) — strict mode.
    # Triplet (Read(x), Write(y), Read(x)) appears twice → deny when min_repeats=2.
    hook = make_loop_breaker(window=5, min_repeats=2)
    sequence = [
        _pre("Read", path="x.py"),
        _pre("Write", path="y.py"),
        _pre("Read", path="x.py"),
        _pre("Write", path="y.py"),
        _pre("Read", path="x.py"),
    ]

    async def drive() -> list[dict[str, Any]]:
        return [await hook(item, item["tool_use_id"], _ctx()) for item in sequence]

    outputs = asyncio.run(drive())
    # First four allow (return {}); the fifth denies.
    assert outputs[:4] == [{}, {}, {}, {}]
    final = outputs[4]
    assert final["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert final["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert final["hookSpecificOutput"]["permissionDecisionReason"] == RECONSIDER_TEXT


def test_hook_default_tolerates_one_repeat() -> None:
    # Default min_repeats=3 — a single repeat of the same triplet is allowed
    # so agents can retry an action once before being told to stop.
    hook = make_loop_breaker()
    sequence = [
        _pre("Read", path="x.py"),
        _pre("Write", path="y.py"),
        _pre("Read", path="x.py"),
        _pre("Write", path="y.py"),
        _pre("Read", path="x.py"),
    ]

    async def drive() -> list[dict[str, Any]]:
        return [await hook(item, item["tool_use_id"], _ctx()) for item in sequence]

    outputs = asyncio.run(drive())
    assert outputs == [{}, {}, {}, {}, {}]


def test_hook_silent_on_distinct_calls() -> None:
    hook = make_loop_breaker(window=5)
    sequence = [
        _pre("Read", path="a.py"),
        _pre("Write", path="b.py"),
        _pre("Grep", pattern="foo"),
        _pre("Bash", argv=["ls"]),
        _pre("Read", path="c.py"),
    ]

    async def drive() -> list[dict[str, Any]]:
        return [await hook(item, item["tool_use_id"], _ctx()) for item in sequence]

    outputs = asyncio.run(drive())
    assert outputs == [{}, {}, {}, {}, {}]


def test_hook_window_drops_old_calls() -> None:
    # Eight calls; the early loop should fall out of a window=5 view by the
    # time we feed fresh distinct calls, so the hook stays silent.
    hook = make_loop_breaker(window=5)
    sequence = [
        _pre("Read", path="x.py"),
        _pre("Write", path="y.py"),
        _pre("Read", path="x.py"),
        # New, distinct tail keeps the last-five window free of repeats.
        _pre("Grep", pattern="aaa"),
        _pre("Bash", argv=["ls"]),
        _pre("Read", path="z.py"),
        _pre("Glob", pattern="*.py"),
        _pre("Edit", path="q.py"),
    ]

    async def drive() -> list[dict[str, Any]]:
        return [await hook(item, item["tool_use_id"], _ctx()) for item in sequence]

    outputs = asyncio.run(drive())
    assert all(out == {} for out in outputs)


def test_each_factory_has_independent_state() -> None:
    # Two independent hook instances must not share window history.
    h1 = make_loop_breaker(window=5, min_repeats=2)
    h2 = make_loop_breaker(window=5, min_repeats=2)
    rigged = [
        _pre("Read", path="x.py"),
        _pre("Write", path="y.py"),
        _pre("Read", path="x.py"),
        _pre("Write", path="y.py"),
        _pre("Read", path="x.py"),
    ]

    async def drive() -> tuple[dict[str, Any], dict[str, Any]]:
        # Drive only h1 with the rigged loop; h2 sees a single benign call.
        for item in rigged:
            await h1(item, item["tool_use_id"], _ctx())
        h1_final = await h1(_pre("Write", path="y.py"), "extra", _ctx())
        h2_only = await h2(_pre("Read", path="x.py"), "single", _ctx())
        return h1_final, h2_only

    h1_final, h2_only = asyncio.run(drive())
    # h1 is still in a repeat window so still denies; h2 is a fresh history.
    assert h1_final["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert h2_only == {}
