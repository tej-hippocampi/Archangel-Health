"""Failure-oriented regressions for the ingestion and onboarding preservation audit."""
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import _asclepius as A
from tests import test_asclepius_ingestion as I
from tests import test_hs_onboarding as H
from asclepius import ingestion, real_cases, hs_mail


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv('ASCLEPIUS_INGEST_DIR', str(tmp_path / 'ingest'))
    monkeypatch.setenv('ASCLEPIUS_ASSET_STORE', str(tmp_path / 'assets'))
    monkeypatch.setenv('DATA_ENCRYPTION_KEY', H._KEY)
    monkeypatch.setenv('ENV', 'test')
    monkeypatch.setenv('ASCLEPIUS_PORTAL_BUDGET_MS', '0')


def upload(entries):
    link = I._mint(I._admin_h())
    result = I._upload(link['token'], I._zip(entries))
    store = I._store()
    return store, store.get_ingest_upload(result['upload_id'])


def test_partial_bundle_is_held_and_original_survives_age():
    store, receipt = upload({'manifest.json': I._manifest(), 'labs.csv': I._CSV, 'unparsed.pdf': b'unknown bytes'})
    assert receipt['status'] == 'needs_review'
    assert receipt['retain_raw']
    cases = store.list_ingest_cases(upload_id=receipt['upload_id'])
    assert cases and all(c['status'] == 'needs_review' for c in cases)
    original = Path(receipt['raw_path']).read_bytes()
    os.utime(receipt['raw_path'], (0, 0))
    ingestion.purge_expired_raw(store)
    assert Path(receipt['raw_path']).read_bytes() == original


def test_multiple_patients_inside_one_file_cannot_create_a_mixed_case():
    csv = I._CSV.replace('p1,BMP,Sodium,2951-2,124', 'p2,BMP,Sodium,2951-2,124')
    store, receipt = upload({'manifest.json': I._manifest(), 'labs.csv': csv})
    assert receipt['status'] == 'rejected'
    assert receipt['retain_raw']
    assert not store.list_ingest_cases(upload_id=receipt['upload_id'])


def test_cross_format_keys_require_explicit_mapping():
    store, receipt = upload({'labs.csv': I._CSV, 'chart.json': I._fhir()})
    cases = store.list_ingest_cases(upload_id=receipt['upload_id'])
    assert len(cases) == 2
    assert all(c['status'] == 'needs_review' for c in cases)


def test_short_clinical_note_survives_in_source_chart():
    note = 'Stop heparin. Active bleeding.'
    store, receipt = upload({'manifest.json': I._manifest(), 'labs.csv': I._CSV, 'note.txt': note})
    cases = store.list_ingest_cases(upload_id=receipt['upload_id'])
    assert any(note in n['text'] for c in cases for n in c['case']['notes'])


def test_different_note_tails_and_specimens_are_preserved():
    shared = 'Clinical assessment unchanged. ' * 20
    notes, _ = real_cases.curate_notes([{'text': shared + ending, 'collected_offset_days': 0}
                                      for ending in ('Start insulin.', 'Stop insulin.')])
    assert len(notes) == 2
    panels, _ = real_cases.curate_lab_panels([
        {'panel': name, 'collected_offset_days': 0, 'results': [{'analyte': 'WBC', 'value': 10, 'unit': unit, 'loinc': code}]}
        for name, unit, code in [('CBC', '10^9/L', '6690-2'), ('Urinalysis', '/hpf', '5821-4')]])
    assert len(panels) == 2


def test_duplicate_task_id_cannot_replace_existing_evidence():
    store = I._store()
    first = store.insert_task(task_id='stable-task', prompt='Original clinical evidence', case={'notes': [{'text': 'Preserve me'}]})
    with pytest.raises(ValueError, match='already exists'):
        store.insert_task(task_id='stable-task', prompt='Replacement')
    assert store.get_task('stable-task') == first


