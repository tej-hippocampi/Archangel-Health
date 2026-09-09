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
