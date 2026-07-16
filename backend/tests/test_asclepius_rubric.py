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


def test_rubric_repricing():
    """FIX-5.1: a rubric is a reusable scoring function, priced by quality — a bare one
    at the $60 base, a fully-loaded (grounded + validated + premium) one ~$164, NEVER
    the old flat $25. The multipliers stack and the breakdown explains why."""
    import asclepius.constants as C
    from asclepius.value import _rubric_marginal

    # A bare confirmed rubric already prices above the old flat $25 (the $60 base).
    bare = _rubric_marginal({"type": "rubric"})
    assert bare == pytest.approx(C.value_rubric_marginal())
    assert bare == pytest.approx(60.0) and bare > 25.0

    # Grounded × validated × premium stack multiplicatively: 60 × 1.4 × 1.5 × 1.3.
    loaded = _rubric_marginal({
        "type": "rubric", "grounded": True, "premium": True,
        "grader_validity": {"rejected_critical_failed": True},
    })
    assert loaded == pytest.approx(60.0 * 1.4 * 1.5 * 1.3)  # 163.8
    assert loaded >= 150.0

    # A grader that was probed but NOT proven to separate is not "validated" — no bump.
    unproven = _rubric_marginal({
        "type": "rubric", "grounded": True, "premium": True,
        "grader_validity": {"skipped": True},
    })
    assert unproven == pytest.approx(60.0 * 1.4 * 1.3) and unproven < loaded

    # The cap holds even if every multiplier were maxed.
    assert loaded <= C.value_rubric_marginal_cap()

    # ...and it flows through estimate_value into the transparent breakdown.
    task, submission = _submission_with_rubric()
    recs = package_submission(task, submission)
    for r in recs:
        if r["type"] == "rubric":
            r["grounded"] = True
            r["premium"] = True
            r["grader_validity"] = {"rejected_critical_failed": True}
    est = estimate_value(recs, task, submission)
    bd = est["breakdown"]
    assert bd["rubric_value"] == pytest.approx(163.8, abs=0.01)
    assert bd["rubric_grounded"] and bd["rubric_validated"] and bd["rubric_premium"]


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


def test_eval_pack_sku():
    """FIX-5.2: the rubric records + grader files + validity report ship as a STANDALONE
    eval pack — a re-licensable-per-model-version recurring SKU, reported SEPARATELY from
    the one-time data sale in both the manifest and the datasheet."""
    admin_h, ev_h = _admin_h(), _ev_h()
    body = {"specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
            "prompt": f"Hyperkalemia {A.uniq(6)}?",
            "candidate_answers": [{"id": "A", "text": "IV calcium then dialyze"},
                                  {"id": "B", "text": "Set dialysate K+ 1.0"}]}
    tid = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h).json()["created"][0]
    client.get("/api/asclepius/tasks/next", headers=ev_h)
    sid = "s-" + uuid.uuid4().hex[:12]
    sub = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better", "chosen_id": "A",
        "rejected_id": "B", "confidence": "high", "time_spent_sec": 150,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stabilize with IV calcium, then insulin/dextrose, then dialyze."},
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"]},
        "rubric": [{"text": "A correct answer stabilizes the myocardium with IV calcium first.", "points": 8, "axis": "safety"},
                   {"text": "A correct answer never uses a 1K dialysate for modest hyperkalemia.", "points": -9, "axis": "safety"}],
    }, headers=ev_h)
    assert sub.status_code == 200, sub.text

    manifest = client.post("/api/asclepius/exports", json={"profile": "default"}, headers=admin_h).json()

    # The eval pack is a first-class, SEPARATE line in the manifest.
    pack = manifest.get("eval_pack")
    assert pack is not None, "eval pack must be reported separately in the manifest"
    assert pack["sku"] == "asclepius_eval_pack"
    assert pack["billing"] == "recurring"
    assert pack["licensing"] == "re-licensable-per-model-version"
    assert pack["revalidation_trigger"] == "buyer_model_version_change"
    assert pack["n_rubrics"] >= 1
    # Priced as a reusable grader (≥ the $60 base per rubric), NOT the old flat $25.
    assert pack["recurring_value_usd"] >= 60.0 * pack["n_rubrics"] - 0.01
    assert pack["recurring_value_usd"] > 25.0
    for f in ("EVAL_PACK.md", "validity_report.json", "grader_prompt.txt", "score.py"):
        assert f in pack["files"] and f in manifest["files"]

    # The pack files + a per-rubric validity report actually ship in the bundle.
    dl = client.get(f"/api/asclepius/exports/{manifest['export_id']}/download", headers=admin_h)
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    names = zf.namelist()
    assert "EVAL_PACK.md" in names and "validity_report.json" in names
    report = json.loads(zf.read("validity_report.json").decode())
    assert "summary" in report and len(report["per_rubric"]) == pack["n_rubrics"]
    assert all("validated" in pr and "needs_review" in pr for pr in report["per_rubric"])

    pack_md = zf.read("EVAL_PACK.md").decode()
    assert "re-licensable-per-model-version" in pack_md and "recurring" in pack_md

    # The datasheet reports it as a separate recurring SKU (not folded into the data).
    datasheet = zf.read("datasheet.md").decode()
    assert "Eval pack (separate recurring SKU)" in datasheet
    assert "re-licensable-per-model-version" in datasheet


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


