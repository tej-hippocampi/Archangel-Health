"""One examination reminder after 36 hours from completed credentials.

Uses the existing scheduler and realm-scoped stores. Provider acceptance is
recorded separately from claiming a send. Unknown outcomes are never retried
automatically: SendGrid/SMTP do not promise idempotent submission.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import logging
import os
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

import realm

log = logging.getLogger(__name__)
AFTER_HOURS = 36
MAX_ATTEMPTS = 3
_EMAIL = re.compile(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+\Z")


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def iso(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def enabled():
    return os.getenv("ASCLEPIUS_EXAM_REMINDER_ENABLED", "1").strip().lower() in ("1", "true", "yes")


def portal_url():
    """Configured first-party destination; never a link from applicant input."""
    base = (os.getenv("ASCLEPIUS_PORTAL_URL") or os.getenv("BASE_URL") or "").strip()
    parts = urlsplit(base)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError("Set ASCLEPIUS_PORTAL_URL to the HTTPS portal origin before sending reminders.")
    path = parts.path.rstrip("/")
    if path not in ("", "/asclepius"):
        raise ValueError("ASCLEPIUS_PORTAL_URL must be the portal origin or its /asclepius page.")
    # public_url appends a realm query; it must precede the fragment.
    return realm.public_url(urlunsplit((parts.scheme, parts.netloc, "/asclepius", "", ""))) + "#examination"


def _historical_completions(ts):
    # Test doubles for the unrelated pre-submit sweep need no new interface.
    read = getattr(ts, "completed_physician_applications", None)
    return {row["email"]: row["completed_at"] for row in read()} if read else {}


def _completion(user, historical):
    return user.get("application_completed_at") or historical.get((user.get("email") or "").strip().lower())


def skip_reason(user, completed_at, delivery=None, *, now=None):
    """The preview and final send use the same predicate."""
    from routers.asclepius_verify import _has_credential_evidence
    from asclepius.onboarding_nudge import _exam_state
    from asclepius.store import NO_PASSWORD_HASH
    now = now or utcnow()
    if not user:
        return "account_missing"
    if user.get("role") != "evaluator" or user.get("account_kind"):
        return "not_physician"
    if user.get("verification_status") != "pending":
        return "application_decided"
    if not user.get("active", 1) or (user.get("is_mock") and not realm.is_sandbox()):
        return "inactive_or_test_account"
    if not _EMAIL.fullmatch((user.get("email") or "").strip()):
        return "invalid_email"
    if not user.get("password_hash") or user["password_hash"] == NO_PASSWORD_HASH:
        return "password_setup_required"
    if not _has_credential_evidence(user):
        return "credentials_missing"
    if _exam_state(user) == "submitted":
        return "examination_submitted"
    if _exam_state(user) not in ("not_started", "in_progress"):
        return "examination_state_unknown"
    if user.get("nudge_exam_sent_at"):
        return "previously_reminded"
    at = parse_time(completed_at)
    if at is None:
        return "completion_time_unknown"
    if at + timedelta(hours=AFTER_HOURS) > now:
        return "not_due"
    if delivery:
        status = delivery["status"]
        if status not in ("retryable",):
            return status
        if delivery["attempts"] >= MAX_ATTEMPTS:
            return "retry_limit"
        retry_at = parse_time(delivery.get("next_attempt_at"))
        if retry_at and retry_at > now:
            return "retry_wait"
    return None


def preview(store, ts, *, now=None):
    """No claims, history changes, or messages. Only operator-safe fields."""
    now = now or utcnow()
    historical = _historical_completions(ts)
    with store._conn() as conn:
        users = conn.execute("SELECT * FROM users WHERE verification_status = 'pending' ORDER BY id").fetchall()
        deliveries = {r["user_id"]: dict(r) for r in conn.execute("SELECT * FROM exam_reminder_deliveries")}
    rows = []
    for raw in users:
        user = dict(raw)
        completed = _completion(user, historical)
        reason = skip_reason(user, completed, deliveries.get(user["id"]), now=now)
        at = parse_time(completed)
        rows.append({"user_id": user["id"], "email": user["email"],
                     "completed_at": iso(at) if at else None,
                     "due_at": iso(at + timedelta(hours=AFTER_HOURS)) if at else None,
                     "eligible": reason is None, "reason": reason or "due"})
    rows.sort(key=lambda row: (row["due_at"] or "9999", row["user_id"]))
    return {"enabled": enabled(), "realm": realm.current(), "after_hours": AFTER_HOURS,
            "counts": dict(Counter(row["reason"] for row in rows)), "recipients": rows}


def history(store, limit=100):
    with store._conn() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM exam_reminder_deliveries ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 1000)),))]


def claim(store, user_id, completed_at, *, now=None):
    """Recheck current eligibility and claim atomically; no I/O under lock."""
    now = now or utcnow()
    token = uuid.uuid4().hex
    with store._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        old = conn.execute("SELECT * FROM exam_reminder_deliveries WHERE user_id = ?", (user_id,)).fetchone()
        if skip_reason(dict(user) if user else {}, completed_at, dict(old) if old else None, now=now):
            return None
        conn.execute("""INSERT INTO exam_reminder_deliveries
            (user_id, status, attempts, claim_token, claimed_at, recipient_email, updated_at)
            VALUES (?, 'sending', 1, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET status = 'sending', attempts = attempts + 1,
                claim_token = excluded.claim_token, claimed_at = excluded.claimed_at,
                recipient_email = excluded.recipient_email, updated_at = excluded.updated_at,
                next_attempt_at = NULL, detail = NULL""",
            (user_id, token, iso(now), user["email"], iso(now)))
        # Backfill only the proven completion event, never account creation.
        conn.execute("UPDATE users SET application_completed_at = ? WHERE id = ? AND application_completed_at IS NULL",
                     (completed_at, user_id))
    return token


def finish(store, user_id, token, status, detail, provider_id=None):
    now = utcnow()
    with store._conn() as conn:
        row = conn.execute("SELECT attempts FROM exam_reminder_deliveries WHERE user_id = ? AND claim_token = ? AND status = 'sending'",
                           (user_id, token)).fetchone()
        if not row:
            return False
        if status == "retryable" and row["attempts"] >= MAX_ATTEMPTS:
            status = "failed"
        retry = iso(now + timedelta(minutes=15 * (2 ** (row["attempts"] - 1)))) if status == "retryable" else None
        accepted = iso(now) if status == "accepted" else None
        conn.execute("""UPDATE exam_reminder_deliveries SET status = ?, detail = ?, provider_id = ?,
            accepted_at = ?, next_attempt_at = ?, updated_at = ? WHERE user_id = ? AND claim_token = ?""",
            (status, str(detail)[:1000], provider_id, accepted, retry, iso(now), user_id, token))
        if accepted:
            conn.execute("UPDATE users SET nudge_exam_sent_at = COALESCE(nudge_exam_sent_at, ?) WHERE id = ?", (accepted, user_id))
    return True


async def sweep(store, ts, *, limit=50):
    from email_utils import is_email_transport_configured, is_email_dev_mode, send_html_email_with_reason
    from onboarding_emails import EXAM_REMINDER_SUBJECT, build_exam_nudge_email, build_exam_nudge_text
    if not enabled() or not is_email_transport_configured():
        return 0
    # Dev-mode acceptance must not consume real applicants' reminders.
    if is_email_dev_mode() and not realm.is_sandbox():
        return 0
    try:
        url = portal_url()
    except ValueError as exc:
        log.warning("[exam reminder] %s", exc)
        return 0
    # A worker lost after provider handoff cannot prove whether mail left.
    # Expiry changes its visible outcome, never makes it eligible to resend.
    def expire_claims():
        with store._conn() as conn:
            conn.execute("UPDATE exam_reminder_deliveries SET status = 'unknown', detail = 'Worker stopped before recording provider outcome', updated_at = ? "
                         "WHERE status = 'sending' AND claimed_at < ?",
                         (iso(utcnow()), iso(utcnow() - timedelta(minutes=15))))
    await asyncio.to_thread(expire_claims)
    report = await asyncio.to_thread(preview, store, ts)
    due = [r for r in report["recipients"] if r["eligible"]][:max(1, min(limit, 500))]
    sent = 0
    for row in due:
        token = await asyncio.to_thread(claim, store, row["user_id"], row["completed_at"])
        if not token:
            continue
        try:
            user = await asyncio.to_thread(store.get_user_by_id, row["user_id"])
            reason = skip_reason(user or {}, row["completed_at"])
            if reason or user["email"] != row["email"]:
                await asyncio.to_thread(finish, store, row["user_id"], token, "suppressed", reason or "address_changed")
                continue
            info = {"reminder_id": token}
            ok, detail = await send_html_email_with_reason(
                user["email"], EXAM_REMINDER_SUBJECT,
                build_exam_nudge_email(first_name=user.get("full_name") or "", portal_url=url),
                text_body=build_exam_nudge_text(first_name=user.get("full_name") or "", portal_url=url),
                delivery_info=info,
            )
            outcome = info.get("outcome", "unknown")
            if ok:
                status = "unknown" if outcome == "dev_mode" else "accepted"
            else:
                # Capturing a sandbox email can fail before any delivery.
                status = "retryable" if outcome == "sandbox_outbox" else outcome
                if status == "accepted":
                    status = "unknown"
            if status not in ("accepted", "retryable", "failed", "suppressed"):
                status = "unknown"
            await asyncio.to_thread(finish, store, user["id"], token, status, detail, info.get("provider_id"))
            sent += status == "accepted"
        except Exception as exc:
            log.exception("[exam reminder] uncertain send for %s", row["user_id"])
            await asyncio.to_thread(finish, store, row["user_id"], token, "unknown", type(exc).__name__)
    return sent
