#!/usr/bin/env python3
"""Find imports that name a backend module which no longer exists. Harness PRD H2.

The incident: a function-local `from routers.x import y` survived the deletion of
`routers/x.py`. Module-level imports fail loudly at boot; a function-local import
fails only when that function is called, which in a flag-gated path can be never
in dev and immediately in production.

Resolution is PURELY on the filesystem — nothing is imported, so the scan has no
side effects and stays fast enough for a per-edit hook. Only first-party names
(whose first segment matches a top-level module or package in backend/) are
checked; stdlib and site-packages are left alone.

  (no args)      scan the whole backend tree
  --file PATH    scan one file — hook mode, milliseconds

Exit 2 on a dangling import (blocks the agent's edit, stderr explains why).
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "venv", ".venv"}


def _first_party_roots() -> set[str]:
    """Top-level module/package names that live in backend/."""
    roots = set()
    for child in BACKEND.iterdir():
        if child.is_dir() and (child / "__init__.py").exists() and child.name not in SKIP_DIRS:
            roots.add(child.name)
        elif child.is_dir() and child.name not in SKIP_DIRS and any(child.glob("*.py")):
            roots.add(child.name)  # namespace-style package (no __init__.py)
        elif child.suffix == ".py":
            roots.add(child.stem)
    return roots


def _resolves(dotted: str) -> bool:
    """True if ``a.b.c`` maps to a real file or package under backend/."""
    # Both `import a.b` and `from a.b import name` require a.b itself to be a
    # module or package. Do NOT fall back to "maybe the last segment is a symbol":
    # that fallback makes `from routers.deleted_router import thing` resolve as
    # long as routers/ exists, which is precisely the incident this script is for.
    base = BACKEND.joinpath(*dotted.split("."))
    return (base.with_suffix(".py").exists()
            or (base / "__init__.py").exists()
            or base.is_dir())


def scan(path: pathlib.Path, roots: set[str]) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"{path}: cannot parse ({exc})"]

    problems = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — resolved against the package, skip
                continue
            if node.module:
                names = [node.module]
        for dotted in names:
            if dotted.split(".")[0] not in roots:
                continue  # stdlib / third-party
            if not _resolves(dotted):
                try:
                    loc = path.relative_to(BACKEND)
                except ValueError:
                    loc = path
                problems.append(
                    f"{loc}:{node.lineno}: imports {dotted!r}, which no longer exists")
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="scan a single file (hook mode)")
    args = ap.parse_args(argv[1:])
    roots = _first_party_roots()

    if args.file:
        path = pathlib.Path(args.file).resolve()
        if path.suffix != ".py" or not path.exists():
            return 0  # nothing to say about a non-Python or deleted file
        targets = [path]
    else:
        targets = [p for p in BACKEND.rglob("*.py")
                   if not (SKIP_DIRS & set(p.parts))]

    problems: list[str] = []
    for path in targets:
        problems.extend(scan(path, roots))

    if problems:
        print("DANGLING IMPORTS:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 2
    if not args.file:
        print(f"no dangling imports ({len(targets)} files scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
