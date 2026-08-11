"""Admin › Physicians › Signups — the in-flight half of the onboarding funnel.

The bug this covers: a physician only becomes an ``asclepius.db`` user on the
LAST click of the wizard (``/api/onboarding/asclepius/finish``), while
``/api/onboarding/self-serve`` emails the founder the moment they request a
link. So every doctor who stalled mid-wizard was invisible to the admin console
— the roster read "1 physician" beside an inbox full of signup notifications.

These tests walk the real wizard, stopping at each stage, and assert the console
can now see and act on all of them.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from routers import asclepius_admin as R  # noqa: E402
from team_store import TeamStore  # noqa: E402

client = TestClient(A.app)

#: Every stage of the self-serve wizard, in order. The last one FINISHES.
_STAGES = ["link", "identity", "otp", "institution", "credentials", "attestations", "finish"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A throwaway tenant store + a fresh Asclepius store, bound to the real app.

    The tenant store is swapped on ``app.state`` (not via env) because ``main``
    is imported once per suite: by the time this module runs, the process-wide
    TeamStore already exists.
    """
    store = A.fresh_store()
    team = TeamStore(db_path=str(tmp_path / f"team_{uuid.uuid4().hex[:8]}.db"))
    monkeypatch.setattr(A.app.state, "team_store", team, raising=False)
    # The wizard provisions through ``app.state.asclepius_store``, the admin
    # console reads the module singleton. Point both at the same fresh DB or a
    # finished signup lands in one store and is counted from the other.
    monkeypatch.setattr(A.app.state, "asclepius_store", store, raising=False)
    monkeypatch.setenv("EMAIL_DEV_MODE", "1")   # send_html_email -> True, no network
    monkeypatch.setenv("LANDING_URL", "https://landing.test")
    admin = A.make_user(store, role="admin")
    return {"store": store, "team": team, "headers": A.headers_for(admin)}


def _walk(email: str, stop_at: str) -> str:
    """Walk the public wizard as a physician would, stopping after ``stop_at``."""
    team = A.app.state.team_store
    r = client.post("/api/onboarding/self-serve", json={"email": email})
    assert r.status_code == 200, r.text
    token = r.json()["onboarding_url"].rsplit("/", 1)[-1]
    if stop_at == "link":
        return token

    assert client.post("/api/onboarding/step1-identity", json={
        "token": token, "first_name": "Ada", "last_name": "Lovelace",
        "email": email}).status_code == 200
    if stop_at == "identity":
        return token

    hs = team.get_health_system_by_onboarding_token(token)
    team.create_otp_challenge(hs["id"], email, "123456")  # stand in for the emailed code
    assert client.post("/api/onboarding/verify-otp",
                       json={"token": token, "code": "123456"}).status_code == 200
    if stop_at == "otp":
        return token

    assert client.post("/api/onboarding/asclepius/institution", json={
        "token": token, "org_name": "Bay Cardiology", "specialty": "cardiology",
        "phone": ""}).status_code == 200
    if stop_at == "institution":
        return token

    assert client.post("/api/onboarding/asclepius/credentials", json={
        "token": token, "credentials": {"fullLegalName": "Ada Lovelace, MD",
                                        "npi": "1234567893",
                                        "primarySpecialty": "cardiology",
                                        "yearsInActivePractice": 12}}).status_code == 200
    if stop_at == "credentials":
        return token

    assert client.post("/api/onboarding/asclepius/attestations", json={
        "token": token, "attestations": {"accurate": True, "noPhi": True}}).status_code == 200
    if stop_at == "attestations":
        return token

    assert client.post("/api/onboarding/asclepius/finish",
                       json={"token": token}).status_code == 200
    return token


