"""Aggregate-only evaluation report: no clinical prose, source spans or identities."""
import json
from collections import Counter,defaultdict
from pathlib import Path
from .common import dumps,identifier
from .constants import settings


def build(*,store,run_groups=None,model=None,task_ids=None,output_dir=None):
    rows=store.ehr_all('ehr_rollouts',limit=10000)
    rows=[r for r in rows if (not run_groups or r['run_group'] in run_groups) and (not model or r['model']==model) and (not task_ids or r['task_id'] in task_ids)]
    models={}; failures=Counter(); examples=defaultdict(list)
    for name in sorted({r['model'] for r in rows}):
        own=[r for r in rows if r['model']==name]
        valid=[r for r in own if r['status']!='reference_excluded']
        finalized=[r for r in valid if r['status']=='final' and r['final_reward'] is not None]
        groups=defaultdict(list);scores=defaultdict(list);success={};provisional=[]
        for row in valid:
            groups[(row['run_group'],row['task_id'])].append(row)
            if row['provisional_reward'] is not None: provisional.append(row['provisional_reward'])
            checkpoints=store.ehr_all('ehr_checkpoints',rollout_id=row['rollout_id'])
            if row not in finalized: continue
            passed=bool(checkpoints) and not row['hard_fail'] and all(c['verdict'] in ('pass','not_gradable') for c in checkpoints)
            if not checkpoints: passed=row['final_reward']==1
            success[row['rollout_id']]=passed
            for cp in checkpoints:
                if cp['score'] is not None: scores[cp['kind']].append(cp['score'])
            trace=json.loads(row['trajectory_json'])
            tags=(trace.get('final_verification') or trace.get('verification',{})).get('failure_tags',[])
            for tag in tags:
                failures[tag]+=1
                if len(examples[tag])<3: examples[tag].append(row['rollout_id'])
        triples=[all(success[r['rollout_id']] for r in values[:3]) for values in groups.values()
                 if len(values)>=3 and all(r['rollout_id'] in success for r in values[:3])]
        final=[r['final_reward'] for r in finalized]
        rate=sum(success.values())/len(success) if success else None
        models[name]={'n':len(own),'n_final':len(finalized),'n_provisional':len(provisional),
            'withdrawn':len(own)-len(valid),'pending':sum(r['status']=='awaiting_review' for r in valid),
            'other_incomplete':sum(r['status'] not in ('final','awaiting_review') for r in valid),
            'mean_final_reward':sum(final)/len(final) if final else None,
            'mean_provisional_reward':sum(provisional)/len(provisional) if provisional else None,
            'full_success_rate':rate,'pass_at_1':rate,
            'pass_pow_3':sum(triples)/len(triples) if triples else None,'groups_with_3':len(triples),
            'hard_fail_rate':sum(r['hard_fail'] for r in finalized)/len(finalized) if finalized else None,
            'checkpoints':{k:sum(v)/len(v) for k,v in scores.items()},
            'harnesses':sorted({r['harness'] for r in own}),
            'document_modes':sorted({json.loads(r['trajectory_json']).get('verification',{}).get('document_mode','probe') for r in valid})}
    selected_ids={r['rollout_id'] for r in rows}
    linked_ids={r['review_id'] for r in store.ehr_all('ehr_review_rollouts') if r['rollout_id'] in selected_ids}
    review_rows=[r for r in store.ehr_all('ehr_reviews') if not (run_groups or model or task_ids) or r['review_id'] in linked_ids]; verdicts=Counter(); pairs=[]
    rubric_pairs=defaultdict(list);rubric_samples=0
    for row in review_rows:
        resolution=json.loads(row.get('resolution_json') or '{}')
        verdicts.update(resolution.get('items',{}).values())
        pair=resolution.get('agreement_pairs',[])
        if len(pair)==2: pairs.extend((value,pair[1][key]) for key,value in pair[0].items() if key in pair[1])
        if row['trigger']=='rubric_sample' and row['status']=='resolved':
            from .reviews import review_items
            from .sealed import unseal
            rollout=store.ehr_get('ehr_rollouts',rollout_id=row['rollout_id']) if row.get('rollout_id') else None
            trace=json.loads(rollout['trajectory_json']) if rollout else {}
            rubric=unseal(trace['rubric_enc']) if trace.get('rubric_enc') else {}
            group=(rubric.get('rubric_version','unknown'),rubric.get('judge_provider','unknown'),rubric.get('judge_model','unknown'))
            rubric_samples+=1
            for item in review_items(row):
                decision=resolution.get('items',{}).get(item['item_id'])
                if decision in ('rubric_met','rubric_unmet'):
                    rubric_pairs[(*group,item['item_id'])].append((bool(item['criterion'].get('met')),decision=='rubric_met'))
    kappa=None
    if pairs:
        a,b=Counter(x for x,_ in pairs),Counter(y for _,y in pairs);n=len(pairs)
        po=sum(x==y for x,y in pairs)/n;pe=sum(a[k]*b[k] for k in set(a)|set(b))/n**2
        kappa=(po-pe)/(1-pe) if pe<1 else 1.
    rubric_agreement={}
    for (version,provider,model_id,criterion),values in rubric_pairs.items():
        n=len(values);a=Counter(x for x,_ in values);b=Counter(y for _,y in values)
        po=sum(x==y for x,y in values)/n;pe=sum(a[k]*b[k] for k in set(a)|set(b))/n**2
        agreement=(po-pe)/(1-pe) if pe<1 else 1.
        rubric_agreement[identifier('rubric-agreement',[version,provider,model_id,criterion])]={'rubric_version':version,'judge_provider':provider,'judge_model':model_id,'criterion_id':criterion,'n':n,'agreement':po,'kappa':agreement,'revision_required':n>=50 and agreement<.6}
    visits=store.ehr_all('ehr_visits',limit=10000);tasks=store.ehr_all('ehr_tasks',limit=10000)
    result={'env_version':settings().env_version,'models':models,'failure_counts':dict(failures),'examples':dict(examples),
        'reviews':{'by_trigger':dict(Counter(r['trigger'] for r in review_rows)),'by_status':dict(Counter(r['status'] for r in review_rows)),
                   'resolved_item_verdicts':dict(verdicts),'reviewer_kappa':kappa,'paired_items':len(pairs),
                   'rubric_samples':rubric_samples,'rubric_agreement':rubric_agreement},
        'data':{'charts':len(store.ehr_all('ehr_charts',limit=10000)),'visits':len(visits),'ready_per_split':dict(Counter(t['split'] for t in tasks if t['status']=='ready')),
                'key_audits':dict(Counter(v['key_audit'] for v in visits)),'key_confidences':[v['key_confidence'] for v in visits]},
        'clinical_rubric_status':__import__(__package__+'.rubric',fromlist=['load']).load()['status']}
    lines=['# Nephrology EHR evaluation','', '| Model | Runs | Final reward | Provisional | pass@1 | pass³ | Hard fail | Pending | Withdrawn |', '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for name,m in models.items():
        def fmt(value): return '—' if value is None else f'{value:.3f}'
        lines.append(f"| {name.replace('|',' ')} | {m['n']} | {fmt(m['mean_final_reward'])} | {fmt(m['mean_provisional_reward'])} | {fmt(m['pass_at_1'])} | {fmt(m['pass_pow_3'])} | {fmt(m['hard_fail_rate'])} | {m['pending']} | {m['withdrawn']} |")
    lines.extend(['','Final success, checkpoint and failure metrics exclude pending reviews and withdrawn references. Provisional reward is labeled separately.', 'pass³ is reported only for task/run groups with at least three repeats.','Clinical rubric: '+result['clinical_rubric_status']+'.','', 'No patient-level free text is included.'])
    markdown='\n'.join(lines)+'\n'
    if output_dir:
        root=Path(output_dir);root.mkdir(parents=True,exist_ok=True);name=identifier('ehr-report',[run_groups,model,task_ids])
        (root/(name+'.json')).write_text(dumps(result));(root/(name+'.md')).write_text(markdown)
    return {'report':result,'markdown':markdown}
