"""Minting a health-system portal account, in one place.

Three doors now need an account created for an organization: an operator
provisioning a partner we met on a call (``/health-systems/provision``), a
health system letting itself in through the landing page, and a member of one
adding a colleague. They must produce the SAME account -- same username
derivation, same collision suffixing, same forced rotation on first sign-in --
or "we emailed you access" means three subtly different things and only one of
them was reviewed.

What the three no longer share is WHICH SECRET is minted. Two of them
(``mint_invite=True``, the self-signup and a partner adding a colleague) mint a
one-time claim link and no credential at all: the person sets their own password
on arrival. The operator's door still mints a passphrase, because it is the one
case where somebody is handed an account they did not ask for, during a call,
and the credential is read out loud rather than clicked.

So the account-minting body of ``provision_health_system_portal`` lives here and
that endpoint calls it, per the PRD's "wrap it, don't fork it". What did NOT
move is anything the three doors legitimately disagree about: which health
system row to use (an operator reuses by name, a public signup must never --
see ``create_health_system_unclaimed``), what the email says, and what the
uploads are destined for. Those stay with their callers.

No email is sent from here, deliberately. The three subjects differ, the send
is awaited in one caller and backgrounded in another, and a helper that both
writes the row and mails the credential cannot be used by a caller that wants
to decide for itself what happens when the send fails.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from asclepius.portal_accounts import (
    derive_hs_username,
    generate_portal_passphrase,
    unique_hs_username,
)

#: What ``provision`` returns in ``action``. Kept as constants because two
#: callers branch on them and a typo in a string literal is a silent no-op.
CREATED = "portal_user_created"
ROTATED = "credentials_rotated"

#: How long a claim link lives. Two weeks, because the person receiving it is a
#: hospital executive who was added by a colleague and may be on service, and a
#: link that dies over a fortnight's leave produces a support conversation
#: rather than a signup. Long enough to be useful, short enough that a forwarded
#: mail thread does not stay a live door for a year.
INVITE_TTL_DAYS = 14


def invite_token_hash(token: str) -> str:
    """SHA-256, matching ``routers/asclepius.py::_token_hash``.

    Public, because the provider router hashes an incoming token with it to look
    an invite up, and minting and resolving have to agree byte for byte.

    The same function twice rather than an import across that boundary: this
    module is called by the provider router, which the purpose-isolation suite
    holds to a stricter standard than the admin surface, and a shared helper
    would be the first thread pulling those two files together.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def provision_account(
    store: Any,
    *,
    hs_id: str,
    org_name: str,
    email: str,
    full_name: Optional[str] = None,
    signup_source: Optional[str] = None,
    invited_by: Optional[str] = None,
    approval_status: Optional[str] = None,
    must_reset: bool = True,
    mint_invite: bool = False,
) -> Dict[str, Any]:
    """Create-or-rotate one portal account. Returns
    ``{username, passphrase, invite_token, action, reused}``.

    ``mint_invite`` is the door that never mails a credential. The account is
    created with an unguessable random password NOBODY holds, plus a one-time
    token and a 14-day expiry, and the CLEARTEXT TOKEN is returned in place of a
    passphrase for the caller to put in a claim link. The person sets their own
    password on arrival, which is why the portal header can finally show a name:
    a claimed account has one, because its owner typed it.

    ``must_reset`` stays 1 on that path even though nothing is being replaced.
    It is the belt to the token's braces: if a claim link is somehow bypassed,
    the account still cannot be used without a password change, and the flag is
    cleared by the claim itself.

    The passphrase is returned in the clear because it exists nowhere else --
    the row holds only its hash, and the ONLY copy is the one the caller is
    about to put in an email. Log it and you have written a live credential to
    disk.

    Re-provisioning an address that already has an active account on this
    organization ROTATES that account's password rather than minting a second
    one. Two accounts for one person is how a hospital ends up with a login
    nobody remembers holding, which is a bigger problem than a rotated password.

    ``must_reset`` defaults True and every caller here leaves it there: a
    credential that travelled through email has to be replaced before it guards
    anything, which is §0.1.1 and the same rule the physician onboarding follows.
    """
    addr = (email or "").strip()
    if not addr:
        raise ValueError("email is required")
    # On the invite path the stored secret is thirty-two bytes of urandom that
    # exist for one statement and are never returned to anyone. The row must
    # still hold a real hash, because a NULL or a known placeholder would be a
    # password every unclaimed account shares.
    passphrase = secrets.token_urlsafe(32) if mint_invite else generate_portal_passphrase()
    token = secrets.token_urlsafe(32) if mint_invite else ""
    expires_at = (datetime.utcnow() + timedelta(days=INVITE_TTL_DAYS)).isoformat()

    existing = [
        u for u in store.list_hs_portal_users(hs_id)
        if (u.get("email") or "").strip().lower() == addr.lower() and u.get("active")
    ]
    if existing:
        username = existing[0]["username"]
        store.set_hs_portal_password(username, passphrase, must_reset=must_reset)
        if mint_invite:
            store.set_hs_portal_invite(
                username, token_hash=invite_token_hash(token),
                expires_at=expires_at, invited_by=invited_by)
        return {"username": username,
                # Never a live credential on the invite path. The caller has a
                # token to put in a link and nothing it could accidentally
                # print into an email or an event payload.
                "passphrase": "" if mint_invite else passphrase,
                "invite_token": token,
                "action": ROTATED, "reused": True}

    username = unique_hs_username(store, derive_hs_username(org_name))
    store.create_hs_portal_user(
        username=username, hs_id=hs_id, password=passphrase, email=addr,
        must_reset=must_reset, full_name=full_name, signup_source=signup_source,
        approval_status=approval_status)
    if mint_invite:
        store.set_hs_portal_invite(
            username, token_hash=invite_token_hash(token),
            expires_at=expires_at, invited_by=invited_by)
    elif invited_by:
        store.set_hs_portal_invited_by(username, invited_by)
    return {"username": username,
            "passphrase": "" if mint_invite else passphrase,
            "invite_token": token,
            "action": CREATED, "reused": False}
