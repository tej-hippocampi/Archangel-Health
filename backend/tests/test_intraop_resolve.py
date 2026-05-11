"""
Unit tests for `resolve_final_tier` (PRD §5.1).

The resolve rule is upward-only — return the higher of (current, proposed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.intraop import resolve_final_tier  # noqa: E402


@pytest.mark.parametrize("current,proposed,expected", [
    ("TIER_1", "TIER_1", "TIER_1"),
    ("TIER_1", "TIER_2", "TIER_2"),
    ("TIER_1", "TIER_3", "TIER_3"),
    ("TIER_2", "TIER_1", "TIER_2"),   # never downgrade
    ("TIER_2", "TIER_2", "TIER_2"),
    ("TIER_2", "TIER_3", "TIER_3"),
    ("TIER_3", "TIER_1", "TIER_3"),   # PRD edge case 18
    ("TIER_3", "TIER_2", "TIER_3"),
    ("TIER_3", "TIER_3", "TIER_3"),
])
def test_upward_only(current, proposed, expected):
    assert resolve_final_tier(current, proposed) == expected
