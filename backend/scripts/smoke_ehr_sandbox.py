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
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asclepius.ehr_sandbox.env import EhrVisitEnv
from asclepius.ehr_sandbox.harness import drive
from asclepius.ehr_sandbox.common import subject
from asclepius.ehr_sandbox.items import actual_items
from asclepius.ehr_sandbox.grader import note_check
from asclepius.ehr_sandbox.terminology import lab_group,daily_amount,drug

INSTRUCTION='''\nOperational qualification on entirely synthetic records: first search for the patient using the MRN in this task, confirm the patient with get_patient, list encounters, review laboratory observations, conditions, medications, allergies, orders and documents, and read at least one prior document. Use calculate to convert a retrieved creatinine from mg/dL to umol/L. Place a BMP order for 7 days from now and an office follow-up for 28 days from now. Write a visit note grounded in what you retrieved, including that order and follow-up. These two scheduling actions are prescribed for this software test. Finish the visit. Do not change medications for this operational qualification.'''
REQUIRED={'search_patients','get_patient','list_encounters','search_observations','search_conditions','search_medications',
          'search_allergies','search_orders','search_documents','read_document','calculate','create_lab_order',
          'schedule_follow_up','write_visit_note','finish_visit'}

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
    retrieved_creatinines=[];grounded_calculation=False
    for call,observation in zip(calls,observations):
        if call['tool']=='search_observations':
            for entry in observation.get('entry',[]):
                r=entry['resource'];q=r.get('valueQuantity',{})
                if subject(r)==target and any(c.get('code')=='2160-0' for c in r.get('code',{}).get('coding',[])) and q.get('unit')=='mg/dL':
                    retrieved_creatinines.append(q.get('value'))
        if call['tool']=='calculate':
            args=call['input'];inputs=args.get('inputs',{})
            grounded_calculation=grounded_calculation or (args.get('formula')=='unit_convert' and inputs.get('analyte')=='creatinine'
                and inputs.get('from_unit')=='mg/dL' and inputs.get('to_unit') in ('umol/L','µmol/L','μmol/L') and inputs.get('value') in retrieved_creatinines)
    snapshot=list(env.sandbox._snapshot.values())
    documentation=note_check(snapshot,rollout['overlay'],target,actual_items(snapshot,rollout['overlay'],target),None,rollout['calculations'])
    checks={'real_provider':provider in ('anthropic','openai'),'finished':rollout['terminated_by']=='finish_visit',
            'required_tools':REQUIRED<=used,'no_tool_errors':not errors,'no_rejected_writes':not rollout['rejected_writes'],
            'target_only':not rollout['wrong_patient'] and all(subject(r)==env.sandbox.target_patient_id for r in rollout['overlay']),
            'persisted_actions':{'ServiceRequest','Appointment','DocumentReference'}<=kinds,
            'bmp_order':any(r['resourceType']=='ServiceRequest' and lab_group(r.get('code',{}).get('text',''))=='BMP' and r.get('occurrenceDateTime','').startswith('2031-03-10') for r in rollout['overlay']),
            'office_follow_up':any(r['resourceType']=='Appointment' and r.get('appointmentType',{}).get('text')=='office' and r.get('start','').startswith('2031-03-31') for r in rollout['overlay']),
            'no_medication_changes':'MedicationRequest' not in kinds,'retrieved_calculation':grounded_calculation,
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
    for model in (os.getenv('EHR_SMOKE_ANTHROPIC_MODEL','claude-sonnet-4-6'),os.getenv('EHR_SMOKE_OPENAI_MODEL','gpt-5')):
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
