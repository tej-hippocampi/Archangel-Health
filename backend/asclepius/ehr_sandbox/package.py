"""Attested train/dev exports with allowlisted code and separately delivered keys."""
import ast
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import uuid
from pathlib import Path
from .common import dumps
from .constants import settings,DECISION_INSTANT
from .sealed import unseal

RUNTIME=('__init__.py','common.py','constants.py','env.py','sandbox.py','tools.py','calculators.py',
         'terminology.py','terminology_cache.json','titration_ladders.json','tool_schemas.json','server.py')
GRADER=('grader.py','items.py','safety_rules.py','review_policy.py')
RESTRICTED=('snomed','sct','cpt','ama-assn')


def strip_restricted(value):
    if isinstance(value,dict):
        if 'system' in value and any(word in str(value['system']).lower() for word in RESTRICTED): return None
        return {k:v for k,child in value.items() if (v:=strip_restricted(child)) is not None or child is None}
    if isinstance(value,list): return [v for child in value if (v:=strip_restricted(child)) is not None or child is None]
    return value


def _root():
    from asclepius.export import export_root
    return Path(export_root())/'ehr-sandbox'


def artifact_path(export_id,artifact,*,store):
    if not re.fullmatch(r'ehrpkg-[a-f0-9]{32}',export_id): raise ValueError('Invalid export id')
    events=store.list_events(entity_type='ehr_export',entity_id=export_id,limit=1)
    if not events or events[0]['event_type']!='ehr_package_built': raise ValueError('Export is not available in this realm')
    path=_root()/(export_id+('.zip' if artifact=='archive' else '.grader.key'))
    if not path.is_file(): raise ValueError('Export artifact is unavailable')
    return str(path)


def _portable_phi(source):
    # Exact union used by validation, frozen at package build time. Pattern
    # serialization avoids importing either application's configuration/runtime.
    from asclepius.validation import _PHI_PATTERNS
    from gold.deid import _RESIDUAL_PATTERNS
    patterns={(kind,p.pattern,p.flags) for kind,p in _PHI_PATTERNS+_RESIDUAL_PATTERNS}
    return ('import re\nPATTERNS = '+repr(sorted(patterns))+'\n'
            'def residual_identifiers(text):\n'
            '    return sorted({kind for kind,pattern,flags in PATTERNS if text and re.search(pattern,text,flags)})\n')


