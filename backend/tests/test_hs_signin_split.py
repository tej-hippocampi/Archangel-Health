"""The front doors: two role cards, no patient card, and the org flow behind one.

Source-level assertions on the landing dialogs and the portal, following the
pattern ``test_signed_in_landing.py`` sets. There is no jsdom in this
environment and standing one up to click two buttons would cost more than it
proves — what actually breaks here is somebody deleting a card, renaming a step,
or reintroducing the patient entry point, and every one of those is visible in
the source.

The one thing a renderer test would add and this cannot is that the cards
RENDER. The landing build (`npm run build`) is what covers that, and a card that
is present in the source and absent on screen is a CSS problem rather than the
regression this file is aimed at.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LANDING = ROOT / "landing" / "src"
SIGN_IN = LANDING / "app" / "components" / "SignInDialog.tsx"
SIGN_UP = LANDING / "app" / "components" / "SignUpDialog.tsx"
AUTH_API = LANDING / "lib" / "auth-api.ts"
PORTAL_JS = ROOT / "frontend" / "provider" / "provider.js"
PORTAL_HTML = ROOT / "frontend" / "provider" / "index.html"
ADMIN_JS = ROOT / "frontend" / "asclepius" / "admin_health.js"


def _strip_tsx_comments(src: str) -> str:
    """Block and line comments out; JSX text and string literals stay.

    Needed because both files now EXPLAIN the patient card's removal in a
    comment, and a test that greps for "Patient" without this would read the
    explanation as the thing it forbids.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


