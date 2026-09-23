"""Community sockets obey the same realm and current-session gates as REST."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import realm
from asclepius import auth as asc_auth, store as asc_store
from community import router as community_router, store as community_store
from community.ws import Hub


class Socket:
    def __init__(self, *, fail=False):
        self.events = []
        self.closed = False
        self.fail = fail

    async def send_json(self, event):
        if self.fail:
            raise ConnectionError("synthetic disconnected socket")
        self.events.append(event)

    async def close(self):
        self.closed = True


def test_hub_isolates_broadcast_dm_typing_and_presence_even_when_user_ids_match():
    async def run():
        hub = Hub()
        live, sandbox = Socket(), Socket()
        with realm.scoped("live"):
            assert await hub.connect(live, "same-user-id")
            assert await hub.online_user_ids() == ["same-user-id"]
        with realm.scoped("sandbox"):
            assert await hub.online_user_ids() == []
            assert await hub.connect(sandbox, "same-user-id")
        with realm.scoped("live"):
            for event in (
                {"type": "message.created", "message": {"body": "live fixture"}},
                {"type": "typing", "channel": "general", "user_id": "same-user-id"},
                {"type": "presence", "online": await hub.online_user_ids()},
            ):
                await hub.broadcast(event)
            await hub.send_to_users(["same-user-id"], {"type": "dm.created", "dm": "dm-live"})
            assert len(live.events) == 4
            assert sandbox.events == []
            # A same-id sandbox session must not hide the last live disconnect.
            assert await hub.disconnect(live) == "same-user-id"
            assert await hub.online_user_ids() == []
        with realm.scoped("sandbox"):
            assert await hub.online_user_ids() == ["same-user-id"]
            await hub.broadcast({"type": "typing", "channel": "general"})
            assert len(sandbox.events) == 1
            assert len(live.events) == 4
    asyncio.run(run())


def test_hub_reaping_and_presence_remain_in_the_failed_sockets_realm():
    async def run():
        hub = Hub()
        live, dead, sandbox = Socket(), Socket(fail=True), Socket()
        with realm.scoped("live"):
            await hub.connect(live, "live-reader")
            await hub.connect(dead, "disconnected-live-reader")
        with realm.scoped("sandbox"):
            await hub.connect(sandbox, "sandbox-reader")
        with realm.scoped("live"):
            await hub.broadcast({"type": "message.created"})
            assert dead.closed
            assert live.events[-1] == {"type": "presence", "online": ["live-reader"]}
        assert sandbox.events == []
    asyncio.run(run())


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "local-synthetic-sandbox-password")
    monkeypatch.setenv("ASCLEPIUS_AUTH_SECRET", "local-synthetic-ws-secret-0123456789")
    monkeypatch.setattr(asc_auth, "_cached_secret", None)
    monkeypatch.setattr(asc_store, "_STORES", {})
    monkeypatch.setattr(community_store, "_stores", {})
    monkeypatch.setattr(community_router, "hub", Hub())
    monkeypatch.setattr(community_router, "_ws_tickets", {})
    stores, users, tokens = {}, {}, {}
    for name in ("live", "sandbox"):
        with realm.scoped(name):
            store = asc_store.reset_store_for_tests(db_path=str(tmp_path / (name + ".db")))
            cstore = community_store.reset_community_store_for_tests(
                db_path=str(tmp_path / (name + "-community.db")))
            user = store.create_user(email=name + "@example.org", password="Original-password-7",
                                     role="evaluator", tier="labeler")
            store.set_verification_status(user["id"], "approved")
            user = store.get_user_by_id(user["id"])
            # Older issue time also exercises password invalidation on older
            # tokens that predate the version-claim rollout.
            payload = asc_auth.decode_token(asc_auth.create_token(user))
            payload["iat"] -= 120
            tokens[name] = jwt.encode(payload, asc_auth.get_asclepius_secret(), algorithm="HS256")
            stores[name], users[name] = (store, cstore), user
    app = FastAPI()
    app.add_middleware(realm.RealmMiddleware)
    app.include_router(community_router.router)

    @app.post("/synthetic-event")
    async def synthetic_event():
        await community_router.hub.broadcast({"type": "message.created", "message": {"body": "fixture"}})
        return {"ok": True}

    yield TestClient(app), stores, users, tokens


def headers(token):
    return {"Authorization": "Bearer " + token}


def test_legitimate_ticket_and_bearer_connections_keep_their_event_shapes(world):
    client, _, users, tokens = world
    for name in ("live", "sandbox"):
        res = client.post("/api/community/ws-ticket", headers=headers(tokens[name]))
        assert res.status_code == 200
        for query in ("ticket=" + res.json()["ticket"] + "&realm=" + name,
                      "token=" + tokens[name]):
            with client.websocket_connect("/api/community/ws?" + query) as ws:
                assert ws.receive_json() == {"type": "hello", "online": [users[name]["id"]]}
                ws.send_json({"type": "ping"})
                assert ws.receive_json() == {"type": "pong"}


def test_live_and_sandbox_connections_never_receive_each_others_events(world):
    client, _, _, tokens = world
    with client.websocket_connect("/api/community/ws?token=" + tokens["live"]) as live:
        assert live.receive_json()["type"] == "hello"
        with client.websocket_connect("/api/community/ws?token=" + tokens["sandbox"]) as sandbox:
            assert sandbox.receive_json()["type"] == "hello"
            client.post("/synthetic-event")
            assert live.receive_json()["type"] == "message.created"
            sandbox.send_json({"type": "ping"})
            assert sandbox.receive_json() == {"type": "pong"}
            client.post("/synthetic-event", headers={realm.HEADER: "sandbox"})
            assert sandbox.receive_json()["type"] == "message.created"
            live.send_json({"type": "ping"})
            assert live.receive_json() == {"type": "pong"}


@pytest.mark.parametrize("credential", ["token", "ticket"])
def test_password_change_invalidates_bearer_and_previously_minted_ticket_on_connect(world, credential):
    client, stores, users, tokens = world
    ticket = client.post("/api/community/ws-ticket", headers=headers(tokens["live"])).json()["ticket"]
    stores["live"][0].set_user_password(users["live"]["id"], "Replacement-password-8")
    query = "token=" + tokens["live"] if credential == "token" else "ticket=" + ticket
    with client.websocket_connect("/api/community/ws?" + query) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401
    # A fresh session still works after the password change.
    with realm.scoped("live"):
        fresh = asc_auth.create_token(stores["live"][0].get_user_by_id(users["live"]["id"]))
    with client.websocket_connect("/api/community/ws?token=" + fresh) as ws:
        assert ws.receive_json()["type"] == "hello"


@pytest.mark.parametrize("decision", ["password", "inactive", "banned", "rejected"])
def test_open_socket_stops_receiving_after_account_or_session_is_revoked(world, decision):
    client, stores, users, tokens = world
    store, cstore = stores["live"]
    uid = users["live"]["id"]
    with client.websocket_connect("/api/community/ws?token=" + tokens["live"]) as ws:
        assert ws.receive_json()["type"] == "hello"
        if decision == "password":
            store.set_user_password(uid, "Replacement-password-8")
        elif decision == "inactive":
            with store._conn() as conn:
                conn.execute("UPDATE users SET active = 0 WHERE id = ?", (uid,))
        elif decision == "banned":
            cstore.ban_member(user_id=uid, banned_by="synthetic-admin")
        else:
            store.set_verification_status(uid, "rejected")
        assert client.post("/synthetic-event").status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_rejected_user_cannot_reuse_an_older_vault_approval(world):
    client, stores, users, tokens = world
    store, _ = stores["live"]
    user = users["live"]
    store.upsert_contributor_credentials(id_hashed=user["id_hashed"], user_id=user["id"],
                                        credentials_verified=True, ship={}, verify={})
    store.set_verification_status(user["id"], "rejected")
    assert client.get("/api/community/me", headers=headers(tokens["live"])).status_code == 403
    with client.websocket_connect("/api/community/ws?token=" + tokens["live"]) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4403


def test_expired_session_stops_outbound_delivery(world, monkeypatch):
    client, _, _, tokens = world

    class AfterSessionExpiry:
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=8)

    with client.websocket_connect("/api/community/ws?token=" + tokens["live"]) as ws:
        assert ws.receive_json()["type"] == "hello"
        monkeypatch.setattr(jwt.api_jwt, "datetime", AfterSessionExpiry)
        assert client.post("/synthetic-event").status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_banned_socket_cannot_keep_sending_inbound_events(world):
    client, stores, users, tokens = world
    with client.websocket_connect("/api/community/ws?token=" + tokens["live"]) as ws:
        assert ws.receive_json()["type"] == "hello"
        stores["live"][1].ban_member(user_id=users["live"]["id"], banned_by="synthetic-admin")
        ws.send_json({"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4403
