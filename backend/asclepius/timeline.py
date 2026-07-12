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
_MDY_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?!\d)")
_MONTHNAME_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
# Date-LIKE shapes we deliberately do NOT guess at (ambiguous / partial): if one
# survives the confident passes above, it is reported unresolved for quarantine.
_DATELIKE_RE = re.compile(r"(?<!\d)\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?(?!\d)")

# Ages ≥90 collapse to the Safe-Harbor bucket (HIPAA §164.514(b)(2)(i)(C)).
_AGE90_RE = re.compile(r"\b(9\d|1[0-1]\d)([\s-]*(?:years?[\s-]*old|y[/.]?o\b))", re.IGNORECASE)


def _parse_token(text: str) -> Optional[date]:
    """Parse one confidently-shaped date token to a date, else None."""
    m = _ISO_RE.fullmatch(text) or _ISO_RE.match(text)
    if m and m.group(0) == text:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_datetime(value: Any) -> Optional[date]:
    """Best-effort parse of a STRUCTURED field value (adapter-supplied
    ``collected_at``, FHIR ``effectiveDateTime``, HL7 ``OBR-7``…) to a date.
    Returns None when it isn't a parseable date."""
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
    # HL7 TS: YYYYMMDD[HHMM[SS]]
    m2 = re.match(r"^(\d{4})(\d{2})(\d{2})(?:\d{2,6})?$", s)
    if m2:
        try:
            return date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        except ValueError:
            return None
    m3 = _MDY_RE.match(s)
    if m3 and m3.start() == 0:
        return _mdy_to_date(m3)
    return None


def _mdy_to_date(m: "re.Match[str]") -> Optional[date]:
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000 if y <= 49 else 1900
    try:
        return date(y, mo, d)
    except ValueError:
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


def _offset_token(d: date, index: date) -> str:
    return f"[day {(d - index).days:+d}]".replace("+0]", "0]")


def rewrite_note_dates(text: str, index: date) -> Tuple[str, int, List[str]]:
    """Rewrite every confidently-parsed date in free text to its relative form
    (``[day -5]``) against ``index``; collapse ages ≥90. Returns
    ``(rewritten_text, dates_rewritten, unresolved_masked_snippets)``.

    Unresolved = date-LIKE tokens we refused to guess at (ambiguous partials like
    "3/14" with no year in a note whose year context we can't trust). They are
    returned MASKED for the quarantine report — never a cleartext identifier."""
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
        d = _mdy_to_date(m)
        if d is None:
            return m.group(0)
        n["count"] += 1
        return _offset_token(d, index)

    def _sub_month(m: "re.Match[str]") -> str:
        # A month-name date WITHOUT a year anchors to the index year — the
        # partner's shift keeps intra-case dates in one window, so this is the
        # only safe default; a wrong-year guess would shift ±365 and read as
        # incoherent to the clinician at the Stage-1 gate (flagged, not shipped).
        d = _monthname_to_date(m, default_year=index.year)
        if d is None:
            return m.group(0)
        n["count"] += 1
        return _offset_token(d, index)

    out = _ISO_RE.sub(_sub_iso, text)
    out = _MDY_RE.sub(_sub_mdy, out)
    out = _MONTHNAME_RE.sub(_sub_month, out)

    # Ages ≥90 → the Safe-Harbor bucket.
    out = _AGE90_RE.sub(lambda m: "90+" + m.group(2), out)

    unresolved = [_mask(m.group(0)) for m in _DATELIKE_RE.finditer(out)]
    return out, n["count"], unresolved


def _collect_structured_dates(fragments: Dict[str, Any]) -> List[date]:
    """Every parseable STRUCTURED timestamp in the assembled fragments — the pool
    the index event is chosen from (labs, vitals timestamps; not note text)."""
    found: List[date] = []
    for lp in fragments.get("lab_panels") or []:
        d = parse_datetime(lp.get("collected_at"))
        if d:
            found.append(d)
        off = lp.get("collected_offset_days")
        if isinstance(off, str):
            d2 = parse_datetime(off)
            if d2:
                found.append(d2)
    return found


def normalize_timeline(
    fragments: Dict[str, Any], *, index_event: Optional[str] = None
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
    }

    index: Optional[date] = None
    if index_event:
        index = parse_datetime(index_event)
        if index is None:
            raise TimelineError(f"manifest index_event {_mask(str(index_event))!r} is not a parseable date")
        report["index_source"] = "manifest"
    else:
        pool = _collect_structured_dates(case)
        if pool:
            index = max(pool)
            report["index_source"] = "latest_observation"

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
            d = parse_datetime(raw) or (parse_datetime(off) if isinstance(off, str) else None)
            lp.pop("collected_offset_days", None)
            if d is not None:
                if index is None:
                    raise TimelineError("dated lab panels present but no index anchor could be established")
                lp["collected_offset_days"] = (d - index).days
                report["panels_converted"] += 1
            else:
                if raw is not None or off is not None:
                    report["unresolved"].append(_mask(str(raw if raw is not None else off)))
                lp["collected_offset_days"] = 0
        new_panels.append(lp)
    case["lab_panels"] = new_panels

    # problem_list.since: a full date generalizes to the year (a bare year is
    # Safe-Harbor-fine and clinically useful: "since 2019").
    probs = []
    for p in case.get("problem_list") or []:
        p = dict(p)
        d = parse_datetime(p.get("since"))
        if d is not None:
            p["since"] = str(d.year)
        probs.append(p)
    if probs:
        case["problem_list"] = probs

    # Free-text rewriting (notes + any string vitals values).
    if index is not None:
        notes = []
        for n in case.get("notes") or []:
            n = dict(n)
            new_text, k, unres = rewrite_note_dates(n.get("text") or "", index)
            n["text"] = new_text
            report["note_dates_rewritten"] += k
            report["unresolved"].extend(unres)
            notes.append(n)
        if notes:
            case["notes"] = notes
        vitals = case.get("vitals") or {}
        if vitals:
            vit = {}
            for k, v in vitals.items():
                if isinstance(v, str):
                    nv, c, unres = rewrite_note_dates(v, index)
                    report["note_dates_rewritten"] += c
                    report["unresolved"].extend(unres)
                    vit[k] = nv
                else:
                    vit[k] = v
            case["vitals"] = vit
    else:
        # No anchor: only acceptable when nothing carries a date at all. If any
        # date-like token exists in the notes, we cannot rewrite → unresolved.
        for n in case.get("notes") or []:
            for m in _DATELIKE_RE.finditer(n.get("text") or ""):
                report["unresolved"].append(_mask(m.group(0)))
            for m in _ISO_RE.finditer(n.get("text") or ""):
                report["unresolved"].append(_mask(m.group(0)))

    return case, report
