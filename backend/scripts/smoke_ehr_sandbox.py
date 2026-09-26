#!/usr/bin/env python3
"""CI-only real-provider qualification on public synthetic upload-derived tasks.
No grading keys, reference plans, source worksheets or oracle enter model input.
This is an operational qualification, not a clinical benchmark score.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asclepius.ehr_sandbox.env import EhrVisitEnv
from asclepius.ehr_sandbox.harness import drive
from asclepius.ehr_sandbox.common import subject
from asclepius.ehr_sandbox.items import actual_items
from asclepius.ehr_sandbox.grader import note_check
from asclepius.ehr_sandbox.terminology import lab_group,daily_amount,drug,GROUPS
from ai.model_config import MODEL_REGISTRY,OPENAI_MODEL

INSTRUCTION='''\nOperational qualification on entirely synthetic records: first search for the patient using the MRN in this task, confirm the patient with get_patient, list encounters, review laboratory observations, conditions, medications, allergies, orders and documents, and read at least one prior document. Use calculate to convert a retrieved creatinine from mg/dL to umol/L. This conversion is the only calculation needed for this operational test. Place a BMP order for 7 days from now and an office follow-up for 28 days from now. Write a visit note grounded in what you retrieved, including that order and follow-up. These two scheduling actions are prescribed for this software test. Finish the visit. Do not change medications for this operational qualification.'''
REQUIRED={'search_patients','get_patient','list_encounters','search_observations','search_conditions','search_medications',
          'search_allergies','search_orders','search_documents','read_document','calculate','create_lab_order',
          'schedule_follow_up','write_visit_note','finish_visit'}

def calculation_grounding(calls,observations,target):
    """Every numeric clinical input must come from earlier returned evidence."""
    fields={'creatinine_mg_dl':('creatinine','mg/dL'),'egfr':('eGFR','mL/min/1.73m2'),
            'uacr_mg_g':('UACR','mg/g'),'cystatin_c_mg_l':('cystatin C','mg/L'),
            'weight_kg':('weight','kg'),'height_cm':('height','cm')}
    codes_by_group={**GROUPS,'weight':{'29463-7'},'height':{'8302-2'}}
    values={k:set() for k in (*fields,'age','sex')};quantities=set()
    required=False;grounded=[]
    def unit(value):return str(value).replace('µ','u').replace('μ','u').replace('²','2')
    for call,result in zip(calls,observations):
        if call['tool']=='get_patient' and result.get('id')==target:
            if result.get('gender'):values['sex'].add(result['gender'])
            if result.get('birthDate'):
                dob=datetime.fromisoformat(result['birthDate']).date();today=datetime(2031,3,3).date()
                values['age'].add(today.year-dob.year-((today.month,today.day)<(dob.month,dob.day)))
        if call['tool']=='search_observations':
            for entry in result.get('entry',[]):
                r=entry['resource'];q=r.get('valueQuantity',{});codes={c.get('code') for c in r.get('code',{}).get('coding',[])}
                if subject(r)!=target or not isinstance(q.get('value'),(int,float)):continue
                for field,(group,expected_unit) in fields.items():
                    if (codes & codes_by_group.get(group,set()) or (group=='cystatin C' and r.get('code',{}).get('text','').strip().lower()=='cystatin c')) and unit(q.get('unit'))==expected_unit:
                        values[field].add(q['value']);quantities.add((group.lower(),unit(q.get('unit')),q['value']))
        if call['tool']=='calculate':
            args=call['input'];inputs=args.get('inputs',{});formula=args.get('formula')
            if formula=='unit_convert':
                good=(str(inputs.get('analyte','')).lower(),unit(inputs.get('from_unit')),inputs.get('value')) in quantities
                required=required or (good and inputs.get('analyte')=='creatinine' and inputs.get('from_unit')=='mg/dL' and unit(inputs.get('to_unit'))=='umol/L')
                if good:quantities.add((inputs['analyte'].lower(),unit(result.get('unit')),result.get('value')))
            else:good=bool(inputs) and all(v in values.get(k,set()) for k,v in inputs.items())
            grounded.append(good)
            if good and str(formula).startswith('egfr_') and isinstance(result.get('value'),(int,float)):values['egfr'].add(result['value'])
    return required,bool(grounded) and all(grounded)

async def episode(task,model,protocol):
    task={**task,'instruction':task['instruction']+INSTRUCTION}
    env=EhrVisitEnv(task,max_steps=35)
    provider=await asyncio.wait_for(drive(env,model=model,harness=protocol),timeout=360)
    rollout=env.rollout()
    calls=[r for r in rollout['trajectory'] if r['type']=='tool_call']
    observations=[json.loads(r['content']) for r in rollout['trajectory'] if r['type']=='observation']
    errors=[r for r in observations if r.get('resourceType')=='OperationOutcome']
    used={c['tool'] for c in calls};kinds={r['resourceType'] for r in rollout['overlay']}
    target=env.sandbox.target_patient_id
    grounded_calculation,all_calculations_grounded=calculation_grounding(calls,observations,target)
    snapshot=list(env.sandbox._snapshot.values())
    documentation=note_check(snapshot,rollout['overlay'],target,actual_items(snapshot,rollout['overlay'],target),None,rollout['calculations'])
    checks={'real_provider':provider in ('anthropic','openai'),'finished':rollout['terminated_by']=='finish_visit',
            'required_tools':REQUIRED<=used,'no_tool_errors':not errors,'no_rejected_writes':not rollout['rejected_writes'],
            'target_only':not rollout['wrong_patient'] and all(subject(r)==env.sandbox.target_patient_id for r in rollout['overlay']),
            'persisted_actions':{'ServiceRequest','Appointment','DocumentReference'}<=kinds,
            'bmp_order':any(r['resourceType']=='ServiceRequest' and lab_group(r.get('code',{}).get('text',''))=='BMP' and r.get('occurrenceDateTime','').startswith('2031-03-10') for r in rollout['overlay']),
            'office_follow_up':any(r['resourceType']=='Appointment' and r.get('appointmentType',{}).get('text')=='office' and r.get('start','').startswith('2031-03-31') for r in rollout['overlay']),
            'no_medication_changes':'MedicationRequest' not in kinds,'retrieved_calculation':grounded_calculation,'calculator_inputs_grounded':all_calculations_grounded,
            'document_consistent':documentation.get('consistency')==1,'document_grounded':documentation.get('grounding')==1}
    # Replay public actions into a clean episode; compare every output and state.
    replay=EhrVisitEnv(task,max_steps=35);replay.reset()
    actual=[replay.step({'tool':c['tool'],'input':c['input']})[0]['observation'] for c in calls]
    checks['deterministic_replay']=actual==observations and replay.rollout()['overlay']==rollout['overlay']
    env.reset();checks['reset_clears_writes']=not env.sandbox.overlay and env.calls==0
    return {'model':model,'provider':provider,'harness':protocol,'task_id':task['task_id'],'calls':len(calls),
            'checks':checks,'missing_tools':sorted(REQUIRED-used),'errors':errors,'document_diagnostics':documentation,'trajectory':rollout['trajectory']}

async def main(args):
    if os.getenv('CI')!='true' or os.getenv('ASCLEPIUS_LLM_PROVIDER','').strip():
        raise SystemExit('Real EHR smoke requires CI=true and the fake provider OFF')
    for name in ('ANTHROPIC_API_KEY','OPENAI_API_KEY'):
        if not os.getenv(name):raise SystemExit(name+' secret is missing')
    public=json.loads(Path(args.tasks).read_text())
    if public.get('synthetic_only') is not True:raise SystemExit('Refusing non-synthetic smoke inputs')
    tasks=public['tasks']
    if not tasks:raise SystemExit('No upload-derived synthetic tasks')
    results=[]
    # A small real extraction check exercises the upload-time model schema too.
    # Its worksheet never enters the separate agent episode input.
    worksheet='Current medications: lisinopril 20 mg once daily.\nAssessment: CKD stage 4 (N18.4).\nPlan: Decrease lisinopril to 10 mg once daily. Check BMP in 7 days. Follow up in 28 days.'
    # Sequential calls keep spending bounded and provider diagnostics attributable.
    for model in (os.getenv('EHR_SMOKE_ANTHROPIC_MODEL',MODEL_REGISTRY['ehr_agent']['model']),os.getenv('EHR_SMOKE_OPENAI_MODEL',OPENAI_MODEL)):
        try:
            from asclepius.ehr_sandbox.worksheet_extract import extract
            os.environ['MODEL_EHR_EXTRACT']=model
            key=await asyncio.wait_for(extract(worksheet),timeout=120)
            checks={'no_dropped_quotes':not key['extraction']['dropped'],
                    'current_regimen':any(drug(m['drug'])['name']=='lisinopril' and daily_amount(m['dose'],m['frequency'])=={'value':20,'unit':'mg'} for m in key.get('medications',[])),
                    'dose_change':any(m['action']=='decrease' and drug(m['drug'])['name']=='lisinopril' and daily_amount(m.get('to_dose'))=={'value':10,'unit':'mg'} for m in key['med_changes']),
                    'bmp_order':any(m.get('loinc_group')=='BMP' and m.get('timing_days')==7 for m in key['orders']),
                    'follow_up':(key.get('follow_up') or {}).get('interval_days')==28}
            result={'model':model,'harness':'worksheet_extraction','checks':checks}
        except Exception as exc:
            result={'model':model,'harness':'worksheet_extraction','checks':{'completed':False},'error_type':type(exc).__name__}
        results.append(result);print(json.dumps(result),flush=True)
        for index,protocol in enumerate(('native_tools','json_protocol')):
            try:
                result=await episode(tasks[index%len(tasks)],model,protocol)
            except Exception as exc:
                result={'model':model,'harness':protocol,'checks':{'completed':False},'error_type':type(exc).__name__}
            results.append(result)
            print(json.dumps({k:v for k,v in result.items() if k!='trajectory'}),flush=True)
            Path(args.output).write_text(json.dumps({'synthetic_only':True,'results':results},indent=2)+'\n')
    return 0 if all(all(r['checks'].values()) for r in results) else 1

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--tasks',required=True);parser.add_argument('--output',required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
