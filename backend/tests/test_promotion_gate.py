"""The promotion gate and the pipeline behind it — PRD-I §4.

The gate is the functional half of the confidentiality requirement and the easy
half to miss, because nothing visibly breaks without it. A promoted brokering
case gets labelled by a physician and ships inside a training bundle sold to a
lab — data a partner sent us to broker, resold as annotation work.

Every test goes through the real routes. The gate is only worth anything at the
HTTP boundary an admin actually reaches.
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EMAIL_DEV_MODE"] = "1"

import tests._asclepius as A  # noqa: E402
from asclepius import ingestion as asc_ingestion  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

API = "/api/asclepius"
client = TestClient(A.app)


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


def _stub_llm(monkeypatch):
    """The promotion path calls out to two frontier models and a judge. Stubbed so
    this file tests the GATE rather than the weather at an inference provider."""
    from routers import asclepius as R
    from asclepius import critic

    async def fake_candidates(prompt, **kw):
        return {"candidates": [{"id": "A", "text": "AKI from volume depletion; hydrate."},
                               {"id": "B", "text": "Obstructive uropathy; image first."}],
                "model": "cand", "intended_flawed_id": "B"}

    async def fake_hardness(prompt, candidates=None, **kw):
        return {"skipped": False, "hardness_score": 0.9, "hardness_axes": ["multi_step"]}

    async def fake_case_judge(case, **kw):
        return {"skipped": False, "coherence": 0.95, "multimodal_necessity": 0.95,
                "reasoning_divergence_potential": 0.95}

    monkeypatch.setattr(R, "generate_candidates_ex", fake_candidates)
    monkeypatch.setattr(critic, "run_hardness_judge", fake_hardness)
    monkeypatch.setattr(critic, "run_case_judge", fake_case_judge)
    monkeypatch.setattr(R, "run_hardness_judge", fake_hardness, raising=False)
    monkeypatch.setattr(R, "run_case_judge", fake_case_judge, raising=False)


def _bundle(patient: str = "pt1") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"specialty": "nephrology",
                                                "patient_key": patient}))
        z.writestr("labs.csv",
                   "patient_key,panel,analyte,value,unit,collected_at\n"
                   f"{patient},BMP,Creatinine,2.4,mg/dL,2025-03-08\n"
                   f"{patient},BMP,Creatinine,1.1,mg/dL,2025-03-01\n")
        z.writestr("note.txt", "Progress nephrology: AKI, creatinine rising since "
                               "3/1/2025, improving by 2025-03-09.")
    return buf.getvalue()


def _upload_as(purpose, *, org="Regional Health", patient="pt1"):
    """Provision a portal account with the given purpose and push a bundle through
    the REAL portal upload route, so the purpose is stamped the way production
    stamps it — by a server-side join, not by the test writing the column."""
    store = _store()
    hs = store.ensure_health_system(org, contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    if purpose:
        store.set_hs_portal_purpose(username, purpose)
    c = TestClient(A.app, base_url="https://testserver")
    assert c.post(f"{API}/hs/login", json={"username": username,
                                           "password": "portal-pass-123456"}).status_code == 200
    r = c.post(f"{API}/hs/uploads",
               files=[("files", ("bundle.zip", _bundle(patient), "application/zip"))])
    assert r.status_code == 200, r.text
    return hs, r.json()["upload_id"]


def _cases(upload_id, status="ingested"):
    return [c for c in _store().list_ingest_cases(upload_id=upload_id)
            if c.get("status") == status]


# ── purpose reaches the case, server-side ────────────────────────────────────
def test_purpose_flows_from_the_account_to_the_case():
    A.fresh_store()
    for purpose in ("task_creation", "brokering"):
        _hs, upload_id = _upload_as(purpose, org=f"Org {purpose}")
        upload = _store().get_ingest_upload(upload_id)
        assert upload["purpose"] == purpose
        cases = _store().list_ingest_cases(upload_id=upload_id)
        assert cases, "no cases produced"
        assert {c["purpose"] for c in cases} == {purpose}, (
            "purpose did not propagate to the cases — the promotion gate reads the "
            "CASE, so a case with no purpose is a case that can be promoted")


def test_a_client_cannot_choose_its_own_purpose():
    """The one thing that would make the whole column meaningless."""
    A.fresh_store()
    store = _store()
    hs = store.ensure_health_system("Sneaky General", contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    store.set_hs_portal_purpose(username, "brokering")
    c = TestClient(A.app, base_url="https://testserver")
    c.post(f"{API}/hs/login", json={"username": username, "password": "portal-pass-123456"})

    import hashlib
    data = _bundle()
    r = c.post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "purpose": "task_creation"})          # ← the attempt
    assert r.status_code == 200
    sid = r.json()["session_id"]
    chunk = r.json()["chunk_size"]
    for n, blob in enumerate([data[i:i + chunk] for i in range(0, len(data), chunk)], 1):
        c.put(f"{API}/hs/uploads/sessions/{sid}/parts/{n}", content=blob,
              headers={"X-Chunk-SHA256": hashlib.sha256(blob).hexdigest()})
    done = c.post(f"{API}/hs/uploads/sessions/{sid}/complete")
    assert done.status_code == 200
    assert _store().get_ingest_upload(done.json()["upload_id"])["purpose"] == "brokering"


# ── the gate ─────────────────────────────────────────────────────────────────
def test_a_task_creation_case_becomes_a_task(monkeypatch):
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as("task_creation")
    cases = _cases(upload_id)
    assert cases, "the bundle produced no ingested case"

    r = client.post(f"{API}/ingestion/cases/{cases[0]['ingest_case_id']}/promote",
                    json={"question": "Classify the AKI and give the next step."},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    task = store.get_task(r.json()["task_id"])
    assert task and task["source"] == "partner_ehr"


def test_a_brokering_case_cannot_be_promoted(monkeypatch):
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as("brokering")
    cases = _cases(upload_id)
    assert cases

    ic_id = cases[0]["ingest_case_id"]
    r = client.post(f"{API}/ingestion/cases/{ic_id}/promote",
                    json={"question": "Classify the AKI and give the next step."},
                    headers=admin_h)
    assert r.status_code == 409
    assert "brokering" in r.json()["detail"]

    # The case survives intact — refusing to promote is not a reason to damage it.
    after = store.get_ingest_case(ic_id)
    assert after["status"] == "ingested" and after["task_id"] is None
    assert after["case"], "the case body was lost"
    # …and the reason is on the record.
    events = [e for e in store.list_events(entity_id=ic_id)
              if e.get("event_type") == "promote_refused_brokering"]
    assert events, "the refusal was not recorded"


def test_no_task_exists_for_a_brokering_bundle_by_any_route(monkeypatch):
    """The invariant stated as an outcome rather than as a status code."""
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as("brokering")
    ic_id = _cases(upload_id)[0]["ingest_case_id"]

    before = len(store.list_tasks(limit=100000))
    client.post(f"{API}/ingestion/cases/{ic_id}/promote",
                json={"question": "q"}, headers=admin_h)
    client.post(f"{API}/ingestion/uploads/{upload_id}/promote-all",
                json={"question": "q"}, headers=admin_h)
    assert len(store.list_tasks(limit=100000)) == before


def test_promote_all_promotes_the_task_creation_cases_and_counts_the_skipped(monkeypatch):
    """A mixed upload must not fail wholesale. Failing the batch pushes the admin
    toward promoting case-by-case, which is the workflow where one eventually
    slips through."""
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as("task_creation")
    cases = _cases(upload_id)
    assert cases
    # Mark one case brokering directly: a single upload is stamped one way, so a
    # genuinely mixed BATCH is the state that arises from a purpose correction on
    # part of an upload, and the batch logic has to handle it.
    extra = store.insert_ingest_case(
        upload_id=upload_id, patient_key="pk-broker", specialty="nephrology",
        case=cases[0]["case"], status="ingested", report=None)
    store.set_ingest_case_purpose(extra["ingest_case_id"], "brokering")

    r = client.post(f"{API}/ingestion/uploads/{upload_id}/promote-all",
                    json={"question": "Classify the AKI."}, headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped_brokering"] == 1
    assert body["promoted"] >= 1
    assert store.get_ingest_case(extra["ingest_case_id"])["status"] == "ingested"
    assert store.get_ingest_case(extra["ingest_case_id"])["task_id"] is None


def test_an_all_brokering_upload_says_why_it_promoted_nothing(monkeypatch):
    A.fresh_store()
    _stub_llm(monkeypatch)
    admin_h = _admin_h(_store())
    _hs, upload_id = _upload_as("brokering")
    r = client.post(f"{API}/ingestion/uploads/{upload_id}/promote-all",
                    json={"question": "q"}, headers=admin_h)
    assert r.status_code == 409
    assert "brokering" in r.json()["detail"]


def test_a_legacy_null_purpose_case_still_promotes(monkeypatch):
    """NULL means a link minted before the column existed. It resolves to
    task_creation HERE and only here, so legacy links keep the behaviour they had
    — while everywhere the admin can see, NULL stays an unresolved work item."""
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as(None, org="Legacy Partner")
    cases = _cases(upload_id)
    assert cases and cases[0]["purpose"] is None
    r = client.post(f"{API}/ingestion/cases/{cases[0]['ingest_case_id']}/promote",
                    json={"question": "Classify the AKI."}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert asc_ingestion.effective_purpose(None) == "task_creation"
    assert asc_ingestion.is_brokering(None) is False


def test_the_refusal_never_reaches_a_provider(monkeypatch):
    """The message names brokering, which is the one place that word is allowed —
    behind require_admin. Assert the provider surface for the same upload says
    nothing about it."""
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    hs = store.ensure_health_system("Quiet General", contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    store.set_hs_portal_purpose(username, "brokering")
    c = TestClient(A.app, base_url="https://testserver")
    c.post(f"{API}/hs/login", json={"username": username, "password": "portal-pass-123456"})
    c.post(f"{API}/hs/uploads",
           files=[("files", ("bundle.zip", _bundle(), "application/zip"))])

    for path in (f"{API}/hs/me", f"{API}/hs/uploads"):
        body = c.get(path).content.decode().lower()
        for word in ("brokering", "broker", "purpose", "promote"):
            assert word not in body, f"{path} leaked {word!r}"


# ── specialty: a wrong label is worse than a missing one ─────────────────────
def test_a_case_with_no_specialty_is_refused_rather_than_defaulted(monkeypatch):
    """The conversion path falls back to a hardcoded specialty for a case that has
    none. A wrong specialty routes the case to the wrong physician pool and
    mislabels it in the export, invisibly and irreversibly once it ships."""
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as("task_creation")
    ic = _cases(upload_id)[0]
    with store._conn() as conn:
        conn.execute("UPDATE ingest_cases SET specialty = '' WHERE ingest_case_id = ?",
                     (ic["ingest_case_id"],))

    r = client.post(f"{API}/ingestion/cases/{ic['ingest_case_id']}/promote",
                    json={"question": "q"}, headers=admin_h)
    assert r.status_code == 409
    assert "Specialty not determined" in r.json()["detail"]
    assert store.get_ingest_case(ic["ingest_case_id"])["task_id"] is None


def test_portal_uploads_are_never_stamped_with_a_guessed_specialty():
    """A bundle that declares no specialty must land 'general' — honestly
    undetermined — not the neutral-looking literal of whichever specialty the
    pipeline happened to be written for."""
    A.fresh_store()
    store = _store()
    hs = store.ensure_health_system("Unlabelled Health", contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    c = TestClient(A.app, base_url="https://testserver")
    c.post(f"{API}/hs/login", json={"username": username, "password": "portal-pass-123456"})
    r = c.post(f"{API}/hs/uploads", files=[
        ("files", ("note.txt", b"Cardiology consult: chest pain, troponin 0.9 on "
                               b"2025-03-04, ECG with ST changes.", "text/plain"))])
    assert r.status_code == 200
    cases = store.list_ingest_cases(upload_id=r.json()["upload_id"])
    assert cases
    assert {c_["specialty"] for c_ in cases} == {"general"}, (
        "ingest guessed a specialty for a bundle that declared none")


# ── admin visibility ─────────────────────────────────────────────────────────
def test_brokering_gets_its_own_bucket_and_never_appears_in_ready_to_promote():
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    hs, brokering_upload = _upload_as("brokering", org="Both Ways Health")
    _hs2, task_upload = _upload_as("task_creation", org="Both Ways Health", patient="pt2")

    r = client.get(f"{API}/admin/health-systems/{hs['hs_id']}", headers=admin_h)
    assert r.status_code == 200, r.text
    buckets = r.json()["buckets"]
    ids = {name: {e["upload_id"] for e in rows} for name, rows in buckets.items()}
    assert brokering_upload in ids["brokering"]
    for name in ("ready_to_promote", "in_production", "needs_review", "needs_attention"):
        assert brokering_upload not in ids[name], (
            f"a brokering upload appeared in {name} — its own bucket exists so it "
            "can never sit next to a Promote button")
    assert task_upload in (ids["ready_to_promote"] | ids["needs_review"]
                           | ids["needs_attention"])


def test_every_admin_row_carries_the_chain_of_custody_and_a_purpose_chip():
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    hs, upload_id = _upload_as("task_creation")
    detail = client.get(f"{API}/admin/health-systems/{hs['hs_id']}",
                        headers=admin_h).json()
    rows = [e for rows in detail["buckets"].values() for e in rows]
    assert rows
    row = [e for e in rows if e["upload_id"] == upload_id][0]
    assert row["sha256"] and row["sha256_short"] and row["verified_at"]
    assert row["label"] == "task creation" and row["accent"] == "green"
    assert row["resolved"] is True


def test_an_unset_purpose_renders_as_a_work_item_not_a_default():
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    hs, upload_id = _upload_as(None, org="Legacy Health")
    detail = client.get(f"{API}/admin/health-systems/{hs['hs_id']}",
                        headers=admin_h).json()
    rows = [e for rows in detail["buckets"].values() for e in rows]
    row = [e for e in rows if e["upload_id"] == upload_id][0]
    assert row["resolved"] is False
    assert row["label"] == asc_ingestion.PURPOSE_UNSET_LABEL
    # Lime means "needs attention". Not pink — brokering and unresolved purposes
    # are business states, and pink means blocking/PHI/critical.
    assert row["accent"] == "lime"
    assert detail["portal_users"][0]["resolved"] is False


def test_an_admin_can_resolve_an_unset_purpose():
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as(None, org="Resolvable Health")
    r = client.post(f"{API}/admin/uploads/{upload_id}/purpose",
                    json={"purpose": "brokering"}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["cases_updated"] >= 1
    assert store.get_ingest_upload(upload_id)["purpose"] == "brokering"
    assert {c["purpose"] for c in store.list_ingest_cases(upload_id=upload_id)} == {"brokering"}


def test_provisioning_requires_a_purpose_and_rejects_anything_else():
    A.fresh_store()
    admin_h = _admin_h(_store())
    missing = client.post(f"{API}/admin/health-systems/provision",
                          json={"organization": "No Purpose Health",
                                "email": "it@example.org"}, headers=admin_h)
    assert missing.status_code == 422, "purpose must be required, not defaulted"
    bad = client.post(f"{API}/admin/health-systems/provision",
                      json={"organization": "Bad Purpose Health",
                            "email": "it@example.org", "purpose": "whatever"},
                      headers=admin_h)
    assert bad.status_code == 400


def test_both_buttons_take_the_same_code_path():
    """Same form, same endpoint, same body shape — one field differs, and nothing
    downstream of the mint does."""
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    out = {}
    for purpose, org in (("task_creation", "Alpha Regional Hospital"),
                         ("brokering", "Bravo Regional Hospital")):
        r = client.post(f"{API}/admin/health-systems/provision",
                        json={"organization": org, "email": "it@example.org",
                              "purpose": purpose}, headers=admin_h)
        assert r.status_code == 200, r.text
        out[purpose] = r.json()
    assert sorted(out["task_creation"]) == sorted(out["brokering"])
    for purpose in out:
        username = out[purpose]["username"]
        assert store.get_hs_portal_user(username)["purpose"] == purpose


# ── the rest of §4.2 ─────────────────────────────────────────────────────────
def test_every_non_terminal_status_maps_to_processing():
    """An unmapped status renders to a hospital as 'Needs attention' and generates
    support traffic from a completely healthy upload."""
    from routers import asclepius_provider as P

    for status in asc_ingestion._NON_TERMINAL_UPLOAD_STATUSES:
        assert P._HS_PORTAL_STATUS.get(status) in ("received", "processing"), status


def test_a_rejected_upload_emails_the_health_systems_contact():
    """The portal door had no failure loop at all: its sentinel link_id has no link
    row, so a rejected hospital upload emailed nobody — on the one door whose users
    we cannot support in real time."""
    from asclepius import ingest_notify

    A.fresh_store()
    store = _store()
    hs = store.ensure_health_system("Contactable Health", contact_email="itdesk@example.org")
    upload = store.insert_ingest_upload(
        link_id="hs-portal", partner_id=hs["hs_id"], filename="bad.zip",
        sha256="d", size_bytes=10, raw_path=None, source_ip=None)
    store.set_upload_health_system(upload["upload_id"], hs["hs_id"])
    fresh = store.get_ingest_upload(upload["upload_id"])
    email, name = ingest_notify._recipient_for(store, fresh)
    assert email == "itdesk@example.org"
    assert name == "Contactable Health"


def test_uploaded_filenames_are_sanitized_before_the_filesystem():
    """The stored name is echoed into a quoted Content-Disposition on the admin
    download path, so an uploader who controls it controls what the admin's
    browser saves."""
    from asclepius import uploads as asc_uploads
    from routers.asclepius_provider import _safe_upload_filename

    hostile = 'x"; filename="Q3-invoice.pdf'
    for fn in (_safe_upload_filename, asc_uploads._safe_name):
        got = fn(hostile)
        assert '"' not in got and ";" not in got and "/" not in got
    assert asc_uploads._safe_name("../../etc/passwd") == "etc_passwd" or \
        "/" not in asc_uploads._safe_name("../../etc/passwd")
    assert asc_uploads._safe_name("") == "upload.zip"
    assert asc_uploads._safe_name("réçu.zip").isascii()


def test_the_gate_holds_even_if_the_purpose_never_reached_the_case():
    """Purpose is copied onto cases at the end of ingest, and that copy is
    best-effort by design — it must never strand an upload, so a failure there is
    logged and swallowed. If the gate read only the case column, a swallowed
    failure would present as NULL and resolve to task_creation: fail-open on the
    one check whose whole job is to fail closed."""
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as("brokering")
    ic_id = _cases(upload_id)[0]["ingest_case_id"]

    # Exactly the state a swallowed propagation failure leaves behind.
    with store._conn() as conn:
        conn.execute("UPDATE ingest_cases SET purpose = NULL WHERE ingest_case_id = ?",
                     (ic_id,))
    assert store.get_ingest_case(ic_id)["purpose"] is None
    assert store.ingest_case_effective_purpose(ic_id) == "brokering"

    r = client.post(f"{API}/ingestion/cases/{ic_id}/promote",
                    json={"question": "q"}, headers=admin_h)
    assert r.status_code == 409 and "brokering" in r.json()["detail"]

    r2 = client.post(f"{API}/ingestion/uploads/{upload_id}/promote-all",
                     json={"question": "q"}, headers=admin_h)
    assert r2.status_code == 409 and "brokering" in r2.json()["detail"]
    assert store.get_ingest_case(ic_id)["task_id"] is None


# ── the cross-account session hijack (review finding #1) ─────────────────────
def _account(store, hs, purpose, tag):
    username = f"hs{tag}{A.uniq(6)}"
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    store.set_hs_portal_purpose(username, purpose)
    c = TestClient(A.app, base_url="https://testserver")
    assert c.post(f"{API}/hs/login", json={"username": username,
                                           "password": "portal-pass-123456"}).status_code == 200
    return c, username


def test_a_brokering_account_cannot_adopt_a_task_creation_session():
    """The failure this nearly shipped with.

    Purpose lives on the ACCOUNT, and the admin surface explicitly supports one
    organization holding one account of each kind. Sessions were keyed and
    authorized on the HEALTH SYSTEM, so a brokering account re-declaring the same
    bytes was handed the task-creation account's session — and the completed
    upload inherited its purpose. A brokering bundle stamped task_creation, filed
    under Ready to promote with a green chip, and promotable. Fail-open on the one
    invariant this release exists to hold."""
    import hashlib

    A.fresh_store()
    store = _store()
    hs = store.ensure_health_system("Two Ways General", contact_email="it@example.org")
    task_c, _task_user = _account(store, hs, "task_creation", "t")
    brok_c, brok_user = _account(store, hs, "brokering", "b")

    data = _bundle()
    digest = hashlib.sha256(data).hexdigest()
    decl = {"filename": "export.zip", "size": len(data), "sha256": digest}

    a = task_c.post(f"{API}/hs/uploads/sessions", json=decl)
    assert a.status_code == 200
    sid_task = a.json()["session_id"]
    chunk = a.json()["chunk_size"]
    first = data[:chunk]
    assert task_c.put(f"{API}/hs/uploads/sessions/{sid_task}/parts/1", content=first,
                      headers={"X-Chunk-SHA256": hashlib.sha256(first).hexdigest()}
                      ).status_code == 200

    # The brokering account declares the SAME bytes for the SAME organization.
    b = brok_c.post(f"{API}/hs/uploads/sessions", json=decl)
    assert b.status_code == 200
    sid_brok = b.json()["session_id"]
    assert sid_brok != sid_task, (
        "the brokering account was handed the task-creation account's session")
    assert b.json()["received_parts"] == [], (
        "the brokering account can see which parts the other account has sent")

    # It cannot reach the other session by id either.
    assert brok_c.get(f"{API}/hs/uploads/sessions/{sid_task}").status_code == 404
    assert brok_c.post(f"{API}/hs/uploads/sessions/{sid_task}/complete").status_code == 404
    assert brok_c.delete(f"{API}/hs/uploads/sessions/{sid_task}").status_code == 404

    # And its own upload is stamped brokering.
    for n, blob in enumerate([data[i:i + chunk] for i in range(0, len(data), chunk)], 1):
        assert brok_c.put(f"{API}/hs/uploads/sessions/{sid_brok}/parts/{n}", content=blob,
                          headers={"X-Chunk-SHA256": hashlib.sha256(blob).hexdigest()}
                          ).status_code == 200
    done = brok_c.post(f"{API}/hs/uploads/sessions/{sid_brok}/complete")
    assert done.status_code == 200, done.text
    assert store.get_ingest_upload(done.json()["upload_id"])["purpose"] == "brokering"


def test_the_promotion_preview_refuses_brokering_data(monkeypatch):
    """`/prepare` cannot create a task, so the invariant holds without it — but it
    renders the clinical case and sends it to a third-party inference provider to
    generate candidate answers. Doing that with brokering data is the activity the
    rule exists to prevent, whether or not a task comes out the other end."""
    A.fresh_store()
    called = []

    from routers import asclepius as R

    async def spy_candidates(prompt, **kw):
        called.append(prompt)
        return {"candidates": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
                "model": "cand", "intended_flawed_id": "B"}

    monkeypatch.setattr(R, "generate_candidates_ex", spy_candidates)
    admin_h = _admin_h(_store())
    _hs, upload_id = _upload_as("brokering")
    r = client.post(f"{API}/ingestion/uploads/{upload_id}/prepare",
                    json={"question": "q"}, headers=admin_h)
    assert r.status_code == 409
    assert not called, "brokering clinical data was sent to the inference provider"


# ── H1: the two doors must resolve purpose at the same instant ───────────────
def test_a_purpose_change_mid_session_lands_on_the_completed_upload():
    """A chunked session snapshotting purpose at DECLARE makes the two upload
    doors disagree about the same bytes.

    Sessions live 24 h and resume across that window, so an admin who mis-clicks
    the mint button — or a partnership that converts to brokering — leaves every
    in-flight session stamped with the old value. The bytes arrive AFTER the
    change, and there is no upload row yet, so the "not retroactive for uploads
    already received" rule does not cover this: it is not a received upload."""
    import hashlib

    A.fresh_store()
    store = _store()
    hs = store.ensure_health_system("Converting Health", contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    store.set_hs_portal_purpose(username, "task_creation")

    c = TestClient(A.app, base_url="https://testserver")
    assert c.post(f"{API}/hs/login", json={"username": username,
                                           "password": "portal-pass-123456"}).status_code == 200
    data = _bundle()
    decl = c.post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest()})
    assert decl.status_code == 200
    sid, chunk = decl.json()["session_id"], decl.json()["chunk_size"]

    # The admin corrects the mint mid-session, before any byte has been stored.
    admin_h = _admin_h(store)
    assert client.post(
        f"{API}/admin/health-systems/{hs['hs_id']}/accounts/{username}/purpose",
        json={"purpose": "brokering"}, headers=admin_h).status_code == 200

    for n, blob in enumerate([data[i:i + chunk] for i in range(0, len(data), chunk)], 1):
        assert c.put(f"{API}/hs/uploads/sessions/{sid}/parts/{n}", content=blob,
                     headers={"X-Chunk-SHA256": hashlib.sha256(blob).hexdigest()}
                     ).status_code == 200
    done = c.post(f"{API}/hs/uploads/sessions/{sid}/complete")
    assert done.status_code == 200, done.text

    upload = store.get_ingest_upload(done.json()["upload_id"])
    assert upload["purpose"] == "brokering", (
        "the chunked door stamped a stale purpose. The multipart door resolves "
        "live from the account, so the same bytes through the two doors would be "
        "recorded differently — and the stale one lands in Ready to promote.")


