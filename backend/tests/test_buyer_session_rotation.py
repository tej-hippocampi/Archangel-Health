"""Password rotation keeps the buyer workspace usable and retires old sessions."""
import json
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient

from tests import _asclepius as A


def test_buyer_password_rotation_refreshes_caller_and_keeps_role_and_password_checks():
    store = A.fresh_store()
    store.provision_buyer(email="synthetic-buyer@example.org", password="Initial-password-123")
    client = TestClient(A.app)
    login = client.post("/api/asclepius/auth/login", json={
        "email": "synthetic-buyer@example.org", "password": "Initial-password-123"})
    assert login.status_code == 200, login.text
    old = {"Authorization": "Bearer " + login.json()["token"]}
    reset = client.post("/api/asclepius/buyer/password", headers=old,
                        json={"new_password": "Replacement-password-456"})
    assert reset.status_code == 200, reset.text
    current = {"Authorization": "Bearer " + reset.json()["token"]}
    assert client.get("/api/asclepius/buyer/me", headers=old).status_code == 401
    assert client.get("/api/asclepius/buyer/me", headers=current).json()["must_reset_password"] is False
    assert client.get("/api/asclepius/buyer/deliveries", headers=current).status_code == 200
    assert client.get("/api/asclepius/tasks/next", headers=current).status_code == 403

    denied = client.post("/api/asclepius/buyer/password", headers=current,
                         json={"current_password": "incorrect", "new_password": "Another-password-789"})
    assert denied.status_code == 400 and "token" not in denied.json()
    assert client.get("/api/asclepius/buyer/me", headers=current).status_code == 200
    changed = client.post("/api/asclepius/buyer/password", headers=current,
                          json={"current_password": "Replacement-password-456", "new_password": "Another-password-789"})
    assert changed.status_code == 200, changed.text
    assert client.get("/api/asclepius/buyer/me", headers=current).status_code == 401
    assert client.get("/api/asclepius/buyer/deliveries", headers={
        "Authorization": "Bearer " + changed.json()["token"]}).status_code == 200


def test_buyer_browser_stores_rotated_token_before_existing_workspace_transition():
    node = shutil.which("node")
    assert node, "Node is required for the buyer transport regression"
    source = (Path(__file__).resolve().parents[2] / "frontend/buyer/buyer.js").read_text()
    script = r'''
const vm = require('vm');
const assert = require('assert/strict');
async function scenario(realm) {
  const elements = new Map(), timers = [], requests = [];
  const key = 'asclepius_buyer_token' + (realm === 'sandbox' ? '_sandbox' : '');
  const storage = new Map([[key, 'old-session']]);
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', firstChild: null, listeners: {},
      content: {cloneNode: () => ({})},
      appendChild() {}, removeChild() {}, focus() {},
      addEventListener(type, fn) { this.listeners[type] = fn; },
    });
    return elements.get(id);
  }
  const context = {
    window: {__REALM: realm}, document: {getElementById: element, createElement: element},
    localStorage: {getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k)},
    setTimeout: fn => { timers.push(fn); },
    fetch: async (path, opts) => {
      requests.push([path, opts]);
      assert.equal(opts.headers['X-Asclepius-Realm'], realm);
      let data;
      if (path.endsWith('/buyer/me')) data = {email: 'synthetic-buyer@example.org', must_reset_password: true};
      else if (path.endsWith('/buyer/password')) {
        assert.equal(opts.headers.Authorization, 'Bearer old-session');
        data = {ok: true, token: 'fresh-session'};
      } else if (path.endsWith('/buyer/deliveries')) {
        assert.equal(opts.headers.Authorization, 'Bearer fresh-session');
        data = {deliveries: []};
      } else throw new Error('Unexpected route: ' + path);
      return {ok: true, status: 200, json: async () => data};
    },
  };
  vm.runInNewContext(SOURCE, context);
  await new Promise(setImmediate);
  element('bwNewPw').value = element('bwConfirmPw').value = 'Replacement-password-456';
  await element('bwResetForm').listeners.submit({preventDefault() {}});
  assert.equal(storage.get(key), 'fresh-session');
  assert.equal(element('bwResetOk').textContent, 'Password updated. Opening your workspace…');
  assert.equal(timers.length, 1);
  timers.shift()();
  await new Promise(setImmediate);
  assert.equal(requests.at(-1)[0], '/api/asclepius/buyer/deliveries');
  assert.equal(element('bwHeader').hidden, false);
}
(async () => { await scenario('live'); await scenario('sandbox'); })()
  .catch(e => { console.error(e); process.exitCode = 1; });
'''
    completed = subprocess.run([node, "-e", "const SOURCE=" + json.dumps(source) + ";\n" + script],
                               capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
