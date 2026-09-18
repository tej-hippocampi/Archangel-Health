"""Specialty routing, evidence gates and isolation. Fixtures are software tests,
not clinical material approved for applicants; real generation runs in CI smoke.
"""
import asyncio
import copy
from datetime import datetime, timezone
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from tests._asclepius import app, fresh_store, make_user, headers_for
from asclepius import onboarding_cases as bank, onboarding_evidence as evidence
from asclepius.onboarding_specialties import canonical, resolve


def fixture_entry(specialty='dermatology', kind='practice'):
    topic = {
        ('dermatology', 'practice'): 'Plaque psoriasis with joint symptoms',
        ('dermatology', 'examination'): 'New blistering eruption with mucosal involvement',
        ('neurology', 'practice'): 'First focal seizure with persistent symptoms',
        ('neurology', 'examination'): 'Progressive fatigable weakness with bulbar symptoms',
    }.get((specialty, kind), 'Specialty assessment with new clinical symptoms')
    note = (topic + '. The fictional patient presents for specialist assessment. '
            'The history describes onset, progression and associated symptoms. '
            'A focused examination and relevant clinical findings are documented. '
            'There is no real patient identity or external image in this software fixture.')
    key = {'answer': 'A structured specialist assessment before choosing treatment.',
           'rationale': 'The described clinical findings require assessment of the safety concern before treatment.',
           'key_data': ['symptom onset', 'focused examination', 'associated symptoms']}
    return {'title': topic, 'question': topic + ': what is the appropriate next clinical step?',
        'case': {'specialty': specialty, 'case_source': 'synthetic',
            'demographics': {'age_band': '40-49'}, 'problem_list': [{'condition': topic}],
            'notes': [{'text': note}], 'ground_truth': key},
        'candidate_answers': [
            {'id': 'A', 'text': 'Assess the specific clinical findings and evaluate the safety concern before selecting a treatment appropriate to this presentation.'},
            {'id': 'B', 'text': 'Dismiss the documented clinical findings and proceed without evaluating the safety concern or the associated clinical symptoms.'}],
        'intended_flawed_id': 'B', 'error_tags': ['unsafe_recommendation'], 'safety_keywords': ['safety', 'symptoms', 'assessment'],
        'evidence_keywords': ['onset', 'examination', 'symptoms'],
        'claims': [{'statement': 'Clinical assessment should consider the documented symptoms.', 'source_ids': ['1']},
                   {'statement': 'Safety concerns should be assessed before selecting treatment.', 'source_ids': ['2']},
                   {'statement': 'The focused examination informs specialist assessment.', 'source_ids': ['1', '2']}]}


SOURCES = [{'id': '1', 'abstract': 'First retrieved abstract'}, {'id': '2', 'abstract': 'Second retrieved abstract'}]


def approved_review(entry):
    return {**{k: True for k in ('on_specialty', 'coherent', 'key_correct', 'sound_answer_safe',
        'evidence_supported', 'distinct_decision', 'no_missing_information')},
        'best_answer_id': 'A', 'confidence': .96, 'issues': [], 'rationale': 'Test reviewer rationale',
        'claim_checks': [{'index': i, 'supported': True, 'source_ids': c['source_ids'], 'reason': 'Test evidence check'}
                         for i, c in enumerate(entry['claims'])]}


def set_credentials(store, user_id, credentials):
    with store._conn() as conn:
        conn.execute('UPDATE users SET credentials_json=? WHERE id=?', (json.dumps(credentials), user_id))


def seed_case(store, specialty='dermatology', kind='practice', slot=1):
    entry = bank.validate_entry(fixture_entry(specialty, kind), specialty, SOURCES)
    ident = bank.task_id(specialty, kind, slot)
    with store._conn() as conn:
        conn.execute('INSERT INTO onboarding_case_bank (task_id,specialty,kind,slot,version,status,entry_json,validation_json,created_at,updated_at) VALUES (?,?,?,?,?,\'ready\',?,?,?,?)',
            (ident, specialty, kind, slot, bank.VERSION, json.dumps(entry), json.dumps({'fixture': True}), bank._now(), bank._now()))
    return ident


@pytest.mark.parametrize('raw,expected', [('Dermatologist', 'dermatology'), ('Neurologist', 'neurology'),
    ('Medical Oncology', 'oncology'), ('Radiation Oncologist', 'radiation oncology'),
    ('Pediatric Cardiology', 'pediatric cardiology'), ('Paediatric Nephrology', 'pediatric nephrology'),
    ('Neurosurgeon', 'neurosurgery'), ('Orthopaedic Surgery', 'orthopedic surgery'),
    ('Neurology and Psychiatry', 'neurology and psychiatry')])
