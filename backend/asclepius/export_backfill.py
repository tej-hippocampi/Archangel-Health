"""Label what already exists so the new Export tab can find it (PRD §4).

The three-status split left a specific, countable population behind: cases whose
EARNING says approved (or paid) while `records.status` — the only thing export
reads — still says `submitted`. We have paid for those. They have never been
sellable. Nothing in the product says so.

This is the one idempotent sweep that fixes them. It is:

* **additive** — statuses move forward, nothing is deleted, dropped or renamed
  (§0), and the ids are untouched, which is what the inventory's digest checks;
* **narrow** — only `submitted` / `auto_validated` / `qa_checked` submissions
  with an approved-or-paid ledger row and no shippable record. A `needs_qa`
  submission is a pending human decision and a migration does not get to make
  it; a `void` earning's records are LEFT ALONE (§4.3) and reported instead,
  because a void may have been a payment decision rather than a quality one;
* **idempotent** — a second run finds nothing, because the first run moved every
  row out of the query's own WHERE clause. Safe to run at every boot, which is
  how it actually runs: this deployment has no migration step and a sweep that
  waits for someone to remember it is a sweep that does not happen.

Every move writes an event (`records_backfilled_from_ledger`) naming the prior
status, so the change is reversible by inspection rather than only by backup.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.export_backfill")

EVENT_TYPE = "records_backfilled_from_ledger"


def backfill_records_from_ledger(
    store: Any, *, dry_run: bool = False, limit: int = 20000,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Make the export gate agree with a ledger that already decided.

    Returns a report: ``{"candidates", "moved", "skipped", "voided_untouched",
    "rows"}``. ``dry_run`` computes the report and writes nothing — run it first
    against production, read the count, then run it for real.
    """
    from asclepius import payments as asc_payments  # noqa: PLC0415 — import-light

    candidates: List[Dict[str, Any]] = store.ledger_approved_but_unshippable(limit=limit)
    report: Dict[str, Any] = {
        "candidates": len(candidates), "moved": 0, "skipped": 0,
        "voided_untouched": 0, "dry_run": bool(dry_run), "rows": [],
    }
    for row in candidates:
        sub_id = row["submission_id"]
        entry = {"submission_id": sub_id, "case_id": row.get("task_id"),
                 "prior_status": row.get("submission_status"),
                 "ledger_status": row.get("ledger_status")}
        if dry_run:
            entry["outcome"] = "would_move"
            report["rows"].append(entry)
            continue
        gate = asc_payments.apply_ledger_decision_to_records(
            store, submission_id=sub_id, decision="approve",
            reason="backfilled_from_ledger", actor=actor)
        entry["outcome"] = gate["outcome"]
        if gate["moved"]:
            report["moved"] += 1
            # A SECOND event beside the generic ``export_ready`` one, naming this
            # sweep. An operator auditing "why is this case exportable" must be
            # able to tell a migration from a decision a person made.
            store.log_event(
                entity_type="submission", entity_id=sub_id, event_type=EVENT_TYPE,
                actor=actor,
                payload={"earning_id": row.get("earning_id"),
                         "ledger_status": row.get("ledger_status"),
                         "prior_status": row.get("submission_status"),
                         "user_id": row.get("user_id")},
            )
        else:
            report["skipped"] += 1
        report["rows"].append(entry)

    # §4.3 — reported, never changed.
    try:
        report["voided_untouched"] = len(store.voided_with_live_records(limit=limit))
    except Exception:  # noqa: BLE001 — a report line must not fail a migration
        log.warning("export backfill: could not count voided-with-live-records",
                    exc_info=True)

    if report["candidates"]:
        log.warning(
            "asclepius.export_backfill: %d case(s) were approved or paid but could "
            "not ship; moved %d to export_ready%s. %d voided earning(s) still have "
            "live records and were LEFT ALONE (they surface in the export "
            "preview's excluded list).",
            report["candidates"], report["moved"],
            " (dry run — nothing written)" if dry_run else "",
            report["voided_untouched"])
    return report


def run_once_at_boot(store: Any) -> Dict[str, Any]:
    """Boot hook: inventory, sweep, inventory, verify — in one pass.

    **The §0 contract has to be taken around the sweep, by the sweep.** Running
    the inventory script by hand "before the deploy" cannot work: the script
    ships WITH this change, so at the moment a before-snapshot is needed it is
    not deployed yet, and by the time it is, the boot sweep has already run.
    Both snapshots would be "after". So the before-snapshot is taken here,
    inside the same process, immediately before the first write — which is the
    only place it is actually before anything.

    The result lands in the log and on ``app.state.asclepius_export_backfill``,
    readable at ``GET /api/asclepius/admin/export/migration-report``. Nobody has
    to SSH into a container to find out whether their data survived.

    Never raises: a migration that can take the portal down is a worse problem
    than the drift it fixes.
    """
    from asclepius import export_inventory  # noqa: PLC0415 — import-light

    empty = {"candidates": 0, "moved": 0, "skipped": 0, "voided_untouched": 0,
             "rows": []}
    try:
        before = export_inventory.inventory(store)
    except Exception:  # noqa: BLE001
        log.warning("asclepius.export_backfill: could not take the before "
                    "inventory; running the sweep without a contract check",
                    exc_info=True)
        before = None

    try:
        report = backfill_records_from_ledger(store, actor="boot_migration")
    except Exception:  # noqa: BLE001
        log.exception("asclepius.export_backfill: boot sweep failed; the portal "
                      "serves as-is and the sweep retries on the next restart")
        return {**empty, "error": True, "contract": None}

    report["contract"] = None
    if before is not None:
        try:
            after = export_inventory.inventory(store)
            problems = export_inventory.violations(before, after)
            report["contract"] = {
                "ok": not problems, "problems": problems,
                "before": before["id_sets"], "after": after["id_sets"],
                "taken_at": after["taken_at"],
            }
            if problems:
                # This should be impossible — the sweep only ever UPDATEs a
                # status column. If it ever fires, it is the loudest thing in
                # the log, because §0 is the promise the whole migration rests
                # on and a silent breach of it is unrecoverable.
                log.error("asclepius.export_backfill: THE NO-DATA-LOSS CONTRACT "
                          "FAILED. %s", "; ".join(problems))
            else:
                log.warning(
                    "asclepius.export_backfill: no-data-loss contract holds — "
                    "every id set is identical across the sweep.")
        except Exception:  # noqa: BLE001
            log.warning("asclepius.export_backfill: could not verify the "
                        "no-data-loss contract", exc_info=True)

    # The headline, in one greppable line: how many cases we had already paid
    # for that could not ship until now.
    log.warning(
        "asclepius.export_backfill: SUMMARY — %d case(s) were approved or paid "
        "but could not ship; %d are now exportable; %d voided earning(s) left "
        "untouched for a human to decide.",
        report["candidates"], report["moved"], report["voided_untouched"])
    return report
