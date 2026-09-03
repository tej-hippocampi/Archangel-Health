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


def _first_name(user: Optional[Dict[str, Any]]) -> str:
    """What a colleague is called in an introduction.

    Falls back through the same ladder as ``_last_name`` and ends at the mailbox
    name, because a room that introduces somebody as "there" is worse than one
    that introduces them by an ugly handle they can correct."""
    u = user or {}
    for key in ("first_name", "given_name"):
        if (u.get(key) or "").strip():
            return str(u[key]).strip()
    full = (u.get("name") or u.get("full_name") or "").strip()
    if full:
        return full.split()[0]
    email = (u.get("email") or "").strip()
    return email.split("@")[0] if email else "a colleague"


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


# ═══ Task Pipeline PRD §B: the per-case room ════════════════════════════════
#: Message ``kind`` on the bot's room posts, so a room's own housekeeping is
#: distinguishable from what the doctors said in it.
ROOM_KIND = "case_room_notice"

#: The rule the room was approved under (CASE_BATCHES_AND_ROUTING §8.5), stated
#: in the room rather than in a policy document nobody in the room has read.
#: The two labels have to stay independent for kappa to mean anything, and the
#: evidence for that is the pre-reveal blind commit, which a conversation about
#: the case would not invalidate on paper and would absolutely invalidate in
#: fact. So the rule is the first thing anybody sees here.
NO_CASE_CONTENT_RULE = (
    "This room is for coordination and introductions only. The case itself "
    "stays in the portal: do not discuss the case, your findings, or your "
    "answer here. Your labels have to be independent, and that is what makes "
    "the work worth anything."
)

#: PRD D3. Said out loud, because a room people believe is private and is not
#: is worse than no room.
ADMIN_VISIBILITY_LINE = "Archangel admins can see this room."


def case_ref_for_task(task: Optional[Dict[str, Any]]) -> Optional[str]:
    """The stable key a case's room is filed under (PRD D2).

    A chart walk keys on its TRAJECTORY: the walk is one case taken forward by
    several people, and keying its points separately would give a relay one room
    per decision point rather than one room per case. Everything else keys on the
    task. Prefixed, so the two id spaces can never collide on one ``case_ref``.
    """
    t = task or {}
    if t.get("trajectory_id"):
        return "traj:" + str(t["trajectory_id"])
    if t.get("task_id"):
        return "task:" + str(t["task_id"])
    return None


def room_title(*, specialty: str, class_label: str) -> str:
    return f"Case room: {specialty} {class_label} case"


def compose_room_intro(*, people: Sequence[Dict[str, Any]], specialty: str,
                       class_label: str) -> str:
    """The bot's first post in a new room.

    Names, roles, the case TYPE and the specialty. NOTHING about the case: no
    stem, no findings, no ground truth, no task id. That restriction is not
    caution, it is the condition §8.5 approved rooms under, and the paragraph
    that states it is in the message the people who could break it will read.

    ``people`` is ``[{"user": <user row>, "role": "label"|"review"}, ...]``.
    """
    lines = [
        room_title(specialty=specialty, class_label=class_label),
        "",
        "You're the team on one case. Introductions first:",
        "",
    ]
    for p in people:
        word = "reviewer" if (p.get("role") == "review") else "labeler"
        lines.append(f"  · Dr. {_first_name(p.get('user'))} · {word}")
    lines += [
        "",
        NO_CASE_CONTENT_RULE,
        "",
        "Use it for the things that are not the case: who is picking it up when, "
        "a handoff, a question for the team.",
        "",
        ADMIN_VISIBILITY_LINE,
        "",
        "— Archangel",
    ]
    return "\n".join(lines)


def compose_roster_change(*, doctor: Dict[str, Any], position: int,
                          n_points: int) -> str:
    """What the room is told when a point changes hands (PRD B5).

    The gap this closes: the replacement was DMed and nobody else on the walk was
    told the roster had changed, so the physician waiting on a handoff was
    waiting on a person who no longer had it.

    Says who has it now and nothing about who lost it, for the same reason
    ``notify_reassigned`` does not: the previous holder had a clinic or a bad
    week, and their colleagues do not need it framed for them.
    """
    return "\n".join([
        f"Dr. {_last_name(doctor)} now has point {position} of {n_points}.",
        "",
        "— Archangel",
    ])


