"""Exercise the shipped health-system portal and real APIs in Chromium."""
import base64
import mimetypes
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from tests import _asclepius as A
from routers import asclepius_provider as P

API = "/api/asclepius/hs"
PASSWORD = "harbor-thistle-meadow-41"


@pytest.fixture(params=[1440, 390], ids=["desktop", "mobile"])
def portal(request, monkeypatch, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    store = A.fresh_store()
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"a" * 32).decode())
    monkeypatch.setattr(P.secrets, "randbelow", lambda n: 123456)
    monkeypatch.setattr(P, "_notify_hs_signup", lambda *a, **kw: None)
    monkeypatch.setattr(P, "_notify_hs_application", lambda *a, **kw: None)
    failures, errors, calls, held = {}, [], [], {}
    client = TestClient(A.app, base_url="https://testserver")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": request.param, "height": 900})
        page = context.new_page()
        page.set_default_timeout(6000)
        page.on("pageerror", lambda e: errors.append(str(e)))

        def dispatch(route):
            req = route.request
            url = urlsplit(req.url)
            if url.netloc == "archangelhealth.ai" and not url.path.startswith("/api/"):
                dist = Path(__file__).resolve().parents[2] / "landing" / "dist"
                asset = dist / ("index.html" if url.path in ("/", "/health-systems") else url.path.lstrip("/"))
                if asset.is_file() and asset.resolve().is_relative_to(dist.resolve()):
                    route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(str(asset))[0] or "application/octet-stream")
                    return
            key = (req.method, url.path)
            calls.append(key)
            if key in failures:
                status = failures.pop(key)
                if status == "offline":
                    route.abort("internetdisconnected")
                else:
                    route.fulfill(status=status, json={"detail": "Temporary service problem. Please try again."})
                return
            # The browser must carry its own cookie; the test client's cookie jar
            # must not disguise a broken Set-Cookie/session handoff.
            client.cookies.clear()
            response = client.request(req.method, url.path + ("?" + url.query if url.query else ""),
                                      content=req.post_data_buffer,
                                      headers={k: v for k, v in req.headers.items() if k != "host"})
            if key in held:
                held[key] = (route, response)
                return
            route.fulfill(status=response.status_code, body=response.content,
                          headers={k: v for k, v in response.headers.items()
                                   if k not in ("content-length", "content-encoding")})

        context.route("**/*", dispatch)
        yield SimpleNamespace(page=page, context=context, store=store, failures=failures,
                              errors=errors, calls=calls, held=held, client=client, output=tmp_path)
        assert not errors, errors
        if request.node.originalname == "test_application_approval_agreement_and_first_upload":
            page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
            page.screenshot(path=str(tmp_path / "onboarding.png"), full_page=True)
        browser.close()
    client.close()


def signup(portal):
    page = portal.page
    page.goto("https://testserver/provider")
    page.locator("#prvToSignup").click()
    page.locator("#prvSuName").fill("Dana Reyes")
    page.locator("#prvSuEmail").fill("dana@example.org")
    page.locator("#prvSuOrg").fill("Browser Health")
    page.locator("#prvSuPw").fill(PASSWORD)
    page.locator("#prvSignupBtn").click()
    page.locator("#prvVerifyCode").fill("123456")
    page.locator("#prvVerifyBtn").click()
    page.locator('input[name="authority"]').first.wait_for()


def answer(page):
    for key in ("authority", "deid_capability"):
        page.locator(f'input[name="{key}"][value="not_sure"]').check()
    page.locator('input[name="export_scope"][value="varies"]').check()
    page.locator('select[name="scale_patients"]').select_option("10k_50k")
    page.locator('select[name="scale_years"]').select_option("5_10")
    page.locator('input[name="scale_specialties"][value="Cardiology"]').check()


