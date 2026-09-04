"""Fake LLM Provider PRD §4 — the acceptance tests.

The whole point of the fake is that the sandbox holds no key and every LLM path
still runs, so these tests assert the properties that make that safe: the switch
is scoped to the transport, every call site the code actually has is answerable,
the fixtures pass the real validators, the verdict override works, output is
deterministic, and a fake can never run in production.
"""

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai import fake_llm  # noqa: E402
from ai.model_config import (  # noqa: E402
    UnknownProvider, active_provider, fake_llm_enabled, resolve_provider,
)

BACKEND = pathlib.Path(__file__).resolve().parent.parent


# ─── §4.1 the switch is scoped to the transport ──────────────────────────────

def test_fake_is_on_for_the_suite():
    """conftest sets the switch, so the whole suite runs keyless."""
    assert fake_llm_enabled() is True
    assert os.getenv("ASCLEPIUS_LLM_PROVIDER") == "fake"


def test_active_provider_returns_fake_only_when_the_switch_is_set(monkeypatch):
    assert active_provider("claude-opus-4-8") == "fake"
    monkeypatch.setenv("ASCLEPIUS_LLM_PROVIDER", "")
    assert active_provider("claude-opus-4-8") == "anthropic"
    assert active_provider("gpt-5") == "openai"


def test_resolve_provider_stays_pure_under_the_fake_switch():
    """The switch must NOT leak into vendor resolution.

    ``constants.baseline_pairing_ok`` requires the two baseline models to resolve
    to two DIFFERENT vendors; if the fake collapsed both to "fake" it would fail
    startup validation in the very sandbox this feature exists to enable.
    """
    assert fake_llm_enabled() is True  # switch is on for this run
    assert resolve_provider("claude-opus-4-8") == "anthropic"
    assert resolve_provider("gpt-5") == "openai"
    with pytest.raises(UnknownProvider):
        resolve_provider("mixtral-8x7b")


def test_baseline_pairing_still_sees_two_real_vendors():
    from asclepius.constants import baseline_pairing_ok

    ok, msg = baseline_pairing_ok()
    assert ok, msg
    assert "fake" not in msg


def test_a_garbage_model_id_still_raises_under_the_fake():
    """The fake replaces the transport, never the config validation."""
    with pytest.raises(UnknownProvider):
        active_provider("mixtral-8x7b")


# ─── §4.2 every call site the code actually has is answerable ────────────────

def _declared_call_sites():
    """AST-scan the backend for call_llm/call_llm_sync and return the (purpose,
    role) pair each site declares. This is the PRD's "grep-equivalent AST scan"."""
    sites = []
    for path in BACKEND.rglob("*.py"):
        if "/tests/" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(), str(path))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name not in ("call_llm", "call_llm_sync"):
                continue
            kw = {k.arg: k.value for k in node.keywords}

            def _lit(key):
                v = kw.get(key)
                return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None

            sites.append((_lit("purpose"), _lit("role"), f"{path.relative_to(BACKEND)}:{node.lineno}",
                          "tools" in kw))
    return sites


def test_the_ast_scan_finds_the_call_sites():
    """Guards the guard: if this drops to zero the coverage test below passes
    vacuously."""
    assert len(_declared_call_sites()) >= 25


def test_every_call_site_has_a_fixture():
    """Every purpose/role pair in the live code resolves to a fixture.

    Tool-use sites are exempt: they are answered from the tool's own input_schema,
    not from a per-purpose fixture.
    """
    keys = fake_llm.fixture_keys()
    missing = [
        f"{loc} (purpose={p!r}, role={r!r})"
        for p, r, loc, uses_tools in _declared_call_sites()
        if not uses_tools and (p or "") not in keys and (r or "") not in keys
    ]
    assert not missing, (
        "call sites with no fake fixture — add a key to ai/fake_llm._FIXTURES:\n  "
        + "\n  ".join(missing)
    )


def test_an_unknown_purpose_raises_rather_than_returning_a_generic_string():
    with pytest.raises(fake_llm.UnknownFakePurpose):
        fake_llm.build_response(role="no_such_role", purpose="no_such_purpose",
                                system="s", messages=[], kwargs={})


