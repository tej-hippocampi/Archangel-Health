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


def enabled() -> bool:
    """Web search needs a key like everything else the model does."""
    return bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())


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


async def _ask(system: str, prompt: str) -> List[Dict[str, Any]]:
    """One search-backed call, with every uncited URL dropped."""
    if not enabled():
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

    cited = _cited_urls(response)
    items = _parse_items(_text_of(response))
    kept: List[Dict[str, Any]] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if _normalize(url) not in cited:
            # The model wrote a URL the search never returned. That is the
            # failure mode this whole module is arranged around.
            log.info("[websearch] dropped an uncited url: %s", url[:120])
            continue
        kept.append(item)
    return kept


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
    return await _ask(_OPPORTUNITY_SYSTEM, prompt)


_DISCUSSION_SYSTEM = (
    "You propose one discussion topic for a community of practising physicians "
    "who evaluate medical AI. Use web search to ground it in something real "
    "and recent. Answer with a JSON array containing exactly one item: "
    '{"title","url","summary","prompt"} where "title" is the topic, "url" is '
    'the article or paper it is grounded in, "summary" is two sentences of '
    'context, and "prompt" is the question posed to the room -- open, '
    "specific, and answerable from clinical experience rather than opinion."
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
    return await _ask(_DISCUSSION_SYSTEM, prompt)
