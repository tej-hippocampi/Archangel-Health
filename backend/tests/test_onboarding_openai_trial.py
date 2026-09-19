"""Fake-only regression checks for a real-CI-only, non-publishing experiment."""
import asyncio
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
