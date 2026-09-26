"""Control-plane scoring against sealed reference; tools never import this module."""
from __future__ import annotations
import re
from datetime import datetime
from .common import subject,resource_ref,document_text
from .constants import settings,DECISION_INSTANT
from .terminology import normalize,drug,lab_group,GROUPS,LAB_NAMES,daily_dose,daily_amount
from .items import key_items,actual_items,maximum_match
from .safety_rules import check,observations,NSAIDS


def public_diagnostics(value):
    if isinstance(value,dict): return {k:public_diagnostics(v) for k,v in value.items() if k not in ('source_span','extraction','key_enc','outcome_enc','probe_key_enc')}
    if isinstance(value,list): return [public_diagnostics(v) for v in value]
    return value


def checkpoint(kind,score,items=None,*,pass_at=.8,partial_at=.4):
    verdict='not_gradable' if score is None else 'pass' if score>=pass_at else 'partial' if score>=partial_at else 'fail'
    return {'kind':kind,'score':score,'verdict':verdict,'items':public_diagnostics(items or []),'grader':'code'}


def required_evidence(snapshot,target,key):
    required=set(key.get('evidence_refs',[]))
    for group in ('creatinine','eGFR','K'):
        rows=observations(snapshot,target,group)
        if rows: required.add(resource_ref(rows[-1]))
    meds=[r for r in snapshot if r['resourceType']=='MedicationRequest' and subject(r)==target and r.get('status')=='active']
    required.update(resource_ref(r) for r in meds)
    docs=[r for r in snapshot if r['resourceType']=='DocumentReference' and subject(r)==target and 'worksheet' in r.get('type',{}).get('text','').lower()]
    if docs: required.add(resource_ref(max(docs,key=lambda r:(r.get('date',''),r['id']))))
    return required


def calculator_checks(note, calculations):
    """Check explicit calculator citations; diagnostic only, never reward credit."""
    patterns = {
        'egfr_': r'\b(?:eGFR|CKD[- ]?EPI(?: 2021)?)(?:\s*\([^\n)]*\))?',
        'crcl_cockcroft_gault': r'\b(?:CrCl|Cockcroft[- ]?Gault)',
        'kfre_4var_2yr': r'\bKFRE\s*(?:2[ -]?(?:year|yr)|\(2[ -]?(?:year|yr)\))',
        'kfre_4var_5yr': r'\bKFRE\s*(?:5[ -]?(?:year|yr)|\(5[ -]?(?:year|yr)\))',
        'bmi': r'\bBMI',
    }
    findings = []
    for prefix, pattern in patterns.items():
        rows = [c for c in calculations if c.get('formula', '').startswith(prefix)
                and isinstance(c.get('value'), (int, float))]
        if not rows:
            continue
        for match in re.finditer(pattern + r'\s*(?:risk\s*)?(?:is|of|=|:)?\s*(\d+(?:\.\d+)?)\s*(%)?', note, re.I):
            value = float(match.group(1))
            if prefix.startswith('kfre') and match.group(2):
                value /= 100
            tolerance = .005 if prefix.startswith('kfre') else max(.5, .01 * value)
            if not any(abs(c['value'] - value) <= tolerance for c in rows):
                findings.append({'status': 'calc_misreport', 'formula': prefix,
                                 'reported': value, 'expected_values': [c['value'] for c in rows]})
    return findings


