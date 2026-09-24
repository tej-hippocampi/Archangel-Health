"""Exercise the shipped portal against real API routes and isolated stores."""
from urllib.parse import urlsplit
import uuid
import os
from pathlib import Path
import json
import mimetypes
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, make_user
from tests._physician_application import PASSWORD, submit_physician_application


@pytest.mark.parametrize("kind,width", [("examination", 1200), ("wizard", 1200), ("examination", 390), ("wizard", 390)])
def test_admin_reminder_preview_and_selected_send(tmp_path, monkeypatch, kind, width):
    """Shipped admin UI + real reminder routes; provider capture never delivers mail."""
    from playwright.sync_api import sync_playwright, expect
    from tests._asclepius import headers_for
    from tests.test_manual_reminders import applicant, signup
    from team_store import TeamStore
    import email_utils

    store = fresh_store()
    team = TeamStore(str(tmp_path / "reminders.db"))
    monkeypatch.setattr(app.state, "team_store", team)
    monkeypatch.setenv("ASCLEPIUS_MANUAL_REMINDERS_ENABLED", "1")
    monkeypatch.setenv("ASCLEPIUS_PORTAL_URL", "https://app.archangelhealth.ai")
    monkeypatch.setenv("LANDING_URL", "https://www.archangelhealth.ai")
    monkeypatch.setattr(email_utils, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(email_utils, "is_email_dev_mode", lambda: False)
    messages = []

    async def capture(to, subject, html, **kwargs):
        messages.append({"to": to, "html": html, **kwargs})
        kwargs["delivery_info"].update(outcome="accepted", provider_id="test-only")
        return True, "accepted"

    monkeypatch.setattr(email_utils, "send_html_email_with_reason", capture)
    admin = make_user(store, role="admin")
    for email, name in (("asha@example.com", "Asha Sharma"), ("liam@example.com", "Liam James")):
        if kind == "examination":
            applicant(store, email=email, full_name=name)
        else:
            hs, _ = signup(team, email=email)
            team.upsert_asclepius_person(hs["id"], email=email, full_name=name,
                clinical_role="director", is_director=True)
    front = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
    shell = (front / "admin_shell.js").read_text()
    helper = shell[shell.index("  function h(tag"):shell.index("  const $ =")]
    css = "\n".join((front / name).read_text() for name in ("_tokens.css", "_base.css", "asclepius.css", "admin.css"))
    errors = []
    client = TestClient(app)  # Keep unrelated background schedulers out of this isolated check.
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def dispatch(route):
            req, url = route.request, urlsplit(route.request.url)
            if url.path.startswith("/api/asclepius/"):
                response = client.request(req.method, url.path + ("?" + url.query if url.query else ""),
                    content=req.post_data_buffer,
                    headers={**headers_for(admin), "Content-Type": "application/json"})
                route.fulfill(status=response.status_code, body=response.content, content_type="application/json")
            else:
                route.fulfill(body='<style>' + css + '</style><body class="asc-body asc-admin-body"><main id="root"></main></body>',
                    content_type="text/html")

        page.route("**/*", dispatch)
        page.goto("https://admin.archangelhealth.ai/fixture")
        page.add_script_tag(content=helper + "\n" + (front / "admin_physicians.js").read_text())
        page.evaluate("""() => {
          const api = async (path, opts={}) => {
            const response = await fetch('/api/asclepius' + path, {
              method: opts.method || 'GET', headers: {'Content-Type':'application/json'},
              body: opts.body ? JSON.stringify(opts.body) : undefined});
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Request failed');
            return data;
          };
          AdminPhysiciansSection.render(document.getElementById('root'), {
            h, api, clear:n=>n.replaceChildren(), loadingCard:t=>h('p',{},t), fmtDate:t=>t||'', toast:()=>{}});
        }""")
        # This fixture has one eligible cohort; empty cohort sections are hidden.
        page.get_by_role("button", name="Preview reminders", exact=True).click()
        dialog = page.get_by_role("dialog", name="Examination reminder" if kind == "examination" else "Onboarding reminder", exact=True)
        expect(dialog.get_by_role("button", name="Send 2 reminders", exact=True)).to_be_enabled()
        expect(dialog.locator(".asc-reminder-recipient.is-selected")).to_have_count(2)
        picker = dialog.get_by_label("Preview recipient", exact=True)
        picker.select_option(label="Liam James · liam@example.com")
        email_body = dialog.frame_locator("iframe").locator("body")
        expect(email_body).to_contain_text("Hi Liam James,")
        expect(email_body).not_to_contain_text("Asha Sharma")
        picker.select_option(label="Asha Sharma · asha@example.com")
        expect(email_body).to_contain_text("Hi Asha Sharma,")
        expect(email_body).not_to_contain_text("Liam James")
        assert not messages
        dialog.get_by_label("Send reminder to Liam James · liam@example.com", exact=True).uncheck()
        expect(dialog.locator(".asc-reminder-recipient.is-selected")).to_have_count(1)
        screenshot(page, f"admin-reminder-{kind}-{width}.png")
        page.screenshot(path=str(tmp_path / f"admin-reminder-{kind}-{width}.png"), full_page=True)
        assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
        dialog.get_by_role("button", name="Send 1 reminder", exact=True).click()
        expect(dialog.get_by_text("Accepted by email service", exact=True)).to_be_visible()
        expect(dialog.get_by_role("button", name="Send 0 reminders", exact=True)).to_be_disabled()
        assert len(messages) == 1 and messages[0]["to"] == "asha@example.com"
        assert "Hi Asha Sharma," in messages[0]["html"]
        assert "Liam James" not in messages[0]["html"]
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize("width,legacy_autofill,country,upload_cv,config_failure", [
    (1440, False, "GB", True, False), (390, True, "IN", False, False),
    (390, False, "GB", True, True), (1440, False, "BR", False, True),
])
def test_public_join_opens_real_international_onboarding(tmp_path, monkeypatch, width, legacy_autofill, country, upload_cv, config_failure):
    """Serve the built /join page with real APIs; no request leaves this test."""
    from team_store import TeamStore, get_team_store, set_team_store
    from routers import onboarding

    playwright = pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect
    store = fresh_store()
    previous_team = get_team_store()
    team = TeamStore(str(tmp_path / "join.db"))
    set_team_store(team)
    monkeypatch.setenv("LANDING_URL", "https://www.archangelhealth.ai")
    monkeypatch.setattr(onboarding, "_email_configured", lambda: False)
    dist = Path(__file__).resolve().parents[2] / "landing" / "dist"
    assert (dist / "index.html").is_file(), "Build landing before browser checks"
    errors, requests = [], []
    try:
        with TestClient(app) as client, playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
            page.on("pageerror", lambda error: errors.append(str(error)))

            def dispatch(route):
                req = route.request
                url = urlsplit(req.url)
                if url.netloc == "www.archangelhealth.ai" and not url.path.startswith("/api/"):
                    asset = dist / (url.path.lstrip("/") if url.path.startswith("/assets/") else "index.html")
                    assert asset.resolve().is_relative_to(dist.resolve())
                    route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(str(asset))[0] or "application/octet-stream")
                    return
                if not url.path.startswith("/api/"):
                    route.fulfill(status=200, body="")  # fonts and external assets
                    return
                content = req.post_data_buffer
                if config_failure and url.path == "/api/onboarding/credential-config":
                    route.fulfill(status=503, body="unavailable")
                    return
                if url.path == "/api/onboarding/self-serve":
                    payload = json.loads(content)
                    assert "company_website" not in payload  # current UI has no trap
                    if legacy_autofill:
                        payload["company_website"] = "https://aiimsjodhpur.edu.in"
                    requests.append(payload)
                    content = json.dumps(payload).encode()
                response = client.request(req.method, url.path + ("?" + url.query if url.query else ""),
                                          content=content,
                                          headers={k: v for k, v in req.headers.items() if k not in ("host", "content-length")})
                route.fulfill(status=response.status_code, body=response.content,
                              headers={k: v for k, v in response.headers.items() if k not in ("content-length", "content-encoding")})

            page.route("**/*", dispatch)
            # The old failure page must also provide a usable way back to /join.
            page.goto("https://www.archangelhealth.ai/onboard/old-decoy-token")
            page.get_by_role("link", name="Start with a new onboarding link").click()
            page.get_by_label("First name", exact=True).fill("Asha")
            page.get_by_label("Last name", exact=True).fill("Sharma")
            page.get_by_label("Work email", exact=True).fill("doctor@aiimsjodhpur.edu.in")
            page.get_by_role("button", name="Start onboarding", exact=True).click()
            page.wait_for_url("**/onboard/**")
            expect(page.get_by_label("Choose a password", exact=True)).to_be_visible()
            expect(page.get_by_label("First name", exact=True)).to_have_value("Asha")
            expect(page.get_by_label("Work email", exact=True)).to_have_value("doctor@aiimsjodhpur.edu.in")
            page.get_by_label("Choose a password", exact=True).fill(PASSWORD)
            page.get_by_label("Confirm password", exact=True).fill(PASSWORD)
            state = page.get_by_label("State you are licensed in", exact=False)
            state.select_option("CA")
            page.get_by_role("button", name="Continue", exact=True).click()
            expect(page.get_by_role("heading", name="Verify your email.", exact=True)).to_be_visible()
            page.get_by_role("button", name="Back", exact=True).click()
            state.select_option(label="Outside the US")
            expect(state).to_have_value("")
            screenshot(page, f"outside-us-selected-{width}.png")
            page.get_by_role("button", name="Continue", exact=True).click()
            expect(page.get_by_role("heading", name="Verify your email.", exact=True)).to_be_visible()
            token = urlsplit(page.url).path.rsplit("/", 1)[1]
            row = team.get_health_system_by_onboarding_token(token)
            assert row and row["director_email"] == "doctor@aiimsjodhpur.edu.in"
            assert int(row["onboarding_step"]) == 1
            assert row["director_license_state"] == ""
            page.reload()
            expect(page.get_by_role("heading", name="Verify your email.", exact=True)).to_be_visible()
            assert len(requests) == 1
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            screenshot(page, f"join-international-{width}.png")
            team.create_otp_challenge(row["id"], row["director_email"], "123456")
            response = client.post("/api/onboarding/verify-otp", json={"token": token, "code": "123456"})
            assert response.status_code == 200, response.text
            page.reload()
            if upload_cv:
                page.locator('input[type="file"]').set_input_files({
                    "name": "international-cv.txt", "mimeType": "text/plain",
                    "buffer": b"Asha Sharma, MBBS\nSpecialty: Nephrology\nConsultant at Example Hospital\n",
                })
                expect(page.get_by_role("heading", name="Review the fields.", exact=True)).to_be_visible(timeout=15000)
                page.reload()
                expect(page.get_by_role("heading", name="Review the fields.", exact=True)).to_be_visible()
                expect(page.get_by_label("Primary medical qualification", exact=False)).to_have_value("MBBS")
            else:
                page.get_by_role("button", name="No CV? Enter manually", exact=False).click()
            expect(page.get_by_label("Where do you practise?", exact=True)).to_have_value("")
            expect(page.get_by_label("Where are you licensed?", exact=False)).to_have_value("")
            expect(page.get_by_label("NPI number", exact=False)).to_have_count(0)
            page.get_by_label("Where do you practise?", exact=True).select_option(country)
            expect(page.get_by_label("Where are you licensed?", exact=False)).to_have_value(country)
            expect(page.get_by_label("NPI number", exact=False)).to_have_count(0)
            label = "Medical registration number" if config_failure else "GMC reference number" if country == "GB" else "Medical council registration number"
            registration = page.get_by_label(label, exact=False)
            expect(registration).to_be_visible()
            registration.fill("7654321")
            page.get_by_label("Where are you licensed?", exact=False).select_option("US")
            expect(page.get_by_label("NPI number", exact=False)).to_be_visible()
            page.get_by_label("Where are you licensed?", exact=False).select_option(country)
            expect(page.get_by_label(label, exact=False)).to_have_value("7654321")
            page.get_by_label("Primary specialty", exact=False).fill("Nephrology")
            page.get_by_label("Primary medical qualification", exact=False).select_option("MBBS")
            assert page.locator("input,select,textarea").evaluate_all("""elements => elements.every(el => {
                const rect = el.getBoundingClientRect();
                return rect.width === 0 || (rect.left >= 0 && rect.right <= innerWidth);
            })"""), "Credential controls must remain inside the viewport"
            screenshot(page, f"international-credentials-{country}-{width}.png")
            page.get_by_role("button", name="Submit my application", exact=True).click()
            expect(page.get_by_role("heading", name="Attestations & rights.", exact=True)).to_be_visible()
            saved = team.get_asclepius_person(row["id"], row["director_email"])["credentials"]
            assert saved["countryOfLicensure"] == country and saved["registrationNumber"] == "7654321"
            assert saved["qualification"] == "MBBS" and not saved["licenseState"]
            page.reload()
            expect(page.get_by_role("heading", name="Attestations & rights.", exact=True)).to_be_visible()
            page.get_by_role("button", name="Back", exact=True).click()
            expect(page.get_by_label("Where are you licensed?", exact=False)).to_have_value(country)
            expect(page.get_by_label(label, exact=False)).to_have_value("7654321")
            page.get_by_role("button", name="Submit my application", exact=True).click()
            page.get_by_role("button", name="Agree to all seven", exact=True).click()
            page.get_by_placeholder("T.P.", exact=True).fill("AS")
            monkeypatch.setattr(onboarding, "_email_configured", lambda: True)
            page.get_by_role("button", name="Sign & send my application", exact=True).click()
            expect(page.get_by_role("heading", name="Thank you, Dr. Sharma.", exact=True)).to_be_visible(timeout=15000)
            user = store.get_user_by_email(row["director_email"])
            assert user and user["country_of_licensure"] == country and user["registry_id"] == "7654321"
            assert user["verification_status"] == "pending"
            if upload_cv:
                assert user["cv_asset_sha"] == saved["cvAssetSha"]
            assert not errors, errors
            browser.close()
    finally:
        set_team_store(previous_team)


