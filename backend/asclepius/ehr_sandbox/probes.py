"""Deterministic questions over the exact same pre-visit chart, never worksheets."""
import copy
import json
from datetime import datetime,timedelta
from .common import subject,identifier,dumps
from .constants import DECISION_INSTANT,audit_now
from .safety_rules import observations,renal_maximum,latest,renal_context
from .items import medication_dose
from .calculators import calculate
from .sealed import seal


def derive(task):
    bundle=task.get('snapshot') or json.loads(task['snapshot_json']); resources=[e['resource'] for e in bundle['entry']]
    target=bundle['identifier']['value']; anchor=datetime.fromisoformat(DECISION_INSTANT); result=[]
    egfr=observations(resources,target,'eGFR'); k=observations(resources,target,'K'); uacr=observations(resources,target,'UACR')
    recent=[r for r in egfr if datetime.fromisoformat(r['effectiveDateTime'])>=anchor-timedelta(days=730)]
    def add(kind,text,answer,**metadata): result.append({'kind':kind,'instruction':text,'answer':answer,**metadata})
    if len(recent)>=2 and recent[0]['valueQuantity']['value']>0:
        first,last=recent[0],recent[-1]; a,b=first['valueQuantity']['value'],last['valueQuantity']['value']
        add('probe_trend','Has eGFR declined by at least 40% from the earliest value in the last 24 months? Submit first_value, first_date, last_value, last_date, declined.',
            {'first_value':a,'first_date':first['effectiveDateTime'][:10],'last_value':b,'last_date':last['effectiveDateTime'][:10],'declined':b<=.6*a})
    if k:
        last=k[-1]
        add('probe_retrieval','What is the latest potassium before today? Submit value and date.',{'value':last['valueQuantity']['value'],'date':last['effectiveDateTime'][:10]})
    persistent={}
    for name,rows,formula,input_name in [('g_stage',egfr,'ckd_stage','egfr'),('a_stage',uacr,'uacr_category','uacr_mg_g')]:
        if len(rows)<2: continue
        last=rows[-1]; stage=calculate(formula,{input_name:last['valueQuantity']['value']})['value']
        for index,r in enumerate(rows[:-1]):
            if (datetime.fromisoformat(last['effectiveDateTime'])-datetime.fromisoformat(r['effectiveDateTime'])).days>90 and all(calculate(formula,{input_name:x['valueQuantity']['value']})['value']==stage for x in rows[index:]):
                persistent[name]=stage;break
    if len(persistent)==2: add('probe_persistence','Report the CKD G-stage and A-stage supported by values persisting over 90 days. Submit g_stage and a_stage.',persistent)
    for med in resources:
        if med['resourceType']!='MedicationRequest' or subject(med)!=target or med.get('status')!='active': continue
        name=med['medicationCodeableConcept']['text']; maximum=renal_maximum(name,**renal_context(resources,target)); dose=medication_dose(med)
        if maximum and not maximum.get('not_gradable') and dose is not None:
            ceiling=maximum['max_daily_mg']; adjusted=min(dose,ceiling)
            add('probe_dose',f'Is the current {name} dose appropriate for kidney function? Submit action (keep/reduce/stop) and daily_dose_mg.',
                {'action':'stop' if ceiling==0 else 'reduce' if dose>ceiling else 'keep','daily_dose_mg':adjusted},drug=name)
    return result


def compile_probes(task,*,store):
    ids=[]
    for index,probe in enumerate(derive(task)):
        tid=identifier('ehrp',[task['task_id'],probe['kind'],index]); ids.append(tid)
        if store.ehr_get('ehr_tasks',task_id=tid): continue
        row={k:task[k] for k in ('visit_id','env_version','split','seed','snapshot_json','budget_tool_calls')}
        row.update(task_id=tid,task_kind=probe['kind'],instruction=task['instruction']+'\nFor this probe, '+probe['instruction']+' Use submit_answer to finish.',
                   probe_key_enc=seal({'answer':probe['answer'],**({'drug':probe['drug']} if probe.get('drug') else {})}),tags_json=dumps(['F2','F4','F5']),created_at=audit_now())
        store.ehr_insert('ehr_tasks',row)
    return ids
