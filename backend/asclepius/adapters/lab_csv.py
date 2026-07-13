"""CSV / TSV lab-results adapter.

Parses a flat table of lab results (one analyte per row) into grouped LabPanels.
Columns are matched by LIBERAL, case-insensitive, strip/underscore-insensitive
fuzzy aliases. Delimiter (comma vs tab) is auto-detected.

Rows are grouped into panels by ``(patient_key, panel, collected_at)``. A row
missing analyte or value is skipped (never crashes). Empty / headerless input
raises :class:`CaseIngestError`.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Optional

from asclepius.case_formats import CaseIngestError

from ._common import to_text

# Canonical field -> the set of accepted header aliases (already normalized:
# lowercased, non-alphanumerics stripped).
_ALIASES: Dict[str, set] = {
    "patient_key": {"patientkey", "patientid", "patient", "subjectid", "mrnkey", "pid"},
    "panel": {"panel", "panelname", "battery", "orderpanel", "test_panel", "testpanel", "group"},
    "analyte": {"analyte", "test", "testname", "name", "component", "observation", "measure"},
    "loinc": {"loinc", "loinccode", "code"},
    "value": {"value", "result", "resultvalue", "observedvalue", "measurement", "val"},
    "unit": {"unit", "units", "uom", "unitofmeasure"},
    "ref_low": {"reflow", "low", "rangelow", "referencelow", "refrangelow", "lowref", "normallow"},
    "ref_high": {"refhigh", "high", "rangehigh", "referencehigh", "refrangehigh", "highref", "normalhigh"},
    "flag": {"flag", "abnormalflag", "interpretation", "abnormal", "interp"},
    "collected_at": {
        "collectedat", "collected", "date", "drawn", "drawndate", "collectiondate",
        "observationdate", "collectiontime", "specimencollected", "datecollected",
    },
}


def _norm_key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").strip().lower())


def _build_column_map(fieldnames: List[str]) -> Dict[str, str]:
    """Map each canonical field to the actual header present in the file."""
    colmap: Dict[str, str] = {}
    for original in fieldnames:
        norm = _norm_key(original)
        for canonical, aliases in _ALIASES.items():
            if canonical in colmap:
                continue
            if norm == canonical or norm in aliases:
                colmap[canonical] = original
                break
    return colmap


def _sniff_delimiter(text: str) -> str:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        return dialect.delimiter
    except csv.Error:
        # Fallback heuristic: pick whichever of tab/comma appears more in the
        # header line.
        first = sample.splitlines()[0] if sample.splitlines() else ""
        if first.count("\t") > first.count(","):
            return "\t"
        return ","


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip()
    return v or None


def _coerce_number(value: Optional[str]) -> Any:
    """Return a float/int when the string is purely numeric, else the raw string
    (labs can be qualitative, e.g. "muddy-brown casts")."""
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        f = float(s)
        return f
    except ValueError:
        return s


def parse(raw, *, specialty: str = "general") -> dict:
    text = to_text(raw)
    if not text.strip():
        raise CaseIngestError("lab_csv: empty input")

    delimiter = _sniff_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise CaseIngestError("lab_csv: no header row / headerless input")

    colmap = _build_column_map(list(reader.fieldnames))
    # Need at least an analyte and value column to be a lab table.
    if "analyte" not in colmap or "value" not in colmap:
        raise CaseIngestError(
            "lab_csv: could not identify analyte/value columns from header "
            f"{list(reader.fieldnames)!r}"
        )

    def get(row: Dict[str, Any], field: str) -> Optional[str]:
        col = colmap.get(field)
        return _clean(row.get(col)) if col else None

    # (patient_key, panel, collected_at) -> panel dict
    panels: "Dict[tuple, Dict[str, Any]]" = {}
    order: List[tuple] = []
    patient_keys: List[str] = []

    for row in reader:
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank line
        analyte = get(row, "analyte")
        value = get(row, "value")
        if not analyte or value is None:
            continue  # skip incomplete rows, don't crash

        patient_key = get(row, "patient_key")
        if patient_key and patient_key not in patient_keys:
            patient_keys.append(patient_key)
        panel_name = get(row, "panel") or "Labs"
        collected_at = get(row, "collected_at")

        key = (patient_key, panel_name, collected_at)
        if key not in panels:
            panels[key] = {
                "panel": panel_name,
                "collected_at": collected_at,
                "results": [],
            }
            order.append(key)

        panels[key]["results"].append({
            "analyte": analyte,
            "loinc": get(row, "loinc"),
            "value": _coerce_number(value),
            "unit": get(row, "unit"),
            "ref_low": _coerce_number(get(row, "ref_low")),
            "ref_high": _coerce_number(get(row, "ref_high")),
            "flag": (get(row, "flag") or ""),
        })

    lab_panels = [panels[k] for k in order if panels[k]["results"]]
    if not lab_panels:
        raise CaseIngestError("lab_csv: no parseable lab rows found")

    patient_key = patient_keys[0] if len(patient_keys) == 1 else (patient_keys[0] if patient_keys else None)

    return {
        "specialty": specialty,
        "patient_key": patient_key,
        "lab_panels": lab_panels,
    }
