"""Seed-corpus loader (PRD §5).

Loads + caches the committed, versioned seed corpus JSON for a specialty (read-
only at runtime), validates every item against the §5.2 schema on load, and
exposes few-shot sampling + dedupe helpers used by the generation engine and the
``GET /generation/seed-corpus`` admin endpoint.

The corpus is *data, not code* — bumping it (v2, …) is a reviewed PR. Whatever
version loads is stamped onto every generated task's provenance. The committed
artifact is the human-ratification target; an unratified corpus loads fine but
reports ``ratified=false`` so the UI/datasheet can flag it.
"""

from __future__ import annotations

import json
import os
import random
from typing import Any, Dict, List, Optional

from asclepius.constants import DIFFICULTIES
from asclepius.specialties import get_specialty_config

# Concept-source categories allowed on a seed item's ``reference_type`` (PRD §5.2).
REFERENCE_TYPES = ("guideline", "review", "primary_literature", "expert_consensus")

# Required per-item keys (PRD §5.2).
_REQUIRED_ITEM_KEYS = (
    "seed_id",
    "specialty",
    "topic",
    "subtopic",
    "difficulty",
    "prompt",
    "ai_failure_mode",
    "why_high_value",
    "reference_basis",
    "reference_type",
    "capture_reasoning_recommended",
    "tags",
)


class CorpusError(ValueError):
    """Raised when a seed corpus is missing, malformed, or schema-invalid."""


_CACHE: Dict[str, Dict[str, Any]] = {}


def _corpus_path(specialty: str) -> str:
    cfg = get_specialty_config(specialty)
    return os.path.join(os.path.dirname(__file__), cfg.seed_corpus)


def validate_item(item: Dict[str, Any], *, bucket_ids: Optional[List[str]] = None) -> List[str]:
    """Return a list of schema errors for one seed item (empty == valid)."""
    errors: List[str] = []
    if not isinstance(item, dict):
        return ["item is not an object"]
    for key in _REQUIRED_ITEM_KEYS:
        if key not in item:
            errors.append(f"missing key: {key}")
    sid = item.get("seed_id", "<no-id>")
    if not (item.get("prompt") or "").strip():
        errors.append(f"{sid}: empty prompt")
    if item.get("difficulty") not in DIFFICULTIES:
        errors.append(f"{sid}: invalid difficulty {item.get('difficulty')!r}")
    if item.get("reference_type") not in REFERENCE_TYPES:
        errors.append(f"{sid}: invalid reference_type {item.get('reference_type')!r}")
    if not isinstance(item.get("tags"), list):
        errors.append(f"{sid}: tags must be a list")
    if not isinstance(item.get("capture_reasoning_recommended"), bool):
        errors.append(f"{sid}: capture_reasoning_recommended must be a boolean")
    if bucket_ids is not None and item.get("topic") not in bucket_ids:
        errors.append(f"{sid}: topic {item.get('topic')!r} not in taxonomy buckets")
    return errors


