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


def test_release_library_contains_all_86_reviewed_cases():
    # Deliberately fails until real CI artifacts have been reviewed and committed.
    # Passing fixtures never substitutes for completed clinical generation.
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
