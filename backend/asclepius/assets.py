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


def _store_root() -> str:
    from asclepius.constants import asset_store
    root = asset_store()
    # Only a local filesystem backend is implemented here; an s3:// URL is accepted
    # by config but a future backend resolves it (never expose the path either way).
    if root.startswith("s3://"):
        raise AssetError("s3 asset backend not built in this release; set a local ASCLEPIUS_ASSET_STORE path")
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
        log.warning("Pillow unavailable — storing raster without metadata strip")
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
    try:
        pages = convert_from_bytes(data, dpi=150)
    except Exception as exc:
        raise AssetError(f"could not render PDF (is poppler installed?): {exc}") from exc
    if not pages:
        raise UnsupportedMediaType("PDF has no renderable pages")
    page_count = len(pages)
    idx = max(0, min((page or 1) - 1, page_count - 1))
    im = pages[idx]
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


def _write_blob(sha256: str, data: bytes) -> None:
    path = _blob_path(sha256)
    if os.path.exists(path):
        return  # content-addressed dedupe — identical image costs once (§9 perf)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def load_asset(asset_or_sha: Any) -> Tuple[bytes, str]:
    """Resolve a StudyAsset (dict) or a bare sha256 → (bytes, mime). Verifies the
    content hash matches (integrity). Raises AssetError if missing/corrupt."""
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
    if _sha256(data) != sha:  # integrity — a corrupted blob must never serve
        raise AssetError(f"asset integrity check failed for {sha[:12]}…")
    return data, mime


def find_asset_by_id(store: Any, asset_id: str) -> Optional[Dict[str, Any]]:
    """Locate the StudyAsset reference for ``asset_id`` by scanning stored V4 cases'
    studies (the DB holds only references). Returns the asset dict or None. The
    serving endpoint uses this to resolve an ``asset_id`` → sha256 → blob without
    ever exposing the store path or partner id."""
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
            if isinstance(a, dict) and a.get("asset_id") == asset_id:
                return a
    return None


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
            "note": "advisory only — partner attestation trusted; not a gate (§9)"}
