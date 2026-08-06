"""The calibration exam — a work sample scored against a reference key.

This is the highest-value item in PRD C, and it is the only feature in the tiering model with
both the strongest empirical support and a clean legal footing: a work sample scored against a
reference key is content-valid almost by construction. Everything else in ``tiering.FEATURES``
is a proxy for the ability to adjudicate. This one *is* the ability to adjudicate, measured.

─── Three rules this module will not bend ─────────────────────────────────────────────────

1. **The key is the aggregate of a reference panel, never one person's opinion.** Structured
   implicit peer review reaches κ ≈ 0.5 with one or two reviewers. Discussion between reviewers
   raises within-pair agreement but not across-pair reliability. An exam keyed by one senior
   physician measures agreement with that physician, which is not the thing we are selling.
   ``PANEL_MIN`` independent judgments, and an item whose panel did not itself converge
   (``PANEL_MIN_AGREEMENT``) is not admissible as a key at all — if the experts disagree, the
   item cannot discriminate and scoring a candidate against it is noise wearing a number.

2. **Items are drawn from the actual task distribution.** An exam built from textbook vignettes
   measures a different skill from the one the job needs. ``build_item_bank`` sources from real
   ``tasks`` rows that already carry a settled adjudication, in the candidate's declared
   specialty.

3. **Raw per-item responses are stored, always.** You will want to re-key the exam later —
   because the panel grew, because an item turned out to be ambiguous, because the task
   distribution moved — and you must be able to do that *without re-testing everyone*.
   ``rescore_attempt`` recomputes an old attempt against today's keys from the raw responses.
   A module that stored only the score would make every future re-key a re-recruitment.

─── What is scored, and why three numbers instead of one ──────────────────────────────────

A TR does three separable things, and a candidate can be strong at one and blind at another:

* **choice** — did they pick the better of two model answers
* **localization** — did they localize the reasoning break to the right step
* **rubric** — does their judgment of which criteria are load-bearing agree with the panel's

Collapsing these into one number lets a candidate pass on the strength of the easiest one. So
all three are scored separately, the composite gates, **and a floor applies to each** — see
``TR_GATE`` below. That floor is this module's one deliberate addition to the PRD's process,
and the reason for it is in ``grade()``.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("asclepius.calibration")

# ─── Exam shape ──────────────────────────────────────────────────────────────────────────
EXAM_MIN_ITEMS = 12       # PRD §4: 12–20 short vignettes
EXAM_MAX_ITEMS = 20

# ─── Reference panel admissibility ───────────────────────────────────────────────────────
PANEL_MIN = 3                  # independent judgments before an item may key anything
PANEL_MIN_AGREEMENT = 0.67     # the panel must itself converge, or the item is not a key

# ─── Gates ───────────────────────────────────────────────────────────────────────────────
# Industry norm for a production annotation gate is >=85% agreement with gold. That is the TR
# bar. The TL bar is deliberately much lower: a TL's work is a first draft that a second TL
# independently repeats and a TR grades, so the structure — not the individual — carries the
# reliability. Setting the TL gate near the TR gate would reject most of the supply to protect
# against an error the double-labelling already catches.
TR_GATE = 0.85
TL_GATE = 0.60

# The per-subscore floor. A candidate scoring 1.00 on choice and 0.55 on localization
# composites to 0.85 and would pass on the PRD's single-number rule — while being unable to do
# the second of the three things the job is. The floor is set well below the composite gate so
# it only ever catches a genuinely lopsided profile, not ordinary sampling noise on 12 items.
TR_SUBSCORE_FLOOR = 0.70

# Weighting of the three subscores in the composite. Choice is the decision the pipeline
# actually consumes, so it carries the most; localization and rubric are what make a TR's
# adjudication *useful* to a buyer rather than merely correct.
SUBSCORE_WEIGHTS = {"choice": 0.5, "localization": 0.3, "rubric": 0.2}

# calibration_z spread used before an empirical population exists. See calibration_z().
_COLD_START_SPREAD = 0.10

# ─── Retake policy ───────────────────────────────────────────────────────────────────────
# Without one, the exam is not a gate. A candidate can sit it repeatedly until a lucky sample
# of items clears 0.85, and — worse — repeated attempts leak the key by trial and error, since
# every attempt draws from the same pool and reveals which answers scored. Two attempts is
# enough to absorb a genuinely bad session; the cooldown makes brute force uneconomic.
MAX_ATTEMPTS = 2
RETAKE_COOLDOWN_DAYS = 90


class RetakeNotAllowed(RuntimeError):
    """The candidate has spent their attempts and the cooldown has not elapsed."""


class ExamNotAvailable(RuntimeError):
    """Not enough admissible keyed items to build an exam for this specialty.

    Raised rather than padded. An exam short of ``EXAM_MIN_ITEMS``, or built from items whose
    reference panels never converged, produces a number that looks like a calibration score and
    is not one — and that number then walks into ``tiering.FEATURES['calibration_z']`` and into
    a TR eligibility decision. Failing closed keeps the TR gate shut, which is the correct
    behaviour when we cannot measure.
    """


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Building the key from a reference panel
# ═══════════════════════════════════════════════════════════════════════════════════════════

def aggregate_panel(judgments: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn N independent expert judgments of one item into a reference key.

    Each judgment is ``{"better_answer_id", "break_step", "load_bearing": [criteria]}``; any
    field may be absent, and a field the panel did not converge on is keyed as ``None`` rather
    than being decided by a plurality of two. Returns the key plus the panel's own agreement,
    which ``admissible()`` then uses to decide whether the item may key anything at all.
    """
    n = len(judgments)
    out: Dict[str, Any] = {"panel_n": n}
    if n < PANEL_MIN:
        return {**out, "admissible": False, "reason": "panel_too_small"}

    def modal(field: str) -> Tuple[Optional[Any], float]:
        vals = [j.get(field) for j in judgments if j.get(field) not in (None, "")]
        if not vals:
            return None, 0.0
        counts: Dict[Any, int] = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
        best, cnt = max(counts.items(), key=lambda kv: kv[1])
        return best, cnt / n

    better, better_agree = modal("better_answer_id")
    step, step_agree = modal("break_step")

    # Load-bearing criteria: a criterion is in the key when a majority of the panel named it.
    # This is the one field where partial agreement is meaningful — panels routinely agree on
    # three of four criteria — so it is scored by set overlap rather than exact match.
    tally: Dict[str, int] = {}
    for j in judgments:
        for c in (j.get("load_bearing") or []):
            key = str(c).strip().casefold()
            if key:
                tally[key] = tally.get(key, 0) + 1
    load_bearing = sorted(c for c, k in tally.items() if k > n / 2)
    load_agree = (sum(tally[c] for c in load_bearing) / (len(load_bearing) * n)
                  if load_bearing else 0.0)

    out.update({
        "better_answer_id": better if better_agree >= PANEL_MIN_AGREEMENT else None,
        "better_answer_agreement": round(better_agree, 3),
        "break_step": step if step_agree >= PANEL_MIN_AGREEMENT else None,
        "break_step_agreement": round(step_agree, 3),
        "load_bearing": load_bearing,
        "load_bearing_agreement": round(load_agree, 3),
    })
    out["admissible"], out["reason"] = admissible(out)
    return out


