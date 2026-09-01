"""The §0 hard invariant: this redesign moves where rows are DISPLAYED, never what
they are.

The Admin Tasks redesign replaces two admin pages and adds three columns. The
thing that would make it unshippable is not a rendering bug — it is a migration
that dropped, truncated or re-keyed a row. Fifty-six tasks exist on admin today
and every one of them must still be there, with the same id, afterwards.

So the load-bearing test here is a SNAPSHOT: take the full id set and the count of
the five tables the redesign touches, run the migration a second time on the same
database, and assert both are byte-identical. It is written against the real
migration (``_migrate`` runs on every ``AsclepiusStore`` construction) rather than
against a hand-rolled DDL script, because a test that re-implements the migration
proves only that the re-implementation is safe.

The second property is subtler and is the one the PRD's own draft got wrong. The
display bucket is a CACHE of a derivation over four columns, and two of those
columns are rewritten after insert. A cache nobody re-derives is a cache that is
silently wrong, so ``test_the_display_bucket_never_drifts`` re-derives every row
in the database and asserts the stored value matches. That is what actually keeps
the Routing rail and ``batch_overview`` telling the same story about a task.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

FIVE_TABLES = ("tasks", "submissions", "assignments", "ingest_cases", "ingest_uploads")

#: Primary key per table, so the snapshot compares IDENTITY and not just cardinality.
#: A migration that deleted one row and inserted another would pass a COUNT(*) check.
PK = {
    "tasks": "task_id",
    "submissions": "submission_id",
    "assignments": "assignment_id",
    "ingest_cases": "ingest_case_id",
    "ingest_uploads": "upload_id",
}


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _snapshot(store):
    """{table: (count, frozenset(ids))} for the five tables §0 protects."""
    out = {}
    with store._conn() as conn:
        for tbl in FIVE_TABLES:
            rows = conn.execute(f"SELECT {PK[tbl]} AS pk FROM {tbl}").fetchall()
            ids = frozenset(str(r["pk"]) for r in rows)
            out[tbl] = (len(rows), ids)
    return out


def _populate(store):
    """One row in every table the invariant covers, across all four buckets."""
    from asclepius import ingestion as asc_ingestion

    tasks = [
        store.insert_task(prompt="synthetic", specialty="nephrology"),
        store.insert_task(prompt="gold", specialty="nephrology", source="gold_seed",
                          generation={"mode": "gold_seed"}),
        store.insert_task(prompt="real", specialty="nephrology",
                          case={"case_source": "real_deid", "notes": [{"text": "n"}]}),
        store.insert_task(prompt="walk-0", specialty="nephrology",
                          case={"case_source": "real_deid", "notes": [{"text": "n"}]},
                          trajectory_id="tr-1", sequence_index=0),
        store.insert_task(prompt="walk-1", specialty="nephrology",
                          case={"case_source": "real_deid", "notes": [{"text": "n"}]},
                          trajectory_id="tr-1", sequence_index=1),
    ]
    user = A.make_user(store, specialty="nephrology", tier="labeler")
    store.insert_submission(
        submission_id="sub-1", task_id=tasks[0]["task_id"], evaluator_id=user["id"],
        verdict="a_better", chosen_id="A", rejected_id="B", confidence="high",
        time_spent_sec=30, payload={}, annotator={}, dedupe_hash="dh-1")
    store.upsert_assignment(task_id=tasks[0]["task_id"], user_id=user["id"],
                            role="label", assigned_by="admin@test")
    store.insert_ingest_upload(
        upload_id="up-1", link_id="lk-1", partner_id="pt-1", filename="b.zip",
        sha256="d" * 64, size_bytes=10, raw_path=None, source_ip=None)
    store.insert_ingest_case(upload_id="up-1", patient_key="p1",
                             specialty="nephrology", case={"a": 1},
                             status="ingested", report={})
    store.set_upload_purpose("up-1", asc_ingestion.PURPOSE_TASK_CREATION)
    return tasks


# ═══════════════════════════════════════════════════════════════════════════════
# §0 — nothing is deleted, truncated or re-keyed
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_migration_preserves_every_id_in_all_five_tables():
    """The invariant, stated as the PRD states it: the rows on admin today are the
    same rows afterwards. Re-running ``_migrate`` is the closest a test can get to
    "deploy this branch onto the existing database"."""
    store = _store()
    _populate(store)
    before = _snapshot(store)
    assert before["tasks"][0] == 5, "fixture did not populate"

    store._migrate()  # idempotent by contract — this is the deploy

    after = _snapshot(store)
    for tbl in FIVE_TABLES:
        assert after[tbl][0] == before[tbl][0], f"{tbl}: row count changed"
        assert after[tbl][1] == before[tbl][1], f"{tbl}: id set changed"


def test_the_migration_is_idempotent_across_repeated_boots():
    """Railway restarts. A backfill that is not safe to run twice is a backfill
    that corrupts on the second deploy of the same day."""
    store = _store()
    _populate(store)
    first = _snapshot(store)
    for _ in range(3):
        store._migrate()
    assert _snapshot(store) == first


def test_this_prd_ships_no_destructive_sql():
    """§0: 'If the agent finds itself writing a DELETE FROM tasks, it has misread
    this PRD.' Asserted against the migration body rather than trusted."""
    import inspect
    import io
    import tokenize

    from asclepius import store as store_mod

    body = inspect.getsource(store_mod.AsclepiusStore._migrate)
    # Comments and docstrings are PROSE and routinely contain these words — the
    # migration explains at length why it truncates a timestamp. Scan executable
    # tokens only, or the guard fires on its own explanation.
    code = []
    for tok in tokenize.generate_tokens(io.StringIO(body).readline):
        if tok.type == tokenize.COMMENT:
            continue
        code.append(tok.string)
    lowered = " ".join(code).lower()
    for forbidden in ("delete from tasks", "drop table tasks", "truncate table",
                      "delete from submissions", "delete from assignments",
                      "delete from ingest_cases", "delete from ingest_uploads"):
        assert forbidden not in lowered, f"_migrate contains {forbidden!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# §5 — every task carries a bucket, and it is the RIGHT one
