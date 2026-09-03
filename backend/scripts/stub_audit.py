#!/usr/bin/env python3
"""Which per-file `call_llm` stubs are still load-bearing? (Fake LLM PRD §2)

PRD §2 says to delete the per-file stubs the fake now satisfies and keep a
monkeypatch "only where a test needs a specific answer". This answers that
question with evidence instead of by reading: for each test that stubs
``call_llm`` / ``first_text``, it removes the stub, runs the test against
``ai/fake_llm.py``, and reports whether it still passes.

**A test that passes without its stub is NOT automatically redundant.** Read
every candidate before deleting one. The common trap here is a test whose stub
IS the mechanism under test, which becomes VACUOUS rather than fixed:

  * a spy that asserts the model is never called (``assert called == []``) —
    without the stub the list is never populated and the assertion is free;
  * a stub returning an invented URL that must be dropped — without it nothing
    is invented and the rule under test is never exercised;
  * a stub that raises, proving the caller swallows it — without it nothing
    fails and "never raises" is trivially true;
  * a stub returning deliberately WRONG keys, proving the parser degrades.

Run: python3 scripts/stub_audit.py [--file tests/test_x.py]
Exit 0 always — this is a report, not a gate.
"""

from __future__ import annotations

import argparse
import ast
import os
import pathlib
import re
import shutil
import subprocess
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
TESTS = BACKEND / "tests"
STUB_RE = re.compile(r"setattr\(.*(call_llm|first_text)")


class _StripStubs(ast.NodeTransformer):
    """Replace whole `monkeypatch.setattr(..., call_llm|first_text, ...)` statements."""

    def visit_Expr(self, node):  # noqa: N802 — ast API
        try:
            src = ast.unparse(node)
        except Exception:  # pragma: no cover — very old grammar
            return node
        return ast.Pass() if STUB_RE.search(src) else node


def stubbing_tests(path: pathlib.Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = ast.get_source_segment(src, node) or ""
            if STUB_RE.search(seg):
                out.append(node.name)
    return out


def audit(path: pathlib.Path) -> tuple[list[str], list[str]]:
    """(tests that FAIL without their stub, tests that still pass)."""
    names = stubbing_tests(path)
    if not names:
        return [], []
    original = path.read_text(encoding="utf-8")
    tree = _StripStubs().visit(ast.parse(original))
    ast.fix_missing_locations(tree)
    backup = path.with_suffix(path.suffix + ".stubaudit.bak")
    shutil.copy(str(path), str(backup))
    try:
        path.write_text(ast.unparse(tree))
        env = {**os.environ,
               "ASCLEPIUS_DB_PATH": "/tmp/stub_audit.db",
               "COMMUNITY_DB_PATH": "/tmp/stub_audit_c.db",
               "ASCLEPIUS_EXPORT_DIR": "/tmp/stub_audit_x"}
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "-q", "-p", "no:cacheprovider"],
            capture_output=True, text=True, env=env, timeout=900, cwd=str(BACKEND))
    finally:
        shutil.move(str(backup), str(path))
    failed = set(re.findall(r"^FAILED [^:]+::([\w\[\]-]+)", proc.stdout, re.M))
    return [n for n in names if n in failed], [n for n in names if n not in failed]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="audit one test file")
    args = ap.parse_args(argv[1:])

    files = ([pathlib.Path(args.file)] if args.file
             else sorted(p for p in TESTS.glob("test_*.py") if STUB_RE.search(p.read_text())))
    load_bearing = candidates = 0
    print(f"{'file':<44}{'load-bearing':>13}{'candidates':>12}")
    for path in files:
        fails, passes = audit(path)
        if not fails and not passes:
            continue
        load_bearing += len(fails)
        candidates += len(passes)
        print(f"  {path.name:<42}{len(fails):>13}{len(passes):>12}")
        for name in passes:
            print(f"      candidate (READ IT — may be vacuous without the stub): {name}")
    print(f"\n  {load_bearing} load-bearing, {candidates} candidate(s) to read by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
