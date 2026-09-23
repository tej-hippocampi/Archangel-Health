"""Durable, resumable real-chart jobs; no clinical data in polling responses."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import sqlite3
import time
import uuid

import realm

log = logging.getLogger(__name__)
LEASE_SECONDS = 90


def get(store, job_id):
    with store._conn() as conn:
        row = conn.execute('SELECT * FROM real_case_generation_jobs WHERE job_id = ?',
                           (job_id,)).fetchone()
    return dict(row) if row else None


def view(row):
    return {
        'job_id': row['job_id'], 'status': row['status'],
        'ingest_case_id': row['ingest_case_id'], 'trajectory_id': row['trajectory_id'],
        'progress': json.loads(row['progress_json']),
        'result': json.loads(row['result_json']) if row['result_json'] else None,
        'error': row['error'],
    }


def request_key(ic, params):
    return hashlib.sha256(json.dumps([ic['ingest_case_id'], ic.get('case'), params],
                                   sort_keys=True).encode()).hexdigest()


def enqueue(store, ic, body, actor, *, auto_upload_id=None):
    params = body.model_dump()
    params['background'] = False
    # Stable across double clicks, refreshes, and retries. Changed inputs require
    # a new job; they must never overwrite the evidence of a previous walk.
    key = request_key(ic, params)
    now = time.time()
    with store._conn() as conn:
        store._immediate(conn)
        # Bind an automatic upload/case to its FIRST job in the same commit as
        # creation. A restart must not derive a different request from changed
        # specialties or create a second walk after insert-before-checkpoint.
        linked = conn.execute('SELECT j.* FROM auto_generation_cases a JOIN real_case_generation_jobs j '
                              'ON j.job_id=a.job_id WHERE a.upload_id=? AND a.ingest_case_id=?',
                              (auto_upload_id, ic['ingest_case_id'])).fetchone() if auto_upload_id else None
        if linked:
            return dict(linked)
        row = conn.execute('SELECT * FROM real_case_generation_jobs WHERE request_key = ?',
                           (key,)).fetchone()
        if row:
            if row['status'] == 'failed':
                conn.execute("UPDATE real_case_generation_jobs SET status = 'queued', error = NULL, "
                             "updated_at = ? WHERE job_id = ?", (str(now), row['job_id']))
            job_id = row['job_id']
        else:
            job_id = 'rcjob-' + uuid.uuid4().hex
            trajectory_id = 'traj-' + uuid.uuid4().hex if body.trajectory else None
            conn.execute('INSERT INTO real_case_generation_jobs '
                         '(job_id, request_key, ingest_case_id, request_json, created_by, '
                         'trajectory_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (job_id, key, ic['ingest_case_id'], json.dumps(params), actor,
                          trajectory_id, str(now), str(now)))
        if auto_upload_id:
            conn.execute('INSERT INTO auto_generation_cases (upload_id, ingest_case_id, job_id) VALUES (?, ?, ?)',
                         (auto_upload_id, ic['ingest_case_id'], job_id))
    return get(store, job_id)


class Run:
    def __init__(self, store, row, owner):
        self.store, self.row, self.owner = store, row, owner
        self.progress = json.loads(row['progress_json'])

    def task_id(self, encounter_index):
        return 't-' + uuid.uuid5(uuid.NAMESPACE_URL,
                               self.row['job_id'] + ':' + str(encounter_index)).hex

    def checkpoint(self, **values):
        self.progress.update(values)
        with self.store._conn() as conn:
            changed = conn.execute(
                "UPDATE real_case_generation_jobs SET progress_json = ?, lease_until = ?, updated_at = ? "
                "WHERE job_id = ? AND lease_owner = ? AND status = 'running'",
                (json.dumps(self.progress), time.time() + LEASE_SECONDS, str(time.time()),
                 self.row['job_id'], self.owner)).rowcount
        if not changed:
            raise RuntimeError('Generation ownership changed. Reopen the chart to check progress.')


async def run(store, job_id, job_realm):
    owner = uuid.uuid4().hex
    with store._conn() as conn:
        claimed = conn.execute(
            "UPDATE real_case_generation_jobs SET status = 'running', lease_owner = ?, lease_until = ? "
            "WHERE job_id = ? AND (status = 'queued' OR (status = 'running' AND lease_until < ?))",
            (owner, time.time() + LEASE_SECONDS, job_id, time.time())).rowcount
    if not claimed:
        return
    current = Run(store, get(store, job_id), owner)

    async def heartbeat():
        while True:
            await asyncio.sleep(15)
            current.checkpoint()

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        from fastapi import BackgroundTasks
        from asclepius.schemas import GenerateRealCasesRequest
        from routers.asclepius import _execute_real_case_generation
        with realm.scoped(job_realm):
            params = json.loads(current.row['request_json'])
            ic = store.get_ingest_case(current.row['ingest_case_id'])
            if not ic or request_key(ic, params) != current.row['request_key']:
                raise RuntimeError('Source chart changed. Preview the current chart before starting a new walk.')
            bg = BackgroundTasks()
            result = await _execute_real_case_generation(
                current.row['ingest_case_id'],
                GenerateRealCasesRequest(**json.loads(current.row['request_json'])),
                bg, {'id': current.row['created_by']}, job=current)
            # Store only generation outcomes, never the public chart/proposals.
            result.pop('proposals', None)
            with store._conn() as conn:
                conn.execute("UPDATE real_case_generation_jobs SET status = 'completed', result_json = ?, "
                             "error = NULL, lease_until = 0, updated_at = ? WHERE job_id = ? AND lease_owner = ?",
                             (json.dumps(result), str(time.time()), job_id, owner))
            await bg()
    except (asyncio.CancelledError, sqlite3.Error, OSError) as exc:
        # Process shutdown: durable tasks survive. Recovery reconciles task IDs
        # before calling a model, including insert-before-checkpoint crashes.
        with store._conn() as conn:
            conn.execute("UPDATE real_case_generation_jobs SET status = 'queued', lease_until = 0 "
                         "WHERE job_id = ? AND lease_owner = ? AND status = 'running'", (job_id, owner))
        if isinstance(exc, asyncio.CancelledError):
            raise
        log.warning('Real-chart job %s will retry after storage failure: %s', job_id, type(exc).__name__)
    except Exception as exc:
        detail = getattr(exc, 'detail', None) or str(exc)
        log.warning('Real-chart job %s paused: %s', job_id, detail)
        with store._conn() as conn:
            conn.execute("UPDATE real_case_generation_jobs SET status = 'failed', error = ?, lease_until = 0, "
                         "result_json = ?, updated_at = ? WHERE job_id = ? AND lease_owner = ? AND status = 'running'",
                         (str(detail)[:2000], json.dumps({"failure": detail}) if isinstance(detail, dict) else None,
                          str(time.time()), job_id, owner))
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


async def worker_loop():
    """Resume acknowledged jobs after restart, even if the browser is closed."""
    from asclepius.store import get_store
    while True:
        for job_realm in realm.active_realms():
            try:
                with realm.scoped(job_realm):
                    store = get_store()
                    with store._conn() as conn:
                        rows = conn.execute("SELECT job_id FROM real_case_generation_jobs WHERE status = 'queued' "
                                            "OR (status = 'running' AND lease_until < ?) ORDER BY created_at LIMIT 10",
                                            (time.time(),)).fetchall()
                    for row in rows:
                        await run(store, row['job_id'], job_realm)
            except Exception:
                log.exception('Real-chart job recovery failed in %s', job_realm)
        await asyncio.sleep(15)
