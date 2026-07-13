"""Timeline normalization — calendar dates → relative integer day offsets
(Data Provider Portal PRD §2 B1, §7.4; build-order #1).

**The killer bug this module fixes.** Real "context-preserved" de-identified
clinical data is *date-shifted* — every patient's calendar is slid by a random,
patient-specific number of days so intervals survive but absolute dates do not
re-identify. But the data STILL CONTAINS DATE STRINGS (``2025-03-01``,
``3/1/2025``). Two downstream invariants make that fatal as-is:

  * ``validation._PHI_PATTERNS`` flags any ``YYYY-MM-DD`` / ``M/D/YYYY`` as a
    residual identifier, so ``case_formats.deidentify()`` **rejects 100% of every
    provider's data** the moment a date string survives.
  * ``LabPanel.collected_offset_days`` **must be an ``int``** — a date string
    there is a hard reject too.

So before the de-id guard can ever pass, every timestamp in an assembled case
fragment must become a **relative integer day offset** anchored to an index
event, and every date written inside note text must become a relative token
(``[day -5]``). Intervals — the clinically vital signal — survive exactly; no
calendar date ever enters the stored model.

**Never persist the shift.** The per-patient anchor date is the re-identification
key. This module uses it to compute offsets and then throws it away — it is never
returned in the normalized case and never written to the report. The report
carries only counts and a *label* for which anchoring rule fired.

Pipeline position (``asclepius/ingestion.py``): adapters → assemble fragments →
**``normalize_case_timeline`` (here)** → ``deid_verify`` → ``deidentify()``.
Post-condition, asserted by :func:`remaining_date_strings`: **zero date strings
anywhere in the case, notes included.**
"""

from __future__ import annotations

import copy
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from asclepius.cases import as_dict

# ─── Date tokens ──────────────────────────────────────────────────────────────
# The two calendar shapes ``validation._PHI_PATTERNS`` rejects, widened to also
# catch an ISO date-TIME (so ``2025-03-01T09:30:00`` is normalized, not left to
# trip the guard on the bare-date prefix). Order matters: try ISO before US.
_ISO = r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
_US = r"\d{1,2}/\d{1,2}/\d{2,4}"
_DATE_TOKEN_RE = re.compile(rf"({_ISO}|{_US})")

# strptime patterns tried in order. ISO date/datetime first, then US M/D/Y.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
)


def _parse_when(value: Any) -> Optional[date]:
    """Parse a date/datetime string (or a ``date``/``datetime``) into a ``date``.

    Accepts the ISO and US shapes real exports use; the time component of an ISO
    datetime is dropped (we anchor to whole days). Returns ``None`` for anything
    unparseable — the caller decides how to degrade, never crashes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # Normalize an ISO datetime down to its date prefix before strptime.
    iso = s.split("T", 1)[0].split(" ", 1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(iso if "-" in iso else s, fmt).date()
        except ValueError:
            continue
    # Last resort: pull the first date-looking token out of a longer string.
    m = _DATE_TOKEN_RE.search(s)
    if m:
        head = m.group(0).split("T", 1)[0].split(" ", 1)[0]
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(head, fmt).date()
            except ValueError:
                continue
    return None


def offset_days(when: Any, anchor: date) -> int:
    """Relative day offset of ``when`` from ``anchor`` (the index event = day 0).

    Earlier than the index event → negative (``-7`` = a week before). Returns 0
    if ``when`` cannot be parsed (the interval is unknown, not fabricated)."""
    d = _parse_when(when)
    if d is None:
        return 0
    return (d - anchor).days


def _fmt_day(offset: int) -> str:
    return f"[day {offset:+d}]" if offset != 0 else "[day 0]"


def rewrite_text_dates(text: Optional[str], anchor: date) -> Tuple[str, int]:
    """Rewrite every calendar date in free text to a relative ``[day N]`` token
    anchored to ``anchor``. Returns ``(new_text, n_rewritten)``. Unparseable
    tokens are still neutralized (replaced with ``[day ?]``) so no date string
    ever survives — a suspected date that we cannot anchor is dropped, not kept."""
    if not text:
        return (text or ""), 0
    n = 0

    def _sub(m: "re.Match[str]") -> str:
        nonlocal n
        n += 1
        d = _parse_when(m.group(0))
        if d is None:
            return "[day ?]"
        return _fmt_day((d - anchor).days)

    return _DATE_TOKEN_RE.sub(_sub, text), n


# ─── Whole-case walk ──────────────────────────────────────────────────────────
def _iter_strings(node: Any) -> List[str]:
    out: List[str] = []

    def _walk(n: Any) -> None:
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, dict):
            for v in n.values():
                _walk(v)
        elif isinstance(n, (list, tuple)):
            for v in n:
                _walk(v)

    _walk(node)
    return out


def _rewrite_node(node: Any, anchor: date) -> Tuple[Any, int]:
    """Return a copy of ``node`` with every date token in every string rewritten
    to a relative ``[day N]`` token, plus the total count rewritten."""
    if isinstance(node, str):
        return rewrite_text_dates(node, anchor)
    if isinstance(node, dict):
        total = 0
        out: Dict[Any, Any] = {}
        for k, v in node.items():
            nv, c = _rewrite_node(v, anchor)
            out[k] = nv
            total += c
        return out, total
    if isinstance(node, list):
        total = 0
        out_list: List[Any] = []
        for v in node:
            nv, c = _rewrite_node(v, anchor)
            out_list.append(nv)
            total += c
        return out_list, total
    return node, 0


def _candidate_dates(case: Dict[str, Any]) -> List[date]:
    """Every parseable calendar date in the case — lab ``collected_at`` fields
    plus any date token inside any string — used to pick the index event."""
    dates: List[date] = []
    for lp in case.get("lab_panels") or []:
        d = _parse_when(lp.get("collected_at"))
        if d is not None:
            dates.append(d)
    for s in _iter_strings(case):
        for tok in _DATE_TOKEN_RE.findall(s):
            d = _parse_when(tok)
            if d is not None:
                dates.append(d)
    return dates


def _coerce_offsets(case: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure every lab panel carries an integer ``collected_offset_days`` and no
    ``collected_at`` — used when there is no anchor (no dates to convert)."""
    for lp in case.get("lab_panels") or []:
        lp.pop("collected_at", None)
        try:
            lp["collected_offset_days"] = int(lp.get("collected_offset_days") or 0)
        except (TypeError, ValueError):
            lp["collected_offset_days"] = 0
    return case


