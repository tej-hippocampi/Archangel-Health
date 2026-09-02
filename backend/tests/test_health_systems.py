"""PRD C Phase 1 — health systems: schema, provisioning, backfill, admin list.

Covers the admin side of the health-system flow: username derivation +
collision suffixing, create-or-reuse by organization name, the two-field
provision endpoint (password emailed once / stored hashed only / must_reset),
and the boot-time backfill that adopts historical partner uploads.
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
from asclepius.store import verify_password  # noqa: E402
from routers import asclepius_admin as R  # noqa: E402

client = TestClient(A.app)

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# A ctx close enough to the real one for the Verification tab: ``h`` mirrors
# asclepius.js's hyperscript helper, everything else is a spy or a no-op.
_JS_CTX = """
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
var ctx = {
  h: h,
  clear: function (el) { while (el.firstChild) el.removeChild(el.firstChild); },
  api: function () { return Promise.resolve({}); },
  toast: function () {},
  loadingCard: function (t) { return h('div', {}, t); },
  downloadBlob: function () {},
  fmtDate: function (d) { return String(d); },
  openPipeline: function () {},
};
eval(require('fs').readFileSync(%(module)s, 'utf8'));
"""


def _pending_tab_script(responses: dict, tail: str, wait_after_tail: bool = False) -> str:
    """Render the Physicians section and report what landed in the DOM.

    Admin Launch PRD §1.2 removed the sub-tab strip, so the section takes no
    view argument: it owns its own Approved / Pending tabs.
    """
    return (_JS_CTX % {"shim": json.dumps(str(_DOM_SHIM)),
                       "module": json.dumps(str(_FRONTEND / "admin_physicians.js"))}) + """
