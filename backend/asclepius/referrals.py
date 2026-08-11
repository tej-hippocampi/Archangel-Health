"""Physician referrals — one implementation, two surfaces (PRD-REF).

A referral is not a transaction. It is a **bet with a long, uncertain
settlement**: a physician refers a colleague on Monday, that colleague signs up
Thursday, is verified the following week, draws a first task the week after, and
it clears review a fortnight later. Five weeks between the act and the money.

So the failure mode this module is written against is not "the button doesn't
work". It is: *a doctor refers two colleagues, sees nothing for a month,
concludes the feature is broken, and never refers again* — and you have lost the
mechanism that produces almost all of your supply. Every function here exists to
make the wait legible: the funnel is reported as a sentence, never as a token,
and a referral is never rendered as absence.

─── Why this file exists at all ──────────────────────────────────────────────
The advisor tier shipped a complete referral spine: ``users.referral_code``, the
``referrals`` table, ``claim_referral_for_signup``, ``advance_referral_for_user``
and two endpoints under ``/api/asclepius/advisor/``. Generalising it to every
approved physician is emphatically NOT a second referral system — two referral
tables is how a bounty gets paid twice.

So the POLICY moved here and both routers call it:

  * ``routers/asclepius_payments.py``  — the physician surface, on Earnings
  * ``routers/asclepius_advisor.py``   — the existing advisor surface

Both write the same table, both resolve through the same claim path, and the
bounty (``asclepius/payments.py``) knows about exactly one of them.

─── The three defects this generalisation must not inherit ───────────────────
1. **The rate limit is keyed on the USER, never the IP.** A hospital NATs; an
   IP-keyed cap means the eleventh referral out of one building gets a 429 while
   the actual threat — a stolen token rotated across a proxy pool — is
   unthrottled. The IP limit stays as a cheap outer wall; the limit that matters
   is ``user["id"]``.
2. **The response never discloses whether an address has an account.** The
   advisor path narrowed its oracle to physician accounts (audit M4), which
   closed the worst version — probing a company inbox — but still answered
   "does this doctor have an account here?" one address at a time. The physician
   surface returns an IDENTICAL shape either way and lets the funnel report the
   outcome, which loses the referrer nothing.
3. **A missing ``full_name`` never falls back to the account email.** That string
   goes into the subject line of a message to a THIRD PARTY. No name on file
   means no named referral, and the neutral copy instead — never an address.

─── What a referrer is entitled to see ───────────────────────────────────────
Name, date, funnel position in plain words. NOT the invitee's NPI, tier score,
verification notes or credential files. Referring someone does not entitle you
to their credentialing dossier. Built by WHITELIST (``public_referral``) rather
than by stripping fields, because a whitelist cannot leak the next column
somebody adds upstream.

And before a referral resolves, the invitee is shown as a MASKED address; after
they sign up and there is a real name, the name. A third party's raw address
does not go back to the referrer once the system knows who they are.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from asclepius import capabilities as _caps
from asclepius import compensation

log = logging.getLogger("asclepius.referrals")


# ═══ Who may refer ════════════════════════════════════════════════════════════
def can_refer(user: Optional[Dict[str, Any]]) -> bool:
    """An approved physician, or anyone holding the advisor REFER capability.

    Written against ``capabilities`` and never against a tier literal. A bare
    ``tier == 'advisor'`` here would be the exact defect ``capabilities.py`` was
    built to remove, and it fails SILENTLY — "this user is not an advisor" is a
    legitimate answer for a labeler, so nothing logs and nothing 500s.

    ``LABEL`` is the operative test for "approved physician": a tier is assigned
    at the moment the verification queue approves an account, so holding it IS
    having been approved. The verification status is re-checked anyway rather
    than leaned on: ``auth.get_current_user`` already refuses pending/rejected
    across the whole evaluator surface, but a gate on an outbound email should
    not be one refactor of somebody else's middleware away from opening.
    """
    u = user or {}
    if u.get("role") == "admin":
        return True
    if u.get("verification_status") in ("pending", "rejected"):
        return False
    if not u.get("active", True):
        return False
    return _caps.can(u, _caps.REFER) or _caps.can(u, _caps.LABEL)


# ═══ Throttles (defect 1) ═════════════════════════════════════════════════════
# 20 a day is generous for a real physician — nobody refers twenty colleagues in
# an afternoon — and useless for a scripted token. The global cap behind it is a
# volumetric backstop on the sending domain itself: one compromised account must
# not be able to burn the reputation every other physician's invite rides on.
REFERRALS_PER_USER = (20, 86400)
REFERRALS_GLOBAL = (200, 3600)
#: Same address, same cap as the public self-serve path. Without it one inbox can
#: be mailed without bound by rotating the referrer, which buries the real invite.
REFERRALS_PER_INVITEE_24H = 3

#: A referral that has not signed up in this long is retired. Without it a
#: doctor's funnel is a graveyard of two-year-old invitations and the page stops
#: meaning anything.
REFERRAL_EXPIRY_DAYS = 90


def expiry_days() -> int:
    raw = (os.getenv("ASCLEPIUS_REFERRAL_EXPIRY_DAYS") or "").strip()
    if not raw:
        return REFERRAL_EXPIRY_DAYS
    try:
        value = int(raw)
    except ValueError:
        log.warning("asclepius.referrals: ASCLEPIUS_REFERRAL_EXPIRY_DAYS=%r is not an "
                    "integer; using %d", raw, REFERRAL_EXPIRY_DAYS)
        return REFERRAL_EXPIRY_DAYS
    return value if value > 0 else REFERRAL_EXPIRY_DAYS


def throttle_keys(user_id: str) -> Tuple[Tuple[str, Tuple[int, int]], ...]:
    """The buckets a referral POST must clear, keyed on the USER (defect 1).

    Returned rather than enforced so the router owns the HTTP shape (429 +
    Retry-After) and this module owns the policy. ``ratelimit`` is imported by
    the caller, not here: this module must stay importable from a test that has
    never built a request.
    """
    return (
        (f"asclepius_referral:{user_id}", REFERRALS_PER_USER),
        ("asclepius_referral:__global__", REFERRALS_GLOBAL),
    )


# ═══ Vocabulary ═══════════════════════════════════════════════════════════════
# A physician should never have to learn our state machine to know whether their
# friend is nearly there. Four states, four SENTENCES — deliberately not a
# progress bar: a bar implies a predictable duration, and this one depends on a
# colleague's schedule. Text is honest where a bar would lie.
STATUS_SENTENCES = {
    "invited": "Invited",
    "signed_up": "Signed up, awaiting verification",
    "verified": "Verified · awaiting first case",
    "approved": "Verified · awaiting first case",
    "declined": "Not verified",
    "expired": "Invitation expired",
}
#: What a referral is worth right now, in one word each. NULL bounty state is
#: 'pending', and that is the state the whole feature is designed around.
BOUNTY_PENDING = "pending"
BOUNTY_EARNED = "earned"
BOUNTY_DUPLICATE = "duplicate"
BOUNTY_EXPIRED = "expired"
BOUNTY_INELIGIBLE = "ineligible"
#: The invitee was not verified, so no first case is coming. DERIVED at read
#: time and never persisted, because ``declined`` is not in ``_REFERRAL_LADDER``
#: and is therefore reversible: a physician refused today can be approved next
#: month, and a persisted terminal bounty state would make that reversal invisible
#: while the funnel silently kept the money it had already written off.
BOUNTY_CLOSED = "closed"

#: Funnel statuses under which no first case can arrive. A referral sitting on
#: one of these must NOT be counted as pending money — "+$150 pending" beside a
#: colleague who was refused verification is the page lying to a physician, and
#: it is the same failure as showing nothing, reached from the other direction.
_NO_FIRST_CASE_COMING = frozenset({"declined"})


def status_sentence(status: Optional[str], bounty_state: Optional[str]) -> str:
    """The plain-language sentence for one referral row.

    ``bounty_state`` outranks the funnel where the two disagree, because it is
    the later fact: a referral that has paid is 'Completed first case' whatever
    the credentialing routes later write into ``status``, and a duplicate is a
    duplicate even though the person did in fact join.
    """
    if bounty_state == BOUNTY_EARNED:
        return "Completed first case"
    if bounty_state == BOUNTY_DUPLICATE:
        return "Joined · already credited to another referrer"
    if bounty_state == BOUNTY_EXPIRED:
        return "Invitation expired"
    return STATUS_SENTENCES.get(status or "invited", "Invited")


def mask_email(email: Optional[str]) -> str:
    """``jane.doe@mgh.org`` -> ``j••••@mgh.org``.

    The domain survives because it is the half that helps a referrer recognise
    who they invited; the local part does not, because once the invitee has
    signed up we show their NAME and the raw address of a third party stops
    being the referrer's business.
    """
    raw = (email or "").strip()
    if "@" not in raw:
        return raw[:1] + "••••" if raw else "—"
    local, _, domain = raw.partition("@")
    head = local[:1] if local else ""
    return f"{head}••••@{domain}"


def display_name(referral: Dict[str, Any]) -> str:
    """The name the referrer typed, or the masked address — never the raw one.

    A referrer who typed "Dr Chen" gets "Dr Chen" back. A referrer who typed only
    an address gets it masked, both before and after the invitee joins: the
    system learning the invitee's real name does not make it the referrer's to
    read, and the referrer already knows who they invited.
    """
    typed = (referral.get("invitee_name") or "").strip()
    if typed:
        return typed
    return mask_email(referral.get("invitee_email"))


def public_referral(
    r: Dict[str, Any], *, bounty_state: Optional[str] = None,
    bounty_cents: Optional[int] = None,
) -> Dict[str, Any]:
    """What a referrer is entitled to see — built by WHITELIST.

    Deliberately thin: who, when, where they are, what it is worth. No NPI, no
    tier score, no verification notes, no credential assets. Building this by
    STRIPPING fields would leak the next column somebody adds to ``referrals`` or
    to the join upstream of it; a whitelist cannot.

    ``bounty_cents`` is the amount STAMPED ON THE LEDGER for an earned row, not
    the rate in force today — see ``store.referral_bounty_amounts``.
    """
    status = r.get("status")
    state = bounty_state if bounty_state is not None else (r.get("bounty_state") or BOUNTY_PENDING)
    return {
        "referral_id": r.get("referral_id"),
        "invitee_display": display_name(r),
        "status": status,
        # The SENTENCE is always derived from the persisted column, never from a
        # read-time override: a derived state changes what the money column says,
        # not what happened to the person.
        "status_sentence": status_sentence(status, r.get("bounty_state")),
        "bounty_state": state,
        "bounty_cents": bounty_cents,
        "invited_at": r.get("invited_at"),
        "resolved_at": r.get("resolved_at"),
    }


# ═══ Invite copy safety ═══════════════════════════════════════════════════════
def header_safe(text: str, *, limit: int = 120) -> str:
    """A string safe to place in an email header.

    Collapses CR/LF — and the unicode line separators an email library may
    normalise into them — to a space, then bounds the length. SendGrid's JSON
    transport is immune; the SMTP fallback assigns the string straight into a
    MIME header, where a CR/LF is header injection. "Only trusted people can set
    a display name" is a fact about today's permissions, not a property of the
    code.
    """
    collapsed = re.sub(r"[\r\n  \x0b\x0c\x85]+", " ", text or "")
    return " ".join(collapsed.split())[:limit]


def referrer_display_name(user: Dict[str, Any]) -> str:
    """The referrer's name for the invite — or an empty string (defect 3).

    NEVER falls back to the account email. That string goes into the subject line
    and body of a message to a third party, so a physician with no name on file
    would have had their personal address disclosed to everyone they invited —
    and "toby@gmail.com suggested you'd be a good fit" is not the sentence that
    makes a named referral work anyway. No name means no named referral.
    """
    return header_safe(((user or {}).get("full_name") or "").strip())


def landing_base() -> str:
    return (os.getenv("LANDING_URL") or os.getenv("BASE_URL")
            or "http://localhost:8000").strip().rstrip("/")


def portal_base() -> str:
    return (os.getenv("ASCLEPIUS_PORTAL_URL") or landing_base()).strip().rstrip("/")


def invite_url(code: Optional[str]) -> Optional[str]:
    """The bare link a physician can paste into a text message.

    Points at the EXISTING physician signup page, not at a referral-specific
    route — there is no such route, and a shareable link that 404s is worse than
    no shareable link. The code rides along as a query parameter for provenance
    only: attribution resolves on the invitee's EMAIL at provisioning time
    (``store.claim_referral_for_signup``), so a link stripped by a messaging app
    still attributes correctly.
    """
    if not code:
        return None
    return f"{landing_base()}/physicians?ref={code}"


# ═══ Creating a referral ══════════════════════════════════════════════════════
#: Outcomes of ``create_referral``. The CALLER decides how much of this to
#: disclose: the physician surface flattens every one of them into an identical
#: response (defect 2), while the advisor surface keeps its legacy shape.
OUTCOME_INVITED = "invited"
OUTCOME_ALREADY_INVITED = "already_invited"
OUTCOME_MEMBER = "member"


#: Free-text bounds. A physician's name and a one-line "knows her from Stanford
#: ortho" — not an essay, and not an upload channel.
_NAME_MAX = 120
_NOTE_MAX = 500


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    text = " ".join((value or "").split())
    return text[:limit] or None


class ReferralRefused(Exception):
    """This invitation cannot be recorded at all. ``code`` is for the event log,
    ``detail`` is what the physician reads, ``status`` is the HTTP shape.

    Note what is NOT in here: "that address already has an account" is not a
    refusal, it is a fact — recorded as an ordinary referral so the referrer sees
    a row instead of an error, and so the response cannot be used as an oracle.
    """

    def __init__(self, code: str, detail: str, *, status: int = 422):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def create_referral(
    store, *, referrer: Dict[str, Any], email: str,
    name: Optional[str] = None, note: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one referral. Returns ``{outcome, referral, referral_code}``.

    Everything that is policy lives here and nothing that is transport does: no
    HTTPException, no request, no email send (the caller does that, because it is
    async and best-effort and must never take the referral row down with it).

    Refuses, and only these:
      * self-referral — checked at invite AND again at accrual, because emails
        change and a physician who adds their own second address later must not
        be able to pay themselves $150;
      * an address invited too many times in 24 hours, by ANYONE;
      * a referrer with no referral code that could not be minted.
    """
    email = (email or "").lower().strip()
    if not email or "@" not in email:
        raise ReferralRefused("bad_email", "That does not look like an email address.")

    own = (referrer.get("email") or "").lower().strip()
    if email == own:
        # At invite AND at accrual. This one is cheap and catches the honest
        # mistake; the accrual-time check catches the patient version.
        raise ReferralRefused(
            "self_referral",
            "That is your own address. A referral has to be somebody else.")

    if store.count_recent_referrals_for_email(email, hours=24) >= REFERRALS_PER_INVITEE_24H:
        raise ReferralRefused(
            "invitee_capped",
            "That address has already been invited several times recently.",
            status=429)

    code = store.ensure_referral_code(referrer["id"])
    if not code:
        raise ReferralRefused(
            "no_code",
            "Your referral link could not be created. Try again in a moment.",
            status=409)

    # Bounded here rather than on each router's pydantic model, so both surfaces
    # inherit it and a third one cannot ship without it. Neither field is
    # validated beyond this — a name is a name — but an unbounded string on an
    # endpoint a physician may hit 20 times a day is free storage for anyone who
    # notices, and the display name also has to fit a table cell.
    name = _clip(name, _NAME_MAX)
    note = _clip(note, _NOTE_MAX)

    if store.has_referral_for_email(referrer["id"], email):
        existing = _latest_for(store, referrer["id"], email)
        return {"outcome": OUTCOME_ALREADY_INVITED, "referral": existing,
                "referral_code": code}

    # Only a PHYSICIAN account counts as an existing member (audit M4). An
    # unfiltered ``get_user_by_email`` spans every role, which turns this into an
    # account-existence oracle for admin, buyer and hospital-contact addresses —
    # a probe anyone could run, one address at a time, against a company inbox.
    existing_user = store.get_user_by_email(email)
    is_member = bool(existing_user
                     and existing_user.get("role") == "evaluator"
                     and not existing_user.get("is_mock"))
    if is_member:
        # A fact, not a failure. The row is recorded so the referrer sees an
        # honest line in their funnel, and no account is created either way.
        ref = store.insert_referral(
            referrer_id=referrer["id"], referral_code=code, invitee_email=email,
            invitee_name=name, note=note, status="signed_up")
        # The invitee already exists, so attach the row to them now rather than
        # waiting for a signup that already happened — otherwise the funnel sits
        # at "signed up" forever and the bounty, which resolves from the INVITEE,
        # can never find it.
        store.claim_referral_for_signup(email=email, user_id=existing_user["id"])
        return {"outcome": OUTCOME_MEMBER,
                "referral": store.get_referral(ref["referral_id"]),
                "referral_code": code, "invitee_user_id": existing_user["id"]}

    ref = store.insert_referral(
        referrer_id=referrer["id"], referral_code=code, invitee_email=email,
        invitee_name=name, note=note, status="invited")
    return {"outcome": OUTCOME_INVITED, "referral": ref, "referral_code": code}


