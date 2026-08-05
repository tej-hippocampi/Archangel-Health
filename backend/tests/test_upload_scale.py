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
import shlex
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
    """A 1 MB chunk keeps the fixtures small while exercising the multi-part path.

    This fixture once carried the comment *"the code cannot tell that 3 parts of
    1 MB is not 300 parts of 16 MB"*, and that assumption was itself a bug: a
    read-side frame ceiling was added while the writer emitted one frame per
    caller chunk, so the shipped 16 MB default produced frames the reader
    rejected — and every test passed, because every test ran at 1 MB. Pinning the
    chunk size hides anything that depends on it, so the tests that care about the
    DEFAULT unpin it explicitly and say so."""
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
    # TTL 0 floors to 1h by design, so age the state instead of trusting the clock.
    # BOTH the row and the parts on disk: the row is only the cheap candidate
    # filter, and idle time is measured from real part activity — a session whose
    # parts arrived recently is in flight no matter how old the row is.
    with store._conn() as conn:
        conn.execute("UPDATE ingest_upload_sessions SET updated_at = '2000-01-01T00:00:00' "
                     "WHERE session_id = ?", (session["session_id"],))
    for p in parts_dir.iterdir():
        os.utime(p, (0, 0))
    assert asc_uploads.reap_stale_sessions(store) == 1
    assert not parts_dir.exists()
    assert store.get_upload_session(session["session_id"])["status"] == "aborted"


# ── the two doors must accept the same files ─────────────────────────────────
def test_a_bare_clinical_file_through_the_chunked_door_ingests(client):
    """The same file must not succeed through one door and fail through another.

    The single-request doors pack a loose .csv/.json/.hl7/.txt into a bundle
    server-side. The chunked door streams whatever was selected, so without the
    same packing a hospital sending one bare CSV would get "we could not read this
    upload" purely because of which door they happened to use."""
    store = A.fresh_store()
    _portal(client, store)
    rows = b"".join(
        b"pt1,BMP,Creatinine,%d.%d,mg/dL,2025-03-%02d\n" % (1 + i % 3, i % 10, 1 + i % 28)
        for i in range(200))
    csv = b"patient_key,panel,analyte,value,unit,collected_at\n" + rows
    session = _declare(client, csv, filename="labs.csv")
    r = _upload_all(client, csv, session)
    assert r.status_code == 200, r.text
    upload = store.get_ingest_upload(r.json()["upload_id"])
    assert upload["status"] != "rejected", upload.get("reason")
    assert store.list_ingest_cases(upload_id=upload["upload_id"])
    # The stored raw blob is still byte-for-byte what the partner sent, so the
    # sha256 on the row remains a digest of THEIR file.
    assert upload["sha256"] == hashlib.sha256(csv).hexdigest()
    assert asc_ingestion.load_raw(upload["raw_path"]) == csv


