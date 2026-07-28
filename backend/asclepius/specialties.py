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
    # Presentation metadata (PRD §1/§6): the picker + case panel read these so a
    # new specialty's chip + scope blurb are config, never a frontend change.
    # ``accent`` is a console-palette token name (green|orange|pink — no blue).
    accent: str = "green"
    blurb: str = ""

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


# ─── Cardiology taxonomy (Specialty Hyper-Personalization PRD §4.2) ────────────
# All buckets ``min_difficulty: hard`` — cardiology is a first-class hard-case
# specialty. The decisive signal lives in a study (ECG/echo/cath/biomarker) and
# contradicts the loud vignette (§4.3). Replaces the earlier 3-bucket stub. Target
# counts sum to 100.
CARDIOLOGY_TAXONOMY: List[TaxonomyBucket] = [
    TaxonomyBucket(
        id="ecg_high_risk_subtle",
        label="Under-called high-risk ECG patterns",
        min_difficulty="hard",
        target_count=20,
        subtopics=["wellens", "de_winter", "posterior_mi", "hyperacute_t",
                   "brugada", "hyperkalemia_morphology", "digoxin_effect_vs_toxicity", "long_qt"],
    ),
    TaxonomyBucket(
        id="great_mimics",
        label="The great mimics (anchoring traps)",
        min_difficulty="hard",
        target_count=20,
        subtopics=["cardiac_amyloid", "dissection_as_mi", "takotsubo",
                   "myocarditis", "minoca"],
    ),
    TaxonomyBucket(
        id="hf_gdmt",
        label="Heart-failure GDMT + electrolyte/renal trade-offs",
        min_difficulty="hard",
        target_count=16,
        subtopics=["arni_washout", "beta_blocker_decompensation", "mra_potassium_ckd",
                   "sglt2i_hfpef", "guideline_recency"],
    ),
    TaxonomyBucket(
        id="arrhythmia_anticoag",
        label="Arrhythmia & anticoagulation trade-offs",
        min_difficulty="hard",
        target_count=16,
        subtopics=["af_stroke_vs_bleed", "doac_dosing_ckd", "periprocedural_bridging",
                   "triple_therapy", "anticoag_after_ich"],
    ),
    TaxonomyBucket(
        id="valve_structural",
        label="Valvular & structural heart disease",
        min_difficulty="hard",
        target_count=14,
        subtopics=["as_vs_amyloid", "low_flow_low_gradient", "endocarditis"],
    ),
    TaxonomyBucket(
        id="acs_nuance",
        label="ACS nuance & troponin interpretation",
        min_difficulty="hard",
        target_count=14,
        subtopics=["type_2_mi", "minoca", "troponin_interpretation", "dapt_strategy"],
    ),
]


# ─── Oncology taxonomy (Specialty Hyper-Personalization PRD §5.2) ──────────────
# All buckets ``min_difficulty: hard``. Oncology's documented failure is
# right-answer-wrong-reason: the decisive signal lives in the pathology/molecular/
# temporal-imaging data and contradicts the histology- or progression-anchored
# shortcut (§5.3). Target counts sum to 100.
ONCOLOGY_TAXONOMY: List[TaxonomyBucket] = [
    TaxonomyBucket(
        id="immunotherapy_toxicity_vs_progression",
        label="Immunotherapy toxicity vs progression",
        min_difficulty="hard",
        target_count=20,
        subtopics=["irae", "pseudoprogression", "hyperprogression",
                   "checkpoint_myocarditis", "pneumonitis_colitis"],
    ),
    TaxonomyBucket(
        id="molecular_therapy_selection",
        label="Molecular-over-histology therapy selection",
        min_difficulty="hard",
        target_count=20,
        subtopics=["egfr", "t790m_resistance", "alk", "braf", "ntrk",
                   "msi_high_tmb", "pd_l1_vs_driver"],
    ),
    TaxonomyBucket(
        id="onc_emergencies",
        label="Oncologic emergencies",
        min_difficulty="hard",
        target_count=20,
        subtopics=["tumor_lysis", "febrile_neutropenia", "cord_compression",
                   "svc_syndrome", "hypercalcemia", "hyperviscosity"],
    ),
    TaxonomyBucket(
        id="staging_biomarker",
        label="Staging & biomarker-confirmatory discrepancy",
        min_difficulty="hard",
        target_count=14,
        subtopics=["tnm_traps", "ai_vs_confirmatory_molecular", "biomarker_discrepancy"],
    ),
    TaxonomyBucket(
        id="paraneoplastic",
        label="Paraneoplastic syndromes",
        min_difficulty="hard",
        target_count=14,
        subtopics=["siadh", "pthrp_hypercalcemia", "lems"],
    ),
    TaxonomyBucket(
        id="supportive_tradeoffs",
        label="Supportive-care trade-offs",
        min_difficulty="hard",
        target_count=12,
        subtopics=["anticoagulation_in_malignancy", "dosing_in_organ_dysfunction",
                   "goals_of_care", "correction_rate_safety"],
    ),
]


SPECIALTY_REGISTRY: Dict[str, SpecialtyConfig] = {
    "nephrology": SpecialtyConfig(
        name="nephrology",
        seed_corpus="seed_corpus/nephrology.v1.json",
        taxonomy=NEPHROLOGY_TAXONOMY,
        enabled=True,
        accent="green",
        blurb="Electrolytes, AKI/CKD, dialysis, transplant, glomerular — labs-driven.",
    ),
    # Config-only onboarding: a new specialty is a corpus file + a taxonomy + a
    # registry entry, nothing else. Cardiology reasoning lives in the ECG/echo/cath
    # + biomarkers (PRD §4).
    "cardiology": SpecialtyConfig(
        name="cardiology",
        seed_corpus="seed_corpus/cardiology.v1.json",
        taxonomy=CARDIOLOGY_TAXONOMY,
        enabled=True,
        accent="orange",
        blurb="ECG/echo/cath grounding, the great mimics, GDMT & anticoagulation trade-offs.",
    ),
    # Oncology reasoning lives in the pathology/molecular/temporal-imaging data
    # (PRD §5); its documented failure is right-answer-wrong-reason.
    "oncology": SpecialtyConfig(
        name="oncology",
        seed_corpus="seed_corpus/oncology.v1.json",
        taxonomy=ONCOLOGY_TAXONOMY,
        enabled=True,
        accent="pink",
        blurb="irAEs vs progression, molecular-over-histology, oncologic emergencies.",
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
                "accent": cfg.accent,
                "blurb": cfg.blurb,
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
