"""
Unit tests for the conservative-default helper (PRD §5.2 / §7.4).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.intraop import apply_conservative_default, resolve_final_tier  # noqa: E402


def test_conservative_default_t1_to_t2():
    res = apply_conservative_default("TIER_1")
    assert res.proposed_tier == "TIER_2"
    assert res.is_conservative_default is True
    assert res.upgrade_steps == 1
    assert any(r.code == "INTRAOP_FORM_OVERDUE_CONSERVATIVE_DEFAULT" for r in res.reasons)


def test_conservative_default_t2_to_t3():
    res = apply_conservative_default("TIER_2")
    assert res.proposed_tier == "TIER_3"
    assert res.is_conservative_default is True


def test_conservative_default_t3_caps_at_t3():
    res = apply_conservative_default("TIER_3")
    assert res.proposed_tier == "TIER_3"
    assert res.is_conservative_default is True


def test_conservative_default_late_lock_resolution_pre_op_t1():
    """PRD §5.5 Example G + edge case 6: conservative default fires from TIER_1
    → TIER_2, then late lock proposes TIER_1 (uneventful); resolve keeps TIER_2."""
    cd = apply_conservative_default("TIER_1")
    assert cd.proposed_tier == "TIER_2"
    # New tier in effect after the conservative default is TIER_2; later
    # uneventful lock proposes TIER_1; resolve keeps the higher.
    final = resolve_final_tier("TIER_2", "TIER_1")
    assert final == "TIER_2"


def test_conservative_default_late_lock_can_still_upgrade():
    """If the late lock has a hard upgrade, the higher tier wins."""
    cd = apply_conservative_default("TIER_1")
    assert cd.proposed_tier == "TIER_2"
    final = resolve_final_tier("TIER_2", "TIER_3")
    assert final == "TIER_3"


def test_conservative_default_reasons_have_label_and_detail():
    res = apply_conservative_default("TIER_1")
    reason = res.reasons[0]
    assert reason.label.startswith("Intra-op data unavailable")
    assert reason.detail and "24" in reason.detail
