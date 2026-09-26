"""Bank setup must survive provider failures and never outlive its physician session."""
import json
from pathlib import Path

import pytest

from tests._js_source import strip_js_comments
from tests.test_first_run_dom import _ctx, _run_node

FRONT = Path(__file__).resolve().parents[2] / 'frontend/asclepius'


def shell_function(name):
    source = strip_js_comments((FRONT / 'asclepius.js').read_text())
    start = source.index('function ' + name + '(')
    return source[start:source.index('\n  function ', start + 10)]


def context():
    return _ctx() + '\n' + shell_function('setRoot') + """
      var state = {screenGeneration:0};
      var host = h('main', {id:'ascRoot'}); document.body.appendChild(host);
      function root(){return host;} function closeTagPopover(){}
      function clear(el){while(el.firstChild)el.removeChild(el.firstChild);}
      ctx.clear = clear; ctx.fmtDate = String;
      var session = 'doctor-A';
      ctx.isCurrentSession = function(){return session === 'doctor-A';};
      var notices = []; ctx.toast = function(text){notices.push(text);};
      window.location = {href:'/portal'};
      // Implement the native button activation missing from the layout-free shim.
      Object.getPrototypeOf(host).click = function(){
        if (!this.hasAttribute('disabled')) this.dispatch('click');
      };
      function deferred(){var resolve,reject;var promise=new Promise(function(a,b){resolve=a;reject=b;});return {promise,resolve,reject};}
      function tick(){return new Promise(function(resolve){setTimeout(resolve,0);});}
      var pendingBank=deferred(), pendingStart=deferred(), pendingEarnings=deferred();
      ctx.bankLinkEnabled=true;
      ctx.api=function(path,opts){apiCalls.push({path,session});
        if(path==='/me/bank-link')return pendingBank.promise;
        if(path==='/me/bank-link/start')return pendingStart.promise;
        if(path==='/earnings')return pendingEarnings.promise;
        return Promise.reject(Error('Unexpected route '+path));};
    """ + f"eval(require('fs').readFileSync({json.dumps(str(FRONT / 'earnings.js'))}, 'utf8'));\n"


@pytest.mark.parametrize('action,starts', [('return', 0), ('refresh', 1)])
def test_return_checks_status_and_only_expiry_renews(action, starts):
    out = _run_node(context() + f"ctx.bankLinkAction={json.dumps(action)};" + """
      (async function(){
        window.EarningsSection.render(host,ctx);
        pendingBank.resolve({live:true,connected:true,bank_link_status:'onboarding',details_submitted:false,payouts_enabled:false});
        await tick();
        console.log(JSON.stringify({calls:apiCalls,text:host.textContent}));
      })();
    """)
    assert sum(c['path'].endswith('/start') for c in out['calls']) == starts
    assert 'Loading your earnings' in out['text']
    assert ('Opening Stripe' if starts else 'Continue Stripe setup') in out['text']


@pytest.mark.parametrize('depart', ['setRoot(h("div",{},"Different screen"));', "session='doctor-B';"])
def test_delayed_expiry_status_cannot_start_after_departure(depart):
    out = _run_node(context() + """
      (async function(){
        ctx.bankLinkAction='refresh'; window.EarningsSection.render(host,ctx);
    """ + depart + """
        pendingBank.resolve({live:true,connected:true,bank_link_status:'onboarding',payouts_enabled:false});
        await tick(); console.log(JSON.stringify(apiCalls));
      })();
    """)
    assert [c['path'] for c in out] == ['/me/bank-link', '/earnings']


@pytest.mark.parametrize('depart', ['setRoot(h("div",{},"Different screen"));', "session='doctor-B';"])
def test_pending_onboarding_cannot_redirect_after_departure(depart):
    out = _run_node(context() + """
      (async function(){
        var button=window.FirstRunWalkthrough.bankCard(ctx);host.appendChild(button);
        button.click(); await tick();
    """ + depart + """
        pendingStart.resolve({url:'https://connect.stripe.com/setup/test'});
        await tick(); console.log(JSON.stringify({href:window.location.href,calls:apiCalls,notices}));
      })();
    """)
    assert out['href'] == '/portal'
    assert len(out['calls']) == 1
    assert not out['notices']


def test_double_click_dispatches_once_and_failure_allows_retry():
    out = _run_node(context() + """
      (async function(){
        var button=window.FirstRunWalkthrough.bankCard(ctx);host.appendChild(button);
        button.dispatch('click');button.dispatch('click');
        var busy=button.getAttribute('aria-busy');await tick();
        pendingStart.reject(Error('Stripe temporarily unavailable'));await tick();
        var retryable=!button.hasAttribute('disabled')&&!button.hasAttribute('aria-busy');
        pendingStart=deferred();button.click();await tick();
        pendingStart.resolve({url:'https://connect.stripe.com/setup/test'});await tick();
        console.log(JSON.stringify({busy,retryable,calls:apiCalls,notices,href:window.location.href}));
      })();
    """)
    assert out['busy'] == 'true'
    assert out['retryable']
    assert len(out['calls']) == 2
    assert len(out['notices']) == 1
    assert out['href'] == 'https://connect.stripe.com/setup/test'


@pytest.mark.parametrize('bank,expected,forbidden', [
    ({'live': True, 'connected': True, 'bank_link_status': 'active', 'payouts_enabled': True}, 'Bank account connected', 'Continue Stripe setup'),
    ({'live': False, 'connected': True, 'bank_link_status': 'active', 'payouts_enabled': None}, 'Check connection again', 'Bank account connected'),
    ({'live': True, 'connected': True, 'bank_link_status': 'restricted', 'payouts_enabled': False}, 'Continue Stripe setup', 'Bank account connected'),
    ({'connected': False}, 'Link your bank account', 'Bank account connected'),
])
def test_bank_state_remains_visible_when_ledger_fails(bank, expected, forbidden):
    out = _run_node(context() + f'var bank={json.dumps(bank)};' + """
      (async function(){
        window.EarningsSection.render(host,ctx);
        pendingBank.resolve(bank);pendingEarnings.reject(Error('Ledger unavailable'));
        await tick();console.log(JSON.stringify({text:host.textContent,calls:apiCalls}));
      })();
    """)
    assert 'Your earnings could not be loaded' in out['text']
    assert expected in out['text']
    assert forbidden not in out['text']
    assert not any(c['path'].endswith('/start') for c in out['calls'])


@pytest.mark.parametrize('hash_value,action', [('#earnings', 'return'), ('#earnings?stripe=return', 'return'), ('#earnings?stripe=refresh', 'refresh')])
def test_stripe_return_routes_to_earnings_before_walkthrough(hash_value, action):
    out = _run_node(_ctx() + shell_function('readBankLinkHash') + shell_function('enterApp') + f"var location={{hash:{json.dumps(hash_value)},pathname:'/asclepius/',search:''}};" + """
      var history={replaceState:function(){location.hash='';}};
      var state={user:{role:'evaluator'}};
      function isAdminUser(){return false;} function isAdvisor(){return false;}
      function isReferralOnly(){return false;} function sessionHasSurface(s){return s==='earnings';}
      function resetCommunityState(){} function renderHeader(){} function renderSidePanel(){}
      function startCommunityPolling(){} function setPanel(name){handoffs.push(name);}
      enterApp();
      console.log(JSON.stringify({handoffs,action:state.bankLinkAction,hash:location.hash}));
    """)
    assert out == {'handoffs': ['earnings'], 'action': action, 'hash': ''}