def test_application_draft_survives_navigation_reload_and_expired_session(portal):
    signup(portal)
    page = portal.page
    answer(page)
    assert page.locator("#prvRail").is_visible(), page.locator("#prvRail").evaluate("el => ({html: el.outerHTML, display: getComputedStyle(el).display, rect: el.getBoundingClientRect().toJSON()})")
    if page.viewport_size["width"] < 900:
        assert page.locator("#prvRail").bounding_box()["y"] < page.locator("#prvRoot").bounding_box()["y"]
    page.get_by_role("button", name="Account", exact=True).click()
    page.get_by_role("button", name="About you", exact=True).click()
    assert page.locator('input[name="authority"][value="not_sure"]').is_checked()
    page.reload()
    assert page.locator('input[name="scale_specialties"][value="Cardiology"]').is_checked()
    portal.context.clear_cookies()
    page.locator("#prvAppBtn").click()
    page.locator("#prvUsername").fill(portal.store.list_hs_portal_users()[0]["username"])
    page.locator("#prvPassword").fill(PASSWORD)
    page.locator("#prvLoginBtn").click()
    assert page.locator('select[name="scale_patients"]').input_value() == "10k_50k"
    page.locator("#prvAppBtn").click()
    page.locator("#prvAppOk").wait_for()
    hs_id = portal.store.list_health_systems()[0]["hs_id"]
    assert portal.store.latest_hs_application(hs_id)["scale_specialties"] == ["Cardiology"]
    assert portal.store.get_health_system(hs_id)["onboarding_state"] == "submitted"
    page.reload()
    assert page.locator('select[name="scale_years"]').input_value() == "5_10"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_failed_submit_preserves_answers_and_retries(portal):
    signup(portal)
    answer(portal.page)
    portal.failures[("POST", API + "/application")] = 503
    portal.page.locator("#prvAppBtn").click()
    portal.page.locator("#prvAppError").wait_for()
    assert portal.page.locator('select[name="scale_years"]').input_value() == "5_10"
    portal.page.locator("#prvAppBtn").click()
    portal.page.locator("#prvAppOk").wait_for()
    assert len(portal.store.list_hs_applications(portal.store.list_health_systems()[0]["hs_id"])) == 1


def test_application_load_can_be_retried_without_an_empty_form_submit(portal):
    signup(portal)
    portal.failures[("GET", API + "/application")] = 503
    portal.page.reload()
    portal.page.locator("#prvAppError").wait_for()
    assert portal.page.locator("#prvAppBtn").is_disabled()
    portal.page.get_by_role("button", name="Try again", exact=True).click()
    portal.page.locator('input[name="authority"]').first.wait_for()


def test_resend_failure_is_not_announced_as_sent(portal):
    page = portal.page
    page.goto("https://testserver/provider")
    page.locator("#prvToSignup").click()
    page.locator("#prvSuName").fill("Dana Reyes")
    page.locator("#prvSuEmail").fill("dana@example.org")
    page.locator("#prvSuOrg").fill("Browser Health")
    page.locator("#prvSuPw").fill(PASSWORD)
    page.locator("#prvSignupBtn").click()
    portal.failures[("POST", API + "/signup/resend")] = 503
    page.locator("#prvResendBtn").click()
    page.locator("#prvVerifyError").wait_for()
    assert "Sent. Check your inbox." not in page.locator("body").inner_text()


def test_failed_signout_does_not_claim_the_session_was_closed(portal):
    signup(portal)
    portal.failures[("POST", API + "/logout")] = "offline"
    portal.page.locator("#prvLogoutBtn").click()
    portal.page.get_by_text("Could not sign out. Please try again.", exact=True).wait_for()
    assert portal.page.locator("#prvHeader").is_visible()
    portal.page.locator("#prvLogoutBtn").click()
    portal.page.locator("#prvLoginForm").wait_for()
    assert not portal.page.locator("#prvRail").is_visible()
    portal.page.reload()
    portal.page.locator("#prvLoginForm").wait_for()


def test_invite_lookup_outage_is_retryable(portal):
    from asclepius import hs_provisioning
    hs = portal.store.create_health_system_unclaimed("Invited Health")
    minted = hs_provisioning.provision_account(portal.store, hs_id=hs["hs_id"],
                                              org_name=hs["name"], email="dana@example.org", mint_invite=True)
    portal.failures[("GET", API + "/invite/" + minted["invite_token"])] = 503
    portal.page.goto("https://testserver/provider?invite=" + minted["invite_token"])
    portal.page.get_by_role("button", name="Try again", exact=True).click()
    assert portal.page.locator("#prvClaimEmail").input_value() == "dana@example.org"
    portal.page.locator("#prvClaimName").fill("Dana Reyes")
    portal.page.locator("#prvClaimPw").fill(PASSWORD)
    portal.page.locator("#prvClaimConfirm").fill(PASSWORD)
    portal.page.locator("#prvClaimBtn").click()
    portal.page.locator("#prvHeader").wait_for()
    assert "invite=" not in portal.page.url


def test_delayed_submit_response_preserves_newer_draft(portal):
    signup(portal)
    page = portal.page
    answer(page)
    key = ("POST", API + "/application")
    portal.held[key] = None
    page.locator("#prvAppBtn").click()
    page.get_by_role("button", name="Account", exact=True).click()
    page.get_by_role("button", name="About you", exact=True).click()
    page.locator('select[name="scale_patients"]').select_option("over_1m")
    route, response = portal.held.pop(key)
    route.fulfill(status=response.status_code, body=response.content, content_type="application/json")
    page.reload()
    assert page.locator('select[name="scale_patients"]').input_value() == "over_1m"


