"""Community email digests (PRD §4): mentions, DMs, announcements, posts, pins.

Notifications are QUEUED durably (``community_notifications``) at write time
and flushed as one digest email per user by a background loop (interval
``COMMUNITY_DIGEST_INTERVAL_SEC``, default 300s) via the shared
``email_utils`` transport. A digest snippet is read from the live message row
at flush time — so an edited message digests its current text and a deleted
message digests nothing. Message bodies have already passed the §7 PHI gate,
and the snippet is capped anyway.

Two kinds arrived once the daily content routine was switched on, because
without them it produced no email at all. ``post`` is a new top-level post in a
channel, and it is opt-in per write (see ``queue_for_message`` for exactly what
opts in and why an ordinary human message does not). ``pin`` is somebody
marking a message as the one to read, which until now was a WebSocket event and
therefore invisible to everyone not connected in that second.

Each kind answers to its own preference switch (``_KIND_STREAM``), so a member
can stop hearing about pins without also silencing the mention that tells them
a colleague asked them something. A failed send is retried rather than marked
delivered, which it used to be: one transient vendor error ate a member's mail
and left a queue that looked perfectly healthy.
"""

from __future__ import annotations

import asyncio
import json
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
    # A post and a pin are not about the reader the way a mention or a DM is,
    # so their leads name the room instead: the channel is the only thing that
    # makes them worth opening.
    "post": "posted in",
    "pin": "A message was pinned in",
}

#: What the channel-naming kinds read as when the channel cannot be resolved.
#: Only reachable if a room is deleted between the queue write and the flush,
#: but "A message was pinned in" with nothing after it is a sentence that ends
#: mid-thought in somebody's inbox.
_KIND_LABELS_NO_CHANNEL = {
    "post": "posted",
    "pin": "A message was pinned",
}

#: Kinds whose lead is completed by the channel the thing happened in.
_CHANNEL_SUFFIX_KINDS = ("post", "pin")

#: Kinds whose lead does NOT name the message's author. A pin's actor is
#: whoever pressed pin, not whoever wrote the message, and the queue row points
#: at the message: "Dr Chen pinned a message" about Dr Chen's own post would be
#: a plain untruth, and adding an actor column to carry the pinner would be a
#: schema change for one line of email copy.
_ACTORLESS_KINDS = ("pin",)

#: Which preference switch each kind answers to. A kind absent here rides
#: ``activity_emails``, which is what every kind did before the split.
_KIND_STREAM = {
    "post": "post",
    "pin": "pin",
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
    notify_post: bool = False,
) -> None:
    """Enqueue digest notifications for one just-persisted message: every
    mentioned member, plus (for a top-level #task-announcements post) every
    member. Never the author; each user at most once per message.

    ``notify_post`` adds the ``post`` kind for every member of the channel.

    WHAT THE post KIND IS SCOPED TO, AND WHY. It is opt-in per write, and the
    only writers that opt in are the bot content paths: the morning routine
    (every scope, including the discussion poll), the news and papers digests,
    and the staff spotlight. An ordinary member's message in #general enqueues
    nothing, exactly as before. So does an admin persona post outside
    #task-announcements, because the admin route still clamps ``announce`` to
    that one channel (PRD-E requirement 12) and widening it is a product
    decision rather than a plumbing one.

    That is deliberate and it is not a member-count judgement. The content run
    is a handful of posts a day into rooms a physician cannot otherwise tell
    have moved, and it is the thing the founders want mailed. Human chatter is
    the opposite shape: it arrives in bursts, it already lights the unread
    badge, and the reader is usually one of the people producing it. Fanning a
    row out for every message in a live thread would mean a "While you were
    away" email every five minutes describing a conversation the member is
    having, which is the one outcome that gets this whole mechanism switched
    off. A member who wants to be told about a specific human message has
    @mention, and the person writing it decides.

    If human posts ever should mail, the switch is here and per-write, so it
    can be turned on for one channel at a time rather than for the product.
    """
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
    elif notify_post and not message.get("parent_message_id"):
        # A thread reply is a continuation of something the room already saw;
        # only a new top-level post is news.
        for uid in member_ids:
            if uid != author and uid not in queued:
                cstore.enqueue_notification(user_id=uid, kind="post", message_id=message["id"])
                queued.add(uid)


