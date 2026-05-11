"""
Unit tests for `lost_contact_status` (PRD §10.2).

Covers the 24h Tier-3 / 72h general thresholds, the no-prior-activity
path, and the boundary conditions.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.postop.scoring.lost_contact import lost_contact_status  # noqa: E402


_NOW = datetime(2026, 5, 1, 12, 0, 0)


def _hours_ago(h: float) -> str:
    return (_NOW - timedelta(hours=h)).isoformat()


def test_no_prior_activity_no_discharge_general_72h_trips():
    """When neither discharge nor last_response is known, conservatively
    treat as silent."""
    s = lost_contact_status(
        current_tier="TIER_1",
        last_response_at_iso=None,
        now=_NOW,
    )
    assert s.general_72h is True
    assert s.tier3_24h is False


def test_no_prior_activity_tier3_also_trips():
    s = lost_contact_status(
        current_tier="TIER_3",
        last_response_at_iso=None,
        now=_NOW,
    )
    assert s.general_72h is True
    assert s.tier3_24h is True


def test_recent_discharge_no_responses_does_not_trip():
    """A patient discharged 1h ago with no responses is not yet silent."""
    s = lost_contact_status(
        current_tier="TIER_1",
        last_response_at_iso=None,
        discharge_at_iso=_hours_ago(1),
        now=_NOW,
    )
    assert s.general_72h is False
    assert s.tier3_24h is False


def test_old_discharge_no_responses_trips_general():
    """Discharge 96h ago, no responses → 96h silence → general_72h trips."""
    s = lost_contact_status(
        current_tier="TIER_1",
        last_response_at_iso=None,
        discharge_at_iso=_hours_ago(96),
        now=_NOW,
    )
    assert s.general_72h is True


def test_recent_response_overrides_old_discharge():
    """Discharge 96h ago BUT response 1h ago → not silent."""
    s = lost_contact_status(
        current_tier="TIER_3",
        last_response_at_iso=_hours_ago(1),
        discharge_at_iso=_hours_ago(96),
        now=_NOW,
    )
    assert s.tier3_24h is False
    assert s.general_72h is False


def test_recent_activity_no_loss():
    s = lost_contact_status(
        current_tier="TIER_3",
        last_response_at_iso=_hours_ago(1),
        now=_NOW,
    )
    assert s.tier3_24h is False
    assert s.general_72h is False


def test_tier3_24h_threshold_trips():
    s = lost_contact_status(
        current_tier="TIER_3",
        last_response_at_iso=_hours_ago(25),
        now=_NOW,
    )
    assert s.tier3_24h is True
    assert s.general_72h is False  # not yet 72h


def test_tier3_at_exactly_24h_trips():
    s = lost_contact_status(
        current_tier="TIER_3",
        last_response_at_iso=_hours_ago(24),
        now=_NOW,
    )
    assert s.tier3_24h is True


def test_non_tier3_silent_24h_does_not_trip_tier3():
    s = lost_contact_status(
        current_tier="TIER_2",
        last_response_at_iso=_hours_ago(30),
        now=_NOW,
    )
    assert s.tier3_24h is False
    assert s.general_72h is False


def test_general_72h_trips_at_threshold():
    s = lost_contact_status(
        current_tier="TIER_1",
        last_response_at_iso=_hours_ago(72),
        now=_NOW,
    )
    assert s.general_72h is True


def test_general_72h_trips_for_tier3_too():
    s = lost_contact_status(
        current_tier="TIER_3",
        last_response_at_iso=_hours_ago(80),
        now=_NOW,
    )
    assert s.tier3_24h is True
    assert s.general_72h is True


def test_invalid_iso_treated_as_silent():
    s = lost_contact_status(
        current_tier="TIER_3",
        last_response_at_iso="not-a-date",
        now=_NOW,
    )
    assert s.general_72h is True
    assert s.tier3_24h is True
