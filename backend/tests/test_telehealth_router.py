from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")
os.environ.setdefault("AUTH_SECRET", "test-auth-secret")

from main import app  # noqa: E402
from tenant_constants import DEMO_HEALTH_SYSTEM_ID  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402


def _seed(*, eligibility: str = "ELIGIBLE") -> str:
    pid = f"th_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "name": "Tele Patient",
        "email": "tp@example.com",
        "health_system_id": DEMO_HEALTH_SYSTEM_ID,
        "eligibility_status": eligibility,
    }
    app.state.team_store.ensure_episode(patient_id=pid)
    return pid


def test_ineligible_blocked():
    pid = _seed(eligibility="INELIGIBLE")
    headers = auth_headers("surgeon", source="landing", email="th-surgeon@test.local")
    with TestClient(app, headers=headers) as client:
        r = client.post("/api/telehealth/encounters", json={"patient_id": pid})
    assert r.status_code == 409


def test_build_claim_g0666_17_min_established():
    pid = _seed()
    headers = auth_headers("surgeon", source="landing", email="th-build@test.local")
    with TestClient(app, headers=headers) as client:
        enc = client.post("/api/telehealth/encounters", json={"patient_id": pid})
        assert enc.status_code == 200
        eid = enc.json()["encounter_id"]
        client.post(f"/api/telehealth/encounters/{eid}/patient-type", json={"patient_type": "ESTABLISHED"})
        client.post(f"/api/telehealth/encounters/{eid}/location", json={"location": "HOME"})
        client.post(
            f"/api/telehealth/encounters/{eid}/end",
            json={"duration_seconds": 17 * 60, "outcome": "COMPLETED"},
        )
        claim = client.post(f"/api/telehealth/encounters/{eid}/build-claim")
    assert claim.status_code == 200, claim.text
    assert claim.json()["hcpcs_code"] == "G0666"


def test_l45_gate_blocks_claim_until_attest():
    pid = _seed()
    headers = auth_headers("surgeon", source="landing", email="th-l45@test.local")
    with TestClient(app, headers=headers) as client:
        enc = client.post("/api/telehealth/encounters", json={"patient_id": pid})
        eid = enc.json()["encounter_id"]
        client.post(f"/api/telehealth/encounters/{eid}/patient-type", json={"patient_type": "ESTABLISHED"})
        client.post(f"/api/telehealth/encounters/{eid}/location", json={"location": "HOME"})
        client.post(
            f"/api/telehealth/encounters/{eid}/end",
            json={"duration_seconds": 47 * 60, "outcome": "COMPLETED"},
        )
        blocked = client.post(f"/api/telehealth/encounters/{eid}/build-claim")
        assert blocked.status_code == 400
        client.post(
            f"/api/telehealth/encounters/{eid}/attest",
            json={"type": "STAFF_ONSITE", "note": "RN present"},
        )
        ok = client.post(f"/api/telehealth/encounters/{eid}/build-claim")
    assert ok.status_code == 200
    assert ok.json()["hcpcs_code"] == "G0668"


def test_no_show_no_claim():
    pid = _seed()
    headers = auth_headers("surgeon", source="landing", email="th-noshow@test.local")
    with TestClient(app, headers=headers) as client:
        enc = client.post("/api/telehealth/encounters", json={"patient_id": pid})
        eid = enc.json()["encounter_id"]
        client.post(f"/api/telehealth/encounters/{eid}/patient-type", json={"patient_type": "ESTABLISHED"})
        client.post(
            f"/api/telehealth/encounters/{eid}/end",
            json={"duration_seconds": 0, "outcome": "NO_SHOW"},
        )
        claim = client.post(f"/api/telehealth/encounters/{eid}/build-claim")
    assert claim.status_code == 400


def test_session_stub_without_daily_key():
    pid = _seed()
    headers = auth_headers("surgeon", source="landing", email="th-stub@test.local")
    with TestClient(app, headers=headers) as client:
        enc = client.post("/api/telehealth/encounters", json={"patient_id": pid})
        eid = enc.json()["encounter_id"]
        sess = client.post(f"/api/telehealth/encounters/{eid}/session")
    assert sess.status_code == 200
    assert "join_url" in sess.json()


def test_telehealth_html_pages_open_without_auth():
    pid = _seed()
    headers = auth_headers("surgeon", source="landing", email="th-html@test.local")
    with TestClient(app, headers=headers) as client:
        enc = client.post("/api/telehealth/encounters", json={"patient_id": pid})
        assert enc.status_code == 200
        eid = enc.json()["encounter_id"]
    with TestClient(app) as client:
        setup = client.get(f"/telehealth/setup/{eid}")
        room = client.get(f"/telehealth/room/{eid}")
    assert setup.status_code == 200, setup.text
    assert room.status_code == 200, room.text
    assert "Pre-visit setup" in setup.text
    assert "Telehealth visit" in room.text


def test_ineligible_blocked_from_eligibility_check_record():
    from eligibility import store as elig_store

    pid = _seed()
    app.state.patient_store[pid].pop("eligibility_status", None)
    check_id = f"chk_{uuid.uuid4().hex[:8]}"
    elig_store.save_check(
        check_id,
        {
            "id": check_id,
            "patient_id": pid,
            "overall_verdict": "INELIGIBLE",
            "status": "DONE",
            "finished_at": "2026-06-01T00:00:00Z",
        },
    )
    app.state.patient_store[pid]["eligibility_check_id"] = check_id
    headers = auth_headers("surgeon", source="landing", email="th-elig-store@test.local")
    with TestClient(app, headers=headers) as client:
        r = client.post("/api/telehealth/encounters", json={"patient_id": pid})
    assert r.status_code == 409


def test_ladder_next_shape():
    pid = _seed()
    headers = auth_headers("surgeon", source="landing", email="th-ladder@test.local")
    with TestClient(app, headers=headers) as client:
        enc = client.post("/api/telehealth/encounters", json={"patient_id": pid})
        eid = enc.json()["encounter_id"]
        client.post(f"/api/telehealth/encounters/{eid}/patient-type", json={"patient_type": "ESTABLISHED"})
        client.post(f"/api/telehealth/encounters/{eid}/start")
        client.post(f"/api/telehealth/encounters/{eid}/heartbeat")
        data = client.get(f"/api/telehealth/encounters/{eid}")
    assert data.status_code == 200
    ladder = data.json().get("ladder_next")
    assert ladder is None or (isinstance(ladder, dict) and "hcpcs" in ladder and "minutes" in ladder)


def test_end_prefers_server_connected_time():
    from datetime import datetime

    pid = _seed()
    headers = auth_headers("surgeon", source="landing", email="th-duration@test.local")
    with TestClient(app, headers=headers) as client:
        enc = client.post("/api/telehealth/encounters", json={"patient_id": pid})
        eid = enc.json()["encounter_id"]
        client.post(f"/api/telehealth/encounters/{eid}/patient-type", json={"patient_type": "ESTABLISHED"})
        client.post(f"/api/telehealth/encounters/{eid}/start")
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        app.state.team_store.update_telehealth_encounter(
            eid,
            connected_seconds=17 * 60,
            last_heartbeat_at=now,
        )
        end = client.post(
            f"/api/telehealth/encounters/{eid}/end",
            json={"duration_seconds": 9999, "outcome": "COMPLETED"},
        )
    assert end.status_code == 200
    assert end.json()["duration_minutes"] == 17