var RESPONSES = %s;
var CALLS = [];
ctx.api = function (path, opts) {
  CALLS.push({ path: path, method: (opts && opts.method) || 'GET',
               body: (opts && opts.body) || null });
  if (!(path in RESPONSES)) return Promise.reject({ message: 'no stub for ' + path });
  return Promise.resolve(RESPONSES[path]);
};
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
var QUEUE_TEXT = null;
window.AdminPhysiciansSection.reset();
window.AdminPhysiciansSection.render(body, ctx);
function later(fn, n) { setTimeout(n > 1 ? function () { later(fn, n - 1); } : fn, 0); }
function report() {
  console.log(JSON.stringify({ text: textOf(body), queueText: QUEUE_TEXT,
                               classes: classesOf(body), calls: CALLS }));
}
later(function () {
%s
  %s
}, 4);
""" % (json.dumps(responses), tail,
       "later(report, 4);" if wait_after_tail else "report();")


_PENDING_QUEUE = {
    "status": "pending", "count": 1, "total": 1, "has_more": False,
    "queue": [{"user_id": "u1", "email": "jane@clinic.org", "full_name": "Jane Doe",
               "specialty": "cardiology"}],
}
_NO_SIGNUPS = {"signups": [], "counts": {"total": 0}, "awaiting_review": 0,
               "can_resend": True, "stalled_after_days": 3}
_DOSSIER = {
    "user_id": "u1", "email": "jane@clinic.org", "full_name": "Jane Doe",
    "specialty": "cardiology", "score": 82, "proposed_tier": "reviewer",
    "tier_words": {"labeler": "Labeler", "reviewer": "Reviewer"},
    "reasons": ["+25 NPI verified against NPPES (MD)", "+20 board certified"],
    "blockers": [], "has_cv": False, "cv_ok": False,
    "npi": {"npi": "1234567893", "result": "verified", "recheck_pending": False},
    "tiering": {"proposed_tier": "reviewer", "score": 4.7},
}


# ─── B1 → Admin Launch §2/§3: the decision surface is native to this section ──
def test_pending_tab_renders_the_decision_surface_in_this_section():
    """The queue is no longer a foreign module mounted into a sub-tab.

    Regression for B1 in its current form: for a whole build round the
    Verification tab probed an invented global, rendered a placeholder, and no
    physician could be approved through the UI. The property that mattered then
    still matters now — a physician waiting for a decision must be REACHABLE and
    DECIDABLE from this section — so it is asserted against the surface that
    actually carries it.
    """
    out = _run_node(_pending_tab_script(
        {"/admin/physicians": {"physicians": [], "counts": {"all": 0}},
         "/verify/queue?status=pending": _PENDING_QUEUE,
         "/admin/signups": _NO_SIGNUPS,
         "/verify/queue/u1": _DOSSIER},
        "  QUEUE_TEXT = textOf(body);\n"
        "  find(body, function (e) { return e.tagName === 'TR'; })"
        "    .filter(function (r) { return textOf(r).indexOf('Jane Doe') !== -1; })[0]"
        "    .dispatch('click');",
        wait_after_tail=True))
    # Pending opens by default whenever anything is waiting, and the physician
    # is reachable from the queue.
    assert "Jane Doe" in out["queueText"]
    assert "ships with the identity-verification work" not in out["queueText"]
    # Opening them lands on a decision, in this section — three buttons, the
    # recommended tier primary, and the score and reasons beside them.
    assert "Approve as Reviewer" in out["text"]
    assert "Approve as Labeler" in out["text"]
    assert "Reject" in out["text"]
    assert "NPI verified against NPPES" in out["text"]
    # §8.8 / §3.1: the report comes from ONE call, not two.
    dossier = [c for c in out["calls"] if c["path"].startswith("/verify/queue/u1")]
    assert len(dossier) == 1, out["calls"]
    assert dossier[0]["path"] == "/verify/queue/u1", (
        "a case_domain was passed; the server defaults to the physician's own "
        "specialty and §3.1 says not to override it")


def test_pending_tab_failure_is_visible_not_silent():
    """With the queue unreachable the operator must SEE a failure.

    A silent placeholder is precisely what hid B1 for an entire build round, so
    the absence case is part of the contract rather than a nicety. There is no
    stub for the queue here, so its promise rejects.
    """
    out = _run_node(_pending_tab_script(
        {"/admin/physicians": {"physicians": [], "counts": {"all": 0}}}, ""))
    assert "asc-inline-error" in " ".join(out["classes"]), (
        "the failure state is not rendered as an error"
    )
    assert "could not be loaded" in out["text"]
    assert "ships with the identity-verification work" not in out["text"], (
        "the silent placeholder is back — this is the B1 regression"
    )


def test_the_frozen_verification_global_is_not_re_derived():
    """Seam 1 freezes the name ``window.AsclepiusVerification`` and PRD-B owns it.

    Admin Launch §2 replaced the mounted sub-tab with a native two-tab section,
    so admin_physicians.js no longer mounts anything — but the rule that broke
    B1 still holds: nobody may INVENT a name for somebody else's global. The
    owner still exports it under the frozen name.
    """
    section = (_FRONTEND / "admin_physicians.js").read_text(encoding="utf-8")
    assert "renderVerificationQueue" not in section, (
        "the guessed global name is back — PRD-B owns window.AsclepiusVerification"
    )
    owner = (_FRONTEND / "onboarding.js").read_text(encoding="utf-8")
    assert "window.AsclepiusVerification = " in owner, (
        "the frozen global lost its owner"
    )


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_headers(store):
    admin = A.make_user(store, role="admin")
    return A.headers_for(admin)


@pytest.fixture()
def email_ok(monkeypatch):
    """Pretend email transport is configured and capture what would be sent."""
    sent = []

    async def _fake_send(to, subject, html, **kw):
        sent.append({"to": to, "subject": subject, "html": html})
        return True

    monkeypatch.setattr(R, "is_email_transport_configured", lambda: True)
    monkeypatch.setattr(R, "send_html_email", _fake_send)
    return sent


# ─── Username derivation ─────────────────────────────────────────────────────
def test_username_derives_from_org_name():
    assert R.derive_hs_username("Mass General Hospital") == "massgeneral"
    assert R.derive_hs_username("Mercy Health") == "mercy"
    # Stopwords never strip the name to nothing — fall back to all words.
    assert R.derive_hs_username("University Health System") == "universityhealthsyst"
    assert R.derive_hs_username("") == "partner"
    assert R.derive_hs_username("!!!") == "partner"


def test_username_collision_suffixes():
    store = _store()
    hs = store.ensure_health_system("Mass General Hospital")
    store.create_hs_portal_user(username="massgeneral", hs_id=hs["hs_id"], password="x" * 12)
    assert R.unique_hs_username(store, "massgeneral") == "massgeneral2"
    store.create_hs_portal_user(username="massgeneral2", hs_id=hs["hs_id"], password="x" * 12)
    assert R.unique_hs_username(store, "massgeneral") == "massgeneral3"


# ─── Health system create-or-reuse ───────────────────────────────────────────
def test_ensure_health_system_reuses_by_name_case_insensitive():
    store = _store()
    a = store.ensure_health_system("Mass General Hospital", contact_email="a@mgh.org")
    b = store.ensure_health_system("  mass   general HOSPITAL ")
    assert a["hs_id"] == b["hs_id"]
    assert len(store.list_health_systems()) == 1
    # The id is the contract shape hs-<slug>-<6hex>.
    assert a["hs_id"].startswith("hs-")


# ─── Provision endpoint ──────────────────────────────────────────────────────
def test_provision_creates_account_and_emails_once(email_ok):
    store = _store()
    res = client.post(
        "/api/asclepius/admin/health-systems/provision",
        json={"organization": "Mass General Hospital", "email": "data@mgh.harvard.edu", "purpose": "task_creation"},
        headers=_admin_headers(store),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["username"] == "massgeneral"
    assert body["health_system"]["name"] == "Mass General Hospital"

    # Exactly one email, containing the username and a passphrase…
    assert len(email_ok) == 1
    assert email_ok[0]["to"] == "data@mgh.harvard.edu"
    assert "massgeneral" in email_ok[0]["html"]

    # …and the password exists only as a hash. must_reset forces a change.
    row = store.get_hs_portal_user("massgeneral")
    assert row is not None
    assert row["must_reset"] == 1
    assert row["password_hash"]
    # The emailed passphrase verifies against the stored hash and never appears
    # in the row itself.
    import re as _re
    # the passphrase is the second <code> cell (the first is the username)
    codes = _re.findall(r"<code>([^<]+)</code>", email_ok[0]["html"])
    passphrase = codes[1]
    assert verify_password(passphrase, row["password_hash"])
    assert passphrase not in str(row)


def test_provision_rotates_existing_account_instead_of_minting_second(email_ok):
    store = _store()
    headers = _admin_headers(store)
    r1 = client.post("/api/asclepius/admin/health-systems/provision",
                     json={"organization": "Mercy Health", "email": "it@mercy.org", "purpose": "task_creation"},
                     headers=headers)
    assert r1.status_code == 200
    first_hash = store.get_hs_portal_user(r1.json()["username"])["password_hash"]

    r2 = client.post("/api/asclepius/admin/health-systems/provision",
                     json={"organization": "Mercy Health", "email": "it@mercy.org", "purpose": "task_creation"},
                     headers=headers)
    assert r2.status_code == 200
    assert r2.json()["username"] == r1.json()["username"]

    hs_id = r1.json()["health_system"]["hs_id"]
    users = store.list_hs_portal_users(hs_id)
    assert len(users) == 1                      # rotated, not duplicated
    row = store.get_hs_portal_user(r1.json()["username"])
    assert row["password_hash"] != first_hash   # password actually rotated
    assert row["must_reset"] == 1


def test_provision_requires_admin(email_ok):
    store = _store()
    evaluator = A.make_user(store, role="evaluator")
    res = client.post("/api/asclepius/admin/health-systems/provision",
                      json={"organization": "X Health", "email": "a@b.org", "purpose": "task_creation"},
                      headers=A.headers_for(evaluator))
    assert res.status_code in (401, 403)
    res2 = client.post("/api/asclepius/admin/health-systems/provision",
                       json={"organization": "X Health", "email": "a@b.org", "purpose": "task_creation"})
    assert res2.status_code in (401, 403)
    assert email_ok == []


def test_provision_503_when_email_unconfigured(monkeypatch):
    store = _store()
    monkeypatch.setattr(R, "is_email_transport_configured", lambda: False)
    res = client.post("/api/asclepius/admin/health-systems/provision",
                      json={"organization": "X Health", "email": "a@b.org", "purpose": "task_creation"},
                      headers=_admin_headers(store))
    assert res.status_code == 503
    # Nothing half-created without a way to tell the recipient.
    assert store.list_hs_portal_users() == []


# ─── Phase 5: admin UI regressions ───────────────────────────────────────────
def test_restored_operator_metrics_are_on_the_page():
    """C-5.1: the four-question restructure claimed "the deeper diagnostics keep
    their cards below" and then deleted them. QA pass rate, average agreement,
    flaw catch rate and Multimodal-in-queue appeared nowhere — and the tell was
    that sc/qpr/flaw/sumValues had become dead code."""
    src = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
    body = src[src.index("async function renderMetricQuestions"):
               src.index("async function renderAdminMetrics")]
    for label in ("QA pass rate", "Flaw catch rate", "Avg agreement", "Multimodal in queue"):
        assert label in body, f"{label!r} was deleted rather than relocated"
    # And they are wired to real /stats fields, not hardcoded.
    for field in ("qa_pass_rate", "flaw_catch_rate", "open_modality_counts", "average_agreement"):
        assert field in body, f"{field} is not read"


def test_bucket_actions_deep_link_to_their_row():
    """C-5.2: the bucket buttons always passed their upload to openPipeline,
    which ignored the argument and just switched tabs — so [Review] and
    [Promote to task] dropped the operator into an unfiltered page."""
    src = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
    assert "openPipeline: (entry)" in src, "openPipeline still takes no argument"
    assert "pipelineFocus" in src
    health = (_FRONTEND / "admin_health.js").read_text(encoding="utf-8")
    assert "openPipeline(it)" in health


def test_specialty_dropdown_is_not_truncated():
    """C-5.3: list_tasks defaults to limit=500, so a specialty outside the 500
    most recent tasks never appeared in the export filter."""
    src = (Path(__file__).resolve().parents[1] / "routers" / "asclepius_admin.py").read_text()
    block = src[src.index("async def export_case_options"):
                src.index("async def export_case_preview")]
    assert "limit=" in block, "list_tasks is still called with its default limit"


def test_ensure_health_system_returns_the_committed_row():
    """C-5.5: get_health_system opened a SECOND connection from inside the
    still-uncommitted write, so the returned dict carried contact_email None
    while the committed row held the real address."""
    store = _store()
    store.ensure_health_system("Stale General")            # created without an email
    got = store.ensure_health_system("Stale General", contact_email="data@stale.example.org")
    assert got["contact_email"] == "data@stale.example.org", "stale row returned"
    assert store.get_health_system(got["hs_id"])["contact_email"] == "data@stale.example.org"


def test_backfill_does_not_name_a_health_system_after_an_internal_id():
    """C-5.6: a live run produced {'name': 'u_abc123'}, which renders in the
    Health Systems table as though a hospital were called that. The same path
    named one after the provider's email address."""
    store = _store()
    with store._conn() as conn:
        conn.execute("INSERT INTO ingest_uploads (upload_id, link_id, partner_id, status, "
                     "created_at, updated_at) VALUES ('upl-int', 'L', 'u_abc123', 'ingested', "
                     "'2025-01-01', '2025-01-01')")
        conn.execute("INSERT INTO data_providers (provider_id, email, org_name, created_at, "
                     "updated_at) VALUES ('u-mail', 'it@mercy.org', '', '2025-01-01', '2025-01-01')")
    store._migrate()
    names = {h["name"] for h in store.list_health_systems()}
    assert "u_abc123" not in names, "a health system is named after an internal user id"
    assert "it@mercy.org" not in names, "a health system is named after an email address"
    assert any(n.startswith("Unnamed partner") for n in names), names
    # Still adopted — the upload is not orphaned, it is just honestly labeled.
    assert store.get_ingest_upload("upl-int")["health_system_id"]