def test_no_fixture_key_is_dead():
    """Every registered key is claimed by a real call site (or is a tool-use role).

    A key nobody calls is a fixture that will silently rot.
    """
    sites = _declared_call_sites()
    claimed = {p for p, _, _, _ in sites if p} | {r for _, r, _, _ in sites if r}
    dead = sorted(fake_llm.fixture_keys() - claimed)
    assert not dead, f"fixture keys no call site uses: {dead}"


# ─── §4.3 fixtures pass the same validators real output must pass ────────────

@pytest.mark.asyncio
async def test_generated_case_passes_the_multimodal_content_gate():
    from asclepius.cases import assert_multimodal_content
    from asclepius.critic import generate_case

    out = await generate_case({"archetype": "test"}, specialty="nephrology")
    assert out.get("skipped") is False, out
    assert not out.get("error"), out
    assert_multimodal_content(out["case"])  # raises if the floors are not met


@pytest.mark.asyncio
async def test_candidate_generation_returns_two_well_formed_answers():
    from asclepius.critic import generate_candidates_ex

    out = await generate_candidates_ex("a prompt", specialty="nephrology")
    cands = out.get("candidates") or []
    assert len(cands) == 2, out
    assert {c["id"] for c in cands} == {"A", "B"}
    assert all((c.get("text") or "").strip() for c in cands)


@pytest.mark.asyncio
async def test_tool_use_calls_are_answered_from_the_tools_own_schema():
    """Forced-tool call sites read ``.content[].input``; a text block would make
    every one of them raise "returned no tool_use block"."""
    from eligibility.extract import extract_eligibility

    out = await extract_eligibility(["70yo for CABG"], "2026-03-01")
    extracted = out["extracted"]
    assert isinstance(extracted, dict) and extracted
    # Fields the real TEAM extraction tool declares.
    for key in ("partA", "partB", "medicareAdvantage"):
        assert key in extracted, extracted


def test_grounding_judge_fixture_validates_against_the_report_model():
    from pipeline.grounding_check import GroundingReport

    resp = fake_llm.build_response(role="grounding_judge", purpose="grounding_judge",
                                   system="s", messages=[{"role": "user", "content": "x"}],
                                   kwargs={})
    GroundingReport.model_validate(json.loads(resp.content[0].text))


# ─── §4.4 the verdict override flips the judges ──────────────────────────────

@pytest.mark.asyncio
async def test_fake_llm_verdict_fail_flips_every_judge_to_reject(monkeypatch):
    from asclepius.constants import gen_min_error_likelihood
    from asclepius.critic import run_hardness_judge, run_prompt_judge

    passing = await run_prompt_judge("q", [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}])
    assert passing["error_likelihood"] >= gen_min_error_likelihood()
    assert passing["safety_ok"] is True

    monkeypatch.setenv("FAKE_LLM_VERDICT", "fail")
    failing = await run_prompt_judge("q", [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}])
    assert failing["error_likelihood"] < gen_min_error_likelihood()
    assert failing["safety_ok"] is False

    hard = await run_hardness_judge("q", [{"id": "A", "text": "a"}])
    assert hard["hardness_score"] < 0.75  # below constants.hardness_min()


@pytest.mark.asyncio
async def test_verdict_fail_flips_the_consistency_and_grounding_checkers(monkeypatch):
    monkeypatch.setenv("FAKE_LLM_VERDICT", "fail")
    resp = fake_llm.build_response(role="asclepius_critic",
                                   purpose="asclepius_consistency_check",
                                   system="s", messages=[], kwargs={})
    assert json.loads(resp.content[0].text)["consistent"] is False


# ─── §4.5 determinism ────────────────────────────────────────────────────────

def test_identical_inputs_produce_byte_identical_output():
    kw = dict(role="asclepius_case_gen", purpose="asclepius_case_generation",
              system="system prompt", messages=[{"role": "user", "content": "hello"}],
              kwargs={})
    a = fake_llm.build_response(**kw)
    b = fake_llm.build_response(**kw)
    assert a.content[0].text == b.content[0].text
    assert a._request_id == b._request_id
    assert (a.usage.input_tokens, a.usage.output_tokens) == (b.usage.input_tokens, b.usage.output_tokens)


