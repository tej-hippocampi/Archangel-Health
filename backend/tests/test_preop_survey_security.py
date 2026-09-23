"""Survey links keep their one-click UI while enforcing patient ownership."""
from datetime import datetime, timedelta
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import main
import patient_session
import realm
from team_store import TeamStore
from tests._role_auth import tenant_token


@pytest.fixture
def survey_env(tmp_path, monkeypatch):
    db = tmp_path / "survey.db"
    monkeypatch.setenv("TEAM_DB_PATH", str(db))
    monkeypatch.setenv("ENFORCE_PATIENT_AUTH", "1")
    monkeypatch.setenv("ENV", "test")
    store = TeamStore(db_path=str(db))
    from token_revocation import is_revoked
    is_revoked("synthetic-initialization")
    patients = {
        "synthetic-survey": {
            "name": "Synthetic Patient", "health_system_id": "synthetic-hospital",
            "email": "synthetic@example.org", "pipeline_type": "pre_op",
            "structured_data": {"procedure_date": (datetime.utcnow() + timedelta(hours=72)).isoformat()},
        },
        "synthetic-other": {"name": "Other", "health_system_id": "other-hospital", "structured_data": {}},
    }
    monkeypatch.setattr(main, "_patient_store", patients)
    monkeypatch.setattr(main, "_team_store", store)
    return TestClient(main.app), store, patients


def test_anonymous_and_foreign_survey_calls_preserve_original_data(survey_env):
    client, store, _ = survey_env
    with store._conn() as conn:
        before = list(conn.iterdump())
    for headers in ({}, {"Authorization": "Bearer " + tenant_token(health_system_id="other-hospital")}):
        assert client.get("/api/preop-survey/questions", params={"window": "t96", "patient_id": "synthetic-survey"}, headers=headers).status_code == 404
        assert client.post("/api/preop-survey/submit", json={"window": "t96", "patient_id": "synthetic-survey", "answers": []}, headers=headers).status_code == 404
    with store._conn() as conn:
        assert list(conn.iterdump()) == before


def test_own_patient_can_read_and_submit_but_cannot_switch_patient(survey_env):
    client, store, _ = survey_env
    client.cookies.set("pt_session", patient_session.create_patient_session("synthetic-survey", "synthetic-hospital"))
    assert client.get("/api/preop-survey/questions?window=t96&patient_id=synthetic-survey").status_code == 200
    assert client.get("/api/preop-survey/questions?window=t96&patient_id=synthetic-other").status_code == 404
    response = client.post("/api/preop-survey/submit", json={"window": "t96", "patient_id": "synthetic-survey", "answers": []})
    assert response.status_code == 200, response.text
    assert store.get_survey_response("synthetic-survey", -4, survey_type="preop") is not None


