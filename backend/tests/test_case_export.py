"""Case-centric metadata + export tests (PRD A Phases 3 and 5).

Phase 3: the emitted record carries the whole case story — original labeling
plus every review — under ``review`` / ``supervision`` keys (never ``kappa``).

Phase 5: the case-keyed bundle — one case exports as ONE object carrying every
labeler submission, every review, and the derived consensus.
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


def _evaluator(specialty="nephrology"):
    return A.make_user(_store(), role="evaluator", specialty=specialty,
                       board_cert="board_certified_nephrology", years_experience=12)


def _task_body(**kw):
    base = {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    base.update(kw)
    return base


def _create_task(admin_h, **task_kw):
    r = client.post("/api/asclepius/tasks", json={"tasks": [_task_body(**task_kw)]}, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def _submit(task_id, evaluator, verdict="A_better"):
    sid = "s-" + uuid.uuid4().hex[:12]
    salt = A.uniq(6)  # distinct free text per labeler so the dedupe gate never fires
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": task_id, "verdict": verdict,
        "chosen_id": "A" if verdict == "A_better" else "B",
        "rejected_id": "B" if verdict == "A_better" else "A",
        "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": f"Stabilize with IV calcium, shift potassium "
                                       f"with insulin and dextrose, then dialyze ({salt})."},
        "chosen_revision": {"edited": False, "why_better_notes": f"B over-lowers K+ ({salt})"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": f"too aggressive {salt}"},
    }, headers=A.headers_for(evaluator))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready"
    return sid


def _add_review(sid, task_id, *, verdict="accept", blinded=True, reviewer=None,
                dimensions=None, corrections=None, notes=None):
    store = _store()
    reviewer = reviewer or A.make_user(store, role="evaluator", specialty="nephrology",
                                       board_cert="board_certified_nephrology",
                                       years_experience=25)
    return store.insert_case_review(
        task_id=task_id, submission_id=sid,
        reviewer_user_id=reviewer["id"], reviewer_id_hashed=reviewer["id_hashed"],
        verdict=verdict,
        dimensions=dimensions or {"clinical_accuracy": "agree", "reasoning_quality": "agree",
                                  "completeness": "agree", "rubric_quality": "cannot_assess"},
        corrections=corrections, reviewer_notes=notes, time_spent_sec=40,
        blinded=blinded,
    )


def _build_export(admin_h, **body):
    r = client.post("/api/asclepius/exports", json={"profile": "default", **body}, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()


def _records_jsonl(manifest):
    lines = (Path(manifest["dir_path"]) / "records.jsonl").read_text().strip().splitlines()
    return [json.loads(l) for l in lines]


# ─── Phase 3: the record carries the review ───────────────────────────────────
def test_exported_record_carries_review_and_supervision_blocks():
    admin_h = _admin_h()
    labeler = _evaluator()
    tid = _create_task(admin_h)
    sid = _submit(tid, labeler)
    review = _add_review(sid, tid, verdict="accept_with_edits",
                         corrections={"notes": "Tighten the dialysis threshold."})

    recs = _records_jsonl(_build_export(admin_h))
    assert recs
    for rec in recs:
        block = rec["review"]
        assert block["reviewed"] is True
        assert block["n_reviews"] == 1
        entry = block["reviews"][0]
        assert entry["reviewer_id_hashed"] == review["reviewer_id_hashed"]
        assert entry["verdict"] == "accept_with_edits"
        assert entry["dimensions"]["rubric_quality"] == "cannot_assess"
        assert entry["corrections"]["notes"] == "Tighten the dialysis threshold."
        assert entry["blinded"] is True
        assert entry["reviewed_at"]
        # Credential ATTRIBUTE only — resolved from users, no identity.
        assert entry["reviewer_credential"] == "board_certified_nephrology"
        assert block["accepted_without_edits"] is False
        # NOT under a kappa/agreement key (PRD A §0).
        assert "kappa" not in block and "agreement" not in block
        sup = rec["supervision"]
        assert sup["labeler_id_hashed"] == labeler["id_hashed"]
        assert sup["independent_second_label"] is False


def test_unreviewed_record_review_block_is_honest():
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    _submit(tid, _evaluator())
    recs = _records_jsonl(_build_export(admin_h))
    for rec in recs:
        assert rec["review"]["reviewed"] is False
        assert rec["review"]["n_reviews"] == 0
        assert rec["review"]["accepted_without_edits"] is False


def test_accept_only_reviews_roll_up_to_accepted_without_edits():
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    sid = _submit(tid, _evaluator())
    _add_review(sid, tid, verdict="accept")
    recs = _records_jsonl(_build_export(admin_h))
    assert all(r["review"]["accepted_without_edits"] is True for r in recs)


def test_independent_second_label_true_only_for_double_labeled_slice():
    admin_h = _admin_h()
    tid = _create_task(admin_h, max_labels=2)
    _submit(tid, _evaluator())
    _submit(tid, _evaluator())  # second INDEPENDENT labeler, agreeing verdict
    recs = _records_jsonl(_build_export(admin_h))
    assert recs
    assert all(r["supervision"]["independent_second_label"] is True for r in recs)


def test_agreeing_second_label_is_not_a_duplicate_but_cross_task_copy_is():
    """The dedupe gate must not punish the double-label slice: an agreeing
    second INDEPENDENT label is the κ measurement working (PRD A §1.3). A
    same-content copy across tasks still flags."""
    admin_h = _admin_h()
    store = _store()
    # Same task, two evaluators, same verdict, unedited -> identical dedupe hash
    # by construction -> must still reach export_ready (asserted inside _submit).
    tid = _create_task(admin_h, max_labels=2, prompt=f"Anion gap case {A.uniq(8)}?")
    _submit(tid, _evaluator())
    _submit(tid, _evaluator())

    # Identical task content re-uploaded as a NEW task: same hash, different
    # task -> the copy still routes to QA as a duplicate.
    prompt = f"Copied case {A.uniq(8)}?"
    tid_a = _create_task(admin_h, prompt=prompt)
    tid_b = _create_task(admin_h, prompt=prompt)
    _submit(tid_a, _evaluator())
    ev = _evaluator()
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid_b, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "chosen_revision": {"edited": False},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "aggressive"},
    }, headers=A.headers_for(ev))
    assert r.status_code == 200
    assert r.json()["status"] == "needs_qa"
    assert "duplicate" in (store.get_submission(sid)["qa_reason"] or "")


def test_quality_report_names_the_two_statistics_separately():
    """PRD A Phase 4: review acceptance and Cohen's κ appear as two separately
    named figures — the review rate is never presented as κ."""
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    sid = _submit(tid, _evaluator())
    _add_review(sid, tid, verdict="accept")

    manifest = _build_export(admin_h)
    quality = (Path(manifest["dir_path"]) / "quality_report.md").read_text()
    assert "Expert review (reviewer-adjudicated — NOT κ)" in quality
    assert "reviewer-adjudicated" in quality
    assert "independently double-labeled" in quality
    assert "Cohen's" in quality
    # Manifest rollup, under its own honest name.
    assert manifest["review_acceptance"]["n"] == 1
    assert manifest["review_acceptance"]["accept_rate"] == 1.0
    # κ stays min-n gated with a stated reason at tiny n.
    assert manifest["kappa"]["overall"] is None
    assert "not reportable" in manifest["kappa"]["reason"]


def test_unblinded_review_still_present_in_record():
    """blinded=0 review data is not hidden from the buyer — it is only excluded
    from the κ statistic (PRD A Phase 5 contract)."""
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    sid = _submit(tid, _evaluator())
    _add_review(sid, tid, verdict="accept", blinded=False)
    recs = _records_jsonl(_build_export(admin_h))
    for rec in recs:
        assert rec["review"]["n_reviews"] == 1
        assert rec["review"]["reviews"][0]["blinded"] is False
