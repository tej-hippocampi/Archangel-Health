"""Advisor PRD Phase 3 — advisory sign-off, one mechanism over four artifacts.

Two properties matter more than the rest:

  * **The security boundary (§4.3).** An advisor may see de-identified clinical
    content; they may NOT see the raw pre-de-identification hospital upload,
    sealed ground truth for a case they might later label, or another
    physician's identity when reviewing their submission. An advisor is more
    senior, not exempt.
  * **The relationship is written by the server.** An advisor holding equity who
    attests that a batch is good enough to ship is a related-party attestation.
    A disclosure the subject of it can author is not a disclosure.

And one property that is NOT here on purpose: nothing blocks. An export always
builds and always ships; the advisor's verdict rides alongside it as feedback.
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
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _admin():
    return A.make_user(asc_store.get_store(), role="admin")


def _advisor():
    store = asc_store.get_store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    return store.appoint_advisor(u["id"], agreement_ref="AGR-1", appointed_by="admin@x")


def _labeler():
    store = asc_store.get_store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET tier = 'labeler' WHERE id = ?", (u["id"],))
    return store.get_user_by_id(u["id"])


def _spec(admin_h, title="Q3 product direction"):
    r = client.post("/api/asclepius/admin/product-specs",
                    json={"title": title, "body_md": "# Direction\n\nShip the thing."},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["spec"]


def _signoff(advisor, artifact_type, artifact_id, verdict, comments=None, **extra):
    body = {"artifact_type": artifact_type, "artifact_id": artifact_id,
            "verdict": verdict, "comments": comments}
    body.update(extra)
    return client.post("/api/asclepius/advisor/signoffs", json=body,
                       headers=A.headers_for(advisor))


# ═══ Validation ══════════════════════════════════════════════════════════════
def test_changes_requested_with_empty_comments_is_a_400():
    """Same rule as PRD A's 'reject requires a reason', for the same reason: an
    unexplained rejection is unusable to everyone downstream."""
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    spec = _spec(admin_h)
    for empty in (None, "", "   ", "\n\t "):
        r = _signoff(advisor, "product_spec", spec["spec_id"],
                     "changes_requested", empty)
        assert r.status_code == 400, f"empty comments {empty!r} was accepted"
        assert "comments" in r.text.lower()

    r = _signoff(advisor, "product_spec", spec["spec_id"], "changes_requested",
                 "The dosing table needs a renal adjustment column.")
    assert r.status_code == 200, r.text


def test_approved_with_comments_needs_the_comments():
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    spec = _spec(admin_h)
    assert _signoff(advisor, "product_spec", spec["spec_id"],
                    "approved_with_comments").status_code == 400
    # A plain approval needs nothing.
    assert _signoff(advisor, "product_spec", spec["spec_id"],
                    "approved").status_code == 200


def test_unknown_verdicts_and_artifact_types_are_refused():
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    spec = _spec(admin_h)
    assert _signoff(advisor, "product_spec", spec["spec_id"],
                    "looks_fine_to_me").status_code == 400
    assert _signoff(advisor, "cap_table", "x", "approved").status_code == 400
    assert _signoff(advisor, "product_spec", "spec-does-not-exist",
                    "approved").status_code == 404


# ═══ The relationship is server-written ══════════════════════════════════════
def test_every_advisor_signoff_records_the_equity_relationship():
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    spec = _spec(admin_h)
    r = _signoff(advisor, "product_spec", spec["spec_id"], "approved")
    assert r.status_code == 200
    assert r.json()["signoff"]["relationship"] == "advisor_equity"


def test_a_client_supplied_relationship_is_ignored():
    """The whole value of the disclosure is that the person disclosed cannot
    write it."""
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    spec = _spec(admin_h)
    r = _signoff(advisor, "product_spec", spec["spec_id"], "approved",
                 relationship="independent_third_party")
    assert r.status_code == 200
    assert r.json()["signoff"]["relationship"] == "advisor_equity"

    stored = asc_store.get_store().list_advisory_signoffs(
        artifact_type="product_spec", artifact_id=spec["spec_id"])
    assert [s["relationship"] for s in stored] == ["advisor_equity"]


def test_an_admin_signoff_is_recorded_as_internal_not_as_equity():
    """An admin can operate every surface, but recording them identically to an
    equity-holding advisor would make the disclosure meaningless."""
    admin = _admin()
    admin_h = A.headers_for(admin)
    spec = _spec(admin_h)
    r = client.post("/api/asclepius/advisor/signoffs",
                    json={"artifact_type": "product_spec", "artifact_id": spec["spec_id"],
                          "verdict": "approved"}, headers=admin_h)
    assert r.status_code == 200
    assert r.json()["signoff"]["relationship"] == "internal_admin"


def test_two_advisors_can_sign_off_on_the_same_artifact_and_both_rows_persist():
    admin_h = A.headers_for(_admin())
    a, b = _advisor(), _advisor()
    spec = _spec(admin_h)
    assert _signoff(a, "product_spec", spec["spec_id"], "approved").status_code == 200
    assert _signoff(b, "product_spec", spec["spec_id"], "changes_requested",
                    "Second opinion: the rubric is too lenient.").status_code == 200
    rows = asc_store.get_store().list_advisory_signoffs(
        artifact_type="product_spec", artifact_id=spec["spec_id"])
    assert len(rows) == 2
    assert {r["advisor_id"] for r in rows} == {a["id"], b["id"]}


def test_a_labeler_cannot_sign_off_on_anything():
    admin_h = A.headers_for(_admin())
    spec = _spec(admin_h)
    labeler = _labeler()
    assert _signoff(labeler, "product_spec", spec["spec_id"],
                    "approved").status_code == 403
    assert client.get("/api/asclepius/advisor/queue",
                      headers=A.headers_for(labeler)).status_code == 403
    assert client.get(f"/api/asclepius/advisor/artifacts/product_spec/{spec['spec_id']}",
                      headers=A.headers_for(labeler)).status_code == 403


# ═══ The security boundary (§4.3) ════════════════════════════════════════════
def test_a_quarantined_case_body_never_reaches_an_advisor():
    """THE regression test for the PHI leak (audit C1).

    De-identification happens at ``ingestion.py`` ``cf.deidentify(normalized)``,
    which is on the SUCCESS path only. The quarantine path stores
    ``quarantine_body`` — the merged hospital fragment — and a case quarantines
    *precisely because* the residual-identifier scanner flagged it or the
    timeline normalizer found raw dates. So the bodies most likely to carry PHI
    are exactly the ones stored un-de-identified.

    ``public_case()`` does not help: it strips ``ground_truth``, ``hard_hook``
    and ``reasoning_divergence``. It is an answer-key stripper, not a
    de-identifier.

    The previous test at this boundary guarded the FILE path
    (``/uploads/{id}/download``) and built an upload with zero cases, so it
    passed vacuously while the leak came through the database. Sentinel strings
    below, asserted absent from the serialized payload — the
    ``_assert_no_identity`` pattern the codebase already uses for review
    blinding, applied to ``inbound_upload``.
    """
    store = asc_store.get_store()
    advisor = _advisor()
    upload = store.insert_ingest_upload(
        link_id="lnk-q", partner_id="hospital-a", filename="bundle.zip",
        sha256=None, size_bytes=None, raw_path="/tmp/raw.zip", source_ip=None)
    uid = upload["upload_id"]

    # Shaped exactly as the quarantine path writes it: a RAW merged body.
    phi = {
        "patient_name": "JOHN Q. SMITH",
        "mrn": "88213347",
        "dob": "1961-03-14",
        "phone": "650-555-0134",
        "ssn": "512-88-4471",
        "treating_clinician": "Alan Greenberg",
        "facility": "Stanford Hospital",
        "narrative": "Seen 03/14/2024 at Stanford Hospital by Dr Alan Greenberg.",
        "ground_truth": {"answer": "SEALED-ANSWER"},
    }
    store.insert_ingest_case(
        upload_id=uid, patient_key="pk-opaque", specialty="nephrology",
        case=phi, status="quarantined",
        report={"quarantine_reason": "de-id verification flagged 5 finding(s) "
                                     "near 03/14/2024 for JOHN Q. SMITH",
                "verification": {"status": "flagged", "verifier": "regex_v2",
                                 "findings": [{"kind": "name"}]}})
    # A clean case in the same upload, so the endpoint still has something to serve.
    store.insert_ingest_case(
        upload_id=uid, patient_key="pk-clean", specialty="nephrology",
        case={"presentation": "CKD stage 3, creatinine trending up"},
        status="ingested", report={"verification": {"status": "pass"}})

    r = client.get(f"/api/asclepius/advisor/artifacts/inbound_upload/{uid}",
                   headers=A.headers_for(advisor))
    assert r.status_code == 200, r.text
    raw = json.dumps(r.json())

    for sentinel in ("JOHN Q. SMITH", "88213347", "1961-03-14", "650-555-0134",
                     "512-88-4471", "Alan Greenberg", "Stanford Hospital",
                     "03/14/2024", "SEALED-ANSWER"):
        assert sentinel not in raw, (
            f"a quarantined case leaked {sentinel!r} to an advisor — an advisor is "
            f"an outside contractor with equity, and this is raw "
            f"pre-de-identification PHI")

    # The advisor must still be able to see THAT a case failed and roughly why —
    # withholding the body is not the same as hiding the finding.
    body = r.json()
    assert body["n_cases"] >= 1
    statuses = {c.get("status") for c in body["cases"]}
    assert "ingested" in statuses
    assert "CKD stage 3" in raw, "the de-identified case body should still be served"


def test_a_body_with_identifiers_is_withheld_even_when_its_status_says_clean():
    """Defence in depth behind the status whitelist.

    The whitelist is only as true as an invariant held by convention across four
    call sites in two files — "nothing sets 'ingested' without also writing a
    de-identified body". That holds today. C1 was precisely a false assumption
    about de-identification, so the bytes about to be served are re-scanned with
    the same verifier the ingest pipeline uses, and a flagged body is withheld
    no matter what its status claims.
    """
    store = asc_store.get_store()
    advisor = _advisor()
    upload = store.insert_ingest_upload(
        link_id="lnk-d", partner_id="hospital-a", filename="b.zip",
        sha256=None, size_bytes=None, raw_path="/tmp/r.zip", source_ip=None)
    uid = upload["upload_id"]
    ic = store.insert_ingest_case(
        upload_id=uid, patient_key="pk", specialty="nephrology",
        case={"patient_name": "ZZTOP SENTINEL", "mrn": "99887766",
              "dob": "1961-03-14"},
        status="quarantined", report={})

    def _case_row():
        r = client.get(f"/api/asclepius/advisor/artifacts/inbound_upload/{uid}",
                       headers=A.headers_for(advisor))
        assert r.status_code == 200
        return json.dumps(r.json()), r.json()["cases"][0]

    # Every status, including the ones the whitelist trusts.
    for status in ("quarantined", "rejected", "needs_review", "promoted", "ingested"):
        store.update_ingest_case(ic["ingest_case_id"], status=status)
        raw, row = _case_row()
        assert "ZZTOP SENTINEL" not in raw, f"PHI leaked at status={status}"
        assert row["body_withheld"] is True

    # And a genuinely de-identified body is still SHOWN — withholding
    # everything would be safe and useless.
    store.update_ingest_case(ic["ingest_case_id"], status="ingested",
                             case_json={"presentation": "CKD stage 3, creatinine rising"})
    raw, row = _case_row()
    assert row["body_withheld"] is False
    assert "CKD stage 3" in raw


def test_the_hospital_supplied_filename_never_reaches_an_advisor():
    """A bundle's filename is chosen by the sending institution and is
    uncontrolled free text — "SMITH_JOHN_2024.zip" and "MRN88213347.zip" are
    both realistic. Same leak class as the quarantined body, through a smaller
    door, and unsanitizable because it may simply BE a patient name."""
    store = asc_store.get_store()
    advisor = _advisor()
    upload = store.insert_ingest_upload(
        link_id="lnk-fn", partner_id="hospital-a",
        filename="SMITH_JOHN_MRN88213347_2024.zip",
        sha256=None, size_bytes=None, raw_path="/tmp/r.zip", source_ip=None)
    uid = upload["upload_id"]
    store.insert_ingest_case(
        upload_id=uid, patient_key="pk", specialty="nephrology",
        case={"presentation": "CKD"}, status="ingested", report={})

    for path in (f"/api/asclepius/advisor/artifacts/inbound_upload/{uid}",
                 "/api/asclepius/advisor/queue"):
        raw = json.dumps(client.get(path, headers=A.headers_for(advisor)).json())
        assert "SMITH_JOHN" not in raw, f"{path} leaked the uploaded filename"
        assert "88213347" not in raw


def test_the_quarantine_reason_is_scrubbed_before_an_advisor_sees_it():
    """``report['quarantine_reason']`` is ``str(exc)`` and the exception text
    quotes the tokens that caused the failure — which for a de-id flag are the
    identifiers themselves."""
    store = asc_store.get_store()
    advisor = _advisor()
    upload = store.insert_ingest_upload(
        link_id="lnk-q2", partner_id="hospital-a", filename="b.zip",
        sha256=None, size_bytes=None, raw_path="/tmp/r.zip", source_ip=None)
    uid = upload["upload_id"]
    store.insert_ingest_case(
        upload_id=uid, patient_key="pk", specialty="nephrology",
        case={"patient_name": "MARIA GARCIA"}, status="quarantined",
        report={"quarantine_reason": "unresolved date-like tokens: 03/14/2024, "
                                     "1961-03-14 for MARIA GARCIA"})
    r = client.get(f"/api/asclepius/advisor/artifacts/inbound_upload/{uid}",
                   headers=A.headers_for(advisor))
    assert r.status_code == 200
    raw = json.dumps(r.json())
    for sentinel in ("MARIA GARCIA", "03/14/2024", "1961-03-14"):
        assert sentinel not in raw, f"quarantine_reason leaked {sentinel!r}"


def test_an_advisor_gets_403_on_the_raw_hospital_upload():
    """The single easiest thing in this build to hand over by accident: the raw
    pre-de-identification bundle sits next to the de-identified view in the same
    admin UI. It is PHI, it is admin-only, and it stays admin-only."""
    store = asc_store.get_store()
    advisor = _advisor()
    upload = store.insert_ingest_upload(
        link_id="lnk-test", partner_id="hospital-a", filename="bundle.zip",
        sha256=None, size_bytes=None, raw_path="/tmp/nope.zip", source_ip=None)
    uid = upload["upload_id"]

    r = client.get(f"/api/asclepius/ingestion/uploads/{uid}/download",
                   headers=A.headers_for(advisor))
    assert r.status_code == 403, (
        "an advisor reached the RAW hospital upload — that is PHI and admin-only")

    # The de-identified view is theirs, and it is a different endpoint.
    r = client.get(f"/api/asclepius/advisor/artifacts/inbound_upload/{uid}",
                   headers=A.headers_for(advisor))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact_type"] == "inbound_upload"
    # Nothing in the de-identified view may point at the raw blob.
    raw = json.dumps(body).lower()
    assert "raw_path" not in raw
    assert "/tmp/nope.zip" not in raw


def test_the_task_batch_preview_never_carries_the_answer_key():
    """An advisor who previews a batch may later be routed to label one of these
    cases. A previewed answer key contaminates their own submission and every κ
    that submission touches."""
    store = asc_store.get_store()
    advisor = _advisor()
    from asclepius.cases import _INTERNAL_CASE_KEYS

    # Seed EVERY key the codebase calls internal, each with its own sentinel, so
    # this test tracks that definition instead of a list somebody typed here.
    # Add a fourth internal key upstream and this assertion covers it for free.
    sealed = {k: f"SEALED-{k.upper()}" for k in _INTERNAL_CASE_KEYS}
    assert sealed, "_INTERNAL_CASE_KEYS is empty — the answer key has no definition"
    store.insert_task(
        prompt="A 62-year-old with rising creatinine…",
        specialty="nephrology",
        candidate_answers=[{"id": "a", "text": "Option A"},
                           {"id": "b", "text": "Option B"}],
        case={"presentation": "CKD stage 3", **sealed})

    queue = client.get("/api/asclepius/advisor/queue",
                       headers=A.headers_for(advisor)).json()
    batches = queue["queue"]["task_batch"]
    assert batches, "the open batch never reached the advisor's queue"
    key = batches[0]["batch_key"]

    r = client.get(f"/api/asclepius/advisor/artifacts/task_batch/{key}",
                   headers=A.headers_for(advisor))
    assert r.status_code == 200, r.text
    raw = json.dumps(r.json())
    for key, sentinel in sealed.items():
        assert sentinel not in raw, f"the advisor preview leaked the sealed {key!r}"
        assert key not in raw, f"the advisor preview carried the {key!r} field"
    # The parts they DO need are there.
    assert "rising creatinine" in raw
    assert "Option A" in raw
    assert "CKD stage 3" in raw


def test_an_advisor_reviewing_a_submission_still_sees_no_labeler_identity():
    """PRD A's blinding rule applies to an advisor exactly as to any reviewer.
    More senior is not exempt."""
    from asclepius import review as asc_review

    store = asc_store.get_store()
    labeler = A.make_user(store, role="evaluator", specialty="nephrology",
                          email=f"dr-mcallister-{uuid.uuid4().hex[:6]}@example.com")
    task = store.insert_task(prompt="p", specialty="nephrology",
                             candidate_answers=[{"id": "a", "text": "A"},
                                                {"id": "b", "text": "B"}])
    sub = store.insert_submission(
        submission_id=f"sub-{uuid.uuid4().hex[:10]}", task_id=task["task_id"],
        evaluator_id=labeler["id"], verdict="a_better", chosen_id="a",
        rejected_id="b", confidence="high", time_spent_sec=60,
        payload={"from_scratch": "My reading of this case."},
        annotator=store.annotator_block(labeler), dedupe_hash=None)

    view = asc_review.blinded_review_view(task, sub)
    assert asc_review.payload_is_blinded(
        view, reviewer_role="evaluator", labeler=labeler) is True
    raw = json.dumps(view).lower()
    assert labeler["email"].lower() not in raw
    assert labeler["id"] not in raw
    assert "evaluator_id" not in raw


# ═══ Nothing blocks ══════════════════════════════════════════════════════════
def test_an_outstanding_changes_requested_never_blocks_an_export():
    """§4.4 as amended by an explicit founder decision: sign-off is recorded and
    surfaced, never blocking. One advisor with a day job must not sit on the
    revenue path.

    This asserts the BEHAVIOUR — an export with an open ``changes_requested``
    against it still builds and still downloads. It deliberately does NOT assert
    that some flag name is absent from the source, which is what this test used
    to do (audit M8): forbidding an identifier stops a future, deliberate
    opt-in gate from ever being written, and a guard should pin the decision
    that was made, not outlaw the one that wasn't.
    """
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    advisor = _advisor()

    export_id = f"exp-{uuid.uuid4().hex[:8]}"
    store.insert_export(export_id=export_id, created_by="admin@x", record_count=3,
                        filters={}, dir_path="", manifest={"profile": "default"})
    r = _signoff(advisor, "export_bundle", export_id, "changes_requested",
                 "The data dictionary omits renal dosing units.")
    assert r.status_code == 200
    assert r.json()["blocking"] is False
    assert store.get_export(export_id)["signoff_status"] == "changes_requested"

    # The objection is recorded and visible — and the export is untouched by it.
    assert store.get_export(export_id) is not None
    listed = client.get("/api/asclepius/exports", headers=admin_h)
    assert listed.status_code == 200
    assert export_id in listed.text

    # And building a NEW export is not gated by any outstanding verdict either.
    r = client.post("/api/asclepius/exports", json={"profile": "default"},
                    headers=admin_h)
    assert "signoff" not in r.text.lower()
    assert "sign-off" not in r.text.lower()


def test_the_signoff_response_says_plainly_that_it_does_not_block():
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    spec = _spec(admin_h)
    r = _signoff(advisor, "product_spec", spec["spec_id"], "changes_requested",
                 "Needs a renal dosing column.")
    assert r.status_code == 200
    assert r.json()["blocking"] is False


def test_a_verdict_is_mirrored_onto_the_artifact_for_the_admin_list():
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    upload = store.insert_ingest_upload(
        link_id="lnk-test", partner_id="hospital-a", filename="b.zip",
        sha256=None, size_bytes=None, raw_path="/tmp/b.zip", source_ip=None)
    uid = upload["upload_id"]
    assert store.get_ingest_upload(uid).get("signoff_status") is None

    _signoff(advisor, "inbound_upload", uid, "changes_requested",
             "Two cases still carry a date of service.")
    assert store.get_ingest_upload(uid)["signoff_status"] == "changes_requested"

    # Admin sees every verdict WITH its relationship — the disclosure is only
    # useful where the decision is read.
    body = client.get("/api/asclepius/admin/signoffs", headers=admin_h).json()
    assert body["count"] == 1
    assert body["signoffs"][0]["relationship"] == "advisor_equity"
    assert body["signoffs"][0]["advisor_name"]


def test_an_approval_cannot_erase_an_outstanding_changes_requested():
    """Audit M2: the mirrored status was last-write-wins.

    With two advisors, one approving after another requested changes flipped the
    field an operator reads before shipping from 'changes_requested' to
    'approved'. Both rows always survived in ``advisory_signoffs``, so the
    evidence was never lost — but the summary said the opposite of the evidence,
    which is worse than saying nothing. ``created_at`` is second-granularity, so
    "most recent" is not even well-defined for same-second writes.
    """
    store = asc_store.get_store()
    a, b = _advisor(), _advisor()
    upload = store.insert_ingest_upload(
        link_id="lnk-m2", partner_id="hospital-a", filename="b.zip",
        sha256=None, size_bytes=None, raw_path="/tmp/b.zip", source_ip=None)
    uid = upload["upload_id"]

    assert _signoff(a, "inbound_upload", uid, "changes_requested",
                    "Two cases still carry a date of service.").status_code == 200
    assert store.get_ingest_upload(uid)["signoff_status"] == "changes_requested"

    # A second advisor approves. The objection must NOT disappear.
    assert _signoff(b, "inbound_upload", uid, "approved").status_code == 200
    assert store.get_ingest_upload(uid)["signoff_status"] == "changes_requested", (
        "an approval erased an outstanding changes_requested from the field an "
        "operator reads before shipping")

    summary = store.signoff_summary("inbound_upload", [uid])[uid]
    assert summary["n"] == 2
    assert summary["verdict"] == "changes_requested"
    assert sorted(summary["verdicts"]) == ["approved", "changes_requested"]


def test_a_task_batch_signoff_records_what_was_actually_signed():
    """Audit M3: a task_batch id is DERIVED (``specialty:YYYY-MM-DD`` over open
    tasks), so its membership changes after the attestation — tasks generated
    later the same day joined the batch and inherited an approval nobody gave
    them. An attestation whose subject cannot be reconstructed is not one."""
    store = asc_store.get_store()
    advisor = _advisor()
    for _ in range(3):
        store.insert_task(prompt="case", specialty="nephrology",
                          candidate_answers=[{"id": "A", "text": "a"},
                                             {"id": "B", "text": "b"}])
    queue = client.get("/api/asclepius/advisor/queue",
                       headers=A.headers_for(advisor)).json()
    batch = queue["queue"]["task_batch"][0]
    key = batch["batch_key"]
    assert batch["n_tasks"] == 3

    r = _signoff(advisor, "task_batch", key, "approved")
    assert r.status_code == 200, r.text
    signoff = r.json()["signoff"]
    assert signoff["subject_n"] == 3
    assert len(signoff["subject_ids"]) == 3
    signed_ids = set(signoff["subject_ids"])

    # A task generated afterwards joins the derived key but must NOT be inside
    # the attestation that already happened.
    later = store.insert_task(prompt="later case", specialty="nephrology",
                              candidate_answers=[{"id": "A", "text": "a"},
                                                 {"id": "B", "text": "b"}])
    stored = store.list_advisory_signoffs(artifact_type="task_batch", artifact_id=key)[0]
    assert set(stored["subject_ids"]) == signed_ids
    assert later["task_id"] not in stored["subject_ids"], (
        "a task created after the sign-off inherited the approval")


def test_the_batch_count_and_the_batch_view_agree():
    """Audit M3: the queue counted with one cap and the artifact view showed
    another, so the same batch was two different sizes depending on the screen —
    a way to make a reviewer confident about something they did not see."""
    store = asc_store.get_store()
    advisor = _advisor()
    for _ in range(7):
        store.insert_task(prompt="case", specialty="cardiology",
                          candidate_answers=[{"id": "A", "text": "a"},
                                             {"id": "B", "text": "b"}])
    queue = client.get("/api/asclepius/advisor/queue",
                       headers=A.headers_for(advisor)).json()
    batch = next(b for b in queue["queue"]["task_batch"]
                 if b["specialty"] == "cardiology")
    view = client.get(
        f"/api/asclepius/advisor/artifacts/task_batch/{batch['batch_key']}",
        headers=A.headers_for(advisor)).json()
    assert batch["n_tasks"] == view["n_tasks"] == 7
    assert view["truncated"] is False


def test_the_same_advisor_resubmitting_an_identical_verdict_is_idempotent():
    """Audit L2: a double-click produced five identical rows, making the history
    unreadable and every count wrong. A CHANGED verdict still records — people
    revise their opinion and the trail should show it."""
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    spec = _spec(admin_h)
    for _ in range(4):
        r = _signoff(advisor, "product_spec", spec["spec_id"], "approved")
        assert r.status_code == 200
    rows = asc_store.get_store().list_advisory_signoffs(
        artifact_type="product_spec", artifact_id=spec["spec_id"])
    assert len(rows) == 1

    r = _signoff(advisor, "product_spec", spec["spec_id"], "changes_requested",
                 "On reflection the rubric is too lenient.")
    assert r.status_code == 200
    assert r.json().get("duplicate") is not True
    rows = asc_store.get_store().list_advisory_signoffs(
        artifact_type="product_spec", artifact_id=spec["spec_id"])
    assert len(rows) == 2, "a genuine change of opinion was swallowed"


def test_worst_verdict_treats_an_unknown_value_as_worst():
    """An unrecognized verdict is not evidence of approval."""
    store = asc_store.get_store()
    assert store.worst_verdict([]) is None
    assert store.worst_verdict(["approved"]) == "approved"
    assert store.worst_verdict(["approved", "approved_with_comments"]) == \
        "approved_with_comments"
    assert store.worst_verdict(["changes_requested", "approved"]) == "changes_requested"
    assert store.worst_verdict(["approved", "something_new"]) == "something_new"


def test_an_advisor_sees_only_their_own_signoff_history():
    admin_h = A.headers_for(_admin())
    a, b = _advisor(), _advisor()
    spec = _spec(admin_h)
    _signoff(a, "product_spec", spec["spec_id"], "approved")
    _signoff(b, "product_spec", spec["spec_id"], "approved")
    body = client.get("/api/asclepius/advisor/signoffs",
                      headers=A.headers_for(a)).json()
    assert len(body["signoffs"]) == 1
    assert body["signoffs"][0]["advisor_id"] == a["id"]


def test_the_queue_omits_artifact_types_the_caller_cannot_open():
    """An absent key means 'you do not hold that capability', which is a
    different fact from an empty list."""
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    body = client.get("/api/asclepius/advisor/queue",
                      headers=A.headers_for(advisor)).json()
    assert set(body["queue"]) == {"task_batch", "export_bundle",
                                  "inbound_upload", "product_spec"}
    assert client.get("/api/asclepius/advisor/queue",
                      headers=A.headers_for(_labeler())).status_code == 403
