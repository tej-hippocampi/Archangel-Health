"""Consent drafts preserve evidence and clinical signup requires seven booleans."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from tests import _asclepius as A
from routers import onboarding
from team_store import TeamStore

SEVEN = {
    "consentCredentialShare": True, "attestIndependentJudgment": True,
    "ipAssignment": True, "noPhi": True, "attestConfidentiality": True,
    "attestNoDisciplinaryAction": True, "attestWorkQuality": True,
}
COMPLETE = {**SEVEN, "signedInitials": "TP", "futureEvidence": {"version": 3}}


@pytest.fixture
def draft(tmp_path, monkeypatch):
    portal = A.fresh_store()
    team = TeamStore(str(tmp_path / "team.db"))
    monkeypatch.setattr(A.app.state, "team_store", team, raising=False)
    monkeypatch.setattr(A.app.state, "asclepius_store", portal, raising=False)
    monkeypatch.setattr(onboarding, "_email_configured", lambda: True)
    sent = []
    async def send(*args, **kw):
        sent.append(args)
        return True
    monkeypatch.setattr(onboarding, "send_html_email", send)
    invite = team.create_health_system_invite(invite_base_url="http://localhost")
    hs_id = invite["health_system_id"]
    token = invite["onboarding_url"].rsplit("/", 1)[-1]
    email = "attestations@example.test"
    team.update_health_system_director_identity(hs_id, first_name="Tej", last_name="Patel", email=email)
    team.set_health_system_product(hs_id, "asclepius")
    team.create_otp_challenge(hs_id, email, "123456")
    assert team.verify_otp_challenge(hs_id, email, "123456")
    team.upsert_asclepius_person(hs_id, email=email, full_name="Tej Patel", clinical_role="physician", is_director=True)
    team.save_asclepius_credentials(hs_id, email, {"fullLegalName": "Tej Patel", "primarySpecialty": "Nephrology"})
    member_token = team.issue_asclepius_member_token(hs_id, email)
    return team, portal, hs_id, email, token, member_token, sent


@pytest.mark.parametrize("flow", ["asclepius", "member"])
def test_a_partial_attestation_post_does_not_erase_the_stored_seven(draft, flow):
    team, _, hs_id, email, token, member_token, _ = draft
    team.save_asclepius_attestations(hs_id, email, COMPLETE)
    response = TestClient(A.app).post(f"/api/onboarding/{flow}/attestations", json={
        "token": token if flow == "asclepius" else member_token,
        "attestations": {"ipAssignment": True, "signedInitials": " tp "},
    })
    assert response.status_code == 200, response.text
    assert team.get_asclepius_person(hs_id, email)["attestations"] == COMPLETE


@pytest.mark.parametrize("flow", ["asclepius", "member"])
@pytest.mark.parametrize("field", list(SEVEN))
@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_finishing_requires_all_seven_to_be_true_not_merely_present(draft, flow, field, value):
    team, portal, hs_id, email, token, member_token, sent = draft
    invalid = {**COMPLETE, field: value}
    if value is None:
        invalid.pop(field)
    team.save_asclepius_attestations(hs_id, email, invalid)
    response = TestClient(A.app).post(f"/api/onboarding/{flow}/finish", json={
        "token": token if flow == "asclepius" else member_token,
    })
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Sign the attestations before finishing."
    assert portal.get_user_by_email(email) is None
    assert not team.get_asclepius_person(hs_id, email).get("onboarding_completed_at")
    assert sent == []


def test_initials_alone_cannot_finish(draft):
    team, _, hs_id, email, token, _, _ = draft
    team.save_asclepius_attestations(hs_id, email, {"signedInitials": "TP"})
    response = TestClient(A.app).post("/api/onboarding/asclepius/finish", json={"token": token})
    assert response.status_code == 400
    assert "attestations" in response.json()["detail"]


@pytest.mark.parametrize("incoming", [None, {}, {"ipAssignment": False}, {"signedInitials": "XY"}])
def test_a_re_onboard_that_omits_attestations_does_not_wipe_them(draft, incoming):
    _, portal, _, email, _, _, _ = draft
    first = portal.provision_user(email=email, attestations=COMPLETE)
    again = portal.provision_user(email=email, attestations=incoming)
    assert again["id"] == first["id"]
    assert json.loads(again["attestations_json"]) == {**COMPLETE, **(incoming or {})}


def test_parallel_partial_saves_preserve_all_keys_and_explicit_false(draft):
    team, _, hs_id, email, *_ = draft
    team.save_asclepius_attestations(hs_id, email, COMPLETE)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: team.save_asclepius_attestations(hs_id, email, {f"new{i}": i}), range(8)))
    team.save_asclepius_attestations(hs_id, email, {"noPhi": False})
    assert team.get_asclepius_person(hs_id, email)["attestations"] == {
        **COMPLETE, **{f"new{i}": i for i in range(8)}, "noPhi": False,
    }


@pytest.mark.parametrize("stored", ['{"broken":', '[]'])
def test_corrupt_stored_evidence_is_not_silently_overwritten(draft, stored):
    team, _, hs_id, email, *_ = draft
    with team._conn() as conn:
        conn.execute("UPDATE asclepius_people SET attestations_json = ? WHERE health_system_id = ?", (stored, hs_id))
    with pytest.raises(ValueError):
        team.save_asclepius_attestations(hs_id, email, COMPLETE)
    with team._conn() as conn:
        assert conn.execute("SELECT attestations_json FROM asclepius_people WHERE health_system_id = ?", (hs_id,)).fetchone()[0] == stored


def test_failed_save_rolls_back_and_retry_preserves_prior_evidence(draft):
    import sqlite3
    team, _, hs_id, email, *_ = draft
    team.save_asclepius_attestations(hs_id, email, COMPLETE)
    with team._conn() as conn:
        conn.execute("CREATE TRIGGER fail_attestation_write BEFORE UPDATE OF attestations_json "
                     "ON asclepius_people BEGIN SELECT RAISE(ABORT, 'test write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='test write failure'):
        team.save_asclepius_attestations(hs_id, email, {"newEvidence": "kept"})
    assert team.get_asclepius_person(hs_id, email)["attestations"] == COMPLETE
    with team._conn() as conn:
        conn.execute("DROP TRIGGER fail_attestation_write")
    for _ in range(2):
        team.save_asclepius_attestations(hs_id, email, {"newEvidence": "kept"})
    assert team.get_asclepius_person(hs_id, email)["attestations"] == {**COMPLETE, "newEvidence": "kept"}
