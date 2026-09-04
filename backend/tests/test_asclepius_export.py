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

    # The manifest names what the bundle IS, plainly. These used to live only
    # indirectly — specialty in `scope`, portal version in `counts` — and the
    # license nowhere at all unless the cut was against a licensed key, which is
    # what failed every unlicensed bundle in scripts/export_audit.py.
    shipped = [json.loads(l) for l in
               (out_dir / "records.jsonl").read_text().strip().splitlines()]
    # Every value the manifest asserts must be the value the LINES carry. Read off
    # the file, not off the manifest's own inputs, or the assertion is circular.
    assert batch["license"] == shipped[0]["license"]
    assert {r["license"] for r in shipped} == {batch["license"]}
    assert batch["specialty"] == shipped[0]["specialty"]
    assert batch["portal_version"] == shipped[0]["portal_version"]

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

    # Audit R C2: κ's `blinded` flag is derived from the pre-reveal blind commit,
    # so a route test that wants a κ observation has to walk the gate the real
    # portal enforces (ASCLEPIUS_WITHHOLD_ANSWERS is on by default).
    for _h in (ev1, ev2):
        assert client.post(f"/api/asclepius/tasks/{tid}/reveal",
                           json={"text": "IV calcium, insulin/dextrose, then dialysis."},
                           headers=_h).status_code == 200

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


# ─────────────────────────────────────────────────────────────────────────────
# Buyer-facing truthfulness of the companion documents.
#
# Four defects shipped in the Centaur nephrology sample, all the same shape: a
# companion document asserting something the records do not say. Each is pinned
# here, because each was invisible to every check that existed at the time —
# scripts/export_audit.py passed the bundle that carried all four.
# ─────────────────────────────────────────────────────────────────────────────

def _partner_ehr_rec():
    """A v4 record: a REAL de-identified chart carrying a model-authored question.

    Reaches ``_synthetic_records`` by its ``generation`` block, not by its
    ``source`` — which is exactly the batch shape the datasheet used to describe
    wrongly.
    """
    return {"payload": {
        "source": "partner_ehr",
        "prompt_clinician_reviewed": True,
        "generation": {"seed_corpus_version": "nephrology.v1"},
    }}


def test_datasheet_never_asserts_a_source_the_records_do_not_carry():
    md = _synthetic_provenance_md([_partner_ehr_rec(), _partner_ehr_rec()])
    # The value the records actually carry, stated; the one they do not, absent.
    assert "`partner_ehr`" in md
    assert "internal_prompt_bank" not in md
    # Chart, task origin and question authorship stay three separate axes.
    assert "**Chart**" in md and "**Task origin**" in md and "**Question:**" in md


def _renders_as_a_value(md: str, token: str) -> bool:
    """True when `token` appears as a COUNTED VALUE, e.g. "**1/1** `unspecified`".

    Naming it in prose is fine and sometimes necessary — the chart bullet has to
    reconcile itself against the manifest's `case_provenance`, which does bucket
    absent cases under `unspecified`. What must never happen is the datasheet
    presenting it as a value the records carry, because a buyer will grep
    records.jsonl for it and find nothing.
    """
    import re
    return re.search(rf"\*\*\d+/\d+\*\* `{re.escape(token)}`", md) is not None


def test_datasheet_reports_chart_and_task_origin_as_separate_axes():
    """`source` is task origin; `case_source` is where the chart came from. The
    datasheet used to answer 'where did the chart come from' with `source`, which
    is a different question with a different vocabulary."""
    rec = {"payload": {
        "source": "partner_ehr",
        "context": {"case_source": "real_deid"},
        "prompt_clinician_reviewed": True,
        "generation": {"seed_corpus_version": "nephrology.v1"},
    }}
    md = _synthetic_provenance_md([rec, rec])
    assert "`real_deid`" in md and "context.case_source" in md
    assert "`partner_ehr`" in md and "`source`" in md
    # No value is invented for an absent field, on EITHER axis. Asserted over the
    # whole document: scoping this to one bullet is what let `unspecified` survive
    # on the chart axis while `source` was being fixed one line below it.
    md_missing = _synthetic_provenance_md([{"payload": {
        "generation": {"seed_corpus_version": "x"}}}])
    assert not _renders_as_a_value(md_missing, "unspecified")
    assert "carry no `source`" in md_missing
    assert "carry no `context.case_source`" in md_missing
    # A text-only batch carries neither axis and must not state either fact twice.
    assert md_missing.count("carry no `source`") == 1


