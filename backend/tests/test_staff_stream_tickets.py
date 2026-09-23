"""Opaque SSE tickets authorize one path/realm without exposing staff sessions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import realm
import staff_stream
import token_revocation
from routers import eligibility, gold
from tests._role_auth import tenant_token

PATHS = ["/api/eligibility-checks/check-a/stream", "/api/eligibility-batches/batch-a/stream",
         "/api/gold/visits/visit-a/stream"]


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-stream-sandbox-password")
    monkeypatch.setattr(staff_stream, "_tickets", {})
    app = FastAPI()
    app.add_middleware(realm.RealmMiddleware)
    app.include_router(eligibility.router)
    app.include_router(gold.router)
    app.state.patient_store = {"patient-a": {"name": "Synthetic", "health_system_id": "stream-a"}}
    def check(_):
        queue = asyncio.Queue()
        queue.put_nowait({"event": "result", "data": {"status": "DONE"}})
        return {"id": "check-a", "patient_id": "patient-a", "status": "DONE", "ring": [], "queue": queue}
    def batch(_):
        queue = asyncio.Queue()
        queue.put_nowait({"event": "done", "data": {"created": 1}})
        return {"id": "batch-a", "health_system_id": "stream-a", "status": "DONE", "ring": [], "queue": queue}
    monkeypatch.setattr(eligibility.store, "get_check", check)
    monkeypatch.setattr(eligibility.store, "get_batch", batch)
    monkeypatch.setattr(gold.store, "get_visit", lambda _: {"id": "visit-a", "tenant_id": "stream-a", "status": "NEEDS_REVIEW"})
    monkeypatch.setattr(gold.store, "get_queue", lambda _: None)
    monkeypatch.setattr(gold.store, "get_ring", lambda _: [])
    monkeypatch.setattr(gold.store, "drop_streams", lambda _: None)
    tokens = {}
    for name in ("live", "sandbox"):
        with realm.scoped(name):
            tokens[name] = tenant_token(health_system_id="stream-a")
    return TestClient(app), tokens


def headers(token):
    return {"Authorization": "Bearer " + token}


def mint(client, token, path):
    response = client.post(path + "-ticket", headers=headers(token))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert token not in response.text and "." not in data["ticket"]
    return {"ticket": data["ticket"], "realm": data["realm"]}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("name", ["live", "sandbox"])
def test_legitimate_stream_and_header_client_keep_existing_events(world, path, name):
    client, tokens = world
    params = mint(client, tokens[name], path)
    response = client.get(path, params=params)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: " + ("done" if "batches" in path else "result") in response.text
    assert client.get(path, params=params).status_code == 401  # single use
    assert client.get(path, headers=headers(tokens[name])).status_code == 200
    assert client.get(path, params=mint(client, tokens[name], path)).status_code == 200  # reconnect


@pytest.mark.parametrize("path", PATHS)
def test_ticket_mint_and_redemption_enforce_staff_and_resource_scope(world, path):
    client, tokens = world
    assert client.post(path + "-ticket").status_code == 401
    foreign = tenant_token(health_system_id="stream-other")
    assert client.post(path + "-ticket", headers=headers(foreign)).status_code == 404
    assert client.get(path, params={"token": tokens["live"]}).status_code == 401
    assert client.post(path + "-ticket", params={"token": tokens["live"]}).status_code == 401
    params = mint(client, tokens["live"], path)
    assert client.get(path.replace("-a/stream", "-b/stream"), params=params).status_code == 401
    assert client.get(path, params={**params, "realm": "sandbox"}).status_code == 401
    other_path = next(other for other in PATHS if other != path)
    assert client.get(other_path, params=params).status_code == 401
    assert client.get(path.removesuffix("/stream"), params=params).status_code == 401
    assert client.get(path.removesuffix("/stream"), headers=headers(params["ticket"])).status_code == 401
    assert client.get(path, params=params).status_code == 200  # mismatched path did not burn it


@pytest.mark.parametrize("reason", ["expired_ticket", "wrong_purpose", "revoked_session"])
def test_expired_or_wrong_purpose_ticket_and_revoked_original_session_are_denied(world, monkeypatch, reason):
    client, tokens = world
    path = PATHS[0]
    params = mint(client, tokens["live"], path)
    key = hashlib.sha256(params["ticket"].encode()).hexdigest()
    row = staff_stream._tickets[key]
    if reason == "expired_ticket":
        staff_stream._tickets[key] = replace(row, expires=0)
    elif reason == "wrong_purpose":
        staff_stream._tickets[key] = replace(row, purpose="other")
    else:
        # The existing staff resolver owns session expiry/revocation semantics.
        async def revoked(**kwargs):
            assert kwargs["authorization"] == "Bearer " + tokens["live"]
            return None
        monkeypatch.setattr(staff_stream, "get_staff_context_optional", revoked)
    assert client.get(path, params=params).status_code == 401


def test_ticket_storage_is_bounded_and_expired_entries_are_reclaimed(world, monkeypatch):
    client, tokens = world
    monkeypatch.setattr(staff_stream, "MAX_PENDING_TICKETS", 1)
    mint(client, tokens["live"], PATHS[0])
    assert client.post(PATHS[0] + "-ticket", headers=headers(tokens["live"])).status_code == 503
    key, row = next(iter(staff_stream._tickets.items()))
    staff_stream._tickets[key] = replace(row, expires=0)
    assert mint(client, tokens["live"], PATHS[0])
    assert len(staff_stream._tickets) == 1


def test_minted_ticket_cannot_outlive_revoked_staff_session(world):
    client, tokens = world
    params = mint(client, tokens["live"], PATHS[0])
    assert token_revocation.revoke_token(tokens["live"])
    assert client.get(PATHS[0], params=params).status_code == 401
    assert client.post(PATHS[0] + "-ticket", headers=headers(tokens["live"])).status_code == 401
    replacement = tenant_token(health_system_id="stream-a")
    assert client.get(PATHS[0], params=mint(client, replacement, PATHS[0])).status_code == 200


def test_concurrent_ticket_redemption_allows_exactly_one_connection(world):
    client, tokens = world
    params = mint(client, tokens["live"], PATHS[0])
    ready = threading.Barrier(8)

    def connect(_):
        ready.wait(timeout=10)
        return client.get(PATHS[0], params=params).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(connect, range(8)))
    assert sorted(statuses) == [200] + [401] * 7


def test_browser_transport_uses_fresh_ticket_on_reconnect_and_closes_cleanly():
    node = shutil.which("node")
    assert node, "Node is required for the staff transport regression"
    page = (Path(__file__).resolve().parents[2] / "frontend/doctor.html").read_text()
    source = page[page.index("    function openStaffStream(path)"):page.index("    async function streamGeneration(")]
    script = r'''
const vm = require('vm');
const assert = require('assert/strict');
const instances = [], timers = [], requests = [];
class Source {
  constructor(url) { this.url = url; this.listeners = {}; instances.push(this); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  close() { this.closed = true; }
}
const context = { EventSource: Source, URLSearchParams, TypeError, API: 'https://local.invalid',
  apiJson: async (path, opts) => { requests.push([path, opts]); return {ticket:'opaque-' + requests.length, realm:'sandbox'}; },
  setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout: id => { timers[id-1] = null; } };
vm.createContext(context);
vm.runInContext(SOURCE + '; this.open = openStaffStream;', context);
(async () => {
  const values = [], errors = [];
  const stream = context.open('/api/eligibility-checks/check-a/stream');
  stream.addEventListener('status', e => values.push(e.data));
  stream.addEventListener('error', e => errors.push(e.data));
  await new Promise(setImmediate);
  assert.equal(requests[0][0], '/api/eligibility-checks/check-a/stream-ticket');
  assert.equal(requests[0][1].method, 'POST');
  let url = new URL(instances[0].url);
  assert.equal(url.searchParams.get('ticket'), 'opaque-1');
  assert.equal(url.searchParams.get('realm'), 'sandbox');
  assert.equal(url.searchParams.has('token'), false);
  instances[0].listeners.status({data:'first'});
  instances[0].onerror({});
  assert.equal(instances[0].closed, true);
  assert.equal(errors.length, 0); // A network blip is not an application failure.
  timers.shift()();
  await new Promise(setImmediate);
  url = new URL(instances[1].url);
  assert.equal(url.searchParams.get('ticket'), 'opaque-2');
  instances[1].listeners.status({data:'second'});
  instances[1].onerror({data:'{"message":"fixture failure"}'});
  assert.equal(errors.length, 1);
  assert.deepEqual(values, ['first','second']);
  stream.close();
  assert.equal(instances[1].closed, true);
  const cancelled = context.open('/api/eligibility-batches/batch-a/stream');
  cancelled.close();
  await new Promise(setImmediate);
  assert.equal(instances.length, 2); // Closing while mint is pending never opens a socket.
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    completed = subprocess.run([node, "-e", "const SOURCE=" + json.dumps(source) + ";\n" + script],
                               capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "?token=" not in page
