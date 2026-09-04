"""Timeline normalization — shifted calendar dates → relative day offsets
(Real EHR Ingestion PRD §7). THE bridge that makes partner data ingestible.

Context-preserved de-identification means DATE-SHIFTING: the partner's export
carries shifted calendar dates, while our ``ClinicalCase`` model demands relative
integer offsets (``collected_offset_days``) and the ``deidentify()`` guard
rejects any surviving date string. This module converts between the two worlds:

  * every structured timestamp becomes ``(event_date − index_date).days`` — an
    int anchored to the case's index event (day 0 = index, −7 = a week prior).
    The clinically-vital INTERVALS survive exactly; the calendar is destroyed.
  * free-text notes are rewritten in place: each parseable date token becomes its
    relative form (``[day -5]``), so a note's temporal logic ("admitted 3/14,
    dialysis 3/19") survives as ("admitted [day -5], dialysis [day 0]").
  * ages ≥90 in note text collapse to the Safe-Harbor ``90+`` bucket.

Anything date-LIKE the rewriter cannot confidently parse is reported as
``unresolved`` (masked) so ingestion can quarantine instead of guessing — a
wrong guess destroys clinical meaning; a missed date is a breach. The final
``deidentify()`` guard still runs downstream as the hard post-condition.

Ordering is load-bearing (PRD §7): parse → assemble → **normalize (this)** →
verify → ``deidentify()``. Running the guard first rejects 100% of real data.

RE-IDENTIFICATION SAFETY: the resolved index DATE is never returned, logged, or
persisted — only its provenance ("manifest" / "latest_observation"). Storing the
anchor date would be a key back to the partner's (already shifted) calendar.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple


class TimelineError(ValueError):
    """The case's timeline cannot be normalized (no parseable anchor, or an
    explicit index_event that does not parse). The bundle should quarantine."""


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Date tokens we can CONFIDENTLY parse, most-specific first. ISO first so
# "2024-03-14T09:30:00Z" consumes the whole timestamp, not just the date part.
_ISO_RE = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
# A three-part slash date. Which component is the month depends on the record's
# date order — see ``infer_date_order``; ``_MDY_RE`` is retained as an alias
# because the module's public behaviour (and ``parse_datetime``) defaults to
# month-first for structured fields.
_SLASH_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?!\d)")
_MDY_RE = _SLASH_DATE_RE
_MONTHNAME_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
# Month/YEAR partials (review finding: these leaked through every layer —
# neither the full-date rewriters nor the PHI scanners match "12/2024",
# "12-2024", or "March 2024"). Safe Harbor allows the YEAR alone, so these
# rewrite to just the year (temporal context survives, the month leaves).
_MONTHYEAR_NUM_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[/-](\d{4})(?!\d)")
_MONTHYEAR_NAME_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
# Date-LIKE shapes we deliberately do NOT guess at (ambiguous partials like
# "3/14" with no year): flagged unresolved for quarantine. Plausibility-gated
# (review finding: the old \d{1,2}[/-]\d{1,2} flagged "BP 90/60" and
# "1-2 weeks", quarantining ordinary clinical notes): slash form requires a
# valid month and an unambiguous day (13–31, so "1/2 tablet" fractions pass);
# dash partials are only date-like WITH a year (handled above) — bare "1-2"
# ranges are prose. Residual risk (e.g. "3/5" month+day both ≤12) is accepted
# and documented; the hard deidentify() guard is unchanged.
_DATELIKE_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])/(1[3-9]|2\d|3[01])(?:/\d{2,4})?(?!\d)")

# ─── Clinical ratios are not dates (Real-Case Generation PRD §2.1) ────────────
# A clinical score written out of a fixed small denominator is shaped exactly like
# ``M/D``. A GCS of 10/15 parses as October 15th, ingestion records an unresolved
# token, and the case quarantines FOREVER — a score can never be resolved into a
# date by a human or a better manifest, so the case can never be released. That
# made every stroke / seizure / TBI / ICU record unpromotable. Verified against
# real ICU notes: 13 such hits in one chart.
#
# Two conditions, both required, because the denominator ALONE is not enough:
# "3/15" is also a perfectly good ambiguous March-date, and silently exempting it
# would turn a quarantine (recoverable) into a missed date (a breach). So the
# match must ALSO sit next to a scale cue — GCS, an E/V/M breakdown, power, MRC,
# Apgar, a pain score. A bare "10/15" with no cue anywhere still quarantines.
_CLINICAL_RATIO_RE = re.compile(r"(?<!\d)\d{1,2}\s*/\s*(?:5|10|15)(?!\d)")
_SCALE_CUE_RE = re.compile(
    r"(?:\bgcs\b|\bapgar\b|\bmrc\b|\bpain\s*(?:score|scale)?\b|\bpower\b|\breflex\w*\b"
    r"|\bmotor\b|\bverbal\b|\beye[\s-]*opening\b|\bscore\b"
    r"|\b[EVM]\s*\d\b)",           # "E4 V2 M4" — the GCS breakdown, written bare
    re.IGNORECASE,
)
# How far either side of the match we look for the cue. Generous on the left
# ("Orientation: *2 / *2; GCS 12/15 / 12/15" — the second ratio is ~8 chars past
# the cue) and short on the right ("3/5 power", "8/10 pain").
_RATIO_CUE_BEFORE = 120
_RATIO_CUE_AFTER = 40

# ─── Day-first vs month-first (Real-Case Generation PRD §2.1, extended) ───────
# ``12/03/2025`` is 12 March in most of the world and 3 December in the US. The
# rewriter used to assume month-first unconditionally, which on a day-first chart
# either produced a WRONG offset (04/03/2022 read as 3 April instead of 4 March —
# "a wrong guess destroys clinical meaning") or failed to parse at all and left a
# mangled residue behind. Measured on the real 14-month record: 321 tokens are
# unambiguously day-first, 0 are unambiguously month-first.
#
# So the order is INFERRED per case from the tokens that can only be read one way
# (a component > 12 fixes the order), majority wins. With no evidence either way
# we keep the historical month-first default and say so in the report — every
# such token is ambiguous by construction, and the residual risk is the one
# already documented above for ``_DATELIKE_RE``.
DATE_ORDER_MDY = "MDY"
DATE_ORDER_DMY = "DMY"

# Ages ≥90 collapse to the Safe-Harbor bucket (HIPAA §164.514(b)(2)(i)(C)).
_AGE90_RE = re.compile(r"\b(9\d|1[0-1]\d)([\s-]*(?:years?[\s-]*old|y[/.]?o\b))", re.IGNORECASE)


def _is_clinical_ratio(text: str, span: Tuple[int, int]) -> bool:
    """True when the match at ``span`` sits inside a /5, /10 or /15 clinical score
    AND a scale cue (GCS, E4 V2 M4, power, MRC, Apgar, pain) is nearby. Both
    conditions are required — see ``_CLINICAL_RATIO_RE``."""
    for m in _CLINICAL_RATIO_RE.finditer(text):
        if m.start() <= span[0] and m.end() >= span[1]:
            window = text[max(0, m.start() - _RATIO_CUE_BEFORE): m.end() + _RATIO_CUE_AFTER]
            if _SCALE_CUE_RE.search(window):
                return True
    return False


def _datelike_unresolved(text: str) -> List[str]:
    """MASKED date-like tokens in ``text``, minus the clinical-score exemptions."""
    return [_mask(m.group(0)) for m in _DATELIKE_RE.finditer(text or "")
            if not _is_clinical_ratio(text or "", m.span())]


def infer_date_order(texts: List[str], *, default: str = DATE_ORDER_MDY) -> Tuple[str, Dict[str, int]]:
    """Infer whether slash dates in this record are day-first or month-first from
    the tokens that can only be read one way. Returns ``(order, evidence)`` where
    evidence counts the unambiguous tokens seen. Ties and no-evidence fall back to
    ``default`` — never to a coin flip."""
    day_first = month_first = 0
    for t in texts or []:
        for m in _SLASH_DATE_RE.finditer(t or ""):
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12 and b <= 12:
                day_first += 1
            elif b > 12 and a <= 12:
                month_first += 1
    if day_first > month_first:
        order = DATE_ORDER_DMY
    elif month_first > day_first:
        order = DATE_ORDER_MDY
    else:
        order = default
    return order, {"day_first": day_first, "month_first": month_first}


def _parse_token(text: str) -> Optional[date]:
    """Parse one confidently-shaped date token to a date, else None."""
    m = _ISO_RE.fullmatch(text) or _ISO_RE.match(text)
    if m and m.group(0) == text:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_datetime(value: Any, *, date_order: str = DATE_ORDER_MDY) -> Optional[date]:
    """Best-effort parse of a STRUCTURED field value (adapter-supplied
    ``collected_at``, FHIR ``effectiveDateTime``, HL7 ``OBR-7``…) to a date.
    Returns None when it isn't a parseable date.

    ``date_order`` is the record's day-first/month-first reading
    (``infer_date_order``) and only matters for a slash token: ``06/01/2024`` is
    June 1 in an MDY record and January 6 in a DMY one. The structured path used
    to ignore the order the free-text rewriter had already inferred (Case
    Generation Fix PRD §A4), so a partner exporting ``dd/mm/yyyy`` in a CSV or
    HL7 field had every lab panel placed on the wrong day — silently, which
    corrupts encounter grouping and the longitudinal answer key. The default is
    unchanged for callers that have no record in hand."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    m = _ISO_RE.match(s)
    if m and m.start() == 0:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # HL7 TS: YYYYMMDD[HHMM[SS[.ssss]]][±ZZZZ] — real OBR-7/PID-7 values carry
    # timezone offsets and fractional seconds (review finding).
    m2 = re.match(r"^(\d{4})(\d{2})(\d{2})(?:\d{2,6})?(?:\.\d+)?(?:[+-]\d{4})?$", s)
    if m2:
        try:
            return date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        except ValueError:
            return None
    m3 = _MDY_RE.match(s)
    if m3 and m3.start() == 0:
        return _mdy_to_date(m3, date_order)
    return None


