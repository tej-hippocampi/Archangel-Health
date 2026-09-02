"""Promotion, and the queue knowing who is actually ready to look at.

Two gaps the meeting named. A physician's tier was decided once, at approval,
from credentials, before anybody had seen a case they filed; the contributor
score then moved with their work and changed nothing. And the review queue could
not answer "who has finished their half of this", so an admin skimmed rows that
were not ready yet.

What is pinned here is mostly what the promotion endpoint REFUSES to do. The
easy version of this feature promotes on a threshold, which turns the score into
a target the moment anybody learns it exists, and lets a work record buy past a
credential gate. Neither is acceptable, so both have tests.
"""

from __future__ import annotations


from fastapi.testclient import TestClient

import tests._asclepius as A

from asclepius import capabilities as caps

client = TestClient(A.app)

_RETIER = "/api/asclepius/verify/retier/{}"
_CANDIDATES = "/api/asclepius/verify/retier-candidates"
_QUEUE = "/api/asclepius/verify/queue"


def _approved(store, tier=caps.LABELER, **kw):
    u = A.make_user(store, role="evaluator", specialty="nephrology", **kw)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved', tier = ? "
                     "WHERE id = ?", (tier, u["id"]))
    return store.get_user_by_id(u["id"])


def _admin(store):
    return A.make_user(store, role="admin")


# ─── What promotion refuses ──────────────────────────────────────────────────
def test_a_tier_change_requires_a_reason():
    """In six months the only person who can explain a role change is whoever
    wrote down why at the time."""
    store = A.fresh_store()
    doc = _approved(store)
    r = client.post(_RETIER.format(doc["id"]),
                    json={"tier": caps.REVIEWER, "note": "   "},
                    headers=A.headers_for(_admin(store)))
    assert r.status_code == 400


def test_an_undecided_account_cannot_be_tiered_here():
    """A tier is one of the outputs of the approval decision. Letting this
    endpoint write one onto a pending account would be a second way to approve
    somebody, skipping every side effect approval owns."""
    store = A.fresh_store()
    pending = A.make_user(store, role="evaluator")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'pending', tier = NULL "
                     "WHERE id = ?", (pending["id"],))
    r = client.post(_RETIER.format(pending["id"]),
                    json={"tier": caps.REVIEWER, "note": "strong work"},
                    headers=A.headers_for(_admin(store)))
    assert r.status_code == 422


def test_an_unknown_tier_is_refused():
    store = A.fresh_store()
    doc = _approved(store)
    r = client.post(_RETIER.format(doc["id"]),
                    json={"tier": "chief", "note": "invented rung"},
                    headers=A.headers_for(_admin(store)))
    assert r.status_code == 400


def test_it_is_admin_only():
    store = A.fresh_store()
    doc = _approved(store)
    r = client.post(_RETIER.format(doc["id"]),
                    json={"tier": caps.REVIEWER, "note": "promoting myself"},
                    headers=A.headers_for(doc))
    assert r.status_code == 403


# ─── What promotion does ─────────────────────────────────────────────────────
def test_promotion_moves_the_tier_and_records_the_evidence_as_it_was():
    """The score is stamped at decision time on purpose. Recomputing it later
    answers a different question than the one the admin acted on."""
    store = A.fresh_store()
    doc = _approved(store)
    r = client.post(_RETIER.format(doc["id"]),
                    json={"tier": caps.REVIEWER, "note": "40 cases, consistently sound"},
                    headers=A.headers_for(_admin(store)))
    assert r.status_code == 200, r.text
    assert r.json()["promoted"] is True
    assert store.get_user_by_id(doc["id"])["tier"] == caps.REVIEWER

    events = [e for e in store.list_events(entity_type="user", entity_id=doc["id"])
              if e.get("event_type") == "tier_changed"]
    assert len(events) == 1
    payload = events[0].get("payload") or {}
    assert payload.get("from") == caps.LABELER and payload.get("to") == caps.REVIEWER
    assert payload.get("note")
    assert "score_at_decision" in payload and "n_cases_at_decision" in payload


