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

import datetime as _dt
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
from fastapi.responses import HTMLResponse, Response

from asclepius import auth as asc_auth
from asclepius import capabilities as asc_caps
from asclepius.credentials import generalized_blurb, find_tier_b_leak
from asclepius.constants import TIER_B_FORBIDDEN_KEYS
from asclepius.store import get_store as get_asclepius_store
from audit import audit_log
from community import attachments as catt
from community import notify as cnotify
from community import phi_gate
from community.schema import (
    DmMessageIn, DmOpen, HandoffRedeem, MessageEdit, MessageIn, ReactionIn, ReadIn,
)
from community import store as cstore_mod
from community.store import get_community_store
from community.ws import hub
from ratelimit import rate_limiter

log = logging.getLogger("community.router")

router = APIRouter(prefix="/api/community", tags=["community"])

GATE_MESSAGE = "Community access is for verified contributors."

# v2.1 broadcast tokens. ``@channel`` (admin-only, durable) stores a sentinel
# mention that ``notify``/``unread_counts`` expand to every member. ``@here``
# (any member, ephemeral) is presentational + real-time only.
BROADCAST_MENTION = "*channel*"
_CHANNEL_TOKEN = re.compile(r"(?<![\w/])@channel\b", re.I)

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
def _verified_colleague(user: Dict[str, Any], cred: Optional[Dict[str, Any]]) -> bool:
    """Verified by EITHER credentialing system: the Contributors-vault flag
    (``contributor_credentials.credentials_verified`` — demo seeds and the
    admin credential PUT) OR the PRD-B admin approval decision
    (``users.verification_status == 'approved'`` — the verify queue, which
    stamps verified_by/verified_at/tier). The two pipelines are disjoint
    writers; a physician verified by either is a colleague here. Three
    outcomes stay three outcomes: only the exact string ``approved`` passes —
    NULL (pre-verification era / undecided), ``pending`` and ``rejected`` all
    fail this leg."""
    if cred and cred.get("credentials_verified"):
        return True
    return (user.get("verification_status") or "").strip().lower() == "approved"


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
    # An account that is not a physician is admitted on its SURFACE cap and
    # nothing else. An advisor holds COMMUNITY_READ and is let in to read; a
    # referral-only account holds neither and is refused here, which is the
    # whole of what "referral link and nothing else" means.
    #
    # This leg is deliberately scoped to non-physician kinds. Asking
    # ``can_surface`` about a PHYSICIAN would widen the gate below by accident:
    # access_level folds a NULL verification_status in with 'approved', so every
    # pre-verification-era account with no vault row would suddenly pass, which
    # is exactly the widening the next comment block warns against.
    kind = asc_caps.account_kind(user)
    if kind is not None:
        return asc_caps.can_surface(user, asc_caps.COMMUNITY_READ)
    # A physician awaiting verification is admitted. The community is the most
    # valuable thing we can hand someone during a one-to-two day wait, and a
    # read-only member is a lurker who does not come back. They have already
    # cleared an OTP on their institutional mailbox, submitted an NPI, and
    # signed the confidentiality and independent-judgment attestations, which is
    # more identity proof than most professional forums ask for before a first
    # post. DMs and attachments still wait: see require_verified_member.
    #
    # _verified_colleague keeps its exact meaning and is still consulted, so a
    # vault-verified contributor carrying a NULL status passes as it always did.
    # It is now a BADGE input as well as a gate input; do not change it.
    # ONLY 'pending' is added here, not "anything the surface table allows".
    # access_level deliberately folds NULL in with 'approved', which is right for
    # the evaluator surface (a pre-verification-era account has always been able
    # to work) and wrong here: the community has always required a positive
    # signal, and a NULL account with no vault row has never been let in. Adding
    # it now would widen this gate as a side effect of a change about pending.
    if user.get("verification_status") == "pending":
        return True
    cred = _astore().get_contributor_credentials(user.get("id_hashed") or "")
    return _verified_colleague(user, cred)


def require_member(
    user: Optional[Dict[str, Any]] = Depends(asc_auth.get_current_user_optional),
) -> Dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in through the doctor portal to open the community.")
    if not _passes_gate(user):
        raise HTTPException(status_code=403, detail=GATE_MESSAGE)
    return user


def require_poster(
    user: Dict[str, Any] = Depends(require_member),
) -> Dict[str, Any]:
    """A member who may put something INTO the community, not just read it.

    ``COMMUNITY_WRITE`` existed in ``capabilities`` from the start and nothing
    imported it, so every content-writing route in here was ``require_member``
    only: reading and writing were the same permission. That was harmless while
    everyone who could reach the community was a physician. It stops being
    harmless the moment a non-clinical account can read the room -- an advisor
    would have been able to post in a channel of doctors, react to their
    messages, open polls and unpin their pins.

    A physician's behaviour does not change: PROVISIONAL still grants
    ``COMMUNITY_WRITE``, so a doctor under review posts exactly as they did.
    """
    if not asc_caps.can_surface(user, asc_caps.COMMUNITY_WRITE):
        raise HTTPException(
            status_code=403,
            detail=(
                "Your account has view-only access to the community. You can "
                "read every channel; posting is for the physicians doing the work."
            ),
        )
    return user


