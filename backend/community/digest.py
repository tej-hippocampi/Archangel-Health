"""#medical-ai-news digest pipeline (Community v2).

fetch → keyword filter → persistent dedup → two-pass LLM curation → one
system post. Digest, never firehose: at most ``COMMUNITY_DIGEST_MAX_ITEMS``
stories per post, one post per run, and a run that finds nothing fresh posts
NOTHING (an empty digest is worse than no digest).

Two kinds share the machinery:
  * ``news``   — reporter RSS sources; scheduled daily.
  * ``papers`` — PubMed + arXiv + medRxiv; scheduled weekly.

Failure policy: every run is recorded in ``community_digest_runs``
(three-outcome ``ok``: NULL running / 1 / 0); a failed source is skipped
(feeds.py), a failed LLM parse posts nothing and fails the run, and the
scheduler loop can never crash. Three consecutive failures of a kind logs a
grep-able ``ADMIN ATTENTION`` line.

The scheduled loop is gated on ``COMMUNITY_NEWS_ENABLED=1``; the internal
trigger endpoint fires a run on demand either way. The gate defaults to OFF,
which dates from when the community was empty and no bot-authored post
belonged in it. That is no longer the case, so a deployment that wants the
digest must set the variable, and ``/internal/community/status`` reports
whether it did, because a loop that never started is otherwise indis-
tinguishable from a quiet week.
"""

from __future__ import annotations

import asyncio
import logging
import os
import realm as _realm
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from community import feeds, links
from community.store import get_community_store
from community.system_posts import post_system_message

log = logging.getLogger("community.digest")

DIGEST_CHANNEL = "medical-ai-news"

_DEFAULT_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "deep learning",
    "llm", "large language model", "foundation model", "neural network",
    "clinical decision support", "medical imaging", "algorithm",
    "chatgpt", "gpt", "claude", "gemini", "openai", "anthropic",
]


