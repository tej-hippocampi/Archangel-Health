"""The backend the two new admin pages stand on.

Three things are new and each is here because a screen would otherwise be able to
write a row nobody can act on.

**Staging (§3).** ``purpose`` already decided whether an upload may become tasks;
what did not exist was the state BEFORE that decision, or any record of which kind
of task the admin chose. ``staging`` names the first and ``task_mode`` the second,
so a bundle you come back to tomorrow can describe itself.

**Per-doctor role (§4.3).** ``assignments.role`` has always carried 'label' and
'review'; the explicit-send builder hardcoded 'label', so an admin naming a
reviewer got a labeling assignment and no indication of it.

**The two refusals.** A named reviewer without the reviewer tier, and a task-mode
change after the first task exists. Both write rows that are legal in SQL and
meaningless in the product, which is the class of bug that reads to everyone as
the product being broken rather than as a rule nobody enforced.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin(store):
    return A.headers_for(A.make_user(store, role="admin"))


def _doc(store, *, specialty="nephrology", tier="labeler", approved=True):
    u = A.make_user(store, specialty=specialty, tier=tier)
    if approved:
        store.set_real_data_approved(u["id"], True)
    return store.get_user_by_id(u["id"])


def _upload(store, upload_id="up-1", *, purpose=None, n_cases=1, promoted=0):
    store.insert_ingest_upload(upload_id=upload_id, link_id="lk", partner_id="pt",
                               filename="bundle.zip", sha256="a" * 64,
                               size_bytes=100, raw_path=None, source_ip=None)
    for i in range(n_cases):
        c = store.insert_ingest_case(upload_id=upload_id, patient_key=f"p{i}",
                                     specialty="nephrology", case={"x": i},
                                     status="ingested", report={})
        if i < promoted:
            store.update_ingest_case(c["ingest_case_id"], status="promoted")
    if purpose:
        store.set_upload_purpose(upload_id, purpose)
    return upload_id


def _real_task(store, **kw):
    return store.insert_task(prompt="q", specialty="nephrology",
                             case={"case_source": "real_deid",
                                   "notes": [{"text": "n"}]}, **kw)


# ═══════════════════════════════════════════════════════════════════════════════
# §3.1 — Box 1: what is this, and where does it go
# ═══════════════════════════════════════════════════════════════════════════════
def test_an_undecided_upload_reports_staging_undecided():
    """NULL purpose is a real third state. ``effective_purpose`` resolves it to
    task_creation for PROMOTION, but the admin has not answered yet and Box 1
    exists to ask — so the list must not report the resolved value as a decision."""
    store = _store()
    _upload(store)
    r = client.get("/api/asclepius/ingestion/uploads", headers=_admin(store))
    assert r.status_code == 200
    row = next(u for u in r.json()["uploads"] if u["upload_id"] == "up-1")
    assert row["staging"] == "undecided"
    assert row["purpose"] is None


def test_an_upload_explicitly_filed_as_storage_is_also_undecided():
    """``storage`` is the DEFAULT purpose and explicitly includes NULL —
    "received, stored, and used for nothing until a person says what it is
    for". Testing ``purpose`` for falsiness would file a row deliberately
    marked 'storage' as task creation and offer to build tasks from it. Box 1
    IS the storage bucket."""
    from asclepius import ingestion as asc_ingestion

    store = _store()
    _upload(store, purpose=asc_ingestion.PURPOSE_STORAGE)
    r = client.get("/api/asclepius/ingestion/uploads", headers=_admin(store))
    row = next(u for u in r.json()["uploads"] if u["upload_id"] == "up-1")
    assert row["purpose"] == "storage"
    assert row["staging"] == "undecided"


def test_an_upload_whose_cases_already_shipped_is_history_not_a_decision():
    """Rows predating the storage default were promoted while NULL still
    resolved to task_creation. Asking an operator to decide what they are for
    asks them to decide something that has already happened — they belong in
    the done fold, never in the queue of pending decisions."""
    store = _store()
    _upload(store, n_cases=3, promoted=3)          # NULL purpose, already promoted
    r = client.get("/api/asclepius/ingestion/uploads", headers=_admin(store))
    row = next(u for u in r.json()["uploads"] if u["upload_id"] == "up-1")
    assert row["purpose"] is None
    assert row["staging"] == "task_creation", "not 'undecided' — this already happened"
    assert row["task_creation_complete"] is True


def test_an_unpromoted_null_upload_is_still_a_pending_decision():
    """The other half of the rule: nothing shipped, so the decision is real."""
    store = _store()
    _upload(store, n_cases=3, promoted=0)
    r = client.get("/api/asclepius/ingestion/uploads", headers=_admin(store))
    row = next(u for u in r.json()["uploads"] if u["upload_id"] == "up-1")
    assert row["staging"] == "undecided"


def test_a_task_creation_upload_moves_to_box_two():
    from asclepius import ingestion as asc_ingestion

    store = _store()
    _upload(store, purpose=asc_ingestion.PURPOSE_TASK_CREATION)
    r = client.get("/api/asclepius/ingestion/uploads", headers=_admin(store))
    row = next(u for u in r.json()["uploads"] if u["upload_id"] == "up-1")
    assert row["staging"] == "task_creation"


def test_a_brokering_upload_leaves_both_boxes():
    from asclepius import ingestion as asc_ingestion

    store = _store()
    _upload(store, purpose=asc_ingestion.PURPOSE_BROKERING)
    r = client.get("/api/asclepius/ingestion/uploads", headers=_admin(store))
    row = next(u for u in r.json()["uploads"] if u["upload_id"] == "up-1")
    assert row["staging"] == "brokering"


def test_the_row_carries_the_case_counts_box_two_prints():
    store = _store()
    _upload(store, n_cases=5, promoted=2)
    r = client.get("/api/asclepius/ingestion/uploads", headers=_admin(store))
    row = next(u for u in r.json()["uploads"] if u["upload_id"] == "up-1")
    assert row["case_counts"]["total"] == 5
    assert row["case_counts"]["promoted"] == 2
    assert row["case_counts"]["ingested"] == 3
    assert row["tasks_created"] == 2
    assert row["task_creation_complete"] is False


def test_an_upload_whose_cases_are_all_tasks_is_complete():
    store = _store()
    _upload(store, n_cases=3, promoted=3)
    r = client.get("/api/asclepius/ingestion/uploads", headers=_admin(store))
    row = next(u for u in r.json()["uploads"] if u["upload_id"] == "up-1")
    assert row["task_creation_complete"] is True


def test_the_per_status_chips_are_not_clobbered_by_the_per_upload_counts():
    """Regression, and the reason it is worth pinning.

    The staging fields are computed in a loop over the page of uploads, and the
    endpoint ALSO returns a per-status tally of every upload under the key
    ``counts``. The first implementation named the loop variable ``counts`` too,
    which shadowed it — so the response's status chips silently became whichever
    upload happened to be last in the page, and a filtered request reported one
    upload's case counts as the totals for the whole pipeline.

    Every test of the new fields still passed, because they all read
    ``case_counts``. This asserts the SHAPE of the two dicts stays distinct."""
    from asclepius import ingestion as asc_ingestion

    store = _store()
    _upload(store, "up-a", purpose=asc_ingestion.PURPOSE_TASK_CREATION,
            n_cases=4, promoted=1)
    _upload(store, "up-b", n_cases=2)
    r = client.get("/api/asclepius/ingestion/uploads", headers=_admin(store))
    body = r.json()
    # The per-status chips: keyed by UPLOAD status, and 'all' counts uploads.
    assert set(body["counts"]) == {"all", "ingested", "needs_review",
                                   "quarantined", "rejected"}
    assert body["counts"]["all"] == 2, "two uploads, not a case count"
    # The per-upload tally: keyed by CASE status, and never at the top level.
    row = next(u for u in body["uploads"] if u["upload_id"] == "up-a")
    assert set(row["case_counts"]) == {"total", "ingested", "promoted",
                                       "needs_review", "quarantined", "rejected"}
    assert row["case_counts"]["total"] == 4


def test_the_chips_survive_a_filtered_request():
    """The shadowing only showed itself under ``?status=`` — the unfiltered page
    happened to end on an upload whose numbers looked plausible."""
    store = _store()
    _upload(store, "up-a", n_cases=3)
    r = client.get("/api/asclepius/ingestion/uploads?status=ingested",
                   headers=_admin(store))
    assert set(r.json()["counts"]) == {"all", "ingested", "needs_review",
                                       "quarantined", "rejected"}


def test_an_admin_can_write_a_description_for_a_bundle_that_arrived_without_one():
    store = _store()
    _upload(store)
    r = client.post("/api/asclepius/admin/uploads/up-1/description",
                    json={"description": "  2019–2023 CKD cohort  "},
                    headers=_admin(store))
    assert r.status_code == 200
    assert store.get_ingest_upload("up-1")["description"] == "2019–2023 CKD cohort"


def test_the_partner_door_still_accepts_an_upload_with_no_description():
    """The compatibility contract. Every partner integration posts only ``file``;
    a required field here would break all of them at once."""
    import inspect

    from routers import asclepius as router_mod

    sig = inspect.signature(router_mod.partner_upload)
    assert sig.parameters["description"].default.default == "", (
        "description must default to empty — a partner posting only `file` is the "
        "entire installed base")


# ═══════════════════════════════════════════════════════════════════════════════
# §3.2 — task_mode is a decision that persists
# ═══════════════════════════════════════════════════════════════════════════════
def test_task_mode_persists_so_a_resumed_batch_stays_in_one_mode():
    from asclepius import ingestion as asc_ingestion

    store = _store()
    _upload(store, purpose=asc_ingestion.PURPOSE_TASK_CREATION, n_cases=4)
    r = client.post("/api/asclepius/admin/uploads/up-1/task-mode",
                    json={"task_mode": "longitudinal"}, headers=_admin(store))
    assert r.status_code == 200
    assert store.get_ingest_upload("up-1")["task_mode"] == "longitudinal"


def test_task_mode_is_refused_on_a_brokering_upload():
    """A control that can never do anything is worse than no control."""
    from asclepius import ingestion as asc_ingestion

    store = _store()
    _upload(store, purpose=asc_ingestion.PURPOSE_BROKERING)
    r = client.post("/api/asclepius/admin/uploads/up-1/task-mode",
                    json={"task_mode": "static"}, headers=_admin(store))
    assert r.status_code == 409
    assert "brokering" in r.json()["detail"].lower()


def test_task_mode_cannot_change_once_tasks_exist_in_the_other_mode():
    """The mode describes the tasks that came out. Flipping it afterwards would
    relabel rows that were built the other way."""
    from asclepius import ingestion as asc_ingestion

    store = _store()
    _upload(store, purpose=asc_ingestion.PURPOSE_TASK_CREATION, n_cases=4, promoted=2)
    store.set_upload_task_mode("up-1", "static")
    r = client.post("/api/asclepius/admin/uploads/up-1/task-mode",
                    json={"task_mode": "longitudinal"}, headers=_admin(store))
    assert r.status_code == 409
    assert "already tasks" in r.json()["detail"]
    assert store.get_ingest_upload("up-1")["task_mode"] == "static"


def test_re_sending_the_same_mode_after_promotion_is_allowed():
    """Resuming a half-finished batch is the normal path and must not 409."""
    from asclepius import ingestion as asc_ingestion

    store = _store()
    _upload(store, purpose=asc_ingestion.PURPOSE_TASK_CREATION, n_cases=4, promoted=2)
    store.set_upload_task_mode("up-1", "static")
    r = client.post("/api/asclepius/admin/uploads/up-1/task-mode",
                    json={"task_mode": "static"}, headers=_admin(store))
    assert r.status_code == 200


def test_an_unknown_task_mode_is_a_400_not_a_stored_typo():
    from asclepius import ingestion as asc_ingestion

    store = _store()
    _upload(store, purpose=asc_ingestion.PURPOSE_TASK_CREATION)
    r = client.post("/api/asclepius/admin/uploads/up-1/task-mode",
                    json={"task_mode": "longitudnal"}, headers=_admin(store))
    assert r.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# §4.3 — per-doctor role
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_named_reviewer_gets_a_review_assignment_not_a_labeling_one():
    """The gap this closes: the explicit-send builder hardcoded 'label', so an
    admin who chose "Reviewer" on the row got a labeler and no sign of it."""
    store = _store()
    labeler = _doc(store)
    reviewer = _doc(store, tier="reviewer")
    task = _real_task(store)
    r = client.post("/api/asclepius/admin/assignments/allocate", json={
        "task_ids": [task["task_id"]],
        "user_ids": [labeler["id"], reviewer["id"]],
        "roles": {reviewer["id"]: "review"},
        "dry_run": False,
    }, headers=_admin(store))
    assert r.status_code == 200, r.text
    by_user = {a["user_id"]: a["role"] for a in r.json()["assignments"]}
    assert by_user[labeler["id"]] == "label"
    assert by_user[reviewer["id"]] == "review"
    rows = {a["user_id"]: a["role"]
            for a in store.assignments_for_task(task["task_id"])}
    assert rows[reviewer["id"]] == "review", "the stored row must carry the role too"


def test_a_doctor_absent_from_the_roles_map_is_still_a_labeler():
    """The compatibility default: every explicit send before this field meant
    'these people label these cases'."""
    store = _store()
    doc = _doc(store)
    task = _real_task(store)
    r = client.post("/api/asclepius/admin/assignments/allocate", json={
        "task_ids": [task["task_id"]], "user_ids": [doc["id"]], "dry_run": True,
    }, headers=_admin(store))
    assert r.status_code == 200
    assert r.json()["assignments"][0]["role"] == "label"


def test_naming_a_labeler_as_reviewer_is_refused_by_name():
    """Same shape as the V4 wall refusal beside it: the review queue gates on an
    explicit reviewer tier, so this assignment could never be served."""
    store = _store()
    doc = _doc(store, tier="labeler")
    task = _real_task(store)
    r = client.post("/api/asclepius/admin/assignments/allocate", json={
        "task_ids": [task["task_id"]], "user_ids": [doc["id"]],
        "roles": {doc["id"]: "review"}, "dry_run": True,
    }, headers=_admin(store))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "not_a_reviewer"
    assert doc["id"] in r.json()["detail"]["user_ids"]


def test_the_refusal_happens_on_a_dry_run_too():
    """An admin must learn this while previewing, not after committing."""
    store = _store()
    doc = _doc(store, tier="labeler")
    task = _real_task(store)
    r = client.post("/api/asclepius/admin/assignments/allocate", json={
        "task_ids": [task["task_id"]], "user_ids": [doc["id"]],
        "roles": {doc["id"]: "review"}, "dry_run": True,
    }, headers=_admin(store))
    assert r.status_code == 400
    assert not store.assignments_for_task(task["task_id"])


def test_an_unknown_role_string_is_refused_at_the_door():
    """'labeler' is not 'label'. A third value writes a row no query matches."""
    store = _store()
    doc = _doc(store)
    task = _real_task(store)
    r = client.post("/api/asclepius/admin/assignments/allocate", json={
        "task_ids": [task["task_id"]], "user_ids": [doc["id"]],
        "roles": {doc["id"]: "labeler"}, "dry_run": True,
    }, headers=_admin(store))
    assert r.status_code == 422


def test_the_contention_note_counts_labelers_not_reviewers():
    """A reviewer does not race labelers for a case, so counting one toward
    ``labels_per_case`` would warn about contention that does not exist."""
    store = _store()
    a, b = _doc(store), _doc(store)
    rev = _doc(store, tier="reviewer")
    task = _real_task(store)
    r = client.post("/api/asclepius/admin/assignments/allocate", json={
        "task_ids": [task["task_id"]],
        "user_ids": [a["id"], b["id"], rev["id"]],
        "roles": {rev["id"]: "review"},
        "labels_per_case": 2, "dry_run": True,
    }, headers=_admin(store))
    assert r.status_code == 200
    assert not [n for n in r.json()["notes"] if "doctors named for" in n], \
        "2 labelers at labels_per_case=2 is not contention"


def test_per_physician_counts_land_in_the_right_column():
    store = _store()
    rev = _doc(store, tier="reviewer")
    task = _real_task(store)
    r = client.post("/api/asclepius/admin/assignments/allocate", json={
        "task_ids": [task["task_id"]], "user_ids": [rev["id"]],
        "roles": {rev["id"]: "review"}, "dry_run": True,
    }, headers=_admin(store))
    per = r.json()["per_physician"][rev["id"]]
    assert per == {"label": 0, "review": 1, "total": 1}