def build(split,attestations,buyer_ref,*,store=None,output_root=None):
    from asclepius.store import get_store
    from .harness import effective_key
    from .sandbox import FhirSandbox
    from .visit_compiler import balance_split
    store=store or get_store()
    if split not in ('train','dev'): raise ValueError('Only train and dev may be exported')
    jurisdiction=str(attestations.get('buyer_jurisdiction','')).strip().upper()
    if not re.fullmatch('[A-Z]{2}',jurisdiction) or not buyer_ref or len(buyer_ref)>200: raise ValueError('Buyer reference and ISO-2 jurisdiction are required')
    # Apply to all packages, including mixed-source decoys. Missing provenance
    # never relaxes the gate. These are buyer-provided contractual attestations.
    if jurisdiction in settings().export_blocked_jurisdictions: raise ValueError('Buyer jurisdiction is blocked')
    if attestations.get('buyer_not_covered_person') is not True: raise ValueError('Covered-person attestation is required')
    if jurisdiction!='US' and attestations.get('onward_transfer_clause') is not True: raise ValueError('Onward-transfer clause is required')
    if not balance_split(split,store=store,dry_run=True)['balanced']: raise ValueError('Split has no balanced visit task set')
    tasks=[];keys={};sources=set();versions=set()
    pending_dose_tasks=set()
    for review in store.ehr_all('ehr_reviews',trigger='dose_sample'):
        if review['status'] not in ('resolved','cancelled'):
            rollout=store.ehr_get('ehr_rollouts',rollout_id=review['rollout_id'])
            if rollout: pending_dose_tasks.add(rollout['task_id'])
    package_id='ehrpkg-'+uuid.uuid4().hex;canary='NEPH-EHR-CANARY-'+str(uuid.uuid4())
    for row in store.ehr_all('ehr_tasks',split=split,status='ready'):
        if row['task_id'] in pending_dose_tasks: raise ValueError('Resolve sampled dose-reference reviews before exporting')
        visit=store.ehr_get('ehr_visits',visit_id=row['visit_id']);chart=store.ehr_get('ehr_charts',chart_id=visit['chart_id'])
        if chart['status']!='built' or visit['key_audit'] not in ('passed','waived','not_sampled'): raise ValueError('A selected task has an unresolved chart or key audit')
        if any(r['status'] not in ('resolved','cancelled') for r in store.ehr_all('ehr_reviews',visit_id=visit['visit_id'],scope='visit')):
            raise ValueError('Resolve visit reviews before exporting')
        task={k:row[k] for k in ('task_id','task_kind','env_version','split','seed','instruction','budget_tool_calls')}
        task.update(snapshot=strip_restricted(json.loads(row['snapshot_json'])),tags=json.loads(row['tags_json'] or '[]'),now=DECISION_INSTANT,canary=canary)
        sb=FhirSandbox(task['snapshot'],rollout_id='export',now=DECISION_INSTANT,target_patient_id=task['snapshot']['identifier']['value'])
        for resource in sb.resources(): sb._scrub(resource)
        tasks.append(task)
        key=strip_restricted(unseal(row['probe_key_enc']) if row.get('probe_key_enc') else effective_key(visit,store=store))
        policy={'decisions':[{'signature':v['item_signature'],'verdict':v['verdict']} for v in store.ehr_all('ehr_verdict_cache',visit_id=visit['visit_id'])],'safety':[]}
        from .reviews import review_items
        for review in store.ehr_all('ehr_reviews',visit_id=visit['visit_id'],trigger='safety',status='resolved'):
            resolutions=json.loads(review['resolution_json'])['items']
            for item in review_items(review):
                finding=item.get('rule',{})
                policy['safety'].append({'rule_id':finding.get('rule_id'),'finding_item_id':finding.get('item_id'),'fingerprint':finding.get('fingerprint'),'verdict':resolutions[item['item_id']]})
        keys[row['task_id']]={'key':key,'policy':policy}
        sources.add(chart['source_country'])
        sources.update(t['code'] for t in task['snapshot'].get('meta',{}).get('tag',[]) if t.get('system')=='https://archangel.health/source-country')
        versions.add(row['env_version'])
    if not tasks: raise ValueError('No ready tasks in the selected split')
    root=Path(output_root) if output_root else _root();root.mkdir(parents=True,exist_ok=True)
    directory=root/package_id;directory.mkdir(mode=0o700)
    for name in ('server/ehr_runtime','grader/ehr_runtime','tasks','graders'): (directory/name).mkdir(parents=True)
    source=Path(__file__).parent
    for name in RUNTIME:
        text=(source/name).read_text()
        if name=='titration_ladders.json':
            table=json.loads(text);table['approval_count']=len(set(table.pop('approved_by',[])));text=dumps(table)
        if name=='sandbox.py': text=text.replace('from asclepius.validation import residual_identifiers','from .phi import residual_identifiers')
        if name=='constants.py': text=text.replace('from asclepius.store import _utcnow_iso\n    return _utcnow_iso()',"raise RuntimeError('audit clock is unavailable in the portable runtime')")
        for dest in ('server','grader'): (directory/dest/'ehr_runtime'/name).write_text(text)
    phi=_portable_phi((source.parent/'validation.py').read_text())
    for dest in ('server','grader'): (directory/dest/'ehr_runtime/phi.py').write_text(phi)
    for name in GRADER: shutil.copyfile(source/name,directory/'grader/ehr_runtime'/name)
    (directory/'tasks'/f'{split}.jsonl').write_text(''.join(dumps(t)+'\n' for t in tasks))
    key=secrets.token_bytes(32);nonce=secrets.token_bytes(12)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ciphertext=AESGCM(key).encrypt(nonce,dumps(keys).encode(),package_id.encode())
    (directory/'graders/keys.enc').write_text(dumps({'package_id':package_id,'nonce':base64.b64encode(nonce).decode(),'ciphertext':base64.b64encode(ciphertext).decode()}))
    key_path=root/(package_id+'.grader.key')
    with key_path.open('x') as f: os.chmod(key_path,0o600);f.write(base64.b64encode(key).decode()+'\n')
    (directory/'requirements.txt').write_text('fastapi==0.115.12\nuvicorn==0.34.2\nfhir.resources==8.3.0\njsonschema==4.26.0\ncryptography>=44,<47\n')
    (directory/'Dockerfile').write_text(f'''FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home runtime
FROM base AS agent
COPY --chown=runtime server/ /app/
COPY --chown=runtime tasks/ /data/tasks/
ENV EHR_TASKS_PATH=/data/tasks/{split}.jsonl
USER runtime
EXPOSE 8000
CMD ["uvicorn","ehr_runtime.server:create_agent_app","--factory","--host","0.0.0.0","--port","8000"]
FROM base AS grader
COPY --chown=runtime grader/ /app/
COPY --chown=runtime tasks/ /data/tasks/
COPY --chown=runtime graders/ /data/graders/
ENV EHR_TASKS_PATH=/data/tasks/{split}.jsonl EHR_KEYS_PATH=/data/graders/keys.enc
USER runtime
EXPOSE 8001
CMD ["uvicorn","ehr_runtime.server:create_grader_app","--factory","--host","0.0.0.0","--port","8001"]
FROM agent AS default
''')
    (directory/'.dockerignore').write_text('*.key\n.env*\n__pycache__\n*.zip\n')
    (directory/'CANARY.txt').write_text(canary+'\n')
    (directory/'openenv.yaml').write_text(f'name: neph-ehr\nversion: {settings().env_version}\ntransport: http\nreset: /reset\nstep: /step\nstate: /state\naction_schema: server/ehr_runtime/tool_schemas.json\nentrypoint: ehr_runtime.server:create_agent_app\n')
    shutil.copyfile(source/'verifiers_adapter.py',directory/'verifiers_adapter.py')
    (directory/'README.md').write_text(f'''# Nephrology EHR environment

Build the agent with `docker build --target agent -t neph-ehr-agent .`; run
`docker run --rm -p 8000:8000 neph-ehr-agent`. Its filesystem has no keys,
grader modules, oracle, source worksheets, physician identities or payment data.

Build the private grader with `docker build --target grader -t neph-ehr-grader .`.
Mount the separately delivered `{package_id}.grader.key` read-only at `/run/secrets/grader.key`,
set `EHR_GRADER_KEY_FILE=/run/secrets/grader.key` and `EHR_GRADER_TOKEN` to a fresh secret,
and expose port 8001 only to the evaluator. Never give the agent that mount or token.

POST `/reset` with `{{"task_id":"..."}}`; retain the returned session_id.
POST `/step` with `{{"session_id":"...","action":{{"tool":"...","input":{{}}}}}}`.
GET `/state?session_id=...`; DELETE `/sessions/<session_id>` to release memory.
POST `/grade` on the private service with a Bearer token and
`{{"task_id":"...","actions":[...]}}`; actions are replayed, never trusted as fabricated logs.
This implements the PRD HTTP protocol; it does not claim compatibility with every OpenEnv SDK version.

DOCUMENT uses deterministic grounding and order consistency here. Approved LLM rubric grading
is supported in the control plane. No unapproved rubric is shipped.
The Verifiers adapter uses the v0 MultiTurnEnv API and requires `verifiers>=0.1.8,<0.2`.

Contains LOINC codes, copyright Regenstrief Institute, Inc., under https://loinc.org/license/.
RxNorm: US National Library of Medicine. SNOMED CT and CPT coding entries removed.
Synthetic names, dates and MRNs are intentional. Keep grader material out of agent training corpora.
Canary: {canary}
''')
    (directory/'DATASHEET.md').write_text(f'''# Dataset facts

Split: {split}. Tasks: {len(tasks)}. Source jurisdictions: {', '.join(sorted(sources))}.
Versions: {', '.join(sorted(versions))}. Action/no-action visit sampling: 40–60%.
Only pre-visit snapshots with same-upload, same-split decoys. Later outcomes excluded.
Reference decisions include append-only physician corrections.
Scores: retrieve 10%, reason 20%, act 50%, document 20%; safety hard failures override reward.
Renal dose rules abstain when indication, renal estimate or evidence is insufficient.
This research benchmark requires clinical validation on source charts before release.
Document mode: deterministic_only. Engineering fixtures imply no clinical validation.
''')
    hashes={str(p.relative_to(directory)):hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}
    (directory/'manifest.json').write_text(dumps({'package_id':package_id,'split':split,'tasks':len(tasks),'env_versions':sorted(versions),'files':hashes,'canary':canary,'source_countries':sorted(sources),'buyer_ref':buyer_ref,'attestations':attestations}))
    archive=shutil.make_archive(str(root/package_id),'zip',root_dir=directory)
    archive_hash=hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    store.log_event(entity_type='ehr_export',entity_id=package_id,event_type='ehr_package_built',payload={
        'buyer_ref':buyer_ref,'attestations':attestations,'split':split,'n_tasks':len(tasks),'archive_sha256':archive_hash,'canary':canary})
    return {'export_id':package_id,'n_tasks':len(tasks),'archive_sha256':archive_hash,'canary':canary,
            'archive_path':archive,'key_path':str(key_path),'directory_path':str(directory),
            'download':'/ehr-sandbox/exports/'+package_id+'/archive','grader_key_download':'/ehr-sandbox/exports/'+package_id+'/grader-key'}
