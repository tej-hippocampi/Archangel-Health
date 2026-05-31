"""Integration tests for grounding gate edge cases."""

from __future__ import annotations

import asyncio

from pipeline.grounding_gate import GroundingGateResult, apply_grounding_to_patient
from pipeline.grounding_check import GroundingReport


def _gate(verdict: str, synthesize: bool, track: str = "post_op_treatment") -> GroundingGateResult:
    return GroundingGateResult(
        script="script",
        report=GroundingReport(
            track=track,
            coverage=[],
            faithfulness=[],
            critical_failures=[] if verdict == "PASS" else ["x"],
            verdict=verdict,
            summary=f"verdict {verdict}",
        ),
        report_id=1,
        accuracy={"coverage_pct": 100.0, "faithfulness_pct": 100.0},
        synthesize=synthesize,
    )


def test_dual_track_mixed_verdicts_keeps_review_flag():
    patient: dict = {}
    apply_grounding_to_patient(patient, "post_op_diagnosis", _gate("PASS", True, "post_op_diagnosis"))
    assert patient.get("requires_clinician_review") is False
    apply_grounding_to_patient(patient, "post_op_treatment", _gate("BLOCK", False, "post_op_treatment"))
    assert patient.get("requires_clinician_review") is True
    assert "post_op_treatment" in patient.get("grounding_pending_tracks", [])


def test_dual_track_both_pass_clears_review():
    patient: dict = {}
    apply_grounding_to_patient(patient, "post_op_diagnosis", _gate("PASS", True, "post_op_diagnosis"))
    apply_grounding_to_patient(patient, "post_op_treatment", _gate("PASS", True, "post_op_treatment"))
    assert patient.get("requires_clinician_review") is False
    assert patient.get("grounding_pending_tracks") == []


def test_force_synthesize_should_not_leave_review_flag_when_audio_shipped():
    """Clinician-confirmed notes path: audit may BLOCK but audio ships — no review hold."""
    patient: dict = {
        "requires_clinician_review": True,
        "grounding_pending_tracks": ["post_op_treatment", "pre_op"],
    }

    async def _run():
        import eligibility.pipeline as ep
        from unittest.mock import AsyncMock

        class _StubGen:
            async def generate(self, sd, pt):
                return "voice", "<html/>"

        async def _blocked(**kwargs):
            return _gate("BLOCK", False, kwargs.get("track", "pre_op"))

        import pipeline.grounding_gate as gg

        orig_audit = gg.audit_and_gate_script
        gg.audit_and_gate_script = _blocked  # type: ignore[method-assign]

        class _EL:
            async def synthesize(self, script, pid):
                return f"/audio/{pid}.mp3"

        import integrations.elevenlabs as el

        el.ElevenLabsClient = lambda: _EL()  # type: ignore[misc]

        import pipeline.generate as gen_mod

        gen_mod.GenerationLayer = lambda: _StubGen()  # type: ignore[misc]

        try:
            await ep.regenerate_materials(
                patient,
                pipeline_type="pre_op",
                notes_text="confirmed notes",
                patient_id="p_force",
                team_store=object(),
                force_synthesize=True,
            )
        finally:
            gg.audit_and_gate_script = orig_audit  # type: ignore[method-assign]

    asyncio.run(_run())
    assert patient["resources"]["preop"]["voice_audio_url"] == "/audio/p_force_preop.mp3"
    assert patient.get("requires_clinician_review") is True
    assert patient.get("grounding_pending_tracks") == ["post_op_treatment"]