def test_both_doors_resolve_the_same_purpose_for_the_same_account():
    """Stated as the invariant rather than as one door's behaviour: whichever door
    a partner happens to use, the answer is the account's purpose right now."""
    import hashlib

    A.fresh_store()
    store = _store()
    hs = store.ensure_health_system("Two Door Health", contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    store.set_hs_portal_purpose(username, "brokering")
    c = TestClient(A.app, base_url="https://testserver")
    c.post(f"{API}/hs/login", json={"username": username, "password": "portal-pass-123456"})

    multipart = c.post(f"{API}/hs/uploads",
                       files=[("files", ("a.zip", _bundle("ptA"), "application/zip"))])
    assert multipart.status_code == 200

    data = _bundle("ptB")
    decl = c.post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest()}).json()
    for n, blob in enumerate(
            [data[i:i + decl["chunk_size"]]
             for i in range(0, len(data), decl["chunk_size"])], 1):
        c.put(f"{API}/hs/uploads/sessions/{decl['session_id']}/parts/{n}", content=blob,
              headers={"X-Chunk-SHA256": hashlib.sha256(blob).hexdigest()})
    chunked = c.post(f"{API}/hs/uploads/sessions/{decl['session_id']}/complete")
    assert chunked.status_code == 200

    got = {store.get_ingest_upload(multipart.json()["upload_id"])["purpose"],
           store.get_ingest_upload(chunked.json()["upload_id"])["purpose"]}
    assert got == {"brokering"}, f"the two doors disagreed: {got}"


