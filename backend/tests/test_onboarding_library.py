"""Release coverage, immutable routing, blinded pixels and failure gates."""
import asyncio
import copy
import hashlib
import io
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from asclepius import onboarding_cases as bank, onboarding_library as library, onboarding_media as media
from asclepius.onboarding_catalog import CURRICULUM, SPECIALTIES, SEARCH_TERMS, topic_for
from asclepius.onboarding_specialties import canonical, match, resolve
from tests._asclepius import app, fresh_store, make_user, headers_for
from tests.test_onboarding_specialty_cases import fixture_entry, seed_case, SOURCES, approved_review, set_credentials


def test_recovery_matrix_targets_only_requested_unique_specialties():
    from scripts.build_onboarding_library import matrix_specialties
    assert matrix_specialties('nephrology, pathology,dermatology') == ['pathology', 'dermatology', 'nephrology']
    all_specialties = matrix_specialties('all')
    assert len(all_specialties) == 43 and set(all_specialties) == set(SPECIALTIES)
    for invalid in ('', 'pathology,', 'unrecognized', 'pathology,pathology', 'all,pathology'):
        with pytest.raises(ValueError):
            matrix_specialties(invalid)


@pytest.mark.parametrize('row', CURRICULUM)
def test_every_requested_specialty_has_a_distinct_pair_and_routes_confirmed_cv(row):
    name, practice, exam = row
    specialty = canonical(name)
    assert match(name) == specialty
    assert specialty in SPECIALTIES
    assert practice != exam
    assert SEARCH_TERMS[practice] != SEARCH_TERMS[exam]
    assert resolve({'credentials_json': {'primarySpecialty': name}, 'specialty': 'nephrology'})['specialty'] == specialty
    assert resolve({'cv_parsed_json': {'specialty_display': name}})['specialty'] == specialty
    store = fresh_store()
    p = seed_case(store, specialty, 'practice')
    e = seed_case(store, specialty, 'examination')
    user = make_user(store, specialty=None, tier=None)
    store.set_verification_status(user['id'], 'pending')
    set_credentials(store, user['id'], {'primarySpecialty': name})
    headers = headers_for(user)
    with TestClient(app) as client:
        assert client.get('/api/asclepius/tutorial/task', headers=headers).json()['task']['task_id'] == p
        assert client.get('/api/asclepius/exam/task', headers=headers).json()['task']['task_id'] == e
    assert p != e
    assert store.get_task(p) is None and store.get_task(e) is None


def publication(specialty='dermatology', kind='practice'):
    entry = bank.validate_entry(fixture_entry(specialty, kind), specialty, SOURCES)
    asset = None
    if specialty == 'pathology':
        asset = media.reference(kind)['asset']
        entry['case']['study_findings_policy'] = 'hidden'
        entry['case']['studies'] = [{'modality': 'pathology', 'label': 'H&E tissue section',
                                    'findings': 'HELD OUT INTERPRETATION', 'asset': asset}]
        entry = bank.validate_entry(entry, specialty, SOURCES, approved_asset=asset)
    reviews = []
    for provider in ('anthropic', 'openai'):
        reviews.append({'provider': provider, 'model': provider + '-test',
            'blind_solution': {'best_answer_id': 'A', 'confidence': .95, 'rationale': 'Fixture only'},
            'image_sha256': asset['sha256'] if asset else None,
            'review': {**approved_review(entry), 'image_supports_key': True,
                       'image_has_no_identifiers': True, 'image_observations': 'Fixture pixel review'}})
    return {'specialty': specialty, 'kind': kind, 'slot': 1, 'task_id': bank.task_id(specialty, kind),
            'entry': entry, 'validation': {'version': bank.VERSION, 'method': 'two_provider_evidence_review',
            'source_quote_review': True,
            'sources': SOURCES, 'reviews': reviews,
            'entry_sha256': hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()}}


def install_fixture(monkeypatch, tmp_path, doc):
    monkeypatch.setattr(library, 'ROOT', tmp_path)
    (tmp_path / (doc['task_id'] + '.json')).write_text(json.dumps(doc))


