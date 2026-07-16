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
    case_novelty_max,
    gen_fewshot_k,
    gen_max_attempts_per_task,
    gen_min_error_likelihood,
    gen_min_revision_value,
    case_coherence_min,
    case_divergence_min,
    case_ground_truth_min,
    case_mm_necessity_min,
    hard_only_generation,
    hardness_min,
    relax_multimodal_gates,
)
from asclepius.cases import (
    MultimodalContentError,
    assert_multimodal_content,
    render_case_prompt,
)
from asclepius.corpus import failure_domain_names, load_hardness_config
from asclepius.critic import (
    generate_candidates_ex,
    generate_case,
    run_case_judge,
    run_hardness_judge,
    run_prompt_gen,
    run_prompt_judge,
)
from asclepius.specialties import get_specialty_config
from asclepius.validation import contamination_hits, residual_identifiers

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


def _analyte_names(case: Optional[Dict[str, Any]]) -> set:
    """The set of normalized analyte names across every lab panel in a case — the
    'which measurements does this case turn on' fingerprint (Two-Model PRD WS-C)."""
    names: set = set()
    for panel in (case or {}).get("lab_panels") or []:
        for r in panel.get("results") or []:
            a = _norm(r.get("analyte"))
            if a:
                names.add(a)
    return names


def _case_signature_tokens(case: Optional[Dict[str, Any]], question: Optional[str]) -> frozenset:
    """Semantic case signature token-set = normalized(question + ground_truth.answer
    + sorted analyte names) (Two-Model PRD Workstream C). This is the anti-duplication
    fingerprint for MULTIMODAL cases: unlike the rendered prompt (which shares heavy
    panel/note scaffolding across an archetype), the signature is dominated by the
    distinctive question, the correct answer, and the specific measurements the case
    hinges on — so two genuinely different cases from the same archetype do NOT
    collide, while a re-skin of the same clinical decision does."""
    gt = ((case or {}).get("ground_truth") or {}).get("answer") or ""
    analytes = " ".join(sorted(_analyte_names(case)))
    return _token_set(f"{question or ''} {gt} {analytes}")


def _existing_case_signatures(store: Any, specialty: str) -> List[frozenset]:
    """Signature token-sets of every existing multimodal case — the gold seed set
    plus every prior generated/loaded multimodal task — for case-level dedupe."""
    sigs: List[frozenset] = []
    # Gold seed set (authored question + case).
    try:
        from asclepius.gold_cases import GOLD_NEPHROLOGY_CASES
        for entry in GOLD_NEPHROLOGY_CASES:
            if (entry.get("case") or {}).get("specialty", specialty) == specialty:
                sig = _case_signature_tokens(entry.get("case"), entry.get("question"))
                if sig:
                    sigs.append(sig)
    except Exception:  # pragma: no cover - gold set is optional
        pass
    # Prior multimodal tasks (question is stamped into the generation block on insert;
    # legacy rows without it still contribute their answer + analyte fingerprint).
    for t in store.list_tasks(specialty=specialty, limit=100000):
        case = t.get("case")
        if not case:
            continue
        q = (t.get("generation") or {}).get("question")
        sig = _case_signature_tokens(case, q)
        if sig:
            sigs.append(sig)
    return sigs


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


def _multimodal_archetypes(specialty: str) -> List[Dict[str, Any]]:
    """Specialty archetypes that carry a ``multimodal`` block (Synthetic Multimodal
    Cases PRD §10) — the seeds the case generator turns into full clinical cases."""
    arches = load_hardness_config(specialty).get("hard_case_archetypes") or []
    return [a for a in arches if isinstance(a, dict) and a.get("multimodal")]


async def _gen_multimodal_items(
    specialty: str, archetypes: List[Dict[str, Any]], start_idx: int, want: int
) -> Dict[str, Any]:
    """Produce ``run_prompt_gen``-shaped output for the multimodal path: each item
    is a rendered case prompt carrying its structured ``_case`` + ``_question`` +
    the ``hard_hook`` as ``ai_failure_mode`` (so the flawed candidate's error is a
    reasoning-over-data error keyed to the trap). ``skipped`` is True only when NO
    item could be generated (no LLM), matching the run_prompt_gen contract."""
    items: List[Dict[str, Any]] = []
    model = None
    for j in range(max(1, want)):
        arche = archetypes[(start_idx + j) % len(archetypes)]
        cg = await generate_case(arche, specialty=specialty)
        # skipped=True means the LLM is UNAVAILABLE — stop (the caller disables
        # generation only if nothing at all was produced). A returned-but-empty
        # case (skipped=False, case=None) is a per-item parse/schema failure: emit
        # an empty-prompt sentinel so the caller counts it as ``case_gen_failed``
        # and moves on, instead of one bad case aborting the whole batch.
        if cg.get("skipped"):
            break
        model = cg.get("model") or model
        case = cg.get("case")
        question = cg.get("question") or ""
        if not case:
            items.append({"prompt": "", "difficulty": "hard",
                          "_archetype_id": arche.get("topic") or arche.get("id")})
            continue
        prompt = render_case_prompt(case, question)
        hook = case.get("hard_hook") or (arche.get("multimodal") or {}).get("hard_hook")
        items.append({
            "prompt": prompt,
            "difficulty": "hard",
            "ai_failure_mode": hook,
            "_case": case,
            "_question": question,
            "_archetype_id": arche.get("topic") or arche.get("id"),
        })
    return {"prompts": items, "model": model, "skipped": len(items) == 0}


