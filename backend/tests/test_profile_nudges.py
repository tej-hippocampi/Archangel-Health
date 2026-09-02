"""The profile-completeness nudge: one question, one field, rarely.

A richer profile routes better work and better pay to a physician, so there is
a real reason to ask for the missing pieces. There is also an obvious way to
get it wrong, which is to mail somebody a list of everything they have not
filled in. That is a scorecard of their failures arriving in an inbox that
already has three hundred unread messages, and the second one gets a filter
rule.

So the rules are narrow and they are enforced in the store rather than in the
copy: exactly one question per email, each field asked about at most once ever,
and at most one profile email per physician per thirty days. A complete profile
is asked nothing at all.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import tests._asclepius as A

from asclepius import onboarding_nudge


_COMPLETE = {
    "languages": ["English"],
    "subspecialties": ["Interventional nephrology"],
    "residency": [{"program": "Mass General", "year": 2012}],
    "practiceSettings": ["Academic medical centre"],
    "practiceCity": "Boston",
    "yearsInActivePractice": 11,
}


class _NoInvites:
    """The pre-submit half of the sweep, stubbed empty. This file is about the
    profile section, and a failure here should be unambiguous about that."""

    def list_unfinished_asclepius_invites(self, **kwargs):
        return []

    def stamp_onboarding_nudge(self, *args, **kwargs):
        return False


def _capture(sent):
    async def _send(to, subject, html_body, **kwargs):  # noqa: ANN001
        sent.append({"to": to, "subject": subject, "body": html_body})
        return True
    return _send


@pytest.fixture
def mail(monkeypatch):
    sent = []
    import email_utils
    monkeypatch.setattr(email_utils, "send_html_email", _capture(sent))
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: True)
    return sent


def _physician(store, creds=None, *, avatar=None, niche=None, linkedin=None):
    """An APPROVED physician, because a pending applicant is being chased about
    their application rather than their subspecialties."""
    user = A.make_user(store, role="evaluator", specialty="nephrology",
                       email=f"doc-{A.uniq()}@example.com")
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET verification_status = 'approved', credentials_json = ?, "
            "avatar_asset_sha = ?, specialty_niche = ?, linkedin_url = ?, "
            "full_name = 'Dr Amara Reid' WHERE id = ?",
            (json.dumps(creds or {}), avatar, niche, linkedin, user["id"]))
    return store.get_user_by_id(user["id"])


def _complete_physician(store):
    return _physician(store, _COMPLETE, avatar="sha", niche="CKD",
                      linkedin="https://example.org/in/reid")


def _sweep(store):
    return asyncio.run(onboarding_nudge.sweep(_NoInvites(), store))


def _for(sent, email):
    return [m for m in sent if m["to"] == email]


# ─── One question ────────────────────────────────────────────────────────────
def test_the_email_asks_exactly_one_question(mail):
    """The single-question rule is the whole design. Two questions is a form,
    and a form is what a busy clinician closes."""
    store = A.fresh_store()
    doc = _physician(store, {})
    _sweep(store)

    body = _for(mail, doc["email"])[0]["body"]
    assert body.count("?") == 1


def test_the_email_names_one_field_and_it_is_one_that_is_actually_missing(mail):
    """Asking for something already on file is worse than not asking: it says
    plainly that nobody read the profile before writing."""
    store = A.fresh_store()
    doc = _physician(store, {"languages": ["English", "Urdu"]})
    _sweep(store)

    body = _for(mail, doc["email"])[0]["body"]
    assert "subspecialties" in body
    assert "languages" not in body


def test_the_copy_carries_no_long_dash(mail):
    """House style, and it applies to every word this product mails."""
    store = A.fresh_store()
    doc = _physician(store, {})
    _sweep(store)
    body = _for(mail, doc["email"])[0]["body"]
    assert "—" not in body and "–" not in body


# ─── Once per field, ever ────────────────────────────────────────────────────
def test_a_field_is_asked_about_once_and_then_never_again():
    """A physician who read the question and decided not to answer has
    answered. Asking again a month later is the product refusing to take no."""
    store = A.fresh_store()
    doc = _physician(store, {})

    assert store.stamp_profile_nudge(doc["id"], "languages") is True
    assert store.stamp_profile_nudge(doc["id"], "languages",
                                     min_days_between=0) is False


def test_a_racing_sweep_cannot_ask_the_same_field_twice():
    """Two sweeps against one row is what a restart looks like. The claim is a
    single guarded write, so exactly one of them gets to send."""
    store = A.fresh_store()
    doc = _physician(store, {})

    claims = [store.stamp_profile_nudge(doc["id"], "subspecialties",
                                        min_days_between=0) for _ in range(5)]
    assert claims.count(True) == 1
    assert claims.count(False) == 4


def test_the_sweep_sends_one_physician_one_email_per_pass(mail):
    """Even with six gaps. The sweep picks the first missing field and stops,
    because the alternative is six emails or one list, and both are the failure
    this design exists to avoid."""
    store = A.fresh_store()
    doc = _physician(store, {})

    assert _sweep(store)["profile"] == 1
    assert len(_for(mail, doc["email"])) == 1


# ─── Thirty-day spacing ──────────────────────────────────────────────────────
def test_a_second_gap_is_not_asked_about_inside_thirty_days(mail):
    """A sparse profile must not turn into a nightly reminder that we are
    dissatisfied with somebody. The spacing is what stops the once-per-field
    rule from adding up to a drip campaign."""
    store = A.fresh_store()
    doc = _physician(store, {})

    _sweep(store)
    assert _sweep(store)["profile"] == 0
    assert len(_for(mail, doc["email"])) == 1


def test_the_next_gap_is_asked_about_once_the_window_has_passed(mail):
    """The other half. Spacing delays the next question, it does not cancel
    it, or a profile filled in halfway would stay halfway forever."""
    store = A.fresh_store()
    doc = _physician(store, {})

    _sweep(store)
    first = _for(mail, doc["email"])[0]["body"]

    # Wind the last-sent stamp back past the window. Stored state rather than a
    # clock patch, because the window is read off that state.
    state = store.profile_nudge_state(doc["id"])
    state["last_sent_at"] = "2020-01-01T00:00:00"
    with store._conn() as conn:
        conn.execute("UPDATE users SET profile_nudge_json = ? WHERE id = ?",
                     (json.dumps(state), doc["id"]))

    assert _sweep(store)["profile"] == 1
    second = _for(mail, doc["email"])[1]["body"]
    assert first != second, "the second email asks about a different field"


# ─── Nothing to ask ──────────────────────────────────────────────────────────
def test_a_complete_profile_is_sent_nothing(mail):
    """A meter at 100 percent that still generates mail is a product that has
    stopped reading its own state."""
    store = A.fresh_store()
    doc = _complete_physician(store)

    assert _sweep(store)["profile"] == 0
    assert _for(mail, doc["email"]) == []
    assert store.profile_nudge_state(doc["id"]) == {}


def test_a_pending_applicant_is_not_asked_about_their_profile(mail):
    """They are waiting on a decision. A question about their subspecialties
    while their application sits undecided reads as the wrong conversation."""
    store = A.fresh_store()
    doc = _physician(store, {})
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'pending' WHERE id = ?",
                     (doc["id"],))

    assert _sweep(store)["profile"] == 0
    assert _for(mail, doc["email"]) == []


# ─── No transport ────────────────────────────────────────────────────────────
def test_no_mail_transport_stamps_nothing(monkeypatch):
    """A field stamped as asked without an email having gone is a question this
    physician is now never asked."""
    store = A.fresh_store()
    doc = _physician(store, {})

    import email_utils
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: False)
    assert _sweep(store)["profile"] == 0
    assert store.profile_nudge_state(doc["id"]) == {}
