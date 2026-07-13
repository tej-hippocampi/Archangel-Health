"""The V4 wall (Real EHR Ingestion PRD §9.5) — real cases are V4, only V4.

Three enforcement layers, each tested:
  1. QUEUE ROUTING — v4 serves only case_source='real_deid'; v1/v2/v3 exclude it;
     v4 additionally requires the contributor to be real_data_approved.
  2. DERIVATION — the stamped portal_version is derived server-side from the
     task's case_source; a mislabel claim is a 400, never a silent normalize.
  3. PACKAGING — a case_source/portal_version mismatch that somehow reaches the
     pipeline routes to needs_qa (no record ships mislabeled).
LLM stubbed throughout.
"""

from __future__ import annotations

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


def _real_case(**over):
    base = {
        "case_source": "real_deid", "specialty": "nephrology",
        "demographics": {"age_band": "70-79", "sex": "F"},
        "lab_panels": [{"panel": "BMP", "collected_offset_days": -3, "results": [
            {"analyte": "Creatinine", "value": 4.1, "unit": "mg/dL", "ref_low": 0.6, "ref_high": 1.2, "flag": "HH"}]}],
        "notes": [{"note_type": "Progress", "author_role": "nephrology", "text": "Oliguric on [day -3]; worsening."}],
    }
    base.update(over)
    return base


def _mk_real_task(specialty="nephrology"):
    return _store().insert_task(
        prompt="CLINICAL QUESTION:\nClassify this AKI.\n\nCLINICAL CASE\nLabs: Cr high",
        specialty=specialty, difficulty="hard", capture_reasoning=True,
        source="partner_ehr",
        candidate_answers=[{"id": "A", "text": "ATN."}, {"id": "B", "text": "Pre-renal."}],
        case=_real_case(),
    )


def _mk_synth_task(specialty="nephrology"):
    return _store().insert_task(
        prompt=f"Hyperkalemia case {A.uniq(8)}?",
        specialty=specialty, difficulty="hard",
        candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    )


def _ev(approved=False):
    st = _store()
    u = A.make_user(st, role="evaluator", specialty="nephrology",
                    board_cert="board_certified_nephrology", years_experience=12)
    if approved:
        st.set_real_data_approved(u["id"], True)
        u = st.get_user_by_id(u["id"])
    return u


# ─── 1. Queue routing ─────────────────────────────────────────────────────────
def test_case_source_column_derived_on_insert():
    real, synth = _mk_real_task(), _mk_synth_task()
    assert real["case_source"] == "real_deid" and real["modality"] == "multimodal"
    assert synth["case_source"] is None


def test_v4_serves_only_real_and_v123_never_real():
    _mk_real_task()
    _mk_synth_task()
    h_ok = A.headers_for(_ev(approved=True))
    # v4 → the real case
    t4 = client.get("/api/asclepius/tasks/next?portal_version=v4", headers=h_ok).json()["task"]
    assert t4 is not None and t4["case"]["case_source"] == "real_deid"
    # v1/v2/v3 → never the real case
    for pv in ("v1", "v2", "v3"):
        h = A.headers_for(_ev(approved=True))
        t = client.get(f"/api/asclepius/tasks/next?portal_version={pv}", headers=h).json()["task"]
        assert t is None or (t.get("case") or {}).get("case_source") != "real_deid", pv


def test_v4_requires_real_data_approval():
    _mk_real_task()
    h_no = A.headers_for(_ev(approved=False))
    t = client.get("/api/asclepius/tasks/next?portal_version=v4", headers=h_no).json()["task"]
    assert t is None  # unapproved → empty queue, never a real case


def test_v4_queue_never_autofills_synthetic(monkeypatch):
    """An empty V4 queue stays empty — real data cannot be fabricated."""
    from routers import asclepius as R
    R._autofill_last_attempt.clear()
    h = A.headers_for(_ev(approved=True))
    t = client.get("/api/asclepius/tasks/next?portal_version=v4", headers=h).json()["task"]
    assert t is None
    assert _store().list_tasks(limit=10) == []  # nothing was generated


def test_direct_task_fetch_gated_by_approval():
    """The wall must not depend on task IDs being unguessable: an unapproved
    evaluator is 403'd on DIRECT access to a v4 task (fetch, reveal, submit);
    an approved one passes; synthetic tasks are unaffected."""
    real = _mk_real_task()
    synth = _mk_synth_task()
    h_no = A.headers_for(_ev(approved=False))
    h_ok = A.headers_for(_ev(approved=True))
    # fetch
    assert client.get(f"/api/asclepius/tasks/{real['task_id']}", headers=h_no).status_code == 403
    assert client.get(f"/api/asclepius/tasks/{real['task_id']}", headers=h_ok).status_code == 200
    assert client.get(f"/api/asclepius/tasks/{synth['task_id']}", headers=h_no).status_code == 200
    # reveal
    r = client.post(f"/api/asclepius/tasks/{real['task_id']}/reveal",
                    json={"text": "ATN from the casts."}, headers=h_no)
    assert r.status_code == 403
    # submit
    r2 = _submit(h_no, real["task_id"], "v4")
    assert r2.status_code == 403
    # admin/QA can still see it (they triage + review)
    admin_h = A.headers_for(A.make_user(_store(), role="admin"))
    assert client.get(f"/api/asclepius/tasks/{real['task_id']}", headers=admin_h).status_code == 200


