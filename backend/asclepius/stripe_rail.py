"""The payout rail: Stripe Connect Express, behind ``ASCLEPIUS_STRIPE_ENABLED``.

THE ONLY MODULE IN THIS CODEBASE THAT IMPORTS ``stripe``, and it imports it
lazily, inside the flag. With the rail dark the package may be absent or broken
and every caller here still behaves exactly as it did before the rail existed;
the dependency is pinned in requirements but never load-bearing while off. That
is not a style preference. It is the property that lets this ship to production
in the same deploy as the code that will one day move money.

WHAT WE STORE, IN FULL: a physician's Connect account id, and a status word.
Not a bank account number, not a routing number, not an SSN, not an EIN, not a
TIN. Stripe collects tax identity during Express onboarding and files the
1099-NECs, and delegating that is only defensible while we hold nothing worth
breaching. Anything richer than id plus status is READ from Stripe at the
moment an admin asks and never cached, because a cached copy of compliance
state is a stale copy from the moment Stripe updates it.

FAILURE IS LOUD. Flag on with a missing key or a missing package raises at the
first Stripe call. A payout rail that silently declines to pay looks identical
to one that is working, right up until a physician asks where their money is.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from asclepius import constants

log = logging.getLogger("asclepius.stripe_rail")

# ─── The bank-link state machine (PRD §B3) ────────────────────────────────────
# coming_soon -> onboarding -> active | restricted, and restricted can recover to
# active. ``coming_soon`` is the waiting list the placeholder interest endpoint
# has been collecting since before there was a rail, so those rows keep their
# meaning rather than being migrated into a state that implies they started
# something they could not start.
COMING_SOON = "coming_soon"
ONBOARDING = "onboarding"
ACTIVE = "active"
RESTRICTED = "restricted"

#: Every value ``users.bank_link_status`` may hold. NULL still means "never asked".
STATUSES = (COMING_SOON, ONBOARDING, ACTIVE, RESTRICTED)


class RailUnavailable(RuntimeError):
    """The flag is on and the rail cannot be used. Never swallowed."""


class TransferFailed(RuntimeError):
    """Stripe refused one transfer. Carries the reason a human needs to act on.

    Deliberately NOT a subclass of RailUnavailable: a declined transfer is a
    reconciliation item on one ledger row, while an unusable rail is a
    configuration incident. Conflating them would let a single bad destination
    account read as "payouts are down".
    """

    def __init__(self, reason: str, *, code: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class SignatureInvalid(RuntimeError):
    """A webhook body did not verify against ``STRIPE_WEBHOOK_SECRET``."""


def enabled() -> bool:
    return constants.stripe_enabled()


def secret_key() -> str:
    return (os.getenv("STRIPE_SECRET_KEY") or "").strip()


def webhook_secret() -> str:
    return (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()


def sdk(*, need_webhook_secret: bool = False):
    """The configured ``stripe`` module, or a loud failure.

    Every entry point in this file goes through here, which is what makes "the
    SDK is imported lazily, inside the flag" a property of the module rather
    than a habit. Called with the flag off it raises rather than returning a
    half-configured client: a caller that reached a Stripe call while dark has
    a bug in its own gate, and returning something usable would hide it.
    """
    if not enabled():
        raise RailUnavailable(
            "The Stripe payout rail is off (ASCLEPIUS_STRIPE_ENABLED=0). "
            "Nothing should be calling Stripe.")
    key = secret_key()
    if not key:
        raise RailUnavailable(
            "ASCLEPIUS_STRIPE_ENABLED is on but STRIPE_SECRET_KEY is not set. "
            "The rail cannot move money without it.")
    if need_webhook_secret and not webhook_secret():
        raise RailUnavailable(
            "ASCLEPIUS_STRIPE_ENABLED is on but STRIPE_WEBHOOK_SECRET is not set. "
            "Webhooks cannot be verified without it, and an unverified webhook is "
            "an unauthenticated write path.")
    try:
        import stripe                                    # noqa: PLC0415
    except ImportError as exc:
        raise RailUnavailable(
            "ASCLEPIUS_STRIPE_ENABLED is on but the stripe package is not "
            "installed. Add the pinned requirement and redeploy.") from exc
    stripe.api_key = key
    return stripe


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field off a Stripe object or off the plain dict a test hands us.

    Stripe's SDK objects are dict-like but not dicts, and the webhook payloads a
    test constructs are dicts. One reader for both keeps the mapping logic
    identical in production and under test, which is the only way a mocked test
    of a money path is worth anything.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except (KeyError, TypeError, AttributeError):
        return getattr(obj, key, default)


# ─── Onboarding (PRD §B1, §B2) ────────────────────────────────────────────────
def create_express_account(*, email: Optional[str] = None,
                           user_id: Optional[str] = None) -> str:
    """Create the physician's Connect Express account and return ITS ID ONLY.

    The return type is the whole point: a string. Everything else Stripe hands
    back about the account is compliance state we deliberately do not keep.
    """
    stripe = sdk()
    account = stripe.Account.create(
        type="express",
        email=email or None,
        # Express, so Stripe owns the onboarding form, the identity documents and
        # the tax forms. Requesting card_payments would make us a merchant of
        # record for these accounts, which is a different regulatory posture for
        # no benefit: we send money out and never take it in.
        capabilities={"transfers": {"requested": True}},
        business_type="individual",
        # Our id, on their object, so a Stripe-side investigation can be traced
        # back without us storing a second copy of their identity.
        metadata={"asclepius_user_id": user_id or ""},
    )
    account_id = _field(account, "id")
    if not account_id:
        raise RailUnavailable("Stripe returned an account with no id.")
    return str(account_id)


def create_account_link(account_id: str, *, portal_url: str) -> Dict[str, Any]:
    """A FRESH onboarding link, every call.

    Account links are single-use and expire in minutes, which is why B1 is
    "create the account once, mint a link every time" rather than storing a URL.
    A stored link is a link that has already expired by the time a physician
    finds the email telling them to click it.
    """
    stripe = sdk()
    base = (portal_url or "").rstrip("/")
    link = stripe.AccountLink.create(
        account=account_id,
        # Refresh is where Stripe sends a physician whose link expired mid-form.
        # It points back at the portal's earnings surface, which re-POSTs start
        # and mints another link, so an expiry is a round trip rather than a
        # dead end.
        refresh_url=f"{base}/#earnings",
        return_url=f"{base}/#earnings",
        type="account_onboarding",
    )
    return {"url": _field(link, "url"), "expires_at": _field(link, "expires_at")}


def retrieve_account(account_id: str) -> Any:
    stripe = sdk()
    return stripe.Account.retrieve(account_id)


def status_for_account(account: Any) -> str:
    """Map a Connect account onto the bank-link state machine.

    Three inputs, in priority order, because they answer different questions:
    a ``disabled_reason`` is Stripe telling us it will not pay this person until
    something is fixed, and it outranks a stale ``payouts_enabled``. Absent
    both, the account exists but has not finished onboarding.
    """
    requirements = _field(account, "requirements") or {}
    disabled_reason = _field(requirements, "disabled_reason")
    if disabled_reason:
        return RESTRICTED
    if bool(_field(account, "payouts_enabled")):
        return ACTIVE
    return ONBOARDING


def account_public_state(account: Any) -> Dict[str, Any]:
    """The live, never-cached read behind ``GET /me/bank-link``.

    Read at the moment of the question and thrown away after answering it. The
    physician-facing surface gets booleans and a reason string, never the
    requirements list, which names identity documents.
    """
    requirements = _field(account, "requirements") or {}
    return {
        "payouts_enabled": bool(_field(account, "payouts_enabled")),
        "details_submitted": bool(_field(account, "details_submitted")),
        "disabled_reason": _field(requirements, "disabled_reason"),
    }


# ─── Transfers (PRD §C, G3) ───────────────────────────────────────────────────
def idempotency_key(earning_id: str) -> str:
    """``earning:{id}``. One ledger row, one transfer, forever.

    Derived from the row rather than generated per attempt, which is what makes
    a retried batch safe: rows that already transferred are no-ops at Stripe,
    rows that failed are genuinely retried, and a double-clicked retry button
    cannot pay twice.
    """
    return f"earning:{earning_id}"


def create_transfer(
    *, earning_id: str, amount_cents: int, destination: str,
    payout_batch_id: Optional[str] = None, user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Move money for ONE ledger row. Raises TransferFailed on a refusal.

    ``transfer_group`` is the payout batch id, so Stripe's ledger reconciles
    against ``GET /admin/earnings?payout_batch_id=...`` one row at a time. A
    single batch-sum transfer would be cheaper to create and would turn a
    partial failure into manual arithmetic.
    """
    stripe = sdk()
    try:
        transfer = stripe.Transfer.create(
            amount=int(amount_cents),
            currency="usd",
            destination=destination,
            transfer_group=payout_batch_id or None,
            metadata={"earning_id": earning_id, "asclepius_user_id": user_id or ""},
            idempotency_key=idempotency_key(earning_id),
        )
    except Exception as exc:
        raise TransferFailed(_failure_reason(exc), code=_failure_code(exc)) from exc
    transfer_id = _field(transfer, "id")
    if not transfer_id:
        raise TransferFailed("Stripe returned a transfer with no id.")
    return {
        "transfer_id": str(transfer_id),
        "amount_cents": int(_field(transfer, "amount", amount_cents) or amount_cents),
        "reversed": bool(_field(transfer, "reversed")),
    }


