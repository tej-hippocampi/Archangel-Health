"""Chunked, resumable, integrity-verified upload (PRD-I §1).

WHY THIS EXISTS
---------------
The platform closes a request body that has not finished uploading within five
minutes. That is not a tunable — it is an edge property. At a realistic hospital
network 20 Mbps it buys ~750 MB; at 5 Mbps, ~180 MB. **A multi-GB single-request
upload is therefore physically impossible**, and raising a byte cap does nothing
about it. The only shape that works is many small requests, because the five
minutes then applies per chunk and is irrelevant at 8-32 MB.

    POST /hs/uploads/sessions              declare {filename, size, sha256}
                                           → server authorizes, mints session,
                                             returns the chunk plan
    GET  /hs/uploads/sessions/{id}         → which parts are already stored
    PUT  /hs/uploads/sessions/{id}/parts/{n}  one chunk + its own sha256
    POST /hs/uploads/sessions/{id}/complete   assemble, recompute the WHOLE-FILE
                                              sha256, verify, then and only then
                                              create the upload row

Presigned direct-to-object-storage would scale further and is the right answer
once a BAA with a storage vendor exists. That is a contract question, not an
engineering one, so it is not in this release.

THE INVARIANT
-------------
**An assembled file with no verified row is invisible to the application.** No
``ingest_uploads`` row exists until ``complete`` has recomputed the digest over
the assembled bytes and matched it against what was declared. There is no window
in which a half-uploaded bundle looks like an upload, because there is nothing
for it to look like.

WHY THE WHOLE-FILE DIGEST, GIVEN PER-CHUNK DIGESTS
--------------------------------------------------
Per-chunk sha256 catches a corrupt chunk. It cannot catch a MISSING one, a
reordered one, or a client that computed its chunk digests correctly over the
wrong bytes. Byte counts do not catch silent truncation either — a short read
that reports its own short length is internally consistent all the way down. The
digest over the assembled whole is the only check that proves the bytes we hold
are the bytes they had, and ``(sha256, byte_size, verified_at)`` on the upload row
is what you show a partner who asks "did you get everything."

WHY PROGRESS LIVES ON DISK
--------------------------
Per-chunk rows would put a single-writer SQLite under a tight write loop for
state the filesystem already knows. Rows are written at declare and at complete;
everything in between is derived from the parts on disk.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from asclepius import ingestion as asc_ingestion

log = logging.getLogger("asclepius.uploads")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PART_NAME = "part-%06d"
_META_SUFFIX = ".meta"


class UploadSessionError(ValueError):
    """A chunked-upload request that cannot be honoured.

    ``code`` is a stable machine token and ``status`` the HTTP status the router
    should return. Both are functions of what the CLIENT sent — never of the
    upload's purpose — which is the property the indistinguishability tests
    assert across variants (PRD-I §3.2)."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class UploadIntegrityError(UploadSessionError):
    """The assembled bytes do not match what was declared. The upload row is never
    written and the assembled blob is destroyed."""


# ─── Limits ──────────────────────────────────────────────────────────────────
def chunk_size_bytes() -> int:
    """Bytes per part. 16 MB sits comfortably inside the five-minute window even
    on a bad hospital link (16 MB at 1 Mbps is ~2 minutes) while keeping the part
    count for a 3 GB bundle in the low hundreds."""
    try:
        return max(1024 * 1024, int(os.getenv("ASCLEPIUS_UPLOAD_CHUNK_BYTES",
                                              str(16 * 1024 * 1024))))
    except ValueError:
        return 16 * 1024 * 1024


def max_bundle_bytes() -> int:
    """Ceiling on ONE declared bundle. Distinct from ``max_zip_bytes`` — that is
    the single-request cap, which the chunked path is specifically built to escape."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_INGEST_MAX_BUNDLE_BYTES",
                                    str(8 * 1024 * 1024 * 1024))))
    except ValueError:
        return 8 * 1024 * 1024 * 1024


def max_parts() -> int:
    """Part-count ceiling, so a pathological chunk_size cannot mint a session with
    millions of directory entries."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_UPLOAD_MAX_PARTS", "4096")))
    except ValueError:
        return 4096


