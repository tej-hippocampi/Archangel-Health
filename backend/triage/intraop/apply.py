"""
`apply_intraop_reassessment` — the single tier-write path for the
intra-op pass (PRD §8.1).

Every code path that needs to materialize an intra-op tier change goes
through this function:

  - `POST /api/episodes/{id}/intraop-form/lock`        (surgeon-driven)
  - `_intraop_overdue_loop` cron                       (system-driven)
  - admin REOPEN → re-lock cycle                       (admin-driven)

It is idempotent in the sense that calling it again on the same locked
form snapshot produces an equivalent reassessment row (a fresh `id` and
`triggered_at`, but the same delta and final tier).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from triage.intraop.conservative_default import apply_conservative_default
from triage.intraop.delta import compute_intraop_delta
from triage.intraop.patient_state import (
    ensure_intraop_patient_state,
    get_anchor_procedure_family,
    get_current_tier,
    set_current_tier,
    set_phase,
)
from triage.intraop.resolve import resolve_final_tier
from triage.intraop.tuning import MODEL_VERSION, PROCEDURE_P90_MINUTES, TUNING_VERSION
from triage.intraop.types import (
    HospitalProcedureStats,
    IntraopDeltaResult,
    IntraopForm,
    ReassessmentEvent,
)
from triage.types import Tier


def _form_from_record(record: dict[str, Any]) -> IntraopForm:
    """Build an `IntraopForm` from a `TeamStore` row (or an empty dict for
    the conservative-default path)."""
    fields = dict(record.get("fields") or {})
    field_origins = record.get("field_origins") or {}
    fields.setdefault("field_origins", field_origins)
    return IntraopForm(**fields)


def apply_intraop_reassessment(
    *,
    patient_id: str,
    patient_store: dict[str, dict],
    team_store,
    triggered_by: str,
    is_conservative_default: bool = False,
    hospital_p90: Optional[dict[str, int]] = None,
) -> ReassessmentEvent:
    """Run the reassessment cycle described in PRD §8.1 and write the result.

    Args:
        patient_id: target patient.
        patient_store: process-local dict mapping `patient_id` → patient blob.
        team_store: a `TeamStore` instance (for the reassessment row + audit).
        triggered_by: actor identifier
            (`SURGEON_LOCK:<user_id>`, `SYSTEM:CONSERVATIVE_DEFAULT`,
            `ADMIN_REOPEN_RELOCK:<user_id>`).
        is_conservative_default: when True, skip delta computation and apply
            the no-data 1-step bump from `conservative_default`.
        hospital_p90: per-hospital observed P90 override; falls back to the
            tuning's national-benchmark map.

    Returns:
        The materialized `ReassessmentEvent` (also persisted to
        `intraop_reassessments`).
    """
    patient = patient_store.get(patient_id)
    if not patient:
        raise KeyError(f"unknown patient_id: {patient_id}")
    ensure_intraop_patient_state(patient)

    form_record = team_store.get_intraop_form(patient_id)
    if form_record is None and not is_conservative_default:
        raise RuntimeError("no intraop form on file; lock impossible")

    family = get_anchor_procedure_family(patient)
    current_tier: Tier = get_current_tier(patient)

    stats = HospitalProcedureStats(
        or_duration_p90_minutes=hospital_p90 or PROCEDURE_P90_MINUTES,
    )

    # ─── compute proposed tier ─────────────────────────────────────────────
    if is_conservative_default:
        delta: IntraopDeltaResult = apply_conservative_default(current_tier)
        # The conservative-default path can fire even before a form exists;
        # use a synthetic form snapshot for audit.
        form_snapshot: dict[str, Any] = (
            {"fields": (form_record or {}).get("fields", {}), "synthetic": True}
            if form_record else {"synthetic": True}
        )
        intraop_form_id = (form_record or {}).get("id") or "no-form"
    else:
        form = _form_from_record(form_record)
        delta = compute_intraop_delta(form, family, stats, current_tier)
        form_snapshot = {
            "fields": form.model_dump(exclude={"field_origins"}),
            "field_origins": form_record.get("field_origins") or {},
        }
        intraop_form_id = form_record["id"]

    proposed_tier: Tier = delta.proposed_tier
    final_tier: Tier = resolve_final_tier(current_tier, proposed_tier)

    # ─── persist reassessment row ──────────────────────────────────────────
    reassessment_id = uuid.uuid4().hex
    reasons_payload = [r.model_dump() for r in delta.reasons]
    team_store.save_intraop_reassessment(
        reassessment_id=reassessment_id,
        patient_id=patient_id,
        intraop_form_id=intraop_form_id,
        form_snapshot=form_snapshot,
        pre_or_current_tier=current_tier,
        proposed_tier=proposed_tier,
        final_tier=final_tier,
        hard_upgrade_applied=delta.hard_upgrade_applied,
        upgrade_steps=delta.upgrade_steps,
        reasons=reasons_payload,
        is_conservative_default=delta.is_conservative_default,
        procedure_family=family,
        model_version=MODEL_VERSION,
        tuning_version=TUNING_VERSION,
        triggered_by=triggered_by,
    )

    # ─── side effects: tier + phase + audit log ────────────────────────────
    if final_tier != current_tier:
        set_current_tier(
            patient,
            final_tier,
            was_hard=(delta.hard_upgrade_applied and not delta.is_conservative_default),
        )

    set_phase(patient, "post_op")

    # ─── post-intra-op floor snapshot (PRD Post-Op §10.1, README §1) ───────
    # Stamped once by the first intra-op reassessment that transitions the
    # episode into POST_OP. Subsequent re-locks (admin REOPEN → re-lock)
    # may move the floor only upward — never below the prior snapshot —
    # because the post-op re-tier reads it as the immutable lower bound.
    prior_floor = patient.get("post_intraop_tier")
    _TIER_RANK = {"TIER_1": 1, "TIER_2": 2, "TIER_3": 3}
    if prior_floor is None or _TIER_RANK[final_tier] > _TIER_RANK.get(prior_floor, 0):
        patient["post_intraop_tier"] = final_tier
    if not patient.get("post_intraop_tier_at"):
        patient["post_intraop_tier_at"] = datetime.utcnow().replace(microsecond=0).isoformat()

    # Pass 3 §1.3 — write `post_intraop_tier` through to episode_snapshots
    # so the post-op algorithm can rehydrate the floor on cold start.
    try:
        team_store.upsert_episode_snapshot(
            patient_id,
            post_intraop_tier=patient.get("post_intraop_tier"),
        )
    except Exception:
        pass

    try:
        team_store.log_event(
            patient_id=patient_id,
            event_type="INTRAOP_REASSESSMENT_APPLIED",
            payload={
                "reassessmentId": reassessment_id,
                "preTier": current_tier,
                "proposedTier": proposed_tier,
                "finalTier": final_tier,
                "hardUpgrade": delta.hard_upgrade_applied,
                "upgradeSteps": delta.upgrade_steps,
                "isConservativeDefault": delta.is_conservative_default,
                "procedureFamily": family,
                "triggeredBy": triggered_by,
                "modelVersion": MODEL_VERSION,
                "tuningVersion": TUNING_VERSION,
            },
        )
    except Exception:
        # Audit failure must never block the tier write; logged upstream.
        pass

    return ReassessmentEvent(
        id=reassessment_id,
        patient_id=patient_id,
        intraop_form_id=intraop_form_id,
        form_snapshot=form_snapshot,
        pre_or_current_tier=current_tier,
        proposed_tier=proposed_tier,
        final_tier=final_tier,
        hard_upgrade_applied=delta.hard_upgrade_applied,
        upgrade_steps=delta.upgrade_steps,
        reasons=delta.reasons,
        is_conservative_default=delta.is_conservative_default,
        model_version=MODEL_VERSION,
        tuning_version=TUNING_VERSION,
        triggered_by=triggered_by,
        triggered_at=datetime.utcnow().replace(microsecond=0).isoformat(),
        procedure_family=family,
    )
