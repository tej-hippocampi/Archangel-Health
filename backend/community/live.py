"""Live delivery for a conversation write: the half that makes it ARRIVE.

Persisting a message and delivering it are two different acts, and until this
module existed only one caller did both. ``community.router.post_dm_message``
enqueued the digest notification and pushed a ``message.created`` down the
socket; every server-authored write — the routing DM from the bot, the case
room and its introduction — called ``insert_message`` and stopped there.

That gap is invisible in the database and total on the screen. The client stops
its 5s poll for as long as the WebSocket is healthy (a healthy socket is
supposed to be the faster path), so a row nobody pushed is a row nobody sees
until they reload. It read as two separate bugs — "a message someone sends does
not pop up" and "the case room is never created" — and it was one.

So: the delivery half lives here, once, and every writer routes through it.

**Best effort, always.** These functions are called after an assignment has been
committed and after a message row has been written. A community outage must
cost a physician a page refresh and never their routing, which is the rule
``asclepius.route_notify.notify_routed`` states for itself. Nothing in here
raises on a delivery failure that a caller could not have prevented.

Imports of ``community.router`` are LOCAL to each function on purpose: the
router imports this module, and the serialization helpers live over there
because they are also what the REST responses are built from. ``route_notify``
already imports ``community.store`` lazily inside its functions for the same
reason; this follows that pattern rather than inventing a second one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("community.live")


def serialize_for_conversation(cstore: Any, dm: Dict[str, Any],
                               message: Dict[str, Any]) -> Dict[str, Any]:
    """One message as the conversation's own participants read it."""
    from community.router import _serialize_messages, member_map  # noqa: PLC0415

    return _serialize_messages([message], member_map(), None, dm_id=dm["id"])[0]


async def deliver_message(
    cstore: Any, dm: Dict[str, Any], message: Dict[str, Any],
    *, serialized: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Queue the digest and push ``message.created`` to everyone in ``dm``.

    Returns the serialized message, because the REST handler needs exactly that
    as its response body and building it twice would be two chances to differ.

    ``hub.send_to_users`` rather than a broadcast: a direct message must never
    ride the all-member fan-out, which is the privacy invariant the DM section
    of the router is written around.
    """
    from community import notify as cnotify  # noqa: PLC0415
    from community.ws import hub  # noqa: PLC0415

    participants = cstore.dm_participants(dm)
    author = message.get("author_user_id")
    for recipient_id in participants:
        if recipient_id != author:
            cnotify.enqueue_dm(cstore, recipient_id=recipient_id, message=message)
    body = serialized or serialize_for_conversation(cstore, dm, message)
    await hub.send_to_users(participants,
                            {"type": "message.created", "message": body})
    return body


async def announce_conversation(
    cstore: Any, dm: Dict[str, Any], *, user_ids: Optional[List[str]] = None,
) -> None:
    """Push ``dm.created`` so a conversation appears in the rail without a reload.

    Sent PER USER rather than as one event to the group, because the summary is
    VIEWER-RELATIVE: a two-party DM names the other person, and which person
    that is depends on who is reading. One shared payload would tell somebody
    they are talking to themselves.

    Safe to call for a conversation that already exists — the client inserts a
    conversation it does not hold and updates one it does — which is what makes
    it correct on a case room that gained a member rather than being created.
    """
    from community.router import _dm_summary, member_map  # noqa: PLC0415
    from community.ws import hub  # noqa: PLC0415

    targets = list(user_ids if user_ids is not None else cstore.dm_participants(dm))
    members = member_map()
    for user_id in targets:
        try:
            summary = _dm_summary(dm, user_id, members)
        except Exception:  # pragma: no cover — one bad row must not stop the rest
            log.info("community.live: could not summarize %s for %s",
                     dm.get("id"), user_id)
            continue
        await hub.send_to_users([user_id], {"type": "dm.created", "dm": summary})