@pytest.mark.parametrize("saved_step", [1, 2, 3, 4, 5])
def test_reminder_link_opens_saved_wizard_step(tmp_path, monkeypatch, saved_step):
    """The email's personal link uses real session state, never a fixed CV URL."""
    from team_store import TeamStore
    from asclepius import manual_reminders
    from playwright.sync_api import sync_playwright, expect

    store = fresh_store()
    team = TeamStore(str(tmp_path / "wizard-steps.db"))
    monkeypatch.setattr(app.state, "team_store", team)
    monkeypatch.setenv("LANDING_URL", "https://www.archangelhealth.ai")
    invite = team.create_health_system_invite(invite_base_url="https://www.archangelhealth.ai",
        director_email="saved-step@example.com", product="asclepius")
    token = invite["onboarding_url"].rsplit("/", 1)[-1]
    hs = team.get_health_system_by_onboarding_token(token)
    with TestClient(app) as client:
        if saved_step >= 2:
            response = client.post("/api/onboarding/step1-identity", json={"token": token,
                "first_name": "Asha", "last_name": "Sharma", "email": "saved-step@example.com", "password": PASSWORD})
            assert response.status_code == 200, response.text
        if saved_step >= 3:
            team.create_otp_challenge(hs["id"], "saved-step@example.com", "123456")
            assert client.post("/api/onboarding/verify-otp", json={"token": token, "code": "123456"}).status_code == 200
        if saved_step == 4:
            team.upsert_asclepius_person(hs["id"], email="saved-step@example.com", full_name="Asha Sharma", clinical_role="director", is_director=True)
            team.merge_asclepius_credentials(hs["id"], "saved-step@example.com", {"cvAssetSha": "saved-cv", "cvParseStage": "done"})
        if saved_step == 5:
            response = client.post("/api/onboarding/asclepius/credentials", json={"token": token,
                "credentials": {"fullLegalName": "Asha Sharma", "primarySpecialty": "nephrology"}})
            assert response.status_code == 200, response.text
        recipient = next(r for r in manual_reminders.candidates(store, team, "wizard") if r["email"] == "saved-step@example.com")
        link = manual_reminders.wizard_url(team, recipient)
        assert token in link
        dist = Path(__file__).resolve().parents[2] / "landing" / "dist"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 390, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def dispatch(route):
                req, url = route.request, urlsplit(route.request.url)
                if url.path.startswith("/api/"):
                    response = client.request(req.method, url.path + ("?" + url.query if url.query else ""),
                        content=req.post_data_buffer, headers={k:v for k,v in req.headers.items() if k not in ("host", "content-length")})
                    route.fulfill(status=response.status_code, body=response.content,
                        headers={k:v for k,v in response.headers.items() if k not in ("content-length", "content-encoding")})
                elif url.netloc == "www.archangelhealth.ai":
                    asset = dist / (url.path.lstrip("/") if url.path.startswith("/assets/") else "index.html")
                    route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(str(asset))[0] or "application/octet-stream")
                else:
                    route.fulfill(status=200, body="")

            page.route("**/*", dispatch)
            page.goto(link)
            if saved_step == 1:
                expect(page.get_by_label("First name", exact=True)).to_be_visible()
            else:
                heading = {2: "Verify your email.", 3: "Upload your CV and we’ll fill this out for you.",
                    4: "Review the fields.", 5: "Attestations & rights."}[saved_step]
                expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
            assert not errors, errors
            screenshot(page, f"reminder-saved-step-{saved_step}.png")
            browser.close()


@pytest.fixture()
def portal(tmp_path, monkeypatch):
    from team_store import TeamStore, get_team_store, set_team_store

    playwright = pytest.importorskip("playwright.sync_api")
    store = fresh_store()
    previous_team = get_team_store()
    set_team_store(TeamStore(str(tmp_path / "team.db")))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    # Real external services are outside this browser regression.
    from asclepius import credentialing
    monkeypatch.setattr(credentialing, "fetch_npi_record", lambda *a, **k: {"result": "unavailable"})
    with TestClient(app) as client, playwright.sync_playwright() as p:
        user = submit_physician_application(client, f"browser-{uuid.uuid4().hex[:8]}@example.org")
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.emulate_media(reduced_motion="reduce")
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def dispatch(route):
            request = route.request
            url = urlsplit(request.url)
            response = client.request(
                request.method, url.path + ("?" + url.query if url.query else ""),
                content=request.post_data_buffer,
                headers={k: v for k, v in request.headers.items() if k != "host"},
            )
            if response.status_code >= 400:
                print("HTTP", url.path, response.status_code, response.text[:500], flush=True)
            route.fulfill(status=response.status_code, body=response.content,
                          headers={k: v for k, v in response.headers.items()
                                   if k not in ("content-length", "content-encoding")})

        page.route("**/*", dispatch)
        page.goto("http://testserver/asclepius")
        page.locator('input[autocomplete="username"]').fill(user["email"])
        page.locator('input[type="password"]').fill(PASSWORD)
        page.get_by_role("button", name="Sign in", exact=True).click()
        try:
            page.locator("#ascExamStart").wait_for(timeout=5000)
        except Exception:
            print(page.locator("body").inner_text(), errors, flush=True)
            raise
        try:
            yield page, store, user, errors
        finally:
            browser.close()
            set_team_store(previous_team)


