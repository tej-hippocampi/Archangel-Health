"""The physician one-pager: source of truth, rendering, PDF, and the override.

A SIBLING OF ``physician_agreement.py`` and ``dla.py``, built on the same three
decisions, because the founders' intro call ends by sending one document and a
link and this is the document.

  * THE CONTENT IS A FILE (``docs/asclepius/PHYSICIAN_ONE_PAGER_v<n>.md``).
    Rewriting the pitch is a markdown edit and a version bump, not a Python
    change. Nothing outside this module knows what the one-pager says.

  * VERSIONS ARE ADDITIVE. A follow-up already sent named the version it
    carried, so the old file stays readable forever.

  * THE RENDERING IS SHARED. ``dla.markdown_rows`` and ``pdf_render`` build the
    PDF, so the one-pager cannot come out looking like a different company's
    document from the agreement the same physician signs a week later.

WHAT THIS ADDS OVER ITS SIBLINGS is the OVERRIDE. The agreement and the DLA are
legal records and must be rendered from their source every time. The one-pager
is marketing collateral, and a founder with a designed PDF should be able to
ship it without waiting for anyone: ``ASCLEPIUS_ONE_PAGER_PDF`` points at a file
on disk and that file is served instead. The rendered document is the DEFAULT,
not the fallback of last resort, so the flow works on a fresh checkout with
nothing configured.

The override is deliberately a path and not a URL. A URL would make the
follow-up email depend on a third-party host being up at send time, and would
put a redirect nobody controls in front of a physician who has just met us.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from functools import lru_cache
from typing import List, Optional, Tuple

from asclepius import dla, pdf_render

log = logging.getLogger("asclepius.one_pager")

#: The version sent today. Bumping this means adding
#: ``docs/asclepius/PHYSICIAN_ONE_PAGER_<n>.md``; the old file stays.
CURRENT_VERSION = "v1"

_FILE_PREFIX = "PHYSICIAN_ONE_PAGER_"
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_VERSION_RE = re.compile(r"^v[0-9]+$")

#: Source labels returned beside the bytes, so a caller (and the admin console)
#: can say WHICH document went out rather than guessing from its size.
SOURCE_RENDERED = "rendered"
SOURCE_FILE = "file"


class OnePagerError(RuntimeError):
    """The one-pager could not be loaded or rendered."""


def docs_dir() -> str:
    """``<repo>/docs/asclepius``. Overridable for tests via ``ARCHANGEL_ASCLEPIUS_DOCS_DIR``."""
    override = (os.getenv("ARCHANGEL_ASCLEPIUS_DOCS_DIR") or "").strip()
    if override:
        return override
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
    return os.path.join(os.path.dirname(here), "docs", "asclepius")


def available_versions() -> List[str]:
    try:
        names = os.listdir(docs_dir())
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith(_FILE_PREFIX) and n.endswith(".md"):
            ver = n[len(_FILE_PREFIX):-len(".md")]
            if _VERSION_RE.match(ver):
                out.append(ver)
    return sorted(out, key=lambda v: int(v[1:]))


@lru_cache(maxsize=8)
def _load_source(version: str) -> str:
    """The file, comments stripped, newlines normalized.

    Cached on the version for the same reason the agreement's loader is: it
    cannot change without a deploy, and the key includes the version so a new
    one is never masked by an old read.
    """
    if not _VERSION_RE.match(version or ""):
        raise OnePagerError(f"not a one-pager version: {version!r}")
    path = os.path.join(docs_dir(), f"{_FILE_PREFIX}{version}.md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise OnePagerError(f"one-pager {version} is not readable: {exc}") from exc
    text = _COMMENT_RE.sub("", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


def render(*, version: Optional[str] = None) -> str:
    """The one-pager as a person reads it, markers stripped."""
    return dla.to_plain(_load_source(version or CURRENT_VERSION))


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def render_pdf(*, version: Optional[str] = None) -> bytes:
    """The rendered document, through the same writer the agreements use."""
    ver = version or CURRENT_VERSION
    rows = dla.markdown_rows(_load_source(ver))
    return pdf_render.render_text_pdf(
        rows, banner=f"Archangel Health - Asclepius for physicians - {ver}")


def override_path() -> str:
    """A founder-supplied PDF to send instead of the rendered one, or ''."""
    return (os.getenv("ASCLEPIUS_ONE_PAGER_PDF") or "").strip()


def pdf_bytes(*, version: Optional[str] = None) -> Tuple[bytes, str]:
    """``(pdf, source)`` for the one-pager that should go out right now.

    A configured override that cannot be read does NOT fail the caller, and that
    is the important behaviour here. This is on the path of a follow-up to a
    physician we just met; a typo in an env var must degrade to sending the
    rendered default, not to sending nothing. The mistake is logged loudly
    because a founder who set that variable believes their document is going
    out.
    """
    path = override_path()
    if path:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            if data[:5] != b"%PDF-":
                raise OnePagerError(f"{path} does not begin with a PDF header")
            return data, SOURCE_FILE
        except (OSError, OnePagerError):
            log.exception(
                "one-pager: ASCLEPIUS_ONE_PAGER_PDF=%r could not be used; "
                "sending the rendered default instead", path)
    return render_pdf(version=version), SOURCE_RENDERED


def pdf_filename(*, version: Optional[str] = None) -> str:
    """A filename a physician can find in a downloads folder a month later."""
    return f"Archangel-Health-Asclepius-for-physicians-{version or CURRENT_VERSION}.pdf"


def clear_cache() -> None:
    """Tests that write a temporary one-pager need the loader to forget."""
    _load_source.cache_clear()
