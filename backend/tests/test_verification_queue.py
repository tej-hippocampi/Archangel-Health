"""PRD B Phases 4–5 — onboarding capture + the admin verification queue.

Launch-day property under test throughout: every failure path degrades to a
'pending' queue entry — never a rejection, never a 500 on the signup form.
"""

from __future__ import annotations

import json
import time
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from asclepius import auth as asc_auth
from tests._asclepius import app, fresh_store, headers_for, make_user

import routers.onboarding as onboarding_module
from asclepius import credentialing

VALID_NPI = "1234567893"  # canonical Luhn-valid NPI

_npi_counter = 0


def _fresh_npi() -> str:
    """A unique, checksum-valid NPI per test.

    Must be unique across pytest INVOCATIONS, not just within one: conftest
    points the suite at a stable ``/tmp/asclepius_suite/asclepius_suite.db``
    that survives between runs, and the 30-day NPI cache is real behavior. A
    counter-derived NPI restarts at 1 every run, hits a registry answer cached
    by a previous run, and the path under test never executes — which is
    exactly how a passing suite hides a regression. Seed from uuid4.
    """
    global _npi_counter
    _npi_counter += 1
    from asclepius.credentialing import npi_checksum_ok
    rand = uuid.uuid4().int % 1_000_000
    base = f"1{rand:06d}{_npi_counter % 100:02d}"  # 9 digits, unique per run+call
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
    # The physician now chooses their own password mid-wizard; finish refuses
    # without one, because nothing is generated for them any more.
    assert client.post("/api/onboarding/asclepius/password",
                       json={"token": token, "password": "correct-horse-battery-1"}).status_code == 200
    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 200, r.text
    return token, hs_id, director_email, creds


# ─── B-1.1: signup must not block the asyncio event loop ─────────────────────
def test_slow_nppes_does_not_delay_other_requests(client: TestClient, monkeypatch):
    """The property, not the symptom.

    Asserting that signup returns 200 while NPPES is slow proves nothing — the
    try/except already guaranteed that, and the form still hung. What matters
    is that a slow third-party call cannot stall OTHER requests in the process.
    Without run_in_threadpool at the call site this test fails: the concurrent
    request waits behind the sleeping httpx call on the event loop.
    """
    import anyio
    import httpx as _httpx

    bystander = make_user(fresh_store(), role="admin")
    token, hs_id, email = _seed_verified(client)
    assert client.post("/api/onboarding/select-product",
                       json={"token": token, "product": "asclepius"}).status_code == 200
    assert client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": token, "org_name": "Northridge Nephrology",
              "specialty": "Nephrology", "phone": "(555) 123-4567"},
    ).status_code == 200
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": token, "credentials": _creds()}).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": token, "attestations": ATTS}).status_code == 200
    # The physician now chooses their own password mid-wizard; finish refuses
    # without one, because nothing is generated for them any more.
    assert client.post("/api/onboarding/asclepius/password",
                       json={"token": token, "password": "correct-horse-battery-1"}).status_code == 200

    SLEEP = 3.0
    def _slow_nppes(npi, timeout=6.0):
        time.sleep(SLEEP)          # a hung NPPES response
        return {"status": "unavailable", "record": None, "reason": "slow"}
    monkeypatch.setattr(credentialing, "fetch_npi_record", _slow_nppes)

    result = {}

    async def _drive():
        transport = _httpx.ASGITransport(app=client.app)
        async with _httpx.AsyncClient(transport=transport, base_url="http://t",
                                      timeout=30.0) as ac:
            # Anchor every measurement to ONE origin taken before either task
            # starts. Timing from inside the bystander coroutine measures only
            # the part of the wait that happens after the loop is free again —
            # a blocked loop cannot run the clock call either, so the stall
            # silently vanishes from the number and the test passes while the
            # bug is present.
            origin = time.monotonic()

            async def _signup():
                r = await ac.post("/api/onboarding/asclepius/finish", json={"token": token})
                result["signup_status"] = r.status_code
                result["signup_done_at"] = time.monotonic() - origin

            async def _bystander():
                await anyio.sleep(0.25)      # let the signup reach the slow call
                r = await ac.get("/api/asclepius/auth/me", headers=headers_for(bystander))
                result["bystander_done_at"] = time.monotonic() - origin
                result["bystander_status"] = r.status_code

            async with anyio.create_task_group() as tg:
                tg.start_soon(_signup)
                tg.start_soon(_bystander)

    anyio.run(_drive)
    assert result["signup_status"] == 200, result
    assert result["bystander_status"] == 200, result
    # The signup really did take the full slow-NPPES hit...
    assert result["signup_done_at"] >= SLEEP, (
        f"signup finished in {result['signup_done_at']:.2f}s — the slow NPPES stub "
        f"was never reached, so this test proves nothing")
    # ...and an unrelated request served during it was not stalled behind it.
    assert result["bystander_done_at"] < 1.0, (
        f"an unrelated request completed only at t+{result['bystander_done_at']:.2f}s "
        f"while signup held the loop until t+{result['signup_done_at']:.2f}s — "
        f"signup is still running on the event loop")


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


