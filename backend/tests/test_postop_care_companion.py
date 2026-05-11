"""
Care Companion contributor tests (Triage Suite Pass 3 §3.4).

Covers the four Care-Companion-driven post-op contributors:

  - Hard escalator: tier-3 verdict + open `chat:semantic*` row → TIER_3
  - Soft +2: tier-2 verdict in the last 24h
  - Audit-only: ≥2 chat sessions in 7d
  - Soft +1: zero chat sessions and `days_since_discharge >= 7`

Plus the resolution flow: marking the open escalation `resolved=True`
must drop the hard contributor on the next re-tier.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402
from triage.postop.apply import apply_postop_retier  # noqa: E402
from triage.postop.delta import compute_postop_delta  # noqa: E402
from triage.postop.types import PostOpReTierInput  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed(pid: str, *, days: int = 8) -> None:
    """Seed a post-op patient AND inject a recent
    `postop_video_events` row so the lost-contact-general hard escalator
    (PRD §10.2) doesn't short-circuit before our soft contributors run."""
    discharge_at = (datetime.utcnow() - timedelta(days=days)).isoformat()
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "post_op",
        "specialty": "Orthopedics",
        "current_tier": "TIER_1",
        "post_intraop_tier": "TIER_1",
        "post_intraop_tier_at": datetime.utcnow().replace(microsecond=0).isoformat(),
        "discharge_at": discharge_at,
        "anchor_procedure_family": "LEJR",
        "structured_data": {"procedure_name": "Total Knee Arthroplasty"},
    }
    from triage.postop.patient_state import ensure_postop_patient_state
    ensure_postop_patient_state(app.state.patient_store[pid])

    # Anchor `last_response_at` to "just now" via a postop_video_events
    # row. Without this any patient seeded ≥3 days post-discharge
    # immediately trips LOST_CONTACT_GENERAL on the first re-tier and
    # we never observe the soft contributors we're testing.
    app.state.team_store.record_postop_video_event(
        patient_id=pid,
        video_kind="RED_FLAG",
        event_type="PLAYED",
        session_id="anchor",
        occurred_at=datetime.utcnow().replace(microsecond=0).isoformat(),
        payload={},
    )


# ─── 1. Hard escalator: tier-3 verdict + open chat:semantic* row ────────────


def test_tier3_semantic_with_open_escalation_forces_tier_3(client):
    pid = f"cc_t3_{uuid.uuid4().hex[:8]}"
    _seed(pid, days=3)
    ts = app.state.team_store

    # Persist the LLM verdict event AND the matching open escalation
    # row (mirrors the prod path where `_classify_and_create_escalation`
    # writes both).
    ts.log_event(
        patient_id=pid,
        event_type="care_companion_semantic_escalation",
        payload={"tier": 3, "reason": "wound dehiscence", "trigger_type": "chat:semantic_tier3"},
    )
    ts.create_escalation(
        patient_id=pid,
        tier=3,
        trigger_type="chat:semantic_tier3",
        message="my incision is opening",
        conversation_snapshot=[],
    )

    ev = apply_postop_retier(
        patient_id=pid,
        patient_store=app.state.patient_store,
        team_store=ts,
        triggered_by="TEST:CC_T3",
    )
    assert ev.tier_after == "TIER_3"
    codes = [r.code for r in ev.reasons]
    assert "CARE_COMPANION_RED_FLAG_TIER_3" in codes


# ─── 2. Soft +2: tier-2 verdict within 24h ──────────────────────────────────


def test_tier2_semantic_within_24h_adds_soft_plus_2(client):
    pid = f"cc_t2_{uuid.uuid4().hex[:8]}"
    _seed(pid, days=3)
    ts = app.state.team_store

    ts.log_event(
        patient_id=pid,
        event_type="care_companion_semantic_escalation",
        payload={"tier": 2, "reason": "increasing pain"},
    )

    ev = apply_postop_retier(
        patient_id=pid,
        patient_store=app.state.patient_store,
        team_store=ts,
        triggered_by="TEST:CC_T2",
    )
    codes = [r.code for r in ev.reasons]
    assert "CARE_COMPANION_SEMANTIC_ESCALATION_TIER_2" in codes
    contributor = next(
        r for r in ev.reasons if r.code == "CARE_COMPANION_SEMANTIC_ESCALATION_TIER_2"
    )
    assert contributor.weight == 2
    assert contributor.kind == "POSITIVE"


# ─── 3. Audit-only: ≥2 chat sessions in last 7 days ─────────────────────────


