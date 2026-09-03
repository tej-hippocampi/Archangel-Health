#!/usr/bin/env python3
"""H1 test — AGENTS.md must not describe a product we no longer build.

The repo shipped an AGENTS.md that named the app "CareGuide", said it had "No
database" (it has four SQLite stores), gave Cursor Cloud instructions, and
pointed at a peri-op patient dashboard as the demo. Every Claude Code session
began by reading it, so every session began from a confident description of the
wrong product. That is the single cheapest bug in the repo to prevent and the
most expensive to leave: it is wrong in the agent's FIRST read, before any code.

Exit 1 on any banned marker, on an over-long file, or on a retired skill that has
crept back into `.claude/skills/`.

Usage:  python3 scripts/check_agents_md.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AGENTS = ROOT / "AGENTS.md"
CLAUDE = ROOT / "CLAUDE.md"
SKILLS = ROOT / ".claude" / "skills"

MAX_LINES = 120

# Each marker is a specific false claim the old file made, not a style rule.
BANNED = {
    "CareGuide": "the product is Archangel Health / Asclepius, not CareGuide",
    "No database": "there are four SQLite stores",
    "Cursor": "Cursor Cloud instructions describe a workflow we do not use",
    "patient/maria": "the peri-op patient dashboard is not the demo",
}

# Allowed only inside the landmines section, where naming dead code is the point.
LANDMINE_ONLY = ("triage",)

RETIRED = ("surgical-risk-triage", "team-eligibility-review", "ehr-extraction")


def _landmines_span(text: str) -> tuple[int, int]:
    """(start, end) character offsets of the landmines section, or (-1, -1)."""
    m = re.search(r"^##\s+The landmines\s*$", text, re.M | re.I)
    if not m:
        return (-1, -1)
    nxt = re.search(r"^##\s+", text[m.end():], re.M)
    return (m.start(), m.end() + (nxt.start() if nxt else len(text) - m.end()))


def main() -> int:
    problems: list[str] = []

    if not AGENTS.exists():
        print("AGENTS.md is missing", file=sys.stderr)
        return 1
    text = AGENTS.read_text()
    lines = text.splitlines()

    if len(lines) > MAX_LINES:
        problems.append(f"AGENTS.md is {len(lines)} lines; the cap is {MAX_LINES} "
                        f"(it is a map, not a manual)")

    low = text.lower()
    for marker, why in BANNED.items():
        if marker.lower() in low:
            n = next((i + 1 for i, l in enumerate(lines) if marker.lower() in l.lower()), 0)
            problems.append(f"AGENTS.md:{n} contains {marker!r} — {why}")

    lm_start, lm_end = _landmines_span(text)
    for marker in LANDMINE_ONLY:
        for m in re.finditer(re.escape(marker), text, re.I):
            if not (lm_start <= m.start() < lm_end):
                n = text[:m.start()].count("\n") + 1
                problems.append(
                    f"AGENTS.md:{n} mentions {marker!r} outside the landmines section — "
                    f"peri-op is flag-gated for deletion; naming it elsewhere sends "
                    f"agents to edit dead code")

    if not CLAUDE.exists():
        problems.append("CLAUDE.md is missing — Claude Code reads it, so it must "
                        "include AGENTS.md or the two files drift apart")
    elif "AGENTS.md" not in CLAUDE.read_text():
        problems.append("CLAUDE.md does not reference AGENTS.md")

    for name in RETIRED:
        if (SKILLS / name).exists():
            problems.append(
                f".claude/skills/{name}/ is active — it describes peri-op code that is "
                f"flag-gated for deletion; move it to .claude/skills/_retired/")

    if problems:
        print("AGENTS.md check FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"AGENTS.md check passed ({len(lines)} lines, no stale markers, "
          f"{len(RETIRED)} retired skills out of the way)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