def screenshot(page, name):
    output = os.getenv("ONBOARDING_SCREENSHOT_DIR")
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(output) / name), full_page=True)


@pytest.fixture
def practice_preservation(portal, tmp_path, request):
    """Compare all existing IDs/fields across the flow and restore its backup."""
    import sqlite3
    from scripts.data_inventory import snapshot, compare
    from team_store import get_team_store
    from community.store import get_community_store
    page, store, user, errors = portal
    before = snapshot(store.db_path)
    backup = tmp_path / "practice-before.db"
    with sqlite3.connect(store.db_path) as source, sqlite3.connect(backup) as dest:
        source.backup(dest)
    assert compare(before, snapshot(backup)) == []
    other_stores = {}
    for name, path in (("team", get_team_store().db_path), ("community", get_community_store().db_path)):
        original = snapshot(path)
        backup_path = tmp_path / f"{name}-before.db"
        with sqlite3.connect(path) as source, sqlite3.connect(backup_path) as dest:
            source.backup(dest)
        assert compare(original, snapshot(backup_path)) == []
        other_stores[name] = {"path": path, "before": original}
    yield
    after = snapshot(store.db_path)
    # Refresh records a session in first_run_json; exam/practice progression
    # changes tutorial_json. Every other existing field and ID must survive.
    allowed = ["users.tutorial_json", "users.first_run_json"]
    problems = compare(before, after, allowed=allowed)
    for name, data in other_stores.items():
        data["after"] = snapshot(data["path"])
        problems.extend(f"{name}: {problem}" for problem in compare(data["before"], data["after"]))
    output = os.getenv("ONBOARDING_SCREENSHOT_DIR")
    if output:
        directory = Path(output) / request.node.name
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in (("before", before), ("after", after), ("preservation", {
            "problems": problems, "backup_restore_matches": True,
            "allowed_changes": allowed,
            "additional_stores_preserved": list(other_stores),
        })):
            (directory / f"{name}.json").write_text(json.dumps(data, indent=2))
        for name, data in other_stores.items():
            for phase in ("before", "after"):
                (directory / f"{name}-{phase}.json").write_text(json.dumps(data[phase], indent=2))
    assert not problems, problems


@pytest.fixture()
def accepted_portal(tmp_path, monkeypatch):
    """Run the shipped page with real auth, queues and isolated stores.

    Overrides are limited to explicit outage tests and the reviewer-only session
    contract. No fixture changes the JavaScript or reaches into its private state.
    """
    from team_store import TeamStore, get_team_store, set_team_store
    from tests._asclepius import token_for

    playwright = pytest.importorskip("playwright.sync_api")
    store = fresh_store()
    previous_team = get_team_store()
    set_team_store(TeamStore(str(tmp_path / "accepted-team.db")))
    monkeypatch.delenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED", raising=False)
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    try:
        with TestClient(app) as client, playwright.sync_playwright() as p:
            browser = p.chromium.launch()

            def open_portal(*, tier="labeler", width=1440, reviewer_only=False,
                            welcome_complete=True):
                user = make_user(store, specialty="nephrology", tier=tier)
                store.set_verification_status(user["id"], "approved")
                if welcome_complete:
                    first_run = store.get_first_run(user["id"])
                    first_run["stops"] = {stop: "done" for stop in
                        ("welcome", "start", "practice", "community", "earnings", "manual")}
                    store.set_first_run(user["id"], first_run)
                user = store.get_user_by_id(user["id"])
                context = browser.new_context(viewport={"width": width, "height": 900},
                                              reduced_motion="reduce")
                page = context.new_page()
                page.set_default_timeout(10000)
                errors, requests, overrides = [], [], {}
                page.on("pageerror", lambda error: errors.append(str(error)))

                def dispatch(route):
                    request, url = route.request, urlsplit(route.request.url)
                    requests.append(url.path)
                    if url.path in overrides:
                        status, body = overrides[url.path]
                        route.fulfill(status=status, json=body)
                        return
                    response = client.request(request.method,
                        url.path + ("?" + url.query if url.query else ""),
                        content=request.post_data_buffer,
                        headers={k: v for k, v in request.headers.items()
                                 if k not in ("host", "content-length")})
                    if reviewer_only and url.path == "/api/asclepius/auth/me":
                        body = response.json()
                        body["capabilities"] = ["review", "refer"]
                        route.fulfill(status=response.status_code, json=body)
                        return
                    route.fulfill(status=response.status_code, body=response.content,
                        headers={k: v for k, v in response.headers.items()
                                 if k not in ("content-length", "content-encoding")})

                context.route("**/*", dispatch)
                page.add_init_script("localStorage.setItem('asclepius_token', "
                                     + json.dumps(token_for(user)) + ");")
                return SimpleNamespace(page=page, store=store, user=user, errors=errors,
                    requests=requests, overrides=overrides, client=client)

            yield open_portal
            browser.close()
    finally:
        set_team_store(previous_team)


def _dashboard_case(store, *, review_ready=False):
    task = store.insert_task(prompt="What is the next step in this assigned kidney case?",
        specialty="nephrology", difficulty="hard", source="synthetic",
        max_labels=2 if review_ready else 1,
        candidate_answers=[{"id": "A", "text": "Repeat the potassium and obtain an ECG."},
                           {"id": "B", "text": "Discharge without follow-up."}])
    if review_ready:
        for _ in range(2):
            labeler = make_user(store, specialty="nephrology")
            store.insert_submission(submission_id="s-" + uuid.uuid4().hex,
                task_id=task["task_id"], evaluator_id=labeler["id"],
                verdict="A_better", chosen_id="A", rejected_id="B", confidence="high",
                time_spent_sec=180, payload={}, annotator=store.annotator_block(labeler),
                dedupe_hash=None, portal_version="v3")
    return task


_WAITING_COPY = "No cases to label or review just yet. We’ll notify you when a case is ready for you."


