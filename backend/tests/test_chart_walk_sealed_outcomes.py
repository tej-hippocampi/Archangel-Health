"""P5: outcome evidence belongs to its point, independently of neighbouring tasks."""
import asyncio
import copy
import json

import pytest
from asclepius import real_cases as RC, trajectory, packaging, profiles, export as EX
from asclepius.sealed_outcomes import validate_sealed_outcome
from asclepius.timeline import datelike_leftovers_in_text
from tests import _asclepius as A
from tests.test_longitudinal_front_door import _isolated, store
from tests.test_patient4_reference_walk import _generation_context
from tests.test_asclepius_longitudinal_e2e import _stub_model_legs
from tests.test_chart_walk_export import _package, _read, _assert_bundle


def _window():
    chart = {
        'notes': [{'note_type': 'Progress', 'collected_offset_days': day,
                   'text': f'Observation marker {day}: documented examination and follow-up assessment.'}
                  for day in (0, 1, 5, 6)],
        'lab_panels': [{'panel': 'Chemistry', 'collected_offset_days': day,
                        'results': [{'analyte': 'Potassium', 'value': day + 1, 'unit': 'mmol/L',
                                     'ref_low': 3.5, 'ref_high': 5.0}]} for day in (0, 1, 5, 6)],
        'medications': [{'drug': f'medication-{day}', 'collected_offset_days': day} for day in (0, 1, 5, 6)],
        'problem_list': [{'condition': f'problem-{day}', 'collected_offset_days': day} for day in (0, 1, 5, 6)],
        'studies': [{'modality': 'ecg', 'label': f'study-{day}', 'collected_offset_days': day} for day in (0, 1, 5, 6)],
        'vitals': {'collected_offset_days': 5, 'hr': 90},
        'study_findings_policy': 'hidden',
    }
    return chart, [{'index': 0, 'start_offset': 0, 'end_offset': 6}]


def test_every_collection_is_bounded_and_rebased_without_mutating_chart():
    chart, encounters = _window()
    before = copy.deepcopy(chart)
    sealed = RC.seal_outcome_window(chart, encounters, index_offset=0, until_offset=5, outcome_encounter_index=1)
    delta = validate_sealed_outcome(sealed, index_offset=0)
    for key in ('lab_panels', 'notes', 'medications', 'problem_list', 'studies'):
        assert [item['collected_offset_days'] for item in delta[key]] == [1, 5]
    assert delta['vitals']['collected_offset_days'] == 5
    assert delta['days_after_decision'] == 5 and delta['n_events'] == 10
    assert delta['study_findings_policy'] == 'hidden'
    assert chart == before


def test_seal_preserves_the_full_window_beyond_the_next_case_note_budget():
    chart, encounters = _window()
    chart['notes'] = [{'note_type': 'Progress', 'collected_offset_days': day,
                       'text': f'Observation {day}: symptoms and examination documented.'}
                      for day in range(31)]
    sealed = RC.seal_outcome_window(chart, encounters, index_offset=0, until_offset=30, outcome_encounter_index=1)
    assert [note['collected_offset_days'] for note in sealed['outcome']['notes']] == list(range(1, 31))


def test_relay_continue_respects_assignments_shared_progress_and_retirement(store):
    from tests.test_asclepius_relay import _walk, _doc, _relay, _submit as relay_submit
    points = _walk(store, n=3)
    doctors = [_doc(store), _doc(store)]
    rotation = _relay(store, points, doctors)
    first_owner, second_owner = rotation[:2]
    relay_submit(store, points[0]['task_id'], first_owner)
    def progress(uid):
        return store.evaluator_trajectory_progress(trajectory_id='traj-relay', evaluator_id=uid)
    waiting = progress(first_owner)
    assert waiting['next_task_id'] is None and not waiting['complete']
    assert progress(second_owner)['next_task_id'] == points[1]['task_id']
    store.mark_task_status(points[1]['task_id'], trajectory.RETIRED_STATUSES[0])
    assert progress(first_owner)['next_task_id'] == points[2]['task_id']
    relay_submit(store, points[2]['task_id'], first_owner)
    assert progress(first_owner)['complete']
    assert progress(second_owner)['next_task_id'] is None
    assert store.get_task(points[1]['task_id']) is not None


@pytest.mark.parametrize('corrupt', ['missing_terminal_bound', 'missing_target', 'bool_version', 'wrong_index',
                                    'late_event', 'early_event', 'date', 'extra_field', 'bad_target'])