def _signups(headers):
    r = client.get("/api/asclepius/admin/signups", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ─── The regression itself ───────────────────────────────────────────────────
def test_every_started_signup_is_visible(env):
    """Six doctors stalled, one finished. The console must account for all seven."""
    emails = {s: f"doc-{s}-{uuid.uuid4().hex[:6]}@hospital.org" for s in _STAGES}
    for stage, email in emails.items():
        _walk(email, stage)

    body = _signups(env["headers"])
    listed = {r["email"] for r in body["signups"]}

    # The six who stopped short are all here...
    for stage in _STAGES[:-1]:
        assert emails[stage] in listed, f"signup stalled at {stage!r} is invisible"
    # ...and the one who finished is NOT: they have an account now.
    assert emails["finish"] not in listed
    assert body["counts"]["total"] == 6
    # And the finished one is exactly the person the admin can act on today.
    assert body["awaiting_review"] == 1

    roster = client.get("/api/asclepius/admin/physicians", headers=env["headers"]).json()
    assert len(roster["physicians"]) == 1  # unchanged: the roster is still accounts only


def test_stage_reports_where_they_stopped(env):
    """The stage is the point of the screen: it says who needs which nudge."""
    expected = {
        "link": "link_sent",
        "identity": "identity",
        "otp": "email_verified",
        "institution": "institution",
        "credentials": "credentials",
        "attestations": "attestations",
    }
    emails = {s: f"doc-{s}-{uuid.uuid4().hex[:6]}@hospital.org" for s in expected}
    for stage, email in emails.items():
        _walk(email, stage)

    by_email = {r["email"]: r for r in _signups(env["headers"])["signups"]}
    for stage, want in expected.items():
        row = by_email[emails[stage]]
        assert row["stage"] == want, f"{stage}: {row['stage']} != {want}"
        assert row["stage_word"], "a stage must render as words, never a raw token"
        assert 1 <= row["stage_index"] <= row["stage_total"]

    # Everything submitted, final button never pressed — one reminder converts them.
    assert by_email[emails["attestations"]]["ready_to_finish"] is True
    assert by_email[emails["credentials"]]["ready_to_finish"] is False
    assert _signups(env["headers"])["counts"]["ready_to_finish"] == 1


def test_details_carried_for_triage(env):
    email = f"doc-{uuid.uuid4().hex[:6]}@hospital.org"
    _walk(email, "credentials")
    row = next(r for r in _signups(env["headers"])["signups"] if r["email"] == email)
    assert row["name"] == "Ada Lovelace"
    assert row["org_name"] == "Bay Cardiology"
    assert row["specialty"] == "cardiology"
    assert row["npi"] == "1234567893"
    assert row["kind"] == "director"
    assert row["days_idle"] == 0 and row["stalled"] is False
    assert row["link_expired"] is False
    # A live onboarding token must never ride out in a list payload.
    assert not any("token" in k for k in row)


def test_clinical_onboarding_is_a_different_funnel(env):
    """Archangel (CareGuide) health-system invites are not physician signups."""
    env["team"].create_health_system_invite(invite_base_url="https://landing.test")
    assert _signups(env["headers"])["counts"]["total"] == 0


def test_expired_link_is_flagged_not_hidden(env):
    email = f"doc-{uuid.uuid4().hex[:6]}@hospital.org"
    _walk(email, "identity")
    team = env["team"]
    hs = next(h for h in team.list_health_systems_admin()
              if (h.get("director_email") or "") == email)
    with team._conn() as conn:
        conn.execute("UPDATE health_systems SET onboarding_token_expires_at = ? WHERE id = ?",
                     ("2020-01-01T00:00:00", hs["id"]))
    body = _signups(env["headers"])
    row = next(r for r in body["signups"] if r["email"] == email)
    # Dead link, still listed: this is precisely the person to re-invite, and
    # dropping them would recreate the invisibility this endpoint exists to end.
    assert row["link_expired"] is True
    assert row["stalled"] is False   # expired is a sharper fact than stalled
    assert body["counts"]["expired"] == 1


def test_invited_member_progress_reads_off_their_own_row(env):
    """A clinician invited by a director never touches ``onboarding_step``."""
    team = env["team"]
    hs = team.create_health_system_invite(
        invite_base_url="https://landing.test", director_email="dir@clinic.org",
        product="asclepius")
    hs_id = hs["health_system_id"]
    team.upsert_asclepius_person(hs_id, email="member@clinic.org", full_name="Grace Hopper",
                                 clinical_role="attending", is_director=False)
    team.issue_asclepius_member_token(hs_id, "member@clinic.org")

    row = next(r for r in _signups(env["headers"])["signups"]
               if r["email"] == "member@clinic.org")
    assert row["kind"] == "invited" and row["stage"] == "link_sent"

    team.save_asclepius_credentials(hs_id, "member@clinic.org", {"npi": "1234567893"})
    row = next(r for r in _signups(env["headers"])["signups"]
               if r["email"] == "member@clinic.org")
    assert row["stage"] == "credentials", "member progress must not read the HS step counter"


def test_people_are_grouped_to_the_right_workspace(env):
    """The people lookup is one query for every workspace, so a grouping slip
    would attribute one practice's clinicians to another — and a resend would
    then post a health_system_id that does not own that address."""
    team = env["team"]
    made = {}
    for org in ("alpha", "beta"):
        hs = team.create_health_system_invite(
            invite_base_url="https://landing.test", director_email=f"dir@{org}.org",
            product="asclepius")
        made[org] = hs["health_system_id"]
        team.update_asclepius_institution(made[org], name=org.title(),
                                          specialty="cardiology", phone="")
        team.upsert_asclepius_person(made[org], email=f"member@{org}.org",
                                     full_name="Grace Hopper", clinical_role="attending",
                                     is_director=False)

    by_email = {r["email"]: r for r in _signups(env["headers"])["signups"]}
    for org in ("alpha", "beta"):
        assert by_email[f"member@{org}.org"]["health_system_id"] == made[org]
        assert by_email[f"member@{org}.org"]["org_name"] == org.title()


def test_existing_account_never_double_counts(env):
    """Re-onboarding an approved physician must not resurrect them as a signup."""
    email = f"doc-{uuid.uuid4().hex[:6]}@hospital.org"
    A.make_user(env["store"], role="evaluator", email=email)
    _walk(email, "credentials")
    assert email not in {r["email"] for r in _signups(env["headers"])["signups"]}


def test_signups_require_admin(env):
    contributor = A.make_user(env["store"], role="evaluator")
    r = client.get("/api/asclepius/admin/signups", headers=A.headers_for(contributor))
    assert r.status_code == 403
    assert client.get("/api/asclepius/admin/signups").status_code in (401, 403)


# ─── Resend: turning an invisible stall into an approvable signup ────────────
def test_resend_rotates_the_token_on_the_same_row(env):
    email = f"doc-{uuid.uuid4().hex[:6]}@hospital.org"
    _walk(email, "credentials")
    team = env["team"]
    hs = next(h for h in team.list_health_systems_admin()
              if (h.get("director_email") or "") == email)
    before = hs["onboarding_token_hash"]

    r = client.post("/api/asclepius/admin/signups/resend", headers=env["headers"],
                    json={"health_system_id": hs["id"], "email": email})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    # The link is mailed to the physician and never returned to the caller.
    assert "onboarding_url" not in r.json()

    after = team.get_health_system_by_id(hs["id"])
    assert after["onboarding_token_hash"] != before          # fresh token
    assert team.onboarding_token_valid(after)
    # Same row, so their credentials are still waiting when they come back —
    # and the funnel count does not double.
    person = team.get_asclepius_person(hs["id"], email)
    assert person["credentials"]["npi"] == "1234567893"
    assert _signups(env["headers"])["counts"]["total"] == 1


def test_resend_refuses_a_finished_signup(env):
    email = f"doc-{uuid.uuid4().hex[:6]}@hospital.org"
    _walk(email, "finish")
    hs = next(h for h in env["team"].list_health_systems_admin()
              if (h.get("director_email") or "") == email)
    r = client.post("/api/asclepius/admin/signups/resend", headers=env["headers"],
                    json={"health_system_id": hs["id"], "email": email})
    assert r.status_code == 409


def test_resend_requires_admin_and_a_real_signup(env):
    contributor = A.make_user(env["store"], role="evaluator")
    assert client.post("/api/asclepius/admin/signups/resend",
                       headers=A.headers_for(contributor),
                       json={"health_system_id": "x", "email": "a@b.org"}).status_code == 403
    assert client.post("/api/asclepius/admin/signups/resend", headers=env["headers"],
                       json={"health_system_id": "nope", "email": "a@b.org"}).status_code == 404


# ─── Stage derivation, in isolation ─────────────────────────────────────────
@pytest.mark.parametrize("kwargs,want", [
    (dict(step=0, email_verified=False, has_credentials=False, has_attestations=False), "link_sent"),
    (dict(step=1, email_verified=False, has_credentials=False, has_attestations=False), "identity"),
    (dict(step=2, email_verified=True, has_credentials=False, has_attestations=False), "email_verified"),
    (dict(step=3, email_verified=True, has_credentials=False, has_attestations=False), "institution"),
    (dict(step=3, email_verified=True, has_credentials=True, has_attestations=False), "credentials"),
    (dict(step=3, email_verified=True, has_credentials=True, has_attestations=True), "attestations"),
    # An invited clinician carries no step counter at all: progress still reads.
    (dict(step=0, email_verified=True, has_credentials=True, has_attestations=False), "credentials"),
])
def test_stage_derivation(kwargs, want):
    assert R._signup_stage(**kwargs) == want


# ─── The view, rendered ─────────────────────────────────────────────────────
# Same node + DOM-shim harness as test_health_systems.py: the shipped module is
# eval'd and driven through the real ``AdminPhysiciansSection.render`` entry,
# so an assertion here is about what the operator actually sees.
_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"

_JS_HARNESS = """
require(%(shim)s);
function h(tag, attrs) {
  var el = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    var v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v; else el.setAttribute(k, v);
  }
  for (var i = 2; i < arguments.length; i++) appendChild_(el, arguments[i]);
  return el;
}
function isText_(c) { return typeof c === 'string' || typeof c === 'number'; }
function appendChild_(el, c) {
  if (c == null || c === '' || c === false) return;
  if (Array.isArray(c)) { c.forEach(function (x) { appendChild_(el, x); }); return; }
  el.appendChild(isText_(c) ? document.createTextNode(String(c)) : c);
}
var CALLS = [], JUMPS = [];
var RESPONSES = %(responses)s;
var ctx = {
  h: h,
  clear: function (el) { while (el.firstChild) el.removeChild(el.firstChild); },
  api: function (path, opts) {
    CALLS.push({ path: path, method: (opts && opts.method) || 'GET',
                 body: (opts && opts.body) || null });
    if (!(path in RESPONSES)) return Promise.reject({ message: 'no stub for ' + path });
    return Promise.resolve(RESPONSES[path]);
  },
  toast: function () {},
  loadingCard: function (t) { return h('div', {}, t); },
  downloadBlob: function () {},
  fmtDate: function (d) { return String(d); },
  openPipeline: function () {},
  openPhysiciansSub: function (sub) { JUMPS.push(sub); },
};
eval(require('fs').readFileSync(%(module)s, 'utf8'));
function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function classesOf(el) {
  var out = el.className ? [el.className] : [];
  (el.childNodes || []).forEach(function (c) { if (c.tagName) out = out.concat(classesOf(c)); });
  return out;
}
function find(el, pred, acc) {
  acc = acc || [];
  if (pred(el)) acc.push(el);
  (el.childNodes || []).forEach(function (c) { if (c.tagName) find(c, pred, acc); });
  return acc;
}
var body = document.createElement('div');
window.AdminPhysiciansSection.render(body, ctx, %(view)s);
// Two ticks: render awaits its fetch, and the roster's notice awaits a second.
setTimeout(function () { setTimeout(function () {
  %(after)s
  console.log(JSON.stringify({ text: textOf(body), classes: classesOf(body),
                               calls: CALLS, jumps: JUMPS }));
}, 0); }, 0);
"""

_SIGNUPS_PAYLOAD = {
    "signups": [
        {"health_system_id": "hs-1", "email": "stalled@clinic.org", "name": "Ada Lovelace",
         "kind": "director", "org_name": "Bay Cardiology", "specialty": "cardiology",
         "npi": "1234567893", "stage": "attestations",
         "stage_word": "Signed attestations — never pressed finish",
         "stage_index": 6, "stage_total": 6, "ready_to_finish": True,
         "started_at": "2026-08-01T00:00:00", "last_activity": "2026-08-01T00:00:00",
         "days_idle": 9, "stalled": True,
         "link_expires_at": "2026-09-01T00:00:00", "link_expired": False},
        {"health_system_id": "hs-2", "email": "fresh@clinic.org", "name": None,
         "kind": "invited", "org_name": None, "specialty": None, "npi": None,
         "stage": "link_sent", "stage_word": "Link sent — not opened",
         "stage_index": 1, "stage_total": 6, "ready_to_finish": False,
         "started_at": "2026-08-10T00:00:00", "last_activity": "2026-08-10T00:00:00",
         "days_idle": 0, "stalled": False,
         "link_expires_at": "2020-01-01T00:00:00", "link_expired": True},
    ],
    "counts": {"total": 2, "ready_to_finish": 1, "stalled": 1, "expired": 1},
    "awaiting_review": 3, "can_resend": True, "stalled_after_days": 3,
}


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _render(view: str, responses: dict, after: str = "") -> dict:
    return _run_node(_JS_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_FRONTEND / "admin_physicians.js")),
        "responses": json.dumps(responses),
        "view": json.dumps(view),
        "after": after,
    })


