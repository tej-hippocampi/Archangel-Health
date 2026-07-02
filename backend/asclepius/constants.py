"""Versioned config + controlled vocabularies for Asclepius (PRD §6.4).

Kept in one place so the taxonomy version stamped onto every emitted record is
unambiguous and easy to bump (mirrors ``APP_AI_CONFIG_VERSION`` in
``ai/model_config.py``).
"""

from __future__ import annotations

import os

from ai.model_config import APP_AI_CONFIG_VERSION

# Bump when the error taxonomy or any controlled vocabulary below changes.
ASCLEPIUS_TAXONOMY_VERSION = "2026-06-30.1"

# Config version stamped on every record (mirrors the model-config version so a
# buyer can tie a record back to the exact pipeline that produced it — opt §1.4).
ASCLEPIUS_CONFIG_VERSION = APP_AI_CONFIG_VERSION

# Asclepius-local roles (NOT the clinical RBAC roles).
ROLES = ("evaluator", "admin", "qa_reviewer")

# Primary verdict on the A/B comparison.
VERDICTS = ("A_better", "B_better", "both_inadequate")

# Stage-1 prompt-validation gate (Eval Flow Upgrade §2). The clinician signs off
# on the prompt before any answer is revealed: ``valid`` upgrades provenance and
# continues capture; ``flagged`` skips the task to admin review (0 records).
PROMPT_REVIEW_VERDICTS = ("valid", "flagged")

# Task-side status for a prompt a clinician flagged as invalid. Excluded from the
# evaluator queue (not ``open``) and surfaced in the admin Tasks list for triage.
PROMPT_FLAGGED_TASK_STATUS = "prompt_flagged"

# Quick confidence buttons.
CONFIDENCE_LEVELS = ("low", "medium", "high")

# Stage-2 independent-capture mode (Speed Optimization, Feature 1). ``stance``
# (default) asks for a 30–45s quick take before reveal — the anti-anchoring
# guard — while the gold SFT answer comes from the specialist-refined chosen
# answer. ``full`` keeps the original long-form blind ideal answer (premium /
# eval batches). Set per task via ``independent_mode``.
INDEPENDENT_MODES = ("stance", "full")
DEFAULT_INDEPENDENT_MODE = "stance"


def normalize_independent_mode(value):
    """Coerce any input to a known independent mode (single source of truth —
    store, packaging, and router all normalize through here)."""
    return value if value in INDEPENDENT_MODES else DEFAULT_INDEPENDENT_MODE


# Evaluator portal versions (Asclepius V2 launch). Contributors choose per
# session: ``v1`` is the classic flow (full blind ideal answer, no model
# assist, no diff view); ``v2`` is the speed-optimized flow (quick stance,
# pre-labeling, diff, dictation, structured reasons). Stage-1 prompt review
# and the packaged record types are identical in both. The version is stamped
# onto every submission + record so buyers/admin can segment by provenance.
PORTAL_VERSIONS = ("v1", "v2")
DEFAULT_PORTAL_VERSION = "v2"


def normalize_portal_version(value):
    return value if value in PORTAL_VERSIONS else DEFAULT_PORTAL_VERSION

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

# Structured-first capture (Speed Optimization, Feature 6): a controlled
# vocabulary of one-tap REASONS attached per selected error tag, so the
# diagnostic "why" is captured without typing. Persisted as
# ``RejectedCritique.error_tag_reasons: {tag: reason}``; free text stays an
# optional nuance field.
ERROR_TAG_REASONS = (
    "dose_too_high",
    "dose_too_low",
    "contraindicated",
    "outdated_threshold",
    "misreads_labs",
    "wrong_order",
    "unsafe",
    "incomplete",
    "not_indicated",
)

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
    # Stage-1 flag: prompt judged clinically invalid; captured for audit but never
    # packaged (Eval Flow Upgrade §2). Terminal side-branch off the happy path.
    "prompt_flagged",
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
# labeled; optional numeric ``step_reward`` may accompany the label. Under the
# Edit-to-Correct flow these are DERIVED from the confirm/correct action (see
# ``label_for_correction_reason``) rather than hand-tapped, but the values stay
# good|neutral|bad for buyer compatibility.
REASONING_STEP_LABELS = ("good", "neutral", "bad")

