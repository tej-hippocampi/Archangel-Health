"""Triage Escalation demo tenant (TRIAGEDM) — smoke + role behaviors."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import (  # noqa: E402
    _build_demo_battlecard,
    app,
)
from team_store import TeamStore  # noqa: E402
from tenant_constants import (  # noqa: E402
    ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
    TRIAGE_DEMO_SLUG,
    TRIAGE_DEMO_RN_EMAIL,
    TRIAGE_DEMO_SURGEON_EMAIL,
)
from tenant_jwt import create_tenant_staff_token  # noqa: E402
from triage_demo_seed import (  # noqa: E402
    ensure_triage_demo_staff,
    merge_triage_patients_into_store,
    seed_triage_demo_sqlite,
    triage_patient_blueprint,
)


def _michael_id() -> str:
    for r in triage_patient_blueprint():
        if "obrien" in r["id"].lower():
            return r["id"]
    return "triage_michael_obrien"


def _patricia_id() -> str:
    return "triage_patricia_alvarez"


def _sandra_id() -> str:
    return "triage_sandra_reyes"


def triage_surgeon_token() -> str:
    return create_tenant_staff_token(
        email=TRIAGE_DEMO_SURGEON_EMAIL,
        name="Dr. Eleanor Thompson, MD",
        role="surgeon",
        health_system_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        tenant_slug=TRIAGE_DEMO_SLUG,
        health_system_code="TRIAGEDM",
        is_team_director=True,
    )


def triage_rn_token() -> str:
    return create_tenant_staff_token(
        email=TRIAGE_DEMO_RN_EMAIL,
        name="Maria Castillo, RN",
        role="rn_coordinator",
        health_system_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        tenant_slug=TRIAGE_DEMO_SLUG,
        health_system_code="TRIAGEDM",
        is_team_director=False,
    )


@pytest.fixture()
def triage_seeded():
    """In-process seed for this module's tests."""
    ts: TeamStore = app.state.team_store
    store = app.state.patient_store
    ensure_triage_demo_staff(ts)
    merge_triage_patients_into_store(store, battlecard_fn=_build_demo_battlecard)
    seed_triage_demo_sqlite(ts, store, strategy="reset")
    yield


@pytest.fixture()
def surgeon_client(triage_seeded):
    with TestClient(app, headers={"Authorization": f"Bearer {triage_surgeon_token()}"}) as c:
        yield c


@pytest.fixture()
def rn_client(triage_seeded):
    with TestClient(app, headers={"Authorization": f"Bearer {triage_rn_token()}"}) as c:
        yield c


def test_tenant_login_roundtrip(triage_seeded):
    client = TestClient(app)
    r = client.post(
        "/api/tenant/archangel-triage-demo/auth/login",
        json={"email": TRIAGE_DEMO_SURGEON_EMAIL, "password": "TriageDemo2025!"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("access_token")


def test_patricia_sandra_seed_and_explain(surgeon_client):
    store = app.state.patient_store
    ts: TeamStore = app.state.team_store
    pid_p = _patricia_id()
    pid_s = _sandra_id()

    p_pat = store[pid_p]
    assert p_pat.get("initial_tier") == "TIER_1"
    assert p_pat.get("current_tier") == "TIER_2"
    codes_p = {r["code"] for r in (p_pat.get("initial_tier_reasons") or [])}
    assert codes_p == {"LEJR_BASE"}

    exp_p = surgeon_client.get(f"/api/episodes/{pid_p}/triage-explain").json()
    assert len(exp_p["reasons"]) <= 3
    rc = {r.get("code") for r in exp_p["reasons"]}
    assert rc == {"T96_READINESS_RED", "INTAKE_BMI_SMOKER", "PAM_LEVEL_LOW"}

    p_san = store[pid_s]
    assert p_san.get("initial_tier") == "TIER_1"
    assert p_san.get("current_tier") == "TIER_3"

    row_s = next(x for x in triage_patient_blueprint() if x["id"] == pid_s)
    assert row_s["episode_day"] == 17

    d7 = ts.get_survey_response(pid_s, 7, "postop")
    assert d7 and str(d7.get("tier") or "").upper() == "RED"
    d14 = ts.get_survey_response(pid_s, 14, "postop")
    assert d14 is None

    evs = ts.get_events(pid_s)
    kinds = [e.get("event_type") for e in evs]
    assert kinds.count("platform_opened") >= 10
    assert "diagnosis_video_watched" in kinds
    assert "treatment_video_watched" in kinds
    assert kinds.count("daily_checkin_response") >= 10

    exp_s = surgeon_client.get(f"/api/episodes/{pid_s}/triage-explain").json()
    assert len(exp_s["reasons"]) == 2
    assert "score" not in exp_s
    curated_s = store[pid_s].get("triage_explain_reasons") or []
    assert len(curated_s) == 2
    scodes = [r.get("code") for r in exp_s["reasons"]]
    assert scodes == ["INTRAOP_BP_VASOPRESSOR", "DAY7_RED_SURVEY"]


def test_escalations_tier3_only_for_surgeon(surgeon_client, rn_client):
    r_rn = rn_client.get("/api/escalations")
    assert r_rn.status_code == 200
    tiers_rn = {int(x["tier"]) for x in r_rn.json().get("escalations", [])}
    assert max(tiers_rn) >= 1

    r_s = surgeon_client.get("/api/escalations")
    assert r_s.status_code == 200
    body = r_s.json()
    assert body.get("filter_applied") == "surgeon_tier3_only"
    for e in body.get("escalations", []):
        assert int(e["tier"]) == 3


def test_list_patients_scoped(triage_seeded):
    with TestClient(app, headers={"Authorization": f"Bearer {triage_rn_token()}"}) as c:
        r = c.get("/api/patients")
        assert r.status_code == 200
        body = r.json()
        ids = {p["id"] for p in body.get("patients", [])}
        triage_ids = {row["id"] for row in triage_patient_blueprint()}
        assert triage_ids.issubset(ids)
        for row in body.get("patients", []):
            assert "tierChain" not in row


def test_intraop_michael_tier3_after_lock(surgeon_client):
    pid = _michael_id()
    r0 = surgeon_client.post(
        f"/api/episodes/{pid}/switch-to-postop",
        json={"orEndedAt": "2026-05-10T18:00:00"},
    )
    assert r0.status_code == 200, r0.text

    full_fields = {
        "documented_complication": False,
        "ebl": 850,
        "transfusion_total_units": 2,
        "conversion": "NO",
        "sustained_hypotension": False,
        "vasopressor_requirement": "NONE",
        "significant_arrhythmia": False,
        "or_duration_minutes": 215,
        "difficult_airway": False,
        "net_fluid_balance": 0,
        "anesthesia_type": "GENERAL",
        "complication_description": "Unanticipated dural tear repaired primarily.",
    }

    with TestClient(app, headers={"Authorization": f"Bearer {triage_rn_token()}"}) as rn:
        assert rn.get(f"/api/episodes/{pid}/intraop-form").status_code == 200
        assert rn.patch(
            f"/api/episodes/{pid}/intraop-form",
            json={"fields": full_fields, "fieldOrigins": {}},
        ).status_code == 200
        assert rn.post(f"/api/episodes/{pid}/intraop-form/mark-ready-for-review").status_code == 200

    lock = surgeon_client.post(f"/api/episodes/{pid}/intraop-form/lock")
    assert lock.status_code == 200, lock.text
    assert lock.json().get("reassessment", {}).get("final_tier") == "TIER_3"
