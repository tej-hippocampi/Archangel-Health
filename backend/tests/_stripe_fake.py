"""A stand-in for the ``stripe`` SDK, so the payout rail is testable with no
network, no keys, and no dependency installed.

It is installed into ``sys.modules`` under the name ``stripe``, which means the
lazy ``import stripe`` inside ``asclepius.stripe_rail.sdk`` picks it up and every
test exercises the REAL call path, including the api-key assignment and the
error translation. A fake injected at the ``stripe_rail`` boundary instead would
test the mock.

Two behaviors are modelled rather than stubbed, because they are the two things
the rail's correctness rests on:

* **idempotency keys.** ``Transfer.create`` with a key it has seen returns the
  first transfer and moves no second dollar, exactly as Stripe does. That is
  what makes a retried batch and a double-clicked retry button safe, so a fake
  that ignored the key would let a real double-payment bug pass.
* **signature verification.** ``Webhook.construct_event`` recomputes an HMAC
  over the body. A header lifted from a different payload fails, which is the
  actual attack an unsigned-webhook test is about.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import types
from typing import Any, Dict, List, Optional


class StripeFakeError(Exception):
    """Stands in for stripe.error.StripeError.

    Carries ``user_message`` and ``code`` because that is what the rail's
    failure-reason extraction reads, and a queue of failed payouts is only
    useful if the reasons in it are sentences an operator can act on.
    """

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.user_message = message
        self.code = code


def sign(payload: bytes, secret: str) -> str:
    """The header Stripe would send for this exact body."""
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"t=1,v1={mac}"


class FakeStripe:
    """The recorder every test asserts against."""

    def __init__(self, *, webhook_secret: str):
        self.webhook_secret = webhook_secret
        self.api_keys: List[str] = []
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.account_create_calls: List[Dict[str, Any]] = []
        self.account_link_calls: List[Dict[str, Any]] = []
        #: idempotency key -> the transfer object returned the FIRST time.
        self.transfers: Dict[str, Dict[str, Any]] = {}
        #: every create call, including the ones deduplicated by key.
        self.transfer_calls: List[Dict[str, Any]] = []
        #: earning id -> reason, to make one row's transfer fail.
        self.transfer_failures: Dict[str, str] = {}

    # ── test-side controls ────────────────────────────────────────────────
    def set_account_state(self, account_id: str, **fields: Any) -> None:
        account = self.accounts.setdefault(
            account_id, {"id": account_id, "payouts_enabled": False,
                         "details_submitted": False, "requirements": {}})
        account.update(fields)

    def fail_transfer_for(self, earning_id: str, reason: str) -> None:
        self.transfer_failures[earning_id] = reason

    def clear_transfer_failures(self) -> None:
        self.transfer_failures.clear()

    @property
    def settled_transfer_count(self) -> int:
        """How many dollars-moving transfers actually happened, as opposed to
        how many times the code asked for one."""
        return len(self.transfers)


def install(monkeypatch, *, secret_key: str = "sk_test_rail",
            webhook_secret: str = "whsec_test_rail",
            enabled: bool = True) -> FakeStripe:
    """Point the rail at the fake and turn the flag on. Returns the recorder."""
    fake = FakeStripe(webhook_secret=webhook_secret)

    class Account:
        @staticmethod
        def create(**kw: Any) -> Dict[str, Any]:
            fake.account_create_calls.append(kw)
            account_id = f"acct_{len(fake.accounts) + 1}"
            fake.accounts[account_id] = {
                "id": account_id,
                # A brand new Express account can do nothing yet, which is the
                # state the onboarding leg of the state machine has to handle.
                "payouts_enabled": False,
                "details_submitted": False,
                "requirements": {"disabled_reason": None},
                "metadata": kw.get("metadata") or {},
            }
            return dict(fake.accounts[account_id])

        @staticmethod
        def retrieve(account_id: str) -> Dict[str, Any]:
            if account_id not in fake.accounts:
                raise StripeFakeError(f"No such account: {account_id}")
            return dict(fake.accounts[account_id])

    class AccountLink:
        @staticmethod
        def create(**kw: Any) -> Dict[str, Any]:
            fake.account_link_calls.append(kw)
            # A new URL every call, because account links are single use. Tests
            # assert the second call is a new link rather than a stored one.
            return {"url": f"https://connect.stripe.test/setup/{len(fake.account_link_calls)}",
                    "expires_at": 1800000000 + len(fake.account_link_calls)}

    class Transfer:
        @staticmethod
        def create(*, idempotency_key: Optional[str] = None, **kw: Any) -> Dict[str, Any]:
            fake.transfer_calls.append({"idempotency_key": idempotency_key, **kw})
            if idempotency_key in fake.transfers:
                return dict(fake.transfers[idempotency_key])
            earning_id = (kw.get("metadata") or {}).get("earning_id")
            reason = fake.transfer_failures.get(earning_id)
            if reason:
                raise StripeFakeError(reason, code="transfer_failed")
            transfer_id = f"tr_{len(fake.transfers) + 1}"
            transfer = {"id": transfer_id, "amount": kw.get("amount"),
                        "currency": kw.get("currency"),
                        "destination": kw.get("destination"),
                        "transfer_group": kw.get("transfer_group"),
                        "metadata": kw.get("metadata") or {}, "reversed": False}
            fake.transfers[idempotency_key] = transfer
            return dict(transfer)

    class Webhook:
        @staticmethod
        def construct_event(payload: Any, signature: str, secret: str) -> Dict[str, Any]:
            raw = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
            if not hmac.compare_digest(sign(raw, secret), signature or ""):
                raise StripeFakeError("No signatures found matching the expected signature.")
            return json.loads(raw.decode("utf-8"))

    def _set_api_key(value: str) -> None:
        fake.api_keys.append(value)

    # api_key is a plain attribute on the real SDK; recording writes to it is how
    # a test proves the rail configured the client rather than relying on ambient
    # environment state.
    class _Module(types.ModuleType):
        @property
        def api_key(self):  # noqa: D401 - mirrors the SDK's plain attribute
            return fake.api_keys[-1] if fake.api_keys else None

        @api_key.setter
        def api_key(self, value):
            _set_api_key(value)

    recording = _Module("stripe")
    recording.Account = Account
    recording.AccountLink = AccountLink
    recording.Transfer = Transfer
    recording.Webhook = Webhook

    monkeypatch.setitem(sys.modules, "stripe", recording)
    monkeypatch.setenv("ASCLEPIUS_STRIPE_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("STRIPE_SECRET_KEY", secret_key)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", webhook_secret)
    return fake


def event_body(event_id: str, event_type: str, obj: Dict[str, Any]) -> bytes:
    """A webhook body in the shape Stripe posts."""
    return json.dumps({"id": event_id, "type": event_type,
                       "data": {"object": obj}}).encode("utf-8")