# Edit-to-Correct (Reasoning Capture v2). When the doctor edits a split step to
# correct it, they pick exactly one of these reasons. The reason is auto-mapped
# to a buyer-facing label: ``minor_wording`` is a non-substantive edit (neutral);
# every other reason means the AI's original step was wrong (bad).
STEP_CORRECTION_REASONS = (
    "factual_error",
    "outdated_guideline",
    "incomplete",
    "unsafe",
    "wrong_order",
    "minor_wording",
)


def label_for_correction_reason(reason):
    """minor_wording is a non-substantive edit (neutral); any other reason means
    the original step was wrong (bad). Used to derive the buyer-facing label."""
    return "neutral" if reason == "minor_wording" else "bad"

# ─── Model-assisted pre-labeling (Speed Optimization, Feature 2) ─────────────
# Suggestions below this confidence are HIDDEN server-side — we never nudge the
# specialist on an uncertain call (quality guardrail; spec Feature 2).
def assist_min_confidence() -> float:
    return _env_float("ASCLEPIUS_ASSIST_MIN_CONF", 0.6)


# Time-floor guard for assisted tasks: confirming pre-labeled suggestions
# implausibly fast routes the submission to needs_qa (rubber-stamp catch —
# never a hard reject). Stricter than the base too-fast floor because the
# doctor is expected to actually verify each flagged step.
def assist_time_floor_sec() -> int:
    return max(0, _env_int("ASCLEPIUS_ASSIST_TIME_FLOOR_SEC", 60))


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


# ─── Contributors view + tiered export (credential tiering) ──────────────────
# The governing rule: records sent to buyers ("Export Data") carry credential
# ATTRIBUTES only; anything that identifies or locates the physician lives in a
# private vault and is released only via "Further Credential Summary" under NDA.
# Enforced at export with a hard validation gate (see export.assert_no_tier_b_leak).

# Roles a contributor can hold within an organization (display only; free-text
# tolerated). Drives the "specific doctor, the specific NP, etc." breakdown.
CONTRIBUTOR_ROLE_TITLES = (
    "Physician (MD)",
    "Physician (DO)",
    "Physician (MBBS)",
    "Nurse Practitioner",
    "Physician Assistant",
    "Registered Nurse",
    "Pharmacist",
    "Other",
)

# Practice-setting categories — the ONLY practice descriptor that ships. Never a
# named institution (that is Tier B).
PRACTICE_SETTING_TYPES = (
    "academic",
    "private_practice",
    "hospital",
    "dialysis_unit",
    "other",
)

# Tier A — SHIP. Credential ATTRIBUTES included in buyer-facing "Export Data"
# records. No value here can identify or locate the physician.
TIER_A_SHIP_FIELDS = (
    "hashed_annotator_id",
    "degree",                    # MD / DO / MBBS
    "board_certifications",      # board + specialty + subspecialty + active status
    "primary_specialty",
    "subspecialties",            # array (dialysis, transplant, CKD…)
    "years_in_active_practice",
    "active_practice",           # boolean
    "practice_setting_type",     # category only — never a named institution
    "languages",                 # array
    "fellowship_trained",        # boolean
    "fellowship_summary",        # generalized string (no named institution)
    "credentials_verified",      # boolean ✓
)

# Tier B — VERIFY ONLY. The private vault. NEVER appears in a shipped record;
# released only inside a "Further Credential Summary" dossier under NDA. The
# field NAMES here are the forbidden set the export leak-gate scans for.
TIER_B_VERIFY_FIELDS = (
    "full_legal_name",
    "npi",
    "medical_license_number",
    "license_state",
    "medical_school",            # institution
    "medical_school_year",
    "residency",                 # institution
    "residency_year",
    "fellowship",                # institution (exact)
    "fellowship_year",
    "practice_name",
    "practice_address",
    "practice_contact",
)

