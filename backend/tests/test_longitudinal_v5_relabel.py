"""V5 = longitudinal, ENV = environments (Longitudinal E2E PRD §5, §6 "v5").

The relabel is not a rename: it changes what ``v5`` MEANS on a buyer-facing
provenance field, and two things had to become true at once for it to be safe —
the V4 and V5 queues must partition the real pool, and a submission's version
must be derived from the task's shape rather than believed from the client.

``test_asclepius_env_isolation.py`` owns the vocabulary boundaries and the ENV
side. This file owns the longitudinal side: stamping, the queue split, the export
scope, and the migration.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from asclepius import trajectory as asc_trajectory  # noqa: E402

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


@pytest.fixture(autouse=True)
def _fresh():
    A.fresh_store()
    yield


def _admin():
    return A.make_user(_store(), role="admin")


def _doctor(store=None, approved=True):
    store = store or _store()
    user = A.make_user(store, role="evaluator", specialty="hepatology",
                       board_cert="board_certified_hepatology", years_experience=11)
    if approved:
        store.set_real_data_approved(user["id"], True)
    return store.get_user_by_id(user["id"]) or user


def _real_case(**over):
    base = {
        "case_source": "real_deid", "specialty": "hepatology",
        "demographics": {"age_band": "30-39", "sex": "M"},
        "lab_panels": [{"panel": "LFT", "collected_offset_days": -2, "results": [
            {"analyte": "GGT", "value": 1361, "unit": "U/L",
             "ref_low": 5, "ref_high": 40, "flag": "H"}]}],
        "notes": [{"note_type": "Progress", "author_role": "hepatology",
                   "collected_offset_days": -1, "text": "Cholestatic picture."}],
    }
    base.update(over)
    return base


def _candidates():
    return [{"id": "A", "text": "Drainage has worked; the bilirubin lags."},
            {"id": "B", "text": "Repeat ERCP now."}]


def _walk(store, n=3, *, specialty="hepatology"):
    """A chart walk: n ordered points sharing one trajectory_id."""
    tid = asc_trajectory.new_trajectory_id()
    return tid, [
        store.insert_task(
            prompt=f"Decision point {i}: what now?", specialty=specialty,
            case=_real_case(specialty=specialty), max_labels=1,
            candidate_answers=_candidates(),
            generation={"index_event_offset": -120 + (i * 30)},
            trajectory_id=tid, sequence_index=i, distribution="assigned_only",
        )
        for i in range(n)
    ]


def _static_real(store, *, specialty="hepatology"):
    return store.insert_task(
        prompt="A static real case.", specialty=specialty,
        case=_real_case(specialty=specialty), max_labels=1,
        candidate_answers=_candidates(), source="partner_ehr")


def _synthetic(store):
    return store.insert_task(prompt="A synthetic case.", specialty="hepatology",
                             max_labels=1, candidate_answers=_candidates())


def _submit(task_id, headers, portal_version):
    sid = "s-" + uuid.uuid4().hex[:12]
    return sid, client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": task_id, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 300,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "quick stance"},
        "portal_version": portal_version,
        "chosen_revision": {"edited": True, "revised_text": "refined", "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }, headers=headers)


# ═══ stamping — derived from the task's shape, never believed from the client ══
def test_a_trajectory_submission_is_stamped_v5():
    store = _store()
    doc, admin_h = _doctor(store), A.headers_for(_admin())
    _tid, points = _walk(store, n=2)
    h = A.headers_for(doc)
    store.upsert_assignment(task_id=points[0]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="admin-test")
    client.post(f"/api/asclepius/tasks/{points[0]['task_id']}/reveal",
                json={"text": "stance"}, headers=h)
    sid, r = _submit(points[0]["task_id"], h, None)
    assert r.status_code == 200, r.text
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["portal_version"] == "v5"


def test_a_static_real_submission_is_still_stamped_v4():
    """The V4 wall is untouched. If this drifts, the relabel took real static
    cases with it and every V4 record in the buyer's bundle is mislabelled."""
    store = _store()
    doc, admin_h = _doctor(store), A.headers_for(_admin())
    task = _static_real(store)
    h = A.headers_for(doc)
    client.post(f"/api/asclepius/tasks/{task['task_id']}/reveal",
                json={"text": "stance"}, headers=h)
    sid, r = _submit(task["task_id"], h, None)
    assert r.status_code == 200, r.text
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["portal_version"] == "v4"


