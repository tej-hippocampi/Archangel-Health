#!/usr/bin/env python3
"""Operator tool for the examination reminder; default action is read-only preview.

Run on the application host with its normal environment/store paths. No public
endpoint is exposed. `run` sends only due messages; `test` requires one explicit
recipient and never claims an applicant. `render` needs no database or transport.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="preview", choices=("preview", "history", "render", "test", "run"))
    parser.add_argument("--realm", choices=("live", "sandbox"), default="live")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--to", help="Explicit test recipient; only used by test")
    parser.add_argument("--out", type=Path, help="HTML output for render")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 500:
        parser.error("--limit must be between 1 and 500")
    if args.action == "test" and not args.to:
        parser.error("test requires --to")
    if args.action == "render" and not args.out:
        parser.error("render requires --out")
    import realm
    from asclepius import exam_reminder as reminder
    from onboarding_emails import EXAM_REMINDER_SUBJECT, build_exam_nudge_email, build_exam_nudge_text
    with realm.scoped(args.realm):
        if args.action in ("render", "test"):
            url = reminder.portal_url()
            body = build_exam_nudge_email(first_name="Dr. Morgan", portal_url=url)
            if args.action == "render":
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(body, encoding="utf-8")
                print(args.out.resolve())
                return 0
            if not reminder._EMAIL.fullmatch(args.to):
                parser.error("--to must be one bare email address")
            from email_utils import send_html_email_with_reason
            info = {}
            ok, reason = asyncio.run(send_html_email_with_reason(
                args.to, "[TEST] " + EXAM_REMINDER_SUBJECT, body,
                text_body=build_exam_nudge_text(first_name="Dr. Morgan", portal_url=url), delivery_info=info))
            print(json.dumps({"ok": ok, "detail": reason, **info}))
            return 0 if ok else 1
        from asclepius.store import get_store
        from team_store import get_team_store
        store = get_store()
        if args.action == "history":
            report = reminder.history(store, args.limit)
        elif args.action == "preview":
            report = reminder.preview(store, get_team_store())
        else:
            # Invalid configuration fails explicitly rather than pretending to
            # complete a requested operator send with zero recipients.
            reminder.portal_url()
            report = {"provider_accepted": asyncio.run(reminder.sweep(store, get_team_store(), limit=args.limit)),
                      "enabled": reminder.enabled(), "realm": args.realm}
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