async def generate_tasks(
    store: Any,
    *,
    specialty: str,
    n: int,
    difficulty_mix: Optional[Dict[str, float]] = None,
    capture_reasoning: bool = False,
    grounding_mode: str = "optional",
    independent_mode: str = "stance",
    max_labels: int = 1,
    buyer_request_id: Optional[str] = None,
    multimodal: bool = False,
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
    # Case-level anti-duplication (Two-Model PRD Workstream C): signatures of every
    # existing multimodal case, so a newly generated case that re-skins an existing
    # clinical decision is dropped as ``case_near_duplicate``. Built once per batch.
    seen_case_sigs = _existing_case_signatures(store, specialty) if multimodal else []
    novelty_max = case_novelty_max()
    ratified = bool(meta.get("ratified"))

    min_err = gen_min_error_likelihood()
    min_rev = gen_min_revision_value()
    k = gen_fewshot_k()
    max_calls = max(1, n * gen_max_attempts_per_task())

    order = _bucket_order(cfg)
    gm = grounding_mode if grounding_mode in ("optional", "required") else "optional"

    # Multimodal (Synthetic Multimodal Cases PRD): the prompt-source is the
    # specialty's multimodal archetypes turned into full cases, not run_prompt_gen.
    # With no multimodal archetypes there is nothing to generate — skip the loop.
    mm_archetypes = _multimodal_archetypes(specialty) if multimodal else []
    if multimodal and capture_reasoning is False:
        capture_reasoning = True  # the multimodal value IS the reasoning trace (§4)

    # difficulty_mix -> integer per-difficulty quotas (None == legacy free choice).
    # Multimodal cases are definitionally hard (§4), so they IGNORE difficulty_mix —
    # otherwise a mix without a 'hard' bucket would drop every case as
    # difficulty_mix_skew and silently produce zero tasks.
    quota = _difficulty_quota(n, None if multimodal else difficulty_mix)
    remaining: Dict[str, int] = dict(quota) if quota else {}

    calls = 0
    idx = 0
    llm_seen_working = False

    while len(created) < n and calls < max_calls and order and (not multimodal or mm_archetypes):
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

        want = min(3, n - len(created))
        exemplars: List[Dict[str, Any]] = []
        if multimodal:
            # Archetype-driven case generation feeds the SAME downstream gates.
            pg = await _gen_multimodal_items(specialty, mm_archetypes, idx, want)
        else:
            exemplars = corpus.sample_exemplars(specialty, bucket.id, k)
            failure_modes = [e.get("ai_failure_mode") for e in exemplars]
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
                dropped["empty_prompt" if not multimodal else "case_gen_failed"] += 1
                continue
            # Multimodal: the structured case rides this item; the rendered case IS
            # the prompt (so candidate-gen already conditions on labs + note).
            case = p.get("_case")
            is_mm = case is not None

            # PHI defensive scan on the generated case (PRD §3.1) — synthetic is
            # PHI-free by construction, but drop rather than ship anything the scan
            # flags (never emit a case with a residual identifier).
            if is_mm and residual_identifiers(prompt):
                dropped["case_gen_failed"] += 1
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
            # SKIPPED for multimodal: cases from the same archetype share the panel /
            # note scaffolding (high token overlap) but carry DIFFERENT synthetic values
            # — each is a distinct evaluation. The Jaccard gate is calibrated for text
            # prompts and would collapse a whole archetype to a single case (so V3 runs
            # dry after the first few). Exact-hash dedup above still blocks true repeats.
            ts = _token_set(prompt)
            if ts and not is_mm and any(_jaccard(ts, s) >= GENERATION_NEAR_DUP_JACCARD for s in seen_token_sets):
                dropped["near_duplicate"] += 1
                continue

            # Gate 2c: case-level anti-duplication (Two-Model PRD Workstream C, V3/V4).
            # For MULTIMODAL cases the prompt Jaccard gate above is skipped (shared
            # scaffolding), so novelty is enforced on the semantic case SIGNATURE
            # (question + ground-truth answer + analyte set). Drop as
            # ``case_near_duplicate`` when the signature is >= novelty_max similar to
            # any existing case — before spending candidate-gen / judge budget.
            case_sig = frozenset()
            if is_mm:
                case_sig = _case_signature_tokens(case, p.get("_question"))
                if case_sig and any(_jaccard(case_sig, s) >= novelty_max for s in seen_case_sigs):
                    dropped["case_near_duplicate"] += 1
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
            #
            # This judge is calibrated for TEXT prompts ("how likely is a top model to
            # err on this open question, and how teachable is the correction"). For a
            # MULTIMODAL case it is the WRONG judge — a structured case with a decisive
            # ground-truth answer legitimately scores LOW on error_likelihood, so
            # applying the text floors here silently dropped nearly every case
            # (low_error_likelihood / low_revision_value) even though the case is
            # excellent. A multimodal case's quality is gated by the CASE JUDGE
            # (Stage 3c: coherence, necessity, divergence) instead. So for multimodal
            # items we keep ONLY the universal safety check and skip the text-prompt
            # quality floors. Text generation is unchanged.
            judge = await run_prompt_judge(prompt, candidates)
            # Always defined for the provenance record below (None for multimodal
            # when the text judge is skipped — the case-judge carries the real scores).
            el = judge.get("error_likelihood")
            rv = judge.get("revision_value")
            if judge.get("skipped"):
                if not is_mm:
                    dropped["judge_failed"] += 1
                    continue
            if not judge.get("safety_ok", True):
                dropped["unsafe"] += 1
                continue
            if not is_mm:
                if not judge.get("on_specialty", True):
                    dropped["off_specialty"] += 1
                    continue
                if el is None or el < min_err:
                    dropped["low_error_likelihood"] += 1
                    continue
                if rv is None or rv < min_rev:
                    dropped["low_revision_value"] += 1
                    continue

            # Stage 3b: Hard-Case gate (Seamless PRD WS2). Score how genuinely hard
            # the prompt is; drop below the floor (``below_hardness_floor``) and
            # STAMP the task ``difficulty=hard`` on accept so the hard-case queue is
            # 100% hard. Degrades safely: a skipped hardness judge (no LLM key)
            # leaves difficulty untouched and never drops.
            #
            # ``insert_difficulty`` is separate from ``difficulty`` on purpose: the
            # difficulty_mix quota (Gate 4 + the decrement below) is accounted on
            # the RAW requested ``difficulty`` the gate admitted; only the stored
            # task is promoted to hard. Overwriting ``difficulty`` here would drain
            # the wrong quota bucket and then spuriously drop later prompts as
            # difficulty_mix_skew.
            hardness = None
            insert_difficulty = difficulty
            # Bring-up relaxation (V3 only): for MULTIMODAL cases the strict quality
            # floors can be relaxed so a structurally-complete case is served even if
            # its scores are below floor — the judges still run and scores are
            # recorded. ``relax`` is only ever True for multimodal (case is not None),
            # so the TEXT path below is byte-for-byte unchanged (V2 unaffected).
            relax = case is not None and relax_multimodal_gates()
            # Multimodal cases ALWAYS run the hardness judge (a case is a hard case
            # or it is not a case); text generation runs it only under hard_only.
            if hard_only_generation() or case is not None:
                hj = await run_hardness_judge(
                    prompt, candidates, failure_domains=failure_domain_names(specialty)
                )
                if hj.get("skipped"):
                    # Non-skippable multimodal gate (BUG-1 §4): an ungated case must
                    # never enter the queue — drop rather than pass. For TEXT
                    # generation a skipped hardness judge still degrades safely
                    # (offline generation unaffected; difficulty untouched). Under the
                    # bring-up relaxation a multimodal case is NOT dropped (still hard).
                    if case is not None and not relax:
                        dropped["hardness_unavailable"] += 1
                        continue
                    if case is not None:
                        insert_difficulty = "hard"
                else:
                    hs = hj.get("hardness_score")
                    if (hs is None or hs < hardness_min()) and not relax:
                        dropped["below_hardness_floor"] += 1
                        continue
                    # Passed the floor, OR relaxed through it: multimodal is always
                    # hard; record whatever score we got (incl. a below-floor score
                    # under relaxation, so pass-rates stay measurable).
                    if case is not None:
                        insert_difficulty = "hard"
                    elif hs is not None and hs >= hardness_min():
                        insert_difficulty = "hard"
                    if hs is not None:
                        hardness = {
                            "score": hs,
                            "axes": hj.get("hardness_axes") or [],
                            "min": hardness_min(),
                            "explanation": hj.get("explanation", ""),
                            "judge_model": hj.get("model"),
                            "below_floor": hs < hardness_min(),
                        }

            # Stage 3c: multimodal case gate (Synthetic Multimodal Cases PRD §3.2)
            # — case-specific dimensions ONLY (hardness was judged in 3b). Runs only
            # for multimodal items; degrades safely (a skipped case judge never
            # drops, same contract as run_hardness_judge).
            case_judge = None
            if case is not None:
                # Belt-and-braces content assertion (BUG-1 §2): generate_case
                # already asserts, but re-check here right before insert so a case
                # that lost content on the wire can never be stored as multimodal.
                try:
                    assert_multimodal_content(case)
                except MultimodalContentError:
                    dropped["insufficient_case_content"] += 1
                    continue
                cj = await run_case_judge(case)
                # Non-skippable multimodal gate (BUG-1 §4): a skipped case judge
                # means the case is UNGATED — drop it, never pass. (Ungated
                # synthetic cases must never enter the queue.) Under the bring-up
                # relaxation we do NOT drop on a skipped judge — the case still ships
                # (with no case_judge scores recorded).
                if cj.get("skipped"):
                    if not relax:
                        dropped["case_judge_unavailable"] += 1
                        continue
                else:
                    # The four multimodal QUALITY floors. Relaxed (V3 bring-up): the
                    # scores are still computed and recorded below, but a below-floor
                    # case is NOT dropped. Strict: drop below any floor as before.
                    if not relax:
                        if (cj.get("coherence") or 0.0) < case_coherence_min():
                            dropped["case_incoherent"] += 1
                            continue
                        if (cj.get("ground_truth_determinable") or 0.0) < case_ground_truth_min():
                            dropped["ground_truth_indeterminate"] += 1
                            continue
                        if (cj.get("multimodal_necessity") or 0.0) < case_mm_necessity_min():
                            dropped["multimodal_not_necessary"] += 1
                            continue
                        if (cj.get("reasoning_divergence_potential") or 0.0) < case_divergence_min():
                            dropped["low_reasoning_divergence"] += 1
                            continue
                    case_judge = {
                        k: cj.get(k) for k in (
                            "coherence", "ground_truth_determinable",
                            "multimodal_necessity", "reasoning_divergence_potential")
                    }
                    case_judge["explanation"] = cj.get("explanation", "")
                    case_judge["judge_model"] = cj.get("model")
                # A multimodal item always ships difficulty=hard (the case is the
                # hard case).
                insert_difficulty = "hard"

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
            # Hardness provenance (WS2): a buyer can filter/prove the case is hard.
            if hardness is not None:
                generation["hardness"] = hardness
            # Multimodal provenance (PRD §3.4): case_source + which archetype + the
            # case-judge scores, so exports can filter/prove the modality.
            if case is not None:
                case.setdefault("case_id", "case-" + _prompt_hash(prompt)[:12])
                generation["case_source"] = case.get("case_source", "synthetic")
                generation["case_id"] = case.get("case_id")
                generation["seed_archetype_id"] = p.get("_archetype_id")
                generation["modality"] = "multimodal"
                # Persist the question so future runs can rebuild this case's novelty
                # signature (Two-Model PRD WS-C) without re-parsing the rendered prompt.
                generation["question"] = p.get("_question")
                # Bring-up audit trail: record whether this case was accepted under the
                # relaxed gates (so exports can separate "shipped for demo" from
                # "cleared the full quality bar" once we re-tighten).
                generation["gates_relaxed"] = bool(relax)
                if case_judge is not None:
                    generation["case_judge"] = case_judge
            cap = bool(capture_reasoning) or bool(p.get("capture_reasoning_recommended"))
            task = store.insert_task(
                prompt=prompt,
                specialty=specialty,
                difficulty=insert_difficulty,
                capture_reasoning=cap,
                source="internal_prompt_bank",
                candidate_answers=candidates,
                max_labels=max(1, int(max_labels or 1)),
                grounding_mode=gm,
                modality=("multimodal" if case is not None else "text"),
                case=case,
                independent_mode=independent_mode,
                buyer_request_id=buyer_request_id,
                created_by=created_by,
                generation=generation,
            )
            created.append(task["task_id"])
            seen.add(ph)
            seen_token_sets.append(ts)
            # Within-batch case dedupe: a later case cannot re-skin this one.
            if case_sig:
                seen_case_sigs.append(case_sig)
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
