"""The realm — ``live`` or ``sandbox`` — every request and background pass runs in.

Sandbox PRD §0–§1. Isolation is a BOUNDARY, not a filter: the sandbox realm
routes to physically separate SQLite files, asset directories and an email
outbox, so nothing a sandbox account does can appear anywhere a real user or the
real admin looks. There is no query that can cross because there is no shared
table.

The four stores used to pick their file from an env var at process start. This
module makes that choice request-scoped: a ``ContextVar`` holds the realm, the
store accessors (``asclepius.store.get_store``, ``community.store
.get_community_store``, ``team_store.get_team_store``, ``asclepius.assets
._store_root``) key on it, and the middleware in ``main`` sets it once per
request from the token's ``realm`` claim (or, on unauthenticated entry points,
the ``X-Asclepius-Realm`` header).

Sandbox paths are DERIVED from the live ones and never configurable on their
own (§1.1, §7): ``<name>_sandbox.db`` beside each live DB, ``<root>/sandbox/``
for directory stores. One environment, two realms — production and sandbox
cannot drift in config, which is the point.
"""

from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar, Token
from typing import Any, Callable, Dict, Iterator, Optional

REALMS = ("live", "sandbox")
LIVE = "live"
SANDBOX = "sandbox"

#: Request header consulted on unauthenticated entry points only (§1.3).
HEADER = "X-Asclepius-Realm"
#: JWT claim carried by every token minted in a realm (§1.3).
CLAIM = "realm"

#: The one variable that turns the realm on (§7). Unset → every ``/sandbox/*``
#: route 404s and the header is ignored, so the feature is dark until an
#: operator sets it and goes dark again the moment they delete it.
ADMIN_PASSWORD_VAR = "ASCLEPIUS_SANDBOX_ADMIN_PASSWORD"
DOCTOR_PASSWORD_VAR = "ASCLEPIUS_SANDBOX_DOCTOR_PASSWORD"

_current: ContextVar[str] = ContextVar("realm", default=LIVE)


class RealmError(ValueError):
    """An unknown realm name."""


def validate(realm: Any) -> str:
    r = (realm or "").strip().lower() if isinstance(realm, str) else ""
    if r not in REALMS:
        raise RealmError(f"unknown realm {realm!r}; expected one of {REALMS}")
    return r


def current() -> str:
    """The realm this request / pass is running in. Defaults to ``live``."""
    return _current.get()


def is_sandbox() -> bool:
    return _current.get() == SANDBOX


def set_for_request(realm: str) -> Token:
    """Middleware only. Returns the token to hand back to :func:`reset`."""
    return _current.set(validate(realm))


def reset(token: Token) -> None:
    _current.reset(token)


@contextlib.contextmanager
def scoped(realm: str) -> Iterator[str]:
    """Run a block in ``realm`` — for background loops, the seed script and
    tests. Requests never use this; the middleware sets the realm once."""
    token = set_for_request(realm)
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def enabled() -> bool:
    """Is the sandbox realm switched on for this deployment (§7)?"""
    return bool((os.getenv(ADMIN_PASSWORD_VAR) or "").strip())


def admin_password() -> str:
    return (os.getenv(ADMIN_PASSWORD_VAR) or "").strip()


def doctor_password() -> str:
    return (os.getenv(DOCTOR_PASSWORD_VAR) or "").strip()


# ─── Path derivation ─────────────────────────────────────────────────────────
#
# The LIVE resolution rules below are copied from the stores they describe
# (``AsclepiusStore.__init__``, ``CommunityStore.__init__``, ``TeamStore
# .__init__``, ``asclepius.constants.asset_store``, ``asclepius.export
# .export_root``, ``asclepius.ingestion._default_ingest_dir``) and the stores
# now call back into this module, so there is exactly one place that knows
# where a file lives. Tests assert the sandbox side is derived, never read
# from its own variable.

def _backend_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def live_asclepius_db() -> str:
    return os.getenv("ASCLEPIUS_DB_PATH") or os.path.join(_backend_dir(), "asclepius.db")


def live_community_db() -> str:
    return os.getenv("COMMUNITY_DB_PATH") or os.path.join(_backend_dir(), "community.db")


def live_team_db() -> str:
    return os.getenv("TEAM_DB_PATH") or os.path.join(_backend_dir(), "team.db")


