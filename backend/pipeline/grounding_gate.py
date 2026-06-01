"""
Gate voice-script synthesis on grounding check verdicts.

PASS  → proceed to ElevenLabs
REVIEW / BLOCK (after regen) → skip synthesis, flag patient for clinician review
BLOCK → auto-regenerate once, re-check
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pipeline.grounding_check import (
    GROUNDING_PROMPT_V,
    GroundingReport,
    check_grounding,
    compute_accuracy,
)

log = logging.getLogger(__name__)

GenerateFn = Callable[[], Awaitable[str]]


@dataclass
class GroundingGateResult:
    script: str
    report: GroundingReport
    report_id: int
    accuracy: dict
    synthesize: bool
    regenerated: bool = False


@dataclass
class PatientGroundingState:
    requires_clinician_review: bool = False
    grounding_reports: Dict[str, int] = field(default_factory=dict)
    grounding_pending_tracks: List[str] = field(default_factory=list)
    grounding_summaries: Dict[str, str] = field(default_factory=dict)


async def audit_and_gate_script(
    *,
    patient_id: str,
    structured_data: dict,
    script: str,
    track: str,
    team_store: Any,
    regenerate_fn: Optional[GenerateFn] = None,
) -> GroundingGateResult:
    """Run grounding check, persist, optionally regen once on BLOCK."""
    report = await check_grounding(structured_data, script, track, patient_id=patient_id)
    accuracy = compute_accuracy(report)
    report_id = team_store.save_grounding_report(
        patient_id=patient_id,
        track=track,
        report=report.model_dump(),
        accuracy=accuracy,
        script=script,
        regenerated=False,
    )
    _log_event(team_store, patient_id, report, report_id, regenerated=False)

    if report.verdict == "BLOCK" and regenerate_fn is not None:
        log.info("Grounding BLOCK for %s/%s — auto-regenerating once", patient_id, track)
        try:
            script = await regenerate_fn()
            report = await check_grounding(structured_data, script, track, patient_id=patient_id)
            accuracy = compute_accuracy(report)
            report_id = team_store.save_grounding_report(
                patient_id=patient_id,
                track=track,
                report=report.model_dump(),
                accuracy=accuracy,
                script=script,
                regenerated=True,
            )
            _log_event(team_store, patient_id, report, report_id, regenerated=True)
            return GroundingGateResult(
                script=script,
                report=report,
                report_id=report_id,
                accuracy=accuracy,
                synthesize=report.verdict == "PASS",
                regenerated=True,
            )
        except Exception as exc:
            log.exception("Auto-regenerate failed for %s/%s: %s", patient_id, track, exc)

    synthesize = report.verdict == "PASS"
    return GroundingGateResult(
        script=script,
        report=report,
        report_id=report_id,
        accuracy=accuracy,
        synthesize=synthesize,
        regenerated=False,
    )


def apply_grounding_to_patient(
    patient: dict,
    track: str,
    gate: GroundingGateResult,
) -> None:
    """Update patient blob with grounding state."""
    if "grounding_reports" not in patient or not isinstance(patient.get("grounding_reports"), dict):
        patient["grounding_reports"] = {}
    patient["grounding_reports"][track] = gate.report_id

    if "grounding_summaries" not in patient or not isinstance(patient.get("grounding_summaries"), dict):
        patient["grounding_summaries"] = {}
    patient["grounding_summaries"][track] = gate.report.summary

    pending = patient.get("grounding_pending_tracks")
    if not isinstance(pending, list):
        pending = []
        patient["grounding_pending_tracks"] = pending

    if gate.synthesize:
        if track in pending:
            pending.remove(track)
    else:
        if track not in pending:
            pending.append(track)

    patient["requires_clinician_review"] = len(pending) > 0


def _log_event(
    team_store: Any,
    patient_id: str,
    report: GroundingReport,
    report_id: int,
    *,
    regenerated: bool,
) -> None:
    try:
        team_store.log_event(
            patient_id=patient_id,
            event_type="grounding_check",
            payload={
                "report_id": report_id,
                "track": report.track,
                "verdict": report.verdict,
                "summary": report.summary,
                "critical_failures": report.critical_failures,
                "model": report.model,
                "prompt_version": GROUNDING_PROMPT_V,
                "regenerated": regenerated,
            },
        )
    except Exception:
        log.exception("Failed to log grounding_check event for %s", patient_id)
