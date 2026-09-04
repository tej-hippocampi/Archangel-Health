"""``note_text`` adapter (EHR PRD §6) — a plain-text/markdown clinical note →
``notes`` fragments. ``note_type`` comes from the manifest, a filename hint, or
the document-type token partners embed in the filename; ``author_role`` is
always a GENERALIZED role (a specialty/service line), never a person — if a
manifest supplies something name-shaped we fall back to "clinician" rather than
carry it.

**The note's date travels with it** (Case Generation Fix PRD §A1). A partner's
text export carries the service date twice — in the filename
(``072_2025-07-01_discharge-summary.txt``) and in a header line (``Service
date: 2025-07-01``) — and this adapter used to read neither. The consequence,
measured on the committed patient-3 chart: 0 of 79 text notes carried an
offset after ``normalize_timeline``, so none of them could form or join an
encounter, the planner truthfully reported one resource type, and a chart with
four discharge summaries yielded "no clinical note ≥ 200 chars". The date is
emitted here as a RAW ``collected_at`` string, exactly as the FHIR adapter does
for a ``DocumentReference.date``; ``timeline.normalize_timeline`` converts it to
a relative ``collected_offset_days`` and destroys the calendar value.

Only a DATE-SHAPED value is ever emitted. A header reading ``Service date:
unknown-date`` (which real exports write for continuation pages) must leave the
note undated — emitting the literal would reach ``_assign_offset`` as an
unparseable date and quarantine the whole chart over a page that simply has no
date, when the planner already tolerates an undated minority.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

_KNOWN_TYPES = ("H&P", "Progress", "Consult", "Nursing", "Discharge", "ED Provider", "Operative")

# A role must look like a service line, not a person: letters/spaces only, no
# honorifics, no digits. "nephrology" ✓ · "Dr. Jane Doe" ✗ · "J. Smith RN" ✗
_ROLE_OK_RE = re.compile(r"^[a-z][a-z /&-]{1,40}$")
_HONORIFIC_RE = re.compile(r"\b(dr|md|do|rn|np|pa)\b\.?", re.IGNORECASE)

# ─── Where a text note's date lives ──────────────────────────────────────────
# Header lines, in order of authority — each one names the DOCUMENT's own
# date. ``Admission Date`` is deliberately not here: it sits inside a discharge
# summary's content and describes the start of the stay, so dating the summary
# by it would place the document BEFORE the course it narrates (and before the
# decision point it should be sealed behind). A note with only that line stays
# undated, which fails closed.
_DATE_HEADER_RES = tuple(re.compile(
    r"^[ \t]*" + label + r"[ \t]*:[ \t]*(?P<value>\S[^\n]*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
    for label in (
        r"service\s+date", r"date\s+of\s+service", r"report\s+date",
    ))
# The partner's filename convention: ``<index>_<YYYY-MM-DD>_<document-type>.txt``.
_FILENAME_DATE_RE = re.compile(r"^\d+_(\d{4}-\d{2}-\d{2})_")
# The shapes ``timeline.parse_datetime`` reads. Anything else — ``unknown-date``,
# ``n/a``, a stray sentence — is NOT a date and is never emitted as one.
_DATE_SHAPED_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"      # ISO date / datetime
    r"|\d{1,2}/\d{1,2}/\d{2,4}"                                   # slash date (order per record)
    r"|\d{8}(?:\d{2,6})?)$")                                      # HL7 TS

# ─── Document-type token → note_type ─────────────────────────────────────────
# The token after the date in the partner's filename. Checked most-specific
# first, and only after the manifest and the ``_KNOWN_TYPES`` scan, so a partner
# who names a file ``consult`` still gets ``Consult``.
_LAB_TOKEN_RE = re.compile(
    r"(?:^|[-_ ])(?:lab|labs|rft|cbc|lft|electrolytes?|coagulation|urinalysis|serology|"
    r"tumor[-_ ]?marker|hematology|biochemistry|other[-_ ]lab|[a-z]+[-_ ]lab)(?:$|[-_ .])")
_FILENAME_TYPE_TOKENS = (
    ("discharge", "Discharge"),
    ("radiology", "Radiology"),
    ("clinical-note", "Progress"),
    ("clinical_note", "Progress"),
)


def _note_type_from(filename: Optional[str], manifest_type: Optional[str]) -> str:
    if manifest_type:
        for t in _KNOWN_TYPES:
            if t.lower() == str(manifest_type).strip().lower():
                return t
        return str(manifest_type).strip()[:40] or "Progress"
    # The FILE name first — it is the most specific thing a partner wrote. The
    # directory is a fallback, not a peer: a ``clinical-notes/`` folder holding
    # ``009_…_rft-renal.txt`` names a lab report, and letting the folder's
    # "clinical-note" token win would call it Progress.
    base = os.path.basename(filename or "").lower()
    found = _type_from_token_source(base)
    if found:
        return found
    # A partner whose layout is ``discharge/001.txt`` carries the type in the
    # folder, and that classified before this adapter learned to read dates.
    folders = os.path.dirname((filename or "").lower())
    if folders:
        found = _type_from_token_source(folders)
        if found:
            return found
    return "Progress"


def _type_from_token_source(name: str) -> Optional[str]:
    """The note type named by ``name`` (a basename or a directory path), or None
    when it names nothing. Most specific first, so a partner who names a file
    ``consult`` still gets ``Consult`` ahead of the lab token."""
    if not name:
        return None
    for t in _KNOWN_TYPES:
        if t.lower().replace("&", "") in name.replace("&", "").replace("_", " ").replace("-", " "):
            return t
    for token, label in _FILENAME_TYPE_TOKENS:
        if token in name:
            return label
    if "consult" in name:
        return "Consult"
    if _LAB_TOKEN_RE.search(name):
        # ``rft-renal.txt``, ``cbc-hematology.txt``, ``other-lab.txt``: a lab
        # report transcribed as text. Labelled as what it is rather than
        # ``Progress``, which is what 79 of patient-3's files used to read as.
        return "Lab report"
    return None


def _date_shaped(value: Optional[str]) -> Optional[str]:
    """``value`` if it is a date ``timeline.parse_datetime`` will actually read,
    else None. The shape check alone let ``2025-13-45`` or an eight-digit
    document number through, and an emitted-but-unparseable ``collected_at``
    quarantines the whole chart — over a header that was ignored before."""
    from asclepius.timeline import parse_datetime

    v = str(value or "").strip()
    if not v or not _DATE_SHAPED_RE.match(v):
        return None
    return v if parse_datetime(v) is not None else None


def note_date_from(text: str, filename: Optional[str], manifest_date: Optional[Any] = None,
                   ) -> Optional[str]:
    """The note's service date as a RAW date string, or None.

    Precedence (§A1): ``manifest.date`` → a header line (``Service date`` /
    ``Date of service`` / ``Report date``) → the filename pattern
    ``^\\d+_YYYY-MM-DD_``. Only a value ``parse_datetime`` reads is returned; a
    file whose header and name both say ``unknown-date`` stays undated, and so
    does one whose header carries a malformed date.
    """
    if manifest_date is not None:
        d = _date_shaped(str(manifest_date))
        if d:
            return d
    for pat in _DATE_HEADER_RES:
        m = pat.search(text or "")
        if m:
            d = _date_shaped(m.group("value"))
            if d:
                return d
            # A header that names the field but carries no date ("unknown-date")
            # is an explicit statement that the page is undated; do not let a
            # lower-authority header override it, but the filename may still
            # carry the date (it does, for every dated page in the real export).
            break
    m2 = _FILENAME_DATE_RE.match(os.path.basename(filename or ""))
    if m2:
        # Same rule as the header: a filename reading ``072_2025-13-45_…`` is a
        # typo, not a date, and emitting it would quarantine the whole chart.
        return _date_shaped(m2.group(1))
    return None


def _safe_role(candidate: Optional[str], specialty: str) -> str:
    c = (candidate or "").strip().lower()
    if c and _ROLE_OK_RE.match(c) and not _HONORIFIC_RE.search(c):
        return c
    return (specialty or "clinician").strip().lower() or "clinician"


def parse(raw: Any, *, specialty: str = "general", manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Note text (str/bytes) → ``{"notes": [{note_type, author_role, text[, collected_at]}]}``.
    ``manifest`` may carry ``{note_type, author_role, filename, patient_key, date}``.

    ``collected_at`` is a raw calendar string for ``timeline.normalize_timeline``
    to convert; it never survives into the case (``_assign_offset`` deletes it)."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    text = text.strip()
    if not text:
        return {"notes": []}
    m = manifest or {}
    note: Dict[str, Any] = {
        "note_type": _note_type_from(m.get("filename"), m.get("note_type")),
        "author_role": _safe_role(m.get("author_role"), specialty),
        "text": text,
    }
    when = note_date_from(text, m.get("filename"), m.get("date"))
    if when:
        note["collected_at"] = when
    frag: Dict[str, Any] = {"notes": [note]}
    if m.get("patient_key"):
        frag["_patient_keys"] = [str(m["patient_key"])]
    return frag
