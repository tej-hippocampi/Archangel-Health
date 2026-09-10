"""Chart Walk P4: exact parser regressions and walk endpoint rules."""
import asyncio
import copy
from pathlib import Path

from asclepius import real_cases as RC
from asclepius.adapters import note_text
from asclepius.cases import _lab_result_ok
from tests.test_asclepius_longitudinal_e2e import build_chart

NOTES = Path(__file__).resolve().parents[1] / 'asclepius/fixtures/patient_bundles/patient-4/clinical-notes'


def test_discharge_a_keeps_only_the_two_opening_history_sentences():
    raw = (NOTES / '115_2024-12-19_discharge-summary.txt').read_text()
    presentation, resolution = RC.split_discharge_summary(raw)
    assert presentation == (
        'Presenting Complaints: ABDOMINAL PAIN, NAUSEA, VOMITING\n'
        'History (opening of the hospital course): Known case of HTN, DM, and IHD. '
        'Presented with generalized and severe abdominal pain, nausea, and vomiting.')
    assert 'Diagnosis: DYSPEPSIA/AGE' in resolution
    assert 'TAB NEBIL 5MG' in resolution and 'TAB DAPA 10MG' in resolution
    assert 'Vitally stable on examination; baseline tests performed and managed accordingly.' in resolution


def test_discharge_b_presentation_and_order_only_continuation():
    first = (NOTES / '113_2025-01-23_discharge-summary.txt').read_text()
    continuation = (NOTES / '114_2025-01-23_discharge-summary.txt').read_text()
    presentation, resolution = RC.split_discharge_summary(first)
    assert 'SHORTNESS OF BREATH' in presentation and 'FLANK PAIN' in presentation
    assert 'DKA' not in presentation and 'shifted to ICU' not in presentation
    assert 'DIABETIC KETOACIDOSES (DKA)' in resolution
    presentation, resolution = RC.split_discharge_summary(continuation)
    assert presentation is None
    assert 'INJ LANTUS 10 UNITS AT 8PM' in resolution
    assert 'Discharge status: Improved' in resolution


def test_drug_set_nulls_misdated_page_but_keeps_genuine_two_year_old_consult():
    raw = (NOTES / '158_2022-03-04_prescription-medication.txt').read_text()
    # Structured activity starts day -406; -1429 is inside the old three-year
    # cutoff. Only the matching later drug set can identify the misdated page.
    old = {'note_type': 'Orders', 'text': raw, 'collected_offset_days': -1429}
    consult = {'note_type': 'Consult', 'collected_offset_days': -1136,
               'text': 'Cardiology consult: exertional dyspnea and fatigue. Examination and risk factors reviewed. '
                       'Plan serial imaging and specialist follow-up; no medication orders at this visit.'}
    orders = '\n'.join(line for line in raw.splitlines() if line.startswith('- '))
    chart = {'lab_panels': [{'collected_offset_days': -406, 'results': []}],
             'notes': [old, consult, {'note_type': 'Orders', 'text': orders, 'collected_offset_days': -300}],
             'medications': [{'drug': 'Tanzo', 'collected_offset_days': -1429}],
             'problem_list': [{'condition': 'Recorded on misdated page', 'since': 'day -1429',
                               'collected_offset_days': -1429}]}
    before = copy.deepcopy(chart)
    curated = RC.prepare_longitudinal_chart(chart)
    assert curated['notes'][0]['collected_offset_days'] is None
    assert curated['notes'][0]['withheld_reason'] == 'implausible_date'
    assert curated['notes'][0]['model_visible'] is False
    assert curated['notes'][1]['collected_offset_days'] == -1136
    assert curated['medications'][0]['collected_offset_days'] is None
    assert curated['problem_list'][0]['collected_offset_days'] is None
    assert curated['problem_list'][0]['since'] is None
    assert chart == before


def test_unitless_inr_requires_range_or_flag():
    result = {'analyte': 'INR', 'value': 0.99, 'ref_low': 0.9, 'ref_high': 1.5}
    assert _lab_result_ok(result)
    assert not _lab_result_ok({'analyte': 'INR', 'value': 0.99})


def test_aggregate_notes_keep_their_document_dates():
    notes = note_text.parse((NOTES / 'clinical_and_icu_notes.txt').read_bytes(), specialty='cardiology')['notes']
    assert len(notes) == 37
    assert sum(bool(n.get('collected_at')) for n in notes) == 28


def test_exporter_synopsis_is_not_a_clinical_note():
    result = note_text.parse((NOTES / '00_clinical_summary.txt').read_bytes(), specialty='cardiology')
    assert result['notes'] == []


def test_trailing_sparse_interval_is_not_a_walk_endpoint():
    chart = build_chart()
    chart['notes'].append({'note_type': 'Progress', 'collected_offset_days': 0,
                           'text': 'Follow-up call: symptoms stable, repeat observations planned at the next visit.'})
    plan = asyncio.run(RC.plan_cases(chart, trajectory=True, derive_questions=False))
    assert plan['encounters'] == 6
    assert plan['walk_points'] == 5 and plan['walk_verifiable_points'] == 4
    last = plan['proposals'][-1]
    assert not last['qualifies_as_point'] and last['point_class'] is None
    assert any('terminal interval visit' in b for b in last['blockers'])


def test_density_qualified_terminal_without_narrative_closes_as_interval():
    chart = build_chart()
    for note in chart['notes']:
        if note['collected_offset_days'] >= -604:
            note['note_type'] = 'Radiology'
    plan = asyncio.run(RC.plan_cases(chart, trajectory=True, derive_questions=False))
    assert plan['walk_points'] == 5 and plan['walk_verifiable_points'] == 4
    assert plan['decision_points'] == 4 and plan['interval_points'] == 1
    assert plan['review_required_points'] == 0
    last = plan['proposals'][-1]
    assert last['density']['qualifies']
    assert last['qualifies_as_point'] and last['point_class'] == 'interval'
    assert last['downgraded'] and not last['presenting_narrative']
    assert last['generatable'] and not last['outcome_verifiable']
