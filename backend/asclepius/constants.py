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
# ``data_partner`` (EHR PRD §4): a partner account that can do exactly one thing
# — upload a de-identified bundle through its own tokenized link. No queue
# access, no exports, no data reads.
ROLES = ("evaluator", "admin", "qa_reviewer", "data_partner")

# Primary verdict on the A/B comparison.
VERDICTS = ("A_better", "B_better", "both_inadequate")

# Stage-1 prompt-validation gate (Eval Flow Upgrade §2). The clinician signs off
# on the prompt before any answer is revealed: ``valid`` upgrades provenance and
# continues capture; ``flagged`` skips the task to admin review (0 records);
# ``not_hard`` (Seamless PRD WS2) means the prompt is clinically valid but NOT
# genuinely hard — it is routed out of the hard-case queue and fed back to
# recalibrate the hardness judge/corpus (human-in-the-loop hardness curation).
# ``case_incoherent`` (Multimodal PRD §5) is the human counterpart to the
# case-judge coherence gate: a MULTIMODAL case whose labs / notes / problem list /
# meds are internally inconsistent (e.g. a value contradicts the narrative). It is
# routed out of the queue and fed back to recalibrate case generation.
PROMPT_REVIEW_VERDICTS = ("valid", "flagged", "not_hard", "case_incoherent")

# Task-side status for a prompt a clinician flagged as invalid. Excluded from the
# evaluator queue (not ``open``) and surfaced in the admin Tasks list for triage.
PROMPT_FLAGGED_TASK_STATUS = "prompt_flagged"

# Task-side status for a prompt flagged "not actually hard" (WS2). Also excluded
# from the queue; distinct from prompt_flagged so hardness-curation feedback is
# separable from clinical-validity triage.
NOT_HARD_TASK_STATUS = "not_hard"

# Task-side status for a multimodal case a clinician flagged as internally
# inconsistent (Multimodal PRD §5). Excluded from the queue; distinct from
# not_hard/prompt_flagged so case-generation feedback is separable.
CASE_INCOHERENT_TASK_STATUS = "case_incoherent"

# Quick confidence buttons.
CONFIDENCE_LEVELS = ("low", "medium", "high")

# Stage-2 independent-capture mode. Three kinds, cheapest→richest:
#   ``instinct`` (V3 default, Seamless PRD WS1) — a ~10s single-line "gut check"
#     (the crux of the right answer) before reveal. The lightest anti-anchoring
#     guard; ships as a context field, NOT a gold answer.
#   ``stance`` (V2 default, Speed Optimization F1) — a 30–45s quick take.
#   ``full`` — the long-form blind ideal answer (premium / eval batches); the
#     only kind that packages an additional premium blind-gold SFT record.
# Set per task via ``independent_mode``; the CAPTURE kind actually stamped is
# resolved by ``independent_capture_kind`` from the contributor's portal version.
INDEPENDENT_MODES = ("instinct", "stance", "full")
DEFAULT_INDEPENDENT_MODE = "stance"

# The pre-reveal capture kinds that are LIGHTWEIGHT anchoring signals (ride the
# primary record as a context field) rather than a gold blind ideal answer.
LIGHTWEIGHT_INDEPENDENT_KINDS = ("instinct", "stance")


def normalize_independent_mode(value):
    """Coerce any input to a known independent mode (single source of truth —
    store, packaging, and router all normalize through here)."""
    return value if value in INDEPENDENT_MODES else DEFAULT_INDEPENDENT_MODE


def independent_capture_kind(portal_version, independent_mode):
    """The Stage-2 capture kind actually stamped, by portal version (one source
    of truth for the reveal endpoint AND packaging):

      * V1 (classic)  → always ``full`` (the classic flow writes the full blind
        ideal answer regardless of the task's mode).
      * V3 (seamless) → ``full`` only when the admin explicitly marked the task
        ``full`` (premium/eval batch); otherwise the ~10s ``instinct`` one-liner.
      * V2 (assisted) → the task's ``independent_mode`` (``stance`` by default).

    A client-supplied kind can never upgrade a lightweight capture into a premium
    blind-gold record — the portal version + task mode are authoritative."""
    pv = normalize_portal_version(portal_version)
    if pv == "v1":
        return "full"
    if pv in ("v3", "v4"):
        # V4 (real cases) behaves EXACTLY like V3 — the flow is identical, only
        # the data differs (EHR PRD §9.5).
        return "full" if independent_mode == "full" else "instinct"
    return normalize_independent_mode(independent_mode)


