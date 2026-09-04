"""Onboarding v2 — every case §8 names.

Grouped the way the PRD groups them: wizard · resume/nudge · credentials ·
walkthrough · emails. Each test asserts the behaviour a physician or an admin
would actually observe, not that a function was called.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

import onboarding_emails as oe  # noqa: E402
import routers.onboarding as onboarding_module  # noqa: E402
from asclepius import assets as asc_assets  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402
from asclepius import credentialing  # noqa: E402
from asclepius import onboarding_nudge  # noqa: E402
from asclepius import plausibility  # noqa: E402
from asclepius import store as asc_store_mod  # noqa: E402
from main import app  # noqa: E402
from tests._asclepius import fresh_store, headers_for, make_user, token_for  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_email(monkeypatch):
    """No SendGrid round-trip, but record what would have been sent: several of
    these tests are ABOUT which email goes out, and asserting on a swallowed
    send would assert nothing."""
    sent: list = []

    async def _send(to, subject, html_body, **kwargs):  # noqa: ANN001
        sent.append({"to": to, "subject": subject, "html": html_body})
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _send)
    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)
    return sent


@pytest.fixture()
def sent(_stub_email):
    return _stub_email


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

CREDS_MINIMAL = {"fullLegalName": "Dr. Amara Okafor", "primarySpecialty": "Nephrology"}
ATTS = {
    "consentCredentialShare": True,
    "attestIndependentJudgment": True,
    "ipAssignment": True,
    "noPhi": True,
    "signedInitials": "AO",
}


def _seed_verified(client: TestClient, *, product: str = "asclepius"):
    """A self-serve invite advanced to step 2 (mailbox proven), product locked."""
    ts = client.app.state.team_store
    invite = ts.create_health_system_invite(
        invite_base_url="http://localhost:5173", product=product)
    token = invite["onboarding_url"].rsplit("/", 1)[-1]
    hs_id = invite["health_system_id"]
    email = f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org"
    ts.update_health_system_director_identity(
        hs_id, first_name="Amara", last_name="Okafor", email=email)
    with sqlite3.connect(ts.db_path) as conn:
        conn.execute("UPDATE health_systems SET onboarding_step = 2 WHERE id = ?", (hs_id,))
        conn.commit()
    return token, hs_id, email


def _age_invite(client: TestClient, hs_id: str, hours: float) -> None:
    """Backdate created_at so the nudge sweep considers this row."""
    when = (datetime.utcnow() - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    ts = client.app.state.team_store
    with sqlite3.connect(ts.db_path) as conn:
        conn.execute("UPDATE health_systems SET created_at = ? WHERE id = ?", (when, hs_id))
        conn.commit()


# ═════════════════════════════════════════════════════════════════════════════
# wizard
# ═════════════════════════════════════════════════════════════════════════════

_WIZARD_TSX = Path(__file__).resolve().parents[2] / "landing/src/app/components/OnboardingWizard.tsx"


def test_physician_order_is_cv_review_and_has_no_password_step():
    """§8: identity→verify→cv→review→attestations→success; no password step.

    Asserted against the source of ``orderFor`` because that function IS the
    contract — it drives the stepper, Back, and every resume target.
    """
    src = _WIZARD_TSX.read_text(encoding="utf-8")
    order_line = '["identity", "verify", "cv", "review", "attestations", "submitted"]'
    assert order_line in src, "the physician+asclepius order is not the v2 order"
    # The password screen must not be reachable on this path. It still exists
    # for member mode and the short signup, so the check is that the physician
    # array does not contain it, not that the step is gone.
    idx = src.index(order_line)
    branch = src[src.index('if (product === "asclepius") {'):idx]
    assert '"password"' not in branch


def test_member_and_short_signup_orders_are_unchanged():
    """§8 regression: member/advisor/referrer paths keep their password step."""
    src = _WIZARD_TSX.read_text(encoding="utf-8")
    assert '["credentials", "attestations", "verify", "password", "ascSuccess"]' in src
    assert 'const head: StepKey[] = ["identity", "verify", "password"];' in src
    assert 'if (kind !== "physician") return [...head, "ascSuccess"];' in src


def test_submit_succeeds_with_only_name_email_and_specialty(client: TestClient):
    """§8: submit succeeds with ONLY name+email+specialty.

    No NPI, no CV, no board certification, no licence, no phone — and no
    password, which is the change that makes the account `pending` rather than
    usable.
    """
    token, hs_id, email = _seed_verified(client)
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": token, "credentials": CREDS_MINIMAL}).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": token, "attestations": ATTS}).status_code == 200
    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["awaiting_review"] is True
    # A session IS minted, which reverses the original v2 rule. That rule was
    # right when there was nothing behind the door: a token would have dropped
    # a physician into a portal that 403s every call. The practice case changed
    # it. An applicant now has real work to do before we decide about them, and
    # it cannot live behind a door they cannot open.
    #
    # No password comes into existence here, so the reasoning at approval time
    # is untouched: approval is still where a durable credential is minted.
    assert body.get("token"), "an applicant needs a way into the practice case"

    asc = client.app.state.asclepius_store
    u = asc.get_user_by_email(email)
    assert u is not None
    assert asc_store_mod.password_is_unset(u), "a v2 application must carry no credential"
    assert u["verification_status"] == "pending"
    assert u["specialty"] == "nephrology"


def test_missing_npi_cv_and_cert_land_as_review_flags_not_blockers(client: TestClient):
    """§8: missing NPI lands as a review flag.

    LOW severity specifically, because ``propose_tier`` suppresses its proposal
    on HIGH findings only — so absence reaches the admin's dossier without
    turning a two-minute application into a manual hold.
    """
    token, hs_id, email = _seed_verified(client)
    client.post("/api/onboarding/asclepius/credentials",
                json={"token": token, "credentials": CREDS_MINIMAL})
    client.post("/api/onboarding/asclepius/attestations",
                json={"token": token, "attestations": ATTS})
    assert client.post("/api/onboarding/asclepius/finish",
                       json={"token": token}).status_code == 200

    asc = client.app.state.asclepius_store
    u = asc.get_user_by_email(email)
    flags = json.loads(u["flags_json"] or "[]")
    issues = {(f["field"], f["issue"]) for f in flags}
    assert ("npi", "not_provided") in issues
    assert ("cv", "not_provided") in issues
    assert ("board_cert", "not_provided") in issues
    assert all(f["severity"] == plausibility.SEVERITY_LOW
               for f in flags if f["issue"] == "not_provided")
    # And none of them suppresses the tier proposal.
    proposal = credentialing.propose_tier(u)
    assert not [b for b in proposal["blockers"] if "not_provided" in b]


def test_submit_without_a_specialty_is_refused_by_name(client: TestClient):
    """The other half of the same rule: the three required fields ARE required,
    and the refusal says which one is missing rather than 'add your credentials'."""
    token, _hs_id, _email = _seed_verified(client)
    client.post("/api/onboarding/asclepius/credentials",
                json={"token": token, "credentials": {"fullLegalName": "Dr. A. Okafor"}})
    client.post("/api/onboarding/asclepius/attestations",
                json={"token": token, "attestations": ATTS})
    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 400
    assert "specialty" in r.json()["detail"].lower()


def test_cv_upload_reports_real_stages_and_parses(client: TestClient):
    """§2 screen 3: the captions track REAL parse stages, and the Review screen's
    prefill comes back from /cv/status."""
    token, hs_id, email = _seed_verified(client)
    cv = (
        "Amara N. Okafor, MD\n\n"
        "NPI: 1234567893\n"
        "https://www.linkedin.com/in/amara-okafor\n\n"
        "TRAINING\n"
        "Residency, Internal Medicine, Massachusetts General Hospital, 2012-2015\n"
        "Fellowship, Nephrology, Brigham and Women's Hospital, 2015-2018\n\n"
        "CERTIFICATIONS\nBoard-certified in Nephrology\n\n"
        "14 years of clinical practice\n"
    )
    r = client.post(
        "/api/onboarding/asclepius/cv",
        data={"token": token},
        files={"file": ("cv.txt", cv.encode("utf-8"), "text/plain")},
    )
    assert r.status_code == 200, r.text
    # 'reading' is stamped synchronously, so a first poll can never read the
    # gap between "upload returned" and "background task started" as idle.
    assert r.json()["stage"] == "reading"

    # TestClient runs BackgroundTasks to completion before returning, so by now
    # the parse is done.
    s = client.get(f"/api/onboarding/asclepius/cv/status?token={token}").json()
    assert s["uploaded"] is True and s["finished"] is True and s["ok"] is True
    parsed = s["parsed"]
    assert parsed["full_name"] == "Amara N. Okafor"
    assert parsed["npi"] == "1234567893"                       # labelled AND checksum-valid
    assert parsed["specialty"] == "nephrology"
    assert parsed["degrees"] == ["MD"]
    assert parsed["linkedin_url"].endswith("/in/amara-okafor")
    assert {t["kind"] for t in parsed["training"]} == {"residency", "fellowship"}


def test_cv_parse_failure_is_an_empty_state_not_an_error(client: TestClient):
    """§8: CV parse failure → review page empty-state, not an error.

    The poll still resolves (``finished``), the application is unaffected, and
    the suggestions are simply absent — which is the same shape the "No CV"
    manual path produces.
    """
    token, _hs_id, email = _seed_verified(client)
    r = client.post(
        "/api/onboarding/asclepius/cv",
        data={"token": token},
        files={"file": ("cv.txt", b"tiny", "text/plain")},   # under the 40-char floor
    )
    assert r.status_code == 200, r.text
    s = client.get(f"/api/onboarding/asclepius/cv/status?token={token}").json()
    assert s["finished"] is True and s["ok"] is False
    assert s["parsed"]["reason"] == "no_extractable_text"
    # Every key the Review screen indexes is present on the failure shape too.
    for key in ("full_name", "degrees", "training", "specialty", "npi", "linkedin_url"):
        assert key in s["parsed"]
    # And the application still completes.
    client.post("/api/onboarding/asclepius/credentials",
                json={"token": token, "credentials": CREDS_MINIMAL})
    client.post("/api/onboarding/asclepius/attestations",
                json={"token": token, "attestations": ATTS})
    assert client.post("/api/onboarding/asclepius/finish",
                       json={"token": token}).status_code == 200


def test_a_client_cannot_set_its_own_cv_parse_or_stage(client: TestClient):
    """The server owns every CV key. A credentials POST claiming a parse — or a
    sha into the shared asset store, which also holds de-identified clinical
    images — must be discarded."""
    token, hs_id, email = _seed_verified(client)
    client.post("/api/onboarding/asclepius/credentials", json={
        "token": token,
        "credentials": {**CREDS_MINIMAL, "cvAssetSha": "a" * 64,
                        "cvParsed": {"ok": True, "npi": "9999999999"},
                        "cvParseStage": "done"},
    })
    ts = client.app.state.team_store
    stored = (ts.get_asclepius_person(hs_id, email) or {}).get("credentials") or {}
    assert "cvAssetSha" not in stored
    assert "cvParsed" not in stored
    assert "cvParseStage" not in stored


def test_live_luhn_warns_but_never_blocks():
    """§8: live Luhn — bad check digit warns, never blocks.

    The port is exercised against the SERVER's implementation on the same
    numbers, which is what stops the two drifting.
    """
    npi_ts = Path(__file__).resolve().parents[2] / "landing/src/lib/npi.ts"
    src = npi_ts.read_text(encoding="utf-8")
    assert "This doesn't look like a valid NPI, double-check?" in src
    # It is a hint, never an `error`: the field the Review screen renders passes
    # npiWarning() to `hint`, and `error` only outside review mode.
    steps = (Path(__file__).resolve().parents[2]
             / "landing/src/app/components/onboarding/steps.tsx").read_text(encoding="utf-8")
    assert 'hint={npiWarning(c.npi) || "National Provider Identifier (10 digits)."}' in steps
    assert "error={!reviewMode && c.npi.length > 0" in steps
    # Server-side truth for the same rule, so a valid number never warns.
    assert credentialing.npi_checksum_ok("1234567893")
    assert not credentialing.npi_checksum_ok("1234567890")


def test_review_mode_requires_only_name_and_specialty():
    """The client gate matches the server's: Submit is live once those two are in."""
    steps = (Path(__file__).resolve().parents[2]
             / "landing/src/app/components/onboarding/steps.tsx").read_text(encoding="utf-8")
    assert ("const reviewValid =\n"
            "    c.fullLegalName.trim().length > 0 && c.primarySpecialty.trim().length > 0;") in steps
    assert "const valid = reviewMode ? reviewValid" in steps