def queue_for_pin(
    cstore: CommunityStore,
    *,
    message: Dict[str, Any],
    pinned_by: str,
    member_ids: List[str],
) -> None:
    """Tell a channel's members that one of its messages was pinned.

    Pinning is a WebSocket event and nothing else, so it is invisible to
    everybody not connected at that second, which is most people most of the
    time. A pin is also the one moderation-shaped act that is meant to be seen:
    it is how the room says "read this one".

    Never the pinner (they just did it) and never the message's author. Riding
    the same queue as every other kind means it inherits the batching, the
    preference check and the retry without a second send path.
    """
    author = message.get("author_user_id")
    for uid in member_ids:
        if uid in (pinned_by, author):
            continue
        cstore.enqueue_notification(user_id=uid, kind="pin", message_id=message["id"])


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


#: Bot posts that carry a structured payload and get their own designed email
#: rather than a line in the activity digest.
DIGEST_KINDS = ("digest_news", "digest_papers")


def digest_payload_of(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The structured digest behind a message, or None for anything else.

    None covers three cases that must all behave identically: not a digest, a
    digest written before ``payload_json`` existed, and a payload row that will
    not parse. All three fall back to the activity line, which is what every
    digest got before this and is still true, just plainer.
    """
    if (message or {}).get("kind") not in DIGEST_KINDS:
        return None
    raw = (message or {}).get("payload_json")
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str) and raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
    else:
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    # The shape, not just the truthiness. ``lead_and_rest`` and the email
    # builder call ``.get`` on every entry, so a payload whose items are strings
    # raises AttributeError
    # INSIDE the flush loop, which has no per-member guard: the exception
    # unwinds to the per-realm handler and aborts that realm's whole flush
    # before any later member's mail is built. Deterministic, so it would repeat
    # every tick and the queue would stall for everyone behind that row rather
    # than drain. The serializer and the JS card both check this already; this
    # is the third renderer agreeing with them.
    if not isinstance(items, list) or not items:
        return None
    if not all(isinstance(i, dict) for i in items):
        return None
    return payload


def _wants_digest(payload: Dict[str, Any], prefs: Dict[str, Any]) -> bool:
    """Does this member's news cadence match this digest?

    The rule the standalone digest mailer has always used, moved here rather
    than reimplemented: news is the daily habit, papers ride the weekly
    preference, and ``off`` takes both. Kept because it is the switch the
    preferences page actually shows for news — ``post_emails`` is the generic
    "the bot posted something" toggle and was never what a physician meant when
    they chose a digest cadence.
    """
    want = "weekly" if payload.get("kind") == "papers" else "daily"
    return (prefs.get("news_frequency") or "daily") == want


def _slugs_by_channel_id(cstore: CommunityStore) -> Dict[str, str]:
    """One channel read per flush, not one per queued row.

    A flush can hold hundreds of rows across dozens of members, and the only
    thing they need from the channel table is a slug for the digest line.
    """
    try:
        return {c["id"]: str(c.get("slug") or "")
                for c in cstore.list_channels(include_inactive=True)}
    except Exception:  # noqa: BLE001 - a digest line is not worth an exception
        return {}


def _channel_slug_of(slugs: Dict[str, str], message: Dict[str, Any]) -> str:
    """The channel a queued message sits in, or "" for a DM or a missing room."""
    cid = str(message.get("channel_id") or "")
    if not cid or cid.startswith("dm-"):
        return ""
    return slugs.get(cid, "")


def _lead(kind: str, actor_name: str, slug: str) -> str:
    """The first half of one digest line: who did what, and where when the
    where is the point."""
    if kind in _CHANNEL_SUFFIX_KINDS and not slug:
        label, where = _KIND_LABELS_NO_CHANNEL[kind], ""
    else:
        label = _KIND_LABELS.get(kind, "posted")
        where = f" #{slug}" if kind in _CHANNEL_SUFFIX_KINDS else ""
    if kind in _ACTORLESS_KINDS:
        return f"{label}{where}"
    return f"{actor_name} {label}{where}"


async def flush_pending(
    cstore: Optional[CommunityStore] = None,
    *,
    resolve_member: Any = None,
) -> int:
    """Send one digest email per user with unsent notifications; mark them
    sent ON SUCCESS. ``resolve_member(user_id) -> {email, display_name} | None``
    is injected by the router (it owns the member map). Returns emails sent.

    A failed send leaves the rows pending and counts an attempt against them,
    so the next flush retries; after ``MAX_NOTIFICATION_ATTEMPTS`` the queue
    gives up and says so in the log. Marking rows sent regardless of the send
    was the old behaviour, and it meant one transient vendor error silently ate
    a member's activity mail with nothing anywhere recording it.
    """
    from email_utils import send_html_email  # local import — optional transport

    cstore = cstore or get_community_store()
    pending = cstore.unsent_notifications()
    if not pending:
        return 0

    by_user: Dict[str, List[Dict[str, Any]]] = {}
    for n in pending:
        by_user.setdefault(n["user_id"], []).append(n)

    slugs = _slugs_by_channel_id(cstore)
    sent_count = 0
    for user_id, items in by_user.items():
        member = resolve_member(user_id) if resolve_member else None
        if not member or not member.get("email"):
            # No longer a member (deactivated, role change): drop silently but
            # mark handled so the queue can't grow unboundedly.
            cstore.mark_notifications_sent([n["id"] for n in items])
            continue
        # One prefs read per member per flush. Every switch this loop consults,
        # and the unsubscribe token, live on the same row.
        prefs = cstore.email_prefs(user_id)

        def wants(stream: str, _prefs: Dict[str, Any] = prefs) -> bool:
            column = CommunityStore.TOGGLE_STREAMS.get(stream)
            raw = _prefs.get(column) if column else None
            return True if raw is None else bool(int(raw))

        rows: List[tuple] = []
        activity_ids: List[int] = []
        # A digest post leaves the activity list and becomes its own email: the
        # structure is the post, and flattening it to a 140-character snippet of
        # the body is what put "**Medical AI Digest** **Clinical Practice** -
        # [Opinion:" in physicians' inboxes verbatim (PRD §2.1).
        digest_sends: List[tuple] = []
        # Handled, but can never produce mail: the message was deleted or is
        # gone. Marked sent regardless of any send below, or the queue re-reads
        # them on every later flush forever.
        orphan_ids: List[int] = []
        dropped_ids: List[int] = []
        kinds: set = set()
        for n in items:
            kind = n["kind"]
            msg = cstore.get_message(n["message_id"])
            if not msg or msg.get("deleted"):
                orphan_ids.append(n["id"])
                continue
            payload = digest_payload_of(msg)
            if payload is not None:
                # A DIGEST ANSWERS TO THE NEWS CADENCE, and to that alone.
                # It is queued as a ``post``, so riding the post toggle as well
                # would make "daily news" mean "daily news, if you also left bot
                # posts on" — a silent AND across two switches, only one of
                # which the preferences page presents as being about news. The
                # cadence is the switch a physician actually chose.
                #
                # A cadence they did not ask for is an opt-out, not a failure:
                # mark it handled so it stops being re-read every flush.
                if not _wants_digest(payload, prefs):
                    dropped_ids.append(n["id"])
                    continue
                digest_sends.append((payload, n["id"]))
                continue
            # Opted out of THIS stream. Mark handled rather than leaving the
            # rows pending, or the queue grows forever and every later flush
            # re-reads them. The in-app notification is unaffected; only the
            # email stops.
            if not wants(_KIND_STREAM.get(kind, "activity")):
                dropped_ids.append(n["id"])
                continue
            activity_ids.append(n["id"])
            actor = resolve_member(msg["author_user_id"]) if resolve_member else None
            actor_name = (actor or {}).get("display_name") or _actor_fallback(
                msg["author_user_id"])
            kinds.add(kind)
            # Plain-text (lead, detail) pairs; the email builder owns escaping
            # and layout, so no HTML is composed here.
            rows.append((_lead(kind, actor_name, _channel_slug_of(slugs, msg)),
                         _snippet(msg.get("body") or "")))
        if dropped_ids:
            cstore.mark_notifications_sent(dropped_ids)
        if orphan_ids:
            cstore.mark_notifications_sent(orphan_ids)

        from community import links  # noqa: PLC0415 — one URL definition

        async def _deliver(subject: str, body: str, ids: List[int]) -> bool:
            """Send one email and settle its rows. True when it went out.

            A failed send leaves the rows PENDING and counts an attempt, so the
            next flush retries. Marking them sent regardless was the old
            behaviour, and it meant one transient vendor error silently ate a
            member's mail with nothing anywhere recording it.
            """
            if await send_html_email(member["email"], subject, body):
                cstore.mark_notifications_sent(ids)
                return True
            gave_up = cstore.record_notification_failure(ids)
            if gave_up:
                log.error(
                    "community email GAVE UP after %d attempts for one recipient "
                    "(%d notification(s) will never be mailed)",
                    cstore.MAX_NOTIFICATION_ATTEMPTS, len(gave_up))
            else:
                log.warning(
                    "community email failed for one recipient; %d notification(s) "
                    "stay queued for the next flush", len(ids))
            return False

        for payload, nid in digest_sends:
            from onboarding_emails import (  # noqa: PLC0415
                build_community_digest_post_email, digest_email_subject,
            )

            if await _deliver(
                digest_email_subject(payload),
                build_community_digest_post_email(
                    payload=payload,
                    community_url=links.community_url(),
                    # No ``kind``: "news" is a cadence, not a toggle stream, so
                    # narrowing to it would resolve to the broad unsubscribe
                    # anyway while claiming in the URL to do something smaller.
                    # This is the same link the digest has always carried.
                    unsubscribe_url=links.unsubscribe_url(
                        prefs.get("unsubscribe_token") or ""),
                    first_name=((member.get("display_name") or "").split()
                                or ["there"])[0],
                ),
                [nid],
            ):
                sent_count += 1

        # ``rows`` and ``activity_ids`` are appended together, so an empty row
        # list means there is no activity email to send and nothing left to
        # settle: the digests above have settled themselves, and the dropped and
        # orphaned rows were marked before them.
        if not rows:
            continue

        from onboarding_emails import build_community_digest_email  # noqa: PLC0415

        body = build_community_digest_email(
            activity_items=rows,
            community_url=links.community_url(),
            unsubscribe_url=links.unsubscribe_url(
                prefs.get("unsubscribe_token") or "",
                kind=_unsubscribe_kind(kinds),
            ),
        )
        if await _deliver(
            "New activity in your Archangel Health community", body, activity_ids
        ):
            sent_count += 1
    return sent_count


def _actor_fallback(author_user_id: str) -> str:
    """A name for an author the member map cannot resolve.

    The bot is the common case and it is not in the map by design (a real
    account would leak into the directory), so "A colleague" would be wrong for
    most of the mail this now sends.
    """
    try:
        from community.persona import display_name  # noqa: PLC0415
        from community.system_posts import SYSTEM_USER_ID  # noqa: PLC0415

        if author_user_id == SYSTEM_USER_ID:
            return display_name()
    except Exception:  # noqa: BLE001
        pass
    return "A colleague"


def _unsubscribe_kind(kinds: set) -> Optional[str]:
    """Which stream this email's one-click link should stop.

    One kind in the batch means the link can be precise: mail that is only
    about pins gets a link that stops pins. A mixed batch keeps the broad link,
    because the email builder takes ONE unsubscribe URL and a link that stopped
    an arbitrary one of the three streams in it would be a lie about which
    button the reader pressed.
    """
    if len(kinds) != 1:
        return None
    only = next(iter(kinds))
    return _KIND_STREAM.get(only)


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
