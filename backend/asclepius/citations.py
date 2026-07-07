"""Citation library + auto-suggest retrieval (Seamless PRD WS3).

Grounded/cited records sell at a premium (value model ×1.3), but citing is
high-friction today so most records ship ungrounded. This module makes citing a
one-click, auto-suggested, *confirm* action: given the clinician's rationale (or
a reasoning step), it retrieves the 1–3 most relevant sources from a curated,
specialty-scoped library so the doctor can open the snippet and confirm with one
tap.

Design mirrors ``corpus.py``: the library is **data, not code** — a committed,
versioned ``citations/<specialty>.vN.json`` (bumping it is a reviewed PR).
Retrieval is a deterministic keyword/token overlap so it ALWAYS works offline; an
optional LLM rerank refines the ordering when a key is configured. Everything
degrades gracefully — no corpus for the specialty → ``skipped`` (the doctor just
types a citation), never an error.

The doctor MUST confirm a suggestion (``citation_confirmed=true``); nothing is
auto-attached. This is an accelerator for the clinician's judgment, not a
replacement for it (mission line).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.citations")

# Fields carried onto a suggestion + (on confirm) the evidence anchor.
_CITATION_KEYS = ("id", "title", "section", "source_type", "identifier", "url", "snippet")

# Very small English + clinical stopword set so overlap scoring keys on content
# words (drug names, electrolytes, guideline terms), not glue words.
_STOPWORDS = frozenset(
    """a an and or the of to in on for with without at by is are be was were this that these those
    patient patients pt yo year old man woman male female history presents present given start started
    starting due should would could may might can will do does how what when which who your you their
    it its as if then than into out over under per not no yes""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+\-]*")

_CACHE: Dict[str, Optional[List[Dict[str, Any]]]] = {}


def _library_path(specialty: str) -> str:
    safe = re.sub(r"[^a-z0-9_]", "", (specialty or "").strip().lower()) or "nephrology"
    return os.path.join(os.path.dirname(__file__), "citations", f"{safe}.v1.json")


def load_library(specialty: str = "nephrology") -> Optional[List[Dict[str, Any]]]:
    """Load + cache the citation library for a specialty. Returns the list of
    citation dicts, or ``None`` when no library file exists (→ degrade to skipped).
    Malformed JSON is treated as "no library" (logged), never raised."""
    key = (specialty or "nephrology").strip().lower()
    if key in _CACHE:
        return _CACHE[key]
    path = _library_path(key)
    lib: Optional[List[Dict[str, Any]]] = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            raw = data.get("citations") if isinstance(data, dict) else data
            if isinstance(raw, list):
                lib = [c for c in raw if isinstance(c, dict) and c.get("id") and c.get("title")]
        except (OSError, ValueError) as exc:
            log.warning("asclepius citations: could not load %s: %s", path, exc)
            lib = None
    _CACHE[key] = lib
    return lib


def clear_cache() -> None:
    _CACHE.clear()


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 1]


def _citation_terms(c: Dict[str, Any]) -> Dict[str, float]:
    """Weighted term set for a citation: keywords are the strongest signal, then
    title/section, then the snippet body."""
    terms: Dict[str, float] = {}
    for kw in c.get("keywords") or []:
        for t in _tokens(str(kw)):
            terms[t] = max(terms.get(t, 0.0), 3.0)
    for t in _tokens(str(c.get("title", "")) + " " + str(c.get("section", ""))):
        terms[t] = max(terms.get(t, 0.0), 1.5)
    for t in _tokens(str(c.get("snippet", ""))):
        terms[t] = max(terms.get(t, 0.0), 1.0)
    return terms


def _score(query_tokens: List[str], c: Dict[str, Any]) -> float:
    if not query_tokens:
        return 0.0
    terms = _citation_terms(c)
    qset = set(query_tokens)
    score = sum(w for t, w in terms.items() if t in qset)
    # Light normalization by query breadth so a longer rationale doesn't
    # mechanically outscore a short step for the same overlap.
    return score / (1.0 + 0.02 * len(qset))


