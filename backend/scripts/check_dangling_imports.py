"""Find imports of modules you are about to delete — function-local ones included.

    cd backend && python3 scripts/check_dangling_imports.py routers.eligibility triage

Exits non-zero and lists every site if anything still imports a named module or a
submodule of it.

This exists because of a specific escape. When the peri-op routers were deleted,
``eligibility/pipeline.py`` was still doing ``from routers.eligibility import
UPLOAD_DIR`` at two call sites — a REVERSE dependency, from a package that was
being kept into a router that was being removed. Both imports were inside
function bodies, so:

  * nothing failed at import time, and the app booted fine;
  * the route diff was clean, because no route changed;
  * a grep for "who imports eligibility" found nothing, because the question was
    backwards — the deleted module was the one being imported, not the importer;
  * only the full test suite caught it, and only because a test happened to
    exercise one of those two lines.

A function-local import is invisible to every cheap check. This walks the AST of
every ``.py`` under the working directory, so it sees them wherever they hide.

Run it before the deletion, not after: it works just as well against modules that
still exist, and a hit found early is a five-minute fix instead of a red suite.
"""

from __future__ import annotations

import ast
import pathlib
import sys

SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}


def imported_modules(node: ast.AST) -> list[str]:
    """Every module name an import node names, whatever its form."""
    if isinstance(node, ast.ImportFrom):
        # Relative imports (``from . import x``) carry no absolute module name
        # and cannot name a package being deleted from outside this one.
        return [node.module] if node.module and node.level == 0 else []
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return []


def find(targets: set[str], root: pathlib.Path) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            for mod in imported_modules(node):
                if any(mod == t or mod.startswith(t + ".") for t in targets):
                    hits.append((str(path), node.lineno, mod))
    return hits


def main(argv: list[str]) -> int:
    targets = {a.strip() for a in argv[1:] if a.strip()}
    if not targets:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2

    hits = find(targets, pathlib.Path("."))
    if not hits:
        print(f"clean — nothing imports {', '.join(sorted(targets))}")
        return 0

    print(f"{len(hits)} dangling import(s):", file=sys.stderr)
    for path, lineno, mod in hits:
        print(f"  {path}:{lineno}: {mod}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