def test_invalid_seals_never_advertise_verifiability(corrupt):
    chart, encounters = _window()
    sealed = RC.seal_outcome_window(chart, encounters, index_offset=0, until_offset=5, outcome_encounter_index=1)
    if corrupt == 'missing_terminal_bound':
        sealed = {'version': 1, 'index_event_offset': 0, 'outcome': None}
    elif corrupt == 'missing_target':
        sealed.pop('outcome_encounter_index')
    elif corrupt == 'bool_version':
        sealed['version'] = True
    elif corrupt == 'wrong_index':
        sealed['index_event_offset'] = 1
    elif corrupt in ('late_event', 'early_event'):
        sealed['outcome']['notes'][0]['collected_offset_days'] = 6 if corrupt == 'late_event' else 0
    elif corrupt == 'date':
        sealed['outcome']['notes'][0]['text'] = 'Discharged on 2025-01-23.'
    elif corrupt == 'extra_field':
        sealed['outcome']['future_answer'] = 'unbounded clinical evidence'
    else:
        sealed['outcome_encounter_index'] = None
    with pytest.raises(ValueError):
        validate_sealed_outcome(sealed, index_offset=0)
    assert not trajectory.outcome_verifiable({'generation': {'index_event_offset': 0, 'sealed_outcome': sealed}},
                                             has_later_point=True)


def _generated(store, monkeypatch, *, failed=None, only=None):
    import routers.asclepius as router
    _stub_model_legs(monkeypatch)
    client, ic, _, admin_h = _generation_context(store)
    original = router._generate_one_real_case
    if failed is not None:
        async def drop(*args, **kwargs):
            if args[2]['encounter_index'] == failed:
                return {'encounter_index': failed, 'error': 'test judge failure'}
            return await original(*args, **kwargs)
        monkeypatch.setattr(router, '_generate_one_real_case', drop)
    body = {'trajectory': True, 'derive_questions': False}
    if only is not None:
        body['encounter_indices'] = only
    url = f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate"
    preview = client.post(url, headers=admin_h, json=body)
    assert preview.status_code == 200, preview.text
    assert 'sealed_outcome' not in preview.text
    response = client.post(url, headers=admin_h, json={**body, 'dry_run': False})
    assert response.status_code == 200, response.text
    result = response.json()
    tasks = [store.get_task(p['task_id']) for p in store.trajectory_points(result['trajectory_id'])]
    doctor = A.make_user(store, role='evaluator', specialty='cardiology', board_cert='board_certified_cardiology')
    store.set_real_data_approved(doctor['id'], True)
    for task in tasks:
        store.upsert_assignment(task_id=task['task_id'], user_id=doctor['id'], role='label', assigned_by='admin')
    return client, tasks, result, preview.json(), doctor, admin_h, ic


def _submit(client, task, doctor):
    response = client.post('/api/asclepius/submissions', headers=A.headers_for(doctor), json={
        'task_id': task['task_id'], 'verdict': 'A_better', 'chosen_id': 'a', 'rejected_id': 'b',
        'confidence': 'high', 'time_spent_sec': 900,
        'expected_trajectory': {'expectations': [{'expectation': 'Symptoms improve.'}], 'falsifiers': []}})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('failed,expected_count', [(1, 5), (6, 6)])
def test_failed_successor_cannot_change_seal_or_verifiability(store, monkeypatch, failed, expected_count):
    client, tasks, result, _, doctor, _, ic = _generated(store, monkeypatch, failed=failed)
    plan = asyncio.run(RC.plan_cases(ic['case'], trajectory=True, specialty_hint='cardiology', derive_questions=False))
    assert result['generated'] == 6 and result['failed'] == 1
    assert result['walk_verifiable_points'] == result['trajectory_verifiable_points'] == expected_count
    assert all(t['generation']['sealed_outcome'] == plan['proposals'][t['generation']['encounter_index']]['sealed_outcome']
               for t in tasks)
    first = tasks[0]
    url = f"/api/asclepius/tasks/{first['task_id']}/trajectory-outcome"
    headers = A.headers_for(doctor)
    assert client.get(url, headers=headers).status_code == 409
    _submit(client, first, doctor)
    revealed = client.get(url, headers=headers)
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()['outcome'] == first['generation']['sealed_outcome']['outcome']
    assert revealed.json()['outcome']['days_after_decision'] == 34
    if failed == 1:
        assert revealed.json()['outcome_task_id'] is None
        assert revealed.json()['progress']['next_task_id'] == tasks[1]['task_id']
    session = client.get(f"/api/asclepius/trajectories/{result['trajectory_id']}", headers=headers).json()
    assert sum(p['outcome_verifiable'] for p in session['points']) == expected_count


