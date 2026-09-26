/* Assignment-only, blinded ex-ante review. No innerHTML or raw untrusted markup. */
(() => {
  'use strict';
  window.EhrReviewSection={async render(host,ctx,reviewId){
    const {h,api,clear}=ctx;const request=(path,body)=>api('/ehr-sandbox'+path,body===undefined?{}:{method:'POST',body});
    clear(host);const status=h('p',{'role':'status'});host.append(h('h2',{},'Nephrology review'),status);
    const error=e=>{status.textContent=e.message||(typeof e.detail==='string'?e.detail:'Unable to load review.');};
    const label=(text,input)=>{input.setAttribute('aria-label',text);return h('label',{},text,input);};
    const pretty=(value)=>String(value??'').replaceAll('_',' ');
    const planView=(plan)=>h('div',{},...Object.entries(plan||{}).map(([key,value])=>h('p',{},h('strong',{},pretty(key)+': '),Array.isArray(value)?value.join(', '):pretty(value))));
    function chartView(resource){
      const title=resource.code?.text||resource.type?.text||resource.medicationCodeableConcept?.text||resource.description||resource.resourceType;
      const card=h('article',{class:'asc-card',style:'padding:12px;margin:8px 0'},h('strong',{},title));
      const line=(label,value)=>{if(value!==undefined&&value!==null&&value!=='')card.append(h('p',{},h('strong',{},label+': '),String(value)));};
      if(resource.resourceType==='Patient'){line('Patient',resource.name?.[0]?.text);line('Date of birth',resource.birthDate);line('MRN',resource.identifier?.[0]?.value);line('Sex',resource.gender);}
      if(resource.resourceType==='Observation'){
        line('Value',resource.valueQuantity?resource.valueQuantity.value+' '+(resource.valueQuantity.unit||''):resource.valueString);
        for(const c of resource.component||[])line(c.code?.text||'Measurement',c.valueQuantity?.value+' '+(c.valueQuantity?.unit||''));
      }
      if(resource.resourceType==='DocumentReference'){
        try{const bytes=Uint8Array.from(atob(resource.content?.[0]?.attachment?.data||''),c=>c.charCodeAt(0));card.append(h('pre',{style:'white-space:pre-wrap'},new TextDecoder().decode(bytes)));}catch(_){line('Document','Unavailable');}
      }
      if(resource.resourceType==='MedicationRequest')line('Regimen',(resource.dosageInstruction||[]).map(d=>d.text||'').join('; '));
      if(resource.resourceType==='AllergyIntolerance')line('Reaction',(resource.reaction||[]).flatMap(r=>(r.manifestation||[]).map(m=>m.text)).join(', '));
      line('Status',resource.status||resource.clinicalStatus?.text);
      line('Date',resource.effectiveDateTime||resource.authoredOn||resource.date||resource.recordedDate||resource.start||resource.period?.start);
      if(resource.resourceType==='ServiceRequest')line('Due',resource.occurrenceDateTime);
      return card;
    }
    function keyForm(initial){
      const key=JSON.parse(JSON.stringify(initial));const editor=h('div',{});
      const fields={assessments:['text','icd10','status'],med_changes:['drug','action','from_dose','to_dose','reason'],orders:['kind','text','loinc_group','codes','timing_days'],referrals:['specialty','reason'],follow_up:['interval_days','tolerance_days'],escalation:['action','reason']};
      const options={status:['active','inactive','resolved'],action:['start','stop','hold','increase','decrease','change'],kind:['lab','imaging']};
      const numeric=new Set(['timing_days','interval_days','tolerance_days']);
      const labels={text:'Decision',icd10:'ICD-10-CM',drug:'Medication',action:'Action',from_dose:'Previous dose and frequency',to_dose:'New dose and frequency',loinc_group:'Lab group',codes:'Test codes, comma separated',timing_days:'Order timing, days',interval_days:'Follow-up interval, days',tolerance_days:'Allowed timing difference, days',source_span:'Exact supporting quote from the worksheet'};
      function draw(){
        clear(editor);
        for(const [category,names] of Object.entries(fields)){
          const many=['assessments','med_changes','orders','referrals'].includes(category);const items=many?(key[category]||[]):(key[category]?[key[category]]:[]);
          const section=h('section',{},h('h4',{},pretty(category)));
          items.forEach((item,index)=>{
            const row=h('div',{class:'asc-card',style:'padding:12px;margin:8px 0'});
            const structured=item.source==='structured';
            if(structured)row.append(h('p',{},'Structured source decision. Its original values are preserved. Remove it only if your audit finds it should not be part of this key.'));
            for(const name of structured?names:[...names,'source_span']){
              let choices=options[name];if(category==='escalation'&&name==='action')choices=['ed_now','same_day_contact','admit_recommended','urgent_referral'];
              const value=Array.isArray(item[name])?item[name].join(', '):(item[name]??'');
              const input=choices?h('select',{class:'asc-input'},h('option',{value:''},'Choose…'),...choices.map(v=>h('option',{value:v},pretty(v)))):h(name==='source_span'?'textarea':'input',{class:'asc-input',type:numeric.has(name)?'number':'text'});
              input.value=value;input.addEventListener('input',()=>{item[name]=name==='codes'?input.value.split(',').map(v=>v.trim()).filter(Boolean):numeric.has(name)?(input.value===''?null:Number(input.value)):input.value;});
              input.disabled=structured;
              row.append(label(labels[name]||pretty(name),input));
            }
            row.append(h('button',{type:'button',class:'asc-btn',onclick:()=>{if(many)key[category].splice(index,1);else key[category]=null;draw();}},'Remove decision'));section.append(row);
          });
          if(many||!items.length)section.append(h('button',{type:'button',class:'asc-btn',onclick:()=>{const item={confidence:1,source_span:''};if(many)(key[category]||=[]).push(item);else key[category]=item;draw();}},'Add decision'));
          editor.append(section);
        }
      }
      draw();return {editor,key};
    }

    function labTrends(resources){
      const groups=new Map();
      const names={'2160-0':'Creatinine','38483-4':'Creatinine','2823-3':'Potassium','33914-3':'eGFR','48642-3':'eGFR','62238-1':'eGFR','98979-8':'eGFR','9318-7':'UACR','14959-1':'UACR'};
      for(const resource of resources){
        if(resource.resourceType!=='Observation'||!resource.valueQuantity)continue;
        const coding=(resource.code?.coding||[]).find(c=>c.system==='http://loinc.org');
        const label=names[coding?.code]||resource.code?.text||coding?.code||'Observation';
        const unit=resource.valueQuantity.unit||'unit not recorded',key=label+'|'+unit;
        if(!groups.has(key))groups.set(key,{label,unit,rows:[]});groups.get(key).rows.push(resource);
      }
      const section=h('section',{},h('h3',{},'Lab and measurement trends'));
      for(const {label,unit,rows} of groups.values()){
        rows.sort((a,b)=>(a.effectiveDateTime||'').localeCompare(b.effectiveDateTime||''));
        const heading=h('h4',{},label+' · '+unit);section.append(heading);
        const values=rows.map(r=>r.valueQuantity.value).filter(Number.isFinite);
        if(values.length>1){
          const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 180 36');svg.setAttribute('width','180');svg.setAttribute('height','36');svg.setAttribute('role','img');svg.setAttribute('aria-label',label+' trend; values are listed in the table');
          const min=Math.min(...values),range=Math.max(...values)-min||1;
          const line=document.createElementNS(svg.namespaceURI,'polyline');line.setAttribute('points',values.map((v,i)=>(4+i*172/(values.length-1))+','+(32-(v-min)*28)).join(' '));line.setAttribute('fill','none');line.setAttribute('stroke','currentColor');line.setAttribute('stroke-width','2');svg.append(line);section.append(svg);
        }
        section.append(h('table',{class:'asc-table'},h('thead',{},h('tr',{},h('th',{},'Date'),h('th',{},'Value'))),h('tbody',{},...rows.map(r=>h('tr',{},h('td',{},r.effectiveDateTime?.slice(0,10)||'Unknown'),h('td',{},String(r.valueQuantity.value)+' '+unit))))));
      }
      return section;
    }

    try{
      if(!reviewId){const queue=await request('/reviews/queue');if(!queue.assignments.length)host.append(h('p',{},'No EHR reviews are assigned to you.'));
        for(const a of queue.assignments)host.append(h('p',{},h('a',{href:'#ehr-review/'+a.review_id,onclick:()=>window.EhrReviewSection.render(host,ctx,a.review_id)},a.trigger+' · due '+a.due_at+' · $'+(a.pay_cents/100).toFixed(2))));return;}
      const data=await request('/reviews/'+encodeURIComponent(reviewId));const started=performance.now();
      host.append(h('p',{},'Review the available chart, then assess each plan independently. Your decision is recorded before any later outcomes are shown.'));
      const chart=h('details',{},h('summary',{},'Pre-visit chart'));
      chart.append(labTrends(data.chart));
      for(const resource of [...data.chart].sort((a,b)=>(b.date||'').localeCompare(a.date||''))){
        if(resource.resourceType==='Observation'&&resource.valueQuantity)continue;
        chart.append(chartView(resource));
      }
      host.append(chart);
      for(const [label,medications] of [['Plan A: medication list after changes',data.plan_a_medications],['Plan B: medication list after changes',data.plan_b_medications],['Proposed medication list after changes',data.proposed_medications]]){
        if(medications)host.append(h('h3',{},label),medications.length?h('div',{},...medications.map(planView)):h('p',{},'No active medications.'));
      }
      if(data.worksheet)host.append(h('h3',{},'Source worksheet'),h('pre',{},data.worksheet));
      if(data.agent_note)host.append(h('h3',{},'Visit note'),h('pre',{},data.agent_note),h('p',{},'Rubric version: '+(data.rubric?.rubric_version||'pending')));
      const choose=(options)=>h('select',{class:'asc-input',required:true},h('option',{value:''},'Choose…'),...options.map(v=>h('option',{value:v},v.replaceAll('_',' '))));
      const fields=[];const form=h('form',{});
      for(const item of data.items){
        if(data.trigger==='key_audit')continue;
        const a=choose(['appropriate','acceptable_alternative','inappropriate','harmful']),b=choose(['appropriate','acceptable_alternative','inappropriate','harmful']),better=choose(['A','B','equivalent']);
        const rationale=h('textarea',{class:'asc-input',required:true,minlength:20,placeholder:'Explain using evidence from the chart'}),critical=h('input',{type:'checkbox'});
        const special=['key_audit','safety','rubric_sample','outcome_flag','dose_sample'].includes(data.trigger);
        if(special){a.required=false;b.required=false;better.required=false;}
        const card=h('section',{class:'asc-card',style:'padding:18px;margin:16px 0'},h('h3',{},'Decision '+(fields.length+1)),
          h('div',{style:'display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:20px'},
            h('div',{},h('h4',{},'Plan A'),planView(item.plan_a),label('Rate A',a)),
            h('div',{},h('h4',{},'Plan B'),planView(item.plan_b),label('Rate B',b))),
          label('Overall preference',better),label('Reason',rationale),label('Critical harm',critical));
        const safety=data.trigger==='safety'?choose(['confirmed','false_positive','uncertain']):null;
        const met=item.criterion?choose(['met','not_met']):null;
        const reference=['outcome_flag','dose_sample'].includes(data.trigger)?choose(['appropriate','acceptable_alternative','inappropriate','harmful','uncertain']):null;
        if(reference)card.append(h('p',{},'Judge this proposed plan using only the information available at the visit.'),planView(item.proposed_plan),label('Proposed plan',reference));
        if(met)card.append(h('p',{},item.criterion.description||item.criterion.id),label('Criterion met?',met));
        if(data.trigger==='key_audit')rationale.required=false;
        if(special){card.querySelector('div[style]').hidden=true; a.closest('label').hidden=true;b.closest('label').hidden=true;better.closest('label').hidden=true;critical.closest('label').hidden=true;}
        if(safety)card.append(h('p',{},item.finding?.reason||'Safety finding'),planView(item.finding?.offending_action),label('Safety finding',safety));
        fields.push({item_id:item.item_id,a,b,better,rationale,critical,safety,met,reference});form.append(card);
      }
      let auditStatus,keyEditor,auditRationale,editedKey;
      if(data.trigger==='key_audit'){
        auditStatus=choose(['correct','corrected']);const editing=keyForm(data.extracted_key);keyEditor=editing.editor;editedKey=editing.key;
        auditRationale=h('textarea',{class:'asc-input',required:true,minlength:20,placeholder:'Explain the audit decision'});
        form.append(h('h3',{},'Reference-key audit'),h('p',{},'Confirm every item and add any missing decision. For each correction, quote the supporting words from the worksheet.'),label('Audit result',auditStatus),keyEditor,auditRationale);
      }
      const confidence=choose(['high','low']);const submit=h('button',{class:'asc-btn asc-btn-primary',type:'submit'},'Submit review');form.append(label('Confidence',confidence),submit);host.append(form);
      form.addEventListener('submit',async event=>{event.preventDefault();submit.disabled=true;try{
        const body={confidence:confidence.value,seconds_spent:Math.floor((performance.now()-started)/1000),items:fields.map(f=>({item_id:f.item_id,plan_a:f.a.value,plan_b:f.b.value,better:f.better.value,rationale:f.rationale.value,critical:f.critical.checked,...(f.safety?{safety_decision:f.safety.value}:{}),...(f.met?{met:f.met.value==='met'}:{}),...(f.reference?{reference_decision:f.reference.value}:{})}))};
        if(auditStatus)body.key_audit={status:auditStatus.value,rationale:auditRationale.value,...(auditStatus.value==='corrected'?{replacement_key:editedKey}:{})};
        await request('/reviews/'+reviewId+'/verdict',body);form.remove();status.textContent='Review recorded. Your review payment has been added to earnings.';
        if(data.trigger==='outcome_flag'){
          const outcome=await request('/reviews/'+reviewId+'/outcome');host.append(h('h3',{},'Later outcome'),h('div',{},...outcome.outcome.map(chartView)));
          const change=choose(['no_change','proposed_plan_was_wrong']),note=h('textarea',{class:'asc-input',minlength:20}),save=h('button',{class:'asc-btn'},'Record reflection');
          host.append(label('Outcome reflection',change),label('Reflection reason',note),save);save.onclick=async()=>{try{await request('/reviews/'+reviewId+'/reflection',{change:change.value,note:note.value});save.disabled=true;status.textContent='Reflection saved. Your original review is preserved.';}catch(e){error(e);}};
        }
      }catch(e){error(e);submit.disabled=false;}});
    }catch(e){error(e);}
  }};
})();