def test_link_consumption_rolls_back_with_receipt_failure():
    store = I._store()
    link = I._mint(I._admin_h())
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON ingest_uploads BEGIN SELECT RAISE(ABORT,'injected disk failure'); END")
    client = TestClient(A.app, raise_server_exceptions=False)
    body = {'file': ('bundle.zip', I._zip({'manifest.json': I._manifest(), 'labs.csv': I._CSV}), 'application/zip')}
    assert client.post(f"/api/asclepius/partner/uploads?t={link['token']}", files=body).status_code == 500
    assert store.get_upload_link(link['link_id'])['used_count'] == 0
    with store._conn() as conn:
        conn.execute('DROP TRIGGER fail_receipt')
    assert client.post(f"/api/asclepius/partner/uploads?t={link['token']}", files=body).status_code == 200
    assert len(store.list_ingest_uploads()) == 1


def onboard():
    client = TestClient(A.app, base_url='https://testserver', raise_server_exceptions=False)
    org = H._signup(client, password=H.PASSWORD)
    assert H._apply(client).status_code == 200
    return client, I._store(), org


def test_approval_and_outbox_failure_leave_application_in_pending_review():
    client, store, org = onboard()
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER fail_mail BEFORE INSERT ON admin_notify_outbox WHEN NEW.kind='hs_dla_request' BEGIN SELECT RAISE(ABORT,'queue unavailable'); END")
    assert H._approve(client, store, org['hs_id']).status_code == 500
    assert store.get_health_system(org['hs_id'])['onboarding_state'] == 'submitted'
    assert store.get_hs_portal_user(org['username'])['approval_status'] == 'pending'
    assert any(u['username'] == org['username'] for u in store.list_hs_pending_signups())


def test_signature_failure_rolls_back_activation_and_receipt_jobs():
    client, store, org = onboard()
    assert H._approve(client, store, org['hs_id']).status_code == 200
    before = hs_mail.status(store, org['hs_id'])
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER fail_signature BEFORE INSERT ON signed_agreements BEGIN SELECT RAISE(ABORT,'signature write failed'); END")
    assert H._sign(client).status_code == 500
    assert store.latest_signed_agreement(org['hs_id']) is None
    assert store.get_health_system(org['hs_id'])['onboarding_state'] == 'approved_awaiting_dla'
    assert hs_mail.status(store, org['hs_id']) == before
    with store._conn() as conn:
        conn.execute('DROP TRIGGER fail_signature')
    assert H._sign(client).status_code == 200
    assert store.get_health_system(org['hs_id'])['onboarding_state'] == 'active'


def test_agreement_email_is_retried_after_transport_failure(monkeypatch):
    client, store, org = onboard()
    async def failed(*args, **kwargs): return False
    monkeypatch.setattr('email_utils.is_email_transport_configured', lambda: True)
    monkeypatch.setattr('email_utils.send_html_email', failed)
    response = H._approve(client, store, org['hs_id'])
    assert response.status_code == 200
    rows = [r for r in hs_mail.status(store, org['hs_id']) if r['kind'] == 'hs_dla_request']
    assert len(rows) == 1 and rows[0]['status'] == 'pending' and rows[0]['send_attempts'] == 1
    sent = []
    async def accepted(to, *args, **kwargs): sent.append(to); return True
    monkeypatch.setattr('email_utils.send_html_email', accepted)
    import main
    asyncio.run(main._drain_admin_notifications())
    asyncio.run(main._drain_admin_notifications())
    assert sent.count(org['email']) == 1
    assert [r for r in hs_mail.status(store, org['hs_id']) if r['kind'] == 'hs_dla_request'][0]['status'] == 'sent'


@pytest.mark.parametrize('answer', ['needs_baa', 'not_sure'])
def test_privacy_requirements_cannot_be_bypassed_with_standard_dla(answer):
    client, store, org = onboard()
    assert H._apply(client, deid_capability=answer).status_code == 200
    assert H._approve(client, store, org['hs_id']).status_code == 409
    assert store.get_health_system(org['hs_id'])['onboarding_state'] == 'submitted'


def test_intake_overflow_is_rejected_instead_of_truncated():
    client, store, org = onboard()
    from routers.asclepius_provider import _HS_INTAKE_PROMPTS
    body = {p['key']: 'Answer' for p in _HS_INTAKE_PROMPTS}
    body[_HS_INTAKE_PROMPTS[0]['key']] = 'x' * 4001
    assert client.post('/api/asclepius/hs/intake', json=body).status_code == 422
    assert not store.list_hs_intake(org['hs_id'])


