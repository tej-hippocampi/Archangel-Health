"""Exercise the shipped portal against real API routes and isolated stores."""
from urllib.parse import urlsplit
import uuid
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, make_user
from tests._physician_application import PASSWORD, submit_physician_application


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


def test_applicant_can_start_and_resume_examination(portal):
    page, store, user, errors = portal
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
        next_button.click()
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
    second = make_user(store, tier=None, practice_case=False)
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
