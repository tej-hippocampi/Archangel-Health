"""Durable receipts and leased restart recovery for submission/upload work."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
import json
import logging
import time
import uuid

import realm

log = logging.getLogger(__name__)
LEASE_SECONDS = 90
TRANSIENT = ("submitted", "auto_validated", "qa_checked")


class LeaseLost(RuntimeError):
    pass


def get(store, kind, ref_id):
    with store._conn() as conn:
        row = conn.execute("SELECT * FROM background_work WHERE kind=? AND ref_id=?", (kind, ref_id)).fetchone()
    return dict(row) if row else None


def enqueue_submission(store, submission_id):
    now = time.time()
    with store._conn() as conn:
        conn.execute("INSERT OR IGNORE INTO background_work "
                     "(kind, ref_id, actor, created_at, updated_at) "
                     "SELECT 'submission', submission_id, evaluator_id, ?, ? FROM submissions "
                     "WHERE submission_id=? AND status IN ('submitted','auto_validated','qa_checked')",
                     (now, now, submission_id))
    return get(store, "submission", submission_id)


class LeaseStore:
    """Use the realm's store, fencing every short DB operation against a lease.

    Store methods are rebound to this facade so nested calls use the same fence.
    Each connection reserves the writer before checking ownership; no write can
    slip between a successful check and a newer worker's claim. No transaction
    spans an awaited model call. No independent store or realm is instantiated.
    """
    def __init__(self, store, kind, ref_id, token):
        self._store, self.kind, self.ref_id, self.token = store, kind, ref_id, token

    def __getattr__(self, name):
        attr = getattr(self._store, name)
        if getattr(attr, "__self__", None) is self._store:
            return attr.__func__.__get__(self, type(self))
        return attr

    def _conn(self):
        conn = self._store._conn()
        try:
            self._store._immediate(conn)
            active = conn.execute("SELECT 1 FROM background_work WHERE kind=? AND ref_id=? "
                                  "AND status='running' AND lease_token=? AND lease_until>?",
                                  (self.kind, self.ref_id, self.token, time.time())).fetchone()
            if not active:
                raise LeaseLost("Background job ownership changed")
            return conn
        except BaseException:
            conn.close()
            raise

    def _immediate(self, conn):
        if not conn.in_transaction:
            self._store._immediate(conn)

    def checkpoint(self, **values):
        with self._conn() as conn:
            row = conn.execute("SELECT progress_json FROM background_work WHERE kind=? AND ref_id=?",
                               (self.kind, self.ref_id)).fetchone()
            progress = json.loads(row[0])
            progress.update(values)
            conn.execute("UPDATE background_work SET progress_json=?, updated_at=? WHERE kind=? AND ref_id=?",
                         (json.dumps(progress), time.time(), self.kind, self.ref_id))
        return progress


async def run(store, kind, ref_id, job_realm=None):
    token, now = uuid.uuid4().hex, time.time()
    with store._conn() as conn:
        claimed = conn.execute("UPDATE background_work SET status='running', lease_token=?, lease_until=?, "
                               "attempts=attempts+1, updated_at=? WHERE kind=? AND ref_id=? AND "
                               "((status='queued' AND next_attempt<=?) OR (status='running' AND lease_until<=?))",
                               (token, now + LEASE_SECONDS, now, kind, ref_id, now, now)).rowcount
    if not claimed:
        return None
    fenced = LeaseStore(store, kind, ref_id, token)

    async def heartbeat():
        while True:
            await asyncio.sleep(15)
            with fenced._conn() as conn:
                conn.execute("UPDATE background_work SET lease_until=? WHERE kind=? AND ref_id=?",
                             (time.time() + LEASE_SECONDS, kind, ref_id))

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        with realm.scoped(job_realm or realm.current()):
            row = get(fenced, kind, ref_id)
            if kind == "submission":
                from routers.asclepius import _finalize_submission
                sub = fenced.get_submission(ref_id)
                if not sub:
                    raise ValueError("Submission is missing")
                result = await _finalize_submission(fenced, sub["task_id"], ref_id, sub["evaluator_id"])
                if fenced.get_submission(ref_id)["status"] in TRANSIENT:
                    raise RuntimeError("Submission processing did not reach a durable outcome")
            elif kind == "auto_upload":
                from asclepius.auto_generate import _run_upload
                result = await _run_upload(fenced, ref_id, row["actor"], job=row)
            else:
                raise ValueError("Unknown background job kind")
            with fenced._conn() as conn:
                conn.execute("UPDATE background_work SET status='completed', result_json=?, error=NULL, "
                             "lease_until=0, updated_at=? WHERE kind=? AND ref_id=?",
                             (json.dumps(result), time.time(), kind, ref_id))
            return result
    except BaseException as exc:
        # The source form/upload and the exact job survive cancellation or death.
        # A fenced-out worker cannot overwrite the new owner's state.
        with store._conn() as conn:
            conn.execute("UPDATE background_work SET status='queued', lease_until=0, next_attempt=?, "
                         "error=?, updated_at=? WHERE kind=? AND ref_id=? AND lease_token=? AND status='running'",
                         (time.time() + (0 if isinstance(exc, asyncio.CancelledError) else 30),
                          type(exc).__name__, time.time(), kind, ref_id, token))
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        log.exception("Background %s job %s will retry", kind, ref_id)
        return None
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat_task


def recover_legacy(store):
    """Adopt old interrupted work after a grace period; never reset old evidence."""
    cutoff = (datetime.utcnow() - timedelta(minutes=2)).replace(microsecond=0).isoformat()
    now = time.time()
    with store._conn() as conn:
        conn.execute("INSERT OR IGNORE INTO background_work (kind, ref_id, actor, created_at, updated_at) "
                     "SELECT 'submission', submission_id, evaluator_id, ?, ? FROM submissions "
                     "WHERE status IN ('submitted','auto_validated','qa_checked') AND updated_at<?",
                     (now, now, cutoff))
        conn.execute("INSERT OR IGNORE INTO background_work "
                     "(kind, ref_id, actor, request_json, created_at, updated_at) "
                     "SELECT 'auto_upload', upload_id, 'automatic-recovery', "
                     "json_object('mode', task_mode, 'legacy_recovery', 1), ?, ? FROM ingest_uploads "
                     "WHERE auto_generate_started_at IS NOT NULL AND auto_generate_report_json IS NULL "
                     "AND updated_at<?", (now, now, cutoff))


async def recover_once(store, job_realm=None, *, kind=None):
    recover_legacy(store)
    with store._conn() as conn:
        rows = conn.execute("SELECT kind, ref_id FROM background_work WHERE "
                            "((status='queued' AND next_attempt<=?) OR (status='running' AND lease_until<=?)) "
                            "AND (? IS NULL OR kind=?) ORDER BY created_at LIMIT 20",
                            (time.time(), time.time(), kind, kind)).fetchall()
    for row in rows:
        await run(store, row["kind"], row["ref_id"], job_realm)


async def worker_loop(kind):
    from asclepius.store import get_store
    while True:
        for job_realm in realm.active_realms():
            try:
                with realm.scoped(job_realm):
                    await recover_once(get_store(), job_realm, kind=kind)
            except Exception:
                log.exception("Background recovery needs attention in %s", job_realm)
        await asyncio.sleep(15)
