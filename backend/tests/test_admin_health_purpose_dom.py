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


def _render(open_detail: bool) -> dict:
    src = (_FRONTEND / "admin_health.js").read_text(encoding="utf-8")
    routes = {
        "/admin/health-systems": _LIST,
        "/admin/health-systems/" + _HS_ID: _DETAIL,
        "/admin/storage/reconcile": _RECONCILE,
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
