"""Content-source fetchers for the #medical-ai-news digest (Community v2).

Free stack only — no paid news API:
  * PubMed E-utilities  (medical-AI query, last-N-days; free key optional)
  * arXiv API           (cs.AI / cs.LG + medical terms, one request per run)
  * medRxiv API         (last-N-days clinical preprints, keyword-filtered later)
  * Reporter RSS        (STAT, Fierce Healthcare, Healthcare IT News by default)

Every fetcher is async (httpx), returns a list of NORMALIZED items —
``{source, external_id, url, title, published_at, abstract}`` — and returns
``[]`` on ANY failure (skip-and-log): one dead source never kills a digest
run. Network timeouts are ours (httpx), not the parser's.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List
from xml.etree import ElementTree

import httpx

log = logging.getLogger("community.feeds")

_TIMEOUT = httpx.Timeout(20.0)
_UA = {"User-Agent": "ArchangelHealth-CommunityDigest/1.0 (contact: support@archangelhealth.ai)"}

# One query string shared by the paper sources: AI x medicine.
_PUBMED_TERM = (
    '("artificial intelligence"[Title/Abstract] OR "machine learning"[Title/Abstract] '
    'OR "large language model"[Title/Abstract] OR "deep learning"[Title/Abstract] '
    'OR "foundation model"[Title/Abstract]) AND (clinical[Title/Abstract] '
    'OR medicine[Title/Abstract] OR medical[Title/Abstract] OR diagnosis[Title/Abstract] '
    'OR "health care"[Title/Abstract] OR healthcare[Title/Abstract])'
)
_ARXIV_QUERY = (
    "(cat:cs.AI OR cat:cs.LG OR cat:cs.CL) AND "
    '(abs:"medical" OR abs:"clinical" OR abs:"healthcare" OR abs:"physician" '
    'OR abs:"diagnosis" OR abs:"radiology" OR abs:"EHR")'
)

_DEFAULT_RSS_FEEDS = {
    "stat-ai": "https://www.statnews.com/topic/artificial-intelligence/feed/",
    "fierce-healthcare": "https://www.fiercehealthcare.com/rss/xml",
    # Healthcare IT News / MobiHealthNews 403 non-browser clients — these two
    # accept plain fetches (verified live 2026-08).
    "medcitynews": "https://medcitynews.com/feed/",
    "healthcaredive": "https://www.healthcaredive.com/feeds/news/",
}


def normalize_url(url: str) -> str:
    """Canonical dedup key: lowercase host, no scheme/www, tracking params
    stripped, no trailing slash."""
    u = (url or "").strip()
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I)
    if "?" in u:
        path, _, query = u.partition("?")
        kept = [
            p for p in query.split("&")
            if p and not re.match(r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref=|source=)", p, flags=re.I)
        ]
        u = path + (("?" + "&".join(kept)) if kept else "")
    return u.rstrip("/").lower()


def normalize_title(title: str) -> str:
    """Cross-outlet dedup key: lowercased, punctuation collapsed."""
    t = re.sub(r"[^\w\s]", " ", (title or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _item(source: str, *, url: str, title: str, external_id: str = None,
          published_at: str = None, abstract: str = None) -> Dict[str, Any]:
    return {
        "source": source,
        "external_id": external_id,
        "url": url,
        "url_norm": normalize_url(url),
        "title": " ".join((title or "").split())[:300],
        "title_norm": normalize_title(title),
        "published_at": published_at,
        "abstract": " ".join((abstract or "").split())[:1500] or None,
    }


async def fetch_pubmed(days: int = 7) -> List[Dict[str, Any]]:
    """E-utilities: esearch (reldate window) then efetch abstracts. The free
    ``PUBMED_API_KEY`` raises the rate cap 3->10 req/s — we make 2 requests."""
    try:
        params = {
            "db": "pubmed", "term": _PUBMED_TERM, "retmode": "json",
            "retmax": "40", "sort": "date", "datetype": "pdat",
            "reldate": str(max(1, int(days))),
        }
        key = (os.getenv("PUBMED_API_KEY") or "").strip()
        if key:
            params["api_key"] = key
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_UA) as client:
            sr = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params=params
            )
            sr.raise_for_status()
            pmids = (sr.json().get("esearchresult") or {}).get("idlist") or []
            if not pmids:
                return []
            fparams = {"db": "pubmed", "id": ",".join(pmids[:40]), "retmode": "xml"}
            if key:
                fparams["api_key"] = key
            fr = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params=fparams
            )
            fr.raise_for_status()
        root = ElementTree.fromstring(fr.text)
        out: List[Dict[str, Any]] = []
        for art in root.iter("PubmedArticle"):
            pmid = (art.findtext(".//PMID") or "").strip()
            title_el = art.find(".//ArticleTitle")
            title = "".join(title_el.itertext()) if title_el is not None else ""
            abstract = " ".join(
                "".join(ab.itertext()) for ab in art.findall(".//Abstract/AbstractText")
            )
            if not pmid or not title:
                continue
            out.append(_item(
                "pubmed",
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                title=title, external_id=pmid, abstract=abstract,
            ))
        return out
    except Exception:
        log.warning("[feeds] pubmed fetch failed: skipped", exc_info=True)
        return []


async def fetch_arxiv(days: int = 7) -> List[Dict[str, Any]]:
    """One Atom query, newest first (trivially inside arXiv's 1-req/3s rule)."""
    try:
        params = {
            "search_query": _ARXIV_QUERY, "start": "0", "max_results": "40",
            "sortBy": "submittedDate", "sortOrder": "descending",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_UA) as client:
            r = await client.get("https://export.arxiv.org/api/query", params=params)
            r.raise_for_status()
        import feedparser  # noqa: PLC0415 — parser only, bytes fetched above
        parsed = feedparser.parse(r.content)
        cutoff = datetime.utcnow() - timedelta(days=max(1, int(days)))
        out: List[Dict[str, Any]] = []
        for e in parsed.entries:
            pub = None
            if getattr(e, "published_parsed", None):
                pub = datetime(*e.published_parsed[:6])
            if pub and pub < cutoff:
                continue
            out.append(_item(
                "arxiv",
                url=getattr(e, "link", "") or "",
                title=getattr(e, "title", "") or "",
                external_id=(getattr(e, "id", "") or "").rsplit("/", 1)[-1] or None,
                published_at=pub.isoformat() + "Z" if pub else None,
                abstract=getattr(e, "summary", "") or "",
            ))
        return [i for i in out if i["url"] and i["title"]]
    except Exception:
        log.warning("[feeds] arxiv fetch failed: skipped", exc_info=True)
        return []


async def fetch_medrxiv(days: int = 7) -> List[Dict[str, Any]]:
    """medRxiv details endpoint over a date range (first page — 100 newest is
    plenty; the keyword filter downstream does the narrowing)."""
    try:
        end = datetime.utcnow().date()
        start = end - timedelta(days=max(1, int(days)))
        url = f"https://api.medrxiv.org/details/medrxiv/{start}/{end}/0/json"
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_UA) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        out: List[Dict[str, Any]] = []
        for c in data.get("collection") or []:
            doi = (c.get("doi") or "").strip()
            title = c.get("title") or ""
            if not doi or not title:
                continue
            out.append(_item(
                "medrxiv",
                url=f"https://www.medrxiv.org/content/{doi}",
                title=title, external_id=doi,
                published_at=c.get("date"), abstract=c.get("abstract") or "",
            ))
        return out
    except Exception:
        log.warning("[feeds] medrxiv fetch failed: skipped", exc_info=True)
        return []


