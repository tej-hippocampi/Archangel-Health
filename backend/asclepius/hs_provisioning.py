"""Minting a health-system portal account, in one place.

Three doors now need an account created for an organization: an operator
provisioning a partner we met on a call (``/health-systems/provision``), a
health system letting itself in through the landing page, and a member of one
adding a colleague. They must produce the SAME account -- same username
derivation, same collision suffixing, same one-time passphrase, same
forced rotation on first sign-in -- or "we emailed you credentials" means three
subtly different things and only one of them was reviewed.

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
) -> Dict[str, Any]:
    """Create-or-rotate one portal account. Returns
    ``{username, passphrase, action, reused}``.

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
    passphrase = generate_portal_passphrase()

    existing = [
        u for u in store.list_hs_portal_users(hs_id)
        if (u.get("email") or "").strip().lower() == addr.lower() and u.get("active")
    ]
    if existing:
        username = existing[0]["username"]
        store.set_hs_portal_password(username, passphrase, must_reset=must_reset)
        return {"username": username, "passphrase": passphrase,
                "action": ROTATED, "reused": True}

    username = unique_hs_username(store, derive_hs_username(org_name))
    store.create_hs_portal_user(
        username=username, hs_id=hs_id, password=passphrase, email=addr,
        must_reset=must_reset, full_name=full_name, signup_source=signup_source,
        approval_status=approval_status)
    if invited_by:
        store.set_hs_portal_invited_by(username, invited_by)
    return {"username": username, "passphrase": passphrase,
            "action": CREATED, "reused": False}
