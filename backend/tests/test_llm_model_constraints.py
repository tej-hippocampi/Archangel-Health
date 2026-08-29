"""Per-model request constraints — sampling params and the thinking budget.

Both rules here were found by pointing the shipped pipeline at the live API, and
both had already taken the premium generation path down without saying so.

  1. **Some models reject a pinned sampling parameter.** ``claude-opus-4-7``,
     ``claude-opus-4-8`` and the Claude 5 family return
     ``400 `temperature` is deprecated for this model``. Six registry roles pin a
     temperature on an Opus id, including the CASE JUDGE — which fails closed —
     so one rejected parameter stops real-case generation entirely and reports
     "Case judge unavailable", naming neither the parameter nor the model.

  2. **A thinking model spends the output budget before it answers.** Claude 5
     may emit a ``thinking`` block from the same ``max_tokens`` allowance as the
     visible text. Candidate generation asks for 2000, thinking consumed all of
     it, the JSON came back truncated mid-sentence, and the pipeline reported
     "no LLM key configured?" — blaming credentials for a token budget.

The measurements below are pinned as tests because both boundaries cut through a
model family, so neither can be reasoned about from an id prefix. What is NOT
asserted is any live call: these are the local rules, and they are what decide
what gets sent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import llm_client  # noqa: E402
from ai import model_config as MC  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# Sampling parameters
# ═══════════════════════════════════════════════════════════════════════════════
#: Measured against the live API. The boundary cuts THROUGH the opus 4 line —
#: 4-6 accepts a pinned temperature, 4-7 does not — which is why this is an exact
#: id list and not a prefix rule.
@pytest.mark.parametrize("model,accepts", [
    ("claude-opus-4-5-20251101", True),
    ("claude-opus-4-6", True),
    ("claude-opus-4-7", False),
    ("claude-opus-4-8", False),
    ("claude-opus-5", False),
    ("claude-sonnet-5", False),
    ("claude-fable-5", False),
    ("claude-sonnet-4-6", True),
    ("claude-haiku-4-5-20251001", True),
    ("gpt-5", True),
])
def test_which_models_accept_a_pinned_sampling_parameter(model, accepts):
    assert MC.accepts_sampling_params(model) is accepts


def test_a_prefix_rule_would_be_wrong():
    """The reason the list is exact, stated as a test so nobody 'simplifies' it.
    A ``claude-opus-4`` prefix would strip sampling from 4-5 and 4-6, silently
    loosening two judges that are pinned to 0.0 on purpose."""
    assert MC.accepts_sampling_params("claude-opus-4-6") is True
    assert MC.accepts_sampling_params("claude-opus-4-7") is False


def test_the_provider_prefix_form_resolves_the_same():
    assert MC.accepts_sampling_params("anthropic:claude-opus-5") is False
    assert MC.accepts_sampling_params("anthropic:claude-sonnet-4-6") is True


def test_the_list_is_extendable_without_a_deploy(monkeypatch):
    """A newly-shipped model with the same constraint must be handleable from the
    environment; waiting for a deploy means the premium path is down meanwhile."""
    assert MC.accepts_sampling_params("claude-future-9") is True
    monkeypatch.setenv("MODEL_FIXED_SAMPLING", "claude-future-9")
    assert MC.accepts_sampling_params("claude-future-9") is False


def test_a_fixed_sampling_model_is_never_sent_a_sampling_parameter(monkeypatch):
    """The whole point: ``_build_kwargs`` drops what the API would reject."""
    monkeypatch.setitem(MC.MODEL_REGISTRY, "asclepius_case_judge",
                        {"model": "claude-opus-5", "temperature": 0.0, "max_tokens": 1200})
    kwargs, _cfg = llm_client._build_kwargs(
        "asclepius_case_judge", "sys", [{"role": "user", "content": "hi"}], {})
    assert kwargs["model"] == "claude-opus-5"
    for p in MC.SAMPLING_PARAMS:
        assert p not in kwargs, f"{p} would be sent to a model that rejects it"


def test_a_normal_model_still_gets_its_pinned_temperature(monkeypatch):
    """The registry's pinned values are not edited away — a judge pinned to 0.0
    still gets 0.0 wherever the model honours it."""
    monkeypatch.setitem(MC.MODEL_REGISTRY, "asclepius_case_judge",
                        {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 1200})
    kwargs, _cfg = llm_client._build_kwargs(
        "asclepius_case_judge", "sys", [{"role": "user", "content": "hi"}], {})
    assert kwargs["temperature"] == 0.0


# ─── the backstop, for a model nobody has listed yet ─────────────────────────
class _Deprecated(Exception):
    status_code = 400

    def __str__(self):
        return ("Error code: 400 - {'type': 'error', 'error': {'type': "
                "'invalid_request_error', 'message': '`temperature` is deprecated "
                "for this model.'}}")


def test_a_rejected_parameter_is_dropped_and_retried():
    kwargs = {"model": "claude-unknown-9", "temperature": 0.0, "max_tokens": 10}
    retry = llm_client._retry_without_deprecated_param(kwargs, _Deprecated())
    assert retry is not None
    assert "temperature" not in retry
    assert retry["model"] == "claude-unknown-9" and retry["max_tokens"] == 10


def test_an_unrelated_error_is_not_swallowed():
    """Only a parameter the API itself named is dropped. Anything else re-raises,
    or a real bug becomes a silent retry."""
    assert llm_client._retry_without_deprecated_param(
        {"model": "m", "temperature": 0}, ValueError("overloaded")) is None


def test_a_parameter_that_was_not_sent_is_not_a_retry():
    """No infinite ping-pong: if we were not sending it, removing it changes
    nothing and the error must surface."""
    assert llm_client._retry_without_deprecated_param(
        {"model": "m", "max_tokens": 10}, _Deprecated()) is None


# ═══════════════════════════════════════════════════════════════════════════════
# The thinking budget
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("model,thinks", [
    ("claude-opus-5", True),
    ("claude-sonnet-5", True),
    ("claude-fable-5", True),
    ("claude-opus-4-8", False),
    ("claude-sonnet-4-6", False),
])
def test_which_models_may_spend_budget_on_thinking(model, thinks):
    assert MC.emits_thinking(model) is thinks


def test_a_thinking_model_gets_headroom_on_top_of_the_answer_budget():
    """Candidate generation asks for 2000 and thinking consumed all of it, so the
    JSON came back cut off mid-sentence. The reserve is the same remedy
    ``_openai_output_cap`` already applies to o1/o3/gpt-5."""
    assert llm_client._anthropic_output_cap("claude-opus-5", 2000) > 2000
    assert llm_client._anthropic_output_cap("claude-sonnet-4-6", 2000) == 2000


def test_the_reserve_is_tunable_and_never_negative(monkeypatch):
    monkeypatch.setenv("LLM_ANTHROPIC_THINKING_RESERVE", "500")
    assert llm_client._anthropic_output_cap("claude-opus-5", 1000) == 1500
    monkeypatch.setenv("LLM_ANTHROPIC_THINKING_RESERVE", "-9000")
    assert llm_client._anthropic_output_cap("claude-opus-5", 1000) == 1000
    monkeypatch.setenv("LLM_ANTHROPIC_THINKING_RESERVE", "not-a-number")
    assert llm_client._anthropic_output_cap("claude-opus-5", 1000) > 1000


def test_thinking_capability_is_extendable_without_a_deploy(monkeypatch):
    assert MC.emits_thinking("claude-future-9") is False
    monkeypatch.setenv("MODEL_THINKING_MODELS", "claude-future-9")
    assert MC.emits_thinking("claude-future-9") is True


# ═══════════════════════════════════════════════════════════════════════════════
# The diagnostic — the part that cost the most time
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_a_truncated_answer_is_reported_as_truncation_not_a_missing_key(monkeypatch):
    """"no LLM key configured?" was right for exactly one of the three ways
    candidate generation fails, and actively misleading for the other two."""
    from asclepius import critic

    class _Resp:
        stop_reason = "max_tokens"
        content = [type("B", (), {"type": "text", "text": '{"candidate_answers": [{"id":'})()]

    async def _call(**kw):
        return _Resp(), {"model": "claude-opus-5"}

    monkeypatch.setattr("ai.llm_client.call_llm", _call)
    out = await critic.generate_candidates_ex("prompt", specialty="hepatology")
    assert out["candidates"] == []
    reason = out["reason"].lower()
    assert "truncat" in reason and "max_tokens" in reason
    assert "key" not in reason, "a truncated answer must not read as a credentials problem"


@pytest.mark.asyncio
async def test_an_unparsable_answer_says_so_too(monkeypatch):
    from asclepius import critic

    class _Resp:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": "I cannot help with that."})()]

    async def _call(**kw):
        return _Resp(), {"model": "claude-sonnet-4-6"}

    monkeypatch.setattr("ai.llm_client.call_llm", _call)
    out = await critic.generate_candidates_ex("prompt")
    assert out["candidates"] == []
    assert "parsed" in out["reason"] and "end_turn" in out["reason"]