def test_survey_email_link_opens_exact_existing_ui_and_removes_credential(survey_env, monkeypatch):
    import asyncio
    import html
    import re
    client, _, _ = survey_env
    messages = []

    async def capture_email(to, subject, body):
        messages.append(body)
        return True

    monkeypatch.setattr(main, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(main, "_send_html_email", capture_email)
    asyncio.run(main._run_preop_survey_outreach())
    assert len(messages) == 1
    link = html.unescape(re.search(r"href='([^']+)'", messages[0]).group(1))
    url = urlsplit(link)
    params = parse_qs(url.query)
    claims = patient_session._decode(params["k"][0])
    assert claims["pid"] == "synthetic-survey"
    assert 23 * 3600 < claims["exp"] - time.time() <= 24 * 3600 + 1
    first = client.get(url.path + "?" + url.query, follow_redirects=False)
    assert first.status_code == 302
    assert "k=" not in first.headers["location"]
    assert first.headers["referrer-policy"] == "no-referrer"
    assert parse_qs(urlsplit(first.headers["location"]).query) == {"patient": ["synthetic-survey"], "window": ["t96"]}
    assert "HttpOnly" in first.headers["set-cookie"]
    rendered = client.get(first.headers["location"])
    expected = Path(main.__file__).parent.parent / "frontend/preop-survey.html"
    assert rendered.status_code == 200 and rendered.text == expected.read_text()
    assert client.get("/api/preop-survey/questions?window=t96&patient_id=synthetic-survey").status_code == 200
    assert TestClient(main.app).get(url.path + "?" + url.query).status_code == 404


def test_survey_page_does_not_accept_foreign_or_expired_entry(survey_env):
    client, _, _ = survey_env
    token = patient_session.create_entry_token("synthetic-other", "other-hospital")
    page = "/static/preop-survey.html?window=t96&patient=synthetic-survey&k="
    assert client.get(page + token).status_code == 404
    claims = patient_session._decode(patient_session.create_entry_token("synthetic-survey", "synthetic-hospital"))
    claims["exp"] = 1
    assert client.get(page + patient_session._encode(claims)).status_code == 404


def test_generic_question_catalog_and_own_clinician_flow_remain_available(survey_env):
    client, _, _ = survey_env
    generic = client.get("/api/preop-survey/questions?window=t96")
    assert generic.status_code == 200 and generic.json()["procedure_date"] is None
    headers = {"Authorization": "Bearer " + tenant_token(health_system_id="synthetic-hospital")}
    assert client.get("/api/preop-survey/questions?window=t96&patient_id=synthetic-survey", headers=headers).status_code == 200


def test_interactive_entry_tokens_keep_existing_five_minute_lifetime():
    claims = patient_session._decode(patient_session.create_entry_token("synthetic-survey", "synthetic-hospital"))
    assert 290 <= claims["exp"] - time.time() <= 301


@pytest.mark.parametrize("email_delivered", [True, False])
def test_sandbox_roster_cannot_schedule_external_outreach_or_throttle_live(survey_env, monkeypatch, email_delivered):
    import asyncio
    from types import SimpleNamespace

    client, store, patients = survey_env
    patients["synthetic-survey"]["phone"] = "+15555550123"
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-sandbox-password")
    monkeypatch.setattr(main.app.state, "last_preop_outreach_mono", 0.0, raising=False)
    monkeypatch.setattr(main.app.state, "preop_outreach_inline_task", None, raising=False)
    calls = []

    async def email(to, subject, body):
        calls.append(("email", realm.current()))
        return email_delivered

    def sms(**kwargs):
        calls.append(("sms", realm.current()))

    monkeypatch.setattr(main, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(main, "_send_html_email", email)
    monkeypatch.setattr(main, "TwilioClient", lambda: SimpleNamespace(send=sms))
    with realm.scoped(realm.SANDBOX):
        token = tenant_token(health_system_id="synthetic-hospital")
    response = client.get("/api/patients", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200, response.text
    assert calls == []
    assert main.app.state.preop_outreach_inline_task is None
    assert main.app.state.last_preop_outreach_mono == 0.0
    assert not store.has_survey_send("synthetic-survey", -4)

    async def live_roster_trigger():
        assert realm.current() == realm.LIVE
        await main._maybe_trigger_preop_outreach(main.app)
        task = main.app.state.preop_outreach_inline_task
        assert task is not None
        await task
        await main._maybe_trigger_preop_outreach(main.app)
        assert main.app.state.preop_outreach_inline_task is task

    asyncio.run(live_roster_trigger())
    assert calls == [("email", "live")] + ([] if email_delivered else [("sms", "live")])
    assert store.has_survey_send("synthetic-survey", -4)


def test_patient_cookie_and_entry_tokens_cannot_cross_realms(survey_env, monkeypatch):
    client, _, _ = survey_env
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-sandbox-password")
    live_cookie = patient_session.create_patient_session("synthetic-survey", "synthetic-hospital")
    live_entry = patient_session.create_entry_token("synthetic-survey", "synthetic-hospital")
    with realm.scoped("sandbox"):
        sandbox_cookie = patient_session.create_patient_session("synthetic-survey", "synthetic-hospital")
        sandbox_entry = patient_session.create_entry_token("synthetic-survey", "synthetic-hospital")
        sandbox_db = patient_session._db_path()
    assert sandbox_db != patient_session._db_path()
    for token, source, foreign in ((live_cookie, "live", "sandbox"), (sandbox_cookie, "sandbox", "live")):
        client.cookies.set("pt_session", token)
        client.cookies.set("pt_session_sandbox", token)
        path = "/api/preop-survey/questions?window=t96&patient_id=synthetic-survey"
        assert client.get(path, headers={realm.HEADER: source}).status_code == 200
        assert client.get(path, headers={realm.HEADER: foreign}).status_code == 404
    client.cookies.clear()
    page = "/static/preop-survey.html?window=t96&patient=synthetic-survey&k="
    assert client.get(page + live_entry, headers={realm.HEADER: "sandbox"}).status_code == 404
    assert client.get(page + sandbox_entry, headers={realm.HEADER: "live"}).status_code == 404


def test_legacy_patient_cookie_is_only_accepted_in_live_realm(survey_env, monkeypatch):
    client, _, _ = survey_env
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-sandbox-password")
    claims = patient_session._decode(patient_session.create_patient_session("synthetic-survey", "synthetic-hospital"))
    claims.pop("realm")
    client.cookies.set("pt_session", patient_session._encode(claims))
    client.cookies.set("pt_session_sandbox", patient_session._encode(claims))
    path = "/api/preop-survey/questions?window=t96&patient_id=synthetic-survey"
    assert client.get(path).status_code == 200
    assert client.get(path, headers={realm.HEADER: "sandbox"}).status_code == 404


def test_sandbox_email_redirect_html_and_survey_write_stay_in_sandbox(survey_env, monkeypatch):
    import asyncio
    import html
    import re
    from realm_patient_store import RealmPatientStore
    from scripts.data_inventory import compare, snapshot

    client, live_store, original_patients = survey_env
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-sandbox-password")
    patients = RealmPatientStore()
    with realm.scoped(realm.SANDBOX):
        patients.update(original_patients)
        sandbox_store = TeamStore(patient_session._db_path())
    stores = {realm.LIVE: live_store, realm.SANDBOX: sandbox_store}
    monkeypatch.setattr(main, "_patient_store", patients)
    monkeypatch.setattr(main, "_team_store", realm.RealmProxy(lambda: stores[realm.current()], "TeamStore"))
    messages = []

    async def capture_email(to, subject, body):
        messages.append(body)
        return True

    monkeypatch.setattr(main, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(main, "_send_html_email", capture_email)
    before_live = snapshot(live_store.db_path)
    with realm.scoped(realm.SANDBOX):
        asyncio.run(main._run_preop_survey_outreach())
    assert len(messages) == 1
    url = urlsplit(html.unescape(re.search(r"href='([^']+)'", messages[0]).group(1)))
    assert parse_qs(url.query)["realm"] == ["sandbox"]
    entry_path = url.path + "?" + url.query
    first = client.get(entry_path, follow_redirects=False)
    assert first.status_code == 302
    clean = first.headers["location"]
    assert parse_qs(urlsplit(clean).query) == {
        "realm": ["sandbox"], "window": ["t96"], "patient": ["synthetic-survey"]}
    assert "pt_session_sandbox" in client.cookies and "pt_session" not in client.cookies
    assert first.headers["referrer-policy"] == "no-referrer"
    rendered = client.get(clean)
    expected = Path(main.__file__).parent.parent / "frontend/preop-survey.html"
    assert rendered.status_code == 200 and rendered.text == expected.read_text()
    headers = {realm.HEADER: "sandbox"}
    query = "/api/preop-survey/questions?window=t96&patient_id=synthetic-survey"
    assert client.get(query, headers=headers).status_code == 200
    assert client.get(query).status_code == 404
    submitted = client.post("/api/preop-survey/submit", headers=headers, json={
        "patient_id": "synthetic-survey", "window": "t96", "answers": []})
    assert submitted.status_code == 200
    assert sandbox_store.get_survey_response("synthetic-survey", -4, survey_type="preop")
    assert compare(before_live, snapshot(live_store.db_path)) == []
    other_browser = TestClient(main.app)
    try:
        assert other_browser.get(entry_path).status_code == 404
    finally:
        other_browser.close()


@pytest.mark.parametrize("logout_realm", ["live", "sandbox"])
def test_patient_cookies_coexist_and_logout_revokes_only_selected_realm(survey_env, monkeypatch, logout_realm):
    client, _, _ = survey_env
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-sandbox-password")
    tokens = {}
    for selected in ("live", "sandbox"):
        with realm.scoped(selected):
            entry = patient_session.create_entry_token("synthetic-survey", "synthetic-hospital")
        response = client.get("/static/preop-survey.html", params={
            "window": "t96", "patient": "synthetic-survey", "k": entry, "realm": selected},
            follow_redirects=False)
        assert response.status_code == 302
        name = "pt_session_sandbox" if selected == "sandbox" else "pt_session"
        tokens[selected] = client.cookies[name]
    query = "/api/preop-survey/questions?window=t96&patient_id=synthetic-survey"
    for selected in ("live", "sandbox"):
        assert client.get(query, headers={realm.HEADER: selected}).status_code == 200
    assert client.post("/api/patient/logout", headers={realm.HEADER: logout_realm}).status_code == 200
    logout_name = "pt_session_sandbox" if logout_realm == "sandbox" else "pt_session"
    other = "live" if logout_realm == "sandbox" else "sandbox"
    assert logout_name not in client.cookies
    assert client.get(query, headers={realm.HEADER: other}).status_code == 200
    # Cookie deletion alone does not revoke a copied token: explicitly replay it.
    client.cookies.set(logout_name, tokens[logout_realm], domain="testserver.local", path="/")
    assert client.get(query, headers={realm.HEADER: logout_realm}).status_code == 404
    with realm.scoped(other):
        assert patient_session.decode_patient_session(tokens[other]) is not None


@pytest.mark.parametrize("selected", ["live", "sandbox"])
def test_survey_javascript_preserves_realm_on_reads_events_and_submit(selected):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the shipped survey client")
    path = Path(main.__file__).parent.parent / "frontend/preop-survey.js"
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const calls = [];
let submit;
const root = {innerHTML: '', querySelector() {return null;}};
const elements = {
  surveyRoot: root,
  preopSurveyForm: {addEventListener(event, callback) {submit = callback;}},
  statusMsg: {}, submitBtn: {},
};
const selected = process.argv[2];
const search = '?window=t96&patient=synthetic-survey' + (selected === 'sandbox' ? '&realm=sandbox' : '');
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  URLSearchParams,
  window: {location: {origin: 'https://synthetic.invalid', search}},
  document: {getElementById(id) {return elements[id];}},
  fetch: async (url, options) => {
    calls.push({url, ...options});
    return {ok: true, json: async () => ({questions: [], tier: 'green'})};
  },
});
(async () => {
  await new Promise(setImmediate);
  assert.equal(typeof submit, 'function');
  await submit({preventDefault() {}});
  assert.equal(calls.length, 3);
  assert.ok(calls[0].url.includes('/api/preop-survey/questions'));
  assert.ok(calls[1].url.endsWith('/events'));
  assert.ok(calls[2].url.endsWith('/api/preop-survey/submit'));
  for (const call of calls) assert.equal(call.headers['X-Asclepius-Realm'], selected);
  for (const call of calls.slice(1)) {
    assert.equal(call.method, 'POST');
    assert.equal(call.headers['Content-Type'], 'application/json');
  }
  assert.equal(JSON.parse(calls[2].body).patient_id, 'synthetic-survey');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script, str(path), selected], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
