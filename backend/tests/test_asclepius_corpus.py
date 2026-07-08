"""Seed corpus + specialty registry tests (PRD §5, §8).

The committed nephrology corpus must load, be fully schema-valid, span all eight
taxonomy buckets, and report itself unratified (AI-drafted pending clinician
review). The registry must enable only nephrology in v1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402,F401  (sets env before imports)
from asclepius import corpus as asc_corpus  # noqa: E402
from asclepius import specialties as asc_specialties  # noqa: E402


def test_corpus_loads_and_is_schema_valid():
    c = asc_corpus.load_corpus("nephrology", force=True)
    assert c["version"] == "nephrology.v1"
    assert c["ratified"] is False
    # Unratified honesty fields (fixes P1-C): present, null until human sign-off.
    assert c["reviewed_by"] is None
    assert c["reviewed_at"] is None
    assert len(c["items"]) == 100
    # every item passes the §5.2 schema
    bucket_ids = asc_specialties.get_specialty_config("nephrology").bucket_ids()
    for it in c["items"]:
        assert asc_corpus.validate_item(it, bucket_ids=bucket_ids) == []


def test_corpus_meets_per_bucket_targets():
    meta = asc_corpus.corpus_metadata("nephrology")
    cfg = asc_specialties.get_specialty_config("nephrology")
    assert meta["total"] == 100
    for b in cfg.taxonomy:
        have = meta["by_bucket"].get(b.id, 0)
        assert have >= b.target_count, f"{b.id}: have {have} < target {b.target_count}"


def test_corpus_honors_bucket_min_difficulty():
    rank = {"easy": 0, "medium": 1, "hard": 2}
    c = asc_corpus.load_corpus("nephrology", force=True)
    cfg = asc_specialties.get_specialty_config("nephrology")
    floors = {b.id: b.min_difficulty for b in cfg.taxonomy}
    for it in c["items"]:
        floor = floors[it["topic"]]
        assert rank[it["difficulty"]] >= rank[floor], it["seed_id"]


def test_corpus_majority_hard():
    c = asc_corpus.load_corpus("nephrology", force=True)
    hard = sum(1 for it in c["items"] if it["difficulty"] == "hard")
    assert hard >= len(c["items"]) // 2


def test_corpus_covers_all_eight_buckets():
    meta = asc_corpus.corpus_metadata("nephrology")
    cfg = asc_specialties.get_specialty_config("nephrology")
    assert set(meta["by_bucket"]) == set(cfg.bucket_ids())
    assert len(cfg.bucket_ids()) == 8
    for bucket_id, n in meta["by_bucket"].items():
        assert n >= 1, bucket_id


def test_corpus_is_contamination_clean():
    from asclepius.validation import contamination_hits
    for p in asc_corpus.all_prompts("nephrology"):
        assert contamination_hits(p) == [], p[:60]


def test_sample_exemplars_returns_k():
    ex = asc_corpus.sample_exemplars("nephrology", "transplant", 6)
    assert len(ex) == 6
    assert all("prompt" in e for e in ex)


def test_registry_enabled_specialties():
    # nephrology (v1) + cardiology (Seamless PRD WS2 config-only onboarding demo).
    specs = {s["specialty"]: s["enabled"] for s in asc_specialties.list_specialties()}
    assert specs.get("nephrology") is True
    assert specs.get("cardiology") is True


def test_unknown_or_disabled_specialty_raises():
    # A specialty with no registry entry raises (config-only onboarding gate).
    with pytest.raises(asc_specialties.SpecialtyNotEnabled):
        asc_specialties.get_specialty_config("dermatology")
    assert asc_specialties.is_enabled("nephrology") is True
    assert asc_specialties.is_enabled("dermatology") is False


# ─── Multimodal archetypes (Synthetic Multimodal Cases PRD §10) ────────────────
def test_multimodal_archetypes_present_and_wellformed():
    from asclepius.generation import _multimodal_archetypes
    neph = _multimodal_archetypes("nephrology")
    card = _multimodal_archetypes("cardiology")
    # Full Appendix A nephrology set + a cardiology multimodal set.
    assert len(neph) >= 10, len(neph)
    assert len(card) >= 4, len(card)
    seen_topics = set()
    for a in neph + card:
        assert a.get("topic") and a["topic"] not in seen_topics, a.get("topic")
        seen_topics.add(a["topic"])
        mm = a.get("multimodal") or {}
        assert mm.get("panels"), a["topic"]          # at least one lab panel to reason across
        assert mm.get("hard_hook"), a["topic"]       # the data-integration trap
        assert mm.get("ground_truth_spec"), a["topic"]  # the fixed answer key spec
        # No imaging modality is ever seeded (PRD §2).
        blob = (json.dumps(mm)).lower()
        assert "dicom" not in blob and "imaging" not in blob, a["topic"]


def test_multimodal_archetype_failure_domains_valid():
    """Each multimodal archetype's failure_domain must be one the corpus declares,
    so the hardness judge gets valid domain context."""
    from asclepius.generation import _multimodal_archetypes, load_hardness_config
    for sp in ("nephrology", "cardiology"):
        domains = {f["name"] for f in (load_hardness_config(sp).get("failure_domains") or [])}
        for a in _multimodal_archetypes(sp):
            assert a.get("failure_domain") in domains, (sp, a.get("topic"), a.get("failure_domain"))
