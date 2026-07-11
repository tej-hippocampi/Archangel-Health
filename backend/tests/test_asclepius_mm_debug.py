"""Multimodal Debug PRD — last-mile wiring + contract locks.

Covers: the case-judge/case-gen PROMPT↔READER key contract (P1.7 — a silent key
rename would zero every score and drop every case), the multimodal autofill flag
(P0.2 — on: the empty queue can refill with cases; off/zero-yield: text fallback
so the queue never starves), and the open-queue modality counts on /stats (P3.11).
LLM stubbed throughout.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    asc_profiles.clear_cache()

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _ev_h():
    return A.headers_for(A.make_user(_store(), role="evaluator", specialty="nephrology",
                                     board_cert="board_certified_nephrology", years_experience=12))


# ─── P1.7 — prompt ↔ reader key contract locks ────────────────────────────────
def test_case_judge_prompt_names_exact_reader_keys():
    """run_case_judge reads these four keys from the judge's JSON. If the system
    prompt stops instructing them BY NAME, every score parses as None→0.0 and
    every case silently drops at the Stage 3c floors. Lock the contract."""
    from asclepius.prompts import ASCLEPIUS_CASE_JUDGE_SYSTEM
    for key in ("coherence", "ground_truth_determinable",
                "multimodal_necessity", "reasoning_divergence_potential"):
        assert f'"{key}"' in ASCLEPIUS_CASE_JUDGE_SYSTEM, key


def test_case_gen_prompt_names_exact_reader_keys():
    """generate_case reads `question` + `case` from the generator's JSON — same
    contract lock as the judge."""
    from asclepius.prompts import ASCLEPIUS_CASE_GEN_SYSTEM
    for key in ('"question"', '"case"'):
        assert key in ASCLEPIUS_CASE_GEN_SYSTEM, key


def test_wrong_judge_keys_would_drop_documented(monkeypatch):
    """Document the failure mode the lock above prevents: a judge response with
    renamed keys yields skipped=False + all-None scores — which the Stage 3c
    gate treats as 0.0 and drops as case_incoherent."""
    import json
    import ai.llm_client as llm
    from asclepius import critic

    async def fake_call(**kw):
        return ({}, {"model": "judge"})

    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", lambda resp: json.dumps(
        {"coherency": 0.9, "gt": 0.9, "necessity": 0.9, "divergence": 0.9}))  # wrong names
    import asyncio
    res = asyncio.run(critic.run_case_judge({"case_source": "synthetic"}))
    assert res["skipped"] is False
    assert res["coherence"] is None  # → 0.0 at the gate → dropped


# ─── P0.2 — multimodal autofill flag ──────────────────────────────────────────
def _mm_case(n=0):
    return {
        "case_source": "synthetic", "specialty": "nephrology",
        "demographics": {"age_band": "70-79", "sex": "M"},
        "lab_panels": [{"panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "Sodium", "value": 110 + n, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"}]}],
        "notes": [{"note_type": "Consult", "author_role": "nephrology", "text": f"Euvolemic; thiazide. Case {n}."}],
        "ground_truth": {"answer": "Thiazide-associated hyponatremia"},
    }


def _fake_mm_generate(accepted=1):
    """A stub for generation.generate_tasks(multimodal=True): inserts real
    multimodal HARD tasks into the store (like the real engine) and reports."""
    async def _fake(store, *, specialty, n, multimodal=False, created_by=None, **kw):
        assert multimodal is True
        made = []
        for i in range(min(accepted, n)):
            t = store.insert_task(
                prompt=f"CLINICAL QUESTION:\nClassify hyponatremia {uuid.uuid4().hex[:6]}.\n\nCLINICAL CASE\nLabs: Na low",
                specialty=specialty, difficulty="hard", capture_reasoning=True,
                source="internal_prompt_bank",
                candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
                case=_mm_case(i), created_by=created_by,
            )
            made.append(t["task_id"])
        return {"job_id": "j", "created": made, "accepted": len(made),
                "dropped": ({} if made else {"case_incoherent": n}), "shortfall": n - len(made)}
    return _fake


def test_autofill_multimodal_flag_serves_a_case(monkeypatch):
    from routers import asclepius as R
    from asclepius import generation as gen
    monkeypatch.setenv("ASCLEPIUS_AUTOFILL_MULTIMODAL", "1")
    monkeypatch.setattr(gen, "generate_tasks", _fake_mm_generate(accepted=2))
    R._autofill_last_attempt.clear()

    r = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=_ev_h())
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task is not None
    assert task["modality"] == "multimodal"
    assert task["case"] and task["case"]["lab_panels"]
    assert "ground_truth" not in task["case"]  # answer key still stripped


def test_autofill_default_off_never_calls_multimodal(monkeypatch):
    from routers import asclepius as R
    from asclepius import generation as gen

    async def _boom(*a, **kw):
        raise AssertionError("multimodal generation must not run when the flag is off")

    monkeypatch.setattr(gen, "generate_tasks", _boom)

    async def _cands(prompt, *, specialty="general", ai_failure_mode=None):
        return {"candidates": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
                "model": "m", "intended_flawed_id": "B"}

    monkeypatch.setattr(R, "generate_candidates_ex", _cands)
    R._autofill_last_attempt.clear()

    r = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=_ev_h())
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task is not None and task["modality"] == "text"


def test_autofill_multimodal_zero_yield_falls_back_to_text(monkeypatch):
    """Queue-never-starves guarantee: flag on but the multimodal batch yields 0
    (e.g. every case dropped at the judge) → autofill still seeds text tasks."""
    from routers import asclepius as R
    from asclepius import generation as gen
    monkeypatch.setenv("ASCLEPIUS_AUTOFILL_MULTIMODAL", "1")
    monkeypatch.setattr(gen, "generate_tasks", _fake_mm_generate(accepted=0))

    async def _cands(prompt, *, specialty="general", ai_failure_mode=None):
        return {"candidates": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
                "model": "m", "intended_flawed_id": "B"}

    monkeypatch.setattr(R, "generate_candidates_ex", _cands)
    R._autofill_last_attempt.clear()

    r = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=_ev_h())
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task is not None and task["modality"] == "text"


# ─── P3.11 — open-queue modality counts on /stats ─────────────────────────────
def test_stats_reports_open_modality_counts():
    st = _store()
    admin_h = A.headers_for(A.make_user(st, role="admin"))
    st.insert_task(prompt="text q", specialty="nephrology", difficulty="hard",
                   candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}])
    st.insert_task(prompt="mm q", specialty="nephrology", difficulty="hard",
                   candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
                   case=_mm_case())
    s = client.get("/api/asclepius/stats", headers=admin_h).json()
    omc = s["open_modality_counts"]
    assert omc["multimodal"] == 1 and omc["text"] == 1