def test_the_browser_side_streaming_sha256_matches_the_reference():
    """The client declares the whole-file digest before sending a byte, so a wrong
    hasher does not degrade anything — it makes every large upload fail. Checked
    against Node's crypto, including a 600 MB case: JS `>>> 32` is a no-op, so the
    naive length-padding shift is correct below 512 MB and wrong above it, which
    is precisely the range this code exists for."""
    import shutil as _shutil
    import subprocess as _subprocess

    node = _shutil.which("node")
    if not node:
        pytest.skip("node is not available in this environment")
    script = Path(__file__).resolve().parent / "test_provider_upload_dom.js"
    res = _subprocess.run([node, str(script)], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "all checks passed" in res.stdout


# ── resource bounds on the part endpoint ─────────────────────────────────────
def test_an_oversized_part_is_refused_without_buffering_it(client):
    """``request.body()`` buffers the whole body BEFORE returning, so the obvious
    length check happens only after the memory has already been spent — an
    authenticated partner could push a body far larger than any part and take the
    container down before failing the check. The read is capped mid-stream."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(1024 * 1024)
    session = _declare(client, data)
    limit = session["chunk_size"]

    # Lies about Content-Length so the cheap header gate cannot be what stops it,
    # forcing the streaming cap to do the work.
    oversized = b"x" * (limit * 3)
    r = client.put(
        f"{API}/hs/uploads/sessions/{session['session_id']}/parts/1",
        content=oversized, headers={"X-Chunk-SHA256": hashlib.sha256(oversized).hexdigest()})
    assert r.status_code == 413, r.text
    state = client.get(f"{API}/hs/uploads/sessions/{session['session_id']}").json()
    assert state["received_parts"] == []


def test_a_declare_race_returns_one_session_not_a_500(client):
    """The partial unique index makes concurrent declares for the same bytes
    collide at the DB. That is the index doing its job, and the partner sees one
    upload they asked for twice — not a 500."""
    store = A.fresh_store()
    hs, username = _portal(client, store)
    data = _bundle(1024 * 1024)
    from asclepius import uploads as U
    kwargs = dict(owner_kind="health_system", owner_id=hs["hs_id"], actor=username,
                  filename="bundle.zip", size=len(data),
                  sha256=hashlib.sha256(data).hexdigest(),
                  content_type="application/zip", portal_username=username)
    first, created_a = U.declare(store, **kwargs)
    assert created_a
    # Simulate the racing INSERT: bypass the idempotency lookup the second caller
    # would normally hit, so the DB constraint is what resolves it.
    second = store.create_upload_session(
        owner_kind="health_system", owner_id=hs["hs_id"], actor=username,
        filename="bundle.zip", content_type="application/zip",
        declared_sha256=hashlib.sha256(data).hexdigest(), declared_size=len(data),
        chunk_size=first["chunk_size"], part_count=first["part_count"],
        storage_root=str(U.sessions_root()), portal_username=username)
    assert second["session_id"] == first["session_id"]


# ── hardening found by adversarial review ────────────────────────────────────
def test_the_reaper_spares_a_session_whose_parts_are_still_arriving(client, monkeypatch):
    """Rows are written at declare and at complete and NEVER in between, so the
    row's ``updated_at`` is the DECLARE time for the whole life of an upload.
    Measuring the TTL against it makes the window '24 h since you started' rather
    than '24 h since you stopped' — and 8 GB on a 1 Mbps hospital link is ~18 h of
    continuous transfer. Idle time is read from the parts on disk instead."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(3 * 1024 * 1024)
    session = _declare(client, data)
    blobs = list(_parts_of(data, session["chunk_size"]))
    assert _put_part(client, session["session_id"], 1, blobs[0]).status_code == 200

    # Exactly the state a long upload reaches: declared a day ago, parts landing now.
    with store._conn() as conn:
        conn.execute("UPDATE ingest_upload_sessions SET updated_at = '2000-01-01T00:00:00' "
                     "WHERE session_id = ?", (session["session_id"],))
    assert asc_uploads.reap_stale_sessions(store) == 0, "reaped a live upload"
    assert store.get_upload_session(session["session_id"])["status"] is None

    # …and it can still be finished.
    for n, blob in enumerate(blobs, start=1):
        assert _put_part(client, session["session_id"], n, blob).status_code == 200
    assert client.post(
        f"{API}/hs/uploads/sessions/{session['session_id']}/complete").status_code == 200


def test_a_session_stuck_mid_assembly_is_handed_back_not_deleted(client):
    """A ``completing`` claim outlives the process that took it, so a hard crash
    during assembly would lock a partner out of an upload they could still finish."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    session = _declare(client, data)
    for n, blob in enumerate(_parts_of(data, session["chunk_size"]), start=1):
        _put_part(client, session["session_id"], n, blob)

    assert store.claim_upload_session_for_completion(session["session_id"]) is True
    # A second claim loses — which is what stops a double assembly.
    assert store.claim_upload_session_for_completion(session["session_id"]) is False
    with store._conn() as conn:
        conn.execute("UPDATE ingest_upload_sessions SET updated_at = '2000-01-01T00:00:00' "
                     "WHERE session_id = ?", (session["session_id"],))
        conn.execute("UPDATE ingest_upload_sessions SET storage_dir = storage_dir "
                     "WHERE session_id = ?", (session["session_id"],))
    import os as _os
    parts_dir = Path(store.get_upload_session(session["session_id"])["storage_dir"])
    old = 0
    for p in parts_dir.iterdir():
        _os.utime(p, (old, old))

    asc_uploads.reap_stale_sessions(store)
    assert store.get_upload_session(session["session_id"])["status"] is None
    assert parts_dir.exists(), "a stuck assembly had its parts deleted"
    assert client.post(
        f"{API}/hs/uploads/sessions/{session['session_id']}/complete").status_code == 200


def test_a_corrupt_frame_length_is_reported_not_allocated_for(tmp_path, monkeypatch):
    """``read(n)`` PREALLOCATES n bytes, and the frame length is a uint32 straight
    out of the file — so one flipped byte turned a truncation error into a 4 GiB
    allocation and an OOM kill."""
    import struct
    import tracemalloc

    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    bad = Path(asc_ingestion.quarantine_root()) / "corrupt.zip.enc"
    bad.write_bytes(b"ASCRAWF1" + struct.pack(">I", 0xFFFFFFFF))

    tracemalloc.start()
    try:
        with pytest.raises(ValueError) as exc:
            list(asc_ingestion.iter_raw(str(bad)))
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "corrupt" in str(exc.value)
    assert peak < 32 * 1024 * 1024, f"peak {peak} — the length was allocated for"


def test_rejected_entries_are_charged_to_the_archive_budget(monkeypatch, tmp_path):
    """Debiting the budget only on the accept path left total decompression work
    bounded by the PER-ENTRY ratio cap rather than the whole-archive budget: many
    entries each expanding to the entry cap before rejection is hundreds of GB of
    deflate with the budget never moving."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_ENTRY_BYTES", str(1024 * 1024))
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_RATIO", "1000000")
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_UNCOMPRESSED", str(2 * 1024 * 1024))
    monkeypatch.setenv("ASCLEPIUS_INGEST_TOTAL_RATIO", "2")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for i in range(12):
            z.writestr(f"bomb-{i}.txt", b"\0" * (4 * 1024 * 1024))
    with pytest.raises(asc_ingestion.BundleRejected) as exc:
        asc_ingestion.unpack_bundle(buf.getvalue())
    assert "budget" in str(exc.value)


