"""Real browser, shipped UI and real API agreement enforcement."""
import pytest
from playwright.sync_api import expect

from tests.test_physician_onboarding_browser import accepted_portal
from tests._asclepius import headers_for


def _sign(portal):
    result = portal.client.post('/api/asclepius/me/agreement/sign', headers=headers_for(portal.user),
        json={'typed_name': 'Tej Patel', 'signed_initials': 'TP', 'consent_esign': True})
    assert result.status_code == 200, result.text


def _sign_on_page(page):
    page.locator('#ascAgreementName').fill('Tej Patel')
    page.locator('#ascAgreementInitials').fill('TP')
    page.locator('#ascAgreementConsent').check()
    page.get_by_role('button', name='Sign and continue', exact=True).click()


def test_reviewer_is_sent_to_existing_sign_screen_and_resumes(accepted_portal, monkeypatch):
    monkeypatch.setenv('ASCLEPIUS_AGREEMENT_GATE', '1')
    portal = accepted_portal(tier='reviewer')
    page = portal.page
    page.goto('http://testserver/asclepius#review')
    expect(page.get_by_role('region', name='Agreement text')).to_be_visible()
    _sign_on_page(page)
    expect(page.get_by_role('region', name='Agreement text')).to_have_count(0)
    assert portal.store.latest_physician_agreement(portal.user['id'])
    expect(page.get_by_role('button', name='Check again', exact=True)).to_be_visible()
    assert portal.requests.count('/api/asclepius/review/pair/next') >= 2
    assert not portal.errors


def test_environment_queue_offers_the_existing_agreement_screen(accepted_portal, monkeypatch):
    monkeypatch.setenv('ASCLEPIUS_AGREEMENT_GATE', '1')
    portal = accepted_portal()
    page = portal.page
    page.goto('http://testserver/asclepius/env/annotate')
    link = page.get_by_role('link', name='Read and sign the agreement')
    expect(link).to_be_visible()
    with page.expect_popup() as popup_info:
        link.click()
    signing = popup_info.value
    expect(signing.get_by_role('region', name='Agreement text')).to_be_visible()
    _sign_on_page(signing)
    expect(signing.get_by_role('region', name='Agreement text')).to_have_count(0)
    page.reload()
    expect(page.locator('#envRoot')).to_contain_text('No trajectories awaiting annotation')
    assert not portal.errors


def test_arming_mid_annotation_keeps_unsaved_input_until_signature(accepted_portal, monkeypatch):
    monkeypatch.setenv('ASCLEPIUS_OPEN_CASE_POOL_ENABLED', '1')
    portal = accepted_portal()
    # The fixture selects assigned mode; this test explicitly covers open pool.
    monkeypatch.setenv('ASCLEPIUS_OPEN_CASE_POOL_ENABLED', '1')
    run = portal.store.insert_env_run(task_id='env-agreement-test', specialty='nephrology',
        task_type='diagnostic_workup', mode='rollout', compiled={},
        trajectory=[{'type': 'answer', 'content': 'A synthetic answer'}])
    page = portal.page
    page.goto('http://testserver/asclepius/env/annotate?run_id=' + run['run_id'])
    page.locator('#missed').fill('urine microscopy')
    monkeypatch.setenv('ASCLEPIUS_AGREEMENT_GATE', '1')
    page.get_by_role('button', name='Submit annotation').click()
    expect(page.get_by_role('link', name='Read and sign the agreement')).to_be_visible()
    expect(page.locator('#missed')).to_have_value('urine microscopy')
    assert not portal.store.get_env_run(run['run_id']).get('physician_annotation')
    _sign(portal)
    page.get_by_role('button', name='Submit annotation').click()
    expect(page.locator('#envSaveMsg')).to_contain_text('Saved')
    assert portal.store.get_env_run(run['run_id'])['physician_annotation']['missed_actions'] == ['urine microscopy']
    assert not portal.errors


@pytest.mark.parametrize('realm,prefix', [('live', ''), ('sandbox', '/sandbox')])
def test_signing_link_keeps_the_current_realm(accepted_portal, realm, prefix):
    portal = accepted_portal()
    page = portal.page
    page.goto('http://testserver/asclepius#agreement')
    expect(page.get_by_role('region', name='Agreement text')).to_be_visible()
    link = page.evaluate('''realm => {
        window.__REALM = realm;
        return window.AsclepiusAgreementGate.signingLink().getAttribute('href');
    }''', realm)
    assert link == prefix + '/asclepius#agreement'


def test_blocked_review_submit_preserves_notes_and_step_judgments(accepted_portal, monkeypatch):
    import json
    from tests.test_paired_review import _paired_task, _admin_h
    portal = accepted_portal(tier='reviewer')
    monkeypatch.setenv('ASCLEPIUS_OPEN_CASE_POOL_ENABLED', '1')
    tid = _paired_task(_admin_h(), max_labels=2)
    # Add divergent reasoning to these local fixture submissions so the UI
    # exposes the per-step judgment that ordinary review drafts do not save.
    with portal.store._conn() as conn:
        rows = conn.execute('SELECT submission_id,payload_json FROM submissions WHERE task_id=?', (tid,)).fetchall()
        for index, row in enumerate(rows):
            payload = json.loads(row['payload_json'])
            payload['reasoning_steps'] = [{'step': 1, 'text': ['Stabilize the myocardium.', 'Immediately start dialysis.'][index]}]
            conn.execute('UPDATE submissions SET payload_json=? WHERE submission_id=?',
                         (json.dumps(payload), row['submission_id']))
    page = portal.page
    page.goto('http://testserver/asclepius#review')
    page.get_by_role('radiogroup', name='Which is stronger?').get_by_role('radio', name='A', exact=True).click()
    for row in page.locator('[data-dim-idx]').all():
        row.get_by_role('radio', name='Agree', exact=True).click()
    fork = page.locator('.asc-rv-fork-row').get_by_role('radio', name='A', exact=True)
    fork.click()
    page.locator('[data-verdict="accept_with_edits"]').click()
    notes = page.get_by_placeholder('What is wrong, and what should change? Required on edits and on reject.')
    notes.fill('Use a safer potassium bath.')
    monkeypatch.setenv('ASCLEPIUS_AGREEMENT_GATE', '1')
    page.get_by_role('button', name='Submit adjudication', exact=True).click()
    expect(page.get_by_role('link', name='Read and sign the agreement')).to_be_visible()
    expect(notes).to_have_value('Use a safer potassium bath.')
    expect(fork).to_have_attribute('aria-checked', 'true')
    assert not portal.store.reviews_for_task(tid)
    _sign(portal)
    page.get_by_role('button', name='Submit adjudication', exact=True).click()
    expect(page.get_by_role('button', name='Check again', exact=True)).to_be_visible()
    reviews = portal.store.reviews_for_task(tid)
    assert reviews and reviews[0]['reviewer_notes'] == 'Use a safer potassium bath.'
    assert reviews[0]['step_divergence'][0]['judged'] in ('A', 'B')
    assert not portal.errors
