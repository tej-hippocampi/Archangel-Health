"""Asclepius Seedmaker — nephrology auto-generation engine (PRD §7).

A single orchestrator, :func:`generate_tasks`, manufactures high-value training
tasks with no human prompt authoring, grounded in the curated seed corpus:

  1. prompt generation  — few-shot from the corpus, round-robin across buckets
  2. candidate-answer    — one strong + one plausibly-flawed answer (server-side
                           ``intended_flawed_id`` kept off the blinded screen)
  3. quality / error-likelihood judge — error_likelihood, revision_value,
                           on_specialty, safety_ok
  4. novelty / contamination / scope gates (reused from ``validation``)

Accepted items land as ordinary tasks (``source="internal_prompt_bank"``) with a
full ``generation`` provenance block, so the proven eval -> QA -> export pipeline
is unchanged. Generation NEVER emits ungated synthetic tasks: with no LLM key it
raises :class:`GenerationDisabled` (PRD §7.3), and it never auto-stamps grounding.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

from asclepius import corpus
from asclepius.constants import (
    ASCLEPIUS_CONFIG_VERSION,
    ASCLEPIUS_ENGINE,
    GENERATION_NEAR_DUP_JACCARD,
    gen_fewshot_k,
    gen_max_attempts_per_task,
    gen_min_error_likelihood,
    gen_min_revision_value,
)
from asclepius.critic import generate_candidates_ex, run_prompt_gen, run_prompt_judge
from asclepius.specialties import get_specialty_config
from asclepius.validation import contamination_hits

log = logging.getLogger("asclepius.generation")

# Difficulty ordering for min_difficulty enforcement + difficulty_mix steering.
_DIFFICULTY_RANK = {"easy": 0, "medium": 1, "hard": 2}


class GenerationDisabled(RuntimeError):
    """Raised when generation cannot run safely (no LLM configured). We never
    emit ungated synthetic tasks, so the router maps this to a clear 503."""


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _prompt_hash(text: Optional[str]) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def _token_set(text: Optional[str]) -> frozenset:
    """Normalized word-token set for fuzzy near-duplicate detection (PRD §7.4)."""
    return frozenset(re.findall(r"[a-z0-9]+", _norm(text)))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def _existing_prompt_hashes(store: Any, specialty: str) -> set:
    """Hashes of every seed prompt + every prior internal-bank task prompt, for
    novelty/dedupe (PRD §7.4)."""
    hashes = {_prompt_hash(p) for p in corpus.all_prompts(specialty)}
    for t in store.list_tasks(specialty=specialty, limit=100000):
        if t.get("source") == "internal_prompt_bank":
            hashes.add(_prompt_hash(t.get("prompt")))
    return hashes


def _existing_token_sets(store: Any, specialty: str) -> List[frozenset]:
    """Token sets of every seed + prior internal-bank prompt for fuzzy dedupe."""
    sets = [_token_set(p) for p in corpus.all_prompts(specialty)]
    for t in store.list_tasks(specialty=specialty, limit=100000):
        if t.get("source") == "internal_prompt_bank":
            sets.append(_token_set(t.get("prompt")))
    return [s for s in sets if s]


def _difficulty_quota(n: int, mix: Optional[Dict[str, float]]) -> Optional[Dict[str, int]]:
    """Turn a normalized ``difficulty_mix`` (weights over easy/medium/hard) into
    integer per-difficulty target counts summing to ``n``. Returns ``None`` when
    no usable mix is supplied (keeps the legacy free-choice behavior)."""
    if not mix:
        return None
    weights = {d: float(mix.get(d, 0) or 0) for d in _DIFFICULTY_RANK if (mix.get(d, 0) or 0) > 0}
    total = sum(weights.values())
    if not weights or total <= 0 or n <= 0:
        return None
    # Largest-remainder apportionment so the quotas sum exactly to n.
    raw = {d: (w / total) * n for d, w in weights.items()}
    quota = {d: int(v) for d, v in raw.items()}
    remainder = n - sum(quota.values())
    for d, _ in sorted(raw.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True):
        if remainder <= 0:
            break
        quota[d] += 1
        remainder -= 1
    return quota


def _bucket_order(cfg: Any) -> List[Any]:
    """Round-robin order weighted by each bucket's target_count so a batch covers
    the spectrum and cannot collapse onto one topic (PRD §7.1)."""
    buckets = list(cfg.taxonomy)
    # Stable round-robin is sufficient; weight by repeating high-target buckets.
    weighted: List[Any] = []
    if buckets:
        base = min((b.target_count or 1) for b in buckets) or 1
        for b in buckets:
            reps = max(1, round((b.target_count or base) / base))
            weighted.extend([b] * reps)
    return weighted or buckets


async def generate_tasks(
    store: Any,
    *,
    specialty: str,
    n: int,
    difficulty_mix: Optional[Dict[str, float]] = None,
    capture_reasoning: bool = False,
    grounding_mode: str = "optional",
    max_labels: int = 1,
    buyer_request_id: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate up to ``n`` validated nephrology tasks. Returns
    ``{job_id, created, accepted, dropped, shortfall, corpus_version}``.

    Raises :class:`SpecialtyNotEnabled` (unknown/disabled specialty) and
    :class:`GenerationDisabled` (no LLM available — never emit ungated tasks)."""
    cfg = get_specialty_config(specialty)  # raises SpecialtyNotEnabled
    meta = corpus.corpus_metadata(specialty)
    n = max(0, int(n or 0))

    created: List[str] = []
    dropped: Counter = Counter()
    seen = _existing_prompt_hashes(store, specialty)
    seen_token_sets = _existing_token_sets(store, specialty)
    ratified = bool(meta.get("ratified"))

    min_err = gen_min_error_likelihood()
    min_rev = gen_min_revision_value()
    k = gen_fewshot_k()
    max_calls = max(1, n * gen_max_attempts_per_task())

    order = _bucket_order(cfg)
    gm = grounding_mode if grounding_mode in ("optional", "required") else "optional"

    # difficulty_mix -> integer per-difficulty quotas (None == legacy free choice).
    quota = _difficulty_quota(n, difficulty_mix)
    remaining: Dict[str, int] = dict(quota) if quota else {}

    calls = 0
    idx = 0
    llm_seen_working = False

    while len(created) < n and calls < max_calls and order:
        bucket = order[idx % len(order)]
        idx += 1
        calls += 1
        floor = bucket.min_difficulty if bucket.min_difficulty in _DIFFICULTY_RANK else "easy"

        # Steer the requested difficulty toward the largest remaining quota,
        # clamped up to this bucket's min_difficulty floor (PRD §7.1, fixes P2-B).
        target_difficulty: Optional[str] = None
        if quota:
            avail = {d: r for d, r in remaining.items() if r > 0}
            if avail:
                target_difficulty = max(avail, key=avail.get)
                if _DIFFICULTY_RANK[target_difficulty] < _DIFFICULTY_RANK[floor]:
                    target_difficulty = floor

        exemplars = corpus.sample_exemplars(specialty, bucket.id, k)
        failure_modes = [e.get("ai_failure_mode") for e in exemplars]
        want = min(3, n - len(created))
        pg = await run_prompt_gen(
            specialty=specialty,
            bucket_id=bucket.id,
            bucket_label=bucket.label,
            exemplars=exemplars,
            failure_modes=failure_modes,
            n=max(1, want),
            difficulty=target_difficulty,
        )
        if pg.get("skipped"):
            # No LLM. If we have produced nothing at all, generation is disabled
            # (never emit ungated synthetic tasks). Otherwise stop early.
            if not llm_seen_working and not created:
                raise GenerationDisabled(
                    "Auto-generation is disabled: no LLM is configured (set "
                    "ANTHROPIC_API_KEY). The system will not emit ungated "
                    "synthetic tasks."
                )
            break
        llm_seen_working = True
        pg_model = pg.get("model")

        for p in pg.get("prompts") or []:
            if len(created) >= n:
                break
            prompt = (p.get("prompt") or "").strip()
            if not prompt:
                dropped["empty_prompt"] += 1
                continue

            # Gate 1: contamination (lifted from a public benchmark).
            if contamination_hits(prompt):
                dropped["contamination"] += 1
                continue
            # Gate 2: novelty / dedupe vs seeds + prior generations (exact hash).
            ph = _prompt_hash(prompt)
            if ph in seen:
                dropped["duplicate"] += 1
                continue
            # Gate 2b: fuzzy near-duplicate (token-set Jaccard) on hash survivors.
            ts = _token_set(prompt)
            if ts and any(_jaccard(ts, s) >= GENERATION_NEAR_DUP_JACCARD for s in seen_token_sets):
                dropped["near_duplicate"] += 1
                continue

            # Gate 3: difficulty floor — drop prompts easier than the bucket's
            # min_difficulty BEFORE spending candidate-gen/judge budget (P2-A).
            raw_diff = p.get("difficulty")
            difficulty = raw_diff if raw_diff in _DIFFICULTY_RANK else "hard"
            if _DIFFICULTY_RANK[difficulty] < _DIFFICULTY_RANK[floor]:
                dropped["below_min_difficulty"] += 1
                continue
            # Gate 4: difficulty_mix steering — if this difficulty's quota is spent
            # while another difficulty still has room, defer it (soft filter, P2-B).
            if quota:
                if remaining.get(difficulty, 0) <= 0 and any(r > 0 for r in remaining.values()):
                    dropped["difficulty_mix_skew"] += 1
                    continue

            # Stage 2: candidate answers (strong + intended-flawed).
            cg = await generate_candidates_ex(
                prompt, specialty=specialty, ai_failure_mode=p.get("ai_failure_mode")
            )
            candidates = cg.get("candidates") or []
            if len(candidates) < 2:
                dropped["candidate_gen_failed"] += 1
                continue

            # Stage 3: quality / error-likelihood judge.
            judge = await run_prompt_judge(prompt, candidates)
            if judge.get("skipped"):
                dropped["judge_failed"] += 1
                continue
            if not judge.get("safety_ok", True):
                dropped["unsafe"] += 1
                continue
            if not judge.get("on_specialty", True):
                dropped["off_specialty"] += 1
                continue
            el = judge.get("error_likelihood")
            rv = judge.get("revision_value")
            if el is None or el < min_err:
                dropped["low_error_likelihood"] += 1
                continue
            if rv is None or rv < min_rev:
                dropped["low_revision_value"] += 1
                continue

            # Accept: stamp provenance + insert as an ordinary internal-bank task.
            generation = {
                "engine": ASCLEPIUS_ENGINE,
                "specialty": specialty,
                "seed_corpus_version": meta["version"],
                # Provenance honesty (PRD §9, fixes P1-C): records carry whether the
                # seed corpus that drove generation is clinician-ratified yet.
                "seed_corpus_ratified": ratified,
                "seed_exemplars": [e.get("seed_id") for e in exemplars],
                "taxonomy_bucket": bucket.id,
                "prompt_gen_model": pg_model,
                "candidate_gen_model": cg.get("model"),
                "judge": {
                    "error_likelihood": el,
                    "revision_value": rv,
                    "explanation": judge.get("explanation", ""),
                },
                "config_version": ASCLEPIUS_CONFIG_VERSION,
                "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                # server-side only; stripped from the blinded eval screen (PRD §7.2, §16)
                "intended_flawed_id": cg.get("intended_flawed_id"),
            }
            cap = bool(capture_reasoning) or bool(p.get("capture_reasoning_recommended"))
            task = store.insert_task(
                prompt=prompt,
                specialty=specialty,
                difficulty=difficulty,
                capture_reasoning=cap,
                source="internal_prompt_bank",
                candidate_answers=candidates,
                max_labels=max(1, int(max_labels or 1)),
                grounding_mode=gm,
                buyer_request_id=buyer_request_id,
                created_by=created_by,
                generation=generation,
            )
            created.append(task["task_id"])
            seen.add(ph)
            seen_token_sets.append(ts)
            if quota and difficulty in remaining and remaining[difficulty] > 0:
                remaining[difficulty] -= 1

    params = {
        "n": n,
        "difficulty_mix": difficulty_mix or {},
        "difficulty_quota": quota or {},
        "capture_reasoning": bool(capture_reasoning),
        "grounding_mode": gm,
        "buyer_request_id": buyer_request_id,
        "seed_corpus_version": meta["version"],
        "seed_corpus_ratified": ratified,
        "fewshot_k": k,
        "min_error_likelihood": min_err,
        "min_revision_value": min_rev,
    }
    job_id = store.insert_generation_job(
        specialty=specialty,
        requested_n=n,
        accepted=len(created),
        dropped_by_reason=dict(dropped),
        params=params,
        created_by=created_by,
    )
    store.log_event(
        entity_type="generation_job",
        entity_id=job_id,
        event_type="generation_run",
        actor=created_by,
        payload={"accepted": len(created), "dropped": dict(dropped), "requested": n},
    )
    return {
        "job_id": job_id,
        "created": created,
        "accepted": len(created),
        "dropped": dict(dropped),
        "shortfall": max(0, n - len(created)),
        "corpus_version": meta["version"],
        "corpus_ratified": ratified,
    }