def test_every_seam4_field_lands_on_the_user_row(client: TestClient):
    """F4 / Seam 4 — the receiver was built and the sender never was.

    users.phone, linkedin_url, cv_asset_sha and cv_parsed_json were
    permanently NULL in production because the React form's Credentials type
    carried none of them, so the entire CV pipeline and the linkedin_present /
    cv_parsed tier weights were unreachable. This walks the real route.
    """
    token, hs_id, email = _seed_verified(client)
    assert client.post("/api/onboarding/select-product",
                       json={"token": token, "product": "asclepius"}).status_code == 200
    assert client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": token, "org_name": "Northridge Nephrology",
              "specialty": "Nephrology", "phone": "(555) 999-0000"},   # ORG phone
    ).status_code == 200

    r = client.post("/api/onboarding/asclepius/cv", data={"token": token},
                    files={"file": ("cv.txt",
                                    b"Harvard Medical School, 2001-2005\n"
                                    b"Board Certified in Nephrology\n", "text/plain")})
    assert r.status_code == 200, r.text

    creds = _creds(phone="+1 555 010 7788",                # the PHYSICIAN's phone
                   linkedinUrl="https://www.linkedin.com/in/tejpatel",
                   healthSystem="Northridge Nephrology Associates")
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": token, "credentials": creds}).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": token, "attestations": ATTS}).status_code == 200
    # The physician now chooses their own password mid-wizard; finish refuses
    # without one, because nothing is generated for them any more.
    assert client.post("/api/onboarding/asclepius/password",
                       json={"token": token, "password": "correct-horse-battery-1"}).status_code == 200
    assert client.post("/api/onboarding/asclepius/finish",
                       json={"token": token}).status_code == 200

    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["phone"] == "+1 555 010 7788"
    assert u["phone"] != "(555) 999-0000", "the org's front-office phone leaked in"
    assert u["linkedin_url"] == "https://www.linkedin.com/in/tejpatel"
    assert u["cv_asset_sha"]
    assert json.loads(u["cv_parsed_json"])["ok"] is True
    assert u["email_domain_class"] == "business"
    # ...and the signals the fields exist to feed actually fire
    prop = credentialing.propose_tier(u)
    assert any("LinkedIn" in r for r in prop["reasons"])
    assert any("CV" in r for r in prop["reasons"])


def test_seam4_fields_reach_the_admin_dossier(client: TestClient):
    """Definition of done #5: a physician signs up with phone, LinkedIn and a
    CV, and all three appear in the admin dossier. Tests the path, not the
    row — the dossier is where a human actually sees them."""
    asc_store_module.reset_store_for_tests(
        db_path=client.app.state.asclepius_store.db_path)
    admin = make_user(client.app.state.asclepius_store, role="admin")

    token, hs_id, email = _seed_verified(client)
    assert client.post("/api/onboarding/select-product",
                       json={"token": token, "product": "asclepius"}).status_code == 200
    assert client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": token, "org_name": "Northridge Nephrology",
              "specialty": "Nephrology", "phone": "(555) 999-0000"},
    ).status_code == 200
    assert client.post(
        "/api/onboarding/asclepius/cv", data={"token": token},
        files={"file": ("cv.txt",
                        b"Curriculum Vitae\nHarvard Medical School, 2001-2005\n"
                        b"Nephrology Fellowship, UCLA Medical Center, 2008-2011\n"
                        b"Board Certified in Nephrology\n", "text/plain")},
    ).status_code == 200
    assert client.post(
        "/api/onboarding/asclepius/credentials",
        json={"token": token, "credentials": _creds(
            phone="+1 555 010 7788",
            linkedinUrl="https://www.linkedin.com/in/tejpatel",
            healthSystem="Northridge Nephrology Associates")},
    ).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": token, "attestations": ATTS}).status_code == 200
    # The physician now chooses their own password mid-wizard; finish refuses
    # without one, because nothing is generated for them any more.
    assert client.post("/api/onboarding/asclepius/password",
                       json={"token": token, "password": "correct-horse-battery-1"}).status_code == 200
    assert client.post("/api/onboarding/asclepius/finish",
                       json={"token": token}).status_code == 200

    uid = client.app.state.asclepius_store.get_user_by_email(email)["id"]
    d = client.get(f"/api/asclepius/verify/queue/{uid}",
                   headers=headers_for(admin)).json()
    assert d["phone"] == "+1 555 010 7788"
    assert d["linkedin_url"] == "https://www.linkedin.com/in/tejpatel"
    assert d["has_cv"] is True and d["cv_ok"] is True
    assert d["cv_asset_sha"]
    # free text the admin reads; PRD-C resolves it to a health_systems id later
    assert d["credentials"]["healthSystem"] == "Northridge Nephrology Associates"
    # and the raw file is fetchable from that dossier
    r = client.get(f"/api/asclepius/verify/queue/{uid}/cv", headers=headers_for(admin))
    assert r.status_code == 200 and b"Harvard" in r.content


