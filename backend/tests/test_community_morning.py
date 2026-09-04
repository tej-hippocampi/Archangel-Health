"""The morning routine.

Every day, before a doctor's first coffee, each room they belong to should have
something in it that was not there yesterday. What these tests hold is the
handful of properties that decide whether that reads as useful or as spam:

  * It fires at 7am WHERE THE DOCTOR IS. A "morning brief" arriving at four in
    the afternoon is a notification.
  * It posts once a day however many times it is called. The trigger runs
    hourly and an admin can fire it by hand, so idempotence cannot rest on
    nobody pressing the button twice.
  * A quiet day is a valid day. Three stale conferences every morning teaches
    people to stop looking.
  * Nothing invented. A model with a search tool will happily produce a
    plausible conference at a plausible URL that does not exist, and that
    costs a doctor their attention and their trust.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List

import asyncio

import pytest

from community import morning, websearch
from community.store import CommunityStore


def _fresh_store() -> CommunityStore:
    path = os.path.join(tempfile.mkdtemp(prefix="cstore-morning-"), "community.db")
    return CommunityStore(db_path=path)


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    monkeypatch.setenv("COMMUNITY_MORNING_HOUR_LOCAL", "7")
    monkeypatch.setenv("ARCHANGEL_HOME_TZ", "America/New_York")
    # 0 disables the per-provider daily call cap, and this file needs it off.
    # The budget ledger is DURABLE and the suite's community DB sits at a fixed
    # /tmp path that outlives the run, so after roughly forty sourcing calls
    # across a day of test runs every search here starts returning [] for a
    # reason that has nothing to do with what is being tested. The cap itself
    # has its own coverage; this file is about the morning.
    monkeypatch.setenv("COMMUNITY_SEARCH_DAILY_CALL_CAP", "0")


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ─── The routine is on unless somebody turns it off ──────────────────────────
def test_the_morning_routine_runs_without_anybody_setting_a_variable(monkeypatch):
    """It shipped off by default, which is how a complete content routine sat
    dormant in production for weeks looking exactly like a quiet community. The
    working state is now the default."""
    monkeypatch.delenv("COMMUNITY_MORNING_ENABLED", raising=False)
    assert morning.enabled() is True


def test_a_blank_gate_variable_reads_as_unset_rather_than_as_off(monkeypatch):
    """Matches the .env loader's own rule: an empty value in a file never wipes
    a value already set, so an empty value must not be a silent kill switch
    either."""
    monkeypatch.setenv("COMMUNITY_MORNING_ENABLED", "   ")
    assert morning.enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_an_operator_can_still_stop_the_routine_without_a_deploy(monkeypatch, value):
    """The variable is the kill switch now, and it has to keep working: the
    whole reason the default flipped is that nobody could tell the routine was
    off, not that nobody should ever be able to turn it off."""
    monkeypatch.setenv("COMMUNITY_MORNING_ENABLED", value)
    assert morning.enabled() is False


def test_the_news_digest_gate_reads_the_same_way(monkeypatch):
    """Two gates, one rule. A second definition of "off" is a second thing to
    get wrong."""
    from community import digest as cdigest

    monkeypatch.delenv("COMMUNITY_NEWS_ENABLED", raising=False)
    assert cdigest.news_enabled() is True
    monkeypatch.setenv("COMMUNITY_NEWS_ENABLED", "0")
    assert cdigest.news_enabled() is False


# ─── The discussion prompt is a daily question, not a weekly one ─────────────
def test_the_discussion_prompt_fires_every_morning_by_default(monkeypatch):
    """A room asked a question once a week is a room people open once a week.
    ``None`` is what ``is_due`` already reads as "no weekday restriction"."""
    monkeypatch.delenv("COMMUNITY_DISCUSSION_DOW", raising=False)
    assert morning.discussion_dow() is None
    # Wednesday and Thursday, and it is due on both.
    assert morning.is_due(None, "America/New_York",
                          now=_utc(2026, 3, 11, 20), dow=morning.discussion_dow()) is True
    assert morning.is_due(None, "America/New_York",
                          now=_utc(2026, 3, 12, 20), dow=morning.discussion_dow()) is True


def test_the_discussion_prompt_can_still_be_pinned_to_one_weekday(monkeypatch):
    monkeypatch.setenv("COMMUNITY_DISCUSSION_DOW", "2")   # Wednesday
    assert morning.discussion_dow() == 2
    assert morning.is_due(None, "America/New_York",
                          now=_utc(2026, 3, 11, 20), dow=2) is True
    assert morning.is_due(None, "America/New_York",
                          now=_utc(2026, 3, 12, 20), dow=2) is False


def test_a_typo_in_the_weekday_means_every_day_not_no_days(monkeypatch):
    """The failure that matters here is a variable that quietly mutes the room
    six days out of seven, so an unparseable value falls back to the loud
    side."""
    monkeypatch.setenv("COMMUNITY_DISCUSSION_DOW", "wednesday")
    assert morning.discussion_dow() is None


# ─── Due-ness: 7am where the doctor is ───────────────────────────────────────
def test_a_scope_is_not_due_before_its_local_morning():
    # Kolkata is UTC+5:30, so its 7am is 01:30 UTC. At 01:00 UTC it is 06:30
    # there and the brief has no business arriving yet.
    assert morning.is_due(None, "Asia/Kolkata", now=_utc(2026, 3, 10, 1)) is False


def test_a_scope_is_due_once_its_local_morning_has_passed():
    # 02:00 UTC is 07:30 in Kolkata.
    assert morning.is_due(None, "Asia/Kolkata", now=_utc(2026, 3, 10, 2)) is True


def test_two_countries_fire_at_different_moments():
    """The whole reason each country carries a timezone: 05:00 UTC is 08:00 in
    Riyadh and midnight in New York."""
    at = _utc(2026, 3, 10, 5)
    assert morning.is_due(None, "Asia/Riyadh", now=at) is True
    assert morning.is_due(None, "America/New_York", now=at) is False


def test_a_scope_that_already_ran_today_is_not_due_again():
    """The trigger fires hourly. Without this it would post twelve times."""
    ran = "2026-03-10T02:05:00"          # just after Kolkata's 7am
    assert morning.is_due(ran, "Asia/Kolkata", now=_utc(2026, 3, 10, 6)) is False


def test_yesterdays_run_does_not_satisfy_today():
    ran = "2026-03-09T02:05:00"
    assert morning.is_due(ran, "Asia/Kolkata", now=_utc(2026, 3, 10, 2)) is True


def test_a_weekly_scope_only_fires_on_its_day():
    """2026-03-10 is a Tuesday (weekday 1)."""
    at = _utc(2026, 3, 10, 13)
    assert morning.is_due(None, "America/New_York", now=at, dow=1) is True
    assert morning.is_due(None, "America/New_York", now=at, dow=4) is False


def test_an_unparseable_last_run_does_not_wedge_the_schedule():
    assert morning.is_due("not-a-date", "Asia/Kolkata", now=_utc(2026, 3, 10, 2)) is True


def test_an_unknown_timezone_falls_back_rather_than_raising():
    assert morning.is_due(None, "Mars/Olympus", now=_utc(2026, 3, 10, 20)) in (True, False)


# ─── Nothing invented ────────────────────────────────────────────────────────
class _Block:
    def __init__(self, text): self.text = text


class _Response:
    def __init__(self, text, urls):
        self.content = [_Block(text)] + [{"type": "web_search_result", "url": u} for u in urls]


@pytest.fixture
def searcher(monkeypatch):
    """Stub the model call; the test supplies the answer and the citations."""
    state: Dict[str, Any] = {"text": "[]", "urls": []}

    async def _call_llm(**kwargs):
        assert any(t.get("name") == "web_search" for t in kwargs.get("tools") or []), \
            "the sourcing calls must actually use the search tool"
        return _Response(state["text"], state["urls"]), {}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import ai.llm_client as llm

    monkeypatch.setattr(llm, "call_llm", _call_llm)
    return state


def test_an_event_the_search_actually_found_survives(searcher):
    searcher["text"] = (
        '[{"title":"Riyadh Health AI Summit","url":"https://example.org/summit",'
        '"when":"14 March","location":"Riyadh","organizer":"SCFHS","why":"Clinical AI."}]'
    )
    searcher["urls"] = ["https://example.org/summit"]
    out = asyncio.run(websearch.search_events(country_name="Saudi Arabia", limit=3))
    assert len(out) == 1
    assert out[0]["title"] == "Riyadh Health AI Summit"


def test_a_url_the_search_never_returned_is_dropped(searcher):
    """The failure this module is arranged around: a plausible conference at a
    plausible URL that does not exist."""
    searcher["text"] = (
        '[{"title":"Invented Summit","url":"https://not-in-the-citations.example/x"}]'
    )
    searcher["urls"] = ["https://example.org/something-else"]
    assert asyncio.run(websearch.search_events(limit=3)) == []


def test_citation_matching_ignores_scheme_www_and_trailing_slash(searcher):
    """A real match must not be thrown away over punctuation."""
    searcher["text"] = '[{"title":"Real","url":"https://www.example.org/summit/"}]'
    searcher["urls"] = ["http://example.org/summit"]
    assert len(asyncio.run(websearch.search_events(limit=3))) == 1


def test_a_non_http_url_is_dropped(searcher):
    searcher["text"] = '[{"title":"Nope","url":"javascript:alert(1)"}]'
    searcher["urls"] = ["javascript:alert(1)"]
    assert asyncio.run(websearch.search_events(limit=3)) == []


def test_an_unparseable_answer_is_an_empty_morning_not_a_crash(searcher):
    searcher["text"] = "I could not find anything useful today."
    assert asyncio.run(websearch.search_news(limit=3)) == []


def test_a_failing_search_never_raises(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("search is down")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import ai.llm_client as llm

    monkeypatch.setattr(llm, "call_llm", _boom)
    assert asyncio.run(websearch.search_events(limit=3)) == []


def test_without_a_key_nothing_is_searched(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert asyncio.run(websearch.search_events(limit=3)) == []


# ─── Posting ─────────────────────────────────────────────────────────────────
@pytest.fixture
def wired(monkeypatch):
    """A store, all channels visible, and no real model calls."""
    store = _fresh_store()
    store.ensure_default_channels(["SA"])
    monkeypatch.setattr(morning, "get_community_store", lambda: store)
    monkeypatch.setattr(morning, "ensure_country_channels", lambda: None)
    monkeypatch.setattr(
        morning, "_visible_channel_slugs",
        lambda: {c["slug"] for c in store.list_channels()})

    import community.system_posts as sp

    monkeypatch.setattr(sp, "get_community_store", lambda: store)
    monkeypatch.setattr("community.router.member_map", lambda **kw: {})
    return store


def test_a_quiet_day_posts_nothing_and_still_counts_as_a_run(wired, monkeypatch):
    """Otherwise the hourly trigger retries all day against sources that have
    nothing, and the channel fills with apologies."""
    async def _none(**kwargs):
        return []

    monkeypatch.setattr(websearch, "search_events", _none)
    scope = morning.Scope(key="morning:events", channel="events",
                          tz="America/New_York")
    result = asyncio.run(morning.run_scope(scope, force=True))
    assert result["outcome"] == "quiet"
    assert wired.last_successful_run_at("morning:events")


# ─── A silent morning must not look like a working one ───────────────────────
def _last_run(store, key):
    return next(r for r in store.latest_run_per_kind() if r["kind"] == key)


def test_a_morning_with_no_search_provider_records_why_it_posted_nothing(
        wired, monkeypatch):
    """The defect this closes: with no ANTHROPIC_API_KEY and no paid retriever
    every search returns [] in silence, the run records ok with zero items, and
    from the admin tab a dead routine is indistinguishable from a Tuesday on
    which the web had nothing new. The reason is the only thing that separates
    them."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    scope = morning.Scope(key="morning:events", channel="events",
                          tz="America/New_York")

    result = asyncio.run(morning.run_scope(scope, force=True))

    assert result["outcome"] == "quiet"
    assert result["reason"] == websearch.NO_MODEL_KEY
    row = _last_run(wired, "morning:events")
    assert row["ok"] == 1 and row["items_posted"] == 0
    assert row["reason"] == websearch.NO_MODEL_KEY


