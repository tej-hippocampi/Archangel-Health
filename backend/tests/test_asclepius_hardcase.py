"""Hard-Case Engine tests (Seamless PRD WS2).

Covers: the hardness judge + generation gate (drop below floor, force
difficulty=hard, stamp provenance; degrade offline), hard-only V3 serving, the
"not actually hard" feedback flag, and config-only specialty onboarding
(cardiology loads + serves with zero pipeline changes).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from asclepius import corpus as asc_corpus  # noqa: E402
from asclepius import specialties as asc_specialties  # noqa: E402

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


# ─── Config-only specialty onboarding ─────────────────────────────────────────
def test_cardiology_enabled_and_hardness_config_loads():
    assert asc_specialties.is_enabled("cardiology")
    hc = asc_corpus.load_hardness_config("cardiology")
    assert hc["failure_domains"] and hc["hard_case_archetypes"] and hc["hardness_rubric"]
    # Corpus items validate against the cardiology taxonomy (topics == bucket ids).
    c = asc_corpus.load_corpus("cardiology", force=True)
    bucket_ids = set(asc_specialties.get_specialty_config("cardiology").bucket_ids())
    assert all(it["topic"] in bucket_ids for it in c["items"])


def test_nephrology_hard_case_archetypes_present():
    hc = asc_corpus.load_hardness_config("nephrology")
    assert len(hc["hard_case_archetypes"]) >= 20  # Appendix A starter set
    assert "electrolyte_acid_base" in asc_corpus.failure_domain_names("nephrology")


# ─── Hardness judge (pure, mocked LLM) ────────────────────────────────────────
def test_hardness_judge_parses_score_and_axes(monkeypatch):
    import ai.llm_client as llm
    from asclepius import critic

    async def fake_call_llm(**kw):
        return ({"stub": True}, {"model": "hard-judge"})

    monkeypatch.setattr(llm, "call_llm", fake_call_llm)
    monkeypatch.setattr(llm, "first_text", lambda resp:
                        '{"hardness_score": 0.86, "hardness_axes": ["multi_step","diagnostic_trap","high_stakes"], "explanation": "trap-laden"}')
    res = asyncio.run(critic.run_hardness_judge("A hard case", [{"id": "A", "text": "x"}],
                                                failure_domains=["electrolyte_acid_base"]))
    assert res["skipped"] is False
    assert res["hardness_score"] == 0.86
    assert "diagnostic_trap" in res["hardness_axes"]


def test_hardness_judge_degrades_offline():
    from asclepius import critic
    # No LLM key in the test env → skipped (so generation never drops on hardness).
    res = asyncio.run(critic.run_hardness_judge("prompt", [{"id": "A", "text": "x"}]))
    assert res["skipped"] is True


# ─── Generation gate ──────────────────────────────────────────────────────────
def test_generation_drops_below_hardness_floor(monkeypatch):
    """A candidate that clears the quality judge but not the hardness floor is
    dropped as below_hardness_floor; a hard one is stamped difficulty=hard."""
    A.fresh_store()
    from asclepius import generation as gen

    # difficulty 'hard' so the pre-existing min-difficulty gate passes for every
    # bucket and the HARDNESS gate is the deciding factor.
    async def fake_prompt_gen(*a, **k):
        return {"prompts": [{"prompt": "A nephrology case about hyperkalemia on HD", "difficulty": "hard",
                             "ai_failure_mode": "dosing"}], "model": "m"}

    async def fake_candidates(prompt, **k):
        return {"candidates": [{"id": "A", "text": "strong"}, {"id": "B", "text": "flawed"}],
                "model": "cg", "intended_flawed_id": "B"}

    async def fake_prompt_judge(prompt, candidates):
        return {"skipped": False, "error_likelihood": 0.9, "revision_value": 0.9,
                "on_specialty": True, "safety_ok": True, "explanation": ""}

    # First run: hardness below floor → dropped.
    async def soft_hardness(prompt, candidates=None, **k):
        return {"skipped": False, "hardness_score": 0.3, "hardness_axes": [], "explanation": "easy"}

    monkeypatch.setattr(gen, "run_prompt_gen", fake_prompt_gen)
    monkeypatch.setattr(gen, "generate_candidates_ex", fake_candidates)
    monkeypatch.setattr(gen, "run_prompt_judge", fake_prompt_judge)
    monkeypatch.setattr(gen, "run_hardness_judge", soft_hardness)

    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1))
    assert res["accepted"] == 0
    assert res["dropped"].get("below_hardness_floor", 0) >= 1

    # Second run: hardness clears the floor → created + difficulty forced to hard.
    async def hard_hardness(prompt, candidates=None, **k):
        return {"skipped": False, "hardness_score": 0.85,
                "hardness_axes": ["multi_step", "high_stakes"], "explanation": "hard", "model": "hj"}

    monkeypatch.setattr(gen, "run_hardness_judge", hard_hardness)
    res2 = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1))
    assert res2["accepted"] >= 1
    tasks = _store().list_tasks(specialty="nephrology", limit=10)
    created = [t for t in tasks if (t.get("generation") or {}).get("hardness")]
    assert created and all(t["difficulty"] == "hard" for t in created)
    assert created[0]["generation"]["hardness"]["score"] == 0.85


def test_generation_does_not_drop_when_hardness_judge_skipped(monkeypatch):
    """Offline (hardness judge skipped) generation is unaffected — no hardness
    drops, difficulty preserved from the seed."""
    A.fresh_store()
    from asclepius import generation as gen

    async def fake_prompt_gen(*a, **k):
        return {"prompts": [{"prompt": "Nephrology hyperkalemia case", "difficulty": "hard",
                             "ai_failure_mode": "dosing", "bucket": "electrolyte_acid_base"}], "model": "m"}

    async def fake_candidates(prompt, **k):
        return {"candidates": [{"id": "A", "text": "s"}, {"id": "B", "text": "f"}], "model": "cg", "intended_flawed_id": "B"}

    async def fake_prompt_judge(prompt, candidates):
        return {"skipped": False, "error_likelihood": 0.9, "revision_value": 0.9, "on_specialty": True, "safety_ok": True}

    async def skipped_hardness(prompt, candidates=None, **k):
        return {"skipped": True, "error": "no key"}

    monkeypatch.setattr(gen, "run_prompt_gen", fake_prompt_gen)
    monkeypatch.setattr(gen, "generate_candidates_ex", fake_candidates)
    monkeypatch.setattr(gen, "run_prompt_judge", fake_prompt_judge)
    monkeypatch.setattr(gen, "run_hardness_judge", skipped_hardness)

    res = asyncio.run(gen.generate_tasks(_store(), specialty="nephrology", n=1))
    assert res["accepted"] >= 1
    assert res["dropped"].get("below_hardness_floor", 0) == 0


# ─── Hard-only V3 serving + not_hard feedback ─────────────────────────────────
def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _ev_h():
    return A.headers_for(A.make_user(_store(), role="evaluator", specialty="nephrology"))


def _mk_task(admin_h, difficulty):
    r = client.post("/api/asclepius/tasks", json={"tasks": [{
        "specialty": "nephrology", "difficulty": difficulty, "source": "lab_supplied",
        "prompt": f"case {uuid.uuid4().hex[:6]} difficulty {difficulty}",
        "candidate_answers": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    }]}, headers=admin_h)
    return r.json()["created"][0]


def _mk_multimodal_task(admin_h):
    """A hard MULTIMODAL task (structured case with labs + note) via the admin
    upload path — modality is derived from the case content."""
    case = {
        "case_source": "synthetic", "specialty": "nephrology",
        "demographics": {"age_band": "70-79", "sex": "M"},
        "problem_list": [{"condition": "CKD"}],
        "medications": [{"drug": "Tacrolimus"}],
        "lab_panels": [{"panel": "BMP", "collected_offset_days": 0, "results": [
            {"analyte": "Na", "value": 120, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "L"}]}],
        "notes": [{"note_type": "Consult", "author_role": "nephrology", "text": "Euvolemic on tacrolimus."}],
    }
    r = client.post("/api/asclepius/tasks", json={"tasks": [{
        "specialty": "nephrology", "difficulty": "hard", "source": "lab_supplied",
        "prompt": f"multimodal {uuid.uuid4().hex[:6]}", "case": case,
        "candidate_answers": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    }]}, headers=admin_h)
    return r.json()["created"][0]


def test_v3_prefers_multimodal_when_available(monkeypatch):
    """V3 multimodal-by-default (ASCLEPIUS_V3_MULTIMODAL_ONLY): when a structured
    case (labs + EHR) IS available, the seamless queue serves it ahead of any bare
    text prompt — so the V3 doctor always lands on a multimodal case. Autofill is
    disabled here so we assert pure serving."""
    monkeypatch.setenv("ASCLEPIUS_V3_MULTIMODAL_ONLY", "1")
    monkeypatch.setenv("ASCLEPIUS_AUTOFILL", "0")
    A.fresh_store()
    admin_h = _admin_h()
    ev_h = _ev_h()
    text_hard = _mk_task(admin_h, "hard")           # a hard TEXT task
    mm = _mk_multimodal_task(admin_h)               # a hard MULTIMODAL case
    served = []
    for _ in range(4):
        t = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=ev_h).json()["task"]
        if t:
            served.append(t["task_id"])
    assert mm in served                             # the structured case is preferred
    assert text_hard not in served                 # the bare text prompt is not served while a case exists
    # And the served V3 task actually carries the case (labs + notes) for the panel.
    t = client.get(f"/api/asclepius/tasks/{mm}?portal_version=v3", headers=ev_h).json()["task"]
    assert t["modality"] == "multimodal" and t["case"]["lab_panels"] and t["case"]["notes"]
    # V2 (not multimodal-preferring) still serves the text task.
    v2 = client.get("/api/asclepius/tasks/next?portal_version=v2", headers=ev_h).json()["task"]
    assert v2 is not None


def test_v3_falls_back_to_text_when_no_multimodal_case(monkeypatch):
    """Regression: the multimodal preference must NOT empty the V3 queue. When no
    structured case has been generated (e.g. no LLM key in the deployment), V3 must
    still serve the available hard text task rather than showing a cleared queue."""
    monkeypatch.setenv("ASCLEPIUS_V3_MULTIMODAL_ONLY", "1")
    monkeypatch.setenv("ASCLEPIUS_AUTOFILL", "0")
    A.fresh_store()
    admin_h = _admin_h()
    ev_h = _ev_h()
    text_hard = _mk_task(admin_h, "hard")           # only a hard TEXT task exists
    t = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=ev_h).json()["task"]
    assert t is not None and t["task_id"] == text_hard  # queue is NOT empty


def test_v3_serves_only_hard_tasks():
    A.fresh_store()
    admin_h = _admin_h()
    ev_h = _ev_h()
    medium = _mk_task(admin_h, "medium")
    hard = _mk_task(admin_h, "hard")
    # V3 (hard-case queue) must serve the hard task, never the medium one.
    served = []
    for _ in range(4):
        t = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=ev_h).json()["task"]
        if t:
            served.append(t["task_id"])
    assert hard in served
    assert medium not in served
    # V2 (assisted, not hard-only) may serve the medium task.
    v2 = client.get("/api/asclepius/tasks/next?portal_version=v2", headers=ev_h).json()["task"]
    assert v2 is not None


def test_hard_only_disabled_lets_v3_serve_any(monkeypatch):
    """Config consistency: if ASCLEPIUS_HARD_ONLY=0 disables the hardness gate
    (nothing gets stamped 'hard'), the V3 queue must NOT keep filtering to hard —
    otherwise every V3 clinician sees an empty queue. It falls back to serving the
    available queue."""
    monkeypatch.setenv("ASCLEPIUS_HARD_ONLY", "0")
    A.fresh_store()
    admin_h = _admin_h()
    ev_h = _ev_h()
    medium = _mk_task(admin_h, "medium")
    served = []
    for _ in range(4):
        t = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=ev_h).json()["task"]
        if t:
            served.append(t["task_id"])
    assert medium in served  # not filtered out when the gate is off


def test_not_hard_flag_routes_task_out_zero_records():
    A.fresh_store()
    admin_h = _admin_h()
    ev_h = _ev_h()
    tid = _mk_task(admin_h, "hard")
    body = {
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid,
        "portal_version": "v3", "time_spent_sec": 20,
        "prompt_review": {"reviewed": True, "verdict": "not_hard", "note": "recall question"},
    }
    r = client.post("/api/asclepius/submissions", json=body, headers=ev_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "not_hard"
    assert r.json()["record_count"] == 0
    # Task is out of the queue (not 'open').
    assert _store().get_task(tid)["status"] == "not_hard"


def test_taxonomy_exposes_not_hard_and_hardness_axes():
    r = client.get("/api/asclepius/taxonomy", headers=_ev_h())
    body = r.json()
    assert "not_hard" in body["prompt_review_verdicts"]
