"""Payments Rail §B: Connect Express onboarding, and the state machine.

The header on ``routers/asclepius_payments.py`` has promised for two releases
that Stripe would hold bank details and tax ids and that this codebase would
hold neither. §B is where that promise either survives contact with an
implementation or quietly stops being true, so the first test here asserts the
exact set of fields a physician's row gains: two, and no more.

The second thing under test is the state machine. ``coming_soon`` is a waiting
list the placeholder endpoint has been filling since before there was a rail,
and ``active`` is the word an admin payout run reads before moving money. Every
transition between them has to come from Stripe rather than from optimism.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from tests import _stripe_fake  # noqa: E402

client = TestClient(A.app)

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


@pytest.fixture()
def stripe_fake(monkeypatch):
    return _stripe_fake.install(monkeypatch)


def _store():
    from asclepius.store import get_store
    return get_store()


def _doctor():
    store = _store()
    user = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (user["id"],))
    return store.get_user_by_id(user["id"])


def _account_event(account_id, *, payouts_enabled, disabled_reason=None, event_id="evt_1"):
    return _stripe_fake.event_body(
        event_id, "account.updated",
        {"id": account_id, "payouts_enabled": payouts_enabled,
         "details_submitted": True,
         "requirements": {"disabled_reason": disabled_reason}})


def _post_webhook(body: bytes, *, secret="whsec_test_rail"):
    return client.post("/api/asclepius/stripe/webhook", content=body,
                       headers={"stripe-signature": _stripe_fake.sign(body, secret)})


def test_start_creates_account_once_and_stores_only_id_and_status(stripe_fake):
    """WHY: G1 is the file-header commitment, and it is a commitment about the
    SHAPE of what we keep, not about intent.

    The assertion is a diff of the physician's row across the whole onboarding
    flow. Exactly two fields may change. Anything else that appeared, however
    innocuous it looked in review, would be compliance state we now have to
    protect, breach-notify on, and keep in sync with Stripe.

    The second half is the account-versus-link split: one account forever
    (a second one is a second place their bank details could live and a payout
    destination that stops matching), a fresh link every call (account links are
    single use and expire in minutes).
    """
    doctor = _doctor()
    headers = A.headers_for(doctor)
    before = dict(_store().get_user_by_id(doctor["id"]))

    first = client.post("/api/asclepius/me/bank-link/start", headers=headers)
    assert first.status_code == 200, first.text
    second = client.post("/api/asclepius/me/bank-link/start", headers=headers)
    assert second.status_code == 200, second.text

    after = dict(_store().get_user_by_id(doctor["id"]))
    changed = {k for k in after if before.get(k) != after.get(k)}
    assert changed == {"stripe_account_id", "bank_link_status"}, (
        f"the rail wrote fields it is not allowed to hold: {sorted(changed)}")
    assert after["bank_link_status"] == "onboarding"
    assert after["stripe_account_id"].startswith("acct_")

    # One account, ever. Two links, both fresh.
    assert len(stripe_fake.account_create_calls) == 1
    assert len(stripe_fake.account_link_calls) == 2
    assert first.json()["url"] != second.json()["url"]
    assert stripe_fake.account_link_calls[0]["type"] == "account_onboarding"
    # Return and refresh both come back into the portal, so an expired link is a
    # round trip rather than a dead end.
    assert stripe_fake.account_link_calls[0]["return_url"]
    assert stripe_fake.account_link_calls[0]["refresh_url"]


def test_start_asks_stripe_for_transfers_only(stripe_fake):
    """WHY: requesting card_payments would make us merchant of record for these
    accounts, a materially different regulatory posture for no benefit. Money
    goes out on this rail and never comes in."""
    client.post("/api/asclepius/me/bank-link/start", headers=A.headers_for(_doctor()))
    capabilities = stripe_fake.account_create_calls[0]["capabilities"]
    assert set(capabilities) == {"transfers"}
    assert stripe_fake.account_create_calls[0]["type"] == "express"


def test_a_waiting_list_row_keeps_its_meaning_until_onboarding_starts(stripe_fake):
    """WHY: B3. ``coming_soon`` is the register of physicians who tapped the
    placeholder card, and the go-live nudge is addressed to exactly those rows.
    Onboarding must be what moves them off it, not the deploy."""
    doctor = _doctor()
    headers = A.headers_for(doctor)
    client.post("/api/asclepius/me/bank-link/interest", headers=headers)
    assert _store().get_user_by_id(doctor["id"])["bank_link_status"] == "coming_soon"

    client.post("/api/asclepius/me/bank-link/start", headers=headers)
    assert _store().get_user_by_id(doctor["id"])["bank_link_status"] == "onboarding"


def test_account_updated_webhook_moves_status(stripe_fake):
    """WHY: B3/D2. The state machine is what an admin payout run reads before
    moving money, so every transition in it has to come from Stripe.

    Including the recovery leg: a restricted physician who uploads the document
    Stripe asked for must come back to ``active`` on their own, without an
    operator noticing and editing a column.
    """
    doctor = _doctor()
    client.post("/api/asclepius/me/bank-link/start", headers=A.headers_for(doctor))
    account_id = _store().get_user_by_id(doctor["id"])["stripe_account_id"]

    def status():
        return _store().get_user_by_id(doctor["id"])["bank_link_status"]

    assert status() == "onboarding"

    assert _post_webhook(_account_event(account_id, payouts_enabled=True,
                                        event_id="evt_active")).status_code == 200
    assert status() == "active"

    # Stripe can disable payouts on an account that was working: an expiring
    # document, a mismatch found in review.
    assert _post_webhook(_account_event(account_id, payouts_enabled=False,
                                        disabled_reason="requirements.past_due",
                                        event_id="evt_restricted")).status_code == 200
    assert status() == "restricted"

    # And the recovery leg.
    assert _post_webhook(_account_event(account_id, payouts_enabled=True,
                                        event_id="evt_recovered")).status_code == 200
    assert status() == "active"


def test_a_disabled_reason_outranks_a_stale_payouts_enabled(stripe_fake):
    """WHY: the two fields answer different questions and can disagree during a
    review. ``disabled_reason`` is Stripe saying it will not pay this person, and
    treating a stale ``payouts_enabled`` as authoritative is how a payout run
    settles a ledger row for someone who cannot be paid."""
    from asclepius import stripe_rail

    account = {"payouts_enabled": True,
               "requirements": {"disabled_reason": "under_review"}}
    assert stripe_rail.status_for_account(account) == "restricted"


def test_get_bank_link_reads_payouts_state_live_and_caches_nothing(stripe_fake):
    """WHY: G1. A cached copy of compliance state is stale from the moment
    Stripe updates it, and the version of that bug that matters is telling a
    physician they are good to be paid after Stripe decided otherwise."""
    doctor = _doctor()
    headers = A.headers_for(doctor)
    client.post("/api/asclepius/me/bank-link/start", headers=headers)
    account_id = _store().get_user_by_id(doctor["id"])["stripe_account_id"]

    read = client.get("/api/asclepius/me/bank-link", headers=headers)
    assert read.status_code == 200
    assert read.json()["payouts_enabled"] is False
    assert read.json()["bank_link_status"] == "onboarding"

    # Stripe changes its mind and no webhook arrives. The read is the backstop.
    stripe_fake.set_account_state(account_id, payouts_enabled=True,
                                  requirements={"disabled_reason": None})
    read = client.get("/api/asclepius/me/bank-link", headers=headers)
    assert read.json()["payouts_enabled"] is True
    assert read.json()["bank_link_status"] == "active"
    assert _store().get_user_by_id(doctor["id"])["bank_link_status"] == "active"


def test_the_bank_link_read_never_returns_a_requirements_list(stripe_fake):
    """WHY: Stripe's ``requirements.currently_due`` names identity documents.
    The physician-facing surface gets booleans and a reason string; the list
    itself is compliance detail we have no reason to relay or to hold."""
    doctor = _doctor()
    headers = A.headers_for(doctor)
    client.post("/api/asclepius/me/bank-link/start", headers=headers)
    account_id = _store().get_user_by_id(doctor["id"])["stripe_account_id"]
    stripe_fake.set_account_state(
        account_id, requirements={"disabled_reason": "requirements.past_due",
                                  "currently_due": ["individual.id_number",
                                                    "external_account"]})
    body = client.get("/api/asclepius/me/bank-link", headers=headers).json()
    assert "individual.id_number" not in json.dumps(body)
    assert "currently_due" not in body
    assert body["disabled_reason"] == "requirements.past_due"


def test_a_slow_stripe_does_not_break_the_physicians_own_page(stripe_fake, monkeypatch):
    """WHY: this endpoint renders on a physician's earnings surface. An upstream
    timeout should degrade to the stored status, not blank the page they use to
    see what they have earned."""
    from asclepius import stripe_rail

    doctor = _doctor()
    headers = A.headers_for(doctor)
    client.post("/api/asclepius/me/bank-link/start", headers=headers)

    def _boom(_account_id):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(stripe_rail, "retrieve_account", _boom)
    body = client.get("/api/asclepius/me/bank-link", headers=headers)
    assert body.status_code == 200
    assert body.json()["bank_link_status"] == "onboarding"
    assert body.json()["live"] is False


def test_the_session_payload_announces_the_rail_only_when_it_is_live(stripe_fake):
    """WHY: B4. The portal decides between the placeholder card and a live
    control from this one key, and the key is absent while dark so an older
    server and a dark server look identical to the client."""
    body = client.get("/api/asclepius/auth/me", headers=A.headers_for(_doctor()))
    assert body.json()["bank_link_enabled"] is True


# ═════════════════════════════════════════════════════════════════════════════
# B4, on screen
# ═════════════════════════════════════════════════════════════════════════════
def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


_JS = """
require(%(shim)s);
function h(tag, attrs) {
  var el = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    var v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class' || k === 'className') el.className = v;
    else if (k === 'disabled') { if (v) el.setAttribute('disabled', ''); }
    else if (k.slice(0, 2) === 'on' && typeof v === 'function') {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else el.setAttribute(k, v);
  }
  for (var i = 2; i < arguments.length; i++) append_(el, arguments[i]);
  return el;
}
function append_(el, c) {
  if (c == null || c === '' || c === false) return;
  if (Array.isArray(c)) { c.forEach(function (x) { append_(el, x); }); return; }
  el.appendChild((typeof c === 'string' || typeof c === 'number')
                 ? document.createTextNode(String(c)) : c);
}
var apiCalls = [];
var rootNode = null;
var toasts = [];
var ctx = {
  h: h,
  setRoot: function (node) { rootNode = node; },
  toast: function (m) { toasts.push(m); },
  user: %(user)s,
  api: function (path, opts) {
    apiCalls.push({ path: path, method: (opts && opts.method) || 'GET' });
    if (path === '/assets/onboarding-demo/meta') return Promise.resolve({ available: false });
    if (path === '/me/bank-link/start') return Promise.resolve(%(start)s);
    return Promise.resolve({});
  },
  onUser: function () {}, startTutorial: function () {}, openCommunity: function () {},
  setPanel: function () {}, exit: function () {},
};
globalThis.localStorage = { _v: {}, getItem: function (k) { return this._v[k] || null; },
  setItem: function (k, v) { this._v[k] = v; }, removeItem: function (k) { delete this._v[k]; } };
