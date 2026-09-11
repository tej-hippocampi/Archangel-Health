"""Execute chart-walk controls, including replan-before-generate and real holds."""
import pytest

from tests.test_asclepius_longitudinal_ui import ADMIN_JS, DOM_SHIM, _extract_function, _run_node


def _script(body):
    functions = '\n'.join(_extract_function(ADMIN_JS, n) for n in (
        'h', 'appendChildren', 'chartWalkSummary', 'renderDensityLine', 'renderProposalRow', 'openCasePlanModal',
        'previewLongitudinal', 'doneRow'))
    return f"""
require({str(DOM_SHIM)!r});
// Extend the minimal shim with the standard tag selectors used by the modal.
const elementProto = Object.getPrototypeOf(document.createElement('div'));
const originalMatches = elementProto._matches;
elementProto._matches = function(sel) {{
 return this.tagName.toLowerCase() === sel || originalMatches.call(this, sel);
}};
const calls = [];
const confirmations = [];
window.confirm = text => {{ confirmations.push(text); return true; }};
const _diffBadgeClass = () => '';
function clear(n) {{ n.textContent = ''; }}
function toast() {{}}
function errText(e) {{ return e.message; }}
function loadIngestionLists() {{}}
function load() {{}}
function loadingCard(text) {{ return h('div', {{}}, text); }}
function autoGenerateFailures() {{ return null; }}
function specialtyResolver(id, cb) {{ globalThis.chooseSpecialty = cb; return h('span', {{}}); }}
async function api(path, req) {{ calls.push({{path, ...req}}); return plan; }}
function all(n) {{ return [n, ...n.children.flatMap(all)]; }}
function checkbox() {{ return all(document.body).find(n=>n.tagName==='INPUT'); }}
function checked(n) {{ return n.checked === undefined ? n.hasAttribute('checked') : n.checked; }}
{functions}
const held = {{encounter_index:1, generatable:false, review_required:true,
 blockers:['Review required: conflicting source evidence'], qualifies_as_point:true, point_class:'decision'}};
const ready = {{encounter_index:6, generatable:true, qualifies_as_decision_point:true,
 qualifies_as_point:true, point_class:'decision'}};
const plan = {{specialty_hint:'cardiology', encounters:7, decision_points:3, generatable:7,
 walk_points:7, interval_points:4, ready_walk_points:7, walk_verifiable_points:6,
 ready_decision_points:3, review_required_points:0, proposals:[0,1,2,3,4,5,6].map(i=>({{
 ...ready, encounter_index:i, point_class:[0,1,6].includes(i)?'decision':'interval', outcome_verifiable:i<6,
 density:{{n_distinct_dates:2,n_events:8,n_resource_types:2}},
 qualifies_as_decision_point:[0,1,6].includes(i)
}}))}};
(async () => {{ {body} }})().catch(e => {{ console.error(e); process.exit(1); }});
"""


def test_held_row_has_no_generate_button():
    result = _run_node(_script("""
const row = renderProposalRow({ingest_case_id:'ic'}, held, null, true);
console.log(JSON.stringify({text:row.textContent, buttons:all(row).filter(n=>n.tagName==='BUTTON').length}));
"""))
    assert 'Held for evidence review' in result['text']
    assert result['buttons'] == 0


def test_per_point_generation_retains_trajectory_mode():
    result = _run_node(_script("""
const row = renderProposalRow({ingest_case_id:'ic'}, ready, null, true);
const button = all(row).find(n=>n.tagName==='BUTTON');
await button._listeners.click[0]({});
console.log(JSON.stringify(calls[0]));
"""))
    assert result['body'] == {'dry_run': False, 'trajectory': True, 'encounter_indices': [6],
                              'include_interval_points': True}


def test_modal_counts_all_walk_points_without_a_narrative_hold_warning():
    result = _run_node(_script("""
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {trajectory:true});
const buttons = all(document.body).filter(n=>n.tagName==='BUTTON').map(n=>n.textContent);
console.log(JSON.stringify({text:document.body.textContent,buttons}));
"""))
    assert '7 encounters · 7 points (3 decision · 4 interval) · 6 verifiable · 7 ready' in result['text']
    assert 'held for evidence review' not in result['text'].lower()
    assert 'Chain 7 point(s) into one trajectory' in result['buttons']
    assert 'Generate all 7 case(s)' in result['buttons']