# ═════════════════════════════════════════════════════════════════════════════
# resume / nudge
# ═════════════════════════════════════════════════════════════════════════════

def _capture_nudges(monkeypatch) -> list:
    """Record (recipient, subject) for every nudge the sweep sends.

    Assertions below are scoped to ONE invite's address rather than to the
    sweep's totals, deliberately: team.db is shared across the suite, so a
    total counts whatever other tests happened to leave unfinished. The
    property under test is per-application anyway — "this physician is nudged
    exactly once" — and a global count would only ever have tested run order.
    """
    import email_utils  # noqa: PLC0415

    sent: list = []

    async def _send(to, subject, html_body, **kwargs):  # noqa: ANN001
        sent.append((to, subject))
        return True

    monkeypatch.setattr(email_utils, "send_html_email", _send)
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: True)
    return sent


def _subjects_for(sent: list, email: str) -> list:
    return [subject for to, subject in sent if to == email]


@pytest.mark.anyio
async def test_nudge_fires_once_at_24h_and_never_twice(client: TestClient, monkeypatch):
    """§8: nudge fires once at >24h unfinished; never twice."""
    token, hs_id, email = _seed_verified(client)
    ts = client.app.state.team_store
    _age_invite(client, hs_id, hours=30)
    sent = _capture_nudges(monkeypatch)

    await onboarding_nudge.sweep(ts)
    # A row aged 30 hours is past the one-hour resume threshold as well, so the
    # "pick up any time" mail rides along. What this test is about is the 24
    # hour nudge and its stamp, so assert on that specifically rather than on
    # the whole set.
    subjects = _subjects_for(sent, email)
    assert "Your application is waiting: 2 minutes to finish" in subjects
    assert ts.get_health_system_by_id(hs_id)["nudge_sent_at"]
    before = len(subjects)

    # Again, immediately. The stamp is the whole idempotency mechanism, so a
    # second sweep must send this physician nothing at all.
    await onboarding_nudge.sweep(ts)
    assert len(_subjects_for(sent, email)) == before