@pytest.mark.parametrize("tier,reviewer_only,width", [
    ("labeler", False, 1440), ("labeler", False, 390),
    ("reviewer", True, 1440), ("reviewer", True, 390),
])
def test_accepted_unassigned_doctor_can_explore_without_starting_work(
        accepted_portal, tier, reviewer_only, width):
    from playwright.sync_api import expect

    portal = accepted_portal(tier=tier, reviewer_only=reviewer_only, width=width)
    # Populate the shared pool: the empty state must be personal, not a side
    # effect of the entire installation having no cases or review pairs.
    _dashboard_case(portal.store)
    _dashboard_case(portal.store, review_ready=True)
    page = portal.page
    page.goto("http://testserver/asclepius")
    expect(page.get_by_role("heading", name="You’re all set.", exact=True)).to_be_visible()
    expect(page.get_by_text(_WAITING_COPY, exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Start →", exact=True)).to_have_count(0)
    expect(page.get_by_text("Start new case", exact=True)).to_have_count(0)
    expect(page.locator(".asc-waiting-card button")).to_have_text(["Open the practice case"])
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_have_count(0)
    assert not page.locator(".asc-inline-error").count()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if reviewer_only:
        assert "/api/asclepius/tasks/available" not in portal.requests
    screenshot(page, f"assigned-waiting-{tier}-{width}.png")

    page.get_by_role("button", name="Guide", exact=True).click()
    expect(page.locator(".asc-guide-h1")).to_be_visible()
    page.get_by_role("button", name="Tasks", exact=True).click()
    expect(page.get_by_text(_WAITING_COPY, exact=True)).to_be_visible()
    with page.expect_popup() as popup:
        page.get_by_role("button", name="Community (opens in a new tab)", exact=True).click()
    popup.value.wait_for_load_state()
    assert urlsplit(popup.value.url).path == "/community"
    popup.value.close()
    page.get_by_role("button", name="Open the practice case", exact=True).click()
    expect(page.get_by_text("One practice case. About 4 minutes.", exact=True)).to_be_visible()
    assert not portal.errors, portal.errors


def test_assigned_doctor_keeps_start_and_continue_flow(accepted_portal):
    from playwright.sync_api import expect

    portal = accepted_portal()
    task = _dashboard_case(portal.store)
    portal.store.upsert_assignment(task_id=task["task_id"], user_id=portal.user["id"],
                                  role="label", assigned_by="test-admin")
    page = portal.page
    page.goto("http://testserver/asclepius")
    expect(page.get_by_text("Start new case", exact=True)).to_be_visible()
    expect(page.get_by_text(_WAITING_COPY, exact=True)).to_have_count(0)
    screenshot(page, "assigned-labeler-ready.png")
    page.get_by_role("button", name="Start →", exact=True).click()
    expect(page.get_by_text(task["prompt"], exact=True)).to_be_visible()
    assert "/api/asclepius/tasks/next" in portal.requests
    page.reload()
    expect(page.get_by_text("Continue case", exact=True)).to_be_visible()
    page.get_by_role("button", name="Continue →", exact=True).click()
    expect(page.get_by_text(task["prompt"], exact=True)).to_be_visible()
    # An assignment can be withdrawn after the dashboard painted. The draw's
    # empty state offers the same practice action, with no queue refresh loop.
    page.get_by_role("button", name="Tasks", exact=True).click()
    page.add_init_script("localStorage.removeItem('asclepius_draft_' + " + json.dumps(task["task_id"]) + ")")
    page.reload()
    expect(page.get_by_role("button", name="Start →", exact=True)).to_be_visible()
    portal.overrides["/api/asclepius/tasks/next"] = (200, {"task": None})
    page.get_by_role("button", name="Start →", exact=True).click()
    expect(page.get_by_text("No cases to label just yet. We’ll notify you when a case is ready for you.", exact=True)).to_be_visible()
    expect(page.locator(".asc-waiting-card button")).to_have_text(["Open the practice case"])
    expect(page.get_by_role("button", name="Refresh queue", exact=True)).to_have_count(0)
    screenshot(page, "assigned-labeling-waiting.png")
    page.get_by_role("button", name="Open the practice case", exact=True).click()
    expect(page.get_by_text("One practice case. About 4 minutes.", exact=True)).to_be_visible()
    assert not portal.errors, portal.errors


def test_accepted_doctor_sees_waiting_state_after_welcome(accepted_portal):
    from playwright.sync_api import expect

    portal = accepted_portal(welcome_complete=False)
    _dashboard_case(portal.store)
    page = portal.page
    page.goto("http://testserver/asclepius")
    expect(page.get_by_role("heading", name="Welcome to Archangel Health.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Let’s get you started →", exact=True).click()
    page.get_by_role("button", name="Start the practice case", exact=False).click()
    page.get_by_role("button", name="Skip practice case walkthrough", exact=True).click()
    for heading in ("The people you’ll be working alongside.", "How you get paid.",
                    "Everything else lives in the manual."):
        expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
        page.get_by_role("button", name="Do this later", exact=True).click()
    expect(page.get_by_text(_WAITING_COPY, exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Start →", exact=True)).to_have_count(0)
    assert portal.store.get_first_run(portal.user["id"])["stops"]["welcome"] == "done"
    assert not portal.errors, portal.errors


@pytest.mark.parametrize("width", [1440, 390])
def test_accepted_welcome_save_failure_can_be_retried(accepted_portal, width):
    from playwright.sync_api import expect

    portal = accepted_portal(welcome_complete=False, width=width)
    page = portal.page
    page.goto("http://testserver/asclepius")
    portal.overrides["/api/asclepius/me/first-run"] = (503, {"detail": "Temporarily unavailable"})
    page.get_by_role("button", name="Let’s get you started →", exact=True).click()
    expect(page.get_by_text("Could not save your progress. Please try again.", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Welcome to Archangel Health.", exact=True)).to_be_visible()
    assert "welcome" not in portal.store.get_first_run(portal.user["id"])["stops"]
    del portal.overrides["/api/asclepius/me/first-run"]
    page.get_by_role("button", name="Let’s get you started →", exact=True).click()
    expect(page.get_by_role("heading", name="Where would you like to start?", exact=True)).to_be_visible()
    assert portal.store.get_first_run(portal.user["id"])["stops"]["welcome"] == "done"
    page.reload()
    expect(page.get_by_role("heading", name="Where would you like to start?", exact=True)).to_be_visible()
    assert not portal.errors, portal.errors


@pytest.mark.parametrize("destination", ["tasks", "community"])
def test_accepted_welcome_navigation_during_save(accepted_portal, destination):
    from playwright.sync_api import expect

    portal = accepted_portal(welcome_complete=False)
    page, pending = portal.page, []
    page.route("**/api/asclepius/me/first-run", lambda route: pending.append(route))
    page.goto("http://testserver/asclepius")
    button = page.get_by_role("button", name="Let’s get you started →", exact=True)
    button.click()
    expect(button).to_be_disabled()
    if destination == "tasks":
        page.get_by_role("button", name="Tasks", exact=True).click()
        expect(page.get_by_text(_WAITING_COPY, exact=True)).to_be_visible()
    else:
        with page.expect_popup() as popup:
            page.get_by_role("button", name="Community (opens in a new tab)", exact=True).click()
        popup.value.wait_for_load_state()
        popup.value.close()
        expect(button).to_be_disabled()
    assert len(pending) == 1
    request = pending[0].request
    response = portal.client.patch("/api/asclepius/me/first-run",
        content=request.post_data_buffer, headers=request.headers)
    pending[0].fulfill(status=response.status_code, json=response.json())
    if destination == "tasks":
        expect(page.get_by_text(_WAITING_COPY, exact=True)).to_be_visible()
        expect(page.locator(".asc-fr-stage")).to_have_count(0)
    else:
        expect(page.get_by_role("heading", name="Where would you like to start?", exact=True)).to_be_visible()
    assert not portal.errors, portal.errors


@pytest.mark.parametrize("width", [1440, 390])
def test_accepted_welcome_opens_community_and_manual(accepted_portal, width):
    from playwright.sync_api import expect

    portal = accepted_portal(welcome_complete=False, width=width)
    portal.store.set_first_run(portal.user["id"], {"version": 1,
        "stops": {"welcome": "done", "start": "done"},
        "practice_skipped_at": "2026-09-21T00:00:00Z"})
    page = portal.page
    page.goto("http://testserver/asclepius")
    page.get_by_role("button", name="Finish these now", exact=True).click()
    with page.expect_popup() as popup:
        page.get_by_role("button", name="Open the community", exact=True).click()
    popup.value.wait_for_load_state()
    expect(popup.value.locator("#cmRoot")).to_contain_text("introductions")
    popup.value.close()
    expect(page.get_by_role("heading", name="How you get paid.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Do this later", exact=True).click()
    page.get_by_role("button", name="Open the manual", exact=True).click()
    expect(page.locator(".asc-guide-h1")).to_have_text("How to produce a premium record")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    state = portal.store.get_first_run(portal.user["id"])
    assert state["stops"]["community"] == state["stops"]["manual"] == "done"
    assert state["practice_skipped_at"]
    assert not portal.errors, portal.errors


def test_assigned_review_card_never_claims_there_is_no_review_work(accepted_portal):
    from playwright.sync_api import expect

    portal = accepted_portal(tier="reviewer", reviewer_only=True)
    task = _dashboard_case(portal.store, review_ready=True)
    portal.store.upsert_assignment(task_id=task["task_id"], user_id=portal.user["id"],
                                  role="review", assigned_by="test-admin")
    page = portal.page
    page.goto("http://testserver/asclepius")
    expect(page.get_by_text("1 pair waiting for your adjudication", exact=True)).to_be_visible()
    expect(page.get_by_text(_WAITING_COPY, exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Start →", exact=True)).to_have_count(0)
    screenshot(page, "assigned-reviewer-ready.png")
    page.locator(".asc-dash-card-review").click()
    expect(page.get_by_role("heading", name="Review", exact=True)).to_be_visible()
    expect(page.locator(".asc-rv-judgment")).to_be_visible()
    assert not portal.errors, portal.errors


@pytest.mark.parametrize("claimed", [False, True])
def test_assigned_review_card_waits_for_single_submission_preparation(
        accepted_portal, monkeypatch, claimed):
    from playwright.sync_api import expect
    from tests._asclepius import headers_for

    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_HALT", "1")
    monkeypatch.setenv("ASCLEPIUS_REVIEW_RATE", "1")
    portal = accepted_portal(tier="reviewer", reviewer_only=True)
    task = _dashboard_case(portal.store)
    labeler = make_user(portal.store, specialty="nephrology")
    submission_id = "s-" + uuid.uuid4().hex
    portal.store.insert_submission(submission_id=submission_id, task_id=task["task_id"],
        evaluator_id=labeler["id"], verdict="A_better", chosen_id="A", rejected_id="B",
        confidence="high", time_spent_sec=180, payload={},
        annotator=portal.store.annotator_block(labeler), dedupe_hash=None, portal_version="v3")
    portal.store.upsert_assignment(task_id=task["task_id"], user_id=portal.user["id"],
                                  role="review", assigned_by="test-admin")
    if claimed:
        assert portal.store.claim_submission_for_review(submission_id,
                                                       reviewer_id=portal.user["id"])
    counts = portal.client.get("/api/asclepius/review/stats",
                               headers=headers_for(portal.user)).json()
    assert counts["review_ready"] == 0
    assert counts["in_review" if claimed else "unreviewed"] == 1

    page = portal.page
    page.goto("http://testserver/asclepius")
    preparing = "Your review assignment is being prepared"
    expect(page.get_by_text(preparing, exact=True)).to_be_visible()
    expect(page.get_by_text(_WAITING_COPY, exact=True)).to_have_count(0)
    page.locator(".asc-dash-card-review").click()
    expect(page.get_by_role("heading", name=preparing, exact=True)).to_be_visible()
    expect(page.get_by_text("We’ll notify you when your case is ready for review.", exact=True)).to_be_visible()
    expect(page.get_by_text("No cases ready for review just yet.", exact=False)).to_have_count(0)
    if not claimed:
        screenshot(page, "assigned-single-review-preparing.png")
    portal.overrides["/api/asclepius/review/stats"] = (503, {"detail": "Review stats unavailable"})
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_text("We could not load your review queue. Please try again.", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="You’re all set.", exact=True)).to_have_count(0)
    assert not portal.errors, portal.errors


@pytest.mark.parametrize("tier,path,message", [
    ("labeler", "/api/asclepius/tasks/available", "Your case queue could not be loaded"),
    ("reviewer", "/api/asclepius/review/stats", "We could not load your review queue."),
])
def test_accepted_doctor_sees_queue_outage_instead_of_waiting_assurance(
        accepted_portal, tier, path, message):
    from playwright.sync_api import expect

    portal = accepted_portal(tier=tier, reviewer_only=tier == "reviewer")
    portal.overrides[path] = (503, {"detail": "Queue temporarily unavailable"})
    page = portal.page
    page.goto("http://testserver/asclepius")
    expect(page.get_by_text(message, exact=False)).to_be_visible()
    expect(page.get_by_text(_WAITING_COPY, exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Start →", exact=True)).to_have_count(0)
    assert not portal.errors, portal.errors


def test_empty_review_console_keeps_errors_distinct(accepted_portal):
    from playwright.sync_api import expect

    portal = accepted_portal(tier="reviewer")
    page = portal.page
    page.goto("http://testserver/asclepius#review")
    empty = "No cases ready for review just yet. We’ll notify you when a case is ready for you."
    expect(page.get_by_text(empty, exact=True)).to_be_visible()
    portal.overrides["/api/asclepius/review/pair/next"] = (503, {"detail": "Review unavailable"})
    page.get_by_role("button", name="Check again", exact=True).click()
    expect(page.get_by_text("Review unavailable", exact=True)).to_be_visible()
    expect(page.get_by_text(empty, exact=True)).to_have_count(0)
    assert not portal.errors, portal.errors


@pytest.mark.parametrize("width,legacy_negative", [(1440, False), (390, True)])
@pytest.mark.usefixtures("practice_preservation")
def test_applicant_can_start_and_resume_examination(portal, width, legacy_negative):
    page, store, user, errors = portal
    page.set_viewport_size({"width": width, "height": 900})
    screenshot(page, "applicant-desktop.png")
    page.locator("#ascExamStart").click()
    page.locator(".asc-exam-banner").wait_for()
    assert not errors
    page.get_by_role("button", name="Looks clinically valid, continue").click()
    page.locator(".asc-instinct-input").fill("Low-solute hyponatremia; correct sodium cautiously with close monitoring.")
    page.locator("#ascRevealBtn").click()
    page.locator(".asc-answer-body").first.wait_for()
    answers = page.locator(".asc-answer-body").all_text_contents()
    assert len(answers) == 2 and all(answers)
    page.get_by_role("button", name="Pause and review").click()
    page.get_by_role("button", name="Resume the examination").wait_for()
    page.reload()
    page.get_by_role("button", name="Resume the examination").click()
    page.locator(".asc-answer-body").first.wait_for()
    assert page.locator(".asc-answer-body").all_text_contents() == answers
    page.get_by_role("button", name="Hide case", exact=True).click()
    assert page.get_by_role("button", name="Pause and review").is_visible()
    page.get_by_role("button", name="Open case", exact=True).click()
    assert not errors
    page.locator('[data-verdict="A_better"]').click()
    page.get_by_role("button", name="Save changes").click()
    section = page.locator('[data-substage="why_better"]')
    section.locator("textarea").fill("The answer identifies low-solute intake and limits correction to reduce osmotic injury.")
    section.locator(".asc-chip").first.click()
    section.get_by_role("button", name="Continue").click()
    page.locator('.asc-substage[data-substage="citations"]').get_by_role("button", name="Continue").click()
    section = page.locator('.asc-substage[data-substage="critique_rejected"]')
    section.locator("[data-tag]").first.click()
    dialog = page.get_by_role("dialog", name="Detail this error")
    dialog.locator(".asc-reason-pills button").first.click()
    dialog.get_by_role("button", name="high", exact=True).click()
    section.get_by_placeholder("One line on the key problem…").fill("Rapid normalization ignores the high risk of overcorrection in low-solute hyponatremia.")
    if section.locator("#ascFailureModes .asc-chip").count():
        section.locator("#ascFailureModes .asc-chip").first.click()
    if legacy_negative:
        pending_split = []
        page.route("**/reasoning/pregrade", lambda route: pending_split.append(route))
        with page.expect_request("**/reasoning/pregrade"):
            section.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Pause and review").click()
        with page.expect_response("**/reasoning/pregrade"):
            pending_split[0].fallback()
        page.unroute("**/reasoning/pregrade")
        with page.expect_request("**/reasoning/pregrade"):
            page.get_by_role("button", name="Resume the examination").click()
    else:
        section.get_by_role("button", name="Continue").click()
    page.locator("[data-step-idx]").first.wait_for()
    for button in page.locator(".asc-step-confirm").all():
        button.click()
    page.locator("#ascStepsCont").click()
    page.locator("#ascRubricWizard textarea").wait_for()
    for _ in range(20):
        next_button = page.locator("#ascRubricWizard").get_by_role("button", name="Next", exact=False)
        if not next_button.count():
            break
        if legacy_negative:
            page.locator('#ascRubricWizard [data-tier="important"]').click()
        next_button.click()
    if legacy_negative:
        from playwright.sync_api import expect
        expect(page.get_by_role("button", name="Save & finish")).to_be_disabled()
        repair = page.locator(".asc-rubric-make-critical").first
        if not repair.count():
            page.get_by_role("button", name="Add a must never criterion", exact=False).click()
            page.locator("#ascRubricWizard textarea").fill("Overcorrect sodium by more than 8 mmol/L per day")
            page.get_by_role("button", name="Next →", exact=True).click()
        else:
            screenshot(page, "exam-scoring-recovery-mobile.png")
            repair.click()
        expect(page.get_by_role("button", name="Save & finish")).to_be_enabled()
    page.get_by_role("button", name="Save & finish").click()
    page.locator('#ascConf [data-conf="high"]').click()
    attempts = []

    def transient_failure(route):
        attempts.append(route.request.method)
        if len(attempts) == 1:
            route.fulfill(status=503, content_type="application/json",
                          body='{"detail":"Temporary test outage"}')
        else:
            route.fallback()

    page.route("**/exam/submit", transient_failure)
    page.locator("#ascSubmit").click()
    page.get_by_text("Could not file your examination: Temporary test outage").wait_for()
    assert page.locator("#ascSubmit").is_enabled(), "a failed submit must remain retryable"
    page.locator("#ascSubmit").click()
    page.get_by_role("heading", name="Your examination is with us.").wait_for()
    screenshot(page, "examination-receipt.png")
    exams = store.list_credentialing_exams(user["id"])
    assert len(exams) == 1
    assert exams[0]["payload"]["verdict"] == "A_better"
    with store._conn() as conn:
        assert conn.execute("SELECT count(*) FROM submissions WHERE evaluator_id=?", (user["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM earnings WHERE user_id=?", (user["id"],)).fetchone()[0] == 0
    page.reload()
    page.get_by_role("heading", name="You’re all set").wait_for()
    assert page.locator("#ascExamStart").count() == 0
    assert not errors


def test_reminder_link_preserves_examination_destination_through_signin(portal):
    page, store, user, errors = portal
    page.evaluate("localStorage.clear()")
    page.context.clear_cookies()
    page.goto("about:blank")
    page.goto("http://testserver/asclepius#examination")
    page.locator('input[autocomplete="username"]').fill(user["email"])
    page.locator('input[type="password"]').fill(PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator("#ascExamStart").wait_for()
    page.wait_for_function("document.activeElement && document.activeElement.id === 'ascExamStart'")
    assert store.get_user_by_id(user["id"])["application_completed_at"]
    assert store.get_tutorial_state(user["id"]).get("exam", {}).get("state", "not_started") == "not_started"
    assert not errors


def test_applicant_guide_matches_guide_tab(portal):
    page, store, user, errors = portal
    page.get_by_role("button", name="Read the labeling guide").click()
    overlay = page.locator("#ascApplicantGuide")
    overlay.wait_for()
    expected = overlay.locator(".asc-guide-section").all_text_contents()
    assert expected
    page.get_by_role("button", name="Close the guide").click()
    page.get_by_role("button", name="Guide", exact=True).click()
    page.locator(".asc-guide-content").wait_for()
    assert page.locator(".asc-guide-section").all_text_contents() == expected
    assert not errors


def test_applicant_can_flag_exam_for_review_on_mobile(portal):
    page, store, user, errors = portal
    page.set_viewport_size({"width": 390, "height": 844})
    screenshot(page, "applicant-mobile.png")
    page.locator("#ascExamStart").click()
    page.get_by_role("button", name="Flag as invalid").click()
    page.get_by_placeholder("Why is this case invalid?", exact=False).fill("The case is missing the prior sodium trend needed to establish chronicity.")
    page.get_by_role("button", name="Send to admin").click()
    page.get_by_role("heading", name="Your examination is with us.").wait_for()
    assert len(store.list_credentialing_exams(user["id"])) == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_unavailable_demo_does_not_block_exam(portal):
    page, store, user, errors = portal
    page.locator(".asc-applicant-video").click()
    page.get_by_text("The video is unavailable right now.", exact=False).wait_for()
    page.locator("#ascExamStart").click()
    page.locator(".asc-exam-banner").wait_for()
    assert not errors


def test_pending_applicant_can_play_demo_with_media_ticket(portal):
    from asclepius import assets

    page, store, user, errors = portal
    # A real, tiny playable clip exercises the media endpoint and Range support.
    payload = bytes(page.evaluate("""async () => {
      const canvas = document.createElement('canvas');
      canvas.width=160; canvas.height=90;
      const stream=canvas.captureStream(10);
      const recorder=new MediaRecorder(stream, {mimeType:'video/webm'}), chunks=[];
      recorder.ondataavailable=e=>chunks.push(e.data);
      const done=new Promise(resolve=>recorder.onstop=resolve);
      recorder.start();
      const draw=setInterval(()=>canvas.getContext('2d').fillRect(0,0,160,90),20);
      await new Promise(resolve=>setTimeout(resolve,250));
      recorder.stop(); await done; clearInterval(draw); stream.getTracks().forEach(t=>t.stop());
      return Array.from(new Uint8Array(await new Blob(chunks).arrayBuffer()));
    }"""))
    media = assets.store_media(iter([payload]), "video/webm")
    store.set_platform_media("onboarding_demo", sha256=media["sha256"],
                             mime="video/webm", byte_size=media["byte_size"], filename="test-demo.webm")
    page.locator(".asc-applicant-video").click()
    page.locator("video").wait_for()
    page.wait_for_function("document.querySelector('video').readyState >= 2")
    assert "?t=" in page.locator("video").get_attribute("src")
    page.keyboard.press("Escape")
    assert page.locator(".asc-applicant-video").evaluate("e => e === document.activeElement")
    page.locator("#ascExamStart").click()
    page.locator(".asc-exam-banner").wait_for()
    assert not errors


def test_exam_drafts_do_not_cross_accounts_or_trust_unowned_legacy_drafts(portal):
    page, store, user, errors = portal
    page.locator("#ascExamStart").click()
    page.get_by_role("button", name="Looks clinically valid, continue").click()
    page.locator(".asc-instinct-input").fill("Applicant A's independent reading of the case.")
    page.locator("#ascRevealBtn").click()
    page.locator(".asc-answer-body").first.wait_for()
    task_id = store.get_tutorial_state(user["id"])["exam"]["task_id"]
    # Also retain an old, unowned draft. It must not be attributed to B.
    page.evaluate("""taskId => {
      const key=Object.keys(localStorage).find(k=>k.includes(':exam:') && k.endsWith(taskId));
      localStorage.setItem('asclepius_draft_'+taskId, localStorage.getItem(key));
    }""", task_id)
    page.get_by_role("button", name="Sign out", exact=True).click()
    second = make_user(store, tier=None, practice_case=False, specialty="nephrology")
    store.set_verification_status(second["id"], "pending")
    page.locator('input[autocomplete="username"]').fill(second["email"])
    page.locator('input[type="password"]').fill("pw-12345678")
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator("#ascExamStart").click()
    page.get_by_role("button", name="Looks clinically valid, continue").click()
    assert page.locator(".asc-instinct-input").input_value() == ""
    assert store.get_independent_commit(task_id, second["id"]) is None
    assert store.get_independent_commit(task_id, user["id"]) is not None
    assert not errors


@pytest.mark.parametrize("session_exit", ["signout", "expired"])
def test_next_account_practice_does_not_inherit_exam_state(portal, session_exit):
    page, store, user, errors = portal
    page.locator("#ascExamStart").click()
    page.get_by_role("button", name="Looks clinically valid, continue").click()
    page.locator(".asc-instinct-input").fill("Applicant A's saved examination answer.")
    if session_exit == "signout":
        page.get_by_role("button", name="Sign out", exact=True).click()
    else:
        page.route("**/tasks/*/reveal", lambda route: route.fulfill(
            status=401, content_type="application/json", body='{"detail":"Session expired"}'))
        page.locator("#ascRevealBtn").click()
    page.locator('input[autocomplete="username"]').wait_for()
    second = make_user(store, specialty="nephrology", tier=None, practice_case=False)
    store.set_verification_status(second["id"], "pending")
    page.locator('input[autocomplete="username"]').fill(second["email"])
    page.locator('input[type="password"]').fill("pw-12345678")
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.get_by_role("button", name="Optional: try a practice case first", exact=False).click()
    page.locator("#ascTourInterstitial").wait_for()
    assert page.locator(".asc-exam-banner").count() == 0
    keys = page.evaluate("Object.keys(localStorage).filter(k => k.includes(':exam:'))")
    assert any(user["id"] in key for key in keys), "A's saved draft must survive"
    assert not any(second["id"] in key for key in keys), "B's practice is not an exam"
    assert not errors


@pytest.mark.parametrize("mid_case", [False, True])
def test_approved_practice_skip_continues_onboarding_and_survives_reload(portal, mid_case):
    page, store, user, errors = portal
    store.set_verification_status(user["id"], "approved")
    store.set_first_run(user["id"], {"version": store.FIRST_RUN_VERSION,
        "stops": {"welcome": "done", "start": "done"}, "sessions_seen": 1})
    page.reload()
    page.get_by_role("button", name="Start the case →", exact=True).wait_for()
    if mid_case:
        page.get_by_role("button", name="Start the case →", exact=True).click()
        page.get_by_role("button", name="Looks clinically valid, continue").click()
        page.locator(".asc-instinct-input").fill("My saved practice note.")
    before_tutorial = store.get_tutorial_state(user["id"])
    attempts = []

    def fail_once(route):
        if route.request.post_data_json.get("action") != "skip_practice":
            route.fallback()
            return
        attempts.append(1)
        if len(attempts) == 1:
            route.fulfill(status=503, content_type="application/json",
                          body='{"detail":"Temporary test outage"}')
        else:
            route.fallback()

    page.route("**/me/first-run", fail_once)
    skip = page.get_by_role("button", name="Skip practice case walkthrough", exact=True)
    skip.click()
    page.get_by_text("Could not save your preference. Please try skipping again.").wait_for()
    assert store.get_first_run(user["id"])["practice_skipped_at"] is None
    skip.click()
    page.get_by_role("button", name="Open the community", exact=True).wait_for()
    assert len(attempts) == 2
    first_run = store.get_first_run(user["id"])
    assert first_run["practice_skipped_at"]
    assert first_run["dismissed_at"] is None
    assert first_run["stops"] == {"welcome": "done", "start": "done"}
    assert store.get_tutorial_state(user["id"]) == before_tutorial
    if mid_case:
        assert page.evaluate("Object.values(localStorage).some(v => v.includes('My saved practice note.'))")
    screenshot(page, "practice-skipped-community.png")
    page.get_by_role("button", name="Do this later", exact=True).click()
    page.get_by_role("button", name="Do this later", exact=True).click()
    page.get_by_role("button", name="Open the manual", exact=True).wait_for()
    page.get_by_role("button", name="Open the manual", exact=True).click()
    page.locator(".asc-guide-content").wait_for()
    assert not store.get_first_run(user["id"])["dismissed_at"]
    page.reload()
    page.get_by_role("button", name="Finish these now", exact=False).wait_for()
    assert page.locator("#ascTourInterstitial").count() == 0
    assert page.get_by_text("Join the community", exact=True).is_visible()
    page.get_by_role("button", name="Sign out", exact=True).click()
    page.locator('input[autocomplete="username"]').fill(user["email"])
    page.locator('input[type="password"]').fill(PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.get_by_role("button", name="Finish these now", exact=False).wait_for()
    assert page.locator("#ascTourInterstitial").count() == 0
    assert not errors


@pytest.mark.parametrize("width,mid_case,resume", [(1440, False, False), (390, True, False), (1440, True, True)])
@pytest.mark.usefixtures("practice_preservation")
def test_applicant_skips_practice_directly_to_exam(portal, width, mid_case, resume):
    from playwright.sync_api import expect
    page, store, user, errors = portal
    page.set_viewport_size({"width": width, "height": 900})
    if resume:
        page.locator("#ascExamStart").click()
        page.get_by_role("button", name="Looks clinically valid, continue").click()
        page.locator(".asc-instinct-input").fill("My existing examination answer.")
        page.get_by_role("button", name="Pause and review").click()
    before_exam = store.get_tutorial_state(user["id"]).get("exam")
    before_first_run = store.get_first_run(user["id"])
    page.get_by_role("button", name="Optional: try a practice case first", exact=False).click()
    page.get_by_role("button", name="Start the case →", exact=True).wait_for()
    if mid_case:
        page.get_by_role("button", name="Start the case →", exact=True).click()
        page.get_by_role("button", name="Looks clinically valid, continue").click()
        page.locator(".asc-instinct-input").fill("My saved practice answer.")
    practice_before_skip = store.get_tutorial_state(user["id"])
    screenshot(page, f"applicant-practice-skip-{width}-{resume}.png")
    label = "Skip practice & continue examination →" if resume else "Skip practice & take examination →"
    page.get_by_role("button", name=label, exact=True).click()
    page.locator(".asc-exam-banner").wait_for()
    assert page.locator("#ascTourLayer, #ascTourInterstitial, #ascTourSkipConfirm").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    after = store.get_tutorial_state(user["id"])
    assert {k: v for k, v in after.items() if k != "exam"} == {
        k: v for k, v in practice_before_skip.items() if k != "exam"}
    assert store.get_first_run(user["id"]) == before_first_run
    assert after["exam"]["state"] == "in_progress"
    if resume:
        assert after["exam"] == before_exam
        expect(page.locator(".asc-instinct-input")).to_have_value("My existing examination answer.")
    if mid_case:
        assert page.evaluate("Object.values(localStorage).some(v => v.includes('My saved practice answer.'))")
    page.reload()
    page.get_by_role("button", name="Resume the examination").wait_for()
    assert page.locator("#ascTourInterstitial").count() == 0
    assert not errors


@pytest.mark.usefixtures("practice_preservation")
def test_applicant_skip_recovers_when_exam_is_temporarily_unavailable(portal):
    page, store, user, errors = portal
    page.get_by_role("button", name="Optional: try a practice case first", exact=False).click()
    page.get_by_role("button", name="Start the case →", exact=True).click()
    page.get_by_role("button", name="Looks clinically valid, continue").click()
    page.locator(".asc-instinct-input").fill("Keep this practice work after an outage.")
    page.route("**/exam/task", lambda route: route.fulfill(status=503, content_type="application/json",
        body='{"detail":"Temporary test outage"}'))
    page.get_by_role("button", name="Skip practice & take examination →", exact=True).click()
    page.get_by_text("Could not open your examination:", exact=False).wait_for()
    page.locator("#ascExamStart").wait_for()
    assert page.locator("#ascTourLayer, #ascTourInterstitial, #ascTourSkipConfirm").count() == 0
    assert page.evaluate("Object.values(localStorage).some(v => v.includes('Keep this practice work after an outage.'))")
    page.unroute("**/exam/task")
    page.locator("#ascExamStart").click()
    page.locator(".asc-exam-banner").wait_for()
    assert store.get_tutorial_state(user["id"])["exam"]["state"] == "in_progress"
    assert not errors


@pytest.mark.parametrize("practice", [True, False])
def test_applicant_skips_or_pauses_while_reveal_is_pending(portal, practice):
    from playwright.sync_api import expect
    page, store, user, errors = portal
    if practice:
        page.get_by_role("button", name="Optional: try a practice case first", exact=False).click()
        page.get_by_role("button", name="Start the case →", exact=True).click()
    else:
        page.locator("#ascExamStart").click()
    page.get_by_role("button", name="Looks clinically valid, continue").click()
    page.locator(".asc-instinct-input").fill("My independent clinical judgment.")
    held = []
    pattern = "**/tutorial/reveal" if practice else "**/tasks/*/reveal"
    page.route(pattern, lambda route: held.append(route))
    page.locator("#ascRevealBtn").click()
    expect(page.locator("#ascRevealBtn")).to_have_text("Revealing…")
    assert len(held) == 1
    if practice:
        page.get_by_role("button", name="Skip practice & take examination →", exact=True).click()
        page.locator(".asc-exam-banner").wait_for()
    else:
        page.get_by_role("button", name="Pause and review").click()
        page.locator("#ascExamStart").wait_for()
    with page.expect_response(lambda response: response.url.endswith("/reveal")):
        held[0].fallback()
    page.unroute(pattern)
    if practice:
        page.get_by_role("button", name="Looks clinically valid, continue").click()
        page.locator(".asc-instinct-input").fill("Low-solute hyponatremia; monitor correction closely.")
    else:
        page.get_by_role("button", name="Resume the examination").click()
        expect(page.locator(".asc-instinct-input")).to_have_value("My independent clinical judgment.")
    page.locator("#ascRevealBtn").click()
    page.locator(".asc-answer-body").first.wait_for()
    assert len(page.locator(".asc-answer-body").all_text_contents()) == 2
    assert page.locator(".asc-exam-banner").is_visible()
    assert not errors


@pytest.mark.parametrize("width,legacy_negative", [(1440, False), (390, True)])
@pytest.mark.usefixtures("practice_preservation")
def test_practice_scoring_can_finish_and_submit(portal, width, legacy_negative):
    from playwright.sync_api import expect
    page, store, user, errors = portal
    page.set_viewport_size({"width": width, "height": 900})
    page.get_by_role("button", name="Optional: try a practice case first", exact=False).click()
    page.get_by_role("button", name="Start the case →", exact=True).click()
    # Use the shipped tutorial's helpers to reach scoring. The rubric itself
    # is authored through its real controls, then submitted to the real API.
    for _ in range(20):
        copy = page.locator(".asc-tour-copy")
        text = copy.inner_text()
        if text.startswith("Add scoring criteria."):
            break
        page.locator(".asc-tour-actions .asc-btn").first.click()
        expect(copy).not_to_have_text(text)
    else:
        pytest.fail("The tutorial did not reach its scoring step")
    page.get_by_role("button", name="+ Add your own (optional)", exact=True).click()
    page.locator("#ascRubricWizard textarea").fill("Give IV fluids despite persistent congestion")
    page.get_by_role("button", name="Switch between must and must never", exact=True).click()
    expect(page.locator('#ascRubricWizard [data-tier="critical"]')).to_have_class("asc-rubric-tier-btn active")
    if legacy_negative:
        page.locator('#ascRubricWizard [data-tier="important"]').click()
    page.get_by_role("button", name="Next →", exact=True).click()
    if legacy_negative:
        expect(page.get_by_role("button", name="Save & finish")).to_be_disabled()
        expect(page.get_by_text("You have a “must never” criterion.", exact=False)).to_be_visible()
        screenshot(page, "practice-scoring-recovery-mobile.png")
        page.get_by_role("button", name="Mark Critical: Give IV fluids despite persistent congestion", exact=True).click()
    expect(page.get_by_role("button", name="Save & finish")).to_be_enabled()
    screenshot(page, f"practice-scoring-ready-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".asc-tour-pop").evaluate("el => { const r = el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }")
    assert page.locator(".asc-tour-skip").evaluate("el => { const r = el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }")
    page.get_by_role("button", name="Save & finish").click()
    page.locator('#ascConf [data-conf="high"]').click()
    page.locator("#ascSubmit").click()
    page.get_by_role("button", name="Take my examination →", exact=True).wait_for()
    assert store.get_tutorial_state(user["id"])["status"] == "completed"
    assert not store.get_tutorial_state(user["id"]).get("exam")
    assert not errors


@pytest.mark.parametrize('specialty,width', [('dermatology', 1440), ('neurology', 390)])
def test_generated_specialty_examination_entire_flow(portal, specialty, width):
    from tests.test_onboarding_specialty_cases import seed_case, set_credentials
    page, store, user, errors = portal
    set_credentials(store, user['id'], {'primarySpecialty': specialty})
    ident = seed_case(store, specialty, 'examination')
    # Exercise the same real API/browser scoring, refresh, retry and receipt
    # journey against a virtual bank case rather than ordinary gold inventory.
    test_applicant_can_start_and_resume_examination(portal, width, False)
    filed = store.list_credentialing_exams(user['id'])
    assert filed[0]['task_id'] == ident and filed[0]['specialty'] == specialty
    assert store.get_task(ident) is None


@pytest.mark.parametrize('kind,width', [('practice', 1440), ('examination', 390)])
def test_pathology_applicant_can_inspect_the_actual_slide(portal, monkeypatch, tmp_path, kind, width):
    from playwright.sync_api import expect
    from asclepius import onboarding_library
    from tests.test_onboarding_library import publication
    from tests.test_onboarding_specialty_cases import set_credentials
    page, store, user, errors = portal
    monkeypatch.setattr(onboarding_library, 'ROOT', tmp_path)
    doc = publication('pathology', kind)
    doc['entry']['case']['case_provenance'] = {'disclaimers': ['Synthetic patient scenario with a public-domain reference micrograph.']}
    import hashlib
    from asclepius import onboarding_cases, onboarding_media
    doc['entry'] = onboarding_cases.validate_entry(doc['entry'], 'pathology', doc['validation']['sources'], approved_asset=onboarding_media.reference(kind)['asset'])
    doc['validation']['entry_sha256'] = hashlib.sha256(json.dumps(doc['entry'], sort_keys=True).encode()).hexdigest()
    (tmp_path / (doc['task_id'] + '.json')).write_text(json.dumps(doc))
    set_credentials(store, user['id'], {'primarySpecialty': 'Pathology'})
    page.set_viewport_size({'width': width, 'height': 900})
    page.reload()
    if kind == 'practice':
        page.get_by_role('button', name='Optional: try a practice case first', exact=False).click()
        page.get_by_role('button', name='Start the case →', exact=True).click()
    else:
        page.locator('#ascExamStart').click()
    tab = page.locator('.asc-case-tab').filter(has_text='Path')
    if tab.count():
        tab.first.click()
    else:
        # Generic specialties use the Studies tab.
        page.locator('.asc-case-tab').filter(has_text='Studies').first.click()
    image = page.locator('.asc-img').first
    expect(image).to_be_visible()
    page.wait_for_function("document.querySelector('.asc-img')?.naturalWidth > 0")
    expect(page.locator('body')).not_to_contain_text('HELD OUT INTERPRETATION')
    expect(page.locator('body')).to_contain_text('public-domain reference micrograph')
    page.get_by_title('Zoom in (+)', exact=True).first.click()
    page.get_by_title('Reset view (0)', exact=True).first.click()
    screenshot(page, f'pathology-{kind}-{width}.png')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors


@pytest.mark.parametrize('specialty', ['dermatology', 'neurology'])
def test_generated_practice_can_be_skipped_and_its_draft_resumed(portal, specialty):
    from tests.test_onboarding_specialty_cases import seed_case, set_credentials
    page, store, user, errors = portal
    set_credentials(store, user['id'], {'primarySpecialty': specialty})
    practice = seed_case(store, specialty)
    exam = seed_case(store, specialty, 'examination')
    test_applicant_skips_practice_directly_to_exam(portal, 390, True, False)
    assert store.get_tutorial_state(user['id'])['practice_task_id'] == practice
    assert store.get_tutorial_state(user['id'])['exam']['task_id'] == exam


def test_pending_specialty_preparation_can_be_skipped_without_stale_navigation(portal):
    from tests.test_onboarding_specialty_cases import seed_case, set_credentials
    from playwright.sync_api import expect
    page, store, user, errors = portal
    set_credentials(store, user['id'], {'primarySpecialty': 'dermatology'})
    exam = seed_case(store, 'dermatology', 'examination')
    pending = []
    page.route('**/tutorial/task', lambda route: pending.append(route))
    page.get_by_role('button', name='Optional: try a practice case first', exact=False).click()
    page.get_by_role('button', name='Skip practice & take examination →', exact=True).click()
    page.locator('.asc-exam-banner').wait_for()
    pending[0].fulfill(status=202, json={'preparing': True, 'retry_after': 1, 'message': 'Checking dermatology practice…'})
    expect(page.locator('.asc-exam-banner')).to_be_visible()
    assert store.get_tutorial_state(user['id'])['exam']['task_id'] == exam
    assert not errors


def test_specialty_exam_preparation_polls_then_opens(portal):
    from tests.test_onboarding_specialty_cases import seed_case, set_credentials
    page, store, user, errors = portal
    set_credentials(store, user['id'], {'primarySpecialty': 'dermatology'})
    ident = seed_case(store, 'dermatology', 'examination')
    polls = []
    def preparing_once(route):
        polls.append(True)
        if len(polls) == 1:
            route.fulfill(status=202, json={'preparing': True, 'retry_after': 1, 'message': 'Checking your dermatology case…'})
        else:
            route.fallback()
    page.route('**/exam/task', preparing_once)
    page.locator('#ascExamStart').click()
    page.get_by_text('Checking your dermatology case…', exact=True).wait_for()
    page.locator('.asc-exam-banner').wait_for()
    assert len(polls) == 2
    assert store.get_tutorial_state(user['id'])['exam']['task_id'] == ident
    assert not errors


# Agreement enforcement: shipped UI, real API, and signing/retry preservation.
def _sign_agreement(portal):
    from tests._asclepius import headers_for
    result = portal.client.post('/api/asclepius/me/agreement/sign', headers=headers_for(portal.user),
        json={'typed_name': 'Tej Patel', 'signed_initials': 'TP', 'consent_esign': True})
    assert result.status_code == 200, result.text


def _sign_agreement_on_page(page):
    page.locator('#ascAgreementName').fill('Tej Patel')
    page.locator('#ascAgreementInitials').fill('TP')
    page.locator('#ascAgreementConsent').check()
    page.get_by_role('button', name='Sign and continue', exact=True).click()


def test_reviewer_is_sent_to_existing_sign_screen_and_resumes(accepted_portal, monkeypatch):
    from playwright.sync_api import expect
    monkeypatch.setenv('ASCLEPIUS_AGREEMENT_GATE', '1')
    portal = accepted_portal(tier='reviewer')
    page = portal.page
    page.goto('http://testserver/asclepius#review')
    expect(page.get_by_role('region', name='Agreement text')).to_be_visible()
    _sign_agreement_on_page(page)
    expect(page.get_by_role('region', name='Agreement text')).to_have_count(0)
    assert portal.store.latest_physician_agreement(portal.user['id'])
    expect(page.get_by_role('button', name='Check again', exact=True)).to_be_visible()
    assert portal.requests.count('/api/asclepius/review/pair/next') >= 2
    assert not portal.errors


def test_environment_queue_offers_the_existing_agreement_screen(accepted_portal, monkeypatch):
    from playwright.sync_api import expect
    monkeypatch.setenv('ASCLEPIUS_AGREEMENT_GATE', '1')
    portal = accepted_portal()
    page = portal.page
    page.goto('http://testserver/asclepius/env/annotate')
    link = page.get_by_role('link', name='Read and sign the agreement')
    expect(link).to_be_visible()
    with page.expect_popup() as popup_info:
        link.click()
    signing = popup_info.value
    expect(signing.get_by_role('region', name='Agreement text')).to_be_visible()
    _sign_agreement_on_page(signing)
    expect(signing.get_by_role('region', name='Agreement text')).to_have_count(0)
    page.reload()
    expect(page.locator('#envRoot')).to_contain_text('No trajectories awaiting annotation')
    assert not portal.errors


def test_arming_mid_annotation_keeps_unsaved_input_until_signature(accepted_portal, monkeypatch):
    from playwright.sync_api import expect
    monkeypatch.setenv('ASCLEPIUS_OPEN_CASE_POOL_ENABLED', '1')
    portal = accepted_portal()
    # The fixture selects assigned mode; this test explicitly covers open pool.
    monkeypatch.setenv('ASCLEPIUS_OPEN_CASE_POOL_ENABLED', '1')
    run = portal.store.insert_env_run(task_id='env-agreement-test', specialty='nephrology',
        task_type='diagnostic_workup', mode='rollout', compiled={},
        trajectory=[{'type': 'answer', 'content': 'A synthetic answer'}])
    page = portal.page
    page.goto('http://testserver/asclepius/env/annotate?run_id=' + run['run_id'])
    page.locator('#missed').fill('urine microscopy')
    monkeypatch.setenv('ASCLEPIUS_AGREEMENT_GATE', '1')
    page.get_by_role('button', name='Submit annotation').click()
    expect(page.get_by_role('link', name='Read and sign the agreement')).to_be_visible()
    expect(page.locator('#missed')).to_have_value('urine microscopy')
    assert not portal.store.get_env_run(run['run_id']).get('physician_annotation')
    _sign_agreement(portal)
    page.get_by_role('button', name='Submit annotation').click()
    expect(page.locator('#envSaveMsg')).to_contain_text('Saved')
    assert portal.store.get_env_run(run['run_id'])['physician_annotation']['missed_actions'] == ['urine microscopy']
    assert not portal.errors


@pytest.mark.parametrize('realm,prefix', [('live', ''), ('sandbox', '/sandbox')])
def test_signing_link_keeps_the_current_realm(accepted_portal, realm, prefix):
    from playwright.sync_api import expect
    portal = accepted_portal()
    page = portal.page
    page.goto('http://testserver/asclepius#agreement')
    expect(page.get_by_role('region', name='Agreement text')).to_be_visible()
    link = page.evaluate('''realm => {
        window.__REALM = realm;
        return window.AsclepiusAgreementGate.signingLink().getAttribute('href');
    }''', realm)
    assert link == prefix + '/asclepius#agreement'


def test_blocked_review_submit_preserves_notes_and_step_judgments(accepted_portal, monkeypatch):
    from playwright.sync_api import expect
    import json
    from tests.test_paired_review import _paired_task, _admin_h
    portal = accepted_portal(tier='reviewer')
    monkeypatch.setenv('ASCLEPIUS_OPEN_CASE_POOL_ENABLED', '1')
    tid = _paired_task(_admin_h(), max_labels=2)
    # Add divergent reasoning to these local fixture submissions so the UI
    # exposes the per-step judgment that ordinary review drafts do not save.
    with portal.store._conn() as conn:
        rows = conn.execute('SELECT submission_id,payload_json FROM submissions WHERE task_id=?', (tid,)).fetchall()
        for index, row in enumerate(rows):
            payload = json.loads(row['payload_json'])
            payload['reasoning_steps'] = [{'step': 1, 'text': ['Stabilize the myocardium.', 'Immediately start dialysis.'][index]}]
            conn.execute('UPDATE submissions SET payload_json=? WHERE submission_id=?',
                         (json.dumps(payload), row['submission_id']))
    page = portal.page
    page.goto('http://testserver/asclepius#review')
    page.get_by_role('radiogroup', name='Which is stronger?').get_by_role('radio', name='A', exact=True).click()
    for row in page.locator('[data-dim-idx]').all():
        row.get_by_role('radio', name='Agree', exact=True).click()
    fork = page.locator('.asc-rv-fork-row').get_by_role('radio', name='A', exact=True)
    fork.click()
    page.locator('[data-verdict="accept_with_edits"]').click()
    notes = page.get_by_placeholder('What is wrong, and what should change? Required on edits and on reject.')
    notes.fill('Use a safer potassium bath.')
    monkeypatch.setenv('ASCLEPIUS_AGREEMENT_GATE', '1')
    page.get_by_role('button', name='Submit adjudication', exact=True).click()
    expect(page.get_by_role('link', name='Read and sign the agreement')).to_be_visible()
    expect(notes).to_have_value('Use a safer potassium bath.')
    expect(fork).to_have_attribute('aria-checked', 'true')
    assert not portal.store.reviews_for_task(tid)
    _sign_agreement(portal)
    page.get_by_role('button', name='Submit adjudication', exact=True).click()
    expect(page.get_by_role('button', name='Check again', exact=True)).to_be_visible()
    reviews = portal.store.reviews_for_task(tid)
    assert reviews and reviews[0]['reviewer_notes'] == 'Use a safer potassium bath.'
    assert reviews[0]['step_divergence'][0]['judged'] in ('A', 'B')
    assert not portal.errors