def test_upgrade_from_a_previously_deployed_database_does_not_duplicate():
    """C-5.6 upgrade path: a database written by the PREVIOUS release already
    holds partners under their raw internal id. Renaming the lookup key without
    migrating those rows inserts a second health system beside the first and
    splits that partner's upload history between them — an operator sees the
    same hospital twice. Only reproduces on a DB that has run before, which is
    why a fresh-DB test cannot catch it."""
    store = _store()
    with store._conn() as conn:
        conn.execute("INSERT INTO health_systems (hs_id, name, active, created_at) "
                     "VALUES ('hs-u-legacy-aaaaaa', 'u_legacy1', 1, '2025-01-01')")
        conn.execute("INSERT INTO ingest_uploads (upload_id, link_id, partner_id, status, "
                     "health_system_id, created_at, updated_at) VALUES ('upl-before', 'L', "
                     "'u_legacy1', 'ingested', 'hs-u-legacy-aaaaaa', '2025-01-01', '2025-01-01')")
        conn.execute("INSERT INTO ingest_uploads (upload_id, link_id, partner_id, status, "
                     "created_at, updated_at) VALUES ('upl-after', 'L', 'u_legacy1', "
                     "'ingested', '2025-01-02', '2025-01-02')")
    store._migrate()
    store._migrate()   # a redeploy must not undo or re-do it

    rows = [h for h in store.list_health_systems() if "legacy1" in h["name"]]
    assert len(rows) == 1, f"the upgrade duplicated the health system: {rows}"
    assert rows[0]["hs_id"] == "hs-u-legacy-aaaaaa", "renamed row lost its id"
    assert rows[0]["name"] == "Unnamed partner (u_legacy1)"
    # Both uploads still point at the SAME system — history was not split.
    assert (store.get_ingest_upload("upl-before")["health_system_id"]
            == store.get_ingest_upload("upl-after")["health_system_id"]
            == "hs-u-legacy-aaaaaa")


