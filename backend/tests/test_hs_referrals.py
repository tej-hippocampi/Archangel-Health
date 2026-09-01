"""Health-system referrals (HS-REF): capture, guards, delivery, funnel, prefill.

``test_referral_program`` and ``test_referral_payout`` own the PHYSICIAN spine , 
the bounty, the cap, link attribution. This file owns the institutional path
that sits beside it in the same tab, and the invariant that keeps the two apart:

  * the capture endpoint's guards (consent, self-referral, per-contact cap);
  * delivery, enrichment gating which body is sent, and a block stopping a send;
  * the funnel's display contract, which carries NO money field at all;
  * the public prefill endpoint, its whitelist, and its non-oracle 200;
  * that nothing on this path can ever reach ``earnings``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import referrals as asc_referrals  # noqa: E402
from asclepius.store import get_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    import ratelimit
    ratelimit.reset()
    yield


def _store():
    return get_store()


def _physician(full_name=None, **kw):
    """An approved physician who can refer.

    ``full_name`` is set by UPDATE rather than passed to ``create_user``, which
    does not accept it, and it matters here in a way it does not for most
    fixtures: the referrer's name IS the mechanism this email runs on, so a
    nameless fixture silently exercises the neutral fallback copy instead of the
    named one.
    """
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology", **kw)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (u["id"],))
        if full_name:
            conn.execute("UPDATE users SET full_name = ? WHERE id = ?",
                         (full_name, u["id"]))
    return store.get_user_by_id(u["id"])


def _payload(**over):
    body = {
        "contact_name": "James Okoye",
        "contact_email": f"coo-{A.uniq(8)}@meridianhealth.org",
        "hs_name": "Meridian Health",
        "relationship": "we were at college together",
        "contact_role": "Chief Operating Officer",
        "note": "They run four hospitals in the region.",
        "consent": True,
    }
    body.update(over)
    return body


def _submit(referrer, **over):
    return client.post("/api/asclepius/referrals/health-system",
                       json=_payload(**over), headers=A.headers_for(referrer))


def _submit_only(referrer, **over):
    """Capture WITHOUT delivering.

    ``TestClient`` runs FastAPI background tasks synchronously once the response
    is returned, so a plain ``_submit`` has already run the real delivery leg , 
    including a real send, by the time the call returns. Every delivery test
    below wants to drive that leg itself with a stubbed enrichment, so the
    scheduled task is neutered for the duration of the capture. Without this the
    tests measure the wrong send and a blocked-contact assertion passes against
    an email that was already out the door.
    """
    from routers import asclepius_payments as R

    async def noop(*a, **kw):
        return None
    orig = R._deliver_hs_referral
    R._deliver_hs_referral = noop
    try:
        return _submit(referrer, **over)
    finally:
        R._deliver_hs_referral = orig


# ═══ Capture ══════════════════════════════════════════════════════════════════
def test_a_named_contact_is_recorded_with_a_landing_token():
    referrer = _physician()
    r = _submit(referrer)
    assert r.status_code == 200, r.text

    rows = _store().list_hs_referrals_by_referrer(referrer["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["hs_name"] == "Meridian Health"
    assert row["contact_email"].islower()
    assert row["landing_token"]
    assert row["referral_code"] == referrer.get("referral_code") or True


def test_the_consent_checkbox_is_required():
    """We send this email in the physician's name. Without the assertion that
    they know the recipient, the claim the email makes is one nobody made."""
    referrer = _physician()
    r = _submit(referrer, consent=False)
    assert r.status_code == 422
    assert not _store().list_hs_referrals_by_referrer(referrer["id"])


def test_referring_yourself_is_refused():
    referrer = _physician()
    r = _submit(referrer, contact_email=referrer["email"].upper())
    assert r.status_code == 422


def test_the_same_contact_cannot_be_introduced_without_bound(monkeypatch):
    """Keyed on the CONTACT, not the referrer: otherwise one inbox is mailed
    without bound by rotating who submits it."""
    import ratelimit
    monkeypatch.setattr(ratelimit, "is_enabled", lambda: True)
    target = "coo@meridianhealth.org"
    for i in range(2):
        assert _submit(_physician(), contact_email=target).status_code == 200, i
    r = _submit(_physician(), contact_email=target)
    assert r.status_code == 429


def test_missing_required_fields_are_refused():
    referrer = _physician()
    for field in ("contact_name", "hs_name", "relationship"):
        assert _submit(referrer, **{field: "   "}).status_code == 422, field
    assert _submit(referrer, contact_email="not-an-email").status_code == 422


def test_a_signed_out_caller_cannot_introduce_anyone():
    assert client.post("/api/asclepius/referrals/health-system",
                       json=_payload()).status_code in (401, 403)


# ═══ Delivery ═════════════════════════════════════════════════════════════════
def _deliver(referrer, row_id, enrichment):
    """Run the background leg directly with a stubbed enrichment result."""
    from asclepius import hs_enrich
    from routers import asclepius_payments as R

    async def fake(**kw):
        return enrichment
    orig = hs_enrich.enrich_health_system
    hs_enrich.enrich_health_system = fake
    try:
        asyncio.run(R._deliver_hs_referral(row_id, referrer["id"]))
    finally:
        hs_enrich.enrich_health_system = orig


_GOOD = {"state": "ok", "reason": None, "data": {
    "role_confirmed": True, "org_confirmed": True, "org_type": "regional system",
    "size_bucket": "regional_system",
    "one_public_fact": "Meridian announced an ambient-scribe rollout in July.",
    "source_url": "https://example.org/news", "seen_date": "2026-07-14",
    "confidence": "high", "do_not_contact": False, "do_not_contact_reason": ""}}


def test_a_confident_finding_is_cited_and_the_send_is_stamped(capsys):
    referrer = _physician(full_name="Priya Patel")
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]

    _deliver(referrer, row["hs_referral_id"], _GOOD)
    out = capsys.readouterr().out

    assert "ambient-scribe" in out
    assert "Priya Patel suggested I reach out" in out
    after = _store().get_hs_referral(row["hs_referral_id"])
    assert after["email_sent_at"], "a delivered introduction must be stamped"
    assert after["status"] == "sent"
    assert after["enrich_state"] == "ok"


@pytest.mark.parametrize("enrichment,label", [
    ({"state": "skipped", "data": None, "reason": "no_api_key"}, "no enrichment at all"),
    ({"state": "ok", "reason": None, "data": dict(_GOOD["data"], confidence="low")}, "low confidence"),
    ({"state": "ok", "reason": None, "data": dict(_GOOD["data"], source_url="")}, "fact without a source"),
])
def test_weak_research_sends_the_clean_body_not_a_shaky_one(capsys, enrichment, label):
    """The gate: an email with one fewer sentence, never one wrong sentence."""
    referrer = _physician(full_name="Priya Patel")
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]

    _deliver(referrer, row["hs_referral_id"], enrichment)
    out = capsys.readouterr().out

    assert "ambient-scribe" not in out, label
    assert "Priya Patel suggested I reach out" in out, label
    assert _store().get_hs_referral(row["hs_referral_id"])["email_sent_at"], label


def test_a_blocked_contact_is_never_emailed(capsys):
    referrer = _physician(full_name="Priya Patel")
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]

    _deliver(referrer, row["hs_referral_id"], {
        "state": "blocked", "reason": "Direct competitor.",
        "data": dict(_GOOD["data"], do_not_contact=True,
                     do_not_contact_reason="Direct competitor.")})
    out = capsys.readouterr().out

    after = _store().get_hs_referral(row["hs_referral_id"])
    assert after["email_sent_at"] is None, "a blocked contact must not be emailed"
    assert after["status"] is None
    assert after["fraud_flag"] == "Direct competitor."
    # The founder still hears about it, a block is the case a human most needs.
    assert "aryaabhatia@berkeley.edu" in out
    assert "Direct competitor." in out
    assert "nothing (blocked)" in out


def test_an_existing_partner_is_not_cold_emailed(capsys):
    """A physician introducing someone we already work with means well. Sending
    that person "let us introduce ourselves" does not, so nothing goes out and a
    founder picks the thread up instead.

    Checked at DELIVERY, not at capture: refusing the submission would tell the
    referrer which organizations we already work with, which is the oracle the
    physician path was rewritten to close."""
    referrer = _physician(full_name="Priya Patel")
    store = _store()
    store.ensure_health_system("Meridian Health", contact_email="j.okoye@meridianhealth.org")

    r = _submit_only(referrer, contact_email="j.okoye@meridianhealth.org")
    # The capture response is byte-identical to the ordinary one.
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "message": "Introduction recorded. We'll reach out and you'll see it below.",
    }

    row = store.list_hs_referrals_by_referrer(referrer["id"])[0]
    _deliver(referrer, row["hs_referral_id"], _GOOD)
    out = capsys.readouterr().out

    after = store.get_hs_referral(row["hs_referral_id"])
    assert after["email_sent_at"] is None, "an existing partner must not be cold-emailed"
    assert after["fraud_flag"] == "already_a_partner"
    assert "already a partner" in out
    assert "aryaabhatia@berkeley.edu" in out


def test_the_kill_switch_stops_the_send_and_keeps_the_lead(capsys, monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_HS_REFERRAL_SEND_ENABLED", "0")
    referrer = _physician(full_name="Priya Patel")
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]

    _deliver(referrer, row["hs_referral_id"], _GOOD)
    out = capsys.readouterr().out

    assert _store().get_hs_referral(row["hs_referral_id"])["email_sent_at"] is None
    assert "sending disabled" in out
    assert "aryaabhatia@berkeley.edu" in out, "the lead must still reach a founder"


def test_enrichment_blowing_up_still_sends_the_clean_introduction(capsys):
    referrer = _physician(full_name="Priya Patel")
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]

    from asclepius import hs_enrich
    from routers import asclepius_payments as R

    async def boom(**kw):
        raise RuntimeError("model unavailable")
    orig = hs_enrich.enrich_health_system
    hs_enrich.enrich_health_system = boom
    try:
        asyncio.run(R._deliver_hs_referral(row["hs_referral_id"], referrer["id"]))
    finally:
        hs_enrich.enrich_health_system = orig

    assert "Priya Patel suggested I reach out" in capsys.readouterr().out
    assert _store().get_hs_referral(row["hs_referral_id"])["email_sent_at"]


# ═══ The funnel ═══════════════════════════════════════════════════════════════
def test_the_funnel_row_carries_a_sentence_and_no_money():
    referrer = _physician()
    assert _submit_only(referrer).status_code == 200

    r = client.get("/api/asclepius/referrals", headers=A.headers_for(referrer))
    assert r.status_code == 200, r.text
    items = r.json()["health_systems"]
    assert len(items) == 1
    item = items[0]

    assert item["hs_name"] == "Meridian Health"
    assert item["status_sentence"]
    # The rule REFERRALS.md records: no figure is printed for an institutional
    # introduction. Not zero, not pending, the keys do not exist.
    for banned in ("bounty_cents", "amount_cents", "reward_cents", "cents", "amount"):
        assert banned not in item, banned
    # And the bearer token / our research never reach the browser.
    assert "landing_token" not in item
    assert "enrich_json" not in item
    assert "contact_email" not in item


def test_a_physician_only_sees_their_own_introductions():
    mine, theirs = _physician(), _physician()
    assert _submit(theirs).status_code == 200
    r = client.get("/api/asclepius/referrals", headers=A.headers_for(mine))
    assert r.json()["health_systems"] == []


def test_status_only_moves_forward():
    """A recipient who books a call and then re-opens the emailed link must not
    walk their own status back to 'opened' in the referrer's funnel."""
    referrer = _physician()
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]
    rid = row["hs_referral_id"]

    _store().advance_hs_referral(rid, "booked")
    _store().advance_hs_referral(rid, "opened")
    assert _store().get_hs_referral(rid)["status"] == "booked"


