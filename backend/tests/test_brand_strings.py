"""The physician reads "Archangel Health". The codebase still says "Asclepius".

Both halves matter, and the second is why this file exists rather than a
one-time grep. "Asclepius" is the internal name of this codebase and it is load
bearing in about a hundred places that are NOT copy: the ``/asclepius`` routes,
the ``window.Asclepius*`` seams the sub-modules attach to, the ``asc-*`` CSS
prefix, the localStorage keys physicians already have on their machines, the
``X-Asclepius-*`` headers, the ``ASCLEPIUS_*`` environment variables Railway is
configured with, the ``backend/asclepius`` package, and the database file name.
Renaming any of those is a migration, not a rebrand, and doing it by accident
during a copy sweep is how a deploy loses every session at once.

So this file pins the rebrand from both directions:

  * no user-visible "Asclepius" survives in the portal shell, the community
    client, or the transactional email builders;
  * every identifier that must NOT be renamed is asserted still present, so a
    future sweep that is too enthusiastic fails here instead of in production.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PORTAL = _ROOT / "frontend" / "asclepius"
_BACKEND = _ROOT / "backend"

BRAND = "Archangel Health"
INTERNAL = "Asclepius"


def _read(rel: Path) -> str:
    return rel.read_text(encoding="utf-8")


# ── The strings a physician actually reads ───────────────────────────────────

def test_the_portal_wordmark_and_title_say_archangel_health():
    html = _read(_PORTAL / "index.html")
    assert f"<title>{BRAND}</title>" in html
    assert f'<span class="asc-logo-text">{BRAND}' in html
    assert f"Loading {BRAND}" in html
    assert "Expert Evaluation Portal" not in html


def test_the_community_page_title_says_archangel_health():
    assert f"<title>{BRAND} Community</title>" in _read(_PORTAL / "community.html")


def _js_string_literals(source: str):
    """Every single- or double-quoted literal in a JS file, roughly.

    Deliberately rough: this only has to be good enough to tell a string apart
    from a comment, and the portal is hand-written vanilla JS with no minified
    payloads or regex-literal thickets to confuse it.
    """
    return re.findall(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"", source)


def _visible_internal_name_hits(path: Path):
    """String literals naming the internal product, minus the identifiers.

    A literal is exempt when it IS an identifier: a route, a storage key, a
    header, a static path. Those are not copy and must not be swept.
    """
    exempt = (
        "/asclepius", "asclepius_token", "asclepius_draft", "asclepius_eval_surface",
        "X-Asclepius", "static/asclepius", "window.Asclepius",
    )
    out = []
    for lit in _js_string_literals(_read(path)):
        if INTERNAL not in lit:
            continue
        if any(e in lit for e in exempt):
            continue
        out.append(lit)
    return out


def test_no_visible_asclepius_string_survives_in_the_portal():
    assert _visible_internal_name_hits(_PORTAL / "asclepius.js") == []


def test_no_visible_asclepius_string_survives_in_the_community_client():
    assert _visible_internal_name_hits(_PORTAL / "community.js") == []


def test_no_transactional_email_calls_the_product_asclepius():
    """Subjects and body copy only. Docstrings and comments are not copy."""
    import ast

    src = _read(_BACKEND / "onboarding_emails.py")
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if INTERNAL in node.value and not node.value.lstrip().startswith(("Asclepius —", "Asclepius:")):
                # A module/function docstring is an ast.Constant too; those are
                # the codebase talking to itself, and they are allowed to use
                # the internal name. Docstrings are the first statement of a
                # body, so they are filtered by position below.
                hits.append(node)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    leaked = [n.value for n in hits if n.value not in docstrings]
    assert leaked == [], f"email copy still says {INTERNAL}: {leaked}"


# ── The identifiers that must NOT be renamed ─────────────────────────────────

def test_the_frozen_identifiers_are_still_here():
    """A rebrand sweep that renames any of these is a production outage.

    Sessions, deep links, the sub-module seams and the deployed environment all
    key off these exact strings.
    """
    portal_js = _read(_PORTAL / "asclepius.js")
    # Each seam is asserted where it is DEFINED, not where it happens to be
    # called: the shell only reaches for some of them, and a test that greps the
    # shell alone would pass while the definition was renamed out from under it.
    seams = {
        "window.AsclepiusSession": "earnings.js",
        "window.AsclepiusCasePanel": "case_panel.js",
        "window.AsclepiusVerification": "onboarding.js",
        "window.AsclepiusCalibration": "onboarding.js",
        "window.AsclepiusDemographics": "onboarding.js",
        "window.AsclepiusReview": "review.js",
    }
    for seam, owner in seams.items():
        assert seam in _read(_PORTAL / owner), f"frozen seam disappeared: {seam} ({owner})"
    assert "asclepius_token" in portal_js
    assert "/api/asclepius" in portal_js

    index_html = _read(_PORTAL / "index.html")
    assert 'href="/asclepius"' in index_html
    assert "/static/asclepius/" in index_html
    assert 'class="asc-logo-text"' in index_html

    main_py = _read(_BACKEND / "main.py")
    assert '"/asclepius"' in main_py or "'/asclepius'" in main_py


# ── The founders' signature ──────────────────────────────────────────────────

def test_the_founder_signature_degrades_to_names_when_there_is_no_photo():
    """A broken image in an inbox is worse than no image.

    The photo is deliberately not in the repo (it is a picture of real people),
    so the DEFAULT state of this code in a fresh checkout is "no photo". That
    state has to be the good one, not a placeholder box with a torn-page icon
    next to a request for someone's licence number.
    """
    import importlib
    import os

    oe = importlib.import_module("onboarding_emails")
    old = os.environ.pop("FOUNDER_PHOTO_URL", None)
    try:
        out = oe._founder_signoff("Tej & Aryaa, founders")
        assert "<img" not in out
        assert "Tej &amp; Aryaa, founders" in out
    finally:
        if old is not None:
            os.environ["FOUNDER_PHOTO_URL"] = old


def test_the_founder_signature_renders_the_photo_when_one_is_configured():
    import importlib
    import os

    oe = importlib.import_module("onboarding_emails")
    old = os.environ.get("FOUNDER_PHOTO_URL")
    os.environ["FOUNDER_PHOTO_URL"] = "https://archangelhealth.ai/email-assets/founders.jpg"
    try:
        out = oe._founder_signoff("Tej & Aryaa, founders")
        assert "https://archangelhealth.ai/email-assets/founders.jpg" in out
        # A table, not flexbox: Outlook renders with Word, which has neither
        # flexbox nor grid, and the signature would stack into two lines.
        assert "<table" in out and "flex" not in out
        # Alt text, because most clients block remote images by default and the
        # reader should still know whose signature this is.
        assert 'alt="Tej &amp; Aryaa, founders"' in out
    finally:
        if old is None:
            os.environ.pop("FOUNDER_PHOTO_URL", None)
        else:
            os.environ["FOUNDER_PHOTO_URL"] = old


def test_a_relative_photo_url_is_never_emitted():
    """An email is read outside our origin; a relative URL resolves to nothing."""
    import importlib
    import os

    oe = importlib.import_module("onboarding_emails")
    saved = {k: os.environ.pop(k, None) for k in ("FOUNDER_PHOTO_URL", "BASE_URL")}
    try:
        url = oe._founder_photo_url()
        assert url == "" or url.startswith(("http://", "https://")), url
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
