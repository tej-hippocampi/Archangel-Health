"""The shareable verified card: one serializer, one renderer.

A physician who has been through verification gets a card their colleagues
recognise: a checkmark, their picture, their name, their specialty, how long
they have been in practice, and where they practise. They can share it outward
the way anyone shares a verified profile.

Three surfaces show that card, and this module is the only place that decides
what is on it (PRD D3): the public page, the share image that page unfurls to,
and the member block a colleague sees in the community. They render the same
dict, so they cannot drift into showing different facts about the same person.

**The field list is a whitelist built by hand, not a blob with the dangerous
keys filtered out.** Filtering fails open: the day somebody adds a key to
``credentials_json`` it appears on a public URL unless a denylist was updated
in the same breath. Building the payload key by key fails closed instead, so a
new field is invisible here until a person decides it should be visible. What
must never appear is not a matter of taste:

  * The contributor score, its band and its components are an internal
    instrument for routing and pay. A physician is never shown their own, so a
    stranger on the internet certainly is not.
  * Email, NPI and registration numbers are identifiers. On a public,
    crawlable URL they invite scraping and impersonation, and the card asserts
    identity precisely so that nobody needs them.
  * Everything in ``tiering.FORBIDDEN_CREDENTIAL_KEYS`` - medical school,
    graduation year, date of birth, sex, IMG status. Those are not collected,
    not scored and not displayed, because a pedigree field on a physician's
    profile becomes a pedigree field in somebody's judgment of them. That is a
    standing legal and fairness position, not an oversight to be tidied up.

**Nothing about the physician is baked into the token.** The page reads the
account live on every load, so revoking the card or un-approving the account
kills it rather than leaving a stale claim of verification in circulation.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("asclepius.card")

#: Exactly what a card carries (PRD D1), and the assertion a test can hold.
#: Anything that is not one of these seven keys does not belong on a card.
CARD_FIELDS: Tuple[str, ...] = (
    "full_name",
    "specialty",
    "years_in_practice",
    "country",
    "avatar",
    "verified",
    "minted_at",
)

#: The account kinds that are not physicians (``capabilities.ADVISOR`` /
#: ``REFERRER``). Named here rather than imported so this module stays a leaf:
#: it is imported by the community plane, and a cycle through capabilities is
#: not worth two string constants.
_NON_PHYSICIAN_KINDS = frozenset({"advisor", "referrer"})

#: Roles that describe a clinician. An admin account is a person doing a job
#: here, not a verified physician, and the checkmark is a claim about the
#: latter.
_PHYSICIAN_ROLES = frozenset({"evaluator", "qa_reviewer"})


def is_card_eligible(user: Optional[Dict[str, Any]]) -> bool:
    """Whether this account may hold a card at all.

    The checkmark says "we verified this person is a practising physician". An
    account that has not cleared verification minting one would make the
    product lie on our own domain, which is worse than the feature being
    missing. So: approved, a physician, and still active. ``verification_status
    is NULL`` (a pre-verification-era row) is deliberately NOT enough here even
    though ``capabilities.access_level`` treats it as full access - full access
    is about what somebody may do, and this is about what we are willing to
    assert on their behalf in public.
    """
    u = user or {}
    if not u:
        return False
    if u.get("active") is not None and not u.get("active"):
        return False
    if (u.get("verification_status") or "").strip().lower() != "approved":
        return False
    if ((u.get("account_kind") or "").strip().lower()) in _NON_PHYSICIAN_KINDS:
        return False
    return (u.get("role") or "").strip().lower() in _PHYSICIAN_ROLES


def _creds(user: Dict[str, Any]) -> Dict[str, Any]:
    """The credential blob, or an empty dict. A malformed blob is not a 500."""
    import json  # noqa: PLC0415 - only needed on this path

    try:
        parsed = json.loads(user.get("credentials_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _country_display(code: Optional[str]) -> Optional[str]:
    """"SA" means nothing to a colleague reading the card; "Saudi Arabia" does.

    Falls back to the raw code for a country we have no registry entry for,
    which is still better than an empty line, and to None when we were told
    nothing at all.
    """
    raw = (code or "").strip().upper()
    if not raw:
        return None
    from asclepius.registry import config as registry_config  # noqa: PLC0415

    name = (registry_config.for_country(raw).country_name or "").strip()
    return name or raw


def _years_in_practice(user: Dict[str, Any], creds: Dict[str, Any]) -> Optional[int]:
    """Years in ACTIVE practice, which is the number the physician typed.

    ``users.years_experience`` is the older, coarser column and stands in only
    when the onboarding answer is absent, so an account that predates the
    richer form still shows something true.
    """
    for candidate in (creds.get("yearsInActivePractice"), user.get("years_experience")):
        if candidate in (None, ""):
            continue
        try:
            years = int(candidate)
        except (TypeError, ValueError):
            continue
        # A negative or absurd value is a typo somewhere upstream, and a card is
        # the wrong place to display one back at the world.
        if 0 <= years <= 80:
            return years
    return None


def _avatar_reference(user: Dict[str, Any]) -> Dict[str, Any]:
    """Picture if there is one, initials and an accent colour if there is not.

    The initials and the accent come from the community's own helpers rather
    than a second implementation, for the same reason the profile's avatar
    block does: the two letters a physician sees beside their messages and the
    two on their card have to be the same two letters.

    ``url`` is the SIGNED-IN avatar route, which is what the community member
    block wants. The public page cannot use it and does not: it inlines the
    bytes instead (see ``avatar_bytes``), so an anonymous visitor never needs a
    session and we never open a second, unauthenticated door onto avatars.
    """
    from community.router import _initials, specialty_accent  # noqa: PLC0415

    sha = (user.get("avatar_asset_sha") or "").strip()
    return {
        "url": (f"/api/asclepius/users/{user.get('id')}/avatar?v={sha[:12]}") if sha else None,
        "initials": _initials(user.get("full_name") or user.get("email") or ""),
        "accent": specialty_accent(user.get("specialty")),
    }


def card_payload(user: Dict[str, Any]) -> Dict[str, Any]:
    """The card, as every surface sees it. Keys are exactly ``CARD_FIELDS``.

    Built one key at a time from named columns. Nothing is copied wholesale
    from the user row or the credential blob, so there is no path by which a
    score, an identifier or a pedigree field reaches a caller of this function.
    """
    creds = _creds(user)
    specialty = (creds.get("primarySpecialty") or user.get("specialty") or "").strip()
    return {
        "full_name": (user.get("full_name") or "").strip() or None,
        "specialty": specialty or None,
        "years_in_practice": _years_in_practice(user, creds),
        "country": _country_display(user.get("country_of_practice")),
        "avatar": _avatar_reference(user),
        # Always True on a card that exists: it is only reachable through the
        # eligibility gate above. Emitted anyway so a surface renders the
        # checkmark from data rather than from an assumption it makes locally.
        "verified": is_card_eligible(user),
        "minted_at": user.get("card_minted_at"),
    }


def display_name(card: Dict[str, Any]) -> str:
    """What to print where the name goes.

    A physician who never filled in a display name still gets a card that reads
    as a card rather than a blank line with a checkmark next to it.
    """
    return (card.get("full_name") or "").strip() or "Verified physician"


def display_specialty(card: Dict[str, Any]) -> Optional[str]:
    """Specialties are stored lower-case ("nephrology"); a card is a headline."""
    value = (card.get("specialty") or "").strip()
    if not value:
        return None
    return value if any(ch.isupper() for ch in value) else value.title()


def practice_line(card: Dict[str, Any]) -> Optional[str]:
    """Years and country as one line, with whichever half we actually have.

    Absent stays absent. "0 years in practice, " reads as a data error, and a
    physician sharing this is putting their professional face on it.
    """
    years = card.get("years_in_practice")
    country = (card.get("country") or "").strip()
    parts = []
    if years is not None:
        parts.append(f"{years} year in practice" if years == 1 else f"{years} years in practice")
    if country:
        parts.append(country)
    return " · ".join(parts) if parts else None


def share_description(card: Dict[str, Any]) -> str:
    """The one sentence a link preview shows under the title."""
    tail = practice_line(card)
    specialty = display_specialty(card)
    bits = [b for b in (specialty, tail) if b]
    lead = "Verified physician on Archangel Health"
    return f"{lead}. {' · '.join(bits)}." if bits else f"{lead}."


def avatar_bytes(user: Dict[str, Any]) -> Optional[bytes]:
    """The physician's picture as raw bytes, or None.

    Best effort on purpose. The asset store is content-addressed and can lose a
    blob to an ephemeral filesystem on redeploy; when that happens the card
    falls back to initials, which is cosmetic. Failing the whole public page
    because a headshot went missing would not be.
    """
    sha = (user.get("avatar_asset_sha") or "").strip()
    if not sha:
        return None
    try:
        from asclepius import assets as asc_assets  # noqa: PLC0415

        data, _mime = asc_assets.load_asset(sha)
        return data
    except Exception:
        log.info("[card] avatar blob unavailable for %s", str(user.get("id"))[:12])
        return None


# ─── URLs ─────────────────────────────────────────────────────────────────────
# The card lives on OUR domain, not on the landing site: the page is served by
# this backend and the whole point of a URL over a bare image is that the origin
# is the thing doing the verifying. Same reasoning as passwords.signin_url.


def _base_url() -> str:
    return (os.getenv("BASE_URL") or "http://localhost:8000").strip().rstrip("/")


def card_url(raw_token: str) -> str:
    return f"{_base_url()}/api/asclepius/card/{raw_token}"


def card_image_url(raw_token: str) -> str:
    return f"{card_url(raw_token)}/image"


# ─── The share image ──────────────────────────────────────────────────────────

#: Open Graph's canonical size. Facebook, LinkedIn, Slack and iMessage all crop
#: toward 1.91:1, so anything else gets somebody's face cut off in the preview.
IMAGE_W, IMAGE_H = 1200, 630

# The email palette, because a card in a link preview sits next to our mail in
# the same inbox and should look like it came from the same place.
_PAPER = (251, 252, 250)
_CANVAS = (238, 240, 239)
_INK = (26, 27, 26)
_INK_SOFT = (92, 94, 90)
_HAIRLINE = (222, 225, 221)
_ACCENT_RGB = {
    "green": (76, 166, 60),
    "orange": (236, 148, 64),
    "pink": (232, 68, 123),
    "lime": (170, 180, 40),
}


def _font(size: int, *, bold: bool = False):
    """A real face if the box has one, Pillow's bundled font otherwise.

    Rendering must not depend on a font being installed: a container without
    DejaVu would otherwise turn a physician's card into a stack trace. Pillow's
    default is plain, and plain beats absent.
    """
    from PIL import ImageFont  # noqa: PLC0415

    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _circular_avatar(data: bytes, diameter: int):
    """The headshot, squared, resized and masked to a circle. None on failure."""
    try:
        from PIL import Image, ImageDraw  # noqa: PLC0415

        im = Image.open(io.BytesIO(data))
        im.load()
        im = im.convert("RGB")
        w, h = im.size
        if w != h:
            side = min(w, h)
            left = (w - side) // 2
            # Bias upward for the same reason avatar.py does: in a portrait the
            # face sits above the centre and a true centre crop beheads people.
            top = (h - side) // 4 if h > w else (h - side) // 2
            im = im.crop((left, top, left + side, top + side))
        im = im.resize((diameter, diameter))
        mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
        im.putalpha(mask)
        return im
    except Exception:
        log.info("[card] could not render the avatar; falling back to initials")
        return None


def render_card_png(card: Dict[str, Any], avatar_png: Optional[bytes] = None) -> bytes:
    """Draw the card and return PNG bytes.

    Takes the SAME dict the page renders, so the image cannot claim anything
    the page does not. It reads only ``CARD_FIELDS``, which is why there is no
    separate audit needed for what the image leaks: there is nothing else in
    its input to leak. Note that a link preview, once fetched, is cached by
    third parties we do not control, so revoking a card stops the page but not
    a copy of this image. That is tolerable only because of what is on it.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    im = Image.new("RGB", (IMAGE_W, IMAGE_H), _CANVAS)
    draw = ImageDraw.Draw(im)

    pad = 56
    draw.rounded_rectangle(
        (pad, pad, IMAGE_W - pad, IMAGE_H - pad),
        radius=32, fill=_PAPER, outline=_HAIRLINE, width=2,
    )

    accent = _ACCENT_RGB.get((card.get("avatar") or {}).get("accent") or "", _ACCENT_RGB["green"])
    left = pad + 72
    centre_y = IMAGE_H // 2 - 20
    diameter = 200
    avatar_box = (left, centre_y - diameter // 2, left + diameter, centre_y + diameter // 2)

    portrait = _circular_avatar(avatar_png, diameter) if avatar_png else None
    if portrait is not None:
        im.paste(portrait, (avatar_box[0], avatar_box[1]), portrait)
    else:
        draw.ellipse(avatar_box, fill=accent)
        initials = ((card.get("avatar") or {}).get("initials") or "?")[:2]
        draw.text(
            (left + diameter // 2, centre_y), initials,
            font=_font(76, bold=True), fill=_PAPER, anchor="mm",
        )

    text_x = left + diameter + 56
    name_font = _font(58, bold=True)
    name = display_name(card)
    draw.text((text_x, centre_y - 76), name, font=name_font, fill=_INK)

    # The checkmark rides beside the name rather than floating in a corner, so
    # what it certifies is unambiguous: this person, not this page.
    name_w = draw.textlength(name, font=name_font)
    badge_r = 22
    badge_cx = int(text_x + name_w + 20 + badge_r)
    badge_cy = centre_y - 76 + 29
    if card.get("verified"):
        draw.ellipse(
            (badge_cx - badge_r, badge_cy - badge_r, badge_cx + badge_r, badge_cy + badge_r),
            fill=_ACCENT_RGB["green"],
        )
        draw.line(
            [(badge_cx - 10, badge_cy + 1), (badge_cx - 3, badge_cy + 8), (badge_cx + 10, badge_cy - 8)],
            fill=_PAPER, width=5, joint="curve",
        )

    line_y = centre_y - 4
    specialty = display_specialty(card)
    if specialty:
        draw.text((text_x, line_y), specialty, font=_font(38), fill=_INK_SOFT)
        line_y += 56
    practice = practice_line(card)
    if practice:
        draw.text((text_x, line_y), practice, font=_font(32), fill=_INK_SOFT)

    draw.text(
        (text_x, IMAGE_H - pad - 76), "Verified on Archangel Health",
        font=_font(26, bold=True), fill=_ACCENT_RGB["green"],
    )

    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