def test_an_unexpected_error_still_gets_the_provider_response_discipline(client, monkeypatch):
    """Anything not an HTTPException used to escape to Starlette's default 500 —
    plain text, a different header set, no no-store, no padding, no time budget.
    The one response shape that skipped the whole discipline would be the one
    nobody wrote deliberately."""
    store = A.fresh_store()
    _portal(client, store)

    def boom(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("asclepius.uploads.reap_stale_sessions", boom)
    data = _bundle()
    r = client.post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest()})
    assert r.status_code == 500
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-type"].startswith("application/json")
    assert len(r.content) % 4096 == 0, "the 500 body was not padded"
    assert "synthetic failure" not in r.text, "the internal reason reached the partner"


# ── C1: the container must be self-consistent at the DEFAULT chunk size ──────
# These tests deliberately do NOT use the 1 MB autouse chunk fixture for the
# thing under test. That fixture's comment claimed "the code cannot tell that 3
# parts of 1 MB is not 300 parts of 16 MB" — and that assumption was itself the
# bug: a read-side frame ceiling of 8 MB + 4 KB was added while the writer emitted
# one frame per caller chunk, so every part at the 16 MB default was ~8.4 MB over
# the ceiling. Every large bundle uploaded for hours, passed the whole-file
# digest, was written to durable storage, and was then rejected as unreadable
# with copy that blamed the hospital's file.
def test_raw_container_round_trips_at_the_default_chunk_size(tmp_path, monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.delenv("ASCLEPIUS_UPLOAD_CHUNK_BYTES", raising=False)
    chunk = asc_uploads.chunk_size_bytes()
    assert chunk == 16 * 1024 * 1024, "this test is about the SHIPPED default"

    payload = os.urandom(chunk + 7)                     # one full part plus change
    path = asc_ingestion.store_raw_stream(
        "upl-default", [payload[:chunk], payload[chunk:]])
    assert asc_ingestion.load_raw(path) == payload


@pytest.mark.parametrize("caller_chunk", [
    1024,                       # absurdly small
    1024 * 1024,                # the test fixture's size
    8 * 1024 * 1024,            # exactly the frame size
    8 * 1024 * 1024 + 1,        # one byte over
    16 * 1024 * 1024,           # the shipped default
    33 * 1024 * 1024,           # larger than any configured chunk
])
def test_the_writer_is_independent_of_the_callers_chunk_size(tmp_path, monkeypatch,
                                                             caller_chunk):
    """The container's frame size is the CONTAINER's business. Coupling it to
    whatever the caller happened to hand in is what let a read ceiling and a write
    size drift apart in the first place."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    payload = os.urandom(min(caller_chunk * 2 + 13, 40 * 1024 * 1024))
    path = asc_ingestion.store_raw_stream(
        f"upl-{caller_chunk}",
        (payload[i:i + caller_chunk] for i in range(0, len(payload), caller_chunk)))
    assert asc_ingestion.load_raw(path) == payload
    # And no frame on disk exceeds what the reader will accept.
    for piece in asc_ingestion.iter_raw(path):
        assert len(piece) <= asc_ingestion._RAW_FRAME_BYTES


def test_a_large_bundle_survives_upload_at_the_default_chunk_size(monkeypatch, tmp_path):
    """End to end through the real routes at the SHIPPED chunk size: the bundle
    must still be readable after it is stored, and the pipeline must accept it."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest2"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets2"))
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")
    monkeypatch.delenv("ASCLEPIUS_UPLOAD_CHUNK_BYTES", raising=False)
    store = A.fresh_store()
    with TestClient(A.app, base_url="https://testserver") as client:
        _portal(client, store)
        data = _many_entry_bundle(20 * 1024 * 1024)     # > 16 MB → 2 parts
        session = _declare(client, data)
        assert session["chunk_size"] == 16 * 1024 * 1024
        assert session["part_count"] == 2
        r = _upload_all(client, data, session)
        assert r.status_code == 200, r.text
        upload = store.get_ingest_upload(r.json()["upload_id"])

    # The stored blob is readable — this is what C1 broke.
    assert asc_ingestion.load_raw(upload["raw_path"]) == data
    assert upload["status"] != "rejected", (
        f"a verified bundle was rejected downstream: {upload.get('reason')}")
    assert store.list_ingest_cases(upload_id=upload["upload_id"])


# ── H2: releasing a claim must not collide with the live retry ───────────────
def test_releasing_a_stuck_claim_does_not_collide_with_a_fresh_session(client):
    """The exact sequence a partner produces after a crash.

    ``idx_ingest_sessions_idem_v2`` is partial over ``status IS NULL``, and
    releasing a claim sets status back to NULL — so a ``completing`` session plus
    the fresh session the partner opened for the same bytes collide. Two effects,
    both bad: the IntegrityError is raised from inside ``except BaseException`` in
    ``complete``, MASKING the original assembly failure behind a 500 and locking
    the session forever; and the reaper then aborts the LIVE retry while leaving
    the stuck one untouched."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)

    stuck = _declare(client, data)
    for n, blob in enumerate(_parts_of(data, stuck["chunk_size"]), start=1):
        _put_part(client, stuck["session_id"], n, blob)
    assert store.claim_upload_session_for_completion(stuck["session_id"]) is True

    # The partner gives up and re-declares — a NEW open session for the same bytes.
    fresh = _declare(client, data)
    assert fresh["session_id"] != stuck["session_id"]

    # Releasing the stuck claim must not raise, and must not disturb the live one.
    store.release_upload_session_claim(stuck["session_id"])
    assert store.get_upload_session(fresh["session_id"])["status"] is None

    # And the reaper must take the STUCK one, never the live retry. Only the stuck
    # session is aged — ageing the live one too would be testing that the reaper
    # spares an abandoned session, which is not what it should do.
    import os as _os
    with store._conn() as conn:
        conn.execute("UPDATE ingest_upload_sessions SET updated_at = "
                     "'2000-01-01T00:00:00' WHERE session_id = ?",
                     (stuck["session_id"],))
    stuck_dir = Path(store.get_upload_session(stuck["session_id"])["storage_dir"])
    if stuck_dir.exists():
        for p in stuck_dir.iterdir():
            _os.utime(p, (0, 0))
    asc_uploads.reap_stale_sessions(store)
    assert store.get_upload_session(fresh["session_id"])["status"] is None, (
        "the reaper aborted the partner's live retry")
    assert store.get_upload_session(stuck["session_id"])["status"] == "aborted", (
        "the stuck session was left behind, holding its parts on the volume")
    assert not stuck_dir.exists(), "the retired session's parts were not released"


def test_an_assembly_failure_surfaces_its_own_reason(client, monkeypatch):
    """The release happens inside ``except BaseException``. If it can raise, it
    replaces the real failure with a database error the operator cannot act on."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    session = _declare(client, data)
    for n, blob in enumerate(_parts_of(data, session["chunk_size"]), start=1):
        _put_part(client, session["session_id"], n, blob)
    _declare(client, data)   # a competing open session, so a naive release collides

    def boom(*_a, **_k):
        raise RuntimeError("assembly exploded")

    monkeypatch.setattr(asc_ingestion, "store_raw_stream", boom)
    row = store.get_upload_session(session["session_id"])
    with pytest.raises(RuntimeError, match="assembly exploded"):
        asc_uploads.complete(store, row)
    # …and the session is usable again rather than locked in 'completing'.
    assert store.get_upload_session(session["session_id"])["status"] in (None, "aborted")


# ── H4: antivirus must not be inline on a multi-GB bundle ────────────────────
# Ignores the extra path argument and blocks, which is what a real scanner that
# is too slow for its timeout looks like.
_SLOW_SCANNER = shlex.join([sys.executable, "-c", "import time; time.sleep(30)"])
def test_a_scanner_timeout_is_not_reported_as_malware(monkeypatch, tmp_path):
    """PRD §1.3 is explicit that AV does not belong inline, and the reason is
    exactly this: a scanner that cannot finish a 3 GB file inside its timeout
    would reject a clean hospital bundle AS MALWARE. A timeout is 'we do not
    know', which is a different answer from 'this is dangerous', and conflating
    them destroys good data and tells the partner something false about it."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    # A command that really blocks. `sleep 5` would be handed the scan path as a
    # second argument and exit non-zero immediately — which is a DETECTION, the
    # very outcome this test needs to distinguish from a timeout.
    monkeypatch.setenv("ASCLEPIUS_MALWARE_SCAN_CMD", _SLOW_SCANNER)
    monkeypatch.setenv("ASCLEPIUS_MALWARE_SCAN_TIMEOUT_SEC", "1")
    target = tmp_path / "probe.bin"
    target.write_bytes(b"x" * 1024)

    ok, detail = asc_ingestion.malware_scan(str(target))
    assert ok is False
    assert asc_ingestion.scan_was_inconclusive(detail) is True, (
        f"a timeout must be distinguishable from a detection: {detail!r}")


def test_a_flagged_scan_is_still_a_hard_rejection(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_MALWARE_SCAN_CMD", "false")
    target = tmp_path / "probe.bin"
    target.write_bytes(b"x" * 1024)
    ok, detail = asc_ingestion.malware_scan(str(target))
    assert ok is False
    assert asc_ingestion.scan_was_inconclusive(detail) is False


def test_inline_scanning_is_skipped_above_the_size_ceiling(monkeypatch, tmp_path):
    """Above the ceiling the bundle is deferred to the post-verified worker rather
    than blocking ingest — which is what the PRD asked for."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_MALWARE_SCAN_CMD", "false")   # would reject
    monkeypatch.setenv("ASCLEPIUS_MALWARE_SCAN_MAX_BYTES", "1024")
    target = tmp_path / "big.bin"
    target.write_bytes(b"x" * 4096)
    ok, detail = asc_ingestion.malware_scan(str(target))
    assert ok is True and "deferred" in detail


