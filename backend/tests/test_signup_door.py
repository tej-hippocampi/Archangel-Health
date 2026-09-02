"""The front door: can a physician who has never signed up actually get in?

Two regressions live here, both found by walking the product rather than by
reading it, and both invisible to every existing test.

1. THE HONEYPOT WAS AUTOFILLABLE. The self-serve field was ``company_website``
   with a visible "Company website" label. Chrome and Safari match
   address-profile fields on name, id, label and placeholder, and both ignore
   ``autocomplete="off"`` for address data — so a real physician with a saved
   profile had the honeypot filled for them, was classified as a bot, and got
   a decoy link that dead-ends on "Invalid or expired onboarding link".
   ``test_honeypot_returns_decoy_and_stores_nothing`` passes throughout: it
   tests that the trap fires, not that only bots step in it. The endpoint
   answers 200 and writes nothing, so there was no failure to observe.

2. THE SIGN-IN SCREEN HAD NO ROUTE TO SIGNUP. The portal door offered a
   password form, a password reset, and a sentence telling you to contact an
   administrator. A physician without an account had nowhere to go.

Both are asserted against the source, in the idiom test_paired_review.py uses
to keep payment vocabulary out of the review router: the property is
structural, so the check is structural.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LANDING = REPO / "landing" / "src" / "app" / "components"

# What Chrome/Safari autofill heuristics latch onto for an address profile.
# A honeypot that carries any of these is a honeypot that catches humans.
AUTOFILL_MAGNETS = (
    "company", "organization", "organisation", "website", "url",
    "address", "street", "city", "state", "zip", "postal",
    "phone", "tel", "name", "email",
)


def _honeypot_blocks(source: str) -> list[str]:
    """The honeypot `<input>` itself, plus any `<label>` bound to it.

    The honeypot is identified by its binding (`value={honeypot}`), never by
    its name — the whole point of the fix is that the name changed.

    Boundaries are tight on purpose. An earlier version walked back to the
    enclosing `<div>`, which swallowed the neighbouring email field's
    placeholder ("a.okafor@hospital.org") into the text being checked. That
    test passed, but only because no magnet token happened to appear in a
    string it should never have been reading. A check with the wrong boundaries
    is a check that will misfire on an unrelated edit and get deleted.
    """
    blocks = []
    for m in re.finditer(r"value=\{honeypot\}", source):
        start = source.rfind("<input", 0, m.start())
        assert start != -1, "honeypot binding is not on an <input>"
        end = source.index("/>", m.end()) + 2
        el = source[start:end]

        # A <label htmlFor=...> for this input, if one sits just above it.
        label = ""
        ids = re.findall(r'id=\{?["`]?([^"`}\s]+)', el)
        if ids:
            pat = re.escape(ids[0]).replace(r"\$\{kind\}", r"\$\{kind\}")
            lm = re.search(
                r"<label[^>]*htmlFor=\{?[\"`]?" + pat + r"[\"`]?\}?[^>]*>(.*?)</label>",
                source, re.S,
            )
            if lm:
                label = lm.group(0)
        blocks.append(el + "\n" + label)
    return blocks


def _signup_sources() -> list[tuple[str, str]]:
    files = [LANDING / "JoinEntry.tsx", LANDING / "LandingContactModals.tsx"]
    return [(f.name, f.read_text(encoding="utf-8")) for f in files if f.exists()]


def test_the_honeypot_carries_no_autofill_magnets():
    """A browser must not be able to fill the trap on a physician's behalf."""
    checked = 0
    for fname, src in _signup_sources():
        for block in _honeypot_blocks(src):
            checked += 1
            # Only the browser-visible identity of the field matters: its name,
            # its id, its placeholder, and any label text sitting with it.
            visible = " ".join(
                re.findall(r'(?:name|id|placeholder)=\{?["`]([^"`}]*)', block)
            )
            labels = " ".join(re.findall(r">([^<>{}]+)</label>", block))
            surface = f"{visible} {labels}".lower()
            for magnet in AUTOFILL_MAGNETS:
                assert magnet not in surface, (
                    f"{fname}: the honeypot's browser-visible identity contains "
                    f"{magnet!r} ({surface.strip()!r}). Chrome and Safari fill "
                    f"address-profile fields matched on name/id/label and ignore "
                    f"autocomplete=off for them, so a real physician gets the "
                    f"decoy link and a dead end."
                )
    assert checked >= 2, f"expected to find the signup honeypots, found {checked}"


def test_the_honeypot_still_exists_and_still_posts_company_website():
    """The trap must survive the rename. Only the browser signals moved; the
    API contract the backend reads (`company_website`) is unchanged."""
    found_field, found_wire = 0, 0
    for _fname, src in _signup_sources():
        found_field += len(_honeypot_blocks(src))
        found_wire += len(re.findall(r"company_website:\s*honeypot", src))
    assert found_field >= 2, "the honeypot field was removed, not renamed"
    assert found_wire >= 2, "the honeypot is no longer posted as company_website"


def test_the_honeypot_is_off_the_tab_order_and_hidden():
    """It still must not be reachable by a person using the form normally."""
    for fname, src in _signup_sources():
        for block in _honeypot_blocks(src):
            assert "tabIndex={-1}" in block, f"{fname}: honeypot is in the tab order"
            assert 'autoComplete="off"' in block, f"{fname}: honeypot allows autocomplete"


def test_the_signin_screen_offers_a_route_to_signup():
    """A physician who has never signed up must have somewhere to go from the
    door. The URL is injected into the shell because the landing is a different
    origin in production and the portal JS cannot derive it."""
    portal_js = (REPO / "frontend" / "asclepius" / "asclepius.js").read_text(encoding="utf-8")
    assert "asc-signup-url" in portal_js, (
        "the sign-in screen does not read the injected signup URL"
    )
    assert "Apply to contribute" in portal_js, (
        "the sign-in screen has no visible route to signing up"
    )

    main_py = (REPO / "backend" / "main.py").read_text(encoding="utf-8")
    shell = main_py[main_py.index("async def asclepius_portal"):][:2000]
    assert "asc-signup-url" in shell, (
        "the /asclepius shell no longer injects the signup URL, so the link "
        "on the sign-in screen renders as nothing"
    )
    assert "LANDING_URL" in shell, (
        "the signup URL is hard-coded rather than read from LANDING_URL"
    )