def live_asset_root() -> str:
    explicit = os.getenv("ASCLEPIUS_ASSET_STORE", "").strip()
    if explicit:
        return explicit
    data_dir = os.getenv("ASCLEPIUS_DATA_DIR", "").strip()
    if data_dir:
        return os.path.join(data_dir, "assets")
    db_path = os.getenv("ASCLEPIUS_DB_PATH", "").strip()
    if db_path:
        return os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", "asclepius_assets")
    return os.path.join(_backend_dir(), "asclepius", "_assetstore")


def live_export_root() -> str:
    return os.getenv("ASCLEPIUS_EXPORT_DIR") or "/tmp/asclepius-exports"


def live_ingest_root() -> str:
    explicit = os.getenv("ASCLEPIUS_INGEST_DIR")
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(os.path.abspath(live_asclepius_db())), "asclepius-ingest")


def sandbox_db_path(live_path: str) -> str:
    """``/data/asclepius.db`` → ``/data/asclepius_sandbox.db``. Same directory,
    so it lands on the same persistent volume automatically."""
    stem, ext = os.path.splitext(live_path)
    return f"{stem}_sandbox{ext or '.db'}"


def sandbox_dir_path(live_root: str) -> str:
    """``<root>`` → ``<root>/sandbox``. Inside the live root on purpose: the
    content-addressed asset layout is ``<root>/<2 hex>/<sha>``, so a directory
    named ``sandbox`` can never collide with a fan-out bucket."""
    return os.path.join(live_root.rstrip("/\\") or live_root, SANDBOX)


def paths(realm: Optional[str] = None) -> Dict[str, str]:
    """Every file-backed store for ``realm`` (default: the current one)."""
    r = validate(realm) if realm is not None else current()
    live = {
        "asclepius": live_asclepius_db(),
        "community": live_community_db(),
        "team": live_team_db(),
        "assets": live_asset_root(),
        "exports": live_export_root(),
        "ingest": live_ingest_root(),
    }
    if r == LIVE:
        return live
    return {
        "asclepius": sandbox_db_path(live["asclepius"]),
        "community": sandbox_db_path(live["community"]),
        "team": sandbox_db_path(live["team"]),
        "assets": sandbox_dir_path(live["assets"]),
        "exports": sandbox_dir_path(live["exports"]),
        "ingest": sandbox_dir_path(live["ingest"]),
    }


# ─── Realm proxy ─────────────────────────────────────────────────────────────
class RealmProxy:
    """Forwards every attribute to the current realm's store (§1.2).

    ``main`` used to pin one ``TeamStore()`` at import and reference it 137
    times; rewriting those call sites is how a realm leak gets introduced by
    the 138th. Instead the pinned name becomes this proxy and every existing
    ``_team_store.foo()`` resolves the realm at call time.
    """

    __slots__ = ("_getter", "_label")

    def __init__(self, getter: Callable[[], Any], label: str = "store") -> None:
        object.__setattr__(self, "_getter", getter)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_getter")(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_getter")(), name, value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        label = object.__getattribute__(self, "_label")
        return f"<RealmProxy {label} realm={current()}>"


# ─── The one sanctioned live read from the sandbox (§4) ──────────────────────
@contextlib.contextmanager
def read_live() -> Iterator[Any]:
    """Open the LIVE asclepius store read-only while running in the sandbox.

    The snapshot copy (``POST /sandbox/copy-health-system``) is the only place
    in the codebase permitted to open both realms' stores in one request, and a
    test greps for this call to keep it that way. The live connection is opened
    with ``?mode=ro`` so live rows are never written — a second test asserts the
    URI. Refuses to run outside the sandbox: there is no reason for live code
    to reach across, and a live→live "snapshot" would be a no-op that hides a
    realm bug.
    """
    if not is_sandbox():
        raise RealmError("read_live() may only be called from the sandbox realm")
    from asclepius.store import AsclepiusStore, bound_db_path  # noqa: PLC0415 — avoid import cycle

    store = AsclepiusStore(db_path=bound_db_path(LIVE), read_only=True)
    try:
        yield store
    finally:
        pass


