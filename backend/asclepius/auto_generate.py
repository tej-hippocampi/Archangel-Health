"""Auto-create tasks when an upload is fully described (Longitudinal E2E PRD §3).

Box 2 needs a click per upload. This removes it — and nothing else.

**The trigger, stated once.** A run starts when all three of these are true, and
never otherwise:

  * ``purpose == 'task_creation'`` — an admin decided this data may become tasks;
  * ``task_mode`` is set — static or longitudinal, so the run knows which product
    to build;
  * ``auto_generate == 1`` — somebody armed this bundle, or its health system's
    default did.

All three are checked **inside one conditional UPDATE**
(``store.claim_auto_generate``), not here in Python. Three separate request paths
can make the condition true — resolving purpose, choosing the mode, arming the
flag — and each of them calls this. A check-then-write in Python leaves a window
where two of them both see "not started yet" and both launch, which on a
25-encounter chart is a second frontier bill nobody approved.

**Auto-created is never auto-served.** Trajectory points land
``distribution='assigned_only'`` because ``generate_real_cases`` already puts them
there; this module removes a click from BUILDING and not one from SENDING.
Nothing reaches a physician until an admin routes it from Task Routing.

**One code path, not two.** The run calls the same
``POST /ingestion/cases/{id}/generate`` handler the button calls — same plan, same
density gate, same per-encounter isolation, same ``max_labels`` forcing, same
notifications. A parallel implementation would be a second place for the gates to
drift, and the gates are the product.

**Failures are isolated and recorded, never raised.** ``generate_real_cases``
already isolates a per-encounter failure so one bad case judge cannot fail the
batch. What was missing was anywhere to READ that afterwards: a run reports
success having dropped three encounters, and the chart is quietly short. The
per-upload report is written to ``ingest_uploads.auto_generate_report_json`` and
surfaced as a count with a `show` link on the row — never as a modal, because 22
points built out of 25 is a result, not an error.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("asclepius.auto_generate")

#: How many ingest cases one unattended run will promote from a single upload.
#: A bundle can legitimately carry many charts, and each one is a full generation
#: pass, so a cap exists — but it is high enough not to bite a real bundle, and a
#: run that hits it says so in the report rather than stopping silently.
MAX_CASES_PER_RUN = 25


def is_armed(upload: Optional[Dict[str, Any]]) -> bool:
    """Whether this upload's row satisfies the §3 trigger, for DISPLAY.

    The authority is ``store.claim_auto_generate``; this is what the UI reads so
    a row can say "will build itself" before anything has happened. Kept beside
    the claim rather than in the router so the two cannot describe different
    conditions.
    """
    u = upload or {}
    return bool(int(u.get("auto_generate") or 0)
                and (u.get("purpose") == "task_creation")
                and (u.get("task_mode") or "").strip())


def has_run(upload: Optional[Dict[str, Any]]) -> bool:
    return bool((upload or {}).get("auto_generate_started_at"))


def maybe_start(
    store: Any, upload_id: str, *, actor: str,
    schedule: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Start the unattended run if the trigger is satisfied and unclaimed.

    ``schedule`` is ``BackgroundTasks.add_task`` from the triggering request, so
    generation never runs in the request path — a 25-encounter chart is minutes of
    frontier calls, and the admin who just clicked "Task creation" is waiting on a
    200. With no scheduler the claim is released and nothing runs, rather than
    blocking the caller.

    Returns ``{"started": bool, "reason": str}``. Never raises: this is called
    from the tail of three ordinary admin requests, and an auto-generation that
    cannot start must not turn "purpose recorded" into a 500.
    """
    try:
        upload = store.get_ingest_upload(upload_id)
        if not upload:
            return {"started": False, "reason": "no such upload"}
        if has_run(upload):
            return {"started": False, "reason": "already run"}
        if not is_armed(upload):
            return {"started": False, "reason": "trigger not satisfied"}
        if not store.claim_auto_generate(upload_id):
            # Lost the race, or the condition changed under us between the read
            # and the claim. Either way somebody else owns this run.
            return {"started": False, "reason": "claimed by another run"}
        if schedule is None:
            store.release_auto_generate_claim(upload_id)
            return {"started": False, "reason": "no scheduler available"}
        schedule(run_upload, store, upload_id, actor)
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="auto_generate_scheduled", actor=actor,
                        payload={"task_mode": upload.get("task_mode")})
        return {"started": True, "reason": "scheduled"}
    except Exception as exc:  # pragma: no cover — a convenience must not 500 a request
        log.exception("auto-generate could not be scheduled for %s", upload_id)
        return {"started": False, "reason": f"error: {exc}"}


