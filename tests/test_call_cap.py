"""Unit tests for the call_cap PreToolUse hook."""
from __future__ import annotations

import asyncio
from typing import Any

from darkfactory.hooks.call_cap import CALL_CAP_DEFAULT, make_call_cap


def _pre(i: int) -> dict[str, Any]:
    return {
        "session_id": "test",
        "transcript_path": "/tmp/transcript",
        "cwd": "/workspace",
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"path": f"f{i}.py"},
        "tool_use_id": f"call-{i}",
    }


def _ctx() -> dict[str, Any]:
    return {"signal": None}


def test_default_cap_is_25() -> None:
    assert CALL_CAP_DEFAULT == 25


def test_below_cap_allows() -> None:
    hook = make_call_cap(cap=3)

    async def drive() -> list[dict[str, Any]]:
        return [await hook(_pre(i), _pre(i)["tool_use_id"], _ctx()) for i in range(3)]

    outputs = asyncio.run(drive())
    assert outputs == [{}, {}, {}]


def test_overflow_denies() -> None:
    hook = make_call_cap(cap=2)

    async def drive() -> list[dict[str, Any]]:
        return [await hook(_pre(i), _pre(i)["tool_use_id"], _ctx()) for i in range(4)]

    outputs = asyncio.run(drive())
    # First two within cap, allow.
    assert outputs[0] == {}
    assert outputs[1] == {}
    # Third and fourth exceed cap, deny.
    for out in outputs[2:]:
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "cap of 2" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_each_factory_has_independent_counter() -> None:
    h1 = make_call_cap(cap=1)
    h2 = make_call_cap(cap=1)

    async def drive() -> tuple[dict[str, Any], dict[str, Any]]:
        # Burn h1's quota.
        await h1(_pre(0), "0", _ctx())
        h1_over = await h1(_pre(1), "1", _ctx())
        # h2 still has its budget.
        h2_first = await h2(_pre(0), "0", _ctx())
        return h1_over, h2_first

    h1_over, h2_first = asyncio.run(drive())
    assert h1_over["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert h2_first == {}
