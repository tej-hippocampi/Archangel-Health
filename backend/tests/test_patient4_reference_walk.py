"""Temporal safeguards, tested on the real reference chart and adversarial cases.

The chart walk includes three decision points and four interval visits, with
seven ready points and six bounded, verifiable outcomes.
"""
import asyncio
import copy
import json
import re
from pathlib import Path

import pytest
from asclepius import real_cases as RC
from tests.test_longitudinal_front_door import _isolated, store, _run

REFERENCE = json.loads((Path(__file__).resolve().parents[2] /
                       'prd-longitudinal-fix/patient4_reference_walk.json').read_text())


def test_reference_offsets_verifiability_and_bounded_reveal(store):
    res = _run(store, bundles=['patient-4'])
    chart = store.list_ingest_cases(upload_id=res['bundles'][0]['upload_id'])[0]['case']
    before = copy.deepcopy(chart)
    plan = asyncio.run(RC.plan_cases(chart, specialty_hint='cardiology',
                                   derive_questions=False, trajectory=True))
    assert chart == before
    points = [p for p in plan['proposals'] if p['qualifies_as_point']]
    assert plan['decision_points'] == REFERENCE['source']['decision_points']
    assert plan['encounters'] == 7
    assert plan['walk_points'] == 7
    assert plan['interval_points'] == 4
    assert plan['review_required_points'] == 0
    assert plan['ready_walk_points'] == 7
    assert plan['walk_verifiable_points'] == 6
    assert [p['generatable'] for p in points] == [True] * 7
    assert plan['verifiable_decision_points'] == REFERENCE['source']['verifiable_decision_points']
    assert [p['point_class'] for p in points] == [
        'decision', 'decision', 'interval', 'interval', 'interval', 'interval', 'decision']
    assert [p['index_event_offset'] for p in points] == [-406, -372, -280, -230, -92, -33, 1]
    for i, point in enumerate(points):
        assert point['encounter_index'] == i
        assert not point.get('review_required')
        assert point['generatable']
        assert point['outcome_verifiable'] == (i < 6)
        assert point['qualifies_as_decision_point'] == (point['point_class'] == 'decision')
        assert all(n['note_type'].lower() not in {'report', 'lab report'} for n in point['case']['notes'])
        assert all(n['collected_offset_days'] <= 0 for n in point['case']['notes'])
        assert not any(re.search('discharge', n['note_type'], re.I)
                       and n['collected_offset_days'] + point['index_event_offset'] >= point['encounter_span'][0]
                       for n in point['case']['notes'])
    first, second = points[:2]
    for point in (first, second):
        assert any(n['note_type'] == 'Admission' and n.get('model_visible') is not False
                   for n in point['case']['notes'])
    assert any(re.search(r'\b(?:nebil|nebivolol)\b', drug, re.I)
               for drug in first['held_out']['newly_started_drugs'])
    assert any(re.search(r'\b(?:dapa|dapagliflozin)\b', drug, re.I)
               for drug in first['held_out']['newly_started_drugs'])
    assert not any(re.search(r'\b(?:ascard|aspirin)\b', drug, re.I)
                   for drug in second['held_out']['newly_started_drugs'])
    assert any('Discharge' in n for n in first['held_out']['narrative_after'])
    assert '[+1d Discharge] Diagnosis: DYSPEPSIA/AGE' in first['case']['ground_truth']['rationale']
    delta = RC.outcome_delta(second['case'], outcome_index_offset=second['index_event_offset'],
                             decision_index_offset=first['index_event_offset'])
    assert any(re.search('discharge', n['note_type'], re.I) for n in delta['notes'])
    horizon = second['index_event_offset'] - first['index_event_offset']
    assert all(0 < n['collected_offset_days'] <= horizon for n in delta['notes'])
    assert not re.search(r'\b\d{4}-\d{2}-\d{2}\b', json.dumps([p['case'] for p in points]))
    assert all(int(day) <= horizon for day in re.findall(r'\+(\d+)d', json.dumps(first['held_out'])))


def _note(kind, off, text):
    return {'note_type': kind, 'collected_offset_days': off, 'text': text * 4}


