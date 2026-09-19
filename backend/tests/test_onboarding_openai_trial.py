"""Fake-only regression checks for a real-CI-only, non-publishing experiment."""
import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from asclepius import onboarding_cases as bank, onboarding_library as library
from scripts import trial_onboarding_openai as trial


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('ENV', 'test')
    monkeypatch.setenv('ASCLEPIUS_LLM_PROVIDER', '')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-never-networked')
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setenv('MODEL_ASCLEPIUS_CASE_GEN', 'gpt-5.6-sol')
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    monkeypatch.setenv('ASCLEPIUS_DB_PATH', str(tmp_path / 'trial.db'))


@pytest.mark.parametrize('mode', ['not-ci', 'production', 'fake', 'no-key', 'anthropic-author'])
def test_trial_guards_run_before_retrieval_or_authorship(monkeypatch, tmp_path, mode):
    from asclepius import onboarding_evidence
    configure(monkeypatch, tmp_path)
    if mode == 'not-ci': monkeypatch.delenv('GITHUB_ACTIONS')
    if mode == 'production': monkeypatch.setenv('ENV', 'production')
    if mode == 'fake': monkeypatch.setenv('ASCLEPIUS_LLM_PROVIDER', 'fake')
    if mode == 'no-key': monkeypatch.delenv('OPENAI_API_KEY')
    if mode == 'anthropic-author': monkeypatch.delenv('MODEL_ASCLEPIUS_CASE_GEN')
    async def forbidden(*args, **kwargs): raise AssertionError('No retrieval or spending allowed')
    monkeypatch.setattr(onboarding_evidence, 'retrieve', forbidden)
    with pytest.raises(ValueError):
        asyncio.run(bank.build_case(None, 'pathology', 'practice', 'trial', openai_trial=True))


def test_trial_uses_blinded_openai_calls_and_cannot_be_released(monkeypatch, tmp_path):
    from ai import llm_client
    from asclepius import onboarding_evidence
    from tests._asclepius import fresh_store
    from tests.test_onboarding_specialty_cases import fixture_entry, approved_review, SOURCES
    configure(monkeypatch, tmp_path)
    calls = []
    async def retrieve(*args, **kwargs): return list(SOURCES)
    async def llm(**kwargs):
        calls.append(kwargs)
        payload = json.loads(kwargs['messages'][0]['content'])
        if kwargs['purpose'] == 'onboarding_case_author': result = fixture_entry()
        elif kwargs['purpose'] == 'onboarding_case_solve':
            assert 'ground_truth' not in payload['case']['case']
            assert 'intended_flawed_id' not in payload['case']
            result = {'best_answer_id': 'A', 'confidence': .96, 'rationale': 'Fake blinded assessment'}
        else: result = approved_review(payload['case_to_review'])
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(result))]), {
            'model': kwargs.get('model', 'gpt-5.6-sol'), 'provider': 'openai'}
    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(trial, 'call_openai', llm)
    store = fresh_store()
    ident = bank.task_id('dermatology', 'practice')
    entry, report = asyncio.run(bank.build_case(store, 'dermatology', 'practice', ident, openai_trial=True))
    assert len(calls) == 5
    assert {c['model'] for c in calls[1:]} == {'gpt-5.6-sol', 'gpt-5.5'}
    assert report['method'] == 'openai_only_trial'
    assert {r['provider'] for r in report['reviews']} == {'openai'}
    with pytest.raises(ValueError, match='Real clinical review required'):
        library.validate({'task_id': ident, 'specialty': 'dermatology', 'kind': 'practice',
                          'entry': entry, 'validation': report})
    assert bank.row_for(store, ident) is None


@pytest.mark.parametrize('bad', ['existing-db', 'outside-db', 'outside-output', 'nonempty-output'])
def test_existing_or_nonisolated_data_is_rejected(monkeypatch, tmp_path, bad):
    configure(monkeypatch, tmp_path)
    output = tmp_path / 'results'
    if bad == 'existing-db': (tmp_path / 'trial.db').write_text('existing original')
    if bad == 'outside-db': monkeypatch.setenv('ASCLEPIUS_DB_PATH', str(tmp_path.parent / 'live.db'))
    if bad == 'outside-output': output = tmp_path.parent / 'results'
    if bad == 'nonempty-output':
        output.mkdir()
        (output / 'original.json').write_text('preserve')
    with pytest.raises(ValueError): trial.isolated_paths(output)


