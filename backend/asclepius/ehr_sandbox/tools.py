"""The 24 tool action space. All mutations go through FhirSandbox validation."""
from __future__ import annotations
import base64
import copy
import json
import re
from datetime import datetime,timedelta
from pathlib import Path
import jsonschema
from .common import concept,extension,outcome
from .terminology import drug,lab_group,GROUPS,LOINC,RXNORM,ICD10
from .calculators import calculate

SCHEMAS=json.loads(Path(__file__).with_name('tool_schemas.json').read_text())
SPECIALTIES=set('vascular_surgery transplant_nephrology cardiology urology dietitian nephrology endocrinology hematology oncology rheumatology pulmonology neurology gastroenterology hepatology infectious_disease dermatology ophthalmology otolaryngology allergy_immunology geriatrics internal_medicine family_medicine palliative_care pain_medicine psychiatry psychology physical_therapy occupational_therapy general_surgery cardiothoracic_surgery neurosurgery orthopedic_surgery colorectal_surgery bariatric_surgery transplant_surgery interventional_radiology sleep_medicine reproductive_endocrinology obstetrics_gynecology podiatry'.split())


def tool_schemas(): return copy.deepcopy(SCHEMAS)
def all_tool_names(): return [s['name'] for s in SCHEMAS]


class ToolRegistry:
    def __init__(self,sandbox,*,task_kind='visit'):
        self.sandbox=sandbox; self.task_kind=task_kind; self.answer=None; self.finished=False; self.calculations=[]

    def execute(self,name,params):
        schema=next((s['input_schema'] for s in SCHEMAS if s['name']==name),None)
        if schema is None: return outcome('unknown tool','not-supported')
        try: jsonschema.validate(params,schema)
        except jsonschema.ValidationError as exc: return outcome('invalid tool input at '+'.'.join(map(str,exc.path))+': '+exc.validator)
        self.sandbox.tool=name
        try: return self._execute(name,params)
        except (ValueError,TypeError,KeyError,IndexError,ArithmeticError) as exc: return outcome(str(exc)[:200])

    def _execute(self,name,p):
        s=self.sandbox
        if name=='search_patients': return s.search('Patient',**{ {'count':'_count','page_token':'_page'}.get(k,k):v for k,v in p.items()})
        if name=='get_patient': return s.read('Patient',p['patient_id'])
        if name=='read_document':
            doc=s.read('DocumentReference',p['document_id'])
            if doc['resourceType']=='OperationOutcome': return doc
            from .common import document_text
            return {'resourceType':'DocumentReference','id':doc['id'],'subject':doc['subject'],'text':document_text(doc)}
        search_types={'list_encounters':'Encounter','search_observations':'Observation','search_conditions':'Condition',
                      'search_medications':'MedicationRequest','search_allergies':'AllergyIntolerance','search_documents':'DocumentReference'}
        if name in search_types:
            kind=search_types[name]; q={'patient':p['patient_id']}
            renames={'clinical_status':'clinical-status','group':'code:group','count':'_count','page_token':'_page','sort':'_sort'}
            for key,value in p.items():
                if key not in ('patient_id','date_from','date_to') and not (key=='status' and value=='all'): q[renames.get(key,key)]=value
            if name=='search_medications' and 'status' not in p: q['status']='active'
            dates=['ge'+p['date_from']] if p.get('date_from') else []
            if p.get('date_to'): dates.append('le'+p['date_to'])
            if dates: q['authoredon' if kind=='MedicationRequest' else 'date']=dates
            return s.search(kind,**q)
        if name=='search_orders':
            from .common import digest
            from .sandbox import date_value
            kind=p.get('kind','all'); candidates=[]
            for resource in s.resources():
                if resource.get('resourceType')=='Appointment':
                    patient=next((x['actor']['reference'].removeprefix('Patient/') for x in resource.get('participant',[]) if x.get('actor',{}).get('reference','').startswith('Patient/')),None)
                    if kind in ('all','appointment') and patient==p['patient_id']: candidates.append(resource)
                elif resource.get('resourceType')=='ServiceRequest' and resource.get('subject',{}).get('reference')=='Patient/'+p['patient_id']:
                    categories=[c.get('code',c.get('text')) for group in resource.get('category',[]) for c in group.get('coding',[group])]
                    if kind=='all' or kind in categories: candidates.append(resource)
            candidates.sort(key=lambda r:r['id']);candidates.sort(key=date_value,reverse=True)
            query=digest({k:v for k,v in p.items() if k!='page'});start=0;count=p.get('count',50)
            if p.get('page'):
                token=json.loads(base64.urlsafe_b64decode(p['page']).decode())
                if token['query']!=query or not isinstance(token['offset'],int) or token['offset']<0: raise ValueError('invalid page token')
                start=token['offset']
            page=candidates[start:start+count]
            clean=[s._scrub(r) for r in page];s._read_log(p,page)
            result={'resourceType':'Bundle','type':'searchset','total':len(candidates),'entry':[{'resource':r} for r in clean]}
            if start+count<len(candidates):
                token=base64.urlsafe_b64encode(json.dumps({'query':query,'offset':start+count}).encode()).decode()
                result['link']=[{'relation':'next','url':'?page='+token}]
            return result
        if name=='calculate':
            result=calculate(p['formula'],p['inputs']); self.calculations.append(result); return result
        if name=='finish_visit': self.finished=True; return {'finished':True,'summary':p.get('summary','')}
        if name=='submit_answer':
            if not self.task_kind.startswith('probe'): return outcome('submit_answer is only available for probes','not-supported')
            self.answer=copy.deepcopy(p['answer']); self.finished=True; return {'submitted':True}
        patient={'reference':'Patient/'+p.get('patient_id','')}
        if name=='create_medication_order':
            mapped=drug(p['drug']); medication=concept(p['drug'],mapped['ingredient'],RXNORM)
            dose={'text':f"{p['dose_value']} {p['dose_unit']} {p['frequency']}",
                  'route':concept(p['route']),'timing':{'code':concept(p['frequency'])},
                  'doseAndRate':[{'doseQuantity':{'value':p['dose_value'],'unit':p['dose_unit']}}]}
            if p.get('duration_days'): dose['timing']['repeat']={'boundsDuration':{'value':p['duration_days'],'unit':'days','system':'http://unitsofmeasure.org','code':'d'}}
            resource={'resourceType':'MedicationRequest','status':'active','intent':'order','subject':patient,
                      'medicationCodeableConcept':medication,'dosageInstruction':[dose]}
            if p.get('reason'): resource['reasonCode']=[concept(p['reason'])]
            return s.create(resource)
        if name in ('change_medication_order','discontinue_medication','hold_medication'):
            old=s.read('MedicationRequest',p['medication_request_id'])
            if old['resourceType']=='OperationOutcome': return old
            if old['status'] not in ('active','on-hold'): raise ValueError('only an active or held medication can be changed')
            replacement=copy.deepcopy(old); old['status']='on-hold' if name=='hold_medication' else 'stopped'
            old['statusReason']=concept(p['reason'])
            if p.get('resume_condition'): old['note']=[{'text':p['resume_condition']}]
            if name!='change_medication_order': return s.update(old['id'],old)
            if not any(k in p for k in ('dose_value','dose_unit','frequency')): raise ValueError('dose or frequency change is required')
            replacement.pop('id',None); replacement['status']='active'; replacement.pop('statusReason',None)
            replacement['priorPrescription']={'reference':'MedicationRequest/'+old['id']}
            dosage=replacement['dosageInstruction'][0]
            source_dose=re.search(r'(\d+(?:\.\d+)?)\s*(mg|mcg|g|mEq|mL|units?)\b',dosage.get('text',''),re.I)
            if not dosage.get('doseAndRate') and source_dose:
                dosage['doseAndRate']=[{'doseQuantity':{'value':float(source_dose.group(1)),'unit':source_dose.group(2)}}]
            if not dosage.get('timing'):
                source_text=dosage.get('text','').lower()
                frequency=next((f for f in ('twice daily','three times daily','four times daily','once daily','every other day','once a week','at bedtime','every night','each morning','once a day','each day','bid','tid','qid','qhs','nightly','qd','qod','daily','weekly','hourly') if re.search(r'\b'+f+r'\b',source_text)),None)
                interval=re.search(r'\b(?:every|q)\s*\d+\s*(?:hours?|h)\b',source_text)
                if interval: frequency=interval.group(0)
                if frequency: dosage['timing']={'code':concept(frequency)}
            quantity=dosage.setdefault('doseAndRate',[{'doseQuantity':{}}])[0].setdefault('doseQuantity',{})
            if 'dose_value' in p: quantity['value']=p['dose_value']
            if 'dose_unit' in p: quantity['unit']=p['dose_unit']
            if 'frequency' in p: dosage['timing']={'code':concept(p['frequency'])}
            frequency=dosage.get('timing',{}).get('code',{}).get('text')
            if not frequency: raise ValueError('frequency is unknown; supply frequency explicitly')
            dosage['text']=f"{quantity['value']} {quantity['unit']} {frequency}"
            replacement['reasonCode']=[concept(p['reason'])]
            written=s.write_many([(old,old['id']),(replacement,None)])
            return {'resourceType':'Bundle','type':'collection','entry':[{'resource':r} for r in written]} if isinstance(written,list) else written
        if name in ('create_lab_order','create_referral','create_imaging_order'):
            category={'create_lab_order':'lab','create_referral':'referral','create_imaging_order':'imaging'}[name]
            if category=='lab':
                group=lab_group(p['code']); code=concept(group or p['code'],p['code'] if not group else None,LOINC)
                if group: code['extension']=[extension('labGroup',group,'String')]
            elif category=='referral':
                if p['specialty'] not in SPECIALTIES: raise ValueError('unsupported referral specialty')
                code=concept(p['specialty'],p['specialty'],'https://archangel.health/fhir/specialty')
            else: code=concept(p['study'])
            resource={'resourceType':'ServiceRequest','status':'active','intent':'order','subject':patient,
                      'category':[concept(category,category,'https://archangel.health/fhir/order-category')],'code':code,
                      'occurrenceDateTime':(datetime.fromisoformat(s.now)+timedelta(days=p.get('timing_days',0))).isoformat()}
            if p.get('reason'): resource['reasonCode']=[concept(p['reason'])]
            if p.get('urgency'): resource['priority']='urgent' if p['urgency']=='urgent' else 'routine'
            if category=='imaging': resource['extension']=[extension('contrast',p.get('contrast',False),'Boolean')]
            return s.create(resource)
        if name=='schedule_follow_up':
            start=datetime.fromisoformat(s.now)+timedelta(days=p['interval_days'])
            return s.create({'resourceType':'Appointment','status':'booked','start':start.isoformat(),'end':(start+timedelta(minutes=30)).isoformat(),
                             'appointmentType':concept(p.get('visit_type','office')),'participant':[{'actor':patient,'status':'accepted'}]})
        if name=='record_assessment':
            resource={'resourceType':'Condition','subject':patient,'code':concept(p['text'],p.get('icd10'),ICD10),
                      'clinicalStatus':concept(p.get('clinical_status','active'),p.get('clinical_status','active'),'http://terminology.hl7.org/CodeSystem/condition-clinical')}
            if p.get('ckd_stage'): resource['extension']=[extension('ckdStage',p['ckd_stage'],'String')]
            return s.create(resource)
        if name=='flag_urgent':
            resources=[({'resourceType':'Flag','status':'active','subject':patient,'code':concept(p['action']),'period':{'start':s.now}},None),
                       ({'resourceType':'CommunicationRequest','status':'active','subject':patient,'priority':'stat','payload':[{'contentString':p['reason']}],
                         'category':[concept(p['action'])]},None)]
            written=s.write_many(resources)
            return {'resourceType':'Bundle','type':'collection','entry':[{'resource':r} for r in written]} if isinstance(written,list) else written
        if name=='write_visit_note':
            text='Assessment:\n'+p['assessment']+'\nPlan:\n'+p['plan']
            if len(text)>6000: raise ValueError('visit note exceeds 6000 characters')
            return s.create({'resourceType':'DocumentReference','status':'current','subject':patient,'type':concept('Visit note'),
                             'content':[{'attachment':{'contentType':'text/plain','data':base64.b64encode(text.encode()).decode()}}]})
        return outcome('unknown tool','not-supported')
