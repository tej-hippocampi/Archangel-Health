"""``lab_csv`` adapter (EHR PRD §6) — a partner's lab-results CSV/TSV →
``lab_panels`` fragments. The ship-first on-ramp: liberal in what we accept
(fuzzy header aliases + an optional explicit column map), strict in what we emit.

Expected (canonical) columns — any order, any casing, aliases below:
    patient_key, panel, analyte, loinc, value, unit, ref_low, ref_high, flag,
    collected_at
Only ``analyte`` + ``value`` are required per row; everything else degrades.
Rows group into one ``LabPanel`` per (panel, collected_at). ``collected_at``
stays a RAW date string here — ``timeline.normalize_timeline`` converts it.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.adapters.lab_csv")

# canonical field -> accepted header aliases (lowercase, punctuation-stripped).
_ALIASES: Dict[str, tuple] = {
    "patient_key": ("patient_key", "patientkey", "patient_id", "patientid", "subject", "subject_id", "mrn_key"),
    "panel": ("panel", "panel_name", "battery", "order_name", "test_panel", "profile"),
    "analyte": ("analyte", "test", "component", "test_name", "observation", "result_name", "lab_test"),
    "loinc": ("loinc", "loinc_code", "loinc_num", "code"),
    "value": ("value", "result", "result_value", "observation_value", "numeric_value"),
    "unit": ("unit", "units", "uom", "result_units"),
    "ref_low": ("ref_low", "reference_low", "low", "range_low", "normal_low", "ref_range_low"),
    "ref_high": ("ref_high", "reference_high", "high", "range_high", "normal_high", "ref_range_high"),
    # A single combined range column ("1.9 - 2.5", "Up to 0.4", "< 7.90") is what
    # real hospital exports actually ship — split below into ref_low/ref_high.
    "ref_range": ("ref_range", "reference_range", "referencerange", "normal_range",
                  "normal_ranges", "range", "reference_interval", "ref_interval",
                  "biological_reference_interval"),
    "flag": ("flag", "abnormal_flag", "abnormal", "interpretation", "result_flag"),
    # ``service_date`` was the real partner's column name and matched NOTHING, so
    # every panel in a 14-month chart collapsed to ``collected_offset_days: 0`` —
    # the entire timeline silently lost, while the file still ingested clean.
    "collected_at": ("collected_at", "collection_date", "collected", "drawn", "drawn_at",
                     "specimen_date", "collection_datetime", "observation_date", "result_date",
                     "service_date", "servicedate", "date", "test_date", "report_date",
                     "resulted_at", "sample_date", "specimen_collected_at", "obs_date",
                     "performed_date", "encounter_date"),
}

_VALID_FLAGS = {"", "L", "H", "LL", "HH"}

# "1.9 - 2.5" · "1.9-2.5" · "10 to 50" · "< 7.90" · "Up to 0.4" · "> 60" · "≥ 5".
# Anything else (the prose ranges a photographed report carries, e.g.
# "Male: 270; Female: 240; Children < 5 Yr: 60-321") is deliberately NOT guessed
# at — a wrong reference range makes a normal value read as abnormal.
_RANGE_PAIR_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(-?\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
_RANGE_UPPER_RE = re.compile(r"^\s*(?:<|<=|≤|up\s+to|upto|less\s+than)\s*(-?\d+(?:\.\d+)?)\s*$",
                             re.IGNORECASE)
_RANGE_LOWER_RE = re.compile(r"^\s*(?:>|>=|≥|greater\s+than|above)\s*(-?\d+(?:\.\d+)?)\s*$",
                             re.IGNORECASE)


def _num_or_none(s: str) -> Any:
    try:
        return int(s) if s.lstrip("+-").isdigit() else float(s)
    except (TypeError, ValueError):
        return None


def split_reference_range(raw: Any) -> tuple:
    """A combined reference-range cell → ``(ref_low, ref_high)``; ``(None, None)``
    when the cell is prose we refuse to interpret."""
    s = str(raw or "").strip()
    if not s:
        return None, None
    m = _RANGE_PAIR_RE.match(s)
    if m:
        return _num_or_none(m.group(1)), _num_or_none(m.group(2))
    m = _RANGE_UPPER_RE.match(s)
    if m:
        return None, _num_or_none(m.group(1))
    m = _RANGE_LOWER_RE.match(s)
    if m:
        return _num_or_none(m.group(1)), None
    return None, None


class LabCsvError(ValueError):
    """The CSV cannot be interpreted as lab results (no mappable headers /
    no usable rows). The bundle entry should quarantine with this reason."""


def _norm_header(h: str) -> str:
    return "".join(c for c in (h or "").strip().lower() if c.isalnum() or c == "_")


def _build_column_map(headers: List[str], override: Optional[Dict[str, str]]) -> Dict[str, str]:
    """canonical field -> actual header. ``override`` (from the partner manifest
    or the admin column-mapping UI) wins over the fuzzy alias table."""
    norm = {_norm_header(h): h for h in headers}
    out: Dict[str, str] = {}
    for field, aliases in _ALIASES.items():
        if override and override.get(field):
            if override[field] in headers:
                out[field] = override[field]
                continue
        for a in aliases:
            if a in norm:
                out[field] = norm[a]
                break
    return out


def _norm_flag(raw: Any) -> str:
    f = str(raw or "").strip().upper()
    if f in _VALID_FLAGS:
        return f
    # Common interpretation spellings → HL7-style flags.
    return {"LOW": "L", "HIGH": "H", "CRITICAL LOW": "LL", "CRITICAL HIGH": "HH",
            "PANIC LOW": "LL", "PANIC HIGH": "HH", "ABNORMAL": "", "NORMAL": "", "N": ""}.get(f, "")


def _num(raw: Any) -> Any:
    """Numeric when it parses, else the original string (e.g. 'muddy-brown casts').
    Non-finite floats ("nan"/"inf") stay strings — NaN survives json round-trips
    into case_json and then 500s every API response that serializes the case
    (review finding)."""
    import math
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return None
    try:
        if s.lstrip("+-").isdigit():
            return int(s)
        v = float(s)
        return v if math.isfinite(v) else s
    except ValueError:
        return s


def parse(raw: Any, *, specialty: str = "general", manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """CSV/TSV text (str or bytes) → ``{"lab_panels": [...], "_patient_keys": [...]}``
    fragments. Raises ``LabCsvError`` when nothing lab-shaped can be read."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    if not text.strip():
        raise LabCsvError("empty CSV")
    dialect_delim = "\t" if ("\t" in text.splitlines()[0] and "," not in text.splitlines()[0]) else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=dialect_delim)
    headers = reader.fieldnames or []
    colmap = _build_column_map(headers, (manifest or {}).get("column_map"))
    if "analyte" not in colmap or "value" not in colmap:
        raise LabCsvError(
            "could not map lab columns (need at least an analyte/test column and a "
            f"value/result column; headers seen: {', '.join(headers[:12])})"
        )

    def cell(row: Dict[str, Any], field: str) -> Optional[str]:
        h = colmap.get(field)
        v = row.get(h) if h else None
        return v.strip() if isinstance(v, str) else v

    # A CSV with no recognisable date column is NOT a clean ingest. Every panel
    # collapses to day 0, the whole longitudinal trend disappears, and the file
    # still reports success — which is exactly how a 14-month chart arrived as one
    # undated blob. Say so loudly, in the log AND on the fragment, so the admin's
    # column-mapping UI has something to show instead of a green row.
    warnings: List[str] = []
    if "collected_at" not in colmap:
        msg = ("no collection-date column matched "
               f"(headers seen: {', '.join(headers[:12])}); every panel will collapse "
               "to a single undated timepoint and the trend will be lost — map the "
               "date column explicitly via manifest.column_map.collected_at")
        warnings.append(msg)
        log.warning("lab_csv: %s", msg)

    # Group rows → one panel per (patient_key, panel name, collected_at).
    panels: Dict[tuple, Dict[str, Any]] = {}
    patient_keys: List[str] = []
    rows_used = 0
    unparsed_ranges = 0
    for row in reader:
        analyte = cell(row, "analyte")
        if not analyte:
            continue
        value = _num(cell(row, "value"))
        if value is None:
            continue
        pk = cell(row, "patient_key") or "default"
        if pk not in patient_keys:
            patient_keys.append(pk)
        panel_name = cell(row, "panel") or "Labs"
        collected = cell(row, "collected_at") or ""
        key = (pk, panel_name, collected)
        panel = panels.setdefault(key, {
            "panel": panel_name, "results": [],
            **({"collected_at": collected} if collected else {"collected_offset_days": 0}),
            "_patient_key": pk,
        })
        result: Dict[str, Any] = {"analyte": analyte, "value": value}
        if cell(row, "loinc"):
            result["loinc"] = cell(row, "loinc")
        if cell(row, "unit"):
            result["unit"] = cell(row, "unit")
        lo, hi = _num(cell(row, "ref_low")), _num(cell(row, "ref_high"))
        if lo is None and hi is None and colmap.get("ref_range"):
            lo, hi = split_reference_range(cell(row, "ref_range"))
            if lo is None and hi is None and cell(row, "ref_range"):
                unparsed_ranges += 1
        if lo is not None:
            result["ref_low"] = lo
        if hi is not None:
            result["ref_high"] = hi
        result["flag"] = _norm_flag(cell(row, "flag"))
        panel["results"].append(result)
        rows_used += 1

    if rows_used == 0:
        raise LabCsvError("no usable lab rows (every row missing analyte or value)")
    # The grouping key must NOT survive inside the panel (review finding: a
    # numeric pseudonymous key would false-trip the deidentify long-number scan
    # and kill the whole case). It lives only in the top-level _patient_keys.
    out_panels = []
    for panel in panels.values():
        panel = dict(panel)
        panel.pop("_patient_key", None)
        out_panels.append(panel)
    if unparsed_ranges:
        msg = (f"{unparsed_ranges} reference-range cell(s) were prose we refuse to "
               "interpret (e.g. per-sex or per-age bands); those results carry no "
               "ref_low/ref_high rather than a guessed one")
        warnings.append(msg)
        log.info("lab_csv: %s", msg)
    frag: Dict[str, Any] = {"lab_panels": out_panels, "_patient_keys": patient_keys}
    if warnings:
        # Underscore-prefixed: fragment metadata, stripped before the case body —
        # it must never reach a model-visible field.
        frag["_adapter_warnings"] = warnings
    return frag
