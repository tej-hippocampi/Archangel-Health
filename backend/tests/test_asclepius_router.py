"""End-to-end HTTP tests for the Asclepius router (opt §4.12).

Covers auth + role gates, the full submitted -> export_ready -> exported
lifecycle, grounding_mode=required submit-gating, buyer-request -> batch
provenance, the lightest-path field guard, export per-line schema validation
(invalid line fails the whole batch), export filters, and isolation from the
clinical RBAC. The LLM critic + grounding check are stubbed (offline).
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

_ANCHOR = {"citation_text": "KDIGO 2024 hyperkalemia", "source_type": "guideline", "identifier": "KDIGO-2024-3.2"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Fresh store per test + offline critic/grounding stubs (consistent)."""
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


def _seed(role="evaluator", specialty="nephrology", **kw):
    return A.make_user(_store(), role=role, specialty=specialty, board_cert="board_certified_nephrology", years_experience=12, **kw)


def _admin():
    return A.make_user(_store(), role="admin")


def _task_body(**kw):
    base = {
        "specialty": "nephrology", "difficulty": "hard", "capture_reasoning": False,
        "source": "lab_supplied", "max_labels": 1, "grounding_mode": "optional",
        "prompt": "72yo on HD, K+ 6.4 with peaked T-waves. Adjust dialysate and meds?",
        "candidate_answers": [
            {"id": "A", "text": "Give calcium gluconate, then dialyze with K+ 2.0.", "generator_model": "model_x"},
            {"id": "B", "text": "Set dialysate K+ to 1.0 immediately.", "generator_model": "model_y"},
        ],
    }
    base.update(kw)
    return base


