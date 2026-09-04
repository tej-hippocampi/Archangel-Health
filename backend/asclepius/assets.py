"""Content-addressed image asset store (V4 Image Embedding PRD §3-§4).

Real de-identified images (ECG strips, echo/CT/PET stills, pathology region images)
are attached to a :class:`~asclepius.cases.Study` via a :class:`StudyAsset`
reference. The image BYTES never live on the ClinicalCase or in ``asclepius.db`` —
only the reference. This module is the store:

  * **Ingest hygiene** (§3.3): strip ALL technical metadata (EXIF/XMP/ICC-beyond-
    color/GPS/device/timestamps/embedded thumbnails) and re-encode to a clean raster.
    This is standard data hygiene (removing risk-bearing, value-free fields) — NOT a
    de-identification check (the partner attestation is trusted, §9).
  * **PDF → raster** (§3.2): render each PDF page to PNG (both vision APIs take raster
    reliably). Default to page 1 unless the ingest specifies.
  * **Caps** (§3.1): reject > ``ASCLEPIUS_IMAGE_MAX_BYTES``; downscale over
    ``ASCLEPIUS_IMAGE_MAX_DIM`` preserving aspect.
  * **Hash + dedupe** (§3.4): ``sha256`` over the CLEANED bytes is identity, dedupe,
    and the A/B integrity check (the same bytes must reach both frontier providers).

The store is a local filesystem directory by default (``ASCLEPIUS_ASSET_STORE``);
blobs are laid out ``<store>/<ab>/<sha256>`` (git-style fan-out).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("asclepius.assets")

# Accepted upload MIME types (V4 Image PRD §3.1) — raster + PDF only. No DICOM,
# no whole-slide pathology this release.
ACCEPTED_MIMES = ("image/png", "image/jpeg", "application/pdf")
_RASTER_MIMES = ("image/png", "image/jpeg")


class UnsupportedMediaType(ValueError):
    """Raised for a non PNG/JPEG/PDF upload → router maps to 415."""


class ImageTooLarge(ValueError):
    """Raised when an upload exceeds ``ASCLEPIUS_IMAGE_MAX_BYTES`` → router maps to 413."""


class AssetError(RuntimeError):
    """Storage/resolution failure."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_EPHEMERAL_WARNED = False


def _store_root() -> str:
    global _EPHEMERAL_WARNED
    from asclepius.constants import asset_store, asset_store_is_ephemeral
    root = asset_store()
    # Only a local filesystem backend is implemented here; an s3:// URL is accepted
    # by config but a future backend resolves it (never expose the path either way).
    if root.startswith("s3://"):
        raise AssetError("s3 asset backend not built in this release; set a local ASCLEPIUS_ASSET_STORE path")
    if asset_store_is_ephemeral() and not _EPHEMERAL_WARNED:
        _EPHEMERAL_WARNED = True
        log.warning(
            "ASCLEPIUS_ASSET_STORE is not set and no durable data dir/DB path is "
            "configured: V4 image blobs are being written under the code tree (%s) "
            "and WILL BE LOST on redeploy. Set ASCLEPIUS_ASSET_STORE to a persistent "
            "volume in production.", root,
        )
    return root


def _blob_path(sha256: str) -> str:
    root = _store_root()
    return os.path.join(root, sha256[:2], sha256)


def _strip_and_normalize_raster(data: bytes, mime: str) -> Tuple[bytes, str, int, int]:
    """Strip technical metadata and enforce the pixel cap on a raster (§3.1/§3.3).
    Returns (clean_bytes, mime, width, height). Re-encodes to a clean PNG/JPEG with
    NO EXIF/XMP/ICC-beyond-color/GPS/thumbnail. Falls back to the raw bytes if Pillow
    is unavailable (still hashed + stored; hygiene is best-effort, not a gate)."""
    from asclepius.constants import image_max_dim
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a hard dep, but degrade safely
        log.warning("Pillow unavailable: storing raster without metadata strip")
        return data, mime, 0, 0
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as exc:
        raise UnsupportedMediaType(f"unreadable image: {exc}") from exc
    fmt = (im.format or "").upper()
    out_mime = "image/png" if fmt == "PNG" else ("image/jpeg" if fmt in ("JPEG", "JPG") else mime)
    # Downscale over the longest-edge cap, preserving aspect.
    max_dim = image_max_dim()
    w, h = im.size
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        w, h = im.size
    # Re-encode WITHOUT metadata: a fresh image from the pixel data carries no EXIF/
    # XMP/GPS/thumbnail. Convert palette/alpha sanely per target format.
    buf = io.BytesIO()
    if out_mime == "image/jpeg":
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(buf, format="JPEG", quality=90, optimize=True)  # no exif kwarg → stripped
    else:
        out_mime = "image/png"
        if im.mode == "P":
            im = im.convert("RGBA")
        im.save(buf, format="PNG", optimize=True)  # no metadata written
    return buf.getvalue(), out_mime, w, h


