"""Asclepius Community — HTTP + WebSocket surface (Community PRD §1, §4, §6, §7).

Mounted at ``/api/community`` in ``main.py``. Every endpoint (REST and WS)
enforces the §1 gate server-side: authenticated Asclepius user, contributor
(evaluator) role with VERIFIED credentials — or Archangel staff (admin /
qa_reviewer) — never a buyer or data partner, never a community-banned user.

The §7 PHI gate runs in the message-create and message-edit handlers BEFORE
persistence and BEFORE any WebSocket broadcast; blocked text is never stored,
and only the detected categories (never content) reach the audit log.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter, Depends, File, HTTPException, Query, Request, UploadFile,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import Response

from asclepius import auth as asc_auth
from asclepius.credentials import generalized_blurb, find_tier_b_leak
from asclepius.constants import TIER_B_FORBIDDEN_KEYS
from asclepius.store import get_store as get_asclepius_store
from audit import audit_log
from community import attachments as catt
from community import notify as cnotify
from community import phi_gate
from community.schema import MessageEdit, MessageIn, ReactionIn, ReadIn
from community.store import get_community_store
from community.ws import hub
from ratelimit import rate_limiter

log = logging.getLogger("community.router")

router = APIRouter(prefix="/api/community", tags=["community"])

GATE_MESSAGE = "Community access is for verified contributors."

# Deterministic specialty accent (mirrors the portal's chip color map: nephrology
# green, cardiology orange, oncology pink, others cycle — Community PRD §2).
_SPECIALTY_ACCENTS = {"nephrology": "green", "cardiology": "orange", "oncology": "pink"}
_ACCENT_CYCLE = ["lime", "green", "orange", "pink"]


def specialty_accent(specialty: Optional[str]) -> str:
    s = (specialty or "").strip().lower()
    if not s:
        return "green"
    if s in _SPECIALTY_ACCENTS:
        return _SPECIALTY_ACCENTS[s]
    acc = 0
    for ch in s:
        acc = (acc + ord(ch)) % 997
    return _ACCENT_CYCLE[acc % len(_ACCENT_CYCLE)]


def _cstore():
    return get_community_store()


def _astore():
    return get_asclepius_store()


def block_flag_threshold() -> int:
    try:
        return max(1, int(os.getenv("COMMUNITY_BLOCK_FLAG_THRESHOLD", "3")))
    except (TypeError, ValueError):
        return 3


# ─── §1 gate ──────────────────────────────────────────────────────────────────
def _passes_gate(user: Optional[Dict[str, Any]]) -> bool:
    """Contributor (verified evaluator) or Archangel staff; nobody else."""
    if not user or not user.get("active"):
        return False
    if _cstore().is_banned(user["id"]):
        return False
    role = user.get("role")
    if role in ("admin", "qa_reviewer"):
        return True
    if role != "evaluator":
        return False
    cred = _astore().get_contributor_credentials(user.get("id_hashed") or "")
    return bool(cred and cred.get("credentials_verified"))


def require_member(
    user: Optional[Dict[str, Any]] = Depends(asc_auth.get_current_user_optional),
) -> Dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in through the doctor portal to open the community.")
    if not _passes_gate(user):
        raise HTTPException(status_code=403, detail=GATE_MESSAGE)
    return user


def require_community_admin(
    user: Dict[str, Any] = Depends(require_member),
) -> Dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def _is_admin(user: Dict[str, Any]) -> bool:
    return user.get("role") == "admin"


# ─── Member profiles (PRD §2 — auto-populated, Tier A only) ───────────────────
def _display_name(user: Dict[str, Any]) -> str:
    full = (user.get("full_name") or "").strip()
    if full:
        return full
    local = (user.get("email") or "member").split("@", 1)[0]
    pretty = re.sub(r"[._\-+]+", " ", local).strip() or "Member"
    return " ".join(w.capitalize() for w in pretty.split())


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _scrub_tier_b(obj: Any) -> Any:
    """Defense in depth: recursively drop any Tier B / identifying key from a
    payload about to leave the server (PRD §2 privacy)."""
    forbidden = {k.lower() for k in TIER_B_FORBIDDEN_KEYS}
    if isinstance(obj, dict):
        return {
            k: _scrub_tier_b(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and k.lower() in forbidden)
        }
    if isinstance(obj, list):
        return [_scrub_tier_b(v) for v in obj]
    return obj


def member_map(*, include_email: bool = False) -> Dict[str, Dict[str, Any]]:
    """Every gated member keyed by user id. Built exclusively from Tier A
    attributes + the users table — the Tier B vault is never opened here."""
    astore = _astore()
    cstore = _cstore()
    banned = set(cstore.banned_user_ids())
    out: Dict[str, Dict[str, Any]] = {}
    for user in astore.list_users():
        if not user.get("active") or user["id"] in banned:
            continue
        role = user.get("role")
        cred = None
        if user.get("id_hashed"):
            cred = astore.get_contributor_credentials(user["id_hashed"])
        verified = bool(cred and cred.get("credentials_verified"))
        if role in ("admin", "qa_reviewer"):
            is_staff = True
        elif role == "evaluator" and verified:
            is_staff = False
        else:
            continue
        ship = (cred or {}).get("ship") or {}
        specialty = ship.get("primary_specialty") or user.get("specialty")
        years = ship.get("years_in_active_practice")
        if years is None:
            years = user.get("years_experience")
        try:
            years = int(years) if years is not None else None
        except (TypeError, ValueError):
            years = None
        name = _display_name(user)
        if is_staff:
            blurb = "Archangel Health team."
        else:
            blurb = (cred or {}).get("blurb") or generalized_blurb(ship, fallback_specialty=specialty)
        member: Dict[str, Any] = {
            "user_id": user["id"],
            "display_name": name,
            "initials": _initials(name),
            "specialty": specialty,
            "specialty_accent": specialty_accent(specialty),
            "years_in_practice": years,
            "institution": (cred or {}).get("organization")
                or user.get("organization") or user.get("org_name"),
            "board_certified": bool(ship.get("board_certifications") or user.get("board_cert")),
            "fellowship_trained": bool(ship.get("fellowship_trained")),
            "verified": verified,
            "is_admin": role == "admin",
            "is_staff": is_staff,
            "blurb": blurb,
        }
        if include_email:
            member["email"] = user.get("email")
        out[user["id"]] = member
    return out


def public_member(member: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not member:
        return None
    pub = {k: v for k, v in member.items() if k != "email"}
    pub = _scrub_tier_b(pub)
    # Belt and braces (PRD §2): the payload must be leak-free by construction;
    # if the scanner still finds a Tier B key, fail loudly rather than serve it.
    leak = find_tier_b_leak(pub)
    if leak:  # pragma: no cover — construction is whitelist-based
        log.error("community member payload contained a Tier B key; withheld")
        raise HTTPException(status_code=500, detail="profile unavailable")
    return pub


_GHOST_MEMBER = {
    "user_id": None,
    "display_name": "Former member",
    "initials": "—",
    "specialty": None,
    "specialty_accent": "green",
    "years_in_practice": None,
    "institution": None,
    "board_certified": False,
    "fellowship_trained": False,
    "verified": False,
    "is_admin": False,
    "is_staff": False,
    "blurb": None,
}


def resolve_member_for_notify(user_id: str) -> Optional[Dict[str, Any]]:
    """Injected into notify.flush_pending — includes the email address (used
    only as the send target, never serialized into an API payload)."""
    return member_map(include_email=True).get(user_id)


# ─── Serialization ────────────────────────────────────────────────────────────
def _serialize_messages(
    msgs: List[Dict[str, Any]],
    members: Dict[str, Dict[str, Any]],
    channel_slug: str,
) -> List[Dict[str, Any]]:
    cstore = _cstore()
    ids = [m["id"] for m in msgs]
    replies = cstore.reply_counts(ids)
    reactions = cstore.reactions_for(ids)
    out = []
    for m in msgs:
        deleted = bool(m.get("deleted"))
        rc = replies.get(m["id"]) or {}
        out.append({
            "id": m["id"],
            "channel": channel_slug,
            "parent_message_id": m.get("parent_message_id"),
            "author": public_member(members.get(m["author_user_id"])) or dict(_GHOST_MEMBER),
            "body": "" if deleted else m["body"],
            "deleted": deleted,
            "created_at": m["created_at"],
            "edited_at": m.get("edited_at"),
            "mentions": [] if deleted else (m.get("mentions") or []),
            "attachments": [] if deleted else (m.get("attachments") or []),
            "reactions": [] if deleted else (reactions.get(m["id"]) or []),
            "reply_count": int(rc.get("count") or 0),
            "last_reply_at": rc.get("last_at"),
        })
    return out


def _serialize_one(msg: Dict[str, Any], channel_slug: str) -> Dict[str, Any]:
    return _serialize_messages([msg], member_map(), channel_slug)[0]


def _audit(request: Optional[Request], user: Dict[str, Any], action: str, outcome: str,
           detail: Dict[str, Any]) -> None:
    """Hash-chained audit event (PRD §7.5) — metadata only, NEVER content."""
    audit_log.record(
        actor_type="asclepius_user",
        actor_id=user.get("id"),
        action=action,
        outcome=outcome,
        resource_type="community",
        resource=str(detail.get("message_id") or detail.get("channel") or ""),
        source_ip=(request.client.host if request and request.client else None),
        user_agent=(request.headers.get("user-agent") if request else None),
        detail=detail,
    )


def _phi_block(request: Optional[Request], user: Dict[str, Any], surface: str,
               findings: List[Dict[str, Any]], channel_slug: Optional[str]) -> HTTPException:
    """Record a §7.3/§7.5 block event (category only), flag repeat offenders,
    and build the structured 422. The blocked text is never stored anywhere."""
    categories = phi_gate.categories_of(findings)
    cstore = _cstore()
    cstore.record_block_event(user_id=user["id"], surface=surface, categories=categories)
    detail: Dict[str, Any] = {"surface": surface, "categories": categories}
    if channel_slug:
        detail["channel"] = channel_slug
    count = cstore.block_count(user["id"])
    if count >= block_flag_threshold():
        detail["repeat_flag"] = True
        detail["block_count"] = count
    _audit(request, user, "community.phi_block", "blocked", detail)
    payload = phi_gate.block_message(findings)
    return HTTPException(status_code=422, detail=payload)


# ─── Me / gate probe ──────────────────────────────────────────────────────────
@router.get("/me")
async def me(user: Dict[str, Any] = Depends(require_member)):
    members = member_map()
    return {
        "member": public_member(members.get(user["id"])) or dict(_GHOST_MEMBER),
        "is_admin": _is_admin(user),
        "notice": "Colleague discussion only. Do not post patient-identifiable information.",
        "retention": "Messages are retained indefinitely unless an admin removes them.",
    }


# ─── Channels ─────────────────────────────────────────────────────────────────
@router.get("/channels")
async def channels(user: Dict[str, Any] = Depends(require_member)):
    cstore = _cstore()
    unread = cstore.unread_counts(user["id"])
    return {
        "channels": [
            {
                "slug": ch["slug"],
                "name": ch["name"],
                "description": ch["description"],
                "post_policy": ch["post_policy"],
                "unread": (unread.get(ch["slug"]) or {}).get("unread", 0),
                "mentions": (unread.get(ch["slug"]) or {}).get("mentions", 0),
            }
            for ch in cstore.list_channels()
        ]
    }


@router.get("/channels/{slug}/messages")
async def channel_messages(
    slug: str,
    before: Optional[int] = Query(default=None, ge=1),
    after: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: Dict[str, Any] = Depends(require_member),
):
    cstore = _cstore()
    channel = cstore.get_channel_by_slug(slug)
    if not channel:
        raise HTTPException(status_code=404, detail="Unknown channel")
    msgs = cstore.list_messages(channel["id"], before_id=before, after_id=after, limit=limit)
    serialized = _serialize_messages(msgs, member_map(), channel["slug"])
    return {
        "channel": channel["slug"],
        "messages": serialized,
        # History pages (default + ``before``) may have older messages above;
        # ``after`` polls are inherently complete up to "now".
        "has_more": after is None and len(msgs) >= limit,
    }


# ─── Messages: create / edit / delete ─────────────────────────────────────────
def _validate_mentions(mention_ids: List[str], members: Dict[str, Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    for uid in mention_ids or []:
        if uid in members and uid not in seen:
            seen.append(uid)
    return seen


def _resolve_attachments(
    ids: List[str], user: Dict[str, Any]
) -> List[Dict[str, Any]]:
    cstore = _cstore()
    out: List[Dict[str, Any]] = []
    for asset_id in ids or []:
        att = cstore.get_attachment(asset_id)
        if not att or att.get("uploader_user_id") != user["id"]:
            raise HTTPException(status_code=400, detail="Unknown attachment")
        if att.get("message_id"):
            raise HTTPException(status_code=400, detail="Attachment already posted")
        out.append({
            "asset_id": att["asset_id"],
            "mime": att["mime"],
            "byte_size": att["byte_size"],
            "name": att.get("orig_name") or "attachment",
        })
    return out


@router.post(
    "/channels/{slug}/messages",
    dependencies=[Depends(rate_limiter("community_post", 30, 60))],
)
async def post_message(
    slug: str,
    body: MessageIn,
    request: Request,
    user: Dict[str, Any] = Depends(require_member),
):
    cstore = _cstore()
    channel = cstore.get_channel_by_slug(slug)
    if not channel:
        raise HTTPException(status_code=404, detail="Unknown channel")

    text = (body.body or "").strip()
    if not text and not body.attachment_ids:
        raise HTTPException(status_code=400, detail="Message is empty")

    parent_id: Optional[int] = None
    if body.parent_message_id is not None:
        parent = cstore.get_message(int(body.parent_message_id))
        if not parent or parent["channel_id"] != channel["id"]:
            raise HTTPException(status_code=404, detail="Thread not found")
        # Replies always attach to the thread ROOT (no nested threads in v1).
        parent_id = parent["parent_message_id"] or parent["id"]
        root = cstore.get_message(parent_id)
        if not root or root.get("deleted"):
            raise HTTPException(status_code=404, detail="Thread not found")

    # #task-announcements: admin-only top-level posts; threaded replies open
    # to everyone (PRD §3).
    if channel["post_policy"] == "admin" and parent_id is None and not _is_admin(user):
        raise HTTPException(
            status_code=403,
            detail="Only the Archangel team can post announcements. Reply in a thread instead.",
        )

    # ── §7: PHI gate — BEFORE persistence, BEFORE broadcast. ──
    findings = phi_gate.scan_text(text)
    if findings:
        raise _phi_block(request, user, "message", findings, channel["slug"])

    members = member_map()
    mentions = _validate_mentions(body.mention_user_ids, members)
    attachments = _resolve_attachments(body.attachment_ids, user)

    msg = cstore.insert_message(
        channel_id=channel["id"],
        author_user_id=user["id"],
        body=text,
        parent_message_id=parent_id,
        mentions=mentions,
        attachments=attachments,
    )
    _audit(request, user, "community.message_create", "ok", {
        "channel": channel["slug"], "message_id": msg["id"],
        "thread": parent_id is not None,
        "attachments": len(attachments), "mentions": len(mentions),
    })

    # The author has obviously read their own message.
    cstore.set_read(user["id"], channel["id"], msg["id"])

    cnotify.queue_for_message(
        cstore, message=msg, channel=channel, member_ids=list(members.keys())
    )

    serialized = _serialize_messages([msg], members, channel["slug"])[0]
    await hub.broadcast({"type": "message.created", "message": serialized})
    return serialized


@router.patch("/messages/{message_id}")
async def edit_message(
    message_id: int,
    body: MessageEdit,
    request: Request,
    user: Dict[str, Any] = Depends(require_member),
):
    cstore = _cstore()
    msg = cstore.get_message(message_id)
    if not msg or msg.get("deleted"):
        raise HTTPException(status_code=404, detail="Message not found")
    if msg["author_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="You can only edit your own messages")

    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")

    # ── §7: PHI gate on the edit path too — an edit is a write. ──
    channel = next((c for c in cstore.list_channels() if c["id"] == msg["channel_id"]), None)
    slug = channel["slug"] if channel else None
    findings = phi_gate.scan_text(text)
    if findings:
        raise _phi_block(request, user, "edit", findings, slug)

    mentions: Optional[List[str]] = None
    if body.mention_user_ids is not None:
        mentions = _validate_mentions(body.mention_user_ids, member_map())
    updated = cstore.edit_message(message_id, body=text, mentions=mentions)
    if not updated or updated.get("deleted"):
        raise HTTPException(status_code=404, detail="Message not found")

    _audit(request, user, "community.message_edit", "ok",
           {"channel": slug, "message_id": message_id})
    serialized = _serialize_one(updated, slug or "")
    await hub.broadcast({"type": "message.updated", "message": serialized})
    return serialized


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: int,
    request: Request,
    user: Dict[str, Any] = Depends(require_member),
):
    cstore = _cstore()
    msg = cstore.get_message(message_id)
    if not msg or msg.get("deleted"):
        raise HTTPException(status_code=404, detail="Message not found")
    if msg["author_user_id"] != user["id"] and not _is_admin(user):
        raise HTTPException(status_code=403, detail="You can only delete your own messages")

    cstore.soft_delete_message(message_id, deleted_by=user["id"])
    channel = next((c for c in cstore.list_channels() if c["id"] == msg["channel_id"]), None)
    slug = channel["slug"] if channel else ""
    _audit(request, user, "community.message_delete", "ok", {
        "channel": slug, "message_id": message_id,
        "moderator": msg["author_user_id"] != user["id"],
    })
    event = {
        "type": "message.deleted",
        "id": message_id,
        "channel": slug,
        "parent_message_id": msg.get("parent_message_id"),
    }
    await hub.broadcast(event)
    return {"ok": True, "id": message_id}


# ─── Reactions ────────────────────────────────────────────────────────────────
_EMOJI_FORBIDDEN = re.compile(r"[A-Za-z0-9<>&\"'`=\\/]")


@router.post("/messages/{message_id}/reactions")
async def toggle_reaction(
    message_id: int,
    body: ReactionIn,
    user: Dict[str, Any] = Depends(require_member),
):
    emoji = (body.emoji or "").strip()
    if not emoji or _EMOJI_FORBIDDEN.search(emoji):
        raise HTTPException(status_code=400, detail="Not an emoji")
    cstore = _cstore()
    msg = cstore.get_message(message_id)
    if not msg or msg.get("deleted"):
        raise HTTPException(status_code=404, detail="Message not found")
    added = cstore.toggle_reaction(message_id, user["id"], emoji)
    reactions = cstore.reactions_for([message_id]).get(message_id) or []
    channel = next((c for c in cstore.list_channels() if c["id"] == msg["channel_id"]), None)
    await hub.broadcast({
        "type": "reaction",
        "message_id": message_id,
        "channel": channel["slug"] if channel else "",
        "parent_message_id": msg.get("parent_message_id"),
        "reactions": reactions,
    })
    return {"ok": True, "added": added, "reactions": reactions}


# ─── Threads ──────────────────────────────────────────────────────────────────
@router.get("/messages/{message_id}/thread")
async def thread(message_id: int, user: Dict[str, Any] = Depends(require_member)):
    cstore = _cstore()
    root = cstore.get_message(message_id)
    if not root:
        raise HTTPException(status_code=404, detail="Message not found")
    if root.get("parent_message_id"):
        root = cstore.get_message(root["parent_message_id"]) or root
    channel = next((c for c in cstore.list_channels() if c["id"] == root["channel_id"]), None)
    slug = channel["slug"] if channel else ""
    members = member_map()
    replies = cstore.list_thread(root["id"])
    return {
        "root": _serialize_messages([root], members, slug)[0],
        "replies": _serialize_messages(replies, members, slug),
    }


# ─── Reads / unread badge ─────────────────────────────────────────────────────
@router.post("/channels/{slug}/read")
async def mark_read(
    slug: str,
    body: ReadIn,
    user: Dict[str, Any] = Depends(require_member),
):
    cstore = _cstore()
    channel = cstore.get_channel_by_slug(slug)
    if not channel:
        raise HTTPException(status_code=404, detail="Unknown channel")
    cstore.set_read(user["id"], channel["id"], body.last_read_message_id)
    return {"ok": True, "unread": cstore.unread_counts(user["id"])}


@router.get("/badge")
async def badge(user: Optional[Dict[str, Any]] = Depends(asc_auth.get_current_user_optional)):
    """Unread badge for the portal side-panel Community item (PRD §4). Soft
    endpoint: a non-member gets ``eligible: false`` rather than an error, so
    the portal can decide whether to render the item at all."""
    if not user or not _passes_gate(user):
        return {"eligible": False, "unread": 0, "mentions": 0}
    counts = _cstore().unread_counts(user["id"])
    return {
        "eligible": True,
        "unread": sum(c["unread"] for c in counts.values()),
        "mentions": sum(c["mentions"] for c in counts.values()),
        "channels": counts,
    }


# ─── Members ──────────────────────────────────────────────────────────────────
@router.get("/members")
async def members_endpoint(
    specialty: Optional[str] = Query(default=None),
    user: Dict[str, Any] = Depends(require_member),
):
    members = [public_member(m) for m in member_map().values()]
    if specialty:
        want = specialty.strip().lower()
        members = [m for m in members if (m.get("specialty") or "").lower() == want]
    online = set(await hub.online_user_ids())
    for m in members:
        m["online"] = m["user_id"] in online
    members.sort(key=lambda m: ((m.get("display_name") or "").lower()))
    return {"members": members, "count": len(members)}


# ─── Search ───────────────────────────────────────────────────────────────────
@router.get("/search", dependencies=[Depends(rate_limiter("community_search", 30, 60))])
async def search(
    q: str = Query(min_length=1, max_length=200),
    user: Dict[str, Any] = Depends(require_member),
):
    cstore = _cstore()
    channels_by_id = {c["id"]: c for c in cstore.list_channels()}
    members = member_map()
    results = []
    for msg in cstore.search_messages(q):
        ch = channels_by_id.get(msg["channel_id"])
        if not ch:
            continue
        results.append(_serialize_messages([msg], members, ch["slug"])[0])
    return {"query": q, "results": results}


# ─── Attachments (PRD §4, §7.4) ───────────────────────────────────────────────
@router.post(
    "/attachments",
    dependencies=[Depends(rate_limiter("community_upload", 10, 60))],
)
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_member),
):
    data = await file.read()
    mime = file.content_type or ""
    try:
        clean, out_mime = catt.process_attachment(data, mime)
    except catt.AttachmentRejected as exc:
        payload = exc.payload
        if payload.get("code") == "phi_detected":
            cstore = _cstore()
            cstore.record_block_event(
                user_id=user["id"], surface="attachment",
                categories=payload.get("categories") or [],
            )
            _audit(request, user, "community.phi_block", "blocked", {
                "surface": "attachment",
                "categories": payload.get("categories") or [],
            })
            raise HTTPException(status_code=422, detail=payload)
        status = {"too_large": 413, "unsupported_type": 415}.get(payload.get("code"), 422)
        raise HTTPException(status_code=status, detail=payload)

    sha = hashlib.sha256(clean).hexdigest()
    from asclepius.assets import _write_blob  # reuse the content-addressed store (PRD §4)
    _write_blob(sha, clean)

    safe_name = re.sub(r"[^\w.\- ]+", "", (file.filename or "attachment"))[:80] or "attachment"
    asset_id = "catt-" + uuid.uuid4().hex[:20]
    att = _cstore().insert_attachment(
        asset_id=asset_id, sha256=sha, mime=out_mime, byte_size=len(clean),
        orig_name=safe_name, uploader_user_id=user["id"],
    )
    _audit(request, user, "community.attachment_upload", "ok",
           {"asset_id": asset_id, "bytes": len(clean), "mime": out_mime})
    return {
        "asset_id": att["asset_id"], "mime": att["mime"],
        "byte_size": att["byte_size"], "name": att["orig_name"],
    }


@router.get("/attachments/{asset_id}")
async def download_attachment(
    asset_id: str,
    user: Dict[str, Any] = Depends(require_member),
):
    att = _cstore().get_attachment(asset_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    # An attachment bound to a deleted message is gone with the message.
    if att.get("message_id"):
        msg = _cstore().get_message(att["message_id"])
        if not msg or msg.get("deleted"):
            raise HTTPException(status_code=404, detail="Attachment not found")
    from asclepius.assets import AssetError, load_asset
    try:
        data, _mime = load_asset(att["sha256"])
    except AssetError:
        raise HTTPException(status_code=404, detail="Attachment not found")
    filename = (att.get("orig_name") or "attachment").replace('"', "")
    return Response(
        content=data,
        media_type=att["mime"],
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ─── Moderation / admin (PRD §7.5) ────────────────────────────────────────────
@router.get("/admin/flags")
async def admin_flags(admin: Dict[str, Any] = Depends(require_community_admin)):
    """Repeat PHI-block offenders (category counts only — never content)."""
    members = member_map()
    flagged = _cstore().flagged_users(threshold=block_flag_threshold())
    for f in flagged:
        m = members.get(f["user_id"])
        f["display_name"] = (m or {}).get("display_name") or "Former member"
    return {"threshold": block_flag_threshold(), "flagged": flagged}


@router.post("/admin/members/{user_id}/deactivate")
async def deactivate_member(
    user_id: str,
    request: Request,
    admin: Dict[str, Any] = Depends(require_community_admin),
):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="You cannot deactivate yourself")
    target = _astore().get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Unknown member")
    _cstore().ban_member(user_id=user_id, banned_by=admin["id"])
    _audit(request, admin, "community.member_deactivate", "ok", {"target_user_id": user_id})
    return {"ok": True}


@router.post("/admin/members/{user_id}/reactivate")
async def reactivate_member(
    user_id: str,
    request: Request,
    admin: Dict[str, Any] = Depends(require_community_admin),
):
    _cstore().unban_member(user_id)
    _audit(request, admin, "community.member_reactivate", "ok", {"target_user_id": user_id})
    return {"ok": True}


# ─── WebSocket (PRD §4, §6) ───────────────────────────────────────────────────
@router.websocket("/ws")
async def community_ws(websocket: WebSocket):
    """Real-time delivery. Browser WebSockets cannot send an Authorization
    header, so the Asclepius JWT arrives as ``?token=``; it is validated with
    the same decode + store lookup + §1 gate as every REST call. A failed gate
    closes with 4401/4403 and never joins the hub."""
    await websocket.accept()
    token = websocket.query_params.get("token") or ""
    payload = asc_auth.decode_token(token)
    user = None
    if payload:
        user = _astore().get_user_by_id(payload.get("sub", ""))
    if not user or not user.get("active"):
        await websocket.close(code=4401)
        return
    if not _passes_gate(user):
        await websocket.close(code=4403)
        return

    members = member_map()
    me_member = members.get(user["id"]) or dict(_GHOST_MEMBER)
    first = await hub.connect(websocket, user["id"])
    try:
        await websocket.send_json({"type": "hello", "online": await hub.online_user_ids()})
        if first:
            await hub.broadcast(
                {"type": "presence", "online": await hub.online_user_ids()},
                exclude=websocket,
            )
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            etype = event.get("type")
            if etype == "ping":
                await websocket.send_json({"type": "pong"})
            elif etype == "typing":
                slug = str(event.get("channel") or "")[:64]
                await hub.broadcast({
                    "type": "typing",
                    "channel": slug,
                    "thread_root": event.get("thread_root"),
                    "user_id": user["id"],
                    "name": me_member.get("display_name") or "Someone",
                }, exclude=websocket)
    finally:
        last_of_user = await hub.disconnect(websocket)
        if last_of_user:
            await hub.broadcast({"type": "presence", "online": await hub.online_user_ids()})