# ═══════════════════════════════════════════════════════════════════════════════
def test_every_task_has_a_non_null_bucket_after_migration():
    store = _store()
    _populate(store)
    store._migrate()
    with store._conn() as conn:
        missing = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE display_bucket IS NULL").fetchone()[0]
    assert missing == 0


def test_the_four_buckets_classify_as_the_prd_predicts():
    store = _store()
    tasks = _populate(store)
    got = {t["prompt"]: store.get_task(t["task_id"])["display_bucket"] for t in tasks}
    assert got == {
        "synthetic": "synthetic",
        "gold": "physician_authored",
        "real": "static_real",
        "walk-0": "longitudinal_real",
        "walk-1": "longitudinal_real",
    }


def test_a_trajectory_point_is_longitudinal_even_though_it_is_also_real_deid():
    """Order in the derivation is load-bearing, not alphabetical: every
    longitudinal point is ALSO ``case_source='real_deid'``, so a rule that tested
    real_deid first would empty the longitudinal rail entirely."""
    store = _store()
    t = store.insert_task(prompt="p", specialty="nephrology",
                          case={"case_source": "real_deid", "notes": [{"text": "n"}]},
                          trajectory_id="tr-9", sequence_index=0)
    assert t["case_source"] == "real_deid"
    assert t["display_bucket"] == "longitudinal_real"


