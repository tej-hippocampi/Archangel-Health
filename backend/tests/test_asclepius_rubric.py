"""Rubric capture tests (FEAT-2): auto-seed, packaging record, value, grader
export, and the suggest endpoint."""

from __future__ import annotations

import json
import sys
import uuid
import zipfile
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import rubric as R  # noqa: E402
from asclepius.packaging import package_submission  # noqa: E402
from asclepius.value import estimate_value  # noqa: E402

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


# ─── Auto-seed ────────────────────────────────────────────────────────────────
def test_propose_rubric_seeds_from_tags():
    task = {"task_id": "t", "specialty": "nephrology"}
    payload = {
        "verdict": "A_better",
        "rejected_critique": {
            "error_tags": ["dosing_error", "unsafe_recommendation"],
            "severities": {"dosing_error": "high"},
            "error_tag_reasons": {"dosing_error": "dose_too_high"},
        },
        "chosen_revision": {"why_better_tags": ["safer"]},
        "reasoning_steps": [
            {"text": "Stabilize the myocardium with IV calcium", "confirmed": True},
            {"text": "Give K+ 2.0 dialysate", "corrected": True, "original_text": "Set dialysate to 1K immediately"},
        ],
    }
    crit = R.propose_rubric(task, payload)
    sources = [c["source"] for c in crit]
    assert any(s.startswith("error_tag:dosing_error") for s in sources)
    assert any(s == "why_better:safer" for s in sources)
    assert any(s == "good_step" for s in sources)
    assert any(s == "corrected_step" for s in sources)
    # High-severity dosing error → −8; safety error tag → safety axis.
    dosing = next(c for c in crit if c["source"].startswith("error_tag:dosing_error"))
    assert dosing["points"] == -8.0 and dosing["axis"] == "accuracy"
    unsafe = next(c for c in crit if c["source"] == "error_tag:unsafe_recommendation")
    assert unsafe["axis"] == "safety" and unsafe["points"] < 0


def test_normalize_rubric_drops_empty_and_zero():
    got = R.normalize_rubric([
        {"text": "keep", "points": 5, "axis": "accuracy"},
        {"text": "", "points": 5},
        {"text": "zero", "points": 0},
        {"text": "bad axis normalizes", "points": -3, "axis": "nonsense"},
    ])
    assert [c["text"] for c in got] == ["keep", "bad axis normalizes"]
    assert got[1]["axis"] == "accuracy"  # unknown axis coerced to default


# ─── Packaging + value ────────────────────────────────────────────────────────
def _submission_with_rubric():
    task = {"task_id": "t1", "specialty": "nephrology", "difficulty": "hard", "source": "lab_supplied",
            "prompt": "Hyperkalemia management?",
            "candidate_answers": [{"id": "A", "text": "calcium then dialyze"}, {"id": "B", "text": "1K bath"}]}
    submission = {"submission_id": "s1", "task_id": "t1", "verdict": "A_better", "chosen_id": "A",
                  "rejected_id": "B", "confidence": "high", "created_at": "2026-07-07T00:00:00",
                  "annotator": {"id_hashed": "x", "credentials": "board_certified_nephrology"},
                  "payload": {"verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "portal_version": "v3",
                              "independent_answer": {"text": "calcium first", "kind": "instinct"},
                              "chosen_revision": {"edited": False, "why_better_notes": "safer"},
                              "rejected_critique": {"error_tags": ["dosing_error"]},
                              "rubric": [
                                  {"text": "A correct answer stabilizes with IV calcium first.", "points": 8, "axis": "safety", "source": "manual"},
                                  {"text": "A correct answer never sets a 1K dialysate for modest hyperkalemia.", "points": -6, "axis": "safety", "source": "error_tag:dosing_error"},
                                  {"text": "junk", "points": 0},
                              ]}}
    return task, submission


def test_packaging_emits_rubric_record():
    task, submission = _submission_with_rubric()
    recs = package_submission(task, submission)
    rub = [r for r in recs if r["type"] == "rubric"]
    assert len(rub) == 1
    r = rub[0]
    assert len(r["criteria"]) == 2  # zero-point junk dropped
    assert r["max_points"] == 8.0   # only positive points count toward the ceiling
    assert r["n_negative"] == 1 and r["n_positive"] == 1
    assert r["annotator_credential"] == "board_certified_nephrology"  # provenance rides


def test_rubric_adds_marginal_value():
    task, submission = _submission_with_rubric()
    recs = package_submission(task, submission)
    with_rubric = estimate_value([r for r in recs], task, submission)
    without = estimate_value([r for r in recs if r["type"] != "rubric"], task, submission)
    assert with_rubric["breakdown"]["has_rubric"] is True
    assert with_rubric["content_value"] > without["content_value"]


# ─── Endpoint + full flow + export ────────────────────────────────────────────
def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin", email=f"a-{uuid.uuid4().hex[:6]}@x.example"))


def _ev_h():
    return A.headers_for(A.make_user(_store(), role="evaluator", specialty="nephrology",
                                     board_cert="board_certified_nephrology", years_experience=12))


def test_rubric_suggest_endpoint_and_export_ships_grader():
    admin_h, ev_h = _admin_h(), _ev_h()
    body = {"specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
            "prompt": f"Hyperkalemia {A.uniq(6)}?",
            "candidate_answers": [{"id": "A", "text": "IV calcium then dialyze"},
                                  {"id": "B", "text": "Set dialysate K+ 1.0"}]}
    tid = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h).json()["created"][0]
    client.get("/api/asclepius/tasks/next", headers=ev_h)

    # Auto-seed suggestion from draft tags.
    sug = client.post("/api/asclepius/rubric/suggest", json={
        "task_id": tid, "verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
        "rejected_critique": {"error_tags": ["dosing_error"], "error_tag_reasons": {"dosing_error": "dose_too_high"}},
        "chosen_revision": {"why_better_tags": ["safer"]},
    }, headers=ev_h)
    assert sug.status_code == 200, sug.text
    seeded = sug.json()["criteria"]
    assert seeded and any(c["points"] < 0 for c in seeded)

    # Submit with a confirmed rubric.
    sid = "s-" + uuid.uuid4().hex[:12]
    sub = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better", "chosen_id": "A",
        "rejected_id": "B", "confidence": "high", "time_spent_sec": 150,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stabilize with IV calcium, then insulin/dextrose, then dialyze."},
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"]},
        "rubric": [{"text": "A correct answer stabilizes the myocardium with IV calcium first.", "points": 8, "axis": "safety"},
                   {"text": "A correct answer never uses a 1K dialysate for modest hyperkalemia.", "points": -6, "axis": "safety"}],
    }, headers=ev_h)
    assert sub.status_code == 200, sub.text
    assert sub.json()["status"] == "export_ready"

    exp = client.post("/api/asclepius/exports", json={"profile": "default"}, headers=admin_h)
    assert exp.status_code == 200, exp.text
    manifest = exp.json()
    assert manifest.get("rubric_count", 0) >= 1
    assert "grader_prompt.txt" in manifest["files"] and "score.py" in manifest["files"]

    dl = client.get(f"/api/asclepius/exports/{manifest['export_id']}/download", headers=admin_h)
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    names = zf.namelist()
    assert "grader_prompt.txt" in names and "score.py" in names
    # The rubric record is in the JSONL.
    lines = [json.loads(l) for l in zf.read("records.jsonl").decode().splitlines() if l.strip()]
    assert any(r.get("type") == "rubric" and r.get("max_points") == 8.0 for r in lines)
