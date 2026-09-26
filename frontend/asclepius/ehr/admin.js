/* EHR control plane: synthetic task metadata only; no answer keys on this screen. */
(() => {
  'use strict';
  window.AdminEhrSection = { async render(host, ctx) {
    const { h, api, clear } = ctx;
    const request = (path, body) => api('/ehr-sandbox' + path, body === undefined ? {} : {method:'POST',body});
    clear(host);
    const heading=h('h2',{},'Nephrology EHR environments');
    const message=h('p',{'role':'status'}); const content=h('div',{});
    host.append(heading,h('p',{},'Build charts from cleared uploads, audit reference decisions, then compile and evaluate visit tasks.'),message,content);
    const show = (value) => { const pre=h('pre',{});pre.textContent=JSON.stringify(value,null,2);clear(content);content.append(pre); };
    const fail = (error) => { message.textContent=error.message || (typeof error.detail==='string'?error.detail:'The operation failed.'); };
    const button=(label,fn) => h('button',{class:'asc-btn',onclick:async(e)=>{ const b=e.currentTarget;b.disabled=true;message.textContent='Working…';try { await fn();message.textContent='Updated.'; } catch(err){fail(err);}finally{b.disabled=false;} }},label);
    const field=(label,input) => h('label',{class:'asc-field'},label,input);
    const input=(placeholder,value='')=>h('input',{class:'asc-input',placeholder,value});
    const select=(values)=>h('select',{class:'asc-input'},...values.map(v=>h('option',{value:v},v)));
    const section=(title,...children)=>h('section',{class:'asc-card',style:'margin:16px 0;padding:20px'},h('h3',{},title),...children);
    const loadCharts=async()=>{
      const result=await request('/charts');clear(content);
      content.append(h('h3',{},'Charts and visits'));
      let rows=result.charts;
      while(result.next_offset!==null){ const page=await request('/charts?offset='+result.next_offset);rows.push(...page.charts);result.next_offset=page.next_offset; }
      if(!rows.length)content.append(h('p',{},'No charts built yet. Use an upload ID from Data → Pipeline.'));
      for(const row of rows){
        const card=section(row.chart_id,h('p',{},row.status+' · '+row.n_visits+' visits · upload '+row.upload_id),
          button('Preview compilation',async()=>show(await request('/charts/'+row.chart_id+'/compile',{dry_run:true}))),
          button('Compile visits',async()=>show(await request('/charts/'+row.chart_id+'/compile',{dry_run:false}))));
        const table=h('table',{class:'asc-table'},h('thead',{},h('tr',{},...['Visit','Confidence','Key audit','Status / reason'].map(v=>h('th',{},v)))));
        const body=h('tbody',{});for(const v of row.visits)body.append(h('tr',{},h('td',{},v.visit_id),h('td',{},String(v.key_confidence)),h('td',{},v.key_audit),h('td',{},v.exclusion_reason||v.status)));
        table.append(body);card.append(table);content.append(card);
      }
    };
    const upload=input('Upload ID');
    host.insertBefore(section('Uploads → charts',field('Cleared upload',upload),button('Build charts',async()=>{ const r=await request('/charts/build',{upload_id:upload.value.trim()});show(r);watch(r.job_id); }),button('Refresh charts',loadCharts)),content);
    const split=select(['dev','train','heldout']),model=input('Model ID','scripted:planted'),repeat=input('Repeats','3'),protocol=select(['native_tools','json_protocol']);repeat.type='number';repeat.min='1';repeat.max='20';
    host.insertBefore(section('Evaluate',field('Split',split),field('Model',model),field('Repeats',repeat),field('Harness',protocol),
      button('Run ready tasks',async()=>{const r=await request('/runs',{split:split.value,model:model.value,k:Number(repeat.value),harness:protocol.value});show(r);watch(r.job_id);}),
      button('Tasks and sanity checks',async()=>{clear(content);let next=0;do{const r=await request('/tasks?split='+split.value+'&offset='+next);for(const t of r.tasks)content.append(section(t.task_id,h('p',{},t.task_kind+' · '+t.status),button('No-op / oracle sanity',async()=>show(await request('/tasks/'+t.task_id+'/sanity',{})))));next=r.next_offset;}while(next!==null);}),
      button('Balance and report',async()=>show(await request('/summary')))),content);
    const exclusionUpload=input('Upload ID'),reviewer=input('Reviewer ID'),reason=select(['source_practice_clinician','family','other']);
    host.insertBefore(section('Physician reviews',button('Review pipeline',async()=>show(await request('/admin/reviews'))),field('Source upload',exclusionUpload),field('Exclude reviewer',reviewer),field('Reason',reason),button('Save source exclusion',async()=>show(await request('/exclusions',{upload_id:exclusionUpload.value.trim(),user_id:reviewer.value.trim(),reason:reason.value})))),content);
    const buyer=input('Buyer reference'),country=input('ISO-2 country','GB'),exportSplit=select(['dev','train']);
    const covered=h('input',{type:'checkbox'}),onward=h('input',{type:'checkbox'});
    host.insertBefore(section('Lab package',field('Split',exportSplit),field('Buyer reference',buyer),field('Buyer jurisdiction',country),
      field('Buyer attests it is not a covered person',covered),field('Contract contains onward-transfer restrictions',onward),
      button('Build package',async()=>{
        const r=await request('/exports/package',{split:exportSplit.value,buyer_ref:buyer.value.trim(),buyer_jurisdiction:country.value.trim().toUpperCase(),buyer_not_covered_person:covered.checked,onward_transfer_clause:onward.checked});show(r);
        for(const [label,path,name] of [['Download archive',r.download,r.export_id+'.zip'],['Download separate grader key',r.grader_key_download,r.export_id+'.grader.key']])content.append(button(label,async()=>{const response=await api(path,{raw:true});if(!response.ok)throw Error('Download failed');const url=URL.createObjectURL(await response.blob());const a=h('a',{href:url,download:name});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));
      })),content);
    async function watch(id){
      for(let i=0;i<240&&host.isConnected;i++){
        await new Promise(resolve=>setTimeout(resolve,2500));if(!host.isConnected)return;
        try{const state=await request('/runs/'+id);const latest=state.events[0];message.textContent='Job '+id+': '+latest.event_type;
          if(['completed','failed'].includes(latest.event_type)){if(latest.event_type==='completed'){content.append(button('View report',async()=>show(await request('/reports/'+id))),button('View charts',loadCharts));}return;}
        }catch(err){fail(err);return;}
      }
    }
    try{const summary=await request('/summary');message.textContent='Clinical rubric: '+summary.rubric_status+'. '+summary.data.charts+' charts.';await loadCharts();}catch(err){fail(err);}
  }};
})();
