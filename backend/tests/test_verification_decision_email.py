"""Every acceptance path queues the same personal welcome, once per physician.

Legacy passwordless accounts use the existing reset flow. Delivery is guarded
by current acceptance, active status and physician role, independent of tier.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import tests._asclepius as A
from onboarding_emails import (
    application_welcome_subject,
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
    """Automatic approval and restoration use the same durable welcome queue."""
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
    assert rows[0]["subject"] == application_welcome_subject(user.get("full_name") or "")


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
def test_labelers_and_reviewers_receive_the_same_personal_welcome():
    kwargs = dict(full_name="Ada Reyes", workspace_url="https://x/asclepius")
    reviewer = build_asclepius_approved_email(**kwargs, tier_word="Reviewer", can_review=True)
    labeler = build_asclepius_approved_email(**kwargs, tier_word="Labeler", can_review=False)
    assert reviewer == labeler
    assert "Welcome, Ada." in reviewer
    assert "That’s you." in reviewer


def test_an_approval_with_no_tier_omits_the_paragraph_rather_than_saying_unassigned():
    """restore_physician can approve carrying no tier, and a placeholder in a
    congratulations email is worse than silence about it."""
    body = build_asclepius_approved_email(
        full_name="Ada Reyes", workspace_url="https://x/asclepius", tier_word="")
    assert "Unassigned" not in body
    assert "approved as a" not in body
    assert "You’re approved." in body


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
    assert rows[0]["subject"] == "About your Archangel Health application"
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


def test_a_passwordless_physician_gets_one_queued_setup_welcome():
    """Legacy accounts receive setup instructions without minting a lost secret."""
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
    assert r.json()["credentials_issued"] is False
    assert r.json()["welcome_email_queued"] is True
    rows = _outbox(store, email=user["email"], kind="physician_approved")
    assert len(rows) == 1 and "Forgot your password?" in rows[0]["body_html"]
    assert _sm.password_is_unset(store.get_user_by_id(user["id"]))


def test_restore_provides_setup_instructions_without_discarding_a_password():
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
    assert _sm.password_is_unset(store.get_user_by_id(user["id"]))
    rows = _outbox(store, email=user["email"], kind="physician_approved")
    assert len(rows) == 1 and "Forgot your password?" in rows[0]["body_html"]
