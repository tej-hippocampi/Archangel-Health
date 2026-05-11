"""
Cohesion sweep across all four triage stages (PRD README §1–§3).

Asserts the cross-cutting invariants that connect:
  - Initial pre-op tier
  - Pre-op re-tier
  - Intra-op reassessment
  - Post-op re-tier

Specifically:
  1. A single live `current_tier` field on the patient blob carries through
     all four stages.
  2. `apply_intraop_reassessment` snapshots `post_intraop_tier`; the
     post-op stage reads it as an immutable floor.
  3. Direction rules — intra-op + post-op never algorithmically downgrade.
  4. Every stage has its own snapshot table + writes a `event_logs` row.
  5. Admin GET /admin/triage/{initial-tier,preop-retier,intraop,postop}/config
     are all reachable with the configured admin token.
  6. Patient app HTML never directly renders `tier` or `post_intraop_tier`
     values (they are served only on doctor / admin surfaces).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


os.environ.setdefault("AUTH_SECRET", "test-secret-cohesion")


@pytest.fixture()
def admin_token() -> str:
    from routers.admin import _create_token
    return _create_token()


@pytest.fixture()
def client():
    from main import app
    with TestClient(app) as c:
        yield c


# ─── 1. Single Episode.tier field across all four stages ───────────────────


def test_patient_blob_carries_single_current_tier_field():
    """Every stage writes to / reads from `current_tier`. Verified by
    inspecting each stage's compute / persist surface."""
    import triage.intraop.apply as intraop_apply
    import triage.postop.apply as postop_apply
    import triage.initial_tier as initial_tier
    import triage.preop_retier as preop_retier  # noqa: F401

    intraop_src = Path(intraop_apply.__file__).read_text()
    postop_src = Path(postop_apply.__file__).read_text()
    assert "current_tier" in intraop_src
    assert "current_tier" in postop_src
    # Initial-tier produces a TierAssignment that the caller writes onto
    # patient.current_tier — the public API is `assign_initial_tier`.
    assert hasattr(initial_tier, "assign_initial_tier")


# ─── 2. post_intraop_tier wiring ───────────────────────────────────────────


def test_intraop_apply_writes_post_intraop_tier_field():
    """`apply_intraop_reassessment` must snapshot `post_intraop_tier`."""
    src = Path("triage/intraop/apply.py").read_text()
    assert 'patient["post_intraop_tier"]' in src
    assert "post_intraop_tier_at" in src


def test_postop_apply_reads_post_intraop_tier_as_floor():
    src = Path("triage/postop/apply.py").read_text()
    assert "post_intraop_tier" in src
    # The floor must be passed into the algorithm input.
    assert "PostOpReTierInput" in src


def test_apply_postop_retier_never_downgrades_below_floor():
    """Belt-and-suspenders run with TIER_2 floor and clean signals."""
    db_path = os.path.join(tempfile.mkdtemp(), f"team_{uuid.uuid4().hex}.db")
    os.environ["TEAM_DB_PATH"] = db_path
    from team_store import TeamStore
    from datetime import datetime, timedelta
    team_store = TeamStore(db_path=db_path)
    patient_store = {}
    pid = "cohesion-1"
    discharge_at = (datetime.utcnow() - timedelta(days=1)).replace(microsecond=0).isoformat()
    patient_store[pid] = {
        "id": pid,
        "phase": "post_op",
        "current_tier": "TIER_2",
        "post_intraop_tier": "TIER_2",
        "post_intraop_tier_at": discharge_at,
        "discharge_at": discharge_at,
        "anchor_procedure_family": "LEJR",
        "structured_data": {},
    }
    from triage.postop.patient_state import ensure_postop_patient_state
    from triage.postop.apply import apply_postop_retier
    ensure_postop_patient_state(patient_store[pid])

    ev = apply_postop_retier(
        patient_id=pid, patient_store=patient_store,
        team_store=team_store, triggered_by="MANUAL:COHESION",
    )
    assert ev.post_intraop_tier == "TIER_2"
    assert ev.tier_after in ("TIER_2", "TIER_3")  # never below floor


# ─── 3. Audit row pattern — every recompute writes to event_logs ──────────


def test_every_stage_uses_log_event_for_audit():
    """Verifies the four stages share the `team_store.log_event` pattern."""
    intraop = Path("triage/intraop/apply.py").read_text()
    postop = Path("triage/postop/apply.py").read_text()
    preop = Path("triage/preop_retier/apply.py").read_text() if Path("triage/preop_retier/apply.py").exists() else ""
    assert "log_event" in intraop
    assert "log_event" in postop
    if preop:
        assert "log_event" in preop


def test_each_stage_has_its_own_snapshot_table():
    """Distinct stage-specific snapshot tables exist in the schema."""
    schema = Path("team_store.py").read_text()
    # Initial pre-op tier persists onto the in-memory blob (Option B);
    # the other three stages each have their own SQLite snapshot table.
    assert "intraop_reassessments" in schema
    assert "postop_retier_events" in schema
    assert "preop_retier_events" in schema
    assert "pam_assessments" in schema


