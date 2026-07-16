import os
from typing import Any

APP_AI_CONFIG_VERSION = "2026-05-31.1"

# temperature: None  -> do NOT send temperature (API default)
#              float -> send exact value
MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "generation": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 2000},
    "extraction": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 2500},
    "eligibility_extract": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 4000},
    "intraop_extract": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 4000},
    "intake_chat": {"model": "claude-sonnet-4-6", "temperature": 0.2, "max_tokens": 3000},
    "escalation_classifier": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 120},
    "care_companion_chat": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 350},
    "avatar_chat": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 150},
    "grounding_judge": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 1500},
    # Gold Standard — conversation capture (Data Training tab). Draft-note +
    # Safe-Harbor de-identification. Overridable via MODEL_GOLD_DRAFT_NOTE / MODEL_GOLD_DEID.
    "gold_draft_note": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 2000},
    "gold_deid": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 4000},
    # Asclepius — Expert Evaluation Portal (PRD §9). Overridable via
    # MODEL_ASCLEPIUS_CRITIC / MODEL_ASCLEPIUS_CANDIDATE_GEN.
    "asclepius_critic": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 1500},
    "asclepius_candidate_gen": {"model": "claude-sonnet-4-6", "temperature": 0.3, "max_tokens": 2000},
    "asclepius_grounding": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 1200},
    # Reasoning splitter (Eval Flow Upgrade §4): break the chosen answer into
    # ordered steps for tap-to-grade. Deterministic (temp 0.0) — a structural
    # split, not a judgment. Overridable via MODEL_ASCLEPIUS_REASONING_SPLIT.
    "asclepius_reasoning_split": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 1200},
    # Speed Optimization §2 — model-assisted pre-labeling (verify, don't author).
    # Suggestions only; never auto-applied. Overridable via MODEL_ASCLEPIUS_PRELABEL
    # / MODEL_ASCLEPIUS_REASONING_PREGRADE / MODEL_ASCLEPIUS_STT_CLEANUP.
    "asclepius_prelabel": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 1200},
    "asclepius_reasoning_pregrade": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 1500},
    # Dictation cleanup (Speed Optimization §4): mechanical transcript tidy.
    "asclepius_stt_cleanup": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 1500},
    # Asclepius Seedmaker auto-generation (nephrology PRD §11). "Current Claude
    # model" is expressed via the registry + env override, never hardcoded in
    # logic. Prompt synthesis + judging default to the strongest model
    # (claude-opus-4-8) for highest-quality, high-value prompts; override via
    # MODEL_ASCLEPIUS_PROMPT_GEN / _JUDGE (e.g. to claude-sonnet-4-6) if cost or
    # availability requires it. Candidate generation intentionally stays on the
    # current/non-max model so realistic, revisable errors are more likely (PRD §7.2).
    "asclepius_prompt_gen": {"model": "claude-opus-4-8", "temperature": 0.7, "max_tokens": 2000},
    "asclepius_prompt_judge": {"model": "claude-opus-4-8", "temperature": 0.0, "max_tokens": 800},
    # Synthetic Multimodal Cases PRD §3 — the V3 (seamless) structured-case pipeline.
    # ``case_gen`` AUTHORS a full PHI-free ClinicalCase (demographics + ≥2 lab panels
    # with trends + EHR notes + meds + ground truth) from a hard-case archetype: the
    # strongest model + generous tokens, since the case IS the product. The two gates
    # are deterministic scorers. Overridable via MODEL_ASCLEPIUS_CASE_GEN /
    # _CASE_JUDGE / _HARDNESS_JUDGE. WITHOUT these entries resolve() raises and every
    # multimodal case is dropped (mis-reported as "no LLM"), so V3 falls back to text.
    "asclepius_case_gen": {"model": "claude-opus-4-8", "temperature": 0.6, "max_tokens": 6000},
    "asclepius_case_judge": {"model": "claude-opus-4-8", "temperature": 0.0, "max_tokens": 1200},
    "asclepius_hardness_judge": {"model": "claude-opus-4-8", "temperature": 0.0, "max_tokens": 1000},
    # Citation retrieval ranking (BUG-3): score candidate library entries for
    # relevance to the answer's claims. Deterministic; small output. Overridable via
    # MODEL_ASCLEPIUS_CITE_RANK.
    "asclepius_cite_rank": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 800},
    # Frontier-model failure capture (FEAT-1): answer the rendered case COLD with a
    # configured frontier model, verbatim. The specific model is chosen per call
    # (model override) from ASCLEPIUS_BASELINE_MODELS; this registry entry only
    # supplies defaults (temperature/max_tokens) and the audit role.
    "asclepius_baseline": {"model": "claude-opus-4-8", "temperature": 0.2, "max_tokens": 2000},
}

_LEGACY_ENV = {"intraop_extract": "INTRAOP_EXTRACTOR_MODEL"}

# The current-best OpenAI reasoning model used as the OpenAI side of the two-frontier
# A/B pair. This is the single place an OpenAI id lives (mirrors the Anthropic-ids
# invariant). Tej overrides it live via env (ASCLEPIUS_BASELINE_MODELS / OPENAI_MODEL)
# with zero code change; the router keys off the id prefix.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")


class UnknownProvider(ValueError):
    """A model id whose provider cannot be determined (never crashes a run — the
    caller records it as an errored run and degrades gracefully)."""


def resolve_provider(model_id: str) -> str:
    """Map a model id to its provider. ``claude*`` / ``anthropic:*`` → anthropic (the
    existing path, untouched); ``gpt*`` / ``o1/o3/o4*`` / ``chatgpt*`` / ``openai:*`` →
    openai. Anything else raises :class:`UnknownProvider`."""
    m = (model_id or "").strip().lower()
    if m.startswith("anthropic:") or m.startswith("claude"):
        return "anthropic"
    if (m.startswith("openai:") or m.startswith("gpt") or m.startswith("chatgpt")
            or m.startswith("o1") or m.startswith("o3") or m.startswith("o4")):
        return "openai"
    raise UnknownProvider(f"cannot resolve provider for model id: {model_id!r}")


def api_model_id(model_id: str) -> str:
    """Strip an optional ``openai:`` / ``anthropic:`` routing prefix so the bare id is
    sent to the SDK (``openai:gpt-5`` → ``gpt-5``)."""
    m = (model_id or "").strip()
    for pfx in ("openai:", "anthropic:"):
        if m.lower().startswith(pfx):
            return m[len(pfx):]
    return m


def is_anthropic_configured() -> bool:
    return bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())


def is_openai_configured() -> bool:
    return bool((os.getenv("OPENAI_API_KEY") or "").strip())


def resolve(role: str) -> dict[str, Any]:
    cfg = dict(MODEL_REGISTRY[role])
    env_model = os.getenv(f"MODEL_{role.upper()}")
    if not env_model and role in _LEGACY_ENV:
        env_model = os.getenv(_LEGACY_ENV[role])
    if env_model:
        cfg["model"] = env_model
    return cfg
