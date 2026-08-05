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


async def post_system_message(
    *,
    channel_slug: str,
    body: str,
    kind: str,
    mention_user_ids: Optional[List[str]] = None,
    notify: bool = False,
) -> Optional[Dict[str, Any]]:
    """Post as the system author into an ACTIVE channel. Returns the
    serialized message, or ``None`` when skipped (unknown/inactive channel,
    PHI finding, empty body).

    Notification policy: when ``notify`` is set, ONLY the mentioned users get
    a digest entry (the welcome ping). A system post never triggers the
    all-member announcement blast — digest posts rely on the unread badge, and
    the existing blast fires solely for #task-announcements, which system
    posts do not target.
    """
    text = (body or "").strip()
    if not text:
        return None

    cstore = get_community_store()
    channel = cstore.get_channel_by_slug(channel_slug)
    if not channel or not channel.get("is_active", 1):
        log.error("[system-post] channel %r missing or inactive — post skipped", channel_slug)
        return None

    # Scan a URL-MASKED copy: article links legitimately carry long digit runs
    # (PMIDs, DOIs) that pattern-match MRN/account-number rules, and a URL is
    # structural metadata, not patient text. Every human-visible character —
    # titles, one-liners, headers — is still scanned verbatim.
    findings = phi_gate.scan_text(_mask_urls(text))
    if findings:
        # Categories only — never the text (§7). Nothing persisted.
        cstore.record_block_event(
            user_id=SYSTEM_USER_ID, surface="system_post",
            categories=phi_gate.categories_of(findings),
        )
        audit_log.record(
            actor_type="system", actor_id=SYSTEM_USER_ID,
            action="community.phi_block", outcome="blocked",
            resource_type="community", resource=channel["slug"],
            detail={"surface": "system_post", "kind": kind,
                    "categories": phi_gate.categories_of(findings)},
        )
        log.error("[system-post] PHI gate blocked a %s post to #%s — skipped (fail-closed)",
                  kind, channel_slug)
        return None

    # Late import — the router imports SYSTEM_MEMBER from this module.
    from community.router import _channel_broadcast, _serialize_messages, member_map  # noqa: PLC0415

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

    serialized = _serialize_messages([msg], members, channel["slug"])[0]
    await _channel_broadcast({"type": "message.created", "message": serialized}, channel)
    return serialized
