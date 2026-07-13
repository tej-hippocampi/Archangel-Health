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
# Eval Flow Upgrade §3: the new flow captures a blind independent answer before
# A/B is revealed. A non-flagged submission without one routes to QA, so the
# happy-path fixtures include one.
_IDEAL = {"text": "Stabilize the myocardium with IV calcium, shift potassium intracellularly with insulin and dextrose plus a beta-agonist, then remove it with dialysis given the ESRD."}


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


def test_onboarding_email_is_the_identity_shown_in_the_portal():
    """The email a clinician onboarded with is the one the eval portal shows at the
    top — on manual sign-in AND when an already-signed-in doctor SSOs in under that
    same email. SSO must RESUME the onboarded account (keeping its admin role +
    organization), never mint a divergent duplicate."""
    from tests._role_auth import tenant_token

    store = _store()
    onb_email = "Director.Onboarded@hospital.org"  # mixed case as a user might type it
    store.provision_user(
        email=onb_email, password="pw-12345678", role="admin",
        full_name="Dana Director", org_name="Northridge Nephrology",
        clinical_role="director", specialty="nephrology",
    )

    # 1) Manual sign-in shows the onboarding email (normalized), not anything else.
    login = client.post(
        "/api/asclepius/auth/login",
        json={"email": onb_email, "password": "pw-12345678"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["email"] == onb_email.lower()

    # 2) Auto-SSO from the doctor portal under the SAME email resumes the SAME
    #    onboarded account — same id, still admin, email unchanged. No duplicate.
    before = store.get_user_by_email(onb_email)
    token = tenant_token("surgeon", email=onb_email)
    sso = client.post("/api/asclepius/auth/sso", json={"token": token})
    assert sso.status_code == 200, sso.text
    assert sso.json()["user"]["email"] == onb_email.lower()
    assert sso.json()["user"]["role"] == "admin"  # not downgraded to evaluator
    after = store.get_user_by_email(onb_email)
    assert after["id"] == before["id"]
    # Exactly one account for this person (no SSO-minted divergent duplicate).
    assert sum(1 for u in store.list_users() if u["email"] == onb_email.lower()) == 1


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
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": _IDEAL,
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
        "independent_answer": _IDEAL,
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
    # Lightest path under the new flow: the blind independent answer + verdict +
    # side (no rationale, tags, or anchors).
    minimal = {"submission_id": sid, "task_id": tid, "verdict": "A_better",
               "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
               "independent_answer": _IDEAL}
    # The structured A/B content is just the verdict + which side -> tiny.
    user_entered = [k for k in ("verdict", "chosen_id", "rejected_id") if minimal.get(k)]
    assert len(user_entered) <= 3
    r = client.post("/api/asclepius/submissions", json=minimal, headers=ev_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready"


# ─── Prompt validation gate (Eval Flow Upgrade §2) ────────────────────────────
def test_flagged_prompt_produces_zero_records_and_flags_task():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)

    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid,
        "prompt_review": {"reviewed": True, "verdict": "flagged", "note": "ambiguous potassium value"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "prompt_flagged"
    assert body["record_count"] == 0

    # Idempotent: replaying the flag returns the same result, no double-capture.
    r2 = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid,
        "prompt_review": {"reviewed": True, "verdict": "flagged", "note": "ambiguous potassium value"},
    }, headers=ev_h)
    assert r2.status_code == 200 and r2.json()["status"] == "prompt_flagged"

    # Task is flagged (out of the queue) and surfaced to admin via the status filter.
    flagged = client.get("/api/asclepius/tasks?status=prompt_flagged", headers=admin_h).json()["tasks"]
    assert any(t["task_id"] == tid for t in flagged)

    # The flagged task is no longer served to evaluators.
    nxt = client.get("/api/asclepius/tasks/next", headers=A.headers_for(_seed())).json()["task"]
    assert nxt is None or nxt["task_id"] != tid


def test_flagged_prompt_note_phi_is_redacted():
    """The flag reason is doctor free-text and the flagged path skips
    validate_submission — so PHI in the note is scanned + redacted at capture."""
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid,
        "prompt_review": {"reviewed": True, "verdict": "flagged",
                          "note": "ambiguous; reach jdoe@example.com to clarify"},
    }, headers=ev_h)
    assert r.status_code == 200 and r.json()["status"] == "prompt_flagged"
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    note = sub["payload"]["prompt_review"]["note"]
    assert "jdoe@example.com" not in note
    assert "redacted" in note.lower()


