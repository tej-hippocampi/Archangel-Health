"""Workstream A — two-frontier (OpenAI ↔ Anthropic) A/B + multi-provider router.

Covers the PRD-A test matrix: provider routing (A-1), a built pair is one OpenAI +
one Anthropic (A-2), 100 pairs converge to a 0.5 slot rate (A-3), one provider
errored → no pair / needs_baseline (A-4), and the server-side provider field never
reaches the client (A-2 blinding). LLM providers are stubbed (no keys needed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from asclepius import baselines as B  # noqa: E402
from ai.model_config import resolve_provider, api_model_id, UnknownProvider  # noqa: E402


# ── A-1: provider resolution ─────────────────────────────────────────────────
def test_provider_resolution():
    assert resolve_provider("gpt-5") == "openai"
    assert resolve_provider("o3-mini") == "openai"
    assert resolve_provider("chatgpt-4o-latest") == "openai"
    assert resolve_provider("openai:gpt-4o") == "openai"
    assert resolve_provider("claude-opus-4-8") == "anthropic"
    assert resolve_provider("anthropic:claude-sonnet-4-6") == "anthropic"
    with pytest.raises(UnknownProvider):
        resolve_provider("mixtral-8x7b")
    assert api_model_id("openai:gpt-5") == "gpt-5"
    assert api_model_id("claude-opus-4-8") == "claude-opus-4-8"


def test_first_text_normalizes_both_shapes():
    from ai import llm_client as c
    # our normalized OpenAI result
    assert c.first_text(c._LLMResult("openai text", 1, 2, "id")) == "openai text"

    # anthropic-shaped (mocked)
    class _Blk:
        type = "text"
        text = "anthropic text"

    class _Resp:
        content = [_Blk()]

    assert c.first_text(_Resp()) == "anthropic text"


def test_openai_reasoning_output_cap_adds_headroom(monkeypatch):
    """Review fix (A#1): OpenAI REASONING models draw hidden reasoning tokens from
    max_output_tokens, so a small answer cap (2000) would be consumed entirely by
    reasoning and return an EMPTY answer — silently zeroing the two-frontier feature.
    The cap must add a reasoning reserve for reasoning ids and leave others alone."""
    from ai import llm_client as c
    monkeypatch.delenv("LLM_OPENAI_REASONING_RESERVE", raising=False)
    # Non-reasoning: unchanged.
    assert c._openai_output_cap(2000, reasoning=False) == 2000
    # Reasoning: answer budget PLUS a generous reserve (default 12000).
    assert c._openai_output_cap(2000, reasoning=True) == 2000 + 12000
    # gpt-5 is classified as reasoning; a plain gpt-4o is not.
    assert c._is_openai_reasoning("gpt-5") and c._is_openai_reasoning("o3-mini")
    assert not c._is_openai_reasoning("gpt-4o")
    # Env override respected.
    monkeypatch.setenv("LLM_OPENAI_REASONING_RESERVE", "5000")
    assert c._openai_output_cap(1000, reasoning=True) == 1000 + 5000


def test_llm_timeout_does_not_retry(monkeypatch):
    """Review fix (A#2): a timeout must NOT re-run an expensive call (2× cost); it
    fails fast so the caller degrades. A non-timeout transient error still retries."""
    import asyncio
    import ai.llm_client as c
    monkeypatch.setattr(c, "_llm_timeout_sec", lambda: 0.05)
    monkeypatch.setattr(c, "resolve_provider", lambda m: "anthropic")

    calls = {"n": 0}

    async def slow_create(kwargs):
        calls["n"] += 1
        await asyncio.sleep(1.0)  # exceeds the 0.05s timeout

    monkeypatch.setattr(c, "_anthropic_create_async", slow_create)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(c.call_llm(role="asclepius_critic", system="s",
                               messages=[{"role": "user", "content": "q"}]))
    assert calls["n"] == 1  # timed out once, NOT retried


# ── A-2 + A-3: the pair is one OpenAI + one Anthropic; slot rate → 0.5 ────────
def test_pair_is_one_per_provider_and_balanced():
    B.reset_ab_state()
    import random
    random.seed(7)
    seen_A_providers = []
    for _ in range(120):
        pair = B.build_baseline_candidates([
            {"response_text": "o", "model": "gpt-5"},
            {"response_text": "a", "model": "claude-opus-4-8"}])
        assert {c["provider"] for c in pair} == {"openai", "anthropic"}   # one each
        assert {c["id"] for c in pair} == {"A", "B"}
        seen_A_providers.append(next(c["provider"] for c in pair if c["id"] == "A"))
    rate = seen_A_providers.count("openai") / len(seen_A_providers)
    assert 0.40 <= rate <= 0.60, rate               # A-3: high slot variability
    assert abs((B.openai_as_A_rate() or 0) - rate) < 1e-9


# ── A-4: one provider errored → no pair (never a gold stand-in) ──────────────
def test_one_provider_errored_yields_no_pair():
    assert B.build_baseline_candidates([
        {"response_text": "", "model": "gpt-5"},                 # errored/empty
        {"response_text": "a", "model": "claude-opus-4-8"}]) == []
    assert B.build_baseline_candidates([
        {"response_text": "a", "model": "claude-opus-4-8"}]) == []


# ── A-2 blinding: provider/baseline_model never reach the client ─────────────
def test_blind_task_strips_provider_and_model():
    from routers.asclepius import _blind_task
    task = {
        "task_id": "t1", "prompt": "q", "modality": "text",
        "candidate_answers": [
            {"id": "A", "text": "openai ans", "source": "baseline", "baseline_model": "gpt-5", "provider": "openai"},
            {"id": "B", "text": "anthropic ans", "source": "baseline", "baseline_model": "claude-opus-4-8", "provider": "anthropic"},
        ],
        "generation": {"baseline_providers": ["openai", "anthropic"]},
    }
    blind = _blind_task(task)
    blob = str(blind)
    assert "provider" not in blob and "gpt-5" not in blob and "claude-opus-4-8" not in blob
    assert "baseline_model" not in blob and "generation" not in blind
    for c in blind["candidate_answers"]:
        assert set(c.keys()) <= {"id", "text"}


# ── Two-frontier default; grade-real-models marks needs_baseline on shortfall ─
def test_run_baselines_stamps_shared_prompt_hash(monkeypatch):
    store = A.fresh_store()
    import ai.llm_client as llm

    class _Blk:
        type = "text"

        def __init__(self, t):
            self.text = t

    class _Resp:
        def __init__(self, t):
            self.content = [_Blk(t)]
            self.usage = type("U", (), {"input_tokens": 5, "output_tokens": 9})()
            self._request_id = "r"

    async def fake_call(*, model, **kw):
        return _Resp(f"answer from {model}"), {"provider": resolve_provider(model), "latency_ms": 3}

    monkeypatch.setattr(llm, "call_llm", fake_call)
    task = {"task_id": "t-mm", "prompt": "A hard renal case with labs."}
    import asyncio
    runs = asyncio.run(B.run_baselines(store, task, models=["gpt-5", "claude-opus-4-8"]))
    assert len(runs) == 2
    hashes = {r["prompt_hash"] for r in runs}
    assert len(hashes) == 1 and next(iter(hashes))          # shared prompt_hash
    assert {r["provider"] for r in runs} == {"openai", "anthropic"}
    pair = B.build_baseline_candidates(runs)
    assert {c["provider"] for c in pair} == {"openai", "anthropic"}


def test_empty_openai_response_recorded_as_error_not_blank_success(monkeypatch):
    """Review fix (Risk 2): a reasoning model that returns an EMPTY answer (output
    budget consumed by reasoning) must be stored as an ERRORED run with an actionable
    message — not a blank 'successful' run with error=None — and the pair must drop to
    []/needs_baseline (never a silent gold stand-in)."""
    store = A.fresh_store()
    import ai.llm_client as llm

    class _Blk:
        type = "text"
        def __init__(self, t): self.text = t

    class _Resp:
        def __init__(self, t):
            self.content = [_Blk(t)]
            self.usage = type("U", (), {"input_tokens": 5, "output_tokens": 9})()
            self._request_id = "r"

    async def fake_call(*, model, **kw):
        # OpenAI returns empty (incomplete), Anthropic returns a real answer.
        text = "" if resolve_provider(model) == "openai" else "real anthropic answer"
        return _Resp(text), {"provider": resolve_provider(model), "latency_ms": 3}

    monkeypatch.setattr(llm, "call_llm", fake_call)
    import asyncio
    runs = asyncio.run(B.run_baselines(store, {"task_id": "t-e", "prompt": "hard case"},
                                       models=["gpt-5", "claude-opus-4-8"]))
    oa = next(r for r in runs if r["provider"] == "openai")
    assert not (oa.get("response_text") or "")            # no blank 'success'
    assert oa.get("error") and "reasoning" in oa["error"].lower()   # actionable signal
    # One provider missing → no pair (caller marks needs_baseline; never a gold fallback).
    assert B.build_baseline_candidates(runs) == []


# ═══════════════════════════════════════════════════════════════════════════════
# PRD §A — the fallback ladder, randomness, blinding, V4 gate + §E cross-cutting
# ═══════════════════════════════════════════════════════════════════════════════

class _Blk:
    type = "text"
    def __init__(self, t): self.text = t

class _Resp:
    def __init__(self, t):
        self.content = [_Blk(t)]
        self.usage = type("U", (), {"input_tokens": 5, "output_tokens": 9})()
        self._request_id = "r"


def _install_llm(monkeypatch, behavior):
    """Stub ai.llm_client.call_llm with a per-model behavior and count calls.
    behavior(model_id) -> ('ok', text) | ('error', msg) | ('empty',)."""
    import ai.llm_client as llm
    calls = {}

    async def fake_call(*, model, **kw):
        calls[model] = calls.get(model, 0) + 1
        outcome = behavior(model)
        kind = outcome[0]
        if kind == "error":
            raise RuntimeError(outcome[1] if len(outcome) > 1 else "boom")
        text = "" if kind == "empty" else outcome[1]
        return _Resp(text), {"provider": resolve_provider(model), "latency_ms": 3}

    monkeypatch.setattr(llm, "call_llm", fake_call)
    return calls


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# ── A1: shared prompt to both providers; divergent pair is discarded ──────────
def test_two_frontier_shared_prompt(monkeypatch):
    store = A.fresh_store()
    _install_llm(monkeypatch, lambda m: ("ok", f"answer {m}"))
    runs = _run(B.run_baselines(store, {"task_id": "t-sp", "prompt": "hard renal case"},
                                models=["gpt-5", "claude-opus-4-8"]))
    assert len({r["prompt_hash"] for r in runs}) == 1          # both share the input hash
    pair = B.build_baseline_candidates(runs)
    assert {c["id"] for c in pair} == {"A", "B"}
    # Mutate one run's prompt_hash → the pair must be DISCARDED (never reach a doctor).
    runs2 = [dict(runs[0]), dict(runs[1])]
    runs2[0]["prompt_hash"] = "deadbeef" * 8
    assert B.build_baseline_candidates(runs2) == []


# ── A2: truly-random slot assignment (balanced but not alternating) ───────────
def test_ab_slot_randomization():
    B.reset_ab_state()
    orientations = []
    for _ in range(200):
        pair = B.build_baseline_candidates([
            {"response_text": "o", "model": "gpt-5", "prompt_hash": "h"},
            {"response_text": "a", "model": "claude-opus-4-8", "prompt_hash": "h"}])
        orientations.append(next(c["provider"] for c in pair if c["id"] == "A"))
    rate = orientations.count("openai") / len(orientations)
    assert 0.42 <= rate <= 0.58, rate                          # balanced ~0.5
    # NOT deterministic alternation: some consecutive orientations must repeat
    # (strict A,B,A,B never repeats). Also both OpenAI-as-A and OpenAI-as-B occur.
    repeats = sum(1 for i in range(1, len(orientations)) if orientations[i] == orientations[i - 1])
    assert repeats > 20, f"looks like alternation (only {repeats} repeats)"
    assert "openai" in orientations and "anthropic" in orientations


# ── A3: the fallback ladder ───────────────────────────────────────────────────
def test_fallback_priority_two_frontier(monkeypatch):
    """Both providers succeed → two_frontier (the ~always path); no fallback; each
    model called exactly once (concurrent, no re-request)."""
    store = A.fresh_store()
    calls = _install_llm(monkeypatch, lambda m: ("ok", f"answer {m}"))
    pair, meta = _run(B.assemble_ab_pair(store, {"task_id": "t1", "prompt": "case"}))
    assert len(pair) == 2 and meta["ab_source"] == "two_frontier"
    assert {c["provider"] for c in pair} == {"openai", "anthropic"}
    assert calls == {"gpt-5": 1, "claude-opus-4-8": 1}         # each once, no fallback


def test_fallback_ladder_single_failure(monkeypatch):
    """OpenAI fails, Anthropic succeeds → legacy_fallback pair (two Anthropic answers),
    fallback_reason set, the surviving Anthropic answer NOT re-requested."""
    store = A.fresh_store()
    calls = _install_llm(monkeypatch,
                         lambda m: ("error", "500 boom") if resolve_provider(m) == "openai"
                         else ("ok", f"answer {m}"))
    pair, meta = _run(B.assemble_ab_pair(store, {"task_id": "t2", "prompt": "case"}))
    assert len(pair) == 2 and meta["ab_source"] == "legacy_fallback"
    assert {c["provider"] for c in pair} == {"anthropic"}      # same-provider (Anthropic) pair
    assert "openai" in (meta["fallback_reason"] or "")
    assert calls.get("claude-opus-4-8") == 1                   # surviving answer reused, not re-called
    assert calls.get("gpt-5") == 1                             # openai tried once (per call_llm)


def test_no_gold_standin(monkeypatch):
    """Total provider failure → no pair (needs_baseline). NEVER a gold case stand-in."""
    store = A.fresh_store()
    _install_llm(monkeypatch, lambda m: ("error", "down"))
    pair, meta = _run(B.assemble_ab_pair(store, {"task_id": "t3", "prompt": "case"}))
    assert pair == [] and meta["ab_source"] is None
    blob = str(pair)
    assert "gold" not in blob.lower()


def test_fallback_rate_guard(monkeypatch):
    """When the rolling fallback rate already exceeds the ceiling, Rung 2 is suppressed:
    a new shortfall returns no pair + alert (needs_baseline), not another legacy pair."""
    store = A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_MAX_FALLBACK_RATE", "0.20")
    # Seed history: 5 legacy_fallback pairs → rolling rate 1.0 > 0.20.
    for i in range(5):
        store.insert_task(prompt=f"seed {i}", specialty="nephrology",
                          generation={"ab_source": "legacy_fallback", "mode": "grade_real_models"})
    assert store.ab_fallback_rate(window=50) == 1.0
    _install_llm(monkeypatch,
                 lambda m: ("error", "down") if resolve_provider(m) == "openai" else ("ok", "a"))
    pair, meta = _run(B.assemble_ab_pair(store, {"task_id": "t4", "prompt": "case"}))
    assert pair == [] and meta["alert"] is True
    assert meta["fallback_reason"] == "fallback_rate_exceeded"


# ── A4: one provider's exception must not kill the other ──────────────────────
def test_gather_isolation(monkeypatch):
    store = A.fresh_store()
    _install_llm(monkeypatch,
                 lambda m: ("error", "openai exploded") if resolve_provider(m) == "openai"
                 else ("ok", "anthropic survived"))
    runs = _run(B.run_baselines(store, {"task_id": "t5", "prompt": "case"},
                                models=["gpt-5", "claude-opus-4-8"]))
    an = next(r for r in runs if r["provider"] == "anthropic")
    oa = next(r for r in runs if r["provider"] == "openai")
    assert an["response_text"] == "anthropic survived"         # sibling not cancelled
    assert not (oa.get("response_text") or "") and oa.get("error")


# ── A7: V4 real cases stay Anthropic-only unless the flag is on ───────────────
def test_v4_two_frontier_gated(monkeypatch):
    store = A.fresh_store()
    monkeypatch.delenv("ASCLEPIUS_TWO_FRONTIER_V4", raising=False)   # default OFF
    calls = _install_llm(monkeypatch, lambda m: ("ok", f"answer {m}"))
    task = {"task_id": "t-v4", "prompt": "real case", "case_source": "real_deid"}
    pair, meta = _run(B.assemble_ab_pair(store, task))
    assert meta["ab_source"] == "anthropic_only_v4"
    assert {c["provider"] for c in pair} == {"anthropic"}
    assert not any(resolve_provider(m) == "openai" for m in calls), calls   # OpenAI never called


def test_v4_two_frontier_opt_in(monkeypatch):
    store = A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_TWO_FRONTIER_V4", "1")
    calls = _install_llm(monkeypatch, lambda m: ("ok", f"answer {m}"))
    task = {"task_id": "t-v4b", "prompt": "real case", "case_source": "real_deid"}
    pair, meta = _run(B.assemble_ab_pair(store, task))
    assert meta["ab_source"] == "two_frontier"
    assert any(resolve_provider(m) == "openai" for m in calls)          # OpenAI now used


# ── E-2: ab_source flows to the packaged record ───────────────────────────────
def test_ab_source_reaches_packaging():
    from asclepius.packaging import package_submission
    task = {"task_id": "tp", "prompt": "q", "specialty": "nephrology",
            "generation": {"ab_source": "legacy_fallback", "fallback_reason": "openai_4xx"},
            "candidate_answers": [{"id": "A", "text": "x", "source": "baseline"},
                                  {"id": "B", "text": "y", "source": "baseline"}]}
    submission = {"submission_id": "s", "task_id": "tp", "verdict": "A_better", "chosen_id": "A",
                  "rejected_id": "B", "confidence": "high", "created_at": "2026-07-16T00:00:00",
                  "annotator": {"id_hashed": "h", "credentials": "board_certified_nephrology"},
                  "payload": {"verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
                              "portal_version": "v3"}}
    recs = package_submission(task, submission)
    assert recs, "no records packaged"
    assert all(r.get("ab_source") == "legacy_fallback" for r in recs)
    assert all(r.get("fallback_reason") == "openai_4xx" for r in recs)


# ── E-3: NULL-provider legacy rows don't break stats ──────────────────────────
def test_null_provider_backcompat():
    store = A.fresh_store()
    # A legacy-shaped baseline run with NULL provider must not break the metrics.
    store.insert_baseline_run(task_id="tn", model="some-model", response_text="ans",
                              provider=None, prompt_hash="h")
    # These must not raise on a NULL-provider row / no pairing history.
    assert store.ab_slot_balance()["openai_as_A_rate"] is None or isinstance(
        store.ab_slot_balance()["openai_as_A_rate"], float)
    assert store.ab_fallback_rate(window=50) is None            # no ab_source history


# ── E-4: score.py critical-negative hard-fail edge (all-negative rubric) ──────
def test_score_hardfail_no_divzero():
    import types
    from asclepius.export import _SCORE_PY
    mod = types.ModuleType("score_scaffold")
    src = (_SCORE_PY
           .replace('HERE = pathlib.Path(__file__).parent', 'HERE = pathlib.Path(".")')
           .replace('PROMPT = (HERE / "grader_prompt.txt").read_text(encoding="utf-8")', 'PROMPT = ""')
           .replace('if __name__ == "__main__":\n    main()', ""))
    exec(compile(src, "score.py", "exec"), mod.__dict__)
    # All-negative rubric (max_points would be 0) + a committed critical negative.
    rubric = {"criteria": [{"text": "never do X", "points": -9, "tier": "critical"}]}
    judged = {"per_criterion": [{"text": "never do X", "met": True}],
              "score": -9, "max_points": 0, "normalized": 0.0}
    out = mod.apply_critical_hard_fail(judged, rubric)          # must not raise
    assert out["critical_failure"] is True and out["normalized"] == 0.0


# ── E-5: real OpenAI SDK shape (integration; skips if openai not installed) ────
def test_openai_sdk_shape_matches_our_calls():
    """The unit tests stub the LLM, so the real SDK surface can rot undetected. This
    integration check asserts our Responses-API kwargs + response fields match the
    installed openai SDK. Skips cleanly when openai isn't installed (e.g. minimal CI)."""
    openai = pytest.importorskip("openai")
    import inspect
    from openai import OpenAI
    c = OpenAI(api_key="sk-not-real-dummy")
    params = set(inspect.signature(c.responses.create).parameters)
    for kw in ("model", "instructions", "input", "max_output_tokens", "temperature"):
        assert kw in params, f"responses.create missing {kw!r} in openai {openai.__version__}"
    from openai.types.responses import Response
    # output_text is a computed property (not a model field) that returns '' on empty.
    assert isinstance(getattr(Response, "output_text", None), property)
    from openai.types.responses.response_usage import ResponseUsage
    uf = set(getattr(ResponseUsage, "model_fields", {}))
    assert {"input_tokens", "output_tokens"} <= uf
