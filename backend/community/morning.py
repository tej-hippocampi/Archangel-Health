"""The morning routine.

Every day, before a doctor's first coffee, each room they belong to should have
something in it that was not there yesterday: three events they could actually
attend, the medical-AI news that matters with a summary so they need not click,
and an open research opportunity. The core channels get the global version, the
specialty channels get theirs, and the country channels get what is happening
where they practise.

Three things shape the design.

**Local, not UTC.** A "morning brief" that lands at four in the afternoon is a
notification. Each country channel fires at 7am in that country's own timezone,
which is most of what makes a daily routine feel like it was written for the
person reading it.

**Idempotent by ledger, not by luck.** Every scope is a row in
``community_digest_runs`` and is due only when it has not succeeded since
today's fire time. The trigger can be called every hour, twice, or by an
impatient admin, and the channel gets one brief.

**A quiet day is a valid day.** No sources, no valid URLs, nothing new to say:
post nothing. A channel that greets its members with three stale conferences
every morning teaches them to stop looking.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from community import countries as ccountries
from community import websearch
from community.store import get_community_store
from community.system_posts import post_system_message

log = logging.getLogger("community.morning")

#: Message kinds this module writes. ``morning_events`` is the one whose cards
#: legitimately carry dates -- see the PHI-gate exemption in system_posts.
KIND_EVENTS = "morning_events"
KIND_NEWS = "morning_news"
KIND_OPPORTUNITIES = "morning_opportunities"
KIND_BRIEF = "morning_brief"
KIND_DISCUSSION = "discussion_prompt"
KIND_TOPIC = "channel_topic"


def enabled() -> bool:
    return (os.getenv("COMMUNITY_MORNING_ENABLED", "0") or "0").strip() not in ("", "0", "false", "False")


def fire_hour() -> int:
    try:
        return max(0, min(23, int(os.getenv("COMMUNITY_MORNING_HOUR_LOCAL", "7"))))
    except (TypeError, ValueError):
        return 7


def home_timezone() -> str:
    return (os.getenv("ARCHANGEL_HOME_TZ") or ccountries.DEFAULT_TIMEZONE).strip()


def events_max() -> int:
    try:
        return max(1, int(os.getenv("COMMUNITY_EVENTS_MAX", "3")))
    except (TypeError, ValueError):
        return 3


def discussion_dow() -> int:
    try:
        return max(0, min(6, int(os.getenv("COMMUNITY_DISCUSSION_DOW", "2"))))
    except (TypeError, ValueError):
        return 2


# ─── Due-ness ────────────────────────────────────────────────────────────────
def is_due(
    last_ok_started: Optional[str], tz_name: str, *, now: Optional[datetime] = None,
    dow: Optional[int] = None,
) -> bool:
    """Has this scope's local fire time passed today without a successful run?

    Pure, so the whole schedule is testable with a frozen clock instead of by
    waiting until tomorrow.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - a bad zone must not stop the world
        tz = ZoneInfo(ccountries.DEFAULT_TIMEZONE)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(tz)
    if dow is not None and local.weekday() != dow:
        return False
    fire_at = local.replace(hour=fire_hour(), minute=0, second=0, microsecond=0)
    if local < fire_at:
        return False
    if not last_ok_started:
        return True
    try:
        last = datetime.fromisoformat(str(last_ok_started).rstrip("Z")).replace(
            tzinfo=timezone.utc)
    except ValueError:
        return True
    return last.astimezone(tz) < fire_at


# ─── Cards ───────────────────────────────────────────────────────────────────
def _domain(url: str) -> str:
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    return host[4:] if host.startswith("www.") else host


def _card(item: Dict[str, Any], *, meta_keys: Tuple[str, ...]) -> Dict[str, Any]:
    url = str(item.get("url") or "").strip()
    meta = " · ".join(
        str(item.get(k)).strip() for k in meta_keys
        if str(item.get(k) or "").strip()
    )
    return {
        "title": str(item.get("title") or "").strip()[:200],
        "url": url,
        "domain": _domain(url),
        "description": str(item.get("summary") or item.get("why") or "").strip()[:400],
        "meta": meta[:160],
        "prompt": str(item.get("prompt") or "").strip()[:300],
    }


