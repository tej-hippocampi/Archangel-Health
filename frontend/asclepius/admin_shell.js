/* ═══════════════════════════════════════════════════════════════════════════
   Asclepius Operations: the admin console, as its own page.

   Until PRD-F the console lived inside asclepius.js. Every physician who
   opened the portal downloaded roughly 3,600 lines of admin code they could
   never run, and every operator worked inside the physician app wearing a
   different hat. This file is that console, moved: the shell, the section
   routing and the admin-only renderers, unchanged in behaviour.

   WHY THIS FILE CARRIES ITS OWN PRIMITIVES. asclepius.js is a closed IIFE
   with no exports and this repo has no build step, so `h`, `api`, `clear` and
   their neighbours cannot be imported. The alternative was a third shared
   file that both pages load in a fragile order for the sake of forty lines of
   helpers. These copies are deliberate, they are small, and the two surfaces
   are now free to diverge in chrome without either one dragging the other.

   SECURITY IS NOT IN THIS FILE. Every admin API call is gated server-side by
   `require_admin` (or the payments router's admin dependencies) and none of
   that moved. What the boot sequence below does is tell an operator the truth
   about the session they are holding instead of mounting furniture whose
   every fetch would 401.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const API_BASE = '/api/asclepius';
  // Sandbox PRD §1.3: which realm this page IS. /sandbox/admin serves this
  // same shell with window.__REALM='sandbox' injected; every request sends
  // the realm header, the stored token is keyed per realm (so a live and a
  // sandbox operator session coexist in one browser), and cross-page links
  // stay inside the realm.
  const REALM = (window.__REALM === 'sandbox') ? 'sandbox' : 'live';
  const REALM_HEADER = 'X-Asclepius-Realm';
  function realmHeaders(h) { h = h || {}; h[REALM_HEADER] = REALM; return h; }
  function realmPath(p) { return REALM === 'sandbox' ? '/sandbox' + p : p; }
  function adminPath() { return REALM === 'sandbox' ? '/sandbox/admin' : '/asclepius/admin'; }
  // Companion header on the credential-verification 403 (asclepius/auth.py
  // AUTH_GATE_HEADER): 'pending' or 'rejected'.
  const AUTH_GATE_HEADER = 'X-Asclepius-Auth-Gate';
  // The SAME storage key the product uses, deliberately. Separation here is
  // surface separation, not a second credential store (PRD F3 / "out of
  // scope"): an operator already signed in on /asclepius is signed in here.
  const TOKEN_KEY = REALM === 'sandbox' ? 'asclepius_token_sandbox' : 'asclepius_token';

  // The two roles the product's console button admitted, unchanged (F5). A
  // qa_reviewer keeps its door; what each of them may DO is still decided by
  // the server on every call.
  const ADMIN_ROLES = ['admin', 'qa_reviewer'];

  const state = {
    token: localStorage.getItem(TOKEN_KEY) || null,
    user: null,
    taxonomy: null,
    // The five keys from the product console are preserved BYTE FOR BYTE
    // (F4): they are read by ADMIN_TAB_ALIASES, by the subnav lookups, by
    // openBatchesFor and by the physician-row route-in. `referrals` is the
    // sixth tab this PR adds; it takes a new key rather than renaming one.
    adminTab: 'physicians', // physicians | work | money | data | community | referrals
    pipelineFocus: null,    // upload_id deep-linked from a Data bucket row
    adminSub: {
      // No 'physicians' key: that section owns its own two-tab strip.
      work: 'tasks',        //   tasks | assign | qa | metrics
      money: 'earnings',    //   earnings | referrals  (referrals routes to its own tab)
      data: 'systems',      //   systems | pipeline | export
      export: 'bycase',     //   bycase | buyers | history (inside Data > Export)
      referrals: 'people',  //   people | systems
      sandbox: 'accounts',  //   accounts | outbox   (Sandbox PRD §3; sandbox realm only)
    },
    // Org → contributor drill-down state, shared shape across Exports + Metrics.
    browse: {
      export: { level: 'orgs', org: null, idHashed: null, contributor: null },
      metrics: { level: 'orgs', org: null, idHashed: null, contributor: null },
    },
    batches: null,
    dataCreation: null,
  };

  // ─── Tiny DOM helper ───────────────────────────────────────────────────────
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v == null || v === false) continue;
        if (k === 'class' || k === 'className') el.className = v;
        else if (k === 'text' || k === 'textContent') el.textContent = v;
        else if (k === 'html') el.innerHTML = v;
        else if (k === 'dataset') { for (const d in v) el.dataset[d] = v[d]; }
        else if (k === 'disabled') { if (v) el.setAttribute('disabled', ''); }
        else if (k === 'hidden') { if (v) el.setAttribute('hidden', ''); }
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') {
          el.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (k === 'value') { el.value = v; }
        else el.setAttribute(k, v);
      }
    }
    appendChildren(el, children);
    return el;
  }
  function appendChildren(el, children) {
    for (const c of children) {
      if (c == null || c === false) continue;
      if (Array.isArray(c)) appendChildren(el, c);
      else if (c instanceof Node) el.appendChild(c);
      else el.appendChild(document.createTextNode(String(c)));
    }
  }
  const $ = (sel, root) => (root || document).querySelector(sel);
  const root = () => document.getElementById('ascAdminRoot');
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function setRoot(node) { const r = root(); clear(r); r.appendChild(node); }

  // ─── Fetch helper (injects Bearer, parses JSON, handles 401) ────────────────
  async function api(path, opts) {
    opts = opts || {};
    const headers = realmHeaders(opts.headers || {});
    if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
    let body = opts.body;
    if (body !== undefined && !opts.isForm) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    let res;
    try {
      // `opts.base` overrides the asclepius prefix for the handful of admin
      // reads that live outside it (the landing lead table is on /api/leads,
      // beside the public form that writes it).
      res = await fetch((opts.base || API_BASE) + path,
                        { method: opts.method || 'GET', headers, body });
    } catch (e) {
      throw { status: 0, detail: 'Network error. Is the backend running?', message: 'Network error' };
    }
    if (res.status === 401 && !opts.noAuthHandler) {
      handleUnauthorized();
      throw { status: 401, detail: 'Session expired', message: 'Session expired' };
    }
    if (opts.raw) return res;
    let data = null;
    const ct = res.headers.get('content-type') || '';
    if (ct.indexOf('application/json') !== -1) {
      data = await res.json().catch(() => null);
    } else {
      data = await res.text().catch(() => null);
    }
    if (!res.ok) {
      const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : data;
      throw {
        status: res.status,
        detail,
        message: detailToMessage(detail, res.status),
        authGate: res.headers.get(AUTH_GATE_HEADER),
      };
    }
    return data;
  }

  function detailToMessage(detail, status) {
    if (!detail) return 'Request failed (' + status + ')';
    if (typeof detail === 'string') return detail;
    if (typeof detail === 'object') {
      if (detail.message) return detail.message;
      try { return JSON.stringify(detail); } catch (e) { return 'Request failed (' + status + ')'; }
    }
    return 'Request failed (' + status + ')';
  }

  function handleUnauthorized() {
    state.token = null;
    state.user = null;
    localStorage.removeItem(TOKEN_KEY);
    renderGate('Your session expired. Sign in again.');
  }

  // ─── Toasts ────────────────────────────────────────────────────────────────
  function toast(msg, kind) {
    const region = document.getElementById('ascToasts');
    if (!region) return;
    const t = h('div', { class: 'asc-toast ' + (kind || 'info') }, msg);
    region.appendChild(t);
    setTimeout(() => {
      t.style.transition = 'opacity .3s';
      t.style.opacity = '0';
      setTimeout(() => t.remove(), 320);
    }, kind === 'error' ? 5200 : 3200);
  }

  function loadingCard(label) {
    return h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), label || 'Loading…'));
  }

  function sectionModuleMissing(body, name) {
    body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-inline-error' },
        name + ' failed to load: refresh the page. If it persists, check that the ' +
        'script is included in admin.html.'))));
  }

  // All admin timestamps are STORED as naive UTC ISO strings (Python
  // datetime.utcnow().isoformat(); no trailing 'Z'/offset). new Date() would
  // otherwise parse them as browser-local time, so we append 'Z' to pin them to
  // UTC, then render in Pacific (America/Los_Angeles handles PST/PDT itself).
  const ASC_TZ = 'America/Los_Angeles';
  function toUtcDate(d) {
    if (d == null) return null;
    if (typeof d === 'string') {
      const s = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(d) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(d)
        ? d + 'Z' : d;
      return new Date(s);
    }
    return new Date(d);
  }
  function fmtDate(d) {
    if (!d) return 'n/a';
    const dt = toUtcDate(d);
    if (isNaN(dt.getTime())) return String(d);
    return dt.toLocaleString('en-US', { timeZone: ASC_TZ }) + ' PT';
  }
  function formatTime(sec) {
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + String(s).padStart(2, '0');
  }
  function trunc(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; }

  // Copy-to-clipboard for case, submission and export ids (Export & Approval
  // PRD 1.3). An id an operator cannot copy is an id they re-type wrong: every
  // table that rendered one truncated it, so an operator who wanted to paste
  // `t-4f2a91...` into the export box had to go find it somewhere else.
  //
  // ONE helper, used everywhere an id renders: the earnings ledger, task
  // routing, and the export tab's results. The full id is always in the DOM and
  // always in the `title`; the copy button is the affordance that makes it
  // usable, with a `document.execCommand` fallback for the browsers (and the
  // insecure-origin cases) where `navigator.clipboard` is not there.
  function copyTextToClipboard(text, onDone) {
    const done = typeof onDone === 'function' ? onDone : () => {};
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => done(true), () => done(legacyCopy(text)));
      return;
    }
    done(legacyCopy(text));
  }

  function legacyCopy(text) {
    // A hidden, SELECTED input, the fallback the PRD asks for. Positioned off
    // screen rather than `display:none`, because a hidden element cannot be
    // selected and the copy silently does nothing.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-1000px;left:-1000px;opacity:0';
    document.body.appendChild(ta);
    let ok = false;
    try {
      ta.select();
      ta.setSelectionRange(0, text.length);
      ok = document.execCommand('copy');
    } catch (e) { ok = false; } finally { ta.remove(); }
    return ok;
  }

  function copyableId(id, opts) {
    const full = (id === null || id === undefined) ? '' : String(id);
    if (!full) return h('span', { class: 'asc-dim', title: 'No id' }, '\u2014');
    const o = opts || {};
    const btn = h('button', {
      type: 'button', class: 'asc-id-copy',
      title: 'Copy ' + full, 'aria-label': 'Copy ' + full,
    }, '⧉');
    btn.addEventListener('click', (ev) => {
      // Ledger and roster rows are themselves clickable. Copying an id is not
      // "open this row", and a copy that also navigates away is a copy the
      // operator never gets to paste.
      ev.stopPropagation();
      ev.preventDefault();
      copyTextToClipboard(full, (ok) => {
        btn.textContent = ok ? 'Copied' : 'Press ⌘C';
        btn.classList.add('is-copied');
        setTimeout(() => { btn.textContent = '⧉'; btn.classList.remove('is-copied'); }, 1400);
      });
    });
    return h('span', { class: 'asc-id' + (o.wrap ? ' is-wrap' : '') },
      h('code', { class: 'asc-id-text', title: full }, full), btn);
  }

  async function fetchAssetBlobUrl(assetId) {
    const res = await fetch(API_BASE + '/assets/' + encodeURIComponent(assetId), {
      headers: realmHeaders(state.token ? { Authorization: 'Bearer ' + state.token } : {}),
    });
    if (!res.ok) throw new Error('asset ' + res.status);
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  }

  // Avatars are bearer-authenticated, so a section cannot just set an <img
  // src>. One shared loader with a shared cache: a roster and a dossier
  // showing the same physician fetch the bytes once.
  const avatarBlobCache = {};
  const avatarBlobPending = {};
  function loadAvatarBlob(url) {
    if (avatarBlobCache[url]) return Promise.resolve(avatarBlobCache[url]);
    if (avatarBlobPending[url]) return avatarBlobPending[url];
    const inflight = fetch(url, {
      headers: state.token ? { Authorization: 'Bearer ' + state.token } : {},
    }).then((res) => (res.ok ? res.blob() : null))
      .then((blob) => {
        if (!blob) return null;
        const objectUrl = URL.createObjectURL(blob);
        avatarBlobCache[url] = objectUrl;
        return objectUrl;
      })
      .catch(() => null);
    // Cleared either way: a failed fetch must be retryable on the next render,
    // and a successful one is answered from avatarBlobCache from here on.
    avatarBlobPending[url] = inflight;
    inflight.then(() => { delete avatarBlobPending[url]; },
                  () => { delete avatarBlobPending[url]; });
    return inflight;
  }

  // ─── QA's edited-answer diff ───────────────────────────────────────────────
  // Moved with qaDiffBlock, which is the only caller on this surface. A
  // reviewer's edit is judged by what CHANGED, and a plain before/after pair
  // makes a QA operator re-read two paragraphs to find one clause.
  function splitSentences(text) {
    const t = text || '';
    const out = [];
    let cur = '';
    for (let i = 0; i < t.length; i++) {
      const ch = t[i];
      cur += ch;
      if (ch === '\n') { out.push(cur); cur = ''; continue; }
      if (ch === '.' || ch === '!' || ch === '?') {
        const prev = t[i - 1], next = t[i + 1];
        const isDecimal = ch === '.' && prev >= '0' && prev <= '9' && next >= '0' && next <= '9';
        if (!isDecimal && (next === undefined || next === ' ' || next === '\t' || next === '\n')) {
          while (i + 1 < t.length && (t[i + 1] === ' ' || t[i + 1] === '\t')) cur += t[++i];
          out.push(cur); cur = '';
        }
      }
    }
    if (cur) out.push(cur);
    return out.length ? out : (t ? [t] : []);
  }
  function normSentence(s) {
    return (s || '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').replace(/\s+/g, ' ').trim();
  }
  // Token set of a normalized sentence (for the soft Jaccard match below).
  function sentTokenSet(norm) {
    const set = new Set();
    for (const t of (norm || '').split(' ')) { if (t) set.add(t); }
    return set;
  }
  // Token-set Jaccard similarity between two normalized sentences.
  function tokenJaccard(setA, setB) {
    if (!setA.size || !setB.size) return 0;
    let inter = 0;
    for (const t of setA) { if (setB.has(t)) inter++; }
    const union = setA.size + setB.size - inter;
    return union ? inter / union : 0;
  }
  // LCS over normalized sentences → per-sentence shared/divergent flags.
  // ``soft`` (§3.1, V3/V4 only): near-identical clinical sentences also count as
  // shared (token-set Jaccard ≥ 0.85), so shared boilerplate dims on real prose
  // instead of only on byte-identical sentences. V1/V2 keep exact matching.
  function diffFlags(aSents, bSents, soft) {
    const aN = aSents.map(normSentence), bN = bSents.map(normSentence);
    const n = aN.length, m = bN.length;
    if (n * m > 40000) { // pathological size: skip dimming rather than lock the UI
      return { a: aSents.map(() => false), b: bSents.map(() => false), any: false };
    }
    const aT = soft ? aN.map(sentTokenSet) : null;
    const bT = soft ? bN.map(sentTokenSet) : null;
    const eq = (i, j) => {
      if (!aN[i]) return false;
      if (aN[i] === bN[j]) return true;
      return !!soft && tokenJaccard(aT[i], bT[j]) >= 0.85;
    };
    const dp = [];
    for (let i = 0; i <= n; i++) dp.push(new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = eq(i, j) ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const aShared = new Array(n).fill(false), bShared = new Array(m).fill(false);
    let i = 0, j = 0, any = false;
    while (i < n && j < m) {
      if (eq(i, j)) { aShared[i] = bShared[j] = true; any = true; i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
      else j++;
    }
    return { a: aShared, b: bShared, any };
  }
  // Pure A/B divergence diff (§3.1): no app state, unit-testable.
  //   * some sentences shared → dim shared, brighten divergent (as before);
  //   * NOTHING shared and opts.markAllWhenDisjoint → every sentence marked
  //     divergent (allDivergent:true) so fully-divergent answers still highlight
  //     (the "diff not working" bug: returning null rendered plain text);
  //   * otherwise null (V1/V2 keep the legacy no-diff fallback).
  function appendTextWithMarks(node, text, errSpans) {
    let rest = text || '';
    for (const es of errSpans || []) {
      const idx = rest.indexOf(es);
      if (idx === -1) continue;
      node.appendChild(document.createTextNode(rest.slice(0, idx)));
      node.appendChild(h('mark', { class: 'asc-err-span', title: 'Model-flagged likely error region' }, es));
      rest = rest.slice(idx + es.length);
    }
    node.appendChild(document.createTextNode(rest));
    return node;
  }
  function sentenceNode(sentence, shared, errSpans) {
    const cls = 'asc-diff-sent' + (shared ? ' asc-diff-shared' : ' asc-diff-changed');
    return appendTextWithMarks(h('span', { class: cls }), sentence, errSpans);
  }
  // Render the revised gold answer with the sentences the doctor CHANGED from the
  // original chosen answer highlighted (Seamless PRD WS4, "see what you changed").
  // Reuses the A/B sentence-diff primitives on (original, revised).
  function renderEditDiff(originalText, revisedText) {
    const wrap = h('div', { class: 'asc-editdiff' });
    const oS = splitSentences(originalText || ''), rS = splitSentences(revisedText || '');
    const flags = diffFlags(oS, rS);
    if (!flags.any) { wrap.appendChild(h('span', { class: 'asc-diff-shared' }, revisedText || '')); return wrap; }
    rS.forEach((s, i) => wrap.appendChild(sentenceNode(s, flags.b[i], null)));
    return wrap;
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     THE MASTHEAD: "the product, flipped"

     The physician's chrome is paper: a light header over a light workspace,
     with a progress bar for the one case in front of them. An operator is
     never looking at their own work, they are looking at everyone's, and the
     console has to say so before it is read. So the chrome inverts to ink and
     the workspace stays the same paper the physician sees: one design system,
     the ground and the figure swapped. It is also the only non-verbal signal
     that separates this page from the physician surface an admin can preview,
     which is a safety property and not a decoration.

     Tabs are the masthead, not a pill strip below it. The console has one
     axis of navigation and giving it two rows of chrome was most of what made
     the old one exhausting.
     ═══════════════════════════════════════════════════════════════════════════ */
  function renderMasthead() {
    const bar = document.getElementById('ascAdminBar');
    if (!bar) return;
    clear(bar);
    bar.removeAttribute('hidden');
    const nav = h('nav', { class: 'asc-admin-tabs', 'aria-label': 'Console sections' });
    ADMIN_TABS.forEach(([id, label]) => {
      const btn = h('button', {
        class: 'asc-admin-tab' + (state.adminTab === id ? ' active' : ''),
        'aria-current': state.adminTab === id ? 'page' : null,
        onClick: () => { state.adminTab = id; renderAdminView(); },
      }, label);
      // The QA pending count rides on the tab QA actually lives under, so the
      // backlog is never invisible from anywhere in the console.
      if (id === 'work') {
        btn.appendChild(h('span', {
          class: 'asc-badge asc-badge-count asc-admin-tab-count',
          id: 'ascQaBadge', hidden: true,
        }));
      }
      nav.appendChild(btn);
    });

    const who = h('div', { class: 'asc-admin-who' },
      h('span', { class: 'asc-admin-who-email' }, (state.user && state.user.email) || ''),
      h('span', { class: 'asc-admin-who-role chrome' }, (state.user && state.user.role) || ''),
      // F6: Evaluate stays in the product. An admin previewing the physician
      // experience is using the physician surface, which is the point of the
      // chooser; duplicating it here would be the intertwining this PR removes,
      // pointed the other way.
      h('a', { class: 'asc-admin-out', href: realmPath('/asclepius') }, 'Physician view'),
      h('button', { class: 'asc-admin-signout', onClick: signOut }, 'Sign out'));

    bar.appendChild(h('div', { class: 'asc-admin-bar-inner' },
      h('a', { class: 'asc-admin-mark', href: adminPath() },
        h('span', { class: 'asc-admin-mark-dot', 'aria-hidden': 'true' }),
        h('span', { class: 'asc-admin-mark-text' }, 'Asclepius',
          h('span', { class: 'asc-admin-mark-sub' }, 'Operations'))),
      nav, who));
    fitMasthead();
  }

  /** The tab strip is navigation; the who-block is information. When one row
   *  cannot hold both — seven sections beside a long operator address on a
   *  laptop — the strip used to scroll with its scrollbar hidden, so the last
   *  sections (Referrals, and Sandbox in the sandbox) were simply not on
   *  screen, with nothing saying so. Measure the overflow and drop the strip
   *  onto its own row instead (admin.css `.stacked`); re-measure on resize. */
  function fitMasthead() {
    const bar = document.getElementById('ascAdminBar');
    const inner = bar && bar.querySelector('.asc-admin-bar-inner');
    const nav = inner && inner.querySelector('.asc-admin-tabs');
    if (!inner || !nav) return;
    inner.classList.remove('stacked');
    if (nav.scrollWidth > nav.clientWidth + 1) inner.classList.add('stacked');
  }
  if (typeof window.addEventListener === 'function') window.addEventListener('resize', fitMasthead);

  function signOut() {
    state.token = null;
    state.user = null;
    localStorage.removeItem(TOKEN_KEY);
    renderGate();
  }

  /* ─── The gate (R4) ────────────────────────────────────────────────────────
   * A physician who lands here is told what is true (this needs admin
   * credentials) rather than shown a console whose every fetch 401s. The
   * distinction between "no session" and "wrong session" matters: the first
   * is a form to fill in, the second is a door that will not open for the
   * account they are holding, and printing a sign-in form at somebody already
   * signed in is how a product teaches people to distrust it. */
  function renderGate(message) {
    const bar = document.getElementById('ascAdminBar');
    if (bar) { clear(bar); bar.setAttribute('hidden', ''); }
    const wrongAccount = !!state.user;
    const card = h('div', { class: 'asc-admin-gate-card' });
    card.appendChild(h('div', { class: 'asc-admin-gate-mark' },
      h('span', { class: 'asc-admin-mark-dot', 'aria-hidden': 'true' }),
      h('span', { class: 'chrome' }, 'Asclepius Operations')));

    if (wrongAccount) {
      card.appendChild(h('h1', { class: 'asc-admin-gate-title' }, 'Admin credentials required'));
      card.appendChild(h('p', { class: 'asc-admin-gate-body' },
        'You are signed in as ' + (state.user.email || 'this account')
        + ', which does not hold operator access. The console is for the '
        + 'Archangel team; your own work lives in the portal.'));
      card.appendChild(h('div', { class: 'asc-admin-gate-actions' },
        h('a', { class: 'asc-btn asc-btn-primary', href: realmPath('/asclepius') }, 'Open the portal'),
        h('button', { class: 'asc-btn asc-btn-ghost', onClick: signOut }, 'Sign in as somebody else')));
      setRoot(h('div', { class: 'asc-admin-gate' }, card));
      return;
    }

    card.appendChild(h('h1', { class: 'asc-admin-gate-title' }, 'Sign in to the console'));
    const errBox = h('div', { class: 'asc-inline-error asc-admin-gate-err', hidden: true });
    if (message) { errBox.textContent = message; errBox.removeAttribute('hidden'); }
    const emailInput = h('input', {
      class: 'asc-input', type: 'email', autocomplete: 'username',
      placeholder: 'you@archangelhealth.ai', 'aria-label': 'Email', required: 'required',
    });
    const pwInput = h('input', {
      class: 'asc-input', type: 'password', autocomplete: 'current-password',
      placeholder: 'Password', 'aria-label': 'Password', required: 'required',
    });
    const submit = h('button', { class: 'asc-btn asc-btn-primary asc-btn-block', type: 'submit' }, 'Sign in');
    const form = h('form', {
      class: 'asc-admin-gate-form',
      onSubmit: async (e) => {
        e.preventDefault();
        errBox.setAttribute('hidden', '');
        submit.setAttribute('disabled', '');
        submit.textContent = 'Signing in…';
        try {
          // noAuthHandler: a bad password is a 401 we show inline, never a
          // global "session expired" bounce back to this same screen.
          const data = await api('/auth/login', {
            method: 'POST', noAuthHandler: true,
            body: { email: emailInput.value.trim(), password: pwInput.value },
          });
          state.token = data.token;
          state.user = data.user;
          localStorage.setItem(TOKEN_KEY, data.token);
          await enterConsole();
        } catch (err) {
          submit.removeAttribute('disabled');
          submit.textContent = 'Sign in';
          errBox.textContent = (err && err.message) || 'Could not sign in.';
          errBox.removeAttribute('hidden');
        }
      },
    }, emailInput, pwInput, submit);
    card.appendChild(errBox);
    card.appendChild(form);
    card.appendChild(h('p', { class: 'asc-admin-gate-foot' },
      h('a', { href: realmPath('/asclepius') }, 'Physician portal')));
    setRoot(h('div', { class: 'asc-admin-gate' }, card));
  }

  function isAdminSession() {
    return !!state.user && ADMIN_ROLES.indexOf(state.user.role) !== -1;
  }

  async function enterConsole() {
    if (!isAdminSession()) { renderGate(); return; }
    // The taxonomy feeds the export-profile and task-source pickers. A 403 on
    // it is not a reason to refuse the console: every other section still
    // works, and those two pickers fall back to their defaults.
    try { state.taxonomy = await api('/taxonomy'); } catch (e) { state.taxonomy = null; }
    renderMasthead();
    renderAdminView();
  }

  async function boot() {
    if (!state.token) { renderGate(); return; }
    try {
      state.user = await api('/auth/me', { noAuthHandler: true });
    } catch (e) {
      state.token = null;
      state.user = null;
      localStorage.removeItem(TOKEN_KEY);
      renderGate();
      return;
    }
    await enterConsole();
  }
  // Legacy tab ids (deep links, stale state) → new section + sub-tab.
  const ADMIN_TAB_ALIASES = {
    tasks: ['work', 'tasks'], qa: ['work', 'qa'],
    metrics: ['work', 'metrics'],
    ingestion: ['data', 'pipeline'], health: ['data', 'systems'],
    export: ['data', 'export'], buyers: ['data', 'export'],
    exports: ['data', 'export'],
  };
  // ─── Specialty resolution for an ingest upload ─────────────────────────────
  //
  // Ingest refuses to guess a specialty — a WRONG one routes the case to the
  // wrong physician pool and mislabels it in the export, invisibly, and neither
  // is visible again once the bundle ships. So an upload that declared none
  // (every hospital-portal upload: the `hs-portal` sentinel has no upload-link
  // row to inherit from) lands on the neutral 'general', and BOTH promote
  // endpoints 409 on it.
  //
  // The resolver endpoint has existed the whole time with zero callers, so the
  // 409 told the admin to use a control that did not exist and the workflow
  // dead-ended. This is that control. It is defined ONCE here and handed to
  // every surface that shows a Promote button, so the two admin screens cannot
  // drift into disagreeing about how a specialty gets set.
  let _specialtyOptions = null;
  let _specialtyOptionsPromise = null;

  function loadSpecialtyOptions() {
    if (_specialtyOptions) return Promise.resolve(_specialtyOptions);
    if (!_specialtyOptionsPromise) {
      _specialtyOptionsPromise = api('/specialties').then((d) => {
        _specialtyOptions = ((d && d.specialties) || [])
          .filter((s) => s && s.enabled && s.specialty)
          .map((s) => s.specialty);
        return _specialtyOptions;
      }).catch(() => {
        // Never cache a failure: a transient blip must not leave the picker
        // permanently empty for the rest of the session.
        _specialtyOptionsPromise = null;
        return [];
      });
    }
    return _specialtyOptionsPromise;
  }

  /** A `<select>` + Set button that resolves an upload's specialty in place.
   *
   *  `onDone(specialty)` fires after the server confirms. The caller re-renders;
   *  this helper owns only the control and its own error line. */
  function specialtyResolver(uploadId, onDone) {
    const wrap = h('span', { class: 'asc-spec-resolver' });
    const sel = h('select', {
      class: 'asc-input asc-spec-select',
      'aria-label': 'Specialty for this upload',
    }, h('option', { value: '' }, 'Loading specialties…'));
    sel.setAttribute('disabled', '');
    const setBtn = h('button', {
      class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button', disabled: true,
    }, 'Set specialty');
    const err = h('span', { class: 'asc-spec-error', hidden: true });

    loadSpecialtyOptions().then((options) => {
      clear(sel);
      if (!options.length) {
        sel.appendChild(h('option', { value: '' }, 'No specialties available'));
        err.textContent = 'Could not load the specialty list. Reload the page.';
        err.removeAttribute('hidden');
        return;
      }
      sel.appendChild(h('option', { value: '' }, 'Choose a specialty…'));
      options.forEach((s) => sel.appendChild(
        h('option', { value: s }, s.charAt(0).toUpperCase() + s.slice(1))));
      sel.removeAttribute('disabled');
    });

    // The button is dead until a real choice is made — an operator cannot submit
    // the placeholder and cannot type a value that is not in the registry.
    sel.addEventListener('change', () => {
      if (sel.value) setBtn.removeAttribute('disabled');
      else setBtn.setAttribute('disabled', '');
    });

    setBtn.addEventListener('click', async () => {
      const chosen = sel.value;
      if (!chosen) return;
      setBtn.setAttribute('disabled', '');
      sel.setAttribute('disabled', '');
      err.setAttribute('hidden', '');
      setBtn.textContent = 'Setting…';
      try {
        const res = await api('/admin/uploads/' + encodeURIComponent(uploadId) + '/specialty',
                              { method: 'POST', body: { specialty: chosen } });
        toast(res.message || ('Specialty set to ' + chosen + '.'), 'success');
        if (onDone) onDone(chosen);
        // The caller re-renders and discards this node, so this is normally
        // invisible. It matters when the re-render fails: without it the row is
        // left with a permanently disabled control under a success toast.
        sel.removeAttribute('disabled');
        setBtn.textContent = 'Set specialty';
      } catch (e) {
        // `detail` is a string for our 400s but a LIST of objects for a FastAPI
        // 422, and assigning that to textContent prints "[object Object]" at the
        // operator. `message` is already normalized by detailToMessage.
        err.textContent = (typeof e.detail === 'string' && e.detail)
          || e.message || 'Could not set the specialty.';
        err.removeAttribute('hidden');
        sel.removeAttribute('disabled');
        setBtn.removeAttribute('disabled');
        setBtn.textContent = 'Set specialty';
      }
    });

    wrap.appendChild(sel);
    wrap.appendChild(setBtn);
    wrap.appendChild(err);
    return wrap;
  }

  // The one sentence an admin reads when Promote is off. Said the same way on
  // every surface, because it is one fact.
  const SPECIALTY_BLOCK_REASON =
    'Specialty not set — choose one to promote. Promoting without it would label '
    + 'these cases with a default that routes them to the wrong physician pool.';

  // Shared helpers handed to the section modules (admin_physicians.js,
  // admin_health.js, admin_export.js): they live in their own files (§3.3
  // ownership) and build DOM exclusively through this ctx.
  /* The five product tabs, plus referrals.

     LABELS ARE DISPLAY-ONLY. The ids are the state keys and they are frozen:
     'work' and 'money' read wrong beside their labels and that is deliberate,
     because ~12 places read them (the alias table above, the subnav lookups,
     the section routing, openBatchesFor, the physician-row route-in).

     Referrals is the sixth. The PRD folded it into Money and Metrics; the
     founder meeting listed it as a tab of its own, and there is a concrete
     reason to believe the meeting: half the referral funnel has no screen at
     all. The health-system introduction endpoints (list, advance, reward)
     shipped with no client, so three of the six funnel stages were
     unreachable and every recorded reward was invisible. That is a surface,
     not a sub-tab of the payout ledger. */
  const ADMIN_TABS = [
    ['physicians', 'Physicians'],
    ['work', 'Tasks'],                // key stays 'work'
    ['money', 'Money and Metrics'],   // key stays 'money'
    ['data', 'Data'],
    ['community', 'Community'],
    ['referrals', 'Referrals'],
  ];
  // Sandbox PRD §3: the sandbox admin console is the SAME console with one
  // more section — Accounts (the ten doctors + credentials, Reset, Seed fresh
  // doctor, snapshot copy) and Outbox (every email the sandbox "sent"). It
  // exists only when this page IS the sandbox; the live console never shows it.
  if (REALM === 'sandbox') ADMIN_TABS.push(['sandbox', 'Sandbox']);

  // Shared helpers handed to the section modules (admin_physicians.js,
  // admin_health.js, admin_export.js, admin_earnings.js, admin_referrals.js,
  // admin_community.js): they live in their own files and build DOM
  // exclusively through this ctx.
  function adminSectionCtx() {
    return {
      h, api, clear, toast, loadingCard, downloadBlob, fmtDate,
      specialtyResolver, specialtyBlockReason: SPECIALTY_BLOCK_REASON,
      // One id renderer for every admin section (PRD §1.3).
      copyableId, copyTextToClipboard,
      // The export history list and the contributor/credential browser both
      // used to live inside the Export tab's own `history` sub-view. That view
      // is gone (PRD §2); they render under the builder instead, so the section
      // module needs a handle on them.
      exportHistory: loadExportsHistory,
      contributorBrowser: (card) => {
        state.browse.export = { level: 'orgs', org: null, idHashed: null, contributor: null };
        renderOrgContribBrowser(card, 'export');
      },
      avatarBlob: loadAvatarBlob,
      // Jump to the pipeline tools (the ingestion review/promote surface),
      // deep-linked to the row that was clicked.
      openPipeline: (entry) => {
        state.adminTab = 'data'; state.adminSub.data = 'pipeline';
        state.pipelineFocus = (entry && (entry.upload_id || entry.uploadId)) || null;
        renderAdminView();
      },
      // Route cases to one physician, entered from their row. The same flow as
      // picking them in the send bar, not a second one: this only pre-selects
      // them in Routing, so there is one place where "who gets what" is decided
      // and one place it can be got wrong.
      openBatchesFor: (physician) => {
        state.adminTab = 'work'; state.adminSub.work = 'assign';
        // The FULL view shape, not a subset. This literal predates the relay
        // and per-doctor-role fields and was never extended with them, so
        // entering Routing from a physician's row and then ticking a case
        // threw on `view.roles[d.id]` and painted nothing: the one route-in
        // this cross-link exists for, dead at the second click. Anything added
        // to the initializer in renderAdminBatches belongs here too.
        state.batches = {
          overview: null, batch: null, rows: null, selected: {}, busy: false,
          err: null, mode: 'explicit', userIds: [physician && physician.id].filter(Boolean),
          specialty: '', doctors: physician ? [physician] : null, proposal: null,
          resolved: null, relay: false, relayWalk: null, relaySeed: null,
          relayPreview: null, chain: null, roles: {},
        };
        renderAdminView();
      },
      // The section owns its views; the shell owns which tab looks selected, so
      // a jump has to come back through here or the two disagree.
      openPhysiciansSub: (sub) => {
        state.adminTab = 'physicians'; state.adminSub.physicians = sub;
        renderAdminView();
      },
    };
  }

  function renderAdminView() {
    const alias = ADMIN_TAB_ALIASES[state.adminTab];
    if (alias) { state.adminTab = alias[0]; state.adminSub[alias[0]] = alias[1]; }
    // The pre-PRD-F deep link into the referral book was Money > Referrals.
    // The book has its own tab now, so that route-in is normalised here rather
    // than by deleting the sub-key, which every stored link still carries.
    if (state.adminTab === 'money' && state.adminSub.money === 'referrals') {
      state.adminTab = 'referrals'; state.adminSub.referrals = 'people';
    }
    renderMasthead();
    const body = h('div', { id: 'ascAdminBody' });
    // Above everything, on every admin tab (see refreshStorageBanner).
    const storageBanner = h('div', { id: 'ascStorageBanner' });
    setRoot(h('div', { class: 'asc-wrap' }, storageBanner, body));
    refreshQaBadge();
    refreshStorageBanner();

    if (state.adminTab === 'physicians') renderAdminPhysiciansSection(body);
    else if (state.adminTab === 'work') renderAdminWorkSection(body);
    else if (state.adminTab === 'money') renderAdminMoneySection(body);
    else if (state.adminTab === 'data') renderAdminDataSection(body);
    else if (state.adminTab === 'community') renderAdminCommunitySection(body);
    else if (state.adminTab === 'referrals') renderAdminReferralsSection(body);
    else if (state.adminTab === 'sandbox' && REALM === 'sandbox') renderAdminSandboxSection(body);
  }

  // Sandbox (Sandbox PRD §3): Accounts and Outbox. Rendered by its own module
  // (admin_sandbox.js) like every other section; the shell only routes to it.
  function renderAdminSandboxSection(body) {
    clear(body);
    body.appendChild(adminSubnav('sandbox', [
      ['accounts', 'Accounts'], ['outbox', 'Outbox'],
    ]));
    const inner = h('div', {});
    body.appendChild(inner);
    if (window.AdminSandboxSection) {
      window.AdminSandboxSection.render(inner, adminSectionCtx(),
        state.adminSub.sandbox === 'outbox' ? 'outbox' : 'accounts');
    } else sectionModuleMissing(inner, 'The Sandbox section');
  }

  /* ═══ Is this deployment going to keep the data? ══════════════════════════
   *
   * If the volume is not attached, NOTHING ELSE ON THIS CONSOLE MATTERS. Every
   * physician account, task, submission, review and payout row is erased on the
   * next redeploy, and the screens go quiet rather than erroring, which reads
   * as "nobody signed up", not as "the database was deleted". That is the
   * failure this banner exists to make impossible to miss.
   *
   * The server has checked this at boot for a long time and said so in a log
   * line. A log line is only read by someone who already suspects a problem, so
   * it is exactly the wrong medium for a failure whose whole signature is that
   * nobody suspects anything. This puts the same verdict where an operator
   * already is, on every admin tab, and says what to change.
   *
   * Two states are worth shouting about:
   *   NOT DURABLE   - data is being destroyed on each deploy, right now.
   *   GATE UNARMED  - durable today, but ENV is not 'production', so the app
   *                   will happily boot onto ephemeral storage after any future
   *                   variable change. Today's green is luck, not a guarantee.
   * Fully durable AND armed renders nothing: a banner that is always there is a
   * banner nobody reads. */
  async function refreshStorageBanner() {
    const host = document.getElementById('ascStorageBanner');
    if (!host) return;
    let s;
    try { s = await api('/admin/storage/durability'); } catch (e) { return; }
    if (!host.isConnected && !document.getElementById('ascStorageBanner')) return;
    clear(host);
    if (s.all_durable && s.gate_armed) return;

    const broken = (s.stores || []).filter((x) => !x.durable);
    const critical = broken.filter((x) => x.severity !== 'recoverable');
    if (critical.length) {
      host.appendChild(h('div', { class: 'asc-card', style: 'margin-bottom:14px' },
        h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-inline-error' },
            'THIS DEPLOYMENT IS DESTROYING ITS DATA ON EVERY REDEPLOY. '
            + critical.length + ' of ' + (s.stores || []).length
            + ' stores are not on a persistent volume: every account, task, '
            + 'submission and payout row written since the last deploy is lost '
            + 'at the next one, silently.'),
          h('ul', {}, broken.map((x) => h('li', {},
            h('strong', {}, x.store), ' \u2014 ', x.detail))),
          s.remedy ? h('div', { class: 'asc-card-sub', style: 'margin-top:8px' },
            s.remedy) : null)));
      return;
    }
    // Nothing irreplaceable is at risk. Two lesser problems still get said,
    // separately, because conflating "your database is being deleted" with
    // "re-cut that bundle" is how an operator learns to ignore both.
    const notes = [];
    broken.forEach((x) => notes.push(x.detail));
    if (!s.gate_armed) {
      notes.push('The fail-closed boot gate is OFF (ENV is not "production"), '
        + 'so nothing would stop this app from booting onto ephemeral storage '
        + 'after a future variable change: it would accept data and destroy '
        + 'it, with no warning. Set ENV=production and it refuses to start '
        + 'instead.');
    }
    if (!notes.length) return;
    host.appendChild(h('div', { class: 'asc-card', style: 'margin-bottom:14px' },
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-warn' },
          'Nothing irreplaceable is at risk, but this is not fully safe yet.'),
        h('ul', {}, notes.map((n) => h('li', {}, n))))));
  }

  // Sub-tab strip shared by the sections that have more than one page.
  function adminSubnav(section, items) {
    return h('div', { class: 'asc-subnav', style: 'margin-bottom:14px' },
      items.map(([id, label]) => h('button', {
        class: 'asc-subnav-btn' + (state.adminSub[section] === id ? ' active' : ''),
        onClick: () => { state.adminSub[section] = id; renderAdminView(); },
      }, label)));
  }
  // Physicians: who supplies our judgment. Tasks and QA live here now, next to
  // the people who produce them.
  function renderAdminPhysiciansSection(body) {
    clear(body);
    // §1.2 — subnav deleted. Approved / Pending is an internal tab strip owned
    // by AdminPhysiciansSection: the operator's job here is one decision loop,
    // not four screens. Signups fold into Pending (a mid-wizard physician is a
    // pending physician who cannot be decided yet) and QA moved under Tasks,
    // next to the work it grades.
    const inner = h('div', {});
    body.appendChild(inner);
    if (window.AdminPhysiciansSection) window.AdminPhysiciansSection.render(inner, adminSectionCtx());
    else sectionModuleMissing(inner, 'The Physicians section');
  }

  // Work: what gets labeled and how it is going. Task import/generation
  // beside the metrics that report on it.
  function renderAdminWorkSection(body) {
    clear(body);
    // §1.3 — QA is NOT deleted. Removing it orphans POST /qa/approve-all and the
    // submission queue, both live. It lands here, beside the work it grades.
    // §2 — the two pages are named for the two questions an operator actually
    // asks, in the order the work moves. The STATE KEYS stay 'tasks' and
    // 'assign': they are read by deep-link aliases, openBatchesFor, and the
    // physician-row route-in, and renaming them is silent breakage for zero
    // benefit — the same reasoning that kept 'work' and 'money' at §1.1.
    body.appendChild(adminSubnav('work', [
      ['tasks', 'Data & Task Creation'], ['assign', 'Task Routing'],
      ['qa', 'QA'], ['metrics', 'Metrics'],
    ]));
    const inner = h('div', {});
    body.appendChild(inner);
    if (state.adminSub.work === 'qa') renderAdminQA(inner);
    else if (state.adminSub.work === 'assign') renderAdminBatches(inner);
    else if (state.adminSub.work === 'metrics') renderAdminMetrics(inner);
    else renderAdminTasks(inner);
  }

  /* Batches: the three case classes, previewed and sent.
   *
   * Before this, routing meant pasting task ids into a textarea — which works
   * only if you already know the ids, and there is nowhere in the product that
   * tells you them for a chart walk. So the surface starts from what an admin
   * actually has: three classes of case, counted.
   *
   * The longitudinal class is the one with a rule attached. A chart walk is
   * ordered, and the server refuses a selection that skips earlier points (it
   * re-derives the required set — the client is not trusted with sequence, and a
   * test asserts this file contains no sequence comparison). So the UI's job is
   * to make the implied set VISIBLE before sending, not to compute authority
   * over it: selecting point 5 shows "+5 earlier points included" and sends them,
   * and if this file ever gets that arithmetic wrong the server still refuses.
   */
  const BATCH_META = {
    longitudinal: { title: 'LONGITUDINAL V4', accent: 'purple' },
    real_static: { title: 'REAL · STATIC V4', accent: 'green' },
    synthetic: { title: 'SYNTHETIC V3', accent: 'orange' },
  };

  function renderAdminBatches(body) {
    clear(body);
    /* The shape of this view, in ONE place.
     *
     * ``state.batches`` is not always built here. ``openBatchesFor(physician)``
     * ("route cases to this doctor", entered from their row in Physicians)
     * constructs a PARTIAL object and then hands control to this function, which
     * finds ``state.batches`` already truthy and keeps it as-is. Every key that
     * caller omits is therefore ``undefined`` at read time, and the two that are
     * read as maps (``view.roles[id]``, ``view.previewed[id]``) throw a
     * TypeError that takes the whole send panel down.
     *
     * So the defaults are named once and BACKFILLED onto whatever arrives,
     * rather than only used when the object is absent. Existing values win, so a
     * pre-selected physician still survives the trip; missing ones stop being a
     * crash. Adding a key here now covers both entry points by construction.
     */
    const BATCH_VIEW_DEFAULTS = {
      overview: null, batch: null, rows: null, selected: {}, busy: false,
      err: null, mode: 'all', userIds: [], specialty: '', doctors: null, proposal: null,
      resolved: null, relay: false, relayWalk: null, relaySeed: null,
      relayPreview: null, chain: null,
      // §4.3: {user_id: 'label'|'review'}. Sparse: a doctor absent from this
      // map is a labeler, matching the server's default for the same field.
      roles: {},
      // Longitudinal E2E §3: {task_id: true} for cases a human has actually
      // opened in THIS session. Session-scoped on purpose: "somebody previewed
      // this in March" is not the assurance the gate is asking for. Never
      // persisted, and never read by the server. The server's own gates
      // (distribution, sequence, capacity) are the enforcement; this is the one
      // thing they cannot check, which is whether a person looked.
      previewed: {},
    };
    const view = state.batches || (state.batches = {});
    Object.keys(BATCH_VIEW_DEFAULTS).forEach(function (k) {
      if (view[k] === undefined) view[k] = BATCH_VIEW_DEFAULTS[k];
    });
    const host = h('div', {});
    body.appendChild(host);

    function selectedIds() { return Object.keys(view.selected).filter((k) => view.selected[k]); }

    /* The implied set comes from the SERVER, and this file contains no
     * comparison of sequence indices at all.
     *
     * The screen still needs to say "3 selected, +5 earlier points included"
     * before an operator commits, and the obvious way to get that number is a
     * loop over sequence_index right here. That is exactly what this product
     * does not allow anywhere, for a reason that outlives this screen: a client
     * that knows how to order a walk is a client somebody will later trust to
     * enforce the order, and the seal would then be one hand-typed task id away
     * from being defeated. A test asserts this file contains no such comparison,
     * and it should keep passing for the admin surface as firmly as for the
     * doctor's.
     *
     * So: one cheap call on selection change, answered by the same function
     * ``allocate`` refuses with — the count shown and the set committed cannot
     * disagree, because they are the same derivation. */
    function resolveSelection() {
      const chosen = selectedIds();
      if (!chosen.length) { view.resolved = null; return Promise.resolve(null); }
      return api('/admin/batches/resolve-selection',
                 { method: 'POST', body: { task_ids: chosen } })
        .then((res) => { view.resolved = res; return res; })
        .catch(() => { view.resolved = null; return null; });
    }

    function load() {
      view.busy = true; paint();
      api('/admin/batches').then((res) => {
        view.overview = res; view.busy = false; paint();
      }).catch((e) => { view.err = e.message; view.busy = false; paint(); });
    }

    function openBatch(key, trajectoryId) {
      view.batch = key; view.rows = null; view.selected = {}; view.proposal = null;
      view.busy = true; paint();
      api('/admin/batches/' + encodeURIComponent(key)).then((res) => {
        view.rows = res.cases || []; view.busy = false;
        view.chain = null;
        // A walk that has already been sent gets its chain loaded, so a stalled
        // one is visible on the screen an admin is already looking at rather
        // than only to somebody who thinks to go looking for it.
        if (key === 'longitudinal' && trajectoryId) loadChain(trajectoryId);
        else paint();
      }).catch((e) => { view.err = e.message; view.busy = false; paint(); });
    }

    function loadChain(trajectoryId) {
      api('/admin/batches/relay/' + encodeURIComponent(trajectoryId))
        .then((c) => { view.chain = c; paint(); })
        .catch(() => paint());               // an unsent walk has no chain yet
    }

    function loadDoctors() {
      if (view.doctors) return Promise.resolve(view.doctors);
      return api('/admin/physicians').then((res) => {
        view.doctors = (res.physicians || res.rows || []).filter((d) => d.real_data_approved);
        return view.doctors;
      }).catch(() => { view.doctors = []; return view.doctors; });
    }

    function send(dryRun) {
      /* The resolved set when we have one, the raw selection otherwise. Falling
       * back is safe: the server re-derives and refuses a payload missing a
       * predecessor, naming what to add, so the worst case is a message rather
       * than a stranded assignment. */
      const ids = (view.resolved && view.resolved.task_ids) || selectedIds();
      if (!ids.length) return;
      if (view.relay && view.relayWalk) { sendRelay(dryRun); return; }
      const payload = { task_ids: ids, dry_run: dryRun, labels_per_case: 1 };
      if (view.mode === 'all') payload.to_all = true;
      else if (view.mode === 'specialty') payload.specialty = view.specialty;
      else {
        // An explicit send with nobody named would post an empty user_ids, which
        // the allocator reads as "no targeting" and answers by picking doctors
        // itself — the admin's screen says "these people" and the server hears
        // "anyone". Refuse it here, where the operator can still fix it.
        if (!view.userIds.length) {
          view.err = 'Pick at least one doctor, or choose a different mode.';
          view.busy = false; paint(); return;
        }
        payload.user_ids = view.userIds;
        // Only for the named doctors, and only when a role was actually chosen.
        // Sending roles for people not in ``user_ids`` would be a payload the
        // server has no use for and a screen state nobody can see.
        const roles = {};
        view.userIds.forEach((id) => { if (view.roles[id]) roles[id] = view.roles[id]; });
        if (Object.keys(roles).length) payload.roles = roles;
      }

      view.busy = true; view.err = null; paint();
      api('/admin/assignments/allocate', { method: 'POST', body: payload })
        .then((res) => {
          view.proposal = res; view.busy = false;
          if (!dryRun) {
            toast(res.targeting === 'all'
              ? `${ids.length} case(s) released to the open queue.`
              : `Sent ${ids.length} case(s) to ${Object.keys(res.per_physician || {}).length} doctor(s).`);
            view.selected = {};
            openBatch(view.batch);
            return;
          }
          paint();
        })
        .catch((e) => {
          view.busy = false;
          /* The server names the points a selection is missing. Saying "400" here
           * and making the admin diff two lists by hand would be the product
           * knowing something and not saying it. */
          const d = e && e.detail;
          if (d && d.error === 'missing_trajectory_predecessors') {
            view.err = 'That selection skips earlier points in a chart walk: '
              + Object.keys(d.missing).map((k) => '#' + d.missing[k].join(', #')).join(' · ')
              + '. A walk must be sent from its first unanswered point onward.';
          } else if (d && d.error === 'not_a_reviewer') {
            view.err = 'Not a reviewer: ' + (d.emails || []).join(', ')
              + '. The review queue gates on the reviewer tier, so that '
              + 'assignment could never be served. Grant the tier on Physicians, '
              + 'or send them the case to label.';
          } else if (d && d.error === 'not_approved_for_real_data') {
            view.err = 'Not approved for real de-identified cases: '
              + (d.emails || []).join(', ') + '. Approve them first, or the '
              + 'assignment could never be served.';
          } else {
            view.err = (e && e.message) || 'Send failed.';
          }
          paint();
        });
    }

    /* Rendered INLINE rather than in a modal, deliberately. An admin deciding
     * who walks a chart opens point 0, then point 3, then point 0 again; a
     * dialog that has to be dismissed between each turns comparison into
     * clicking. It also keeps the table's selection state on screen. */
    function preview(taskId) {
      view.previewFor = taskId;
      api('/admin/batches/preview/' + encodeURIComponent(taskId)).then((res) => {
        if (view.previewFor !== taskId) return;   // a later click won
        view.preview = res;
        /* §3: remember, for this session, that a human has actually LOOKED at
         * this case. Auto-generation removes the click that used to force an
         * admin past a preview on the way to creating tasks; without this, the
         * first time anyone sees a machine-built case could be after a physician
         * has already been sent it. Recorded on success only: a preview that
         * errored showed nobody anything. */
        view.previewed[taskId] = true;
        paint();
      }).catch((e) => toast('Could not load preview: ' + e.message, 'error'));
    }

    /* The relay send. A separate endpoint, not a flag on allocate, because it
     * commits a different thing: a rotation (which doctor takes which point),
     * plus walk_mode on every point. The seed is fixed from the preview so the
     * mapping the admin was SHOWN is the one that commits — otherwise preview and
     * commit are two draws from the same distribution and the screen is a lie
     * they cannot detect. */
    function sendRelay(dryRun) {
      if (!view.userIds.length) {
        view.err = 'Pick the doctors for the relay first.'; paint(); return;
      }
      if (view.relaySeed == null) view.relaySeed = Math.floor(Math.random() * 1e9);
      view.busy = true; view.err = null; paint();
      api('/admin/batches/relay', { method: 'POST', body: {
        trajectory_id: view.relayWalk, user_ids: view.userIds,
        dry_run: dryRun, seed: view.relaySeed,
      } }).then((res) => {
        view.busy = false;
        if (dryRun) { view.relayPreview = res; paint(); return; }
        toast(`Relay sent — ${res.n_points} point(s) across ${res.n_doctors} doctors.`);
        view.relayPreview = null; view.relaySeed = null; view.selected = {};
        openBatch(view.batch);
      }).catch((e) => {
        view.busy = false;
        const d = e && e.detail;
        view.err = (d && d.message) || (e && e.message) || 'Relay send failed.';
        paint();
      });
    }

    function reassign(trajectoryId, point) {
      loadDoctors().then((docs) => {
        const pick = (docs || []).filter((d) => d.id !== point.user_id);
        if (!pick.length) { toast('No other approved doctor to hand it to.', 'error'); return; }
        const sel = h('select', { class: 'asc-input' },
          pick.map((d) => h('option', { value: d.id }, d.name || d.email)));
        const go = h('button', { class: 'asc-btn asc-btn-primary', type: 'button' }, 'Reassign');
        const box = h('div', { class: 'asc-card asc-card-pad' },
          h('div', {}, 'Hand point #' + point.sequence_index + ' to:'), sel, go);
        go.addEventListener('click', () => {
          api('/admin/batches/relay/' + encodeURIComponent(trajectoryId) + '/reassign',
              { method: 'POST', body: { task_id: point.task_id, user_id: sel.value } })
            .then((res) => { view.chain = res.chain; toast('Reassigned.'); paint(); })
            .catch((e) => toast('Could not reassign: ' + e.message, 'error'));
        });
        host.appendChild(box);
      });
    }

    /* ═══ PRD ADMIN-TASKS §4 — three columns, always all three ══════════════
     *
     * The old shape was two LEVELS: pick a batch, then a table with a send bar
     * that appeared under it. That answers "what is in this batch" but not the
     * question an admin actually arrives with, which is one question with three
     * parts — what is ready, what does it look like, who gets it — and levels
     * make you navigate between the parts of a single decision.
     *
     * So: rail (what is ready) · list (what it is, previewable) · panel (who).
     * The panel is CONTEXT-SENSITIVE and that is the whole point of the re-cut.
     * A static selection and a chart walk are routed by different rules, and the
     * old bar showed one set of controls for both — relay toggles next to
     * synthetic cases they cannot apply to. It now shows only what the selection
     * admits, and a one-line hint when nothing is selected.
     *
     * Everything the server owns, the server still owns. The implied predecessor
     * set is resolved by /resolve-selection, the send goes through allocate, and
     * this file still contains no comparison of sequence indices anywhere.
     */
    function paint() {
      clear(host);
      if (view.err) host.appendChild(h('div', { class: 'asc-inline-error' }, view.err));
      if (view.busy && !view.overview) { host.appendChild(loadingCard('Loading batches…')); return; }

      host.appendChild(h('div', { class: 'asc-route-grid' },
        paintRail(), paintCentre(), paintPanel()));
    }

    // ─── Left rail: the three classes, counted ──────────────────────────────
    function paintRail() {
      const ov = view.overview || {};
      const lg = ov.longitudinal || {};
      const rail = h('div', { class: 'asc-route-rail' });
      rail.appendChild(railBtn('longitudinal',
        `${lg.n_trajectories || 0} trajector${(lg.n_trajectories === 1) ? 'y' : 'ies'} · ${lg.n_points || 0} points`,
        `${lg.n_unrouted || 0} unrouted`));
      rail.appendChild(railBtn('real_static',
        `${(ov.real_static || {}).n_cases || 0} cases`,
        `${(ov.real_static || {}).n_open || 0} in open queue`));
      rail.appendChild(railBtn('synthetic',
        `${(ov.synthetic || {}).n_cases || 0} cases`,
        `${(ov.synthetic || {}).n_open || 0} in open queue`));
      rail.appendChild(h('div', { class: 'asc-dim', style: 'margin-top:6px' },
        'Longitudinal cases are held back from every doctor’s queue until you '
        + 'send them. That is the resting state, not a fault.'));
      return rail;
    }

    function railBtn(key, line1, line2) {
      const meta = BATCH_META[key];
      const b = h('button', {
        class: 'asc-route-rail-btn' + (view.batch === key ? ' active' : ''),
        type: 'button',
      },
        h('div', { class: 'asc-route-rail-title' }, meta.title),
        h('div', { class: 'asc-route-rail-count' }, line1),
        h('div', { class: 'asc-dim' }, line2));
      b.addEventListener('click', () => openBatch(key));
      return b;
    }

    // ─── Centre: the task list, every row previewable ───────────────────────
    function paintCentre() {
      const col = h('div', {});
      if (!view.batch) {
        col.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-dim' }, 'Pick a batch to see its cases.'))));
        return col;
      }
      if (view.busy && !view.rows) { col.appendChild(loadingCard('Loading cases…')); return col; }
      const rows = view.rows || [];
      const table = h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, ''), h('th', {}, 'Case'), h('th', {}, 'Specialty'),
          h('th', {}, 'Difficulty'), h('th', {}, 'Status'), h('th', {}, ''))),
        h('tbody', {}, rows.map(rowFor)));
      col.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-pad' },
          h('h3', {}, BATCH_META[view.batch].title),
          rows.length
            ? h('div', { class: 'asc-table-wrap' }, table)
            : h('div', { class: 'asc-empty' }, h('p', {}, 'Nothing in this batch yet.')))));
      paintPreviewInto(col);
      paintChainInto(col);
      return col;
    }

    function statusLabel(r) {
      if (r.assigned_to) return 'routed → ' + r.assigned_to;
      if (r.distribution === 'assigned_only') return 'unrouted';
      if (r.label_count > 0) return `labeled ${r.label_count}/${r.max_labels || 1}`;
      return 'in open queue';
    }

    /* A task made in the last day carries a `new` chip, so what you just built
     * on the other page is findable here without sorting or searching. Read off
     * created_at, which the batch query already returns. */
    function isFresh(r) {
      if (!r.created_at) return false;
      // toUtcDate, not Date.parse: the server writes a bare
      // 'YYYY-MM-DDTHH:MM:SS' with no zone, which Date.parse reads as LOCAL.
      // In UTC+14 that shifts a 30-hour-old task inside the 24-hour window and
      // chips it "new", which is a lie on the one screen an operator uses to
      // find what they just made.
      const dt = toUtcDate(r.created_at);
      if (!dt || isNaN(dt.getTime())) return false;
      return (Date.now() - dt.getTime()) < 86400000;
    }

    function rowFor(r) {
      const cb = h('input', { type: 'checkbox' });
      cb.checked = !!view.selected[r.task_id];
      cb.addEventListener('change', () => {
        view.selected[r.task_id] = cb.checked;
        if (!cb.checked) delete view.selected[r.task_id];
        paint();                                 // immediate, with the old count
        resolveSelection().then(paint);          // then the authoritative one
      });
      const prev = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' }, 'Preview');
      prev.addEventListener('click', () => preview(r.task_id));
      // The case id, copyable (PRD §1.3): Task Routing is where an operator
      // first SEES a case id, and the Export tab is where they have to type it.
      const label = (r.trajectory_id && r.sequence_index != null)
        ? h('span', {}, h('span', { class: 'asc-mono' }, '#' + r.sequence_index), ' ',
            copyableId(r.task_id))
        : copyableId(r.task_id);
      // Gold sits in the SYNTHETIC class — its case_source is not real_deid, and
      // that is what batch_overview counts on. The chip is how it stays visible
      // as physician-authored without inventing a fourth rail item that would
      // then disagree with the backend's three.
      const chips = h('span', {},
        r.display_bucket === 'physician_authored'
          ? h('span', { class: 'asc-chip', title: 'Hand-authored and clinician-ratified' }, 'physician-authored')
          : null,
        isFresh(r) ? h('span', { class: 'asc-chip asc-chip-new' }, 'new') : null);
      return h('tr', {},
        h('td', {}, cb), h('td', {}, label, ' ', chips), h('td', {}, r.specialty || '—'),
        h('td', {}, r.difficulty || '—'), h('td', {}, statusLabel(r)), h('td', {}, prev));
    }

    function paintPreviewInto(col) {
      const res = view.preview;
      if (!res) return;
      const close = h('button', { class: 'asc-btn asc-btn-ghost', type: 'button' }, 'Close preview');
      close.addEventListener('click', () => { view.preview = null; view.previewFor = null; paint(); });
      // The two-frontier provenance and the re-grade lever, kept from the old
      // Tasks table. They belong beside the case they describe rather than in a
      // column of a table that no longer exists — and dropping them would have
      // quietly removed the only way to see a HELD "needs baseline" task.
      const t = res.task || {};
      col.appendChild(h('div', { class: 'asc-card asc-preview-card' },
        h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-eyebrow' }, res.eyebrow),
          res.trajectory
            ? h('div', { class: 'asc-dim' },
                `Decision point ${res.trajectory.position} of ${res.trajectory.n_points} · `
                + 'the chart is truncated here exactly as the physician sees it')
            : null,
          h('div', { class: 'asc-prompt-label' }, 'Clinical question'),
          h('div', { class: 'asc-prompt-text' }, res.prompt || ''),
          renderCasePanelReadOnly(res.task),
          t.task_id ? baselineCell(t) : null,
          close)));
    }

    function paintChainInto(col) {
      const c = view.chain;
      if (!c) return;
      const dot = { done: '✓', waiting: '●', later: '–', retired: '×' };
      const cells = (c.points || []).map((p) => {
        const late = p.state === 'waiting' && (p.waiting_hours || 0) >= 24;
        const kids = [h('span', { class: 'asc-chain-mark' }, dot[p.state] || '–'),
          h('span', {}, '#' + p.sequence_index)];
        if (p.state === 'waiting') {
          kids.push(h('span', { class: 'asc-dim' },
            ' waiting ' + (p.waiting_hours == null ? '?' : Math.round(p.waiting_hours)) + 'h'));
          const re = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
            'Reassign');
          re.addEventListener('click', () => reassign(c.trajectory_id, p));
          kids.push(re);
        }
        return h('span', { class: 'asc-chain-cell' + (late ? ' is-late' : '') }, ...kids);
      });
      col.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('h3', {}, 'Chain · ' + (c.walk_mode || 'solo')),
        h('div', { class: 'asc-dim' }, c.n_done + ' of ' + c.n_points + ' done'),
        h('div', { class: 'asc-chain' }, ...cells))));
    }

    // ─── Right panel: who gets it, shaped by what is selected ───────────────
    function paintPanel() {
      const panel = h('div', { class: 'asc-route-panel' });
      const chosen = selectedIds();
      if (!chosen.length) {
        panel.appendChild(h('div', { class: 'asc-route-panel-hint' },
          'Select cases to route them. The controls here follow the selection — '
          + 'a chart walk and a set of standalone cases are sent by different '
          + 'rules, so they are never offered the same ones.'));
        return panel;
      }

      const extra = (view.resolved && view.resolved.n_added) || 0;

      /* Whole-walk detection, unchanged: a relay is defined over a walk, so it
       * is offered only when the selection IS one. This counts membership; it
       * does not order anything, and the server re-derives the required set. */
      const walkIds = {};
      (view.rows || []).forEach(function (r) {
        if (r.trajectory_id) walkIds[r.trajectory_id] = (walkIds[r.trajectory_id] || 0) + 1;
      });
      const chosenWalks = {};
      chosen.forEach(function (id) {
        const row = (view.rows || []).find(function (r) { return r.task_id === id; });
        if (row && row.trajectory_id) {
          chosenWalks[row.trajectory_id] = (chosenWalks[row.trajectory_id] || 0) + 1;
        }
      });
      const walkKeys = Object.keys(chosenWalks);
      const wholeWalk = (walkKeys.length === 1
        && chosenWalks[walkKeys[0]] === walkIds[walkKeys[0]]
        && chosen.length === chosenWalks[walkKeys[0]]) ? walkKeys[0] : null;
      view.relayWalk = wholeWalk;
      if (!wholeWalk) view.relay = false;

      panel.appendChild(h('div', { style: 'font-weight:600;margin-bottom:8px' },
        wholeWalk
          ? ('Send trajectory · ' + chosen.length + ' point(s)')
          : ('Send ' + chosen.length + ' case(s)')));
      if (extra > 0) {
        panel.appendChild(h('div', { class: 'asc-dim', style: 'margin-bottom:8px' },
          `(+${extra} required earlier point(s) included)`));
      }

      if (wholeWalk) panel.appendChild(walkControls());
      else panel.appendChild(flatControls());

      const dry = h('button', { class: 'asc-btn', type: 'button' }, 'Preview send');
      dry.addEventListener('click', () => send(true));
      /* §3: Send is disabled until a human has opened at least one case in this
       * selection this session. "Preview send" shows the ROUTING (who gets what);
       * this asks whether anyone has read the CASE. That distinction stopped
       * being academic when generation became unattended: the whole batch can now
       * reach the routing screen without a person having read one line of it.
       *
       * At least one, not all: an admin sending a thirteen-point walk should not
       * have to open thirteen cases, and reading point 0 of a chart tells them
       * whether the chart is right. */
      const seen = chosen.some(function (id) { return view.previewed[id]; });
      const go = h('button', { class: 'asc-btn asc-btn-primary', type: 'button',
                               disabled: seen ? null : '' }, 'Send');
      go.addEventListener('click', () => { if (seen) send(false); });
      panel.appendChild(h('div', { class: 'asc-stage-actions' }, dry, go));
      if (!seen) {
        panel.appendChild(h('div', { class: 'asc-dim' },
          'Open one of these cases first. Nothing goes to a physician that nobody '
          + 'here has read, and a bundle that built itself may never have been.'));
      }

      if (view.proposal && view.proposal.dry_run) {
        const per = view.proposal.per_physician || {};
        panel.appendChild(h('div', { style: 'margin-top:12px' },
          h('div', { style: 'font-weight:600' }, 'Proposed send'),
          h('div', { class: 'asc-dim' },
            `${view.proposal.cases} case(s) · ${Object.keys(per).length} doctor(s)`),
          (view.proposal.notes || []).map((n) => h('div', { class: 'asc-dim' }, n))));
      }
      if (view.relayPreview) panel.appendChild(relayPreviewBlock());
      return panel;
    }

    /* Static and synthetic: three ways to choose who, and a per-doctor ROLE.
     * The role was always in ``assignments.role`` and never in this screen, so
     * an admin who wanted a reviewer got a labeler and nothing said so. */
    function flatControls() {
      const box = h('div', {});
      const modeSel = h('select', { class: 'asc-input' },
        h('option', { value: 'all' }, 'All approved doctors'),
        h('option', { value: 'specialty' }, 'Specialty'),
        h('option', { value: 'explicit' }, 'Specific doctors'));
      modeSel.value = view.mode;
      modeSel.addEventListener('change', () => {
        view.mode = modeSel.value;
        if (view.mode === 'explicit') loadDoctors().then(paint);
        else paint();
      });
      box.appendChild(h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'To'), modeSel));

      if (view.mode === 'specialty') {
        const inp = h('input', { class: 'asc-input', placeholder: 'e.g. hepatology' });
        inp.value = view.specialty;
        inp.addEventListener('input', () => { view.specialty = inp.value.trim().toLowerCase(); });
        box.appendChild(inp);
      } else if (view.mode === 'explicit') {
        box.appendChild(doctorPicker());
      }

      /* Send to All on a longitudinal batch UN-SEALS it — the cases leave
       * assigned_only and any eligible doctor may draw them. That is a real,
       * deliberate choice and it is stated before the click, not discovered
       * after it. */
      if (view.mode === 'all' && view.batch === 'longitudinal') {
        box.appendChild(h('div', { class: 'asc-inline-warn' },
          'Longitudinal cases sent to All enter the open queue — any eligible '
          + 'doctor may draw them, in sequence order.'));
      }
      return box;
    }

    /* One checkbox + two role radios per doctor. The role rides in ``roles`` on
     * the allocate payload; a name absent from that map is a labeler, which is
     * what every explicit send meant before the field existed. */
    function doctorPicker(opts) {
      const withRoles = !opts || opts.roles !== false;
      const list = h('div', {});
      (view.doctors || []).forEach((d) => {
        const on = view.userIds.indexOf(d.id) !== -1;
        const cb = h('input', { type: 'checkbox', checked: on });
        cb.addEventListener('change', () => {
          if (cb.checked) { if (view.userIds.indexOf(d.id) === -1) view.userIds.push(d.id); }
          else {
            view.userIds = view.userIds.filter((x) => x !== d.id);
            delete view.roles[d.id];
          }
          paint();
        });
        // Roles only for a doctor who is actually selected. Greyed radios beside
        // an unchecked name are noise, and a pre-selected "Labeler" on somebody
        // nobody chose reads as a decision that was never made.
        const roles = (withRoles && on) ? ['label', 'review'].map((role) => {
          const rb = h('input', {
            type: 'radio', name: 'role-' + d.id, value: role,
            checked: (view.roles[d.id] || 'label') === role,
          });
          rb.addEventListener('change', () => {
            if (rb.checked) { view.roles[d.id] = role; paint(); }
          });
          return h('label', { class: 'asc-route-role' }, rb, role === 'label' ? 'Labeler' : 'Reviewer');
        }) : null;
        list.appendChild(h('div', { class: 'asc-route-doc' },
          cb,
          h('span', { class: 'asc-route-doc-name' },
            (d.name || d.email) + ' · ' + (d.specialty || '—')),
          roles ? h('span', { class: 'asc-route-roles' }, roles) : null));
      });
      if (!(view.doctors || []).length) {
        list.appendChild(h('div', { class: 'asc-dim' }, 'No approved doctors to name.'));
      }
      return list;
    }

    /* A whole walk is routed as a walk: solo, relay, or deliberately un-sealed.
     *
     * THE MODE IS SET EXPLICITLY HERE, and that is load-bearing rather than
     * tidy. The first cut rendered a doctor picker and left ``view.mode`` at its
     * default of 'all', so naming a doctor for a solo walk and pressing Send
     * posted ``to_all`` — no assignments written, the whole trajectory flipped
     * to the open queue, and the un-sealing warning not even shown, because it
     * lives on the flat control. The operator asked for one doctor and got
     * everybody, silently. Every branch below names its own targeting. */
    function walkControls() {
      const box = h('div', {});
      const MODES = [
        ['solo', 'Solo walk — one doctor, all points'],
        ['relay', 'Send as relay — one doctor per point'],
        ['open', 'Open queue — any eligible doctor, in sequence'],
      ];
      const current = view.walkMode || 'solo';
      view.walkMode = current;
      view.relay = current === 'relay';
      // Keep the targeting the send path reads in step with the choice on
      // screen, rather than letting a stale default decide it.
      view.mode = (current === 'open') ? 'all' : 'explicit';
      const radios = MODES.map(([id, label]) => {
        const r = h('input', { type: 'radio', name: 'walk-mode', checked: current === id });
        r.addEventListener('change', () => {
          if (!r.checked) return;
          view.walkMode = id;
          view.relayPreview = null; view.proposal = null;
          paint();
        });
        return h('label', { class: 'asc-route-role' }, r, label);
      });
      box.appendChild(h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Mode'), radios));

      if (current === 'open') {
        box.appendChild(h('div', { class: 'asc-inline-warn' },
          'Longitudinal cases sent to All enter the open queue — any eligible '
          + 'doctor may draw them, in sequence order.'));
        return box;
      }
      // Fetch-then-REPAINT. A bare loadDoctors() resolves into a screen that has
      // already been drawn, so the picker renders "No approved doctors to name."
      // and stays that way — and on this path that empty list IS the control.
      if (!view.doctors) loadDoctors().then(paint);
      // Roles are a label/review split on an ALLOCATE send. The relay endpoint
      // takes a rotation and no roles, so offering them there would be a control
      // that silently does nothing.
      box.appendChild(doctorPicker({ roles: current === 'solo' }));
      if (current === 'relay') {
        box.appendChild(h('div', { class: 'asc-dim' },
          'Only the first point is serveable on send; each later point unlocks '
          + 'when the one before it is submitted.'));
      }
      return box;
    }

    function relayPreviewBlock() {
      const r = view.relayPreview;
      const reshuffle = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
        'Reshuffle');
      reshuffle.addEventListener('click', () => {
        view.relaySeed = Math.floor(Math.random() * 1e9); sendRelay(true);
      });
      return h('div', { style: 'margin-top:12px' },
        h('div', { style: 'font-weight:600' }, 'Proposed relay'),
        h('div', { class: 'asc-dim' },
          r.n_points + ' point(s) across ' + r.n_doctors + ' doctor(s).'),
        h('div', { class: 'asc-chain' }, (r.mapping || []).map((m) =>
          h('span', { class: 'asc-chain-cell' },
            h('span', {}, '#' + m.sequence_index + ' → '),
            h('span', { class: 'asc-dim' }, m.email || m.user_id)))),
        reshuffle);
    }

    load();
  }

  /* A read-only case body for the admin preview.
   *
   * It renders the SERVED payload (the endpoint hands back exactly what the
   * doctor's own /tasks/{id} returns), so there is nothing to truncate here —
   * a longitudinal point's stored case IS its visible window. This function must
   * never reach past `task` for "more context": the whole point of previewing
   * through the serve payload is that admin cannot see what the portal hides. */
  function renderCasePanelReadOnly(task) {
    const c = (task && task.case) || null;
    if (!c) return h('div', { class: 'asc-dim' }, 'No structured case on this task.');
    const section = (title, items, fmt) => (items && items.length)
      ? h('div', { class: 'asc-case-section' },
          h('div', { class: 'asc-case-h' }, title),
          h('ul', {}, items.map((it) => h('li', {}, fmt(it)))))
      : null;
    return h('div', { class: 'asc-case-panel' },
      section('Problems', c.problem_list, (p) => p.condition || String(p)),
      section('Medications', c.medications, (m) => [m.drug, m.dose].filter(Boolean).join(' ')),
      section('Labs', c.lab_panels, (p) =>
        `${p.panel || 'panel'} · day ${p.collected_offset_days}`),
      section('Notes', c.notes, (n) =>
        `day ${n.collected_offset_days}: ${(n.text || '').slice(0, 400)}`),
      section('Studies', c.studies, (st) => st.modality || st.study || String(st)));
  }

  /* Assign: who does which case.
   *
   * Cases reached physicians purely by pull before this: whoever opened the
   * queue first got the oldest one, so a hundred promoted nephrology cases
   * could land on one fast labeler and nobody could say who was meant to do
   * what.
   *
   * DRY RUN FIRST, always. The proposal is a table an operator reads and can
   * disagree with, and committing is a second, deliberate click. Same shape as
   * ingest promotion, for the same reason: iterate before a physician is told
   * to do anything.
   */
  function renderAdminAssign(body) {
    clear(body);
    let proposal = null;
    let busy = false;
    let err = null;

    const idsArea = h('textarea', {
      class: 'asc-textarea',
      placeholder: 'Task ids, one per line. Paste them from the Tasks tab.',
      rows: 6,
    });
    const labelsInput = h('input', { class: 'asc-input', type: 'number', value: '2', min: '1', max: '5' });
    const reviewsInput = h('input', { class: 'asc-input', type: 'number', value: '1', min: '0', max: '3' });
    const shareInput = h('input', { class: 'asc-input', type: 'number', value: '0.35', min: '0.05', max: '1', step: '0.05' });

    const results = h('div', {});

    function taskIds() {
      return idsArea.value.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
    }

    function run(dryRun) {
      const ids = taskIds();
      if (!ids.length) { err = 'Paste at least one task id.'; paint(); return; }
      busy = true; err = null; paint();
      api('/admin/assignments/allocate', {
        method: 'POST',
        body: {
          task_ids: ids,
          labels_per_case: Number(labelsInput.value) || 2,
          reviewers_per_case: Number(reviewsInput.value),
          max_share: Number(shareInput.value) || 0.35,
          dry_run: dryRun,
        },
      }).then((res) => {
        proposal = res; busy = false; paint();
        if (!dryRun) toast(`Assigned ${res.committed.length} slot(s).`);
      }).catch((e) => {
        busy = false; err = (e && e.message) || 'Allocation failed.'; paint();
      });
    }

    function paint() {
      clear(results);
      if (err) results.appendChild(h('div', { class: 'asc-inline-error' }, err));
      if (busy) { results.appendChild(loadingCard('Working out who should do what...')); return; }
      if (!proposal) return;

      /* Per physician first. It is the question an operator actually has:
       * "who is this landing on?" A list of 300 rows does not answer it. */
      const per = proposal.per_physician || {};
      const uids = Object.keys(per);
      results.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('h3', {}, proposal.dry_run ? 'Proposed' : 'Committed'),
        h('div', { class: 'asc-dim' },
          `${proposal.cases} case(s), ${proposal.physicians_considered} physician(s) considered`),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {}, h('th', {}, 'Physician'), h('th', {}, 'Labels'),
            h('th', {}, 'Reviews'), h('th', {}, 'Total'))),
          h('tbody', {}, uids.map((uid) => h('tr', {},
            h('td', { class: 'asc-mono' }, uid),
            h('td', { class: 'asc-mono' }, String(per[uid].label)),
            h('td', { class: 'asc-mono' }, String(per[uid].review)),
            h('td', { class: 'asc-mono' }, String(per[uid].total))))))))));

      /* A case nobody could take is shown WITH ITS REASON. Dropping it from
       * the list would read as "all handled". */
      if ((proposal.unassigned || []).length) {
        results.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
          h('h3', {}, `${proposal.unassigned.length} case(s) nobody could take`),
          h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
            h('thead', {}, h('tr', {}, h('th', {}, 'Case'), h('th', {}, 'Why'))),
            h('tbody', {}, proposal.unassigned.map((u) => h('tr', {},
              h('td', { class: 'asc-mono' }, u.task_id),
              h('td', {}, u.reason)))))))));
      }
      (proposal.notes || []).forEach((n) => {
        results.appendChild(h('div', { class: 'asc-dim' }, n));
      });
    }

    const dryBtn = h('button', { class: 'asc-btn', type: 'button' }, 'Preview allocation');
    dryBtn.addEventListener('click', () => run(true));
    const commitBtn = h('button', { class: 'asc-btn asc-btn-primary', type: 'button' }, 'Assign');
    commitBtn.addEventListener('click', () => run(false));

    body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('h3', {}, 'Assign cases'),
      h('div', { class: 'asc-dim' },
        'An assignment is a priority, not a permission: an assigned case goes to '
        + 'the top of that physician\'s queue, and everyone else still sees it '
        + 'exactly where it was.'),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Cases'), idsArea),
      h('div', { class: 'asc-row' },
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Labels per case'), labelsInput),
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Reviewers per case'), reviewsInput),
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Max share per person'), shareInput)),
      h('div', { class: 'asc-rv-actions' }, dryBtn, ' ', commitBtn))));
    body.appendChild(results);
  }

  // Money: the ledger and the referral book. Both were API-only for a while;
  // an admin should never need curl to see what the product owes people.
  /* Money: the payer's side of the physician's earnings page.
   *
   * The subnav is gone because the section is one page now. The referral book
   * that used to be its second tab has its own tab, and a strip with one
   * button in it is chrome pretending there is a choice. `adminSub.money`
   * still exists and still accepts 'referrals' — renderAdminView routes that
   * value to the Referrals tab — so every stored deep link keeps working. */
  function renderAdminMoneySection(body) {
    clear(body);
    const inner = h('div', {});
    body.appendChild(inner);
    if (window.AdminEarningsSection) {
      window.AdminEarningsSection.render(inner, adminSectionCtx(), 'earnings');
    } else sectionModuleMissing(inner, 'The Money section');
  }

  // Data: who supplies it, the pipeline that ingests it, and what ships out.
  function renderAdminDataSection(body) {
    clear(body);
    body.appendChild(adminSubnav('data', [
      ['systems', 'Systems'], ['pipeline', 'Pipeline tools'], ['export', 'Export'],
    ]));
    const inner = h('div', {});
    body.appendChild(inner);
    if (state.adminSub.data === 'pipeline') renderAdminIngestion(inner);
    else if (state.adminSub.data === 'export') renderAdminExportSection(inner);
    else renderAdminHealthSection(inner);
  }

  // Health Systems (inside Data): who supplies our data, and the partner leads
  // that arrived through the public door.
  function renderAdminHealthSection(inner) {
    if (window.AdminHealthSection) window.AdminHealthSection.render(inner, adminSectionCtx());
    else sectionModuleMissing(inner, 'The Health Systems section');
  }

  // Export: ONE PAGE (Export & Approval PRD §2). The subnav is gone: there is
  // no `bycase` / `buyers` / `history` split any more, because three sub-tabs
  // meant four ways to cut a bundle in three places, none of which agreed about
  // what was going to ship. The builder, its preview (including what is being
  // EXCLUDED and why) and the history now render top to bottom in one view.
  //
  // `renderAdminBuyers` is no longer mounted: the buyer CRM is retired and its
  // tables are kept (docs/asclepius/BUYER_CRM_RETIRED.md). The delivery rail
  // lives on as "Export + send to \u25be" inside the builder.
  function renderAdminExportSection(body) {
    clear(body);
    const inner = h('div', {});
    body.appendChild(inner);
    if (window.AdminExportSection) window.AdminExportSection.render(inner, adminSectionCtx());
    else sectionModuleMissing(inner, 'The Export section');
  }

  // Community: the member's channels, read from the operator's side.
  function renderAdminCommunitySection(body) {
    clear(body);
    const inner = h('div', {});
    body.appendChild(inner);
    if (window.AdminCommunitySection) window.AdminCommunitySection.render(inner, adminSectionCtx());
    else sectionModuleMissing(inner, 'The Community section');
  }

  /* Referrals: both halves of one funnel.
   *
   * 'people' is the physician referral book, rendered by the SAME module the
   * ledger uses, in its 'referrals' mode — not a second implementation of
   * /admin/referrals. 'systems' is the health-system introduction funnel,
   * which had no client at all. */
  function renderAdminReferralsSection(body) {
    clear(body);
    body.appendChild(adminSubnav('referrals', [
      ['people', 'Physician referrals'], ['systems', 'Health-system introductions'],
    ]));
    const inner = h('div', {});
    body.appendChild(inner);
    if (state.adminSub.referrals === 'systems') {
      if (window.AdminReferralsSection) window.AdminReferralsSection.render(inner, adminSectionCtx());
      else sectionModuleMissing(inner, 'The Referrals section');
      return;
    }
    if (window.AdminEarningsSection) {
      window.AdminEarningsSection.render(inner, adminSectionCtx(), 'referrals');
    } else sectionModuleMissing(inner, 'The Referrals section');
  }
  async function refreshQaBadge() {
    const badge = document.getElementById('ascQaBadge');
    if (!badge) return;
    try {
      const data = await api('/qa/queue');
      const n = (data.submissions || []).length;
      if (n > 0) { badge.textContent = String(n); badge.removeAttribute('hidden'); }
      else { badge.setAttribute('hidden', ''); }
    } catch (e) { /* leave the badge hidden on error */ }
  }

  // ─── Admin: QA queue (BUG-2) ────────────────────────────────────────────────
  // The backend (/qa/queue, /qa/approve-all, /qa/{id}/decision) was always there;
  // this is the missing UI. Shows the queue with the reason each submission needs
  // review, a diff-style detail view, and Approve / Reject (with reason).
  function renderAdminQA(body) {
    clear(body);
    const headBar = h('div', { style: 'display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:6px' });
    const approveAllBtn = h('button', { class: 'asc-btn asc-btn-primary' }, '✓ Approve all pending');
    const approveAllStatus = h('span', { class: 'asc-label-hint' });
    approveAllBtn.addEventListener('click', async () => {
      if (!window.confirm('Approve EVERY pending submission and move it to export-ready?')) return;
      approveAllBtn.setAttribute('disabled', ''); approveAllBtn.textContent = 'Approving…';
      try {
        const res = await api('/qa/approve-all', { method: 'POST' });
        toast('Approved ' + (res.approved || 0) + ' submission(s).', 'success');
        renderAdminQA(body); refreshQaBadge();
      } catch (e) {
        clear(approveAllStatus); approveAllStatus.appendChild(h('span', { class: 'asc-inline-error' }, e.message));
        approveAllBtn.removeAttribute('disabled'); approveAllBtn.textContent = '✓ Approve all pending';
      }
    });
    headBar.appendChild(approveAllBtn);
    headBar.appendChild(approveAllStatus);

    const listCard = h('div', { class: 'asc-card', id: 'ascQaList' }, loadingCard('Loading QA queue…'));
    const detailCard = h('div', { class: 'asc-card', id: 'ascQaDetail', hidden: true });
    body.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title' }, 'Quality review queue'),
      h('div', { class: 'asc-card-sub', style: 'margin-bottom:12px' },
        'Submissions the pipeline routed to human review (sampled, flagged by the LLM critic, low inter-annotator agreement, or a validation issue). Approve to release to export, or reject with a reason.'),
      headBar));
    body.appendChild(listCard);
    body.appendChild(detailCard);
    loadQaQueue();
  }

  async function loadQaQueue() {
    const listCard = document.getElementById('ascQaList');
    if (!listCard) return;
    try {
      const data = await api('/qa/queue');
      const subs = data.submissions || [];
      clear(listCard);
      listCard.appendChild(h('div', { class: 'asc-card-head' },
        h('div', { class: 'asc-card-title' }, 'Pending (' + subs.length + ')')));
      if (!subs.length) {
        listCard.appendChild(h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-card-sub' }, 'Queue is clear. Nothing needs QA right now.')));
        return;
      }
      const table = h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Submission'), h('th', {}, 'Contributor'), h('th', {}, 'Verdict'),
          h('th', {}, 'Why flagged'), h('th', {}, 'Time'), h('th', {}, ''))));
      const tbody = h('tbody', {});
      subs.forEach((s) => {
        const ann = s.annotator || {};
        const ident = s.contributor || {};
        const reasons = (s.qa_reason || '').split(',').filter(Boolean);
        // Contributor identity is admin-visible (name / org / email); it never
        // ships in a data package, but the admin is allowed to see who labelled.
        const contribCell = h('td', {},
          h('div', { style: 'font-weight:600' }, ident.name || ann.credentials || ann.specialty || 'n/a'),
          ident.organization ? h('div', { class: 'asc-card-sub' }, ident.organization) : null,
          ident.email ? h('div', { class: 'asc-card-sub asc-mono' }, ident.email) : null);
        tbody.appendChild(h('tr', {},
          h('td', { class: 'asc-cell-id' }, copyableId(s.submission_id)),
          contribCell,
          h('td', {}, s.verdict || 'n/a'),
          h('td', {}, reasons.length
            ? reasons.map((r) => h('span', { class: 'asc-chip asc-chip-warn', style: 'margin:2px' }, r))
            : h('span', { class: 'asc-label-hint' }, 'n/a')),
          h('td', {}, (s.time_spent_sec || 0) + 's'),
          h('td', {}, h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
            onClick: () => openQaDetail(s.submission_id) }, 'Review'))));
      });
      table.appendChild(tbody);
      listCard.appendChild(h('div', { class: 'asc-card-pad' }, table));
    } catch (e) {
      clear(listCard);
      listCard.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message)));
    }
  }

  async function openQaDetail(sid) {
    const detail = document.getElementById('ascQaDetail');
    if (!detail) return;
    detail.removeAttribute('hidden');
    clear(detail); detail.appendChild(loadingCard('Loading submission…'));
    detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
    try {
      const sub = await api('/submissions/' + sid);
      const task = sub.task || {};
      const payload = sub.payload || {};
      clear(detail);
      const head = h('div', { class: 'asc-card-head' },
        h('div', {},
          h('div', { class: 'asc-card-title' }, 'Review submission'),
          h('div', { class: 'asc-card-sub asc-mono' }, sid)));
      detail.appendChild(head);

      const pad = h('div', { class: 'asc-card-pad' });
      pad.appendChild(h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Prompt'),
        h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, task.prompt || 'n/a')));

      // Verdict + the doctor's chosen/rejected + rationale (diff-style).
      const verdict = sub.verdict || payload.verdict;
      const cands = {};
      (task.candidate_answers || []).forEach((c) => { cands[c.id] = c.text; });
      if (verdict === 'A_better' || verdict === 'B_better') {
        const rev = payload.chosen_revision || {};
        const crit = payload.rejected_critique || {};
        pad.appendChild(qaDiffBlock('Chosen (' + (sub.chosen_id || '') + ')',
          cands[sub.chosen_id] || '', rev.edited ? rev.revised_text : null));
        pad.appendChild(qaField('Rejected (' + (sub.rejected_id || '') + ')', cands[sub.rejected_id] || ''));
        pad.appendChild(qaField('Why worse', crit.why_worse || 'n/a'));
        if ((crit.error_tags || []).length) {
          pad.appendChild(h('div', { class: 'asc-field' },
            h('label', { class: 'asc-label' }, 'Error tags on rejected'),
            h('div', {}, crit.error_tags.map((t) => h('span', { class: 'asc-chip', style: 'margin:2px' }, t)))));
        }
      } else if (verdict === 'both_inadequate') {
        const fs = payload.from_scratch || {};
        pad.appendChild(qaField('Ideal answer (from scratch)', fs.ideal_answer || 'n/a'));
      }
      const critic = (sub.critic || {}).consistency || {};
      if (critic && critic.consistent === false) {
        pad.appendChild(h('div', { class: 'asc-inline-warn' },
          'Consistency critic flagged: ' + (critic.explanation || (critic.issues || []).join(', '))));
      }

      // Approve / Reject (with reason).
      const rejReason = h('input', { class: 'asc-input', placeholder: 'Reason for rejection (required)' });
      const rejBox = h('div', { hidden: true, style: 'margin-top:10px' },
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Rejection reason'), rejReason));
      const status = h('div', { style: 'margin-top:10px' });
      const approveBtn = h('button', { class: 'asc-btn asc-btn-primary' }, '✓ Approve → export-ready');
      const rejectBtn = h('button', { class: 'asc-btn asc-btn-danger', style: 'margin-left:8px' }, '✕ Reject');
      approveBtn.addEventListener('click', () => qaDecide(sid, 'approve', null, status, [approveBtn, rejectBtn]));
      rejectBtn.addEventListener('click', () => {
        if (rejBox.hasAttribute('hidden')) { rejBox.removeAttribute('hidden'); rejReason.focus(); return; }
        if (!rejReason.value.trim()) { rejReason.focus(); return; }
        qaDecide(sid, 'reject', rejReason.value.trim(), status, [approveBtn, rejectBtn]);
      });
      pad.appendChild(h('div', { style: 'margin-top:16px' }, approveBtn, rejectBtn));
      pad.appendChild(rejBox);
      pad.appendChild(status);
      detail.appendChild(pad);
    } catch (e) {
      clear(detail);
      detail.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message)));
    }
  }

  function qaField(label, value) {
    return h('div', { class: 'asc-field' },
      h('label', { class: 'asc-label' }, label),
      h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, value || 'n/a'));
  }
  function qaDiffBlock(label, original, revised) {
    const field = h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, label));
    if (revised && revised.trim() && revised.trim() !== (original || '').trim()) {
      field.appendChild(h('div', { class: 'asc-readbox' }, renderEditDiff(original || '', revised)));
      field.appendChild(h('div', { class: 'asc-label-hint', style: 'margin-top:4px' }, 'Highlighted sentences were revised by the specialist.'));
    } else {
      field.appendChild(h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, original || 'n/a'));
    }
    return field;
  }
  async function qaDecide(sid, decision, notes, status, buttons) {
    buttons.forEach((b) => b.setAttribute('disabled', ''));
    clear(status);
    try {
      const res = await api('/qa/' + sid + '/decision', { method: 'POST', body: { decision, notes } });
      toast('Submission ' + (res.status === 'export_ready' ? 'approved' : 'rejected') + '.', 'success');
      const detail = document.getElementById('ascQaDetail');
      if (detail) { clear(detail); detail.setAttribute('hidden', ''); }
      loadQaQueue(); refreshQaBadge();
    } catch (e) {
      status.appendChild(h('div', { class: 'asc-inline-error' }, e.message));
      buttons.forEach((b) => b.removeAttribute('disabled'));
    }
  }

  // ─── Admin: Real EHR Ingestion (EHR PRD §4, §8, §9) ─────────────────────────
  // The ONLY door that produces V4 real cases (the Seedmaker card produces V3
  // synthetic: two doors, clearly signed). Mint secure partner links, watch
  // uploads land, triage quarantine, and promote ingested cases to V4 tasks.
  function renderAdminIngestion(body) {
    clear(body);

    // PRD-C: send upload access to a health system. Two fields; everything else
    // (health-system row, username, passphrase, forced reset) is derived
    // server-side. Replaces the old six-field link-minting form.
    // ═══ PRD-I §2.2: two buttons, one form, one code path ═══
    // The two buttons post the SAME body to the SAME endpoint with a different
    // purpose, and everything downstream of the mint is identical: same email,
    // same subject, same portal, same URL shape. The recipient cannot tell which
    // button was pressed, and that is the requirement: not a nicety. Only the
    // admin knows.
    //
    // THREE buttons now, and storage is the one to press unless you already
    // know. It used to be two, on the reasoning that a partner minted with no
    // purpose was a decision nobody made which the gate would read as task
    // creation — true when it was written, and no longer: unset and storage
    // both mean "held until somebody reads the file", and neither promotes.
    //
    // So the third option is not an escape hatch, it is the normal path: take
    // the data, store it, look at it, then decide. The other two remain for a
    // partner whose answer is already settled.
    const hsOrg = h('input', { class: 'asc-input', placeholder: 'Mass General Hospital' });
    const hsEmail = h('input', { type: 'email', class: 'asc-input', placeholder: 'data@mgh.harvard.edu' });
    const mintStatus = h('div', {});
    const mintButtons = [];
    async function sendUploadAccess(purpose) {
      clear(mintStatus);
      const org = hsOrg.value.trim();
      const email = hsEmail.value.trim();
      if (!org) { mintStatus.appendChild(h('div', { class: 'asc-inline-error' }, 'Organization is required.')); return; }
      if (!email) { mintStatus.appendChild(h('div', { class: 'asc-inline-error' }, 'Email is required.')); return; }
      mintButtons.forEach((b) => b.setAttribute('disabled', ''));
      try {
        const res = await api('/admin/health-systems/provision', { method: 'POST', body: {
          organization: org, email: email, purpose: purpose,
        } });
        mintStatus.appendChild(h('div', { class: 'asc-inline-ok' }, res.message
          || ('Upload access sent to ' + email + '.')));
        hsOrg.value = ''; hsEmail.value = '';
        loadIngestionLists();
      } catch (e) { mintStatus.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); }
      finally { mintButtons.forEach((b) => b.removeAttribute('disabled')); }
    }
    const mintStorageBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Send link: storage');
    const mintTaskBtn = h('button', { class: 'asc-btn asc-btn-subtle', style: 'margin-left:8px' }, 'Send link: task creation');
    const mintBrokerBtn = h('button', { class: 'asc-btn asc-btn-subtle', style: 'margin-left:8px' }, 'Send link: brokering');
    mintStorageBtn.addEventListener('click', () => sendUploadAccess('storage'));
    mintTaskBtn.addEventListener('click', () => sendUploadAccess('task_creation'));
    mintBrokerBtn.addEventListener('click', () => sendUploadAccess('brokering'));
    mintButtons.push(mintStorageBtn, mintTaskBtn, mintBrokerBtn);
    const mintCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Send a health system its upload access'),
        h('div', { class: 'asc-card-sub' }, 'The contact receives a username and one-time passphrase by email, signs into the password-protected portal, and uploads. Specialty is determined at ingest: not asked of hospital IT.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Organization'), hsOrg),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Email'), hsEmail)),
        mintStorageBtn, mintTaskBtn, mintBrokerBtn,
        h('div', { class: 'asc-label-hint', style: 'margin-top:8px' },
          'All three links are byte-identical to the recipient. Which button you '
          + 'press is recorded on our side only. Storage takes the data and holds '
          + 'it, used for nothing, until you read a file and set what it is for on '
          + 'its row — nothing is promoted or sent to a model before that.'),
        mintStatus));

    const uploadsCard = h('div', { class: 'asc-card', id: 'ascIngestUploads' }, loadingCard('Loading uploads…'));
    const casesCard = h('div', { class: 'asc-card', id: 'ascIngestCases' }, loadingCard('Loading ingested cases…'));
    body.appendChild(mintCard);
    body.appendChild(renderReconcileCard());
    body.appendChild(uploadsCard);
    body.appendChild(casesCard);
    // Deep-linked from a Health Systems bucket row (C-5.2): say which upload the
    // operator arrived for, and scroll to it once the lists land: otherwise
    // [Review] / [Promote to task] drop them into an unfiltered page.
    if (state.pipelineFocus) {
      const focus = state.pipelineFocus;
      state.pipelineFocus = null;
      body.insertBefore(h('div', { class: 'asc-inline-ok', style: 'margin-bottom:12px' },
        'Showing pipeline tools for upload ', h('code', { class: 'asc-mono' }, focus)), mintCard);
      loadIngestionLists().then(() => {
        const row = document.querySelector('[data-upload="' + focus + '"]');
        if (row && row.scrollIntoView) row.scrollIntoView({ block: 'center' });
        if (row && row.classList) row.classList.add('asc-row-focus');
      }).catch(() => {});
      return;
    }
    loadIngestionLists();
  }

  // Terminal-state reconciliation (V4 Build Spec §9.3): re-bind unbound sealed keys
  // and hold cases whose asset blob went missing. Runs at startup + nightly; this is
  // the on-demand trigger with a live count readout.
  function renderReconcileCard() {
    const out = h('div', { class: 'asc-card-sub', style: 'margin-top:10px' },
      'Checks ingested cases for an unbound answer key or a missing image blob: defects that develop after ingest.');
    const btn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm' }, 'Run reconciliation');
    btn.addEventListener('click', async () => {
      btn.setAttribute('disabled', ''); btn.textContent = 'Reconciling…';
      try {
        const res = await api('/ingestion/reconcile', { method: 'POST', body: {} });
        const c = res.reconcile || {};
        clear(out);
        out.appendChild(h('div', {},
          'Sealed keys re-bound: ' + (c.sealed_bound || 0)
          + ' · orphaned keys: ' + (c.sealed_orphans || 0)
          + ' · missing image blobs: ' + (c.assets_missing || 0)
          + ' · corrupt blobs: ' + (c.assets_corrupt || 0)
          + ' · cases held for review: ' + (c.cases_held || 0) + '.'));
        if ((c.cases_held || 0) > 0) loadIngestionLists();
        toast('Reconciliation complete.', 'success');
      } catch (e) {
        toast('Reconciliation failed: ' + (e.detail || e.message || ''), 'error');
      } finally {
        btn.removeAttribute('disabled'); btn.textContent = 'Run reconciliation';
      }
    });
    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Integrity reconciliation'),
        h('div', { class: 'asc-card-sub' }, 'Terminal-state consistency check for ingested real cases.'))),
      h('div', { class: 'asc-card-pad' }, btn, out));
  }

  // Cached uploads (used to filter the promote list by partner/file search).
  let _ingestUploads = [];

  const _UPLOADS_PAGE = 50;
  let _uploadsOffset = 0;
  let _uploadsTotal = 0;
  // Admin review queue (V4 Build Spec §21.5): the active status filter for the
  // uploads table. null == All. `needs_review` surfaces the review queue.
  let _uploadsStatus = null;

  // Every other status on this screen is a real word: render 'Needs review',
  // never the raw `needs_review` token (§21.7).
  const UPLOAD_STATUS_LABEL = {
    ingested: 'Ready', needs_review: 'Needs review', quarantined: 'Quarantined',
    rejected: 'Rejected', received: 'Received', parsing: 'Parsing', failed: 'Failed',
  };
  const uploadStatusLabel = (s) => UPLOAD_STATUS_LABEL[s] || s || '–';
  // `asc-badge-accent` already exists and is unused in this table, so no CSS change.
  const uploadBadgeClass = (s) => (
    s === 'ingested' ? 'asc-badge-green'
      : s === 'needs_review' ? 'asc-badge-accent'
        : s === 'quarantined' ? 'asc-badge-amber'
          : s === 'rejected' ? 'asc-badge-red' : 'asc-badge-gray');

  async function loadIngestionLists() {
    const up = document.getElementById('ascIngestUploads');
    const cc = document.getElementById('ascIngestCases');
    if (!up) return;
    // The uploads TABLE paginates over full history (renderUploadsTable). The
    // promote list needs the eligible set across history, so fetch a wide window
    // for it separately (keeps its behavior even when the table is on page 2+).
    try {
      const pdata = await api('/ingestion/uploads?limit=200&offset=0');
      _ingestUploads = pdata.uploads || [];
    } catch (e) { _ingestUploads = _ingestUploads || []; }
    await renderUploadsTable(0);
    // Ingested cases → promote, grouped by partner upload, searchable.
    renderPromoteByUpload(cc, '');
  }

  // Full-history, server-paginated uploads table. Each row: Download the original
  // file, and (for anything that didn't cleanly ingest) notify the sender.
  async function renderUploadsTable(offset) {
    const up = document.getElementById('ascIngestUploads');
    if (!up) return;
    try {
      const q = '/ingestion/uploads?limit=' + _UPLOADS_PAGE + '&offset=' + Math.max(0, offset)
        + (_uploadsStatus ? '&status=' + encodeURIComponent(_uploadsStatus) : '');
      const data = await api(q);
      const uploads = data.uploads || [];
      const counts = data.counts || {};
      _uploadsOffset = data.offset || 0;
      _uploadsTotal = data.total || 0;
      clear(up);
      up.appendChild(h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' },
        'Partner uploads (' + (counts.all != null ? counts.all : _uploadsTotal) + ')')));
      // Filter chips (default All). Counts come from the server so they hold across
      // pages and the active filter.
      const chipDefs = [
        { key: null, label: 'All', n: counts.all },
        { key: 'ingested', label: 'Ready', n: counts.ingested },
        { key: 'needs_review', label: 'Needs review', n: counts.needs_review },
        { key: 'quarantined', label: 'Quarantined', n: counts.quarantined },
        { key: 'rejected', label: 'Rejected', n: counts.rejected },
      ];
      const chips = chipDefs.map((d) => {
        const active = (_uploadsStatus || null) === (d.key || null);
        // `.active.err` is the design-system pink emphasis: use it for the review
        // chip when there's a real queue so the hold is visible even unselected.
        const accent = d.key === 'needs_review' && (d.n || 0) > 0;
        const chip = h('button', {
          class: 'asc-chip' + (active ? ' active' : '') + (accent ? ' err' : ''),
        }, d.label + (d.n != null ? ' (' + d.n + ')' : ''));
        chip.addEventListener('click', () => { _uploadsStatus = d.key || null; renderUploadsTable(0); });
        return chip;
      });
      up.appendChild(h('div', { class: 'asc-chips asc-card-pad', style: 'padding-top:0' }, chips));
      if (!uploads.length) {
        up.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-card-sub' },
          _uploadsStatus ? ('No uploads with status "' + uploadStatusLabel(_uploadsStatus) + '".')
                         : 'No uploads yet: send a health system its upload access above.')));
        return;
      }
      const rows = [];
      uploads.forEach((u) => {
        const dlBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
          onClick: () => downloadBlob('/ingestion/uploads/' + u.upload_id + '/download', u.filename || (u.upload_id + '.zip')) },
          '⬇ Download file');
        const actions = [dlBtn];
        // A held upload gets a Review action that expands an inline drawer (never a
        // modal: the table already renders rows).
        if (u.status === 'needs_review') {
          const rbtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm' }, 'Review');
          rbtn.addEventListener('click', () => toggleReviewDrawer(u, rbtn));
          actions.push(rbtn);
        }
        if (u.status !== 'ingested') {
          const canNotify = !!u.contact_email;
          const nbtn = h('button', {
            class: 'asc-btn asc-btn-subtle asc-btn-sm',
            title: canNotify ? ('Email ' + u.contact_email + ' that this upload didn’t come through')
                             : 'No contact email on this upload’s link. Set one when minting the link',
            onClick: () => notifySender(u),
          }, u.failure_notified ? 'Re-notify sender' : 'Notify sender');
          if (!canNotify) nbtn.disabled = true;
          actions.push(nbtn);
        }
        const row = h('tr', { 'data-upload': u.upload_id },
          h('td', {}, fmtDate(u.created_at)),
          h('td', {}, u.partner_label || u.partner_id || 'n/a'),
          h('td', { class: 'asc-mono' }, (u.filename || '') + ' · ' + Math.round((u.size_bytes || 0) / 1024) + 'KB'),
          h('td', {}, h('span', { class: 'asc-badge ' + uploadBadgeClass(u.status) }, uploadStatusLabel(u.status))),
          h('td', {}, h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' }, actions)));
        rows.push(row);
        // Placeholder row the drawer expands into, kept adjacent for correct order.
        const drawer = h('tr', { class: 'asc-review-drawer-row', 'data-drawer-for': u.upload_id, style: 'display:none' },
          h('td', { colspan: '5', style: 'padding:0' }, h('div', { class: 'asc-review-drawer' })));
        rows.push(drawer);
      });
      up.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['When', 'Partner', 'File', 'Status', ''].map((c) => h('th', {}, c)))),
        h('tbody', {}, rows))));
      // Pager
      const to = _uploadsOffset + uploads.length;
      const prev = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm' }, '‹ Prev');
      const next = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm' }, 'Next ›');
      if (_uploadsOffset <= 0) prev.disabled = true;
      if (to >= _uploadsTotal) next.disabled = true;
      prev.addEventListener('click', () => renderUploadsTable(Math.max(0, _uploadsOffset - _UPLOADS_PAGE)));
      next.addEventListener('click', () => renderUploadsTable(_uploadsOffset + _UPLOADS_PAGE));
      up.appendChild(h('div', { class: 'asc-card-pad', style: 'display:flex;justify-content:space-between;align-items:center;gap:10px' },
        h('div', { class: 'asc-card-sub' }, 'Showing ' + (_uploadsOffset + 1) + '–' + to + ' of ' + _uploadsTotal),
        h('div', { style: 'display:flex;gap:8px' }, prev, next)));
    } catch (e) {
      clear(up);
      up.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message || 'Failed to load uploads')));
    }
  }

  async function notifySender(u) {
    try {
      const res = await api('/ingestion/uploads/' + u.upload_id + '/notify-sender', { method: 'POST' });
      toast('Sender notified' + (res && res.detail ? ': ' + res.detail : ''), 'success');
      renderUploadsTable(_uploadsOffset);   // reflect the notified state
    } catch (e) {
      toast('Could not notify sender: ' + (e.detail || e.message || ''), 'error');
    }
  }

  // ── Admin review queue drawer (V4 Build Spec §21.5-§21.7) ────────────────────
  // Expands inline under a `needs_review` row. Blocking reasons render ABOVE
  // advisory, always. A blocking image row outlines the top/bottom 12% bands in the
  // design-system flag colour so a reviewer certifies "no PHI" from pixels, not a
  // filename. The advisory row states the case is already in the annotation queue.
  async function toggleReviewDrawer(upload, btn) {
    const drawerRow = document.querySelector('tr[data-drawer-for="' + upload.upload_id + '"]');
    if (!drawerRow) return;
    if (drawerRow.style.display !== 'none') {
      drawerRow.style.display = 'none';
      if (btn) btn.textContent = 'Review';
      return;
    }
    drawerRow.style.display = '';
    if (btn) btn.textContent = 'Hide review';
    const host = drawerRow.querySelector('.asc-review-drawer');
    clear(host);
    host.appendChild(loadingCard('Loading review reasons…'));
    let data;
    try {
      data = await api('/ingestion/uploads/' + upload.upload_id + '/review');
    } catch (e) {
      clear(host);
      host.appendChild(h('div', { class: 'asc-inline-error' }, e.message || 'Could not load review reasons.'));
      return;
    }
    renderReviewDrawer(host, upload, data.cases || []);
  }

  function renderReviewDrawer(host, upload, cases) {
    clear(host);
    if (!cases.length) {
      host.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-card-sub' },
        'Nothing left to review on this upload.')));
      return;
    }
    const box = h('div', { class: 'asc-card-pad', style: 'display:flex;flex-direction:column;gap:14px' });
    cases.forEach((c) => box.appendChild(renderReviewCase(upload, c)));
    host.appendChild(box);
  }

  function renderReviewCase(upload, c) {
    const reasons = c.reasons || [];              // already blocking-first from the API
    const blocking = reasons.filter((r) => r.severity === 'blocking');
    const advisory = reasons.filter((r) => r.severity !== 'blocking');
    const studiesByAsset = {};
    (c.studies || []).forEach((s) => { if (s && s.asset && s.asset.asset_id) studiesByAsset[s.asset.asset_id] = s; });

    const wrap = h('div', { class: 'asc-review-case', style: 'border:1px solid var(--asc-line);border-radius:10px;padding:12px' });
    wrap.appendChild(h('div', { class: 'asc-card-sub asc-mono', style: 'margin-bottom:8px' },
      'case ' + c.ingest_case_id + (c.review_status ? ' · ' + c.review_status : '')));

    // Blocking reasons FIRST: the PHI hold outranks the advisory note.
    blocking.forEach((r) => {
      const block = h('div', { style: 'margin-bottom:12px' });
      block.appendChild(h('div', { style: 'display:flex;align-items:center;gap:8px' },
        h('span', { class: 'asc-badge asc-badge-red' }, 'Blocking'),
        h('strong', {}, reasonTitle(r.reason))));
      block.appendChild(h('div', { class: 'asc-card-sub', style: 'margin:4px 0 8px' }, r.detail || ''));
      // Show a study image with the top/bottom bands flagged, when we have one.
      const firstStudy = (c.studies || [])[0];
      const withAsset = (c.studies || []).find((s) => s && s.asset && s.asset.asset_id);
      if (withAsset) {
        block.appendChild(renderBandedImage(withAsset));
      } else {
        block.appendChild(h('div', { class: 'asc-inline-warn' },
          'No render is available for this study: its pixels were withheld from the content store '
          + 'until a human clears them. Download the original bundle above to inspect the source image '
          + 'before certifying no PHI.'));
      }
      wrap.appendChild(block);
    });

    // Advisory reasons: the case is clinically intact and, if not otherwise held,
    // already in the annotation queue. Nobody needs to chase it.
    advisory.forEach((r) => {
      const adv = h('div', { style: 'margin-bottom:10px' });
      adv.appendChild(h('div', { style: 'display:flex;align-items:center;gap:8px' },
        h('span', { class: 'asc-badge asc-badge-gray' }, 'Advisory'),
        h('strong', {}, reasonTitle(r.reason))));
      adv.appendChild(h('div', { class: 'asc-card-sub', style: 'margin:4px 0' }, r.detail || ''));
      adv.appendChild(h('div', { class: 'asc-card-sub' },
        blocking.length ? 'This note travels with the case; it did not hold it.'
                        : 'This case is already in the annotation queue: no action needed.'));
      wrap.appendChild(adv);
    });

    // Actions. Only a blocking hold needs clear/reject; a pure-advisory case just
    // gets an Acknowledge that closes the drawer.
    const status = h('div', { style: 'margin-top:8px' });
    const actions = h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' });
    if (blocking.length) {
      const clearBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm' }, 'No PHI: clear');
      clearBtn.addEventListener('click', () => clearReview(upload, c, status));
      const rejectBtn = h('button', { class: 'asc-btn asc-btn-danger asc-btn-sm' }, 'PHI present: reject case');
      rejectBtn.addEventListener('click', () => rejectReview(upload, c, status));
      actions.appendChild(clearBtn);
      actions.appendChild(rejectBtn);
    } else {
      const ackBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm' }, 'Acknowledge');
      ackBtn.addEventListener('click', () => renderUploadsTable(_uploadsOffset));
      actions.appendChild(ackBtn);
    }
    wrap.appendChild(actions);
    wrap.appendChild(status);
    return wrap;
  }

  function reasonTitle(code) {
    return ({
      burned_in_phi_unverified: 'Burned-in PHI could not be screened',
      asset_blob_missing: 'Image asset is missing from storage',
      sealed_key_unbound: 'Sealed answer key is unbound',
      completeness_unverified: 'Declared evidence could not be verified',
      deid_partner_flag_only: 'Image cleared on DICOM tags alone (no OCR)',
      adapter_parse_gap: 'A parser gap degraded this case (e.g. an unmapped date column)',
    })[code] || code;
  }

  // Render a study image at review size with the top and bottom 12% bands outlined
  // in the design-system flag colour: the regions where burned-in PHI lives.
  function renderBandedImage(study) {
    const frame = h('div', { style: 'position:relative;max-width:420px;border:1px solid var(--asc-line);border-radius:8px;overflow:hidden;background:var(--asc-surface-2)' });
    const img = h('img', { alt: (study.label || study.modality || 'clinical') + ' image', style: 'display:block;width:100%;height:auto' });
    frame.appendChild(img);
    const band = (top) => h('div', { style: 'position:absolute;left:0;right:0;height:12%;'
      + (top ? 'top:0;' : 'bottom:0;')
      + 'border:2px solid var(--pink-deep);background:var(--pink-wash);pointer-events:none' });
    frame.appendChild(band(true));
    frame.appendChild(band(false));
    fetchAssetBlobUrl(study.asset.asset_id).then((url) => { img.src = url; }).catch(() => {
      frame.appendChild(h('div', { class: 'asc-inline-warn' }, 'Could not load the image.'));
    });
    return frame;
  }

  async function clearReview(upload, c, status) {
    const note = window.prompt('Certify no burned-in PHI in this image. Enter a review note (required):');
    if (note == null) return;                     // cancelled
    if (!note.trim()) { toast('A review note is required to clear a case.', 'error'); return; }
    clear(status);
    status.appendChild(loadingCard('Clearing…'));
    try {
      await api('/ingestion/cases/' + c.ingest_case_id + '/review/clear',
        { method: 'POST', body: { note: note, reason: (c.reasons && c.reasons[0] && c.reasons[0].reason) || null } });
      toast('Case cleared: back in the annotation queue.', 'success');
      loadIngestionLists();
    } catch (e) {
      clear(status);
      status.appendChild(h('div', { class: 'asc-inline-error' }, e.detail || e.message || 'Clear failed.'));
    }
  }

  async function rejectReview(upload, c, status) {
    if (!window.confirm('Reject this case? It will be quarantined and never served for annotation.')) return;
    clear(status);
    status.appendChild(loadingCard('Rejecting…'));
    try {
      await api('/ingestion/cases/' + c.ingest_case_id + '/review/reject', { method: 'POST' });
      toast('Case rejected and quarantined.', 'success');
      loadIngestionLists();
    } catch (e) {
      clear(status);
      status.appendChild(h('div', { class: 'asc-inline-error' }, e.detail || e.message || 'Reject failed.'));
    }
  }

  // Group ingested cases by the partner upload they came from, filtered by a
  // partner/file search box. Promote runs on the WHOLE file: prepare a sample →
  // review → extend case creation to the rest.
  function renderPromoteByUpload(cc, query) {
    if (!cc) return;
    clear(cc);
    // Build the header + search input ONCE; typing only re-renders the results
    // container below (so the input keeps focus + caret between keystrokes).
    const listBox = h('div', {});
    const search = h('input', { class: 'asc-input', placeholder: 'Search a partner upload (e.g. "Gray Scrubs Lab") or file name…', value: query || '' });
    search.addEventListener('input', () => renderPromoteList(listBox, search.value));
    cc.appendChild(h('div', { class: 'asc-card-head' }, h('div', { style: 'flex:1' },
      h('div', { class: 'asc-card-title' }, 'Ready to promote, by partner upload'),
      h('div', { class: 'asc-card-sub' }, 'Pick a partner file and promote it to V4. We convert the real records, run automated tests, and show you one sample case (labs, notes, EHR + candidates) to review, then extend case creation to the rest of the file.'),
      h('div', { class: 'asc-field', style: 'margin-top:12px;margin-bottom:0' }, search))));
    cc.appendChild(listBox);
    renderPromoteList(listBox, query || '');
  }

  function renderPromoteList(listBox, query) {
    clear(listBox);
    const q = (query || '').trim().toLowerCase();
    // Include held uploads too (V4 §4.7) so a `needs_review` file is visible here,
    // with a "Held for review" marker instead of a promote button, rather than
    // silently vanishing while an unresolved blocking reason keeps its cases out.
    const eligible = (_ingestUploads || []).filter((u) => ((u.ingested_case_count || 0) > 0 || u.status === 'needs_review')
      && (!q || (u.partner_label || '').toLowerCase().includes(q)
              || (u.partner_id || '').toLowerCase().includes(q)
              || (u.filename || '').toLowerCase().includes(q)));
    if (!eligible.length) {
      listBox.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-card-sub' },
        q ? 'No partner uploads match "' + query + '" with cases ready to promote.' : 'No uploads have ingested cases awaiting promotion.')));
      return;
    }
    eligible.forEach((u) => {
      const st = h('div', { style: 'margin-top:10px' });
      const ready = (u.ingested_case_count || 0) > 0;
      // Why the server would refuse, decided by the server (see _promote_block in
      // routers/asclepius.py). A button that is clickable into a 409 is a button
      // that lies about what it does, so a blocked upload gets a disabled control
      // carrying the actual reason — and, where the reason is a fixable one, the
      // control that fixes it.
      const block = u.promote_block || null;
      let action;
      if (block && block.reason === 'brokering') {
        // Brokering never gets a Promote button at all: the server refusing is
        // the enforcement, and this is the affordance. A design that relies on
        // only one of the two is a design that eventually promotes one by
        // accident (PRD-I §5).
        action = h('span', { class: 'asc-badge asc-badge-gray', title: block.message }, 'Brokering');
      } else if (!ready) {
        action = h('span', { class: 'asc-badge asc-badge-accent', title: 'A case in this upload is held for admin review: clear it in Partner uploads above.' }, 'Held for review');
      } else if (block) {
        action = h('button', {
          class: 'asc-btn asc-btn-primary asc-btn-sm', disabled: true, title: block.message,
        }, 'Promote to V4 task');
      } else {
        const b = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm' }, 'Promote to V4 task');
        b.addEventListener('click', () => openPromoteReview(u, st));
        action = b;
      }
      // Real-case GENERATION (Real-Case Generation PRD §5). A 14-month chart is
      // not one case; it is a series of decision points. Preview is the whole
      // point of the control — an admin must read every proposed question before
      // it reaches a physician, because generating a wrong question at scale is
      // how you burn physician goodwill. Preview writes nothing.
      let previewAction = null;
      if (ready && !(block && block.reason === 'brokering')) {
        const pb = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm' }, 'Preview cases');
        pb.addEventListener('click', () => openCasePreview(u, st));
        previewAction = pb;
      }

      const sub = ready
        ? (u.ingested_case_count || 0) + ' case(s) ready · uploaded ' + fmtDate(u.created_at)
        : 'Held for review · uploaded ' + fmtDate(u.created_at);
      const info = h('div', {},
        h('strong', {}, (u.partner_label || u.partner_id || 'partner')),
        h('div', { class: 'asc-card-sub asc-mono' }, u.filename || ''),
        h('div', { class: 'asc-card-sub' }, sub));
      if ((u.specialties || []).length) {
        info.appendChild(h('div', { class: 'asc-card-sub' },
          'Specialty: ' + u.specialties.join(', ')));
      }

      const row = h('div', { class: 'asc-card-pad', style: 'border-top:1px solid var(--asc-line)' },
        h('div', { style: 'display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center' },
          info,
          h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' },
            h('span', { class: 'asc-badge-real' }, 'real · V4'),
            previewAction,
            action)));
      // The reason, inline and unmissable — a disabled button with only a
      // tooltip is a dead end for anyone who does not hover it.
      if (block && block.reason === 'specialty') {
        row.appendChild(h('div', { class: 'asc-promote-block' },
          h('div', { class: 'asc-promote-block-why' }, block.message),
          specialtyResolver(u.upload_id, () => loadIngestionLists())));
      } else if (block && block.reason !== 'brokering' && ready) {
        row.appendChild(h('div', { class: 'asc-promote-block' },
          h('div', { class: 'asc-promote-block-why' }, block.message)));
      }
      row.appendChild(st);
      listBox.appendChild(row);
    });
  }

  // ── Real-case generation: preview the plan, then generate (PRD §5) ─────────
  // The dry run returns the FULL plan — every proposed case with its index event,
  // question, tags and difficulty band — and writes nothing. Skipped encounters
  // are shown with their reason: a preview that lists only the survivors reads as
  // "this chart had two decision points" when it had twelve.
  async function openCasePreview(upload, statusBox) {
    clear(statusBox);
    statusBox.appendChild(loadingCard('Segmenting the chart into decision points…'));
    let cases = [];
    try {
      const data = await api('/ingestion/cases?status=ingested&upload_id='
        + encodeURIComponent(upload.upload_id));
      cases = data.cases || [];
    } catch (e) {
      clear(statusBox);
      statusBox.appendChild(h('div', { class: 'asc-inline-error' },
        e.message || 'Could not load the ingested cases for this file.'));
      return;
    }
    if (!cases.length) {
      clear(statusBox);
      statusBox.appendChild(h('div', { class: 'asc-card-sub' },
        'No ingested cases in this file.'));
      return;
    }
    const ic = cases[0];
    let plan;
    try {
      plan = await api('/ingestion/cases/' + ic.ingest_case_id + '/generate',
        { method: 'POST', body: { dry_run: true, derive_questions: true } });
    } catch (e) {
      clear(statusBox);
      statusBox.appendChild(h('div', { class: 'asc-inline-error' },
        (e && e.detail && e.detail.error) || e.message || 'Could not build a case plan.'));
      return;
    }
    clear(statusBox);
    statusBox.appendChild(h('div', { class: 'asc-card-sub' },
      (ic.patient_key || 'chart') + ' · ' + (plan.encounters || 0) + ' encounters detected · '
      + (plan.generatable || 0) + ' generatable'));
    openCasePlanModal(upload, ic, plan, statusBox);
  }

  const _diffBadgeClass = (band) => (
    band === 'hard' ? 'asc-badge-red' : band === 'medium' ? 'asc-badge-amber' : 'asc-badge-gray');

  // Longitudinal density (PRD 2 §2): why this encounter is or is not a decision
  // point, WITH the measurements. 34 of the 59 encounters across the four real
  // charts fail this gate; an admin looking at a chart that yielded 3 points out
  // of 17 needs to read which threshold each one missed, or the gate is
  // unarguable — and the gate is the product.
  function renderDensityLine(p) {
    const d = p.density;
    if (!d) return null;
    if (p.qualifies_as_decision_point) {
      return h('div', { class: 'asc-case-note-meta' },
        'Decision point · ' + d.n_distinct_dates + ' date(s), ' + d.n_events
        + ' event(s), ' + d.n_resource_types + ' resource type(s)'
        + (p.outcome_verifiable
            ? ' · a later encounter can check it'
            : ' · nothing later in the record to check it against'));
    }
    return h('div', { class: 'asc-case-note-meta' },
      'Below the decision-point gate: ' + (d.reasons || []).join('; '));
  }

  function renderProposalRow(ic, p, refresh) {
    const wrap = h('div', {
      class: 'asc-card-pad',
      style: 'border:1px solid var(--asc-line);border-radius:10px;margin-bottom:12px'
        + (p.generatable ? '' : ';opacity:.72'),
    });
    const d = p.difficulty || {};
    const head = h('div', { style: 'display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center' },
      h('div', {},
        h('strong', {}, 'Encounter ' + p.encounter_index),
        h('div', { class: 'asc-card-sub asc-mono' },
          'index event day ' + p.index_event_offset
          + ' · window ' + (p.encounter_span || []).join(' … ')
          + ' · ' + (p.n_events || 0) + ' recorded events'),
        h('div', { class: 'asc-card-sub' }, (p.index_rationale || {}).reason || ''),
        renderDensityLine(p)),
      h('div', { style: 'display:flex;gap:6px;align-items:center;flex-wrap:wrap' },
        // The difficulty band is a CLAIM until it is measured, and the plan never
        // measures — so the preview says so rather than showing a bare band.
        h('span', { class: 'asc-badge ' + _diffBadgeClass(d.band) },
          (d.band || '–') + (d.measured ? '' : ' · proposed')),
        p.taxonomy_bucket ? h('span', { class: 'asc-badge asc-badge-gray' }, p.taxonomy_bucket) : null,
        p.subtopic ? h('span', { class: 'asc-badge asc-badge-gray' }, p.subtopic) : null));
    wrap.appendChild(head);

    if (p.question) {
      // ORANGE = model output. The proposed question is model output until a
      // physician accepts it, and that distinction is the whole point of the
      // console's colour semantics — green is physician-authored.
      const modelAuthored = p.question_source === 'model';
      wrap.appendChild(h('div', { class: 'asc-field', style: 'margin-top:10px' },
        h('label', { class: 'asc-label' },
          'Proposed question' + (modelAuthored ? ' · model output, not yet accepted'
                                               : ' · derived from the chart')),
        h('div', {
          class: 'asc-readbox',
          style: 'white-space:pre-wrap' + (modelAuthored
            ? ';border-color:var(--orange-line);background:var(--orange-wash)' : ''),
        }, p.question)));
    }

    const c = p.content || {};
    wrap.appendChild(h('div', { class: 'asc-card-sub', style: 'margin-top:8px' },
      (p.specialty || 'specialty not determined')
      + (p.specialty_confidence != null ? ' (confidence ' + p.specialty_confidence + ')' : '')
      + ' · ' + (c.lab_panels || 0) + ' panel(s) · ' + (c.notes || 0) + ' note(s) · '
      + (c.medications || 0) + ' med(s) · ' + (c.problem_list || 0) + ' problem(s)'));

    if (d.axes) {
      wrap.appendChild(h('div', { class: 'asc-card-sub asc-mono', style: 'margin-top:4px' },
        'difficulty ' + d.score + ' — '
        + Object.keys(d.axes).map((k) => k + ' ' + d.axes[k]).join(' · ')));
    }
    if (d.gate_note) {
      wrap.appendChild(h('div', { class: 'asc-inline-warn', style: 'margin-top:6px' }, d.gate_note));
    }

    if (!p.generatable) {
      // LIME = needs attention. A blocked proposal is not a failure, it is work
      // an admin can act on (set the specialty, accept the thin encounter).
      wrap.appendChild(h('div', {
        class: 'asc-card-sub',
        style: 'margin-top:8px;padding:8px 10px;border-radius:8px;'
          + 'background:var(--lime-wash);border:1px solid var(--lime-line);color:var(--lime-deep)',
      }, 'Not generatable: ' + (p.blockers || []).join(' · ')));
      return wrap;
    }

    const status = h('div', { style: 'margin-top:10px' });
    const btn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', style: 'margin-top:10px' },
      'Generate this case');
    btn.addEventListener('click', async () => {
      btn.setAttribute('disabled', '');
      btn.textContent = 'Measuring difficulty and generating…';
      clear(status);
      try {
        const r = await api('/ingestion/cases/' + ic.ingest_case_id + '/generate', {
          method: 'POST',
          body: { dry_run: false, encounter_indices: [p.encounter_index] },
        });
        const ok = (r.task_ids || []).length;
        status.appendChild(h('div', { class: ok ? 'asc-inline-ok' : 'asc-inline-warn' },
          ok ? 'Created task ' + r.task_ids[0]
             : 'Gated: ' + JSON.stringify(((r.details || {}).gated || (r.details || {}).failed || []))));
        if (ok && refresh) refresh();
      } catch (e) {
        status.appendChild(h('div', { class: 'asc-inline-error' },
          (e && e.detail && e.detail.error) || e.message || 'Generation failed.'));
      }
      btn.removeAttribute('disabled');
      btn.textContent = 'Generate this case';
    });
    wrap.appendChild(btn);
    wrap.appendChild(status);
    return wrap;
  }

  /* ``opts.trajectory`` carries the LONGITUDINAL choice from the dry run into
   * the commit. It has to: the plan an admin approves was computed with
   * ``trajectory: true``, and a commit that dropped the flag would generate
   * independent cases from the same chart — the right number of tasks, silently
   * the wrong product, with no trajectory_id and so no sequence gate. Defaults
   * to the static behaviour, so the ingestion page's existing call is unchanged. */
  function openCasePlanModal(upload, ic, plan, statusBox, opts) {
    const trajectory = !!(opts && opts.trajectory);
    const overlay = h('div', {
      class: 'call-team-overlay is-open',
      onClick: (e) => { if (e.target === overlay) overlay.remove(); },
    });
    const list = h('div', {});
    const proposals = plan.proposals || [];
    proposals.forEach((p) => list.appendChild(
      renderProposalRow(ic, p, () => loadIngestionLists())));

    const status = h('div', { style: 'margin-top:12px' });
    const nGen = plan.generatable || 0;
    const allBtn = h('button', { class: 'asc-btn asc-btn-primary' },
      'Generate all ' + nGen + ' case(s)');
    if (!nGen) allBtn.setAttribute('disabled', '');
    allBtn.addEventListener('click', async () => {
      allBtn.setAttribute('disabled', '');
      allBtn.textContent = 'Generating…';
      clear(status);
      try {
        const r = await api('/ingestion/cases/' + ic.ingest_case_id + '/generate',
          { method: 'POST', body: { dry_run: false, trajectory } });
        overlay.remove();
        clear(statusBox);
        statusBox.appendChild(h('div', { class: 'asc-inline-ok' },
          (trajectory
            ? 'Built a chart walk of ' + r.generated + ' decision point(s). They are '
              + 'held back from every queue until you send them from Task Routing.'
            : 'Generated ' + r.generated + ' V4 case(s)')
          + (r.gated ? ' · ' + r.gated + ' gated' : '')
          + (r.failed ? ' · ' + r.failed + ' failed' : '') + '.'));
        toast('Generated ' + r.generated + ' case(s) from this chart.', 'success');
        loadIngestionLists();
      } catch (e) {
        status.appendChild(h('div', { class: 'asc-inline-error' },
          (e && e.detail && e.detail.error) || e.message || 'Generation failed.'));
        allBtn.removeAttribute('disabled');
        allBtn.textContent = 'Generate all ' + nGen + ' case(s)';
      }
    });

    // ── Longitudinal trajectory (PRD 2 §4 Phase 5) ───────────────────────────
    // A different product from a batch of independent cases, so it is a different
    // button with its own stated count and its own stated price. The confirm is not
    // ceremony: this writes N tasks at $75 a completed submission, and §9.3 exists
    // because "a trajectory is not a discount on physician time — it is N tasks
    // that happen to share a chart".
    const nPoints = plan.decision_points || 0;
    const nVerifiable = plan.verifiable_decision_points || 0;
    const trajBtn = h('button', { class: 'asc-btn asc-btn-primary' },
      'Chain ' + nPoints + ' decision point(s) into one trajectory');
    if (!nPoints) trajBtn.setAttribute('disabled', '');
    trajBtn.addEventListener('click', async () => {
      const cost = (nPoints * 75).toLocaleString();
      if (!window.confirm(
        'Create a ' + nPoints + '-point longitudinal trajectory from this chart?\n\n'
        + '· ' + nVerifiable + ' of the ' + nPoints + ' points can be checked against a later '
        + 'encounter. The last point has nothing after it in the record.\n'
        + '· Points are single-labelled. They are excluded from the κ pool by '
        + 'construction, so a second label buys no agreement statistic.\n'
        + '· Physician cost at the standard rate: about $' + cost + '.\n\n'
        + 'Each physician answers the points in order and cannot read ahead.')) return;
      trajBtn.setAttribute('disabled', '');
      trajBtn.textContent = 'Generating trajectory…';
      clear(status);
      try {
        const r = await api('/ingestion/cases/' + ic.ingest_case_id + '/generate',
          { method: 'POST', body: { dry_run: false, trajectory: true } });
        overlay.remove();
        clear(statusBox);
        statusBox.appendChild(h('div', { class: 'asc-inline-ok' },
          'Trajectory ' + (r.trajectory_id || '') + ': ' + (r.trajectory_points || 0)
          + ' point(s), ' + (r.trajectory_verifiable_points || 0) + ' outcome-verifiable'
          + (r.estimated_cost_usd ? ' · est. $' + r.estimated_cost_usd : '')
          + (r.gated ? ' · ' + r.gated + ' gated' : '')
          + (r.failed ? ' · ' + r.failed + ' failed' : '') + '.'));
        toast('Chained ' + (r.trajectory_points || 0) + ' decision points into one trajectory.', 'success');
        loadIngestionLists();
      } catch (e) {
        status.appendChild(h('div', { class: 'asc-inline-error' },
          (e && e.detail && e.detail.error) || e.message || 'Trajectory generation failed.'));
        trajBtn.removeAttribute('disabled');
        trajBtn.textContent = 'Chain ' + nPoints + ' decision point(s) into one trajectory';
      }
    });

    const popup = h('div', {
      class: 'call-team-popup',
      style: 'max-width:900px;max-height:90vh;overflow:auto;text-align:left',
      onClick: (e) => e.stopPropagation(),
    },
      h('div', { class: 'call-team-title' },
        'Proposed cases: ' + (upload.partner_label || upload.partner_id || 'partner')),
      h('div', { class: 'call-team-sub' },
        (ic.patient_key || '') + ' · ' + (plan.encounters || 0) + ' encounters detected · '
        + nGen + ' generatable'
        + (plan.specialty_hint ? ' · specialty ' + plan.specialty_hint : '')),
      h('div', { class: 'asc-card-sub', style: 'margin-bottom:6px' },
        'Nothing here has been written. Difficulty is measured only when you generate — '
        + 'a band shown as "proposed" is the structural prior, not a frontier failure rate.'),
      // Both numbers, always, because they are what a chart walk is priced on and
      // they are never the same number.
      h('div', { class: 'asc-card-sub', style: 'margin-bottom:14px' },
        nPoints + ' encounter(s) clear the decision-point gate (≥2 dates, ≥8 events, '
        + '≥2 resource types) · ' + nVerifiable + ' have a later encounter to be checked '
        + 'against. Encounters below the gate are single-contact draws, not decisions.'),
      list,
      status,
      h('div', { style: 'display:flex;gap:10px;margin-top:16px;flex-wrap:wrap' },
        allBtn, trajBtn,
        h('button', { class: 'asc-btn asc-btn-ghost', style: 'margin-left:auto',
                      onClick: () => overlay.remove() }, 'Close')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  // Prepare + review a sample case for an upload, then promote the rest.
  async function openPromoteReview(upload, statusBox) {
    clear(statusBox);
    statusBox.appendChild(loadingCard('Converting real records and running automated tests on a sample case…'));
    let prep;
    try {
      prep = await api('/ingestion/uploads/' + upload.upload_id + '/prepare', { method: 'POST', body: {} });
    } catch (e) {
      clear(statusBox);
      statusBox.appendChild(h('div', { class: 'asc-inline-error' }, typeof e.message === 'string' ? e.message : 'Could not prepare a sample.'));
      return;
    }
    clear(statusBox);
    openSampleReviewModal(upload, prep, statusBox);
  }

  function openSampleReviewModal(upload, prep, statusBox) {
    const s = prep.sample || {};
    const kase = s.case || {};
    const overlay = h('div', { class: 'call-team-overlay is-open', onClick: (e) => { if (e.target === overlay) overlay.remove(); } });

    // Automated-test result banner.
    const testBanner = s.tests_passed
      ? h('div', { class: 'asc-grounding-banner', style: 'margin-bottom:14px' },
          h('div', { class: 'asc-gb-icon', 'aria-hidden': 'true' }),
          h('div', {}, h('div', { class: 'asc-gb-title' }, 'Automated tests passed'),
            h('div', { class: 'asc-gb-text' }, 'The sample case cleared the real-case gate (coherence, multimodal necessity, reasoning divergence).')))
      : h('div', { class: 'asc-inline-warn', style: 'margin-bottom:14px' },
          'Sample did not pass the automated gate: ' + ((s.failures || []).join('; ') || 'see scores below') +
          '. You can still promote; cases that fail the gate stay ingested with the reason recorded.');

    // Case panel: labs, notes, meds, problems, demographics.
    const demo = kase.demographics || {};
    const labs = (kase.lab_panels || []).map((p) => h('div', { style: 'margin-bottom:8px' },
      h('div', { style: 'font-weight:600' }, (p.panel || 'panel') + (p.collected_offset_days != null ? '  (day ' + p.collected_offset_days + ')' : '')),
      h('div', { class: 'asc-mono', style: 'font-size:12px;white-space:pre-wrap' },
        (p.results || []).map((r) => (r.analyte || '?') + ': ' + (r.value != null ? r.value : '') + ' ' + (r.unit || '') + (r.flag ? ' [' + r.flag + ']' : '')).join('\n'))));
    const notes = (kase.notes || []).map((n) => h('div', { style: 'margin-bottom:8px' },
      h('div', { style: 'font-weight:600' }, (n.note_type || 'Note') + ' · ' + (n.author_role || '')),
      h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap;max-height:160px;overflow:auto' }, n.text || '')));
    const cands = (s.candidates || []).map((c) => h('div', { style: 'margin-bottom:8px' },
      h('div', { style: 'font-weight:600' }, 'Candidate ' + (c.id || '')),
      h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap;max-height:160px;overflow:auto' }, c.text || '')));

    const status = h('div', { style: 'margin-top:12px' });

    // Fan-out (V4 Cases & Promotion PRD §4). Two things get conflated here and
    // the copy exists to keep them apart: VISIBLE to every approved physician is
    // specialty routing, and LABELLED by every approved physician is max_labels.
    // This checkbox is the first one only. Defaulted OFF — specialty routing is a
    // quality control, and this suspends it deliberately rather than by accident.
    const fanoutBox = h('input', { type: 'checkbox', id: 'asc-promote-fanout' });
    const fanout = h('label', {
      class: 'asc-field',
      for: 'asc-promote-fanout',
      style: 'display:flex;gap:10px;align-items:flex-start;margin-top:14px;cursor:pointer',
    },
      fanoutBox,
      h('span', {},
        h('span', { style: 'font-weight:600' }, 'Show to all approved physicians (ignores specialty routing)'),
        h('span', { class: 'asc-card-sub', style: 'display:block' },
          'Visibility only. It does not change how many labels we pay for — that is ' +
          'the label count, which stays as promoted.')));

    const promoteAllBtn = h('button', { class: 'asc-btn asc-btn-primary' }, '✓ Looks good, create the rest (' + (prep.ingested_count || 0) + ')');
    promoteAllBtn.addEventListener('click', async () => {
      promoteAllBtn.setAttribute('disabled', ''); promoteAllBtn.textContent = 'Creating cases…';
      clear(status);
      try {
        const r = await api('/ingestion/uploads/' + upload.upload_id + '/promote-all', {
          method: 'POST',
          body: { open_to_all_specialties: !!fanoutBox.checked },
        });
        overlay.remove();
        clear(statusBox);
        statusBox.appendChild(h('div', { class: 'asc-inline-ok' },
          'Promoted ' + r.promoted + ' case(s) to V4' + (r.gated ? ' · ' + r.gated + ' gated' : '') + (r.failed ? ' · ' + r.failed + ' failed' : '') + '.'));
        toast('Promoted ' + r.promoted + ' case(s) to the V4 queue.', 'success');
        loadIngestionLists();
      } catch (e) {
        clear(status);
        status.appendChild(h('div', { class: 'asc-inline-error' }, typeof e.message === 'string' ? e.message : 'Promotion failed.'));
        promoteAllBtn.removeAttribute('disabled'); promoteAllBtn.textContent = '✓ Looks good, create the rest (' + (prep.ingested_count || 0) + ')';
      }
    });

    const popup = h('div', { class: 'call-team-popup', style: 'max-width:820px;max-height:90vh;overflow:auto;text-align:left', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'call-team-title' }, 'Review a sample case: ' + (prep.partner_label || upload.partner_id || 'partner')),
      h('div', { class: 'call-team-sub' }, (prep.filename || '') + ' · ' + (prep.ingested_count || 0) + ' case(s) in this file'),
      testBanner,
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Clinical question'),
        h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, s.question || 'n/a')),
      h('div', { class: 'asc-card-sub', style: 'margin:4px 0 14px' },
        (s.specialty || '') + ' · ' + (demo.age_band || '?') + ' ' + (demo.sex || '') + ' · ' +
        (kase.lab_panels || []).length + ' lab panel(s) · ' + (kase.notes || []).length + ' note(s)'
        // The difficulty band is a CLAIM the admin is approving. Say whether it was
        // measured against frontier models or is the structural prior.
        + ((s.difficulty && s.difficulty.band)
          ? (' · difficulty ' + s.difficulty.band
             + (s.difficulty.measured
               ? ' (measured, frontier failure ' + s.difficulty.model_failure_rate + ')'
               : ' (proposed — no frontier measurement)'))
          : '')),
      labs.length ? h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Lab panels'), h('div', {}, labs)) : null,
      notes.length ? h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Notes / EHR records'), h('div', {}, notes)) : null,
      cands.length ? h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Generated candidate answers'), h('div', {}, cands)) : null,
      fanout,
      status,
      h('div', { style: 'display:flex;gap:10px;margin-top:16px' },
        promoteAllBtn,
        h('button', { class: 'asc-btn asc-btn-ghost', style: 'margin-left:auto', onClick: () => overlay.remove() }, 'Cancel')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }


  // ─── Admin: Data & Task Creation (PRD ADMIN-TASKS §3) ──────────────────────
  //
  // Two boxes, and the page is the two boxes. Data comes IN (Box 1: what is
  // this, and is it ours to make tasks from), then data BECOMES tasks (Box 2:
  // static or longitudinal, preview, create). Everything the old Tasks tab
  // carried that was not one of those two questions moved to the surface that
  // owns it — Frontier-model failures to Metrics, where a measurement belongs —
  // or went away with its card while its endpoint stayed live.
  //
  // THE ONE-WAY DOOR. Box 1's two buttons are not symmetrical and the UI must
  // not imply they are. `task_creation` → `brokering` is always allowed: it
  // removes a promotion path. The reverse is refused by the server with a 409,
  // deliberately and permanently — data a partner sent us to broker never
  // enters the task pipeline, and no admin click can convert it. So Brokering
  // asks for confirmation and says what it costs, and a brokering row renders
  // its state as final rather than as a toggle somebody could flip back.
  function renderAdminTasks(body) {
    clear(body);
    const view = state.dataCreation || (state.dataCreation = {
      uploads: null, err: null, busy: false, doneOpen: false, previewFor: null,
    });

    const host = h('div', {});
    body.appendChild(host);

    function load() {
      view.busy = true; paint();
      api('/ingestion/uploads?limit=200')
        .then((res) => {
          view.uploads = res.uploads || []; view.err = null; view.busy = false; paint();
        })
        .catch((e) => { view.err = e.message; view.busy = false; paint(); });
    }

    // ─── Box 1: the purpose decision ────────────────────────────────────────
    function resolvePurpose(upload, purpose) {
      api('/admin/uploads/' + encodeURIComponent(upload.upload_id) + '/purpose',
          { method: 'POST', body: { purpose } })
        .then((res) => {
          toast(res.message || 'Recorded.');
          load();
        })
        .catch((e) => {
          // The 409 the server raises on brokering → task creation is a RULE,
          // not a failure. Render its sentence, which explains the rule.
          toast((e && e.detail) || e.message || 'Could not set the purpose.', 'error');
        });
    }

    function askBrokering(upload) {
      const overlay = h('div', {
        class: 'call-team-overlay is-open',
        onClick: (e) => { if (e.target === overlay) overlay.remove(); },
      });
      const go = h('button', { class: 'asc-btn asc-btn-primary' },
        'Yes — record as brokering');
      go.addEventListener('click', () => { overlay.remove(); resolvePurpose(upload, 'brokering'); });
      overlay.appendChild(h('div', { class: 'asc-modal-card' },
        h('div', { class: 'asc-card-pad' },
          h('h3', {}, 'Record this upload as brokering?'),
          h('div', { class: 'asc-inline-warn', style: 'margin:12px 0' },
            'This cannot be undone. Brokering data never enters the task '
            + 'pipeline, so this upload can never become tasks — the server '
            + 'refuses the reverse change, on purpose. If it turns out to be '
            + 'task-creation data, the partner has to send it again on a '
            + 'task-creation link.'),
          h('div', { class: 'asc-dim' },
            (upload.partner_label || 'Unknown sender') + ' · '
            + (upload.filename || 'bundle') + ' · '
            + ((upload.case_counts || {}).total || 0) + ' case(s)'),
          h('div', { style: 'display:flex;gap:10px;margin-top:16px' },
            go,
            h('button', {
              class: 'asc-btn asc-btn-ghost', style: 'margin-left:auto',
              onClick: () => overlay.remove(),
            }, 'Cancel')))));
      document.body.appendChild(overlay);
    }

    // ─── Box 2: making the tasks ────────────────────────────────────────────
    function setMode(upload, mode) {
      api('/admin/uploads/' + encodeURIComponent(upload.upload_id) + '/task-mode',
          { method: 'POST', body: { task_mode: mode } })
        .then(() => load())
        .catch((e) => toast((e && e.detail) || e.message || 'Could not set the mode.', 'error'));
    }

    /* Static: the two-step promote that already exists. `prepare` converts and
     * gates ONE case and hands it back for review; `promote-all` extends it to
     * the rest. The sample is not a formality — it is the only point at which a
     * human sees what a partner file actually turns into before the whole
     * bundle becomes physician work. */
    function previewStatic(upload, statusBox) {
      clear(statusBox);
      statusBox.appendChild(loadingCard('Converting one sample case…'));
      api('/ingestion/uploads/' + encodeURIComponent(upload.upload_id) + '/prepare',
          { method: 'POST', body: {} })
        .then((prep) => { clear(statusBox); openSampleReviewModal(upload, prep, statusBox); })
        .catch((e) => {
          clear(statusBox);
          statusBox.appendChild(h('div', { class: 'asc-inline-error' },
            (e && e.detail) || e.message || 'Could not prepare a sample.'));
        });
    }

    /* Longitudinal: one chart becomes one ordered walk. Runs per case and dry
     * first, because a trajectory is a different product with a different price
     * and a different labeling policy — the plan is what an admin approves, not
     * the flag that produced it. Points land assigned_only, invisible to every
     * doctor until Task Routing sends them. */
    function previewLongitudinal(upload, statusBox) {
      clear(statusBox);
      statusBox.appendChild(loadingCard('Planning the chart walk…'));
      api('/ingestion/uploads/' + encodeURIComponent(upload.upload_id))
        .then((full) => {
          const first = (full.cases || []).find((c) => c.status === 'ingested');
          if (!first) throw new Error('No ingested cases left to plan in this upload.');
          return api('/ingestion/cases/' + encodeURIComponent(first.ingest_case_id) + '/generate',
                     { method: 'POST', body: { dry_run: true, trajectory: true } })
            .then((plan) => {
              clear(statusBox);
              openCasePlanModal(upload, first, plan, statusBox, { trajectory: true });
            });
        })
        .catch((e) => {
          clear(statusBox);
          statusBox.appendChild(h('div', { class: 'asc-inline-error' },
            (e && e.detail) || e.message || 'Could not plan this chart.'));
        });
    }

    // ─── Rows ───────────────────────────────────────────────────────────────
    // ``withCounts`` off in Box 2: that row prints a richer count line of its
    // own immediately below, and §6.2 gives a row three lines, not four.
    function headerLines(u, withCounts) {
      const counts = u.case_counts || {};
      const bits = [
        u.partner_label || 'Unknown sender',
        u.created_at ? ('uploaded ' + fmtDate(u.created_at)) : null,
        u.filename || null,
        u.size_bytes ? (Math.round(u.size_bytes / 1048576) + ' MB') : null,
      ].filter(Boolean);
      const integrity = u.verified_at
        ? h('span', { class: 'asc-chip asc-chip-ok', title: 'Whole-file digest recomputed and matched' }, 'sha ✓')
        : h('span', { class: 'asc-chip', title: 'No verified whole-file digest on this row' }, 'sha —');
      return h('div', {},
        h('div', { class: 'asc-stage-head' }, bits.join(' · '), ' ', integrity),
        h('div', { class: 'asc-stage-desc' },
          (u.specialties || []).length
            ? h('span', { class: 'asc-badge asc-badge-primary' }, specialtiesLabel(u))
            // Amber, not primary. An unset specialty renders in the same slot as
            // a real one, and both promote endpoints 409 on it — so a chip that
            // looked like every other specialty would hide the one fact on the
            // row that stops the bundle progressing.
            : h('span', { class: 'asc-badge asc-badge-amber',
                title: 'Ingest refuses to guess a specialty, and both promote '
                     + 'endpoints refuse this upload until one is set.' },
                'specialty not set'),
          ' ',
          u.description
            ? h('span', {}, '“' + u.description + '”')
            : h('span', { class: 'asc-dim' }, 'No description was sent with this bundle.')),
        withCounts === false ? null : h('div', { class: 'asc-stage-counts' },
          (counts.total || 0) + ' ingest case(s)',
          counts.needs_review ? (' · ' + counts.needs_review + ' need review') : '',
          counts.quarantined ? (' · ' + counts.quarantined + ' quarantined') : '',
          counts.promoted ? (' · ' + counts.promoted + ' already tasks') : ''));
    }

    function specialtiesLabel(u) {
      const s = u.specialties || [];
      return s.length ? s.join(', ') : 'specialty not set';
    }

    function describeBtn(u) {
      const b = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
        u.description ? 'Edit description' : 'Add description');
      b.addEventListener('click', () => {
        const next = window.prompt('What is this data? (free text)', u.description || '');
        if (next == null) return;
        api('/admin/uploads/' + encodeURIComponent(u.upload_id) + '/description',
            { method: 'POST', body: { description: next } })
          .then(() => load())
          .catch((e) => toast(e.message || 'Could not save the description.', 'error'));
      });
      return b;
    }

    function previewCasesBtn(u) {
      const b = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' }, 'Preview cases');
      b.addEventListener('click', () => {
        view.previewFor = (view.previewFor === u.upload_id) ? null : u.upload_id;
        paint();
      });
      return b;
    }

    function casesDrawer(u) {
      if (view.previewFor !== u.upload_id) return null;
      const drawer = h('div', { class: 'asc-stage-drawer' }, loadingCard('Loading cases…'));
      /* The FULL case list, not the review queue. `/review` omits every case
       * with no review reason, so a clean 27-case bundle would open an empty
       * drawer captioned "Preview cases" — which reads as a broken button. */
      api('/ingestion/uploads/' + encodeURIComponent(u.upload_id))
        .then((full) => {
          clear(drawer);
          const cases = full.cases || [];
          if (!cases.length) {
            drawer.appendChild(h('div', { class: 'asc-dim' }, 'No cases parsed from this upload yet.'));
            return;
          }
          drawer.appendChild(h('table', { class: 'asc-table' },
            h('thead', {}, h('tr', {},
              h('th', {}, 'Case'), h('th', {}, 'Specialty'),
              h('th', {}, 'Status'), h('th', {}, 'Review'))),
            h('tbody', {}, cases.slice(0, 100).map((c) => h('tr', {},
              h('td', { class: 'asc-mono' }, (c.patient_key || c.ingest_case_id || '').slice(0, 18)),
              h('td', {}, c.specialty || '—'),
              h('td', {}, c.status || '—'),
              h('td', {}, (c.review || []).length
                ? h('span', { class: 'asc-badge asc-badge-amber' },
                    String((c.review || []).length) + ' reason(s)')
                : '—'))))));
        })
        .catch((e) => { clear(drawer); drawer.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); });
      return drawer;
    }

    function box1Row(u) {
      const toTasks = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button' }, 'Task creation');
      toTasks.addEventListener('click', () => resolvePurpose(u, 'task_creation'));
      // Bordered, not bare. The base .asc-btn carries `border: 1px solid
      // transparent`, so an unqualified button renders as plain text — and this
      // one commits an IRREVERSIBLE decision. Quiet is right for it; invisible
      // is not.
      const toBroker = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' }, 'Brokering');
      toBroker.addEventListener('click', () => askBrokering(u));

      const dl = h('a', {
        class: 'asc-btn asc-btn-ghost asc-btn-sm',
        href: '/api/asclepius/ingestion/uploads/' + encodeURIComponent(u.upload_id) + '/download',
      }, 'Download');

      return h('div', { class: 'asc-stage-row' },
        headerLines(u),
        h('div', { class: 'asc-stage-actions' },
          dl, previewCasesBtn(u), describeBtn(u),
          h('span', { class: 'asc-stage-spacer' }),
          h('span', { class: 'asc-dim' }, 'This data is for: '),
          toTasks, toBroker),
        casesDrawer(u));
    }

    /* §3: let this bundle build its tasks without a click.
     *
     * Off by default and armed per bundle, because a run costs a frontier probe,
     * a candidate generation and two judges PER ENCOUNTER: a 25-point chart that
     * started itself unasked is a bill nobody approved. What it does NOT change is
     * who can see the output: points still land assigned_only, so this removes a
     * click from BUILDING and never one from SENDING. */
    function autoGenerateToggle(u) {
      if (u.auto_generate_has_run) {
        return h('span', { class: 'asc-chip asc-chip-ok',
                           title: 'This bundle already had its one automatic run. '
                                + 'Promote anything left from the button on this row.' },
          'auto \u2713');
      }
      const input = h('input', { type: 'checkbox', checked: !!u.auto_generate });
      input.addEventListener('change', () => {
        input.setAttribute('disabled', '');
        api('/admin/uploads/' + encodeURIComponent(u.upload_id) + '/auto-generate',
            { method: 'POST', body: { enabled: input.checked } })
          .then((res) => {
            if (res.run && res.run.started) {
              toast('Building tasks from this bundle now \u2014 no click needed.', 'success');
            }
            load();
          })
          .catch((e) => {
            input.checked = !input.checked;
            input.removeAttribute('disabled');
            toast((e && e.detail) || e.message || 'Could not change that.', 'error');
          });
      });
      return h('label', { class: 'asc-stage-radio',
                          title: 'Build the tasks as soon as this bundle has a purpose '
                               + 'and a mode. Nothing is sent to a doctor either way.' },
        input, ' Build automatically');
    }

    /* The per-encounter drops a run isolated, as a COUNT with a `show` link.
     * Never a modal: 22 points built out of 25 is a result, not an error, and a
     * modal would present it as one. */
    function autoGenerateFailures(u) {
      const f = u.auto_generate_failures;
      if (!f || !f.count) return null;
      const detail = h('div', { class: 'asc-stage-drawer', hidden: true },
        (f.errors || []).map((e) => h('div', { class: 'asc-dim' }, e)),
        (f.dropped || []).map((d) => h('div', { class: 'asc-dim' },
          'encounter ' + (d.encounter_index == null ? '?' : d.encounter_index) + ': '
          + (typeof d.reason === 'string' ? d.reason : JSON.stringify(d.reason || 'dropped')))));
      const link = h('button', { class: 'asc-btn-link', type: 'button' }, 'show');
      link.addEventListener('click', () => {
        const showing = !detail.hasAttribute('hidden');
        if (showing) { detail.setAttribute('hidden', ''); link.textContent = 'show'; }
        else { detail.removeAttribute('hidden'); link.textContent = 'hide'; }
      });
      return h('div', {},
        h('div', { class: 'asc-dim' },
          f.count + ' encounter' + (f.count === 1 ? '' : 's')
          + ' did not become a task in the automatic run. ', link),
        detail);
    }

    function box2Row(u) {
      const counts = u.case_counts || {};
      const statusBox = h('div', { style: 'margin-top:10px' });
      const mode = u.task_mode || null;

      const radios = ['static', 'longitudinal'].map((m) => {
        const input = h('input', {
          type: 'radio', name: 'mode-' + u.upload_id, value: m,
          checked: mode === m, disabled: !!counts.promoted && mode !== m,
        });
        input.addEventListener('change', () => { if (input.checked) setMode(u, m); });
        return h('label', { class: 'asc-stage-radio' }, input,
          m === 'static' ? ' Static real cases' : ' Longitudinal real cases');
      });

      const eligible = counts.ingested || 0;
      const create = h('button', {
        class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button',
        disabled: !mode || !eligible,
      }, mode === 'longitudinal'
        ? ('Build the chart walk →')
        : ('Create ' + eligible + ' task' + (eligible === 1 ? '' : 's') + ' →'));
      create.addEventListener('click', () => {
        if (mode === 'longitudinal') previewLongitudinal(u, statusBox);
        else previewStatic(u, statusBox);
      });

      const hint = !mode
        ? h('div', { class: 'asc-dim' }, 'Choose a mode — it is stored on this upload, so a half-finished batch resumes the same way.')
        : (!eligible
          ? h('div', { class: 'asc-dim' }, 'No ingested cases are waiting: every case here is already a task, blocked in review, or quarantined.')
          : null);

      return h('div', { class: 'asc-stage-row' },
        headerLines(u, false),
        h('div', { class: 'asc-stage-counts' },
          (counts.total || 0) + ' case(s) · ' + eligible + ' eligible'
          + (counts.needs_review ? (' · ' + counts.needs_review + ' blocked (review)') : '')
          + ' · ' + (counts.promoted || 0) + ' made into tasks'),
        h('div', { class: 'asc-stage-actions' },
          h('span', { class: 'asc-dim' }, 'Make tasks as: '), radios,
          autoGenerateToggle(u),
          h('span', { class: 'asc-stage-spacer' }),
          previewCasesBtn(u), create),
        u.auto_generate_will_run && !u.auto_generate_has_run
          ? h('div', { class: 'asc-dim' },
              'This bundle builds its tasks on its own. They land held back from '
              + 'every queue; you still choose who walks them, in Task Routing.')
          : null,
        autoGenerateFailures(u),
        hint,
        casesDrawer(u),
        statusBox);
    }

    function doneRow(u) {
      const counts = u.case_counts || {};
      const go = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
        'view in Task Routing →');
      go.addEventListener('click', () => {
        state.adminSub.work = 'assign';
        renderAdminView();
      });
      return h('div', { class: 'asc-stage-row is-done' },
        h('div', { class: 'asc-stage-head' },
          '✓ ' + (counts.promoted || 0) + ' task(s) created · '
          + (u.partner_label || 'Unknown sender') + ' · ' + (u.filename || 'bundle')
          + (u.task_mode ? (' · ' + u.task_mode) : '')),
        go);
    }

    // ─── Paint ──────────────────────────────────────────────────────────────
    function paint() {
      clear(host);
      if (view.err) host.appendChild(h('div', { class: 'asc-inline-error' }, view.err));
      if (view.busy && !view.uploads) { host.appendChild(loadingCard('Loading incoming data…')); return; }

      const all = view.uploads || [];
      const box1 = all.filter((u) => u.staging === 'undecided');
      const box2 = all.filter((u) => u.staging === 'task_creation' && !u.task_creation_complete);
      const done = all.filter((u) => u.staging === 'task_creation' && u.task_creation_complete);

      host.appendChild(h('div', { class: 'asc-stage-toolbar' },
        h('div', {},
          h('h2', { class: 'asc-stage-title' }, 'Data & task creation'),
          h('div', { class: 'asc-dim' },
            'Data arrives, you say what it is for, then you turn it into tasks.')),
        uploadButton()));

      // Box 1
      const b1 = h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-stage-actions' },
          h('h3', {}, 'Incoming data'),
          h('span', { class: 'asc-stage-spacer' }),
          ingestFixturesBtn()),
        h('div', { class: 'asc-dim' },
          'Every bundle whose purpose has not been decided yet. Choosing '
          + '“Brokering” cannot be undone.'),
        box1.length
          ? h('div', { class: 'asc-stage-list' }, box1.map(box1Row))
          : h('div', { class: 'asc-empty' }, h('p', {}, 'Nothing waiting on a decision.'))));
      host.appendChild(b1);

      // Box 2
      const b2 = h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('h3', {}, 'Task creation'),
        h('div', { class: 'asc-dim' },
          'Bundles cleared for task creation, with cases still to turn into tasks.'),
        box2.length
          ? h('div', { class: 'asc-stage-list' }, box2.map(box2Row))
          : h('div', { class: 'asc-empty' }, h('p', {}, 'No bundles waiting to become tasks.')),
        done.length ? doneFold(done) : null));
      host.appendChild(b2);

      host.appendChild(autoGenerateCard());
    }

    /* §2.1: the front door for the records we already hold.
     *
     * The four de-identified patient charts the V4 static cases were written
     * FROM had never been uploaded, so no ingest case existed for them and
     * `generate`, the only path that creates trajectories, had nothing to run
     * on. This sends them through the same door a hospital's bundle takes.
     *
     * Small and in the header, not a primary action: it is a one-time operator
     * task, and it must not compete with the Upload button that hospitals'
     * data actually arrives through. Idempotent server-side on the bundle
     * sha256, so a second click reports "already ingested" rather than
     * duplicating four charts.
     */
    function ingestFixturesBtn() {
      const b = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
        'Ingest committed patient records');
      b.addEventListener('click', () => {
        b.setAttribute('disabled', '');
        b.textContent = 'Sending through the ingest door\u2026';
        api('/admin/fixtures/ingest-patient-records', { method: 'POST', body: {} })
          .then((res) => {
            toast(res.message || 'Done.', (res.failed || 0) ? 'error' : 'success');
            // Name the failures rather than only counting them: "1 failed" with
            // no bundle name is a number an operator cannot act on.
            (res.bundles || []).filter((r) => r.status === 'failed').forEach((r) => {
              toast(r.bundle + ': ' + (r.error || 'failed'), 'error');
            });
            load();
          })
          .catch((e) => toast((e && e.detail) || e.message || 'Could not ingest the records.', 'error'))
          .finally(() => {
            b.removeAttribute('disabled');
            b.textContent = 'Ingest committed patient records';
          });
      });
      return b;
    }

    /* Finished bundles fold rather than disappear. A row that vanishes on
     * completion takes its history with it, and "what did we make from St
     * Mary's in August" stops being answerable on the screen that made it. */
    function doneFold(done) {
      const list = h('div', { class: 'asc-stage-list' }, done.map(doneRow));
      list.hidden = !view.doneOpen;
      const toggle = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
        (view.doneOpen ? '▾ ' : '▸ ') + 'Done (' + done.length + ')');
      toggle.addEventListener('click', () => {
        view.doneOpen = !view.doneOpen;
        list.hidden = !view.doneOpen;
        toggle.textContent = (view.doneOpen ? '▾ ' : '▸ ') + 'Done (' + done.length + ')';
      });
      return h('div', { class: 'asc-stage-done' }, toggle, list);
    }


    /* §3.3 — one Upload button, one modal, a REQUIRED "what is this".
     *
     * The mode picker is required because the four answers go to four different
     * places, and guessing wrong is expensive in both directions: a real bundle
     * treated as a task file is a parse error, and a task file treated as a real
     * bundle is PHI handling we did not need to do.
     *
     * Real records go through the PARTNER DOOR — mint a link, post to it — and
     * not through a second admin-only ingest endpoint. That door fails closed on
     * unconfigured encryption and on non-durable storage, and a second door
     * would have to reproduce both exactly or quietly become the unsafe way in.
     * One door for raw bundles is a property worth keeping, so the admin path
     * borrows it rather than bypassing it.
     *
     * The mode picker already declares intent ("real records, longitudinal"), so
     * an admin upload lands in Box 2 with that mode set, rather than in Box 1
     * asking a question it was just answered.
     */
    function uploadButton() {
      const b = h('button', { class: 'asc-btn asc-btn-primary', type: 'button' }, 'Upload');
      b.addEventListener('click', openUploadModal);
      return b;
    }

    function openUploadModal() {
      const overlay = h('div', {
        class: 'call-team-overlay is-open',
        onClick: (e) => { if (e.target === overlay) overlay.remove(); },
      });
      const status = h('div', { style: 'margin-top:12px' });

      const MODES = [
        ['real_static', 'Real records — static',
          'A partner bundle. Each qualifying encounter becomes one standalone V4 case.'],
        ['real_longitudinal', 'Real records — longitudinal',
          'A partner bundle. Each chart becomes ONE ordered walk of decision points, '
          + 'held back from every queue until you route it.'],
        ['gold', 'Physician-authored cases (gold)',
          'The ratified, hand-authored seed cases. No file and no LLM: this loads what is '
          + 'already committed, and is safe to run repeatedly.'],
        ['task_file', 'Task file (JSON/CSV)',
          'Already-formed tasks. They go straight to Task Routing — there is nothing to stage.'],
      ];
      let mode = null;

      const fileInput = h('input', { type: 'file', accept: '.zip,.json,.csv,.hl7,.txt', class: 'asc-input' });
      const fileField = h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'File'), fileInput);
      const specInput = h('input', { class: 'asc-input', value: 'nephrology' });
      const specField = h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Specialty'), specInput,
        h('div', { class: 'asc-dim' },
          'Ingest refuses to guess: a wrong specialty routes the case to the wrong '
          + 'pool and mislabels it in the export, invisibly.'));
      const descInput = h('input', { class: 'asc-input', placeholder: 'What am I looking at?' });
      const descField = h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Description'), descInput);
      const go = h('button', { class: 'asc-btn asc-btn-primary', disabled: true }, 'Upload');

      function syncMode() {
        const needsFile = mode === 'real_static' || mode === 'real_longitudinal' || mode === 'task_file';
        const needsSpec = mode === 'real_static' || mode === 'real_longitudinal' || mode === 'gold';
        fileField.hidden = !needsFile;
        specField.hidden = !needsSpec;
        descField.hidden = !(mode === 'real_static' || mode === 'real_longitudinal');
        if (mode) go.removeAttribute('disabled'); else go.setAttribute('disabled', '');
        go.textContent = mode === 'gold' ? 'Load gold cases' : 'Upload';
      }

      const picker = h('div', { class: 'asc-mode-picker' }, MODES.map(([id, label, sub]) => {
        const input = h('input', { type: 'radio', name: 'asc-upload-mode', value: id });
        input.addEventListener('change', () => { if (input.checked) { mode = id; syncMode(); } });
        return h('label', { class: 'asc-mode-option' }, input,
          h('span', {}, h('span', { class: 'asc-mode-label' }, label),
            h('span', { class: 'asc-mode-sub' }, sub)));
      }));

      go.addEventListener('click', () => {
        clear(status);
        go.setAttribute('disabled', '');
        const done = (node) => { clear(status); status.appendChild(node); go.removeAttribute('disabled'); };
        const fail = (e) => done(h('div', { class: 'asc-inline-error' },
          (e && e.detail) || (e && e.message) || 'Upload failed.'));

        if (mode === 'gold') {
          status.appendChild(loadingCard('Loading ratified gold cases…'));
          api('/generation/' + encodeURIComponent(specInput.value.trim() || 'nephrology') + '/load-gold',
              { method: 'POST' })
            .then((res) => {
              overlay.remove();
              toast('Loaded ' + (res.loaded || 0) + ' gold case(s), skipped '
                + (res.skipped || 0) + ' already present.', 'success');
              load();
            })
            .catch(fail);
          return;
        }

        if (!fileInput.files || !fileInput.files[0]) {
          done(h('div', { class: 'asc-inline-error' }, 'Choose a file first.'));
          return;
        }

        if (mode === 'task_file') {
          const fd = new FormData();
          fd.append('file', fileInput.files[0]);
          status.appendChild(loadingCard('Uploading task file…'));
          api('/tasks/upload-file', { method: 'POST', body: fd, isForm: true })
            .then((res) => {
              overlay.remove();
              toast('Created ' + (res.count || 0) + ' task(s). They are in Task Routing.', 'success');
            })
            .catch(fail);
          return;
        }

        // Real records: mint a one-time link, then post through the partner door.
        const wantMode = (mode === 'real_longitudinal') ? 'longitudinal' : 'static';
        status.appendChild(loadingCard('Uploading through the ingest door…'));
        api('/admin/upload-links', { method: 'POST', body: {
          partner_id: 'admin-upload',
          partner_label: 'Uploaded by admin',
          specialty: specInput.value.trim() || 'nephrology',
          purpose: 'task_creation',
          one_time: true,
        } }).then((link) => {
          const fd = new FormData();
          fd.append('file', fileInput.files[0]);
          fd.append('description', descInput.value.trim());
          return api('/partner/uploads?t=' + encodeURIComponent(link.token),
                     { method: 'POST', body: fd, isForm: true })
            .then((up) => api('/admin/uploads/' + encodeURIComponent(up.upload_id) + '/task-mode',
                              { method: 'POST', body: { task_mode: wantMode } })
              .catch(() => null)   // the bundle is in; a mode we can set later must not read as a failed upload
              .then(() => up));
        }).then(() => {
          overlay.remove();
          toast('Uploaded. Parsing runs in the background — the row appears under '
            + 'Task creation as its cases land.', 'success');
          load();
        }).catch(fail);
      });

      syncMode();
      overlay.appendChild(h('div', { class: 'asc-modal-card' },
        h('div', { class: 'asc-card-pad' },
          h('h3', {}, 'Upload'),
          h('div', { class: 'asc-label', style: 'margin-top:12px' }, 'What is this?'),
          picker, specField, descField, fileField, status,
          h('div', { style: 'display:flex;gap:10px;margin-top:16px' },
            go,
            h('button', {
              class: 'asc-btn asc-btn-ghost', style: 'margin-left:auto',
              onClick: () => overlay.remove(),
            }, 'Cancel')))));
      document.body.appendChild(overlay);
    }

    /* §3.4 — synthetic generation, as ONE compact card and no jobs table.
     * Its output is a task the moment it exists, so the status line links to
     * Task Routing rather than growing a second inventory here. */
    function autoGenerateCard() {
      const spec = selectFrom(['nephrology', 'cardiology'], 'nephrology');
      const count = h('input', { type: 'number', class: 'asc-input', value: '10', min: '1', max: '200' });
      const status = h('div', { style: 'margin-top:10px' });
      const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' }, 'Generate');
      btn.addEventListener('click', () => {
        clear(status);
        const n = Math.max(1, parseInt(count.value, 10) || 1);
        btn.setAttribute('disabled', '');
        status.appendChild(loadingCard('Generating ' + n + ' case(s)… this calls the LLM.'));
        api('/generation/' + encodeURIComponent(spec.value), { method: 'POST', body: { count: n } })
          .then((res) => {
            clear(status);
            const link = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
              'View in Task Routing →');
            link.addEventListener('click', () => { state.adminSub.work = 'assign'; renderAdminView(); });
            status.appendChild(h('div', { class: 'asc-inline-ok' },
              'Accepted ' + (res.accepted || 0) + ' of ' + n + '.'
              + ((res.shortfall || 0) ? ' Shortfall ' + res.shortfall + '.' : '')));
            const dropped = res.dropped || {};
            const dk = Object.keys(dropped).filter((k) => dropped[k] > 0);
            if (dk.length) {
              status.appendChild(h('div', { class: 'asc-dim' }, 'Dropped: '
                + dk.map((k) => k.replace(/_/g, ' ') + ' ' + dropped[k]).join(' · ')));
            }
            status.appendChild(link);
          })
          .catch((e) => {
            clear(status);
            status.appendChild(h('div', { class: 'asc-inline-error' },
              e.status === 503 ? (e.message || 'No LLM key configured.') : e.message));
          })
          .finally(() => btn.removeAttribute('disabled'));
      });
      return h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('h3', {}, 'Auto-generate (synthetic V1–V3)'),
        h('div', { class: 'asc-dim' },
          'Novel synthetic cases from the seed corpus, quality-gated. They are tasks '
          + 'the moment they exist, so they appear in Task Routing, not here.'),
        h('div', { class: 'asc-stage-actions' },
          h('span', { class: 'asc-dim' }, 'Specialty '), spec,
          h('span', { class: 'asc-dim' }, ' How many '), count,
          h('span', { class: 'asc-stage-spacer' }), btn),
        status));
    }

    load();
  }

  // Model-Failure view (FEAT-1): per-model failure summary + the individual
  // "model failed here, expert corrected it" cases, filterable by model.
  async function loadModelFailures(model) {
    const card = document.getElementById('ascModelFailures');
    if (!card) return;
    try {
      const q = model ? ('?model=' + encodeURIComponent(model)) : '';
      const data = await api('/baselines/model-failures' + q);
      clear(card);
      const summary = data.summary || [];
      const failures = data.failures || [];
      card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Frontier-model failures'),
        h('div', { class: 'asc-card-sub' }, 'Cases a real frontier model got wrong, with the expert correction, the artifact to put in front of a lab.'))));
      if (!summary.length && !failures.length) {
        card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-card-sub' },
          'No model failures captured yet. On a task, admins can "Grade the real models" to A/B two real frontier answers; a rejected model is recorded here.')));
        return;
      }
      const pad = h('div', { class: 'asc-card-pad' });
      // Provider rollup headline ("OpenAI failed N; Anthropic failed K"); the
      // two-frontier lab-facing framing.
      const byProv = data.by_provider || {};
      const provKeys = Object.keys(byProv);
      if (provKeys.length) {
        pad.appendChild(h('div', { style: 'display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px' },
          ...provKeys.map((p) => h('span', { class: 'asc-chip', style: 'font-weight:700' },
            (p === 'openai' ? 'OpenAI' : p === 'anthropic' ? 'Anthropic' : p) +
            ' failed ' + (byProv[p].failures || 0)))));
      }
      // Summary chips (click to filter).
      const chipRow = h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px' });
      chipRow.appendChild(h('button', { class: 'asc-chip' + (model ? '' : ' active'), type: 'button',
        onClick: () => loadModelFailures(null) }, 'All'));
      summary.forEach((sm) => {
        chipRow.appendChild(h('button', { class: 'asc-chip' + (model === sm.model ? ' active' : ''), type: 'button',
          onClick: () => loadModelFailures(sm.model) }, sm.model + ' (' + sm.failures + ')'));
      });
      pad.appendChild(chipRow);
      failures.slice(0, 40).forEach((f) => {
        pad.appendChild(h('div', { class: 'asc-readbox', style: 'margin-bottom:10px' },
          h('div', { style: 'font-weight:700;margin-bottom:4px' }, f.model,
            (f.error_tags || []).map((t) => h('span', { class: 'asc-chip asc-chip-warn', style: 'margin-left:6px' }, t))),
          h('div', { class: 'asc-label-hint', style: 'margin-bottom:6px;white-space:pre-wrap' }, (f.prompt || '').slice(0, 400)),
          h('div', {}, h('strong', {}, 'Expert correction: '), (f.expert_correction || 'n/a'))));
      });
      card.appendChild(pad);
    } catch (e) {
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message)));
    }
  }

  // §4.2 two-frontier provenance (ADMIN-ONLY; ab_meta/needs_baseline exist only
  // on the admin /tasks payload; the blinded evaluator payload never carries a
  // provider). Shows which provider filled each blinded slot and whether both
  // answers share one prompt_hash, plus a held-task "needs baseline" alert.
  function baselineCell(t) {
    const cell = h('div', {}, gradeRealModelsBtn(t));
    const meta = t.ab_meta;
    if (meta && (meta.candidates || []).length) {
      cell.appendChild(h('div', { class: 'asc-card-sub asc-mono', style: 'margin-top:4px' },
        meta.candidates.map((c) => (c.id || '?') + ':' + (c.provider || '?')).join(' · ')));
      const flags = h('div', { style: 'margin-top:2px;display:flex;gap:4px;flex-wrap:wrap' });
      flags.appendChild(meta.prompt_hash_match
        ? h('span', { class: 'asc-badge asc-badge-green',
            title: 'Both answers were produced from byte-identical input (one prompt_hash).' }, 'same prompt ✓')
        : h('span', { class: 'asc-badge asc-badge-red',
            title: 'prompt_hash mismatch: the pair compares prompts, not models. It should have been discarded.' }, 'prompt divergence'));
      if (!meta.two_providers) {
        flags.appendChild(h('span', { class: 'asc-badge asc-badge-amber',
          title: 'Both slots came from one provider (legacy/fallback pair), not a two-frontier comparison.' }, 'one provider'));
      }
      cell.appendChild(flags);
    }
    if (t.needs_baseline) {
      cell.appendChild(h('div', { style: 'margin-top:4px' },
        h('span', { class: 'asc-badge asc-badge-amber',
          title: 'No two-frontier pair could be assembled (e.g. OPENAI_API_KEY unset or a provider down). The task is HELD; never silently served two same-provider answers.' },
          'needs baseline')));
    }
    return cell;
  }

  function gradeRealModelsBtn(t) {
    const btn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
      title: 'Answer this case cold with frontier models, then A/B two real answers' },
      'Grade real');
    btn.addEventListener('click', async () => {
      if (!window.confirm('Run the configured frontier models on this case and replace the A/B pair with two REAL model answers?')) return;
      btn.setAttribute('disabled', ''); btn.textContent = 'Running…';
      try {
        const res = await api('/tasks/' + t.task_id + '/grade-real-models', { method: 'POST' });
        toast('Swapped in ' + (res.candidate_count || 0) + ' real-model answers. This task now grades the real models.', 'success');
      } catch (e) {
        toast(e.status === 503 ? (e.message || 'Baseline generation unavailable (no LLM key?).') : e.message, 'error');
      } finally { btn.removeAttribute('disabled'); btn.textContent = 'Grade real'; }
    });
    return btn;
  }

  // ─── Admin: Buyers & Requests: REMOVED (Export & Approval PRD §5) ─────────
  // `renderAdminBuyers` and the buyer-request forms that lived here are gone
  // with the endpoints behind them (`/buyers`, `/buyer-requests*`). The tables
  // stay and their rows are untouched (docs/asclepius/BUYER_CRM_RETIRED.md).
  // The one part that mattered, "build a bundle and send it to a buyer", is now
  // the Export tab's `Export + send to \u25be`, attached to the export it actually
  // built rather than to a second, differently-filtered one.

  // `refreshExportReadyCount` and `quickExportAll` / `approveAllAndExport` are
  // REMOVED with the Export-history sub-view that hosted them (PRD §2). The
  // headline count they maintained ("142 records waiting") was the number that
  // could not be trusted: it counted export-ready records and said nothing about
  // the submissions sitting unapproved behind them. The Export builder's preview
  // is its replacement, and it reports both halves.

  // `renderAdminExports`, the old "Export history" sub-view, is REMOVED
  // (Export & Approval PRD §2). It carried four separate ways to cut a bundle
  // (quick-export-all, an export-by-version cohort, a per-contributor export,
  // and the history list), none of which previewed what would actually ship.
  // All four are now scopes of the ONE Export builder in admin_export.js:
  //   quick export all  -> scope "All"
  //   version cohort    -> scope "Version"
  //   per contributor   -> scope "Physician"
  // The history list itself (`loadExportsHistory`) and the contributor browser
  // (which is also where a Further Credential Summary is generated) survive and
  // are mounted UNDER the builder via `adminSectionCtx`.

  // ═══════════════════════════════════════════════════════════════════════════
  //  Contributors browser: shared org → contributor drill-down used by both the
  //  Exports tab (mode 'export': Export Data + Further Credential Summary) and
  //  the Metrics tab (mode 'metrics': per-org / per-contributor metric tiles).
  // ═══════════════════════════════════════════════════════════════════════════
  function renderOrgContribBrowser(card, mode) {
    const nav = state.browse[mode];
    clear(card);
    if (nav.level === 'orgs') return renderOrgList(card, mode);
    if (nav.level === 'org') return renderOrgDetail(card, mode);
    if (nav.level === 'contributor') return renderContributorDetail(card, mode);
  }

  function browseTitle(mode) {
    return mode === 'export'
      ? 'Contributors & credential summaries'
      : 'Metrics by organization & contributor';
  }
  function browseSub(mode) {
    // Not a competing export surface: the builder above is that, and its
    // Physician scope covers the per-contributor cut. What only lives HERE is
    // the organization-wide cut and the Further Credential Summary: the NDA
    // dossier a buyer asks for when they want to verify who labelled their data.
    return mode === 'export'
      ? 'Browse every contributor by organization. Export everything one organization labelled, or open a contributor to generate a Further Credential Summary (released under NDA / non-circumvention).'
      : 'Drill from overall metrics into a single organization, then a single contributor, including when they last labelled.';
  }

  async function renderOrgList(card, mode) {
    clear(card);
    card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
      h('div', { class: 'asc-card-title' }, browseTitle(mode)),
      h('div', { class: 'asc-card-sub' }, browseSub(mode)))));
    const listBox = h('div', { class: 'asc-card-pad' }, loadingCard('Loading organizations…'));
    card.appendChild(listBox);
    let orgs;
    try {
      const path = mode === 'export' ? '/organizations' : '/metrics/organizations';
      orgs = (await api(path)).organizations || [];
    } catch (e) { clear(listBox); listBox.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); return; }
    clear(listBox);
    if (!orgs.length) { listBox.appendChild(h('div', { class: 'asc-empty' }, h('p', {}, 'No contributors yet.'))); return; }
    const statusBox = h('div', { style: 'margin-bottom:10px' });
    listBox.appendChild(statusBox);
    orgs.forEach((o) => {
      const open = () => { state.browse[mode] = { level: 'org', org: o.organization, idHashed: null, contributor: null }; renderOrgContribBrowser(card, mode); };
      const meta = [
        o.contributor_count + ' contributor' + (o.contributor_count === 1 ? '' : 's'),
        o.record_count + ' record' + (o.record_count === 1 ? '' : 's') + ' labelled',
        'last labelled ' + fmtDate(o.last_labeled_at),
      ];
      const right = mode === 'export'
        ? h('button', {
            class: 'asc-btn asc-btn-primary asc-btn-sm',
            onClick: (ev) => { ev.stopPropagation(); exportOrg(o.organization, statusBox); },
          }, '⬇ Export all org data')
        : h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', onClick: open }, 'View →');
      listBox.appendChild(h('div', { class: 'asc-browse-row', onClick: open },
        h('div', { class: 'asc-browse-main' },
          h('div', { class: 'asc-browse-name' }, o.organization),
          h('div', { class: 'asc-browse-meta' }, meta.join(' · '))),
        right));
    });
  }

  async function renderOrgDetail(card, mode) {
    const org = state.browse[mode].org;
    clear(card);
    const back = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm',
      onClick: () => { state.browse[mode] = { level: 'orgs', org: null, idHashed: null, contributor: null }; renderOrgContribBrowser(card, mode); } }, '← All organizations');
    const head = h('div', { class: 'asc-card-head' }, h('div', {},
      h('div', { class: 'asc-browse-crumb' }, back),
      h('div', { class: 'asc-card-title' }, org)));
    if (mode === 'export') {
      const statusBox = h('div', {});
      head.appendChild(h('div', {},
        h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', onClick: () => exportOrg(org, statusBox) }, '⬇ Export all org data')));
      card.appendChild(head);
      card.appendChild(h('div', { class: 'asc-card-pad', style: 'padding-bottom:0' }, statusBox));
    } else {
      card.appendChild(head);
    }

    const listBox = h('div', { class: 'asc-card-pad' }, loadingCard('Loading contributors…'));
    card.appendChild(listBox);
    let contributors;
    try {
      const path = (mode === 'export' ? '/contributors' : '/metrics/contributors') + '?organization=' + encodeURIComponent(org);
      contributors = (await api(path)).contributors || [];
    } catch (e) { clear(listBox); listBox.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); return; }
    clear(listBox);
    if (mode === 'metrics') listBox.appendChild(orgMetricTiles(contributors, org));
    if (!contributors.length) { listBox.appendChild(h('div', { class: 'asc-empty' }, h('p', {}, 'No contributors in this organization.'))); return; }
    contributors.forEach((c) => {
      const open = () => { state.browse[mode] = { level: 'contributor', org, idHashed: c.id_hashed, contributor: c }; renderOrgContribBrowser(card, mode); };
      const role = c.role_title || (c.degree ? c.degree : (c.role || 'contributor'));
      const meta = [
        role,
        c.primary_specialty || c.specialty || 'n/a',
        c.record_count + ' record' + (c.record_count === 1 ? '' : 's'),
        'last labelled ' + fmtDate(c.last_labeled_at),
      ];
      listBox.appendChild(h('div', { class: 'asc-browse-row', onClick: open },
        h('div', { class: 'asc-browse-main' },
          h('div', { class: 'asc-browse-name' },
            (c.display_name || c.id_hashed),
            c.is_mock ? h('span', { class: 'asc-badge asc-badge-amber', style: 'margin-left:8px' }, 'Mock Contributor Account') : null,
            c.credentials_verified ? h('span', { class: 'asc-badge asc-badge-green', style: 'margin-left:8px' }, 'verified ✓') : null),
          h('div', { class: 'asc-browse-meta' }, meta.join(' · '))),
        h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', onClick: open }, 'Open →')));
    });
  }

  function orgMetricTiles(contributors, org) {
    const sum = (k) => contributors.reduce((a, c) => a + (Number(c[k]) || 0), 0);
    const last = contributors.reduce((a, c) => (c.last_labeled_at && (!a || c.last_labeled_at > a) ? c.last_labeled_at : a), null);
    return h('div', { class: 'asc-stat-grid', style: 'margin-bottom:16px' },
      stat(contributors.length, 'Contributors', null, true),
      stat(sum('submission_count'), 'Submissions'),
      stat(sum('record_count'), 'Records labelled'),
      stat(sum('grounded_submissions'), 'Grounded subs'),
      stat(Math.round(sum('total_hours') * 10) / 10 + 'h', 'Total hours'),
      stat(fmtDate(last), 'Last labelled'));
  }

  async function renderContributorDetail(card, mode) {
    const { org, idHashed, contributor } = state.browse[mode];
    clear(card);
    const back = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm',
      onClick: () => { state.browse[mode] = { level: 'org', org, idHashed: null, contributor: null }; renderOrgContribBrowser(card, mode); } }, '← ' + org);
    card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
      h('div', { class: 'asc-browse-crumb' }, back))));
    const pad = h('div', { class: 'asc-card-pad' }, loadingCard('Loading profile…'));
    card.appendChild(pad);

    if (mode === 'metrics') return renderContributorMetrics(pad, contributor);

    // Export mode: fetch the full profile (blurb + 2 buttons).
    let prof;
    try { prof = await api('/contributors/' + encodeURIComponent(idHashed)); }
    catch (e) { clear(pad); pad.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); return; }
    clear(pad);
    const cr = prof.credentials || {};
    const c = prof.contributor || {};
    // Admin-visible identity: real name + email (never ships in an export).
    const displayName = c.full_name || c.display_name || idHashed;
    pad.appendChild(h('div', { class: 'asc-profile-head' },
      h('div', { class: 'asc-profile-avatar', 'aria-hidden': 'true' }),
      h('div', {},
        h('div', { class: 'asc-profile-name' }, displayName,
          c.is_mock ? h('span', { class: 'asc-badge asc-badge-amber', style: 'margin-left:10px' }, 'Mock Contributor Account') : null,
          cr.credentials_verified ? h('span', { class: 'asc-badge asc-badge-green', style: 'margin-left:10px' }, 'verified ✓') : null),
        c.email ? h('div', { class: 'asc-card-sub asc-mono', style: 'margin-top:2px' }, c.email) : null,
        h('div', { class: 'asc-meta-row', style: 'margin-top:6px' },
          h('span', { class: 'asc-badge asc-badge-primary' }, cr.role_title || 'n/a'),
          h('span', { class: 'asc-badge asc-badge-gray' }, (cr.ship && cr.ship.primary_specialty) || c.primary_specialty || 'n/a'),
          h('span', { class: 'asc-badge asc-badge-gray' }, 'id ' + (c.id_hashed || '').slice(0, 12)),
          h('span', { class: 'asc-badge asc-badge-amber' }, (c.record_count || 0) + ' records')))));
    pad.appendChild(h('div', { class: 'asc-blurb' }, prof.blurb || 'n/a'));

    // Tier A attribute chips (what ships).
    const ship = cr.ship || {};
    const chips = [];
    if (ship.degree) chips.push(ship.degree);
    if (ship.years_in_active_practice) chips.push('~' + ship.years_in_active_practice + ' yrs practice');
    if (ship.practice_setting_type) chips.push(String(ship.practice_setting_type).replace(/_/g, ' '));
    (ship.subspecialties || []).forEach((s) => chips.push(s));
    (ship.languages || []).forEach((l) => chips.push(l));
    if (chips.length) {
      pad.appendChild(h('div', { class: 'asc-chip-row' }, chips.map((t) => h('span', { class: 'asc-chip' }, t))));
    }

    if (c.is_mock) {
      pad.appendChild(h('div', { class: 'asc-grounding-banner', style: 'margin-top:14px' },
        h('div', { class: 'asc-gb-icon', 'aria-hidden': 'true' }),
        h('div', {},
          h('div', { class: 'asc-gb-title' }, 'Sandbox account, excluded from exports'),
          h('div', { class: 'asc-gb-text' }, 'This is the Mock Contributor Account. Its submissions are hard-excluded from every export batch by default so a demo never contaminates a shipped dataset.'))));
    }
    const statusBox = h('div', { style: 'margin-top:14px' });
    pad.appendChild(h('div', { class: 'asc-profile-actions' },
      h('button', { class: 'asc-btn asc-btn-primary',
        onClick: (ev) => exportContributor(idHashed, statusBox, ev.target) }, '⬇ Export Data'),
      h('button', { class: 'asc-btn asc-btn-secondary',
        onClick: () => openCredentialSummaryModal(idHashed, c.display_name || idHashed) }, 'Further Credential Summary')));
    pad.appendChild(h('p', { class: 'asc-label-hint', style: 'margin-top:8px' },
      'Export Data ships credential attributes only (no identifying info). Further Credential Summary releases the full verification dossier under NDA / non-circumvention.'));
    pad.appendChild(statusBox);

    // ── Time-windowed exports (today / this week / all-time, Pacific) ─────────
    const winStatus = h('div', { style: 'margin-top:10px' });
    pad.appendChild(h('div', { style: 'margin-top:18px;border-top:1px solid var(--asc-line);padding-top:16px' },
      h('div', { class: 'asc-card-title', style: 'font-size:15px' }, 'Export by time window'),
      h('div', { class: 'asc-card-sub', style: 'margin-bottom:10px' }, 'Package just what this contributor labelled in a window (Pacific time).'),
      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' },
        h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
          onClick: (ev) => exportContributorWindow(idHashed, { since: windowSinceISO(0) }, 'today', winStatus, ev.target) }, '⬇ Today'),
        h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
          onClick: (ev) => exportContributorWindow(idHashed, { since: windowSinceISO(6) }, 'this week', winStatus, ev.target) }, '⬇ This week'),
        h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
          onClick: (ev) => exportContributorWindow(idHashed, {}, 'all time', winStatus, ev.target) }, '⬇ All time')),
      winStatus));

    // ── Per-task list: every task the contributor completed (single-file export) ──
    const tasksCard = h('div', { style: 'margin-top:18px;border-top:1px solid var(--asc-line);padding-top:16px' },
      h('div', { class: 'asc-card-title', style: 'font-size:15px' }, 'Tasks completed'),
      h('div', { class: 'asc-card-sub', style: 'margin-bottom:10px' }, 'Each task shows its completion time (Pacific) and the product version it was labelled with. Export any single task as one file.'),
      h('div', { id: 'ascContribTasks' }, loadingCard('Loading tasks…')));
    pad.appendChild(tasksCard);
    loadContributorTasks(idHashed);
  }

  async function loadContributorTasks(idHashed) {
    const box = document.getElementById('ascContribTasks');
    if (!box) return;
    try {
      const data = await api('/contributors/' + encodeURIComponent(idHashed) + '/submissions');
      const subs = data.submissions || [];
      clear(box);
      if (!subs.length) { box.appendChild(h('div', { class: 'asc-card-sub' }, 'No completed tasks yet.')); return; }
      const rows = subs.map((s) => {
        const st = h('div', {});
        const btn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
          onClick: (ev) => exportContributorWindow(idHashed, { submission_id: s.submission_id }, 'this task', st, ev.target) }, '⬇ Export this task');
        return h('tr', {},
          h('td', {}, fmtDate(s.created_at)),
          h('td', {}, h('span', { class: 'asc-badge asc-badge-gray' }, ascVerLabel(s.portal_version))),
          h('td', { style: 'max-width:340px' }, s.prompt_preview || 'n/a'),
          h('td', {}, btn, st));
      });
      box.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['Completed (PT)', 'Version', 'Task', ''].map((cc) => h('th', {}, cc)))),
        h('tbody', {}, rows))));
    } catch (e) {
      clear(box); box.appendChild(h('div', { class: 'asc-inline-error' }, e.message));
    }
  }

  // Scoped contributor export with an optional {submission_id} or {since,until}
  // window. Reuses the contributor export endpoint (Tier A only, leak-gated).
  async function exportContributorWindow(idHashed, scopeBody, label, statusBox, btn) {
    clear(statusBox);
    if (btn) btn.setAttribute('disabled', '');
    statusBox.appendChild(h('div', { class: 'asc-inline-ok' }, 'Packaging ' + label + '…'));
    try {
      const manifest = await api('/contributors/' + encodeURIComponent(idHashed) + '/export',
        { method: 'POST', body: Object.assign({ profile: 'default' }, scopeBody) });
      clear(statusBox);
      const n = manifest.record_count || 0;
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' }, 'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + ' (' + label + '). Downloading…'));
      await downloadExport(manifest.export_id);
      loadExportsHistory();
    } catch (e) {
      clear(statusBox);
      const msg = e.status === 400 ? ('No export-ready records for ' + label + '.')
        : (e.status === 422 ? 'Export blocked: ' + e.message : (e.message || 'Export failed'));
      statusBox.appendChild(h('div', { class: 'asc-inline-error' }, msg));
    } finally {
      if (btn) btn.removeAttribute('disabled');
    }
  }

  // Start-of-day in Pacific for (today - daysBack), as a UTC ISO string with the
  // trailing 'Z' stripped so it lexicographically compares against the naive-UTC
  // created_at strings the backend stores. daysBack=0 → since midnight today PT.
  function windowSinceISO(daysBack) {
    const now = new Date();
    const [y, m, d] = new Intl.DateTimeFormat('en-CA', { timeZone: ASC_TZ, year: 'numeric', month: '2-digit', day: '2-digit' })
      .format(now).split('-').map(Number);
    const guess = new Date(Date.UTC(y, m - 1, d - (daysBack || 0), 0, 0, 0));
    // Correct the guess by the Pacific offset so the wall-clock is 00:00 PT.
    const offsetMs = new Date(guess.toLocaleString('en-US', { timeZone: 'UTC' }))
      - new Date(guess.toLocaleString('en-US', { timeZone: ASC_TZ }));
    return new Date(guess.getTime() + offsetMs).toISOString().replace('Z', '');
  }

  function renderContributorMetrics(pad, c) {
    clear(pad);
    if (!c) { pad.appendChild(h('div', { class: 'asc-inline-error' }, 'No contributor selected.')); return; }
    pad.appendChild(h('div', { class: 'asc-profile-head' },
      h('div', { class: 'asc-profile-avatar', 'aria-hidden': 'true' }),
      h('div', {},
        h('div', { class: 'asc-profile-name' }, c.display_name || c.id_hashed,
          c.credentials_verified ? h('span', { class: 'asc-badge asc-badge-green', style: 'margin-left:10px' }, 'verified ✓') : null),
        h('div', { class: 'asc-meta-row', style: 'margin-top:6px' },
          h('span', { class: 'asc-badge asc-badge-primary' }, c.role_title || c.role || 'n/a'),
          h('span', { class: 'asc-badge asc-badge-gray' }, c.primary_specialty || c.specialty || 'n/a')))));
    pad.appendChild(h('div', { class: 'asc-stat-grid', style: 'margin-top:14px' },
      stat(c.submission_count || 0, 'Submissions', null, true),
      stat(c.record_count || 0, 'Records labelled'),
      stat(c.grounded_submissions || 0, 'Grounded subs'),
      stat((c.total_hours != null ? c.total_hours : 0) + 'h', 'Total hours'),
      stat(c.premium_submissions || 0, 'Premium subs'),
      stat(c.avg_time_sec != null ? formatTime(Math.round(c.avg_time_sec)) : 'n/a', 'Avg time / task'),
      stat(fmtDate(c.last_labeled_at), 'Last labelled')));
  }

  // ── Tiered-export actions ─────────────────────────────────────────────────
  async function exportOrg(org, statusBox) {
    clear(statusBox);
    statusBox.appendChild(h('div', { class: 'asc-inline-ok' }, 'Packaging ' + org + '…'));
    try {
      const manifest = await api('/organizations/' + encodeURIComponent(org) + '/export', { method: 'POST', body: { profile: 'default' } });
      clear(statusBox);
      const n = manifest.record_count || 0;
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' }, 'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + '. Downloading…'));
      await downloadExport(manifest.export_id);
      loadExportsHistory();
    } catch (e) {
      clear(statusBox);
      const msg = e.status === 400 ? 'No export-ready records for this organization yet.'
        : (e.status === 422 ? 'Export blocked: ' + e.message : (e.message || 'Export failed'));
      statusBox.appendChild(h('div', { class: 'asc-inline-error' }, msg));
    }
  }

  async function exportContributor(idHashed, statusBox, btn) {
    clear(statusBox);
    if (btn) { btn.setAttribute('disabled', ''); }
    statusBox.appendChild(h('div', { class: 'asc-inline-ok' }, 'Packaging this contributor’s data…'));
    try {
      const manifest = await api('/contributors/' + encodeURIComponent(idHashed) + '/export', { method: 'POST', body: { profile: 'default' } });
      clear(statusBox);
      const n = manifest.record_count || 0;
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' }, 'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + '. Downloading…'));
      await downloadExport(manifest.export_id);
      loadExportsHistory();
    } catch (e) {
      clear(statusBox);
      const msg = e.status === 400 ? 'No export-ready records for this contributor yet.'
        : (e.status === 422 ? 'Export blocked (Tier B leak gate): ' + e.message : (e.message || 'Export failed'));
      statusBox.appendChild(h('div', { class: 'asc-inline-error' }, msg));
    } finally {
      if (btn) btn.removeAttribute('disabled');
    }
  }

  // ── Further Credential Summary: §9 ack click-through → generate → download ──
  async function openCredentialSummaryModal(idHashed, displayName) {
    let policy = {};
    try { policy = await api('/credential-policy'); } catch (e) { /* notice falls back below */ }
    const overlay = h('div', { class: 'call-team-overlay is-open', onClick: (e) => { if (e.target === overlay) overlay.remove(); } });
    const recipient = h('input', { class: 'asc-input', placeholder: 'Verification lab / recipient (optional)' });
    const ack = h('input', { type: 'checkbox' });
    const status = h('div', { style: 'margin-top:10px' });
    const genBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Generate verification summary');

    genBtn.onclick = async () => {
      clear(status);
      if (!ack.checked) { status.appendChild(h('div', { class: 'asc-inline-error' }, 'Please acknowledge the notice to continue.')); return; }
      genBtn.setAttribute('disabled', ''); genBtn.textContent = 'Generating…';
      try {
        const res = await api('/contributors/' + encodeURIComponent(idHashed) + '/credential-summary',
          { method: 'POST', body: { recipient: recipient.value.trim() || null, acknowledged: true } });
        clear(status);
        status.appendChild(h('div', { class: 'asc-inline-ok' }, 'Credential summary generated (' + (res.summary_id || '') + ').'));
        const base = '/contributors/' + encodeURIComponent(idHashed) + '/credential-summary/' + encodeURIComponent(res.summary_id) + '/download';
        status.appendChild(h('div', { style: 'display:flex;gap:10px;margin-top:10px' },
          h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', onClick: () => downloadBlob(base + '?format=pdf', 'credential-summary-' + res.summary_id + '.pdf') }, '⬇ Download PDF'),
          h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', onClick: () => downloadBlob(base + '?format=json', 'credential-summary-' + res.summary_id + '.json') }, '⬇ Download JSON')));
      } catch (e) {
        clear(status);
        status.appendChild(h('div', { class: 'asc-inline-error' }, e.message || 'Generation failed'));
      } finally {
        genBtn.removeAttribute('disabled'); genBtn.textContent = 'Generate verification summary';
      }
    };

    const noticeText = policy.non_circumvention_notice
      || 'CONFIDENTIAL: credential verification, provided under NDA / non-circumvention.';
    const popup = h('div', { class: 'call-team-popup', style: 'max-width:720px;max-height:90vh;overflow:auto;text-align:left', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'call-team-title' }, 'Further Credential Summary'),
      h('p', { class: 'asc-help' }, 'Verification dossier for ', h('strong', {}, displayName),
        '. It releases the private (Tier B) credentials under NDA. Watermarked confidential and logged for audit.'),
      h('div', { class: 'asc-notice-box' }, noticeText),
      policy.legal_disclaimer ? h('p', { class: 'asc-label-hint' }, policy.legal_disclaimer) : null,
      h('div', { class: 'asc-field', style: 'margin-top:12px' }, h('label', { class: 'asc-label' }, 'Intended recipient'), recipient),
      h('label', { class: 'asc-checkbox-row', style: 'margin-top:10px' }, ack,
        ' I have read and agree to the Non-Circumvention & Confidentiality Notice above.'),
      status,
      h('div', { style: 'display:flex;gap:10px;margin-top:16px' },
        genBtn,
        h('button', { class: 'asc-btn asc-btn-ghost', style: 'margin-left:auto', onClick: () => overlay.remove() }, 'Close')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  async function downloadBlob(path, filename) {
    try {
      const res = await api(path, { raw: true });
      if (!res.ok) {
        // Surface the server's reason (e.g. a 410 raw-blob-lost/purged message)
        // instead of a bare status code, so an admin knows why and what to do.
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch (_) { /* not JSON */ }
        toast('Download failed (' + res.status + ')' + (detail ? ': ' + detail : ''), 'error');
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
    } catch (e) { if (e.status !== 401) toast('Download failed: ' + (e.message || ''), 'error'); }
  }

  /* Export history, rendered under the builder (PRD §2.4).
   *
   * Takes the card rather than looking one up by id: it used to be the tail of
   * the Export-history sub-tab, and it is now mounted by the export section
   * module. The id lookup stays as the fallback so nothing that called it with
   * no argument breaks.
   *
   * The SCOPE column is new. An export row could say how many records it
   * carried and never what slice it was, so "which batch did the nephrology
   * cases go out in?" had no answer. Rows written before `exports.scope_json`
   * existed render as `legacy`: their scope is genuinely unknown and inventing
   * one would be worse than saying so. Nothing is re-generated. */
  function scopeCell(x) {
    const sc = x.scope;
    if (!sc || !sc.type) {
      return h('span', {
        class: 'asc-dim',
        title: 'Built before exports recorded their scope. The bundle is intact; '
             + 'only the label is unknown.',
      }, 'legacy');
    }
    const parts = [sc.type];
    if (sc.case_ids && sc.case_ids.length) {
      parts.push(sc.case_ids.length === 1 ? sc.case_ids[0]
                                          : sc.case_ids.length + ' cases');
    }
    if (sc.specialty) parts.push(sc.specialty);
    if (sc.portal_version) parts.push(sc.portal_version);
    // A physician-scoped export is labelled by HASH here for the same reason
    // the bundle is: the name never travels with the data (PRD §2.1).
    if (sc.annotator_id_hashed) parts.push(String(sc.annotator_id_hashed).slice(0, 10) + '\u2026');
    return h('span', { class: 'asc-scope-tag' }, parts.join(' \u00b7 '));
  }

  async function loadExportsHistory(cardEl) {
    const card = cardEl || document.getElementById('ascExportHistory');
    if (!card) return;
    clear(card);
    card.appendChild(loadingCard('Loading export history\u2026'));
    try {
      const [data, delivered] = await Promise.all([
        api('/exports'),
        // Delivery status per export (§2.4). Best-effort: history is still
        // history if the delivery list is unavailable.
        api('/admin/buyer-deliveries').catch(() => ({ deliveries: [] })),
      ]);
      const exports = data.exports || [];
      const sentTo = {};
      ((delivered && delivered.deliveries) || []).forEach((d) => {
        if (d.export_id) sentTo[d.export_id] = d.buyer_email || 'a buyer';
      });
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'Export history (' + exports.length + ')')));
      if (!exports.length) { card.appendChild(h('div', { class: 'asc-empty' }, h('p', {}, 'No exports yet.'))); return; }
      const rows = exports.map((x) => h('tr', {},
        h('td', { class: 'asc-cell-id' }, copyableId(x.export_id)),
        h('td', {}, scopeCell(x)),
        h('td', {}, String(x.record_count != null ? x.record_count : (x.count != null ? x.count : 'n/a'))),
        h('td', {}, fmtDate(x.created_at)),
        h('td', {}, sentTo[x.export_id]
          ? h('span', { class: 'asc-dim' }, 'sent to ' + sentTo[x.export_id])
          : h('span', { class: 'asc-dim' }, '\u2014')),
        h('td', {}, h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', onClick: () => downloadExport(x.export_id) }, '\u2b07 Download'))));
      card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['ID', 'Scope', 'Records', 'Created', 'Delivery', ''].map((c) => h('th', {}, c)))),
        h('tbody', {}, rows))));
    } catch (e) {
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message)));
    }
  }

  async function downloadExport(exportId) {
    try {
      const res = await api('/exports/' + exportId + '/download', { raw: true });
      if (!res.ok) { toast('Download failed (' + res.status + ')', 'error'); return; }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = exportId + '.zip';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
    } catch (e) { if (e.status !== 401) toast('Download failed: ' + (e.message || ''), 'error'); }
  }

  // ─── Admin: Metrics ────────────────────────────────────────────────────────
  // ─── Metrics: the four questions (PRD-C Phase 6) ───────────────────────────
  function metricSparkBars(series) {
    const vals = (series || []).slice(-14);
    const max = Math.max.apply(null, vals.concat([1]));
    return h('div', { class: 'asc-metric-q-spark', 'aria-hidden': 'true' },
      vals.map((v) => h('span', {
        class: 'asc-metric-spark-bar' + (v ? '' : ' is-zero'),
        style: 'height:' + (2 + Math.round((v / max) * 26)) + 'px',
      })));
  }

  function metricQuestionCard(name, headline, subLabel, spark, rows) {
    return h('div', { class: 'asc-metric-q' },
      h('div', { class: 'asc-metric-q-name' }, name),
      h('div', { class: 'asc-metric-q-headline' }, String(headline)),
      h('div', { class: 'asc-metric-q-sub' }, subLabel),
      metricSparkBars(spark),
      h('div', { class: 'asc-metric-q-rows' }, rows.map(([label, value]) =>
        h('div', { class: 'asc-metric-row' },
          h('span', { class: 'asc-metric-row-label' }, label),
          h('span', { class: 'asc-metric-row-value' }, String(value))))));
  }

  async function renderMetricQuestions(mount, s) {
    mount.appendChild(h('div', { class: 'asc-dim' }, 'Loading the four questions…'));
    let q;
    try { q = await api('/admin/metrics/questions'); }
    catch (e) {
      clear(mount);
      mount.appendChild(h('div', { class: 'asc-inline-error' }, e.message));
      return;
    }
    clear(mount);
    const supply = q.supply || {}, quality = q.quality || {},
          pipeline = q.pipeline || {}, demand = q.demand || {};
    const kappa = (s && s.kappa) || {};
    const grounded = (s && s.grounded) || {};
    // Operator diagnostics that the four-question restructure dropped on the
    // floor (C-5.1). They belong INSIDE the questions they answer, not in a
    // separate wall of tiles: but they do have to be on the page.
    const qpr = (s && s.qa_pass_rate) || {};
    const flaw = (s && s.flaw_catch_rate) || {};
    const omc = (s && s.open_modality_counts) || {};
    const sc = (s && s.status_counts) || {};
    // Tri-state acceptance: null means "no reviews yet", which must never be
    // shown as a 0% acceptance rate.
    const acc = quality.expert_acceptance;
    const accHeadline = acc == null ? '–' : Math.round(acc * 100) + '%';
    const accSub = acc == null
      ? 'expert acceptance: no reviews yet'
      : 'expert acceptance (' + (quality.reviews_scored || 0) + ' reviews)';
    mount.appendChild(h('div', { class: 'asc-metric-questions' },
      metricQuestionCard('Supply', supply.physicians_active_week || 0,
        'physicians active this week', supply.spark, [
          ['Cases labeled', supply.cases_labeled || 0],
          ['Cases reviewed', supply.cases_reviewed || 0]]),
      // Expert acceptance and Cohen's κ are DIFFERENT statistics, presented
      // separately and labeled: merging them would misreport the number a
      // buyer audits most closely. "Not rejected" is the combined figure
      // (accept + accept_with_edits) and carries its own name for the same
      // reason: a different number needs a different word.
      metricQuestionCard('Quality', accHeadline, accSub, quality.spark, [
        ["Cohen's κ (independent slice)",
         fmtNum(kappa.overall) + ' · n=' + (kappa.n != null ? kappa.n : 0)],
        ['Not rejected', quality.not_rejected == null
          ? '–' : Math.round(quality.not_rejected * 100) + '%'],
        ['Citation rate', (grounded.grounded_pct != null ? grounded.grounded_pct : 0) + '%'],
        // Restored (C-5.1): the restructure was right, deleting these was not.
        ['QA pass rate', (qpr.pass_rate != null ? Math.round(qpr.pass_rate * 100) : 0) + '%'
          + ' (' + (qpr.passed || 0) + '/' + (qpr.reviewed || 0) + ')'],
        ['Flaw catch rate', flaw.rate != null
          ? Math.round(flaw.rate * 100) + '% (' + (flaw.caught || 0) + '/' + (flaw.scored || 0) + ')'
          : '–'],
        ['Avg agreement', fmtNum(s.average_agreement)]]),
      metricQuestionCard('Pipeline', pipeline.uploads_received || 0,
        'uploads received', pipeline.spark, [
          ['Awaiting review', pipeline.awaiting_review || 0],
          ['Promoted to task', pipeline.promoted_to_task || 0],
          // Multimodal Debug PRD §P3.11: "0" here is the tell that generation
          // stalled, before anyone wonders why no case panel appears.
          ['Multimodal in queue', (omc.multimodal != null ? omc.multimodal : 0)
            + ' (' + (omc.text != null ? omc.text : 0) + ' text)'],
          ['Submissions', sumValues(sc)]]),
      metricQuestionCard('Demand', demand.buyer_requests || 0,
        'buyer requests', demand.spark, [
          ['Exports', demand.exports || 0],
          ['Records shipped', demand.records_shipped || 0]])));
  }

  async function renderAdminMetrics(body) {
    clear(body);
    state.browse.metrics = { level: 'orgs', org: null, idHashed: null, contributor: null };
    body.appendChild(loadingCard('Loading metrics…'));
    let s;
    try { s = await api('/stats'); }
    catch (e) { clear(body); body.appendChild(h('div', { class: 'asc-card asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message))); return; }
    clear(body);

    const sc = s.status_counts || {};
    const qpr = s.qa_pass_rate || {};
    const kappa = s.kappa || {};
    const grounded = s.grounded || {};
    const flaw = s.flaw_catch_rate || {};

    // PRD-C Phase 6: the wall of undifferentiated numbers becomes FOUR
    // QUESTIONS: Supply, Quality, Pipeline, Demand: one headline figure and a
    // sparkline each. Cohen's κ (from /stats, the independent slice) and expert
    // acceptance (from PRD-A's reviews) render SEPARATELY and labeled: expert
    // acceptance is not κ, and merging them would misreport the number a buyer
    // audits most closely. The deeper diagnostics keep their cards below.
    const questionsMount = h('div', {});
    body.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:14px' }, 'Is any of this working?'),
      questionsMount));
    renderMetricQuestions(questionsMount, s);

    // Model-Failure view (FEAT-1): "cases where model X failed, with the expert
    // correction", the artifact you put in front of a lab.
    const mfCard = h('div', { class: 'asc-card', id: 'ascModelFailures' }, loadingCard('Loading model failures…'));
    body.appendChild(mfCard);
    loadModelFailures();

    // Data by product version: how many submissions came from the V1 (classic),
    // V2 (assisted), and V3 (seamless) evaluator flows.
    const pvc = s.portal_version_counts || {};
    const v1n = pvc.v1 || 0, v2n = pvc.v2 || 0, v3n = pvc.v3 || 0, v4n = pvc.v4 || 0, pvTotal = v1n + v2n + v3n + v4n;
    const pct = (n) => pvTotal ? Math.round((100 * n) / pvTotal) + '%' : '0%';
    // Position-bias QC (Seamless PRD WS6): the A/B slot is randomized 50/50 so a
    // reward model can't learn "A is better"; a rate drifting from ~50% is an alarm.
    const abb = s.ab_balance || {};
    const abRate = abb.a_stronger_rate;
    const abOk = abRate == null || (abRate >= 0.4 && abRate <= 0.6);
    body.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:14px' }, 'Data by product version'),
      h('div', { class: 'asc-stat-grid' },
        stat(v4n, 'V4 · Real Cases', pct(v4n) + ' of labeled data'),
        stat(v3n, 'V3 · Seamless', pct(v3n) + ' of labeled data'),
        stat(v2n, 'V2 · Assisted', pct(v2n) + ' of labeled data'),
        stat(v1n, 'V1 · Classic', pct(v1n) + ' of labeled data'),
        stat(abRate == null ? 'n/a' : Math.round(abRate * 100) + '%',
          (abOk ? '' : 'alert · ') + 'A-is-stronger rate',
          'target ~50% · n=' + (abb.n || 0) + ' (position-bias QC)'),
        (function () {
          // Two-frontier slot balance (A3): OpenAI-in-slot-A rate over built pairs.
          const sb = s.ab_slot_balance || {};
          const r = sb.openai_as_A_rate;
          const ok = r == null || (r >= 0.4 && r <= 0.6);
          return stat(r == null ? 'n/a' : Math.round(r * 100) + '%',
            (ok ? '' : 'alert · ') + 'OpenAI-as-A rate',
            'target ~50% · n=' + (sb.pairs || 0) + ' (two-frontier QC)');
        })(),
        (function () {
          // Two-frontier fallback health (PRD §A3 Rung 3): a RED chip when the rolling
          // legacy-fallback rate exceeds the ceiling; a provider is likely down and new
          // pairs are being held (needs_baseline) instead of shipping mostly-legacy data.
          const fb = s.ab_fallback || {};
          const r = fb.rate;
          const alert = !!fb.alert;
          return stat(r == null ? 'n/a' : Math.round(r * 100) + '%',
            (alert ? 'alert · ' : '') + 'Legacy-fallback rate',
            alert
              ? 'ABOVE ceiling ' + Math.round((fb.ceiling || 0) * 100) + '%. A provider looks down; new pairs held. Fix OPENAI_API_KEY / the provider.'
              : 'ceiling ' + Math.round((fb.ceiling || 0) * 100) + '% · two-frontier fallback health');
        })()),
      h('p', { class: 'asc-help', style: 'margin-top:10px' },
        'All three flows capture the same judgment and produce the same record types; every record is stamped with its source version. '
        + 'The A/B slot is randomized 50/50 so preference data carries no position bias.')));

    // Value per clinician-minute (Value-per-Minute PRD Part A): the north-star
    // metric (sellable dollars produced per minute of clinician time) reported
    // REALIZED (bankable) with the PROJECTED reuse forecast alongside, and always
    // next to κ + the assist override rate so a rising ratio with falling quality
    // reads as the regression it is.
    const vpt = s.value_per_time || {};
    const vptOverall = vpt.overall || {};
    const vTarget = (s.value_per_time_target != null) ? s.value_per_time_target : (vpt.target != null ? vpt.target : 10);
    const byVer = vpt.by_portal_version || {};
    const ovr = s.override_rate || {};
    const ratio = (v) => (v == null ? 'n/a' : (Math.round(v * 10) / 10) + ' : 1');
    const realizedOverall = vptOverall.realized_vpm;
    const meets = (realizedOverall != null && realizedOverall >= vTarget);
    const pctOr = (o) => {
      const r = o && o.override_rate;
      return r == null ? 'n/a' : Math.round(r * 100) + '%';
    };
    body.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:6px' }, 'Value per clinician-minute'),
      h('p', { class: 'asc-help', style: 'margin-top:0;margin-bottom:14px' },
        'North-star: sellable $ produced per minute of clinician time. Held to realized ≥ '
          + ratio(vTarget) + '. Projected includes the ×reuse forecast (not banked).'),
      h('div', { class: 'asc-stat-grid' },
        stat(ratio(realizedOverall), (meets ? '✓ ' : '') + 'Realized V/T',
          'target ' + ratio(vTarget) + ' · n=' + (vptOverall.n || 0)),
        stat(ratio(vptOverall.projected_vpm), 'Projected V/T', '× reuse forecast'),
        stat(ratio((byVer.v3 || {}).realized_vpm), 'V3 realized V/T', 'n=' + ((byVer.v3 || {}).n || 0)),
        stat(ratio((byVer.v2 || {}).realized_vpm), 'V2 realized V/T', 'n=' + ((byVer.v2 || {}).n || 0)),
        stat(ratio((byVer.v1 || {}).realized_vpm), 'V1 realized V/T', 'n=' + ((byVer.v1 || {}).n || 0)),
        stat(fmtNum(kappa.overall), "Cohen's κ", 'quality anchor · n=' + (kappa.n != null ? kappa.n : 0)),
        stat(pctOr(ovr.verdict), 'Verdict override', 'assist accepted vs changed'),
        stat(pctOr(ovr.steps), 'Step override', 'rubber-stamp guard')),
      h('p', { class: 'asc-help', style: 'margin-top:12px' },
        'A near-zero override rate flags rubber-stamping: V/T only counts when κ holds and the clinician still stands behind every judgment.')));

    // Status counts
    const statusRows = Object.keys(sc).map((k) => h('tr', {}, h('td', {}, k.replace(/_/g, ' ')), h('td', {}, String(sc[k]))));
    if (statusRows.length) {
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'Queue by status')),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {}, h('th', {}, 'Status'), h('th', {}, 'Count'))),
          h('tbody', {}, statusRows)))));
    }

    // Kappa by specialty
    const bySpec = kappa.by_specialty || {};
    const specRows = Object.keys(bySpec).map((k) => {
      const v = bySpec[k];
      const val = (v && typeof v === 'object') ? v.kappa : v;
      return h('tr', {}, h('td', {}, k), h('td', {}, fmtNum(val)));
    });
    if (specRows.length) {
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, "Cohen's κ by specialty")),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {}, h('th', {}, 'Specialty'), h('th', {}, 'κ'))),
          h('tbody', {}, specRows)))));
    }

    // Evaluator throughput
    const thr = s.evaluator_throughput || [];
    if (thr.length) {
      const rows = thr.map((t) => h('tr', {},
        h('td', {}, t.email || t.evaluator_id || 'n/a'),
        h('td', {}, String(t.count != null ? t.count : (t.submissions != null ? t.submissions : 0))),
        h('td', {}, t.avg_time_sec != null ? formatTime(Math.round(t.avg_time_sec)) : (t.average_time_sec != null ? formatTime(Math.round(t.average_time_sec)) : 'n/a'))));
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'Evaluator throughput')),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {}, ['Evaluator', 'Submissions', 'Avg time / task'].map((c) => h('th', {}, c)))),
          h('tbody', {}, rows)))));
    }

    // Contributor stats. (contributor_stats() returns submissions / grounded /
    // premium / total_hours, not count/approved.)
    const contrib = s.contributor_stats || [];
    if (contrib.length) {
      const rows = contrib.map((t) => h('tr', {},
        h('td', {}, t.email || t.evaluator_id || 'n/a'),
        h('td', {}, t.specialty || 'n/a'),
        h('td', {}, String(t.submissions != null ? t.submissions : 0)),
        h('td', {}, String(t.grounded_submissions != null ? t.grounded_submissions : 0)),
        h('td', {}, t.total_hours != null ? t.total_hours + 'h' : 'n/a')));
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'Contributors')),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {}, ['Contributor', 'Specialty', 'Submissions', 'Grounded', 'Hours'].map((c) => h('th', {}, c)))),
          h('tbody', {}, rows)))));
    }

    // Per-organization → per-contributor metrics (same drill-down UI as Exports).
    const browseCard = h('div', { class: 'asc-card', id: 'ascMetricsBrowser' });
    body.appendChild(browseCard);
    renderOrgContribBrowser(browseCard, 'metrics');
  }

  function stat(value, label, sub, hero) {
    return h('div', { class: 'asc-stat' + (hero ? ' asc-stat-hero' : '') },
      h('div', { class: 'asc-stat-value' }, String(value)),
      h('div', { class: 'asc-stat-label' }, label),
      sub ? h('div', { class: 'asc-stat-sub' }, sub) : null);
  }

  // ─── Small utilities ───────────────────────────────────────────────────────
  function selectFrom(options, selected) {
    const sel = h('select', { class: 'asc-select' },
      ...options.map((o) => h('option', { value: o }, o === '' ? 'any' : o.replace(/_/g, ' '))));
    sel.value = selected != null ? selected : (options[0] || '');
    return sel;
  }
  function profileNames() {
    const profiles = (state.taxonomy && state.taxonomy.export_profiles) || [];
    const names = profiles.map((p) => (typeof p === 'string' ? p : (p.name || p.id || p.profile))).filter(Boolean);
    return names.length ? names : ['default'];
  }
  function sumValues(obj) { return Object.keys(obj || {}).reduce((a, k) => a + (Number(obj[k]) || 0), 0); }
  function fmtNum(n) { return (n == null || isNaN(n)) ? 'n/a' : (Math.round(n * 1000) / 1000).toString(); }
  // Product-version label shared by the exports history + per-task version badges.
  function ascVerLabel(v) {
    return { v4: 'V4 · Real', v3: 'V3', v2: 'V2', v1: 'V1' }[v] || (v || 'n/a');
  }

  // ─── Boot ──────────────────────────────────────────────────────────────────
  boot();
})();
