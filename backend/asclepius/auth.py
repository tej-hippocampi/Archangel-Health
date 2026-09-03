"""Standalone Asclepius auth (PRD §3, §7.1).

Email/password -> Asclepius JWT (HS256, signed with ``ASCLEPIUS_AUTH_SECRET``).
Completely independent of the clinical/landing/tenant auth planes: its own
secret, its own user table (``asclepius.db``), its own FastAPI dependencies.

Reuses ``PyJWT`` + ``passlib`` (already in requirements) — no new auth library.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, Header, HTTPException

import realm as _realm
from asclepius import capabilities as _caps
from asclepius.store import AsclepiusStore, get_store, verify_password

log = logging.getLogger("asclepius.auth")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
_PLACEHOLDER = "change-me-asclepius"
_MIN_SECRET_LEN = 16

_cached_secret: Optional[str] = None


def _is_production() -> bool:
    return (os.getenv("ENV") or "").strip().lower() == "production"


def get_asclepius_secret() -> str:
    """Resolve the signing secret. In production a strong ``ASCLEPIUS_AUTH_SECRET``
    is required; in dev we fall back to an ephemeral per-process secret so the
    portal works out of the box (mirrors ``auth_secret.get_auth_secret``)."""
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret
    raw = (os.getenv("ASCLEPIUS_AUTH_SECRET") or "").strip()
    strong = bool(raw) and raw != _PLACEHOLDER and len(raw) >= _MIN_SECRET_LEN
    if strong:
        _cached_secret = raw
        return _cached_secret
    if _is_production():
        raise RuntimeError(
            "ASCLEPIUS_AUTH_SECRET must be set to a strong (>=16 char) value in production."
        )
    if raw:
        _cached_secret = raw
        return _cached_secret
    _cached_secret = secrets.token_urlsafe(48)
    log.warning(
        "ASCLEPIUS_AUTH_SECRET is not set; generated an ephemeral per-process secret. "
        "Tokens will not survive a restart. Set ASCLEPIUS_AUTH_SECRET for stable sessions."
    )
    return _cached_secret


def create_token(user: Dict[str, Any]) -> str:
    now = datetime.utcnow()
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "typ": "asclepius",
        "sub": user["id"],
        "email": user["email"],
        "role": user["role"],
        "jti": uuid.uuid4().hex,
        # Issued-at, checked against users.password_changed_at on every request.
        # Without it a password reset changes only what the owner types next
        # time: an attacker holding a token keeps the account for the remaining
        # seven days, which is most of what a reset is supposed to stop.
        "iat": _epoch_utc(now),
        "exp": expire,
    }
    # Sandbox PRD §1.3: a token is born in a realm and only ever works there.
    _realm.stamp(payload)
    return jwt.encode(payload, get_asclepius_secret(), algorithm=ALGORITHM)


#: How long a media ticket is good for. Long enough to start a 73 MB video on a
#: slow connection and to scrub around inside it; short enough that one leaking
#: into a log or a referrer is worth nothing an hour later.
MEDIA_TICKET_TTL_MINUTES = 30


def create_media_ticket(user: Dict[str, Any], *, slot: str) -> str:
    """A short-lived, single-purpose token for a <video> element.

    A ``<video src>`` cannot carry an Authorization header, and the alternatives
    are both bad: fetch the whole file with a header and play it as a blob (which
    throws away the Range support that makes the timeline scrub, and puts 73 MB
    in the tab), or put the SESSION token in the query string (which writes a
    credential good for every endpoint into access logs, referrers and browser
    history).

    So: a token that can do exactly one thing. Different ``typ``, so
    ``decode_token`` refuses it and it cannot authenticate an API call; a
    ``slot`` claim, so a ticket for the onboarding demo cannot fetch anything
    else; and thirty minutes, so a leaked one expires before it is interesting.
    """
    now = datetime.utcnow()
    payload = {
        "typ": "asclepius_media",
        "sub": user["id"],
        "slot": slot,
        "iat": _epoch_utc(now),
        "exp": now + timedelta(minutes=MEDIA_TICKET_TTL_MINUTES),
    }
    _realm.stamp(payload)
    return jwt.encode(payload, get_asclepius_secret(), algorithm=ALGORITHM)


def decode_media_ticket(ticket: str, *, slot: str) -> Optional[str]:
    """The user id a media ticket names, or None. Never raises."""
    if not ticket:
        return None
    try:
        payload = jwt.decode(ticket, get_asclepius_secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    # Both checks matter: ``typ`` stops a session token being used as a ticket
    # (and vice versa), and ``slot`` stops a ticket for one asset opening another.
    if payload.get("typ") != "asclepius_media" or payload.get("slot") != slot:
        return None
    if not _realm.token_matches(payload):
        return None
    return payload.get("sub") or None


def _epoch_utc(dt: datetime) -> int:
    """Epoch seconds for a NAIVE datetime that is already UTC.

    ``datetime.utcnow()`` returns UTC wall-clock with no tzinfo, and calling
    ``.timestamp()`` on that reads it as LOCAL time. West of UTC that puts the
    claim in the future and PyJWT refuses the token; east of it the revocation
    comparison silently skews. Attach UTC explicitly instead of assuming.
    """
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _token_predates_password_change(payload: Dict[str, Any], user: Dict[str, Any]) -> bool:
    """True when this token was minted before the account's password last changed.

    Both halves fail open by design, and each for its own reason. A token with
    no ``iat`` predates this feature, and a user with no ``password_changed_at``
    has never used the chosen-password flow, which is every account that existed
    before it shipped. Treating either as suspect would log out the entire user
    base on deploy.
    """
    changed = (user.get("password_changed_at") or "").strip()
    iat = payload.get("iat")
    if not changed or not isinstance(iat, (int, float)):
        return False
    try:
        changed_at = datetime.fromisoformat(changed)
    except ValueError:
        return False
    # One second of slack: the stamp is written at second resolution, so a token
    # minted in the same second as the change (the reset endpoint signs the user
    # straight back in) must not invalidate itself.
    return float(iat) < (_epoch_utc(changed_at) - 1)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, get_asclepius_secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "asclepius":
        return None
    return payload


def authenticate(store: AsclepiusStore, email: str, password: str) -> Optional[Dict[str, Any]]:
    user = store.get_user_by_email(email)
    if not user or not user.get("active"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def _bank_link_rail() -> Dict[str, Any]:
    """Payments Rail §B4: tell the portal the rail is live, and only then.

    Absent while dark, rather than present-and-false. The lock on this build is
    that flag-off responses are byte-identical to the ones that shipped before
    the rail existed, and a key nobody reads is still a key a client can see.
    """
    from asclepius import constants as _constants          # noqa: PLC0415

    return {"bank_link_enabled": True} if _constants.stripe_enabled() else {}


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **_bank_link_rail(),
        "id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "specialty": user.get("specialty"),
        "board_cert": user.get("board_cert"),
        "years_experience": user.get("years_experience"),
        # PRD-B credential verification state: NULL (pre-verification-era account),
        # 'pending', 'approved' or 'rejected'. The portal needs this to explain the
        # wait — the Guide's `awaiting-verification` section is gated on
        # `showWhen: 'pending'` and could never match while this key was absent, so
        # the one screen written to tell a waiting physician what is happening was
        # unreachable for every user in every state. Display truth only; the gate
        # itself is get_current_user below.
        "verification_status": user.get("verification_status"),
        # V4 access gate (EHR PRD §9.5): the client uses this to show the
        # "V4 · Real Cases" box unlocked/locked. Serving is enforced server-side
        # regardless — this is display truth, not the gate itself.
        "real_data_approved": bool(user.get("real_data_approved")),
        # First-run tutorial state ("Calibration Case 1"). Display truth for the
        # client's launch decision; the transition rules that make completion
        # permanent live in PATCH /me/tutorial, not here.
        "tutorial": _parse_tutorial(user.get("tutorial_json")),
        # Advisor PRD §6.2: the portal decides which sections to render from
        # CAPABILITIES, not from a tier string it has to interpret. Shipping the
        # tier alone would push the two-state check into the frontend, which is
        # exactly the failure this build exists to remove. Display truth only —
        # every endpoint re-checks server-side.
        "tier": user.get("tier"),
        "tier_word": _caps.tier_word(user.get("tier")),
        "capabilities": sorted(_caps.granted(user)),
        # The second axis. ``capabilities`` keeps its exact current meaning
        # (what the TIER grants) so the portal's existing sessionCan() is
        # untouched; these two say what the ACCESS LEVEL grants, which is what
        # the rail needs to decide between hiding a surface and showing it
        # locked. verification_status is still shipped verbatim above: four
        # states must stay distinguishable for the admin queue even though the
        # gate only cares about three.
        "access_level": _caps.access_level(user),
        "surfaces": sorted(_caps.surfaces(user)),
        # Which door they came through. NULL is a physician. The portal uses
        # it to stop showing a referral-only account a rail full of locked
        # doors it will never open.
        "account_kind": _caps.account_kind(user),
        # Onboarding v2 §0.1: this account is signed in on a TEMPORARY password
        # from the welcome email, and the next thing it may do is choose its own.
        # Display truth for the portal's rotation screen; the flag itself is
        # retired server-side by ``set_user_password`` and by nothing else, so a
        # client that ignores this cannot skip the rotation — it can only skip
        # being asked politely.
        "must_change_password": bool(user.get("must_change_password")),
        # §6: the first-login walkthrough checklist, so the portal knows on the
        # first paint whether to open it. Server-side and not localStorage,
        # because doctors switch devices.
        "first_run": _first_run_public(user),
        # §6 stop 5: the payout rail. NULL until banking goes live.
        "bank_link_status": user.get("bank_link_status"),
        # The physician's own picture, or None until they upload one. The rail's
        # profile avatar needs this from the SESSION payload: it renders on every
        # screen, and /me/profile is fetched only when the profile page opens.
        # Same URL shape and same sha cache-buster as the profile page's avatar
        # block, so both surfaces hit one cached response rather than two.
        # Display truth only — the endpoint itself is bearer-authenticated.
        "avatar_url": _avatar_url(user),
    }


def _avatar_url(user: Dict[str, Any]) -> Optional[str]:
    sha = (user.get("avatar_asset_sha") or "").strip()
    if not sha:
        return None
    return f"/api/asclepius/users/{user['id']}/avatar?v={sha[:12]}"


def _first_run_public(user: Dict[str, Any]) -> Dict[str, Any]:
    """The walkthrough state as the portal reads it.

    A corrupt or stale-version blob degrades to the empty shape rather than
    raising, for the same reason ``_parse_tutorial`` does: the worst cost of a
    bad read is one extra walkthrough, and the worst cost of a raise is a
    physician who cannot open the portal.
    """
    from asclepius import first_run as asc_first_run  # noqa: PLC0415
    from asclepius.store import AsclepiusStore  # noqa: PLC0415

    version = AsclepiusStore.FIRST_RUN_VERSION
    empty = _project_first_run(asc_first_run.normalize(None, version=version))
    raw = user.get("first_run_json")
    if not raw:
        return empty
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return empty
    if not isinstance(parsed, dict) or \
            int(parsed.get("version") or 0) != version:
        return empty
    return _project_first_run(asc_first_run.normalize(parsed, version=version))


def _project_first_run(state: Dict[str, Any]) -> Dict[str, Any]:
    """The normalized state, minus the bookkeeping the portal has no use for.

    ``last_session_counted`` is a token id. It is an idempotency key for the
    server's own counter and nothing on screen reads it, so it does not go into
    a payload that is returned on every request and lands in every client's
    memory. ``sessions_seen`` DOES ship: it is the cadence clock, and
    ``first_run.js`` cannot choose between the re-entry page and the banner
    without it.
    """
    return {
        "version": state["version"],
        "stops": state["stops"],
        "sessions_seen": state["sessions_seen"],
        "completed_at": state["completed_at"],
        "dismissed_at": state["dismissed_at"],
    }


def _parse_tutorial(raw: Any) -> Dict[str, Any]:
    """The practice-case state, PROJECTED to what the physician may see.

    The stored blob carries ``score`` ({matched, total} against the four-item
    answer key) and the tour's saved ``step``. Neither belongs in a session
    payload: a physician sees no grade for their own work anywhere in this
    product, and shipping one here would put it in every /auth/me response
    whether or not a screen renders it. The admin reads the raw blob through
    the store.

    ``gate_state`` is lifted out of the nested gate object because the client
    launches the practice case off it, and a nested read in the boot path is
    one more thing to get wrong on a cached payload.
    """
    parsed: Dict[str, Any] = {}
    if raw:
        try:
            candidate = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(candidate, dict) and candidate.get("status"):
                parsed = candidate
        except (ValueError, TypeError):
            parsed = {}
    if not parsed:
        return {"status": "not_started", "version": None,
                "gate_state": _caps.GATE_LOCKED, "attempts": 0}

    gate = parsed.get("gate")
    gate = gate if isinstance(gate, dict) else {}
    try:
        attempts = int(gate.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return {
        "status": parsed.get("status"),
        "version": parsed.get("version"),
        "started_at": parsed.get("started_at"),
        "completed_at": parsed.get("completed_at"),
        "gate_state": _caps.practice_gate_state({"tutorial_json": parsed}),
        "attempts": attempts,
    }


# ─── FastAPI dependencies ─────────────────────────────────────────────────────

# Machine-readable companion to the credential-verification 403 below. Values are
# exactly the stored statuses: "pending" or "rejected".
AUTH_GATE_HEADER = "X-Asclepius-Auth-Gate"


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()


def get_current_user_optional(
    authorization: Optional[str] = Header(None),
) -> Optional[Dict[str, Any]]:
    token = _bearer(authorization)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    # Sandbox PRD §1.3 / §6.2: the token's realm must be the realm this request
    # runs in. The middleware routes on the claim, so this is belt-and-braces —
    # but it is the check that makes "a sandbox token can never touch live
    # stores" true even for a request that reached here some other way.
    if not _realm.token_matches(payload):
        return None
    user = get_store().get_user_by_id(payload.get("sub", ""))
    if not user or not user.get("active"):
        return None
    if _token_predates_password_change(payload, user):
        return None
    return user


def get_current_account(
    user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Identity and plane only. Says nothing about verification beyond "not
    refused".

    This is the dependency for everything a physician awaiting verification may
    still reach: their own profile, the practice case, the community, their own
    password. It is deliberately NOT the default, so a new endpoint that forgets
    to pick one is restrictive rather than open.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="Asclepius authentication required")
    # Deny-by-default (EHR PRD §4): a ``data_partner`` may use ONLY the locked-down
    # provider portal endpoints (``require_data_partner``). Since every evaluator /
    # admin / QA path depends on this, denying the role here excludes it from the
    # entire main API surface in one place — not by hiding buttons in the UI.
    if user.get("role") == "data_partner":
        raise HTTPException(
            status_code=403,
            detail="This account can only use the data provider upload portal.",
        )
    # A ``buyer`` may use ONLY the locked-down buyer workspace endpoints
    # (``require_buyer``). Denying it here excludes it from the entire evaluator /
    # admin / QA surface in one place (deny-by-default, same as data_partner).
    if user.get("role") == "buyer":
        raise HTTPException(
            status_code=403,
            detail="This account can only use the buyer data workspace.",
        )
    # A refused account is a final decision, not a wait. It gets nothing, and it
    # is the ONE state that never reaches any surface.
    if _caps.access_level(user) == _caps.NONE and user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="This account was not approved for the evaluator portal.",
            headers={AUTH_GATE_HEADER: "rejected"},
        )
    return user


def require_full_access(
    user: Dict[str, Any] = Depends(get_current_account),
) -> Dict[str, Any]:
    """Everything ``get_current_account`` refuses, plus: no provisional users.

    Aliased as ``get_current_user`` below, so the ~120 admin / QA / advisor /
    review endpoints that already depend on that name keep their exact current
    behavior and the restrictive option stays the default.
    """
    if _caps.access_level(user) == _caps.PROVISIONAL and user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail=(
                "We are still verifying your credentials. This opens as soon as "
                "that is done, usually within one to two business days."
            ),
            # The portal tells "still waiting" from "refused" apart to show the
            # right screen. Prose is not a protocol: matching on the detail
            # string would break the moment the copy is edited, so the state
            # travels in a header and `detail` keeps its shape for every
            # existing consumer.
            headers={AUTH_GATE_HEADER: "pending"},
        )
    return user


def require_surface(surface: str):
    """Dependency factory: authenticated, not refused, and allowed this surface.

    The policy lives in ``capabilities._BY_ACCESS``, so widening what a
    provisional physician may reach is one edit in one table rather than a hunt
    for every endpoint that happened to name a status string.
    """

    def _dep(user: Dict[str, Any] = Depends(get_current_account)) -> Dict[str, Any]:
        if not _caps.can_surface(user, surface):
            level = _caps.access_level(user)
            raise HTTPException(
                status_code=403,
                detail=(
                    "We are still verifying your credentials. This opens as soon "
                    "as that is done, usually within one to two business days."
                    if level == _caps.PROVISIONAL
                    else "This account was not approved for the evaluator portal."
                ),
                headers={AUTH_GATE_HEADER: ("pending" if level == _caps.PROVISIONAL else "rejected")},
            )
        return user

    return _dep


#: Back-compat alias. Every existing ``Depends(asc_auth.get_current_user)`` keeps
#: meaning "fully verified", so this change cannot quietly widen an endpoint that
#: was not reviewed as part of it.
get_current_user = require_full_access


def require_data_partner(
    user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Gate for the provider portal (EHR PRD §4). Admits ONLY a ``data_partner`` —
    an evaluator/admin/QA token is rejected here, and a ``data_partner`` is
    rejected everywhere else (see ``get_current_user``)."""
    if user is None:
        raise HTTPException(status_code=401, detail="Asclepius authentication required")
    if user.get("role") != "data_partner":
        raise HTTPException(status_code=403, detail="Data provider role required")
    return user


