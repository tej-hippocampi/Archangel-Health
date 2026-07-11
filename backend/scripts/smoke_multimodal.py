"""Live end-to-end smoke test for multimodal case generation (Multimodal Debug
PRD P1.4–P1.6). The unit suite stubs the LLM, so THIS is the first time the live
path runs — run it once on any machine with ANTHROPIC_API_KEY before trusting a
deployment.

    cd backend && python scripts/smoke_multimodal.py [--specialty nephrology] [--n 3]

What it does (isolated by default — a TEMP database, your real data untouched):
  1. runs the real pipeline: archetype → case-gen LLM → contamination/dedupe →
     hardness judge → case judge (Stage 3c) → stored task,
  2. prints accepted / dropped-by-reason (the PRD's tuning signal: a 0-accepted
     batch with `case_incoherent`/`multimodal_not_necessary` drops means the
     env floors need loosening, not that the pipeline is broken),
  3. verifies every created task: modality=multimodal, structured case with lab
     panels + note, difficulty=hard, capture_reasoning on, rendered prompt embeds
     the labs, and the evaluator-facing blind view strips the answer key,
  4. prints one rendered case prompt so you can eyeball clinical quality.

Exit 0 = accepted > 0 and every check passed. Exit 1 otherwise (with the reason).
Use --db-keep to run against the REAL configured ASCLEPIUS_DB_PATH instead (the
generated tasks then stay in the queue — handy to seed a demo).

Floors are env-tunable if yield is low (PRD P1.6):
  ASCLEPIUS_CASE_COHERENCE_MIN (0.8)   ASCLEPIUS_CASE_MM_NECESSITY_MIN (0.7)
  ASCLEPIUS_CASE_GROUND_TRUTH_MIN (0.7) ASCLEPIUS_CASE_DIVERGENCE_MIN (0.5)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="Live multimodal generation smoke test")
    ap.add_argument("--specialty", default="nephrology")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--db-keep", action="store_true",
                    help="use the real ASCLEPIUS_DB_PATH (tasks persist) instead of a temp DB")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("FAIL: ANTHROPIC_API_KEY is not set — the live path needs a real LLM key.")
        return 1

    if not args.db_keep:
        os.environ["ASCLEPIUS_DB_PATH"] = os.path.join(
            tempfile.mkdtemp(prefix="asclepius_smoke_"), "smoke.db")
        print(f"[smoke] isolated temp DB: {os.environ['ASCLEPIUS_DB_PATH']}")
    else:
        print("[smoke] --db-keep: writing to the REAL configured Asclepius DB")

    from asclepius.store import get_store
    from asclepius import generation as gen
    from routers.asclepius import _blind_task

    store = get_store()
    print(f"[smoke] generating {args.n} multimodal case(s) for {args.specialty} "
          f"(live LLM — this takes a minute)…")
    try:
        res = asyncio.run(gen.generate_tasks(
            store, specialty=args.specialty, n=args.n, multimodal=True,
            created_by="smoke_multimodal",
        ))
    except gen.GenerationDisabled as exc:
        print(f"FAIL: generation disabled: {exc}")
        return 1

    accepted, dropped = res.get("accepted", 0), res.get("dropped") or {}
    print(f"[smoke] accepted: {accepted} / {args.n}")
    if dropped:
        print("[smoke] dropped by reason:")
        for k, v in sorted(dropped.items(), key=lambda kv: -kv[1]):
            print(f"         {k}: {v}")
    if accepted == 0:
        case_drops = {k: v for k, v in dropped.items() if k in (
            "case_incoherent", "ground_truth_indeterminate",
            "multimodal_not_necessary", "low_reasoning_divergence")}
        if case_drops:
            print("FAIL: 0 accepted, all at the case-judge floors — the pipeline WORKS; "
                  "loosen the env floors (see module docstring) and re-run.")
        else:
            print("FAIL: 0 accepted — see the drop reasons above.")
        return 1

    # Verify every created task end-to-end (PRD acceptance criteria).
    problems: list[str] = []
    tasks = [store.get_task(tid) for tid in res.get("created") or []]
    for t in tasks:
        tid = t["task_id"][:12]
        if t.get("modality") != "multimodal":
            problems.append(f"{tid}: modality={t.get('modality')!r} (want multimodal)")
        case = t.get("case") or {}
        if not case.get("lab_panels"):
            problems.append(f"{tid}: case has no lab_panels")
        if not case.get("notes"):
            problems.append(f"{tid}: case has no notes")
        if t.get("difficulty") != "hard":
            problems.append(f"{tid}: difficulty={t.get('difficulty')!r} (want hard)")
        if not t.get("capture_reasoning"):
            problems.append(f"{tid}: capture_reasoning is off")
        if "CLINICAL CASE" not in (t.get("prompt") or ""):
            problems.append(f"{tid}: rendered prompt does not embed the case")
        blind = _blind_task(t)
        bcase = blind.get("case") or {}
        for leak in ("ground_truth", "hard_hook", "reasoning_divergence"):
            if leak in bcase:
                problems.append(f"{tid}: ANSWER KEY LEAK — {leak!r} in the blind view")
        if "generator_model" in blind or "intended_flawed_id" in str(blind):
            problems.append(f"{tid}: generator provenance leaked to the blind view")

    if problems:
        print("FAIL: task verification problems:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"\n[smoke] all {len(tasks)} task(s) verified: multimodal, hard, reasoning "
          f"capture on, case embedded, answer key stripped from the blind view. ✅")
    print("\n[smoke] sample rendered case prompt (first 1200 chars):\n" + "─" * 60)
    print((tasks[0].get("prompt") or "")[:1200])
    print("─" * 60)
    cj = ((tasks[0].get("generation") or {}).get("case_judge") or {})
    if cj:
        print("[smoke] case-judge scores on the sample: " + ", ".join(
            f"{k}={cj.get(k)}" for k in ("coherence", "ground_truth_determinable",
                                         "multimodal_necessity", "reasoning_divergence_potential")))
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
