"""Explicit admin nudges with frozen previews and independent delivery history.

No scheduler calls this module. A preview never mints a link or sends email.
Each reviewed recipient has a durable one-use send ID. Uncertain outcomes are
held for operator reconciliation, never blindly retried.
"""
import asyncio
from datetime import timedelta
import json
import os
from urllib.parse import urlsplit
import uuid

from fastapi import HTTPException

import email_utils
import manual_reminder_emails as templates
import realm
from asclepius.exam_reminder import _EMAIL, iso, parse_time, portal_url, utcnow


def enabled():
    # Explicit rollout after the founder reviews these templates.
    return os.getenv("ASCLEPIUS_MANUAL_REMINDERS_ENABLED", "0").lower() in ("1", "true", "yes")


def candidates(store, ts, kind):
    from routers.asclepius_admin import _signup_report
    from routers.asclepius_verify import _has_credential_evidence
    from asclepius.onboarding_nudge import _exam_state
    from asclepius.store import NO_PASSWORD_HASH

    if kind == "wizard":
        rows = _signup_report(ts, store)["signups"]
        # Prefer the furthest progress, then newest activity, with a stable tie
        # break. One mailbox may own several abandoned signup links.
        rows.sort(key=lambda r: (r["stage_index"], r.get("last_activity") or "", r["health_system_id"]), reverse=True)
        rows = [{"email": r["email"], "name": r["name"] or "", "target": r["health_system_id"],
                 "member": r["kind"] == "invited"} for r in rows]
    else:
        rows = []
        for user in store.list_users():
            if (user.get("role") != "evaluator" or user.get("account_kind")
                    or user.get("verification_status") != "pending"
                    or not user.get("active", 1)
                    or (user.get("is_mock") and not realm.is_sandbox())
                    or not user.get("password_hash") or user["password_hash"] == NO_PASSWORD_HASH
                    or not _has_credential_evidence(user)
                    or _exam_state(user) not in ("not_started", "in_progress")):
                continue
            # Filed evidence wins even if a stale tutorial flag disagrees.
            if store.list_credentialing_exams(user["id"]):
                continue
            rows.append({"email": user["email"], "name": user.get("full_name") or "",
                         "target": user["id"], "member": False})
    unique = {}
    for row in rows:
        row["email"] = row["email"].strip().lower()
        if _EMAIL.fullmatch(row["email"]):
            unique.setdefault(row["email"], row)
    return list(unique.values())


