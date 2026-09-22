"""Timeout-free generation, checkpoint recovery, and complete physician routing."""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import BackgroundTasks

from asclepius import real_case_jobs as jobs
from asclepius.schemas import GenerateRealCasesRequest
from routers import asclepius as routes
from tests import _asclepius as A
from tests.test_longitudinal_front_door import _isolated, store
from tests.test_patient4_reference_walk import _generation_context
from tests.test_asclepius_longitudinal_e2e import _stub_model_legs


def _queue(store, **options):
    client, ic, admin, headers = _generation_context(store)
    body = GenerateRealCasesRequest(dry_run=False, trajectory=True, background=True, **options)
    bg = BackgroundTasks()
    response = asyncio.run(routes.generate_real_cases(ic['ingest_case_id'], body, bg, admin))
    assert response.status_code == 202
    row = json.loads(response.body)
    return client, ic, admin, headers, body, bg, row


def test_acknowledges_before_models_and_generates_seven_linked_points(store, monkeypatch):
    _stub_model_legs(monkeypatch)
    calls = []
    original = routes._generate_one_real_case

    async def tracked(*args, **kwargs):
        calls.append(args[2]['encounter_index'])
        return await original(*args, **kwargs)

    monkeypatch.setattr(routes, '_generate_one_real_case', tracked)
    client, ic, admin, headers, body, bg, queued = _queue(store)
    assert calls == []
    assert queued['status'] == 'queued'
    asyncio.run(bg())
    result = jobs.view(jobs.get(store, queued['job_id']))
    assert result['status'] == 'completed', result
    assert calls == list(range(7))
    assert result['result']['generated'] == 7
    assert result['result']['walk_verifiable_points'] == 6
    assert result['progress']['generated'] == 7
    assert 'proposals' not in result['result']
    points = store.trajectory_points(result['trajectory_id'])
    assert [p['sequence_index'] for p in points] == list(range(7))
    tasks = [store.get_task(p['task_id']) for p in points]
    assert all(t['distribution'] == 'assigned_only' for t in tasks)
    # Browser refresh / repeated bulk click is the same completed walk.
    again_bg = BackgroundTasks()
    again = asyncio.run(routes.generate_real_cases(ic['ingest_case_id'], body, again_bg, admin))
    assert json.loads(again.body)['job_id'] == queued['job_id']
    assert again_bg.tasks == []
    assert len(store.trajectory_points(result['trajectory_id'])) == 7
    status_url = f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generation-jobs/{queued['job_id']}"
    assert client.get(status_url, headers=headers).json()['status'] == 'completed'
    doctor = A.make_user(store, role='evaluator', specialty='cardiology', board_cert='board_certified_cardiology')
    store.set_real_data_approved(doctor['id'], True)
    assert client.get(status_url, headers=A.headers_for(doctor)).status_code == 403
    sent = client.post('/api/asclepius/admin/assignments/allocate', headers=headers,
                       json={'task_ids': [t['task_id'] for t in tasks], 'user_ids': [doctor['id']], 'dry_run': False})
    assert sent.status_code == 200, sent.text
    first = client.get('/api/asclepius/tasks/' + tasks[0]['task_id'], headers=A.headers_for(doctor))
    assert first.status_code == 200, first.text
    assert client.get('/api/asclepius/tasks/' + tasks[1]['task_id'], headers=A.headers_for(doctor)).status_code == 409