# ─── Backfill ────────────────────────────────────────────────────────────────
def test_backfill_adopts_historical_partner_uploads():
    store = _store()
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO data_providers (provider_id, email, org_name, created_at, updated_at) "
            "VALUES ('u-legacy', 'it@stluke.org', 'St Luke Medical Center', '2025-01-01', '2025-01-01')"
        )
        conn.execute(
            "INSERT INTO ingest_uploads (upload_id, link_id, partner_id, status, created_at, updated_at) "
            "VALUES ('upl-legacy', 'account', 'u-legacy', 'ingested', '2025-01-01', '2025-01-01')"
        )
    store._migrate()

    up = store.get_ingest_upload("upl-legacy")
    assert up["health_system_id"], "historical upload was not adopted"
    names = {h["name"] for h in store.list_health_systems()}
    assert "St Luke Medical Center" in names

    # Idempotent: a second boot neither duplicates nor re-stamps.
    before = up["health_system_id"]
    store._migrate()
    assert store.get_ingest_upload("upl-legacy")["health_system_id"] == before
    assert len([h for h in store.list_health_systems()
                if h["name"] == "St Luke Medical Center"]) == 1


# ─── Detail: pipeline buckets (Phase 2) ──────────────────────────────────────
def _mk_upload(store, hs_id: str, filename: str) -> str:
    up = store.insert_ingest_upload(
        link_id="hs-portal", partner_id=hs_id, filename=filename,
        sha256="0" * 64, size_bytes=1234, raw_path=None, source_ip=None)
    store.set_upload_health_system(up["upload_id"], hs_id)
    return up["upload_id"]


