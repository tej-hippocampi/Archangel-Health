"""The data licensing agreement: source of truth, rendering, hashing, PDF.

The agreement is a FILE (``docs/legal/DLA_v<n>.md``), not a string in this
module and not a row in a table. That is the whole design:

  * a lawyer can read and redline it without opening a Python file;
  * ``git log`` on it is the amendment history;
  * and the version a partner signed is recoverable from the repository at the
    commit that was deployed, which is what "the signed version is versioned"
    has to mean if it is going to survive an argument.

WHAT IS HASHED is the exact text the signer saw, after organization
substitution and after the signer blanks are rendered as blanks. Not the file on
disk -- two organizations sign different texts, because each names its own
Licensor -- and not the PDF, whose bytes carry a timestamp and a signature block
that did not exist when the signer read the words. ``render()`` is therefore
deterministic for a given (version, organization): call it at read time to
display, call it again at sign time to verify, and the two hashes agree or
something changed underneath the signer and the signature is refused.

THE EFFECTIVE DATE is a phrase rather than a date, deliberately. A rendered date
would change between the moment the agreement is displayed and the moment it is
signed, so the hash the signer saw would never match the hash at signature --
and the only ways out of that are to hash something other than what was on
screen, or to freeze a date before anyone agreed to it. Both are worse than a
contract that says it takes effect when it is signed, which is what most of them
say anyway.
"""

from __future__ import annotations

import hashlib
import os
import re
import realm as _realm
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from asclepius import pdf_render

#: The version offered for signature today. Bumping this means adding
#: ``docs/legal/DLA_v<n>.md``; the old file stays, because rows in
#: ``signed_agreements`` point at it.
CURRENT_VERSION = "v1"

#: The phrase that stands in for a date nobody can know at render time. See the
#: module docstring.
_EFFECTIVE_DATE_PHRASE = (
    "the date of Licensor's signature recorded in the signature record below"
)

#: What an unfilled signature line looks like on screen and in the hashed text.
_BLANK = "________________________"

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_VERSION_RE = re.compile(r"^v[0-9]+$")


class AgreementError(RuntimeError):
    """The document could not be loaded or rendered. Never shown to a partner."""


def docs_dir() -> str:
    """``<repo>/docs/legal``. Overridable for tests via ``ARCHANGEL_LEGAL_DIR``."""
    override = (os.getenv("ARCHANGEL_LEGAL_DIR") or "").strip()
    if override:
        return override
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
    return os.path.join(os.path.dirname(here), "docs", "legal")


def available_versions() -> List[str]:
    try:
        names = os.listdir(docs_dir())
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith("DLA_") and n.endswith(".md"):
            ver = n[len("DLA_"):-len(".md")]
            if _VERSION_RE.match(ver):
                out.append(ver)
    return sorted(out, key=lambda v: int(v[1:]))