# ═══════════════════════════════════════════════════════════════════════════════
# The cache cannot go stale — the property the PRD's draft predicate lost
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_display_bucket_never_drifts_from_its_derivation():
    """Re-derive every row and compare. This is the test that keeps the stored
    column honest, and it is why storing a derived value is safe here at all."""
    from asclepius.store import display_bucket_for_row

    store = _store()
    _populate(store)
    # Exercise both paths that rewrite a discriminator after insert.
    plain = store.insert_task(prompt="becomes real", specialty="nephrology")
    store.update_task_case(plain["task_id"],
                           {"case_source": "real_deid", "notes": [{"text": "n"}]})
    gold = store.insert_task(prompt="regraded gold", specialty="nephrology",
                             source="gold_seed", generation={"mode": "gold_seed"})
    store.set_task_candidates(gold["task_id"], candidates=[{"id": "A", "text": "x"}],
                              generation_patch={"mode": "grade_real_models"})

    drifted = [(t["task_id"], t["display_bucket"], display_bucket_for_row(t))
               for t in store.list_tasks(limit=1000)
               if t["display_bucket"] != display_bucket_for_row(t)]
    assert not drifted, f"stored bucket disagrees with derivation: {drifted}"


def test_grading_a_gold_case_does_not_reclassify_it_as_synthetic():
    """The specific regression the PRD's draft predicate would have shipped.

    ``json_extract(generation_json,'$.mode') = 'gold'`` matches nothing (the
    literal written by ``gold_cases.py`` is 'gold_seed'), and even the corrected
    literal is rewritten by "Grade real", which patches mode to
    'grade_real_models'. Physician-authored provenance is read off ``source``,
    which no code path rewrites."""
    store = _store()
    t = store.insert_task(prompt="g", specialty="nephrology", source="gold_seed",
                          generation={"mode": "gold_seed"})
    assert t["display_bucket"] == "physician_authored"
    store.set_task_candidates(t["task_id"], candidates=[{"id": "A", "text": "x"}],
                              generation_patch={"mode": "grade_real_models"})
    after = store.get_task(t["task_id"])
    assert after["generation"]["mode"] == "grade_real_models"
    assert after["display_bucket"] == "physician_authored"


def test_the_bucket_grouping_agrees_with_batch_overview():
    """Two surfaces, one grouping. ``batch_overview`` counts in SQL and the bucket
    is derived in Python; if they ever disagree the rail and the list describe the
    same task differently and neither is obviously wrong."""
    store = _store()
    _populate(store)
    ov = store.batch_overview()
    buckets = [t["display_bucket"] for t in store.list_tasks(limit=1000)]
    assert (ov.get("longitudinal") or {}).get("n_points", 0) == \
        buckets.count("longitudinal_real")
    assert (ov.get("real_static") or {}).get("n_cases", 0) == buckets.count("static_real")
    # Gold sits in the synthetic class for batch_overview (its case_source is not
    # real_deid), and carries the physician_authored CHIP rather than a fourth rail.
    assert (ov.get("synthetic") or {}).get("n_cases", 0) == \
        buckets.count("synthetic") + buckets.count("physician_authored")


# ═══════════════════════════════════════════════════════════════════════════════
# §3 — the staging columns
# ═══════════════════════════════════════════════════════════════════════════════
def test_upload_description_and_task_mode_round_trip():
    store = _store()
    _populate(store)
    store.set_upload_description("up-1", "  2019–2023 CKD cohort, deid'd in-house  ")
    store.set_upload_task_mode("up-1", "longitudinal")
    up = store.get_ingest_upload("up-1")
    assert up["description"] == "2019–2023 CKD cohort, deid'd in-house"
    assert up["task_mode"] == "longitudinal"


def test_a_blank_description_is_stored_as_absent_not_as_an_empty_quote():
    store = _store()
    _populate(store)
    store.set_upload_description("up-1", "   ")
    assert store.get_ingest_upload("up-1")["description"] is None


def test_an_unrecognised_task_mode_is_refused_at_the_write():
    """The UI branches on this string. A typo stored here renders a row with
    neither mode selected and no way to tell why."""
    store = _store()
    _populate(store)
    with pytest.raises(ValueError):
        store.set_upload_task_mode("up-1", "longitudnal")  # transposed


def test_upload_task_counts_reports_what_promote_all_would_act_on():
    store = _store()
    _populate(store)
    counts = store.upload_task_counts("up-1")
    assert counts["total"] == 1 and counts["ingested"] == 1 and counts["promoted"] == 0
