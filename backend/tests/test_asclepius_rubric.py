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
    # Tiered rubric (Two-Model PRD WS-B): +8 is critical, −6 is important.
    assert r["tiers"] == {"critical": 1, "important": 1}
    assert r["n_critical"] == 1
    # The one negative is IMPORTANT (−6), not critical → no critical negative here.
    assert r["has_critical_negative"] is False
    assert all(c.get("tier") in ("critical", "important", "helpful") for c in r["criteria"])


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
        # A v3 rubric must name at least one CRITICAL negative (Two-Model PRD WS-B):
        # the −9 criterion is critical (|points| 8-10); the +8 positive is critical too.
        "rubric": [{"text": "A correct answer stabilizes the myocardium with IV calcium first.", "points": 8, "axis": "safety"},
                   {"text": "A correct answer never uses a 1K dialysate for modest hyperkalemia.", "points": -9, "axis": "safety"}],
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


# ─── Tiered rubric (Two-Model PRD Workstream B) ───────────────────────────────
def test_tier_for_points_bands():
    from asclepius.constants import tier_for_points
    assert tier_for_points(-10) == "critical" and tier_for_points(8) == "critical"
    assert tier_for_points(-7) == "important" and tier_for_points(4) == "important"
    assert tier_for_points(-3) == "helpful" and tier_for_points(1) == "helpful"
    assert tier_for_points(-100) == "critical"      # clamps up
    assert tier_for_points(0.5) == "helpful"        # clamps down


def test_propose_rubric_stamps_tier_and_high_severity_is_critical_negative():
    task = {"task_id": "t", "specialty": "nephrology"}
    payload = {
        "verdict": "A_better",
        "rejected_critique": {
            "error_tags": ["unsafe_recommendation", "omission"],
            "severities": {"unsafe_recommendation": "high", "omission": "low"},
        },
    }
    crit = R.propose_rubric(task, payload)
    for c in crit:
        assert c["tier"] in ("critical", "important", "helpful")
        assert c["critical"] == (c["tier"] == "critical")
    # A high-severity error → −8 → critical negative; low-severity → −3 → helpful.
    assert R.has_critical_negative(crit) is True
    hi = next(c for c in crit if c["source"] == "error_tag:unsafe_recommendation")
    assert hi["tier"] == "critical" and hi["points"] == -8.0
    lo = next(c for c in crit if c["source"] == "error_tag:omission")
    assert lo["tier"] == "helpful"


def test_normalize_rubric_recomputes_mismatched_tier():
    # Client claims "helpful" on a −9 weight → recomputed to critical (tier follows
    # points, always consistent in the packaged record).
    out = R.normalize_rubric([
        {"text": "never do X", "points": -9, "tier": "helpful", "axis": "safety"},
        {"text": "include Y", "points": 5, "axis": "accuracy"},   # tier absent → derived
    ])
    x = next(c for c in out if c["text"] == "never do X")
    assert x["tier"] == "critical" and x["critical"] is True
    y = next(c for c in out if c["text"] == "include Y")
    assert y["tier"] == "important" and y["critical"] is False


def test_has_critical_negative_only_counts_critical_negatives():
    # A critical POSITIVE (+9) is not a critical negative; an important negative (−6) isn't either.
    assert R.has_critical_negative([{"text": "a", "points": 9}]) is False
    assert R.has_critical_negative([{"text": "a", "points": -6}]) is False
    assert R.has_critical_negative([{"text": "a", "points": -8}]) is True


def _v_task(admin_h, ev_h):
    body = {"specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
            "prompt": f"Hyperkalemia {A.uniq(6)}?",
            "candidate_answers": [{"id": "A", "text": "IV calcium then dialyze"},
                                  {"id": "B", "text": "Set dialysate K+ 1.0"}]}
    tid = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h).json()["created"][0]
    client.get("/api/asclepius/tasks/next", headers=ev_h)
    return tid


def _submit_body(tid, *, portal_version, rubric):
    return {
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "confidence": "high", "time_spent_sec": 150,
        "portal_version": portal_version,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stabilize with IV calcium, then insulin/dextrose, then dialyze."},
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"]},
        "rubric": rubric,
    }


_IMPORTANT_ONLY = [
    {"text": "A correct answer stabilizes with IV calcium first.", "points": 6, "axis": "safety"},
    {"text": "A correct answer never uses a 1K dialysate for modest hyperkalemia.", "points": -6, "axis": "safety"},
]
_WITH_CRIT_NEG = [
    {"text": "A correct answer stabilizes with IV calcium first.", "points": 6, "axis": "safety"},
    {"text": "A correct answer never uses a 1K dialysate for modest hyperkalemia.", "points": -9, "axis": "safety"},
]


def test_v3_submit_blocked_without_critical_negative():
    admin_h, ev_h = _admin_h(), _ev_h()
    tid = _v_task(admin_h, ev_h)
    r = client.post("/api/asclepius/submissions", json=_submit_body(tid, portal_version="v3", rubric=_IMPORTANT_ONLY), headers=ev_h)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "critical_negative_required"