def test_signup_without_any_optional_field_still_completes(client: TestClient):
    """The other half: nothing optional supplied, signup still finishes."""
    minimal = {"fullLegalName": "Dr. Solo Practitioner", "npi": _fresh_npi(),
               "phone": "+1 555 222 3333", "degree": "MD",
               "primarySpecialty": "Nephrology"}
    _, _, email, _ = _run_director_signup(client, creds=minimal)
    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["verification_status"] == "pending"
    assert u["phone"] == "+1 555 222 3333"
    assert u["linkedin_url"] is None and u["cv_asset_sha"] is None


def test_npi_is_normalized_on_the_way_in(client: TestClient):
    """B-5.1 — every lookup uses the cleaned form, so an NPI stored with a
    dash matched no cache row and no duplicate row: the duplicate-NPI blocker
    was defeated by punctuation the API (unlike the React form) never strips."""
    npi = _fresh_npi()
    dashed = f"{npi[:4]}-{npi[4:]}"
    _, _, email, _ = _run_director_signup(client, creds=_creds(npi=dashed))
    store = client.app.state.asclepius_store
    u = store.get_user_by_email(email)
    assert u["npi"] == npi                       # stored clean, not "1234-567893"
    assert store.find_users_by_npi(npi)          # duplicate detection can see it
    assert store.get_cached_npi_fetch(npi) is not None


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
    # F6: the failed check is an attempt, not a result — it never lands in the
    # result columns, so it cannot overwrite evidence on a re-onboard.
    assert json.loads(u["npi_last_attempt_json"])["result"] == "unavailable"
    assert u["npi_payload_json"] is None and u["npi_verified"] is None
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


# ─── F5: SSO arrivals must not skip verification ─────────────────────────────
def _sso_token(email: str) -> str:
    """A doctor-portal staff token — the bridge /auth/sso accepts."""
    from tenant_jwt import create_tenant_staff_token
    return create_tenant_staff_token(
        email=email, name="Dr SSO Arrival", role="surgeon",
        health_system_id="hs-test", tenant_slug="hs-test",
        health_system_code="HST",
    )


def test_sso_first_arrival_lands_pending_and_cannot_draw_tasks(client: TestClient):
    """The test that would have caught F5.

    /auth/sso provisions via create_user, which never sets
    verification_status, and auth.py treats NULL as pass-through — correct for
    pre-migration rows, wrong for one created yesterday. The clinician got an
    evaluator seat with zero credentialing and never appeared in the queue.
    """
    store = asc_store_module.reset_store_for_tests(
        db_path=client.app.state.asclepius_store.db_path)
    email = f"sso_{uuid.uuid4().hex[:8]}@hospital.org"
    r = client.post("/api/asclepius/auth/sso", json={"token": _sso_token(email)})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]

    row = store.get_user_by_email(email)
    assert row["verification_status"] == "pending"
    assert row["id"] in [u["id"] for u in store.list_verification_queue("pending")]

    hdrs = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/asclepius/tasks/next", headers=hdrs).status_code == 403
    # /auth/me is open now: a first arrival lands INSIDE the product and waits
    # there. Only the real-work surface stays shut.
    me = client.get("/api/asclepius/auth/me", headers=hdrs)
    assert me.status_code == 200
    assert me.json()["access_level"] == "provisional"


def test_sso_does_not_backfill_or_relock_existing_users(client: TestClient):
    """A pre-existing NULL-status account must keep working, and a returning
    SSO user must not be re-flagged after an admin approved them."""
    store = asc_store_module.reset_store_for_tests(
        db_path=client.app.state.asclepius_store.db_path)
    email = f"legacy_{uuid.uuid4().hex[:8]}@hospital.org"
    legacy = store.create_user(email=email, password="pw-12345678", role="evaluator")
    assert legacy["verification_status"] is None

    r = client.post("/api/asclepius/auth/sso", json={"token": _sso_token(email)})
    assert r.status_code == 200
    assert store.get_user_by_email(email)["verification_status"] is None  # not backfilled
    assert client.get("/api/asclepius/auth/me",
                      headers={"Authorization": f"Bearer {r.json()['token']}"}
                      ).status_code == 200

    store.set_verification_status(legacy["id"], "approved")
    r = client.post("/api/asclepius/auth/sso", json={"token": _sso_token(email)})
    assert r.status_code == 200
    assert store.get_user_by_email(email)["verification_status"] == "approved"


