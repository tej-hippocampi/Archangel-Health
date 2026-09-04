"""Sandbox PRD §6.3 — THE LEAK TEST. The one that matters.

Seed the sandbox. Have all ten doctors draw and submit a case. Onboard a fake
organization through the real HS signup (its OTP read back from the sandbox
outbox). Build an export. Post in the community. Then, as the LIVE admin, hit
EVERY admin-facing GET endpoint in the route table — enumerated from
``app.routes`` so a new admin endpoint is automatically included — with every
path parameter filled from a sandbox id, and assert that not one identifier
that exists only in the sandbox appears in any response.

Sandbox ids are collected by dumping every id-like column of every table in
the sandbox asclepius and community databases and subtracting anything that
also exists in the live databases (shared vocabulary: channel slugs, the
fixture partner id, specialties). What is left exists ONLY in the sandbox,
and a live response containing any of it is a leak.

Two positive controls keep the test honest: the SANDBOX admin does see those
ids through the same endpoints, and the live admin does see a live row.

Run with ``-s`` to print the report the PR carries.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import uuid
from typing import Any, Dict, List, Set

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

import realm  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402
from asclepius import sandbox_seed  # noqa: E402

client = TestClient(A.app)

ADMIN_PW = "sandbox-admin-secret-leak"
DOCTOR_PW = "sandbox-doctor-secret-leak"

_IDEAL = {"text": "Stabilize the myocardium with IV calcium, shift potassium intracellularly "
                  "with insulin and dextrose plus a beta-agonist, then remove it with dialysis."}

#: Prefixes of the admin-facing surface (§6.3 names them; the route table
#: supplies the rest). Everything else under /api/asclepius and /api/community
#: is included too — a leak through a non-admin route is still a leak.
_SURFACE_PREFIXES = ("/api/asclepius", "/api/community", "/community")

#: Id-like columns. Values shorter than this are too generic to be evidence.
_MIN_ID_LEN = 6
_ID_COLS = re.compile(r"(^id$|_id$|^email$|^slug$|^username$|^referral_code$|^export_id$|^hs_id$|^token_hash$)")


def _task_body(**kw):
    base = {
        "specialty": "nephrology", "difficulty": "hard", "capture_reasoning": False,
        "source": "lab_supplied", "max_labels": 1, "grounding_mode": "optional",
        "prompt": "72yo on HD, K+ 6.4 with peaked T-waves. Adjust dialysate and meds?",
        "candidate_answers": [
            {"id": "A", "text": "Give calcium gluconate, then dialyze with K+ 2.0.", "generator_model": "model_x"},
            {"id": "B", "text": "Set dialysate K+ to 1.0 immediately.", "generator_model": "model_y"},
        ],
    }
    base.update(kw)
    return base


def _login(email: str, password: str, *, sandbox: bool) -> str:
    h = {realm.HEADER: "sandbox"} if sandbox else {}
    r = client.post("/api/asclepius/auth/login", headers=h, json={"email": email, "password": password})
    assert r.status_code == 200, (email, r.text)
    return r.json()["token"]


def _dump_ids(conn) -> Set[str]:
    out: Set[str] = set()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        keep = [c for c in cols if _ID_COLS.search(c)]
        if not keep:
            continue
        for row in conn.execute(f"SELECT {', '.join(keep)} FROM {t}").fetchall():
            for v in row:
                if isinstance(v, str) and len(v) >= _MIN_ID_LEN:
                    out.add(v)
    return out


@pytest.fixture
def realms(monkeypatch):
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, ADMIN_PW)
    monkeypatch.setenv(realm.DOCTOR_PASSWORD_VAR, DOCTOR_PW)
    from community.store import reset_community_store_for_tests
    live = A.fresh_store()
    live_c = reset_community_store_for_tests(str(pathlib.Path(A.TMP_DIR) / f"community_live_{A.uniq()}.db"))
    live_c.ensure_default_channels([])
    live_admin = A.make_user(live, role="admin", email="live-admin@asclepius.example.com")
    with realm.scoped("sandbox"):
        sb = A.fresh_store()
        sb_c = reset_community_store_for_tests(str(pathlib.Path(A.TMP_DIR) / f"community_sb_{A.uniq()}.db"))
        sb_c.ensure_default_channels([])
        sandbox_seed.ensure_sandbox_admin()
    return {"live": live, "live_c": live_c, "sb": sb, "sb_c": sb_c, "live_admin": live_admin}


def _exercise_sandbox(realms, monkeypatch) -> Dict[str, Any]:
    """Everything §6.3 lists, done in the sandbox through the real API."""
    from asclepius import pipeline as asc_pipeline
    # Offline: the critic/grounding side of submissions is not what is under test.
    monkeypatch.setattr(asc_pipeline, "_should_sample", lambda: False, raising=False)

    sb_admin_tok = _login(sandbox_seed.ADMIN_EMAIL, ADMIN_PW, sandbox=True)
    sb_admin_h = {"Authorization": "Bearer " + sb_admin_tok}
    r = client.post("/api/asclepius/sandbox/seed", headers=sb_admin_h)
    assert r.status_code == 200, r.text

    # Ten tasks, one per doctor, in each doctor's specialty; every doctor draws and submits.
    made = {"tasks": [], "submissions": []}
    for spec in sandbox_seed.PHYSICIANS:
        r = client.post("/api/asclepius/tasks", headers=sb_admin_h,
                        json={"tasks": [_task_body(specialty=spec["specialty"])]})
        assert r.status_code == 200, r.text
        made["tasks"].append(r.json()["created"][0])
    for spec in sandbox_seed.PHYSICIANS:
        tok = _login(spec["email"], DOCTOR_PW, sandbox=True)
        h = {"Authorization": "Bearer " + tok}
        nxt = client.get("/api/asclepius/tasks/next", headers=h,
                         params={"specialty": spec["specialty"], "portal_version": "v1"})
        assert nxt.status_code == 200, (spec["email"], nxt.text)
        task = (nxt.json() or {}).get("task")
        if not task:
            continue   # reviewers may have no labeling queue; the labelers cover the assertion
        sid = "s-" + uuid.uuid4().hex[:12]
        body = {
            "submission_id": sid, "task_id": task["task_id"], "verdict": "A_better",
            "chosen_id": "A", "rejected_id": "B", "confidence": "high", "time_spent_sec": 140,
            "prompt_review": {"reviewed": True, "verdict": "valid"},
            "independent_answer": _IDEAL,
            "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
            "rejected_critique": {"error_tags": ["dosing_error"]},
        }
        r = client.post("/api/asclepius/submissions", headers=h, json=body)
        assert r.status_code in (200, 202), (spec["email"], r.text)
        made["submissions"].append(sid)
    assert len(made["submissions"]) >= 5, made

    # Onboard a fake org through the real HS signup; the OTP comes from the outbox.
    org_email = "ops@sandbox-general.example"
    r = client.post("/api/asclepius/hs/signup", headers={realm.HEADER: "sandbox"},
                    json={"full_name": "Sam Sandbox", "email": org_email, "organization": "Sandbox General Hospital"})
    assert r.status_code == 200, r.text
    with realm.scoped("sandbox"):
        msgs = [m for m in realms["sb"].outbox_list() if m["to_email"] == org_email]
    assert msgs and msgs[0]["codes"], msgs
    r = client.post("/api/asclepius/hs/signup/verify", headers={realm.HEADER: "sandbox"},
                    json={"email": org_email, "code": msgs[0]["codes"][0]})
    assert r.status_code == 200, r.text

    # Build an export.
    r = client.post("/api/asclepius/exports", headers=sb_admin_h, json={"profile": "default"})
    assert r.status_code in (200, 201, 409, 422), r.text   # 409/422 = nothing exportable yet is fine
    export_id = (r.json() or {}).get("export_id") if r.status_code in (200, 201) else None

    # Post in the community (a u-system post into the sandbox community DB).
    with realm.scoped("sandbox"):
        chans = realms["sb_c"].list_channels()
        assert chans
        msg = realms["sb_c"].insert_message(channel_id=chans[0]["id"], author_user_id="u-system",
                                            body="Sandbox-only community post " + uuid.uuid4().hex, kind="system")
    return {**made, "export_id": export_id, "org_email": org_email, "message": msg,
            "sb_admin_h": sb_admin_h, "channel_slug": chans[0]["slug"]}


def _sandbox_only_ids(realms) -> Set[str]:
    with realm.scoped("sandbox"):
        with realms["sb"]._conn() as c:
            sb_ids = _dump_ids(c)
        with realms["sb_c"]._conn() as c:
            sb_ids |= _dump_ids(c)
    with realms["live"]._conn() as c:
        live_ids = _dump_ids(c)
    with realms["live_c"]._conn() as c:
        live_ids |= _dump_ids(c)
    return sb_ids - live_ids


def _param_fill(name: str, ids: Dict[str, str]) -> str:
    n = name.lower()
    for key, val in ids.items():
        if key in n:
            return val
    return ids["user_id"]


def _surface_routes() -> List[APIRoute]:
    out = []
    for r in A.app.routes:
        if not isinstance(r, APIRoute) or "GET" not in (r.methods or ()):
            continue
        if not r.path.startswith(_SURFACE_PREFIXES):
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.path)


def test_the_leak_test(realms, monkeypatch, capsys):
    made = _exercise_sandbox(realms, monkeypatch)
    sb_only = _sandbox_only_ids(realms)
    # Sanity: what we made is in the set.
    for tid in made["tasks"]:
        assert tid in sb_only
    for sid in made["submissions"]:
        assert sid in sb_only
    assert made["org_email"] in sb_only
    for spec in sandbox_seed.PHYSICIANS:
        assert spec["email"] in sb_only
    assert len(sb_only) > 40, len(sb_only)

    # Path-parameter fill: every {param} gets a sandbox id of the matching kind.
    with realm.scoped("sandbox"):
        sb = realms["sb"]
        sb_user = sb.get_user_by_email(sandbox_seed.PHYSICIANS[0]["email"])
        hs_rows = [r for r in sb.list_health_systems()]
        uploads = []
        for hs in hs_rows:
            uploads += sb.list_uploads_for_health_system(hs["hs_id"])
    fills = {
        "task_id": made["tasks"][0], "submission_id": made["submissions"][0],
        "user_id": sb_user["id"], "hashed": sb_user.get("id_hashed") or sb_user["id"],
        "hs_id": (hs_rows[0]["hs_id"] if hs_rows else "hs-sandbox-none"),
        "export_id": made["export_id"] or "exp-sandbox-none",
        "upload_id": (uploads[0]["upload_id"] if uploads else "upl-sandbox-none"),
        "slug": made["channel_slug"], "message_id": str(made["message"]["id"]),
        "email": sandbox_seed.PHYSICIANS[0]["email"], "code": sb_user.get("referral_code") or "SBCODE",
    }

    live_h = A.headers_for(realms["live_admin"])
    live_task = client.post("/api/asclepius/tasks", headers=live_h, json={"tasks": [_task_body()]})
    assert live_task.status_code == 200, live_task.text
    live_task_id = live_task.json()["created"][0]

    errors: List[str] = []
    checked = 0
    responses: List[tuple] = []
    for route in _surface_routes():
        path = route.path
        sent: Set[str] = set()
        for p in route.param_convertors:
            val = _param_fill(p, fills)
            sent.add(val)
            path = path.replace("{" + p + "}", val)
        try:
            resp = client.get(path, headers=live_h)
        except Exception as exc:  # a route that raises on a foreign id is a bug, not a leak
            errors.append(f"{route.path} raised {type(exc).__name__}: {exc}")
            continue
        checked += 1
        if resp.status_code >= 500:
            errors.append(f"{route.path} -> {resp.status_code}")
        responses.append((route.path, resp.status_code, resp.text or "", sent))

    # The sweep primed the fifteen-minute storage-reconcile cache (a process-
    # wide dict) with a report over the suite's shared live asset tree. Drop
    # it so a later durability test measures its own fixture, not this one.
    from routers import asclepius_admin as _adm
    _adm._RECONCILE_CACHE.clear()

    # A hit is a LEAK when the id (a) exists only in the sandbox — re-checked
    # AFTER the sweep, because some live GETs create deterministic fixture rows
    # (gold cases) whose ids the sandbox also minted, and a row that now exists
    # in live is by definition not a sandbox row — and (b) was not the id this
    # request itself sent in the path: a 404 that echoes "no such task t-…" is
    # the live store saying it has never heard of it, which is the point.
    sb_only_after = _sandbox_only_ids(realms)
    leaks: List[str] = []
    for path, status, text, sent in responses:
        hits = sorted(v for v in sb_only_after if v in text and v not in sent)
        if hits:
            leaks.append(f"{path} ({status}): {hits[:5]}")
    # And the direct lookups: a sandbox row fetched by id from live is never a 200 with the row.
    by_id = {
        "/api/asclepius/tasks/" + made["tasks"][0]: "task",
        "/api/asclepius/submissions/" + made["submissions"][0] + "/status": "submission",
        "/api/asclepius/admin/health-systems/" + fills["hs_id"]: "health system",
    }
    for path, what in by_id.items():
        resp = client.get(path, headers=live_h)
        assert resp.status_code in (403, 404, 422), (what, path, resp.status_code, resp.text[:200])

    # Positive controls: the same surface DOES show sandbox ids to the sandbox
    # admin, and live ids to the live admin.
    sb_list = client.get("/api/asclepius/admin/health-systems", headers=made["sb_admin_h"]).text
    assert any(v in sb_list for v in sb_only), "sandbox admin should see sandbox rows"
    live_tasks = client.get("/api/asclepius/admin/tasks", headers=live_h)
    assert live_task_id in (live_tasks.text if live_tasks.status_code == 200 else
                            client.get("/api/asclepius/tasks/" + live_task_id, headers=live_h).text)

    report = "\n".join([
        "═══ Sandbox leak test (PRD §6.3) ═══",
        f"sandbox-only identifiers: {len(sb_only)}",
        f"sandbox activity: {len(made['tasks'])} tasks, {len(made['submissions'])} submissions, "
        f"1 org signup ({made['org_email']}), export={'built' if made['export_id'] else 'none exportable'}, 1 community post",
        f"live admin GET endpoints exercised (from app.routes): {checked}",
        f"server errors: {len(errors)}" + ("".join("\n  ! " + e for e in errors) if errors else ""),
        f"direct by-id lookups of sandbox rows from live: {len(by_id)} — all refused",
        f"leaks: {len(leaks)}" + ("".join("\n  ✗ " + l for l in leaks) if leaks else " — ZERO sandbox rows visible to the live admin"),
    ])
    print("\n" + report)
    pathlib.Path(A.TMP_DIR, "sandbox_leak_report.txt").write_text(report, encoding="utf-8")
    assert not leaks, "\n".join(leaks)
    assert not errors, "\n".join(errors)