def test_custom_encounter_gap_does_not_expose_early_discharge():
    chart = {'notes': [_note('Discharge', 0, 'Discharged with resolution. '),
                       _note('Progress', 10, 'Follow up information. ')]}
    enc = RC.segment_longitudinal_record(chart, min_gap_days=14)[0]
    visible, held, _ = RC.build_encounter_case(chart, enc, 9)
    assert not visible['notes']
    assert any('Discharge' in n for n in held['narrative_after'])


def test_held_out_bounds_every_collection():
    chart = {'notes': [_note('Progress', 2, 'Near future. '), _note('Progress', 20, 'Remote future. ')],
             'problem_list': [{'condition': 'early problem', 'collected_offset_days': 2},
                              {'condition': 'remote problem', 'collected_offset_days': 20}],
             'medications': [{'drug': 'Tab aspirin 75 mg OD', 'collected_offset_days': 2},
                             {'drug': 'Tab levetiracetam 500 mg BD', 'collected_offset_days': 20}],
             'lab_panels': [{'collected_offset_days': off, 'results': [
                 {'analyte': name, 'value': 1, 'flag': 'L'}]}
                 for off, name in [(2, 'near'), (20, 'remote')]]}
    held = RC._held_out_summary(chart, 0, until_offset=2)
    text = json.dumps(held)
    assert 'early problem' in text and 'aspirin' in text and 'near' in text
    assert 'remote' not in text.lower() and 'levetiracetam' not in text


def test_dedupe_removes_headers_but_preserves_repeat_visits():
    text = 'Presenting history with a unique narrative. ' * 10
    notes = [_note('Progress', 1, text), _note('Progress', 2, text)]
    notes.append({**notes[0], 'text': 'Document index: 1\nDocument type: progress\nCONTENT\n' + '-'*40 + '\n' + notes[0]['text']})
    result, stats = RC.curate_notes(notes)
    assert len(result) == 2 and stats['dropped_duplicate'] == 1


def test_implausible_note_dates_fail_closed_without_mutating_source():
    chart = {'lab_panels': [{'collected_offset_days': 0, 'results': []}],
             'notes': [_note('Nursing', -1200, 'Medication nursing history. '),
                       _note('Nursing', -1000, 'A supported threshold boundary. ')],
             'medications': [{'drug': 'levetiracetam', 'collected_offset_days': -1200}],
             'problem_list': [{'condition': 'seizure', 'since': 'day -1200', 'collected_offset_days': -1200}]}
    before = copy.deepcopy(chart)
    result = RC.prepare_longitudinal_chart(chart)
    assert result['notes'][0]['withheld_reason'] == 'implausible_date'
    assert result['notes'][1]['collected_offset_days'] == -1000
    assert result['medications'][0]['collected_offset_days'] is None
    assert result['problem_list'][0]['since'] is None
    assert chart == before


def test_note_role_budget_prefers_narrative_over_reports():
    notes = [_note('Nursing', -10, 'Earliest trend. '),
             _note('Orders', 0, 'Latest orders. '),
             _note('Report interpretation', 0, 'Latest report. '),
             _note('H&P', -2, 'Presenting complaint. ')]
    result = RC._budget(notes, 2, {}, 'drops', note_roles=True)
    assert [n['note_type'] for n in result] == ['Nursing', 'H&P']


def test_empty_chart_still_errors():
    with pytest.raises(RC.RealCaseError, match='empty case'):
        asyncio.run(RC.plan_cases(None, derive_questions=False))


