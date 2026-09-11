---
name: data-inventory
description: Snapshot and compare every affected SQLite table's identities and field hashes, plus file-tree hashes, before and after any data-related change.
---

# /data-inventory

"56 tasks, none may be lost." Rows in these tables are physician work that was
paid for. A migration that drops one is not a bug you fix forward — the row is
gone, and the contributor's evidence of their own work goes with it.

## Use it as a bracket

```bash
# BEFORE the change, with writers paused and explicit store/file scope
python3 backend/scripts/data_inventory.py --db /path/to/store.db \
  --blob-root originals=/path/to/originals --snapshot --output BEFORE.json

# ... make the change, run the migration ...

# AFTER
python3 backend/scripts/data_inventory.py --db /path/to/store.db --diff BEFORE.json
```

Exit 2 means evidence is missing, incomplete or changed. Stop release, preserve
both states and investigate; use a verified backup for a scoped recovery. Never
restore over production automatically. Added rows and columns are allowed.
Missing IDs, columns or files, changed field/file hashes, missing databases and
legacy ID-only baselines fail. A reviewed `--allow-change table.column` permits
that column's intentional value changes; it cannot permit a missing row/column.

## When to run it

Every data-related PRD or change follows `docs/data-safety/POLICY.md`. Run
separately for each affected live/sandbox SQLite database and every associated
blob tree. The script inventories all application tables. File hashing cost
depends on the dataset size. PostgreSQL and object storage require equivalent
manifests; this script does not inspect them or recover encryption keys.

## The rule that makes the check almost never fire

**Never `DELETE` in a migration.** Add a column, backfill it, flip a flag. A row
that should no longer appear is filtered in the query, not removed from the
table. The `store.py` edit hook enforces this on `DELETE FROM` against these
tables; this skill is the check for everything the hook cannot see, such as a
migration run by hand or an ORM-level cascade.

## Reading a diff

The diff lists missing identities and changed fields/files. A successful diff
proves preservation only for the frozen baseline's scope. It is not a backup,
an absolute no-loss guarantee or proof that a synthetic fixture covers live data.
Record backup and isolated restore evidence as required by the policy.
