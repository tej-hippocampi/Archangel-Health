"""Mixed-patient front-door ingestion and fail-closed adapter regressions."""
import io
import json
import zipfile
from pathlib import Path

import pytest

from asclepius.adapters import ccda, pdf_doc
from asclepius.adapters.ehr_mapping import PatientUnmapped
from asclepius.case_formats import CaseIngestError, deidentify, ingest_real_deid
from asclepius.ingestion import _classify, _merge_fragments, _patient_key_and_source
from asclepius.timeline import normalize_timeline
from tests import _asclepius as A
from tests import test_asclepius_ingestion as I

ROOT = Path(__file__).parent / 'fixtures' / 'ehr_sandbox'


@pytest.mark.parametrize('text', ['Patient: male age band 60-69', 'Physician Fresh Orders', 'Physician Note\nTime',
                                  'Repeat in 12 hrs\n- CT', 'Day 0 contrast-enhanced CT',
                                  'A value of 1.0 contrast-enhanced CT'])
def test_ehr_privacy_patterns_do_not_join_clinical_headers_or_decimal_values(text):
    from asclepius.validation import residual_identifiers
    assert not residual_identifiers(text)


@pytest.mark.parametrize('text,kind', [('Patient name: Alice Example', 'name'),
    ('Physician name: Alice Example', 'name'), ('Seen by Dr. Alice Example', 'name'),
    ('Address: 123 Privacy Street', 'address')])
def test_ehr_privacy_patterns_still_detect_explicit_identifiers(text, kind):
    from asclepius.validation import residual_identifiers
    assert kind in residual_identifiers(text)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv('ASCLEPIUS_INGEST_DIR', str(tmp_path / 'ingest'))
    monkeypatch.setenv('ASCLEPIUS_ASSET_STORE', str(tmp_path / 'assets'))
    monkeypatch.setenv('EHR_OCR_ENABLED', '0')
    monkeypatch.setenv('ENV', 'test')


def fixture_file(name):
    with zipfile.ZipFile(ROOT / 'mixed_patients.zip') as z:
        return z.read(name), {**json.loads(z.read('manifest.json')), 'filename': name}


def test_ehr_ccda_extracts_structure_and_opaque_identity():
    raw, _ = fixture_file('p01__chart.xml')
    parsed = ccda.parse(raw)
    assert len(parsed['notes']) == 3 and len(parsed['encounters']) == 3
    assert len(parsed['lab_panels']) == 3
    assert parsed['medications'][0]['status'] == 'active'
    assert parsed['medications'][0]['started_at'] == '20230101'
    assert parsed['_patient_keys'][0].startswith('ccda-')
    assert 'fixture-1' not in json.dumps(parsed)
    result = ingest_real_deid(raw, 'ccda')
    assert isinstance(result['medications'][0]['start_offset_days'], int)
    assert '20230101' not in json.dumps(result)


def test_ehr_ccda_template_oid_overrides_unrecognized_code():
    raw, _ = fixture_file('p01__chart.xml')
    raw = raw.replace(b'<code code="30954-2"/>', b'<templateId root="2.16.840.1.113883.10.20.22.2.3.1"/><code code="unknown"/>')
    assert len(ccda.parse(raw)['lab_panels']) == 3


@pytest.mark.parametrize('raw', [b'<broken', b'<ClinicalDocument/>',
    b'<!DOCTYPE ClinicalDocument [<!ENTITY x SYSTEM "file:///etc/passwd">]><ClinicalDocument xmlns="urn:hl7-org:v3">&x;</ClinicalDocument>'])
def test_ehr_ccda_malformed_and_entities_rejected(raw):
    with pytest.raises(ValueError):
        ccda.parse(raw)


def test_ehr_ccda_identifiers_dropped_before_narrative_extraction():
    raw, _ = fixture_file('p01__chart.xml')
    raw = raw.replace(b'<patient>', b'<addr>123 Privacy Street</addr><telecom value="tel:415-555-0199"/><patient><name>Dr. Canary Identifier</name>')
    text = json.dumps(ccda.parse(raw))
    assert all(token not in text for token in ('Canary', '123 Privacy', '415-555', 'fixture-1'))


