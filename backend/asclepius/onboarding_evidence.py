"""Bounded guideline retrieval for synthetic onboarding case validation.

Only the specialty is sent to NCBI. No physician/CV or patient information is
used. URLs returned by models or publications are never fetched. The actual
retrieved abstracts and reusable Europe PMC excerpts, IDs and hashes accompany
the internal validation record. No reference-source URL supplied by a model runs.
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


def source_text(source: dict) -> str:
    """The exact retained evidence, including explicitly labelled OA excerpts."""
    if source.get("body_excerpts") and hashlib.sha256(source["body_excerpts"].encode()).hexdigest() != source.get("body_excerpts_sha256"):
        raise ValueError("Retained full-text evidence checksum mismatch")
    return source["abstract"] + ("\n" + source["body_excerpts"] if source.get("body_excerpts") else "")


def passages(sources: list[dict]) -> dict[str, dict]:
    """Give reviewers stable handles for literal text, avoiding retyped quotes.

    Handles do not establish support. Reviewers must still judge entailment and
    all clinical flags; the final report retains and revalidates the actual text.
    """
    result = {}
    for source in sources:
        remaining, index = " ".join(source_text(source).split()), 0
        while remaining:
            end = len(remaining)
            if end > 900:
                end = remaining.rfind(". ", 200, 900)
                end = end + 1 if end >= 200 else remaining.rfind(" ", 0, 900)
                if end <= 0:
                    end = 900
            text, remaining = remaining[:end], remaining[end:].lstrip()
            if len(text) >= 16:
                result[f"{source['id']}:p{index}"] = {"source_id": source["id"], "quote": text}
            index += 1
    return result


def parse_open_text(raw: bytes, source: dict, topic: str) -> dict | None:
    """Select intact passages from licensed primary text, never model URLs.

    Europe PMC's fullTextXML endpoint serves its Open Access subset. We further
    restrict reusable passages to CC BY / CC0, and require matching article IDs.
    https://europepmc.org/RestfulWebService
    """
    if b"<!ENTITY" in raw.upper():
        raise ValueError("Unexpected XML entity")
    root = ElementTree.fromstring(raw)
    meta = root.find("./front/article-meta")
    if meta is None or meta.findtext("./article-id[@pub-id-type='pmid']") != source["id"]:
        return None
    pmcids = [n.text or "" for n in meta.findall('./article-id') if n.get('pub-id-type') in {'pmc', 'pmcid'}]
    if not pmcids or any(pmcid.removeprefix("PMC") != source["pmcid"].removeprefix("PMC") for pmcid in pmcids):
        return None
    license_nodes = [node for license_node in meta.findall("./permissions/license")
                     for node in license_node.iter()]
    license_url = next((node.get("{http://www.w3.org/1999/xlink}href", "")
        for node in license_nodes
        if re.fullmatch(r"https?://creativecommons\.org/(licenses/by|publicdomain/zero)/[1-4]\.0/?",
                        node.get("{http://www.w3.org/1999/xlink}href", ""))), None)
    if not license_url:
        return None
    body = root.find("./body")
    if body is None:
        return None
    terms = set(re.findall(r"[a-z]{4,}", topic.lower())) - {"with", "using", "assessment", "initial", "management"}
    blocks = []

    def walk(node, headings=()):
        if node.tag == "sec":
            title = node.find("./title")
            headings += (" ".join(title.itertext()) if title is not None else "",)
        if node.tag in {"p", "table-wrap"}:
            text = " ".join(" ".join(node.itertext()).split())
            if 40 <= len(text) <= 12000:
                heading = " > ".join(h for h in headings if h)
                text = (heading + "\n" if heading else "") + text
                words = set(re.findall(r"[a-z]{4,}", text.lower()))
                overlap = len(words & terms)
                score = overlap * 3 + (2 if re.search(r"recommend|should|must", text, re.I) else 0)
                blocks.append((score, len(blocks), text))
            return
        for child in node:
            if child.tag in {"sec", "p", "table-wrap", "boxed-text", "list", "list-item"}:
                walk(child, headings)

    walk(body)
    chosen, size = [], 0
    for score, index, text in sorted(blocks, key=lambda b: (-b[0], b[1])):
        if score <= 0 or size + len(text) > 32000:
            continue
        chosen.append((index, text))
        size += len(text)
    if not chosen:
        return None
    text = "\n\n".join(text for _, text in sorted(chosen))
    return {"body_excerpts": text, "body_excerpts_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "body_excerpts_url": f"https://www.ebi.ac.uk/europepmc/webservices/rest/{source['pmcid']}/fullTextXML",
            "body_license_url": license_url}


async def _open_text(client: httpx.AsyncClient, source: dict, topic: str) -> dict | None:
    pmcid = source.get("pmcid", "")
    if not re.fullmatch(r"PMC[0-9]+", pmcid):
        return None
    # Fixed host/path; publication-provided links and redirect targets never run.
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    try:
        async with client.stream("GET", url) as response:
            if response.status_code in {403, 404}:
                return None
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 2_000_000:
                    return None
                chunks.append(chunk)
        return parse_open_text(b"".join(chunks), source, topic)
    except (httpx.HTTPError, ElementTree.ParseError):
        # An unavailable full text leaves the original abstract intact; clinical
        # review still rejects any recommendation it cannot substantiate.
        return None


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
        row = {"id": pmid, "title": title, "year": year,
                     "url": "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/",
                     "abstract": abstract,
                     "sha256": hashlib.sha256(abstract.encode()).hexdigest(),
                     "retrieved_at": today.isoformat()}
        pmcid = article.findtext("./PubmedData/ArticleIdList/ArticleId[@IdType='pmc']", "")
        if re.fullmatch(r"PMC[0-9]+", pmcid):
            row["pmcid"] = pmcid
        rows.append(row)
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
    # Untagged PubMed terms also match society/author affiliations. Searching
    # "asthma" previously returned urticaria guidelines merely authored by an
    # asthma society. Bind every disease term to the actual title/abstract.
    filters = ('(guideline[Publication Type] OR practice guideline[Publication Type] '
             'OR systematic review[Publication Type] OR consensus development conference[Publication Type]) '
             f'AND ("{year - 7}"[Date - Publication] : "{year}"[Date - Publication]) '
             'NOT (retracted publication[Publication Type] OR retraction of publication[Publication Type])')
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        rows = []
        seen = set()
        # Start with articles about the disease itself. Title/Abstract is a
        # fallback for narrower topics that have too few title matches. Even
        # abstracts sometimes name an asthma society in unrelated guidelines.
        for field in ('Title', 'Title/Abstract'):
            disease_query = ' AND '.join(f'{word}[{field}]' for word in clinical_term.split())
            search = json.loads(await _request(client, "esearch.fcgi",
                {"term": f'({disease_query}) AND {filters}', "retmode": "json", "retmax": 12, "sort": "relevance"}))
            ids = [str(i) for i in search.get("esearchresult", {}).get("idlist", []) if str(i).isdigit() and str(i) not in seen][:12]
            seen.update(ids)
            # Guidelines can carry huge reference lists; keep response pages small.
            for start in range(0, len(ids), 3):
                rows.extend(parse_articles(await _request(client, "efetch.fcgi",
                    {"id": ",".join(ids[start:start + 3]), "retmode": "xml"})))
                if len(rows) >= 8:
                    break
            if len(rows) >= 8:
                break
        # Supply the actual recommendations where reusable full text exists,
        # rather than expecting a scope-only abstract to establish an algorithm.
        expanded = 0
        for source in rows[:8]:
            if expanded >= 3:
                break
            extra = await _open_text(client, source, topic or clinical_term)
            if extra:
                source.update(extra)
                expanded += 1
    if len(rows) < 2:
        raise ValueError("Insufficient clinical reference material")
    return rows[:8]
