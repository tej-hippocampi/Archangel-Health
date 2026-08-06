"""Specialty-tagged task-notification fan-out: recipient match, idempotency,
and the at-scale delivery guarantees (atomic claim, retry-with-cap + dead-letter,
crash/lease recovery, drain-until-empty).

These exercise the store + ``asclepius.task_notify`` directly (no HTTP), with the
email transport monkeypatched so we assert on delivery outcomes deterministically.
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from asclepius import task_notify  # noqa: E402


# ─── helpers ────────────────────────────────────────────────────────────────
def _task(store, specialty="nephrology"):
    return store.insert_task(
        prompt=f"case {uuid.uuid4().hex[:6]}", specialty=specialty,
        difficulty="medium", source="synthetic", created_by="system:test",
        candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    )


def _evaluator(store, specialty="nephrology"):
    return A.make_user(store, role="evaluator", specialty=specialty)


def _set_active(store, user_id, active):
    with sqlite3.connect(store.db_path) as c:
        c.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))


def _rows(store):
    with sqlite3.connect(store.db_path) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM task_notify_outbox ORDER BY id").fetchall()]


def _expire_lease(store):
    """Backdate claimed_at so lease-gated rows (failed retry / stale sending)
    become reclaimable now — the deterministic stand-in for wall-clock passing."""
    old = (datetime.utcnow() - timedelta(hours=1)).replace(microsecond=0).isoformat()
    with sqlite3.connect(store.db_path) as c:
        c.execute("UPDATE task_notify_outbox SET claimed_at = ? WHERE claimed_at IS NOT NULL", (old,))


def _patch_email(monkeypatch, results):
    """Patch the transport. ``results`` is either a fixed (ok, reason) tuple or a
    callable(email)->(ok, reason). Records every recipient emailed."""
    sent_to = []

    async def _stub(to_email, subject, html_body, **kw):
        sent_to.append(to_email)
        out = results(to_email) if callable(results) else results
        return out

    monkeypatch.setattr("email_utils.send_html_email_with_reason", _stub)
    return sent_to


# ─── recipient match ─────────────────────────────────────────────────────────
def test_match_only_active_evaluators_of_that_specialty(monkeypatch):
    store = A.fresh_store()
    nephro = _evaluator(store, "nephrology")
    _evaluator(store, "cardiology")                 # wrong specialty
    A.make_user(store, role="admin", specialty="nephrology")  # wrong role
    inactive = _evaluator(store, "nephrology")
    _set_active(store, inactive["id"], False)        # right specialty, inactive

    matched = {u["id"] for u in store.list_evaluators_by_specialty("nephrology")}
    assert matched == {nephro["id"]}


def test_match_is_case_and_space_insensitive():
    store = A.fresh_store()
    e = _evaluator(store, "Nephrology")
    assert {u["id"] for u in store.list_evaluators_by_specialty("  nephrology ")} == {e["id"]}


# ─── enqueue: counts + idempotency + batch dedup ─────────────────────────────
def test_enqueue_counts_per_specialty_and_is_idempotent():
    store = A.fresh_store()
    n1, n2 = _evaluator(store, "nephrology"), _evaluator(store, "nephrology")
    c1 = _evaluator(store, "cardiology")
    created = [_task(store, "nephrology") for _ in range(3)] + [_task(store, "cardiology")]
    batch = uuid.uuid4().hex

    n = task_notify.enqueue_for_batch(store, batch_id=batch, created_tasks=created)
    assert n == 3  # 2 nephro evaluators + 1 cardio evaluator

    rows = _rows(store)
    by_email = {r["recipient_email"]: r for r in rows}
    assert by_email[n1["email"]]["task_count"] == 3
    assert by_email[n2["email"]]["task_count"] == 3
    assert by_email[c1["email"]]["task_count"] == 1

    # Re-running the SAME batch enqueues nothing new (idempotency key holds).
    assert task_notify.enqueue_for_batch(store, batch_id=batch, created_tasks=created) == 0
    assert len(_rows(store)) == 3


# ─── drain: happy path ───────────────────────────────────────────────────────
def test_drain_sends_each_recipient_once(monkeypatch):
    store = A.fresh_store()
    _evaluator(store, "nephrology"); _evaluator(store, "nephrology")
    created = [_task(store, "nephrology") for _ in range(2)]
    task_notify.enqueue_for_batch(store, batch_id=uuid.uuid4().hex, created_tasks=created)

    sent_to = _patch_email(monkeypatch, (True, "sent"))
    sent, failed = task_notify.drain_outbox(store)
    assert (sent, failed) == (2, 0)
    assert len(sent_to) == 2
    assert all(r["status"] == "sent" for r in _rows(store))
    # A second drain has nothing to claim.
    assert task_notify.drain_outbox(store) == (0, 0)


# ─── drain: retry then dead-letter ───────────────────────────────────────────
def test_transient_failure_is_retried_then_succeeds(monkeypatch):
    store = A.fresh_store()
    _evaluator(store, "nephrology")
    task_notify.enqueue_for_batch(
        store, batch_id=uuid.uuid4().hex, created_tasks=[_task(store, "nephrology")])

    _patch_email(monkeypatch, (False, "boom"))
    assert task_notify.drain_outbox(store) == (0, 1)
    assert _rows(store)[0]["status"] == "failed"
    assert _rows(store)[0]["send_attempts"] == 1

    # Backoff: an immediate re-drain does NOT re-attempt (lease not elapsed) —
    # this is what stops one drain from burning all attempts on a transient blip.
    assert task_notify.drain_outbox(store) == (0, 0)

    # After the lease elapses, a later drain re-claims and it now succeeds.
    _expire_lease(store)
    _patch_email(monkeypatch, (True, "sent"))
    assert task_notify.drain_outbox(store) == (1, 0)
    assert _rows(store)[0]["status"] == "sent"


def test_persistent_failure_dead_letters_at_cap(monkeypatch):
    store = A.fresh_store()
    _evaluator(store, "nephrology")
    task_notify.enqueue_for_batch(
        store, batch_id=uuid.uuid4().hex, created_tasks=[_task(store, "nephrology")])

    _patch_email(monkeypatch, (False, "boom"))
    # MAX_ATTEMPTS default 5: each drain makes one attempt, then the lease must
    # elapse before the next retry — so expire it between drains.
    for _ in range(5):
        task_notify.drain_outbox(store)
        _expire_lease(store)
    row = _rows(store)[0]
    assert row["send_attempts"] == 5
    assert row["status"] == "failed"
    # Past the cap it is a terminal dead-letter — no further drain re-claims it,
    # even once the lease has elapsed.
    assert task_notify.drain_outbox(store) == (0, 0)
    assert store.count_task_notifications_by_status().get("dead_letter") == 1


# ─── drain-until-empty past the page limit ───────────────────────────────────
def test_drain_processes_all_rows_beyond_one_page(monkeypatch):
    monkeypatch.setenv("TASK_NOTIFY_DRAIN_BATCH", "2")  # tiny page → forces looping
    store = A.fresh_store()
    for _ in range(5):
        _evaluator(store, "nephrology")
    task_notify.enqueue_for_batch(
        store, batch_id=uuid.uuid4().hex, created_tasks=[_task(store, "nephrology")])

    sent_to = _patch_email(monkeypatch, (True, "sent"))
    sent, failed = task_notify.drain_outbox(store)
    assert (sent, failed) == (5, 0)
    assert len(sent_to) == 5
    assert all(r["status"] == "sent" for r in _rows(store))


# ─── concurrency: two drains never claim the same row ────────────────────────
def test_concurrent_claims_are_disjoint():
    store = A.fresh_store()
    for _ in range(4):
        _evaluator(store, "nephrology")
    task_notify.enqueue_for_batch(
        store, batch_id=uuid.uuid4().hex, created_tasks=[_task(store, "nephrology")])

    first = store.claim_task_notifications(limit=100)
    second = store.claim_task_notifications(limit=100)  # lease still valid → nothing left
    assert len(first) == 4
    assert second == []
    assert not ({r["id"] for r in first} & {r["id"] for r in second})


# ─── crash recovery: stale 'sending' rows are reclaimed after the lease ──────
def test_stale_sending_rows_are_reclaimed_after_lease():
    store = A.fresh_store()
    _evaluator(store, "nephrology")
    task_notify.enqueue_for_batch(
        store, batch_id=uuid.uuid4().hex, created_tasks=[_task(store, "nephrology")])

    claimed = store.claim_task_notifications(limit=100)
    assert len(claimed) == 1  # now 'sending' (simulating a drain that then crashed)

    # Not yet expired → not reclaimable.
    assert store.claim_task_notifications(limit=100, lease_seconds=300) == []

    # Backdate the lease as if the claiming drain died 10 minutes ago.
    with sqlite3.connect(store.db_path) as c:
        old = (datetime.utcnow() - timedelta(minutes=10)).replace(microsecond=0).isoformat()
        c.execute("UPDATE task_notify_outbox SET claimed_at = ?", (old,))

    reclaimed = store.claim_task_notifications(limit=100, lease_seconds=300)
    assert len(reclaimed) == 1
    assert reclaimed[0]["send_attempts"] == 2  # bumped on each claim