def _render_pdf_page(data: bytes, page: int) -> Tuple[bytes, int, int, int]:
    """Render a single PDF page to a clean PNG (§3.2). Returns
    (png_bytes, width, height, page_count). Requires pdf2image + poppler; raises
    AssetError with an actionable message if unavailable."""
    try:
        from pdf2image import convert_from_bytes
        from pdf2image.exceptions import PDFInfoNotInstalledError
    except Exception as exc:  # pragma: no cover
        raise AssetError(f"PDF rendering needs pdf2image + poppler: {exc}") from exc
    from asclepius.constants import image_max_dim
    # Page count first (cheap — no rasterization), so we render ONLY the requested
    # page rather than rasterizing every page into memory (a many-page PDF would OOM).
    try:
        from pdf2image import pdfinfo_from_bytes
        page_count = int(pdfinfo_from_bytes(data).get("Pages") or 1)
    except Exception:
        page_count = 1
    want = max(1, min(page or 1, page_count))
    try:
        pages = convert_from_bytes(data, dpi=150, first_page=want, last_page=want)
    except Exception as exc:
        raise AssetError(f"could not render PDF (is poppler installed?): {exc}") from exc
    if not pages:
        raise UnsupportedMediaType("PDF has no renderable pages")
    im = pages[0]
    max_dim = image_max_dim()
    w, h = im.size
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        w, h = im.size
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), w, h, page_count


def process_upload(
    data: bytes, mime: str, *, page: int = 1, source: str = "partner_deidentified",
) -> Dict[str, Any]:
    """Full ingest sub-pipeline for one image (§3): validate type + size, PDF→raster,
    strip metadata, hash, store content-addressed (dedup on sha256), and return a
    ``StudyAsset``-shaped dict. Raises UnsupportedMediaType / ImageTooLarge on a bad
    upload. The image bytes are written to the asset store, never the DB."""
    from asclepius.constants import image_max_bytes
    mime = (mime or "").strip().lower()
    if mime not in ACCEPTED_MIMES:
        raise UnsupportedMediaType(f"unsupported media type {mime!r}; accept {ACCEPTED_MIMES}")
    if len(data) > image_max_bytes():
        raise ImageTooLarge(f"image is {len(data)} bytes; max is {image_max_bytes()}")

    page_count: Optional[int] = None
    if mime == "application/pdf":
        clean, w, h, page_count = _render_pdf_page(data, page)
        out_mime = "image/png"
        rendered_page = max(1, min(page or 1, page_count))
    else:
        clean, out_mime, w, h = _strip_and_normalize_raster(data, mime)
        rendered_page = None

    sha = _sha256(clean)
    burnin = _maybe_burnin_scan(clean, out_mime)
    _write_blob(sha, clean)
    asset: Dict[str, Any] = {
        "asset_id": "asset-" + sha[:24],
        "mime": out_mime,
        "sha256": sha,
        "width": w or None,
        "height": h or None,
        "byte_size": len(clean),
        "page": rendered_page,
        "page_count": page_count,
        "source": source or "partner_deidentified",
    }
    if burnin is not None:
        asset["burnin_flag"] = burnin  # advisory only, never a gate (§9)
    return asset