def _public(c: Dict[str, Any]) -> Dict[str, Any]:
    return {k: c.get(k) for k in _CITATION_KEYS if c.get(k) is not None}


def suggest_citations(text: str, specialty: str = "nephrology", k: int = 3) -> List[Dict[str, Any]]:
    """Deterministic keyword/token retrieval — the always-available core. Returns
    up to ``k`` citation dicts ranked by relevance to ``text`` (only positive
    scores). Empty when the library exists but nothing matches; also empty when
    there is no library (callers distinguish via ``load_library``)."""
    lib = load_library(specialty)
    if not lib:
        return []
    qt = _tokens(text)
    scored = [(c, _score(qt, c)) for c in lib]
    scored = [(c, s) for c, s in scored if s > 0]
    scored.sort(key=lambda cs: cs[1], reverse=True)
    return [_public(c) for c, _s in scored[: max(1, k)]]


async def _rerank_llm(text: str, candidates: List[Dict[str, Any]], k: int) -> Optional[List[Dict[str, Any]]]:
    """Optional LLM rerank of the top retrieval candidates (best-effort). Returns
    a reordered subset, or ``None`` on any failure so the caller keeps the
    deterministic order. Never raises."""
    if not candidates:
        return None
    try:
        from ai.llm_client import call_llm, first_text
        from asclepius.prompts import ASCLEPIUS_CITE_RANK_SYSTEM
    except Exception:  # pragma: no cover - prompts/llm not available
        return None
    lines = [
        f"[{i}] {c.get('identifier') or c.get('title')} — {c.get('snippet', '')}"
        for i, c in enumerate(candidates)
    ]
    user = "CLINICAL TEXT:\n" + (text or "") + "\n\nCANDIDATE SOURCES:\n" + "\n".join(lines) + \
        f"\n\nReturn the indices of the {k} most relevant, best first, as a JSON list of integers."
    try:
        resp, _rec = await call_llm(
            role="asclepius_cite_rank",
            system=ASCLEPIUS_CITE_RANK_SYSTEM,
            messages=[{"role": "user", "content": user}],
            prompt_id="asclepius_cite_rank",
            purpose="asclepius_citation_rank",
        )
    except Exception as exc:
        log.info("asclepius citation rerank unavailable: %s", exc)
        return None
    m = re.search(r"\[[\s\d,]*\]", first_text(resp) or "")
    if not m:
        return None
    try:
        idxs = [int(i) for i in json.loads(m.group(0))]
    except (ValueError, TypeError):
        return None
    picked = [candidates[i] for i in idxs if 0 <= i < len(candidates)]
    return picked[:k] or None


async def suggest_citations_ranked(
    text: str, specialty: str = "nephrology", k: int = 3, *, use_llm: bool = True
) -> Dict[str, Any]:
    """Retrieval (always) + optional LLM rerank. Returns
    ``{suggestions, source, skipped}``. ``skipped=True`` only when there is no
    library for the specialty (degrade to manual citation) — an existing library
    with no match returns ``suggestions=[]`` and ``skipped=False``."""
    lib = load_library(specialty)
    if lib is None:
        return {"suggestions": [], "source": None, "skipped": True}
    # Retrieve a slightly wider candidate pool for the LLM to reorder.
    pool = suggest_citations(text, specialty, k=max(k, 5))
    if not pool:
        return {"suggestions": [], "source": "retrieval", "skipped": False}
    if use_llm and (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ASCLEPIUS_CITE_RANK", "")):
        reranked = await _rerank_llm(text, pool, k)
        if reranked:
            return {"suggestions": reranked, "source": "llm_rank", "skipped": False}
    return {"suggestions": pool[:k], "source": "retrieval", "skipped": False}