def _dedupe_new(cstore: Any, items: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    """Drop anything this community has already been shown.

    Reuses ``community_content_items``, whose normalized-URL uniqueness (and
    14-day title match) is what stops the same conference being announced in
    three channels across three mornings. ``upsert_content_items`` returns the
    FRESH rows only, so what comes back is exactly what is new.

    Each surviving item carries its row id, so the post can mark precisely what
    it used rather than everything that happened to be new.
    """
    if not items:
        return []
    from community.feeds import normalize_title, normalize_url  # noqa: PLC0415

    payload = []
    by_url: Dict[str, Dict[str, Any]] = {}
    for it in items:
        url = str(it.get("url") or "").strip()
        title = str(it.get("title") or "").strip()
        if not url or not title:
            continue
        payload.append({
            "source": source,
            "external_id": None,
            "url": url,
            "url_norm": normalize_url(url),
            "title": title,
            "title_norm": normalize_title(title),
            "published_at": None,
            "abstract": str(it.get("summary") or it.get("why") or "")[:1000],
        })
        by_url[url] = it
    if not payload:
        return []
    try:
        fresh = cstore.upsert_content_items(payload)
    except Exception:  # noqa: BLE001 - dedupe is a nicety, not a gate
        log.warning("[morning] dedupe unavailable; posting without it", exc_info=True)
        return items
    out = []
    for row in fresh:
        item = by_url.get(row.get("url"))
        if item is not None:
            out.append({**item, "_content_id": row.get("id")})
    return out


def _mark_posted(cstore: Any, items: List[Dict[str, Any]], message_id: Any = None) -> None:
    ids = [it["_content_id"] for it in items if it.get("_content_id")]
    if not ids:
        return
    try:
        cstore.mark_content_items(ids, status="posted", posted_message_id=message_id)
    except Exception:  # noqa: BLE001
        log.warning("[morning] could not mark content posted", exc_info=True)


# ─── The scopes ──────────────────────────────────────────────────────────────
class Scope:
    """One channel's morning, and where its content comes from."""

    def __init__(self, *, key: str, channel: str, tz: str,
                 country_name: Optional[str] = None,
                 specialty: Optional[str] = None,
                 dow: Optional[int] = None):
        self.key = key
        self.channel = channel
        self.tz = tz
        self.country_name = country_name
        self.specialty = specialty
        self.dow = dow


def _visible_channel_slugs() -> set:
    """Only channels members can actually see.

    Posting into a hidden channel would make it visible -- ``visible_channels``
    keeps a channel with history -- so a below-threshold country room would
    open itself by being written to, which is exactly backwards.
    """
    try:
        from community.router import member_map, visible_channels  # noqa: PLC0415

        return {c["slug"] for c in visible_channels(member_map())}
    except Exception:  # noqa: BLE001
        log.warning("[morning] could not resolve visible channels", exc_info=True)
        return set()


def build_scopes() -> List[Scope]:
    """Every channel getting a brief today, most specific first.

    Order matters: a conference in Riyadh should land in #saudi-arabia rather
    than being spent on the global #events feed, and the URL-level dedupe gives
    the item to whoever asks first.
    """
    cstore = get_community_store()
    visible = _visible_channel_slugs()
    scopes: List[Scope] = []

    for ch in cstore.list_channels():
        slug = ch["slug"]
        if slug not in visible:
            continue
        grp = ch.get("grp") or "core"
        if grp == "country":
            country = ccountries.get(ch.get("country"))
            if not country:
                continue
            scopes.append(Scope(key=f"morning:country:{country.code}", channel=slug,
                                tz=country.timezone, country_name=country.name))
        elif grp == "specialty":
            scopes.append(Scope(key=f"morning:specialty:{slug}", channel=slug,
                                tz=home_timezone(), specialty=ch.get("specialty") or slug))

    home = home_timezone()
    scopes.append(Scope(key="morning:events", channel="events", tz=home))
    scopes.append(Scope(key="morning:news", channel="medical-ai-news", tz=home))
    scopes.append(Scope(key="morning:opportunities", channel="research-and-opportunities",
                        tz=home))
    scopes.append(Scope(key="morning:discussion", channel="future-of-medical-ai",
                        tz=home, dow=discussion_dow()))
    return [s for s in scopes if s.channel in visible]


# ─── Composers ───────────────────────────────────────────────────────────────
async def _compose_events(cstore: Any, scope: Scope) -> Optional[Dict[str, Any]]:
    items = await websearch.search_events(
        country_name=scope.country_name, specialty=scope.specialty,
        limit=events_max())
    items = _dedupe_new(cstore, items, "websearch:events")[:events_max()]
    if not items:
        return None
    where = scope.country_name or scope.specialty or "worth a look"
    body = (f"**Coming up** ({where})\n\n"
            "Three you could actually get to. Tap through for details.")
    return {"body": body, "kind": KIND_EVENTS,
            "cards": [_card(i, meta_keys=("when", "location", "organizer")) for i in items],
            "items": items}


async def _compose_news(cstore: Any, scope: Scope) -> Optional[Dict[str, Any]]:
    items = await websearch.search_news(
        country_name=scope.country_name, specialty=scope.specialty, limit=3)
    items = _dedupe_new(cstore, items, "websearch:news")[:3]
    if not items:
        return None
    body = "**What happened in medical AI**\n\nSummaries below, so you can skip the click."
    return {"body": body, "kind": KIND_NEWS,
            "cards": [_card(i, meta_keys=()) for i in items], "items": items}


async def _compose_opportunities(cstore: Any, scope: Scope) -> Optional[Dict[str, Any]]:
    items = await websearch.search_opportunities(
        country_name=scope.country_name, specialty=scope.specialty, limit=3)
    items = _dedupe_new(cstore, items, "websearch:opps")[:3]
    if not items:
        return None
    body = "**Open to you right now**\n\nGrants, fellowships and calls worth an application."
    return {"body": body, "kind": KIND_OPPORTUNITIES,
            "cards": [_card(i, meta_keys=("deadline",)) for i in items], "items": items}


async def _compose_discussion(cstore: Any, scope: Scope) -> Optional[Dict[str, Any]]:
    try:
        seen = [
            r.get("title") for r in cstore.new_content_items(max_age_days=60)
            if (r.get("source") or "") == "websearch:discussion"
        ][:8]
    except Exception:  # noqa: BLE001
        seen = []
    items = await websearch.search_discussion_topic(avoid_titles=seen)
    items = _dedupe_new(cstore, items, "websearch:discussion")[:1]
    if not items:
        return None
    item = items[0]
    prompt = str(item.get("prompt") or "").strip()
    body = f"**{str(item.get('title') or 'This week').strip()}**\n\n{prompt}"
    return {"body": body, "kind": KIND_DISCUSSION,
            "cards": [_card(item, meta_keys=())], "items": items}


async def _compose_brief(cstore: Any, scope: Scope) -> Optional[Dict[str, Any]]:
    """A country or specialty room gets one post, not three.

    Three separate bot posts every morning in a room of forty people is a feed
    nobody reads. One brief with everything in it is a thing to open.
    """
    events = await websearch.search_events(
        country_name=scope.country_name, specialty=scope.specialty, limit=2)
    events = _dedupe_new(cstore, events, "websearch:events")[:2]
    news = await websearch.search_news(
        country_name=scope.country_name, specialty=scope.specialty, limit=2)
    news = _dedupe_new(cstore, news, "websearch:news")[:2]
    opps = await websearch.search_opportunities(
        country_name=scope.country_name, specialty=scope.specialty, limit=1)
    opps = _dedupe_new(cstore, opps, "websearch:opps")[:1]

    items = events + news + opps
    if not items:
        return None
    label = scope.country_name or (scope.specialty or "").title() or "Today"
    body = f"**{label} this morning**\n\nEvents, news and one opportunity."
    cards = (
        [_card(i, meta_keys=("when", "location")) for i in events]
        + [_card(i, meta_keys=()) for i in news]
        + [_card(i, meta_keys=("deadline",)) for i in opps]
    )
    return {"body": body, "kind": KIND_BRIEF, "cards": cards, "items": items}


_COMPOSERS: Dict[str, Callable] = {
    "morning:events": _compose_events,
    "morning:news": _compose_news,
    "morning:opportunities": _compose_opportunities,
    "morning:discussion": _compose_discussion,
}


def _composer_for(scope: Scope) -> Callable:
    return _COMPOSERS.get(scope.key, _compose_brief)


# ─── The run ─────────────────────────────────────────────────────────────────
async def run_scope(scope: Scope, *, force: bool = False) -> Dict[str, Any]:
    """One channel's morning. Never raises."""
    cstore = get_community_store()
    if not force and not is_due(cstore.last_successful_run_at(scope.key), scope.tz,
                                dow=scope.dow):
        return {"scope": scope.key, "outcome": "not_due"}

    run_id = cstore.start_digest_run(scope.key)
    try:
        composed = await _composer_for(scope)(cstore, scope)
        if not composed:
            # A quiet day is a valid day, and it counts as a run: otherwise the
            # trigger retries every hour against sources that have nothing.
            cstore.finish_digest_run(run_id, ok=True, items_posted=0)
            return {"scope": scope.key, "outcome": "quiet"}

        message = await post_system_message(
            channel_slug=scope.channel,
            body=composed["body"],
            kind=composed["kind"],
            cards=composed["cards"],
        )
        if not message:
            # The PHI gate skips a system post silently by design. Silent is
            # wrong here: a morning that posted nothing because it was blocked
            # must be distinguishable from one that had nothing to say.
            cstore.finish_digest_run(run_id, ok=False, error="post_blocked")
            return {"scope": scope.key, "outcome": "blocked"}

        _mark_posted(cstore, composed["items"], message.get("id"))
        cstore.finish_digest_run(run_id, ok=True, items_posted=len(composed["cards"]))
        return {"scope": scope.key, "outcome": "posted",
                "cards": len(composed["cards"])}
    except Exception as exc:  # noqa: BLE001 - one bad scope must not stop the rest
        log.warning("[morning] scope %s failed", scope.key, exc_info=True)
        try:
            cstore.finish_digest_run(run_id, ok=False, error=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
        return {"scope": scope.key, "outcome": "failed", "error": str(exc)[:200]}


async def run_morning(*, only: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Every scope that is due. Called hourly; posts once per channel per day."""
    ensure_country_channels()
    scopes = build_scopes()
    if only:
        scopes = [s for s in scopes if s.key == only or s.channel == only]
    results = [await run_scope(s, force=force) for s in scopes]
    await ensure_channel_topics()
    summary = {
        "ran": [r["scope"] for r in results if r["outcome"] == "posted"],
        "quiet": [r["scope"] for r in results if r["outcome"] == "quiet"],
        "skipped": [r["scope"] for r in results if r["outcome"] == "not_due"],
        "failed": [r for r in results if r["outcome"] in ("failed", "blocked")],
    }
    log.info("[morning] ran=%d quiet=%d skipped=%d failed=%d",
             len(summary["ran"]), len(summary["quiet"]),
             len(summary["skipped"]), len(summary["failed"]))
    return summary


def ensure_country_channels() -> None:
    """Open a country's room once its physicians are here."""
    try:
        from main import _member_country_codes  # noqa: PLC0415

        get_community_store().ensure_default_channels(_member_country_codes())
    except Exception:  # noqa: BLE001
        log.warning("[morning] country channel refresh failed", exc_info=True)


# ─── Pinned topics ───────────────────────────────────────────────────────────
_TOPIC_BY_GROUP = {
    "country": ("What this room is for", "Colleagues practising in the same "
                "country: local events, regulation, and how medical AI is "
                "actually landing in your hospitals. The morning brief posts "
                "here; everything else is yours."),
    "specialty": ("What this room is for", "Your specialty: cases in the "
                  "abstract, literature worth reading, and the task work "
                  "specific to it. De-identified always."),
}

_TOPIC_BY_SLUG = {
    "future-of-medical-ai": (
        "Where is this actually going?",
        "The room for the argument. Takes, papers, predictions, and the "
        "questions nobody has settled. A grounded discussion topic is posted "
        "here weekly; disagreeing with it is the point."),
    "events": ("Events worth your evening",
               "Conferences, webinars, grand rounds and CME. Three are posted "
               "each morning; tap Interested on an event card to get a "
               "reminder before it starts."),
    "medical-ai-news": ("The news, with the reading done",
                        "Each story arrives with a summary so you can decide "
                        "from here whether it is worth the click, and a "
                        "question if it is worth an argument."),
    "research-and-opportunities": (
        "Things you can apply to",
        "Grants, fellowships, calls for reviewers and paid collaborations, "
        "posted as they open. Deadlines on the card."),
    "introductions": ("Say hello",
                      "Specialty, where you practise, what you are curious "
                      "about. New colleagues are introduced here as they "
                      "arrive."),
}


async def ensure_channel_topics() -> None:
    """One pinned bot post per channel saying what the room is for.

    Idempotent: a channel that already has a topic post keeps the one it has.
    A person walking into eleven channels with no idea which is which is how a
    community reads as empty even when it is not.
    """
    cstore = get_community_store()
    visible = _visible_channel_slugs()
    for ch in cstore.list_channels():
        slug = ch["slug"]
        if slug not in visible:
            continue
        try:
            if cstore.has_system_post_of_kind(ch["id"], KIND_TOPIC):
                continue
        except Exception:  # noqa: BLE001
            continue
        topic = _TOPIC_BY_SLUG.get(slug) or _TOPIC_BY_GROUP.get(ch.get("grp") or "core")
        if not topic:
            continue
        title, text = topic
        message = await post_system_message(
            channel_slug=slug, body=f"**{title}**\n\n{text}", kind=KIND_TOPIC)
        if not message:
            continue
        try:
            cstore.pin_message(channel_id=ch["id"], message_id=message["id"],
                               pinned_by="u-system")
        except Exception:  # noqa: BLE001
            log.warning("[morning] could not pin the topic for #%s", slug, exc_info=True)


# ─── An in-process fallback ──────────────────────────────────────────────────
# The scheduled trigger is the reliable path: it survives a restart, it says so
# in version control, and it leaves a log somewhere a person can read. But
# making the whole routine depend on it means a deploy without that workflow
# configured is a deploy where the community quietly stops filling up, and
# nothing anywhere says so.
#
# So the app can also drive itself. Same endpoint logic, same ledger, same
# due-calculation, so the two cannot double-post: whichever gets there first
# marks the run and the other finds nothing due. Ticks hourly because every
# scope's decision is "has my local 7am passed today", which only needs
# checking at that resolution.
_loop_task: Optional["asyncio.Task"] = None
_TICK_SEC = 3600


def start_morning_loop() -> None:
    """Start (once) the in-process morning tick. Only when enabled."""
    global _loop_task
    import asyncio  # noqa: PLC0415

    if _loop_task is not None and not _loop_task.done():
        return

    async def _run() -> None:
        while True:
            # Sleep first: startup should never wait on this, and a task
            # created during startup begins at the first await anyway.
            await asyncio.sleep(_TICK_SEC)
            try:
                await run_morning()
            except Exception:  # pragma: no cover - the loop must survive
                log.warning("[morning] tick failed", exc_info=True)
            try:
                from community import newsletter as _cnewsletter  # noqa: PLC0415

                await _cnewsletter.run_newsletter()
            except Exception:  # pragma: no cover
                log.warning("[morning] newsletter tick failed", exc_info=True)

    _loop_task = asyncio.get_running_loop().create_task(_run())
    log.info("[morning] in-process loop started (hourly; fires at %02d:00 local per scope)",
             fire_hour())


def loop_running() -> bool:
    """True when the in-process morning tick is alive.

    Distinct from ``enabled()``, which only reports what the environment asked
    for. The external GitHub Actions trigger drives the same code, so this
    being False is not on its own a fault, but both being False, with no cron
    installed, means no channel ever gets a brief.
    """
    return _loop_task is not None and not _loop_task.done()


def stop_morning_loop() -> None:
    global _loop_task
    task, _loop_task = _loop_task, None
    if task:
        task.cancel()
