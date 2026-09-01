"""The admin Health Systems section actually renders the purpose — PRD-I §2.2, §5.

Source-grepping a frontend module proves it was written, not that it works. This
repo has already paid for that lesson once: a section can be complete, correct
and INVISIBLE for a whole build round because nothing mounted it and the failure
was quiet. So this executes ``admin_health.js`` against the DOM shim and asserts
what lands in the document.

What is asserted is the thing an operator's eye actually keys off: brokering has
its OWN bucket with no Promote button, an unresolved purpose renders as a work
item rather than a default, and the chain-of-custody triple is on every row.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


_HS_ID = "hs-alpha-000001"

_DETAIL = {
    "health_system": {"hs_id": _HS_ID, "name": "Alpha Regional Hospital",
                      "contact_email": "it@example.org", "active": True},
    "portal_users": [
        {"username": "alpharegional", "email": "it@example.org", "last_login": None,
         "active": True, "purpose": "brokering", "label": "brokering",
         "accent": "grey", "resolved": True},
        {"username": "alphalegacy", "email": "old@example.org", "last_login": None,
         "active": True, "purpose": None, "label": "Purpose not set — legacy link",
         "accent": "lime", "resolved": False},
    ],
    "physicians_linked": 3,
    "uploads_total": 2,
    "last_activity": "2026-08-01T10:00:00",
    "link_purpose_note": "Links minted from the legacy magic-link form carry no purpose.",
    "buckets": {
        "needs_attention": [],
        "rejected": [],
        "needs_review": [],
        "ready_to_promote": [{
            "upload_id": "upl-task-1", "filename": "task.zip",
            "received_at": "2026-08-01T09:00:00", "size_bytes": 2048,
            "sha256": "a" * 64, "sha256_short": "aaaaaaaaaaaa",
            "verified_at": "2026-08-01T09:01:00",
            "purpose": "task_creation", "label": "task creation", "accent": "green",
            "resolved": True, "upload_status": "ingested", "case_total": 1,
            "case_counts": {"held": 0, "clean": 1, "promoted": 0},
            "specialties": ["nephrology"], "specialty_determined": True,
            "specialty_undetermined_cases": 0, "reasons": [], "note": None}],
        "in_production": [],
        "brokering": [{
            "upload_id": "upl-broker-1", "filename": "broker.zip",
            "received_at": "2026-08-01T08:00:00", "size_bytes": 4096,
            "sha256": "b" * 64, "sha256_short": "bbbbbbbbbbbb",
            "verified_at": "2026-08-01T08:01:00",
            "purpose": "brokering", "label": "brokering", "accent": "grey",
            "resolved": True, "upload_status": "ingested", "case_total": 1,
            "case_counts": {"held": 0, "clean": 1, "promoted": 0},
            "specialties": ["nephrology"], "specialty_determined": True,
            "specialty_undetermined_cases": 0, "reasons": [], "note": None}],
    },
}

_LIST = {"health_systems": [{
    "hs_id": _HS_ID, "name": "Alpha Regional Hospital", "contact_email": "it@example.org",
    "active": True, "created_at": "2026-07-01T00:00:00",
    "portal_users": [{"username": "alpharegional", "email": "it@example.org",
                      "last_login": None, "active": True}],
    "purposes": [{"purpose": "brokering", "label": "brokering", "accent": "grey",
                  "resolved": True}],
    "purpose_unresolved": 0, "brokering_uploads": 1,
    "physicians_linked": 3, "uploads_count": 2, "last_activity": "2026-08-01T10:00:00"}]}

_RECONCILE = {"missing_blobs": [], "orphan_blobs": ["c" * 64], "n_rows": 7, "n_files": 8,
              "checked_at": "2026-08-05T00:00:00", "missing_count": 0, "orphan_count": 1,
              "storage": [{"store": "database", "durable": True, "detail": "ok"},
                          {"store": "raw ingest", "durable": True, "detail": "ok"},
                          {"store": "asset store", "durable": True, "detail": "ok"}],
              "all_durable": True}

_JS = """
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
var ROUTES = %(routes)s;
var ctx = {
  h: h,
  clear: function (el) { while (el.firstChild) el.removeChild(el.firstChild); },
  api: function (path) { return Promise.resolve(ROUTES[path] || {}); },
  toast: function () {},
  loadingCard: function (t) { return h('div', {}, t); },
  downloadBlob: function () {},
  fmtDate: function (d) { return String(d); },
  openPipeline: function () {},
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
function byTag(el, tag) {
  var out = [];
  (el.childNodes || []).forEach(function (c) {
    if (!c.tagName) return;
    if (c.tagName === tag) out.push(c);
    out = out.concat(byTag(c, tag));
  });
  return out;
}
%(src)s
var mount = document.createElement('div');
var body = document.createElement('div');
mount.appendChild(body);
window.AdminHealthSection.reset();
%(open)s
window.AdminHealthSection.render(body, ctx);
setTimeout(function () {
  // Group the rendered cards so assertions can talk about buckets rather than
  // the whole document — a substring match across the page would pass on the
  // brokering row appearing in the WRONG card, which is the failure that matters.
  var cards = [];
  (function walk(el) {
    (el.childNodes || []).forEach(function (c) {
      if (!c.tagName) return;
      if ((c.className || '').indexOf('asc-card') === 0) {
        var titleEl = null;
        (function find(e) {
          (e.childNodes || []).forEach(function (x) {
            if (!x.tagName || titleEl) return;
            if ((x.className || '').indexOf('asc-card-title') === 0) { titleEl = x; return; }
            find(x);
          });
        })(c);
        cards.push({ title: titleEl ? textOf(titleEl) : '',
                     text: textOf(c), classes: classesOf(c).join(' '),
                     buttons: byTag(c, 'BUTTON').map(textOf) });
      }
      walk(c);
    });
  })(mount);
  console.log(JSON.stringify({ text: textOf(mount), cards: cards,
                               buttons: byTag(mount, 'BUTTON').map(textOf),
                               classes: classesOf(mount).join(' ') }));
}, 60);
"""


_DEMO_META = {"available": True, "byte_size": 76388352, "mime": "video/mp4",
              "max_upload_bytes": 536870912,
              "url": "/api/asclepius/assets/onboarding-demo"}


def _render(open_detail: bool, *, health_systems: dict | None = None,
            reconcile: dict | None = None) -> dict:
    src = (_FRONTEND / "admin_health.js").read_text(encoding="utf-8")
    routes = {
        "/admin/health-systems": health_systems if health_systems is not None else _LIST,
        "/admin/health-systems/" + _HS_ID: _DETAIL,
        "/admin/storage/reconcile": reconcile if reconcile is not None else _RECONCILE,
        "/assets/onboarding-demo/meta": _DEMO_META,
    }
    return _run_node(_JS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "routes": json.dumps(routes),
        "src": src,
        "open": (f"window.AdminHealthSection.open({json.dumps(_HS_ID)});"
                 if open_detail else ""),
    })


def _card(out: dict, title: str) -> dict:
    """Locate a card by its TITLE, not by a substring of the whole card.

    A page-wide substring match would pass on the brokering row appearing inside
    the WRONG card — which is precisely the failure these tests exist to catch."""
    for c in out["cards"]:
        if title.lower() in (c.get("title") or "").lower():
            return c
    raise AssertionError(f"no card titled {title!r}. Titles: "
                         f"{[c.get('title') for c in out['cards']]}")


# ── the brokering bucket ─────────────────────────────────────────────────────
def test_brokering_renders_as_its_own_bucket():
    out = _render(open_detail=True)
    card = _card(out, "Brokering")
    assert "upl-broker-1" in card["text"]
    assert "never promoted" in card["text"].lower()


def test_the_brokering_bucket_has_no_promote_button():
    """The server refuses to promote brokering data. A design that relies only on
    the server refusing is one where an operator keeps clicking — so the control
    that would try is not rendered at all."""
    out = _render(open_detail=True)
    card = _card(out, "Brokering")
    assert not [b for b in card["buttons"] if "promote" in b.lower()], card["buttons"]
    assert [b for b in card["buttons"] if "download" in b.lower()], (
        "every bucket downloads, including this one")


def test_a_brokering_upload_never_appears_in_ready_to_promote():
    out = _render(open_detail=True)
    ready = _card(out, "Ready to promote")
    assert "upl-task-1" in ready["text"]
    assert "upl-broker-1" not in ready["text"]


# ── purpose chips ────────────────────────────────────────────────────────────
def test_the_purpose_chip_uses_the_accent_the_server_assigned():
    out = _render(open_detail=True)
    ready = _card(out, "Ready to promote")
    assert "task creation" in ready["text"]
    assert "asc-badge-green" in ready["classes"], (
        "task creation is green — it becomes physician-authored work")
    broker = _card(out, "Brokering")
    assert "asc-badge-gray" in broker["classes"], (
        "brokering is muted grey, NOT pink: it is a normal business line, and pink "
        "in this palette means flag / PHI / critical")
    assert "asc-badge-red" not in broker["classes"]


def test_an_unresolved_purpose_renders_as_a_work_item_with_a_way_to_fix_it():
    out = _render(open_detail=True)
    accounts = _card(out, "Portal accounts")
    assert "Purpose not set" in accounts["text"]
    assert "asc-badge-lime" in accounts["classes"], "lime means needs attention"
    # A work item you cannot action is a decoration.
    assert [b for b in accounts["buttons"] if "Set:" in b], accounts["buttons"]


def test_every_upload_row_carries_the_chain_of_custody():
    out = _render(open_detail=True)
    ready = _card(out, "Ready to promote")
    assert "aaaaaaaaaaaa" in ready["text"], "truncated sha256 is missing"
    assert "verified" in ready["text"].lower()
    assert "2.0 KB" in ready["text"] or "2 KB" in ready["text"], ready["text"]


def test_the_legacy_link_note_is_surfaced():
    out = _render(open_detail=True)
    assert "legacy magic-link" in out["text"]


# ── the list view + storage panel ────────────────────────────────────────────
def test_the_list_shows_a_purpose_column():
    out = _render(open_detail=False)
    card = _card(out, "Health systems")
    assert "Purpose" in card["text"]
    assert "brokering" in card["text"]


def test_the_storage_panel_states_the_healthy_case_explicitly():
    """An empty panel and a healthy panel must not look identical — otherwise
    'nothing shown' reads as 'nothing wrong' whether or not the check ever ran."""
    out = _render(open_detail=False)
    card = _card(out, "Storage integrity")
    assert "All 7 asset references resolve" in card["text"]
    assert "1 unreferenced blob" in card["text"]
    assert "never deleted" in card["text"]


# ═════════════════════════════════════════════════════════════════════════════
# The panels that are not about health systems
# ═════════════════════════════════════════════════════════════════════════════
# The storage and demo-video panels render inside this section but describe the
# deployment, not the partners. They were mounted after an early return taken on
# `!rows.length`, so on a deployment with no health systems both disappeared —
# and a deployment with no partners is a FRESH one, which is precisely when the
# demo video has not been uploaded and the volume has not been checked. The
# console uploader exists so that job needs no terminal; gated this way, it left
# no door at all.

def test_the_storage_and_demo_panels_render_when_there_are_no_health_systems():
    out = _render(open_detail=False, health_systems={"health_systems": []})
    assert "No health systems yet" in out["text"], "the empty state still renders"
    _card(out, "Storage integrity")
    _card(out, "Onboarding demo video")


def test_the_demo_uploader_is_reachable_with_no_health_systems():
    """Not merely present — usable. The drop zone is the only upload control."""
    out = _render(open_detail=False, health_systems={"health_systems": []})
    card = _card(out, "Onboarding demo video")
    assert "asc-demo-drop" in card["classes"]


def test_both_panels_still_render_when_health_systems_exist():
    out = _render(open_detail=False)
    _card(out, "Storage integrity")
    _card(out, "Onboarding demo video")


# ── the storage badge tells the truth about durability ───────────────────────
# It used to be computed from `missing_count` alone, so a deployment whose asset
# store was ephemeral showed a green OK directly above the sentence "blobs will
# be lost on redeploy": the panel contradicting itself, with the reassuring half
# set in the larger type. Data already gone is red; data that WILL go is lime,
# which is what lime means everywhere else in this palette.

def _reconcile_with_ephemeral_asset_store() -> dict:
    rep = copy.deepcopy(_RECONCILE)
    rep["storage"][2] = {"store": "asset store", "durable": False,
                         "detail": "asset store /data/assets is NOT under the "
                                   "persistent volume mounted at /srv/volume"}
    rep["all_durable"] = False
    return rep


def test_an_ephemeral_asset_store_is_never_badged_ok():
    out = _render(open_detail=False, reconcile=_reconcile_with_ephemeral_asset_store())
    card = _card(out, "Storage integrity")
    assert "asc-badge-green" not in card["classes"], (
        "a store that loses everything on redeploy is not OK")
    assert "asc-badge-lime" in card["classes"], "lime is this palette's needs-attention"
    assert "Not durable" in card["text"]
    # And the reason is on the page, naming the resolved path.
    assert "/data/assets" in card["text"]


def test_missing_blobs_outrank_a_durability_warning():
    """Red beats lime: data already gone is the more urgent of the two, and the
    badge has one slot."""
    rep = _reconcile_with_ephemeral_asset_store()
    rep["missing_count"] = 2
    out = _render(open_detail=False, reconcile=rep)
    card = _card(out, "Storage integrity")
    assert "asc-badge-red" in card["classes"]
    assert "2 missing" in card["text"]


def test_a_healthy_deployment_still_says_where_its_stores_are():
    """Listing only the failures left the panel blank when things were fine,
    which reads as "not checked" rather than "safe" — and left an operator
    asking where the demo video actually lives with nowhere to look."""
    rep = copy.deepcopy(_RECONCILE)
    rep["storage"][2]["detail"] = ("asset store /data/assets is on the persistent "
                                   "volume mounted at /data")
    out = _render(open_detail=False, reconcile=rep)
    card = _card(out, "Storage integrity")
    assert "asc-badge-green" in card["classes"]
    assert "/data/assets" in card["text"], "name the resolved path, not just 'durable'"
    for store in ("database", "raw ingest", "asset store"):
        assert store in card["text"]

