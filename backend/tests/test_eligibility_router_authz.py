"""Authorization tests for the TEAM eligibility router.

These pin the three holes closed in `routers/eligibility.py` on launch night.
Every test here fails against the pre-fix router:

  1. `GET /api/eligibility-batches/{id}` had no auth dependency at all, so
     anyone holding a batch id got the serialized batch (patient names and
     eligibility verdicts) back.
  2. `_assert_patient_access` skipped its tenant check when `staff` was None,
     so a caller with no token at all was granted access to any patient.
  3. `stream_batch` authenticated but applied no tenant filter, so staff at
     health system A could stream health system B's batch.

The positive cases matter as much as the refusals: the clinician console
(frontend/doctor.html) is the only caller of these routes and it must keep
working, so each refusal is paired with the same request made by the staff
member who legitimately owns the record.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("UPLOAD_DIR", "/tmp/elysium-eligibility-tests")

from eligibility import pipeline as elig_pipeline  # noqa: E402
from eligibility import store as elig_store  # noqa: E402
from main import app  # noqa: E402
from tests._role_auth import tenant_token  # noqa: E402

HS_A = "hs_alpha"
HS_B = "hs_beta"


@pytest.fixture(autouse=True)
def _clean_state():
    elig_store.ELIGIBILITY_CHECKS.clear()
    elig_store.ELIGIBILITY_DOCS.clear()
    elig_store.BATCHES.clear()
    elig_store.AUDIT_LOG.clear()
    elig_store._RATE_BUCKETS.clear()  # type: ignore[attr-defined]
    app.state.patient_store.clear()
    yield


@pytest.fixture(autouse=True)
def _event_loop():
    """Keep a live loop available for the asyncio.Queue a batch record holds."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def client():
    return TestClient(app)


def _headers(health_system_id: str) -> Dict[str, str]:
    tok = tenant_token(email=f"staff@{health_system_id}.example", health_system_id=health_system_id)
    return {"Authorization": f"Bearer {tok}"}


def _seed_patient(patient_id: str, health_system_id: str) -> Dict[str, Any]:
    """Put one patient straight into the store, owned by one health system."""
    rec = {
        "name": "Margaret O'Sullivan",
        "health_system_id": health_system_id,
        "pipeline_type": "pre_op",
        "eligibility_status": "PENDING",
        "relevant_files": [],
        "structured_data": {
            "patient_name": "Margaret O'Sullivan",
            "procedure_name": "TKR",
            "pre_op_instructions": "STOP eating solid food at midnight.",
            "post_op_instructions": "Keep the dressing dry for 48 hours.",
        },
    }
    app.state.patient_store[patient_id] = rec
    return rec


def _seed_batch(batch_id: str, health_system_id: str) -> Dict[str, Any]:
    """A finished batch record, shaped the way create_eligibility_batch leaves it."""
    rec: Dict[str, Any] = {
        "id": batch_id,
        "created_at": "2026-09-03T00:00:00Z",
        "updated_at": "2026-09-03T00:00:00Z",
        "actor": f"tenant:staff@{health_system_id}.example",
        "health_system_id": health_system_id,
        "status": "DONE",
        "created": [{"patient_id": "p1", "name": "Margaret O'Sullivan"}],
        "needs_review": [],
        "errors": [],
        "queue": elig_store.new_check_queue(),
        "ring": elig_store.ring_buffer(),
    }
    # A terminal event on the queue so the SSE generator returns instead of
    # sitting on its 15 second heartbeat timeout.
    rec["queue"].put_nowait({"event": "done", "data": {"created": 1}})
    elig_store.save_batch(batch_id, rec)
    return rec


# ─── Hole 1: unauthenticated batch read ─────────────────────────────────────
def test_get_batch_refuses_anonymous(client):
    """The whole finding: no token, no batch. Pre-fix this returned 200."""
    _seed_batch("batch-a", HS_A)
    r = client.get("/api/eligibility-batches/batch-a")
    assert r.status_code == 401, r.text


def test_get_batch_allows_the_owning_tenant(client):
    _seed_batch("batch-a", HS_A)
    r = client.get("/api/eligibility-batches/batch-a", headers=_headers(HS_A))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "batch-a"
    # The serializer still drops the non-JSON plumbing.
    assert "queue" not in body and "ring" not in body


def test_get_batch_refuses_another_tenant(client):
    _seed_batch("batch-a", HS_A)
    r = client.get("/api/eligibility-batches/batch-a", headers=_headers(HS_B))
    assert r.status_code == 404, r.text


def test_get_batch_does_not_accept_a_query_string_token(client):
    """The JSON poll must not inherit the ?token= surface its SSE sibling needs.

    fetch() can set an Authorization header, so there is no reason for this
    route to accept a 7 day staff JWT that lands in every access log.
    """
    _seed_batch("batch-a", HS_A)
    tok = tenant_token(email=f"staff@{HS_A}.example", health_system_id=HS_A)
    r = client.get(f"/api/eligibility-batches/batch-a?token={tok}")
    assert r.status_code == 401, r.text


def test_get_batch_hides_whether_an_unknown_batch_exists(client):
    """Auth is checked before existence, so an anonymous prober learns nothing."""
    r = client.get("/api/eligibility-batches/never-existed")
    assert r.status_code == 401, r.text


# ─── Hole 3: cross-tenant batch stream ──────────────────────────────────────
def test_stream_batch_refuses_another_tenant(client):
    """Pre-fix the route authenticated and then streamed anyone's batch."""
    _seed_batch("batch-a", HS_A)
    r = client.get("/api/eligibility-batches/batch-a/stream", headers=_headers(HS_B))
    assert r.status_code == 404, r.text


