"""Revisiting a labeling step preserves blind data and the final correction."""
from copy import deepcopy
import asyncio

import pytest
from fastapi.testclient import TestClient
from tests._asclepius import app, fresh_store, make_user, headers_for
from asclepius.answer_revision import preserve_blind_answer
from asclepius.packaging import package_submission
from tests.test_asclepius_validation import _GOOD_PAYLOAD, _task, _submission


@pytest.mark.parametrize("verdict", ["valid", "flagged", "case_incoherent", "not_hard"])
def test_post_reveal_revision_survives_every_submission_path(verdict):
    store = fresh_store()
    user = make_user(store, specialty="nephrology")
    task = store.insert_task(prompt="Which action follows this potassium result?", specialty="nephrology",
        candidate_answers=[{"id": "A", "text": "Obtain an ECG."}, {"id": "B", "text": "Discharge."}])
    h = headers_for(user)
    client = TestClient(app)
    url = f"/api/asclepius/tasks/{task['task_id']}/reveal"
    revealed = client.post(url, headers=h, json={"text": "My initial answer", "portal_version": "v1"})
    assert revealed.status_code == 200, revealed.text
    original = revealed.json()["independent_answer"]
    retry = client.post(url, headers=h, json={"text": "Edited after a lost response", "portal_version": "v1"})
    assert retry.json()["independent_answer"] == original
    payload = deepcopy(_GOOD_PAYLOAD)
    payload.update(task_id=task["task_id"], submission_id="s-revision", portal_version="v1", time_spent_sec=120,
        prompt_review={"reviewed": True, "verdict": verdict, "attest_clinically_valid": True, "note": "The question lacks needed information."},
        independent_answer={"text": "My reconsidered answer"},
        independent_answer_revision={"text": "Client forgery", "capture_phase": "before_reveal"})
    if verdict != "valid":
        payload["chosen_revision"]["why_better_notes"] = "Contact clinician@example.org"
    response = client.post("/api/asclepius/submissions", headers=h, json=payload)
    assert response.status_code == 200, response.text
    saved = store.get_submission("s-revision")["payload"]
    assert saved["independent_answer"] == original
    assert saved["independent_answer_revision"]["text"] == "My reconsidered answer"
    assert saved["independent_answer_revision"]["capture_phase"] == "after_model_reveal"
    assert store.get_independent_commit(task["task_id"], user["id"])["payload"] == original
    if verdict in ("flagged", "case_incoherent"):
        assert saved["prompt_review"]["attest_clinically_valid"] is False
    if verdict != "valid":
        assert "clinician@example.org" not in str(saved)
        assert store.records_for_submission("s-revision") == []
    payload["independent_answer"]["text"] = "A later retry must not change the accepted answer"
    assert client.post("/api/asclepius/submissions", headers=h, json=payload).status_code == 200
    assert store.get_submission("s-revision")["payload"] == saved


def test_citation_only_revision_and_unchanged_draft():
    store = fresh_store()
    anchor = {"citation_text": "Guideline", "source_type": "guideline", "identifier": "KDIGO",
              "url": "https://example.org/old"}
    store.commit_independent_answer(task_id="t", evaluator_id="u", payload={"text": "Initial", "evidence_anchor": anchor})
    same = {"independent_answer": {"text": "Initial", "evidence_anchors": [anchor], "captured_at": "client-time"}}
    preserve_blind_answer(store, "t", "u", same)
    assert "independent_answer_revision" not in same
    revised = {"independent_answer": {"text": "Initial", "evidence_anchor": {**anchor, "url": "https://example.org/correct"}}}
    preserve_blind_answer(store, "t", "u", revised)
    assert revised["independent_answer_revision"]["evidence_anchor"]["url"].endswith("/correct")
    assert revised["independent_answer"]["evidence_anchor"]["url"].endswith("/old")
    uncommitted = {"independent_answer": {"text": "Unrevealed"}, "independent_answer_revision": {"text": "forged"}}
    preserve_blind_answer(store, "other", "u", uncommitted)
    assert "independent_answer_revision" not in uncommitted


def test_revision_is_visible_context_and_never_the_blind_training_target():
    from asclepius.label_view import submission_view_keys
    payload = deepcopy(_GOOD_PAYLOAD)
    payload["independent_answer_revision"] = {"text": "Reconsidered", "capture_phase": "after_model_reveal"}
    records = package_submission(_task(independent_mode="full"), _submission(payload))
    blind = next(r for r in records if r.get("independent"))
    assert blind["ideal_answer"] == payload["independent_answer"]["text"]
    assert all(r["independent_answer_revision"]["text"] == "Reconsidered" for r in records)
    assert "independent_answer_revision" in submission_view_keys()