async def run_upload(store: Any, upload_id: str, actor: str) -> Dict[str, Any]:
    """Generate every eligible case in one upload, unattended.

    Runs OUTSIDE the request path (see ``maybe_start``). Every exception is caught
    and recorded: this has no caller to report to, so a traceback that escaped
    here would land in the logs and nowhere an operator looks.
    """
    from fastapi import BackgroundTasks

    from asclepius.schemas import GenerateRealCasesRequest

    report: Dict[str, Any] = {
        "upload_id": upload_id, "cases": [], "generated": 0, "gated": 0,
        "failed": 0, "trajectories": [], "errors": [],
    }
    try:
        upload = store.get_ingest_upload(upload_id)
        mode = (upload or {}).get("task_mode") or "static"
        trajectory = (mode == "longitudinal")
        rows = [c for c in store.list_ingest_cases(upload_id=upload_id)
                if c.get("status") == "ingested"]
        report["mode"] = mode
        report["eligible_cases"] = len(rows)
        if len(rows) > MAX_CASES_PER_RUN:
            # Said out loud rather than silently truncated: an operator who sent 40
            # charts and got 25 needs to know the other 15 are waiting for a click,
            # not that they failed.
            report["errors"].append(
                f"{len(rows)} eligible case(s); this run promoted the first "
                f"{MAX_CASES_PER_RUN}. Promote the rest from Task creation.")
            rows = rows[:MAX_CASES_PER_RUN]

        # The SAME handler the button calls (see the module docstring). Imported
        # here rather than at module scope because ``routers.asclepius`` imports
        # half the package and this module is imported from inside it.
        from routers.asclepius import generate_real_cases

        for case in rows:
            entry: Dict[str, Any] = {"ingest_case_id": case["ingest_case_id"]}
            bg = BackgroundTasks()
            try:
                res = await generate_real_cases(
                    case["ingest_case_id"],
                    GenerateRealCasesRequest(dry_run=False, trajectory=trajectory),
                    bg, {"id": actor},
                )
            except Exception as exc:
                # Per-CASE isolation, mirroring the per-ENCOUNTER isolation inside
                # the handler: one chart that cannot be planned must not stop the
                # other nine in the same bundle.
                log.warning("auto-generate failed for %s: %s", case["ingest_case_id"], exc)
                entry.update({"error": _readable(exc)})
                report["failed"] += 1
                report["cases"].append(entry)
                continue
            # The handler queued its new-task notifications on the BackgroundTasks
            # it was handed. Nothing else will ever run them here, so they are run
            # now — otherwise an auto-generated batch is the one batch nobody is
            # told about.
            try:
                await bg()
            except Exception:  # pragma: no cover
                log.exception("auto-generate: task notifications failed for %s",
                              case["ingest_case_id"])
            entry.update({
                "generated": res.get("generated", 0),
                "gated": res.get("gated", 0),
                "failed": res.get("failed", 0),
                "trajectory_id": res.get("trajectory_id"),
                "points": res.get("trajectory_points"),
            })
            # The per-encounter failures the handler isolated, carried up with
            # enough detail to act on: an encounter index and a reason.
            details = (res.get("details") or {})
            drops = [{"encounter_index": d.get("encounter_index"),
                      "reason": d.get("error") or d.get("failures")}
                     for d in (details.get("failed") or []) + (details.get("gated") or [])]
            if drops:
                entry["dropped"] = drops
            report["generated"] += entry["generated"]
            report["gated"] += entry["gated"]
            report["failed"] += entry["failed"]
            if entry.get("trajectory_id"):
                report["trajectories"].append(entry["trajectory_id"])
            report["cases"].append(entry)
    except Exception as exc:  # pragma: no cover
        log.exception("auto-generate run failed for %s", upload_id)
        report["errors"].append(_readable(exc))

    try:
        store.set_upload_auto_generate_report(upload_id, report)
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="auto_generate_finished", actor=actor,
                        payload={k: report[k] for k in
                                 ("generated", "gated", "failed", "trajectories")})
    except Exception:  # pragma: no cover
        log.exception("auto-generate: could not record the report for %s", upload_id)
    return report


def _readable(exc: Exception) -> str:
    """An HTTPException's ``detail`` says what actually went wrong; ``str(exc)``
    on one says almost nothing. Everything else stringifies as usual."""
    detail = getattr(exc, "detail", None)
    if detail is None:
        return f"{type(exc).__name__}: {exc}"
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or detail)
    return str(detail)


def failure_summary(upload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A COUNT plus the detail behind it, for the row (§3).

    ``None`` when a run produced no drops, so a clean bundle grows no chip. A
    count with no way to see what it counts is the same defect as no count at
    all, so the reasons travel with it.
    """
    report = (upload or {}).get("auto_generate_report")
    if not report:
        return None
    dropped: List[Dict[str, Any]] = []
    for case in report.get("cases") or []:
        for d in case.get("dropped") or []:
            dropped.append({"ingest_case_id": case.get("ingest_case_id"), **d})
    errors = list(report.get("errors") or [])
    if not dropped and not errors:
        return None
    return {"count": len(dropped) + len(errors), "dropped": dropped, "errors": errors}