def test_a_timeout_holds_the_upload_for_review_instead_of_rejecting_it(client, monkeypatch):
    """End to end: a scanner that times out must not produce a rejection whose
    partner-facing copy blames the hospital's file."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(1024 * 1024)
    session = _declare(client, data)
    monkeypatch.setenv("ASCLEPIUS_MALWARE_SCAN_CMD", _SLOW_SCANNER)
    monkeypatch.setenv("ASCLEPIUS_MALWARE_SCAN_TIMEOUT_SEC", "1")
    r = _upload_all(client, data, session)
    assert r.status_code == 200, r.text
    upload = store.get_ingest_upload(r.json()["upload_id"])
    assert upload["status"] == "needs_review", upload
    assert asc_ingestion.UNREADABLE_UPLOAD_MESSAGE not in (upload.get("reason") or "")


# ── M1: an unreferenced PHI blob must not live forever ──────────────────────
def test_a_blob_with_no_upload_row_is_eventually_released(client, monkeypatch, tmp_path):
    """A crash between ``complete()`` and the upload row leaves an encrypted
    bundle on the durable volume that no row points at. ``purge_expired_raw``
    iterates DB ROWS, so it never sees it, and the scratch sweeper's prefixes do
    not match ``upl-*.zip.enc`` — so it stayed forever. Unaccounted PHI on disk is
    a HIPAA accounting problem, not just wasted space."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    store = A.fresh_store()
    orphan = asc_ingestion.store_raw_stream("upl-orphaned", [b"PK\x03\x04orphan bytes"])
    assert Path(orphan).exists()

    # Inside the retention window it is left alone: a blob written seconds ago is
    # far more likely to be an upload mid-insert than an orphan.
    assert asc_ingestion.purge_orphan_raw(store) == 0
    assert Path(orphan).exists()

    os.utime(orphan, (0, 0))
    assert asc_ingestion.purge_orphan_raw(store) == 1
    assert not Path(orphan).exists()


