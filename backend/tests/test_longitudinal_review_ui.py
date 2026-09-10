"""Execute the shipped admin controls: held points cannot expose Generate."""
from tests.test_asclepius_longitudinal_ui import ADMIN_JS, DOM_SHIM, _extract_function, _run_node


def _script(body):
    functions = '\n'.join(_extract_function(ADMIN_JS, n) for n in (
        'h', 'appendChildren', 'renderDensityLine', 'renderProposalRow', 'openCasePlanModal',
        'previewLongitudinal', 'doneRow'))
    return f"""
require({str(DOM_SHIM)!r});
const calls = [];
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
{functions}
const held = {{encounter_index:1, generatable:false, review_required:true,
 blockers:['Review required: missing encounter narrative'], qualifies_as_decision_point:true}};
const ready = {{encounter_index:7, generatable:true, qualifies_as_decision_point:true}};
const plan = {{specialty_hint:'cardiology', decision_points:3, generatable:1,
 ready_decision_points:1, review_required_points:2, proposals:[held, ready]}};
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
    assert result['body'] == {'dry_run': False, 'trajectory': True, 'encounter_indices': [7]}


def test_modal_counts_only_ready_points_in_build_control():
    result = _run_node(_script("""
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {trajectory:true});
const buttons = all(document.body).filter(n=>n.tagName==='BUTTON').map(n=>n.textContent);
console.log(JSON.stringify({text:document.body.textContent,buttons}));
"""))
    assert '2 decision point(s) held for evidence review · 1 ready to build' in result['text']
    assert 'Chain 1 decision point(s) into one trajectory' in result['buttons']
    assert 'Generate all 1 case(s)' in result['buttons']


def test_static_chain_control_replans_before_writing_tasks():
    result = _run_node(_script("""
openCasePlanModal({}, {ingest_case_id:'ic'}, plan, h('div',{}), {});
const button = all(document.body).find(n=>n.tagName==='BUTTON' && n.textContent.startsWith('Chain'));
await button._listeners.click[0]({});
console.log(JSON.stringify({calls}));
"""))
    assert len(result['calls']) == 1
    assert result['calls'][0]['body'] == {'dry_run': True, 'trajectory': True}


def test_specialty_change_after_static_chain_keeps_trajectory_replan():
    result = _run_node(_script("""
plan.specialty_hint = null;
let staticReplanned = false;
openCasePlanModal({}, {ingest_case_id:'ic',upload_id:'up'}, plan, h('div',{}),
 {replan:()=>{staticReplanned=true;}});
const button = all(document.body).find(n=>n.tagName==='BUTTON' && n.textContent.startsWith('Chain'));
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