def test_retirement_and_later_edits_do_not_change_reveal_or_leak_the_seal(store, monkeypatch):
    client, tasks, result, _, doctor, admin_h, _ = _generated(store, monkeypatch)
    headers = A.headers_for(doctor)
    first, second = tasks[:2]
    url = f"/api/asclepius/tasks/{first['task_id']}"
    assert 'sealed_outcome' not in client.get(url, headers=headers).text
    assert 'sealed_outcome' not in client.get('/api/asclepius/tasks', headers=admin_h).text
    assert 'sealed_outcome' not in client.get('/api/asclepius/tasks/available?portal_version=v5', headers=headers).text
    assert 'sealed_outcome' not in client.get(f"/api/asclepius/admin/batches/preview/{first['task_id']}", headers=admin_h).text
    assert 'sealed_outcome' not in client.get(f"/api/asclepius/trajectories/{result['trajectory_id']}", headers=headers).text
    _submit(client, first, doctor)
    before = client.get(url + '/trajectory-outcome', headers=headers).json()['outcome']
    store.mark_task_status(second['task_id'], trajectory.RETIRED_STATUSES[0])
    store.update_task_case(second['task_id'], {**second['case'], 'notes': []})
    after = client.get(url + '/trajectory-outcome', headers=headers).json()
    assert after['outcome'] == before
    assert after['progress']['next_task_id'] == tasks[2]['task_id']
    assert client.get(f"/api/asclepius/tasks/{tasks[2]['task_id']}", headers=headers).status_code == 200
    assert store.get_task(second['task_id']) is not None
    assert not datelike_leftovers_in_text(json.dumps(before))
    # A profile explicitly requesting generation must never receive the seal.
    prof = copy.deepcopy(profiles.load_profile('default'))
    for rtype in prof['record_types']:
        mapping = prof['field_maps'][rtype]
        if rtype == 'preference':
            mapping = mapping['flat']
        mapping['generation'] = 'generation'
    monkeypatch.setattr(profiles, 'load_profile', lambda name: prof)
    _package(store, first)
    exported = EX.build_export(store, created_by='admin', case_id=first['task_id'])
    _assert_bundle(exported)
    for name in (EX.JSONL_NAME, EX.CASES_NAME):
        assert 'sealed_outcome' not in _read(exported, name)
    assert 'sealed_outcome' not in packaging._generation_provenance(first)


@pytest.mark.parametrize('only,verifiable', [([0], 1), ([6], 0)])
def test_single_point_generation_keeps_original_boundary_and_terminal_state(store, monkeypatch, only, verifiable):
    client, tasks, result, preview, doctor, _, _ = _generated(store, monkeypatch, only=only)
    assert result['generated'] == 1
    assert preview['walk_verifiable_points'] == result['walk_verifiable_points'] == verifiable
    task = tasks[0]
    _submit(client, task, doctor)
    response = client.get(f"/api/asclepius/tasks/{task['task_id']}/trajectory-outcome", headers=A.headers_for(doctor))
    assert response.status_code == 200, response.text
    assert (response.json()['outcome'] is not None) == bool(verifiable)
    scored = client.post(f"/api/asclepius/tasks/{task['task_id']}/trajectory-self-score", headers=A.headers_for(doctor),
                        json={'marks': [{'index': 0, 'state': 'held'}], 'falsifier_fired': False})
    assert scored.status_code == (200 if verifiable else 409), scored.text


def test_corrupt_present_seal_never_falls_back_to_another_task(store, monkeypatch):
    import routers.asclepius as router
    client, tasks, _, _, doctor, _, _ = _generated(store, monkeypatch, only=[0, 1])
    _submit(client, tasks[0], doctor)
    original = store.get_task
    def corrupt(tid):
        task = original(tid)
        if task and tid == tasks[0]['task_id']:
            return {**task, 'generation': {**task['generation'], 'sealed_outcome': {}}}
        return task
    monkeypatch.setattr(store, 'get_task', corrupt)
    monkeypatch.setattr(router, '_outcome_point', lambda *args: pytest.fail('corrupt seal reached legacy fallback'))
    response = client.get(f"/api/asclepius/tasks/{tasks[0]['task_id']}/trajectory-outcome", headers=A.headers_for(doctor))
    assert response.status_code == 409
    assert response.json()['detail']['error'] == 'outcome_not_reconstructible'
