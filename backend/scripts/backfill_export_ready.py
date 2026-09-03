#!/usr/bin/env python3
"""Run the PRD §4 export backfill by hand.

    # look, change nothing:
    ASCLEPIUS_DB_PATH=/data/asclepius.db \
      python3 backend/scripts/backfill_export_ready.py --dry-run

    # do it:
    ASCLEPIUS_DB_PATH=/data/asclepius.db \
      python3 backend/scripts/backfill_export_ready.py

The same sweep runs automatically at server boot, so this script exists for the
case where you want to SEE the number before a deploy, or re-run it against a
copy of the database. It is idempotent either way: a second run finds nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from asclepius import export_backfill                      # noqa: E402
from asclepius.store import AsclepiusStore                 # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would move; write nothing")
    ap.add_argument("--db", default=None, help="database path (default $ASCLEPIUS_DB_PATH)")
    ap.add_argument("--verbose", action="store_true", help="list every affected case")
    args = ap.parse_args()

    store = AsclepiusStore(db_path=args.db) if args.db else AsclepiusStore()
    report = export_backfill.backfill_records_from_ledger(
        store, dry_run=args.dry_run, actor="manual_script")

    print(f"candidates       : {report['candidates']}")
    print(f"moved            : {report['moved']}"
          + ("  (dry run — nothing written)" if args.dry_run else ""))
    print(f"skipped          : {report['skipped']}")
    print(f"voided untouched : {report['voided_untouched']}  "
          "(§4.3 — surfaced in the export preview, never auto-rejected)")
    if args.verbose:
        for row in report["rows"]:
            print(f"  {row['submission_id']}  case={row.get('case_id')}  "
                  f"{row.get('prior_status')} -> {row.get('outcome')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
