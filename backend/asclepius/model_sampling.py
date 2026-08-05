"""Sampling, judging, and measurement discipline ported from the eval fixes into
the V3/V4 product backend (Buyer Response PRD §10 / Aryaa Eval Fix PRD §F1-§F5).

Pure, dependency-light helpers so every model-call role in the product is:
  * explicit about temperature (G1) — generation is deliberately warm, measurement
    and grading are reproducible at 0.0; a missing role raises at startup rather than
    inheriting a silent provider default (~1.0) baked into shipped records;
  * recusal-aware (G3) — a judge never shares a provider family with the model under
    test;
  * span-validated (G4) — a judge verdict whose quoted evidence does not appear in the
    graded answer is excluded, not merely annotated;
  * parsed safely (G5) — a balanced-object JSON extractor so braces inside a quoted
    evidence span never corrupt depth counting.

Also carries the Wilson score interval used to report empirical-difficulty with a
confidence bound (G2) rather than a bare point estimate that gets used for pricing.
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# ─── G1: explicit temperature per model-call ROLE ─────────────────────────────
# Explicit and separated by role (Buyer Response PRD §10 G1). Grading and
# measurement MUST be reproducible; candidate generation is deliberately warmer,
# because the point there is to surface the range of answers a model might give a
# clinician — temperature 0 would hide the variance that makes the physician's
# critique valuable.
_DEFAULT_TEMPERATURES: Dict[str, float] = {
    "baseline_candidate": 0.7,   # generation: variety is the product
    "critic_generation": 0.7,
    "difficulty_probe": 0.0,     # measurement: must be reproducible
    "judge": 0.0,                # grading: must be reproducible
}

# Map each concrete llm_client role/purpose used by the product onto one of the
# temperature roles above, so a call site can look up its temperature by the role it
# already passes.
ROLE_TEMPERATURE_KEY: Dict[str, str] = {
    "asclepius_baseline": "baseline_candidate",
    "asclepius_baseline_candidate": "baseline_candidate",
    "asclepius_critic": "critic_generation",
    "asclepius_critic_generation": "critic_generation",
    "asclepius_empirical_difficulty": "difficulty_probe",
    "asclepius_empirical_difficulty_judge": "judge",
    "asclepius_judge": "judge",
    "asclepius_grader": "judge",
}


def model_temperatures() -> Dict[str, float]:
    """The configured temperature per role, each overridable via
    ``ASCLEPIUS_TEMP_<ROLE>`` (e.g. ``ASCLEPIUS_TEMP_JUDGE=0.0``)."""
    out: Dict[str, float] = {}
    for role, default in _DEFAULT_TEMPERATURES.items():
        raw = os.getenv(f"ASCLEPIUS_TEMP_{role.upper()}")
        try:
            out[role] = float(raw) if raw is not None else default
        except (TypeError, ValueError):
            out[role] = default
    return out


def temperature_for(role: str) -> float:
    """Explicit temperature for a temperature-role or a concrete llm_client role.
    Raises KeyError for an unknown role — a KeyError at a call site beats a silent
    provider default in production (Buyer Response PRD §10 G1)."""
    temps = model_temperatures()
    key = role if role in temps else ROLE_TEMPERATURE_KEY.get(role)
    if key is None or key not in temps:
        raise KeyError(
            f"no explicit temperature configured for role {role!r}; every "
            f"model-call role must set one (Buyer Response PRD §10 G1)")
    return temps[key]


def assert_temperatures_configured() -> None:
    """Startup assertion (Buyer Response PRD §10 G1): every configured role has an
    explicit temperature. Raises KeyError at boot rather than letting a call inherit a
    provider default at ~1.0 into a shipped record."""
    temps = model_temperatures()
    for role in _DEFAULT_TEMPERATURES:
        if role not in temps or temps[role] is None:
            raise KeyError(f"temperature role {role!r} is not configured")
    # Every concrete role we map must resolve, too.
    for role in ROLE_TEMPERATURE_KEY:
        temperature_for(role)


# ─── G1: prompt-hash version + digest that includes sampling params ───────────
# Bumped from the implicit v1: existing hashes are NOT comparable to new ones and
# must not silently appear to be (Buyer Response PRD §10 G1).
PROMPT_HASH_VERSION = "v2"


def prompt_hash(system: str, user: str, image_sha256: Optional[str] = None, *,
                temperature: Optional[float] = None, model: Optional[str] = None) -> str:
    """One digest over the exact inputs that make two records comparable — INCLUDING
    the sampling temperature and the model. Two records with the same hash must be
    genuinely comparable; without temperature and model in the digest the hash
    asserts a reproducibility it cannot deliver."""
    parts = [PROMPT_HASH_VERSION, system, user]
    if image_sha256:
        parts.append("img:" + image_sha256)
    if temperature is not None:
        parts.append(f"temp:{float(temperature):.3f}")
    if model:
        parts.append("model:" + str(model))
    import hashlib
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ─── G3: judge recusal ────────────────────────────────────────────────────────
def provider_family(model_or_provider: Optional[str]) -> Optional[str]:
    """Normalize a model id or provider to a coarse provider FAMILY for recusal.
    Same-family judging is the conflict §F2 removes: a model must not be graded by a
    judge from its own provider family."""
    if not model_or_provider:
        return None
    s = str(model_or_provider).lower()
    # Anchor the o-series to token boundaries (``o1``/``o3``/``o4``/``o4-mini``) so a
    # SKU that merely CONTAINS "o1"/"o3" as a substring is not misclassified as OpenAI
    # (which would break judge recusal). "gpt"/"openai" are distinctive enough as bare
    # substrings.
    if "gpt" in s or "openai" in s or re.search(r"\bo[1-4]\b", s):
        return "openai"
    if any(k in s for k in ("claude", "anthropic")):
        return "anthropic"
    if any(k in s for k in ("gemini", "google", "palm")):
        return "google"
    if any(k in s for k in ("llama", "meta")):
        return "meta"
    if "mistral" in s:
        return "mistral"
    if "grok" in s or "xai" in s:
        return "xai"
    return s


def eligible_judges(panel: List[str], model_provider_family: Optional[str], *,
                    min_panel_size: int = 2) -> Tuple[List[str], Optional[str]]:
    """(eligible_judges, deadlock_reason). Remove any judge sharing the model-under-
    test's provider family (Buyer Response PRD §10 G3). If fewer than
    ``min_panel_size`` judges remain, return ([], reason) so the caller routes the
    grade to physician adjudication (``passed=None``) rather than trusting a
    conflicted or too-thin panel."""
    fam = provider_family(model_provider_family)
    eligible = [j for j in (panel or []) if provider_family(j) != fam]
    if len(eligible) < max(1, min_panel_size):
        return [], (
            f"only {len(eligible)} eligible judge(s) after recusing the "
            f"{fam or 'unknown'} family; below min_panel_size={min_panel_size} — "
            f"routing to physician adjudication")
    return eligible, None


# ─── G4: evidence-span validation ─────────────────────────────────────────────
def _normalize_span(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def verify_span(span: Optional[str], answer: Optional[str]) -> bool:
    """True when ``span`` (a judge's quoted evidence) actually appears in ``answer``,
    via a normalization ladder: exact → whitespace/case-normalized → ellipsis-aware
    (a ``…``/``...`` split requires every non-empty fragment to appear in order).
    An unverified span excludes that judge's vote (Buyer Response PRD §10 G4) rather
    than merely annotating it."""
    if not span or not str(span).strip():
        return False
    if not answer:
        return False
    if span in answer:  # exact
        return True
    nspan, nans = _normalize_span(span), _normalize_span(answer)
    if nspan and nspan in nans:  # normalized
        return True
    # ellipsis-aware: fragments must appear in order.
    frags = [f for f in re.split(r"\.\.\.|…", nspan) if f.strip()]
    if len(frags) < 2:
        return False
    pos = 0
    for frag in frags:
        idx = nans.find(frag.strip(), pos)
        if idx < 0:
            return False
        pos = idx + len(frag.strip())
    return True


# ─── G5: balanced-object JSON extractor ───────────────────────────────────────
def extract_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract the first balanced top-level JSON object from ``text``, STRING-AWARE so
    braces inside a quoted evidence span do not affect depth counting (Buyer Response
    PRD §10 G5 / eval §F5.1). Tries a whole-string parse first, then scans for the
    first balanced ``{...}`` respecting quotes and escapes. Returns None on failure —
    never a greedy ``{.*}`` that would swallow prose braces before the verdict."""
    if not text:
        return None
    import json
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else None
    except Exception:
        pass
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        val = json.loads(text[start:i + 1])
                        if isinstance(val, dict):
                            return val
                    except Exception:
                        start = -1
    return None


# ─── G2: Wilson score interval for a binomial rate ────────────────────────────
def wilson_interval(successes: int, n: int, *, z: float = 1.96) -> Tuple[float, float]:
    """(low, high) Wilson score interval for a binomial proportion at ~95% (z=1.96).
    Used to report empirical-difficulty with a confidence bound (Buyer Response PRD
    §10 G2/§F1.4) instead of a bare point estimate. n==0 → (0.0, 1.0) (maximally
    uncertain)."""
    if n <= 0:
        return (0.0, 1.0)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))