def _mdy_to_date(m: "re.Match[str]", order: str = DATE_ORDER_MDY) -> Optional[date]:
    """A three-part slash token → a date, read in the record's ``order``.

    The order is a per-record property (``infer_date_order``), not a per-token
    guess. When the token cannot be read in the record's order but CAN be read the
    other way (a day-first chart carrying one stray "3/14/2031"), the unambiguous
    reading wins — refusing it would leave a real date in the text."""
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000 if y <= 49 else 1900
    pairs = [(b, a)] if order == DATE_ORDER_DMY else [(a, b)]
    pairs.append((a, b) if order == DATE_ORDER_DMY else (b, a))
    for mo, d in pairs:
        try:
            return date(y, mo, d)
        except ValueError:
            continue
    return None


def _monthname_to_date(m: "re.Match[str]", default_year: Optional[int]) -> Optional[date]:
    mo = _MONTHS.get(m.group(1)[:3].lower())
    d = int(m.group(2))
    y = int(m.group(3)) if m.group(3) else default_year
    if not mo or y is None:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _mask(snippet: str) -> str:
    """Mask a suspected date/identifier for reporting: keep shape, hide digits."""
    return re.sub(r"\d", "•", snippet)


# ─── Partner de-identification provenance headers (V4 PRD §1.2) ───────────────
# The partner's de-identification tool writes a per-note header describing what IT
# did — "De-identification: Omitted nurse name/designation fields (green
# redaction); year as printed (11/7/21)". Measured: 413 such lines across the three
# real bundles, and one of them carries ``(11/7/21)`` — an UNSHIFTED ORIGINAL DATE
# sitting inside metadata ABOUT the de-identification.
#
# That single line breaks the date pipeline in two different directions, and both
# are wrong:
#
#   * with an index anchor, ``rewrite_note_dates`` happily parses it and rewrites
#     it to a relative offset — so a genuine leak in the PARTNER's export is
#     laundered into a plausible-looking ``[day -1257]`` and nobody is ever told;
#   * with no anchor (and on the quarantine SCRUB re-check, which runs
#     ``datelike_leftovers`` with no anchor by construction), the same line yields
#     ``11/7/21`` and the ``7/21`` inside it as unresolved date-like tokens, and
#     the whole chart quarantines on the partner's own footer.
#
# Neither outcome is right, because a provenance header is not clinical text. So it
# is REMOVED before any scan or rewrite, and what was removed is reported — the
# chart proceeds, the finding is recorded as a partner-quality advisory. That
# distinction (advisory, not quarantine, and never silent) is the whole fix.
#
# Line-anchored, NOT note-anchored: unlike ``real_cases._NON_CLINICAL_NOTE_RE``,
# which strips README-level furniture by matching a note's OPENING, this header
# sits on line 5 of an otherwise ordinary clinical note.
#
# This lives here, in the module that owns the date scan and imports nothing from
# the package, so the rewriter, the leftover scan and the curator cannot drift
# apart on what counts as a header. ``real_cases`` re-exports it.
_PROVENANCE_LINE_RE = re.compile(
    r"^[ \t]*(?:de-?identification|de-?identified|redaction|anonymi[sz]ation)[ \t]*:.*$",
    re.I | re.MULTILINE)


