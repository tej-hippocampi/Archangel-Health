"""Browser coverage for the real admin module and assignment review controls."""
import base64
import json
import os
from pathlib import Path
import pytest
from tests.test_sandbox_admin_ui import FRONTEND, _serve_console, _launch_browser, _boot_console, client, sandbox_on


def screenshot(page,name):
    directory=os.environ.get('EHR_SCREENSHOTS')
    if directory:
        target=Path(directory);target.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(target/name),full_page=True)


def test_ehr_admin_tab_loads_and_compiles_without_rendering_sealed_data(sandbox_on):
    server,base=_serve_console({'/sandbox/admin':client.get('/sandbox/admin').text})
    playwright,browser=_launch_browser()
    try:
        page=_boot_console(browser,base,'/sandbox/admin','asclepius_token_sandbox',1440)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        def api(route):
            url=route.request.url
            if url.endswith('/summary'):body={'rubric_status':'awaiting_physician_authorship','data':{'charts':1}}
            elif url.endswith('/charts'):body={'charts':[{'chart_id':'synthetic-chart','status':'built','n_visits':3,'upload_id':'synthetic-upload','visits':[{'visit_id':'synthetic-visit','key_confidence':.95,'key_audit':'passed','status':'ready'}]}],'next_offset':None}
            elif url.endswith('/compile'):body={'built':[{'task_id':'synthetic-task'}],'excluded':[]}
            else:body={}
            route.fulfill(status=200,content_type='application/json',body=json.dumps(body))
        page.route('**/api/asclepius/ehr-sandbox/**',api)
        page.get_by_role('button',name='EHR environments',exact=True).click()
        page.get_by_role('heading',name='synthetic-chart',exact=True).wait_for()
        assert page.get_by_text('Clinical rubric: awaiting_physician_authorship. 1 charts.').is_visible()
        screenshot(page,'ehr-admin.png')
        page.get_by_role('button',name='Preview compilation',exact=True).click()
        page.get_by_text('synthetic-task',exact=False).wait_for()
        assert not errors
        assert 'key_enc' not in page.locator('body').inner_text()
    finally:
        browser.close();playwright.stop();server.shutdown()


def test_ehr_outcome_review_is_submitted_before_future_reveal():
    playwright,browser=_launch_browser()
    try:
        page=browser.new_page(viewport={'width':1280,'height':900});errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.set_content('<body class="asc-body"><main class="asc-main" id="review"></main></body>')
        for stylesheet in ['_tokens.css','_base.css','asclepius.css']:
            page.add_style_tag(path=str(FRONTEND/stylesheet))
        source=(FRONTEND/'asclepius.js').read_text()
        helpers=source[source.index('  function h('):source.index('  // Copyable ids')]
        page.add_script_tag(content=helpers+'\nfunction clear(node){node.replaceChildren();}')
        page.add_script_tag(path=str(FRONTEND/'ehr/review.js'))
        data={'review_id':'synthetic-review','trigger':'outcome_flag','chart':[
            {'resourceType':'Patient','id':'synthetic','name':[{'text':'Morgan Example'}],'gender':'female','birthDate':'1965-01-01'},
            {'resourceType':'Observation','code':{'text':'Potassium'},'valueQuantity':{'value':4.2,'unit':'mmol/L'},'effectiveDateTime':'2031-02-24'}],
            'items':[{'item_id':'med-1','plan_a':{},'plan_b':{},'proposed_plan':{'decision':'lisinopril','action':'increase','daily_dose_mg':20}}],
            'proposed_medications':[{'decision':'lisinopril','daily_dose_mg':20}], 'outcome_available_after_submit':True}
        future={'resourceType':'Observation','code':{'text':'Later potassium'},'valueQuantity':{'value':6.2,'unit':'mmol/L'}}
        page.evaluate('''async ({data,future})=>{
          window.calls=[];
          const api=async(path,options)=>{calls.push({path,options});if(path.endsWith('/outcome'))return {outcome:[future]};if(path.endsWith('/verdict'))return {status:'resolved'};return data;};
          await EhrReviewSection.render(document.getElementById('review'),{h,clear,api},data.review_id);
        }''',{'data':data,'future':future})
        screenshot(page,'ehr-review-before-outcome.png')
        assert not page.locator('[role="status"]').inner_text(),page.locator('body').inner_text()
        assert page.locator('form').count(),page.locator('body').inner_text()
        assert not page.get_by_text('Later potassium',exact=True).count()
        assert not page.get_by_role('heading',name='Plan A',exact=True).is_visible()
        page.get_by_label('Proposed plan',exact=True).select_option('appropriate')
        page.get_by_label('Reason',exact=True).fill('The available chart supports this proposed clinical plan.')
        page.get_by_label('Confidence',exact=True).select_option('high')
        page.get_by_role('button',name='Submit review',exact=True).click()
        page.get_by_text('Later potassium',exact=True).wait_for()
        calls=page.evaluate('calls')
        assert next(i for i,c in enumerate(calls) if c['path'].endswith('/verdict'))<next(i for i,c in enumerate(calls) if c['path'].endswith('/outcome'))
        submission=next(c for c in calls if c['path'].endswith('/verdict'))['options']['body']
        assert submission['items'][0]['reference_decision']=='appropriate'
        assert not errors
    finally:
        browser.close();playwright.stop()
