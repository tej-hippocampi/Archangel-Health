---
name: data-inventory
description: Snapshot and diff row ids in tasks, submissions, records, earnings, uploads, assignments and exports around a risky change. Use for any PRD or migration that touches those tables, before and after the change.
---

# /data-inventory

"56 tasks, none may be lost." Rows in these tables are physician work that was
paid for. A migration that drops one is not a bug you fix forward — the row is
gone, and the contributor's evidence of their own work goes with it.

## Use it as a bracket

```bash
# BEFORE the change
python3 backend/scripts/data_inventory.py --snapshot
#   -> docs/asclepius/INVENTORY_<date>.json

# ... make the change, run the migration ...

# AFTER
python3 backend/scripts/data_inventory.py --diff docs/asclepius/INVENTORY_<date>.json
```

Exit 2 means an id that existed before is missing now. **Stop and restore.**
Added ids never fail the check — growth is normal, loss is not.

## When to run it

Any PRD or migration touching `tasks`, `submissions`, `records`, `earnings`,
`uploads`, `assignments` or `exports`. If you are unsure whether your change
touches them, run it — the snapshot costs milliseconds.

## The rule that makes the check almost never fire

**Never `DELETE` in a migration.** Add a column, backfill it, flip a flag. A row
that should no longer appear is filtered in the query, not removed from the
table. The `store.py` edit hook enforces this on `DELETE FROM` against these
tables; this skill is the check for everything the hook cannot see, such as a
migration run by hand or an ORM-level cascade.

## Reading a diff

Per-table `before -> after (+added, -missing)`. Investigate any `-` immediately;
that number should be zero for the life of the product.