def test_ehr_pdf_splits_dos_and_holds_unreadable_scans(monkeypatch):
    raw, manifest = fixture_file('p05__worksheets.pdf')
    frag = pdf_doc.parse(raw, manifest=manifest)
    assert len(frag['notes']) == 7
    assert len({n['collected_at'] for n in frag['notes']}) == 7
    assert all(n['note_type'] == 'Visit worksheet' for n in frag['notes'])
    assert pdf_doc.service_date('Service date: unknown-date') is None
    assert pdf_doc.service_date('DOS: 2024-13-44') is None
    raw, manifest = fixture_file('p09__worksheets.pdf')
    with pytest.raises(pdf_doc.PdfParseError, match='ocr_unavailable'):
        pdf_doc.parse(raw, manifest=manifest)


def test_ehr_pdf_ocr_path_uses_text_without_persisting_images(monkeypatch):
    raw, manifest = fixture_file('p09__worksheets.pdf')
    # Exercise the adapter path without requiring tesseract in the default suite.
    calls = []
    def ocr(page):
        calls.append(page)
        return f'DOS: 2024-0{len(calls)}-15\nAssessment: CKD stage 4. Plan: Continue current medications. Follow up in 28 days.'
    monkeypatch.setattr(pdf_doc, '_ocr_page', ocr)
    result = pdf_doc.parse(raw, manifest=manifest)
    assert len(calls) == len(result['notes']) == 5
    assert 'studies' not in result and 'asset' not in json.dumps(result)


def test_ehr_patient_mapping_applies_to_all_formats_and_cannot_merge_multiple_patients():
    manifest = {'files': {'labs.csv': 'a', 'worksheet.pdf': 'a'}}
    assert _patient_key_and_source({'_patient_keys': ['csv-id']}, 'labs.csv', manifest) == ('manifest-a', 'manifest')
    assert _patient_key_and_source({'_patient_keys': ['manifest-a']}, 'worksheet.pdf', manifest) == ('manifest-a', 'manifest')
    with pytest.raises(ValueError):
        _patient_key_and_source({'_patient_keys': ['one', 'two']}, 'labs.csv', manifest)
    with pytest.raises(PatientUnmapped):
        _patient_key_and_source({}, 'unmapped.txt', manifest)
    with pytest.raises(PatientUnmapped):
        _patient_key_and_source({}, 'labs.csv', {**manifest, 'patient_key': 'global'})


def test_ehr_new_collections_survive_merge_timeline_and_deid():
    frag = {'notes': [{'text': 'Visit worksheet', 'collected_at': '2024-01-10'}],
            'encounters': [{'encounter_ref': 'enc-1', 'start': '2024-01-10'}],
            'orders': [{'kind': 'lab', 'text': 'Repeat BMP 2024-01-17', 'authored_on': '2024-01-10', 'due_at': '2024-01-17'}],
            'medication_events': [{'action': 'hold', 'drug': 'lisinopril', 'authored_on': '2024-01-10', 'reason': 'Reviewed 2024-01-10'}],
            'allergies': [{'substance': 'penicillin', 'recorded_at': '2024-01-02'}],
            'medications': [{'drug': 'lisinopril', 'started_at': '2024-01-01', 'stopped_at': '2024-01-10'}]}
    merged = _merge_fragments([frag])
    normalized, report = normalize_timeline(merged, index_event='2024-01-10')
    safe = deidentify(normalized)
    assert not report['unresolved']
    assert safe['notes'][0]['note_id'] == 'note-0'
    assert safe['encounters'][0]['collected_offset_days'] == 0
    assert safe['orders'][0]['due_offset_days'] == 7
    assert safe['allergies'][0]['collected_offset_days'] == -8
    assert safe['medications'][0]['start_offset_days'] == -9
    assert safe['medications'][0]['stop_offset_days'] == 0
    assert safe['medications'][0]['collected_offset_days'] == -9
    assert '2024-' not in json.dumps(safe)


