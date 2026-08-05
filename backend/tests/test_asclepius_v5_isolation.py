"""V5 isolation — V5 exists ONLY behind the V5 entry point; V1–V4 are untouched.

The V5 agentic tier is additive by construction (its own router prefix, its own
``env_runs`` table, its own queue). These tests ENFORCE that rather than trusting it:

  * the single-turn ``/taxonomy`` still advertises exactly v1–v4;
  * a v5 claim can never be stamped onto a single-turn submission (it is rejected,
    not silently relabeled — a silent relabel would attribute agentic work to V3);
  * V5 surfaces refuse to serve unless ``portal_version == "v5"``;
  * V5 writes only to ``env_runs`` and never to the V1–V4 tables;
  * the V1/V2/V3 flows and the V4 real-case wall behave identically with V5 present.
"""

from __future__ import annotations

import uuid
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from asclepius.constants import (  # noqa: E402
    ASSISTED_PORTAL_VERSIONS,
    ENV_PORTAL_VERSION,
    PORTAL_VERSIONS,
    REAL_CASE_PORTAL_VERSION,
    SINGLE_TURN_PORTAL_VERSIONS,
    SYNTHETIC_PORTAL_VERSIONS,
    is_env_portal_version,
)

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


def _seed(role="evaluator", **kw):
    return A.make_user(_store(), role=role, specialty="nephrology",
                       board_cert="board_certified_nephrology", years_experience=12, **kw)


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


def _submit(tid, ev_h, portal_version):
    sid = "s-" + uuid.uuid4().hex[:12]
    return sid, client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 120,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "quick stance"},
        "portal_version": portal_version,
        "chosen_revision": {"edited": True, "revised_text": "refined gold", "why_better_notes": "safer"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "x"},
    }, headers=ev_h)


# ─── vocabulary boundaries ────────────────────────────────────────────────────
def test_v5_is_registered_but_excluded_from_every_single_turn_vocabulary():
    assert ENV_PORTAL_VERSION == "v5" and "v5" in PORTAL_VERSIONS
    # …and absent from every list that drives single-turn behavior
    assert "v5" not in SINGLE_TURN_PORTAL_VERSIONS
    assert "v5" not in ASSISTED_PORTAL_VERSIONS   # never lights up assisted surfaces
    assert "v5" not in SYNTHETIC_PORTAL_VERSIONS
    assert REAL_CASE_PORTAL_VERSION == "v4"


def test_v5_gate_matches_only_the_literal_v5():
    assert is_env_portal_version("v5") is True
    for other in ("v1", "v2", "v3", "v4", "", None, "V5", "v50"):
        assert is_env_portal_version(other) is False, other


def test_single_turn_taxonomy_still_advertises_exactly_v1_to_v4():
    """The V1–V4 contract the existing portal reads. V5 must not appear here — the
    single-turn picker would otherwise offer a version its queue cannot serve."""
    r = client.get("/api/asclepius/taxonomy", headers=A.headers_for(_seed()))
    assert r.status_code == 200
    assert r.json()["portal_versions"] == ["v1", "v2", "v3", "v4"]


# ─── the single-turn flow rejects v5 rather than mislabeling it ────────────────
def test_a_v5_claim_on_a_single_turn_submission_is_rejected():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)
    _sid, r = _submit(tid, ev_h, "v5")
    assert r.status_code == 400, r.text
    assert "v5" in r.text


def test_the_single_turn_queue_never_serves_a_real_case_to_a_v5_claim():
    """Even if a client forges portal_version=v5 on the queue, the V4 wall holds: v5
    is not the real-case version, so real de-identified cases stay excluded."""
    ev_h = A.headers_for(_seed())
    r = client.get("/api/asclepius/tasks/next?portal_version=v5", headers=ev_h)
    assert r.status_code in (200, 204, 404)
    body = r.json() if r.status_code == 200 else None
    if isinstance(body, dict) and body:
        assert (body.get("case_source") or "synthetic") != "real_deid"


# ─── V5 surfaces are gated on portal_version == v5 ────────────────────────────
def test_the_v5_annotation_queue_requires_portal_version_v5():
    ev_h = A.headers_for(_seed())
    for bad in ("v1", "v2", "v3", "v4"):
        r = client.get(f"/api/asclepius/environments/annotation-queue?portal_version={bad}",
                       headers=ev_h)
        assert r.status_code == 400, bad
    ok = client.get("/api/asclepius/environments/annotation-queue?portal_version=v5",
                    headers=ev_h)
    assert ok.status_code == 200
    assert ok.json()["portal_version"] == "v5"


def test_v5_admin_surfaces_require_admin():
    ev_h = A.headers_for(_seed())
    assert client.post("/api/asclepius/environments/nephrology/generate",
                       json={"n": 1}, headers=ev_h).status_code == 403
    assert client.get("/api/asclepius/environments/export?mode=raw",
                      headers=ev_h).status_code == 403
    assert client.get("/api/asclepius/environments", headers=ev_h).status_code == 403


def test_v5_routes_live_under_their_own_prefix_and_cannot_shadow_v1_to_v4():
    from routers.asclepius import router as core_router
    from routers.asclepius_env import router as env_router

    env_paths = {r.path for r in env_router.routes}
    core_paths = {r.path for r in core_router.routes}
    assert env_paths, "the V5 router must expose routes"
    assert all(p.startswith("/api/asclepius/environments") for p in env_paths)
    assert not (env_paths & core_paths), "V5 must not collide with a single-turn route"


