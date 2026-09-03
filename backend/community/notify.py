"""Community email digests (PRD §4): @mentions + #task-announcements posts.

Notifications are QUEUED durably (``community_notifications``) at write time
and flushed as one digest email per user by a background loop (interval
``COMMUNITY_DIGEST_INTERVAL_SEC``, default 300s) via the shared
``email_utils`` transport. A digest snippet is read from the live message row
at flush time — so an edited message digests its current text and a deleted
message digests nothing. Message bodies have already passed the §7 PHI gate,
and the snippet is capped anyway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import realm as _realm
from typing import Any, Dict, List, Optional

from community.store import CommunityStore, get_community_store

log = logging.getLogger("community.notify")

_KIND_LABELS = {
    "mention": "mentioned you",
    "announcement": "posted in #task-announcements",
    "broadcast": "sent a broadcast (@channel)",
    "dm": "sent you a direct message",
}
_SNIPPET_LEN = 140

# Mirrors community.router.BROADCAST_MENTION — a sentinel mention that expands
# to every member at notify time (the @channel broadcast).
_BROADCAST_MENTION = "*channel*"


def digest_interval_sec() -> int:
    try:
        return max(15, int(os.getenv("COMMUNITY_DIGEST_INTERVAL_SEC", "300")))
    except (TypeError, ValueError):
        return 300


def queue_for_message(
    cstore: CommunityStore,
    *,
    message: Dict[str, Any],
    channel: Dict[str, Any],
    member_ids: List[str],
) -> None:
    """Enqueue digest notifications for one just-persisted message: every
    mentioned member, plus (for a top-level #task-announcements post) every
    member. Never the author; each user at most once per message."""
    author = message["author_user_id"]
    queued: set = set()
    mentions = list(message.get("mentions") or [])
    # @channel broadcast: the sentinel fans out to every member (once each).
    if _BROADCAST_MENTION in mentions:
        for uid in member_ids:
            if uid != author and uid not in queued:
                cstore.enqueue_notification(user_id=uid, kind="broadcast", message_id=message["id"])
                queued.add(uid)
    for uid in mentions:
        if uid == _BROADCAST_MENTION:
            continue
        if uid != author and uid in member_ids and uid not in queued:
            cstore.enqueue_notification(user_id=uid, kind="mention", message_id=message["id"])
            queued.add(uid)
    if channel.get("slug") == "task-announcements" and not message.get("parent_message_id"):
        for uid in member_ids:
            if uid != author and uid not in queued:
                cstore.enqueue_notification(user_id=uid, kind="announcement", message_id=message["id"])
                queued.add(uid)


def enqueue_dm(cstore: CommunityStore, *, recipient_id: str, message: Dict[str, Any]) -> None:
    """Queue a digest entry for a received direct message. The digest email
    goes only to the conversation's other participant — the snippet is their
    own message to read, and the body has already passed the §7 PHI gate."""
    if recipient_id != message["author_user_id"]:
        cstore.enqueue_notification(user_id=recipient_id, kind="dm", message_id=message["id"])


def _snippet(body: str) -> str:
    text = " ".join((body or "").split())
    if len(text) > _SNIPPET_LEN:
        text = text[: _SNIPPET_LEN - 1].rstrip() + "…"
    return text


async def flush_pending(
    cstore: Optional[CommunityStore] = None,
    *,
    resolve_member: Any = None,
) -> int:
    """Send one digest email per user with unsent notifications; mark them
    sent. ``resolve_member(user_id) -> {email, display_name} | None`` is
    injected by the router (it owns the member map). Returns emails sent."""
    from email_utils import send_html_email  # local import — optional transport

    cstore = cstore or get_community_store()
    pending = cstore.unsent_notifications()
    if not pending:
        return 0

    by_user: Dict[str, List[Dict[str, Any]]] = {}
    for n in pending:
        by_user.setdefault(n["user_id"], []).append(n)

    sent_count = 0
    for user_id, items in by_user.items():
        member = resolve_member(user_id) if resolve_member else None
        if not member or not member.get("email"):
            # No longer a member (deactivated, role change): drop silently but
            # mark handled so the queue can't grow unboundedly.
            cstore.mark_notifications_sent([n["id"] for n in items])
            continue
        if not cstore.wants_activity_email(user_id):
            # Opted out. Mark handled rather than leaving the rows pending, or
            # the queue grows forever and every later flush re-reads them. The
            # in-app notification is unaffected; only the email stops.
            cstore.mark_notifications_sent([n["id"] for n in items])
            continue
        rows: List[tuple] = []
        handled_ids: List[int] = []
        for n in items:
            handled_ids.append(n["id"])
            msg = cstore.get_message(n["message_id"])
            if not msg or msg.get("deleted"):
                continue
            actor = resolve_member(msg["author_user_id"]) if resolve_member else None
            actor_name = (actor or {}).get("display_name") or "A colleague"
            label = _KIND_LABELS.get(n["kind"], "posted")
            # Plain-text (lead, detail) pairs; the email builder owns escaping
            # and layout, so no HTML is composed here.
            rows.append((f"{actor_name} {label}", _snippet(msg.get("body") or "")))
        if rows:
            from onboarding_emails import build_community_digest_email  # noqa: PLC0415

            from community import links  # noqa: PLC0415 — one URL definition

            body = build_community_digest_email(
                activity_items=rows,
                community_url=links.community_url(),
                unsubscribe_url=links.unsubscribe_url(
                    cstore.email_prefs(user_id).get("unsubscribe_token") or ""
                ),
            )
            ok = await send_html_email(
                member["email"],
                "New activity in your Asclepius community",
                body,
            )
            if ok:
                sent_count += 1
            else:
                log.warning("community digest email failed for one recipient (will not retry)")
        cstore.mark_notifications_sent(handled_ids)
    return sent_count


_loop_task: Optional[asyncio.Task] = None


def start_digest_loop(*, resolve_member: Any) -> None:
    """Start (once) the background digest flusher. Called from app startup."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return

    async def _run() -> None:
        while True:
            await asyncio.sleep(digest_interval_sec())
            # Sandbox PRD §1.3 / §1.4: one flush per realm. In the sandbox the
            # digest lands in the sandbox outbox, never in a real inbox.
            for r in _realm.active_realms():
                try:
                    with _realm.scoped(r):
                        await flush_pending(resolve_member=resolve_member)
                except Exception:  # pragma: no cover — the loop must survive
                    log.warning("community digest flush failed (%s)", r, exc_info=True)

    _loop_task = asyncio.get_running_loop().create_task(_run())


def stop_digest_loop() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
