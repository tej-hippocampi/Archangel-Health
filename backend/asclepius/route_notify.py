"""Telling a physician that work was routed to them (Case Batches PRD §4).

An assignment is a database row. Until somebody is told, it is a row that changes
what the queue serves and nothing a human knows about — the doctor finds their
routed case only by opening the portal and happening to draw it. This module is
the sentence that closes that gap.

THREE RULES, and each of them was a decision rather than a default.

**One message per doctor per SEND, never per case.** An admin routing a 13-point
chart walk to one physician is one piece of news: "a chart walk landed, here is
what is in it". Thirteen DMs is not thirteen times as informative, it is a reason
to mute the sender — and the physician who mutes us is exactly the one we most
need to reach next time.

**Best-effort, and never in the transaction.** The assignment is the truth; the
ping is a courtesy. A community outage must not roll back routing that has
already happened and that the queue is already honouring — the doctor would then
have neither the work nor the message. So every failure here is logged and
swallowed, on the same rule ``mark_community_welcomed`` states for itself.

**No deadline pressure that was not actually set.** The copy says when work is
due only if ``due_at`` exists. Contributors are volunteers with clinics to run;
inventing urgency is how a channel stops being read.

The message also has to be honest about a detail the product gets asked about:
a routed case does NOT interrupt whatever the physician is doing. Assignment
affects the NEXT draw. So the copy says "finish the one you're on — yours comes
up right after you submit", because promising instant replacement and then not
delivering it reads as a broken queue.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from asclepius.ingest_notify import _run_coro

log = logging.getLogger("asclepius.route_notify")

#: Message ``kind`` on the channel post, so the dedupe query can find it.
ANNOUNCEMENT_KIND = "cases_routed"

#: What a case is called when we tell a physician about it. Keyed on the same
#: discriminators the queue and the Batches screen use, so the three cannot
#: describe one case three different ways.
CLASS_LABELS = {
    "longitudinal": "longitudinal",
    "real_static": "real de-identified",
    "synthetic": "synthetic multimodal",
}

#: The paragraph that renders only when the send contains trajectory points.
#: A physician meeting a chart walk for the first time needs to know it is a walk
#: BEFORE they open it — the commitment they are about to write is graded against
#: the chart's own next encounter, which is not how any other case here works.
LONGITUDINAL_PARAGRAPH = (
    "Longitudinal cases walk one real patient forward in time. You'll commit to "
    "an assessment, a plan, and what you expect to happen next — then the chart's "
    "next encounter is revealed and you check your own prediction against what "
    "actually happened. Take them in order; each point unlocks the next."
)


def classify(task: Optional[Dict[str, Any]]) -> str:
    """Which of the three classes this task belongs to."""
    t = task or {}
    if t.get("trajectory_id"):
        return "longitudinal"
    if t.get("case_source") == "real_deid":
        return "real_static"
    return "synthetic"


def _last_name(user: Optional[Dict[str, Any]]) -> str:
    u = user or {}
    for key in ("last_name", "family_name"):
        if (u.get(key) or "").strip():
            return str(u[key]).strip()
    full = (u.get("name") or u.get("full_name") or "").strip()
    if full:
        return full.split()[-1]
    email = (u.get("email") or "").strip()
    return email.split("@")[0] if email else "there"


def compose_dm(
    *, doctor: Dict[str, Any], tasks: Sequence[Dict[str, Any]],
    due_at: Optional[str] = None,
) -> str:
    """The message body for one doctor, listing every case in this send."""
    rows = list(tasks or [])
    classes = [classify(t) for t in rows]
    n = len(rows)
    label = CLASS_LABELS[classes[0]] if len(set(classes)) == 1 else "new"
    lines = [
        "New cases routed to you",
        "",
        f"Dr. {_last_name(doctor)} — {n} new {label} case{'s' if n != 1 else ''} "
        f"just landed in your queue:",
        "",
    ]
    for t, cls in zip(rows, classes):
        bits = [str(t.get("specialty") or "general"), str(t.get("difficulty") or "—"),
                CLASS_LABELS[cls]]
        if cls == "longitudinal" and t.get("sequence_index") is not None:
            bits.append(f"decision {int(t['sequence_index']) + 1}")
        lines.append("  · " + " · ".join(bits))
    lines += [
        "",
        "They'll appear automatically: if you're mid-case, finish it — your routed "
        "case comes up right after you submit. If you're starting fresh, just hit "
        "Start new case.",
    ]
    if "longitudinal" in classes:
        lines += ["", LONGITUDINAL_PARAGRAPH]
    if due_at:
        lines += ["", f"These are yours first until {str(due_at)[:10]}."]
    lines += ["", "Questions mid-case? Post in #questions-help.", "— Archangel"]
    return "\n".join(lines)


def compose_channel_post(tasks: Sequence[Dict[str, Any]]) -> str:
    """The #task-announcements post, for a send-to-all only."""
    rows = list(tasks or [])
    classes = {classify(t) for t in rows}
    label = CLASS_LABELS[next(iter(classes))] if len(classes) == 1 else "new"
    specialties = sorted({str(t.get("specialty") or "general") for t in rows})
    n = len(rows)
    body = (f"{n} {label} case{'s' if n != 1 else ''} "
            f"({', '.join(specialties)}) are now in the open queue. "
            f"Any approved physician may pick them up — hit Start new case.")
    if "longitudinal" in classes:
        body += "\n\n" + LONGITUDINAL_PARAGRAPH
    return body


