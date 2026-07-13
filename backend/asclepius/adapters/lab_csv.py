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
from typing import Any, Dict, List, Optional

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
    "flag": ("flag", "abnormal_flag", "abnormal", "interpretation", "result_flag"),
    "collected_at": ("collected_at", "collection_date", "collected", "drawn", "drawn_at",
                     "specimen_date", "collection_datetime", "observation_date", "result_date"),
}

_VALID_FLAGS = {"", "L", "H", "LL", "HH"}


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

    # Group rows → one panel per (patient_key, panel name, collected_at).
    panels: Dict[tuple, Dict[str, Any]] = {}
    patient_keys: List[str] = []
    rows_used = 0
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
    return {"lab_panels": out_panels, "_patient_keys": patient_keys}
