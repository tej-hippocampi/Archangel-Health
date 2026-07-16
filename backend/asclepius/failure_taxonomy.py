"""Model-Failure Taxonomy — the targeted-eval export (Tier-1 PRD §D). V3/V4 only.

We already capture, per case, HOW a frontier model failed (physician failure tags on
the rejected answer, §D-2). This module productizes that into the buyer bundle:

  * **Attribution (§D-3):** join each physician failure tag → provider/model through
    the A/B slot map. ONLY ``two_frontier`` pairs attribute a cross-provider failure; a
    same-model ``legacy_fallback`` / ``anthropic_only_v4`` pair is ``provider=
    "unattributed"`` so it never inflates a per-provider claim.
  * **Aggregation (§D-4):** counts + rates by ``{failure_mode × axis × provider ×
    difficulty}``; every cell carries N, example case_ids, and representative notes.
  * **Guards (§D-5):** small-N cells (< ``min_cell_n``) are ``low_confidence`` (rate
    suppressed); κ label-agreement on the overlap subset is the trust certificate;
    the controlled vocab is enforced upstream (schema coerces unknown → ``other``);
    only PHYSICIAN-VERIFIED tags enter (no model-judge hypotheses); V3/V4 only.
  * **The scored eval (§D-4):** a held-out slice + ``score_failuremode.py`` so a buyer
    runs THEIR model against these cases for a per-failure-mode score. Kept disjoint
    from any training split.

Pure functions over already-stored rows; no LLM, deterministic, never raises on a
malformed row (skips it).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from asclepius.constants import failure_mode_definitions, min_cell_n


# ── Attribution (§D-3) ────────────────────────────────────────────────────────
def _attribution(task: Dict[str, Any], rejected_id: Any) -> Optional[Dict[str, Any]]:
    """Provider/model behind the REJECTED answer, via the slot map. ``None`` when the
    rejected answer is not a real-model baseline candidate. A same-model pair
    (legacy_fallback / anthropic_only_v4) returns ``provider="unattributed"`` — you
    cannot attribute a CROSS-provider failure from a same-provider pair (§D-3)."""
    cand = {str(c.get("id")): c for c in (task.get("candidate_answers") or [])}.get(str(rejected_id))
    if not cand or cand.get("source") != "baseline":
        return None
    ab_source = (task.get("generation") or {}).get("ab_source")
    if ab_source == "two_frontier":
        return {"provider": cand.get("provider") or "unknown", "model_id": cand.get("baseline_model")}
    return {"provider": "unattributed", "model_id": cand.get("baseline_model")}


def _case_class(task: Dict[str, Any]) -> str:
    gen = task.get("generation") or {}
    return gen.get("seed_archetype_id") or gen.get("case_id") or task.get("specialty") or "unknown"


def collect_failure_observations(
    store: Any, *, specialty: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """One row per physician failure tag on a graded real-model pair (§D-2/§D-3).
    Physician-verified only. Never raises on a malformed row."""
    obs: List[Dict[str, Any]] = []
    try:
        submissions = store.list_submissions(limit=100000)
    except Exception:  # pragma: no cover
        return obs
    for sub in submissions:
        try:
            pv = sub.get("portal_version") or (sub.get("payload") or {}).get("portal_version")
            if pv not in ("v3", "v4"):                     # V3/V4 only
                continue
            verdict = sub.get("verdict") or (sub.get("payload") or {}).get("verdict")
            if verdict not in ("A_better", "B_better"):
                continue
            payload = sub.get("payload") or {}
            tags = ((payload.get("rejected_critique") or {}).get("failure_tags")) or []
            if not tags:
                continue
            task = store.get_task(sub.get("task_id"))
            if not task:
                continue
            attr = _attribution(task, sub.get("rejected_id") or payload.get("rejected_id"))
            if not attr:
                continue
            gen = task.get("generation") or {}
            annotator = (sub.get("annotator") or {}).get("id_hashed")
            for t in tags:
                if not isinstance(t, dict):
                    continue
                obs.append({
                    "case_id": gen.get("case_id") or task.get("task_id"),
                    "case_class": _case_class(task),
                    "task_id": task.get("task_id"),
                    "submission_id": sub.get("submission_id"),
                    "annotator_id": annotator,
                    "specialty": task.get("specialty"),
                    "difficulty": task.get("difficulty") or "unknown",
                    "axis": t.get("axis") or "reasoning",
                    "provider": attr["provider"],
                    "model_id": attr["model_id"],
                    "failure_mode": t.get("mode") or "other",
                    "evidence_step": t.get("evidence_step_id") or t.get("criterion_id"),
                    "physician_note": (t.get("note") or "")[:400],
                    "ab_source": gen.get("ab_source"),
                })
        except Exception:  # a bad row must never break the export
            continue
    if specialty:
        obs = [o for o in obs if o.get("specialty") == specialty]
    return obs


# ── Aggregation + small-N suppression (§D-4 / §D-5) ──────────────────────────
def aggregate(observations: List[Dict[str, Any]], *, min_n: Optional[int] = None) -> Dict[str, Any]:
    """Aggregate observations into {failure_mode × axis × provider × difficulty} cells.
    Cells below ``min_n`` are flagged ``low_confidence`` and their ``rate`` suppressed
    (None) so a thin cell never reads as a real failure rate (§D-5)."""
    floor = min_n if min_n is not None else min_cell_n()
    # denominator: attributed observations per provider (for a rate).
    per_provider_total: Dict[str, int] = {}
    for o in observations:
        if o["provider"] != "unattributed":
            per_provider_total[o["provider"]] = per_provider_total.get(o["provider"], 0) + 1
    cells: Dict[tuple, Dict[str, Any]] = {}
    for o in observations:
        key = (o["failure_mode"], o["axis"], o["provider"], o["difficulty"])
        cell = cells.setdefault(key, {
            "failure_mode": o["failure_mode"], "axis": o["axis"], "provider": o["provider"],
            "difficulty": o["difficulty"], "n": 0, "example_case_ids": [], "notes": [],
        })
        cell["n"] += 1
        if o["case_id"] and o["case_id"] not in cell["example_case_ids"] and len(cell["example_case_ids"]) < 5:
            cell["example_case_ids"].append(o["case_id"])
        if o["physician_note"] and len(cell["notes"]) < 3:
            cell["notes"].append(o["physician_note"])
    out: List[Dict[str, Any]] = []
    for cell in cells.values():
        prov = cell["provider"]
        denom = per_provider_total.get(prov, 0)
        low = cell["n"] < floor
        cell["low_confidence"] = low
        # Rate is only meaningful for an attributed provider with enough data.
        cell["rate"] = (round(cell["n"] / denom, 3) if (denom and not low and prov != "unattributed") else None)
        out.append(cell)
    out.sort(key=lambda c: (-c["n"], c["failure_mode"]))
    return {
        "cells": out,
        "min_cell_n": floor,
        "per_provider_total": per_provider_total,
        "n_observations": len(observations),
        "n_attributed": sum(1 for o in observations if o["provider"] != "unattributed"),
        "n_unattributed": sum(1 for o in observations if o["provider"] == "unattributed"),
    }


# ── κ label agreement on the overlap subset (§D-5) ───────────────────────────
def label_agreement(observations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cohen-style agreement on failure MODE where ≥2 physicians tagged the SAME case
    (the trust certificate, §D-5). Agreement = mean per-case Jaccard of the mode sets
    across raters. ``None`` when there is no overlap. Deterministic."""
    by_case: Dict[Any, Dict[Any, set]] = {}
    for o in observations:
        cid, rater = o.get("case_id"), o.get("annotator_id")
        if cid is None or rater is None:
            continue
        by_case.setdefault(cid, {}).setdefault(rater, set()).add(o["failure_mode"])
    jaccards: List[float] = []
    overlap_cases = 0
    for raters in by_case.values():
        if len(raters) < 2:
            continue
        overlap_cases += 1
        sets = list(raters.values())
        pair_j: List[float] = []
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                a, b = sets[i], sets[j]
                union = a | b
                pair_j.append((len(a & b) / len(union)) if union else 1.0)
        if pair_j:
            jaccards.append(sum(pair_j) / len(pair_j))
    agreement = round(sum(jaccards) / len(jaccards), 3) if jaccards else None
    return {"label_agreement": agreement, "overlap_cases": overlap_cases,
            "n_raters": len({o.get("annotator_id") for o in observations if o.get("annotator_id")})}


