"""Sandbox PRD §2 — seeding the sandbox realm, and §3.2's ``Reset sandbox``.

Everything here runs INSIDE the sandbox realm (``realm.is_sandbox()`` is
asserted at every entry) against the sandbox store the realm-keyed accessors
hand back. Nothing in this module can reach a live file: it never names a
path except through ``realm.paths("sandbox")``, and ``reset`` refuses any path
that is not recognisably a sandbox path before it touches the filesystem.

Idempotent by stable ids (emails): running the seed twice leaves ten
physicians, one admin, one community — with passwords reset to the configured
values, so an operator can always regain the sandbox logins.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

import realm as _realm

log = logging.getLogger("asclepius.sandbox")

SANDBOX_DOMAIN = "archangelhealth.ai"
ADMIN_EMAIL = f"sandbox-admin@{SANDBOX_DOMAIN}"
ORGANIZATION = "Sandbox Test Hospital"

#: §2 — ten physicians, deterministic, obviously fake, specialty-spread so
#: routing is testable. (number, display name, email local part, tier, specialty)
PHYSICIANS: List[Dict[str, Any]] = [
    {"n": 1, "name": "Dr. Ada Test", "email": f"sb-labeler-1@{SANDBOX_DOMAIN}", "tier": "labeler", "specialty": "nephrology"},
    {"n": 2, "name": "Dr. Ben Test", "email": f"sb-labeler-2@{SANDBOX_DOMAIN}", "tier": "labeler", "specialty": "nephrology"},
    {"n": 3, "name": "Dr. Cy Test", "email": f"sb-labeler-3@{SANDBOX_DOMAIN}", "tier": "labeler", "specialty": "cardiology"},
    {"n": 4, "name": "Dr. Dee Test", "email": f"sb-labeler-4@{SANDBOX_DOMAIN}", "tier": "labeler", "specialty": "cardiology"},
    {"n": 5, "name": "Dr. Eli Test", "email": f"sb-labeler-5@{SANDBOX_DOMAIN}", "tier": "labeler", "specialty": "oncology"},
    {"n": 6, "name": "Dr. Fay Test", "email": f"sb-labeler-6@{SANDBOX_DOMAIN}", "tier": "labeler", "specialty": "hepatology"},
    {"n": 7, "name": "Dr. Gus Test", "email": f"sb-labeler-7@{SANDBOX_DOMAIN}", "tier": "labeler", "specialty": "nephrology"},
    {"n": 8, "name": "Dr. Hal Review", "email": f"sb-reviewer-1@{SANDBOX_DOMAIN}", "tier": "reviewer", "specialty": "nephrology"},
    {"n": 9, "name": "Dr. Ivy Review", "email": f"sb-reviewer-2@{SANDBOX_DOMAIN}", "tier": "reviewer", "specialty": "cardiology"},
    {"n": 10, "name": "Dr. Jo Review", "email": f"sb-reviewer-3@{SANDBOX_DOMAIN}", "tier": "reviewer", "specialty": "oncology"},
]

#: §2 ``--fresh`` — one physician left un-onboarded, to test the walkthrough.
FRESH_PREFIX = "sb-fresh"

_BOARD_CERT = {
    "nephrology": "ABIM Nephrology",
    "cardiology": "ABIM Cardiovascular Disease",
    "oncology": "ABIM Medical Oncology",
    "hepatology": "ABIM Gastroenterology (Transplant Hepatology)",
}


class NotSandbox(RuntimeError):
    """Raised when a sandbox-only operation is attempted outside the realm."""


def _require_sandbox() -> None:
    if not _realm.is_sandbox():
        raise NotSandbox("sandbox seed/reset may only run in the sandbox realm")


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


# ─── Physicians ──────────────────────────────────────────────────────────────
def _complete_first_run(store: Any, user_id: str) -> None:
    from asclepius import first_run as asc_first_run  # noqa: PLC0415
    from asclepius.schemas import FIRST_RUN_STOPS  # noqa: PLC0415

    state = store.get_first_run(user_id)
    state["stops"] = {stop: asc_first_run.DONE for stop in FIRST_RUN_STOPS}
    state["completed_at"] = _utcnow_iso()
    store.set_first_run(user_id, state)


def _pass_practice_case(store: Any, user_id: str) -> None:
    from asclepius import tutorial_case as _tc  # noqa: PLC0415

    state = store.get_tutorial_state(user_id)
    state["status"] = "completed"
    state["gate"] = {"state": "passed", "passed_version": _tc.TUTORIAL_VERSION,
                     "attempts": 1, "source": "sandbox_seed"}
    store.set_tutorial_state(user_id, state)


def ensure_physician(store: Any, spec: Dict[str, Any], *, password: str,
                     onboarded: bool = True) -> Dict[str, Any]:
    """One fake physician, idempotent by email. ``onboarded=False`` leaves the
    first-run checklist and practice case open (the ``--fresh`` doctor)."""
    _require_sandbox()
    email = spec["email"].lower()
    existing = store.get_user_by_email(email)
    if existing is None:
        user = store.create_user(
            email=email, password=password, role="evaluator",
            specialty=spec["specialty"], board_cert=_BOARD_CERT.get(spec["specialty"]),
            years_experience=8 + int(spec.get("n", 0)), organization=ORGANIZATION,
            tier=spec["tier"],
        )
    else:
        user = existing
        with store._conn() as conn:  # reset the shared password + role/tier drift
            from asclepius.store import hash_password  # noqa: PLC0415
            conn.execute(
                "UPDATE users SET password_hash = ?, role = 'evaluator', active = 1, "
                "specialty = ?, tier = ?, organization = ? WHERE id = ?",
                (hash_password(password), spec["specialty"], spec["tier"], ORGANIZATION, user["id"]),
            )
    uid = user["id"]
    store.set_verification_status(uid, "approved", notes="sandbox seed")
    store.set_real_data_approved(uid, True, source="auto:sandbox_seed")
    if onboarded:
        _pass_practice_case(store, uid)
        _complete_first_run(store, uid)
    store.ensure_referral_code(uid)
    return store.get_user_by_id(uid)


def fresh_physician_spec(store: Any) -> Dict[str, Any]:
    """The next unused ``sb-fresh-N`` address."""
    n = 1
    while store.get_user_by_email(f"{FRESH_PREFIX}-{n}@{SANDBOX_DOMAIN}") is not None:
        n += 1
    return {"n": 100 + n, "name": f"Dr. Fresh {n}", "email": f"{FRESH_PREFIX}-{n}@{SANDBOX_DOMAIN}",
            "tier": "labeler", "specialty": "nephrology"}


# ─── Community ───────────────────────────────────────────────────────────────
async def welcome_all(users: List[Dict[str, Any]]) -> int:
    """Post the one-time community welcome for each physician (§2: community-
    welcomed). Runs the real ``welcome_new_member`` so the sandbox community
    gets the same u-system posts a live one does — into the sandbox DB."""
    _require_sandbox()
    from community.onboard import welcome_new_member  # noqa: PLC0415

    n = 0
    for u in users:
        try:
            if await welcome_new_member(u):
                n += 1
        except Exception:  # pragma: no cover — a missed welcome is not a failed seed
            log.warning("[sandbox] welcome failed for %s", u.get("email"), exc_info=True)
    return n


def _member_country_codes(store: Any) -> List[str]:
    codes: List[str] = []
    for user in store.list_users():
        if not user.get("active") or user.get("role") != "evaluator":
            continue
        code = (user.get("country_of_practice") or user.get("country_of_licensure") or "").strip().upper()
        if code and code not in codes:
            codes.append(code)
    return codes


def ensure_sandbox_admin() -> Optional[Dict[str, Any]]:
    """The sandbox admin exists as soon as the realm is switched on (§2): the
    seed endpoint needs an admin to call it, and this is that admin. Runs at
    boot and on every ``/sandbox/status`` read, idempotently; a no-op while the
    realm is dark or outside the sandbox realm."""
    if not _realm.enabled() or not _realm.is_sandbox():
        return None
    from asclepius.store import get_store  # noqa: PLC0415

    return get_store().ensure_admin(email=ADMIN_EMAIL, password=_realm.admin_password())


# ─── The seed ────────────────────────────────────────────────────────────────
def seed_sync(*, admin_password: str, doctor_password: str, fresh: bool = False) -> Dict[str, Any]:
    """Everything except the async community welcomes. Returns the users so
    the caller can ``await welcome_all``."""
    _require_sandbox()
    if not admin_password or not doctor_password:
        raise ValueError("both sandbox passwords are required to seed")
    from asclepius.store import get_store  # noqa: PLC0415
    from community.store import get_community_store  # noqa: PLC0415

    store = get_store()
    cstore = get_community_store()
    admin = store.ensure_admin(email=ADMIN_EMAIL, password=admin_password)
    physicians = [ensure_physician(store, spec, password=doctor_password) for spec in PHYSICIANS]
    fresh_user: Optional[Dict[str, Any]] = None
    if fresh:
        fresh_user = ensure_physician(store, fresh_physician_spec(store), password=doctor_password,
                                      onboarded=False)
    try:
        cstore.ensure_default_channels(_member_country_codes(store))
    except Exception:  # pragma: no cover
        log.warning("[sandbox] community channel seeding failed", exc_info=True)
    return {"admin": admin, "physicians": physicians, "fresh": fresh_user}


async def seed(*, admin_password: str, doctor_password: str, fresh: bool = False) -> Dict[str, Any]:
    out = seed_sync(admin_password=admin_password, doctor_password=doctor_password, fresh=fresh)
    welcomed = await welcome_all(out["physicians"] + ([out["fresh"]] if out["fresh"] else []))
    return {
        "ok": True,
        "realm": _realm.current(),
        "admin_email": out["admin"]["email"],
        "physicians": [p["email"] for p in out["physicians"]],
        "fresh": out["fresh"]["email"] if out["fresh"] else None,
        "community_welcomed": welcomed,
    }


def roster(store: Any) -> List[Dict[str, Any]]:
    """§3.2 Accounts tab: the seeded physicians and their state, plus any
    fresh doctors. Passwords are NOT here — the router adds them from env."""
    _require_sandbox()
    out = []
    for spec in PHYSICIANS:
        u = store.get_user_by_email(spec["email"])
        out.append({**spec, "seeded": u is not None, "user_id": u["id"] if u else None,
                    "onboarded": bool(u and store.get_first_run(u["id"]).get("completed_at"))})
    for user in store.list_users():
        if (user.get("email") or "").startswith(FRESH_PREFIX + "-"):
            out.append({"n": None, "name": "Fresh doctor", "email": user["email"], "tier": user.get("tier"),
                        "specialty": user.get("specialty"), "seeded": True, "user_id": user["id"],
                        "onboarded": False, "fresh": True})
    return out


# ─── Reset (§3.2, §6.6) ──────────────────────────────────────────────────────
RESET_CONFIRMATION = "RESET SANDBOX"


def _assert_sandbox_path(path: str) -> str:
    """Refuse to delete anything that is not recognisably a sandbox path.
    The derivation is deterministic (``realm.sandbox_db_path`` /
    ``sandbox_dir_path``), so this is a real check, not a formality."""
    base = os.path.basename(path.rstrip("/\\"))
    if not (base.endswith("_sandbox.db") or base == _realm.SANDBOX):
        raise NotSandbox(f"refusing to delete non-sandbox path {path!r}")
    return path


def reset_files() -> Dict[str, Any]:
    """Delete the three sandbox DBs (and their WAL/SHM sidecars) and the
    sandbox asset / ingest / export directories. Sandbox-only — the realm is
    re-checked here, after the router's own check, BEFORE any file is touched
    (§6.6)."""
    _require_sandbox()
    paths = _realm.paths(_realm.SANDBOX)
    # Every path validated first, so a bad derivation cannot delete half.
    files = [_assert_sandbox_path(paths[k]) for k in ("asclepius", "community", "team")]
    dirs = [_assert_sandbox_path(paths[k]) for k in ("assets", "ingest", "exports")]
    # Forget the open handles BEFORE unlinking so nothing keeps writing to a
    # deleted inode and the next accessor call opens a fresh file.
    from asclepius.store import drop_store_for_realm  # noqa: PLC0415
    from community.store import drop_community_store_for_realm  # noqa: PLC0415
    from team_store import drop_team_store_for_realm  # noqa: PLC0415

    drop_store_for_realm(_realm.SANDBOX)
    drop_community_store_for_realm(_realm.SANDBOX)
    drop_team_store_for_realm(_realm.SANDBOX)
    removed: List[str] = []
    for f in files:
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = f + suffix
            if os.path.exists(p):
                os.remove(p)
                removed.append(p)
    for d in dirs:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return {"removed": removed}


async def reset(*, admin_password: str, doctor_password: str, fresh: bool = False) -> Dict[str, Any]:
    _require_sandbox()
    removed = reset_files()
    seeded = await seed(admin_password=admin_password, doctor_password=doctor_password, fresh=fresh)
    return {**seeded, "reset": removed}