def test_flag_pulls_back_already_graded_sibling():
    """max_labels=2: one evaluator grades to export_ready, another flags the
    prompt -> the graded sibling is pulled back to QA and the task stays flagged
    (a flagged prompt never silently ships)."""
    admin_h = A.headers_for(_admin())
    e1 = A.headers_for(_seed())
    e2 = A.headers_for(_seed())
    tid = _upload_task(admin_h, max_labels=2)

    s1 = "s-" + uuid.uuid4().hex[:12]
    r1 = client.post("/api/asclepius/submissions", json={
        "submission_id": s1, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
        "independent_answer": _IDEAL,
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }, headers=e1)
    assert r1.json()["status"] == "export_ready"

    s2 = "s-" + uuid.uuid4().hex[:12]
    rf = client.post("/api/asclepius/submissions", json={
        "submission_id": s2, "task_id": tid,
        "prompt_review": {"reviewed": True, "verdict": "flagged", "note": "not answerable"},
    }, headers=e2)
    assert rf.json()["status"] == "prompt_flagged"

    assert client.get(f"/api/asclepius/submissions/{s1}", headers=admin_h).json()["status"] == "needs_qa"
    flagged = client.get("/api/asclepius/tasks?status=prompt_flagged", headers=admin_h).json()["tasks"]
    assert any(t["task_id"] == tid for t in flagged)


def test_grading_after_flag_is_routed_to_qa_not_exported():
    """The reverse race: the prompt is flagged first, then a grading that was
    already in progress lands -> routed to QA, not auto-exported; task stays flagged."""
    admin_h = A.headers_for(_admin())
    e1 = A.headers_for(_seed())
    e2 = A.headers_for(_seed())
    tid = _upload_task(admin_h, max_labels=2)

    client.post("/api/asclepius/submissions", json={
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid,
        "prompt_review": {"reviewed": True, "verdict": "flagged", "note": "bad premise"},
    }, headers=e1)

    sg = "s-" + uuid.uuid4().hex[:12]
    rg = client.post("/api/asclepius/submissions", json={
        "submission_id": sg, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
        "independent_answer": _IDEAL,
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }, headers=e2)
    assert rg.json()["status"] == "needs_qa"
    assert "prompt_flagged" in rg.json()["issues"]
    flagged = client.get("/api/asclepius/tasks?status=prompt_flagged", headers=admin_h).json()["tasks"]
    assert any(t["task_id"] == tid for t in flagged)


def test_valid_prompt_review_stamps_clinician_reviewed_on_records():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    # full independent mode: the blind capture ships as its own ideal record.
    tid = _upload_task(admin_h, independent_mode="full")

    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": _IDEAL,
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text

    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["records"]
    for rec in sub["records"]:
        assert rec["payload"]["prompt_clinician_reviewed"] is True
    # The blind independent answer rode along as its own ideal_answer record.
    assert any(rec["payload"].get("independent") for rec in sub["records"])


