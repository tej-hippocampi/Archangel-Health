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
from typing import Any, Dict, List, Optional


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


# ─── Hepatology taxonomy (V4 Cases & Promotion PRD §1.4) ──────────────────────
# Onboarded to unblock the real hepatobiliary charts, which had no enabled
# specialty to route to at all: patient-1 and patient-3 are portal-hypertension /
# portal-biliopathy records, and ``/generate`` refused them with "specialty
# 'hepatology' is not enabled in this release".
#
# All buckets ``min_difficulty: hard``. Hepatology's characteristic model failure
# is the DISSOCIATION: one number moves the wrong way while the enzymes that
# actually answer the question move the right way, and the model anchors on the
# single loud value. Target counts sum to 100.
HEPATOLOGY_TAXONOMY: List[TaxonomyBucket] = [
    TaxonomyBucket(
        id="portal_hypertension",
        label="Portal hypertension & its decompensations",
        min_difficulty="hard",
        target_count=24,
        subtopics=["variceal bleeding", "ascites", "hepatorenal syndrome",
                   "portal vein thrombosis", "hepatic encephalopathy"],
    ),
    TaxonomyBucket(
        id="biliary_obstruction",
        label="Biliary obstruction & post-procedural course",
        min_difficulty="hard",
        target_count=24,
        subtopics=["choledocholithiasis", "stricture", "post-ERCP complications",
                   "cholangitis", "stent management"],
    ),
    TaxonomyBucket(
        id="liver_injury_patterns",
        label="Liver-injury patterns & the dissociations that mislead",
        min_difficulty="hard",
        target_count=22,
        subtopics=["cholestatic vs hepatocellular", "drug-induced liver injury",
                   "viral hepatitis", "enzyme-bilirubin dissociation"],
    ),
    TaxonomyBucket(
        id="cirrhosis_complications",
        label="Cirrhosis: synthetic failure & competing-risk management",
        min_difficulty="hard",
        target_count=18,
        subtopics=["coagulopathy vs bleeding risk", "transfusion thresholds",
                   "spontaneous bacterial peritonitis", "hyponatremia in cirrhosis"],
    ),
    TaxonomyBucket(
        id="hepatic_drug_safety",
        label="Drug safety & dosing in liver disease",
        min_difficulty="hard",
        target_count=12,
        subtopics=["hepatotoxicity", "dosing in hepatic impairment",
                   "sedation and encephalopathy", "anticoagulation in PVT"],
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
    # Hepatology reasoning lives in the ENZYME TRAJECTORY, not in any single value
    # (V4 PRD §1.4); its documented failure is anchoring on one loud number while
    # the trend that answers the question runs the other way.
    "hepatology": SpecialtyConfig(
        name="hepatology",
        seed_corpus="seed_corpus/hepatology.v1.json",
        taxonomy=HEPATOLOGY_TAXONOMY,
        enabled=True,
        # green|orange|pink only — no blue (console palette). Green is shared with
        # nephrology; the chip is labelled, and the palette has no fourth token.
        accent="green",
        blurb="Portal hypertension, biliary obstruction, and the enzyme-bilirubin "
              "dissociations that mislead.",
    ),
}


class SpecialtyMisconfigured(RuntimeError):
    """An ENABLED specialty has no usable seed corpus (V4 PRD §1.4).

    Raised at import, deliberately: the corpus is what ``classify_case_to_bucket``
    matches against, so a specialty enabled with an empty or stub corpus does not
    fail — it silently sorts every case of that specialty into NO bucket, and the
    export ships with an unusable taxonomy field that nobody notices until a buyer
    filters on it. A registry entry and a corpus file are one change; this makes
    them one change in fact and not just by convention."""


def _assert_enabled_specialties_have_corpora() -> None:
    """Every ``enabled=True`` specialty must have a readable, non-empty corpus.

    Deliberately a plain JSON existence + item-count check rather than a call to
    :mod:`asclepius.corpus`: that module imports this one, and the point is to fail
    at import of the registry itself, before anything can consult it. ``corpus``
    still does the FULL schema validation on first load — this is the cheap check
    that cannot be deferred, not a replacement for it."""
    import json as _json
    import os as _os

    here = _os.path.dirname(__file__)
    broken: List[str] = []
    for cfg in SPECIALTY_REGISTRY.values():
        if not cfg.enabled:
            continue          # a held specialty is allowed to have no corpus yet
        path = _os.path.join(here, cfg.seed_corpus)
        if not _os.path.exists(path):
            broken.append(f"{cfg.name}: seed corpus missing ({cfg.seed_corpus})")
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = _json.load(fh)
        except (OSError, ValueError) as exc:
            broken.append(f"{cfg.name}: seed corpus unreadable ({exc})")
            continue
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list) or not items:
            broken.append(f"{cfg.name}: seed corpus has no items — a stub corpus "
                          "classifies every case into no bucket")
        if not cfg.taxonomy:
            broken.append(f"{cfg.name}: enabled with an empty taxonomy")
    if broken:
        raise SpecialtyMisconfigured(
            "Specialty registry is inconsistent with the seed corpora on disk. "
            "Either ship the corpus or set enabled=False and hold the cases: "
            + "; ".join(broken))