# ─── Phase 4: the pending gate — a pending user cannot draw tasks ────────────
def test_pending_evaluator_cannot_draw_tasks_or_use_portal(client: TestClient):
    store = fresh_store()
    u = make_user(store)
    store.set_verification_status(u["id"], "pending")
    r = client.get("/api/asclepius/tasks/next", headers=headers_for(u))
    assert r.status_code == 403
    # The machine-readable state, not the prose. Matching on copy breaks the
    # moment a writer rewords it, which is the whole reason this header exists.
    assert r.headers.get(asc_auth.AUTH_GATE_HEADER) == "pending"
    # In the product while they wait.
    assert client.get("/api/asclepius/auth/me", headers=headers_for(u)).status_code == 200


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
    # B-5.7: the sha is NOT returned to the client and never round-trips
    # through it — it is recorded on the person row server-side.
    assert "sha256" not in r.json()

    # The credentials POST arrives AFTER the upload and carries no CV fields;
    # it must not erase what the server recorded.
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": ts_token, "credentials": _creds()}).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": ts_token, "attestations": ATTS}).status_code == 200
    assert client.post("/api/onboarding/asclepius/password",
                       json={"token": ts_token, "password": "correct-horse-battery-1"}).status_code == 200
    assert client.post("/api/onboarding/asclepius/finish",
                       json={"token": ts_token}).status_code == 200

    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["cv_asset_sha"], "the server-recorded CV did not reach the user row"
    parsed = json.loads(u["cv_parsed_json"])
    assert parsed["ok"] is True
    assert any("Harvard" in i for i in parsed["institutions"])


def test_client_supplied_cv_sha_is_ignored(client: TestClient):
    """B-5.7 — ``credentials`` is a free-form dict, so a signup could otherwise
    name any sha in the shared asset store (which also holds de-identified
    clinical images) and have it parsed and served back through the dossier."""
    from asclepius import credentialing as _cred
    planted = _cred.store_cv(b"not this physician's document", "text/plain")["sha256"]
    _, _, email, _ = _run_director_signup(
        client, creds=_creds(cvAssetSha=planted, cvMime="text/plain"))
    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["cv_asset_sha"] != planted
    assert u["cv_asset_sha"] is None      # nothing was ever uploaded for them