def admissible(key: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """An item may key an exam only if its panel converged on at least the primary judgment.

    If the reference panel could not agree which of two model answers is better, the item does
    not measure the candidate — it measures the item. Scoring against it manufactures
    disagreement and pushes real physicians below the gate for no reason.
    """
    if int(key.get("panel_n") or 0) < PANEL_MIN:
        return False, "panel_too_small"
    if not key.get("better_answer_id"):
        return False, "panel_did_not_converge_on_better_answer"
    return True, None


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Grading
# ═══════════════════════════════════════════════════════════════════════════════════════════

def _jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa = {str(x).strip().casefold() for x in a if str(x).strip()}
    sb = {str(x).strip().casefold() for x in b if str(x).strip()}
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def grade(items: Sequence[Dict[str, Any]], responses: Dict[str, Dict[str, Any]]
          ) -> Dict[str, Any]:
    """Score one attempt against the current keys. Pure — no store, no clock.

    ``items`` are ``{"item_id", "key": {...}}``; ``responses`` is
    ``{item_id: {"better_answer_id", "break_step", "load_bearing"}}`` exactly as the candidate
    submitted it, which is also exactly what gets persisted.

    Each of the three subscores is computed over **only the items that key that field**. An
    item whose panel converged on the better answer but not on the break step contributes to
    ``choice`` and not to ``localization``, rather than counting as a miss — otherwise a
    candidate is penalised for the panel's disagreement, which is the opposite of the
    measurement we want.
    """
    tallies = {k: [0.0, 0] for k in ("choice", "localization", "rubric")}
    per_item: List[Dict[str, Any]] = []

    for item in items:
        key = item.get("key") or {}
        ok, why = admissible(key)
        if not ok:
            per_item.append({"item_id": item.get("item_id"), "scored": False, "reason": why})
            continue
        resp = responses.get(item.get("item_id")) or {}
        row: Dict[str, Any] = {"item_id": item.get("item_id"), "scored": True}

        got = resp.get("better_answer_id")
        hit = 1.0 if got and got == key.get("better_answer_id") else 0.0
        tallies["choice"][0] += hit
        tallies["choice"][1] += 1
        row["choice"] = hit

        if key.get("break_step") is not None:
            got_step = resp.get("break_step")
            hit_s = 1.0 if got_step is not None and got_step == key["break_step"] else 0.0
            tallies["localization"][0] += hit_s
            tallies["localization"][1] += 1
            row["localization"] = hit_s

        if key.get("load_bearing"):
            j = _jaccard(resp.get("load_bearing") or [], key["load_bearing"])
            tallies["rubric"][0] += j
            tallies["rubric"][1] += 1
            row["rubric"] = round(j, 3)

        per_item.append(row)

    subscores: Dict[str, Optional[float]] = {}
    for name, (total, n) in tallies.items():
        subscores[name] = round(total / n, 4) if n else None

    # The composite renormalizes over the subscores that were actually measurable, so an exam
    # whose items carry no keyed break steps is scored on what it does measure rather than
    # being silently halved.
    num = den = 0.0
    for name, weight in SUBSCORE_WEIGHTS.items():
        if subscores.get(name) is not None:
            num += weight * float(subscores[name])
            den += weight
    composite = round(num / den, 4) if den else None

    scored_n = sum(1 for r in per_item if r.get("scored"))
    floors_ok = all(
        v >= TR_SUBSCORE_FLOOR for v in
        (subscores[k] for k in subscores if subscores[k] is not None)
    )
    tr_pass = bool(
        composite is not None and composite >= TR_GATE
        and floors_ok and scored_n >= EXAM_MIN_ITEMS
    )
    tl_pass = bool(composite is not None and composite >= TL_GATE
                   and scored_n >= EXAM_MIN_ITEMS)

    return {
        "subscores": subscores,
        "composite": composite,
        "scored_items": scored_n,
        "total_items": len(items),
        "tr_gate": TR_GATE,
        "tl_gate": TL_GATE,
        "tr_subscore_floor": TR_SUBSCORE_FLOOR,
        "tr_gate_passed": tr_pass,
        "tl_gate_passed": tl_pass,
        "failed_floors": [k for k, v in subscores.items()
                          if v is not None and v < TR_SUBSCORE_FLOOR],
        "per_item": per_item,
    }


def calibration_z(composite: Optional[float],
                  population: Optional[Sequence[float]] = None) -> float:
    """Turn a composite into ``tiering.FEATURES['calibration_z']``.

    Centred on the TR gate, so ``z = 0`` means "exactly at the production bar" and the feature
    contributes nothing on its own — a candidate who scrapes the gate is not thereby a TR, they
    are merely eligible to be considered as one.

    With ``population`` of five or more prior composites, the empirical spread is used and the
    score becomes a genuine standardization against the physicians who have actually sat this
    exam. Below five, a fixed spread is used: inventing an sd from three data points would make
    the first three candidates' z-scores an artifact of who happened to go first. Clipped to
    ±3 either way, so one outlier cannot dominate ``s``.
    """
    if composite is None:
        return 0.0
    pop = [float(p) for p in (population or []) if p is not None]
    if len(pop) >= 5:
        mean = sum(pop) / len(pop)
        var = sum((p - mean) ** 2 for p in pop) / (len(pop) - 1)
        sd = math.sqrt(var)
        if sd < 1e-6:
            return 0.0
        return max(-3.0, min(3.0, (float(composite) - mean) / sd))
    return max(-3.0, min(3.0, (float(composite) - TR_GATE) / _COLD_START_SPREAD))


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Exam assembly + re-keying
# ═══════════════════════════════════════════════════════════════════════════════════════════

def check_retake_allowed(store: Any, *, user_id: str, specialty: str,
                         now: Optional[str] = None) -> None:
    """Raise ``RetakeNotAllowed`` if this candidate has spent their attempts.

    Attempts are counted SUBMITTED, not started: an attempt abandoned halfway — a browser
    closed, a pager going off mid-case — is not a data point about the physician and must not
    cost them one of two chances.
    """
    from datetime import datetime, timedelta

    attempts = [a for a in store.calibration_attempts_for_user(user_id, specialty=specialty)
                if a.get("submitted_at")]
    if len(attempts) < MAX_ATTEMPTS:
        return
    if any(a.get("tr_gate_passed") for a in attempts):
        # Already through the gate. Re-sitting can only lower it, and offering that is a trap.
        raise RetakeNotAllowed(
            "You have already passed the calibration exam for this specialty.")
    latest = max(a["submitted_at"] for a in attempts)
    ref = datetime.fromisoformat(now) if now else datetime.utcnow()
    try:
        elapsed = ref - datetime.fromisoformat(latest)
    except ValueError:
        return
    if elapsed < timedelta(days=RETAKE_COOLDOWN_DAYS):
        days = RETAKE_COOLDOWN_DAYS - elapsed.days
        raise RetakeNotAllowed(
            f"You have used both calibration attempts for this specialty. "
            f"You can sit it again in {days} day(s).")


def build_exam(store: Any, *, specialty: str, seed: Optional[int] = None,
               size: int = EXAM_MIN_ITEMS) -> Dict[str, Any]:
    """Select ``size`` admissible keyed items in ``specialty``.

    Raises ``ExamNotAvailable`` rather than returning a short exam. See the class docstring:
    a short exam produces a number that walks straight into a TR eligibility decision.
    """
    size = max(EXAM_MIN_ITEMS, min(int(size), EXAM_MAX_ITEMS))
    pool = [it for it in store.list_calibration_items(specialty=specialty, active_only=True)
            if admissible(it.get("key") or {})[0]]
    if len(pool) < size:
        raise ExamNotAvailable(
            f"{specialty}: {len(pool)} admissible keyed items, need {size}. "
            "Key more items with a reference panel of at least "
            f"{PANEL_MIN} independent judgments before opening the exam.")
    import random as _random
    rng = _random.Random(seed) if seed is not None else _random.Random()
    chosen = rng.sample(pool, size)
    return {
        "specialty": specialty,
        "size": size,
        # The candidate NEVER receives the key. This is the one place a leak would be silent
        # and total — a candidate who can see better_answer_id scores 1.00 and the whole
        # feature becomes noise pointed at the admin queue.
        "items": [blind_item(it) for it in chosen],
        "item_ids": [it["item_id"] for it in chosen],
    }


def blind_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """The candidate-facing shape. Whitelist, not blacklist: a new key field added upstream
    must fail to appear here rather than leak by default."""
    v = item.get("vignette") or {}
    return {
        "item_id": item.get("item_id"),
        "specialty": item.get("specialty"),
        "stem": v.get("stem"),
        "answers": [{"id": a.get("id"), "text": a.get("text")}
                    for a in (v.get("answers") or [])],
        "steps": [{"id": s.get("id"), "text": s.get("text")}
                  for s in (v.get("steps") or [])],
        "criteria": list(v.get("criteria") or []),
    }


def rescore_attempt(store: Any, attempt_id: str) -> Dict[str, Any]:
    """Recompute a stored attempt against TODAY's keys, from the raw responses.

    This is why raw responses are persisted. Re-keying an item — because the panel grew, or
    because the item turned out to be ambiguous — otherwise means re-testing every physician
    who already sat it, which in practice means never re-keying at all and living with a
    known-bad item in a gate.
    """
    attempt = store.get_calibration_attempt(attempt_id)
    if not attempt:
        raise KeyError(attempt_id)
    responses = attempt.get("responses") or {}
    items = store.get_calibration_items(list(responses.keys()))
    result = grade(items, responses)
    store.record_calibration_score(attempt_id, result, rescored=True)
    log.info("[calibration] rescored %s: composite %s -> %s", attempt_id,
             attempt.get("composite"), result.get("composite"))
    return result


def build_item_from_task(task: Dict[str, Any], judgments: Sequence[Dict[str, Any]]
                         ) -> Optional[Dict[str, Any]]:
    """Turn a real task plus its reference-panel judgments into a calibration item.

    Sourcing from ``tasks`` is rule 2 in the module docstring: the exam has to be drawn from
    the actual task distribution or it measures a different skill from the one the job needs.
    Returns None when the panel is not admissible, so a caller can sweep the task table and
    keep only what can legitimately key an exam.
    """
    key = aggregate_panel(judgments)
    if not key.get("admissible"):
        return None
    answers = [{"id": c.get("id"), "text": c.get("text") or ""}
               for c in (task.get("candidate_answers") or [])]
    if len(answers) < 2:
        return None
    steps = []
    for src in (task.get("generation") or {}, task.get("case") or {}):
        for i, st in enumerate(((src or {}).get("reasoning_steps") or [])):
            steps.append({"id": st.get("id") or f"step_{i + 1}",
                          "text": st.get("text") or st.get("claim") or ""})
        if steps:
            break
    return {
        "specialty": (task.get("specialty") or "").strip().lower(),
        "source_task_id": task.get("task_id"),
        "vignette": {
            "stem": task.get("prompt") or "",
            "answers": answers,
            "steps": steps,
            "criteria": sorted({str(c) for j in judgments
                                for c in (j.get("load_bearing") or []) if str(c).strip()}),
        },
        "key": key,
    }
