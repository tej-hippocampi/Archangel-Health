"""Specialty scoping + future-proof registry (PRD §8).

v1 is nephrology-only, but the generation engine never hardcodes a specialty: it
looks up a :class:`SpecialtyConfig` here. Adding a future specialty is pure
config — drop a ``seed_corpus/<specialty>.vN.json`` + a taxonomy + flip
``enabled=True`` — with zero pipeline changes (PRD §15).

A request for a specialty that is unknown or ``enabled=False`` raises
:class:`SpecialtyNotEnabled`, which the router maps to ``400 specialty_not_enabled``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


class SpecialtyNotEnabled(ValueError):
    """Raised when a specialty is unknown or not enabled in v1 (PRD §8)."""


@dataclass(frozen=True)
class TaxonomyBucket:
    """One coverage bucket (PRD §5.3). ``target_count`` is the full-corpus goal
    (sums to 100); ``min_difficulty`` keeps the bucket off easy recall."""

    id: str
    label: str
    min_difficulty: str = "medium"
    target_count: int = 0
    subtopics: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SpecialtyConfig:
    name: str
    seed_corpus: str  # path relative to this package (asclepius/)
    taxonomy: List[TaxonomyBucket]
    enabled: bool = False

    def bucket(self, bucket_id: str) -> TaxonomyBucket:
        for b in self.taxonomy:
            if b.id == bucket_id:
                return b
        raise KeyError(bucket_id)

    def bucket_ids(self) -> List[str]:
        return [b.id for b in self.taxonomy]


# ─── Nephrology taxonomy (PRD §5.3) — 8 buckets, target counts sum to 100 ──────
NEPHROLOGY_TAXONOMY: List[TaxonomyBucket] = [
    TaxonomyBucket(
        id="renal_drug_dosing",
        label="Renal drug dosing & contraindications by eGFR",
        min_difficulty="medium",
        target_count=16,
        subtopics=["metformin_threshold", "doac_dosing", "gabapentinoid",
                   "sglt2i_initiation", "contrast", "nsaid", "antibiotic_adjustment"],
    ),
    TaxonomyBucket(
        id="dialysis_prescription",
        label="Dialysis prescription & adequacy",
        min_difficulty="medium",
        target_count=14,
        subtopics=["hyperkalemia_dialysate_K", "anemia_esa_iv_iron", "ultrafiltration_rate",
                   "kt_v_adequacy", "mineral_bone_disease"],
    ),
    TaxonomyBucket(
        id="electrolyte_acid_base",
        label="Electrolyte & acid-base correction rates and safety",
        min_difficulty="medium",
        target_count=16,
        subtopics=["hyponatremia_ods", "hyperkalemia_treatment", "hypercalcemia",
                   "mixed_acid_base"],
    ),
    TaxonomyBucket(
        id="recent_standard_of_care",
        label="Recently-updated standard-of-care (AI cutoff-lag zone)",
        min_difficulty="medium",
        target_count=14,
        subtopics=["sglt2i_ckd", "finerenone", "glp1_ckd", "kdigo_2024_ckd", "kdigo_2025_igan"],
    ),
    TaxonomyBucket(
        id="transplant",
        label="Transplant nephrology",
        min_difficulty="hard",
        target_count=10,
        subtopics=["tacrolimus_dosing", "tacrolimus_interaction", "rejection_workup",
                   "bk_cmv", "immunosuppression_special"],
    ),
    TaxonomyBucket(
        id="glomerular_autoimmune",
        label="Glomerular & autoimmune disease",
        min_difficulty="medium",
        target_count=12,
        subtopics=["lupus_nephritis", "anca_vasculitis", "igan", "nephrotic_management"],
    ),
    TaxonomyBucket(
        id="aki_critical_care",
        label="AKI & critical care nephrology",
        min_difficulty="hard",
        target_count=10,
        subtopics=["crrt_vs_ihd", "contrast_associated_aki", "hepatorenal", "rhabdomyolysis"],
    ),
    TaxonomyBucket(
        id="special_populations",
        label="Special populations & tradeoff-heavy judgment calls",
        min_difficulty="hard",
        target_count=8,
        subtopics=["pregnancy_ckd", "frailty_conservative", "pediatric_dosing", "goals_of_care"],
    ),
]


# ─── Cardiology taxonomy (Seamless PRD WS2 — config-only onboarding demo) ──────
# Proves the engine is specialty-agnostic: this taxonomy + seed_corpus/
# cardiology.v1.json + the registry entry below are the ONLY additions needed to
# enable a new specialty. The Seedmaker, hardness judge, and hard-only serving
# read this config with zero pipeline changes (see docs/ADD_A_SPECIALTY.md).
CARDIOLOGY_TAXONOMY: List[TaxonomyBucket] = [
    TaxonomyBucket(
        id="hf_gdmt",
        label="Heart-failure guideline-directed medical therapy",
        min_difficulty="hard",
        target_count=40,
        subtopics=["arni_initiation", "beta_blocker_titration", "mra_potassium", "sglt2i_hf"],
    ),
    TaxonomyBucket(
        id="arrhythmia_anticoag",
        label="Arrhythmia & anticoagulation trade-offs",
        min_difficulty="hard",
        target_count=30,
        subtopics=["doac_dosing_ckd", "af_stroke_bleeding", "periprocedural_bridging"],
    ),
    TaxonomyBucket(
        id="acs_antithrombotic",
        label="ACS antithrombotic strategy",
        min_difficulty="hard",
        target_count=30,
        subtopics=["dapt_duration", "de_escalation", "triple_therapy"],
    ),
]


SPECIALTY_REGISTRY: Dict[str, SpecialtyConfig] = {
    "nephrology": SpecialtyConfig(
        name="nephrology",
        seed_corpus="seed_corpus/nephrology.v1.json",
        taxonomy=NEPHROLOGY_TAXONOMY,
        enabled=True,
    ),
    # Config-only onboarding demo (PRD §15 / Seamless WS2): a new specialty is a
    # corpus file + a taxonomy + a registry entry, nothing else. Enabled so the
    # hard-case engine + serving can be demonstrated end-to-end for cardiology.
    "cardiology": SpecialtyConfig(
        name="cardiology",
        seed_corpus="seed_corpus/cardiology.v1.json",
        taxonomy=CARDIOLOGY_TAXONOMY,
        enabled=True,
    ),
}


def get_specialty_config(specialty: str) -> SpecialtyConfig:
    """Return the config for an ENABLED specialty, else raise SpecialtyNotEnabled."""
    cfg = SPECIALTY_REGISTRY.get((specialty or "").strip().lower())
    if cfg is None:
        raise SpecialtyNotEnabled(f"Unknown specialty: {specialty!r}")
    if not cfg.enabled:
        raise SpecialtyNotEnabled(f"Specialty not enabled in this release: {specialty!r}")
    return cfg


def is_enabled(specialty: str) -> bool:
    cfg = SPECIALTY_REGISTRY.get((specialty or "").strip().lower())
    return bool(cfg and cfg.enabled)


def list_specialties() -> List[Dict[str, Any]]:
    """Public listing for ``GET /specialties`` (drives future doctor self-serve)."""
    out: List[Dict[str, Any]] = []
    for cfg in SPECIALTY_REGISTRY.values():
        out.append(
            {
                "specialty": cfg.name,
                "enabled": cfg.enabled,
                "seed_corpus": cfg.seed_corpus,
                "buckets": [
                    {
                        "id": b.id,
                        "label": b.label,
                        "min_difficulty": b.min_difficulty,
                        "target_count": b.target_count,
                    }
                    for b in cfg.taxonomy
                ],
            }
        )
    return out