def test_static_chain_control_replans_before_writing_tasks():
    result = _run_node(_script("""
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {});
const button = all(document.body).find(n=>n.tagName==='BUTTON' && n.textContent==='Preview chart walk');
await button._listeners.click[0]({});
console.log(JSON.stringify({calls}));
"""))
    assert len(result['calls']) == 1
    assert result['calls'][0]['body'] == {'dry_run': True, 'trajectory': True,
                                         'derive_questions': False, 'include_interval_points': True}


def test_specialty_change_after_static_chain_keeps_trajectory_replan():
    result = _run_node(_script("""
plan.specialty_hint = null;
let staticReplanned = false;
openCasePlanModal({}, {ingest_case_id:'ic',upload_id:'up'}, plan, h('div',{}),
 {replan:()=>{staticReplanned=true;}});
const button = all(document.body).find(n=>n.tagName==='BUTTON' && n.textContent==='Preview chart walk');
await button._listeners.click[0]({});
globalThis.chooseSpecialty();
await new Promise(resolve=>setImmediate(resolve));
console.log(JSON.stringify({calls,staticReplanned}));
"""))
    assert not result['staticReplanned']
    assert len(result['calls']) == 2
    assert all(call['body']['trajectory'] for call in result['calls'])


def test_completed_upload_keeps_read_only_plan_access():
    result = _run_node(_script("""
api = async (path, req) => {
 calls.push({path,...req});
 return path.endsWith('/generate') ? plan : {cases:[{ingest_case_id:'ic',status:'promoted'}]};
};
const row = doneRow({upload_id:'up', task_mode:'longitudinal',case_counts:{promoted:1}});
const review = all(row).find(n=>n.tagName==='BUTTON' && n.textContent==='Review chart plan');
review._listeners.click[0]({});
await new Promise(resolve=>setImmediate(resolve));
const buttons = all(document.body).filter(n=>n.tagName==='BUTTON' && /^(Chain|Generate)/.test(n.textContent));
console.log(JSON.stringify({calls,text:document.body.textContent,disabled:buttons.every(n=>n.hasAttribute('disabled'))}));
"""))
    assert 'Read-only chart review' in result['text']
    assert result['disabled']
    requests = [c for c in result['calls'] if c['path'].endswith('/generate')]
    assert requests[0]['body'] == {'dry_run': True, 'trajectory': True, 'derive_questions': False}


def test_other_evidence_holds_still_count_only_ready_points():
    result = _run_node(_script("""
plan.review_required_points = 2;
plan.ready_walk_points = 5;
plan.proposals = [held, ready];
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {trajectory:true});
console.log(JSON.stringify({text:document.body.textContent}));
"""))
    assert '2 point(s) held for evidence review · 5 ready to build' in result['text']
    assert 'Chain 5 point(s)' in result['text']
    assert 'predecessors' not in result['text']


def test_default_walk_generation_confirms_seven_points_at_75_each():
    result = _run_node(_script("""
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {trajectory:true});
const defaultChecked = checked(checkbox());
const button = all(document.body).find(n=>n.tagName==='BUTTON' && n.textContent.startsWith('Chain'));
await button._listeners.click[0]({});
console.log(JSON.stringify({defaultChecked,calls,confirmations}));
"""))
    assert result['defaultChecked']
    assert result['calls'][0]['body'] == {'dry_run': False, 'trajectory': True, 'include_interval_points': True}
    assert '7-point' in result['confirmations'][0]
    assert '6 of the 7' in result['confirmations'][0]
    assert '$525' in result['confirmations'][0]


@pytest.mark.parametrize('action', ['Chain', 'Generate all', 'Generate this'])
def test_toggle_replans_and_every_generation_path_preserves_the_selection(action):
    result = _run_node(_script("""
const filtered = {...plan, walk_points:3, ready_walk_points:3, interval_points:0,
 walk_verifiable_points:2, proposals:plan.proposals.filter(p=>p.point_class==='decision')};
api = async (path, req) => { calls.push({path,...req}); return filtered; };
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {trajectory:true});
const toggle = checkbox();
toggle.checked = false;
await toggle._listeners.change[0]({});
const text = document.body.textContent;
const isChecked = checked(checkbox());
const button = all(document.body).find(n=>n.tagName==='BUTTON' && n.textContent.startsWith(ACTION));
await button._listeners.click[0]({});
console.log(JSON.stringify({calls,text,isChecked,confirmations}));
""".replace('ACTION', repr(action))))
    assert not result['isChecked']
    assert '7 encounters · 3 points (3 decision · 0 interval) · 2 verifiable · 3 ready' in result['text']
    assert len(result['calls']) == 2
    assert result['calls'][0]['body'] == {'dry_run': True, 'trajectory': True,
        'derive_questions': False, 'include_interval_points': False}
    assert result['calls'][1]['body']['dry_run'] is False
    assert result['calls'][1]['body']['trajectory'] is True
    assert result['calls'][1]['body']['include_interval_points'] is False
    if action == 'Chain':
        assert '$225' in result['confirmations'][0]


