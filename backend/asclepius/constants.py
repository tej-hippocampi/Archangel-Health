"""Versioned config + controlled vocabularies for Asclepius (PRD §6.4).

Kept in one place so the taxonomy version stamped onto every emitted record is
unambiguous and easy to bump (mirrors ``APP_AI_CONFIG_VERSION`` in
``ai/model_config.py``).
"""

from __future__ import annotations

import os

from ai.model_config import APP_AI_CONFIG_VERSION

# Bump when the error taxonomy or any controlled vocabulary below changes.
ASCLEPIUS_TAXONOMY_VERSION = "2026-06-26.3"

# Config version stamped on every record (mirrors the model-config version so a
# buyer can tie a record back to the exact pipeline that produced it — opt §1.4).
ASCLEPIUS_CONFIG_VERSION = APP_AI_CONFIG_VERSION

# Asclepius-local roles (NOT the clinical RBAC roles).
ROLES = ("evaluator", "admin", "qa_reviewer")

# Primary verdict on the A/B comparison.
VERDICTS = ("A_better", "B_better", "both_inadequate")

# Quick confidence buttons.
CONFIDENCE_LEVELS = ("low", "medium", "high")

# Where the task (prompt + candidate answers) originated.
TASK_SOURCES = ("lab_supplied", "internal_prompt_bank")

# Structured "why it's better" tags for the chosen answer (PRD §4.1).
WHY_BETTER_TAGS = (
    "more_accurate",
    "safer",
    "better_reasoning",
    "clearer",
    "better_dosing",
)

# Error taxonomy applied to the rejected answer (PRD §6.4). Each tag may carry
# an optional severity ("low" | "medium" | "high") on the submission.
ERROR_TAXONOMY = (
    "dosing_error",
    "unsafe_recommendation",
    "hallucination",
    "omission",
    "wrong_diagnosis",
    "outdated_guideline",
    "misreads_labs",
    "wrong_contraindication",
    "other",
)

ERROR_SEVERITIES = ("low", "medium", "high")

# Status lifecycle of a submission (PRD §5). ``needs_qa`` and ``rejected`` are
# the side branches off the happy path; everything else is the linear spine:
#   submitted -> auto_validated -> qa_checked -> export_ready -> exported
SUBMISSION_STATUSES = (
    "submitted",
    "auto_validated",
    "needs_qa",
    "qa_checked",
    "export_ready",
    "exported",
    "rejected",
)

# Packaged training-record types (PRD §6.3).
RECORD_TYPES = ("preference", "ideal_answer", "reasoning_trace")

# Difficulty hints (free-form is tolerated, these are the canonical buckets).
DIFFICULTIES = ("easy", "medium", "hard")


# ─── Data-optimization vocabularies (opt prompt) ──────────────────────────────

# Evidence anchor source types (opt §1.2). Every anchor declares where the
# citation came from so a buyer can filter on guideline-grounded evidence.
EVIDENCE_SOURCE_TYPES = ("guideline", "primary_literature", "expert_consensus", "other")

# PRM800K-style per-step labels (opt §1.1). Each reasoning step is independently
# labeled; optional numeric ``step_reward`` may accompany the label.
REASONING_STEP_LABELS = ("good", "neutral", "bad")

# Grounding Mode (opt §1.2). ``optional`` keeps the lightest path sacred;
# ``required`` is the premium SKU that gates Submit on a valid citation.
GROUNDING_MODES = ("optional", "required")
DEFAULT_GROUNDING_MODE = "optional"

# The "earn-more" disclaimer copy shown in ``required`` mode (opt §1.2). Kept in
# ONE place so it is trivial to tune. Surfaced near the verdict buttons only when
# the task is grounding_mode=required.
GROUNDED_PREMIUM_DISCLAIMER = (
    "⏱️💲 Premium grounded task. This task asks you to cite the clinical guideline "
    "or source behind your judgment. It takes a bit more time per task — but "
    "grounded, guideline-cited data sells at a premium, so you earn more per task. "
    "Your effort and citations are tracked and credited."
)

# hh-rlhf preference export variants (opt §1.1): "flat" = prompt/chosen/rejected
# strings; "chat" = messages arrays with roles. Selectable via the buyer profile.
PREFERENCE_VARIANTS = ("flat", "chat")

# Inter-annotator agreement threshold (opt §1.3). Tasks whose double-labeled
# verdicts disagree are flagged for re-review rather than silently exported.
KAPPA_THRESHOLD = 0.7

# Buyer-request lifecycle (opt §2.5).
BUYER_REQUEST_STATUSES = ("draft", "accepted", "in_progress", "delivered")

# Public medical benchmarks we contamination-check prompts against (opt §1.5).
CONTAMINATION_BENCHMARKS = ("MedQA", "MedMCQA", "PubMedQA", "MMLU-med")

