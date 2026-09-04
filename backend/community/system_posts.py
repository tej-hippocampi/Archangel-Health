"""System ("Archangel bot") posts — the single trusted internal write path.

Community v2: welcomes (#introductions) and content digests (#medical-ai-news)
are authored by a VIRTUAL system author, not a users row — a real account
would leak into the member directory, the verification queue, and exports on
the asclepius plane. ``insert_message`` takes any author id string; the
router's serializer special-cases ``SYSTEM_USER_ID`` so the bot renders with
a distinct badge. The system author can never log in, be DM'd, or appear in
``member_map``.

Trust model: this function is reachable only from in-process code (approval
hooks, the digest pipeline, the internal trigger endpoint) — never from a
member-facing route — so ``post_policy`` does not apply. The §7 PHI gate DOES
still run: external feed titles/summaries are untrusted text, and a blocked
system post is silently skipped (there is no user to bounce a 422 to),
logged, and never persisted. Fail-closed, like everything else here.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from audit import audit_log
from community import phi_gate
from community.store import get_community_store

log = logging.getLogger("community.system_posts")

# Real user ids are ``u-<uuid hex>``; this fixed id can never collide.
SYSTEM_USER_ID = "u-system"

# Serialized exactly like a member profile (Tier A keys only + is_bot).
SYSTEM_MEMBER: Dict[str, Any] = {
    "user_id": SYSTEM_USER_ID,
    "display_name": "Archangel",
    "initials": "AH",
    "specialty": None,
    "specialty_accent": "green",
    "years_in_practice": None,
    "institution": "Archangel Health",
    "board_certified": False,
    "fellowship_trained": False,
    "verified": True,
    "is_admin": False,
    "is_staff": True,
    "is_bot": True,
    "blurb": "Automated posts from the Archangel Health platform.",
}


_URL_RE = re.compile(r"https?://[^\s)\"'<>]+", re.I)


def _mask_urls(text: str) -> str:
    """Replace every http(s) URL with a digit-free placeholder before the PHI
    scan. Length-preserving is NOT needed — a blocked system post is skipped
    wholesale, spans are never surfaced."""
    return _URL_RE.sub("masked://link", text or "")


def _resolve_channel(channel_slug: str) -> Optional[Dict[str, Any]]:
    """The active channel behind a slug, or None with a loud log.

    Deliberately NOT the visibility-gated lookup the member routes use: the bot
    posts into the staff room, and it posts the first message into a
    below-threshold room by design. Shared by the message and poll paths so
    there is one answer to "where is the bot allowed to write".
    """
    channel = get_community_store().get_channel_by_slug(channel_slug)
    if not channel or not channel.get("is_active", 1):
        log.error("[system-post] channel %r missing or inactive, post skipped", channel_slug)
        return None
    return channel


def channel_member_ids(
    channel: Dict[str, Any], members: Dict[str, Any]
) -> List[str]:
    """Who a post in this channel may be mailed about.

    Channels have no roster: every gated member is in every visible room, which
    is what makes "the members of this channel" the whole member map. The one
    exception is a ``staff_only`` room, whose contents must not reach a
    physician by email any more than they reach one over the socket or the REST
    list. Shared by the message and poll paths so there is one answer.
    """
    if channel.get("staff_only"):
        return [uid for uid, m in members.items() if (m or {}).get("is_staff")]
    return list(members.keys())


def _queue_channel_notifications(
    channel: Dict[str, Any], message: Dict[str, Any], members: Dict[str, Any]
) -> None:
    """Fan a bot post out to the digest queue.

    ``notify.queue_for_message`` decides the kind from the channel: a top-level
    #task-announcements post is an ``announcement`` (the fan-out rule that
    predates this), anything else is a ``post``. Passing both means the caller
    says "this deserves email" and the notify layer keeps owning what that
    means.
    """
    from community import notify as cnotify  # noqa: PLC0415 - avoid an import cycle

    cnotify.queue_for_message(
        get_community_store(), message=message, channel=channel,
        member_ids=channel_member_ids(channel, members), notify_post=True,
    )


def _phi_clear(channel: Dict[str, Any], kind: str, text: str,
               *, exempt: tuple = ()) -> bool:
    """Run the §7 gate over everything human-visible in a bot post.

    Returns False when the post must be dropped, having recorded the block
    (categories only, never the text) and audited it. Factored out of
    ``post_system_message`` when the poll path arrived: a second bot-writing
    function that scanned its own text slightly differently is exactly how a
    gate develops a hole.
    """
    findings = phi_gate.scan_text(_mask_urls(text), exempt_categories=exempt)
    if not findings:
        return True
    cstore = get_community_store()
    # Categories only: never the text (§7). Nothing persisted.
    cstore.record_block_event(
        user_id=SYSTEM_USER_ID, surface="system_post",
        categories=phi_gate.categories_of(findings),
    )
    audit_log.record(
        actor_type="system", actor_id=SYSTEM_USER_ID,
        action="community.phi_block", outcome="blocked",
        resource_type="community", resource=channel["slug"],
        detail={"surface": "system_post", "kind": kind,
                "categories": phi_gate.categories_of(findings),
                "exempted": list(exempt)},
    )
    log.error("[system-post] PHI gate blocked a %s post to #%s, skipped (fail-closed)",
              kind, channel["slug"])
    return False


async def post_system_poll(
    *,
    channel_slug: str,
    body: str,
    question: str,
    options: List[str],
    kind: str = "poll",
    cards: Optional[List[Dict[str, Any]]] = None,
    announce: bool = False,
) -> Optional[Dict[str, Any]]:
    """Post a poll AS THE BOT, and the only place that is allowed to.

    ``POST /community/polls`` cannot do this and should not learn how: it
    requires a member and deliberately authors every poll as its creator, so a
    member poll reads as "Dr. X asked". Loosening that route to let the bot
    through would put a system-authorship branch on a member-facing endpoint.
    Instead the weekly discussion prompt comes here and drives the same store
    primitives directly, so voting, results and the ``poll.updated`` broadcast
    are byte-identical to a member's poll and only the author differs.

    Two options minimum is enforced by the caller, not here: a one-option poll
    is a composition failure with a prose fallback, not something to silently
    repair at the write layer.
    """
    text = (body or "").strip()
    q = (question or "").strip()
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if not text or not q or len(opts) < 2:
        return None

    channel = _resolve_channel(channel_slug)
    if not channel:
        return None
    # The question and the options are human-visible text assembled from
    # somewhere else's page, so they are scanned exactly like the body.
    if not _phi_clear(channel, kind, "\n".join([text, q] + opts)):
        return None

    from community.router import (  # noqa: PLC0415 - the router imports this module
        _serialize_messages, broadcast_channel_event, member_map,
    )

    cstore = get_community_store()
    poll = cstore.create_poll(channel_id=channel["id"], question=q,
                              options=opts, created_by=SYSTEM_USER_ID)
    msg = cstore.insert_message(
        channel_id=channel["id"], author_user_id=SYSTEM_USER_ID, body=text,
        kind=kind, cards=cards or None,
    )
    # Linked BEFORE serialization: the serializer attaches the poll payload by
    # looking the link up, so a message serialized first would broadcast a
    # poll-kind message with no poll in it and every client would render an
    # empty card.
    cstore.link_poll_message(poll["id"], msg["id"])
    if announce:
        # After the link, so the member reading the digest and opening the
        # channel finds a poll rather than an empty card.
        _queue_channel_notifications(channel, msg, member_map())
    audit_log.record(
        actor_type="system", actor_id=SYSTEM_USER_ID,
        action="community.system_poll", outcome="ok",
        resource_type="community", resource=str(msg["id"]),
        detail={"channel": channel["slug"], "kind": kind,
                "message_id": msg["id"], "poll_id": poll["id"],
                "options": len(opts)},
    )
    serialized = _serialize_messages([msg], member_map(), channel["slug"])[0]
    await broadcast_channel_event(
        {"type": "message.created", "message": serialized}, channel)
    return serialized


async def post_system_message(
    *,
    channel_slug: str,
    body: str,
    kind: str,
    mention_user_ids: Optional[List[str]] = None,
    notify: bool = False,
    announce: bool = False,
    cards: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Post as the system author into an ACTIVE channel. Returns the
    serialized message, or ``None`` when skipped (unknown/inactive channel,
    PHI finding, empty body).

    Notification policy: when ``notify`` is set, ONLY the mentioned users get
    a digest entry (the welcome ping). Digest posts otherwise rely on the
    unread badge.

    ``cards`` are link cards rendered under the body: title, url, domain, a
    one-line summary and an optional discussion prompt. Their human-visible
    text is scanned exactly like the body -- an external page's title is
    untrusted text and this is where it enters the product.

    ``announce`` opts one call into the email fan-out in
    ``notify.queue_for_message``. It began as the #task-announcements rule
    (task announcements are authored by the bot, because an announcement signed
    by whichever admin happened to upload renders as "Former member" once that
    account is gone, and losing their email fan-out in the move would have been
    a silent regression). It now also carries the ``post`` kind everywhere
    else, which is how the daily content run produces mail at all: every bot
    post was written with ``announce=False``, so a routine that filled six
    channels every morning notified precisely nobody.
    """
    text = (body or "").strip()
    if not text:
        return None

    cstore = get_community_store()
    channel = _resolve_channel(channel_slug)
    if not channel:
        return None

    # Scan a URL-MASKED copy: article links legitimately carry long digit runs
    # (PMIDs, DOIs) that pattern-match MRN/account-number rules, and a URL is
    # structural metadata, not patient text. Every human-visible character —
    # titles, one-liners, headers — is still scanned verbatim.
    # An events listing has to carry the date of the event, and "March 14"
    # trips exact_date -- the right rule for a message that might be about a
    # patient, the wrong one for a conference assembled from public web pages.
    # Narrow on purpose: one category, two kinds, and it is recorded below.
    exempt: tuple = ()
    if kind in ("morning_events", "morning_brief"):
        exempt = ("exact_date",)

    # Card text is human-visible and comes from somewhere else's page, so it
    # is scanned with the body rather than trusted because a bot assembled it.
    card_text = ""
    if cards:
        card_text = "\n".join(
            " ".join(str(c.get(k) or "") for k in ("title", "description", "meta", "prompt"))
            for c in cards
        )
    if not _phi_clear(channel, kind,
                      text + ("\n" + card_text if card_text else ""), exempt=exempt):
        return None

    # Late import — the router imports SYSTEM_MEMBER from this module.
    from community.router import (  # noqa: PLC0415
        _serialize_messages, broadcast_channel_event, member_map,
    )

    members = member_map()
    mentions = [uid for uid in (mention_user_ids or []) if uid in members]

    msg = cstore.insert_message(
        channel_id=channel["id"],
        author_user_id=SYSTEM_USER_ID,
        body=text,
        parent_message_id=None,
        mentions=mentions,
        attachments=[],
        kind=kind,
        cards=cards or None,
    )
    audit_log.record(
        actor_type="system", actor_id=SYSTEM_USER_ID,
        action="community.system_post", outcome="ok",
        resource_type="community", resource=str(msg["id"]),
        detail={"channel": channel["slug"], "kind": kind,
                "message_id": msg["id"], "mentions": len(mentions)},
    )

    if notify and mentions:
        for uid in mentions:
            cstore.enqueue_notification(user_id=uid, kind="mention", message_id=msg["id"])

    if announce:
        _queue_channel_notifications(channel, msg, members)

    serialized = _serialize_messages([msg], members, channel["slug"])[0]
    await broadcast_channel_event(
        {"type": "message.created", "message": serialized}, channel)
    return serialized
