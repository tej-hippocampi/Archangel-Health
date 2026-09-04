"""Bundle ingestion orchestration (EHR Ingestion PRD §3, §5, §8).

    unpack (zip-bomb-safe) → classify each entry → format adapters →
    assemble one case per patient → normalize timeline → verify de-id →
    deidentify() hard guard → ingest_cases row ('ingested' | 'quarantined')

Design rules enforced here:
  * NOTHING PARTIAL LANDS: a patient's case either fully validates or the whole
    case quarantines with a readable (masked) reason. A DICOM entry rejects that
    ENTRY and the rest of the bundle continues — unless imaging was the only
    content, which rejects the upload.
  * The raw partner zip lives ONLY as an AES-GCM-encrypted blob under the
    quarantine dir (0700), auto-purged after ``ASCLEPIUS_RAW_RETENTION_DAYS``.
  * Chain of custody: every step emits a ``store.log_event`` audit event.
  * Malware scanning is a pluggable hook (``ASCLEPIUS_MALWARE_SCAN_CMD`` — any
    command returning non-zero rejects the upload); the built-in baseline
    validates zip magic/structure and rejects executable entries. State that
    honestly in ops docs: a real AV engine is the hook's job.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from pydantic import ValidationError

from asclepius import case_formats as cf
from asclepius import deid_verify
from asclepius.cases import ClinicalCase
from asclepius.store import _utcnow_iso
from asclepius.timeline import TimelineError, normalize_timeline

log = logging.getLogger("asclepius.ingestion")

# ─── Limits (env-tunable) ─────────────────────────────────────────────────────
def max_zip_bytes() -> int:
    try:
        return int(os.getenv("ASCLEPIUS_INGEST_MAX_ZIP_BYTES", str(100 * 1024 * 1024)))
    except ValueError:
        return 100 * 1024 * 1024


def max_entries() -> int:
    """Entry-count ceiling. Raised from 500 for the chunked door (PRD-I §1): a real
    multi-GB hospital bundle routinely carries thousands of files, and the caps that
    actually stop a zip bomb are the per-entry byte cap, the per-entry compression
    ratio, and the total-output budget below — not the file count."""
    try:
        return int(os.getenv("ASCLEPIUS_INGEST_MAX_ENTRIES", "5000"))
    except ValueError:
        return 5000


def max_uncompressed_bytes() -> int:
    """FLOOR for the total decompressed-output budget. The effective budget is
    ``max(this, total_expansion_ratio() * archive_size)`` — see
    ``total_output_budget``: a 3 GB bundle cannot be held to a 500 MB output cap,
    but its budget must still be a function of bytes the partner actually sent."""
    try:
        return int(os.getenv("ASCLEPIUS_INGEST_MAX_UNCOMPRESSED", str(500 * 1024 * 1024)))
    except ValueError:
        return 500 * 1024 * 1024


def max_entry_bytes() -> int:
    """Per-entry decompressed cap. This is the MEMORY bound: an entry is handed to
    its format adapter as one ``bytes`` object, so this is the largest single
    allocation the ingest path can be made to perform. Clinical text / FHIR / CSV /
    a DICOM instance are all far below this."""
    try:
        return int(os.getenv("ASCLEPIUS_INGEST_MAX_ENTRY_BYTES", str(64 * 1024 * 1024)))
    except ValueError:
        return 64 * 1024 * 1024


def entry_compression_ratio_cap() -> float:
    """Per-entry decompressed:compressed ratio ceiling (PRD-I §1.3). Real clinical
    text compresses ~5-15:1; 100:1 is comfortably above legitimate data and far
    below what a bomb needs."""
    try:
        return max(2.0, float(os.getenv("ASCLEPIUS_INGEST_MAX_RATIO", "100")))
    except ValueError:
        return 100.0


def total_expansion_ratio() -> float:
    """Whole-archive decompressed:compressed ceiling. Bounds total output as a
    multiple of what the partner actually uploaded, so the budget scales with real
    bundles without ever being attacker-amplifiable beyond this factor."""
    try:
        return max(2.0, float(os.getenv("ASCLEPIUS_INGEST_TOTAL_RATIO", "10")))
    except ValueError:
        return 10.0


def total_output_budget(archive_bytes: int) -> int:
    return max(max_uncompressed_bytes(),
               int(total_expansion_ratio() * max(0, int(archive_bytes))))


def raw_retention_days() -> int:
    try:
        return max(1, int(os.getenv("ASCLEPIUS_RAW_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


def _default_ingest_dir() -> Path:
    """Co-locate raw blobs with the persistent DB, so the two share durability.

    The admin download + retry paths read this encrypted blob days after upload,
    so it MUST survive redeploys/restarts. Defaulting to ``/tmp`` was the bug:
    on Railway/Render ``/tmp`` is ephemeral and wiped on every redeploy, while
    the DB (``ASCLEPIUS_DB_PATH`` → mounted volume) persists — leaving the upload
    row pointing at a blob that no longer exists, which the download endpoint
    reports as a spurious 410 "already purged". Placing the ingest dir next to
    the DB file means a raw blob is exactly as durable as its DB row. Mirrors
    ``AsclepiusStore``'s DB-path resolution so the two never diverge."""
    db_path = os.getenv("ASCLEPIUS_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "asclepius.db")
    return Path(os.path.dirname(os.path.abspath(db_path))) / "asclepius-ingest"


def quarantine_root() -> Path:
    root = Path(os.getenv("ASCLEPIUS_INGEST_DIR") or _default_ingest_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _ingest_root_path() -> Path:
    """The configured raw-ingest dir, made absolute WITHOUT creating or resolving it.

    ``quarantine_root`` adds the ``mkdir`` (and ``.resolve()``) for real filesystem
    use. A durability *check* must do neither: probing a root-owned ephemeral prefix
    like ``/run`` raises PermissionError instead of returning "not durable" — the very
    answer the check exists to give (PRD I-0 §F1) — and ``.resolve()`` rewrites e.g.
    ``/tmp`` to its real target so the ephemeral-prefix match silently misses."""
    return Path(os.path.abspath(
        os.getenv("ASCLEPIUS_INGEST_DIR") or str(_default_ingest_dir())))


_EXECUTABLE_EXTS = (".exe", ".dll", ".so", ".sh", ".bat", ".cmd", ".ps1", ".msi",
                    ".jar", ".app", ".scr", ".com", ".vbs", ".js", ".py")


# ─── Purpose (PRD-I §2) ───────────────────────────────────────────────────────
# What an upload is FOR. Admin-side only: this vocabulary is imported by the admin
# router and the promotion gate, and by nothing a provider can reach.
PURPOSE_TASK_CREATION = "task_creation"
PURPOSE_BROKERING = "brokering"
#: Received, stored, and used for NOTHING until a person has looked at the file
#: and said what it is for. This is the DEFAULT, and it is the only one of the
#: three that is not a decision -- it is the absence of one, made explicit and
#: made safe. An upload sitting here is not in limbo; it is exactly where it is
#: supposed to be until somebody reads it.
PURPOSE_STORAGE = "storage"
PURPOSES = (PURPOSE_TASK_CREATION, PURPOSE_BROKERING, PURPOSE_STORAGE)

#: What an account, a link or an upload gets when nobody says otherwise.
DEFAULT_PURPOSE = PURPOSE_STORAGE

#: The only value that lets a case become a task or reach a model. Named as a
#: list of ONE so that adding a fourth purpose forces whoever adds it to decide
#: whether it belongs here, rather than inheriting promotability by default.
PROMOTABLE_PURPOSES = (PURPOSE_TASK_CREATION,)

# A row nobody has made this decision about. Rendered to the admin as a WORK
# ITEM, never quietly filled in — a row silently becoming task_creation on a
# screen is how brokering data would end up in a training bundle.
#
# It used to read "Purpose not set — legacy link", because when it was written
# the only way to reach this state was a row predating the column. That is no
# longer true: a health system that signs itself up is minted with this UNSET
# on purpose, so each of its uploads is resolved deliberately on the per-upload
# control. Calling that a legacy link tells the operator the wrong thing about
# the newest partner on the page.
PURPOSE_UNSET_LABEL = "Purpose not set"


def effective_purpose(value: Optional[str]) -> str:
    """Resolve a stored purpose for the PROMOTION GATE and nowhere else.

    NULL RESOLVES TO STORAGE. It used to resolve to task_creation, on the reading
    that this preserved what legacy links already did -- which was true, and was
    also the one place the system decided something consequential on a partner's
    behalf because nobody had decided it yet. An upload whose destination nobody
    has chosen could become a physician task and reach a third-party model, and
    the only thing standing in the way was an operator noticing a lime chip.

    Now it fails closed. Nothing is promoted, and nothing is sent to a model,
    until a person has read the file and said what it is for. The cost is real
    and is the point: rows that predate this stop being promotable until somebody
    resolves them, and the control that resolves one sits on the row itself.

    Confined to this one decision, as before: everywhere the admin can SEE, NULL
    stays NULL, so the difference between "held by default" and "somebody chose
    to hold this" is still legible.
    """
    return value if value in PURPOSES else DEFAULT_PURPOSE


def is_brokering(value: Optional[str]) -> bool:
    """Literally brokering. NOT the promotion gate -- see ``blocks_promotion``.

    Kept distinct because the two questions came apart when storage arrived: the
    brokering BUCKET, the export rules and the no-promotion-ever rule are about
    brokering specifically, while the gate is about everything that is not
    cleared for task creation."""
    return effective_purpose(value) == PURPOSE_BROKERING


def is_storage(value: Optional[str]) -> bool:
    """Held, awaiting a person. Includes NULL, which now means the same thing."""
    return effective_purpose(value) == PURPOSE_STORAGE


def blocks_promotion(value: Optional[str]) -> bool:
    """THE GATE. True unless this has been cleared for task creation.

    An allowlist, not a denylist. ``not is_brokering(...)`` was the old test and
    it was correct only while there were exactly two purposes -- the moment a
    third existed it silently admitted it, which is precisely how a file nobody
    had reviewed would have become a task.
    """
    return effective_purpose(value) not in PROMOTABLE_PURPOSES


def promotion_block_reason(value: Optional[str]) -> str:
    """Why this cannot be promoted, in words an operator can act on.

    ADMIN-FACING only -- every caller is behind require_admin. A refusal that
    does not name the control that lifts it is a refusal somebody files a bug
    about."""
    if is_brokering(value):
        return ("This is held for brokering. Brokering data is never promoted to "
                "tasks: the server refuses it.")
    return ("This has not been reviewed yet. Everything arrives as storage and "
            "stays there until you set what it is for; set it to task creation "
            "on this row and the promote controls open.")


# What ingest writes when nothing declared a specialty. It is a real value in the
# column, so a guard testing for an EMPTY string can never fire — which is how the
# promotion specialty check came to be unreachable.
UNDETERMINED_SPECIALTY = "general"


def specialty_is_undetermined(value: Optional[str]) -> bool:
    """True when nothing has actually decided this case's specialty.

    ``general`` is not a specialty, it is the absence of one. Promoting on it
    routes the case to a generic physician pool and labels the export with a
    value nobody chose — and a WRONG specialty is worse than a missing one,
    because it is invisible once the bundle ships."""
    return (value or "").strip().lower() in ("", UNDETERMINED_SPECIALTY)


class BundleRejected(ValueError):
    """The whole upload is unusable (not a zip, zip bomb, malware-scan fail,
    imaging-only). Recorded on the upload row with the reason."""


# ─── Raw storage (encrypted at rest) ─────────────────────────────────────────
# Two on-disk shapes, both read transparently by ``load_raw`` / ``iter_raw``:
#
#   * LEGACY single-blob — one ``field_crypto.encrypt_bytes`` output covering the
#     whole file. Simple, and every upload written before PRD-I looks like this.
#   * FRAMED (``ASCRAWF1``) — ``magic || repeat(uint32 len || frame) || uint32 0``
#     where each frame is an independent ``encrypt_bytes`` output over one plaintext
#     chunk. This is what makes multi-GB possible at all: the single-blob form
#     requires the entire file in RAM to encrypt AND again to decrypt, so a 3 GB
#     bundle OOMs a small container long before it reaches the unpacker. Framing
#     bounds peak memory at one chunk regardless of file size.
#
# Frames are individually authenticated (AES-GCM), so a tampered frame fails to
# decrypt. Frame ORDER and COUNT are not themselves authenticated by the container
# — the whole-file sha256 recorded at verification time is what covers those, and
# it is checked before the upload row is ever written (PRD-I §1.1).
_RAW_FRAME_MAGIC = b"ASCRAWF1"
_RAW_FRAME_BYTES = 8 * 1024 * 1024
# Generous ceiling on a frame as READ BACK: the plaintext frame plus AEAD nonce,
# tag and version header. Anything larger did not come from store_raw_stream.
_RAW_FRAME_MAX = _RAW_FRAME_BYTES + 4096


def _raw_path_for(upload_id: str) -> Path:
    return quarantine_root() / f"{upload_id}.zip.enc"


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def store_raw(upload_id: str, data: bytes) -> str:
    """Write the raw partner zip as an AES-GCM blob (field_crypto; passthrough
    only when no DATA_ENCRYPTION_KEY is configured — dev). 0700 dir, 0600 file."""
    from field_crypto import encrypt_bytes
    path = _raw_path_for(upload_id)
    path.write_bytes(encrypt_bytes(data))
    _chmod_600(path)
    return str(path)


def store_raw_stream(upload_id: str, chunks: Iterable[bytes]) -> str:
    """Write the raw partner bundle from a stream of plaintext chunks, encrypting
    frame by frame so peak memory is one frame rather than the whole file.

    Written to a ``.part`` file and atomically renamed, so a crash mid-assembly can
    never leave a truncated blob at the path an upload row would point at."""
    from field_crypto import encrypt_bytes
    path = _raw_path_for(upload_id)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(_RAW_FRAME_MAGIC)
            for chunk in chunks:
                if not chunk:
                    continue
                # RE-CHUNK to the container's own frame size. The caller's chunk
                # size is the caller's business — an upload part is 16 MB by
                # default, and emitting one frame per part coupled the container's
                # frame size to it. That coupling is what let the read ceiling
                # (_RAW_FRAME_MAX) and the write size drift apart: every part at
                # the shipped default was ~8.4 MB over what the reader would
                # accept, so every large bundle uploaded for hours, passed the
                # whole-file digest, was written to durable storage, and was then
                # rejected as unreadable — with copy that blamed the hospital's
                # file. The container decides its own framing; nothing a caller
                # does can make it self-inconsistent.
                view = memoryview(bytes(chunk))
                for i in range(0, len(view), _RAW_FRAME_BYTES):
                    frame = encrypt_bytes(bytes(view[i:i + _RAW_FRAME_BYTES]))
                    fh.write(struct.pack(">I", len(frame)))
                    fh.write(frame)
            fh.write(struct.pack(">I", 0))
            fh.flush()
            os.fsync(fh.fileno())
        _chmod_600(tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _chmod_600(path)
    return str(path)


def iter_raw(raw_path: str, *, chunk_size: int = _RAW_FRAME_BYTES) -> Iterator[bytes]:
    """Yield the decrypted raw bundle in bounded chunks, for either on-disk shape.

    The legacy single-blob form cannot stream (the AES-GCM tag covers the whole
    ciphertext, so it is one decrypt), and is yielded as a single chunk — those
    blobs predate chunked upload and are bounded by the old 100 MB request cap."""
    from field_crypto import decrypt_bytes
    with open(raw_path, "rb") as fh:
        magic = fh.read(len(_RAW_FRAME_MAGIC))
        if magic != _RAW_FRAME_MAGIC:
            fh.seek(0)
            yield decrypt_bytes(fh.read()) or b""
            return
        while True:
            header = fh.read(4)
            if len(header) < 4:
                raise ValueError("raw bundle is truncated (incomplete frame header)")
            (length,) = struct.unpack(">I", header)
            if length == 0:
                return
            if length > _RAW_FRAME_MAX:
                # The length is a uint32 read from the file, and read(n)
                # PREALLOCATES n bytes — so a single flipped byte in a length
                # field turns the truncation error below into a 4 GiB allocation
                # and an OOM kill of the worker. The writer never emits a frame
                # larger than _RAW_FRAME_BYTES plus AEAD overhead, so anything
                # above the ceiling is corruption by definition and is reported
                # as corruption instead of being allocated for.
                raise ValueError(
                    f"raw bundle frame length {length} exceeds the maximum "
                    f"{_RAW_FRAME_MAX}: the file is corrupt")
            frame = fh.read(length)
            if len(frame) != length:
                raise ValueError("raw bundle is truncated (incomplete frame body)")
            plain = decrypt_bytes(frame) or b""
            # Re-chunk to the caller's size so a large frame does not force a large
            # allocation downstream.
            for i in range(0, len(plain), chunk_size):
                yield plain[i:i + chunk_size]


def load_raw(raw_path: str) -> bytes:
    """The whole decrypted bundle as one ``bytes``. Retained for callers that need
    it in memory (the admin download path). Prefer ``iter_raw`` /
    ``decrypted_copy`` on any path that can see a multi-GB bundle."""
    return b"".join(iter_raw(raw_path))


def ensure_zip_on_disk(path: str, *, filename: Optional[str] = None) -> Tuple[str, bool]:
    """(path_to_a_zip, wrapped). Wraps a bare clinical file into a one-entry zip.

    The chunked door streams whatever the partner selected, which may be a bare
    ``.json`` / ``.csv`` / ``.hl7`` / ``.txt`` — exactly what the single-request
    doors accept, because ``wrap_loose_files`` packs those server-side. Without
    the same treatment here, the SAME file would succeed through one door and be
    rejected as "not a zip" through the other, which is precisely the drift
    ``wrap_loose_files`` was extracted to prevent.

    Done at unpack time rather than at upload time on purpose: the stored raw blob
    stays byte-for-byte what the partner sent, so the sha256 on the upload row is a
    digest of THEIR file and remains something you can ask them to verify."""
    with open(path, "rb") as fh:
        if fh.read(2) == b"PK":
            return path, False
    name = os.path.basename((filename or "upload.dat").replace("\\", "/")) or "upload.dat"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".") or "upload.dat"
    fd, wrapped = tempfile.mkstemp(prefix="wrapped-", suffix=".zip",
                                   dir=str(quarantine_root()))
    os.close(fd)
    with zipfile.ZipFile(wrapped, "w", zipfile.ZIP_DEFLATED) as z, open(path, "rb") as src:
        with z.open(name, "w") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
    _chmod_600(Path(wrapped))
    return wrapped, True


@contextlib.contextmanager
def decrypted_copy(raw_path: str) -> Iterator[str]:
    """Materialize the decrypted bundle to a private temp file and yield its path.

    The unpacker needs random access (zip central directory lives at the end), so
    a seekable file is required; doing it on disk instead of in RAM is what keeps
    memory flat on a multi-GB bundle. The file is 0600 inside the 0700 quarantine
    root and removed unconditionally — plaintext PHI exists on disk only for the
    duration of the unpack, which is strictly less exposure than the previous
    behaviour of holding the same plaintext in the process heap."""
    root = quarantine_root()
    fd, tmp = tempfile.mkstemp(prefix="unpack-", suffix=".zip", dir=str(root))
    try:
        with os.fdopen(fd, "wb") as fh:
            for chunk in iter_raw(raw_path):
                fh.write(chunk)
        _chmod_600(Path(tmp))
        yield tmp
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def delete_raw(raw_path: Optional[str]) -> None:
    """Best-effort removal of a raw blob (cleanup when a one-time claim is lost
    after the bytes were already written). Never raises."""
    if not raw_path:
        return
    try:
        Path(raw_path).unlink()
    except OSError:
        pass


# Filesystems where a redeploy/restart wipes the data — never durable for the
# raw partner bundle (this is what caused the "download failed (410)" incident:
# blobs on /tmp vanished on redeploy while the DB row survived). Imported from
# constants so assets.py, constants.py and this module cannot drift (PRD I-0 §F1).
from asclepius.constants import EPHEMERAL_PREFIXES as _EPHEMERAL_PREFIXES  # noqa: E402


def ingest_storage_durable() -> Tuple[bool, str]:
    """(ok, detail) — is the raw ingest dir safe to keep partner bundles in?

    Two signals: (1) the dir must not sit on an ephemeral, redeploy-wiped path;
    (2) it should live on the same volume (device) as the DB, so a blob is
    exactly as durable as its row. (1) is fail-closed-worthy; (2) is a warning
    (a deliberately separate durable mount is legitimate)."""
    from asclepius.constants import (
        VOLUME_MOUNT_ENV, declared_volume_mount, path_under_declared_volume,
    )

    # A durability check must never touch the filesystem: quarantine_root() would
    # mkdir the probe path (raising on a root-owned /run) and .resolve() it (hiding
    # an ephemeral /tmp behind its real target). _ingest_root_path() does neither.
    root = _ingest_root_path()
    root_str = str(root)
    # A declared volume mount beats the prefix list, which cannot tell a real
    # volume at /data from a container-local directory of the same name.
    if path_under_declared_volume(root_str) is False:
        return False, (
            f"raw ingest dir {root_str} is NOT under the persistent volume this "
            f"platform mounted at {declared_volume_mount()} ({VOLUME_MOUNT_ENV}); a "
            "redeploy will delete partner uploads. Set ASCLEPIUS_INGEST_DIR to a "
            "path inside that mount."
        )
    for pre in _EPHEMERAL_PREFIXES:
        if root_str == pre or root_str.startswith(pre + "/"):
            return False, (
                f"raw ingest dir {root_str} is on ephemeral storage ({pre}); "
                "a redeploy will delete partner uploads. Set ASCLEPIUS_INGEST_DIR "
                "to a path on your persistent volume (e.g. beside ASCLEPIUS_DB_PATH)."
            )
    db_path = os.getenv("ASCLEPIUS_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "asclepius.db")
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "/"
    try:
        if os.stat(root_str).st_dev != os.stat(db_dir).st_dev:
            return True, (
                f"raw ingest dir {root_str} is on a different volume than the DB "
                f"({db_dir}); confirm that volume is persistent, or move the ingest "
                "dir next to the DB so raw blobs share its durability."
            )
    except OSError:
        pass
    return True, f"raw ingest dir {root_str} is on the DB's volume"


def purge_expired_raw(store: Any) -> int:
    """Delete raw blobs older than the retention window (PRD §4: we keep the
    derived case, not the partner file). Called opportunistically on ingestion
    activity — no cron needed at pod scale. Returns files deleted."""
    cutoff = time.time() - raw_retention_days() * 86400
    # Retain-raw (Audit §9.4): an upload whose entries ALL failed to parse keeps its
    # raw blob past the window so it can be re-run after a parser fix. Skip those paths.
    retained: set = set()
    try:
        for u in store.list_uploads_with_retained_raw():
            rp = u.get("raw_path")
            if rp:
                retained.add(os.path.basename(rp))
    except Exception:  # pragma: no cover - defensive; never block a purge on this
        retained = set()
    deleted = 0
    for p in quarantine_root().glob("*.zip.enc"):
        try:
            if p.name in retained:
                continue
            if p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except OSError:
            continue
    if deleted:
        store.log_event(entity_type="ingest", event_type="raw_purged",
                        payload={"deleted": deleted, "retention_days": raw_retention_days()})
    purge_stale_scratch()
    purge_orphan_raw(store)
    return deleted


def orphan_raw_grace_hours() -> int:
    """How long a blob may exist with no row before it counts as orphaned.

    Generous on purpose. The window between writing the bytes and inserting the
    row is milliseconds, so anything recent is overwhelmingly likely to be an
    upload mid-insert rather than an orphan — and deleting one of those destroys a
    verified bundle at the exact moment it becomes real."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_ORPHAN_RAW_GRACE_HOURS", "24")))
    except ValueError:
        return 24


def purge_orphan_raw(store: Any) -> int:
    """Delete raw blobs that NO upload row references (Audit M1).

    ``purge_expired_raw`` walks database rows, so a blob whose row was never
    written is invisible to it — and a crash between ``complete()`` (which writes
    the bytes) and ``insert_ingest_upload`` leaves exactly that. The result was
    encrypted PHI sitting on the durable volume forever, referenced by nothing and
    counted by nothing, which is an accounting problem before it is a disk one.

    Deliberately conservative in the dangerous direction: a blob is removed only
    when it is older than the grace window AND no row of any status names it. A
    wrongly-deleted blob is a bundle the partner has to re-send; an orphan kept one
    day too long costs disk."""
    try:
        referenced = {os.path.basename(u.get("raw_path") or "")
                      for u in store.list_ingest_uploads(limit=1000000)}
    except Exception as exc:  # pragma: no cover - never delete on a failed read
        log.warning("orphan raw sweep skipped: could not list uploads: %s", exc)
        return 0
    referenced.discard("")
    cutoff = time.time() - orphan_raw_grace_hours() * 3600
    deleted = 0
    for p in quarantine_root().glob("*.zip.enc"):
        try:
            if p.name in referenced or p.stat().st_mtime >= cutoff:
                continue
            p.unlink()
            deleted += 1
        except OSError:
            continue
    if deleted:
        with contextlib.suppress(Exception):
            store.log_event(entity_type="ingest", event_type="orphan_raw_purged",
                            payload={"deleted": deleted})
        log.warning("released %d raw blob(s) that no upload row referenced, a crash "
                    "between assembly and row insert leaves these behind", deleted)
    return deleted


# Scratch that ``process_upload`` normally removes itself: the decrypted archive
# copy and the spilled entry bytes. Both are PLAINTEXT PHI, so a crash between
# creating one and releasing it must not leave it lying around indefinitely. Six
# hours is far longer than any ingest and far shorter than a retention window.
_SCRATCH_PREFIXES = ("entries-", "unpack-", "unpack-mem-")
_SCRATCH_MAX_AGE_SEC = 6 * 3600


def purge_stale_scratch() -> int:
    """Remove ingest scratch left behind by an interrupted run. Never raises."""
    cutoff = time.time() - _SCRATCH_MAX_AGE_SEC
    removed = 0
    try:
        root = quarantine_root()
    except Exception:  # pragma: no cover - defensive
        return 0
    for p in root.iterdir() if root.exists() else []:
        if not p.name.startswith(_SCRATCH_PREFIXES):
            continue
        try:
            if p.stat().st_mtime >= cutoff:
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# Non-terminal upload states: the pipeline was mid-flight. A redeploy kills the
# in-process BackgroundTask, so without recovery these would sit stuck forever.
_NON_TERMINAL_UPLOAD_STATUSES = ["received", "scanning", "parsing"]


def recover_interrupted_uploads(store: Any) -> int:
    """Re-run the pipeline for uploads left mid-flight by a crash/redeploy.

    The raw blob is durable (persistent volume), so reprocessing is lossless.
    We clear each upload's un-promoted cases first, so a partially-processed
    upload reprocesses cleanly instead of double-inserting cases. An upload
    whose raw blob is genuinely gone is marked rejected (never left dangling).
    Returns the number of uploads re-enqueued/handled. Best-effort; never raises."""
    handled = 0
    try:
        stuck = store.list_uploads_in_status(_NON_TERMINAL_UPLOAD_STATUSES)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("ingest recovery: could not list interrupted uploads: %s", exc)
        return 0
    for upload in stuck:
        uid = upload.get("upload_id")
        raw_path = upload.get("raw_path")
        try:
            if not raw_path or not os.path.exists(raw_path):
                store.update_ingest_upload(
                    uid, status="rejected",
                    reason="raw upload was lost before processing completed "
                           "(interrupted by a restart); ask the partner to re-upload")
                store.log_event(entity_type="ingest_upload", entity_id=uid,
                                event_type="upload_recovery_failed",
                                payload={"reason": "raw blob missing"})
                try:
                    from asclepius import ingest_notify
                    ingest_notify.notify_upload_failed(
                        store, store.get_ingest_upload(uid), outcome="lost")
                except Exception:  # pragma: no cover - defensive
                    pass
                handled += 1
                continue
            removed = store.delete_unpromoted_ingest_cases(uid)
            store.log_event(entity_type="ingest_upload", entity_id=uid,
                            event_type="upload_recovery_requeued",
                            payload={"prior_status": upload.get("status"),
                                     "cleared_cases": removed})
            process_upload(store, uid)
            handled += 1
        except Exception as exc:  # pragma: no cover - defensive per-upload
            log.warning("ingest recovery: upload %s failed to reprocess: %s", uid, exc)
    # Terminal-state reconciliation (Audit §9.3): recover_interrupted_uploads only
    # revisits NON-terminal uploads; this catches cases that reached a terminal state
    # but are internally inconsistent (unbound sealed key, missing asset blob).
    reconcile_ingested_cases(store)
    if handled:
        log.info("ingest recovery: handled %d interrupted upload(s)", handled)
    return handled


def reconcile_ingested_cases(store: Any) -> Dict[str, Any]:
    """Reconcile TERMINAL cases that are internally inconsistent (Audit §9.3). Two
    defects the ingest-time checks cannot see because they develop after the fact:
    a sealed key left unbound by a crash (§H1), and an asset blob that has since gone
    missing/corrupt on disk (§P2). Both hold the affected case for admin review.
    Runs at startup (via recover_interrupted_uploads) and can be scheduled nightly.
    Best-effort; never raises. Returns counts for the admin ingestion card."""
    out: Dict[str, Any] = {"sealed_bound": 0, "sealed_orphans": 0,
                           "assets_missing": 0, "assets_corrupt": 0, "cases_held": 0}
    try:
        rec = store.reconcile_sealed_ground_truth()
        out["sealed_bound"] = rec.get("bound", 0)
        out["sealed_orphans"] = len(rec.get("orphans") or [])
        out["cases_held"] += out["sealed_bound"]
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("sealed reconciliation failed: %s", exc)
    try:
        from asclepius import assets
        rep = assets.verify_case_assets(store)
        for m in (rep.get("missing") or []) + (rep.get("corrupt") or []):
            cid = m.get("ingest_case_id")
            if cid and store.hold_ingest_case_for_review(
                    cid, m.get("reason") or "asset_blob_missing",
                    m.get("detail") or "asset blob is missing or corrupt on disk"):
                out["cases_held"] += 1
        out["assets_missing"] = len(rep.get("missing") or [])
        out["assets_corrupt"] = len(rep.get("corrupt") or [])
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("asset reconciliation failed: %s", exc)
    if out["cases_held"] or out["sealed_orphans"]:
        log.info("ingested-case reconciliation: %s", out)
    return out


# ─── Malware scan hook ────────────────────────────────────────────────────────
# A scan that could not reach a verdict. NOT the same answer as "this is
# dangerous", and the pipeline must not collapse the two: a detection is a hard
# rejection with copy telling the partner their file was refused, while an
# inconclusive scan is our problem and holds the bundle for a human.
_SCAN_INCONCLUSIVE = "scan-inconclusive: "


def scan_was_inconclusive(detail: Optional[str]) -> bool:
    return bool(detail and detail.startswith(_SCAN_INCONCLUSIVE))


def malware_scan_max_bytes() -> int:
    """Above this, inline scanning is SKIPPED and the object is left to the
    post-``verified`` worker (PRD-I §1.3).

    The PRD is explicit that AV does not belong inline, and the reason is
    arithmetic: ClamAV's own scan-size limits fight multi-GB files, its
    "unlimited" configuration OOMs a small container, and any scanner that cannot
    finish inside its timeout turns a clean hospital bundle into a rejection. 512 MB
    is comfortably scannable inline; past it the answer is 'later', not 'no'."""
    try:
        return max(0, int(os.getenv("ASCLEPIUS_MALWARE_SCAN_MAX_BYTES",
                                    str(512 * 1024 * 1024))))
    except ValueError:
        return 512 * 1024 * 1024


def malware_scan_timeout_sec(size_bytes: int = 0) -> int:
    """Timeout scaled to the bytes actually being scanned.

    A flat 120 s was a rejection generator: it is generous for a 4 MB CSV and
    impossible for a 500 MB bundle, so the same configuration meant 'scan' for
    one partner and 'refuse' for another. Roughly 20 MB/s with a 60 s floor."""
    try:
        base = max(1, int(os.getenv("ASCLEPIUS_MALWARE_SCAN_TIMEOUT_SEC", "0")))
        if os.getenv("ASCLEPIUS_MALWARE_SCAN_TIMEOUT_SEC"):
            return base
    except ValueError:
        pass
    return max(60, int(size_bytes / (20 * 1024 * 1024)) + 60)


def malware_scan(path: str) -> Tuple[bool, str]:
    """(ok, detail). With ASCLEPIUS_MALWARE_SCAN_CMD set (e.g. ``clamscan
    --no-summary``), the command runs against the file and non-zero rejects.
    Without it, the baseline is structural zip validation only — honest floor,
    not an AV engine.

    Three outcomes, not two. Clean, FLAGGED (a detection — hard rejection), and
    INCONCLUSIVE (timeout, missing binary, crash). Inconclusive still fails
    closed, but it is marked so the caller can hold the bundle for a human rather
    than telling a hospital their file was malware when it was our scanner that
    could not finish."""
    cmd = (os.getenv("ASCLEPIUS_MALWARE_SCAN_CMD") or "").strip()
    if not cmd:
        return True, "baseline (structural checks only; set ASCLEPIUS_MALWARE_SCAN_CMD for AV)"
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    ceiling = malware_scan_max_bytes()
    if ceiling and size > ceiling:
        # PRD-I §1.3: AV belongs in a separate worker against stored objects after
        # `verified`. Skipping here is that policy, not a gap.
        return True, (f"deferred to the post-verified scan worker "
                      f"({size} bytes over the {ceiling} inline ceiling)")
    timeout = malware_scan_timeout_sec(size)
    try:
        res = subprocess.run(shlex.split(cmd) + [path], capture_output=True,
                             timeout=timeout)
        if res.returncode != 0:
            return False, f"malware scan flagged the upload (exit {res.returncode})"
        return True, "scanned clean"
    except subprocess.TimeoutExpired:
        return False, (f"{_SCAN_INCONCLUSIVE}the scanner did not finish within "
                       f"{timeout}s on {size} bytes")
    except Exception as exc:
        # Fail CLOSED: a configured scanner that cannot run means we cannot claim
        # the file is safe — but it is OUR failure, not a finding about their file.
        return False, f"{_SCAN_INCONCLUSIVE}malware scanner unavailable ({exc})"


# ─── Loose-file wrapping — single source of truth for BOTH upload doors ───────
# (Buyer Response PRD §2 A1). The magic-link door (routers/asclepius.py
# partner_upload) and the account door (routers/asclepius_provider.py
# provider_upload) used to disagree: the account door WRAPPED loose files into a
# zip, while the link door REJECTED anything whose first two bytes were not the
# ``PK`` zip magic. That meant the exact partner file we mailed a health-system
# succeeded or failed depending only on which URL we happened to send. This is the
# one packing implementation both doors call before ``store_raw``, so they cannot
# drift again.
# The ZIP format stores a modification time per entry, and ``writestr`` given a
# plain string stamps it with ``time.localtime()`` — at the format's two-second
# resolution. That makes the packer NON-DETERMINISTIC across a two-second tick,
# which is not a cosmetic detail here: ``routers/asclepius_provider`` wraps an
# upload and then takes ``sha256_hex`` OF THE WRAPPED BYTES, so the same upload
# submitted twice hashed differently and anything keyed on that digest silently
# failed to match. It also made ``test_both_upload_doors_identical`` fail on a
# clock boundary rather than on a defect — the two doors had not drifted, the
# second between them had.
#
# A fixed date is the standard fix (it is what reproducible-build tooling does).
# 1980-01-01 because it is the earliest instant the format can express, so it
# reads as "deliberately none" rather than as a real timestamp. Everything else
# is set to exactly what ``writestr`` would have set, so only the clock changes.
_ZIP_FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def _zip_entry(name: str) -> "zipfile.ZipInfo":
    info = zipfile.ZipInfo(filename=name, date_time=_ZIP_FIXED_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def wrap_loose_files(files: List[Dict[str, Any]], *, specialty: Optional[str]) -> bytes:
    """Loose partner files -> one zip for the shared pipeline.

    A genuinely-zip single upload passes through untouched (keyed on the ``PK``
    magic bytes, NOT the extension, so a mis-named ``.zip`` that is really a CSV
    still gets wrapped instead of failing the unpacker). Everything else is
    packed, with a synthesized ``manifest.json`` carrying the specialty when the
    bundle does not already include one.

    Each file is ``{"filename": str, "content": bytes}``. Extracted from
    routers/asclepius_provider.py so the magic-link door and the account door
    cannot drift again.
    """
    if len(files) == 1 and (files[0].get("content") or b"")[:2] == b"PK":
        return files[0]["content"]
    has_manifest = any(
        os.path.basename((f.get("filename") or "")).lower() == "manifest.json"
        for f in files
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.writestr(_zip_entry(os.path.basename(f.get("filename") or "") or "file"),
                       f.get("content") or b"")
        if not has_manifest and specialty:
            z.writestr(_zip_entry("manifest.json"), json.dumps({"specialty": specialty}))
    return buf.getvalue()


# Partner-facing copy for an upload we genuinely cannot read (used by both doors).
# A hospital IT team must be told what to DO, not that the "zip magic bytes" were
# wrong — a message that means nothing to them.
UNREADABLE_UPLOAD_MESSAGE = (
    "We could not read this upload. Send a .zip, or individual .json / .csv / "
    ".hl7 / .txt files and we will package them. If the problem persists, contact "
    "your Archangel Health point of contact and we will take it by secure transfer."
)


# ─── Unpack + classify (PRD §5) ───────────────────────────────────────────────
def _classify(name: str, head: bytes, text_head: str) -> str:
    lower = name.lower()
    base = os.path.basename(lower)
    if head[:4] == b"DICM" or head[128:132] == b"DICM" or lower.endswith(".dcm"):
        return "dicom"
    if base == "manifest.json":
        return "manifest"
    if lower.endswith((".json",)):
        return "fhir_r4" if '"resourceType"' in text_head and '"Bundle"' in text_head else "unsupported"
    if lower.endswith((".hl7", ".oru")) or text_head.startswith("MSH|"):
        return "hl7v2"
    if lower.endswith((".csv", ".tsv")):
        return "lab_csv"
    if lower.endswith((".txt", ".md", ".note")):
        return "note_text"
    return "unsupported"


# Archive members we will not open. There is exactly ONE level of nesting in this
# format (the bundle itself), which is the nesting-depth cap in PRD-I §1.3 stated
# as a rule rather than a counter: nothing inside a bundle is ever extracted as an
# archive, so recursive-bomb depth cannot exceed one by construction.
_ARCHIVE_EXTS = (".zip", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst")
_ARCHIVE_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"7z\xbc\xaf", b"Rar!", b"\xfd7zXZ")

# A single-member gzip is a COMPRESSED FILE, not an archive: it has no directory
# and cannot contain another entry, so decompressing it once adds no recursion
# depth and the nesting rule above still holds at one level. It is carved out
# because a FHIR bulk export ships ``*.ndjson.gz`` — sweeping it into the
# nested-archive rejection refused the single most standard way a hospital
# exports at scale. The bytes it produces are counted against exactly the same
# per-entry cap, ratio cap and archive budget as any other entry.
_GZIP_MAGIC = b"\x1f\x8b"
_GZIP_EXTS = (".gz",)


class _OutputBudgetExceeded(Exception):
    """Total decompressed output crossed the budget mid-stream."""


class _SpilledEntry(dict):
    """A bundle entry whose decompressed bytes live on disk until asked for.

    Streaming each entry out of the zip bounded the memory cost of ONE member, but
    the entries were then all retained in a list — so a 3 GB bundle of ordinary
    1 MB clinical files still held 3 GB, and the per-entry cap that looked like the
    memory bound was doing nothing at the whole-bundle level. Spilling makes the
    real bound one entry at a time, which is what §1.3's "never load the archive or
    a member fully into memory" actually requires.

    Behaves like the plain dict every existing caller expects: ``e["data"]`` and
    ``e.get("data")`` both return the bytes, read fresh each time rather than
    cached — caching would restore exactly the retention this exists to remove."""

    __slots__ = ("_spill_path",)

    def __init__(self, *args: Any, spill_path: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._spill_path = spill_path

    def __getitem__(self, key: str) -> Any:
        if key == "data":
            with open(self._spill_path, "rb") as fh:
                return fh.read()
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        if key == "data":
            # NOT swallowed. A missing spill file means the staging directory was
            # released while an entry was still live, and returning ``default``
            # would make a PHI pipeline read that entry as EMPTY and carry on —
            # silent data loss, which is the worst failure mode available here. A
            # raise is loud and recoverable; ``process_upload`` already turns a
            # per-entry failure into a readable quarantine.
            return self["data"]
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key == "data" or super().__contains__(key)


def _read_entry_streamed(zf: zipfile.ZipFile, info: zipfile.ZipInfo,
                         *, remaining_budget: int,
                         spend: Optional[List[int]] = None,
                         ) -> Tuple[Optional[bytes], Optional[str]]:
    """Decompress ONE entry with real byte accounting. Returns (data, reject_reason).

    The declared ``info.file_size`` is attacker-controlled and is therefore used for
    nothing here. Bytes are counted as they are produced and the read is abandoned
    the moment any ceiling is crossed, so a bomb costs us the chunk in flight rather
    than the whole expansion. Raises ``_OutputBudgetExceeded`` when the WHOLE-ARCHIVE
    budget is gone — that condemns the bundle, not just this entry."""
    entry_cap = max_entry_bytes()
    ratio_cap = entry_compression_ratio_cap()
    # A stored (uncompressed) entry has ratio 1 by definition; only guard entries
    # that actually claim compression, and floor the divisor so a 0-byte compressed
    # size cannot divide by zero.
    ratio_allowance = max(1, int(ratio_cap * max(1, info.compress_size)))
    out = io.BytesIO()
    written = 0
    try:
        with zf.open(info, "r") as src:
            while True:
                chunk = src.read(262144)
                if not chunk:
                    break
                written += len(chunk)
                if written > entry_cap:
                    return None, f"entry too large (over {entry_cap} bytes decompressed)"
                if written > ratio_allowance:
                    return None, (f"compression ratio over {int(ratio_cap)}:1 "
                                  "(zip-bomb defense)")
                if written > remaining_budget:
                    raise _OutputBudgetExceeded(
                        f"decompressed output exceeded the bundle budget mid-extraction "
                        f"(zip-bomb defense)")
                out.write(chunk)
    finally:
        # Charge the budget for EVERY byte decompressed, including an entry that
        # was then rejected. Debiting only on the accept path left total work
        # bounded by the per-entry ratio cap rather than the whole-archive budget:
        # 5000 entries each expanding to the 64 MB entry cap before rejection is
        # ~320 GB of deflate in one background task, with the budget never moving.
        # Memory stayed bounded; CPU and wall-clock did not.
        if spend is not None:
            spend[0] = written
    return out.getvalue(), None


def _gunzip_bounded(data: bytes, *, remaining_budget: int,
                    ) -> Tuple[Optional[bytes], Optional[str]]:
    """Decompress a gzip member with the SAME produced-byte accounting as the zip
    reader. Without this the ``.gz`` carve-out would be the hole that the archive
    rejection was closing."""
    import gzip
    import zlib

    entry_cap = max_entry_bytes()
    ratio_allowance = max(1, int(entry_compression_ratio_cap() * max(1, len(data))))
    out = io.BytesIO()
    written = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            while True:
                chunk = gz.read(262144)
                if not chunk:
                    break
                written += len(chunk)
                if written > entry_cap:
                    return None, f"entry too large (over {entry_cap} bytes decompressed)"
                if written > ratio_allowance:
                    return None, (f"compression ratio over "
                                  f"{int(entry_compression_ratio_cap())}:1 "
                                  "(zip-bomb defense)")
                if written > remaining_budget:
                    raise _OutputBudgetExceeded(
                        "decompressed output exceeded the bundle budget "
                        "mid-extraction (zip-bomb defense)")
                out.write(chunk)
    except _OutputBudgetExceeded:
        raise
    except (OSError, EOFError, zlib.error) as exc:
        return None, f"unreadable gzip member ({exc})"
    return out.getvalue(), None


def unpack_bundle_from_path(zip_path: str, *, spill: bool = True) -> Dict[str, Any]:
    """Zip on disk → classified entries, with zip-bomb + path-traversal defense.

    Path-based and streaming on purpose (PRD-I §1.3). The previous byte-based
    implementation had two defects that only appear at scale: it trusted the
    header-declared ``file_size`` sum as its bomb check (attacker-controlled, so no
    check at all), and ``zf.read()`` materialized each member in full BEFORE any
    ceiling could apply — a member declaring 100 bytes and decompressing to 10 GB
    exhausted memory before the size assertion it would eventually have failed.
    Here every ceiling is enforced against bytes actually produced, mid-write.

    With ``spill`` (the default) each entry's bytes go to a private staging dir and
    are read back on access, so the whole-bundle memory cost is one entry rather
    than the sum of all of them. **The caller MUST invoke the returned ``cleanup``**
    — ``process_upload`` does so in a ``finally``. ``spill=False`` returns the bytes
    inline for callers that already hold the whole archive in memory anyway."""
    try:
        archive_bytes = os.path.getsize(zip_path)
    except OSError as exc:
        raise BundleRejected(f"unreadable archive: {exc}") from exc
    with open(zip_path, "rb") as probe:
        if probe.read(2) != b"PK":
            raise BundleRejected("not a zip archive (bad magic bytes)")

    entries: List[Dict[str, Any]] = []
    manifest: Dict[str, Any] = {}
    budget = total_output_budget(archive_bytes)
    staging = tempfile.mkdtemp(prefix="entries-", dir=str(quarantine_root())) if spill else None

    def _cleanup() -> None:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)

    def _hold(index: int, base: Dict[str, Any], data: bytes) -> Dict[str, Any]:
        if not staging:
            return {**base, "data": data}
        path = os.path.join(staging, f"e{index:06d}")
        with open(path, "wb") as fh:
            fh.write(data)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return _SpilledEntry(base, spill_path=path)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > max_entries():
                raise BundleRejected(f"too many entries ({len(infos)} > {max_entries()})")
            for index, info in enumerate(infos):
                name = info.filename
                # Path traversal / absolute paths: reject the ENTRY, keep the bundle.
                if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                    entries.append({"name": name, "kind": "rejected",
                                    "reason": "path traversal"})
                    continue
                lower = name.lower()
                if any(lower.endswith(ext) for ext in _EXECUTABLE_EXTS):
                    entries.append({"name": name, "kind": "rejected",
                                    "reason": "executable entry"})
                    continue
                if any(lower.endswith(ext) for ext in _ARCHIVE_EXTS):
                    entries.append({"name": name, "kind": "rejected",
                                    "reason": "nested archive (not extracted)"})
                    continue
                spend = [0]
                data, reject = _read_entry_streamed(zf, info, remaining_budget=budget,
                                                    spend=spend)
                budget -= spend[0]
                if reject is not None:
                    entries.append({"name": name, "kind": "rejected", "reason": reject})
                    if budget <= 0:
                        raise _OutputBudgetExceeded(
                            "decompressed output exceeded the bundle budget "
                            "(zip-bomb defense)")
                    continue
                assert data is not None
                if data[:4] in _ARCHIVE_MAGICS:
                    entries.append({"name": name, "kind": "rejected",
                                    "reason": "nested archive (not extracted)"})
                    continue
                if data[:2] == _GZIP_MAGIC or lower.endswith(_GZIP_EXTS):
                    data, reject = _gunzip_bounded(data, remaining_budget=budget)
                    if reject is not None:
                        entries.append({"name": name, "kind": "rejected",
                                        "reason": reject})
                        continue
                    budget -= len(data)
                    # Classify on the INNER name: export.ndjson.gz is ndjson.
                    name_for_kind, lower = name[:-3], lower[:-3]
                else:
                    name_for_kind = name
                head = data[:512]
                text_head = head.decode("utf-8", errors="replace").lstrip()[:200]
                kind = _classify(name_for_kind,
                                 data[:256] if len(data) < 512 else data[:512],
                                 text_head)
                if kind == "manifest":
                    try:
                        manifest = json.loads(data.decode("utf-8", errors="replace"))
                        entries.append({"name": name, "kind": "manifest"})
                    except Exception:
                        entries.append({"name": name, "kind": "rejected",
                                        "reason": "unparseable manifest.json"})
                    continue
                entries.append(_hold(index, {"name": name, "kind": kind}, data))
                del data  # the spilled copy is the one that survives this loop
    except _OutputBudgetExceeded as exc:
        _cleanup()
        raise BundleRejected(str(exc)) from exc
    except zipfile.BadZipFile as exc:
        _cleanup()
        raise BundleRejected(f"corrupt zip: {exc}") from exc
    except BaseException:
        _cleanup()
        raise
    return {"entries": entries, "manifest": manifest if isinstance(manifest, dict) else {},
            "cleanup": _cleanup}


def unpack_bundle(zip_bytes: bytes) -> Dict[str, Any]:
    """In-memory convenience wrapper over ``unpack_bundle_from_path``.

    ONE unpacking implementation, reached two ways — the caps and the classification
    cannot drift between the byte and path callers. Callers that can see a large
    bundle should use the path form (or ``decrypted_copy``) directly."""
    if not zip_bytes or zip_bytes[:2] != b"PK":
        raise BundleRejected("not a zip archive (bad magic bytes)")
    root = quarantine_root()
    fd, tmp = tempfile.mkstemp(prefix="unpack-mem-", suffix=".zip", dir=str(root))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(zip_bytes)
        _chmod_600(Path(tmp))
        # spill=False: this caller already holds the whole archive in memory, so
        # spilling would buy nothing and would hand back entries whose backing
        # files this function is about to delete.
        return unpack_bundle_from_path(tmp, spill=False)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


# ─── Assembly (PRD §3) ────────────────────────────────────────────────────────
def _merge_fragments(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge per-file fragments into ONE case's fragments: lists concatenate,
    demographics/vitals merge (first non-empty wins per key), the latest
    ``_index_event`` wins.

    ``studies`` and ``source_refs`` (Buyer Response PRD §2 A2/A3) are model-facing
    case fields and MUST be carried here — without the ``studies`` key, adapter
    studies were silently dropped during bundle assembly (the bug that would make
    A2 look fixed in a unit test and still broken in production). Answer-adjacent
    metadata (sealed key, eval task, case provenance) is carried under underscore
    keys so ``deidentify``/``_strip_meta`` keep it out of the model-visible body."""
    out: Dict[str, Any] = {"demographics": {}, "lab_panels": [], "notes": [],
                           "medications": [], "problem_list": [], "vitals": {},
                           "studies": [], "source_refs": []}
    index_event = None
    sealed = None
    eval_task = None
    case_provenance = None
    synthetic_declared = False
    for p in parts:
        for k in ("lab_panels", "notes", "medications", "problem_list", "studies",
                  "source_refs"):
            out[k].extend(p.get(k) or [])
        for k, v in (p.get("demographics") or {}).items():
            out["demographics"].setdefault(k, v)
        for k, v in (p.get("vitals") or {}).items():
            out["vitals"].setdefault(k, v)
        ie = p.get("_index_event")
        if ie and (index_event is None or str(ie) > str(index_event)):
            index_event = ie
        if p.get("_sealed_ground_truth") and sealed is None:
            sealed = p["_sealed_ground_truth"]
        if p.get("_eval_task") and eval_task is None:
            eval_task = p["_eval_task"]
        if p.get("_case_provenance") and case_provenance is None:
            case_provenance = p["_case_provenance"]
        synthetic_declared = synthetic_declared or bool(p.get("_synthetic_declared"))
        # Adapter warnings (e.g. "no collection-date column matched") ride to the
        # ingest report so the operator sees the parser gap instead of a green row.
        for w in p.get("_adapter_warnings") or []:
            out.setdefault("_adapter_warnings", []).append(w)
        # the latest vital-sign date across fragments — the timing marker for the
        # merged (flat) vitals set, used by the V5 temporal gate.
        va = p.get("_vitals_at")
        if va and (out.get("_vitals_at") is None or str(va) > str(out["_vitals_at"])):
            out["_vitals_at"] = va
    if index_event:
        out["_index_event"] = index_event
    if sealed:
        out["_sealed_ground_truth"] = sealed
    if eval_task:
        out["_eval_task"] = eval_task
    if case_provenance:
        out["_case_provenance"] = case_provenance
    out["_synthetic_declared"] = synthetic_declared
    return out


# ─── Completeness + answer-leakage guards (Buyer Response PRD §2 A4, §3 B1) ────
_COMPLETENESS_STOPWORDS = frozenset({
    "the", "and", "of", "a", "an", "with", "for", "in", "on", "to", "longitudinal",
    "clinical", "serial", "required", "study", "studies", "panel", "panels", "test",
    "tests", "imaging", "image", "images", "data",
})
# A declared token we cannot confidently resolve is NOT a missing modality. Synonyms
# let the common abbreviations resolve; anything still unresolved is reported as
# unverified rather than treated as absent (Audit PRD §P1).
_MODALITY_SYNONYMS = {
    "lm": ("light microscopy", "pas", "h&e", "hematoxylin"),
    "em": ("electron microscopy", "ultrastructural"),
    "if": ("immunofluorescence",),
    "routine if": ("frozen if", "immunofluorescence"),
    "pronase if": ("pronase", "paraffin immunofluorescence"),
    "urine microscopy": ("urine sediment", "sediment", "urinalysis"),
    "hematology studies": ("flow cytometry", "spep", "free light chain", "immunofixation"),
    "longitudinal labs": ("labs", "laboratory"),
    "clinical notes": ("note", "progress", "consult", "discharge"),
}


def _completeness_haystack(case: Dict[str, Any]) -> str:
    """Everything a declared modality could legitimately name. Declarations are
    TECHNIQUES ('pronase IF', 'EM'); Study.modality is a coarse ENUM ('pathology').
    Matching modality alone can never satisfy them, so the label and findings — where
    the technique is actually named — are part of the haystack."""
    bits: List[str] = []
    for s in case.get("studies") or []:
        bits += [str(s.get("modality") or ""), str(s.get("label") or ""),
                 str(s.get("findings") or "")]
    for p in case.get("lab_panels") or []:
        bits.append(str(p.get("panel") or ""))
        for r in p.get("results") or []:
            bits.append(str(r.get("analyte") or ""))
    for n in case.get("notes") or []:
        bits += [str(n.get("note_type") or ""), str(n.get("text") or "")]
    if case.get("lab_panels"):
        bits.append("labs longitudinal laboratory")
    if case.get("notes"):
        bits.append("clinical notes note")
    if case.get("medications"):
        bits.append("medications")
    return " ".join(bits).lower()


def completeness_check(declared: List[str], case: Dict[str, Any]) -> Dict[str, Any]:
    """Tri-state completeness (Buyer Response PRD §2 A4, corrected — Audit PRD §P1).

    Returns ``{present, missing, unresolved}``. Only ``missing`` — a token we
    RECOGNISED and confirmed absent — may quarantine. An ``unresolved`` token means our
    matcher did not understand the partner's wording, which is a fact about our parser,
    not about their data; quarantining on it rejects good cases (exactly what happened
    to the real PGNMID bundle) and hides the parser gap behind a clinical-sounding
    rejection."""
    hay = _completeness_haystack(case)
    present, missing, unresolved = [], [], []
    for tok in declared or []:
        t = tok.strip().lower()
        if not t:
            continue
        candidates = (t,) + tuple(_MODALITY_SYNONYMS.get(t, ()))
        recognised = t in _MODALITY_SYNONYMS or len(t.split()) <= 4
        if any(c in hay for c in candidates):
            present.append(tok)
        elif recognised:
            missing.append(tok)
        else:
            unresolved.append(tok)
    return {"present": present, "missing": missing, "unresolved": unresolved}


# ─── Per-truncation modality declaration (Longitudinal Cases PRD §4.2.1) ─────
# Coarse token → the predicate that says this truncated window actually carries it.
# Deliberately COARSE. A declaration is a claim about what the case contains, and a
# claim this function cannot re-verify against the same haystack ``completeness_check``
# reads is a quarantine waiting to happen. Everything here is checked by the
# round-trip invariant asserted in the tests: for any case ``c``,
# ``completeness_check(modalities_present_in(c), c)["missing"] == []``.
_TRUNCATION_MODALITY_RULES: Tuple[Tuple[str, Any], ...] = (
    ("longitudinal labs", lambda c: bool(c.get("lab_panels"))),
    ("clinical notes", lambda c: bool(c.get("notes"))),
    ("medications", lambda c: bool(c.get("medications"))),
)


def modalities_present_in(case: Dict[str, Any]) -> List[str]:
    """The modality declaration FOR THIS WINDOW — never inherited from the chart.

    READ THIS BEFORE REACHING FOR THE PARENT CHART'S DECLARATION. Inheriting is the
    obvious implementation, which is exactly why it has to be ruled out in writing.

    A case truncated at encounter *k* **legitimately lacks** modalities the full
    chart carries. Patient-1's ERCP report exists at day −1242; a case truncated at
    day −1810 must not contain it, and must not claim to. Inherit the chart's
    declaration onto that truncation and ``completeness_check`` returns
    ``missing = ['ERCP procedure report']`` — a token it recognised and confirmed
    absent — which quarantines the case at ``ingestion``'s only quarantining branch.

    Worse than the quarantine is how it reads: *"the case's decisive evidence is
    absent — quarantining rather than shipping an unanswerable case"*. That is a
    clinical-sounding rejection for what is actually correct behaviour, and every
    early decision point in every trajectory would produce it.

    A decision point is not an incomplete case. It is a **complete case about an
    earlier moment**, and declaring the chart's full modality set on it asserts
    evidence the physician is not supposed to have yet.

    Study techniques are named from the studies the window itself carries, using
    the study's own ``label``/``modality`` text, so the declaration says what is
    there rather than what the parent bundle promised.
    """
    c = case or {}
    out: List[str] = []
    for token, present in _TRUNCATION_MODALITY_RULES:
        if present(c):
            out.append(token)
    # Studies are declared individually: a window carrying a renal biopsy and a CT
    # should say so, because that is what a buyer filters on. The token used is the
    # study's own label when it has one (that is the text ``completeness_check``
    # matches against), falling back to the coarse modality enum.
    for study in c.get("studies") or []:
        label = str(study.get("label") or "").strip() or str(study.get("modality") or "").strip()
        if not label:
            continue
        # A declaration token must be RECOGNISABLE to ``completeness_check`` or it
        # lands in ``unresolved`` and stamps the case 'unverified' for no reason.
        # Its recognition rule is "a known synonym, or at most four words", so a
        # long OCR'd study label is trimmed to its first four words rather than
        # declared whole. A trailing connective left behind by that cut ("ERCP
        # procedure report with") reads as a truncation defect on a buyer-facing
        # field, so the tail is trimmed back to a content word.
        words = label.lower().split()[:4]
        while words and words[-1] in _COMPLETENESS_STOPWORDS:
            words.pop()
        token = " ".join(words)
        if token and token not in out:
            out.append(token)
    return out


# ─── Admin review queue (Audit PRD §20-§21) ──────────────────────────────────
def _raise_review(reasons: List[Dict[str, Any]], reason: str, severity: str,
                  detail: str) -> None:
    """Append a review reason instead of logging it into a void (Audit PRD §21.2).
    ``blocking`` holds the case out of the annotation queue (a physician must not see
    it yet); ``advisory`` does not (the case is clinically intact, only a claim about
    it is unverified — holding it would rebuild the completeness bug with a nicer UI)."""
    reasons.append({"reason": reason, "severity": severity, "detail": detail,
                    "raised_at": _utcnow_iso()})


def _upload_status_from_cases(ingested: int, quarantined: int, needs_review: int) -> str:
    """Upload status is the WORST state among its cases (Audit PRD §21.3;
    Real-Case Generation PRD §2.3).

    ``quarantined`` outranks both ``needs_review`` and ``ingested``. This used to
    rank ``ingested`` above ``quarantined``, so the real 3-fragment record — one
    labs-only case ingested, two cases carrying the ENTIRE narrative quarantined —
    rendered as a clean green row. An operator seeing green over a 2-of-3 failure
    is how that stayed hidden for a full record. Report the worst, not the best."""
    if ingested == 0 and quarantined == 0 and needs_review == 0:
        return "rejected"
    if quarantined:
        return "quarantined"
    if needs_review:
        return "needs_review"
    return "ingested"


def cf_case_has_asset(study: Dict[str, Any]) -> bool:
    from asclepius.cases import study_has_valid_asset
    return study_has_valid_asset(study)


def key_image_series_cap() -> int:
    """Above this many instances in one series, the partner (or an annotator) MUST
    designate key images — the 1-5 instances the reasoning depends on (Buyer Response
    PRD §4 C2). Without it, one CT study produces 200 assets, blows the prompt budget,
    and buries the finding, so a large series is archived-only until key images are
    named."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_KEY_IMAGE_SERIES_CAP", "5")))
    except ValueError:
        return 5


def _dicom_entries_to_studies(
    dicom_entries: List[Dict[str, Any]], manifest: Dict[str, Any], specialty: str,
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], bool]:
    """Turn DICOM bundle entries into per-patient Study fragments (Buyer Response PRD
    §4 C1-C3). Each entry: de-identify (PS3.15 Annex E) → burned-in risk → render/
    archive. Returns (per_patient_frags, file_outcomes, produced_any_gradable).

    Key-image discipline (§4 C2): a series larger than the cap promotes an asset ONLY
    for instances the manifest designates as key images; the rest are archived so a
    200-instance CT does not produce 200 gradable assets."""
    from asclepius import dicom_deid

    key_ids = {str(k).strip().lower() for k in (manifest.get("key_images") or [])}
    cap = key_image_series_cap()

    # First pass: parse + de-id, group by series, so we know each series' size.
    parsed: List[Dict[str, Any]] = []
    series_counts: Dict[str, int] = {}
    for e in dicom_entries:
        name = e.get("name")
        try:
            ds = dicom_deid.read(e["data"])
            clean, dreport = dicom_deid.deidentify_dicom(ds)
        except dicom_deid.DicomDeidError as exc:
            parsed.append({"name": name, "error": str(exc)})
            continue
        series = str(getattr(clean, "SeriesInstanceUID", "") or name)
        sop = str(getattr(clean, "SOPInstanceUID", "") or "")
        series_counts[series] = series_counts.get(series, 0) + 1
        parsed.append({"name": name, "ds": clean, "series": series, "sop": sop,
                       "report": dreport})

    per_patient: Dict[str, List[Dict[str, Any]]] = {}
    outcomes: List[Dict[str, Any]] = []
    produced = False
    pk = str(manifest.get("patient_key") or "default")
    for p in parsed:
        name = p["name"]
        if p.get("error"):
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": f"rejected_unreadable: {p['error']}"})
            continue
        ds = p["ds"]
        risk, why = dicom_deid.burned_in_risk(ds)
        if risk == "blocked":
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": f"rejected_burned_in_phi: {why}"})
            continue
        # Key-image gate: a large series promotes an asset only for designated images.
        is_key = (series_counts.get(p["series"], 1) <= cap
                  or (name or "").lower() in key_ids
                  or p["sop"].lower() in key_ids)
        if not is_key:
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": "archived_only: large series, not a designated key image"})
            continue
        # Rendering can fail on undecodable/compressed pixel data or a missing
        # decoder — that must NEVER crash process_upload (a background task that must
        # always land a terminal status). A render failure downgrades this ONE entry
        # to an unreadable outcome; the rest of the bundle continues.
        try:
            frag = dicom_deid.to_study_fragment(
                ds, render=(risk == "clear"), needs_review=(risk == "suspect"),
                specialty=specialty, risk=risk, reason=why)
        except Exception as exc:
            log.warning("dicom render/fragment failed for %s: %s", name, exc)
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": f"rejected_unreadable: could not render pixels ({exc})"})
            continue
        study = {k: v for k, v in frag.items() if not str(k).startswith("_")}
        per_patient.setdefault(pk, []).append({"studies": [study], "_dicom": True})
        if risk == "clear" and cf_case_has_asset(study):
            produced = True
            outcomes.append({"name": name, "kind": "dicom", "outcome": "parsed"})
        else:
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": f"needs_burnin_review: {why}"})
    return per_patient, outcomes, produced


def _stage_sealed_ground_truth(store: Any, upload_id: str, patient_key: str,
                               sealed: Optional[Dict[str, Any]]) -> Optional[str]:
    """Stage the sealed answer key BEFORE the case row (Audit §H1), returning the
    ``sealed_id`` to bind later. Returns None when there is no key to stage OR when
    staging failed — the caller distinguishes the two (a truthy ``sealed`` with a
    None ref means storage failed, and it quarantines rather than shipping a case
    whose adjudication was lost)."""
    if not sealed:
        return None
    try:
        return store.stage_sealed_ground_truth(
            upload_id=upload_id, patient_key=patient_key, payload=sealed)
    except Exception as exc:
        log.error("sealed ground truth staging failed for upload %s patient %s: %s",
                  upload_id, patient_key, exc)
        return None


def _bind_sealed_ground_truth(store: Any, sealed_ref: Optional[str],
                              ingest_case_id: str, upload_id: str, pk: str) -> None:
    """Bind a staged key to its case row (Audit §H1). A bind that fails to land is
    NOT fatal: the key is still on disk under (upload_id, patient_key), and
    reconciliation re-binds it — the strictly better failure than losing the key."""
    if not sealed_ref:
        return
    try:
        store.bind_sealed_ground_truth(sealed_ref, ingest_case_id)
    except Exception as exc:  # pragma: no cover - defensive; reconciliation recovers
        log.error("sealed ground truth bind failed for %s: %s", ingest_case_id, exc)
        return
    try:
        store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                        event_type="sealed_ground_truth_ingested",
                        payload={"upload_id": upload_id, "patient_key": opaque_patient_key(pk)})
    except Exception:  # pragma: no cover - audit is best-effort
        pass


class AnswerLeakageError(BundleRejected):
    """A distinctive sealed-answer span appears in the model-visible case (Buyer
    Response PRD §3 B1). This must FAIL THE INGEST — a leaked answer silently
    invalidates every score computed from the case."""


def _distinctive_tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if w not in _COMPLETENESS_STOPWORDS and len(w) >= 3]


# Ordinary clinical vocabulary that a lone token would flag on by coincidence — a
# problem list saying "anemia" is not a leaked answer key (Audit §M1).
_COMMON_CLINICAL_TOKENS = frozenset("""
    acute chronic renal kidney cardiac disease syndrome failure injury infection
    anemia patient history normal abnormal elevated decreased positive negative
    treatment therapy diagnosis management follow biopsy blood urine serum
""".split())


def _is_checkable_single_token(tok: str) -> bool:
    """A lone token is worth checking when it is DISTINCTIVE: long enough not to be an
    abbreviation collision, and not ordinary clinical vocabulary. Short decisive
    acronyms (PGNMID, MGRS) are exactly the answers most damaging to leak, and the
    >=2-token rule skipped them (Audit §M1)."""
    return len(tok) >= 4 and tok not in _COMMON_CLINICAL_TOKENS


def _longest_contiguous_run(needle: List[str], hay_padded: str) -> int:
    """Longest contiguous run of ``needle`` tokens appearing as a whole-token span in
    ``hay_padded`` (which is the space-joined hay wrapped in sentinel spaces so a match
    respects token boundaries — 'a b c' never matches inside 'xa b cx')."""
    best = 0
    for i in range(len(needle)):
        j = i
        while j < len(needle) and (" " + " ".join(needle[i:j + 1]) + " ") in hay_padded:
            j += 1
        best = max(best, j - i)
    return best


def assert_no_answer_leakage(case: Dict[str, Any], sealed: Optional[Dict[str, Any]]) -> None:
    """Post-condition (Buyer Response PRD §3 B1): the SEALED answer-key resource must
    not have leaked into the model-visible case. Runs after ``deidentify()``, before
    the case is stored.

    The failure mode this guards is the sealed ``Basic`` being MERGED into the case
    body during parsing (a note, a study finding). It must NOT quarantine a real
    de-identified record whose clinical notes legitimately state the diagnosis — a
    pathology report says "Final: <diagnosis>" and a problem list carries the known
    condition, and the adjudicated answer key naturally restates those same facts.
    Quarantining on that coincidence rejects real hospital data, the exact
    two-conditions-one-check anti-pattern the audit calls out (§17).

    The distinguisher: a genuine merge reproduces a whole answer-key LEAF nearly
    verbatim; a clinical coincidence reproduces only the diagnosis NAME, a fraction of
    the leaf. So we flag only when a sealed leaf is SUBSTANTIALLY REPRODUCED (>=80% of
    its distinctive tokens appear as one contiguous run). ``source_refs`` are excluded
    because they are never rendered into the model prompt (annotator-only, §H4)."""
    if not sealed:
        return
    from asclepius.cases import render_case_prompt

    # Model-visible surface ONLY — render_case_prompt already honors model_visible
    # notes + study_findings_policy. source_refs are NOT model-visible.
    hay = " ".join(_distinctive_tokens(render_case_prompt(case, "")))
    hay_padded = " " + hay + " "

    hay_tokens = set(hay.split())  # whole-token membership for the single-token check
    answer_key = sealed.get("answer_key") if isinstance(sealed, dict) else sealed
    for leaf in _sealed_leaf_strings(answer_key):
        toks = _distinctive_tokens(leaf)
        if not toks:
            continue
        if len(toks) == 1:
            # A distinctive single-token answer (PGNMID, MGRS) leaks if it appears as a
            # WHOLE token in the model-visible case (Audit §M1). Whole-token only — a
            # substring match on a short token would fire inside longer words, and this
            # path raises a hard error.
            if _is_checkable_single_token(toks[0]) and toks[0] in hay_tokens:
                raise AnswerLeakageError(
                    "sealed answer key leaked: distinctive single-token answer present "
                    "in the model-visible case (Buyer Response PRD §3 B1)")
            continue
        need = max(2, -(-len(toks) * 8 // 10))  # ceil(0.8 * len)
        if _longest_contiguous_run(toks, hay_padded) >= need:
            raise AnswerLeakageError(
                "sealed answer key leaked into the model-visible case: a sealed leaf "
                "was substantially reproduced; refusing to ship (Buyer Response PRD §3 B1)")


def _sealed_leaf_strings(obj: Any) -> List[str]:
    """Every leaf string value inside the sealed answer key (recursively), so the
    leakage guard checks the ANSWER content, not JSON scaffolding tokens."""
    out: List[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_sealed_leaf_strings(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_sealed_leaf_strings(v))
    elif obj is not None:
        out.append(str(obj))
    return out


def _patient_key_of(fragment: Dict[str, Any], entry_name: str, manifest: Dict[str, Any]) -> str:
    return _patient_key_and_source(fragment, entry_name, manifest)[0]


def _patient_key_and_source(
    fragment: Dict[str, Any], entry_name: str, manifest: Dict[str, Any],
) -> Tuple[str, str]:
    """``(grouping_key, how_we_got_it)``. The source matters because reconciling
    keys across formats (``unify_patient_keys``) is only safe when we know which
    system minted each one."""
    # The manifest is the AUTHORITATIVE grouping hint (PRD §5): when the partner
    # declares a patient_key, every entry in the bundle belongs to that one case
    # (FHIR ids / CSV keys are per-system and would otherwise split the case).
    if manifest.get("patient_key"):
        return str(manifest["patient_key"]), "manifest"
    keys = fragment.get("_patient_keys") or []
    if keys:
        return str(keys[0]), "adapter"
    # filename convention: "<patient>__anything.ext"
    base = os.path.basename(entry_name)
    if "__" in base:
        return base.split("__", 1)[0], "filename"
    return "default", "default"


# Which minting system wins when one physical patient carries several keys
# (Real-Case Generation PRD §2.3). Most authoritative first.
_KEY_SOURCE_PRECEDENCE = ("manifest", "fhir_r4", "hl7v2", "lab_csv", "note_text",
                          "filename", "default")


def unify_patient_keys(
    per_patient: Dict[str, List[Dict[str, Any]]],
    key_sources: Dict[str, str],
    *, manifest: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """One physical patient is one case, regardless of how many formats the
    hospital sent. Returns ``(per_patient, unification_report_or_None)``.

    Without this, a FHIR bundle and its own HL7 export and its own lab CSV become
    THREE ingest_cases from one chart — and if any of them quarantines, part of the
    chart is silently lost. Measured on the real record: the FHIR fragment (164
    notes, 159 meds, 10 problems — the entire narrative) and the notes+CSV fragment
    both quarantined while the labs-only HL7 fragment ingested clean.

    Precedence: ``manifest.patient_key`` > FHIR ``Patient.id`` > HL7 PID-3 hash >
    CSV ``patient_key`` > filename convention > ``default``.

    SAFETY — this only fires when the upload describes ONE patient. If any single
    source minted two or more distinct identity keys (two FHIR Patient.ids, two
    filename prefixes…), the bundle is multi-patient and the grouping is left
    exactly as it was: merging two real patients into one case is far worse than
    splitting one patient into two. A partner who genuinely needs a cross-format
    crosswalk for a multi-patient bundle declares ``manifest.patient_key`` per
    upload, which short-circuits this entirely.
    """
    if (manifest or {}).get("patient_key"):
        return per_patient, None            # already one key by declaration
    if len(per_patient) <= 1:
        return per_patient, None

    # "default" is the ABSENCE of an identity, not an identity — it never blocks
    # unification and it never wins.
    identity_by_source: Dict[str, set] = {}
    for key in per_patient:
        if key == "default":
            continue
        identity_by_source.setdefault(key_sources.get(key, "default"), set()).add(key)

    if not identity_by_source:
        return per_patient, None            # nothing but 'default' — nothing to do
    for source, keys in identity_by_source.items():
        if len(keys) > 1:
            return per_patient, {
                "unified": False,
                "reason": f"{len(keys)} distinct patient keys from {source}, treating "
                          "this as a multi-patient bundle rather than merging charts",
                "sources": {s: len(k) for s, k in identity_by_source.items()},
            }

    winner_source = next(
        (s for s in _KEY_SOURCE_PRECEDENCE if s in identity_by_source), None)
    if winner_source is None:               # pragma: no cover - defensive
        return per_patient, None
    winner = sorted(identity_by_source[winner_source])[0]

    merged: List[Dict[str, Any]] = []
    for key in sorted(per_patient, key=lambda k: (k != winner, k)):
        merged.extend(per_patient[key])
    report = {
        "unified": True,
        "into_source": winner_source,
        "into": opaque_patient_key(winner),
        # Opaque forms only: a raw key may be an MRN and never passes the case-body
        # PHI scan (see ``opaque_patient_key``).
        "merged": [opaque_patient_key(k) for k in per_patient if k != winner],
        "sources": {s: sorted(opaque_patient_key(k) for k in ks)
                    for s, ks in identity_by_source.items()},
    }
    return {winner: merged}, report


def opaque_patient_key(raw_key: str) -> str:
    """The PERSISTED/LOGGED form of a grouping key (security review): a partner
    may put an MRN or a name in the CSV/manifest ``patient_key`` — which never
    passes through the case-body PHI scan — so anything stored in ingest_cases
    or emitted to the audit log is an opaque SHA-256 tag, never the raw key. The
    raw key exists only in-memory for grouping within one ingest run."""
    return "pk-" + hashlib.sha256((raw_key or "default").encode("utf-8")).hexdigest()[:12]


# ─── The orchestration (PRD §3) ───────────────────────────────────────────────
def process_upload(store: Any, upload_id: str) -> Dict[str, Any]:
    """Run the full pipeline for a received upload. Never raises — every outcome
    (ingested / quarantined / rejected) lands on the upload + case rows with
    audit events. Returns a summary dict."""
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        return {"error": "upload not found"}

    # Entry bytes are spilled to a staging dir rather than retained in memory, so
    # every exit from this function has to release it. Kept as a list rather than
    # a `with` block because the body below has many early returns and wrapping
    # ~180 lines in another indent level to gain what one call gives is a worse
    # trade than being explicit.
    _staged: List[Any] = []

    def _discard_staged() -> None:
        while _staged:
            with contextlib.suppress(Exception):
                _staged.pop()()

    def _fail(reason: str, *, retain_raw: bool = False) -> Dict[str, Any]:
        _discard_staged()
        # retain_raw (Audit §9.4): keep the raw blob past the retention window when the
        # rejection is a fixable parser gap (every entry failed), so it can be re-run.
        fields: Dict[str, Any] = {"status": "rejected", "reason": reason}
        if retain_raw:
            fields["retain_raw"] = 1
        store.update_ingest_upload(upload_id, **fields)
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="upload_rejected", payload={"reason": reason})
        # Auto-notify the sender their upload didn't come through (no PHI). Best
        # effort — a notification issue must never affect the pipeline outcome.
        try:
            from asclepius import ingest_notify
            ingest_notify.notify_upload_failed(
                store, store.get_ingest_upload(upload_id), outcome="rejected")
        except Exception:  # pragma: no cover - defensive
            pass
        return {"status": "rejected", "reason": reason}

    # ONE decrypted copy on disk, used for BOTH the malware scan and the unpack
    # (PRD-I §1). Two changes from the previous shape, both required by scale:
    #
    #  * The bundle is no longer materialized in RAM. ``load_raw`` on a 3 GB blob
    #    is an OOM, and the unpacker needs random access anyway (a zip's central
    #    directory is at the end), so a seekable file on the durable volume is the
    #    only shape that works.
    #  * The scanner now sees PLAINTEXT. It was being handed the encrypted blob,
    #    so with DATA_ENCRYPTION_KEY configured a real AV engine was scanning
    #    ciphertext and reporting clean every time — a silent false negative in
    #    exactly the control we tell operators is their AV hook.
    store.update_ingest_upload(upload_id, status="scanning")
    try:
        with decrypted_copy(upload["raw_path"]) as plain_path:
            ok, detail = malware_scan(plain_path)
            store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                            event_type="malware_scan", payload={"ok": ok, "detail": detail})
            if not ok:
                if scan_was_inconclusive(detail):
                    # OUR scanner could not reach a verdict. Rejecting here would
                    # tell a hospital their clean bundle was refused as malware,
                    # and the auto-notifier would email them saying so. Hold it
                    # for a human instead: the bytes are verified and durable, and
                    # nothing downstream sees the case until someone decides.
                    _discard_staged()
                    store.update_ingest_upload(
                        upload_id, status="needs_review",
                        reason="malware scan could not complete: held for review "
                               f"({detail})")
                    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                                    event_type="upload_held_scan_inconclusive",
                                    payload={"detail": detail})
                    return {"status": "needs_review", "reason": detail}
                return _fail(detail)
            store.update_ingest_upload(upload_id, status="parsing")
            # A bare clinical file gets the same server-side packing the single-
            # request doors apply, so the same file cannot succeed through one door
            # and fail through another.
            zip_path, wrapped = ensure_zip_on_disk(plain_path,
                                                   filename=upload.get("filename"))
            try:
                bundle = unpack_bundle_from_path(zip_path)
            finally:
                if wrapped:
                    with contextlib.suppress(OSError):
                        os.unlink(zip_path)
            _staged.append(bundle["cleanup"])
    except BundleRejected as exc:
        # Partner-facing reason is the actionable copy (Audit §9.1), never the raw
        # "bad magic bytes" text; the specific detail is retained in the audit log.
        log.info("upload %s unreadable: %s", upload_id, exc)
        return _fail(UNREADABLE_UPLOAD_MESSAGE)
    except Exception as exc:  # unreadable blob, key rotation issue, …
        log.warning("upload %s could not be read/unpacked: %s", upload_id, exc)
        return _fail(UNREADABLE_UPLOAD_MESSAGE)

    manifest = bundle["manifest"]
    # Undetermined resolves to the NEUTRAL default, never a specific specialty
    # (FIX-C C-3.2). The health-system portal carries the sentinel link_id
    # 'hs-portal', which has no upload-link row, so this chain used to end at the
    # literal 'nephrology' and stamped EVERY hospital upload with it — a bare
    # cardiology .json landed labeled nephrology, which routes the case to the
    # wrong physician pool and mislabels it in the export, invisibly. 'general'
    # is the ClinicalCase default and claims nothing; the admin treats it as
    # "not yet determined" and prompts an operator to set the real value before
    # promotion.
    specialty = (manifest.get("specialty")
                 or (store.get_upload_link(upload["link_id"]) or {}).get("specialty")
                 or "general")

    # Adapter pass: entry → fragments, grouped per patient. ``key_sources`` records
    # which minting system produced each grouping key so ``unify_patient_keys`` can
    # reconcile one patient across formats without ever merging two real patients.
    per_patient: Dict[str, List[Dict[str, Any]]] = {}
    key_sources: Dict[str, str] = {}
    file_outcomes: List[Dict[str, Any]] = []
    imaging_rejected = 0
    parsed_any = False
    # DICOM is no longer an automatic rejection (Buyer Response PRD §4 C3, retiring the
    # "no imaging" invariant, dated 2026-07): de-identify (PS3.15 Annex E) → burned-in
    # risk → render/archive. Handled as a batch so series size (key-image discipline)
    # is known.
    dicom_entries = [e for e in bundle["entries"] if e.get("kind") == "dicom"]
    if dicom_entries:
        try:
            d_per_patient, d_outcomes, d_produced = _dicom_entries_to_studies(
                dicom_entries, manifest, specialty)
        except Exception as exc:  # never strand the upload on a DICOM surprise
            log.warning("dicom batch processing failed for upload %s: %s", upload_id, exc)
            d_per_patient, d_produced = {}, False
            d_outcomes = [{"name": e.get("name"), "kind": "dicom",
                           "outcome": f"rejected_unreadable: {exc}"} for e in dicom_entries]
        for pk, frags in d_per_patient.items():
            key_sources.setdefault(pk, "dicom")
            per_patient.setdefault(pk, []).extend(frags)
        file_outcomes.extend(d_outcomes)
        parsed_any = parsed_any or d_produced
    for e in bundle["entries"]:
        name, kind = e.get("name"), e.get("kind")
        if kind == "manifest":
            file_outcomes.append({"name": name, "kind": kind, "outcome": "used"})
            continue
        if kind == "dicom":
            continue  # handled in the batch above
        if kind in ("rejected", "unsupported"):
            file_outcomes.append({"name": name, "kind": kind,
                                  "outcome": e.get("reason") or "unsupported"})
            continue
        entry_manifest = dict(manifest)
        entry_manifest["filename"] = name
        try:
            frag = cf.FORMATS[kind](e["data"], specialty=specialty, manifest=entry_manifest)
            parsed_any = True
            pk, how = _patient_key_and_source(frag, name, manifest)
            # An adapter-minted key belongs to the FORMAT that minted it — that is
            # what makes "two distinct FHIR Patient.ids" (multi-patient, do not
            # merge) distinguishable from "a FHIR id and its own HL7 hash" (one
            # patient, two systems).
            key_sources.setdefault(pk, kind if how == "adapter" else how)
            per_patient.setdefault(pk, []).append(frag)
            file_outcomes.append({"name": name, "kind": kind, "outcome": "parsed",
                                  "patient_key": opaque_patient_key(pk)})
        except Exception as exc:
            file_outcomes.append({"name": name, "kind": kind, "outcome": f"parse_failed: {exc}"})

    if not parsed_any:
        store.update_ingest_upload(upload_id, files_json=file_outcomes)
        # The "imaging-only bundle is rejected wholesale" invariant is RETIRED (Buyer
        # Response PRD §4 C3, 2026-07): a pathology or radiology case may legitimately
        # be imaging-only. What we require instead is at least one GRADABLE study — a
        # cleared/reviewer-approved asset. A bundle whose only DICOMs were blocked or
        # left pending burned-in review produced nothing gradable and is rejected with
        # that reason, not a blanket "imaging is never gradable".
        if dicom_entries and not any(
            e.get("kind") not in ("manifest", "dicom") for e in bundle["entries"]
        ):
            return _fail("no gradable study in the bundle: every image was blocked or "
                         "left pending burned-in review (needs a cleared/approved asset)",
                         retain_raw=True)
        return _fail("no parseable clinical content in the bundle", retain_raw=True)

    # Reconcile one physical patient across formats BEFORE the fragments merge
    # (Real-Case Generation PRD §2.3). Runs here rather than inside the adapter
    # pass because it needs the whole upload in view to tell a one-patient
    # multi-format export from a genuinely multi-patient bundle.
    per_patient, unify_report = unify_patient_keys(per_patient, key_sources,
                                                   manifest=manifest)
    if unify_report:
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="patient_keys_unified", payload=unify_report)

    # Per patient: assemble → normalize → verify → hard guard → land or quarantine.
    ingested, quarantined, needs_review = 0, 0, 0
    for pk, parts in per_patient.items():
        merged = _merge_fragments(parts)
        report: Dict[str, Any] = {"patient_key": opaque_patient_key(pk)}
        if unify_report:
            report["patient_key_unification"] = unify_report
        adapter_warnings = list(merged.get("_adapter_warnings") or [])
        if adapter_warnings:
            report["adapter_warnings"] = adapter_warnings
        # Answer-adjacent + author metadata pulled OUT before assembling the body —
        # never merged into a model-visible field (Buyer Response PRD §2 A4, §3 B1).
        sealed = merged.get("_sealed_ground_truth")
        eval_task = merged.get("_eval_task") or {}
        case_provenance = merged.get("_case_provenance")
        # Sealed-key ordering (Audit §H1): STAGE the answer key before the case row so
        # a crash between the two can never leave an ingested case with no key. Bound
        # to the case id once it's inserted, in both the ingested and quarantine paths.
        sealed_ref = _stage_sealed_ground_truth(store, upload_id, opaque_patient_key(pk), sealed)
        if sealed:
            report["sealed_present"] = True
        if eval_task:
            report["eval_task"] = {k: v for k, v in eval_task.items() if k != "answer_key"}
        # The quarantined body must be EXACTLY the object the findings describe
        # (spans are offsets into it) — the normalized case once normalization
        # succeeds, the raw merge only when normalization itself failed.
        quarantine_body = {k: v for k, v in merged.items() if not str(k).startswith("_")}
        try:
            # Staging the sealed key must have succeeded (Audit §H1): a truthy sealed
            # with no ref means storage failed. Raise here so the existing except
            # quarantines through the path that already works — never ship a case whose
            # adjudication could not be stored.
            if sealed and sealed_ref is None:
                raise cf.CaseIngestError(
                    "sealed answer key could not be stored; quarantining rather than "
                    "shipping a case whose adjudication was lost")
            normalized, treport = normalize_timeline(
                quarantine_body,
                index_event=manifest.get("index_event") or merged.get("_index_event"),
                # passed explicitly: ``quarantine_body`` has the underscore-prefixed
                # fragment metadata stripped, so the marker would not survive on it.
                vitals_at=merged.get("_vitals_at"),
            )
            report["timeline"] = treport
            quarantine_body = normalized
            if treport.get("unresolved"):
                raise cf.CaseIngestError(
                    "unresolved date-like tokens: " + ", ".join(treport["unresolved"][:5]))
            verification = deid_verify.verify_deid(normalized)
            report["verification"] = verification
            if verification["status"] == "flagged":
                raise cf.CaseIngestError(
                    f"de-id verification flagged {len(verification['findings'])} finding(s)")
            safe = cf.deidentify(normalized)
            # Inject author-declared metadata (Buyer Response PRD §2 A3/A4) + compute
            # the study-findings policy (§3 B2): hidden when any study carries a
            # resolvable image asset (the real multimodal test), visible otherwise.
            declared_mods = list(eval_task.get("required_modalities") or [])
            any_asset = any(cf_case_has_asset(s) for s in (safe.get("studies") or []))
            case = ClinicalCase(**{
                **safe, "case_source": "real_deid",
                "specialty": safe.get("specialty") or specialty,
                "declared_difficulty": eval_task.get("declared_difficulty"),
                "required_modalities": declared_mods,
                "case_provenance": case_provenance,
                "study_findings_policy": "hidden" if any_asset else "visible",
            }).model_dump()
            # Declared-vs-delivered completeness (Buyer Response PRD §2 A4; corrected
            # to tri-state — Audit PRD §P1). Only a RECOGNISED-and-absent token
            # quarantines. An UNRESOLVED token is a parser gap, not missing evidence:
            # ingest, flag as unverified, and (Phase 4) surface it as an ADVISORY
            # review reason — quarantining on it rejects good hospital data.
            comp = completeness_check(declared_mods, case)
            report["completeness"] = comp
            if comp["missing"]:
                report["missing_modalities"] = comp["missing"]
                raise cf.CaseIngestError(
                    f"bundle declares required modalities not delivered: {sorted(comp['missing'])}; "
                    f"the case's decisive evidence is absent, quarantining rather than "
                    f"shipping an unanswerable case")
            case["completeness_status"] = "unverified" if comp["unresolved"] else "verified"
            # Post-condition (Buyer Response PRD §3 B1): no distinctive sealed-answer
            # span may appear in the model-visible case. Runs after deidentify(),
            # before the case is stored.
            assert_no_answer_leakage(case, sealed)
            # Admin review queue (Audit PRD §21). Collect the three "unknown" states
            # that used to have nowhere to go and route them to a human. Blocking
            # reasons hold the case out of the annotation queue; advisory reasons
            # ingest cleanly (the case is intact, only a claim about it is unverified).
            review_reasons: List[Dict[str, Any]] = []
            for st in (case.get("studies") or []):
                phi = st.get("phi_screening") or {}
                if phi.get("burned_in_risk") == "suspect":
                    _raise_review(review_reasons, "burned_in_phi_unverified", "blocking",
                                  phi.get("reason")
                                  or f"study '{st.get('label') or st.get('modality')}': "
                                     "burned-in PHI could not be screened")
                elif phi.get("method") == "tag_only":
                    _raise_review(review_reasons, "deid_partner_flag_only", "advisory",
                                  f"study '{st.get('label') or st.get('modality')}': "
                                  "OCR screening unavailable; cleared on DICOM tags alone")
            for w in adapter_warnings:
                # ADVISORY, not blocking: the case is clinically intact, but a
                # parser gap silently degraded it (a lost timeline, a dropped
                # reference range). Holding the case would rebuild the completeness
                # bug; saying nothing is how the green row happened.
                _raise_review(review_reasons, "adapter_parse_gap", "advisory", w)
            if comp["unresolved"]:
                _raise_review(review_reasons, "completeness_unverified", "advisory",
                              "could not resolve declared modalities against delivered "
                              f"evidence: {sorted(comp['unresolved'])}")
            # Partner-quality advisory (V4 PRD §1.2). The partner's own
            # de-identification footer carried a date. That is a real finding about
            # THEIR pipeline — an unshifted original date in a header that should
            # carry no dates at all — and they should be told. It is not a finding
            # about this chart, whose clinical text is clean, so it must never
            # quarantine: ADVISORY. Recording it is the point; the alternative to
            # this line is stripping the header and saying nothing, which is how a
            # partner keeps shipping the same leak.
            _prov_dates = (report.get("timeline") or {}).get("provenance_header_dates") or []
            if _prov_dates:
                _raise_review(review_reasons, "provenance_header_dates", "advisory",
                              "the partner's de-identification header contains "
                              f"{len(_prov_dates)} date-like token(s) ({', '.join(_prov_dates[:3])}); "
                              "the header was removed before scanning. Their de-identification "
                              "footer should carry no dates at all, report this to the partner.")
            blocking = [r for r in review_reasons if r["severity"] == "blocking"]
            case_status = "needs_review" if blocking else "ingested"
            ic = store.insert_ingest_case(upload_id=upload_id,
                                          patient_key=opaque_patient_key(pk),
                                          specialty=specialty, case=case,
                                          status=case_status, report=report,
                                          review_status=("needs_review" if review_reasons else None),
                                          review_json=review_reasons or None)
            # Bind the pre-staged answer key to the case row now that it exists
            # (Audit §H1). Staging already succeeded (checked at the top of the try),
            # so the key is on disk; binding is a safe UPDATE, and a bind that somehow
            # fails leaves the key recoverable by reconciliation — never a lost key.
            _bind_sealed_ground_truth(store, sealed_ref, ic["ingest_case_id"], upload_id, pk)
            if blocking:
                needs_review += 1
            else:
                ingested += 1
            store.log_event(entity_type="ingest_case", entity_id=ic["ingest_case_id"],
                            event_type=("case_needs_review" if blocking else "case_ingested"),
                            payload={"upload_id": upload_id,
                                     "patient_key": opaque_patient_key(pk),
                                     "panels": len(case.get("lab_panels") or []),
                                     "notes": len(case.get("notes") or []),
                                     "studies": len(case.get("studies") or []),
                                     "source_refs": len(case.get("source_refs") or []),
                                     "review_reasons": [r["reason"] for r in review_reasons],
                                     "sealed_stored": bool(sealed)})
        except (cf.CaseIngestError, TimelineError, ValidationError, AnswerLeakageError) as exc:
            # ValidationError (BUG-1 hardening): a real bundle whose structure
            # drifts from the ClinicalCase schema — now that the case models are
            # extra="forbid" — quarantines with a readable reason instead of
            # silently dropping the stray field (the old extra="ignore" data loss)
            # OR crashing the background ingest job. Loud, recoverable, never silent.
            report["quarantine_reason"] = str(exc)
            # The quarantine body is a plain merge that may still carry the raw
            # ``studies``/``source_refs`` — strip any answer-adjacent metadata was
            # already excluded (underscore keys). Never let a sealed key ride along.
            ic = store.insert_ingest_case(
                upload_id=upload_id, patient_key=opaque_patient_key(pk),
                specialty=specialty, case=quarantine_body,
                status="quarantined", report=report)
            quarantined += 1
            # The pre-staged answer key binds to the quarantined case too (Audit §H1):
            # it is still the referring institution's adjudication, used for §7 F3
            # external-agreement once the case is reviewed. If staging itself failed,
            # sealed_ref is None and there is simply nothing to bind.
            _bind_sealed_ground_truth(store, sealed_ref, ic["ingest_case_id"], upload_id, pk)
            store.log_event(entity_type="ingest_case", entity_id=ic["ingest_case_id"],
                            event_type="case_quarantined",
                            payload={"upload_id": upload_id,
                                     "patient_key": opaque_patient_key(pk),
                                     "reason": str(exc)})
        except Exception as exc:  # infra error (store/DB/log) — never strand the upload
            # process_upload must always land a terminal status. An unexpected error
            # (DB write, log_event, crypto) is recorded as a per-patient quarantine
            # rather than escaping the BackgroundTask and leaving the upload stuck in
            # 'parsing' forever.
            log.warning("ingest: unexpected error for patient %s in upload %s: %s",
                        opaque_patient_key(pk), upload_id, exc)
            try:
                report["quarantine_reason"] = f"unexpected ingest error: {exc}"
                ic = store.insert_ingest_case(
                    upload_id=upload_id, patient_key=opaque_patient_key(pk),
                    specialty=specialty, case=quarantine_body,
                    status="quarantined", report=report)
                quarantined += 1
                store.log_event(entity_type="ingest_case", entity_id=ic["ingest_case_id"],
                                event_type="case_quarantined",
                                payload={"upload_id": upload_id, "reason": "ingest_error"})
            except Exception:  # pragma: no cover - last-resort; do not re-raise
                log.exception("ingest: could not even quarantine patient %s in upload %s",
                              opaque_patient_key(pk), upload_id)

    # Purpose flows link/account → upload → case, copied SERVER-SIDE by joining
    # the upload row (PRD-I §2.1). Done once here rather than threaded through
    # every insert_ingest_case call site, so a future case-creation path cannot
    # forget it and produce a case with no purpose. A case that misses this is no
    # longer promotable-by-accident — the gate holds an unstamped case in storage
    # — but it is still wrong: it reaches an operator as a decision to make about
    # data whose destination was in fact already chosen at the door.
    try:
        store.propagate_purpose_to_cases(upload_id)
    except Exception as exc:  # pragma: no cover - defensive; never strand an upload
        log.warning("could not propagate purpose for upload %s: %s", upload_id, exc)

    status = _upload_status_from_cases(ingested, quarantined, needs_review)
    reason = None
    if status == "needs_review":
        reason = "one or more cases held for admin review"
    elif status == "quarantined":
        # Partial-failure wording matters: "all cases quarantined" over a 2-of-3
        # failure reads as a different (and smaller) problem than it is.
        reason = ("all cases quarantined: review findings" if ingested == 0 and needs_review == 0
                  else f"{quarantined} of {quarantined + ingested + needs_review} cases "
                       "quarantined: review findings")
    elif status == "rejected":
        reason = "nothing ingested"
    store.update_ingest_upload(upload_id, status=status, reason=reason, files_json=file_outcomes)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="upload_processed",
                    payload={"status": status, "ingested": ingested,
                             "quarantined": quarantined, "needs_review": needs_review,
                             "imaging_rejected": imaging_rejected})
    _discard_staged()
    purge_expired_raw(store)
    return {"status": status, "ingested": ingested, "quarantined": quarantined,
            "needs_review": needs_review, "imaging_rejected": imaging_rejected,
            "files": file_outcomes}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
