from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")
os.environ.setdefault("AUTH_SECRET", "test-auth-secret")

from main import app  # noqa: E402
from tests._role_auth import admin_headers  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_admin_ai_compliance_requires_auth(client):
    assert client.get("/admin/grounding/reports").status_code == 401
    assert client.get("/admin/grounding/stats").status_code == 401
    assert client.get("/admin/ai-calls").status_code == 401
    assert client.get("/admin/ai-calls/stats").status_code == 401
    assert client.get("/admin/ai-calls/prompts").status_code == 401


def test_admin_grounding_endpoints_return_seeded_rows(client):
    team_store = app.state.team_store
    pid_block = "admin_ground_block"
    pid_pass = "admin_ground_pass"
    app.state.patient_store[pid_block] = {"name": "Block P", "structured_data": {"patient_name": "Block P"}}
    app.state.patient_store[pid_pass] = {"name": "Pass P", "structured_data": {"patient_name": "Pass P"}}

    block_id = team_store.save_grounding_report(
        patient_id=pid_block,
        track="pre_op",
        report={
            "track": "pre_op",
            "coverage": [{"id": "x", "status": "MISSING", "severity": "CRITICAL", "category": "medication"}],
            "faithfulness": [],
            "critical_failures": ["missing med hold"],
            "verdict": "BLOCK",
            "summary": "blocked",
            "model": "claude-sonnet-4-6",
            "prompt_version": "1.0.0",
        },
        accuracy={"coverage_pct": 0.0, "faithfulness_pct": 100.0, "critical_failures": 1},
        script="bad script",
        regenerated=False,
    )
    team_store.save_grounding_report(
        patient_id=pid_pass,
        track="post_op_treatment",
        report={
            "track": "post_op_treatment",
            "coverage": [],
            "faithfulness": [],
            "critical_failures": [],
            "verdict": "PASS",
            "summary": "ok",
            "model": "claude-sonnet-4-6",
            "prompt_version": "1.0.0",
        },
        accuracy={"coverage_pct": 100.0, "faithfulness_pct": 100.0, "critical_failures": 0},
        script="good script",
        regenerated=False,
    )

    headers = admin_headers()
    reports = client.get("/admin/grounding/reports?verdict=BLOCK&limit=50", headers=headers)
    assert reports.status_code == 200
    rows = reports.json().get("reports") or []
    assert rows
    assert all(r.get("verdict") == "BLOCK" for r in rows)

    stats = client.get("/admin/grounding/stats?window_days=3650", headers=headers)
    assert stats.status_code == 200
    payload = stats.json()
    assert payload["block"] >= 1
    assert payload["pass"] >= 1

    detail = client.get(f"/admin/grounding/reports/{block_id}", headers=headers)
    assert detail.status_code == 200
    report = detail.json().get("report") or {}
    assert "coverage" in report
    assert "faithfulness" in report


def test_admin_ai_calls_filters_and_stats(client):
    team_store = app.state.team_store
    headers = admin_headers()

    team_store.log_event(
        patient_id="ai_call_patient_1",
        event_type="llm_call",
        payload={
            "role": "generation",
            "model": "claude-sonnet-4-6",
            "ai_config_version": "2026-05-31.1",
            "prompt": {"prompt_id": "preop_voice", "version": "1.0.0", "sha": "abc123def456"},
            "usage": {"input": 100, "output": 40},
            "latency_ms": 220,
        },
    )
    team_store.log_event(
        patient_id="ai_call_patient_2",
        event_type="llm_call",
        payload={
            "role": "grounding_judge",
            "model": "claude-sonnet-4-6",
            "ai_config_version": "2026-05-31.1",
            "prompt": {"prompt_id": "grounding_judge", "version": "1.0.0", "sha": "fff111eee222"},
            "usage": {"input": 80, "output": 20},
            "latency_ms": 180,
        },
    )

    by_role = client.get("/admin/ai-calls?role=generation&limit=50", headers=headers)
    assert by_role.status_code == 200
    role_rows = by_role.json().get("calls") or []
    assert role_rows
    assert all(r.get("role") == "generation" for r in role_rows)

    by_ver = client.get("/admin/ai-calls?prompt_version=1.0.0&limit=50", headers=headers)
    assert by_ver.status_code == 200
    assert len(by_ver.json().get("calls") or []) >= 2

    stats = client.get("/admin/ai-calls/stats?window_days=3650", headers=headers)
    assert stats.status_code == 200
    s = stats.json()
    assert s["total_calls"] >= 2
    assert s["total_input_tokens"] >= 180
    assert "generation" in (s.get("by_role") or {})

    prompts = client.get("/admin/ai-calls/prompts", headers=headers)
    assert prompts.status_code == 200
    plist = prompts.json().get("prompts") or []
    assert any(p.get("prompt_id") == "grounding_judge" for p in plist)
    gj = next(p for p in plist if p.get("prompt_id") == "grounding_judge")
    assert gj.get("version")
    assert gj.get("sha")


def test_admin_ai_calls_skips_malformed_payload_rows(client):
    team_store = app.state.team_store
    headers = admin_headers()

    with team_store._conn() as conn:  # noqa: SLF001 - test-only direct insert
        conn.execute(
            "INSERT INTO event_logs (patient_id, event_type, occurred_at, payload_json, episode_open_date) VALUES (?, ?, ?, ?, ?)",
            ("bad_row_patient", "llm_call", "2026-06-01T00:00:00", "{not-json", None),
        )
    team_store.log_event(
        patient_id="good_row_patient",
        event_type="llm_call",
        payload={
            "role": "generation",
            "model": "claude-sonnet-4-6",
            "ai_config_version": "2026-05-31.1",
            "prompt": {"prompt_id": "preop_voice", "version": "1.0.0", "sha": "abc123def456"},
            "usage": {"input": 10, "output": 5},
            "latency_ms": 20,
        },
    )

    res = client.get("/admin/ai-calls?limit=50", headers=headers)
    assert res.status_code == 200
    calls = res.json().get("calls") or []
    assert any(c.get("patient_id") == "good_row_patient" for c in calls)
