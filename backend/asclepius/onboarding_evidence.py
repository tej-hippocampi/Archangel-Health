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
import json
from pathlib import Path
import re
import weakref
from xml.etree import ElementTree

import httpx

_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# A peer-reviewed, CC BY pathology teaching article describes both slide
# differentials. It is educational morphology evidence, not a treatment guideline.
# Explicit identity pinning avoids broadening the guideline search to case reports.
_PATHOLOGY_TEACHING = {"40687210": ("PMC12271062", "Educational Case: Squamous cell carcinoma.")}
_ILLUSTRATION_TAGS = {"table-wrap", "table-wrap-group", "table", "fig", "fig-group", "graphic", "media"}


def _restrictive_license(node: ElementTree.Element) -> bool:
    # Check both the human-readable license and its links/attributes. A CC BY
    # link cannot override a contradictory NonCommercial/NoDerivatives label.
    text = " ".join(node.itertext()) + " " + " ".join(
        value for child in node.iter() for value in child.attrib.values())
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return bool(re.search(
        r"\b(?:non[\s-]*commercial|no[\s-]*deriv(?:ative)?s?|share[\s-]*alike)\b"
        r"|\b(?:by|attribution)[\s-]+(?:nc|nd|sa)\b", text, re.I))


def _restricted_illustration(node: ElementTree.Element) -> bool:
    # An article's license does not grant reuse of separately credited material.
    # Be conservative when a table/figure carries its own copyright or permission
    # notice; do not remove the credit and retain its underlying clinical text.
    if _restrictive_license(node) or any(
        child.tag in {"copyright-statement", "copyright-holder"}
        or (child.tag == "attrib" and " ".join(child.itertext()).strip())
        for child in node.iter()
    ):
        return True
    text = " ".join(" ".join(node.itertext()).split())
    return bool(re.search(
        r"\bcopyright\b|©|\ball rights reserved\b|\bcourtesy of\b"
        r"|\bwith\s+(?:kind\s+)?permission\b"
        r"|\b(?:adapted|reproduced|reprinted|republished|modified|used)\b.{0,100}\bpermission\b"
        r"|\bpermission\s+(?:of|from|granted)\b"
        r"|\b(?:adapted|reproduced|reprinted|republished|modified)\s+from\b"
        r"|\bsource\s*:", text, re.I))


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
    licenses = meta.findall("./permissions/license")
    if any(_restrictive_license(license_node) for license_node in licenses):
        return None
    license_nodes = [node for license_node in licenses
                     for node in license_node.iter()]
    # Some publishers put the URL in plain license-p text, not an ext-link.
    # Read only the permissions/license node; a URL in the article body is not
    # a reuse license. Exact matching still excludes NC/ND/SA and lookalike hosts.
    license_candidates = [node.get("{http://www.w3.org/1999/xlink}href", "") for node in license_nodes]
    license_candidates += [url.rstrip(".,;)") for node in meta.findall("./permissions/license")
                           for url in re.findall(r"https?://[^\s<>]+", " ".join(node.itertext()))]
    license_url = next((url for url in license_candidates if re.fullmatch(
        r"https?://creativecommons\.org/(licenses/by|publicdomain/zero)/[1-4]\.0/?", url)), None)
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
            # JATS permits tables/figures inside paragraphs. itertext() would
            # otherwise copy a restricted nested object with its surrounding
            # prose. Skip the entire block rather than splice its text.
            if any(child.tag in _ILLUSTRATION_TAGS and _restricted_illustration(child)
                   for child in node.iter()):
                return
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
        for attempt in range(3):
            await asyncio.sleep(0.4)
            async with client.stream("GET", _BASE + endpoint,
                                     params={"db": "pubmed", "tool": "archangel_onboarding", **params}) as response:
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    # Brief public-service throttles are not a clinical rejection.
                    # Respect numeric Retry-After with a bounded wait; never
                    # discard source/clinical checks or continue with empty text.
                    delay = response.headers.get("Retry-After", "")
                    await asyncio.sleep(min(30, max(2, int(delay))) if delay.isdigit() else 2 ** (attempt + 1))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ValueError("Clinical reference response too large")
                    chunks.append(chunk)
                return b"".join(chunks)


