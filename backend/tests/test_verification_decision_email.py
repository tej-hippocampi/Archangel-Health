"""Every path that approves a physician tells the physician, exactly once.

Three code paths set verification_status='approved': the admin console, the
verification agent's auto-approval, and /admin/physicians/restore. Only the
console sent anything, and nothing failed: a silent path fails nothing.

Onboarding v2 made that worse rather than moot. Its wizard has no password
step, so approval is the moment an account becomes usable at all, and only the
console minted a credential. An agent-approved or operator-repaired physician
therefore had no mail AND no password: they could not sign in, and nobody was
told.

The split now is: the console keeps its inline, synchronous welcome when it
MINTS credentials, because that mail carries a secret this request created and
nothing else can. Every other approval, on any path, is queued by the hook on
store.record_verification_decision, which is the only production writer of that
status and of the tier columns. One mail per approval between them.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import tests._asclepius as A
from onboarding_emails import (
    build_asclepius_approved_email,
    build_asclepius_rejected_email,
)

client = TestClient(A.app)

_INTERNAL_NOTE = "NPI belongs to a different Jane Doe, likely fraud"


def _outbox(store, email=None, kind=None):
    sql = "SELECT kind, recipient_email, subject, body_html, status, send_after " \
          "FROM admin_notify_outbox WHERE 1=1"
    args = []
    if email:
        sql += " AND recipient_email = ?"
        args.append(email)
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    with store._conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def _physician(store, **kw):
    kw.setdefault("tier", None)
    kw.setdefault("practice_case", False)
    return A.make_user(store, **kw)


# ─── The point of the whole change ───────────────────────────────────────────
@pytest.mark.parametrize("path", ["agent", "restore"])
def test_the_paths_that_used_to_say_nothing_now_tell_the_physician(path):
    """Onboarding v2 made approval the moment credentials come into existence,
    and only the admin console mints them. So under v2 an agent-approved or
    operator-repaired physician had no mail AND no password: they could not sign
    in at all, and nothing anywhere said so.

    The console path is v2's and is left alone; it sends synchronously and warns
    the admin when the send fails. These two are the ones nobody was watching.
    """
    store = A.fresh_store()
    admin = A.make_user(store, role="admin", practice_case=False)

    if path == "agent":
        # The agent writes through the same store method rather than its own
        # SQL, which is exactly why hooking the write covers it.
        user = _physician(store)
        store.record_verification_decision(user["id"], status="approved",
                                           decided_by="agent:auto", tier="labeler")
    else:
        user = _physician(store, role="admin", specialty="nephrology",
                          board_cert="board_certified_nephrology", years_experience=15)
        r = client.post(f"/api/asclepius/admin/physicians/restore?email={user['email']}",
                        json={"approve_verification": True, "tier": "labeler"},
                        headers=A.headers_for(admin))
        assert r.status_code == 200, r.text

    rows = _outbox(store, email=user["email"], kind="physician_approved")
    assert len(rows) == 1, f"{path} told the physician nothing"
    assert rows[0]["subject"] == "You're approved for Asclepius"


def test_approving_twice_emails_once():
    """A retried request, a double-click and a restore that re-stamps the row
    all produce one mail. The UNIQUE idempotency key is the arbiter."""
    store = A.fresh_store()
    user = _physician(store)
    for _ in range(3):
        store.record_verification_decision(user["id"], status="approved",
                                           decided_by="admin@x", tier="labeler")
    assert len(_outbox(store, email=user["email"])) == 1


def test_a_restore_that_only_changes_a_tier_does_not_claim_they_are_newly_approved():
    """restore_physician re-stamps an already-approved account to move a tier,
    and "you're approved" months after the fact is not news. The hook gates on
    the TRANSITION rather than on the final state."""
    store = A.fresh_store()
    user = _physician(store)
    store.set_verification_status(user["id"], "approved")
    store.record_verification_decision(user["id"], status="approved",
                                       decided_by="admin@x", tier="reviewer")
    assert _outbox(store, email=user["email"]) == []


def test_an_account_that_already_has_a_password_is_told_once_not_twice(monkeypatch):
    """The console used to send this notice inline. It is queued now, so the
    console branch and the hook cannot both fire for the same approval, and a
    transport blip becomes a retry rather than a mail lost forever with a log
    line. The tier gets named on the way, which that branch never did."""
    import email_utils

    async def explode(*a, **kw):
        raise RuntimeError("SendGrid is down")

    monkeypatch.setattr(email_utils, "send_html_email", explode)
    store = A.fresh_store()
    admin = A.make_user(store, role="admin", practice_case=False)
    user = _physician(store)
    store.set_verification_status(user["id"], "pending")
    r = client.post(f"/api/asclepius/verify/queue/{user['id']}/approve",
                    json={"tier": "labeler", "note": "ok"}, headers=A.headers_for(admin))
    assert r.status_code == 200
    assert len(_outbox(store, email=user["email"])) == 1


def test_a_broken_outbox_never_costs_a_physician_their_approval(monkeypatch):
    store = A.fresh_store()

    def explode(*a, **kw):
        raise RuntimeError("outbox is wedged")

    monkeypatch.setattr(store, "enqueue_admin_notification", explode)
    user = _physician(store)
    updated = store.record_verification_decision(user["id"], status="approved",
                                                 decided_by="admin@x", tier="labeler")
    assert updated["verification_status"] == "approved"
    assert updated["tier"] == "labeler"


# ─── What it says ────────────────────────────────────────────────────────────
def test_the_approval_names_the_tier_as_a_word_and_never_as_a_token():
    body = build_asclepius_approved_email(
        full_name="Ada Reyes", workspace_url="https://x/asclepius",
        tier_word="Reviewer", can_review=True)
    assert "Reviewer" in body
    assert "both queues" in body, "a reviewer should be told what the tier opens"
    # The raw column value must never reach a physician.
    assert not re.search(r">\s*reviewer\b", body)


def test_an_approval_with_no_tier_omits_the_paragraph_rather_than_saying_unassigned():
    """restore_physician can approve carrying no tier, and a placeholder in a
    congratulations email is worse than silence about it."""
    body = build_asclepius_approved_email(
        full_name="Ada Reyes", workspace_url="https://x/asclepius", tier_word="")
    assert "Unassigned" not in body
    assert "approved as a" not in body
    assert "You&rsquo;re approved." in body


def test_the_approval_promises_no_promotion():
    """There is no promotion mechanism in the product: the only writers of
    users.tier are approval-time and the restore backfill. An email implying one
    is a promise the codebase cannot keep."""
    body = build_asclepius_approved_email(
        full_name="Ada", workspace_url="https://x", tier_word="Labeler")
    for word in ("promot", "move up", "upgrade", "work your way"):
        assert word not in body.lower(), word


def test_a_rejection_reaches_the_physician_and_never_quotes_the_internal_note():
    """The note is mandatory and is written by an admin for an audit trail. It
    may carry an accusation, a suspicion, or a third party's name, none of which
    was drafted to be read by its subject."""
    store = A.fresh_store()
    user = _physician(store)
    store.record_verification_decision(user["id"], status="rejected",
                                       decided_by="admin@x", note=_INTERNAL_NOTE)
    rows = _outbox(store, email=user["email"], kind="physician_rejected")
    assert len(rows) == 1
    assert rows[0]["subject"] == "About your Asclepius application"
    body = rows[0]["body_html"]
    for leak in ("Jane Doe", "fraud", "NPI"):
        assert leak not in body, leak
    # And it is still recorded where it belongs.
    assert "fraud" in (store.get_user_by_id(user["id"])["verification_notes"] or "")


def test_the_rejection_leaves_a_door_open_to_a_person():
    body = build_asclepius_rejected_email(full_name="Ada Reyes")
    assert "reply to this email" in body.lower()
    assert "look again" in body


def test_approving_inside_the_grace_window_voids_the_rejection():
    """A rejection is irreversible in somebody's inbox. Half an hour buys back a
    misclick."""
    store = A.fresh_store()
    user = _physician(store)
    store.record_verification_decision(user["id"], status="rejected",
                                       decided_by="admin@x", note="a misclick")
    queued = _outbox(store, email=user["email"], kind="physician_rejected")[0]
    assert queued["send_after"], "a rejection must not go out immediately"

    store.record_verification_decision(user["id"], status="approved",
                                       decided_by="admin@x", tier="labeler")
    by_kind = {r["kind"]: r["status"] for r in _outbox(store, email=user["email"])}
    assert by_kind["physician_rejected"] == "void"
    assert by_kind["physician_approved"] == "pending"


def test_neither_builder_uses_a_long_dash():
    """House style, and email bodies are where it slips in."""
    bodies = [
        build_asclepius_approved_email(full_name="A", workspace_url="https://x",
                                       tier_word="Labeler"),
        build_asclepius_rejected_email(full_name="A"),
    ]
    for body in bodies:
        assert not re.search(r"[–—]", body), body[:200]


def test_the_approval_keeps_its_high_importance_when_it_moved_to_the_outbox():
    """The inline send used importance_headers=True. Moving it to a queue that
    did not know about that flag would have quietly dropped it."""
    from notifications import IMPORTANT_KINDS

    assert "physician_approved" in IMPORTANT_KINDS
    main_src = (__import__("pathlib").Path(__file__).resolve().parents[1]
                / "main.py").read_text(encoding="utf-8")
    drain = main_src[main_src.index("async def _drain_admin_notifications"):]
    drain = drain[:drain.index("\n\nasync def")]
    assert "IMPORTANT_KINDS" in drain


def test_the_credentials_welcome_stays_inline_and_is_not_also_queued():
    """A v2 account has no password until approval mints one, and that mail
    carries the secret. It must go from the request that created it, and it must
    not be doubled by a queued notice pointing at a door with no key."""
    import tests._asclepius as _A
    from asclepius import store as _sm

    store = _A.fresh_store()
    admin = _A.make_user(store, role="admin", practice_case=False)
    user = _physician(store)
    with store._conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (_sm.NO_PASSWORD_HASH, user["id"]))
    store.set_verification_status(user["id"], "pending")

    r = client.post(f"/api/asclepius/verify/queue/{user['id']}/approve",
                    json={"tier": "labeler", "note": "ok"}, headers=A.headers_for(admin))
    assert r.status_code == 200
    assert r.json()["credentials_issued"] is True
    assert _outbox(store, email=user["email"]) == [], (
        "the queued notice would be a second mail, and one with no password in it"
    )
    assert not _sm.password_is_unset(store.get_user_by_id(user["id"]))


def test_the_silent_paths_issue_a_credential_before_they_announce_one():
    """Under v2 these two left the physician with no password at all. Telling
    them they are approved without giving them a way in would be the same bug
    wearing an email."""
    import tests._asclepius as _A
    from asclepius import store as _sm

    store = _A.fresh_store()
    admin = _A.make_user(store, role="admin", practice_case=False)
    user = _physician(store, role="admin", specialty="nephrology",
                      board_cert="board_certified_nephrology", years_experience=15)
    with store._conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (_sm.NO_PASSWORD_HASH, user["id"]))

    r = client.post(f"/api/asclepius/admin/physicians/restore?email={user['email']}",
                    json={"approve_verification": True, "tier": "labeler"},
                    headers=A.headers_for(admin))
    assert r.status_code == 200, r.text
    assert not _sm.password_is_unset(store.get_user_by_id(user["id"])), (
        "restore approved them and left them unable to sign in"
    )
    assert len(_outbox(store, email=user["email"], kind="physician_approved")) == 1