def test_cv_upload_rejects_bad_type_and_bad_token(client: TestClient):
    """.docx is accepted now, so the refusal this pins moved.

    It used to be "we do not take Word files", which is no longer true and was
    never a good answer: .docx is the single most common thing a physician
    attaches. What is still refused is a file whose BYTES are not something we
    can read, whatever it claims to be, and a zip that is not a Word document
    is the sharpest version of that: .docx, .xlsx, .jar, .epub and a plain .zip
    all begin PK\x03\x04, so accepting on the magic bytes would accept all of
    them into a blob that is later served inline from our own origin.
    """
    import io
    import zipfile

    token, _, _ = _seed_verified(client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.txt", "not a cv")
    r = client.post(
        "/api/onboarding/asclepius/cv",
        data={"token": token},
        files={"file": ("cv.docx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert r.status_code == 400, r.text
    # And the refusal says what to attach instead, which the old one did not.
    assert "PDF" in r.text and "Word" in r.text
    r = client.post(
        "/api/onboarding/asclepius/cv",
        data={"token": "bogus-token"},
        files={"file": ("cv.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 404


def test_unparseable_cv_never_blocks_upload_or_signup(client: TestClient, monkeypatch):
    """A CV that cannot be parsed leaves the field empty and the admin reads
    the raw file — the upload still succeeds and signup still completes."""
    def _boom(*a, **kw):
        raise RuntimeError("parser exploded")
    monkeypatch.setattr(credentialing, "parse_cv", _boom)

    token, hs_id, email = _seed_verified(client)
    assert client.post("/api/onboarding/select-product",
                       json={"token": token, "product": "asclepius"}).status_code == 200
    assert client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": token, "org_name": "N", "specialty": "Nephrology",
              "phone": "(555) 123-4567"},
    ).status_code == 200
    r = client.post("/api/onboarding/asclepius/cv", data={"token": token},
                    files={"file": ("cv.txt", b"a resume", "text/plain")})
    assert r.status_code == 200, r.text          # parse failure is non-fatal
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": token, "credentials": _creds()}).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": token, "attestations": ATTS}).status_code == 200
    # The physician now chooses their own password mid-wizard; finish refuses
    # without one, because nothing is generated for them any more.
    assert client.post("/api/onboarding/asclepius/password",
                       json={"token": token, "password": "correct-horse-battery-1"}).status_code == 200
    assert client.post("/api/onboarding/asclepius/finish",
                       json={"token": token}).status_code == 200

    u = client.app.state.asclepius_store.get_user_by_email(email)
    assert u["verification_status"] == "pending"
    assert u["cv_asset_sha"]                     # raw file still available to the admin
    assert u["cv_parsed_json"] is None           # no suggestions, no crash


def test_oversize_cv_is_rejected_before_it_is_buffered(client: TestClient):
    """B-5.4 — the cap was enforced inside store_cv, i.e. after the whole body
    was already resident in memory."""
    token, _, _ = _seed_verified(client)
    from asclepius.credentialing import CV_MAX_BYTES
    big = b"%PDF-1.4" + b"0" * (CV_MAX_BYTES + 4096)
    r = client.post("/api/onboarding/asclepius/cv", data={"token": token},
                    files={"file": ("cv.pdf", big, "application/pdf")})
    assert r.status_code == 413


# ─── Phase 5: the admin verification queue ───────────────────────────────────
from asclepius import store as asc_store_module  # noqa: E402


def _pending_physician(store, *, npi=None, email=None, family="Patel", **extra):
    """A signup as the queue sees it, created directly against the store."""
    npi = npi or _fresh_npi()
    email = email or f"doc_{uuid.uuid4().hex[:8]}@nephrology-associates.com"
    u = store.provision_user(
        email=email, password="pw-12345678", role="evaluator",
        full_name=f"Dr. Ana {family}", npi=npi,
        specialty=extra.pop("specialty", "nephrology"),
        board_cert=extra.pop("board_cert", "ABIM — Nephrology"),
        years_experience=extra.pop("years_experience", 12),
    )
    store.update_identity_capture(
        u["id"], phone="+1 555 000 1111",
        linkedin_url="https://linkedin.com/in/ana",
        email_domain_class="business")
    store.set_npi_result(u["id"], {
        "result": extra.pop("npi_result", "verified"),
        "npi": npi,
        "reason": extra.pop("npi_reason", None),
        "record": {"number": npi, "enumeration_type": "NPI-1", "status": "A",
                   "first_name": "Ana", "last_name": family, "credential": "M.D.",
                   "enumeration_date": "2010-01-01",
                   "taxonomy": {"code": "x", "desc": "Nephrology", "state": "CA",
                                "license": "1"},
                   "location": {"city": "LA", "state": "CA"}},
    })
    store.set_verification_status(u["id"], "pending")
    return store.get_user_by_id(u["id"])


def test_queue_requires_admin_auth(client: TestClient):
    store = fresh_store()
    evaluator = make_user(store)
    assert client.get("/api/asclepius/verify/queue").status_code == 401
    assert client.get("/api/asclepius/verify/queue",
                      headers=headers_for(evaluator)).status_code == 403


def test_queue_lists_pending_newest_first_with_score_reasons_blockers(client: TestClient):
    store = fresh_store()
    admin = make_user(store, role="admin")
    first = _pending_physician(store)
    second = _pending_physician(store)
    r = client.get("/api/asclepius/verify/queue", headers=headers_for(admin))
    assert r.status_code == 200
    q = r.json()["queue"]
    ids = [row["user_id"] for row in q]
    assert ids.index(second["id"]) < ids.index(first["id"])  # newest first
    row = q[0]
    assert row["score"] > 0
    assert row["reasons"]
    assert row["blockers"] == []
    assert row["proposed_tier"] in ("labeler", "reviewer")
    assert row["npi"]["result"] == "verified"


def test_signup_flow_lands_in_queue_within_seconds(client: TestClient):
    # e2e: the onboarding router writes via app.state; point the singleton the
    # verify router uses at the same DB before running the flow.
    asc_store_module.reset_store_for_tests(
        db_path=client.app.state.asclepius_store.db_path)
    admin = make_user(client.app.state.asclepius_store, role="admin")
    _, _, email, creds = _run_director_signup(client)
    r = client.get("/api/asclepius/verify/queue", headers=headers_for(admin))
    assert r.status_code == 200
    match = [row for row in r.json()["queue"] if row["email"] == email]
    assert match, "fresh signup missing from the pending queue"
    assert match[0]["npi"]["npi"] == creds["npi"]


def test_dossier_has_payloads_and_never_the_password_hash(client: TestClient):
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store)
    r = client.get(f"/api/asclepius/verify/queue/{u['id']}", headers=headers_for(admin))
    assert r.status_code == 200
    d = r.json()
    assert d["npi_payload"]["result"] == "verified"
    assert d["score"] > 0 and d["reasons"]
    assert "password_hash" not in json.dumps(d)
    assert client.get("/api/asclepius/verify/queue/u-nonexistent",
                      headers=headers_for(admin)).status_code == 404


def test_approve_without_tier_is_400(client: TestClient):
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store)
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/approve",
                    json={}, headers=headers_for(admin))
    assert r.status_code == 400
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/approve",
                    json={"tier": "supervisor"}, headers=headers_for(admin))
    assert r.status_code == 400
    assert store.get_user_by_id(u["id"])["verification_status"] == "pending"