def population_matches(title: str, mesh_terms: set[str], age_scope: str | None) -> bool:
    """Exclude explicit population mismatches without discarding unindexed papers.

    This is a retrieval filter, not a substitute for reviewer population checks.
    Mixed human/animal and adult/pediatric material still needs clinical review.
    """
    if 'Animals' in mesh_terms and 'Humans' not in mesh_terms:
        return False
    if 'Humans' not in mesh_terms and re.search(r'\b(veterinary|dogs|cats|canine|feline|equine|murine|mice|rats)\b', title, re.I):
        return False
    child_mesh = bool(mesh_terms & {'Child', 'Child, Preschool', 'Adolescent', 'Infant', 'Infant, Newborn'})
    adult_mesh = bool(mesh_terms & {'Adult', 'Young Adult', 'Middle Aged', 'Aged', 'Aged, 80 and over'})
    # Society names and childhood-onset diseases do not identify the study
    # population. Prefer age indexing, preserve explicitly mixed/maternal work,
    # and use only clear population wording when age indexing is unavailable.
    pediatric = child_mesh or bool(re.search(
        r'\b(children|infants|neonates|adolescents|(?:pediatric|paediatric) (?:patients|population|patients? with))\b'
        r'|^\W*(?:pediatric|paediatric|neonatal|adolescent)\b', title, re.I))
    adult = adult_mesh or bool(re.search(r'\b(adults?|elderly|maternal|pregnant women|older (?:adults|people|persons))\b', title, re.I))
    if age_scope in {'adult', 'older_adult'} and pediatric and not adult:
        return False
    if age_scope == 'pediatric' and adult and not pediatric:
        return False
    return True


