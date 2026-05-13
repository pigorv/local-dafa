"""Build-stage timeout scales by Work Package count.

A flat 15-minute ``start_to_close_timeout`` was tight on multi-WP briefs.
``_build_stage_timeout`` returns ``5 + 4 * wp_count`` minutes, capped at
60. The helper reads ``build_order`` first (the supervisor populates it)
and falls back to ``spec`` length.
"""
from __future__ import annotations

from datetime import timedelta

from darkfactory.runtime.workflow import (
    BUILD_STAGE_MAX_MINUTES,
    _build_stage_timeout,
)


def test_build_stage_timeout_single_wp() -> None:
    # 5 base + 4 per WP × 1 = 9 minutes.
    assert _build_stage_timeout({"build_order": ["wp-1"]}) == timedelta(minutes=9)


def test_build_stage_timeout_three_wp_from_build_order() -> None:
    state = {"build_order": ["wp-1", "wp-2", "wp-3"]}
    # 5 + 4*3 = 17 minutes.
    assert _build_stage_timeout(state) == timedelta(minutes=17)


def test_build_stage_timeout_falls_back_to_spec_length() -> None:
    # build_order not yet populated by the supervisor; spec drives the budget.
    state = {"spec": [{"story_id": f"wp-{i}"} for i in range(5)]}
    # 5 + 4*5 = 25 minutes.
    assert _build_stage_timeout(state) == timedelta(minutes=25)


def test_build_stage_timeout_floor_is_one_wp() -> None:
    # Empty state still gets one WP's worth so the activity isn't strangled.
    assert _build_stage_timeout({}) == timedelta(minutes=9)


def test_build_stage_timeout_capped_at_max() -> None:
    state = {"build_order": [f"wp-{i}" for i in range(20)]}
    # 5 + 4*20 = 85 minutes → capped at BUILD_STAGE_MAX_MINUTES (60).
    assert _build_stage_timeout(state) == timedelta(
        minutes=BUILD_STAGE_MAX_MINUTES
    )
