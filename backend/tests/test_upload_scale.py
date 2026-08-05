"""Chunked upload at scale — PRD-I §1.

Every test here goes through the real HTTP routes a hospital would call. A test
that builds its state by calling the store directly proves the store works, which
was never in doubt; what is in doubt is whether the ROUTE a partner reaches does
the right thing, and that is only visible from outside.
"""

from __future__ import annotations

import hashlib
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
from asclepius import uploads as asc_uploads  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

API = "/api/asclepius"


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch, tmp_path):
    """A 1 MB chunk keeps the fixtures small while exercising the real multi-part
    path — the code cannot tell that 3 parts of 1 MB is not 300 parts of 16 MB."""
    monkeypatch.setenv("ASCLEPIUS_UPLOAD_CHUNK_BYTES", str(1024 * 1024))
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")


@pytest.fixture
def client():
    """https:// on purpose — the session cookie is unconditionally ``Secure``, and
    a conforming cookie jar will not return it over plain http, so an http client
    would silently exercise a session-less portal and prove nothing."""
    with TestClient(A.app, base_url="https://testserver") as c:
        yield c


def _portal(client, store, *, org="Mass General Hospital", purpose="task_creation"):
    """Provision a health system + portal account through the store, then sign in
    through the real login route so the caller holds a real session cookie."""
    hs = store.ensure_health_system(org, contact_email="data@example.org")
    username = f"hs{A.uniq(6)}"
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password="portal-pass-123456", email="data@example.org")
    store.set_hs_portal_password(username, "portal-pass-123456", must_reset=False)
    if purpose:
        store.set_hs_portal_purpose(username, purpose)
    r = client.post(f"{API}/hs/login", json={"username": username,
                                             "password": "portal-pass-123456"})
    assert r.status_code == 200, r.text
    return hs, username


def _bundle(n_bytes: int = 0) -> bytes:
    """A real zip with parseable clinical content, optionally padded to a size."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"specialty": "nephrology",
                                                "patient_key": "pt1"}))
        z.writestr("labs.csv",
                   "patient_key,panel,analyte,value,unit,collected_at\n"
                   "pt1,BMP,Creatinine,2.4,mg/dL,2025-03-08\n"
                   "pt1,BMP,Creatinine,1.1,mg/dL,2025-03-01\n")
        z.writestr("note.txt", "Progress nephrology: AKI, creatinine rising since "
                               "3/1/2025, improving by 2025-03-09.")
    data = buf.getvalue()
    if n_bytes and len(data) < n_bytes:
        # Pad by appending an incompressible member so the archive really is the
        # requested size on the wire.
        buf = io.BytesIO(data)
        with zipfile.ZipFile(buf, "a", zipfile.ZIP_STORED) as z:
            z.writestr("filler.bin", os.urandom(n_bytes - len(data) - 200))
        data = buf.getvalue()
    return data


def _declare(client, data: bytes, filename="bundle.zip"):
    r = client.post(f"{API}/hs/uploads/sessions", json={
        "filename": filename, "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "content_type": "application/zip"})
    assert r.status_code == 200, r.text
    return r.json()


def _parts_of(data: bytes, chunk: int):
    for i in range(0, len(data), chunk):
        yield data[i:i + chunk]


def _put_part(client, sid, n, blob, *, sha=None):
    return client.put(f"{API}/hs/uploads/sessions/{sid}/parts/{n}", content=blob,
                      headers={"X-Chunk-SHA256": sha or hashlib.sha256(blob).hexdigest()})


def _upload_all(client, data: bytes, session):
    chunk = session["chunk_size"]
    for n, blob in enumerate(_parts_of(data, chunk), start=1):
        assert _put_part(client, session["session_id"], n, blob).status_code == 200
    return client.post(f"{API}/hs/uploads/sessions/{session['session_id']}/complete")


# ── the handshake ────────────────────────────────────────────────────────────
def test_multi_part_upload_assembles_and_verifies(client):
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(3 * 1024 * 1024)          # 3 parts at a 1 MB chunk
    session = _declare(client, data)
    assert session["part_count"] >= 3
    assert session["received_parts"] == [] and session["complete"] is False

    r = _upload_all(client, data, session)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    assert body["total_bytes"] == len(data)

    upload = store.get_ingest_upload(body["upload_id"])
    # The chain-of-custody triple: what we hold, how much of it, and when we
    # proved it. Two numbers without the third are not a claim.
    assert upload["sha256"] == hashlib.sha256(data).hexdigest()
    assert upload["size_bytes"] == len(data)
    assert upload["verified_at"]


def test_a_corrupt_chunk_is_rejected_by_its_own_sha256(client):
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    session = _declare(client, data)
    chunk = session["chunk_size"]
    first = data[:chunk]
    flipped = bytes([first[0] ^ 0xFF]) + first[1:]

    r = _put_part(client, session["session_id"], 1, flipped,
                  sha=hashlib.sha256(first).hexdigest())
    assert r.status_code == 400
    assert "sha256" in r.json()["detail"]

    state = client.get(f"{API}/hs/uploads/sessions/{session['session_id']}").json()
    assert state["received_parts"] == []      # a rejected chunk is not stored


def test_a_part_with_no_digest_is_refused(client):
    """Fail closed: an unverified chunk is the one the whole-file digest would
    reject after the partner had already spent bandwidth on every other part."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    session = _declare(client, data)
    r = client.put(f"{API}/hs/uploads/sessions/{session['session_id']}/parts/1",
                   content=data[:session["chunk_size"]])
    assert r.status_code == 400 and "X-Chunk-SHA256" in r.json()["detail"]


