"""PRD demo-day P0-2 — promoting a hospital case must not dead-end in a 409.

The chain that broke path 3 ("hospital data becomes a labelable clinical case"):
the portal upload door writes no manifest specialty, resolution falls through to
the neutral ``general`` because the ``hs-portal`` sentinel has no upload-link row,
and both promote endpoints refuse ``general`` — correctly, because a WRONG
specialty routes the case to the wrong physician pool and mislabels the export
invisibly. The resolver endpoint existed with **zero frontend callers**, so the
409 told the admin to use a control that did not exist.

What is asserted here:

  * the uploads list the admin promotes from carries the server's OWN verdict on
    whether promotion is possible, and why not — so a Promote button is never
    rendered live into a guaranteed 409;
  * that verdict names the same three refusals the promote endpoints enforce, in
    the same precedence order (brokering before specialty, so a brokering upload
    is never told to pick a specialty it will never use);
  * the resolver refuses a specialty that is not in the enabled registry, because
    a free-text field would reproduce, through the fix, the exact failure the gate
    exists to prevent;
  * setting a real specialty clears the block and promotion proceeds.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EMAIL_DEV_MODE"] = "1"

import tests._asclepius as A  # noqa: E402

API = "/api/asclepius"
client = TestClient(A.app)

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_h(store):
    return A.headers_for(A.make_user(store, role="admin"))


def _bundle() -> bytes:
    """A real bundle that simply declares no specialty — the common case, since
    specialty is a property of the data and is not asked of hospital IT."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"patient_key": "ptS"}))
        z.writestr("labs.csv",
                   "patient_key,panel,analyte,value,unit,collected_at\n"
                   "ptS,BMP,Creatinine,2.4,mg/dL,2025-03-08\n"
                   "ptS,BMP,Creatinine,1.1,mg/dL,2025-03-01\n")
        z.writestr("note.txt", "Progress note: AKI, creatinine rising since "
                               "3/1/2025, improving by 2025-03-09.")
    return buf.getvalue()