@pytest.mark.parametrize('kind,text,offset,extra,expected', [
    ('Progress', 'Patient seen and examined; symptoms improving on current treatment. ', 0, {}, True),
    ('Consult', 'New presentation reviewed; assessment and treatment discussed. ', -1, {}, True),
    ('Progress', 'Form: Fresh Orders\nContinue scheduled medication chart. ', 0, {}, False),
    ('Progress', 'Form: Medication Form\nAdminister the scheduled doses. ', 0, {}, False),
    ('Progress', 'Patient Assessment\nSkin intact; routine nursing assessment. ', 0, {}, False),
    ('Radiology', 'Chest image reviewed; findings and interpretation follow. ', 0, {}, False),
    ('Discharge', 'Presenting complaints and final discharge treatment summary. ', 0, {}, False),
    ('Progress', 'Patient seen and examined; symptoms improving on current treatment. ', -20, {}, False),
    ('Progress', 'Patient seen and examined; symptoms improving on current treatment. ', 0, {'model_visible': False}, False),
    ('Progress', 'Patient seen and examined; symptoms improving on current treatment. ', None, {}, False),
])
def test_only_visible_contemporaneous_narratives_support_decision_class(kind, text, offset, extra, expected):
    case = {'notes': [{**_note(kind, offset, text), **extra}]}
    assert RC.has_encounter_narrative(case, {'start_offset': -2}, 0) is expected


def test_downgraded_middle_point_keeps_the_chain_bounded(monkeypatch):
    from tests.test_asclepius_longitudinal_e2e import build_chart
    chart = build_chart()
    encs = RC.segment_longitudinal_record(chart)
    middle = encs[1]
    for note in chart['notes']:
        if middle['start_offset'] <= note['collected_offset_days'] <= middle['end_offset']:
            note['note_type'] = 'Radiology'
    calls = []
    async def author(case, held, specialty):
        calls.append(case)
        return 'A question grounded in the current encounter.', 'test'
    monkeypatch.setattr(RC, 'derive_clinical_question', author)
    plan = asyncio.run(RC.plan_cases(chart, specialty_hint='hepatology', trajectory=True))
    points = [p for p in plan['proposals'] if p['qualifies_as_point']]
    assert [p['point_class'] for p in points] == ['decision', 'interval', 'decision', 'decision', 'decision']
    assert points[1]['density']['qualifies'] and points[1]['downgraded']
    assert all(p['generatable'] and not p.get('review_required') for p in points)
    assert len(calls) == plan['generatable'] == 5
    assert all(p['question'] for p in points)
    horizon = points[1]['index_event_offset'] - points[0]['index_event_offset']
    assert all(int(day) <= horizon for day in re.findall(r'\+(\d+)d', json.dumps(points[0]['held_out'])))