def test_failure_after_first_point_resumes_same_walk_without_duplicates(store, monkeypatch):
    _stub_model_legs(monkeypatch)
    original = routes._generate_one_real_case
    calls = []
    fail = True

    async def interrupted(*args, **kwargs):
        index = args[2]['encounter_index']
        calls.append(index)
        if fail and index == 1:
            return {'error': 'Upstream model unavailable', 'task_id': None}
        return await original(*args, **kwargs)

    monkeypatch.setattr(routes, '_generate_one_real_case', interrupted)
    client, ic, admin, headers, body, bg, queued = _queue(store)
    asyncio.run(bg())
    failed = jobs.view(jobs.get(store, queued['job_id']))
    assert failed['status'] == 'failed'
    assert failed['progress']['generated'] == 1
    assert 'Encounter 2' in failed['error']
    saved = store.trajectory_points(failed['trajectory_id'])[0]['task_id']
    doctor = A.make_user(store, role='evaluator', specialty='cardiology')
    sent = client.post('/api/asclepius/admin/assignments/allocate', headers=headers,
                       json={'task_ids': [saved], 'user_ids': [doctor['id']], 'dry_run': False})
    assert sent.status_code == 409
    assert not store.assignments_for_task(saved)
    relay = client.post('/api/asclepius/admin/batches/relay', headers=headers,
                        json={'trajectory_id': failed['trajectory_id'], 'user_ids': [doctor['id']], 'dry_run': False})
    assert relay.status_code == 409
    fail = False
    bg = BackgroundTasks()
    retried = asyncio.run(routes.generate_real_cases(ic['ingest_case_id'], body, bg, admin))
    assert json.loads(retried.body)['job_id'] == queued['job_id']
    asyncio.run(bg())
    complete = jobs.view(jobs.get(store, queued['job_id']))
    assert complete['status'] == 'completed', complete
    assert complete['result']['walk_verifiable_points'] == 6
    assert complete['result']['task_ids'][0] == saved
    assert calls.count(0) == 1
    assert len(set(complete['result']['task_ids'])) == 7


@pytest.mark.parametrize('crash_at', [0, 6])
def test_insert_before_checkpoint_crash_recovers_committed_task(store, monkeypatch, crash_at):
    _stub_model_legs(monkeypatch)
    original = routes._generate_one_real_case
    calls = []

    async def crash(*args, **kwargs):
        calls.append(args[2]['encounter_index'])
        result = await original(*args, **kwargs)
        if args[2]['encounter_index'] == crash_at:
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(routes, '_generate_one_real_case', crash)
    _, _, _, _, _, bg, queued = _queue(store)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bg())
    assert jobs.get(store, queued['job_id'])['status'] == 'queued'
    asyncio.run(jobs.run(store, queued['job_id'], 'live'))
    result = jobs.view(jobs.get(store, queued['job_id']))
    assert result['status'] == 'completed', result
    assert calls == list(range(7))
    assert result['result']['generated'] == 7
    assert result['progress']['generated'] == 7


def test_parallel_enqueue_and_workers_do_not_duplicate_generation(store, monkeypatch):
    _stub_model_legs(monkeypatch)
    _, ic, admin, _, body, _, queued = _queue(store)
    body = body.model_copy(update={'specialty': 'cardiology'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: jobs.enqueue(store, ic, body, admin['id']), range(8)))
    assert {r['job_id'] for r in rows} == {queued['job_id']}

    async def workers():
        await asyncio.gather(*(jobs.run(store, queued['job_id'], 'live') for _ in range(3)))

    asyncio.run(workers())
    result = jobs.view(jobs.get(store, queued['job_id']))
    assert result['status'] == 'completed', result
    assert len(store.trajectory_points(result['trajectory_id'])) == 7


def test_stale_worker_cannot_insert_after_lease_reassignment(store, monkeypatch):
    _stub_model_legs(monkeypatch)
    original = routes._generate_one_real_case

    async def replace_lease(*args, **kwargs):
        job = kwargs['job']
        with store._conn() as conn:
            conn.execute('UPDATE real_case_generation_jobs SET lease_owner = ?, lease_until = ? WHERE job_id = ?',
                         ('replacement', time.time() + 90, job.row['job_id']))
        return await original(*args, **kwargs)

    monkeypatch.setattr(routes, '_generate_one_real_case', replace_lease)
    _, _, _, _, _, bg, queued = _queue(store)
    asyncio.run(bg())
    assert store.trajectory_points(queued['trajectory_id']) == []