def test_a_v5_claim_on_a_non_trajectory_task_is_a_400():
    """Both directions: a synthetic task and a real STATIC one. Neither is part
    of a walk, so neither can be V5 — the claim is refused rather than
    normalized, because quietly stamping it something else corrupts provenance in
    a field a buyer audits.

    Asserted on the REVEAL, which is where a client's declared version actually
    lands: the reveal commit stamps the version that drove the capture kind, and
    submit reads it from there. See the companion test below for why that is not
    a gap."""
    store = _store()
    doc = _doctor(store)
    h = A.headers_for(doc)
    for task in (_synthetic(store), _static_real(store)):
        r = client.post(f"/api/asclepius/tasks/{task['task_id']}/reveal",
                        json={"text": "stance", "portal_version": "v5"}, headers=h)
        assert r.status_code == 400, r.text
        assert "trajectory" in r.text


def test_a_v4_claim_on_a_trajectory_point_is_a_400():
    """The mirror, and the one that would silently break a sold artifact: a walk
    stamped v4 ships as unrelated static cases with no reassembly key."""
    store = _store()
    doc = _doctor(store)
    _tid, points = _walk(store, n=2)
    store.upsert_assignment(task_id=points[0]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="admin-test")
    h = A.headers_for(doc)
    r = client.post(f"/api/asclepius/tasks/{points[0]['task_id']}/reveal",
                    json={"text": "stance", "portal_version": "v4"}, headers=h)
    assert r.status_code == 400, r.text
    assert "V5 flow" in r.text


def test_the_reveal_commit_outranks_a_later_claim_on_the_submission():
    """Why the two tests above assert on the reveal and not on submit.

    ``submit`` takes the version from the reveal COMMIT when one exists — it is
    what drove the capture kind, so it is the honest record of how the physician
    actually worked — and only falls back to the request body when there is no
    commit. So a client that reveals honestly and then claims something else on
    submit does not get a 400; it gets ignored, which is stronger. Pinned here
    because it looks like a hole and is the opposite of one."""
    store = _store()
    doc, admin_h = _doctor(store), A.headers_for(_admin())
    _tid, points = _walk(store, n=2)
    store.upsert_assignment(task_id=points[0]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="admin-test")
    h = A.headers_for(doc)
    client.post(f"/api/asclepius/tasks/{points[0]['task_id']}/reveal",
                json={"text": "stance"}, headers=h)
    sid, r = _submit(points[0]["task_id"], h, "v3")   # a late, false claim
    assert r.status_code == 200, r.text
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["portal_version"] == "v5"


def test_a_direct_api_submit_with_no_commit_still_derives_v5():
    """The fallback path — no reveal commit, so the body's claim is what reaches
    the wall. It is DERIVED, not believed: a trajectory point is v5 whatever the
    client said, and a wrong claim is refused rather than accepted."""
    store = _store()
    doc, admin_h = _doctor(store), A.headers_for(_admin())
    _tid, points = _walk(store, n=2)
    store.upsert_assignment(task_id=points[0]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="admin-test")
    h = A.headers_for(doc)
    _sid, bad = _submit(points[0]["task_id"], h, "v4")
    assert bad.status_code == 400, bad.text
    sid, ok = _submit(points[0]["task_id"], h, None)
    assert ok.status_code == 200, ok.text
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["portal_version"] == "v5"


# ═══ the queue split — the load-bearing half (§5.4 Group B) ═══════════════════
def test_the_v4_queue_never_serves_a_trajectory_point():
    """Before the split this was FALSE and silently so: a trajectory point is
    ``case_source='real_deid'``, so the V4 wall admitted it and a physician who
    chose "Real cases" could be handed decision point 0 of somebody's chart walk
    — inside a flow with no sequence UI, no reveal and no self-score."""
    store = _store()
    doc = _doctor(store)
    _tid, points = _walk(store, n=3)
    store.upsert_assignment(task_id=points[0]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="admin-test")
    _static_real(store)   # something the V4 queue legitimately holds
    served = store.eligible_tasks_for_evaluator(
        evaluator_id=doc["id"], specialty="hepatology", real_only=True)
    assert served, "the V4 queue must still serve static real cases"
    assert not [t for t in served if t.get("trajectory_id")]