def test_specialty_identity(raw, expected):
    assert canonical(raw) == expected


def test_confirmed_then_profile_then_actual_cv_column():
    assert resolve({'specialty': None, 'credentials_json': json.dumps({'primarySpecialty': 'Dermatology'})})['specialty'] == 'dermatology'
    assert resolve({'specialty': 'nephrology', 'credentials_json': {'primarySpecialty': 'Dermatology'}})['specialty'] == 'dermatology'
    assert resolve({'cv_parsed_json': json.dumps({'specialty_display': 'Neurology'})})['specialty'] == 'neurology'
    assert resolve({'specialty': None})['specialty'] == ''


def test_board_issuer_never_overrides_specialist_field():
    from asclepius.credentialing import _extract_specialty
    assert _extract_specialty('', ['American Board of Internal Medicine Medical Oncology']) == 'oncology'
    assert _extract_specialty('', ['American Board of Psychiatry and Neurology Neurology']) == 'neurology'
    assert _extract_specialty('Primary specialty: Dermatology', ['American Board of Internal Medicine']) == 'dermatology'
    assert _extract_specialty('', ['American Board of Internal Medicine', 'ABIM — Nephrology']) == 'nephrology'
    assert _extract_specialty('Specialty: Pediatric Nephrology', []) == 'pediatric nephrology'


@pytest.mark.parametrize('specialty', ['dermatology', 'neurology'])
def test_specialty_case_routes_reveal_and_file_without_entering_paid_data(specialty, tmp_path):
    from scripts.data_inventory import snapshot, compare
    store = fresh_store()
    practice = seed_case(store, specialty)
    exam = seed_case(store, specialty, 'examination')
    user = make_user(store, specialty=None)
    set_credentials(store, user['id'], {'primarySpecialty': specialty})
    store.set_verification_status(user['id'], 'pending')
    user = store.get_user_by_id(user['id'])
    headers = headers_for(user)
    before = snapshot(store.db_path)
    backup = tmp_path / 'before.db'
    with sqlite3.connect(store.db_path) as source, sqlite3.connect(backup) as dest:
        source.backup(dest)
    assert compare(before, snapshot(backup)) == []
    with TestClient(app) as client:
        p = client.get('/api/asclepius/tutorial/task', headers=headers)
        assert p.status_code == 200, p.text
        assert p.json()['task']['task_id'] == practice
        assert 'ground_truth' not in p.json()['task']['case']
        reveal = client.post('/api/asclepius/tutorial/reveal', headers=headers,
            json={'task_id': practice, 'text': 'My independent assessment of the clinical symptoms.'})
        assert reveal.status_code == 200, reveal.text
        assert reveal.json()['answers'][0]['text']
        assert client.post('/api/asclepius/tutorial/report', headers=headers,
            json={'task_id': practice, 'note': 'The case needs additional clinical information.'}).status_code == 200
        assert store.get_tutorial_state(user['id'])['practice_concerns'][0]['task_id'] == practice
        result = client.post('/api/asclepius/tutorial/submit', headers=headers, json={'task_id': practice, 'verdict': 'A_better',
            'chosen_id': 'A', 'rejected_id': 'B', 'independent_answer': {'text': 'Assess onset and symptoms safely.'}})
        assert result.status_code == 200, result.text
        assert result.json()['result']['teaching']['reference_answer'] == fixture_entry(specialty)['case']['ground_truth']['answer']
        draw = client.get('/api/asclepius/exam/task', headers=headers)
        assert draw.status_code == 200, draw.text
        assert draw.json()['task']['task_id'] == exam
        assert draw.json()['is_own_specialty'] is True
        assert client.get('/api/asclepius/tasks/' + practice, headers=headers).status_code == 403
        wrong = seed_case(store, 'radiology', 'examination')
        assert client.get('/api/asclepius/tasks/' + wrong, headers=headers).status_code == 403
        reveal = client.post('/api/asclepius/tasks/' + exam + '/reveal', headers=headers,
            json={'text': 'Specialist assessment based on the clinical findings.', 'portal_version': 'v3'})
        assert reveal.status_code == 200, reveal.text
        assert store.get_independent_commit(exam, user['id'])['payload']['purpose'] == 'credentialing_exam'
        assert client.get('/api/asclepius/tasks/' + exam + '/answers', headers=headers).status_code == 200
        for _ in range(2):
            submitted = client.post('/api/asclepius/exam/submit', headers=headers,
                json={'task_id': exam, 'chosen_id': 'A', 'verdict': 'A_better'})
            assert submitted.status_code == 200, submitted.text
        assert len(store.list_credentialing_exams(user['id'])) == 1
        filed = store.list_credentialing_exams(user['id'])[0]
        assert filed['specialty'] == specialty
        store.set_verification_status(user['id'], 'approved')
        assert client.post('/api/asclepius/submissions', headers=headers,
            json={'task_id': exam, 'chosen_id': 'A', 'verdict': 'A_better'}).status_code == 403
    for ident in (practice, exam):
        assert store.get_task(ident) is None
    with store._conn() as conn:
        assert conn.execute('SELECT count(*) FROM submissions').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM records').fetchone()[0] == 0
    assert compare(before, snapshot(store.db_path), allowed=['users.tutorial_json', 'users.verification_status']) == []