# ── H3: the magic-link door, which is the ONLY table the PRD names ───────────
def _mint_link(admin_h, purpose, *, label="Link Partner"):
    body = {"partner_id": "p" + A.uniq(6), "partner_label": label,
            "specialty": "nephrology", "expires_hours": 24, "one_time": False}
    if purpose is not None:
        body["purpose"] = purpose
    return client.post(f"{API}/admin/upload-links", json=body, headers=admin_h)


def _upload_via_link(token, patient="ptL"):
    return client.post(f"{API}/partner/uploads?t={token}",
                       files=[("file", ("bundle.zip", _bundle(patient), "application/zip"))])


def test_a_brokering_link_stamps_its_uploads_brokering():
    """PRD §2.1 names exactly one table — ``ingest_upload_links`` — and the column
    existed with no writer and no reader: ``set_upload_link_purpose`` had zero
    callers and ``attach_upload_provenance`` had no link branch. So every
    link-door upload landed NULL, resolved to task_creation, and promoted. The
    requirement written first was met for neither door it names."""
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    r = _mint_link(admin_h, "brokering")
    assert r.status_code == 200, r.text
    assert r.json()["purpose"] == "brokering"

    up = _upload_via_link(r.json()["token"])
    assert up.status_code == 200, up.text
    upload = store.get_ingest_upload(up.json()["upload_id"])
    assert upload["purpose"] == "brokering"
    cases = store.list_ingest_cases(upload_id=upload["upload_id"])
    assert cases and {c["purpose"] for c in cases} == {"brokering"}