# ═══ The public prefill ═══════════════════════════════════════════════════════
def test_the_prefill_returns_only_what_we_already_emailed_them():
    referrer = _physician(full_name="Priya Patel")
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]

    r = client.get(f"/api/asclepius/hs-referral/{row['landing_token']}")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["found"] is True
    assert body["hs_name"] == "Meridian Health"
    assert body["contact_name"] == "James Okoye"
    assert body["referrer_first_name"] == "Priya"
    # Never: the referrer's address, their note, or our research.
    assert referrer["email"] not in r.text
    assert "note" not in body
    assert "enrich_json" not in body
    assert "landing_token" not in body
    # Opening the link moves the physician's funnel row on its own.
    assert _store().get_hs_referral(row["hs_referral_id"])["status"] == "opened"


def test_an_unknown_token_is_a_200_not_a_404():
    """A 404 would make this a membership oracle: feed it tokens and the status
    code says which are live. The page renders an empty form either way."""
    r = client.get("/api/asclepius/hs-referral/definitely-not-a-real-token")
    assert r.status_code == 200
    assert r.json() == {"found": False}


# ═══ Admin: the last two stages, and the money ════════════════════════════════
def _admin():
    return A.make_user(_store(), role="admin")


def test_an_admin_records_the_stages_that_cannot_stamp_themselves():
    """The email sending, the page opening and the form posting all stamp
    themselves. A meeting and a signature cannot, so a human records them."""
    referrer = _physician()
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]
    admin = _admin()

    r = client.post(f"/api/asclepius/admin/hs-referrals/{row['hs_referral_id']}/advance",
                    json={"status": "met"}, headers=A.headers_for(admin))
    assert r.status_code == 200, r.text
    assert _store().get_hs_referral(row["hs_referral_id"])["status"] == "met"

    bad = client.post(f"/api/asclepius/admin/hs-referrals/{row['hs_referral_id']}/advance",
                      json={"status": "nonsense"}, headers=A.headers_for(admin))
    assert bad.status_code == 422