async def _dm_one(cstore: Any, *, doctor_id: str, body: str) -> bool:
    from community.system_posts import SYSTEM_USER_ID

    dm = cstore.get_or_create_dm(SYSTEM_USER_ID, doctor_id)
    cstore.insert_message(channel_id=dm["id"], author_user_id=SYSTEM_USER_ID,
                          body=body, kind=ANNOUNCEMENT_KIND)
    return True


def notify_routed(
    store: Any, *, assignments: Sequence[Dict[str, Any]],
    to_all: bool = False, due_at: Optional[str] = None,
    task_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Tell everyone this send concerns. Never raises.

    ``assignments`` are the committed rows (``task_id``/``user_id``); ``task_ids``
    is used for the send-to-all case, where there are no assignments by design —
    the cases went to the open queue and the announcement is the whole delivery.

    Returns a small report so the caller can log what actually went out, rather
    than assuming. A caller that logged "notified 4 doctors" without checking
    would be reporting an intention.
    """
    report: Dict[str, Any] = {"dms": 0, "channel": False, "errors": []}
    try:
        from community.store import get_community_store
        cstore = get_community_store()
    except Exception as exc:                       # pragma: no cover - import guard
        log.info("route_notify: community unavailable (%s)", exc)
        report["errors"].append(f"community_unavailable:{exc}")
        return report

    # ── the targeted half: one DM per doctor, listing their cases ────────────
    by_user: Dict[str, List[Dict[str, Any]]] = {}
    for a in (assignments or []):
        if (a.get("role") or "label") != "label":
            continue
        task = store.get_task(a.get("task_id"))
        if task:
            by_user.setdefault(a["user_id"], []).append(task)

    for user_id, tasks in by_user.items():
        try:
            doctor = store.get_user_by_id(user_id) or {"id": user_id}
            tasks.sort(key=lambda t: (t.get("trajectory_id") or "",
                                      t.get("sequence_index") if t.get("sequence_index")
                                      is not None else -1))
            body = compose_dm(doctor=doctor, tasks=tasks, due_at=due_at)
            if _run_coro(_dm_one(cstore, doctor_id=user_id, body=body)):
                report["dms"] += 1
        except Exception as exc:
            # One doctor's DM failing must not cost the other three theirs, and
            # must not cost anybody their assignment.
            log.info("route_notify: DM to %s failed: %s", user_id, exc)
            report["errors"].append(f"dm:{user_id}:{exc}")

    # ── the send-to-all half: one channel post, and no DMs ──────────────────
    if to_all:
        try:
            rows = [t for t in (store.get_task(tid) for tid in (task_ids or [])) if t]
            if rows:
                report["channel"] = bool(_run_coro(
                    _post_announcement(compose_channel_post(rows))))
        except Exception as exc:
            log.info("route_notify: channel post failed: %s", exc)
            report["errors"].append(f"channel:{exc}")
    return report


async def _post_announcement(body: str) -> bool:
    """#task-announcements, authored by the bot rather than by the admin.

    An admin-signed announcement renders as "Former member" the moment that
    account is deprovisioned, and it is the platform speaking here, not a person
    — the same reasoning ``task_notify.post_community_announcement`` gives.
    """
    from community.system_posts import post_system_message

    # By SLUG, not id: post_system_message resolves the channel itself and skips
    # an unknown or inactive one rather than writing into it. ``announce=True`` is
    # the all-member fan-out, which #task-announcements is the only channel
    # entitled to — and a send-to-all is exactly the case it exists for.
    msg = await post_system_message(
        channel_slug="task-announcements", body=body,
        kind=ANNOUNCEMENT_KIND, announce=True)
    return msg is not None