def test_a_brokering_link_case_cannot_be_promoted(monkeypatch):
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    token = _mint_link(admin_h, "brokering").json()["token"]
    upload_id = _upload_via_link(token).json()["upload_id"]
    cases = _cases(upload_id)
    assert cases
    r = client.post(f"{API}/ingestion/cases/{cases[0]['ingest_case_id']}/promote",
                    json={"question": "q"}, headers=admin_h)
    assert r.status_code == 409 and "brokering" in r.json()["detail"]


def test_a_task_creation_link_still_promotes(monkeypatch):
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    token = _mint_link(admin_h, "task_creation").json()["token"]
    upload_id = _upload_via_link(token).json()["upload_id"]
    cases = _cases(upload_id)
    assert cases
    r = client.post(f"{API}/ingestion/cases/{cases[0]['ingest_case_id']}/promote",
                    json={"question": "Classify the AKI."}, headers=admin_h)
    assert r.status_code == 200, r.text


def test_minting_a_link_requires_a_purpose():
    """Same rule as the portal door: a link with no purpose is a decision nobody
    made, and the gate reads NULL as task creation."""
    A.fresh_store()
    admin_h = _admin_h(_store())
    assert _mint_link(admin_h, None).status_code == 422
    assert _mint_link(admin_h, "whatever").status_code == 400


