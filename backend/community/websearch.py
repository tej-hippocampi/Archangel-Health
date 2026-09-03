"""Finding things worth a doctor's morning, from the open web.

``feeds.py`` already pulls PubMed, arXiv, medRxiv and a handful of RSS feeds,
which is the right way to get papers. It is the wrong way to get everything
else: nobody publishes an RSS feed of "nephrology conferences in Saudi Arabia
in the next two months", and the events, grants and fellowships a physician
would actually click are scattered across society pages, university calendars
and Luma listings that no aggregator covers.

So this module asks a model with a web-search tool, and then refuses to trust
it. Every returned URL must appear in the search tool's own citations before
it survives: a plausible-looking conference at a plausible-looking URL that
does not exist is worse than an empty channel, because the first one costs a
doctor their attention and their trust and the second costs nothing.

The only place in the community that calls the model with a search tool. Every
function returns a list and never raises -- a bad morning for a search API is
a quiet channel, not a broken run.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("community.websearch")

#: The server-side web-search tool. Declared here rather than inline so the
#: one place that decides how much searching a run may do is visible.
_SEARCH_TOOL_TYPE = "web_search_20250305"


def max_uses() -> int:
    try:
        return max(1, int(os.getenv("COMMUNITY_WEBSEARCH_MAX_USES", "5")))
    except (TypeError, ValueError):
        return 5


def daily_call_cap() -> int:
    """Paid searches allowed per provider per UTC day. 0 disables the cap.

    Counts CALLS, not dollars. Per-provider pricing drifts and a spend figure
    this module cannot verify would be worse than no figure: it would read as a
    guarantee. Calls are what we can actually count, and the ledger is durable
    so a restart cannot hand the day a fresh budget.
    """
    try:
        return max(0, int(os.getenv("COMMUNITY_SEARCH_DAILY_CALL_CAP", "40")))
    except (TypeError, ValueError):
        return 40


def enabled() -> bool:
    """True when at least one configured provider can actually be called.

    Composing over the provider list rather than checking ANTHROPIC_API_KEY
    directly, so a deployment running Exa and Firecrawl without an Anthropic
    key still has a morning. The synthesis pass needs a model, so a
    retrieval-only deployment still degrades to nothing; that is stated in
    ``_ask_grounded`` rather than assumed here.
    """
    from community import search_providers as _sp  # noqa: PLC0415

    return any(_sp.available(name) for name in _sp.provider_order())


def _spend(provider: str) -> bool:
    """Claim one call against today's cap. False means "do not call".

    Never raises: a budget-ledger problem must not be the thing that breaks a
    morning, so it fails OPEN. The cap exists to stop a runaway loop, not to
    police a correct one, and refusing every search because SQLite hiccuped
    would be the more expensive failure.
    """
    try:
        from community.store import get_community_store  # noqa: PLC0415

        return get_community_store().claim_search_call(provider, cap=daily_call_cap())
    except Exception:  # noqa: BLE001
        log.warning("[websearch] budget ledger unavailable; allowing the call", exc_info=True)
        return True


def _tool() -> Dict[str, Any]:
    return {"type": _SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": max_uses()}


_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


def _cited_urls(response: Any) -> set:
    """Every URL the search tool actually returned.

    The allowlist a model's answer is checked against. Read defensively: the
    response shape is the provider's, not ours, and a shape we do not
    recognize has to mean "cite nothing" rather than "trust everything".
    """
    found: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "url" and isinstance(value, str):
                    found.add(_normalize(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            # Search results sometimes arrive as text blocks carrying URLs.
            for match in _URL_RE.findall(node):
                found.add(_normalize(match))

    try:
        content = getattr(response, "content", None)
        walk(content if content is not None else response)
    except Exception:  # noqa: BLE001
        return set()
    return found


def _normalize(url: str) -> str:
    u = (url or "").strip().rstrip(".,);]")
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I)
    return u.rstrip("/").lower()


def _text_of(response: Any) -> str:
    """Concatenated text blocks of a model response."""
    try:
        parts = []
        for block in (getattr(response, "content", None) or []):
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    except Exception:  # noqa: BLE001
        return ""


def _parse_items(text: str) -> List[Dict[str, Any]]:
    """Pull the JSON array out of a model answer.

    Tolerant of a fenced block or a sentence of preamble, strict about the
    result being a list of objects.
    """
    if not text:
        return []
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    start, end = body.find("["), body.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        parsed = json.loads(body[start:end + 1])
    except ValueError:
        return []
    return [p for p in parsed if isinstance(p, dict)] if isinstance(parsed, list) else []


def _keep_cited(items: List[Dict[str, Any]], cited: set) -> List[Dict[str, Any]]:
    """Drop every item whose URL is not in the allowlist.

    The one rule this module exists to enforce, factored out so that BOTH the
    tool-backed path and the retrieval-backed path go through it. A provider
    added later that skips this function is a provider that can publish an
    invented conference into a doctor's morning.
    """
    kept: List[Dict[str, Any]] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if _normalize(url) not in cited:
            log.info("[websearch] dropped an uncited url: %s", url[:120])
            continue
        kept.append(item)
    return kept


async def retrieve(
    query: str, *, limit: int = 8, days: Optional[int] = None,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run the configured paid retrievers and merge what they hold.

    Returns [] when no retrieval provider is configured, which is the signal
    the callers use to fall back to the Anthropic search tool.
    """
    from community import search_providers as _sp  # noqa: PLC0415

    rows: List[Dict[str, Any]] = []
    for name in _sp.provider_order():
        if name == "anthropic" or not _sp.available(name):
            continue
        if not _spend(name):
            log.info("[websearch] %s daily call cap reached; skipping", name)
            continue
        if name == "exa":
            rows.extend(await _sp.search_exa(query, limit=limit, days=days, category=category))
        elif name == "firecrawl":
            rows.extend(await _sp.search_firecrawl(query, limit=limit))
    return _sp.dedupe(rows)