def _int_env(name: str, default: int, floor: int = 1) -> int:
    try:
        return max(floor, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def max_items() -> int:
    return _int_env("COMMUNITY_DIGEST_MAX_ITEMS", 15)


def max_tokens() -> int:
    return _int_env("COMMUNITY_DIGEST_MAX_TOKENS", 1200, floor=200)


def keywords() -> List[str]:
    raw = (os.getenv("COMMUNITY_NEWS_KEYWORDS") or "").strip()
    if not raw:
        return list(_DEFAULT_KEYWORDS)
    return [k.strip().lower() for k in raw.split(",") if k.strip()] or list(_DEFAULT_KEYWORDS)


def _keyword_filter(items: List[Dict[str, Any]], *, require: bool) -> List[Dict[str, Any]]:
    """Keep items whose title/abstract hits a keyword. Paper sources are
    already query-constrained to medical-AI, so they skip the filter
    (``require=False``); the general reporter feeds must hit."""
    if not require:
        return items
    kws = keywords()
    out = []
    for it in items:
        hay = ((it.get("title") or "") + " " + (it.get("abstract") or "")).lower()
        if any(k in hay for k in kws):
            out.append(it)
    return out


# ─── LLM curation (two passes, both size-capped) ──────────────────────────────
_SELECT_SYSTEM = (
    "You curate a news digest for a private community of verified physicians who do "
    "paid AI-evaluation work. Judge each candidate item strictly on relevance to AI in "
    "medicine (models, evals, regulation, deployments, research). Return ONLY a JSON "
    "object: {\"items\": [{\"id\": <int>, \"keep\": <bool>, \"relevance\": <0..1>, "
    "\"one_liner\": \"<=25 words, factual, no hype — say what happened, never invent\"}]}. "
    "Every input id must appear exactly once. Do not add fields or prose."
)

_COMPOSE_SYSTEM = (
    "You write the digest post for #medical-ai-news in a physicians' community. "
    "Input: a JSON list of kept items (title, url, one_liner, source). Output: the post "
    "body ONLY, markdown-lite (no HTML): start with one bold header line naming the "
    "digest (e.g. **Medical AI Digest** or **Papers of the Week**) — NO calendar date "
    "in the header or anywhere else (the platform timestamps the post; full dates "
    "false-trip the clinical PHI filter), rephrase any full date in a one_liner to "
    "month-year or 'this week'. Then group items under 2-4 bold section lines (e.g. "
    "**Research**, **Industry & deployment**, **Regulation**), each item exactly one "
    "bullet: \"- [title](url) — one_liner\". If two items cover the same story, keep "
    "one bullet and fold the second link in as \"(also: [source](url))\". No intro "
    "paragraph, no sign-off, no invented facts, no items beyond the input."
)


async def _curate(kind: str, items: List[Dict[str, Any]]) -> Tuple[Optional[str], Dict[int, Dict[str, Any]]]:
    """Two LLM passes. Returns ``(post_body | None, {item_id: {summary, relevance}})``.
    ``None`` body = nothing worth posting (a valid quiet day). A parse failure
    RAISES — the caller records the run as failed and posts nothing."""
    from ai.llm_client import call_llm, first_text  # noqa: PLC0415
    from asclepius.model_sampling import extract_json  # noqa: PLC0415

    capped = items[: max_items() * 2]  # give the selector some slack to cut
    lines = [
        {"id": it["id"], "title": it["title"], "source": it["source"],
         "snippet": (it.get("abstract") or "")[:400]}
        for it in capped
    ]
    import json as _json  # noqa: PLC0415

    resp, _meta = await call_llm(
        role="community_digest",
        system=_SELECT_SYSTEM,
        messages=[{"role": "user", "content": _json.dumps({"items": lines})}],
        prompt_id=f"community_digest_select_{kind}",
        purpose="community news digest — select/score items",
        temperature=0.2,
    )
    parsed = extract_json(first_text(resp))
    rows = (parsed or {}).get("items")
    if not isinstance(rows, list):
        raise ValueError("digest select pass returned unparseable JSON")

    by_id = {it["id"]: it for it in capped}
    kept: List[Dict[str, Any]] = []
    summaries: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        try:
            iid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        if iid not in by_id or iid in summaries:
            continue  # unknown or repeated id — never a duplicate bullet
        summaries[iid] = {
            "summary": (r.get("one_liner") or "").strip()[:300] or None,
            "relevance": float(r.get("relevance") or 0.0),
        }
        if r.get("keep"):
            kept.append({**by_id[iid], **summaries[iid]})
    kept.sort(key=lambda x: -(x.get("relevance") or 0.0))
    kept = kept[: max_items()]
    if not kept:
        return None, summaries

    compose_input = [
        {"title": k["title"], "url": k["url"],
         "one_liner": k.get("summary") or "", "source": k["source"]}
        for k in kept
    ]
    resp2, _meta2 = await call_llm(
        role="community_digest",
        system=_COMPOSE_SYSTEM,
        messages=[{"role": "user", "content": _json.dumps(
            {"digest_kind": kind, "items": compose_input})}],
        prompt_id=f"community_digest_compose_{kind}",
        purpose="community news digest — compose post",
        temperature=0.2,
        max_tokens=max_tokens(),
    )
    body = (first_text(resp2) or "").strip()
    if not body:
        raise ValueError("digest compose pass returned empty text")
    # mark kept for the caller
    for k in kept:
        summaries[k["id"]]["kept"] = True
    return body, summaries


async def _fetch(kind: str) -> List[Dict[str, Any]]:
    if kind == "papers":
        # Fetchers already skip-and-log internally; return_exceptions is the
        # second belt — one source raising unexpectedly never kills the run.
        batches = await asyncio.gather(
            feeds.fetch_pubmed(days=7), feeds.fetch_arxiv(days=7), feeds.fetch_medrxiv(days=7),
            return_exceptions=True,
        )
        items = []
        for b in batches:
            if isinstance(b, list):
                items.extend(b)
            elif isinstance(b, BaseException):
                log.warning("[digest] paper source raised — skipped: %s", b)
        return _keyword_filter(items, require=False)
    items = await feeds.fetch_rss()
    return _keyword_filter(items, require=True)


def _headline_from(body: str) -> str:
    """First non-empty, non-bullet line, trimmed. The model is asked for a lead
    line; this is the fallback that keeps a subject from being empty."""
    for raw in (body or "").split("\n"):
        line = raw.strip().lstrip("#").strip()
        if line and not line.startswith(("-", "*")):
            return line[:120]
    return "What moved in medical AI"


async def _email_digest(kind: str, body: str) -> int:
    """Mail the digest to members whose preference matches this run.

    News is the daily habit; papers ride the weekly preference. Members who have
    never been asked get the default the moment their prefs row is created,
    which happens here on first read.
    """
    from email_utils import is_email_transport_configured, send_html_email  # noqa: PLC0415
    from onboarding_emails import build_community_news_digest_email  # noqa: PLC0415
    from community.router import member_map  # noqa: PLC0415

    if not is_email_transport_configured():
        return 0

    # When the morning routine is on it owns the daily email, and this digest
    # is one of the things it carries. Two automated emails on the same morning
    # from the same product is one too many, and the one people would unsubscribe
    # from is whichever arrived second. The in-app post still happens.
    from community import morning as _cmorning  # noqa: PLC0415

    if _cmorning.enabled():
        log.info("[digest] morning routine owns the daily email; skipping the digest send")
        return 0
    cstore = get_community_store()
    weekly = kind == "papers"
    headline = _headline_from(body)

    sent = 0
    for uid, member in (member_map(include_email=True) or {}).items():
        email = (member or {}).get("email")
        if not email:
            continue
        prefs = cstore.email_prefs(uid)
        want = "weekly" if weekly else "daily"
        if prefs.get("news_frequency") != want:
            continue
        unsub = links.unsubscribe_url(prefs.get("unsubscribe_token") or "")
        try:
            ok = await send_html_email(
                email,
                headline,
                build_community_news_digest_email(
                    first_name=((member.get("display_name") or "").split() or ["there"])[0],
                    headline=headline,
                    body_markdown=body,
                    community_url=links.community_url(),
                    unsubscribe_url=unsub,
                ),
            )
            if ok:
                sent += 1
        except Exception:
            log.warning("[digest] email failed for one recipient", exc_info=True)
    log.info("[digest] %s emailed to %d member(s)", kind, sent)
    return sent


async def run_digest(kind: str, *, claim_window: Optional[str] = None) -> Dict[str, Any]:
    """One full digest run. Never raises — the outcome lands in
    ``community_digest_runs`` and the returned summary dict.

    ``claim_window`` names the scheduling window this run should RESERVE, and
    the scheduler is the only caller that passes one. Reserving matters because
    ``_due`` is a read: two runners can both find today's digest outstanding,
    both curate (one LLM call each), and both post. The loser of the reservation
    returns ``outcome='already_running'`` having spent nothing.

    A manual trigger passes nothing and always runs, which is the point of a
    manual trigger: the operator, not the ledger, decided.
    """
    if kind not in ("news", "papers"):
        return {"ok": False, "error": f"unknown digest kind {kind!r}"}
    cstore = get_community_store()
    run_id = cstore.claim_digest_run(kind, window_key=claim_window)
    if run_id is None:
        log.info("[digest] %s already claimed for %s", kind, claim_window)
        return {"ok": True, "kind": kind, "outcome": "already_running",
                "fetched": 0, "posted": 0, "emailed": 0}
    fetched = 0
    try:
        items = await _fetch(kind)
        fetched = len(items)
        cstore.upsert_content_items(items)
        # Candidates = every recent still-'new' row for this kind — including
        # items STRANDED by a previously failed run. Without this, a failed
        # day's stories are deduped into oblivion and the retry records a
        # hollow ok run (review finding).
        prefixes = ("pubmed", "arxiv", "medrxiv") if kind == "papers" else ("rss:",)
        fresh = [it for it in cstore.new_content_items(max_age_days=3)
                 if str(it.get("source") or "").startswith(prefixes)]
        if not fresh:
            cstore.finish_digest_run(run_id, ok=True, items_fetched=fetched, items_posted=0)
            log.info("[digest] %s run: nothing fresh (%d fetched) — no post", kind, fetched)
            return {"ok": True, "kind": kind, "fetched": fetched, "fresh": 0,
                    "posted": 0, "emailed": 0}

        body, summaries = await _curate(kind, fresh)
        if body is None:
            # Fresh items, none worth keeping — a valid quiet day.
            cstore.mark_content_items(
                [it["id"] for it in fresh], status="skipped", summaries=summaries)
            cstore.finish_digest_run(run_id, ok=True, items_fetched=fetched, items_posted=0)
            log.info("[digest] %s run: %d fresh, none kept — no post", kind, len(fresh))
            return {"ok": True, "kind": kind, "fetched": fetched,
                    "fresh": len(fresh), "posted": 0, "emailed": 0}

        posted = await post_system_message(
            channel_slug=DIGEST_CHANNEL, body=body,
            kind=("digest_papers" if kind == "papers" else "digest_news"),
        )
        if posted is None:
            raise RuntimeError("system post was skipped (channel or PHI gate)")

        # Email fan-out, AFTER the in-app post succeeded. Ordering matters: the
        # channel post is the durable record, and mailing a digest that failed
        # to post would point people at a discussion that does not exist.
        emailed = 0
        try:
            emailed = await _email_digest(kind, body)
        except Exception:
            log.exception("[digest] email fan-out failed (the post stands)")

        kept_ids = [iid for iid, s in summaries.items() if s.get("kept")]
        other_ids = [it["id"] for it in fresh if it["id"] not in set(kept_ids)]
        cstore.mark_content_items(kept_ids, status="posted",
                                  posted_message_id=posted["id"], summaries=summaries)
        cstore.mark_content_items(other_ids, status="skipped", summaries=summaries)
        cstore.finish_digest_run(run_id, ok=True, items_fetched=fetched,
                                 items_posted=len(kept_ids))
        log.info("[digest] %s run: posted %d of %d fresh (message %s)",
                 kind, len(kept_ids), len(fresh), posted["id"])
        return {"ok": True, "kind": kind, "fetched": fetched, "fresh": len(fresh),
                "posted": len(kept_ids), "emailed": emailed, "message_id": posted["id"]}
    except Exception as exc:
        cstore.finish_digest_run(run_id, ok=False, items_fetched=fetched,
                                 error=str(exc)[:500])
        log.warning("[digest] %s run failed: %s", kind, exc, exc_info=True)
        fails = cstore.consecutive_digest_failures(kind)
        if fails >= 3:
            log.error("[digest] ADMIN ATTENTION: %s digest has failed %d consecutive runs",
                      kind, fails)
        return {"ok": False, "kind": kind, "error": str(exc)[:500]}


# ─── The daily staff spotlight ───────────────────────────────────────────────
# One story a day, for the team, in a room members cannot see. Two reasons it
# is a channel rather than a Slack message or a standup habit: it is where the
# team is already reading, and it is durable, so "what were we saying about
# this in March" has an answer.
#
# It shares the digest's item pool rather than fetching its own. A second
# fetcher would double the feed traffic to say the same thing, and the pool is
# already curated and relevance-scored by the digest's LLM pass.
SPOTLIGHT_CHANNEL = "team-ai-spotlight"
SPOTLIGHT_KIND = "spotlight"
#: A distinct content status, so a story used by the spotlight is not offered
#: to the news digest tomorrow as though it had never been seen.
SPOTLIGHT_STATUS = "spotlight"


def _spotlight_body(item: Dict[str, Any]) -> str:
    """The post. One story, said plainly, with the link on a card below."""
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or item.get("abstract") or "").strip()
    lines = ["**Today in medical AI**", "", f"**{title}**"]
    if summary:
        lines += ["", summary[:600]]
    return "\n".join(lines)