def rss_feed_urls() -> Dict[str, str]:
    """``COMMUNITY_NEWS_FEEDS`` = comma-separated ``key=url`` pairs; falls back
    to the built-in reporter set (sources with actual reporters, not press-release
    aggregators)."""
    raw = (os.getenv("COMMUNITY_NEWS_FEEDS") or "").strip()
    if not raw:
        return dict(_DEFAULT_RSS_FEEDS)
    out: Dict[str, str] = {}
    for pair in raw.split(","):
        key, _, url = pair.strip().partition("=")
        if key and url.startswith("http"):
            out[key.strip()] = url.strip()
    return out or dict(_DEFAULT_RSS_FEEDS)


async def fetch_rss() -> List[Dict[str, Any]]:
    """All configured reporter feeds; each feed independently skip-and-log."""
    import feedparser  # noqa: PLC0415

    out: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers=_UA, follow_redirects=True
    ) as client:
        for key, url in rss_feed_urls().items():
            try:
                r = await client.get(url)
                r.raise_for_status()
                parsed = feedparser.parse(r.content)
                for e in parsed.entries[:30]:
                    link = getattr(e, "link", "") or ""
                    title = getattr(e, "title", "") or ""
                    if not link or not title:
                        continue
                    pub = None
                    if getattr(e, "published_parsed", None):
                        pub = datetime(*e.published_parsed[:6]).isoformat() + "Z"
                    out.append(_item(
                        f"rss:{key}", url=link, title=title,
                        published_at=pub,
                        abstract=getattr(e, "summary", "") or "",
                    ))
            except Exception:
                log.warning("[feeds] rss feed %r failed: skipped", key, exc_info=True)
    return out
