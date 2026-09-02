"""The no-data-loss contract for the export/approval rework (PRD §0).

Step zero is an INVENTORY, not a change. This module is the one definition of
what that inventory is, so the "before" run, the "after" run, the migration
sweep's self-check and the test that guards all three are reading the same
numbers rather than three hand-written SQL strings that drift.

Two things are captured for every table that matters:

* a **count**, grouped the way the PRD asks (status, type, kind, version), and
* the **id set**, as a count plus a SHA-256 digest of the sorted ids.

The digest is what makes "exactly equal for the id sets" checkable without
pasting fifty thousand ids into a markdown file: two runs whose digests match
saw the same rows, and two runs whose digests differ did not — no matter which
direction the difference went.

The contract itself (``violations``): counts may only ever go UP, and the id
sets must be IDENTICAL. A status may move — that is the whole point of the
migration — but a row may never disappear.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

#: (table, id column) for every table the contract protects. Order is the order
#: the report renders in.
ID_SETS: Tuple[Tuple[str, str], ...] = (
    ("submissions", "submission_id"),
    ("records", "record_id"),
    ("earnings", "earning_id"),
    ("tasks", "task_id"),
    ("exports", "export_id"),
    ("buyers", "buyer_id"),
    ("buyer_requests", "request_id"),
)

#: The grouped counts from PRD §0, verbatim, as (label, SQL).
GROUPED_COUNTS: Tuple[Tuple[str, str], ...] = (
    ("submissions_by_status",
     "SELECT status AS k1, NULL AS k2, COUNT(*) AS n FROM submissions GROUP BY status"),
    ("records_by_status_type",
     "SELECT status AS k1, type AS k2, COUNT(*) AS n FROM records GROUP BY status, type"),
    ("earnings_by_status_kind",
     "SELECT status AS k1, kind AS k2, COUNT(*) AS n FROM earnings GROUP BY status, kind"),
    ("submissions_by_version_case_source",
     "SELECT s.portal_version AS k1, t.case_source AS k2, COUNT(*) AS n "
     "FROM tasks t JOIN submissions s ON s.task_id = t.task_id "
     "GROUP BY s.portal_version, t.case_source"),
)


def _digest(ids: List[str]) -> str:
    h = hashlib.sha256()
    for i in sorted(ids):
        h.update(str(i).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def inventory(store: Any) -> Dict[str, Any]:
    """The full inventory for one database, as a plain dict.

    Never raises on a missing table: a deployment that predates a table should
    produce a report saying so, not a traceback in the middle of a migration.
    """
    out: Dict[str, Any] = {
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db_path": getattr(store, "db_path", None),
        "counts": {},
        "id_sets": {},
    }
    with store._conn() as conn:                                # noqa: SLF001
        for label, sql in GROUPED_COUNTS:
            table = label.split("_by_")[0]
            if not _table_exists(conn, table):
                out["counts"][label] = None
                continue
            rows = conn.execute(sql).fetchall()
            out["counts"][label] = [
                {"key": r["k1"], "key2": r["k2"], "n": int(r["n"] or 0)} for r in rows
            ]
        for table, id_col in ID_SETS:
            if not _table_exists(conn, table):
                out["id_sets"][table] = None
                continue
            ids = [str(r[0]) for r in conn.execute(
                f"SELECT {id_col} FROM {table}").fetchall()]
            out["id_sets"][table] = {"count": len(ids), "digest": _digest(ids)}
    return out


def violations(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Every way ``after`` breaks the §0 contract against ``before``.

    An empty list is the assertion the migration test makes. Each entry is a
    sentence an operator can act on, not a diff dump.
    """
    problems: List[str] = []
    for table, _col in ID_SETS:
        b = (before.get("id_sets") or {}).get(table)
        a = (after.get("id_sets") or {}).get(table)
        if b is None:
            continue                      # table did not exist before; nothing to lose
        if a is None:
            problems.append(f"{table}: the table existed before the migration and does not now.")
            continue
        if a["count"] < b["count"]:
            problems.append(
                f"{table}: {b['count']} rows before, {a['count']} after — "
                f"{b['count'] - a['count']} row(s) were destroyed.")
        if a["digest"] != b["digest"]:
            problems.append(
                f"{table}: the id set changed (before {b['digest'][:12]}…, "
                f"after {a['digest'][:12]}…). Ids must be identical; a status may "
                "move, a row may not.")
    for label, _sql in GROUPED_COUNTS:
        b_rows = (before.get("counts") or {}).get(label)
        a_rows = (after.get("counts") or {}).get(label)
        if b_rows is None or a_rows is None:
            continue
        b_total = sum(r["n"] for r in b_rows)
        a_total = sum(r["n"] for r in a_rows)
        if a_total < b_total:
            problems.append(
                f"{label}: {b_total} rows before, {a_total} after — the total may "
                "only ever go up.")
    return problems