def _upload_task(admin_h, **kw):
    r = client.post("/api/asclepius/tasks", json={"tasks": [_task_body(**kw)]}, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


# ─── Auth & role gates ────────────────────────────────────────────────────────
def test_login_and_me():
    user = _seed()
    r = client.post("/api/asclepius/auth/login", json={"email": user["email"], "password": "pw-12345678"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    me = client.get("/api/asclepius/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["role"] == "evaluator"


def test_login_bad_password():
    user = _seed()
    r = client.post("/api/asclepius/auth/login", json={"email": user["email"], "password": "wrong"})
    assert r.status_code == 401


def test_taxonomy_requires_auth_and_exposes_optimization_vocab():
    assert client.get("/api/asclepius/taxonomy").status_code == 401
    r = client.get("/api/asclepius/taxonomy", headers=A.headers_for(_seed()))
    assert r.status_code == 200
    body = r.json()
    assert "required" in body["grounding_modes"]
    assert "guideline" in body["evidence_source_types"]
    assert body["reasoning_step_labels"] == ["good", "neutral", "bad"]
    assert "flat" in body["preference_variants"]


def test_evaluator_cannot_reach_admin_endpoints():
    ev = A.headers_for(_seed())
    assert client.get("/api/asclepius/stats", headers=ev).status_code == 403
    assert client.post("/api/asclepius/tasks", json={"tasks": []}, headers=ev).status_code == 403
    assert client.get("/api/asclepius/qa/queue", headers=ev).status_code == 403


def test_clinical_jwt_is_rejected_by_asclepius_auth():
    """Isolation: a clinical tenant JWT is NOT accepted by the standalone
    Asclepius auth plane (team.db / clinical RBAC untouched)."""
    from tests._role_auth import tenant_token
    clinical = {"Authorization": f"Bearer {tenant_token('surgeon', is_team_director=True)}"}
    assert client.get("/api/asclepius/auth/me", headers=clinical).status_code == 401
    # And the asclepius DB is a separate file, never team.db.
    assert "team.db" not in _store().db_path


def test_sso_exchanges_doctor_session_for_asclepius_session():
    """A clinician already signed into the doctor portal can trade that
    tenant_staff token for an Asclepius session — no credential typing."""
    from tests._role_auth import tenant_token
    user = _seed(email="sso-clinician@hospital.org")
    token = tenant_token("surgeon", email="sso-clinician@hospital.org")
    r = client.post("/api/asclepius/auth/sso", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == user["email"]
    # The minted token is a real Asclepius session usable on protected routes.
    me = client.get("/api/asclepius/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == user["email"]


def test_sso_auto_provisions_an_evaluator_on_first_arrival():
    """A valid doctor token with no Asclepius account yet is auto-provisioned an
    evaluator seat and signed straight in (no second login barrier)."""
    from tests._role_auth import tenant_token
    assert _store().get_user_by_email("newdoc@hospital.org") is None
    token = tenant_token("surgeon", email="newdoc@hospital.org")
    r = client.post("/api/asclepius/auth/sso", json={"token": token})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "newdoc@hospital.org"
    assert r.json()["user"]["role"] == "evaluator"
    # The account now persists, so a second SSO resumes the same seat.
    user = _store().get_user_by_email("newdoc@hospital.org")
    assert user is not None
    r2 = client.post("/api/asclepius/auth/sso", json={"token": token})
    assert r2.status_code == 200
    assert _store().get_user_by_email("newdoc@hospital.org")["id"] == user["id"]


def test_sso_still_refuses_an_anonymous_visitor():
    """No valid doctor session -> no exchange (the portal is never left open)."""
    r = client.post("/api/asclepius/auth/sso", json={"token": "not-a-real-jwt"})
    assert r.status_code == 401


def test_empty_queue_auto_generates_from_corpus(monkeypatch):
    """An evaluator opening an empty queue triggers on-demand seeding from the
    ratified corpus — only the A/B answers are LLM-generated — so a real task
    (vetted prompt + candidates) appears automatically with no admin upload."""
    from routers import asclepius as R

    calls = {"n": 0}

    async def _fake_candidates(prompt, *, specialty="general", ai_failure_mode=None):
        calls["n"] += 1
        return {
            "candidates": [
                {"id": "A", "text": "Strong answer", "generator_model": "m"},
                {"id": "B", "text": "Flawed answer", "generator_model": "m"},
            ],
            "model": "claude-sonnet-4-6",
            "intended_flawed_id": "B",
        }

    monkeypatch.setattr(R, "generate_candidates_ex", _fake_candidates)
    R._autofill_last_attempt.clear()  # bypass cross-test cooldown

    ev_h = A.headers_for(_seed())  # nephrology evaluator
    r = client.get("/api/asclepius/tasks/next", headers=ev_h)
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task is not None
    assert (task.get("prompt") or "").strip()          # a real corpus prompt
    assert len(task.get("candidate_answers") or []) == 2
    assert calls["n"] >= 1


def test_empty_queue_no_llm_returns_empty_not_error(monkeypatch):
    """If candidate generation yields nothing (no LLM), an empty queue returns
    null gracefully — never a 500."""
    from routers import asclepius as R

    async def _no_candidates(prompt, *, specialty="general", ai_failure_mode=None):
        return {"candidates": [], "model": None, "intended_flawed_id": None}

    monkeypatch.setattr(R, "generate_candidates_ex", _no_candidates)
    R._autofill_last_attempt.clear()

    r = client.get("/api/asclepius/tasks/next", headers=A.headers_for(_seed()))
    assert r.status_code == 200
    assert r.json()["task"] is None


# ─── Full lifecycle ───────────────────────────────────────────────────────────
def test_full_lifecycle_submitted_to_exported():
    admin_h = A.headers_for(_admin())
    ev_user = _seed()
    ev_h = A.headers_for(ev_user)

    tid = _upload_task(admin_h)

    nxt = client.get("/api/asclepius/tasks/next", headers=ev_h).json()["task"]
    assert nxt["task_id"] == tid
    # Blinded: never leaks generator_model.
    assert all("generator_model" not in c for c in nxt["candidate_answers"])

    sid = "s-" + uuid.uuid4().hex[:12]
    body = {
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "confidence": "high", "time_spent_sec": 140,
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+", "why_better_tags": ["safer"]},
        "rejected_critique": {"error_tags": ["dosing_error"], "severities": {}, "why_worse": "too aggressive"},
    }
    r = client.post("/api/asclepius/submissions", json=body, headers=ev_h)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["status"] == "export_ready"
    assert result["record_count"] >= 1

    # Idempotent re-submit returns the same submission, no double-capture.
    r2 = client.post("/api/asclepius/submissions", json=body, headers=ev_h)
    assert r2.status_code == 200
    assert r2.json()["submission_id"] == sid

    # Export.
    exp = client.post("/api/asclepius/exports", json={"profile": "default"}, headers=admin_h)
    assert exp.status_code == 200, exp.text
    manifest = exp.json()
    assert manifest["record_count"] >= 1
    assert manifest["profile"] == "default"
    assert manifest["content_hashes"]["records.jsonl"]
    assert "kappa" in manifest
    # The submission is now exported.
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["status"] == "exported"

    # Download returns a zip.
    dl = client.get(f"/api/asclepius/exports/{manifest['export_id']}/download", headers=admin_h)
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"


def test_stats_reports_exportable_record_backlog():
    """The Exports tab's 'ready to export' count tracks export_ready records and
    drops to 0 once they're packaged."""
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)

    assert client.get("/api/asclepius/stats", headers=admin_h).json()["exportable_records"] == 0

    sid = "s-" + uuid.uuid4().hex[:12]
    client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 90,
    }, headers=ev_h)
    assert client.get("/api/asclepius/stats", headers=admin_h).json()["exportable_records"] >= 1

    # One-click default export (no filters) packages the backlog and downloads.
    exp = client.post("/api/asclepius/exports", json={"profile": "default"}, headers=admin_h)
    assert exp.status_code == 200, exp.text
    assert exp.json()["record_count"] >= 1
    after = client.get("/api/asclepius/stats", headers=admin_h).json()
    assert after["exportable_records"] == 0          # nothing fresh left
    assert after["exported_records"] >= 1            # but it's shipped, not gone
    assert after["total_records"] >= 1

    # A plain re-export now finds nothing fresh (400), but include_exported lets
    # the admin re-download the already-shipped bundle.
    assert client.post("/api/asclepius/exports", json={"profile": "default"},
                       headers=admin_h).status_code == 400
    re = client.post("/api/asclepius/exports",
                     json={"profile": "default", "include_exported": True}, headers=admin_h)
    assert re.status_code == 200, re.text
    assert re.json()["record_count"] >= 1


def test_qa_held_submission_can_be_bulk_approved_then_exported(monkeypatch):
    """A submission sampled/flagged into QA isn't exportable until approved. The
    one-click bulk approve clears the whole QA backlog to export_ready so the
    admin can export immediately."""
    monkeypatch.setenv("ASCLEPIUS_QA_SAMPLE_PCT", "100")  # force every submit into QA
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)

    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 90,
    }, headers=ev_h)
    assert r.json()["status"] == "needs_qa"
    st = client.get("/api/asclepius/stats", headers=admin_h).json()
    assert st["qa_pending"] >= 1 and st["exportable_records"] == 0

    appr = client.post("/api/asclepius/qa/approve-all", headers=admin_h)
    assert appr.status_code == 200 and appr.json()["approved"] >= 1
    st2 = client.get("/api/asclepius/stats", headers=admin_h).json()
    assert st2["qa_pending"] == 0 and st2["exportable_records"] >= 1

    exp = client.post("/api/asclepius/exports", json={"profile": "default"}, headers=admin_h)
    assert exp.status_code == 200 and exp.json()["record_count"] >= 1