def test_signups_view_renders_every_in_flight_physician():
    out = _render("signups", {"/admin/signups": _SIGNUPS_PAYLOAD})
    assert "stalled@clinic.org" in out["text"]
    assert "fresh@clinic.org" in out["text"]
    # The stage is words, never a raw token — the same vocabulary rule the tier
    # and verification columns follow.
    assert "never pressed finish" in out["text"]
    assert "attestations" not in out["text"].replace("Signed attestations", "")
    # The screen must say plainly that these are not approvals, and where the
    # approvable ones are.
    assert "cannot be approved yet" in out["text"]
    assert "3 physicians are" in out["text"]


def test_signups_empty_state_does_not_read_as_nobody_signed_up():
    out = _render("signups", {"/admin/signups": {
        "signups": [], "counts": {"total": 0, "ready_to_finish": 0, "stalled": 0, "expired": 0},
        "awaiting_review": 0, "can_resend": True, "stalled_after_days": 3}})
    assert "either finished or already been decided" in out["text"]


def test_resend_posts_the_composite_key():
    out = _render(
        "signups", {"/admin/signups": _SIGNUPS_PAYLOAD,
                    "/admin/signups/resend": {"ok": True, "message": "sent"}},
        after="find(body, function (e) { return e.tagName === 'BUTTON'; })[0].dispatch('click');")
    posts = [c for c in out["calls"] if c["path"] == "/admin/signups/resend"]
    assert len(posts) == 1
    assert posts[0]["method"] == "POST"
    # health_system_id + email: a director is keyed by the health system row and
    # an invited clinician by their address on it, so neither alone identifies a
    # signup.
    assert posts[0]["body"] == {"health_system_id": "hs-1", "email": "stalled@clinic.org"}