def asset_storage_durable() -> Tuple[bool, str]:
    """(ok, detail) — is the asset store safe for V4 image blobs? (Audit PRD §P2)

    Mirrors ``ingestion.ingest_storage_durable()``. Images must be exactly as durable
    as the case rows that reference them: a surviving row pointing at a vanished blob
    is WORSE than a refused upload, because ``study_findings_policy='hidden'`` means the
    caption was withheld on purpose and the case becomes unanswerable, not merely
    degraded. The raw-upload path already fails closed (503) on non-durable storage;
    this extends the same gate to the derived image blobs."""
    from asclepius.constants import (
        VOLUME_MOUNT_ENV, asset_store, asset_store_is_ephemeral,
        declared_volume_mount, path_is_ephemeral, path_under_declared_volume,
    )
    root = asset_store()
    if root.startswith("s3://"):
        return True, "s3 backend configured"
    # The platform's own word first, when it gives one. Everything below this is
    # inference from a list of well-known temp directories, and inference cannot
    # tell a real volume mounted at /data from a container-local directory of the
    # same name — it calls both durable. That is the failure this check exists to
    # catch, so a DECLARED mount outranks the guess in both directions: outside it
    # is a refusal, inside it is a durability claim we can actually stand behind.
    on_volume = path_under_declared_volume(root)
    if on_volume is False:
        return False, (f"asset store {root} is NOT under the persistent volume this "
                       f"platform mounted at {declared_volume_mount()} "
                       f"({VOLUME_MOUNT_ENV}); everything written there, the "
                       f"onboarding demo video included: is destroyed on the next "
                       f"redeploy. Point ASCLEPIUS_ASSET_STORE (or "
                       f"ASCLEPIUS_DATA_DIR) at a path inside that mount.")
    # One shared prefix list (PRD I-0 §F1) — this used to re-implement the /tmp
    # check that ``asset_store_is_ephemeral`` should have been doing all along.
    if path_is_ephemeral(root):
        return False, (f"asset store {root} is on ephemeral storage; V4 image blobs "
                       f"will be lost on redeploy. Set ASCLEPIUS_ASSET_STORE to a "
                       f"path on your persistent volume.")
    if asset_store_is_ephemeral():
        return False, (f"asset store {root} is under the code tree; V4 image blobs will "
                       f"be lost on redeploy. Set ASCLEPIUS_ASSET_STORE to a persistent "
                       f"volume.")
    db_path = os.getenv("ASCLEPIUS_DB_PATH", "").strip()
    if db_path:
        try:
            if os.stat(root).st_dev != os.stat(os.path.dirname(os.path.abspath(db_path))).st_dev:
                return True, (f"asset store {root} is on a different volume than the DB; "
                              f"confirm that volume is persistent")
        except OSError:
            pass
    if on_volume:
        # Say WHICH volume. "durable" on its own is a claim an operator has no way
        # to check; naming the mount the platform declared is one they can.
        return True, (f"asset store {root} is on the persistent volume mounted at "
                      f"{declared_volume_mount()}")
    return True, f"asset store {root} is durable"


def _write_blob(sha256: str, data: bytes) -> None:
    path = _blob_path(sha256)
    if os.path.exists(path):
        return  # content-addressed dedupe — identical image costs once (§9 perf)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Unique temp name so two concurrent ingests of the same content don't race on a
    # shared ``.tmp`` (the second os.replace could otherwise hit FileNotFoundError).
    import uuid
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())          # survive an unclean container stop (Audit §P2)
    try:
        os.replace(tmp, path)         # atomic: no half-written blob is ever visible
    except OSError:  # a concurrent writer won the race — identical content, fine
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return
    # Read back and re-hash. A blob that cannot be re-read does not exist, and finding
    # out now beats a physician annotating a case whose image 404s (Audit §P2). A blob
    # that fails verification is removed so nothing can later resolve corrupt content.
    with open(path, "rb") as f:
        if _sha256(f.read()) != sha256:
            try:
                os.remove(path)
            except OSError:  # pragma: no cover
                pass
            raise AssetError(f"blob {sha256[:12]} failed post-write verification")


def load_asset(asset_or_sha: Any, *, verify: bool = False) -> Tuple[bytes, str]:
    """Resolve a StudyAsset (dict) or a bare sha256 → (bytes, mime). ``verify`` re-
    hashes the blob (integrity) — done at WRITE time, so it is OFF on the read/serve
    path (re-hashing a 25 MB blob on every GET is wasted CPU; the content-addressed
    path is the guarantee). Raises AssetError if missing/corrupt."""
    if isinstance(asset_or_sha, dict):
        sha = asset_or_sha.get("sha256")
        mime = asset_or_sha.get("mime") or "image/png"
    else:
        sha = str(asset_or_sha)
        mime = "image/png"
    if not sha:
        raise AssetError("no sha256 to resolve")
    path = _blob_path(sha)
    if not os.path.exists(path):
        raise AssetError(f"asset blob not found for {sha[:12]}…")
    with open(path, "rb") as f:
        data = f.read()
    if verify and _sha256(data) != sha:  # integrity — a corrupted blob must never serve
        raise AssetError(f"asset integrity check failed for {sha[:12]}…")
    return data, mime


