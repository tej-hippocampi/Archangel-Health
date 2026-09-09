"""Temporal safeguards, tested on the real reference chart and adversarial cases.

The PRD's 7-encounter/13-patient1-point/narrative expectations are unresolved
acceptance conflicts. Existing front-door assertions intentionally still expose
those discrepancies rather than weakening a density or leakage threshold.
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
    assert plan['verifiable_decision_points'] == REFERENCE['source']['verifiable_decision_points']
    # The reference's encounter ordinals predate removal of unsupported dates.
    # Chart-relative index offsets remain the stable identity of each point.
    for point, ref in zip(points, REFERENCE['points']):
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
