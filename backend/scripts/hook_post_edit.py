#!/usr/bin/env python3
"""PostToolUse hook: check the file the agent just edited. Harness PRD H2.

Claude Code hands hooks a JSON payload on stdin. This reads the edited path and
dispatches by file type, so one hook entry covers the whole H2 table instead of
six matchers that each pay Python startup:

  *.js                      node --check                 (syntax slips in a 13k-line file)
  *.css                     css_balance.py               (the unclosed @media at asclepius.css:4814)
  backend/**/*.py           pyflakes + dangling imports  (function-local import of a deleted router)
  backend/asclepius/store.py  DELETE FROM guard          (the no-data-loss contract)

Exit 2 blocks the action and feeds stderr back to the agent as the reason.
Everything else exits 0: a hook that fails open on its own bugs is a hook that
stays installed.

Budget: well under the 5s ceiling — the Python checks are single-file mode.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = BACKEND / "scripts"

# Tables whose rows are the product. A migration adds a column and backfills; it
# never deletes a row. "56 tasks, none may be lost."
PROTECTED = ("tasks", "submissions", "records", "earnings", "uploads",
             "assignments", "exports")
_DELETE_RE = re.compile(
    r"DELETE\s+FROM\s+[\"'`\[]?(" + "|".join(PROTECTED) + r")\b", re.I)


def _edited_path(payload: dict) -> str:
    ti = payload.get("tool_input") or {}
    for key in ("file_path", "notebook_path", "path"):
        if ti.get(key):
            return str(ti[key])
    return ""


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 0, f"(hook skipped: {exc})"  # fail open


def check(path_s: str) -> list[str]:
    path = pathlib.Path(path_s)
    if not path.exists() or path.is_dir():
        return []
    problems: list[str] = []
    suffix = path.suffix.lower()

    if suffix == ".js" and shutil.which("node"):
        rc, out = _run(["node", "--check", str(path)])
        if rc != 0:
            problems.append(f"node --check failed:\n{out}")

    elif suffix == ".css":
        rc, out = _run([sys.executable, str(SCRIPTS / "css_balance.py"), str(path)])
        if rc != 0:
            problems.append(out)

    elif suffix == ".py":
        try:
            inside_backend = path.resolve().is_relative_to(BACKEND)
        except (AttributeError, ValueError):  # pragma: no cover — py<3.9
            inside_backend = str(BACKEND) in str(path.resolve())
        if inside_backend:
            # pyflakes is advisory: absent in some environments, and a warning
            # must not block an edit the agent is midway through.
            rc, out = _run([sys.executable, "-m", "pyflakes", str(path)])
            if rc not in (0,) and out and "No module named" not in out:
                problems.append(f"pyflakes:\n{out}")
            rc, out = _run([sys.executable, str(SCRIPTS / "check_dangling_imports.py"),
                            "--file", str(path)])
            if rc == 2:
                problems.append(out)

            if path.resolve() == (BACKEND / "asclepius" / "store.py").resolve():
                hits = [f"  {i}: {l.strip()}"
                        for i, l in enumerate(path.read_text().splitlines(), 1)
                        if _DELETE_RE.search(l)]
                if hits:
                    problems.append(
                        "DELETE FROM a protected table in store.py — the no-data-loss "
                        "contract. Add a column and backfill; never delete rows.\n"
                        + "\n".join(hits))
    return problems


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0  # not invoked as a hook; nothing to check

    path = _edited_path(payload)
    if not path:
        return 0
    try:
        problems = check(path)
    except Exception as exc:  # noqa: BLE001 — a hook must never break the session
        print(f"(post-edit hook error, ignored: {exc!r})", file=sys.stderr)
        return 0

    if problems:
        print(f"Edit to {os.path.basename(path)} failed its checks:\n", file=sys.stderr)
        for p in problems:
            print(p, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