# ─── v2 anti-peeking: answers withheld until reveal (Eval Flow Upgrade §1) ────
def test_answers_withheld_until_independent_answer_committed(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1")
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)

    nxt = client.get("/api/asclepius/tasks/next", headers=ev_h).json()["task"]
    assert nxt["answers_withheld"] is True
    # Text is not even on the wire during Stages 1-2; only the blinded ids are.
    assert nxt["candidate_answers"] and all("text" not in c for c in nxt["candidate_answers"])
    assert all(c.get("id") for c in nxt["candidate_answers"])

    # GATE: the answers cannot be fetched until an independent answer is committed.
    blocked = client.get(f"/api/asclepius/tasks/{tid}/answers", headers=ev_h)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"] == "independent_answer_required"

    # Revealing requires a non-empty independent answer.
    empty = client.post(f"/api/asclepius/tasks/{tid}/reveal", json={"text": "   "}, headers=ev_h)
    assert empty.status_code == 400

    # Commit -> answers returned, still blinded (no generator_model).
    rev = client.post(f"/api/asclepius/tasks/{tid}/reveal", json={"text": _IDEAL["text"]}, headers=ev_h)
    assert rev.status_code == 200, rev.text
    ans = rev.json()["answers"]
    assert {a["id"] for a in ans} == {"A", "B"}
    assert all((a.get("text") or "").strip() for a in ans)
    assert all("generator_model" not in a for a in ans)

    # After committing, the GET re-fetch (refresh-resume) now succeeds for this evaluator.
    assert client.get(f"/api/asclepius/tasks/{tid}/answers", headers=ev_h).status_code == 200
    # ...but NOT for a different evaluator who never committed.
    other = client.get(f"/api/asclepius/tasks/{tid}/answers", headers=A.headers_for(_seed()))
    assert other.status_code == 403


def test_committed_independent_answer_is_authoritative_at_packaging(monkeypatch):
    """Defeats "commit garbage to unlock, then submit an AI-copied answer": the
    PACKAGED independent answer is the one committed before reveal, not whatever
    the post-reveal submission carries."""
    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1")
    admin_h = A.headers_for(_admin())
    ev_user = _seed()
    ev_h = A.headers_for(ev_user)
    # full independent mode so the committed answer ships as the blind ideal record.
    tid = _upload_task(admin_h, independent_mode="full")

    committed = "My own pre-reveal plan: IV calcium, then insulin and dextrose, then dialysis."
    client.post(f"/api/asclepius/tasks/{tid}/reveal", json={"text": committed}, headers=ev_h)

    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
        # Client tries to pass off a DIFFERENT (post-reveal) independent answer.
        "independent_answer": {"text": "Copied from the revealed AI answer A."},
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text

    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    blind = [rec for rec in sub["records"]
             if rec["type"] == "ideal_answer" and rec["payload"].get("independent")]
    assert len(blind) == 1
    assert blind[0]["payload"]["ideal_answer"] == committed  # committed wins, not the client value


def test_answers_inline_when_withholding_disabled(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "0")
    ev_h = A.headers_for(_seed())
    tid = _upload_task(A.headers_for(_admin()))
    nxt = client.get("/api/asclepius/tasks/next", headers=ev_h).json()["task"]
    assert nxt["answers_withheld"] is False
    assert all(c.get("text") is not None for c in nxt["candidate_answers"])


def test_task_answers_requires_auth():
    assert client.get("/api/asclepius/tasks/whatever/answers").status_code == 401
    assert client.post("/api/asclepius/tasks/whatever/reveal", json={"text": "x"}).status_code == 401


# ─── Tap-to-grade reasoning split (Eval Flow Upgrade §4) ──────────────────────
def test_reasoning_split_returns_ordered_steps():
    ev_h = A.headers_for(_seed())
    text = ("Give IV calcium to stabilize the myocardium.\n"
            "Shift potassium intracellularly with insulin and dextrose.\n"
            "Remove potassium from the body via dialysis.")
    r = client.post("/api/asclepius/reasoning/split",
                    json={"text": text, "prompt": "hyperkalemia in ESRD", "specialty": "nephrology"},
                    headers=ev_h)
    assert r.status_code == 200, r.text
    steps = r.json()["steps"]
    assert len(steps) >= 3
    assert any("calcium" in s.lower() for s in steps)
    # No LLM key in tests -> graceful heuristic fallback (never errors the doctor).
    assert r.json()["source"] in ("llm", "heuristic")


def test_reasoning_split_empty_text_returns_no_steps():
    ev_h = A.headers_for(_seed())
    r = client.post("/api/asclepius/reasoning/split", json={"text": "   "}, headers=ev_h)
    assert r.status_code == 200
    assert r.json()["steps"] == []


def test_reasoning_split_requires_auth():
    r = client.post("/api/asclepius/reasoning/split", json={"text": "x"})
    assert r.status_code == 401


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
                   "independent_answer": _IDEAL,
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
            "independent_answer": _IDEAL,
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