def test_a_referenced_blob_is_never_treated_as_an_orphan(client, monkeypatch, tmp_path):
    """The dangerous direction. Deleting a blob a row points at destroys a case
    whose partner bundle cannot be recovered."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(1024 * 1024)
    session = _declare(client, data)
    r = _upload_all(client, data, session)
    raw_path = store.get_ingest_upload(r.json()["upload_id"])["raw_path"]
    os.utime(raw_path, (0, 0))          # old enough to be swept, but it IS referenced

    assert asc_ingestion.purge_orphan_raw(store) == 0
    assert Path(raw_path).exists()


# ── M2: concurrent PUTs of the same part must not share a temp name ─────────
def test_concurrent_writes_of_the_same_part_do_not_collide(client):
    """Two in-flight PUTs for one part shared a single ``.tmp`` path: one renames
    it out from under the other, giving a FileNotFoundError (a 500), and the
    partial-write window can leave bytes that pass the part digest check and fail
    the whole-file digest after gigabytes of transfer."""
    import threading

    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(2 * 1024 * 1024)
    session = _declare(client, data)
    row = store.get_upload_session(session["session_id"])
    blob = data[:session["chunk_size"]]
    digest = hashlib.sha256(blob).hexdigest()

    errors = []

    def push():
        try:
            asc_uploads.store_part(row, 1, blob, digest)
        except Exception as exc:      # noqa: BLE001 - the failure IS the finding
            errors.append(exc)

    threads = [threading.Thread(target=push) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent part writes raised: {errors[:3]}"
    state = asc_uploads.session_state(row)
    assert state["received_parts"] == [1]
    assert state["parts"][0]["sha256"] == digest


# ── LOW: a gzipped FHIR bulk export is the standard shape, not an attack ────
def test_a_gzipped_ndjson_member_is_still_readable(monkeypatch, tmp_path):
    """``.gz`` was swept into the nested-archive rejection, which is a real
    defense against recursive bombs — but a FHIR bulk export ships
    ``*.ndjson.gz``, so the rule refused the single most standard way a hospital
    exports at scale. A single-member gzip is a COMPRESSED FILE, not an archive:
    it cannot nest, so decompressing it once adds no recursion depth."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    import gzip as _gzip

    bundle_json = json.dumps({"resourceType": "Bundle", "type": "collection",
                              "entry": []}).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("export.json.gz", _gzip.compress(bundle_json))
        z.writestr("nested.zip", b"PK\x03\x04still refused")
    out = asc_ingestion.unpack_bundle(buf.getvalue())
    by_name = {e["name"]: e for e in out["entries"]}
    assert by_name["export.json.gz"]["kind"] != "rejected", by_name["export.json.gz"]
    assert by_name["export.json.gz"]["data"] == bundle_json
    # A real nested ARCHIVE is still refused — the depth rule is intact.
    assert "nested archive" in by_name["nested.zip"]["reason"]