def test_failed_toggle_blocks_stale_generation_and_restores_the_old_plan():
    result = _run_node(_script("""
let reject;
api = () => new Promise((resolve, fail) => { reject = fail; });
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {trajectory:true});
const toggle = checkbox();
toggle.checked = false;
const pending = toggle._listeners.change[0]({});
const buttons = all(document.body).filter(n=>n.tagName==='BUTTON');
const whilePending = buttons.every(n=>n.hasAttribute('disabled')) && toggle.hasAttribute('disabled');
reject(new Error('preview unavailable'));
await pending;
console.log(JSON.stringify({whilePending,isChecked:checked(toggle),text:document.body.textContent,
 restored:buttons.every(n=>!n.hasAttribute('disabled'))}));
"""))
    assert result['whilePending'] and result['restored'] and result['isChecked']
    assert '7 points (3 decision · 4 interval)' in result['text']


def test_read_only_toggle_stays_read_only_after_replanning():
    result = _run_node(_script("""
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {trajectory:true,reviewOnly:true});
const toggle = checkbox();
toggle.checked = false;
await toggle._listeners.change[0]({});
const generation = all(document.body).filter(n=>n.tagName==='BUTTON' && /^(Chain|Generate)/.test(n.textContent));
console.log(JSON.stringify({calls,text:document.body.textContent,disabled:generation.every(n=>n.hasAttribute('disabled'))}));
"""))
    assert result['disabled']
    assert 'Read-only chart review' in result['text']
    assert len(result['calls']) == 1 and result['calls'][0]['body']['dry_run'] is True


def test_specialty_replan_keeps_the_interval_setting():
    result = _run_node(_script("""
plan.specialty_hint = null;
openCasePlanModal({upload_id:'up'}, {ingest_case_id:'ic',upload_id:'up'}, plan,
 h('div',{}), {trajectory:true,includeIntervalPoints:false});
globalThis.chooseSpecialty();
await new Promise(resolve=>setImmediate(resolve));
console.log(JSON.stringify({calls}));
"""))
    assert len(result['calls']) == 1
    assert result['calls'][0]['body']['include_interval_points'] is False
    assert result['calls'][0]['body']['trajectory'] is True


@pytest.mark.parametrize('action', ['Chain', 'Generate all', 'Generate this'])
def test_pending_generation_cannot_replan_or_start_another_write(action):
    result = _run_node(_script("""
let reject;
api = (path, req) => { calls.push({path,...req}); return new Promise((resolve, fail)=>{reject=fail;}); };
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {trajectory:true});
const buttons = all(document.body).filter(n=>n.tagName==='BUTTON');
const button = buttons.find(n=>n.textContent.startsWith(ACTION));
const pending = button._listeners.click[0]({});
const toggle = checkbox();
const whilePending = buttons.every(n=>n.hasAttribute('disabled')) && toggle.hasAttribute('disabled');
toggle.checked = false;
await toggle._listeners.change[0]({});
const other = buttons.find(n=>n!==button && n.textContent.startsWith('Generate'));
await other._listeners.click[0]({});
const overlay = document.body.querySelector('.call-team-overlay');
overlay._listeners.click[0]({target:overlay});
const retained = overlay.parentNode === document.body;
reject(new Error('generation unavailable'));
await pending;
console.log(JSON.stringify({calls,whilePending,retained,isChecked:checked(toggle),
 restored:buttons.every(n=>!n.hasAttribute('disabled')),overlays:document.body.querySelectorAll('.call-team-overlay').length}));
""".replace('ACTION', repr(action))))
    assert result['whilePending'] and result['retained'] and result['restored']
    assert result['isChecked'] and result['overlays'] == 1
    assert len(result['calls']) == 1
    assert result['calls'][0]['body']['dry_run'] is False
