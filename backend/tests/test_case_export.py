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
from asclepius import review as asc_review  # noqa: E402

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
        # Mirror the router: every review written by the product carries a
        # scan result. NULL means 'never scanned', and unscanned prose is
        # withheld from buyers by design — so a helper that omitted this would
        # be testing a legacy row, not the product.
        identifier_flags=asc_review.scan_review_free_text(notes, corrections),
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


# ─── Phase 5: the case-keyed bundle ───────────────────────────────────────────
def _cases_jsonl(manifest):
    lines = (Path(manifest["dir_path"]) / "cases.jsonl").read_text().strip().splitlines()
    return [json.loads(l) for l in lines]


def test_case_with_two_labelers_and_review_exports_as_one_object():
    from asclepius.export import export_by_case

    admin_h = _admin_h()
    tid = _create_task(admin_h, max_labels=2)
    sid1 = _submit(tid, _evaluator())
    sid2 = _submit(tid, _evaluator())
    _add_review(sid1, tid, verdict="accept")

    manifest = export_by_case(_store(), created_by="admin", case_id=tid)
    assert manifest["case_count"] == 1
    assert manifest["filters"]["case_id"] == tid
    assert "cases.jsonl" in manifest["files"]
    assert manifest["content_hashes"]["cases.jsonl"]

    cases = _cases_jsonl(manifest)
    assert len(cases) == 1  # ONE artifact, not three unrelated rows
    case = cases[0]
    assert case["case_id"] == tid
    assert case["n_labelers"] == 2
    assert {l["submission_id"] for l in case["labels"]} == {sid1, sid2}
    assert all(l["records"] for l in case["labels"])  # mapped records embedded
    assert case["review"]["n_reviews"] == 1
    assert case["consensus"]["n_labels"] == 2
    assert case["consensus"]["majority_verdict"] == "A_better"
    assert case["consensus"]["unanimous"] is True
    assert case["consensus"]["agreement_observation"]["verdict_agree"] is True
    assert case["supervision"]["independent_second_label"] is True
    # Records.jsonl still ships unchanged alongside (compatibility).
    assert (Path(manifest["dir_path"]) / "records.jsonl").exists()
    # No labeler identity anywhere in the case object.
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                assert k not in ("evaluator_id", "email", "full_name", "npi"), k
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(case)


def test_case_export_specialty_filter():
    from asclepius.export import export_by_case

    admin_h = _admin_h()
    tid_neph = _create_task(admin_h, specialty="nephrology")
    tid_card = _create_task(admin_h, specialty="cardiology")
    _submit(tid_neph, _evaluator("nephrology"))
    _submit(tid_card, _evaluator("cardiology"))

    manifest = export_by_case(_store(), created_by="admin", specialty="cardiology")
    cases = _cases_jsonl(manifest)
    assert [c["case_id"] for c in cases] == [tid_card]
    assert all(c["specialty"] == "cardiology" for c in cases)


def test_case_export_portal_version_filter():
    from asclepius.export import export_by_case

    admin_h = _admin_h()
    tid = _create_task(admin_h)
    _submit(tid, _evaluator())
    store = _store()
    stamped = {(r.get("payload") or {}).get("portal_version") for r in store.list_records()}
    version = next(iter(stamped - {None}))

    manifest = export_by_case(store, created_by="admin", portal_version=version)
    cases = _cases_jsonl(manifest)
    assert cases and all(version in c["portal_versions"] for c in cases)

    # A version with no records fails loudly, not silently empty (v3/v4/v5 all
    # route through this same pass-through filter).
    with pytest.raises(ValueError):
        export_by_case(store, created_by="admin", portal_version="v5")


# ═══════════════════════════════════════════════════════════════════════════════
# FIX ROUND — Phase 5: the export seam (A-5.1 .. A-5.5).
# ═══════════════════════════════════════════════════════════════════════════════
def test_case_bundle_sees_previously_exported_labels():
    """A-5.1 / Seam 2: build_export selects only 'export_ready' unless told
    otherwise, so a case whose first labeler already shipped in an earlier batch
    came out with n_labelers=1 AND consensus.unanimous=true computed from that
    single label — a wrong number in a buyer's hands, produced silently."""
    from asclepius.export import export_by_case

    admin_h = _admin_h()
    store = _store()
    tid = _create_task(admin_h, max_labels=2)
    sid1 = _submit(tid, _evaluator())
    sid2 = _submit(tid, _evaluator())

    # Ship labeler A's records in an earlier batch; they leave 'export_ready'.
    _build_export(admin_h, submission_id=sid1)
    assert store.get_submission(sid1)["status"] == "exported"

    cases = _cases_jsonl(export_by_case(store, created_by="admin", case_id=tid))
    assert len(cases) == 1
    case = cases[0]
    # Both labels present — the case is whole.
    assert case["n_labelers"] == 2
    assert {l["submission_id"] for l in case["labels"]} == {sid1, sid2}
    assert case["consensus"]["n_labels"] == 2


def test_export_by_case_signature_is_frozen_for_seam_2():
    """Seam 2: the admin export endpoint calls exactly this signature."""
    import inspect
    from asclepius.export import export_by_case

    sig = inspect.signature(export_by_case)
    for name in ("created_by", "case_id", "specialty", "portal_version", "include_exported"):
        assert name in sig.parameters, f"missing frozen parameter {name!r}"
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["include_exported"].default is True