# ─── Large media blobs (Onboarding v2 §0.1) ──────────────────────────────────
# The image path above re-encodes every upload to strip technical metadata. A
# video cannot go through it: there is no Pillow raster to re-encode, the file is
# two orders of magnitude larger than the image cap, and the whole point of the
# demo is that it plays and SEEKS — which needs the bytes on disk, byte-range
# addressable, exactly as they were authored.
#
# So media takes its own door: streamed to disk while hashing, never buffered
# whole, stored content-addressed in the same fan-out so one store stays one
# store. Nothing about it is a de-identification path — the only thing that ever
# reaches this function is company-authored marketing footage uploaded by an
# admin, which is why there is no strip, no scan, and no partner attestation.

#: Container formats a browser's native <video> element plays without a library.
#: MOV is accepted on upload because that is what a screen recording arrives as;
#: it is NOT reliably playable in Firefox, so the admin upload path says so.
ACCEPTED_MEDIA_MIMES = ("video/mp4", "video/webm", "video/quicktime")

#: Deliberately generous and separate from ``image_max_bytes``: the demo is ~73 MB
#: today and a re-record is not a reason to redeploy. Bounded all the same, because
#: an unbounded upload endpoint is a disk-exhaustion endpoint.
_MEDIA_MAX_DEFAULT = 512 * 1024 * 1024


def media_max_bytes() -> int:
    try:
        return max(1, int(os.getenv("ASCLEPIUS_MEDIA_MAX_BYTES", "").strip() or _MEDIA_MAX_DEFAULT))
    except ValueError:
        return _MEDIA_MAX_DEFAULT


class MediaTooLarge(ValueError):
    """Upload exceeded ``ASCLEPIUS_MEDIA_MAX_BYTES`` → router maps to 413."""


