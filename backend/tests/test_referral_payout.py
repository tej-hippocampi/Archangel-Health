"""PRD-REF — the referral bounty, and the guards that stop it being free money.

A referral is a bet with a five-week settlement, so almost everything that can go
wrong here goes wrong QUIETLY: a bounty paid twice looks like a generous month, a
bounty never paid looks like a doctor who did not refer anyone, and a funnel row
stranded at 'invited' looks exactly like a colleague who never replied. None of
those raise anything. So the properties are asserted directly.

The load-bearing ones:

  * **Approval, not submission, is the trigger.** Otherwise the cheapest $150 is
    a referral who submits one thing and leaves.
  * **One bounty per referred physician, EVER** — not per referral row. Two
    physicians referring the same colleague is the normal case for a
    well-connected candidate, not the edge one, and ``UNIQUE(kind, ref_id)``
    does nothing about it because those are two different ref_ids.
  * **The loser's row says 'duplicate'**, never 'invited' forever. A referrer
    whose colleague demonstrably joined and worked must not be looking at a
    funnel that still says "awaiting their first case" — that is the advisor
    stranding bug repeating with money attached.
  * **The response is not an oracle.** Byte-identical for a new address and an
    existing account.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import payments as asc_payments  # noqa: E402
from asclepius import referrals as asc_referrals  # noqa: E402
from asclepius.store import get_store  # noqa: E402

client = TestClient(A.app)

BOUNTY = asc_payments.referral_bounty_cents()


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    import ratelimit
    ratelimit.reset()
    yield


def _store():
    return get_store()


def _physician(**kw):
    """An APPROVED physician — which is what makes them able to refer at all."""
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology", **kw)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (u["id"],))
    return store.get_user_by_id(u["id"])


def _equity_only_reviewer():
    """A reviewer carrying compensation_model='equity_only' (a legacy advisor
    row the boot migration has not yet cleared). The rule under test survives
    the retired tier: equity_only accrues no cash, bounties included."""
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET tier = 'reviewer', verification_status = 'approved', "
                     "compensation_model = 'equity_only' WHERE id = ?", (u["id"],))
    return store.get_user_by_id(u["id"])


def _refer(referrer, email, name=None, expect=200):
    r = client.post("/api/asclepius/referrals",
                    json={"email": email, "name": name},
                    headers=A.headers_for(referrer))
    assert r.status_code == expect, r.text
    return r.json()


def _funnel(user):
    r = client.get("/api/asclepius/referrals", headers=A.headers_for(user))
    assert r.status_code == 200, r.text
    return r.json()


def _signup(email, *, full_name=None):
    """The invitee comes in through the normal provisioning path."""
    store = _store()
    user = store.provision_user(email=email, password="pw-12345678",
                                role="evaluator", full_name=full_name)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved', "
                     "tier = 'labeler' WHERE id = ?", (user["id"],))
    store.claim_referral_for_signup(email=email, user_id=user["id"])
    return store.get_user_by_id(user["id"])


def _approved_task(user, ref):
    """One task earning already in APPROVED — the bounty's actual trigger.

    Written straight to the ledger rather than driven through submit + review,
    because the trigger under test is "a task earning for this physician reached
    APPROVED" and ``payments`` owns exactly one definition of that. Driving the
    review pipeline here would be testing the pipeline.
    """
    return _store().insert_earning(
        earning_id=f"earn-{ref}", user_id=user["id"], kind=asc_payments.KIND_TASK,
        ref_id=ref, amount_cents=7500, rate_cents=7500,
        status=asc_payments.APPROVED, accrued_at="2026-08-01T00:00:00",
        resolved_at="2026-08-02T00:00:00")


def _bounties_for(user):
    return [e for e in _store().earnings_for_user(user["id"])
            if e["kind"] == asc_payments.KIND_REFERRAL]


def _email():
    return f"invitee-{A.uniq(10)}@example.com"


# ═══ 1-3 · The trigger ═══════════════════════════════════════════════════════
def test_the_first_approved_task_accrues_exactly_one_bounty():
    referrer = _physician()
    email = _email()
    _refer(referrer, email, name="Dr Whitfield")
    invitee = _signup(email, full_name="Dr A. Whitfield")

    # Before their first approved task there is no money, and — the part that
    # matters — the funnel says so out loud rather than showing nothing.
    funnel = _funnel(referrer)
    assert funnel["pending_count"] == 1
    assert funnel["pending_cents"] == BOUNTY
    assert funnel["referrals"][0]["bounty_state"] == "pending"
    assert _bounties_for(referrer) == []

    _approved_task(invitee, "sub-1")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])

    rows = _bounties_for(referrer)
    assert len(rows) == 1
    assert rows[0]["amount_cents"] == BOUNTY
    # A bounty has no review step: it is earned or it is not, so it goes straight
    # to APPROVED rather than passing through `accrued` the way a task does.
    assert rows[0]["status"] == asc_payments.APPROVED
    assert "completed their first case" in rows[0]["note"]
    assert "Dr A. Whitfield" in rows[0]["note"]


def test_their_second_third_and_tenth_approved_tasks_accrue_nothing_further():
    referrer = _physician()
    email = _email()
    _refer(referrer, email)
    invitee = _signup(email)

    paid = []
    for i in range(10):
        _approved_task(invitee, f"sub-{i}")
        paid.append(asc_payments.accrue_referral_bounty(
            _store(), referred_user_id=invitee["id"]))

    assert len(_bounties_for(referrer)) == 1, (
        "a bounty is one-time; ten approved tasks is still one referral")
    # Only the call that actually wrote the row reports an id. The reconcile
    # counters and the event log both key off that, so a re-observation that
    # claimed to have paid would log a payment on every page load.
    assert paid[0] is not None
    assert paid[1:] == [None] * 9


def test_a_rejected_submission_accrues_nothing():
    """The trigger is APPROVAL, not submission. Otherwise the cheapest way to
    earn $150 is to refer someone who submits one thing and leaves."""
    referrer = _physician()
    email = _email()
    _refer(referrer, email)
    invitee = _signup(email)

    _store().insert_earning(
        earning_id="earn-void", user_id=invitee["id"], kind=asc_payments.KIND_TASK,
        ref_id="sub-void", amount_cents=7500, rate_cents=7500,
        status=asc_payments.VOID, accrued_at="2026-08-01T00:00:00",
        resolved_at="2026-08-02T00:00:00")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])
    assert _bounties_for(referrer) == []

    # And an ACCRUED row — submitted, awaiting review — is not approval either.
    _store().insert_earning(
        earning_id="earn-pending", user_id=invitee["id"], kind=asc_payments.KIND_TASK,
        ref_id="sub-pending", amount_cents=7500, rate_cents=7500,
        status=asc_payments.ACCRUED, accrued_at="2026-08-01T00:00:00")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])
    assert _bounties_for(referrer) == []


# ═══ 4 · Concurrency ═════════════════════════════════════════════════════════
def test_five_concurrent_accruals_still_write_exactly_one_row():
    """``UNIQUE(kind, ref_id)`` makes this true at the DATABASE level rather than
    by an application check that could race. Five threads, one row."""
    import threading

    referrer = _physician()
    email = _email()
    _refer(referrer, email)
    invitee = _signup(email)
    _approved_task(invitee, "sub-1")

    errors = []

    def _go():
        try:
            asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])
        except Exception as exc:                      # pragma: no cover - a failure IS the finding
            errors.append(exc)

    threads = [threading.Thread(target=_go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent accrual raised: {errors}"
    assert len(_bounties_for(referrer)) == 1


# ═══ 5-6 · Who may be paid ═══════════════════════════════════════════════════
def test_an_equity_only_referrer_accrues_nothing():
    """``compensation.accrues_payment`` is the predicate, and it holds on
    referrals exactly as it holds on tasks and sessions. An equity_only row
    holds equity instead of a cash rate; a bounty is cash."""
    advisor = _equity_only_reviewer()
    assert advisor["compensation_model"] == "equity_only"
    email = _email()
    r = client.post("/api/asclepius/referrals", json={"email": email},
                    headers=A.headers_for(advisor))
    assert r.status_code == 200, r.text
    invitee = _signup(email)
    _approved_task(invitee, "sub-1")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])

    assert _bounties_for(advisor) == []
    # And the row is SETTLED rather than left pending forever — a funnel row that
    # will never resolve is the same failure as an empty page, reached from the
    # other direction.
    funnel = _funnel(advisor)
    assert funnel["earns_bounty"] is False
    assert funnel["referrals"][0]["bounty_state"] == "ineligible"
    assert funnel["pending_cents"] == 0


def test_self_referral_is_refused_at_invite_and_at_accrual():
    referrer = _physician()

    # At invite: the honest mistake.
    r = client.post("/api/asclepius/referrals", json={"email": referrer["email"]},
                    headers=A.headers_for(referrer))
    assert r.status_code == 422
    assert "somebody else" in r.json()["detail"].lower()

    # At accrual: the patient version. Emails change, so a row can become a
    # self-referral after the fact — here by the referrer signing up under the
    # address they invited.
    store = _store()
    code = store.ensure_referral_code(referrer["id"])
    ref = store.insert_referral(
        referrer_id=referrer["id"], referral_code=code,
        invitee_email="later@example.com", status="signed_up")
    with store._conn() as conn:
        conn.execute("UPDATE referrals SET user_id = ? WHERE referral_id = ?",
                     (referrer["id"], ref["referral_id"]))
    _approved_task(referrer, "sub-self")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=referrer["id"])

    assert _bounties_for(referrer) == []
    assert store.get_referral(ref["referral_id"])["bounty_state"] == "ineligible"


# ═══ 7 · Two referrers, one invitee ══════════════════════════════════════════
def test_two_referrers_one_invitee_is_one_bounty_and_the_loser_says_duplicate():
    """A well-connected candidate is exactly who gets referred twice, so this is
    the normal case. ``UNIQUE(kind, ref_id)`` does NOT cover it — two referral
    rows are two different ref_ids — which is why the winner is picked inside one
    transaction, and why the loser is marked rather than stranded."""
    first, second = _physician(), _physician()
    email = _email()
    _refer(first, email, name="Dr Popular")
    _refer(second, email, name="Dr Popular")
    invitee = _signup(email, full_name="Dr Popular")
    _approved_task(invitee, "sub-1")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])

    paid = _bounties_for(first) + _bounties_for(second)
    assert len(paid) == 1, "one physician, one bounty — not one per referral row"

    states = {r["bounty_state"] for r in _store().referrals_for_invitee(invitee["id"])}
    assert states == {"earned", "duplicate"}, (
        "the losing row must SAY duplicate; left at 'invited' it renders as "
        "'awaiting their first case' forever, long after the person joined")

    # The loser's funnel is honest about it, and shows no phantom pending money.
    loser = second if _bounties_for(first) else first
    funnel = _funnel(loser)
    assert funnel["pending_cents"] == 0
    assert "already credited to another referrer" in funnel["referrals"][0]["status_sentence"]


# ═══ 8 · Expiry ══════════════════════════════════════════════════════════════
def test_a_ninety_one_day_old_invitation_expires_and_can_never_pay():
    """Without expiry a doctor's funnel is a graveyard of two-year-old
    invitations and the page stops meaning anything."""
    store = _store()
    referrer = _physician()
    email = _email()
    _refer(referrer, email)

    stale = (datetime.now(timezone.utc) - timedelta(days=91)).replace(
        tzinfo=None, microsecond=0).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE referrals SET invited_at = ? WHERE referrer_id = ?",
                     (stale, referrer["id"]))

    funnel = _funnel(referrer)
    assert funnel["referrals"][0]["bounty_state"] == "expired"
    assert funnel["referrals"][0]["status_sentence"] == "Invitation expired"
    assert funnel["pending_cents"] == 0

    # And it stays dead: a signup and an approved task afterwards pay nothing.
    invitee = _signup(email)
    _approved_task(invitee, "sub-late")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])
    assert _bounties_for(referrer) == []


def test_an_invitee_who_was_not_verified_stops_counting_as_pending_money():
    """The other graveyard, and the expiry sweep does not reach it: the invitee
    DID sign up, so ``user_id`` is set and the row is never expired — but they
    were refused verification, so no first case is coming. Left alone the funnel
    renders "+$150 pending" forever beside a colleague who was turned down, which
    is the page lying to a physician about their own money."""
    store = _store()
    referrer = _physician()
    email = _email()
    _refer(referrer, email, name="Dr Turned Down")
    invitee = _signup(email)
    store.advance_referral_for_user(invitee["id"], "declined")

    funnel = _funnel(referrer)
    row = funnel["referrals"][0]
    assert row["status_sentence"] == "Not verified"
    assert row["bounty_state"] == "closed"
    assert funnel["pending_count"] == 0
    assert funnel["pending_cents"] == 0

    # DERIVED, never stored — 'declined' is not on the funnel ladder and is
    # therefore reversible. A physician refused today can be approved next month,
    # and the money has to come back with them.
    assert store.list_referrals_by_referrer(referrer["id"])[0]["bounty_state"] is None
    store.advance_referral_for_user(invitee["id"], "approved")
    back = _funnel(referrer)
    assert back["referrals"][0]["bounty_state"] == "pending"
    assert back["pending_cents"] == BOUNTY


def test_a_rate_change_never_restates_a_bounty_already_earned():
    """Every rate in this system is stamped on the ledger row at accrual so a
    change to the env constant cannot rewrite history. The funnel is the one
    surface where a doctor would READ that restatement, so it reports what the
    ledger paid rather than what the rate is today."""
    referrer = _physician()
    email = _email()
    _refer(referrer, email, name="Dr Whitfield")
    invitee = _signup(email, full_name="Dr A. Whitfield")
    _approved_task(invitee, "sub-1")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])
    assert _bounties_for(referrer)[0]["amount_cents"] == BOUNTY

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setenv("ASCLEPIUS_REFERRAL_BOUNTY_CENTS", str(BOUNTY * 2))
        assert asc_payments.referral_bounty_cents() == BOUNTY * 2
        body = client.get("/api/asclepius/earnings",
                          headers=A.headers_for(referrer)).json()
    finally:
        monkey.undo()

    row = body["referrals"]["referrals"][0]
    assert row["bounty_state"] == "earned"
    assert row["bounty_cents"] == BOUNTY, "the earned row was restated at the new rate"
    assert body["referrals"]["earned_cents"] == BOUNTY
    # And the breakdown, which reads the ledger directly, agrees with it.
    line = [l for l in body["lines"] if l["kind"] == "referral"][0]
    assert line["cents"] == BOUNTY
    # The card's forward-looking promise DOES move — a referral sent tomorrow is
    # worth the new rate, and saying otherwise would be the opposite error.
    assert body["referrals"]["bounty_cents"] == BOUNTY * 2


def test_a_fresh_invitation_is_not_swept_by_the_expiry_pass():
    """The negative half. A sweep that expires everything is worse than none."""
    referrer = _physician()
    _refer(referrer, _email())
    funnel = _funnel(referrer)
    assert funnel["referrals"][0]["bounty_state"] == "pending"
    assert funnel["pending_count"] == 1


# ═══ 9-10 · The endpoint ═════════════════════════════════════════════════════
def test_the_post_response_is_identical_for_a_new_email_and_an_existing_account():
    """The account-existence oracle, closed. Generalising this endpoint from
    three advisors to every approved physician multiplies who can run the probe,
    so 'already a member' stops being reported in the response at all — the
    referrer learns the outcome from their OWN funnel a request later."""
    referrer = _physician()
    member = _physician()

    fresh = _refer(referrer, _email())
    existing = _refer(referrer, member["email"])

    assert json.dumps(fresh, sort_keys=True) == json.dumps(existing, sort_keys=True), (
        "the response differed, so it can be used to enumerate accounts")
    assert "already" not in fresh
    assert "email_sent" not in fresh
    # The referral is RECORDED either way — the oracle is closed by not telling,
    # not by refusing to act.
    assert _funnel(referrer)["total"] == 2


def test_the_rate_limit_is_keyed_on_the_user_not_the_ip():
    """A hospital NATs. An IP-keyed cap 429s the eleventh referral out of one
    building while the actual threat — a stolen token rotated across a proxy
    pool — goes unthrottled."""
    import ratelimit

    a, b = _physician(), _physician()
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(ratelimit, "is_enabled", lambda: True)
        # Both physicians share one client IP under TestClient. Burn most of A's
        # personal budget; B must be entirely unaffected.
        for _ in range(6):
            _refer(a, _email())
        assert _refer(b, _email())["ok"] is True
    finally:
        monkey.undo()
        ratelimit.reset()

    assert asc_referrals.REFERRALS_PER_USER[0] == 20
    keys = dict(asc_referrals.throttle_keys("user-x"))
    assert "asclepius_referral:user-x" in keys, "the bucket must carry the user id"


# ═══ 11 · What a referrer may see ════════════════════════════════════════════
def test_the_funnel_never_carries_the_invitees_credentialing_file():
    """Referring someone does not entitle you to their credentialing dossier.
    Asserted on the whole serialized payload, because the rule is about what
    CANNOT appear rather than about a list of fields somebody stripped."""
    store = _store()
    referrer = _physician()
    email = _email()
    _refer(referrer, email, name="Dr Chen")
    invitee = _signup(email, full_name="Dr Chen")
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET tier_score = 88, npi = '1234567893', "
            "verification_notes = 'SENTINEL-CV-NOTE-strong', npi_verified = 1 "
            "WHERE id = ?", (invitee["id"],))

    raw = json.dumps(_funnel(referrer)).lower()
    # "awaiting verification" is COPY — the funnel's own plain-language sentence
    # — so the sentinels are the field names and the values, not the bare word.
    # A test whose sentinel collides with legitimate output gets muted, and a
    # muted test guards nothing.
    for leaked in ("npi", "tier_score", "verification_notes", "verification_status",
                   "sentinel-cv-note", "1234567893", "board_cert", "cv_asset",
                   "id_hashed", "verified_by"):
        assert leaked not in raw, f"the referrer's funnel leaked {leaked!r}"


def test_a_referral_with_no_name_shows_a_masked_address_never_a_raw_one():
    """A third party's address does not go back to the referrer once the system
    knows who they are — and the domain survives because that is the half that
    helps a referrer recognise who they invited."""
    referrer = _physician()
    _refer(referrer, "jane.doe@mgh.example.org")
    row = _funnel(referrer)["referrals"][0]
    assert row["invitee_display"] == "j••••@mgh.example.org"
    assert "jane.doe@" not in json.dumps(_funnel(referrer))


def test_no_raw_status_token_reaches_the_referrer():
    """A physician should never have to learn our state machine to know whether
    their friend is nearly there."""
    referrer = _physician()
    email = _email()
    _refer(referrer, email, name="Dr Osei")
    _signup(email, full_name="Dr Osei")

    row = _funnel(referrer)["referrals"][0]
    assert row["status_sentence"] == "Signed up, awaiting verification"
    for token in ("signed_up", "paid_out"):
        assert token not in row["status_sentence"]


def test_one_physician_cannot_read_anothers_funnel_by_any_parameter():
    """Scoped from the SESSION. The route takes no id at all, which is the IDOR
    rule applied at the design level rather than validated after the fact."""
    a, b = _physician(), _physician()
    _refer(b, _email(), name="B's referral")

    for query in ("", f"?user_id={b['id']}", f"?referrer_id={b['id']}",
                  f"?id={b['id']}"):
        body = client.get("/api/asclepius/referrals" + query,
                          headers=A.headers_for(a)).json()
        assert body["total"] == 0, f"leaked B's funnel via {query!r}"


def test_a_physician_awaiting_verification_cannot_refer():
    """The gate is re-checked at the boundary that sends mail to a third party,
    even though ``get_current_user`` already refuses these accounts."""
    store = _store()
    pending = A.make_user(store, role="evaluator", tier=None)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'pending' WHERE id = ?",
                     (pending["id"],))
    r = client.post("/api/asclepius/referrals", json={"email": _email()},
                    headers=A.headers_for(store.get_user_by_id(pending["id"])))
    assert r.status_code == 403
    assert asc_referrals.can_refer(store.get_user_by_id(pending["id"])) is False


# ═══ 12 · The email ══════════════════════════════════════════════════════════
def test_a_referrer_with_no_name_produces_the_neutral_email_with_no_address():
    """That string goes into the SUBJECT LINE of a message to a third party. A
    physician with no name on file would have had their personal address
    disclosed to everyone they invited — and "toby@gmail.com suggested you'd be a
    good fit" is not the sentence that makes a named referral work anyway."""
    import asyncio

    sent = {}

    async def _fake_send(to, subject, html, **kw):
        sent.update({"to": to, "subject": subject, "html": html})
        return True

    referrer = _physician()
    assert not (referrer.get("full_name") or "").strip()

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr("email_utils.is_email_transport_configured", lambda: True)
        monkey.setattr("email_utils.send_html_email", _fake_send)
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            asc_referrals.send_invite(
                referrer=referrer, email="invitee@example.com", name="Dr Chen",
                code=_store().ensure_referral_code(referrer["id"])))
    finally:
        monkey.undo()

    assert sent, "no email was sent"
    assert referrer["email"] not in sent["subject"]
    assert referrer["email"] not in sent["html"]
    assert "@" not in sent["subject"]
    assert "suggested you" not in sent["html"], "no name means no named referral"


