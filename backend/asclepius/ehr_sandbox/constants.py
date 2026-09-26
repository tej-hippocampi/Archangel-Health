"""Versioned policy and the single audit-clock boundary for ENV-EHR."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

ANCHOR = date(2031, 3, 3)
DECISION_INSTANT = "2031-03-03T09:00:00-08:00"
OFFSET_URL = "https://archangel.health/fhir/StructureDefinition/offsetDays"
CHECKPOINTS = ("retrieve", "reason", "act", "document")
EHR_TABLES = (
    "ehr_charts", "ehr_visits", "ehr_tasks", "ehr_rollouts", "ehr_checkpoints",
    "ehr_reviews", "ehr_review_rollouts", "ehr_review_assignments",
    "ehr_review_verdicts", "ehr_verdict_cache", "ehr_key_corrections",
    "ehr_source_exclusions",
)


def audit_now() -> str:
    """Wall time is for audit records only; clinical time comes from the task."""
    from asclepius.store import _utcnow_iso
    return _utcnow_iso()


@dataclass(frozen=True)
class Settings:
    env_version: str = "neph-ehr-1.1.0"
    key_min_confidence: float = 0.80
    budget_tool_calls: int = 40
    checkpoint_weights: tuple[float, ...] = (0.10, 0.20, 0.50, 0.20)
    rubric_sample_rate: float = 0.10
    review_pay_cents: int = 2500
    review_daily_cap: int = 150
    review_max_open: int = 8
    review_due_hours: int = 72
    harness_concurrency: int = 4
    export_blocked_jurisdictions: tuple[str, ...] = ("CN", "HK", "MO", "RU", "IR", "KP", "CU", "VE")
    ocr_enabled: str = "auto"


def settings() -> Settings:
    """Read per operation, so realm/test settings cannot be cached on import."""
    import math
    values = {}
    for field, default in Settings().__dict__.items():
        raw = os.getenv("EHR_" + field.upper())
        if raw is None:
            continue
        if field == "checkpoint_weights":
            value = tuple(float(x.strip()) for x in raw.split(","))
            if len(value) != 4 or any(not math.isfinite(x) or x < 0 for x in value) or abs(sum(value) - 1) > 1e-9:
                raise ValueError("EHR_CHECKPOINT_WEIGHTS must contain four nonnegative weights summing to 1")
        elif field == "export_blocked_jurisdictions":
            value = tuple(sorted(set(x.strip().upper() for x in raw.split(",") if x.strip())))
            if not value or any(len(x) != 2 or not x.isalpha() for x in value):
                raise ValueError("EHR_EXPORT_BLOCKED_JURISDICTIONS requires ISO-2 codes")
        else:
            value = type(default)(raw)
            if isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"Invalid EHR_{field.upper()}")
            if field in ("key_min_confidence", "rubric_sample_rate") and value > 1:
                raise ValueError(f"EHR_{field.upper()} must be between 0 and 1")
            if isinstance(default, int) and value < 1:
                raise ValueError(f"EHR_{field.upper()} must be positive")
        values[field] = value
    result = Settings(**values)
    if result.ocr_enabled not in ("auto", "0", "1"):
        raise ValueError("EHR_OCR_ENABLED must be auto, 0 or 1")
    return result