@pytest.mark.parametrize('dimension', ['coherence', 'multimodal_necessity', 'reasoning_divergence_potential', 'unavailable'])
def test_interval_quality_failure_preserves_diagnostics_and_saved_prefix(store, monkeypatch, dimension):
    from asclepius import critic
    _stub_model_legs(monkeypatch)
    visits = []
    fail = True

    async def judge(case, case_source='synthetic', **context):
        visits.append(context)
        result = {'skipped': False, 'coherence': .95, 'multimodal_necessity': .95,
                  'reasoning_divergence_potential': .95, 'explanation': 'Insufficient reasoning evidence.'}
        if fail and context.get('point_class') == 'interval':
            if dimension == 'unavailable':
                return {'skipped': True}
            result[dimension] = .1
        return result

    monkeypatch.setattr(critic, 'run_case_judge', judge)
    _, ic, admin, _, body, bg, queued = _queue(store)
    asyncio.run(bg())
    failed = jobs.view(jobs.get(store, queued['job_id']))
    assert failed['status'] == 'failed'
    assert failed['progress']['generated'] == 2
    failure = failed['progress']['failure']
    assert failure['encounter_index'] == 2
    assert failure['code'] == ('case_judge_unavailable' if dimension == 'unavailable' else 'quality_rejected')
    assert 'Insufficient reasoning evidence' not in json.dumps(failed['progress'])
    saved = [p['task_id'] for p in store.trajectory_points(queued['trajectory_id'])]
    with store._conn() as conn:
        audit = json.loads(conn.execute("SELECT payload_json FROM events WHERE event_type='real_case_generation_failed' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert audit['job_id'] == queued['job_id']
    if dimension != 'unavailable':
        assert audit['scores'][dimension] == .1
        assert audit['judges']['explanation'] == 'Insufficient reasoning evidence.'
    assert visits[-1]['question'] and visits[-1]['encounter_window'] == [0, 0]
    fail = False
    bg = BackgroundTasks()
    asyncio.run(routes.generate_real_cases(ic['ingest_case_id'], body, bg, admin))
    asyncio.run(bg())
    complete = jobs.view(jobs.get(store, queued['job_id']))
    assert complete['status'] == 'completed'
    assert complete['result']['task_ids'][:2] == saved
    assert complete['progress']['failure'] is None


def test_background_preserves_purpose_and_dry_run_gates(store):
    client, ic, _, headers = _generation_context(store)
    url = f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate"
    store.set_upload_purpose(ic['upload_id'], 'brokering')
    refused = client.post(url, headers=headers, json={'dry_run': False, 'background': True, 'trajectory': True})
    assert refused.status_code == 409
    with store._conn() as conn:
        assert conn.execute('SELECT count(*) FROM real_case_generation_jobs').fetchone()[0] == 0


def test_restart_refuses_planner_drift_without_mixing_sequences(store, monkeypatch):
    from asclepius import real_cases
    _stub_model_legs(monkeypatch)
    original_plan = real_cases.plan_cases
    original_generate = routes._generate_one_real_case
    old_plan = True

    async def changing_plan(*args, **kwargs):
        plan = await original_plan(*args, **kwargs)
        if old_plan:
            plan['proposals'] = [p for p in plan['proposals'] if p['encounter_index'] in (0, 1, 6)]
        return plan

    async def crash_after_insert(*args, **kwargs):
        result = await original_generate(*args, **kwargs)
        if args[2]['encounter_index'] == 6:
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(real_cases, 'plan_cases', changing_plan)
    monkeypatch.setattr(routes, '_generate_one_real_case', crash_after_insert)
    _, _, _, _, _, bg, queued = _queue(store)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bg())
    before = store.trajectory_points(queued['trajectory_id'])
    assert len(before) == 3
    old_plan = False
    asyncio.run(jobs.run(store, queued['job_id'], 'live'))
    result = jobs.view(jobs.get(store, queued['job_id']))
    assert result['status'] == 'failed'
    assert 'plan changed' in result['error']
    assert store.trajectory_points(queued['trajectory_id']) == before