def session_ttl_hours() -> int:
    """How long an unfinished session's parts survive (PRD-I §1.1: the reaper
    deletes unverified parts older than 24 h)."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_UPLOAD_SESSION_TTL_HOURS", "24")))
    except ValueError:
        return 24


def sessions_root() -> Path:
    """Under the DURABLE ingest root, not a temp dir. A resumable upload that does
    not survive a redeploy is not resumable — the partner would be told to resume
    at 3.2 GB and find nothing there."""
    root = asc_ingestion.quarantine_root() / "sessions"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


# ─── Plan ────────────────────────────────────────────────────────────────────
def plan_for(size: int) -> Tuple[int, int]:
    """(chunk_size, part_count) for a declared size. Server-decided: the client
    does not choose the layout, so it cannot ask for one part of 3 GB and put us
    back inside the five-minute window we are here to escape."""
    size = int(size)
    chunk = chunk_size_bytes()
    parts = max(1, -(-size // chunk)) if size else 1
    if parts > max_parts():
        # Grow the chunk instead of refusing: a huge bundle is the case this
        # exists for, and the part cap is about directory hygiene, not safety.
        chunk = -(-size // max_parts())
        parts = max(1, -(-size // chunk))
    return chunk, parts


def expected_part_size(session: Dict[str, Any], n: int) -> int:
    chunk = int(session["chunk_size"])
    total = int(session["declared_size"])
    parts = int(session["part_count"])
    if n < parts:
        return chunk
    return total - chunk * (parts - 1)


# ─── Session lifecycle ───────────────────────────────────────────────────────
def _session_dir(session: Dict[str, Any]) -> Path:
    return Path(session["storage_dir"])


def _part_paths(session: Dict[str, Any], n: int) -> Tuple[Path, Path]:
    base = _session_dir(session) / (_PART_NAME % n)
    return base, base.with_name(base.name + _META_SUFFIX)


def declare(
    store: Any, *, owner_kind: str, owner_id: str, actor: Optional[str],
    filename: Optional[str], size: int, sha256: str,
    content_type: Optional[str], portal_username: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Authorize and mint (or resume) a session. Returns ``(session, created)``.

    Idempotent on ``(owner, sha256, size)``: re-declaring the same bytes returns
    the EXISTING session, so a browser refresh at 3.2 GB resumes rather than
    restarting. ``portal_username`` names the authorizing account; everything the
    admin side derives from it is resolved by the store, not here — this module is
    provider-reachable and therefore has no business knowing what is derived."""
    sha = (sha256 or "").strip().lower()
    if not _SHA256_RE.match(sha):
        raise UploadSessionError("bad_digest",
                                 "sha256 must be 64 lowercase hex characters.")
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise UploadSessionError("bad_size", "size must be an integer number of bytes.")
    if size <= 0:
        raise UploadSessionError("bad_size", "size must be greater than zero.")
    if size > max_bundle_bytes():
        raise UploadSessionError(
            "too_large",
            "This upload is larger than we can accept in one bundle. Please split "
            "it into smaller batches and send them one at a time.", status=413)

    existing = store.find_open_upload_session(
        owner_kind=owner_kind, owner_id=owner_id, declared_sha256=sha,
        declared_size=size)
    if existing:
        # A verified session whose parts are long gone still answers correctly:
        # the caller sees complete=True and the upload_id it already produced.
        if existing.get("status") is None:
            _session_dir(existing).mkdir(parents=True, exist_ok=True, mode=0o700)
        return existing, False

    chunk, parts = plan_for(size)
    session = store.create_upload_session(
        owner_kind=owner_kind, owner_id=owner_id, actor=actor,
        filename=_safe_name(filename), content_type=(content_type or None),
        declared_sha256=sha, declared_size=size, chunk_size=chunk,
        part_count=parts, storage_root=str(sessions_root()),
        portal_username=portal_username)
    _session_dir(session).mkdir(parents=True, exist_ok=True, mode=0o700)
    return session, True


def _safe_name(name: Optional[str]) -> str:
    """Reduce a partner-supplied filename to a safe basename BEFORE it reaches the
    filesystem or a ``Content-Disposition`` header (PRD-I §4.2). Applied at declare
    so nothing downstream has to remember to."""
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    return (base or "upload.zip")[:120]


