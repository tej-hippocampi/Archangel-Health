"""Real de-identified chart → V4 case proposals (Real-Case Generation PRD §3).

The audit that produced this module found the gap in one sentence: **there is no
case CONSTRUCTION on the real path, only conversion.** ``_convert_and_gate``
serialized whatever was stored and asked an LLM for two candidate answers. Every
tag a V3 synthetic case carries — question, taxonomy bucket, subtopic, failure
mode, case type, measured difficulty — was simply absent, and ``difficulty`` was
the literal string ``"hard"``.

This module builds the missing half. A 14-month chart is not one case; it is a
series of clinical decision points, and each one becomes a case:

    segment → select_decision_point → curate → infer_specialty → classify_bucket
            → derive_question → score_difficulty → derive_ai_failure_mode

Four rules shape everything here:

* **The index event is the product.** The adapter's ``_index_event`` is the LAST
  day of the record, which makes every event past, every case a summary, and the
  answer readable off the chart. ``select_decision_point`` instead picks the
  moment a physician actually had to decide, and seals everything after it.
* **Difficulty is measured or it is marketing.** The structural axes here are a
  PRIOR. A case frontier models get right is not hard, however baroque the chart,
  so ``score_difficulty`` cannot return ``hard`` without a model failure rate.
* **An honest None beats a wrong tag.** A bucket is what a buyer filters on; a
  guessed one is worse than a blank one.
* **No calendar date ever survives.** Offsets are re-based to the decision point;
  the resolved index DATE is never returned, logged, or persisted (see
  ``timeline``'s re-identification note — that rule holds here too).

Everything in this module except ``derive_clinical_question`` and the difficulty
MEASUREMENT is deterministic and offline, so an admin dry-run costs nothing and
returns the same plan twice.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from asclepius.cases import (
    MultimodalContentError,
    as_dict,
    assert_multimodal_content,
    case_type_signature,
    public_case,
)
from asclepius.specialties import SPECIALTY_REGISTRY, is_enabled
# Imported as a module (not by name) so the provenance-header definitions below
# are unmistakably re-exports of the one canonical copy, not a second definition.
from asclepius import timeline as _timeline

log = logging.getLogger("asclepius.real_cases")

# Every collection on a ClinicalCase that carries a per-item relative timestamp.
_TIMED_COLLECTIONS = ("lab_panels", "notes", "studies", "medications", "problem_list")
#: HL7-style abnormal flags. One definition — the curation, the difficulty axis and
#: the question builder must agree on what "abnormal" means.
_ABNORMAL_FLAGS = ("L", "H", "LL", "HH")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def max_panels_per_case() -> int:
    """How many lab panels a generated case may carry.

    A physician drawing a case must see a chart, not a 149-row lab matrix. The
    real record's last encounter renders ~54k characters of prompt unbudgeted,
    which is not a case — it is a data dump that no one reads and that costs a
    frontier probe a fortune to answer."""
    return _env_int("ASCLEPIUS_REAL_CASE_MAX_PANELS", 12)


def max_notes_per_case() -> int:
    return _env_int("ASCLEPIUS_REAL_CASE_MAX_NOTES", 10)


class RealCaseError(ValueError):
    """A real chart could not be turned into case proposals (no timing, no
    content). Surfaced to the admin verbatim — never swallowed into an empty plan."""


# ═══════════════════════════════════════════════════════════════════════════════
# §3.1  Segmentation — encounters, not records
# ═══════════════════════════════════════════════════════════════════════════════
def _offset_of(item: Any) -> Optional[int]:
    if isinstance(item, dict) and isinstance(item.get("collected_offset_days"), int):
        return int(item["collected_offset_days"])
    return None


def _timed_offsets(case: Dict[str, Any], keys: Sequence[str] = _TIMED_COLLECTIONS) -> List[int]:
    """Every relative day on which something in ``keys`` was recorded."""
    out: List[int] = []
    for key in keys:
        for item in case.get(key) or []:
            off = _offset_of(item)
            if off is not None:
                out.append(off)
    if "vitals" in keys:
        vit = case.get("vitals") or {}
        if isinstance(vit.get("collected_offset_days"), int):
            out.append(int(vit["collected_offset_days"]))
    return out


# What an ENCOUNTER is made of. Deliberately NOT ``problem_list`` or
# ``medications``: a problem's first-noted date ("stroke, 2016") is chart state,
# not a visit, and clustering on it invents ten-year-old "encounters" that contain
# one problem and no labs. Activity means observation — a draw, a note, a study.
_ACTIVITY_COLLECTIONS = ("lab_panels", "notes", "studies", "vitals")


def prepare_longitudinal_chart(case: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Curate a copy; unsupported early note dates never seed chart state."""
    c = copy.deepcopy(as_dict(case) or {})
    if not c:
        return c
    c["notes"], _ = curate_notes(c.get("notes") or [])
    structured = _timed_offsets(c, ("lab_panels", "studies"))
    if structured:
        earliest = min(structured)
        suspect = {o for n in c.get("notes") or []
                   if (o := _offset_of(n)) is not None and earliest - o > 3 * 365
                   and not any(abs(o - d) <= 30 for d in structured)}
        # A page dated a year or more before any structured record, with no
        # structured record near it, whose drug orders are the drug orders of a
        # later dated page, is a mis-read handwritten year, not history.
        later_drugs = {_drug_identity(m.get("drug")) for m in c.get("medications") or []
                       if (o := _offset_of(m)) is not None and o >= earliest and m.get("drug")}
        for n in c.get("notes") or []:
            o = _offset_of(n)
            if o is not None and o >= earliest:
                for line in str(n.get("text") or "").splitlines():
                    parsed = parse_medication_line(line)
                    if parsed and parsed.get("drug"):
                        later_drugs.add(_drug_identity(parsed["drug"]))
        for n in c.get("notes") or []:
            o = _offset_of(n)
            if o is None or o in suspect or earliest - o <= 365 or any(abs(o - d) <= 30 for d in structured):
                continue
            drugs = {_drug_identity(p["drug"]) for line in str(n.get("text") or "").splitlines()
                     if (p := parse_medication_line(line)) and p.get("drug")}
            if len(drugs) >= 3 and len(drugs & later_drugs) / len(drugs) >= 0.75:
                suspect.add(o)
        for key in ("notes", "medications", "problem_list"):
            for item in c.get(key) or []:
                if _offset_of(item) in suspect:
                    item["collected_offset_days"] = None
                    if key == "notes":
                        item.update(model_visible=False, withheld_reason="implausible_date")
                    elif key == "problem_list":
                        item["since"] = None
    return c


# ── Discharge summaries carry BOTH halves of a decision point ────────────────
# The presenting record (complaints, history, examination) is what the physician
# had when the decision was made; the diagnosis, course and discharge orders are
# the outcome. The two are split by section header. Unknown headers fall to the
# resolution side: a section we cannot classify is withheld, never shown.
_DS_HEADER_RE = re.compile(r"^[ \t]*(?P<h>[A-Z][A-Za-z0-9/&' \-]{2,60}?)[ \t]*:[ \t]*(?P<rest>.*)$", re.M)
_DS_PRESENTATION_RE = re.compile(
    r"present|complain|chief|history|h/o|known case|past medical|drug history|"
    r"allerg|examination|on exam|vital|social|family|personal|review of system", re.I)
_DS_DROP_RE = re.compile(r"^(?:rmo|consultant|signature|name|initial|page)\b", re.I)
_DS_COURSE_RE = re.compile(r"course", re.I)
_DS_RESOLUTION_HEADER_RE = re.compile(
    r"discharg|diagnos|treatment|procedure|instruction|follow[ -]?up|status|resolution", re.I)
# A course narrative usually OPENS with the presentation ("Known case of HTN, DM
# and IHD. Presented with ...") before it turns into the outcome. The leading run
# of sentences that carry presentation language and no resolution language is
# visible; the first sentence that resolves anything ends the run.
_DS_PRESENT_SENTENCE_RE = re.compile(
    r"\b(?:known case|k/c|presented|presenting|complain|c/o|history|on examination|vitally|"
    r"on arrival|brought|referred with)\b", re.I)
_DS_RESOLVE_SENTENCE_RE = re.compile(
    r"\b(?:suggest\w*|diagnos\w*|consistent with|impression|managed|treat\w*|start\w*|"
    r"commenc\w*|initiat\w*|consult\w*|improv\w*|discharg\w*|shift\w*|admit\w*|icu|ward|"
    r"respond\w*|recover\w*|expired|transferr?ed|operat\w*|procedure|stent\w*|dialys\w*)\b", re.I)


def _leading_presentation(course_text: str) -> str:
    body = re.sub(r"^[^:]{2,60}:\s*", "", course_text.strip(), count=1)
    kept: List[str] = []
    for sentence in re.split(r"(?<=[.;])\s+", body):
        if not sentence.strip():
            continue
        if _DS_RESOLVE_SENTENCE_RE.search(sentence) or not _DS_PRESENT_SENTENCE_RE.search(sentence):
            break
        kept.append(sentence.strip())
    return " ".join(kept)


def split_discharge_summary(text: str) -> Tuple[Optional[str], str]:
    """``(presentation, resolution)`` — the visible half and the held-out half of a
    discharge summary. ``presentation`` is None when no presenting section parses
    (a continuation page of discharge orders, for example)."""
    raw = str(text or "")
    heads = list(_DS_HEADER_RE.finditer(raw))
    if not heads:
        return None, raw
    pres: List[str] = []
    reso: List[str] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
        block = raw[m.start():end].strip()
        head = m.group("h").strip()
        if _DS_DROP_RE.match(head):
            continue
        # Outcome qualifiers outrank an otherwise presenting heading: an
        # examination or vitals AT DISCHARGE are not admission observations.
        if _DS_RESOLUTION_HEADER_RE.search(head):
            reso.append(block)
            continue
        if _DS_COURSE_RE.search(head) and not _DS_PRESENTATION_RE.search(head):
            lead = _leading_presentation(block)
            if lead:
                pres.append("History (opening of the hospital course): " + lead)
            reso.append(block)
            continue
        (pres if _DS_PRESENTATION_RE.search(head) else reso).append(block)
    presentation = "\n".join(pres).strip()
    resolution = ("\n".join(reso).strip()) or raw
    return (presentation if len(presentation) >= 40 else None), resolution


def _activity_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """Panel renderings remain in the chart but cannot inflate the density gate."""
    offsets = set(_timed_offsets(case, ("lab_panels",)))
    return {**case, "notes": [n for n in case.get("notes") or []
            if not (_is_panel_note(n) and _offset_of(n) in offsets)]}


def _is_panel_note(note: Dict[str, Any]) -> bool:
    return str(note.get("note_type") or "").strip().lower() in {"report", "lab report"}


def segment_longitudinal_record(
    case: Optional[Dict[str, Any]], *, min_gap_days: int = 7,
) -> List[Dict[str, Any]]:
    """Split one chart into candidate encounters — one per clinical decision point.

    An encounter boundary is a gap in recorded activity greater than
    ``min_gap_days``. The real 14-month record clusters at offsets −408…−405,
    −373…−371, −280, −230, −33 and −3…0: six natural encounters, not one 32k-char
    prompt that asks a physician to summarise a completed story.

    Returns encounters oldest-first, each ``{index, start_offset, end_offset,
    offsets, n_events}``. A chart with no timing at all returns a single encounter
    spanning everything, which the caller can still gate on content.
    """
    c = _activity_case(prepare_longitudinal_chart(case))
    offsets = sorted(set(_timed_offsets(c, _ACTIVITY_COLLECTIONS)))
    if not offsets:
        return [{"index": 0, "start_offset": 0, "end_offset": 0,
                 "offsets": [], "n_events": 0, "undated": True}]

    gap = max(1, int(min_gap_days))
    clusters: List[List[int]] = [[offsets[0]]]
    for off in offsets[1:]:
        if off - clusters[-1][-1] > gap:
            clusters.append([off])
        else:
            clusters[-1].append(off)

    encounters: List[Dict[str, Any]] = []
    for i, cluster in enumerate(clusters):
        encounters.append({
            "index": i,
            "start_offset": cluster[0],
            "end_offset": cluster[-1],
            "offsets": list(cluster),
            "n_events": sum(1 for o in _timed_offsets(c, _ACTIVITY_COLLECTIONS)
                            if cluster[0] <= o <= cluster[-1]),
            "undated": False,
        })
    return encounters


# ═══════════════════════════════════════════════════════════════════════════════
# Longitudinal Cases PRD §2 — the density gate, and why it is the product
# ═══════════════════════════════════════════════════════════════════════════════
# Measured across patient-1…patient-4: 59 encounters, of which 25 clear this gate
# and 21 have a later qualifying encounter to be checked against.
#
# **DO NOT LOWER THESE TO RAISE THE COUNT.** 34 of the 59 fail, and they fail
# because they are single-date, few-event contacts — a repeat lab draw, a
# prescription refill. A repeat lab draw is not a decision, and a task built on
# one teaches a model that medicine is a series of trivia questions. Every point
# below the gate is a point a specialist is paid the standard per-submission rate
# to answer, and a buyer is asked to price as clinical judgment.
# The gate is the product.
ENCOUNTER_MIN_DISTINCT_DATES = 2
ENCOUNTER_MIN_EVENTS = 8
ENCOUNTER_MIN_RESOURCE_TYPES = 2


def _resource_types_in_window(case: Dict[str, Any], lo: int, hi: int) -> List[str]:
    """Which activity collections actually recorded something in ``[lo, hi]``.

    Scoped to ``_ACTIVITY_COLLECTIONS`` — the same definition of "activity" the
    segmentation clusters on — deliberately, and not widened to medications or the
    problem list. Those are chart STATE, not observations made in this window (see
    the note on ``_ACTIVITY_COLLECTIONS``), and counting a decade-old problem entry
    as a second "resource type" would let a single-lab-draw contact clear a gate
    built to exclude exactly that.
    """
    out: List[str] = []
    for key in _ACTIVITY_COLLECTIONS:
        if any(lo <= off <= hi for off in _timed_offsets(case, (key,))):
            out.append(key)
    return out


def qualify_encounter(
    case: Optional[Dict[str, Any]], encounter: Dict[str, Any],
) -> Dict[str, Any]:
    """Is this encounter a DECISION POINT? ``{qualifies, ...why}``.

    The §2 density gate: **≥ 2 distinct dates, ≥ 8 events, ≥ 2 resource types.**

    Returns the measurements alongside the verdict, always — never a bare bool.
    An admin looking at a chart that yielded 3 points out of 17 encounters needs to
    see *which* threshold each skipped encounter missed and by how much; a gate
    that reports only its verdict is a gate nobody can argue with, and this one
    should be arguable with evidence.

    A chart with no timing at all (``undated``) never qualifies. The whole
    construct is "truncate here, reveal what came after", and neither half of that
    means anything without an axis to truncate on.
    """
    c = _activity_case(prepare_longitudinal_chart(case))
    enc = encounter or {}
    offsets = list(enc.get("offsets") or [])
    reasons: List[str] = []

    if enc.get("undated") or not offsets:
        return {"qualifies": False, "n_distinct_dates": 0, "n_events": 0,
                "resource_types": [], "n_resource_types": 0,
                "reasons": ["encounter has no recorded timepoint"]}

    lo, hi = offsets[0], offsets[-1]
    n_dates = len(set(offsets))
    n_events = int(enc.get("n_events") or 0)
    types = _resource_types_in_window(c, lo, hi)

    if n_dates < ENCOUNTER_MIN_DISTINCT_DATES:
        reasons.append(f"{n_dates} distinct date(s); the gate is "
                       f"{ENCOUNTER_MIN_DISTINCT_DATES}")
    if n_events < ENCOUNTER_MIN_EVENTS:
        reasons.append(f"{n_events} recorded event(s); the gate is {ENCOUNTER_MIN_EVENTS}")
    if len(types) < ENCOUNTER_MIN_RESOURCE_TYPES:
        reasons.append(f"{len(types)} resource type(s) ({', '.join(types) or 'none'}); "
                       f"the gate is {ENCOUNTER_MIN_RESOURCE_TYPES}")

    return {
        "qualifies": not reasons,
        "n_distinct_dates": n_dates,
        "n_events": n_events,
        "resource_types": types,
        "n_resource_types": len(types),
        "reasons": reasons,
    }


