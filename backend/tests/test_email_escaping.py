"""Nothing a stranger types reaches an operator's inbox as markup.

The health-system signup form is PUBLIC. Three fields, no approval, and the
organization name a stranger types is rendered straight into the headline of the
alert that lands in the founders' inbox — beside the Approve and Decline
buttons they are about to press.

``_h1`` does not escape, deliberately: several callers pass markup on purpose, so
escaping inside it would double-escape them. The convention is that the caller
escapes, and three of these builders did not. That is the kind of rule a comment
cannot hold on its own, so it is held here instead.

The test is written against the BUILDERS rather than the routes because that is
where the escaping decision lives, and because a builder is the thing somebody
copies when they add the next alert.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import onboarding_emails as oe  # noqa: E402

#: A payload that is legible in a failure message and covers the two things that
#: actually matter in an email client: a link the reader might click, and an
#: image that phones home when the message is opened.
EVIL = ('Mercy Health<a href="https://attacker.example/approve">Approve</a>'
        '<img src="https://attacker.example/pixel.gif">')

#: Every builder that renders an attacker-supplied ORGANIZATION name, and how to
#: call it. All three are reachable from the public self-signup flow.
_ORG_ALERTS = {
    "application": lambda org: oe.build_hs_application_alert(
        organization=org, hs_id="hs-x", full_name="Dana Reyes",
        email="d@example.org", answers=[("Authority to license", "Yes")]),
    "signup": lambda org: oe.build_hs_signup_alert(
        full_name="Dana Reyes", email="d@example.org", organization=org,
        hs_id="hs-x", username="u"),
    "intake": lambda org: oe.build_hs_intake_alert(
        full_name="Dana Reyes", email="d@example.org", organization=org,
        answers={"data_held": "Epic"}, hs_id="hs-x"),
}


@pytest.mark.parametrize("name", sorted(_ORG_ALERTS))
def test_an_organization_name_cannot_carry_markup_into_our_inbox(name):
    body = _ORG_ALERTS[name](EVIL)
    assert '<a href="https://attacker.example/approve">' not in body
    assert '<img src="https://attacker.example/pixel.gif">' not in body
    # Escaped, not stripped: the operator still reads what they actually typed,
    # which is what tells them this signup is hostile.
    assert "&lt;a href=" in body
    assert "Mercy Health" in body


@pytest.mark.parametrize("name", sorted(_ORG_ALERTS))
def test_the_organization_name_still_renders(name):
    """The escaping must not have been bought by dropping the value."""
    body = _ORG_ALERTS[name]("St Mary&apos;s Health")
    assert "St Mary" in body


def test_a_partner_facing_letter_escapes_the_organization_too():
    """Not only the internal alerts. These go to a colleague the signer named,
    so the attacker chooses both the organization and the recipient."""
    for body in (
        oe.build_hs_access_email(organization=EVIL, full_name="Dana",
                                 claim_url="https://example.org/provider?invite=t",
                                 portal_url="https://example.org/provider"),
        oe.build_hs_member_added_email(organization=EVIL, added_by="Dana",
                                       claim_url="https://example.org/provider?invite=t",
                                       portal_url="https://example.org/provider"),
        # The two /partner letters, which reach an organization we have never
        # met: the name in them is whatever a stranger typed into a public form.
        oe.build_hs_interest_thanks_email(
            full_name="Dana", organization=EVIL,
            booking_url="https://example.org/book"),
        oe.build_hs_interest_reminder_email(
            full_name=EVIL, organization=EVIL,
            booking_url="https://example.org/book"),
        oe.build_hs_dla_request_email(organization=EVIL,
                                      portal_url="https://example.org/provider"),
        oe.build_hs_uploads_open_email(organization=EVIL,
                                       portal_url="https://example.org/provider",
                                       signer_name="Dana", signed_at="2026-03-14"),
    ):
        assert '<a href="https://attacker.example/approve">' not in body
        assert '<img src="https://attacker.example/pixel.gif">' not in body


def test_the_signers_typed_name_cannot_carry_markup_either():
    """It reaches the countersigned receipt and the uploads-are-open broadcast.
    The route caps and whitespace-collapses it; the builder must not rely on
    that, because the next caller might not."""
    body = oe.build_hs_agreement_receipt_email(
        organization="St Mary's Health", doc_version="v1",
        signer_name=EVIL, signer_title=EVIL, signed_at="2026-03-14T00:00:00",
        doc_sha256="a" * 64)
    assert '<a href="https://attacker.example/approve">' not in body
    body = oe.build_hs_uploads_open_email(
        organization="St Mary's Health", portal_url="https://example.org/provider",
        signer_name=EVIL, signed_at="2026-03-14T00:00:00")
    assert '<a href="https://attacker.example/approve">' not in body


def test_h1_still_does_not_escape_and_that_is_why_this_file_exists():
    """Pin the premise. If somebody makes ``_h1`` escape, the call sites become
    double-escaped and this file should be deleted rather than left passing for
    the wrong reason."""
    assert "<b>x</b>" in oe._h1("<b>x</b>")


def test_the_fields_that_reach_a_subject_line_are_bounded(monkeypatch, tmp_path):
    """RFC 5322 puts a hard ceiling on a header line, so an unbounded name is a
    signup that can stop its own invitations from being delivered. Capped where
    it is staged, so every consumer inherits it rather than each send site
    remembering.

    monkeypatch and a fresh store, not a bare os.environ write: this file is
    otherwise pure and would otherwise leave ASCLEPIUS_DB_PATH pointing at a
    temp directory for every test that runs after it in the same process.
    """
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(tmp_path / "cap.db"))
    monkeypatch.setenv("ENV", "test")
    from tests import _asclepius as A

    store = A.fresh_store()
    store.create_hs_signup(email="cap@example.org", full_name="N" * 500,
                           organization="O" * 500, password="x" * 24,
                           code="123456")
    row = store.get_live_hs_signup("cap@example.org")
    assert len(row["full_name"]) == 120
    assert len(row["organization"]) == 120
    # A newline still cannot survive either, which is the other half.
    store.create_hs_signup(email="nl@example.org", full_name="A\nB",
                           organization="C\r\nD", password="x" * 24, code="123456")
    row = store.get_live_hs_signup("nl@example.org")
    assert "\n" not in row["full_name"] and "\r" not in row["full_name"]
    assert "\n" not in row["organization"] and "\r" not in row["organization"]