def _latest_for(store, referrer_id: str, email: str) -> Optional[Dict[str, Any]]:
    for r in store.list_referrals_by_referrer(referrer_id):
        if (r.get("invitee_email") or "") == email:
            return r
    return None


async def send_invite(*, referrer: Dict[str, Any], email: str,
                      name: Optional[str], code: str) -> bool:
    """Best-effort delivery of the ONE invite email this product has.

    Never fails the caller: the referral row and the shareable link are the
    deliverable, and losing the row because SendGrid was down would lose the
    attribution permanently.

    This function deliberately MINTS NOTHING. An earlier version of the advisor
    path called ``team_store.create_health_system_invite``, which inserts a
    pending tenant row carrying a live 30-day token — and completing that wizard
    provisions an account with ``role="admin"``. The public endpoint that mints
    the same artifact layers five guards on it; reaching past it inherited none
    of them. So the invitee lands on ``/physicians`` and gets their onboarding
    link from the guarded path like everybody else.
    """
    from email_utils import is_email_transport_configured, send_html_email
    from onboarding_emails import build_asclepius_invite_email

    if not is_email_transport_configured():
        return False

    referrer_name = referrer_display_name(referrer)
    html_body = build_asclepius_invite_email(
        invitee_first_name=((name or "").strip().split(" ")[0] if name else ""),
        # The email's own copy says "<X> has invited you to join"; with no name
        # on file "a colleague" is the honest filler and discloses nothing.
        director_full_name=referrer_name or "a colleague",
        role_label="Physician contributor",
        org_name="Archangel Health",
        specialty=(referrer.get("specialty") or ""),
        onboarding_url=invite_url(code) or portal_base(),
        invitee_email=email,
        referrer_name=referrer_name,
    )
    # The referrer's name is the subject line and the first sentence — that is
    # the entire mechanism, and it is why this is not a cold invite. Already
    # collapsed through ``header_safe`` above, so no CR/LF can reach the header.
    subject = (f"{referrer_name} suggested you'd be a good fit for Asclepius"
               if referrer_name
               else "You're invited to contribute to Asclepius")
    try:
        return bool(await send_html_email(email, subject, html_body))
    except Exception:
        log.exception("asclepius.referrals: invite email failed (the referral row stands)")
        return False


