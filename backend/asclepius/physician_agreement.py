"""The physician contributor agreement: source of truth, rendering, hashing, PDF.

A SIBLING OF ``dla.py``, deliberately, and not a novel invention. The health
system side already answered the question "what exactly did this party agree to,
and when" and the answer it landed on is worth having twice: the agreement is a
FILE (``docs/legal/PHYSICIAN_AGREEMENT_v<n>.md``), the signature is an
append-only row carrying the hash of the text that was on the signer's screen,
and the artifact is rendered from the version they signed. Everything shared is
imported from ``dla`` rather than copied, so the two documents cannot drift in
how they are flattened, hashed or printed.

WHAT THIS ADDS OVER THE DLA, and why:

  * SUPERSESSION. A health system signs once and is done; a physician works for
    us for months across versions of the terms. So the question "is the version
    this person signed still the current one" has to be answerable, and has to
    be able to stop them drawing new work when the answer is no. That is
    ``resignature_reason`` and it is the only piece of policy in this module.

  * THE INTERIM MARKING. The operative language is an external dependency
    (counsel is supplying it). v1 is the seven attestations the product has
    always collected, assembled into a readable document and marked as interim
    on its face. Swapping counsel's language in is a new file plus a bump of
    ``CURRENT_VERSION`` -- a content change, not a code change. That is the whole
    reason the document is a file.

WHAT THE SEVEN ATTESTATIONS STILL DO is unchanged. This wraps them; it does not
replace them. The onboarding form still collects all seven booleans plus typed
initials, ``tiering`` still hard-gates on A6 and A7, and the finish endpoint
still 400s without them. What did not exist before is a document naming what
those booleans mean, a versioned record of which text was agreed to, and an
artifact that can be produced years later from the version signed.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from asclepius import dla, pdf_render

#: The version offered for signature today. Bumping this means adding
#: ``docs/legal/PHYSICIAN_AGREEMENT_<n>.md``; the old file stays, because rows in
#: ``physician_agreements`` point at it, and every physician who signed the older
#: one is asked to read the new one before their next case.
CURRENT_VERSION = "v1"

#: Shared with the DLA rather than restated. The reasoning is in ``dla``'s module
#: docstring: a rendered date would change between display and signature, so the
#: hash the signer saw could never match the hash at signature.
_EFFECTIVE_DATE_PHRASE = dla._EFFECTIVE_DATE_PHRASE

_BLANK = dla._BLANK

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_VERSION_RE = re.compile(r"^v[0-9]+$")

_FILE_PREFIX = "PHYSICIAN_AGREEMENT_"

#: Short tokens the labeling gate returns, so a client picks a screen rather
#: than matching prose. Same discipline as ``capabilities.practice_gate_reason``.
NEVER_SIGNED = "never_signed"
SUPERSEDED = "superseded"


class AgreementError(RuntimeError):
    """The document could not be loaded or rendered. Never shown to a physician."""


def docs_dir() -> str:
    """``<repo>/docs/legal``, the same directory the DLA lives in."""
    return dla.docs_dir()


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


def version_ordinal(version: str) -> int:
    """``v3`` -> 3, and -1 for anything that is not a version.

    Supersession is an ORDER question, and comparing version strings is how you
    ship a bug where v10 is older than v9.
    """
    if not _VERSION_RE.match(version or ""):
        return -1
    return int(version[1:])


@lru_cache(maxsize=8)
def _load_source(version: str) -> str:
    """The file, comments stripped, newlines normalized.

    Cached for the same reason the DLA's loader is: it is read on every render
    of the agreement and it cannot change without a deploy.
    """
    if not _VERSION_RE.match(version or ""):
        raise AgreementError(f"not an agreement version: {version!r}")
    path = os.path.join(docs_dir(), f"{_FILE_PREFIX}{version}.md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise AgreementError(f"agreement {version} is not readable: {exc}") from exc
    text = _COMMENT_RE.sub("", raw)
    # A checkout on Windows must not produce a different agreement from a
    # checkout on Linux, and the hash is what makes that observable.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


def render_markdown(*, physician: str, version: Optional[str] = None,
                    signer_name: str = "", signed_initials: str = "",
                    signed_at: str = "") -> str:
    """The source with its placeholders filled in, still carrying its markers.

    INTERNAL. Only the PDF layout consumes this, because it needs the heading
    markers to decide type sizes.
    """
    who = " ".join((physician or "").split()) or "Contributor"
    text = _load_source(version or CURRENT_VERSION)
    substitutions = {
        "PHYSICIAN_NAME": who,
        "EFFECTIVE_DATE": _EFFECTIVE_DATE_PHRASE,
        "SIGNER_NAME": signer_name or _BLANK,
        "SIGNED_INITIALS": signed_initials or _BLANK,
        "SIGNED_AT": signed_at or _BLANK,
    }
    for key, value in substitutions.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def render(*, physician: str, version: Optional[str] = None,
           signer_name: str = "", signed_initials: str = "",
           signed_at: str = "") -> str:
    """The agreement as a person reads it.

    With the signer arguments left empty this is the SIGNABLE text: what the
    portal displays and what gets hashed. Passing them produces the EXECUTED
    text, which only ever builds the PDF.
    """
    return dla.to_plain(render_markdown(
        physician=physician, version=version, signer_name=signer_name,
        signed_initials=signed_initials, signed_at=signed_at))


def sha256_of(text: str) -> str:
    """The hash convention, shared with the DLA so there is exactly one."""
    return dla.sha256_of(text)


def signable(*, physician: str, version: Optional[str] = None) -> Tuple[str, str]:
    """``(text, sha256)`` for display and for signature verification."""
    text = render(physician=physician, version=version or CURRENT_VERSION)
    return text, sha256_of(text)


def utcnow_iso() -> str:
    return dla.utcnow_iso()


# ─── Supersession ────────────────────────────────────────────────────────────
def gate_enabled() -> bool:
    """Whether an unsigned agreement actually stops a physician drawing work.
    OFF by default.

    Ships off on exactly the reasoning ``payout.enabled`` states, and for a
    harder operational reason on top of it. ARMING THIS BEFORE THE EXISTING
    PHYSICIANS HAVE BEEN ASKED TO SIGN WOULD STOP THE ENTIRE LABELING
    OPERATION IN ONE DEPLOY. Nobody has signed, because until this change there
    was nothing to sign, so every doctor on the platform would open the portal
    to a locked queue on a Tuesday morning because of a merge.

    So the order is: merge the mechanism, let counsel replace the interim
    document, put the signature screen in front of the physicians who are
    already here, and then arm it on a day somebody chooses.

    THE SCREEN NOW EXISTS, and that is what makes arming this survivable at all:
    ``renderAgreementView`` in ``frontend/asclepius/asclepius.js`` renders the
    text, takes the signature and POSTs ``/me/agreement/sign``, and both the
    dashboard and the next-case path route this gate's 403 into it (they match on
    ``AGREEMENT_GATE_HEADER`` / ``error == "agreement_required"``, so the copy can
    be reworded without breaking the routing). Physicians can also reach it
    unprompted from Profile, which is how step three above gets done. What is
    still owed before the flag is worth flipping is step two, counsel's language,
    and asking the roster to sign while the gate is still off -- arming it with
    an unsigned roster still stops every one of them at their next draw.
    Off, everything
    else in this feature still runs: the agreement is readable, signatures are
    taken and recorded, artifacts render, and supersession is computed and
    reported by ``/me/agreement``. The only thing the flag decides is whether
    the answer is enforced at the queue.
    """
    raw = (os.getenv("ASCLEPIUS_AGREEMENT_GATE", "0") or "0").strip()
    return raw not in ("", "0", "false", "False")



def resignature_reason(row: Optional[Dict[str, Any]], *,
                       required_version: Optional[str] = None) -> Optional[str]:
    """None when this physician's signature is current. Otherwise WHY it is not.

    Takes the ROW rather than the store so the policy is a pure function of what
    was signed, testable without a database, and callable from a gate that has
    already loaded the row for another reason.

    Only the ORDINAL is compared, never the string, and an UNRECOGNISABLE stored
    version is treated as superseded rather than as current. A row whose
    ``doc_version`` we cannot parse is a row we cannot vouch for, and the safe
    reading of "we do not know what they signed" is "ask them to sign".
    """
    if not row:
        return NEVER_SIGNED
    signed = version_ordinal(str(row.get("doc_version") or ""))
    required = version_ordinal(required_version or CURRENT_VERSION)
    if signed < 0:
        return SUPERSEDED
    if required < 0:
        # The deploy's own CURRENT_VERSION is unparseable. Refusing to serve work
        # over a bug in our constant would be the wrong way to fail: the
        # physician did everything asked of them.
        return None
    return SUPERSEDED if signed < required else None


# ─── PDF ─────────────────────────────────────────────────────────────────────
def signature_rows(signature: Dict[str, Any]) -> List[Tuple[str, str]]:
    """The evidence block appended to every executed PDF.

    The same fields as the DLA's, minus the authority-to-bind leg (a physician
    binds themselves, so there is nothing to have authority over) and plus the
    typed initials, which are the act of signing on this document.
    """
    def row(label: str, value: Any, mono: bool = False) -> Tuple[str, str]:
        kind = pdf_render.KIND_MONO if mono else pdf_render.KIND_BODY
        return (kind, f"{label}: {value}")

    sha = str(signature.get("doc_sha256") or "")
    rows: List[Tuple[str, str]] = [
        (pdf_render.KIND_GAP, ""),
        (pdf_render.KIND_BODY, "-" * 72),
        (pdf_render.KIND_SUB, "Electronic signature record"),
        row("Contributor", signature.get("physician") or ""),
        row("Signed by", signature.get("typed_name") or ""),
        row("Initials", signature.get("signed_initials") or ""),
        row("Signed at (UTC)", signature.get("signed_at") or "", mono=True),
        row("Portal account", signature.get("user_id") or "", mono=True),
        row("Email of record", signature.get("signer_email") or "-"),
        row("Network address", signature.get("ip") or "-", mono=True),
        row("Client", (signature.get("user_agent") or "-")[:80]),
        row("Agreement version", signature.get("doc_version") or ""),
        (pdf_render.KIND_GAP, ""),
        (pdf_render.KIND_BODY, "SHA-256 of the exact agreement text signed:"),
        (pdf_render.KIND_MONO, sha[:32]),
        (pdf_render.KIND_MONO, sha[32:]),
        (pdf_render.KIND_GAP, ""),
    ]
    for chunk in pdf_render.wrap(
        "The signer consented to conduct this transaction electronically, in "
        "accordance with the Electronic Signatures in Global and National "
        "Commerce Act, 15 U.S.C. 7001 et seq., and the Uniform Electronic "
        "Transactions Act. This record was generated by Archangel Health at the "
        "moment of signature and is retained unaltered."
    ):
        rows.append((pdf_render.KIND_BODY, chunk))
    return rows


def render_pdf(*, physician: str, version: str, signature: Dict[str, Any]) -> bytes:
    """The immutable countersigned document: agreement text, then the record.

    ``version`` is a REQUIRED argument with no default, and that is the point of
    this signature. The artifact must be rendered from the version the physician
    actually signed, not from whatever ``CURRENT_VERSION`` happens to be when
    somebody asks for a copy -- otherwise shipping v2 would silently rewrite
    every v1 signer's executed contract into a document they never saw.
    """
    body = render_markdown(
        physician=physician, version=version,
        signer_name=str(signature.get("typed_name") or ""),
        signed_initials=str(signature.get("signed_initials") or ""),
        signed_at=str(signature.get("signed_at") or ""),
    )
    rows = dla.markdown_rows(body) + signature_rows(
        dict(signature, physician=physician, doc_version=version))
    banner = (f"Archangel Health - Physician Contributor Agreement {version} - "
              f"executed {str(signature.get('signed_at') or '')[:10]}")
    return pdf_render.render_text_pdf(rows, banner=banner)


def pdf_from_row(*, physician: str, row: Dict[str, Any]) -> bytes:
    """Rebuild the executed PDF from a ``physician_agreements`` row.

    THE ROW IS THE RECORD, not the blob, on the DLA's reasoning verbatim: an
    asset store that loses a blob becomes an inconvenience rather than the loss
    of a contract. The version comes from the ROW and there is no fallback to
    ``CURRENT_VERSION``, because a row with no readable version is a row we
    cannot rebuild honestly and a wrong document is worse than an error.
    """
    version = str(row.get("doc_version") or "")
    if not _VERSION_RE.match(version):
        raise AgreementError(
            f"signature row {row.get('agreement_id')!r} names no readable version")
    return render_pdf(physician=physician, version=version, signature=dict(row))


def pdf_filename(*, physician: str, version: str) -> str:
    """A filename a person can find in a downloads folder six months later."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (physician or "physician")).strip("-")[:48]
    return f"Archangel-Physician-Agreement-{version}-{slug or 'physician'}.pdf"


def clear_cache() -> None:
    """Tests that write a temporary agreement file need the loader to forget."""
    _load_source.cache_clear()
