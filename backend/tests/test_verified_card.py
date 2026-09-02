"""The shareable verified card.

A physician who has been verified gets a link they can put in a bio, a signature
or a message to a colleague, and it unfurls into a card with a checkmark. These
tests hold the two properties that make that safe to ship.

**The card can only ever say practice facts.** Name, specialty, years in
practice, country, picture. Never the contributor score or its band, which the
physician is not shown either; never an email, NPI or registration number,
which on a crawlable URL invite scraping and impersonation; never medical
school, graduation year, date of birth, sex or IMG status, which this product
does not collect at all because a pedigree field on a profile becomes a
pedigree field in somebody's judgment. That is a standing position, so the
assertions below are written against the whole payload and the whole rendered
page rather than against a list of keys somebody remembered to check.

**The link is live, revocable, and never an oracle.** The page reads the
account on every load, so revoking the token or losing approval takes it down.
An unknown token, a revoked token and an un-approved account all answer with
the same 404: anything else would let a stranger holding an old link probe
whether a named physician's standing had changed.
"""

from __future__ import annotations

import io
import json
import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from asclepius import card as asc_card
from asclepius.tiering import FORBIDDEN_CREDENTIAL_KEYS
from tests._asclepius import app, fresh_store, headers_for, make_user

client = TestClient(app)


def _approved_physician(store, **overrides):
    """A verified nephrologist with a full profile, as a real approved account.

    Approval is set through the store rather than assumed: ``create_user``
    leaves ``verification_status`` NULL, and NULL is deliberately not good
    enough to mint a card.
    """
    user = make_user(store, role="evaluator", tier="labeler", specialty="nephrology")
    fields = {
        "full_name": "Ahmed Al Otaibi",
        "npi": "1982736450",
        "country_of_practice": "SA",
        "country_of_licensure": "SA",
        "registry_id": "SCFHS-99881",
        "credentials_json": json.dumps({
            "primarySpecialty": "nephrology",
            "yearsInActivePractice": 12,
            # A legacy blob carrying keys we never collect. It must not matter:
            # the card is built from a whitelist, so these have no route out.
            "medicalSchool": "King Saud University",
            "gradYear": 2009,
            "sex": "M",
        }),
    }
    fields.update(overrides)
    sets = ", ".join(f"{k} = ?" for k in fields)
    with store._conn() as conn:
        conn.execute(f"UPDATE users SET {sets} WHERE id = ?",
                     (*fields.values(), user["id"]))
    store.set_verification_status(user["id"], "approved")
    return store.get_user_by_id(user["id"])


@pytest.fixture
def physician():
    store = fresh_store()
    return store, _approved_physician(store)


def _path(url: str) -> str:
    """The card URL as this TestClient can fetch it.

    ``card_url`` builds an absolute address on BASE_URL because that is what a
    physician pastes somewhere; the test drives the same route through the app.
    """
    return urlparse(url).path


def _mint(user) -> dict:
    r = client.post("/api/asclepius/me/card", headers=headers_for(user))
    assert r.status_code == 200, r.text
    return r.json()


# ─── Minting, re-minting, revoking ────────────────────────────────────────────


def test_minting_returns_a_url_that_a_stranger_can_open(physician):
    """The whole feature is a link that works for someone with no account."""
    _store, user = physician
    minted = _mint(user)
    page = client.get(_path(minted["url"]))
    assert page.status_code == 200
    assert "Ahmed Al Otaibi" in page.text


def test_re_minting_kills_the_previous_url(physician):
    """"I shared that with the wrong person" has to be one button.

    Re-minting overwrites the single stored hash, so there is no second live
    token to forget about.
    """
    _store, user = physician
    first = _mint(user)
    second = _mint(user)
    assert first["url"] != second["url"]
    assert client.get(_path(first["url"])).status_code == 404
    assert client.get(_path(second["url"])).status_code == 200


def test_revoking_takes_the_page_down(physician):
    """Revocation has to reach the page itself, not just the portal's view."""
    _store, user = physician
    minted = _mint(user)
    assert client.get(_path(minted["url"])).status_code == 200

    r = client.delete("/api/asclepius/me/card", headers=headers_for(user))
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert client.get(_path(minted["url"])).status_code == 404
    assert client.get(_path(minted["image_url"])).status_code == 404


def test_losing_approval_kills_a_card_that_was_already_minted(physician):
    """The token proves nothing about standing; the account is read live.

    A card is a claim that we verified this person. If that stops being true
    the link has to stop working on the next load, rather than circulating as a
    checkmark we have withdrawn.
    """
    store, user = physician
    minted = _mint(user)
    store.set_verification_status(user["id"], "pending")
    assert client.get(_path(minted["url"])).status_code == 404
    assert client.get(_path(minted["image_url"])).status_code == 404


def test_every_way_of_missing_answers_the_same_404(physician):
    """The page must not be an oracle about anybody's standing.

    Unknown, revoked and un-approved are indistinguishable from outside, so an
    old link cannot be used to find out that a named physician was rejected.
    """
    store, user = physician
    minted = _mint(user)
    store.set_verification_status(user["id"], "rejected")
    withdrawn = client.get(_path(minted["url"]))
    unknown = client.get("/api/asclepius/card/there-is-no-such-token")
    assert withdrawn.status_code == unknown.status_code == 404
    assert withdrawn.json() == unknown.json()


# ─── Who may mint ─────────────────────────────────────────────────────────────


def test_a_physician_awaiting_verification_cannot_mint(physician):
    """The checkmark asserts we checked. Minting before that is the product
    lying on its own domain, which is worse than the feature being absent."""
    store, user = physician
    store.set_verification_status(user["id"], "pending")
    r = client.post("/api/asclepius/me/card", headers=headers_for(user))
    assert r.status_code == 403


