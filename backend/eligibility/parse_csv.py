"""CSV eligibility parser.

Uses stdlib csv + difflib for header alias resolution. Per PRD §7.1:
- 8 canonical columns (see COLUMN_ALIASES).
- If the best match for any canonical key scores < 0.8, the CSV is marked
  ``needs_llm=True`` and we hand the raw text to the extractor instead.
- Multi-patient CSVs are split on the ``mbi`` (or best match) column.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional

# Canonical field → accepted header spellings (case-insensitive, whitespace/_ normalized).
COLUMN_ALIASES: Dict[str, List[str]] = {
    "partA_eff": ["part_a_effective", "medicare_a_start", "parta_eff_dt", "part a eff", "medA_start"],
    "partA_term": ["part_a_term", "medicare_a_end", "parta_end"],
    "partB_eff": ["part_b_effective", "medicare_b_start", "partb_eff_dt", "part b eff"],
    "partB_term": ["part_b_term", "medicare_b_end"],
    "ma_plan_id": ["ma_plan", "maplanid", "ma_contract", "mapd_plan"],
    "msp_indicator": ["msp", "secondary_payer", "primary_payer"],
    "esrd_indicator": ["esrd", "esrd_basis"],
    "umwa_indicator": ["umwa"],
    # Identity
    "mbi": ["mbi", "medicare_beneficiary_identifier", "medicare_id", "hicn"],
    "first_name": ["first_name", "fname", "given_name", "firstname"],
    "last_name": ["last_name", "lname", "surname", "lastname"],
    "dob": ["dob", "date_of_birth", "birthdate", "birth_date"],
    "surgery_date": ["surgery_date", "procedure_date", "scheduled_surgery_date", "dos"],
}

SIMILARITY_THRESHOLD = 0.8


@dataclass
class CSVParseResult:
    headers: List[str] = field(default_factory=list)
    rows: List[Dict[str, str]] = field(default_factory=list)
    resolved: Dict[str, str] = field(default_factory=dict)  # canonical → actual header
    needs_llm: bool = False
    raw_text: str = ""
    row_count: int = 0

    def to_dict(self) -> dict:
        return {
            "headers": self.headers,
            "rows": self.rows,
            "resolved": self.resolved,
            "needs_llm": self.needs_llm,
            "row_count": self.row_count,
        }


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower().strip() if ch.isalnum())


def _best_match(target_aliases: List[str], headers: List[str]) -> tuple[Optional[str], float]:
    """Return (matching_header, similarity_score) for the best alias hit."""
    best_header: Optional[str] = None
    best_score = 0.0
    norm_targets = [_norm(a) for a in target_aliases]
    for h in headers:
        nh = _norm(h)
        for nt in norm_targets:
            if not nh or not nt:
                continue
            score = SequenceMatcher(None, nh, nt).ratio()
            if score > best_score:
                best_score = score
                best_header = h
    return best_header, best_score


def parse_csv(raw: bytes | str) -> CSVParseResult:
    """Parse CSV bytes/str into rows + resolved canonical column map.

    Never raises on malformed CSV — returns needs_llm=True with raw_text set so
    the extractor can see the whole file.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    result = CSVParseResult(raw_text=text)

    # csv.Sniffer handles tab-separated too
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect=dialect)
    try:
        headers = next(reader)
    except StopIteration:
        result.needs_llm = True
        return result

    result.headers = [h.strip() for h in headers]

    # Resolve canonical → actual header
    any_missing = False
    for canonical, aliases in COLUMN_ALIASES.items():
        hdr, score = _best_match(aliases + [canonical], result.headers)
        if hdr and score >= SIMILARITY_THRESHOLD:
            result.resolved[canonical] = hdr
        else:
            any_missing = True

    # Parse rows regardless — even if we fall through, having dicts helps the LLM
    rows: List[Dict[str, str]] = []
    for raw_row in reader:
        if not raw_row or all(not (c or "").strip() for c in raw_row):
            continue
        row = {
            result.headers[i]: (raw_row[i].strip() if i < len(raw_row) else "")
            for i in range(len(result.headers))
        }
        rows.append(row)
    result.rows = rows
    result.row_count = len(rows)

    # Mark needs_llm when required Medicare columns (Part A/B, MA, MSP/ESRD/UMWA)
    # couldn't all be resolved confidently.
    required_medicare = {"partA_eff", "partB_eff", "ma_plan_id"}
    resolved_keys = set(result.resolved.keys())
    if not required_medicare.issubset(resolved_keys):
        result.needs_llm = True
    if any_missing and not result.resolved:
        result.needs_llm = True

    return result


def split_by_mbi(result: CSVParseResult) -> List[List[Dict[str, str]]]:
    """Split multi-patient CSVs by MBI column (PRD §5.3). Each group returns a
    list of rows belonging to a single patient.

    Falls back to one group per row if the MBI column couldn't be resolved.
    """
    mbi_col = result.resolved.get("mbi")
    if not mbi_col:
        return [[r] for r in result.rows]
    groups: Dict[str, List[Dict[str, str]]] = {}
    for r in result.rows:
        key = (r.get(mbi_col) or "").strip()
        groups.setdefault(key or f"__unkeyed_{len(groups)}", []).append(r)
    return list(groups.values())


def format_for_llm(result: CSVParseResult, filename: Optional[str] = None) -> str:
    header_bits = f"=== CSV PARSED (rows={result.row_count}, needs_llm={result.needs_llm}) ==="
    if filename:
        header_bits += f"  file={filename}"
    lines = [header_bits, f"Headers: {', '.join(result.headers)}"]
    if result.resolved:
        lines.append("Resolved columns:")
        for canonical, actual in sorted(result.resolved.items()):
            lines.append(f"  {canonical} ← {actual}")
    # Cap rows shown to LLM at 25
    for i, row in enumerate(result.rows[:25], 1):
        pretty = " | ".join(f"{k}={v}" for k, v in row.items() if v)
        lines.append(f"Row {i}: {pretty}")
    if result.row_count > 25:
        lines.append(f"... ({result.row_count - 25} more rows)")
    return "\n".join(lines)