def test_team_store_documents_option_b_at_top_of_file():
    """Option B / event-stream architecture is documented at the top of
    `team_store.py` so future readers see the choice front-and-center."""
    schema = Path("team_store.py").read_text()
    head = schema[:2000]
    assert "Option B" in head
    assert "event-stream" in head.lower()
    assert "initial_tier_was_hard_escalator" in head
    assert "post_intraop_tier" in head


def test_preop_retier_events_schema_columns_match_spec():
    """Triage Suite Pass 2 §2.2 contract — column layout."""
    from team_store import TeamStore
    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), f"team_{uuid.uuid4().hex}.db")
    ts = TeamStore(db_path=db_path)
    with ts._conn() as conn:
        rows = conn.execute("PRAGMA table_info('preop_retier_events')").fetchall()
    cols = {r[1] for r in rows}
    expected = {
        "id", "episode_id", "triggered_by", "inputs_snapshot_json",
        "initial_tier", "initial_tier_was_hard",
        "computed_delta", "computed_tier",
        "tier_before", "tier_after", "changed", "reasons_json",
        "model_version", "tuning_version", "created_at",
    }
    missing = expected - cols
    assert not missing, f"preop_retier_events missing columns: {missing}"


def test_pam_assessments_schema_columns_match_spec():
    """Triage Suite Pass 2 §2.1 contract — column layout."""
    from team_store import TeamStore
    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), f"team_{uuid.uuid4().hex}.db")
    ts = TeamStore(db_path=db_path)
    with ts._conn() as conn:
        rows = conn.execute("PRAGMA table_info('pam_assessments')").fetchall()
    cols = {r[1] for r in rows}
    expected = {
        "id", "episode_id", "patient_id", "responses_json",
        "raw_sum", "items_scored", "raw_average", "activation_score",
        "level", "is_complete",
        "model_version", "tuning_version", "completed_at", "created_at",
    }
    missing = expected - cols
    assert not missing, f"pam_assessments missing columns: {missing}"


# ─── 4. Admin viewer surfaces all four configs ─────────────────────────────


def test_admin_triage_logic_endpoints_reachable(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    paths = [
        "/admin/triage/initial-tier/config",
        "/admin/triage/preop-retier/config",
        "/admin/triage/intraop/config",
        "/admin/triage/postop/config",
    ]
    for p in paths:
        r = client.get(p, headers=headers)
        assert r.status_code == 200, (p, r.status_code, r.text)
        body = r.json()
        # Every stage must surface a model + tuning version.
        assert "modelVersion" in body
        assert "tuningVersion" in body


def test_admin_postop_config_excludes_wound_photo_entries(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    r = client.get("/admin/triage/postop/config", headers=headers)
    assert r.status_code == 200
    body = r.json()
    weights = body.get("positiveWeights", {})
    audit = body.get("engagementAuditFlags", [])
    for k in list(weights) + list(audit):
        assert "WOUND_PHOTO" not in str(k).upper(), (
            f"Wound-photo entry leaked into post-op config: {k}"
        )
    # The flagged-disabled block must explicitly call out wound photo.
    disabled = body.get("disabledInV1", {})
    assert disabled.get("wound_photo_feature") is False or "wound" in str(disabled).lower()


# ─── 5. Patient app never renders tier / score values ─────────────────────


def test_patient_app_html_never_renders_tier_or_score_strings():
    """Scrape patient-facing UIs for any literal references to
    `current_tier`, `post_intake_tier`, `post_intraop_tier`, `TIER_3`,
    `tier_after`, or `activation_score` / `activation_level`. The
    patient surface must not show any of these (PRD README §3 / §12
    invariant; Triage Suite Pass 3 §4.5 extends the list with
    `post_intake_tier`)."""
    forbidden_in_patient_ui = (
        re.compile(r"current_tier"),
        re.compile(r"post_intake_tier"),
        re.compile(r"post_intraop_tier"),
        re.compile(r"\bTIER_(?:1|2|3)\b"),
        re.compile(r"tier_?after", re.IGNORECASE),
        re.compile(r"activation_score", re.IGNORECASE),
        re.compile(r"activation_level", re.IGNORECASE),
    )
    paths = [
        Path("../frontend/index.html"),
        Path("../frontend/postop.js"),
        Path("../frontend/preop-survey.html"),
        Path("../frontend/preop-survey.js"),
        Path("../frontend/pre-op.js"),
    ]
    for p in paths:
        if not p.exists():
            continue
        content = p.read_text()
        for pat in forbidden_in_patient_ui:
            assert not pat.search(content), f"{p} unexpectedly contains {pat.pattern!r}"


def test_doctor_portal_renders_tier_information():
    """Doctor portal (separate surface) IS allowed to render tier values."""
    p = Path("../frontend/doctor.html")
    if not p.exists():
        pytest.skip("doctor.html not present")
    content = p.read_text()
    # Doctor surface intentionally references TIER_<n> for the badge column.
    assert "TIER_" in content or "current_tier" in content


# ─── 6. Tuning version is stamped on every snapshot row ───────────────────


def test_postop_event_carries_model_and_tuning_version():
    """Every PostOpReTierEvent must carry `model_version` + `tuning_version`."""
    from triage.postop.types import PostOpReTierEvent
    fields = PostOpReTierEvent.model_fields
    assert "model_version" in fields
    assert "tuning_version" in fields