def test_a_named_referrer_is_the_subject_line_and_the_first_sentence():
    """That one sentence IS the mechanism — and the exit line beside it is what
    keeps a spam complaint from costing the sending domain every other
    physician's invite."""
    from onboarding_emails import build_asclepius_invite_email

    html = build_asclepius_invite_email(
        invitee_first_name="Sarah", director_full_name="Sarah Chen",
        role_label="Physician contributor", org_name="Archangel Health",
        specialty="nephrology", onboarding_url="https://example.com/onboard/abc",
        invitee_email="sarah@example.com", referrer_name="Sarah Chen")
    assert "Sarah Chen" in html
    assert "suggested you" in html
    assert "we won&rsquo;t follow up" in html

    # And it stays ONE invite email: with no referrer the org invite path renders
    # byte-identically to before.
    plain = build_asclepius_invite_email(
        invitee_first_name="Sarah", director_full_name="Toby Barrack",
        role_label="Physician contributor", org_name="Archangel Health",
        specialty="nephrology", onboarding_url="https://example.com/onboard/abc",
        invitee_email="sarah@example.com")
    assert "suggested you" not in plain
    assert "follow up" not in plain


def test_free_text_on_a_referral_is_bounded_and_collapsed():
    """A name and a one-line note — not an upload channel. Bounded in the shared
    module rather than on each router's model, so a third surface cannot ship
    without the cap, and collapsed so a name with a newline in it cannot break
    the row it renders in."""
    referrer = _physician()
    r = client.post("/api/asclepius/referrals",
                    json={"email": _email(), "name": "Dr " + "X" * 500,
                          "note": "N" * 5000},
                    headers=A.headers_for(referrer))
    assert r.status_code == 200, r.text

    stored = _store().list_referrals_by_referrer(referrer["id"])[0]
    assert len(stored["invitee_name"]) == 120
    assert len(stored["note"]) == 500
    assert _funnel(referrer)["referrals"][0]["invitee_display"] == stored["invitee_name"]

    assert asc_referrals._clip("  Dr   Jane\n Chen  ", 120) == "Dr Jane Chen"
    assert asc_referrals._clip("   ", 120) is None