def _failure_reason(exc: Exception) -> str:
    """One human sentence an operator can act on.

    Stripe's exceptions carry a ``user_message`` when there is one worth showing
    and a str() that is at least specific; both beat "an error occurred", which
    is what a queue of failed payouts must never be full of.
    """
    for attr in ("user_message", "_message"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    return str(exc) or exc.__class__.__name__


def _failure_code(exc: Exception) -> Optional[str]:
    code = getattr(exc, "code", None)
    return str(code) if code else None


# ─── Webhooks (PRD §D) ────────────────────────────────────────────────────────
def construct_event(payload: bytes, signature: Optional[str]) -> Dict[str, Any]:
    """Verify and parse a webhook. NEVER trusts the JSON body.

    The signature is the only thing separating this route from an
    unauthenticated write path into the payout ledger, so the event handlers are
    given the object Stripe's own verifier constructed and are not reachable any
    other way.
    """
    stripe = sdk(need_webhook_secret=True)
    if not signature:
        raise SignatureInvalid("No Stripe-Signature header.")
    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret())
    except Exception as exc:
        raise SignatureInvalid(str(exc) or "Signature verification failed.") from exc
    return _as_event_dict(event)


def _as_event_dict(event: Any) -> Dict[str, Any]:
    """Normalize a verified event into the plain shape the handlers read."""
    data = _field(event, "data") or {}
    return {
        "id": _field(event, "id"),
        "type": _field(event, "type"),
        "object": _field(data, "object") or {},
    }


#: Transfer event types the rail acts on. ``transfer.reversed`` is here for
#: VISIBILITY only (G7): a reversal is a Stripe-dashboard treasury operation and
#: writes no ledger row, because a ledger that auto-mutated on reversal would
#: contradict the attributed, human-decided shape of every other money action in
#: this system.
TRANSFER_EVENTS = ("transfer.created", "transfer.updated", "transfer.reversed")


def transfer_status_from_event(event_type: str, obj: Any) -> str:
    """What a transfer event says the attempt's status now is."""
    if event_type == "transfer.reversed":
        return "reversed"
    if bool(_field(obj, "reversed")):
        return "reversed"
    return "transferred"
