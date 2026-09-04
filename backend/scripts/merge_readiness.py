#!/usr/bin/env python3
"""Is this branch safe to push? Harness PRD H2/H3.

The incident: a longitudinal branch was cut from a stale merge-base, predating the
assignments feature. Both features were real, both were correct on their own
branch, and a naive conflict resolution would have silently killed one.

Checks, in the order that matters:
  1. how far behind origin/main the merge-base is — more than --max-behind
     (default 20) commits is a STOP, rebase first;
  2. a dry-run merge against origin/main, listing conflicting files;
  3. for the three files that have collided in every branch so far
     (store.py, export.py, asclepius.js), print both sides' hunk headers, because
     "conflicts in store.py" is not enough information to resolve it safely.

Exit 2 on a stop condition, 0 when clear. Never modifies the working tree: the
merge check runs with `git merge-tree`, which writes nothing.

Usage:  python3 scripts/merge_readiness.py [--max-behind 20] [--base origin/main]
"""

from __future__ import annotations

import argparse
import subprocess
import sys

COLLISION_FILES = ("store.py", "export.py", "asclepius.js")


def git(*args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--max-behind", type=int, default=20)
    args = ap.parse_args(argv[1:])

    rc, _ = git("rev-parse", "--git-dir")
    if rc != 0:
        print("not a git repository — skipping merge readiness", file=sys.stderr)
        return 0

    # A missing remote ref is not a failure: a fresh clone or an offline sandbox
    # should not be blocked from pushing by a check it cannot run.
    if git("rev-parse", "--verify", args.base)[0] != 0:
        print(f"{args.base} not present locally (try `git fetch origin main`); "
              f"merge readiness skipped", file=sys.stderr)
        return 0

    rc, base = git("merge-base", "HEAD", args.base)
    if rc != 0:
        print(f"no merge base with {args.base}; skipped", file=sys.stderr)
        return 0

    behind = git("rev-list", "--count", f"{base}..{args.base}")[1] or "0"
    ahead = git("rev-list", "--count", f"{base}..HEAD")[1] or "0"
    print(f"merge-base {base[:9]} — {ahead} commit(s) ahead, {behind} behind {args.base}")

    problems: list[str] = []
    if int(behind) > args.max_behind:
        problems.append(
            f"{behind} commits behind {args.base} (limit {args.max_behind}). "
            f"Rebase before pushing: a branch this stale resolves conflicts against "
            f"a tree that no longer exists, which is how a feature gets silently "
            f"dropped in a 'clean' merge.")

    # merge-tree writes nothing; conflicts show as <<<<<<< in its output.
    rc, out = git("merge-tree", "--write-tree", "--name-only", args.base, "HEAD")
    conflicts: list[str] = []
    if rc != 0 and out:
        conflicts = [l.strip() for l in out.splitlines()
                     if l.strip() and "/" in l and not l.startswith(("Auto", "warning", "CONFLICT"))]
    if conflicts:
        print("\nconflicting files:", file=sys.stderr)
        for f in conflicts:
            print(f"  {f}", file=sys.stderr)
        hot = [f for f in conflicts if any(f.endswith(c) for c in COLLISION_FILES)]
        for f in hot:
            print(f"\n--- {f}: both sides' hunks (this file collides in every branch) ---",
                  file=sys.stderr)
            for ref, label in ((args.base, "base"), ("HEAD", "ours")):
                d = git("diff", f"{base}..{ref}", "--", f)[1]
                heads = [l for l in d.splitlines() if l.startswith("@@")][:8]
                print(f"  {label}: {len(heads)} hunk(s)", file=sys.stderr)
                for h in heads:
                    print(f"    {h}", file=sys.stderr)
        problems.append(f"{len(conflicts)} conflicting file(s) against {args.base}")

    if problems:
        print("\nMERGE READINESS: STOP", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    print("merge readiness: clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