def test_access_probe_uses_only_openai_and_redacts_errors(monkeypatch, tmp_path):
    import openai
    configure(monkeypatch, tmp_path)
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0 and kwargs['timeout'] == 30
            self.responses = self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def create(self, **kwargs):
            calls.append(kwargs)
            error = RuntimeError('SECRET SDK BODY')
            error.status_code = 429
            raise error
    monkeypatch.setattr(openai, 'AsyncOpenAI', Client)
    with pytest.raises(RuntimeError, match='HTTP 429') as exc:
        asyncio.run(trial.check_access())
    assert 'SECRET' not in str(exc.value)
    assert calls == [{'model': 'gpt-5.6-sol', 'input': 'Reply OK.', 'max_output_tokens': 64, 'store': False}]


def test_matrix_skips_complete_specialties_without_spending(monkeypatch):
    existing = {bank.task_id('cardiology', k) for k in ('practice', 'examination')}
    existing.add(bank.task_id('dermatology', 'practice'))
    monkeypatch.setattr(library, 'row_for', lambda ident: {} if ident in existing else None)
    assert trial.matrix('pathology,dermatology,neurology,cardiology') == ['pathology', 'dermatology', 'neurology']


def test_trial_transport_sanitizes_errors_without_retries_or_logging(monkeypatch, tmp_path, capsys):
    import openai
    configure(monkeypatch, tmp_path)
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0
            self.responses = self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def create(self, **kwargs):
            calls.append(kwargs)
            error = RuntimeError('SECRET SDK BODY')
            error.status_code = 429
            raise error
    monkeypatch.setattr(openai, 'AsyncOpenAI', Client)
    with pytest.raises(trial.TrialProviderError) as exc:
        asyncio.run(trial.call_openai(role='asclepius_case_gen', system='Synthetic test',
            messages=[{'role': 'user', 'content': 'Test'}], purpose='onboarding_case_author', max_tokens=100))
    assert exc.value.status_code == 429
    assert 'SECRET' not in str(exc.value)
    assert 'SECRET' not in capsys.readouterr().err
    assert len(calls) == 1 and calls[0]['store'] is False


def test_trial_transport_retains_usage_without_shared_team_store(monkeypatch, tmp_path):
    import openai
    configure(monkeypatch, tmp_path)
    class Client:
        def __init__(self, **kwargs): self.responses = self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def create(self, **kwargs):
            return SimpleNamespace(status='completed', output_text='{}', model='gpt-5.6-sol',
                id='synthetic-request-id', usage=SimpleNamespace(input_tokens=20, output_tokens=10))
    monkeypatch.setattr(openai, 'AsyncOpenAI', Client)
    records = []
    token = trial.CALL_USAGE.set(records)
    try:
        asyncio.run(trial.call_openai(role='asclepius_case_gen', system='Synthetic test',
            messages=[{'role': 'user', 'content': 'Test'}], purpose='onboarding_case_author', max_tokens=100))
    finally: trial.CALL_USAGE.reset(token)
    assert records[0]['request_id'] == 'synthetic-request-id'
    assert records[0]['input_tokens'] == 20 and records[0]['output_tokens'] == 10