def test_a_physician_cannot_advance_their_own_introduction():
    referrer = _physician()
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]
    r = client.post(f"/api/asclepius/admin/hs-referrals/{row['hs_referral_id']}/advance",
                    json={"status": "signed"}, headers=A.headers_for(referrer))
    assert r.status_code in (401, 403)


def test_paying_an_introduction_is_a_typed_amount_and_pays_once():
    """There is no rate and nothing that derives one, institutional terms are
    negotiated per deal. The ledger's UNIQUE(kind, ref_id) makes a double-click
    or two admins working the same deal harmless."""
    from asclepius import payments as asc_payments

    referrer = _physician()
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]
    admin = _admin()
    url = f"/api/asclepius/admin/hs-referrals/{row['hs_referral_id']}/reward"

    first = client.post(url, json={"amount_cents": 250000}, headers=A.headers_for(admin))
    assert first.status_code == 200, first.text
    assert first.json()["already"] is False
    assert first.json()["earning"]["amount_cents"] == 250000

    second = client.post(url, json={"amount_cents": 999999}, headers=A.headers_for(admin))
    assert second.json()["already"] is True
    assert second.json()["earning"]["amount_cents"] == 250000, "must not restate"

    with _store()._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM earnings WHERE kind = ?",
            (asc_payments.KIND_HS_REFERRAL,)).fetchone()["n"]
    assert n == 1

    after = _store().get_hs_referral(row["hs_referral_id"])
    assert after["reward_state"] == "paid"
    assert after["reward_earning_id"]