window.addEventListener = function () {};
window.removeEventListener = function () {};
window.location = { href: '' };
globalThis.URL = globalThis.URL || { createObjectURL: function () { return 'blob:x'; },
                                     revokeObjectURL: function () {} };
function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function find(el, cls) {
  var out = [];
  (el.childNodes || []).forEach(function (c) {
    if (!c.tagName) return;
    if (c.classList && c.classList.contains(cls)) out.push(c);
    out = out.concat(find(c, cls));
  });
  return out;
}
function done(fn) { setTimeout(function () { fn(); }, 0); }
eval(require('fs').readFileSync(%(module)s, 'utf8'));
"""


def _js(*, enabled: bool, start=None) -> str:
    user = {"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done", "community": "done"}}}
    if enabled:
        user["bank_link_enabled"] = True
    return _JS % {
        "shim": json.dumps(str(Path(__file__).resolve().parent / "_asclepius_dom.js")),
        "module": json.dumps(str(_FRONTEND / "first_run.js")),
        "user": json.dumps(user),
        "start": json.dumps(start if start is not None
                            else {"ok": True, "bank_link_status": "onboarding",
                                  "url": "https://connect.stripe.test/setup/1"}),
    }


def test_the_earnings_stop_renders_a_live_card_behind_the_flag():
    """WHY: B4. The card was disabled and clearly labelled because it did
    nothing. Once it does something, the same words have to become a control
    that starts onboarding, and the copy has to say whose form the doctor is
    about to type a bank account number into."""
    out = _run_node(_js(enabled=True) + """
      window.FirstRunWalkthrough.resume(ctx);
      done(function () {
        var bank = find(rootNode, 'asc-fr-bank')[0];
        bank.dispatch('click');
        done(function () {
          console.log(JSON.stringify({
            disabled: bank.getAttribute('disabled'),
            live: bank.classList.contains('asc-fr-bank-live'),
            text: textOf(rootNode),
            calls: apiCalls.map(function (c) { return c.method + ' ' + c.path; }),
            href: window.location.href,
          }));
        });
      });
    """)
    assert out["live"], "the live card did not render behind the flag"
    assert "coming soon" not in out["text"]
    assert "Stripe collects your bank and tax details" in out["text"]
    assert "POST /me/bank-link/start" in out["calls"]
    assert out["href"] == "https://connect.stripe.test/setup/1"
    # The waiting-list POST is pointless once there is nothing left to wait for.
    assert not any("interest" in c for c in out["calls"])


def test_the_earnings_stop_is_the_placeholder_without_the_flag():
    """WHY: the flag-off screen must be the one that shipped, down to the chip
    and the promise of a DM."""
    out = _run_node(_js(enabled=False) + """
      window.FirstRunWalkthrough.resume(ctx);
      done(function () {
        var bank = find(rootNode, 'asc-fr-bank')[0];
        console.log(JSON.stringify({
          disabled: bank.getAttribute('disabled') === '',
          aria: bank.getAttribute('aria-disabled'),
          live: bank.classList.contains('asc-fr-bank-live'),
          text: textOf(rootNode),
        }));
      });
    """)
    assert out["disabled"] and out["aria"] == "true"
    assert not out["live"]
    assert "coming soon" in out["text"]
    assert "we’ll DM you the moment it does" in out["text"]