def _count_table(rows: Optional[List[Dict[str, Any]]], header: Tuple[str, str]) -> str:
    if rows is None:
        return "_table not present in this database_\n"
    if not rows:
        return "_no rows_\n"
    two = any(r.get("key2") is not None for r in rows)
    lines = [f"| {header[0]} | {header[1] if two else 'count'} |"
             + (" count |" if two else ""),
             "|---|---|" + ("---|" if two else "")]
    for r in sorted(rows, key=lambda r: (str(r["key"]), str(r["key2"]))):
        k1 = r["key"] if r["key"] is not None else "(null)"
        if two:
            k2 = r["key2"] if r["key2"] is not None else "(null)"
            lines.append(f"| `{k1}` | `{k2}` | {r['n']} |")
        else:
            lines.append(f"| `{k1}` | {r['n']} |")
    total = sum(r["n"] for r in rows)
    lines.append(f"\n**Total: {total}**\n")
    return "\n".join(lines) + "\n"


def render_markdown(runs: List[Tuple[str, Dict[str, Any]]]) -> str:
    """The committed report. ``runs`` is [(label, inventory), …] — typically
    [("before", …)] on the first pass and [("before", …), ("after", …)] after."""
    parts = [
        "# Export migration inventory (PRD §0)",
        "",
        "The no-data-loss contract for the one-approval / one-export-tab rework.",
        "Generated by `python3 backend/scripts/export_migration_inventory.py`.",
        "",
        "Counts may only ever go **up**; the id sets must be **identical**. A row's",
        "`status` may move — that is what the migration does — but no row is ever",
        "deleted, dropped, or renamed. The id digest is SHA-256 over the sorted ids,",
        "so two runs that saw the same rows produce the same digest.",
        "",
        "## Where the deploy-time check actually happens",
        "",
        "**Not here.** The boot sweep takes its own before/after snapshot around",
        "itself (`export_backfill.run_once_at_boot`), because a by-hand",
        "before-run cannot work: this script ships WITH the migration, so at the",
        "moment a before-snapshot is needed it is not deployed yet. Read the",
        "result at `GET /api/asclepius/admin/export/migration-report`.",
        "",
        "This script is for auditing a database at any other time — before a",
        "risky change, or on a copy. It is a pure read.",
        "",
        "```sh",
        "# On the Railway container the cwd is /app/backend and",
        "# ASCLEPIUS_DB_PATH is already set. Write the report to the VOLUME:",
        "python3 scripts/export_migration_inventory.py --label check \\",
        "  --out /data/inventory.md",
        "```",
        "",
        "Two runs with different `--label`s append to the same file and the",
        "second exits non-zero if any count went down or any id set changed.",
        "",
    ]
    if all((row or {}).get("count", 0) == 0
           for _t, _c in ID_SETS
           for row in [(runs[0][1].get("id_sets") or {}).get(_t)]):
        parts += [
            "> **This snapshot was taken against an EMPTY database.** It is the",
            "> committed template and the proof the tooling runs. Re-run the two",
            "> commands above against the production volume to get the real",
            "> before/after numbers.",
            "",
        ]
    for label, inv in runs:
        parts += [
            f"## {label}",
            "",
            f"- Taken at: `{inv.get('taken_at')}`",
            f"- Database: `{inv.get('db_path')}`",
            "",
            "### submissions by status", "",
            _count_table(inv["counts"].get("submissions_by_status"), ("status", "")),
            "### records by status and type", "",
            _count_table(inv["counts"].get("records_by_status_type"), ("status", "type")),
            "### earnings by status and kind", "",
            _count_table(inv["counts"].get("earnings_by_status_kind"), ("status", "kind")),
            "### submissions by portal version and case source", "",
            _count_table(inv["counts"].get("submissions_by_version_case_source"),
                         ("portal_version", "case_source")),
            "### id sets", "",
            "| table | rows | sha256(sorted ids) |",
            "|---|---|---|",
        ]
        for table, _col in ID_SETS:
            row = inv["id_sets"].get(table)
            if row is None:
                parts.append(f"| `{table}` | — | _not present_ |")
            else:
                parts.append(f"| `{table}` | {row['count']} | `{row['digest']}` |")
        parts.append("")
    if len(runs) >= 2:
        problems = violations(runs[0][1], runs[-1][1])
        parts += ["## Contract check", ""]
        parts += (["**PASS** — every count is ≥ its before value and every id set is "
                   "identical."] if not problems
                  else ["**FAIL**", ""] + [f"- {p}" for p in problems])
        parts.append("")
    return "\n".join(parts)


def to_json(inv: Dict[str, Any]) -> str:
    return json.dumps(inv, indent=2, sort_keys=True)