def session_state(session: Dict[str, Any]) -> Dict[str, Any]:
    """Which parts are stored, derived from the filesystem (PRD-I §1.2).

    A part counts as received only when BOTH its data file and its sidecar exist —
    the sidecar is written last, after an atomic rename of the data, so a crash
    mid-write leaves a part that is correctly reported as still missing."""
    parts: List[Dict[str, Any]] = []
    received: List[int] = []
    bytes_received = 0
    for n in range(1, int(session["part_count"]) + 1):
        data_path, meta_path = _part_paths(session, n)
        if not (data_path.exists() and meta_path.exists()):
            continue
        try:
            digest, raw_len = meta_path.read_text(encoding="utf-8").split()
        except (OSError, ValueError):
            continue
        received.append(n)
        bytes_received += int(raw_len)
        parts.append({"part": n, "size": int(raw_len), "sha256": digest})
    missing = [n for n in range(1, int(session["part_count"]) + 1) if n not in set(received)]
    return {"parts": parts, "received_parts": received, "missing_parts": missing,
            "bytes_received": bytes_received}


def store_part(session: Dict[str, Any], n: int, data: bytes,
               declared_sha: Optional[str]) -> Dict[str, Any]:
    """Store one chunk, rejecting it on its own sha256 (PRD-I §1.1).

    Fail-closed on a missing per-chunk digest: an unverified chunk is exactly the
    thing the whole-file digest would later reject after the partner had already
    spent the bandwidth on every remaining part."""
    if session.get("status") is not None:
        raise UploadSessionError("session_closed",
                                 "This upload has already been completed.", status=409)
    total_parts = int(session["part_count"])
    if n < 1 or n > total_parts:
        raise UploadSessionError("bad_part",
                                 f"part must be between 1 and {total_parts}.")
    want = expected_part_size(session, n)
    if len(data) != want:
        raise UploadSessionError(
            "bad_part_size",
            f"part {n} must be exactly {want} bytes; received {len(data)}.")
    sha = (declared_sha or "").strip().lower()
    if not _SHA256_RE.match(sha):
        raise UploadSessionError(
            "missing_part_digest",
            "each part must carry its own sha256 in the X-Chunk-SHA256 header.")
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha:
        raise UploadSessionError(
            "part_digest_mismatch",
            f"part {n} did not match its declared sha256 — re-send this part.")

    from field_crypto import encrypt_bytes
    data_path, meta_path = _part_paths(session, n)
    data_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = data_path.with_name(data_path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(encrypt_bytes(data))
        fh.flush()
        os.fsync(fh.fileno())
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, data_path)
    # Sidecar LAST: it is the commit marker session_state reads, so a crash between
    # the two leaves the part correctly reported as not yet received.
    meta_path.write_text(f"{actual} {len(data)}", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(meta_path, 0o600)
    return {"part": n, "size": len(data), "sha256": actual}


def _iter_assembled(session: Dict[str, Any]) -> Iterator[bytes]:
    """Yield the plaintext bundle part by part, in order. One part in memory at a
    time — this is what keeps peak memory flat regardless of bundle size."""
    from field_crypto import decrypt_bytes
    for n in range(1, int(session["part_count"]) + 1):
        data_path, _meta = _part_paths(session, n)
        with open(data_path, "rb") as fh:
            yield decrypt_bytes(fh.read()) or b""


def complete(store: Any, session: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble, recompute the whole-file digest, and verify.

    Returns ``{upload_id, raw_path, sha256, byte_size, verified_at}``. Raises
    ``UploadIntegrityError`` when the assembled bytes do not match what was
    declared — and in that case NOTHING is created: no upload row, no blob. The
    caller inserts the ``ingest_uploads`` row only after this returns."""
    if session.get("status") == "verified" and session.get("upload_id"):
        upload = store.get_ingest_upload(session["upload_id"])
        if upload:
            return {"upload_id": upload["upload_id"], "raw_path": upload.get("raw_path"),
                    "sha256": upload.get("sha256"), "byte_size": upload.get("size_bytes"),
                    "verified_at": session.get("verified_at"), "already": True}
    if session.get("status") is not None:
        raise UploadSessionError("session_closed",
                                 "This upload can no longer be completed.", status=409)

    state = session_state(session)
    if state["missing_parts"]:
        raise UploadSessionError(
            "incomplete",
            f"{len(state['missing_parts'])} part(s) have not been received yet.",
            status=409)
    if state["bytes_received"] != int(session["declared_size"]):
        raise UploadIntegrityError(
            "size_mismatch",
            "the parts received do not add up to the declared size.", status=409)

    upload_id = store.new_upload_id()
    digest = hashlib.sha256()
    total = 0

    def _counted() -> Iterator[bytes]:
        nonlocal total
        for chunk in _iter_assembled(session):
            digest.update(chunk)
            total += len(chunk)
            yield chunk

    raw_path = asc_ingestion.store_raw_stream(upload_id, _counted())
    actual = digest.hexdigest()
    if actual != session["declared_sha256"] or total != int(session["declared_size"]):
        # Destroy the assembled blob. A blob with no verified row is invisible to
        # the application by design, but leaving it on disk would still be PHI we
        # cannot account for.
        asc_ingestion.delete_raw(raw_path)
        store.update_upload_session(session["session_id"], status="failed")
        _purge_parts(session)
        raise UploadIntegrityError(
            "digest_mismatch",
            "The assembled upload did not match the checksum you declared. "
            "Nothing was stored — please start the upload again.", status=409)

    verified_at = datetime.now(timezone.utc).isoformat()
    return {"upload_id": upload_id, "raw_path": raw_path, "sha256": actual,
            "byte_size": total, "verified_at": verified_at, "already": False}


def finalize(store: Any, session: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Mark the session verified and drop its parts. Called by the router AFTER the
    upload row exists, so a crash in between leaves the parts on disk and the
    session resumable rather than leaving a verified session with no upload."""
    store.update_upload_session(session["session_id"], status="verified",
                                upload_id=result["upload_id"],
                                verified_at=result["verified_at"])
    _purge_parts(session)


def abort(store: Any, session: Dict[str, Any]) -> None:
    store.update_upload_session(session["session_id"], status="aborted")
    _purge_parts(session)


def _purge_parts(session: Dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(_session_dir(session), ignore_errors=True)


def reap_stale_sessions(store: Any) -> int:
    """Delete unverified parts older than the TTL (PRD-I §1.1). Best-effort; never
    raises — a reaper that can crash the request path it is called from is worse
    than a reaper that occasionally skips a run."""
    cutoff = (datetime.utcnow() - timedelta(hours=session_ttl_hours())
              ).replace(microsecond=0).isoformat()
    reaped = 0
    try:
        stale = store.list_stale_upload_sessions(older_than_iso=cutoff)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("upload session reaper could not list sessions: %s", exc)
        return 0
    for session in stale:
        try:
            _purge_parts(session)
            store.update_upload_session(session["session_id"], status="aborted")
            reaped += 1
        except Exception as exc:  # pragma: no cover - defensive per-session
            log.warning("upload session reaper failed for %s: %s",
                        session.get("session_id"), exc)
    if reaped:
        with contextlib.suppress(Exception):
            store.log_event(entity_type="ingest_upload_session",
                            event_type="sessions_reaped",
                            payload={"reaped": reaped, "ttl_hours": session_ttl_hours()})
    return reaped


def public_session(session: Dict[str, Any], state: Optional[Dict[str, Any]] = None,
                   ) -> Dict[str, Any]:
    """The partner-facing view of a session.

    An EXPLICIT allowlist of fields, and the only place a session is serialized
    for a provider. A ``{k: v for k, v in session.items()}`` here would ship every
    column the row ever grows, which is precisely how an admin-only field reaches
    a partner six months after anyone thought about it (PRD-I §3.1)."""
    state = state if state is not None else session_state(session)
    return {
        "session_id": session["session_id"],
        "filename": session.get("filename"),
        "size": int(session["declared_size"]),
        "sha256": session["declared_sha256"],
        "chunk_size": int(session["chunk_size"]),
        "part_count": int(session["part_count"]),
        "received_parts": state["received_parts"],
        "missing_parts": state["missing_parts"],
        "bytes_received": state["bytes_received"],
        "complete": session.get("status") == "verified",
        "upload_id": session.get("upload_id"),
    }
