"""
Unit tests for `apply_delta_upward_only` and `resolve_post_op_tier`
(PRD §10.4 / §10.1).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.postop.mapping import (  # noqa: E402
    apply_delta_upward_only,
    resolve_post_op_tier,
)


# ─── apply_delta_upward_only ────────────────────────────────────────────────


@pytest.mark.parametrize("delta", [0, 1, 2])
def test_below_threshold_returns_floor(delta):
    assert apply_delta_upward_only("TIER_1", delta) == "TIER_1"
    assert apply_delta_upward_only("TIER_2", delta) == "TIER_2"


@pytest.mark.parametrize("delta", [3, 4, 5])
def test_one_step_upgrade(delta):
    assert apply_delta_upward_only("TIER_1", delta) == "TIER_2"
    assert apply_delta_upward_only("TIER_2", delta) == "TIER_3"


@pytest.mark.parametrize("delta", [6, 7, 12, 100])
def test_two_step_upgrade(delta):
    assert apply_delta_upward_only("TIER_1", delta) == "TIER_3"
    # TIER_2 + 2 still TIER_3 (capped).
    assert apply_delta_upward_only("TIER_2", delta) == "TIER_3"


def test_floor_is_already_tier_3():
    """No further upgrade possible; result is always TIER_3."""
    for delta in (0, 3, 6, 100):
        assert apply_delta_upward_only("TIER_3", delta) == "TIER_3"


def test_negative_delta_treated_as_zero():
    assert apply_delta_upward_only("TIER_2", -5) == "TIER_2"


# ─── resolve_post_op_tier ──────────────────────────────────────────────────


def test_resolve_picks_max_rank():
    assert resolve_post_op_tier(floor="TIER_1", current_tier="TIER_2", target_tier="TIER_1") == "TIER_2"
    assert resolve_post_op_tier(floor="TIER_2", current_tier="TIER_1", target_tier="TIER_3") == "TIER_3"


def test_resolve_never_below_floor():
    """The floor (post_intraop_tier) is the lower bound."""
    assert resolve_post_op_tier(floor="TIER_2", current_tier="TIER_2", target_tier="TIER_1") == "TIER_2"


def test_resolve_never_below_current():
    """Most-conservative — never below current."""
    assert resolve_post_op_tier(floor="TIER_1", current_tier="TIER_3", target_tier="TIER_1") == "TIER_3"