# Evaluator portal versions. Contributors choose per session:
#   ``v1`` classic   — full blind ideal answer, no model assist, no diff view.
#   ``v2`` assisted  — quick stance, pre-labeling, diff, dictation, structured
#                      reasons (Speed Optimization + Value-per-Minute).
#   ``v3`` seamless  — the newest flow (Seamless + Hard-Cases PRD): a ~10s
#                      instinct one-liner, AI suggestions hidden until the verdict
#                      is committed, one-click citations, a larger edit surface,
#                      brighter A/B diff, and a hard-case-only queue. Inherits
#                      every V2 assisted capability.
#   ``v4`` real cases — the V3 seamless flow over REAL, de-identified patient
#                      cases (Real EHR Ingestion PRD §9.5). Identical UX to V3;
#                      only the DATA differs (case_source="real_deid"). Served
#                      exclusively to contributors flagged ``real_data_approved``.
# Stage-1 prompt review and the packaged record TYPES are identical across all
# versions. The version is stamped onto every submission + record so buyers/admin
# can segment by provenance. V3 is the recommended default for new sessions.
PORTAL_VERSIONS = ("v1", "v2", "v3", "v4")
DEFAULT_PORTAL_VERSION = "v3"

# Portal versions that get the ASSISTED capabilities (model pre-labeling, diff,
# dictation, value-aware routing). V1 (classic) is deliberately excluded.
ASSISTED_PORTAL_VERSIONS = ("v2", "v3", "v4")

# The V4 wall (EHR PRD §9.5): a real (case_source="real_deid") task is a V4 task
# and ONLY a V4 task; a synthetic task can never be V4. Enforced server-side in
# queue routing, submission derivation, and packaging — never trusted from the UI.
REAL_CASE_PORTAL_VERSION = "v4"
SYNTHETIC_PORTAL_VERSIONS = ("v1", "v2", "v3")


def normalize_portal_version(value):
    return value if value in PORTAL_VERSIONS else DEFAULT_PORTAL_VERSION

# Where the task (prompt + candidate answers) originated. ``partner_ehr`` (EHR
# PRD): a real, de-identified case ingested from a data partner's secure upload.
TASK_SOURCES = ("lab_supplied", "internal_prompt_bank", "partner_ehr")

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
    # Stage-1 "not actually hard" flag (Seamless PRD WS2): valid but not hard;
    # captured for hardness-judge recalibration, never packaged.
    "not_hard",
    # Stage-1 "case internally inconsistent" flag (Multimodal PRD §5): captured for
    # case-generation recalibration, never packaged.
    "case_incoherent",
)

# Packaged training-record types (PRD §6.3). ``rubric`` (FEAT-2) is a standalone,
# sellable, HealthBench-shaped scoring function derived from the doctor's tags.
RECORD_TYPES = ("preference", "ideal_answer", "reasoning_trace", "rubric")

# Rubric criterion axes (FEAT-2). Every criterion is scored on exactly one axis so
# a buyer can weight/filter by dimension (a reward model, an RL run, and a
# benchmark all consume these).
RUBRIC_AXES = ("accuracy", "completeness", "safety", "reasoning", "grounding", "communication")

# Difficulty hints (free-form is tolerated, these are the canonical buckets).
DIFFICULTIES = ("easy", "medium", "hard")


# ─── Data-optimization vocabularies (opt prompt) ──────────────────────────────