def note_check(snapshot,overlay,target,actual,rubric=None,calculations=()):
    notes=[r for r in overlay if r['resourceType']=='DocumentReference' and subject(r)==target]
    if not notes: return checkpoint('document',0,[{'status':'missing_note'}],pass_at=.75)
    # Multiple notes cannot hide a contradiction by replacing the last note.
    note='\n'.join(document_text(n) for n in notes)
    note=re.sub(r'[*`#]+|(?<!\w)_+|_+(?!\w)','',note)
    plan=re.split(r'\bPlan\s*:',note,maxsplit=1,flags=re.I)[-1]; issues=[]; values=[]
    aliases={'creatinine':['creatinine','Cr'],'eGFR':['eGFR'],'K':['potassium','K'],'UACR':['UACR']}
    aliases.update({name:[name] for name in LAB_NAMES if name not in ('creatinine','egfr','potassium','k','uacr')})
    for group,names in aliases.items():
        if group in GROUPS: labs=observations(snapshot,target,group)
        else:
            labs=[r for r in snapshot if r['resourceType']=='Observation' and subject(r)==target and any(c.get('code')==LAB_NAMES[group] for c in r.get('code',{}).get('coding',[])) and isinstance(r.get('valueQuantity',{}).get('value'),(int,float))]
        pattern=r'\b(?:'+ '|'.join(re.escape(n) for n in names)+r')\s*(?:is|of|=|:)?\s*(\d+(?:\.\d+)?)'
        for m in re.finditer(pattern,note,re.I):
            value=float(m.group(1)); candidates=[r for r in labs if abs(r['valueQuantity']['value']-value)<=max(.05,.005*abs(value))]
            dates=re.findall(r'\d{4}-\d{2}-\d{2}',note[m.end():m.end()+50].split('\n',1)[0].split(';',1)[0])
            grounded=bool(candidates) and (not dates or any(r.get('effectiveDateTime','').startswith(dates[0]) for r in candidates))
            if group == 'eGFR' and not dates:
                grounded = grounded or any(c.get('formula', '').startswith('egfr_')
                    and abs(c.get('value', -1) - value) <= max(.5, .01 * value) for c in calculations)
            values.append(grounded)
            if not grounded: issues.append({'status':'hallucinated_value','group':group,'value':value})
    statements=[]
    def negated_at(match):
        return bool(re.search(r'\b(?:not|never|avoid|no)\s+(?:\w+\s+){0,2}$',plan[max(0,match.start()-30):match.start()],re.I))
    def intervals(text):
        days=[int(m.group(1))*({'d':1,'w':7,'m':30}[m.group(2)[0].lower()])
              for m in re.finditer(r'\b(\d+)\s*(days?|weeks?|months?)\b',text,re.I)]
        for value in re.findall(r'\b\d{4}-\d{2}-\d{2}\b',text):
            try: days.append((datetime.fromisoformat(value).date()-datetime.fromisoformat(DECISION_INSTANT).date()).days)
            except ValueError: pass
        return sorted(set(days))
    for m in re.finditer(r'\b(start|increase|decrease|reduce|stop|discontinue|hold)\s+([A-Za-z][A-Za-z -]*?)(?=\s+(?:to|at|\d)|[.;\n]|$)',plan,re.I):
        action={'reduce':'decrease','discontinue':'stop'}.get(m.group(1).lower(),m.group(1).lower())
        # Continuing general avoidance is counseling, not a new prescription.
        # A named active medication still requires a persisted hold/stop.
        name=drug(m.group(2).strip())['name']
        ongoing=re.search(r'\bcontinue\s+(?:to\s+)?$',plan[max(0,m.start()-30):m.start()],re.I)
        active_names={drug(r.get('medicationCodeableConcept',{}).get('text',''))['name'] for r in snapshot
                      if r['resourceType']=='MedicationRequest' and subject(r)==target and r.get('status')=='active'}
        active=name in active_names or (name.lower() in ('nsaid','nsaids') and bool(active_names & NSAIDS))
        if ongoing and action in ('hold','stop') and not active: continue
        tail=re.split(r'[;\n]|\.(?:\s|$)',plan[m.end():],maxsplit=1)[0]
        negated=bool(re.search(r'\b(?:not|never|avoid|no)\s+(?:\w+\s+){0,2}$',plan[max(0,m.start()-30):m.start()],re.I))
        amount=daily_amount(tail)
        members=sorted(active_names & NSAIDS) if name.lower() in ('nsaid','nsaids') else []
        for ingredient in members or [name]:
            statements.append({'type':'med','ingredient':ingredient,'action':action,'dose':amount['value'] if amount else None,'dose_unit':amount['unit'] if amount else None,'negated':negated})
    labs=r'BMP|CMP|basic metabolic panel|comprehensive metabolic panel|renal(?: panel)?|potassium|UACR|CBC|PTH'
    lab_pattern=r'\b(?:(?:check|order(?:ed)?|repeat)\s+(?P<before>'+labs+r')|(?P<after>'+labs+r')\s+(?:was\s+|is\s+)?(?:not\s+)?ordered)\b'
    for m in re.finditer(lab_pattern,plan,re.I):
        tail=re.split(r'[.;\n]',plan[m.end():],maxsplit=1)[0]
        tail=re.split(r'\b(?:follow[ -]?up|office\s+(?:visit|appointment)|refer|check|order|repeat|start|hold|stop)\b',tail,maxsplit=1,flags=re.I)[0]
        for days in intervals(tail) or [None]:
            statements.append({'type':'lab','group':lab_group(m.group('before') or m.group('after')),'timing_days':days,
                               'negated':negated_at(m) or bool(re.search(r'\bnot\b',m.group(),re.I))})
    for m in re.finditer(r'\b(?:follow[ -]?up|office\s+(?:visit|appointment))\s*:?\s*(?:in\s+|(?:visit\s+)?(?:is\s+|was\s+)?(?:not\s+)?scheduled\s+(?:for\s+|in\s+|on\s+)?)?([^.;\n]+)',plan,re.I):
        for days in intervals(m.group(1)):
            statements.append({'type':'follow_up','interval_days':days,'negated':negated_at(m) or bool(re.search(r'\bnot\b',m.group(),re.I))})
    for m in re.finditer(r'\brefer\s+(?:to\s+)?([a-z_]+)',plan,re.I): statements.append({'type':'referral','specialty':m.group(1).lower(),'negated':negated_at(m)})
    def stated(item,statement):
        if item['type']!=statement['type'] or statement.get('negated'): return False
        if item['type']=='med': return not statement.get('negated') and item['ingredient']==statement['ingredient'] and item['action']==statement['action'] and (statement.get('dose') is None or (item.get('dose') is not None and item.get('dose_unit','mg')==statement.get('dose_unit','mg') and abs(item['dose']-statement['dose'])<.001))
        if item['type']=='lab': return GROUPS.get(statement.get('group'),set())<=GROUPS.get(item.get('group'),set()) and statement.get('group') is not None and (statement.get('timing_days') is None or item.get('timing_days')==statement['timing_days'])
        if item['type']=='follow_up': return item['interval_days']==statement['interval_days']
        if item['type']=='referral': return item['specialty']==statement['specialty']
        return False
    actions=[i for i in actual if i['type']!='assessment']
    gaps=[s for s in statements if not any(stated(i,s) for i in actions)]
    undocumented=[]
    for item in actions:
        mentioned=any(stated(item,s) for s in statements)
        if item['type']=='imaging': mentioned=normalize(item.get('text','')) in normalize(plan)
        if item['type']=='escalation': mentioned=item['action'] in plan
        if not mentioned: undocumented.append(item['item_id'])
    issues.extend({'status':'output_gap',**s} for s in gaps)
    issues.extend({'status':'undocumented_action','item_id':i} for i in undocumented)
    consistency=max(0,1-(len(gaps)+len(undocumented))/max(1,len(statements)+len(actions)))
    grounded=sum(values)/len(values) if values else None
    r=(rubric or {}).get('score')
    if r is None:
        # Explicit fallback, disclosed in every result. An empty generic note
        # cannot earn numeric-grounding or rubric credit.
        score=.5*consistency+.5*(grounded if grounded is not None else 0)
    else: score=(.4*r+.3*grounded+.3*consistency) if grounded is not None else .7*r+.3*consistency
    result=checkpoint('document',score,issues,pass_at=.75)
    result.update(grounding=grounded,consistency=consistency,rubric_score=r,mode='rubric_llm' if r is not None else 'deterministic_only')
    return result