def pair_decision_points(
    case: Optional[Dict[str, Any]], encounters: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Consecutive QUALIFYING encounters, as ``(k, k+1)`` pairs — the verifiable set.

    Returns ``[{decision, outcome, decision_index, outcome_index}, ...]`` oldest
    first, where each element is one of the input encounters.

    "*k+1*" means **the next encounter that also qualifies**, not the next encounter
    on the axis. A decision at *k* checked against a single stray lab draw is not
    verified against anything; it is verified against noise, and the resulting
    "outcome" would train a model on a coincidence. Non-qualifying encounters that
    fall between the two are not skipped — they are part of the sealed future and
    are revealed with the outcome — they simply cannot BE the outcome.

    The last qualifying encounter is never a decision point: there is no later
    qualifying encounter to check it against, which is the whole difference between
    the 25 encounters that pass the gate and the 21 that are verifiable.
    """
    c = as_dict(case) or {}
    qualifying = [e for e in (encounters or []) if qualify_encounter(c, e)["qualifies"]]
    return [
        {
            "decision": qualifying[i],
            "outcome": qualifying[i + 1],
            "decision_index": qualifying[i].get("index"),
            "outcome_index": qualifying[i + 1].get("index"),
        }
        for i in range(len(qualifying) - 1)
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# §3.2  The decision point — the single most important choice in the pipeline
# ═══════════════════════════════════════════════════════════════════════════════
# What ENDS the window: the arrival of a definitive result, a therapeutic
# commitment, or a disposition change. The index event is the last timepoint
# strictly BEFORE the first of these, because that is the moment the decisive datum
# has landed and the resolving action has not.
_RESOLVING_SIGNALS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("definitive_result", re.compile(
        r"\b(?:histopath\w*|biopsy\s+(?:report|result)|culture\s+(?:grew|positive|isolated)"
        r"|organism[\s-]*\d*\s*[:\-]|sensitivity\s+(?:pattern|report)|final\s+report"
        r"|confirm(?:ed|atory)\b|diagnos(?:is|ed)\s+(?:confirmed|established))", re.I)),
    ("therapeutic_commitment", re.compile(
        r"\b(?:started?\s+on|commenced|initiat(?:ed|ing)|shifted?\s+to|switch(?:ed)?\s+to"
        r"|thromboly\w+|intubat\w+|dialys\w+|transfus\w+|surgery|operative|laparotom\w+"
        r"|stent\w*|angioplast\w+|craniotom\w+)\b", re.I)),
    ("disposition_change", re.compile(
        r"\b(?:discharge\w*|shifted?\s+to\s+(?:icu|ward|hdu)|admit(?:ted)?\s+(?:in|to)\s+icu"
        r"|transferr?ed|expired|death|dnr|lama|referred\s+out|escalat\w+)\b", re.I)),
)


def _encounter_items(case: Dict[str, Any], key: str, lo: int, hi: int) -> List[Dict[str, Any]]:
    return [it for it in (case.get(key) or [])
            if (off := _offset_of(it)) is not None and lo <= off <= hi]


def select_decision_point(
    case: Optional[Dict[str, Any]], encounter: Dict[str, Any],
) -> Tuple[Optional[int], Dict[str, Any]]:
    """``(index_offset, rationale)`` for one encounter.

    Everything at or before ``index_offset`` is VISIBLE; everything after is HELD
    OUT and becomes the outcome the physician's answer is checked against.

    The rule: the index event is the latest timepoint at which the decisive datum
    has arrived but the resolving action has NOT — the last recorded day strictly
    before the first of a definitive diagnostic result, a therapeutic commitment,
    or a disposition change. Choosing the last day of the encounter instead (which
    is what the adapter's ``_index_event`` does for the whole record) makes the
    case a summary; choosing too early makes it unanswerable.

    Returns ``(None, rationale)`` when no timepoint leaves anything held out — a
    single-day encounter with no resolving event has no future to seal, and a case
    with no held-out outcome cannot be graded.
    """
    c = as_dict(case) or {}
    offsets = list(encounter.get("offsets") or [])
    if not offsets:
        return None, {"reason": "encounter has no recorded timepoint",
                      "resolving_offset": None}

    lo, hi = offsets[0], offsets[-1]

    if len(offsets) == 1:
        # A one-day encounter — a single outpatient draw, a single ED visit — is
        # still a decision point: the question is what you do THAT day, and the
        # outcome is the chart's subsequent course. It only fails when nothing at
        # all was recorded afterwards, because then there is no future to seal.
        if not any(o > hi for o in _timed_offsets(c)):
            return None, {"reason": "single-timepoint encounter with nothing recorded "
                                    "afterwards — no outcome to hold out",
                          "resolving_offset": None}
        return hi, {"reason": "single-timepoint encounter; the chart's subsequent "
                              "course is the held-out outcome",
                    "resolving_offset": None, "signal": "next_encounter"}
    resolving: Optional[int] = None
    kind: Optional[str] = None
    for item in _encounter_items(c, "notes", lo, hi) + _encounter_items(c, "studies", lo, hi):
        text = " ".join(str(item.get(f) or "") for f in ("text", "findings", "impression"))
        if not text.strip():
            continue
        off = _offset_of(item)
        for name, pat in _RESOLVING_SIGNALS:
            if pat.search(text) and (resolving is None or off < resolving):
                resolving, kind = off, name
                break
    # A drug ordered inside the encounter is itself a therapeutic commitment.
    for med in _encounter_items(c, "medications", lo, hi):
        off = _offset_of(med)
        if off is not None and off > lo and (resolving is None or off < resolving):
            resolving, kind = off, "therapeutic_commitment"

    if resolving is not None:
        candidates = [o for o in offsets if o < resolving]
        if candidates:
            return candidates[-1], {"reason": f"last timepoint before the first {kind}",
                                    "resolving_offset": resolving, "signal": kind}

    # No resolving event we can name. Fall back to the second-to-last recorded day,
    # which still seals a real future rather than asking for a summary of the whole
    # encounter. This is a WEAKER choice and is labelled as such so the admin can see
    # which proposals rest on it.
    return offsets[-2], {"reason": "no resolving event detected; sealed the final "
                                   "recorded day as the outcome",
                         "resolving_offset": offsets[-1], "signal": None,
                         "weak": True}


# ═══════════════════════════════════════════════════════════════════════════════
# §3.5  Curation — what a physician should actually be shown
# ═══════════════════════════════════════════════════════════════════════════════
# Report furniture an OCR'd lab export emits as if it were a result. These are
# labels on the page, not analytes: 92 of 149 distinct "analytes" in the real
# record were rows of this kind.
_ANALYTE_FURNITURE = frozenset({
    "result keys", "normal ranges", "normal range", "reference", "reference range",
    "ref", "refs", "location", "status stamp", "specimen", "remarks", "remark",
    "comment", "comments", "method", "note", "notes", "printed by", "verified by",
    "report status", "sample type", "collected", "received", "reported",
})
# A "result" whose NAME is an age or demographic band is a reference-range row that
# lost its column alignment ("- Adult" with value 3.5 and "unit" -5.2).
_ANALYTE_BAND_RE = re.compile(
    r"^[\s\-–—]*(?:adult|children|child|infant|newborn|neonate|male|female|adults?"
    r"|>\s*\d+\s*(?:years?|yrs?|y)|<\s*\d+\s*(?:years?|yrs?|y)"
    r"|\d+\s*[-–]\s*\d+\s*(?:years?|yrs?|y|days?|months?)"
    r"|\d+\s*(?:years?|yrs?|y|days?|months?)\s*[-–]\s*\d+\s*(?:years?|yrs?|y|days?|months?))"
    r"[\s:]*$", re.I)
# A unit cell that is really half a reference range ("-5.2", "Reference: 15-45").
_BROKEN_UNIT_RE = re.compile(r"^\s*(?:[-–]\s*\d|reference\b|ref\b|normal\b|\d+\s*[-–]\s*\d+\s*$)", re.I)

# ─── Polluted unit cells (V4 PRD §2) ─────────────────────────────────────────
# The partner's OCR concatenates unit + reference range + interpretation into the
# unit column: ``mmol/L (19 - 24) — low``. ``_BROKEN_UNIT_RE`` above anchors on a
# unit that STARTS with a range or a dash-digit, so it caught 0 of the 42 real
# occurrences — every one of them starts with a perfectly good unit.
#
# Split rather than reject. The unit is real and recoverable and the NUMBER IS
# FINE; only the tail is noise. Dropping the row would discard a genuine result
# over a formatting defect, which is the failure mode this whole repair exists to
# avoid.
_UNIT_TAIL_RE = re.compile(
    r"\s*[\(\[].*$|\s*[—–-]\s*(?:low|high|below|above|elevated|normal|critical).*$", re.I)
# The range we can recover from inside the tail — a parenthesised pair only. A
# lone number or prose is left alone; a guessed reference range makes a normal
# value read as abnormal.
_RECOVERED_RANGE_RE = re.compile(
    r"[\(\[]\s*(-?\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(-?\d+(?:\.\d+)?)\s*[\)\]]")
# A reference range that is really a DATE ("(0.25-08-2021)"). Three dash-separated
# components with a 4-digit tail is a date, not a range, and the correct move is to
# null the range — never to parse it into anything. The shape test lives in
# ``timeline`` with the rest of the date-shape knowledge, so the lab adapter (which
# sees the partner's raw column) and this curator cannot disagree about it.
_RANGE_DATELIKE_RE = _timeline._DATE_IN_FIELD_RE


def split_polluted_unit(unit: Any) -> Tuple[str, Optional[str]]:
    """``'mmol/L (19 - 24) — low'`` → ``('mmol/L', '19 - 24')``. Never drops the value.

    Returns ``(clean_unit, recovered_range_or_None)``. The recovered range is
    returned for REPORTING only — see ``derive_flag``, which may never use it.
    A unit with no pollution comes back unchanged with ``None``.
    """
    raw = str(unit or "").strip()
    if not raw:
        return "", None
    clean = _UNIT_TAIL_RE.sub("", raw).strip(" \t—–-")
    if clean == raw:
        return raw, None
    m = _RECOVERED_RANGE_RE.search(raw)
    recovered = f"{m.group(1)} - {m.group(2)}" if m and not _RANGE_DATELIKE_RE.search(m.group(0)) else None
    # If the tail ate the ENTIRE cell there was never a unit here — the column held
    # a bare range or an interpretation ("(19 - 24)"). Say so with an empty string
    # rather than handing the garbage back: ``keep_lab_result`` requires a truthy
    # unit, so an honest "" drops the row, while returning the original would let
    # a reference range ride into the case pretending to be a unit.
    return clean, recovered


def reference_range_is_datelike(result: Dict[str, Any]) -> bool:
    """True when this row's reference range is a DATE that OCR dropped into the
    range column (``ref='(0.25-08-2021)'``, measured 16 times).

    A date in a reference range means the range is UNUSABLE. Null it, keep the
    value, flag the row — and do not attempt to parse ``0.25-08-2021`` into
    anything, because every reading of it is wrong."""
    # Already recognised upstream: the CSV adapter is the only place the partner's
    # RAW range column is still in scope, so when it marks a row we take its word
    # rather than re-deriving from the parsed pair it deliberately left empty.
    if (result or {}).get("ref_range_unusable"):
        return True
    for key in ("reference_range", "ref_range", "_ref_range_raw"):
        if _RANGE_DATELIKE_RE.search(str((result or {}).get(key) or "")):
            return True
    lo, hi = (result or {}).get("ref_low"), (result or {}).get("ref_high")
    # The adapter refuses to parse a datelike cell into numbers (it returns
    # ``(None, None)``), so a surviving pair cannot be one. This is the belt for a
    # hand-built or differently-adapted row that carries the raw text in-place.
    return bool(_RANGE_DATELIKE_RE.search(f"{lo if lo is not None else ''}")
                or _RANGE_DATELIKE_RE.search(f"{hi if hi is not None else ''}"))


# ─── Physiologic plausibility (V4 PRD §2.1) ──────────────────────────────────
# Measured: patient-4 on one day carries THREE conflicting bicarbonates — 15.6,
# 10.0 and 1.7 mmol/L. A serum bicarbonate of 1.7 is not survivable and is
# contradicted by the same day's ABG (pH 7.392, pCO2 26.3, HCO3 15.6, base excess
# -8.0). It is an OCR artifact, and it is the kind of number a physician builds a
# whole wrong answer around.
#
# READ THIS BEFORE ADDING A ROW. The asymmetry here is brutal and one-directional:
# shipping one artifact costs a case; deleting one real value silently removes the
# decisive datum from a chart and nobody ever finds out. So this table is scoped by
# three rules, each of which cost a real false positive when it was missing:
#
#   1. Bounds are "incompatible with any MEASUREMENT", not "incompatible with
#      life" and certainly not "alarming". A potassium of 9.4 in an ESRD patient is
#      a dialysis emergency that gets measured, reported and acted on — it is
#      exactly the decisive datum of a hard nephrology case, and the PRD's proposed
#      ceiling of 9.0 deleted it. Likewise a bicarbonate of 4 is real in extreme
#      DKA, so the floor sits below that and still catches the 1.7.
#   2. NO BARE ABBREVIATIONS. "K", "Na" and "pH" carry no specimen, and a urine
#      panel writes all three: urine K is ~45, urine Na is ~20 (the pre-renal vs
#      ATN datum this product is partly built on), urine pH is 4.5-8.0. Each was
#      dropped as "impossible" until these aliases were removed. Only names that
#      can only mean the serum/blood analyte are matched.
#   3. A NON-SERUM SPECIMEN disables the bound entirely, whether it is named on the
#      analyte or on the panel.
#
# And the honest caveat: an absolute bound is a blunt instrument, and it is not
# actually what proves the 1.7 wrong. What proves it wrong is the same day's ABG —
# a pH of 7.392 with a bicarbonate of 1.7 is arithmetically impossible. This table
# catches the artifact; it does not reason about the chart.
_IMPLAUSIBLE_BOUNDS: Dict[str, Tuple[float, float]] = {
    "bicarbonate": (3.0, 60.0),
    "ph_blood": (6.5, 8.0),
    "potassium": (1.0, 10.0),
    "sodium": (90.0, 200.0),
}
# Analyte-name → bounds key. Real exports write the same analyte a dozen ways, but
# every key here is UNAMBIGUOUS about its specimen — see rule 2 above.
_PLAUSIBILITY_ALIASES: Dict[str, str] = {
    "bicarbonate": "bicarbonate", "serum bicarbonate": "bicarbonate",
    "bicarb": "bicarbonate", "hco3": "bicarbonate", "hco3-": "bicarbonate",
    "serum hco3": "bicarbonate",
    # Blood pH only. A bare "pH" is a urinalysis row as often as a gas, so it is
    # deliberately absent and gets no bound at all.
    "blood ph": "ph_blood", "arterial ph": "ph_blood", "venous ph": "ph_blood",
    "ph arterial": "ph_blood", "ph venous": "ph_blood", "abg ph": "ph_blood",
    "serum potassium": "potassium", "potassium": "potassium",
    "plasma potassium": "potassium", "serum sodium": "sodium",
    "sodium": "sodium", "plasma sodium": "sodium",
}
# A specimen that is not blood. Any of these on the analyte OR the panel name and
# no bound applies: the reference physiology is completely different.
_NON_SERUM_SPECIMEN_RE = re.compile(
    r"\b(?:urine|urinary|urinalysis|csf|cerebrospinal|gastric|stool|fa?ecal|sweat"
    r"|dialysate|dialysis\s+fluid|ascit\w+|pleural|peritoneal|drain|sputum|saliva"
    r"|24\s*(?:-|\s)?\s*(?:h|hr|hour)\w*)\b", re.I)


def implausible_value(result: Dict[str, Any], *, panel_name: Any = None) -> bool:
    """True when this result is physiologically impossible for its analyte.

    Only fires for the handful of analytes in ``_IMPLAUSIBLE_BOUNDS``, and only
    when nothing about the row says the specimen is not blood. Everything else is
    kept. A hit means DROP THE RESULT and record it — never quarantine the chart,
    which would throw away a good record over one OCR artifact.

    ``panel_name`` is the disambiguator that the analyte name alone cannot always
    provide ("Sodium" inside a "Urine studies" panel). Pass it wherever it is in
    scope; omitting it only makes this function more conservative, never less.
    """
    if not isinstance(result, dict):
        return False
    name = _clean_analyte(result.get("analyte")).strip().lower()
    if _NON_SERUM_SPECIMEN_RE.search(name) or _NON_SERUM_SPECIMEN_RE.search(str(panel_name or "")):
        return False
    key = _PLAUSIBILITY_ALIASES.get(name)
    if key is None:
        # "(calc)"-style qualifiers and trailing charge signs, normalized.
        stripped = re.sub(r"\s*\(.*?\)\s*|[-+\s]+$", "", name).strip()
        key = _PLAUSIBILITY_ALIASES.get(stripped)
    if key is None or not _is_numeric(result.get("value")):
        return False
    lo, hi = _IMPLAUSIBLE_BOUNDS[key]
    return not (lo <= float(str(result["value"]).strip()) <= hi)


# Qualitative results that ARE clinical evidence even with no unit: culture
# sensitivities (S/I/R) and microscopy descriptors.
_QUALITATIVE_VALUE_RE = re.compile(
    r"^\s*(?:S|I|R|sensitive|intermediate|resistant|positive|negative|nil|absent|present"
    r"|trace|reactive|non[\s-]*reactive|\+{1,4}|yellow|colou?rless|clear|turbid|hazy"
    r"|pale\s+yellow|amber)\s*$", re.I)


def _clean_analyte(name: Any) -> str:
    """Analyte name with the OCR bullet/dash prefix stripped ("- Potassium")."""
    return re.sub(r"^[\s\-–—•*]+", "", str(name or "")).strip()


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    try:
        return math.isfinite(float(str(value).strip()))
    except (TypeError, ValueError):
        return False


def keep_lab_result(result: Dict[str, Any]) -> bool:
    """Is this row a real laboratory result, or report furniture?

    PRD §3.5: allowlist by LOINC where present; otherwise require a numeric value
    and a real unit. Extended in one place the PRD's rule would have over-pruned:
    a culture sensitivity (``AMIKACIN = S``) and a urinalysis descriptor
    (``Casts = GRANULAR CAST (02) /HPF``) carry no unit and are unmistakably
    clinical, so an explicit qualitative shape is kept too. Everything else — the
    page's own legend, location stamp, and reference-band rows — is dropped.
    """
    if not isinstance(result, dict):
        return False
    name = _clean_analyte(result.get("analyte"))
    if not name:
        return False
    if name.strip().lower() in _ANALYTE_FURNITURE:
        return False
    if _ANALYTE_BAND_RE.match(name):
        return False
    # Physiologic plausibility is deliberately NOT checked here. This function
    # answers "is this a lab result or is it page furniture?"; whether the NUMBER
    # is possible is a different question, and it needs the panel name to know the
    # specimen — which is not in scope here. Conflating the two dropped every urine
    # sodium and potassium in the chart, because bare "Sodium: 20" reads as an
    # impossible serum value only until you notice the panel says "Urine studies".
    # ``curate_lab_panels`` runs ``implausible_value`` with the panel, once.
    if result.get("loinc"):
        return True                              # the lab coded it; it is a result
    # A polluted unit ("mmol/L (19 - 24) — low") is a formatting defect, not a bad
    # value (V4 PRD §2, rule 1). Judge the SALVAGED unit — the raw cell is what
    # made ``_BROKEN_UNIT_RE`` see nothing wrong with 42 broken rows and then, on
    # the ones it did catch, throw the number away.
    unit = split_polluted_unit(result.get("unit"))[0]
    if _is_numeric(result.get("value")) and unit and not _BROKEN_UNIT_RE.match(unit):
        return True
    if _QUALITATIVE_VALUE_RE.match(str(result.get("value") or "")):
        return True
    return False


def _canonical_value(value: Any) -> str:
    """``122`` and ``"122.0"`` are the same result arriving from two formats."""
    try:
        f = float(str(value).strip())
        return repr(round(f, 6))
    except (TypeError, ValueError):
        return str(value or "").strip().lower()


def derive_flag(result: Dict[str, Any]) -> str:
    """``L``/``H`` from the value against the lab's OWN printed reference range.

    Most real exports flag only a fraction of results — 69 of 430 observations in
    the real bundle — while printing a reference range on nearly all of them. A
    value outside its range is abnormal whether or not the analyser wrote a letter
    next to it, and treating it as normal zeroed the abnormal-analyte difficulty
    axis on charts that were full of abnormal values. Derived only when the lab
    gave no flag and both the value and the bound are numeric; never overrides a
    flag the lab did emit.

    V4 PRD §2, rule 3 — the bound must be one the PARTNER SUPPLIED in the reference
    range column. A flag computed from a range reconstructed out of a corrupted
    unit string is a clinical claim built on OCR repair, and it will be wrong
    silently: the recovered range never reaches ``ref_low``/``ref_high``, so this
    function cannot see it, and ``ref_range_unusable`` rows are refused outright.
    """
    existing = str(result.get("flag") or "").strip().upper()
    if existing:
        return existing
    # The range was nulled because it was a date (V4 PRD §2, rule 2). There is
    # nothing to compare against and inventing one is exactly the silent clinical
    # claim this guard exists to prevent.
    if result.get("ref_range_unusable"):
        return ""
    if not _is_numeric(result.get("value")):
        return ""
    value = float(str(result["value"]).strip())
    lo, hi = result.get("ref_low"), result.get("ref_high")
    if _is_numeric(lo) and value < float(str(lo)):
        return "L"
    if _is_numeric(hi) and value > float(str(hi)):
        return "H"
    return ""


def _repair_result(result: Dict[str, Any], stats: Dict[str, int]) -> Dict[str, Any]:
    """Apply the V4 PRD §2 row repairs and return a new result dict.

    Two repairs, and the rule that governs both is *never drop the number*:

      1. A unit polluted with the range and the interpretation is SPLIT — the unit
         is salvaged, the range is recovered for the report, the value is kept. The
         recovered range is deliberately NOT written to ``ref_low``/``ref_high``
         (rule 3): a flag derived from a range reconstructed out of a corrupted
         string is a clinical claim built on OCR repair and it will be wrong
         silently. It is counted and dropped.
      2. A reference range that is really a date is NULLED and the row marked
         ``ref_range_unusable`` (rule 2). No attempt is made to read
         ``0.25-08-2021`` as anything, because every reading of it is wrong.
    """
    clean_unit, recovered = split_polluted_unit(result.get("unit"))
    if recovered is not None or (result.get("unit") and clean_unit != str(result["unit"]).strip()):
        stats["units_split"] = stats.get("units_split", 0) + 1
        result = {**result, "unit": clean_unit}
        if recovered is not None:
            stats["ranges_recovered"] = stats.get("ranges_recovered", 0) + 1
    if reference_range_is_datelike(result):
        stats["ref_range_datelike"] = stats.get("ref_range_datelike", 0) + 1
        result = {**result, "ref_low": None, "ref_high": None, "ref_range_unusable": True}
    return result


def _result_identity(panel_offset: Optional[int], result: Dict[str, Any]) -> Tuple:
    return (panel_offset, _clean_analyte(result.get("analyte")).lower(),
            _canonical_value(result.get("value")))


def curate_lab_panels(panels: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Drop report furniture, de-duplicate results across formats, and merge the
    panels that survive. Returns ``(panels, stats)``.

    De-duplication is load-bearing, not cosmetic: a partner who sends the same
    labs as a FHIR bundle AND an HL7 export AND a CSV — which is the normal case —
    produces every result two or three times. Unified into one case (§2.3) that is
    a lab table with each value repeated, which a physician reads as three
    separate draws.
    """
    stats = {"dropped_furniture": 0, "dropped_duplicate": 0, "panels_in": len(panels or []),
             "results_in": 0, "enriched_from_duplicate": 0,
             # V4 PRD §2 repairs, counted separately from furniture so an operator
             # can tell "the partner's OCR is broken" from "this row was a page
             # legend". Silent repair is how 42 polluted units shipped.
             "units_split": 0, "ranges_recovered": 0, "ref_range_datelike": 0,
             "implausible_value": 0}
    seen: Dict[Tuple, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        off = _offset_of(panel)
        kept: List[Dict[str, Any]] = []
        for result in panel.get("results") or []:
            stats["results_in"] += 1
            # Counted as its own outcome, not as furniture: a bicarbonate of 1.7 is
            # a real row the partner really sent, and "we deleted an impossible
            # value" is a different report to an operator than "we dropped a page
            # legend" (V4 PRD §2.1).
            if implausible_value(result, panel_name=panel.get("panel")):
                stats["implausible_value"] += 1
                continue
            if not keep_lab_result(result):
                stats["dropped_furniture"] += 1
                continue
            result = _repair_result(result, stats)
            ident = _result_identity(off, result)
            first = seen.get(ident)
            if first is not None:
                stats["dropped_duplicate"] += 1
                # The formats carry different amounts of detail for the SAME result:
                # the CSV has the LOINC and no abnormal flag, the HL7 export has the
                # flag and the reference range. Keeping whichever arrived first
                # threw away the flag on every duplicated analyte, which zeroed the
                # abnormal-ratio difficulty axis on charts that were full of
                # abnormal values. Merge the missing fields instead of discarding.
                for field in ("flag", "unit", "loinc", "ref_low", "ref_high"):
                    if not first.get(field) and result.get(field):
                        first[field] = result[field]
                        stats["enriched_from_duplicate"] += 1
                # A duplicate that carries a USABLE range repairs a row whose own
                # range we nulled as datelike — the other format got the column
                # right. Clear the marker so the row is flagged only while it is
                # actually rangeless, and ``derive_flag`` may use the good range.
                if first.get("ref_range_unusable") and not result.get("ref_range_unusable") and (
                        first.get("ref_low") is not None or first.get("ref_high") is not None):
                    first["ref_range_unusable"] = False
                continue
            merged = {**result, "analyte": _clean_analyte(result.get("analyte"))}
            seen[ident] = merged
            kept.append(merged)
        if kept:
            out.append({**panel, "results": kept})
    # Flags are derived AFTER the merge, so a result that picked up its reference
    # range from a duplicate is flagged against that range too.
    for panel in out:
        for result in panel["results"]:
            flag = derive_flag(result)
            if flag and not result.get("flag"):
                result["flag"] = flag
                stats["flags_derived"] = stats.get("flags_derived", 0) + 1
    stats["panels_out"] = len(out)
    stats["results_out"] = sum(len(p["results"]) for p in out)
    return out, stats


# Bundle documentation and export scaffolding that arrives as a .txt entry and is
# not clinical narrative. Matched on the note's OPENING, not anywhere in the body,
# so a progress note that merely mentions de-identification is untouched.
_NON_CLINICAL_NOTE_RE = re.compile(
    r"^\s*(?:#\s*\w[\w -]*—\s*de-?identified\s+ehr\s+export"
    r"|de-?identified\s+clinical\s+summary\b"
    r"|##?\s*contents\b|##?\s*anonymi[sz]ation\b"
    r"|this\s+(?:archive|bundle|export)\s+contains\b)", re.I)

# Per-note provenance headers written by the partner's de-identification tool
# (V4 PRD §1.2). These are METADATA ABOUT the de-identification, not clinical
# text — and at least one of them carries an unshifted original date ("year as
# printed (11/7/21)"), which the timeline scanner reads as a leaked date.
#
# Distinct from ``_NON_CLINICAL_NOTE_RE`` above in the way that matters: that one
# is anchored against a note's OPENING and drops the whole note; this one is
# LINE-anchored, because the header sits inside an otherwise ordinary clinical
# note and only the header should leave.
#
# The definition lives in :mod:`asclepius.timeline` — the module that owns the
# date scan and imports nothing from this package — so the scanner and this
# curator cannot drift on what counts as a header. Re-exported here because this
# is where a reader looking for note curation will come first.
_PROVENANCE_LINE_RE = _timeline._PROVENANCE_LINE_RE
strip_provenance_lines = _timeline.strip_provenance_lines
provenance_lines = _timeline.provenance_lines
provenance_header_dates = _timeline.provenance_header_dates


def _normalized_note_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def curate_notes(notes: Sequence[Dict[str, Any]], *, min_chars: int = 40,
                 ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """De-duplicate narratives and drop the bundle's own documentation.

    A partner export routinely ships each note twice — once as a FHIR
    ``DocumentReference`` and once as a plain-text file — plus a README describing
    the export. Left in, the case prompt is two copies of the chart wrapped around
    a file manifest.

    The partner's per-note de-identification header (V4 PRD §1.2) is removed here
    too. Ingest already strips it — this is the belt for that braces, because a
    case can reach curation from a path that never ran ``normalize_timeline`` (a
    hand-built fixture, an authored case, a re-curated stored case), and a
    physician should never be shown a redaction footer as if a clinician wrote it.
    Removal happens BEFORE the duplicate check, so two copies of the same note that
    differ only in their header still collapse to one.
    """
    stats = {"notes_in": len(notes or []), "dropped_duplicate": 0,
             "dropped_non_clinical": 0, "dropped_short": 0,
             "provenance_lines_stripped": 0}
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for note in notes or []:
        if not isinstance(note, dict):
            continue
        raw = str(note.get("text") or "").strip()
        text = strip_provenance_lines(raw).strip()
        text = re.sub(r"^Document index:.*?^-{20,}[^\S\n]*\n?", "", text,
                      flags=re.MULTILINE | re.DOTALL | re.IGNORECASE).strip()
        if text != raw:
            stats["provenance_lines_stripped"] += len(provenance_lines(raw))
            note = {**note, "text": text}
        if len(text) < min_chars:
            stats["dropped_short"] += 1
            continue
        if _NON_CLINICAL_NOTE_RE.match(text):
            stats["dropped_non_clinical"] += 1
            continue
        # Repeated templates on different visits remain distinct observations.
        norm = (_offset_of(note), _normalized_note_text(text)[:300])
        if norm in seen:
            stats["dropped_duplicate"] += 1
            continue
        seen.add(norm)
        out.append(note)
    stats["notes_out"] = len(out)
    return out, stats


def _budget(items: List[Dict[str, Any]], limit: int, stats: Dict[str, int],
            key: str, *, note_roles: bool = False) -> List[Dict[str, Any]]:
    """Keep the ``limit`` items closest to the decision point, plus the earliest
    one in the window so the TREND survives the cut. Dropping the oldest panel
    would delete exactly the comparison the case is testing."""
    if len(items) <= limit:
        return items
    ordered = sorted(items, key=lambda it: (_offset_of(it) is None, _offset_of(it) or 0))
    # ``limit == 1`` has no room for a trend anchor, and ``ordered[-0:]`` is the
    # WHOLE list — a slice that silently disables the budget rather than applying it.
    if note_roles:
        def role(it):
            typ = str(it.get("note_type") or "").lower()
            classes = (r"\b(?:er|triage|emergency)\b", r"h&p|history|admission", r"progress",
                       r"consult", r"discharge", r"radiology|interpretation|report",
                       r"order|prescription", r"nurs")
            return next((i for i, pat in enumerate(classes) if re.search(pat, typ)), 8)
        ranked = sorted(ordered, key=lambda it: (role(it), -(_offset_of(it) or 0)))
        kept = ranked[:1] if limit <= 1 else [ordered[0]] + [it for it in ranked if it is not ordered[0]][:limit-1]
    else:
        kept = [ordered[-1]] if limit <= 1 else ordered[-(limit - 1):] + [ordered[0]]
    stats[key] = len(items) - len(kept)
    keep_ids = {id(it) for it in kept}
    return [it for it in items if id(it) in keep_ids]


# ── Medications ──────────────────────────────────────────────────────────────
# A real export puts the WHOLE order sheet into ``medications[].drug``, one line
# per OCR'd row: headings ("Orders:", "Treatment:"), form footers, nursing prose,
# and — mixed in among them — the actual drugs. 159 "medications" on the real
# record reduce to roughly two dozen once the sheet furniture is removed.
_MED_FORM_RE = re.compile(
    r"^\s*(?:inj(?:ection)?|tab(?:let)?|cap(?:sule)?|syp|syr(?:up)?|susp|neb(?:s|uli\w*)?"
    r"|iv|i/v|im|i/m|sc|s/c|po|p/o|ng|t\.|c\.|drip|infusion|sol(?:ution)?|oint\w*|supp\w*)"
    r"[\s.:]+(?P<rest>\S.*)$", re.I)
_MED_DOSE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?)\s*"
    r"(mg|mcg|g|gm|gms|units?|u|ml|cc|iu|mmol|meq|%)\b", re.I)
_MED_ROUTE_RE = re.compile(r"\b(i\s*/?\s*v|i\s*/?\s*m|s\s*/?\s*c|p\s*/?\s*o|ng|sl|pr|neb|top(?:ical)?)\b", re.I)
_MED_FREQ_RE = re.compile(
    r"\b(od|bd|bid|tds|tid|qds|qid|hs|sos|stat|prn|q\d+\s*h|\d+\s*hourly|\d+\s*[-\s]*hrly"
    # "x 6 H°" is how these order sheets write six-hourly.
    r"|\d+\s*h[°ºo]|once\s+daily|twice\s+daily|continuous|sliding\s+scale"
    # ``(?!\w)`` rather than ``\b``: the degree sign in "6 H°" is already a
    # non-word character, so a trailing ``\b`` can never match after it.
    r"|\d\s*\+\s*\d\s*\+\s*\d)(?!\w)", re.I)
# Order-sheet punctuation that is not part of a drug name: the "x" multiplication
# sign, the Latin "c̄" (cum, "with"), and a trailing bare count ("Tab Vita-6 1").
_MED_NAME_LEAD_RE = re.compile(r"^(?:c[̄̅]?|c/|with)\s+", re.I)
_MED_NAME_TAIL_RE = re.compile(r"(?:\s+x|\s*\+|\s+\d+(?:\s*/\s*\d+)?)\s*$", re.I)
# Lines that are page furniture regardless of what follows.
_MED_FURNITURE_RE = re.compile(
    r"^\s*(?:orders?|treatment|fresh\s+order\w*|form|form\s+note|form\s+footer|time"
    r"|header|footer|readings?|drugs?\s*&|date\s+column|role\s+notation|notation"
    r"|rn\s+signature|nurses?\s+notes?|intake\s*/\s*output|medication\s+administration"
    r"|iv\s+cannulation|clinical\s+notes?\s*/|status|left\s+margin|via\s+ng|subcutaneous)\b"
    r"|^\s*\d+\s*[.)]\s|:\s*$", re.I)
_MED_MAX_WORDS = 12


def parse_medication_line(line: str) -> Optional[Dict[str, str]]:
    """One order-sheet line → ``{drug, dose, route, freq}``, or None when the line
    is not a medication order.

    Deliberately conservative and deliberately deterministic: a wrong drug on a
    physician-facing chart is worse than a missing one, and a dry-run plan an
    admin reads twice must say the same thing twice.
    """
    raw = re.sub(r"\s+", " ", str(line or "")).strip(" -–—•*\t")
    if not raw or _MED_FURNITURE_RE.match(raw):
        return None
    form = _MED_FORM_RE.match(raw)
    if not form:
        return None
    rest = form.group("rest").strip()
    if len(rest.split()) > _MED_MAX_WORDS:
        return None                                    # a sentence, not an order
    dose = _MED_DOSE_RE.search(rest)
    freq = _MED_FREQ_RE.search(rest)
    route = _MED_ROUTE_RE.search(rest)
    # The drug name is what precedes the first dose/route/frequency token.
    cut = min([m.start() for m in (dose, freq, route) if m] or [len(rest)])
    drug = rest[:cut].strip(" ,;:-–—")
    drug = _MED_NAME_LEAD_RE.sub("", drug)
    for _ in range(3):                       # "Vita-6 1 x" → "Vita-6 1" → "Vita-6"
        stripped = _MED_NAME_TAIL_RE.sub("", drug).strip(" ,;:-–—")
        if stripped == drug:
            break
        drug = stripped
    if not drug or not re.search(r"[A-Za-z]{3}", drug):
        return None
    if len(drug.split()) > 4:
        return None
    out = {"drug": drug}
    if dose:
        out["dose"] = dose.group(0).strip()
    if route:
        out["route"] = route.group(0).strip()
    if freq:
        out["freq"] = freq.group(0).strip()
    return out


def curate_medications(
    medications: Sequence[Dict[str, Any]], notes: Sequence[Dict[str, Any]] = (),
    *, index_offset: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Structured medications + medications recoverable from the encounter's own
    notes → one de-duplicated drug list. Returns ``(medications, stats)``.

    Notes are mined only for what the structured list does not already carry, and
    ONLY from notes at or before ``index_offset`` — a drug named in a note written
    after the decision point IS the diagnosis and must stay sealed.
    """
    stats = {"meds_in": len(medications or []), "dropped_furniture": 0,
             "dropped_duplicate": 0, "recovered_from_notes": 0}
    seen: set = set()
    out: List[Dict[str, Any]] = []

    def _add(parsed: Dict[str, str], offset: Optional[int]) -> bool:
        key = re.sub(r"[^a-z0-9]", "", parsed["drug"].lower())
        if not key or key in seen:
            stats["dropped_duplicate"] += 1
            return False
        seen.add(key)
        item = dict(parsed)
        if offset is not None:
            item["collected_offset_days"] = offset
        out.append(item)
        return True

    for med in medications or []:
        if not isinstance(med, dict):
            continue
        parsed = parse_medication_line(med.get("drug"))
        if parsed is None:
            stats["dropped_furniture"] += 1
            continue
        # A structured export that already split dose/route/freq keeps its own
        # values; only the drug NAME is taken from the line parse.
        for field in ("dose", "route", "freq"):
            if med.get(field):
                parsed[field] = str(med[field])
        _add(parsed, _offset_of(med))

    for note in notes or []:
        if not isinstance(note, dict):
            continue
        off = _offset_of(note)
        if index_offset is not None and off is not None and off > index_offset:
            continue
        for line in str(note.get("text") or "").splitlines():
            parsed = parse_medication_line(line)
            if parsed is not None and _add(parsed, off):
                stats["recovered_from_notes"] += 1

    stats["meds_out"] = len(out)
    return out, stats


# ═══════════════════════════════════════════════════════════════════════════════
# Building one encounter case (visible window + sealed outcome)
# ═══════════════════════════════════════════════════════════════════════════════
def _rebase(item: Dict[str, Any], index_offset: int) -> Dict[str, Any]:
    """Re-anchor an item so day 0 is the DECISION POINT, not the end of the record."""
    off = _offset_of(item)
    if off is None:
        return dict(item)
    return {**item, "collected_offset_days": off - index_offset}


def _drug_identity(name: Any) -> str:
    """The identity of a drug ACROSS the several ways one chart writes it.

    An OCR'd order sheet writes the same drug as "Tab Extor 5/160 mg x OD" on one
    page and "Tab Extor 5/160 (1+0+0)" on the next, so the parsed names differ
    ("Extor" vs "Extor 5/160") even though the drug does not. Matching on the first
    alphabetic token collapses those, which is what the continued-vs-newly-started
    comparison needs: a re-order is not a treatment decision, and calling one "new"
    puts a drug the chart already shows into the answer key.

    Deliberately LENIENT, and deliberately not used for de-duplicating the visible
    medication list — there it would collapse two different insulins into one."""
    for tok in re.split(r"[^A-Za-z0-9]+", str(name or "")):
        if re.search(r"[A-Za-z]{3}", tok):
            return tok.lower()
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _discharge_medication_orders(text: str) -> List[Dict[str, str]]:
    """Read line orders and explicit inline orders in a discharge narrative.

    Hospital-course lists often use commas rather than newlines. These orders
    already appear in the visible history, so repeating one later cannot make it
    a newly revealed treatment. Keep the conservative medication-line parser and
    require an order at the start or an affirmative medication-list lead-in.
    """
    orders = []
    lead_in = re.compile(
        r"(?:^|;)\s*(?:inpatient\s+)?(?:meds|medications|treatment)"
        r"\s+(?:included|includes?|given|administered)\s*:?\s+", re.I)
    for line in str(text or "").splitlines():
        if not parse_medication_line(re.split(r"[,;]", line)[0]):
            # Do not mine a list introduced by 'avoid' or 'allergic to' for orders.
            if not (match := lead_in.search(line)):
                continue
            line = line[match.end():]
        for fragment in re.split(r"[,;]", line):
            parsed = parse_medication_line(fragment)
            if not parsed or not parsed.get("drug"):
                break
            orders.append(parsed)
    return orders


def _held_out_summary(case: Dict[str, Any], index_offset: int,
                      visible_text: str = "",
                      visible_drugs: Optional[set] = None,
                      until_offset: Optional[int] = None, *,
                      include_discharge_orders: bool = True) -> Dict[str, Any]:
    """What actually happened after the decision point — the outcome the
    physician's answer is checked against. Assembled deterministically from the
    chart itself, never invented.

    ``visible_text`` separates what was ESTABLISHED after the decision point from
    what was merely re-recorded. A chronic problem the chart already names before
    the index event ("ascites", carried on the problem list for a year) is not the
    answer to "what do you do now" — it is context the physician can already read.
    Counting it as the answer both mis-grades the model and trips the leakage guard
    on a term the visible chart legitimately contains."""
    after = lambda key: [it for it in (case.get(key) or [])                    # noqa: E731
                         if (o := _offset_of(it)) is not None and o > index_offset
                         and (until_offset is None or o <= until_offset)]
    problems = [str(p.get("condition") or "") for p in after("problem_list")]
    # The answer key is graded against, so it gets the same curation as the visible
    # side: a key listing "Left margin: Aspiration Pneumonia; Admit in ICU" as a
    # DRUG grades a model down for not predicting an OCR artifact.
    drugs: List[Tuple[str, str]] = []          # (normalized name, display string)
    for m in after("medications"):
        parsed = parse_medication_line(m.get("drug"))
        if parsed:
            drugs.append((
                _drug_identity(parsed["drug"]),
                " ".join(x for x in (parsed.get("drug"), parsed.get("dose"),
                                     parsed.get("route"), parsed.get("freq")) if x),
            ))
    lines: List[str] = []
    panel_offsets = set(_timed_offsets(case, ("lab_panels",)))
    outcome_notes, _ = curate_notes([n for n in after("notes")
        if not (_is_panel_note(n) and _offset_of(n) in panel_offsets)])
    # The outcome key needs the resolution narrative before a stack of reports.
    outcome_notes.sort(key=lambda n: (
        not bool(re.search("discharge", str(n.get("note_type") or ""), re.I)),
        _offset_of(n)))
    for note in outcome_notes:
        text = str(note.get("text") or "").strip()
        if text:
            lines.append(f"[+{_offset_of(note) - index_offset}d {note.get('note_type') or 'Note'}] "
                         + text[:600])
        # Discharge orders are the treatment decision the answer key is graded on,
        # and in many exports they exist only as text. Parse them like an order
        # sheet so "dapagliflozin started" reaches the key, not just the reveal.
        if include_discharge_orders and re.search("discharge", str(note.get("note_type") or ""), re.I):
            for parsed in _discharge_medication_orders(text):
                drugs.append((
                    _drug_identity(parsed["drug"]),
                    " ".join(x for x in (parsed.get("drug"), parsed.get("dose"),
                                         parsed.get("route"), parsed.get("freq")) if x),
                ))
    abnormal: List[str] = []
    for panel in after("lab_panels"):
        for r in panel.get("results") or []:
            if str(r.get("flag") or "").upper() in _ABNORMAL_FLAGS:
                abnormal.append(f"{r.get('analyte')} {r.get('value')} {r.get('unit') or ''}"
                                f" ({r.get('flag')}) at +{_offset_of(panel) - index_offset}d")
    seen_before = (visible_text or "").lower()
    recorded_after = [p for p in problems if p]
    newly_established = [
        p for p in recorded_after
        if not all(t in seen_before for t in _distinctive(p)[:3] or ["\0"])
    ]
    # A drug the patient was ALREADY on and that the next order sheet simply
    # re-orders is not a treatment decision — and treating it as one both mis-grades
    # the model and trips the leakage guard against a med list that legitimately
    # names it. Only a NEW drug class is the answer ("tolvaptan → SIADH").
    # Matched on the WHOLE cleaned drug name, not its first word: "c̄ Clenil" and
    # "Clenil" must resolve to the same drug or the same order reads as new.
    on_board = visible_drugs or set()
    if include_discharge_orders:
        seen_keys: set = set()
        drugs = [(k, d) for k, d in drugs if d and not (k in seen_keys or seen_keys.add(k))]
    started = [display for _key, display in drugs if display]
    newly_started = [display for key, display in drugs if display and key not in on_board]
    return {
        "problems_recorded_after": recorded_after,
        "newly_established_problems": newly_established,
        "drugs_started_after": started,
        "newly_started_drugs": newly_started,
        "abnormal_results_after": abnormal[:40],
        "narrative_after": lines[:12],
    }


def _ground_truth_from_held_out(held_out: Dict[str, Any]) -> Dict[str, Any]:
    """The internal answer key: the chart's OWN subsequent course.

    This is what makes the case gradeable without a physician having answered it
    yet — the empirical-difficulty probe grades a frontier model against what the
    treating team actually found and did. ``public_case`` strips it, so it never
    reaches a blinded evaluator or an export."""
    established = held_out.get("newly_established_problems") or []
    bits: List[str] = []
    if established:
        bits.append("Newly established after this point: " + "; ".join(established[:8]))
    started = held_out.get("newly_started_drugs") or held_out.get("drugs_started_after") or []
    if started:
        bits.append("Treatment started after this point: " + "; ".join(started[:12]))
    if held_out.get("abnormal_results_after"):
        bits.append("Subsequent abnormal results: "
                    + "; ".join(held_out["abnormal_results_after"][:10]))
    # ``key_data`` holds MULTI-WORD phrases only. A single-token leaf ("Ascites")
    # is not an answer to "what do you do now", and it is exactly the shape the
    # leakage guard treats as a distinctive answer span — so a chronic problem
    # name would fail the guard against a chart that legitimately contains it.
    key_data = [p for p in established if len(_distinctive(p)) >= 2][:8]
    return {
        "answer": " | ".join(bits) or "No documented change after this point.",
        "rationale": "\n".join(held_out.get("narrative_after") or [])[:4000] or None,
        "key_data": key_data,
    }


def build_encounter_case(
    case: Optional[Dict[str, Any]], encounter: Dict[str, Any], index_offset: int,
    *, until_offset: Optional[int] = None, trajectory: bool = False,
    encounters: Optional[Sequence[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """``(visible_case, held_out, curation_stats)`` for one decision point.

    VISIBLE is everything recorded at or before the index event, re-based so day 0
    is the decision point. HELD OUT is everything after it. The split is temporal
    and total: no collection is exempt, because a problem added afterwards is
    literally the answer and a drug started afterwards names the diagnosis.
    """
    c = prepare_longitudinal_chart(case)
    # Resolution documents belong to the end of their own encounter. Prior
    # resolutions remain available as history and as the next point's reveal.
    spans = encounters if encounters is not None else [encounter]
    presentation_notes: List[Dict[str, Any]] = []
    for note in c.get("notes") or []:
        off = _offset_of(note)
        if off is not None and re.search("discharge", str(note.get("note_type") or ""), re.I):
            span = next((e for e in spans if e["start_offset"] <= off <= e["end_offset"]), None)
            if span:
                # The presenting record inside the summary is contemporaneous with
                # admission and is the only presenting narrative some exports carry.
                # It becomes its own visible note at the encounter start; the summary
                # keeps only its resolution half, dated to the encounter end.
                presentation, resolution = (split_discharge_summary(note.get("text"))
                                            if trajectory else (None, note.get("text")))
                if presentation:
                    presentation_notes.append({
                        **{k: v for k, v in note.items() if k not in ("model_visible", "withheld_reason")},
                        "note_type": "Admission",
                        "text": "Presenting record (from the encounter's discharge summary):\n" + presentation,
                        "collected_offset_days": span["start_offset"],
                        "source_section": "discharge_summary_presentation",
                    })
                    note["text"] = resolution
                note["collected_offset_days"] = span["end_offset"]
                if index_offset < span["end_offset"]:
                    note.update(model_visible=False, withheld_reason="encounter_resolution")
    if presentation_notes:
        c["notes"] = list(c.get("notes") or []) + presentation_notes
    lookback = encounter.get("start_offset")
    if not isinstance(lookback, int):
        lookback = index_offset

    untimed: Dict[str, int] = {}

    def _visible(key: str, *, from_start: bool = False) -> List[Dict[str, Any]]:
        lo = lookback if from_start else None
        out = []
        for it in c.get(key) or []:
            off = _offset_of(it)
            if off is None:
                # Unknown timing FAILS CLOSED. An item we cannot place on the axis
                # could equally be a pre-decision observation or the post-decision
                # answer, and showing it would be a coin flip on whether the case
                # leaks. Counted so the omission is visible to the admin, never
                # silent.
                untimed[key] = untimed.get(key, 0) + 1
                continue
            if off > index_offset:
                continue
            if lo is not None and off < lo:
                if key != "notes" or not re.search("discharge", str(it.get("note_type") or ""), re.I):
                    continue
            out.append(it)
        return out

    # Labs and narratives come from the ENCOUNTER window (a trend the physician was
    # actually looking at). Problems and medications are chart-level state: every
    # one recorded at or before the index event, however long ago — that is what a
    # chart shows you when you open it.
    panels, lab_stats = curate_lab_panels(_visible("lab_panels", from_start=True))
    note_candidates = _visible("notes", from_start=True)
    panel_offsets = set(_timed_offsets(c, ("lab_panels",)))
    notes, note_stats = curate_notes([n for n in note_candidates
        if not (_is_panel_note(n) and _offset_of(n) in panel_offsets)])
    note_stats["dropped_panel_rendering"] = len(note_candidates) - len([
        n for n in note_candidates if not (_is_panel_note(n) and _offset_of(n) in panel_offsets)])
    panels = _budget(panels, max_panels_per_case(), lab_stats, "dropped_over_budget")
    notes = _budget(notes, _env_int("ASCLEPIUS_REAL_CASE_MAX_NOTES", 16) if trajectory
                    else max_notes_per_case(), note_stats, "dropped_over_budget", note_roles=True)
    meds, med_stats = curate_medications(_visible("medications"), notes,
                                         index_offset=index_offset)
    problems = _visible("problem_list")
    studies = _visible("studies", from_start=True)

    # The held-out summary is built AGAINST the visible window, so "established
    # after the decision point" means established, not merely re-recorded.
    seen_before = "\n".join(
        [str(n.get("text") or "") for n in notes]
        + [str(p.get("condition") or "") for p in problems]
        + [str(s.get(f) or "") for s in studies for f in ("findings", "impression")])
    # Drugs the physician can already read in a visible discharge summary are on
    # board; re-ordering them later is continuity, not a new treatment decision.
    on_board = {_drug_identity(m.get("drug")) for m in meds if m.get("drug")}
    for n in notes if trajectory else []:
        if re.search("discharge", str(n.get("note_type") or ""), re.I):
            for parsed in _discharge_medication_orders(n.get("text")):
                on_board.add(_drug_identity(parsed["drug"]))
    held_out = _held_out_summary(
        c, index_offset, seen_before, on_board,
        until_offset=until_offset, include_discharge_orders=trajectory)
    visible = {
        "case_source": "real_deid",
        "specialty": c.get("specialty") or "general",
        "demographics": dict(c.get("demographics") or {}),
        "problem_list": [_rebase(p, index_offset) for p in problems],
        "medications": [_rebase(m, index_offset) for m in meds],
        "lab_panels": [_rebase(p, index_offset) for p in panels],
        "notes": [_rebase(n, index_offset) for n in notes],
        "studies": [_rebase(s, index_offset) for s in studies],
        "vitals": _visible_vitals(c, index_offset),
        "source_refs": list(c.get("source_refs") or []),
        "case_provenance": c.get("case_provenance"),
        "study_findings_policy": c.get("study_findings_policy") or "visible",
        "ground_truth": _ground_truth_from_held_out(held_out),
    }
    if trajectory:
        # Keep static cases and their content floors unchanged. Only a walk can
        # explicitly render an absent prior record as the state of its first point.
        visible.update(
            medications_absent_on_record=(not meds and not _visible("medications")),
            problems_absent_on_record=(not problems),
        )
    # §4.2.1 — the declaration is computed FROM THIS WINDOW, never inherited from
    # the parent chart. See ``ingestion.modalities_present_in`` for why inheriting
    # quarantines every early decision point with a clinical-sounding rejection for
    # what is correct behaviour. Local import: ``ingestion`` pulls in the store, and
    # this module is meant to stay runnable in an offline admin dry-run.
    from asclepius.ingestion import modalities_present_in
    visible["required_modalities"] = modalities_present_in(visible)
    stats = {"labs": lab_stats, "notes": note_stats, "medications": med_stats,
             "problems": len(problems), "studies": len(studies),
             "withheld_untimed": untimed}
    return visible, held_out, stats


def outcome_delta(
    outcome_case: Optional[Dict[str, Any]], *,
    outcome_index_offset: int, decision_index_offset: int,
) -> Dict[str, Any]:
    """What encounter *k+1* adds that encounter *k* did not already show.

    THE PHASE 4 REVEAL (Longitudinal Cases PRD §4, Phase 4). Both cases are stored
    re-based to their OWN decision point, so this lifts the outcome case's items
    back onto the parent chart's axis, keeps only what was recorded AFTER the
    decision point being graded, and re-bases the survivors to that decision point
    — so the physician reads "day +12", counted from the moment they committed.

    Two rules make this safe to serve:

    * It is a FILTER over a case that has already been de-identified, leak-gated
      and stored. Nothing here reaches back into the parent chart, so the reveal
      cannot surface a field the ingestion pipeline never cleared.
    * ``> decision_index_offset``, strictly. An item recorded ON the decision day
      was visible when the physician committed and is not an outcome; including it
      would let a physician "verify" an expectation against a datum they had
      already read.

    Raises ``RealCaseError`` when either offset is missing. **FAIL CLOSED**: with no
    axis to place the two windows on, the only alternatives are serving the outcome
    case whole — which shows the physician chart state they already saw as if it
    were new, and can reach further back than the decision point — or serving
    nothing while saying it worked. Both are worse than an error the admin sees.
    """
    if not isinstance(outcome_index_offset, int) or not isinstance(decision_index_offset, int):
        raise RealCaseError(
            "cannot build the outcome reveal: one of the two decision-point offsets "
            "is missing, so the two windows cannot be placed on a common axis")
    c = as_dict(outcome_case) or {}
    shift = outcome_index_offset - decision_index_offset   # ≥ 0 for a later encounter
    if shift < 0:
        # The "outcome" precedes the decision it is meant to verify — a walk built
        # out of chronological order. Every item would be filtered out and the
        # physician would be shown an empty panel reading "the record adds
        # nothing", which is a false statement about the chart rather than a
        # missing one. Fail closed and name it.
        raise RealCaseError(
            f"the outcome encounter is {abs(shift)} day(s) BEFORE the decision it "
            "would verify; the trajectory is not in chronological order")

    def _after(key: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for item in c.get(key) or []:
            off = _offset_of(item)
            if off is None or off + shift <= 0:
                # Untimed items are dropped, not shown. On the visible side untimed
                # FAILS CLOSED because it might be the answer; here it fails closed
                # because an outcome we cannot date is not evidence that anything
                # happened after the decision.
                continue
            rebased = dict(item)
            rebased["collected_offset_days"] = off + shift
            out.append(rebased)
        return out

    vitals = dict(c.get("vitals") or {})
    v_off = vitals.get("collected_offset_days")
    if isinstance(v_off, int) and v_off + shift > 0:
        vitals["collected_offset_days"] = v_off + shift
    else:
        vitals = {}

    delta = {
        "lab_panels": _after("lab_panels"),
        "notes": _after("notes"),
        "studies": _after("studies"),
        "medications": _after("medications"),
        "problem_list": _after("problem_list"),
        "vitals": vitals,
        # Carried so the reveal renders under the same findings rule as the case it
        # came from. §9.5: this legitimately VARIES across one trajectory — a window
        # with no imaging is 'visible', a later one carrying a study asset is
        # 'hidden' — and the physician may notice findings appearing or disappearing
        # as they move forward in time. That is the policy reflecting what each
        # window contains, and it is stated in the data dictionary rather than left
        # to be discovered in a buyer's diligence.
        "study_findings_policy": c.get("study_findings_policy") or "visible",
        "days_after_decision": shift,
    }
    delta["n_events"] = sum(len(delta[k]) for k in
                            ("lab_panels", "notes", "studies", "medications", "problem_list"))
    return delta


def _visible_vitals(case: Dict[str, Any], index_offset: int) -> Dict[str, Any]:
    """Vitals are one flat dict carrying ONE timing marker for the set, so they are
    all-or-nothing against the cutoff — a post-decision vitals set is withheld
    whole rather than partially."""
    vit = dict(case.get("vitals") or {})
    off = vit.get("collected_offset_days")
    if isinstance(off, int):
        if off > index_offset:
            return {}
        vit["collected_offset_days"] = off - index_offset
    return vit


# ═══════════════════════════════════════════════════════════════════════════════
# §3.4  Specialty + taxonomy, from the chart
# ═══════════════════════════════════════════════════════════════════════════════
# Signals are (weight, pattern). Analyte names and problem text are matched against
# the same table — a creatinine trend and a "CKD stage 4" problem are the same
# evidence about which specialist should see this case.
_SPECIALTY_SIGNALS: Dict[str, Tuple[Tuple[float, "re.Pattern[str]"], ...]] = {
    "nephrology": (
        (1.0, re.compile(r"\b(?:creatinine|egfr|urea|bun|cystatin)\b", re.I)),
        (1.0, re.compile(r"\b(?:aki|acute\s+kidney|ckd|chronic\s+kidney|nephropath\w*"
                         r"|nephritic|nephrotic|glomerul\w+|dialys\w+|renal\s+failure"
                         r"|hepatorenal)\b", re.I)),
        (0.7, re.compile(r"\b(?:potassium|sodium|bicarbonate|chloride|phosphate|magnesium"
                         r"|calcium)\b", re.I)),
        (0.7, re.compile(r"\b(?:hyperkal\w+|hypokal\w+|hypernatr\w+|hyponatr\w+"
                         r"|metabolic\s+acidosis|metabolic\s+alkalosis|acid[\s-]*base)\b", re.I)),
        (0.6, re.compile(r"\b(?:urinalysis|urine\s+(?:protein|albumin|output)|proteinuria"
                         r"|albuminuria|casts?)\b", re.I)),
        (0.5, re.compile(r"\brft\b|\brenal\s+(?:function|panel)\b", re.I)),
    ),
    "cardiology": (
        (1.0, re.compile(r"\b(?:troponin|ck[\s-]*mb|nt[\s-]*pro[\s-]*bnp|probnp|bnp)\b", re.I)),
        (1.0, re.compile(r"\b(?:acute\s+coronary|acs|stemi|nstemi|myocardial\s+infarct\w*"
                         r"|angina|heart\s+failure|cardiomyopath\w*|atrial\s+fibrillation"
                         r"|arrhythmi\w+|valv\w+|endocarditis)\b", re.I)),
        (0.7, re.compile(r"\b(?:ecg|ekg|echo(?:cardiogram)?|ejection\s+fraction|\bef\b"
                         r"|angiograph\w+|cath(?:eteri\w+)?)\b", re.I)),
        (0.5, re.compile(r"\b(?:ischaemi\w+|ischemi\w+|chest\s+pain)\b", re.I)),
    ),
    # Hepatology (V4 PRD §1.4). Deliberately weighted so a chart is hepatology
    # when the LIVER-SPECIFIC signals fire, not merely because it mentions a
    # creatinine — an AKI in a cirrhotic is a real routing contest and the
    # nephrology signals above are as strong. What breaks the tie is the presence
    # of biliary/portal/synthetic-function evidence, which is what a hepatologist
    # is the answer key for.
    "hepatology": (
        (1.0, re.compile(r"\b(?:cirrhos\w+|portal\s+hypertension|portal\s+vein\s+thromb\w+"
                         r"|cavernous\s+transformation|portal\s+bilio?path\w+|vari(?:x|ces|ceal)\w*"
                         r"|hepatic\s+encephalopath\w+|cholangitis|choledocholithias\w+"
                         r"|hepatorenal|\bercp\b|\bmrcp\b)\b", re.I)),
        (1.0, re.compile(r"\b(?:bilirubin|ggt|gamma[\s-]*gt|alkaline\s+phosphatase|\balp\b"
                         r"|\balt\b|\bast\b|\bsgpt\b|\bsgot\b|transaminas\w+)\b", re.I)),
        (0.7, re.compile(r"\b(?:jaundice|icteric|cholestat\w+|hepatocellular|hepatomegaly"
                         r"|splenomegaly|hypersplenism|ascites|paracentes\w+|\bsaag\b)\b", re.I)),
        (0.7, re.compile(r"\b(?:child[\s-]*pugh|\bmeld\b|hepatotoxic\w+|drug[\s-]*induced\s+liver"
                         r"|\bdili\b|hepatitis\s*[abcde]\b|\bstent\w*\b.{0,30}\bbil\w+)\b", re.I)),
        (0.5, re.compile(r"\blft\b|liver\s+(?:function|panel|biops\w+)\b|\bhepat\w+", re.I)),
    ),
    "oncology": (
        (1.0, re.compile(r"\b(?:carcinom\w+|adenocarcinom\w+|lymphom\w+|leukaemi\w+|leukemi\w+"
                         r"|myelom\w+|sarcom\w+|metastas\w+|malignan\w+|neoplas\w+|tumou?r)\b", re.I)),
        (1.0, re.compile(r"\b(?:chemotherap\w+|immunotherap\w+|checkpoint|pembrolizumab"
                         r"|nivolumab|rituximab|tyrosine\s+kinase|egfr\s+mutation|alk\b"
                         r"|braf\b|pd[\s-]*l1)\b", re.I)),
        (0.7, re.compile(r"\b(?:histopath\w*|biopsy|cytolog\w+|immunohistochem\w+|ngs\b"
                         r"|molecular\s+panel|ca\s*19[\s-]*9|ca\s*125|psa\b|afp\b|cea\b)\b", re.I)),
        (0.6, re.compile(r"\b(?:febrile\s+neutropeni\w+|tumou?r\s+lysis|cord\s+compression"
                         r"|svc\s+syndrome)\b", re.I)),
    ),
}

# Subtopic signals, keyed ``specialty/bucket_id/subtopic``. Deliberately partial:
# a chart signal only exists for the subtopics a chart can actually evidence, and
# an unmatched subtopic returns None rather than the nearest-looking label.
_SUBTOPIC_SIGNALS: Dict[str, "re.Pattern[str]"] = {
    "nephrology/electrolyte_acid_base/hyponatremia_ods":
        re.compile(r"\bhyponatr\w+|\bsodium\b.{0,40}\b1[0-2]\d\b|osmotic\s+demyelin\w+", re.I),
    "nephrology/electrolyte_acid_base/hyperkalemia_treatment":
        re.compile(r"\bhyperkal\w+|\bhypokal\w+|\bpotassium\b", re.I),
    "nephrology/electrolyte_acid_base/hypercalcemia":
        re.compile(r"\bhypercalca?emi\w+|\bhypocalca?emi\w+", re.I),
    "nephrology/electrolyte_acid_base/mixed_acid_base":
        re.compile(r"\bacid[\s-]*base\b|metabolic\s+acidosis|respiratory\s+alkalosis"
                   r"|\bbase\s+excess\b|\banion\s+gap\b|\bketoacidos\w+|\babg\b", re.I),
    "nephrology/aki_critical_care/hepatorenal":
        re.compile(r"\bhepatorenal\b|\bascites\b|portal\s+hypertension|\bcirrhos\w+", re.I),
    "nephrology/aki_critical_care/contrast_associated_aki":
        re.compile(r"contrast[\s-]*(?:induced|associated)|\bcontrast\s+nephropath\w*", re.I),
    "nephrology/aki_critical_care/rhabdomyolysis":
        re.compile(r"\brhabdomyolys\w+|\bcreatine\s+kinase\b|\bcpk\b", re.I),
    "nephrology/aki_critical_care/crrt_vs_ihd":
        re.compile(r"\bcrrt\b|\bsledd\b|intermittent\s+h(?:a)?emodialysis|\brrt\b", re.I),
    "nephrology/renal_drug_dosing/contrast":
        re.compile(r"\bcontrast\s+(?:study|ct|mri)\b", re.I),
    "nephrology/renal_drug_dosing/antibiotic_adjustment":
        re.compile(r"\b(?:vancomycin|meropenem|piperacillin|tazobactam|amikacin|gentamicin"
                   r"|colistin)\b", re.I),
    "nephrology/glomerular_autoimmune/nephrotic_management":
        re.compile(r"\bnephrotic\b|\bproteinuria\b|\balbuminuria\b", re.I),
    "cardiology/acs_nuance/troponin_interpretation":
        re.compile(r"\btroponin\b", re.I),
    "cardiology/acs_nuance/type_2_mi":
        re.compile(r"\btype\s*2\s*mi\b|demand\s+ischaemi\w+|demand\s+ischemi\w+", re.I),
    "cardiology/hf_gdmt/beta_blocker_decompensation":
        re.compile(r"\bheart\s+failure\b|\bdecompensat\w+|\bpulmonary\s+o?edema\b", re.I),
    "cardiology/arrhythmia_anticoag/af_stroke_vs_bleed":
        re.compile(r"\batrial\s+fibrillation\b|\baf\b.{0,20}\banticoag\w+", re.I),
    "cardiology/ecg_high_risk_subtle/hyperkalemia_morphology":
        re.compile(r"\becg\b.{0,60}\bhyperkal\w+|\bpeaked\s+t\b", re.I),
    "oncology/onc_emergencies/febrile_neutropenia":
        re.compile(r"febrile\s+neutropeni\w+|\bneutropeni\w+\b.{0,40}\bfever\b", re.I),
    "oncology/onc_emergencies/tumor_lysis":
        re.compile(r"tumou?r\s+lysis|\buric\s+acid\b.{0,40}\bpotassium\b", re.I),
    "oncology/onc_emergencies/hypercalcemia":
        re.compile(r"\bhypercalca?emi\w+\b.{0,60}\bmalignan\w+", re.I),
    "oncology/paraneoplastic/siadh":
        re.compile(r"\bsiadh\b|inappropriate\s+antidiuretic", re.I),
    "oncology/molecular_therapy_selection/egfr":
        re.compile(r"\begfr\s+mutation\b|\bexon\s*(?:19|21)\b", re.I),
    "oncology/staging_biomarker/biomarker_discrepancy":
        re.compile(r"\bbiomarker\b|\bimmunohistochem\w+", re.I),
    # Hepatology (V4 PRD §1.4). Onboarding a specialty is a corpus file + a
    # taxonomy + a registry entry — and THESE, without which
    # ``classify_case_to_bucket`` returns (None, None) for every hepatology case
    # and the export ships an empty taxonomy field. The registry guard catches a
    # missing corpus; only a signal here makes the bucket reachable.
    "hepatology/biliary_obstruction/post-ERCP complications":
        re.compile(r"post[\s-]*ercp|\bercp\b.{0,60}\bpancreatit\w+"
                   r"|\bpancreatit\w+.{0,60}\bercp\b", re.I),
    "hepatology/biliary_obstruction/stent management":
        re.compile(r"\bstent\w*\b.{0,60}\b(?:cbd|bile|biliary|common\s+bile)\b"
                   r"|\b(?:cbd|biliary)\b.{0,40}\bstent\w*\b|stent\s+(?:occlusion|revision|exchange)", re.I),
    "hepatology/biliary_obstruction/choledocholithiasis":
        re.compile(r"choledocholithias\w+|\bcbd\s+stone\w*|common\s+bile\s+duct\s+stone\w*", re.I),
    "hepatology/biliary_obstruction/stricture":
        re.compile(r"\b(?:cbd|biliary|bile\s+duct)\b.{0,30}strictur\w+|strictur\w+.{0,30}\b(?:cbd|bile\s+duct)\b", re.I),
    "hepatology/biliary_obstruction/cholangitis":
        re.compile(r"\bcholangitis\b", re.I),
    "hepatology/portal_hypertension/portal vein thrombosis":
        re.compile(r"portal\s+vein\s+thromb\w+|\bpvt\b|cavernous\s+transformation"
                   r"|portal\s+bilio?path\w+", re.I),
    "hepatology/portal_hypertension/hepatorenal syndrome":
        re.compile(r"\bhepatorenal\b|\bhrs\b[\s-]*aki|\bterlipressin\b", re.I),
    "hepatology/portal_hypertension/variceal bleeding":
        re.compile(r"\bvari(?:x|ces|ceal)\w*\b|\boesophageal\s+vari\w+|\besophageal\s+vari\w+", re.I),
    "hepatology/portal_hypertension/ascites":
        re.compile(r"\bascites\b|\bparacentes\w+|\bsaag\b", re.I),
    "hepatology/portal_hypertension/hepatic encephalopathy":
        re.compile(r"hepatic\s+encephalopath\w+|\bh\.?e\.?\b.{0,20}\blactulose\b|\brifaximin\b"
                   r"|\basterixis\b", re.I),
    "hepatology/liver_injury_patterns/enzyme-bilirubin dissociation":
        re.compile(r"\bbilirubin\b.{0,120}\b(?:ggt|alp|alkaline\s+phosphatase|alt|ast)\b"
                   r"|\b(?:ggt|alp|alkaline\s+phosphatase)\b.{0,120}\bbilirubin\b", re.I),
    "hepatology/liver_injury_patterns/cholestatic vs hepatocellular":
        re.compile(r"\bcholestat\w+|hepatocellular\s+(?:pattern|injury)|\br\s*ratio\b", re.I),
    "hepatology/liver_injury_patterns/drug-induced liver injury":
        re.compile(r"drug[\s-]*induced\s+liver|\bdili\b|hepatotoxic\w+"
                   r"|co[\s-]*amoxiclav|\bparacetamol\b|\bacetaminophen\b", re.I),
    "hepatology/liver_injury_patterns/viral hepatitis":
        re.compile(r"\bhepatitis\s*[abcde]\b|\bhbsag\b|\banti[\s-]*hcv\b|\bhbv\b|\bhcv\b", re.I),
    "hepatology/cirrhosis_complications/transfusion thresholds":
        re.compile(r"\btransfus\w+|\bpacked\s+(?:red\s+)?cells?\b|\bprbc\b|\bunits?\s+of\s+blood\b", re.I),
    "hepatology/cirrhosis_complications/coagulopathy vs bleeding risk":
        re.compile(r"\binr\b|\bcoagulopath\w+|fresh\s+frozen\s+plasma|\bffp\b|rebalanced\s+h?aemostas\w+", re.I),
    "hepatology/cirrhosis_complications/spontaneous bacterial peritonitis":
        re.compile(r"spontaneous\s+bacterial\s+peritonitis|\bsbp\b", re.I),
    "hepatology/cirrhosis_complications/hyponatremia in cirrhosis":
        re.compile(r"\bhyponatr\w+.{0,80}\b(?:cirrhos\w+|ascites|liver)\b"
                   r"|\b(?:cirrhos\w+|ascites)\b.{0,80}\bhyponatr\w+", re.I),
    "hepatology/hepatic_drug_safety/dosing in hepatic impairment":
        re.compile(r"child[\s-]*pugh|hepatic\s+impairment|\bmeld\b", re.I),
    "hepatology/hepatic_drug_safety/sedation and encephalopathy":
        re.compile(r"\bbenzodiazepin\w+|\blorazepam\b|\bmidazolam\b|\bdiazepam\b|\bsedati\w+", re.I),
    "hepatology/hepatic_drug_safety/hepatotoxicity":
        re.compile(r"\bhepatotoxic\w+|\bn[\s-]*acetylcystein\w+|\bnac\b", re.I),
    "hepatology/hepatic_drug_safety/anticoagulation in PVT":
        re.compile(r"\banticoagul\w+.{0,60}\b(?:portal|pvt|cirrhos\w+)\b"
                   r"|\b(?:portal|pvt)\b.{0,60}\banticoagul\w+", re.I),
}

_SPECIALTY_CONFIDENCE_FLOOR = 0.6


def _case_signal_text(case: Dict[str, Any]) -> str:
    """The MODEL-VISIBLE surface a specialty/bucket judgement may read. Never the
    ground truth — inferring the specialty from the answer key would tag the case
    by information the physician is not given."""
    c = public_case(as_dict(case) or {}) or {}
    bits: List[str] = []
    for p in c.get("problem_list") or []:
        bits.append(str(p.get("condition") or ""))
    for m in c.get("medications") or []:
        bits.append(str(m.get("drug") or ""))
    for panel in c.get("lab_panels") or []:
        bits.append(str(panel.get("panel") or ""))
        for r in panel.get("results") or []:
            flag = str(r.get("flag") or "").upper()
            bits.append(f"{r.get('analyte')} {r.get('value')}" + (f" [{flag}]" if flag else ""))
    for s in c.get("studies") or []:
        bits += [str(s.get("modality") or ""), str(s.get("label") or ""),
                 str(s.get("findings") or "")]
    for n in c.get("notes") or []:
        if n.get("model_visible") is not False:
            bits.append(str(n.get("text") or ""))
    return "\n".join(b for b in bits if b)


def infer_specialty(case: Optional[Dict[str, Any]]) -> Tuple[Optional[str], float, Dict[str, float]]:
    """``(specialty, confidence, per_specialty_scores)`` from the chart itself.

    Below ``_SPECIALTY_CONFIDENCE_FLOOR`` the answer is ``None`` — route to admin
    rather than guess. A WRONG specialty is worse than a missing one: it routes the
    case to the wrong physician pool and mislabels it in the export, invisibly.
    The real record is stroke / diabetes / pancreatitis / ascites / seizure, and
    the batch path today would stamp it nephrology and ask the nephrology default
    question.
    """
    text = _case_signal_text(case)
    scores: Dict[str, float] = {}
    for specialty, signals in _SPECIALTY_SIGNALS.items():
        if not is_enabled(specialty):
            continue
        score = 0.0
        for weight, pat in signals:
            hits = len(pat.findall(text))
            if hits:
                # Saturating, not linear: a chart mentioning creatinine 200 times is
                # not 200× more nephrological than one mentioning it twice.
                score += weight * min(1.0, math.log1p(hits) / math.log(6))
        scores[specialty] = round(score, 3)
    if not scores or max(scores.values()) <= 0:
        return None, 0.0, scores
    total = sum(scores.values())
    best = max(scores, key=lambda s: scores[s])
    confidence = round(scores[best] / total, 3) if total else 0.0
    if confidence < _SPECIALTY_CONFIDENCE_FLOOR:
        return None, confidence, scores
    return best, confidence, scores


def classify_case_to_bucket(
    case: Optional[Dict[str, Any]], specialty: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """``(bucket_id, subtopic)`` from ``SPECIALTY_REGISTRY`` — or ``(None, None)``.

    An honest None beats a wrong bucket, because the bucket is what a buyer filters
    on. A bucket with no matching subtopic still returns the bucket: the coverage
    claim is real even when the sub-claim is not."""
    cfg = SPECIALTY_REGISTRY.get((specialty or "").strip().lower())
    if cfg is None or not cfg.enabled:
        return None, None
    text = _case_signal_text(case)
    best: Optional[Tuple[int, str, str]] = None
    for bucket in cfg.taxonomy:
        for subtopic in bucket.subtopics:
            pat = _SUBTOPIC_SIGNALS.get(f"{cfg.name}/{bucket.id}/{subtopic}")
            if pat is None:
                continue
            hits = len(pat.findall(text))
            if hits and (best is None or hits > best[0]):
                best = (hits, bucket.id, subtopic)
    if best is None:
        return None, None
    return best[1], best[2]


# ═══════════════════════════════════════════════════════════════════════════════
# §3.6  Difficulty — measured, not asserted
# ═══════════════════════════════════════════════════════════════════════════════
DIFFICULTY_WEIGHTS: Dict[str, float] = {
    # EMPIRICAL — the only axis with an outcome behind it. The pooled frontier
    # failure rate against the sealed held-out outcome.
    "model_failure_rate": 0.50,
    # STRUCTURAL — a prior, computed from the case, never from a model.
    "competing_problems": 0.12,      # active problems on the differential, capped at 5
    "abnormal_analyte_ratio": 0.08,  # flagged results / total, at the index event
    "longitudinal_span": 0.10,       # encounters the decisive trend spans, capped at 4
    "decisive_datum_buried": 0.12,   # the deciding value is not in the most recent panel
    "guideline_recency": 0.08,       # bucket is tagged recent_standard_of_care
}
# A case frontier models get right is not hard, however baroque the chart. This is
# the whole product claim, so it is a hard gate and not a weight.
HARD_BAND_MIN_FAILURE_RATE = 0.4
_HARD_SCORE_MIN = 0.66
_MEDIUM_SCORE_MIN = 0.33

# Difficulty is a property of the ITEM RELATIVE TO A SOLVER, not of the text — in
# Item Response Theory the difficulty parameter *b* lives on the same latent scale
# as the population answering it. Stable IRT calibration needs roughly 30 responses
# for ±1 logit and 100 for ±0.5 (Linacre, Rasch Measurement Transactions 7:4),
# which we will not have per case. So: model failure is the empirical axis,
# structure is the prior, and a physician is the arbiter on first label.


def _structural_axes(
    visible: Dict[str, Any], encounters_spanned: int, bucket_id: Optional[str],
) -> Dict[str, float]:
    problems = [p for p in (visible.get("problem_list") or []) if p.get("condition")]
    panels = sorted((visible.get("lab_panels") or []),
                    key=lambda p: _offset_of(p) if _offset_of(p) is not None else -10**6)
    results = [r for p in panels for r in (p.get("results") or [])]
    abnormal = [r for r in results if str(r.get("flag") or "").upper() in _ABNORMAL_FLAGS]

    # The decisive datum is buried when the most extreme abnormal result is NOT in
    # the LATEST DRAW — a physician (or a model) who reads only the most recent
    # labs misses it. Compared against every panel at the latest offset, not just
    # the last panel in the list: a single draw routinely produces several panels.
    buried = 0.0
    if abnormal and panels:
        latest_off = _offset_of(panels[-1])
        critical = [r for r in results if str(r.get("flag") or "").upper() in ("LL", "HH")]
        pool = {str(r.get("analyte") or "").strip().lower() for r in (critical or abnormal)}
        in_latest = any(
            str(r.get("analyte") or "").strip().lower() in pool
            and str(r.get("flag") or "").upper() in _ABNORMAL_FLAGS
            for p in panels if _offset_of(p) == latest_off
            for r in (p.get("results") or [])
        )
        buried = 0.0 if in_latest else 1.0

    return {
        "competing_problems": min(1.0, len(problems) / 5.0),
        "abnormal_analyte_ratio": (len(abnormal) / len(results)) if results else 0.0,
        "longitudinal_span": min(1.0, max(0, encounters_spanned) / 4.0),
        "decisive_datum_buried": buried,
        "guideline_recency": 1.0 if (bucket_id or "") == "recent_standard_of_care" else 0.0,
    }


def score_difficulty(
    visible: Dict[str, Any], *, encounters_spanned: int = 1,
    bucket_id: Optional[str] = None,
    model_failure_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """``{score, band, axes, measured, gate}`` for one proposed case.

    ``score >= 0.66`` → ``hard`` **and** ``model_failure_rate >= 0.4``;
    ``0.33–0.66`` → ``medium``; below → ``easy``.

    Structural features alone can PROPOSE hard but cannot confer it. With no
    measurement available the band is capped at ``medium`` and ``measured`` is
    False, which is exactly what ``ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY`` exists
    to keep out of the queue."""
    axes = _structural_axes(visible, encounters_spanned, bucket_id)
    measured = model_failure_rate is not None
    if measured:
        axes["model_failure_rate"] = max(0.0, min(1.0, float(model_failure_rate)))
        score = sum(DIFFICULTY_WEIGHTS[k] * v for k, v in axes.items())
    else:
        # Renormalise over the structural weights only, so an unmeasured case is
        # scored on its own terms rather than penalised by a missing axis.
        structural_weight = sum(w for k, w in DIFFICULTY_WEIGHTS.items()
                                if k != "model_failure_rate")
        score = sum(DIFFICULTY_WEIGHTS[k] * v for k, v in axes.items()) / structural_weight

    score = round(min(1.0, max(0.0, score)), 3)
    if score >= _HARD_SCORE_MIN and measured and axes["model_failure_rate"] >= HARD_BAND_MIN_FAILURE_RATE:
        band = "hard"
    elif score >= _MEDIUM_SCORE_MIN:
        band = "medium"
    else:
        band = "easy"
    gate = None
    if score >= _HARD_SCORE_MIN and band != "hard":
        gate = ("structure proposes hard but "
                + ("the frontier failure rate is below "
                   f"{HARD_BAND_MIN_FAILURE_RATE}" if measured
                   else "no frontier measurement is available")
                + " — banded down")
    return {"score": score, "band": band, "axes": {k: round(v, 3) for k, v in axes.items()},
            "measured": measured, "model_failure_rate": model_failure_rate,
            "weights": dict(DIFFICULTY_WEIGHTS), "gate_note": gate}


# ═══════════════════════════════════════════════════════════════════════════════
# §3.3 / §3.7  Question + failure mode
# ═══════════════════════════════════════════════════════════════════════════════
class AnswerLeakage(ValueError):
    """A derived question references material that is held out. A question that
    mentions the answer is not a question."""


def _distinctive(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]{4,}", str(text or "").lower())]


_QUESTION_STOPWORDS = frozenset({
    "patient", "after", "point", "started", "recorded", "results", "result",
    "problems", "problem", "treatment", "documented", "change", "this", "with",
    "chronic", "acute", "workup", "features", "possible", "elevation",
})


def assert_question_has_no_leakage(question: str, held_out: Dict[str, Any]) -> None:
    """Refuse a question that reproduces a distinctive held-out term.

    Checked against the SEALED side only. The bar is a distinctive multi-word
    phrase or a rare single term — a question is allowed to say "seizure" when the
    visible chart already says "seizure disorder"; it is not allowed to introduce
    "aspiration pneumonia" that appears only after the decision point."""
    q_tokens = set(_distinctive(question)) - _QUESTION_STOPWORDS
    if not q_tokens:
        return
    for leaf in (list(held_out.get("newly_established_problems")
                      or held_out.get("problems_recorded_after") or [])
                 + list(held_out.get("newly_started_drugs")
                        or held_out.get("drugs_started_after") or [])):
        toks = [t for t in _distinctive(leaf) if t not in _QUESTION_STOPWORDS]
        if not toks:
            continue
        overlap = [t for t in toks if t in q_tokens]
        if len(overlap) >= max(2, math.ceil(0.8 * len(toks))):
            raise AnswerLeakage(
                "derived question reproduces a held-out term; it states the answer "
                f"instead of asking for it (overlap: {sorted(set(overlap))[:4]})")


def _fallback_question(visible: Dict[str, Any], specialty: Optional[str]) -> str:
    """A deterministic, case-SPECIFIC question built from the visible window.

    Not a per-specialty default string — that is the thing this replaces. It names
    the patient, the most abnormal visible datum and the active problem, then asks
    for the call and the reason, in the register of ``gold_cases``."""
    demo = visible.get("demographics") or {}
    who = " ".join(x for x in [
        (f"A {demo['age_band']}" if demo.get("age_band") else "A patient"),
        {"M": "male", "F": "female"}.get(str(demo.get("sex") or ""), ""),
    ] if x).strip() or "A patient"

    problems = [str(p.get("condition") or "").strip()
                for p in (visible.get("problem_list") or []) if p.get("condition")]
    context = "; ".join(problems[:3]) if problems else "an undifferentiated presentation"

    worst: Optional[Dict[str, Any]] = None
    for panel in visible.get("lab_panels") or []:
        for r in panel.get("results") or []:
            rank = {"LL": 3, "HH": 3, "L": 1, "H": 1}.get(str(r.get("flag") or "").upper(), 0)
            if rank and (worst is None or rank > worst["_rank"]):
                worst = {**r, "_rank": rank}
    if worst:
        datum = (f"The decisive value at this point is {worst.get('analyte')} "
                 f"{worst.get('value')}{(' ' + str(worst['unit'])) if worst.get('unit') else ''}.")
    else:
        datum = "The decisive findings are in the notes and studies below."

    return (f"{who} with {context} is at a decision point. {datum} "
            "State the primary problem driving the current picture, give its most "
            "likely cause, and name the single most important next step in "
            "management — with the reason it is the next step.")


async def derive_clinical_question(
    visible: Dict[str, Any], held_out: Dict[str, Any], specialty: Optional[str],
) -> Tuple[str, str]:
    """``(question, source)`` — a case-specific question authored from the VISIBLE
    window only. ``source`` is ``"model"`` or ``"deterministic"``.

    Two hard rules, both enforced rather than requested:
      1. the question may only reference data visible at or before the index event;
      2. it must be answerable from that window.

    A model-authored question that fails ``assert_question_has_no_leakage`` is
    DISCARDED for the deterministic one — a leaking question is not repairable by
    asking again, and shipping it burns the case.
    """
    fallback = _fallback_question(visible, specialty)
    try:
        from ai.llm_client import call_llm, first_text
        from asclepius.cases import render_case_prompt
    except Exception:                                      # pragma: no cover
        return fallback, "deterministic"

    system = (
        "You write the single clinical question for a physician-grade evaluation "
        "case. You are shown ONLY what the treating team could see at the decision "
        "point. Write one question that:\n"
        "  * states the patient in one clause and the decisive visible finding in "
        "one clause, then asks for the call PLUS the reason;\n"
        "  * references ONLY data present in the case below;\n"
        "  * never states or hints at the answer, the eventual diagnosis, or the "
        "treatment that was chosen;\n"
        "  * is answerable from the case as shown.\n"
        "Reply with the question text and nothing else. No preamble, no quotes."
    )
    try:
        resp, _rec = await call_llm(
            role="asclepius_prompt_gen",
            system=system,
            messages=[{"role": "user", "content": render_case_prompt(visible, "")[:24000]}],
            prompt_id="asclepius_real_case_question",
            purpose="asclepius_real_case_question",
        )
        text = (first_text(resp) or "").strip().strip('"').strip()
    except Exception as exc:
        log.info("real-case question generation unavailable: %s", exc)
        return fallback, "deterministic"

    if not text or len(text) < 40:
        return fallback, "deterministic"
    try:
        assert_question_has_no_leakage(text, held_out)
    except AnswerLeakage as exc:
        log.info("discarded model-authored question: %s", exc)
        return fallback, "deterministic"
    return text, "model"


#: Last resort when the models failed but the judge gave no usable reason. Still
#: gated on a non-zero failure rate — this describes a trap they DID fall for.
_GENERIC_FAILURE_MODE = (
    "anchoring on the loudest abnormality rather than the decisive one; the value "
    "that settles the question is not the most extreme number on the page")


def derive_ai_failure_mode(
    visible: Dict[str, Any], difficulty: Dict[str, Any],
    model_results: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Name the trap from what the models actually got wrong.

    Free text, in the register of the ratified gold cases ("anchoring (SIADH → 3%
    saline); overtreatment / osmotic demyelination risk"). Derived from the
    k-sample failures — **if the models did not fall for it, it is not the trap**,
    so with no failures this returns None and the flawed candidate stays unkeyed
    rather than keyed to an invented one.
    """
    reasons: List[str] = []
    for r in model_results or []:
        for field in ("failure_reason", "explanation", "reasoning_flaw", "why_failed"):
            v = str((r or {}).get(field) or "").strip()
            if v:
                reasons.append(v)
                break
    if reasons:
        # The most common failure across draws, trimmed to one clause.
        best = max(set(reasons), key=reasons.count)
        return re.split(r"(?<=[.;])\s", best.strip())[0][:240]

    rate = difficulty.get("model_failure_rate")
    if not rate:
        return None
    axes = difficulty.get("axes") or {}
    if axes.get("decisive_datum_buried"):
        return ("anchoring on the most recent panel; the deciding value sits in an "
                "earlier draw and the trend, not the latest number, is the finding")
    if (axes.get("competing_problems") or 0) >= 0.8:
        return ("premature closure on the loudest active problem while a second, "
                "less prominent problem on the list is driving the current picture")
    return _GENERIC_FAILURE_MODE


# ═══════════════════════════════════════════════════════════════════════════════
# The plan
# ═══════════════════════════════════════════════════════════════════════════════
class TemporalLeak(ValueError):
    """A case carries an item recorded after its own decision point."""


def assert_temporal_split(visible: Dict[str, Any]) -> None:
    """No item in a generated case may sit after day 0.

    On a real chart this — not token overlap — is the leakage guarantee. The
    visible window is built by a total temporal split, and an item with a positive
    offset means the split failed somewhere: a re-base that missed a collection, a
    curation step that re-introduced an item. Cheap, exact, and it fails the case
    rather than shipping one whose answer is in the prompt."""
    for key in _TIMED_COLLECTIONS:
        for item in visible.get(key) or []:
            off = _offset_of(item)
            if off is not None and off > 0:
                raise TemporalLeak(
                    f"{key} carries an item at day +{off}, after the decision point")
    off = (visible.get("vitals") or {}).get("collected_offset_days")
    if isinstance(off, int) and off > 0:
        raise TemporalLeak(f"vitals were recorded at day +{off}, after the decision point")


def _content_blockers(visible: Dict[str, Any]) -> List[str]:
    """Why this proposal could not become a task, in the caller's words. Runs the
    REAL gate (``assert_multimodal_content``) rather than a lookalike, so a
    proposal marked generatable actually generates."""
    blockers: List[str] = []
    try:
        assert_multimodal_content(visible)
    except MultimodalContentError as exc:
        blockers.append(str(exc))
    try:
        assert_temporal_split(visible)
    except TemporalLeak as exc:
        blockers.append(str(exc))
    return blockers


_NARRATIVE_TYPE_RE = re.compile(
    r"^(?:progress(?: note)?|consult(?:ation)?(?: note)?|h&p|history(?: and physical)?|"
    r"admission(?: note)?|emergency(?: note)?|er|triage|clinical note|note)$", re.I)
_NON_NARRATIVE_OPENING_RE = re.compile(
    r"^(?:form:\s*)?(?:fresh orders|medication form|medication administration|"
    r"head to toe assessment|patient assessment|intake\s*/\s*output|"
    r"nursing (?:assessment|chart)|vital signs|orders?\s*:)", re.I)


def has_encounter_narrative(visible: Dict[str, Any], encounter: Dict[str, Any],
                            index_offset: int) -> bool:
    """A visible clinical narrative from THIS encounter, not a prior resolution.

    This is a conservative document-role check, not a clinical quality judgment.
    Known order/nursing forms sometimes arrive mislabeled as Progress; they do
    not establish a presenting narrative merely by carrying that type label.
    """
    lo = encounter.get("start_offset")
    if not isinstance(lo, int):
        return False
    for note in visible.get("notes") or []:
        off = _offset_of(note)
        text = str(note.get("text") or "").strip()
        if (off is not None and lo <= off + index_offset <= index_offset
                and note.get("model_visible") is not False
                and not note.get("withheld_reason")
                and len(text) >= 40
                and _NARRATIVE_TYPE_RE.fullmatch(str(note.get("note_type") or "").strip())
                and not _NON_NARRATIVE_OPENING_RE.match(text)):
            return True
    return False


def has_encounter_observation(visible: Dict[str, Any], encounter: Dict[str, Any],
                              index_offset: int) -> bool:
    """An INTERVAL point needs an observation from its own window: a lab panel, a
    report, an order sheet, or a narrative. Chart state (problems, medications)
    does not count — it was there before the visit."""
    lo = encounter.get("start_offset")
    if not isinstance(lo, int):
        return False
    def _in_window(item: Dict[str, Any]) -> bool:
        off = _offset_of(item)
        return off is not None and lo <= off + index_offset <= index_offset
    if any(_in_window(p) and (p.get("results") or []) for p in visible.get("lab_panels") or []):
        return True
    if any(_in_window(s) for s in visible.get("studies") or []):
        return True
    return any(_in_window(n) and n.get("model_visible") is not False
               and not n.get("withheld_reason")
               and len(str(n.get("text") or "").strip()) >= 40
               for n in visible.get("notes") or [])


def interval_content_blockers(visible: Dict[str, Any], encounter: Dict[str, Any],
                              index_offset: int) -> List[str]:
    """The content floor for an INTERVAL point. Lighter than the decision-point
    floor by design: a follow-up visit is graded by the next encounter exactly as
    a decision point is, but it is a checkpoint, not a workup, and is priced and
    exported as one (``point_class``)."""
    blockers: List[str] = []
    if not (visible.get("problem_list") or []) and not visible.get("problems_absent_on_record"):
        blockers.append("case has an empty problem_list")
    if not (visible.get("medications") or []) and not visible.get("medications_absent_on_record"):
        blockers.append("case has an empty medication list")
    if not has_encounter_observation(visible, encounter, index_offset):
        blockers.append("interval visit recorded no observation (lab, report, order or note) in its own window")
    try:
        assert_temporal_split(visible)
    except TemporalLeak as exc:
        blockers.append(str(exc))
    return blockers


def _hold_proposal(proposal: Dict[str, Any], reason: str, message: str) -> None:
    proposal["review_required"] = True
    proposal.setdefault("review_reasons", []).append({"reason": reason, "message": message})
    proposal["blockers"].append(message)
    proposal["generatable"] = False


async def plan_cases(
    case: Optional[Dict[str, Any]], *, max_cases: Optional[int] = None,
    min_gap_days: int = 7, specialty_hint: Optional[str] = None,
    derive_questions: bool = True, question_indices: Optional[Sequence[int]] = None,
    trajectory: bool = False, include_interval_points: bool = True,
) -> Dict[str, Any]:
    """One ingested chart → the full list of proposed cases, WITHOUT writing
    anything. This is what the admin dry-run returns and what generation iterates.

    Every encounter appears in the plan, including the ones that cannot become
    tasks — with the reason. An admin generating cases from a chart needs to see
    what was skipped and why; a plan that silently returns only the survivors is
    how a partner's chart quietly yields two cases instead of six.
    """
    c = prepare_longitudinal_chart(case)
    if not c:
        raise RealCaseError("empty case")

    encounters = segment_longitudinal_record(c, min_gap_days=min_gap_days)
    total_encounters = len(encounters)
    proposals: List[Dict[str, Any]] = []

    # Longitudinal Cases PRD §2 — the density gate and the verifiable set,
    # measured once for the whole chart so the plan can report BOTH numbers: how
    # many encounters are decision points, and how many of those have a later
    # decision point to be checked against. An admin pricing a chart walk needs the
    # second number, and it is always the first minus one.
    verifiable_pairs = pair_decision_points(c, encounters)
    verifiable_indices = {p["decision_index"] for p in verifiable_pairs}

    # Point classes (Chart Walk PRD §3). ``decision`` = the §2 density gate,
    # unchanged. ``interval`` = a dated contact between decision points that
    # recorded an observation; it is a checkpoint the next point grades, never a
    # substitute for a decision point, and it is never terminal. The gate is not
    # lowered: the two classes are counted, priced and exported separately.
    point_class: Dict[int, Optional[str]] = {}
    for enc in encounters:
        d = qualify_encounter(c, enc)
        if d["qualifies"]:
            point_class[enc["index"]] = "decision"
        elif trajectory and include_interval_points and not enc.get("undated") \
                and enc.get("n_events", 0) >= 1:
            point_class[enc["index"]] = "interval"
        else:
            point_class[enc["index"]] = None
    # An interval visit with no later point has nothing to grade it. A density-
    # qualifying encounter may close the walk even when it is later downgraded to
    # interval class for lack of a presenting narrative: it is a real encounter,
    # and the walk's terminal point is sealed rather than graded either way.
    last_decision = max((e["index"] for e in encounters if point_class.get(e["index"]) == "decision"), default=None)
    for enc in encounters:
        if point_class.get(enc["index"]) == "interval" and (last_decision is None or enc["index"] > last_decision):
            point_class[enc["index"]] = None
    point_indices = [e["index"] for e in encounters if point_class.get(e["index"])]
    walk_verifiable = {i for i in point_indices if any(j > i for j in point_indices)}

    for enc in encounters:
        index_offset, index_rationale = select_decision_point(c, enc)
        density = qualify_encounter(c, enc)
        proposal: Dict[str, Any] = {
            "encounter_index": enc["index"],
            "encounter_span": [enc["start_offset"], enc["end_offset"]],
            "n_events": enc.get("n_events", 0),
            "index_event_offset": index_offset,
            "index_rationale": index_rationale,
            # §2: reported on every proposal, passing or not, WITH the measurements.
            # 34 of the 59 encounters across the four real charts fail this gate,
            # and an admin looking at a chart that yielded 3 points out of 17 needs
            # to see which threshold each one missed rather than only that it did.
            "density": density,
            "qualifies_as_decision_point": density["qualifies"],
            "outcome_verifiable": (enc["index"] in walk_verifiable) if trajectory
                                  else (enc["index"] in verifiable_indices),
            "blockers": [],
        }
        if trajectory:
            proposal.update(point_class=point_class.get(enc["index"]),
                            qualifies_as_point=bool(point_class.get(enc["index"])))
        if trajectory and point_class.get(enc["index"]) is None and not density["qualifies"]:
            proposal["blockers"].append(
                "not a walk point: " + ("terminal interval visit has no later encounter to grade it"
                                        if enc.get("n_events", 0) >= 1 else "no recorded observation"))
        if index_offset is None:
            proposal["blockers"].append(
                "no decision point: " + str(index_rationale.get("reason")))
            proposals.append(proposal)
            continue

        later = [e for e in encounters if e["index"] > enc["index"]
                 and (point_class.get(e["index"]) if trajectory else qualify_encounter(c, e)["qualifies"])]
        until_offset = select_decision_point(c, later[0])[0] if later else max(_timed_offsets(c), default=index_offset)
        visible, held_out, stats = build_encounter_case(
            c, enc, index_offset, until_offset=until_offset, trajectory=trajectory,
            encounters=encounters)
        specialty, confidence, scores = infer_specialty(visible)
        if specialty_hint and is_enabled(specialty_hint):
            # An admin who has set the specialty on the upload outranks the
            # inference — they have seen the chart.
            specialty, confidence = specialty_hint, max(confidence, 1.0)
        visible["specialty"] = specialty or visible.get("specialty") or "general"
        bucket_id, subtopic = classify_case_to_bucket(visible, specialty)

        # The decisive trend's span, in encounters — the longitudinal axis of the
        # structural difficulty prior.
        spanned = sum(1 for e in encounters if e["start_offset"] <= index_offset)
        difficulty = score_difficulty(visible, encounters_spanned=spanned,
                                      bucket_id=bucket_id)

        proposal.update({
            "specialty": specialty,
            "specialty_confidence": confidence,
            "specialty_scores": scores,
            "taxonomy_bucket": bucket_id,
            "subtopic": subtopic,
            "encounters_spanned": spanned,
            "case": visible,
            "held_out": held_out,
            "curation": stats,
            "difficulty": difficulty,
            "case_type": case_type_signature(visible),
            "decision_offset_days": 0,      # the case is re-based on its own index
        })
        if trajectory and point_class.get(enc["index"]) == "interval":
            proposal["blockers"].extend(interval_content_blockers(visible, enc, index_offset))
        else:
            proposal["blockers"].extend(_content_blockers(visible))
        if specialty is None:
            proposal["blockers"].append(
                "specialty not served: this chart's signal does not clear the "
                f"confidence floor for any enabled specialty (best {confidence:.2f} "
                f"of {sorted(scores)}) — an admin must set it")
        proposal["generatable"] = not proposal["blockers"]
        # A decision point is a presentation plus a decision. An encounter that
        # clears the density gate but carries no presenting narrative (labs and
        # reports only) is still a real contact the next encounter grades, so it
        # stays in the walk as an INTERVAL point rather than being held. The
        # downgrade is recorded on the proposal and the export, never silent.
        narrative = has_encounter_narrative(visible, enc, index_offset)
        if trajectory:
            proposal["presenting_narrative"] = narrative
        if trajectory and point_class.get(enc["index"]) == "decision" and not narrative:
            point_class[enc["index"]] = "interval"
            proposal["point_class"] = "interval"
            proposal["qualifies_as_decision_point"] = False
            proposal["downgraded"] = ("no presenting narrative from this encounter: labs and reports only. "
                                      "Kept as an interval point; upload the admission or progress notes to "
                                      "restore it as a decision point.")
            # re-run the class-appropriate floor
            proposal["blockers"] = [b for b in proposal["blockers"] if b not in _content_blockers(visible)]
            proposal["blockers"].extend(interval_content_blockers(visible, enc, index_offset))
            proposal["generatable"] = not proposal["blockers"]
        proposals.append(proposal)

    # No backward hold propagation: every point's answer key and reveal window are
    # sealed at generation against ITS OWN bounded window (``until_offset``), so a
    # later point that fails to generate cannot widen an earlier point's reveal.

    # Authoring a question is the ONE plan step that costs a model call, so it is
    # scoped: generating a single encounter must not author six questions.
    wanted_q = set(question_indices) if question_indices is not None else None
    if derive_questions:
        for p in proposals:
            if not p.get("generatable"):
                continue
            if wanted_q is not None and p["encounter_index"] not in wanted_q:
                continue
            q, src = await derive_clinical_question(
                p["case"], p["held_out"], p.get("specialty"))
            p["question"], p["question_source"] = q, src

    generatable = [p for p in proposals if p.get("generatable")]
    if max_cases is not None and max_cases > 0:
        # Cap the GENERATABLE set, never the plan: an admin must still see the
        # encounters that were skipped and why. Silently truncating a plan reads as
        # "this chart only had two decision points".
        keep = {id(p) for p in generatable[:max_cases]}
        for p in generatable[max_cases:]:
            p["generatable"] = False
            p["blockers"].append(f"beyond max_cases={max_cases}")
        generatable = [p for p in generatable if id(p) in keep]

    result = {
        "encounters": total_encounters,
        "proposals": proposals,
        "generatable": len(generatable),
        "review_required_points": sum(bool(p.get("review_required")) for p in proposals
                                      if p.get("qualifies_as_point" if trajectory else "qualifies_as_decision_point")),
        "ready_decision_points": sum(bool(p.get("generatable")) for p in proposals
                                     if p.get("qualifies_as_decision_point")),
        "min_gap_days": min_gap_days,
        "why": "no model was called: 0 encounters cleared the gate" if not generatable else None,
        "omitted_implausible_dates": sum(n.get("withheld_reason") == "implausible_date" for n in c.get("notes") or []),
        # §2 — the two numbers a chart walk is priced on, stated separately because
        # they are different facts. ``decision_points`` is what clears the density
        # gate; ``verifiable_decision_points`` is how many of those the record can
        # actually check, which is always one fewer (the last has no later
        # qualifying encounter). Measured across the four real charts: 25 and 21.
        "decision_points": sum(1 for p in proposals if p.get("qualifies_as_decision_point")),
        "verifiable_decision_points": len(verifiable_pairs),
        "density_gate": {
            "min_distinct_dates": ENCOUNTER_MIN_DISTINCT_DATES,
            "min_events": ENCOUNTER_MIN_EVENTS,
            "min_resource_types": ENCOUNTER_MIN_RESOURCE_TYPES,
        },
    }
    if trajectory:
        result.update(
            interval_points=sum(1 for p in proposals if p.get("point_class") == "interval"),
            walk_points=sum(1 for p in proposals if p.get("qualifies_as_point")),
            walk_verifiable_points=sum(1 for p in proposals if p.get("qualifies_as_point") and p.get("outcome_verifiable")),
            ready_walk_points=sum(bool(p.get("generatable")) for p in proposals if p.get("qualifies_as_point")),
            downgraded_points=sum(1 for p in proposals if p.get("downgraded")),
        )
    return result