def test_bundled_case_is_ready_offline_without_writes_and_preserves_existing_rows(monkeypatch, tmp_path):
    from scripts.data_inventory import snapshot, compare
    from asclepius.gold_cases import load_gold_cases
    store = fresh_store()
    load_gold_cases(store, specialty='cardiology')
    doc = publication()
    install_fixture(monkeypatch, tmp_path, doc)
    before = snapshot(store.db_path)
    assert bank.request_case(store, 'dermatology', 'practice')['status'] == 'ready'
    assert compare(before, snapshot(store.db_path)) == []
    ident = seed_case(store)
    stored = bank.row_for(store, ident)['entry_json']
    assert stored == bank.row_for(store, ident)['entry_json']
    assert bank.row_for(store, ident)['validation_json'] == '{"fixture": true}'


@pytest.mark.parametrize('damage', ['fake', 'hash', 'provider', 'blind', 'pixels'])
def test_reject_unreviewed_changed_or_unseen_material(damage):
    doc = publication('pathology')
    if damage == 'fake': doc['validation']['method'] = 'fake_fixture_only'
    if damage == 'hash': doc['entry']['question'] += ' Changed after review.'
    if damage == 'provider': doc['validation']['reviews'][1]['provider'] = 'anthropic'
    if damage == 'blind': doc['validation']['reviews'][0]['blind_solution']['best_answer_id'] = 'B'
    if damage == 'pixels': doc['validation']['reviews'][0]['image_sha256'] = 'wrong'
    with pytest.raises(ValueError): library.validate(doc)


def test_misnamed_release_case_cannot_claim_another_specialty_or_kind(monkeypatch, tmp_path):
    doc = publication()
    install_fixture(monkeypatch, tmp_path, doc)
    wrong = bank.task_id('neurology', 'examination')
    (tmp_path / (wrong + '.json')).write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='requested case identity'):
        library.row_for(wrong)


@pytest.mark.parametrize('field', ['key', 'key_data', 'answer', 'rationale'])
def test_author_cannot_smuggle_extra_answer_fields_into_blind_review(field):
    entry = fixture_entry()
    entry[field] = 'LEAKED ANSWER KEY'
    entry['candidate_answers'][0]['private_key'] = 'LEAKED ANSWER KEY'
    assert 'LEAKED ANSWER KEY' not in json.dumps(bank.blind_entry(entry))
    with pytest.raises(ValueError, match='unexpected_entry_fields'):
        bank.validate_entry(entry, 'dermatology', SOURCES)
    entry.pop(field)
    with pytest.raises(ValueError, match='invalid_candidates'):
        bank.validate_entry(entry, 'dermatology', SOURCES)


@pytest.mark.parametrize('specialty,band,scope', [
    ('nephrology', 'neonate 0-28 days', 'adult'),
    ('cardiology', '2-5', 'adult'), ('geriatrics', '40-49', 'older_adult'),
    ('pediatrics', '60-69', 'pediatric'), ('nephrology', '18-24 months', 'adult')])
def test_patient_age_must_match_the_specialty_curriculum(specialty, band, scope):
    entry = fixture_entry(specialty)
    entry['case']['demographics']['age_band'] = band
    with pytest.raises(ValueError, match='age_outside_curriculum_scope'):
        bank.validate_entry(entry, specialty, SOURCES, age_scope=scope)


@pytest.mark.parametrize('band', ['0-28 days', '2-5 weeks', '18-24 months', '10-14 years'])
def test_pediatric_age_units_are_accepted(band):
    entry = fixture_entry('pediatrics')
    entry['case']['demographics']['age_band'] = band
    assert bank.validate_entry(entry, 'pediatrics', SOURCES, age_scope='pediatric')


def test_pathology_prelabel_receives_visible_pixels_without_held_out_key(monkeypatch, tmp_path):
    import base64
    from ai import llm_client
    from asclepius.critic import run_prelabel
    doc = publication('pathology')
    install_fixture(monkeypatch, tmp_path, doc)
    task = bank.get_task(fresh_store(), doc['task_id'])
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps({
            'suggested_weaker': 'B', 'confidence': .95, 'suggested_error_tags': [],
            'suggested_rationale': 'Fixture only', 'error_spans': []}))]), {'model': 'fixture'}

    monkeypatch.setattr(llm_client, 'call_llm', call)
    assert asyncio.run(run_prelabel(task))['skipped'] is False
    blocks = calls[0]['messages'][0]['content']
    assert base64.b64decode(blocks[1]['source']['data']) == media.load(media.reference('practice')['asset'])
    assert 'HELD OUT INTERPRETATION' not in blocks[0]['text']
    assert 'ground_truth' not in blocks[0]['text']