def test_explicit_selection_generates_patient4_presenting_admissions(store, monkeypatch):
    from fastapi.testclient import TestClient
    from tests import _asclepius as A
    from tests.test_asclepius_longitudinal_e2e import _stub_model_legs
    _stub_model_legs(monkeypatch)
    res = _run(store, bundles=['patient-4'])
    uid = res['bundles'][0]['upload_id']
    store.set_upload_purpose(uid, 'task_creation')
    ic = store.list_ingest_cases(upload_id=uid)[0]
    admin = A.headers_for(A.make_user(store, role='admin'))
    r = TestClient(A.app).post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
        headers=admin, json={'dry_run': False, 'trajectory': True, 'encounter_indices': [0, 1],
                             'apply_density_gate': False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['generated'] == 2 and body['gated'] == body['failed'] == 0
    assert body['review_required_points'] == 0 and body['details']['held'] == []
    assert [p['encounter_index'] for p in body['details']['generated']] == [0, 1]
    assert store.upload_task_counts(uid)['promoted'] == 1  # One chart, two tasks.
    assert len(store.trajectory_points(body['trajectory_id'])) == 2


def test_auto_generation_builds_patient4_walk_without_holds(store, monkeypatch):
    from asclepius import auto_generate as AG
    from tests.test_asclepius_longitudinal_e2e import _stub_model_legs
    import routers.asclepius as router
    _stub_model_legs(monkeypatch)
    res = _run(store, bundles=['patient-4'])
    uid = res['bundles'][0]['upload_id']
    store.set_upload_purpose(uid, 'task_creation')
    store.set_upload_task_mode(uid, 'longitudinal')
    seen = []
    original = router._generate_one_real_case
    async def checked(st, ic, point, admin, **kw):
        seen.append(point['encounter_index'])
        assert not point.get('review_required')
        return await original(st, ic, point, admin, **kw)
    monkeypatch.setattr(router, '_generate_one_real_case', checked)
    report = asyncio.run(AG.run_upload(store, uid, 'admin-test'))
    assert seen == list(range(7))
    assert report['generated'] == 7 and report['review_required_points'] == 0
    summary = AG.failure_summary(store.get_ingest_upload(uid))
    assert summary is None
    points = store.trajectory_points(report['trajectories'][0])
    assert len(points) == 7
    assert all(p['distribution'] == 'assigned_only' for p in points)


def test_interval_point_precedes_a_downgraded_successor_without_a_hold():
    from tests.test_asclepius_longitudinal_e2e import build_chart
    chart = build_chart()
    encs = RC.segment_longitudinal_record(chart)
    first, second = encs[:2]
    chart['lab_panels'] = [p for p in chart['lab_panels']
        if not first['start_offset'] <= p['collected_offset_days'] <= first['end_offset']]
    chart['studies'] = [p for p in chart['studies']
        if not first['start_offset'] <= p['collected_offset_days'] <= first['end_offset']]
    for note in chart['notes']:
        if second['start_offset'] <= note['collected_offset_days'] <= second['end_offset']:
            note['note_type'] = 'Radiology'
    plan = asyncio.run(RC.plan_cases(chart, specialty_hint='hepatology', trajectory=True, derive_questions=False))
    first_point = plan['proposals'][0]
    assert not first_point['qualifies_as_decision_point']
    assert first_point['qualifies_as_point'] and first_point['point_class'] == 'interval'
    assert first_point['generatable'] and not first_point.get('review_required')
    assert plan['proposals'][1]['downgraded'] and plan['proposals'][1]['generatable']
    assert plan['review_required_points'] == 0
    horizon = plan['proposals'][1]['index_event_offset'] - first_point['index_event_offset']
    assert all(int(day) <= horizon for day in re.findall(r'\+(\d+)d', json.dumps(first_point['held_out'])))


def test_all_report_encounters_generate_as_interval_points(store, monkeypatch):
    from asclepius import auto_generate as AG
    from tests.test_asclepius_longitudinal_e2e import _stub_model_legs
    _stub_model_legs(monkeypatch)
    res = _run(store, bundles=['patient-4'])
    uid = res['bundles'][0]['upload_id']
    store.set_upload_purpose(uid, 'task_creation')
    store.set_upload_task_mode(uid, 'longitudinal')
    ic = store.list_ingest_cases(upload_id=uid)[0]
    chart = ic['case']
    for note in chart['notes']:
        note['note_type'] = 'Radiology'
    plan = asyncio.run(RC.plan_cases(chart, specialty_hint='cardiology',
                                   trajectory=True, derive_questions=False))
    assert plan['decision_points'] == 0
    assert plan['walk_points'] == plan['interval_points'] == plan['ready_walk_points'] == 7
    assert plan['review_required_points'] == 0
    assert all(p['point_class'] == 'interval' and not p.get('review_required')
               for p in plan['proposals'] if p['qualifies_as_point'])
    assert store.upload_task_counts(uid)['promoted'] == 0
    store.update_ingest_case(ic['ingest_case_id'], case_json=chart)
    report = asyncio.run(AG.run_upload(store, uid, 'admin-test'))
    assert report['generated'] == 7
    assert report['cases_failed'] == report['failed'] == report['gated'] == report['review_required_points'] == 0
    tasks = [store.get_task(p['task_id']) for p in store.trajectory_points(report['trajectories'][0])]
    assert all(t['generation']['point_class'] == 'interval' for t in tasks)
    assert sum(bool(t['generation']['downgraded']) for t in tasks) == plan['downgraded_points']
    assert AG.failure_summary(store.get_ingest_upload(uid)) is None


def _generation_context(store):
    from fastapi.testclient import TestClient
    from tests import _asclepius as A
    res = _run(store, bundles=['patient-4'])
    uid = res['bundles'][0]['upload_id']
    store.set_upload_purpose(uid, 'task_creation')
    ic = store.list_ingest_cases(upload_id=uid)[0]
    admin = A.make_user(store, role='admin')
    return TestClient(A.app), ic, admin, A.headers_for(admin)


@pytest.mark.parametrize('include,indices', [(True, list(range(7))), (False, [0, 1, 6])])
def test_patient4_router_preview_and_generation_include_interval_toggle(store, monkeypatch, include, indices):
    from asclepius.timeline import datelike_leftovers, datelike_leftovers_in_text
    from tests.test_asclepius_longitudinal_e2e import _stub_model_legs
    _stub_model_legs(monkeypatch)
    client, ic, _, headers = _generation_context(store)
    url = f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate"
    body = {'trajectory': True, 'derive_questions': False}
    if not include:  # Omission must default to including interval visits.
        body['include_interval_points'] = False
    preview = client.post(url, headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan['trajectory_points'] == plan['walk_points'] == plan['ready_walk_points'] == len(indices)
    assert plan['decision_points'] == 3
    assert plan['interval_points'] == (4 if include else 0)
    assert plan['walk_verifiable_points'] == len(indices) - 1
    assert plan['downgraded_points'] == plan['review_required_points'] == 0
    proposals = [p for p in plan['proposals'] if p['qualifies_as_point']]
    assert [p['encounter_index'] for p in proposals] == indices
    assert all('ground_truth' not in p['case'] and 'held_out' not in p for p in proposals)
    assert all(p['point_class'] == ('decision' if p['encounter_index'] in (0, 1, 6) else 'interval')
               and p['downgraded'] is None for p in proposals)
    assert all(p['presenting_narrative'] for p in proposals if p['point_class'] == 'decision')

    response = client.post(url, headers=headers, json={**body, 'dry_run': False})
    assert response.status_code == 200, response.text
    generated = response.json()
    assert generated['generated'] == generated['trajectory_points'] == len(indices)
    assert generated['gated'] == generated['failed'] == 0
    assert generated['walk_verifiable_points'] == len(indices) - 1
    assert generated['estimated_cost_usd'] == 75 * len(indices)
    points = store.trajectory_points(generated['trajectory_id'])
    assert [p['sequence_index'] for p in points] == list(range(len(indices)))
    tasks = [store.get_task(p['task_id']) for p in points]
    assert [t['generation']['encounter_index'] for t in tasks] == indices
    for task, proposal in zip(tasks, proposals):
        assert task['distribution'] == 'assigned_only' and task['max_labels'] == 1
        assert task['specialty'] == 'cardiology'
        for key in ('point_class', 'presenting_narrative', 'downgraded'):
            assert task['generation'][key] == proposal[key]
        assert not datelike_leftovers(task['case'])
        assert not datelike_leftovers_in_text(task['prompt'])


@pytest.mark.parametrize('bad_evidence', ['no_current_observation', 'future_note', 'sealed_answer'])
def test_interval_generation_preserves_content_and_leakage_gates(store, monkeypatch, bad_evidence):
    import routers.asclepius as router
    from asclepius import empirical_difficulty
    _, ic, admin, _ = _generation_context(store)
    plan = asyncio.run(RC.plan_cases(ic['case'], specialty_hint='cardiology',
                                   trajectory=True, derive_questions=False))
    point = plan['proposals'][4]
    assert point['point_class'] == 'interval'
    case = point['case']
    if bad_evidence == 'no_current_observation':
        case['notes'] = [n for n in case['notes'] if n['collected_offset_days'] < 0]
    elif bad_evidence == 'future_note':
        case['notes'].append(_note('Progress', 1, 'A future observation that must be withheld. '))
    else:
        answer = 'Unique confirmatory diagnostic finding'
        point['held_out']['newly_established_problems'] = [answer]
        case['notes'].append(_note('Progress', 0, answer + '. '))
    async def forbidden(*args, **kwargs):
        raise AssertionError('invalid evidence reached a model')
    monkeypatch.setattr(empirical_difficulty, 'measure_empirical_difficulty', forbidden)
    result = asyncio.run(router._generate_one_real_case(
        store, ic, point, admin, max_labels=1, grounding_mode=None, independent_mode=None,
        trajectory_id='test-interval-gates', sequence_index=0, distribution='assigned_only'))
    assert result['task_id'] is None
    assert result['error'].startswith('content/leakage gate:')
    assert store.upload_task_counts(ic['upload_id'])['promoted'] == 0


@pytest.mark.parametrize('apply_gate,selected', [(True, 6), (False, 7)])
def test_density_override_keeps_all_generatable_proposals(store, monkeypatch, apply_gate, selected):
    client, ic, _, headers = _generation_context(store)
    original = RC.plan_cases
    async def with_ineligible_point(*args, **kwargs):
        plan = await original(*args, **kwargs)
        plan['proposals'][4]['qualifies_as_point'] = False
        return plan
    monkeypatch.setattr(RC, 'plan_cases', with_ineligible_point)
    response = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
        headers=headers, json={'trajectory': True, 'derive_questions': False,
                               'apply_density_gate': apply_gate})
    assert response.status_code == 200, response.text
    assert response.json()['selected'] == selected


@pytest.mark.parametrize('all_fail,n', [(False, 6), (True, 0)])
def test_generated_walk_count_excludes_failed_points(store, monkeypatch, all_fail, n):
    import routers.asclepius as router
    from tests.test_asclepius_longitudinal_e2e import _stub_model_legs
    _stub_model_legs(monkeypatch)
    client, ic, _, headers = _generation_context(store)
    original = router._generate_one_real_case
    async def fail_middle(*args, **kwargs):
        point = args[2]
        if all_fail or point['encounter_index'] == 3:
            return {'encounter_index': point['encounter_index'], 'error': 'test generation failure'}
        return await original(*args, **kwargs)
    monkeypatch.setattr(router, '_generate_one_real_case', fail_middle)
    response = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
        headers=headers, json={'trajectory': True, 'dry_run': False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['generated'] == body['trajectory_points'] == n
    assert body['failed'] == 7 - n
    assert body['walk_verifiable_points'] == max(0, n - 1)
    if n:
        assert body['estimated_cost_usd'] == n * 75
        assert [p['sequence_index'] for p in store.trajectory_points(body['trajectory_id'])] == list(range(n))


def test_walk_requires_one_declared_specialty(store):
    client, ic, _, headers = _generation_context(store)
    store.set_ingest_specialty_for_upload(ic['upload_id'], 'undetermined')
    response = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
        headers=headers, json={'trajectory': True, 'derive_questions': False})
    assert response.status_code == 422
    assert 'specialty' in response.json()['detail']
    selected = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
        headers=headers, json={'trajectory': True, 'derive_questions': False, 'specialty': 'hepatology'})
    assert selected.status_code == 200, selected.text
    assert all(p['specialty'] == 'hepatology' for p in selected.json()['proposals'])


