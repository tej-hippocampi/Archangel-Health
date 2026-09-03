"""What a health system is owed, and how a settlement gets recorded.

─── Why this module exists at all ────────────────────────────────────────────
The health-system payout rail fell through a mutual deferral.
``docs/prds/prd-health-systems.md`` sent it to a later group; the payments-rail
PRD sent it back out of scope as "a different rail decision for a different
counterparty size". Meanwhile ``prd-health-systems.md`` left accrual manual, so
nothing computed what a partner was owed either, and the only path from
"we accepted your data" to "here is money" was an operator typing a number into
a box. A number typed into a box cannot be recomputed, cannot be checked, and
gives a hospital's finance contact nothing to reconcile their own records
against.

─── Why it is not asclepius/payments.py ──────────────────────────────────────
That module owns physician money and says so. Every path in it assumes
physician semantics: a user_id, a quality multiplier, a fourteen-day
auto-approve, a Connect account. A health system has none of those. Threading a
discriminator through the physician ledger would put a branch inside the money
path that every future edit has to remember, and forgetting it once pays the
wrong counterparty. This module imports nothing from it, and nothing from the
physician Stripe rail on the parallel branch.

What IS copied is the SHAPE, deliberately, because it is proven:

  1. An append-only ledger row per accrued item, never a running total.
  2. The rate STAMPED ON THE ROW at accrual. A price change decides what the
     next upload is worth and nothing about what a settled one was.
  3. Compare-and-set on settlement, with the settlement reference as the
     idempotency key, so a double-submit records once.

─── Where a business decision was required ───────────────────────────────────
Three, and each is behind a named constant so a founder can change it without
reading this file. Each was resolved to the option that cannot overpromise.

  * THE UNIT is one accepted upload bundle (``ACCRUAL_UNIT``). Per-case was the
    alternative and it is worse here: the accepted-upload count is already the
    number both the uploads page and the payouts page show a partner, it is
    stable once the pipeline finishes with a bundle, and a per-case rate
    multiplies by a count the pipeline can revise after acceptance, which would
    restate an accrual we had already shown somebody.

  * THE PRICE IS NOT SET. ``ASCLEPIUS_HS_UPLOAD_RATE_CENTS`` defaults to 0 and a
    rate of zero accrues NOTHING: no rows, no figures, and the portal keeps
    showing exactly what it shows today, a count of accepted uploads awaiting
    pricing. No default figure is invented anywhere in this file, because a
    price nobody agreed to, printed on the page a hospital's finance contact
    reads, is a price that gets quoted back at us.

  * INVOICE TERMS ARE NOT PROMISED. An invoice here records a period, an amount
    and the date it was issued. It carries no due date and no net-N terms,
    because those are negotiated per agreement and a default one would be a
    commitment nobody made.

─── The rail, and the seam where a real one attaches ─────────────────────────
Invoice-and-record. The portal shows what is accrued, what is invoiced and what
has settled; an admin records settlement when the transfer clears out of band.
We store NO account number, routing number, IBAN or tax identifier, for a health
system any more than for a physician, and ``test_hs_accrual_rail`` asserts the
absence mechanically rather than trusting this paragraph.

``SETTLEMENT_RAIL`` names the current answer. An automated rail attaches at
``record_settlement``: that function is the only place a settlement is written,
so a processor callback becomes one more caller of it rather than a second path
into the ledger.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.hs_billing")

#: What one ledger row is for. Changing this is a pricing-model decision, not a
#: refactor: the ledger's UNIQUE constraint is keyed on (kind, ref), so a
#: different unit is a different set of rows entirely.
ACCRUAL_UNIT = "upload"

#: How a settlement reaches a partner today. Not a Stripe transfer and not an
#: ACH origination from this codebase: an invoice, a bank transfer somebody
#: performs, and a record of it here.
SETTLEMENT_RAIL = "invoice_and_record"

#: The environment variable that would set a price. Deliberately unset, and
#: deliberately defaulting to zero rather than to a guess.
RATE_ENV = "ASCLEPIUS_HS_UPLOAD_RATE_CENTS"

#: What the portal says about how settlement works. It states the mechanism and
#: refuses to state a schedule, because the schedule is in the agreement each
#: partner signed and no two of them have to match.
SETTLEMENT_NOTE = (
    "We invoice for accepted data and settle by bank transfer against that "
    "invoice, on the terms in your signed agreement. We never ask for account "
    "numbers or tax identifiers through this portal, and we do not store them."
)

#: What the portal says when nothing has been priced yet. The honest empty
#: state, and the one most likely to get softened later into a promise.
UNPRICED_NOTE = (
    "Your accepted data has not been priced yet. Our team agrees a price with "
    "you before anything becomes an amount owed, so nothing here is an offer."
)

ACCRUED = "accrued"
INVOICED = "invoiced"
SETTLED = "settled"
VOID = "void"

#: The pipeline status that means a bundle is ours and finished with. Read from
#: the portal's own vocabulary rather than redefined, so accrual and the page a
#: partner checks it against cannot disagree about what "accepted" means.
ACCEPTED_UPLOAD_STATUS = "ingested"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat()


def _env_rate_cents() -> int:
    """The fallback price per accepted upload, in cents. Zero unless somebody
    deliberately sets one, and a malformed value is zero rather than a guess."""
    raw = (os.getenv(RATE_ENV) or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        log.warning("asclepius.hs_billing: %s is not an integer (%r); treating as unpriced",
                    RATE_ENV, raw)
        return 0
    return value if value > 0 else 0


def rate_for(store, hs_id: str) -> int:
    """The price in force RIGHT NOW for this organization, in cents.

    Zero means not priced, which is a real answer and the default one. The
    per-organization figure wins over the environment because a data licence is
    negotiated one partner at a time, and an environment variable that silently
    priced every partner the same would be the wrong default in both directions.
    """
    hs = store.get_health_system(hs_id) or {}
    own = hs.get("data_rate_cents")
    if own is not None:
        return max(0, int(own))
    return _env_rate_cents()


def reconcile_accruals(store, *, hs_id: str, now: Optional[datetime] = None) -> Dict[str, int]:
    """Materialise the ledger rows this organization's accepted uploads have
    earned. Idempotent, and safe to call on every read.

    Derived from the uploads table rather than hooked into the ingestion
    pipeline, for the reason ``payments.reconcile_task_accruals`` gives and which
    holds here too: a read is a contract-free dependency where a callback is
    not, it back-fills bundles that predate this feature, and it cannot be
    bypassed by a second ingestion path. Its one cost is that the rate is
    stamped when the sweep first observes the upload rather than at the instant
    the pipeline finished, which is bounded by how often anyone opens the
    payouts page and matters only if a price changed inside that window.

    ``accrued_at`` is the moment the upload was ACCEPTED, not the moment this
    sweep noticed it, so a backfill does not date a partner's ledger by our
    deploy schedule.

    Nothing is written while the organization is unpriced. That is the whole
    conservative posture: a zero-value row is not an obligation, it is a number
    we would then have to explain.
    """
    counts = {"accrued": 0, "skipped_unpriced": 0}
    rate = rate_for(store, hs_id)
    uploads = [u for u in store.list_uploads_for_health_system(hs_id)
               if (u.get("status") or "") == ACCEPTED_UPLOAD_STATUS]
    if rate <= 0:
        counts["skipped_unpriced"] = len(uploads)
        return counts

    now = now or _now()
    already = store.hs_accrued_upload_ids(hs_id)
    for up in uploads:
        upload_id = up.get("upload_id")
        if not upload_id or upload_id in already:
            continue
        accepted_at = up.get("updated_at") or up.get("created_at") or _ts(now)
        written = store.insert_hs_accrual(
            accrual_id="hsacc_" + uuid.uuid4().hex[:20],
            hs_id=hs_id, ref_kind=ACCRUAL_UNIT, ref_id=upload_id,
            # STAMPED. The row is worth what the price was when the work landed
            # in our ledger, forever, and a later price change cannot restate it.
            rate_cents=rate, amount_cents=rate,
            accrued_at=accepted_at,
            description="Accepted data bundle",
        )
        if written is None:
            continue
        counts["accrued"] += 1
        store.log_event(
            entity_type="health_system", entity_id=hs_id,
            event_type="hs_accrual_written", actor=None,
            payload={"accrual_id": written["accrual_id"], "upload_id": upload_id,
                     "rate_cents": rate, "unit": ACCRUAL_UNIT},
        )
    return counts


def invoice_open_accruals(
    store, *, hs_id: str, period: str, created_by: str,
    description: Optional[str] = None, now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Roll everything currently accrued into one invoice for ``period``.

    Returns None when there is nothing open to bill, and raises nothing when the
    period is already invoiced: ``UNIQUE(hs_id, period)`` on ``hs_invoices`` is
    the double-billing guard and the caller gets None rather than a second
    invoice for a month it already billed.

    The invoice is created ``sent``, not ``draft``. A draft is a number an
    operator is still deciding about and the portal filters those out, so an
    invoice nobody can see is one the partner cannot reconcile against. If a
    review step is wanted later it belongs before this call, not as a state a
    partner-facing total quietly excludes.
    """
    now = now or _now()
    open_rows = store.list_hs_accruals(hs_id, status=ACCRUED)
    if not open_rows:
        return None
    total = sum(int(r["amount_cents"]) for r in open_rows)
    invoice = store.create_hs_invoice(
        hs_id=hs_id, period=period, amount_cents=total, created_by=created_by,
        description=description or f"Accepted data, {len(open_rows)} bundles",
        status="sent")
    if invoice is None:
        return None
    moved = store.attach_hs_accruals_to_invoice(
        hs_id=hs_id, invoice_id=invoice["invoice_id"], invoiced_at=_ts(now),
        accrual_ids=[r["accrual_id"] for r in open_rows])
    store.log_event(
        entity_type="health_system", entity_id=hs_id,
        event_type="hs_invoice_issued", actor=created_by,
        payload={"invoice_id": invoice["invoice_id"], "period": period,
                 "amount_cents": total, "accruals": len(moved)},
    )
    return {"invoice": invoice, "accruals": len(moved)}


