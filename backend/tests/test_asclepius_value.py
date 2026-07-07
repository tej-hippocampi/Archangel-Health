"""Value-per-Minute tests (PRD Parts A–D).

Two layers:
  * unit — the pure value model (``asclepius.value``) checked against the PRD §A3
    worked scenarios (baseline = 7.0:1, V2 typical ≥ 10:1, grounded / hard /
    Mode-B premiums, the tier cap, projected = × reuse).
  * integration — value is persisted per submission, surfaced on
    ``/metrics/value-per-time`` + ``/stats``, value-aware ``/tasks/next`` routing
    fires ONLY for the v2 flow (the "edits only on V2" guarantee), and the
    assist override rate (Part D rubber-stamp guard) is reported.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402
from asclepius.value import (  # noqa: E402
    estimate_value,
    expected_value_for_task,
    routing_score,
    value_per_minute,
)

client = TestClient(A.app)


# ─── Unit: the value model against the §A3 worked scenarios ───────────────────
def _records(*, preference=True, ideal=True, reasoning=True, step_pairs=0, grounded=False, independent_full=False):
    recs = []
    if preference:
        recs.append({"type": "preference", "grounded": grounded})
    if ideal:
        recs.append({"type": "ideal_answer", "grounded": grounded, "independent": independent_full})
    if reasoning:
        recs.append({"type": "reasoning_trace", "grounded": grounded, "step_pairs": [{"i": i} for i in range(step_pairs)]})
    return recs


def _task(difficulty="medium", source="internal_prompt_bank", independent_mode="stance",
          max_labels=1, grounding_mode="optional", capture_reasoning=False):
    return {
        "difficulty": difficulty, "source": source, "independent_mode": independent_mode,
        "max_labels": max_labels, "grounding_mode": grounding_mode,
        "capture_reasoning": capture_reasoning,
    }


def _sub(agreement_score=None, credentials="board_certified_nephrology", grounded=False):
    return {"annotator": {"credentials": credentials}, "agreement_score": agreement_score,
            "grounded": grounded, "payload": {}}


def test_baseline_scenario_is_seven_to_one():
    """§A3 baseline: medium, plain, off-policy (Mode A), stance, single, 10 min."""
    est = estimate_value(_records(step_pairs=0), _task(), _sub())
    assert est["content_value"] == 70.0
    assert est["tier_mult"] == 1.0
    assert est["realized_value"] == 70.0
    assert value_per_minute(est["realized_value"], 600) == 7.0


def test_v2_typical_clears_ten_to_one():
    """§A3 V2 typical: medium, plain, 1 step-pair, 7 min → ≥ 10:1 on the time cut
    alone (the acceptance-criteria floor)."""
    est = estimate_value(_records(step_pairs=1), _task(), _sub())
    assert est["content_value"] == 76.0
    assert est["realized_value"] == 76.0
    assert value_per_minute(est["realized_value"], 7 * 60) >= 10.0


def test_grounded_premium_multiplier():
    """§A3 V2 grounded: the grounded SKU adds a 1.30× premium."""
    est = estimate_value(_records(step_pairs=1, grounded=True), _task(grounding_mode="required"), _sub())
    assert est["tier_mult"] == 1.3
    assert est["realized_value"] == 98.8  # 76 × 1.30
    assert value_per_minute(98.8, int(7.5 * 60)) == round(98.8 / 7.5, 2)


def test_hard_grounded_stacks_difficulty_and_grounding():
    """§A3 V2 hard + grounded: 1.30 × 1.40 = 1.82×, content 82 (2 step-pairs)."""
    est = estimate_value(_records(step_pairs=2, grounded=True), _task(difficulty="hard"), _sub())
    assert est["content_value"] == 82.0
    assert round(est["tier_mult"], 2) == 1.82
    assert est["realized_value"] == round(82 * 1.82, 2)


def test_mode_b_hits_the_tier_cap():
    """§A3 V2 Mode B: 1.30 × 1.40 × 1.50 = 2.73 → capped at 2.50."""
    est = estimate_value(_records(step_pairs=2, grounded=True),
                         _task(difficulty="hard", source="lab_supplied"), _sub())
    assert est["tier_mult"] == 2.5
    assert est["realized_value"] == round(82 * 2.5, 2)


def test_double_labeled_credentialed_kappa_premium():
    est = estimate_value(_records(), _task(max_labels=2), _sub(agreement_score=1.0))
    assert round(est["tier_mult"], 2) == 1.15
    # An un-credentialed double-label does NOT earn the κ premium.
    plain = estimate_value(_records(), _task(max_labels=2), _sub(agreement_score=1.0, credentials=None))
    assert plain["tier_mult"] == 1.0


def test_projected_is_realized_times_reuse():
    est = estimate_value(_records(step_pairs=1), _task(), _sub())
    assert est["projected_value"] == round(est["realized_value"] * 1.5, 2)


def test_value_per_minute_is_undefined_without_time():
    assert value_per_minute(70.0, 0) is None
    assert value_per_minute(70.0, None) is None


def test_routing_prefers_higher_expected_value_per_minute():
    """B3: a hard on-policy reasoning task outranks an easy off-policy one for the
    same clinician speed."""
    hard = _task(difficulty="hard", source="lab_supplied", capture_reasoning=True)
    easy = _task(difficulty="easy", source="internal_prompt_bank")
    assert routing_score(hard, 7 * 60) > routing_score(easy, 7 * 60)
    # expected value is a pure forward estimate (no records needed)
    assert expected_value_for_task(hard)["realized_value"] > expected_value_for_task(easy)["realized_value"]


# ─── Integration ──────────────────────────────────────────────────────────────
_IDEAL = {"text": "Stabilize the myocardium with IV calcium, shift potassium with insulin and dextrose, then dialyze."}


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


def _seed(specialty="nephrology", **kw):
    return A.make_user(_store(), role="evaluator", specialty=specialty,
                       board_cert="board_certified_nephrology", years_experience=12, **kw)


def _admin():
    return A.make_user(_store(), role="admin")


def _task_body(**kw):
    base = {
        "specialty": "nephrology", "difficulty": "hard", "capture_reasoning": False,
        "source": "lab_supplied", "max_labels": 1, "grounding_mode": "optional",
        "prompt": "72yo on HD, K+ 6.4 with peaked T-waves. Adjust dialysate and meds?",
        "candidate_answers": [
            {"id": "A", "text": "Give calcium gluconate, then dialyze with K+ 2.0.", "generator_model": "model_x"},
            {"id": "B", "text": "Set dialysate K+ to 1.0 immediately.", "generator_model": "model_y"},
        ],
    }
    base.update(kw)
    return base


def _upload(admin_h, **kw):
    r = client.post("/api/asclepius/tasks", json={"tasks": [_task_body(**kw)]}, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def _submit(ev_h, tid, *, verdict="A_better", time_spent_sec=140, portal_version="v2", **extra):
    body = {
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid, "verdict": verdict,
        "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "time_spent_sec": time_spent_sec, "portal_version": portal_version,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": _IDEAL,
        "chosen_revision": {"edited": True, "revised_text": "Calcium first, then insulin/dextrose, then dialyze.",
                            "why_better_notes": "B over-lowers K+", "why_better_tags": ["safer"]},
        "rejected_critique": {"error_tags": ["dosing_error"], "severities": {}, "why_worse": "too aggressive"},
    }
    body.update(extra)
    r = client.post("/api/asclepius/submissions", json=body, headers=ev_h)
    assert r.status_code == 200, r.text
    return r.json()


def test_submission_persists_and_returns_value_estimate():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload(admin_h)
    res = _submit(ev_h, tid, time_spent_sec=420)
    assert res["value_estimate_usd"] is not None
    assert res["value_estimate_projected_usd"] == round(res["value_estimate_usd"] * 1.5, 2)
    # Persisted on the row.
    sub = _store().get_submission(res["submission_id"])
    assert sub["value_estimate_usd"] == res["value_estimate_usd"]
    assert sub["clinician_review_seconds"] == 420


def test_metrics_value_per_time_reports_realized_and_projected():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload(admin_h)
    _submit(ev_h, tid, time_spent_sec=420)

    m = client.get("/api/asclepius/metrics/value-per-time", headers=admin_h)
    assert m.status_code == 200, m.text
    body = m.json()
    vpt = body["value_per_time"]
    assert vpt["overall"]["n"] == 1
    assert vpt["overall"]["realized_vpm"] is not None
    # Split by product version has a v2 bucket (the flow we submitted under).
    assert "v2" in vpt["by_portal_version"]
    assert body["target_realized_vpm"] == 10.0
    # κ + override rate ride alongside V/T (Part D).
    assert "kappa" in body and "override_rate" in body


def test_stats_includes_value_per_time_summary():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload(admin_h)
    _submit(ev_h, tid, time_spent_sec=420)
    s = client.get("/api/asclepius/stats", headers=admin_h).json()
    assert "value_per_time" in s
    assert s["value_per_time_target"] == 10.0
    assert s["value_per_time"]["overall"]["n"] == 1
    assert "override_rate" in s


def test_value_aware_routing_is_v2_only():
    """The "edits only on V2" guarantee: value-aware routing reorders the queue
    ONLY when the request declares the v2 flow. Absent or v1 → classic oldest-
    first, byte-for-byte unchanged."""
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    # Low-value task inserted FIRST (so it is the oldest), high-value SECOND.
    low = _upload(admin_h, difficulty="easy", source="internal_prompt_bank", capture_reasoning=False)
    high = _upload(admin_h, difficulty="hard", source="lab_supplied", capture_reasoning=True)

    # Classic (no param): oldest wins — V1 behavior is untouched.
    classic = client.get("/api/asclepius/tasks/next", headers=ev_h).json()["task"]
    assert classic["task_id"] == low
    # Explicit v1 is still classic.
    v1 = client.get("/api/asclepius/tasks/next?portal_version=v1", headers=ev_h).json()["task"]
    assert v1["task_id"] == low
    # An empty or typo'd param must NOT silently opt into value-aware routing
    # (only the literal "v2" does) — otherwise a v1/garbage request gets a
    # reordered queue.
    empty = client.get("/api/asclepius/tasks/next?portal_version=", headers=ev_h).json()["task"]
    assert empty["task_id"] == low
    typo = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=ev_h).json()["task"]
    assert typo["task_id"] == low
    # v2: value-aware routing serves the higher expected value-per-minute task.
    v2 = client.get("/api/asclepius/tasks/next?portal_version=v2", headers=ev_h).json()["task"]
    assert v2["task_id"] == high


def test_override_rate_flags_assist_disagreement():
    """Part D rubber-stamp guard: when the clinician's final verdict differs from
    the model's suggestion, the override rate reflects it (a near-zero rate would
    flag rubber-stamping)."""
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload(admin_h)
    # Assist suggested B_better; clinician chose A_better → an override.
    _submit(ev_h, tid, verdict="A_better",
            assist={"prelabeled": True, "suggested_verdict": "B_better", "confidence": 0.9})
    ov = _store().override_rate_stats(portal_version="v2")
    assert ov["verdict"]["assisted"] == 1
    assert ov["verdict"]["override_rate"] == 1.0


def test_value_is_measured_for_v1_without_changing_its_flow():
    """V1 submissions are still MEASURED (the metric needs the baseline) — but
    nothing about the v1 capture changes: it produces records and a value
    estimate exactly like before, just tagged v1."""
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload(admin_h)
    res = _submit(ev_h, tid, time_spent_sec=600, portal_version="v1")
    assert res["value_estimate_usd"] is not None
    sub = _store().get_submission(res["submission_id"])
    assert sub["portal_version"] == "v1"
    assert sub["value_estimate_usd"] is not None
