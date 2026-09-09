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

The scheduled loop reads ``COMMUNITY_NEWS_ENABLED``, which now defaults to ON;
setting it to 0 is the operator's kill switch, and the internal trigger
endpoint fires a run on demand either way. It used to default to OFF, which
dated from when the community was empty and no bot-authored post belonged in
it. That stopped being true and the default did not follow, so the pipeline sat
dormant in production looking exactly like a quiet week.
``/internal/community/status`` still reports which way the gate resolved and
whether the loop actually started, because those two can differ.

Every run also records WHY it posted nothing. ``ok=1, items_posted=0`` is
written for a real quiet day and for a run with no model key, and an operator
cannot tell a dead pipeline from a slow news week without the reason beside the
count.
"""

from __future__ import annotations

import asyncio
import logging
import os
import realm as _realm
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from community import digest_contract, feeds, links
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
    "\"one_liner\": \"<=25 words, factual, no hype, say what happened, never invent\"}]}. "
    "Every input id must appear exactly once. Do not add fields or prose."
)

# The compose pass returns STRUCTURE, and the product renders it (PRD §2.2).
# It used to return "markdown-lite" prose, which the web rendered as a subset of
# markdown, the email rendered as literal asterisks and brackets, and the
# notification snippet rendered as a whitespace-collapsed copy of the raw
# string. Three renderings of one blob, none of them designed.
#
# Shaping kept items is also a SMALLER job than writing a post: the selection
# pass has already decided what is in and written the factual one-liner, so this
# pass rewrites two short fields per item and picks a section. Every rule below
# is also enforced by ``digest_contract`` after the call, because a prompt is a
# request and a validator is a guarantee.
_COMPOSE_SYSTEM = (
    "You shape a news digest for a private community of verified physicians. "
    "Input: a JSON list of kept items (title, url, one_liner, source). Output: "
    "ONLY a JSON object {\"items\": [{\"headline\", \"deck\", \"why_it_matters\", "
    "\"source\", \"url\", \"section\", \"urgent\", \"urgent_kind\"}]}. No prose, no "
    "markdown, no code fence.\n"
    "Rules, all enforced by a validator that DISCARDS the whole run on a "
    "violation:\n"
    "- Return 3 to 5 items. Never more. Choose the strongest; drop the rest.\n"
    "- headline: aim for 8 words and never exceed 10, plain declarative, "
    "sentence case, no trailing period, a verb in every one. Say what happened, "
    "not why it is interesting.\n"
    "- deck: aim for 20 words and never exceed 25. ONE sentence, ONE fact, and "
    "it must NOT repeat the headline. Write one for every item; only the lead "
    "story keeps its deck.\n"
    "- why_it_matters: aim for 12 words and never exceed 14. ONE sentence, "
    "second person allowed, written the way you would say it to a physician "
    "friend: what changes for patient care or for AI evaluation. Then cut it in "
    "half.\n"
    "- urgent: false on every item unless a SAME-DAY event of one of exactly "
    "four kinds happened, in which case also set urgent_kind to that word: "
    "'regulatory' (a decision, clearance or approval), 'safety' (a recall or "
    "safety notice), 'trial' (a major clinical trial readout), 'release' (a lab "
    "or model release carrying a medical claim). Nothing else is urgent. A few "
    "times a month, not most mornings.\n"
    "- Banned everywhere: em dash and en dash (use a comma or a period), "
    "hashtags, asterisks, emoji, exclamation marks, and the words game-changer, "
    "revolutionary, exciting, groundbreaking, breakthrough, unprecedented.\n"
    "- No calendar dates anywhere (the platform timestamps the post, and a full "
    "date false-trips the clinical PHI filter). Say 'this week' or a month and "
    "year instead.\n"
    "- section: exactly one of Research, Regulation, Deployment, Evals, Opinion. "
    "Never invent a section.\n"
    "- source: the publisher's name as a person would say it (STAT, Nature "
    "Medicine, JAMA), never a URL host like statnews.com.\n"
    "- url: copy the input url exactly. Never invent a fact or an item that is "
    "not in the input, and never repeat a url."
)


async def _curate(kind: str, items: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    """Two LLM passes. Returns ``(payload | None, {item_id: {summary, relevance}})``.

    ``payload`` is the validated §2.2 structure (``digest_contract``), not a post
    body: the product renders the card, the email and the plain-text body from
    it, so there is one description of the digest and three views of it.

    ``None`` = nothing worth posting (a valid quiet day). A parse failure or a
    contract violation RAISES — the caller records the run as failed and posts
    nothing, which is what a malformed digest has always done. Half a digest is
    worse than none, because nobody goes looking for the one that is missing.
    """
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
        purpose="community news digest: select/score items",
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
            # What the SELECT pass decided, kept separately from what actually
            # reached the channel. The two differ on any day the compose pass
            # trims to the cap or the run stops below the floor, and conflating
            # them is what retired stories that were never even offered.
            "selected": bool(r.get("keep")),
        }
        if r.get("keep"):
            kept.append({**by_id[iid], **summaries[iid]})
    kept.sort(key=lambda x: -(x.get("relevance") or 0.0))
    kept = kept[: max_items()]
    if not kept:
        return None, summaries

    # Below the floor there is no digest to shape, so the compose call is not
    # made at all: a two-item day is a quiet day, and spending a model call to
    # be told so is the kind of cost that only shows up on the bill.
    if len(kept) < digest_contract.MIN_ITEMS:
        log.info("[digest] %s: %d item(s) kept, floor is %d; treating as a quiet day",
                 kind, len(kept), digest_contract.MIN_ITEMS)
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
        purpose="community news digest: compose post",
        temperature=0.2,
        max_tokens=max_tokens(),
    )
    raw = first_text(resp2) or ""
    if not raw.strip():
        raise ValueError("digest compose pass returned empty text")
    composed = extract_json(raw)
    if composed is None:
        raise ValueError("digest compose pass returned unparseable JSON")
    # Raises DigestContractError on a violation, which the caller records as a
    # failed run. Deliberately NOT repaired into something publishable: the
    # rules exist because an unconstrained model wrote the post, and a
    # post-processor that quietly rewrites a 30-word headline is the same
    # problem with an extra step.
    payload = digest_contract.validate_payload(composed, kind=kind)

    # The TOP STORY (§2.2). ``kept`` is already sorted by the select pass's
    # relevance, so the lead is its first entry — matched into the payload on
    # the NORMALISED url for the same reason the provenance join below is: a
    # trailing slash or a re-encoded character is the same story everywhere else
    # in this pipeline, and an exact compare would silently promote item one
    # instead. A compose pass that dropped the top story leaves no match, and
    # ``mark_lead`` falls back to the model's own first item.
    lead_url = None
    if kept:
        wanted = feeds.normalize_url(kept[0].get("url") or "")
        lead_url = next(
            (i["url"] for i in payload["items"]
             if feeds.normalize_url(i["url"]) == wanted), None)
    digest_contract.mark_lead(payload, lead_url)

    # Which fetched items actually reached the post. Matched on the NORMALISED
    # url — the same identity ``upsert_content_items`` dedups on — because an
    # exact string compare is a join on the model's typing. A trailing slash, a
    # dropped tracking parameter or a percent-encoded character is the same
    # story to every other part of this pipeline, and to an exact compare it is
    # a different one: the digest posts, nothing matches, and the run records
    # "posted 0" beside a message that is sitting in the channel, with the
    # provenance link from story to message lost for good.
    posted_urls = {feeds.normalize_url(i["url"]) for i in payload["items"]}
    for k in kept:
        if feeds.normalize_url(k.get("url") or "") in posted_urls:
            summaries[k["id"]]["posted"] = True
    return payload, summaries


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
                log.warning("[digest] paper source raised: skipped: %s", b)
        return _keyword_filter(items, require=False)
    items = await feeds.fetch_rss()
    return _keyword_filter(items, require=True)


# ─── Who mails the digest ────────────────────────────────────────────────────
# ONE SENDER, and it is the notification queue.
#
# There used to be two. ``post_system_message(announce=True)`` queues a ``post``
# notification for every member and ``notify.flush_pending`` mails it, and
# ``_email_digest`` ALSO walked the member map and sent the same thing directly.
# The two were kept apart by ``_email_digest`` switching itself off whenever the
# morning routine was enabled — which is the default — so in production only the
# queue ever sent, and the second sender was dead code that would wake up the
# moment somebody set COMMUNITY_MORNING_ENABLED=0 and mail every physician the
# same digest twice.
#
# That guard was aimed at the right problem and hit the wrong target. The
# morning routine has no mailer of its own; it posts with ``announce=True`` and
# rides the same queue. So "the morning owns the daily email" was never a reason
# for a second sender to exist, only a reason for it to be quiet, and a sender
# whose correctness depends on staying switched off is a duplicate waiting for a
# configuration change.
#
# The queue does everything this did and does it better: it batches per member
# per flush, it retries a failed send instead of dropping it, it counts attempts
# and gives up loudly, and ``notify._wants_digest`` applies the same news-cadence
# rule this applied (news daily, papers weekly, off takes both). The one thing it
# does differently is timing — the mail goes out on the next flush rather than
# inside the run — and for a daily digest that is not a property worth a second
# code path to preserve.


# ─── Why a run posted nothing ────────────────────────────────────────────────
# Same argument as the morning's: a successful run with zero items is written
# for a quiet week and for a pipeline that cannot reach a model, and only the
# reason tells them apart.
REASON_POSTED = "posted"
REASON_NOTHING_FETCHED = "no_source_items"
REASON_NOTHING_FRESH = "nothing_new"
REASON_NOTHING_KEPT = "nothing_worth_posting"
#: The selector found something, but fewer stories than a digest is worth
#: posting. Distinct from ``nothing_worth_posting`` because the fix is
#: different: one says the news was thin, the other says the sources were.
REASON_BELOW_FLOOR = "below_item_floor"
REASON_NO_MODEL_KEY = "no_model_key"
REASON_ERROR = "run_failed"
REASON_BLOCKED = "post_blocked"
#: The compose pass answered, and what it answered will not be published.
#: Distinct from ``run_failed`` because the fix is different in kind: the
#: pipeline is healthy, the model is reachable, and the post was refused on its
#: contents. Without it, a week of contract violations reads on the admin card
#: exactly like a week of network errors.
REASON_CONTRACT = "contract_violation"


def _failure_reason(exc: Optional[BaseException] = None) -> str:
    """The reason behind a raised run. A missing key is the one worth naming:
    every LLM call fails identically without it, and the fix is one variable."""
    if isinstance(exc, digest_contract.DigestContractError):
        return REASON_CONTRACT
    if not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        return REASON_NO_MODEL_KEY
    return REASON_ERROR


def _settle_items(cstore: Any, fresh: List[Dict[str, Any]],
                  summaries: Dict[int, Dict[str, Any]],
                  *, message_id: Optional[int] = None) -> List[int]:
    """Record what happened to every item this run looked at. Returns the ids
    that reached the channel.

    Three outcomes, and the middle one is the whole point:

    * ``posted``  — it is in the message. Carries ``posted_message_id``, which
      is the only link from a story back to the post that carried it.
    * ``new``     — the selector wanted it and the digest had no room, or the
      run stopped below the floor. STILL A CANDIDATE. Marking these ``skipped``
      is a pool-starvation bug: a feed that yields two strong stories a day
      never reaches the three-item floor, and burning both means tomorrow starts
      from nothing and the digest never posts again while reporting a healthy
      "nothing worth posting" every single day. ``new_content_items`` already
      bounds the retry window to three days, so nothing accumulates forever.
    * ``skipped`` — the selector judged it not worth posting. That is a decision
      about the story, so it retires.
    """
    posted_ids = [iid for iid, s in summaries.items() if s.get("posted")]
    posted_set = set(posted_ids)
    held, retired = [], []
    for it in fresh:
        iid = it["id"]
        if iid in posted_set:
            continue
        (held if (summaries.get(iid) or {}).get("selected") else retired).append(iid)
    if posted_ids:
        cstore.mark_content_items(posted_ids, status="posted",
                                  posted_message_id=message_id, summaries=summaries)
    # status="new" is a no-op on the row's candidacy and still writes the
    # summary and relevance the select pass produced, so a held item arrives at
    # tomorrow's run already scored.
    cstore.mark_content_items(held, status="new", summaries=summaries)
    cstore.mark_content_items(retired, status="skipped", summaries=summaries)
    if held:
        log.info("[digest] %d item(s) held for the next run (selected, not posted)",
                 len(held))
    return posted_ids


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
            reason = REASON_NOTHING_FETCHED if not fetched else REASON_NOTHING_FRESH
            cstore.finish_digest_run(run_id, ok=True, items_fetched=fetched,
                                     items_posted=0, reason=reason)
            log.info("[digest] %s run: nothing fresh (%d fetched), no post", kind, fetched)
            return {"ok": True, "kind": kind, "fetched": fetched, "fresh": 0,
                    "posted": 0, "emailed": 0, "reason": reason}

        payload, summaries = await _curate(kind, fresh)
        if payload is None:
            # No post today. Which is NOT the same as "none of these stories was
            # any good": the run also lands here when the selector kept one or
            # two and the digest floor is three. Those are held, not burned.
            _settle_items(cstore, fresh, summaries)
            held = sum(1 for it in fresh
                       if (summaries.get(it["id"]) or {}).get("selected"))
            reason = REASON_BELOW_FLOOR if held else REASON_NOTHING_KEPT
            cstore.finish_digest_run(run_id, ok=True, items_fetched=fetched,
                                     items_posted=0, reason=reason)
            log.info("[digest] %s run: %d fresh, %d selected, no post (%s)",
                     kind, len(fresh), held, reason)
            return {"ok": True, "kind": kind, "fetched": fetched,
                    "fresh": len(fresh), "posted": 0, "emailed": 0,
                    "reason": reason}

        posted = await post_system_message(
            channel_slug=DIGEST_CHANNEL,
            # The body is a plain-text rendering of the payload, not the post.
            # It exists so the row is searchable, so a client that predates the
            # card still shows something true, and so the PHI gate has every
            # human-visible character in one string to scan.
            body=digest_contract.plain_text_body(payload),
            kind=("digest_papers" if kind == "papers" else "digest_news"),
            payload=payload,
            # The digest is a bot post in a room nobody is watching at 13:00
            # UTC; without this it produced no notification row at all.
            announce=True,
        )
        if posted is None:
            # The write path refused it: the PHI gate found something, or the
            # body broke the digest style rules. Both are a BLOCKED POST, which
            # is why REASON_BLOCKED exists; letting it raise made it read as
            # "the run raised" on the admin card, i.e. indistinguishable from a
            # network error, which is exactly the confusion REASON_CONTRACT was
            # added to end one commit earlier.
            _settle_items(cstore, fresh, summaries)
            cstore.finish_digest_run(run_id, ok=False, items_fetched=fetched,
                                     items_posted=0, error="post_blocked",
                                     reason=REASON_BLOCKED)
            log.error("[digest] %s run: the post was refused by the write path "
                      "(PHI gate or style rules); nothing posted", kind)
            return {"ok": False, "kind": kind, "fetched": fetched,
                    "fresh": len(fresh), "posted": 0, "emailed": 0,
                    "reason": REASON_BLOCKED}

        # The email fan-out already happened, inside ``post_system_message``:
        # ``announce=True`` queued a notification for every member, and the
        # notify flush turns that into the designed digest email. Ordering is
        # still right for the reason it always was — the channel post is the
        # durable record, and mail that pointed at a discussion which failed to
        # post would be a link to nothing — but it is now guaranteed by
        # construction rather than by a second call placed after this one.
        #
        # ``emailed`` is what the queue accepted, not what a transport
        # confirmed. A count of delivered mail is not knowable here any more,
        # and reporting the recipients as if it were is how a run summary
        # starts lying about a broken transport.
        kept_ids = _settle_items(cstore, fresh, summaries, message_id=posted["id"])
        cstore.finish_digest_run(run_id, ok=True, items_fetched=fetched,
                                 items_posted=len(kept_ids), reason=REASON_POSTED)
        log.info("[digest] %s run: posted %d of %d fresh (message %s)",
                 kind, len(kept_ids), len(fresh), posted["id"])
        return {"ok": True, "kind": kind, "fetched": fetched, "fresh": len(fresh),
                "posted": len(kept_ids), "emailed": None, "message_id": posted["id"],
                "email_queued": True}
    except Exception as exc:
        cstore.finish_digest_run(run_id, ok=False, items_fetched=fetched,
                                 error=str(exc)[:500], reason=_failure_reason(exc))
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
            cstore.finish_digest_run(run_id, ok=True, items_posted=0,
                                     reason=REASON_NOTHING_FRESH)
            log.info("[spotlight] nothing in the pool, no post")
            return {"ok": True, "kind": SPOTLIGHT_KIND, "outcome": "quiet", "posted": 0}

        for item in pool:
            posted = await post_system_message(
                channel_slug=SPOTLIGHT_CHANNEL,
                body=_spotlight_body(item),
                kind=SPOTLIGHT_KIND,
                cards=[_spotlight_card(item)],
                # Staff-only, and the fan-out knows it: ``channel_member_ids``
                # narrows a staff_only room to staff, so this mails the team
                # and nobody else.
                announce=True,
            )
            if posted is not None:
                cstore.mark_content_items([item["id"]], status=SPOTLIGHT_STATUS,
                                          posted_message_id=posted["id"])
                cstore.finish_digest_run(run_id, ok=True, items_fetched=len(pool),
                                         items_posted=1, reason=REASON_POSTED)
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
        cstore.finish_digest_run(run_id, ok=True, items_fetched=len(pool),
                                 items_posted=0, reason=REASON_BLOCKED)
        log.info("[spotlight] every candidate was gated, no post")
        return {"ok": True, "kind": SPOTLIGHT_KIND, "outcome": "quiet", "posted": 0}
    except Exception as exc:
        cstore.finish_digest_run(run_id, ok=False, error=str(exc)[:500],
                                 reason=_failure_reason(exc))
        log.warning("[spotlight] run failed: %s", exc, exc_info=True)
        return {"ok": False, "kind": SPOTLIGHT_KIND, "error": str(exc)[:500]}


# ─── Scheduler (in-process, restart-safe, gated OFF by default) ──────────────
def news_enabled() -> bool:
    # Defaults to ON, with COMMUNITY_NEWS_ENABLED=0 as the operator kill
    # switch. It defaulted to off, and an unset variable disabled the whole
    # pipeline in total silence, which is how it stayed off in production for
    # weeks without anyone being able to tell. Startup still logs which way it
    # resolved. One definition of "off", shared with the morning gate.
    from community.morning import gate_on  # noqa: PLC0415 - one gate rule

    return gate_on("COMMUNITY_NEWS_ENABLED")


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


def next_run_at(kind: str = "news", *, now: Optional[datetime] = None) -> Optional[str]:
    """When the next scheduled digest of ``kind`` is due, ISO-8601 UTC.

    ASKS ``_due``, rather than reimplementing the calendar beside it. An earlier
    version computed "today's fire time, or tomorrow's if that has passed",
    which is a different question and gives a different answer for most of the
    day: at 14:30 UTC with yesterday's run the newest successful one, ``_due``
    says the digest is outstanding RIGHT NOW while the arithmetic says tomorrow.
    The empty ``#medical-ai-news`` then told a physician the next run was
    tomorrow afternoon during exactly the window the message exists to explain,
    and went on saying it all day after a failed run, because ``_due`` stays
    true until a run succeeds.

    ``None`` when the routine is switched off: "the next one is at 13:00" is
    false then, and an empty room should say nothing rather than something
    wrong.
    """
    if not news_enabled():
        return None
    now = now or datetime.utcnow()
    try:
        last_ok = get_community_store().last_successful_run_at(kind)
    except Exception:  # noqa: BLE001 - a schedule line is not worth an exception
        last_ok = None
    # Outstanding right now: the honest answer is this window, not the next one.
    if _due(kind, now, last_ok):
        fire = now.replace(hour=_news_hour_utc(), minute=0, second=0, microsecond=0)
        return fire.isoformat() + "Z"

    fire = now.replace(hour=_news_hour_utc(), minute=0, second=0, microsecond=0)
    if now >= fire:
        fire = fire + timedelta(days=1)
    if kind == "papers":
        # Forward to the next occurrence of the papers weekday, counting the
        # candidate day itself when its fire time has not passed.
        fire = fire + timedelta(days=(_papers_dow() - fire.weekday()) % 7)
    return fire.isoformat() + "Z"


async def run_scheduled_digest(
    kind: str, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """One tick of the digest SCHEDULE. Safe to call every hour, by anything.

    ``run_digest`` reserves no window and therefore always runs, which is right
    for a manual trigger (the operator decided) and exactly wrong for a cron:
    an hourly job pointed at it would post a fresh digest every hour. This
    applies the three checks the in-process loop applies and nothing else, so
    an external scheduler and the loop drive identical behaviour and cannot
    both post: due today, not inside the failure backoff, and the day reserved
    before anything is composed.
    """
    if kind not in ("news", "papers"):
        return {"ok": False, "kind": kind, "error": f"unknown digest kind {kind!r}"}
    cstore = get_community_store()
    at = now or datetime.utcnow()
    if not _due(kind, at, cstore.last_successful_run_at(kind)):
        return {"ok": True, "kind": kind, "outcome": "not_due", "posted": 0}
    # Failure backoff: after a failed attempt, wait 2h before retrying (not
    # every tick) — an all-day-broken source or a missing API key must not
    # hammer the LLM 40× a day. The manual trigger bypasses this deliberately.
    last_try = cstore.last_run_attempt_at(kind)
    if last_try:
        try:
            since = (at - datetime.fromisoformat(last_try.rstrip("Z"))).total_seconds()
        except ValueError:
            since = None
        if since is not None and since < 7200 and \
                cstore.consecutive_digest_failures(kind) > 0:
            return {"ok": True, "kind": kind, "outcome": "backing_off", "posted": 0}
    return await run_digest(kind, claim_window=_window_key(at))


_loop_task: Optional[asyncio.Task] = None
_TICK_SEC = 900  # 15 min


def start_content_loop() -> None:
    """Start (once) the digest scheduler. Called from app startup ONLY when
    ``COMMUNITY_NEWS_ENABLED=1``."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return

    async def _tick_one_realm() -> None:
        for kind in ("news", "papers"):
            await run_scheduled_digest(kind)
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