def test_a_zero_or_negative_reward_is_refused():
    referrer = _physician()
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]
    admin = _admin()
    url = f"/api/asclepius/admin/hs-referrals/{row['hs_referral_id']}/reward"
    for amount in (0, -100):
        assert client.post(url, json={"amount_cents": amount},
                           headers=A.headers_for(admin)).status_code == 422, amount


def test_the_admin_list_never_hands_out_the_bearer_token():
    """An admin needs the contact details to work the lead. They do not need the
    landing token, and it would let anyone holding a screenshot read those same
    details off the PUBLIC prefill route."""
    referrer = _physician()
    assert _submit_only(referrer).status_code == 200
    r = client.get("/api/asclepius/admin/hs-referrals", headers=A.headers_for(_admin()))
    assert r.status_code == 200, r.text
    rows = r.json()["referrals"]
    assert len(rows) == 1
    assert rows[0]["contact_email"]
    assert rows[0]["referrer_email"] == referrer["email"]
    assert "landing_token" not in rows[0]


# ═══ Attribution from the landing page ════════════════════════════════════════
def test_submitting_the_partner_form_advances_the_funnel():
    referrer = _physician()
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]

    r = client.post("/api/leads", json={
        "source": "health_system_partner",
        "email": "j.okoye@meridianhealth.org",
        "message": "Health system:\nMeridian Health",
        "referral_token": row["landing_token"],
    })
    assert r.status_code == 200, r.text
    assert _store().get_hs_referral(row["hs_referral_id"])["status"] == "submitted"


def test_a_lead_with_an_unknown_token_is_still_accepted():
    """A stale or mistyped token must not cost us the lead."""
    r = client.post("/api/leads", json={
        "source": "health_system_partner", "email": "someone@example.org",
        "message": "We hold records.", "referral_token": "not-a-real-token",
    })
    assert r.status_code == 200, r.text


# ═══ The separation that keeps the two paths apart ════════════════════════════
def test_a_health_system_referral_never_touches_the_physician_ledger():
    """The reason ``hs_referrals`` is its own table. Nothing on this path may
    accrue an earning, and no row may appear in the physician funnel."""
    referrer = _physician(full_name="Priya Patel")
    assert _submit_only(referrer).status_code == 200
    row = _store().list_hs_referrals_by_referrer(referrer["id"])[0]
    _deliver(referrer, row["hs_referral_id"], _GOOD)

    r = client.get("/api/asclepius/referrals", headers=A.headers_for(referrer))
    payload = r.json()
    assert payload["referrals"] == [], "an institutional intro is not a physician referral"
    assert payload["earned_cents"] == 0
    assert payload["pending_count"] == 0

    with _store()._conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM earnings").fetchone()["n"]
    assert n == 0, "nothing on the health-system path may reach the ledger"
