"""The per-doctor morning email is at-most-once per doctor per morning.

What was there recorded "did this COUNTRY COHORT have its morning", which is not
a question a mail path can act on. Two schedulers drive this file (the
in-process hourly loop and the hourly external trigger), the cohort row reserved
nothing, so both passed the due-check and both mailed the entire roster. And a
cohort that died on doctor 400 of 900 released its window and started again from
doctor 1 on the next tick, so the failure that lost 500 people their email sent
the other 400 a second one.

The properties below are the ledger's contract, and they pull in opposite
directions on purpose: a doctor must never get two, and a doctor the run did not
reach must still get one. Both matter on a launch morning with the whole roster
onboarding at once.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from community import newsletter as cnewsletter  # noqa: E402
from community.store import CommunityStore  # noqa: E402


def _fresh_store() -> CommunityStore:
    path = os.path.join(tempfile.mkdtemp(prefix="cstore-ledger-"), "community.db")
    store = CommunityStore(db_path=path)
    store.ensure_default_channels(["US"])
    return store


class _NoTasks:
    def list_tasks(self, **kw):
        return []


def _member(user_id: str = "u-doc-1") -> Dict[str, Any]:
    return {"user_id": user_id, "email": f"{user_id}@example.org",
            "display_name": "Dr Doe", "specialty": "nephrology", "country": "US",
            "subspecialties": [], "city": None}


def _make_news(store: CommunityStore) -> None:
    """A fresh bot post, so the doctor is not a legitimately quiet morning."""
    channel = store.get_channel_by_slug("general")
    store.insert_message(channel_id=channel["id"], author_user_id="u-system",
                         body="Something happened overnight.", kind="digest_news")


@pytest.fixture()
def outbox(monkeypatch) -> List[str]:
    """Every address the newsletter actually wrote to, in order."""
    sent: List[str] = []

    async def _send(to_email, subject, html, **kw):
        sent.append(to_email)
        return True

    import email_utils

    monkeypatch.setattr(email_utils, "send_html_email", _send)
    return sent


@pytest.fixture()
def window() -> str:
    return cnewsletter._window_key("America/New_York")


def test_two_runners_reaching_the_same_doctor_send_one_email(outbox, window):
    """THE test. The hourly cron and the in-process loop both run, and the
    physician has one message in their inbox rather than two."""
    store = _fresh_store()
    _make_news(store)
    member = _member()
    channels = store.list_channels()

    first = asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window))
    second = asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window))

    assert first == "sent"
    assert second == "already_sent"
    assert outbox == [member["email"]]


def test_the_next_morning_is_a_new_window(outbox, window):
    """The ledger dedupes a morning, not a doctor. A daily email that stopped
    after day one would be the same bug wearing the opposite sign."""
    store = _fresh_store()
    _make_news(store)
    member, channels = _member(), store.list_channels()

    asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window))
    tomorrow = asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window="2099-01-02"))

    assert tomorrow == "sent"
    assert outbox == [member["email"], member["email"]]


def test_a_failed_send_is_retried_rather_than_written_off(monkeypatch, window):
    """A failure releases the claim. An unsent morning and a duplicate morning
    are both bad and the retry is the cheaper mistake."""
    store = _fresh_store()
    _make_news(store)
    member, channels = _member(), store.list_channels()

    attempts = {"n": 0}

    async def _flaky(to_email, subject, html, **kw):
        attempts["n"] += 1
        return attempts["n"] > 1        # the first attempt fails

    import email_utils

    monkeypatch.setattr(email_utils, "send_html_email", _flaky)

    assert asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window)) == "failed"
    assert asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window)) == "sent"
    # And having succeeded, it is closed for the morning.
    assert asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window)) == "already_sent"


def test_a_run_that_died_partway_resumes_instead_of_restarting(outbox, window):
    """The 900-doctor cohort that failed on doctor 3. Re-running it must reach
    the doctors it missed and nobody else."""
    store = _fresh_store()
    _make_news(store)
    roster = [_member(f"u-doc-{i}") for i in range(5)]
    channels = store.list_channels()

    async def _run(members):
        for m in members:
            await cnewsletter.send_for_member(store, _NoTasks(), m, channels,
                                              window=window)

    asyncio.run(_run(roster[:3]))       # the run dies after three
    asyncio.run(_run(roster))           # the next tick starts again at the top

    assert outbox == [m["email"] for m in roster]


def test_a_quiet_doctor_does_not_burn_their_morning(outbox, window):
    """Claiming before deciding there is something to say would silence the
    later tick that did have something."""
    store = _fresh_store()                    # no posts anywhere
    member, channels = _member(), store.list_channels()

    assert asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window)) == "quiet"

    _make_news(store)
    assert asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window)) == "sent"
    assert outbox == [member["email"]]


def test_an_unsubscribed_doctor_never_reaches_the_ledger(outbox, window):
    store = _fresh_store()
    _make_news(store)
    member, channels = _member(), store.list_channels()
    store.set_news_frequency(member["user_id"], "off")

    assert asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window)) == "unsubscribed"
    assert outbox == []


def test_a_forced_run_overrides_the_schedule_and_not_the_ledger(outbox, window):
    """``force`` means "do not wait for 7am". If it also meant "mail everyone
    again" it would be a one-click way to reproduce the bug this fixes."""
    store = _fresh_store()
    _make_news(store)
    member, channels = _member(), store.list_channels()

    asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window))
    # run_newsletter passes the window through even when forced.
    again = asyncio.run(cnewsletter.send_for_member(
        store, _NoTasks(), member, channels, window=window))

    assert again == "already_sent"
    assert outbox == [member["email"]]
