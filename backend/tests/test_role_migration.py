"""
Pass-4 role-token migration regression tests.

The migration runs once on TeamStore startup (`_migrate_team_member_roles_v4`)
and is keyed on `_schema_migrations.team_members_roles_v4`. Legacy tokens
(`doctor` / `nurse` / `director`) get mapped to the pass-4 taxonomy
(`surgeon` / `rn_coordinator` / `surgeon` + `is_team_director=1`). Legacy
tenant JWTs and landing user records are normalized lazily by the
staff-context resolver.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth as auth_module  # noqa: E402
from staff_context import _normalize_legacy_role, get_staff_context_optional  # noqa: E402
from team_store import TeamStore  # noqa: E402
from tenant_jwt import create_tenant_staff_token  # noqa: E402


def _fresh_store(tmp_path: Path) -> TeamStore:
    db = tmp_path / f"team_{uuid.uuid4().hex[:8]}.db"
    return TeamStore(db_path=str(db))


def _seed_legacy_member(
    store: TeamStore,
    *,
    hs_id: str,
    email: str,
    name: str,
    legacy_role: str,
) -> None:
    """Bypass the ORM-style insert so we can plant pre-migration tokens."""
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO team_members (health_system_id, email, name, role, password_hash, is_team_director, created_at)
            VALUES (?, ?, ?, ?, ?, 0, datetime('now'))
            """,
            (hs_id, email, name, legacy_role, "x"),
        )
        conn.commit()


def _force_remigrate(store: TeamStore) -> None:
    """Wipe the v4 marker and re-init the schema so the migration runs again."""
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "DELETE FROM _schema_migrations WHERE name = 'team_members_roles_v4'"
        )
        conn.commit()
    store._init_schema()  # noqa: SLF001


def test_legacy_doctor_row_migrates_to_surgeon(tmp_path):
    store = _fresh_store(tmp_path)
    hs_id = "hs_legacy_doctor"
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO health_systems (id, slug, status, created_at) VALUES (?, ?, 'active', datetime('now'))",
            (hs_id, "legacy-doctor"),
        )
        conn.commit()
    _seed_legacy_member(
        store, hs_id=hs_id, email="doc@x.com", name="Dr. Doc", legacy_role="doctor"
    )
    _force_remigrate(store)
    members = store.list_team_members(hs_id)
    assert len(members) == 1
    assert members[0]["role"] == "surgeon"
    assert members[0]["is_team_director"] is False


def test_legacy_nurse_row_migrates_to_rn_coordinator(tmp_path):
    store = _fresh_store(tmp_path)
    hs_id = "hs_legacy_nurse"
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO health_systems (id, slug, status, created_at) VALUES (?, ?, 'active', datetime('now'))",
            (hs_id, "legacy-nurse"),
        )
        conn.commit()
    _seed_legacy_member(
        store, hs_id=hs_id, email="rn@x.com", name="RN One", legacy_role="nurse"
    )
    _force_remigrate(store)
    members = store.list_team_members(hs_id)
    assert members[0]["role"] == "rn_coordinator"
    assert members[0]["is_team_director"] is False


def test_legacy_director_row_migrates_to_surgeon_with_director_flag(tmp_path):
    store = _fresh_store(tmp_path)
    hs_id = "hs_legacy_director"
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO health_systems (id, slug, status, created_at) VALUES (?, ?, 'active', datetime('now'))",
            (hs_id, "legacy-director"),
        )
        conn.commit()
    _seed_legacy_member(
        store,
        hs_id=hs_id,
        email="director@x.com",
        name="Dir Surgeon",
        legacy_role="director",
    )
    _force_remigrate(store)
    members = store.list_team_members(hs_id)
    assert members[0]["role"] == "surgeon"
    assert members[0]["is_team_director"] is True


