from __future__ import annotations

import asyncio
import types

from pipeline import gated_synthesis


def _run(coro):
    return asyncio.run(coro)


class _StoreStub:
    def __init__(self):
        self.events = []

    def log_event(self, *, patient_id=None, event_type: str, payload=None, occurred_at=None):
        self.events.append(
            {
                "patient_id": patient_id,
                "event_type": event_type,
                "payload": payload or {},
                "occurred_at": occurred_at,
            }
        )


def test_force_override_logs_event_with_actor(monkeypatch):
    gate = types.SimpleNamespace(
        script="safe script",
        synthesize=False,
        report=types.SimpleNamespace(verdict="BLOCK"),
        report_id=42,
    )

    async def _fake_gate(**_kwargs):
        return gate

    class _Eleven:
        async def synthesize(self, script, audio_id):
            return f"/audio/{audio_id}.mp3"

    monkeypatch.setattr(gated_synthesis, "audit_and_gate_script", _fake_gate)
    monkeypatch.setattr(gated_synthesis, "ElevenLabsClient", lambda: _Eleven())

    store = _StoreStub()
    _, audio = _run(
        gated_synthesis.synthesize_script(
            patient_id="p1",
            structured_data={"patient_name": "Pat"},
            script="raw",
            track="pre_op",
            team_store=store,
            audio_id="p1_preop",
            force_synthesize=True,
            override_actor="doctor@archangel.test",
            override_reason="clinician-confirmed notes",
        )
    )
    assert audio == "/audio/p1_preop.mp3"
    assert any(
        e["event_type"] == "grounding_override"
        and e["payload"].get("actor") == "doctor@archangel.test"
        and e["payload"].get("report_id") == 42
        for e in store.events
    )


def test_blocked_without_actor_still_ships_audio_for_review(monkeypatch):
    # New behaviour: the voice audio is ALWAYS generated so the clinician can
    # review and send it, even when the grounding gate flagged the script and no
    # acting clinician was supplied. The grounding verdict is still recorded for
    # compliance, and a review-audit event is logged.
    gate = types.SimpleNamespace(
        script="blocked script",
        synthesize=False,
        report=types.SimpleNamespace(verdict="BLOCK"),
        report_id=11,
    )

    async def _fake_gate(**_kwargs):
        return gate

    class _Eleven:
        async def synthesize(self, script, audio_id):
            return f"/audio/{audio_id}.mp3"

    monkeypatch.setattr(gated_synthesis, "audit_and_gate_script", _fake_gate)
    monkeypatch.setattr(gated_synthesis, "ElevenLabsClient", lambda: _Eleven())

    store = _StoreStub()
    _, audio = _run(
        gated_synthesis.synthesize_script(
            patient_id="p2",
            structured_data={"patient_name": "Pat"},
            script="raw",
            track="pre_op",
            team_store=store,
            audio_id="p2_preop",
            force_synthesize=True,
            override_actor="",
        )
    )
    assert audio == "/audio/p2_preop.mp3"
    assert any(
        e["event_type"] == "grounding_review_audio_for_clinician"
        and e["payload"].get("report_id") == 11
        for e in store.events
    )
