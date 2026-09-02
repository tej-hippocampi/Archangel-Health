"""Password policy and reset-token helpers for the Asclepius auth plane.

Physicians choose their own password during onboarding. Before this existed the
system generated one and mailed it, which meant the credential lived forever in
an inbox and there was no way to change it or recover it.

Two rules shape what follows:

  * A reset token is a bearer credential for the account. It is stored only as a
    sha256 hash, so a leaked database read cannot be replayed against the reset
    endpoint. This mirrors ``ingest_upload_links.token_hash`` rather than
    inventing a second convention.
  * No composition rules. Requiring a symbol and a digit reliably produces
    "Password1!" and nothing safer. Length plus a check against the obvious
    guesses does more, and annoys a busy physician less.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta
from typing import Tuple

#: Twelve, matching the floor the provider portal already enforces. One floor,
#: not two.
MIN_LENGTH = 12
MAX_LENGTH = 200

RESET_TTL_MINUTES = 60
#: Live (unconsumed, unexpired) resets one account may hold in the window. A
#: physician who clicks "forgot" four times gets the fourth request quietly
#: dropped rather than an error that would confirm the account exists.
MAX_LIVE_RESETS = 3

#: Passwords that are common enough that length alone is not protection.
_DENYLIST = frozenset(
    {
        "password", "password1", "password123", "passw0rd", "qwertyuiop",
        "123456789012", "1234567890123", "letmeinplease", "iloveyou123",
        "administrator", "welcome12345", "changeme1234", "archangelhealth",
        "asclepius123", "medicine123", "doctor123456",
    }
)


class PasswordRejected(ValueError):
    """Raised with a message written to be shown to the person typing."""


def validate(password: str, *, email: str = "") -> None:
    pw = password or ""
    if len(pw) < MIN_LENGTH:
        raise PasswordRejected(f"Use at least {MIN_LENGTH} characters.")
    if len(pw) > MAX_LENGTH:
        raise PasswordRejected(f"Use at most {MAX_LENGTH} characters.")
    if pw.strip() != pw:
        raise PasswordRejected("Remove the leading or trailing spaces.")
    folded = pw.casefold()
    if folded in _DENYLIST:
        raise PasswordRejected("That password is too common. Choose another.")
    addr = (email or "").casefold().strip()
    if addr:
        if folded == addr:
            raise PasswordRejected("Your password cannot be your email address.")
        local = addr.split("@", 1)[0]
        if local and len(local) >= 4 and folded == local:
            raise PasswordRejected("Your password cannot be your email name.")
    if len(set(pw)) < 5:
        raise PasswordRejected("That password repeats too few characters.")


def new_reset_token() -> Tuple[str, str]:
    """Return ``(raw, hashed)``. The raw token exists only in the email."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_reset_token(raw)


def hash_reset_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def reset_expires_at(now: datetime | None = None) -> str:
    base = now or datetime.utcnow()
    return (base + timedelta(minutes=RESET_TTL_MINUTES)).replace(microsecond=0).isoformat()


def reset_url(raw_token: str) -> str:
    base = (os.getenv("LANDING_URL") or os.getenv("BASE_URL") or "http://localhost:5173").rstrip("/")
    return f"{base}/reset-password?token={raw_token}"


# ─── Pre-approval sign-in links ───────────────────────────────────────────────
# An applicant has no password: Onboarding v2 removed that step, and approval is
# where a credential comes into existence. They still need to get back in before
# a decision, to finish the practice case, so this is the door.
#
# It is deliberately the weakest door that works. Fifteen minutes rather than the
# reset link's sixty, because this one is requested and used in the same sitting,
# and single-use, because the thing it opens is an account nobody has yet decided
# to trust. It grants the PROVISIONAL surface set and nothing else, so even a
# leaked link reaches a practice case and a status page.

SIGNIN_TTL_MINUTES = 15


def new_signin_token() -> Tuple[str, str]:
    """Return ``(raw, hashed)``. The raw token exists only in the email."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_reset_token(raw)


def signin_expires_at(now: datetime | None = None) -> str:
    base = now or datetime.utcnow()
    return (base + timedelta(minutes=SIGNIN_TTL_MINUTES)).replace(microsecond=0).isoformat()


def signin_url(raw_token: str) -> str:
    """Land in the portal itself, not on the landing site.

    The portal is what an applicant is being sent back to, and it is served by
    this backend, so BASE_URL is the right root here even though the reset link
    above prefers LANDING_URL. The token rides as a query parameter that the
    portal exchanges for a session and then strips from the address bar.
    """
    base = (os.getenv("BASE_URL") or "http://localhost:8000").rstrip("/")
    return f"{base}/asclepius?signin={raw_token}"