def store_media(chunks: Any, mime: str, *, max_bytes: Optional[int] = None) -> Dict[str, Any]:
    """Stream an iterable of byte chunks into the asset store.

    Hashes and writes in one pass, so a 500 MB upload costs one chunk of memory
    rather than 500 MB of it. Returns ``{sha256, mime, byte_size}``.

    The temp file is written first and renamed only once the whole stream has
    landed: an interrupted upload leaves a ``.tmp`` that the reconcile sweep
    already ignores, never a truncated blob that would serve as a corrupt video.
    """
    import uuid

    mime = (mime or "").strip().lower()
    if mime not in ACCEPTED_MEDIA_MIMES:
        raise UnsupportedMediaType(
            f"unsupported media type {mime!r}; accept {ACCEPTED_MEDIA_MIMES}")
    cap = max_bytes if max_bytes is not None else media_max_bytes()

    root = _store_root()
    os.makedirs(root, exist_ok=True)
    tmp = os.path.join(root, f"upload.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    digest = hashlib.sha256()
    total = 0
    try:
        with open(tmp, "wb") as fh:
            for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > cap:
                    raise MediaTooLarge(f"media is over {cap} bytes")
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        if total == 0:
            raise UnsupportedMediaType("empty upload")
        sha = digest.hexdigest()
        final = _blob_path(sha)
        if os.path.exists(final):
            os.remove(tmp)          # content-addressed dedupe: identical bytes cost once
        else:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            os.replace(tmp, final)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    return {"sha256": sha, "mime": mime, "byte_size": total}


def media_blob_path(sha256: str) -> str:
    """Filesystem path of a stored blob, for range-serving.

    Range serving is why this exists as its own accessor rather than reusing
    ``load_asset``: that function returns BYTES, and answering a seek into a
    73 MB video by reading all 73 MB and slicing is how one physician scrubbing
    a timeline becomes a 73 MB memory spike per request.
    """
    if not sha256:
        raise AssetError("no sha256 to resolve")
    path = _blob_path(sha256)
    if not os.path.exists(path):
        raise AssetError(f"media blob not found for {sha256[:12]}…")
    return path


def find_asset_by_id(store: Any, asset_id: str) -> Optional[Dict[str, Any]]:
    """Resolve ``asset_id`` → a reference dict {asset_id, sha256, mime, task_id,
    case_source} via the ``study_assets`` index (O(1)). Falls back to a one-time scan
    of stored V4 cases only if the index misses (legacy assets), backfilling the index
    so the next lookup is indexed. Returns None if unknown. Never exposes the store
    path or partner id."""
    try:
        ref = store.get_asset_ref(asset_id)
    except Exception:  # pragma: no cover - index missing on a very old DB
        ref = None
    if ref and ref.get("sha256"):
        return ref
    # Fallback (legacy assets ingested before the index existed): scan once, backfill.
    try:
        tasks = store.list_tasks(limit=100000)
    except Exception:  # pragma: no cover
        return None
    for t in tasks:
        case = t.get("case") or {}
        if case.get("case_source") != "real_deid":
            continue
        for s in case.get("studies") or []:
            a = (s or {}).get("asset")
            if isinstance(a, dict) and a.get("asset_id") == asset_id and a.get("sha256"):
                try:
                    store.insert_asset_ref(asset_id=asset_id, sha256=a["sha256"],
                                           mime=a.get("mime") or "image/png",
                                           task_id=t.get("task_id"), case_source="real_deid")
                except Exception:  # pragma: no cover
                    pass
                return {"asset_id": asset_id, "sha256": a["sha256"],
                        "mime": a.get("mime") or "image/png",
                        "task_id": t.get("task_id"), "case_source": "real_deid"}
    return None


def reconcile_assets(store: Any) -> Dict[str, Any]:
    """Inventory: every StudyAsset reference whose blob is gone, and every blob on
    disk with no reference (PRD I-0 §F4).

    READ-ONLY, deliberately. An orphan blob costs disk; a wrongly-deleted blob costs
    a case that can never be re-created, because the partner bundle behind it is
    purged on a 30-day retention. Deleting an orphan on the strength of a query is
    how a reporting bug becomes data loss, so this reports and nothing else.

    Answers the question nothing could answer before: *what did the last redeploy
    already take from us?* A row pointing at a vanished blob renders fine in the
    admin, and only fails when a physician opens the image — or, worse, ships to a
    buyer as a reference that resolves to nothing.

    Returns ``{missing_blobs, orphan_blobs, n_rows, n_files, checked_at}``."""
    from asclepius.store import _utcnow_iso

    referenced: Dict[str, Dict[str, Any]] = {}
    missing: List[Dict[str, Any]] = []

    def _scan(rows: List[Dict[str, Any]], *, case_key: str, id_key: str) -> None:
        for row in rows:
            case = row.get("case") or {}
            for study in (case.get("studies") or []):
                a = (study or {}).get("asset")
                if not (isinstance(a, dict) and a.get("sha256")):
                    continue
                sha = a["sha256"]
                referenced.setdefault(sha, {
                    "sha256": sha,
                    "source": case_key,
                    "case_id": row.get(id_key),
                    "study_id": study.get("study_id") or study.get("label"),
                    "ingested_at": row.get("created_at"),
                })

    for getter, case_key, id_key in (
        (lambda: store.list_ingest_cases(limit=1000000), "ingest_case", "ingest_case_id"),
        (lambda: store.list_tasks(limit=1000000), "task", "task_id"),
    ):
        try:
            _scan(getter(), case_key=case_key, id_key=id_key)
        except Exception as exc:  # pragma: no cover - defensive; report what we can
            log.warning("asset reconcile: could not scan %s rows: %s", case_key, exc)

    # Platform media (the onboarding demo video) lives in the same content-addressed
    # store but is referenced by a `platform_media` row rather than a case study, so
    # the two scans above cannot see it. Left out, its blob is inventoried as an
    # UNREFERENCED ORPHAN — first in line for any future sweep — and a demo video
    # that vanished off the volume is reported as nothing at all. Both inversions of
    # the truth, on the one asset an operator uploads by hand and expects to stay put.
    try:
        for row in store.list_platform_media():
            sha = (row or {}).get("sha256")
            if not sha:
                continue
            referenced.setdefault(sha, {
                "sha256": sha,
                "source": "platform_media",
                "case_id": row.get("slot"),
                "study_id": row.get("filename") or row.get("slot"),
                "ingested_at": row.get("uploaded_at"),
                "detail": "platform media slot " + str(row.get("slot")),
            })
    except Exception as exc:  # pragma: no cover - defensive; report what we can
        log.warning("asset reconcile: could not scan platform_media rows: %s", exc)

    for sha, ref in referenced.items():
        if not os.path.exists(_blob_path(sha)):
            missing.append(dict(ref))

    on_disk: List[str] = []
    try:
        root = _store_root()
    except AssetError:  # s3 backend — nothing local to inventory
        root = ""
    if root and os.path.isdir(root):
        for fan in sorted(os.listdir(root)):
            sub = os.path.join(root, fan)
            if not os.path.isdir(sub) or len(fan) != 2:
                continue
            for name in sorted(os.listdir(sub)):
                if name.endswith(".tmp"):
                    continue  # a write in flight is not an orphan
                on_disk.append(name)

    orphans = sorted(set(on_disk) - set(referenced))
    return {
        "missing_blobs": sorted(missing, key=lambda m: str(m.get("sha256"))),
        "orphan_blobs": orphans,
        "n_rows": len(referenced),
        "n_files": len(on_disk),
        "checked_at": _utcnow_iso(),
    }


def _maybe_burnin_scan(data: bytes, mime: str) -> Optional[Dict[str, Any]]:
    """Optional OCR backstop (§9): FLAG (never block) an image whose text looks like a
    burned-in identifier. Default OFF; returns None unless the flag is on. Not a
    de-identification gate — advisory metadata for admin review only."""
    from asclepius.constants import image_burnin_scan_enabled
    if not image_burnin_scan_enabled():
        return None
    try:
        import pytesseract
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(io.BytesIO(data))) or ""
    except Exception as exc:  # OCR unavailable → cannot flag, but never block
        return {"scanned": False, "reason": f"ocr_unavailable:{exc}"}
    import re
    looks_like_id = bool(re.search(r"\b(MRN|DOB|SSN)\b", text, re.I) or
                         re.search(r"\b\d{2}[/-]\d{2}[/-]\d{2,4}\b", text))
    return {"scanned": True, "flagged": looks_like_id,
            "note": "advisory only: partner attestation trusted; not a gate (§9)"}