# ═══════════════════════════════════════════════════════════════════════════════
# Rubric Rigor Companion (§C): FIX-1/2/3/4/6/8
# ═══════════════════════════════════════════════════════════════════════════════
import asyncio as _asyncio


def test_rubric_concretization_gate():
    """FIX-1: seeds name a specific entity; no 160-char fragments; vague criteria flagged."""
    from asclepius.rubric import is_specific_text, propose_rubric
    assert is_specific_text("A correct answer states K+ must be <=5.0 before finerenone.")
    assert not is_specific_text("A correct answer is safer than a plausible alternative.")
    assert not is_specific_text("manages electrolytes appropriately")
    # De-truncation: a long good-step keeps full text (no 160-char fragment).
    long_step = ("Stabilize the myocardium with IV calcium gluconate because the ECG shows peaked "
                 "T waves and this protects against arrhythmia while other measures lower potassium " * 2)
    crit = propose_rubric({}, {"reasoning_steps": [{"text": long_step, "confirmed": True}]})
    txt = next(c["text"] for c in crit if c["source"] == "good_step")
    assert len(txt) > 170 and "A correct answer includes:" in txt   # not clipped to 160
    # key_data → concrete, specific positive.
    crit2 = propose_rubric({"case": {"ground_truth": {"key_data": ["urine osmolality 120 (LOW)"]}}}, {})
    kd = next(c for c in crit2 if c["source"] == "key_data")
    assert kd["specific"] is True


def test_rubric_evidence_anchor():
    """FIX-3: a critical criterion can carry an anchor; all-critical-grounded → grounded."""
    from asclepius.rubric import normalize_rubric, grounding_summary
    crit = normalize_rubric([
        {"text": "never use a 1K dialysate for modest hyperkalemia", "points": -9, "axis": "safety",
         "evidence_anchor": {"citation_text": "KDIGO 2024 hyperkalemia", "identifier": "KDIGO2024"}},
        {"text": "give IV calcium 1g", "points": 5, "axis": "safety"}])
    neg = next(c for c in crit if c["points"] < 0)
    assert neg.get("evidence_anchor")                       # carried through normalization
    g = grounding_summary(crit)
    assert g["grounded"] is True and g["n_grounded_criteria"] == 1
    # Drop the anchor → not grounded.
    crit2 = normalize_rubric([{"text": "never 1K dialysate", "points": -9, "axis": "safety"}])
    assert grounding_summary(crit2)["grounded"] is False