def preview(store, ts, kind, actor, *, target=None, email=None):
    rows = candidates(store, ts, kind)
    if target:
        rows = [r for r in rows if r["target"] == target]
    if email:
        rows = [r for r in rows if r["email"] == email.strip().lower()]
    # The endpoint previews the entire cohort, independent of queue pagination.
    if len(rows) > 500:
        raise HTTPException(409, "More than 500 recipients. Use the individual reminder controls.")
    batch = uuid.uuid4().hex
    now = iso(utcnow())
    with store._conn() as conn:
        for row in rows:
            row["id"] = uuid.uuid4().hex
            conn.execute("""INSERT INTO manual_onboarding_reminders
                (id,batch_id,kind,recipient_email,recipient_json,actor,template_version,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (row["id"], batch, kind, row["email"], json.dumps(row),
                                                actor, templates.VERSION, now, now))
    example_url = "https://example.invalid/personal-onboarding-link" if kind == "wizard" else "https://example.invalid/asclepius#examination"
    return {"batch_id": batch, "recipients": rows,
            "template": templates.render(kind, rows[0]["name"] if rows else "", example_url),
            "recipient_templates": {r["id"]: templates.render(kind, r["name"], example_url) for r in rows},
            "preview_recipient": rows[0]["email"] if rows else None,
            "can_send": enabled() and email_utils.is_email_transport_configured()
            and (realm.is_sandbox() or not email_utils.is_email_dev_mode()),
            "enabled": enabled(), "expires_in_minutes": 30}


def claim(store, reminder_id, actor):
    now = utcnow()
    with store._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        raw = conn.execute("SELECT * FROM manual_onboarding_reminders WHERE id = ?", (reminder_id,)).fetchone()
        if not raw:
            raise HTTPException(404, "Reminder preview not found in this realm.")
        row = dict(raw)
        if row["actor"] != actor:
            raise HTTPException(403, "Open your own reminder preview before sending.")
        if row["status"] != "preview":
            return row, False
        if row["template_version"] != templates.VERSION or parse_time(row["created_at"]) + timedelta(minutes=30) < now:
            raise HTTPException(409, "This preview expired or the template changed. Open a fresh preview.")
        other = conn.execute("""SELECT id FROM manual_onboarding_reminders
            WHERE recipient_email = ? AND id != ? AND
              (status IN ('sending','unknown') OR (status = 'accepted' AND updated_at > ?)) LIMIT 1""",
            (row["recipient_email"], reminder_id, iso(now - timedelta(minutes=10)))).fetchone()
        if other:
            conn.execute("UPDATE manual_onboarding_reminders SET status='held',updated_at=? WHERE id=?", (iso(now), reminder_id))
            row["status"] = "held"
            return row, False
        conn.execute("UPDATE manual_onboarding_reminders SET status='sending',claimed_at=?,updated_at=? WHERE id=?", (iso(now), iso(now), reminder_id))
        return row, True


def finish(store, reminder_id, status, provider_id=None):
    with store._conn() as conn:
        conn.execute("UPDATE manual_onboarding_reminders SET status=?,provider_id=?,updated_at=? WHERE id=? AND status='sending'",
                     (status, provider_id, iso(utcnow()), reminder_id))


def wizard_url(ts, recipient):
    from routers.asclepius_admin import _landing_base
    base = _landing_base()
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Configure LANDING_URL with the HTTPS onboarding origin.")
    hs = ts.get_health_system_by_id(recipient["target"])
    if recipient["member"]:
        token = ts.issue_asclepius_member_token(hs["id"], recipient["email"])
        return realm.public_url(f"{base}/onboard/m/{token}")
    existing = (hs.get("last_generated_invite_url") or "").strip()
    if existing and ts.onboarding_token_valid(hs):
        # Do not disclose a resume credential to an unexpected host.
        expected, actual = urlsplit(base), urlsplit(existing)
        if actual.scheme != expected.scheme or actual.netloc != expected.netloc or not actual.path.startswith(expected.path.rstrip("/") + "/onboard/"):
            raise ValueError("Stored onboarding link does not match LANDING_URL.")
        return realm.public_url(existing)
    return realm.public_url(ts.reissue_onboarding_token(hs["id"], invite_base_url=base)["onboarding_url"])


def history(store):
    with store._conn() as conn:
        return [dict(row) for row in conn.execute("""SELECT id,kind,recipient_email,status,
            claimed_at,updated_at,provider_id FROM manual_onboarding_reminders
            WHERE status != 'preview' ORDER BY updated_at DESC LIMIT 200""")]


async def send(store, ts, reminder_id, actor):
    if not enabled():
        raise HTTPException(409, "Manual reminders are preview-only until the templates are approved and sending is enabled.")
    if not email_utils.is_email_transport_configured() or (email_utils.is_email_dev_mode() and not realm.is_sandbox()):
        raise HTTPException(503, "A live email transport is required.")
    row, claimed = await asyncio.to_thread(claim, store, reminder_id, actor)
    if not claimed:
        return {"id": reminder_id, "status": row["status"]}
    handed_off = False
    try:
        old = json.loads(row["recipient_json"])
        current = await asyncio.to_thread(candidates, store, ts, row["kind"])
        recipient = next((r for r in current if r["email"] == old["email"] and r["target"] == old["target"] and r["member"] == old["member"]), None)
        if not recipient:
            await asyncio.to_thread(finish, store, reminder_id, "skipped")
            return {"id": reminder_id, "status": "skipped"}
        url = portal_url() if row["kind"] == "examination" else await asyncio.to_thread(wizard_url, ts, recipient)
        mail = templates.render(row["kind"], recipient["name"], url)
        # Link creation can race with completion. Recheck before provider I/O.
        current = await asyncio.to_thread(candidates, store, ts, row["kind"])
        if recipient not in current:
            await asyncio.to_thread(finish, store, reminder_id, "skipped")
            return {"id": reminder_id, "status": "skipped"}
        info = {"reminder_id": reminder_id}
        handed_off = True
        ok, _detail = await email_utils.send_html_email_with_reason(
            recipient["email"], mail["subject"], mail["html"], text_body=mail["text"],
            reply_to=templates.REPLY_TO, delivery_info=info)
        outcome = info.get("outcome", "unknown")
        status = "accepted" if ok and outcome == "accepted" else "unknown"
        if not ok and outcome in ("failed", "retryable", "suppressed"):
            status = "failed" if outcome == "retryable" else outcome
        if ok and realm.is_sandbox() and outcome in ("dev_mode", "sandbox_outbox"):
            status = "sandbox"
        await asyncio.to_thread(finish, store, reminder_id, status, info.get("provider_id"))
        return {"id": reminder_id, "status": status}
    except Exception:
        status = "unknown" if handed_off else "failed"
        await asyncio.to_thread(finish, store, reminder_id, status)
        return {"id": reminder_id, "status": status}
