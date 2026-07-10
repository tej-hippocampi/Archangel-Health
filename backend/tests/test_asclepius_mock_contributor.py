"""Mock/sandbox contributor account + export isolation (internal demo tool).

Verifies: the mock contributor seeds idempotently as an isolated evaluator; its
records are HARD-EXCLUDED from a default export and only appear with an explicit
include_mock; the contributor directory + demo-credentials catalog surface it so
the admin can see it labeled "Mock Contributor Account" and reach the portal.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402
from asclepius.export import build_export  # noqa: E402

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


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


# ─── Seeding + isolation helpers (unit) ───────────────────────────────────────
def test_ensure_mock_contributor_seeds_isolated_evaluator():
    st = _store()
    u = asc_auth.ensure_mock_contributor(st)
    assert u["role"] == "evaluator" and u["is_mock"] == 1
    # idempotent — same account, password re-applied, still the only mock
    u2 = asc_auth.ensure_mock_contributor(st)
    assert u2["id"] == u["id"]
    assert st.mock_annotator_id_hashes() == {u["id_hashed"]}
    # authenticates with the configured credentials
    cfg = asc_auth.mock_credentials()
    assert asc_auth.authenticate(st, cfg["email"], cfg["password"]) is not None


def test_mock_disabled_seeds_nothing(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_MOCK_ENABLED", "0")
    st = _store()
    assert asc_auth.ensure_mock_contributor(st) is None
    assert st.mock_annotator_id_hashes() == set()


# ─── Export isolation (end-to-end) ────────────────────────────────────────────
def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _submit_export_ready(headers, specialty="nephrology"):
    tid = client.post("/api/asclepius/tasks", json={"tasks": [{
        "specialty": specialty, "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."}, {"id": "B", "text": "Dialysate K 1.0."}],
    }]}, headers=_admin_h()).json()["created"][0]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stabilize with IV calcium, shift with insulin+dextrose, then dialyze given the ESRD."},
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready", r.text
    return tid


def _mock_evaluator_headers():
    st = _store()
    u = asc_auth.ensure_mock_contributor(st)
    return A.headers_for(u), u["id_hashed"]


def test_mock_records_hard_excluded_from_default_export():
    st = _store()
    # A real credentialed evaluator + the mock contributor each produce a record.
    real_h = A.headers_for(A.make_user(st, role="evaluator", specialty="nephrology",
                                       board_cert="board_certified_nephrology", years_experience=12))
    _submit_export_ready(real_h)
    mock_h, mock_hash = _mock_evaluator_headers()
    _submit_export_ready(mock_h)

    # Default export excludes the mock record entirely.
    m = build_export(st, created_by="admin", profile="default")
    assert m["filters"]["include_mock"] is False
    assert m["filters"]["mock_excluded"] is True
    import json as _json
    lines = [_json.loads(x) for x in (Path(m["dir_path"]) / "records.jsonl").read_text().strip().splitlines()]
    assert lines, "the real record should still export"
    assert all(rec.get("annotator_id_hashed") != mock_hash for rec in lines), "mock record leaked into export"

    # Opt-in include_mock re-includes it (with include_exported to re-pull the shipped real one too).
    m2 = build_export(st, created_by="admin", profile="default", include_mock=True, include_exported=True)
    l2 = [_json.loads(x) for x in (Path(m2["dir_path"]) / "records.jsonl").read_text().strip().splitlines()]
    assert any(rec.get("annotator_id_hashed") == mock_hash for rec in l2), "include_mock should surface mock records"


def test_mock_only_batch_yields_no_default_export():
    """If the ONLY export-ready records are the mock's, a default export refuses
    (nothing to ship) — the sandbox can never accidentally become a batch."""
    st = _store()
    mock_h, _ = _mock_evaluator_headers()
    _submit_export_ready(mock_h)
    with pytest.raises(ValueError):
        build_export(st, created_by="admin", profile="default")


# ─── Admin surfaces the mock (labeling + reachability) ────────────────────────
def test_contributor_directory_flags_mock():
    st = _store()
    mock_h, mock_hash = _mock_evaluator_headers()
    _submit_export_ready(mock_h)
    directory = {c["id_hashed"]: c for c in st.contributor_directory()}
    assert directory[mock_hash]["is_mock"] is True


def test_demo_credentials_includes_asclepius_mock_with_portal():
    from demo_credentials import list_demo_credentials
    accounts = list_demo_credentials(cedar_password="x")
    mock = next((a for a in accounts if a.get("id") == "asclepius-mock-contributor"), None)
    assert mock is not None
    assert mock["label"] == "Asclepius — Mock Contributor Account"
    assert mock["email"] == asc_auth.mock_credentials()["email"]
    assert mock["password"] == asc_auth.mock_credentials()["password"]
    assert mock["signInUrls"]["asclepiusPortal"].endswith("/asclepius")


def test_demo_credentials_omits_mock_when_disabled(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_MOCK_ENABLED", "0")
    from demo_credentials import list_demo_credentials
    accounts = list_demo_credentials(cedar_password="x")
    assert not any(a.get("id") == "asclepius-mock-contributor" for a in accounts)