def test_revision_feedback_is_author_only_and_public_boundary_is_explicit(monkeypatch, tmp_path):
    from asclepius import onboarding_evidence
    from tests._asclepius import fresh_store
    from tests.test_onboarding_specialty_cases import fixture_entry, approved_review, SOURCES
    configure(monkeypatch, tmp_path)
    observed = []
    async def retrieve(*args, **kwargs): return list(SOURCES)
    async def llm(**kwargs):
        payload = json.loads(kwargs['messages'][0]['content'])
        observed.append(kwargs['purpose'])
        if kwargs['purpose'] == 'onboarding_case_author':
            assert payload['revision_feedback'] == 'AUTHOR_ONLY_REPAIR'
            assert payload['draft_to_revise'] == fixture_entry()
            result = fixture_entry()
        elif kwargs['purpose'] == 'onboarding_case_solve':
            assert 'AUTHOR_ONLY_REPAIR' not in json.dumps(payload)
            assert 'ground_truth' not in payload['case']['case']
            result = {'best_answer_id': 'A', 'confidence': .96, 'rationale': 'Fixture assessment'}
        else:
            assert 'AUTHOR_ONLY_REPAIR' not in json.dumps(payload)
            assert payload['applicant_visible_case'] == bank.blind_entry(payload['case_to_review'])
            assert 'ground_truth' not in payload['applicant_visible_case']['case']
            result = approved_review(payload['case_to_review'])
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(result))]), {
            'model': kwargs.get('model', 'gpt-5.6-sol'), 'provider': 'openai'}
    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(trial, 'call_openai', llm)
    ident = bank.task_id('dermatology', 'practice')
    asyncio.run(bank.build_case(fresh_store(), 'dermatology', 'practice', ident,
        openai_trial=True, revision={'entry': fixture_entry(), 'feedback': 'AUTHOR_ONLY_REPAIR'}))
    assert len(observed) == 5


def test_resume_requires_audit_and_matching_input_hash(monkeypatch, tmp_path):
    import hashlib
    path = tmp_path / 'inputs.json'
    monkeypatch.setattr(trial, 'REVISION_INPUTS', path)
    entry = {'question': 'Immutable synthetic trial input'}
    row = {'entry': entry, 'resume': True, 'audit_disposition': 'clear',
           'entry_sha256': hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()}
    path.write_text(json.dumps({'cases': {'test': row}}))
    assert trial.revision_inputs()['test']['resume']
    row['entry']['question'] += ' changed'
    path.write_text(json.dumps({'cases': {'test': row}}))
    with pytest.raises(ValueError, match='checksum'): trial.revision_inputs()
    row['entry_sha256'] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    row['audit_disposition'] = 'hold'
    path.write_text(json.dumps({'cases': {'test': row}}))
    with pytest.raises(ValueError, match='independently audited'): trial.revision_inputs()


def prepared_revision(entry=None):
    from tests.test_onboarding_specialty_cases import fixture_entry, SOURCES
    if entry is None:
        entry = bank.validate_entry(fixture_entry(), 'dermatology', SOURCES)
    return {'review_prepared': True, 'entry': entry,
            'entry_sha256': hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest(),
            'feedback': 'AUTHOR_ONLY_REPAIR'}


@pytest.mark.parametrize('defect', ['changed-entry', 'missing-entry', 'missing-hash', 'nonboolean', 'resume'])
def test_prepared_inputs_fail_before_retrieval_models_or_runner_probe(monkeypatch, tmp_path, defect):
    from asclepius import onboarding_evidence
    configure(monkeypatch, tmp_path)
    revision = prepared_revision()
    if defect == 'changed-entry': revision['entry']['title'] += ' changed'
    if defect == 'missing-entry': revision.pop('entry')
    if defect == 'missing-hash': revision.pop('entry_sha256')
    if defect == 'nonboolean': revision['review_prepared'] = 'true'
    if defect == 'resume': revision.update(resume=True, audit_disposition='clear')
    async def forbidden(*args, **kwargs): raise AssertionError('Invalid input must not spend or retrieve')
    monkeypatch.setattr(onboarding_evidence, 'retrieve', forbidden)
    monkeypatch.setattr(trial, 'call_openai', forbidden)
    monkeypatch.setattr(trial, 'check_access', forbidden)
    ident = bank.task_id('dermatology', 'practice')
    with pytest.raises(ValueError):
        asyncio.run(bank.build_case(None, 'dermatology', 'practice', ident,
                                   openai_trial=True, revision=revision))
    path = tmp_path / 'inputs.json'
    path.write_text(json.dumps({'cases': {ident: revision}}))
    monkeypatch.setattr(trial, 'REVISION_INPUTS', path)
    monkeypatch.setattr(trial, 'matrix', lambda specialty: [specialty])
    with pytest.raises(ValueError):
        asyncio.run(trial.run('dermatology', tmp_path / 'results'))
    monkeypatch.setattr('sys.argv', ['trial_onboarding_openai', '--check-access'])
    with pytest.raises(ValueError): trial.main()
    assert not (tmp_path / 'trial.db').exists()
    assert not (tmp_path / 'results').exists()


