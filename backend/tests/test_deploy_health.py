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
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402  (sets the temp-path env before main)
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(A.app)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def initialized_health_databases(monkeypatch, tmp_path):
    """Healthy checks start with real schemas, independent of prior suite runs."""
    import realm
    import team_store
    from community import store as community_store

    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "team.db"))
    monkeypatch.setenv("COMMUNITY_DB_PATH", str(tmp_path / "community.db"))
    # Restore both caches after the test so temporary stores cannot leak into
    # later modules. Use the same realm-scoped accessors as the application.
    monkeypatch.setattr(team_store, "_STORES", {})
    monkeypatch.setattr(community_store, "_stores", {})
    with realm.scoped("live"):
        team_store.get_team_store()
        community_store.get_community_store()


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


@pytest.mark.parametrize("variable,label", [("TEAM_DB_PATH", "team"),
                                            ("COMMUNITY_DB_PATH", "community")])
def test_healthz_missing_database_in_existing_directory_fails_without_creating(
        monkeypatch, tmp_path, variable, label):
    missing = tmp_path / "missing.db"
    monkeypatch.setenv(variable, str(missing))
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["checks"][f"db:{label}"] == "UNAVAILABLE"
    assert not missing.exists()


@pytest.mark.parametrize("contents", [b"", b"not a SQLite database"])
def test_healthz_rejects_empty_or_corrupt_database(monkeypatch, tmp_path, contents):
    database = tmp_path / "invalid-community.db"
    database.write_bytes(contents)
    monkeypatch.setenv("COMMUNITY_DB_PATH", str(database))
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["checks"]["db:community"] == "UNAVAILABLE"
    assert database.read_bytes() == contents


def test_healthz_reads_committed_wal_with_uri_characters_in_path(monkeypatch, tmp_path):
    database = tmp_path / "community?#.db"
    with sqlite3.connect(database) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE health_fixture (id INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO health_fixture VALUES (1)")
        writer.commit()
        # Keep the writer open so committed schema/data can remain in the WAL.
        assert Path(str(database) + "-wal").exists()
        monkeypatch.setenv("COMMUNITY_DB_PATH", str(database))
        r = client.get("/healthz")
        assert r.status_code == 200, r.text
        assert r.json()["checks"]["db:community"] == "ok"
        assert writer.execute("SELECT id FROM health_fixture").fetchall() == [(1,)]


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