@lru_cache(maxsize=8)
def _load_source(version: str) -> str:
    """The file, comments stripped, newlines normalized.

    Cached because it is read on every portal page load of the agreement and it
    cannot change without a deploy. The cache key includes the version, so a new
    version is never masked by an old read.
    """
    if not _VERSION_RE.match(version or ""):
        raise AgreementError(f"not an agreement version: {version!r}")
    path = os.path.join(docs_dir(), f"DLA_{version}.md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise AgreementError(f"agreement {version} is not readable: {exc}") from exc
    text = _COMMENT_RE.sub("", raw)
    # Normalize line endings before hashing anything: a checkout on Windows must
    # not produce a different agreement from a checkout on Linux.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse the runs of blank lines the stripped comments leave behind, so
    # the rendered document does not open with a hole.
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


def render_markdown(*, organization: str, version: str = CURRENT_VERSION,
                    signer_name: str = "", signer_title: str = "",
                    signed_at: str = "") -> str:
    """The source with its placeholders filled in, still carrying its markers.

    INTERNAL. Only the PDF layout consumes this, because it needs the heading
    markers to decide type sizes. Nothing displays it and nothing hashes it --
    see ``render`` for why.
    """
    org = " ".join((organization or "").split()) or "Licensor"
    text = _load_source(version)
    substitutions = {
        "LICENSOR_NAME": org,
        "EFFECTIVE_DATE": _EFFECTIVE_DATE_PHRASE,
        "SIGNER_NAME": signer_name or _BLANK,
        "SIGNER_TITLE": signer_title or _BLANK,
        "SIGNED_AT": signed_at or _BLANK,
    }
    for key, value in substitutions.items():
        text = text.replace("{{" + key + "}}", value)
    return text


#: Emphasis, possibly spanning a line break -- ``**Expert\nDetermination**`` is
#: one run in the source and two lines on screen. Bounded to 400 characters so a
#: stray unbalanced marker eats a phrase rather than the rest of the contract.
_EMPHASIS_RE = re.compile(r"\*{1,3}(?=\S)(.{1,400}?)(?<=\S)\*{1,3}", re.S)
_RULE_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")


def to_plain(markdown: str) -> str:
    """Markdown source -> the document a person actually reads.

    THE FILE IS MARKDOWN SO A LAWYER CAN EDIT IT. That is the only reason, and
    it is a good one -- but a contract shown to a hospital's CIO with visible
    ``##`` and ``**`` reads as an unfinished draft, and the reasonable-notice
    question a court asks about a clickwrap is about the document as PRESENTED.
    So there is exactly one canonical rendering, produced here, and it is what
    the portal displays, what gets hashed, and what the PDF prints. Not three
    nearly-identical strings that could drift.

    Deliberately narrow: headings lose their hashes, emphasis loses its
    asterisks, a horizontal rule becomes a rule. Nothing is reflowed, reordered
    or reworded, because every one of those would change the document.
    """
    # Emphasis FIRST, over the whole text: a run like ``**Expert\nDetermination**``
    # is one span in the source and two lines on screen, so a line-by-line pass
    # would leave half of every wrapped one behind.
    text = _EMPHASIS_RE.sub(r"\1", markdown or "")
    out = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if _RULE_RE.match(stripped):
            out.append("-" * 60)
            continue
        if stripped.startswith("#"):
            line = line.lstrip("#").strip()
        out.append(line)
    return "\n".join(out)


def render(*, organization: str, version: str = CURRENT_VERSION,
           signer_name: str = "", signer_title: str = "",
           signed_at: str = "") -> str:
    """The agreement as a person reads it.

    With the signer arguments left empty this is the SIGNABLE text: what the
    portal displays and what gets hashed. Passing them produces the EXECUTED
    text, which is only ever used to build the PDF -- never hashed, never
    displayed as if it were the thing being agreed to.
    """
    return to_plain(render_markdown(
        organization=organization, version=version, signer_name=signer_name,
        signer_title=signer_title, signed_at=signed_at))


def sha256_of(text: str) -> str:
    """The hash convention for this document, in one place: UTF-8, no
    normalization beyond what ``render`` already did."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def signable(*, organization: str, version: str = CURRENT_VERSION) -> Tuple[str, str]:
    """``(text, sha256)`` for display and for signature verification."""
    text = render(organization=organization, version=version)
    return text, sha256_of(text)


# ─── PDF ─────────────────────────────────────────────────────────────────────
def markdown_rows(text: str) -> List[Tuple[str, str]]:
    """Flatten the markdown to ``(kind, line)`` rows for the PDF writer.

    Public rather than private because the physician contributor agreement is
    the same kind of document and prints through the same writer. A second copy
    of this function would be a second way a signed record could come out
    looking different from the screen, which is the one defect neither document
    can have.

    A deliberately small subset: headings, horizontal rules, and paragraphs.
    The document is written to survive this -- no tables, no nested lists, no
    images -- because a signed record that renders differently from the screen
    is the one defect this whole feature cannot have.
    """
    rows: List[Tuple[str, str]] = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            rows.append((pdf_render.KIND_GAP, ""))
            continue
        if line.strip() in ("---", "***", "___"):
            # ASCII, not a box-drawing glyph: the PDF font is cp1252 and a
            # character it cannot draw is substituted, so a horizontal rule
            # made of U+2500 renders as a row of question marks.
            rows.append((pdf_render.KIND_BODY, "-" * 72))
            continue
        stripped = line.lstrip("#").strip() if line.startswith("#") else line
        if line.startswith("# "):
            kind = pdf_render.KIND_HEAD
        elif line.startswith("#"):
            kind = pdf_render.KIND_SUB
        else:
            kind = pdf_render.KIND_BODY
        # Strip the emphasis markers rather than rendering them literally: bold
        # is not available in a single-font PDF and `**Section 3.1**` on a
        # contract page reads as a typo.
        clean = stripped.replace("**", "").replace("*", "")
        if kind == pdf_render.KIND_BODY:
            for chunk in pdf_render.wrap(clean):
                rows.append((pdf_render.KIND_BODY, chunk))
        else:
            for chunk in pdf_render.wrap(clean, width=70):
                rows.append((kind, chunk))
    return rows


def signature_rows(signature: Dict[str, Any]) -> List[Tuple[str, str]]:
    """The evidence block appended to every executed PDF.

    Everything a court would ask for, on one page, in the order it would be
    asked: who, in what capacity, when, from where, with what consent, over
    which exact text.
    """
    def row(label: str, value: Any, mono: bool = False) -> Tuple[str, str]:
        kind = pdf_render.KIND_MONO if mono else pdf_render.KIND_BODY
        return (kind, f"{label}: {value}")

    sha = str(signature.get("doc_sha256") or "")
    rows: List[Tuple[str, str]] = [
        (pdf_render.KIND_GAP, ""),
        (pdf_render.KIND_BODY, "-" * 72),
        (pdf_render.KIND_SUB, "Electronic signature record"),
        row("Licensor", signature.get("organization") or ""),
        row("Signed by", signature.get("typed_name") or ""),
        row("Title", signature.get("typed_title") or ""),
        row("Signed at (UTC)", signature.get("signed_at") or "", mono=True),
        row("Portal account", signature.get("signer_user_id") or "", mono=True),
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
        "The signer affirmed authority to bind the Licensor and consented to "
        "conduct this transaction electronically, in accordance with the "
        "Electronic Signatures in Global and National Commerce Act, 15 U.S.C. "
        "§ 7001 et seq., and the Uniform Electronic Transactions Act. This "
        "record was generated by Archangel Health at the moment of signature "
        "and is retained unaltered."
    ):
        rows.append((pdf_render.KIND_BODY, chunk))
    return rows


def render_pdf(*, organization: str, version: str, signature: Dict[str, Any]) -> bytes:
    """The immutable countersigned document: agreement text, then the record.

    The BODY is re-rendered with the signer's details filled into the signature
    lines, so a reader of the PDF sees an executed contract rather than a blank
    one with a note stapled to it. The HASH printed in the record is still the
    hash of the SIGNABLE text -- what they agreed to -- and never of this
    rendering, which is why both appear in the same document.
    """
    # The MARKDOWN, not the plain text: the heading markers are what tell the
    # layout which lines are headings. The WORDS are identical either way --
    # `to_plain` only removes markers -- so the PDF prints the same document
    # that was displayed and hashed.
    body = render_markdown(
        organization=organization, version=version,
        signer_name=str(signature.get("typed_name") or ""),
        signer_title=str(signature.get("typed_title") or ""),
        signed_at=str(signature.get("signed_at") or ""),
    )
    rows = markdown_rows(body) + signature_rows(dict(signature, organization=organization))
    banner = (f"Archangel Health · Data Licensing Agreement {version} · "
              f"executed {str(signature.get('signed_at') or '')[:10]}")
    # Sandbox PRD §5: a test signature can never be mistaken for a real one if
    # the file is ever moved — the document header says which realm signed it.
    if _realm.is_sandbox():
        banner = "SANDBOX: test signature, not a real agreement · " + banner
    return pdf_render.render_text_pdf(rows, banner=banner)


def pdf_from_row(*, organization: str, row: Dict[str, Any]) -> bytes:
    """Rebuild the executed PDF from a ``signed_agreements`` row.

    THE ROW IS THE RECORD, not the blob. Everything the PDF prints -- the
    signer, the title, the timestamp, the address, the client, the hash of what
    was signed -- lives in the row, so the document is reproducible from it
    exactly. That makes an asset store that loses a blob an inconvenience rather
    than the loss of a contract, which is worth having given the store is a
    volume somebody has to remember to make persistent.

    It does NOT make the blob redundant: the blob is the artifact that was
    hashed, emailed and downloaded, and a rebuild that differed from it byte for
    byte would be a different document. So callers verify the rebuild against
    ``pdf_sha256`` and say plainly when it does not match.
    """
    return render_pdf(organization=organization,
                      version=str(row.get("doc_version") or CURRENT_VERSION),
                      signature=dict(row))


def pdf_filename(*, organization: str, version: str) -> str:
    """A filename a person can find in a downloads folder six months later."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (organization or "licensor")).strip("-")[:48]
    return f"Archangel-DLA-{version}-{slug or 'licensor'}.pdf"


def utcnow_iso() -> str:
    """Whole-second UTC, matching the store's stamp format."""
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()


def clear_cache() -> None:
    """Tests that write a temporary agreement file need the loader to forget."""
    _load_source.cache_clear()