def test_stale_unfinished_draw_is_archived_but_submitted_draw_is_preserved():
    store = fresh_store()
    user = make_user(store, specialty='nephrology')
    store.set_verification_status(user['id'], 'pending')
    headers = headers_for(user)
    with TestClient(app) as client:
        old = client.get('/api/asclepius/exam/task', headers=headers).json()['task']['task_id']
        set_credentials(store, user['id'], {'primarySpecialty': 'dermatology'})
        new = seed_case(store, 'dermatology', 'examination')
        assert client.post('/api/asclepius/exam/submit', headers=headers, json={'task_id': old}).status_code == 409
        assert client.get('/api/asclepius/exam/task', headers=headers).json()['task']['task_id'] == new
        assert store.get_tutorial_state(user['id'])['previous_exam_draws'][0]['task_id'] == old
        assert store.is_onboarding_answer(old, user['id'])
        assert client.post('/api/asclepius/exam/submit', headers=headers, json={'task_id': new}).status_code == 200
        set_credentials(store, user['id'], {'primarySpecialty': 'neurology'})
        assert client.get('/api/asclepius/exam/task', headers=headers).json()['task']['task_id'] == new
        assert client.post('/api/asclepius/exam/submit', headers=headers, json={'task_id': new}).status_code == 200


@pytest.mark.parametrize('defect', ['wrong_specialty', 'unverified_source', 'missing_key', 'unsafe', 'disagreement', 'missing_claim', 'low_confidence'])
def test_publication_requires_all_clinical_and_evidence_gates(defect):
    entry = fixture_entry()
    review = approved_review(entry)
    if defect == 'wrong_specialty': entry['case']['specialty'] = 'nephrology'
    if defect == 'unverified_source': entry['claims'][0]['source_ids'] = ['invented']
    if defect == 'missing_key': entry['case']['ground_truth'] = {}
    if defect == 'unsafe': review['sound_answer_safe'] = False
    if defect == 'disagreement': review['best_answer_id'] = 'B'
    if defect == 'missing_claim': review['claim_checks'].pop()
    if defect == 'low_confidence': review['confidence'] = .8
    with pytest.raises(ValueError):
        entry = bank.validate_entry(entry, 'dermatology', SOURCES)
        bank.validate_review(review, entry)


def test_real_or_incomplete_charts_cannot_publish():
    for field, value in [('case_source', 'real_deid'), ('notes', []), ('study_findings_policy', 'hidden')]:
        entry = fixture_entry()
        entry['case'][field] = value
        with pytest.raises(ValueError): bank.validate_entry(entry, 'dermatology', SOURCES)


def test_case_may_not_carry_assets_or_source_refs():
    """Real models populate source_refs because the supplied schema advertises it.
    Both causes are rejected, and each reports its own code so a failed CI smoke
    names the offending field instead of a shared one."""
    asset_study = {'modality': 'pathology', 'label': 'Skin biopsy',
                   'findings': 'Fictional structured report text for this software fixture.',
                   'asset': {'asset_id': 'fixture-asset', 'mime': 'image/png', 'sha256': 'a' * 64}}
    for field, value, code in [
            ('studies', [asset_study], 'external_case_asset'),
            ('source_refs', [{'title': 'Fictional reference', 'identifier': 'PMID:12345678'}],
             'case_carries_source_refs')]:
        entry = fixture_entry()
        entry['case'][field] = value
        with pytest.raises(ValueError) as excinfo:
            bank.validate_entry(entry, 'dermatology', SOURCES)
        assert str(excinfo.value) == code
    # The intended citation channel still validates unchanged.
    assert bank.validate_entry(fixture_entry(), 'dermatology', SOURCES)['claims']


