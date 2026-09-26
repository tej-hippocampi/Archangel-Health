"""Canonical charts, extraction integrity and fail-closed sealed-key storage."""
import asyncio
import base64
import copy
import json
from pathlib import Path
import pytest
from asclepius.store import reset_store_for_tests
from asclepius.ehr_sandbox import chart_builder as charts, worksheet_extract as extraction
from asclepius.ehr_sandbox.common import validate
from asclepius.ehr_sandbox.sealed import seal, unseal
ROOT = Path(__file__).parent/'fixtures'/'ehr_sandbox'/'cases'

@pytest.fixture
def store(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_ENCRYPTION_KEY',base64.b64encode(b'k'*32).decode())
    return reset_store_for_tests(str(tmp_path/'charts.db'))

def seed(store,case,status='ingested'):
    return store.insert_ingest_case(upload_id='synthetic-upload',patient_key=case['case_id'],specialty='nephrology',case=case,status=status,report={})['ingest_case_id']

@pytest.mark.parametrize('path',sorted(ROOT.glob('*.json')),ids=lambda p:p.stem)
def test_ehr_chart_validity_and_relative_time(path):
    case=json.loads(path.read_text())
    resources,visits=charts.resources_from_case(case,'synthetic-patient')
    assert len(visits)==len(case['notes'])
    for r in resources:
        validate(r)
        assert not any(k in r for k in ('date','authoredOn','birthDate','effectiveDateTime'))
    assert {'Patient','Observation','MedicationRequest','Condition','DocumentReference','Encounter','AllergyIntolerance','Appointment'} <= {r['resourceType'] for r in resources}

def test_ehr_chart_build_seals_keys_and_unchanged_rebuild_is_noop(store,monkeypatch):
    case=json.loads((ROOT/'p01.json').read_text()); cid=seed(store,case)
    result=asyncio.run(charts.build_chart(cid,store=store))
    assert result['status']=='built'
    report=json.loads(result['extraction_json']); key=unseal(report['visits'][1]['key_enc'])
    assert len(key['med_changes'])==1
    assert key['orders'][0]['loinc_group']=='BMP'
    assert 'Decrease Prinivil' not in result['extraction_json']
    async def unexpected(*args,**kwargs):
        raise AssertionError('unchanged rebuild called the extractor')
    monkeypatch.setattr(extraction,'extract',unexpected)
    assert asyncio.run(charts.build_chart(cid,store=store))==result
    assert len(store.ehr_list('ehr_charts'))==1

def test_ehr_I2_no_plaintext_key_fallback(monkeypatch):
    monkeypatch.delenv('DATA_ENCRYPTION_KEY',raising=False); monkeypatch.delenv('DATA_ENCRYPTION_KEY_RING',raising=False)
    with pytest.raises(ValueError,match='configured'): seal({'answer':'sealed'})
    with pytest.raises(ValueError,match='encrypted'): unseal('{"answer":"plaintext"}')

def test_ehr_extraction_drops_invented_span():
    note=json.loads((ROOT/'p01.json').read_text())['notes'][1]['text']; key=asyncio.run(extraction.extract(note))
    candidate={k:copy.deepcopy(key[k]) for k in extraction.SCHEMA['required']}
    candidate['orders'][0]['source_span']='A fabricated passage that never existed'
    result=extraction.grounded_key(candidate,note)
    assert not result['orders'] and result['extraction']['dropped'][0]['reason']=='span_mismatch'

def test_ehr_medication_reconciliation_flags_conflicting_structure():
    case=json.loads((ROOT/'p01.json').read_text()); key=asyncio.run(extraction.extract(case['notes'][1]['text']))
    case['medication_events'][0]['action']='increase'
    assert charts.reconcile_medications(key,case,-100)[1]

def test_ehr_I7_chart_boundary_rescans_and_withholds_resources(store):
    case=json.loads((ROOT/'p01.json').read_text())
    case['notes'][0]['text']+='\nName: Jane Smith\nAddress: 123 Main Street, Boston, MA 02110'
    result=asyncio.run(charts.build_chart(seed(store,case),store=store))
    assert result['status']=='quarantined' and result['resources_json']=='[]'
    assert 'Jane Smith' not in json.dumps(result)

def test_ehr_chart_refuses_unresolved_ingestion(store):
    case=json.loads((ROOT/'p01.json').read_text())
    with pytest.raises(ValueError,match='ingested'): asyncio.run(charts.build_chart(seed(store,case,'needs_review'),store=store))

def test_ehr_I13_sealed_evidence_cannot_be_overwritten(store):
    with pytest.raises(ValueError,match='immutable'): store.ehr_update('ehr_visits',{'visit_id':'v1'},{'key_enc':'replacement'})
    with pytest.raises(ValueError,match='immutable'): store.ehr_update('ehr_key_corrections',{'correction_id':'c1'},{'correction_json':'{}'})


def test_medication_event_is_historical_regimen():
    from asclepius.ehr_sandbox.common import ext
    case=json.loads((ROOT/'p01.json').read_text()); resources,_=charts.resources_from_case(case,'patient')
    meds=[r for r in resources if r['resourceType']=='MedicationRequest']
    assert len(meds)==3 and any(ext(r)==-100 and '10 mg' in r['dosageInstruction'][0]['text'] for r in meds)
    assert any(r.get('priorPrescription') for r in meds)


def test_dose_conflict_not_only_direction():
    case=json.loads((ROOT/'p01.json').read_text()); key=asyncio.run(extraction.extract(case['notes'][1]['text']))
    case['medication_events'][0]['dose']='5 mg'
    assert charts.reconcile_medications(key,case,-100)[1]


def test_rebuild_always_rechecks_privacy_and_quarantine_retry(store,monkeypatch):
    case=json.loads((ROOT/'p01.json').read_text()); cid=seed(store,case)
    result=asyncio.run(charts.build_chart(cid,store=store))
    monkeypatch.setattr(charts,'verify_deid',lambda _: {'status':'fail','findings':[{'kind':'name'}]})
    assert asyncio.run(charts.build_chart(cid,store=store))['status']=='quarantined'
    assert asyncio.run(charts.build_chart(cid,store=store))['status']=='quarantined'
    assert store.ehr_get('ehr_charts',chart_id=result['chart_id'])['status']=='quarantined'


def test_extracted_findings_in_history(store):
    case=json.loads((ROOT/'p01.json').read_text()); result=asyncio.run(charts.build_chart(seed(store,case),store=store))
    resources=json.loads(result['resources_json'])
    assert any(r.get('code',{}).get('text')=='Hypertension' for r in resources)


def test_combination_drug_retains_all_text():
    from asclepius.ehr_sandbox.terminology import drug
    result=drug('lisinopril-hydrochlorothiazide')
    assert not result['mapped'] and 'hydrochlorothiazide' in result['name']


def test_malformed_extraction_rejected():
    import jsonschema
    note=json.loads((ROOT/'p01.json').read_text())['notes'][1]['text']; key=asyncio.run(extraction.extract(note))
    candidate={k:copy.deepcopy(key[k]) for k in extraction.SCHEMA['required']}
    candidate['med_changes'][0].pop('drug')
    with pytest.raises(jsonschema.ValidationError): extraction.grounded_key(candidate,note)