def test_the_v5_queue_never_serves_a_static_case():
    store = _store()
    doc = _doctor(store)
    _tid, points = _walk(store, n=3)
    store.upsert_assignment(task_id=points[0]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="admin-test")
    static = _static_real(store)
    served = store.eligible_tasks_for_evaluator(
        evaluator_id=doc["id"], specialty="hepatology", trajectory_only=True)
    assert [t["task_id"] for t in served] == [points[0]["task_id"]]
    assert static["task_id"] not in {t["task_id"] for t in served}


def test_the_v5_queue_still_requires_real_data_approval():
    """A chart walk is more sensitive than a static real case, not less, so the
    same BAA/training gate applies. An unapproved physician asking for v5 gets an
    empty queue — never a real chart."""
    store = _store()
    unapproved = _doctor(store, approved=False)
    _tid, points = _walk(store, n=2)
    store.upsert_assignment(task_id=points[0]["task_id"], user_id=unapproved["id"],
                            role="label", assigned_by="admin-test")
    r = client.get("/api/asclepius/tasks/available?portal_version=v5",
                   headers=A.headers_for(unapproved))
    assert r.status_code == 200
    assert r.json()["tasks"] == []
    assert r.json()["longitudinal_available"] == 0


def test_the_dashboard_reports_how_many_points_are_routed_to_this_doctor():
    """The V5 tab renders only when this is non-zero. Counted through the same
    eligibility the queue uses, so a tab can never appear over an empty queue."""
    store = _store()
    doc = _doctor(store)
    _tid, points = _walk(store, n=4)
    h = A.headers_for(doc)
    # Unrouted: assigned_only with no assignment is invisible, and the count says so.
    assert client.get("/api/asclepius/tasks/available?portal_version=v5",
                      headers=h).json()["longitudinal_available"] == 0
    store.upsert_assignment(task_id=points[0]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="admin-test")
    # One, not four: the sequence seal means only point 0 is openable.
    assert client.get("/api/asclepius/tasks/available?portal_version=v5",
                      headers=h).json()["longitudinal_available"] == 1


# ═══ export (§5.1, §5.3) ══════════════════════════════════════════════════════
def test_version_to_portal_round_trips_v3_v4_v5():
    from routers.asclepius_admin import _VERSION_TO_PORTAL

    assert _VERSION_TO_PORTAL == {"V3": "v3", "V4": "v4", "V5": "v5"}


def test_the_export_version_list_has_three_entries_each_described():
    admin_h = A.headers_for(_admin())
    body = client.get("/api/asclepius/admin/export/case-options", headers=admin_h).json()
    assert body["versions"] == ["V3", "V4", "V5"]
    assert set(body["version_descriptions"]) == {"V3", "V4", "V5"}
    assert body["version_descriptions"]["V5"] == "real longitudinal"


def test_a_v5_scope_export_ships_the_whole_walk_in_order():
    """§5.3 — selecting one point exports every point of that point's walk,
    ordered. A fragment of a trajectory is not a cheaper trajectory: point 2 with
    no point 1 has no state to have been reasoned from."""
    from asclepius import export as asc_export

    store = _store()
    tid, points = _walk(store, n=3)
    # Records land out of order on purpose — the sort is what is under test.
    for i in (2, 0, 1):
        _mk_record(store, points[i], tid, i)

    res = asc_export.export_by_case(
        store, created_by="admin", case_id=points[1]["task_id"],
        portal_version="v5")
    assert res["record_count"] == 3, "one point selected must export the whole walk"

    import json
    seq = [(json.loads(l).get("trajectory") or {}).get("sequence_index")
           for l in _records_jsonl(res)]
    assert seq == [0, 1, 2], f"the walk must ship ordered, got {seq}"


def test_a_v5_scope_export_names_the_walk_in_the_datasheet():
    from asclepius import export as asc_export

    store = _store()
    tid, points = _walk(store, n=2)
    for i, p in enumerate(points):
        _mk_record(store, p, tid, i)
    res = asc_export.export_by_case(store, created_by="admin",
                                    case_id=points[0]["task_id"], portal_version="v5")
    sheet = _datasheet(res)
    assert "V5 longitudinal" in sheet
    assert "1 trajectory · 2 points" in sheet
    # The reassembly instruction ships WITH the bundle, not only in a PRD.
    assert "trajectory.sequence_index" in sheet