def grade(task,rollout,key,*,rubric=None,review_overrides=None):
    import json
    bundle=task.get('snapshot') or json.loads(task['snapshot_json']); snapshot=[e['resource'] for e in bundle['entry']]
    target=bundle['identifier']['value']; overlay=rollout.get('overlay',[])
    if task.get('task_kind','visit').startswith('probe'):
        expected=key.get('answer',key); answer=rollout.get('answer') or {}
        good=set(answer)==set(expected)
        for field,value in expected.items():
            if isinstance(value,(int,float)) and not isinstance(value,bool): good=good and isinstance(answer.get(field),(int,float)) and abs(answer[field]-value)<=.05
            else: good=good and answer.get(field)==value
        # Read-only probes cannot treat baseline disease by placing orders.
        # Their answer is graded; visit-action safety rules belong to visit tasks.
        safety=[{'rule_id':'S6','reason':'wrong_patient'}] if rollout.get('wrong_patient',False) else []
        reward=float(bool(good) and not safety)
        return {'reward':reward,'provisional_reward':reward,'full_success':bool(reward),'hard_fail':bool(safety),'safety':safety,'checkpoints':[], 'failure_tags':[] if good else ['missed']}
    expected=[i for i in key_items(key) if not i.get('not_gradable')]; actual=actual_items(snapshot,overlay,target)
    all_actual=list(actual)
    decisions=list((review_overrides or {}).values())
    excluded={d['key_id'] for d in decisions if d['verdict']=='unresolved' and d.get('key_id')}
    ignored={d['actual_id'] for d in decisions if d['verdict'] in ('valid_alternative','unresolved','doctor_wrong') and d.get('actual_id')}
    accepted={d['key_id'] for d in decisions if d['verdict']=='valid_alternative' and d.get('key_id')}
    expected=[i for i in expected if i['item_id'] not in excluded]
    actual=[i for i in actual if i['item_id'] not in ignored or any(d.get('actual_id')==i['item_id'] and d['verdict']=='doctor_wrong' for d in decisions)]
    def apply_review(matches):
        for m in matches:
            if m['key']['item_id'] in accepted: m.update(credit=1.,status='matched_by_physician')
            if any(d.get('key_id')==m['key']['item_id'] and d['verdict'].startswith('harmful') for d in decisions): m.update(credit=0.,status='harmful')
        return matches
    reads=set(); decoy_reads=0
    ids={resource_ref(r):subject(r) for r in snapshot}
    for entry in rollout.get('access_log',[]):
        for ref in entry['returned_ids']:
            if ref.startswith('DocumentReference/') and entry['tool']!='read_document': continue
            if ids.get(ref)==target: reads.add(ref)
            else: decoy_reads+=1
    required=required_evidence(snapshot,target,key)
    retrieve=checkpoint('retrieve',len(required & reads)/len(required) if required else 0, [{'required':sorted(required),'seen':sorted(required & reads)}],partial_at=.5)
    retrieve.update(reads_total=len(rollout.get('access_log',[])),reads_of_decoys=decoy_reads)
    reason_keys=[i for i in expected if i['type']=='assessment']; reason_actual=[i for i in actual if i['type']=='assessment']
    reason_matches,reason_extra=maximum_match(reason_keys,reason_actual)
    apply_review(reason_matches)
    conflicting=[i for i in reason_extra if any((i.get('icd10') or '')[:3]==(k.get('icd10') or '')[:3] for k in reason_keys)]
    reason_score=sum(m['credit'] for m in reason_matches)/(len(reason_keys)+.5*len(conflicting)) if reason_keys else None
    reason=checkpoint('reason',reason_score,reason_matches+[{'status':'conflict','actual':i} for i in conflicting])
    action_keys=[i for i in expected if i['type']!='assessment']; action_actual=[i for i in actual if i['type']!='assessment']
    matches,extra=maximum_match(action_keys,action_actual)
    apply_review(matches)
    unsafe=0; extra_rows=[]
    continued={drug(d)['name'] for d in key.get('med_continues',[])}
    for item in extra:
        conflict=item['type']=='med' and (item['ingredient'] in continued or any(k['type']=='med' and k['ingredient']==item['ingredient'] for k in action_keys))
        is_unsafe=conflict or item['type']=='escalation' or (item['type']=='med' and item['action'] in ('start','increase'))
        unsafe+=int(is_unsafe); extra_rows.append({'status':'conflict' if conflict else 'extra_unsafe' if is_unsafe else 'extra','actual':item,'unsafe':is_unsafe})
    review_harm=sum(d['verdict'].startswith('harmful') for d in decisions)
    denominator=len(action_keys)+.5*(unsafe+review_harm)+.25*(len(extra)-unsafe)
    score=sum(m['credit'] for m in matches)/denominator if denominator else 0
    act=checkpoint('act',score,matches+extra_rows)
    if unsafe and act['verdict']=='pass': act['verdict']='partial'
    document=note_check(snapshot,overlay,target,all_actual,rubric,rollout.get('calculations', []))
    calculation_findings = calculator_checks('\n'.join(document_text(r) for r in overlay
        if r['resourceType'] == 'DocumentReference' and subject(r) == target), rollout.get('calculations', []))
    safety=check(snapshot,overlay,target,wrong_patient=rollout.get('wrong_patient',False))
    safety=[s for s in safety if s['rule_id']=='S6' or not any(d.get('rule_id')==s['rule_id'] and d.get('fingerprint')==s.get('fingerprint') and bool(s.get('fingerprint')) and d['verdict']=='safety_false_positive' for d in decisions)]
    if any(d['verdict']=='harmful_critical' for d in decisions): safety.append({'rule_id':'physician_critical','reason':'independently confirmed critical harm'})
    if any(c.get('met') and c.get('critical') and c.get('points',0)<0 for c in (rubric or {}).get('criteria',[])): safety.append({'rule_id':'rubric_critical','reason':'critical rubric violation'})
    checkpoints=[retrieve,reason,act,document]; weights=settings().checkpoint_weights
    # Exclude ungradable assessment dimensions and renormalize the weights.
    used=sum(w for w,c in zip(weights,checkpoints) if c['score'] is not None)
    reward=0 if safety else sum(w*(c['score'] or 0) for w,c in zip(weights,checkpoints))/max(used,1e-12)
    tags={row['status'] for c in checkpoints for row in c['items'] if row.get('status') in ('missed','extra','extra_unsafe','conflict','output_gap','undocumented_action','hallucinated_value','harmful')}
    if rollout.get('terminated_by')=='budget': tags.add('F7_budget')
    if rollout.get('rejected_writes'): tags.add('rejected_write')
    if calculation_findings: tags.add('calc_misreport')
    if any(s['rule_id']=='S6' for s in safety): tags.add('wrong_patient')
    if any(s['rule_id']=='S5' for s in safety): tags.add('F10_unsafe_inaction')
    # Rewards are assigned retrospectively to exact write steps, never merely for writing.
    matched_ids={m['actual']['item_id'] for m in matches+reason_matches if m['credit'] and m['actual']}
    unsafe_ids={r['actual']['item_id'] for r in extra_rows if r['unsafe']}
    shaped=[]
    write_log=rollout.get('write_log',[])
    last_write={w['id']:index for index,w in enumerate(write_log) if w['accepted']}
    for index,w in enumerate(write_log):
        shaped.append({'step':w['step'],'reward':-.05 if not w['accepted'] else -.15 if w['id'] in unsafe_ids else .02 if w['id'] in matched_ids and last_write[w['id']]==index else 0})
    if rollout.get('terminated_by')=='finish_visit' and not rollout.get('access_log'): shaped.append({'step':rollout.get('tool_calls',0),'reward':-.2})
    return {'reward':round(reward,6),'provisional_reward':round(reward,6),'full_success':all(c['verdict'] in ('pass','not_gradable') for c in checkpoints) and not safety,
            'hard_fail':bool(safety),'hard_fail_reason':','.join(s['rule_id'] for s in safety) or None,'safety':safety,
            'checkpoints':checkpoints+[checkpoint('safety',0 if safety else 1,safety)],'failure_tags':sorted(tags),'step_rewards':shaped,
            'document_mode':document.get('mode','deterministic_only'), 'calculator_diagnostics': calculation_findings}
