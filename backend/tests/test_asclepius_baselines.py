"""Frontier-model failure capture tests (FEAT-1): baseline runs stored, the
grade-real-models mode blinds source/baseline_model, and the per-model failure
record + Model-Failure artifact."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from asclepius import baselines as B  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    A.fresh_store()

    async def _ok(*a, **k):
        return {"consistent": True, "grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok)
    yield


def _stub_llm(monkeypatch, answers):
    """Stub ai.llm_client so each baseline model returns a canned answer."""
    import ai.llm_client as llm
    calls = {"n": 0}

    class _Resp:
        class usage:
            input_tokens = 100
            output_tokens = 50

    async def fake_call(**kw):
        calls["n"] += 1
        model = kw.get("model")
        return _Resp(), {"model": model, "latency_ms": 42}

    monkeypatch.setattr(llm, "call_llm", fake_call)
    monkeypatch.setattr(llm, "first_text", lambda resp: answers[(calls["n"] - 1) % len(answers)])
    return calls


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin", email=f"a-{uuid.uuid4().hex[:6]}@x.example"))


def _ev_h():
    return A.headers_for(A.make_user(_store(), role="evaluator", specialty="nephrology",
                                     board_cert="board_certified_nephrology", years_experience=12))


def _make_task(admin_h):
    body = {"specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
            "prompt": f"Hyperkalemia case {A.uniq(6)}: K+ 6.4, peaked T-waves. Plan?",
            "candidate_answers": [{"id": "A", "text": "gen-A"}, {"id": "B", "text": "gen-B"}]}
    return client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h).json()["created"][0]


def test_run_baselines_stores_verbatim_runs(monkeypatch):
    _stub_llm(monkeypatch, ["Give calcium then dialyze.", "Set dialysate 1K now."])
    monkeypatch.setenv("ASCLEPIUS_BASELINE_MODELS", "model-x,model-y")
    admin_h = _admin_h()
    tid = _make_task(admin_h)
    r = client.post(f"/api/asclepius/tasks/{tid}/baselines", headers=admin_h)
    assert r.status_code == 200, r.text
    runs = r.json()["runs"]
    assert {x["model"] for x in runs} == {"model-x", "model-y"}
    assert all(x["response_text"] for x in runs)
    # Stored + listable.
    assert len(_store().list_baseline_runs(task_id=tid)) == 2


def test_grade_real_models_blinds_source_and_records_failure(monkeypatch):
    _stub_llm(monkeypatch, ["Baseline answer ONE: give 1K dialysate immediately.",
                            "Baseline answer TWO: IV calcium then insulin/dextrose then dialyze."])
    monkeypatch.setenv("ASCLEPIUS_BASELINE_MODELS", "gpt-x,claude-y")
    admin_h = _admin_h()
    ev_h = _ev_h()
    tid = _make_task(admin_h)

    swap = client.post(f"/api/asclepius/tasks/{tid}/grade-real-models", headers=admin_h)
    assert swap.status_code == 200, swap.text
    assert swap.json()["candidate_count"] == 2

    # The blinded task must NOT leak source / baseline_model to the evaluator.
    nxt = client.get(f"/api/asclepius/tasks/{tid}", headers=ev_h).json()["task"]
    for c in nxt["candidate_answers"]:
        assert set(c.keys()) <= {"id", "text"}

    # Grade: reject whichever candidate is the "1K dialysate" (the wrong one).
    task = _store().get_task(tid)
    ans = client.post(f"/api/asclepius/tasks/{tid}/reveal",
                      json={"text": "My plan: IV calcium, insulin/dextrose, then dialyze."},
                      headers=ev_h).json()["answers"]
    reject_id = next(a["id"] for a in ans if "1K dialysate" in a["text"])
    chosen_id = "A" if reject_id == "B" else "B"
    sid = "s-" + uuid.uuid4().hex[:12]
    sub = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": f"{chosen_id}_better",
        "chosen_id": chosen_id, "rejected_id": reject_id, "confidence": "high", "time_spent_sec": 150,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "IV calcium, insulin/dextrose, then dialyze given the ESRD."},
        "chosen_revision": {"edited": False, "why_better_notes": "stabilizes first"},
        "rejected_critique": {"error_tags": ["unsafe_recommendation"], "why_worse": "1K bath is arrhythmogenic"},
    }, headers=ev_h)
    assert sub.status_code == 200, sub.text

    # A per-model failure record for the rejected baseline model was persisted.
    failures = client.get("/api/asclepius/baselines/model-failures", headers=admin_h).json()
    assert failures["failures"], failures
    f0 = failures["failures"][0]
    assert f0["model"] in ("gpt-x", "claude-y")
    assert "unsafe_recommendation" in f0["error_tags"]
    assert "calcium" in (f0["expert_correction"] or "").lower()
    assert any(s["model"] == f0["model"] for s in failures["summary"])


def test_build_baseline_candidates_needs_two():
    assert B.build_baseline_candidates([{"response_text": "only one", "model": "m"}]) == []
    pair = B.build_baseline_candidates(
        [{"response_text": "a", "model": "m1"}, {"response_text": "b", "model": "m2"}])
    assert {c["id"] for c in pair} == {"A", "B"}
    assert all(c["source"] == "baseline" for c in pair)