# A three-component dash/slash/dot token with a 2-4 digit tail, or an ISO date.
# Used to recognise a DATE that OCR dropped into a column that is not a date column
# — most notably a lab reference range ("(0.25-08-2021)", V4 PRD §2 rule 2). Kept
# here, with the rest of the date-shape knowledge, so the lab adapter and the case
# curator agree on what a date looks like without either importing the other.
_DATE_IN_FIELD_RE = re.compile(
    r"\d{1,4}\s*[-–/.]\s*\d{1,2}\s*[-–/.]\s*\d{2,4}|\b\d{4}-\d{2}-\d{2}\b")


def looks_like_date_string(value: Any) -> bool:
    """True when ``value`` contains a date-shaped token.

    Deliberately shape-only and deliberately narrow: it answers "did a date land in
    a field that is not for dates?", which is a data-quality question, and it is
    never used to decide de-identification (that is ``deidentify()``'s hard guard)."""
    return bool(_DATE_IN_FIELD_RE.search(str(value or "")))


def provenance_lines(text: str) -> List[str]:
    """The partner de-identification header lines present in ``text``, verbatim.

    Verbatim because the caller needs to scan them for leaked dates; nothing
    returned here may reach a report unmasked."""
    return [m.group(0) for m in _PROVENANCE_LINE_RE.finditer(text or "")]