@pytest.mark.parametrize('text', [
    'Avoid TAB aspirin 75 mg, TAB clopidogrel 75 mg',
    'Avoid TAB aspirin 75 mg; TAB clopidogrel 75 mg',
    'Allergic to INJ ceftriaxone 2 g',
    'Consider starting TAB carvedilol 6.25 mg at next review',
])
def test_discharge_prose_does_not_invent_medication_orders(text):
    assert RC._discharge_medication_orders(text) == []


def test_inline_historical_orders_are_known_but_future_orders_stay_sealed():
    chart = {'notes': [{
        'note_type': 'Discharge', 'collected_offset_days': -20,
        'text': 'Hospital Course: symptoms improved; inpatient meds included '
                'INJ ONSET 8MG IV OD, INJ CEFTRO 2G IV OD.\nFollow-up advised.',
    }], 'medications': [
        {'drug': drug, 'collected_offset_days': 2} for drug in
        ['INJ ONSET 8MG IV OD', 'INJ CEFTRO 2G IV OD', 'TAB dapagliflozin 10 mg OD']
    ]}
    case, held, _ = RC.build_encounter_case(chart,
        {'start_offset': 0, 'end_offset': 3}, 0, until_offset=3, trajectory=True)
    assert {RC._drug_identity(d) for d in held['newly_started_drugs']} == {'dapagliflozin'}
    assert len(held['drugs_started_after']) == 3
    assert 'dapagliflozin' not in json.dumps({k: v for k, v in case.items() if k != 'ground_truth'})
    from asclepius.ingestion import assert_no_answer_leakage
    assert_no_answer_leakage(case, {'answer_key': {'treatment': held['newly_started_drugs']}})