def verify_case_assets(store: Any) -> Dict[str, Any]:
    """Every V4 study asset → does its blob still exist and hash correctly? Run at
    startup and nightly (Audit PRD §P2). Returns ``{checked, ok, missing, corrupt}``
    where ``missing``/``corrupt`` are review-reason-shaped dicts. A case with a missing
    blob raises the ``asset_blob_missing`` BLOCKING review reason so it leaves the
    annotation queue rather than being annotated blind — wired to the review queue in
    Phase 4. A surviving row pointing at a vanished blob is the failure this detects."""
    checked = ok = 0
    missing: List[Dict[str, Any]] = []
    corrupt: List[Dict[str, Any]] = []
    try:
        cases = store.list_ingest_cases(status="ingested", limit=1000000)
    except Exception:  # pragma: no cover - defensive
        cases = []
    for c in cases:
        case = c.get("case") or {}
        if case.get("case_source") != "real_deid":
            continue
        for s in case.get("studies") or []:
            a = (s or {}).get("asset")
            if not (isinstance(a, dict) and a.get("sha256")):
                continue
            checked += 1
            sha = a["sha256"]
            path = _blob_path(sha)
            if not os.path.exists(path):
                missing.append({"ingest_case_id": c.get("ingest_case_id"),
                                "reason": "asset_blob_missing", "severity": "blocking",
                                "detail": f"asset blob {sha[:12]}… is missing on disk"})
                continue
            try:
                with open(path, "rb") as fh:
                    good = _sha256(fh.read()) == sha
            except OSError:
                good = False
            if good:
                ok += 1
            else:
                corrupt.append({"ingest_case_id": c.get("ingest_case_id"),
                                "reason": "asset_blob_missing", "severity": "blocking",
                                "detail": f"asset blob {sha[:12]}… failed re-hash"})
    return {"checked": checked, "ok": ok, "missing": missing, "corrupt": corrupt}
