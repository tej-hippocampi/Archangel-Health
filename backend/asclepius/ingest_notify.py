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

    def _worker() -> None:
        box["v"] = asyncio.run(coro)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    return box.get("v")


def _recipient_for(store: Any, upload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(email, display_name) for the sender of this upload, or (None, name).

    Scoped to the secure-LINK door for now: the recipient is the link's
    ``contact_email``. Account-door uploads (link_id == 'account') intentionally
    resolve to NO recipient — wiring notifications for that door is a deliberate
    follow-up, so a rejected account upload never emails the provider."""
    link_id = upload.get("link_id")
    if not link_id or link_id == "account":
        return None, None
    link = store.get_upload_link(link_id) or {}
    name = (link.get("partner_label") or "").strip() or None
    email = (link.get("contact_email") or "").strip() or None
    return (email or None), name


def _subject() -> str:
    return "Your upload to Archangel Health didn't go through"


def _html_body(display_name: Optional[str], filename: Optional[str], outcome: str) -> str:
    who = html.escape(display_name) if display_name else "there"
    fname = html.escape(filename) if filename else "your file"
    # A high-level, non-technical line — never the internal reason string.
    what = ("we couldn't finish processing it, so it was not added to our system"
            if outcome != "lost" else
            "it could not be retrieved for processing, so it was not added to our system")
    return f"""\
<div style="font-family:Inter,system-ui,Arial,sans-serif;font-size:15px;color:#111827;line-height:1.6">
  <p>Hi {who},</p>
  <p>We received your recent upload (<strong>{fname}</strong>), but {what}.
     <strong>It has not been ingested.</strong></p>
  <p style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;padding:12px 14px;color:#065f46">
     Your data is safe. <strong>Nothing was leaked and there was no data breach</strong> —
     the file simply did not make it through our intake, and any partial copy has been discarded.
  </p>
  <p><strong>What to do next:</strong> please re-send the bundle using your secure upload link.
     If the link has expired or you need a fresh one, just reply to this email and we'll issue a new link.</p>
  <p>Thanks for helping us get this right,<br>The Archangel Health team</p>
</div>"""


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
