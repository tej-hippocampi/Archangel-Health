"""Specialty-tagged task notifications: notify every matching evaluator once
per admin upload batch that new work is available.

Modeled on ``ingest_notify.py``'s pattern (never raise, sync/async bridge via
``_run_coro``), but delivery is decoupled from computation via a persistent
outbox (``task_notify_outbox``) so up to ~1000 emails never run inline in the
admin's request and a crash mid-send loses nothing — unsent rows just stay
``pending`` until the next drain (background or the manual re-drain endpoint).
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List, Tuple

from asclepius.ingest_notify import _run_coro

log = logging.getLogger("asclepius.task_notify")


def _app_base() -> str:
    return (os.getenv("BASE_URL") or "http://localhost:8000").strip().rstrip("/")


def _workspace_url() -> str:
    return f"{_app_base()}/asclepius"


def _specialty_label(specialty: str) -> str:
    return (specialty or "").strip().replace("_", " ").title() or "general"


def _idempotency_key(*, batch_id: str, specialty: str, recipient_email: str) -> str:
    raw = f"{batch_id}|{specialty}|{recipient_email.lower().strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _counts_by_specialty(created_tasks: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for t in created_tasks:
        specialty = (t.get("specialty") or "").strip()
        if not specialty:
            continue
        counts[specialty] = counts.get(specialty, 0) + 1
    return counts


def enqueue_for_batch(store: Any, *, batch_id: str, created_tasks: List[Dict[str, Any]]) -> int:
    """Group ``created_tasks`` (each ``{"task_id":..., "specialty":...}``) by
    specialty, then enqueue ONE outbox row per matching evaluator per specialty
    (task_count = how many new tasks in that specialty this batch). Never
    raises — a notify problem must not break task upload."""
    try:
        counts = _counts_by_specialty(created_tasks)
        enqueued = 0
        for specialty, task_count in counts.items():
            recipients = store.list_evaluators_by_specialty(specialty)
            for user in recipients:
                email = (user.get("email") or "").strip()
                if not email:
                    continue
                key = _idempotency_key(batch_id=batch_id, specialty=specialty, recipient_email=email)
                row_id = store.enqueue_task_notification(
                    idempotency_key=key,
                    batch_id=batch_id,
                    specialty=specialty,
                    task_count=task_count,
                    recipient_user_id=user.get("id"),
                    recipient_email=email,
                )
                if row_id is not None:
                    enqueued += 1
        return enqueued
    except Exception as exc:  # pragma: no cover - defensive; never break task upload
        log.warning("task_notify: enqueue_for_batch(batch_id=%s) failed: %s", batch_id, exc)
        return 0


def drain_outbox(store: Any, *, limit: int = 500) -> Tuple[int, int]:
    """Send every ``pending`` outbox row (up to ``limit``). Returns
    ``(sent_count, failed_count)``. Never raises."""
    sent = 0
    failed = 0
    try:
        from email_utils import send_html_email_with_reason
        from onboarding_emails import build_asclepius_task_notification_email

        workspace_url = _workspace_url()
        for row in store.list_pending_task_notifications(limit=limit):
            row_id = row["id"]
            email = row["recipient_email"]
            try:
                html_body = build_asclepius_task_notification_email(
                    specialty_label=_specialty_label(row["specialty"]),
                    task_count=int(row["task_count"]),
                    workspace_url=workspace_url,
                )
                subject = (
                    f"{row['task_count']} new {_specialty_label(row['specialty'])} "
                    f"{'task' if row['task_count'] == 1 else 'tasks'} ready in your Asclepius workspace"
                )
                ok, reason = _run_coro(
                    send_html_email_with_reason(email, subject, html_body)
                )
                if ok:
                    store.mark_task_notification_sent(row_id)
                    store.log_event(
                        entity_type="task_notify", entity_id=str(row_id),
                        event_type="task_notification_sent",
                        payload={
                            "batch_id": row["batch_id"], "specialty": row["specialty"],
                            "task_count": row["task_count"],
                            "recipient_domain": email.split("@")[-1] if "@" in email else None,
                        },
                    )
                    sent += 1
                else:
                    store.mark_task_notification_failed(row_id, reason or "email transport failed")
                    store.log_event(
                        entity_type="task_notify", entity_id=str(row_id),
                        event_type="task_notification_failed",
                        payload={
                            "batch_id": row["batch_id"], "specialty": row["specialty"],
                            "reason": reason,
                        },
                    )
                    failed += 1
            except Exception as exc:  # pragma: no cover - defensive per-row
                log.warning("task_notify: drain row %s failed: %s", row_id, exc)
                try:
                    store.mark_task_notification_failed(row_id, str(exc))
                except Exception:
                    pass
                failed += 1
        return sent, failed
    except Exception as exc:  # pragma: no cover - defensive; never break the caller
        log.warning("task_notify: drain_outbox failed: %s", exc)
        return sent, failed


def _announcement_text(created_tasks: List[Dict[str, Any]]) -> str:
    counts = _counts_by_specialty(created_tasks)
    if not counts:
        return ""
    parts = []
    for specialty, task_count in counts.items():
        label = _specialty_label(specialty)
        noun = "task" if task_count == 1 else "tasks"
        parts.append(f"{task_count} new {label} {noun}")
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    verb = "is" if sum(counts.values()) == 1 else "are"
    return f"{joined} {verb} ready to review."


def post_community_announcement(
    store: Any, *, admin_user_id: str, created_tasks: List[Dict[str, Any]],
) -> bool:
    """Post a one-line system announcement to #task-announcements (as the
    uploading admin) when new tasks land, so contributors see it in-app —
    not just via email. #task-announcements already fans out an email digest
    to every member on a top-level post (community/notify.py), so this is
    the whole integration; no new channel/membership model needed. Never
    raises — a community-post problem must not break task upload."""
    try:
        text = _announcement_text(created_tasks)
        if not text or not admin_user_id:
            return False

        from community import notify as cnotify
        from community import phi_gate
        from community.router import member_map
        from community.store import get_community_store

        cstore = get_community_store()
        channel = cstore.get_channel_by_slug("task-announcements")
        if not channel:
            log.warning("task_notify: #task-announcements channel missing")
            return False

        # Server-generated boilerplate (specialty names + counts), but the
        # PHI gate is cheap and every other write path into this channel
        # runs it — stay consistent rather than assume this text is exempt.
        if phi_gate.scan_text(text):
            log.warning("task_notify: announcement text unexpectedly flagged by PHI gate, skipping post")
            return False

        members = member_map()
        msg = cstore.insert_message(
            channel_id=channel["id"], author_user_id=admin_user_id, body=text,
        )
        cstore.set_read(admin_user_id, channel["id"], msg["id"])
        cnotify.queue_for_message(
            cstore, message=msg, channel=channel, member_ids=list(members.keys())
        )
        store.log_event(
            entity_type="task_notify", event_type="task_notification_community_posted",
            actor=admin_user_id, payload={"message_id": msg["id"], "text": text},
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive; never break task upload
        log.warning("task_notify: post_community_announcement failed: %s", exc)
        return False
