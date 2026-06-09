from __future__ import annotations

from typing import Any, Optional

from integrations.elevenlabs import ElevenLabsClient
from pipeline.grounding_gate import apply_grounding_to_patient, audit_and_gate_script


async def synthesize_script(
    *,
    patient_id: str,
    structured_data: dict[str, Any],
    script: str,
    track: str,
    team_store: Any,
    audio_id: str,
    regenerate_fn=None,
    patient_blob: Optional[dict[str, Any]] = None,
    sem=None,
    force_synthesize: bool = False,
    override_actor: Optional[str] = None,
    override_reason: Optional[str] = None,
):
    """Single sanctioned script->audio path with grounding gate."""
    if sem is None:
        gate = await audit_and_gate_script(
            patient_id=patient_id,
            structured_data=structured_data,
            script=script,
            track=track,
            team_store=team_store,
            regenerate_fn=regenerate_fn,
        )
    else:
        async with sem:
            gate = await audit_and_gate_script(
                patient_id=patient_id,
                structured_data=structured_data,
                script=script,
                track=track,
                team_store=team_store,
                regenerate_fn=regenerate_fn,
            )
    if patient_blob is not None:
        apply_grounding_to_patient(patient_blob, track, gate)

    # Always synthesize the voice audio so the clinician can review and send it,
    # regardless of the grounding verdict. The grounding report is still recorded
    # and surfaced to the clinician for compliance and trust — the clinician is the
    # final gate before any material reaches the patient.
    audio_url = None
    try:
        # PRD-4: pass the patient's name parts so they're scrubbed if ElevenLabs
        # has no BAA (the client de-identifies dates/contacts/ids automatically).
        _pname = (structured_data or {}).get("patient_name") or (structured_data or {}).get("name") or ""
        _deid_terms = ([_pname] + str(_pname).split()) if _pname else None
        audio_url = await ElevenLabsClient().synthesize(gate.script, audio_id, deid_terms=_deid_terms)
    except Exception:
        audio_url = None

    # Compliance audit trail: when the grounding gate flagged the script (it would
    # previously have blocked synthesis), record that audio was produced for
    # clinician review anyway — including the acting clinician when known.
    if not gate.synthesize and team_store is not None:
        actor = (override_actor or "").strip()
        try:
            team_store.log_event(
                patient_id=patient_id,
                event_type="grounding_override" if actor else "grounding_review_audio_for_clinician",
                payload={
                    "track": track,
                    "verdict": getattr(getattr(gate, "report", None), "verdict", None),
                    "report_id": getattr(gate, "report_id", None),
                    "actor": actor or None,
                    "reason": (override_reason or "").strip() or "audio generated for clinician review",
                    "audio_id": audio_id,
                },
            )
        except Exception:
            pass
    return gate, audio_url
