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

    audio_url = None
    overrode = False
    if gate.synthesize:
        try:
            audio_url = await ElevenLabsClient().synthesize(gate.script, audio_id)
        except Exception:
            audio_url = None
    elif force_synthesize:
        actor = (override_actor or "").strip()
        if not actor:
            if team_store is not None:
                try:
                    team_store.log_event(
                        patient_id=patient_id,
                        event_type="grounding_override_missing_actor",
                        payload={
                            "track": track,
                            "verdict": getattr(getattr(gate, "report", None), "verdict", None),
                            "report_id": getattr(gate, "report_id", None),
                            "audio_id": audio_id,
                        },
                    )
                except Exception:
                    pass
            return gate, None
        overrode = True
        try:
            audio_url = await ElevenLabsClient().synthesize(gate.script, audio_id)
        except Exception:
            audio_url = None
        if team_store is not None and overrode:
            try:
                team_store.log_event(
                    patient_id=patient_id,
                    event_type="grounding_override",
                    payload={
                        "track": track,
                        "verdict": getattr(getattr(gate, "report", None), "verdict", None),
                        "report_id": getattr(gate, "report_id", None),
                        "actor": actor,
                        "reason": (override_reason or "").strip() or "clinician-confirmed notes",
                        "audio_id": audio_id,
                    },
                )
            except Exception:
                pass
    return gate, audio_url
