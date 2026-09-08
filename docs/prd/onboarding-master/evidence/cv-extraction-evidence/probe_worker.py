from pathlib import Path
import ast,typing,json,os
root=Path(__file__).resolve().parent
repo=Path(os.environ.get('ARCHANGEL_REPO', str(Path.cwd()))).resolve()
p=repo/'backend/routers/onboarding.py'
tree=ast.parse(p.read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_record_cv_on_person')
module=ast.Module(body=[fn],type_ignores=[])
ctx={'Any':typing.Any,'Optional':typing.Optional,'Dict':typing.Dict}
exec(compile(module,str(p),'exec'),ctx)
class MemoryStore:
 def __init__(self):self.creds={}
 def get_asclepius_person(self,*args):return {'credentials':dict(self.creds)}
 def save_asclepius_credentials(self,*args):self.creds=dict(args[-1])
ts=MemoryStore();record=ctx['_record_cv_on_person']
record(ts,'test','test@example.com',sha='A',mime='application/pdf',stage='reading',filename='A.pdf')
record(ts,'test','test@example.com',sha='B',mime='application/pdf',stage='reading',filename='B.pdf')
record(ts,'test','test@example.com',sha='A',mime='application/pdf',stage='done',parsed={'full_name':'Applicant A','ok':True})
r={'scope':'Existing _record_cv_on_person AST executed unchanged with an in-memory store. Demonstrates late A write after B upload; no production race was induced.','expected':{'cvAssetSha':'B','cvFilename':'B.pdf','cvParseStage':'reading'},'actual':ts.creds,'passed':ts.creds.get('cvAssetSha')=='B'}
(root/'worker_race_result.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps(r,indent=2))