def test_the_link_token_gives_the_partner_no_hint_of_its_purpose():
    """Both variants minted for the same partner must be byte-indistinguishable to
    the recipient — the token, the URL and the upload response."""
    A.fresh_store()
    admin_h = _admin_h(_store())
    a = _mint_link(admin_h, "task_creation", label="Alpha Regional").json()
    b = _mint_link(admin_h, "brokering", label="Bravo Regional").json()
    assert len(a["token"]) == len(b["token"])
    assert a["upload_url"].split("?")[0] == b["upload_url"].split("?")[0]

    ra, rb = _upload_via_link(a["token"], "ptA"), _upload_via_link(b["token"], "ptB")
    assert ra.status_code == rb.status_code == 200
    ka, kb = sorted(ra.json()), sorted(rb.json())
    assert ka == kb
    for word in ("purpose", "brokering", "task_creation"):
        assert word not in ra.text.lower() and word not in rb.text.lower()


# ── M5: purpose corrections must not create a promotion path ────────────────
def test_brokering_cannot_be_relabelled_as_task_creation():
    """The invariant is 'brokering never becomes a task'. A one-call relabel from
    brokering to task_creation IS that transition, just spelled differently — and
    it would apply to cases a physician may already have seen. Resolving an UNSET
    purpose is the legitimate use; overturning a decided one is not."""
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as("brokering", org="Relabel Health")

    r = client.post(f"{API}/admin/uploads/{upload_id}/purpose",
                    json={"purpose": "task_creation"}, headers=admin_h)
    assert r.status_code == 409
    assert store.get_ingest_upload(upload_id)["purpose"] == "brokering"
    assert {c["purpose"] for c in store.list_ingest_cases(upload_id=upload_id)} == {"brokering"}


