#!/usr/bin/env python3
"""Brace-balance a CSS file. Harness PRD H2.

An unclosed `@media` does not break CSS loudly — it swallows every rule after it
until the file ends. `asclepius.css:4814` opened a media query that never closed
and hid 69 rules; the page rendered, nothing errored, and the styles were simply
gone. This is a two-second check that turns that into a failed edit.

Exit 2 (blocks the agent's edit and feeds stderr back as the reason) when depth
is non-zero at EOF, naming the line of the innermost block still open.

Usage:  python3 scripts/css_balance.py <file.css> [...]
"""

from __future__ import annotations

import re
import sys

# Strip comments and quoted strings first: a brace inside /* */ or content:"{"
# is not structure, and counting it produces a false alarm on a correct file.
_NOISE = re.compile(r"/\*.*?\*/|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", re.S)


def check(path: str) -> list[str]:
    """Return a list of problems (empty when the file is balanced)."""
    try:
        src = open(path, encoding="utf-8").read()
    except OSError as exc:
        return [f"{path}: cannot read ({exc})"]

    # Blank out noise but keep newlines, so line numbers stay exact.
    clean = _NOISE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), src)

    stack: list[tuple[int, str]] = []
    problems: list[str] = []
    line = 1
    for ch in clean:
        if ch == "\n":
            line += 1
        elif ch == "{":
            opener = clean.split("\n")[line - 1].strip()[:60] if line <= clean.count("\n") + 1 else ""
            stack.append((line, opener))
        elif ch == "}":
            if stack:
                stack.pop()
            else:
                problems.append(f"{path}:{line}: '}}' with no matching '{{'")

    for ln, text in stack:
        problems.append(f"{path}:{ln}: block opened here and never closed — {text!r}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    problems: list[str] = []
    for path in argv[1:]:
        if path.endswith(".css"):
            problems.extend(check(path))
    if problems:
        print("CSS brace balance FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
