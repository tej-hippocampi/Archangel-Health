"""Health-system onboarding, end to end: signup → intake → approval → DLA → uploads.

The §8 checklist of the onboarding PRD, one test per line, plus the cases that
line implies. The through-line of the file is that EVERY gate is asserted from
the outside — through the HTTP surface a real partner and a real operator use —
because the two gates involved (the account's surface and the organization's
state) are separate objects and the only place they are guaranteed to compose is
in the response.

The upload refusals are asserted at ALL FOUR doors, following the precedent
``test_hs_gating.py`` sets: a gate added to one door and forgotten on another is
not a gate, it is a detour sign.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

API = "/api/asclepius"
_KEY = base64.urlsafe_b64encode(b"hs-onboarding-test-key-32-byte!!").decode()
PASSWORD = "harbor-thistle-meadow-41"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("ENV", "test")
    # The portal's fixed response-time budget makes every request take at least
    # 120 ms, which is the right behaviour in production and forty seconds of
    # nothing across this file.
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _client() -> TestClient:
    return TestClient(A.app, base_url="https://testserver")


def _admin_headers(store):
    return A.headers_for(A.make_user(store, role="admin"))


@pytest.fixture()
def mail(monkeypatch):
    """Capture every email the flow would send, with its attachments."""
    sent = []

    async def _fake_send(to, subject, body, **kw):
        sent.append({"to": to, "subject": subject, "body": body,
                     "attachments": kw.get("attachments") or []})
        return True

    for module in ("routers.asclepius_provider", "routers.asclepius_admin"):
        monkeypatch.setattr(f"{module}.is_email_transport_configured",
                            lambda: True, raising=False)
        monkeypatch.setattr(f"{module}.send_html_email", _fake_send, raising=False)
    # The provider router mails members and the signed copy through the house
    # bridge, which imports send_html_email from the module rather than the
    # router, so both have to be replaced or half the letters vanish.
    monkeypatch.setattr("email_utils.send_html_email", _fake_send, raising=False)
    monkeypatch.setattr("email_utils.is_email_transport_configured",
                        lambda: True, raising=False)
    return sent


# ─── The whole flow, as one organization walks it ───────────────────────────
def _signup(client, *, email=None, org=None, password=""):
    """Signup + verify, returning the details the flow needs downstream.

    The staged code is hashed, so the test cannot read it back. It re-stages the
    same signup with a code it chose, which is what the resend path does anyway
    and exercises exactly the same verify.
    """
    store = _store()
    addr = email or f"it{uuid.uuid4().hex[:6]}@example.org"
    name = org or f"Test Health {uuid.uuid4().hex[:6]}"
    body = {"full_name": "Dana Reyes", "email": addr, "organization": name}
    if password:
        body["password"] = password
    r = client.post(f"{API}/hs/signup", json=body)
    assert r.status_code == 200, r.text
    code = "424242"
    store.create_hs_signup(email=addr, full_name="Dana Reyes", organization=name,
                           password=password or "x" * 40, code=code,
                           needs_temp_password=not password)
    r = client.post(f"{API}/hs/signup/verify", json={"email": addr, "code": code})
    assert r.status_code == 200, r.text
    payload = r.json()
    hs = [h for h in store.list_health_systems() if h["name"] == name][0]
    return {"email": addr, "organization": name, "hs_id": hs["hs_id"],
            "username": payload["username"], "must_reset": payload["must_reset"]}


def _rotate(client, new_password=PASSWORD):
    r = client.post(f"{API}/hs/password",
                    json={"current_password": "", "new_password": new_password})
    assert r.status_code == 200, r.text


def _apply(client, **overrides):
    body = {"authority": "not_sure", "deid_capability": "needs_baa",
            "export_scope": "varies", "scale_patients": "10k_50k",
            "scale_years": "5_10", "scale_specialties": ["Nephrology"]}
    body.update(overrides)
    return client.post(f"{API}/hs/application", json=body)


def _sign(client, *, name="Dana Reyes", title="Chief Information Officer",
          authority=True, esign=True, sha=None):
    return client.post(f"{API}/hs/agreement/sign", json={
        "typed_name": name, "typed_title": title,
        "authority_affirmed": authority, "consent_esign": esign,
        "doc_sha256": sha or ""})


def _approve(client, store, hs_id, **body):
    return client.post(f"{API}/admin/health-systems/{hs_id}/approve",
                       json=body, headers=_admin_headers(store))


# ════════════════════════════════════════════════════════════════════════════
#  §2 — org signup: three fields, OTP, straight into the portal
# ════════════════════════════════════════════════════════════════════════════
def test_three_fields_are_enough_and_the_org_lands_in_intake():
    client = _client()
    org = _signup(client)
    me = client.get(f"{API}/hs/me").json()
    assert me["organization"] == org["organization"]
    assert me["state"] == "intake"
    assert me["next_step"]
    # Signed in already. The username was derived from the organization name and
    # they have never seen it, so a bounce to a login form would strand them.
    assert me["username"] == org["username"]


def test_a_password_free_signup_is_mailed_a_temporary_one_and_must_replace_it(mail):
    client = _client()
    org = _signup(client)
    assert org["must_reset"] is True
    assert client.get(f"{API}/hs/me").json()["must_reset"] is True

    access = [m for m in mail if "your portal access" in m["subject"].lower()]
    assert len(access) == 1, [m["subject"] for m in mail]
    assert org["email"] == access[0]["to"]
    body = access[0]["body"]
    # §2.3: mission block, the credentials card, and the bookmark line.
    assert "Doctors earn from their judgment" in body
    assert "temporary" in body.lower()
    assert "Bookmark this email" in body

    # The temporary password in that email actually signs in. Read it back out
    # of the letter rather than out of the database: what matters is that the
    # credential the recipient can see is the credential that works.
    temp = re.search(r">([a-z]+-[a-z]+-[a-z]+-[0-9a-f]{6})<", body)
    assert temp, "the access email does not show a password"
    fresh = _client()
    r = fresh.post(f"{API}/hs/login",
                   json={"username": org["username"], "password": temp.group(1)})
    assert r.status_code == 200, r.text
    assert r.json()["must_reset"] is True

    # And it stops working the moment they choose their own — the forced
    # rotation, which is the whole reason a mailed credential is acceptable.
    _rotate(fresh)
    assert fresh.get(f"{API}/hs/me").json()["must_reset"] is False
    stale = _client()
    r = stale.post(f"{API}/hs/login",
                   json={"username": org["username"], "password": temp.group(1)})
    assert r.status_code == 401


def test_a_signup_that_chooses_a_password_is_not_forced_to_rotate(mail):
    client = _client()
    org = _signup(client, password=PASSWORD)
    assert org["must_reset"] is False
    assert client.get(f"{API}/hs/me").json()["must_reset"] is False
    # And it gets the other letter — the one whose job is to deliver the
    # username, because that is the only thing they do not already have.
    assert any("upload portal" in m["subject"].lower() for m in mail)


def test_the_abuse_guards_still_fire_on_the_three_field_door():
    client = _client()
    store = _store()
    addr = "spam@example.org"
    # Honeypot: same body, nothing staged.
    r = client.post(f"{API}/hs/signup", json={
        "full_name": "Bot", "email": addr, "organization": "Bot Health",
        "company_website": "http://spam"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store.get_live_hs_signup(addr) is None

    # Per-address cap, silently dropped rather than confirming the address.
    for _ in range(3):
        client.post(f"{API}/hs/signup", json={
            "full_name": "Dana", "email": addr, "organization": "Cap Health"})
    before = store.count_recent_hs_signups_for_email(addr)
    r = client.post(f"{API}/hs/signup", json={
        "full_name": "Dana", "email": addr, "organization": "Cap Health"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store.count_recent_hs_signups_for_email(addr) == before


def test_a_missing_field_is_refused_but_a_missing_password_is_not():
    client = _client()
    r = client.post(f"{API}/hs/signup",
                    json={"full_name": "", "email": "a@b.org", "organization": "X"})
    assert r.status_code == 400
    r = client.post(f"{API}/hs/signup",
                    json={"full_name": "Dana", "email": "a@b.org", "organization": "X"})
    assert r.status_code == 200
    # A password that IS supplied is still held to the policy.
    r = client.post(f"{API}/hs/signup", json={
        "full_name": "Dana", "email": "c@b.org", "organization": "X",
        "password": "short"})
    assert r.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
#  §3 — the intake application and the team
# ════════════════════════════════════════════════════════════════════════════
def test_the_four_questions_are_asked_in_the_prds_order():
    client = _client()
    _signup(client)
    _rotate(client)
    prompts = client.get(f"{API}/hs/application").json()["prompts"]
    assert [q["key"] for q in prompts] == [
        "authority", "deid_capability", "export_scope", "scale"]
    # Nothing blocks submission: every question has an honest escape hatch.
    # For the first two that is literally "Not sure"; for the third it is
    # "Depends by system", which is the same admission in the vocabulary that
    # question is actually asked in.
    for q in prompts[:2]:
        assert any(o["value"] == "not_sure" for o in q["options"])
    assert any(o["value"] == "varies" for o in prompts[2]["options"])
    scale = {f["key"]: f for f in prompts[3]["fields"]}
    assert scale["scale_specialties"]["kind"] == "multiselect"
    assert len(scale["scale_specialties"]["options"]) > 10


def test_not_sure_answers_submit_and_land_verbatim_on_the_admin(mail):
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    r = _apply(client, authority="not_sure", deid_capability="not_sure",
               export_scope="varies", scale_patients="not_sure",
               scale_years="not_sure", scale_specialties=["Nephrology", "Cardiology"])
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "submitted"

    detail = client.get(f"{API}/admin/health-systems/{org['hs_id']}",
                        headers=_admin_headers(store)).json()
    assert detail["onboarding_state"] == "submitted"
    application = detail["applications"][0]
    answers = {a["key"]: a for a in application["answers"]}
    assert answers["authority"]["value"] == "not_sure"
    # Verbatim means the WORDS they saw, not only the token we stored.
    assert answers["authority"]["words"] == "Not sure"
    assert answers["deid_capability"]["words"] == "Not sure"
    assert answers["export_scope"]["words"] == "Depends by system"
    assert application["specialties"] == ["Nephrology", "Cardiology"]
    assert application["authority_unclear"] is True


def test_a_baa_answer_is_flagged_for_the_operator():
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    _apply(client, deid_capability="needs_baa")
    detail = client.get(f"{API}/admin/health-systems/{org['hs_id']}",
                        headers=_admin_headers(store)).json()
    assert detail["applications"][0]["needs_baa"] is True
    listing = client.get(f"{API}/admin/health-systems",
                         headers=_admin_headers(store)).json()
    row = [h for h in listing["health_systems"] if h["hs_id"] == org["hs_id"]][0]
    assert row["application"]["needs_baa"] is True
    assert row["onboarding_state"] == "submitted"


def test_an_invented_answer_is_refused():
    client = _client()
    _signup(client)
    _rotate(client)
    assert _apply(client, authority="maybe").status_code == 400
    # And a specialty we do not offer is dropped rather than stored.
    r = _apply(client, scale_specialties=["Nephrology", "Astrology"])
    assert r.status_code == 200
    store = _store()
    hs = store.list_health_systems()[0]
    assert store.latest_hs_application(hs["hs_id"])["scale_specialties"] == ["Nephrology"]


def test_resubmitting_appends_and_never_overwrites():
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    _apply(client, authority="not_sure")
    _apply(client, authority="yes")
    rows = store.list_hs_applications(org["hs_id"])
    assert len(rows) == 2
    assert {r["authority"] for r in rows} == {"yes", "not_sure"}


def test_members_are_provisioned_emailed_and_can_sign_in(mail):
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    r = client.post(f"{API}/hs/members", json={
        "emails": ["k.patel@example.org", org["email"], "not-an-address"]})
    assert r.status_code == 200, r.text
    # The colleague was added; the caller's own address and the junk were not.
    assert r.json()["added"] == ["k.patel@example.org"]
    assert len(r.json()["members"]) == 2

    invite = [m for m in mail if "added you to" in m["subject"]]
    assert len(invite) == 1
    assert invite[0]["to"] == "k.patel@example.org"
    assert "Dana Reyes" in invite[0]["subject"]
    # The credential is NOT echoed to the colleague who added them.
    assert "passphrase" not in r.text and "temp_password" not in r.text

    # The member's account exists on the same organization and must rotate.
    members = [u for u in store.list_hs_portal_users(org["hs_id"])
               if (u.get("email") or "") == "k.patel@example.org"]
    assert len(members) == 1
    assert members[0]["must_reset"] == 1
    assert members[0]["invited_by"] == org["username"]


def test_a_member_cannot_be_added_to_another_organization():
    """The route takes addresses and nothing else, so there is no version of it
    that names a health system. This asserts the property rather than a check."""
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    other = store.create_health_system_unclaimed("Somebody Else Health")
    client.post(f"{API}/hs/members", json={"emails": ["x@example.org"],
                                           "hs_id": other["hs_id"]})
    assert store.list_hs_portal_users(other["hs_id"]) == []


# ════════════════════════════════════════════════════════════════════════════
#  §6 — the upload gate, at every door, in every state
# ════════════════════════════════════════════════════════════════════════════
def _bundle() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.json", '{"note": "hello"}')
    return buf.getvalue()


def _open_session(client, data: bytes) -> str:
    """Declare a real chunked session. Only possible while the door is open, so
    callers that want to test a CLOSED door open one first and close it after."""
    r = client.post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": "application/zip"})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _try_every_upload_door(client, session_id: str):
    """The four doors, each returning its status code.

    A REAL session id, not a made-up one: ``part`` and ``complete`` resolve the
    session before they check anything else, so a fabricated id returns 404 and
    the test would pass without the gate existing. The interesting case is an
    in-flight upload whose organization loses the door underneath it, and this
    is the only way to reach it.
    """
    data = _bundle()
    sha = hashlib.sha256(data).hexdigest()
    multipart = client.post(f"{API}/hs/uploads",
                            files={"files": ("b.zip", data, "application/zip")})
    declare = client.post(f"{API}/hs/uploads/sessions", json={
        "filename": "c.zip", "size": len(data), "sha256": sha,
        "content_type": "application/zip"})
    part = client.put(f"{API}/hs/uploads/sessions/{session_id}/parts/1", content=data)
    complete = client.post(f"{API}/hs/uploads/sessions/{session_id}/complete")
    return [multipart.status_code, declare.status_code,
            part.status_code, complete.status_code]


@pytest.mark.parametrize("state", ["intake", "submitted", "approved_awaiting_dla"])
def test_uploads_are_refused_in_every_state_except_active(state):
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    # Approve the ACCOUNT so the only thing standing between them and an upload
    # is the organization's state. Without this the account gate would refuse
    # first and the test would prove nothing about the state gate.
    store.set_hs_approval(org["username"], "approved", by="test")
    # Open a real session while the door is open, then close it. An upload
    # already in flight must stop, not finish.
    store.set_hs_onboarding_state(org["hs_id"], "active")
    session_id = _open_session(client, _bundle())
    store.set_hs_onboarding_state(org["hs_id"], state)
    assert _try_every_upload_door(client, session_id) == [403, 403, 403, 403]


def test_the_state_gate_is_server_side_and_survives_an_approved_account():
    """The two gates are separate objects. An approved ACCOUNT on an unsigned
    ORGANIZATION must still be refused, which is the case a UI-side check misses
    because the rail would happily show the upload tab."""
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    store.set_hs_approval(org["username"], "approved", by="test")
    store.set_hs_onboarding_state(org["hs_id"], "active")
    session_id = _open_session(client, _bundle())
    store.set_hs_onboarding_state(org["hs_id"], "approved_awaiting_dla")
    me = client.get(f"{API}/hs/me").json()
    assert "upload" in me["surfaces"]          # the account may
    assert me["state"] == "approved_awaiting_dla"
    assert _try_every_upload_door(client, session_id) == [403, 403, 403, 403]


def test_a_health_system_that_predates_the_state_machine_keeps_uploading():
    """The zero-backfill promise. A NULL state is an organization provisioned
    before any of this existed; a deploy must not lock it out of a door it has
    been using."""
    client = _client()
    store = _store()
    uname = "legacy" + uuid.uuid4().hex[:6]
    hs = store.ensure_health_system("Legacy General")
    store.create_hs_portal_user(username=uname, hs_id=hs["hs_id"],
                                password=PASSWORD, email="it@legacy.org",
                                must_reset=False)
    assert store.get_health_system(hs["hs_id"])["onboarding_state"] is None
    r = client.post(f"{API}/hs/login", json={"username": uname, "password": PASSWORD})
    assert r.status_code == 200
    assert client.get(f"{API}/hs/me").json()["state"] == "active"
    r = client.post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": 64, "sha256": "a" * 64})
    assert r.status_code == 200, r.text


# ════════════════════════════════════════════════════════════════════════════
#  §4 — approve and decline
# ════════════════════════════════════════════════════════════════════════════
def test_approve_flips_the_state_and_mails_every_member(mail):
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    client.post(f"{API}/hs/members", json={"emails": ["k.patel@example.org"]})
    _apply(client)
    mail.clear()

    r = _approve(client, store, org["hs_id"])
    assert r.status_code == 200, r.text
    assert r.json()["onboarding_state"] == "approved_awaiting_dla"
    assert store.get_health_system(org["hs_id"])["onboarding_state"] == "approved_awaiting_dla"

    dla = [m for m in mail if "One signature away" in m["subject"]]
    assert sorted(m["to"] for m in dla) == sorted([org["email"], "k.patel@example.org"])
    assert r.json()["emailed"] == 2
    # Every account is now full, so the only remaining gate is the signature.
    for account in store.list_hs_portal_users(org["hs_id"]):
        assert account["approval_status"] == "approved"


def test_approve_leaves_the_upload_destination_unset_by_default():
    """§6: accounts are minted with it unset so the admin resolves each upload
    deliberately, on the per-upload control that already exists."""
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    _apply(client)
    _approve(client, store, org["hs_id"])
    assert store.hs_purposes_for(org["hs_id"]) == [None]
    listing = client.get(f"{API}/admin/health-systems",
                         headers=_admin_headers(store)).json()
    row = [h for h in listing["health_systems"] if h["hs_id"] == org["hs_id"]][0]
    assert row["purpose_unresolved"] == 1


def test_approve_can_still_set_a_destination_when_the_operator_knows_it():
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    _apply(client)
    r = _approve(client, store, org["hs_id"], purpose="task_creation")
    assert r.status_code == 200, r.text
    assert store.hs_purposes_for(org["hs_id"]) == ["task_creation"]
    assert _approve(client, store, org["hs_id"], purpose="nonsense").status_code in (400, 409)


def test_approving_twice_is_refused_rather_than_re_mailing_everyone(mail):
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    _apply(client)
    assert _approve(client, store, org["hs_id"]).status_code == 200
    mail.clear()
    r = _approve(client, store, org["hs_id"])
    assert r.status_code == 409
    assert mail == []


def test_decline_requires_a_note_and_closes_every_account(mail):
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    client.post(f"{API}/hs/members", json={"emails": ["k.patel@example.org"]})
    _apply(client)
    mail.clear()

    r = client.post(f"{API}/admin/health-systems/{org['hs_id']}/decline",
                    json={"reason": "   "}, headers=_admin_headers(store))
    assert r.status_code == 400

    r = client.post(f"{API}/admin/health-systems/{org['hs_id']}/decline",
                    json={"reason": "No authority to license."},
                    headers=_admin_headers(store))
    assert r.status_code == 200, r.text
    assert store.get_health_system(org["hs_id"])["onboarding_state"] == "declined"
    for account in store.list_hs_portal_users(org["hs_id"]):
        assert account["active"] == 0
        assert account["decision_reason"] == "No authority to license."
    # Deliberately silent: a refusal at this deal size is a conversation.
    assert mail == []


# ════════════════════════════════════════════════════════════════════════════
#  §5 — the e-signed agreement
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def approved(mail):
    """An organization sitting on `approved_awaiting_dla`, signed in."""
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    client.post(f"{API}/hs/members", json={"emails": ["k.patel@example.org"]})
    _apply(client)
    _approve(client, store, org["hs_id"])
    mail.clear()
    return client, store, org


def test_the_agreement_is_readable_in_full_without_downloading_anything(approved):
    client, _store_, org = approved
    body = client.get(f"{API}/hs/agreement").json()
    assert body["can_sign"] is True
    assert body["doc_version"] == "v1"
    # Full text, not a link. The organization's own name is in it, and the
    # clauses §5.2 requires are all present.
    assert org["organization"] in body["text"]
    assert len(body["text"]) > 8000
    for clause in ("De-identified Data", "164.514", "Derived Works",
                   "Brokering", "Delaware", "15 U.S.C.", "Schedule A"):
        assert clause in body["text"], clause


def test_signing_needs_both_boxes_and_a_name_and_a_title(approved):
    client, store, org = approved
    sha = client.get(f"{API}/hs/agreement").json()["doc_sha256"]
    assert _sign(client, authority=False, sha=sha).status_code == 400
    assert _sign(client, esign=False, sha=sha).status_code == 400
    assert _sign(client, name="  ", sha=sha).status_code == 400
    assert _sign(client, title="", sha=sha).status_code == 400
    assert store.latest_signed_agreement(org["hs_id"]) is None
    assert _sign(client, sha=sha).status_code == 200


def test_the_signature_records_what_was_signed_and_opens_the_door(approved, mail):
    client, store, org = approved
    shown = client.get(f"{API}/hs/agreement").json()
    assert _sign(client, sha=shown["doc_sha256"]).status_code == 200

    row = store.latest_signed_agreement(org["hs_id"])
    assert row["typed_name"] == "Dana Reyes"
    assert row["typed_title"] == "Chief Information Officer"
    assert row["consent_esign"] == 1 and row["authority_affirmed"] == 1
    assert row["signer_user_id"] == org["username"]
    assert row["ip"] and row["signed_at"]
    # The hash is of the exact text that was on their screen.
    assert row["doc_sha256"] == shown["doc_sha256"]
    from asclepius import dla as asc_dla
    _text, sha = asc_dla.signable(organization=org["organization"])
    assert row["doc_sha256"] == sha

    # The PDF is in the asset store, addressed by its own hash.
    from asclepius import assets as asc_assets
    data, _mime = asc_assets.load_asset(row["pdf_sha256"], verify=True)
    assert data.startswith(b"%PDF-")
    assert hashlib.sha256(data).hexdigest() == row["pdf_sha256"]

    # And the door is open.
    assert store.get_health_system(org["hs_id"])["onboarding_state"] == "active"
    r = client.post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": 64, "sha256": "b" * 64})
    assert r.status_code == 200, r.text


def test_the_signed_copy_is_emailed_to_the_signer_and_the_team(approved, mail):
    client, _store_, org = approved
    _sign(client, sha=client.get(f"{API}/hs/agreement").json()["doc_sha256"])

    receipts = [m for m in mail if m["subject"].startswith("Signed:")]
    assert [m["to"] for m in receipts] == [org["email"]]
    # E-SIGN retention: the copy is ATTACHED, not linked.
    name, mime, blob = receipts[0]["attachments"][0]
    assert name.endswith(".pdf") and mime == "application/pdf"
    assert blob.startswith(b"%PDF-")

    opened = [m for m in mail if "Uploads are open" in m["subject"]]
    assert sorted(m["to"] for m in opened) == sorted([org["email"],
                                                      "k.patel@example.org"])
    assert "Dana Reyes" in opened[0]["body"]


def test_a_colleague_who_arrives_after_the_signature_sees_who_signed(approved):
    client, store, org = approved
    _sign(client, sha=client.get(f"{API}/hs/agreement").json()["doc_sha256"])

    # The member signs in for the first time, on the same organization.
    member = [u for u in store.list_hs_portal_users(org["hs_id"])
              if (u.get("email") or "") == "k.patel@example.org"][0]
    store.set_hs_portal_password(member["username"], PASSWORD, must_reset=False)
    colleague = _client()
    r = colleague.post(f"{API}/hs/login",
                       json={"username": member["username"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    me = colleague.get(f"{API}/hs/me").json()
    assert me["state"] == "active"
    assert me["agreement"]["signed_by"] == "Dana Reyes"
    # And they are not asked to sign it again.
    body = colleague.get(f"{API}/hs/agreement").json()
    assert body["can_sign"] is False
    assert body["signed"]["signed_by"] == "Dana Reyes"
    assert _sign(colleague, name="Kiran Patel").status_code == 409


def test_the_signature_row_can_never_be_updated_or_deleted(approved):
    """Append-only, enforced by the database rather than by everyone
    remembering. A trigger holds for a migration script and a 2am console
    session too."""
    import sqlite3

    client, store, org = approved
    _sign(client, sha=client.get(f"{API}/hs/agreement").json()["doc_sha256"])
    row = store.latest_signed_agreement(org["hs_id"])

    with pytest.raises(sqlite3.Error):
        with store._conn() as conn:
            conn.execute("UPDATE signed_agreements SET typed_name = 'Someone Else'")
    with pytest.raises(sqlite3.Error):
        with store._conn() as conn:
            conn.execute("DELETE FROM signed_agreements")
    assert store.latest_signed_agreement(org["hs_id"])["typed_name"] == row["typed_name"]


def test_a_second_version_is_a_new_row_and_leaves_the_first_untouched(approved):
    """§5.3: 'a re-signed newer version is a new row'. The trigger permits
    INSERT, which is what makes that possible at all."""
    client, store, org = approved
    _sign(client, sha=client.get(f"{API}/hs/agreement").json()["doc_sha256"])
    first = store.latest_signed_agreement(org["hs_id"])

    second = store.record_signed_agreement(
        hs_id=org["hs_id"], doc_version="v2", doc_sha256="f" * 64,
        signer_user_id=org["username"], typed_name="Dana Reyes",
        typed_title="Chief Information Officer", consent_esign=True,
        authority_affirmed=True)
    rows = store.list_signed_agreements(org["hs_id"])
    assert len(rows) == 2
    assert {r["doc_version"] for r in rows} == {"v1", "v2"}
    kept = store.get_signed_agreement(first["agreement_id"])
    assert kept == first
    assert second["agreement_id"] != first["agreement_id"]


def test_a_document_that_changed_under_the_signer_is_refused(approved):
    client, _store_, _org = approved
    assert _sign(client, sha="0" * 64).status_code == 409


def test_the_signed_pdf_is_downloadable_by_both_parties(approved):
    client, store, org = approved
    _sign(client, sha=client.get(f"{API}/hs/agreement").json()["doc_sha256"])

    # The partner's own copy, scoped to their session and taking no identifier.
    r = client.get(f"{API}/hs/agreement/document")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")

    # And ours, by agreement id.
    row = store.latest_signed_agreement(org["hs_id"])
    r = client.get(f"{API}/admin/agreements/{row['agreement_id']}/document",
                   headers=_admin_headers(store))
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")


def test_the_admin_card_carries_the_whole_signature_record(approved):
    client, store, org = approved
    _sign(client, sha=client.get(f"{API}/hs/agreement").json()["doc_sha256"])
    detail = client.get(f"{API}/admin/health-systems/{org['hs_id']}",
                        headers=_admin_headers(store)).json()
    record = detail["agreements"][0]
    assert record["typed_name"] == "Dana Reyes"
    assert record["ip"] and record["user_agent"]
    assert record["consent_esign"] is True
    assert record["download_url"].endswith("/document")
    assert detail["onboarding_state"] == "active"

    listing = client.get(f"{API}/admin/health-systems",
                         headers=_admin_headers(store)).json()
    row = [h for h in listing["health_systems"] if h["hs_id"] == org["hs_id"]][0]
    assert row["agreement"]["doc_version"] == "v1"
    assert row["agreement"]["signed_by"] == "Dana Reyes"


def test_the_portal_never_shows_a_colleague_where_the_signer_signed_from(approved):
    """The network address and client string are on the row because a court may
    want them. A colleague reading the portal has no business with either."""
    client, _store_, _org = approved
    _sign(client, sha=client.get(f"{API}/hs/agreement").json()["doc_sha256"])
    body = client.get(f"{API}/hs/me").text
    assert "testclient" not in body.lower()
    assert '"ip"' not in body


# ════════════════════════════════════════════════════════════════════════════
#  §7 — invoices: the shape money will move in, and no Stripe
# ════════════════════════════════════════════════════════════════════════════
def test_invoices_are_recorded_and_never_double_billed():
    client = _client()
    store = _store()
    org = _signup(client)
    headers = _admin_headers(store)
    r = client.post(f"{API}/admin/health-systems/{org['hs_id']}/invoices",
                    json={"period": "2026-Q1", "amount_cents": 125000,
                          "description": "Nephrology extract, Q1"}, headers=headers)
    assert r.status_code == 200, r.text
    invoice = r.json()["invoice"]
    assert invoice["status"] == "draft" and invoice["stripe_invoice_id"] is None

    dup = client.post(f"{API}/admin/health-systems/{org['hs_id']}/invoices",
                      json={"period": "2026-Q1", "amount_cents": 1}, headers=headers)
    assert dup.status_code == 409

    r = client.post(
        f"{API}/admin/health-systems/{org['hs_id']}/invoices/{invoice['invoice_id']}/status",
        json={"status": "paid"}, headers=headers)
    assert r.status_code == 200 and r.json()["invoice"]["paid_at"]


def test_nothing_in_this_release_calls_stripe():
    """The disbursement seam, asserted rather than remembered."""
    source = (Path(__file__).resolve().parent.parent / "routers"
              / "asclepius_admin.py").read_text(encoding="utf-8")
    invoices = source[source.index("class HsInvoiceRequest"):]
    for word in ("stripe.", "import stripe", "STRIPE_"):
        assert word not in invoices, f"invoice code reaches for {word}"


def test_the_invoice_table_holds_no_bank_details():
    """Same rule hs_payouts follows: a change that wants a routing number is the
    signal it belongs behind a payment processor instead."""
    store = _store()
    with store._conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(hs_invoices)")}
    for forbidden in ("bank_account", "routing_number", "iban", "tax_id",
                      "ssn", "ein"):
        assert forbidden not in cols


# ════════════════════════════════════════════════════════════════════════════
#  Two properties the flow rests on, asserted directly
# ════════════════════════════════════════════════════════════════════════════
def test_a_lost_pdf_blob_does_not_lose_the_contract(approved):
    """The ROW is the record. Everything the document prints lives on it, so an
    asset store that loses a blob is an incident rather than the loss of a
    contract — and the rebuild has to be byte-identical, or it is a different
    document wearing the same name."""
    from asclepius import assets as asc_assets
    from asclepius import dla as asc_dla

    client, store, org = approved
    _sign(client, sha=client.get(f"{API}/hs/agreement").json()["doc_sha256"])
    row = store.latest_signed_agreement(org["hs_id"])
    original, _mime = asc_assets.load_asset(row["pdf_sha256"])

    rebuilt = asc_dla.pdf_from_row(organization=org["organization"], row=row)
    assert hashlib.sha256(rebuilt).hexdigest() == row["pdf_sha256"]
    assert rebuilt == original

    # And the download survives the blob going away.
    Path(asc_assets._blob_path(row["pdf_sha256"])).unlink()
    r = client.get(f"{API}/hs/agreement/document")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF-")
    r = client.get(f"{API}/admin/agreements/{row['agreement_id']}/document",
                   headers=_admin_headers(store))
    assert r.status_code == 200
    # And it says so, rather than passing a rebuild off as the stored artifact.
    assert r.headers.get("x-agreement-source") == "rebuilt-from-row"


def test_an_operator_may_approve_an_organization_that_never_filled_the_form_in():
    """A partner we already met on a call signs up; the person who had that call
    approves them without making them answer four questions they answered out
    loud. The PORTAL cannot take this edge — it only ever submits."""
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    assert store.get_health_system(org["hs_id"])["onboarding_state"] == "intake"
    r = _approve(client, store, org["hs_id"])
    assert r.status_code == 200, r.text
    assert store.get_health_system(org["hs_id"])["onboarding_state"] == "approved_awaiting_dla"


def test_the_two_copies_of_the_answer_wording_stay_in_step():
    """The partner-facing question list lives in the provider router and the
    operator-facing labels live in the admin router, duplicated deliberately:
    one module is provider-reachable and one is not, and importing across that
    boundary to save eight lines would be the first crack in the separation the
    isolation suite rests on. This is the cost of that decision, paid here."""
    from routers.asclepius_admin import _HS_ANSWER_WORDS
    from routers.asclepius_provider import _HS_ANSWER_LABELS

    for key, options in _HS_ANSWER_WORDS.items():
        for value, words in options.items():
            assert _HS_ANSWER_LABELS.get(f"{key}:{value}") == words, (key, value)
    # And nothing the partner can choose is missing from the operator's copy.
    for compound, label in _HS_ANSWER_LABELS.items():
        key, _, value = compound.partition(":")
        if key == "scale_specialties":
            continue          # free list, rendered as-is on both sides
        assert _HS_ANSWER_WORDS.get(key, {}).get(value) == label, compound


def test_the_agreement_ships_with_the_application():
    """The document is SOURCE, read at request time, not documentation about the
    feature. Two files decide whether it reaches a container, and both of them
    are easy to change without thinking about this: the Dockerfile copies
    `backend/` and `frontend/` and would otherwise leave it behind, and
    `.dockerignore` excludes `*.md` wholesale. Get either wrong and every health
    system's agreement page 503s, no signature is ever taken, and no upload door
    opens — on a deploy, with nothing failing in CI to say so.
    """
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY docs/legal/" in dockerfile

    ignore = (root / ".dockerignore").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in ignore.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert "!docs/legal/*.md" in lines
    # Last match wins, so the exception has to come AFTER the blanket rule.
    assert lines.index("!docs/legal/*.md") > lines.index("*.md")


def test_the_current_agreement_version_exists_and_renders():
    """A CURRENT_VERSION pointing at a file nobody added is a 503 on the one
    page this whole feature exists to show."""
    from asclepius import dla as asc_dla

    assert asc_dla.CURRENT_VERSION in asc_dla.available_versions()
    text, sha = asc_dla.signable(organization="Any Health")
    assert len(sha) == 64
    assert "{{" not in text, "an unsubstituted placeholder reached the signer"
    assert "<!--" not in text, "an editorial comment reached the signer"


# ════════════════════════════════════════════════════════════════════════════
#  §7 — what the partner sees of money
# ════════════════════════════════════════════════════════════════════════════
def test_the_payouts_tab_says_what_it_is_before_anything_is_in_it():
    """The empty state has to promise the right thing. A ledger that fills
    itself is exactly what it must not imply — nothing accrues automatically."""
    client = _client()
    org = _signup(client)
    _rotate(client)
    body = client.get(f"{API}/hs/payouts").json()
    assert body["payouts"] == [] and body["invoices"] == []
    assert body["empty_note"] == (
        "Compensation for licensed data appears here. Invoicing goes live "
        "shortly; your agreement's Schedule A governs amounts.")


def test_a_draft_invoice_is_never_shown_to_the_partner():
    """A draft is a number an operator is still deciding about. Showing a
    hospital's finance contact an amount we have not committed to is a
    conversation nobody wants to have twice."""
    client = _client()
    store = _store()
    org = _signup(client)
    _rotate(client)
    headers = _admin_headers(store)
    r = client.post(f"{API}/admin/health-systems/{org['hs_id']}/invoices",
                    json={"period": "2026-Q1", "amount_cents": 125000,
                          "description": "Nephrology extract"}, headers=headers)
    invoice_id = r.json()["invoice"]["invoice_id"]
    assert client.get(f"{API}/hs/payouts").json()["invoices"] == []

    client.post(f"{API}/admin/health-systems/{org['hs_id']}/invoices/{invoice_id}/status",
                json={"status": "sent"}, headers=headers)
    shown = client.get(f"{API}/hs/payouts").json()["invoices"]
    assert len(shown) == 1
    assert shown[0]["period"] == "2026-Q1"
    assert shown[0]["status"] == "issued"      # partner words, not ours
    assert shown[0]["amount_cents"] == 125000
    # Ours stays ours: no internal id, no author, no processor reference.
    for ours in ("invoice_id", "created_by", "stripe_invoice_id", "hs_id"):
        assert ours not in shown[0]


def test_the_agreement_reads_as_a_document_not_as_markdown():
    """The file is markdown so a lawyer can edit it. What a hospital's CIO sees
    must not be. A contract on screen with visible ``##`` and ``**`` reads as an
    unfinished draft, and the reasonable-notice question a court asks about a
    clickwrap is about the document AS PRESENTED.

    One canonical rendering, used for display, for the hash, and for the PDF's
    words — not three nearly-identical strings that could drift.
    """
    from asclepius import dla as asc_dla

    text, sha = asc_dla.signable(organization="St Mary's Health")
    assert "*" not in text, "emphasis markers reached the signer"
    for line in text.splitlines():
        assert not line.lstrip().startswith("#"), f"a heading marker reached the signer: {line!r}"
    # The words survive the normalization — only the markers go.
    assert "Both purposes are granted" in text
    assert "at Archangel's sole discretion" in text
    assert sha == asc_dla.sha256_of(text)

    # And the PDF prints the same document, headings and all.
    from PyPDF2 import PdfReader

    pdf = asc_dla.render_pdf(
        organization="St Mary's Health", version=asc_dla.CURRENT_VERSION,
        signature={"typed_name": "Dana Reyes", "typed_title": "CIO",
                   "signed_at": "2026-03-14T16:20:05", "signer_user_id": "stmarys",
                   "signer_email": "d@x.org", "ip": "1.2.3.4", "user_agent": "ua",
                   "doc_version": asc_dla.CURRENT_VERSION, "doc_sha256": sha})
    rendered = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "**" not in rendered and "##" not in rendered
    assert "Both purposes are granted" in rendered
    # The hash of what was signed is printed in it, in two halves.
    assert sha[:32] in rendered and sha[32:] in rendered
