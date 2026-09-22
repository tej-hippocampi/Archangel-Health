"""Failures observed in the production seven-point walk, without real model calls."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ai import llm_client as llm
from asclepius import critic


PAIR = {'candidate_answers': [{'id': 'A', 'text': 'Sound reassessment.'},
                              {'id': 'B', 'text': 'Plausible shortcut.'}],
        'intended_flawed_id': 'B'}


def response(payload, stop='end_turn'):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(payload))],
                           stop_reason=stop)


@pytest.mark.parametrize('bad', [
    response(PAIR, 'max_tokens'),  # Valid JSON is still unfinished.
    response({'candidate_answers': 'not a list'}),
    response({'candidate_answers': [PAIR['candidate_answers'][0]]}),
    response({'candidate_answers': [{'text': ''}, {'text': 'second'}]}),
    response({'candidate_answers': [{'text': 'same'}, {'text': ' same '}]}),
    response({'candidate_answers': [{'id': 'A', 'text': 'first'}, {'id': 'A', 'text': 'second'}],
              'intended_flawed_id': 'A'}),
    response({**PAIR, 'intended_flawed_id': 'C'}),
])
def test_incomplete_pair_retries_once_and_keeps_flawed_text_identity(monkeypatch, bad):
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return (bad if len(calls) == 1 else response(PAIR)), {'model': 'test-model'}

    monkeypatch.setattr(llm, 'call_llm', call)
    result = asyncio.run(critic.generate_candidates_ex('Visible chart and question',
                                                      ai_failure_mode='Ignore the interval finding'))
    assert len(calls) == 2
    assert calls[1]['max_tokens'] >= 6000
    assert calls[0]['messages'][0]['content'] in calls[1]['messages'][0]['content']
    assert len(result['candidates']) == 2
    assert next(c['text'] for c in result['candidates'] if c['id'] == result['intended_flawed_id']) == 'Plausible shortcut.'


def test_repeated_truncation_stops_without_partial_answers(monkeypatch):
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return response(PAIR, 'max_tokens'), {}

    monkeypatch.setattr(llm, 'call_llm', call)
    result = asyncio.run(critic.generate_candidates_ex('chart'))
    assert len(calls) == 2
    assert result['candidates'] == []
    assert result['error_code'] == 'candidate_truncated'


@pytest.mark.parametrize('error', [RuntimeError('401 Unauthorized'), TimeoutError('provider unavailable')])
def test_provider_error_does_not_get_an_outer_formatting_retry(monkeypatch, error):
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(llm, 'call_llm', call)
    result = asyncio.run(critic.generate_candidates_ex('chart'))
    assert len(calls) == 1
    assert result['candidates'] == []
    assert result['error_code'] == 'candidate_provider_error'


@pytest.mark.parametrize('use_chat', [False, True])
@pytest.mark.parametrize('sync', [False, True])
def test_openai_adapter_retains_truncation_on_parseable_json(monkeypatch, use_chat, sync):
    raw = SimpleNamespace(output_text=json.dumps(PAIR), status='incomplete',
                          incomplete_details=SimpleNamespace(reason='max_output_tokens'),
                          usage=None, id='test-response',
                          choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(PAIR)), finish_reason='length')])

    def create(**kwargs):
        return raw

    async def acreate(**kwargs):
        return raw

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create if sync else acreate)))
    if not use_chat:
        client.responses = SimpleNamespace(create=create if sync else acreate)
    monkeypatch.setattr(llm, '_sopenai' if sync else '_aopenai', lambda: client)
    args = ('gpt-5', 'system', [{'role': 'user', 'content': 'chart'}], 2000, None)
    result = llm._openai_create_sync(*args) if sync else asyncio.run(llm._openai_create_async(*args))
    assert result.stop_reason == 'max_tokens'
    assert json.loads(llm.first_text(result)) == PAIR


def test_interval_judge_receives_actual_question_and_visible_context_only(monkeypatch):
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return response({'coherence': .95, 'multimodal_necessity': .9,
                         'reasoning_divergence_potential': .2, 'ground_truth_determinable': 1,
                         'explanation': 'The reassessment has little meaningful divergence.'}), {'model': 'judge'}

    monkeypatch.setattr(llm, 'call_llm', call)
    case = {'case_source': 'real_deid', 'notes': [{'text': 'Dated follow-up observation.'}],
            'ground_truth': {'answer': 'HIDDEN_ANSWER'}, 'hard_hook': 'HIDDEN_HOOK',
            'reasoning_divergence': 'HIDDEN_DIVERGENCE'}
    result = asyncio.run(critic.run_case_judge(case, case_source='real_deid',
                        question='Should the existing plan change today?', point_class='interval',
                        encounter_window=[-1, 0]))
    sent = calls[0]
    text = json.dumps(sent)
    assert 'Should the existing plan change today?' in text
    assert 'POINT CLASS: interval' in text and '[-1, 0]' in text
    assert 'HIDDEN_' not in text
    assert sent['prompt_id'] == 'asclepius_real_case_judge'
    assert result['ground_truth_determinable'] is None
    assert result['reasoning_divergence_potential'] == .2  # Context never inflates a score.


def test_interval_question_is_anchored_to_current_visit_and_has_safe_fallback(monkeypatch):
    from asclepius import real_cases
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        raise TimeoutError('upstream')

    monkeypatch.setattr(llm, 'call_llm', call)
    question, source = asyncio.run(real_cases.derive_clinical_question(
        {'notes': [{'text': 'Visible dated observation.'}]},
        {'newly_established_problems': ['HIDDEN_FUTURE']}, 'cardiology',
        point_class='interval', encounter_window=[0, 0]))
    assert source == 'deterministic'
    assert 'interval visit' in question and 'existing management plan' in question
    assert 'INTERVAL VISIT' in calls[0]['system']
    assert 'relative days: [0, 0]' in calls[0]['system']
    assert 'HIDDEN_FUTURE' not in json.dumps(calls)