def test_author_prompt_states_every_machine_checked_requirement():
    """Real runs failed on requirements the validator enforces and the prompt never
    named (study_findings_policy, where the answer key lives, candidate length).
    An unstated gate is one the model can only satisfy by luck."""
    for needle in ('source_refs', 'claims[].source_ids', 'study_findings_policy',
                   'case.ground_truth', '80 characters', '20 characters',
                   'case.demographics.age_band', '"synthetic"'):
        assert needle in bank.AUTHOR_SYSTEM, needle


def test_review_prompt_scopes_issues_to_blocking_defects():
    """Real reviewers approved sound neurology cases while writing observations
    into issues ('Minor:', 'does not affect the recommended answer', 'Correctly
    flagged as the intended flawed answer'). validate_review treats any entry as
    fatal, so the prompt must say issues is blocking and notes go in rationale."""
    assert 'BLOCKING' in bank.REVIEW_SYSTEM
    assert 'empty array' in bank.REVIEW_SYSTEM
    assert 'rationale' in bank.REVIEW_SYSTEM
    # The threshold itself is untouched: any issue still rejects.
    entry = fixture_entry()
    review = approved_review(entry)
    review['issues'] = [{'note': 'a minor observation'}]
    with pytest.raises(ValueError):
        bank.validate_review(review, entry)


def test_author_prompt_scopes_study_findings_policy_to_the_case():
    """A real run put study_findings_policy inside studies[0]; Study forbids extra
    keys, so the case died in schema validation before any gate could see it."""
    assert 'NOT of any entry in case.studies' in bank.AUTHOR_SYSTEM
    study = {'modality': 'ct', 'label': 'CT', 'findings': 'Fictional report text.',
             'study_findings_policy': 'visible'}
    entry = fixture_entry()
    entry['case']['studies'] = [study]
    with pytest.raises(Exception) as excinfo:
        bank.validate_entry(entry, 'dermatology', SOURCES)
    assert 'study_findings_policy' in str(excinfo.value)


def test_review_rejection_names_the_signal_that_declined():
    """A real run rejected three cases and reported 'clinical_review_failed: []' —
    refused, with no ground given. The message must name the failing signal."""
    entry = fixture_entry()
    review = approved_review(entry)
    review['key_correct'] = False
    review['sound_answer_safe'] = False
    review['issues'] = []
    with pytest.raises(ValueError) as excinfo:
        bank.validate_review(review, entry)
    message = str(excinfo.value)
    assert 'key_correct' in message and 'sound_answer_safe' in message
    assert 'on_specialty' not in message  # only the declined signals, not all seven


@pytest.mark.parametrize('field,value,expected', [
    ('best_answer_id', 'B', 'best_answer_id'),
    ('confidence', .5, 'confidence'),
    ('confidence', True, 'confidence'),
    ('rationale', '', 'rationale'),
    ('issues', [{'problem': 'late objection'}], 'issues'),
])
def test_review_disagreement_names_its_cause(field, value, expected):
    """Four independent conditions shared one code, so a rejected run could not say
    which one tripped. Thresholds are unchanged; only the message is specific."""
    entry = fixture_entry()
    review = approved_review(entry)
    review[field] = value
    with pytest.raises(ValueError) as excinfo:
        bank.validate_review(review, entry)
    assert expected in str(excinfo.value)
    assert str(excinfo.value).startswith('clinical_review_disagreement')


def test_approved_review_still_passes_unchanged():
    """The split must not move any threshold — a good review still ratifies."""
    entry = bank.validate_entry(fixture_entry(), 'dermatology', SOURCES)
    assert bank.validate_review(approved_review(entry), entry) is None