def _spotlight_card(item: Dict[str, Any]) -> Dict[str, Any]:
    url = str(item.get("url") or "").strip()
    from urllib.parse import urlparse  # noqa: PLC0415

    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:  # noqa: BLE001
        host = ""
    return {
        "title": str(item.get("title") or "").strip()[:200],
        "url": url,
        "domain": host[4:] if host.startswith("www.") else host,
        "description": str(item.get("summary") or item.get("abstract") or "").strip()[:400],
        "meta": str(item.get("source") or "")[:160],
        "prompt": "",
    }


async def run_spotlight_digest(*, force: bool = False) -> Dict[str, Any]:
    """One story a day into the staff room. Never raises.

    Due-ness rides the same ledger as every other digest kind, which is what
    makes "one a day regardless of run order" true: whichever of the news
    digest and the spotlight fires first, the second finds the day's spotlight
    row already recorded and posts nothing extra.
    """
    cstore = get_community_store()
    now = datetime.utcnow()
    if not force and not _due(SPOTLIGHT_KIND, now,
                              cstore.last_successful_run_at(SPOTLIGHT_KIND)):
        return {"ok": True, "kind": SPOTLIGHT_KIND, "outcome": "not_due", "posted": 0}

    # Same reservation as the digests: the due check is a read two runners can
    # both pass, so the day is claimed before anything is composed. A forced run
    # claims nothing.
    run_id = cstore.claim_digest_run(
        SPOTLIGHT_KIND, window_key=None if force else _window_key(now))
    if run_id is None:
        log.info("[spotlight] already claimed for %s", _window_key(now))
        return {"ok": True, "kind": SPOTLIGHT_KIND, "outcome": "already_running",
                "posted": 0}
    try:
        pool = cstore.candidate_items_for_spotlight()
        if not pool:
            cstore.finish_digest_run(run_id, ok=True, items_posted=0)
            log.info("[spotlight] nothing in the pool, no post")
            return {"ok": True, "kind": SPOTLIGHT_KIND, "outcome": "quiet", "posted": 0}

        for item in pool:
            posted = await post_system_message(
                channel_slug=SPOTLIGHT_CHANNEL,
                body=_spotlight_body(item),
                kind=SPOTLIGHT_KIND,
                cards=[_spotlight_card(item)],
            )
            if posted is not None:
                cstore.mark_content_items([item["id"]], status=SPOTLIGHT_STATUS,
                                          posted_message_id=posted["id"])
                cstore.finish_digest_run(run_id, ok=True, items_fetched=len(pool),
                                         items_posted=1)
                log.info("[spotlight] posted %r (message %s)",
                         item.get("title"), posted["id"])
                return {"ok": True, "kind": SPOTLIGHT_KIND, "outcome": "posted",
                        "posted": 1, "message_id": posted["id"]}
            # None means either the channel is gone or this item's text tripped
            # the PHI gate. A missing channel fails the run outright. A gated
            # item must leave the pool before we move on: 'skipped' rows stay
            # spotlight candidates, so without a terminal status the same story
            # would be re-picked and re-fail every tick for its whole window.
            channel = cstore.get_channel_by_slug(SPOTLIGHT_CHANNEL)
            if not channel or not channel.get("is_active", 1):
                raise RuntimeError("system post was skipped (channel missing or inactive)")
            cstore.mark_content_items([item["id"]], status="blocked")
            log.warning("[spotlight] item %s (%r) blocked by the PHI gate, "
                        "trying the next candidate", item["id"], item.get("title"))
        cstore.finish_digest_run(run_id, ok=True, items_fetched=len(pool), items_posted=0)
        log.info("[spotlight] every candidate was gated, no post")
        return {"ok": True, "kind": SPOTLIGHT_KIND, "outcome": "quiet", "posted": 0}
    except Exception as exc:
        cstore.finish_digest_run(run_id, ok=False, error=str(exc)[:500])
        log.warning("[spotlight] run failed: %s", exc, exc_info=True)
        return {"ok": False, "kind": SPOTLIGHT_KIND, "error": str(exc)[:500]}