def test_an_unset_purpose_can_still_be_resolved_either_way():
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    for purpose in ("task_creation", "brokering"):
        _hs, upload_id = _upload_as(None, org=f"Unset {purpose}")
        r = client.post(f"{API}/admin/uploads/{upload_id}/purpose",
                        json={"purpose": purpose}, headers=admin_h)
        assert r.status_code == 200, r.text
        assert store.get_ingest_upload(upload_id)["purpose"] == purpose


def test_task_creation_can_be_downgraded_to_brokering():
    """The safe direction stays open: it removes a promotion path, never adds one."""
    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    _hs, upload_id = _upload_as("task_creation", org="Downgrade Health")
    r = client.post(f"{API}/admin/uploads/{upload_id}/purpose",
                    json={"purpose": "brokering"}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert store.get_ingest_upload(upload_id)["purpose"] == "brokering"


def test_an_account_that_has_sent_brokering_data_cannot_be_flipped_to_task_creation():
    """Resolving purpose LIVE at completion is what makes the two doors agree, but
    it also means flipping an account from brokering to task creation converts
    data that is already in flight or already received. Correcting a fresh
    mis-click is legitimate; converting a partner's brokering history is the
    invariant failing by another route."""
    import hashlib

    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    hs = store.ensure_health_system("Flip Health", contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    store.set_hs_portal_purpose(username, "brokering")

    path = (f"{API}/admin/health-systems/{hs['hs_id']}/accounts/{username}/purpose")
    # Nothing has arrived yet — a genuine mis-click, and correctable.
    assert client.post(path, json={"purpose": "task_creation"},
                       headers=admin_h).status_code == 200
    assert client.post(path, json={"purpose": "brokering"},
                       headers=admin_h).status_code == 200

    # Now the partner sends something under the brokering mint.
    c = TestClient(A.app, base_url="https://testserver")
    c.post(f"{API}/hs/login", json={"username": username, "password": "portal-pass-123456"})
    assert c.post(f"{API}/hs/uploads",
                  files=[("files", ("b.zip", _bundle(), "application/zip"))]
                  ).status_code == 200

    r = client.post(path, json={"purpose": "task_creation"}, headers=admin_h)
    assert r.status_code == 409, r.text
    assert store.get_hs_portal_user(username)["purpose"] == "brokering"


def test_an_in_flight_brokering_session_also_blocks_the_flip():
    """The bytes have not landed yet, which is exactly why it matters: they arrive
    AFTER the change and would be resolved with the new value."""
    import hashlib

    A.fresh_store()
    store = _store()
    admin_h = _admin_h(store)
    hs = store.ensure_health_system("In Flight Health", contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    store.set_hs_portal_purpose(username, "brokering")
    c = TestClient(A.app, base_url="https://testserver")
    c.post(f"{API}/hs/login", json={"username": username, "password": "portal-pass-123456"})
    data = _bundle()
    assert c.post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest()}).status_code == 200

    r = client.post(
        f"{API}/admin/health-systems/{hs['hs_id']}/accounts/{username}/purpose",
        json={"purpose": "task_creation"}, headers=admin_h)
    assert r.status_code == 409


# ── LOW: the specialty guard was unreachable ────────────────────────────────
def test_an_undetermined_specialty_blocks_promotion(monkeypatch):
    """Ingest never leaves specialty empty — it writes ``general``, which means
    "nothing declared one". So a guard testing for an EMPTY string could never
    fire, and the case it was written to stop went through: a case routed to a
    generic physician pool and exported under a label nobody chose."""
    A.fresh_store()
    _stub_llm(monkeypatch)
    store = _store()
    admin_h = _admin_h(store)
    hs = store.ensure_health_system("Unlabelled Health", contact_email="it@example.org")
    username = "hs" + A.uniq(8)
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="it@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    store.set_hs_portal_purpose(username, "task_creation")
    c = TestClient(A.app, base_url="https://testserver")
    c.post(f"{API}/hs/login", json={"username": username, "password": "portal-pass-123456"})
    # A real bundle that simply declares no specialty — the common case, since
    # specialty is a property of the data and is not asked of hospital IT.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"patient_key": "ptU"}))
        z.writestr("labs.csv",
                   "patient_key,panel,analyte,value,unit,collected_at\n"
                   "ptU,BMP,Creatinine,2.4,mg/dL,2025-03-08\n"
                   "ptU,BMP,Creatinine,1.1,mg/dL,2025-03-01\n")
        z.writestr("note.txt", "Progress note: AKI, creatinine rising since "
                               "3/1/2025, improving by 2025-03-09.")
    r = c.post(f"{API}/hs/uploads",
               files=[("files", ("b.zip", buf.getvalue(), "application/zip"))])
    assert r.status_code == 200
    upload_id = r.json()["upload_id"]
    ic = _cases(upload_id)
    assert ic and ic[0]["specialty"] == "general"

    blocked = client.post(f"{API}/ingestion/cases/{ic[0]['ingest_case_id']}/promote",
                          json={"question": "q"}, headers=admin_h)
    assert blocked.status_code == 409
    assert "Specialty not determined" in blocked.json()["detail"]

    # The admin resolves it through the existing endpoint, and promotion proceeds.
    assert client.post(f"{API}/admin/uploads/{upload_id}/specialty",
                       json={"specialty": "cardiology"}, headers=admin_h
                       ).status_code == 200
    ok = client.post(f"{API}/ingestion/cases/{ic[0]['ingest_case_id']}/promote",
                     json={"question": "Classify the chest pain."}, headers=admin_h)
    assert ok.status_code == 200, ok.text
    assert store.get_task(ok.json()["task_id"])["specialty"] == "cardiology"