def test_smoke_retries_a_rejected_case_and_counts_attempts(monkeypatch):
    """Production re-leases a retry_wait row, so the smoke must too — and must
    report the attempts rather than hiding a poor first-pass yield behind them."""
    from scripts import smoke_onboarding_cases as smoke

    async def no_sleep(_): return None
    monkeypatch.setattr(smoke.asyncio, 'sleep', no_sleep)

    class FakeBank:
        _RUNNING: set = set()
        def __init__(self, statuses): self.statuses, self.calls = list(statuses), 0
        def task_id(self, specialty, kind): return f'onboarding-{kind}'
        def request_case(self, store, specialty, kind): self.calls += 1
        def row_for(self, store, ident):
            code = self.statuses[self.calls - 1]
            return ({'status': 'ready'} if code is None
                    else {'status': 'retry_wait', 'error_code': code, 'lease_until': 0})

    fake = FakeBank([None])
    ident, row, attempts, failures = asyncio.run(smoke.prepare(None, fake, 'dermatology', 'practice'))
    assert (attempts, failures, row['status']) == (1, [], 'ready')

    fake = FakeBank(['answer_key_missing', 'case_carries_source_refs', None])
    _, _, attempts, failures = asyncio.run(smoke.prepare(None, fake, 'dermatology', 'practice'))
    assert attempts == 3
    assert failures == ['answer_key_missing', 'case_carries_source_refs']

    monkeypatch.setattr(smoke, 'ATTEMPTS', 2)
    fake = FakeBank(['answer_key_missing', 'answer_key_missing'])
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(smoke.prepare(None, fake, 'dermatology', 'practice'))
    assert 'no case after 2 attempts' in str(excinfo.value)


def test_author_key_is_withheld_from_both_independent_solvers(monkeypatch):
    from ai import llm_client
    store = fresh_store()
    calls = []
    async def retrieve(_): return SOURCES
    async def llm(**kwargs):
        calls.append(kwargs)
        purpose = kwargs['purpose']
        payload = json.loads(kwargs['messages'][0]['content'])
        if purpose == 'onboarding_case_author':
            result = fixture_entry()
        elif purpose == 'onboarding_case_solve':
            assert 'ground_truth' not in payload['case']['case']
            assert 'intended_flawed_id' not in payload['case']
            assert 'claims' not in payload['case']
            result = {'best_answer_id': 'A', 'confidence': .97, 'rationale': 'Independent test assessment.'}
        else:
            result = approved_review(payload['case_to_review'])
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(result))]), {'model': kwargs.get('model', 'author-model')}
    monkeypatch.setattr(evidence, 'retrieve', retrieve)
    monkeypatch.setattr(llm_client, 'call_llm', llm)
    entry, validation = asyncio.run(bank.build_case(store, 'dermatology', 'practice', bank.task_id('dermatology', 'practice')))
    assert len(calls) == 5
    assert {r['provider'] for r in validation['reviews']} == {'anthropic', 'openai'}
    assert validation['physician_ratified'] is False


def test_generation_leases_retry_and_never_publish_failed_work(monkeypatch):
    store = fresh_store()
    calls = []
    async def scenario():
        blocked = asyncio.Event()
        async def build(*args):
            calls.append(args)
            await blocked.wait()
            return bank.validate_entry(fixture_entry(), 'dermatology', SOURCES), {'test': True}
        monkeypatch.setattr(bank, 'build_case', build)
        for _ in range(5): assert bank.request_case(store, 'dermatology', 'practice')['status'] == 'generating'
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(calls) == 1
        assert bank.get_task(store, bank.task_id('dermatology', 'practice')) is None
        blocked.set()
        await asyncio.gather(*list(bank._RUNNING))
        assert bank.request_case(store, 'dermatology', 'practice')['status'] == 'ready'
        async def fail(*args): raise ValueError('unsafe evidence')
        monkeypatch.setattr(bank, 'build_case', fail)
        bank.request_case(store, 'neurology', 'practice')
        await asyncio.gather(*list(bank._RUNNING))
        ident = bank.task_id('neurology', 'practice')
        assert bank.get_task(store, ident) is None
        assert bank.request_case(store, 'neurology', 'practice')['status'] == 'retry_wait'
        with store._conn() as conn:
            conn.execute("UPDATE onboarding_case_bank SET status='generating',lease_until=0,lease_token='dead-process' WHERE task_id=?", (ident,))
        bank.request_case(store, 'neurology', 'practice')
        await asyncio.gather(*list(bank._RUNNING))
        assert bank.row_for(store, ident)['lease_token'] != 'dead-process'
        assert bank.get_task(store, ident) is None
    asyncio.run(scenario())


def test_expired_worker_cannot_overwrite_new_lease(monkeypatch):
    store = fresh_store()
    ident = seed_case(store)
    with store._conn() as conn:
        conn.execute("UPDATE onboarding_case_bank SET status='generating',lease_token='new' WHERE task_id=?", (ident,))
    async def build(*args): return fixture_entry(), {'stale': True}
    monkeypatch.setattr(bank, 'build_case', build)
    asyncio.run(bank._run(store, ident, 'dermatology', 'practice', 'old'))
    assert bank.get_task(store, ident) is None
    assert bank.row_for(store, ident)['lease_token'] == 'new'