def record_settlement(
    store, *, hs_id: str, settlement_ref: str, actor: Optional[str] = None,
    accrual_ids: Optional[List[str]] = None, invoice_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Record that a transfer against this organization's ledger cleared.

    THE SEAM. This is the only place a settlement is written, so an automated
    rail attaches by becoming one more caller rather than a second path into the
    ledger. It moves no money and stores no credential; ``settlement_ref`` is
    whatever the treasury side calls the transfer, and it is what reconciles the
    two records afterwards.
    """
    now = now or _now()
    result = store.settle_hs_accruals(
        hs_id=hs_id, settlement_ref=settlement_ref, settled_at=_ts(now),
        accrual_ids=accrual_ids, invoice_id=invoice_id)

    # The invoice follows its accruals rather than being marked separately: an
    # invoice whose every line settled is paid, and one with a line outstanding
    # is not, and deriving that here is how the two records cannot disagree.
    for inv_id in {r.get("invoice_id") for r in result["settled"] if r.get("invoice_id")}:
        remaining = [r for r in store.list_hs_accruals(hs_id)
                     if r.get("invoice_id") == inv_id and r["status"] in (ACCRUED, INVOICED)]
        if not remaining:
            store.set_hs_invoice_status(inv_id, "paid")

    if result["settled"]:
        store.log_event(
            entity_type="health_system", entity_id=hs_id,
            event_type="hs_settlement_recorded", actor=actor,
            payload={"settlement_ref": result["settlement_ref"],
                     "count": len(result["settled"]),
                     "amount_cents": result["amount_cents"],
                     "rail": SETTLEMENT_RAIL},
        )
        log.warning("asclepius.hs_billing: settlement %s recorded for %s "
                    "(%d rows, %d cents) by %s", result["settlement_ref"], hs_id,
                    len(result["settled"]), result["amount_cents"], actor or "unknown")
    return {
        "settlement_ref": result["settlement_ref"],
        "settled": len(result["settled"]),
        "amount_cents": result["amount_cents"],
        "already_in_ref": result["already_in_ref"],
        "skipped": result["skipped"],
    }


def partner_rail(store, hs_id: str) -> Dict[str, Any]:
    """The three numbers a partner may see, and the words that go with them.

    ``priced`` is the load-bearing field. False means nobody has agreed a price
    with this organization, every figure below is zero, and the portal must show
    the accepted-upload COUNT instead: three zeroes on a money page read as "you
    are owed nothing", which is a different and false statement.
    """
    rate = rate_for(store, hs_id)
    summary = store.hs_accrual_summary(hs_id)
    return {
        "priced": rate > 0,
        "accrued_cents": summary["accrued_cents"],
        "invoiced_cents": summary["invoiced_cents"],
        "settled_cents": summary["settled_cents"],
        "outstanding_cents": summary["outstanding_cents"],
        "count": summary["count"],
        # The unit, in words a partner reads, because "$400 accrued" without it
        # invites the question we would rather answer on the page.
        "unit": "accepted data bundle",
        "note": SETTLEMENT_NOTE if rate > 0 else UNPRICED_NOTE,
    }