# Evidence anchor source types (opt §1.2). Every anchor declares where the
# citation came from so a buyer can filter on guideline-grounded evidence.
EVIDENCE_SOURCE_TYPES = ("guideline", "primary_literature", "expert_consensus", "fda_label", "other")

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
    "below_hardness_floor",  # hardness judge: score < HARDNESS_MIN (Seamless PRD WS2)
    "candidate_gen_failed",  # could not produce two candidate answers
    "empty_prompt",         # model returned an empty/blank prompt
    "judge_failed",         # judge unavailable / unparseable for this item
    # ── Multimodal case gates (Synthetic Multimodal Cases PRD §3.2, Stage 3c) ──
    "case_incoherent",         # labs/note/problem-list/meds internally inconsistent
    "ground_truth_indeterminate",  # no objectively correct, guideline/lab-anchorable answer
    "multimodal_not_necessary",    # answer derivable from the stem alone (decorative labs)
    "low_reasoning_divergence",    # no sound-vs-shortcut path (right-answer-wrong-reason)
    "case_gen_failed",         # case generation unavailable / unparseable / PHI-flagged
    # ── Multimodal non-skippable gates + content assertion (BUG-1 §2, §4) ──
    "insufficient_case_content",   # case lacks the mandatory labs/note/problem/med content
    "case_judge_unavailable",      # multimodal: case judge degraded to skipped → drop (never pass ungated)
    "hardness_unavailable",        # multimodal: hardness judge degraded to skipped → drop (never pass ungated)
)

# ─── Hard-Case Engine (Seamless PRD WS2) ──────────────────────────────────────
# Every generated candidate is scored 0–1 on a hardness rubric; below the floor
# it is dropped (``below_hardness_floor``). A passing candidate is auto-set to
# ``difficulty=hard`` and stamped with its hardness score + axes so exports can
# filter/prove hardness. The hard-case queue (V3) serves ONLY difficulty=hard.
HARDNESS_AXES = (
    "multi_step",         # requires multi-step reasoning, not single-fact recall
    "competing_risks",    # a genuine trade-off (e.g. decongestion vs. rising Cr)
    "diagnostic_trap",    # the "obvious" answer is wrong or incomplete
    "guideline_nuance",   # rewards guideline nuance
    "recent_change",      # rewards a recent guideline/dosing change (AI cutoff lag)
    "high_stakes",        # safety-relevant / high clinical stakes
    "model_failure_domain",  # sits in a known model-weak area for the specialty
)


def hardness_min() -> float:
    """Minimum hardness score (0–1) a generated candidate must reach to be served
    as a hard case. Env-overridable so the floor can be tuned to judge behavior.

    Raised to 0.75 (BUG-1): the multimodal product's whole value is that the cases
    are genuinely hard — a 0.7 floor let borderline cases through."""
    return _env_float("ASCLEPIUS_HARDNESS_MIN", 0.75)


def v3_multimodal_only() -> bool:
    """Whether the V3 (seamless) queue PREFERS multimodal cases — i.e. the default
    V3 experience is a structured clinical case (demographics + lab panels with
    trends + EHR notes + meds) whenever one is available, served ahead of any bare
    text prompt. Default ON.

    This is a PREFERENCE, not a hard filter: structured cases require synthetic
    multimodal generation (an LLM key — the case-gen model synthesizes the
    labs/notes), so if none have been generated yet the V3 queue falls back to the
    normal hard queue rather than showing the clinician an empty "queue cleared"
    screen. Set ASCLEPIUS_V3_MULTIMODAL_ONLY=0 to disable the preference entirely
    (plain hard/text V3 queue)."""
    return (os.getenv("ASCLEPIUS_V3_MULTIMODAL_ONLY", "1").strip().lower()
            in ("1", "true", "yes", "on"))


def hard_only_generation() -> bool:
    """Whether the generator gates on hardness (drops below the floor + forces
    difficulty=hard). Default ON — the engine's purpose is hard cases — but the
    hardness judge degrades to skipped with no LLM key, so offline generation is
    unaffected. Set ASCLEPIUS_HARD_ONLY=0 to disable the gate entirely."""
    return (os.getenv("ASCLEPIUS_HARD_ONLY", "1").strip().lower() in ("1", "true", "yes", "on"))


# ─── Multimodal case judge floors (Synthetic Multimodal Cases PRD §3.2) ────────
# Stage 3c scores case-specific dimensions ONLY — hardness is REUSED from Stage 3b
# (run_hardness_judge). Degrades safely: a skipped case judge never drops.
def case_coherence_min() -> float:
    """Labs/note/problem-list/meds must be internally consistent (no impossible panel)."""
    return _env_float("ASCLEPIUS_CASE_COHERENCE_MIN", 0.8)


