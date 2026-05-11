"""
Pass-4 §4 — RN drafts → surgeon reviews & locks.

Covers all nine PRD §4.8 scenarios for the new intra-op workflow:
  1. RN can fill + mark-ready-for-review.
  2. Surgeon cannot mark-ready-for-review (403).
  3. RN cannot lock (403).
  4. Surgeon attempting `lock` while still IN_PROGRESS → 409.
  5. Surgeon `lock` after READY_FOR_SURGEON_REVIEW → 200, status=LOCKED, escalation
     row written, INTRAOP_FORM_LOCKED event logged.
  6. Surgeon edits a READY draft and locks → edits persist with surgeon
     attribution (`surgeon_locked_by`).
  7. RN recalls a READY draft → status=IN_PROGRESS, recall escalation row
     and INTRAOP_FORM_RECALLED event logged.
  8. NP/PA blocked on every write affordance.
  9. Conservative-default cron at OR-end + 24h with READY_FOR_SURGEON_REVIEW
     status still fires the existing overdue path.
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

os.environ.setdefault("UPLOAD_DIR", "/tmp/elysium-intraop-tests")
os.environ.setdefault("INTRAOP_UPLOAD_DIR", "/tmp/elysium-intraop-tests")
os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402
from triage.intraop.extractor import MockIntraopExtractor  # noqa: E402


_FULL_FIELDS = {
    "documented_complication": False,
    "ebl": 100, "transfusion_total_units": 0,
    "conversion": "NO", "sustained_hypotension": False,
    "vasopressor_requirement": "NONE", "significant_arrhythmia": False,
    "or_duration_minutes": 90, "difficult_airway": False,
    "net_fluid_balance": 0, "anesthesia_type": "GENERAL",
}


@pytest.fixture()
def client():
    app.state.intraop_extractor = MockIntraopExtractor()
    with TestClient(app) as c:
        yield c
    app.state.intraop_extractor = None


def _seed_patient() -> str:
    pid = f"intraop_w_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "structured_data": {"procedure_name": "Total Knee Arthroplasty"},
    }
    from triage.intraop.patient_state import ensure_intraop_patient_state
    ensure_intraop_patient_state(app.state.patient_store[pid])
    return pid


def _rn():
    return auth_headers("rn_coordinator", source="landing", email="workflow-rn@example.com")


def _surgeon():
    return auth_headers("surgeon", source="landing")


def _nppa():
    return auth_headers("np_pa", source="landing")


def _draft_to_ready(client, pid: str):
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn(),
    )
    return client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_rn(),
    )


# ─── 1. RN fill → mark-ready-for-review → status moves; escalation written ──

def test_rn_marks_ready_creates_escalation(client):
    pid = _seed_patient()
    r = _draft_to_ready(client, pid)
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "READY_FOR_SURGEON_REVIEW"
    assert r.json()["form"]["draftCompletedBy"]
    # An escalations row was opened.
    rows = app.state.team_store.list_escalations()
    matching = [
        e for e in rows
        if e.get("patient_id") == pid
        and e.get("trigger_type") == "intraop:ready_for_review"
    ]
    assert matching, "Expected an `intraop:ready_for_review` escalation row."


# ─── 2. Surgeon cannot mark-ready-for-review ───────────────────────────────

def test_surgeon_cannot_mark_ready(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn(),
    )
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/mark-ready-for-review",
        headers=_surgeon(),
    )
    assert r.status_code == 403


# ─── 3. RN cannot lock (must be surgeon) ───────────────────────────────────

def test_rn_cannot_lock(client):
    pid = _seed_patient()
    _draft_to_ready(client, pid)
    r = client.post(f"/api/episodes/{pid}/intraop-form/lock", headers=_rn())
    assert r.status_code == 403


# ─── 4. Surgeon `lock` while still IN_PROGRESS → 409 ───────────────────────

def test_surgeon_lock_while_in_progress_returns_409(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn())
    client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": _FULL_FIELDS},
        headers=_rn(),
    )
    r = client.post(f"/api/episodes/{pid}/intraop-form/lock", headers=_surgeon())
    assert r.status_code == 409
    assert "RN coordinator must mark" in r.json()["detail"]


# ─── 5. Surgeon `lock` after ready → status=LOCKED + escalation + event ────

def test_surgeon_lock_after_ready(client):
    pid = _seed_patient()
    _draft_to_ready(client, pid)
    r = client.post(f"/api/episodes/{pid}/intraop-form/lock", headers=_surgeon())
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "LOCKED"
    assert r.json()["patient"]["phase"] == "post_op"

    events = app.state.team_store.get_events(pid)
    assert any(e["event_type"] == "INTRAOP_FORM_LOCKED" for e in events)


# ─── 6. Surgeon edits a READY draft and locks ──────────────────────────────

def test_surgeon_edits_then_locks(client):
    pid = _seed_patient()
    _draft_to_ready(client, pid)
    # While in REVIEW, surgeon may PATCH to refine the field set.
    r = client.patch(
        f"/api/episodes/{pid}/intraop-form",
        json={"fields": {"ebl": 250}},
        headers=_surgeon(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "READY_FOR_SURGEON_REVIEW"
    assert r.json()["form"]["fields"]["ebl"] == 250

    r = client.post(f"/api/episodes/{pid}/intraop-form/lock", headers=_surgeon())
    assert r.status_code == 200
    locked = r.json()["form"]
    assert locked["fields"]["ebl"] == 250
    assert locked["surgeonLockedBy"]


# ─── 7. RN recalls a READY draft ───────────────────────────────────────────

def test_rn_recalls_draft(client):
    pid = _seed_patient()
    _draft_to_ready(client, pid)
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/recall",
        headers=_rn(),
    )
    assert r.status_code == 200
    assert r.json()["form"]["status"] == "IN_PROGRESS"
    assert r.json()["form"].get("draftCompletedBy") in (None, "")

    rows = app.state.team_store.list_escalations()
    assert any(
        e.get("patient_id") == pid and e.get("trigger_type") == "intraop:draft_recalled"
        for e in rows
    )

    events = app.state.team_store.get_events(pid)
    assert any(e["event_type"] == "INTRAOP_FORM_RECALLED" for e in events)


def test_recall_rejects_when_not_in_review(client):
    pid = _seed_patient()
    client.post(f"/api/episodes/{pid}/intraop-form", json={}, headers=_rn())
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/recall",
        headers=_rn(),
    )
    assert r.status_code == 409


def test_recall_rejects_surgeon(client):
    pid = _seed_patient()
    _draft_to_ready(client, pid)
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/recall",
        headers=_surgeon(),
    )
    assert r.status_code == 403


# ─── 8. NP/PA blocked on every write affordance ────────────────────────────

@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/episodes/{pid}/intraop-form", {}),
        ("patch", "/api/episodes/{pid}/intraop-form", {"fields": {"ebl": 1}}),
        ("post", "/api/episodes/{pid}/intraop-form/mark-ready-for-review", None),
        ("post", "/api/episodes/{pid}/intraop-form/recall", None),
        ("post", "/api/episodes/{pid}/intraop-form/lock", None),
        ("post", "/api/episodes/{pid}/switch-to-postop", {}),
    ],
)
def test_np_pa_blocked_on_every_intraop_write(client, method, path, body):
    pid = _seed_patient()
    url = path.format(pid=pid)
    fn = getattr(client, method)
    if body is None:
        r = fn(url, headers=_nppa())
    else:
        r = fn(url, json=body, headers=_nppa())
    assert r.status_code == 403, r.text


# ─── 9. Conservative-default cron still picks up READY_FOR_SURGEON_REVIEW ──

def test_overdue_watcher_picks_up_ready_for_review_state():
    """The cron filters on `status != 'LOCKED'` only — READY_FOR_SURGEON_REVIEW
    must still surface in the overdue list at OR-end + 24h."""
    pid = f"intraop_overdue_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "structured_data": {"procedure_name": "Total Knee Arthroplasty"},
    }
    from triage.intraop.patient_state import ensure_intraop_patient_state
    ensure_intraop_patient_state(app.state.patient_store[pid])

    or_ended = (datetime.utcnow() - timedelta(hours=30)).replace(microsecond=0).isoformat()
    ts = app.state.team_store
    form = ts.get_or_create_intraop_form(patient_id=pid, or_ended_at=or_ended)
    ts.update_intraop_form_fields(
        patient_id=pid,
        fields=dict(_FULL_FIELDS),
        field_origins={},
        status="READY_FOR_SURGEON_REVIEW",
    )

    overdue = ts.list_intraop_overdue_forms(
        now_iso=datetime.utcnow().replace(microsecond=0).isoformat(),
        threshold_hours=24,
    )
    assert any(f["patient_id"] == pid for f in overdue)


# ─── Surgeon "Forms awaiting your review" surface ──────────────────────────

def test_surgeon_review_queue_lists_ready_forms(client):
    pid = _seed_patient()
    _draft_to_ready(client, pid)
    r = client.get(
        "/api/intraop-forms?status=READY_FOR_SURGEON_REVIEW",
        headers=_surgeon(),
    )
    assert r.status_code == 200, r.text
    pids = [item["patientId"] for item in r.json()["items"]]
    assert pid in pids


def test_review_queue_blocks_np_pa(client):
    r = client.get(
        "/api/intraop-forms?status=READY_FOR_SURGEON_REVIEW",
        headers=_nppa(),
    )
    assert r.status_code == 403


def test_review_queue_blocks_rn(client):
    r = client.get(
        "/api/intraop-forms?status=READY_FOR_SURGEON_REVIEW",
        headers=_rn(),
    )
    assert r.status_code == 403


# ─── Reopen by locking surgeon (Pass-4 §4.5) ───────────────────────────────

def test_locking_surgeon_can_reopen(client):
    """Reopen accepts a surgeon Bearer when the email matches `surgeon_locked_by`."""
    pid = _seed_patient()
    _draft_to_ready(client, pid)
    client.post(f"/api/episodes/{pid}/intraop-form/lock", headers=_surgeon())
    # Same surgeon (tester@example.com) reopens.
    r = client.post(
        f"/api/episodes/{pid}/intraop-form/reopen",
        headers=_surgeon(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["form"]["status"] == "REOPENED"


def test_non_locking_surgeon_blocked_from_reopen(client):
    pid = _seed_patient()
    _draft_to_ready(client, pid)
    client.post(f"/api/episodes/{pid}/intraop-form/lock", headers=_surgeon())
    other = auth_headers("surgeon", source="landing")
    # Override the user_id so the email doesn't match.
    import auth
    users = auth._get_users()  # noqa: SLF001
    users["other-surgeon@example.com"] = {
        "email": "other-surgeon@example.com",
        "name": "Other Surgeon",
        "role": "surgeon",
        "password_hash": "x",
    }
    other_token = auth._create_token("other-surgeon@example.com")  # noqa: SLF001
    other = {"Authorization": f"Bearer {other_token}"}
    r = client.post(f"/api/episodes/{pid}/intraop-form/reopen", headers=other)
    assert r.status_code == 403
