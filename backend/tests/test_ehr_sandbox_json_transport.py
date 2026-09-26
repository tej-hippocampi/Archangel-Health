"""Multi-turn JSON agents retain roles and request one provider JSON object."""
import asyncio
from types import SimpleNamespace as NS
import pytest
from ai import llm_client
from ai.model_config import OPENAI_MODEL

@pytest.mark.parametrize('sync',[False,True])
@pytest.mark.parametrize('fallback',[False,True])
def test_openai_json_transport_preserves_conversation(monkeypatch,sync,fallback):
    captured=[]
    response=NS(output_text='{"tool":"finish_visit","input":{}}',id='synthetic',usage=None,
                choices=[NS(message=NS(content='{"tool":"finish_visit","input":{}}'),finish_reason='stop')])
    def respond(**params):
        if fallback and 'instructions' in params:raise AttributeError('legacy SDK')
        captured.append(params);return response
    async def arespond(**params):return respond(**params)
    create=respond if sync else arespond
    client=NS(responses=NS(create=create),chat=NS(completions=NS(create=create)))
    monkeypatch.setattr(llm_client,'_sopenai' if sync else '_aopenai',lambda:client)
    messages=[{'role':'user','content':'Task: JSON action please.'},
              {'role':'assistant','content':'{"tool":"get_patient","input":{"patient_id":"p1"}}'},
              {'role':'user','content':'{"result":{"id":"p1"}}'}]
    args=(OPENAI_MODEL,'Return exactly one JSON object.',messages,2000,0)
    result=llm_client._openai_create_sync(*args,json_object=True) if sync else asyncio.run(llm_client._openai_create_async(*args,json_object=True))
    params=captured[0];conversation=params['messages'][1:] if fallback else params['input'][1:]
    if not fallback:assert params['input'][0]['role']=='developer' and 'JSON' in params['input'][0]['content']
    if not fallback:assert conversation[1]['content'][0]=={'type':'output_text','text':messages[1]['content'],'annotations':[]}
    assert [m['role'] for m in conversation]==['user','assistant','user']
    assert [m['content'][0]['text'] for m in conversation]==[m['content'] for m in messages]
    assert (params['response_format'] if fallback else params['text']['format'])=={'type':'json_object'}
    assert llm_client.first_text(result)=='{"tool":"finish_visit","input":{}}'


def test_qualification_calculations_require_retrieved_inputs():
    from scripts.smoke_ehr_sandbox import calculation_grounding
    result={'entry':[{'resource':{'resourceType':'Observation','subject':{'reference':'Patient/p1'},'code':{'coding':[{'code':code}]},'valueQuantity':{'value':value,'unit':unit}}}
                     for code,value,unit in [('2160-0',3.6,'mg/dL'),('33914-3',28,'mL/min/1.73m2')]]}
    calls=[{'tool':'search_observations','input':{}},
           {'tool':'calculate','input':{'formula':'unit_convert','inputs':{'value':3.6,'analyte':'creatinine','from_unit':'mg/dL','to_unit':'umol/L'}}},
           {'tool':'calculate','input':{'formula':'ckd_stage','inputs':{'egfr':28}}}]
    outputs=[result,{'value':318.24,'unit':'umol/L'},{'value':'G4'}]
    assert calculation_grounding(calls,outputs,'p1')==(True,True)
    calls.append({'tool':'calculate','input':{'formula':'uacr_category','inputs':{'uacr_mg_g':300}}});outputs.append({'value':'A2'})
    assert calculation_grounding(calls,outputs,'p1')==(True,False)
    assert calculation_grounding(calls,outputs,'other')==(False,False)