def require_buyer(
    user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """Gate for the buyer data workspace. Admits ONLY a ``buyer`` — an
    evaluator/admin/QA/data_partner token is rejected here, and a ``buyer`` is
    rejected everywhere else (see ``get_current_user``)."""
    if user is None:
        raise HTTPException(status_code=401, detail="Asclepius authentication required")
    if user.get("role") != "buyer":
        raise HTTPException(status_code=403, detail="Buyer role required")
    return user


def require_admin(
    user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def require_qa(
    user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    if user.get("role") not in ("admin", "qa_reviewer"):
        raise HTTPException(status_code=403, detail="QA reviewer or admin role required")
    return user


def ensure_admin_from_env(store: AsclepiusStore) -> Optional[Dict[str, Any]]:
    """Idempotently provision the operator-specified admin on every boot.

    ``seed_default_admin`` only fires when the user table is EMPTY, so setting
    ``ASCLEPIUS_ADMIN_EMAIL`` / ``ASCLEPIUS_ADMIN_PASSWORD`` after the portal has
    already booted once (and seeded demo/default users) silently has no effect —
    the account is never created and the operator is locked out.

    This closes that gap: whenever BOTH env vars are set, ensure that admin
    account exists with the given password (creating it, or resetting an existing
    account to role='admin', active, matching password). Runs in all
    environments — it is the supported way to (re)gain admin access. No-op when
    either env var is unset (nothing to provision)."""
    admin_email = (os.getenv("ASCLEPIUS_ADMIN_EMAIL") or "").strip().lower()
    admin_pw = os.getenv("ASCLEPIUS_ADMIN_PASSWORD")
    if not admin_email or not admin_pw:
        return None
    # ═══ Never take a working physician's account away from them ═══
    #
    # This runs on EVERY boot and forces role='admin'. Pointed at a doctor's
    # email it is not a one-time promotion, it is a standing override: the
    # console's own "set role" button appears to work, and the next deploy
    # silently undoes it. The physician also loses the real-case queue as a side
    # effect — real-data approval follows APPROVED + LABELING, and an account
    # sitting at role='admin' is not being verified as a labeler — so the visible
    # symptom is an empty V4 queue with nothing on screen connecting it to an
    # environment variable nobody was looking at.
    #
    # So a deliberate physician account wins over the env var, and the refusal is
    # logged at ERROR naming the fix. The one exception is lockout: if this is the
    # last way into the console, the bootstrap still runs, because an operator
    # with no admin cannot repair anything. That check is a real query, not an
    # assumption.
    existing = store.get_user_by_email(admin_email)
    if existing and (existing.get("role") or "") == "evaluator":
        others = store.count_active_admins(excluding=existing.get("id"))
        if others > 0:
            log.error(
                "Asclepius: ASCLEPIUS_ADMIN_EMAIL names '%s', which is a PHYSICIAN "
                "account (role=evaluator). Refusing to convert it to an admin — "
                "doing so on every boot would revert the role set in the console "
                "and keep this doctor out of the real-case queue. %d other active "
                "admin(s) exist, so console access is not at risk. Point "
                "ASCLEPIUS_ADMIN_EMAIL at a separate operations account.",
                admin_email, others,
            )
            return None
        log.error(
            "Asclepius: ASCLEPIUS_ADMIN_EMAIL names '%s', which is a PHYSICIAN "
            "account — but it is the ONLY active admin, so it is being promoted "
            "to avoid locking the console out entirely. Create a separate "
            "operations admin, then repoint ASCLEPIUS_ADMIN_EMAIL at it so this "
            "doctor can go back to labeling.",
            admin_email,
        )
    admin = store.ensure_admin(email=admin_email, password=admin_pw)
    log.warning(
        "Asclepius: ensured admin account '%s' from ASCLEPIUS_ADMIN_EMAIL/"
        "ASCLEPIUS_ADMIN_PASSWORD. Rotate the password after logging in.",
        admin_email,
    )
    return admin


# ─── Mock / sandbox contributor (internal demo tool) ──────────────────────────
# A stable, credentialed evaluator account an operator can log into on the LIVE
# portal to exercise the latest flow (V3, multimodal cases, …). Its submissions
# are HARD-EXCLUDED from real exports by default and labeled in the admin, so a
# demo never contaminates a shipped training batch. Enabled in all environments
# (it is a safe, isolated sandbox) but can be turned off with
# ASCLEPIUS_MOCK_ENABLED=0. Credentials are env-overridable.
_MOCK_DEFAULT_ID = "mockadmin"
_MOCK_DEFAULT_PASSWORD = "MockContributor-2026"


def mock_enabled() -> bool:
    return (os.getenv("ASCLEPIUS_MOCK_ENABLED", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def mock_credentials() -> Dict[str, Any]:
    """Resolve the mock contributor's login + display profile (env-overridable).

    The login is a plain USERNAME/ID (default ``mockadmin``), not an email —
    ``ASCLEPIUS_MOCK_ID`` sets it (``ASCLEPIUS_MOCK_EMAIL`` still honored for
    back-compat). The portal login accepts a username or an email, so you sign in
    with just the id + password. Stored in the identity column like any login."""
    login_id = (os.getenv("ASCLEPIUS_MOCK_ID")
                or os.getenv("ASCLEPIUS_MOCK_EMAIL")
                or _MOCK_DEFAULT_ID).strip().lower()
    return {
        "enabled": mock_enabled(),
        "email": login_id,   # the login identifier (username or email)
        "password": os.getenv("ASCLEPIUS_MOCK_PASSWORD") or _MOCK_DEFAULT_PASSWORD,
        "specialty": (os.getenv("ASCLEPIUS_MOCK_SPECIALTY") or "nephrology").strip().lower(),
        "board_cert": os.getenv("ASCLEPIUS_MOCK_BOARD_CERT") or "board_certified_nephrology",
        "years_experience": _mock_years(),
        # BUG-6: the mock/demo account's organization is explicitly "mockadmin"
        # (env-overridable) so its labeled records group under a real org name in
        # Exports/Metrics instead of falling into the ungrouped bucket.
        "organization": os.getenv("ASCLEPIUS_MOCK_ORG") or "mockadmin",
    }


def _mock_years() -> int:
    try:
        return int(os.getenv("ASCLEPIUS_MOCK_YEARS", "12"))
    except (ValueError, TypeError):
        return 12


_MOCK_PROD_FIX = ("Set ASCLEPIUS_MOCK_PASSWORD to a private value to run the sandbox "
                  "on production (this also unlocks the V4 real-case demo), or set "
                  "ASCLEPIUS_MOCK_ENABLED=0 to turn it off entirely.")


def _refuse_default_password_mock_in_production(store: AsclepiusStore, login_id: str) -> None:
    """Production boot with the published default password: leave no usable login.

    Two states to handle, and only refusing the first would be half a fix. A
    fresh production deployment simply never gets the account. A deployment that
    has booted before already has the row on disk WITH the default password,
    because ``ensure_mock_user`` reset it on every previous boot; walking past it
    would leave the exact credential this refusal exists to remove.

    The rotation is one-way on purpose. The new secret is generated here, handed
    straight to the store and never logged, so nobody (including us) can sign in
    as the sandbox on production. Recovering the account means setting
    ASCLEPIUS_MOCK_PASSWORD, which is the supported path anyway.
    """
    existing = store.get_user_by_email(login_id)
    if not existing:
        log.error(
            "Asclepius: REFUSING to provision the MOCK contributor '%s' in production: "
            "it would be a login-capable physician account guarded by a password "
            "published in this repo. %s",
            login_id, _MOCK_PROD_FIX,
        )
        return
    store.ensure_mock_user(
        email=login_id,
        # Never logged, never returned, not derived from anything guessable.
        password=secrets.token_urlsafe(32),
        specialty=existing.get("specialty"),
        board_cert=existing.get("board_cert"),
        years_experience=existing.get("years_experience"),
        organization=existing.get("organization"),
        real_data_approved=False,
    )
    log.error(
        "Asclepius: MOCK contributor '%s' already existed in production under the "
        "published default password. Its password has been rotated to an unrecoverable "
        "random value and its real-case access revoked; the account can no longer be "
        "signed into. %s",
        login_id, _MOCK_PROD_FIX,
    )


def ensure_mock_contributor(store: AsclepiusStore) -> Optional[Dict[str, Any]]:
    """Idempotently provision the mock/sandbox contributor on every boot (no-op
    when ASCLEPIUS_MOCK_ENABLED=0). Safe in production: the account is isolated
    (is_mock=1) and its data never ships in a default export.

    V4 (real patient cases) access: the sandbox is V4-approved ONLY when its
    password is not the known default in production. An out-of-the-box prod
    deployment must never expose real de-identified cases behind published
    credentials (security review); set ASCLEPIUS_MOCK_PASSWORD to a private
    value to unlock the V4 demo on prod. Dev/staging stays unlocked.

    PRODUCTION + DEFAULT PASSWORD: no login-capable account at all. The V4 gate
    above keeps real patient cases away from this account, but it was never the
    whole exposure. The account still authenticates on the live portal with a
    password published in the repo, in ``demo_credentials``, and in the admin
    console. What it reaches once signed in, confirmed against the code rather
    than assumed: role=evaluator with tier='labeler', so it draws from the
    SYNTHETIC task queue and submits labels; the practice-case gate and the
    physician-agreement gate both exempt it explicitly on ``is_mock``; it appears
    in the contributor directory; and it holds a BROWSE-level ``/me/*`` surface.
    It does NOT reach real patient cases (the V4 gate), the contributor community
    (``community.router._passes_gate`` needs a verified vault row it has none of),
    default exports, the physician roster, allocation or weekly metrics. So the
    blast radius is a physician-shaped session on our production portal for
    anyone who has read the source, not a PHI breach, which is why this is a
    same-night fix rather than an incident.

    So in ``ENV=production`` with no ``ASCLEPIUS_MOCK_PASSWORD``:
      * if the account does not exist, it is NOT created (loud ERROR, no
        silently-weakened variant);
      * if a previous boot already created it, its published password is
        rotated to an unguessable value that is never logged and never
        recoverable, because refusing to touch the row would leave exactly the
        live default-credential login this fix exists to remove. The row itself
        stays, so its historic sandbox submissions, directory entry and export
        exclusion keep resolving.
    Either way this returns None: no usable sandbox account came out of it.

    Dev and staging are untouched: the sandbox is genuinely useful there, and
    neither serves real patients. Production operators who want the demo set
    ASCLEPIUS_MOCK_PASSWORD (also the switch that unlocks V4), and operators who
    want it gone everywhere set ASCLEPIUS_MOCK_ENABLED=0."""
    cfg = mock_credentials()
    if not cfg["enabled"]:
        return None
    custom_password = bool(os.getenv("ASCLEPIUS_MOCK_PASSWORD"))
    if _is_production() and not custom_password:
        _refuse_default_password_mock_in_production(store, cfg["email"])
        return None
    v4_ok = custom_password or not _is_production()
    user = store.ensure_mock_user(
        email=cfg["email"], password=cfg["password"], specialty=cfg["specialty"],
        board_cert=cfg["board_cert"], years_experience=cfg["years_experience"],
        organization=cfg["organization"], real_data_approved=v4_ok,
    )
    log.warning(
        "Asclepius: ensured MOCK contributor '%s' (sandbox; data hard-excluded from "
        "exports; V4 real-case demo %s). Disable with ASCLEPIUS_MOCK_ENABLED=0.",
        cfg["email"], "UNLOCKED" if v4_ok else "LOCKED (set ASCLEPIUS_MOCK_PASSWORD to unlock)",
    )
    return user


def seed_default_admin(store: AsclepiusStore) -> Optional[Dict[str, Any]]:
    """Create a bootstrap admin (and, outside production, a demo evaluator) on
    first boot if the user table is empty.

    Production hardening (FIX 4): in ``ENV=production`` we NEVER seed known
    default credentials. The bootstrap admin is created only when BOTH
    ``ASCLEPIUS_ADMIN_EMAIL`` and ``ASCLEPIUS_ADMIN_PASSWORD`` are explicitly set;
    otherwise we skip seeding with a clear warning. The demo evaluator is never
    seeded in production (``ASCLEPIUS_SEED_DEMO_EVALUATOR`` is ignored there)."""
    if store.count_users() > 0:
        return None

    admin_email = (os.getenv("ASCLEPIUS_ADMIN_EMAIL") or "").strip().lower()
    admin_pw = os.getenv("ASCLEPIUS_ADMIN_PASSWORD")

    if _is_production():
        if not admin_email or not admin_pw:
            log.warning(
                "Asclepius: skipping bootstrap admin seed in production because "
                "ASCLEPIUS_ADMIN_EMAIL and/or ASCLEPIUS_ADMIN_PASSWORD are not set. "
                "Create the first admin explicitly (no default credentials are seeded in prod)."
            )
            return None
        admin = store.create_user(email=admin_email, password=admin_pw, role="admin")
        log.warning(
            "Asclepius: seeded bootstrap admin '%s' from explicit env credentials; "
            "rotate the password after first login.",
            admin_email,
        )
        return admin

    # Non-production: dev/demo convenience defaults (logged as a warning).
    admin_email = admin_email or "admin@asclepius.local"
    admin_pw = admin_pw or "asclepius-admin-2026"
    admin = store.create_user(email=admin_email, password=admin_pw, role="admin")
    log.warning(
        "Asclepius: seeded bootstrap admin '%s' (dev default). Set ASCLEPIUS_ADMIN_EMAIL / "
        "ASCLEPIUS_ADMIN_PASSWORD and rotate immediately.",
        admin_email,
    )
    # A demo evaluator makes the eval screen usable immediately in local/demo.
    if os.getenv("ASCLEPIUS_SEED_DEMO_EVALUATOR", "1").strip().lower() in ("1", "true", "yes", "on"):
        try:
            demo = store.create_user(
                email=(os.getenv("ASCLEPIUS_DEMO_EVALUATOR_EMAIL") or "evaluator@asclepius.local"),
                password=(os.getenv("ASCLEPIUS_DEMO_EVALUATOR_PASSWORD") or "asclepius-eval-2026"),
                role="evaluator",
                specialty=(os.getenv("ASCLEPIUS_DEMO_EVALUATOR_SPECIALTY") or "nephrology"),
                board_cert="board_certified_nephrology",
                years_experience=12,
                organization="Riverside Nephrology Associates",
                # "makes the eval screen usable immediately" is only true if the
                # account can draw a task: LABEL is enforced at /tasks/next.
                tier="labeler",
            )
            _seed_demo_contributors(store, demo)
        except Exception:
            log.warning("Asclepius: failed to seed demo evaluator", exc_info=True)
    return admin


def _seed_demo_contributors(store: AsclepiusStore, demo_evaluator: Dict[str, Any]) -> None:
    """Populate the Contributors view in dev/demo: credential profiles (Tier A
    ship + Tier B vault) across two organizations so the org → contributor →
    profile drill-down and the tiered export are demonstrable out of the box.
    Never runs in production (only called from the dev branch above)."""
    # The demo evaluator becomes a fully credentialed nephrologist.
    store.upsert_contributor_credentials(
        id_hashed=demo_evaluator["id_hashed"],
        user_id=demo_evaluator["id"],
        organization="Riverside Nephrology Associates",
        role_title="Physician (MD)",
        credentials_verified=True,
        ship={
            "degree": "MD",
            "board_certifications": "ABIM — Internal Medicine; Nephrology (active)",
            "primary_specialty": "nephrology",
            "subspecialties": ["dialysis", "transplant", "CKD"],
            "years_in_active_practice": 17,
            "active_practice": True,
            "practice_setting_type": "private_practice",
            "languages": ["English", "Spanish"],
            "fellowship_trained": True,
            "fellowship_summary": "fellowship-trained in nephrology at a major US academic medical center",
            "credentials_verified": True,
        },
        verify={
            "full_legal_name": "Jane A. Doe, MD",
            "npi": "1234567893",
            "medical_license_number": "A-104872",
            "license_state": "CA",
            "medical_school": "University of California, San Francisco",
            "medical_school_year": "2004",
            "residency": "Stanford University Medical Center",
            "residency_year": "2007",
            "fellowship": "UCLA Medical Center — Nephrology",
            "fellowship_year": "2009",
            "practice_name": "Riverside Nephrology Associates",
            "practice_address": "1200 Riverside Dr, Suite 300, Sacramento, CA 95814",
            "practice_contact": "jdoe@riversidenephrology.example",
        },
    )

    extra = [
        {
            "email": "npaul.np@asclepius.local",
            "specialty": "nephrology",
            "organization": "Riverside Nephrology Associates",
            "role_title": "Nurse Practitioner",
            "verified": True,
            "ship": {
                "degree": "DNP",
                "board_certifications": "AANP — Adult-Gerontology Acute Care NP (active)",
                "primary_specialty": "nephrology",
                "subspecialties": ["dialysis", "CKD"],
                "years_in_active_practice": 9,
                "active_practice": True,
                "practice_setting_type": "dialysis_unit",
                "languages": ["English"],
                "fellowship_trained": False,
                "credentials_verified": True,
            },
            "verify": {
                "full_legal_name": "Nadia Paul, DNP, AGACNP-BC",
                "npi": "1982736450",
                "medical_license_number": "NP-55821",
                "license_state": "CA",
                "medical_school": "Johns Hopkins School of Nursing",
                "medical_school_year": "2015",
                "practice_name": "Riverside Nephrology Associates",
                "practice_address": "1200 Riverside Dr, Suite 300, Sacramento, CA 95814",
            },
        },
        {
            "email": "rkhan.do@asclepius.local",
            "specialty": "nephrology",
            "organization": "Lakeside Kidney Institute",
            "role_title": "Physician (DO)",
            "verified": True,
            "ship": {
                "degree": "DO",
                "board_certifications": "AOBIM — Nephrology (active)",
                "primary_specialty": "nephrology",
                "subspecialties": ["transplant", "glomerular disease"],
                "years_in_active_practice": 22,
                "active_practice": True,
                "practice_setting_type": "academic",
                "languages": ["English", "Urdu"],
                "fellowship_trained": True,
                "fellowship_summary": "fellowship-trained in transplant nephrology at a major US academic medical center",
                "credentials_verified": True,
            },
            "verify": {
                "full_legal_name": "Rashid Khan, DO",
                "npi": "1457893021",
                "medical_license_number": "D-220194",
                "license_state": "IL",
                "medical_school": "Chicago College of Osteopathic Medicine",
                "medical_school_year": "1999",
                "residency": "Rush University Medical Center",
                "residency_year": "2002",
                "fellowship": "Northwestern Memorial Hospital — Transplant Nephrology",
                "fellowship_year": "2004",
                "practice_name": "Lakeside Kidney Institute",
                "practice_address": "55 Lakeshore Ave, Chicago, IL 60611",
            },
        },
    ]
    for c in extra:
        try:
            u = store.create_user(
                email=c["email"],
                password=secrets.token_urlsafe(24),
                role="evaluator",
                specialty=c["specialty"],
                organization=c["organization"],
            )
            store.upsert_contributor_credentials(
                id_hashed=u["id_hashed"],
                user_id=u["id"],
                organization=c["organization"],
                role_title=c["role_title"],
                credentials_verified=c["verified"],
                ship=c["ship"],
                verify=c["verify"],
            )
        except Exception:
            log.warning("Asclepius: failed to seed demo contributor %s", c["email"], exc_info=True)