# Extra identifying-token aliases the leak-gate also rejects (EXACT key match, so
# they can never collide with legitimate shipped fields like `license` — the
# rights string — or `annotator_credential`). A mis-mapped buyer profile cannot
# smuggle PII under one of these key names.
TIER_B_FORBIDDEN_ALIASES = (
    "legal_name",
    "physician_name",
    # Onboarding (Steps 3–8) writes these identifying fields onto the users table;
    # they must never ride a shipped record (full_name identifies; org_name is the
    # named practice/org — a locator, like practice_name).
    "full_name",
    "org_name",
    "license_number",
    "dob",
    "date_of_birth",
    "home_address",
    "practice_phone",
    "practice_email",
    "ssn",
)

# The complete forbidden-key set scanned (exact, case-insensitive) on every
# exported record line.
TIER_B_FORBIDDEN_KEYS = tuple(sorted(set(TIER_B_VERIFY_FIELDS) | set(TIER_B_FORBIDDEN_ALIASES)))


def company_name() -> str:
    """The legal entity named in the credential-verification notice. Env-
    overridable so the same notice text works across deployments."""
    return (os.getenv("ASCLEPIUS_COMPANY_NAME") or "Archangel Health").strip()


# Header watermark stamped on every Further Credential Summary page (spec §6).
CREDENTIAL_SUMMARY_WATERMARK = (
    "CONFIDENTIAL — credential verification, provided under NDA / non-circumvention."
)


def non_circumvention_notice() -> str:
    """The §9 Non-Circumvention & Confidentiality Notice, auto-prepended to every
    Further Credential Summary and surfaced as a click-through acknowledgment
    before generation. ``[Company]`` is substituted with ``company_name()``."""
    co = company_name()
    return f"""CONFIDENTIAL — CREDENTIAL VERIFICATION

This Credential Verification Summary (the "Summary") is provided by {co} solely to enable the recipient ("Recipient") to verify the qualifications of the credentialed contributor(s) associated with data licensed from or evaluated through {co}. By accessing this Summary, Recipient agrees to the following:

1. Confidential use. The Summary and all information it contains (including names, NPI, license numbers, education history, and other credentials) are confidential and are provided solely for credential verification. Recipient will not copy, store beyond the verification period, distribute, or use the information for any other purpose.

2. Non-circumvention / non-solicitation. For a period of twenty-four (24) months from receipt, Recipient and its affiliates shall not, directly or indirectly, contact, solicit, recruit, engage, contract with, employ, or attempt to source services from any contributor identified in this Summary, nor otherwise circumvent {co} to obtain such contributor's services, without {co}'s prior written consent.

3. Services provided through {co}. Recipient acknowledges that all expert evaluation and data-labeling services are provided through {co}, and that the contributor relationship is engaged exclusively through {co} for the purposes contemplated herein.

4. Remedies. Recipient acknowledges that a breach of this Notice would cause irreparable harm for which monetary damages may be inadequate, and that {co} shall be entitled to seek injunctive relief, in addition to any other available remedies, together with reasonable attorneys' fees.

5. Term & survival. These obligations survive the completion of verification and any related transaction and remain in effect for the period stated above.

This Notice supplements, and does not replace, any Master Services Agreement, NDA, or Data License Agreement between the parties. Where a signed agreement exists, the more protective terms control."""


# Surfaced in-app and on the dossier: this template is not legal advice.
CREDENTIAL_SUMMARY_LEGAL_DISCLAIMER = (
    "The Non-Circumvention & Confidentiality Notice is a template for discussion, "
    "not legal advice. Have a qualified attorney review and adapt it (governing law, "
    "term length, definitions, enforceability by jurisdiction) before relying on it. "
    "Ideally the same protections also appear in a signed NDA + Master Services "
    "Agreement with each buyer and in your contributor agreement."
)