def test_prepared_revision_is_unavailable_to_normal_runtime(monkeypatch, tmp_path):
    from asclepius import onboarding_evidence
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv('ASCLEPIUS_LLM_PROVIDER', 'fake')
    async def forbidden(*args, **kwargs): raise AssertionError('Trial context must not reach retrieval')
    monkeypatch.setattr(onboarding_evidence, 'retrieve', forbidden)
    with pytest.raises(ValueError, match='Trial context is not production material'):
        asyncio.run(bank.build_case(None, 'dermatology', 'practice', 'test', revision=prepared_revision()))


def test_prepared_runner_reviews_exact_draft_without_authoring_or_publication(monkeypatch, tmp_path):
    from asclepius import onboarding_evidence
    from tests._asclepius import fresh_store
    from tests.test_onboarding_specialty_cases import approved_review, SOURCES
    configure(monkeypatch, tmp_path)
    ident = bank.task_id('dermatology', 'practice')
    companion_id = bank.task_id('dermatology', 'examination')
    revision = prepared_revision()
    original = copy.deepcopy(revision['entry'])
    companion = {'question': 'Different independently assessed clinical decision',
                 'case': {'ground_truth': {'answer': 'Companion private answer'}}}
    resumed = {'entry': companion, 'resume': True, 'audit_disposition': 'clear',
               'entry_sha256': hashlib.sha256(json.dumps(companion, sort_keys=True).encode()).hexdigest()}
    path = tmp_path / 'inputs.json'
    path.write_text(json.dumps({'cases': {ident: revision, companion_id: resumed}}))
    monkeypatch.setattr(trial, 'REVISION_INPUTS', path)
    monkeypatch.setattr(library, 'row_for', lambda ident: None)
    store = fresh_store()
    monkeypatch.setattr('asclepius.store.get_store', lambda: store)
    events, calls = [], []
    async def probe(): events.append('probe')
    async def retrieve(*args, **kwargs):
        events.append('retrieve')
        return copy.deepcopy(SOURCES)
    async def llm(**kwargs):
        calls.append(kwargs)
        payload = json.loads(kwargs['messages'][0]['content'])
        assert 'AUTHOR_ONLY_REPAIR' not in json.dumps(payload)
        assert payload['sources'] == SOURCES
        assert kwargs['purpose'] != 'onboarding_case_author'
        if kwargs['purpose'] == 'onboarding_case_solve':
            assert payload['case'] == bank.blind_entry(original)
            assert 'ground_truth' not in payload['case']['case']
            assert 'below 0.90' in kwargs['system'] and 'Do not inflate confidence' in kwargs['system']
            result = {'best_answer_id': 'A', 'confidence': .96, 'rationale': 'Independent fixture assessment'}
        else:
            assert payload['case_to_review'] == original
            assert payload['applicant_visible_case'] == bank.blind_entry(original)
            assert {'question': companion['question'], 'answer_key': companion['case']['ground_truth']} in payload['previous_cases']
            assert "must match that claim's source_ids exactly" in kwargs['system']
            assert 'do not add sources' in kwargs['system']
            result = approved_review(original)
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(result))]), {
            'model': kwargs['model'], 'provider': 'openai', 'request_id': 'fixture-review'}
    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(trial, 'call_openai', llm)
    monkeypatch.setattr(trial, 'check_access', probe)
    # fresh_store is separate from the runner-local path checked by isolated_paths.
    monkeypatch.setenv('ASCLEPIUS_DB_PATH', str(tmp_path / 'new-trial.db'))
    output = tmp_path / 'results'
    asyncio.run(trial.run('dermatology', output))
    document = json.loads((output / ('trial-' + ident + '.json')).read_text())
    report = document['validation']
    assert document['entry'] == original
    assert document['release_eligible'] is False
    assert report['entry_sha256'] == revision['entry_sha256']
    assert report['author_model'] is None and report['authoring_method'] == 'prepared_revision'
    assert report['method'] == 'openai_only_trial' and report['physician_ratified'] is False
    assert report['source_quote_review'] is True
    assert events == ['probe', 'retrieve']
    assert len(calls) == 4
    assert {(c['model'], c['purpose']) for c in calls} == {
        (model, purpose) for model in ('gpt-5.6-sol', 'gpt-5.5')
        for purpose in ('onboarding_case_solve', 'onboarding_case_review')}
    with pytest.raises(ValueError, match='Real clinical review required'): library.validate(document)
    assert bank.row_for(store, ident) is None
    summary = json.loads((output / 'trial-summary.json').read_text())
    assert summary['release_eligible'] is False
    assert summary['results'][1]['status'] == 'audited_trial_case_skipped'