def require_verified_member(
    user: Dict[str, Any] = Depends(require_poster),
) -> Dict[str, Any]:
    """A member whose credentials have actually cleared.

    Channel posts are open to a physician still under review; direct messages
    and file uploads are not. Those two are the unsolicited-contact and the PHI
    vectors respectively, and they are the two worth making someone wait a day
    for. Everything else in the community is the same for both.

    Chained on ``require_poster`` rather than ``require_member``: a DM is
    strictly more privileged than a channel post, so someone who may not post at
    all may certainly not message a physician privately. Without that chain an
    advisor would have passed this gate outright, because a non-clinical account
    carries a NULL verification_status and access_level folds NULL in with
    'approved'.
    """
    if asc_caps.access_level(user) != asc_caps.FULL and user.get("role") not in ("admin", "qa_reviewer"):
        raise HTTPException(
            status_code=403,
            detail=(
                "Direct messages and file uploads open once your credentials are "
                "verified. You can post in any channel in the meantime."
            ),
        )
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
        # A non-physician account is never a member here, however its
        # verification lands. This is the directory, the mention target list and
        # the population the specialty and country channels count to decide
        # whether they exist -- an advisor belongs in none of those, and an
        # admin approving one must not quietly add them to a room of colleagues.
        # They read the community; they are not part of it.
        if asc_caps.account_kind(user) is not None:
            continue
        cred = None
        if user.get("id_hashed"):
            cred = astore.get_contributor_credentials(user["id_hashed"])
        # Same bridge as the gate — an approval-path member (no vault row)
        # must serialize as a member, not a ghost, and appear in the directory.
        # _verified_colleague keeps its exact original meaning. access_level
        # folds NULL in with 'approved', so using it here would mark every
        # pre-verification-era evaluator as a verified colleague, which is the
        # widening the gate above deliberately refuses.
        verified = _verified_colleague(user, cred)
        # A provisional physician can post, so they must also resolve in the
        # directory: a member who appears in a channel but in no member list is
        # exactly the ghost this function was written to avoid.
        provisional = (
            role == "evaluator"
            and not verified
            and user.get("verification_status") == "pending"
        )
        if role in ("admin", "qa_reviewer"):
            is_staff = True
        elif role == "evaluator" and (verified or provisional):
            is_staff = False
        else:
            continue
        verification_state = "verified" if verified else "provisional"
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
            # A face, if they uploaded one. This crosses no line the display
            # name above has not already crossed -- the community has always
            # shown physicians their colleagues' real names -- and it is
            # opt-in: absent unless they went and set it. The initials stay as
            # the fallback and remain what most members render as.
            "avatar_url": (
                f"/api/asclepius/users/{user['id']}/avatar"
                f"?v={(user.get('avatar_asset_sha') or '')[:12]}"
                if (user.get("avatar_asset_sha") or "").strip() else None
            ),
            "specialty": specialty,
            "specialty_accent": specialty_accent(specialty),
            # Where they practise, for the country channels. The CODE only:
            # a country is Tier A the way a specialty is, but a city or a
            # region would start to locate a named physician, and nothing in
            # the community needs that.
            "country": (user.get("country_of_practice")
                        or user.get("country_of_licensure") or None),
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


def _viewer_identity(user: Dict[str, Any]) -> Dict[str, Any]:
    """How a non-physician reader sees THEMSELVES in the community header.

    They are deliberately absent from ``member_map``: they are not colleagues,
    they must not appear in the directory, must not count toward the specialty
    and country channel thresholds, and must never receive a mention or a digest
    email. But a reader whose own name renders as "Former member" -- which is
    what the ghost fallback would have given them -- reads as a bug in the
    product on their first screen. This is a self-view only; it is never put in
    a member list and nothing they do produces an author record.
    """
    name = (user.get("full_name") or user.get("email") or "").strip()
    parts = [p for p in name.replace(".", " ").split() if p]
    initials = "".join(p[0] for p in parts[:2]).upper() or "?"
    return {
        **_GHOST_MEMBER,
        "user_id": user["id"],
        "display_name": name or "You",
        "initials": initials,
    }


def resolve_member_for_notify(user_id: str) -> Optional[Dict[str, Any]]:
    """Injected into notify.flush_pending — includes the email address (used
    only as the send target, never serialized into an API payload). The system
    author has no email and is not a member — returns None, which notify's
    drop-silently branch already handles."""
    return member_map(include_email=True).get(user_id)


# ─── Channel visibility (Community v2 — threshold-gated specialty channels) ───
def specialty_threshold() -> int:
    """Members of a specialty required before its channel appears
    (``COMMUNITY_SPECIALTY_MIN_MEMBERS``, default 3, floor 1)."""
    try:
        return max(1, int(os.getenv("COMMUNITY_SPECIALTY_MIN_MEMBERS", "3")))
    except (TypeError, ValueError):
        return 3