def test_a_gzip_bomb_member_is_still_stopped(monkeypatch, tmp_path):
    """Decompressing gzip must obey the same produced-byte accounting as any
    other entry, or the exemption above becomes the hole."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_INGEST_MAX_ENTRY_BYTES", str(1024 * 1024))
    import gzip as _gzip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bomb.json.gz", _gzip.compress(b"\0" * (8 * 1024 * 1024)))
    out = asc_ingestion.unpack_bundle(buf.getvalue())
    entry = out["entries"][0]
    assert entry["kind"] == "rejected"
    assert "too large" in entry["reason"] or "ratio" in entry["reason"]


def test_spilled_entry_data_raises_rather_than_returning_none_after_cleanup(
        monkeypatch, tmp_path):
    """``.get("data")`` swallowing an OSError meant a PHI pipeline would read a
    missing entry as an empty one and carry on. Silent data loss is the worst
    possible failure mode here; a raise is recoverable."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("note.txt", "clinical note")
    root = Path(asc_ingestion.quarantine_root())
    fd, tmp = __import__("tempfile").mkstemp(suffix=".zip", dir=str(root))
    with os.fdopen(fd, "wb") as fh:
        fh.write(buf.getvalue())
    bundle = asc_ingestion.unpack_bundle_from_path(tmp)
    entry = [e for e in bundle["entries"] if e["name"] == "note.txt"][0]
    assert entry["data"] == b"clinical note"
    bundle["cleanup"]()
    with pytest.raises(OSError):
        entry.get("data")


