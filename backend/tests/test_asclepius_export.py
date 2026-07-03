"""Export companions + manifest tests (PRD §5, opt §1.4, §2, §4.12).

Drives a record to export_ready over HTTP, builds an export, and inspects the
on-disk batch: records.jsonl, batch.json manifest (content hashes + profile +
filters + kappa), data_dictionary.md, datasheet.md, quality_report.md.
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
from asclepius.export import _synthetic_provenance_md  # noqa: E402


def _synthetic_rec(*, reviewed: bool, ratified: bool = False):
    """A packaged record from a synthetic (Seedmaker) prompt, as stored."""
    return {"payload": {
        "source": "internal_prompt_bank",
        "prompt_clinician_reviewed": reviewed,
        "generation": {"seed_corpus_version": "nephrology.v1", "seed_corpus_ratified": ratified},
    }}


def test_datasheet_upgrades_language_when_prompts_clinician_reviewed():
    # Unratified corpus but every prompt clinician-reviewed at eval -> upgraded.
    md = _synthetic_provenance_md([_synthetic_rec(reviewed=True), _synthetic_rec(reviewed=True)])
    assert "clinician-reviewed at evaluation" in md
    assert "prompt_clinician_reviewed: true" in md
    assert "NOT yet clinician-ratified" not in md


def test_datasheet_keeps_warning_when_not_reviewed():
    md = _synthetic_provenance_md([_synthetic_rec(reviewed=True), _synthetic_rec(reviewed=False)])
    assert "NOT yet clinician-ratified" in md
    assert "clinician-reviewed at evaluation" not in md

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


def _evaluator_h(specialty="nephrology"):
    return A.headers_for(A.make_user(_store(), role="evaluator", specialty=specialty,
                                     board_cert="board_certified_nephrology", years_experience=12))


def _task_body(**kw):
    base = {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."}, {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    base.update(kw)
    return base


def _submit_export_ready(admin_h, ev_h, **task_kw):
    tid = client.post("/api/asclepius/tasks", json={"tasks": [_task_body(**task_kw)]}, headers=admin_h).json()["created"][0]
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stabilize with IV calcium, shift potassium with insulin and dextrose, then dialyze given the ESRD."},
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready"
    return sid


def test_export_writes_all_companions_and_manifest():
    admin_h, ev_h = _admin_h(), _evaluator_h()
    _submit_export_ready(admin_h, ev_h)

    manifest = client.post("/api/asclepius/exports", json={"profile": "default", "note": "first delivery"},
                           headers=admin_h).json()
    out_dir = Path(manifest["dir_path"])
    assert (out_dir / "records.jsonl").exists()
    assert (out_dir / "batch.json").exists()
    assert (out_dir / "data_dictionary.md").exists()
    assert (out_dir / "datasheet.md").exists()
    assert (out_dir / "quality_report.md").exists()

    # Manifest carries content hashes + profile + filters + kappa (opt §1.4, §2).
    batch = json.loads((out_dir / "batch.json").read_text())
    assert batch["profile"] == "default"
    assert batch["content_hashes"]["records.jsonl"]
    assert "filters" in batch and "kappa" in batch
    assert batch["filters"]["profile"] == "default"

    # records.jsonl validates as JSON, one object per line, carrying provenance.
    lines = (out_dir / "records.jsonl").read_text().strip().splitlines()
    assert lines
    rec = json.loads(lines[0])
    assert rec["annotator_credential"] == "board_certified_nephrology"
    assert rec["license"] and rec["contains_phi"] is False

    # Datasheet + quality report are Datasheets-for-Datasets style (opt §1.4).
    datasheet = (out_dir / "datasheet.md").read_text()
    assert "Datasheet" in datasheet and "Limitations" in datasheet and "Annotator credentials" in datasheet
    quality = (out_dir / "quality_report.md").read_text()
    assert "Cohen's" in quality and "Grounded" in quality and "Contributor breakdown" in quality


def test_export_history_lists_built_batch():
    admin_h, ev_h = _admin_h(), _evaluator_h()
    _submit_export_ready(admin_h, ev_h)
    client.post("/api/asclepius/exports", json={"profile": "default"}, headers=admin_h)
    hist = client.get("/api/asclepius/exports", headers=admin_h).json()["exports"]
    assert len(hist) >= 1


def test_double_label_disagreement_routes_to_qa_then_approve():
    """A double-labeled task with disagreeing verdicts is flagged for re-review
    (κ/agreement gate, opt §1.3), never silently exported; QA can then approve."""
    admin_h = _admin_h()
    ev1 = _evaluator_h()
    ev2 = _evaluator_h()
    tid = client.post("/api/asclepius/tasks", json={"tasks": [_task_body(max_labels=2)]}, headers=admin_h).json()["created"][0]

    s1 = "s-" + uuid.uuid4().hex[:12]
    r1 = client.post("/api/asclepius/submissions", json={
        "submission_id": s1, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "independent_answer": {"text": "Stabilize with IV calcium, shift potassium with insulin and dextrose, then dialyze given the ESRD."},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }, headers=ev1)
    assert r1.status_code == 200
    assert r1.json()["status"] == "export_ready"  # first label passes initially

    # Second evaluator disagrees -> both pulled to needs_qa (low_agreement).
    s2 = "s-" + uuid.uuid4().hex[:12]
    r2 = client.post("/api/asclepius/submissions", json={
        "submission_id": s2, "task_id": tid, "verdict": "B_better",
        "chosen_id": "B", "rejected_id": "A", "time_spent_sec": 130,
        "independent_answer": {"text": "Stabilize with IV calcium, shift potassium with insulin and dextrose, then dialyze given the ESRD."},
        "rejected_critique": {"error_tags": ["omission"], "why_worse": "y"},
    }, headers=ev2)
    assert r2.status_code == 200
    assert r2.json()["status"] == "needs_qa"

    # The first submission was pulled back off export_ready.
    s1_detail = client.get(f"/api/asclepius/submissions/{s1}", headers=admin_h).json()
    assert s1_detail["status"] == "needs_qa"

    # QA approves one of them -> export_ready.
    dec = client.post(f"/api/asclepius/qa/{s2}/decision", json={"decision": "approve"}, headers=admin_h)
    assert dec.status_code == 200
    assert dec.json()["status"] == "export_ready"

    # Aggregate kappa observation is recorded for the task.
    stats = client.get("/api/asclepius/stats", headers=admin_h).json()
    assert stats["kappa"]["n"] >= 1


def test_pre_v2_records_still_export_and_count_as_v1(monkeypatch):
    """Data-preservation guarantee: a record packaged BEFORE the V2 feature — no
    portal_version, stance, assist, or error_tag_reasons fields — must still
    export cleanly (never dropped) and be counted/tagged as v1. Additive
    migrations don't rewrite existing records, so this simulates a real
    pre-upgrade batch sitting in the DB."""
    from asclepius.export import build_export
    from asclepius.store import get_store

    store = get_store()
    admin = A.make_user(store, role="admin")
    # A legacy preference record exactly as the OLD packager emitted it — the new
    # V2 fields simply do not exist on it.
    legacy_payload = {
        "type": "preference",
        "prompt": "Legacy hyperkalemia case — how do you manage?",
        "chosen": "Give IV calcium, then insulin-dextrose, then dialyze.",
        "rejected": "Set dialysate K+ to 1.0 immediately.",
        "context": {"specialty": "nephrology", "difficulty": "hard"},
        "rationale": "safer sequencing",
        "confidence": "high",
        "annotator_credential": "board_certified_nephrology",
        "annotator_specialty": "nephrology",
        "annotator_id_hashed": "legacyhash0001",
        "submission_id": "s-legacy-0001",
        "task_id": "t-legacy-0001",
        "source": "lab_supplied",
        "taxonomy_version": "old",
        "config_version": "old",
        "license": "CC-BY-NC-4.0-clinical-eval",
        "ip_cleared": True,
        "contains_phi": False,
        "captured_at": "2026-05-01T00:00:00",
        # NOTE: no portal_version / stance / assist / error_tag_reasons.
    }
    store.insert_record(
        submission_id="s-legacy-0001", task_id="t-legacy-0001", rtype="preference",
        specialty="nephrology", payload=legacy_payload, status="export_ready",
    )

    # Export everything — the legacy record must be included, not dropped.
    manifest = build_export(store, created_by=admin["id"], profile="default")
    assert manifest["record_count"] >= 1
    # Counted under v1 (unstamped legacy == classic).
    assert manifest["counts"]["by_portal_version"].get("v1", 0) >= 1

    # And it survives the V2 cohort filter as a v1 record.
    store.update_records_status_for_submission("s-legacy-0001", "export_ready")
    v1_manifest = build_export(
        store, created_by=admin["id"], profile="default",
        portal_version="v1", include_exported=True,
    )
    assert v1_manifest["record_count"] >= 1
    assert set(v1_manifest["counts"]["by_portal_version"]) == {"v1"}