def test_longitudinal_style_is_scoped_and_hashes_actual_prompt(monkeypatch):
    from asclepius import baselines, critic
    from asclepius.prompts import LONGITUDINAL_ANSWER_STYLE
    import ai.llm_client as llm
    calls = []
    async def fake(**kwargs):
        calls.append(kwargs)
        return object(), {"model": "test", "latency_ms": 1}
    monkeypatch.setattr(llm, "call_llm", fake)
    monkeypatch.setattr(llm, "first_text", lambda _: '{"candidate_answers":[{"id":"A","text":"Answer A"},{"id":"B","text":"Answer B"}],"intended_flawed_id":"B"}')
    asyncio.run(critic.generate_candidates_ex("Question", longitudinal=True))
    assert LONGITUDINAL_ANSWER_STYLE in calls[-1]["system"]
    asyncio.run(critic.generate_candidates_ex("Question"))
    assert LONGITUDINAL_ANSWER_STYLE not in calls[-1]["system"]
    store = fresh_store()
    monkeypatch.setattr(baselines, "_case_image_for_baseline", lambda task: (None, None, None))
    task = {"task_id": "t", "prompt": "Question", "trajectory_id": "walk"}
    longitudinal = asyncio.run(baselines.run_baselines(store, task, models=["model-x"]))
    assert LONGITUDINAL_ANSWER_STYLE in calls[-1]["system"]
    task.pop("trajectory_id")
    static = asyncio.run(baselines.run_baselines(store, task, models=["model-x"]))
    assert LONGITUDINAL_ANSWER_STYLE not in calls[-1]["system"]
    assert static[0]["prompt_hash"] != longitudinal[0]["prompt_hash"]


def _client_script(*functions):
    from tests.test_asclepius_eval_ui_overhaul import DOM_SHIM, JS, _extract_function
    return DOM_SHIM.read_text() + "\n" + "\n".join(_extract_function(JS, f) for f in functions)


def test_answer_formatting_keeps_clinical_text_and_annotation_offsets():
    from tests.test_asclepius_eval_ui_overhaul import _run_node
    out = _run_node(_client_script("h", "appendChildren", "appendFormattedAnswer", "splitSentences") + r'''
const raw = '**1. Potassium:** K+ 5.84 mmol/L. **Do not omit** glucose. 2 ** 3 = 8; gene__id__x; <script>alert(1)</script>';
const sents = splitSentences(raw);
const node = appendFormattedAnswer(document.createElement('div'), raw,
  {sents, shared: sents.map((_, i) => i === 0)}, ['5.84 mmol/L', '**Do not omit**']);
function collect(node, tag) { return node.children.filter(n=>n.tagName===tag).concat(node.children.flatMap(n=>collect(n,tag))); }
console.log(JSON.stringify({text:node.textContent, strong:collect(node,'STRONG').map(n=>n.textContent),
  marks:collect(node,'MARK').map(n=>n.textContent), scripts:collect(node,'SCRIPT').length, original:raw}));
''')
    assert out["text"] == '1. Potassium: K+ 5.84 mmol/L. Do not omit glucose. 2 ** 3 = 8; gene__id__x; <script>alert(1)</script>'
    assert "5.84 mmol/L" in out["marks"] and "Do not omit" in out["marks"]
    assert out["scripts"] == 0 and out["original"].startswith("**1.")
    assert "Potassium:" in "".join(out["strong"])


@pytest.mark.parametrize("force,navigate,mutate", [(True, False, False), (False, True, False), (False, False, True)])
def test_reasoning_request_respects_edits_and_back_navigation(force, navigate, mutate):
    from tests.test_asclepius_eval_ui_overhaul import _run_node
    script = _client_script("autoSplitChosen", "workspaceRequestIsCurrent") + '''
const state = {task:{prompt:'question'}, draft:{stage:'compare', chosen_id:'A'}, tutorial:null, _verdictRevision:1};
let steps = INITIAL;
let resolve;
function chosenRefinedText() { return 'Source answer'; }
function activeSteps() { return steps; }
function api() { return new Promise(r=>{resolve=r;}); }
function isV3() { return false; } function isAssisted() { return true; }
function newStep(text) { return {text}; }
function saveDraft() {} function repaintSteps() {} function updateSubmitState() {}
(async()=>{
 const pending=autoSplitChosen('none', FORCE);
 if (NAVIGATE) state.draft.stage='independent_answer';
 if (MUTATE) { state._verdictRevision++; steps=[{text:'new branch'}]; }
 resolve({steps:['Generated for original answer']}); await pending;
 console.log(JSON.stringify(steps));
})();
'''
    script = script.replace("INITIAL", '[{text:"Old split"}]' if force else '[]').replace("FORCE", str(force).lower()).replace("NAVIGATE", str(navigate).lower()).replace("MUTATE", str(mutate).lower())
    out = _run_node(script)
    assert out == [{"text": "new branch" if mutate else "Generated for original answer"}]


