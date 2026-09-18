"""Bounded PubMed guideline retrieval for synthetic onboarding case validation.

Only the specialty is sent to NCBI. No physician/CV or patient information is
used. URLs returned by models or publications are never fetched. The actual
retrieved abstracts, IDs and hashes accompany the internal validation record.
See https://www.ncbi.nlm.nih.gov/books/NBK25497/ for E-utilities policies and
https://www.ncbi.nlm.nih.gov/About/disclaimer.html for source terms.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import re
import weakref
from xml.etree import ElementTree

import httpx

_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def _request(client: httpx.AsyncClient, endpoint: str, params: dict) -> bytes:
    # Serialize and space calls in this event loop, below NCBI's unkeyed limit.
    loop = asyncio.get_running_loop()
    lock = _LOCKS.setdefault(loop, asyncio.Lock())
    async with lock:
        await asyncio.sleep(0.4)
        async with client.stream("GET", _BASE + endpoint,
                                 params={"db": "pubmed", "tool": "archangel_onboarding", **params}) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError("Clinical reference response too large")
                chunks.append(chunk)
            return b"".join(chunks)


def parse_articles(raw: bytes, *, now: datetime | None = None) -> list[dict]:
    if b"<!ENTITY" in raw.upper():
        raise ValueError("Unexpected XML entity")
    root = ElementTree.fromstring(raw)
    today = now or datetime.now(timezone.utc)
    rows = []
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext("./MedlineCitation/PMID", "")
        title = "".join(article.find(".//ArticleTitle").itertext()) if article.find(".//ArticleTitle") is not None else ""
        abstract = "\n".join("".join(p.itertext()) for p in article.findall(".//Abstract/AbstractText"))
        types = {p.text or "" for p in article.findall(".//PublicationType")}
        relations = {p.get("RefType") for p in article.findall(".//CommentsCorrections")}
        pubdate = article.find(".//JournalIssue/PubDate")
        date_text = " ".join(pubdate.itertext()) if pubdate is not None else ""
        year_match = re.search(r"\b(19|20)\d{2}\b", date_text)
        year = int(year_match.group()) if year_match else 0
        if (not pmid.isdigit() or len(abstract) < 300 or len(abstract) > 16000
                or not today.year - 7 <= year <= today.year
                or types & {"Retracted Publication", "Retraction of Publication"}
                or relations & {"RetractionIn", "ExpressionOfConcernIn"}
                or not types & {"Guideline", "Practice Guideline", "Systematic Review", "Consensus Development Conference"}):
            continue
        rows.append({"id": pmid, "title": title, "year": year,
                     "url": "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/",
                     "abstract": abstract,
                     "sha256": hashlib.sha256(abstract.encode()).hexdigest(),
                     "retrieved_at": today.isoformat()})
    return rows


async def retrieve(specialty: str, *, topic: str | None = None) -> list[dict]:
    import json

    # Quote a cleaned clinical term. It cannot introduce an Entrez operator or
    # control a URL. A free-text specialty remains untrusted input to the LLM.
    term = re.sub(r"[^\w\s]", " ", specialty)[:100]
    year = datetime.now(timezone.utc).year
    # Curriculum topics are maintained in code; never supplied by a physician.
    from asclepius.onboarding_catalog import SEARCH_TERMS
    clinical_term = re.sub(r"[^\w\s]", " ", SEARCH_TERMS.get(topic, term))[:240]
    query = (f'({clinical_term}) AND '
             '(guideline[Publication Type] OR practice guideline[Publication Type] '
             'OR systematic review[Publication Type] OR consensus development conference[Publication Type]) '
             f'AND ("{year - 7}"[Date - Publication] : "{year}"[Date - Publication]) '
             'NOT (retracted publication[Publication Type] OR retraction of publication[Publication Type])')
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        search = json.loads(await _request(client, "esearch.fcgi",
            {"term": query, "retmode": "json", "retmax": 12, "sort": "relevance"}))
        ids = search.get("esearchresult", {}).get("idlist", [])
        ids = [str(i) for i in ids if str(i).isdigit()][:12]
        if not ids:
            raise ValueError("No clinical references available")
        rows = []
        # Guidelines can carry very large reference lists. Fetch small bounded
        # pages, retaining only vetted abstracts rather than raising the limit
        # for a single unbounded response containing the whole search result.
        for start in range(0, len(ids), 3):
            rows.extend(parse_articles(await _request(client, "efetch.fcgi",
                {"id": ",".join(ids[start:start + 3]), "retmode": "xml"})))
            if len(rows) >= 8:
                break
    if len(rows) < 2:
        raise ValueError("Insufficient clinical reference material")
    return rows[:8]