def test_fhir_vitals_use_full_timestamp_and_preserve_all_readings():
    from asclepius.adapters.fhir_r4 import parse
    resources = [{'resourceType': 'Observation', 'category': [{'coding': [{'code': 'vital-signs'}]}],
                  'code': {'text': 'Heart rate'}, 'valueQuantity': {'value': value}, 'effectiveDateTime': date}
                 for value, date in [(120, '2031-03-19T16:00:00Z'), (70, '2031-03-19T08:00:00Z'), (55, 'unreadable')]]
    fragment = parse({'resourceType': 'Bundle', 'entry': [{'resource': r} for r in resources]})
    assert fragment['vitals']['Heart rate'] == 120
    assert len(fragment['notes']) == 3


def test_fhir_subject_ownership_without_patient_rows_is_not_merged():
    from asclepius.adapters.fhir_r4 import parse
    resources = [{'resourceType': 'Condition', 'subject': {'reference': 'Patient/' + key}, 'code': {'text': 'Asthma'}} for key in ('alpha', 'beta')]
    frag = parse({'resourceType': 'Bundle', 'entry': [{'resource': r} for r in resources]})
    with pytest.raises(ingestion.BundleRejected, match='multiple patients'):
        ingestion._patient_key_and_source(frag, 'chart.json', {'patient_key': 'override'})


def test_inventory_detects_content_loss_and_missing_files(tmp_path):
    sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
    import data_inventory as inv
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE forms (id TEXT PRIMARY KEY, answer TEXT)')
        conn.execute("INSERT INTO forms VALUES('same-id','complete answer')")
    blobs = tmp_path / 'blobs'; blobs.mkdir(); (blobs / 'original').write_bytes(b'full original')
    before = inv.snapshot(path, {'raw': blobs})
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE forms SET answer='truncated'")
    (blobs / 'original').write_bytes(b'truncated')
    issues = inv.compare(before, inv.snapshot(path, {'raw': blobs}))
    assert any('answer' in issue for issue in issues)
    assert any('original' in issue for issue in issues)
    with pytest.raises(ValueError, match='no database'):
        inv.snapshot(tmp_path / 'missing.db')
    assert inv.compare({'tables': {}}, before)


def test_sql_guard_rejects_destructive_data_changes():
    sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
    from data_change_guard import violations
    for sql in ['DELETE FROM ingest_cases', 'DROP TABLE signed_agreements', 'INSERT OR REPLACE INTO tasks', 'TRUNCATE media_files']:
        assert violations(sql)
    assert not violations('ALTER TABLE tasks ADD COLUMN revision TEXT')


def test_acknowledged_database_writes_use_full_synchronous(tmp_path):
    from team_store import connect_team_db
    for conn in (I._store()._conn(), connect_team_db(str(tmp_path / 'team.db'))):
        try: assert conn.execute('PRAGMA synchronous').fetchone()[0] == 2
        finally: conn.close()


def test_fhir_aliases_resolve_to_the_same_patient():
    from asclepius.adapters.fhir_r4 import parse
    entries = [{'fullUrl': 'urn:uuid:external', 'resource': {'resourceType': 'Patient', 'id': 'alpha'}}]
    entries.extend({'resource': {'resourceType': 'Condition', 'subject': {'reference': ref}, 'code': {'text': 'Asthma'}}}
                   for ref in ('urn:uuid:external', 'Patient/alpha', '#alpha', 'urn:uuid:alpha'))
    fragment = parse({'resourceType': 'Bundle', 'entry': entries})
    key, _ = ingestion._patient_key_and_source(fragment, 'chart.json', {})
    assert key == 'alpha'


