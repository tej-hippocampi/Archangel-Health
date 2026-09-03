"""Sandbox PRD §1.3 — the client side of the realm.

  * The landing app has ONE ``apiHeaders()`` helper that injects
    ``X-Asclepius-Realm`` from ``?realm=sandbox`` (persisted in sessionStorage
    for the wizard's multi-page flow), and every fetch goes through it. The
    PRD's lint: no bare ``headers: {`` object remains in ``landing/src/lib``.
  * Each portal page module (evaluator/admin, community, provider, buyer)
    reads ``window.__REALM``, sends the header, and keys its stored token per
    realm so a live and a sandbox session coexist in one browser.
  * The ``/sandbox/*`` shell tag names the realm, wraps ``fetch`` and paints
    the banner (§3.1) before any module runs.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
LANDING_LIB = ROOT / "landing" / "src" / "lib"
LANDING_SRC = ROOT / "landing" / "src"
FRONTEND = ROOT / "frontend"

_PORTAL_MODULES = {
    "asclepius/asclepius.js": "asclepius_token",
    "asclepius/admin_shell.js": "asclepius_token",
    "asclepius/community.js": "asclepius_token",
    "buyer/buyer.js": "asclepius_buyer_token",
}


def _code_lines(text: str):
    """Source lines that are not comments (good enough for a lint)."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("*", "//", "/*")):
            continue
        yield line


# ─── Landing ─────────────────────────────────────────────────────────────────
def test_landing_lib_has_no_bare_headers_object():
    offenders = []
    for path in LANDING_LIB.glob("*.ts"):
        for i, line in enumerate(_code_lines(path.read_text(encoding="utf-8")), 1):
            if re.search(r"headers:\s*\{", line) and "apiHeaders(" not in line:
                offenders.append(f"{path.name}:{line.strip()}")
    assert not offenders, offenders


def _fetch_calls(text: str):
    """Each ``fetch(`` call's full argument text (brace/paren matched)."""
    for m in re.finditer(r"\bfetch\(", text):
        depth = 0
        j = m.end() - 1
        while j < len(text):
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        yield text[m.start():j + 1]


def test_every_landing_fetch_goes_through_api_headers():
    offenders = []
    for path in LANDING_SRC.rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        if "fetch(" not in text:
            continue
        for call in _fetch_calls(text):
            if call.startswith("fetch(`") or call.startswith('fetch("') or call.startswith("fetch(\n"):
                if "apiHeaders(" not in call:
                    offenders.append(f"{path.relative_to(ROOT)}: {call[:90]!r}")
    assert not offenders, "landing fetches without apiHeaders (Sandbox PRD §1.3):\n" + "\n".join(offenders)


def test_landing_api_headers_reads_the_realm_param_and_persists_it():
    text = (LANDING_LIB / "auth-api.ts").read_text(encoding="utf-8")
    assert "export function apiHeaders(" in text
    assert "export function currentRealm(" in text
    assert 'get("realm")' in text
    assert "sessionStorage" in text
    assert 'REALM_HEADER = "X-Asclepius-Realm"' in text
    # A sandbox sign-in lands on the sandbox shell.
    assert '"/sandbox/asclepius"' in text and '"/sandbox/provider"' in text


# ─── Portal page modules ─────────────────────────────────────────────────────
@pytest.mark.parametrize("rel,token_key", sorted(_PORTAL_MODULES.items()))
def test_portal_modules_send_the_realm_and_key_tokens_per_realm(rel, token_key):
    text = (FRONTEND / rel).read_text(encoding="utf-8")
    assert "window.__REALM" in text, rel
    assert "X-Asclepius-Realm" in text, rel
    assert f"'{token_key}_sandbox'" in text or f'"{token_key}_sandbox"' in text, rel
    assert f"'{token_key}'" in text or f'"{token_key}"' in text, rel


def test_provider_module_sends_the_realm():
    text = (FRONTEND / "provider" / "provider.js").read_text(encoding="utf-8")
    assert "window.__REALM" in text and "X-Asclepius-Realm" in text


def test_portal_cross_page_links_stay_in_the_realm():
    asc = (FRONTEND / "asclepius" / "asclepius.js").read_text(encoding="utf-8")
    assert "realmPath(t ? ('/community?t=' + encodeURIComponent(t)) : '/community')" in asc
    cm = (FRONTEND / "asclepius" / "community.js").read_text(encoding="utf-8")
    assert "href: realmPath('/asclepius')" in cm
    assert "href: '/asclepius'" not in cm


# ─── The shell tag ───────────────────────────────────────────────────────────
def test_sandbox_shell_tag_names_the_realm_wraps_fetch_and_paints_the_banner():
    import main
    tag = main._SANDBOX_SHELL_TAG
    assert "window.__REALM='sandbox'" in tag
    assert "h.set('X-Asclepius-Realm','sandbox')" in tag
    assert "ascRealmBanner" in tag
    assert "SANDBOX · nothing here reaches real users" in tag
    assert "/api/asclepius/sandbox/status" in tag
    assert "#c6f542" in tag                       # lime
    assert "dismiss" not in tag.lower()           # not dismissible


# ─── Audit finding: EVERY module that reads a token keys it per realm ─────────
def test_every_portal_module_that_reads_a_token_keys_it_per_realm():
    """earnings.js (the billable session client) and onboarding.js read the
    unkeyed token, so sandbox review sessions could never open; the XHR video
    upload in admin_health.js wrote into the live asset store."""
    offenders = []
    for path in sorted((FRONTEND / "asclepius").glob("*.js")):
        text = path.read_text(encoding="utf-8")
        if "localStorage.getItem" not in text or "asclepius_token" not in text:
            continue
        if "asclepius_token_sandbox" not in text:
            offenders.append(path.name)
    assert not offenders, offenders


def test_the_xhr_upload_names_the_realm_and_keys_its_token():
    text = (FRONTEND / "asclepius" / "admin_health.js").read_text(encoding="utf-8")
    assert "xhr.setRequestHeader('X-Asclepius-Realm', realm)" in text
    assert "localStorage.getItem('asclepius_token')" not in text
