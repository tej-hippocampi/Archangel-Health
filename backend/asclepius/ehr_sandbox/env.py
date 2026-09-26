"""Gym/OpenEnv compatible episode. Contains only the pre-sliced task payload."""
import copy
import json
from .common import digest,dumps,outcome
from .constants import DECISION_INSTANT
from .sandbox import FhirSandbox,cached_snapshot
from .tools import ToolRegistry,tool_schemas


class EhrVisitEnv:
    def __init__(self,task,*,max_steps=None,verify_fn=None):
        # Explicit allowlist prevents accidental key/outcome inclusion by callers.
        fields=('task_id','instruction','snapshot_json','snapshot','seed','env_version','budget_tool_calls','task_kind','tags_json')
        self.task={k:copy.deepcopy(task[k]) for k in fields if k in task and k not in ('snapshot','snapshot_json')}
        snapshot_json=task.get('snapshot_json') or dumps(task['snapshot'])
        self._snapshot=cached_snapshot(task['task_id'],snapshot_json)
        self.budget=int(max_steps or task.get('budget_tool_calls',40))
        if self.budget<1: raise ValueError('budget must be positive')
        self.verify_fn=verify_fn; self.sandbox=None

    def reset(self,seed=None):
        self.seed=self.task.get('seed',0) if seed is None else seed
        snapshot=self._snapshot
        target=snapshot.target
        self.sandbox=FhirSandbox(snapshot,rollout_id=digest([self.task['task_id'],self.seed]),now=DECISION_INSTANT,target_patient_id=target)
        self.registry=ToolRegistry(self.sandbox,task_kind=self.task.get('task_kind','visit'))
        self.trajectory=[]; self.step_rewards=[]; self.calls=0; self.terminated=False; self.truncated=False; self.terminated_by=None
        return {'instruction':self.task['instruction'],'now':DECISION_INSTANT}, {'tool_schemas':self.action_space(),'budget':self.budget,'seed':self.seed,'env_version':self.task.get('env_version')}

    def step(self,action):
        if self.sandbox is None: raise RuntimeError('reset required')
        if self.terminated or self.truncated: return {},0.0,self.terminated,self.truncated,{'note':'episode already ended'}
        if not isinstance(action,dict): action={'tool':'','input':{}}
        step=len(self.trajectory)+1; reward=0.0
        if action.get('type')=='thought' or ('tool' not in action and action.get('type') is None):
            self.trajectory.append({'step':step,'type':'thought','content':str(action.get('content',''))[:16000]}); observation={}
        else:
            self.calls+=1; self.sandbox.step=self.calls
            name=action.get('tool',''); params=action.get('input',{})
            self.trajectory.append({'step':step,'type':'tool_call','tool':name,'input':copy.deepcopy(params)})
            before=len(self.sandbox.write_log); result=self.registry.execute(name,params)
            self.trajectory.append({'step':step+1,'type':'observation','content':dumps(result)})
            observation={'observation':result}
            writes=self.sandbox.write_log[before:]
            if writes: reward=0.0 if all(w['accepted'] for w in writes) else -0.05
            if self.registry.finished:
                self.terminated=True; self.terminated_by=name
                self.trajectory.append({'step':step+2,'type':'final_output','content':dumps(params)})
            if not self.terminated and self.calls>=self.budget:
                self.truncated=True; self.terminated_by='budget'
        self.step_rewards.append({'step':step,'action':self.calls,'reward':reward})
        return observation,reward,self.terminated,self.truncated,self.state()

    def verify(self):
        if self.sandbox is None: raise RuntimeError('reset required')
        if self.verify_fn is None: raise RuntimeError('grading requires a control-plane verifier or separate /grade endpoint')
        return self.verify_fn(self.rollout())

    def rollout(self):
        return {'task_id':self.task['task_id'],'trajectory':copy.deepcopy(self.trajectory),'access_log':copy.deepcopy(self.sandbox.access_log),
                'write_log':copy.deepcopy(self.sandbox.write_log),'overlay':copy.deepcopy(list(self.sandbox.overlay.values())),
                'rejected_writes':copy.deepcopy(self.sandbox.rejected_writes),'wrong_patient':self.sandbox.wrong_patient,
                'terminated_by':self.terminated_by,'tool_calls':self.calls,'answer':copy.deepcopy(self.registry.answer),
                'calculations':copy.deepcopy(self.registry.calculations),'step_rewards':copy.deepcopy(self.step_rewards)}

    def state(self):
        return {'task_id':self.task['task_id'],'step':self.calls,'budget_left':max(0,self.budget-self.calls),
                'n_writes':len(self.sandbox.overlay),'terminated':self.terminated or self.truncated}
    def action_space(self): return tool_schemas()
    def observation_space(self): return {'type':'object'}
    def render(self): return dumps(self.state())
    def close(self): self.sandbox=None
    def as_gym(self):
        import gymnasium as gym
        from gymnasium.spaces import Space
        parent=self
        class JsonSpace(Space):
            def contains(self,value): return isinstance(value,dict)
            def sample(self,mask=None): return {'tool':'finish_visit','input':{}}
        class Wrapped(gym.Env):
            action_space=JsonSpace(); observation_space=JsonSpace()
            def reset(self,*,seed=None,options=None):
                super().reset(seed=seed); return parent.reset(seed)
            def step(self,action): return parent.step(action)
            def render(self): return parent.render()
            def close(self): return parent.close()
        return Wrapped()