# ── idempotency + resume ─────────────────────────────────────────────────────
def test_redeclaring_the_same_file_returns_the_same_session(client):
    """'The contact refreshed the tab at 3.2 GB' must be a non-event."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    first = _declare(client, data)
    again = _declare(client, data)
    assert again["session_id"] == first["session_id"]
    assert again["part_count"] == first["part_count"]


def test_resume_after_an_interrupted_part_uploads_only_the_missing_part(client):
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(3 * 1024 * 1024)
    session = _declare(client, data)
    chunk = session["chunk_size"]
    blobs = list(_parts_of(data, chunk))

    # parts 1 and 3 land; part 2 is the one the connection dropped on
    assert _put_part(client, session["session_id"], 1, blobs[0]).status_code == 200
    assert _put_part(client, session["session_id"], 3, blobs[2]).status_code == 200

    resumed = _declare(client, data)          # client comes back, re-declares
    assert resumed["session_id"] == session["session_id"]
    assert resumed["received_parts"] == [1, 3]
    assert resumed["missing_parts"] == [2]

    # completing now is refused — nothing partial may become an upload
    early = client.post(f"{API}/hs/uploads/sessions/{session['session_id']}/complete")
    assert early.status_code == 409
    assert store.count_ingest_uploads() == 0

    assert _put_part(client, session["session_id"], 2, blobs[1]).status_code == 200
    done = client.post(f"{API}/hs/uploads/sessions/{session['session_id']}/complete")
    assert done.status_code == 200
    assert done.json()["sha256"] == hashlib.sha256(data).hexdigest()


# ── the whole-file digest is the only real proof ─────────────────────────────
def test_internally_consistent_parts_that_are_the_wrong_file_fail_the_whole_digest(client):
    """The point of the whole-file digest.

    Every part here passes its own sha256 and every part is exactly the length it
    should be — the client simply sent different bytes and computed honest digests
    over them. Per-chunk digests and byte counts both say 'fine'. Only the digest
    over the assembled whole catches it, and it must catch it BEFORE an upload row
    exists."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    session = _declare(client, data)          # declares sha256 of `data`
    chunk = session["chunk_size"]

    tampered = bytearray(data)
    tampered[-50] ^= 0xFF                     # same length, different content
    for n, blob in enumerate(_parts_of(bytes(tampered), chunk), start=1):
        assert _put_part(client, session["session_id"], n, blob).status_code == 200

    r = client.post(f"{API}/hs/uploads/sessions/{session['session_id']}/complete")
    assert r.status_code == 409
    assert "checksum" in r.json()["detail"]
    # Nothing was created: no row, and no orphaned blob left behind.
    assert store.count_ingest_uploads() == 0
    blobs = list((Path(asc_ingestion.quarantine_root())).glob("*.zip.enc"))
    assert blobs == []


def test_a_short_part_is_refused_before_it_is_stored(client):
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    session = _declare(client, data)
    truncated = data[:session["chunk_size"] - 1000]
    r = _put_part(client, session["session_id"], 1, truncated)
    assert r.status_code == 400 and "bytes" in r.json()["detail"]