def test_detail_buckets_follow_workflow_order():
    store = _store()
    headers = _admin_headers(store)
    hs = store.ensure_health_system("Bucket Health")
    hs_id = hs["hs_id"]

    up_clean = _mk_upload(store, hs_id, "clean.zip")
    store.insert_ingest_case(upload_id=up_clean, patient_key="p1", specialty="nephrology",
                             case={"x": 1}, status="ingested", report=None)

    up_held = _mk_upload(store, hs_id, "held.zip")
    ic = store.insert_ingest_case(upload_id=up_held, patient_key="p2", specialty="nephrology",
                                  case={"x": 2}, status="ingested", report=None)
    store.hold_ingest_case_for_review(ic["ingest_case_id"], "phi_unverified",
                                      "unverified burned-in PHI", severity="blocking")

    up_live = _mk_upload(store, hs_id, "live.zip")
    store.insert_ingest_case(upload_id=up_live, patient_key="p3", specialty="nephrology",
                             case={"x": 3}, status="promoted", report=None)

    up_fresh = _mk_upload(store, hs_id, "fresh.zip")   # no cases yet — not examined

    res = client.get(f"/api/asclepius/admin/health-systems/{hs_id}", headers=headers)
    assert res.status_code == 200, res.text
    b = res.json()["buckets"]
    ids = {k: {e["upload_id"] for e in v} for k, v in b.items()}

    # These uploads carry no destination, so their ingested cases are HELD IN
    # STORAGE rather than ready to promote — that is the one bucket storage
    # replaces, because it is the one whose action is unavailable until somebody
    # says what the data is for.
    assert up_clean in ids["storage"]
    assert up_clean not in ids["ready_to_promote"]
    assert up_held in ids["needs_attention"]
    # Already promoted is HISTORY. An upload whose cases went out under the old
    # rules must not be filed as "awaiting your decision" — that asks an operator
    # to decide something that has already happened.
    assert up_live in ids["in_production"]
    assert up_live not in ids["storage"]
    assert up_fresh in ids["needs_review"]
    # A safety hold is never buried in a normal bucket's entry list silently —
    # the reason travels with it.
    held_entry = next(e for e in b["needs_attention"] if e["upload_id"] == up_held)
    assert any("PHI" in r for r in held_entry["reasons"])
    # The fresh upload appears ONLY in needs_review. Nothing has ingested yet, so
    # there is nothing to decide about and nothing to promote.
    assert up_fresh not in (ids["needs_attention"] | ids["ready_to_promote"]
                            | ids["in_production"] | ids["storage"])

    # And once a destination IS set, the same upload moves to the promote queue.
    from asclepius import ingestion as asc_ingestion

    store.set_upload_purpose(up_clean, asc_ingestion.PURPOSE_TASK_CREATION)
    again = client.get(f"/api/asclepius/admin/health-systems/{hs_id}", headers=headers).json()
    moved = {k: {e["upload_id"] for e in v} for k, v in again["buckets"].items()}
    assert up_clean in moved["ready_to_promote"]
    assert up_clean not in moved["storage"]


def test_detail_404_for_unknown_health_system():
    store = _store()
    res = client.get("/api/asclepius/admin/health-systems/hs-nope-000000",
                     headers=_admin_headers(store))
    assert res.status_code == 404