@pytest.mark.anyio
async def test_day_six_expiry_warning_fires_once(client: TestClient, monkeypatch):
    """§8: day-6 expiry warning once."""
    token, hs_id, email = _seed_verified(client)
    ts = client.app.state.team_store
    _age_invite(client, hs_id, hours=150)   # past both thresholds
    sent = _capture_nudges(monkeypatch)

    await onboarding_nudge.sweep(ts)
    # Three now, not two: the "pick up any time" mail moved off the mint and
    # onto this sweep at the one-hour mark, and this row is aged past every
    # threshold at once. Each still has its own stamp, which is what the second
    # sweep below is checking.
    assert sorted(_subjects_for(sent, email)) == sorted([
        "Pick up your Archangel Health application any time",
        "Your application is waiting: 2 minutes to finish",
        "Your Archangel Health link expires tomorrow",
    ])
    row = ts.get_health_system_by_id(hs_id)
    assert row["resume_sent_at"] and row["nudge_sent_at"] and row["expiry_warned_at"]

    await onboarding_nudge.sweep(ts)
    assert len(_subjects_for(sent, email)) == 3


@pytest.mark.anyio
async def test_a_finished_application_is_never_nudged(client: TestClient, monkeypatch):
    """The one thing worse than no nudge: nudging someone who already applied."""
    token, hs_id, email = _seed_verified(client)
    client.post("/api/onboarding/asclepius/credentials",
                json={"token": token, "credentials": CREDS_MINIMAL})
    client.post("/api/onboarding/asclepius/attestations",
                json={"token": token, "attestations": ATTS})
    client.post("/api/onboarding/asclepius/finish", json={"token": token})
    _age_invite(client, hs_id, hours=200)
    sent = _capture_nudges(monkeypatch)

    ts = client.app.state.team_store
    await onboarding_nudge.sweep(ts)
    assert _subjects_for(sent, email) == []


@pytest.mark.anyio
async def test_no_mail_transport_stamps_nothing(client: TestClient, monkeypatch):
    """A deployment with no mail configured must not silently burn the one
    nudge every physician gets."""
    token, hs_id, email = _seed_verified(client)
    _age_invite(client, hs_id, hours=30)
    import email_utils
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: False)
    ts = client.app.state.team_store
    # Every kind reports zero, including the three post-submit ones: the
    # transport check is one early return in front of all of them, so a
    # deployment with no mail configured burns nobody's one nudge.
    assert (await onboarding_nudge.sweep(ts)) == {
        "resume": 0, "nudge": 0, "expiry": 0,
        "credentials": 0, "practice": 0, "profile": 0}
    row = ts.get_health_system_by_id(hs_id)
    assert row["resume_sent_at"] is None
    assert row["nudge_sent_at"] is None
    assert row["expiry_warned_at"] is None


def test_resume_restores_the_exact_screen_and_state(client: TestClient):
    """§8: resume link restores the exact screen and state.

    The session payload is what the wizard resumes from, so this asserts the
    payload carries everything the CV and Review screens need.
    """
    token, hs_id, email = _seed_verified(client)
    cv = ("Amara N. Okafor, MD\n\nBoard-certified in Nephrology\n"
          "Residency, Internal Medicine, Massachusetts General Hospital, 2012-2015\n"
          "12 years of clinical practice\n")
    client.post("/api/onboarding/asclepius/cv", data={"token": token},
                files={"file": ("cv.txt", cv.encode("utf-8"), "text/plain")})

    s = client.get(f"/api/onboarding/session?token={token}").json()
    assert s["status"] == "pending"
    assert s["director_cv"]["uploaded"] is True
    assert s["director_cv"]["stage"] == "done"
    assert s["director_cv"]["parsed"]["full_name"] == "Amara N. Okafor"
    assert s["director_cv"]["filename"] == "cv.txt"

    # After the Review screen saves, the resume point moves with it.
    client.post("/api/onboarding/asclepius/credentials",
                json={"token": token, "credentials": CREDS_MINIMAL})
    s2 = client.get(f"/api/onboarding/session?token={token}").json()
    assert s2["director_credentials"]["primarySpecialty"] == "Nephrology"