def test_active_last_7d_emits_audit_flag_only(client):
    pid = f"cc_active_{uuid.uuid4().hex[:8]}"
    _seed(pid, days=3)
    ts = app.state.team_store

    for _ in range(3):
        ts.log_event(patient_id=pid, event_type="avatar_chat", payload={"source": "chat"})

    ev = apply_postop_retier(
        patient_id=pid,
        patient_store=app.state.patient_store,
        team_store=ts,
        triggered_by="TEST:CC_ACTIVE",
    )
    codes = [r.code for r in ev.reasons]
    assert "CARE_COMPANION_ACTIVE_LAST_7D" in codes
    audit = next(r for r in ev.reasons if r.code == "CARE_COMPANION_ACTIVE_LAST_7D")
    assert audit.kind == "ENGAGEMENT_AUDIT"
    assert audit.weight == 0


# ─── 4. Soft +1: zero engagement and days_since_discharge >= 7 ──────────────


def test_never_used_by_d7_fires_once(client):
    pid = f"cc_never_{uuid.uuid4().hex[:8]}"
    _seed(pid, days=8)
    ts = app.state.team_store

    ev1 = apply_postop_retier(
        patient_id=pid,
        patient_store=app.state.patient_store,
        team_store=ts,
        triggered_by="TEST:CC_NEVER_1",
    )
    codes1 = [r.code for r in ev1.reasons]
    assert codes1.count("CARE_COMPANION_NEVER_USED_BY_D7") == 1

    # Repeat re-tier — the contributor should still fire exactly once
    # in the new event's reasons (not double-emitted, not stale-cached).
    ev2 = apply_postop_retier(
        patient_id=pid,
        patient_store=app.state.patient_store,
        team_store=ts,
        triggered_by="TEST:CC_NEVER_2",
    )
    codes2 = [r.code for r in ev2.reasons]
    assert codes2.count("CARE_COMPANION_NEVER_USED_BY_D7") == 1


# ─── 5. Resolution: closing the escalation row drops the contributor ───────


def test_resolution_clears_tier3_contributor_on_next_run(client):
    pid = f"cc_resolve_{uuid.uuid4().hex[:8]}"
    _seed(pid, days=3)
    ts = app.state.team_store

    ts.log_event(
        patient_id=pid,
        event_type="care_companion_semantic_escalation",
        payload={"tier": 3, "reason": "wound dehiscence"},
    )
    esc_id = ts.create_escalation(
        patient_id=pid,
        tier=3,
        trigger_type="chat:semantic_tier3",
        message="my incision is opening",
        conversation_snapshot=[],
    )

    ev1 = apply_postop_retier(
        patient_id=pid,
        patient_store=app.state.patient_store,
        team_store=ts,
        triggered_by="TEST:CC_RESOLVE_1",
    )
    assert ev1.tier_after == "TIER_3"

    # Resolve the underlying chat:semantic* row.
    ts.set_escalation_resolved(esc_id, resolved=True)

    ev2 = apply_postop_retier(
        patient_id=pid,
        patient_store=app.state.patient_store,
        team_store=ts,
        triggered_by="TEST:CC_RESOLVE_2",
    )
    codes = [r.code for r in ev2.reasons]
    assert "CARE_COMPANION_RED_FLAG_TIER_3" not in codes, (
        "Closing the chat:semantic* escalation must drop the hard "
        "contributor on the next re-tier."
    )
    # current_tier already at TIER_3 (post-op never auto-downgrades) so
    # the apply layer keeps tier_after at TIER_3 — but the algorithmic
    # output should not include the hard reason. The new event's
    # reasons must reflect that.


# ─── 6. Tuning gate — disabled flag suppresses all CC contributors ──────────


def test_tuning_disabled_suppresses_contributors(client, monkeypatch):
    """When the operator flips `care_companion_enabled` back to False,
    none of the four CC contributors must fire (they read state through
    `_gather_state` which gates on the flag)."""
    from triage.postop import tuning as tuning_mod

    pid = f"cc_off_{uuid.uuid4().hex[:8]}"
    _seed(pid, days=8)
    ts = app.state.team_store
    ts.log_event(
        patient_id=pid,
        event_type="care_companion_semantic_escalation",
        payload={"tier": 3, "reason": "wound"},
    )
    ts.create_escalation(
        patient_id=pid, tier=3, trigger_type="chat:semantic_tier3",
        message="x", conversation_snapshot=[],
    )

    monkeypatch.setitem(tuning_mod.DISABLED_IN_V1, "care_companion_enabled", False)
    ev = apply_postop_retier(
        patient_id=pid,
        patient_store=app.state.patient_store,
        team_store=ts,
        triggered_by="TEST:CC_DISABLED",
    )
    codes = [r.code for r in ev.reasons]
    for c in (
        "CARE_COMPANION_RED_FLAG_TIER_3",
        "CARE_COMPANION_SEMANTIC_ESCALATION_TIER_2",
        "CARE_COMPANION_ACTIVE_LAST_7D",
        "CARE_COMPANION_NEVER_USED_BY_D7",
    ):
        assert c not in codes, f"{c} must not fire when CC tuning is disabled"