def test_migration_is_idempotent(tmp_path):
    store = _fresh_store(tmp_path)
    hs_id = "hs_idem"
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO health_systems (id, slug, status, created_at) VALUES (?, ?, 'active', datetime('now'))",
            (hs_id, "idem"),
        )
        conn.commit()
    _seed_legacy_member(
        store, hs_id=hs_id, email="dir@x.com", name="Dir", legacy_role="director"
    )
    _force_remigrate(store)
    members_first = store.list_team_members(hs_id)
    # Second run hits the schema-migrations marker fast-path; should be a no-op.
    store._init_schema()  # noqa: SLF001
    members_second = store.list_team_members(hs_id)
    assert members_first == members_second


def test_complete_onboarding_finalize_writes_surgeon_with_director_flag(tmp_path):
    store = _fresh_store(tmp_path)
    hs_id = "hs_finalize"
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO health_systems (id, slug, status, onboarding_step, created_at)
            VALUES (?, ?, 'pending_onboarding', 3, datetime('now'))
            """,
            (hs_id, "finalize"),
        )
        conn.commit()
    store.complete_onboarding_finalize(
        hs_id,
        director_email="dir@hs.com",
        director_first_name="Dir",
        director_last_name="Ector",
        director_password_hash="x",
    )
    members = store.list_team_members(hs_id)
    assert len(members) == 1
    assert members[0]["role"] == "surgeon"
    assert members[0]["is_team_director"] is True


def _resolve(token: str):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            get_staff_context_optional(authorization=f"Bearer {token}")
        )
    finally:
        loop.close()


def test_legacy_tenant_jwt_resolves_to_surgeon():
    token = create_tenant_staff_token(
        email="legacy@hs.com",
        name="Legacy Doc",
        role="doctor",
        health_system_id="hs_legacy",
        tenant_slug="legacy",
        health_system_code="ABC",
    )
    ctx = _resolve(token)
    assert ctx is not None
    assert ctx.role == "surgeon"
    assert ctx.is_team_director is False


def test_legacy_director_jwt_resolves_to_surgeon_with_director_flag():
    token = create_tenant_staff_token(
        email="dir@hs.com",
        name="Dir Surgeon",
        role="director",
        health_system_id="hs_dir",
        tenant_slug="dir",
        health_system_code="XYZ",
    )
    ctx = _resolve(token)
    assert ctx is not None
    assert ctx.role == "surgeon"
    assert ctx.is_team_director is True


def test_pass4_jwt_resolves_with_itd_claim():
    token = create_tenant_staff_token(
        email="newdir@hs.com",
        name="New Dir",
        role="surgeon",
        health_system_id="hs_new",
        tenant_slug="new",
        health_system_code="NEW",
        is_team_director=True,
    )
    ctx = _resolve(token)
    assert ctx is not None
    assert ctx.role == "surgeon"
    assert ctx.is_team_director is True


def test_normalize_legacy_role_helper():
    assert _normalize_legacy_role("doctor") == "surgeon"
    assert _normalize_legacy_role("director") == "surgeon"
    assert _normalize_legacy_role("nurse") == "rn_coordinator"
    assert _normalize_legacy_role("surgeon") == "surgeon"
    assert _normalize_legacy_role("rn_coordinator") == "rn_coordinator"
    assert _normalize_legacy_role("np_pa") == "np_pa"
    assert _normalize_legacy_role(None) == "surgeon"
    assert _normalize_legacy_role("") == "surgeon"


def test_landing_user_role_normalized_on_load(tmp_path):
    """Auth's _load_users migrates persisted legacy role tokens in-place."""
    users_path = tmp_path / "auth_users.json"
    users_path.write_text(
        '{"someone@x.com": {"email": "someone@x.com", "role": "doctor", "password_hash": "x"}}'
    )
    original = auth_module.USERS_FILE
    try:
        auth_module.USERS_FILE = users_path
        auth_module._users = {}  # noqa: SLF001
        users = auth_module._get_users()  # noqa: SLF001
        assert users["someone@x.com"]["role"] == "surgeon"
    finally:
        auth_module.USERS_FILE = original
        auth_module._users = {}  # noqa: SLF001
