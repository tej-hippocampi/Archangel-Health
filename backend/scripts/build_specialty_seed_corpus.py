#!/usr/bin/env python3
"""Reproducible builder for the cardiology + oncology seed corpora (PRD §7).

Each committed corpus (``backend/asclepius/seed_corpus/{cardiology,oncology}.v1.json``)
is built from the SAME 20 authored hard cases that seed the gold sets, so the
Seedmaker few-shots from genuinely model-breaking exemplars. The seed ``items`` and
the multimodal ``hard_case_archetypes`` are derived from ``gold_cases.py`` (guaranteeing
topic/subtopic consistency with the taxonomy); the ``failure_domains`` and
``hardness_rubric`` are curated from the PRD's §4.1/§5.1 research grounding.

Usage (run from backend/):
    python3 scripts/build_specialty_seed_corpus.py            # write both corpora
    python3 scripts/build_specialty_seed_corpus.py --check    # validate only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius.gold_cases import GOLD_CARDIOLOGY_CASES, GOLD_ONCOLOGY_CASES  # noqa: E402
from asclepius.specialties import get_specialty_config  # noqa: E402

SEED_DIR = Path(__file__).resolve().parent.parent / "asclepius" / "seed_corpus"


# ── Curated per-specialty hard-case-engine config (PRD §4/§5) ────────────────
CARDIOLOGY_FAILURE_DOMAINS = [
    {"name": "visual_finding_grounding", "weight": 0.30,
     "why": "The dominant cardiology failure: the model knows a diagnosis's criteria "
            "but does not read the ECG/echo tracing INTO the reasoning — it pattern-"
            "matches the vignette words (right-answer-wrong-reason at scale)."},
    {"name": "great_mimics_anchoring", "weight": 0.25,
     "why": "Amyloid masquerading as AS/HCM/ACS; aortic dissection mimicking STEMI; "
            "takotsubo vs anterior STEMI — the catastrophic error is antithrombotics "
            "in dissection."},
    {"name": "under_called_high_risk_ecg", "weight": 0.20,
     "why": "Wellens, de Winter, posterior/right-sided MI, hyperacute T, Brugada, "
            "hyperkalemia morphology, digoxin effect vs toxicity — the benign-looking "
            "but deadly trap."},
    {"name": "gdmt_electrolyte_sequencing", "weight": 0.13,
     "why": "ARNI washout after ACEi, MRA + potassium + CKD, SGLT2i in HFpEF, "
            "beta-blocker in decompensation, guideline-recency lag."},
    {"name": "anticoagulation_tradeoffs", "weight": 0.12,
     "why": "AF stroke-vs-bleed, DOAC dosing in CKD, periprocedural bridging, triple "
            "therapy, anticoagulation after intracranial hemorrhage."},
]

CARDIOLOGY_RUBRIC = [
    "The decisive signal lives in a STUDY (ECG/echo/cath/biomarker) and contradicts the loud vignette.",
    "Requires reading the study finding into the reasoning, not pattern-matching the stem.",
    "Involves a genuine competing-risk or efficacy-vs-safety trade-off.",
    "Sits in a documented cardiology model-failure domain.",
    "Contains a plausible trap (the reflex answer is wrong or dangerous for this patient).",
    "At least one case per batch carries a catastrophic-if-wrong action (unsafe_recommendation).",
    "High clinical stakes (safety-relevant).",
]

ONCOLOGY_FAILURE_DOMAINS = [
    {"name": "reasoning_errors_right_answer_wrong_reason", "weight": 0.28,
     "why": "Reasoning errors dominate oncology-note interpretation (confirmation bias "
            "+ anchoring); models reach correct conclusions via faulty reasoning — "
            "invisible to accuracy-only evaluation."},
    {"name": "immunotherapy_toxicity_vs_progression", "weight": 0.22,
     "why": "Pseudoprogression vs true progression; irAEs (pneumonitis/colitis/"
            "hypophysitis/myocarditis) mis-attributed to progression or infection."},
    {"name": "molecular_over_histology", "weight": 0.20,
     "why": "Actionable driver/resistance mutation (EGFR T790M→osimertinib, MSI-high→"
            "checkpoint, ALK/BRAF/NTRK) changes the answer; models treat by histology "
            "and miss the NGS panel."},
    {"name": "oncologic_emergencies", "weight": 0.18,
     "why": "TLS from modern targeted/immuno agents (not just heme/cytotoxic), febrile "
            "neutropenia, cord compression, SVC, hypercalcemia — under-urgency kills."},
    {"name": "paraneoplastic_and_supportive", "weight": 0.12,
     "why": "SIADH (correction-rate/ODS), PTHrP hypercalcemia, LEMS; anticoagulation "
            "and dosing trade-offs in organ dysfunction."},
]

ONCOLOGY_RUBRIC = [
    "The decisive signal lives in the pathology/molecular/temporal-imaging data and contradicts the histology- or progression-anchored shortcut.",
    "At least half of each batch is reachable by faulty reasoning to the correct answer (the reasoning trace carries the value).",
    "Requires integrating the molecular/temporal evidence, not the loud narrative.",
    "Sits in a documented oncology model-failure domain.",
    "Contains a plausible trap (the reflex 'switch therapy / treat by histology / TLS only if heme' is wrong).",
    "At least one case per batch carries a time-critical or catastrophic-if-delayed action.",
    "High clinical stakes (safety-relevant).",
]


# Each taxonomy bucket maps to a DECLARED failure_domain name (the hardness judge
# gets valid domain context; corpus tests enforce this).
BUCKET_TO_FAILURE_DOMAIN = {
    # cardiology
    "ecg_high_risk_subtle": "under_called_high_risk_ecg",
    "great_mimics": "great_mimics_anchoring",
    "hf_gdmt": "gdmt_electrolyte_sequencing",
    "arrhythmia_anticoag": "anticoagulation_tradeoffs",
    "valve_structural": "visual_finding_grounding",
    "acs_nuance": "visual_finding_grounding",
    # oncology
    "immunotherapy_toxicity_vs_progression": "immunotherapy_toxicity_vs_progression",
    "molecular_therapy_selection": "molecular_over_histology",
    "onc_emergencies": "oncologic_emergencies",
    "staging_biomarker": "molecular_over_histology",
    "paraneoplastic": "paraneoplastic_and_supportive",
    "supportive_tradeoffs": "paraneoplastic_and_supportive",
}


def _panels(case):
    return [p.get("panel", "panel") for p in case.get("lab_panels", [])]


def _study_labels(case):
    return [f"{s.get('modality', 'study').upper()}: {s.get('label') or s.get('modality')}"
            for s in case.get("studies", [])]


def _archetype_from_case(entry):
    """A multimodal hard_case_archetype seeded from an authored gold case — the
    generator varies it into new PHI-free cases of the same difficulty shape."""
    case = entry["case"]
    hard_hook = case.get("hard_hook", "")
    return {
        "topic": entry["taxonomy_bucket"],
        "failure_domain": BUCKET_TO_FAILURE_DOMAIN.get(entry["taxonomy_bucket"], entry["taxonomy_bucket"]),
        "why_hard": hard_hook,
        "axes": ["diagnostic_trap", "data_integration", "multi_step", "high_stakes"],
        "multimodal": {
            "panels": _panels(case),
            "studies": _study_labels(case),
            "note_types": [n.get("note_type", "Progress") for n in case.get("notes", [])],
            "hard_hook": hard_hook,
            "ground_truth_spec": (case.get("ground_truth") or {}).get("answer", "")[:400],
            "reasoning_divergence": case.get("reasoning_divergence", ""),
        },
    }


def _item_from_case(entry, idx, prefix):
    case = entry["case"]
    return {
        "seed_id": f"{prefix}-seed-{idx:04d}",
        "specialty": case["specialty"],
        "topic": entry["taxonomy_bucket"],
        "subtopic": entry["subtopic"],
        "difficulty": "hard",
        "prompt": entry["question"],
        "ai_failure_mode": entry["ai_failure_mode"],
        "why_high_value": case.get("reasoning_divergence", ""),
        "reference_basis": (case.get("ground_truth") or {}).get("rationale", ""),
        "reference_type": "guideline",
        "capture_reasoning_recommended": True,
        "tags": [entry["taxonomy_bucket"], entry["subtopic"], case["specialty"]],
    }


def build_corpus(specialty, gold, prefix, failure_domains, rubric):
    # One archetype per DISTINCT bucket (dedupe), plus one seed item per gold case.
    archetypes, seen_buckets = [], set()
    for entry in gold:
        b = entry["taxonomy_bucket"]
        if b not in seen_buckets:
            seen_buckets.add(b)
            archetypes.append(_archetype_from_case(entry))
    items = [_item_from_case(e, i + 1, prefix) for i, e in enumerate(gold)]
    return {
        "version": f"{specialty}.v1",
        "specialty": specialty,
        "ratified": True,
        "review_status": "clinician_authored_exemplars",
        "note": (
            f"Built from the {len(gold)} authored hard {specialty} cases "
            f"(Asclepius_Cardiology_Oncology_Cases.md). Each is engineered so a frontier "
            f"model fails — wrong ground truth, or right answer via broken reasoning. The "
            f"decisive signal lives in a STUDY (ECG/echo/cath/pathology/molecular/imaging). "
            f"These seed the Seedmaker few-shots and the gold set; every generated case "
            f"must still clear the empirical-difficulty gate (PRD §9) before it ships."
        ),
        "reviewed_by": "asclepius_specialty_hyperpersonalization_prd",
        "reviewed_at": None,
        "generated_by": "scripts/build_specialty_seed_corpus.py",
        "failure_domains": failure_domains,
        "hardness_rubric": rubric,
        "hard_case_archetypes": archetypes,
        "items": items,
    }


SPECS = [
    ("cardiology", GOLD_CARDIOLOGY_CASES, "cardio", CARDIOLOGY_FAILURE_DOMAINS, CARDIOLOGY_RUBRIC),
    ("oncology", GOLD_ONCOLOGY_CASES, "onc", ONCOLOGY_FAILURE_DOMAINS, ONCOLOGY_RUBRIC),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate against the taxonomy, do not write")
    args = ap.parse_args()

    for specialty, gold, prefix, fds, rubric in SPECS:
        corpus = build_corpus(specialty, gold, prefix, fds, rubric)
        bucket_ids = set(get_specialty_config(specialty).bucket_ids())
        bad = [it["seed_id"] for it in corpus["items"] if it["topic"] not in bucket_ids]
        if bad:
            raise SystemExit(f"{specialty}: items with off-taxonomy topic: {bad}")
        bad_arch = [a["topic"] for a in corpus["hard_case_archetypes"] if a["topic"] not in bucket_ids]
        if bad_arch:
            raise SystemExit(f"{specialty}: archetypes with off-taxonomy topic: {bad_arch}")
        out = SEED_DIR / f"{specialty}.v1.json"
        if args.check:
            print(f"[check] {specialty}: {len(corpus['items'])} items, "
                  f"{len(corpus['hard_case_archetypes'])} archetypes — OK")
            continue
        out.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[write] {out} — {len(corpus['items'])} items, "
              f"{len(corpus['hard_case_archetypes'])} archetypes")


if __name__ == "__main__":
    main()