def test_datasheet_provenance_renders_for_every_batch_shape():
    """Text-only, mixed-provenance, and no-source-at-all all render cleanly."""
    def rec(**payload):
        payload.setdefault("generation", {"seed_corpus_version": "x"})
        return {"payload": payload}

    text_only = _synthetic_provenance_md([rec()])
    real = rec(source="partner_ehr", context={"case_source": "real_deid"})
    synth = rec(source="internal_prompt_bank", context={"case_source": "synthetic"})
    mixed = _synthetic_provenance_md([real, synth])
    no_source = _synthetic_provenance_md([rec(context={"case_source": "real_deid"})])

    for md in (text_only, mixed, no_source):
        assert not _renders_as_a_value(md, "unspecified")
        assert " · ;" not in md and "; ;" not in md   # no empty tally clause
        assert "None" not in md
    # Mixed batch names both values on each axis, and glosses only what it uses.
    assert "`real_deid`" in mixed and "`synthetic`" in mixed
    assert "`partner_ehr`" in mixed and "`internal_prompt_bank`" in mixed
    # A real_deid-only batch does not define `synthetic`.
    assert "clinician-curated archetype" not in no_source
    # Absence is stated once, in its own clause, not as a value in the list.
    assert no_source.count("carry no `source`") == 1
    assert "`source`" in no_source


def test_datasheet_still_names_internal_prompt_bank_when_that_is_the_source():
    md = _synthetic_provenance_md([_synthetic_rec(reviewed=True)])
    assert "`internal_prompt_bank`" in md
    assert "partner_ehr" not in md


def test_quality_report_labels_the_platform_wide_qa_figure():
    from asclepius.export import _quality_report_md

    md = _quality_report_md(
        export_id="exp-t", profile_name="default",
        records=[{"payload": {"portal_version": "v4"}, "type": "preference",
                  "submission_id": "s-1"}],
        stats={"qa_pass_rate": {"pass_rate": 1.0, "passed": 37, "reviewed": 37},
               "kappa": {}, "flag_counts": {}, "contributors": []},
    )
    batch_line = next(l for l in md.splitlines() if l.startswith("- **This batch**"))
    # The batch line carries the BATCH's numbers and none of the store's.
    assert "1 submission(s), 1 records" in batch_line
    assert "37" not in batch_line
    # The store-wide figure may appear, but never unqualified under a per-batch
    # heading: an unlabelled "37/37" in a 1-record batch is what shipped before.
    flat = " ".join(md.split())
    assert "platform-wide to date — context, NOT a statistic about this batch" in flat
    assert "**1.0** (37 of 37" in flat
    # It must not be called a QA pass rate over "reviewed" submissions: the
    # denominator is export_ready + exported + rejected, which is a packaging
    # outcome, not evidence anybody reviewed anything.
    assert "reviewed submissions" not in md


def test_manifest_license_is_the_shipped_license_not_the_captured_one():
    """The license is re-stamped at emit, so a record captured under the old NC
    license ships commercial. The manifest must say what shipped: reading it off
    the stored record instead put `CC-BY-NC-4.0-clinical-eval` in batch.json
    beside `archangel-commercial-v1` in records.jsonl — and tripped
    export_audit.py's non-commercial check on a wholly commercial bundle."""
    admin_h, ev_h = _admin_h(), _evaluator_h()
    _submit_export_ready(admin_h, ev_h)
    store = _store()
    # Rewrite one record's CAPTURED license to the historical NC value — the state
    # the entire back catalogue is actually in, and which is deliberately never
    # migrated (export.py re-stamps at emit instead).
    rows = store.list_records(status="export_ready")
    assert rows, "no export_ready records to patch"
    store.patch_record_payload(rows[0]["record_id"],
                               {"license": "CC-BY-NC-4.0-clinical-eval"})

    manifest = client.post("/api/asclepius/exports", json={"profile": "default"},
                           headers=admin_h).json()
    out_dir = Path(manifest["dir_path"])
    batch = json.loads((out_dir / "batch.json").read_text())
    shipped = {json.loads(l)["license"]
               for l in (out_dir / "records.jsonl").read_text().strip().splitlines()}
    assert shipped == {batch["license"]}, (
        f"manifest says {batch['license']!r}, lines carry {shipped!r}")
    assert "NC" not in batch["license"]


def test_quality_report_labels_an_unknown_portal_version_as_unknown():
    from asclepius.export import _quality_report_md, PORTAL_VERSION_LABELS

    # v5 is a KNOWN version that used to fall through a dict default to
    # "assisted"; v9 is genuinely unknown and must not be given a plausible label.
    for known in PORTAL_VERSION_LABELS:
        md = _quality_report_md(
            export_id="exp-t", profile_name="default",
            records=[{"payload": {"portal_version": known}, "type": "preference",
                      "submission_id": "s-1"}],
            stats={"kappa": {}, "flag_counts": {}, "contributors": []},
        )
        assert f"{known} ({PORTAL_VERSION_LABELS[known]})" in md
    md = _quality_report_md(
        export_id="exp-t", profile_name="default",
        records=[{"payload": {"portal_version": "v9"}, "type": "preference",
                  "submission_id": "s-1"}],
        stats={"kappa": {}, "flag_counts": {}, "contributors": []},
    )
    assert "v9 (unrecognised portal version)" in md
    for label in PORTAL_VERSION_LABELS.values():
        assert f"v9 ({label})" not in md


def test_data_dictionary_covers_every_vocabulary_value_the_records_can_carry():
    from asclepius.constants import TASK_SOURCES
    from asclepius.export import _data_dictionary_md, PORTAL_VERSION_LABELS

    dd = _data_dictionary_md("default")
    for source in TASK_SOURCES:
        assert f"`{source}`" in dd, f"data_dictionary.md does not define source {source!r}"
    for version in PORTAL_VERSION_LABELS:
        assert f"`{version}`" in dd, f"data_dictionary.md does not define {version!r}"