def test_real_smoke_bypasses_release_cache(monkeypatch, tmp_path):
    from scripts.smoke_onboarding_cases import prepare
    doc = publication()
    install_fixture(monkeypatch, tmp_path, doc)
    called = []
    async def build(*args):
        called.append(True)
        return doc['entry'], doc['validation']
    monkeypatch.setattr(bank, 'build_case', build)
    store = fresh_store()
    asyncio.run(prepare(store, bank, 'dermatology', 'practice'))
    assert called == [True]
    assert library.USE_BUNDLED.get() is True


@pytest.mark.parametrize('kind', ['practice', 'examination'])
def test_pathology_image_is_authorized_blinded_and_metadata_free(monkeypatch, tmp_path, kind):
    doc = publication('pathology', kind)
    install_fixture(monkeypatch, tmp_path, doc)
    store = fresh_store()
    user = make_user(store, specialty='pathology', tier=None)
    store.set_verification_status(user['id'], 'pending')
    other = make_user(store, specialty='cardiology')
    headers = headers_for(user)
    asset = media.reference(kind)['asset']
    url = '/api/asclepius/assets/' + asset['asset_id']
    with TestClient(app) as client:
        assert client.get(url, headers=headers).status_code == 403
        task = client.get('/api/asclepius/' + ('tutorial' if kind == 'practice' else 'exam') + '/task', headers=headers).json()['task']
        assert 'HELD OUT INTERPRETATION' not in json.dumps(task)
        assert task['case']['studies'][0]['asset']['asset_id'] == asset['asset_id']
        image = client.get(url, headers=headers)
        assert image.status_code == 200
        assert hashlib.sha256(image.content).hexdigest() == asset['sha256']
        assert not Image.open(io.BytesIO(image.content)).info
        assert client.get(url, headers=headers_for(other)).status_code == 403
        assert client.get(url).status_code == 401
        assert client.get('/api/asclepius/assets/partner-image', headers=headers).status_code == 403
        admin = make_user(store, role='admin')
        assert client.get(url, headers=headers_for(admin)).content == image.content
    # The internal key is preserved for grading.
    assert bank.entry_for(store, doc['task_id'])['case']['studies'][0]['findings'] == 'HELD OUT INTERPRETATION'


def test_release_library_contains_all_86_reviewed_cases(monkeypatch):
    # Deliberately fails until real CI artifacts have been reviewed and committed.
    # Passing fixtures never substitutes for completed clinical generation.
    from pathlib import Path
    monkeypatch.setattr(library, 'ROOT', Path(library.__file__).with_name('onboarding_material') / 'cases')
    coverage = library.coverage()
    missing = [(row['specialty'], row['kind']) for row in coverage if not row['ready']]
    assert len(coverage) == 86 and not missing, missing


def test_both_pathology_reviewers_see_same_pixels_without_caption_or_key(monkeypatch):
    from ai import llm_client
    from asclepius import onboarding_evidence
    calls = []
    async def retrieve(*args, **kwargs): return list(SOURCES)
    async def llm(**kw):
        content = kw['messages'][0]['content']
        payload = json.loads(content[0]['text'])
        assert content[1]['type'] == 'image'
        import base64
        sha = hashlib.sha256(base64.b64decode(content[1]['source']['data'])).hexdigest()
        assert sha == media.reference('practice')['asset']['sha256']
        calls.append(kw['purpose'])
        if kw['purpose'] == 'onboarding_case_author':
            result = publication('pathology')['entry']
            result['case']['studies'][0].pop('asset')
        elif kw['purpose'] == 'onboarding_case_solve':
            assert not payload['case']['case'].get('ground_truth')
            assert 'HELD OUT INTERPRETATION' not in json.dumps(payload)
            assert all(not r['id'].startswith('reference-slide-') for r in payload['sources'])
            result = {'best_answer_id': 'A', 'confidence': .96, 'rationale': 'Fixture pixel interpretation'}
        else:
            result = {**approved_review(payload['case_to_review']), 'image_supports_key': True,
                      'image_has_no_identifiers': True, 'image_observations': 'Fixture pixel findings'}
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(result))]), {'model': kw.get('model', 'fixture-author')}
    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(llm_client, 'call_llm', llm)
    entry, report = asyncio.run(bank.build_case(fresh_store(), 'pathology', 'practice', bank.task_id('pathology', 'practice')))
    assert calls.count('onboarding_case_solve') == calls.count('onboarding_case_review') == 2
    assert entry['case']['case_provenance']['disclaimers']
    assert report['method'] == 'fake_fixture_only'


