"""Asclepius V3 (Seamless + Hard-Cases PRD) — PR1 tests.

Covers the V3 "box" scaffolding + the two must-fix workstreams shipped in PR1:
  * WS1 backend: the ~10s ``instinct`` capture kind (V3 default) rides the record
    as a lightweight anchoring field, never a gold blind ideal; ``full`` still
    packages the premium record. V1/V2 kinds are unchanged.
  * WS6: A/B slot randomization is ~50/50 (regression) + the observed
    A-is-stronger QC rate is exposed on ``/stats``.
  * WS7: ``/transcribe`` returns the transcript text (dictation smoke test).
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402
from asclepius.constants import independent_capture_kind  # noqa: E402
from asclepius.packaging import package_submission  # noqa: E402

client = TestClient(A.app)


# ─── WS1 backend: instinct capture kind (pure) ────────────────────────────────
def test_capture_kind_by_portal_version():
    # V3 defaults to the ~10s instinct one-liner; only an explicit 'full' task upgrades.
    assert independent_capture_kind("v3", "stance") == "instinct"
    assert independent_capture_kind("v3", None) == "instinct"
    assert independent_capture_kind("v3", "full") == "full"
    # V1/V2 unchanged.
    assert independent_capture_kind("v1", "stance") == "full"
    assert independent_capture_kind("v2", "stance") == "stance"
    assert independent_capture_kind("v2", "full") == "full"
    # Unknown version coerces to the default (v3).
    assert independent_capture_kind("v9", "stance") == "instinct"


def _task(**kw):
    base = {
        "task_id": "t-v3-1", "specialty": "nephrology", "difficulty": "hard",
        "source": "lab_supplied", "prompt": "K+ 6.4 on HD, peaked T-waves. Manage.",
        "candidate_answers": [
            {"id": "A", "text": "Calcium gluconate, then dialyze."},
            {"id": "B", "text": "Dialysate K+ 1.0 immediately."},
        ],
    }
    base.update(kw)
    return base


def _submission(payload, **kw):
    base = {
        "submission_id": "s-v3-1", "task_id": "t-v3-1", "verdict": payload.get("verdict"),
        "chosen_id": payload.get("chosen_id"), "rejected_id": payload.get("rejected_id"),
        "confidence": "high", "created_at": "2026-07-07T00:00:00",
        "annotator": {"id_hashed": "abc", "credentials": "board_certified_nephrology"},
        "payload": payload,
    }
    base.update(kw)
    return base


def test_v3_instinct_rides_as_lightweight_field_not_gold():
    """A V3 instinct one-liner ships as the record's ``stance`` context field with
    ``independent_kind='instinct'`` — NOT as a premium blind ideal_answer record."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
        "independent_answer": {"text": "continue reduced-dose calcium; dialyze", "kind": "instinct"},
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
        "portal_version": "v3",
    }
    recs = package_submission(_task(independent_mode="stance"), _submission(payload, portal_version="v3"))
    pref = [r for r in recs if r["type"] == "preference"][0]
    assert pref["stance"] == "continue reduced-dose calcium; dialyze"
    assert pref["independent_kind"] == "instinct"
    # No independent blind-gold ideal_answer record was produced from an instinct.
    assert not any(r["type"] == "ideal_answer" and r.get("independent") for r in recs)


def test_v3_full_task_still_packages_blind_gold():
    """An admin-marked 'full' V3 task still yields the premium independent ideal."""
    payload = {
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
        "independent_answer": {"text": "Stabilize membrane with IV calcium, shift K+, then dialyze.", "kind": "full"},
        "chosen_revision": {"edited": False},
        "rejected_critique": {"error_tags": ["dosing_error"]},
        "portal_version": "v3",
    }
    recs = package_submission(_task(independent_mode="full"), _submission(payload, portal_version="v3"))
    assert any(r["type"] == "ideal_answer" and r.get("independent") for r in recs)


# ─── WS6: A/B randomization + QC rate ─────────────────────────────────────────
def test_ab_slot_randomization_is_balanced(monkeypatch):
    """The intended-flawed answer lands in slot A ~50% of the time over many
    builds (position-bias regression). The LLM is stubbed; only the server-side
    ``random.shuffle`` slotting is exercised."""
    import ai.llm_client as llm
    from asclepius import critic

    async def fake_call_llm(**kw):
        return ({"stub": True}, {"model": "stub-model"})

    def fake_first_text(resp):
        return json.dumps({
            "candidate_answers": [{"id": "A", "text": "strong answer"}, {"id": "B", "text": "flawed answer"}],
            "intended_flawed_id": "B",
        })

    monkeypatch.setattr(llm, "call_llm", fake_call_llm)
    monkeypatch.setattr(llm, "first_text", fake_first_text)

    import random

    async def _run_many(n):
        return [await critic.generate_candidates_ex("prompt", specialty="nephrology") for _ in range(n)]

    # Seed deterministically for a non-flaky assertion, but SAVE/RESTORE the global
    # random state so this test never leaks determinism into the rest of the suite.
    saved = random.getstate()
    random.seed(20260707)
    try:
        n = 500
        results = asyncio.run(_run_many(n))
    finally:
        random.setstate(saved)
    flawed_in_a = sum(1 for res in results if res["intended_flawed_id"] == "A")
    rate = flawed_in_a / n
    # Balanced within a generous band (seeded, so deterministic).
    assert 0.42 < rate < 0.58, f"A/B slot not balanced: flawed-in-A rate={rate}"