def test_reference_parser_excludes_old_retracted_or_unusable_material():
    def article(pmid, year=2025, pubtype='Practice Guideline', abstract=None, relation=''):
        return f'<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>Clinical guidance</ArticleTitle><Journal><JournalIssue><PubDate><Year>{year}</Year></PubDate></JournalIssue></Journal><Abstract><AbstractText>{abstract if abstract is not None else "Clinical recommendation. " * 30}</AbstractText></Abstract><PublicationTypeList><PublicationType>{pubtype}</PublicationType></PublicationTypeList></Article>{relation}</MedlineCitation></PubmedArticle>'
    raw = '<PubmedArticleSet>' + article('1') + article('2', year=2001) + article('3', pubtype='Retracted Publication') + article('4', abstract='short') + article('5', relation='<CommentsCorrections RefType="RetractionIn"/>') + '</PubmedArticleSet>'
    rows = evidence.parse_articles(raw.encode(), now=datetime(2026, 9, 17, tzinfo=timezone.utc))
    assert [row['id'] for row in rows] == ['1']
    assert rows[0]['url'] == 'https://pubmed.ncbi.nlm.nih.gov/1/'
    assert len(rows[0]['sha256']) == 64
    with pytest.raises(ValueError): evidence.parse_articles(b'<!ENTITY malicious>')


def test_legacy_exam_commit_stays_excluded_after_approval_and_stamp_change():
    store = fresh_store()
    user = make_user(store, specialty='nephrology')
    store.set_verification_status(user['id'], 'pending')
    headers = headers_for(user)
    with TestClient(app) as client:
        ident = client.get('/api/asclepius/exam/task', headers=headers).json()['task']['task_id']
        assert client.post('/api/asclepius/tasks/' + ident + '/reveal', headers=headers,
            json={'text': 'Original independent examination assessment.', 'portal_version': 'v3'}).status_code == 200
        store.set_tutorial_state(user['id'], {'status': 'completed', 'exam': {'state': 'not_started', 'attempt': 2}})
        store.set_verification_status(user['id'], 'approved')
        assert client.post('/api/asclepius/submissions', headers=headers,
            json={'task_id': ident, 'verdict': 'A_better', 'chosen_id': 'A'}).status_code == 403
        assert store.get_independent_commit(ident, user['id'])['payload']['text'] == 'Original independent examination assessment.'


@pytest.mark.parametrize('assigned', [True, False])
def test_paid_queue_excludes_exam_answers_before_counting_or_offering(assigned, monkeypatch):
    monkeypatch.setenv('ASCLEPIUS_OPEN_CASE_POOL_ENABLED', '0' if assigned else '1')
    store = fresh_store()
    user = make_user(store, specialty='nephrology')
    seen = store.insert_task(prompt='Previous exam clinical case', specialty='nephrology')
    unseen = store.insert_task(prompt='An unrelated paid clinical case', specialty='nephrology')
    if assigned:
        for task in (seen, unseen):
            store.upsert_assignment(task_id=task['task_id'], user_id=user['id'], role='label', assigned_by='test-admin')
    def ids():
        sql, params = store.labeler_queue_sql(evaluator_id=user['id'], specialty='nephrology')
        with store._conn() as conn:
            return {r['task_id'] for r in conn.execute(sql, params)}
    assert ids() == {seen['task_id'], unseen['task_id']}
    store.record_credentialing_exam(user_id=user['id'], task_id=seen['task_id'], specialty='nephrology', attempt=1, payload={})
    assert ids() == {unseen['task_id']}


@pytest.mark.parametrize('reject', [False, True])
def test_full_generation_uses_registered_fake_transport_and_fail_switch(monkeypatch, reject):
    async def retrieve(_): return SOURCES
    monkeypatch.setattr(evidence, 'retrieve', retrieve)
    monkeypatch.setenv('FAKE_LLM_VERDICT', 'fail' if reject else 'pass')
    store = fresh_store()
    async def scenario():
        bank.request_case(store, 'dermatology', 'practice')
        await asyncio.gather(*list(bank._RUNNING))
    asyncio.run(scenario())
    row = bank.row_for(store, bank.task_id('dermatology', 'practice'))
    assert row['status'] == ('retry_wait' if reject else 'ready')
    if not reject:
        assert json.loads(row['validation_json'])['method'] == 'fake_fixture_only'
        assert store.get_task(row['task_id']) is None