def case_mm_necessity_min() -> float:
    """The answer must REQUIRE integrating ≥1 lab panel and/or the note — not be
    derivable from the question stem alone (the anti-"decorative labs" gate).

    Raised to 0.8 (BUG-1): decorative labs are the #1 way a "multimodal" case is
    really a text case wearing a lab table — hold the necessity bar high."""
    return _env_float("ASCLEPIUS_CASE_MM_NECESSITY_MIN", 0.8)


# An objectively correct, guideline/lab-anchorable answer must exist; and the case
# must admit a sound path AND a plausible shortcut/unsound path to it (the
# right-answer-wrong-reason surface). Gated at fixed, sensible floors (the PRD
# names only the two env floors above); env-overridable for tuning.
def case_ground_truth_min() -> float:
    return _env_float("ASCLEPIUS_CASE_GROUND_TRUTH_MIN", 0.7)


def case_divergence_min() -> float:
    return _env_float("ASCLEPIUS_CASE_DIVERGENCE_MIN", 0.5)

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


# ─── Value model (Value-per-Minute PRD, Part A) ───────────────────────────────
# north-star = value ÷ time = dollars of sellable data value produced per minute
# of clinician time. Every coefficient is env-overridable so the model can be
# recalibrated to realized sales WITHOUT a code change. The defaults are honest
# MARGINAL dollars (§A2): the four formats derive from ONE correlated judgment,
# so a standard bundle (preference + ideal + reasoning, 0 corrections) totals
# $70 — matching today's per-record figure — NOT 4× a standalone record.
#
# ``value_tier`` is an optional ADMIN hint on a task ("premium" / "standard" /
# "on_policy" …) surfaced to value-aware routing; it never gates capture and is
# free-text-tolerant (routing scores from the estimated value, not the label).
VALUE_TIERS = ("standard", "premium", "on_policy", "eval")


def value_preference_base() -> float:
    """Reward-model anchor (the hh-rlhf pair). The base every completed judgment
    carries — even a ``both_inadequate`` verdict is a preference-grade signal."""
    return _env_float("ASCLEPIUS_VALUE_PREFERENCE_BASE", 45.0)


def value_ideal_answer_marginal() -> float:
    """SFT (refined chosen) — overlaps the pair, so priced as a marginal add-on."""
    return _env_float("ASCLEPIUS_VALUE_IDEAL_ANSWER_MARGINAL", 12.0)


def value_reasoning_trace_marginal() -> float:
    """PRM step-level trace — a distinct training use from the preference pair."""
    return _env_float("ASCLEPIUS_VALUE_REASONING_TRACE_MARGINAL", 13.0)


def baseline_models() -> list:
    """The frontier models to answer a case COLD for failure capture (FEAT-1).
    Comma-separated ``ASCLEPIUS_BASELINE_MODELS`` (frontier model ids). These route
    through the shared ``ai.llm_client``; a model id the backend can't reach is
    recorded as an errored run, never a crash.

    The DEFAULT is resolved from the ``asclepius_baseline`` role in
    ``ai/model_config.py`` — the single source of truth for model ids — so no
    model literal ever lives in this file (repo invariant: model ids only in
    model_config)."""
    raw = os.getenv("ASCLEPIUS_BASELINE_MODELS")
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    from ai.model_config import resolve
    return [resolve("asclepius_baseline")["model"]]


def value_rubric_marginal() -> float:
    """A confirmed rubric (FEAT-2) is a reusable SCORING FUNCTION (a grader), not a
    single label — priced above a label. Marginal add-on over the judgment it was
    seeded from."""
    return _env_float("ASCLEPIUS_VALUE_RUBRIC_MARGINAL", 25.0)


def value_step_pair_each() -> float:
    """Each corrected step = one step-level preference pair (rejected→chosen)."""
    return _env_float("ASCLEPIUS_VALUE_STEP_PAIR_EACH", 6.0)


def value_step_pair_max() -> int:
    """Cap on counted step-pairs per judgment — no runaway stacking."""
    return max(0, _env_int("ASCLEPIUS_VALUE_STEP_PAIR_MAX", 4))


def value_grounded_mult() -> float:
    """≥1 valid evidence anchor → premium grounded SKU."""
    return _env_float("ASCLEPIUS_VALUE_GROUNDED_MULT", 1.30)