def test_v3_submit_allowed_with_critical_negative():
    admin_h, ev_h = _admin_h(), _ev_h()
    tid = _v_task(admin_h, ev_h)
    r = client.post("/api/asclepius/submissions", json=_submit_body(tid, portal_version="v3", rubric=_WITH_CRIT_NEG), headers=ev_h)
    assert r.status_code == 200, r.text


def test_v3_submit_allowed_with_empty_rubric():
    # The rubric stays OPTIONAL: an empty rubric on v3 is never blocked.
    admin_h, ev_h = _admin_h(), _ev_h()
    tid = _v_task(admin_h, ev_h)
    r = client.post("/api/asclepius/submissions", json=_submit_body(tid, portal_version="v3", rubric=[]), headers=ev_h)
    assert r.status_code == 200, r.text


def test_v1_v2_submit_unaffected_by_critical_negative_gate():
    # GUARDRAIL: the gate is scoped to portal_version ∈ {v3,v4}. V1/V2 submit a rubric
    # WITHOUT a critical negative and are NOT blocked (byte-for-byte unchanged).
    for pv in ("v1", "v2"):
        admin_h, ev_h = _admin_h(), _ev_h()
        tid = _v_task(admin_h, ev_h)
        r = client.post("/api/asclepius/submissions", json=_submit_body(tid, portal_version=pv, rubric=_IMPORTANT_ONLY), headers=ev_h)
        assert r.status_code == 200, (pv, r.text)


def test_omitted_portal_version_on_synthetic_task_not_gated():
    """Review fix (B#1): portal_version DEFAULTS to v3 when omitted, so a legacy /
    direct API client that omits the field and posts a rubric WITHOUT a critical
    negative must NOT newly 400 (that would be a wire-contract regression). The gate
    fires only when v3/v4 is UNAMBIGUOUS — an explicit claim or a real (v4) task."""
    admin_h, ev_h = _admin_h(), _ev_h()
    tid = _v_task(admin_h, ev_h)
    body = _submit_body(tid, portal_version=None, rubric=_IMPORTANT_ONLY)  # no critical negative
    body.pop("portal_version")                                            # OMIT entirely
    r = client.post("/api/asclepius/submissions", json=body, headers=ev_h)
    assert r.status_code == 200, r.text          # not gated — regression fixed


def test_empty_text_rubric_rows_do_not_trip_gate():
    """Review fix (B#4): the gate tests the NORMALIZED rubric, so empty-text /
    zero-point rows (which package to nothing) don't trip the critical-negative 400."""
    admin_h, ev_h = _admin_h(), _ev_h()
    tid = _v_task(admin_h, ev_h)
    junk = [{"text": "", "points": 0}, {"text": "   ", "points": -9}]
    r = client.post("/api/asclepius/submissions", json=_submit_body(tid, portal_version="v3", rubric=junk), headers=ev_h)
    assert r.status_code == 200, r.text          # normalized rubric is empty → not gated


def test_rubric_criterion_validator_always_recomputes_tier():
    """Review fix (B#2): the schema validator ALWAYS derives tier from |points| — a
    valid-but-mismatched wire tier can never drift the stored critical flag."""
    from asclepius.schemas import RubricCriterion
    c = RubricCriterion(text="never do X", points=-9, tier="helpful")   # lies: -9 is critical
    assert c.tier == "critical" and c.critical is True
    c2 = RubricCriterion(text="include Y", points=5, tier="critical")   # lies: 5 is important
    assert c2.tier == "important" and c2.critical is False


def test_grader_prompt_and_score_py_carry_critical_hard_fail():
    from asclepius.export import _GRADER_PROMPT, _SCORE_PY
    assert "CRITICAL-NEGATIVE HARD FAIL" in _GRADER_PROMPT
    assert "critical_failure" in _GRADER_PROMPT
    assert "apply_critical_hard_fail" in _SCORE_PY and "critical_failure" in _SCORE_PY


def test_score_py_apply_critical_hard_fail_floors_normalized():
    # Execute the generated score.py's hard-fail backstop in isolation.
    import types
    from asclepius.export import _SCORE_PY
    mod = types.ModuleType("score_scaffold")
    # Only the helper needs to run: neutralize the top-level file read (needs
    # __file__ + grader_prompt.txt on disk) and the __main__ guard.
    src = (_SCORE_PY
           .replace('HERE = pathlib.Path(__file__).parent', 'HERE = pathlib.Path(".")')
           .replace('PROMPT = (HERE / "grader_prompt.txt").read_text(encoding="utf-8")', 'PROMPT = ""')
           .replace('if __name__ == "__main__":\n    main()', ""))
    exec(compile(src, "score.py", "exec"), mod.__dict__)  # noqa: S102 - trusted scaffold under test
    rubric = {"criteria": [
        {"text": "never uses a 1K dialysate", "points": -9, "tier": "critical"},
        {"text": "includes IV calcium", "points": 8, "tier": "critical"},
    ]}
    judged = {"per_criterion": [
        {"text": "never uses a 1K dialysate", "met": True},
        {"text": "includes IV calcium", "met": True},
    ], "score": -1, "max_points": 8, "normalized": 0.9}
    out = mod.apply_critical_hard_fail(judged, rubric)
    assert out["critical_failure"] is True
    assert out["normalized"] == 0.0
    assert "never uses a 1K dialysate" in out["failed_critical_criteria"]