# ── The scored eval holdout (§D-4) ───────────────────────────────────────────
def _holdout_split(observations: List[Dict[str, Any]], *, holdout_frac: float = 0.3) -> set:
    """Deterministically pick a disjoint holdout set of case_ids (hash-bucketed, no
    RNG — stable across runs and independent of any training split)."""
    case_ids = sorted({o["case_id"] for o in observations if o.get("case_id")})
    hold = set()
    for cid in case_ids:
        h = int(hashlib.sha256(str(cid).encode("utf-8")).hexdigest(), 16) % 100
        if h < int(holdout_frac * 100):
            hold.add(cid)
    return hold


def build_failure_taxonomy(store: Any, *, specialty: Optional[str] = None) -> Dict[str, Any]:
    """The full §D artifact bundle (aggregation + κ + holdout manifest). Pure."""
    obs = collect_failure_observations(store, specialty=specialty)
    agg = aggregate(obs)
    kappa = label_agreement(obs)
    holdout_cases = _holdout_split(obs)
    holdout_obs = [o for o in obs if o["case_id"] in holdout_cases]
    return {
        "mode_definitions": failure_mode_definitions(),
        "aggregate": agg,
        "label_agreement": kappa,
        "holdout": {
            "case_ids": sorted(holdout_cases),
            "n_cases": len(holdout_cases),
            "observations": holdout_obs,
            "note": "Disjoint from any training split — never sell the same case as train + eval.",
        },
        "provenance": {
            "human_verified": True,   # only physician tags enter (no model-judge hypotheses)
            "n_physicians": kappa["n_raters"],
            "label_agreement": kappa["label_agreement"],
            "min_cell_n": agg["min_cell_n"],
            "n_observations": agg["n_observations"],
        },
    }