@pytest.mark.parametrize('defect', ['schema-normalization', 'boolean-coercion', 'numeric-coercion',
                                  'identifier', 'answer-cue', 'bad-source-hash'])
def test_prepared_drafts_keep_schema_identifier_and_source_gates(monkeypatch, tmp_path, defect):
    from asclepius import onboarding_evidence
    from tests._asclepius import fresh_store
    from tests.test_onboarding_specialty_cases import fixture_entry, SOURCES
    configure(monkeypatch, tmp_path)
    entry = prepared_revision()['entry']
    if defect == 'schema-normalization': entry = fixture_entry()
    if defect in ('boolean-coercion', 'numeric-coercion'):
        if defect == 'boolean-coercion': entry['case']['medications_absent_on_record'] = 1
        else: entry['case']['notes'][0]['collected_offset_days'] = 1.0
        normalized = bank.validate_entry(entry, 'dermatology', SOURCES)
        assert entry == normalized  # Python equality hides 1 -> True and 1.0 -> 1.
        assert json.dumps(entry, sort_keys=True) != json.dumps(normalized, sort_keys=True)
    if defect == 'identifier': entry['case']['notes'][0]['text'] += ' Contact patient@example.com.'
    if defect == 'answer-cue': entry['case']['required_modalities'] = ['Future diagnostic answer']
    revision = prepared_revision(entry)
    if defect == 'bad-source-hash':
        revision['sources'] = [{'id': 'retained', 'abstract': 'Retained evidence', 'abstract_sha256': 'bad'}]
    async def retrieve(*args, **kwargs): return copy.deepcopy(SOURCES)
    async def forbidden(**kwargs): raise AssertionError('Invalid prepared content must not reach models')
    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(trial, 'call_openai', forbidden)
    expected = {'schema-normalization': 'complete validated case schema',
                'boolean-coercion': 'complete validated case schema',
                'numeric-coercion': 'complete validated case schema', 'identifier': 'identifier',
                'answer-cue': 'authoring_modality_hint_forbidden', 'bad-source-hash': 'checksum'}[defect]
    with pytest.raises(ValueError, match=expected):
        asyncio.run(bank.build_case(fresh_store(), 'dermatology', 'practice', 'test',
                                   openai_trial=True, revision=revision))


@pytest.mark.parametrize('defect', ['blind-confidence', 'unsafe', 'not-distinct', 'unsupported',
                                  'invented-passage', 'added-source', 'wrong-model'])
