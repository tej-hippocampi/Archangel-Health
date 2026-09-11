"""P1–P4 acceptance through the committed fixture door and real HTTP routes."""
import asyncio
import json

import pytest
from asclepius import real_cases as RC
from asclepius.timeline import datelike_leftovers, datelike_leftovers_in_text
from tests import _asclepius as A
from tests.test_longitudinal_front_door import _isolated, store, _run
from tests.test_patient4_reference_walk import _generation_context
from tests.test_asclepius_longitudinal_e2e import _stub_model_legs
from tests.test_chart_walk_export import _package, _assert_bundle, _read
from asclepius import export as EX


@pytest.mark.parametrize('bundle,specialty,counts,static_counts', [
    ('patient-1', 'hepatology', (22, 2, 20, 22, 21, 0), (22, 9)),
    ('patient-3', 'hepatology', (5, 3, 2, 5, 4, 0), (5, 4)),
    ('patient-4', 'cardiology', (7, 3, 4, 7, 6, 0), (7, 3)),
])
def test_fixture_door_measured_counts(store, bundle, specialty, counts, static_counts):
    res = _run(store, bundles=[bundle])
    chart = store.list_ingest_cases(upload_id=res['bundles'][0]['upload_id'])[0]['case']
    plan = asyncio.run(RC.plan_cases(chart, specialty_hint=specialty, trajectory=True, derive_questions=False))
    keys = ('encounters', 'decision_points', 'interval_points', 'walk_points',
            'walk_verifiable_points', 'review_required_points')
    assert tuple(plan[k] for k in keys) == counts
    assert plan['ready_walk_points'] == counts[3]
    points = [p for p in plan['proposals'] if p['qualifies_as_point']]
    assert sum(p['generatable'] for p in points) == counts[3]
    assert all(not datelike_leftovers(p['case']) for p in points)
    static = asyncio.run(RC.plan_cases(chart, specialty_hint=specialty, trajectory=False, derive_questions=False))
    assert (static['encounters'], static['decision_points']) == static_counts
    print(f'{bundle}: encounters={counts[0]}, decision={counts[1]}, interval={counts[2]}, '
          f'generatable={counts[3]}, verifiable={counts[4]}, held={counts[5]}')


@pytest.mark.parametrize('mode', ['solo', 'relay'])
def test_patient4_seven_generated_points_route_reveal_and_export(store, monkeypatch, mode):
    _stub_model_legs(monkeypatch)
    client, ic, _, admin_h = _generation_context(store)
    response = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
        headers=admin_h, json={'trajectory': True, 'dry_run': False})
    assert response.status_code == 200, response.text
    generated = response.json()
    assert generated['generated'] == generated['trajectory_points'] == 7
    assert generated['failed'] == generated['gated'] == 0
    assert generated['walk_verifiable_points'] == 6
    assert generated['estimated_cost_usd'] == 7 * 75
    tid = generated['trajectory_id']
    tasks = [store.get_task(p['task_id']) for p in store.trajectory_points(tid)]
    assert [t['sequence_index'] for t in tasks] == list(range(7))
    assert all(t['trajectory_id'] == tid and t['distribution'] == 'assigned_only'
               and t['max_labels'] == 1 and t['specialty'] == 'cardiology' for t in tasks)
    overview = client.get('/api/asclepius/admin/batches', headers=admin_h).json()['longitudinal']
    assert (overview['n_trajectories'], overview['n_points'], overview['n_unrouted']) == (1, 7, 7)
    docs = [A.make_user(store, role='evaluator', specialty='cardiology',
                        board_cert='board_certified_cardiology') for _ in range(3)]
    for doctor in docs:
        store.set_real_data_approved(doctor['id'], True)
    if mode == 'solo':
        sent = client.post('/api/asclepius/admin/assignments/allocate', headers=admin_h,
            json={'task_ids': [t['task_id'] for t in tasks], 'user_ids': [docs[0]['id']], 'dry_run': False})
        assigned = [0] * 7
    else:
        body = {'trajectory_id': tid, 'user_ids': [d['id'] for d in docs], 'seed': 7, 'dry_run': True}
        dry = client.post('/api/asclepius/admin/batches/relay', headers=admin_h, json=body)
        assert dry.status_code == 200, dry.text
        shown = dry.json()
        assigned = [next(i for i, d in enumerate(docs) if d['id'] == m['user_id']) for m in shown['mapping']]
        assert assigned == [2, 0, 1, 2, 0, 1, 2]
        sent = client.post('/api/asclepius/admin/batches/relay', headers=admin_h,
                          json={**body, 'seed': shown['seed'], 'dry_run': False})
        assert sent.json()['mapping'] == shown['mapping']
    assert sent.status_code == 200, sent.text
    first_h = A.headers_for(docs[assigned[0]])
    second_h = A.headers_for(docs[assigned[1]])
    first_url = f"/api/asclepius/tasks/{tasks[0]['task_id']}"
    second_url = f"/api/asclepius/tasks/{tasks[1]['task_id']}"
    opened = client.get(first_url, headers=first_h)
    assert opened.status_code == 200, opened.text
    assert client.get(second_url, headers=second_h).status_code == 409
    current = opened.json()['task']
    assert any(n['note_type'] == 'Admission' for n in current['case']['notes'])
    prompt = current['prompt'].lower()
    assert 'echo' in prompt and ('troponin' in prompt or 'trop' in prompt)
    assert 'dyspepsia/age' not in prompt and 'medication on discharge' not in prompt
    assert not datelike_leftovers_in_text(prompt)
    submit = client.post('/api/asclepius/submissions', headers=first_h,
        json={'task_id': tasks[0]['task_id'], 'verdict': 'A_better', 'chosen_id': 'a',
              'rejected_id': 'b', 'confidence': 'high', 'time_spent_sec': 900})
    assert submit.status_code == 200, submit.text
    assert client.get(second_url, headers=second_h).status_code == 200
    reveal = client.get(first_url + '/trajectory-outcome', headers=first_h)
    assert reveal.status_code == 200, reveal.text
    delta = reveal.json()['outcome']
    text = json.dumps(delta).lower()
    assert 'nebil' in text and 'spiromide' in text and 'dapa' in text
    assert 'shortness of breath' in text and 'flank pain' in text
    assert 'successful management' not in text and 'ready for discharge amid' not in text
    assert not datelike_leftovers_in_text(text)
    offsets = [item['collected_offset_days'] for key in ('notes', 'lab_panels', 'studies', 'medications', 'problem_list')
               for item in delta.get(key, []) if item.get('collected_offset_days') is not None]
    assert offsets and all(0 < day <= 34 for day in offsets)
    if mode == 'solo':
        # Exercise the real packager on these generated cases; QA readiness is
        # supplied by the test so this acceptance does not depend on model calls.
        for task in reversed(tasks):
            _package(store, task)
        result = EX.build_export(store, created_by='admin', case_ids=[t['task_id'] for t in tasks])
        _assert_bundle(result)
        rows = [json.loads(line) for line in _read(result, EX.JSONL_NAME).splitlines()]
        assert all(r['annotator_specialty'] == 'cardiology' for r in rows)
        assert all(r['trajectory']['point_counts'] == {'decision': 3, 'interval': 4, 'unclassified': 0} for r in rows)
        assert '1 trajectory · 7 points' in _read(result, EX.DATASHEET_NAME)