def test_approve_sets_tier_verified_by_and_at_and_unlocks_portal(client: TestClient):
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store)
    # In the product, but real work is locked while pending.
    assert client.get("/api/asclepius/auth/me", headers=headers_for(u)).status_code == 200
    assert client.get("/api/asclepius/tasks/next", headers=headers_for(u)).status_code == 403
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/approve",
                    json={"tier": "reviewer", "note": "strong NPPES + 12y"},
                    headers=headers_for(admin))
    assert r.status_code == 200, r.text
    row = store.get_user_by_id(u["id"])
    assert row["verification_status"] == "approved"
    assert row["tier"] == "reviewer"
    assert row["tier_score"] is not None
    assert row["verified_by"] == admin["email"]
    assert row["verified_at"] is not None
    assert row["tier_assigned_by"] == admin["email"]
    # decision is logged, and the physician can now use the portal
    events = store.list_events(entity_type="user", entity_id=u["id"])
    assert any(e["event_type"] == "verification_approved" for e in events)
    assert client.get("/api/asclepius/auth/me", headers=headers_for(u)).status_code == 200


def test_admin_may_override_the_proposal(client: TestClient):
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store)  # proposal will say reviewer (high signal)
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/approve",
                    json={"tier": "labeler"}, headers=headers_for(admin))
    assert r.status_code == 200
    assert store.get_user_by_id(u["id"])["tier"] == "labeler"
    ev = [e for e in store.list_events(entity_type="user", entity_id=u["id"])
          if e["event_type"] == "verification_approved"][0]
    assert ev["payload"]["followed_proposal"] is False


def test_reject_without_note_is_400_with_note_rejects(client: TestClient):
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store)
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/reject",
                    json={}, headers=headers_for(admin))
    assert r.status_code == 400
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/reject",
                    json={"note": "   "}, headers=headers_for(admin))
    assert r.status_code == 400
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/reject",
                    json={"note": "NPI belongs to a different specialty"},
                    headers=headers_for(admin))
    assert r.status_code == 200
    row = store.get_user_by_id(u["id"])
    assert row["verification_status"] == "rejected"
    assert row["verified_by"] == admin["email"]
    assert row["tier"] is None
    r = client.get("/api/asclepius/auth/me", headers=headers_for(u))
    assert r.status_code == 403 and "not approved" in r.json()["detail"].lower()


def test_recheck_npi_updates_unavailable_in_place(client: TestClient, monkeypatch):
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store, npi_result="unavailable", npi_reason="rate_limited")
    store.set_npi_result(u["id"], {"result": "unavailable", "reason": "rate_limited"})
    assert store.get_user_by_id(u["id"])["npi_verified"] is None
    monkeypatch.setattr(
        credentialing, "fetch_npi_record",
        lambda npi, timeout=6.0: {"status": "found",
                                  "record": _nppes_record(last_name="Patel"),
                                  "reason": None})
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/recheck-npi",
                    headers=headers_for(admin))
    assert r.status_code == 200, r.text
    assert r.json()["npi_verified"] == 1
    assert r.json()["npi"]["result"] == "verified"
    events = store.list_events(entity_type="user", entity_id=u["id"])
    assert any(e["event_type"] == "npi_rechecked" for e in events)


# ─── F6: a failed recheck must not destroy a verified result ────────────────
def test_recheck_against_down_nppes_preserves_a_verified_physician(
        client: TestClient, monkeypatch):
    """The test that would have caught F6.

    The old write was unconditional, so an UNAVAILABLE outcome set
    npi_verified back to NULL, replaced the NPPES record with
    {"result":"unavailable","record":null} and cleared npi_checked_at —
    destroying the evidence, dropping the score 25 points, making 'reviewer'
    unproposable, and evicting the 30-day cache for EVERY user of that NPI.
    The trigger is an admin clicking Recheck while NPPES rate-limits, i.e.
    exactly when that button is used.
    """
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store)          # verified
    npi = u["npi"]
    before = store.get_user_by_id(u["id"])
    assert before["npi_verified"] == 1
    assert store.get_cached_npi_fetch(npi) is not None
    score_before = credentialing.propose_tier(before)

    monkeypatch.setattr(
        credentialing, "fetch_npi_record",
        lambda n, timeout=6.0: {"status": "unavailable", "record": None,
                                "reason": "rate_limited"})
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/recheck-npi",
                    headers=headers_for(admin))
    assert r.status_code == 200, r.text

    after = store.get_user_by_id(u["id"])
    assert after["npi_verified"] == 1, "a failed recheck erased a verified result"
    assert after["npi_checked_at"] == before["npi_checked_at"]
    assert json.loads(after["npi_payload_json"])["result"] == "verified"
    assert store.get_cached_npi_fetch(npi) is not None, "shared 30-day cache evicted"
    assert credentialing.propose_tier(after)["score"] == score_before["score"]
    # ...but the attempt is recorded and visible to the admin
    assert after["npi_last_attempt_at"] is not None
    assert json.loads(after["npi_last_attempt_json"])["reason"] == "rate_limited"
    assert r.json()["npi"]["last_attempt"] == "rate_limited"
    assert r.json()["npi"]["result"] == "verified"