def test_latest_vitals_keep_their_own_values_across_files():
    from asclepius.adapters.fhir_r4 import parse
    def fragment(value, stamp):
        return parse({'resourceType': 'Bundle', 'entry': [{'resource': {'resourceType': 'Observation',
            'category': [{'coding': [{'code': 'vital-signs'}]}], 'code': {'text': 'Heart rate'},
            'valueQuantity': {'value': value}, 'effectiveDateTime': stamp}}]})
    parts = [fragment(70, '2031-03-14T16:00:00Z'), fragment(125, '2031-03-19T16:00:00Z')]
    for ordered in (parts, list(reversed(parts))):
        merged = ingestion._merge_fragments(ordered)
        assert merged['vitals']['Heart rate'] == 125
        assert merged['_vitals_at'] == '2031-03-19T16:00:00Z'
        assert len(merged['notes']) == 2


def test_chunk_receipt_rollback_retains_parts_and_retries_once():
    from tests import test_upload_scale as U
    client = TestClient(A.app, base_url='https://testserver', raise_server_exceptions=False)
    store = I._store()
    U._portal(client, store)
    data = U._bundle()
    session = U._declare(client, data)
    for n, part in enumerate(U._parts_of(data, session['chunk_size']), 1):
        assert U._put_part(client, session['session_id'], n, part).status_code == 200
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER fail_verified BEFORE UPDATE OF status ON ingest_upload_sessions WHEN NEW.status='verified' BEGIN SELECT RAISE(ABORT,'session write failed'); END")
    url = f"/api/asclepius/hs/uploads/sessions/{session['session_id']}/complete"
    assert client.post(url).status_code == 500
    assert store.count_ingest_uploads() == 0
    row = store.get_upload_session(session['session_id'])
    assert row['status'] is None and Path(row['storage_dir']).exists()
    with store._conn() as conn:
        conn.execute('DROP TRIGGER fail_verified')
    accepted = client.post(url)
    retried = client.post(url)
    assert accepted.status_code == retried.status_code == 200
    assert accepted.json()['upload_id'] == retried.json()['upload_id']
    assert store.count_ingest_uploads() == 1


@pytest.mark.parametrize('failure', ['default', 'cleanup', 'event'])
def test_post_receipt_failure_does_not_strand_an_accepted_upload(monkeypatch, failure):
    from tests import test_upload_scale as U
    from asclepius import uploads
    client = TestClient(A.app, base_url='https://testserver', raise_server_exceptions=False)
    store = I._store()
    U._portal(client, store)
    data = U._bundle()
    session = U._declare(client, data)
    for n, part in enumerate(U._parts_of(data, session['chunk_size']), 1):
        assert U._put_part(client, session['session_id'], n, part).status_code == 200
    def fail(*a, **kw): raise OSError('injected post-commit fault')
    if failure == 'default': monkeypatch.setattr(store, 'apply_auto_generate_default', fail)
    elif failure == 'cleanup': monkeypatch.setattr(uploads, 'finalize', fail)
    else:
        original = store.log_event
        def log(**kw):
            if kw.get('event_type') == 'upload_received': fail()
            return original(**kw)
        monkeypatch.setattr(store, 'log_event', log)
    response = client.post(f"/api/asclepius/hs/uploads/sessions/{session['session_id']}/complete")
    assert response.status_code == 200
    uid = response.json()['upload_id']
    assert store.get_ingest_upload(uid)['status'] == 'ingested'
    assert store.list_ingest_cases(upload_id=uid)


def test_intake_and_notification_commit_together(monkeypatch):
    client, store, org = onboard()
    monkeypatch.setattr('notifications.founder_recipients', lambda store: ['founder@example.org'])
    from routers.asclepius_provider import _HS_INTAKE_PROMPTS
    body = {p['key']: 'Complete answer' for p in _HS_INTAKE_PROMPTS}
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER fail_intake_mail BEFORE INSERT ON admin_notify_outbox WHEN NEW.kind='hs_intake' BEGIN SELECT RAISE(ABORT,'queue unavailable'); END")
    assert client.post('/api/asclepius/hs/intake', json=body).status_code == 500
    assert not store.list_hs_intake(org['hs_id'])
    with store._conn() as conn:
        conn.execute('DROP TRIGGER fail_intake_mail')
    assert client.post('/api/asclepius/hs/intake', json=body).status_code == 200
    assert store.list_hs_intake(org['hs_id'])[0]['answers'] == body
    assert any(r['kind'] == 'hs_intake' for r in hs_mail.status(store, org['hs_id']))
