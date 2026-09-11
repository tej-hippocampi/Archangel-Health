"""P4 export contracts on real packager output and filtered buyer profiles."""
import copy
import hashlib
import json
import re
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from asclepius import export as EX, packaging, profiles
from asclepius.timeline import datelike_leftovers_in_text
from tests.test_longitudinal_front_door import _isolated, store
from tests.test_longitudinal_v5_relabel import _real_case, _candidates


def _point(store, seq, cls, trajectory_id='walk-p4', downgraded=None):
    return store.insert_task(prompt=f'Assess the observations at step {seq}.', specialty='hepatology',
        case=_real_case(), candidate_answers=_candidates(), trajectory_id=trajectory_id,
        sequence_index=seq if trajectory_id else None, distribution='assigned_only', max_labels=1,
        generation={'point_class': cls, 'presenting_narrative': cls == 'decision',
                    'downgraded': downgraded, 'generated_at': '2026-08-01T12:00:00'})


def _package(store, task, *, only=None, rationale='Interpret the recorded trend.'):
    sid = 'sub-' + uuid.uuid4().hex
    specialty = task['specialty']
    chosen, rejected = [a['id'] for a in task['candidate_answers'][:2]]
    sub = store.insert_submission(submission_id=sid, task_id=task['task_id'], evaluator_id='doctor-p4',
        verdict='A_better', chosen_id=chosen, rejected_id=rejected, confidence='high', time_spent_sec=300,
        payload={'chosen_revision': {'edited': True, 'revised_text': 'Continue monitoring the trend.',
                                    'why_better_notes': rationale},
                 'rubric': [{'text': 'Interprets the recorded clinical observations', 'points': 3},
                            {'text': 'Avoids unindicated repeat intervention', 'points': -3}]},
        annotator={'credential': 'board_certified_' + specialty, 'specialty': specialty,
                   'id_hashed': 'doctor-hash-p4'}, dedupe_hash=None, portal_version='v5', status='export_ready')
    packaged = packaging.package_submission(task, sub)
    assert packaged
    for payload in packaged:
        if only and payload['type'] != only:
            continue
        assert 'trajectory' not in payload  # Fresh records do not preattach the export annex.
        store.insert_record(submission_id=sid, task_id=task['task_id'], rtype=payload['type'],
                            specialty=specialty, payload=payload, status='export_ready')
    return sid


def _read(result, name):
    return (Path(result['dir_path']) / name).read_text()


def _assert_bundle(result):
    for name in result['files']:
        text = _read(result, name)
        assert not datelike_leftovers_in_text(text), name
        assert not re.search(r'"[^"\n]*(?:price|_usd|_cents)[^"\n]*"\s*:', text, re.I), name
        if name in result['content_hashes']:
            assert hashlib.sha256(text.encode()).hexdigest() == result['content_hashes'][name]


def test_classes_count_distinct_shipped_points_and_keep_stored_audit(store):
    decision = _point(store, 0, 'decision')
    interval = _point(store, 1, 'interval', downgraded='no presenting narrative')
    legacy = _point(store, 2, None)
    _package(store, legacy)
    _package(store, interval)
    sid = _package(store, decision)
    _package(store, decision)  # A second label and multiple record types are one point.
    before = copy.deepcopy(store.list_records(status='export_ready'))
    result = EX.build_export(store, created_by='admin')
    _assert_bundle(result)
    rows = [json.loads(line) for line in _read(result, EX.JSONL_NAME).splitlines()]
    assert {r['type'] for r in rows} >= {'preference', 'ideal_answer', 'rubric'}
    assert [r['trajectory']['sequence_index'] for r in rows] == sorted(r['trajectory']['sequence_index'] for r in rows)
    expected = {'decision': 1, 'interval': 1, 'unclassified': 1}
    assert all(r['trajectory']['point_counts'] == expected for r in rows)
    assert {r['trajectory']['point_class'] for r in rows} == {'decision', 'interval', None}
    assert all(r['trajectory']['downgraded'] == 'no presenting narrative'
               for r in rows if r['trajectory']['sequence_index'] == 1)
    cases = [json.loads(line) for line in _read(result, EX.CASES_NAME).splitlines()]
    assert len(cases) == 3 and all(c['point_counts'] == expected for c in cases)
    assert '1 trajectory · 3 points' in _read(result, EX.DATASHEET_NAME)
    assert '| `walk-p4` | 1 | 1 | 1 |' in _read(result, EX.DATASHEET_NAME)
    assert store.get_submission(sid)['created_at']
    after = {r['record_id']: r for r in store.list_records(status='exported')}
    assert all(after[r['record_id']]['payload'] == r['payload'] for r in before)
    assert result['created_at'] and result['date_free_chart_walk']
    # A lost export directory must not expose the internal dated manifest.
    fallback = EX.zip_export({'dir_path': '/missing-p4-directory', 'manifest': result})
    manifest_text = zipfile.ZipFile(BytesIO(fallback)).read(EX.MANIFEST_NAME).decode()
    assert not datelike_leftovers_in_text(manifest_text)


