"""Frontend Worker — SDK-native no-op stub.

The target app is Java backend only (per ARCHITECTURE.md §5.3 and §10). The
stub exists so the Builder Supervisor's routing code can dispatch to a
``frontend`` slice without a special case; ``run_frontend`` short-circuits
without opening an SDK client and produces no patches.
"""
from __future__ import annotations

ROLE = "frontend"

NO_FRONTEND_NOTE = "no frontend work"


async def run_frontend(state_slice: dict) -> dict:
    return {"patches": [], "note": NO_FRONTEND_NOTE}
