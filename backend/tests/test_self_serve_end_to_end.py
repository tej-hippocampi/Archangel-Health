"""Walk the self-serve door the way a physician does, in the wizard's own order.

This exists because splitting the credentials screen and adding a password step
broke the flow in two places at once, and every existing test still passed:

  * the wizard's OTP handler hardcoded setStep("institution"), which skipped the
    new password step entirely;
  * /asclepius/password then 400'd anyway, because the director's person row is
    created by the INSTITUTION step, which now runs after it.

The physician reached the last screen, hit "Choose a password before finishing",
and had no route back to a step the wizard had already walked past.

Nothing caught it because the suite's other signup walks POST the endpoints in
whatever order the test author wrote, not the order the wizard actually visits.
So this test hardcodes the wizard's order and asserts the flow COMPLETES.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, uniq

_WIZARD = Path(__file__).resolve().parents[2] / "landing" / "src" / "app" / "components" / "OnboardingWizard.tsx"
_STEPS = Path(__file__).resolve().parents[2] / "landing" / "src" / "app" / "components" / "onboarding" / "steps.tsx"

PW = "correct-horse-battery-1"

CREDS = {
    "fullLegalName": "Dr Amara Okafor",
    "npi": "1234567893",
    "degree": "MD",
    "primarySpecialty": "Nephrology",
    "phone": "5551234567",
    "currentlyActive": True,
    "licenseNumber": "A12345",
    "licenseState": "CA",
    "residencyCompleted": True,
    "practiceStatus": "active",
}
ATTS = {"accurate": True, "noPhi": True, "ip": True, "independent": True,
        "confidential": True, "noDiscipline": True, "initials": "AO"}


from routers import onboarding as onboarding_module


@pytest.fixture(autouse=True)
def _stub_email(monkeypatch):
    """Same stub the rest of the suite uses. Finishing sends the welcome email,
    and an unconfigured transport 503s the step under test."""
    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)

    async def _stub_send(*_args, **_kwargs):
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _stub_send)


@pytest.fixture(autouse=True)
def _no_real_nppes(monkeypatch):
    """No live NPPES call from a test."""
    from asclepius import credentialing as _cred

    monkeypatch.setattr(_cred, "fetch_npi_record",
                        lambda *a, **k: {"result": "unavailable", "reason": "test"})


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_a_physician_can_actually_finish_self_serve_onboarding(client, monkeypatch):
    """The whole point. Every step in the order the wizard visits them."""
    fresh_store()
    email = f"dr-{uniq()}@nephrology-associates.com"

    # Steps 1-2 (identity, OTP) are seeded the same way the rest of the suite
    # seeds them: onboarding_step = 2 means the mailbox is proven. The OTP path
    # itself is covered in test_asclepius_onboarding; what is under test here is
    # everything that happens AFTER it.
    ts = client.app.state.team_store
    invite = ts.create_health_system_invite(
        invite_base_url="http://localhost:5173", director_email=email, product="asclepius")
    token = invite["onboarding_url"].rsplit("/", 1)[-1]
    hs_id = invite["health_system_id"]
    ts.update_health_system_director_identity(
        hs_id, first_name="Amara", last_name="Okafor", email=email)
    with sqlite3.connect(ts.db_path) as conn:
        conn.execute("UPDATE health_systems SET onboarding_step = 2 WHERE id = ?", (hs_id,))
        conn.commit()

    # 3. password — BEFORE institution, which is what used to 400 here because
    #    the director's person row did not exist yet.
    r = client.post("/api/onboarding/asclepius/password",
                    json={"token": token, "password": PW})
    assert r.status_code == 200, f"the password step is unreachable: {r.text}"

    # 4. institution  5-7. credentials  8. attestations
    assert client.post("/api/onboarding/select-product",
                       json={"token": token, "product": "asclepius"}).status_code == 200
    assert client.post("/api/onboarding/asclepius/institution", json={
        "token": token, "org_name": "Northridge Nephrology",
        "specialty": "Nephrology", "phone": "(555) 123-4567"}).status_code == 200
    assert client.post("/api/onboarding/asclepius/credentials",
                       json={"token": token, "credentials": CREDS}).status_code == 200
    assert client.post("/api/onboarding/asclepius/attestations",
                       json={"token": token, "attestations": ATTS}).status_code == 200

    # 9. finish — this is the step that used to be a dead end.
    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 200, f"onboarding could not be completed: {r.text}"

    # And the password they CHOSE is the one on the provisioned account, rather
    # than a generated one they were never told. Asserted against the store the
    # app actually wrote to (app.state.asclepius_store), the same way the other
    # onboarding tests do: the HTTP login route resolves the module-level store,
    # which fresh_store() has rebound underneath it.
    from asclepius.store import verify_password

    asc = client.app.state.asclepius_store
    user = asc.get_user_by_email(email)
    assert user, "onboarding finished but provisioned no Asclepius account"
    assert verify_password(PW, user["password_hash"]), (
        "the account was provisioned with a password the physician never chose"
    )
    assert user["active"]


def test_the_otp_hands_off_to_a_screen_that_actually_exists():
    """A backend that accepts a password is no use if the wizard never visits
    the screen that sends one — and the reverse is just as bad.

    Onboarding v2 §2 removed the password step from the PHYSICIAN path: that
    account is created `pending` with no credential, and one is minted and
    emailed on approval. So the invariant is no longer "post-OTP goes to
    password"; it is that each door hands off to a screen its own order
    contains. Routing a physician to a step that is not in their order would
    strand them exactly the way skipping the password step used to.
    """
    src = _WIZARD.read_text(encoding="utf-8")
    m = re.search(
        r'setStep\(signupKind === "physician" \? "([a-zA-Z]+)" : "([a-zA-Z]+)"\);', src)
    assert m, "the post-OTP branch changed shape; re-check where each door goes"
    physician_next, other_next = m.group(1), m.group(2)
    assert physician_next == "cv", (
        f"post-OTP sends a physician to {physician_next!r}; v2 §2 goes to the CV screen"
    )
    assert other_next == "password", (
        f"post-OTP sends the short signup to {other_next!r}; those accounts open "
        "immediately and still choose their own password"
    )

    i = src.index("function orderFor")
    order_fn = src[i:src.index("\n}\n", i)]
    physician_order = re.search(
        r'return \["identity", "verify", "cv", "review", "attestations", "submitted"\];',
        order_fn)
    assert physician_order, "the physician order is not the v2 order"
    assert f'"{physician_next}"' in physician_order.group(0)


def test_the_password_step_is_still_in_the_orders_that_have_one():
    """v2 removed it from the physician path ONLY. Member mode and the
    advisor/referrer short signup provision an account that opens immediately,
    so they still choose a credential — and a change that dropped it from those
    would leave an open account nobody can sign in to."""
    src = _WIZARD.read_text(encoding="utf-8")
    i = src.index("function orderFor")
    order = src[i:src.index("\n}\n", i)]
    assert '["credentials", "attestations", "verify", "password", "ascSuccess"]' in order
    assert 'const head: StepKey[] = ["identity", "verify", "password"];' in order


def test_each_credentials_screen_gates_only_on_its_own_fields():
    """Screen 1's Continue must not be dead because a licence number it never
    displayed is empty."""
    src = _STEPS.read_text(encoding="utf-8")
    assert "const identityValid" in src and "const trainingValid" in src, (
        "the phase split lost its per-phase validation, so screen 1 gates on "
        "fields only screens 2 and 3 show"
    )
    assert "phase === 3 ? true" in src, "the optional screen must never block"