def load_corpus(specialty: str = "nephrology", *, force: bool = False) -> Dict[str, Any]:
    """Load + validate + cache the corpus for ``specialty``.

    Raises :class:`CorpusError` if the file is missing, not JSON, has no items,
    or any item fails schema validation (fail loud — never ship a bad corpus)."""
    key = (specialty or "").strip().lower()
    if not force and key in _CACHE:
        return _CACHE[key]

    cfg = get_specialty_config(key)
    path = _corpus_path(key)
    if not os.path.exists(path):
        raise CorpusError(f"Seed corpus not found for {key!r}: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise CorpusError(f"Seed corpus {path} is not valid JSON: {exc}") from exc

    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise CorpusError(f"Seed corpus {path} has no items")

    bucket_ids = cfg.bucket_ids()
    all_errors: List[str] = []
    seen_ids = set()
    for it in items:
        all_errors.extend(validate_item(it, bucket_ids=bucket_ids))
        sid = (it or {}).get("seed_id")
        if sid in seen_ids:
            all_errors.append(f"duplicate seed_id: {sid}")
        seen_ids.add(sid)
    if all_errors:
        raise CorpusError(
            f"Seed corpus {path} failed validation ({len(all_errors)} error(s)): "
            + "; ".join(all_errors[:10])
        )

    parsed = {
        "version": data.get("version") if isinstance(data, dict) else f"{key}.v1",
        "specialty": key,
        "ratified": bool(data.get("ratified")) if isinstance(data, dict) else False,
        "review_status": (data.get("review_status") if isinstance(data, dict) else None)
        or "unknown",
        "reviewed_by": (data.get("reviewed_by") if isinstance(data, dict) else None),
        "reviewed_at": (data.get("reviewed_at") if isinstance(data, dict) else None),
        "items": items,
        # Hard-Case Engine config (Seamless PRD WS2). Optional top-level keys; a
        # corpus without them still loads (hardness scoring falls back to the
        # universal rubric with no specialty failure-domain context).
        "failure_domains": (data.get("failure_domains") if isinstance(data, dict) else None) or [],
        "hard_case_archetypes": (data.get("hard_case_archetypes") if isinstance(data, dict) else None) or [],
        "hardness_rubric": (data.get("hardness_rubric") if isinstance(data, dict) else None) or [],
    }
    _CACHE[key] = parsed
    return parsed


def load_hardness_config(specialty: str = "nephrology") -> Dict[str, Any]:
    """Per-specialty Hard-Case Engine config (Seamless PRD WS2): the model
    ``failure_domains``, ``hard_case_archetypes``, and ``hardness_rubric`` the
    judge + generation read. Returns empty lists when the corpus omits them, so
    onboarding a specialty with hardness is purely additive corpus data."""
    c = load_corpus(specialty)
    return {
        "failure_domains": c.get("failure_domains") or [],
        "hard_case_archetypes": c.get("hard_case_archetypes") or [],
        "hardness_rubric": c.get("hardness_rubric") or [],
    }


def failure_domain_names(specialty: str = "nephrology") -> List[str]:
    """Flat list of failure-domain names to give the hardness judge as context."""
    out: List[str] = []
    for fd in load_hardness_config(specialty).get("failure_domains") or []:
        name = fd.get("name") if isinstance(fd, dict) else str(fd)
        if name:
            out.append(str(name))
    return out


def items_for_bucket(specialty: str, bucket_id: str) -> List[Dict[str, Any]]:
    return [it for it in load_corpus(specialty)["items"] if it.get("topic") == bucket_id]


def sample_exemplars(
    specialty: str,
    bucket_id: str,
    k: int,
    *,
    rng: Optional[random.Random] = None,
) -> List[Dict[str, Any]]:
    """Sample up to ``k`` seed exemplars from ``bucket_id`` (falls back to the
    whole corpus if the bucket is sparse) for few-shot prompting (PRD §7.1)."""
    rng = rng or random
    pool = items_for_bucket(specialty, bucket_id)
    if len(pool) < k:
        # Top up from other buckets so the model always has K exemplars.
        others = [it for it in load_corpus(specialty)["items"] if it.get("topic") != bucket_id]
        rng.shuffle(others)
        pool = pool + others
    if len(pool) <= k:
        return list(pool)
    return rng.sample(pool, k)


def all_prompts(specialty: str) -> List[str]:
    """Every seed prompt string — used for novelty/dedupe checks (PRD §7.4)."""
    return [it.get("prompt", "") for it in load_corpus(specialty)["items"]]


def corpus_metadata(specialty: str = "nephrology") -> Dict[str, Any]:
    """Admin-facing metadata for ``GET /generation/seed-corpus`` (PRD §10)."""
    corpus = load_corpus(specialty)
    by_bucket: Dict[str, int] = {}
    by_difficulty: Dict[str, int] = {}
    for it in corpus["items"]:
        b = it.get("topic") or "unknown"
        by_bucket[b] = by_bucket.get(b, 0) + 1
        d = it.get("difficulty") or "unknown"
        by_difficulty[d] = by_difficulty.get(d, 0) + 1
    cfg = get_specialty_config(specialty)
    return {
        "version": corpus["version"],
        "specialty": corpus["specialty"],
        "ratified": corpus["ratified"],
        "review_status": corpus["review_status"],
        "reviewed_by": corpus.get("reviewed_by"),
        "reviewed_at": corpus.get("reviewed_at"),
        "total": len(corpus["items"]),
        "by_bucket": by_bucket,
        "by_difficulty": by_difficulty,
        "taxonomy": [
            {"id": b.id, "label": b.label, "target_count": b.target_count,
             "min_difficulty": b.min_difficulty, "have": by_bucket.get(b.id, 0)}
            for b in cfg.taxonomy
        ],
    }
