"""Multimodal case generation + case judge + Stage 3c gate (PR-B).

The LLM is stubbed (no key needed). Verifies: generate_case / run_case_judge
parse + degrade; the multimodal generation branch produces multimodal tasks with
a structured case + provenance; each Stage 3c gate drops as specified; and a
skipped case judge never drops (same contract as the hardness judge).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

from asclepius import generation as gen  # noqa: E402


def _store():
    from asclepius.store import get_store
    return get_store()


def _case(**over):
    base = dict(
        case_source="synthetic", specialty="nephrology",
        demographics={"age_band": "70-79", "sex": "M"},
        lab_panels=[{"panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "Sodium", "value": 112, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"}]}],
        notes=[{"note_type": "Consult", "author_role": "nephrology", "text": "Euvolemic; on thiazide."}],
        ground_truth={"answer": "Thiazide-associated hyponatremia", "key_data": ["urine osm"]},
        hard_hook="urine studies decide", reasoning_divergence="SIADH shortcut ignores thiazide",
    )
    base.update(over)
    return base


# ─── Unit: generate_case + run_case_judge (mocked LLM) ────────────────────────
def test_generate_case_parses_and_stamps_synthetic(monkeypatch):
    import ai.llm_client as llm
    import json
    from asclepius import critic

    async def fake_call(**kw):
        return ({}, {"model": "case-model"})

    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", lambda resp: json.dumps({
        "question": "Classify the hyponatremia and set a safe correction rate.",
        "case": _case(),
    }))
    res = asyncio.run(critic.generate_case({"topic": "hyponatremia", "multimodal": {"panels": ["BMP"]}}, specialty="nephrology"))
    assert res["skipped"] is False
    assert res["question"].startswith("Classify")
    assert res["case"]["case_source"] == "synthetic"
    assert res["case"]["lab_panels"][0]["results"][0]["analyte"] == "Sodium"


def test_generate_case_degrades_offline():
    from asclepius import critic
    res = asyncio.run(critic.generate_case({"topic": "x", "multimodal": {}}, specialty="nephrology"))
    assert res["skipped"] is True


def test_run_case_judge_parses_scores(monkeypatch):
    import ai.llm_client as llm
    import json
    from asclepius import critic

    async def fake_call(**kw):
        return ({}, {"model": "judge"})

    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", lambda resp: json.dumps({
        "coherence": 0.9, "ground_truth_determinable": 0.85,
        "multimodal_necessity": 0.8, "reasoning_divergence_potential": 0.7, "explanation": "ok"}))
    res = asyncio.run(critic.run_case_judge(_case()))
    assert res["skipped"] is False
    assert res["coherence"] == 0.9 and res["multimodal_necessity"] == 0.8


# ─── Integration: the multimodal generation branch ────────────────────────────
def _install(monkeypatch, *, case_scores=None, hardness=0.85):
    """Stub the whole multimodal chain: one archetype, one case, passing judges."""
    counter = {"i": 0}

    # Distinct note narratives so cases aren't collapsed by the exact/near-dup
    # gates (real generation varies substantially per case).
    _narratives = [
        "Euvolemic on chronic thiazide; poor oral intake reported over the weekend.",
        "Hypervolemic with peripheral edema; heart failure exacerbation, diuretics held.",
        "Postoperative, hypotonic fluids running; nausea and headache described.",
        "Marathon runner, collapse after race; excessive free water intake noted.",
        "Cirrhosis with ascites; recent large-volume paracentesis and albumin given.",
    ]

    async def fake_generate_case(archetype, *, specialty="general"):
        counter["i"] += 1
        na = 106 + counter["i"]
        note = _narratives[counter["i"] % len(_narratives)]
        case = _case(
            lab_panels=[{"panel": "BMP", "collected_offset_days": 0, "results": [
                {"analyte": "Sodium", "value": na, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"}]}],
            notes=[{"note_type": "Consult", "author_role": "nephrology", "text": note}])
        return {"case": case, "question": f"Classify this hyponatremia presentation ({counter['i']}) and set a safe correction rate.",
                "model": "cg", "skipped": False}

    async def fake_candidates(prompt, **k):
        return {"candidates": [{"id": "A", "text": "s"}, {"id": "B", "text": "f"}], "model": "cand", "intended_flawed_id": "B"}

    async def fake_prompt_judge(prompt, candidates):
        return {"skipped": False, "error_likelihood": 0.9, "revision_value": 0.9, "on_specialty": True, "safety_ok": True}

    async def fake_hardness(prompt, candidates=None, **k):
        return {"skipped": False, "hardness_score": hardness, "hardness_axes": ["multi_step"], "explanation": "h"}

    scores = case_scores if case_scores is not None else {
        "coherence": 0.9, "ground_truth_determinable": 0.85,
        "multimodal_necessity": 0.8, "reasoning_divergence_potential": 0.7}

    async def fake_case_judge(case):
        return {"skipped": False, **scores, "explanation": "", "model": "cj"}

    monkeypatch.setattr(gen, "_multimodal_archetypes", lambda specialty: [
        {"topic": "hyponatremia_beer_potomania_vs_siadh", "multimodal": {"panels": ["BMP"], "hard_hook": "urine studies"}}])
    monkeypatch.setattr(gen, "generate_case", fake_generate_case)
    monkeypatch.setattr(gen, "generate_candidates_ex", fake_candidates)
    monkeypatch.setattr(gen, "run_prompt_judge", fake_prompt_judge)
    monkeypatch.setattr(gen, "run_hardness_judge", fake_hardness)
    monkeypatch.setattr(gen, "run_case_judge", fake_case_judge)


def test_multimodal_generation_produces_case_tasks(monkeypatch):
    A.fresh_store()
    _install(monkeypatch)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=2, multimodal=True))
    assert res["accepted"] == 2, res["dropped"]
    tasks = _store().list_tasks(specialty="nephrology", limit=10)
    mm = [t for t in tasks if t["modality"] == "multimodal"]
    assert len(mm) == 2
    t = mm[0]
    assert t["difficulty"] == "hard"                     # multimodal is always hard
    assert t["capture_reasoning"] is True                # the value is the reasoning trace
    assert t["case"]["lab_panels"]                       # structured case stored
    assert "Sodium" in t["prompt"]                       # rendered case in the prompt
    g = t["generation"]
    assert g["modality"] == "multimodal" and g["case_source"] == "synthetic"
    assert g["case_judge"]["multimodal_necessity"] == 0.8
    assert g["seed_archetype_id"] == "hyponatremia_beer_potomania_vs_siadh"
    # server-side answer key present on the stored task, not leaked to prompt
    assert t["case"]["ground_truth"]["answer"] not in t["prompt"]


def test_case_judge_gates_drop_as_specified(monkeypatch):
    for field, reason in [
        ("coherence", "case_incoherent"),
        ("ground_truth_determinable", "ground_truth_indeterminate"),
        ("multimodal_necessity", "multimodal_not_necessary"),
        ("reasoning_divergence_potential", "low_reasoning_divergence"),
    ]:
        A.fresh_store()
        bad = {"coherence": 0.9, "ground_truth_determinable": 0.85,
               "multimodal_necessity": 0.8, "reasoning_divergence_potential": 0.7}
        bad[field] = 0.1  # below every floor
        _install(monkeypatch, case_scores=bad)
        res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
        assert res["accepted"] == 0, (field, res)
        assert res["dropped"].get(reason, 0) >= 1, (field, reason, res["dropped"])


def test_multimodal_does_not_drop_when_case_judge_skipped(monkeypatch):
    A.fresh_store()
    _install(monkeypatch)

    async def skipped_case_judge(case):
        return {"skipped": True, "error": "no key"}

    monkeypatch.setattr(gen, "run_case_judge", skipped_case_judge)
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1, multimodal=True))
    assert res["accepted"] >= 1
    assert res["dropped"].get("case_incoherent", 0) == 0


def test_multimodal_with_no_archetypes_creates_nothing(monkeypatch):
    A.fresh_store()
    _install(monkeypatch)
    monkeypatch.setattr(gen, "_multimodal_archetypes", lambda specialty: [])
    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=2, multimodal=True))
    assert res["accepted"] == 0