def test_rubric_completeness_gate():
    """FIX-4: a thin rubric → standard; a rich one meeting the bar → premium."""
    from asclepius.rubric import rubric_completeness
    thin = [{"text": "give calcium 1g", "points": 9, "axis": "safety", "tier": "critical", "specific": True},
            {"text": "never 1K dialysate", "points": -9, "axis": "safety", "tier": "critical", "specific": True}]
    r = rubric_completeness(thin)
    assert r["premium"] is False and any("criteria" in m for m in r["missing"])
    rich = [
        {"text": "give IV calcium 1g", "points": 9, "axis": "safety", "tier": "critical", "specific": True},
        {"text": "never use a 1K dialysate", "points": -9, "axis": "safety", "tier": "critical", "specific": True},
        {"text": "insulin 10 units with dextrose", "points": 6, "axis": "accuracy", "tier": "important", "specific": True},
        {"text": "recheck K+ in 2 hours", "points": 4, "axis": "completeness", "tier": "important", "specific": True},
        {"text": "explain the ECG arrhythmia risk", "points": 3, "axis": "communication", "tier": "helpful", "specific": True}]
    r2 = rubric_completeness(rich)
    assert r2["premium"] is True and r2["missing"] == [] and r2["n_axes"] >= 3


def test_rubric_core_axis_nudge():
    """FIX-7: a rubric missing a core axis (safety/accuracy/reasoning) gets an ADVISORY
    nudge — never a gate. The nudge stays out of `missing`, so a premium rubric with no
    reasoning criterion is still premium but is still told to consider adding one."""
    from asclepius.rubric import rubric_completeness
    # The 'rich' rubric covers safety+accuracy but NOT reasoning → nudge, still premium.
    rich_no_reasoning = [
        {"text": "give IV calcium 1g", "points": 9, "axis": "safety"},
        {"text": "never use a 1K dialysate", "points": -9, "axis": "safety"},
        {"text": "insulin 10 units with dextrose", "points": 6, "axis": "accuracy"},
        {"text": "recheck K+ in 2 hours", "points": 4, "axis": "completeness"},
        {"text": "explain the ECG arrhythmia risk", "points": 3, "axis": "communication"}]
    r = rubric_completeness(rich_no_reasoning)
    assert r["premium"] is True                       # nudge NEVER blocks premium
    assert r["core_axes_missing"] == ["reasoning"]
    assert r["covers_core_axes"] is False
    assert r["nudges"] and "reasoning" in r["nudges"][0]
    assert not any("reasoning" in m for m in r["missing"])   # advisory, not in the gate
    # Add a reasoning criterion → no nudge, all core axes covered.
    covered = rich_no_reasoning + [{"text": "weigh the ECG against the K+ before dosing", "points": 5, "axis": "reasoning"}]
    r2 = rubric_completeness(covered)
    assert r2["covers_core_axes"] is True and r2["nudges"] == [] and r2["core_axes_missing"] == []


def test_rubric_failure_coverage():
    """FIX-8 (deterministic): negative criteria must cover the rejected error tags."""
    from asclepius.rubric import failure_coverage
    covered = failure_coverage(
        [{"text": "never make an unsafe recommendation", "points": -9, "tier": "critical",
          "source": "error_tag:unsafe_recommendation"}],
        {"generation": {"ai_failure_mode": "overtreatment"}},
        {"payload": {"rejected_critique": {"error_tags": ["unsafe_recommendation"]}}})
    assert covered["covered"] is True and covered["uncovered_failure_modes"] == []
    uncovered = failure_coverage(
        [{"text": "give calcium", "points": 5}], {},
        {"payload": {"rejected_critique": {"error_tags": ["dosing_error"]}}})
    assert "dosing_error" in uncovered["uncovered_failure_modes"]


def test_constants_no_dupes():
    """FIX-6 / §E-1: no shadowed (duplicated) constant/function definitions."""
    import ast
    import asclepius.constants as C
    tree = ast.parse(open(C.__file__).read())
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"shadowed top-level defs in constants.py: {dupes}"