async def _ask_grounded(
    system: str, prompt: str, results: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Select and summarize from RETRIEVED results.

    The allowlist here is a set we built out of a search response, not one
    parsed back out of the model's own prose, so the citation gate is strictly
    stronger than on the tool path: the model is told to answer only with URLs
    from the supplied list, and anything else is dropped regardless.
    """
    if not results:
        return []
    try:
        from ai.llm_client import call_llm  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []

    catalogue = json.dumps(
        [
            {"title": r["title"], "url": r["url"],
             "published": r.get("published") or "", "excerpt": (r.get("snippet") or "")[:600]}
            for r in results[:20]
        ],
        ensure_ascii=False,
    )
    grounded_system = (
        system
        + " You are given SEARCH RESULTS. Use ONLY these results. Every \"url\" "
        "you return must be copied exactly from the supplied list. Do not "
        "search further, do not invent a URL, and if the results do not "
        "support an item, return fewer items."
    )
    try:
        response, _meta = await call_llm(
            role="community_digest",
            system=grounded_system,
            messages=[{"role": "user",
                       "content": f"SEARCH RESULTS:\n{catalogue}\n\nTASK:\n{prompt}"}],
            purpose="community morning content (grounded)",
        )
    except Exception:  # noqa: BLE001
        log.warning("[websearch] grounded call failed", exc_info=True)
        return []

    allow = {_normalize(r["url"]) for r in results}
    return _keep_cited(_parse_items(_text_of(response)), allow)


async def _ask(system: str, prompt: str) -> List[Dict[str, Any]]:
    """One search-backed call, with every uncited URL dropped."""
    from community import search_providers as _sp  # noqa: PLC0415

    if not _sp.available("anthropic") or "anthropic" not in _sp.provider_order():
        return []
    if not _spend("anthropic"):
        log.info("[websearch] anthropic daily call cap reached; skipping")
        return []
    try:
        from ai.llm_client import call_llm  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []

    try:
        response, _meta = await call_llm(
            role="community_digest",
            system=system,
            messages=[{"role": "user", "content": prompt}],
            purpose="community morning content",
            tools=[_tool()],
        )
    except Exception:  # noqa: BLE001 - a quiet channel, never a broken run
        log.warning("[websearch] search call failed", exc_info=True)
        return []

    # The model wrote a URL the search never returned is the failure mode this
    # whole module is arranged around; the gate is shared with the grounded path.
    return _keep_cited(_parse_items(_text_of(response)), _cited_urls(response))


# ─── The four things a morning is made of ────────────────────────────────────
_EVENTS_SYSTEM = (
    "You find real, upcoming events that a practising physician could attend. "
    "Use web search. Only report events you have actually found on a page, "
    "with the registration or information URL from that page. Never invent a "
    "URL, a date or an organiser. If you find fewer than asked, report fewer. "
    "Answer with a JSON array only, each item: "
    '{"title","url","when","location","organizer","why"} where "when" is the '
    'date as written on the page, "location" is a city and country or the word '
    '"Online", and "why" is one sentence on who it is for.'
)


async def search_events(
    *, country_name: Optional[str] = None, specialty: Optional[str] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Conferences, webinars, grand rounds and CME in the next two months."""
    where = f"in or accessible from {country_name}" if country_name else "anywhere in the world"
    focus = f"{specialty} or medical AI" if specialty else "medical AI, clinical AI or health technology"
    prompt = (
        f"Find {limit} upcoming {focus} events for physicians, {where}, happening "
        "in the next 60 days. Prefer a mix: at least one that can be attended "
        "online. Include conferences, summits, webinars, grand rounds and CME "
        "sessions. Return the JSON array only."
    )
    # Events are the reason the paid rung exists: nobody publishes an RSS feed
    # of "nephrology conferences in Saudi Arabia in the next two months".
    results = await retrieve(
        f"upcoming {focus} conference OR summit OR CME for physicians {where} 2026",
        limit=12,
    )
    if results:
        return await _ask_grounded(_EVENTS_SYSTEM, prompt, results)
    return await _ask(_EVENTS_SYSTEM, prompt)


_NEWS_SYSTEM = (
    "You find real, recent news about AI in medicine that a practising "
    "physician would find worth five minutes. Use web search. Only report "
    "stories you have actually found, with the article URL. Never invent a "
    "URL or a headline. Answer with a JSON array only, each item: "
    '{"title","url","summary","prompt"} where "summary" is two sentences a '
    'busy clinician can read instead of the article, and "prompt" is one '
    "question that would start a real discussion among doctors."
)


async def search_news(
    *, country_name: Optional[str] = None, specialty: Optional[str] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    scope = []
    if specialty:
        scope.append(f"relevant to {specialty}")
    if country_name:
        scope.append(f"relevant to physicians practising in {country_name}")
    where = (", ".join(scope)) or "of broad interest to clinicians"
    prompt = (
        f"Find {limit} news stories from the last 7 days about AI in medicine, "
        f"{where}. Prefer things with consequences for practice: regulation, "
        "deployments, trial results, safety findings. Return the JSON array only."
    )
    results = await retrieve(
        f"AI in medicine news {where} regulation deployment trial results",
        limit=12, days=7, category="news",
    )
    if results:
        return await _ask_grounded(_NEWS_SYSTEM, prompt, results)
    return await _ask(_NEWS_SYSTEM, prompt)


_OPPORTUNITY_SYSTEM = (
    "You find real, currently-open opportunities for physicians: paid research "
    "collaborations, grants, fellowships, calls for reviewers or study "
    "participants, and clinician roles in medical AI. Use web search. Only "
    "report opportunities you have actually found, with the application or "
    "information URL. Never invent one. Answer with a JSON array only, each "
    'item: {"title","url","summary","deadline"} where "summary" is two '
    'sentences on what it is and who should apply, and "deadline" is the '
    'closing date as written on the page or "" if none is given.'
)


async def search_opportunities(
    *, country_name: Optional[str] = None, specialty: Optional[str] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    scope = f"in {specialty}" if specialty else "in medical AI or clinical research"
    where = f", open to physicians in {country_name}" if country_name else ""
    prompt = (
        f"Find {limit} currently-open opportunities for physicians {scope}{where}. "
        "Include grants, fellowships, paid research collaboration, and calls for "
        "expert reviewers. Return the JSON array only."
    )
    results = await retrieve(
        f"open grant OR fellowship OR call for reviewers for physicians {scope}{where}",
        limit=12,
    )
    if results:
        return await _ask_grounded(_OPPORTUNITY_SYSTEM, prompt, results)
    return await _ask(_OPPORTUNITY_SYSTEM, prompt)


_DISCUSSION_SYSTEM = (
    "You propose one discussion topic for a community of practising physicians "
    "who evaluate medical AI. Use web search to ground it in something real "
    "and recent. Answer with a JSON array containing exactly one item: "
    '{"title","url","summary","prompt","options"} where "title" is the topic, '
    '"url" is the article or paper it is grounded in, "summary" is two '
    'sentences of context, and "prompt" is the question posed to the room -- '
    "open, specific, and answerable from clinical experience rather than "
    'opinion. "options" is an array of 2 to 4 short stances a physician could '
    "actually hold on that question: each under twelve words, genuinely "
    "different from one another, and none of them the obviously correct "
    'answer. Omit "options" entirely rather than inventing weak ones -- the '
    "prompt is posted as a question without a poll when there is nothing real "
    "to choose between."
)


async def search_discussion_topic(
    *, avoid_titles: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    avoid = ""
    if avoid_titles:
        joined = "; ".join(t for t in avoid_titles[:8] if t)
        if joined:
            avoid = f" Do not repeat any of these recent topics: {joined}."
    prompt = (
        "Propose one discussion topic about where AI in medicine is actually "
        "going: something clinicians disagree about, grounded in a real recent "
        "development." + avoid + " Return the JSON array only."
    )
    # The one item that earns a second step. It runs weekly, it claims to
    # summarize a specific source, and it asks a room of physicians to argue
    # about it -- so it should have READ the source rather than a snippet.
    results = await retrieve(
        "recent development in clinical AI that doctors disagree about",
        limit=10, days=21,
    )
    if not results:
        return await _ask(_DISCUSSION_SYSTEM, prompt)

    from community import search_providers as _sp  # noqa: PLC0415

    if _sp.available("firecrawl") and _spend("firecrawl"):
        for row in results[:3]:
            body = await _sp.fetch_page(row["url"])
            if body:
                # Attach the real text to the row it came from. The URL still
                # has to survive the same allowlist, so reading a page cannot
                # smuggle in a source the search never returned.
                row["snippet"] = body[:4000]
                break
    return await _ask_grounded(_DISCUSSION_SYSTEM, prompt, results)
