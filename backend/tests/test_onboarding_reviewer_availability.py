"""No live providers: verify unavailable reviewer access cannot waste authorship."""
import asyncio
from types import SimpleNamespace

import pytest

from scripts import check_onboarding_reviewer as probe


def configure(monkeypatch, error=None):
    import anthropic
    calls = []
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('ASCLEPIUS_LLM_PROVIDER', '')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-key-never-networked')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-key-never-networked')
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['max_retries'] == 0 and kwargs['timeout'] == 30
            self.messages = self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def create(self, **kwargs):
            calls.append(kwargs)
            if error: raise error
            return SimpleNamespace(content=[])
    monkeypatch.setattr(anthropic, 'AsyncAnthropic', Client)
    return calls


def test_small_reviewer_probe_has_no_case_data_or_sampling_parameters(monkeypatch):
    calls = configure(monkeypatch)
    asyncio.run(probe.check_reviewer())
    assert len(calls) == 1
    assert calls[0]['max_tokens'] == 64
    assert calls[0]['messages'] == [{'role': 'user', 'content': 'Reply OK.'}]
    assert 'temperature' not in calls[0]


@pytest.mark.parametrize('mode', ['outside-ci', 'fake', 'missing-key'])
def test_probe_guards_precede_provider_access(monkeypatch, mode):
    calls = configure(monkeypatch)
    if mode == 'outside-ci': monkeypatch.delenv('GITHUB_ACTIONS')
    if mode == 'fake': monkeypatch.setenv('ASCLEPIUS_LLM_PROVIDER', 'fake')
    if mode == 'missing-key': monkeypatch.delenv('OPENAI_API_KEY')
    with pytest.raises(probe.ReviewerUnavailable): asyncio.run(probe.check_reviewer())
    assert calls == []


@pytest.mark.parametrize('status,message,expected', [
    (400, 'Your credit balance is too low. PRIVATE SDK BODY', 'credits are unavailable'),
    (401, 'PRIVATE SDK BODY', 'HTTP 401'),
    (429, 'PRIVATE SDK BODY', 'HTTP 429'),
])
def test_probe_failure_is_bounded_and_redacts_raw_provider_body(monkeypatch, status, message, expected):
    error = RuntimeError(message)
    error.status_code = status
    calls = configure(monkeypatch, error)
    with pytest.raises(probe.ReviewerUnavailable, match=expected) as exc:
        asyncio.run(probe.check_reviewer())
    assert 'PRIVATE SDK BODY' not in str(exc.value)
    assert len(calls) == 1


def test_unavailable_reviewer_prevents_any_authoring_or_retry():
    from scripts.smoke_onboarding_cases import prepare
    async def unavailable(): raise probe.ReviewerUnavailable('Fixture credit failure')
    def should_not_run(*args): raise AssertionError('No authorship while reviewer unavailable')
    bank = SimpleNamespace(task_id=lambda *args: 'onboarding-fixture', request_case=should_not_run)
    with pytest.raises(probe.ReviewerUnavailable):
        asyncio.run(prepare(None, bank, 'dermatology', 'practice', before_attempt=unavailable))


def test_credit_failure_stops_remaining_kind_but_keeps_passing_companion(monkeypatch, tmp_path):
    import json
    from asclepius import onboarding_library as library, store as stores
    from scripts import build_onboarding_library as builder, smoke_onboarding_cases as smoke
    from tests.test_onboarding_library import publication
    from tests._asclepius import fresh_store
    doc = publication()
    row = {'entry_json': json.dumps(doc['entry']), 'validation_json': json.dumps(doc['validation'])}
    calls = []
    async def prepare(*args, **kwargs):
        calls.append(args[3])
        assert kwargs['before_attempt'] is probe.check_reviewer
        if args[3] == 'examination': raise probe.ReviewerUnavailable('Fixture credit failure')
        return doc['task_id'], row, 1, []
    monkeypatch.setattr(stores, 'get_store', fresh_store)
    monkeypatch.setattr(library, 'row_for', lambda _: None)
    monkeypatch.setattr(smoke, 'prepare', prepare)
    with pytest.raises(probe.ReviewerUnavailable):
        asyncio.run(builder._build('dermatology', tmp_path))
    assert calls == ['practice', 'examination']
    assert json.loads((tmp_path / (doc['task_id'] + '.json')).read_text())['entry'] == doc['entry']
