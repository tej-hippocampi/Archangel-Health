"""ENV isolation — the agentic tier exists ONLY behind its own entry point.

The ENV tier is additive by construction (its own router prefix, its own
``env_runs`` table, its own queue). These tests ENFORCE that rather than trusting
it:

  * ``env`` is not a portal version at all, and appears in no single-turn vocabulary;
  * an ``env`` claim can never be stamped onto a single-turn submission (it is
    rejected, not silently relabeled — a silent relabel would attribute agentic
    work to V3);
  * ENV surfaces refuse to serve unless ``portal_version == "env"``;
  * ENV writes only to ``env_runs`` and never to the single-turn tables;
  * the V1–V5 flows behave identically with ENV present.

**This file used to assert all of the above under the literal ``"v5"``.** That
literal now means the LONGITUDINAL portal version (Longitudinal E2E PRD §5), so
every assertion here moved to ``"env"`` — and the file gained the MIRROR of each
one: a longitudinal v5 submission is a real single-turn submission and is not an
env run, which is the half a pure rename would have left untested.
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
    ENV_LEGACY_PORTAL_VERSION,
    ENV_PORTAL_VERSION,
    LONGITUDINAL_PORTAL_VERSION,
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
def test_env_is_not_a_portal_version_at_all():
    """The strongest form of the isolation claim, and the reason the rename was
    worth doing: ``env`` is outside ``PORTAL_VERSIONS``, so ``normalize`` cannot
    return it and no single-turn code path can produce it by accident."""
    assert ENV_PORTAL_VERSION == "env"
    assert "env" not in PORTAL_VERSIONS
    assert "env" not in SINGLE_TURN_PORTAL_VERSIONS
    assert "env" not in ASSISTED_PORTAL_VERSIONS
    assert "env" not in SYNTHETIC_PORTAL_VERSIONS
    from asclepius.constants import normalize_portal_version
    assert normalize_portal_version("env") != "env"


def test_v5_now_means_longitudinal_everywhere():
    """The other half of the same fact. A test that only asserted ``env != 'v5'``
    would pass with ``v5`` meaning nothing at all."""
    assert LONGITUDINAL_PORTAL_VERSION == "v5" and "v5" in PORTAL_VERSIONS
    assert "v5" in SINGLE_TURN_PORTAL_VERSIONS   # case → commit → reveal, one turn
    assert "v5" in ASSISTED_PORTAL_VERSIONS      # the V3/V4 seamless flow, not classic
    assert "v5" not in SYNTHETIC_PORTAL_VERSIONS  # real patient data
    assert REAL_CASE_PORTAL_VERSION == "v4"


def test_env_gate_matches_only_the_literal_env():
    assert is_env_portal_version("env") is True
    for other in ("v1", "v2", "v3", "v4", "v5", "", None, "ENV", "env2"):
        assert is_env_portal_version(other) is False, other


def test_the_env_gate_accepts_the_legacy_literal_only_when_asked():
    """One release of grace for a page cached before the rename (§5.4). Opt-in per
    call site, never the default: a surface that DECIDED anything on the legacy
    literal would file a chart walk as an agentic rollout."""
    assert ENV_LEGACY_PORTAL_VERSION == "v5"
    assert is_env_portal_version("v5", allow_legacy=True) is True
    assert is_env_portal_version("v5") is False
    # Grace is for the legacy env literal alone — not a general "anything goes".
    for other in ("v1", "v4", "", None):
        assert is_env_portal_version(other, allow_legacy=True) is False, other


def test_single_turn_taxonomy_advertises_v1_to_v5_and_never_env():
    """The contract the portal reads. V5 belongs here now (it is single-turn work);
    ``env`` must never appear, or the single-turn picker would offer a version its
    queue cannot serve."""
    r = client.get("/api/asclepius/taxonomy", headers=A.headers_for(_seed()))
    assert r.status_code == 200
    versions = r.json()["portal_versions"]
    assert versions == ["v1", "v2", "v3", "v4", "v5"]
    assert "env" not in versions


# ─── the single-turn flow rejects env rather than mislabeling it ──────────────
def test_an_env_claim_on_a_single_turn_submission_is_rejected():
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)
    _sid, r = _submit(tid, ev_h, "env")
    assert r.status_code == 400, r.text
    assert "env" in r.text


def test_a_v5_claim_on_a_synthetic_task_is_still_rejected():
    """The mirror the rename could have lost. ``v5`` is a valid portal version now,
    so the guard could no longer be "reject an unknown string" — it has to be
    "reject a version this task cannot be". A synthetic task carries no trajectory,
    so it can never be V5."""
    admin_h = A.headers_for(_admin())
    ev_h = A.headers_for(_seed())
    tid = _upload_task(admin_h)
    _sid, r = _submit(tid, ev_h, "v5")
    assert r.status_code == 400, r.text
    assert "trajectory" in r.text


def test_the_single_turn_queue_never_serves_a_real_case_to_an_env_claim():
    """Even if a client forges portal_version=env on the queue, the walls hold:
    ``env`` is neither the real-case version nor the longitudinal one, so real
    de-identified cases stay excluded."""
    ev_h = A.headers_for(_seed())
    r = client.get("/api/asclepius/tasks/next?portal_version=env", headers=ev_h)
    assert r.status_code in (200, 204, 404)
    body = r.json() if r.status_code == 200 else None
    if isinstance(body, dict) and body:
        assert (body.get("case_source") or "synthetic") != "real_deid"


# ─── ENV surfaces are gated on portal_version == env ──────────────────────────
def test_the_env_annotation_queue_requires_portal_version_env():
    ev_h = A.headers_for(_seed())
    for bad in ("v1", "v2", "v3", "v4"):
        r = client.get(f"/api/asclepius/environments/annotation-queue?portal_version={bad}",
                       headers=ev_h)
        assert r.status_code == 400, bad
    ok = client.get("/api/asclepius/environments/annotation-queue?portal_version=env",
                    headers=ev_h)
    assert ok.status_code == 200
    assert ok.json()["portal_version"] == "env"


def test_a_page_cached_before_the_rename_still_reaches_the_env_queue():
    """§5.4 back-compat: the legacy literal is accepted for one release so a
    physician mid-annotation is not 400'd over a string their page will stop
    sending on reload. What comes BACK is always the new value — the grace is for
    reading a stale request, never for writing a stale one."""
    ev_h = A.headers_for(_seed())
    r = client.get("/api/asclepius/environments/annotation-queue?portal_version=v5",
                   headers=ev_h)
    assert r.status_code == 200
    assert r.json()["portal_version"] == "env"


def test_env_admin_surfaces_require_admin():
    ev_h = A.headers_for(_seed())
    assert client.post("/api/asclepius/environments/nephrology/generate",
                       json={"n": 1}, headers=ev_h).status_code == 403
    assert client.get("/api/asclepius/environments/export?mode=raw",
                      headers=ev_h).status_code == 403
    assert client.get("/api/asclepius/environments", headers=ev_h).status_code == 403


def test_env_routes_live_under_their_own_prefix_and_cannot_shadow_the_portal():
    from routers.asclepius import router as core_router
    from routers.asclepius_env import router as env_router

    env_paths = {r.path for r in env_router.routes}
    core_paths = {r.path for r in core_router.routes}
    assert env_paths, "the ENV router must expose routes"
    assert all(p.startswith("/api/asclepius/environments") for p in env_paths)
    assert not (env_paths & core_paths), "ENV must not collide with a single-turn route"


# ─── storage isolation ────────────────────────────────────────────────────────
def test_env_writes_only_to_env_runs_and_leaves_the_single_turn_tables_untouched():
    from asclepius.environments import service

    store = _store()

    def counts():
        return (len(store.list_tasks(limit=100000)),
                len(store.list_submissions(limit=100000)))

    before = counts()
    built = service.generate_from_gold(store, "nephrology", n=1)
    assert built["built"], built
    assert counts() == before, "generating an ENV environment must not touch the portal's tables"
    assert store.list_env_runs(mode="generated", limit=10), "the environment belongs in env_runs"


def test_an_env_environment_is_invisible_to_the_single_turn_queue():
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


# ─── V1–V5 behavior is unchanged with ENV present ─────────────────────────────
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


def test_the_single_turn_export_rejects_env_and_accepts_v5():
    """An ENV export is a different artifact (agentic trajectories, not preference
    pairs). Accepting 'env' here would build an EMPTY single-turn bundle labelled as
    the agentic tier — worse than an error, because it looks like a successful
    delivery.

    V5 is the mirror, and it is the bug this relabel fixes: a longitudinal export
    was rejected by this same guard, so the one product the whole pipeline exists to
    ship could not be cut from the export tab at all."""
    admin_h = A.headers_for(_admin())
    r = client.post("/api/asclepius/exports", json={"portal_version": "env"}, headers=admin_h)
    assert r.status_code == 400
    assert "environments/export" in r.text
    # v1–v5 are all acceptable values
    for good in ("v1", "v2", "v3", "v4", "v5"):
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