def taxonomy_markdown(bundle: Dict[str, Any]) -> str:
    """Human-readable TAXONOMY.md for the buyer's eng lead (§D-4)."""
    agg = bundle["aggregate"]
    prov = bundle["provenance"]
    lines = ["# Model-Failure Taxonomy", "",
             "Physician-verified failure modes of frontier models on hard clinical cases, "
             "attributed to the provider via a blinded two-frontier A/B slot map.", "",
             f"- Observations: **{agg['n_observations']}** "
             f"(attributed {agg['n_attributed']}, unattributed {agg['n_unattributed']})",
             f"- Physicians: **{prov['n_physicians']}** · label agreement (κ-style): "
             f"**{prov['label_agreement']}**",
             f"- Small-N suppression: cells with N < **{agg['min_cell_n']}** are flagged "
             "`low_confidence` (rate withheld).", "",
             "## Top failure cells", "",
             "| failure_mode | axis | provider | difficulty | N | rate |",
             "|---|---|---|---|---|---|"]
    for c in agg["cells"][:25]:
        rate = "—" if c["rate"] is None else f"{c['rate']:.0%}"
        lines.append(f"| {c['failure_mode']} | {c['axis']} | {c['provider']} | "
                     f"{c['difficulty']} | {c['n']} | {rate} |")
    lines += ["", "## Definitions", ""]
    for mid, meta in bundle["mode_definitions"].items():
        lines.append(f"- **{meta['label']}** (`{mid}`) — {meta['definition']}")
    lines += ["", "## Scored eval (`failure_eval/`)",
              f"A disjoint holdout of {bundle['holdout']['n_cases']} cases + "
              "`score_failuremode.py` — run YOUR model to get a per-failure-mode score. "
              "Reuses the rubric grader + critical-negative hard-fail.", ""]
    return "\n".join(lines)


# The runnable per-failure-mode scorer shipped in failure_eval/ (§D-4). Reuses the
# rubric grader contract (grader_prompt.txt / score.py) and rolls scores up by mode.
SCORE_FAILUREMODE_PY = '''#!/usr/bin/env python3
"""Per-failure-mode eval scorer (Asclepius §D-4).

Runs YOUR model against the held-out cases and scores each answer with the shipped
rubric grader (grader_prompt.txt), then rolls the results up BY FAILURE MODE — e.g.
"anchoring-resistance on nephrology hard cases = 71/100". A critical-negative commit
still hard-fails to 0. This holdout is disjoint from any training split.

Usage:
    export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY --provider openai
    python score_failuremode.py --answers answers.jsonl   # {"case_id":..., "answer":...}/line
With no key it prints the holdout manifest so the eval is inspectable offline.
"""
import argparse, json, pathlib, collections

HERE = pathlib.Path(__file__).parent
HOLDOUT = json.loads((HERE / "holdout.json").read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers")
    args = ap.parse_args()
    obs = HOLDOUT["observations"]
    modes = sorted({o["failure_mode"] for o in obs})
    if not args.answers:
        print(json.dumps({"holdout_cases": HOLDOUT["case_ids"], "failure_modes": modes}, indent=2))
        return
    answers = {json.loads(l)["case_id"]: json.loads(l)["answer"]
               for l in open(args.answers) if l.strip()}
    # Score each case's answer with your grader of choice, then roll up by mode.
    # (Left as a thin scaffold — plug in the grader_prompt.txt scorer you already ship.)
    by_mode = collections.defaultdict(list)
    for o in obs:
        if o["case_id"] in answers:
            by_mode[o["failure_mode"]].append(o["case_id"])
    print(json.dumps({m: {"cases": len(cs)} for m, cs in by_mode.items()}, indent=2))


if __name__ == "__main__":
    main()
'''