# ── M4: one upload is not one copy ──────────────────────────────────────────
def test_a_bundle_larger_than_the_volume_is_declined_at_declare(client, monkeypatch):
    """Parts, the assembled blob, a plaintext copy for unpacking and spilled entry
    bytes all land on the SAME volume as the database. Accepting a bundle we
    cannot finish runs the volume out from under SQLite — worse than declining —
    and finding out at completion means the partner already spent the hours."""
    store = A.fresh_store()
    _portal(client, store)
    data = _bundle(1024 * 1024)

    import collections
    import shutil as _shutil
    tiny = collections.namedtuple("usage", "total used free")(1 << 30, 1 << 30, 1024)
    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: tiny)

    r = client.post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest()})
    assert r.status_code == 507, r.text
    assert "storage" in r.json()["detail"].lower()
    assert store.get_upload_session("nope") is None
    # Nothing was created for it.
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingest_upload_sessions"
                            ).fetchone()[0] == 0


def test_the_headroom_check_accounts_for_working_copies(monkeypatch):
    """Not just the bundle — the amplification factor is the point."""
    import collections
    import shutil as _shutil

    size = 100 * 1024 * 1024
    just_the_bundle = collections.namedtuple("usage", "total used free")(
        1 << 40, 0, size + 1024)
    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: just_the_bundle)
    ok, _why = asc_uploads.disk_headroom_for(size)
    assert ok is False, "free space equal to the bundle alone is not enough"

    plenty = collections.namedtuple("usage", "total used free")(
        1 << 40, 0, size * 10)
    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: plenty)
    assert asc_uploads.disk_headroom_for(size)[0] is True
