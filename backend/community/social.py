"""Community v2.1 social surfaces — events, polls, pinned messages, bookmarks.

A separate router module (AGENTS.md: new feature = new router, don't grow the
big one). Mounted at ``/api/community`` in ``main.py`` beside the core
community router. Reuses that router's gate (``require_member`` /
``require_community_admin``), member map, serializer, and audit — this module
never re-implements auth or PHI handling, it composes them.

Every write that produces human-visible text (event bodies, poll questions and
options, bookmark titles) is posted through the same PHI-gated paths as chat;
pins reference already-scanned content. Realtime updates fan out through
``router.broadcast_channel_event``, which is the shared WS hub scoped to the
people the channel is visible to -- a staff-only room's contents must not ride
the socket to every connected physician.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from community import events as cevents
from community import notify as cnotify
from community import phi_gate
from community.schema import BookmarkIn, EventIn, PollIn, VoteIn
from community.system_posts import channel_member_ids, post_system_message
from ratelimit import rate_limiter

# Reused from the core community router — no cycle (router.py never imports this).
from community.router import (
    _audit, _cstore, _is_admin, _require_message_access, _serialize_messages,
    _visible_channel_or_404, broadcast_channel_event, channel_by_id, is_staff_user,
    member_map, require_community_admin, require_member, require_poster,
)

log = logging.getLogger("community.social")

router = APIRouter(prefix="/api/community", tags=["community-social"])


# ══════════════════════════════════════════════════════════════════════════════
# EVENTS
# ══════════════════════════════════════════════════════════════════════════════
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _norm_iso_z(value: Optional[str]) -> Optional[str]:
    """Coerce an incoming datetime string to ISO-Z UTC form for storage. Accepts
    ``2026-09-10T17:00:00Z`` / ``...+00:00`` / ``2026-09-10 17:00``; rejects
    anything not date-time shaped so a garbage start can't break sorting."""
    if not value:
        return None
    v = value.strip()
    if not _ISO_RE.match(v):
        raise HTTPException(status_code=400, detail="Invalid date/time; use ISO-8601")
    v = v.replace(" ", "T")
    if v.endswith("Z"):
        return v
    if re.search(r"[+-]\d{2}:\d{2}$", v):
        from datetime import datetime, timezone  # noqa: PLC0415
        # astimezone(utc) EXPLICITLY — the argless form converts to the host's
        # local zone, which we would then mislabel 'Z' and store hours off on
        # any non-UTC deploy (bad start time, bad .ics, bad reminder window).
        return (datetime.fromisoformat(v).astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"))
    # No zone marker → treat as already-UTC wall time.
    if len(v) == 16:  # YYYY-MM-DDTHH:MM
        v += ":00"
    return v + "Z"


def _event_out(cstore, event: Dict[str, Any], user_id: Optional[str]) -> Dict[str, Any]:
    return cstore.event_public(event, viewer_id=user_id)


@router.post("/events", dependencies=[Depends(rate_limiter("community_event", 20, 60))])
async def create_event(
    body: EventIn, request: Request,
    admin: Dict[str, Any] = Depends(require_community_admin),
):
    cstore = _cstore()
    members = member_map()
    channel = _visible_channel_or_404(body.channel_slug, members, user=admin)

    starts = _norm_iso_z(body.starts_at)
    ends = _norm_iso_z(body.ends_at) if body.ends_at else None
    if ends and ends < starts:
        raise HTTPException(status_code=400, detail="Event ends before it starts")

    # PHI gate on all free text before anything is stored (title/desc/host/loc).
    blob = " \n ".join(x for x in [body.title, body.description, body.host, body.location] if x)
    if phi_gate.scan_text(blob):
        raise HTTPException(status_code=422, detail={
            "code": "phi_detected",
            "message": "Event text looks like it contains patient-identifiable "
                       "information. Events are colleague-facing only.",
        })

    event = cstore.create_event(
        channel_id=channel["id"], title=body.title.strip(),
        description=(body.description or "").strip() or None,
        starts_at=starts, ends_at=ends, timezone=(body.timezone or None),
        location=(body.location or "").strip() or None,
        host=(body.host or "").strip() or None, created_by=admin["id"],
    )

    # Linked in-stream message (kind='event') so the event threads + appears in
    # the channel history. The date is DELIBERATELY not in the body — a full
    # calendar date trips the PHI exact-date rule (same as digests), and the
    # card renders the date from the structured event row anyway.
    parts = [f"📅 **{body.title.strip()}**"]
    if body.host:
        parts.append("Host: " + body.host.strip())
    if body.description:
        parts.append(body.description.strip())
    parts.append("_See the pinned card at the top of #events for the time and to RSVP._")
    posted = await post_system_message(
        channel_slug=channel["slug"], body="\n".join(parts), kind="event")
    if posted:
        cstore.link_event_message(event["id"], posted["id"])
        event = cstore.get_event(event["id"])

    _audit(request, admin, "community.event_create", "ok",
           {"channel": channel["slug"], "event_id": event["id"]})
    out = _event_out(cstore, event, admin["id"])
    await broadcast_channel_event(
        {"type": "event.created", "event": out, "channel": channel["slug"]}, channel)
    return out


@router.get("/events")
async def list_events(
    scope: str = "upcoming", channel_slug: str = "events",
    user: Dict[str, Any] = Depends(require_member),
):
    cstore = _cstore()
    members = member_map()
    channel = _visible_channel_or_404(channel_slug, members, user=user)
    if scope not in ("upcoming", "past"):
        scope = "upcoming"
    rows = cstore.list_events(channel["id"], scope=scope)
    return {"scope": scope, "events": [_event_out(cstore, e, user["id"]) for e in rows]}


@router.get("/events/{event_id}")
async def get_event(event_id: int, user: Dict[str, Any] = Depends(require_member)):
    cstore = _cstore()
    event = cstore.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_out(cstore, event, user["id"])


@router.post("/events/{event_id}/rsvp",
             dependencies=[Depends(rate_limiter("community_rsvp", 60, 60))])
async def rsvp_event(event_id: int, request: Request,
                     user: Dict[str, Any] = Depends(require_poster)):
    cstore = _cstore()
    event = cstore.get_event(event_id)
    if not event or event.get("cancelled_at"):
        raise HTTPException(status_code=404, detail="Event not found")
    interested = cstore.toggle_rsvp(event_id, user["id"])
    _audit(request, user, "community.event_rsvp", "ok",
           {"event_id": event_id, "interested": interested})
    out = _event_out(cstore, event, user["id"])
    await broadcast_channel_event(
        {"type": "event.rsvp", "event_id": event_id, "rsvp_count": out["rsvp_count"]},
        channel_by_id(event.get("channel_id")))
    return out


@router.post("/events/{event_id}/cancel")
async def cancel_event(event_id: int, request: Request,
                       admin: Dict[str, Any] = Depends(require_community_admin)):
    cstore = _cstore()
    event = cstore.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    cstore.cancel_event(event_id)
    _audit(request, admin, "community.event_cancel", "ok", {"event_id": event_id})
    out = _event_out(cstore, cstore.get_event(event_id), admin["id"])
    await broadcast_channel_event({"type": "event.updated", "event": out},
                                  channel_by_id(event.get("channel_id")))
    return out


@router.get("/events/{event_id}/calendar.ics")
async def event_ics(event_id: int, user: Dict[str, Any] = Depends(require_member)):
    cstore = _cstore()
    event = cstore.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    ics = cevents.build_ics(event)
    fname = re.sub(r"[^\w.-]+", "-", (event.get("title") or "event"))[:60] or "event"
    return Response(
        content=ics, media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="{fname}.ics"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# POLLS
# ══════════════════════════════════════════════════════════════════════════════
def _poll_channel_or_404(poll: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    """The poll's channel, or a 404 for someone who cannot see that room.

    A poll id is a small integer, so "you need the id to reach it" is not a
    control. Without this a member could vote in the staff room and read the
    question back out of the results payload.
    """
    channel = channel_by_id(poll.get("channel_id"))
    if not channel or (channel.get("staff_only") and not is_staff_user(user)):
        raise HTTPException(status_code=404, detail="Poll not found")
    return channel
@router.post("/polls", dependencies=[Depends(rate_limiter("community_poll", 20, 60))])
async def create_poll(body: PollIn, request: Request,
                      user: Dict[str, Any] = Depends(require_poster)):
    cstore = _cstore()
    members = member_map()
    channel = _visible_channel_or_404(body.channel_slug, members, user=user)
    # An admin-post channel restricts top-level posts to admins — a poll IS a
    # top-level post, so honor the same gate.
    if channel["post_policy"] == "admin" and not _is_admin(user):
        raise HTTPException(status_code=403,
                            detail="Only the Archangel team can post here.")
    question = body.question.strip()
    options = [o.strip() for o in body.options if o and o.strip()]
    if len(options) < 2:
        raise HTTPException(status_code=400, detail="A poll needs at least two options")
    if phi_gate.scan_text(question + " \n " + " \n ".join(options)):
        raise HTTPException(status_code=422, detail={
            "code": "phi_detected",
            "message": "Poll text looks like it contains patient-identifiable information.",
        })

    poll = cstore.create_poll(channel_id=channel["id"], question=question,
                              options=options, created_by=user["id"])
    # A poll is member-created content — always authored by its creator (never
    # the bot), so it reads as "Dr. X asked…". The question was PHI-scanned
    # above, so the direct insert is safe.
    msg = cstore.insert_message(channel_id=channel["id"], author_user_id=user["id"],
                                body="📊 **Poll:** " + question, kind="poll")
    cstore.link_poll_message(poll["id"], msg["id"])
    posted = _serialize_messages([msg], members, channel["slug"], viewer_id=user["id"])[0]
    await broadcast_channel_event({"type": "message.created", "message": posted}, channel)

    _audit(request, user, "community.poll_create", "ok",
           {"channel": channel["slug"], "poll_id": poll["id"], "message_id": posted["id"]})
    results = cstore.poll_results(poll["id"], viewer_id=user["id"])
    await broadcast_channel_event(
        {"type": "poll.updated", "message_id": posted["id"], "poll": results}, channel)
    return {"poll": results, "message_id": posted["id"]}


@router.post("/polls/{poll_id}/vote",
             dependencies=[Depends(rate_limiter("community_vote", 60, 60))])
async def vote_poll(poll_id: int, body: VoteIn, request: Request,
                    user: Dict[str, Any] = Depends(require_poster)):
    cstore = _cstore()
    poll = cstore.get_poll(poll_id)
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    channel = _poll_channel_or_404(poll, user)
    if poll.get("closed_at"):
        raise HTTPException(status_code=409, detail="This poll is closed")
    try:
        cstore.vote_poll(poll_id, body.option_id, user["id"])
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown option")
    results = cstore.poll_results(poll_id, viewer_id=user["id"])
    await broadcast_channel_event(
        {"type": "poll.updated", "message_id": poll.get("message_id"),
         "poll": {**results, "your_vote": None}}, channel)
    return results


@router.post("/polls/{poll_id}/close")
async def close_poll(poll_id: int, request: Request,
                     user: Dict[str, Any] = Depends(require_poster)):
    cstore = _cstore()
    poll = cstore.get_poll(poll_id)
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    channel = _poll_channel_or_404(poll, user)
    if poll["created_by"] != user["id"] and not _is_admin(user):
        raise HTTPException(status_code=403, detail="Only the poll's author or an admin can close it")
    cstore.close_poll(poll_id)
    _audit(request, user, "community.poll_close", "ok", {"poll_id": poll_id})
    results = cstore.poll_results(poll_id, viewer_id=user["id"])
    # your_vote is VIEWER-SPECIFIC — never fan it out (same guard as /vote).
    # Broadcasting the closer's choice both leaks it and makes every other
    # client render that option as its own selection.
    await broadcast_channel_event(
        {"type": "poll.updated", "message_id": poll.get("message_id"),
         "poll": {**results, "your_vote": None}}, channel)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# PINNED MESSAGES
# ══════════════════════════════════════════════════════════════════════════════
async def _broadcast_pins(cstore, channel: Dict[str, Any]) -> List[Dict[str, Any]]:
    members = member_map()
    pins = _serialize_messages(cstore.list_pins(channel["id"]), members, channel["slug"])
    await broadcast_channel_event(
        {"type": "pins.updated", "channel": channel["slug"], "pins": pins}, channel)
    return pins


@router.post("/messages/{message_id}/pin")
async def pin_message(message_id: int, request: Request,
                      user: Dict[str, Any] = Depends(require_poster)):
    cstore = _cstore()
    msg = cstore.get_message(message_id)
    if not msg or msg.get("deleted"):
        raise HTTPException(status_code=404, detail="Message not found")
    kind, container = _require_message_access(user, msg)
    if kind != "channel":
        raise HTTPException(status_code=400, detail="Only channel messages can be pinned")
    cstore.pin_message(channel_id=container["id"], message_id=message_id, pinned_by=user["id"])
    _audit(request, user, "community.message_pin", "ok",
           {"channel": container["slug"], "message_id": message_id})
    # Pinning was WebSocket-only, so it reached whoever happened to be
    # connected in that second and nobody else. A pin is the one act that means
    # "everybody read this one", so it earns a queued notification like a
    # mention does, on the same batching loop and the same email.
    cnotify.queue_for_pin(
        cstore, message=msg, pinned_by=user["id"],
        member_ids=channel_member_ids(container, member_map()),
    )
    pins = await _broadcast_pins(cstore, container)
    return {"ok": True, "pinned": True, "pins": pins}


@router.delete("/messages/{message_id}/pin")
async def unpin_message(message_id: int, request: Request,
                        user: Dict[str, Any] = Depends(require_poster)):
    cstore = _cstore()
    msg = cstore.get_message(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    kind, container = _require_message_access(user, msg)
    if kind != "channel":
        raise HTTPException(status_code=400, detail="Only channel messages can be pinned")
    cstore.unpin_message(channel_id=container["id"], message_id=message_id)
    _audit(request, user, "community.message_unpin", "ok",
           {"channel": container["slug"], "message_id": message_id})
    pins = await _broadcast_pins(cstore, container)
    return {"ok": True, "pinned": False, "pins": pins}


@router.get("/channels/{slug}/pins")
async def list_pins(slug: str, user: Dict[str, Any] = Depends(require_member)):
    cstore = _cstore()
    members = member_map()
    channel = _visible_channel_or_404(slug, members, user=user)
    pins = _serialize_messages(cstore.list_pins(channel["id"]), members, channel["slug"])
    return {"channel": channel["slug"], "pins": pins}


# ══════════════════════════════════════════════════════════════════════════════
# CHANNEL BOOKMARKS
# ══════════════════════════════════════════════════════════════════════════════
_URL_OK = re.compile(r"^https?://", re.I)


@router.post("/channels/{slug}/bookmarks")
async def add_bookmark(slug: str, body: BookmarkIn, request: Request,
                       user: Dict[str, Any] = Depends(require_poster)):
    if not _URL_OK.match(body.url.strip()):
        raise HTTPException(status_code=400, detail="Bookmark URL must start with http(s)://")
    # A bookmark TITLE is new human-visible free text, not already-scanned
    # content — it goes through the §7 gate like every other member write.
    # The URL itself is structural and deliberately not scanned (article links
    # carry digit runs that false-trip MRN rules — same call as system_posts).
    if phi_gate.scan_text(body.title):
        raise HTTPException(status_code=422, detail={
            "code": "phi_detected",
            "message": "Bookmark title looks like it contains patient-identifiable "
                       "information. Links are colleague-facing only.",
        })
    cstore = _cstore()
    members = member_map()
    channel = _visible_channel_or_404(slug, members, user=user)
    bm = cstore.add_bookmark(channel_id=channel["id"], title=body.title.strip(),
                             url=body.url.strip(), added_by=user["id"])
    _audit(request, user, "community.bookmark_add", "ok",
           {"channel": channel["slug"], "bookmark_id": bm["id"]})
    out = _bookmark_out(bm)
    await broadcast_channel_event(
        {"type": "bookmark.added", "channel": channel["slug"], "bookmark": out}, channel)
    return out


@router.delete("/bookmarks/{bookmark_id}")
async def remove_bookmark(bookmark_id: int, request: Request,
                          user: Dict[str, Any] = Depends(require_poster)):
    cstore = _cstore()
    bm = cstore.get_bookmark(bookmark_id)
    if not bm:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    if bm["added_by"] != user["id"] and not _is_admin(user):
        raise HTTPException(status_code=403, detail="Only the person who added it (or an admin) can remove it")
    cstore.remove_bookmark(bookmark_id)
    channel = next((c for c in cstore.list_channels(include_inactive=True)
                    if c["id"] == bm["channel_id"]), None)
    slug = channel["slug"] if channel else None
    _audit(request, user, "community.bookmark_remove", "ok",
           {"channel": slug, "bookmark_id": bookmark_id})
    await broadcast_channel_event(
        {"type": "bookmark.removed", "channel": slug, "bookmark_id": bookmark_id}, channel)
    return {"ok": True, "id": bookmark_id}


@router.get("/channels/{slug}/bookmarks")
async def list_bookmarks(slug: str, user: Dict[str, Any] = Depends(require_member)):
    cstore = _cstore()
    members = member_map()
    channel = _visible_channel_or_404(slug, members, user=user)
    return {"channel": channel["slug"],
            "bookmarks": [_bookmark_out(b) for b in cstore.list_bookmarks(channel["id"])]}


def _bookmark_out(bm: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": bm["id"], "title": bm["title"], "url": bm["url"],
            "added_by": bm["added_by"]}
