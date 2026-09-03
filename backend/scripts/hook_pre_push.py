#!/usr/bin/env python3
"""PreToolUse hook: gate `git push` on merge readiness. Harness PRD H2.

Claude Code matches this hook on every Bash call, so the FIRST thing it does is
decide whether the command is actually a push. Anything else exits 0 immediately
— the cost of the hook on a normal Bash call is one Python startup.

Exit 2 blocks the push and feeds stderr back to the agent, which is the whole
point: a stale merge-base is cheap to fix before the push and expensive after.

Set HOOK_SKIP_MERGE_READINESS=1 to bypass (for a deliberate push of a branch you
know is behind).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parent

# `git push`, `git -C dir push`, `git push --force`; also after a shell separator
# (`cd x && git push`). NOT `git push --help`, and not a mention of the words in
# some other command's arguments (`echo git push`) — `git` must sit in COMMAND
# position, which is what the leading anchor enforces.
_PUSH_RE = re.compile(
    r"(?:^|[;&|]\s*|\n)\s*git\b(?:\s+-[^\s]+(?:\s+[^\s]+)?)*\s+push\b")


def is_push(command: str) -> bool:
    if not command or "--help" in command:
        return False
    return bool(_PUSH_RE.search(command))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0

    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not is_push(command):
        return 0
    if os.getenv("HOOK_SKIP_MERGE_READINESS") == "1":
        print("merge-readiness check skipped (HOOK_SKIP_MERGE_READINESS=1)", file=sys.stderr)
        return 0

    try:
        p = subprocess.run([sys.executable, str(SCRIPTS / "merge_readiness.py")],
                           capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"(merge-readiness hook skipped: {exc})", file=sys.stderr)
        return 0  # fail open — never block a push on the hook's own bug

    if p.returncode == 2:
        print(p.stdout, file=sys.stderr)
        print(p.stderr, file=sys.stderr)
        print("\nPush blocked. Rebase (or resolve the listed files), then push again. "
              "HOOK_SKIP_MERGE_READINESS=1 bypasses this deliberately.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