def test_reviewer_free_text_with_an_identifier_is_withheld_from_buyers():
    """A-5.3: review_block shipped `corrections` verbatim. find_tier_b_leak
    scans KEYS, not values, so a reviewer writing a name or a date into their
    correction shipped that string to a lab."""
    admin_h = _admin_h()
    store = _store()
    tid = _create_task(admin_h)
    sid = _submit(tid, _evaluator())

    reviewer = A.make_user(store, role="evaluator", specialty="nephrology",
                           board_cert="board_certified_nephrology")
    with store._conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "tier" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN tier TEXT")
        conn.execute("UPDATE users SET tier='reviewer' WHERE id=?", (reviewer["id"],))

    client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))
    dirty = "Per Dr. Chen's note the potassium was 6.2 on 03/14/2024."
    r = client.post(f"/api/asclepius/review/{sid}", json={
        "verdict": "accept_with_edits",
        "dimensions": {k: "agree" for k in
                       ("clinical_accuracy", "reasoning_quality", "completeness", "rubric_quality")},
        "corrections": {"notes": dirty},
        "reviewer_notes": dirty,
        "time_spent_sec": 60,
    }, headers=A.headers_for(reviewer))
    assert r.status_code == 200
    body = r.json()
    # The reviewer is TOLD, so they can rewrite it.
    assert body["corrections_withheld"] is True and body["identifier_flags"]

    # The clinical judgment survives; only the prose is held back.
    for rec in _records_jsonl(_build_export(admin_h)):
        entry = rec["review"]["reviews"][0]
        assert entry["verdict"] == "accept_with_edits"
        assert entry["dimensions"]["clinical_accuracy"] == "agree"
        assert entry["corrections"] == {}
        assert entry["corrections_withheld"] is True
    # And the identifier string appears nowhere in the shipped bytes.
    out_dir = Path(_build_export(admin_h, include_exported=True)["dir_path"])
    for name in ("records.jsonl", "cases.jsonl"):
        assert "Chen" not in (out_dir / name).read_text()


def test_clean_reviewer_corrections_still_ship():
    """The withholding must not swallow legitimate corrections."""
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    sid = _submit(tid, _evaluator())
    _add_review(sid, tid, verdict="accept_with_edits",
                corrections={"notes": "Dose should be renally adjusted."})
    entry = _records_jsonl(_build_export(admin_h))[0]["review"]["reviews"][0]
    assert entry["corrections"]["notes"] == "Dose should be renally adjusted."
    assert entry["corrections_withheld"] is False


def test_case_bundle_drops_internal_state_and_duplicate_annexes():
    """A-5.4 internal workflow state ('in_review'/'not_routed') is meaningless
    to a buyer. A-5.5 the same review payload appeared at three nesting levels."""
    from asclepius.export import export_by_case

    admin_h = _admin_h()
    tid = _create_task(admin_h)
    sid = _submit(tid, _evaluator())
    _add_review(sid, tid, verdict="accept")

    case = _cases_jsonl(export_by_case(_store(), created_by="admin", case_id=tid))[0]
    for label in case["labels"]:
        assert "review_status" not in label
        for rec in label["records"]:
            # Stated once, at case level.
            assert "review" not in rec and "supervision" not in rec
    assert case["review"]["n_reviews"] == 1
    assert "independent_second_label" in case["supervision"]


def test_data_dictionary_documents_every_annex_it_ships():
    """A-5.2: `review`, `supervision` and the whole cases.jsonl companion shipped
    to buyers undocumented. An undocumented field in a delivered artifact is
    indistinguishable from a leak."""
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    sid = _submit(tid, _evaluator())
    _add_review(sid, tid, verdict="accept")
    manifest = _build_export(admin_h)
    dd = (Path(manifest["dir_path"]) / "data_dictionary.md").read_text()

    for term in ("review.reviewed", "review.reviews[].verdict", "corrections_withheld",
                 "supervision.independent_second_label", "cases.jsonl",
                 "consensus.majority_verdict", "cannot_assess"):
        assert term in dd, f"data dictionary does not document {term!r}"
    # And it states the honesty rule the whole tier rests on.
    assert "NOT inter-rater agreement" in dd
    assert "out of the buyer profile" in dd.lower() or "out of profile schema" in dd.lower()


def test_unscanned_legacy_review_prose_is_withheld_not_assumed_clean():
    """Tri-state discipline: NULL identifier_flags means 'never scanned', which
    is not 'scanned and clean'. Rows written before A-5.3 shipped have NULL, and
    their free text must be withheld rather than trusted."""
    admin_h = _admin_h()
    store = _store()
    tid = _create_task(admin_h)
    sid = _submit(tid, _evaluator())
    reviewer = A.make_user(store, role="evaluator", specialty="nephrology",
                           board_cert="board_certified_nephrology")
    # A legacy row: inserted with no scan result at all.
    store.insert_case_review(
        task_id=tid, submission_id=sid, reviewer_user_id=reviewer["id"],
        reviewer_id_hashed=reviewer["id_hashed"], verdict="accept_with_edits",
        dimensions={"clinical_accuracy": "agree"},
        corrections={"notes": "Legacy note nobody ever scanned."},
        blinded=True, identifier_flags=None,
    )
    entry = _records_jsonl(_build_export(admin_h))[0]["review"]["reviews"][0]
    assert entry["corrections"] == {}
    assert entry["corrections_withheld"] is True
    # The judgment still ships — only the unscanned prose is held back.
    assert entry["verdict"] == "accept_with_edits"