def test_ab_balance_stats_reports_a_stronger_rate():
    store = A.fresh_store()
    # flawed in B (stronger in A) ×3, flawed in A (stronger in B) ×1 → a_stronger_rate 0.75
    for fid, strong in (("B", "A"), ("B", "A"), ("B", "A"), ("A", "B")):
        store.insert_task(
            prompt=f"case {uuid.uuid4().hex[:6]}", specialty="nephrology",
            candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
            generation={"mode": "test", "intended_flawed_id": fid},
        )
    bal = store.ab_balance_stats()
    assert bal["n"] == 4
    assert bal["a_stronger"] == 3
    assert bal["a_stronger_rate"] == 0.75


def test_stats_exposes_ab_balance():
    store = A.fresh_store()
    admin = A.make_user(store, role="admin")
    s = client.get("/api/asclepius/stats", headers=A.headers_for(admin)).json()
    assert "ab_balance" in s
    assert set(s["ab_balance"].keys()) == {"n", "a_stronger", "a_stronger_rate"}


def _store():
    from asclepius.store import get_store
    return get_store()


# ─── V3 end-to-end through the API ────────────────────────────────────────────
@pytest.fixture()
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


def test_v3_submission_stamps_v3_and_instinct(_isolated):
    """A V3 submission is stamped portal_version='v3' on the row and every record,
    and its instinct one-liner rides as the lightweight stance field (never a
    blind-gold ideal)."""
    admin = A.make_user(_store(), role="admin")
    ev = A.make_user(_store(), role="evaluator", specialty="nephrology",
                     board_cert="board_certified_nephrology", years_experience=10)
    admin_h, ev_h = A.headers_for(admin), A.headers_for(ev)
    r = client.post("/api/asclepius/tasks", json={"tasks": [{
        "specialty": "nephrology", "difficulty": "hard", "source": "lab_supplied",
        "prompt": "72yo on HD, K+ 6.4 with peaked T-waves. Adjust dialysate and meds?",
        "candidate_answers": [
            {"id": "A", "text": "Calcium gluconate, then dialyze with K+ 2.0.", "generator_model": "mx"},
            {"id": "B", "text": "Dialysate K+ 1.0 immediately.", "generator_model": "my"},
        ],
    }]}, headers=admin_h)
    tid = r.json()["created"][0]

    body = {
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "confidence": "high", "time_spent_sec": 90,
        "portal_version": "v3",
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "calcium first, then shift K+, then dialyze", "kind": "instinct"},
        "chosen_revision": {"edited": True, "revised_text": "IV calcium, insulin/dextrose, then HD.", "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "over-lowers K+"},
    }
    res = client.post("/api/asclepius/submissions", json=body, headers=ev_h)
    assert res.status_code == 200, res.text
    sid = res.json()["submission_id"]

    sub = _store().get_submission(sid)
    assert sub["portal_version"] == "v3"
    recs = _store().records_for_submission(sid)
    assert recs and all(r["payload"]["portal_version"] == "v3" for r in recs)
    pref = [r for r in recs if r["type"] == "preference"][0]["payload"]
    assert pref["stance"] == "calcium first, then shift K+, then dialyze"
    assert pref["independent_kind"] == "instinct"
    # An instinct one-liner never becomes a premium blind-gold ideal record.
    assert not any(r["payload"].get("independent") for r in recs if r["type"] == "ideal_answer")


# ─── WS7: dictation transcribe smoke test ─────────────────────────────────────
@pytest.fixture()
def _fresh():
    A.fresh_store()
    asc_profiles.clear_cache()
    yield


def test_transcribe_returns_text(monkeypatch, _fresh):
    """A mocked provider transcript is returned by /transcribe so the frontend
    mic can write it into the focused field (dictation smoke test)."""
    from asclepius import stt as asc_stt

    async def fake_transcribe(data, mime="audio/webm"):
        assert data  # audio bytes were forwarded
        return {"text": "reduce metformin dose and recheck eGFR in three months", "provider": "deepgram", "skipped": False}

    monkeypatch.setattr(asc_stt, "transcribe", fake_transcribe)
    ev_h = A.headers_for(A.make_user(_store(), role="evaluator", specialty="nephrology"))
    files = {"file": ("dictation.webm", io.BytesIO(b"fake-audio-bytes"), "audio/webm")}
    r = client.post("/api/asclepius/transcribe", files=files, headers=ev_h)
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "reduce metformin dose and recheck eGFR in three months"


def test_transcribe_degrades_to_503_when_no_provider(monkeypatch, _fresh):
    from asclepius import stt as asc_stt

    async def fake_transcribe(data, mime="audio/webm"):
        return {"text": "", "provider": None, "skipped": True, "error": "no_stt_provider_configured"}

    monkeypatch.setattr(asc_stt, "transcribe", fake_transcribe)
    ev_h = A.headers_for(A.make_user(_store(), role="evaluator", specialty="nephrology"))
    files = {"file": ("dictation.webm", io.BytesIO(b"x"), "audio/webm")}
    r = client.post("/api/asclepius/transcribe", files=files, headers=ev_h)
    assert r.status_code == 503