def test_grader_validity_separation(monkeypatch):
    """FIX-2: chosen scores high, rejected low/critical-fails; low separation → needs_review."""
    import asclepius.grader_eval as ge

    async def sep(criteria, prompt, answer, **k):
        if "GOOD" in answer:
            return {"per_criterion": [], "normalized": 0.92, "critical_failure": False}
        return {"per_criterion": [], "normalized": 0.15, "critical_failure": True}
    monkeypatch.setattr(ge, "run_grader", sep)
    v = _asyncio.run(ge.grader_validity([{"text": "c", "points": 9}], "p", "GOOD chosen", "BAD rejected"))
    assert v["chosen_normalized"] > v["rejected_normalized"]
    assert v["rejected_critical_failed"] is True and v["needs_review"] is False

    async def flat(criteria, prompt, answer, **k):
        return {"per_criterion": [], "normalized": 0.5, "critical_failure": False}
    monkeypatch.setattr(ge, "run_grader", flat)
    v2 = _asyncio.run(ge.grader_validity([{"text": "c", "points": 9}], "p", "GOOD", "BAD"))
    assert v2["needs_review"] is True                       # no separation → flagged


def test_grader_reliability_variance(monkeypatch):
    """FIX-2: stable grader → consistent; a critical criterion that flips → unreliable."""
    import asclepius.grader_eval as ge

    async def stable(criteria, prompt, answer, **k):
        return {"per_criterion": [{"text": "c", "met": True}], "normalized": 0.9, "critical_failure": False}
    monkeypatch.setattr(ge, "run_grader", stable)
    crit = [{"text": "c", "points": 9, "tier": "critical", "critical": True}]
    r = _asyncio.run(ge.grader_reliability(crit, "p", "ans", runs=3))
    assert r["consistent"] is True and r["flip_rate"] == 0.0 and r["unreliable"] is False

    calls = {"i": 0}

    async def flipper(criteria, prompt, answer, **k):
        calls["i"] += 1
        return {"per_criterion": [{"text": "c", "met": calls["i"] % 2 == 0}],
                "normalized": 0.5, "critical_failure": False}
    monkeypatch.setattr(ge, "run_grader", flipper)
    r2 = _asyncio.run(ge.grader_reliability(crit, "p", "ans", runs=3))
    assert r2["unreliable"] is True and r2["critical_flip"] is True


def test_rubric_gameable_probe(monkeypatch):
    """FIX-8: padded-hollow must NOT beat terse-correct; gameable when it does."""
    import asclepius.grader_eval as ge

    async def not_gameable(criteria, prompt, answer, **k):
        return ({"per_criterion": [], "normalized": 0.2, "critical_failure": True} if len(answer) > 300
                else {"per_criterion": [], "normalized": 0.85, "critical_failure": False})
    monkeypatch.setattr(ge, "run_grader", not_gameable)
    h = _asyncio.run(ge.hackability([{"text": "c", "points": 9}], "p",
                                    "Give IV calcium now.", "Give a 1K dialysate immediately."))
    assert h["gameable"] is False and h["padded_normalized"] < h["terse_correct_normalized"]

    async def gameable(criteria, prompt, answer, **k):
        return ({"per_criterion": [], "normalized": 0.9, "critical_failure": False} if len(answer) > 300
                else {"per_criterion": [], "normalized": 0.3, "critical_failure": False})
    monkeypatch.setattr(ge, "run_grader", gameable)
    h2 = _asyncio.run(ge.hackability([{"text": "c", "points": 9}], "p", "Give calcium.", "Give 1K dialysate."))
    assert h2["gameable"] is True


def test_rubric_probes_skip_without_llm():
    """Contract: with no LLM configured, every probe degrades to skipped (never raises)."""
    import asclepius.grader_eval as ge
    rec = {"type": "rubric", "criteria": [{"text": "c", "points": 9}], "prompt": "p"}
    out = _asyncio.run(ge.run_rubric_probes(
        rec, {"candidate_answers": []},
        {"payload": {"chosen_revision": {"revised_text": "x"}, "rejected_id": "B"}}))
    assert out["grader_validity"] == {"skipped": True}
    assert out["hackability"] == {"skipped": True}