def test_profile_alias_and_filter_counts_only_shipped_points(store, monkeypatch):
    _package(store, _point(store, 0, 'decision'), only='preference')
    _package(store, _point(store, 1, 'interval'), only='rubric')
    prof = copy.deepcopy(profiles.load_profile('default'))
    prof['record_types'] = ['preference']
    prof['field_maps']['preference']['flat']['context'] = 'payload'
    prof['field_maps']['preference']['flat']['captured_at'] = 'capture_date'
    monkeypatch.setattr(profiles, 'load_profile', lambda name: prof)
    result = EX.build_export(store, created_by='admin')
    rows = [json.loads(line) for line in _read(result, EX.JSONL_NAME).splitlines()]
    assert len(rows) == 1 and 'payload' in rows[0] and 'capture_date' not in rows[0]
    assert rows[0]['trajectory']['point_counts'] == {'decision': 1, 'interval': 0, 'unclassified': 0}
    assert '1 trajectory · 1 point' in _read(result, EX.DATASHEET_NAME)
    _assert_bundle(result)


@pytest.mark.parametrize('where', ['record', 'companion', 'expiry', 'required_alias'])
def test_rejected_walk_export_preserves_record_and_submission_state(store, monkeypatch, where):
    sid = _package(store, _point(store, 0, 'decision'),
                   rationale='Review on 2025-01-23.' if where == 'record' else 'Interpret the trend.')
    kwargs = {}
    if where == 'companion':
        kwargs['note'] = 'Clinical event on 2025-01-23.'
    elif where == 'expiry':
        kwargs.update(licensed_to='lab-p4', license_expires_at='2027-01-23')
    elif where == 'required_alias':
        prof = copy.deepcopy(profiles.load_profile('default'))
        prof['field_maps']['preference']['flat']['prompt'] = 'created_at'
        prof['schemas']['preference']['flat']['required'] = ['created_at']
        monkeypatch.setattr(profiles, 'load_profile', lambda name: prof)
    before = copy.deepcopy(store.list_records(status='export_ready'))
    with pytest.raises(EX.ExportValidationError):
        EX.build_export(store, created_by='admin', **kwargs)
    assert store.list_records(status='export_ready') == before
    assert store.get_submission(sid)['status'] == 'export_ready'


def test_static_profile_can_name_its_text_field_payload(store, monkeypatch):
    _package(store, _point(store, 0, None, trajectory_id=None), only='preference')
    prof = {'name': 'text_alias', 'record_types': ['preference'],
            'field_maps': {'preference': {'flat': {'prompt': 'payload', 'type': 'type'}}},
            'schemas': {'preference': {'flat': {'required': ['payload', 'type'],
                                              'properties': {'payload': {'type': 'string'}}}}}}
    monkeypatch.setattr(profiles, 'load_profile', lambda name: prof)
    result = EX.build_export(store, created_by='admin')
    rows = [json.loads(line) for line in _read(result, EX.JSONL_NAME).splitlines()]
    assert rows[0]['payload'] == 'Assess the observations at step 0.'
    assert 'Scope: **V5 longitudinal**' not in _read(result, EX.DATASHEET_NAME)