def strip_provenance_lines(text: str) -> str:
    """Remove partner de-identification headers from note text.

    Returns the text with those lines removed. Callers scanning for residual PHI
    must run this FIRST — a date inside a provenance header is a real finding
    about the partner's pipeline, but it is not clinical content and must not
    quarantine a chart on its own.
    """
    if not text:
        return text
    out, removed = _PROVENANCE_LINE_RE.subn("", text)
    if not removed:
        # Nothing matched: return the note completely untouched. The blank-line
        # tidy below must never run on a note we did not edit — it would rewrite
        # a clinician's own paragraph spacing for no reason at all.
        return text
    # The substitution leaves the header's own newline behind. Collapse only the
    # runs of blank lines it just created, so the note a physician reads is not
    # full of gaps where the headers were.
    return re.sub(r"\n{3,}", "\n\n", out)


def provenance_header_dates(text: str) -> List[str]:
    """MASKED date/date-like tokens found INSIDE this text's provenance headers.

    These are advisory: they say the partner's de-identification footer leaked a
    date, which they should be told about, and they never quarantine the chart."""
    out: List[str] = []
    for line in provenance_lines(text):
        for pat in (_ISO_RE, _SLASH_DATE_RE, _MONTHYEAR_NUM_RE):
            out.extend(_mask(m.group(0)) for m in pat.finditer(line))
        out.extend(_datelike_unresolved(line))
    return out


def _offset_token(d: date, index: date) -> str:
    return f"[day {(d - index).days:+d}]".replace("+0]", "0]")


def rewrite_note_dates(text: str, index: date,
                       date_order: str = DATE_ORDER_MDY) -> Tuple[str, int, List[str]]:
    """Rewrite every confidently-parsed date in free text to its relative form
    (``[day -5]``) against ``index``; collapse ages ≥90. Returns
    ``(rewritten_text, dates_rewritten, unresolved_masked_snippets)``.

    ``date_order`` is the record-level day-first/month-first reading inferred by
    ``infer_date_order`` — a per-record property, never a per-token guess.

    Unresolved = date-LIKE tokens we refused to guess at (ambiguous partials like
    "3/14" with no year in a note whose year context we can't trust). They are
    returned MASKED for the quarantine report — never a cleartext identifier.
    Clinical scores out of a fixed denominator (a GCS of 10/15) are exempt: they
    are not dates and no human could ever resolve them into one."""
    if not text:
        return text, 0, []
    n = {"count": 0}

    def _sub_iso(m: "re.Match[str]") -> str:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return m.group(0)
        n["count"] += 1
        return _offset_token(d, index)

    def _sub_mdy(m: "re.Match[str]") -> str:
        d = _mdy_to_date(m, date_order)
        if d is None:
            return m.group(0)
        n["count"] += 1
        return _offset_token(d, index)

    def _sub_month(m: "re.Match[str]") -> str:
        # Lowercase bare "may" without a year is almost always the MODAL VERB
        # ("patient may 5 days later…") — rewriting it corrupts the note (review
        # finding). Only treat "may" as a month when capitalized or dated.
        if m.group(1) == "may" and not m.group(3):
            return m.group(0)
        if m.group(3):
            d = _monthname_to_date(m, default_year=None)
        else:
            # Year-less month-name date: pick the candidate year (index±1) with
            # the SMALLEST |offset| — a Dec note against a Jan index anchors to
            # the prior year, not +357 days into the future (review finding).
            candidates = [
                _monthname_to_date(m, default_year=y)
                for y in (index.year - 1, index.year, index.year + 1)
            ]
            candidates = [c for c in candidates if c is not None]
            d = min(candidates, key=lambda c: abs((c - index).days)) if candidates else None
        if d is None:
            return m.group(0)
        n["count"] += 1
        return _offset_token(d, index)

    def _sub_monthyear_num(m: "re.Match[str]") -> str:
        # "12/2024" → "2024": Safe Harbor permits the year; the month leaves.
        n["count"] += 1
        return m.group(2)

    def _sub_monthyear_name(m: "re.Match[str]") -> str:
        n["count"] += 1
        return m.group(2)

    out = _ISO_RE.sub(_sub_iso, text)
    out = _SLASH_DATE_RE.sub(_sub_mdy, out)
    out = _MONTHNAME_RE.sub(_sub_month, out)
    # AFTER the full-date passes (a "March 14, 2031" already rewrote); what's
    # left as month+year partials generalizes to the year.
    out = _MONTHYEAR_NUM_RE.sub(_sub_monthyear_num, out)
    out = _MONTHYEAR_NAME_RE.sub(_sub_monthyear_name, out)

    # Ages ≥90 → the Safe-Harbor bucket.
    out = _AGE90_RE.sub(lambda m: "90+" + m.group(2), out)

    return out, n["count"], _datelike_unresolved(out)