def test_a_provider_that_fails_is_not_recorded_as_a_quiet_day(wired, monkeypatch):
    """A search API having a bad morning and the web being quiet are different
    facts, and only one of them is worth waking somebody up about."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def _boom(**kwargs):
        raise RuntimeError("the search tool is down")

    import ai.llm_client as llm

    monkeypatch.setattr(llm, "call_llm", _boom)
    scope = morning.Scope(key="morning:news", channel="medical-ai-news",
                          tz="America/New_York")

    result = asyncio.run(morning.run_scope(scope, force=True))

    assert result["outcome"] == "quiet"
    assert result["reason"] == websearch.PROVIDER_ERROR
    assert _last_run(wired, "morning:news")["reason"] == websearch.PROVIDER_ERROR


def test_a_genuinely_quiet_day_says_it_found_nothing(wired, monkeypatch):
    """The other half of the same property: a run that could search and found
    nothing must NOT be blamed on a missing key, or the reason column becomes
    noise an operator learns to ignore."""
    async def _none(**kwargs):
        return []

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(websearch, "search_events", _none)
    scope = morning.Scope(key="morning:events", channel="events",
                          tz="America/New_York")

    result = asyncio.run(morning.run_scope(scope, force=True))

    assert result["reason"] == morning.REASON_NOTHING_FOUND
    assert _last_run(wired, "morning:events")["reason"] == morning.REASON_NOTHING_FOUND


def test_a_run_that_posted_records_that_it_posted(wired, monkeypatch):
    async def _one(**kwargs):
        return [{"title": "Riyadh Health AI Summit", "url": "https://example.org/s",
                 "when": "14 March", "location": "Riyadh", "why": "Clinical AI."}]

    monkeypatch.setattr(websearch, "search_events", _one)
    scope = morning.Scope(key="morning:events", channel="events", tz="America/New_York")
    assert asyncio.run(morning.run_scope(scope, force=True))["outcome"] == "posted"
    row = _last_run(wired, "morning:events")
    assert row["reason"] == morning.REASON_POSTED and row["items_posted"] == 1


def test_the_missing_provider_beats_the_provider_error_in_the_reason(wired):
    """Most fixable first. A run that noted both should name the thing an
    operator can actually go and set."""
    assert morning.quiet_reason(
        {websearch.PROVIDER_ERROR, websearch.NO_PROVIDER}) == websearch.NO_PROVIDER
    assert morning.quiet_reason(set()) == morning.REASON_NOTHING_FOUND


def test_a_brief_is_posted_once_and_not_again_the_same_day(wired, monkeypatch):
    async def _one(**kwargs):
        return [{"title": "Riyadh Health AI Summit", "url": "https://example.org/s",
                 "when": "14 March", "location": "Riyadh", "why": "Clinical AI."}]

    monkeypatch.setattr(websearch, "search_events", _one)
    scope = morning.Scope(key="morning:events", channel="events", tz="America/New_York")

    first = asyncio.run(morning.run_scope(scope, force=True))
    assert first["outcome"] == "posted"

    # The hourly trigger comes back an hour later; the ledger says no.
    second = asyncio.run(morning.run_scope(scope))
    assert second["outcome"] == "not_due"

    channel = wired.get_channel_by_slug("events")
    messages, _ = wired.list_messages(channel["id"])
    assert len([m for m in messages if m.get("kind") == morning.KIND_EVENTS]) == 1


def test_two_runners_racing_the_same_morning_post_one_brief(wired, monkeypatch):
    """Two schedulers drive this: the hourly GitHub Actions cron and the
    in-process hourly loop. Both used to read "no successful run since today's
    fire time", both find it true, both spend an LLM call composing, and both
    post, because a ledger READ is not a claim. The run has to reserve its
    window before it composes anything.

    The second runner arrives mid-compose, which is exactly when the ledger
    still says nothing has succeeded today.
    """
    # 00:00 local, so the fire time has passed for every scope and both runs
    # reach the claim rather than short-circuiting on due-ness.
    monkeypatch.setenv("COMMUNITY_MORNING_HOUR_LOCAL", "0")
    scope = morning.Scope(key="morning:events", channel="events", tz="America/New_York")
    second: List[Dict[str, Any]] = []

    async def _one(**kwargs):
        if not second:
            # The other runner's tick lands while this one is still sourcing.
            # It gets no further than the claim, so this does not recurse.
            second.append(await morning.run_scope(scope))
        return [{"title": "Riyadh Health AI Summit", "url": "https://example.org/s",
                 "when": "14 March", "location": "Riyadh", "why": "Clinical AI."}]

    monkeypatch.setattr(websearch, "search_events", _one)

    first = asyncio.run(morning.run_scope(scope))
    assert first["outcome"] == "posted"
    # The loser exits without composing and without posting.
    assert second[0]["outcome"] == "already_running"

    channel = wired.get_channel_by_slug("events")
    messages, _ = wired.list_messages(channel["id"])
    assert len([m for m in messages if m.get("kind") == morning.KIND_EVENTS]) == 1


def test_a_failed_morning_releases_the_day_so_the_next_tick_retries(wired, monkeypatch):
    """The reservation must not cost a channel its day over a transient error.
    A run that fails hands its window back; only a successful one keeps it."""
    monkeypatch.setenv("COMMUNITY_MORNING_HOUR_LOCAL", "0")
    scope = morning.Scope(key="morning:events", channel="events", tz="America/New_York")

    async def _boom(**kwargs):
        raise RuntimeError("search is down")

    monkeypatch.setattr(websearch, "search_events", _boom)
    assert asyncio.run(morning.run_scope(scope))["outcome"] == "failed"

    async def _one(**kwargs):
        return [{"title": "Riyadh Health AI Summit", "url": "https://example.org/s",
                 "when": "14 March", "location": "Riyadh", "why": "Clinical AI."}]

    monkeypatch.setattr(websearch, "search_events", _one)
    assert asyncio.run(morning.run_scope(scope))["outcome"] == "posted"


def test_an_event_card_may_carry_its_date(wired, monkeypatch):
    """"14 March" trips the PHI gate's exact_date rule, which is right for a
    message that might be about a patient and wrong for a conference. Without
    the narrow exemption every events post would be silently dropped."""
    async def _dated(**kwargs):
        return [{"title": "Grand rounds", "url": "https://example.org/gr",
                 "when": "March 14, 2026", "location": "Online", "why": "AI in CKD."}]

    monkeypatch.setattr(websearch, "search_events", _dated)
    scope = morning.Scope(key="morning:events", channel="events", tz="America/New_York")
    assert (asyncio.run(morning.run_scope(scope, force=True)))["outcome"] == "posted"


def test_a_blocked_post_is_recorded_as_a_failure_not_a_quiet_day(wired, monkeypatch):
    """The PHI gate skips a system post silently by design. Silent is wrong
    here: a morning that was blocked must be distinguishable from one that had
    nothing to say."""
    async def _one(**kwargs):
        return [{"title": "x", "url": "https://example.org/x"}]

    async def _blocked(**kwargs):
        return None

    monkeypatch.setattr(websearch, "search_news", _one)
    monkeypatch.setattr(morning, "post_system_message", _blocked)
    scope = morning.Scope(key="morning:news", channel="medical-ai-news",
                          tz="America/New_York")
    result = asyncio.run(morning.run_scope(scope, force=True))
    assert result["outcome"] == "blocked"


def test_one_scope_failing_does_not_stop_the_others(wired, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(websearch, "search_news", _boom)
    scope = morning.Scope(key="morning:news", channel="medical-ai-news",
                          tz="America/New_York")
    assert (asyncio.run(morning.run_scope(scope, force=True)))["outcome"] == "failed"


def test_the_morning_never_posts_into_a_hidden_channel(wired, monkeypatch):
    """Posting into a below-threshold country room would give it history,
    which is what makes a channel visible. It would open itself by being
    written to, which is exactly backwards."""
    monkeypatch.setattr(morning, "_visible_channel_slugs", lambda: {"general"})
    scopes = morning.build_scopes()
    assert all(s.channel == "general" for s in scopes)


# ─── The operator can see all of that ────────────────────────────────────────
def test_the_admin_summary_reports_the_last_run_of_each_scope_with_its_reason(
        wired, monkeypatch):
    """The counts on the admin community tab answer "is the community alive".
    They cannot answer "is the BOT alive", because a run with no model key
    writes the same ok-with-zero-items row a quiet day writes. This is the read
    that separates them, and it is asserted end to end rather than on the store
    method alone: the value of the panel is that the reason reaches a screen.
    """
    from fastapi.testclient import TestClient

    from tests import _asclepius as A

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    scope = morning.Scope(key="morning:events", channel="events",
                          tz="America/New_York")
    asyncio.run(morning.run_scope(scope, force=True))

    # The endpoint reads the process-global community store, which ``wired``
    # deliberately does not rebind (it patches the module's getter instead), so
    # point the global at the same DB the run just wrote.
    import community.store as cstore_mod

    monkeypatch.setattr(cstore_mod, "_store", wired, raising=False)
    monkeypatch.setattr(cstore_mod, "get_community_store", lambda: wired)
    monkeypatch.setattr("community.router._cstore", lambda: wired)

    store = A.fresh_store()
    admin = A.make_user(store, role="admin")
    with TestClient(A.app) as client:
        body = client.get("/api/asclepius/admin/community/summary",
                          headers=A.headers_for(admin)).json()

    automation = body["automation"]
    assert [g["var"] for g in automation["gates"]] == [
        "COMMUNITY_MORNING_ENABLED", "COMMUNITY_NEWS_ENABLED"]
    assert all(g["on"] for g in automation["gates"]), \
        "both gates default on, and the panel must say so"
    assert automation["dependencies"]["anthropic_api_key"] is False
    row = next(r for r in automation["runs"] if r["kind"] == "morning:events")
    assert row["items_posted"] == 0
    assert row["reason"] == websearch.NO_MODEL_KEY
    assert "ANTHROPIC_API_KEY" in row["reason_text"], \
        "the reason has to read as an instruction, not as a token"


def test_the_run_panel_does_not_list_one_row_per_doctor(wired):
    """The newsletter ledgers a kind per DOCTOR per day. Listing those would
    bury the dozen scopes an operator is looking at under the roster."""
    wired.claim_digest_run("morning:newsletter:member:u-someone", window_key=None)
    wired.claim_digest_run("morning:events", window_key=None)

    kinds = [r["kind"] for r in wired.latest_run_per_kind(
        exclude_prefixes=["morning:newsletter:member:"])]

    assert "morning:events" in kinds
    assert not any(k.startswith("morning:newsletter:member:") for k in kinds)


# ─── Pinned topics ───────────────────────────────────────────────────────────
def test_each_channel_gets_one_pinned_topic_post(wired):
    asyncio.run(morning.ensure_channel_topics())
    channel = wired.get_channel_by_slug("events")
    assert wired.has_system_post_of_kind(channel["id"], morning.KIND_TOPIC)
    pins = wired.list_pins(channel["id"])
    assert pins, "the topic post should be pinned, not just posted"
    # list_pins returns message rows, not pin rows.
    assert wired.is_pinned(pins[0]["id"])
    assert pins[0]["kind"] == morning.KIND_TOPIC


def test_topic_posts_are_not_repeated_on_the_next_run(wired):
    asyncio.run(morning.ensure_channel_topics())
    asyncio.run(morning.ensure_channel_topics())
    channel = wired.get_channel_by_slug("events")
    messages, _ = wired.list_messages(channel["id"])
    topics = [m for m in messages if m.get("kind") == morning.KIND_TOPIC]
    assert len(topics) == 1
