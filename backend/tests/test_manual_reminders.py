"""Manual nudges: explicit approval, audience, link safety, and send retries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from asclepius import manual_reminders as R
from asclepius.exam_reminder import iso, utcnow
import email_utils
import manual_reminder_emails as templates
import realm
from team_store import TeamStore
from tests import _asclepius as A

client = TestClient(A.app)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = A.fresh_store()
    team = TeamStore(str(tmp_path / "team.db"))
    monkeypatch.setattr(A.app.state, "team_store", team)
    monkeypatch.setenv("ASCLEPIUS_PORTAL_URL", "https://app.archangelhealth.ai")
    monkeypatch.setenv("LANDING_URL", "https://www.archangelhealth.ai")
    monkeypatch.setenv("ASCLEPIUS_MANUAL_REMINDERS_ENABLED", "1")
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(email_utils, "is_email_dev_mode", lambda: False)
    messages = []

    async def send(to, subject, html, **kwargs):
        messages.append({"to": to, "subject": subject, "html": html, **kwargs})
        kwargs["delivery_info"].update(outcome="accepted", provider_id="mock-provider")
        return True, "accepted"

    monkeypatch.setattr(email_utils, "send_html_email_with_reason", send)
    admin = A.make_user(store, role="admin")
    return store, team, admin, messages


def applicant(store, **changes):
    user = A.make_user(store, tier=None)
    fields = {"verification_status": "pending", "cv_asset_sha": "stored-cv", **changes}
    with store._conn() as conn:
        conn.execute("UPDATE users SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?", (*fields.values(), user["id"]))
    return store.get_user_by_id(user["id"])


def signup(team, email="wizard@example.com", step=1):
    invite = team.create_health_system_invite(director_email=email,
                                              invite_base_url="https://www.archangelhealth.ai", product="asclepius")
    token = invite["onboarding_url"].rsplit("/", 1)[-1]
    hs = team.get_health_system_by_onboarding_token(token)
    with team._conn() as conn:
        conn.execute("UPDATE health_systems SET onboarding_step=? WHERE id=?", (step, hs["id"]))
    return team.get_health_system_by_id(hs["id"]), token


def preview(env, kind="examination", **kwargs):
    return R.preview(env[0], env[1], kind, env[2]["id"], **kwargs)


def send(env, reminder_id):
    return asyncio.run(R.send(env[0], env[1], reminder_id, env[2]["id"]))


def test_preview_cannot_send_or_change_automatic_stamps(env):
    store, team, admin, messages = env
    user = applicant(store, nudge_exam_sent_at="2026-09-12")
    p = preview(env)
    assert len(p["recipients"]) == 1 and not messages
    result = send(env, p["recipients"][0]["id"])
    assert result["status"] == "accepted"
    assert store.get_user_by_id(user["id"])["nudge_exam_sent_at"] == "2026-09-12"
    assert messages[0]["reply_to"] == "tejpatel@berkeley.edu"
    assert 'https://app.archangelhealth.ai/asclepius#examination' in messages[0]["html"]
    assert "email and password" in messages[0]["text_body"]
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM exam_reminder_deliveries").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["examination", "wizard"])
def test_each_email_uses_its_recipients_actual_signup_name(env, kind):
    people = [("first@example.com", "Asha Sharma"), ("second@example.com", "Liam O’Neil")]
    for email, name in people:
        if kind == "examination":
            applicant(env[0], email=email, full_name=name)
        else:
            hs, _ = signup(env[1], email=email)
            first, last = name.split(" ", 1)
            with env[1]._conn() as conn:
                conn.execute("UPDATE health_systems SET director_first_name=?,director_last_name=? WHERE id=?", (first, last, hs["id"]))
    p = preview(env, kind)
    assert "[Name]" not in p["template"]["html"]
    assert "Hi " + p["recipients"][0]["name"] + "," in p["template"]["text"]
    assert not env[3], "Inspecting personalized previews must never send email"
    for recipient in p["recipients"]:
        mail = p["recipient_templates"][recipient["id"]]
        assert "Hi " + dict(people)[recipient["email"]] + "," in mail["text"]
        for other_email, other_name in people:
            if other_email != recipient["email"]:
                assert other_name not in mail["html"] and other_name not in mail["text"]
        assert send(env, recipient["id"])["status"] == "accepted"
    assert len(env[3]) == 2
    expected = dict(people)
    for message in env[3]:
        greeting = "Hi " + expected[message["to"]] + ","
        assert greeting in message["html"] and greeting in message["text_body"]
        assert "[Name]" not in message["html"] and "[Name]" not in message["text_body"]


def test_send_uses_current_saved_name_and_never_snapshot_placeholder(env):
    user = applicant(env[0], full_name="Original Name")
    key = preview(env)["recipients"][0]["id"]
    with env[0]._conn() as conn:
        conn.execute("UPDATE users SET full_name='Updated Name' WHERE id=?", (user["id"],))
    assert send(env, key)["status"] == "accepted"
    assert "Hi Updated Name," in env[3][0]["html"]
    assert "Original Name" not in env[3][0]["html"]


def test_no_saved_name_uses_neutral_greeting(env):
    applicant(env[0], full_name=None)
    key = preview(env)["recipients"][0]["id"]
    assert send(env, key)["status"] == "accepted"
    assert "Hello," in env[3][0]["text_body"]
    assert "[Name]" not in env[3][0]["text_body"]


@pytest.mark.parametrize("changes,state", [({"active": 0}, None), ({"verification_status": "approved"}, None),
    ({"account_kind": "advisor"}, None), ({"cv_asset_sha": None}, None), ({}, "submitted"), ({}, "unknown")])
def test_examination_cohort_excludes_ineligible(env, changes, state):
    user = applicant(env[0], **changes)
    if state:
        env[0].set_tutorial_state(user["id"], {"exam": {"state": state}})
    assert preview(env)["recipients"] == []


def test_filed_exam_evidence_wins_over_stale_tutorial(env, monkeypatch):
    applicant(env[0])
    monkeypatch.setattr(env[0], "list_credentialing_exams", lambda uid: [{"exam_id": "filed"}])
    assert not preview(env)["recipients"]


def test_in_progress_eligible_and_completion_after_preview_skips(env):
    user = applicant(env[0])
    env[0].set_tutorial_state(user["id"], {"exam": {"state": "in_progress"}})
    reminder_id = preview(env)["recipients"][0]["id"]
    env[0].set_tutorial_state(user["id"], {"exam": {"state": "submitted"}})
    assert send(env, reminder_id)["status"] == "skipped"
    assert not env[3]


def test_wizard_duplicates_choose_most_progress_and_preserve_live_link(env):
    hs1, token1 = signup(env[1], step=1)
    hs2, token2 = signup(env[1], step=3)
    with env[1]._conn() as conn:
        for hs, name in ((hs1, "Older Name"), (hs2, "Current Name")):
            first, last = name.split()
            conn.execute("UPDATE health_systems SET director_first_name=?,director_last_name=? WHERE id=?",
                         (first, last, hs["id"]))
    p = preview(env, "wizard", email="wizard@example.com")
    assert len(p["recipients"]) == 1 and p["recipients"][0]["target"] == hs2["id"]
    assert "Hi Current Name," in p["recipient_templates"][p["recipients"][0]["id"]]["text"]
    assert token1 not in json.dumps(p) and token2 not in json.dumps(p)
    assert not env[3]
    assert send(env, p["recipients"][0]["id"])["status"] == "accepted"
    assert "/onboard/" + token2 in env[3][0]["html"]
    assert "Hi Current Name," in env[3][0]["text_body"]
    assert "Older Name" not in env[3][0]["html"]
    assert env[1].get_health_system_by_onboarding_token(token2)["onboarding_step"] == 3
    assert env[1].get_health_system_by_onboarding_token(token1)


def test_invited_member_never_receives_directors_name(env):
    team = env[1]
    hs, _ = signup(team, email="director@example.com")
    team.upsert_asclepius_person(hs["id"], email="director@example.com",
        full_name="Director Smith", clinical_role="director", is_director=True)
    team.upsert_asclepius_person(hs["id"], email="member@example.com",
        full_name="Member James", clinical_role="attending", is_director=False)
    team.issue_asclepius_member_token(hs["id"], "member@example.com")
    p = preview(env, "wizard", email="member@example.com")
    assert len(p["recipients"]) == 1
    member = p["recipients"][0]
    assert member["member"] and member["name"] == "Member James"
    assert "Hi Member James," in p["recipient_templates"][member["id"]]["text"]
    assert send(env, member["id"])["status"] == "accepted"
    assert env[3][0]["to"] == "member@example.com"
    assert "Hi Member James," in env[3][0]["text_body"]
    assert "Director Smith" not in env[3][0]["html"]


def test_expired_wizard_link_rotates_only_on_send(env):
    hs, token = signup(env[1])
    with env[1]._conn() as conn:
        conn.execute("UPDATE health_systems SET onboarding_token_expires_at='2020-01-01' WHERE id=?", (hs["id"],))
    p = preview(env, "wizard")
    assert env[1].get_health_system_by_onboarding_token(token)
    assert send(env, p["recipients"][0]["id"])["status"] == "accepted"
    assert token not in env[3][0]["html"]
    assert env[1].get_health_system_by_id(hs["id"])["onboarding_step"] == 1


def test_provisioned_signup_and_completed_after_preview_do_not_send(env):
    hs, token = signup(env[1])
    p = preview(env, "wizard")
    A.make_user(env[0], email="wizard@example.com")
    assert not preview(env, "wizard")["recipients"]
    assert send(env, p["recipients"][0]["id"])["status"] == "skipped"
    assert not env[3]


@pytest.mark.parametrize("expired", [False, True])
def test_sandbox_director_links_keep_realm_when_reused_or_renewed(env, expired):
    with realm.scoped("sandbox"):
        hs, token = signup(env[1])
        if expired:
            with env[1]._conn() as conn:
                conn.execute("UPDATE health_systems SET onboarding_token_expires_at='2020-01-01' WHERE id=?", (hs["id"],))
        recipient = R.candidates(env[0], env[1], "wizard")[0]
        url = R.wizard_url(env[1], recipient)
        assert "realm=sandbox" in url
        assert (token in url) is not expired


def test_unusable_portal_configuration_fails_without_provider_call(env, monkeypatch):
    applicant(env[0])
    key = preview(env)["recipients"][0]["id"]
    monkeypatch.setenv("ASCLEPIUS_PORTAL_URL", "http://wrong.example")
    assert send(env, key)["status"] == "failed"
    assert not env[3]


def test_concurrent_claims_and_two_previews_hold_duplicates(env):
    applicant(env[0])
    a = preview(env)["recipients"][0]["id"]
    b = preview(env)["recipients"][0]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda key: R.claim(env[0], key, env[2]["id"]), [a, b]))
    assert sum(claimed for _, claimed in claims) == 1
    winner = next(row["id"] for row, claimed in claims if claimed)
    assert R.claim(env[0], winner, env[2]["id"])[1] is False


def test_retry_after_response_loss_returns_recorded_result(env):
    applicant(env[0])
    reminder_id = preview(env)["recipients"][0]["id"]
    assert send(env, reminder_id)["status"] == "accepted"
    assert send(env, reminder_id)["status"] == "accepted"
    assert len(env[3]) == 1
    second = preview(env)["recipients"][0]["id"]
    assert send(env, second)["status"] == "held"


def test_uncertain_provider_submission_blocks_later_attempts(env, monkeypatch):
    applicant(env[0])
    async def timeout(*args, **kwargs):
        raise TimeoutError("possible acceptance")
    monkeypatch.setattr(email_utils, "send_html_email_with_reason", timeout)
    reminder_id = preview(env)["recipients"][0]["id"]
    assert send(env, reminder_id)["status"] == "unknown"
    second = preview(env)["recipients"][0]["id"]
    assert send(env, second)["status"] == "held"


def test_template_approval_switch_is_closed_by_default(env, monkeypatch):
    applicant(env[0])
    monkeypatch.delenv("ASCLEPIUS_MANUAL_REMINDERS_ENABLED")
    p = preview(env)
    assert not p["can_send"]
    with pytest.raises(HTTPException) as err:
        send(env, p["recipients"][0]["id"])
    assert err.value.status_code == 409 and not env[3]


@pytest.mark.parametrize("column,value", [("template_version", "old"), ("created_at", iso(utcnow()-timedelta(hours=1)))])
def test_stale_previews_require_fresh_review(env, column, value):
    applicant(env[0])
    key = preview(env)["recipients"][0]["id"]
    with env[0]._conn() as conn:
        conn.execute("UPDATE manual_onboarding_reminders SET " + column + "=? WHERE id=?", (value, key))
    with pytest.raises(HTTPException) as err:
        send(env, key)
    assert err.value.status_code == 409


def test_routes_admin_only_and_preview_bound_to_actor(env):
    user = applicant(env[0])
    path = "/api/asclepius/admin/manual-reminders/preview"
    assert client.post(path, json={"kind": "wizard"}).status_code in (401, 403)
    assert client.post(path, json={"kind": "wizard"}, headers=A.headers_for(user)).status_code == 403
    response = client.post(path, json={"kind": "examination"}, headers=A.headers_for(env[2]))
    assert response.status_code == 200
    key = response.json()["recipients"][0]["id"]
    other = A.make_user(env[0], role="admin")
    assert client.post(f"/api/asclepius/admin/manual-reminders/{key}/send", headers=A.headers_for(other)).status_code == 403
    response = client.post(f"/api/asclepius/admin/manual-reminders/{key}/send", headers=A.headers_for(env[2]))
    assert response.status_code == 200 and response.json()["status"] == "accepted"


def test_templates_escape_untrusted_names_and_url():
    for kind in templates.COPY:
        mail = templates.render(kind, '<img src=x onerror="bad()">', 'https://example.test/?a=1&b="quoted"')
        assert '<img src=x' not in mail["html"]
        assert '&lt;img' in mail["html"] and '&amp;b=&quot;' in mail["html"]
        assert templates.REPLY_TO in mail["text"]
