"""Tenant-scoped AI Security & Compliance endpoints (grounding, ai-calls, audit log)."""

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
from tenant_constants import DEMO_HEALTH_SYSTEM_ID, DEMO_HEALTH_SYSTEM_SLUG  # noqa: E402
from tests._role_auth import auth_headers  # noqa: E402


@pytest.fixture()
def client():
    app.state.team_store.ensure_demo_health_system(
        hs_id=DEMO_HEALTH_SYSTEM_ID,
        slug=DEMO_HEALTH_SYSTEM_SLUG,
        name="Demo Health System",
        health_system_code="DEMO",
    )
    with TestClient(app) as c:
        yield c


def test_tenant_ai_compliance_requires_auth(client):
    slug = DEMO_HEALTH_SYSTEM_SLUG
    assert client.get(f"/api/tenant/{slug}/grounding/stats").status_code == 401
    assert client.get(f"/api/tenant/{slug}/ai-calls").status_code == 401
    assert client.get(f"/api/tenant/{slug}/audit-log").status_code == 401


def test_tenant_ai_compliance_endpoints_return_200_for_rn(client):
    slug = DEMO_HEALTH_SYSTEM_SLUG
    headers = auth_headers("rn_coordinator")

    assert client.get(f"/api/tenant/{slug}/grounding/stats?window_days=30", headers=headers).status_code == 200
    assert client.get(f"/api/tenant/{slug}/grounding/inspector-recall", headers=headers).status_code == 200
    assert client.get(f"/api/tenant/{slug}/grounding/reports?limit=10", headers=headers).status_code == 200
    assert client.get(f"/api/tenant/{slug}/ai-calls/stats?window_days=30", headers=headers).status_code == 200
    assert client.get(f"/api/tenant/{slug}/ai-calls?limit=10", headers=headers).status_code == 200
    assert client.get(f"/api/tenant/{slug}/ai-calls/prompts", headers=headers).status_code == 200
    assert client.get(f"/api/tenant/{slug}/audit-log", headers=headers).status_code == 200
