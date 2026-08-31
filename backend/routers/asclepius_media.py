"""Platform media: the onboarding demo video (Onboarding v2 §0.1, §6 stop 2).

Why this is not a file in the repo
──────────────────────────────────
The demo is ~73 MB. Committing it would put a binary that changes wholesale on
every re-record into git history forever, add it to every clone and every image
build, and still leave it un-seekable — a static mount answers a scrub request
with the whole file. So the bytes live in the content-addressed asset store
(``ASCLEPIUS_ASSET_STORE``, which must be a persistent volume in production) and
this router is the only way in or out of them.

The three properties that matter, in order:

1. **Range support is not an optimization.** ``<video controls>`` will render a
   timeline it cannot scrub if the server answers every request with 200 and the
   whole body; Safari refuses to start playback at all. A 206 with a correct
   ``Content-Range`` is what makes the control bar real.
2. **The body is streamed from disk, never buffered.** One seek must cost one
   chunk of memory, not 73 MB — see ``assets.media_blob_path`` for why this does
   not reuse ``load_asset``.
3. **Authentication is required.** ``get_current_account`` and not
   ``require_full_access``: a physician awaiting verification is exactly who
   watches this, and the walkthrough runs before their queue opens.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Iterator, Optional, Tuple

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from asclepius import assets
from asclepius import auth as asc_auth
from asclepius.store import get_store

log = logging.getLogger("asclepius.media")

router = APIRouter(tags=["asclepius-media"])

#: The one slot this release defines. A named slot rather than an asset id so the
#: player's URL is stable across re-records — swapping the video is an upload,
#: not a frontend deploy.
DEMO_SLOT = "onboarding_demo"

#: One day. Private, because the response is per-account and a shared cache must
#: never hold it; a day, because the video changes when we re-record it and a
#: physician who watches it twice in a week should not re-download 73 MB.
_CACHE_CONTROL = "private, max-age=86400"

#: Read granularity for the streamed body. 256 KB keeps the syscall count sane on
#: a full-file GET without holding a meaningful amount of memory per request.
_CHUNK = 256 * 1024


def _store():
    return get_store()


def _resolve_demo() -> Tuple[Dict[str, Any], str]:
    """(media row, blob path) or a 404 that says which of the two is missing.

    The two failures are genuinely different and an operator needs to tell them
    apart: no row means nobody has uploaded the video yet, while a row with no
    blob means the asset store was wiped — almost always a redeploy against
    ephemeral storage, which is a configuration bug and not a missing upload.
    """
    row = _store().get_platform_media(DEMO_SLOT)
    if not row:
        raise HTTPException(status_code=404, detail="No onboarding demo has been uploaded yet.")
    try:
        return row, assets.media_blob_path(row["sha256"])
    except assets.AssetError as exc:
        log.error("[media] demo blob missing for sha %s — asset store may be ephemeral",
                  str(row.get("sha256"))[:12])
        raise HTTPException(
            status_code=404,
            detail="The onboarding demo is registered but its file is missing from the "
                   "asset store. Re-upload it, and check that ASCLEPIUS_ASSET_STORE "
                   "points at a persistent volume.",
        ) from exc


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """``Range`` → inclusive (start, end), or None for "serve the whole thing".

    Raises 416 for a syntactically valid but unsatisfiable range, which is the
    spec's answer and what a player expects; a malformed header is IGNORED
    (returns None) rather than refused, because RFC 9110 says a recipient that
    cannot parse a Range must serve the full representation, and refusing would
    break playback on any client with a header quirk.
    """
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None                      # unparseable → full body, per RFC 9110 §14.2
    raw_start, raw_end = m.group(1), m.group(2)
    if not raw_start and not raw_end:
        return None
    if not raw_start:
        # "bytes=-N" — the LAST n bytes. Players use this to read the moov atom
        # of an MP4 whose metadata was not moved to the front.
        suffix = int(raw_end)
        if suffix <= 0:
            raise HTTPException(status_code=416, detail="Unsatisfiable range",
                                headers={"Content-Range": f"bytes */{size}"})
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
        end = min(end, size - 1)
    if start >= size or start > end:
        raise HTTPException(status_code=416, detail="Unsatisfiable range",
                            headers={"Content-Range": f"bytes */{size}"})
    return start, end


def _file_chunks(path: str, start: int, length: int) -> Iterator[bytes]:
    """Yield exactly ``length`` bytes from ``start``. Opened per request and
    closed by the generator's own scope, so a client that disconnects mid-scrub
    does not leak a descriptor."""
    remaining = length
    with open(path, "rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(_CHUNK, remaining))
            if not chunk:
                break                    # truncated blob: stop rather than spin
            remaining -= len(chunk)
            yield chunk


@router.get("/api/asclepius/assets/onboarding-demo")
async def onboarding_demo(
    range_header: Optional[str] = Header(None, alias="Range"),
    user: Dict[str, Any] = Depends(asc_auth.get_current_account),
):
    """Stream the demo, honoring HTTP Range.

    Starlette answers HEAD from this same route, which is what a player issues
    first to learn the length before it will draw a timeline.
    """
    row, path = _resolve_demo()
    size = os.path.getsize(path)
    mime = row.get("mime") or "video/mp4"
    # The sha IS the ETag: content-addressed storage means identical bytes are
    # the same blob, so a strong validator costs nothing to compute and is
    # exactly right. A re-record changes the sha, which invalidates every cached
    # copy without anyone having to remember to bust a query string.
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": _CACHE_CONTROL,
        "ETag": f'"{row["sha256"]}"',
        "X-Content-Type-Options": "nosniff",
        # The store also holds de-identified clinical images. Nothing served from
        # it should ever be framed by a third-party page.
        "Content-Disposition": "inline",
    }
    rng = _parse_range(range_header, size)
    if rng is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(_file_chunks(path, 0, size), media_type=mime, headers=headers)
    start, end = rng
    length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(_file_chunks(path, start, length), status_code=206,
                             media_type=mime, headers=headers)


@router.get("/api/asclepius/assets/onboarding-demo/meta")
async def onboarding_demo_meta(
    user: Dict[str, Any] = Depends(asc_auth.get_current_account),
):
    """Is there a demo to play, and how big is it?

    The walkthrough asks this BEFORE it renders stop 2, so a deployment that has
    not had the video uploaded yet shows the practice case on its own instead of
    a card that plays a 404.
    """
    row = _store().get_platform_media(DEMO_SLOT)
    if not row:
        return {"available": False}
    try:
        assets.media_blob_path(row["sha256"])
    except assets.AssetError:
        return {"available": False, "reason": "blob_missing"}
    return {
        "available": True,
        "mime": row.get("mime"),
        "byte_size": row.get("byte_size"),
        "duration_s": row.get("duration_s"),
        "url": "/api/asclepius/assets/onboarding-demo",
        # Cache-buster for the player's src. Same value as the ETag, so a
        # re-record changes the URL and nobody has to clear anything.
        "version": str(row.get("sha256") or "")[:12],
    }


@router.post("/api/asclepius/admin/assets/onboarding-demo")
async def upload_onboarding_demo(
    request: Request,
    file: UploadFile = File(...),
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """One-time (per re-record) admin upload of the demo video.

    Deliberately an ENDPOINT and not only a deploy step: the asset store lives on
    a persistent volume that a developer's laptop does not have, so "copy the file
    onto the server" is not a thing anyone can actually do on a managed host.
    ``backend/scripts/upload_onboarding_demo.py`` drives this over HTTPS.

    The body is streamed straight through to the store — nothing here reads the
    whole file — so a 500 MB upload costs one chunk of memory on the way past.
    """
    durable, detail = assets.asset_storage_durable()
    if not durable:
        # Fail loudly rather than accept an upload that the next redeploy erases.
        # A demo that plays today and 404s on Tuesday is worse than a refused
        # upload, because nobody is watching for it.
        raise HTTPException(status_code=503, detail=detail)

    def _chunks(fh) -> Iterator[bytes]:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                return
            yield chunk

    # Starlette's UploadFile spools to a temp FILE past ~1 MB, so the bytes are
    # already on disk by the time this handler runs — reading them back through a
    # sync generator keeps the peak at one chunk. Buffering the upload into a list
    # first (the obvious way to bridge async→sync) would put the whole 73 MB in
    # the process, which is the exact cost this design exists to avoid.
    file.file.seek(0)
    try:
        meta = await run_in_threadpool(
            assets.store_media, _chunks(file.file), file.content_type or "")
    except assets.UnsupportedMediaType as exc:
        raise HTTPException(
            status_code=415,
            detail=f"{exc}. Re-encode to MP4 (H.264 + AAC) for the widest playback.",
        ) from exc
    except assets.MediaTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except assets.AssetError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    row = _store().set_platform_media(
        DEMO_SLOT,
        sha256=meta["sha256"], mime=meta["mime"], byte_size=meta["byte_size"],
        filename=file.filename, uploaded_by=admin.get("email"),
    )
    _store().log_event(
        entity_type="platform_media", entity_id=DEMO_SLOT,
        event_type="platform_media_uploaded", actor=admin.get("email"),
        payload={"sha256": meta["sha256"], "byte_size": meta["byte_size"],
                 "mime": meta["mime"], "filename": file.filename},
    )
    warning = None
    if meta["mime"] == "video/quicktime":
        warning = ("Stored, but .mov does not play in Firefox. Re-encode to MP4 "
                   "(H.264 + AAC) and re-upload before launch.")
    return {"ok": True, "slot": DEMO_SLOT, "sha256": meta["sha256"],
            "byte_size": meta["byte_size"], "mime": meta["mime"],
            "uploaded_at": row.get("uploaded_at"), "warning": warning}
