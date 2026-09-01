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
    # Community v2 — #medical-ai-news digest curation (select/score + compose).
    # Cheap, high-volume, low-stakes summarization: the small model tier is the
    # point (pennies per run). Temperature is passed explicitly by the caller;
    # max_tokens here covers the select pass, the compose pass overrides it via
    # COMMUNITY_DIGEST_MAX_TOKENS. Overridable via MODEL_COMMUNITY_DIGEST.
    "community_digest": {"model": "claude-haiku-4-5-20251001", "temperature": None, "max_tokens": 2000},
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


# Model families that can accept an image input (V4 Image Embedding PRD §5.1). The
# current frontier ids (gpt-5, claude-opus/sonnet/haiku 4.x, o-series) are all
# vision-capable; a legacy/text-only id must degrade the case to needs_baseline
# rather than silently grade text-only. Override the allow/deny via
# MODEL_VISION_ALLOW / MODEL_VISION_DENY (comma-separated id prefixes).
_VISION_CAPABLE_PREFIXES = (
    "gpt-5", "gpt-4o", "gpt-4.1", "chatgpt", "o1", "o3", "o4",
    "claude-opus", "claude-sonnet", "claude-haiku", "claude-3", "claude-4",
    "claude-fable", "anthropic:claude", "openai:gpt", "openai:o",
)
# Known text-only ids that must NOT be used for a vision A/B.
_VISION_INCAPABLE_PREFIXES = ("gpt-3.5", "claude-instant", "claude-2", "claude-1")

# ─── Models that accept only DEFAULT sampling ────────────────────────────────
# Some frontier models reject a pinned sampling parameter outright: sending
# ``temperature`` (at any value other than the default) or ``top_p`` returns
#
#     400 invalid_request_error: `temperature` is deprecated for this model.
#
# Measured against the live API, not assumed. Rejecting: ``claude-opus-4-7``,
# ``claude-opus-4-8`` and the whole Claude 5 family (``claude-opus-5``,
# ``claude-sonnet-5``, ``claude-fable-5``). Accepting: ``claude-opus-4-6`` and
# older, plus ``claude-sonnet-4-6`` and ``claude-haiku-4-5``.
#
# So this is NOT a family or prefix rule, and the boundary cuts THROUGH the opus
# 4 line: 4-6 accepts, 4-7 does not. A prefix of ``claude-opus-4`` would wrongly
# strip sampling from 4-1, 4-5 and 4-6, silently loosening two judges that are
# pinned to 0.0 on purpose. Exact ids only.
#
# WHY THIS MATTERS MORE THAN IT LOOKS: six registry roles pin a temperature on
# an Opus id — prompt synthesis, the prompt judge, case generation, the CASE
# JUDGE, the hardness judge, and the frontier baseline. The case judge fails
# CLOSED, so with it 400ing, real-case generation stops entirely and reports
# "Case judge unavailable"; the baseline 400ing makes every empirical difficulty
# come back ``measured=False``. One rejected parameter takes down the whole
# premium generation path, and the symptom names neither the parameter nor the
# model.
#
# ``MODEL_FIXED_SAMPLING`` (comma-separated ids) extends this at runtime, so a
# newly-shipped model can be handled without a deploy. ``llm_client`` also
# retries once without the offending parameter when the API says it is
# deprecated, which is the backstop for a model nobody has listed yet.
_FIXED_SAMPLING_MODELS = (
    "claude-opus-4-7", "claude-opus-4-8",
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5",
)

#: Request parameters that a fixed-sampling model refuses.
SAMPLING_PARAMS = ("temperature", "top_p", "top_k")

# ─── Models that may spend output budget on THINKING ─────────────────────────
# The Claude 5 family can emit a ``thinking`` content block before its answer,
# and those tokens come out of the SAME ``max_tokens`` budget as the visible
# text. This is the Anthropic mirror of the problem ``_openai_output_cap``
# already documents for o1/o3/gpt-5, and it fails the same silent way: a role
# with a tight budget (candidate generation is 2000) returns
# ``stop_reason="max_tokens"`` with the JSON truncated mid-sentence, the parse
# yields nothing, and the caller reports "no LLM key configured?" — blaming
# credentials for what is a token budget.
#
# Thinking is ADAPTIVE, not a fixed property: measured on one trivial prompt,
# ``claude-opus-5`` and ``claude-fable-5`` emitted a thinking block and
# ``claude-sonnet-5`` did not, and the same model will differ by prompt. So this
# cannot be detected per-response in advance — it is a per-model CAPABILITY, and
# any model that can think needs the headroom whether or not it uses it.
_THINKING_CAPABLE_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5")


def emits_thinking(model_id: str) -> bool:
    """True when this model may spend part of its output budget on thinking.

    Extend at runtime with ``MODEL_THINKING_MODELS`` (comma-separated ids) so a
    newly-shipped model can be given headroom without a deploy."""
    m = (model_id or "").strip().lower()
    m = m.split(":", 1)[1] if m.startswith("anthropic:") or m.startswith("openai:") else m
    return m in (set(_THINKING_CAPABLE_MODELS) | set(_csv_env("MODEL_THINKING_MODELS")))


def accepts_sampling_params(model_id: str) -> bool:
    """False when this model must be called with DEFAULT sampling only.

    Callers drop ``temperature``/``top_p`` rather than sending a value the API
    will reject. The consequence is worth stating where it is decided: a role
    that asked for ``temperature=0.0`` gets the model's default instead, so a
    judge pinned for determinism becomes non-deterministic. That is the API's
    constraint, not a choice available to us — the alternative is a 400 and no
    answer at all — but it is why the pinned values stay in the registry rather
    than being edited to 1.0: when a model that honours them is routed, they
    apply again.
    """
    m = (model_id or "").strip().lower()
    m = m.split(":", 1)[1] if m.startswith("anthropic:") or m.startswith("openai:") else m
    listed = set(_FIXED_SAMPLING_MODELS) | set(_csv_env("MODEL_FIXED_SAMPLING"))
    return m not in listed


def _csv_env(name: str) -> tuple:
    raw = (os.getenv(name) or "").strip()
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def is_vision_capable(model_id: str) -> bool:
    """True if ``model_id`` can accept an image input (V4 Image PRD §5.1). Used by the
    baseline preflight to degrade a misconfigured non-vision model to
    ``needs_baseline`` instead of silently grading an image case text-only."""
    m = (model_id or "").strip().lower()
    if not m:
        return False
    for pfx in _csv_env("MODEL_VISION_DENY") or ():
        if m.startswith(pfx):
            return False
    if any(m.startswith(pfx) for pfx in _VISION_INCAPABLE_PREFIXES):
        return False
    if any(m.startswith(pfx) for pfx in _csv_env("MODEL_VISION_ALLOW") or ()):
        return True
    return any(m.startswith(pfx) for pfx in _VISION_CAPABLE_PREFIXES)


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
