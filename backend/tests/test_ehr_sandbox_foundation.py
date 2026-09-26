"""M0: additive persistence, realm isolation and reproducible synthetic inputs."""
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from asclepius.cases import AllergyItem, ClinicalCase, EncounterItem, MedicationEvent, OrderItem
from asclepius.ehr_sandbox.constants import EHR_TABLES, settings
from asclepius.store import reset_store_for_tests
from scripts import data_change_guard, data_inventory

FIXTURES = Path(__file__).parent / 'fixtures' / 'ehr_sandbox'


@pytest.fixture
def store(tmp_path):
    return reset_store_for_tests(str(tmp_path / 'ehr.db'))


def test_ehr_I16_schema_preserves_rows_and_inventory_covers_every_table(store):
    store.insert_task(task_id='retained-source', prompt='Complete synthetic source evidence',
                      case={'notes': [{'text': 'Immutable historical evidence.'}]})
    before = data_inventory.snapshot(store.db_path)
    store._init_schema()
    after = data_inventory.snapshot(store.db_path)
    assert data_inventory.compare(before, after) == []
    assert set(EHR_TABLES) <= after['tables'].keys()
    with store._conn() as conn:
        for table in EHR_TABLES:
            assert any(row['pk'] for row in conn.execute(f'PRAGMA table_info({table})'))


@pytest.mark.parametrize('statement', ['DELETE FROM', 'DROP TABLE', 'TRUNCATE TABLE', 'INSERT OR REPLACE INTO'])
@pytest.mark.parametrize('table', EHR_TABLES)
def test_ehr_I16_guard_blocks_destructive_sql(statement, table):
    assert data_change_guard.violations(f'{statement} {table}')


def test_ehr_schema_keeps_review_deduplication_and_independence(store):
    with store._conn() as conn:
        conn.execute("INSERT INTO ehr_reviews(review_id,scope,visit_id,trigger,dedupe_key,items_json,blind_order,created_at) "
                     "VALUES ('r1','visit','v1','key_audit','v1|key_audit','[]','a_is_doctor','synthetic')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO ehr_reviews(review_id,scope,visit_id,trigger,dedupe_key,items_json,blind_order,created_at) "
                         "VALUES ('r2','visit','v1','key_audit','v1|key_audit','[]','a_is_doctor','synthetic')")
        params = ('r1', 'u1', 'synthetic', 'synthetic', 'synthetic')
        conn.execute("INSERT INTO ehr_review_assignments(review_assignment_id,review_id,user_id,round,offered_at,due_at,expires_at) "
                     "VALUES ('a1',?,?,1,?,?,?)", params)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO ehr_review_assignments(review_assignment_id,review_id,user_id,round,offered_at,due_at,expires_at) "
                         "VALUES ('a2',?,?,2,?,?,?)", params)
        assert conn.execute('SELECT COUNT(*) FROM assignments').fetchone()[0] == 0


def test_ehr_tables_are_realm_scoped(tmp_path):
    import realm
    stores = []
    for scope in ('live', 'sandbox'):
        with realm.scoped(scope):
            stores.append(reset_store_for_tests(str(tmp_path / f'{scope}.db')))
    with stores[0]._conn() as conn:
        conn.execute("INSERT INTO ehr_source_exclusions VALUES ('upload','clinician','source_practice_clinician')")
    with stores[1]._conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM ehr_source_exclusions').fetchone()[0] == 0