def test_rubric_response_cannot_cross_verdict_branches():
    from tests.test_asclepius_eval_ui_overhaul import _run_node
    out = _run_node(_client_script("seedRubric", "workspaceRequestIsCurrent") + '''
const state={task:{},tutorial:null,draft:{rubric:[],chosen_revision:{},rejected_critique:{},from_scratch:{}},_verdictRevision:1};
let resolve;
function tutorialActive(){return false;} function buildSubmissionPayload(){return {};}
function api(){return new Promise(r=>{resolve=r;});} function saveDraft(){}
function updateSubmitState(){} function repaintRubricUI(){}
(async()=>{const pending=seedRubric(false); state._verdictRevision++; state.draft.rubric=[];
resolve({criteria:[{text:'Criterion for A'}]}); await pending; console.log(JSON.stringify(state.draft.rubric));})();
''')
    assert out == []


def test_reviewer_renders_original_and_revision_with_their_own_sources():
    from tests.test_asclepius_eval_ui_overhaul import _run_node, _extract_function, JS_PATH
    source = JS_PATH.with_name("review.js").read_text()
    functions = "\n".join(_extract_function(source, f) for f in
        ("answerCard", "anchorsOf", "anchorLine", "grounded", "section", "verdictLabel", "stepsOf", "trajectory"))
    out = _run_node(_client_script("h", "appendChildren") + functions + r'''
const original={text:'Original blind answer',evidence_anchor:{citation_text:'Original source',url:'https://example.org/original'}};
const revision={text:'Reconsidered answer <img src=x>',evidence_anchor:{citation_text:'Corrected source',url:'https://example.org/corrected'}};
const card=answerCard({label:'A',answer:{independent_answer:original,independent_answer_revision:revision}}, {}, {}).card;
function all(n) {return [n,...n.children.flatMap(all)];}
console.log(JSON.stringify({text:card.textContent,links:all(card).filter(n=>n.tagName==='A').map(n=>n.attributes.href),images:all(card).filter(n=>n.tagName==='IMG').length}));
''')
    assert "Pre-reveal independent answer" in out["text"]
    assert "Revised after seeing the AI answers" in out["text"]
    assert "Original blind answer" in out["text"] and "Reconsidered answer" in out["text"]
    assert out["links"] == ["https://example.org/original", "https://example.org/corrected"]
    assert out["images"] == 0


def test_formatting_only_save_does_not_create_a_physician_correction():
    from tests.test_asclepius_eval_ui_overhaul import _run_node
    out = _run_node(_client_script("h", "appendChildren", "appendFormattedAnswer", "answerPlainText", "newDraft", "buildSubmissionPayload") + r'''
const state={servedVersion:'v3',task:{task_id:'t'}};
function randomId(){return 's-test';} function emptyAnchor(){return {};}
function getPortalVersion(){return 'v3';} function draftVersion(){return 'v3';}
function getElapsed(){return 30;} function cleanAnchor(a){return a;}
function anchorsForSubmit(){return [];} function assistData(){return null;}
function cleanSteps(s){return s;} function chosenText(){return '**Keep treatment.**';}
state.draft=newDraft(state.task); state.draft.verdict='A_better'; state.draft.chosen_id='A';
state.draft.chosen_revision.revised_text='Keep treatment.';
const untouched=buildSubmissionPayload().chosen_revision;
state.draft.chosen_revision.revised_text='Change treatment after checking potassium.';
console.log(JSON.stringify({untouched,edited:buildSubmissionPayload().chosen_revision}));
''')
    assert out["untouched"]["edited"] is False
    assert out["untouched"]["revised_text"] == "**Keep treatment.**"
    assert out["edited"]["edited"] is True