def _decode_cards(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            out = json.loads(raw)
        except ValueError:
            return []
        return out if isinstance(out, list) else []
    return []


def _specialty_counts(members: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Verified NON-STAFF members per lowercased specialty."""
    out: Dict[str, int] = {}
    for m in members.values():
        if m.get("is_staff"):
            continue
        s = (m.get("specialty") or "").strip().lower()
        if s:
            out[s] = out.get(s, 0) + 1
    return out


def _country_counts(members: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Non-staff members per country code."""
    out: Dict[str, int] = {}
    for m in members.values():
        if m.get("is_staff"):
            continue
        c = (m.get("country") or "").strip().upper()
        if c:
            out[c] = out.get(c, 0) + 1
    return out


def country_threshold() -> int:
    """How many colleagues a country needs before its channel appears.

    Same reasoning as the specialty threshold: a room with one person in it
    reads as an empty building, and the first doctor from a country should
    meet the community before they meet a channel with only their own name
    in it.
    """
    try:
        return max(1, int(os.getenv("COMMUNITY_COUNTRY_MIN_MEMBERS", "3")))
    except (TypeError, ValueError):
        return 3


def visible_channels(members: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The channel list every member sees (visibility is deliberately GLOBAL —
    identical for all members, so unread badges, search scope, and the WS
    broadcast never diverge per user). Core channels always show; a specialty
    channel shows once its specialty has >= threshold members, and STICKS once
    it has history (a channel with messages never vanishes because someone
    was deactivated)."""
    cstore = _cstore()
    counts = _specialty_counts(members)
    threshold = specialty_threshold()
    countries = _country_counts(members)
    country_min = country_threshold()
    out: List[Dict[str, Any]] = []
    for ch in cstore.list_channels():
        grp = (ch.get("grp") or "core")
        if grp == "specialty":
            spec = (ch.get("specialty") or "").strip().lower()
            if counts.get(spec, 0) >= threshold or cstore.channel_has_messages(ch["id"]):
                out.append(ch)
            continue
        if grp == "country":
            code = (ch.get("country") or "").strip().upper()
            if countries.get(code, 0) >= country_min or cstore.channel_has_messages(ch["id"]):
                out.append(ch)
            continue
        out.append(ch)
    return out


def _visible_channel_or_404(slug: str, members: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve a slug against the VISIBLE set — a hidden (below-threshold or
    deactivated) channel answers with the same 404 as an unknown one, so the
    API is no oracle for what exists (mirrors ``_require_message_access``)."""
    want = (slug or "").strip().lower()
    channel = next((c for c in visible_channels(members) if c["slug"] == want), None)
    if not channel:
        raise HTTPException(status_code=404, detail="Unknown channel")
    return channel


# ─── Message container resolution / access control ────────────────────────────
def _container_of(msg: Dict[str, Any]) -> tuple:
    """Resolve a message's container: ``("channel", row)`` or ``("dm", row)``
    or ``(None, None)``."""
    cid = msg.get("channel_id") or ""
    if cid.startswith("dm-"):
        dm = _cstore().get_dm(cid)
        return ("dm", dm) if dm else (None, None)
    # include_inactive: a message inside a deactivated channel must still
    # resolve for moderation/audit paths; the list/read routes above never
    # serve a hidden channel in the first place.
    channel = next(
        (c for c in _cstore().list_channels(include_inactive=True) if c["id"] == cid), None
    )
    return ("channel", channel) if channel else (None, None)


def _is_case_room(dm: Optional[Dict[str, Any]]) -> bool:
    return (dm or {}).get("kind") == cstore_mod.CommunityStore.ROOM_KIND


def _dm_access(user: Dict[str, Any], dm: Optional[Dict[str, Any]]) -> bool:
    """May this account reach this conversation at all?

    Two rules, and the second is a deliberate, narrow exception to the first.

    A two-party DM is its participants' and nobody else's, admins included.
    A CASE ROOM is the routed team plus Archangel admins (PRD D3): the room
    exists so founders can step into a stuck case, which is a capability a
    read-only exception could not deliver. Its intro says so out loud, and case
    CONTENT is forbidden in there precisely because the room is not private.
    """
    if not dm:
        return False
    if user["id"] in _cstore().dm_participants(dm):
        return True
    return _is_case_room(dm) and user.get("role") == "admin"


def _require_message_access(user: Dict[str, Any], msg: Dict[str, Any]) -> tuple:
    """THE visibility rule for anything reached by message id (edit, delete,
    react, thread, attachment download): channel messages are visible to every
    member; a DM message is visible ONLY to its participants, admins included:
    they have no read access to others' private conversations, except on a case
    room (``_dm_access``). A non-participant gets the same 404 as a
    nonexistent message (no oracle)."""
    kind, container = _container_of(msg)
    if kind is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if kind == "dm" and not _dm_access(user, container):
        raise HTTPException(status_code=404, detail="Message not found")
    return kind, container


# ─── Serialization ────────────────────────────────────────────────────────────
def _serialize_messages(
    msgs: List[Dict[str, Any]],
    members: Dict[str, Dict[str, Any]],
    channel_slug: Optional[str],
    *,
    dm_id: Optional[str] = None,
    viewer_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    from community.system_posts import SYSTEM_MEMBER, SYSTEM_USER_ID  # noqa: PLC0415 — avoids import cycle

    cstore = _cstore()
    ids = [m["id"] for m in msgs]
    replies = cstore.reply_counts(ids)
    reactions = cstore.reactions_for(ids)
    pinned_ids = cstore.pinned_message_ids(ids)  # v2.1 — batch pin lookup
    out = []
    for m in msgs:
        deleted = bool(m.get("deleted"))
        rc = replies.get(m["id"]) or {}
        if m["author_user_id"] == SYSTEM_USER_ID:
            # The virtual bot author — never a users row, never in member_map.
            author = public_member(dict(SYSTEM_MEMBER))
        else:
            author = public_member(members.get(m["author_user_id"])) or dict(_GHOST_MEMBER)
        row = {
            "id": m["id"],
            "channel": channel_slug,
            "dm": dm_id,
            "parent_message_id": m.get("parent_message_id"),
            "author": author,
            "kind": m.get("kind"),
            "body": "" if deleted else m["body"],
            # Link cards on a bot post. A deleted message shows none, for the
            # same reason its body goes: the point of a delete is that the
            # content stops being served.
            "cards": [] if deleted else _decode_cards(m.get("cards_json")),
            "deleted": deleted,
            "created_at": m["created_at"],
            "edited_at": m.get("edited_at"),
            "mentions": [] if deleted else (m.get("mentions") or []),
            "attachments": [] if deleted else (m.get("attachments") or []),
            "reactions": [] if deleted else (reactions.get(m["id"]) or []),
            "reply_count": int(rc.get("count") or 0),
            "last_reply_at": rc.get("last_at"),
            "pinned": m["id"] in pinned_ids,
        }
        # v2.1: attach the structured card payload for special message kinds.
        if not deleted and m.get("kind") == "poll":
            poll = cstore.poll_for_message(m["id"])
            if poll:
                row["poll"] = cstore.poll_results(poll["id"], viewer_id=viewer_id)
        elif not deleted and m.get("kind") == "event":
            ev = cstore.event_for_message(m["id"])
            if ev:
                row["event"] = cstore.event_public(ev, viewer_id=viewer_id)
        out.append(row)
    return out


def _serialize_one_resolved(msg: Dict[str, Any], *, viewer_id: Optional[str] = None) -> Dict[str, Any]:
    """Serialize a single message with its container resolved (slug or dm)."""
    kind, container = _container_of(msg)
    slug = container["slug"] if kind == "channel" else None
    dm_id = container["id"] if kind == "dm" else None
    return _serialize_messages([msg], member_map(), slug, dm_id=dm_id, viewer_id=viewer_id)[0]


async def _emit_message_event(event_type: str, serialized: Dict[str, Any],
                              dm: Optional[Dict[str, Any]]) -> None:
    """Channel events broadcast to every member; DM events go ONLY to the two
    participants (never the broadcast — PRD-level privacy invariant)."""
    event = {"type": event_type, "message": serialized}
    if dm:
        await hub.send_to_users(_cstore().dm_participants(dm), event)
    else:
        await hub.broadcast(event)


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
    member = public_member(members.get(user["id"]))
    if member is None and asc_caps.account_kind(user) is not None:
        member = _viewer_identity(user)
    return {
        "member": member or dict(_GHOST_MEMBER),
        # Whether this reader may put anything into the room. The composer keys
        # on it: a disabled box with a line saying why beats a box that accepts
        # a message and then 403s on send.
        "can_post": asc_caps.can_surface(user, asc_caps.COMMUNITY_WRITE),
        "is_admin": _is_admin(user),
        "notice": "Colleague discussion only. Do not post patient-identifiable information.",
        "retention": "Messages are retained indefinitely unless an admin removes them.",
    }


# ─── Channels ─────────────────────────────────────────────────────────────────
@router.get("/channels")
async def channels(user: Dict[str, Any] = Depends(require_member)):
    cstore = _cstore()
    members = member_map()
    visible = visible_channels(members)
    unread = cstore.unread_counts(user["id"], channels=visible)
    return {
        "channels": [
            {
                "slug": ch["slug"],
                "name": ch["name"],
                "description": ch["description"],
                "post_policy": ch["post_policy"],
                "group": ch.get("grp") or "core",
                "specialty": ch.get("specialty"),
                "country": ch.get("country"),
                "unread": (unread.get(ch["slug"]) or {}).get("unread", 0),
                "mentions": (unread.get(ch["slug"]) or {}).get("mentions", 0),
            }
            for ch in visible
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
    members = member_map()
    channel = _visible_channel_or_404(slug, members)
    msgs, has_more = cstore.list_messages(
        channel["id"], before_id=before, after_id=after, limit=limit
    )
    serialized = _serialize_messages(msgs, members, channel["slug"], viewer_id=user["id"])
    return {
        "channel": channel["slug"],
        "messages": serialized,
        # Computed by the store from the RAW row count, pre-tombstone-filter
        # (audit finding); meaningful for history pages AND ``after`` polls.
        "has_more": has_more,
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
    user: Dict[str, Any] = Depends(require_poster),
):
    cstore = _cstore()
    members = member_map()
    channel = _visible_channel_or_404(slug, members)

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

    mentions = _validate_mentions(body.mention_user_ids, members)

    # ── v2.1: @channel / @here broadcast tokens ──
    # @channel is a DURABLE broadcast (admin-only): a sentinel is stored so the
    # mention badge lights for every member and an email digest goes out.
    # @here is EPHEMERAL (any member): no stored sentinel, no email — it rides
    # the normal message.created WS to whoever is online, which is exactly its
    # meaning. Both are highlighted in the rendered body.
    has_channel = bool(_CHANNEL_TOKEN.search(text))
    if has_channel and not _is_admin(user):
        raise HTTPException(
            status_code=403,
            detail="@channel is limited to the Archangel team. Use @here to ping people who are online.",
        )
    if has_channel and BROADCAST_MENTION not in mentions:
        mentions = mentions + [BROADCAST_MENTION]

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

    # The author has obviously read their own message — but ONLY a top-level
    # post may advance the cursor: a thread reply's id is greater than every
    # unseen top-level message, so advancing on it would silently mark the
    # whole channel read (audit finding).
    if parent_id is None:
        cstore.set_read(user["id"], channel["id"], msg["id"])

    cnotify.queue_for_message(
        cstore, message=msg, channel=channel, member_ids=list(members.keys())
    )

    serialized = _serialize_messages([msg], members, channel["slug"])[0]
    await hub.broadcast({"type": "message.created", "message": serialized})
    return serialized


@router.patch(
    "/messages/{message_id}",
    dependencies=[Depends(rate_limiter("community_edit", 30, 60))],
)
async def edit_message(
    message_id: int,
    body: MessageEdit,
    request: Request,
    user: Dict[str, Any] = Depends(require_poster),
):
    cstore = _cstore()
    msg = cstore.get_message(message_id)
    if not msg or msg.get("deleted"):
        raise HTTPException(status_code=404, detail="Message not found")
    kind, container = _require_message_access(user, msg)
    if msg["author_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="You can only edit your own messages")

    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")

    # ── §7: PHI gate on the edit path too — an edit is a write. ──
    slug = container["slug"] if kind == "channel" else "dm"
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
    serialized = _serialize_one_resolved(updated)
    await _emit_message_event("message.updated", serialized,
                              container if kind == "dm" else None)
    return serialized


@router.delete(
    "/messages/{message_id}",
    dependencies=[Depends(rate_limiter("community_delete", 30, 60))],
)
async def delete_message(
    message_id: int,
    request: Request,
    user: Dict[str, Any] = Depends(require_poster),
):
    cstore = _cstore()
    msg = cstore.get_message(message_id)
    if not msg or msg.get("deleted"):
        raise HTTPException(status_code=404, detail="Message not found")
    kind, container = _require_message_access(user, msg)
    if msg["author_user_id"] != user["id"] and not _is_admin(user):
        raise HTTPException(status_code=403, detail="You can only delete your own messages")

    cstore.soft_delete_message(message_id, deleted_by=user["id"])
    slug = container["slug"] if kind == "channel" else None
    _audit(request, user, "community.message_delete", "ok", {
        "channel": slug or "dm", "message_id": message_id,
        "moderator": msg["author_user_id"] != user["id"],
    })
    event = {
        "type": "message.deleted",
        "id": message_id,
        "channel": slug,
        "dm": container["id"] if kind == "dm" else None,
        "parent_message_id": msg.get("parent_message_id"),
    }
    if kind == "dm":
        await hub.send_to_users(cstore.dm_participants(container), event)
    else:
        await hub.broadcast(event)
    return {"ok": True, "id": message_id}


# ─── Reactions ────────────────────────────────────────────────────────────────
_EMOJI_FORBIDDEN = re.compile(r"[A-Za-z0-9<>&\"'`=\\/]")


@router.post(
    "/messages/{message_id}/reactions",
    dependencies=[Depends(rate_limiter("community_react", 60, 60))],
)
async def toggle_reaction(
    message_id: int,
    body: ReactionIn,
    user: Dict[str, Any] = Depends(require_poster),
):
    emoji = (body.emoji or "").strip()
    if not emoji or _EMOJI_FORBIDDEN.search(emoji):
        raise HTTPException(status_code=400, detail="Not an emoji")
    cstore = _cstore()
    msg = cstore.get_message(message_id)
    if not msg or msg.get("deleted"):
        raise HTTPException(status_code=404, detail="Message not found")
    kind, container = _require_message_access(user, msg)
    added = cstore.toggle_reaction(message_id, user["id"], emoji)
    reactions = cstore.reactions_for([message_id]).get(message_id) or []
    event = {
        "type": "reaction",
        "message_id": message_id,
        "channel": container["slug"] if kind == "channel" else None,
        "dm": container["id"] if kind == "dm" else None,
        "parent_message_id": msg.get("parent_message_id"),
        "reactions": reactions,
    }
    if kind == "dm":
        await hub.send_to_users(cstore.dm_participants(container), event)
    else:
        await hub.broadcast(event)
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
    kind, container = _require_message_access(user, root)
    slug = container["slug"] if kind == "channel" else None
    dm_id = container["id"] if kind == "dm" else None
    members = member_map()
    replies = cstore.list_thread(root["id"])
    return {
        "root": _serialize_messages([root], members, slug, dm_id=dm_id, viewer_id=user["id"])[0],
        "replies": _serialize_messages(replies, members, slug, dm_id=dm_id, viewer_id=user["id"]),
    }


# ─── Reads / unread badge ─────────────────────────────────────────────────────
@router.post("/channels/{slug}/read")
async def mark_read(
    slug: str,
    body: ReadIn,
    user: Dict[str, Any] = Depends(require_member),
):
    cstore = _cstore()
    members = member_map()
    channel = _visible_channel_or_404(slug, members)
    cstore.set_read(user["id"], channel["id"], body.last_read_message_id)
    return {
        "ok": True,
        "unread": cstore.unread_counts(user["id"], channels=visible_channels(members)),
    }


@router.get("/prefs")
async def get_email_prefs(user: Dict[str, Any] = Depends(require_member)):
    prefs = _cstore().email_prefs(user["id"])
    return {
        "news_frequency": prefs["news_frequency"],
        "options": list(cstore_mod.NEWS_FREQUENCIES),
    }


@router.post("/prefs")
async def set_email_prefs(body: Dict[str, Any], user: Dict[str, Any] = Depends(require_member)):
    freq = str((body or {}).get("news_frequency") or "").strip()
    try:
        prefs = _cstore().set_news_frequency(user["id"], freq)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Choose one of: {', '.join(cstore_mod.NEWS_FREQUENCIES)}.",
        )
    return {"ok": True, "news_frequency": prefs["news_frequency"]}


@router.get("/unsubscribe")
async def unsubscribe(token: str = ""):
    """One click, no sign-in required.

    An unsubscribe link that makes someone log in first is an unsubscribe link
    that gets a spam complaint instead, and one complaint costs the sending
    domain that every other physician's mail goes through. The token only ever
    turns mail OFF, so the worst a leaked one can do is stop an email.
    """
    uid = _cstore().unsubscribe_by_token(token)
    body = (
        "You will not get the medical AI digest any more."
        if uid
        else "That link has already been used, or is not valid any more."
    )
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Email preferences</title>"
        "<body style=\"margin:0;background:#eef0ef;color:#1a1b1a;"
        "font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;\">"
        "<div style=\"max-width:34rem;margin:12vh auto;padding:32px;background:#fbfcfa;"
        "border:1px solid rgba(26,27,26,0.08);border-radius:18px;\">"
        "<h1 style=\"font-weight:400;font-size:1.6rem;margin:0 0 10px;\">Email preferences</h1>"
        f"<p style=\"color:#5c5e5a;line-height:1.6;margin:0 0 18px;\">{body}</p>"
        "<p style=\"color:#8b8d89;font-size:0.85rem;line-height:1.6;margin:0;\">"
        "You are still a member of the community, and nothing else has changed. "
        "You can turn the digest back on from your community preferences at any time.</p>"
        "</div></body>"
    )


@router.get("/badge")
async def badge(user: Optional[Dict[str, Any]] = Depends(asc_auth.get_current_user_optional)):
    """Unread badge for the portal side-panel Community item (PRD §4). Soft
    endpoint: a non-member gets ``eligible: false`` rather than an error, so
    the portal can decide whether to render the item at all."""
    if not user or not _passes_gate(user):
        return {"eligible": False, "unread": 0, "mentions": 0}
    cstore = _cstore()
    counts = cstore.unread_counts(user["id"], channels=visible_channels(member_map()))
    dm_unread = cstore.dm_unread_total(user["id"])
    return {
        "eligible": True,
        "unread": sum(c["unread"] for c in counts.values()) + dm_unread,
        "mentions": sum(c["mentions"] for c in counts.values()),
        "dm_unread": dm_unread,
        "channels": counts,
    }


# ─── Direct messages (user-requested extension beyond the v1 PRD scope) ───────
# Every DM shares the channel message pipeline — the same §7 PHI gate on every
# write, the same soft delete, the same audit trail (metadata only). The two
# invariants that make DMs private: participant-only access on every message-id
# path (``_require_message_access``), and targeted WS delivery
# (``hub.send_to_users``) so a DM never rides the broadcast.
def _dm_or_404(dm_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    dm = _cstore().get_dm(dm_id)
    if not _dm_access(user, dm):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return dm


def _room_title(dm: Dict[str, Any]) -> str:
    return (dm.get("title") or "").strip() or "Case room"


def _dm_summary(dm: Dict[str, Any], user_id: str,
                members: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    from community.system_posts import SYSTEM_MEMBER, SYSTEM_USER_ID  # noqa: PLC0415

    base = {
        "id": dm["id"],
        "last_message_id": dm.get("last_message_id"),
        "last_message_at": dm.get("last_message_at"),
        "unread": int(dm.get("unread") or 0),
    }
    if _is_case_room(dm):
        # A room is named by its case, and ``peer`` is populated with that name
        # rather than left out: every existing client renders a conversation by
        # its peer's display name, and a room with no peer would render as an
        # anonymous ghost in all of them.
        roster = [public_member(members.get(uid)) for uid
                  in (dm.get("participants") or _cstore().room_participants(dm["id"]))]
        title = _room_title(dm)
        return dict(base, kind=cstore_mod.CommunityStore.ROOM_KIND, title=title,
                    case_ref=dm.get("case_ref"),
                    participants=[m for m in roster if m],
                    peer=dict(SYSTEM_MEMBER, display_name=title, initials="CR"))
    peer_id = dm["user_b"] if dm["user_a"] == user_id else dm["user_a"]
    # The Archangel bot is a virtual author: never a users row, never in
    # member_map. ``_serialize_messages`` has always special-cased it for the
    # message AUTHOR; this is the same case one level up, for the conversation
    # PEER, and it was missing only because nothing had ever DM'd from the bot.
    # Without it a system DM lands in the doctor's inbox attributed to a ghost —
    # an anonymous, deleted-looking conversation telling them to go do work.
    peer = (dict(SYSTEM_MEMBER) if peer_id == SYSTEM_USER_ID
            else members.get(peer_id))
    return dict(base, kind="dm", peer=public_member(peer) or dict(_GHOST_MEMBER))


@router.get("/dms")
async def list_dms(user: Dict[str, Any] = Depends(require_member)):
    members = member_map()
    return {
        "dms": [
            _dm_summary(d, user["id"], members)
            for d in _cstore().list_dms_for(user["id"])
        ]
    }


@router.post("/dms")
async def open_dm(
    body: DmOpen,
    request: Request,
    user: Dict[str, Any] = Depends(require_verified_member),
):
    """Get-or-create the conversation with another member. The peer must pass
    the same §1 gate (verified contributor or staff) — you cannot DM an
    account that cannot itself enter the community."""
    if body.user_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot message yourself")
    members = member_map()
    if body.user_id not in members:
        raise HTTPException(status_code=404, detail="Member not found")
    cstore = _cstore()
    existed = cstore.get_or_create_dm(user["id"], body.user_id)
    # Re-read through list_dms_for so the summary carries unread/last-message.
    dm = next((d for d in cstore.list_dms_for(user["id"]) if d["id"] == existed["id"]), existed)
    if dm is existed:
        dm = dict(dm, unread=0, last_message_id=None, last_message_at=None)
    _audit(request, user, "community.dm_open", "ok", {"dm_id": existed["id"]})
    return _dm_summary(dm, user["id"], members)


@router.get("/dms/{dm_id}/messages")
async def dm_messages(
    dm_id: str,
    before: Optional[int] = Query(default=None, ge=1),
    after: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: Dict[str, Any] = Depends(require_member),
):
    dm = _dm_or_404(dm_id, user)
    msgs, has_more = _cstore().list_messages(
        dm["id"], before_id=before, after_id=after, limit=limit
    )
    return {
        "dm": dm["id"],
        "messages": _serialize_messages(msgs, member_map(), None, dm_id=dm["id"]),
        "has_more": has_more,
    }


@router.post(
    "/dms/{dm_id}/messages",
    dependencies=[Depends(rate_limiter("community_post", 30, 60))],
)
async def post_dm_message(
    dm_id: str,
    body: DmMessageIn,
    request: Request,
    user: Dict[str, Any] = Depends(require_verified_member),
):
    dm = _dm_or_404(dm_id, user)
    text = (body.body or "").strip()
    if not text and not body.attachment_ids:
        raise HTTPException(status_code=400, detail="Message is empty")

    # ── §7: the identical PHI gate — a 1:1 case discussion is exactly where
    # an identifier will eventually be pasted. BEFORE persistence/delivery. ──
    findings = phi_gate.scan_text(text)
    if findings:
        raise _phi_block(request, user, "message", findings, "dm")

    attachments = _resolve_attachments(body.attachment_ids, user)
    cstore = _cstore()
    msg = cstore.insert_message(
        channel_id=dm["id"],
        author_user_id=user["id"],
        body=text,
        parent_message_id=None,  # no threads inside DMs
        mentions=[],
        attachments=attachments,
    )
    _audit(request, user, "community.message_create", "ok", {
        "channel": "dm", "dm_id": dm["id"], "message_id": msg["id"],
        "attachments": len(attachments),
    })
    cstore.set_read(user["id"], dm["id"], msg["id"])

    # Everyone in the conversation except the author. On a two-party DM that is
    # the peer, exactly as before; on a room it is the rest of the case team, so
    # the room rides the DM unread and digest plumbing with no second path.
    participants = cstore.dm_participants(dm)
    for recipient_id in participants:
        if recipient_id != user["id"]:
            cnotify.enqueue_dm(cstore, recipient_id=recipient_id, message=msg)

    serialized = _serialize_messages([msg], member_map(), None, dm_id=dm["id"])[0]
    await hub.send_to_users(participants,
                            {"type": "message.created", "message": serialized})
    return serialized


@router.post("/dms/{dm_id}/read")
async def mark_dm_read(
    dm_id: str,
    body: ReadIn,
    user: Dict[str, Any] = Depends(require_member),
):
    dm = _dm_or_404(dm_id, user)
    _cstore().set_read(user["id"], dm["id"], body.last_read_message_id)
    return {"ok": True, "dm_unread": _cstore().dm_unread_total(user["id"])}


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
    members = member_map()
    # Visibility-scoped: hidden/deactivated channels are excluded from the
    # search SQL exactly like someone else's DMs.
    channels_by_id = {c["id"]: c for c in visible_channels(members)}
    my_dms = {d["id"]: d for d in cstore.list_dms_for(user["id"])}
    # §4: "search across messages the user can see" — the public channels plus
    # THEIR OWN direct messages, enforced in SQL (someone else's DM content can
    # never match).
    visible_ids = list(channels_by_id.keys()) + list(my_dms.keys())
    hits = cstore.search_messages(q, channel_ids=visible_ids)
    # Serialize per container group (batched reply/reaction lookups), then
    # restore the newest-first hit order.
    serialized: Dict[int, Dict[str, Any]] = {}
    by_container: Dict[str, List[Dict[str, Any]]] = {}
    for m in hits:
        by_container.setdefault(m["channel_id"], []).append(m)
    for cid, group in by_container.items():
        if cid in channels_by_id:
            rows = _serialize_messages(group, members, channels_by_id[cid]["slug"])
        else:
            rows = _serialize_messages(group, members, None, dm_id=cid)
        for s in rows:
            serialized[s["id"]] = s
    return {"query": q, "results": [serialized[m["id"]] for m in hits]}


# ─── Attachments (PRD §4, §7.4) ───────────────────────────────────────────────
@router.post(
    "/attachments",
    dependencies=[Depends(rate_limiter("community_upload", 10, 60))],
)
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_verified_member),
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
    if att.get("message_id"):
        # Bound to a message: gone with the message if it was deleted, and
        # subject to the message's visibility (a DM attachment is served only
        # to the conversation's participants).
        msg = _cstore().get_message(att["message_id"])
        if not msg or msg.get("deleted"):
            raise HTTPException(status_code=404, detail="Attachment not found")
        _require_message_access(user, msg)
    elif att.get("uploader_user_id") != user["id"]:
        # Uploaded but never posted: visible to its uploader only (audit
        # finding — a pending upload is not yet shared with the community).
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
    from community.system_posts import SYSTEM_USER_ID  # noqa: PLC0415

    members = member_map()
    flagged = _cstore().flagged_users(threshold=block_flag_threshold(),
                                      exclude_user_ids=[SYSTEM_USER_ID])
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


# ─── Portal side-panel integration (Side Panel PRD contract) ──────────────────
# The portal's persistent side panel (merged on main) polls two UNPREFIXED
# routes every 60s: ``GET /community/unread`` → {total} for the rail badge,
# and ``POST /community/handoff`` → {token} — a short-lived, single-use code
# it appends as ``/community?t=…`` so the new tab is signed in even when
# localStorage isn't shared. The community page redeems the code for a real
# Asclepius session via ``POST /api/community/handoff/redeem``.
page_router = APIRouter(tags=["community"])

_HANDOFF_TTL_SEC = 300  # outlives the portal's 60s refresh cadence
_handoff_tokens: Dict[str, tuple] = {}  # token -> (user_id, expires_monotonic)
_handoff_lock = __import__("threading").Lock()


def _mint_handoff(user_id: str) -> str:
    import secrets as _secrets
    import time as _time
    token = _secrets.token_urlsafe(24)
    now = _time.monotonic()
    with _handoff_lock:
        for t in [t for t, (_u, exp) in _handoff_tokens.items() if exp < now]:
            _handoff_tokens.pop(t, None)
        _handoff_tokens[token] = (user_id, now + _HANDOFF_TTL_SEC)
    return token


def _redeem_handoff(token: str) -> Optional[str]:
    import time as _time
    with _handoff_lock:
        entry = _handoff_tokens.pop(token, None)  # single use
    if not entry:
        return None
    user_id, expires = entry
    return user_id if _time.monotonic() <= expires else None


@page_router.get("/community/unread")
async def portal_unread(
    user: Optional[Dict[str, Any]] = Depends(asc_auth.get_current_user_optional),
):
    """Rail-badge total for the portal side panel. Soft for non-members
    (0, not an error) so the panel never surfaces a community error."""
    if not user or not _passes_gate(user):
        return {"total": 0}
    cstore = _cstore()
    counts = cstore.unread_counts(user["id"], channels=visible_channels(member_map()))
    return {
        "total": sum(c["unread"] for c in counts.values())
                 + cstore.dm_unread_total(user["id"]),
    }


@page_router.post("/community/handoff")
async def portal_handoff(user: Dict[str, Any] = Depends(require_member)):
    return {"token": _mint_handoff(user["id"]), "expires_in": _HANDOFF_TTL_SEC}


# ─── Admin Launch PRD §5.1 — redeeming an invite link ────────────────────────
# Unauthenticated by necessity: this is the URL in the physician's email client,
# and they may not have a session in that browser. Redeeming does exactly ONE
# thing — claim the community welcome — and then sends them to /community, which
# runs the ordinary §1 gate. It never mints a session and never grants access on
# its own, so a leaked link is worth a welcome post and nothing else.
@page_router.get(
    "/community/join/{token}",
    dependencies=[Depends(rate_limiter("community_invite_redeem", 30, 600))],
)
async def redeem_community_invite(token: str):
    from fastapi.responses import RedirectResponse  # noqa: PLC0415

    astore = _astore()
    token_hash = _hash_community_invite(token)
    invite = astore.get_community_invite(token_hash)
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    if not invite or str(invite.get("expires_at") or "") < now:
        # One message for "no such link" and "expired link". A distinguishing
        # error would let anyone probe which tokens ever existed.
        raise HTTPException(
            status_code=404,
            detail="That invite link is not valid any more. Ask your admin to resend it.")

    user = astore.get_user_by_id(invite["user_id"])
    if not user or (user.get("verification_status") or "") != "approved":
        # Re-checked at REDEMPTION, not only at send: a physician whose approval
        # was reversed between the two must not walk through the old link.
        raise HTTPException(
            status_code=403,
            detail="This account is not an approved contributor.")

    astore.redeem_community_invite(token_hash)
    # ``welcome_new_member`` is the one place that claims the welcome, and it
    # claims it through ``store.mark_community_welcomed`` — a guarded UPDATE that
    # returns whether THIS call won. That is what makes a concurrent approve +
    # invite safe, so it is called rather than reimplemented, and never replaced
    # with a read-then-write. Best-effort: a failed welcome post must not stop a
    # physician walking through their own link.
    try:
        from community.onboard import welcome_new_member  # noqa: PLC0415
        await welcome_new_member(user)
    except Exception:
        log.exception("[community] welcome failed on invite redemption (join stands)")
    return RedirectResponse(url="/community", status_code=303)


def _hash_community_invite(raw: str) -> str:
    """Must match ``asclepius_admin._hash_invite_token`` exactly — the sender
    stores this hash and the redeemer looks it up by it."""
    return hashlib.sha256((raw or "").strip().encode("utf-8")).hexdigest()


@router.post(
    "/handoff/redeem",
    dependencies=[Depends(rate_limiter("community_handoff", 10, 60))],
)
async def redeem_handoff(body: HandoffRedeem):
    """Exchange a portal handoff code for an Asclepius session. The code is
    single-use and short-lived; the §1 gate is re-checked at redemption."""
    uid = _redeem_handoff((body.token or "").strip())
    user = _astore().get_user_by_id(uid) if uid else None
    if not user or not _passes_gate(user):
        raise HTTPException(status_code=401, detail="Handoff expired — sign in through the portal.")
    return {"token": asc_auth.create_token(user)}


# ─── WebSocket (PRD §4, §6) ───────────────────────────────────────────────────
# Browser WebSockets cannot send an Authorization header, and putting the
# 7-day JWT in the URL would land it in access/proxy logs. So the client first
# exchanges its JWT (over a normal authenticated POST) for a SHORT-LIVED,
# SINGLE-USE ticket and connects with ``?ticket=``. ``?token=`` remains
# accepted for API clients; both paths run the identical §1 gate.
_WS_TICKET_TTL_SEC = 60
_ws_tickets: Dict[str, tuple] = {}  # ticket -> (user_id, expires_monotonic)
_ws_tickets_lock = __import__("threading").Lock()


def _mint_ws_ticket(user_id: str) -> str:
    import secrets as _secrets
    import time as _time
    ticket = _secrets.token_urlsafe(24)
    now = _time.monotonic()
    with _ws_tickets_lock:
        # opportunistic prune so the map cannot grow unboundedly
        for t in [t for t, (_u, exp) in _ws_tickets.items() if exp < now]:
            _ws_tickets.pop(t, None)
        _ws_tickets[ticket] = (user_id, now + _WS_TICKET_TTL_SEC)
    return ticket


def _redeem_ws_ticket(ticket: str) -> Optional[str]:
    import time as _time
    with _ws_tickets_lock:
        entry = _ws_tickets.pop(ticket, None)  # single use
    if not entry:
        return None
    user_id, expires = entry
    return user_id if _time.monotonic() <= expires else None


@router.post("/ws-ticket")
async def ws_ticket(user: Dict[str, Any] = Depends(require_member)):
    return {"ticket": _mint_ws_ticket(user["id"]), "expires_in": _WS_TICKET_TTL_SEC}


# Per-socket typing relay floor — a client cannot flood every member's socket
# with typing broadcasts (audit finding).
_TYPING_MIN_INTERVAL_SEC = 1.0


@router.websocket("/ws")
async def community_ws(websocket: WebSocket):
    """Real-time delivery. Auth = a single-use ``?ticket=`` (preferred) or a
    bearer ``?token=``; both are validated with the same store lookup + §1
    gate as every REST call. A failed gate closes with 4401/4403 and never
    joins the hub."""
    await websocket.accept()
    user = None
    ticket = websocket.query_params.get("ticket") or ""
    if ticket:
        uid = _redeem_ws_ticket(ticket)
        if uid:
            user = _astore().get_user_by_id(uid)
    else:
        payload = asc_auth.decode_token(websocket.query_params.get("token") or "")
        if payload:
            user = _astore().get_user_by_id(payload.get("sub", ""))
    if not user or not user.get("active"):
        await websocket.close(code=4401)
        return
    if not _passes_gate(user):
        await websocket.close(code=4403)
        return

    import time as _time

    members = member_map()
    me_member = members.get(user["id"]) or dict(_GHOST_MEMBER)
    first = await hub.connect(websocket, user["id"])
    last_typing_relay = 0.0
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
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "ping":
                await websocket.send_json({"type": "pong"})
            elif etype == "typing":
                now = _time.monotonic()
                if now - last_typing_relay < _TYPING_MIN_INTERVAL_SEC:
                    continue
                last_typing_relay = now
                name = me_member.get("display_name") or "Someone"
                dm_field = event.get("dm")
                if isinstance(dm_field, str) and dm_field.startswith("dm-"):
                    # DM typing relays ONLY inside the conversation: the peer on
                    # a two-party DM, the rest of the case team in a room.
                    dm = _cstore().get_dm(dm_field)
                    members_here = _cstore().dm_participants(dm) if dm else []
                    if dm and user["id"] in members_here:
                        await hub.send_to_users(
                            [u for u in members_here if u != user["id"]], {
                                "type": "typing", "dm": dm_field,
                                "user_id": user["id"], "name": name,
                            })
                    continue
                slug = str(event.get("channel") or "")[:64]
                thread_root = event.get("thread_root")
                await hub.broadcast({
                    "type": "typing",
                    "channel": slug,
                    "thread_root": thread_root if isinstance(thread_root, int) else None,
                    "user_id": user["id"],
                    "name": name,
                }, exclude=websocket)
    finally:
        last_of_user = await hub.disconnect(websocket)
        if last_of_user:
            await hub.broadcast({"type": "presence", "online": await hub.online_user_ids()})