def normalize_case_timeline(
    case: Any, *, index_event: Optional[Any] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Convert a case fragment's calendar timeline to relative day offsets.

    This is **build-order #1** in the ingestion pipeline — it runs *before* the
    de-id guard so that guard can pass. It:

      1. Picks the index event (day 0): the manifest's ``index_event`` when given
         and parseable, else the **latest** collection date found anywhere in the
         case (so earlier events are negative offsets).
      2. Converts every lab panel's ``collected_at`` → integer
         ``collected_offset_days`` and drops ``collected_at``.
      3. Rewrites every date token inside every remaining string (note text, any
         stray date-bearing field) to a relative ``[day N]`` token.

    Returns ``(normalized_case, report)``. The **anchor date is never included**
    in either — it is the re-identification key and is discarded here. ``report``
    carries counts and the anchoring rule that fired, safe to log and persist.

    Post-condition (verify with :func:`remaining_date_strings`): the returned
    case contains **zero calendar date strings**, notes included.
    """
    c = copy.deepcopy(as_dict(case) or {})

    anchor = _parse_when(index_event)
    anchor_source = "manifest_index_event" if anchor is not None else None
    if anchor is None:
        candidates = _candidate_dates(c)
        if candidates:
            anchor = max(candidates)
            anchor_source = "latest_collection"

    if anchor is None:
        # No dates anywhere — nothing to anchor; just make offsets well-typed.
        report = {
            "anchor_source": "none",
            "panels_converted": 0,
            "dates_rewritten": 0,
            "panels": len(c.get("lab_panels") or []),
        }
        return _coerce_offsets(c), report

    # 2. Lab panels: calendar → integer offset, drop the absolute date.
    panels_converted = 0
    for lp in c.get("lab_panels") or []:
        ca = lp.pop("collected_at", None)
        d = _parse_when(ca) if ca is not None else None
        if d is not None:
            lp["collected_offset_days"] = (d - anchor).days
            panels_converted += 1
        else:
            try:
                lp["collected_offset_days"] = int(lp.get("collected_offset_days") or 0)
            except (TypeError, ValueError):
                lp["collected_offset_days"] = 0

    # 3. Rewrite every remaining date token in every string (notes included).
    c, dates_rewritten = _rewrite_node(c, anchor)

    report = {
        "anchor_source": anchor_source,
        "panels_converted": panels_converted,
        "dates_rewritten": dates_rewritten,
        "panels": len(c.get("lab_panels") or []),
    }
    return c, report


def remaining_date_strings(case: Any) -> List[str]:
    """Every calendar date string still present anywhere in ``case`` — the
    post-condition check for :func:`normalize_case_timeline`. Empty == the case
    is date-free and will clear ``deidentify()``'s date guard."""
    found: List[str] = []
    for s in _iter_strings(as_dict(case) or {}):
        found.extend(_DATE_TOKEN_RE.findall(s))
    return found