def test_a_crlf_in_a_display_name_cannot_reach_a_mime_header():
    """SendGrid's JSON transport is immune; the SMTP fallback assigns the string
    straight into a header, where a CR/LF is injection. "Only trusted people can
    set a display name" is a fact about today's permissions, not a property of
    the code."""
    dirty = "Dr Evil\r\nBcc: everyone@example.com"
    assert "\r" not in asc_referrals.header_safe(dirty)
    assert "\n" not in asc_referrals.header_safe(dirty)
    assert asc_referrals.header_safe(dirty).startswith("Dr Evil Bcc:")


# ═══ The Earnings payload ════════════════════════════════════════════════════
def test_the_earnings_page_carries_the_pending_line_that_is_the_whole_feature():
    """A doctor who refers two colleagues and sees nothing for a month concludes
    it is broken, and you lose the mechanism that produces most of your supply.
    The pending line is what stops that."""
    referrer = _physician()
    email = _email()
    _refer(referrer, email, name="Dr Osei")
    _signup(email, full_name="Dr Osei")

    body = client.get("/api/asclepius/earnings",
                      headers=A.headers_for(referrer)).json()
    line = [l for l in body["lines"] if l["kind"] == "referral"]
    assert len(line) == 1
    assert line[0]["pending_count"] == 1
    assert line[0]["pending_cents"] == BOUNTY
    assert line[0]["pending_label"] == "1 invited, awaiting their first case"
    assert body["referrals"]["pending_cents"] == BOUNTY