def test_an_account_that_is_not_a_physician_cannot_mint():
    """An advisor and a referrer are supporters, not clinicians.

    Approving one opens the surfaces their account kind allows and changes
    nothing about whether we will vouch for them as a doctor.
    """
    store = fresh_store()
    for kind in ("advisor", "referrer"):
        user = _approved_physician(store, account_kind=kind)
        r = client.post("/api/asclepius/me/card", headers=headers_for(user))
        assert r.status_code == 403, kind


def test_a_never_minted_card_has_no_page(physician):
    """Opt-in: an approved physician who never asked for one publishes nothing."""
    _store, _user = physician
    assert client.get("/api/asclepius/card/anything-at-all").status_code == 404


# ─── What the card may say ────────────────────────────────────────────────────


def test_the_serializer_emits_exactly_the_agreed_fields(physician):
    """A whitelist by construction, asserted as a whole set rather than by
    spot-checking absences: a key added to the payload later fails here rather
    than appearing quietly on a public URL."""
    _store, user = physician
    card = asc_card.card_payload(user)
    assert set(card) == set(asc_card.CARD_FIELDS)
    assert card["full_name"] == "Ahmed Al Otaibi"
    assert card["specialty"] == "nephrology"
    assert card["years_in_practice"] == 12
    assert card["country"] == "Saudi Arabia"
    assert card["verified"] is True


def test_nothing_internal_or_identifying_reaches_the_payload(physician):
    """Score, band, email, NPI, registration number, pedigree. None of it."""
    _store, user = physician
    blob = json.dumps(asc_card.card_payload(user)).lower()
    for banned in ("score", "band", "component", "npi", "registration",
                   "@", user["email"].lower(), "1982736450", "scfhs"):
        assert banned not in blob, banned
    for key in FORBIDDEN_CREDENTIAL_KEYS:
        assert key.lower() not in blob, key


def test_nothing_internal_or_identifying_reaches_the_rendered_page(physician):
    """The same audit against the HTML, because the page is what gets shared.

    The payload being clean is necessary and not sufficient: the page could
    have read the user row directly. It must not.
    """
    _store, user = physician
    minted = _mint(user)
    page = client.get(_path(minted["url"])).text
    haystack = page.lower()
    for banned in ("tier_score", "contributor score", "band", "npi",
                   "1982736450", "scfhs", user["email"].lower(),
                   "king saud", "2009"):
        assert banned not in haystack, banned
    # Whole words here, not substrings: a page carries markup as well as data,
    # and "og:image" contains "age" without saying anything about anybody.
    for key in FORBIDDEN_CREDENTIAL_KEYS:
        assert not re.search(rf"\b{re.escape(key.lower())}\b", haystack), key


def test_the_page_carries_the_tags_a_link_preview_reads(physician):
    """A shared link that unfurls to nothing is the same as not shipping this.

    Crawlers never run the page's JavaScript, which is why the page is rendered
    server-side and the tags are in the delivered HTML.
    """
    _store, user = physician
    minted = _mint(user)
    page = client.get(_path(minted["url"])).text
    for tag in ('property="og:title"', 'property="og:description"',
                'property="og:image"', 'property="og:url"',
                'name="twitter:card"'):
        assert tag in page, tag
    image_tag = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    assert image_tag and image_tag.group(1).endswith("/image")


def test_a_name_is_escaped_rather_than_rendered(physician):
    """A display name is typed by the physician, so it is untrusted input.

    The page is served from the same origin as the portal, whose session lives
    in localStorage, which is exactly the shape of hole a reflected name would
    open.
    """
    store, user = physician
    with store._conn() as conn:
        conn.execute("UPDATE users SET full_name = ? WHERE id = ?",
                     ("<script>alert(1)</script>", user["id"]))
    minted = _mint(store.get_user_by_id(user["id"]))
    page = client.get(_path(minted["url"])).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_a_sparse_profile_renders_as_absent_rather_than_as_a_blank(physician):
    """Absent stays absent. "0 years in practice" reads as a data error, and a
    physician is putting their professional face on this."""
    store, _user = physician
    bare = _approved_physician(
        store, full_name="Grace Achieng", npi=None, country_of_practice=None,
        registry_id=None, credentials_json="{}",
    )
    card = asc_card.card_payload(bare)
    assert card["years_in_practice"] is None
    assert card["country"] is None
    assert asc_card.practice_line(card) is None
    page = client.get(_path(_mint(bare)["url"])).text
    assert "Grace Achieng" in page
    assert "years in practice" not in page


# ─── The share image ──────────────────────────────────────────────────────────


def test_the_image_endpoint_returns_a_real_png(physician):
    """Previewers fetch the bytes and sniff them; a JSON error page with an
    image content-type would unfurl as a broken tile everywhere."""
    from PIL import Image

    _store, user = physician
    minted = _mint(user)
    r = client.get(_path(minted["image_url"]))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG\r\n\x1a\n")
    im = Image.open(io.BytesIO(r.content))
    im.load()
    assert im.size == (asc_card.IMAGE_W, asc_card.IMAGE_H)


def test_the_image_is_drawn_from_the_same_payload_as_the_page(physician):
    """One serializer, so the picture and the page cannot claim different
    things about the same physician. The renderer takes the card dict, which
    means there is nothing else in its input for it to leak."""
    _store, user = physician
    card = asc_card.card_payload(user)
    png = asc_card.render_card_png(card, None)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # Renders without an avatar too: a lost blob on an ephemeral asset store is
    # cosmetic, and must not take the share image down with it.
    assert len(png) > 1000