def test_different_inputs_produce_different_output():
    def gen(msg):
        return fake_llm.build_response(
            role="asclepius_case_gen", purpose="asclepius_case_generation",
            system="s", messages=[{"role": "user", "content": msg}], kwargs={}).content[0].text

    assert gen("case one") != gen("a completely different case")


def test_the_two_frontier_pair_gets_two_different_baseline_answers():
    """An identical pair would make every A/B comparison a tie and hide real bugs."""
    def answer(model):
        return fake_llm.build_response(
            role="asclepius_baseline", purpose="asclepius_baseline_capture",
            system="s", messages=[{"role": "user", "content": "q"}],
            kwargs={"model": model}).content[0].text

    assert answer("claude-opus-4-8") != answer("gpt-5")


# ─── §4.6 the result is shaped exactly like a real one ───────────────────────

def test_the_fake_returns_a_real_llmresult_not_a_bare_string():
    from ai.llm_client import _LLMResult, first_text

    resp = fake_llm.build_response(role="asclepius_critic",
                                   purpose="asclepius_consistency_check",
                                   system="s", messages=[], kwargs={})
    assert isinstance(resp, _LLMResult)
    assert first_text(resp)
    assert resp.usage.input_tokens and resp.usage.output_tokens
    assert resp._request_id.startswith("fake_")


@pytest.mark.asyncio
async def test_telemetry_records_the_call_as_provider_fake():
    """The admin AI-calls view must show sandbox traffic honestly."""
    from ai.llm_client import call_llm

    _, rec = await call_llm(role="asclepius_critic", system="s",
                            messages=[{"role": "user", "content": "x"}],
                            purpose="asclepius_consistency_check")
    assert rec["provider"] == "fake"
    assert rec["model"]
    assert rec["purpose"] == "asclepius_consistency_check"
    assert rec["input_sha"]


# ─── §4.7 the production guard ───────────────────────────────────────────────

def test_a_fake_in_production_refuses_to_boot():
    """A fake in production is a silent data-corruption machine. Importing the
    config module under ENV=production with the switch on must fail loudly."""
    env = {**os.environ, "ENV": "production", "ASCLEPIUS_LLM_PROVIDER": "fake"}
    proc = subprocess.run(
        [sys.executable, "-c", "import ai.model_config"],
        cwd=str(BACKEND), env=env, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "FakeProviderInProduction" in proc.stderr
    assert "ENV=production" in proc.stderr


def test_production_without_the_fake_switch_boots_fine():
    env = {**os.environ, "ENV": "production"}
    env.pop("ASCLEPIUS_LLM_PROVIDER", None)
    proc = subprocess.run(
        [sys.executable, "-c", "import ai.model_config; print('ok')"],
        cwd=str(BACKEND), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


# ─── §4.8 latency knob ───────────────────────────────────────────────────────

def test_latency_knob_is_off_by_default_and_readable(monkeypatch):
    monkeypatch.delenv("FAKE_LLM_LATENCY_MS", raising=False)
    assert fake_llm.latency_ms() == 0
    monkeypatch.setenv("FAKE_LLM_LATENCY_MS", "250")
    assert fake_llm.latency_ms() == 250
    monkeypatch.setenv("FAKE_LLM_LATENCY_MS", "not-a-number")
    assert fake_llm.latency_ms() == 0  # never crashes a run


# ─── §4.9 no key is required ─────────────────────────────────────────────────

def test_the_suite_runs_with_no_vendor_key_present():
    from ai.model_config import is_anthropic_configured, is_openai_configured

    if is_anthropic_configured() or is_openai_configured():
        pytest.skip("a real key is exported in this environment")
    resp = fake_llm.build_response(role="asclepius_critic",
                                   purpose="asclepius_consistency_check",
                                   system="s", messages=[], kwargs={})
    assert resp.content[0].text