#: Every free-text field the rewriter touches, as ``(collection_key, field_names)``.
#: ONE declaration, used by both the date-order inference and the rewrite walk, so
#: a field can never be inferred-from but not rewritten (or the reverse).
_FREE_TEXT_FIELDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("notes", ("text",)),
    ("studies", ("findings", "impression")),
    # Real partner exports put whole order lines into ``medications[].drug`` and
    # whole problem descriptions into ``problem_list[].condition`` — both carry
    # calendar dates ("Date column: 04/03/2022"), and neither was ever rewritten.
    # The case then cleared the timeline gate and died at ``deidentify()`` with
    # "residual identifiers detected (date)" — a hard guard the admin override
    # cannot bypass, so the case quarantined a SECOND time after the GCS fix.
    ("medications", ("drug", "dose", "route", "freq")),
    ("problem_list", ("condition",)),
)


def _free_text_fields(case: Dict[str, Any]) -> List[str]:
    """Every free-text string in a case that the rewriter will touch, plus flat
    string vitals. The inference pool for ``infer_date_order``."""
    out: List[str] = []
    for key, fields in _FREE_TEXT_FIELDS:
        for item in (case or {}).get(key) or []:
            if not isinstance(item, dict):
                continue
            for f in fields:
                v = item.get(f)
                if isinstance(v, str) and v:
                    out.append(v)
    for v in ((case or {}).get("vitals") or {}).values():
        if isinstance(v, str) and v:
            out.append(v)
    return out


#: Every STRUCTURED calendar field an adapter may attach to a chart item, per
#: collection. ONE declaration, read by the anchor pool, the day-first inference
#: and the offset assignment — so a collection can never anchor on a field it does
#: not convert, or convert one the anchor pool never saw.
_STRUCTURED_DATE_KEYS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("lab_panels", ("collected_at",)),
    ("notes", ("collected_at", "authored_on", "recorded_at")),
    ("studies", ("collected_at", "effective_at", "recorded_at")),
    ("problem_list", ("recorded_at", "recorded_date", "collected_at")),
    ("medications", ("authored_on", "ordered_at", "collected_at")),
)


def _structured_date_strings(fragments: Dict[str, Any]) -> List[str]:
    """Every raw structured date STRING in the fragments (before conversion), for
    the day-first inference. An ISO or HL7 value carries no slash and adds no
    evidence; a ``dd/mm/yyyy`` CSV column does, and used to be ignored."""
    out: List[str] = []
    for key, fields in _STRUCTURED_DATE_KEYS:
        for item in fragments.get(key) or []:
            if not isinstance(item, dict):
                continue
            for f in fields:
                v = item.get(f)
                if isinstance(v, str) and v:
                    out.append(v)
            off = item.get("collected_offset_days")
            if isinstance(off, str):
                out.append(off)
    return out


def _collect_structured_dates(
    fragments: Dict[str, Any], *, date_order: str = DATE_ORDER_MDY,
) -> List[Tuple[date, str]]:
    """Every parseable STRUCTURED timestamp in the assembled fragments, as
    ``(date, collection)`` — the pool the index event is chosen from.

    Widened from lab panels alone to every dated collection (Case Generation Fix
    PRD §A3): a fragment that carries notes and no panels — the notes-only slice a
    split bundle produces — used to get ``index = None``, and with no anchor
    every real date in every note was masked into the "unresolved date-like
    tokens" hold. The dates were never unparseable; there was nothing to measure
    them against. With note/study/problem/medication timing in the pool, such a
    fragment anchors on its latest dated item. Structured timing never comes from
    free text — this reads the adapter-supplied calendar fields only."""
    found: List[Tuple[date, str]] = []
    for key, fields in _STRUCTURED_DATE_KEYS:
        for item in fragments.get(key) or []:
            if not isinstance(item, dict):
                continue
            raw = next((item.get(f) for f in fields if item.get(f) is not None), None)
            d = parse_datetime(raw, date_order=date_order)
            if d:
                found.append((d, key))
            off = item.get("collected_offset_days")
            if isinstance(off, str):
                d2 = parse_datetime(off, date_order=date_order)
                if d2:
                    found.append((d2, key))
    return found