# ═══ The funnel ═══════════════════════════════════════════════════════════════
def _now() -> datetime:
    return datetime.now(timezone.utc)


def sweep_expiries(store, *, referrer_id: str, now: Optional[datetime] = None) -> int:
    """Retire this referrer's invitations that were never taken up.

    Runs on READ rather than on a nightly cron, for the same reason the ledger's
    auto-approve sweep does: this deployment has no scheduler, and a sweep that
    only runs when somebody is looking at the number has always run by the time
    the number is shown. Scoped to one referrer, so a page load costs what that
    physician's own funnel costs and nothing more.
    """
    now = now or _now()
    stamp = now.replace(tzinfo=None, microsecond=0).isoformat()
    cutoff = (now - timedelta(days=expiry_days())).replace(
        tzinfo=None, microsecond=0).isoformat()
    try:
        return store.expire_stale_referrals(
            referrer_id=referrer_id, cutoff=cutoff, resolved_at=stamp)
    except Exception:
        # An expiry sweep that fails must never take the funnel down with it —
        # a stale row is a cosmetic problem, a 500 on Earnings is not.
        log.exception("asclepius.referrals: expiry sweep failed for %s", referrer_id)
        return 0


def funnel(
    store, *, referrer: Dict[str, Any], bounty_cents: int, limit: int = 200,
) -> Dict[str, Any]:
    """One physician's own referrals, and what they are worth.

    Session-scoped by construction: this takes the referrer's own row and there
    is no id parameter anywhere in the call chain, which is the IDOR rule applied
    at the design level rather than validated after the fact.

    ``pending_count``/``pending_cents`` are the whole feature. A referral that has
    not converted yet must render as "+$150 pending", never as absence — a doctor
    who refers two colleagues and sees nothing for a month concludes it is
    broken, and you lose the mechanism that produces most of your supply.
    """
    rows = store.list_referrals_by_referrer(referrer["id"], limit=limit)
    # An equity-holding advisor does not accrue cash — including on referrals.
    # Reporting their referrals as "+$150 pending" would be a promise the
    # compensation model does not keep.
    earns = compensation.accrues_payment(referrer)
    # What the LEDGER paid, per row. One query, and never the live constant: a
    # rate change must not restate a bounty already earned, and the funnel is the
    # surface where the doctor would read the restatement.
    paid_amounts = store.referral_bounty_amounts([r["referral_id"] for r in rows])

    items: List[Dict[str, Any]] = []
    earned = pending = 0
    earned_cents = 0
    for r in rows:
        state = r.get("bounty_state") or BOUNTY_PENDING
        if state == BOUNTY_PENDING:
            if not earns:
                state = BOUNTY_INELIGIBLE
            elif (r.get("status") or "") in _NO_FIRST_CASE_COMING:
                # Derived, not stored — the invitee may yet be approved on a
                # second look, and this state has to be able to fall back to
                # pending when they are.
                state = BOUNTY_CLOSED
        row_cents = paid_amounts.get(r["referral_id"])
        if state == BOUNTY_EARNED:
            earned += 1
            earned_cents += row_cents if row_cents is not None else int(bounty_cents)
        elif state == BOUNTY_PENDING:
            pending += 1
        items.append(public_referral(
            r, bounty_state=state,
            # An unearned row is worth the CURRENT rate if it converts; an earned
            # one is worth what it was actually paid.
            bounty_cents=row_cents if row_cents is not None else int(bounty_cents)))

    return {
        "can_refer": can_refer(referrer),
        "earns_bounty": earns,
        "bounty_cents": int(bounty_cents),
        "referral_code": referrer.get("referral_code"),
        "invite_url": invite_url(referrer.get("referral_code")),
        "referrals": items,
        "total": len(items),
        "earned_count": earned,
        "earned_cents": earned_cents if earns else 0,
        # The line that IS the design. Without it the doctor sees nothing and
        # assumes nothing happened.
        "pending_count": pending,
        "pending_cents": pending * int(bounty_cents) if earns else 0,
    }