@pytest.mark.parametrize('collection,field,payload', [
    ('orders', 'source_span', {'kind': 'lab'}),
    ('medication_events', 'reason', {'action': 'hold', 'drug': 'lisinopril'}),
    ('allergies', 'reaction', {'substance': 'penicillin'}),
])
def test_ehr_I7_new_fields_scanned_for_phi(collection, field, payload):
    with pytest.raises(CaseIngestError):
        deidentify({collection: [{**payload, field: 'MRN: 9988776655 phone 415-555-0199'}]})


def test_ehr_mixed_zip_front_door_has_twelve_patients_and_blocking_scan_review():
    admin = I._admin_h()
    link = I._mint(admin)
    result = I._upload(link['token'], (ROOT / 'mixed_patients.zip').read_bytes())
    store = I._store()
    upload = store.get_ingest_upload(result['upload_id'])
    cases = store.list_ingest_cases(upload_id=result['upload_id'])
    assert len(cases) == len({c['patient_key'] for c in cases}) == 12
    assert all(c['status'] == 'needs_review' for c in cases), [(c['status'], c.get('report')) for c in cases]
    assert upload['retain_raw']
    assert all(c['case'].get('notes') or c['case'].get('lab_panels') for c in cases)
    assert all('2024-' not in json.dumps(c['case']) for c in cases)


def test_ehr_no_manifest_does_not_merge_three_patients():
    link = I._mint(I._admin_h())
    result = I._upload(link['token'], (ROOT / 'unmapped_patients.zip').read_bytes())
    cases = I._store().list_ingest_cases(upload_id=result['upload_id'])
    assert len(cases) == len({c['patient_key'] for c in cases}) == 3
    assert all(c['status'] == 'needs_review' for c in cases)


def test_ehr_long_xml_head_detected_through_front_door():
    raw, _ = fixture_file('p01__chart.xml')
    raw = raw.replace(b'<?xml version="1.0"?>', b'<?xml version="1.0"?>' + b' ' * 1000)
    link = I._mint(I._admin_h())
    result = I._upload(link['token'], I._zip({'chart.xml': raw}))
    cases = I._store().list_ingest_cases(upload_id=result['upload_id'])
    assert len(cases) == 1 and cases[0]['status'] == 'ingested'


def test_ehr_empty_ccda_is_incomplete_not_silently_parsed():
    raw = b'<ClinicalDocument xmlns="urn:hl7-org:v3"><recordTarget><patientRole><id root="synthetic" extension="1"/></patientRole></recordTarget></ClinicalDocument>'
    link = I._mint(I._admin_h())
    result = I._upload(link['token'], I._zip({'empty.xml': raw, 'labs.csv': I._CSV}))
    cases = I._store().list_ingest_cases(upload_id=result['upload_id'])
    assert len(cases) == 1 and cases[0]['status'] == 'needs_review'
    with pytest.raises(CaseIngestError, match='could not parse'):
        ingest_real_deid(raw, 'ccda')


def test_ehr_merge_remaps_encounters_and_note_references_together():
    fragment = {'notes': [{'note_id': 'local-note', 'text': 'visit'}],
                'encounters': [{'encounter_ref': 'enc-000', 'note_ids': ['local-note']}],
                'orders': [{'kind': 'lab', 'encounter_ref': 'enc-000'}]}
    result = _merge_fragments([fragment, fragment])
    assert [x['encounter_ref'] for x in result['encounters']] == ['enc-000', 'enc-001']
    assert [x['encounter_ref'] for x in result['orders']] == ['enc-000', 'enc-001']
    assert [x['note_ids'] for x in result['encounters']] == [['note-0'], ['note-1']]
    assert fragment['notes'][0]['note_id'] == 'local-note'


def test_ehr_I1_explicit_unknown_pdf_date_never_inherits_prior_visit(monkeypatch):
    raw, manifest = fixture_file('p09__worksheets.pdf')
    texts = iter(['DOS: 2024-01-15\nAssessment: Stable. Plan: Continue medication unchanged for now.',
                  'DOS: unknown-date\nAssessment: Later decision without known timing. Plan: Stop drug.',
                  'DOS: 2024-03-15\nAssessment: Stable. Plan: Continue medication unchanged for now.',
                  'DOS: 2024-04-15\nAssessment: Stable. Plan: Continue medication unchanged for now.',
                  'DOS: 2024-05-15\nAssessment: Stable. Plan: Continue medication unchanged for now.'])
    monkeypatch.setattr(pdf_doc, '_ocr_page', lambda page: next(texts))
    parsed = pdf_doc.parse(raw, manifest=manifest)
    normalized, _ = normalize_timeline(parsed)
    assert len(normalized['notes']) == 5
    assert normalized['notes'][1].get('collected_offset_days') is None


