"""Prime Intellect Verifiers v0 adapter (verifiers>=0.1.8,<0.2).

The evaluator owns the private grader URL/token. Neither appears in prompts.
Uses JSON actions so providers can share one action vocabulary.
"""
import json
import os
from pathlib import Path


def load_environment(tasks_path, grader_url, grader_token=None):
    import httpx
    import verifiers as vf
    from datasets import Dataset
    try:
        from ehr_runtime.env import EhrVisitEnv
        from ehr_runtime.tools import tool_schemas
    except ImportError:
        from .env import EhrVisitEnv
        from .tools import tool_schemas
    tasks={r['task_id']:r for line in Path(tasks_path).read_text().splitlines() if line.strip() for r in [json.loads(line)]}
    token=grader_token or os.environ['EHR_GRADER_TOKEN']
    prompt='Operate the EHR using one JSON object per turn: {"tool":"name","input":{...}}. Finish with finish_visit. Tools: '+json.dumps(tool_schemas())
    dataset=Dataset.from_list([{'prompt':[{'role':'system','content':prompt},{'role':'user','content':t['instruction']}],
                               'answer':'','info':{'task_id':t['task_id']}} for t in tasks.values()])
    class Environment(vf.MultiTurnEnv):
        async def setup_state(self,state):
            task=tasks[state['info']['task_id']];env=EhrVisitEnv(task);env.reset()
            state['ehr_env']=env;state['ehr_actions']=[];state['ehr_processed']=0
            return state
        def process(self,state):
            trajectory=state.get('trajectory',[])
            for turn in trajectory[state['ehr_processed']:]:
                completion=turn.get('completion',[])
                text=completion[-1].get('content','') if completion else ''
                try:
                    action=json.loads(text)
                    if not isinstance(action,dict) or 'tool' not in action: raise ValueError()
                except (ValueError,TypeError): action={'tool':'invalid','input':{}}
                state['ehr_actions'].append(action)
                state['ehr_observation']=state['ehr_env'].step(action)[0]
            state['ehr_processed']=len(trajectory)
        @vf.stop
        async def ehr_done(self,state):
            self.process(state)
            return state['ehr_env'].terminated or state['ehr_env'].truncated
        async def env_response(self,messages,state,**kwargs):
            self.process(state)
            return [{'role':'user','content':json.dumps(state.get('ehr_observation',{}))}]
    async def reward(state,**kwargs):
        async with httpx.AsyncClient(timeout=60) as client:
            response=await client.post(grader_url.rstrip('/')+'/grade',headers={'Authorization':'Bearer '+token},
                json={'task_id':state['info']['task_id'],'actions':state['ehr_actions']})
            response.raise_for_status(); return response.json()['reward']
    return Environment(dataset=dataset,rubric=vf.Rubric(funcs=[reward]),max_turns=40)