_EVAL_PACK_STUB = {
    "sku": "asclepius_eval_pack", "title": "Asclepius Rubric Eval Pack",
    "licensing": "re-licensable-per-model-version", "billing": "recurring",
    "revalidation_trigger": "buyer_model_version_change", "recurring_value_usd": 60.0,
    "n_rubrics": 1, "n_probed": 0, "n_validated": 0, "n_needs_review": 0,
    "n_reliable": 0, "n_gameable": 0, "n_premium": 0, "n_grounded": 0,
    "n_critical_negative": 0,
    "files": ["records.jsonl", "grader_prompt.txt", "score.py",
              "validity_report.json", "EVAL_PACK.md"],
}


def test_no_shipped_text_artifact_carries_an_internal_spec_reference():
    """PRD/opt section numbers point at documents no buyer can open.

    Asserted over every generated text artifact, not a sample of them: checking
    three functions while claiming a property of the bundle is how
    `failure_eval/score_failuremode.py` kept shipping `§D-4` after its sibling
    `score.py` was cleaned.
    """
    from asclepius import export as E
    from asclepius.failure_taxonomy import SCORE_FAILUREMODE_PY

    empty_stats = {"kappa": {}, "flag_counts": {}, "contributors": []}
    rec = [{"payload": {"portal_version": "v4", "source": "partner_ehr",
                        "generation": {"seed_corpus_version": "x"}},
            "type": "preference", "submission_id": "s-1"}]
    artifacts = {
        "data_dictionary.md": E._data_dictionary_md("default"),
        "quality_report.md": E._quality_report_md(
            export_id="exp-t", profile_name="default", records=rec, stats=empty_stats),
        "datasheet.md": E._datasheet_md(
            export_id="exp-t", profile_name="default", counts=E._counts(rec),
            records=rec, contributors=[], scope=None, eval_pack=None),
        "score.py": E._SCORE_PY,
        "grader_prompt.txt": E._GRADER_PROMPT,
        "failure_eval/score_failuremode.py": SCORE_FAILUREMODE_PY,
        "EVAL_PACK.md": E._eval_pack_md("exp-t", _EVAL_PACK_STUB),
        # The datasheet's eval-pack section renders only when a pack is present,
        # so the eval_pack=None datasheet above never reaches this text.
        "datasheet.md (eval pack section)": E._datasheet_md(
            export_id="exp-t", profile_name="default", counts=E._counts(rec),
            records=rec, contributors=[], scope=None, eval_pack=_EVAL_PACK_STUB),
    }
    for name, doc in artifacts.items():
        assert "§" not in doc, f"{name} ships an internal spec reference"
        assert "FEAT-" not in doc, f"{name} ships a ticket id"
        assert "PRD" not in doc, f"{name} ships an internal document name"


# ─── _sole_shipped_value: the manifest's "what does every line say" helper ─────
# Lifted to module scope precisely so these cases can be exercised directly. Its
# None branch is what export_audit.py's "records no 'specialty'" check depends on.

def _mapped(*values, field="specialty"):
    emitted = [{"type": "preference"} for _ in values]
    mapped = [({} if v is None else {field: v}) for v in values]
    return emitted, mapped


def test_sole_shipped_value_returns_the_value_every_line_carries():
    from asclepius.export import _sole_shipped_value
    e, m = _mapped("nephrology", "nephrology")
    assert _sole_shipped_value(e, m, {}, "specialty") == "nephrology"


def test_sole_shipped_value_is_none_when_lines_disagree_or_any_line_lacks_it():
    from asclepius.export import _sole_shipped_value
    # Disagreement.
    e, m = _mapped("nephrology", "cardiology")
    assert _sole_shipped_value(e, m, {}, "specialty") is None
    # PARTIAL stamping — a legacy record with no portal_version mixed into a
    # current cut. A bundle-wide claim contradicted by one shipped line is the
    # defect this helper exists to prevent, so it asserts nothing.
    e, m = _mapped("nephrology", None)
    assert _sole_shipped_value(e, m, {}, "specialty") is None
    # Nothing carries it.
    e, m = _mapped(None, None)
    assert _sole_shipped_value(e, m, {}, "specialty") is None
    # Empty batch.
    assert _sole_shipped_value([], [], {}, "specialty") is None


def test_sole_shipped_value_follows_the_profiles_field_rename():
    """A profile renames our canonical field to the buyer's (TEMPLATE.json renames
    annotator_credential to expert). Reading our name off a renamed line reports
    'no specialty' for a bundle whose every line carries one."""
    from asclepius.export import _sole_shipped_value
    prof = {"field_maps": {"ideal_answer": {"specialty": "their_specialty"}}}
    emitted = [{"type": "ideal_answer"}]
    mapped = [{"their_specialty": "nephrology"}]
    assert _sole_shipped_value(emitted, mapped, prof, "specialty") == "nephrology"
    assert _sole_shipped_value(emitted, [{"specialty": "nephrology"}], prof,
                               "specialty") is None