def test_a_second_link_for_an_applicant_reports_the_review_not_a_signin(client: TestClient):
    """A physician who already applied and asks for another link is not told to
    sign in to an account that has no password."""
    token, hs_id, email = _seed_verified(client)
    client.post("/api/onboarding/asclepius/credentials",
                json={"token": token, "credentials": CREDS_MINIMAL})
    client.post("/api/onboarding/asclepius/attestations",
                json={"token": token, "attestations": ATTS})
    client.post("/api/onboarding/asclepius/finish", json={"token": token})

    ts = client.app.state.team_store
    invite = ts.create_health_system_invite(
        invite_base_url="http://localhost:5173", product="asclepius",
        director_email=email)
    token2 = invite["onboarding_url"].rsplit("/", 1)[-1]
    s = client.get(f"/api/onboarding/session?token={token2}").json()
    assert s["status"] == "application_pending"
    assert s["verification_status"] == "pending"


# ═════════════════════════════════════════════════════════════════════════════
# credentials
# ═════════════════════════════════════════════════════════════════════════════

def test_approve_mints_a_hashed_temp_password_and_sends_the_welcome(client: TestClient, monkeypatch):
    """§8: approve mints hashed temp password + must_change_password=1 + sends 4.4."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    applicant = store.provision_user(
        email=f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org",
        password_hash=asc_store_mod.NO_PASSWORD_HASH,
        role="evaluator", full_name="Amara Okafor", specialty="nephrology",
        credentials={}, attestations={},
    )
    store.set_verification_status(applicant["id"], "pending")

    sent: list = []

    async def _send(to, subject, html_body, **kwargs):  # noqa: ANN001
        sent.append({"to": to, "subject": subject, "html": html_body})
        return True

    import routers.asclepius_verify as verify_module
    monkeypatch.setattr(verify_module, "send_html_email", _send)
    monkeypatch.setattr(verify_module, "is_email_transport_configured", lambda: True)

    c = TestClient(app)
    r = c.post(f"/api/asclepius/verify/queue/{applicant['id']}/approve",
               json={"tier": "labeler"}, headers=headers_for(admin))
    assert r.status_code == 200, r.text

    fresh = store.get_user_by_id(applicant["id"])
    assert fresh["must_change_password"] == 1
    assert not asc_store_mod.password_is_unset(fresh)
    assert fresh["password_hash"] not in ("", None)

    assert len(sent) == 1
    assert sent[0]["subject"] == "Welcome to Archangel Health, Dr. Okafor"
    html = sent[0]["html"]
    # The credential is in the email, which is the whole ask...
    assert "Temporary password" in html
    # ...and the mission block and the founders' intro are there with it (§4.4).
    assert "The hardest cases become the most valuable data." in html
    assert "calendly.com/tejpatel-berkeley" in html
    # The plaintext password is never written to the audit log.
    events = store.list_events(entity_type="user", entity_id=applicant["id"]) \
        if hasattr(store, "list_events") else []
    for e in events:
        assert "password" not in json.dumps(e.get("payload") or {}).lower() \
            or e.get("event_type") == "temp_password_issued"


def test_a_failed_credential_mint_sends_nothing_and_says_so(client: TestClient, monkeypatch):
    """"You're approved, open your workspace" pointing at a door this physician
    has no key to is worse than silence — and the admin who clicked approve is
    the only person positioned to notice, so the response has to say it."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    applicant = store.provision_user(
        email=f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org",
        password_hash=asc_store_mod.NO_PASSWORD_HASH,
        role="evaluator", full_name="Amara Okafor", credentials={}, attestations={},
    )
    store.set_verification_status(applicant["id"], "pending")

    sent: list = []

    async def _send(to, subject, html_body, **kwargs):  # noqa: ANN001
        sent.append(subject)
        return True

    def _boom(*_a, **_k):
        raise RuntimeError("disk is full")

    import routers.asclepius_verify as verify_module
    monkeypatch.setattr(verify_module, "send_html_email", _send)
    monkeypatch.setattr(verify_module, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(store, "set_temp_password", _boom)

    c = TestClient(app)
    r = c.post(f"/api/asclepius/verify/queue/{applicant['id']}/approve",
               json={"tier": "labeler"}, headers=headers_for(admin))
    # The approval still commits — a credential-minting failure must never undo
    # a decision an admin has made.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verification_status"] == "approved"
    assert body["credentials_issued"] is False
    assert body["welcome_email_sent"] is False
    assert "no sign-in details" in body["warning"]
    assert sent == [], "an approval with no credential must not promise a workspace"


def test_a_successful_approval_reports_that_the_welcome_went_out(client: TestClient, monkeypatch):
    store = fresh_store()
    admin = make_user(store, role="admin")
    applicant = store.provision_user(
        email=f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org",
        password_hash=asc_store_mod.NO_PASSWORD_HASH,
        role="evaluator", full_name="Amara Okafor", credentials={}, attestations={},
    )
    store.set_verification_status(applicant["id"], "pending")

    async def _send(to, subject, html_body, **kwargs):  # noqa: ANN001
        return True

    import routers.asclepius_verify as verify_module
    monkeypatch.setattr(verify_module, "send_html_email", _send)
    monkeypatch.setattr(verify_module, "is_email_transport_configured", lambda: True)

    c = TestClient(app)
    body = c.post(f"/api/asclepius/verify/queue/{applicant['id']}/approve",
                  json={"tier": "labeler"}, headers=headers_for(admin)).json()
    assert body["credentials_issued"] is True
    assert body["welcome_email_sent"] is True
    assert body["warning"] is None


def test_approving_an_account_that_already_has_a_password_does_not_rotate_it(client: TestClient, monkeypatch):
    """An invited member or a pre-v2 signup chose their own password. Minting
    over it would replace a credential they are using today."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    member = make_user(store, tier=None)
    original = store.get_user_by_id(member["id"])["password_hash"]
    store.set_verification_status(member["id"], "pending")

    sent: list = []

    async def _send(to, subject, html_body, **kwargs):  # noqa: ANN001
        sent.append(subject)
        return True

    import routers.asclepius_verify as verify_module
    monkeypatch.setattr(verify_module, "send_html_email", _send)
    monkeypatch.setattr(verify_module, "is_email_transport_configured", lambda: True)

    c = TestClient(app)
    assert c.post(f"/api/asclepius/verify/queue/{member['id']}/approve",
                  json={"tier": "labeler"}, headers=headers_for(admin)).status_code == 200

    fresh = store.get_user_by_id(member["id"])
    assert fresh["password_hash"] == original
    assert not fresh["must_change_password"]

    # The welcome IS sent here now, and that is the change. It used to fall
    # through to a plain queued notice, so a physician who chose their own
    # password silently lost the mission block, the sign-in button and the
    # founders' Calendly: the whole content of the welcome, missing, because of
    # an implementation detail about where their password came from. Since the
    # wizard started taking a password on screen one, that is nearly everyone.
    assert len(sent) == 1, f"expected exactly one welcome, got {sent}"
    assert "Welcome" in sent[0] or "welcome" in sent[0].lower(), sent

    # And exactly one. The hook on record_verification_decision queued the plain
    # notice before this handler ran, and the handler voids it: two "you're
    # approved" emails for one approval is the visible failure here. Read off
    # the real drain queue rather than a guess, so this cannot pass vacuously.
    due = store.due_admin_notifications(limit=100)
    approvals = [r for r in due if "approved" in str(r.get("idempotency_key") or "")]
    assert not approvals, f"the queued notice was not voided: {approvals}"
    with store._conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT subject FROM admin_notify_outbox WHERE recipient_email = ? "
            "AND kind = 'physician_approved'", (member["email"],))]
    assert [r["subject"] for r in rows] == ["You're approved for Archangel Health"]


def test_first_login_forces_a_password_change_and_the_second_does_not(client: TestClient):
    """§8: first login forces password change, clears flag; second login doesn't."""
    store = fresh_store()
    u = make_user(store)
    store.set_temp_password(u["id"], "temp-Password-123")

    c = TestClient(app)
    r = c.post("/api/asclepius/auth/login",
               json={"email": u["email"], "password": "temp-Password-123"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["must_change_password"] is True
    token = r.json()["token"]

    r = c.post("/api/asclepius/auth/password/change",
               json={"current_password": "temp-Password-123",
                     "new_password": "a-password-they-chose-99"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    r = c.post("/api/asclepius/auth/login",
               json={"email": u["email"], "password": "a-password-they-chose-99"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["must_change_password"] is False


def test_pending_no_password_login_returns_the_pending_gate(client: TestClient):
    """§8: pending/no-password login → authGate 'pending' copy.

    Not "invalid email or password", which is false in both halves and sends a
    physician to a reset flow that cannot help them.
    """
    store = fresh_store()
    applicant = store.provision_user(
        email=f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org",
        password_hash=asc_store_mod.NO_PASSWORD_HASH,
        role="evaluator", full_name="Amara Okafor", credentials={}, attestations={},
    )
    store.set_verification_status(applicant["id"], "pending")

    c = TestClient(app)
    r = c.post("/api/asclepius/auth/login",
               json={"email": applicant["email"], "password": "anything-at-all"})
    assert r.status_code == 403, r.text
    assert r.headers.get(asc_auth.AUTH_GATE_HEADER) == "pending"
    assert "in review" in r.json()["detail"]
    assert "24–48 hours" in r.json()["detail"]


def test_an_unknown_address_still_gets_the_generic_401(client: TestClient):
    """The pending answer must not become an account-existence oracle for
    accounts that HAVE a credential — only for ones that have none."""
    fresh_store()
    c = TestClient(app)
    r = c.post("/api/asclepius/auth/login",
               json={"email": "nobody@example.org", "password": "x"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password"


# ═════════════════════════════════════════════════════════════════════════════
# walkthrough
# ═════════════════════════════════════════════════════════════════════════════

def test_first_run_persists_per_stop_and_a_defer_is_not_permanent(client: TestClient):
    """§8: first_run_json persists per stop across devices.

    Welcome package v2 §1 REPLACED this test's original second half. It used to
    assert that a skip was permanent and could never upgrade to ``done``, which
    is precisely the rule that made the walkthrough ask about the optional stops
    exactly once and then go silent forever. The outcome is ``deferred`` now, it
    means "asked, declined this session", and finishing the stop later — from the
    re-entry page, the banner, or the dashboard chip — has to be allowed to write
    ``done`` over it. What stays permanent is ``done``: see the monotonic case
    below and ``test_a_required_stop_cannot_be_deferred``.
    """
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)

    r = c.patch("/api/asclepius/me/first-run",
                json={"action": "done", "stop": "welcome"}, headers=headers_for(u))
    assert r.status_code == 200, r.text
    assert r.json()["first_run"]["stops"] == {"welcome": "done"}

    r = c.patch("/api/asclepius/me/first-run",
                json={"action": "defer", "stop": "community"}, headers=headers_for(u))
    assert r.json()["first_run"]["stops"]["community"] == "deferred"

    # Server-side, so a different device (a fresh token, no client state) sees it.
    r = c.get("/api/asclepius/auth/me", headers=headers_for(u))
    assert r.json()["first_run"]["stops"] == {"welcome": "done", "community": "deferred"}

    # A deferred stop is still open work: finishing it later records 'done'.
    c.patch("/api/asclepius/me/first-run",
            json={"action": "done", "stop": "community"}, headers=headers_for(u))
    r = c.get("/api/asclepius/auth/me", headers=headers_for(u))
    assert r.json()["first_run"]["stops"]["community"] == "done"

    # ...and 'done' does NOT decay back to deferred on a later ask.
    c.patch("/api/asclepius/me/first-run",
            json={"action": "defer", "stop": "community"}, headers=headers_for(u))
    r = c.get("/api/asclepius/auth/me", headers=headers_for(u))
    assert r.json()["first_run"]["stops"]["community"] == "done"


def test_a_required_stop_cannot_be_deferred(client: TestClient):
    """§1/§5: welcome, start and practice have no skip control, and the refusal
    is the SERVER's — a client that invents one is refused, not obeyed."""
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    for stop in ("welcome", "start", "practice"):
        for action in ("defer", "skip"):
            r = c.patch("/api/asclepius/me/first-run",
                        json={"action": action, "stop": stop}, headers=headers_for(u))
            assert r.status_code == 400, (stop, action, r.text)
            assert r.json()["detail"]["error"] == "stop_is_required"
    # Nothing was written by any of those refusals.
    fr = c.get("/api/asclepius/auth/me", headers=headers_for(u)).json()["first_run"]
    assert fr["stops"] == {}


def test_the_old_skip_word_still_defers_an_optional_stop(client: TestClient):
    """A physician holding a stale tab through the deploy must not be 422'd
    mid-walkthrough, so 'skip' is kept as an alias for 'defer'."""
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    r = c.patch("/api/asclepius/me/first-run",
                json={"action": "skip", "stop": "manual"}, headers=headers_for(u))
    assert r.status_code == 200, r.text
    assert r.json()["first_run"]["stops"]["manual"] == "deferred"


def test_defer_all_closes_every_remaining_optional_stop(client: TestClient):
    """§4.2: leaving the re-entry page is ONE request, not three racing ones."""
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    c.patch("/api/asclepius/me/first-run",
            json={"action": "done", "stop": "community"}, headers=headers_for(u))
    r = c.patch("/api/asclepius/me/first-run",
                json={"action": "defer_all"}, headers=headers_for(u))
    assert r.status_code == 200, r.text
    stops = r.json()["first_run"]["stops"]
    # The finished one is untouched; the other two are deferred. Required stops
    # are never swept up by this — they are not optional and have no defer.
    assert stops["community"] == "done"
    assert stops["earnings"] == "deferred"
    assert stops["manual"] == "deferred"
    assert "welcome" not in stops and "start" not in stops and "practice" not in stops


def test_deferring_every_optional_stop_does_not_complete_the_checklist(client: TestClient):
    """§1: 'Deferred stops never complete it.' This is the bug that made the
    walkthrough never return — the old model marked the set complete as soon as
    every stop carried ANY outcome."""
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    for stop in ("welcome", "start", "practice"):
        c.patch("/api/asclepius/me/first-run",
                json={"action": "done", "stop": stop}, headers=headers_for(u))
    c.patch("/api/asclepius/me/first-run",
            json={"action": "defer_all"}, headers=headers_for(u))
    fr = c.get("/api/asclepius/auth/me", headers=headers_for(u)).json()["first_run"]
    assert fr["completed_at"] is None


def test_closing_all_six_stops_completes_the_checklist(client: TestClient):
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    from asclepius.schemas import FIRST_RUN_STOPS
    for stop in FIRST_RUN_STOPS:
        c.patch("/api/asclepius/me/first-run",
                json={"action": "done", "stop": stop}, headers=headers_for(u))
    fr = c.get("/api/asclepius/auth/me", headers=headers_for(u)).json()["first_run"]
    assert fr["completed_at"]


def test_an_unknown_stop_id_is_refused(client: TestClient):
    """The stop vocabulary is the server's. A checklist whose '3 of 6' nobody
    can reproduce is worse than one that refuses an unknown id."""
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    r = c.patch("/api/asclepius/me/first-run",
                json={"action": "done", "stop": "invented"}, headers=headers_for(u))
    assert r.status_code == 422


def test_practice_case_completion_checks_the_checklist_via_the_tutorial_event(client: TestClient):
    """§8: practice-case completion checks the checklist via the EXISTING
    tutorial event — not a parallel tracker the client has to remember."""
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    r = c.patch("/api/asclepius/me/tutorial", json={"action": "complete"},
                headers=headers_for(u))
    assert r.status_code == 200, r.text
    assert r.json()["first_run"]["stops"]["practice"] == "done"

    # Skip is retired: the practice case is a hard gate on real work, so a
    # skip grants nothing and is refused as a no-op. The checklist box stays
    # OPEN — it points at work the physician still owes.
    u2 = make_user(store)
    r = c.patch("/api/asclepius/me/tutorial", json={"action": "skip"},
                headers=headers_for(u2))
    assert r.status_code == 200
    assert "practice" not in (r.json()["first_run"].get("stops") or {})
    assert r.json()["tutorial"]["status"] != "skipped"


def test_bank_link_interest_is_recorded_once(client: TestClient):
    """§6 stop 5: the card is disabled and clearly labelled; this is all that is
    behind it, and it stores a status rather than pretending to link anything."""
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    assert c.post("/api/asclepius/me/bank-link/interest",
                  headers=headers_for(u)).json()["bank_link_status"] == "coming_soon"
    assert store.get_user_by_id(u["id"])["bank_link_status"] == "coming_soon"


def test_existing_contributors_are_not_dropped_into_the_walkthrough_on_deploy(client: TestClient):
    """The backfill. A physician who has been labeling for months must not meet
    "Welcome to Archangel Health" on the deploy that ships §6.

    Scoped to accounts that have already been INSIDE the portal, so someone who
    applied the day before still gets the welcome they were always going to get.
    """
    import sqlite3 as _sqlite3

    store = fresh_store()
    veteran = make_user(store)
    store.set_verification_status(veteran["id"], "approved")
    applicant = make_user(store, tier=None)
    store.set_verification_status(applicant["id"], "pending")

    # Rewind to the moment before the column existed, then re-run the migration.
    with _sqlite3.connect(store.db_path) as conn:
        conn.execute("ALTER TABLE users RENAME COLUMN first_run_json TO _gone")
        conn.execute("ALTER TABLE users DROP COLUMN _gone")
    store._init_schema()

    assert store.get_first_run(veteran["id"])["dismissed_at"], \
        "an existing contributor was going to be shown the first-login walkthrough"
    assert store.get_first_run(applicant["id"])["dismissed_at"] is None, \
        "an application still waiting on us must still get its welcome"

    # And it is a ONE-TIME backfill: a later boot cannot re-dismiss a checklist
    # somebody is halfway through.
    store.set_first_run(veteran["id"], {"version": store.FIRST_RUN_VERSION,
                                        "stops": {"welcome": "done"},
                                        "completed_at": None, "dismissed_at": None})
    store._init_schema()
    assert store.get_first_run(veteran["id"])["dismissed_at"] is None


def test_the_full_v2_wizard_order_completes_over_http(client: TestClient):
    """Walk the six screens in the order the wizard actually visits them.

    The suite's other signups POST the endpoints in whatever order the test
    author wrote. This one follows `orderFor`, because v2 dropped the
    institution screen that used to seed the director's row — so the CV upload
    and the Review save are now the FIRST things to touch it, and an ordering
    bug there is invisible to a test that calls /institution first.
    """
    token, hs_id, email = _seed_verified(client)

    # 3. CV — before anything has created the person row.
    cv = "Amara N. Okafor, MD\n\nBoard-certified in Nephrology\n12 years of clinical practice\n"
    assert client.post("/api/onboarding/asclepius/cv", data={"token": token},
                       files={"file": ("cv.txt", cv.encode("utf-8"), "text/plain")}
                       ).status_code == 200
    # 4. Review.
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": token, "credentials": CREDS_MINIMAL}).status_code == 200
    # 5. Attestations.
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": token, "attestations": ATTS}).status_code == 200
    # 6. Submitted.
    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 200, f"the v2 order cannot be completed: {r.text}"
    assert r.json()["awaiting_review"] is True

    asc = client.app.state.asclepius_store
    u = asc.get_user_by_email(email)
    assert u and u["active"] and u["cv_asset_sha"], "the CV never reached the account"
    assert u["verification_status"] == "pending"


# ─── The demo video endpoint ─────────────────────────────────────────────────

def _install_demo(store, payload: bytes = b"z" * 4000 + b"TAIL"):
    meta = asc_assets.store_media(iter([payload]), "video/mp4")
    store.set_platform_media("onboarding_demo", sha256=meta["sha256"], mime="video/mp4",
                             byte_size=meta["byte_size"], filename="demo.mp4")
    return payload


def test_video_endpoint_honours_range_and_requires_auth(client: TestClient):
    """§8: video endpoint honors Range (206) — seek works; auth required."""
    store = fresh_store()
    u = make_user(store)
    data = _install_demo(store)
    c = TestClient(app)

    r = c.get("/api/asclepius/assets/onboarding-demo", headers=headers_for(u))
    assert r.status_code == 200
    assert r.content == data
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["cache-control"] == "private, max-age=86400"

    # A seek into the middle: the 206 and the Content-Range are what make the
    # player's timeline real rather than decorative.
    r = c.get("/api/asclepius/assets/onboarding-demo",
              headers={**headers_for(u), "Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == data[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{len(data)}"

    # The suffix form a player uses to read an MP4's trailing moov atom.
    r = c.get("/api/asclepius/assets/onboarding-demo",
              headers={**headers_for(u), "Range": "bytes=-4"})
    assert r.status_code == 206 and r.content == b"TAIL"

    # Past the end.
    r = c.get("/api/asclepius/assets/onboarding-demo",
              headers={**headers_for(u), "Range": "bytes=99999-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(data)}"

    # Auth required.
    assert c.get("/api/asclepius/assets/onboarding-demo").status_code == 401


def test_a_matching_validator_saves_the_re_download(client: TestClient):
    """The one avoidable cost on this route is re-sending 73 MB to a browser
    that already has it. A Range request is deliberately NOT short-circuited:
    that is a separate negotiation, and half-implementing it breaks seeking.
    """
    store = fresh_store()
    u = make_user(store)
    data = _install_demo(store)
    c = TestClient(app)

    first = c.get("/api/asclepius/assets/onboarding-demo", headers=headers_for(u))
    etag = first.headers["etag"]
    assert etag

    again = c.get("/api/asclepius/assets/onboarding-demo",
                  headers={**headers_for(u), "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""

    # A stale validator still gets the body.
    stale = c.get("/api/asclepius/assets/onboarding-demo",
                  headers={**headers_for(u), "If-None-Match": '"nope"'})
    assert stale.status_code == 200 and stale.content == data

    # And a range is served even when the validator matches.
    ranged = c.get("/api/asclepius/assets/onboarding-demo",
                   headers={**headers_for(u), "If-None-Match": etag, "Range": "bytes=0-9"})
    assert ranged.status_code == 206 and ranged.content == data[:10]


def test_the_literal_demo_path_is_not_swallowed_by_the_asset_id_route(client: TestClient):
    """asclepius_router owns /assets/{asset_id} and FastAPI matches in
    registration order, so the media router has to be mounted FIRST. This is
    what catches someone reordering the mounts in main.py."""
    store = fresh_store()
    u = make_user(store)
    _install_demo(store)
    c = TestClient(app)
    r = c.get("/api/asclepius/assets/onboarding-demo", headers=headers_for(u))
    assert r.status_code == 200, "the {asset_id} route swallowed the literal path"


def test_a_media_ticket_plays_the_demo_and_can_do_nothing_else(client: TestClient):
    """A <video src> cannot send a header. The ticket is what it uses instead —
    and it must not be usable as a session token."""
    store = fresh_store()
    u = make_user(store)
    data = _install_demo(store)
    c = TestClient(app)

    ticket = c.post("/api/asclepius/assets/onboarding-demo/ticket",
                    headers=headers_for(u)).json()["ticket"]
    r = c.get(f"/api/asclepius/assets/onboarding-demo?t={ticket}",
              headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == data[:10]

    # Not an API credential. Points at a live session-gated route on purpose:
    # against a route that no longer exists this would pass on the 404 and
    # stop testing that a media ticket is refused as a bearer token.
    assert c.get("/api/asclepius/me/profile",
                 headers={"Authorization": f"Bearer {ticket}"}).status_code == 401
    # And a session token is not a ticket.
    assert c.get(f"/api/asclepius/assets/onboarding-demo?t={token_for(u)}").status_code == 401
    # Nor is a ticket minted for some other slot.
    other = asc_auth.create_media_ticket(u, slot="something_else")
    assert c.get(f"/api/asclepius/assets/onboarding-demo?t={other}").status_code == 401


def test_demo_meta_reports_absence_rather_than_offering_a_broken_card(client: TestClient):
    """The walkthrough asks this before rendering stop 2, so a deployment that
    has not had the video uploaded shows the practice case alone."""
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    body = c.get("/api/asclepius/assets/onboarding-demo/meta", headers=headers_for(u)).json()
    assert body["available"] is False


def test_meta_states_the_real_upload_limit(client: TestClient):
    """The admin drop zone prints the limit and pre-checks against it. A number
    hardcoded in the client would drift from ASCLEPIUS_MEDIA_MAX_BYTES and start
    telling operators a file is too big when it is not."""
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)

    # Present whether or not a demo is installed — the panel draws the drop zone
    # in both states.
    empty = c.get("/api/asclepius/assets/onboarding-demo/meta", headers=headers_for(u)).json()
    assert empty["available"] is False
    assert empty["max_upload_bytes"] == asc_assets.media_max_bytes()

    _install_demo(store)
    full = c.get("/api/asclepius/assets/onboarding-demo/meta", headers=headers_for(u)).json()
    assert full["available"] is True
    assert full["max_upload_bytes"] == asc_assets.media_max_bytes()

    # And the default comfortably clears the 72.8 MB demo it exists for.
    assert asc_assets.media_max_bytes() >= 100 * 1024 * 1024


def test_a_seventy_three_megabyte_upload_goes_through(client: TestClient, monkeypatch):
    """The demo is ~73 MB. Nothing in the request path may cap below it: no body
    middleware, no buffering of the whole file, and a store cap far above it."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    c = TestClient(app)
    # The suite's asset store lives under /tmp, which the durability gate
    # correctly refuses — see the test below, which pins that refusal. Stand it
    # down HERE so this test exercises the thing it is about: the size.
    import routers.asclepius_media as media_module
    monkeypatch.setattr(media_module.assets, "asset_storage_durable",
                        lambda: (True, "test volume"))

    # A real 73 MB body, streamed through the actual multipart path.
    payload = b"\x00" * (73 * 1024 * 1024)
    r = c.post("/api/asclepius/admin/assets/onboarding-demo",
               headers=headers_for(admin),
               files={"file": ("demo.mp4", payload, "video/mp4")})
    assert r.status_code == 200, r.text[:400]
    assert r.json()["byte_size"] == len(payload)
    assert r.json()["warning"] is None

    # And it serves back byte-for-byte, including a seek near the end.
    viewer = make_user(store)
    tail = c.get("/api/asclepius/assets/onboarding-demo",
                 headers={**headers_for(viewer), "Range": "bytes=-16"})
    assert tail.status_code == 206 and tail.content == payload[-16:]


def test_an_upload_onto_ephemeral_storage_is_refused(client: TestClient):
    """A demo that plays today and 404s on Tuesday is worse than a refused
    upload, because nobody is watching for it. The suite's own store is under
    /tmp, so this is the real gate refusing a real request."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    c = TestClient(app)
    r = c.post("/api/asclepius/admin/assets/onboarding-demo",
               headers=headers_for(admin),
               files={"file": ("demo.mp4", b"data", "video/mp4")})
    assert r.status_code == 503
    # And it names the variable to set, rather than just saying no.
    assert "ASCLEPIUS_ASSET_STORE" in r.json()["detail"]


def test_uploading_the_demo_is_admin_only(client: TestClient):
    store = fresh_store()
    u = make_user(store)
    c = TestClient(app)
    r = c.post("/api/asclepius/admin/assets/onboarding-demo",
               headers=headers_for(u),
               files={"file": ("d.mp4", b"data", "video/mp4")})
    assert r.status_code == 403


def test_the_asset_store_refuses_a_non_video(client: TestClient):
    with pytest.raises(asc_assets.UnsupportedMediaType):
        asc_assets.store_media(iter([b"not a video"]), "application/zip")


def test_media_upload_is_capped(client: TestClient):
    with pytest.raises(asc_assets.MediaTooLarge):
        asc_assets.store_media(iter([b"x" * 500, b"y" * 500]), "video/mp4", max_bytes=600)


# ═════════════════════════════════════════════════════════════════════════════
# emails
# ═════════════════════════════════════════════════════════════════════════════

def test_the_four_builders_render_and_are_in_the_preview():
    """§8: emails — four builders render in email_preview.py (in its _cases)."""
    built = {
        "start": oe.build_application_start_email(
            first_name="Amara", onboarding_url="https://x/onboard/t", expires_days=7),
        "nudge": oe.build_application_nudge_email(
            first_name="Amara", onboarding_url="https://x/onboard/t"),
        "expiring": oe.build_application_expiring_email(
            first_name="Amara", onboarding_url="https://x/onboard/t"),
        "submitted": oe.build_application_submitted_email(full_name="Amara Okafor"),
        "welcome": oe.build_application_welcome_email(
            full_name="Amara Okafor", email="a@x.org", temp_password="Kf3-tQ92mXbW7p",
            sign_in_url="https://x/asclepius"),
    }
    for name, html in built.items():
        assert html.startswith("<!doctype html>"), name
        assert "Archangel Health" in html, name
        assert "Tej" in html and "Aryaa" in html, f"{name} is not signed by the founders"

    # The PRD copy, verbatim where it says verbatim.
    assert "You&rsquo;re most of the way there." in built["nudge"]
    assert "We read every application personally" in built["nudge"]
    assert "within 24&ndash;48 hours" in built["submitted"] \
        or "24–48 hours" in built["submitted"]
    assert "We keep review human on purpose" in built["submitted"]
    assert ("Doctors earn from their judgment. Models learn from it. "
            "The hardest cases become the most valuable data.") in built["welcome"]
    assert "Verification is the scarce input in medical AI." in built["welcome"]
    assert "Kf3-tQ92mXbW7p" in built["welcome"]
    assert oe.FOUNDER_INTRO_CALENDLY in built["welcome"]

    preview = (Path(__file__).resolve().parent.parent / "scripts/email_preview.py") \
        .read_text(encoding="utf-8")
    for builder in ("build_application_start_email", "build_application_nudge_email",
                    "build_application_expiring_email", "build_application_submitted_email",
                    "build_application_welcome_email"):
        assert builder in preview, f"{builder} is not in the email preview"


def test_physician_names_are_escaped_not_double_escaped():
    """A physician named O'Brien is greeted by name, not by an entity."""
    html = oe.build_application_submitted_email(full_name="Amara O'Brien")
    assert "Dr. O&#x27;Brien" in html
    assert "O&amp;#x27;Brien" not in html


def test_the_submitted_email_is_what_a_v2_application_receives(client: TestClient, sent):
    """Not 'your workspace is ready' — nothing is ready, and saying so would be
    the first thing we got wrong."""
    token, _hs_id, email = _seed_verified(client)
    client.post("/api/onboarding/asclepius/credentials",
                json={"token": token, "credentials": CREDS_MINIMAL})
    client.post("/api/onboarding/asclepius/attestations",
                json={"token": token, "attestations": ATTS})
    client.post("/api/onboarding/asclepius/finish", json={"token": token})
    subjects = [m["subject"] for m in sent if m["to"] == email]
    assert "We've got your application" in subjects
    assert "Your Archangel Health workspace is ready" not in subjects