def parse_articles(raw: bytes, *, now: datetime | None = None, pathology_teaching: bool = False,
                   age_scope: str | None = None, pinned: dict | None = None) -> list[dict]:
    if b"<!ENTITY" in raw.upper():
        raise ValueError("Unexpected XML entity")
    root = ElementTree.fromstring(raw)
    today = now or datetime.now(timezone.utc)
    rows = []
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext("./MedlineCitation/PMID", "")
        title = "".join(article.find(".//ArticleTitle").itertext()) if article.find(".//ArticleTitle") is not None else ""
        mesh = {node.text or '' for node in article.findall('.//MeshHeading/DescriptorName')}
        if not population_matches(title, mesh, age_scope):
            continue
        abstract = "\n".join("".join(p.itertext()) for p in article.findall(".//Abstract/AbstractText"))
        types = {p.text or "" for p in article.findall(".//PublicationType")}
        relations = {p.get("RefType") for p in article.findall(".//CommentsCorrections")}
        pubdate = article.find(".//JournalIssue/PubDate")
        date_text = " ".join(pubdate.itertext()) if pubdate is not None else ""
        year_match = re.search(r"\b(19|20)\d{2}\b", date_text)
        year = int(year_match.group()) if year_match else 0
        pmcid = article.findtext("./PubmedData/ArticleIdList/ArticleId[@IdType='pmc']", "")
        teaching = pathology_teaching and _PATHOLOGY_TEACHING.get(pmid) == (pmcid, title)
        pin = (pinned or {}).get(pmid)
        exact_pin = bool(pin and (pin["pmcid"], pin["title"]) == (pmcid, title))
        if pinned is not None and not exact_pin:
            continue
        if pathology_teaching and not teaching:
            # The pinned fetch must not be satisfied by an unrelated article,
            # even if that article would qualify for the ordinary guideline path.
            continue
        if (not pmid.isdigit() or (len(abstract) < 300 and not teaching) or len(abstract) > 16000
                or not today.year - 7 <= year <= today.year
                or types & {"Retracted Publication", "Retraction of Publication"}
                or relations & {"RetractionIn", "ExpressionOfConcernIn"}
                or (not teaching and not exact_pin and not types & {"Guideline", "Practice Guideline", "Systematic Review", "Consensus Development Conference"})):
            continue
        row = {"id": pmid, "title": title, "year": year,
                     "url": "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/",
                     "abstract": abstract,
                     "sha256": hashlib.sha256(abstract.encode()).hexdigest(),
                     "retrieved_at": today.isoformat()}
        if exact_pin:
            row["source_type"] = pin["source_type"]
        if teaching:
            row["source_type"] = "peer_reviewed_pathology_teaching"
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
    from asclepius.onboarding_catalog import SEARCH_TERMS, age_scope_for
    age_scope = age_scope_for(specialty)
    clinical_term = re.sub(r"[^\w\s]", " ", SEARCH_TERMS.get(topic, term))[:240]
    # Untagged PubMed terms also match society/author affiliations. Searching
    # "asthma" previously returned urticaria guidelines merely authored by an
    # asthma society. Bind every disease term to the actual title/abstract.
    filters = (f'("{year - 7}"[Date - Publication] : "{year}"[Date - Publication]) '
             'NOT (retracted publication[Publication Type] OR retraction of publication[Publication Type])')
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        rows = []
        seen = set()
        if specialty == "pathology":
            teaching = parse_articles(await _request(client, "efetch.fcgi",
                {"id": ",".join(_PATHOLOGY_TEACHING), "retmode": "xml"}), pathology_teaching=True)
            if len(teaching) != len(_PATHOLOGY_TEACHING) or {s['id'] for s in teaching} != set(_PATHOLOGY_TEACHING):
                raise ValueError("Pinned pathology teaching reference unavailable or retracted")
            for source in teaching:
                extra = await _open_text(client, source, "basal squamous carcinoma histology morphology keratin palisading")
                if not extra:
                    raise ValueError("Licensed pathology morphology evidence unavailable")
                source.update(extra)
                rows.append(source)
                seen.add(source["id"])
        # Narrow exceptions for independently verified guidance/systematic
        # reviews whose PubMed publication-type indexing omits that class.
        # Identity, recency, retraction, population and CC BY/CC0 checks remain.
        configured = json.loads((Path(__file__).with_name("onboarding_material") / "evidence_pins.json").read_text()).get(topic, {})
        pins = {s["id"]: s for s in configured.get("sources", [])}
        excerpt_topic = configured.get("excerpt_terms") or topic or clinical_term
        if pins:
            pinned_rows = parse_articles(await _request(client, "efetch.fcgi",
                {"id": ",".join(pins), "retmode": "xml"}), age_scope=age_scope, pinned=pins)
            if {s["id"] for s in pinned_rows} != set(pins):
                raise ValueError("Pinned clinical reference unavailable, changed or retracted")
            for source in pinned_rows:
                extra = await _open_text(client, source, excerpt_topic)
                if pins[source["id"]]["require_licensed_fulltext"] and not extra:
                    raise ValueError("Licensed pinned clinical evidence unavailable")
                if extra:
                    source.update(extra)
                rows.append(source)
                seen.add(source["id"])
        # First obtain actual practice guidance. A combined query can fill all
        # eight slots with narrow systematic reviews and exclude the guideline
        # containing the clinical algorithm. Within each publication category,
        # start with disease-title matches and then use title/abstract fallback.
        guidance = '(guideline[Publication Type] OR practice guideline[Publication Type] OR consensus development conference[Publication Type])'
        searches = [(category, field) for category in (guidance, 'systematic review[Publication Type]')
                    for field in ('Title', 'Title/Abstract')]
        for category, field in searches:
            # PubMed does not index standalone stopwords (e.g. the "A" in
            # type A dissection). The curriculum still enforces that subtype.
            words = [word for word in clinical_term.split() if len(word) > 1 and word.lower() not in {'and', 'of', 'the'}]
            # Explicit field tags disable PubMed's automatic singular/plural
            # expansion. Without a suffix, "infant" misses the AAP guideline
            # titled "Febrile Infants". NCBI permits truncation from four chars.
            disease_query = ' AND '.join(
                f'(mass[{field}] OR masses[{field}])' if word.lower() == 'mass'
                else f'{word}{"*" if len(word) >= 4 else ""}[{field}]' for word in words)
            search = json.loads(await _request(client, "esearch.fcgi",
                {"term": f'({disease_query}) AND ({category}) AND {filters}', "retmode": "json", "retmax": 12, "sort": "relevance"}))
            ids = [str(i) for i in search.get("esearchresult", {}).get("idlist", []) if str(i).isdigit() and str(i) not in seen][:12]
            seen.update(ids)
            # Guidelines can carry huge reference lists; keep response pages small.
            for start in range(0, len(ids), 3):
                rows.extend(parse_articles(await _request(client, "efetch.fcgi",
                    {"id": ",".join(ids[start:start + 3]), "retmode": "xml"}), age_scope=age_scope))
                if len(rows) >= 8:
                    break
            if len(rows) >= 8:
                break
        # Supply the actual recommendations where reusable full text exists,
        # rather than expecting a scope-only abstract to establish an algorithm.
        expanded = sum(bool(s.get("body_excerpts")) for s in rows[:8])
        for source in rows[:8]:
            if expanded >= 3:
                break
            if source.get("body_excerpts"):
                continue
            extra = await _open_text(client, source, excerpt_topic)
            if extra:
                source.update(extra)
                expanded += 1
    if len(rows) < 2:
        raise ValueError("Insufficient clinical reference material")
    return rows[:8]
