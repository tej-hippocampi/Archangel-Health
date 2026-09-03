"""The deploy healthcheck: can the platform tell a dead backend from a live one?

railway.json used to point its healthcheck at ``/docs``. FastAPI serves ``/docs``
from its own OpenAPI machinery, so it answers 200 for a process that has lost
every dependency the product needs, and the three ``app.mount()`` calls in
main.py are each wrapped in try/except: an image built without ``frontend/``
boots a backend that serves no UI at all and still passes. These tests pin the
difference, because a healthcheck that cannot fail lets a broken deploy replace
a working one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402  (sets the temp-path env before main)
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(A.app)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_healthz_is_ok_on_a_working_process():
    r = client.get("/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["failures"] == []
    # The frontend tree and both databases were actually looked at, not assumed.
    assert body["checks"]["mount:static"] == "ok"
    assert body["checks"]["db:team"] == "ok"
    assert body["checks"]["db:community"] == "ok"


def test_healthz_fails_when_a_database_cannot_be_opened(monkeypatch, tmp_path):
    """A volume that failed to attach, or a path that is not a database, has to
    turn the check red. Pointing COMMUNITY_DB_PATH at a directory reproduces the
    open failure without needing an unmounted volume."""
    monkeypatch.setenv("COMMUNITY_DB_PATH", str(tmp_path))
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    assert body["checks"]["db:community"] == "UNAVAILABLE"
    # The failure has to name the variable the operator must set, not just say
    # "unhealthy". That is the whole difference between a five-minute fix and
    # an outage spent guessing.
    assert any("COMMUNITY_DB_PATH" in f for f in body["failures"])


def test_healthz_fails_when_a_static_mount_is_missing(monkeypatch):
    """The failure /docs cannot see: the app boots, answers, and serves no UI."""
    monkeypatch.setattr(main, "_HEALTH_MOUNTS",
                        (("static", "/nonexistent-frontend-dir", "index.html"),))
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["checks"]["mount:static"] == "MISSING"


def test_docs_cannot_detect_the_broken_mount_but_healthz_can(monkeypatch):
    """Why the healthcheck moved, asserted rather than asserted in a comment."""
    monkeypatch.setattr(main, "_HEALTH_MOUNTS",
                        (("static", "/nonexistent-frontend-dir", "index.html"),))
    assert client.get("/docs").status_code == 200
    assert client.get("/healthz").status_code == 503


def test_non_durable_storage_is_reported_but_does_not_fail_the_check():
    """Durability is a warning here on purpose. The boot gate already refuses to
    start in production, and 503ing on a deliberate STORAGE_GATE_ALLOW_EPHEMERAL
    override would take the service straight back down; locally the databases sit
    beside the code and are non-durable by design."""
    previous = getattr(main.app.state, "storage_durability", None)
    main.app.state.storage_durability = {
        "checked": True, "ok": False, "gate_overridden": True,
        "failures": [{"store": "tenant database", "variable": "TEAM_DB_PATH",
                      "why": "on ephemeral storage"}],
    }
    try:
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["degraded"] is True
        assert body["storage_gate_overridden"] is True
        assert any("TEAM_DB_PATH" in w for w in body["storage_warnings"])
    finally:
        if previous is None:
            delattr(main.app.state, "storage_durability")
        else:
            main.app.state.storage_durability = previous


def test_healthz_never_creates_the_database_it_reports_on(monkeypatch, tmp_path):
    """A check that materialises an empty database on a fresh volume would report
    "ok" for the exact deploy that just lost everything."""
    missing = tmp_path / "not-created" / "community.db"
    monkeypatch.setenv("COMMUNITY_DB_PATH", str(missing))
    client.get("/healthz")
    assert not missing.parent.exists()


def test_railway_healthcheck_points_at_healthz():
    cfg = json.loads((_REPO_ROOT / "railway.json").read_text(encoding="utf-8"))
    deploy = cfg["deploy"]
    assert deploy["healthcheckPath"] == "/healthz"
    assert deploy["healthcheckPath"] != "/docs"
    # A one-shot restart budget turns any transient boot failure into a stopped
    # service that only a human can restart.
    assert int(deploy["restartPolicyMaxRetries"]) >= 5


def test_healthz_is_registered_and_hidden_from_the_public_schema():
    routes = {getattr(r, "path", None): r for r in main.app.routes}
    assert "/healthz" in routes
    assert getattr(routes["/healthz"], "include_in_schema", True) is False