# ─── Token claim helpers (§1.3) ──────────────────────────────────────────────
def stamp(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add the ``realm`` claim to a JWT payload being minted. Every token
    plane (Asclepius session, media ticket, HS portal cookie, landing auth,
    tenant staff) calls this so a token always says where it was born."""
    payload[CLAIM] = current()
    return payload


def token_realm(payload: Optional[Dict[str, Any]]) -> str:
    """The realm a decoded token belongs to. Tokens minted before this claim
    existed are live tokens — the sandbox did not exist when they were made."""
    if not payload:
        return LIVE
    try:
        return validate(payload.get(CLAIM) or LIVE)
    except RealmError:
        return LIVE


def token_matches(payload: Optional[Dict[str, Any]]) -> bool:
    """§1.3: a token's realm always wins. A token authenticates ONLY in the
    realm it was minted in — a sandbox token can never touch live stores and a
    live token can never touch sandbox ones. Every auth dependency checks this
    after verifying the signature, so it holds even if a request somehow
    reached a handler without the middleware."""
    return token_realm(payload) == current()


def _peek_claim(token: str) -> Optional[str]:
    """The ``realm`` claim of a JWT WITHOUT verifying it. Used by the middleware
    to pick which realm's store a request will consult; it grants nothing — the
    auth dependency verifies the signature against that realm's users and a
    forged claim simply fails there. Returns None for anything that is not a
    JWT carrying the claim (legacy tokens, opaque tokens, garbage)."""
    if not token:
        return None
    try:
        import jwt  # noqa: PLC0415

        payload = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except Exception:
        return None
    raw = payload.get(CLAIM) if isinstance(payload, dict) else None
    if raw is None:
        return None
    try:
        return validate(raw)
    except RealmError:
        return None


#: Path prefixes that ARE the sandbox: the SPA aliases and the sandbox router.
SANDBOX_PATH_PREFIXES = ("/sandbox", "/api/asclepius/sandbox")
#: Cookie the health-system portal keeps its session in (mirrors
#: ``routers.asclepius_provider._HS_COOKIE``; asserted equal by a test).
HS_COOKIE = "hs_portal_session"


def _is_sandbox_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in SANDBOX_PATH_PREFIXES)


def resolve_for_request(*, path: str, header: Optional[str], token_claim: Optional[str]):
    """Decide a request's realm. Returns ``(realm, error)`` where ``error`` is
    ``None`` or ``(status, code)``. Pure so the rules are unit-testable:

      * sandbox OFF → everything is live; ``/sandbox/*`` is 404; the header is
        ignored (§7);
      * a ``/sandbox/*`` path IS the sandbox;
      * a token's claim wins over the header and over the path — a mismatch is
        401 ``realm_mismatch`` (§1.3, §6.2);
      * otherwise the header, defaulting to live.
    """
    wants_sandbox_path = _is_sandbox_path(path)
    if not enabled():
        if wants_sandbox_path:
            return LIVE, (404, "sandbox_disabled")
        return LIVE, None
    hdr: Optional[str] = None
    if header is not None and header.strip():
        try:
            hdr = validate(header)
        except RealmError:
            return LIVE, (400, "unknown_realm")
    implied = SANDBOX if wants_sandbox_path else hdr
    if token_claim is not None:
        if implied is not None and implied != token_claim:
            return token_claim, (401, "realm_mismatch")
        return token_claim, None
    return implied or LIVE, None


class RealmMiddleware:
    """Pure-ASGI (so the ContextVar is visible to the route handler and to the
    background tasks that run inside the response). Sets the realm once per
    HTTP request from, in order of authority: the bearer / HS-cookie token's
    ``realm`` claim, the ``/sandbox/*`` path, the ``X-Asclepius-Realm`` header.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        header = None
        bearer = None
        cookie_blob = ""
        for key, val in scope.get("headers", []) or []:
            if key == b"x-asclepius-realm":
                header = val.decode("latin-1", "ignore")
            elif key == b"authorization":
                raw = val.decode("latin-1", "ignore")
                if raw.lower().startswith("bearer "):
                    bearer = raw.split(" ", 1)[1].strip()
            elif key == b"cookie":
                cookie_blob += ("; " if cookie_blob else "") + val.decode("latin-1", "ignore")
        claim = _peek_claim(bearer) if bearer else None
        if claim is None and cookie_blob:
            try:
                from http.cookies import SimpleCookie  # noqa: PLC0415

                jar = SimpleCookie()
                jar.load(cookie_blob)
                morsel = jar.get(HS_COOKIE)
                if morsel and morsel.value:
                    claim = _peek_claim(morsel.value)
            except Exception:
                claim = None
        r, error = resolve_for_request(path=scope.get("path") or "", header=header, token_claim=claim)
        if error is not None:
            status, code = error
            from starlette.responses import JSONResponse  # noqa: PLC0415

            detail = {
                "sandbox_disabled": "Not found.",
                "unknown_realm": f"Unknown realm; expected one of {list(REALMS)}.",
                "realm_mismatch": "This session belongs to a different realm.",
            }[code]
            await JSONResponse({"detail": detail, "code": code}, status_code=status)(scope, receive, send)
            return
        token = set_for_request(r)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)
