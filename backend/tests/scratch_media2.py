from fastapi.testclient import TestClient
from tests._asclepius import app, fresh_store, make_user, headers_for, token_for
from asclepius import assets, auth as asc_auth

def test_ticket_and_range():
    store = fresh_store()
    u = make_user(store)
    data = b"y" * 3000 + b"TAIL"
    meta = assets.store_media(iter([data]), "video/mp4")
    store.set_platform_media("onboarding_demo", sha256=meta["sha256"], mime="video/mp4",
                             byte_size=meta["byte_size"])
    c = TestClient(app)
    r = c.post("/api/asclepius/assets/onboarding-demo/ticket", headers=headers_for(u))
    assert r.status_code == 200, r.text
    ticket = r.json()["ticket"]
    r = c.get("/api/asclepius/assets/onboarding-demo?t=" + ticket, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == data[:10], (r.status_code, r.content[:20])
    # A ticket is not a session token.
    r = c.get("/api/asclepius/score", headers={"Authorization": "Bearer " + ticket})
    assert r.status_code == 401, r.status_code
    # A session token is not a ticket.
    r = c.get("/api/asclepius/assets/onboarding-demo?t=" + token_for(u))
    assert r.status_code == 401, r.status_code
    # No credential at all.
    r = c.get("/api/asclepius/assets/onboarding-demo")
    assert r.status_code == 401, r.status_code
    print("PASS")