def test_a_v4_export_datasheet_has_no_longitudinal_scope_line():
    """The line is emitted only when a walk is actually present, so a V1–V4
    datasheet is byte-for-byte what it was."""
    from asclepius import export as asc_export

    store = _store()
    task = _static_real(store)
    _mk_record(store, task, None, None, portal_version="v4")
    res = asc_export.export_by_case(store, created_by="admin",
                                    case_id=task["task_id"], portal_version="v4")
    assert "V5 longitudinal" not in _datasheet(res)


# ═══ migration (§5.2) ═════════════════════════════════════════════════════════
def test_the_backfill_is_idempotent_and_loses_no_rows():
    store = _store()
    tid, points = _walk(store, n=2)
    # A submission filed as v4 on a trajectory point — the exact row the branch
    # state could contain, and the one the migration exists for.
    store.insert_submission(
        submission_id="s-legacy-1", task_id=points[0]["task_id"], evaluator_id="e1",
        verdict="A_better", chosen_id="A", rejected_id="B", confidence="high",
        time_spent_sec=300, payload={}, annotator={}, dedupe_hash=None,
        portal_version="v4")

    first = store.migrate_portal_versions_for_longitudinal()
    assert first["longitudinal_backfilled"] == 1
    assert first["total_before"] == first["total_after"], "a relabel adds and drops nothing"

    again = store.migrate_portal_versions_for_longitudinal()
    assert again["longitudinal_backfilled"] == 0, "re-running must be a no-op"
    assert again["total_after"] == first["total_after"]
    assert store.get_submission("s-legacy-1")["portal_version"] == "v5"


def test_the_backfill_leaves_a_static_real_submission_alone():
    store = _store()
    task = _static_real(store)
    store.insert_submission(
        submission_id="s-static-1", task_id=task["task_id"], evaluator_id="e1",
        verdict="A_better", chosen_id="A", rejected_id="B", confidence="high",
        time_spent_sec=300, payload={}, annotator={}, dedupe_hash=None,
        portal_version="v4")
    store.migrate_portal_versions_for_longitudinal()
    assert store.get_submission("s-static-1")["portal_version"] == "v4"


def test_an_ambiguous_v5_row_is_reported_and_never_rewritten():
    """A 'v5' submission on a task that is neither an env run nor a trajectory
    point has no fact saying which it was. Guessing would put an unattributable
    row into a buyer's provenance, so it is counted and left."""
    store = _store()
    task = _synthetic(store)
    store.insert_submission(
        submission_id="s-ambig-1", task_id=task["task_id"], evaluator_id="e1",
        verdict="A_better", chosen_id="A", rejected_id="B", confidence="high",
        time_spent_sec=300, payload={}, annotator={}, dedupe_hash=None,
        portal_version="v5")
    res = store.migrate_portal_versions_for_longitudinal()
    assert "s-ambig-1" in res["ambiguous_v5_submission_ids"]
    assert store.get_submission("s-ambig-1")["portal_version"] == "v5"


# ─── helpers that touch the export plumbing ───────────────────────────────────
def _mk_record(store, task, trajectory_id, sequence_index, portal_version="v5"):
    """One export-ready record for a task, with the trajectory annex attached.

    Shaped like the records the packager actually writes (``type: preference``,
    the credential + licence fields), because ``build_export`` drops anything the
    buyer profile cannot map — a shortcut fixture would fail with "no records
    match the profile" and prove nothing about the trajectory scope."""
    sid = "s-" + uuid.uuid4().hex[:10]
    specialty = task.get("specialty") or "hepatology"
    payload = {
        "type": "preference",
        "prompt": task.get("prompt") or "How do you manage this?",
        "chosen": "Drainage has worked; the bilirubin lags.",
        "rejected": "Repeat ERCP now.",
        "context": {"specialty": specialty, "difficulty": "hard"},
        "rationale": "the enzymes have normalised",
        "confidence": "high",
        "annotator_credential": "board_certified_hepatology",
        "annotator_specialty": specialty,
        "annotator_id_hashed": "realhash" + uuid.uuid4().hex[:6],
        "submission_id": sid, "task_id": task["task_id"],
        "source": "partner_ehr", "taxonomy_version": "t1", "config_version": "c1",
        "license": "CC-BY-NC-4.0-clinical-eval",
        "ip_cleared": True, "contains_phi": False,
        "portal_version": portal_version,
    }
    if trajectory_id:
        payload["trajectory"] = {"trajectory_id": trajectory_id,
                                 "sequence_index": sequence_index}
    return store.insert_record(
        submission_id=sid, task_id=task["task_id"], rtype="preference",
        specialty=specialty, status="export_ready", payload=payload)