# ─── Physicians (Phase 3) ────────────────────────────────────────────────────
def test_physicians_roster_renders_before_prd_b_merges():
    """PRD-B owns the tier/verification columns. Before B merges they simply do
    not exist — the roster must render every physician as Unassigned / not
    checked instead of crashing."""
    store = _store()
    headers = _admin_headers(store)
    # tier=None explicitly: this test is ABOUT the unassigned state, and a
    # fixture that gets it by default would stop asserting it the moment the
    # default changes (which is exactly what happened when LABEL became enforced
    # and make_user started minting realistic, tiered contributors).
    A.make_user(store, role="evaluator", specialty="nephrology", tier=None)
    A.make_user(store, role="evaluator", tier=None)
    A.make_user(store, role="qa_reviewer", tier=None)   # not a physician
    store.ensure_mock_user(email="mock@asclepius.example.com", password="pw-12345678")

    res = client.get("/api/asclepius/admin/physicians", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["counts"]["all"] == 2               # admin, qa, mock excluded
    assert body["counts"]["unassigned"] == 2
    assert body["counts"]["pending"] == 0
    for p in body["physicians"]:
        assert p["tier"] is None                    # renders as "Unassigned"
        assert p["verification_status"] is None     # renders as "Not checked"
        assert p["slack_joined"] is None            # tri-state survives as null


def _add_prd_b_columns(store):
    with store._conn() as conn:
        for ddl in ("ALTER TABLE users ADD COLUMN tier TEXT",
                    "ALTER TABLE users ADD COLUMN verification_status TEXT",
                    "ALTER TABLE users ADD COLUMN phone TEXT",
                    "ALTER TABLE users ADD COLUMN slack_joined INTEGER",
                    "ALTER TABLE users ADD COLUMN health_system_id TEXT"):
            try:
                conn.execute(ddl)
            except Exception:
                pass  # PRD-B merged and created it already — even better


def test_physicians_roster_counts_with_prd_b_columns():
    store = _store()
    headers = _admin_headers(store)
    _add_prd_b_columns(store)
    hs = store.ensure_health_system("Roster Health")
    # u1/u2 get their tier from the UPDATE below; u3 stays unassigned, which is
    # the distinction this test exists to prove ("pending but unassigned — two
    # distinct facts"). Stated, not defaulted.
    u1 = A.make_user(store, role="evaluator", tier=None)
    u2 = A.make_user(store, role="evaluator", tier=None)
    u3 = A.make_user(store, role="evaluator", tier=None)
    with store._conn() as conn:
        conn.execute("UPDATE users SET tier='labeler', verification_status='approved', "
                     "slack_joined=1, health_system_id=? WHERE id=?", (hs["hs_id"], u1["id"]))
        conn.execute("UPDATE users SET tier='reviewer', verification_status='pending' "
                     "WHERE id=?", (u2["id"],))
        conn.execute("UPDATE users SET verification_status='pending' WHERE id=?", (u3["id"],))

    body = client.get("/api/asclepius/admin/physicians", headers=headers).json()
    c = body["counts"]
    assert (c["all"], c["pending"], c["labelers"], c["reviewers"], c["unassigned"]) == (3, 2, 1, 1, 1)
    by_id = {p["id"]: p for p in body["physicians"]}
    assert by_id[u1["id"]]["health_system_name"] == "Roster Health"
    assert by_id[u1["id"]]["slack_joined"] is True
    assert by_id[u2["id"]]["health_system_name"] is None
    assert by_id[u3["id"]]["tier"] is None          # pending but unassigned — distinct facts


def test_physician_profile_histories_are_defensive():
    store = _store()
    headers = _admin_headers(store)
    doc = A.make_user(store, role="evaluator", specialty="nephrology")

    res = client.get(f"/api/asclepius/admin/physicians/{doc['id']}", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["physician"]["name"]
    assert body["review_history"] == []             # case_reviews absent pre-PRD-A → [], not 500
    assert body["task_history"] == []
    assert body["npi_payload"] is None

    # Once PRD-A's table exists, the same call returns the reviewer's rows.
    with store._conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS case_reviews (
            review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            submission_id TEXT NOT NULL, reviewer_user_id TEXT NOT NULL,
            reviewer_id_hashed TEXT NOT NULL, verdict TEXT NOT NULL,
            dimension_json TEXT, corrections_json TEXT, reviewer_notes TEXT,
            time_spent_sec INTEGER, blinded INTEGER, created_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO case_reviews (review_id, task_id, submission_id, "
                     "reviewer_user_id, reviewer_id_hashed, verdict, created_at) "
                     "VALUES ('rev-1', 't-1', 's-1', ?, 'h1', 'accept', '2026-01-01')",
                     (doc["id"],))
    body2 = client.get(f"/api/asclepius/admin/physicians/{doc['id']}", headers=headers).json()
    assert len(body2["review_history"]) == 1
    assert body2["review_history"][0]["verdict"] == "accept"


def test_physician_profile_404_for_non_physician():
    store = _store()
    headers = _admin_headers(store)
    admin = A.make_user(store, role="admin")
    assert client.get(f"/api/asclepius/admin/physicians/{admin['id']}",
                      headers=headers).status_code == 404
    assert client.get("/api/asclepius/admin/physicians/u-nope",
                      headers=headers).status_code == 404


# ─── Export by case (Phase 4) ────────────────────────────────────────────────
def _mk_export_record(store, *, task_id: str, submission_id: str,
                      specialty: str = "nephrology", portal_version: str = "v3"):
    payload = {
        "type": "preference",
        "prompt": f"Case {task_id} — how do you manage?",
        "chosen": "Give IV calcium, then insulin-dextrose, then dialyze.",
        "rejected": "Set dialysate K+ to 1.0 immediately.",
        "context": {"specialty": specialty, "difficulty": "hard"},
        "rationale": "safer sequencing",
        "confidence": "high",
        "annotator_credential": "board_certified_nephrology",
        "annotator_specialty": specialty,
        "annotator_id_hashed": "realhash" + submission_id[-4:],
        "submission_id": submission_id,
        "task_id": task_id,
        "source": "lab_supplied",
        "taxonomy_version": "t1",
        "config_version": "c1",
        "license": "CC-BY-NC-4.0-clinical-eval",
        "ip_cleared": True,
        "contains_phi": False,
        "portal_version": portal_version,
    }
    store.insert_record(submission_id=submission_id, task_id=task_id, rtype="preference",
                        specialty=specialty, payload=payload, status="export_ready")


def test_export_case_preview_filters_combine():
    store = _store()
    headers = _admin_headers(store)
    _mk_export_record(store, task_id="t-case-01", submission_id="s-0001",
                      specialty="nephrology", portal_version="v3")
    _mk_export_record(store, task_id="t-case-02", submission_id="s-0002",
                      specialty="cardiology", portal_version="v4")

    base = "/api/asclepius/admin/export/case-preview"
    p = client.get(base, headers=headers).json()
    assert (p["cases"], p["labeler_submissions"], p["specialty_count"]) == (2, 2, 2)
    assert p["estimated_bytes"] > 0
    assert p["exportable"] is True

    p2 = client.get(base + "?specialty=cardiology", headers=headers).json()
    assert (p2["cases"], p2["labeler_submissions"]) == (1, 1)

    p3 = client.get(base + "?version=V3", headers=headers).json()
    assert p3["cases"] == 1

    p4 = client.get(base + "?case_id=t-case-01", headers=headers).json()
    assert (p4["cases"], p4["labeler_submissions"]) == (1, 1)

    # Combined filters that contradict each other match nothing.
    p5 = client.get(base + "?case_id=t-case-01&specialty=cardiology", headers=headers).json()
    assert p5["cases"] == 0
    assert p5["exportable"] is False


def test_export_case_preview_env_routes_to_environments():
    """ENV, not V5. Since the relabel (Longitudinal E2E PRD §5.1) the environments
    redirect is keyed on the ENV scope; keying it on V5 is what made the export tab
    answer "ships via the environments pipeline" for the one product this whole
    pipeline exists to sell."""
    store = _store()
    headers = _admin_headers(store)
    p = client.get("/api/asclepius/admin/export/case-preview?version=ENV",
                   headers=headers).json()
    assert p["exportable"] is False
    assert "environments pipeline" in (p["note"] or "")


def test_export_case_preview_v5_resolves_records_like_v3_and_v4():
    """The bug the relabel fixes. V5 must resolve through the ordinary record path
    — a longitudinal submission is a record in ``records``, not an env run — so an
    empty V5 slice reads as "nothing matches these filters", never as a redirect to
    a pipeline that does not hold it."""
    store = _store()
    headers = _admin_headers(store)
    p = client.get("/api/asclepius/admin/export/case-preview?version=V5",
                   headers=headers).json()
    assert "environments pipeline" not in (p["note"] or "")


def test_export_case_options_describes_every_version():
    """An operator picking a slice to send a buyer sees what each one IS. "V4"
    alone does not say "real static", and the wrong slice is not recoverable."""
    store = _store()
    headers = _admin_headers(store)
    body = client.get("/api/asclepius/admin/export/case-options", headers=headers).json()
    assert body["versions"] == ["V3", "V4", "V5"]
    assert body["version_descriptions"] == {
        "V3": "synthetic multimodal",
        "V4": "real static",
        "V5": "real longitudinal",
    }
    assert "ENV" not in body["versions"], "the agentic tier is not a portal version"


def test_export_case_bundle_builds_one_bundle_and_409s_on_empty():
    """Seam 2: an exact-case cut is ONE call to export_by_case, producing ONE
    bundle. It used to loop build_export once per labeler submission, so the
    operator got N bundles to download individually and none of them carried the
    case-keyed cases.jsonl that "export by case" is named for."""
    from asclepius import export as asc_export

    store = _store()
    headers = _admin_headers(store)
    # Two submissions of the SAME case — the exact shape that used to fragment.
    _mk_export_record(store, task_id="t-case-03", submission_id="s-0003")
    _mk_export_record(store, task_id="t-case-03", submission_id="s-0004")

    r = client.post("/api/asclepius/admin/export/case-bundle",
                    json={"case_id": "t-case-03"}, headers=headers)
    if not hasattr(asc_export, "export_by_case"):
        assert r.status_code == 503, "PRD-A absent: fail legibly, do not 500"
        pytest.skip("export_by_case ships with PRD-A; verified in the integration tree")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bundle_count"] == 1, "an exact-case cut fragmented into several bundles"
    assert body["record_count"] == 2
    assert body["exports"][0]["export_id"]

    # What the preview showed is what shipped — same resolver, so an empty
    # slice refuses to build instead of shipping the wrong thing.
    r2 = client.post("/api/asclepius/admin/export/case-bundle",
                     json={"case_id": "t-nope"}, headers=headers)
    assert r2.status_code == 409


def test_export_wires_the_case_centric_entry_point():
    """Seam 2 is a contract, and the previous round's failure was a comment
    saying it would be wired that never wired it. Assert the call site."""
    src = (Path(__file__).resolve().parents[1] / "routers" / "asclepius_admin.py").read_text()
    assert "export_by_case" in src
    assert "submission_id=sid" not in src, "the per-submission loop is back"


# ─── Metrics: the four questions (Phase 6) ───────────────────────────────────
def test_metrics_four_questions_defensive_and_populated():
    store = _store()
    headers = _admin_headers(store)
    doc = A.make_user(store, role="evaluator")
    store.insert_submission(
        submission_id="s-m1", task_id="t-m1", evaluator_id=doc["id"], verdict="A",
        chosen_id="a", rejected_id="b", confidence="high", time_spent_sec=60,
        payload={}, annotator={}, dedupe_hash=None)

    res = client.get("/api/asclepius/admin/metrics/questions", headers=headers)
    assert res.status_code == 200, res.text
    q = res.json()
    assert set(q) == {"supply", "quality", "pipeline", "demand"}
    assert q["supply"]["physicians_active_week"] == 1
    assert q["supply"]["cases_labeled"] == 1
    assert len(q["supply"]["spark"]) == 14
    assert q["supply"]["spark"][-1] == 1              # today's submission
    # No case_reviews table yet: acceptance is null (no data), NOT a 0% rate.
    assert q["quality"]["expert_acceptance"] is None
    assert q["quality"]["reviews_scored"] == 0
    assert q["pipeline"]["uploads_received"] == 0
    assert q["demand"]["exports"] == 0

    # Once PRD-A's reviews exist, acceptance is computed from verdicts —
    # separately from κ, which this payload deliberately does not carry.
    with store._conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS case_reviews (
            review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            submission_id TEXT NOT NULL, reviewer_user_id TEXT NOT NULL,
            reviewer_id_hashed TEXT NOT NULL, verdict TEXT NOT NULL,
            dimension_json TEXT, corrections_json TEXT, reviewer_notes TEXT,
            time_spent_sec INTEGER, blinded INTEGER, created_at TEXT NOT NULL)""")
        for i, verdict in enumerate(["accept", "accept_with_edits", "reject", "accept"]):
            conn.execute(
                "INSERT INTO case_reviews (review_id, task_id, submission_id, "
                "reviewer_user_id, reviewer_id_hashed, verdict, created_at) "
                "VALUES (?, 't-m1', 's-m1', 'u-r', 'h', ?, ?)",
                (f"rev-m{i}", verdict, "2026-01-01T00:00:00+00:00"))
    q2 = client.get("/api/asclepius/admin/metrics/questions", headers=headers).json()
    assert q2["quality"]["reviews_scored"] == 4
    assert "kappa" not in q2["quality"]

    # Seam 3: acceptance comes from agreement.review_acceptance() and NOWHERE
    # else. Strict accepts only — 2 of 4 here. The old inline SQL counted
    # accept + accept_with_edits and so reported 0.75 for the same reviews,
    # which is why the dashboard and quality_report.md disagreed.
    from asclepius import agreement as asc_agreement
    if hasattr(asc_agreement, "review_acceptance"):
        assert q2["quality"]["expert_acceptance"] == 0.5, (
            "expert acceptance is not agreement.review_acceptance()'s number"
        )
        # The combined figure still exists — under a different word.
        assert q2["quality"]["not_rejected"] == 0.75
    else:
        # PRD-A not merged: report unknown rather than a rival definition.
        assert q2["quality"]["expert_acceptance"] is None
        assert q2["quality"]["not_rejected"] is None


def test_expert_acceptance_has_exactly_one_definition():
    """Seam 3: no second implementation may reappear in the PRD-C block."""
    src = (Path(__file__).resolve().parents[1] / "asclepius" / "store.py").read_text()
    assert "accept_with_edits" not in src or "review_acceptance" in src
    # The specific inline SQL that produced the rival number must stay gone.
    assert "verdict IN ('accept', 'accept_with_edits')" not in src, (
        "the inline acceptance SQL is back — agreement.review_acceptance() is "
        "the only definition (Seam 3)"
    )


def test_metrics_questions_requires_admin():
    store = _store()
    doc = A.make_user(store, role="evaluator")
    assert client.get("/api/asclepius/admin/metrics/questions",
                      headers=A.headers_for(doc)).status_code in (401, 403)


# ─── Admin list ──────────────────────────────────────────────────────────────
def test_admin_list_health_systems(email_ok):
    store = _store()
    headers = _admin_headers(store)
    client.post("/api/asclepius/admin/health-systems/provision",
                json={"organization": "Mass General Hospital", "email": "data@mgh.harvard.edu", "purpose": "task_creation"},
                headers=headers)
    res = client.get("/api/asclepius/admin/health-systems", headers=headers)
    assert res.status_code == 200
    rows = res.json()["health_systems"]
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Mass General Hospital"
    assert row["portal_users"][0]["username"] == "massgeneral"
    assert row["uploads_count"] == 0
    assert row["physicians_linked"] == 0