# ─── Scheduler (in-process, restart-safe, gated OFF by default) ──────────────
def news_enabled() -> bool:
    # Defaults to OFF. Set COMMUNITY_NEWS_ENABLED=1 to run the scheduled loop.
    # Startup logs which way this resolved: an unset variable used to disable
    # the whole pipeline in total silence, which is how it stayed off in
    # production for weeks without anyone being able to tell.
    return (os.getenv("COMMUNITY_NEWS_ENABLED") or "0").strip() in ("1", "true", "yes", "on")


def _news_hour_utc() -> int:
    return min(23, _int_env("COMMUNITY_DIGEST_NEWS_HOUR_UTC", 13, floor=0))


def _papers_dow() -> int:  # 0 = Monday (Python weekday)
    return min(6, _int_env("COMMUNITY_DIGEST_PAPERS_DOW", 0, floor=0))


def _window_key(now: datetime) -> str:
    """The window a scheduled digest run reserves: the UTC date.

    UTC because ``_due`` is computed in UTC too (the fire hour is a UTC hour),
    and a window that disagreed with the due check about which day it is would
    let both runners through on one side of the boundary."""
    return now.date().isoformat()


def _due(kind: str, now: datetime, last_ok_started: Optional[str]) -> bool:
    """Due when past today's fire time and the newest successful run started
    before it. Derived from ``community_digest_runs`` — restarts cannot
    double-post."""
    if now.hour < _news_hour_utc():
        return False
    if kind == "papers" and now.weekday() != _papers_dow():
        return False
    fire_at = now.replace(hour=_news_hour_utc(), minute=0, second=0, microsecond=0)
    if not last_ok_started:
        return True
    try:
        last = datetime.fromisoformat(last_ok_started.rstrip("Z"))
    except ValueError:
        return True
    return last < fire_at


