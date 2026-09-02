"""The verified card: minting it, revoking it, and serving it to the world.

Two private endpoints the physician drives, and two public ones nobody signs in
for. The split is the point, and so is what each side is allowed to know.

**Sharing is a URL, not an image.** An image cannot be verified: anyone can
edit a PNG and claim a checkmark. A page on our origin is self-authenticating,
revocable, and gives crawlers something to unfurl, so the share image is what
the page points AT rather than the thing being passed around (PRD D2).

**The token is a bearer credential and is stored only as a SHA-256 hash**,
following ``ingest_upload_links`` and the password-reset tokens rather than
inventing a third convention. The raw value is returned exactly once, at mint,
and never again: re-minting is how a physician who lost it gets a new one, and
that same write is what kills the old URL.

**All three failure modes return the same 404.** An unknown token, a revoked
token and a token whose account is no longer approved are indistinguishable to
the caller, deliberately. Anything else turns a public URL into an oracle:
somebody holding an old link could probe whether a named physician had been
un-approved, which is nobody's business and is exactly the kind of standing
information a person cannot correct.
"""

from __future__ import annotations

import html
import logging
import secrets
from base64 import b64encode
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response

from asclepius import auth as asc_auth
from asclepius import capabilities as asc_caps
from asclepius import card as asc_card
from asclepius import passwords as asc_passwords
from asclepius.store import get_store
from ratelimit import rate_limiter

log = logging.getLogger("asclepius.card")

router = APIRouter(prefix="/api/asclepius", tags=["asclepius-card"])

#: The same sentence for every way of not finding a card. See the module note.
_NOT_FOUND = "No card here."


def _store():
    return get_store()


# ─── The physician's own controls ─────────────────────────────────────────────


def _require_card_eligible(user: Dict[str, Any]) -> Dict[str, Any]:
    """Fresh row, then the approval gate, with one message for every refusal.

    Re-read rather than trusting the token's claims: a session minted before an
    admin changed a decision still carries the old status, and this is the one
    endpoint where acting on a stale "approved" would publish a claim we have
    since withdrawn.

    Pending, rejected, advisor and referrer accounts all land here. They get the
    same sentence, because the difference between them is not something this
    endpoint should be explaining to whoever is holding the session.
    """
    row = _store().get_user_by_id(user["id"]) or {}
    if not asc_card.is_card_eligible(row):
        raise HTTPException(
            status_code=403,
            detail=(
                "A verified card is available once your credentials have been "
                "verified."
            ),
        )
    return row


@router.post(
    "/me/card",
    dependencies=[Depends(rate_limiter("asclepius_card_mint", 10, 600))],
)
async def mint_my_card(
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE)),
):
    """Mint or re-mint the card URL. The raw token is returned once, here.

    Re-minting deliberately reuses one column rather than keeping a history:
    there is no second row to forget to revoke, so "I shared that with the
    wrong person" is one button and the old link is dead the moment it lands.
    """
    row = _require_card_eligible(user)
    raw = secrets.token_urlsafe(32)
    store = _store()
    if not store.set_card_token(row["id"], asc_passwords.hash_reset_token(raw)):
        raise HTTPException(status_code=404, detail="Account not found.")
    row = store.get_user_by_id(row["id"]) or row
    store.log_event(
        entity_type="user", entity_id=row["id"], event_type="card_minted",
        actor=row.get("email"), payload={},
    )
    return {
        "ok": True,
        # Once. It is a hash in the database from here on, so a physician who
        # loses it re-mints rather than asking us to look it up.
        "url": asc_card.card_url(raw),
        "image_url": asc_card.card_image_url(raw),
        "card": asc_card.card_payload(row),
    }


@router.delete("/me/card")
async def revoke_my_card(
    user: Dict[str, Any] = Depends(asc_auth.require_surface(asc_caps.BROWSE)),
):
    """Take the card down.

    Not gated on approval, unlike minting: somebody whose standing changed
    after they minted one must still be able to withdraw it, and a revoke can
    only ever reduce what is public. ``ok`` is True either way because the
    caller asked for a state, not for a transition, and telling them "there was
    nothing to revoke" invites a retry loop over an outcome they already have.
    """
    store = _store()
    revoked = store.revoke_card_token(user["id"])
    if revoked:
        store.log_event(
            entity_type="user", entity_id=user["id"], event_type="card_revoked",
            actor=user.get("email"), payload={},
        )
    return {"ok": True, "revoked": revoked}


# ─── The public page ──────────────────────────────────────────────────────────


def _resolve(token: str) -> Dict[str, Any]:
    """Token to a live, still-approved account, or the one 404.

    Every check collapses to the same failure on purpose (see the module note).
    The account is read live, so nothing here can be satisfied by what was true
    at mint time.
    """
    raw = (token or "").strip()
    if not raw or len(raw) > 200:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    row = _store().get_user_by_card_token_hash(asc_passwords.hash_reset_token(raw))
    if not row or not asc_card.is_card_eligible(row):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return row


def _avatar_data_uri(user: Dict[str, Any]) -> Optional[str]:
    """The headshot inlined into the page.

    The avatar route is signed-in only, and it should stay that way: opening a
    second unauthenticated door onto every physician's picture to serve one
    card would widen far more than this feature asked for. Inlining keeps the
    picture scoped to the one page whose token the visitor already holds.
    """
    data = asc_card.avatar_bytes(user)
    if not data:
        return None
    return "data:image/png;base64," + b64encode(data).decode("ascii")


