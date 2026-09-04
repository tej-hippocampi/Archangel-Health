"""The product is called Archangel Health. The codebase is still called Asclepius.

A physician walking the portal read "Asclepius" in the corner of a page whose
emails, landing site and signed agreement all say Archangel Health. The company
name is the one they should see, everywhere they can see anything.

This test pins BOTH directions, because a one-time sweep only protects the half
that is easy to do. It asserts that no visible "Asclepius" survives in the
shells, the clients or the mail we send, AND that every frozen identifier is
still exactly where it was.

WHY THE IDENTIFIERS ARE FROZEN. "Asclepius" is a word in copy and a name in
code, and only the first of those is the founders' to change. The /asclepius
route, the window.Asclepius* seams the sub-modules attach to, the asc-* CSS
prefix, the asclepius_token key physicians already carry in localStorage, the
X-Asclepius-* headers, the ASCLEPIUS_* variables the host is configured with,
the backend/asclepius package and the database file are all identifiers with
live readers on the other end. Renaming one is a migration with a rollout, not
a word swap, and doing it by accident during a copy sweep logs every open
session out at once.

Two deliberate exceptions, stated here so a later reader does not "fix" them:

  * ``product`` in the health-system onboarding router takes the literal value
    "asclepius", so the 400 that lists the accepted values names it. That
    string is the API's vocabulary, not a brand.
  * Log lines say "Asclepius:" as a subsystem prefix. An operator reading a log
    is looking for the subsystem, and nobody outside the team ever sees one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND = _ROOT / "frontend"
_ASC = _FRONTEND / "asclepius"
_BACKEND = _ROOT / "backend"
_LANDING = _ROOT / "landing" / "src"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── The shells ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "relative, expected_title",
    [
        ("asclepius/index.html", "<title>Archangel Health</title>"),
        ("asclepius/community.html", "<title>Archangel Health Community</title>"),
        ("asclepius/admin.html", "<title>Archangel Health Operations</title>"),
        ("buyer/index.html", "<title>Secure Data Workspace | Archangel Health</title>"),
    ],
)
def test_every_shell_titles_itself_with_the_company_name(relative, expected_title):
    """The browser tab is the one piece of chrome that is on screen even when
    the page is not, and it was the last place still saying Asclepius."""
    assert expected_title in _read(_FRONTEND / relative)


def test_the_portal_wordmark_and_its_loading_screen_say_archangel_health():
    html = _read(_ASC / "index.html")
    assert '<span class="asc-logo-text">Archangel Health' in html
    assert "Loading Archangel Health" in html
    assert "Loading Asclepius" not in html


def test_the_buyer_workspace_wordmark_says_archangel_health():
    assert '<span class="asc-logo-text">Archangel Health' in _read(_FRONTEND / "buyer/index.html")


# ── The clients ──────────────────────────────────────────────────────────────

#: Identifier substrings that legitimately carry the old name. A string literal
#: containing one of these is a key, a path or a header, not a sentence.
_JS_IDENTIFIER_EXEMPTIONS = (
    "/asclepius",
    "asclepius_token",
    "asclepius_draft",
    "asclepius_eval_surface",
    "X-Asclepius",
    "static/asclepius",
    "window.Asclepius",
)

_JS_STRING = re.compile(r"'([^'\\\n]*(?:\\.[^'\\\n]*)*)'|\"([^\"\\\n]*(?:\\.[^\"\\\n]*)*)\"")
_JS_LINE_COMMENT = re.compile(r"(?m)^\s*(?://|\*|/\*).*$")


def _js_string_literals(src: str):
    """Every quoted literal, with comment lines dropped first.

    Comments are excluded because this repo explains itself at length in them,
    and those explanations NAME the thing they are explaining. A test that read
    a comment about window.AsclepiusSession as shipped copy would force the
    reasoning to be deleted to make the brand check pass.
    """
    body = _JS_LINE_COMMENT.sub("", src)
    for m in _JS_STRING.finditer(body):
        yield m.group(1) if m.group(1) is not None else m.group(2)


@pytest.mark.parametrize(
    "relative",
    ["asclepius.js", "community.js", "admin_shell.js", "referral.js", "earnings.js"],
)
def test_no_visible_asclepius_survives_in_a_client_string(relative):
    for literal in _js_string_literals(_read(_ASC / relative)):
        if "Asclepius" not in literal:
            continue
        assert any(token in literal for token in _JS_IDENTIFIER_EXEMPTIONS), (
            f"{relative} ships a visible 'Asclepius' string: {literal!r}")


def test_the_landing_tells_a_user_the_company_name_not_the_codename():
    src = _read(_LANDING / "lib" / "auth-api.ts")
    assert "Could not open Asclepius workspace" not in src
    assert "has an Asclepius account" not in src
    assert "Archangel Health account" in src


# ── The mail, and everything else a person reads off the backend ─────────────

#: Modules whose STRING CONSTANTS reach a person: an inbox, an HTTP error a
#: client renders, or a file we hand a buyer.
_BACKEND_COPY_MODULES = (
    "onboarding_emails.py",
    "community/notify.py",
    "routers/asclepius_verify.py",
    "routers/asclepius_media.py",
    "asclepius/referrals.py",
    "asclepius/export.py",
)

#: Literals that name the API's own vocabulary rather than the product. See the
#: module docstring: `product` really is spelled "asclepius" on the wire.
_BACKEND_ALLOWED = ("asclepius",)


def _string_constants(path: Path):
    """String constants via AST, with docstrings excluded.

    Docstrings are the codebase talking to itself. Parsing rather than grepping
    is what makes "is this a docstring" a fact instead of a guess.
    """
    tree = ast.parse(_read(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.value


@pytest.mark.parametrize("relative", _BACKEND_COPY_MODULES)
def test_no_visible_asclepius_survives_in_backend_copy(relative):
    for value in _string_constants(_BACKEND / relative):
        if "Asclepius" not in value:
            continue
        pytest.fail(f"{relative} ships a visible 'Asclepius' string: {value!r}")


def test_the_admin_prompt_registry_labels_say_archangel_health():
    """Staff-visible, but still a UI: these labels render in the prompt tab."""
    src = _read(_BACKEND / "prompts" / "registry.py")
    assert '"label": "Asclepius' not in src
    assert '"label": "Archangel Health' in src


# ── The frozen identifiers, asserted where they are DEFINED ──────────────────

def test_the_seams_the_sub_modules_attach_to_are_untouched():
    """Asserted at the file that DEFINES each seam, not at a caller that merely
    reaches for one: a caller can be deleted while the seam survives, and the
    test would then pass while proving nothing."""
    assert "window.AsclepiusSession" in _read(_ASC / "earnings.js")
    assert "window.AsclepiusCasePanel" in _read(_ASC / "case_panel.js")
    assert "window.AsclepiusReview" in _read(_ASC / "review.js")
    assert "window.AsclepiusVerification" in _read(_ASC / "onboarding.js")


def test_the_route_the_storage_key_and_the_asset_prefix_are_untouched():
    html = _read(_ASC / "index.html")
    assert 'href="/asclepius"' in html
    assert "/static/asclepius/" in html
    assert 'class="asc-logo-text"' in html

    js = _read(_ASC / "asclepius.js")
    assert "asclepius_token" in js
    assert "/api/asclepius" in js

    assert '"/asclepius"' in _read(_BACKEND / "main.py")


def test_the_realm_and_auth_gate_headers_are_untouched():
    assert 'REALM_HEADER = "X-Asclepius-Realm"' in _read(_LANDING / "lib" / "auth-api.ts")
    assert 'AUTH_GATE_HEADER = "X-Asclepius-Auth-Gate"' in _read(_BACKEND / "asclepius" / "auth.py")