def test_lightest_path_minimal_fields_reaches_export_ready():
    """Pick a side + submit (no rationale, no tags, no anchors) still works and
    auto-packages — the ≤3-min lightest path is sacred (opt §3)."""
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)

    sid = "s-" + uuid.uuid4().hex[:12]
    minimal = {"submission_id": sid, "task_id": tid, "verdict": "A_better",
               "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120}
    # The evaluator-entered content is just the verdict + which side -> tiny.
    user_entered = [k for k in ("verdict", "chosen_id", "rejected_id") if minimal.get(k)]
    assert len(user_entered) <= 3
    r = client.post("/api/asclepius/submissions", json=minimal, headers=ev_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready"


# ─── Grounding Mode = required (opt §1.2) ─────────────────────────────────────
def test_grounding_required_gates_submit():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h, grounding_mode="required")

    nxt = client.get("/api/asclepius/tasks/next", headers=ev_h).json()["task"]
    assert nxt["grounding_mode"] == "required"
    assert nxt["grounding_disclaimer"]  # earn-more disclaimer surfaced

    # No citation -> 400 grounding_required (non-silent).
    no_anchor = {"submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid, "verdict": "A_better",
                 "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
                 "chosen_revision": {"edited": False, "why_better_notes": "safer"}}
    r = client.post("/api/asclepius/submissions", json=no_anchor, headers=ev_h)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "grounding_required"

    # With a valid citation -> accepted + grounded.
    with_anchor = {"submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid, "verdict": "A_better",
                   "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
                   "chosen_revision": {"edited": False, "why_better_notes": "safer", "evidence_anchor": _ANCHOR}}
    r2 = client.post("/api/asclepius/submissions", json=with_anchor, headers=ev_h)
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "export_ready"
    # Grounded premium tier reflected in stats.
    stats = client.get("/api/asclepius/stats", headers=admin_h).json()
    assert stats["grounded"]["submissions_grounded"] >= 1


# ─── Buyer request -> batch provenance (opt §2.5) ─────────────────────────────
def test_buyer_request_batch_stamps_provenance():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())

    buyer = client.post("/api/asclepius/buyers", json={"name": "Hungry Lab", "export_profile": "default"}, headers=admin_h)
    assert buyer.status_code == 200, buyer.text
    bid = buyer.json()["buyer_id"]

    req = client.post("/api/asclepius/buyer-requests", json={
        "buyer_id": bid, "source": "lab_supplied", "export_profile": "default",
        "specialty": "nephrology", "grounding_mode": "optional",
        "prompts": [_task_body()],
    }, headers=admin_h)
    assert req.status_code == 200, req.text
    rid = req.json()["request_id"]

    batch = client.post(f"/api/asclepius/buyer-requests/{rid}/batch", json={"count": 0}, headers=admin_h)
    assert batch.status_code == 200, batch.text
    assert batch.json()["count"] == 1

    # Evaluator grades the buyer's task.
    nxt = client.get("/api/asclepius/tasks/next", headers=ev_h).json()["task"]
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": nxt["task_id"], "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text

    # Every record stamps source + buyer_request_id.
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["records"]
    for rec in sub["records"]:
        assert rec["payload"]["buyer_request_id"] == rid
        assert rec["payload"]["source"] == "lab_supplied"

    # Request moved to in_progress.
    assert client.get(f"/api/asclepius/buyer-requests/{rid}", headers=admin_h).json()["status"] == "in_progress"