def test_reonboard_while_nppes_down_preserves_verification(client: TestClient, monkeypatch):
    """The other F6 trigger: provision_user is an idempotent upsert, so a
    re-onboard re-runs verification against a possibly-down registry."""
    store = fresh_store()
    u = _pending_physician(store)
    monkeypatch.setattr(
        credentialing, "fetch_npi_record",
        lambda n, timeout=6.0: {"status": "unavailable", "record": None,
                                "reason": "rate_limited"})
    onboarding_module._run_signup_verification(
        store, store.get_user_by_id(u["id"]),
        {"fullLegalName": "Dr. Ana Patel", "npi": u["npi"]})
    assert store.get_user_by_id(u["id"])["npi_verified"] == 1


def test_definitive_recheck_still_updates_and_clears_the_attempt(
        client: TestClient, monkeypatch):
    """Do not over-correct: a recheck that DOES reach NPPES must overwrite."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store, npi_result="unavailable")
    store.set_npi_result(u["id"], {"result": "unavailable", "reason": "rate_limited"})
    assert store.get_user_by_id(u["id"])["npi_last_attempt_at"] is not None
    monkeypatch.setattr(
        credentialing, "fetch_npi_record",
        lambda n, timeout=6.0: {"status": "found",
                                "record": _nppes_record(last_name="Patel"),
                                "reason": None})
    r = client.post(f"/api/asclepius/verify/queue/{u['id']}/recheck-npi",
                    headers=headers_for(admin))
    assert r.status_code == 200
    row = store.get_user_by_id(u["id"])
    assert row["npi_verified"] == 1 and row["npi_checked_at"] is not None
    assert row["npi_last_attempt_at"] is None      # stale attempt cleared


def test_retry_list_and_bulk_sweep(client: TestClient, monkeypatch):
    """PRD §1.2 said UNAVAILABLE 'routes to manual review AND schedules a
    retry'. There was no retry path at all — this is it."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    stuck = _pending_physician(store, npi_result="unavailable")
    store.set_npi_result(stuck["id"], {"result": "unavailable", "reason": "rate_limited"})
    done = _pending_physician(store)  # verified — must not be swept

    r = client.get("/api/asclepius/verify/recheck-pending?older_than_minutes=0",
                   headers=headers_for(admin))
    assert r.status_code == 200
    ids = [x["user_id"] for x in r.json()["users"]]
    assert stuck["id"] in ids and done["id"] not in ids

    monkeypatch.setattr(
        credentialing, "fetch_npi_record",
        lambda n, timeout=6.0: {"status": "found",
                                "record": _nppes_record(last_name="Patel"),
                                "reason": None})
    r = client.post("/api/asclepius/verify/recheck-pending?older_than_minutes=0",
                    headers=headers_for(admin))
    assert r.status_code == 200, r.text
    assert r.json()["outcomes"].get("verified", 0) >= 1
    assert store.get_user_by_id(stuck["id"])["npi_verified"] == 1
    # the sweep drains: nothing left to retry
    r = client.get("/api/asclepius/verify/recheck-pending?older_than_minutes=0",
                   headers=headers_for(admin))
    assert stuck["id"] not in [x["user_id"] for x in r.json()["users"]]


def test_retry_endpoints_require_admin(client: TestClient):
    store = fresh_store()
    evaluator = make_user(store)
    assert client.get("/api/asclepius/verify/recheck-pending").status_code == 401
    assert client.post("/api/asclepius/verify/recheck-pending",
                       headers=headers_for(evaluator)).status_code == 403


def test_duplicate_detection_sees_legacy_unnormalized_npis(client: TestClient):
    """B-5.1, read side. Normalizing on WRITE fixes new rows; rows already in
    the database still hold '1234-567893', and comparing the raw column leaves
    them invisible to duplicate detection — the same defect, still open for
    everyone who signed up before the fix. Normalized in SQL rather than by
    rewriting stored data.
    """
    store = fresh_store()
    admin = make_user(store, role="admin")
    npi = _fresh_npi()
    dashed = f"{npi[:4]}-{npi[4:7]}.{npi[7:]}"
    legacy = store.provision_user(email=f"legacy_{uuid.uuid4().hex[:8]}@h.org",
                                  password="pw-12345678", role="evaluator",
                                  full_name="Dr Legacy", npi=dashed)   # raw, as before
    assert store.get_user_by_id(legacy["id"])["npi"] == dashed
    fresh = _pending_physician(store, npi=npi)                          # clean

    assert len(store.find_users_by_npi(npi)) == 2
    assert store.npi_claim_counts().get(npi) == 2
    store.set_verification_status(legacy["id"], "pending")
    q = client.get("/api/asclepius/verify/queue", headers=headers_for(admin)).json()["queue"]
    flagged = {r["user_id"]: r["blockers"] for r in q
               if r["user_id"] in (legacy["id"], fresh["id"])}
    assert len(flagged) == 2
    for blockers in flagged.values():
        assert any("Duplicate NPI" in b for b in blockers)


