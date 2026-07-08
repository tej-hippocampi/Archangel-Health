"""Seedmaker generation engine tests (PRD §7, §10, §16).

The LLM (prompt-gen / candidate-gen / judge) is stubbed — no API key needed. We
verify: N accepted with full provenance; the server-side intended-flawed id is
stripped from the blinded task; every gate (contamination / dedupe / off-specialty
/ unsafe / low-error-likelihood / low-revision-value / candidate-gen-failed /
judge-failed) routes to ``dropped{reason}``; no-LLM disables generation; generated
tasks package to grounded:false; the buyer-request spec-only path invokes the
engine and stamps buyer_request_id; router auth + disabled-specialty 400; and the
"did the doctor catch it" metric (caught_flaw + /stats flaw_catch_rate).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import random as _random  # noqa: E402

import ai.llm_client as _llm  # noqa: E402
from tests import _asclepius as A  # noqa: E402
from asclepius import corpus as asc_corpus  # noqa: E402
from asclepius import critic as asc_critic  # noqa: E402
from asclepius import generation as asc_generation  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from routers import asclepius as asc_router  # noqa: E402

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    asc_corpus.load_corpus("nephrology", force=True)
    yield


def install_stubs(
    monkeypatch,
    *,
    prompt_text=None,
    candidates=2,
    intended_flawed="B",
    skip_pg=False,
    judge_overrides=None,
):
    counter = {"i": 0}

    async def _pg(**kw):
        if skip_pg:
            return {"prompts": [], "skipped": True}
        out = []
        for _ in range(kw["n"]):
            counter["i"] += 1
            txt = prompt_text or (
                f"Synthetic nephrology vignette {counter['i']}: pick the safe dialysate "
                f"potassium and renal drug dosing for patient {uuid.uuid4().hex[:6]}."
            )
            out.append({
                "prompt": txt, "topic": kw["bucket_id"], "subtopic": "x",
                "difficulty": "hard", "ai_failure_mode": "unsafe dosing",
                "capture_reasoning_recommended": False,
            })
        return {"prompts": out, "skipped": False, "model": "stub-pg"}

    async def _cg(prompt, **kw):
        cands = [{"id": "A", "text": "strong"}, {"id": "B", "text": "flawed"}][:candidates]
        return {"candidates": cands, "model": "stub-cg", "intended_flawed_id": intended_flawed}

    judge = {
        "skipped": False, "error_likelihood": 0.9, "revision_value": 0.9,
        "on_specialty": True, "safety_ok": True, "explanation": "", "model": "stub-judge",
    }
    judge.update(judge_overrides or {})

    async def _judge(prompt, candidates):
        return dict(judge)

    monkeypatch.setattr(asc_generation, "run_prompt_gen", _pg)
    monkeypatch.setattr(asc_generation, "generate_candidates_ex", _cg)
    monkeypatch.setattr(asc_generation, "run_prompt_judge", _judge)


def _run(coro):
    return asyncio.run(coro)


def _gen(**kw):
    store = _store()
    defaults = dict(specialty="nephrology", n=3, created_by="admin-1")
    defaults.update(kw)
    return _run(asc_generation.generate_tasks(store, **defaults))


# ─── Happy path + provenance ─────────────────────────────────────────────────
def test_generates_n_with_full_provenance(monkeypatch):
    install_stubs(monkeypatch)
    res = _gen(n=3)
    assert res["accepted"] == 3
    assert len(res["created"]) == 3
    assert res["dropped"] == {}
    store = _store()
    task = store.get_task(res["created"][0])
    gen = task["generation"]
    assert gen["engine"] == "asclepius_seedmaker"
    assert gen["seed_corpus_version"] == "nephrology.v1"
    assert gen["taxonomy_bucket"] in {b.id for b in asc_router.asc_specialties.NEPHROLOGY_TAXONOMY}
    assert gen["seed_exemplars"]
    assert gen["judge"]["error_likelihood"] == 0.9
    # provenance honesty: the v1 corpus is not clinician-ratified yet (P1-C)
    assert gen["seed_corpus_ratified"] is False
    assert res["corpus_ratified"] is False
    assert gen["intended_flawed_id"] == "B"
    # a generation_jobs row was written
    jobs = store.list_generation_jobs()
    assert len(jobs) == 1 and jobs[0]["accepted"] == 3


def test_blinded_task_strips_intended_flawed_id(monkeypatch):
    install_stubs(monkeypatch)
    res = _gen(n=1)
    task = _store().get_task(res["created"][0])
    assert task["generation"]["intended_flawed_id"] == "B"  # stored server-side
    blinded = asc_router._blind_task(task)
    assert "generation" not in blinded
    assert all("generator_model" not in c for c in blinded["candidate_answers"])


def test_generated_task_packages_grounded_false(monkeypatch):
    install_stubs(monkeypatch)
    res = _gen(n=1)
    from asclepius import packaging
    task = _store().get_task(res["created"][0])
    sub = {
        "submission_id": "s1", "verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
        "annotator": {"credentials": "board_certified_nephrology"},
        "payload": {"verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
                    "chosen_revision": {}, "rejected_critique": {}},
        "created_at": "2026-01-01",
    }
    recs = packaging.package_submission(task, sub)
    assert recs and recs[0]["grounded"] is False
    assert recs[0]["generation"]["engine"] == "asclepius_seedmaker"
    assert "intended_flawed_id" not in recs[0]["generation"]


# ─── Gates ───────────────────────────────────────────────────────────────────
def test_contamination_dropped(monkeypatch):
    install_stubs(monkeypatch, prompt_text="A patient presents; which of the following is the most likely diagnosis?")
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("contamination", 0) >= 1


def test_duplicate_vs_corpus_dropped(monkeypatch):
    seed_prompt = asc_corpus.all_prompts("nephrology")[0]
    install_stubs(monkeypatch, prompt_text=seed_prompt)
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("duplicate", 0) >= 1


def test_off_specialty_dropped(monkeypatch):
    install_stubs(monkeypatch, judge_overrides={"on_specialty": False})
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("off_specialty", 0) >= 1


def test_unsafe_dropped(monkeypatch):
    install_stubs(monkeypatch, judge_overrides={"safety_ok": False})
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("unsafe", 0) >= 1


def test_low_error_likelihood_dropped(monkeypatch):
    install_stubs(monkeypatch, judge_overrides={"error_likelihood": 0.1})
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("low_error_likelihood", 0) >= 1


def test_low_revision_value_dropped(monkeypatch):
    install_stubs(monkeypatch, judge_overrides={"revision_value": 0.2})
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("low_revision_value", 0) >= 1


def test_candidate_gen_failed_dropped(monkeypatch):
    install_stubs(monkeypatch, candidates=1)
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("candidate_gen_failed", 0) >= 1


def test_judge_failed_dropped(monkeypatch):
    install_stubs(monkeypatch, judge_overrides={"skipped": True})
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("judge_failed", 0) >= 1


def test_below_min_difficulty_dropped(monkeypatch):
    """A medium prompt in a hard-floor bucket is dropped before candidate-gen (P2-A)."""
    install_stubs(monkeypatch)  # candidate/judge stubs would otherwise accept
    from asclepius.specialties import get_specialty_config
    cfg = get_specialty_config("nephrology")
    hard_bucket = next(b for b in cfg.taxonomy if b.min_difficulty == "hard")
    monkeypatch.setattr(asc_generation, "_bucket_order", lambda c: [hard_bucket])

    async def _pg(**kw):
        return {
            "prompts": [{
                "prompt": f"Unique medium-difficulty vignette {uuid.uuid4().hex}.",
                "topic": kw["bucket_id"], "subtopic": "x", "difficulty": "medium",
                "ai_failure_mode": "x", "capture_reasoning_recommended": False,
            }],
            "skipped": False, "model": "stub-pg",
        }

    monkeypatch.setattr(asc_generation, "run_prompt_gen", _pg)
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("below_min_difficulty", 0) >= 1


def test_near_duplicate_dropped(monkeypatch):
    """A reworded near-duplicate of a seed prompt is fuzzy-dropped (P2-C)."""
    seed = asc_corpus.all_prompts("nephrology")[0]
    near = seed + " Please advise on the plan."  # different hash, ~identical tokens
    install_stubs(monkeypatch, prompt_text=near)
    res = _gen(n=2)
    assert res["accepted"] == 0
    assert res["dropped"].get("near_duplicate", 0) >= 1
    assert res["dropped"].get("duplicate", 0) == 0  # not an exact-hash dup


def test_difficulty_mix_steers_target(monkeypatch):
    """difficulty_mix quotas reach run_prompt_gen as a target difficulty (P2-B)."""
    seen_targets = []

    async def _pg(**kw):
        seen_targets.append(kw.get("difficulty"))
        return {
            "prompts": [{
                "prompt": f"Vignette {uuid.uuid4().hex}.", "topic": kw["bucket_id"],
                "subtopic": "x", "difficulty": kw.get("difficulty") or "hard",
                "ai_failure_mode": "x", "capture_reasoning_recommended": False,
            }],
            "skipped": False, "model": "stub-pg",
        }

    async def _cg(prompt, **kw):
        return {"candidates": [{"id": "A", "text": "s"}, {"id": "B", "text": "f"}],
                "model": "cg", "intended_flawed_id": "B"}

    async def _judge(prompt, candidates):
        return {"skipped": False, "error_likelihood": 0.9, "revision_value": 0.9,
                "on_specialty": True, "safety_ok": True, "explanation": "", "model": "j"}

    monkeypatch.setattr(asc_generation, "run_prompt_gen", _pg)
    monkeypatch.setattr(asc_generation, "generate_candidates_ex", _cg)
    monkeypatch.setattr(asc_generation, "run_prompt_judge", _judge)

    res = _gen(n=4, difficulty_mix={"hard": 1.0})
    assert res["accepted"] == 4
    assert "hard" in seen_targets
    # an all-hard quota never requests an easier difficulty
    assert all(t in (None, "hard") for t in seen_targets)

    # A medium-weighted mix on a medium-floor bucket steers "medium" (no clamp).
    seen_targets.clear()
    from asclepius.specialties import get_specialty_config
    cfg = get_specialty_config("nephrology")
    med_bucket = next(b for b in cfg.taxonomy if b.min_difficulty == "medium")
    monkeypatch.setattr(asc_generation, "_bucket_order", lambda c: [med_bucket])
    A.fresh_store()
    asc_corpus.load_corpus("nephrology", force=True)
    _gen(n=2, difficulty_mix={"medium": 1.0})
    assert "medium" in seen_targets


# ─── No-LLM → disabled (never emit ungated tasks) ─────────────────────────────
def test_no_llm_disables_generation(monkeypatch):
    install_stubs(monkeypatch, skip_pg=True)
    with pytest.raises(asc_generation.GenerationDisabled):
        _gen(n=3)


def test_unknown_specialty_raises(monkeypatch):
    install_stubs(monkeypatch)
    from asclepius.specialties import SpecialtyNotEnabled
    with pytest.raises(SpecialtyNotEnabled):
        _gen(specialty="dermatology", n=1)


# ─── "Did the doctor catch it?" metric (PRD §16) ──────────────────────────────
def _make_evaluator():
    return A.make_user(_store(), role="evaluator", specialty="nephrology",
                       board_cert="board_certified_nephrology", years_experience=10)


def _submit_generated(monkeypatch, *, rejected_id):
    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)

    store = _store()
    task = store.insert_task(
        prompt="generated nephrology prompt", specialty="nephrology", difficulty="hard",
        source="internal_prompt_bank",
        candidate_answers=[{"id": "A", "text": "strong"}, {"id": "B", "text": "flawed"}],
        generation={"engine": "asclepius_seedmaker", "intended_flawed_id": "B",
                    "seed_corpus_version": "nephrology.v1"},
    )
    ev = _make_evaluator()
    sid = f"s-{uuid.uuid4().hex[:8]}"
    chosen = "A" if rejected_id == "B" else "B"
    verdict = "A_better" if chosen == "A" else "B_better"
    sub = store.insert_submission(
        submission_id=sid, task_id=task["task_id"], evaluator_id=ev["id"],
        verdict=verdict, chosen_id=chosen, rejected_id=rejected_id, confidence="high",
        time_spent_sec=120,
        payload={"verdict": verdict, "chosen_id": chosen, "rejected_id": rejected_id,
                 "chosen_revision": {}, "rejected_critique": {}},
        annotator=store.annotator_block(ev), dedupe_hash=f"h-{sid}",
        grounded=False, grounding_mode="optional", status="submitted",
    )
    _run(asc_pipeline.process_submission(store, task, sub))
    return sid


def test_caught_flaw_true_when_flawed_rejected(monkeypatch):
    sid = _submit_generated(monkeypatch, rejected_id="B")
    store = _store()
    assert store.get_submission(sid)["caught_flaw"] == 1
    rate = store.flaw_catch_rate()
    assert rate["scored"] == 1 and rate["caught"] == 1 and rate["rate"] == 1.0


def test_caught_flaw_false_when_flawed_chosen(monkeypatch):
    sid = _submit_generated(monkeypatch, rejected_id="A")
    store = _store()
    assert store.get_submission(sid)["caught_flaw"] == 0
    rate = store.flaw_catch_rate()
    assert rate["scored"] == 1 and rate["caught"] == 0 and rate["rate"] == 0.0


# ─── Router auth + endpoints ──────────────────────────────────────────────────
def test_router_generation_requires_admin(monkeypatch):
    install_stubs(monkeypatch)
    ev = _make_evaluator()
    r = client.post("/api/asclepius/generation/nephrology",
                    json={"count": 1}, headers=A.headers_for(ev))
    assert r.status_code == 403


def test_router_generation_admin_ok(monkeypatch):
    install_stubs(monkeypatch)
    admin = A.make_user(_store(), role="admin")
    r = client.post("/api/asclepius/generation/nephrology",
                    json={"count": 2}, headers=A.headers_for(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 2 and body["job_id"]


def test_router_disabled_specialty_400(monkeypatch):
    install_stubs(monkeypatch)
    admin = A.make_user(_store(), role="admin")
    r = client.post("/api/asclepius/generation/dermatology",
                    json={"count": 1}, headers=A.headers_for(admin))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "specialty_not_enabled"


def test_router_seed_corpus_and_specialties(monkeypatch):
    admin = A.make_user(_store(), role="admin")
    r = client.get("/api/asclepius/generation/seed-corpus", headers=A.headers_for(admin))
    assert r.status_code == 200
    assert r.json()["version"] == "nephrology.v1" and r.json()["total"] == 100
    r2 = client.get("/api/asclepius/specialties", headers=A.headers_for(admin))
    specs = {s["specialty"]: s["enabled"] for s in r2.json()["specialties"]}
    assert specs["nephrology"] is True


def test_stats_exposes_flaw_catch_rate(monkeypatch):
    _submit_generated(monkeypatch, rejected_id="B")
    admin = A.make_user(_store(), role="admin")
    r = client.get("/api/asclepius/stats", headers=A.headers_for(admin))
    assert r.status_code == 200
    assert r.json()["flaw_catch_rate"]["rate"] == 1.0


# ─── Buyer-request spec-only path invokes the engine ──────────────────────────
def test_buyer_request_spec_only_invokes_engine(monkeypatch):
    install_stubs(monkeypatch)
    admin = A.make_user(_store(), role="admin")
    buyer = client.post("/api/asclepius/buyers", json={"name": "LabCo"},
                        headers=A.headers_for(admin)).json()
    req = client.post("/api/asclepius/buyer-requests", json={
        "buyer_id": buyer["buyer_id"], "source": "internal_prompt_bank",
        "specialty": "nephrology", "difficulty": "hard",
    }, headers=A.headers_for(admin)).json()
    r = client.post(f"/api/asclepius/buyer-requests/{req['request_id']}/batch",
                    json={"count": 2}, headers=A.headers_for(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["generation"]["accepted"] == 2
    # every generated task is stamped to the buyer request
    store = _store()
    for tid in body["created"]:
        assert store.get_task(tid)["buyer_request_id"] == req["request_id"]


# ─── A/B position randomization (Eval Flow Upgrade §5) ────────────────────────
def test_candidate_ab_position_is_randomized(monkeypatch):
    """The model always marks "B" as the intended-flawed answer; the server must
    randomize the A/B slot per task so the flawed answer isn't position-biased,
    while keeping the flawed marker tied to its TEXT (never the slot)."""
    payload = ('{"candidate_answers":[{"id":"A","text":"strong"},'
               '{"id":"B","text":"weak"}],"intended_flawed_id":"B"}')

    async def _call(**kw):
        return ("RESP", {"model": "stub-cg"})

    monkeypatch.setattr(_llm, "call_llm", _call)
    monkeypatch.setattr(_llm, "first_text", lambda resp: payload)

    _random.seed(12345)
    flawed_slots = set()
    for _ in range(50):
        res = _run(asc_critic.generate_candidates_ex("prompt", specialty="nephrology"))
        fid = res["intended_flawed_id"]
        assert fid in ("A", "B")
        # both distinct texts always present
        assert {c["text"] for c in res["candidates"]} == {"strong", "weak"}
        # the flawed marker follows the 'weak' TEXT regardless of slot
        ftext = next(c["text"] for c in res["candidates"] if c["id"] == fid)
        assert ftext == "weak"
        flawed_slots.add(fid)
    assert flawed_slots == {"A", "B"}  # position genuinely varies across tasks
