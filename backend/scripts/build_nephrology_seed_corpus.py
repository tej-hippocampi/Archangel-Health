#!/usr/bin/env python3
"""Reproducible scaffold for the nephrology seed corpus (PRD §5.4).

The COMMITTED artifact is the human-ratification target
(``backend/asclepius/seed_corpus/nephrology.v1.json``). This script is
reproducible scaffolding, NOT an autopilot — it never marks a corpus
``ratified``. It supports two modes:

  validate   (default) — load + schema-validate the committed corpus and print a
                         per-bucket coverage report. Runs offline, no API key.
                         Exits non-zero if the corpus is invalid (CI-friendly).

  expand     — when ANTHROPIC_API_KEY is set, draft NEW candidate items per
               under-filled taxonomy bucket via the prompt-gen model, run each
               through the error-likelihood judge (hardness filter, PRD §5.4 step
               3) + contamination/dedupe gates, and APPEND survivors to an output
               file with ``ratified: false`` for clinician review. Never edits the
               committed corpus in place unless --out points at it.

Usage:
    python3 scripts/build_nephrology_seed_corpus.py                 # validate
    python3 scripts/build_nephrology_seed_corpus.py --report
    python3 scripts/build_nephrology_seed_corpus.py expand --per-bucket 3 \
        --out /tmp/nephrology.v2.draft.json

Run from the ``backend/`` directory (so the ``asclepius`` package is importable).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius import corpus as asc_corpus  # noqa: E402
from asclepius.constants import (  # noqa: E402
    gen_min_error_likelihood,
    gen_min_revision_value,
)
from asclepius.specialties import get_specialty_config  # noqa: E402
from asclepius.validation import contamination_hits  # noqa: E402

SPECIALTY = "nephrology"


def cmd_validate(report: bool) -> int:
    try:
        meta = asc_corpus.corpus_metadata(SPECIALTY)
    except asc_corpus.CorpusError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {meta['version']} — {meta['total']} items — ratified={meta['ratified']} "
          f"({meta['review_status']})")
    if report:
        print("\nCoverage by bucket (have / target, min difficulty):")
        for b in meta["taxonomy"]:
            print(f"  {b['id']:<26} {b['have']:>3} / {b['target_count']:<3}  (min {b['min_difficulty']})")
        print("\nBy difficulty:", meta["by_difficulty"])
    return 0


async def _draft_for_bucket(bucket, per_bucket: int):
    from asclepius.critic import (
        generate_candidates_ex,
        run_prompt_gen,
        run_prompt_judge,
    )

    exemplars = asc_corpus.sample_exemplars(SPECIALTY, bucket.id, 6)
    pg = await run_prompt_gen(
        specialty=SPECIALTY,
        bucket_id=bucket.id,
        bucket_label=bucket.label,
        exemplars=exemplars,
        failure_modes=[e.get("ai_failure_mode") for e in exemplars],
        n=per_bucket,
    )
    if pg.get("skipped"):
        return []
    survivors = []
    existing = {asc_corpus._norm(p) if hasattr(asc_corpus, "_norm") else p.strip().lower()
                for p in asc_corpus.all_prompts(SPECIALTY)}
    min_err, min_rev = gen_min_error_likelihood(), gen_min_revision_value()
    for p in pg.get("prompts") or []:
        prompt = (p.get("prompt") or "").strip()
        if not prompt or contamination_hits(prompt) or prompt.lower() in existing:
            continue
        cg = await generate_candidates_ex(prompt, specialty=SPECIALTY,
                                          ai_failure_mode=p.get("ai_failure_mode"))
        if len(cg.get("candidates") or []) < 2:
            continue
        judge = await run_prompt_judge(prompt, cg["candidates"])
        if judge.get("skipped") or not judge.get("safety_ok") or not judge.get("on_specialty"):
            continue
        if (judge.get("error_likelihood") or 0) < min_err or (judge.get("revision_value") or 0) < min_rev:
            continue
        survivors.append({
            "seed_id": f"neph-seed-draft-{bucket.id}-{len(survivors)+1}",
            "specialty": SPECIALTY,
            "topic": bucket.id,
            "subtopic": p.get("subtopic") or bucket.id,
            "difficulty": p.get("difficulty") if p.get("difficulty") in ("easy", "medium", "hard") else "hard",
            "prompt": prompt,
            "ai_failure_mode": p.get("ai_failure_mode") or "",
            "why_high_value": judge.get("explanation") or "passed error-likelihood + revision-value gate",
            "reference_basis": "AI-drafted; clinician to supply concept source on ratification",
            "reference_type": "expert_consensus",
            "capture_reasoning_recommended": bool(p.get("capture_reasoning_recommended")),
            "tags": ["ai_drafted", "pending_ratification"],
        })
    return survivors


def cmd_expand(per_bucket: int, out: str) -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("expand requires ANTHROPIC_API_KEY (LLM drafting). Aborting.", file=sys.stderr)
        return 2
    cfg = get_specialty_config(SPECIALTY)

    async def _run():
        drafted = []
        for bucket in cfg.taxonomy:
            items = await _draft_for_bucket(bucket, per_bucket)
            print(f"  {bucket.id}: drafted {len(items)} item(s) past the gates")
            drafted.extend(items)
        return drafted

    drafted = asyncio.run(_run())
    if not drafted:
        print("No items survived the hardness/contamination/dedupe gates.")
        return 0
    payload = {
        "version": "nephrology.v-draft",
        "specialty": SPECIALTY,
        "ratified": False,
        "review_status": "ai_drafted_pending_clinician_review",
        "generated_by": "build_nephrology_seed_corpus.expand",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "items": drafted,
    }
    Path(out).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(drafted)} drafted (UNRATIFIED) item(s) to {out}")
    print("Next: a nephrologist reviews/edits/approves before these are merged into a versioned corpus.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Nephrology seed corpus build scaffold")
    sub = parser.add_subparsers(dest="command")
    parser.add_argument("--report", action="store_true", help="print coverage report")
    exp = sub.add_parser("expand", help="LLM-draft new items per under-filled bucket (needs API key)")
    exp.add_argument("--per-bucket", type=int, default=2)
    exp.add_argument("--out", default="/tmp/nephrology.v-draft.json")
    sub.add_parser("validate", help="validate the committed corpus (default)")
    args = parser.parse_args()

    if args.command == "expand":
        return cmd_expand(args.per_bucket, args.out)
    return cmd_validate(report=args.report)


if __name__ == "__main__":
    raise SystemExit(main())