def test_stream_batch_refuses_another_tenant_via_query_token(client):
    """The EventSource path gets the same tenant filter as the header path."""
    _seed_batch("batch-a", HS_A)
    tok = tenant_token(email=f"staff@{HS_B}.example", health_system_id=HS_B)
    r = client.get(f"/api/eligibility-batches/batch-a/stream?token={tok}")
    assert r.status_code == 404, r.text


def test_stream_batch_refuses_anonymous(client):
    _seed_batch("batch-a", HS_A)
    r = client.get("/api/eligibility-batches/batch-a/stream")
    assert r.status_code == 401, r.text


def test_stream_batch_still_serves_the_owning_tenant(client):
    """The console's real flow: EventSource with ?token= for its own batch."""
    _seed_batch("batch-a", HS_A)
    tok = tenant_token(email=f"staff@{HS_A}.example", health_system_id=HS_A)
    r = client.get(f"/api/eligibility-batches/batch-a/stream?token={tok}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: done" in r.text


def test_created_batch_is_stamped_with_the_creating_tenant(client, monkeypatch):
    """End to end: the tenant filter has something to filter on.

    Before the fix the batch record carried no health_system_id at all, which
    is why the stream route commented itself 'no tenant filter on batch'.
    """

    async def _noop_run_batch(batch_id, payloads, hs_id, actor, app_):
        return None

    monkeypatch.setattr(elig_pipeline, "run_batch", _noop_run_batch)

    files = [("files", ("a.x12", b"ISA*00*", "application/octet-stream"))]
    created = client.post("/api/eligibility-batches", files=files, headers=_headers(HS_A))
    assert created.status_code == 202, created.text
    batch_id = created.json()["id"]

    assert elig_store.get_batch(batch_id)["health_system_id"] == HS_A
    assert client.get(f"/api/eligibility-batches/{batch_id}", headers=_headers(HS_A)).status_code == 200
    assert client.get(f"/api/eligibility-batches/{batch_id}", headers=_headers(HS_B)).status_code == 404


# ─── Hole 2: _assert_patient_access failed open ─────────────────────────────
# One row per route that reaches the helper. Pre-fix every one of these
# returned the patient's record to a caller holding no token whatsoever.
ANONYMOUS_PHI_READS = [
    ("GET", "/api/patient/{pid}/preop-notes", None),
    ("GET", "/api/patient/{pid}/postop-notes", None),
    ("GET", "/api/patient/{pid}/eligibility-documents", None),
]

ANONYMOUS_PHI_WRITES = [
    ("POST", "/api/patient/{pid}/preop-notes/confirm", {"text": "attacker supplied"}),
    ("POST", "/api/patient/{pid}/postop-notes/confirm", {"text": "attacker supplied"}),
]


@pytest.mark.parametrize("method,path,body", ANONYMOUS_PHI_READS + ANONYMOUS_PHI_WRITES)
def test_patient_routes_refuse_a_missing_staff_context(client, method, path, body):
    _seed_patient("pat-a", HS_A)
    r = client.request(method, path.format(pid="pat-a"), json=body)
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}: {r.text}"


@pytest.mark.parametrize("method,path,body", ANONYMOUS_PHI_READS + ANONYMOUS_PHI_WRITES)
def test_patient_routes_refuse_another_tenant(client, method, path, body):
    _seed_patient("pat-a", HS_A)
    r = client.request(method, path.format(pid="pat-a"), json=body, headers=_headers(HS_B))
    assert r.status_code == 404, f"{method} {path} -> {r.status_code}: {r.text}"


@pytest.mark.parametrize("method,path,body", ANONYMOUS_PHI_READS)
def test_patient_reads_still_serve_the_owning_tenant(client, method, path, body):
    _seed_patient("pat-a", HS_A)
    r = client.request(method, path.format(pid="pat-a"), json=body, headers=_headers(HS_A))
    assert r.status_code == 200, f"{method} {path} -> {r.status_code}: {r.text}"


def test_anonymous_cannot_probe_which_patient_ids_exist(client):
    """Auth before existence: a real and a fake id must answer identically."""
    _seed_patient("pat-a", HS_A)
    real = client.get("/api/patient/pat-a/preop-notes")
    fake = client.get("/api/patient/pat-nope/preop-notes")
    assert real.status_code == fake.status_code == 401
    assert real.json() == fake.json()


def test_document_upload_refuses_a_missing_staff_context(client):
    _seed_patient("pat-a", HS_A)
    files = {"file": ("test.x12", b"ISA*00*", "application/octet-stream")}
    r = client.post("/api/eligibility-documents", data={"patientId": "pat-a"}, files=files)
    assert r.status_code == 401, r.text


def test_check_read_refuses_a_missing_staff_context(client):
    _seed_patient("pat-a", HS_A)
    elig_store.save_check("chk-a", {"id": "chk-a", "patient_id": "pat-a", "status": "DONE"})
    assert client.get("/api/eligibility-checks/chk-a").status_code == 401
    assert client.get("/api/eligibility-checks/chk-a", headers=_headers(HS_B)).status_code == 404
    assert client.get("/api/eligibility-checks/chk-a", headers=_headers(HS_A)).status_code == 200


def test_check_stream_refuses_a_missing_staff_context(client):
    _seed_patient("pat-a", HS_A)
    elig_store.save_check(
        "chk-a",
        {
            "id": "chk-a",
            "patient_id": "pat-a",
            "status": "DONE",
            "queue": elig_store.new_check_queue(),
            "ring": elig_store.ring_buffer(),
        },
    )
    assert client.get("/api/eligibility-checks/chk-a/stream").status_code == 401
    tok = tenant_token(email=f"staff@{HS_B}.example", health_system_id=HS_B)
    assert client.get(f"/api/eligibility-checks/chk-a/stream?token={tok}").status_code == 404
