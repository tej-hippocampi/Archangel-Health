"""Notify a partner when their secure upload did NOT come through.

Deliberately contains NO PHI and NO internal pipeline detail — the whole point
is a reassuring, safe message: *your file didn't process, nothing was leaked,
there was no breach, please re-send*. Used two ways:

  * automatically, when an upload ends in a terminal failure (rejected, or its
    raw blob was lost) — fired once per upload (deduped on ``failure_notified_at``);
  * manually, from the admin "Notify sender" button on an upload row.

The recipient is resolved from the upload's link ``contact_email`` (magic-link
door) or the data-provider account email (account door). If neither exists we
report that back to the admin rather than guessing.
"""

from __future__ import annotations

import asyncio
import contextvars
import html
import logging
import re
import threading
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("asclepius.ingest_notify")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def looks_like_email(value: Optional[str]) -> bool:
    return bool(value and _EMAIL_RE.match(value.strip()))


def _run_coro(coro: Any) -> Any:
    """Run an async coroutine from sync code, whether or not a loop is running.

    The auto path runs inside a sync BackgroundTask / a to_thread worker (no
    running loop → asyncio.run is fine). The manual path runs inside the async
    request handler (a loop IS running → nest it in a worker thread)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: Dict[str, Any] = {}
    # A bare Thread starts with an EMPTY context: the realm ContextVar (and any
    # other) is not inherited, so everything the coroutine touched ran in the
    # live realm — a sandbox "send to all" announcement posted into the real
    # #task-announcements, a sandbox upload notice sent as a real email. Run
    # the worker inside a copy of the caller's context instead.
    ctx = contextvars.copy_context()

    def _worker() -> None:
        box["v"] = ctx.run(asyncio.run, coro)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    return box.get("v")


def _recipient_for(store: Any, upload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(email, display_name) for the sender of this upload, or (None, name).

    Two doors resolve differently:
      * secure LINK door — the recipient is the link's ``contact_email``.
      * health-system PORTAL door — the recipient is the health system's
        ``contact_email`` (FIX-C C-3.3). This door had no failure loop at all:
        its sentinel link_id has no link row, so a rejected hospital upload
        emailed nobody, on the one door whose users we cannot support in real
        time and whose URL is public.

    The account door (link_id == 'account') still resolves to no recipient."""
    link_id = upload.get("link_id")
    if not link_id or link_id == "account":
        return None, None
    if link_id == "hs-portal" or upload.get("health_system_id"):
        getter = getattr(store, "get_health_system", None)
        hs = (getter(upload.get("health_system_id") or "") or {}) if getter else {}
        name = (hs.get("name") or "").strip() or None
        email = (hs.get("contact_email") or "").strip() or None
        return (email or None), name
    link = store.get_upload_link(link_id) or {}
    name = (link.get("partner_label") or "").strip() or None
    email = (link.get("contact_email") or "").strip() or None
    return (email or None), name


def _subject() -> str:
    return "Your upload to Archangel Health didn't go through"


def _html_body(display_name: Optional[str], filename: Optional[str], outcome: str) -> str:
    from onboarding_emails import build_upload_failed_email  # noqa: PLC0415

    # A high-level, non-technical line. NEVER the internal reason string: this
    # email goes to an external data partner and must not describe our pipeline.
    what = ("we couldn't finish processing it, so it was not added to our system"
            if outcome != "lost" else
            "it could not be retrieved for processing, so it was not added to our system")
    return build_upload_failed_email(
        recipient_name=display_name or "there",
        filename=filename or "your file",
        reason=what,
    )


def notify_upload_failed(
    store: Any, upload: Dict[str, Any], *, outcome: str = "rejected",
    manual: bool = False, actor: Optional[str] = None,
) -> Tuple[bool, str]:
    """Email the sender that their upload failed. Returns ``(sent, detail)``.

    ``manual=False`` (auto) is idempotent: it no-ops if already notified. Never
    raises — a notification problem must not affect the ingestion pipeline."""
    try:
        upload_id = upload.get("upload_id")
        if not manual and upload.get("failure_notified_at"):
            return False, "already notified"
        email, name = _recipient_for(store, upload)
        if not looks_like_email(email):
            detail = ("no contact email on this upload's link — set one when minting "
                      "the link, or ask the partner for their address")
            log.info("ingest notify: upload %s has no recipient (%s)", upload_id, detail)
            return False, detail

        from email_utils import send_html_email_with_reason

        ok, reason = _run_coro(send_html_email_with_reason(
            email, _subject(), _html_body(name, upload.get("filename"), outcome)))
        if ok:
            store.mark_upload_failure_notified(upload_id)
            store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                            event_type="upload_failure_notified", actor=actor,
                            payload={"outcome": outcome, "manual": manual,
                                     "recipient_domain": email.split("@")[-1]})
            return True, f"notified {email}"
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="upload_failure_notify_error", actor=actor,
                        payload={"outcome": outcome, "manual": manual, "reason": reason})
        return False, reason or "email transport failed"
    except Exception as exc:  # pragma: no cover - defensive; never break ingestion
        log.warning("ingest notify: upload %s failed: %s", upload.get("upload_id"), exc)
        return False, str(exc)
