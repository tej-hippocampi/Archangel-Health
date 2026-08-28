"""Paid retrieval for the morning routine: Exa and Firecrawl.

``websearch.py`` had exactly one way to find anything: Anthropic's server-side
web-search tool, gated on ``ANTHROPIC_API_KEY``, returning ``[]`` in silence
without it. That is one vendor, one index, and one failure mode that looks
identical to a quiet day.

These providers are RETRIEVERS, not answerers. Each returns a list of results
that a real index actually holds:

    {"title", "url", "snippet", "published", "provider"}

That distinction is the point. When the model is handed retrieved results and
asked to select among them, the allowlist for the citation gate is a set WE
built from a search response, rather than a set parsed back out of the model's
own prose. The invariant ``websearch`` is arranged around ("a URL the model
wrote that the search never returned is dropped") gets strictly easier to
enforce, not harder, as providers are added.

Every function returns a list and never raises. A provider being down, keyless
or slow is a shorter morning, never a broken run.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("community.search_providers")

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

#: Order matters: the first provider that returns results is not preferred, but
#: results are merged in this order and deduped by URL, so an earlier provider
#: wins the metadata for a URL both found.
DEFAULT_PROVIDER_ORDER = ("exa", "firecrawl", "anthropic")


def provider_order() -> List[str]:
    """Which retrievers to use, in order.

    Defaults to Anthropic alone so an existing deployment behaves exactly as it
    did until someone opts in. Set ``COMMUNITY_SEARCH_PROVIDERS=exa,firecrawl,
    anthropic`` to turn the paid rungs on.
    """
    raw = (os.getenv("COMMUNITY_SEARCH_PROVIDERS") or "anthropic").strip()
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    return [n for n in names if n in DEFAULT_PROVIDER_ORDER] or ["anthropic"]


def exa_key() -> str:
    return (os.getenv("EXA_API_KEY") or "").strip()


def firecrawl_key() -> str:
    return (os.getenv("FIRECRAWL_API_KEY") or "").strip()


def available(name: str) -> bool:
    if name == "exa":
        return bool(exa_key())
    if name == "firecrawl":
        return bool(firecrawl_key())
    if name == "anthropic":
        return bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())
    return False


def _result(
    *, title: Any, url: Any, snippet: Any = "", published: Any = "", provider: str
) -> Optional[Dict[str, Any]]:
    """Normalize one provider row, or None if it is not usable.

    A result with no http(s) URL is not a result: it cannot be cited, opened or
    verified, and it is exactly what the citation gate exists to refuse.
    """
    u = str(url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return None
    t = str(title or "").strip()
    if not t:
        return None
    return {
        "title": t[:300],
        "url": u,
        "snippet": str(snippet or "").strip()[:1200],
        "published": str(published or "").strip()[:40],
        "provider": provider,
    }


async def search_exa(
    query: str, *, limit: int = 8, days: Optional[int] = None,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Exa neural search. Good at the things no RSS feed covers: society pages,
    university calendars, fellowship listings."""
    key = exa_key()
    if not key or not (query or "").strip():
        return []
    payload: Dict[str, Any] = {
        "query": query,
        "numResults": max(1, min(int(limit), 25)),
        "contents": {"text": {"maxCharacters": 1200}},
    }
    if category:
        payload["category"] = category
    if days:
        from datetime import datetime, timedelta, timezone

        start = (datetime.now(timezone.utc) - timedelta(days=int(days))).date().isoformat()
        payload["startPublishedDate"] = f"{start}T00:00:00.000Z"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": key, "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001 — a shorter morning, never a broken run
        log.warning("[search] exa query failed", exc_info=True)
        return []

    out: List[Dict[str, Any]] = []
    for row in (data or {}).get("results") or []:
        if not isinstance(row, dict):
            continue
        item = _result(
            title=row.get("title"),
            url=row.get("url"),
            snippet=row.get("text") or row.get("summary") or "",
            published=row.get("publishedDate") or "",
            provider="exa",
        )
        if item:
            out.append(item)
    return out


async def search_firecrawl(query: str, *, limit: int = 8) -> List[Dict[str, Any]]:
    """Firecrawl search. Used alongside Exa because the two indexes disagree,
    and the disagreement is where the events nobody aggregates turn up."""
    key = firecrawl_key()
    if not key or not (query or "").strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.firecrawl.dev/v1/search",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"query": query, "limit": max(1, min(int(limit), 20))},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001
        log.warning("[search] firecrawl query failed", exc_info=True)
        return []

    rows = (data or {}).get("data")
    if isinstance(rows, dict):
        rows = rows.get("web") or []
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = _result(
            title=row.get("title"),
            url=row.get("url"),
            snippet=row.get("description") or row.get("markdown") or "",
            published=row.get("publishedDate") or "",
            provider="firecrawl",
        )
        if item:
            out.append(item)
    return out


async def fetch_page(url: str, *, max_chars: int = 6000) -> str:
    """Read one page's text, for the agentic pass. "" on any failure.

    Only used where a snippet is genuinely not enough to say something true:
    the weekly discussion prompt is grounded in one source and claims to
    summarize it, so it should have read it.
    """
    key = firecrawl_key()
    if not key or not (url or "").strip():
        return ""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001
        log.warning("[search] firecrawl scrape failed", exc_info=True)
        return ""
    body = ((data or {}).get("data") or {}).get("markdown") or ""
    return str(body)[:max_chars]


def dedupe(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per URL, first provider wins.

    Normalized the same way ``websearch._normalize`` normalizes, so a URL that
    survives dedupe here is a URL the citation gate will recognize there. Two
    different normalizations would silently drop every result.
    """
    from community.websearch import _normalize  # noqa: PLC0415 — one definition

    seen = set()
    out: List[Dict[str, Any]] = []
    for row in results or []:
        key = _normalize(row.get("url") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
