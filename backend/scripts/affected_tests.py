#!/usr/bin/env python3
"""Which test files cover the files I changed? Harness PRD H5.

The full suite is ~13 minutes. That is fine for CI and far too slow for a loop, so
agents skip it and learn they were wrong much later. This prints the subset worth
running now; the full suite stays for /merge-readiness and CI.

Selection is the union of two cheap signals:
  (a) IMPORT GRAPH — a test that transitively imports a changed module (AST, no
      imports executed, so no side effects and no boot cost);
  (b) NAMING — `x.py` → `tests/test_x*.py`, which catches tests that exercise a
      module through the app rather than importing it directly.

A changed test file always selects itself. If nothing matches, prints nothing and
exits 0 — the caller runs the full suite rather than a silent zero-test pass.

Usage:
  python3 scripts/affected_tests.py                  # vs HEAD (unstaged + staged)
  python3 scripts/affected_tests.py --base origin/main
  python3 scripts/affected_tests.py --files a.py b.py
  pytest -x --lf -q $(python3 scripts/affected_tests.py)
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import subprocess
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
TESTS = BACKEND / "tests"


def _changed(base: str | None) -> list[pathlib.Path]:
    args = ["git", "diff", "--name-only"] + ([base] if base else ["HEAD"])
    p = subprocess.run(args, capture_output=True, text=True, cwd=str(BACKEND.parent))
    names = [l.strip() for l in (p.stdout or "").splitlines() if l.strip()]
    if not base:  # include untracked files too
        p2 = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                            capture_output=True, text=True, cwd=str(BACKEND.parent))
        names += [l.strip() for l in (p2.stdout or "").splitlines() if l.strip()]
    out = []
    for n in names:
        path = (BACKEND.parent / n).resolve()
        if path.suffix == ".py" and path.exists():
            out.append(path)
    return out


def _module_name(path: pathlib.Path) -> str | None:
    """`backend/asclepius/critic.py` → `asclepius.critic`."""
    try:
        rel = path.resolve().relative_to(BACKEND)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or None


def _imports(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            # `from asclepius import critic` — the submodule is the real dependency.
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def select(changed: list[pathlib.Path]) -> list[str]:
    changed_mods = {m for m in (_module_name(p) for p in changed) if m}
    selected: set[pathlib.Path] = set()

    for path in changed:
        if path.is_relative_to(TESTS) and path.name.startswith("test_"):
            selected.add(path)

    # (b) naming convention
    for path in changed:
        stem = path.stem
        if stem and not stem.startswith("test_"):
            selected.update(TESTS.glob(f"test_{stem}*.py"))

    # (a) import graph — a test selects if any import prefix-matches a changed module
    if changed_mods:
        for test in TESTS.glob("test_*.py"):
            if test in selected:
                continue
            for imported in _imports(test):
                # The test must import the changed module itself, or something
                # inside it. NOT the reverse: a test that imports the `asclepius`
                # PACKAGE would otherwise select on every change to any module in
                # it, which pulled in 122 of 263 files and defeats the purpose.
                if any(imported == m or imported.startswith(m + ".")
                       for m in changed_mods):
                    selected.add(test)
                    break

    return sorted(str(p.relative_to(BACKEND)) for p in selected if p.exists())


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", help="diff against this ref instead of HEAD")
    ap.add_argument("--files", nargs="*", help="use these files instead of git")
    args = ap.parse_args(argv[1:])

    changed = ([pathlib.Path(f).resolve() for f in args.files]
               if args.files else _changed(args.base))
    try:
        for name in select([p for p in changed if p.exists()]):
            print(name)
    except BrokenPipeError:  # piped into `head` — not an error
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
