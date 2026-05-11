"""
Pass-4 surgical pod cap tests (PRD §2.4).

The director slot is auto-seeded as the surgeon on `/finish`; the wizard only
invites RN coordinators and NP/PAs. The pod is exactly 4 people = director +
1 RN + 2 NP/PAs. We exercise the cap inside `routers/onboarding.add_team_member`
through the HTTP surface so the 409 detail strings + status codes match
production behavior.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

import routers.onboarding as onboarding_module  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_email(monkeypatch):
    """Bypass SendGrid; pod-cap tests only care about insert ordering."""
    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)

    async def _stub_send(*_args, **_kwargs):  # noqa: ANN001
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _stub_send)


def _seed_pending_pod(client: TestClient) -> str:
    """Create a fresh pending health system advanced to step 3 (org details done)."""
    ts = client.app.state.team_store
    invite = ts.create_health_system_invite(invite_base_url="http://localhost:5173")
    raw_token = invite["onboarding_url"].rsplit("/", 1)[-1]
    hs_id = invite["health_system_id"]
    ts.update_health_system_director_identity(
        hs_id,
        first_name="Dir",
        last_name="Ector",
        email=f"dir_{uuid.uuid4().hex[:6]}@hs.com",
    )
    # Advance past OTP + org steps without the email round-trip.
    import sqlite3

    with sqlite3.connect(ts.db_path) as conn:
        conn.execute(
            "UPDATE health_systems SET onboarding_step = 3, name = 'Pod HS', surgery_department = 'Ortho', phone = '5551111' WHERE id = ?",
            (hs_id,),
        )
        conn.commit()
    return raw_token


def _add(client: TestClient, token: str, *, role: str, email: str | None = None) -> Any:
    return client.post(
        "/api/onboarding/add-team-member",
        json={
            "token": token,
            "full_name": f"{role.title()} Person",
            "email": email or f"{role}_{uuid.uuid4().hex[:6]}@hs.com",
            "role": role,
        },
    )


def test_add_first_rn_coordinator_succeeds(client: TestClient):
    token = _seed_pending_pod(client)
    r = _add(client, token, role="rn_coordinator")
    assert r.status_code == 200, r.text


def test_second_rn_coordinator_rejected_409(client: TestClient):
    token = _seed_pending_pod(client)
    assert _add(client, token, role="rn_coordinator").status_code == 200
    r = _add(client, token, role="rn_coordinator")
    assert r.status_code == 409
    assert "RN care coordinator" in r.json().get("detail", "")


def test_two_np_pa_succeeds_third_rejected(client: TestClient):
    token = _seed_pending_pod(client)
    assert _add(client, token, role="np_pa").status_code == 200
    assert _add(client, token, role="np_pa").status_code == 200
    r = _add(client, token, role="np_pa")
    assert r.status_code == 409
    assert "2 NP/PAs" in r.json().get("detail", "")


def test_non_director_surgeon_always_rejected(client: TestClient):
    token = _seed_pending_pod(client)
    r = _add(client, token, role="surgeon")
    assert r.status_code == 409
    assert "director" in r.json().get("detail", "").lower()


def test_team_full_after_one_rn_two_nppa(client: TestClient):
    token = _seed_pending_pod(client)
    assert _add(client, token, role="rn_coordinator").status_code == 200
    assert _add(client, token, role="np_pa").status_code == 200
    assert _add(client, token, role="np_pa").status_code == 200
    # Third NP/PA hits the np_pa cap first (before the team-full check).
    r = _add(client, token, role="np_pa")
    assert r.status_code == 409


def test_invalid_role_token_rejected(client: TestClient):
    token = _seed_pending_pod(client)
    r = _add(client, token, role="anesthesia_provider")
    assert r.status_code == 400


def test_director_finalize_writes_surgeon_with_director_flag(client: TestClient):
    token = _seed_pending_pod(client)
    r = client.post("/api/onboarding/finish", json={"token": token})
    assert r.status_code == 200, r.text
    ts = client.app.state.team_store
    hs = ts.get_health_system_by_onboarding_token(token)
    members = ts.list_team_members(hs["id"])
    director_rows = [m for m in members if m["is_team_director"]]
    assert len(director_rows) == 1
    assert director_rows[0]["role"] == "surgeon"


def test_session_response_filters_director_via_flag(client: TestClient):
    """Step-4 hydration must hide the director from the team list (the UI shows them in their own card)."""
    token = _seed_pending_pod(client)
    assert _add(client, token, role="rn_coordinator").status_code == 200
    # Finalize so the director row gets created.
    assert client.post("/api/onboarding/finish", json={"token": token}).status_code == 200
    r = client.get(f"/api/onboarding/session?token={token}")
    body = r.json()
    serialized_emails = {m["email"] for m in body.get("team_members", [])}
    assert all("dir_" not in e for e in serialized_emails)


def test_serialized_team_member_uses_pass4_label(client: TestClient):
    token = _seed_pending_pod(client)
    assert _add(client, token, role="np_pa").status_code == 200
    r = client.get(f"/api/onboarding/session?token={token}")
    body = r.json()
    members = body.get("team_members", [])
    assert any(m["role"] == "NP / PA" for m in members)