@pytest.mark.parametrize('model,payload', [
    (EncounterItem, {'encounter_ref': 'enc-1'}),
    (OrderItem, {'kind': 'lab'}),
    (MedicationEvent, {'action': 'hold', 'drug': 'lisinopril'}),
    (AllergyItem, {'substance': 'penicillin'}),
])
def test_ehr_new_models_reject_unconverted_dates(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate({**payload, 'collected_at': '2024-03-05'})
    assert model.model_validate(payload).collected_offset_days is None


def test_ehr_old_cases_validate_and_new_lists_do_not_share_mutations():
    first, second = ClinicalCase(), ClinicalCase()
    first.orders.append(OrderItem(kind='lab'))
    assert not second.orders
    for field in ('encounters', 'medication_events', 'allergies'):
        assert getattr(first, field) == []


def test_ehr_additive_fields_preserve_legacy_serialization_and_explicit_values():
    legacy = ClinicalCase(notes=[{'text': 'Historical note'}], medications=[{'drug': 'losartan'}])
    dumped = legacy.model_dump()
    assert not set(legacy._ehr_defaults) & dumped.keys()
    assert 'note_id' not in dumped['notes'][0]
    assert not {'status', 'start_offset_days', 'stop_offset_days'} & dumped['medications'][0].keys()
    assert dumped['notes'][0]['collected_offset_days'] is None  # historical default stays
    assert json.loads(legacy.model_dump_json()) == dumped
    explicit = ClinicalCase(orders=[], notes=[{'note_id': None}], medications=[{'drug': 'x', 'status': None}])
    assert explicit.model_dump()['orders'] == []
    assert explicit.model_dump()['notes'][0]['note_id'] is None
    assert explicit.model_dump()['medications'][0]['status'] is None
    legacy.orders.append(OrderItem(kind='lab'))
    assert legacy.model_dump()['orders'][0]['kind'] == 'lab'


def test_ehr_synthetic_fixtures_have_expected_patients_visits_and_formats():
    from pypdf import PdfReader
    manifest = json.loads((FIXTURES / 'fixture_manifest.json').read_text())
    assert manifest['synthetic'] is True
    assert len(manifest['patients']) == 12
    counts = {}
    with zipfile.ZipFile(FIXTURES / 'mixed_patients.zip') as bundle:
        mapping = json.loads(bundle.read('manifest.json'))
        assert 'patient_key' not in mapping
        assert set(mapping['files']) == set(bundle.namelist()) - {'manifest.json'}
        assert len(set(mapping['files'].values())) == 12
        for p in manifest['patients']:
            counts[p['primary_format']] = counts.get(p['primary_format'], 0) + 1
            c = ClinicalCase.model_validate_json((FIXTURES / 'cases' / f"{p['label']}.json").read_text())
            assert 3 <= len(c.encounters) == len(c.notes) == p['visits'] <= 8
            assert len(set(n.collected_offset_days for n in c.notes)) == len(c.notes)
            if 'pdf' in p['primary_format']:
                pdf = next(f for f in p['files'] if f.endswith('.pdf'))
                pages = PdfReader(io.BytesIO(bundle.read(pdf))).pages
                assert len(pages) == p['visits']
                if p['primary_format'] == 'scanned_pdf':
                    assert all(not page.extract_text().strip() for page in pages)
                    assert all(page.images for page in pages)
                else:
                    assert all('DOS:' in page.extract_text() and 'Assessment:' in page.extract_text() for page in pages)
    assert counts == {'ccda': 4, 'text_pdf': 4, 'scanned_pdf': 2, 'csv_text': 2}
    with zipfile.ZipFile(FIXTURES / 'unmapped_patients.zip') as bundle:
        assert 'manifest.json' not in bundle.namelist()
        assert len(bundle.namelist()) == 6
        assert all('__' not in p for p in bundle.namelist())


def test_ehr_fixture_archive_hashes_are_declared():
    import hashlib
    manifest = json.loads((FIXTURES / 'fixture_manifest.json').read_text())
    for name, expected in manifest['archives'].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == expected


def test_ehr_csv_values_agree_with_companion_cases():
    import csv
    with zipfile.ZipFile(FIXTURES / 'mixed_patients.zip') as archive:
        for name in archive.namelist():
            if name.endswith('.csv'):
                case = json.loads((FIXTURES / 'cases' / (name.split('__')[0] + '.json')).read_text())
                values = [r['value'] for p in case['lab_panels'] for r in p['results'] if r['loinc'] == '2160-0']
                rows = csv.DictReader(io.StringIO(archive.read(name).decode()))
                assert [float(r['value']) for r in rows if r['loinc'] == '2160-0'] == values


@pytest.mark.parametrize('name,value', [('KEY_MIN_CONFIDENCE', 'nan'), ('RUBRIC_SAMPLE_RATE', '1.1'),
                                      ('BUDGET_TOOL_CALLS', '0'), ('CHECKPOINT_WEIGHTS', '0.1,0.2,0.3,0.5'),
                                      ('EXPORT_BLOCKED_JURISDICTIONS', ''), ('OCR_ENABLED', 'maybe')])
def test_ehr_invalid_configuration_fails_explicitly(monkeypatch, name, value):
    monkeypatch.setenv('EHR_' + name, value)
    with pytest.raises(ValueError):
        settings()


def test_ehr_configuration_overrides_are_not_cached(monkeypatch):
    monkeypatch.setenv('EHR_BUDGET_TOOL_CALLS', '17')
    assert settings().budget_tool_calls == 17
    monkeypatch.setenv('EHR_BUDGET_TOOL_CALLS', '29')
    assert settings().budget_tool_calls == 29


def test_ehr_test_file_is_in_exactly_one_ci_shard():
    from scripts.ci_shard import assign, discover
    root = Path(__file__).resolve().parents[1]
    filename = 'tests/test_ehr_sandbox_foundation.py'
    assert sum(filename in shard for shard in assign(discover(str(root)), 4)) == 1
