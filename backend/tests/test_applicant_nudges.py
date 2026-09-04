"""The two post-submission nudges: credentials, and the practice case.

An applicant who submitted an application and then stopped is not idle, they
are BLOCKED, and usually on one small thing: nobody uploaded a CV, or the
practice case is still sitting there unopened. Neither is visible to them,
because the dashboard says "in review" either way, and neither is visible to
the founders as anything other than another row that cannot be decided.

So each gap gets exactly ONE email, ever. The properties below are about what
"exactly one, ever" has to survive:

  * a worker that dies between claiming and sending;
  * two sweeps running at once against the same row;
  * a deployment with no mail transport configured;
  * the applicant having already done the thing between the query and the send.

The last one is why the due list is not the whole filter. Whether an applicant
still owes us credentials is a question about a column, an NPI and a JSON blob,
so the sweep asks the same predicate the admin queue asks rather than inventing
a second answer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

import tests._asclepius as A

from asclepius import onboarding_nudge


class _NoInvites:
    """A team store with nothing pre-submit due.

    The sweep covers five kinds across two stores; this file is about the two
    that read accounts, so the invite half is stubbed to empty rather than
    seeded, which keeps a failure here unambiguous about which half broke.
    """

    def list_unfinished_asclepius_invites(self, **kwargs):
        return []

    def stamp_onboarding_nudge(self, *args, **kwargs):
        return False


def _run(coro):
    return asyncio.run(coro)


def _capture(sent):
    async def _send(to, subject, html_body, **kwargs):  # noqa: ANN001
        sent.append({"to": to, "subject": subject, "body": html_body})
        return True
    return _send


@pytest.fixture
def mail(monkeypatch):
    """Capture sends and declare a transport configured."""
    sent = []
    import email_utils
    monkeypatch.setattr(email_utils, "send_html_email", _capture(sent))
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: True)
    return sent


def _applicant(store, *, hours_old=48, creds=None, npi=None, cv=None,
               practice_passed=False, email=None):
    """A submitted, undecided clinical applicant of a given age."""
    user = A.make_user(store, role="evaluator", tier=None, practice_case=practice_passed,
                       specialty="nephrology",
                       email=email or f"applicant-{A.uniq()}@example.com")
    created = (datetime.utcnow() - timedelta(hours=hours_old)).replace(
        microsecond=0).isoformat()
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET verification_status = 'pending', created_at = ?, "
            "credentials_json = ?, npi = ?, cv_asset_sha = ?, full_name = 'Dr Amara Reid' "
            "WHERE id = ?",
            (created, json.dumps(creds or {}), npi, cv, user["id"]))
    return store.get_user_by_id(user["id"])


def _sweep(store):
    return _run(onboarding_nudge.sweep(_NoInvites(), store))


def _subjects(sent, email):
    return [m["subject"] for m in sent if m["to"] == email]


# ─── Who is due ──────────────────────────────────────────────────────────────
def test_an_applicant_with_nothing_to_verify_them_against_is_due_for_both():
    """The 24-hour window exists so a physician who is mid-application is not
    chased mid-sentence. Past it, an application with no evidence and no
    practice case is stuck on two separate things and is asked about both."""
    store = A.fresh_store()
    doc = _applicant(store)

    creds = store.list_applicants_needing_nudge("credentials", 24, 50)
    practice = store.list_applicants_needing_nudge("practice", 24, 50)
    assert doc["id"] in [u["id"] for u in creds]
    assert doc["id"] in [u["id"] for u in practice]


def test_an_application_younger_than_the_window_is_not_due():
    """Chasing somebody an hour after they applied reads as an automated system
    that has not noticed they are still in the room."""
    store = A.fresh_store()
    doc = _applicant(store, hours_old=2)
    due = store.list_applicants_needing_nudge("credentials", 24, 50)
    assert doc["id"] not in [u["id"] for u in due]


def test_a_decided_application_is_never_chased():
    """Approval and rejection both end the conversation. A nudge after a
    decision is the product asking for something it no longer needs."""
    store = A.fresh_store()
    doc = _applicant(store)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (doc["id"],))
    assert store.list_applicants_needing_nudge("credentials", 24, 50) == []


def test_an_applicant_who_already_did_the_thing_gets_no_email(mail):
    """The due list cannot see credential evidence: it lives across a column, an
    NPI and a JSON blob. So the sweep re-asks the question the admin queue asks,
    and a physician who uploaded a CV yesterday hears nothing."""
    store = A.fresh_store()
    done = _applicant(store, cv="sha-of-a-real-cv", practice_passed=True)

    _sweep(store)
    assert _subjects(mail, done["email"]) == []


def test_a_registration_number_counts_as_evidence_for_a_non_us_physician(mail):
    """An NPI is not the only way to be checkable. A physician licensed outside
    the US who gave us their registration number is not missing anything, and
    chasing them for credentials would be the product telling them their
    licence does not count."""
    store = A.fresh_store()
    doc = _applicant(store, creds={"registrationNumber": "GMC-7712345"},
                     practice_passed=True)
    _sweep(store)
    assert _subjects(mail, doc["email"]) == []


# ─── Claim first, once ever ──────────────────────────────────────────────────
def test_each_kind_is_sent_once_and_only_once(mail):
    """The stamp is the entire idempotency mechanism. A second sweep an hour
    later must send this applicant nothing at all."""
    store = A.fresh_store()
    doc = _applicant(store)

    first = _sweep(store)
    assert first["credentials"] == 1 and first["practice"] == 1
    assert sorted(_subjects(mail, doc["email"])) == sorted([
        "One thing missing from your application",
        "Your practice case is waiting",
    ])

    second = _sweep(store)
    assert second["credentials"] == 0 and second["practice"] == 0
    assert len(_subjects(mail, doc["email"])) == 2


def test_the_two_kinds_have_separate_stamps():
    """One column per kind, so an applicant who was chased about credentials is
    still chaseable about the practice case. Sharing a stamp would silently
    swallow whichever nudge lost the race."""
    store = A.fresh_store()
    doc = _applicant(store)

    assert store.stamp_applicant_nudge(doc["id"], "credentials") is True
    assert store.stamp_applicant_nudge(doc["id"], "practice") is True
    assert store.stamp_applicant_nudge(doc["id"], "credentials") is False


def test_a_racing_worker_cannot_claim_a_row_that_is_already_claimed():
    """Two sweeps against one row is the normal case on a restart, not the
    exotic one. The claim is a conditional UPDATE, so sqlite picks exactly one
    winner and the loser sends nothing."""
    store = A.fresh_store()
    doc = _applicant(store)

    claims = [store.stamp_applicant_nudge(doc["id"], "credentials") for _ in range(5)]
    assert claims.count(True) == 1
    assert claims.count(False) == 4


def test_a_claimed_but_unsent_nudge_is_never_retried(mail, monkeypatch):
    """The deliberate trade. A worker that dies after the stamp costs this
    physician one email they never got; doing it the other way round costs them
    a duplicate, and there is no way to take that back."""
    store = A.fresh_store()
    doc = _applicant(store)

    async def _explode(*args, **kwargs):
        raise RuntimeError("transport blew up mid-send")

    import email_utils
    monkeypatch.setattr(email_utils, "send_html_email", _explode)
    _sweep(store)
    assert _subjects(mail, doc["email"]) == []

    # Transport healthy again, and this applicant still hears nothing: the
    # claim committed before the send that failed.
    monkeypatch.setattr(email_utils, "send_html_email", _capture(mail))
    assert _sweep(store) == {"resume": 0, "nudge": 0, "expiry": 0,
                             "credentials": 0, "practice": 0, "profile": 0}
    assert _subjects(mail, doc["email"]) == []


# ─── No transport ────────────────────────────────────────────────────────────
def test_no_mail_transport_stamps_nothing(monkeypatch):
    """A deployment with no mail configured must not burn every applicant's one
    nudge into a column while sending nobody anything. The check is one early
    return in front of the whole sweep, before any claim."""
    store = A.fresh_store()
    doc = _applicant(store)

    import email_utils
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: False)
    assert _sweep(store) == {"resume": 0, "nudge": 0, "expiry": 0,
                             "credentials": 0, "practice": 0, "profile": 0}

    row = store.get_user_by_id(doc["id"])
    assert row["nudge_credentials_sent_at"] is None
    assert row["nudge_practice_sent_at"] is None


# ─── The copy ────────────────────────────────────────────────────────────────
def test_neither_email_carries_a_long_dash_or_a_deadline(mail):
    """House style on the dash. On the deadline: these nudges chase a person
    who is waiting on US to decide, so inventing a countdown for them would be
    the product manufacturing urgency it does not actually have."""
    store = A.fresh_store()
    doc = _applicant(store)
    _sweep(store)

    bodies = [m["body"] for m in mail if m["to"] == doc["email"]]
    assert len(bodies) == 2
    for body in bodies:
        assert "—" not in body and "–" not in body
        for word in ("deadline", "expires", "last chance", "final"):
            assert word not in body.lower()