def test_new_server_answers_do_not_discard_unfinished_edits(portal):
    signup(portal)
    page = portal.page
    answer(page)
    user = portal.store.list_hs_portal_users()[0]
    portal.store.record_hs_application(hs_id=user["hs_id"], username=user["username"],
                                      authority="yes", deid_capability="needs_baa", export_scope="varies",
                                      scale_patients="under_10k", scale_years="under_2", scale_specialties=[])
    page.reload()
    assert page.locator('select[name="scale_patients"]').input_value() == "10k_50k"
    page.get_by_role("button", name="Use submitted answers", exact=True).click()
    assert page.locator('select[name="scale_patients"]').input_value() == "under_10k"


def test_public_site_links_to_working_health_system_signup(portal):
    dist = Path(__file__).resolve().parents[2] / "landing" / "dist" / "index.html"
    assert dist.exists(), "Build the landing app before running the browser gate: npm --prefix landing run build"
    portal.page.goto("https://archangelhealth.ai/health-systems")
    portal.page.get_by_role("link", name="Set up your data portal", exact=True).click()
    portal.page.locator("#prvToSignup").click()
    portal.page.locator("#prvSuName").wait_for()
    assert portal.page.url == "https://app.archangelhealth.ai/provider"


def test_application_approval_agreement_and_first_upload(portal):
    signup(portal)
    page = portal.page
    page.locator("#prvAppBtn").click()
    page.locator("#prvAppError").wait_for()
    hs_id = portal.store.list_health_systems()[0]["hs_id"]
    assert portal.store.latest_hs_application(hs_id) is None
    answer(page)
    page.locator("#prvAppBtn").click()
    page.locator("#prvAppOk").wait_for()
    page.get_by_role("button", name="Upload Locked", exact=True).click()
    page.locator("#prvPendingTitle").wait_for()
    admin = A.headers_for(A.make_user(portal.store, role="admin"))
    response = portal.client.post(f"/api/asclepius/admin/health-systems/{hs_id}/approve", json={}, headers=admin)
    assert response.status_code == 200, response.text
    # The portal remains open during review: signing must refresh the account's
    # surfaces as well as the organization's state without requiring a reload.
    page.get_by_role("button", name="Agreement", exact=True).click()
    page.locator("#prvSignName").fill("Dana Reyes")
    page.locator("#prvSignTitle").fill("Chief Information Officer")
    assert page.locator("#prvSignBtn").is_disabled()
    page.locator("#prvSignAuthority").check()
    page.locator("#prvSignEsign").check()
    page.locator("#prvSignBtn").click()
    page.locator("#prvDrop").wait_for()
    assert portal.store.get_health_system(hs_id)["onboarding_state"] == "active"
    page.get_by_role("button", name="Upload", exact=True).wait_for()
    page.locator("#prvFileInput").set_input_files({"name": "export.json", "mimeType": "application/json",
                                                  "buffer": b'{"resourceType":"Bundle","type":"collection","entry":[]}'})
    page.get_by_text("export.json", exact=True).first.wait_for()
    assert len(portal.store.list_uploads_for_health_system(hs_id)) == 1
    page.get_by_role("button", name="Agreement", exact=True).click()
    page.locator("#prvSignedBox").wait_for()
    page.get_by_role("button", name="About you", exact=True).click()
    assert page.locator('select[name="scale_patients"]').input_value() == "10k_50k"
    page.locator('select[name="scale_patients"]').select_option("50k_250k")
    page.locator("#prvAppBtn").click()
    page.locator("#prvAppOk").wait_for()
    assert portal.store.get_health_system(hs_id)["onboarding_state"] == "active"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_password_free_signup_recovers_from_profile_outage_and_sets_password(portal):
    page = portal.page
    page.goto("https://testserver/provider")
    page.locator("#prvLoginForm").wait_for()
    result = page.evaluate("""async () => {
      const post = (path, data) => fetch('/api/asclepius/hs/' + path, {
        method: 'POST', credentials: 'include', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
      });
      await post('signup', {full_name: 'Dana Reyes', email: 'dana@example.org', organization: 'No Password Health'});
      return (await post('signup/verify', {email: 'dana@example.org', code: '123456'})).json();
    }""")
    assert result["must_reset"] is True
    portal.failures[("GET", API + "/me")] = 503
    page.reload()
    page.get_by_role("button", name="Try again", exact=True).click()
    page.locator("#prvNewPw").fill(PASSWORD)
    page.locator("#prvConfirmPw").fill(PASSWORD)
    page.locator("#prvResetBtn").click()
    page.locator('input[name="authority"]').first.wait_for()
    answer(page)
    page.locator('input[name="export_scope"][value="not_sure"]').check()
    page.locator("#prvAppBtn").click()
    page.locator("#prvAppOk").wait_for()
    user = portal.store.list_hs_portal_users()[0]
    assert not user["must_reset"]
    assert portal.store.latest_hs_application(user["hs_id"])["export_scope"] == "not_sure"