# Characteristic signatures (normalized, lowercased substrings) that strongly
# indicate a prompt was lifted from a public benchmark. Substring/shingle match
# in ``validation.contamination_hits``. Kept conservative to avoid false hits;
# extend as new benchmark fingerprints surface.
CONTAMINATION_SIGNATURES = {
    # explicit benchmark self-references
    "medqa": "MedQA",
    "medmcqa": "MedMCQA",
    "pubmedqa": "PubMedQA",
    "mmlu": "MMLU-med",
    # canonical USMLE-style stems that recur verbatim across MedQA/MMLU-med
    "which of the following is the most likely diagnosis": "MedQA",
    "which of the following is the best next step in management": "MedQA",
    "which of the following is the most appropriate next step": "MedQA",
}


# Mode A internal seed prompt bank (opt §2.5) — our anchor nephrology private
# practice produces the first sellable dataset with ZERO buyer involvement.
# Specialty defaults to nephrology. Used by "new batch from internal bank".
INTERNAL_PROMPT_BANK = (
    "72yo on hemodialysis presents with K+ 6.4 and peaked T-waves. How do you adjust the dialysate and medications?",
    "55yo with CKD stage 4 (eGFR 22) and new metabolic acidosis (HCO3 17). What is the management?",
    "Patient on tacrolimus post kidney transplant with rising creatinine. Outline your differential and workup.",
    "Diabetic with nephrotic-range proteinuria (UPCR 4.2 g/g). Which agents reduce proteinuria and how do you titrate?",
    "ESRD patient on PD with cloudy effluent and abdominal pain. What is your empiric peritonitis management?",
    "Hyponatremia (Na 118) with seizures in a 68yo. How do you correct safely and avoid osmotic demyelination?",
)


def default_license() -> str:
    """The rights/license string stamped on every record (opt §1.4).

    Env-overridable so a buyer-specific license can be set per deployment.
    """
    return (os.getenv("ASCLEPIUS_LICENSE") or "CC-BY-NC-4.0-clinical-eval").strip()


def default_ip_cleared() -> bool:
    """Whether records assert IP clearance (opt §1.4). Default true; the data is
    synthetic / de-identified expert judgment cleared for sale."""
    return (os.getenv("ASCLEPIUS_IP_CLEARED", "1").strip().lower() in ("1", "true", "yes", "on"))


def double_label_pct() -> float:
    """Percentage of tasks to route to a second evaluator for IAA (opt §1.3).

    Surfaced as ``max_labels=2`` on newly created tasks when > 0; the actual
    routing is enforced by ``store.next_task_for_evaluator`` honoring max_labels.
    """
    try:
        return float(os.getenv("ASCLEPIUS_DOUBLE_LABEL_PCT", "0"))
    except ValueError:
        return 0.0


# ─── Seedmaker auto-generation (Mode A, nephrology v1 — PRD §7, §9, §11) ──────

# Engine name stamped into every generated task's provenance block (PRD §9.1).
ASCLEPIUS_ENGINE = "asclepius_seedmaker"

# Canonical reasons a generated candidate is dropped before becoming a task
# (PRD §7.3, §7.4). Surfaced as ``dropped: {reason: count}`` on the job + UI.
GENERATION_DROP_REASONS = (
    "contamination",        # prompt matched a public-benchmark fingerprint
    "duplicate",            # exact-hash duplicate of a seed or a prior generation
    "near_duplicate",       # fuzzy (token-set) near-duplicate of an existing prompt
    "below_min_difficulty",  # prompt easier than the bucket's min_difficulty floor
    "difficulty_mix_skew",  # would exceed its difficulty quota under difficulty_mix
    "off_specialty",        # judge: on_specialty == false
    "unsafe",               # judge: safety_ok == false
    "low_error_likelihood",  # judge: below ASCLEPIUS_GEN_MIN_ERROR_LIKELIHOOD
    "low_revision_value",   # judge: below ASCLEPIUS_GEN_MIN_REVISION_VALUE
    "candidate_gen_failed",  # could not produce two candidate answers
    "empty_prompt",         # model returned an empty/blank prompt
    "judge_failed",         # judge unavailable / unparseable for this item
)

# Near-duplicate threshold: token-set Jaccard >= this against any existing prompt
# (seed or prior generation) drops the new prompt as ``near_duplicate`` (PRD §7.4).
GENERATION_NEAR_DUP_JACCARD = 0.8


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def gen_min_error_likelihood() -> float:
    return _env_float("ASCLEPIUS_GEN_MIN_ERROR_LIKELIHOOD", 0.5)


def gen_min_revision_value() -> float:
    return _env_float("ASCLEPIUS_GEN_MIN_REVISION_VALUE", 0.5)


def gen_max_attempts_per_task() -> int:
    return max(1, _env_int("ASCLEPIUS_GEN_MAX_ATTEMPTS_PER_TASK", 4))


def gen_fewshot_k() -> int:
    return max(1, _env_int("ASCLEPIUS_GEN_FEWSHOT_K", 6))