def test_ehr_ocr_fallback_preserves_original_and_closes_images(monkeypatch):
    import pytesseract
    import pdf2image
    from PIL import Image
    raw,manifest=fixture_file('p09__worksheets.pdf')
    original=bytes(raw);images=[]
    monkeypatch.setenv('EHR_OCR_ENABLED','1')
    monkeypatch.setattr(pdf_doc,'ocrmypdf',None)
    monkeypatch.setattr(pdf_doc.shutil,'which',lambda name:'/synthetic/'+name)
    def render(*args,**kwargs):
        assert kwargs['first_page']==kwargs['last_page']==1 and kwargs['timeout']==60
        image=Image.new('L',(10,10));images.append(image);return [image]
    monkeypatch.setattr(pdf2image,'convert_from_path',render)
    monkeypatch.setattr(pytesseract,'image_to_string',lambda image,timeout:'DOS: 2024-01-15\nAssessment: CKD stage4. Plan: Continue medications. Follow up in 28 days.')
    result=pdf_doc.parse(raw,manifest=manifest)
    assert raw==original and len(images)==5 and result['notes']
    for image in images:
        with pytest.raises(ValueError):image.getpixel((0,0))


def test_ehr_real_ocr_preserves_five_synthetic_visit_dates(monkeypatch):
    import os,re
    if os.getenv('EHR_REAL_OCR_TEST')!='1':pytest.skip('real OCR qualification runs in the manual CI smoke')
    monkeypatch.setenv('EHR_OCR_ENABLED','1');monkeypatch.setattr(pdf_doc,'ocrmypdf',None)
    raw,manifest=fixture_file('p09__worksheets.pdf')
    notes=pdf_doc.parse(raw,manifest=manifest)['notes']
    assert len(notes)==5 and len({n['collected_at'] for n in notes})==5
    assert all(n['collected_at'] and n['note_type']=='Visit worksheet' for n in notes)
    assert all(re.search(r'follow\s*up\s*in\s*28\s*days',n['text'],re.I) for n in notes)
    assert all('current medications' in n['text'].lower() for n in notes)

@pytest.mark.parametrize('header',['DOS 2024-01-15','DOS:2024-01-15','Service date 2024-01-15'])
def test_ehr_ocr_date_headers_survive_missing_colon(header):
    assert pdf_doc.service_date(header)=='2024-01-15'
    assert pdf_doc.service_date('Dosage 2024-01-15') is None

@pytest.mark.parametrize('use_ocrmypdf',[False,True])
@pytest.mark.parametrize('bounds',[[612,0,0,792],[0,0,0,792],[0,0,100000,100000]])
def test_ocr_rejects_unsafe_page_bounds_before_render(monkeypatch,use_ocrmypdf,bounds):
    from types import SimpleNamespace
    from pypdf import PageObject
    from pypdf.generic import RectangleObject
    import pdf2image
    page=PageObject.create_blank_page(width=612,height=792);page.mediabox=RectangleObject(bounds)
    monkeypatch.setenv('EHR_OCR_ENABLED','1')
    monkeypatch.setattr(pdf_doc.shutil,'which',lambda name:'/synthetic/'+name)
    rendered=[]
    def render(*args,**kwargs):rendered.append(True);raise AssertionError('unsafe render')
    monkeypatch.setattr(pdf_doc,'ocrmypdf',SimpleNamespace(ocr=render) if use_ocrmypdf else None)
    monkeypatch.setattr(pdf2image,'convert_from_path',render)
    with pytest.raises(pdf_doc.PdfParseError,match='ocr_failed'):pdf_doc._ocr_page(page)
    assert not rendered
