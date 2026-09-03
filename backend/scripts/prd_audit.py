#!/usr/bin/env python3
"""Audit a PRD's `file:line` citations against the tree. Harness PRD H3.

Every PRD in this repo has drifted at least once — `ingestion.py:1533→1534`,
`asclepius.js:7462→7997`, `store.py:291→503`. A drifted citation is worse than no
citation: it sends an agent to edit a confidently-named wrong line.

For each `path:line` found in the PRD, this reads that line and checks it against
the symbols backticked near the citation. Output is a table:

    citation                     verdict  actual line
    ai/llm_client.py:419         OK       async def call_llm(

Also asserts the PRD carries an invariant/design section, a tests section, and a
do-not-touch section (H3's structural requirement).

**When a citation is wrong, fix the PRD — never the code.** The code is the fact.

Exit 1 if any citation is stale or a required section is missing.

Usage:  python3 scripts/prd_audit.py PRD.md [PRD2.md ...] [--root DIR]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

# `file.py:123`, but NOT `file.py:123→456`: the arrow form is a PRD narrating a
# citation that ALREADY drifted (an incident report), not a live claim about the
# current tree. Auditing those re-reports history as a fresh failure every run.
CITATION = re.compile(
    r"`?([A-Za-z0-9_./-]+\.(?:py|js|css|ts|tsx|json|ya?ml|md))`?:(\d+)(?!\d)(?!\s*[→>-]+\s*\d)")
BACKTICKED = re.compile(r"`([A-Za-z0-9_./]+)`")

REQUIRED_SECTIONS = {
    "invariant/design": re.compile(r"^#+.*\b(design|invariant|architecture|deliverable)\b",
                                   re.I | re.M),
    "tests": re.compile(r"^#+.*\btests?\b", re.I | re.M),
    "do-not-touch": re.compile(r"^#+.*\b(do not touch|do-not-touch|out of scope|non-goals)\b",
                               re.I | re.M),
}


def _resolve(rel: str, root: pathlib.Path) -> pathlib.Path | None:
    for base in (root, root / "backend", root / "frontend"):
        p = base / rel
        if p.exists() and p.is_file():
            return p
    matches = [p for p in root.rglob(pathlib.Path(rel).name)
               if "node_modules" not in p.parts and ".git" not in p.parts]
    if len(matches) == 1:
        return matches[0]
    # A bare basename with several matches (four store.py files live here) is
    # ambiguous by construction — say so rather than guessing a file and then
    # confidently auditing the wrong one.
    return None


def audit(prd: pathlib.Path, root: pathlib.Path) -> tuple[list[tuple], list[str]]:
    text = prd.read_text(encoding="utf-8")
    rows: list[tuple] = []

    for raw_line in text.splitlines():
        # Scope symbols to the markdown TABLE CELL holding the citation. A hooks
        # table row backticks `PostToolUse`, a matcher and a command alongside a
        # file:line that has nothing to do with any of them; taking the whole row
        # as context reports a correct citation as drifted.
        for cell in (raw_line.split("|") if "|" in raw_line else [raw_line]):
            near = set(BACKTICKED.findall(cell))
            for rel, lineno in CITATION.findall(cell):
                target = _resolve(rel, root)
                cite = f"{rel}:{lineno}"
                if target is None:
                    rows.append((cite, "NO FILE", "—"))
                    continue
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                n = int(lineno)
                if not (1 <= n <= len(lines)):
                    rows.append((cite, "OUT OF RANGE", f"file has {len(lines)} lines"))
                    continue
                actual = lines[n - 1].strip()
                symbols = {t for t in near if t != rel and "/" not in t and len(t) > 2}
                if not symbols:
                    rows.append((cite, "NO SYMBOL", actual[:70]))
                elif any(t in actual for t in symbols):
                    rows.append((cite, "OK", actual[:70]))
                else:
                    rows.append((cite, "DRIFTED",
                                 f"{actual[:50]}  (want one of {sorted(symbols)})"))

    missing = [name for name, rx in REQUIRED_SECTIONS.items() if not rx.search(text)]
    return rows, missing


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prds", nargs="+")
    ap.add_argument("--root", default=str(REPO))
    args = ap.parse_args(argv[1:])
    root = pathlib.Path(args.root).resolve()

    bad = 0
    for name in args.prds:
        prd = pathlib.Path(name).resolve()
        if not prd.exists():
            print(f"{name}: not found", file=sys.stderr)
            bad += 1
            continue
        rows, missing = audit(prd, root)
        print(f"\n=== {prd.name} — {len(rows)} citation(s) ===")
        if rows:
            w = max(len(r[0]) for r in rows)
            for cite, verdict, actual in rows:
                mark = "OK  " if verdict in ("OK", "NO SYMBOL") else "FAIL"
                print(f"  [{mark}] {cite:<{w}}  {verdict:<12} {actual}")
        drifted = [r for r in rows if r[1] not in ("OK", "NO SYMBOL")]
        if drifted:
            bad += len(drifted)
            print(f"  -> {len(drifted)} stale citation(s). Fix the PRD, not the code.")
        if missing:
            bad += len(missing)
            print(f"  -> missing required section(s): {', '.join(missing)}")
        if not drifted and not missing:
            print("  -> all citations resolve; required sections present.")

    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
