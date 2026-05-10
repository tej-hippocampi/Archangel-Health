"""Synthetic validation set for the deterministic evaluator (PRD §12.18).

50 hand-constructed cases. Each case provides:
- a synthetic ``extracted`` dict (as if returned by the LLM tool)
- the expected overall verdict per the rules in eval_mod.evaluate
"""

from __future__ import annotations

from typing import Any, Dict, List


def _coverage(active: bool, eff: str = "2020-01-01", term: str | None = None) -> Dict[str, Any]:
    return {
        "status": "ACTIVE" if active else "INACTIVE",
        "effectiveDate": eff,
        "terminationDate": term,
        "sourceExcerpt": "synthetic",
    }


def _ma(enrolled: str) -> Dict[str, Any]:
    return {"enrolled": enrolled, "sourceExcerpt": "synthetic"}


def _primary(is_primary: str) -> Dict[str, Any]:
    return {"isPrimary": is_primary, "sourceExcerpt": "synthetic"}


def _esrd(is_esrd: str) -> Dict[str, Any]:
    return {"isESRDBasis": is_esrd, "sourceExcerpt": "synthetic"}


def _umwa(is_umwa: str) -> Dict[str, Any]:
    return {"isUMWA": is_umwa, "sourceExcerpt": "synthetic"}


SURGERY = "2026-06-01"


def _make(
    *,
    id_: str,
    name: str,
    expected: str,
    partA: Dict[str, Any] | None = None,
    partB: Dict[str, Any] | None = None,
    ma: str = "NO",
    primary: str = "YES",
    esrd: str = "NO",
    umwa: str = "NO",
) -> Dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "surgery_date": SURGERY,
        "extracted": {
            "partA": partA or _coverage(True),
            "partB": partB or _coverage(True),
            "medicareAdvantage": _ma(ma),
            "medicarePrimary": _primary(primary),
            "esrdBasis": _esrd(esrd),
            "umwa": _umwa(umwa),
            "overallConfidence": "MEDIUM",
        },
        "expected_overall": expected,
    }


CASES: List[Dict[str, Any]] = []

# 10 simple ELIGIBLE cases (varying effective dates, all PASS)
for i in range(10):
    CASES.append(
        _make(
            id_=f"e{i + 1:03d}",
            name=f"Original Medicare, clean #{i + 1}",
            expected="ELIGIBLE",
            partA=_coverage(True, eff=f"20{15 + i}-01-01"),
            partB=_coverage(True, eff=f"20{15 + i}-01-01"),
        )
    )

# 8 MA-enrollment cases (INELIGIBLE)
for i in range(8):
    CASES.append(
        _make(
            id_=f"ma{i + 1:03d}",
            name=f"Medicare Advantage enrolled #{i + 1}",
            expected="INELIGIBLE",
            ma="YES",
        )
    )

# 6 ESRD cases (INELIGIBLE)
for i in range(6):
    CASES.append(
        _make(
            id_=f"esrd{i + 1:03d}",
            name=f"ESRD-basis entitlement #{i + 1}",
            expected="INELIGIBLE",
            esrd="YES",
        )
    )

# 4 UMWA cases (INELIGIBLE)
for i in range(4):
    CASES.append(
        _make(
            id_=f"umwa{i + 1:03d}",
            name=f"UMWA Health Plan #{i + 1}",
            expected="INELIGIBLE",
            umwa="YES",
        )
    )

# 4 Medicare-not-primary cases (INELIGIBLE)
for i in range(4):
    CASES.append(
        _make(
            id_=f"msp{i + 1:03d}",
            name=f"Medicare not primary (MSP) #{i + 1}",
            expected="INELIGIBLE",
            primary="NO",
        )
    )

# 4 termed-Part-A cases (INELIGIBLE)
for i in range(4):
    CASES.append(
        _make(
            id_=f"termA{i + 1:03d}",
            name=f"Part A terminated before surgery #{i + 1}",
            expected="INELIGIBLE",
            partA=_coverage(True, eff="2010-01-01", term=f"202{1 + i}-12-31"),
        )
    )

# 4 termed-Part-B cases (INELIGIBLE)
for i in range(4):
    CASES.append(
        _make(
            id_=f"termB{i + 1:03d}",
            name=f"Part B terminated before surgery #{i + 1}",
            expected="INELIGIBLE",
            partB=_coverage(True, eff="2010-01-01", term=f"202{1 + i}-12-31"),
        )
    )

# 5 UNKNOWN-bias cases (BLOCKED_UNKNOWN)
for i in range(5):
    CASES.append(
        _make(
            id_=f"u{i + 1:03d}",
            name=f"Mixed unknowns #{i + 1}",
            expected="BLOCKED_UNKNOWN",
            ma="UNKNOWN",
            esrd=("UNKNOWN" if i % 2 else "NO"),
            primary=("UNKNOWN" if i % 3 == 0 else "YES"),
        )
    )

# 5 edge: term==surgery (PRD §11.8) → still ELIGIBLE
for i in range(5):
    CASES.append(
        _make(
            id_=f"edge{i + 1:03d}",
            name=f"Coverage termed == surgery date #{i + 1}",
            expected="ELIGIBLE",
            partA=_coverage(True, eff="2010-01-01", term=SURGERY),
            partB=_coverage(True, eff="2010-01-01", term=SURGERY),
        )
    )

assert len(CASES) >= 50, f"need >=50 fixtures, have {len(CASES)}"
