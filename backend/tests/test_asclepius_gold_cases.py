"""Gold multimodal nephrology cases — seed set, loader, few-shot, and V3 serving.

These 10 hand-authored cases must (1) be valid multimodal cases that clear the real
content gate and construct under the strict ClinicalCase schema, (2) load into the
queue idempotently as ready-to-serve V3 tasks WITH an A/B candidate pair (no LLM
needed), (3) be served on V3, and (4) be the fallback autofill uses when live
generation is unavailable — so V3 shows real structured cases even with no API key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from asclepius import gold_cases as gc  # noqa: E402
from asclepius.cases import ClinicalCase, assert_multimodal_content, public_case  # noqa: E402

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _ev_h():
    return A.headers_for(A.make_user(_store(), role="evaluator", specialty="nephrology"))


# ─── The data itself ──────────────────────────────────────────────────────────
def test_there_are_ten_gold_cases():
    assert len(gc.GOLD_NEPHROLOGY_CASES) == 10
    ids = [c["case_id"] for c in gc.GOLD_NEPHROLOGY_CASES]
    assert len(set(ids)) == 10  # unique


def test_every_gold_case_is_valid_multimodal_and_strict_schema():
    multi_panel = 0   # "two data sources" — a temporal trend and/or same-day panels
    for e in gc.GOLD_NEPHROLOGY_CASES:
        case = e["case"]
        assert_multimodal_content(case)          # clears the real content gate
        ClinicalCase(**case)                     # constructs under extra='forbid'
        if len(case["lab_panels"]) >= 2:
            multi_panel += 1
        # an authored A/B preference pair with a marked flawed answer
        ids = sorted(c["id"] for c in e["candidate_answers"])
        assert ids == ["A", "B"]
        assert e["intended_flawed_id"] in ("A", "B")
        assert all((c.get("text") or "").strip() for c in e["candidate_answers"])
        # internal answer key present but strippable
        assert case.get("ground_truth") and case.get("hard_hook")
        assert "ground_truth" not in public_case(case)
    # Most cases integrate ≥2 lab panels (a trend, or labs + urine studies). A couple
    # are single-panel by design (the insufficient-data case; the single-ABG case).
    assert multi_panel >= 8, multi_panel


def test_fewshot_block_is_valid_json_exemplars():
    import json
    block = gc.fewshot_prompt_block(k=2, start=0)
    assert "WORKED EXAMPLES" in block
    # every JSON object embedded parses and is a public {question, case}
    objs = [ln for ln in block.splitlines() if ln.strip().startswith("{")]
    assert len(objs) >= 2
    for o in objs:
        parsed = json.loads(o)
        assert parsed["question"] and parsed["case"]["lab_panels"]
        assert "ground_truth" not in parsed["case"]  # answer key never in the exemplar


# ─── Loader ───────────────────────────────────────────────────────────────────
def test_loader_inserts_ten_then_is_idempotent():
    A.fresh_store()
    res = gc.load_gold_cases(_store())
    assert res["loaded"] == 10 and res["skipped"] == 0
    tasks = _store().list_tasks(specialty="nephrology", limit=50)
    mm = [t for t in tasks if t["modality"] == "multimodal"]
    assert len(mm) == 10
    t = mm[0]
    assert t["case"]["lab_panels"] and t["case"]["notes"]
    assert len(t["candidate_answers"]) == 2
    assert t["difficulty"] == "hard"
    # second call adds nothing
    res2 = gc.load_gold_cases(_store())
    assert res2["loaded"] == 0 and res2["skipped"] == 10


# ─── Serving on V3 ────────────────────────────────────────────────────────────
def test_v3_serves_a_gold_case_with_labs_and_ehr(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_V3_MULTIMODAL_ONLY", "1")
    monkeypatch.setenv("ASCLEPIUS_AUTOFILL", "0")
    A.fresh_store()
    gc.load_gold_cases(_store())
    ev_h = _ev_h()
    t = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=ev_h).json()["task"]
    assert t is not None
    assert t["modality"] == "multimodal"
    assert t["case"]["lab_panels"] and t["case"]["notes"]
    assert len(t["candidate_answers"]) == 2   # A/B pair present for the compare step
    # the internal answer key is NOT leaked to the served task
    assert "ground_truth" not in t["case"]


def test_v3_autofill_falls_back_to_gold_when_no_llm(monkeypatch):
    """The end-to-end unblock: with the multimodal preference ON and live generation
    unavailable (no LLM in the suite → GenerationDisabled), a V3 request auto-seeds the
    gold cases and serves one — a real structured case, not a text prompt."""
    monkeypatch.setenv("ASCLEPIUS_V3_MULTIMODAL_ONLY", "1")
    monkeypatch.setenv("ASCLEPIUS_AUTOFILL", "1")
    A.fresh_store()
    import routers.asclepius as R
    R._autofill_last_attempt.clear()  # ensure the cooldown doesn't suppress the seed
    ev_h = _ev_h()
    t = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=ev_h).json()["task"]
    assert t is not None, "V3 served nothing"
    assert t["modality"] == "multimodal", f"V3 served a non-multimodal task: {t.get('modality')}"
    assert t["case"]["lab_panels"] and t["case"]["notes"]


# ─── Debug endpoint ───────────────────────────────────────────────────────────
def test_debug_load_gold_cases_endpoint(monkeypatch):
    A.fresh_store()
    ev_h = _ev_h()
    r = client.get("/api/asclepius/debug/load-gold-cases", headers=ev_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["loaded"] == 10
    assert body["multimodal_in_queue"] >= 10