def _page_html(card: Dict[str, Any], *, url: str, image_url: str,
               avatar_uri: Optional[str]) -> str:
    """The card as a page, with the OG tags a link preview reads.

    Every interpolated value is escaped. The name on this page is typed by the
    physician themselves and the page is served from our origin next to a
    portal whose session lives in localStorage, so a display name is
    attacker-controlled input in exactly the way an uploaded file is. Escaping
    at the boundary is the same discipline ``onboarding_emails`` applies to
    every value it puts in a mail body.
    """
    name = html.escape(asc_card.display_name(card))
    specialty = asc_card.display_specialty(card)
    practice = asc_card.practice_line(card)
    description = html.escape(asc_card.share_description(card))
    title = f"{name}, verified physician"
    initials = html.escape(((card.get("avatar") or {}).get("initials") or "?")[:2])
    accent = html.escape(str((card.get("avatar") or {}).get("accent") or "green"), quote=True)

    portrait = (
        f'<img class="avatar" src="{html.escape(avatar_uri, quote=True)}" alt="">'
        if avatar_uri
        else f'<div class="avatar initials accent-{accent}">{initials}</div>'
    )
    lines = "".join(
        f'<p class="line">{html.escape(text)}</p>'
        for text in (specialty, practice) if text
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<meta property="og:type" content="profile">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="{html.escape(url, quote=True)}">
<meta property="og:image" content="{html.escape(image_url, quote=True)}">
<meta property="og:image:width" content="{asc_card.IMAGE_W}">
<meta property="og:image:height" content="{asc_card.IMAGE_H}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{description}">
<meta name="twitter:image" content="{html.escape(image_url, quote=True)}">
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #eef0ef; color: #1a1b1a;
         font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                      Helvetica, Arial, sans-serif; }}
  .card {{ background: #fbfcfa; border: 1px solid rgba(26,27,26,0.08);
          border-radius: 20px; padding: 40px; max-width: 560px; width: calc(100% - 48px);
          display: flex; gap: 28px; align-items: center; }}
  .avatar {{ width: 112px; height: 112px; border-radius: 50%; flex: 0 0 112px;
            object-fit: cover; }}
  .initials {{ display: flex; align-items: center; justify-content: center;
              font-size: 38px; font-weight: 600; color: #fbfcfa; }}
  .accent-green {{ background: #4ca63c; }}
  .accent-orange {{ background: #ec9440; }}
  .accent-pink {{ background: #e8447b; }}
  .accent-lime {{ background: #aab428; }}
  h1 {{ font-size: 28px; margin: 0 0 8px; display: flex; align-items: center; gap: 8px; }}
  .check {{ width: 22px; height: 22px; border-radius: 50%; background: #4ca63c;
           color: #fbfcfa; font-size: 13px; line-height: 22px; text-align: center;
           flex: 0 0 22px; }}
  .line {{ margin: 0 0 4px; color: #5c5e5a; font-size: 16px; }}
  .stamp {{ margin: 14px 0 0; color: #3c7a31; font-size: 13px;
           font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; }}
</style>
</head>
<body>
<main class="card">
  {portrait}
  <div>
    <h1>{name}<span class="check" aria-label="Verified">&#10003;</span></h1>
    {lines}
    <p class="stamp">Verified on Archangel Health</p>
  </div>
</main>
</body>
</html>
"""


@router.get(
    "/card/{token}",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limiter("asclepius_card_page", 60, 60))],
)
async def public_card_page(token: str):
    """The card, server-rendered, for anyone holding the link.

    Server-rendered rather than a client-side fetch because the audience
    includes crawlers: a preview unfurled by Slack or LinkedIn never runs the
    JavaScript, and a shared link that unfurls to nothing is the same as not
    having built the feature. Rate-limited like the other public doors, since
    this one takes a token from a stranger and hits the database with it.
    """
    row = _resolve(token)
    card = asc_card.card_payload(row)
    body = _page_html(
        card,
        url=asc_card.card_url(token),
        image_url=asc_card.card_image_url(token),
        avatar_uri=_avatar_data_uri(row),
    )
    return HTMLResponse(
        content=body,
        headers={
            # Short and public: crawlers and colleagues may cache it, but a
            # revoke should stop being papered over within minutes, not hours.
            "Cache-Control": "public, max-age=300",
            "X-Content-Type-Options": "nosniff",
            # The page is self-contained (inline CSS, inlined avatar), so it
            # needs nothing from anywhere and may be told so.
            "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'",
        },
    )


@router.get(
    "/card/{token}/image",
    dependencies=[Depends(rate_limiter("asclepius_card_image", 60, 60))],
)
async def public_card_image(token: str):
    """The share image, rendered from the same payload the page renders.

    Same 404 rules as the page, for the same reason: an image endpoint that
    answered when the page did not would leak precisely the standing the page
    is careful not to.
    """
    row = _resolve(token)
    card = asc_card.card_payload(row)
    try:
        png = asc_card.render_card_png(card, asc_card.avatar_bytes(row))
    except Exception:
        # A preview that fails to unfurl is a worse outcome than a 503, but a
        # 500 with a stack trace on a public URL is worse than both.
        log.exception("[card] could not render the share image")
        raise HTTPException(status_code=503, detail="The card image is unavailable right now.")
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
