#!/usr/bin/env python3
"""Snapshot and diff the app's HTTP route table. Harness PRD H2.

Routes in this app are flag-gated in several places, so a route can stop being
registered because an import moved, a flag flipped, or a router stopped being
included — and nothing fails. The app boots, the suite is green, and one endpoint
is simply gone. A PRD range that "swallowed the live login route" is the same
class of failure seen from the other side.

  --snapshot  write the ordered route table to docs/asclepius/ROUTES.json
  --diff      compare the live table to the snapshot; exit 1 on any difference

Exit 1 on drift is the point: a deliberate route change is accompanied by a
re-run of --snapshot in the same diff, so the snapshot is the record of intent.

Usage:  python3 scripts/route_baseline.py --snapshot | --diff
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT = BACKEND.parent / "docs" / "asclepius" / "ROUTES.json"

_IGNORED_METHODS = {"HEAD", "OPTIONS"}


def live_routes() -> list[str]:
    """``"METHOD path"`` for every registered route, sorted and de-duplicated."""
    sys.path.insert(0, str(BACKEND))
    # Keep the import side-effect-free: temp stores, no real transports, no key.
    os.environ.setdefault("ASCLEPIUS_LLM_PROVIDER", "fake")
    os.environ.setdefault("EMAIL_DEV_MODE", "1")
    os.environ.setdefault("RATE_LIMIT_ENABLED", "0")
    import main  # noqa: E402  — imported for its `app`

    out: set[str] = set()
    for route in main.app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = getattr(route, "methods", None) or {"WS"}
        for m in methods:
            if m not in _IGNORED_METHODS:
                out.add(f"{m} {path}")
    return sorted(out)


def load_snapshot() -> list[str]:
    if not SNAPSHOT.exists():
        return []
    try:
        return list(json.loads(SNAPSHOT.read_text()).get("routes") or [])
    except (ValueError, AttributeError):
        return []


def main_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", action="store_true", help="write the snapshot")
    ap.add_argument("--diff", action="store_true", help="compare against the snapshot")
    args = ap.parse_args(argv[1:])
    if not (args.snapshot or args.diff):
        ap.error("choose --snapshot or --diff")

    routes = live_routes()

    if args.snapshot:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps({"count": len(routes), "routes": routes}, indent=2) + "\n")
        print(f"wrote {len(routes)} routes to {SNAPSHOT.relative_to(BACKEND.parent)}")
        return 0

    before = load_snapshot()
    if not before:
        print(f"no snapshot at {SNAPSHOT.relative_to(BACKEND.parent)} — "
              f"run --snapshot once to create the baseline", file=sys.stderr)
        return 1

    added = [r for r in routes if r not in set(before)]
    removed = [r for r in before if r not in set(routes)]
    if not (added or removed):
        print(f"route table unchanged ({len(routes)} routes)")
        return 0

    print("ROUTE DRIFT — the live table differs from the snapshot:", file=sys.stderr)
    for r in removed:
        print(f"  - {r}", file=sys.stderr)
    for r in added:
        print(f"  + {r}", file=sys.stderr)
    print("\nIf this change is intended, re-run --snapshot and commit "
          "docs/asclepius/ROUTES.json in the same diff.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main_cli(sys.argv))