def _audit_room(action: str, *, case_ref: str, dm_id: str,
                detail: Optional[Dict[str, Any]] = None) -> None:
    """Room lifecycle into the community audit chain, with ``case_ref`` (PRD B7).

    Supports D4: if a labeled pair is ever suspected of having compared notes,
    the audit trail says which room existed for that case and when its
    membership moved, which is the evidence a blind-commit claim gets checked
    against. Metadata only, like every other community audit line.
    """
    try:
        from audit import audit_log

        audit_log.record(
            actor_type="system", actor_id="asclepius.route_notify",
            action=action, outcome="ok", resource_type="community", resource=dm_id,
            detail=dict(detail or {}, case_ref=case_ref, dm_id=dm_id),
        )
    except Exception as exc:  # pragma: no cover - audit must never break routing
        log.info("route_notify: room audit (%s) failed: %s", action, exc)


def ensure_case_room(cstore: Any, *, case_ref: str,
                     people: Sequence[Dict[str, Any]], specialty: str,
                     class_label: str) -> Dict[str, Any]:
    """Get-or-create the room for one case and introduce the team ONCE.

    The intro is posted only by the caller that actually created the row, so a
    second send against the same case reuses the room and does not re-introduce
    people who have been talking in it for a week.
    """
    from community.system_posts import SYSTEM_USER_ID

    room = cstore.get_or_create_case_room(
        case_ref, [p["user"]["id"] for p in people if (p.get("user") or {}).get("id")],
        title=room_title(specialty=specialty, class_label=class_label))
    if room.get("created"):
        cstore.insert_message(
            channel_id=room["id"], author_user_id=SYSTEM_USER_ID,
            body=compose_room_intro(people=people, specialty=specialty,
                                    class_label=class_label),
            kind=ROOM_KIND)
        _audit_room("community.case_room_created", case_ref=case_ref,
                    dm_id=room["id"], detail={"participants": len(people)})
    return room