# ─── Speed Optimization: quick stance, pre-labeling, pregrade, transcribe ─────
def test_blind_task_exposes_independent_mode_default_stance():
    ev_h = A.headers_for(_seed())
    _upload_task(A.headers_for(_admin()))
    nxt = client.get("/api/asclepius/tasks/next", headers=ev_h).json()["task"]
    assert nxt["independent_mode"] == "stance"


def test_stance_ships_on_preference_not_as_gold_ideal(monkeypatch):
    """Feature 1: on a stance-mode task the pre-reveal quick take rides the
    preference record as ``stance`` (anchoring guard) and is NOT emitted as an
    independent ideal_answer record; the reveal gate still requires it."""
    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1")
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)  # default independent_mode = stance

    # Reveal still gates on a non-empty capture.
    assert client.post(f"/api/asclepius/tasks/{tid}/reveal", json={"text": " "}, headers=ev_h).status_code == 400
    stance = "continue reduced-dose metformin; recheck eGFR in three months; watch for lactic acidosis"
    rev = client.post(f"/api/asclepius/tasks/{tid}/reveal", json={"text": stance}, headers=ev_h)
    assert rev.status_code == 200, rev.text

    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": stance},
        "chosen_revision": {"edited": True, "revised_text": "Give calcium gluconate, insulin-dextrose, then dialyze with K+ 2.0.", "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text

    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    prefs = [rec for rec in sub["records"] if rec["type"] == "preference"]
    assert len(prefs) == 1
    assert prefs[0]["payload"]["stance"] == stance
    # No independent blind-ideal record in stance mode…
    assert not any(rec["payload"].get("independent") for rec in sub["records"])
    # …and the gold ideal_answer is the refined CHOSEN answer (unchanged logic).
    ideals = [rec for rec in sub["records"] if rec["type"] == "ideal_answer"]
    assert len(ideals) == 1
    assert ideals[0]["payload"]["ideal_answer"].startswith("Give calcium gluconate")


def test_prelabel_gated_behind_independent_commit(monkeypatch):
    """Anti-peeking: the prelabel suggestion describes the A/B answers, so it is
    unreachable until the evaluator commits their independent capture."""
    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1")
    ev_h = A.headers_for(_seed())
    tid = _upload_task(A.headers_for(_admin()))
    blocked = client.post("/api/asclepius/assist/prelabel", json={"task_id": tid}, headers=ev_h)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"] == "independent_answer_required"

    client.post(f"/api/asclepius/tasks/{tid}/reveal", json={"text": "my quick take"}, headers=ev_h)
    r = client.post("/api/asclepius/assist/prelabel", json={"task_id": tid}, headers=ev_h)
    assert r.status_code == 200, r.text
    # No LLM key in tests -> graceful degrade to skipped (manual labeling works).
    assert r.json()["skipped"] is True


def test_prelabel_returns_suggestion_and_never_applies_it(monkeypatch):
    import routers.asclepius as asc_router

    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1")
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)
    client.post(f"/api/asclepius/tasks/{tid}/reveal", json={"text": "quick take"}, headers=ev_h)

    async def _fake_prelabel(task):
        return {
            "skipped": False, "suggested_weaker": "B",
            "suggested_error_tags": ["dosing_error"],
            "suggested_rationale": "Dialysate K+ 1.0 is unsafely aggressive here.",
            "error_spans": ["dialysate K+ to 1.0"], "confidence": 0.9,
        }

    monkeypatch.setattr(asc_router, "run_prelabel", _fake_prelabel)
    r = client.post("/api/asclepius/assist/prelabel", json={"task_id": tid}, headers=ev_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    assert body["suggested_weaker"] == "B"
    assert body["suggested_error_tags"] == ["dosing_error"]
    assert body["confidence"] == 0.9

    # The suggestion is NEVER server-applied: no submission/verdict exists and
    # the task is untouched in the queue.
    assert _store().submissions_for_task(tid) == []
    assert _store().get_task(tid)["status"] == "open"


def test_prelabel_hides_low_confidence_suggestions(monkeypatch):
    import routers.asclepius as asc_router

    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1")
    ev_h = A.headers_for(_seed())
    tid = _upload_task(A.headers_for(_admin()))
    client.post(f"/api/asclepius/tasks/{tid}/reveal", json={"text": "quick take"}, headers=ev_h)

    async def _uncertain_prelabel(task):
        return {"skipped": False, "suggested_weaker": "A", "suggested_error_tags": [],
                "suggested_rationale": None, "error_spans": [], "confidence": 0.4}

    monkeypatch.setattr(asc_router, "run_prelabel", _uncertain_prelabel)
    r = client.post("/api/asclepius/assist/prelabel", json={"task_id": tid}, headers=ev_h)
    assert r.status_code == 200
    assert r.json() == {"skipped": True, "reason": "low_confidence"}


def test_reasoning_pregrade_degrades_to_unlabeled_heuristic():
    """Offline the pregrade endpoint still splits (heuristic) but suggests NO
    labels — silence is not a suggestion; the doctor grades manually."""
    ev_h = A.headers_for(_seed())
    text = "Give IV calcium gluconate.\nStart insulin with dextrose.\nArrange urgent dialysis."
    r = client.post("/api/asclepius/reasoning/pregrade",
                    json={"text": text, "prompt": "hyperkalemia", "specialty": "nephrology"},
                    headers=ev_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["steps"]) == 3
    assert all(s["suggested_label"] is None for s in body["steps"])
    assert body["source"] in ("llm", "heuristic")


def test_reasoning_pregrade_requires_auth():
    assert client.post("/api/asclepius/reasoning/pregrade", json={"text": "x"}).status_code == 401


def test_transcribe_degrades_to_503_without_provider(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ev_h = A.headers_for(_seed())
    r = client.post("/api/asclepius/transcribe",
                    files={"file": ("dictation.webm", b"\x1aE\xdf\xa3fakeaudio", "audio/webm")},
                    headers=ev_h)
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "stt_unavailable"


def test_transcribe_requires_auth():
    r = client.post("/api/asclepius/transcribe",
                    files={"file": ("d.webm", b"x", "audio/webm")})
    assert r.status_code == 401


def test_prelabel_gate_is_unconditional_even_with_withholding_off(monkeypatch):
    """The prelabel suggestion describes the answers, so the independent-commit
    gate applies even in v1 mode (ASCLEPIUS_WITHHOLD_ANSWERS=0)."""
    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "0")
    ev_h = A.headers_for(_seed())
    tid = _upload_task(A.headers_for(_admin()))
    blocked = client.post("/api/asclepius/assist/prelabel", json={"task_id": tid}, headers=ev_h)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"] == "independent_answer_required"


def test_buyer_request_independent_mode_constraint_applies_to_batch():
    """A premium/eval buyer request with independent_mode='full' produces
    full-mode tasks without repeating the field on every prompt row."""
    admin_h = A.headers_for(_admin())
    buyer = client.post("/api/asclepius/buyers", json={"name": "Lab Z"}, headers=admin_h).json()
    req = client.post("/api/asclepius/buyer-requests", json={
        "buyer_id": buyer["buyer_id"], "source": "lab_supplied",
        "independent_mode": "full",
        "prompts": [_task_body()],
    }, headers=admin_h).json()
    batch = client.post(f"/api/asclepius/buyer-requests/{req['request_id']}/batch",
                        json={}, headers=admin_h)
    assert batch.status_code == 200, batch.text
    tid = batch.json()["created"][0]
    assert _store().get_task(tid)["independent_mode"] == "full"


# ─── Asclepius V2: portal version end-to-end ──────────────────────────────────
def test_taxonomy_exposes_portal_versions():
    r = client.get("/api/asclepius/taxonomy", headers=A.headers_for(_seed()))
    body = r.json()
    assert body["portal_versions"] == ["v1", "v2", "v3", "v4"]
    # Seamless PRD: the ~10s instinct capture is now a first-class mode.
    assert "instinct" in body["independent_modes"]


def test_v1_reveal_stamps_full_kind_on_stance_task(monkeypatch):
    """A V1 evaluator on a stance-default task: the reveal commit is stamped
    kind='full', portal_version='v1', and the submission ships the classic blind
    ideal record tagged portal_version='v1'."""
    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1")
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)  # stance default

    rev = client.post(f"/api/asclepius/tasks/{tid}/reveal",
                      json={"text": "full ideal answer", "portal_version": "v1"}, headers=ev_h)
    assert rev.status_code == 200, rev.text

    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "ignored — commit wins"},
        "portal_version": "v1",
        "chosen_revision": {"edited": False, "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["portal_version"] == "v1"
    blind = [rec for rec in sub["records"] if rec["type"] == "ideal_answer" and rec["payload"].get("independent")]
    assert len(blind) == 1  # classic full blind ideal even though task is stance-default
    assert blind[0]["payload"]["ideal_answer"] == "full ideal answer"
    assert all(rec["payload"]["portal_version"] == "v1" for rec in sub["records"])


def test_v2_reveal_stance_on_stance_task(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1")
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)

    client.post(f"/api/asclepius/tasks/{tid}/reveal",
                json={"text": "quick stance", "portal_version": "v2"}, headers=ev_h)
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "quick stance"},
        "portal_version": "v2",
        "chosen_revision": {"edited": True, "revised_text": "refined gold", "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["portal_version"] == "v2"
    assert not any(rec["payload"].get("independent") for rec in sub["records"])
    pref = [rec for rec in sub["records"] if rec["type"] == "preference"][0]
    assert pref["payload"]["stance"] == "quick stance"


def test_stats_reports_portal_version_counts(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_WITHHOLD_ANSWERS", "1")
    admin_h = A.headers_for(_admin())
    for pv in ("v1", "v2"):
        ev_h = A.headers_for(_seed())
        tid = _upload_task(admin_h)
        client.post(f"/api/asclepius/tasks/{tid}/reveal",
                    json={"text": "answer", "portal_version": pv}, headers=ev_h)
        client.post("/api/asclepius/submissions", json={
            "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid, "verdict": "A_better",
            "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
            "prompt_review": {"reviewed": True, "verdict": "valid"},
            "independent_answer": {"text": "answer"}, "portal_version": pv,
            "chosen_revision": {"edited": False, "why_better_notes": "safer"},
            "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
        }, headers=ev_h)
    stats = client.get("/api/asclepius/stats", headers=admin_h).json()
    assert stats["portal_version_counts"].get("v1") == 1
    assert stats["portal_version_counts"].get("v2") == 1


def test_export_filters_by_portal_version_and_reports_breakdown():
    """Admin can export a single V1/V2 cohort, and the manifest reports the
    per-version breakdown (Asclepius V2 admin surfacing)."""
    admin_h = A.headers_for(_admin())

    def _ready(pv):
        ev_h = A.headers_for(_seed())
        tid = _upload_task(admin_h, prompt=f"Cohort case {A.uniq(8)}?")
        client.post("/api/asclepius/submissions", json={
            "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid, "verdict": "A_better",
            "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
            "independent_answer": _IDEAL, "portal_version": pv,
            "chosen_revision": {"edited": False, "why_better_notes": "safer"},
            "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
        }, headers=ev_h)

    _ready("v1")
    _ready("v2")
    _ready("v2")

    # V2-only cohort export.
    r = client.post("/api/asclepius/exports",
                    json={"profile": "default", "portal_version": "v2"}, headers=admin_h)
    assert r.status_code == 200, r.text
    man = r.json()
    bpv = man["counts"]["by_portal_version"]
    assert set(bpv) == {"v2"} and bpv["v2"] >= 2  # only v2 records shipped
    assert man["filters"]["portal_version"] == "v2"

    # Both-cohort export reports the split.
    r2 = client.post("/api/asclepius/exports",
                     json={"profile": "default", "include_exported": True}, headers=admin_h)
    assert r2.status_code == 200, r2.text
    both = r2.json()["counts"]["by_portal_version"]
    assert both.get("v1", 0) >= 1 and both.get("v2", 0) >= 2


def test_export_invalid_portal_version_rejected():
    admin_h = A.headers_for(_admin())
    r = client.post("/api/asclepius/exports",
                    json={"profile": "default", "portal_version": "v3"}, headers=admin_h)
    assert r.status_code == 400