_assert_enabled_specialties_have_corpora()


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


# ─── Matching a free-text specialty to a registry entry ──────────────────────
#
# Written because task-notify recipient resolution was a bare SQL equality
# (``lower(trim(specialty)) = lower(trim(?))``). A task filed as "renal" or a
# physician who typed "Nephrology - Transplant" matched NOBODY, enqueued zero
# outbox rows, and the caller swallowed the empty result. The batch reached
# nobody and nothing said so.
#
# These are MATCHING aids for notification and channel routing only. They are
# deliberately NOT used to widen who may draw a case: that is a separate
# decision (``store.labeler_queue_sql``) and widening it here by accident is
# how a cardiologist ends up labelling a nephrology chart.

# Common ways one specialty gets written down. Lowercase, matched whole.
SPECIALTY_ALIASES: Dict[str, str] = {
    "renal": "nephrology",
    "renal medicine": "nephrology",
    "kidney": "nephrology",
    "nephro": "nephrology",
    "cardiac": "cardiology",
    "cardio": "cardiology",
    "heart": "cardiology",
    "cardiovascular": "cardiology",
    "cardiovascular medicine": "cardiology",
    "onc": "oncology",
    "medical oncology": "oncology",
    "haematology oncology": "oncology",
    "hematology oncology": "oncology",
    "hem onc": "oncology",
    "heme onc": "oncology",
    "hepatic": "hepatology",
    "hepato": "hepatology",
    "liver": "hepatology",
    "gi hepatology": "hepatology",
}


def _normalize(raw: str) -> str:
    """Lowercase, trim, and collapse the separators people actually type.

    "Nephrology - Transplant", "nephrology/transplant" and "Nephrology
    (transplant)" all normalize to the same leading token set.
    """
    s = (raw or "").strip().lower()
    for ch in ("-", "/", "\\", "(", ")", ",", ":", "|", "&", "+", "."):
        s = s.replace(ch, " ")
    return " ".join(s.split())


def _contains_phrase(tokens: List[str], phrase: List[str]) -> bool:
    """Whether ``phrase`` appears as a contiguous run of WHOLE tokens.

    Whole tokens, because substring matching turns "cardiothoracic" into
    "cardio" and mails a surgeon somebody else's queue.
    """
    if not phrase or len(phrase) > len(tokens):
        return False
    return any(
        tokens[i : i + len(phrase)] == phrase
        for i in range(len(tokens) - len(phrase) + 1)
    )


def match_specialty(raw: str) -> Optional[str]:
    """Best-effort map from free text to a registry specialty name, or None.

    Order matters: an exact registry hit wins, then a whole-string alias, then
    a leading-token match ("nephrology transplant" -> nephrology), then a
    contained registry name. Returns None rather than guessing when nothing
    matches, because a WRONG specialty is worse than a missing one: it routes
    the case to the wrong physician pool and mislabels it in the export,
    invisibly. That rule already governs promotion (``specialty_is_undetermined``)
    and it governs matching too.
    """
    s = _normalize(raw)
    if not s:
        return None
    if s in SPECIALTY_REGISTRY:
        return s
    if s in SPECIALTY_ALIASES:
        return SPECIALTY_ALIASES[s]
    tokens = s.split()
    if tokens:
        head = tokens[0]
        if head in SPECIALTY_REGISTRY:
            return head
        if head in SPECIALTY_ALIASES:
            return SPECIALTY_ALIASES[head]
    # The practitioner noun: "nephrologist" for "nephrology". Anchored on the
    # registry name's stem and applied per token, so "cardiologist" matches
    # cardiology while "cardiothoracic" (stem "cardiolog" is not a prefix of it)
    # still does not.
    for name in SPECIALTY_REGISTRY:
        stem = name[:-1] if name.endswith("y") else name
        if len(stem) >= 6 and any(tok.startswith(stem) for tok in tokens):
            return name

    # Fall back to a WHOLE-TOKEN match, never a substring one. "cardiothoracic
    # surgery" contains the letters of the "cardio" alias, and a cardiothoracic
    # surgeon is not a cardiologist: routing their inbox as one is the exact
    # failure this function is supposed to prevent.
    for name in SPECIALTY_REGISTRY:
        if _contains_phrase(tokens, name.split()):
            return name
    for alias, name in SPECIALTY_ALIASES.items():
        if _contains_phrase(tokens, alias.split()):
            return name
    return None


def equivalent_specialty_terms(specialty: str) -> List[str]:
    """Every spelling that should be treated as this specialty, for an SQL IN.

    Includes the canonical name and every alias pointing at it. The caller
    still normalizes the stored column, so this covers vocabulary drift rather
    than whitespace or case.
    """
    canon = match_specialty(specialty)
    if not canon:
        term = _normalize(specialty)
        return [term] if term else []
    terms = {canon}
    terms.update(a for a, target in SPECIALTY_ALIASES.items() if target == canon)
    return sorted(terms)