_loop_task: Optional[asyncio.Task] = None
_TICK_SEC = 900  # 15 min


def start_content_loop() -> None:
    """Start (once) the digest scheduler. Called from app startup ONLY when
    ``COMMUNITY_NEWS_ENABLED=1``."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return

    async def _tick_one_realm() -> None:
        cstore = get_community_store()
        now = datetime.utcnow()
        for kind in ("news", "papers"):
            if not _due(kind, now, cstore.last_successful_run_at(kind)):
                continue
            # Failure backoff: after a failed attempt, wait 2h before
            # retrying (not every tick) — an all-day-broken source or a
            # missing API key must not hammer the LLM 40× a day. The
            # manual trigger bypasses this deliberately.
            last_try = cstore.last_run_attempt_at(kind)
            if last_try:
                try:
                    since = (now - datetime.fromisoformat(
                        last_try.rstrip("Z"))).total_seconds()
                except ValueError:
                    since = None
                if since is not None and since < 7200 and \
                        cstore.consecutive_digest_failures(kind) > 0:
                    continue
            await run_digest(kind, claim_window=_window_key(now))
        # After the digests, so on a normal day the spotlight is
        # choosing from a pool the news run has already scored and
        # marked. It reads 'skipped' rows too, so the reverse order
        # costs it nothing.
        await run_spotlight_digest()

    async def _run() -> None:
        while True:
            await asyncio.sleep(_TICK_SEC)
            # Sandbox PRD §1.4: the u-system digests run in the sandbox too,
            # into the sandbox community DB (its own run ledger, so the two
            # realms never double-post or block each other).
            for r in _realm.active_realms():
                try:
                    with _realm.scoped(r):
                        await _tick_one_realm()
                except Exception:  # pragma: no cover — the loop must survive
                    log.warning("[digest] scheduler tick failed (%s)", r, exc_info=True)

    _loop_task = asyncio.get_running_loop().create_task(_run())
    log.info("[digest] content loop started (news daily %02d:00 UTC, papers weekly dow=%d)",
             _news_hour_utc(), _papers_dow())


def loop_running() -> bool:
    """True when the scheduler task is actually alive.

    Deliberately distinct from ``news_enabled()``: the gate reports what the
    environment asked for, this reports what the process is doing. They differ
    when startup raised after the gate passed, so a status surface must show
    both rather than infer one from the other.
    """
    return _loop_task is not None and not _loop_task.done()


def stop_content_loop() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