@pytest.fixture(scope="module")
def sign_in() -> str:
    return _strip_tsx_comments(SIGN_IN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sign_up() -> str:
    return _strip_tsx_comments(SIGN_UP.read_text(encoding="utf-8"))


# ─── §1 the split ────────────────────────────────────────────────────────────
def test_both_dialogs_offer_physician_and_health_system(sign_in, sign_up):
    for name, src in (("SignInDialog", sign_in), ("SignUpDialog", sign_up)):
        assert 'adg-role-title">Physician<' in src, f"{name} lost the physician card"
        assert "Health system / organization" in src, f"{name} lost the health-system card"
        # Sentence case, per §1. Title Case here reads as a product name and
        # this is a description of who they are.
        assert "Health System / Organization" not in src


def test_the_card_copy_is_the_prds(sign_in, sign_up):
    for src in (sign_in, sign_up):
        assert "Label, review, advise" in src
        assert "Contribute clinical data for task creation and licensing" in src


def test_the_patient_card_is_gone_from_both(sign_in, sign_up):
    for name, src in (("SignInDialog", sign_in), ("SignUpDialog", sign_up)):
        assert 'adg-role-title">Patient<' not in src, f"{name} still offers a patient card"


def test_the_patient_deep_link_still_works():
    """The card went; the STEP behind it did not, and must not.

    Care-team emails already in inboxes link to `/#recovery-plan`, which opens
    the sign-up dialog straight at its code-entry step. Deleting that step to
    tidy up the role screen would break a live URL for people who never see the
    role screen at all.
    """
    raw = SIGN_UP.read_text(encoding="utf-8")
    assert '"patient-codes"' in raw
    assert 'initialStep?: "role" | "patient-codes"' in raw
    hook = (LANDING / "app" / "hooks" / "useLandingAuth.ts").read_text(encoding="utf-8")
    assert "#recovery-plan" in hook
    assert 'setSignUpInitialStep("patient-codes")' in hook


def test_physician_routing_is_unchanged(sign_in, sign_up):
    """"Physician" routes exactly where "Doctor" routed — no backend change."""
    assert 'setStep("doctor")' in sign_in
    assert "redirectToDoctorPortal" in sign_in
    assert 'setStep("register")' in sign_up


def test_the_patient_backend_routes_are_untouched():
    """§1: a dialog edit. The peri-op surface keeps its endpoints."""
    api = AUTH_API.read_text(encoding="utf-8")
    assert "getPatientByCodes" in api
    main = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    assert "/api/patient/" in main


def test_signing_in_as_an_organization_goes_to_the_portal(sign_in):
    """They already have a username and a password for a different app, so the
    card navigates to it rather than rendering a second login form that would
    have to be kept in step with the portal's own."""
    assert "healthSystemPortalUrl()" in sign_in


# ─── §2 the three-field signup, in the dialog ────────────────────────────────
def test_the_org_signup_asks_for_three_things_and_no_password(sign_up):
    assert '"org"' in sign_up and '"org-verify"' in sign_up
    assert "Your name" in sign_up
    assert "Work email" in sign_up
    assert "Health system / organization name" in sign_up
    # One button, and it says Continue. The JSX renders it through a ternary,
    # so the assertion is on the string rather than on the element.
    assert '"Continue"' in sign_up
    # The password field belongs to the portal's own signup screen, not to this
    # one. A password input here would defeat §0.1.1's whole point.
    org_step = sign_up[sign_up.index('{step === "org" && ('):sign_up.index('{step === "org-verify"')]
    assert 'type="password"' not in org_step


def test_the_org_signup_carries_the_same_abuse_guards(sign_up):
    """The honeypot the rest of the landing forms use, under the same field
    name, so bots already filling it in keep filling it in."""
    assert 'name="company_website"' in sign_up
    assert "orgHoneypot" in sign_up


def test_the_verify_step_sends_the_session_cookie_home():
    """`credentials: "include"` is load-bearing: verification hands back an
    HttpOnly cookie on the API origin, and without this the browser drops it and
    the organization lands on a login screen holding a username it has never
    seen."""
    api = AUTH_API.read_text(encoding="utf-8")
    block = api[api.index("export async function healthSystemVerify"):]
    assert 'credentials: "include"' in block[:800]


def test_the_username_is_shown_before_the_redirect(sign_up):
    """It was derived from their organization name and they never chose it. If
    the cookie does not survive the hop to the portal, this screen is the only
    place they can read it before their email arrives."""
    assert '"org-done"' in sign_up
    assert "{orgUsername}" in sign_up


# ─── The portal renders by state ─────────────────────────────────────────────
def test_the_portal_gates_its_upload_tab_on_the_organizations_state():
    src = PORTAL_JS.read_text(encoding="utf-8")
    assert "function orgState()" in src
    assert 'if (item.dest === "upload") return orgState() === "active";' in src


def test_the_portal_has_a_panel_for_every_step_of_the_flow():
    html = PORTAL_HTML.read_text(encoding="utf-8")
    for template in ("tplApplication", "tplAgreement", "tplUpload", "tplPayouts",
                     "tplAccount", "tplPending"):
        assert f'id="{template}"' in html, template
    js = PORTAL_JS.read_text(encoding="utf-8")
    for fn in ("renderApplication", "renderAgreement", "renderMembers", "renderNotes"):
        assert f"function {fn}(" in js, fn


def test_the_signing_surface_requires_both_boxes_before_the_button_enables():
    """The server refuses either way. This is the same rule made visible, which
    is what affirmative assent means in practice."""
    js = PORTAL_JS.read_text(encoding="utf-8")
    block = js[js.index("async function renderAgreement("):]
    assert "btn.disabled = !(authority.checked && esign.checked &&" in block
    # And the text is rendered as TEXT. A contract parsed as markup is a
    # contract that renders differently from the bytes that were hashed.
    assert "textEl.textContent = data.text" in block
    assert "innerHTML" not in block


def test_the_portal_shows_the_integrity_receipt_for_every_upload():
    js = PORTAL_JS.read_text(encoding="utf-8")
    assert "prv-hist-integrity" in js
    assert "u.sha256.slice(0, 16)" in js


# ─── The admin card ──────────────────────────────────────────────────────────
def test_the_admin_list_carries_a_state_chip_and_a_dla_chip():
    js = ADMIN_JS.read_text(encoding="utf-8")
    assert "const STATE_CHIPS" in js
    for state in ("intake", "submitted", "approved_awaiting_dla", "active", "declined"):
        assert state in js, state
    assert "function dlaChip(" in js
    # "no DLA" is rendered rather than an empty cell: an organization that is
    # active without an agreement on file is a real condition, and a blank
    # reads as "not loaded".
    assert "'no DLA'" in js


def test_the_admin_queue_offers_approve_and_decline_with_a_required_note():
    js = ADMIN_JS.read_text(encoding="utf-8")
    assert "'/approve'" in js
    assert "'/decline'" in js
    assert "A reason is required to decline." in js


def test_the_admin_never_offers_to_edit_a_signature():
    """The row is append-only in the database. A UI offering an edit the
    database refuses teaches an operator that the record is negotiable."""
    js = ADMIN_JS.read_text(encoding="utf-8")
    block = js[js.index("function renderAgreementsCard("):]
    block = block[:block.index("\n  // ─── What they told us")]
    for verb in ("PUT", "DELETE", "method: 'DELETE'"):
        assert verb not in block


# ─── §8 the emails ───────────────────────────────────────────────────────────
def test_every_new_email_renders_in_the_preview():
    """The preview is the only place these are ever looked at before they land
    in a hospital's inbox, so a builder that is not in it is a builder nobody
    has seen."""
    import onboarding_emails as oe

    preview = (ROOT / "backend" / "scripts" / "email_preview.py").read_text(encoding="utf-8")
    for builder in ("build_hs_access_email", "build_hs_member_added_email",
                    "build_hs_dla_request_email", "build_hs_agreement_receipt_email",
                    "build_hs_uploads_open_email", "build_hs_application_alert"):
        assert hasattr(oe, builder), builder
        assert f"oe.{builder}(" in preview, f"{builder} is not in the email preview"


def test_the_preview_actually_renders_them():
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "backend" / "scripts" / "email_preview.py"),
             "--out", tmp],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        rendered = {p.name for p in Path(tmp).glob("*.html")}
    for slug in ("26-hs-access.html", "27-hs-member-added.html",
                 "28-hs-dla-request.html", "29-hs-agreement-receipt.html",
                 "30-hs-uploads-open.html", "31-hs-application-alert.html"):
        assert slug in rendered, slug
