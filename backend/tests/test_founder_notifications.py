"""Founder alerts: the events we listen for, and the ones we do not.

The first test here is the important one and it is why this file exists. While
writing the feature two of the nine event names in the dispatch table did not
exist anywhere in the codebase (`submission_created`, `qa_decision`). Nothing
failed. No test broke, no log line appeared, the outbox simply stayed empty and
the feature would have shipped looking finished while silently notifying nobody
about two of the things it claimed to cover. A dispatch table keyed on strings
that another file emits needs the join checked mechanically, or it rots the
moment somebody renames an event.
"""
from __future__ import annotations

import html
import re
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

import notifications as N  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    monkeypatch.setenv("FOUNDER_NOTIFY_EMAILS", "arya@example.com,tej@example.com")
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _outbox(store, kind=None):
    with store._conn() as conn:
        if kind:
            rows = conn.execute(
                "SELECT * FROM admin_notify_outbox WHERE kind = ?", (kind,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM admin_notify_outbox").fetchall()
    return [dict(r) for r in rows]


# ─── The join between this table and the rest of the codebase ────────────────

def test_every_event_we_listen_for_is_one_the_codebase_actually_emits():
    """A name in the dispatch table that nothing emits is a notification that
    never fires and never complains. Two of them shipped in the first draft of
    this feature; this is what caught them."""
    emitted = set()
    for path in list((BACKEND / "routers").glob("*.py")) + \
            list((BACKEND / "asclepius").glob("*.py")) + \
            list((BACKEND / "asclepius").glob("**/*.py")):
        emitted |= set(re.findall(r'event_type="([a-z_]+)"',
                                  path.read_text(encoding="utf-8")))
    missing = sorted(N.notable_event_types() - emitted)
    assert not missing, (
        "these event types are listened for but never emitted, so the alert "
        f"can never fire: {missing}")


def test_the_dispatch_table_is_well_formed():
    for event_type, spec in N._EVENT_ALERTS.items():
        assert set(spec) == {"kind", "eyebrow", "headline", "lede"}, event_type
        # The lede is formatted with exactly these two names; anything else
        # raises KeyError inside a swallowing except and vanishes.
        placeholders = set(re.findall(r"\{(\w+)\}", spec["lede"]))
        assert placeholders <= {"who", "what"}, (event_type, placeholders)


# ─── Behaviour ───────────────────────────────────────────────────────────────

def test_a_notable_event_queues_one_row_per_recipient():
    store = _store()
    store.log_event(entity_type="health_system", entity_id="hs-x",
                    event_type="self_signup_verified", actor="stmarys",
                    payload={"organization": "St Mary's Health"})
    rows = _outbox(store, "hs_signup")
    assert sorted(r["recipient_email"] for r in rows) == ["arya@example.com", "tej@example.com"]
    # Escaped, because the builder escapes: asserting the raw apostrophe would
    # be asserting that we do not escape, which is the opposite of what we want.
    assert html.escape("St Mary's Health") in rows[0]["body_html"]
    assert rows[0]["status"] == "pending"


def test_an_uninteresting_event_queues_nothing():
    """Most of the ~150 event types are ours to read in a log, not to be told
    about. A feature that emails on all of them gets filtered within a week."""
    store = _store()
    for event_type in ("login_succeeded", "avatar_set", "raw_purged", "promote_dry_run"):
        store.log_event(entity_type="user", entity_id="u1", event_type=event_type)
    assert _outbox(store) == []


def test_the_same_event_twice_queues_once():
    """A retried request or a double-submit is one thing happening, not two."""
    store = _store()
    for _ in range(3):
        store.log_event(entity_type="health_system", entity_id="hs-y",
                        event_type="intake_submitted", actor="stmarys")
    assert len(_outbox(store, "hs_intake")) == 2  # one per recipient, not six


def test_work_events_coalesce_into_a_window_and_urgent_ones_do_not():
    """Twelve cases in an evening is one email, not twelve. A signup is someone
    waiting on a reply, so it goes now."""
    store = _store()
    for i in range(12):
        store.log_event(entity_type="submission", entity_id=f"sub-{i}",
                        event_type="submission_completed", actor="dr-chen",
                        payload={"task_id": f"t-{i}"})
    work = _outbox(store, "case_submitted")
    # Same actor, same window: one notification per recipient for the batch.
    assert len(work) == 2, f"expected one per recipient, got {len(work)}"
    assert all(r["send_after"] for r in work), "work alerts should wait for the window"

    store.log_event(entity_type="health_system", entity_id="hs-z",
                    event_type="self_signup_verified", actor="mercy")
    urgent = _outbox(store, "hs_signup")
    assert all(r["send_after"] is None for r in urgent), "a signup should not wait"


def test_a_rolled_up_alert_says_how_many_rather_than_describing_the_first():
    """The rollup collapses twelve events into one queued row. Without the
    amend, that row still reads "a case was submitted", which is true of the
    first one and a lie about the batch."""
    store = _store()
    for i in range(5):
        store.log_event(entity_type="submission", entity_id=f"sub-{i}",
                        event_type="submission_completed", actor="dr-chen")
    row = _outbox(store, "case_submitted")[0]
    assert "5 cases were submitted" in row["subject"], row["subject"]
    assert "5 cases were submitted" in row["body_html"]
    # ...and the singular wording is gone, not merely appended to.
    assert "A case was submitted" not in row["subject"]


def test_two_different_physicians_are_not_coalesced_together():
    """The window groups a person's own burst. Two people working at once are
    two things worth knowing."""
    store = _store()
    for actor in ("dr-chen", "dr-okafor"):
        store.log_event(entity_type="submission", entity_id="sub-" + actor,
                        event_type="submission_completed", actor=actor)
    assert len(_outbox(store, "case_submitted")) == 4  # 2 physicians x 2 recipients


# ─── Safety ──────────────────────────────────────────────────────────────────

def test_a_broken_notification_never_breaks_the_write_that_triggered_it(monkeypatch):
    """A physician's case submission must not 500 because our mail queue had an
    opinion. The event itself still has to land."""
    def _explode(*a, **kw):
        raise RuntimeError("outbox is on fire")

    store = _store()
    monkeypatch.setattr(store, "enqueue_admin_notification", _explode)
    store.log_event(entity_type="submission", entity_id="sub-1",
                    event_type="submission_completed", actor="dr-chen")
    with store._conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM events WHERE entity_id = 'sub-1'").fetchone()[0]
    assert n == 1, "the event was lost when the notification failed"


def test_long_payload_values_are_left_out_of_the_body():
    """SendGrid is not PHI-eligible unless a BAA is signed, and an alert
    addressed to us is exactly the kind that invites detail. Short scalars are
    ids and counts; a long string in a payload is where case text would arrive."""
    store = _store()
    note = "PATIENT NARRATIVE " + ("x" * 400)
    store.log_event(entity_type="submission", entity_id="sub-2",
                    event_type="submission_completed", actor="dr-chen",
                    payload={"task_id": "t-9", "note": note})
    body = _outbox(store, "case_submitted")[0]["body_html"]
    assert "t-9" in body
    assert "PATIENT NARRATIVE" not in body
    assert "xxxx" not in body


def test_recipients_fall_back_rather_than_silently_going_nowhere(monkeypatch):
    """A notification feature that no-ops because one env var is unset is worse
    than not having one, because you believe it is working."""
    for var in ("FOUNDER_NOTIFY_EMAILS", "ASCLEPIUS_ADMIN_NOTIFY_EMAILS",
                "ASCLEPIUS_ADMIN_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    store = _store()
    admin = A.make_user(store, role="admin", email="onlyadmin@example.com")
    assert N.founder_recipients(store) == ["onlyadmin@example.com"]
    # ...and with no admin either, the built-in default rather than nothing.
    store.set_user_active(admin["id"], False) if hasattr(store, "set_user_active") else None
    assert N.founder_recipients(None), "no recipients at all"


def test_the_explicit_list_wins_over_everything(monkeypatch):
    monkeypatch.setenv("FOUNDER_NOTIFY_EMAILS", "just.me@example.com")
    monkeypatch.setenv("ASCLEPIUS_ADMIN_EMAIL", "someone.else@example.com")
    assert N.founder_recipients(_store()) == ["just.me@example.com"]