# ─── storage isolation ────────────────────────────────────────────────────────
def test_v5_writes_only_to_env_runs_and_leaves_the_single_turn_tables_untouched():
    from asclepius.environments import service

    store = _store()

    def counts():
        return (len(store.list_tasks(limit=100000)),
                len(store.list_submissions(limit=100000)))

    before = counts()
    built = service.generate_from_gold(store, "nephrology", n=1)
    assert built["built"], built
    assert counts() == before, "generating a V5 environment must not touch V1–V4 tables"
    assert store.list_env_runs(mode="generated", limit=10), "the environment belongs in env_runs"


def test_a_v5_environment_is_invisible_to_the_single_turn_queue():
    from asclepius.environments import service

    store = _store()
    service.generate_from_gold(store, "nephrology", n=2)
    env_ids = {e["task_id"] for e in store.list_env_runs(mode="generated", limit=100)}
    assert env_ids
    ev_h = A.headers_for(_seed())
    for version in ("v1", "v2", "v3"):
        r = client.get(f"/api/asclepius/tasks/next?portal_version={version}", headers=ev_h)
        if r.status_code == 200 and isinstance(r.json(), dict):
            assert (r.json() or {}).get("task_id") not in env_ids, version


# ─── V1–V4 behavior is unchanged with V5 present ──────────────────────────────
@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_synthetic_versions_still_stamp_themselves_on_a_submission(version):
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)
    client.post(f"/api/asclepius/tasks/{tid}/reveal",
                json={"text": "quick stance", "portal_version": version}, headers=ev_h)
    sid, r = _submit(tid, ev_h, version)
    assert r.status_code == 200, r.text
    sub = client.get(f"/api/asclepius/submissions/{sid}", headers=admin_h).json()
    assert sub["portal_version"] == version


def test_the_single_turn_export_rejects_a_v5_portal_version():
    """A V5 export is a different artifact (trajectories, not preference pairs).
    Accepting 'v5' here would build an EMPTY V1–V4 bundle labeled as the agentic
    tier — worse than an error, because it looks like a successful delivery."""
    admin_h = A.headers_for(_admin())
    r = client.post("/api/asclepius/exports", json={"portal_version": "v5"}, headers=admin_h)
    assert r.status_code == 400
    assert "environments/export" in r.text
    # v1–v4 remain acceptable values
    for good in ("v1", "v2", "v3", "v4"):
        ok = client.post("/api/asclepius/exports", json={"portal_version": good}, headers=admin_h)
        assert ok.status_code != 400 or "portal_version" not in ok.text, good


# ─── the V4 ingestion path must survive the new timing fields ─────────────────
def test_note_and_study_dates_are_always_stripped_even_with_no_anchor():
    """Regression: the new per-item timing fields enter as RAW date keys
    (``collected_at``/``authored_on``/``recorded_at``). ``ClinicalNote``/``Study`` are
    ``extra="forbid"``, so if normalization ever left one behind — e.g. when no index
    anchor could be established — ``ClinicalCase(**case)`` would raise and V4
    ingestion would break outright."""
    from asclepius.cases import ClinicalCase
    from asclepius.timeline import normalize_timeline

    # no lab/vitals dates at all → no anchor can be established (index is None)
    fragments = {
        "demographics": {"age_band": "60-69", "sex": "M"},
        "notes": [{"note_type": "Admit", "author_role": "md", "text": "no dates here",
                   "collected_at": "2031-03-14"}],
        "studies": [{"modality": "ecg", "label": "12-lead", "findings": "NSR",
                     "collected_at": "2031-03-14"}],
        "problem_list": [{"condition": "CKD", "recorded_at": "2031-03-14"}],
        "medications": [{"drug": "lisinopril", "authored_on": "2031-03-14"}],
    }
    case, _report = normalize_timeline(fragments)
    for key, items in (("notes", case.get("notes")), ("studies", case.get("studies")),
                       ("problem_list", case.get("problem_list")),
                       ("medications", case.get("medications"))):
        for item in items or []:
            leftovers = {"collected_at", "authored_on", "recorded_at", "effective_at"} & set(item)
            assert not leftovers, f"{key} kept raw date key(s) {leftovers}"
    # the whole point: this must validate
    ClinicalCase(**{**case, "case_source": "real_deid", "specialty": "nephrology"})


def test_vitals_timing_marker_survives_the_ingestion_merge():
    """``_vitals_at`` is underscore-prefixed fragment metadata, and ingestion strips
    those keys before normalizing — so it must be merged and passed EXPLICITLY or the
    vitals temporal gate silently never engages on real data."""
    from asclepius.ingestion import _merge_fragments
    from asclepius.timeline import normalize_timeline

    merged = _merge_fragments([
        {"vitals": {"bp": "110/70"}, "_vitals_at": "2031-03-14",
         "lab_panels": [{"panel": "BMP", "collected_at": "2031-03-20", "results": []}]},
        {"vitals": {"hr": 92}, "_vitals_at": "2031-03-18"},
    ])
    assert merged["_vitals_at"] == "2031-03-18", "the LATEST vitals date must win"
    body = {k: v for k, v in merged.items() if not str(k).startswith("_")}  # as ingestion does
    case, _ = normalize_timeline(body, index_event=None, vitals_at=merged.get("_vitals_at"))
    # index = latest structured date (3/20); vitals at 3/18 → offset -2
    assert case["vitals"]["collected_offset_days"] == -2


def test_v4_remains_the_only_real_case_version():
    """The V4 wall (EHR PRD §9.5) is untouched: a synthetic task still refuses a v4
    claim, which is what keeps real and synthetic provenance separable."""
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)
    _sid, r = _submit(tid, ev_h, "v4")
    assert r.status_code == 400
    assert "v4" in r.text
