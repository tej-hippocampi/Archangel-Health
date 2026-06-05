"""PRD-1 — patient PHI access control (close the unauthenticated IDOR).

Verifies that patient-facing routes require EITHER a patient session bound to the
exact patient_id OR scoped clinical staff, and that the entry-token -> HttpOnly
cookie flow works end-to-end.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Isolate the jti / team DB for this module and force enforcement on.
os.environ["TEAM_DB_PATH"] = os.path.join(tempfile.gettempdir(), f"pac_team_{uuid.uuid4().hex}.db")
os.environ["ENFORCE_PATIENT_AUTH"] = "1"

from jose import jwt  # noqa: E402

import patient_session as ps_mod  # noqa: E402
from main import app, DEMO_HEALTH_SYSTEM_ID  # noqa: E402
from tests._role_auth import tenant_token  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed_patient(*, pipeline="post_op", health_system_id=DEMO_HEALTH_SYSTEM_ID,
                  clinic_code="TESTCLN1", resource_code=None) -> str:
    pid = f"pac_{uuid.uuid4().hex[:8]}"
    rc = resource_code or uuid.uuid4().hex[:8].upper()
    app.state.patient_store[pid] = {
        "name": "Test Patient",
        "health_system_id": health_system_id,
        "phone": "+13105550000",
        "email": "test@example.com",
        "pipeline_type": pipeline,
        "voice_audio_url": None,
        "avatar_url": None,
        "battlecard_html": "<div>card</div>",
        "voice_script": "hello",
        "structured_data": {
            "patient_name": "Test Patient",
            "procedure_name": "Laparoscopic Appendectomy",
            "procedure_date": "2099-01-01",
            "status": "completed",
        },
        "clinic_code": clinic_code,
        "resource_code": rc,
        "resources": {
            "diagnosis": {"voice_audio_url": None, "battlecard_html": "<div>d</div>"},
            "treatment": {"voice_audio_url": None, "battlecard_html": "<div>t</div>"},
        },
    }
    return pid


# ─── 1. Anonymous access is blocked ──────────────────────────────────────────

def test_anon_api_blocked(client):
    pid = _seed_patient()
    r = client.get(f"/api/patient/{pid}/discharge")
    assert r.status_code == 404, r.text


def test_anon_page_without_k_blocked(client):
    pid = _seed_patient()
    r = client.get(f"/patient/{pid}", follow_redirects=False)
    assert r.status_code == 404, r.text


def test_anon_chat_blocked_before_llm(client):
    pid = _seed_patient()
    r = client.post("/api/digital-care-companion/chat",
                    json={"patient_id": pid, "message": "hi", "conversation_history": []})
    assert r.status_code == 404, r.text


# ─── 2. Full happy-path flow via by-codes ────────────────────────────────────

def test_full_flow_by_codes_then_api(client):
    rc = "RCFULL01"
    pid = _seed_patient(clinic_code="CLINFULL", resource_code=rc)

    r = client.get("/api/patient/by-codes",
                   params={"clinic_code": "CLINFULL", "resource_code": rc})
    assert r.status_code == 200, r.text
    url = r.json()["dashboard_url"]
    assert "?k=" in url

    # First-party navigation to the page sets the pt_session cookie.
    path = url[url.index("/patient/"):]
    page = client.get(path)  # follows the 302; cookie is stored by the client
    assert page.status_code == 200, page.text
    assert "pt_session" in client.cookies

    # Subsequent same-origin API call carries the cookie automatically.
    cfg = client.get(f"/api/patient/{pid}/config")
    assert cfg.status_code == 200, cfg.text
    assert cfg.json()["id"] == pid


# ─── 3. Wrong-patient session is rejected ────────────────────────────────────

def test_wrong_patient_session_blocked(client):
    a = _seed_patient()
    b = _seed_patient()
    token = ps_mod.create_patient_session(a, DEMO_HEALTH_SYSTEM_ID)
    client.cookies.set("pt_session", token)
    r = client.get(f"/api/patient/{b}/discharge")
    assert r.status_code == 404, r.text
    # but A's own resource is reachable
    r2 = client.get(f"/api/patient/{a}/discharge")
    assert r2.status_code == 200, r2.text


# ─── 4. Single-use entry token ───────────────────────────────────────────────

def test_entry_token_single_use(client):
    pid = _seed_patient()
    token = ps_mod.create_entry_token(pid, DEMO_HEALTH_SYSTEM_ID)
    first = client.get(f"/patient/{pid}?k={token}")
    assert first.status_code == 200, first.text

    # A brand-new client (no cookie) replaying the same token is rejected.
    with TestClient(app) as fresh:
        again = fresh.get(f"/patient/{pid}?k={token}", follow_redirects=False)
        assert again.status_code == 404, again.text


# ─── 5. Expired / revoked sessions ───────────────────────────────────────────

def test_expired_session_rejected(client):
    pid = _seed_patient()
    payload = {
        "typ": "patient", "pid": pid, "tid": DEMO_HEALTH_SYSTEM_ID,
        "jti": uuid.uuid4().hex,
        "exp": datetime.utcnow() - timedelta(minutes=1),
    }
    expired = jwt.encode(payload, ps_mod.AUTH_SECRET, algorithm=ps_mod.ALGORITHM)
    client.cookies.set("pt_session", expired)
    r = client.get(f"/api/patient/{pid}/discharge")
    assert r.status_code == 404, r.text


def test_logout_revokes_session(client):
    pid = _seed_patient()
    token = ps_mod.create_patient_session(pid, DEMO_HEALTH_SYSTEM_ID)
    client.cookies.set("pt_session", token)
    assert client.get(f"/api/patient/{pid}/config").status_code == 200

    # Revoke the jti directly, then the same token must stop working.
    ps_mod.revoke_patient_session(token)
    client.cookies.set("pt_session", token)
    assert client.get(f"/api/patient/{pid}/config").status_code == 404


# ─── 6. Staff access still works and stays tenant-scoped ─────────────────────

def test_tenant_staff_access(client):
    pid = _seed_patient(health_system_id="hs_alpha")
    ok = tenant_token("surgeon", email="s@alpha.com", health_system_id="hs_alpha")
    r = client.get(f"/api/patient/{pid}/discharge", headers={"Authorization": f"Bearer {ok}"})
    assert r.status_code == 200, r.text


def test_cross_tenant_staff_blocked(client):
    pid = _seed_patient(health_system_id="hs_alpha")
    other = tenant_token("surgeon", email="s@beta.com", health_system_id="hs_beta")
    r = client.get(f"/api/patient/{pid}/discharge", headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 404, r.text


# ─── 6b. Self-registered landing user cannot read real tenant PHI ────────────

def test_landing_user_cannot_read_tenant_patient(client):
    """A self-registered landing account (role surgeon, no tenant) must be scoped
    to the demo health system — not able to read a real tenant patient's PHI."""
    from auth import create_access_token, register_user
    pid = _seed_patient(health_system_id="hs_real_tenant")
    try:
        register_user("attacker@example.com", "pw123456", "Attacker")
    except ValueError:
        pass
    token = create_access_token("attacker@example.com")
    for route in (f"/api/patient/{pid}/discharge", f"/api/patient/{pid}/config",
                  f"/api/patient/{pid}/battlecard"):
        r = client.get(route, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404, f"{route} -> {r.status_code}"


# ─── 6c. Patient sessions cannot reach staff-only routes ─────────────────────

def test_patient_session_blocked_on_staff_only_routes(client):
    pid = _seed_patient()
    client.cookies.set("pt_session", ps_mod.create_patient_session(pid, DEMO_HEALTH_SYSTEM_ID))
    # Clinician HTML view is staff-only.
    r1 = client.get(f"/doctor/patient/{pid}", follow_redirects=False)
    assert r1.status_code in (401, 403, 404), r1.status_code
    # Sending the patient link is a staff-only action.
    r2 = client.post(f"/api/send-to-patient/{pid}")
    assert r2.status_code in (401, 403, 404), r2.status_code


# ─── 7. Enumeration guard: no anonymous 200 on any {patient_id} GET route ─────

def test_no_anonymous_phi_route_returns_200(client):
    random_pid = f"nonexistent_{uuid.uuid4().hex}"
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "GET" not in methods:
            continue
        if "{patient_id}" not in path or path.count("{") != 1:
            continue
        if not (path.startswith("/patient/") or path.startswith("/api/patient/")):
            continue
        url = path.replace("{patient_id}", random_pid)
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code != 200, f"{url} returned 200 unauthenticated"
        checked += 1
    assert checked > 0, "route-walker matched no patient routes"
