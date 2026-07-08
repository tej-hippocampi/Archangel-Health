"""Multimodal export + value multiplier + real_deid ingest seam (PR-C).

Covers the delivery-side of the Synthetic Multimodal Cases PRD:
  * the 1.35× multimodal value multiplier (value.py),
  * packaging carrying modality/case/case_source onto records (multimodal only),
  * export counts + modality/case_source filters + the include_answer_key
    benchmark opt-in (raw ground_truth stays withheld by default),
  * the real_deid format-adapter seam + de-identification guard.

The LLM is never called. Records are constructed directly where a full HTTP
round-trip isn't needed; one end-to-end export drives a real multimodal task
through packaging to an on-disk batch.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402
from asclepius import value as V  # noqa: E402
from asclepius import constants as C  # noqa: E402


# ─── Value multiplier (unit) ──────────────────────────────────────────────────
def test_multimodal_lifts_value_over_text():
    """Same content/tier, but a multimodal judgment carries the structured-case
    premium — strictly more realized value than the text-only equivalent."""
    recs = [{"type": "preference"}, {"type": "ideal_answer"}, {"type": "reasoning_trace", "step_pairs": [1]}]
    sub = {"grounded": False}
    text = V.estimate_value(recs, {"difficulty": "hard", "modality": "text"}, sub)
    mm = V.estimate_value(recs, {"difficulty": "hard", "modality": "multimodal"}, sub)
    assert mm["realized_value"] > text["realized_value"]
    assert round(mm["tier_mult"] / text["tier_mult"], 4) == round(C.value_multimodal_mult(), 4)


def test_multimodal_detected_from_case_or_record_context():
    assert V._is_multimodal({"case": {"lab_panels": []}}, []) is True
    assert V._is_multimodal({}, [{"context": {"modality": "multimodal"}}]) is True
    assert V._is_multimodal({}, [{"context": {"modality": "text"}}]) is False
    assert V._is_multimodal({}, []) is False


def test_expected_value_uses_multimodal():
    text = V.expected_value_for_task({"difficulty": "hard"})
    mm = V.expected_value_for_task({"difficulty": "hard", "modality": "multimodal"})
    assert mm["realized_value"] > text["realized_value"]


# ─── Packaging carries modality only for multimodal (unit) ────────────────────
def test_packaging_context_multimodal_keys():
    from asclepius.packaging import _context
    case = {
        "case_source": "synthetic", "lab_panels": [{"panel": "BMP", "results": []}],
        "ground_truth": {"answer": "secret"},
    }
    ctx = _context({"specialty": "nephrology", "difficulty": "hard", "modality": "multimodal", "case": case})
    assert ctx["modality"] == "multimodal"
    assert ctx["case_source"] == "synthetic"
    # answer key stripped from the buyer-facing case
    assert "ground_truth" not in ctx["case"]
    assert ctx["case"]["lab_panels"]


def test_packaging_context_text_unchanged():
    from asclepius.packaging import _context
    ctx = _context({"specialty": "nephrology", "difficulty": "medium"})
    assert "modality" not in ctx and "case" not in ctx and "case_source" not in ctx


# ─── Export counts + filters (unit) ───────────────────────────────────────────
def _rec(modality="text", case_source=None, rtype="preference"):
    ctx = {"difficulty": "hard"}
    if modality == "multimodal":
        ctx["modality"] = "multimodal"
        if case_source:
            ctx["case_source"] = case_source
    return {"type": rtype, "specialty": "nephrology", "payload": {"context": ctx, "portal_version": "v3"}}


def test_counts_break_out_modality_and_case_source():
    from asclepius.export import _counts
    recs = [_rec(), _rec("multimodal", "synthetic"), _rec("multimodal", "real_deid")]
    c = _counts(recs)
    assert c["by_modality"] == {"text": 1, "multimodal": 2}
    assert c["by_case_source"] == {"synthetic": 1, "real_deid": 1}


def test_passes_filters_modality_and_case_source():
    from asclepius.export import _passes_filters

    def f(rec, **kw):
        base = dict(difficulty=None, grounded_only=False, confidence_floor=None,
                    min_agreement=None, buyer_request_id=None, annotator_ids=None)
        base.update(kw)
        return _passes_filters(rec, **base)

    text, syn, real = _rec(), _rec("multimodal", "synthetic"), _rec("multimodal", "real_deid")
    assert f(text, modality="text") and not f(syn, modality="text")
    assert f(syn, modality="multimodal") and not f(text, modality="multimodal")
    assert f(syn, case_source="synthetic") and not f(real, case_source="synthetic")
    assert not f(text, case_source="synthetic")  # text has no case_source


# ─── real_deid ingest seam (unit) ─────────────────────────────────────────────
def test_age_banding():
    from asclepius.case_formats import age_to_band
    assert age_to_band(74) == "70-79"
    assert age_to_band(90) == "90+" and age_to_band(103) == "90+"
    assert age_to_band(None) is None and age_to_band("x") is None


def test_deidentify_collapses_age_and_rejects_residual_date():
    from asclepius import case_formats as cf
    clean = cf.deidentify({"demographics": {"age": 74, "sex": "F"},
                           "lab_panels": [{"panel": "BMP", "collected_offset_days": -3, "results": []}],
                           "notes": [{"text": "euvolemic; poor intake"}]})
    assert clean["demographics"].get("age") is None
    assert clean["demographics"]["age_band"] == "70-79"
    with pytest.raises(cf.CaseIngestError):
        cf.deidentify({"notes": [{"text": "admitted 03/14/2024 with AKI"}]})


def test_deidentify_rejects_absolute_offset():
    from asclepius import case_formats as cf
    with pytest.raises(cf.CaseIngestError):
        cf.deidentify({"lab_panels": [{"panel": "BMP", "collected_offset_days": "2024-01-01", "results": []}]})


def test_format_registry_dicom_rejected_others_seamed():
    from asclepius import case_formats as cf
    assert set(cf.FORMATS) == set(cf.CASE_FORMATS)
    with pytest.raises(cf.ImagingRejected):
        cf.ingest_real_deid(b"DICM", "dicom")
    for fmt in ("lab_csv", "fhir_r4", "hl7v2"):
        with pytest.raises(cf.CaseFormatNotImplemented):
            cf.ingest_real_deid("raw", fmt)
    with pytest.raises(cf.CaseIngestError):
        cf.ingest_real_deid("raw", "unknown_format")


# ─── End-to-end multimodal export ─────────────────────────────────────────────
client = TestClient(A.app)


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


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _evaluator_h():
    return A.headers_for(A.make_user(_store(), role="evaluator", specialty="nephrology",
                                     board_cert="board_certified_nephrology", years_experience=12))


def _mm_task_body():
    return {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "modality": "multimodal",
        "prompt": f"Classify the hyponatremia {A.uniq(8)} and set a correction rate.",
        "case": {
            "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "70-79", "sex": "M"},
            "lab_panels": [{"panel": "BMP", "collected_offset_days": 0, "results": [
                {"analyte": "Sodium", "value": 112, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"}]}],
            "notes": [{"note_type": "Consult", "author_role": "nephrology", "text": "Euvolemic; chronic thiazide."}],
            "ground_truth": {"answer": "Thiazide-associated hyponatremia", "key_data": ["urine osm"]},
        },
        "candidate_answers": [{"id": "A", "text": "Thiazide hyponatremia; hold thiazide, correct slowly."},
                              {"id": "B", "text": "SIADH; fluid restrict."}],
    }


def _submit_export_ready(admin_h, ev_h):
    tid = client.post("/api/asclepius/tasks", json={"tasks": [_mm_task_body()]}, headers=admin_h).json()["created"][0]
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 140,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Thiazide-associated hyponatremia; hold the thiazide and correct sodium slowly to avoid osmotic demyelination."},
        "chosen_revision": {"edited": False, "why_better_notes": "B ignores the thiazide"},
        "rejected_critique": {"error_tags": ["omission"], "why_worse": "misses thiazide"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready", r.text
    return tid


def test_multimodal_export_filters_and_answer_key_optin():
    admin_h, ev_h = _admin_h(), _evaluator_h()
    _submit_export_ready(admin_h, ev_h)

    # Default export: multimodal records present, answer key WITHHELD.
    m = client.post("/api/asclepius/exports", json={"profile": "default"}, headers=admin_h).json()
    assert m["multimodal_count"] >= 1
    assert m["counts"]["by_modality"].get("multimodal", 0) >= 1
    out_dir = Path(m["dir_path"])
    lines = [json.loads(x) for x in (out_dir / "records.jsonl").read_text().strip().splitlines()]
    assert lines and all("answer_key" not in r for r in lines)
    # The internal answer-key OBJECT never ships: no forbidden ground_truth key,
    # and no key_data marker (which lives only inside the held-out ground_truth).
    assert all('"ground_truth"' not in json.dumps(r) for r in lines)
    assert all("urine osm" not in json.dumps(r) for r in lines)
    datasheet = (out_dir / "datasheet.md").read_text()
    assert "Multimodal cases" in datasheet and "No imaging" in datasheet
    quality = (out_dir / "quality_report.md").read_text()
    assert "Multimodal cases" in quality

    # Benchmark export with include_answer_key: answer key present under answer_key.
    m2 = client.post("/api/asclepius/exports",
                     json={"profile": "default", "modality": "multimodal", "include_answer_key": True,
                           "include_exported": True},
                     headers=admin_h).json()
    out2 = Path(m2["dir_path"])
    l2 = [json.loads(x) for x in (out2 / "records.jsonl").read_text().strip().splitlines()]
    assert l2 and any("answer_key" in r for r in l2)
    ak = next(r["answer_key"] for r in l2 if "answer_key" in r)
    assert ak["answer"] == "Thiazide-associated hyponatremia"


def test_text_only_filter_excludes_multimodal():
    admin_h, ev_h = _admin_h(), _evaluator_h()
    _submit_export_ready(admin_h, ev_h)
    r = client.post("/api/asclepius/exports", json={"profile": "default", "modality": "text"}, headers=admin_h)
    # The only record is multimodal, so a text-only batch has nothing to ship.
    assert r.status_code == 400


def test_invalid_modality_and_case_source_rejected():
    admin_h = _admin_h()
    assert client.post("/api/asclepius/exports", json={"modality": "bogus"}, headers=admin_h).status_code == 400
    assert client.post("/api/asclepius/exports", json={"case_source": "bogus"}, headers=admin_h).status_code == 400