def value_difficulty_mult() -> Dict[str, float]:
    """Harder cases are worth more (N+1 signal). Env override is a JSON-ish
    ``easy:0.75,medium:1.0,hard:1.4`` string; falls back to the defaults."""
    raw = os.getenv("ASCLEPIUS_VALUE_DIFFICULTY_MULT")
    base = {"easy": 0.75, "medium": 1.00, "hard": 1.40}
    if raw:
        for pair in raw.split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                try:
                    base[k.strip()] = float(v)
                except ValueError:
                    pass
    return base


def value_on_policy_mult() -> float:
    """Mode B — grading the buyer's OWN model outputs (``source=lab_supplied``).
    On-policy data commands the highest price."""
    return _env_float("ASCLEPIUS_VALUE_ON_POLICY_MULT", 1.50)


def value_full_independent_mult() -> float:
    """``independent_mode == 'full'`` — an uncontaminated blind gold answer."""
    return _env_float("ASCLEPIUS_VALUE_FULL_INDEPENDENT_MULT", 1.20)


def value_credentialed_kappa_mult() -> float:
    """Double-labeled + credentialed → a reportable κ (eval-grade)."""
    return _env_float("ASCLEPIUS_VALUE_CREDENTIALED_KAPPA_MULT", 1.15)


def value_tier_mult_cap() -> float:
    """Hard ceiling on the stacked tier multiplier — no fantasy stacking."""
    return _env_float("ASCLEPIUS_VALUE_TIER_MULT_CAP", 2.50)


def value_reuse_mult() -> float:
    """Non-exclusive + benchmark repackaging. PROJECTED, not banked — the team is
    held to REALIZED V/T; this only feeds the projected forecast column."""
    return _env_float("ASCLEPIUS_VALUE_REUSE_MULT", 1.50)


def value_multimodal_mult() -> float:
    """Structured multimodal case (labs/note integration) — Synthetic Multimodal
    Cases PRD §9. Folded into the tier multiplier under TIER_MULT_CAP.

    HONESTY GUARDRAIL (must be in the datasheet): synthetic multimodal is the
    ARCHITECTURE PROOF, not the ~2× tier. The ~2× premium applies to REAL,
    context-preserved multimodal (case_source == 'real_deid'); a synthetic case is
    marked 'synthetic' and priced with this modest multiplier — never let a
    datasheet imply synthetic multimodal is real-patient data."""
    return _env_float("ASCLEPIUS_VALUE_MULTIMODAL_MULT", 1.35)


def value_real_case_mult() -> float:
    """REAL de-identified case premium (EHR PRD §9.5) — the 2–3× tier. Keys off
    ``case_source == 'real_deid'`` (the ground truth), never the version label,
    so a mislabeled session cannot game it. Applied ON TOP of the multimodal
    factor (a real case is also multimodal), still under TIER_MULT_CAP."""
    return _env_float("ASCLEPIUS_VALUE_REAL_CASE_MULT", 2.0)


def value_per_minute_target() -> float:
    """The north-star floor: realized value-per-clinician-minute the team is held
    to on v2 ``capture_reasoning`` tasks (PRD acceptance criteria)."""
    return _env_float("ASCLEPIUS_VALUE_PER_MINUTE_TARGET", 10.0)


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

# The bucket a contributor with no resolvable organization lands in (BUG-6). A
# record that exists but appears in NO org grouping is the worst admin failure
# mode; every ungrouped contributor is collected here so nothing is ever
# invisible. Kept as one constant so exports + metrics + directory agree.
UNASSIGNED_ORG = "(unassigned)"

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

# Multimodal case answer-key fields (Synthetic Multimodal Cases PRD §6, §8). These
# are internal generation/QA metadata that must NEVER ship on a normal record —
# packaging strips them via ``cases.public_case``, and adding them here makes the
# export leak-gate reject the whole batch loudly if one ever reaches a record
# (defense-in-depth over the case block, which the recursive scan already visits).
CASE_ANSWER_KEY_FIELDS = ("ground_truth", "hard_hook", "reasoning_divergence")

# The complete forbidden-key set scanned (exact, case-insensitive) on every
# exported record line.
TIER_B_FORBIDDEN_KEYS = tuple(sorted(
    set(TIER_B_VERIFY_FIELDS) | set(TIER_B_FORBIDDEN_ALIASES) | set(CASE_ANSWER_KEY_FIELDS)
))


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