def _assign_offset(
    item: Dict[str, Any], index: Optional[date], report: Dict[str, Any], *,
    date_keys: Tuple[str, ...], date_order: str = DATE_ORDER_MDY,
) -> Dict[str, Any]:
    """Convert an item's raw calendar date (any of ``date_keys``) into a relative
    ``collected_offset_days`` and DELETE the raw date. Shared by notes, studies,
    problems, and medications so every chart item gets its recording time on the
    same axis as ``lab_panels`` — which is what lets the V5 environment enforce one
    temporal cutoff across the whole chart (Clinical RL Environments PRD §8.4.2).

    An already-relative integer offset is left untouched (synthetic-style input).
    An unparseable date is recorded as MASKED-unresolved and the offset is left
    ``None`` (unknown), which the environment treats as fail-closed on real cases.
    The resolved calendar date never survives this function."""
    raw = None
    for k in date_keys:
        v = item.pop(k, None)
        if raw is None and v is not None:
            raw = v
    if isinstance(item.get("collected_offset_days"), int):
        return item
    d = parse_datetime(raw, date_order=date_order)
    if d is not None and index is not None:
        item["collected_offset_days"] = (d - index).days
    elif raw is not None:
        report["unresolved"].append(_mask(str(raw)))
    return item


def _strip_provenance_from_case(case: Dict[str, Any], report: Dict[str, Any]) -> None:
    """Remove partner de-identification headers from every free-text field of
    ``case`` IN PLACE, recording the count and the masked dates found inside them
    on ``report``.

    Walks exactly ``_FREE_TEXT_FIELDS`` plus string vitals — the same surface the
    rewriter and the leftover scan walk — so a header can never be stripped from a
    field the scanner reads, or left in one it does."""
    found: List[str] = []
    stripped = 0

    def _clean(value: str) -> str:
        nonlocal stripped
        lines = provenance_lines(value)
        if not lines:
            return value
        stripped += len(lines)
        found.extend(provenance_header_dates(value))
        return strip_provenance_lines(value)

    for key, fields in _FREE_TEXT_FIELDS:
        items = case.get(key) or []
        if not items:
            continue
        new_items = []
        for item in items:
            if not isinstance(item, dict):
                new_items.append(item)
                continue
            item = dict(item)
            for field in fields:
                v = item.get(field)
                if isinstance(v, str) and v:
                    item[field] = _clean(v)
            new_items.append(item)
        case[key] = new_items

    vitals = case.get("vitals") or {}
    if vitals:
        case["vitals"] = {k: (_clean(v) if isinstance(v, str) else v)
                          for k, v in vitals.items()}

    if stripped:
        report["provenance_lines_stripped"] = stripped
    if found:
        # Deduplicated and bounded: this is an advisory the admin reads, not a
        # transcript. Order-stable so the same bundle reports the same way twice.
        seen: List[str] = []
        for tok in found:
            if tok not in seen:
                seen.append(tok)
        report["provenance_header_dates"] = seen[:20]


