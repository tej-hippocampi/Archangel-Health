#!/usr/bin/env python3
"""Id+count snapshot of the tables that hold the product. Harness PRD H3.

"56 tasks, none may be lost." Rows in these tables are physician work that was
paid for; a migration that drops one is not a bug you can fix forward, because
the row is gone. So: snapshot before the change, diff after, and fail if any id
disappeared.

  --snapshot           write docs/asclepius/INVENTORY_<date>.json
  --diff BEFORE.json   compare the live tables to that snapshot

Exit 2 if any id present in BEFORE is missing now. Added ids are reported but
never fail — growth is normal, loss is not.

Usage:
  python3 scripts/data_inventory.py --snapshot
  ... make the change ...
  python3 scripts/data_inventory.py --diff docs/asclepius/INVENTORY_2026-09-03.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = BACKEND.parent / "docs" / "asclepius"

# The five from the Export PRD §0, plus the two that carry assignment and
# provenance and are just as unrecoverable.
TABLES = ("tasks", "submissions", "records", "earnings", "uploads",
          "assignments", "exports")

# Each table's id column, best-effort: the first of these that exists.
ID_CANDIDATES = ("id", "task_id", "submission_id", "record_id", "earning_id",
                 "upload_id", "export_id", "assignment_id")


def _db_path() -> str:
    return os.getenv("ASCLEPIUS_DB_PATH") or str(BACKEND / "asclepius.db")


def _table_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def snapshot() -> dict:
    path = _db_path()
    if not pathlib.Path(path).exists():
        print(f"no database at {path}", file=sys.stderr)
        return {"db": path, "tables": {}}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    present = _table_names(conn)
    out: dict[str, dict] = {}
    for table in TABLES:
        # `uploads` is really ingest_uploads in this schema; accept either.
        real = table if table in present else f"ingest_{table}"
        if real not in present:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({real})")}
        id_col = next((c for c in ID_CANDIDATES if c in cols), None)
        if id_col is None:
            count = conn.execute(f"SELECT COUNT(*) FROM {real}").fetchone()[0]
            out[table] = {"table": real, "count": count, "ids": None}
            continue
        ids = [str(r[0]) for r in conn.execute(f"SELECT {id_col} FROM {real}")]
        out[table] = {"table": real, "id_column": id_col,
                      "count": len(ids), "ids": sorted(ids)}
    conn.close()
    return {"db": path, "taken_at": dt.datetime.now().isoformat(timespec="seconds"),
            "tables": out}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--diff", metavar="BEFORE.json")
    args = ap.parse_args(argv[1:])
    if not (args.snapshot or args.diff):
        ap.error("choose --snapshot or --diff")

    now = snapshot()

    if args.snapshot:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / f"INVENTORY_{dt.date.today().isoformat()}.json"
        out.write_text(json.dumps(now, indent=2) + "\n")
        total = sum(t["count"] for t in now["tables"].values())
        print(f"wrote {out.relative_to(BACKEND.parent)} — "
              f"{len(now['tables'])} table(s), {total} row(s)")
        for name, t in sorted(now["tables"].items()):
            print(f"  {name:12} {t['count']:>6}")
        return 0

    before = json.loads(pathlib.Path(args.diff).read_text())
    lost: list[str] = []
    for name, prev in before.get("tables", {}).items():
        cur = now["tables"].get(name)
        if cur is None:
            lost.append(f"{name}: table is gone (had {prev['count']} rows)")
            continue
        if prev.get("ids") is None or cur.get("ids") is None:
            if cur["count"] < prev["count"]:
                lost.append(f"{name}: {prev['count']} -> {cur['count']} rows")
            continue
        missing = sorted(set(prev["ids"]) - set(cur["ids"]))
        added = len(set(cur["ids"]) - set(prev["ids"]))
        if missing:
            lost.append(f"{name}: {len(missing)} id(s) missing — "
                        f"{missing[:10]}{' ...' if len(missing) > 10 else ''}")
        print(f"  {name:12} {prev['count']:>6} -> {cur['count']:<6} "
              f"(+{added}, -{len(missing)})")

    if lost:
        print("\nDATA LOSS — ids present before the change are gone:", file=sys.stderr)
        for line in lost:
            print(f"  {line}", file=sys.stderr)
        print("\nThis is not fixable forward. Restore before continuing.", file=sys.stderr)
        return 2
    print("\nno ids lost")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
