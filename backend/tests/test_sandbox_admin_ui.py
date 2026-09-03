"""Sandbox PRD §3 — the sandbox admin console, and §6.7 (the banner always renders).

Source assertions over the shell and the new module (the pattern every other
admin section test in this suite uses), API assertions for the origin chip's
data (§3.4), and a real-engine render of every ``/sandbox/*`` route asserting
the banner is on screen (Playwright, when available in the environment).
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

import realm  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402

client = TestClient(A.app)

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
SHELL_JS = (FRONTEND / "admin_shell.js").read_text(encoding="utf-8")
SANDBOX_JS = (FRONTEND / "admin_sandbox.js").read_text(encoding="utf-8")
HEALTH_JS = (FRONTEND / "admin_health.js").read_text(encoding="utf-8")
ADMIN_HTML = (FRONTEND / "admin.html").read_text(encoding="utf-8")
INDEX = (FRONTEND / "index.html").read_text(encoding="utf-8")

SANDBOX_ROUTES = ["/sandbox/asclepius", "/sandbox/admin", "/sandbox/provider",
                  "/sandbox/buyer", "/sandbox/workspace", "/sandbox/community"]


@pytest.fixture
def sandbox_on(monkeypatch):
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "sandbox-admin-secret")
    monkeypatch.setenv(realm.DOCTOR_PASSWORD_VAR, "sandbox-doctor-secret")
    yield


# ─── §3.2 / §3.3 — the console gains a Sandbox section, sandbox realm only ───
def test_console_adds_the_sandbox_tab_only_in_the_sandbox_realm():
    """The console lives on /asclepius/admin (admin_shell.js, PRD-F R3); the
    physician bundle must not grow a console back."""
    assert "if (REALM === 'sandbox') ADMIN_TABS.push(['sandbox', 'Sandbox']);" in SHELL_JS
    assert "state.adminTab === 'sandbox' && REALM === 'sandbox'" in SHELL_JS
    assert "sandbox: 'accounts'" in SHELL_JS
    assert "['accounts', 'Accounts'], ['outbox', 'Outbox']" in SHELL_JS
    assert '<script src="/static/asclepius/admin_sandbox.js" defer></script>' in ADMIN_HTML
    assert "admin_sandbox.js" not in INDEX
    assert "window.AdminSandboxSection = { render };" in SANDBOX_JS
    # The shell sends the realm and keys its token per realm like every page.
    assert "window.__REALM" in SHELL_JS and "X-Asclepius-Realm" in SHELL_JS
    assert "'asclepius_token_sandbox'" in SHELL_JS


def test_accounts_tab_has_credentials_reset_fresh_and_onboarding_doors():
    for needle in ("/accounts", "/seed", "/accounts/fresh", "/reset", "RESET SANDBOX",
                   "Copy password", "Seed fresh doctor", "Reset sandbox",
                   "Start a fake physician onboarding", "Start a fake org onboarding",
                   "/copy-sources", "/copy-health-system/"):
        assert needle in SANDBOX_JS, needle
    # Passwords come from the server (env) — never from this file.
    assert "sandbox-admin" not in SANDBOX_JS.lower().replace("sandbox-admin@", "")
    assert not re.search(r"password\s*[:=]\s*['\"][^'\"]{6,}['\"]", SANDBOX_JS)


def test_outbox_tab_shows_codes_links_and_rendered_html():
    for needle in ("/outbox", "m.codes", "m.links", "srcdoc", "sandbox: ''", "Clear outbox"):
        assert needle in SANDBOX_JS, needle


# ─── §3.4 origin chip ────────────────────────────────────────────────────────
def test_health_systems_list_carries_origin_and_the_chip_renders_it():
    assert "originChip(h, fmtDate, r)" in HEALTH_JS
    assert "'production copy'" in HEALTH_JS and "'sandbox onboarded'" in HEALTH_JS


def test_a_health_system_created_in_the_sandbox_is_stamped_sandbox(sandbox_on):
    live = A.fresh_store()
    live_admin = A.make_user(live, role="admin")
    live_hs = live.create_health_system_unclaimed("Live General Hospital", contact_email="x@live.example")
    assert live_hs["origin"] is None
    with realm.scoped("sandbox"):
        sb = A.fresh_store()
        sb_admin = A.make_user(sb, role="admin")
        sb_hs = sb.create_health_system_unclaimed("Sandbox Test Hospital", contact_email="x@sb.example")
        assert sb_hs["origin"] == "sandbox"
        sb_token = asc_auth.create_token(sb_admin)
    r = client.get("/api/asclepius/admin/health-systems", headers={"Authorization": "Bearer " + sb_token})
    assert r.status_code == 200, r.text
    rows = {x["hs_id"]: x for x in r.json()["health_systems"]}
    assert rows[sb_hs["hs_id"]]["origin"] == "sandbox"
    assert live_hs["hs_id"] not in rows
    r = client.get("/api/asclepius/admin/health-systems", headers=A.headers_for(live_admin))
    rows = {x["hs_id"]: x for x in r.json()["health_systems"]}
    assert rows[live_hs["hs_id"]]["origin"] is None
    assert sb_hs["hs_id"] not in rows


# ─── §3.1 / §6.7 — the banner, on every route, in a real engine ──────────────
def _launch_browser():
    pw = pytest.importorskip("playwright.sync_api")
    p = pw.sync_playwright().start()
    browser = None
    last = None
    # The bundled download first; then the pre-installed system chromium
    # (CI images and the dev container pin one at this path).
    for kwargs in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try:
            browser = p.chromium.launch(**kwargs)
            break
        except Exception as exc:  # pragma: no cover — no browser in this environment
            last = exc
    if browser is None:
        p.stop()
        pytest.skip(f"no chromium available for playwright: {last}")
    return p, browser


@pytest.mark.parametrize("path", SANDBOX_ROUTES)
def test_realm_banner_renders_on_every_sandbox_route(sandbox_on, path):
    html = client.get(path).text
    p, browser = _launch_browser()
    try:
        page = browser.new_page()
        # Module scripts 404 under set_content (no server); the shell tag is
        # inline and runs regardless — which is the point of putting it there.
        page.route("**/*", lambda route: route.abort() if route.request.url.startswith("http") else route.continue_())
        page.set_content(html, wait_until="domcontentloaded")
        banner = page.locator("#ascRealmBanner")
        assert banner.count() == 1, path
        assert banner.is_visible(), path
        assert "SANDBOX" in banner.inner_text() and "nothing here reaches real users" in banner.inner_text()
        box = banner.bounding_box()
        assert box is not None and box["y"] == 0, (path, box)          # top of viewport
        bg = banner.evaluate("el => getComputedStyle(el).backgroundColor")
        assert bg == "rgb(198, 245, 66)", bg                              # lime
        # Nothing dismissible: no button, no close control inside it.
        assert banner.locator("button, [role=button], .close").count() == 0
        # The admin page gets the full banner, the others the thinner one.
        fs = banner.evaluate("el => getComputedStyle(el).fontSize")
        assert fs == ("14px" if path == "/sandbox/admin" else "12px"), (path, fs)
    finally:
        browser.close()
        p.stop()


def test_sandbox_admin_serves_the_operations_shell(sandbox_on):
    html = client.get("/sandbox/admin").text
    assert "admin_shell.js" in html and "admin_sandbox.js" in html
    assert "window.__REALM_FULL_BANNER=true" in html
    portal = client.get("/sandbox/asclepius").text
    assert '<script src="/static/asclepius/admin_shell.js"' not in portal
    assert "window.__REALM_FULL_BANNER=true" not in portal


def test_live_routes_have_no_banner(sandbox_on):
    for path in ("/asclepius", "/asclepius/admin", "/provider", "/workspace", "/community"):
        html = client.get(path).text
        assert "window.__REALM='sandbox'" not in html, path
        assert "b.id='ascRealmBanner'" not in html, path
