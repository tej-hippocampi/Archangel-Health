"""
Asclepius (data-training product) onboarding flow.

Exercises the product split + the Steps 3–8 director flow and the invited-member
flow through the HTTP surface, asserting that people are provisioned into the
Asclepius plane (asclepius.db) with the right RBAC role + credential record.

Email transport is stubbed (no SendGrid round-trip); the flow only cares about
persistence + provisioning.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from pathlib import Path

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
    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)

    async def _stub_send(*_args, **_kwargs):  # noqa: ANN001
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _stub_send)


def _seed_verified(client: TestClient):
    """Pending health system advanced to step 2 (email verified), no product yet."""
    ts = client.app.state.team_store
    invite = ts.create_health_system_invite(invite_base_url="http://localhost:5173")
    token = invite["onboarding_url"].rsplit("/", 1)[-1]
    hs_id = invite["health_system_id"]
    director_email = f"dir_{uuid.uuid4().hex[:8]}@org.com"
    ts.update_health_system_director_identity(
        hs_id, first_name="Tej", last_name="Patel", email=director_email
    )
    with sqlite3.connect(ts.db_path) as conn:
        conn.execute(
            "UPDATE health_systems SET onboarding_step = 2 WHERE id = ?", (hs_id,)
        )
        conn.commit()
    return token, hs_id, director_email


CREDS = {
    "fullLegalName": "Dr. Tej Patel",
    "npi": "1234567890",
    "degree": "MD",
    "primarySpecialty": "Nephrology",
    "yearsInActivePractice": "12",
    "currentlyActive": True,
    "boardCertifications": [
        {"board": "ABIM", "specialty": "Internal Medicine", "subspecialty": "Nephrology", "active": True}
    ],
    "subspecialties": ["Dialysis", "Transplant"],
    "practiceSettings": ["Academic"],
    "languages": ["English", "Spanish"],
}
ATTS = {
    "consentCredentialShare": True,
    "attestIndependentJudgment": True,
    "ipAssignment": True,
    "noPhi": True,
    "signedInitials": "TP",
}


def test_director_full_asclepius_flow_provisions_admin(client: TestClient):
    token, hs_id, director_email = _seed_verified(client)

    assert client.post("/api/onboarding/select-product", json={"token": token, "product": "asclepius"}).status_code == 200

    r = client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": token, "org_name": "Northridge Nephrology", "specialty": "Nephrology", "phone": "(555) 123-4567"},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("slug")

    assert client.post("/api/onboarding/asclepius/credentials", json={"token": token, "credentials": CREDS}).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations", json={"token": token, "attestations": ATTS}).status_code == 200

    r = client.post(
        "/api/onboarding/asclepius/add-member",
        json={"token": token, "full_name": "Nina Lee", "email": f"nina_{uuid.uuid4().hex[:8]}@org.com", "role": "np"},
    )
    assert r.status_code == 200, r.text

    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 200, r.text
    assert r.json()["workspace_url"].endswith("/asclepius")

    # Director is now an Asclepius admin carrying the credential record.
    asc = client.app.state.asclepius_store
    u = asc.get_user_by_email(director_email)
    assert u and u["role"] == "admin"
    assert u["npi"] == "1234567890"
    assert u["full_name"] == "Dr. Tej Patel"

    # Onboarding is sealed.
    hs = client.app.state.team_store.get_health_system_by_id(hs_id)
    assert hs["status"] == "active" and hs["product"] == "asclepius"


def test_finish_requires_credentials_and_attestations(client: TestClient):
    token, _hs_id, _email = _seed_verified(client)
    client.post("/api/onboarding/select-product", json={"token": token, "product": "asclepius"})
    client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": token, "org_name": "Org X", "specialty": "Nephrology", "phone": "5551234"},
    )
    # No credentials/attestations yet → finish blocked.
    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 400
    assert "credential" in r.json()["detail"].lower()


def test_asclepius_endpoint_rejects_archangel_product(client: TestClient):
    token, _hs_id, _email = _seed_verified(client)
    client.post("/api/onboarding/select-product", json={"token": token, "product": "archangel"})
    r = client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": token, "org_name": "Org Y", "specialty": "Nephrology", "phone": "5551234"},
    )
    assert r.status_code == 409


def test_invited_member_flow_provisions_evaluator(client: TestClient):
    token, hs_id, _email = _seed_verified(client)
    ts = client.app.state.team_store
    client.post("/api/onboarding/select-product", json={"token": token, "product": "asclepius"})
    client.post(
        "/api/onboarding/asclepius/institution",
        json={"token": token, "org_name": "Northridge", "specialty": "Nephrology", "phone": "5551234"},
    )
    member_email = f"member_{uuid.uuid4().hex[:8]}@org.com"
    client.post(
        "/api/onboarding/asclepius/add-member",
        json={"token": token, "full_name": "Nina Lee", "email": member_email, "role": "np"},
    )
    # Mint a usable raw member token (the emailed one isn't returned by the API).
    mtoken = ts.issue_asclepius_member_token(hs_id, member_email)

    r = client.get(f"/api/onboarding/member/session?token={mtoken}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "asclepius_member"
    assert body["org_name"] == "Northridge" and body["specialty"] == "Nephrology"
    assert body["email"] == member_email

    member_creds = {**CREDS, "fullLegalName": "Nina Lee", "npi": "9876543210"}
    assert client.post("/api/onboarding/member/credentials", json={"token": mtoken, "credentials": member_creds}).status_code == 200
    assert client.post("/api/onboarding/member/attestations", json={"token": mtoken, "attestations": ATTS}).status_code == 200
    r = client.post("/api/onboarding/member/finish", json={"token": mtoken})
    assert r.status_code == 200, r.text

    asc = client.app.state.asclepius_store
    u = asc.get_user_by_email(member_email)
    assert u and u["role"] == "evaluator"
    assert u["npi"] == "9876543210"

    # Single-use: the token is consumed after finishing.
    assert client.get(f"/api/onboarding/member/session?token={mtoken}").status_code in (404, 410)