def _portal_upload(store, *, purpose: str = "task_creation") -> str:
    """Walk the real hospital-portal door — the one that writes no specialty."""
    hs = store.ensure_health_system("Specialty General", contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    store.set_hs_portal_purpose(username, purpose)
    c = TestClient(A.app, base_url="https://testserver")
    assert c.post(f"{API}/hs/login",
                  json={"username": username, "password": "portal-pass-123456"}
                  ).status_code == 200
    data = _bundle()
    r = c.post(f"{API}/hs/uploads",
               files=[("files", ("b.zip", data, "application/zip"))])
    assert r.status_code == 200, r.text
    assert hashlib.sha256(data).hexdigest()  # bundle is deterministic; sanity only
    return r.json()["upload_id"]


def _row(admin_h, upload_id):
    r = client.get(f"{API}/ingestion/uploads?limit=200&offset=0", headers=admin_h)
    assert r.status_code == 200, r.text
    for u in r.json()["uploads"]:
        if u["upload_id"] == upload_id:
            return u
    raise AssertionError(f"{upload_id} missing from the promote list")


# ─── The row the Promote button is rendered from says it is blocked ───────────
def test_an_undetermined_specialty_is_visible_before_the_click(monkeypatch):
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    upload_id = _portal_upload(store)

    row = _row(admin_h, upload_id)
    assert row["ingested_case_count"] > 0, "the upload really did ingest cases"
    assert row["specialty_determined"] is False
    assert row["specialty_undetermined_cases"] == row["ingested_case_count"]

    block = row["promote_block"]
    assert block and block["reason"] == "specialty", (
        "the admin must be able to see that Promote would 409 WITHOUT clicking it"
    )
    assert "Specialty not set" in block["message"]


def test_the_block_and_the_actual_refusal_agree(monkeypatch):
    """A reason shown on the row that differs from the reason the server gives on
    the click is worse than no reason at all."""
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    upload_id = _portal_upload(store)

    assert _row(admin_h, upload_id)["promote_block"]["reason"] == "specialty"
    refused = client.post(f"{API}/ingestion/uploads/{upload_id}/prepare",
                          json={}, headers=admin_h)
    assert refused.status_code == 409
    assert "Specialty not determined" in refused.json()["detail"]


def test_brokering_outranks_specialty_in_the_reason_shown(monkeypatch):
    """A brokering upload is never promoted at all, so telling its operator to
    pick a specialty would send them to do work that changes nothing."""
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    upload_id = _portal_upload(store, purpose="brokering")

    block = _row(admin_h, upload_id)["promote_block"]
    assert block and block["reason"] == "brokering"
    assert "never promoted" in block["message"]


# ─── The resolver clears it, and only with a real specialty ───────────────────
def test_setting_a_real_specialty_clears_the_block(monkeypatch):
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    upload_id = _portal_upload(store)

    res = client.post(f"{API}/admin/uploads/{upload_id}/specialty",
                      json={"specialty": "nephrology"}, headers=admin_h)
    assert res.status_code == 200, res.text
    assert res.json()["cases_updated"] >= 1

    row = _row(admin_h, upload_id)
    assert row["promote_block"] is None
    assert row["specialty_determined"] is True
    assert row["specialties"] == ["nephrology"]


@pytest.mark.parametrize("bad", ["nefrology", "general", "  ", "cardio"])
def test_the_resolver_refuses_anything_not_in_the_enabled_registry(monkeypatch, bad):
    """The control built to stop a wrong specialty must not itself be a way to
    set one. 'general' is refused explicitly: it is the absence of a specialty,
    and accepting it here would mark the upload resolved while leaving the
    promote gate refusing it — a block with no way out."""
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    upload_id = _portal_upload(store)

    res = client.post(f"{API}/admin/uploads/{upload_id}/specialty",
                      json={"specialty": bad}, headers=admin_h)
    assert res.status_code == 400, res.text
    # Still blocked, and still blocked for the same recoverable reason.
    assert _row(admin_h, upload_id)["promote_block"]["reason"] == "specialty"


def test_the_error_names_the_choices_so_the_operator_can_act(monkeypatch):
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    upload_id = _portal_upload(store)

    detail = client.post(f"{API}/admin/uploads/{upload_id}/specialty",
                         json={"specialty": "nefrology"}, headers=admin_h
                         ).json()["detail"]
    assert "nephrology" in detail and "cardiology" in detail


def test_the_picker_is_populated_from_a_route_an_admin_can_reach(monkeypatch):
    """The selector reads GET /specialties. If that route stops admitting admins
    the picker silently renders empty and the dead end comes straight back."""
    A.fresh_store()
    store = _store()
    res = client.get(f"{API}/specialties", headers=_admin_h(store))
    assert res.status_code == 200
    enabled = [s["specialty"] for s in res.json()["specialties"] if s.get("enabled")]
    assert "nephrology" in enabled and "cardiology" in enabled


# ─── The whole path, end to end ───────────────────────────────────────────────
def test_a_hospital_upload_promotes_once_the_specialty_is_set(monkeypatch):
    """Path 3, walked: hospital bundle → blocked with a stated reason → operator
    resolves it through the endpoint the UI now calls → a real V4 task."""
    from tests.test_promotion_gate import _stub_llm

    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    upload_id = _portal_upload(store)

    cases = store.list_ingest_cases(upload_id=upload_id)
    case_id = cases[0]["ingest_case_id"]

    blocked = client.post(f"{API}/ingestion/cases/{case_id}/promote",
                          json={"question": "q"}, headers=admin_h)
    assert blocked.status_code == 409

    assert client.post(f"{API}/admin/uploads/{upload_id}/specialty",
                       json={"specialty": "nephrology"}, headers=admin_h
                       ).status_code == 200

    ok = client.post(f"{API}/ingestion/cases/{case_id}/promote",
                     json={"question": "Explain the rising creatinine."}, headers=admin_h)
    assert ok.status_code == 200, ok.text
    assert store.get_task(ok.json()["task_id"])["specialty"] == "nephrology"


# ─── The frontend actually calls it ───────────────────────────────────────────
#
# The entire P0 was "the endpoint exists and nothing calls it", which no backend
# test can catch. `grep -rn "/specialty" frontend/` returning one unrelated
# comment is what shipped, so that grep is the test.
def test_a_frontend_surface_calls_the_resolver_endpoint():
    hits = []
    for path in sorted(_FRONTEND.glob("*.js")):
        src = path.read_text(encoding="utf-8")
        if re.search(r"/specialty['\"]|\+ '/specialty'|/specialty\"", src):
            hits.append(path.name)
    assert hits, (
        "POST /admin/uploads/{id}/specialty had zero frontend callers: the "
        "promote 409 told the admin to use a control that did not exist"
    )
    assert "asclepius.js" in hits


def test_the_promote_surfaces_gate_on_the_servers_verdict():
    portal = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
    health = (_FRONTEND / "admin_health.js").read_text(encoding="utf-8")
    # One implementation of the control, handed to both surfaces — not two that
    # can drift.
    assert "function specialtyResolver(" in portal
    assert "specialtyResolver" in health
    # The promote list reads the server's block rather than re-deriving it.
    assert "promote_block" in portal
    assert "specialty_undetermined_cases" in health
