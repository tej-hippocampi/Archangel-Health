"""Forensic Medicine scope and wrong-specialty recovery (synthetic applicants)."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from asclepius import credentialing as cv, onboarding_cases as bank
from asclepius.onboarding_catalog import topic_for, scope_for
from asclepius.onboarding_specialties import canonical, resolve
from tests._asclepius import app, fresh_store, make_user, headers_for
from tests.test_onboarding_specialty_cases import seed_case, set_credentials, fixture_entry, SOURCES


@pytest.mark.parametrize('title', [
    'Forensic Medicine', 'Legal Medicine', 'Forensic Medicine / Legal Medicine',
    'Clinical Forensic Medicine', 'Forensic and Legal Medicine',
    'Legal and Forensic Medicine', 'Forensic physician', 'Rechtsmedizin',
    'Facharzt für Rechtsmedizin', 'Fachärztin für Rechtsmedizin',
    'Rechtsmediziner', 'Rechtsmedizinerin', 'Sudska medicina',
    'Specijalista sudska medicina', 'Судска медицина', 'Специјалиста судска медицина',
    'Specijalista sudske medicine', 'Специјалиста судске медицине',
])
def test_international_forensic_titles_preserve_scope(title):
    assert canonical(title) == 'forensic medicine'
    for text in (title, 'Primary specialty: ' + title):
        parsed = cv._parse_cv_text('Example Physician, MD\n' + text)
        assert parsed['specialty'] == 'forensic medicine', (text, parsed['specialty_candidates'])
        assert parsed['specialty_display'] == 'Forensic Medicine / Legal Medicine'
        assert parsed['specialty_status'] == 'resolved'


@pytest.mark.parametrize('raw,expected', [
    ('Forensic Pathologist', 'forensic pathology'), ('Forensic Pathology', 'forensic pathology'),
    ('Forensic Psychiatry', 'forensic psychiatry'), ('Forensic Geneticist', 'forensic genetics'),
    ('Anatomic Pathology', 'pathology'), ('Clinical Genetics', 'medical genetics'),
])
def test_adjacent_specialties_keep_their_scope(raw, expected):
    assert canonical(raw) == expected
    parsed = cv._parse_cv_text('Example Physician, MD\nSpecialty: ' + raw)
    assert parsed['specialty'] == expected
    assert canonical(parsed['specialty_display']) == expected


def test_representative_cv_recognizes_medicine_and_does_not_override_confirmation():
    parsed = cv._parse_cv_text('''Example Physician, MD
Profile
Physician and forensic-medicine specialist evaluating clinical, forensic, genetic and donor information.
Relevant Experience
Institute of Forensic Medicine
External Forensic Medicine Consultant
Clinical Forensic Outpatient Clinic
Physician in Forensic Medicine (Specialist from June 2020)
Conducted clinical-forensic examinations and assessed injury patterns.
Education
Master of Medical Sciences - Forensic Genetics
Specialist Qualification in Forensic Medicine
Selected Certifications
German Specialist Recognition in Forensic Medicine - Medical Association
''')
    assert parsed['specialty'] == 'forensic medicine'
    assert not any(c['specialty'] in {'pathology', 'medical genetics'} for c in parsed['specialty_candidates'])
    user = {'specialty': 'pathology', 'cv_parsed': {**parsed, 'ok': True}}
    assert resolve(user)['specialty'] == 'forensic medicine'
    user['credentials'] = {'primarySpecialty': 'Forensic Pathologist'}
    assert resolve(user)['specialty'] == 'forensic pathology'


@pytest.mark.parametrize('specialty', ['Forensic Pathology', 'Forensic Medicine'])
def test_training_keeps_forensic_qualifier(specialty):
    entries = cv._extract_training(['Example University Hospital 2015-2019', f'Residency in {specialty}'])
    assert entries and canonical(entries[0]['specialty']) == canonical(specialty)


@pytest.mark.parametrize('modality', ['pathology', ' Pathology ', 'histopathology', 'histology', 'microscopy'])
def test_medicolegal_topics_and_no_pathology_studies(modality):
    assert 'interpretation of genital lacerations' in topic_for('Legal Medicine', 'practice')
    assert 'neck compression' in topic_for('Rechtsmedizin', 'examination')
    assert 'histopathology' in scope_for('forensic medicine')
    entry = fixture_entry('forensic medicine')
    entry['case']['studies'] = [{'modality': modality, 'label': 'Microscopic skin specimen',
                               'findings': 'A histopathology interpretation'}]
    with pytest.raises(ValueError, match='outside_forensic_medicine_scope'):
        bank.validate_entry(entry, 'forensic medicine', SOURCES)


@pytest.mark.parametrize('field,text', [
    ('label', 'Microscopic skin specimen'),
    ('label', 'Pathology report'),
    ('label', 'H&E tissue section'),
    ('findings', 'H & E shows tumor cells in the specimen.'),
    ('findings', 'Microscopy demonstrates invasive nests.'),
    ('findings', 'Hematoxylin and eosin stained tissue shows atypical cells.'),
    ('impression', 'Histopathology supports invasive carcinoma.'),
    ('impression', 'The supplied tissue-slide supports the diagnosis.'),
    ('modality', 'immunohistochemistry'),
    ('findings', 'Immunohistochemistry demonstrates tumor-cell staining.'),
    ('impression', 'Cytopathology confirms squamous cell carcinoma.'),
])
def test_generic_modality_cannot_hide_tissue_study(field, text):
    entry = fixture_entry('forensic medicine')
    study = {'modality': 'clinical examination', 'label': 'Examination findings',
             'findings': 'Findings supplied for the assessment.'}
    study[field] = text
    entry['case']['studies'] = [study]
    with pytest.raises(ValueError, match='outside_forensic_medicine_scope'):
        bank.validate_entry(entry, 'forensic medicine', SOURCES)


@pytest.mark.parametrize('study', [
    {'modality': 'CT', 'label': 'Head CT report',
     'findings': 'No fracture or hemorrhage.', 'impression': 'No acute intracranial pathology.'},
    {'modality': 'forensic genetics', 'label': 'DNA comparison report',
     'findings': 'The supplied DNA profiles differ at two tested loci.'},
    {'modality': 'CT', 'label': 'Intracranial pathology assessment',
     'findings': 'No fracture or hemorrhage.'},
    {'modality': 'clinical examination', 'label': 'Pathological reflex assessment',
     'findings': 'Plantar responses are flexor bilaterally.'},
    {'modality': 'forensic genetics', 'label': 'DNA comparison report',
     'findings': 'Biopsy specimens revealed a DNA profile different from the comparator.'},
    {'modality': 'clinical examination', 'label': 'Injury documentation',
     'findings': 'Soft tissue showed bruising. The record separately reports a history of carcinoma.'},
    {'modality': 'forensic genetics', 'label': 'DNA comparison report',
     'findings': 'Biopsy revealed a DNA profile, and the record separately mentions prior carcinoma.'},
    {'modality': 'clinical examination', 'label': 'Injury documentation',
     'findings': 'Soft tissue showed atypical discoloration around the contusion.'},
])
def test_forensic_record_review_studies_remain_allowed(study):
    entry = fixture_entry('forensic medicine')
    entry['case']['studies'] = [study]
    assert bank.validate_entry(entry, 'forensic medicine', SOURCES)['case']['studies']


@pytest.mark.parametrize('field', ['label', 'findings', 'impression'])
@pytest.mark.parametrize('absent', [
    'No histopathology was performed.',
    'Histology was not performed.',
    'No microscopic examination was undertaken.',
    'Microscopy never performed.',
    'No tissue slides were provided.',
    'No biopsy sections were provided.',
    'No immunohistochemistry was performed.',
    'Cytopathology was not performed.',
    'H&E sections were not obtained.',
])
def test_explicit_absent_tissue_test_does_not_exclude_clinical_exam(field, absent):
    entry = fixture_entry('forensic medicine')
    study = {'modality': 'clinical examination', 'label': 'Injury documentation',
             'findings': 'Superficial soft-tissue bruising was documented.'}
    study[field] = 'Bruising was documented; ' + absent
    entry['case']['studies'] = [study]
    assert bank.validate_entry(entry, 'forensic medicine', SOURCES)['case']['studies']


@pytest.mark.parametrize('field', ['label', 'findings', 'impression'])
@pytest.mark.parametrize('test_name', [
    'immunohistochemical study',
    'immunohistochemical analysis',
    'immunohistochemical examination',
    'cytopathological examination',
    'cytopathological analysis',
    'cytopathologic study',
    'histologic study',
    'histopathologic examination',
])
def test_adjective_test_names_distinguish_absence_from_results(field, test_name):
    entry = fixture_entry('forensic medicine')
    study = {'modality': 'clinical examination', 'label': 'Injury documentation',
             'findings': 'Superficial bruising was documented.'}
    entry['case']['studies'] = [study]
    for absence in (f'No {test_name} was performed.', f'{test_name} was not performed.'):
        study[field] = absence
        assert bank.validate_entry(entry, 'forensic medicine', SOURCES)['case']['studies']
    for result in (
        f'{test_name} showed malignant cells.',
        f'{test_name} showed no malignant cells.',
        f'No {test_name} was performed; biopsy sections showed invasive carcinoma.',
        f'No {test_name} was performed; biopsy revealed invasive carcinoma.',
        f'No {test_name} was performed; tissue showed malignant cells.',
    ):
        study[field] = result
        with pytest.raises(ValueError, match='outside_forensic_medicine_scope'):
            bank.validate_entry(entry, 'forensic medicine', SOURCES)


@pytest.mark.parametrize('findings', [
    'Histopathology showed no malignancy.',
    'No malignant cells were seen on H&E.',
    'No histopathology was performed; microscopy showed tumor cells.',
    'No histology was performed, but H&E shows invasive nests.',
    'Histology was not performed. The tissue-slide shows atypical cells.',
    'No histology was performed; biopsy sections showed invasive carcinoma.',
    'Biopsy showed no malignancy.',
    'Tissue samples demonstrated atypical cells.',
])
def test_negative_results_or_other_tissue_evidence_remain_excluded(findings):
    entry = fixture_entry('forensic medicine')
    entry['case']['studies'] = [{'modality': 'clinical examination',
                               'label': 'Examination findings', 'findings': findings}]
    with pytest.raises(ValueError, match='outside_forensic_medicine_scope'):
        bank.validate_entry(entry, 'forensic medicine', SOURCES)


def test_paused_pathology_exam_replaced_without_consuming_attempt_or_erasing_evidence(tmp_path):
    from scripts.data_inventory import snapshot, compare
    store = fresh_store()
    old_practice = seed_case(store, 'pathology', 'practice')
    old_exam = seed_case(store, 'pathology', 'examination')
    new_practice = seed_case(store, 'forensic medicine', 'practice')
    new_exam = seed_case(store, 'forensic medicine', 'examination')
    user = make_user(store, specialty='pathology')
    store.set_verification_status(user['id'], 'pending')
    headers = headers_for(user)
    with TestClient(app) as client:
        assert client.get('/api/asclepius/tutorial/task', headers=headers).json()['task']['task_id'] == old_practice
        assert client.get('/api/asclepius/exam/task', headers=headers).json()['task']['task_id'] == old_exam
        set_credentials(store, user['id'], {'primarySpecialty': 'Forensic Medicine / Legal Medicine'})
        before = snapshot(store.db_path)
        backup = tmp_path / 'before.db'
        with sqlite3.connect(store.db_path) as source, sqlite3.connect(backup) as dest:
            source.backup(dest)
        assert compare(before, snapshot(backup)) == []
        assert client.post('/api/asclepius/tutorial/reveal', headers=headers,
            json={'task_id': old_practice, 'text': 'Saved independent assessment of the findings.'}).status_code == 409
        assert client.post('/api/asclepius/tutorial/submit', headers=headers,
            json={'task_id': old_practice, 'chosen_id': 'A', 'verdict': 'A_better'}).status_code == 400
        assert client.post('/api/asclepius/exam/submit', headers=headers, json={'task_id': old_exam}).status_code == 409
        for _ in range(2):
            assert client.get('/api/asclepius/tutorial/task', headers=headers).json()['task']['task_id'] == new_practice
            draw = client.get('/api/asclepius/exam/task', headers=headers).json()
            assert draw['task']['task_id'] == new_exam and draw['attempt'] == 1
            assert draw['task']['case']['specialty'] == 'forensic medicine'
            assert 'ground_truth' not in draw['task']['case']
        state = store.get_tutorial_state(user['id'])
        assert state['previous_exam_draws'] == [{'state': 'in_progress', 'attempt': 1,
            'task_id': old_exam, 'reason': 'specialty_corrected'}]
        assert state['previous_practice_tasks'] == [old_practice]
        assert store.list_credentialing_exams(user['id']) == []
        assert bank.get_task(store, old_exam) and bank.get_task(store, old_practice)
        assert compare(before, snapshot(store.db_path), allowed=['users.tutorial_json']) == []
        restored = tmp_path / 'restored.db'
        with sqlite3.connect(backup) as source, sqlite3.connect(restored) as dest:
            source.backup(dest)
        assert compare(before, snapshot(restored)) == []
        assert client.post('/api/asclepius/exam/submit', headers=headers, json={'task_id': new_exam}).status_code == 200
        set_credentials(store, user['id'], {'primarySpecialty': 'Forensic Pathology'})
        assert client.get('/api/asclepius/exam/task', headers=headers).json()['task']['task_id'] == new_exam
        assert len(store.list_credentialing_exams(user['id'])) == 1


@pytest.mark.parametrize('kind,endpoint', [('practice', 'tutorial'), ('examination', 'exam')])
def test_missing_reviewed_case_waits_without_serving_pathology(monkeypatch, kind, endpoint):
    store = fresh_store()
    seed_case(store, 'pathology', kind)
    user = make_user(store, specialty='forensic medicine')
    store.set_verification_status(user['id'], 'pending')
    requested = []
    def unavailable(_store, specialty, requested_kind, attempt):
        requested.append((specialty, requested_kind))
        return {'status': 'retry_wait'}
    monkeypatch.setattr(bank, 'request_case', unavailable)
    with TestClient(app) as client:
        response = client.get(f'/api/asclepius/{endpoint}/task', headers=headers_for(user))
    assert response.status_code == 503
    assert requested == [('forensic medicine', kind)]
    assert store.list_credentialing_exams(user['id']) == []
