"""PRD B Phases 4–5 — onboarding capture + the admin verification queue.

Launch-day property under test throughout: every failure path degrades to a
'pending' queue entry — never a rejection, never a 500 on the signup form.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

import routers.onboarding as onboarding_module
from asclepius import credentialing

VALID_NPI = "1234567893"  # canonical Luhn-valid NPI

_npi_counter = 0


def _fresh_npi() -> str:
    """A unique, checksum-valid NPI per test. The suite shares one DB, and the
    30-day NPI cache is REAL behavior — reusing one NPI across tests would
    (correctly) serve cached registry answers and mask the path under test."""
    global _npi_counter
    _npi_counter += 1
    base = f"1{_npi_counter:07d}9"  # 9 digits
    from asclepius.credentialing import npi_checksum_ok
    for check in "0123456789":
        if npi_checksum_ok(base + check):
            return base + check
    raise AssertionError("unreachable: some check digit always validates")


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_email(monkeypatch):
    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)

    async def _stub_send(*_args, **_kwargs):
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _stub_send)


@pytest.fixture(autouse=True)
def _no_real_nppes(monkeypatch):
    """Default every test to a deterministic NPPES answer; individual tests
    override. Nothing in this suite may touch the network."""
    monkeypatch.setattr(
        credentialing, "fetch_npi_record",
        lambda npi, timeout=6.0: {"status": "found", "record": _nppes_record(),
                                  "reason": None},
    )


def _nppes_record(last_name="Patel", status="A", taxonomy_desc="Nephrology"):
    return {
        "number": VALID_NPI,
        "enumeration_type": "NPI-1",
        "basic": {"first_name": "Tej", "last_name": last_name, "credential": "M.D.",
                  "status": status, "enumeration_date": "2010-02-01"},
        "taxonomies": [{"code": "207RN0300X", "desc": taxonomy_desc, "primary": True,
                        "state": "CA", "license": "A1"}],
        "addresses": [{"address_purpose": "LOCATION", "city": "LA", "state": "CA"}],
    }


def _creds(**overrides):
    base = {
        "fullLegalName": "Dr. Tej Patel",
        "npi": _fresh_npi(),
        "phone": "+1 555 010 7788",
        "linkedinUrl": "https://www.linkedin.com/in/tejpatel",
        "degree": "MD",
        "primarySpecialty": "Nephrology",
        "yearsInActivePractice": "12",
        "boardCertifications": [
            {"board": "ABIM", "specialty": "Internal Medicine",
             "subspecialty": "Nephrology", "active": True}
        ],
    }
    base.update(overrides)
    return base


ATTS = {
    "consentCredentialShare": True,
    "attestIndependentJudgment": True,
    "ipAssignment": True,
    "noPhi": True,
    "signedInitials": "TP",
}


def _seed_verified(client: TestClient):
    ts = client.app.state.team_store
    invite = ts.create_health_system_invite(invite_base_url="http://localhost:5173")
    token = invite["onboarding_url"].rsplit("/", 1)[-1]
    hs_id = invite["health_system_id"]
    director_email = f"dir_{uuid.uuid4().hex[:8]}@nephrology-associates.com"
    ts.update_health_system_director_identity(
        hs_id, first_name="Tej", last_name="Patel", email=director_email)
    with sqlite3.connect(ts.db_path) as conn:
        conn.execute("UPDATE health_systems SET onboarding_step = 2 WHERE id = ?", (hs_id,))
        conn.commit()
    return token, hs_id, director_email


def _run_director_signup(client: TestClient, creds=None):
    creds = creds or _creds()
    token, hs_id, director_email = _seed_verified(client)
    assert client.post("/api/onboarding/select-product",
                       json={"token": token, "product": "asclepius"}).status_code == 200
    assert client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": token, "org_name": "Northridge Nephrology",
              "specialty": "Nephrology", "phone": "(555) 123-4567"},
    ).status_code == 200
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": token, "credentials": creds}
                       ).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": token, "attestations": ATTS}).status_code == 200
    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 200, r.text
    return token, hs_id, director_email, creds


# ─── Phase 4: signup capture ─────────────────────────────────────────────────
def test_signup_captures_identity_and_verifies_npi(client: TestClient):
    _, _, email, creds = _run_director_signup(client)
    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["phone"] == "+1 555 010 7788"
    assert u["linkedin_url"] == "https://www.linkedin.com/in/tejpatel"
    assert u["email_domain_class"] == "business"
    assert u["npi"] == creds["npi"]
    assert u["npi_verified"] == 1
    payload = json.loads(u["npi_payload_json"])
    assert payload["result"] == "verified"
    assert payload["record"]["taxonomy"]["desc"] == "Nephrology"
    assert u["verification_status"] == "pending"   # a human still decides
    assert u["tier"] is None                       # never auto-assigned


def test_signup_lands_event_in_provenance_log(client: TestClient):
    _, _, email, creds = _run_director_signup(client)
    store = client.app.state.asclepius_store
    u = store.get_user_by_email(email)
    events = store.list_events(entity_type="user", entity_id=u["id"])
    assert any(e["event_type"] == "verification_pending" for e in events)


def test_nppes_down_degrades_to_pending_not_500(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        credentialing, "fetch_npi_record",
        lambda npi, timeout=6.0: {"status": "unavailable", "record": None,
                                  "reason": "rate_limited"})
    _, _, email, _ = _run_director_signup(client)   # signup completes: no 500
    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["npi_verified"] is None             # could not check ≠ does not exist
    assert u["npi_checked_at"] is None
    assert u["verification_status"] == "pending"


def test_verifier_crash_degrades_to_pending_not_500(client: TestClient, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("catastrophic")
    monkeypatch.setattr(credentialing, "verify_npi", _boom)
    _, _, email, _ = _run_director_signup(client)   # still 200
    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert json.loads(u["npi_payload_json"])["result"] == "unavailable"
    assert u["verification_status"] == "pending"


def test_family_name_mismatch_recorded_as_mismatch(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        credentialing, "fetch_npi_record",
        lambda npi, timeout=6.0: {"status": "found",
                                  "record": _nppes_record(last_name="Someone"),
                                  "reason": None})
    _, _, email, creds = _run_director_signup(client)
    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["npi_verified"] == 0
    assert json.loads(u["npi_payload_json"])["result"] == "mismatch"
    assert u["verification_status"] == "pending"  # review flag, not a rejection


def test_reonboard_never_downgrades_an_approved_user(client: TestClient):
    _, _, email, creds = _run_director_signup(client)
    store = client.app.state.asclepius_store
    u = store.get_user_by_email(email)
    store.set_verification_status(u["id"], "approved")
    # the same physician re-runs onboarding (idempotent upsert path)
    onboarding_module._run_signup_verification(store, store.get_user_by_id(u["id"]), _creds())
    assert store.get_user_by_id(u["id"])["verification_status"] == "approved"


# ─── Phase 4: the pending gate — a pending user cannot draw tasks ────────────
def test_pending_evaluator_cannot_draw_tasks_or_use_portal(client: TestClient):
    store = fresh_store()
    u = make_user(store)
    store.set_verification_status(u["id"], "pending")
    r = client.get("/api/asclepius/tasks/next", headers=headers_for(u))
    assert r.status_code == 403
    assert "verification" in r.json()["detail"].lower()
    assert client.get("/api/asclepius/auth/me", headers=headers_for(u)).status_code == 403


def test_rejected_evaluator_is_blocked_with_distinct_message(client: TestClient):
    store = fresh_store()
    u = make_user(store)
    store.set_verification_status(u["id"], "rejected")
    r = client.get("/api/asclepius/auth/me", headers=headers_for(u))
    assert r.status_code == 403
    assert "not approved" in r.json()["detail"].lower()


def test_legacy_null_status_user_is_untouched(client: TestClient):
    store = fresh_store()
    u = make_user(store)  # verification_status stays NULL
    assert client.get("/api/asclepius/auth/me", headers=headers_for(u)).status_code == 200


def test_approved_evaluator_passes(client: TestClient):
    store = fresh_store()
    u = make_user(store)
    store.set_verification_status(u["id"], "approved")
    assert client.get("/api/asclepius/auth/me", headers=headers_for(u)).status_code == 200


def test_pending_admin_is_never_locked_out(client: TestClient):
    store = fresh_store()
    admin = make_user(store, role="admin")
    store.set_verification_status(admin["id"], "pending")
    assert client.get("/api/asclepius/auth/me", headers=headers_for(admin)).status_code == 200


# ─── Phase 4: CV upload endpoint ─────────────────────────────────────────────
def test_cv_upload_roundtrip_through_signup(client: TestClient):
    token, _, email = (None, None, None)
    ts_token, hs_id, email = _seed_verified(client)
    assert client.post("/api/onboarding/select-product",
                       json={"token": ts_token, "product": "asclepius"}).status_code == 200
    assert client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": ts_token, "org_name": "Northridge Nephrology",
              "specialty": "Nephrology", "phone": "(555) 123-4567"},
    ).status_code == 200

    cv_text = ("Curriculum Vitae\nHarvard Medical School, 2001-2005\n"
               "Nephrology Fellowship, UCLA Medical Center, 2008-2011\n"
               "Board Certified in Nephrology\n")
    r = client.post(
        "/api/onboarding/asclepius/cv",
        data={"token": ts_token},
        files={"file": ("cv.txt", cv_text.encode(), "text/plain")},
    )
    assert r.status_code == 200, r.text
    sha = r.json()["sha256"]

    creds = _creds(cvAssetSha=sha, cvMime="text/plain")
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": ts_token, "credentials": creds}).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": ts_token, "attestations": ATTS}).status_code == 200
    assert client.post("/api/onboarding/asclepius/finish",
                       json={"token": ts_token}).status_code == 200

    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["cv_asset_sha"] == sha
    parsed = json.loads(u["cv_parsed_json"])
    assert parsed["ok"] is True
    assert any("Harvard" in i for i in parsed["institutions"])


def test_cv_upload_rejects_bad_type_and_bad_token(client: TestClient):
    token, _, _ = _seed_verified(client)
    r = client.post(
        "/api/onboarding/asclepius/cv",
        data={"token": token},
        files={"file": ("cv.docx", b"zzz",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert r.status_code == 400
    r = client.post(
        "/api/onboarding/asclepius/cv",
        data={"token": "bogus-token"},
        files={"file": ("cv.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 404


def test_broken_cv_never_blocks_signup(client: TestClient):
    # points at an asset sha that does not exist — parse fails, signup completes
    _, _, email, _ = _run_director_signup(client, creds=_creds(cvAssetSha="0" * 64))
    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["verification_status"] == "pending"
    parsed = json.loads(u["cv_parsed_json"])
    assert parsed["ok"] is False