# ─── Export schema validation + filters (opt §2) ──────────────────────────────
def _make_export_ready(admin_h, ev_h, *, grounded=False):
    tid = _upload_task(admin_h, prompt=f"Manage hyperkalemia case {A.uniq(8)}?")
    sid = "s-" + uuid.uuid4().hex[:12]
    body = {"submission_id": sid, "task_id": tid, "verdict": "A_better",
            "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
            "chosen_revision": {"edited": False, "why_better_notes": "safer",
                                **({"evidence_anchor": _ANCHOR} if grounded else {})},
            "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"}}
    r = client.post("/api/asclepius/submissions", json=body, headers=ev_h)
    assert r.status_code == 200, r.text
    return sid


def test_export_invalid_line_fails_whole_batch(monkeypatch):
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    _make_export_ready(admin_h, ev_h)

    # Force every mapped line to fail the profile schema.
    monkeypatch.setattr(
        asc_profiles, "schema_for",
        lambda profile, rtype: {"type": "object", "required": ["__never_present__"]},
    )
    r = client.post("/api/asclepius/exports", json={"profile": "default"}, headers=admin_h)
    assert r.status_code == 422
    # No partial export: nothing got marked exported.
    stats = client.get("/api/asclepius/stats", headers=admin_h).json()
    assert stats["status_counts"].get("exported", 0) == 0
    assert stats["status_counts"].get("export_ready", 0) >= 1


def test_export_grounded_only_filter():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    _make_export_ready(admin_h, ev_h, grounded=False)
    _make_export_ready(admin_h, ev_h, grounded=True)

    r = client.post("/api/asclepius/exports", json={"profile": "default", "grounded_only": True}, headers=admin_h)
    assert r.status_code == 200, r.text
    manifest = r.json()
    assert manifest["record_count"] >= 1
    assert manifest["grounded_count"] == manifest["record_count"]


def test_export_no_matching_records_is_400():
    admin_h = A.headers_for(_admin())
    r = client.post("/api/asclepius/exports", json={"profile": "default"}, headers=admin_h)
    assert r.status_code == 400
