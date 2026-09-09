"""Retry preserves audit history, specialty, and task links."""
import pytest
from tests.test_asclepius_ingestion import (
    _isolated, _store, _admin_h, _mint, _upload, _zip, _manifest, _CSV, client,
)
from asclepius import ingestion
from routers.asclepius import _upload_content_view


def _seed():
    st = _store()
    admin = _admin_h()
    link = _mint(admin)
    uid = _upload(link['token'], _zip({'manifest.json': _manifest(), 'labs.csv': _CSV}))['upload_id']
    for status in ('quarantined', 'needs_review'):
        st.insert_ingest_case(upload_id=uid, patient_key=status, specialty='cardiology',
                              case={'notes': []}, status=status, report={'legacy': True})
    st.set_ingest_specialty_for_upload(uid, 'cardiology')
    return st, admin, uid


def test_retry_keeps_all_history_and_declared_specialty():
    st, admin, uid = _seed()
    before = st.list_ingest_cases(upload_id=uid)
    r = client.post(f'/api/asclepius/ingestion/uploads/{uid}/retry', headers=admin)
    assert r.status_code == 200, r.text
    after = st.list_ingest_cases(upload_id=uid)
    assert len(after) == 4
    active = [c for c in after if c['status'] != 'superseded']
    assert len(active) == 1 and active[0]['status'] == 'ingested'
    assert active[0]['specialty'] == 'cardiology'
    assert active[0]['report']['pipeline_version'] == ingestion.INGEST_PIPELINE_VERSION
    for old in before:
        saved = st.get_ingest_case(old['ingest_case_id'])
        assert saved['status'] == 'superseded'
        for key in ('case', 'report', 'task_id'):
            assert saved.get(key) == old.get(key)
    assert _upload_content_view(after)['charts'] == 1
    assert st.upload_task_counts(uid)['total'] == 1


def test_retry_refuses_promoted_or_task_linked_rows():
    st, admin, uid = _seed()
    old = st.list_ingest_cases(upload_id=uid)[0]
    st.update_ingest_case(old['ingest_case_id'], status='promoted', task_id='task-preserved')
    r = client.post(f'/api/asclepius/ingestion/uploads/{uid}/retry', headers=admin)
    assert r.status_code == 409 and 'promoted_cases' in r.text
    assert len(st.list_ingest_cases(upload_id=uid)) == 3
    assert st.get_ingest_case(old['ingest_case_id'])['task_id'] == 'task-preserved'


def test_duplicate_retry_is_reserved_once():
    st, _, uid = _seed()
    assert st.begin_ingest_retry(uid) == 3
    with pytest.raises(ValueError, match='retry_in_progress'):
        st.begin_ingest_retry(uid)
    assert len(st.list_ingest_cases(upload_id=uid)) == 3


def test_retry_winning_race_prevents_task_from_stale_conversion():
    st, _, uid = _seed()
    old = next(c for c in st.list_ingest_cases(upload_id=uid) if c['status'] == 'ingested')
    st.begin_ingest_retry(uid)
    with pytest.raises(ValueError, match='ingest_case_changed'):
        st.insert_task(prompt='Already converted', ingest_case_id=old['ingest_case_id'], task_id='race-task')
    assert st.get_task('race-task') is None
    assert st.get_ingest_case(old['ingest_case_id'])['status'] == 'superseded'


def test_task_winning_race_blocks_retry_atomically():
    st, _, uid = _seed()
    old = next(c for c in st.list_ingest_cases(upload_id=uid) if c['status'] == 'ingested')
    task = st.insert_task(prompt='Converted', ingest_case_id=old['ingest_case_id'])
    with pytest.raises(ValueError, match='promoted_cases'):
        st.begin_ingest_retry(uid)
    assert st.get_ingest_case(old['ingest_case_id'])['task_id'] == task['task_id']


def test_zero_generatable_explains_no_model_call(monkeypatch):
    st, admin, uid = _seed()
    old = next(c for c in st.list_ingest_cases(upload_id=uid) if c['status'] == 'ingested')
    from asclepius import real_cases
    async def unexpected(*args, **kwargs):
        raise AssertionError('zero generatable must not author a question')
    monkeypatch.setattr(real_cases, 'derive_clinical_question', unexpected)
    r = client.post(f"/api/asclepius/ingestion/cases/{old['ingest_case_id']}/generate",
                    headers=admin, json={'dry_run': True, 'trajectory': True})
    assert r.status_code == 200, r.text
    assert r.json()['generatable'] == 0
    assert r.json()['why'] == 'no model was called: 0 encounters cleared the gate'


def test_old_unbound_key_never_attaches_to_retried_generation():
    st, _, uid = _seed()
    old = next(c for c in st.list_ingest_cases(upload_id=uid) if c['status'] == 'ingested')
    sid = st.stage_sealed_ground_truth(upload_id=uid, patient_key=old['patient_key'],
                                       payload={'answer': 'previous generation'})
    st.begin_ingest_retry(uid)
    new = st.insert_ingest_case(upload_id=uid, patient_key=old['patient_key'],
                                specialty='cardiology', case=old['case'], status='ingested', report={})
    result = st.reconcile_sealed_ground_truth(older_than_seconds=0)
    assert any(o['sealed_id'] == sid for o in result['orphans'])
    assert result['bound'] == 0
    assert st.get_ingest_case(new['ingest_case_id'])['status'] == 'ingested'
