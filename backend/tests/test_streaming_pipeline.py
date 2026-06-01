from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pipeline.streaming import StreamingPipelineContext, run_postop_stream, run_preop_stream


def _run(coro):
    return asyncio.run(coro)


def _collect(gen):
    out = []

    async def _inner():
        async for ev in gen:
            out.append(ev)

    _run(_inner())
    return out


def _make_gate(*, script: str, verdict: str, synthesize: bool, regenerated: bool = False):
    report = SimpleNamespace(
        verdict=verdict,
        coverage=[{"id": "x", "status": "COVERED", "severity": "MAJOR"}],
        faithfulness=[],
        critical_failures=[],
        summary="ok",
    )
    return SimpleNamespace(script=script, report=report, report_id=1, regenerated=regenerated, synthesize=synthesize)


def test_run_postop_stream_emits_expected_events(monkeypatch):
    class _Extractor:
        async def extract(self, raw):
            return {
                "patient_name": "Jane Doe",
                "procedure_name": "CABG",
                "medications": [{"name": "Aspirin", "status": "new"}],
                "red_flags": ["fever"],
                "follow_up": {"date": "2026-07-01"},
                "missing_critical_data": [],
            }

    class _Generator:
        async def generate_two_resources(self, structured):
            return {
                "diagnosis": {"voice_script": "diagnosis voice", "battlecard_html": "<div>dx</div>"},
                "treatment": {"voice_script": "treatment voice", "battlecard_html": "<div>tx</div>"},
            }

    async def _synth(**kwargs):
        track = kwargs["track"]
        if track == "post_op_diagnosis":
            return _make_gate(script="diagnosis gated", verdict="PASS", synthesize=True), "/audio/p_dx.mp3"
        return _make_gate(script="treatment gated", verdict="PASS", synthesize=True), "/audio/p_tx.mp3"

    monkeypatch.setattr("pipeline.streaming.ExtractionLayer", _Extractor)
    monkeypatch.setattr("pipeline.streaming.GenerationLayer", _Generator)
    monkeypatch.setattr("pipeline.streaming.synthesize_script", _synth)
    monkeypatch.setattr("pipeline.streaming.build_required_items", lambda *_a, **_k: [{"id": "x", "category": "medication", "text": "Item", "severity": "MAJOR"}])
    monkeypatch.setattr("pipeline.streaming.compute_accuracy", lambda report: {"coverage_pct": 100.0, "faithfulness_pct": 100.0})

    store = {}
    team_store = SimpleNamespace(ensure_episode=lambda **_kwargs: None)
    ctx = StreamingPipelineContext(patient_store=store, team_store=team_store, persist_demo=lambda: None, base_url="http://localhost:8000")
    input_data = SimpleNamespace(patient_name="Jane Doe", phone_number="", email="", discharge_notes="postop notes")
    events = _collect(
        run_postop_stream(
            input_data,
            patient_id="p1",
            clinic_code=None,
            resource_code=None,
            office_phone=None,
            health_system_id=None,
            ctx=ctx,
        )
    )

    stages = [e["stage"] for e in events]
    assert stages[0] == "pipeline.start"
    assert "extract.start" in stages
    assert "extract.done" in stages
    assert "generate.start" in stages
    assert "generate.done" in stages
    assert "grounding.start" in stages
    assert "grounding.result" in stages
    assert "synthesize.done" in stages
    assert stages[-1] == "complete"
    complete = events[-1]["payload"]
    assert complete["patient_id"] == "p1"
    assert complete["diagnosis"]["voice_audio_url"] == "/audio/p_dx.mp3"
    assert complete["treatment"]["voice_audio_url"] == "/audio/p_tx.mp3"
    assert complete["diagnosis"]["voice_script"] == "diagnosis gated"
    assert complete["treatment"]["voice_script"] == "treatment gated"
    assert "postop notes" not in str(events)


def test_run_preop_stream_block_emits_synthesize_skipped(monkeypatch):
    class _Extractor:
        async def extract(self, raw):
            return {"patient_name": "Amy", "procedure_name": "THA", "medications": [], "red_flags": [], "follow_up": {}}

    class _Generator:
        async def generate(self, structured, pipeline_type):
            return "voice draft", "<div>battlecard</div>"

    async def _synth(**kwargs):
        return _make_gate(script="blocked draft", verdict="BLOCK", synthesize=False, regenerated=True), None

    monkeypatch.setattr("pipeline.streaming.ExtractionLayer", _Extractor)
    monkeypatch.setattr("pipeline.streaming.GenerationLayer", _Generator)
    monkeypatch.setattr("pipeline.streaming.synthesize_script", _synth)
    monkeypatch.setattr("pipeline.streaming.build_required_items", lambda *_a, **_k: [{"id": "x", "category": "red_flag", "text": "Item", "severity": "CRITICAL"}])
    monkeypatch.setattr("pipeline.streaming.compute_accuracy", lambda report: {"coverage_pct": 62.5, "faithfulness_pct": 100.0})

    store = {}
    team_store = SimpleNamespace(ensure_episode=lambda **_kwargs: None)
    ctx = StreamingPipelineContext(patient_store=store, team_store=team_store, persist_demo=lambda: None, base_url="http://localhost:8000")
    input_data = SimpleNamespace(
        patient_name="Amy",
        phone_number="",
        email="",
        preparation_notes="prep",
        procedure_type="THA",
        scheduled_surgery_date="2026-08-01",
    )
    events = _collect(
        run_preop_stream(
            input_data,
            patient_id="pre1",
            clinic_code=None,
            resource_code=None,
            office_phone=None,
            health_system_id=None,
            specialty_from_procedure=lambda _: "Orthopedic",
            ctx=ctx,
        )
    )
    stages = [e["stage"] for e in events]
    assert "grounding.regenerated" in stages
    assert "synthesize.skipped" in stages
    assert "synthesize.done" not in stages
    complete = events[-1]["payload"]
    assert complete["preop"]["voice_audio_url"] is None
    assert complete["preop"]["voice_script"] == "blocked draft"
