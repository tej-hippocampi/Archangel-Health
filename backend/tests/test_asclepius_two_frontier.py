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