def _records_jsonl(res):
    """The bundle's records, in the order they were written — an export is a
    DIRECTORY until someone downloads it, so this reads the file rather than a zip."""
    from pathlib import Path
    text = (Path(res["dir_path"]) / "records.jsonl").read_text()
    return [line for line in text.splitlines() if line.strip()]


def _datasheet(res):
    from pathlib import Path
    return (Path(res["dir_path"]) / "datasheet.md").read_text()


def test_an_env_stamped_record_never_appears_under_any_portal_version_scope():
    """§6 — "env rollouts stamp 'env'; never appear under any V1–V5 scope".

    An agentic rollout lives in ``env_runs`` and produces no ``records`` row at
    all, so in ordinary operation this is true by construction. What this asserts
    is the case that is NOT by construction: a record carrying ``portal_version:
    'env'`` — which the §5.2 migration can leave on a historical row — must be
    invisible to every V1–V5 export scope rather than falling into one of them.

    The failure this guards against is specific and silent. ``env`` is
    deliberately outside ``PORTAL_VERSIONS``, so anything that normalizes it gets
    the DEFAULT, ``v3`` — and the record would ship to a buyer as V3 synthetic
    seamless work.
    """
    from asclepius import export as asc_export

    store = _store()
    task = _synthetic(store)
    _mk_record(store, task, None, None, portal_version="env")

    for version in ("v1", "v2", "v3", "v4", "v5"):
        with pytest.raises(ValueError, match="No export-ready records"):
            asc_export.export_by_case(store, created_by="admin",
                                      case_id=task["task_id"], portal_version=version)


def test_packaging_does_not_relabel_env_as_v3():
    """The same fact one layer down, where the relabel would actually happen."""
    from asclepius.packaging import _portal_version

    assert _portal_version({"portal_version": "env"}, {}) == "env"
    # …while everything unknown still falls to the default, as before.
    assert _portal_version({"portal_version": "v9"}, {}) == "v3"
    assert _portal_version({"portal_version": "v5"}, {}) == "v5"


def test_the_dashboard_count_is_a_sql_count_not_a_materialized_list():
    """Found by auditing, not by a failing test: the obvious spelling of this
    count — ``len(eligible_tasks_for_evaluator(...))`` — fetches the full task
    row for every candidate. Measured at 217 ms for a physician holding 200
    routed points, paid on EVERY dashboard load including v3 and v4, where the
    number is never read.

    Two things are asserted, and the second is why the count was routed through
    ``labeler_queue_sql`` in the first place: it must stay CHEAP, and it must
    keep agreeing with the queue it describes."""
    store = _store()
    doc = _doctor(store)
    for _ in range(40):
        _tid, points = _walk(store, n=3)
        store.upsert_assignment(task_id=points[0]["task_id"], user_id=doc["id"],
                                role="label", assigned_by="admin-test")

    n_sql = store.count_eligible_tasks_for_evaluator(
        evaluator_id=doc["id"], specialty="hepatology", trajectory_only=True)
    n_rows = len(store.eligible_tasks_for_evaluator(
        evaluator_id=doc["id"], specialty="hepatology", trajectory_only=True))
    assert n_sql == n_rows == 40, (n_sql, n_rows)

    # The endpoint reports the same number the queue would serve.
    body = client.get("/api/asclepius/tasks/available?portal_version=v5&limit=5",
                      headers=A.headers_for(doc)).json()
    assert body["longitudinal_available"] == 40

    # …and it is a COUNT, not a fetch. A materializing implementation issues one
    # get_task per candidate; this asserts the router never does that.
    import inspect
    import routers.asclepius as R
    src = inspect.getsource(R.list_available_tasks) if hasattr(R, "list_available_tasks") else None
    if src is None:                       # the endpoint's name may differ
        src = inspect.getsource(R)
    idx = src.find("longitudinal_available = ")
    assert idx != -1
    window = src[idx:idx + 700]
    assert "count_eligible_tasks_for_evaluator" in window, window[:300]
    assert "len(store.eligible_tasks_for_evaluator" not in window, (
        "the dashboard count went back to materializing every candidate row")
