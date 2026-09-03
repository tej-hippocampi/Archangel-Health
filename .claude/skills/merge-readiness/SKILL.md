---
name: merge-readiness
description: Check a branch is safe to push or open a PR from - commits behind origin/main, merge conflicts, and the three files that collide in every branch. Use before git push, before opening a pull request, or when working in parallel worktrees.
---

# /merge-readiness

A longitudinal branch was cut from a merge-base that predated the assignments
feature. Both features were real and correct on their own branch, and a naive
conflict resolution would have silently killed one of them. This is the check
that catches that before the push, not after the merge.

## Run it

```bash
python3 backend/scripts/merge_readiness.py
```

Exit 0 = clear. Exit 2 = stop, with the reason on stderr.

## What it checks, in order

1. **How far behind.** `git merge-base HEAD origin/main`, then how many commits
   `origin/main` has that the base does not. **More than 20 → stop and rebase.**
   A branch this stale resolves conflicts against a tree that no longer exists.
2. **Conflicts.** A dry-run merge (`git merge-tree`, which writes nothing) lists
   every conflicting file.
3. **The three collision files.** For any conflict in `store.py`, `export.py` or
   `asclepius.js`, it prints both sides' hunk headers. These three have collided
   in every branch so far, and "conflicts in store.py" is not enough information
   to resolve one safely.
4. **The affected tests** on the rebased tree:
   ```bash
   python3 backend/scripts/affected_tests.py --base origin/main | xargs python3 -m pytest -x --lf -q
   ```

## When it stops you

Rebase, or resolve the named files, then run it again. Do not push through it:
`HOOK_SKIP_MERGE_READINESS=1` exists for a deliberate, explained exception, not
for getting past a red check.

## Parallel worktrees

This is what makes concurrent builders safe. Two branches editing `asclepius.js`
are caught here, before either pushes, rather than in whichever one merges second.
