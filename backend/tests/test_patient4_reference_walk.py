"""Temporal safeguards, tested on the real reference chart and adversarial cases.

The user accepted the measured yields and evidence-review holds. Detection
counts remain distinct from readiness: patient-4 detects three points, holds
two narrative-incomplete points, and permits only the terminal point to build.
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
    points = [p for p in plan['proposals'] if p['qualifies_as_decision_point']]
    assert len(points) == REFERENCE['source']['decision_points']
    assert plan['encounters'] == 8
    assert plan['review_required_points'] == 2
    assert plan['ready_decision_points'] == 1
    assert [p['generatable'] for p in points] == [False, False, True]
    assert plan['verifiable_decision_points'] == REFERENCE['source']['verifiable_decision_points']
    # The accepted reference retains historical ordinals separately for audit.
    for point, ref in zip(points, REFERENCE['points']):
        assert point['encounter_index'] == ref['encounter_index']
        assert bool(point.get('review_required')) == ref['review_required']
        assert point['generatable'] == ref['generation_ready']
        assert point['index_event_offset'] == ref['index_event_offset_chart_days']
        assert point['outcome_verifiable'] == ref['outcome_verifiable']
        assert point['qualifies_as_decision_point'] == ref['qualifies_as_decision_point']
        assert all(n['note_type'].lower() not in {'report', 'lab report'} for n in point['case']['notes'])
        assert all(n['collected_offset_days'] <= 0 for n in point['case']['notes'])
        assert not any(re.search('discharge', n['note_type'], re.I)
                       and n['collected_offset_days'] + point['index_event_offset'] >= point['encounter_span'][0]
                       for n in point['case']['notes'])
    first, second = points[:2]
    assert any('Discharge' in n for n in first['held_out']['narrative_after'])
    assert 'DISCHARGE SUMMARY' in first['case']['ground_truth']['rationale']
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
def test_only_visible_contemporaneous_narratives_clear_review(kind, text, offset, extra, expected):
    case = {'notes': [{**_note(kind, offset, text), **extra}]}
    assert RC.has_encounter_narrative(case, {'start_offset': -2}, 0) is expected


def test_chain_does_not_bridge_a_held_outcome(monkeypatch):
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
    points = [p for p in plan['proposals'] if p['qualifies_as_decision_point']]
    assert points[0]['review_reasons'][0]['reason'] == 'outcome_requires_review'
    assert points[1]['review_reasons'][0]['reason'] == 'missing_encounter_narrative'
    assert not points[0]['generatable'] and not points[1]['generatable']
    assert points[-1]['generatable']
    assert len(calls) == plan['generatable']
    assert all('question' not in p for p in points[:2])


def test_explicit_selection_cannot_override_patient4_holds(store, monkeypatch):
    from fastapi.testclient import TestClient
    from tests import _asclepius as A
    res = _run(store, bundles=['patient-4'])
    uid = res['bundles'][0]['upload_id']
    store.set_upload_purpose(uid, 'task_creation')
    ic = store.list_ingest_cases(upload_id=uid)[0]
    admin = A.headers_for(A.make_user(store, role='admin'))
    async def forbidden(*args, **kwargs):
        raise AssertionError('held point reached a model call')
    monkeypatch.setattr(RC, 'derive_clinical_question', forbidden)
    r = TestClient(A.app).post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
        headers=admin, json={'dry_run': False, 'trajectory': True, 'encounter_indices': [1, 2],
                             'apply_density_gate': False})
    assert r.status_code == 422, r.text
    assert 'Review required' in r.text
    assert store.upload_task_counts(uid)['promoted'] == 0


def test_auto_generation_preserves_and_reports_patient4_holds(store, monkeypatch):
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
    assert seen == [7]
    assert report['generated'] == 1 and report['review_required_points'] == 2
    summary = AG.failure_summary(store.get_ingest_upload(uid))
    assert summary['count'] == 2
    assert all(d['review_required'] for d in summary['dropped'])
    points = store.trajectory_points(report['trajectories'][0])
    assert len(points) == 1
    assert points[0]['distribution'] == 'assigned_only'


def test_density_override_cannot_bridge_a_held_successor():
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
    assert first_point['review_required'] and not first_point['generatable']
    assert first_point['review_reasons'][0]['reason'] == 'outcome_requires_review'


def test_all_held_auto_run_is_reported_as_evidence_review(store, monkeypatch):
    from asclepius import auto_generate as AG
    res = _run(store, bundles=['patient-4'])
    uid = res['bundles'][0]['upload_id']
    store.set_upload_purpose(uid, 'task_creation')
    store.set_upload_task_mode(uid, 'longitudinal')
    ic = store.list_ingest_cases(upload_id=uid)[0]
    chart = ic['case']
    for note in chart['notes']:
        note['note_type'] = 'Radiology'
    store.update_ingest_case(ic['ingest_case_id'], case_json=chart)
    async def forbidden(*args, **kwargs):
        raise AssertionError('all-held run reached a model')
    monkeypatch.setattr(RC, 'derive_clinical_question', forbidden)
    report = asyncio.run(AG.run_upload(store, uid, 'admin-test'))
    assert report['generated'] == report['cases_failed'] == 0
    assert report['review_required_points'] == 3
    assert AG.failure_summary(store.get_ingest_upload(uid))['count'] == 3
    assert store.upload_task_counts(uid)['promoted'] == 0