def test_duplicate_npi_flags_both_queue_rows(client: TestClient):
    store = fresh_store()
    admin = make_user(store, role="admin")
    shared = _fresh_npi()
    a = _pending_physician(store, npi=shared)
    b = _pending_physician(store, npi=shared)
    q = client.get("/api/asclepius/verify/queue",
                   headers=headers_for(admin)).json()["queue"]
    flagged = {row["user_id"]: row["blockers"] for row in q
               if row["user_id"] in (a["id"], b["id"])}
    assert len(flagged) == 2
    for blockers in flagged.values():
        assert any("Duplicate NPI" in bl for bl in blockers)
    # dossier names the other claimant
    d = client.get(f"/api/asclepius/verify/queue/{a['id']}",
                   headers=headers_for(admin)).json()
    assert any(dc["user_id"] == b["id"] for dc in d["duplicate_claims"])


def test_signup_rate_limit_does_not_lock_out_a_team_behind_one_nat(
        client: TestClient, monkeypatch):
    """B-5.3 — the bucket was asclepius_signup:<ip>, shared across BOTH finish
    endpoints at 5/hour. A health system egresses through one NAT gateway, so
    the 6th physician of a 10-person team invited in the same hour got a 429
    and could not finish signup. And client_ip() uses the LAST XFF hop, which
    with Cloudflare in front is an edge IP — one bucket per PoP for the whole
    planet.
    """
    import ratelimit
    monkeypatch.setattr(ratelimit, "is_enabled", lambda: True)
    ratelimit.reset()
    try:
        completed = 0
        for _ in range(8):                     # one team, one apparent IP
            _run_director_signup(client)       # each has its OWN token
            completed += 1
        assert completed == 8
    finally:
        ratelimit.reset()


def test_signup_rate_limit_still_throttles_one_replayed_token(
        client: TestClient, monkeypatch):
    """The other half: the token key must still stop a single invitation from
    being hammered."""
    import ratelimit
    monkeypatch.setattr(ratelimit, "is_enabled", lambda: True)
    ratelimit.reset()
    try:
        token, _, _, _ = _run_director_signup(client)
        codes = [client.post("/api/onboarding/asclepius/finish",
                             json={"token": token}).status_code
                 for _ in range(10)]
        assert 429 in codes, "a replayed onboarding token was never throttled"
    finally:
        ratelimit.reset()


def test_queue_paginates(client: TestClient):
    """B-5.8 — the approved tab only grows; an unpaginated queue degrades
    linearly forever."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    for _ in range(5):
        _pending_physician(store)
    r = client.get("/api/asclepius/verify/queue?limit=2", headers=headers_for(admin))
    assert r.status_code == 200
    body = r.json()
    assert len(body["queue"]) == 2
    assert body["total"] >= 5 and body["has_more"] is True
    r2 = client.get("/api/asclepius/verify/queue?limit=2&offset=2",
                    headers=headers_for(admin))
    first = {x["user_id"] for x in body["queue"]}
    second = {x["user_id"] for x in r2.json()["queue"]}
    assert not (first & second)


def test_cv_download_sets_nosniff(client: TestClient):
    """B-5.5 — served inline from the app origin to an admin whose bearer
    token is in localStorage. The sibling image endpoint already sets this."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store)
    meta = credentialing.store_cv(b"plain text resume", "text/plain")
    store.set_cv(u["id"], meta["sha256"], {"ok": True})
    r = client.get(f"/api/asclepius/verify/queue/{u['id']}/cv",
                   headers=headers_for(admin))
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-type"].startswith("text/plain")


def test_cv_download_is_admin_only_and_sniffs_type(client: TestClient):
    store = fresh_store()
    admin = make_user(store, role="admin")
    u = _pending_physician(store)
    meta = credentialing.store_cv(b"plain text resume", "text/plain")
    store.set_cv(u["id"], meta["sha256"], {"ok": True})
    r = client.get(f"/api/asclepius/verify/queue/{u['id']}/cv",
                   headers=headers_for(admin))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.content == b"plain text resume"
    assert client.get(f"/api/asclepius/verify/queue/{u['id']}/cv",
                      headers=headers_for(u)).status_code == 403
