"""Ingested case to validated, relative-time FHIR chart and sealed extraction."""
from __future__ import annotations
import base64
import copy
import json
import re
import jsonschema

from asclepius.cases import ClinicalCase
from asclepius.deid_verify import verify_deid
from asclepius.store import get_store
from .common import concept, digest, dumps, extension, identifier, validate, ext, resource_ref
from .constants import audit_now
from .sealed import seal
from . import terminology as terms, worksheet_extract

CHART_VERSION = 2


def resources_from_case(case, patient_id):
    resources = []
    def add(kind, item, fields, label):
        resource = {'resourceType':kind, 'id':identifier(kind.lower(), [patient_id, label]), **fields}
        offset = item.get('collected_offset_days')
        if isinstance(offset,int) and not isinstance(offset,bool):
            resource.setdefault('extension',[]).append(extension('offsetDays',offset))
        validate(resource)
        resources.append(resource)
        return resource
    subject = {'reference':'Patient/'+patient_id}
    demo = case.get('demographics') or {}
    add('Patient', {}, {'id':patient_id, 'gender':{'M':'male','F':'female'}.get(demo.get('sex'),'unknown'),
                       'extension':[extension('ageBand',demo.get('age_band') or 'unknown','String')]}, 'patient')
    visits = {}
    for i,note in enumerate(case.get('notes', [])):
        text = note.get('text','')
        kind = note.get('note_type','Other')
        resource = add('DocumentReference',note, {'status':'current','subject':subject,'type':concept(kind),
                    'content':[{'attachment':{'contentType':'text/plain','data':base64.b64encode(text.encode()).decode()}}]}, ['note',i])
        if kind == 'Visit worksheet' and isinstance(note.get('collected_offset_days'),int):
            visits.setdefault(note['collected_offset_days'],[]).append({'note':note, 'resource_id':resource['id']})
    encounter_days = set()
    for i,encounter in enumerate(case.get('encounters',[])):
        add('Encounter',encounter, {'status':'finished','class':{'system':'http://terminology.hl7.org/CodeSystem/v3-ActCode','code':'AMB'},'subject':subject}, ['encounter',i])
        encounter_days.add(encounter.get('collected_offset_days'))
    for day in visits:
        if day not in encounter_days:
            add('Encounter',{'collected_offset_days':day}, {'status':'finished','class':{'system':'http://terminology.hl7.org/CodeSystem/v3-ActCode','code':'AMB'},'subject':subject}, ['worksheet-encounter',day])
    panels=copy.deepcopy(case.get('lab_panels',[]))
    vitals=case.get('vitals') or {}
    if vitals:
        results=[]
        for key,label,loinc,unit in [('systolic','Systolic blood pressure','8480-6','mm[Hg]'),('diastolic','Diastolic blood pressure','8462-4','mm[Hg]'),('weight_kg','Body weight','29463-7','kg'),('height_cm','Body height','8302-2','cm'),('heart_rate','Heart rate','8867-4','/min')]:
            value=vitals.get(key)
            if isinstance(value,(int,float)): results.append({'analyte':label,'loinc':loinc,'value':value,'unit':unit})
        if results: panels.append({'panel':'Vitals','collected_offset_days':vitals.get('collected_offset_days'),'results':results})
    for i,panel in enumerate(panels):
        refs = []
        category = 'vital-signs' if panel.get('panel','').lower() == 'vitals' else 'laboratory'
        values=panel.get('results',[])
        bp=[r for r in values if r.get('loinc') in ('8480-6','8462-4')]
        if category=='vital-signs' and len({r.get('loinc') for r in bp})==2 and all(isinstance(r.get('value'),(int,float)) for r in bp):
            fields={'status':'final','subject':subject,'category':[concept('Vital signs','vital-signs','http://terminology.hl7.org/CodeSystem/observation-category')],
                    'code':concept('Blood pressure','85354-9',terms.LOINC),'component':[{'code':concept(r['analyte'],r['loinc'],terms.LOINC),'valueQuantity':{'value':r['value'],'unit':r.get('unit') or 'mmHg','system':'http://unitsofmeasure.org','code':'mm[Hg]'}} for r in bp]}
            resource=add('Observation',panel,fields,['bp',i]);refs.append({'reference':resource_ref(resource)})
            values=[r for r in values if r not in bp]
        for j,result in enumerate(values):
            value = result.get('value')
            fields = {'status':'final','subject':subject,
                      'category':[{'coding':[{'system':'http://terminology.hl7.org/CodeSystem/observation-category','code':category}]}],
                      'code':concept(result.get('analyte','Unknown'),result.get('loinc') or terms.lab_code(result.get('analyte','')),terms.LOINC)}
            if isinstance(value,(int,float)) and not isinstance(value,bool):
                fields['valueQuantity'] = {'value':value, 'unit':result.get('unit') or '1', 'system':'http://unitsofmeasure.org','code':result.get('unit') or '1'}
            else:
                fields['valueString'] = str(value)
            ranges = {bound:{'value':result[key]} for bound,key in [('low','ref_low'),('high','ref_high')] if isinstance(result.get(key),(int,float))}
            if ranges:
                fields['referenceRange'] = [ranges]
            if result.get('flag'):
                fields['interpretation'] = [concept(result['flag'], result['flag'], 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation')]
            resource = add('Observation',panel,fields,['lab',i,j])
            refs.append({'reference':'Observation/'+resource['id']})
        if refs:
            add('DiagnosticReport',panel,{'status':'final','subject':subject,'code':concept(panel.get('panel','Labs')),'result':refs},['panel',i])
    for i,med in enumerate(case.get('medications',[])):
        drug = terms.drug(med['drug'])
        extensions = [extension(k,med[field]) for k,field in [('startOffset','start_offset_days'),('stopOffset','stop_offset_days')]
                      if isinstance(med.get(field),int)]
        add('MedicationRequest',med,{'status':med.get('status') if med.get('status') in ('active','stopped','completed','on-hold','cancelled') else 'unknown',
             'intent':'order','subject':subject,'medicationCodeableConcept':concept(med['drug'],drug['ingredient'],terms.RXNORM),
             'dosageInstruction':[{'text':' '.join(str(med.get(x) or '') for x in ('dose','route','freq')).strip() or 'Unspecified'}],
             'extension':extensions},['med',i])
    for i,problem in enumerate(case.get('problem_list',[])):
        text = problem.get('condition','')
        match = re.search(r'\b[A-Z]\d{2}(?:\.\d+)?\b',text)
        add('Condition',problem,{'subject':subject,'code':concept(text,match.group(0) if match else None,terms.ICD10),
             'clinicalStatus':concept('active','active','http://terminology.hl7.org/CodeSystem/condition-clinical')},['problem',i])
    for i,allergy in enumerate(case.get('allergies',[])):
        fields={'patient':subject,'code':concept(allergy['substance'])}
        if allergy.get('reaction'):
            fields['reaction']=[{'manifestation':[concept(allergy['reaction'])]}]
        add('AllergyIntolerance',allergy,fields,['allergy',i])
    for i,order in enumerate(case.get('orders',[])):
        extensions=[]
        if isinstance(order.get('due_offset_days'),int):
            extensions.append(extension('dueInterval',order['due_offset_days']))
        if order.get('kind') == 'follow_up':
            add('Appointment',order,{'status':'proposed','participant':[{'actor':subject,'status':'accepted'}],
                                    'description':order.get('text','Follow up'),'extension':extensions},['order',i])
        else:
            add('ServiceRequest',order,{'status':'active','intent':'order','subject':subject,
                'category':[concept(order.get('kind','other'),order.get('kind','other'),'https://archangel.health/fhir/order-category')], 'code':concept(order.get('text',''),order.get('code'),order.get('code_system')),
                'extension':extensions},['order',i])
    apply_medication_events(resources, case.get('medication_events',[]), patient_id)
    return resources, visits


def reconcile_medications(key, case, day):
    """Add structured changes and flag contradictory directions for mandatory audit."""
    key = copy.deepcopy(key)
    changes = [e for e in case.get('medication_events',[]) if e.get('collected_offset_days') == day and e.get('action') != 'continue']
    for med in case.get('medications',[]):
        if med.get('start_offset_days') == day:
            prior = [m for m in case.get('medications',[]) if m is not med and terms.drug(m['drug'])['name'] == terms.drug(med['drug'])['name']
                     and m.get('stop_offset_days') == day]
            before = terms.daily_amount(prior[0].get('dose'),prior[0].get('freq')) if prior else None
            after = terms.daily_amount(med.get('dose'),med.get('freq'))
            comparable=before and after and before['unit']==after['unit']
            action = ('increase' if after['value'] > before['value'] else 'decrease' if after['value'] < before['value'] else 'change') if comparable else 'start'
            changes.append({**med,'action':action})
        elif med.get('stop_offset_days') == day and not any(terms.drug(m['drug'])['name'] == terms.drug(med['drug'])['name']
                                                        and m.get('start_offset_days') == day for m in case.get('medications',[])):
            changes.append({**med,'action':'stop'})
    conflict = False
    for i,event in enumerate(changes):
        name = terms.drug(event['drug'])
        matches = [m for m in key['med_changes'] if terms.drug(m['drug'])['name'] == name['name']]
        if matches:
            def disagrees(item):
                extracted=terms.daily_amount(item.get('to_dose'));structured=terms.daily_amount(event.get('dose'),event.get('freq'))
                return item.get('action')!=event['action'] or bool(extracted and structured and (extracted['unit']!=structured['unit'] or abs(extracted['value']-structured['value'])>.001))
            if any(disagrees(m) for m in matches):
                conflict = True
            else:
                for m in matches:
                    m['confidence'] = max(m['confidence'],0.95)
        else:
            key['med_changes'].append({'item_id':f'structured-med-{i}','action':event['action'],'drug':event['drug'],
                                       'rxnorm_ingredient':name['ingredient'],'to_dose':' '.join(str(event.get(k) or '') for k in ('dose','freq')).strip() or None,
                                       'source':'structured','confidence':0.95})
    return key, conflict


async def build_chart(ingest_case_id, *, store=None, source_country='US'):
    store = store or get_store()
    row = store.get_ingest_case(ingest_case_id)
    if not row or row['status'] != 'ingested':
        raise ValueError('chart build requires an ingested case with no blocking review')
    case = ClinicalCase.model_validate(row['case']).model_dump()
    prior = store.ehr_list('ehr_charts',ingest_case_id=ingest_case_id,status='built')
    source_hash = digest(case)
    chart_id = identifier('chart',[ingest_case_id,source_hash,CHART_VERSION])
    patient_id = identifier('patient',chart_id)
    now = audit_now()
    base = {'chart_id':chart_id,'ingest_case_id':ingest_case_id,'upload_id':row['upload_id'],'specialty':'nephrology',
            'source_country':source_country,'created_at':now,'updated_at':now}
    existing=store.ehr_get('ehr_charts',chart_id=chart_id)
    def quarantine(reason,findings=0):
        if existing:
            store.ehr_update('ehr_charts',{'chart_id':chart_id},{'status':'quarantined','updated_at':now})
            # Existing evidence is immutable. Return only masked metadata, never it.
            return {**base,'resources_json':'[]','status':'quarantined','extraction_json':dumps({'reason':reason,'findings':findings})}
        return store.ehr_insert('ehr_charts',{**base,'resources_json':'[]','chart_hash':digest([]),'status':'quarantined',
                  'extraction_json':dumps({'reason':reason,'findings':findings})})
    verification = verify_deid(case)
    if verification['status'] != 'pass':
        return quarantine('residual_phi',len(verification['findings']))
    if existing:
        # Re-verification is mandatory even when extraction can be reused.
        return existing
    try:
        resources, visits = resources_from_case(case,patient_id)
    except (ValueError, TypeError):
        return quarantine('invalid_fhir_resource')
    extracted=[]
    for index,(day,notes) in enumerate(sorted(visits.items())):
        text='\n'.join(n['note']['text'] for n in notes)
        try:
            key=await worksheet_extract.extract(text, {'specialty':'nephrology'})
        except (ValueError, TypeError, KeyError, jsonschema.ValidationError):
            return quarantine('invalid_worksheet_extraction')
        key,conflict=reconcile_medications(key,case,day)
        augment_from_key(resources,key,case,day,patient_id)
        extracted.append({'offset_days':day,'encounter_ref':f'visit-{index}','key_enc':seal(key),
                          'key_confidence':key['extraction']['min_confidence'],'key_conflict':int(conflict),
                          'document_ids':[n['resource_id'] for n in notes]})
    for resource in resources: validate(resource)
    chart = {**base,'n_visits':len(extracted),'resources_json':dumps(resources),'chart_hash':digest(resources),'status':'built',
             'extraction_json':dumps({'source_hash':source_hash,'builder_version':CHART_VERSION,'visits':extracted,'patient_id':patient_id})}
    with store._conn() as conn:
        store._immediate(conn)
        existing=store.ehr_get('ehr_charts',chart_id=chart_id,_connection=conn)
        if existing:
            return existing
        store.ehr_insert('ehr_charts',chart,_connection=conn)
        for old in prior:
            store.ehr_update('ehr_charts',{'chart_id':old['chart_id']},{'status':'superseded','updated_at':now},_connection=conn)
    return chart


def apply_medication_events(resources,events,patient_id):
    """Materialize regimens; stop offsets remain metadata until the slice is made."""
    for index,event in enumerate(sorted(events,key=lambda e:e.get('collected_offset_days') if isinstance(e.get('collected_offset_days'),int) else -100000)):
        day=event.get('collected_offset_days'); action=event.get('action')
        if not isinstance(day,int) or action=='continue': continue
        mapped=terms.drug(event['drug'])
        previous=[r for r in resources if r['resourceType']=='MedicationRequest'
                  and terms.drug(r['medicationCodeableConcept']['text'])['name']==mapped['name']
                  and isinstance(ext(r),int) and ext(r)<=day and (ext(r,'stopOffset') is None or ext(r,'stopOffset')>=day)]
        old=max(previous,key=ext) if previous else None
        if action in ('stop','hold','increase','decrease','change') and old:
            old['status']='on-hold' if action=='hold' else 'stopped'
            old['extension']=[e for e in old.get('extension',[]) if not e['url'].endswith('stopOffset')]+[extension('stopOffset',day)]
            if event.get('reason'): old['statusReason']={'text':event['reason']}
        if action in ('stop','hold'): continue
        if not event.get('dose'): continue
        text=' '.join(str(event.get(k) or '') for k in ('dose','route','freq')).strip()
        med={'resourceType':'MedicationRequest','id':identifier('med-event',[patient_id,day,event]),
             'status':'active','intent':'order','subject':{'reference':'Patient/'+patient_id},
             'medicationCodeableConcept':concept(event['drug'],mapped['ingredient'],terms.RXNORM),
             'dosageInstruction':[{'text':text}], 'extension':[extension('offsetDays',day),extension('startOffset',day)]}
        if old: med['priorPrescription']={'reference':resource_ref(old)}
        if not any(r['id']==med['id'] for r in resources): resources.append(validate(med))


def augment_from_key(resources,key,case,day,patient_id):
    """Only grounded extractions become dated historical resources; raw notes remain."""
    patient={'reference':'Patient/'+patient_id}
    structured_today={terms.drug(e['drug'])['name'] for e in case.get('medication_events',[]) if e.get('collected_offset_days')==day and e.get('action')!='continue'}
    structured_today.update(terms.drug(m['drug'])['name'] for m in case.get('medications',[]) if m.get('start_offset_days')==day or m.get('stop_offset_days')==day)
    # Each affirmative current regimen is a dated historical assertion. A later
    # changed dose becomes a successor, preserving the prior regimen and its
    # dates. Nothing from this worksheet is visible to this same visit's agent.
    for med in key.get('medications',[]):
        name=terms.drug(med['drug'])['name']
        # Structured same-day decisions are already materialized. The current
        # medication list describes the pre-plan state and cannot undo them.
        if name in structured_today: continue
        known=[r for r in resources if r['resourceType']=='MedicationRequest' and terms.drug(r['medicationCodeableConcept']['text'])['name']==name
               and isinstance(ext(r),int) and ext(r)<=day and (ext(r,'stopOffset') is None or ext(r,'stopOffset')>=day)]
        previous=max(known,key=ext) if known else None
        from .items import medication_amount
        prior_text=previous['dosageInstruction'][0]['text'] if previous else ''
        same_regimen=previous is not None and medication_amount(previous)==terms.daily_amount(med['dose'],med['frequency']) and terms.daily_amount(prior_text,'daily')==terms.daily_amount(med['dose'],'daily') and terms.frequency_per_day(prior_text)==terms.frequency_per_day(med['frequency']) and (not med.get('route') or med['route'].lower() in prior_text.lower())
        if not same_regimen:
            apply_medication_events(resources,[{'drug':med['drug'],'action':'change' if previous else 'start','dose':med['dose'],
                'freq':med['frequency'],'route':med.get('route',''),'collected_offset_days':day}],patient_id)
    for item in key['assessments']:
        resources.append(validate({'resourceType':'Condition','id':identifier('assessment',[patient_id,day,item['item_id']]),
                          'subject':patient,'code':concept(item['text'],item.get('icd10'),terms.ICD10),
                          'clinicalStatus':concept(item.get('status','active'),item.get('status','active'),'http://terminology.hl7.org/CodeSystem/condition-clinical'),
                          'extension':[extension('offsetDays',day)]}))
    for item in key['orders']+[{**r,'kind':'referral'} for r in key['referrals']]:
        text=item.get('text') or item.get('loinc_group') or item.get('specialty') or 'Unspecified order'
        resources.append(validate({'resourceType':'ServiceRequest','id':identifier('extracted-order',[patient_id,day,item['item_id']]),
                          'subject':patient,'status':'active','intent':'order','code':concept(text),
                          'category':[concept(item['kind'],item['kind'],'https://archangel.health/fhir/order-category')],
                          'extension':[extension('offsetDays',day)]+([extension('dueInterval',item['timing_days'])] if isinstance(item.get('timing_days'),int) else [])}))
    if key.get('follow_up') and not any(o.get('kind')=='follow_up' and o.get('collected_offset_days')==day for o in case.get('orders',[])):
        resources.append(validate({'resourceType':'Appointment','id':identifier('follow-up',[patient_id,day]),'status':'proposed',
                          'participant':[{'actor':patient,'status':'accepted'}],
                          'extension':[extension('offsetDays',day),extension('dueInterval',key['follow_up']['interval_days'])]}))
    structured={terms.drug(e['drug'])['name'] for e in case.get('medication_events',[]) if e.get('collected_offset_days')==day}
    structured.update(terms.drug(m['drug'])['name'] for m in case.get('medications',[]) if m.get('start_offset_days')==day or m.get('stop_offset_days')==day)
    events=[{'drug':m['drug'],'action':m['action'],'dose':m.get('to_dose'),'collected_offset_days':day}
            for m in key['med_changes'] if terms.drug(m['drug'])['name'] not in structured]
    apply_medication_events(resources,events,patient_id)