def normalize_timeline(
    fragments: Dict[str, Any], *, index_event: Optional[str] = None,
    vitals_at: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Convert an assembled case's shifted calendar timeline to relative integer
    day offsets (PRD §7). Returns ``(case_fragments, report)``.

    * Index event: ``index_event`` (the partner manifest's anchor, authoritative)
      else the LATEST structured collection date (deterministic, documented).
    * ``lab_panels[].collected_at`` (date string) → ``collected_offset_days``
      (int); the raw date field is deleted.
    * ``problem_list[].since`` full dates generalize to the year only.
    * every free-text field (note text, vitals string values) is date-rewritten.
    * report: counts + MASKED unresolved tokens + index provenance — NEVER the
      resolved index date itself (a re-identification key; it dies here).

    Raises ``TimelineError`` when an explicit ``index_event`` doesn't parse, or
    when dated panels exist but no anchor can be established."""
    case = dict(fragments or {})
    report: Dict[str, Any] = {
        "index_source": None, "panels_converted": 0,
        "note_dates_rewritten": 0, "unresolved": [],
        # Partner-quality advisory (V4 PRD §1.2), never a quarantine reason:
        # how many de-identification provenance headers we removed, and the MASKED
        # date tokens we found inside them. A non-empty list means the partner's
        # de-identification footer is leaking dates and they should be told.
        "provenance_lines_stripped": 0, "provenance_header_dates": [],
    }

    # The partner's own de-identification footer leaves BEFORE anything reads a
    # date (V4 PRD §1.2) — before the day-first inference, before the rewrite,
    # before the no-anchor leftover scan. It is metadata about the redaction, not
    # clinical text, and the one measured example carries an unshifted original
    # date that would otherwise either bias the order inference, launder into a
    # fake offset, or quarantine the chart. What was removed is reported.
    _strip_provenance_from_case(case, report)

    # Day-first vs month-first is decided ONCE, from every free-text field in the
    # case AND every raw structured date string, before a single token is
    # rewritten or converted. Deciding per token would let one chart carry both
    # readings; deciding from free text alone let a ``dd/mm/yyyy`` CSV column be
    # read month-first while the notes beside it were read day-first (§A4).
    date_order, order_evidence = infer_date_order(
        _free_text_fields(case) + _structured_date_strings(case))
    report["date_order"] = date_order
    report["date_order_evidence"] = order_evidence

    index: Optional[date] = None
    if index_event:
        index = parse_datetime(index_event, date_order=date_order)
        if index is None:
            raise TimelineError(f"manifest index_event {_mask(str(index_event))!r} is not a parseable date")
        report["index_source"] = "manifest"
    else:
        pool = _collect_structured_dates(case, date_order=date_order)
        if pool:
            index = max(d for d, _ in pool)
            # Provenance only — never the date. ``latest_observation`` is kept for
            # the lab-anchored case (its historical name); a chart that anchored on
            # a note or a study says so, because an admin reading the report needs
            # to know the axis came from narrative timing rather than a lab draw.
            at_index = {coll for d, coll in pool if d == index}
            report["index_source"] = ("latest_observation" if "lab_panels" in at_index
                                      else "latest_" + sorted(at_index)[0])
            counts: Dict[str, int] = {}
            for _d, coll in pool:
                counts[coll] = counts.get(coll, 0) + 1
            report["index_pool"] = counts

    # Structured panel timestamps → integer offsets.
    panels = case.get("lab_panels") or []
    new_panels: List[Dict[str, Any]] = []
    for lp in panels:
        lp = dict(lp)
        raw = lp.pop("collected_at", None)
        off = lp.get("collected_offset_days")
        if isinstance(off, int):
            pass  # already relative (synthetic-style input) — leave untouched
        else:
            d = (parse_datetime(raw, date_order=date_order)
                 or (parse_datetime(off, date_order=date_order) if isinstance(off, str) else None))
            lp.pop("collected_offset_days", None)
            if d is not None:
                if index is None:
                    raise TimelineError("dated lab panels present but no index anchor could be established")
                lp["collected_offset_days"] = (d - index).days
                report["panels_converted"] += 1
            elif raw is not None or off is not None:
                # A date WAS supplied but could not be parsed. Do NOT invent day 0 —
                # that fails OPEN (a post-decision panel would read as pre-decision
                # and leak the answer downstream). Leave the offset UNKNOWN (absent)
                # so the V5 temporal gate withholds it fail-closed on real cases.
                report["unresolved"].append(_mask(str(raw if raw is not None else off)))
            else:
                # No timing supplied at all — the synthetic/authored convention is
                # "everything is at the index", so day 0 (unchanged behavior).
                lp["collected_offset_days"] = 0
        new_panels.append(lp)
    case["lab_panels"] = new_panels

    # problem_list.since: a full date generalizes to the year (a bare year is
    # Safe-Harbor-fine and clinically useful: "since 2019"). Separately, the
    # chart-RECORDING time becomes a relative offset: a problem recorded AFTER the
    # decision point is the answer itself, so V5 must be able to hold it out.
    probs = []
    for p in case.get("problem_list") or []:
        p = dict(p)
        d = parse_datetime(p.get("since"), date_order=date_order)
        if d is not None:
            p["since"] = str(d.year)
        probs.append(_assign_offset(p, index, report,
                                    date_keys=_date_keys_for("problem_list"),
                                    date_order=date_order))
    if probs:
        case["problem_list"] = probs

    # medications: the ORDER time becomes a relative offset. A drug started after
    # the decision point IS the diagnosis (tolvaptan → SIADH, rasburicase → tumor
    # lysis), so it must be gateable by the environment.
    meds = []
    for m in case.get("medications") or []:
        meds.append(_assign_offset(dict(m), index, report,
                                   date_keys=_date_keys_for("medications"),
                                   date_order=date_order))
    if meds:
        case["medications"] = meds

    # Structured note/study timing → relative offset. This runs UNCONDITIONALLY,
    # outside the ``index is not None`` guard below: ``_assign_offset`` always DELETES
    # the raw calendar key, and ``ClinicalNote``/``Study`` are ``extra="forbid"`` — so
    # leaving a raw ``collected_at`` behind when no anchor could be established would
    # make ``ClinicalCase(**case)`` raise and break ingestion outright.
    notes = [
        _assign_offset(dict(n), index, report,
                       date_keys=_date_keys_for("notes"), date_order=date_order)
        for n in case.get("notes") or []
    ]
    if notes:
        case["notes"] = notes
    studies = [
        _assign_offset(dict(s), index, report,
                       date_keys=_date_keys_for("studies"), date_order=date_order)
        for s in case.get("studies") or []
    ]
    if studies:
        case["studies"] = studies

    # Free-text rewriting. Driven by ``_FREE_TEXT_FIELDS`` so notes, study
    # findings/impressions, medication order lines and problem descriptions are all
    # rewritten on the same terms — a post-decision path report and a med order
    # line are equally capable of carrying the outcome, and a calendar date in any
    # of them is equally fatal at ``deidentify()``.
    if index is not None:
        for key, fields in _FREE_TEXT_FIELDS:
            items = case.get(key) or []
            if not items:
                continue
            rewritten = []
            for item in items:
                item = dict(item)
                for field in fields:
                    v = item.get(field)
                    if isinstance(v, str) and v:
                        nv, c, unres = rewrite_note_dates(v, index, date_order)
                        item[field] = nv
                        report["note_dates_rewritten"] += c
                        report["unresolved"].extend(unres)
                rewritten.append(item)
            case[key] = rewritten
        vitals = case.get("vitals") or {}
        if vitals:
            vit = {}
            for k, v in vitals.items():
                if isinstance(v, str):
                    nv, c, unres = rewrite_note_dates(v, index, date_order)
                    report["note_dates_rewritten"] += c
                    report["unresolved"].extend(unres)
                    vit[k] = nv
                else:
                    vit[k] = v
            # Vitals are one flat dict, so they carry ONE timing marker for the set,
            # sourced from the latest vital-sign date the adapter saw. Passed in
            # explicitly (like ``index_event``) because the caller strips underscore-
            # prefixed fragment metadata before calling us. V5 gates the whole set on
            # it; the key is stripped before any agent read.
            marker = vitals_at if vitals_at is not None else case.pop("_vitals_at", None)
            if marker is not None and not isinstance(vit.get("collected_offset_days"), int):
                d = parse_datetime(marker, date_order=date_order)
                if d is not None:
                    vit["collected_offset_days"] = (d - index).days
                else:
                    report["unresolved"].append(_mask(str(marker)))
            case["vitals"] = vit
        else:
            case.pop("_vitals_at", None)
    else:
        case.pop("_vitals_at", None)
        # No anchor: only acceptable when nothing carries a date at all. If any
        # date-like token exists in the free text, we cannot rewrite → unresolved.
        leftovers: List[str] = []
        for text in _free_text_fields(case):
            leftovers.extend(datelike_leftovers_in_text(text))
        if leftovers:
            report["unresolved"].extend(leftovers)
            # Name the hold for what it is (§A3). "unresolved date-like tokens"
            # says the dates could not be read; here they were never tried,
            # because there was nothing to measure them against. An admin can act
            # on "no anchor — supply a manifest index_event"; they cannot act on
            # a list of masked tokens that were fine all along.
            report["hold_reason"] = (
                f"no index anchor: {len(leftovers)} date-like token(s) in the chart "
                "text, no dated structured item, and no manifest index_event, "
                "supply an index_event or a dated lab/note and the dates resolve")

    return case, report


def _date_keys_for(collection: str) -> Tuple[str, ...]:
    """The structured date fields ``normalize_timeline`` converts for ``collection``
    — the same declaration the anchor pool reads (``_STRUCTURED_DATE_KEYS``)."""
    for key, fields in _STRUCTURED_DATE_KEYS:
        if key == collection:
            return fields
    return ()


def datelike_leftovers_in_text(text: str) -> List[str]:
    """MASKED date/date-like tokens still present in a text — every shape the
    rewriter knows (full dates, month/year partials, ambiguous partials). Applies
    the SAME clinical-score exemption as the rewriter: if it did not count a GCS
    as unresolved, the scrub re-check must not count it as a leftover either, or a
    scrubbed case could never be released.

    For the same reason it applies the SAME provenance-header exemption: the
    rewriter strips the partner's de-identification footer before it looks at
    anything, so a scrub re-check that still counted a date inside that footer
    would hold a chart in quarantine over a line the pipeline already removed —
    and scrub, by design, cannot fix it. The finding is not lost: it is reported
    as ``provenance_header_dates`` (see ``provenance_header_dates``)."""
    clinical = strip_provenance_lines(text or "")
    out: List[str] = []
    for pat in (_ISO_RE, _SLASH_DATE_RE, _MONTHYEAR_NUM_RE):
        out.extend(_mask(m.group(0)) for m in pat.finditer(clinical))
    out.extend(_datelike_unresolved(clinical))
    return out


def datelike_leftovers(case: Dict[str, Any]) -> List[str]:
    """MASKED date-like tokens anywhere in a case's free text (notes + string
    vitals + problem 'since'). Used by the quarantine SCRUB re-check (review
    finding: a timeline-unresolved quarantine must not flip to ingested while
    the ambiguous tokens are still in the text — scrub can't fix what only a
    human or a better manifest anchor can resolve)."""
    out: List[str] = []
    # Every field the rewriter touches (notes, studies, medication order lines,
    # problem descriptions) plus string vitals — the scrub re-check and the
    # rewriter must look at exactly the same surface.
    for text in _free_text_fields(case or {}):
        out.extend(datelike_leftovers_in_text(text))
    for p in (case or {}).get("problem_list") or []:
        if p.get("since") and parse_datetime(p["since"]) is not None:
            out.append(_mask(str(p["since"])))
    return out
