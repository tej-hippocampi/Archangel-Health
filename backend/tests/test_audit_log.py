"""PRD-5 — tamper-evident ePHI access audit log."""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["TEAM_DB_PATH"] = os.path.join("/tmp", f"audit_{uuid.uuid4().hex}.db")
os.environ.setdefault("ADMIN_USERNAME", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testadminpass")

import patient_session as ps_mod  # noqa: E402
from audit import audit_log  # noqa: E402
from main import app, DEMO_HEALTH_SYSTEM_ID  # noqa: E402
from routers.admin import _create_token as _admin_token  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_audit():
    # Each test starts with a fresh chain (the tamper test deliberately corrupts it).
    with sqlite3.connect(os.environ["TEAM_DB_PATH"]) as conn:
        audit_log._ensure_table(conn)
        conn.execute("DELETE FROM audit_events")
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed(client) -> str:
    pid = f"aud_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "name": "Audit Pt", "health_system_id": DEMO_HEALTH_SYSTEM_ID,
        "pipeline_type": "post_op", "structured_data": {}, "voice_script": "x",
    }
    return pid


# ─── Access recording ────────────────────────────────────────────────────────

def test_records_denied_and_success(client):
    pid = _seed(client)
    client.get(f"/api/patient/{pid}/discharge")  # anonymous -> denied
    client.cookies.set("pt_session", ps_mod.create_patient_session(pid, DEMO_HEALTH_SYSTEM_ID))
    client.get(f"/api/patient/{pid}/discharge")   # patient -> success

    events = audit_log.list_events(limit=20, patient_id=pid)
    outcomes = {(e["actor_type"], e["outcome"]) for e in events}
    assert ("anonymous", "denied") in outcomes
    assert ("patient", "success") in outcomes
    # every event names the patient + the route + an IP, and stores no PHI body
    for e in events:
        assert e["patient_id"] == pid
        assert e["action"] in ("GET", "POST", "PATCH", "DELETE")
        assert "discharge" in e["resource"]
        assert set(e["detail"].keys()) <= {"status"}  # minimum necessary only


def test_non_phi_paths_not_audited(client):
    before = len(audit_log.list_events(limit=1000))
    client.get("/recovery")          # code-entry page, no PHI
    client.get("/docs")              # api docs
    after = len(audit_log.list_events(limit=1000))
    assert after == before


# ─── Hash chain / tamper evidence ────────────────────────────────────────────

def test_chain_verifies_then_tamper_detected(client):
    pid = _seed(client)
    for _ in range(3):
        client.get(f"/api/patient/{pid}/discharge")
    assert audit_log.verify()["ok"] is True

    # Tamper: mutate a stored row directly.
    with sqlite3.connect(os.environ["TEAM_DB_PATH"]) as conn:
        row = conn.execute("SELECT id FROM audit_events ORDER BY id ASC LIMIT 1").fetchone()
        conn.execute("UPDATE audit_events SET outcome='success' WHERE id=?", (row[0],))
    result = audit_log.verify()
    assert result["ok"] is False
    assert result["broken_at_id"] == row[0]


# ─── Admin endpoints ─────────────────────────────────────────────────────────

def test_admin_audit_endpoints(client):
    pid = _seed(client)
    client.get(f"/api/patient/{pid}/discharge")
    h = {"Authorization": f"Bearer {_admin_token()}"}

    r = client.get("/admin/audit/events", params={"patient_id": pid}, headers=h)
    assert r.status_code == 200
    assert any(e["patient_id"] == pid for e in r.json()["events"])

    v = client.get("/admin/audit/verify", headers=h)
    assert v.status_code == 200 and v.json()["ok"] is True


def test_admin_audit_requires_admin(client):
    assert client.get("/admin/audit/events").status_code == 401
    assert client.get("/admin/audit/verify").status_code == 401
