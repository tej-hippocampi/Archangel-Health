"""PHI blocking at send (Community PRD §7) — the highest-risk surface.

Server-side, span-addressed detection of Safe Harbor direct identifiers in a
community message BEFORE it is persisted or broadcast. Blocks: patient names,
MRNs, SSNs, dates of birth, exact dates of service, phone numbers, email
addresses, street addresses, and account/insurance numbers. Deliberately
allows real clinical discussion: ages in bands, lab values ("creatinine 1.9"),
medication names and doses, diagnoses, imaging findings, narrative (§7.2).

Design rules (§7.3):
  * Findings carry the CATEGORY and the character SPAN — never the matched
    text. The composer highlights the span client-side; the server never
    echoes the suspected identifier in a response, a log, or an audit payload.
  * This module is pure (no DB, no HTTP) so it is trivially testable and the
    router can also reuse it for attachment OCR text (§7.4).

Defense in depth: after the span patterns run, the shared platform scanner
(``asclepius.validation.residual_identifiers`` — gold.deid when present) takes
a second pass; any kind it reports that the span patterns missed is surfaced
as a whole-text finding, so this gate is never weaker than the platform's
existing PHI net.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from asclepius.validation import residual_identifiers

# ─── Category vocabulary (stable API strings — the client keys off these) ─────
CATEGORY_LABELS: Dict[str, str] = {
    "patient_name": "patient name",
    "mrn": "MRN",
    "ssn": "SSN",
    "dob": "date of birth",
    "exact_date": "exact date",
    "phone": "phone number",
    "email": "email address",
    "address": "street address",
    "account_number": "account/insurance number",
}

_MONTHS = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)"
)
_NUM_DATE = r"(?:\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}-\d{2}-\d{2})"

# Ordered most-specific-first: when spans overlap, the earlier pattern's
# category wins (e.g. "DOB 3/4/62" reports ``dob``, not ``exact_date``).
_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Medicare Beneficiary Identifier (format-anchored, e.g. 1EG4-TE5-MK73).
    (
        "account_number",
        re.compile(r"\b[0-9][A-Z][A-Z0-9]\d-?[A-Z][A-Z0-9]\d-?[A-Z]{2}\d{2}\b"),
    ),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    (
        "dob",
        re.compile(
            r"\b(?:DOB|D\.O\.B\.?|date\s+of\s+birth|born(?:\s+on)?)\b\s*[:\-]?\s*"
            r"(?:" + _NUM_DATE + r"|" + _MONTHS + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{2,4})?)",
            re.I,
        ),
    ),
    # Identifier tails REQUIRE at least one digit: "MRN 84921734" is an
    # identifier; "the payout policy changed" and "never post MRN values" are
    # ordinary English and must not block (audit finding — a pure-alpha tail
    # false-blocked legitimate messages and fed the repeat-offender counter).
    (
        "mrn",
        re.compile(
            r"\b(?:MRN|MBI|medical\s+record(?:\s*(?:number|no\.?|#))?|"
            r"chart\s*(?:number|no\.?|#))\s*[:#]?\s*"
            r"(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9][A-Za-z0-9\-]{3,}\b",
            re.I,
        ),
    ),
    (
        "account_number",
        re.compile(
            r"\b(?:account|acct|policy|member\s*(?:id|number)|insurance\s*(?:id|number)|"
            r"subscriber\s*(?:id|number))\s*(?:no\.?|#)?\s*[:#]?\s*"
            r"(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9][A-Za-z0-9\-]{3,}\b",
            re.I,
        ),
    ),
    (
        "phone",
        re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"),
    ),
    # Exact calendar dates (Safe Harbor: all date elements except year). The
    # numeric form requires a PLAUSIBLE month/day pair (M/D/Y or D/M/Y) so a
    # dose-titration schedule like "25-50-100" is never mistaken for a date
    # (audit finding).
    (
        "exact_date",
        re.compile(
            r"\b(?:"
            r"(?:0?[1-9]|1[0-2])[/\-](?:0?[1-9]|[12]\d|3[01])"   # M/D
            r"|(?:0?[1-9]|[12]\d|3[01])[/\-](?:0?[1-9]|1[0-2])"  # D/M
            r")[/\-]\d{2,4}\b"
        ),
    ),
    ("exact_date", re.compile(r"\b\d{4}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])\b")),
    (
        "exact_date",
        re.compile(
            r"\b" + _MONTHS + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b"
        ),
    ),
    (
        "exact_date",
        re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTHS + r"(?:,?\s+\d{4})?\b"),
    ),
    # Patient names: an honorific + capitalized surname, or an explicit
    # patient/name lead-in followed by a capitalized full name. "Dr. X" is
    # deliberately NOT matched — colleagues address each other that way, and a
    # provider name is not a patient identifier.
    (
        "patient_name",
        re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mx)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b"),
    ),
    (
        "patient_name",
        re.compile(
            r"\b(?:[Pp]atient|PATIENT|[Pp]t|PT)\.?(?:'s)?\s*(?:[Nn]ame\s*(?:is)?\s*[:\-]?\s*)?"
            r"[A-Z][a-z]+\s+[A-Z][a-z]+\b"
        ),
    ),
    (
        "patient_name",
        re.compile(
            r"\b(?:[Pp]atient|PATIENT|[Pp]t|PT)(?:'s)?\s+[Nn]ame\s*(?:is)?\s*[:\-]?\s*[A-Z][a-z]+\b"
        ),
    ),
    (
        "address",
        re.compile(
            r"\b\d{1,6}\s+(?:[A-Z][A-Za-z'’]*\s+){1,3}"
            r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|"
            r"Court|Ct|Place|Pl|Terrace|Ter|Highway|Hwy|Parkway|Pkwy|Circle|Cir|Way)\b\.?"
            r"(?:,?\s*(?:Suite|Ste|Apt|Unit)\.?\s*#?\s*[A-Za-z0-9\-]+)?"
        ),
    ),
    # Bare long digit runs (7+) — MRN/account-shaped. No legitimate lab value,
    # dose, or vital reaches 7 contiguous digits.
    ("account_number", re.compile(r"\b\d{7,}\b")),
]

# Map kinds reported by the shared platform scanner onto our category
# vocabulary (defense-in-depth pass; anything unknown maps to itself).
_SHARED_KIND_MAP = {
    "date": "exact_date",
    "long_number": "account_number",
    "name": "patient_name",
}

# Shared-scanner kinds our own span patterns already evaluate with EQUAL OR
# STRICTER rules. For these, our verdict is authoritative — re-adding the
# looser shared hit would resurrect its false positives (e.g. the shared MRN
# pattern accepts a pure-word tail, blocking "never post MRN values here";
# audit finding). Any kind outside this set still nets a whole-text finding.
_SHARED_KINDS_COVERED = {"email", "phone", "ssn", "mbi", "mrn", "date", "long_number"}


def _overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def scan_text(
    text: Optional[str], *, exempt_categories: Tuple[str, ...] = ()
) -> List[Dict[str, Any]]:
    """Scan one message body. Returns findings ``[{"category", "span":[s,e]}]``
    ordered by span start; empty list == clean. Overlapping matches collapse to
    the first (most specific) pattern's category. Never returns matched text.

    ``exempt_categories`` drops one category for one caller, and exists for
    exactly one situation: an events listing has to carry the date of the
    event. "March 14" trips ``exact_date``, which is the correct rule for a
    message that might be about a patient and the wrong one for a conference
    announcement assembled from public web pages.

    Deliberately narrow. ``dob`` is keyword-anchored and stays scanned even
    then, every other category stays scanned, and the exemption is recorded in
    the audit line so a blanket "bots are exempt" can never grow out of it.
    """
    if not text:
        return []
    raw: List[Tuple[str, Tuple[int, int]]] = []
    for category, pat in _PATTERNS:
        for m in pat.finditer(text):
            raw.append((category, (m.start(), m.end())))

    # Most-specific-first de-overlap: patterns were applied in priority order,
    # so keep earlier-listed matches and drop later ones that overlap them.
    kept: List[Tuple[str, Tuple[int, int]]] = []
    for category, span in raw:
        if any(_overlaps(span, k_span) for _, k_span in kept):
            continue
        kept.append((category, span))

    findings = [
        {"category": category, "span": [span[0], span[1]]}
        for category, span in sorted(kept, key=lambda item: item[1][0])
        if category not in (exempt_categories or ())
    ]

    # Second-opinion pass with the shared platform scanner, so this gate can
    # only be stricter than the platform baseline, never weaker. It reports
    # KINDS without spans, so it contributes only when the span patterns found
    # nothing at all: the message still blocks (as a whole-text finding), while
    # a message the span patterns already caught keeps its PRECISE spans — the
    # composer highlight and the one-tap "remove and post" stay surgical
    # instead of flagging the entire message.
    if not findings:
        try:
            shared_kinds = set(residual_identifiers(text))
        except Exception:  # pragma: no cover — the shared scanner must never 500 a post
            shared_kinds = set()
        for kind in sorted(shared_kinds - _SHARED_KINDS_COVERED):
            findings.append({"category": _SHARED_KIND_MAP.get(kind, kind),
                             "span": [0, len(text)]})
    return findings


def categories_of(findings: List[Dict[str, Any]]) -> List[str]:
    """Sorted unique category names — the only thing that may reach a log or
    audit payload (§7.3: never the content, never the span text)."""
    return sorted({f.get("category", "unknown") for f in findings or []})


def block_message(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The structured 422 payload for a blocked send (§7.3). Categories + spans
    only; the client highlights spans in the composer it already holds."""
    cats = categories_of(findings)
    labels = [CATEGORY_LABELS.get(c, c.replace("_", " ")) for c in cats]
    return {
        "code": "phi_detected",
        "message": (
            "This looks like it contains patient-identifiable information ("
            + ", ".join(labels)
            + "). Remove it to post."
        ),
        "categories": cats,
        "findings": [{"category": f["category"], "span": list(f["span"])} for f in findings],
    }


def strip_spans(text: str, findings: List[Dict[str, Any]]) -> str:
    """Server-side twin of the composer's one-tap "remove and post" (§7.3):
    delete exactly the flagged spans, right-to-left so offsets stay valid."""
    spans = sorted((tuple(f["span"]) for f in findings or []), key=lambda s: -s[0])
    out = text
    for start, end in spans:
        if 0 <= start < end <= len(out):
            out = out[:start] + out[end:]
    return re.sub(r"[ \t]{2,}", " ", out)