# ── the invariant ────────────────────────────────────────────────────────────
def test_an_unverified_session_is_invisible_to_the_application(client):
    """An assembled file with no verified row is invisible. Assert it from the
    partner's own uploads list, which is the surface that would betray it."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    session = _declare(client, data)
    _put_part(client, session["session_id"], 1, data[:session["chunk_size"]])

    assert client.get(f"{API}/hs/uploads").json()["uploads"] == []
    assert store.count_ingest_uploads() == 0


def test_another_health_system_cannot_touch_the_session(client):
    store = A.fresh_store()
    _portal(client, store, org="Mass General Hospital")
    data = _bundle(1024 * 1024)
    session = _declare(client, data)
    # a second hospital signs in on the same client, replacing the cookie
    _portal(client, store, org="Cleveland Clinic")
    for call in (
        lambda: client.get(f"{API}/hs/uploads/sessions/{session['session_id']}"),
        lambda: _put_part(client, session["session_id"], 1, data),
        lambda: client.post(f"{API}/hs/uploads/sessions/{session['session_id']}/complete"),
    ):
        r = call()
        assert r.status_code == 404, r.text


# ── end to end ───────────────────────────────────────────────────────────────
def test_a_chunked_bundle_reaches_an_ingested_case(client):
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    session = _declare(client, data)
    r = _upload_all(client, data, session)
    assert r.status_code == 200, r.text
    upload_id = r.json()["upload_id"]

    upload = store.get_ingest_upload(upload_id)
    assert upload["status"] in ("ingested", "needs_review", "quarantined"), upload
    cases = store.list_ingest_cases(upload_id=upload_id)
    assert cases, "the chunked bundle produced no cases"


def test_the_partner_sees_the_chunked_upload_in_their_history(client):
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(1024 * 1024)
    session = _declare(client, data)
    assert _upload_all(client, data, session).status_code == 200
    rows = client.get(f"{API}/hs/uploads").json()["uploads"]
    assert len(rows) == 1
    assert rows[0]["total_bytes"] == len(data)
    assert rows[0]["status"] in ("received", "processing", "accepted")


# ── memory stays flat ────────────────────────────────────────────────────────
def _many_entry_bundle(total_bytes: int, entry_bytes: int = 1024 * 1024) -> bytes:
    """A bundle of many modest entries — the shape a real hospital export has, and
    the shape that isolates ASSEMBLY memory from per-entry memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.json", json.dumps({"specialty": "nephrology",
                                                "patient_key": "pt1"}))
        z.writestr("note.txt", "Progress nephrology: AKI, creatinine rising.")
        written = 0
        i = 0
        while written < total_bytes:
            z.writestr(f"filler-{i:03d}.bin", os.urandom(entry_bytes))
            written += entry_bytes
            i += 1
    return buf.getvalue()


def test_memory_stays_flat_across_a_large_upload(client):
    """Assemble a ~24 MB bundle and measure ONLY the server-side work.

    The parts are uploaded first, untraced, because the tracer cannot separate the
    test's own copy of the payload from the server's — a test that holds 24 MB in
    a local variable cannot honestly assert the server did not. What is traced is
    the ``complete`` call: assembly, whole-file digest, framed encryption, and the
    full ingest pipeline that runs as its background task.

    The bundle is built from 1 MB entries so no single entry legitimately needs a
    large allocation, which is what makes the ceiling meaningful: anything near
    24 MB here means something buffered the whole bundle."""
    import tracemalloc

    store = A.fresh_store()
    _portal(client, store)
    data = _many_entry_bundle(24 * 1024 * 1024)
    session = _declare(client, data)
    chunk = session["chunk_size"]
    for n, blob in enumerate(_parts_of(data, chunk), start=1):
        assert _put_part(client, session["session_id"], n, blob).status_code == 200

    tracemalloc.start()
    try:
        r = client.post(f"{API}/hs/uploads/sessions/{session['session_id']}/complete")
        assert r.status_code == 200, r.text
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # A few 1 MB units in flight (part, its decrypted copy, a frame, one entry),
    # not the 24 MB whole. A whole-file implementation peaks above 24 MB here, and
    # above 48 MB once its encrypted copy exists alongside it.
    assert peak < 12 * 1024 * 1024, f"peak allocation {peak} suggests whole-file buffering"
    assert store.get_ingest_upload(r.json()["upload_id"])["sha256"] == \
        hashlib.sha256(data).hexdigest()


