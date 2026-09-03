#!/usr/bin/env python3
"""Take (and commit) the export-migration inventory — PRD §0, step zero.

    python3 backend/scripts/export_migration_inventory.py --label before
    …run the migration…
    python3 backend/scripts/export_migration_inventory.py --label after

The first run writes `docs/asclepius/EXPORT_MIGRATION_INVENTORY.md`; the second
APPENDS its run to the same file and prints the contract check. Nothing here
writes to the database — it is a read, on purpose, so it is safe to run against
production at any time.

Set `ASCLEPIUS_DB_PATH` to point at the database you mean; without it the store
resolves to `backend/asclepius.db`, which on a Railway deploy is the ephemeral
copy beside the code rather than the volume.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from asclepius import export_inventory as inv_mod          # noqa: E402
from asclepius.store import AsclepiusStore                 # noqa: E402

_DEFAULT_DOC = _BACKEND.parent / "docs" / "asclepius" / "EXPORT_MIGRATION_INVENTORY.md"
_STATE_SUFFIX = ".runs.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="before",
                    help="which pass this is (before / after / any name)")
    ap.add_argument("--out", default=str(_DEFAULT_DOC),
                    help="markdown report to write (default docs/asclepius/…)")
    ap.add_argument("--db", default=None,
                    help="database path (default: $ASCLEPIUS_DB_PATH)")
    ap.add_argument("--json", action="store_true",
                    help="also print the raw inventory as JSON")
    args = ap.parse_args()

    store = AsclepiusStore(db_path=args.db) if args.db else AsclepiusStore()
    snapshot = inv_mod.inventory(store)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Runs accumulate in a sidecar so a later pass can render the contract check
    # against the earlier one without re-parsing markdown.
    state = out.with_suffix(out.suffix + _STATE_SUFFIX)
    runs = []
    if state.exists():
        try:
            runs = json.loads(state.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            runs = []
    runs = [r for r in runs if r.get("label") != args.label]
    runs.append({"label": args.label, "inventory": snapshot})
    state.write_text(json.dumps(runs, indent=2, sort_keys=True), encoding="utf-8")

    pairs = [(r["label"], r["inventory"]) for r in runs]
    out.write_text(inv_mod.render_markdown(pairs), encoding="utf-8")
    print(f"[inventory] {args.label}: wrote {out}")

    if args.json:
        print(inv_mod.to_json(snapshot))

    if len(pairs) >= 2:
        problems = inv_mod.violations(pairs[0][1], pairs[-1][1])
        if problems:
            print("[inventory] CONTRACT FAILED:")
            for p in problems:
                print("  - " + p)
            return 1
        print("[inventory] contract holds: no row was lost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
