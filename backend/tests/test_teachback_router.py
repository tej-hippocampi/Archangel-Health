from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed_patient() -> str:
    pid = f"tb_{uuid.uuid4().hex[:8]}"
    app.state.patient_store[pid] = {
        "id": pid,
        "phase": "post_op",
        "pipeline_type": "post_op",
        "current_tier": "TIER_1",
        "post_intraop_tier": "TIER_1",
        "discharge_at": (datetime.utcnow() - timedelta(days=1)).replace(microsecond=0).isoformat(),
        "structured_data": {"procedure_name": "Total Knee Arthroplasty"},
        "resources": {
            "preop": {"voice_script": "Stop warfarin 5 days before surgery.", "battlecard_html": "<div><h3>Prep</h3><p>Stop warfarin 5 days before surgery.</p></div>"},
            "diagnosis": {"voice_script": "Your diagnosis is severe OA.", "battlecard_html": "<div><h3>Diagnosis</h3><p>Severe osteoarthritis.</p></div>"},
            "treatment": {"voice_script": "If chest pain, call 911 now.", "battlecard_html": "<div><h3>Red flag</h3><p>Call 911 for chest pain.</p></div>"},
        },
    }
    return pid


def test_preop_teachback_full_flow(client, monkeypatch):
    pid = _seed_patient()

    async def _fake_generate(**_kwargs):
        questions = [
            {
                "id": "tb-q1",
                "track": "pre_op",
                "severity": "CRITICAL",
                "domain": "MED_HOLD",
                "form": "OPEN_ENDED",
                "question": "What is your warfarin hold plan?",
                "expected": "Stop warfarin 5 days before surgery.",
                "source_quote": "Stop warfarin 5 days before surgery.",
                "battlecard_anchor": "tb-anchor-warfarin",
            },
            {
                "id": "tb-q2",
                "track": "pre_op",
                "severity": "CRITICAL",
                "domain": "FASTING",
                "form": "OPEN_ENDED",
                "question": "What are your fasting instructions?",
                "expected": "No food after midnight.",
                "source_quote": "No food after midnight.",
                "battlecard_anchor": "tb-anchor-fasting",
            },
        ]
        html = "<div><h3 id='tb-anchor-warfarin'>Prep</h3><p id='tb-anchor-fasting'>No food after midnight.</p></div>"
        return questions, html

    monkeypatch.setattr("routers.teachback.generate_teachback_questions", _fake_generate)

    start = client.post(f"/api/episodes/{pid}/teachback/pre_op/start", json={})
    assert start.status_code == 200
    body = start.json()
    assert body["session_id"] > 0
    assert len(body["questions"]) == 2

    call_count = {"n": 0}

    async def _fake_grade(question, patient_answer, structured_data, **_kwargs):
        from pipeline.teachback_grade import TeachBackGrade

        call_count["n"] += 1
        if call_count["n"] == 1:
            return TeachBackGrade(
                question_id=question["id"],
                status="PARTIAL",
                missing=["MISSING_DETAIL"],
                evidence="first miss",
                severity=question["severity"],
                domain=question["domain"],
            )
        return TeachBackGrade(
            question_id=question["id"],
            status="PASS",
            missing=[],
            evidence="good",
            severity=question["severity"],
            domain=question["domain"],
        )

    async def _noop_retier(**_kwargs):
        return None

    monkeypatch.setattr("routers.teachback.grade_answer", _fake_grade)
    monkeypatch.setattr("routers.teachback._trigger_retier_after_completion", _noop_retier)

    r1 = client.post(
        f"/api/episodes/{pid}/teachback/pre_op/answer",
        json={"session_id": body["session_id"], "question_id": "tb-q1", "answer": "not sure"},
    )
    assert r1.status_code == 200
    assert r1.json()["retry"] is True
    assert r1.json()["completed"] is False

    r2 = client.post(
        f"/api/episodes/{pid}/teachback/pre_op/answer",
        json={"session_id": body["session_id"], "question_id": "tb-q1", "answer": "Stop 5 days before"},
    )
    assert r2.status_code == 200
    assert r2.json()["retry"] is False
    assert r2.json()["completed"] is False

    r3 = client.post(
        f"/api/episodes/{pid}/teachback/pre_op/answer",
        json={"session_id": body["session_id"], "question_id": "tb-q2", "answer": "No food after midnight"},
    )
    assert r3.status_code == 200
    assert r3.json()["completed"] is True

    events = app.state.team_store.get_events(pid)
    tb_events = [e for e in events if e.get("event_type") == "teachback_result"]
    assert tb_events, "teachback_result should be logged once completed"
    payload = tb_events[-1].get("payload") or {}
    assert "answer" not in str(payload).lower()


def test_postop_diagnosis_teachback_starts_without_video_events(client, monkeypatch):
    pid = _seed_patient()

    async def _fake_generate(**_kwargs):
        return (
            [
                {
                    "id": "tb-dx-1",
                    "track": "post_op_diagnosis",
                    "severity": "MAJOR",
                    "domain": "MAIN_PROBLEM",
                    "form": "OPEN_ENDED",
                    "question": "What was your diagnosis?",
                    "expected": "Severe osteoarthritis.",
                    "source_quote": "Severe osteoarthritis.",
                    "battlecard_anchor": "tb-anchor-dx",
                }
            ],
            "<div><h3 id='tb-anchor-dx'>Diagnosis</h3></div>",
        )

    monkeypatch.setattr("routers.teachback.generate_teachback_questions", _fake_generate)
    r = client.post(f"/api/episodes/{pid}/teachback/post_op_diagnosis/start", json={})
    assert r.status_code == 200