def _room_people(store: Any, assignments: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Resolve assignment rows to ``{user, role}`` entries, labelers first.

    Deduped by user: a doctor holding two points of the same walk is one person
    in the room, not two.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for a in assignments or []:
        uid = a.get("user_id")
        if not uid or uid in seen:
            continue
        user = store.get_user_by_id(uid) or {"id": uid}
        seen[uid] = {"user": user, "role": (a.get("role") or "label")}
    return sorted(seen.values(), key=lambda p: 1 if p["role"] == "review" else 0)


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
    report: Dict[str, Any] = {"dms": 0, "channel": False, "rooms": 0, "errors": []}
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

    # ── the room half: one per CASE, labelers plus reviewer (PRD B4) ─────────
    # Reviewers are in the room and not in the DM loop above on purpose: the DM
    # says "cases landed in your queue", which is not what a reviewer was sent.
    # The room is the one place the whole team is addressed at once.
    #
    # A send-to-all writes no assignments by design, so this loop is empty for
    # one and no room is created (B6): there is no roster to introduce.
    by_case: Dict[str, Dict[str, Any]] = {}
    for a in (assignments or []):
        task = store.get_task(a.get("task_id"))
        ref = case_ref_for_task(task)
        if not ref:
            continue
        grp = by_case.setdefault(ref, {"task": task, "assignments": []})
        grp["assignments"].append(a)
    for ref, grp in by_case.items():
        try:
            people = _room_people(store, grp["assignments"])
            if len(people) < 2:
                # One person is not a team, and a room the bot introduces you to
                # yourself in reads as a bug.
                continue
            ensure_case_room(
                cstore, case_ref=ref, people=people,
                specialty=str(grp["task"].get("specialty") or "clinical"),
                class_label=CLASS_LABELS[classify(grp["task"])])
            report["rooms"] += 1
        except Exception as exc:
            # PRD B4: room creation never fails the send. The assignment is
            # already committed and the DMs already went; a community write that
            # falls over must cost the case its room and nothing else.
            log.info("route_notify: case room for %s failed: %s", ref, exc)
            report["errors"].append(f"room:{ref}:{exc}")

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


# ═══ §8.6 — relay messages ═══════════════════════════════════════════════════
def compose_relay_assignment(*, doctor, position, n_points, specialty, is_first):
    """What each doctor is told at send: their point, their place in line, and
    whether they are up now or waiting.

    Everyone is told at send rather than only when their turn arrives, because a
    physician who gets a "you're up" DM about a chart they have never heard of
    reads it as spam. The difference between "yours now" and "yours later" is
    stated plainly, so nobody opens the portal looking for work that is correctly
    still sealed and concludes the queue is broken.
    """
    lines = [
        "You're on a care-team relay" if not is_first else "You're up — care-team relay",
        "",
        f"Dr. {_last_name(doctor)} — a {specialty} chart walk is being taken "
        f"forward by several physicians, one decision point each. "
        f"You have point {position} of {n_points}.",
        "",
    ]
    if is_first:
        lines += [
            "You're first, so it's live in your queue now: finish any case you're "
            "mid-way through and it comes up right after, or hit Start new case.",
            "",
            "You'll see the chart up to your point and commit an assessment, a plan "
            "and what you expect to happen next. The physician after you reads your "
            "commitment as their handoff.",
        ]
    else:
        lines += [
            "It isn't your turn yet — each point unlocks when the one before it is "
            "submitted, and you'll get a message the moment yours does. Nothing to "
            "do until then; it will not appear in your queue before that.",
            "",
            "When it does, you'll see the chart up to your point plus the previous "
            "physician's committed assessment as your handoff.",
        ]
    lines += ["", "— Archangel"]
    return "\n".join(lines)


def compose_relay_unlock(*, doctor, position, n_points, specialty):
    """The turn DM, fired when the predecessor submits."""
    return "\n".join([
        f"You're up, Dr. {_last_name(doctor)}",
        "",
        f"Point {position} of {n_points} on the {specialty} relay case is now "
        f"yours. The physician before you just committed theirs — you'll see "
        f"their assessment as your handoff.",
        "",
        "It's live in your queue now: finish any case you're mid-way through and "
        "it comes up right after, or hit Start new case.",
        "",
        "— Archangel",
    ])


def notify_relay_send(store, *, mapping, trajectory_id):
    """One DM per doctor at send, plus the room for the walk. Never raises (same
    rule as ``notify_routed``)."""
    report = {"dms": 0, "rooms": 0, "errors": []}
    try:
        from community.store import get_community_store
        cstore = get_community_store()
    except Exception as exc:                       # pragma: no cover
        report["errors"].append(f"community_unavailable:{exc}")
        return report

    rows = list(mapping or [])
    n = len(rows)
    first_by_user = {}
    for row in rows:
        first_by_user.setdefault(row["user_id"], row)
    specialty = "clinical"
    for user_id, row in first_by_user.items():
        try:
            doctor = store.get_user_by_id(user_id) or {"id": user_id}
            task = store.get_task(row["task_id"]) or {}
            specialty = str(task.get("specialty") or specialty)
            idx = row.get("sequence_index") or 0
            body = compose_relay_assignment(
                doctor=doctor, position=int(idx) + 1, n_points=n,
                specialty=str(task.get("specialty") or "clinical"),
                is_first=(int(idx) == 0))
            if _run_coro(_dm_one(cstore, doctor_id=user_id, body=body)):
                report["dms"] += 1
        except Exception as exc:
            log.info("route_notify: relay DM to %s failed: %s", user_id, exc)
            report["errors"].append(f"dm:{user_id}:{exc}")

    # One room for the WALK, not one per point (PRD D2): a relay is several
    # physicians on a single chart, and a room per decision point would put the
    # handoff conversation in a different place from the people handing off.
    try:
        ref = "traj:" + str(trajectory_id)
        people = _room_people(store, [{"user_id": uid, "role": "label"}
                                      for uid in first_by_user])
        if len(people) >= 2:
            ensure_case_room(cstore, case_ref=ref, people=people,
                             specialty=specialty,
                             class_label=CLASS_LABELS["longitudinal"])
            report["rooms"] = 1
    except Exception as exc:
        log.info("route_notify: relay room for %s failed: %s", trajectory_id, exc)
        report["errors"].append(f"room:{trajectory_id}:{exc}")
    return report


def notify_relay_unlock(store, *, task) -> bool:
    """Tell the next physician their point just opened. Never raises.

    Fired on the PREDECESSOR's submit, which is the moment the relay gate starts
    letting the next point through — so the message and the availability are the
    same event rather than a sweep noticing later.
    """
    try:
        from asclepius import trajectory as tj
        if not tj.is_relay(task):
            return False
        idx = tj.sequence_index(task)
        if idx is None:
            return False
        points = store.trajectory_points(task.get("trajectory_id"))
        nxt = next((p for p in points
                    if p.get("sequence_index") is not None
                    and int(p["sequence_index"]) > int(idx)), None)
        if not nxt:
            return False                        # the last point: nobody is next
        holders = [a for a in store.assignments_for_task(nxt["task_id"])
                   if a.get("role") == "label"
                   and a.get("status") in ("offered", "claimed")]
        if not holders:
            return False
        from community.store import get_community_store
        cstore = get_community_store()
        doctor = store.get_user_by_id(holders[0]["user_id"]) or {}
        body = compose_relay_unlock(
            doctor=doctor, position=int(nxt["sequence_index"]) + 1,
            n_points=len(points),
            specialty=str(nxt.get("specialty") or "clinical"))
        return bool(_run_coro(_dm_one(cstore, doctor_id=holders[0]["user_id"],
                                      body=body)))
    except Exception as exc:
        log.info("route_notify: relay unlock ping failed: %s", exc)
        return False


# ═══ §8.7 — the stall sweep ══════════════════════════════════════════════════
#: Whether the 24-hour nudge actually SENDS. Ships OFF.
#:
#: This is the only thing in the product that messages a physician on a timer
#: with nobody deciding to. Contributors are volunteers with clinics to run, and
#: the first automated chase somebody receives should not also be the first time
#: anyone saw how many the sweep would send. So it computes the list, records it,
#: and logs what it WOULD deliver until an operator has watched it for a while;
#: turning it on is a config change, not a deploy.
#:
#: The sweep still runs when this is off, and still marks nothing as nudged —
#: so flipping it on does not fire a backlog of chases at everybody who was
#: stalled during the observation window.
def nudges_enabled() -> bool:
    import os
    return (os.getenv("ASCLEPIUS_RELAY_NUDGE_ENABLED", "0").strip().lower()
            in ("1", "true", "yes", "on"))


def stall_nudge_hours() -> int:
    import os
    try:
        return max(1, int(os.getenv("ASCLEPIUS_RELAY_NUDGE_HOURS", "24")))
    except (TypeError, ValueError):
        return 24


def compose_stall_nudge(*, doctor, position, n_points, specialty, waiting_hours, mode):
    """One nudge, and it reads as a colleague checking in rather than a system
    chasing a ticket — because that is what it is, and because a volunteer who
    feels chased stops being a volunteer.

    It says what is waiting and offers the way out. It does NOT say "urgent", does
    not count down, and never fires twice: recurring nudges to unpaid specialists
    are how a channel gets muted, and a muted physician is unreachable for the
    thing that actually matters next time.
    """
    waited = (f"about {int(round(waiting_hours))} hours"
              if isinstance(waiting_hours, (int, float)) else "a little while")
    lines = [f"Still with you, Dr. {_last_name(doctor)}",
             ""]
    if mode == "relay":
        lines += [
            f"The {specialty} relay is waiting on point {position} of {n_points}, "
            f"which has been yours for {waited}. The physicians after you can't "
            f"start until it's in.",
        ]
    else:
        lines += [
            f"Decision point {position} of {n_points} on your {specialty} chart "
            f"walk has been open for {waited}.",
        ]
    lines += [
        "",
        "No rush if you're mid-clinic — this is the only reminder you'll get. If "
        "you'd rather hand it back, reply here and we'll pass it on.",
        "",
        "— Archangel",
    ]
    return "\n".join(lines)


def sweep_stalled_points(store, *, now_iso=None) -> Dict[str, Any]:
    """Find stalled points and nudge their assignees. Never raises.

    Returns what it found and what it did, distinguishing the two: ``would_notify``
    is the list it built, ``sent`` is what actually went out. While the flag is off
    those numbers differ by design, and the log line says so — a sweep that
    reported "notified 4" while sending nothing would be the exact dishonesty this
    staged rollout exists to avoid.
    """
    report: Dict[str, Any] = {"stalled": 0, "would_notify": [], "sent": 0,
                              "enabled": nudges_enabled(), "errors": []}
    try:
        rows = store.stalled_trajectory_points(
            older_than_hours=stall_nudge_hours(), now_iso=now_iso)
    except Exception as exc:
        log.info("route_notify: stall sweep query failed: %s", exc)
        report["errors"].append(f"query:{exc}")
        return report

    report["stalled"] = len(rows)
    if not rows:
        return report

    cstore = None
    if report["enabled"]:
        try:
            from community.store import get_community_store
            cstore = get_community_store()
        except Exception as exc:               # pragma: no cover
            report["errors"].append(f"community_unavailable:{exc}")
            report["enabled"] = False

    for row in rows:
        try:
            points = store.trajectory_points(row.get("trajectory_id"))
            doctor = store.get_user_by_id(row.get("user_id")) or {}
            body = compose_stall_nudge(
                doctor=doctor, position=int(row.get("sequence_index") or 0) + 1,
                n_points=len(points) or 1,
                specialty=str(row.get("specialty") or "clinical"),
                waiting_hours=row.get("waiting_hours"),
                mode=str(row.get("walk_mode") or "solo"))
            report["would_notify"].append({
                "task_id": row.get("task_id"), "user_id": row.get("user_id"),
                "trajectory_id": row.get("trajectory_id"),
                "sequence_index": row.get("sequence_index"),
                "waiting_hours": row.get("waiting_hours"),
            })
            if not report["enabled"]:
                continue
            if _run_coro(_dm_one(cstore, doctor_id=row["user_id"], body=body)):
                # Marked ONLY on a real send, so the observation window does not
                # silently consume everybody's one nudge.
                store.mark_assignment_nudged(row["assignment_id"], now_iso=now_iso)
                report["sent"] += 1
        except Exception as exc:
            log.info("route_notify: nudge for %s failed: %s", row.get("task_id"), exc)
            report["errors"].append(f"nudge:{row.get('task_id')}:{exc}")

    if report["would_notify"]:
        log.info("relay stall sweep: %d stalled, %d would be nudged, %d sent "
                 "(nudges %s) — %s", report["stalled"], len(report["would_notify"]),
                 report["sent"], "ON" if report["enabled"] else "OFF (log only)",
                 [w["task_id"] for w in report["would_notify"]])
    return report


def notify_reassigned(store, *, task, doctor,
                      replaced_user_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Tell the replacement the point is theirs now, and tell the room. Never raises.

    The solo DM deliberately does NOT say it was taken from somebody. The previous
    holder had a clinic, or a bad week, and the new physician does not need a
    colleague's lapse framed for them before they read a chart.

    The ROOM post is the part that was missing (PRD B5, closing the gap
    ``CASE_BATCHES_AND_ROUTING.md`` records under "What is NOT built"): the
    replacement used to be DMed and nobody else on the walk was told the roster
    had changed, so the physician waiting on a handoff was waiting on somebody
    who no longer had the point. The membership swap and the notice are the same
    event: ``replaced_user_ids`` lose posting rights here, which is the half of
    a reassignment a DM cannot express.
    """
    report: Dict[str, Any] = {"dms": 0, "room": False, "errors": []}
    try:
        from community.store import get_community_store
        cstore = get_community_store()
        points = store.trajectory_points(task.get("trajectory_id"))
        idx = int(task.get("sequence_index") or 0)
        body = compose_relay_unlock(
            doctor=doctor, position=idx + 1, n_points=len(points) or 1,
            specialty=str(task.get("specialty") or "clinical"))
        if _run_coro(_dm_one(cstore, doctor_id=doctor["id"], body=body)):
            report["dms"] = 1
    except Exception as exc:
        log.info("route_notify: reassign DM failed: %s", exc)
        report["errors"].append(str(exc))

    # Separate try: a room that cannot be updated must not cost the replacement
    # the DM that tells them they have work.
    try:
        from community.store import get_community_store
        cstore = get_community_store()
        ref = case_ref_for_task(task)
        room = cstore.get_case_room(ref) if ref else None
        if room:
            cstore.add_room_participant(room["id"], doctor["id"])
            points = store.trajectory_points(task.get("trajectory_id"))
            removed = []
            for old in (replaced_user_ids or []):
                if not old or old == doctor["id"]:
                    continue
                # A doctor who still holds another live point of the same walk
                # stays in the room: only this point changed hands, not the case.
                if any(a.get("user_id") == old
                       and a.get("status") in ("offered", "claimed")
                       for p in points
                       for a in store.assignments_for_task(p["task_id"])):
                    continue
                cstore.remove_room_participant(room["id"], old)
                removed.append(old)
            from community.system_posts import SYSTEM_USER_ID
            cstore.insert_message(
                channel_id=room["id"], author_user_id=SYSTEM_USER_ID,
                body=compose_roster_change(
                    doctor=doctor,
                    position=int(task.get("sequence_index") or 0) + 1,
                    n_points=len(points) or 1),
                kind=ROOM_KIND)
            _audit_room("community.case_room_roster_changed", case_ref=ref,
                        dm_id=room["id"],
                        detail={"added": doctor["id"], "removed": removed})
            report["room"] = True
    except Exception as exc:
        log.info("route_notify: reassign room notice failed: %s", exc)
        report["errors"].append(f"room:{exc}")
    return report