def test_a_physician_who_has_referred_nobody_gets_no_referral_line():
    """The card does the asking. A permanent "Referrals 0 × $150 · $0" row is the
    growth-loop instinct this feature is supposed to resist."""
    body = client.get("/api/asclepius/earnings",
                      headers=A.headers_for(_physician())).json()
    assert [l for l in body["lines"] if l["kind"] == "referral"] == []
    assert body["referrals"]["total"] == 0
    assert body["referrals"]["can_refer"] is True


def test_an_earned_bounty_lands_on_the_earnings_page_through_the_ordinary_read():
    """End to end, through the surface the physician actually uses: the referrer
    never has to do anything for the money to appear, and never has to wonder
    whether it worked."""
    referrer = _physician()
    email = _email()
    _refer(referrer, email, name="Dr Whitfield")
    invitee = _signup(email, full_name="Dr A. Whitfield")
    _approved_task(invitee, "sub-1")

    body = client.get("/api/asclepius/earnings",
                      headers=A.headers_for(referrer)).json()
    line = [l for l in body["lines"] if l["kind"] == "referral"][0]
    assert line["count"] == 1
    assert line["cents"] == BOUNTY
    assert line["pending_count"] == 0
    assert body["approved_cents"] == BOUNTY
    assert body["referrals"]["referrals"][0]["bounty_state"] == "earned"
    assert body["referrals"]["referrals"][0]["status_sentence"] == "Completed first case"
    row = [r for r in body["recent"] if r["kind"] == "referral"][0]
    assert row["kind_label"] == "Referral"
    assert row["status_word"] == "Approved"


def test_the_bounty_survives_the_verification_routes_writing_the_funnel_again():
    """The reason ``bounty_state`` is its own column. ``advance_referral_for_user``
    is called from the verification decision points, and a PAID referral must not
    be rewritable by an event about credentialing."""
    referrer = _physician()
    email = _email()
    _refer(referrer, email)
    invitee = _signup(email)
    _approved_task(invitee, "sub-1")
    asc_payments.accrue_referral_bounty(_store(), referred_user_id=invitee["id"])
    assert len(_bounties_for(referrer)) == 1

    _store().advance_referral_for_user(invitee["id"], "approved")
    _store().advance_referral_for_user(invitee["id"], "declined")

    funnel = _funnel(referrer)
    assert funnel["referrals"][0]["bounty_state"] == "earned"
    assert funnel["referrals"][0]["status_sentence"] == "Completed first case"
    assert len(_bounties_for(referrer)) == 1
