"""New work reaches the physicians who can do it.

``asclepius/task_notify.py`` had no tests at all, which is why the gaps below
survived: the specialty-match recipient query, the outbox drain, and the
community announcement were all untested, so nothing noticed that four of the
six paths that create a task told nobody it existed.

The three properties worth pinning:

1. A promoted real de-identified chart notifies somebody. It is the most
   valuable case the pipeline produces and it was the quietest.
2. Recipient matching survives how specialties are actually written down.
   "renal" and "Nephrology - Transplant" are nephrologists; "cardiothoracic
   surgery" is not a cardiologist.
3. The announcement lands in the specialty's own room, not only in the general
   feed, and never in a room members cannot see.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests._asclepius import fresh_store, make_user

from asclepius import task_notify
from community.store import get_community_store


@pytest.fixture()
def store():
    return fresh_store()


def _neph(store, specialty: str = "nephrology"):
    """An approved evaluator with the given specialty spelling.

    ``create_user`` leaves verification_status NULL, which the recipient query
    treats as included on purpose (pre-verification-era accounts have always
    been able to work), so this is already an eligible physician.
    """
    return make_user(store, role="evaluator", specialty=specialty)


def _seed_channels():
    """The rooms have to exist for the post path to run at all, and today's
    announcements have to be cleared for the dedupe not to swallow the post.

    The suite's community DB lives at a fixed temp path and OUTLIVES the run
    (tests/conftest.py), so an identical announcement body written by an
    earlier run is still inside the one-day dedupe window. That dedupe is
    correct behaviour and is pinned by its own test below; here it just has to
    start from an empty window.
    """
    cstore = get_community_store()
    cstore.ensure_default_channels()
    with cstore._conn() as conn:  # noqa: SLF001 — test-local reset
        conn.execute("DELETE FROM community_messages WHERE kind = 'task_batch'")
    return cstore


# ─── Recipient resolution ────────────────────────────────────────────────────

def test_a_physician_who_wrote_renal_is_a_nephrologist(store):
    """The bare SQL equality this replaced matched nobody, enqueued nothing,
    and the caller swallowed the empty result."""
    _neph(store, "renal")
    found = store.list_evaluators_by_specialty("nephrology")
    assert len(found) == 1


def test_a_subspecialty_suffix_still_matches(store):
    _neph(store, "Nephrology - Transplant")
    assert len(store.list_evaluators_by_specialty("nephrology")) == 1


def test_the_practitioner_noun_matches(store):
    _neph(store, "Nephrologist")
    assert len(store.list_evaluators_by_specialty("nephrology")) == 1


def test_a_cardiothoracic_surgeon_is_not_a_cardiologist(store):
    """A wrong specialty is worse than a missing one: it routes the case to the
    wrong pool and mislabels it in the export, invisibly. Substring matching on
    the "cardio" alias would have caught this surgeon."""
    make_user(store, role="evaluator", specialty="cardiothoracic surgery")
    assert store.list_evaluators_by_specialty("cardiology") == []


def test_an_unrelated_specialty_never_matches(store):
    make_user(store, role="evaluator", specialty="dermatology")
    assert store.list_evaluators_by_specialty("nephrology") == []


def test_a_physician_is_returned_once_not_twice(store):
    """The canonical-term query and the normalize-per-row pass both select the
    same physician when their spelling is already canonical."""
    _neph(store, "nephrology")
    found = store.list_evaluators_by_specialty("nephrology")
    assert len(found) == 1


def test_a_pending_physician_is_not_told_about_work_they_cannot_draw(store):
    u = make_user(store, role="evaluator", specialty="nephrology")
    store.set_verification_status(u["id"], "pending")
    assert store.list_evaluators_by_specialty("nephrology") == []


# ─── The outbox ──────────────────────────────────────────────────────────────

def test_enqueue_writes_one_row_per_matching_physician(store):
    _neph(store)
    _neph(store, "renal")
    make_user(store, role="evaluator", specialty="oncology")
    n = task_notify.enqueue_for_batch(
        store, batch_id=uuid.uuid4().hex,
        created_tasks=[{"task_id": "t1", "specialty": "nephrology"}],
    )
    assert n == 2


def test_the_same_batch_enqueued_twice_does_not_double_mail(store):
    _neph(store)
    batch = uuid.uuid4().hex
    tasks = [{"task_id": "t1", "specialty": "nephrology"}]
    first = task_notify.enqueue_for_batch(store, batch_id=batch, created_tasks=tasks)
    second = task_notify.enqueue_for_batch(store, batch_id=batch, created_tasks=tasks)
    assert first == 1
    assert second == 0


def test_a_batch_that_reaches_nobody_enqueues_nothing_and_does_not_raise(store):
    n = task_notify.enqueue_for_batch(
        store, batch_id=uuid.uuid4().hex,
        created_tasks=[{"task_id": "t1", "specialty": "dermatology"}],
    )
    assert n == 0


def test_the_drain_sends_each_pending_row_once(store, monkeypatch):
    _neph(store)
    task_notify.enqueue_for_batch(
        store, batch_id=uuid.uuid4().hex,
        created_tasks=[{"task_id": "t1", "specialty": "nephrology"}],
    )
    sent = []

    async def _ok(to, subject, body):
        sent.append((to, subject))
        return True, None

    monkeypatch.setattr("email_utils.send_html_email_with_reason", _ok)

    ok_count, failed = task_notify.drain_outbox(store)
    assert (ok_count, failed) == (1, 0)
    assert len(sent) == 1
    # Nothing pending afterwards, so a second tick of the drain loop is a no-op.
    assert task_notify.drain_outbox(store) == (0, 0)


def test_a_transport_failure_leaves_the_row_recorded_not_lost(store, monkeypatch):
    _neph(store)
    task_notify.enqueue_for_batch(
        store, batch_id=uuid.uuid4().hex,
        created_tasks=[{"task_id": "t1", "specialty": "nephrology"}],
    )

    async def _fail(to, subject, body):
        return False, "no transport configured"

    monkeypatch.setattr("email_utils.send_html_email_with_reason", _fail)
    ok_count, failed = task_notify.drain_outbox(store)
    assert (ok_count, failed) == (0, 1)


# ─── The community announcement ──────────────────────────────────────────────

def _post_announcement(store, tasks):
    return asyncio.run(
        task_notify.post_community_announcement(
            store, admin_user_id="u-admin", created_tasks=tasks
        )
    )


def test_the_general_room_always_hears_about_it(store, monkeypatch):
    posted = []

    async def _capture(**kw):
        posted.append(kw)
        return {"id": f"m-{len(posted)}"}

    _seed_channels()
    monkeypatch.setattr("community.system_posts.post_system_message", _capture)
    monkeypatch.setattr(task_notify, "_visible_channel_slugs", lambda: set())

    _post_announcement(store, [{"task_id": "t1", "specialty": "nephrology"}])
    assert [p["channel_slug"] for p in posted] == ["task-announcements"]


def test_the_specialty_room_hears_about_it_too(store, monkeypatch):
    """Nothing in the codebase has ever written into #nephrology. A
    nephrologist looking at their own room saw an empty room while nephrology
    work sat in the queue."""
    posted = []

    async def _capture(**kw):
        posted.append(kw)
        return {"id": f"m-{len(posted)}"}

    _seed_channels()
    monkeypatch.setattr("community.system_posts.post_system_message", _capture)
    monkeypatch.setattr(
        task_notify, "_visible_channel_slugs",
        lambda: {"task-announcements", "nephrology"},
    )

    _post_announcement(store, [{"task_id": "t1", "specialty": "nephrology"}])
    slugs = [p["channel_slug"] for p in posted]
    assert "nephrology" in slugs
    assert "task-announcements" in slugs


def test_only_the_general_post_carries_the_all_member_fan_out(store, monkeypatch):
    """A second copy of the same news in everyone's inbox is how a useful
    announcement becomes noise."""
    posted = []

    async def _capture(**kw):
        posted.append(kw)
        return {"id": f"m-{len(posted)}"}

    _seed_channels()
    monkeypatch.setattr("community.system_posts.post_system_message", _capture)
    monkeypatch.setattr(
        task_notify, "_visible_channel_slugs",
        lambda: {"task-announcements", "nephrology"},
    )

    _post_announcement(store, [{"task_id": "t1", "specialty": "nephrology"}])
    by_slug = {p["channel_slug"]: p for p in posted}
    assert by_slug["task-announcements"]["announce"] is True
    assert by_slug["nephrology"]["announce"] is False


def test_a_hidden_specialty_room_is_never_written_into(store, monkeypatch):
    """Posting would MAKE it visible: visible_channels keeps any channel with
    history, so a below-threshold room would open itself by being announced
    into."""
    posted = []

    async def _capture(**kw):
        posted.append(kw)
        return {"id": f"m-{len(posted)}"}

    _seed_channels()
    monkeypatch.setattr("community.system_posts.post_system_message", _capture)
    monkeypatch.setattr(
        task_notify, "_visible_channel_slugs", lambda: {"task-announcements"}
    )

    _post_announcement(store, [{"task_id": "t1", "specialty": "nephrology"}])
    assert [p["channel_slug"] for p in posted] == ["task-announcements"]


def test_an_unrecognised_specialty_still_gets_the_general_post(store, monkeypatch):
    """No room to route it to is not a reason for the work to be silent."""
    posted = []

    async def _capture(**kw):
        posted.append(kw)
        return {"id": f"m-{len(posted)}"}

    _seed_channels()
    monkeypatch.setattr("community.system_posts.post_system_message", _capture)
    monkeypatch.setattr(
        task_notify, "_visible_channel_slugs", lambda: {"task-announcements", "nephrology"}
    )

    _post_announcement(store, [{"task_id": "t1", "specialty": "dermatology"}])
    assert [p["channel_slug"] for p in posted] == ["task-announcements"]


def test_the_specialty_room_line_counts_only_that_specialty(store, monkeypatch):
    posted = []

    async def _capture(**kw):
        posted.append(kw)
        return {"id": f"m-{len(posted)}"}

    _seed_channels()
    monkeypatch.setattr("community.system_posts.post_system_message", _capture)
    monkeypatch.setattr(
        task_notify, "_visible_channel_slugs",
        lambda: {"task-announcements", "nephrology", "oncology"},
    )

    _post_announcement(store, [
        {"task_id": "t1", "specialty": "nephrology"},
        {"task_id": "t2", "specialty": "nephrology"},
        {"task_id": "t3", "specialty": "oncology"},
    ])
    by_slug = {p["channel_slug"]: p["body"] for p in posted}
    assert "2 new Nephrology tasks" in by_slug["nephrology"]
    assert "1 new Oncology task" in by_slug["oncology"]


def test_the_same_announcement_is_not_repeated_the_same_day(store, monkeypatch):
    """Repeated uploads of one task are one piece of news."""
    posted = []

    async def _capture(**kw):
        posted.append(kw)
        return {"id": f"m-{len(posted)}"}

    cstore = _seed_channels()
    real_post = None

    async def _record_and_capture(**kw):
        posted.append(kw)
        # Write a real row so the dedupe query has something to find.
        channel = cstore.get_channel_by_slug(kw["channel_slug"])
        cstore.insert_message(
            channel_id=channel["id"], author_user_id="u-system",
            body=kw["body"], kind=kw["kind"],
        )
        return {"id": f"m-{len(posted)}"}

    monkeypatch.setattr("community.system_posts.post_system_message", _record_and_capture)
    monkeypatch.setattr(task_notify, "_visible_channel_slugs", lambda: {"task-announcements"})

    tasks = [{"task_id": "t1", "specialty": "nephrology"}]
    assert _post_announcement(store, tasks) is True
    assert _post_announcement(store, tasks) is False
    assert len(posted) == 1


def test_a_community_failure_never_breaks_the_upload(store, monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("community is down")

    _seed_channels()
    monkeypatch.setattr("community.system_posts.post_system_message", _boom)
    assert _post_announcement(store, [{"task_id": "t1", "specialty": "nephrology"}]) is False
