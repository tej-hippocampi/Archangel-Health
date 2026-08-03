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
def test_an_export_ships_with_no_signoff_and_there_is_no_gate_to_turn_on():
    """§4.4 as amended: sign-off is recorded and surfaced, never blocking. One
    advisor with a day job must not sit on the revenue path."""
    import asclepius.export as asc_export
    import routers.asclepius_advisor as advisor_router
    import asclepius.store as store_mod

    # There is no env flag, in any of the three places one could hide.
    for module in (asc_export, advisor_router, store_mod):
        src = Path(module.__file__).read_text(encoding="utf-8")
        assert "REQUIRE_ADVISOR_SIGNOFF" not in src, (
            f"{module.__name__} carries a blocking flag — sign-off never blocks")

    admin_h = A.headers_for(_admin())
    r = client.post("/api/asclepius/exports", json={"profile": "default"},
                    headers=admin_h)
    # A build with no eligible records is a legitimate outcome; a 4xx that
    # mentions sign-off is not.
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
