#!/usr/bin/env python3
"""Sandbox PRD §2 — seed (or reset) the sandbox realm from a shell.

    cd backend
    ASCLEPIUS_SANDBOX_ADMIN_PASSWORD=… ASCLEPIUS_SANDBOX_DOCTOR_PASSWORD=… \\
        python3 scripts/sandbox_seed.py [--fresh] [--reset]

Same code path as ``POST /api/asclepius/sandbox/seed``: the realm ContextVar
is set to ``sandbox`` for the run, so every store call lands in the sandbox
files derived from this environment's live paths. Never prints a password.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import realm  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fresh", action="store_true", help="also seed one un-onboarded physician")
    ap.add_argument("--reset", action="store_true",
                    help="drop the sandbox DBs and asset dir first (sandbox files only)")
    args = ap.parse_args(argv)

    admin_pw = realm.admin_password()
    doctor_pw = realm.doctor_password()
    if not admin_pw or not doctor_pw:
        print(f"set {realm.ADMIN_PASSWORD_VAR} and {realm.DOCTOR_PASSWORD_VAR} first", file=sys.stderr)
        return 2

    from asclepius import sandbox_seed  # noqa: PLC0415

    with realm.scoped(realm.SANDBOX):
        paths = realm.paths()
        print(f"[sandbox] realm={realm.current()} db={paths['asclepius']}")
        if args.reset:
            out = asyncio.run(sandbox_seed.reset(admin_password=admin_pw, doctor_password=doctor_pw,
                                                 fresh=args.fresh))
            for p in out["reset"]["removed"]:
                print(f"[sandbox] removed {p}")
        else:
            out = asyncio.run(sandbox_seed.seed(admin_password=admin_pw, doctor_password=doctor_pw,
                                                fresh=args.fresh))
    print(f"[sandbox] admin: {out['admin_email']}")
    for email in out["physicians"]:
        print(f"[sandbox] physician: {email}")
    if out.get("fresh"):
        print(f"[sandbox] fresh (un-onboarded): {out['fresh']}")
    print(f"[sandbox] community welcomes posted: {out['community_welcomed']}")
    print("[sandbox] credentials: see the sandbox admin's Accounts tab (/sandbox/admin)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
