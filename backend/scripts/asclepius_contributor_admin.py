"""Asclepius contributor ops tool — inspect a contributor's exportability, and
re-attribute one account's labelled work onto another (e.g. merge a stray
SSO-provisioned duplicate into the real onboarded account).

Runs against the LIVE Asclepius DB. Point ``ASCLEPIUS_DB_PATH`` at the deployed
database (default ``backend/asclepius.db``) before running.

Usage:
    cd backend

    # 1) Verify a contributor's records exist and would export:
    python3 scripts/asclepius_contributor_admin.py inspect kp9808@gmail.com
    #    ...and clear their QA-held records to export_ready in the same step:
    python3 scripts/asclepius_contributor_admin.py inspect kp9808@gmail.com --approve-qa

    # 2) Move a stray account's work onto the canonical account (dry-run first):
    python3 scripts/asclepius_contributor_admin.py reattribute \
        --from tejxpatel23@gmail.com --to tejpatel@berkeley.edu
    # ...then commit it (and deactivate the now-empty source):
    python3 scripts/asclepius_contributor_admin.py reattribute \
        --from tejxpatel23@gmail.com --to tejpatel@berkeley.edu --apply

``reattribute`` is a DRY RUN unless ``--apply`` is passed. Pass
``--keep-source-active`` to leave the source account enabled after moving.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius.store import get_store  # noqa: E402


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _approve_qa(store, user) -> int:
    """Promote this contributor's needs_qa submissions (and their records) to
    export_ready via the same QA-approve path the portal uses (logged for audit).
    Returns the number of submissions approved."""
    pending = store.list_submissions(status="needs_qa", evaluator_id=user["id"], limit=100000)
    for sub in pending:
        asc_pipeline.apply_qa_decision(
            store, sub, decision="approve",
            reviewer_id="ops:asclepius_contributor_admin",
            notes="contributor ops --approve-qa",
        )
    return len(pending)


def _resolve(store, email: str):
    user = store.get_user_by_email(email)
    if not user:
        print(f"ERROR: no Asclepius account found for {email!r}.")
        return None
    return user


def cmd_inspect(store, args) -> int:
    user = _resolve(store, args.email)
    if not user:
        return 1
    if args.approve_qa:
        n = _approve_qa(store, user)
        print(f"Approved {n} needs_qa submission(s) for {args.email} → export_ready.\n")
    diag = store.contributor_record_diagnostics(user)
    _print(diag)
    if diag["records_total"] == 0:
        print(f"\n→ {args.email} has labelled NO records in this database.")
    elif diag["exportable_records"] == 0:
        stuck = {
            k: v for k, v in diag["records_by_status"].items()
            if k not in ("export_ready", "exported")
        }
        print(
            f"\n→ {args.email} has {diag['records_total']} record(s) but 0 are export-ready.\n"
            f"  Records awaiting QA / not yet approved: {stuck or '{}'}.\n"
            f"  Approve them in the QA queue (or via /qa/approve-all) to make them exportable."
        )
        if diag["annotator_id_mismatch_records"]:
            print(
                f"  NOTE: {diag['annotator_id_mismatch_records']} shippable record(s) carry a "
                f"different hashed-annotator id than this account — they were labelled under "
                f"another identity. Use `reattribute` to fold them in."
            )
    else:
        print(
            f"\n→ {args.email} has {diag['exportable_records']} export-ready record(s). "
            f"The contributor \"Export Data\" button will package them."
        )
    return 0


def cmd_reattribute(store, args) -> int:
    source = _resolve(store, args.source)
    target = _resolve(store, args.target)
    if not source or not target:
        return 1
    if source["id"] == target["id"]:
        print("ERROR: --from and --to resolve to the same account.")
        return 1

    print(f"Source (work moves FROM): {source['email']}  id_hashed={source.get('id_hashed')}")
    print(f"Target (work moves TO):   {target['email']}  id_hashed={target.get('id_hashed')}")
    print("\nBefore — source:")
    _print(store.contributor_record_diagnostics(source))
    print("Before — target:")
    _print(store.contributor_record_diagnostics(target))

    if not args.apply:
        n = store.contributor_record_diagnostics(source)["records_total"]
        print(
            f"\nDRY RUN — would move {n} record(s) and their submissions from "
            f"{source['email']} to {target['email']}"
            + ("" if args.keep_source_active else f", then deactivate {source['email']}")
            + (", then approve the moved needs_qa records" if args.approve_qa else "")
            + ".\nRe-run with --apply to commit."
        )
        return 0

    summary = store.reattribute_contributor(
        source_user=source, target_user=target,
        deactivate_source=not args.keep_source_active,
    )
    print("\nAPPLIED:")
    _print(summary)
    if args.approve_qa:
        n = _approve_qa(store, store.get_user_by_email(target["email"]))
        print(f"Approved {n} moved needs_qa submission(s) → export_ready.")
    # Re-resolve target (id_hashed unchanged) and show the post-state.
    print("After — target:")
    _print(store.contributor_record_diagnostics(store.get_user_by_email(target["email"])))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Asclepius contributor ops tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ins = sub.add_parser("inspect", help="show a contributor's record/export status")
    p_ins.add_argument("email")
    p_ins.add_argument("--approve-qa", action="store_true",
                       help="first promote this contributor's needs_qa records to export_ready")

    p_re = sub.add_parser("reattribute", help="move one account's work onto another")
    p_re.add_argument("--from", dest="source", required=True, help="source account email")
    p_re.add_argument("--to", dest="target", required=True, help="target account email")
    p_re.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    p_re.add_argument("--keep-source-active", action="store_true",
                      help="do not deactivate the source account after moving")
    p_re.add_argument("--approve-qa", action="store_true",
                      help="after moving, promote the target's needs_qa records to export_ready (requires --apply)")

    args = parser.parse_args()
    store = get_store()
    print(f"Asclepius DB: {store.db_path}\n")
    if args.command == "inspect":
        return cmd_inspect(store, args)
    if args.command == "reattribute":
        return cmd_reattribute(store, args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