def test_prepared_drafts_cannot_override_review_rejections(monkeypatch, tmp_path, defect):
    from asclepius import onboarding_evidence
    from tests._asclepius import fresh_store
    from tests.test_onboarding_specialty_cases import approved_review, SOURCES
    configure(monkeypatch, tmp_path)
    revision = prepared_revision()
    calls, diagnostics = [], []
    async def retrieve(*args, **kwargs): return copy.deepcopy(SOURCES)
    async def llm(**kwargs):
        calls.append(kwargs['purpose'])
        assert kwargs['purpose'] != 'onboarding_case_author'
        if kwargs['purpose'] == 'onboarding_case_solve':
            result = {'best_answer_id': 'A', 'confidence': .89 if defect == 'blind-confidence' else .96,
                      'rationale': 'Specific fixture uncertainty'}
        else:
            result = approved_review(revision['entry'])
            if defect == 'unsafe': result['sound_answer_safe'] = False
            if defect == 'not-distinct': result['distinct_decision'] = False
            if defect == 'unsupported': result['claim_checks'][0]['supported'] = False
            if defect == 'invented-passage': result['claim_checks'][0]['source_passage_ids'] = ['1:invented']
            if defect == 'added-source': result['claim_checks'][0]['source_ids'] = ['1', '2']
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(result))]), {
            'model': 'wrong-model' if defect == 'wrong-model' else kwargs['model'], 'provider': 'openai'}
    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(trial, 'call_openai', llm)
    token = bank.REVIEW_DIAGNOSTICS.set(diagnostics.append)
    try:
        with pytest.raises(ValueError, match='clinical_review_rejected'):
            asyncio.run(bank.build_case(fresh_store(), 'dermatology', 'practice', 'test',
                                       openai_trial=True, revision=revision))
    finally: bank.REVIEW_DIAGNOSTICS.reset(token)
    assert calls.count('onboarding_case_solve') == 2
    assert len(diagnostics) == 1
    assert diagnostics[0]['author_model'] is None and diagnostics[0]['author_request_id'] is None
    assert diagnostics[0]['authoring_method'] == 'prepared_revision'
    assert diagnostics[0]['entry'] == revision['entry']
    assert len(diagnostics[0]['reviews']) == 2


@pytest.mark.parametrize('image_clear', [True, False])
def test_prepared_pathology_keeps_exact_pixels_and_image_review_gate(monkeypatch, tmp_path, image_clear):
    import base64
    from asclepius import onboarding_evidence, onboarding_media
    from tests._asclepius import fresh_store
    from tests.test_onboarding_specialty_cases import approved_review, SOURCES
    from tests.test_onboarding_library import publication
    configure(monkeypatch, tmp_path)
    entry = publication('pathology')['entry']
    revision = prepared_revision(entry)
    calls = []
    async def retrieve(*args, **kwargs): return copy.deepcopy(SOURCES)
    async def llm(**kwargs):
        calls.append(kwargs['purpose'])
        assert kwargs['purpose'] != 'onboarding_case_author'
        content = kwargs['messages'][0]['content']
        assert hashlib.sha256(base64.b64decode(content[1]['source']['data'])).hexdigest() == onboarding_media.reference('practice')['asset']['sha256']
        payload = json.loads(content[0]['text'])
        if kwargs['purpose'] == 'onboarding_case_solve':
            assert 'HELD OUT INTERPRETATION' not in json.dumps(payload)
            assert all(not s['id'].startswith('reference-slide-') for s in payload['sources'])
            result = {'best_answer_id': 'A', 'confidence': .96, 'rationale': 'Independent fixture pixel assessment'}
        else:
            assert payload['case_to_review'] == entry
            result = {**approved_review(entry), 'image_supports_key': image_clear,
                      'image_has_no_identifiers': True, 'image_observations': 'Fixture pixel observations'}
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(result))]), {
            'model': kwargs['model'], 'provider': 'openai'}
    monkeypatch.setattr(onboarding_evidence, 'retrieve', retrieve)
    monkeypatch.setattr(trial, 'call_openai', llm)
    task = bank.build_case(fresh_store(), 'pathology', 'practice', 'test', openai_trial=True, revision=revision)
    if image_clear:
        actual, report = asyncio.run(task)
        assert actual == entry and report['entry_sha256'] == revision['entry_sha256']
    else:
        with pytest.raises(ValueError, match='image_review_failed'): asyncio.run(task)
    assert len(calls) == 4
