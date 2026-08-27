"""Profile pictures.

A physician's own headshot: shown on their profile, beside their messages in
the community, and on the admin dossier when a person is checking their
credentials against a registry.

This is its own module rather than a few lines in the router because three of
the decisions here are security decisions and they should sit together where
they can be read at once.

**The bytes decide the type, never the header.** ``Content-Type`` is
attacker-controlled and the stored blob is served ``inline`` from the app
origin, to an admin whose bearer token lives in localStorage. Trusting the
header on the way in and sniffing on the way out is the exact combination that
turns an upload into stored XSS. ``credentialing.sniff_cv_mime`` established
this rule for CVs; an avatar is the same shape of risk with a friendlier name.

**The re-encode is not an optimisation.** ``_strip_and_normalize_raster``
rebuilds the image from pixel data, so no EXIF survives. Phone photographs
routinely carry GPS coordinates, and a physician uploading a selfie has no idea
they are also uploading where they took it.

**Not routed through the community attachment pipeline.** That path OCRs every
image and runs the text through the PHI scanner, and it fails closed when the
OCR toolchain is unavailable. Telling a doctor "image screening is unavailable
right now" because they tried to add a headshot would be absurd.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional, Tuple

from asclepius import assets as asc_assets
from asclepius.constants import _env_int  # noqa: PLC2701 — same package

log = logging.getLogger("asclepius.avatar")


class AvatarRejected(Exception):
    """Not a usable picture. Carries a sentence a physician can act on."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


#: Square, and small. An avatar renders at 96px on the profile and 24px in a
#: message row; 512 covers a 2x display with room to spare and keeps a hundred
#: physicians' pictures well under a megabyte each. The global
#: ``image_max_dim()`` is 4000, which is right for a chest film and absurd here.
AVATAR_DIM = 512


def avatar_max_bytes() -> int:
    """5 MB. Generous for a headshot, and small enough that the streaming cap
    in front of it rejects a wrong-file-picked mistake quickly."""
    return max(1, _env_int("ASCLEPIUS_AVATAR_MAX_BYTES", 5 * 1024 * 1024))


#: What a browser will actually produce from a file picker or a phone camera.
#: HEIC is deliberately absent: Pillow cannot read it without a plugin, and a
#: clear "PNG or JPEG" beats a silent failure on every iPhone photo.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def sniff_image_mime(data: bytes) -> Optional[str]:
    """What these bytes actually are, or None.

    An allowlist by construction: anything not matching a known raster header
    is refused, so there is no need to enumerate the dangerous formats. That
    matters because the dangerous one here is SVG, which is a document that can
    carry script and which every "is it an image?" check written from the
    filename gets wrong.
    """
    if not data:
        return None
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # RIFF....WEBP. Accepted on the way IN only: the normalizer below re-encodes
    # it to PNG, so nothing downstream ever has to serve a webp.
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def process_avatar(data: bytes) -> Tuple[bytes, str, str]:
    """Validate, strip and square an uploaded picture.

    Returns ``(clean_bytes, mime, sha256)``. Raises ``AvatarRejected`` with a
    sentence meant for the physician, never a stack trace and never a bare code.
    """
    if not data:
        raise AvatarRejected("That file was empty.", code="empty")
    if len(data) > avatar_max_bytes():
        mb = avatar_max_bytes() // (1024 * 1024)
        raise AvatarRejected(f"That image is too large. Keep it under {mb} MB.",
                             code="too_large")

    sniffed = sniff_image_mime(data)
    if not sniffed:
        raise AvatarRejected("That is not a PNG or JPEG image.", code="unsupported_type")

    try:
        clean, mime, _w, _h = asc_assets._strip_and_normalize_raster(data, sniffed)
    except asc_assets.UnsupportedMediaType as exc:
        raise AvatarRejected("That image could not be read.", code="unreadable") from exc
    except Exception as exc:  # pragma: no cover - Pillow surface is wide
        log.exception("[avatar] normalize failed")
        raise AvatarRejected("That image could not be read.", code="unreadable") from exc

    clean, mime = _square(clean, mime)
    return clean, mime, hashlib.sha256(clean).hexdigest()


def _square(data: bytes, mime: str) -> Tuple[bytes, str]:
    """Centre-crop to a square and cap at AVATAR_DIM.

    Cropping server-side rather than letting CSS ``object-fit`` do it means the
    stored bytes are the thing everyone sees, so a 4000x600 panorama cannot
    render as a sliver in one place and a face in another. Best-effort: if
    Pillow is missing the already-stripped image is stored as-is, because a
    correctly-shaped picture is worth less than a picture at all.
    """
    try:
        import io

        from PIL import Image
    except Exception:  # pragma: no cover
        return data, mime
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        w, h = im.size
        if w != h:
            side = min(w, h)
            left = (w - side) // 2
            # Bias the vertical crop UPWARDS. In a portrait the face is above
            # the centre, and a true centre crop reliably beheads people.
            top = (h - side) // 4 if h > w else (h - side) // 2
            im = im.crop((left, top, left + side, top + side))
        if im.size[0] > AVATAR_DIM:
            im = im.resize((AVATAR_DIM, AVATAR_DIM))
        buf = io.BytesIO()
        if mime == "image/jpeg":
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(buf, format="JPEG", quality=88, optimize=True)
        else:
            mime = "image/png"
            if im.mode == "P":
                im = im.convert("RGBA")
            im.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), mime
    except Exception:  # pragma: no cover
        log.exception("[avatar] square failed; storing the stripped original")
        return data, mime


def store(data: bytes) -> Tuple[str, str]:
    """Process and write to the content-addressed store. Returns (sha, mime).

    A note on durability, because the code around this one fails closed and a
    silent divergence reads as a bug: if ``ASCLEPIUS_ASSET_STORE`` is unset in
    production, blobs land under the code tree and vanish on the next deploy.
    ``assets`` treats that as a hard failure for clinical images, correctly. A
    lost avatar is cosmetic -- the initials come back and the physician can
    upload again -- so this path does NOT fail closed. It is a deliberate
    difference, not an oversight.
    """
    clean, mime, sha = process_avatar(data)
    asc_assets._write_blob(sha, clean)
    return sha, mime
