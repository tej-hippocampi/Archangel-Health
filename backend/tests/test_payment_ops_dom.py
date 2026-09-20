"""Exercise the admin money UI against the existing DOM harness."""
import json
from tests.test_payments_earnings_dom import _JS_CTX, _run_node, _DOM_SHIM, _FRONTEND


def run(body, *, failure=False):
    prefix = '/api/asclepius/admin/payment-ops'
    routes = {
        prefix: {'eligible': [], 'tax_accounts': [], 'mode': 'test', 'bank_payouts': [], 'batches': [
            {'batch_id': 'pb_example', 'status': 'draft', 'stripe_mode': 'test', 'total_cents': 7500,
             'fingerprint': 'a' * 64, 'items': [{'email': 'physician@example.test', 'destination': 'acct_one',
                'amount_cents': 7500, 'status': 'queued', 'earning_id': 'e_one'}]}]},
        prefix + '/readiness': {'mode': 'test', 'stripe_check': 'unavailable', 'tax_year': 2026,
            'worker_enabled': False, 'live_execution_enabled': False, 'available_usd_cents': None},
    }
    script = _JS_CTX % {'shim': json.dumps(str(_DOM_SHIM)), 'module': json.dumps(str(_FRONTEND / 'admin_payment_ops.js')),
        'routes': json.dumps(routes), 'fail': json.dumps({prefix: {'detail': 'Offline'}} if failure else {})}
    return _run_node(script + '''
function nodes(el, tag) { var out=[]; (el.childNodes||[]).forEach(function(c) {
  if(c.tagName===tag) out.push(c); out=out.concat(nodes(c,tag)); }); return out; }
var body = document.createElement('div');
window.AdminPaymentOpsSection.render(body, ctx);
''' + body)


def test_preview_requires_explicit_approval_and_renders_exact_total():
    data = run('''done(function() {
var approve = nodes(body, 'BUTTON').find(function(b) { return textOf(b).startsWith('Approve $75.00'); });
var check = nodes(body, 'INPUT').find(function(i) { return i.getAttribute('type')==='checkbox'; });
var initial = approve.disabled;
check.checked=true; check.dispatch('change');
var posts=[]; ctx.api=function(path,opts){ posts.push({path:path,body:JSON.parse(opts.body)}); return Promise.resolve({}); };
approve.dispatch('click');
done(function(){ console.log(JSON.stringify({initial:initial, posts:posts, text:textOf(body)})); });
});''')
    assert data['initial'] is True
    assert data['posts'][0]['path'].endswith('/batches/pb_example/approve')
    assert data['posts'][0]['body'] == {'fingerprint': 'a' * 64}


def test_unknown_funding_never_shows_zero_and_no_bank_events_is_explicit():
    data = run("done(function(){console.log(JSON.stringify({text:textOf(body)}));});")
    assert 'Not supplied' in data['text']
    assert 'No bank payout events received yet' in data['text']
    assert 'TEST environment' in data['text']


def test_api_failure_is_visible():
    data = run("done(function(){console.log(JSON.stringify({text:textOf(body)}));});", failure=True)
    assert 'Payments unavailable' in data['text'] and 'Offline' in data['text']
