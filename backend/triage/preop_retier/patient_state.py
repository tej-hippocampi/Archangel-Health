"""
Helpers for the pre-op-re-tier-relevant fields on the in-memory patient blob.

The pre-op re-tier stage extends the same `app.state.patient_store` dict
the initial-tier router already manages. It adds a small set of
denormalized fields used by `triage.preop_retier.algo` and rendered on
the doctor / admin queue.

Per the Option B / event-stream architecture (see
`backend/team_store.py` top-of-file note), these fields are live-source
of truth on the patient blob and are *also* persisted as snapshot rows
in `preop_retier_events`. Reads go to the blob; audits go to the table.

Fields (the two algorithm-guard fields are documented in detail in the
top-level comment of `team_store.py`):

    initial_tier                          : Tier — algorithmic outcome
                                             of the initial-tier router
    initial_tier_was_hard_escalator       : bool — sticky-hard guard
                                             input for the re-tier algo
    current_tier                          : Tier — live tier in effect
    preop_retier_last_run_at              : ISO timestamp denormalized
                                             for queue rendering
    preop_retier_last_delta               : signed int snapshot
    preop_retier_top_reasons              : top-3 reasons for queue
    preop_retier_last_tier                : last tier_after we wrote
    preop_retier_version                  : algo MODEL_VERSION
    preop_retier_tuning_version           : algo TUNING_VERSION
    preop_retier_initial_tier_was_hard    : copy of the guard flag at
                                             the time of last run
"""

from __future__ import annotations

from typing import Any, Optional

from triage.types import Tier


_VALID_TIERS = ("TIER_1", "TIER_2", "TIER_3")


def ensure_preop_retier_patient_state(patient: dict) -> dict:
    """Mutate the patient blob in place so it has every pre-op re-tier
    field we depend on. Idempotent. Returns the patient dict for chaining.
    """
    patient.setdefault("initial_tier", None)
    patient.setdefault("initial_tier_score", None)
    patient.setdefault("initial_tier_was_hard_escalator", False)
    patient.setdefault("initial_tier_assigned_at", None)
    patient.setdefault("initial_tier_input_snapshot", None)
    patient.setdefault("initial_tier_reasons", None)
    patient.setdefault("initial_tier_model_version", None)
    patient.setdefault("initial_tier_tuning_version", None)
    patient.setdefault("initial_tier_override", None)
    patient.setdefault("initial_tier_override_reason", None)
    patient.setdefault("initial_tier_override_by", None)
    patient.setdefault("initial_tier_override_at", None)

    patient.setdefault("preop_retier_last_run_at", None)
    patient.setdefault("preop_retier_last_delta", None)
    patient.setdefault("preop_retier_top_reasons", None)
    patient.setdefault("preop_retier_last_tier", None)
    patient.setdefault("preop_retier_version", None)
    patient.setdefault("preop_retier_tuning_version", None)
    patient.setdefault("preop_retier_initial_tier_was_hard", None)

    if patient.get("current_tier") in (None, ""):
        if patient.get("initial_tier") in _VALID_TIERS:
            patient["current_tier"] = patient["initial_tier"]

    return patient


def get_initial_tier(patient: dict) -> Optional[Tier]:
    ensure_preop_retier_patient_state(patient)
    val = patient.get("initial_tier")
    return val if val in _VALID_TIERS else None  # type: ignore[return-value]


def get_initial_tier_was_hard_escalator(patient: dict) -> bool:
    ensure_preop_retier_patient_state(patient)
    return bool(patient.get("initial_tier_was_hard_escalator"))


def update_preop_retier_denorm(
    patient: dict,
    *,
    last_run_at: str,
    last_delta: int,
    top_reasons: list[dict[str, Any]],
    last_tier: Tier,
    model_version: str,
    tuning_version: int,
    initial_tier_was_hard: bool,
) -> None:
    """Denormalize the most recent re-tier outcome onto the patient blob
    for cheap rendering on the doctor / admin queue."""
    ensure_preop_retier_patient_state(patient)
    patient["preop_retier_last_run_at"] = last_run_at
    patient["preop_retier_last_delta"] = int(last_delta)
    patient["preop_retier_top_reasons"] = list(top_reasons[:3])
    patient["preop_retier_last_tier"] = last_tier
    patient["preop_retier_version"] = model_version
    patient["preop_retier_tuning_version"] = int(tuning_version)
    patient["preop_retier_initial_tier_was_hard"] = bool(initial_tier_was_hard)


def to_public(patient: dict) -> dict[str, Any]:
    """Snapshot of the pre-op-re-tier subset for API responses.
    Intentionally excludes input snapshots (rendered through the
    `preop_retier_events` audit endpoint instead)."""
    ensure_preop_retier_patient_state(patient)
    return {
        "initialTier":                   patient.get("initial_tier"),
        "initialTierScore":              patient.get("initial_tier_score"),
        "initialTierWasHardEscalator":   bool(patient.get("initial_tier_was_hard_escalator")),
        "initialTierAssignedAt":         patient.get("initial_tier_assigned_at"),
        "initialTierOverride":           patient.get("initial_tier_override"),
        "currentTier":                   patient.get("current_tier"),
        "preOpReTierLastRunAt":          patient.get("preop_retier_last_run_at"),
        "preOpReTierLastDelta":          patient.get("preop_retier_last_delta"),
        "preOpReTierTopReasons":         patient.get("preop_retier_top_reasons"),
        "preOpReTierLastTier":           patient.get("preop_retier_last_tier"),
        "preOpReTierVersion":            patient.get("preop_retier_version"),
        "preOpReTierTuningVersion":      patient.get("preop_retier_tuning_version"),
    }