# ─── 2. Derivation (never trust the client) ───────────────────────────────────
def _submit(headers, tid, pv, **payload_over):
    body = {
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid,
        "verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
        "time_spent_sec": 130, "portal_version": pv,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "ATN from the muddy-brown casts and the creatinine trajectory; hold nephrotoxins and support."},
        "chosen_revision": {"edited": False, "why_better_notes": "fits the sediment"},
        "rejected_critique": {"error_tags": ["omission"], "why_worse": "ignores casts"},
    }
    body.update(payload_over)
    return client.post("/api/asclepius/submissions", json=body, headers=headers)


def test_synthetic_claim_on_real_task_is_400():
    real = _mk_real_task()
    h = A.headers_for(_ev(approved=True))
    r = _submit(h, real["task_id"], "v3")
    assert r.status_code == 400
    assert "V4" in r.json()["detail"] or "v4" in str(r.json()["detail"])


def test_v4_claim_on_synthetic_task_is_400():
    synth = _mk_synth_task()
    h = A.headers_for(_ev(approved=True))
    r = _submit(h, synth["task_id"], "v4")
    assert r.status_code == 400


def test_real_task_submission_derives_v4_and_packages():
    real = _mk_real_task()
    h = A.headers_for(_ev(approved=True))
    r = _submit(h, real["task_id"], "v4")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready", r.text
    sid = r.json()["submission_id"]
    sub = _store().get_submission(sid)
    assert (sub.get("payload") or {}).get("portal_version") == "v4"
    recs = _store().records_for_submission(sid)
    assert recs
    for rec in recs:
        assert (rec["payload"] or {}).get("portal_version") == "v4"
        assert ((rec["payload"] or {}).get("context") or {}).get("case_source") == "real_deid"


def test_reveal_derives_v4_on_real_task():
    real = _mk_real_task()
    h = A.headers_for(_ev(approved=True))
    r = client.post(f"/api/asclepius/tasks/{real['task_id']}/reveal",
                    json={"text": "ATN — casts + trajectory.", "portal_version": "v2"}, headers=h)
    assert r.status_code == 400  # synthetic claim on a real case rejected at reveal too


# ─── 3. Packaging assertion (belt and braces) ─────────────────────────────────
def test_packaging_mismatch_routes_to_needs_qa():
    """A mismatch that bypasses the router (direct pipeline write) must route to
    needs_qa — no record ships mislabeled."""
    from asclepius.validation import validate_submission
    real_task = {"case_source": "real_deid", "grounding_mode": "optional"}
    sub = {"verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
           "time_spent_sec": 130,
           "payload": {"portal_version": "v3", "verdict": "A_better",
                       "independent_answer": {"text": "x" * 60},
                       "rejected_critique": {"error_tags": [], "why_worse": "y"}}}
    vres = validate_submission(real_task, sub, [])
    assert "portal_version_case_source_mismatch" in vres["issues"]
    # And the consistent stamp passes the wall check.
    sub["payload"]["portal_version"] = "v4"
    vres2 = validate_submission(real_task, sub, [])
    assert "portal_version_case_source_mismatch" not in vres2["issues"]


# ─── V4 flow parity + surfaces ────────────────────────────────────────────────
def test_v4_capture_kind_matches_v3():
    from asclepius.constants import independent_capture_kind
    assert independent_capture_kind("v4", "stance") == "instinct"
    assert independent_capture_kind("v4", "full") == "full"
    assert independent_capture_kind("v3", "stance") == "instinct"


def test_public_user_exposes_approval_flag():
    from asclepius import auth as asc_auth
    u = _ev(approved=True)
    assert asc_auth.public_user(u)["real_data_approved"] is True
    assert asc_auth.public_user(_ev(approved=False))["real_data_approved"] is False


def test_admin_approval_endpoint_toggles():
    st = _store()
    admin_h = A.headers_for(A.make_user(st, role="admin"))
    u = _ev(approved=False)
    r = client.post(f"/api/asclepius/users/{u['id']}/real-data-approval",
                    json={"approved": True}, headers=admin_h)
    assert r.status_code == 200 and r.json()["real_data_approved"] is True
    r2 = client.post(f"/api/asclepius/users/{u['id']}/real-data-approval",
                     json={"approved": False}, headers=admin_h)
    assert r2.status_code == 200 and r2.json()["real_data_approved"] is False


def test_mock_contributor_is_v4_approved():
    from asclepius import auth as asc_auth
    u = asc_auth.ensure_mock_contributor(_store())
    assert u["real_data_approved"] == 1
