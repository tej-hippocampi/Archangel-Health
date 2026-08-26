"""Empty a community DB of machine-generated content, from the shell.

Local twin of ``POST /internal/community/purge``: hard-deletes every post by
the ``u-system`` bot (news digests, welcome posts) and every post whose author
has no account in the users plane (demo-seeded doctors), together with the
replies under them. Channels, DMs and human top-level posts survive.

Usage, from ``backend/``:

    python3 scripts/purge_community.py            # purge the configured DBs
    python3 scripts/purge_community.py --dry-run  # report what would go

Respects ``ASCLEPIUS_DB_PATH`` / ``COMMUNITY_DB_PATH`` (and ``backend/.env``
via python-dotenv when present), so pointing it at a production volume means
setting those, deliberately.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report the authors that would be purged, delete nothing")
    args = parser.parse_args()

    from asclepius.store import get_store
    from community.store import get_community_store

    cstore = get_community_store()
    valid_ids = [u["id"] for u in get_store().list_users()]

    if args.dry_run:
        import sqlite3

        with sqlite3.connect(cstore.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT author_user_id, COUNT(*) AS n FROM community_messages "
                "GROUP BY author_user_id"
            ).fetchall()
        doomed = [
            (r["author_user_id"], r["n"]) for r in rows
            if r["author_user_id"] == "u-system" or r["author_user_id"] not in valid_ids
        ]
        if not doomed:
            print("Nothing to purge.")
        for author, n in doomed:
            print(f"would purge {n:4d} message(s) by {author}")
        return 0

    counts = cstore.purge_generated_content(valid_user_ids=valid_ids)
    print("Purged:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