def test_new_draft_cannot_leak_future_tests_through_modality_metadata(monkeypatch):
    from ai import llm_client
    from asclepius import onboarding_evidence
    async def retrieve(*args, **kwargs): return list(SOURCES)
    async def llm(**kw):
        assert kw['purpose'] == 'onboarding_case_author'  # reject before blinded review
        entry = fixture_entry()
        entry['case']['required_modalities'] = ['Future diagnostic answer']
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(entry))]), {'model': 'fixture-author'}
    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(llm_client, 'call_llm', llm)
    with pytest.raises(ValueError, match='authoring_modality_hint_forbidden'):
        asyncio.run(bank.build_case(fresh_store(), 'dermatology', 'practice', bank.task_id('dermatology', 'practice')))


def test_rejected_review_retains_both_outcomes_without_publishing_or_orphaning(monkeypatch):
    from ai import llm_client
    from asclepius import onboarding_evidence
    reports, completed = [], []

    async def retrieve(*args, **kwargs): return list(SOURCES)

    async def llm(**kw):
        model = kw.get('model', 'fixture-author')
        if kw['purpose'] == 'onboarding_case_author':
            result = fixture_entry()
        else:
            assert kw['purpose'] == 'onboarding_case_solve'
            if model.startswith('gpt'):
                await asyncio.sleep(.02)  # Must still finish after the other rejects.
            result = {'best_answer_id': 'A', 'confidence': .8, 'rationale': 'Uncertain evidence'}
            completed.append(model)
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(result))]), {
            'model': model, 'request_id': 'request-' + model}

    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(llm_client, 'call_llm', llm)
    token = bank.REVIEW_DIAGNOSTICS.set(reports.append)
    store = fresh_store()
    ident = bank.task_id('dermatology', 'practice')
    try:
        with pytest.raises(ValueError, match='clinical_review_rejected'):
            asyncio.run(bank.build_case(store, 'dermatology', 'practice', ident))
    finally:
        bank.REVIEW_DIAGNOSTICS.reset(token)
    assert len(completed) == 2
    assert bank.get_task(store, ident) is None
    assert len(reports) == 1 and reports[0]['status'] == 'rejected'
    assert {r['provider'] for r in reports[0]['reviews']} == {'openai', 'anthropic'}
    assert all(r['status'] == 'rejected' and r['solve_request_id'] for r in reports[0]['reviews'])
    assert 'ground_truth' not in json.dumps(reports[0]['blinded_input'])


@pytest.mark.parametrize('field', ['question', 'title', 'claim', 'safety_keywords', 'evidence_keywords'])
def test_identifier_rejection_reports_categories_without_retaining_unsafe_entry(monkeypatch, field):
    from ai import llm_client
    from asclepius import onboarding_evidence
    reports = []
    entry = fixture_entry()
    if field == 'claim':
        entry['claims'][0]['statement'] += ' Contact test-person@example.org.'
    elif field.endswith('_keywords'):
        entry[field][0] = 'test-person@example.org'
    else:
        entry[field] += ' Contact test-person@example.org.'

    async def retrieve(*args, **kwargs): return list(SOURCES)
    async def llm(**kw):
        assert kw['purpose'] == 'onboarding_case_author'
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(entry))]), {'model': 'fixture'}

    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(llm_client, 'call_llm', llm)
    token = bank.REVIEW_DIAGNOSTICS.set(reports.append)
    try:
        with pytest.raises(ValueError, match='possible_identifier: email') as error:
            asyncio.run(bank.build_case(fresh_store(), 'dermatology', 'practice', bank.task_id('dermatology', 'practice')))
    finally:
        bank.REVIEW_DIAGNOSTICS.reset(token)
    assert 'test-person@example.org' not in str(error.value)
    assert reports == []