def test_raw_blob_round_trips_through_the_framed_container(tmp_path, monkeypatch):
    """The framed container is what makes multi-GB possible at all — assert it
    reproduces the exact bytes, and that ``iter_raw`` never yields the whole file
    as one allocation."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    payload = os.urandom(5 * 1024 * 1024)
    path = asc_ingestion.store_raw_stream("upl-framed", (payload[i:i + 1024 * 1024]
                                                         for i in range(0, len(payload), 1024 * 1024)))
    assert asc_ingestion.load_raw(path) == payload
    chunks = list(asc_ingestion.iter_raw(path, chunk_size=1024 * 1024))
    assert b"".join(chunks) == payload
    assert max(len(c) for c in chunks) <= 1024 * 1024


def test_legacy_single_blob_still_reads(tmp_path, monkeypatch):
    """Every upload written before PRD-I is a single ``encrypt_bytes`` blob. The
    reader must keep handling them or the admin download path breaks for history."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    payload = b"PK\x03\x04legacy bundle bytes"
    path = asc_ingestion.store_raw("upl-legacy", payload)
    assert asc_ingestion.load_raw(path) == payload
    assert b"".join(asc_ingestion.iter_raw(path)) == payload


# ── archive safety at size ───────────────────────────────────────────────────
def _zip_bomb(entry_bytes: int = 40 * 1024 * 1024) -> bytes:
    """A member of highly-compressible zeros: tiny on the wire, large decompressed."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("bomb.txt", b"\0" * entry_bytes)
    return buf.getvalue()


def test_zip_bomb_aborts_mid_extraction(monkeypatch, tmp_path):
    """Header-declared sizes are attacker-controlled and are used for nothing.
    The entry is abandoned on bytes ACTUALLY produced, mid-write."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_ENTRY_BYTES", str(4 * 1024 * 1024))
    bundle = _zip_bomb()
    out = asc_ingestion.unpack_bundle(bundle)
    bomb = [e for e in out["entries"] if e["name"] == "bomb.txt"][0]
    assert bomb["kind"] == "rejected"
    assert "too large" in bomb["reason"] or "ratio" in bomb["reason"]
    assert "data" not in bomb          # nothing was retained


def test_compression_ratio_cap_rejects_the_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_ENTRY_BYTES", str(512 * 1024 * 1024))
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_RATIO", "10")
    out = asc_ingestion.unpack_bundle(_zip_bomb(8 * 1024 * 1024))
    bomb = [e for e in out["entries"] if e["name"] == "bomb.txt"][0]
    assert bomb["kind"] == "rejected" and "ratio" in bomb["reason"]


def test_total_output_budget_condemns_the_whole_bundle(monkeypatch, tmp_path):
    """A single entry over its own cap is one bad file. Blowing the whole-archive
    budget means the ARCHIVE is a bomb, and the bundle is refused outright."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_UNCOMPRESSED", str(1024 * 1024))
    monkeypatch.setenv("ASCLEPIUS_INGEST_TOTAL_RATIO", "2")
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_ENTRY_BYTES", str(64 * 1024 * 1024))
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_RATIO", "100000")
    with pytest.raises(asc_ingestion.BundleRejected) as exc:
        asc_ingestion.unpack_bundle(_zip_bomb(16 * 1024 * 1024))
    assert "budget" in str(exc.value)


def test_zip_slip_and_nested_archives_are_rejected_per_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    buf = io.BytesIO()
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("deep.txt", "nested")
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../etc/passwd", "root:x:0:0")
        z.writestr("/abs/path.txt", "absolute")
        z.writestr("inner.zip", inner.getvalue())
        z.writestr("note.txt", "legitimate clinical note")
    out = asc_ingestion.unpack_bundle(buf.getvalue())
    by_name = {e["name"]: e for e in out["entries"]}
    assert by_name["../../etc/passwd"]["reason"] == "path traversal"
    assert by_name["/abs/path.txt"]["reason"] == "path traversal"
    assert "nested archive" in by_name["inner.zip"]["reason"]
    # The bundle survives — one bad entry does not condemn a hospital's whole file.
    assert by_name["note.txt"]["kind"] == "note_text"


def test_the_reaper_deletes_unverified_parts(client, monkeypatch):
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(1024 * 1024)
    session = _declare(client, data)
    _put_part(client, session["session_id"], 1, data[:session["chunk_size"]])
    parts_dir = Path(store.get_upload_session(session["session_id"])["storage_dir"])
    assert list(parts_dir.iterdir())

    monkeypatch.setenv("ASCLEPIUS_UPLOAD_SESSION_TTL_HOURS", "0")
    # TTL 0 floors to 1h by design, so age the row instead of trusting the clock.
    with store._conn() as conn:
        conn.execute("UPDATE ingest_upload_sessions SET updated_at = '2000-01-01T00:00:00' "
                     "WHERE session_id = ?", (session["session_id"],))
    assert asc_uploads.reap_stale_sessions(store) == 1
    assert not parts_dir.exists()
    assert store.get_upload_session(session["session_id"])["status"] == "aborted"
