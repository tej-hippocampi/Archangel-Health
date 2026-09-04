#!/usr/bin/env python3
"""Test names are the spec. Harness PRD H5.4.

`test_queue` tells an agent nothing when it fails. `test_v4_queue_never_serves_a
_trajectory_point` tells it exactly which rule broke, which is the thing it needs
in order to fix it. The failure message IS the specification.

Scoped to NEW tests by default — the existing suite has hundreds of older names
and rewriting them is churn with no reader. `--all` lints everything (reporting
only), `--base REF` lints tests added since REF.

A good name: >= MIN_WORDS words after `test_`, and reads as a claim rather than a
label. Exit 1 on a new test that does not.

Usage:
  python3 scripts/lint_test_names.py --base origin/main
  python3 scripts/lint_test_names.py --files tests/test_x.py
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import subprocess
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
TESTS = BACKEND / "tests"
MIN_WORDS = 4

# Words that make a name a label rather than a claim when they are all it has.
_WEAK = {"test", "works", "ok", "basic", "simple", "case", "cases", "it", "thing"}


def _added_test_names(base: str) -> dict[pathlib.Path, set[str]]:
    """Test functions added (not merely touched) since `base`."""
    p = subprocess.run(["git", "diff", "-U0", base, "--", "backend/tests"],
                       capture_output=True, text=True, cwd=str(BACKEND.parent))
    out: dict[pathlib.Path, set[str]] = {}
    current: pathlib.Path | None = None
    for line in (p.stdout or "").splitlines():
        if line.startswith("+++ b/"):
            current = BACKEND.parent / line[6:]
        elif line.startswith("+") and current is not None:
            body = line[1:].lstrip()
            for kw in ("def ", "async def "):
                if body.startswith(kw):
                    name = body[len(kw):].split("(")[0].strip()
                    if name.startswith("test_"):
                        out.setdefault(current, set()).add(name)
    return out


def _test_names(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (SyntaxError, OSError):
        return set()
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_")}


def judge(name: str) -> str | None:
    """Return a complaint, or None if the name reads as a sentence."""
    words = [w for w in name[len("test_"):].split("_") if w]
    if len(words) < MIN_WORDS:
        return (f"{len(words)} word(s); needs >= {MIN_WORDS}. Say what rule holds, "
                f"e.g. test_v4_queue_never_serves_a_trajectory_point")
    if all(w.lower() in _WEAK for w in words):
        return "reads as a label, not a claim about behaviour"
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", help="lint tests added since this ref")
    ap.add_argument("--files", nargs="*", help="lint every test in these files")
    ap.add_argument("--all", action="store_true", help="report on the whole suite")
    args = ap.parse_args(argv[1:])

    targets: dict[pathlib.Path, set[str]] = {}
    if args.base:
        targets = _added_test_names(args.base)
    elif args.files:
        for f in args.files:
            p = pathlib.Path(f).resolve()
            if p.exists():
                targets[p] = _test_names(p)
    elif args.all:
        for p in sorted(TESTS.glob("test_*.py")):
            targets[p] = _test_names(p)
    else:
        ap.error("choose --base, --files or --all")

    problems = []
    checked = 0
    for path, names in targets.items():
        for name in sorted(names):
            checked += 1
            complaint = judge(name)
            if complaint:
                try:
                    loc = path.relative_to(BACKEND.parent)
                except ValueError:
                    loc = path
                problems.append(f"{loc}::{name} — {complaint}")

    if problems:
        print(f"TEST NAME LINT — {len(problems)} of {checked} name(s) are not "
              f"sentences:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        if args.all:
            print("\n(--all is advisory; new tests are gated with --base)", file=sys.stderr)
            return 0
        return 1
    print(f"test names OK ({checked} checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