def test_diagnostic_artifacts_are_separate_and_observer_is_reset_on_failure(monkeypatch, tmp_path):
    from scripts import build_onboarding_library as builder
    diagnostic = {'status': 'rejected', 'task_id': bank.task_id('dermatology', 'practice')}

    async def fail(*args):
        bank.REVIEW_DIAGNOSTICS.get()(diagnostic)
        raise ValueError('Rejected fixture')

    monkeypatch.setattr(builder, '_build', fail)
    original = bank.REVIEW_DIAGNOSTICS.get()
    with pytest.raises(ValueError, match='Rejected fixture'):
        asyncio.run(builder.build('dermatology', tmp_path / 'cases', tmp_path / 'diagnostics'))
    assert bank.REVIEW_DIAGNOSTICS.get() is original
    assert not list((tmp_path / 'cases').glob('*.json'))
    files = list((tmp_path / 'diagnostics').glob('rejected-*.json'))
    assert len(files) == 1 and json.loads(files[0].read_text()) == diagnostic
    assert library.row_for(files[0].stem) is None


def test_claims_reject_unknown_fields_that_could_escape_identifier_screening():
    entry = fixture_entry()
    entry['claims'][0]['comment'] = 'test-person@example.org'
    with pytest.raises(ValueError, match='unexpected_claim_fields'):
        bank.validate_entry(entry, 'dermatology', SOURCES)


@pytest.mark.parametrize('damage', ['missing', 'invented', 'wrong_source', 'unquoted_source'])
def test_clinical_review_requires_quotes_from_each_actual_retrieved_source(damage):
    entry = fixture_entry()
    review = approved_review(entry)
    assert bank.validate_review(review, entry, SOURCES) is None
    if damage == 'missing':
        review['claim_checks'][0].pop('source_quotes')
    elif damage == 'invented':
        review['claim_checks'][0]['source_quotes'][0]['quote'] = 'An invented guideline recommendation not in the source'
    elif damage == 'wrong_source':
        review['claim_checks'][0]['source_quotes'][0]['source_id'] = '2'
    else:
        review['claim_checks'][2]['source_quotes'].pop()
    with pytest.raises(ValueError, match='source_quote'):
        bank.validate_review(review, entry, SOURCES)


def test_release_revalidates_recorded_source_quotes():
    doc = publication()
    doc['validation']['source_quote_review'] = True
    library.validate(doc)
    doc['validation']['reviews'][0]['review']['claim_checks'][0]['source_quotes'][0]['quote'] = 'An invented recommendation from model memory'
    with pytest.raises(ValueError, match='source_quote_not_in_retrieved_text'):
        library.validate(doc)


@pytest.mark.parametrize('marker', [None, False, 'true', 1])
def test_new_release_cannot_downgrade_source_review_protocol(marker):
    doc = publication()
    if marker is None:
        doc['validation'].pop('source_quote_review')
    else:
        doc['validation']['source_quote_review'] = marker
    with pytest.raises(ValueError, match='Source-quote review required'):
        library.validate(doc)


def test_legacy_evidence_audit_binds_the_entire_document(monkeypatch, tmp_path):
    doc = publication()
    doc['validation'].pop('source_quote_review')
    manifest = tmp_path / 'legacy.json'
    manifest.write_text(json.dumps({'artifacts': [{
        'task_id': doc['task_id'],
        'document_sha256': hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()}]}))
    monkeypatch.setattr(library, 'LEGACY_AUDITS', manifest)
    library.validate(doc)
    # Even a change outside the entry checksum invalidates the legacy exception.
    doc['validation']['sources'][0] = {**SOURCES[0], 'abstract': 'Changed after independent source audit'}
    with pytest.raises(ValueError, match='Source-quote review required'):
        library.validate(doc)