def test_resend_disabled_when_the_server_cannot_send_email():
    payload = dict(_SIGNUPS_PAYLOAD, can_resend=False)
    out = _render("signups", {"/admin/signups": payload},
                  after="var b = find(body, function (e) { return e.tagName === 'BUTTON'; })[0];"
                        "if (!('disabled' in b.attributes)) throw new Error('resend not disabled');")
    assert out["calls"][0]["path"] == "/admin/signups"


def test_roster_says_where_the_missing_physicians_are():
    """The regression in one assertion: an empty-looking roster must not read as
    'nobody signed up' while people are mid-wizard."""
    out = _render("roster", {
        "/admin/physicians": {"physicians": [], "counts": {"all": 0, "pending": 0}},
        "/admin/signups": _SIGNUPS_PAYLOAD,
    })
    assert "2 physicians mid-onboarding, not on this roster" in out["text"]
    assert "asc-card asc-signup-notice" in out["classes"]


def test_roster_notice_is_silent_when_the_funnel_is_empty():
    out = _render("roster", {
        "/admin/physicians": {"physicians": [], "counts": {"all": 0}},
        "/admin/signups": {"signups": [], "counts": {"total": 0}, "awaiting_review": 0},
    })
    assert "mid-onboarding" not in out["text"]


def test_roster_survives_a_failing_signups_call():
    """The funnel count is secondary content: it must never take the roster with
    it. ``/admin/signups`` has no stub here, so the promise rejects."""
    out = _render("roster", {"/admin/physicians": {
        "physicians": [{"id": "u1", "name": "Ada", "email": "a@b.org", "tier": "labeler",
                        "verification_status": "approved"}],
        "counts": {"all": 1, "pending": 0}}})
    assert "a@b.org" in out["text"]
    assert "mid-onboarding" not in out["text"]


def test_roster_notice_jumps_through_the_shell():
    out = _render(
        "roster",
        {"/admin/physicians": {"physicians": [], "counts": {"all": 0}},
         "/admin/signups": _SIGNUPS_PAYLOAD},
        after="find(body, function (e) { return e.tagName === 'BUTTON'; })[0].dispatch('click');")
    # Not a local view flip: the shell owns which sub-tab looks selected.
    assert out["jumps"] == ["signups"]
