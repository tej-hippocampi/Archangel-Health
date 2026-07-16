"""Model-Failure Taxonomy Export (Tier-1 PRD §D): controlled vocab, capture gate,
provider attribution via the A/B slot map, small-N suppression, κ label agreement,
human-verified-only, and the disjoint scored-eval holdout."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from asclepius import failure_taxonomy as FT  # noqa: E402


def _store():
    from asclepius.store import get_store
    return get_store()


# ── D-1/D-2: controlled vocabulary ────────────────────────────────────────────
def test_failure_tag_controlled_vocab():
    from asclepius.schemas import FailureTag
    assert FailureTag(mode="anchoring").mode == "anchoring"
    assert FailureTag(mode="unsafe_recommendation").mode == "unsafe_recommendation"
    assert FailureTag(mode="totally_made_up").mode == "other"        # coerced to other


# ── D-3: attribution via the slot map ─────────────────────────────────────────
def _pair_task(ab_source):
    return {"generation": {"ab_source": ab_source, "case_id": "c1", "seed_archetype_id": "hyponatremia"},
            "difficulty": "hard", "specialty": "nephrology", "task_id": "t1",
            "candidate_answers": [
                {"id": "A", "text": "x", "source": "baseline", "provider": "openai", "baseline_model": "gpt-5"},
                {"id": "B", "text": "y", "source": "baseline", "provider": "anthropic", "baseline_model": "claude-opus-4-8"}]}


def test_failure_attribution_two_frontier():
    a = FT._attribution(_pair_task("two_frontier"), "A")
    assert a["provider"] == "openai" and a["model_id"] == "gpt-5"
    b = FT._attribution(_pair_task("two_frontier"), "B")
    assert b["provider"] == "anthropic"


def test_failure_attribution_legacy_is_unattributed():
    # A same-model legacy_fallback pair CANNOT attribute a cross-provider failure.
    a = FT._attribution(_pair_task("legacy_fallback"), "A")
    assert a["provider"] == "unattributed"
    v4 = FT._attribution(_pair_task("anthropic_only_v4"), "A")
    assert v4["provider"] == "unattributed"
    # A generated (non-baseline) candidate is not attributed at all.
    gen_task = {"generation": {"ab_source": None},
                "candidate_answers": [{"id": "A", "text": "x", "source": "internal_prompt_bank"}]}
    assert FT._attribution(gen_task, "A") is None


# ── D-4/D-5: aggregation, small-N suppression, κ ──────────────────────────────
def _obs(mode, provider, difficulty="hard", case_id="c1", axis="reasoning", rater="r1"):
    return {"case_id": case_id, "case_class": "x", "task_id": "t", "submission_id": "s",
            "annotator_id": rater, "specialty": "nephrology", "difficulty": difficulty,
            "axis": axis, "provider": provider, "model_id": "m", "failure_mode": mode,
            "evidence_step": None, "physician_note": "note", "ab_source": "two_frontier"}


def test_failure_taxonomy_smalln_suppression():
    # One observation → below the default floor of 5 → low_confidence, rate suppressed.
    agg = FT.aggregate([_obs("anchoring", "openai")], min_n=5)
    cell = agg["cells"][0]
    assert cell["n"] == 1 and cell["low_confidence"] is True and cell["rate"] is None
    # Enough observations → a real rate is reported.
    obs = [_obs("anchoring", "openai", case_id=f"c{i}") for i in range(6)]
    agg2 = FT.aggregate(obs, min_n=5)
    cell2 = next(c for c in agg2["cells"] if c["failure_mode"] == "anchoring")
    assert cell2["low_confidence"] is False and cell2["rate"] == 1.0     # 6/6 attributed


def test_failure_label_agreement_kappa():
    # Two raters agree on the SAME case → agreement 1.0; a third case with one rater is ignored.
    obs = [_obs("anchoring", "openai", case_id="c1", rater="r1"),
           _obs("anchoring", "openai", case_id="c1", rater="r2"),
           _obs("overtreatment", "openai", case_id="c2", rater="r1")]
    k = FT.label_agreement(obs)
    assert k["overlap_cases"] == 1 and k["label_agreement"] == 1.0 and k["n_raters"] == 2
    # Disagreement → agreement < 1.
    obs2 = [_obs("anchoring", "openai", case_id="c1", rater="r1"),
            _obs("overtreatment", "openai", case_id="c1", rater="r2")]
    assert FT.label_agreement(obs2)["label_agreement"] == 0.0


def test_failure_eval_holdout_is_disjoint_and_deterministic():
    obs = [_obs("anchoring", "openai", case_id=f"case-{i}") for i in range(40)]
    h1 = FT._holdout_split(obs)
    h2 = FT._holdout_split(obs)
    assert h1 == h2 and 0 < len(h1) < 40          # deterministic + a real, partial holdout
    # The scored-eval scaffold is valid python and lists the modes offline.
    import types
    mod = types.ModuleType("sfm")
    src = (FT.SCORE_FAILUREMODE_PY
           .replace('HERE = pathlib.Path(__file__).parent', 'HERE = pathlib.Path(".")')
           .replace('HOLDOUT = json.loads((HERE / "holdout.json").read_text(encoding="utf-8"))',
                    'HOLDOUT = {"case_ids": [], "observations": []}')
           .replace('if __name__ == "__main__":\n    main()', ""))
    exec(compile(src, "score_failuremode.py", "exec"), mod.__dict__)
    assert callable(mod.main)


# ── D-5: human-verified only + end-to-end collect from the store ──────────────
def _insert_graded_pair(store, *, ab_source, failure_tags, rater_email):
    ev = A.make_user(store, role="evaluator", specialty="nephrology",
                     board_cert="board_certified_nephrology", years_experience=10, email=rater_email)
    task = store.insert_task(
        prompt="Hyperkalemia case", specialty="nephrology", difficulty="hard",
        candidate_answers=[
            {"id": "A", "text": "give calcium", "source": "baseline", "provider": "openai", "baseline_model": "gpt-5"},
            {"id": "B", "text": "1K dialysate", "source": "baseline", "provider": "anthropic", "baseline_model": "claude-opus-4-8"}],
        generation={"ab_source": ab_source, "case_id": "case-" + uuid.uuid4().hex[:6],
                    "mode": "grade_real_models"})
    sid = "s-" + uuid.uuid4().hex[:10]
    store.insert_submission(
        submission_id=sid, task_id=task["task_id"], evaluator_id=ev["id"], verdict="A_better",
        chosen_id="A", rejected_id="B", confidence="high", time_spent_sec=120,
        payload={"verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "portal_version": "v3",
                 "rejected_critique": {"error_tags": ["unsafe_recommendation"], "failure_tags": failure_tags}},
        annotator=store.annotator_block(ev), dedupe_hash=None, grounded=False,
        grounding_mode="optional", portal_version="v3", status="submitted")
    return task, sid


def test_collect_only_physician_tags_and_two_frontier_attribution():
    store = A.fresh_store()
    # A two_frontier pair WITH a physician failure tag → one attributed observation.
    _insert_graded_pair(store, ab_source="two_frontier",
                        failure_tags=[{"mode": "unsafe_recommendation", "note": "1K bath is arrhythmogenic"}],
                        rater_email=f"r1-{uuid.uuid4().hex[:5]}@x.example")
    # A legacy_fallback pair with a tag → contributes but UNATTRIBUTED.
    _insert_graded_pair(store, ab_source="legacy_fallback",
                        failure_tags=[{"mode": "overtreatment", "note": "unnecessary"}],
                        rater_email=f"r2-{uuid.uuid4().hex[:5]}@x.example")
    # A pair with NO failure tags → contributes NOTHING (no model-judge hypotheses).
    _insert_graded_pair(store, ab_source="two_frontier", failure_tags=[],
                        rater_email=f"r3-{uuid.uuid4().hex[:5]}@x.example")

    obs = FT.collect_failure_observations(store)
    assert len(obs) == 2                                    # only the two tagged pairs
    modes = {o["failure_mode"]: o["provider"] for o in obs}
    # The tag describes the REJECTED answer (id=B → anthropic) → attributed to anthropic.
    assert modes["unsafe_recommendation"] == "anthropic"    # two_frontier → attributed
    assert modes["overtreatment"] == "unattributed"         # legacy_fallback → unattributed

    bundle = FT.build_failure_taxonomy(store)
    assert bundle["provenance"]["human_verified"] is True
    assert bundle["aggregate"]["n_attributed"] == 1 and bundle["aggregate"]["n_unattributed"] == 1
    assert set(bundle["mode_definitions"]) >= {"anchoring", "unsafe_recommendation", "other"}


# ── D-2: the failure_tag_required submit gate (V3/V4, critical-negative rubric) ─
def test_failure_tag_required_gate():
    from fastapi.testclient import TestClient
    client = TestClient(A.app)
    store = A.fresh_store()
    admin = A.make_user(store, role="admin", email=f"a-{uuid.uuid4().hex[:5]}@x.example")
    ev = A.make_user(store, role="evaluator", specialty="nephrology",
                     board_cert="board_certified_nephrology", years_experience=10)
    ev_h = A.headers_for(ev)
    # A real-model (baseline) A/B pair.
    task = store.insert_task(
        prompt="Hyperkalemia case", specialty="nephrology", difficulty="hard",
        candidate_answers=[
            {"id": "A", "text": "IV calcium then dialyze", "source": "baseline", "provider": "openai", "baseline_model": "gpt-5"},
            {"id": "B", "text": "1K dialysate now", "source": "baseline", "provider": "anthropic", "baseline_model": "claude-opus-4-8"}],
        generation={"ab_source": "two_frontier", "mode": "grade_real_models"})
    tid = task["task_id"]
    crit_rubric = [
        {"text": "A correct answer gives IV calcium 1g first.", "points": 8, "axis": "safety"},
        {"text": "A correct answer never uses a 1K dialysate for modest hyperkalemia.", "points": -9, "axis": "safety"}]

    def _body(failure_tags):
        rc = {"error_tags": ["unsafe_recommendation"]}
        if failure_tags is not None:
            rc["failure_tags"] = failure_tags
        return {"submission_id": "s-" + uuid.uuid4().hex[:10], "task_id": tid, "verdict": "A_better",
                "chosen_id": "A", "rejected_id": "B", "confidence": "high", "time_spent_sec": 150,
                "portal_version": "v3",
                "prompt_review": {"reviewed": True, "verdict": "valid"},
                "independent_answer": {"text": "IV calcium, then insulin/dextrose, then dialyze."},
                "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
                "rejected_critique": rc, "rubric": crit_rubric}

    # Critical-negative rubric on a baseline pair with NO failure tag → 400.
    r = client.post("/api/asclepius/submissions", json=_body(None), headers=ev_h)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "failure_tag_required", r.text
    # With a physician failure tag → 200.
    ok = client.post("/api/asclepius/submissions",
                     json=_body([{"mode": "unsafe_recommendation", "note": "1K bath is arrhythmogenic"}]),
                     headers=ev_h)
    assert ok.status_code == 200, ok.text


# ── Frontend payload shape round-trips end-to-end (§C + §D) ───────────────────
def test_frontend_shaped_payload_roundtrips():
    """The EXACT payload the V3 SPA builds — rubric criteria with tier/specific/
    evidence_anchor (FIX-1/3) and rejected_critique.failure_tags with all four keys
    (§D-2) — must be accepted and land on the packaged record. Guards against a
    frontend/backend shape drift."""
    from fastapi.testclient import TestClient
    client = TestClient(A.app)
    store = A.fresh_store()
    ev = A.make_user(store, role="evaluator", specialty="nephrology",
                     board_cert="board_certified_nephrology", years_experience=10)
    ev_h = A.headers_for(ev)
    task = store.insert_task(
        prompt="Hyperkalemia case", specialty="nephrology", difficulty="hard",
        candidate_answers=[
            {"id": "A", "text": "IV calcium then dialyze", "source": "baseline", "provider": "openai", "baseline_model": "gpt-5"},
            {"id": "B", "text": "1K dialysate now", "source": "baseline", "provider": "anthropic", "baseline_model": "claude-opus-4-8"}],
        generation={"ab_source": "two_frontier", "mode": "grade_real_models"})
    sid = "s-" + uuid.uuid4().hex[:10]
    body = {
        "submission_id": sid, "task_id": task["task_id"], "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "confidence": "high", "time_spent_sec": 150,
        "portal_version": "v3",
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "IV calcium 1g, then insulin/dextrose, then dialyze."},
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {
            "error_tags": ["unsafe_recommendation"],
            # §D-2: exactly what renderFailureTags → buildSubmissionPayload emits.
            "failure_tags": [{"mode": "unsafe_recommendation", "note": "1K bath is arrhythmogenic",
                              "criterion_id": None, "evidence_step_id": None}]},
        # §C: rubric criteria exactly as the SPA maps them.
        "rubric": [
            {"text": "A correct answer gives IV calcium 1g first.", "points": 8, "axis": "safety",
             "source": "manual", "tier": "critical", "specific": True,
             "evidence_anchor": {"citation_text": "KDIGO 2024 hyperkalemia", "source_type": "guideline", "identifier": "KDIGO2024"}},
            {"text": "A correct answer never uses a 1K dialysate for modest hyperkalemia.", "points": -9,
             "axis": "safety", "source": "manual", "tier": "critical", "specific": True,
             "evidence_anchor": {"citation_text": "KDIGO 2024", "source_type": "guideline", "identifier": "KDIGO2024"}}],
    }
    r = client.post("/api/asclepius/submissions", json=body, headers=ev_h)
    assert r.status_code == 200, r.text                    # accepted (gates satisfied)
    # The packaged rubric record carries the §C fields; the failure tag attributes.
    recs = store.records_for_submission(sid)
    rub = next((x["payload"] for x in recs if x["payload"].get("type") == "rubric"), None)
    assert rub is not None
    assert rub["grounded"] is True and rub["premium"] is False   # both critical anchored; <5 criteria
    assert rub["criteria"][0].get("evidence_anchor")            # anchor round-tripped
    assert all(c.get("specific") for c in rub["criteria"])
    obs = FT.collect_failure_observations(store)
    assert obs and obs[0]["provider"] == "anthropic" and obs[0]["failure_mode"] == "unsafe_recommendation"