def test_re_applying_the_same_tier_is_a_no_op_rather_than_an_event():
    """A double-clicked button should not write a second history entry saying
    somebody was promoted twice."""
    store = A.fresh_store()
    doc = _approved(store, tier=caps.REVIEWER)
    r = client.post(_RETIER.format(doc["id"]),
                    json={"tier": caps.REVIEWER, "note": "no change"},
                    headers=A.headers_for(_admin(store)))
    assert r.status_code == 200 and r.json().get("unchanged") is True
    events = [e for e in store.list_events(entity_type="user", entity_id=doc["id"])
              if e.get("event_type") == "tier_changed"]
    assert events == []


def test_a_demotion_is_allowed_and_sends_nothing():
    """Narrowing what an account can do must never be blocked. And the reasons
    are specific to the work, so they belong in a conversation rather than in a
    template that reports a drop in standing to somebody with nobody to ask."""
    store = A.fresh_store()
    doc = _approved(store, tier=caps.REVIEWER)
    r = client.post(_RETIER.format(doc["id"]),
                    json={"tier": caps.LABELER, "note": "agreement slipped, discussed on call"},
                    headers=A.headers_for(_admin(store)))
    assert r.status_code == 200, r.text
    assert r.json()["promoted"] is False
    assert r.json()["email_sent"] is False
    assert store.get_user_by_id(doc["id"])["tier"] == caps.LABELER


# ─── Candidates are a list, not a trigger ────────────────────────────────────
def test_candidates_never_promote_anybody():
    """A score crossing a band is a reason to look, not a reason to act.
    Automating it makes the number a target the moment anyone learns it
    exists."""
    store = A.fresh_store()
    doc = _approved(store)
    r = client.get(_CANDIDATES, headers=A.headers_for(_admin(store)))
    assert r.status_code == 200, r.text
    assert store.get_user_by_id(doc["id"])["tier"] == caps.LABELER
    assert "criteria" in r.json()


def test_a_labeler_with_too_few_cases_is_not_a_candidate():
    """A blended score over three cases is mostly the credential prior wearing
    a number."""
    store = A.fresh_store()
    doc = _approved(store)
    store.upsert_contributor_score(user_id=doc["id"], score=91.0, n_cases=2, components={})
    listed = client.get(_CANDIDATES, headers=A.headers_for(_admin(store))).json()["candidates"]
    assert doc["id"] not in [c["user_id"] for c in listed]


def test_a_labeler_with_the_record_for_it_is_listed():
    """The guard on the two tests above: without this, an endpoint that always
    returned an empty list would satisfy both of them."""
    store = A.fresh_store()
    doc = _approved(store)
    store.upsert_contributor_score(user_id=doc["id"], score=88.0, n_cases=31, components={})
    listed = client.get(_CANDIDATES, headers=A.headers_for(_admin(store))).json()["candidates"]
    mine = [c for c in listed if c["user_id"] == doc["id"]]
    assert mine, "a labeler over both thresholds should surface for review"
    assert mine[0]["n_cases"] == 31 and mine[0]["score"] == 88.0


def test_candidates_is_admin_only():
    store = A.fresh_store()
    doc = _approved(store)
    assert client.get(_CANDIDATES, headers=A.headers_for(doc)).status_code == 403


# ─── The queue's readiness filter ────────────────────────────────────────────
def test_the_queue_reports_the_practice_case_on_the_row():
    """Half of what an applicant owes us, so "who is ready to look at" has to
    be answerable while skimming rather than one click deep."""
    store = A.fresh_store()
    applicant = A.make_user(store, role="evaluator")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'pending' WHERE id = ?",
                     (applicant["id"],))
    rows = client.get(_QUEUE, headers=A.headers_for(_admin(store))).json()["queue"]
    mine = [r for r in rows if r["user_id"] == applicant["id"]]
    assert mine and "practice_case" in mine[0]
    assert "ready_for_review" in mine[0]


def test_the_ready_filter_says_what_it_is_hiding():
    """A filter that silently shrinks a queue is how somebody waits a week
    because nobody noticed the count had changed."""
    store = A.fresh_store()
    applicant = A.make_user(store, role="evaluator")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'pending' WHERE id = ?",
                     (applicant["id"],))
    body = client.get(_QUEUE, params={"ready": "true"},
                      headers=A.headers_for(_admin(store))).json()
    assert body["ready"] is True
    assert body["total_unfiltered"] >= body["total"]
