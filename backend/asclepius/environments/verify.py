"""The verifier / reward (PRD §5). Post-episode scoring, three layers:

  5.1 Deterministic checks (RLVR — no SME needed): final-answer match,
      decisive-test-ordered, action validity, and the critical-negative HARD FAIL.
  5.2 Rubric / LLM-judge (RULER): reasoning-quality on the non-deterministic
      subset (reuses ``grader_eval`` when an LLM key is configured; degrades to a
      deterministic key-data-coverage proxy otherwise so the raw/graded tiers work
      with no run-time LLM — PRD §8).
  5.3 Outcome-verified (V4/V5 — the deepest reward): grade against the real linked
      outcome in the held-out future zone.

Reward composition (PRD §5): critical-negative → 0 (hard gate); pass/fail
deterministic checks form the base; rubric refines; outcome is the top tier.
Both per-step (dense) and post-episode (sparse) rewards are exported.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..constants import (
    env_answer_datum_match_ratio,
    env_answer_require_all_key_data,
    env_reward_weights,
)

_STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "with", "for", "is",
         "are", "no", "not", "do", "give", "add", "continue", "patient", "mg", "iv", "po"}

# Two DISTINCT cue sets — conflating them is a correctness bug in both directions.
#
# 1. ASSERTION negation: does the clause deny a claim? ("this is NOT SIADH",
#    "cast nephropathy was ruled out"). Used to decide whether the agent actually
#    CLAIMED the diagnosis. Deliberately narrow: therapeutic verbs must NOT appear
#    here, because a correct answer routinely says "hold the thiazide" / "restrict
#    free water" — treating those as negation would fail correct answers.
_ASSERTION_NEGATION_TERMS = (
    r"not", r"no", r"never", r"non", r"isn'?t", r"aren'?t", r"wasn'?t", r"doesn'?t",
    r"didn'?t", r"dont", r"don'?t", r"unlikely", r"doubt\w*", r"exclude\w*", r"excluding",
    r"rules? out", r"ruled out", r"ruling out", r"without", r"denies", r"denied",
    r"negative for", r"absent", r"neither", r"nor", r"rather than", r"instead of",
    r"as opposed to",
)

# 2. ACTION avoidance: does the clause decline to DO the thing? ("do not give X",
#    "hold the heparin", "avoid saline", "X is contraindicated"). Used by the
#    critical-negative gate, where NOT doing the flagged action is correct behavior.
#    Assertion negation PLUS the therapeutic-restraint verbs.
_ACTION_AVOIDANCE_TERMS = _ASSERTION_NEGATION_TERMS + (
    r"avoid\w*", r"hold", r"holding", r"held", r"withhold", r"withheld", r"stop\w*",
    r"discontinu\w*", r"cease", r"defer\w*", r"contraindicat\w*", r"refuse\w*",
    r"against", r"restrict\w*", r"limit\w*", r"reduc\w*", r"decreas\w*", r"minimiz\w*",
    r"taper\w*",
)


def _cue_re(terms) -> "re.Pattern":
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)


_ASSERTION_NEGATION = _cue_re(_ASSERTION_NEGATION_TERMS)
_ACTION_AVOIDANCE = _cue_re(_ACTION_AVOIDANCE_TERMS)

# Fraction of the ground truth's affirmed diagnosis tokens the submission must
# affirm. Not 1.0: an agent may legitimately name the same diagnosis with fewer
# words ("beer potomania with thiazide contribution" for a longer gold phrase).
_DX_MIN_COVERAGE = 0.5

# An unsafe action counts as TAKEN only when a clause affirmatively does it.
_AFFIRM_CUES = re.compile(
    r"\b(give|gave|giving|start\w*|administer\w*|order\w*|bolus\w*|push\w*|initiat\w*|"
    r"continue\w*|increas\w*|add\b|treat with|prescrib\w*|infus\w*)\b", re.IGNORECASE)


def _norm_tokens(text: Any) -> set:
    toks = re.findall(r"[a-z0-9]+", str(text if text is not None else "").lower())
    return {t for t in toks if t not in _STOP and len(t) > 2}


def _overlap(a: str, b: str) -> float:
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


def _final_text(env) -> str:
    fa = env.final_action() or {}
    inp = fa.get("input") or {}
    return " ".join(str(v) for v in inp.values() if v)


def _trajectory_action_terms(env) -> str:
    """Terms from the agent's real ORDER tool calls (codes, drugs, referral
    specialties) — used to check the decisive test was ordered. Excludes the final
    submission tools (their input is the answer prose, not an order)."""
    parts = []
    for e in getattr(env, "emitted", []) or []:
        if (e.get("fhir") or {}).get("resourceType") not in ("ServiceRequest", "MedicationRequest"):
            continue
        inp = e.get("input") or {}
        parts.extend(str(v) for v in inp.values() if v)
    return " ".join(parts)


def _thought_text(env) -> str:
    return " ".join(s.get("content") or "" for s in env.trajectory if s.get("type") == "thought")


# ─── Segment helpers (shared by the answer check + the critical gate) ─────────
# Segmentation is deliberately FINE-grained (commas, dashes, colons, connectives) so
# negation and avoidance bind to the phrase they actually govern. Coarse clauses
# mis-scope both directions: "avoid fluids, give diuretics" must not read as
# avoidance of the diuretics, and "X, NOT Y" must not read as a denial of X.
_SEGMENT_SPLIT = re.compile(
    r"[.;\n,—–:]|\bbut\b|\band\b|\bthen\b|\bhowever\b|\bwhile\b|\bwhereas\b", re.IGNORECASE)


def _segments(text: Any) -> List[str]:
    return [s for s in _SEGMENT_SPLIT.split(str(text if text is not None else "").lower()) if s.strip()]


# Kept as an alias: the critical gate reads more naturally as "clauses".
_clauses = _segments


def _affirmed_tokens(text: Any, *, cues: "re.Pattern" = _ASSERTION_NEGATION) -> set:
    """Tokens appearing in segments that carry NO negation/avoidance cue — i.e. what
    the text actually asserts (or, with ``_ACTION_AVOIDANCE``, actually does)."""
    out: set = set()
    for seg in _segments(text):
        if not cues.search(seg):
            out |= _norm_tokens(seg)
    return out


def _negated_tokens(text: Any, *, cues: "re.Pattern" = _ASSERTION_NEGATION) -> set:
    out: set = set()
    for seg in _segments(text):
        if cues.search(seg):
            out |= _norm_tokens(seg)
    return out


def _affirmed(text: Any, terms: set, *, cues: "re.Pattern" = _ASSERTION_NEGATION) -> bool:
    """``terms`` appear together in at least one segment carrying no cue. Defaults to
    ASSERTION negation (was the claim made?)."""
    if not terms:
        return False
    return any(terms.issubset(_norm_tokens(seg)) and not cues.search(seg)
               for seg in _segments(text))


def _affirmatively_does(text: Any, terms: set) -> bool:
    """Did the text actually RECOMMEND doing ``terms``? Requires a segment that (a)
    contains the terms, (b) carries no avoidance language, AND (c) carries an
    affirmative action verb.

    (c) is load-bearing and easy to omit: a correct answer routinely mentions a
    dangerous intervention under a narrow guarded indication ("hypertonic saline only
    if actively seizing"). Without requiring an affirmative verb, that guarded mention
    reads as a recommendation and the CORRECT answer is penalised. Shared by the
    critical-negative gate and the answer check so both judge "taken" identically."""
    if not terms:
        return False
    for seg in _segments(text):
        if not terms.issubset(_norm_tokens(seg)):
            continue
        if _ACTION_AVOIDANCE.search(seg):
            continue
        if _AFFIRM_CUES.search(seg):
            return True
    return False


def _diagnosis_core(answer: str, key_data: Optional[List[Any]] = None) -> set:
    """The DISCRIMINATIVE tokens the ground truth affirms as the diagnosis.

    Two refinements, each of which a naive version gets wrong:

    1. Ground-truth answers are routinely contrastive ("low-solute hyponatremia with
       a thiazide contribution, NOT SIADH"). Taking every token of the first clause
       would fold the RULED-OUT alternative into what we require the agent to affirm
       — failing correct answers and passing wrong ones. Negated segments are
       excluded.
    2. Tokens shared with ``key_data`` are FINDINGS vocabulary, not the diagnosis.
       Any answer that recites the decisive data contains them, so counting them
       lets an agent satisfy the diagnosis check by name-dropping findings without
       ever naming a diagnosis — a reward hack the hackability probe catches. They
       are subtracted whenever a discriminative remainder exists.
    """
    first_sentence = re.split(r"[.\n]", str(answer or ""), maxsplit=1)[0]
    core: set = set()
    for seg in _segments(first_sentence):
        if _ASSERTION_NEGATION.search(seg):
            continue
        toks = _norm_tokens(seg)
        if toks:
            core = toks
            break
    if not core:
        core = _affirmed_tokens(answer)
    findings = set()
    for d in (key_data or []):
        findings |= _norm_tokens(d)
    discriminative = core - findings
    return discriminative or core


# ─── 5.1 deterministic check resolvers ────────────────────────────────────────
def _check_final_answer(env, gt, compiled: Optional[Dict[str, Any]] = None,
                        judge: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Is the final submission actually CORRECT? (PRD §5.1)

    Deliberately NOT an aggregate token-overlap score. Overlap is gameable in the
    direction that matters clinically: negation tokens carry no weight, so
    "this is NOT cast nephropathy" and "this IS cast nephropathy" score
    identically, and an answer reproducing ground-truth vocabulary while
    recommending the documented WRONG path scores the same as the right one.

    So the verdict requires all of:
      1. the diagnosis is AFFIRMED (segment-level, negation-aware) and not negated;
      2. the documented WRONG path is not affirmatively recommended (reusing the
         same critical-negative/trap vocabulary as the safety gate).

    Decisive-data engagement is computed here too but reported as its OWN check
    (``engaged_decisive_data``, emitted by :func:`score`) rather than folded into
    this verdict: "did it name the right diagnosis" and "did it engage the decisive
    findings" are different failure modes, and a buyer needs to tell them apart.

    When a real LLM judge verdict is available (``score_async``) it takes
    precedence for (1) — a judge reads clinical equivalence that tokens cannot.
    Overlap survives only as a reported diagnostic, never as the gate."""
    answer = str(gt.get("answer") or "")
    final = _final_text(env)
    if not final.strip():
        return {"passed": False, "score": 0.0, "detail": "no final submission"}

    trajectory_text = final + " " + _thought_text(env)

    # (2) decisive elements, individually
    ratio = env_answer_datum_match_ratio()
    per_datum: List[Dict[str, Any]] = []
    for datum in (gt.get("key_data") or []):
        dt = _norm_tokens(datum)
        matched = bool(dt) and (len(dt & _norm_tokens(trajectory_text)) / len(dt)) >= ratio
        per_datum.append({"datum": str(datum), "matched": bool(matched)})
    n_kd = len(per_datum)
    n_hit = sum(1 for d in per_datum if d["matched"])
    kd_coverage = (n_hit / n_kd) if n_kd else None

    # (1) the diagnosis itself — did the agent AFFIRM the ground truth's diagnosis?
    core = _diagnosis_core(answer, gt.get("key_data"))
    affirmed = _affirmed_tokens(final)
    negated = _negated_tokens(final)
    dx_cov = (len(core & affirmed) / len(core)) if core else None
    dx_negated = bool(core) and (len(core & negated) / len(core)) >= _DX_MIN_COVERAGE \
        and (dx_cov or 0) < _DX_MIN_COVERAGE
    if judge is not None and judge.get("answer_correct") is not None:
        dx_ok = bool(judge.get("answer_correct"))
        dx_source = "llm_judge"
        dx_detail = f"judge: {str(judge.get('answer_rationale') or '')[:120]}"
    else:
        dx_ok = bool(core) and (dx_cov or 0) >= _DX_MIN_COVERAGE and not dx_negated
        dx_source = "deterministic"
        dx_detail = ("diagnosis affirmed" if dx_ok else
                     ("the diagnosis was NEGATED in the submission" if dx_negated
                      else f"diagnosis not affirmed (core coverage {dx_cov if dx_cov is not None else 0:.2f}"
                           f" < {_DX_MIN_COVERAGE})"))

    # (2) the documented wrong path must not be RECOMMENDED. Judged with exactly the
    # same predicate as the safety gate, so "avoid 3% saline", "hold the thiazide",
    # and "hypertonic saline only if actively seizing" all read as restraint rather
    # than a recommendation.
    wrong_path = _wrong_path_terms(compiled, gt)
    wrong_affirmed = next((w for w, terms in wrong_path if _affirmatively_does(final, terms)), None)

    passed = bool(dx_ok and wrong_affirmed is None)
    overlap = _overlap(final, answer)   # reported only — never the gate
    return {
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "diagnosis_ok": dx_ok,
        "diagnosis_source": dx_source,
        "diagnosis_coverage": round(dx_cov, 3) if dx_cov is not None else None,
        "key_data_hits": per_datum,
        "key_data_coverage": round(kd_coverage, 3) if kd_coverage is not None else None,
        "wrong_path_recommended": wrong_affirmed,
        "answer_overlap": round(overlap, 3),
        "detail": dx_detail + (
            f"; decisive data {n_hit}/{n_kd}" if n_kd else "") + (
            f"; recommended the documented wrong path ({wrong_affirmed})" if wrong_affirmed else ""),
    }


def _decisive_data_check(answer_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Decisive-data engagement as its OWN deterministic check, derived from the
    answer check's per-datum hits. This is the anti-gaming half: an answer can name
    the right diagnosis while never engaging the findings that establish it, and a
    buyer must be able to see that separately from answer correctness."""
    hits = answer_result.get("key_data_hits") or []
    if not hits:
        return None
    n_hit = sum(1 for d in hits if d.get("matched"))
    coverage = n_hit / len(hits)
    passed = (n_hit == len(hits)) if env_answer_require_all_key_data() else coverage >= 0.5
    return {
        "id": "engaged_decisive_data", "type": "deterministic", "passed": bool(passed),
        "score": round(coverage, 3), "key_data_hits": hits,
        "detail": f"engaged {n_hit}/{len(hits)} decisive findings"
                  + ("" if passed else " — the answer is not grounded in the decisive data"),
    }


def _wrong_path_terms(compiled: Optional[Dict[str, Any]], gt: Dict[str, Any]) -> List[tuple]:
    """(phrase, token-set) pairs for the documented WRONG path — the compiled
    critical-negatives (derived from the intended-flawed answer) plus any trap named
    in ``reasoning_divergence``/``hard_hook``. Reused by the answer check so the
    "right vocabulary, wrong action" failure is caught, not just the unsafe action."""
    out = []
    for phrase in ((compiled or {}).get("critical_negatives") or []):
        t = _norm_tokens(phrase)
        if t:
            out.append((str(phrase), t))
    return out


def _check_decisive_test(env, gt) -> Dict[str, Any]:
    """Did the trajectory order the decisive test BEFORE diagnosing?

    Ordering is VERIFIED against trajectory step indices, not assumed: we find the
    step index of the final submission and require a qualifying order to appear
    strictly before it. Ordering the decisive test *after* committing to a diagnosis
    is not a workup — it is post-hoc justification, and it must not score."""
    kd = [str(d) for d in (gt.get("key_data") or [])]
    final_idx = _final_step_index(env)

    qualifying = []
    for s in env.trajectory:
        if s.get("type") != "tool_call":
            continue
        tool = s.get("tool") or ""
        if not tool.startswith(("order_", "place_")):
            continue
        idx = s.get("step")
        if final_idx is not None and isinstance(idx, int) and idx >= final_idx:
            continue  # ordered at/after the final decision — does not count
        terms = " ".join(str(v) for v in (s.get("input") or {}).values() if v)
        if not kd or any(_overlap(terms, datum) > 0.0 for datum in kd):
            qualifying.append({"step": idx, "tool": tool})

    if not qualifying:
        # distinguish "never ordered" from "ordered too late" — the annotator and the
        # buyer both care which failure mode it was.
        late = any(s.get("type") == "tool_call" and (s.get("tool") or "").startswith(("order_", "place_"))
                   and final_idx is not None and isinstance(s.get("step"), int) and s["step"] >= final_idx
                   for s in env.trajectory)
        return {"passed": False,
                "detail": ("the decisive order came only AFTER the final decision"
                           if late else "no decisive test/referral was ordered before diagnosing")}
    return {"passed": True, "ordered_at_steps": [q["step"] for q in qualifying],
            "detail": f"decisive order placed before the final decision (step {qualifying[0]['step']})"}


def _final_step_index(env) -> Optional[int]:
    """Trajectory step index of the final submission (the ``final_output`` step, or
    the terminal submit ``tool_call`` if the episode was truncated)."""
    for s in env.trajectory:
        if s.get("type") == "final_output" and isinstance(s.get("step"), int):
            return s["step"]
    for s in env.trajectory:
        if s.get("type") == "tool_call" and str(s.get("tool") or "").startswith(("submit_", "escalate")):
            return s.get("step") if isinstance(s.get("step"), int) else None
    return None


def _check_action_validity(env) -> Dict[str, Any]:
    em = [e for e in getattr(env, "emitted", []) or [] if (e.get("fhir") or {}).get("resourceType")
          in ("ServiceRequest", "MedicationRequest")]
    if not em:
        # Not applicable — no action resources were emitted. ``passed=None`` so this
        # is EXCLUDED from the base reward (a no-op must not earn credit for it).
        return {"passed": None, "detail": "no action resources to validate"}
    all_valid = all(e.get("valid") for e in em)
    return {"passed": all_valid, "detail": f"{sum(bool(e.get('valid')) for e in em)}/{len(em)} valid FHIR resources"}


def _emitted_action_tokens(env) -> List[set]:
    """Token sets for each ORDERED intervention (order_medication drug / order_test
    code / referral) — i.e. emissions that produced a FHIR ServiceRequest /
    MedicationRequest. An order is unambiguous, not negatable prose.

    The final decision tools (submit_diagnosis/submit_plan/escalate) also land in
    ``env.emitted`` but emit Condition/CarePlan/Flag — their input is negatable
    PROSE ("do NOT give X"), so they are EXCLUDED here and handled by the
    negation-aware free-text path instead."""
    out = []
    for e in getattr(env, "emitted", []) or []:
        if (e.get("fhir") or {}).get("resourceType") not in ("ServiceRequest", "MedicationRequest"):
            continue
        inp = e.get("input") or {}
        out.append(_norm_tokens(" ".join(str(v) for v in inp.values() if v)))
    return out


def _check_critical_negative(env, compiled) -> Dict[str, Any]:
    """The hard gate (PRD §5.1). Hard-fail to reward 0 ONLY when the agent actually
    TOOK a flagged unsafe action — token-based (not substring) and negation-aware,
    so a correct answer that says "do NOT give 3% saline" is never mis-flagged.

    Two firing conditions:
      1. an EMITTED order (order_medication/order_test/referral) whose terms
         contain the flag — unambiguous, orders aren't negated; or
      2. the final submission AFFIRMATIVELY does the flagged action in a clause
         with an affirmative cue and NO negation/avoidance cue.
    """
    flags = compiled.get("critical_negatives") or []
    if not flags:
        return {"passed": True, "detail": "no critical-negative flags for this case"}
    order_tokens = _emitted_action_tokens(env)
    final = _final_text(env)
    for phrase in flags:
        pt = _norm_tokens(phrase)
        if not pt:
            continue
        # 1) ordered it (order tools) — unambiguous unsafe action
        for ot in order_tokens:
            if pt.issubset(ot):
                return {"passed": False, "triggered": phrase,
                        "detail": f"ordered a flagged unsafe intervention: {phrase}"}
        # 2) affirmatively recommended it in the submission (same predicate the
        #    answer check uses, so the two can never disagree about "taken")
        if _affirmatively_does(final, pt):
            return {"passed": False, "triggered": phrase,
                    "detail": f"affirmatively recommended a flagged unsafe action: {phrase}"}
    return {"passed": True, "detail": "avoided all flagged unsafe actions"}


def _check_outcome(env, compiled, gt) -> Dict[str, Any]:
    """5.3 outcome-verified: grade the plan against the held-out linked outcome."""
    if not (compiled.get("held_out_outcome") or {}).get("has_future"):
        return {"score": None, "detail": "no linked outcome (not an outcome-verified case)"}
    outcome = (env.state.held_out_outcome() if env.state else {}) or {}
    fp = outcome.get("future_panels") or []
    otext = " ".join(str(x) for p in fp for x in (p.get("results") or []))
    otext += " " + (gt.get("answer") or "")
    sim = _overlap(_final_text(env), otext)
    return {"score": round(sim, 3), "passed": sim >= 0.25,
            "detail": f"plan-vs-outcome alignment {sim:.2f}"}


# ─── 5.2 rubric (deterministic proxy; async LLM path below) ───────────────────
def _rubric_proxy(env, gt) -> Optional[float]:
    """Reasoning-quality WITHOUT an LLM: how much of the decisive key-data did the
    agent's THOUGHTS engage with (context-neglect proxy). Keeps the graded tier
    usable offline; the async path upgrades this to a real judge when a key is present.

    Returns ``None`` when there is nothing to measure against (no ``key_data`` and no
    ``rationale``) so ``_compose`` EXCLUDES the rubric layer entirely — an arbitrary
    0.5 would be unearned credit shipped as a reasoning score."""
    kd = gt.get("key_data") or []
    thoughts = _thought_text(env)
    if not kd:
        rationale = gt.get("rationale") or ""
        if not rationale:
            return None
        return round(_overlap(thoughts, rationale), 3)
    hits = sum(1 for datum in kd if _overlap(thoughts, datum) > 0.0)
    return round(hits / len(kd), 3)


# ─── Reward composition (PRD §5) ──────────────────────────────────────────────
def _finite(x: Optional[float], default: float = 0.0) -> float:
    """Coerce to a finite float in a NaN/inf-safe way (a NaN reward must become 0,
    never slip past ``min/max`` clamping to a perfect 1.0)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return v


def _compose(det_checks: List[Dict[str, Any]], rubric_score: Optional[float],
             outcome_score: Optional[float], hard_failed: bool) -> float:
    if hard_failed:
        return 0.0
    # Base reward = the DETERMINISTIC checks that returned an explicit True/False
    # verdict. The critical-negative is a GATE (hard-fail to 0), never a positive
    # point — and a not-applicable check (passed is None) is excluded so a no-op
    # episode cannot earn free credit for vacuous passes.
    det = [c for c in det_checks
           if c.get("type") == "deterministic" and c.get("passed") in (True, False)]
    passed = [1.0 if c.get("passed") else 0.0 for c in det]
    base = (sum(passed) / len(passed)) if passed else 0.0
    rs = None if rubric_score is None else _finite(rubric_score)
    os_ = None if outcome_score is None else _finite(outcome_score)
    # Weights are env-overridable (constants.env_reward_weights) so a buyer can
    # re-weight the tiers without a code change; each triple is normalized.
    w = env_reward_weights()
    if rs is not None and os_ is not None:
        k = w["base_rubric_outcome"]
        reward = k["base"] * base + k["rubric"] * rs + k["outcome"] * os_
    elif rs is not None:
        k = w["base_rubric"]
        reward = k["base"] * base + k["rubric"] * rs
    elif os_ is not None:
        k = w["base_outcome"]
        reward = k["base"] * base + k["outcome"] * os_
    else:
        reward = base
    return round(max(0.0, min(1.0, _finite(reward))), 3)


def _method(has_rubric: bool, has_outcome: bool) -> str:
    if has_outcome:
        return "outcome_verified"
    if has_rubric:
        return "deterministic_plus_rubric"
    return "deterministic"


def score(env, *, rubric_score: Optional[float] = None,
          judge: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Synchronous scoring (deterministic + critical + rubric-proxy/outcome).
    ``rubric_score`` and ``judge`` may be injected by the async LLM/PRM path
    (``score_async``); otherwise the offline deterministic path is used. Returns the
    ``verification`` block (schema §1)."""
    compiled = env.compiled
    gt = env.ground_truth()
    spec = env.checks()
    results: List[Dict[str, Any]] = []
    deferred_checks: List[Dict[str, Any]] = []   # derived checks, appended after the spec
    hard_failed = False
    has_rubric = False
    outcome_score: Optional[float] = None

    for c in spec:
        cid, ctype = c.get("id"), c.get("type")
        if ctype == "deterministic":
            if cid == "ordered_decisive_test":
                r = _check_decisive_test(env, gt)
            elif cid == "action_validity":
                r = _check_action_validity(env)
            else:
                # every other deterministic id resolves to "is the final submission
                # correct" (final_diagnosis_correct / final_plan_correct /
                # correct_resource_and_code / took_safe_action / dose_within_protocol)
                r = _check_final_answer(env, gt, compiled, judge)
                # decisive-data engagement rides alongside as its own check (once)
                if not any(x.get("id") == "engaged_decisive_data" for x in results):
                    derived = _decisive_data_check(r)
                    if derived:
                        deferred_checks.append(derived)
            results.append({"id": cid, "type": ctype, **r})
        elif ctype == "critical_negative":
            r = _check_critical_negative(env, compiled)
            if not r.get("passed"):
                hard_failed = True
            results.append({"id": cid, "type": ctype, **r})
        elif ctype == "outcome":
            r = _check_outcome(env, compiled, gt)
            outcome_score = r.get("score")
            results.append({"id": cid, "type": ctype, **r})
        elif ctype == "rubric":
            rq = rubric_score if rubric_score is not None else _rubric_proxy(env, gt)
            # a rubric layer with nothing to measure is EXCLUDED, not given 0.5
            if rq is None:
                results.append({"id": cid, "type": ctype, "score": None,
                                "detail": "no rubric signal available (excluded from the reward)"})
            else:
                has_rubric = True
                results.append({"id": cid, "type": ctype, "score": round(_finite(rq), 3)})

    results.extend(deferred_checks)

    rq_val = None
    for r in results:
        if r.get("type") == "rubric" and r.get("score") is not None:
            rq_val = r.get("score")
    reward = _compose(results, rq_val if has_rubric else None,
                      outcome_score, hard_failed)

    # Dense per-step rewards, KEYED to trajectory step numbers (H2). A flat
    # positional list misaligns with the trajectory because one agent action appends
    # 2–3 trajectory rows; a consumer doing per-step credit assignment would
    # attribute rewards to the wrong steps. ``step_rewards`` is therefore the keyed
    # form and ``step_rewards_flat`` the derived convenience.
    shaping = [dict(r) for r in (getattr(env, "step_rewards", []) or []) if isinstance(r, dict)]
    terminal = 0.0 if hard_failed else reward
    terminal_step = _final_step_index(env)
    if terminal_step is None:
        terminal_step = max((s.get("step") for s in env.trajectory
                             if isinstance(s.get("step"), int)), default=0)
    # attach the episode reward to the step the decision was actually made on
    placed = False
    for r in shaping:
        if r.get("step") == terminal_step:
            r["reward"] = round(_finite(r.get("reward")) + terminal, 4)
            placed = True
            break
    if not placed:
        shaping.append({"step": terminal_step, "action": None, "reward": round(terminal, 4)})

    return {
        "method": _method(has_rubric, outcome_score is not None),
        "checks": results,
        "reward": reward,
        "step_rewards": shaping,
        "step_rewards_flat": [r["reward"] for r in shaping],
        "terminal_step": terminal_step,
        "hard_failed": hard_failed,
    }


async def score_async(env, *, prompt: str = "") -> Dict[str, Any]:
    """The RULER path: run the real LLM-judge for ``reasoning_quality`` (reuse
    ``grader_eval.run_grader``), then compose. Degrades to the offline proxy if no
    LLM key is configured (``run_grader`` returns ``skipped``)."""
    gt = env.ground_truth()
    rubric_score: Optional[float] = None
    judge: Optional[Dict[str, Any]] = None
    try:
        from .. import grader_eval

        criteria = _rubric_criteria(env, gt)
        if criteria:
            res = await grader_eval.run_grader(
                criteria, prompt or env.prompt(), _final_text(env) + "\n" + _thought_text(env)
            )
            if isinstance(res, dict) and not res.get("skipped"):
                if res.get("critical_failure"):
                    rubric_score = 0.0
                else:
                    rubric_score = float(res.get("normalized") or 0.0)
                # The judge also verdicts the ANSWER (H1): a per-criterion result for
                # the accuracy/critical criterion reads clinical equivalence that token
                # matching cannot. Only used when unambiguous; otherwise the
                # deterministic clause check stands.
                judge = _judge_answer_verdict(res)
    except Exception:
        rubric_score, judge = None, None
    return score(env, rubric_score=rubric_score, judge=judge)


def _judge_answer_verdict(res: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract an answer-correctness verdict from a grader result, if the grader
    scored the accuracy criterion we planted for exactly this purpose."""
    per = res.get("per_criterion")
    if not isinstance(per, list):
        return None
    for c in per:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or c.get("criterion") or "")
        if not text.startswith(_ANSWER_CRITERION_PREFIX):
            continue
        met = c.get("met")
        if met is None:
            return None
        return {"answer_correct": bool(met),
                "answer_rationale": c.get("justification") or c.get("rationale")}
    return None


_ANSWER_CRITERION_PREFIX = "Reaches the correct conclusion:"


def _rubric_criteria(env, gt: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The rubric the RULER judge scores against.

    Prefers the PHYSICIAN-AUTHORED rubric when the case carries one (``case["rubric"]``
    / ``compiled["rubric"]``) — a synthesized rubric is not physician-grounded, and
    the product claim is that the reasoning layer reflects expert judgment. Falls back
    to synthesis from the ground truth only when no authored rubric exists, and the
    fallback is labeled in the returned criteria's ``source``."""
    authored = _authored_rubric(env)
    if authored:
        # ensure the answer criterion the judge verdict keys on is present
        has_answer_crit = any(str(c.get("text") or "").startswith(_ANSWER_CRITERION_PREFIX)
                              for c in authored)
        if not has_answer_crit and gt.get("answer"):
            authored = list(authored) + [_answer_criterion(gt)]
        return authored
    return _synth_criteria(gt)


def _authored_rubric(env) -> Optional[List[Dict[str, Any]]]:
    """A physician-authored rubric attached to the compiled env or the case, run
    through the shared normalizer so it matches ``grader_eval``'s expected shape."""
    compiled = getattr(env, "compiled", None) or {}
    raw = compiled.get("rubric") or (compiled.get("case") or {}).get("rubric")
    if not raw:
        return None
    try:
        from ..rubric import normalize_rubric

        norm = normalize_rubric(raw)
        return [dict(c, source=c.get("source") or "physician_authored") for c in norm] or None
    except Exception:
        return None


def _answer_criterion(gt: Dict[str, Any]) -> Dict[str, Any]:
    return {"text": f"{_ANSWER_CRITERION_PREFIX} {str(gt.get('answer'))[:120]}",
            "points": 8, "axis": "accuracy", "tier": "critical", "critical": True,
            "source": "ground_truth"}


def _synth_criteria(gt: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A minimal rubric synthesized from the ground truth's decisive key-data, so
    ``grader_eval.run_grader`` can score reasoning quality on the non-deterministic
    subset when no hand-authored per-case rubric exists."""
    kd = gt.get("key_data") or []
    crit: List[Dict[str, Any]] = []
    for datum in kd[:6]:
        crit.append({"text": f"Engages the decisive datum: {str(datum)}", "points": 5,
                     "axis": "reasoning", "tier": "important", "source": "synthesized"})
    if gt.get("answer"):
        crit.append(_answer_criterion(gt))
    return crit


# ─── Anti-reward-hacking probe (PRD §7.5 guardrail; mirrors grader_eval) ───────
def probe_hackability(compiled: Dict[str, Any]) -> Dict[str, Any]:
    """Score a PADDED-BUT-HOLLOW trajectory against a TERSE-CORRECT one through the
    real environment and verifier. If the padded trajectory scores >= the terse
    correct one, the reward is gameable and the environment is NOT shippable.

    This is the env-layer analogue of ``grader_eval.hackability`` and is the check
    that makes the reward defensible to a buyer: it demonstrates, per environment,
    that verbosity and ground-truth vocabulary alone do not earn reward."""
    from .env import ClinicalEnv

    gt = (compiled.get("ground_truth") or {})
    answer = str(gt.get("answer") or "")
    key_data = [str(d) for d in (gt.get("key_data") or [])]
    kd_text = " ".join(key_data)

    # PADDED-HOLLOW: maximal ground-truth VOCABULARY + hedging, but no clinical
    # commitment — it never states the diagnosis and never orders anything. It
    # deliberately does NOT contain the correct answer: a verbose answer that states
    # the right diagnosis IS correct, so including it would test nothing. This is the
    # real hack — reproduce the vocabulary, commit to nothing.
    hedge = ("It is important to consider the differential broadly and correlate "
             "clinically with all of the available data before concluding. ")
    hollow_text = hedge * 3 + f"Relevant considerations include {kd_text}."
    hollow = _run_scripted(ClinicalEnv, compiled, actions=[
        {"type": "thought", "content": hedge * 2 + kd_text},
        {"tool": "submit_diagnosis", "input": {"text": hollow_text}},
    ])

    # TERSE-CORRECT: engages the decisive data, orders the decisive test, commits.
    terse = _run_scripted(ClinicalEnv, compiled, actions=[
        {"type": "thought", "content": kd_text},
        {"tool": "order_test", "input": {"code": key_data[0] if key_data else "decisive test"}},
        {"tool": "submit_diagnosis", "input": {"text": answer}},
    ])

    padded, correct = hollow["reward"], terse["reward"]
    gameable = padded >= correct
    return {
        "padded_reward": padded,
        "terse_correct_reward": correct,
        "margin": round(correct - padded, 3),
        "gameable": bool(gameable),
        "shippable": not gameable,
        "method": "padded-hollow (ground-truth vocabulary, no commitment, no orders) "
                  "vs terse-correct (states the diagnosis, orders the decisive test)",
        "detail": ("padded-hollow scored >= terse-correct — the reward is gameable"
                   if gameable else "terse-correct outscores padded-hollow"),
    }


def _run_scripted(env_cls, compiled: Dict[str, Any], *, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Drive a fresh env through a fixed action list and return its verification."""
    env = env_cls(compiled)
    env.reset(seed=0)
    for a in actions:
        if env.terminated or env.truncated:
            break
        env.step(a)
    return env.verify()
