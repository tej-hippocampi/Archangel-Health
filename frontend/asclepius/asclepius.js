/* ═══════════════════════════════════════════════════════════════════════════
   Asclepius: Expert Evaluation Portal (vanilla SPA)
   Standalone Asclepius JWT auth. No frameworks, no build step.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const API_BASE = '/api/asclepius';
  // Companion header on the credential-verification 403 (asclepius/auth.py
  // AUTH_GATE_HEADER): 'pending' or 'rejected'.
  const AUTH_GATE_HEADER = 'X-Asclepius-Auth-Gate';
  const TOKEN_KEY = 'asclepius_token';
  const DRAFT_PREFIX = 'asclepius_draft_';
  // Contributor's chosen evaluator experience: 'v1' classic | 'v2' assisted |
  // 'v3' seamless (the recommended default). Persisted per browser; the default
  // for every new task.
  const PORTAL_VERSION_KEY = 'asclepius_portal_version';
  const DEFAULT_PORTAL_VERSION = 'v3';
  // The V3/V4 specialty the physician is grading (Specialty Hyper-Personalization
  // PRD §1). Persisted per browser like the portal version; sent on task fetch.
  const PORTAL_SPECIALTY_KEY = 'asclepius_portal_specialty';
  const DEFAULT_PORTAL_SPECIALTY = 'nephrology';
  // Doctor-portal session token (same origin). If present, we silently exchange
  // it for an Asclepius session so affiliated clinicians skip the login form.
  const DOCTOR_TOKEN_KEY = 'archangel_doctor_auth_token';
  // Set when the user explicitly signs out. Suppresses the silent doctor-portal
  // SSO on the NEXT boot so a clinician who onboarded under one email (their
  // standing workspace credentials) can actually reach the sign-in form to use
  // that identity, instead of being re-exchanged straight back into the
  // doctor-portal account. Cleared the moment they sign in (either path).
  const SUPPRESS_SSO_KEY = 'asclepius_suppress_sso';

  // ─── App state ─────────────────────────────────────────────────────────────
  const state = {
    token: localStorage.getItem(TOKEN_KEY) || null,
    user: null,
    taxonomy: null,
    view: 'eval',          // 'eval' | 'admin'  (header-level: evaluate vs admin console)
    panel: 'tasks',        // side-panel destination: 'tasks' | 'guide' (Community is external nav)
    // PRD M: which of the three manuals the Guide is showing. NULL means "not
    // chosen yet", which resolves to the most senior manual the session holds:
    // distinct from a physician having actively picked one.
    manualRole: null,
    // Community integration state (Community PRD boundary: every field optional,
    // degrades silently if the community backend is unbuilt). unread drives the
    // badge; handoffToken is pre-minted so the new tab can open synchronously.
    community: { unread: 0, handoffToken: null, unavailable: false, unreadUnavailable: false, pollTimer: null },
    // PRD-C admin restructure: sections mirror the assets the company owns
    // (Physicians · Health Systems · Export · Metrics); the old stage-named
    // views live on as sub-tabs inside them.
    adminTab: 'physicians',   // physicians | work | money | data
    pipelineFocus: null,      // upload_id deep-linked from a Data bucket row
    adminSub: {               // active sub-tab per section
      physicians: 'roster',   //   roster | signups | verify | qa
      work: 'tasks',          //   tasks | metrics
      money: 'earnings',      //   earnings | referrals
      data: 'systems',        //   systems | pipeline | export
      export: 'bycase',       //   bycase | buyers | history (inside Data > Export)
    },
    // Org → contributor drill-down state, shared shape across Exports + Metrics.
    browse: {
      export: { level: 'orgs', org: null, idHashed: null, contributor: null },
      metrics: { level: 'orgs', org: null, idHashed: null, contributor: null },
    },
    task: null,            // current blinded task
    draft: null,           // in-progress submission draft
    timerStart: 0,
    baseElapsed: 0,
    timerInterval: null,
    submitting: false,
    assistLoadingFor: null, // task_id of the /assist/prelabel fetch in flight
    assistFailedFor: null,  // task_id whose assist fetch failed (retry next load)
    showFullText: false,    // compare view: full text vs highlighted diff
    portalChosen: false,    // has the evaluator picked V1/V2 on the home page yet
    specialtyChosen: false, // has the evaluator picked a specialty this session (V3/V4)
    specialties: null,      // cached GET /specialties listing (drives the picker)
    // First-run tutorial (Calibration Case 1). Null when not running; set by
    // startTutorial to {active, replay, idx}. Server state (user.tutorial) is
    // the launch authority; this is only the live tour position.
    tutorial: null,
    instrOpen: false,       // instruction drawer visibility
  };
  function tutorialActive() { return !!(state.tutorial && state.tutorial.active); }

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
  const root = () => document.getElementById('ascRoot');
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  // The tag popover is portaled to <body>, so clearing #ascRoot no longer takes
  // it with it. Every re-render goes through here; close it on the way past or
  // it outlives the chip it belongs to.
  function setRoot(node) { closeTagPopover(); const r = root(); clear(r); r.appendChild(node); }

  // ─── Fetch helper (injects Bearer, parses JSON, handles 401) ────────────────
  async function api(path, opts) {
    opts = opts || {};
    const headers = opts.headers || {};
    if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
    let body = opts.body;
    if (body !== undefined && !opts.isForm) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(API_BASE + path, { method: opts.method || 'GET', headers, body });
    } catch (e) {
      throw { status: 0, detail: 'Network error. Is the backend running?', message: 'Network error' };
    }
    // A 401 mid-session means the token expired -> bounce to login. But for
    // foreground auth calls (login, /auth/me probe) a 401 is just "bad creds /
    // stale token"; opts.noAuthHandler lets the caller handle it and show the
    // real server message instead of a misleading "session expired".
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
        // 'pending' | 'rejected' on a credential-verification 403 (see
        // asclepius/auth.py AUTH_GATE_HEADER); null for every other failure.
        // Lets the caller pick the right screen without matching on prose.
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
    stopTimer();
    stopCommunityPolling();
    resetCommunityState(); // bump generation now so any in-flight fetch is voided
    teardownSidePanel();
    renderHeader();
    renderLogin('Your session expired. Please sign in again.');
  }

  // ─── Toasts ────────────────────────────────────────────────────────────────
  function toast(msg, kind) {
    const region = document.getElementById('ascToasts');
    const t = h('div', { class: 'asc-toast ' + (kind || 'info') }, msg);
    region.appendChild(t);
    setTimeout(() => {
      t.style.transition = 'opacity .3s';
      t.style.opacity = '0';
      setTimeout(() => t.remove(), 320);
    }, kind === 'error' ? 5200 : 3200);
  }

  // ─── Header ────────────────────────────────────────────────────────────────
  function renderHeader() {
    const header = document.getElementById('ascHeader');
    if (!state.user) { header.setAttribute('hidden', ''); return; }
    header.removeAttribute('hidden');

    const nav = document.getElementById('ascNav');
    clear(nav);
    // Everyone gets a Dashboard tab so a doctor can return home from a case.
    nav.appendChild(h('button', {
      class: 'asc-nav-btn' + (state.view === 'home' ? ' active' : ''),
      onClick: () => switchView('home'),
    }, 'Dashboard'));
    const isAdmin = state.user.role === 'admin' || state.user.role === 'qa_reviewer';
    if (isAdmin) {
      nav.appendChild(h('button', {
        class: 'asc-nav-btn' + (state.view === 'eval' ? ' active' : ''),
        onClick: () => switchView('eval'),
      }, 'Evaluate'));
      nav.appendChild(h('button', {
        class: 'asc-nav-btn' + (state.view === 'admin' ? ' active' : ''),
        onClick: () => switchView('admin'),
      }, 'Admin console'));
    }
    // Community entry lives in the persistent SIDE PANEL (per the Community
    // PRD §1 and the Side Panel PRD), not the header: see renderSidePanel().

    const badge = document.getElementById('ascUserBadge');
    clear(badge);
    badge.appendChild(h('span', { class: 'asc-user-email' }, state.user.email));
    badge.appendChild(h('span', { class: 'asc-user-role' },
      state.user.role.replace('_', ' ') + (state.user.specialty ? ' · ' + state.user.specialty : '')));

    // The corner ? tab (below) is the single help entry point: replay the
    // practice case or view a summary of it. No separate header control.
    ensureInstrTab();

    document.getElementById('ascLogoutBtn').onclick = logout;
  }

  function switchView(view) {
    if (view === 'admin' && state.view !== 'admin') saveDraft();
    state.view = view;
    // The header nav (Evaluate / Admin console) always lands back inside the
    // Tasks side-panel destination: the Guide is a peer, not a sub-view of it.
    // Leaving the Guide this way must also stop its scroll-spy observer.
    if (state.panel === 'guide' && guideObserver) { guideObserver.disconnect(); guideObserver = null; }
    state.panel = 'tasks';
    renderHeader();
    renderSidePanel();
    if (view === 'home') renderDashboardView();
    else if (view === 'eval') renderEvalView();
    else renderAdminView();
  }

  // ─── Side panel destination router (Tasks / Guide; Community is external) ────
  // Tasks re-enters the existing header view (eval or admin); Guide renders the
  // in-portal Instruction Manual. Community never routes here: it opens a tab.
  function setPanel(dest) {
    if (dest === 'community') { openCommunity(); return; }
    // PRD-R: the expert review console is its own page (review.html). It is
    // external for the same reason Community is: it is not a panel inside this
    // shell. Gated on the SERVER's capability list, never on a tier string.
    if (dest === 'review') {
      if (!sessionCan('review')) return;
      window.open('/asclepius/review', '_blank', 'noopener');
      return;
    }
    if (dest !== 'tasks' && dest !== 'guide' && dest !== 'earnings'
        && dest !== 'referral' && dest !== 'verification') return;
    // Server-gated destinations are re-checked here, not only hidden in the
    // rail: a stale deep link or a hand-typed state change must not open a
    // section the session was never granted. (The API 403s regardless.)
    if (dest === 'referral' && !sessionCan('refer')) return;
    if (dest === 'verification') { state.panel = dest; renderVerificationPanel(); return; }
    if (dest === state.panel) return; // already here: no needless re-render/refetch
    saveDraft(); // preserve any in-progress eval draft before setRoot() wipes it
    // Leaving the Guide: stop the scroll-spy observer so it never watches the
    // detached section nodes that setRoot() is about to replace.
    if (dest !== 'guide' && guideObserver) { guideObserver.disconnect(); guideObserver = null; }
    state.panel = dest;
    renderSidePanel();
    if (dest === 'referral') {
      renderReferralView();
    } else if (dest === 'earnings') {
      renderEarningsView();
    } else if (dest === 'guide') {
      renderGuide();
    } else if (state.view === 'admin') {
      renderAdminView();
    } else {
      renderEvalView();
    }
    updateHeaderProgress();
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     SIDE PANEL: a persistent left rail (Tasks / Community / Guide) that lives
     OUTSIDE #ascRoot, so the per-view setRoot() re-renders never wipe it. Built
     once, then updated in place. Collapses to icons < 1100px; becomes a bottom
     tab bar on mobile (all handled in CSS).
     ═══════════════════════════════════════════════════════════════════════════ */

  // Inline, token-palette icons (stroke = currentColor, no fills). 20×20 grid.
  const RAIL_ICONS = {
    tasks: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M7 4h9M7 10h9M7 16h9" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M3.2 4.2l1 1 1.4-1.7M3.2 10.2l1 1 1.4-1.7M3.2 16.2l1 1 1.4-1.7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    community: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M4 15V6a1.5 1.5 0 011.5-1.5h9A1.5 1.5 0 0116 6v5.5A1.5 1.5 0 0114.5 13H7l-3 2.5z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M7.5 8.5h5M7.5 10.5h3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    // A colleague joining a colleague: one figure, one plus.
    referral: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="8" cy="7" r="2.6" stroke="currentColor" stroke-width="1.5"/><path d="M3.4 16c.6-2.6 2.4-4 4.6-4s4 1.4 4.6 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M15 6v5M12.5 8.5h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    // PRD-R: two panels side by side, the shape of the paired review.
    review: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2.8" y="4" width="6" height="12" rx="1.3" stroke="currentColor" stroke-width="1.5"/><rect x="11.2" y="4" width="6" height="12" rx="1.3" stroke="currentColor" stroke-width="1.5"/><path d="M4.6 8.2h2.4M4.6 11h2.4M13 8.2h2.4M13 11h2.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
    guide: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M4 4.5A1.5 1.5 0 015.5 3H10v14H5.5A1.5 1.5 0 014 15.5v-11z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M10 3h4.5A1.5 1.5 0 0116 4.5v11a1.5 1.5 0 01-1.5 1.5H10" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M6.5 7h1.5M6.5 9.5h1.5M12 7h1.5M12 9.5h1.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
    earnings: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M10 3.2v13.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M12.9 6.3a2.6 2.6 0 00-2.4-1.3h-.8a2.35 2.35 0 000 4.7h.6a2.35 2.35 0 010 4.7h-.8a2.6 2.6 0 01-2.4-1.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  // Chrome affordances that are not nav destinations. Same 20×20 stroke grid,
  // same currentColor rule, kept apart from RAIL_ICONS because these belong to
  // the layout controls rather than to a section.
  const CHROME_ICONS = {
    // Double chevron pointing left = collapse. It ROTATES rather than swaps: a
    // glyph that changes identity reads as a different button, a glyph that
    // turns reads as the same button in a different state.
    chevrons: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M9.6 5.6L5.2 10l4.4 4.4M15.2 5.6L10.8 10l4.4 4.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    // Arrows pulling back into a box = leave the expanded state.
    collapse: '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M11.4 8.6h4M11.4 8.6v-4M11.4 8.6L16 4M8.6 11.4h-4M8.6 11.4v4M8.6 11.4L4 16" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  // A layer that owns the screen owns the keyboard with it. Without this, `f`
  // pressed inside the specialty picker toggles focus mode behind the modal,
  // and one Escape fires two handlers — mine and the layer's own — so the
  // physician loses focus mode AND the dialog they were actually dismissing.
  function modalLayerOpen() {
    return !!(state._tagPop
      || document.body.classList.contains('asc-sheet-open')
      || document.querySelector('.call-team-overlay.is-open')
      || tutorialActive());
  }

  // A keyboard shortcut must never fire inside a field. Steps 2 and 4 are full
  // of textareas: a physician typing "the patient is afebrile" is not asking for
  // focus mode on the f.
  function isTypingTarget(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
    return !!(el.closest && el.closest('[contenteditable]'));
  }

  const RAIL_ITEMS = [
    // `surface` is the access-level axis (see backend asclepius/capabilities.py).
    // It behaves differently from `capability` on purpose: a capability the
    // session lacks HIDES the item (a labeler should never see a Review tab they
    // will not get), whereas a surface it lacks SHOWS IT LOCKED. A physician
    // waiting on credentials is going to get these in a day or two, and hiding
    // them makes the product look empty at exactly the moment we are trying to
    // show them what they joined.
    { dest: 'tasks',     label: 'Tasks', surface: 'real_work',
      lockedHint: 'Opens when your credentials clear' },
    { dest: 'community', label: 'Community', surface: 'community_read', external: true },
    // Referral (PRD-REF). Gated on the SURFACE, which every live account holds
    // including one still under review. It used to gate on the 'refer'
    // capability, which comes from a tier, which is only assigned at approval —
    // so a physician who had just signed up had the tab filtered out of their
    // rail entirely, and the most enthusiastic moment they will ever have about
    // this place passed with nothing to act on.
    { dest: 'referral',  label: 'Referral', surface: 'referral' },
    // Earnings (PRD-P §5). Visible to any signed-in physician — what you have
    // made is not a privileged surface, and every endpoint behind it scopes from
    // the session, so there is nothing here to gate on a capability. It reads
    // zero before verification, which is true rather than hidden.
    { dest: 'earnings',  label: 'Earnings', surface: 'earnings' },
    { dest: 'guide',     label: 'Guide' },
  ];

  // The capability list the server put on the session. Absent (an older token,
  // a cached payload) means no extra sections: deny, never assume.
  function sessionCan(capability) {
    const caps = (state.user && state.user.capabilities) || [];
    return caps.indexOf(capability) !== -1;
  }

  // The surface list the server put on the session. Absent means deny, exactly
  // like sessionCan: an older token or a cached payload must not be read as
  // permission.
  function sessionHasSurface(surface) {
    const list = (state.user && state.user.surfaces) || [];
    return list.indexOf(surface) !== -1;
  }

  // True while credentials are under review: in the product, but not yet on
  // real cases. Read from the server's access_level, never re-derived from
  // verification_status here.
  function sessionIsProvisional() {
    return !!(state.user && state.user.access_level === 'provisional');
  }

  function visibleRailItems() {
    return RAIL_ITEMS
      .filter((it) => !it.capability || sessionCan(it.capability))
      .map((it) => Object.assign({}, it, {
        locked: !!it.surface && !sessionHasSurface(it.surface),
      }));
  }

  let chromeMetricsBound = false;
  let communityGen = 0; // session generation: see resetCommunityState()

  // Deterministic specialty → accent-dot color (palette only). Purely cosmetic
  // wayfinding; any specialty maps stably to one of the four console accents.
  function specialtyDotColor(spec) {
    const s = String(spec || '').toLowerCase();
    const accents = ['dot-green', 'dot-orange', 'dot-pink', 'dot-lime'];
    let hash = 0;
    for (let i = 0; i < s.length; i++) hash = (hash * 31 + s.charCodeAt(i)) & 0xffff;
    return accents[hash % accents.length];
  }

  // Best-effort human display name. The session user has no guaranteed `name`
  // field (email is the identity), so fall back to a title-cased email local part.
  function railDisplayName() {
    const u = state.user || {};
    const explicit = u.name || u.full_name || u.display_name;
    if (explicit) return String(explicit);
    const email = String(u.email || '');
    const local = email.split('@')[0] || 'Clinician';
    const pretty = local.replace(/[._-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).trim();
    return pretty ? ('Dr. ' + pretty) : 'Clinician';
  }

  function railItemActive(dest) {
    if (dest === 'guide') return state.panel === 'guide';
    if (dest === 'referral') return state.panel === 'referral';
    if (dest === 'earnings') return state.panel === 'earnings';
    return state.panel === 'tasks' && dest === 'tasks';
  }

  // ─── Compact: the rail collapsed to icons ───────────────────────────────────
  // Compact is the default answer to "let the case fill the screen": it reclaims
  // 152px and still keeps the active-section indicator, the Community unread
  // signal, and a visible way back. It PERSISTS across sessions, unlike Focus —
  // a physician who collapses the rail meant it and will recognise the icon rail
  // next week. That asymmetry is the whole safety design.
  const RAIL_KEY = 'asc_rail_compact';
  function railCompact() {
    try { return localStorage.getItem(RAIL_KEY) === '1'; } catch (_) { return false; }
  }
  function railCollapseTitle() {
    return railCompact() ? 'Expand navigation  ( [ )' : 'Collapse navigation  ( [ )';
  }
  function applyRailState() {
    document.body.classList.toggle('asc-rail-compact', railCompact());
  }
  function toggleRailCompact() {
    try { localStorage.setItem(RAIL_KEY, railCompact() ? '0' : '1'); } catch (_) { /* ignore quota */ }
    applyRailState();
    // Re-title in place; a full re-render would lose the rail's scroll position.
    const b = document.querySelector('.asc-rail-collapse');
    if (b) {
      b.setAttribute('aria-expanded', String(!railCompact()));
      b.title = railCollapseTitle();
      b.setAttribute('aria-label', railCollapseTitle());
    }
  }

  // ─── Focus: zero chrome, with four ways out ─────────────────────────────────
  // Focus removes the rail entirely. It is the state that can trap someone, so
  // its exits are specified before its entry: the persistent chip below, Esc, F,
  // and the edge peek. Four independent ways out, because any single mechanism
  // can fail a user — a chip can be overlooked, a key can be unknown, a hover
  // can be undiscovered. Together they are safe.
  //
  // Focus is SESSION-SCOPED and never persisted: a body class plus one
  // sessionStorage flag for the one-time toast. A new tab starts in whatever
  // Compact state the physician chose and never in Focus, which means reloading
  // always returns them to a known state. That is the single most important
  // property in this section.
  function focusOn() { return document.body.classList.contains('asc-focus'); }
  function enterFocus() {
    document.body.classList.add('asc-focus');
    let seen = true;
    try { seen = sessionStorage.getItem('asc_focus_seen') === '1'; } catch (_) { seen = true; }
    if (!seen) {
      try { sessionStorage.setItem('asc_focus_seen', '1'); } catch (_) { /* ignore quota */ }
      // The one moment you have their attention on how to leave, for 3.2s.
      toast('Focus mode. Press Esc to exit.');
    }
    syncChromeMetrics();   // the header just thinned; the rail is pinned to it
  }
  function exitFocus() {
    document.body.classList.remove('asc-focus', 'asc-rail-peek');
    syncChromeMetrics();
  }

  // Exit 1, the primary: a chip where the rail used to be. 55% opacity at rest
  // and 100% on hover — quiet enough not to compete with the case, present
  // enough that the eye finds it when it goes looking. It never fades out and
  // never auto-hides on a timer, because an exit that disappears is not an exit.
  // It carries the Esc hint inline, which is what makes the keyboard routes
  // discoverable rather than trivia.
  function renderFocusChip() {
    return h('button', {
      class: 'asc-focus-chip', type: 'button',
      'aria-label': 'Exit focus mode',
      title: 'Exit focus mode  ( Esc )',
      onClick: exitFocus,
    },
      h('span', { class: 'asc-focus-chip-ico', html: CHROME_ICONS.collapse, 'aria-hidden': 'true' }),
      h('span', { class: 'asc-focus-chip-label' }, 'FOCUS'),
      h('kbd', { class: 'asc-focus-chip-kbd' }, 'esc'));
  }

  // Exit 4, the accident-recovery path: hovering the extreme left edge slides
  // the rail back as an OVERLAY. It does not reflow the page, so a physician
  // reading a lab trend does not lose their place to check a nav badge. 250ms,
  // not instant: an instant trigger fires every time the cursor crosses the
  // screen and the rail flaps, while a quarter-second reads as intent.
  function armEdgePeek() {
    let t = null;
    document.addEventListener('mousemove', (e) => {
      if (!focusOn()) return;
      if (e.clientX <= 12 && !t) {
        // Re-check on fire: leaving focus mid-wait must not land a peek class
        // on a body that is no longer in focus.
        t = setTimeout(() => { if (focusOn()) document.body.classList.add('asc-rail-peek'); t = null; }, 250);
      } else if (e.clientX > 12 && t) { clearTimeout(t); t = null; }
    }, { passive: true });
    document.addEventListener('mouseover', (e) => {
      if (document.body.classList.contains('asc-rail-peek')
          && e.target.closest && !e.target.closest('.asc-rail') && !e.target.closest('.asc-focus-chip')) {
        document.body.classList.remove('asc-rail-peek');
      }
    }, { passive: true });
  }

  function renderSidePanel() {
    if (!state.user) { teardownSidePanel(); return; }
    document.body.classList.add('asc-has-rail');
    applyRailState();
    // The chip lives outside #ascRoot, next to the rail, so per-view re-renders
    // never wipe the only always-visible way out of Focus.
    if (!document.querySelector('.asc-focus-chip')) document.body.appendChild(renderFocusChip());
    // Mark the guide view so the print stylesheet can scope its aggressive
    // header/padding overrides to the manual only (never to eval/admin prints).
    document.body.classList.toggle('asc-view-guide', state.panel === 'guide');
    syncChromeMetrics();

    let rail = document.getElementById('ascRail');
    if (!rail) {
      rail = h('aside', { class: 'asc-rail', id: 'ascRail', 'aria-label': 'Portal navigation' });
      // Insert as a body child so it is never inside the header or #ascRoot.
      document.body.appendChild(rail);
    }
    // Bind the re-measure listeners exactly once for the app's lifetime, so
    // repeated sign-in/out cycles never stack duplicates. Re-measure on resize,
    // on full load, and once web fonts settle: the header's height changes when
    // Instrument Sans / IBM Plex Mono swap in, and the rail is pinned to it.
    if (!chromeMetricsBound) {
      window.addEventListener('resize', syncChromeMetrics);
      window.addEventListener('load', syncChromeMetrics);
      if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(syncChromeMetrics).catch(function () {});
      }
      chromeMetricsBound = true;
    }
    clear(rail);

    const nav = h('nav', { class: 'asc-rail-nav', 'aria-label': 'Sections' });
    visibleRailItems().forEach((item) => {
      const active = railItemActive(item.dest);
      const children = [
        h('span', { class: 'asc-rail-ico', 'aria-hidden': 'true', html: RAIL_ICONS[item.dest] }),
        h('span', { class: 'asc-rail-label' }, item.label),
      ];
      if (item.dest === 'community') children.push(communityBadgeEl());
      if (item.locked) children.push(h('span', { class: 'asc-rail-lock', 'aria-hidden': 'true' }, '\u00b7'));
      nav.appendChild(h('button', {
        type: 'button',
        class: 'asc-rail-item' + (active ? ' active' : '') + (item.locked ? ' locked' : ''),
        'aria-current': active ? 'page' : null,
        'aria-disabled': item.locked ? 'true' : null,
        // aria-label carries the accessible name even in the icon-collapsed rail,
        // where the visible label span is display:none (and thus off the a11y tree).
        'aria-label': item.external ? item.label + ' (opens in a new tab)' : item.label,
        title: item.external ? item.label + ' (opens in a new tab)' : item.label,
        onClick: () => {
          // A locked surface opens the explanation, not a 403.
          if (item.locked) { setPanel('verification'); return; }
          setPanel(item.dest);
        },
      }, children));
    });

    const specColor = specialtyDotColor(state.user.specialty);
    const foot = h('div', { class: 'asc-rail-foot' },
      h('div', { class: 'asc-rail-user' },
        h('span', { class: 'asc-rail-name', title: railDisplayName() }, railDisplayName()),
        state.user.specialty
          ? h('span', { class: 'asc-rail-spec' },
              h('span', { class: 'dot ' + specColor, 'aria-hidden': 'true' }),
              h('span', {}, state.user.specialty))
          : null),
      h('button', { type: 'button', class: 'asc-rail-signout', onClick: logout }, 'Sign out'));

    // The bottom edge of the rail is the one place that is neither a destination
    // nor an identity: top-of-rail competes with the wordmark, inline-with-nav
    // competes with the sections. margin-top:auto pins it above the user block.
    const collapseBtn = h('button', {
      class: 'asc-rail-collapse', type: 'button',
      'aria-expanded': String(!railCompact()),
      'aria-controls': 'ascRail',
      'aria-label': railCollapseTitle(),
      title: railCollapseTitle(),
      onClick: toggleRailCompact,
    }, h('span', { class: 'asc-rail-collapse-ico', html: CHROME_ICONS.chevrons, 'aria-hidden': 'true' }));

    rail.appendChild(nav);
    rail.appendChild(collapseBtn);
    rail.appendChild(foot);
  }

  function teardownSidePanel() {
    if (guideObserver) { guideObserver.disconnect(); guideObserver = null; }
    const rail = document.getElementById('ascRail');
    if (rail) rail.remove();
    const chip = document.querySelector('.asc-focus-chip');
    if (chip) chip.remove();
    // Signing out must never leave a login screen wearing Focus, whose only
    // exits were the chrome that just went away.
    exitFocus();
    document.body.classList.remove('asc-has-rail', 'asc-view-guide');
  }

  // Pin the fixed rail directly beneath the (variable-height) sticky header by
  // publishing the header height as a CSS var. Called on render + resize.
  function syncChromeMetrics() {
    const header = document.getElementById('ascHeader');
    const hVisible = header && !header.hasAttribute('hidden');
    const hh = hVisible ? header.offsetHeight : 0;
    // Only publish a real measurement. If layout isn't ready (offsetHeight 0),
    // clear the var so the CSS fallback (57px) applies instead of pinning the
    // rail to 0 and tucking its first item under the header.
    if (hh > 0) document.documentElement.style.setProperty('--asc-header-h', hh + 'px');
    else document.documentElement.style.removeProperty('--asc-header-h');
  }

  /* ── Community integration (Community PRD boundary; all endpoints optional) ── */

  function communityBadgeEl() {
    const n = state.community.unread | 0;
    const badge = h('span', {
      class: 'asc-rail-badge', id: 'ascCommunityBadge',
      hidden: n <= 0,
      'aria-label': n > 0 ? (n + ' unread') : null,
    },
      h('span', { class: 'dot dot-lime', 'aria-hidden': 'true' }),
      h('span', { class: 'asc-rail-badge-n' }, n > 99 ? '99+' : String(n)));
    return badge;
  }

  function updateCommunityBadge() {
    const rail = document.getElementById('ascRail');
    if (!rail) return;
    const old = document.getElementById('ascCommunityBadge');
    if (old) old.replaceWith(communityBadgeEl());
  }

  // Open the Community in a NEW TAB. window.open is called synchronously inside
  // the click gesture (never after an await) so it is never popup-blocked; a
  // pre-minted handoff token, if we have one, rides along to skip a second login.
  // noopener per the integration contract.
  function openCommunity() {
    const t = state.community.handoffToken;
    // Handoff codes are SINGLE-USE server-side: consume it here so a second
    // click inside the refresh window opens bare (same-origin session covers
    // it) instead of sending an already-spent code, and pre-mint the next one.
    state.community.handoffToken = null;
    const url = t ? ('/community?t=' + encodeURIComponent(t)) : '/community';
    window.open(url, '_blank', 'noopener');
    refreshCommunityHandoff();
  }

  // Mint a short-lived signed handoff token (reuses the Asclepius session). Kept
  // fresh by the poll loop so openCommunity() always has a recent one ready.
  // Degrades silently: any failure just means the community tab opens bare and
  // falls back to its own session cookie.
  async function refreshCommunityHandoff() {
    if (state.community.unavailable) return;
    const gen = communityGen;
    try {
      const res = await fetch('/community/handoff', {
        method: 'POST',
        headers: state.token ? { 'Authorization': 'Bearer ' + state.token } : {},
        credentials: 'include',
      });
      if (gen !== communityGen) return; // session changed mid-flight: drop result
      if (res.status === 404) { state.community.unavailable = true; return; }
      if (!res.ok) return;
      const data = await res.json().catch(() => null);
      if (gen !== communityGen) return; // re-check after the second await
      if (data && data.token) state.community.handoffToken = data.token;
    } catch (_) { /* network error: keep any existing token, open bare otherwise */ }
  }

  async function refreshCommunityUnread() {
    if (state.community.unreadUnavailable) return;
    const gen = communityGen;
    try {
      const res = await fetch('/community/unread', {
        headers: state.token ? { 'Authorization': 'Bearer ' + state.token } : {},
        credentials: 'include',
      });
      if (gen !== communityGen) return; // session changed mid-flight: drop result
      // 404 → the unread endpoint isn't deployed; back off permanently for this
      // session (mirrors handoff) so we don't hammer a missing route every 60s.
      if (res.status === 404) { state.community.unreadUnavailable = true; return; }
      if (!res.ok) return; // transient (500/timeout) → leave badge as-is, retry next tick
      const data = await res.json().catch(() => null);
      if (gen !== communityGen) return; // re-check after the second await
      const total = data && typeof data.total === 'number' ? data.total : 0;
      if (total !== state.community.unread) {
        state.community.unread = Math.max(0, total | 0);
        updateCommunityBadge();
      }
    } catch (_) { /* degrade silently: never surface a community error in the rail */ }
  }

  function pollCommunityOnce() {
    if (!state.community.unreadUnavailable) refreshCommunityUnread();
    if (!state.community.unavailable) refreshCommunityHandoff();
    // Both endpoints confirmed absent (community backend unbuilt) → stop polling
    // entirely; there is nothing left to fetch this session.
    if (state.community.unreadUnavailable && state.community.unavailable) stopCommunityPolling();
  }

  // Poll every 60s while the portal is open (integration contract §3).
  function startCommunityPolling() {
    stopCommunityPolling();
    pollCommunityOnce();
    state.community.pollTimer = setInterval(pollCommunityOnce, 60000);
  }
  function stopCommunityPolling() {
    if (state.community.pollTimer) { clearInterval(state.community.pollTimer); state.community.pollTimer = null; }
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     INSTRUCTION MANUAL (Guide): renders window.ASC_MANUAL (structured data)
     through this one component. Two columns: a sticky scroll-spy TOC + the
     sections. Three-line sections, good/weak examples, collapsed detail,
     per-section anchors, a reading-time estimate, and a print stylesheet.
     ═══════════════════════════════════════════════════════════════════════════ */

  let guideObserver = null;

  // PRD M — scoped manuals, picked by CAPABILITY rather than by tier: what a
  // physician can DO decides the document. Order is least to most, so the
  // default is the most senior manual they hold.
  const MANUAL_ROLES = [
    { key: 'labeler', capability: 'label', label: 'Labeling' },
    { key: 'reviewer', capability: 'review', label: 'Reviewing' },
  ];

  function heldManuals() {
    const manuals = window.ASC_MANUALS || {};
    return MANUAL_ROLES.filter((r) => manuals[r.key] && sessionCan(r.capability));
  }

  function renderGuide() {
    const manuals = window.ASC_MANUALS || {};
    const held = heldManuals();
    // Fall back to the labeler manual rather than to nothing: a session whose
    // capability list did not load must still get a document, and every
    // physician can label.
    const active = (state.manualRole && manuals[state.manualRole])
      ? state.manualRole
      : ((held.length ? held[held.length - 1].key : 'labeler'));
    const manual = manuals[active] || window.ASC_MANUAL;
    if (!manual || !Array.isArray(manual.sections)) {
      setRoot(h('div', { class: 'asc-wrap' },
        h('div', { class: 'asc-card asc-card-pad' },
          h('div', { class: 'asc-inline-error' }, 'The instruction manual failed to load.'))));
      return;
    }
    if (guideObserver) { guideObserver.disconnect(); guideObserver = null; }

    // `showWhen` is the one conditional field, and it names a verification
    // status. A section without one always renders.
    const sections = manual.sections.filter(
      (sec) => !sec.showWhen || sec.showWhen === (state.user && state.user.verification_status));

    // Sticky TOC (desktop): one link per section, scroll-spy highlights.
    const tocList = h('ul', { class: 'asc-guide-toc-list' });
    sections.forEach((sec) => {
      tocList.appendChild(h('li', {},
        h('a', {
          class: 'asc-guide-toc-link', href: '#' + sec.id, dataset: { tocId: sec.id },
          onClick: () => { setGuideHash(sec.id); },
        },
          h('span', { class: 'asc-guide-toc-num' }, sec.num),
          h('span', { class: 'asc-guide-toc-txt' }, sec.title))));
    });
    const toc = h('nav', { class: 'asc-guide-toc', 'aria-label': 'Contents' },
      h('div', { class: 'chrome asc-guide-toc-head' }, 'Contents'),
      tocList);

    // Mobile TOC: a dropdown that jumps to a section.
    const mobileSelect = h('select', {
      class: 'asc-guide-toc-select', 'aria-label': 'Jump to section',
      onChange: (e) => { const id = e.target.value; if (id) scrollToSection(id); },
    }, h('option', { value: '' }, 'Jump to a section…'),
       ...sections.map((s) => h('option', { value: s.id }, s.num + ' · ' + s.title)));

    // Content column: intro header + every section.
    const content = h('div', { class: 'asc-guide-content' });
    // The switcher: a mono chip row above the title, matching the existing
    // chrome pattern. Only when more than one manual is held — one chip is not a
    // choice, it is decoration. A reviewer re-reading the labeling manual is
    // reading it to grade someone against it, so nothing here is hidden from
    // anyone who holds it.
    const switcher = held.length > 1
      ? h('div', { class: 'asc-guide-switch', role: 'tablist', 'aria-label': 'Manual' },
          held.map((r) => {
            const btn = h('button', {
              class: 'asc-guide-switch-chip' + (r.key === active ? ' active' : ''),
              type: 'button', role: 'tab', 'aria-selected': r.key === active ? 'true' : 'false',
            }, r.label);
            btn.addEventListener('click', () => {
              if (r.key === active) return;
              state.manualRole = r.key;
              setGuideHash('');
              renderGuide();
            });
            return btn;
          }))
      : null;

    content.appendChild(h('header', { class: 'asc-guide-intro' },
      h('div', { class: 'chrome chrome-strong' }, 'INSTRUCTION MANUAL'),
      switcher,
      h('h1', { class: 'asc-guide-h1' }, manual.title),
      manual.subtitle ? h('p', { class: 'asc-guide-sub' }, manual.subtitle) : null,
      h('div', { class: 'asc-guide-meta' },
        h('span', { class: 'chip' },
          h('span', { class: 'dot dot-lime', 'aria-hidden': 'true' }),
          (manual.readingTimeMin || 5) + ' min read'),
        h('span', { class: 'asc-guide-meta-hint chrome' }, manual.metaHint || 'V3 · V4 tasks')),
      mobileSelect));

    sections.forEach((sec) => content.appendChild(guideSection(sec)));

    const layout = h('div', { class: 'asc-guide' }, toc, content);
    setRoot(layout);

    // Scroll-spy: highlight the section nearest the top of the viewport.
    setupGuideScrollSpy(sections);

    // Deep-link: if the URL already targets a section, jump to it after mount.
    const hash = (location.hash || '').replace('#', '');
    if (hash && sections.some((s) => s.id === hash)) {
      requestAnimationFrame(() => scrollToSection(hash));
    } else {
      const main = document.getElementById('ascRoot');
      if (main) main.scrollIntoView({ block: 'start' });
    }
  }

  function guideSection(sec) {
    const card = h('section', { class: 'asc-card asc-guide-section', id: sec.id, tabindex: '-1' });

    // Chrome micro-label + anchored title.
    card.appendChild(h('div', { class: 'asc-guide-sec-head' },
      h('span', { class: 'chrome asc-guide-sec-chrome' }, sec.num + ' · ' + (sec.chromeLabel || sec.title)),
      h('a', {
        class: 'asc-guide-anchor', href: '#' + sec.id,
        'aria-label': 'Link to “' + sec.title + '”', title: 'Copy link to this section',
        onClick: () => { setGuideHash(sec.id); },
      }, '#')));
    card.appendChild(h('h2', { class: 'asc-guide-sec-title' }, sec.title));

    // Body: the three-line form, or a list / note where the section calls for it.
    if (Array.isArray(sec.list)) {
      const ul = h('ul', { class: 'asc-guide-checklist' });
      sec.list.forEach((item) => ul.appendChild(h('li', {},
        h('span', { class: 'asc-guide-check', 'aria-hidden': 'true' }, '✓'),
        h('span', {}, item))));
      card.appendChild(ul);
    } else if (sec.note) {
      card.appendChild(h('blockquote', { class: 'asc-guide-note' }, guideNoteNodes(sec.note)));
    } else {
      card.appendChild(guideThreeLines(sec));
    }

    if (sec.wireframe) { const fig = guideWireframe(sec.wireframe); if (fig) card.appendChild(fig); }

    if (sec.example) card.appendChild(guideExample(sec.example));

    (sec.callouts || []).forEach((c) => card.appendChild(guideCallout(c)));

    if (sec.detail) card.appendChild(guideDetail(sec.detail));

    return card;
  }

  function guideThreeLines(sec) {
    const wrap = h('div', { class: 'asc-guide-lines' });
    const line = (tag, cls, text) => h('div', { class: 'asc-guide-line ' + cls },
      h('span', { class: 'chrome asc-guide-line-tag' }, tag),
      h('span', { class: 'asc-guide-line-txt' }, text));
    if (sec.what) wrap.appendChild(line('WHAT', 'is-what', sec.what));
    if (sec.why) wrap.appendChild(line('WHY', 'is-why', sec.why));
    if (sec.how) wrap.appendChild(line('HOW', 'is-how', sec.how));
    return wrap;
  }

  function guideExample(ex) {
    return h('div', { class: 'asc-guide-eg' },
      h('div', { class: 'asc-guide-eg-col asc-guide-eg-good' },
        h('div', { class: 'asc-guide-eg-head' },
          h('span', { class: 'dot dot-green', 'aria-hidden': 'true' }),
          h('span', { class: 'chrome' }, 'STRONG')),
        h('p', { class: 'asc-guide-eg-txt' }, ex.good)),
      h('div', { class: 'asc-guide-eg-col asc-guide-eg-weak' },
        h('div', { class: 'asc-guide-eg-head' },
          h('span', { class: 'dot dot-pink', 'aria-hidden': 'true' }),
          h('span', { class: 'chrome' }, 'WEAK')),
        h('p', { class: 'asc-guide-eg-txt' }, ex.weak)));
  }

  function guideCallout(c) {
    const isMistake = c.kind === 'mistake';
    return h('div', { class: 'asc-guide-callout ' + (isMistake ? 'is-mistake' : 'is-why') },
      h('span', { class: 'chrome asc-guide-callout-tag' }, isMistake ? 'COMMON MISTAKE' : 'WHY THIS MATTERS'),
      h('span', { class: 'asc-guide-callout-txt' }, c.text));
  }

  function guideDetail(detail) {
    const paras = Array.isArray(detail) ? detail : [detail];
    const d = h('details', { class: 'asc-guide-detail' },
      h('summary', { class: 'asc-guide-detail-sum' }, 'Show detail'));
    paras.forEach((p) => d.appendChild(h('p', { class: 'asc-guide-detail-p' }, p)));
    return d;
  }

  // Linkify a note: bold #channel tokens, mailto: for email addresses.
  function guideNoteNodes(text) {
    const nodes = [];
    const re = /(#[a-z0-9-]+|[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})/gi;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) nodes.push(text.slice(last, m.index));
      const tok = m[0];
      if (tok[0] === '#') nodes.push(h('strong', { class: 'asc-guide-channel' }, tok));
      else nodes.push(h('a', { class: 'asc-guide-link', href: 'mailto:' + tok }, tok));
      last = m.index + tok.length;
    }
    if (last < text.length) nodes.push(text.slice(last));
    return nodes;
  }

  // Update the URL hash without a scroll jump (native anchor handles scrolling).
  function setGuideHash(id) {
    try { history.replaceState(null, '', '#' + id); } catch (_) { /* ignore */ }
  }

  function prefersReducedMotion() {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) { return false; }
  }

  function scrollToSection(id) {
    const el = document.getElementById(id);
    if (!el) return;
    // Honor reduced-motion for programmatic scroll: the JS smooth option ignores
    // the CSS scroll-behavior override, so gate it explicitly.
    el.scrollIntoView({ block: 'start', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    setGuideHash(id);
    // Move focus for keyboard/AT users without yanking the scroll position.
    try { el.focus({ preventScroll: true }); } catch (_) { el.focus(); }
  }

  function setupGuideScrollSpy(sections) {
    if (!('IntersectionObserver' in window)) return;
    const setActive = (id) => {
      document.querySelectorAll('.asc-guide-toc-link').forEach((a) => {
        a.classList.toggle('active', a.dataset.tocId === id);
        if (a.dataset.tocId === id) a.setAttribute('aria-current', 'true');
        else a.removeAttribute('aria-current');
      });
    };
    guideObserver = new IntersectionObserver((entries) => {
      // Choose the entry closest to the top band that is intersecting.
      const visible = entries.filter((e) => e.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible.length) setActive(visible[0].target.id);
    }, { rootMargin: '-38% 0px -55% 0px', threshold: 0 });
    sections.forEach((s) => { const el = document.getElementById(s.id); if (el) guideObserver.observe(el); });
    if (sections.length) setActive(sections[0].id);
  }

  // ── Labeled token-palette SVG wireframes (monochrome ink; accent classes only
  //    where the green/pink semantics carry meaning). Recognizable, not literal. ──
  function guideWireframe(kind) {
    const svg = GUIDE_WIREFRAMES[kind];
    if (!svg) return null;
    return h('figure', { class: 'asc-guide-fig', 'aria-hidden': 'true' },
      h('div', { class: 'asc-guide-fig-inner', html: svg }));
  }

  const GUIDE_WIREFRAMES = {
    casePanel:
      '<svg viewBox="0 0 300 104" role="img">' +
      '<g class="wl">' +
      '<rect x="8" y="10" width="46" height="16" rx="4"/><rect x="58" y="10" width="40" height="16" rx="4" class="wf"/>' +
      '<rect x="102" y="10" width="42" height="16" rx="4"/><rect x="148" y="10" width="40" height="16" rx="4"/><rect x="192" y="10" width="44" height="16" rx="4"/>' +
      '<rect x="8" y="36" width="284" height="60" rx="6" class="wbox"/>' +
      '<polyline points="24,80 60,66 96,72 132,50 168,58 204,40 240,46 276,30" class="wtrend"/>' +
      '<circle cx="132" cy="50" r="3.5" class="wdot"/></g></svg>',
    compare:
      '<svg viewBox="0 0 300 104" role="img"><g class="wl">' +
      '<rect x="8" y="10" width="134" height="84" rx="6" class="wbox"/>' +
      '<rect x="158" y="10" width="134" height="84" rx="6" class="wbox"/>' +
      '<text x="20" y="28" class="wtx">A</text><text x="170" y="28" class="wtx">B</text>' +
      '<rect x="20" y="40" width="110" height="7" rx="3.5" class="wln"/>' +
      '<rect x="20" y="54" width="90" height="7" rx="3.5" class="wln wmark"/>' +
      '<rect x="20" y="68" width="100" height="7" rx="3.5" class="wln"/>' +
      '<rect x="170" y="40" width="110" height="7" rx="3.5" class="wln"/>' +
      '<rect x="170" y="54" width="90" height="7" rx="3.5" class="wln wmark2"/>' +
      '<rect x="170" y="68" width="100" height="7" rx="3.5" class="wln"/></g></svg>',
    verdict:
      '<svg viewBox="0 0 300 76" role="img"><g class="wl">' +
      '<rect x="8" y="20" width="88" height="36" rx="8" class="wbox wgreenline"/>' +
      '<text x="52" y="43" class="wtc">A is better</text>' +
      '<rect x="106" y="20" width="88" height="36" rx="8" class="wbox"/>' +
      '<text x="150" y="43" class="wtc">B is better</text>' +
      '<rect x="204" y="20" width="88" height="36" rx="8" class="wbox wpinkline"/>' +
      '<text x="248" y="43" class="wtc">Both inadequate</text></g></svg>',
    reasoning:
      '<svg viewBox="0 0 300 104" role="img"><g class="wl">' +
      '<circle cx="20" cy="20" r="8" class="wnum"/><text x="20" y="24" class="wtn">1</text><rect x="38" y="14" width="240" height="10" rx="5" class="wln"/>' +
      '<circle cx="20" cy="46" r="8" class="wnum"/><text x="20" y="50" class="wtn">2</text><rect x="38" y="40" width="220" height="10" rx="5" class="wln"/>' +
      '<circle cx="20" cy="72" r="8" class="wnum wpinkfill"/><text x="20" y="76" class="wtn">3</text><rect x="38" y="66" width="128" height="10" rx="5" class="wln wmark2"/>' +
      '<text x="292" y="75" class="wflag">first break</text></g></svg>',
    rubric:
      '<svg viewBox="0 0 300 104" role="img"><g class="wl">' +
      '<rect x="8" y="12" width="284" height="26" rx="6" class="wbox"/>' +
      '<rect x="18" y="21" width="180" height="8" rx="4" class="wln"/>' +
      '<rect x="212" y="18" width="70" height="14" rx="7" class="wpill wgreenfill"/>' +
      '<rect x="8" y="44" width="284" height="26" rx="6" class="wbox"/>' +
      '<rect x="18" y="53" width="150" height="8" rx="4" class="wln"/>' +
      '<rect x="212" y="50" width="70" height="14" rx="7" class="wpill"/>' +
      '<rect x="8" y="76" width="284" height="26" rx="6" class="wbox wpinkline"/>' +
      '<rect x="18" y="85" width="160" height="8" rx="4" class="wln wmark2"/>' +
      '<rect x="204" y="82" width="78" height="14" rx="7" class="wpill wpinkfill2"/></g></svg>',
  };

  // ─── Auth / bootstrap ────────────────────────────────────────────────────--

  /** The server's reason when an account exists but is not allowed in yet.
   *
   *  A 403 from `/auth/me` or `/taxonomy` is NOT a stale session. It is
   *  `get_current_user` refusing a `pending` (awaiting credential verification)
   *  or `rejected` account, and the backend already writes the sentence a
   *  physician needs to read ("Your account is awaiting credential
   *  verification — you'll hear from us within 24 hours."). Treating it like an
   *  expired token — silently dropping the token and rendering a blank login
   *  form — is how a normal, expected waiting state came to look like a broken
   *  product. Returns `{state, message}` where `state` is 'pending' or
   *  'rejected', or null when this is an ordinary auth failure the existing
   *  paths should handle. */
  function verificationGate(err) {
    if (!err || err.status !== 403) return null;
    const gate = err.authGate === 'pending' || err.authGate === 'rejected'
      ? err.authGate : null;
    // A 403 with no gate header is one of the other deny-by-default role gates
    // (data_partner, buyer). Those are not a waiting state and must not be
    // dressed as one — return null and let the caller show the plain message.
    if (!gate) return null;
    const detail = err.detail;
    return {
      state: gate,
      message: (typeof detail === 'string' && detail.trim())
        ? detail.trim()
        : (gate === 'pending'
          ? 'Your credentials are still being verified.'
          : 'This account was not approved for the evaluator portal.'),
    };
  }

  // Landing → Asclepius sign-in handoff (mirrors doctor-sign-in.html's own
  // consumeHandoffFromUrl for the tenant plane). The landing SPA can be a
  // different origin, so it can't write localStorage['asclepius_token']
  // directly — it trades the token for a short-lived, single-use code
  // (POST /api/asclepius/auth/portal-handoff) and redirects here with
  // ?asc_handoff=<code>; this consumes it and stores the real token before
  // the normal boot sequence below runs.
  async function consumeHandoffFromUrl() {
    const url = new URL(window.location.href);
    const code = (url.searchParams.get('asc_handoff') || '').trim();
    if (!code) return;
    url.searchParams.delete('asc_handoff');
    const qs = url.searchParams.toString();
    window.history.replaceState(null, '', url.pathname + (qs ? '?' + qs : '') + url.hash);
    try {
      const res = await fetch(API_BASE + '/auth/portal-handoff/consume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ handoff_code: code }),
      });
      if (!res.ok) return;
      const data = await res.json().catch(() => null);
      if (data && data.token) {
        state.token = data.token;
        try { localStorage.setItem(TOKEN_KEY, data.token); } catch (_) { /* ignore quota */ }
        try { localStorage.removeItem(SUPPRESS_SSO_KEY); } catch (_) { /* ignore */ }
      }
    } catch (_) { /* network error — fall through to the normal boot sequence */ }
  }

  async function boot() {
    await consumeHandoffFromUrl();
    // 1) Resume an existing Asclepius session if the stored token is still valid.
    if (state.token) {
      try {
        // noAuthHandler: a stale/expired token must NOT short-circuit to the
        // "session expired" screen. We want to fall through to SSO below.
        state.user = await api('/auth/me', { noAuthHandler: true });
        await loadTaxonomy();
        renderHeader();
        enterApp();
        return;
      } catch (e) {
        // `/auth/me` succeeds and `/taxonomy` then 403s for a gated account, so
        // the email is known here even though the boot did not complete.
        const knownEmail = (state.user && state.user.email) || null;
        // Stale token (or transient): drop it and try the seamless paths.
        state.token = null;
        state.user = null;
        try { localStorage.removeItem(TOKEN_KEY); } catch (_) { /* ignore */ }
        // …unless the account is real and simply gated. SSO would mint a fresh
        // token for the same account and hit the same 403, so falling through
        // would just repeat the silence. Say why, and stop here.
        const gate = verificationGate(e);
        if (gate) { renderGated(gate, knownEmail); return; }
      }
    }
    // 2) Already signed into the doctor portal? Exchange that session for an
    //    Asclepius one (SSO): no second login barrier. Skipped right after an
    //    explicit sign-out so the user can choose to sign in with their
    //    onboarding (workspace) credentials instead of being pulled back into
    //    the doctor-portal identity.
    let suppressSso = false;
    try { suppressSso = localStorage.getItem(SUPPRESS_SSO_KEY) === '1'; } catch (_) { suppressSso = false; }
    if (!suppressSso) {
      const sso = await trySsoLogin();
      if (sso.entered) return;
      // An SSO account is provisioned `pending` on purpose
      // (routers/asclepius.py), so "we are still verifying you" is the NORMAL
      // outcome for a clinician arriving from the doctor portal, not an edge
      // case — and it gets the screen written for it, not a bare form.
      if (sso.gate) { renderGated(sso.gate, sso.email); return; }
    }
    // 3) Otherwise, fall back to the manual login form.
    renderLogin();
  }

  // Silent SSO: trade a doctor-portal token for an Asclepius session. Uses a raw
  // fetch (not api()) so a rejected probe doesn't trip the 401 session handler;
  // an unknown/expired doctor token just means "fall back to the login form".
  //
  // Returns {entered, gate, email}. `entered` is the old boolean — true means we
  // are inside the app and the caller must stop. `gate` is set when the bridge
  // failed for a reason the user can act on (today: the account is real but
  // awaiting credential verification), and carries the state + the server's
  // message. An ordinary "no doctor session / bad token" failure carries no
  // gate and the login form renders bare, exactly as before.
  const SSO_NO = { entered: false, gate: null, email: null };

  async function trySsoLogin() {
    let doctorToken = '';
    try { doctorToken = localStorage.getItem(DOCTOR_TOKEN_KEY) || ''; } catch (e) { doctorToken = ''; }
    if (!doctorToken) return SSO_NO;
    let res;
    try {
      res = await fetch(API_BASE + '/auth/sso', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: doctorToken }),
      });
    } catch (e) {
      return SSO_NO; // network error; let renderLogin() show the form
    }
    if (!res.ok) return SSO_NO; // 401 (bad token) / 403 (no evaluator account)
    let data = null;
    try { data = await res.json(); } catch (e) { return SSO_NO; }
    if (!data || !data.token) return SSO_NO;
    state.token = data.token;
    state.user = data.user;
    try { localStorage.setItem(TOKEN_KEY, data.token); } catch (e) { /* ignore quota */ }
    try { localStorage.removeItem(SUPPRESS_SSO_KEY); } catch (_) { /* ignore */ }
    try {
      await loadTaxonomy();
    } catch (e) {
      // /auth/sso hands back 200 + a token WITHOUT running the verification
      // gate, so a pending clinician gets a valid token and then 403s here.
      // Leaving that token in localStorage made the next reload resume it, 403
      // again, and land on the same blank form — the loop that made a normal
      // waiting state look like a broken login. Drop it and carry the reason
      // out to the caller.
      const knownEmail = (state.user && state.user.email) || null;
      state.token = null;
      state.user = null;
      state.taxonomy = null;
      try { localStorage.removeItem(TOKEN_KEY); } catch (_) { /* ignore */ }
      return { entered: false, gate: verificationGate(e), email: knownEmail };
    }
    renderHeader();
    enterApp();
    return { entered: true, gate: null, email: null };
  }

  async function loadTaxonomy() {
    if (state.taxonomy) return state.taxonomy;
    state.taxonomy = await api('/taxonomy');
    return state.taxonomy;
  }

  function enterApp() {
    // Land on the dashboard (home), not straight into a case. The dashboard
    // routes into the existing eval flow when the doctor picks or starts a case.
    state.view = 'home';
    state.panel = 'tasks';
    // Fresh community state for this session. On a shared device, a prior user's
    // unread count or (more importantly) their signed handoff token must never
    // bleed into the next login before the first poll overwrites it.
    resetCommunityState();
    renderHeader();
    renderSidePanel();
    startCommunityPolling();
    // First-run tutorial gate (Calibration Case 1): a brand-new evaluator lands
    // in the practice case; in_progress resumes it. completed/skipped NEVER
    // re-trigger (server-authoritative via PATCH /me/tutorial). Admin/QA skip it.
    const tut = (state.user && state.user.tutorial) || {};
    if (state.user.role === 'evaluator'
        && (tut.status === 'not_started' || tut.status === 'in_progress')) {
      startTutorial({ resume: tut.status === 'in_progress', resumeStep: tut.step });
      return;
    }
    renderDashboardView();
  }

  // Bumping the generation invalidates any community fetch that is still in
  // flight: its late-resolving .then() sees a newer generation and drops its
  // result, so a hung request from a previous user can never write that user's
  // unread count (or, critically, their signed handoff token) into this
  // session's state on a shared device.
  function resetCommunityState() {
    communityGen++;
    state.community.unread = 0;
    state.community.handoffToken = null;
    state.community.unavailable = false;       // handoff endpoint
    state.community.unreadUnavailable = false; // unread endpoint
  }

  function logout() {
    state.token = null;
    state.user = null;
    // Tear down the logged-in-only chrome (corner ? tab, its menu, the panel).
    const instrTab = document.getElementById('ascInstrTab');
    if (instrTab) instrTab.remove();
    const cornerMenu = document.getElementById('ascCornerMenu');
    if (cornerMenu) cornerMenu.remove();
    const instrDrawer = document.getElementById('ascInstrDrawer');
    if (instrDrawer) instrDrawer.remove();
    localStorage.removeItem(TOKEN_KEY);
    // Suppress the silent doctor-portal SSO on the next boot so signing out
    // actually lands on the sign-in form (otherwise an active doctor session
    // would re-exchange straight back in, trapping the user on that identity).
    try { localStorage.setItem(SUPPRESS_SSO_KEY, '1'); } catch (_) { /* ignore */ }
    stopTimer();
    stopCommunityPolling();
    resetCommunityState(); // bump generation now so any in-flight fetch is voided
    teardownSidePanel();
    renderHeader();
    renderLogin();
  }

  // ─── Login screen ────────────────────────────────────────────────────────--
  /** `tone` is 'error' (default) or 'notice'. A notice is a message about a
   *  state the physician is legitimately in — "we are still verifying your
   *  credentials" — not a failure they caused or can retry away. */
  function renderLogin(errorMsg, tone) {
    // Defensive: the login screen is never behind the portal chrome. Every caller
    // already tears the rail down, but guarantee it here so a stray path can't
    // leave an orphaned rail over the sign-in form.
    teardownSidePanel();
    document.getElementById('ascHeader').setAttribute('hidden', '');
    // Accepts an email OR a username/id (e.g. the `mockadmin` sandbox login), so
    // it's a plain text field, not type=email (which would block a username).
    const emailInput = h('input', { class: 'asc-input', type: 'text', placeholder: 'you@hospital.org or username', autocomplete: 'username', required: 'required' });
    const pwInput = h('input', { class: 'asc-input', type: 'password', placeholder: '••••••••', autocomplete: 'current-password', required: 'required' });
    const errBox = h('div', {
      class: 'asc-login-error' + (tone === 'notice' ? ' asc-login-notice' : ''),
      hidden: !errorMsg,
    }, errorMsg || '');
    const submitBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-block asc-btn-lg', type: 'submit' }, 'Sign in');

    const form = h('form', {
      onSubmit: async (e) => {
        e.preventDefault();
        errBox.setAttribute('hidden', '');
        submitBtn.setAttribute('disabled', '');
        submitBtn.textContent = 'Signing in…';
        try {
          const data = await api('/auth/login', {
            method: 'POST',
            body: { email: emailInput.value.trim(), password: pwInput.value },
            // A bad password is a 401 we want to show inline ("Invalid email or
            // password"), NOT swallow as a global "session expired" redirect.
            noAuthHandler: true,
          });
          state.token = data.token;
          state.user = data.user;
          localStorage.setItem(TOKEN_KEY, data.token);
          // The user made an explicit identity choice, so allow SSO again later.
          try { localStorage.removeItem(SUPPRESS_SSO_KEY); } catch (_) { /* ignore */ }
          await loadTaxonomy();
          renderHeader();
          enterApp();
        } catch (err) {
          // A pending account signing in with the RIGHT password has proved who
          // they are — the only thing left is the wait, so send them to the
          // screen that explains it rather than leaving them on a form they have
          // already completed correctly. Every other failure stays inline.
          const gate = verificationGate(err);
          if (gate && gate.state === 'pending') {
            const signedInEmail = (state.user && state.user.email)
              || emailInput.value.trim() || null;
            // The token was minted before the gate rejected the follow-up call;
            // leaving it would make the next reload resume straight back into
            // the same 403.
            state.token = null;
            state.user = null;
            state.taxonomy = null;
            try { localStorage.removeItem(TOKEN_KEY); } catch (_) { /* ignore */ }
            renderGated(gate, signedInEmail);
            return;
          }
          // A refused account is not waiting for anything: the answer is final
          // and inline is the right place for it. Still a notice, not an alarm —
          // pink in this palette means flag / PHI / critical.
          errBox.classList.toggle('asc-login-notice', !!gate);
          errBox.textContent = (gate && gate.message) || err.message || 'Sign in failed';
          errBox.removeAttribute('hidden');
          submitBtn.removeAttribute('disabled');
          submitBtn.textContent = 'Sign in';
        }
      },
    },
      errBox,
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Email or username'), emailInput),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Password'), pwInput),
      submitBtn,
    );

    // Recovery. Until this existed the only route back into a forgotten account
    // was to ask someone to re-run onboarding, which silently replaced the
    // password. The endpoint answers identically for a known and an unknown
    // address, so there is nothing to branch on here and nothing to leak.
    const forgot = h('button', {
      type: 'button',
      class: 'asc-linkish',
      onClick: async () => {
        const addr = (emailInput && emailInput.value || '').trim();
        if (!addr) { errBox.classList.add('asc-login-notice'); errBox.textContent = 'Enter your email above first.'; return; }
        try {
          const res = await fetch(API_BASE + '/auth/password/forgot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: addr }),
          });
          const data = await res.json().catch(() => null);
          errBox.classList.add('asc-login-notice');
          errBox.textContent = (data && data.message)
            || "If that email has an Asclepius account, we've sent a reset link.";
        } catch (_) {
          errBox.classList.add('asc-login-notice');
          errBox.textContent = 'Could not reach the server. Try again in a moment.';
        }
      },
    }, 'Forgot your password?');

    const body = h('div', { class: 'asc-login-body' },
      form,
      h('div', { class: 'asc-login-forgot' }, forgot),
      h('p', { class: 'asc-login-hint' }, 'Board-certified clinician access only. Contact your program administrator for credentials.'),
    );
    // Escape hatch for clinicians who reach the portal from the doctor portal:
    // only shown when a doctor session exists, so signing out (which suppresses
    // the silent SSO) never traps an SSO-only user on a password form.
    let hasDoctorToken = false;
    try { hasDoctorToken = !!localStorage.getItem(DOCTOR_TOKEN_KEY); } catch (_) { hasDoctorToken = false; }
    if (hasDoctorToken) {
      body.appendChild(h('button', {
        class: 'asc-btn-link', type: 'button', style: 'display:block;margin:14px auto 0',
        onClick: async () => {
          try { localStorage.removeItem(SUPPRESS_SSO_KEY); } catch (_) { /* ignore */ }
          const sso = await trySsoLogin();
          // Prefer the server's reason ("awaiting credential verification") over
          // the generic one: the clinician pressed this button deliberately, so
          // "could not resume" would be the second time we told them nothing.
          if (sso.entered) return;
          if (sso.gate) renderGated(sso.gate, sso.email);
          else renderLogin('Could not resume your clinical session. Sign in above.');
        },
      }, 'Continue with my clinical portal session'));
    }
    const card = h('div', { class: 'asc-login-card' },
      h('div', { class: 'asc-login-head' },
        h('div', { class: 'asc-login-mark', 'aria-hidden': 'true' }),
        h('h1', {}, 'Asclepius'),
        h('p', {}, 'Expert Evaluation Portal'),
      ),
      body,
    );
    setRoot(h('div', { class: 'asc-login-wrap' }, card));
    setTimeout(() => emailInput.focus(), 30);
  }

  // ─── Awaiting verification ───────────────────────────────────────────────--
  //
  // The waiting state, as a screen.
  //
  // A `pending` physician cannot enter the portal at all — the verification gate
  // lives in `get_current_user`, so /auth/me and /taxonomy both 403 and no
  // in-app surface can render for them. The Guide's `awaiting-verification`
  // section was therefore unreachable no matter what `public_user()` returned:
  // the reader it was written for never gets far enough to open the Guide. So
  // the section is rendered HERE, in the one place that user actually lands,
  // from the same `manual-content.js` data the Guide reads — one copy of the
  // words, and the gate is not weakened by a single line.
  //
  // Only for `pending`. A rejected account is not waiting for anything, and
  // showing it "most decisions take one to two business days" would be a lie.
  function awaitingVerificationSection() {
    const manuals = window.ASC_MANUALS || {};
    const order = [manuals.labeler, manuals.reviewer, window.ASC_MANUAL];
    for (let i = 0; i < order.length; i++) {
      const secs = (order[i] && order[i].sections) || [];
      for (let j = 0; j < secs.length; j++) {
        if (secs[j] && secs[j].id === 'awaiting-verification') return secs[j];
      }
    }
    return null;
  }

  function renderAwaitingVerification(gate, email) {
    teardownSidePanel();
    document.getElementById('ascHeader').setAttribute('hidden', '');
    const sec = awaitingVerificationSection();

    const body = h('div', { class: 'asc-login-body' });
    // The server's own sentence, first and unedited — it is the one that carries
    // the timeframe the physician is actually waiting on.
    body.appendChild(h('div', { class: 'asc-login-error asc-login-notice' },
      gate.message));

    if (sec) {
      const rows = [['What', sec.what], ['Why', sec.why], ['What to do', sec.how]];
      const dl = h('div', { class: 'asc-wait-rows' });
      rows.forEach((r) => {
        if (!r[1]) return;
        dl.appendChild(h('div', { class: 'asc-wait-row' },
          h('div', { class: 'asc-wait-row-label' }, r[0]),
          h('div', { class: 'asc-wait-row-text' }, r[1])));
      });
      body.appendChild(dl);
      (sec.detail || []).forEach((p) => {
        body.appendChild(h('p', { class: 'asc-wait-detail' }, p));
      });
    }

    if (email) {
      body.appendChild(h('p', { class: 'asc-login-hint' },
        'We will email ' + email + ' either way.'));
    }

    const again = h('button', {
      class: 'asc-btn asc-btn-primary asc-btn-block asc-btn-lg', type: 'button',
    }, 'Check again');
    again.addEventListener('click', async () => {
      again.setAttribute('disabled', '');
      again.textContent = 'Checking…';
      try {
        // boot() re-walks resume → SSO → login and lands back here if the answer
        // has not changed. Nothing is cached, so this is a real re-check.
        await boot();
      } catch (e) {
        // boot() normally renders the next screen itself. If it throws, this
        // screen is still on-page and the button must not be left reading
        // "Checking…" forever — a dead control is how a waiting physician
        // concludes the product is broken, which is the whole failure here.
        again.removeAttribute('disabled');
        again.textContent = 'Check again';
      }
    });
    body.appendChild(again);

    const other = h('button', {
      class: 'asc-btn-link', type: 'button', style: 'display:block;margin:14px auto 0',
    }, 'Sign in with a different account');
    other.addEventListener('click', () => {
      // An explicit identity choice: suppress the silent SSO exactly as sign-out
      // does, or the doctor-portal token pulls them straight back to this screen.
      try { localStorage.setItem(SUPPRESS_SSO_KEY, '1'); } catch (_) { /* ignore */ }
      renderLogin();
    });
    body.appendChild(other);

    setRoot(h('div', { class: 'asc-login-wrap' },
      h('div', { class: 'asc-login-card' },
        h('div', { class: 'asc-login-head' },
          h('div', { class: 'asc-login-mark', 'aria-hidden': 'true' }),
          h('h1', {}, (sec && sec.title) || 'Your credentials are being verified'),
          h('p', {}, 'Asclepius · Expert Evaluation Portal'),
        ),
        body)));
  }

  /** Route a gated 403 to the right screen: the waiting screen for `pending`,
   *  the login form carrying the reason for `rejected` (there is no wait to
   *  explain, and the account may not be the one they meant to use). */
  function renderGated(gate, email) {
    if (gate.state === 'pending') renderAwaitingVerification(gate, email);
    else renderLogin(gate.message, 'notice');
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  EVALUATOR WORKSPACE
  // ═══════════════════════════════════════════════════════════════════════════
  async function renderEvalView() {
    // Home page: the evaluator picks their experience (V3 seamless (the
    // recommended default) / V2 assisted / V1 classic) before any labeling. Shown
    // on entry until a choice is made this session (and again on "Change experience").
    if (!state.portalChosen) { renderVersionHome(); return; }
    // V3/V4 are the specialty-scoped flows: pick the specialty before the case
    // loads (PRD §1). V1/V2 are text prompts and skip the picker.
    const ver = getPortalVersion();
    const needsSpecialty = (ver === 'v3' || ver === 'v4') && !state.specialtyChosen;
    const wrap = h('div', { class: 'asc-wrap' });
    const loadCard = h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), 'Loading next evaluation…'));
    wrap.appendChild(loadCard);
    setRoot(wrap);
    // §2: no separate route for the picker. The workspace scaffold mounts once;
    // the picker floats over it and hands control back here, so entry costs one
    // render instead of two full page transitions.
    if (needsSpecialty) {
      stopTimer();
      loadCard.hidden = true;
      await renderSpecialtyPicker({ dismissable: false });
      loadCard.hidden = false;
    }
    try {
      // Declare the active flow so the server applies it: V3 serves the hard-case
      // queue (difficulty=hard only) with value-aware routing; V2 value-aware;
      // V1 classic. WITHOUT this param the server safely falls back to the classic
      // oldest-first queue, i.e. the whole V3/V2 serving path is dead unless the
      // client sends its selected version here.
      const data = await api('/tasks/next?portal_version=' + encodeURIComponent(getPortalVersion())
        + '&specialty=' + encodeURIComponent(getPortalSpecialty()));
      state.task = data.task;
      if (!state.task) { renderEvalEmpty(); return; }
      initDraftForTask(state.task);
      // Resuming straight into the compare stage (e.g. mid-task refresh) needs the
      // withheld answer texts loaded before they're rendered.
      if (state.draft.stage === 'compare') {
        try { await loadWithheldAnswersIfNeeded(); } catch (e) { /* compare shows a reload hint */ }
      }
      renderTaskWorkspace();
    } catch (e) {
      if (e.status !== 401) {
        setRoot(h('div', { class: 'asc-wrap' },
          h('div', { class: 'asc-card asc-card-pad' },
            h('div', { class: 'asc-inline-error' }, 'Could not load the next task: ' + e.message))));
      }
    }
  }

  function renderEvalEmpty() {
    stopTimer();
    updateHeaderProgress(); // no open task, so the §16 bar hides here
    const ver = getPortalVersion();
    const isSpecScoped = ver === 'v3' || ver === 'v4';
    const sp = getPortalSpecialty();
    const spLabel = sp.charAt(0).toUpperCase() + sp.slice(1);
    setRoot(h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'asc-empty' },
          h('div', { class: 'asc-empty-icon' }, '✓'),
          h('h3', {}, isSpecScoped ? ('No ' + spLabel + ' cases available yet') : 'Your queue is clear'),
          h('p', {}, isSpecScoped
            ? ('No ' + spLabel + ' cases are available right now. Check back soon.')
            : 'No evaluation tasks are waiting for you right now. Check back soon.'),
          h('div', { style: 'margin-top:16px' },
            h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', onClick: renderEvalView }, 'Refresh queue')),
        ))));
  }

  // ─── Verification status, INSIDE the product ────────────────────────────────
  // This used to be a wall in front of the app: a physician finished a five-step
  // signup and got a screen telling them to come back tomorrow. The same words
  // are worth saying, but from inside, next to the things they CAN do.

  function provisionalBannerEl() {
    if (!sessionIsProvisional()) return null;
    return h('div', { class: 'asc-provisional-banner', role: 'status' },
      h('span', { class: 'asc-provisional-dot', 'aria-hidden': 'true' }),
      h('div', { class: 'asc-provisional-copy' },
        h('strong', {}, 'We are verifying your credentials.'),
        ' Usually one to two business days. Try the practice case and meet '
        + 'colleagues in the community while you wait. Real cases and earnings '
        + 'open as soon as we are done.'),
      h('button', {
        type: 'button',
        class: 'asc-btn asc-btn-ghost asc-btn-sm',
        onClick: () => setPanel('verification'),
      }, 'What happens next'));
  }

  // The panel a locked rail item opens. Built from the SAME manual section the
  // old waiting screen used (id 'awaiting-verification'), so the copy has one
  // home and the guide and this panel cannot drift apart.
  function renderVerificationPanel() {
    state.view = 'verification';
    renderHeader();
    const sec = awaitingVerificationSection();
    const rows = (sec && sec.rows) || [];
    const card = h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-chrome' }, 'Verification'),
      h('h2', { class: 'asc-verif-title' }, 'We are checking your credentials.'),
      h('p', {},
        'This usually takes one to two business days. You do not need to do '
        + 'anything, and we will email you either way.'),
      rows.length
        ? h('div', { class: 'asc-verif-rows' }, ...rows.map((r) => h('div', { class: 'asc-verif-row' },
            h('div', { class: 'asc-chrome' }, r.label || ''),
            h('div', {}, r.body || ''))))
        : null,
      h('div', { class: 'asc-verif-open' },
        h('div', { class: 'asc-chrome' }, 'Open to you now'),
        h('ul', {},
          h('li', {}, 'The practice case, start to finish.'),
          h('li', {}, 'The community: introduce yourself and follow the medical-AI digest.'),
          h('li', {}, 'The guide, so you know how the work runs before your first real case.')))));
    setRoot(h('div', { class: 'asc-wrap' }, card));
  }

  // ─── Dashboard (home) ───────────────────────────────────────────────────────
  // The landing surface after login: shows the cases this reviewer can pick right
  // now, a "start next case" CTA, or a reassuring empty state. It routes into the
  // EXISTING case flow (renderEvalView / the task workspace) without changing it.
  async function renderDashboardView() {
    state.view = 'home';
    stopTimer();
    updateHeaderProgress();
    renderHeader();
    // Default the flow so opening a case needs no picker: V3 + the doctor's own
    // specialty. Both stay changeable from the dashboard / header.
    state.portalChosen = true;
    state.specialtyChosen = true;
    const ver = getPortalVersion();
    const spec = ((state.user && state.user.specialty) || getPortalSpecialty()).trim().toLowerCase();
    setPortalSpecialty(spec);
    const specLabel = spec.charAt(0).toUpperCase() + spec.slice(1);

    setRoot(h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), 'Loading your dashboard…'))));

    let data = { tasks: [] };
    let stats = null;
    let scoreInfo = null;
    let queueError = null;
    // A physician whose credentials are still being checked has no real queue
    // and no earnings, and BOTH endpoints below are on the real-work surface.
    // Asking anyway would produce two 403s and render "we could not load your
    // queue", which is a bug report, not the truth. The truth is that the queue
    // does not exist for them yet, and the banner says so.
    const provisional = sessionIsProvisional();
    // The score is browse-gated on purpose: a provisional physician's "in
    // review" state renders FROM it, so it is fetched outside the real-work
    // try below and never blocks anything.
    const scorePromise = api('/score').catch(() => null);
    // One Tasks surface for every kind of work: a reviewer's queue arrives
    // here as a distinct card instead of a separate nav tab. The card is the
    // console's route now, so it renders for every reviewer, count or no
    // count, and the stats fetch is best-effort.
    const reviewPromise = sessionCan('review')
      ? api('/review/stats').catch(() => null)
      : Promise.resolve(null);
    try {
      if (provisional) throw { __provisional: true };
      const [tasksRes, statsRes] = await Promise.all([
        api('/tasks/available?portal_version=' + encodeURIComponent(ver)
          + '&specialty=' + encodeURIComponent(spec)),
        api('/me/stats').catch(() => null), // widget is non-critical; never block the queue on it
      ]);
      data = tasksRes;
      stats = statsRes;
    } catch (e) {
      if (e && e.__provisional) { data = { tasks: [] }; stats = null; }
      else {
      if (e.status === 401) return;
      // Everything else used to fall through to the empty state, so a 403, a
      // 500 and a dead backend all rendered "You are all set. No cases to grade
      // right now." — a reassuring sentence, with a checkmark, that is not true.
      // "We could not load your queue" and "your queue is empty" are different
      // facts and must never look the same to a physician.
      queueError = e;
      }
    }
    const tasks = data.tasks || [];
    scoreInfo = await scorePromise;
    const reviewStats = await reviewPromise;

    const wrap = h('div', { class: 'asc-wrap' });
    const banner = provisionalBannerEl();
    if (banner) wrap.appendChild(banner);
    wrap.appendChild(h('div', { class: 'asc-dash-head' },
      h('div', {},
        h('h2', { class: 'asc-dash-hello' }, 'Your dashboard'),
        h('p', { class: 'asc-dash-sub' }, specLabel + ' cases'))));

    const cols = h('div', { class: 'asc-dash-cols' });
    const main = h('div', { class: 'asc-dash-main' });

    if (sessionCan('review')) {
      const ready = reviewStats ? Number(reviewStats.review_ready || 0) : null;
      main.appendChild(h('button', {
        class: 'asc-dash-card asc-dash-card-review', type: 'button',
        onClick: () => setPanel('review'),
      },
        h('div', { class: 'asc-dash-card-main' },
          h('span', { class: 'asc-chip asc-chip-specialty asc-chip-pink' },
            h('span', { class: 'asc-chip-dot', 'aria-hidden': 'true' }),
            h('span', {}, 'Review work')),
          h('span', { class: 'asc-dash-card-meta' },
            ready == null ? 'Open the review console'
              : ready === 0 ? 'No pairs waiting right now'
                : ready === 1 ? '1 pair waiting for your adjudication'
                  : ready + ' pairs waiting for your adjudication')),
        h('span', { class: 'asc-dash-card-go', 'aria-hidden': 'true' }, '\u2192')));
    }

    if (queueError) {
      main.appendChild(renderDashboardError(queueError));
    } else if (provisional) {
      main.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('h3', {}, 'Your first real case is waiting on us, not on you.'),
        h('p', {},
          'Real ' + specLabel + ' cases appear here the moment your credentials '
          + 'clear. In the meantime the practice case is the real interface with '
          + 'a case we wrote for it, so nothing about it is a mock-up.'),
        h('div', { class: 'asc-dash-cta' },
          h('button', {
            class: 'asc-btn asc-btn-primary',
            onClick: () => startTutorial({ replay: true }),
          }, 'Open the practice case'),
          h('button', {
            class: 'asc-btn asc-btn-ghost',
            onClick: () => setPanel('community'),
          }, 'Meet the community')))));
    } else if (!tasks.length) {
      main.appendChild(renderDashboardEmpty(specLabel));
    } else {
      main.appendChild(h('div', { class: 'asc-dash-hero' },
        h('div', { class: 'asc-dash-hero-main' },
          h('span', { class: 'asc-dash-hero-icon', 'aria-hidden': 'true' }, '→'),
          h('div', {},
            h('div', { class: 'asc-dash-hero-title' }, 'Start next case'),
            h('div', { class: 'asc-dash-hero-sub' },
              String(tasks.length) + (tasks.length === 1 ? ' case available' : ' cases available')))),
        h('button', {
          class: 'asc-btn asc-btn-primary asc-btn-lg',
          onClick: () => { state.view = 'eval'; renderHeader(); renderEvalView(); },
        }, 'Start →')));

      const VISIBLE_CAP = 6;
      const list = h('div', { class: 'asc-dash-list' });
      tasks.slice(0, VISIBLE_CAP).forEach((t) => list.appendChild(renderTaskCard(t)));
      main.appendChild(list);
      const hiddenCount = tasks.length - VISIBLE_CAP;
      if (hiddenCount > 0) {
        const moreBtn = h('button', {
          class: 'asc-dash-more', type: 'button',
          onClick: () => {
            tasks.slice(VISIBLE_CAP).forEach((t) => list.appendChild(renderTaskCard(t)));
            moreBtn.remove();
          },
        }, 'Show ' + hiddenCount + ' more in your queue ↓');
        main.appendChild(moreBtn);
      }
    }
    cols.appendChild(main);
    const side = h('div', { class: 'asc-dash-side' });
    const scoreWidget = renderScoreWidget(scoreInfo);
    if (scoreWidget) side.appendChild(scoreWidget);
    side.appendChild(renderDashboardWidget(stats));
    cols.appendChild(side);
    wrap.appendChild(cols);
    setRoot(wrap);
  }

  // ─── The contributor-score widget (PRD-SCORE) ───────────────────────────────
  // The one number a physician watches: their initial rating out of 100, then
  // the blended score as graded cases fold in. Rendered from GET /score and
  // NEVER computed client-side; absent payload = absent widget, not a zero.
  function renderScoreWidget(info) {
    if (!info || info.score == null) return null;
    const inReview = !!info.in_review;
    const widget = h('div', { class: 'asc-dash-widget asc-score-widget' },
      h('div', { class: 'asc-dash-widget-title' }, 'Your rating'),
      h('div', { class: 'asc-score-line' },
        h('span', { class: 'asc-score-value' }, String(info.score)),
        h('span', { class: 'asc-score-band' }, info.band || '')),
      inReview
        ? h('div', { class: 'asc-score-review' },
            'Your profile is currently in review.')
        : (info.n_cases
            ? h('div', { class: 'asc-score-note' },
                'Across ' + info.n_cases + (info.n_cases === 1 ? ' graded case' : ' graded cases') + '.')
            : h('div', { class: 'asc-score-note' },
                'Your initial rating, from your profile.')),
      h('button', {
        class: 'asc-linkish asc-score-more', type: 'button',
        onClick: () => openScoreInfo(info),
      }, 'What is this?'));
    return widget;
  }

  function openScoreInfo(info) {
    if (document.getElementById('ascScoreInfo')) return;
    const bands = info.bands || { reviewer: 70, labeler: 30 };
    const overlay = h('div', { class: 'call-team-overlay is-open asc-tour-interstitial', id: 'ascScoreInfo' });
    const close = () => overlay.remove();
    overlay.addEventListener('click', close);
    const popup = h('div', { class: 'call-team-popup asc-tour-inter-pop', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'asc-tour-chrome' }, 'YOUR RATING'),
      h('div', { class: 'call-team-title' }, String(info.score) + ' out of 100'),
      h('p', { class: 'asc-help', style: 'margin:6px 0 10px' },
        info.in_review
          ? 'Your profile is currently in review. This number is your initial '
            + 'rating, built from what you told us: years of experience, board '
            + 'certification, training, and the credentials we could verify.'
          : 'Your rating starts from your profile (experience, certification, '
            + 'training) and updates after every completed, QA-graded case: the '
            + 'grade, the evidence you cite, the depth of your reasoning, and '
            + 'the care you take relative to the case\u2019s difficulty all move it.'),
      h('p', { class: 'asc-help', style: 'margin:0 0 16px' },
        'At ' + bands.reviewer + ' and above you can review other physicians\u2019 '
        + 'casework as well as label your own. Everyone can label. Nothing here '
        + 'is ever shown to other physicians.'),
      h('div', { style: 'display:flex;gap:10px;align-items:center' },
        h('button', { class: 'asc-btn asc-btn-primary', type: 'button', onClick: close }, 'Got it')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  // ─── "Your activity" tracking widget (side column) ──────────────────────────
  // Real numbers only, pulled from GET /me/stats: total cases completed, cases
  // in the last 7 days, and when the doctor last submitted one. No earnings or
  // streak data exists anywhere in this product, so this stays limited to
  // what's actually true rather than inventing filler metrics.
  function formatRelativeTime(iso) {
    if (!iso) return 'No submissions yet';
    const then = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
    const diffMs = Date.now() - then.getTime();
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'Just now';
    if (mins < 60) return mins + (mins === 1 ? ' minute ago' : ' minutes ago');
    const hours = Math.floor(mins / 60);
    if (hours < 24) return hours + (hours === 1 ? ' hour ago' : ' hours ago');
    const days = Math.floor(hours / 24);
    if (days < 7) return days + (days === 1 ? ' day ago' : ' days ago');
    return then.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function renderDashboardWidget(stats) {
    const total = stats ? stats.submissions_total : null;
    const week = stats ? stats.submissions_this_week : null;
    const lastAt = stats ? stats.last_submission_at : null;
    return h('div', { class: 'asc-dash-widget' },
      h('div', { class: 'asc-dash-widget-title' }, 'Your activity'),
      h('div', { class: 'asc-dash-widget-row' },
        h('span', { class: 'asc-dash-widget-label' }, 'Cases completed'),
        h('span', { class: 'asc-dash-widget-n' }, total == null ? '–' : String(total))),
      h('div', { class: 'asc-dash-widget-row' },
        h('span', { class: 'asc-dash-widget-label' }, 'This week'),
        h('span', { class: 'asc-dash-widget-n asc-dash-widget-n-sm' }, week == null ? '–' : String(week))),
      h('div', { class: 'asc-dash-widget-row asc-dash-widget-row-last' },
        h('span', { class: 'asc-dash-widget-label' }, 'Last submission'),
        h('span', { class: 'asc-dash-widget-meta' }, formatRelativeTime(lastAt))));
  }

  function renderTaskCard(t) {
    const spec = (t.specialty || 'general');
    const specMeta = ((state.specialties || []).find((s) => s.specialty === spec)) || {};
    const diff = t.difficulty || 'medium';
    const isReal = t.case_source === 'real_deid';
    return h('button', {
      class: 'asc-dash-card', type: 'button', onClick: () => openTaskById(t.task_id),
    },
      h('div', { class: 'asc-dash-card-main' },
        h('span', { class: 'asc-chip asc-chip-specialty asc-chip-' + (specMeta.accent || 'green') },
          h('span', { class: 'asc-chip-dot', 'aria-hidden': 'true' }),
          h('span', {}, spec.charAt(0).toUpperCase() + spec.slice(1))),
        h('span', { class: 'asc-dash-card-meta' },
          h('span', { class: 'asc-dot ' + (DIFFICULTY_DOT[diff] || 'asc-dot-orange') }), 'Difficulty: ' + diff),
        t.modality === 'multimodal' ? h('span', { class: 'asc-dash-card-meta' }, 'Multimodal case') : null,
        isReal ? h('span', { class: 'asc-dash-card-meta' }, 'Real (de-identified)') : null),
      h('span', { class: 'asc-dash-card-go' }, 'Open →'));
  }

  async function openTaskById(id) {
    setRoot(h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), 'Opening case…'))));
    try {
      const data = await api('/tasks/' + encodeURIComponent(id));
      if (!data || !data.task) { renderDashboardView(); return; }
      state.view = 'eval';
      state.portalChosen = true;
      state.specialtyChosen = true;
      state.task = data.task;
      renderHeader();
      initDraftForTask(state.task);
      if (state.draft.stage === 'compare') {
        try { await loadWithheldAnswersIfNeeded(); } catch (e) { /* compare shows a reload hint */ }
      }
      renderTaskWorkspace();
    } catch (e) {
      if (e.status !== 401) toast('Could not open that case: ' + e.message, 'error');
    }
  }

  /** The queue did not load. Say so — never a reassuring zero.
   *
   *  A 403 here is a real answer about this account (a tier not yet assigned,
   *  a role that cannot label), so the server's own sentence is shown verbatim;
   *  anything else is ours to explain. Either way the physician learns that the
   *  number in front of them is not a number. */
  function renderDashboardError(err) {
    const gated = err && err.status === 403;
    const detail = (err && (typeof err.detail === 'string' ? err.detail : err.message))
      || 'The server did not respond.';
    const retry = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' }, 'Try again');
    retry.addEventListener('click', renderDashboardView);
    return h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-inline-error' },
        h('strong', {}, gated
          ? 'This account cannot draw cases right now.'
          : 'Your case queue could not be loaded, so nothing below is a real number.'),
        h('div', { style: 'margin-top:6px' }, detail)),
      h('div', { style: 'margin-top:16px' }, retry));
  }

  function renderDashboardEmpty(specLabel) {
    return h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-empty' },
        h('div', { class: 'asc-empty-icon' }, '✓'),
        h('h3', {}, 'You are all set. No cases to grade right now.'),
        h('p', {}, 'You are verified and in the ' + specLabel + ' pool. New cases arrive in batches, '
          + 'and we will email you when the next one is ready.'),
        h('div', { style: 'margin-top:16px' },
          h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', onClick: renderDashboardView }, 'Refresh'))));
  }

  // ─── Draft + timer ─────────────────────────────────────────────────────────
  function draftKey(taskId) { return DRAFT_PREFIX + taskId; }
  function randomId() {
    let s = '';
    const a = '0123456789abcdef';
    for (let i = 0; i < 12; i++) s += a[Math.floor(Math.random() * 16)];
    return 's-' + s;
  }
  function emptyAnchor() { return { citation_text: '', source_type: '', identifier: '' }; }
  function newDraft(task) {
    return {
      submission_id: randomId(),
      task_id: task.task_id,
      // Gated-capture stage machine (Eval Flow Upgrade §1): prompt_review ->
      // independent_answer -> compare. Persisted so a refresh resumes the stage.
      stage: 'prompt_review',
      // Evaluator experience this task is graded under (Asclepius V2). Mirrors
      // the live selection during Stage 1, then pins when Stage 2 begins.
      portal_version: getPortalVersion(),
      prompt_review: { reviewed: false, verdict: null, note: '', reviewed_at: null },
      independent_answer: { text: '', evidence_anchor: emptyAnchor(), captured_at: null },
      verdict: null,
      chosen_id: null,
      rejected_id: null,
      chosen_revision: { edited: false, revised_text: null, why_better_tags: [], why_better_notes: '', evidence_anchor: emptyAnchor() },
      rejected_critique: { error_tags: [], severities: {}, why_worse: '', error_tag_anchors: {}, error_tag_reasons: {}, failure_tags: [] },
      from_scratch: { ideal_answer: '', approach_notes: '', reasoning_steps: [], evidence_anchor: emptyAnchor() },
      // Decisive action (Audit §13): physician-named verifiable outcome, skippable.
      decisive_action: { action: '', tool_name: '', rationale: '', must_precede_final_answer: true },
      reasoning_steps: [],
      // §1 substage machine (Evaluation UX Overhaul): V3/V4 only. ``substage``
      // is the persisted position INSIDE stage==='compare'; the *_done flags are
      // the explicit per-section completions (a section advances only when its
      // Save/Continue is clicked, never silently). All additive + backfilled.
      substage: null,
      refine_saved: false,
      why_better_done: false,
      citations_reviewed: false,
      critique_done: false,
      from_scratch_saved: false,
      reasoning_done: false,
      rubric_done: false,
      confidence_set: false,
      rubricCursor: 0,
      rubricSeedHash: null,
      // Rubric capture (FEAT-2): the weighted +/− criteria the doctor confirms.
      // ``rubricSeeded`` guards the one-time auto-seed from the doctor's tags.
      rubric: [],
      rubricSeeded: false,
      confidence: 'medium',
      // Model-assist suggestions (Speed Optimization §2), cached on the draft so a
      // refresh never re-bills the LLM. {fetched, skipped, suggested_weaker, ...}
      assist: null,
      elapsedSec: 0,
    };
  }
  function initDraftForTask(task) {
    let draft = null;
    try { draft = JSON.parse(localStorage.getItem(draftKey(task.task_id)) || 'null'); } catch (e) { draft = null; }
    if (!draft || draft.task_id !== task.task_id) draft = newDraft(task);
    // Backfill any newly-added fields for older drafts.
    if (!draft.chosen_revision.evidence_anchor) draft.chosen_revision.evidence_anchor = emptyAnchor();
    if (!draft.from_scratch.evidence_anchor) draft.from_scratch.evidence_anchor = emptyAnchor();
    if (!draft.rejected_critique.error_tag_anchors) draft.rejected_critique.error_tag_anchors = {};
    if (!draft.rejected_critique.error_tag_reasons) draft.rejected_critique.error_tag_reasons = {};
    if (!Array.isArray(draft.rejected_critique.failure_tags)) draft.rejected_critique.failure_tags = [];
    if (draft.assist === undefined) draft.assist = null;
    if (!draft.portal_version) draft.portal_version = getPortalVersion();
    if (!draft.prompt_review) draft.prompt_review = { reviewed: false, verdict: null, note: '', reviewed_at: null };
    if (!draft.independent_answer) draft.independent_answer = { text: '', evidence_anchor: emptyAnchor(), captured_at: null };
    if (!draft.independent_answer.evidence_anchor) draft.independent_answer.evidence_anchor = emptyAnchor();
    if (!Array.isArray(draft.rubric)) draft.rubric = [];
    if (!draft.decisive_action) draft.decisive_action = { action: '', tool_name: '', rationale: '', must_precede_final_answer: true };
    if (draft.rubricSeeded === undefined) draft.rubricSeeded = false;
    if (!draft.stage) draft.stage = 'prompt_review';
    // §1 substage machine backfill (all additive; an in-flight draft resumes at
    // the first incomplete section with its data prefilled, so nothing is lost).
    if (draft.substage === undefined) draft.substage = null;
    ['refine_saved', 'why_better_done', 'citations_reviewed', 'critique_done',
      'from_scratch_saved', 'reasoning_done', 'rubric_done', 'confidence_set']
      .forEach((k) => { if (draft[k] === undefined) draft[k] = false; });
    if (draft.rubricCursor === undefined) draft.rubricCursor = 0;
    if (draft.rubricSeedHash === undefined) draft.rubricSeedHash = null;
    // §13: per-step free-text "what's off" (step_note); backfill older steps.
    [].concat(draft.reasoning_steps || [], (draft.from_scratch || {}).reasoning_steps || [])
      .forEach((s) => { if (s && s.step_note === undefined) s.step_note = ''; });
    state.draft = draft;
    startTimer(draft.elapsedSec || 0);
  }
  function startTimer(base) {
    stopTimer();
    state.baseElapsed = base || 0;
    state.timerStart = Date.now();
    state.timerInterval = setInterval(() => {
      const el = document.getElementById('ascTimer');
      if (el) el.textContent = formatTime(getElapsed());
      // Persist periodically so a refresh resumes accurately.
      if (getElapsed() % 5 === 0) saveDraft();
    }, 1000);
  }
  function stopTimer() {
    if (state.timerInterval) { clearInterval(state.timerInterval); state.timerInterval = null; }
  }
  function getElapsed() {
    return Math.floor(state.baseElapsed + (Date.now() - state.timerStart) / 1000);
  }
  function formatTime(sec) {
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + String(s).padStart(2, '0');
  }
  function saveDraft() {
    if (!state.draft) return;
    state.draft.elapsedSec = getElapsed();
    try { localStorage.setItem(draftKey(state.draft.task_id), JSON.stringify(state.draft)); } catch (e) { /* ignore quota */ }
  }
  function clearDraft(taskId) {
    try { localStorage.removeItem(draftKey(taskId)); } catch (e) { /* ignore */ }
  }

  // ─── Portal version (V1 classic · V2 assisted · V3 seamless) ────────────────
  const PORTAL_VERSIONS = ['v1', 'v2', 'v3', 'v4'];
  function getPortalVersion() {
    let v = null;
    try { v = localStorage.getItem(PORTAL_VERSION_KEY); } catch (e) { v = null; }
    return PORTAL_VERSIONS.indexOf(v) !== -1 ? v : DEFAULT_PORTAL_VERSION;
  }
  function setPortalVersion(v) {
    v = PORTAL_VERSIONS.indexOf(v) !== -1 ? v : DEFAULT_PORTAL_VERSION;
    try { localStorage.setItem(PORTAL_VERSION_KEY, v); } catch (e) { /* ignore quota */ }
  }

  // ─── Specialty selection (Specialty Hyper-Personalization PRD §1) ───────────
  function getPortalSpecialty() {
    let s = null;
    try { s = localStorage.getItem(PORTAL_SPECIALTY_KEY); } catch (e) { s = null; }
    return (s || DEFAULT_PORTAL_SPECIALTY).trim().toLowerCase();
  }
  function setPortalSpecialty(s) {
    try { localStorage.setItem(PORTAL_SPECIALTY_KEY, String(s || '').trim().toLowerCase()); } catch (e) { /* ignore quota */ }
  }
  // The current task's specialty (drives the case panel's data-driven tabs).
  function caseSpecialty() {
    const c = multimodalCase();
    return ((c && c.specialty) || (state.task && state.task.specialty) || 'nephrology').toLowerCase();
  }

  // Per-specialty case-panel layout: ONE code path, config not a render fork
  // (PRD §6). Each entry is an ordered list of tab specs; a tab appears only when
  // its data exists. ``study`` groups the case's ``studies`` by modality; ``strip``
  // renders a scannable ECG findings block; ``timeline`` renders imaging as a
  // baseline→on-treatment sequence; ``ngs`` renders molecular variants as a VAF table.
  const SPECIALTY_UI = {
    nephrology: [
      { kind: 'overview' },
      { kind: 'labs', label: 'Labs (trend)' },
      { kind: 'study', key: 'studies', label: 'Studies', modalities: ['pathology', 'ct', 'mri', 'pet', 'echo', 'other'] },
      { kind: 'notes' }, { kind: 'meds' }, { kind: 'vitals' },
    ],
    cardiology: [
      { kind: 'overview' },
      { kind: 'study', key: 'ecg', label: 'ECG', modalities: ['ecg'], strip: true },
      { kind: 'study', key: 'echoimg', label: 'Echo/Imaging', modalities: ['echo', 'cath', 'ct', 'mri', 'pet'] },
      { kind: 'labs', label: 'Labs' },
      { kind: 'notes' }, { kind: 'meds' }, { kind: 'vitals' },
    ],
    oncology: [
      { kind: 'overview' },
      { kind: 'study', key: 'pathology', label: 'Pathology', modalities: ['pathology'] },
      { kind: 'study', key: 'imaging', label: 'Imaging', modalities: ['ct', 'mri', 'pet'], timeline: true },
      { kind: 'study', key: 'molecular', label: 'Molecular', modalities: ['molecular'], ngs: true },
      { kind: 'labs', label: 'Labs' },
      { kind: 'notes' }, { kind: 'meds' }, { kind: 'vitals' },
    ],
  };
  const DEFAULT_SPECIALTY_UI = SPECIALTY_UI.nephrology;
  // Modality → short chip label for the modality chips in a study tab.
  const MODALITY_LABEL = {
    ecg: 'ECG', echo: 'Echo', cath: 'Cath', ct: 'CT', mri: 'MRI', pet: 'PET',
    pathology: 'Pathology', molecular: 'Molecular', other: 'Study',
  };
  // The version a task is graded under: pinned onto the draft when the doctor
  // leaves Stage 1 (so a switch mid-task can't produce a half-assisted record);
  // until then it mirrors the live selection.
  function draftVersion() {
    return (state.draft && state.draft.portal_version) || getPortalVersion();
  }
  function isV2() { return draftVersion() === 'v2'; }
  // The SEAMLESS-flow gate: V4 (real cases) is the V3 flow over real data -
  // every V3 UX behavior (instinct one-liner, hidden-until-verdict suggestions,
  // one-click citations, bright diff, big editor) applies identically to v4.
  function isV3() { return draftVersion() === 'v3' || draftVersion() === 'v4'; }
  // Assisted flows (V2 + V3) share model pre-labeling, the A/B diff, dictation,
  // and value-aware routing. V1 (classic) is the only non-assisted flow. Most
  // former ``isV2()`` gates are really "is assisted"; V3-specific behavior
  // (the instinct one-liner, hide-suggestions-until-verdict) uses ``isV3()``.
  function isAssisted() { return draftVersion() !== 'v1'; }

  // ─── Multimodal cases (Synthetic Multimodal Cases PRD §5) ───────────────────
  // The current task's PUBLIC structured case (answer key already stripped
  // server-side), or null for a plain text task.
  function multimodalCase() {
    const t = state.task;
    if (!t || (t.modality || 'text') !== 'multimodal') return null;
    return (t.case && typeof t.case === 'object') ? t.case : null;
  }

  // The clinical question, split out of a rendered multimodal prompt so the
  // prompt card shows the question and the case panel shows the data (no dupe).
  // Mirrors ``cases.render_case_prompt`` ("CLINICAL QUESTION:\n{q}\n\nCLINICAL
  // CASE…"); falls back to the whole prompt if the markers aren't present.
  function caseQuestion(prompt) {
    const s = String(prompt || '');
    const idx = s.indexOf('\n\nCLINICAL CASE');
    const head = idx !== -1 ? s.slice(0, idx) : s;
    return head.replace(/^CLINICAL QUESTION:\s*/i, '').trim() || s.trim();
  }

  // Lab out-of-range flag → severity class for cell highlighting.
  function labFlagClass(flag) {
    const f = String(flag || '').toUpperCase();
    if (f === 'LL' || f === 'HH') return 'asc-lab-crit';
    if (f === 'L' || f === 'H') return 'asc-lab-warn';
    return '';
  }
  function fmtOffset(off) {
    const n = parseInt(off, 10) || 0;
    if (n === 0) return 'day 0';
    return 'day ' + (n > 0 ? '+' : '') + n;
  }

  // A trend table across all lab panels: one row per analyte, one column per
  // distinct collection offset (oldest → newest), so a clinician reads the
  // trajectory (e.g. a falling sodium) at a glance. Cells are flag-highlighted.
  function renderLabsTrend(panels) {
    const ps = (panels || []).slice().sort(
      (a, b) => (parseInt(a.collected_offset_days, 10) || 0) - (parseInt(b.collected_offset_days, 10) || 0));
    if (!ps.length) return null;
    const offsets = [];
    ps.forEach((p) => { const o = parseInt(p.collected_offset_days, 10) || 0; if (offsets.indexOf(o) === -1) offsets.push(o); });
    // analyte order = first-seen; carry unit + ref range + which panel.
    const order = [];
    const meta = {};
    const cell = {}; // analyte -> offset -> {value, flag}
    ps.forEach((p) => {
      const off = parseInt(p.collected_offset_days, 10) || 0;
      (p.results || []).forEach((r) => {
        const a = String(r.analyte || '');
        if (!a) return;
        if (order.indexOf(a) === -1) { order.push(a); meta[a] = { unit: r.unit || '', ref: refRange(r), panel: p.panel || '' }; }
        cell[a] = cell[a] || {};
        cell[a][off] = { value: r.value, flag: r.flag };
      });
    });
    const head = h('tr', {},
      h('th', {}, 'Analyte'),
      h('th', {}, 'Ref'),
      ...offsets.map((o) => h('th', { class: 'asc-lab-num' }, fmtOffset(o))));
    const rows = order.map((a) => h('tr', {},
      h('td', { class: 'asc-lab-analyte' }, a + (meta[a].unit ? ' (' + meta[a].unit + ')' : '')),
      h('td', { class: 'asc-lab-ref' }, meta[a].ref),
      ...offsets.map((o) => {
        const c = (cell[a] || {})[o];
        if (!c || c.value == null || c.value === '') return h('td', { class: 'asc-lab-num' }, '·');
        return h('td', { class: 'asc-lab-num ' + labFlagClass(c.flag) },
          String(c.value) + (c.flag ? ' ' + String(c.flag).toUpperCase() : ''));
      })));
    return h('div', { class: 'asc-lab-scroll' },
      h('table', { class: 'asc-lab-table' }, h('thead', {}, head), h('tbody', {}, ...rows)));
  }
  function refRange(r) {
    const lo = (r.ref_low === null || r.ref_low === undefined) ? '' : r.ref_low;
    const hi = (r.ref_high === null || r.ref_high === undefined) ? '' : r.ref_high;
    if (lo === '' && hi === '') return 'n/a';
    return lo + '–' + hi;
  }

  // A compact measurements/variants table (reuses the lab flag classes) for a
  // study's numeric findings: EF %, valve gradient, SUVmax, molecular VAF, etc.
  function renderMeasurements(measurements, valueHead) {
    const ms = (measurements || []).filter((m) => m && m.analyte);
    if (!ms.length) return null;
    const head = h('tr', {}, h('th', {}, valueHead || 'Measure'), h('th', { class: 'asc-lab-num' }, 'Value'),
      h('th', {}, 'Ref'));
    const rows = ms.map((m) => h('tr', {},
      h('td', { class: 'asc-lab-analyte' }, String(m.analyte) + (m.unit ? ' (' + m.unit + ')' : '')),
      h('td', { class: 'asc-lab-num ' + labFlagClass(m.flag) }, String(m.value == null ? '·' : m.value) + (m.flag ? ' ' + String(m.flag).toUpperCase() : '')),
      h('td', { class: 'asc-lab-ref' }, refRange(m))));
    return h('div', { class: 'asc-lab-scroll' },
      h('table', { class: 'asc-lab-table' }, h('thead', {}, head), h('tbody', {}, ...rows)));
  }

  // Fetch a cleaned image asset over the AUTHENTICATED endpoint (Bearer token, an
  // <img src> can't carry it, so we fetch a blob and use an object URL). Blinding
  // holds: the bytes carry no provider/model/partner identity (V4 Image PRD §6).
  async function fetchAssetBlobUrl(assetId) {
    const res = await fetch(API_BASE + '/assets/' + encodeURIComponent(assetId), {
      headers: state.token ? { Authorization: 'Bearer ' + state.token } : {},
    });
    if (!res.ok) throw new Error('asset ' + res.status);
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  }

  // The V4 image viewer (V4 Image Embedding PRD §6): the image renders ABOVE its
  // structured findings, with zoom (scroll / ＋－), pan (drag), fit/reset, and a
  // full-screen toggle, keyboard-operable (＋/－/0, arrows, Esc). One component,
  // design-tokens only, with a real load-failure state (never a broken-image icon).
  function renderStudyImage(study) {
    if (!study || !study.asset || !study.asset.asset_id) return null;
    const asset = study.asset;
    const pageCount = parseInt(asset.page_count, 10) || 1;
    const shownPage = parseInt(asset.page, 10) || 1;
    const view = { scale: 1, x: 0, y: 0 };
    const wrap = h('div', { class: 'asc-img-viewer' });
    const stage = h('div', { class: 'asc-img-stage', tabindex: '0', role: 'img',
      'aria-label': (study.label || study.modality || 'clinical') + ' image' });
    const img = h('img', { class: 'asc-img', alt: (study.label || study.modality || 'clinical image'), draggable: 'false' });
    const skeleton = h('div', { class: 'asc-img-skeleton' }, h('div', { class: 'loading-spinner' }));
    stage.appendChild(skeleton);
    // Track the blob URL so it is revoked (no leaked decoded-image memory over a
    // long grading session). Cleaned up on load, on error/reload, and on teardown.
    let objUrl = null;
    function revoke() { if (objUrl) { try { URL.revokeObjectURL(objUrl); } catch (e) { /* ignore */ } objUrl = null; } }

    function apply() {
      img.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.scale + ')';
    }
    function zoom(delta) {
      const next = Math.min(8, Math.max(1, view.scale + delta));
      view.scale = next;
      if (next === 1) { view.x = 0; view.y = 0; }
      apply();
    }
    function reset() { view.scale = 1; view.x = 0; view.y = 0; apply(); }

    // Zoom on scroll; pan on drag. The pan listeners live on `window` ONLY for the
    // duration of a drag (added on mousedown, removed on mouseup) so they never
    // accumulate across cases/tabs; the stage-scoped listeners are GC'd with the node.
    stage.addEventListener('wheel', (e) => { e.preventDefault(); zoom(e.deltaY < 0 ? 0.25 : -0.25); }, { passive: false });
    let drag = null;
    function onMove(e) { if (!drag) return; view.x = e.clientX - drag.x; view.y = e.clientY - drag.y; apply(); }
    function onUp() { drag = null; stage.classList.remove('asc-img-grabbing');
      window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); }
    stage.addEventListener('mousedown', (e) => {
      drag = { x: e.clientX - view.x, y: e.clientY - view.y };
      stage.classList.add('asc-img-grabbing');
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
    });
    // Keyboard: +/- zoom, 0 reset, arrows pan, Esc exit full-screen.
    stage.addEventListener('keydown', (e) => {
      const step = 40;
      if (e.key === '+' || e.key === '=') { zoom(0.25); }
      else if (e.key === '-' || e.key === '_') { zoom(-0.25); }
      else if (e.key === '0') { reset(); }
      else if (e.key === 'ArrowLeft') { view.x += step; apply(); }
      else if (e.key === 'ArrowRight') { view.x -= step; apply(); }
      else if (e.key === 'ArrowUp') { view.y += step; apply(); }
      else if (e.key === 'ArrowDown') { view.y -= step; apply(); }
      else if (e.key === 'Escape') { if (wrap.classList.contains('asc-img-full')) toggleFull(); return; }
      else { return; }
      e.preventDefault();
    });
    function toggleFull() { wrap.classList.toggle('asc-img-full'); reset(); stage.focus(); }

    const toolbar = h('div', { class: 'asc-img-toolbar' },
      h('button', { class: 'asc-img-btn', type: 'button', title: 'Zoom out (−)', onClick: () => zoom(-0.25) }, '−'),
      h('button', { class: 'asc-img-btn', type: 'button', title: 'Zoom in (+)', onClick: () => zoom(0.25) }, '＋'),
      h('button', { class: 'asc-img-btn', type: 'button', title: 'Reset view (0)', onClick: reset }, 'Reset'),
      h('button', { class: 'asc-img-btn', type: 'button', title: 'Full screen', onClick: toggleFull }, '⤢'));
    // Multi-page PDF: the ingested asset is a SINGLE rendered page (page N of the
    // source), so we show an honest static indicator, never non-functional nav
    // buttons that would let a clinician believe they are viewing a page that isn't
    // loaded. (Per-page fetch is a follow-up; the endpoint serves one page today.)
    if (pageCount > 1) {
      toolbar.appendChild(h('span', { class: 'asc-img-page' }, 'Page ' + shownPage + ' of ' + pageCount));
    }

    stage.appendChild(img);
    wrap.appendChild(toolbar);
    wrap.appendChild(stage);

    // Load the bytes. On failure show a real error state with a reload; the doctor
    // must NEVER grade a case whose image didn't render (PRD §6).
    fetchAssetBlobUrl(asset.asset_id).then((url) => {
      objUrl = url;
      // Revoke once decoded. The browser keeps the rendered image; frees the blob.
      img.onload = () => { if (skeleton.parentNode) skeleton.parentNode.removeChild(skeleton); apply(); revoke(); };
      img.onerror = () => { revoke(); showError(); };
      img.src = url;
    }).catch(() => showError());
    function showError() {
      revoke();
      onUp();  // ensure any in-flight drag listeners are removed
      clear(stage);
      stage.appendChild(h('div', { class: 'asc-img-error' },
        h('div', {}, 'Could not load the image.'),
        h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
          onClick: () => { const p = wrap.parentNode; if (p) { const fresh = renderStudyImage(study); p.replaceChild(fresh, wrap); } } }, 'Reload')));
    }
    return wrap;
  }

  // One study rendered inside its tab: the image first (if any), then the modality
  // chip, the structured findings report (as a scannable "rhythm strip" block for an
  // ECG), the numeric measurements table, and the impression. This is the grounding
  // anchor the model is supposed to read, surfaced prominently (PRD §3/§6).
  function renderStudyCard(study, opts) {
    opts = opts || {};
    const modality = String(study.modality || 'study').toLowerCase();
    const chipLabel = MODALITY_LABEL[modality] || modality.toUpperCase();
    const findings = (study.findings || '').trim();
    const isNgs = opts.ngs || modality === 'molecular';
    return h('div', { class: 'asc-study' },
      h('div', { class: 'asc-study-head' },
        h('span', { class: 'asc-chip asc-chip-modality' },
          h('span', { class: 'asc-chip-dot', 'aria-hidden': 'true' }),
          h('span', {}, chipLabel)),
        study.label ? h('span', { class: 'asc-study-label' }, study.label) : null),
      renderStudyImage(study),
      findings
        ? (opts.strip
            ? h('div', { class: 'asc-ecg-strip' }, h('div', { class: 'asc-ecg-strip-text' }, findings))
            : h('div', { class: 'asc-study-findings' }, findings))
        : null,
      renderMeasurements(study.measurements, isNgs ? 'Variant' : 'Measure'),
      study.impression ? h('div', { class: 'asc-study-impression' }, h('strong', {}, 'Impression: '), study.impression) : null);
  }

  // Build a study tab body from the case's ``studies`` filtered to ``modalities``.
  // ``timeline`` lays the studies out as a baseline→on-treatment sequence (the
  // temporal judgment pseudoprogression turns on, PRD §6).
  function renderStudyTab(studies, spec) {
    const mods = spec.modalities;
    const items = (studies || []).filter((s) => s && (!mods || mods.indexOf(String(s.modality || '').toLowerCase()) !== -1));
    if (!items.length) return null;
    if (spec.timeline && items.length) {
      return h('div', { class: 'asc-case-body' },
        h('div', { class: 'asc-timeline' }, ...items.map((s, i) => h('div', { class: 'asc-timeline-step' },
          h('div', { class: 'asc-timeline-marker', 'aria-hidden': 'true' }),
          renderStudyCard(s, spec)))));
    }
    return h('div', { class: 'asc-case-body' }, ...items.map((s) => renderStudyCard(s, spec)));
  }

  // The tabbed case panel. Tabs are built only for sections that carry data, driven
  // by the per-specialty ``SPECIALTY_UI`` config (one code path, never a render fork
  // per specialty, PRD §6). State (active tab) lives on ``state._caseTab`` keyed by
  // task so it survives re-renders within a task.
  function renderCasePanel() {
    const c = multimodalCase();
    if (!c) return null;
    const spec = caseSpecialty();
    const ui = SPECIALTY_UI[spec] || DEFAULT_SPECIALTY_UI;
    const demo = c.demographics || {};
    const who = [demo.sex, demo.age_band ? ('age ' + demo.age_band) : null].filter(Boolean).join(', ');
    const meds = c.medications || [];
    const vitals = c.vitals || {};
    const vkeys = Object.keys(vitals).filter((k) => vitals[k] !== null && vitals[k] !== undefined && vitals[k] !== '');
    const studies = c.studies || [];

    const tabs = [];
    ui.forEach((t) => {
      if (t.kind === 'overview') {
        tabs.push({ key: 'overview', label: 'Patient', body: h('div', { class: 'asc-case-body' },
          h('div', { class: 'asc-case-patient' }, who ? ('Patient: ' + who) : 'Patient (de-identified)'),
          (c.problem_list && c.problem_list.length)
            ? h('div', { class: 'asc-case-sub' }, 'Active problems: ' + c.problem_list.map((p) => p.condition + (p.since ? ' (since ' + p.since + ')' : '')).join('; '))
            : null,
          h('div', { class: 'asc-case-note-meta' }, 'De-identified · relative dates · structured studies')) });
      } else if (t.kind === 'labs' && c.lab_panels && c.lab_panels.length) {
        tabs.push({ key: 'labs', label: t.label || 'Labs', body: h('div', { class: 'asc-case-body' }, renderLabsTrend(c.lab_panels)) });
      } else if (t.kind === 'study') {
        const body = renderStudyTab(studies, t);
        if (body) tabs.push({ key: t.key, label: t.label, body: body });
      } else if (t.kind === 'notes' && c.notes && c.notes.length) {
        tabs.push({ key: 'notes', label: 'EHR' + (c.notes.length > 1 ? ' (' + c.notes.length + ')' : ''),
          body: h('div', { class: 'asc-case-body' }, ...c.notes.map((n) => h('div', { class: 'asc-case-note' },
            h('div', { class: 'asc-case-note-meta' }, '[' + (n.note_type || 'Note') + ' · ' + (n.author_role || 'clinician') + ']'),
            h('div', { class: 'asc-case-note-text' }, (n.text || '').trim())))) });
      } else if (t.kind === 'meds' && meds.length) {
        tabs.push({ key: 'meds', label: 'Meds', body: h('div', { class: 'asc-case-body' },
          h('ul', { class: 'asc-case-list' }, ...meds.map((m) => h('li', {},
            [m.drug, m.dose, m.route, m.freq].filter(Boolean).join(' '))))) });
      } else if (t.kind === 'vitals' && vkeys.length) {
        tabs.push({ key: 'vitals', label: 'Vitals', body: h('div', { class: 'asc-case-body' },
          h('div', { class: 'asc-case-vitals' }, ...vkeys.map((k) => h('span', { class: 'asc-vital' },
            h('span', { class: 'asc-vital-k' }, k), ' ', h('span', { class: 'asc-vital-v' }, String(vitals[k])))))) });
      }
    });
    if (!tabs.length) return null;

    const tid = state.task && state.task.task_id;
    if (!state._caseTab || state._caseTabTask !== tid) { state._caseTab = tabs[0].key; state._caseTabTask = tid; }
    if (!tabs.some((t) => t.key === state._caseTab)) state._caseTab = tabs[0].key;

    const bodyHost = h('div', { class: 'asc-case-host' });
    const tabRow = h('div', { class: 'asc-case-tabs', role: 'tablist', dataset: { tour: 'case-tabs' } });
    function paint() {
      clear(bodyHost);
      const active = tabs.find((t) => t.key === state._caseTab) || tabs[0];
      bodyHost.appendChild(active.body);
      Array.prototype.forEach.call(tabRow.children, (btn) => {
        btn.classList.toggle('asc-case-tab-active', btn.getAttribute('data-tab') === state._caseTab);
      });
    }
    tabs.forEach((t) => {
      const btn = h('button', { class: 'asc-case-tab', type: 'button', role: 'tab', 'data-tab': t.key,
        onClick: () => { state._caseTab = t.key; paint(); } }, t.label);
      tabRow.appendChild(btn);
    });

    // Specialty chip (deterministic color: nephrology green, cardiology orange,
    // oncology pink; PRD §6). Accent comes from the cached /specialties listing so
    // a new specialty needs no frontend change.
    const specMeta = ((state.specialties || []).find((s) => s.specialty === spec)) || {};
    const specChip = h('span', { class: 'asc-chip asc-chip-specialty asc-chip-' + (specMeta.accent || 'green') },
      h('span', { class: 'asc-chip-dot', 'aria-hidden': 'true' }),
      h('span', {}, spec.charAt(0).toUpperCase() + spec.slice(1)));
    const panel = h('div', { class: 'asc-card asc-case-card' },
      h('div', { class: 'asc-case-head' },
        specChip,
        h('span', { class: 'asc-badge asc-badge-accent' }, 'Multimodal case'),
        h('span', { class: 'asc-case-source' }, (c.case_source === 'real_deid' ? 'Real (de-identified)' : 'Synthetic'))),
      tabRow, bodyHost);
    paint();
    return panel;
  }

  // ─── Grounding (mirror of backend validation.grounding_status) ──────────────
  function isValidAnchor(a) {
    if (!a) return false;
    if (!(a.citation_text || '').trim()) return false;
    const types = (state.taxonomy && state.taxonomy.evidence_source_types) || [];
    if (types.indexOf(a.source_type) === -1) return false;
    if (!(a.identifier || '').trim()) return false;
    return true;
  }
  function rationaleAnchor() {
    const d = state.draft;
    if (d.verdict === 'A_better' || d.verdict === 'B_better') return d.chosen_revision.evidence_anchor;
    if (d.verdict === 'both_inadequate') return d.from_scratch.evidence_anchor;
    return null;
  }
  function activeSteps() {
    const d = state.draft;
    return d.verdict === 'both_inadequate' ? d.from_scratch.reasoning_steps : d.reasoning_steps;
  }
  function groundingSatisfied() {
    const task = state.task;
    if ((task.grounding_mode || 'optional') !== 'required') return { ok: true, reasons: [] };
    const reasons = [];
    if (!isValidAnchor(rationaleAnchor())) reasons.push('missing_rationale_anchor');
    const steps = activeSteps();
    const isReasoningTask = !!task.capture_reasoning || steps.length > 0;
    if (isReasoningTask && steps.length) {
      for (const s of steps) { if (!isValidAnchor(s.evidence_anchor)) { reasons.push('missing_step_anchor'); break; } }
    }
    return { ok: reasons.length === 0, reasons };
  }
  // Edit-to-Correct gating: on a capture_reasoning task every split step must be
  // resolved (confirmed / corrected / added) before Submit enables, and every
  // corrected step needs a reason. Silence ≠ endorsement.
  function stepsReview() {
    if (!state.task.capture_reasoning) return { ok: true, reasons: [] };
    const reasons = [];
    for (const s of activeSteps()) {
      if (!(s.text || '').trim()) continue;
      if (!(s.confirmed || s.corrected || s.added)) reasons.push('pending_step');
      // §13 (V3/V4): a corrected step's "why" is the free-text step_note; the
      // server derives the error-tag classification from it. V1/V2 keep the
      // one-tap correction_reason picker.
      if (s.corrected && !(s.correction_reason || '').trim()
          && !(isV3() && (s.step_note || '').trim())) {
        reasons.push('missing_correction_reason');
      }
    }
    return { ok: reasons.length === 0, reasons };
  }

  // ─── Tiered rubric (Two-Model PRD Workstream B, V3/V4) ─────────────────────
  // Criticality tier from |points|: critical 8-10, important 4-7, helpful 1-3.
  function tierForPoints(points) {
    const mag = Math.abs(Number(points) || 0);
    if (mag >= 8) return 'critical';
    if (mag >= 4) return 'important';
    return 'helpful';
  }
  function hasCriticalNegative(rubric) {
    return (rubric || []).some((c) => (Number(c.points) || 0) < 0
      && tierForPoints(c.points) === 'critical' && (c.text || '').trim());
  }
  // On V3/V4, a captured rubric must name at least one CRITICAL negative, the one
  // thing a correct answer must never do. An empty rubric is allowed (optional); the
  // gate fires only once criteria exist. Scoped to isV3() (v3+v4), so V1/V2 are
  // unaffected.
  function rubricGate() {
    if (!isV3()) return { ok: true };
    const rubric = (state.draft.rubric || []).filter((c) => (c.text || '').trim());
    if (!rubric.length) return { ok: true };
    if (hasCriticalNegative(rubric)) return { ok: true };
    // §12: name the control the physician actually taps: "must never", set
    // Critical.
    return { ok: false, msg: 'mark one “must never” criterion as Critical (−8 to −10) to continue' };
  }

  // ─── Rubric Rigor (§C) + Model-Failure Taxonomy (§D): V3/V4 only ──────────
  // FIX-1 concreteness. PORTED VERBATIM from backend asclepius/rubric.py so the UI
  // NEVER disagrees with the server about "specific" (a mismatch would mislabel a
  // rubric premium/standard). Kept in lock-step by test/rubric_ui_parity.
  const _VAGUE_MARKERS = [
    'better than', 'safer than', 'clearer than', 'more accurate than', 'plausible alternative',
    'appropriately', 'as appropriate', 'as needed', 'as indicated', 'adequately', 'properly',
    'correctly manage', 'manages appropriately', 'reasonable', 'good clinical', 'high quality',
  ];
  const _UNIT_RE = /\b(mg|mcg|g|mmol|meq|ml|l|mmhg|mg\/dl|mmol\/l|meq\/l|g\/dl|ml\/min|ml\/kg|units?|%|mosm|mosm\/kg)\b/;
  const _CLINICAL_RE = /\b(calcium|potassium|sodium|magnesium|phosphate|bicarbonate|chloride|insulin|dextrose|dialysate|dialysis|hemodialysis|thiazide|hydrochlorothiazide|loop diuretic|furosemide|finerenone|spironolactone|amiloride|eplerenone|kayexalate|patiromer|osmolality|osmolarity|creatinine|egfr|fena|feurea|urine|urea|guideline|kdigo|contraindicat|acei|arb|nsaid|sglt2|hyperkalemia|hypokalemia|hyponatremia|hypernatremia|acidosis|alkalosis|volume|ddavp|hypertonic saline|normal saline|fluid restriction|peaked t|ecg|ekg)\b/;
  function isSpecificText(text) {
    const t = String(text || '').trim().toLowerCase();
    if (!t) return false;
    if (_VAGUE_MARKERS.some((m) => t.indexOf(m) !== -1)) return false;
    if (/\d/.test(t)) return true;
    if (_UNIT_RE.test(t)) return true;
    if (_CLINICAL_RE.test(t)) return true;
    return false;
  }

  // FIX-4 completeness/premium: mirrors backend rubric.rubric_completeness exactly.
  const _PREMIUM_MIN_CRITERIA = 5;
  const _PREMIUM_MIN_AXES = 3;
  // FIX-7 (axis-coverage nudge): the three axes a defensible grader almost always
  // touches. Mirrors backend constants.RUBRIC_CORE_AXES exactly. ADVISORY only:
  // never affects premium/missing.
  const _RUBRIC_CORE_AXES = ['safety', 'accuracy', 'reasoning'];
  function rubricCompleteness(criteria) {
    const crit = (criteria || []).filter((c) => (c.text || '').trim());
    const n = crit.length;
    const nPos = crit.filter((c) => (Number(c.points) || 0) > 0).length;
    const nNeg = crit.filter((c) => (Number(c.points) || 0) < 0).length;
    // §11: a criterion can score on several axes, so coverage counts EVERY one.
    // Reading `c.axis` alone would under-count the moment multi-select ships.
    // Falls back to the legacy single value for stored/V2-shaped records.
    const axes = new Set(crit.reduce((acc, c) => acc.concat(
      (Array.isArray(c.axes) && c.axes.length) ? c.axes : (c.axis ? [c.axis] : [])), []).filter(Boolean));
    const hasCritNeg = hasCriticalNegative(crit);
    const keyCriteria = crit.filter((c) => { const tr = tierForPoints(c.points); return tr === 'critical' || tr === 'important'; });
    const allKeySpecific = keyCriteria.length ? keyCriteria.every((c) => isSpecificText(c.text)) : false;
    const missing = [];
    if (n < _PREMIUM_MIN_CRITERIA) missing.push('add ' + (_PREMIUM_MIN_CRITERIA - n) + ' more criteria (≥' + _PREMIUM_MIN_CRITERIA + ' total)');
    if (nPos < 1) missing.push('add ≥1 positive criterion');
    if (nNeg < 1) missing.push('add ≥1 negative criterion');
    if (!hasCritNeg) missing.push('name ≥1 CRITICAL negative (−8 to −10)');
    if (axes.size < _PREMIUM_MIN_AXES) missing.push('cover ≥' + _PREMIUM_MIN_AXES + ' axes (have ' + axes.size + ')');
    if (!allKeySpecific) missing.push('make every critical/important criterion specific (name the fact/drug/dose/threshold)');
    // FIX-7: advisory core-axis coverage, kept OUT of `missing` so it never gates premium.
    const coreAxesMissing = _RUBRIC_CORE_AXES.filter((a) => !axes.has(a));
    const nudges = [];
    if (coreAxesMissing.length) {
      nudges.push('consider covering ' + coreAxesMissing.join(', ')
        + ' (a grader is stronger when it scores safety, accuracy, and reasoning)');
    }
    return { premium: missing.length === 0, tier: missing.length === 0 ? 'premium' : 'standard',
      n_criteria: n, n_axes: axes.size, missing,
      core_axes: _RUBRIC_CORE_AXES.slice(), core_axes_missing: coreAxesMissing,
      covers_core_axes: coreAxesMissing.length === 0, nudges };
  }

  // Is this task a REAL-MODEL (baseline) A/B pair? (needed for the §D failure-tag gate)
  function isBaselinePair() {
    return ((state.task && state.task.candidate_answers) || []).some((c) => c && c.source === 'baseline');
  }

  // §D-2: on V3/V4, when the rubric names a critical negative AND this is a real-model
  // pair, require ≥1 physician failure tag on the rejected answer. Mirrors the backend
  // submit gate (400 failure_tag_required) exactly, so submit never surprises the doctor.
  function failureTagGate() {
    if (!isV3()) return { ok: true };
    const v = state.draft.verdict;
    if (v !== 'A_better' && v !== 'B_better') return { ok: true };
    const rubric = (state.draft.rubric || []).filter((c) => (c.text || '').trim());
    if (!rubric.length || !hasCriticalNegative(rubric)) return { ok: true };
    if (!isBaselinePair()) return { ok: true };
    const tags = ((state.draft.rejected_critique || {}).failure_tags) || [];
    if (tags.length) return { ok: true };
    return { ok: false, msg: 'tag ≥1 failure mode on the rejected answer to continue' };
  }

  // ─── §1 Substage machine (Evaluation UX Overhaul, V3/V4 only) ──────────────
  // One decision per screen: inside stage==='compare', sections mount one at a
  // time, each only when the previous one is explicitly completed. V1/V2 never
  // enter this machine (renderRationale keeps their render-everything path).
  const SUBSTAGE_META = {
    compare:           { chrome: 'PICK THE STRONGER ANSWER', title: 'Compare the answers' },
    refine:            { chrome: 'REFINE THE ANSWER', title: 'Refine the winning answer' },
    why_better:        { chrome: 'WHY IT’S BETTER', title: 'Why is this answer better?' },
    citations:         { chrome: 'CITE YOUR SOURCES', title: 'Ground it in a source' },
    critique_rejected: { chrome: 'WHAT’S WRONG WITH THE OTHER ANSWER', title: 'Critique the rejected answer' },
    from_scratch:      { chrome: 'WRITE THE IDEAL ANSWER', title: 'Compose the ideal answer' },
    reasoning:         { chrome: 'CHECK THE REASONING', title: 'Check the reasoning' },
    rubric:            { chrome: 'BUILD THE SCORING GUIDE', title: 'Build the scoring guide' },
    confidence:        { chrome: 'CONFIDENCE & SUBMIT', title: 'How confident are you with your answer?' },
  };

  // The ordered substage list for the current draft. Depends on the verdict
  // (both_inadequate skips the winner-refinement path) and on capture_reasoning.
  function compareSubstages() {
    const d = state.draft;
    if (!d || !d.verdict) {
      // No verdict yet: assume the (far more common) A/B path so the §16
      // progress total is stable; picking a verdict must never make the bar
      // jump backwards because the denominator grew.
      const dflt = ['compare', 'refine', 'why_better', 'citations', 'critique_rejected'];
      if (state.task && state.task.capture_reasoning) dflt.push('reasoning');
      dflt.push('rubric', 'confidence');
      return dflt;
    }
    if (d.verdict === 'both_inadequate') {
      return ['compare', 'from_scratch', 'reasoning', 'rubric', 'confidence'];
    }
    const list = ['compare', 'refine', 'why_better', 'citations', 'critique_rejected'];
    if (state.task.capture_reasoning) list.push('reasoning');
    list.push('rubric', 'confidence');
    return list;
  }

  function whyBetterConditionsMet() {
    const rev = state.draft.chosen_revision;
    return !!(rev.why_better_notes || '').trim() && (rev.why_better_tags || []).length >= 1;
  }

  // §12 completion: a required "why is it worse?" line, ≥1 error tag, and each
  // selected tag carrying its severity (captured via the tag-tap popover). On a
  // real-model pair the §D failure-mode tags are also required HERE, so submit
  // can never surprise the doctor after this section has collapsed.
  function critiqueConditionsMet() {
    const crit = state.draft.rejected_critique;
    if (!(crit.why_worse || '').trim()) return false;
    if (!(crit.error_tags || []).length) return false;
    for (const t of crit.error_tags) { if (!crit.severities[t]) return false; }
    if (isBaselinePair() && (state.taxonomy.failure_modes || []).length
        && !(crit.failure_tags || []).length) return false;
    return true;
  }

  function reasoningConditionsMet() {
    if (!stepsReview().ok) return false;
    // Grounding-required tasks hard-gate a citation on EVERY step (the server
    // 400s otherwise); enforce it HERE, where the per-step anchor UI lives,
    // so submit can never dead-end later with no way to fix it.
    if ((state.task.grounding_mode || 'optional') === 'required') {
      const steps = activeSteps().filter((s) => (s.text || '').trim());
      const isReasoningTask = !!state.task.capture_reasoning || steps.length > 0;
      if (isReasoningTask) {
        for (const s of steps) { if (!isValidAnchor(s.evidence_anchor)) return false; }
      }
    }
    return true;
  }

  // Explicit completion per substage: the *_done flag (the Save/Continue click)
  // AND the section's data conditions, so deleting data from a re-opened
  // section honestly regresses the flow instead of leaving a stale checkmark.
  function substageComplete(key) {
    const d = state.draft;
    switch (key) {
      case 'compare': return !!d.verdict;
      case 'refine': return !!d.refine_saved;
      case 'why_better': return !!d.why_better_done && whyBetterConditionsMet();
      case 'citations': return !!d.citations_reviewed;
      case 'critique_rejected': return !!d.critique_done && critiqueConditionsMet();
      case 'from_scratch': return !!d.from_scratch_saved && !!(d.from_scratch.ideal_answer || '').trim();
      case 'reasoning': return !!d.reasoning_done && reasoningConditionsMet();
      case 'rubric': return !!d.rubric_done && rubricGate().ok;
      case 'confidence': return !!d.confidence_set;
      default: return false;
    }
  }

  // First incomplete substage, or 'done' when everything is complete.
  function currentSubstage() {
    for (const key of compareSubstages()) {
      if (!substageComplete(key)) return key;
    }
    return 'done';
  }

  // Reset every downstream completion when the verdict/chosen side changes:
  // sections completed against the previous answer must not survive it.
  function resetStagedFlow() {
    const d = state.draft;
    d.refine_saved = false;
    d.why_better_done = false;
    d.citations_reviewed = false;
    d.critique_done = false;
    d.from_scratch_saved = false;
    d.reasoning_done = false;
    d.rubric_done = false;
    d.confidence_set = false;
    d.rubricCursor = 0;
    state._reopenedSubstage = null;
    // Force the next renderRationale to treat the (re)computed current section
    // as freshly mounted, so the scroll-into-view fires after a verdict switch.
    state._lastSubstage = null;
  }

  // One save + re-render + submit-state pass; every section's Save/Continue
  // funnels through here so advancing is a single code path.
  function refreshStagedFlow() {
    saveDraft();
    renderRationale();
    updateSubmitState();
  }

  // Smooth-scroll a freshly-mounted section's heading to ~120px below the
  // sticky chrome. Honors prefers-reduced-motion (jump, don't animate).
  function scrollToSubstage(key) {
    const el = document.querySelector('[data-substage="' + key + '"]');
    if (!el) return;
    const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const y = el.getBoundingClientRect().top + window.pageYOffset - 120;
    try { window.scrollTo({ top: Math.max(0, y), behavior: reduce ? 'auto' : 'smooth' }); }
    catch (e) { window.scrollTo(0, Math.max(0, y)); }
  }

  // ─── §16 Task progress: ONE source of truth ────────────────────────────────
  // The full ordered step list: prompt_review → independent_answer → the §1
  // compare substages → submit. Both the header bar and stageHeader's step
  // counter read from here, so they can never disagree.
  function taskProgress() {
    const d = state.draft;
    if (!d) return { done: 0, total: 1, pct: 0 };
    const steps = [];
    steps.push({ key: 'prompt_review', done: d.stage !== 'prompt_review' });
    steps.push({ key: 'independent_answer', done: d.stage === 'compare' });
    if (isV3()) {
      compareSubstages().forEach((k) => {
        steps.push({ key: k, done: d.stage === 'compare' && substageComplete(k) });
      });
    } else {
      steps.push({ key: 'compare', done: false }); // completes at submit
    }
    // Submit is the last increment; the bar reads 100% only while submitting.
    steps.push({ key: 'submit', done: !!state.submitting });
    const done = steps.filter((s) => s.done).length;
    return { done, total: steps.length, pct: Math.round((100 * done) / steps.length) };
  }

  // The slim header progress rail (§16): centered between the wordmark and the
  // account cluster, visible only while a V3/V4 task is open. Width animates via
  // transform (scaleX) only; reduced-motion disables the transition in CSS.
  function updateHeaderProgress() {
    const header = document.getElementById('ascHeader');
    if (!header) return;
    let host = document.getElementById('ascHeaderProgress');
    const active = !!(state.user && state.view === 'eval' && state.panel === 'tasks'
      && state.portalChosen && state.task && state.draft && state.draft.stage && isV3());
    if (!active) { if (host) host.remove(); return; }
    if (!host) {
      const fill = h('div', { class: 'asc-hp-fill', id: 'ascHeaderProgressFill' });
      host = h('div', {
        class: 'asc-header-progress', id: 'ascHeaderProgress',
        role: 'progressbar', 'aria-label': 'Task progress',
        'aria-valuemin': '0', 'aria-valuemax': '100',
      },
        h('div', { class: 'asc-hp-track' }, fill),
        h('span', { class: 'asc-hp-pct', id: 'ascHeaderProgressPct' }, '0%'));
      const nav = document.getElementById('ascNav');
      if (nav && nav.parentNode) nav.parentNode.insertBefore(host, nav.nextSibling);
      else header.appendChild(host);
    }
    const p = taskProgress();
    host.setAttribute('aria-valuenow', String(p.pct));
    const fillEl = document.getElementById('ascHeaderProgressFill');
    if (fillEl) fillEl.style.transform = 'scaleX(' + (p.pct / 100) + ')';
    const pctEl = document.getElementById('ascHeaderProgressPct');
    if (pctEl) pctEl.textContent = p.pct + '%';
  }

  // ─── §2 Info-dot: the reusable "?" explainer ───────────────────────────────
  // A 16px circular "?" after a section title; click/keyboard opens a small
  // anchored popover (two short lines), dismissed on outside-click or Esc.
  // Help, never a gate.
  function infoDot(titleText, bodyLines) {
    const btn = h('button', {
      class: 'asc-info-dot', type: 'button',
      'aria-label': 'What is this?', 'aria-expanded': 'false',
    }, '?');
    const wrap = h('span', { class: 'asc-info-wrap' }, btn);
    let pop = null;
    const close = () => {
      if (!pop) return;
      pop.remove(); pop = null;
      btn.setAttribute('aria-expanded', 'false');
      document.removeEventListener('click', onDocClick, true);
      document.removeEventListener('keydown', onKey, true);
    };
    const onDocClick = (e) => { if (pop && !wrap.contains(e.target)) close(); };
    const onKey = (e) => { if (e.key === 'Escape') { close(); btn.focus(); } };
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (pop) { close(); return; }
      pop = h('div', { class: 'asc-info-pop', role: 'note' },
        titleText ? h('div', { class: 'asc-info-pop-title' }, titleText) : null,
        (bodyLines || []).map((l) => h('p', { class: 'asc-info-pop-line' }, l)));
      wrap.appendChild(pop);
      btn.setAttribute('aria-expanded', 'true');
      document.addEventListener('click', onDocClick, true);
      document.addEventListener('keydown', onKey, true);
    });
    return wrap;
  }

  // Section shell: mono chrome step label (STEP N · …), the title, an optional
  // info dot; every §1 section mounts through this.
  function sectionCard(key, info, ...children) {
    const meta = SUBSTAGE_META[key] || { chrome: key.toUpperCase(), title: key };
    const n = compareSubstages().indexOf(key) + 1;
    return h('div', { class: 'asc-card asc-card-pad asc-substage', dataset: { substage: key } },
      h('div', { class: 'asc-substage-head' },
        h('div', { class: 'asc-substage-step' }, 'STEP ' + n + ' · ' + meta.chrome),
        h('div', { class: 'asc-substage-title' }, meta.title, info || null)),
      ...children);
  }

  // ─── Workspace render (3 gated stages) ─────────────────────────────────────
  // Stage 1 prompt_review + Stage 2 independent_answer NEVER render the candidate
  // answer text into the DOM (anti-peeking, Eval Flow Upgrade §1). Only the
  // compare stage reveals A/B.
  const STAGES = ['prompt_review', 'independent_answer', 'compare'];

  function stageHeader(label) {
    const d = state.draft;
    const n = STAGES.indexOf(d.stage) + 1;
    const dots = h('div', { class: 'asc-stage-dots' });
    STAGES.forEach((s, i) => dots.appendChild(
      h('span', { class: 'asc-stage-dot' + (i < n ? ' done' : '') + (i === n - 1 ? ' active' : '') })));
    // V1/V2: the compare stage's submit bar owns the live #ascTimer (avoid a
    // duplicate id, only the first match would update); stages 1–2 host it here.
    // V3/V4: the submit bar is DEFERRED to the confidence substage (§15), and its
    // V3 variant carries no timer; this header owns #ascTimer in every stage.
    const timer = (d.stage === 'compare' && !isV3())
      ? null
      : h('span', { class: 'asc-timer', id: 'ascTimer', 'data-tour-ignore': '1' }, formatTime(getElapsed()));
    // §16: the step counter reads from taskProgress(); the same single source
    // of truth as the header bar. V1/V2's 3-stage list yields the same "Step N
    // of 3" text as before; V3/V4 span the full substage flow.
    let stepText;
    if (isV3()) {
      const p = taskProgress();
      stepText = 'Step ' + Math.min(p.done + 1, p.total) + ' of ' + p.total;
    } else {
      stepText = 'Step ' + n + ' of ' + STAGES.length;
    }
    return h('div', { class: 'asc-stage-head' },
      h('div', { class: 'asc-stage-meta' },
        h('span', { class: 'asc-stage-step' }, stepText),
        h('span', { class: 'asc-stage-label' }, label)),
      h('div', { class: 'asc-stage-right' }, dots, timer));
  }

  // ─── Home page: choose your evaluation experience (§5) ─────────────────────
  // Cards lead with WHAT the physician will work on, never a version number.
  // Order: Real (v4) · Synthetic (v3) · Longitudinal (v5, coming soon). The
  // legacy V1/V2 flows keep their exact cards behind an "Other flows" affordance.
  const VERSION_OPTS = [
    {
      // V4 (EHR PRD §9.5): the V3 flow over REAL de-identified patient cases.
      // Shown LOCKED unless the contributor is real_data_approved; serving is
      // enforced server-side regardless; the lock is honest UI, not the gate.
      v: 'v4', label: 'Real De-Identified Multimodal Cases', tag: 'Real patient data', dot: 'asc-dot-pink',
      requiresRealData: true,
      blurb: 'Work through real, de-identified patient cases: labs, notes, and a real clinical snapshot. Same task as synthetic; the data is real.',
      bullets: [
        'Read a real de-identified case: labs, notes, meds, vitals',
        'Give a 10-second first impression before you see the AI answers',
        'Pick the better of two AI answers, refine it, and say why',
        'A single point-in-time (static) case, not a timeline',
        'Requires real-data approval (BAA / training)',
      ],
    },
    {
      v: 'v3', label: 'Synthetic Multimodal Cases', tag: 'Recommended', dot: 'asc-dot-lime',
      blurb: 'Structured synthetic cases (labs, EHR notes, and meds) built to be hard.',
      bullets: [
        'Read a multimodal case: labs, EHR notes, meds, vitals',
        'Give a 10-second first impression before the AI answers appear',
        'Compare two AI answers and pick the stronger one',
        'Refine it, flag the weaker one’s errors, and check the reasoning',
      ],
    },
    {
      // V5: the AGENTIC tier (Clinical RL Environments PRD). A different KIND of
      // task, not a variant of the single-turn flow, so selecting it navigates to
      // its own surface instead of calling chooseVersion(): the single-turn queue,
      // submit path, and portal_version stamping are never touched by V5.
      v: 'v5', label: 'Clinical RL Environment', tag: 'New', dot: 'asc-dot-orange',
      route: '/asclepius/v5/annotate',
      blurb: 'Review an AI agent working a case step by step: label each move and write what it should have done instead.',
      bullets: [
        'Watch an agent order tests and reason across multiple steps',
        'Label each step correct / suboptimal / wrong',
        'Mark the first error and write the correct next action',
        'Validate the environment’s auto-reward against your judgment',
      ],
    },
  ];
  // The legacy flows, content untouched, tucked behind "Other flows".
  const LEGACY_VERSION_OPTS = [
    {
      v: 'v2', label: 'V2 · Assisted', tag: null, dot: 'asc-dot-orange',
      blurb: 'The assisted flow, under 10 minutes per task.',
      bullets: [
        'A 30-second quick take before you see the answers',
        'Model-suggested labels you verify (never auto-applied)',
        'Side-by-side answer diff: read only what differs',
        'Voice dictation on every field',
      ],
    },
    {
      v: 'v1', label: 'V1 · Classic', tag: null, dot: 'asc-dot-faint',
      blurb: 'The original flow: write your full ideal answer.',
      bullets: [
        'Write your complete ideal answer before reveal',
        'No AI suggestions, your judgment only',
        'Full-text answer comparison',
      ],
    },
  ];
  function chooseVersion(v) {
    setPortalVersion(v);
    state.portalChosen = true;
    renderEvalView();
  }
  // A version option may either enter the single-turn flow (chooseVersion) or, for a
  // tier that is a different KIND of task (V5 agentic), navigate to its own surface.
  // V5 deliberately does NOT go through setPortalVersion(), so 'v5' can never end up
  // in the single-turn queue params or on a single-turn submission.
  function selectVersion(o) {
    if (o.route) { window.location.href = o.route; return; }
    chooseVersion(o.v);
  }
  function versionCard(o, last, approved) {
    const locked = !!(o.requiresRealData && !approved);
    const soon = !!o.comingSoon;
    const inert = locked || soon;
    return h('div', {
      class: 'asc-ver-card' + (last === o.v && !soon ? ' last-used' : '')
        + (locked ? ' asc-ver-locked' : '') + (soon ? ' asc-ver-soon' : ''),
      role: soon ? null : 'button',
      tabindex: soon ? null : '0',
      'aria-disabled': inert ? 'true' : null,
      onClick: inert ? null : () => selectVersion(o),
      onKeydown: inert ? null : (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectVersion(o); }
      },
    },
      h('div', { class: 'asc-ver-card-head' },
        h('span', { class: 'asc-ver-card-icon ' + (o.dot || 'asc-dot-faint'), 'aria-hidden': 'true' }),
        h('div', {},
          h('div', { class: 'asc-ver-card-title' }, o.label,
            o.tag ? h('span', { class: 'asc-ver-card-tag' + (o.requiresRealData ? ' asc-ver-tag-real' : '') }, o.tag) : null,
            last === o.v && !inert ? h('span', { class: 'asc-ver-card-last' }, 'Last used') : null),
          h('div', { class: 'asc-ver-card-blurb' }, o.blurb))),
      h('ul', { class: 'asc-ver-card-list' }, o.bullets.map((b) => h('li', {}, b))),
      soon
        ? h('button', { class: 'asc-btn asc-btn-ghost asc-btn-block', type: 'button', tabindex: '-1', disabled: true },
            'Coming soon')
        : locked
          ? h('button', { class: 'asc-btn asc-btn-ghost asc-btn-block', type: 'button', tabindex: '-1', disabled: true },
              'Requires real-data approval')
          : h('button', { class: 'asc-btn asc-btn-primary asc-btn-block', type: 'button', tabindex: '-1' },
              'Start →'));
  }
  function renderVersionHome() {
    stopTimer();
    updateHeaderProgress(); // no open task, so the §16 bar hides here
    const last = getPortalVersion();
    const approved = !!(state.user && state.user.real_data_approved);
    const cards = h('div', { class: 'asc-ver-cards' });
    VERSION_OPTS.forEach((o) => cards.appendChild(versionCard(o, last, approved)));

    // Legacy flows, folded away, exactly as they were, one click deeper.
    const legacyCards = h('div', { class: 'asc-ver-cards', hidden: true });
    LEGACY_VERSION_OPTS.forEach((o) => legacyCards.appendChild(versionCard(o, last, approved)));
    const legacyToggle = h('button', {
      class: 'asc-btn-link', type: 'button', style: 'display:block;margin:18px auto 0',
      onClick: () => {
        const showing = !legacyCards.hasAttribute('hidden');
        if (showing) { legacyCards.setAttribute('hidden', ''); legacyToggle.textContent = 'Other flows (classic & assisted) ▾'; }
        else { legacyCards.removeAttribute('hidden'); legacyToggle.textContent = 'Other flows (classic & assisted) ▴'; }
      },
    }, 'Other flows (classic & assisted) ▾');

    setRoot(h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-ver-home' },
        h('h1', { class: 'asc-ver-home-title' }, 'Choose your evaluation experience'),
        h('p', { class: 'asc-ver-home-sub' },
          'Same clinical judgment, same training data. Pick how you want to work.'),
        cards,
        legacyToggle,
        legacyCards)));
  }

  // ─── V3/V4 specialty picker (Specialty Hyper-Personalization PRD §1) ────────
  // A popover over the current view (Eval UI Overhaul §2), shown before the case
  // loads. Each option is one card: its palette dot (nephrology green ·
  // cardiology orange · oncology pink) and the specialty name: nothing else.
  // The choice sets ``state.portalSpecialty`` (persisted) and is sent on every
  // task fetch. Reads GET /specialties, so enabling a 4th specialty later needs
  // NO frontend change.
  //
  // §2: a pick sets state and nothing else. The picker is a popover now, so a
  // pick no longer implies a route: the two call sites decide what it means
  // (first entry continues into the load it is already running; "Change
  // specialty" reloads only when the specialty actually changed).
  function chooseSpecialty(sp) {
    setPortalSpecialty(sp);
    state.specialtyChosen = true;
  }

  // §2: three options, reversible. A full-page route reads as a bigger decision
  // than it is, and the return trip re-renders the whole view. Same data, same
  // fetch, no navigation: a centred popover over whatever is already on screen.
  //
  // Resolves with the chosen specialty, or null if dismissed. ``dismissable``
  // is false on first entry: the choice is required there, so an escape hatch
  // would leave the physician looking at a view with no case in it.
  async function renderSpecialtyPicker(opts) {
    opts = opts || {};
    const dismissable = !!opts.dismissable;
    if (!state.specialties) {
      try { const d = await api('/specialties'); state.specialties = (d && d.specialties) || []; }
      catch (e) { state.specialties = []; }
    }
    const last = getPortalSpecialty();
    const enabled = (state.specialties || []).filter((s) => s.enabled);
    const list = enabled.length ? enabled : [{ specialty: 'nephrology', blurb: '', buckets: [] }];

    return new Promise((resolve) => {
      const restoreFocus = document.activeElement;
      let settled = false;
      const close = (value) => {
        if (settled) return;
        settled = true;
        document.removeEventListener('keydown', onKeydown, true);
        if (sheet.parentNode) sheet.parentNode.removeChild(sheet);
        document.body.classList.remove('asc-sheet-open');
        if (restoreFocus && restoreFocus.focus) { try { restoreFocus.focus(); } catch (e) { /* detached */ } }
        resolve(value);
      };

      const cards = h('div', { class: 'asc-spec-grid' });
      list.forEach((s) => {
        const name = s.specialty.charAt(0).toUpperCase() + s.specialty.slice(1);
        // The card IS the button: a "Grade Nephrology →" button inside a
        // clickable card is the same tap described twice.
        cards.appendChild(h('button', {
          class: 'asc-spec-card' + (last === s.specialty ? ' last-used' : ''),
          type: 'button',
          onClick: () => { chooseSpecialty(s.specialty); close(s.specialty); },
        },
          h('span', { class: 'asc-spec-card-dot ' + specialtyDot(s.specialty), 'aria-hidden': 'true' }),
          h('span', { class: 'asc-spec-card-name' }, name),
          last === s.specialty ? h('span', { class: 'asc-ver-card-last' }, 'Last used') : null));
      });

      const card = h('div', { class: 'asc-sheet-card' },
        h('h2', { class: 'asc-sheet-title', id: 'ascSheetTitle' }, 'Choose a specialty'),
        cards);
      const sheet = h('div', {
        class: 'asc-sheet', role: 'dialog', 'aria-modal': 'true',
        'aria-labelledby': 'ascSheetTitle',
        onClick: (e) => { if (dismissable && e.target === sheet) close(null); },
      }, card);

      function onKeydown(e) {
        if (e.key === 'Escape' && dismissable) { e.preventDefault(); close(null); return; }
        if (e.key !== 'Tab') return;
        // Keep Tab inside the dialog: aria-modal is a promise to assistive
        // tech that has to be true of the focus order too.
        const focusable = Array.from(sheet.querySelectorAll('button:not([disabled])'));
        if (!focusable.length) return;
        const first = focusable[0], lastEl = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); lastEl.focus(); }
        else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); first.focus(); }
      }

      document.addEventListener('keydown', onKeydown, true);
      document.body.classList.add('asc-sheet-open');
      document.body.appendChild(sheet);
      const preferred = cards.querySelector('.last-used') || cards.firstChild;
      if (preferred && preferred.focus) preferred.focus();
    });
  }

  // Small read-only indicator inside the workspace: which experience this task
  // is being graded under. Case type and specialty are assigned by us (queue
  // routing + qualification), not chosen by the doctor, so this is
  // informational only, no controls to switch it.
  // This row is the task bar: what kind of case this is on the left, and the one
  // control that changes the layout on the right. The toggle has a single home
  // and never moves — only its label flips — so it is muscle memory by the
  // fortieth case. Called with no arguments everywhere except the V3 stage-3
  // branch, where the ternary yields null and the row renders as before.
  function renderExperienceBadge(toggle, isOpen) {
    const v = draftVersion();
    const meta = { v4: 'Real · De-identified Cases', v3: 'Synthetic Multimodal',
                   v2: 'V2 · Assisted', v1: 'V1 · Classic' }[v] || 'V1 · Classic';
    return h('div', { class: 'asc-exp-badge' },
      h('span', { class: 'asc-exp-badge-label' }, meta),
      toggle
        ? h('button', {
            class: 'asc-btn asc-btn-ghost asc-btn-sm asc-case-toggle',
            type: 'button', title: isOpen ? 'Hide case' : 'Open case', onClick: toggle,
          }, isOpen ? 'Hide case' : 'Open case')
        : null);
  }

  // ─── §6 semantic case-tag chips (V3/V4) ─────────────────────────────────────
  // Consistent, meaningful color from the console palette (no blue, it left
  // with the design-system migration): a stable hue per specialty, semantic
  // difficulty (hard=pink, medium=orange, easy=green), lime=multimodal,
  // orange=reasoning (model), pink=grounding (attention). Color always pairs
  // with the text label, never the sole carrier.
  const SPECIALTY_DOT = { nephrology: 'asc-dot-green', cardiology: 'asc-dot-orange', oncology: 'asc-dot-pink' };
  const _SPECIALTY_CYCLE = ['asc-dot-lime', 'asc-dot-green', 'asc-dot-orange', 'asc-dot-pink'];
  function specialtyDot(spec) {
    const s = (spec || '').toLowerCase();
    if (SPECIALTY_DOT[s]) return SPECIALTY_DOT[s];
    // Deterministic per-specialty hue for anything unmapped: same specialty,
    // same color, every time.
    let acc = 0;
    for (let i = 0; i < s.length; i++) acc = (acc + s.charCodeAt(i)) % 997;
    return _SPECIALTY_CYCLE[acc % _SPECIALTY_CYCLE.length];
  }
  const DIFFICULTY_DOT = { hard: 'asc-dot-pink', medium: 'asc-dot-orange', easy: 'asc-dot-green' };
  function metaChip(label, dotClass, title) {
    return h('span', { class: 'asc-meta-chip', title: title || null },
      h('span', { class: 'asc-meta-chip-dot ' + dotClass, 'aria-hidden': 'true' }), label);
  }

  function renderTaskWorkspace() {
    const task = state.task;
    const d = state.draft;
    const required = (task.grounding_mode || 'optional') === 'required';

    const caseObj = multimodalCase();
    // For a multimodal task the case is shown in the structured panel below, so
    // the prompt card carries only the clinical QUESTION (parsed out of the
    // rendered prompt); no duplicated wall of serialized case text.
    const promptText = caseObj ? caseQuestion(task.prompt) : (task.prompt || '');
    const diff = (task.difficulty || 'medium');
    // §6: V3/V4 get the semantic dot chips; V1/V2 keep the muted badges as-is.
    const metaRow = isV3()
      ? h('div', { class: 'asc-meta-row' },
          metaChip(task.specialty || 'general', specialtyDot(task.specialty),
            'Specialty: same specialty, same color, always'),
          metaChip('Difficulty: ' + diff, DIFFICULTY_DOT[diff] || 'asc-dot-orange',
            'hard = pink · medium = orange · easy = green'),
          caseObj ? metaChip('Multimodal case', 'asc-dot-lime') : null,
          task.capture_reasoning ? metaChip('Reasoning capture', 'asc-dot-orange',
            'This task captures the model’s step-by-step reasoning') : null,
          required ? metaChip('Grounding required', 'asc-dot-pink',
            'Evidence citations are required on this task') : null)
      : h('div', { class: 'asc-meta-row' },
          h('span', { class: 'asc-badge asc-badge-primary' }, task.specialty || 'general'),
          h('span', { class: 'asc-badge asc-badge-gray' }, 'Difficulty: ' + (task.difficulty || 'medium')),
          caseObj ? h('span', { class: 'asc-badge asc-badge-accent' }, 'Multimodal case') : null,
          task.capture_reasoning ? h('span', { class: 'asc-badge asc-badge-accent' }, 'Reasoning capture') : null,
          required ? h('span', { class: 'asc-badge asc-badge-amber' }, 'Grounding required') : null,
        );
    const promptCard = h('div', { class: 'asc-card asc-prompt-card' },
      h('div', { class: 'asc-card-pad' },
        metaRow,
        h('div', { class: 'asc-prompt-label' }, caseObj ? 'Clinical question' : 'Clinical prompt'),
        h('div', { class: 'asc-prompt-text' }, promptText),
      ));

    // Grounding disclaimer banner (required mode only)
    let groundingBanner = null;
    if (required && task.grounding_disclaimer) {
      groundingBanner = h('div', { class: 'asc-grounding-banner' },
        h('div', { class: 'asc-gb-icon', 'aria-hidden': 'true' }),
        h('div', {},
          h('div', { class: 'asc-gb-title' }, 'Evidence required for this task'),
          h('div', { class: 'asc-gb-text' }, task.grounding_disclaimer),
        ));
    }

    // ── V1/V2 (classic): unchanged single-column layout ─────────────────────
    // The staged split-screen is a V3/V4 feature; V1 and V2 render exactly as
    // before so their submissions and exports stay byte-for-byte identical.
    if (!isV3()) {
      const wrap = h('div', { class: 'asc-wrap' }, renderExperienceBadge(), promptCard, renderCasePanel(), groundingBanner);
      if (d.stage === 'prompt_review') {
        wrap.appendChild(stageHeader('Review the prompt'));
        wrap.appendChild(renderPromptGate());
        wrap.appendChild(blurredPlaceholder('The AI answers stay hidden until you confirm the prompt is clinically valid.'));
      } else if (d.stage === 'independent_answer') {
        wrap.appendChild(stageHeader('Write your answer'));
        wrap.appendChild(renderIndependentAnswer());
        wrap.appendChild(blurredPlaceholder('Write your ideal answer first, then reveal the AI answers to compare.'));
      } else {
        wrap.appendChild(stageHeader('Compare & grade'));
        renderCompareStage(wrap);
      }
      setRoot(wrap);
      if (d.stage === 'compare') { refreshAnswerHighlight(); renderRationale(); updateSubmitState(); loadAssist(); }
      updateHeaderProgress();
      return;
    }

    // ── V3/V4 stages 1–2: the case IS the thing being judged ────────────────
    // The case is the object of judgment in stages 1–2 and a reference in stage
    // 3+. Placement follows that, not the other way round: a physician asked "is
    // this case valid?" should not be reading it in a 38% sidebar.
    const CENTRE_STAGE_CASE = d.stage === 'prompt_review' || d.stage === 'independent_answer';
    if (CENTRE_STAGE_CASE) {
      // No collapse control on these two stages, deliberately. A "hide the case"
      // button on the screen that asks *is this case clinically valid* invites
      // answering without reading, and that gate feeds everything downstream.
      const wrap = h('div', { class: 'asc-wrap asc-wrap-case' },
        renderExperienceBadge(), promptCard, renderCasePanel(), groundingBanner);
      wrap.appendChild(stageHeader(d.stage === 'prompt_review' ? 'Review the prompt' : 'Write your answer'));
      wrap.appendChild(d.stage === 'prompt_review' ? renderPromptGate() : renderIndependentAnswer());
      wrap.appendChild(blurredPlaceholder(d.stage === 'prompt_review'
        ? 'The AI answers stay hidden until you confirm the prompt is clinically valid.'
        : 'Write your ideal answer first, then reveal the AI answers to compare.'));
      setRoot(wrap);
      updateHeaderProgress();
      return;
    }

    // ── V3/V4 stage 3+: split-screen: the case stays pinned beside the workflow ──
    // Left rail = clinical question + structured case (its own scroll), right
    // column = the step-by-step staged flow. The rail collapses to a wide single
    // column, and on narrow screens it stacks behind the slim sticky case bar.
    // The two column headers — "CASE" and "STEP n OF m" — are placed in the SAME
    // GRID ROW rather than one inside each column. That is what makes the card
    // tops line up, and it is structural: the browser sizes a row to its tallest
    // cell, so row 2 starts at the same y on both sides and cannot drift. Two
    // previous attempts matched heights by hand across separate columns and both
    // missed — by 17px, then by 51px — because the columns carry different gaps
    // and the work side has one more header row than the rail.
    const shell = h('div', {
      class: 'asc-task-shell' + (state._caseRailCollapsed ? ' is-collapsed' : ''),
    });
    const grid = h('div', { class: 'asc-case-cols' + (state._caseRailCollapsed ? ' is-collapsed' : '') });
    const workCol = h('div', { class: 'asc-work-col' });
    const railHead = h('div', { class: 'asc-rail-head' },
      h('span', { class: 'asc-rail-title' }, 'Case'));
    const toggleRail = () => {
      state._caseRailCollapsed = !state._caseRailCollapsed;
      grid.classList.toggle('is-collapsed', state._caseRailCollapsed);
      shell.classList.toggle('is-collapsed', state._caseRailCollapsed);
      paintCaseToggle();
    };
    // Expose it for the `C` shortcut, which fires from the document and has no
    // way into this closure otherwise.
    state._toggleCaseRail = toggleRail;
    // ONE toggle, one home, above both columns. It changes the whole grid, so it
    // belongs to the grid and not to a pane inside it — pinned to the right edge
    // of the left column it landed in the visual centre of the page and read as
    // belonging to neither side. Only the label moves between states, so the
    // control itself stays where the hand already is.
    const taskBar = renderExperienceBadge(toggleRail, !state._caseRailCollapsed);
    function paintCaseToggle() {
      const b = taskBar.querySelector('.asc-case-toggle');
      if (!b) return;
      const label = state._caseRailCollapsed ? 'Open case' : 'Hide case';
      b.textContent = label;
      b.title = label;
    }
    const caseRail = h('aside', { class: 'asc-case-rail' },
      promptCard,
      renderCasePanel() || h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' },
                             promptText || 'n/a'),
      groundingBanner);

    workCol.appendChild(renderCaseSticky(promptText));

    let stageHead;
    if (d.stage === 'prompt_review') {
      stageHead = stageHeader('Review the prompt');
      workCol.appendChild(renderPromptGate());
      workCol.appendChild(blurredPlaceholder('The AI answers stay hidden until you confirm the prompt is clinically valid.'));
    } else if (d.stage === 'independent_answer') {
      stageHead = stageHeader('Write your answer');
      workCol.appendChild(renderIndependentAnswer());
      workCol.appendChild(blurredPlaceholder('Write your ideal answer first, then reveal the AI answers to compare.'));
    } else {
      stageHead = stageHeader('Compare & grade');
      renderCompareStage(workCol);
    }

    // Row 1: the two chrome labels, side by side. Row 2: the two content columns.
    grid.appendChild(railHead);
    grid.appendChild(stageHead);
    grid.appendChild(caseRail);
    grid.appendChild(workCol);
    shell.appendChild(taskBar);
    shell.appendChild(grid);
    setRoot(shell);
    if (d.stage === 'compare') {
      refreshAnswerHighlight();
      renderRationale();
      updateSubmitState();
      loadAssist(); // fire-and-forget: suggestions appear when ready (Speed Opt §2)
    }
    updateHeaderProgress();
  }

  // `C` toggles the case panel from anywhere in the staged flow, so a physician
  // doing forty cases learns one key instead of hunting for a control.
  function toggleCaseRail() {
    if (state._toggleCaseRail && document.querySelector('.asc-case-cols')) state._toggleCaseRail();
  }

  // ─── §17 Sticky case strip + case overlay (V3/V4 compare stage) ─────────────
  // A slim sticky bar carrying the clinical question and a "View case" control,
  // so labs/notes/meds are reachable from any section without scrolling back up.
  function renderCaseSticky(questionText) {
    const hasCase = !!multimodalCase();
    return h('div', { class: 'asc-case-sticky' },
      h('span', { class: 'asc-case-sticky-label' }, 'CASE'),
      h('span', { class: 'asc-case-sticky-q', title: questionText || '' }, questionText || ''),
      h('button', {
        class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
        onClick: () => openCaseOverlay(questionText),
      }, hasCase ? 'View case ▾' : 'View question ▾'));
  }

  function openCaseOverlay(questionText) {
    const overlay = h('div', {
      class: 'call-team-overlay is-open',
      onClick: (e) => { if (e.target === overlay) closeOverlay(); },
    });
    const onKey = (e) => { if (e.key === 'Escape') closeOverlay(); };
    // Escape closes and focus goes back where it came from, the same contract the
    // tag popover keeps — this one IS modal, so it says so.
    const opener = document.activeElement;
    function closeOverlay() {
      overlay.remove();
      document.removeEventListener('keydown', onKey, true);
      if (opener && opener.isConnected && opener.focus) opener.focus();
    }
    document.addEventListener('keydown', onKey, true);
    const panel = renderCasePanel();
    const popup = h('div', {
      class: 'call-team-popup asc-case-popup', onClick: (e) => e.stopPropagation(),
      role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Case reference',
      style: 'max-width:860px;max-height:88vh;overflow:auto;text-align:left',
    },
      h('div', { class: 'call-team-title' }, 'Case reference'),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Clinical question'),
        h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, questionText || state.task.prompt || 'n/a')),
      panel || h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, state.task.prompt || 'n/a'),
      h('div', { style: 'display:flex;margin-top:14px' },
        h('button', { class: 'asc-btn asc-btn-ghost', style: 'margin-left:auto', onClick: closeOverlay }, 'Close')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  // ─── Model-assisted pre-labeling (Speed Optimization §2) ────────────────────
  // Fetch the prelabel suggestion once per task (cached on the draft so a
  // refresh never re-bills the LLM). Suggestions are hints only: no verdict is
  // ever auto-selected, nothing is applied without an explicit tap, and the
  // server already hides low-confidence calls.
  function assistData() {
    const a = state.draft && state.draft.assist;
    return (a && a.fetched && !a.skipped && a.suggested_weaker) ? a : null;
  }
  function persistDraft(d) {
    try { localStorage.setItem(draftKey(d.task_id), JSON.stringify(d)); } catch (e) { /* ignore quota */ }
  }
  async function loadAssist() {
    const d = state.draft;
    if (!d || d.stage !== 'compare') return;
    if (tutorialActive()) return; // /assist/prelabel is task-scoped; the practice case is virtual
    if (!isAssisted()) return;  // model assist is an assisted-flow (V2 + V3) feature
    // V3 anti-rubber-stamp guard (Seamless PRD WS1): AI suggestions are hidden
    // until the clinician commits their OWN verdict. We don't merely hide them;
    // we don't even FETCH them, so the suggestion never reaches the client before
    // the verdict. The fetch is (re)triggered from selectVerdict once a side is
    // chosen. V2 keeps fetching on reveal (its established behavior).
    if (isV3() && !(d.verdict)) return;
    // Only a SUCCESSFUL response (including a server-side "skipped" degrade) is
    // cached on the draft; a transient failure (network blip, restart, 5xx) is
    // remembered in memory only, so the next page load retries instead of the
    // feature staying silently dead for the rest of the task.
    if ((d.assist && d.assist.fetched) || state.assistLoadingFor === d.task_id
        || state.assistFailedFor === d.task_id) { renderAssistUI(); return; }
    state.assistLoadingFor = d.task_id;
    try {
      const res = await api('/assist/prelabel', { method: 'POST', body: { task_id: d.task_id } });
      d.assist = Object.assign({ fetched: true }, res);
      // The LLM call can take seconds; the doctor may already be on another
      // task. Persist the result onto the draft it belongs to, and only touch
      // the live UI when that task is still the one on screen.
      persistDraft(d);
      if (state.draft === d) renderAssistUI();
    } catch (e) {
      state.assistFailedFor = d.task_id;
    } finally {
      if (state.assistLoadingFor === d.task_id) state.assistLoadingFor = null;
    }
  }
  // Surface freshly-arrived suggestions (BUG-4, decoupled from the diff):
  //   * The A/B diff is painted SYNCHRONOUSLY on reveal (renderCompareStage) and
  //     memoized per task_id (computeAnswerDiff), so it never waits on the LLM.
  //   * When the assist arrives LATER, we patch it in ADDITIVELY: update the
  //     verdict hint, and only touch the answer cards when there are actually
  //     error spans to highlight (nothing else about the cards changes on assist).
  //   * Never rebuild while the doctor is typing in the rationale (would steal
  //     focus); the chips appear on the next natural re-render instead.
  function renderAssistUI() {
    if (!isAssisted()) return;
    renderAssistHint();
    const a = assistData();
    if (!a) return;
    // Only re-render the answer cards if the suggestion carries error spans to
    // mark; otherwise the already-painted diff is unchanged, so leave it be.
    if ((a.error_spans || []).length) renderAnswersInto(document.getElementById('ascAnswers'));
    const active = document.activeElement;
    const rationale = document.getElementById('ascRationale');
    const typing = active && rationale && rationale.contains(active)
      && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT');
    if (state.draft && state.draft.verdict && !typing) renderRationale();
  }
  function renderAssistHint() {
    const el = document.getElementById('ascAssistHint');
    if (!el) return;
    clear(el);
    // §4 (V3/V4): the model's weaker-answer guess is retained in `assist`
    // (it still rides the submitted payload for override-rate analysis), but it is
    // never rendered. Surfacing it anchors the physician before they commit,
    // which is the exact bias the blinded A/B exists to prevent.
    if (isV3()) return;
    const a = assistData();
    if (!a) return;
    el.appendChild(h('span', { class: 'asc-assist-chip', 'aria-hidden': 'true' }));
    el.appendChild(h('span', {},
      'Model thinks ', h('strong', {}, a.suggested_weaker), ' is weaker. Tap a verdict to decide.'));
  }

  // A non-peekable stand-in for the answers/verdict during Stages 1–2. The real
  // candidate text is deliberately NOT placed in the DOM here.
  function blurredPlaceholder(caption) {
    const fake = h('div', { class: 'asc-blur-cards' },
      h('div', { class: 'asc-blur-card' }),
      h('div', { class: 'asc-blur-card' }));
    return h('div', { class: 'asc-card asc-card-pad asc-blur-wrap' },
      h('div', { class: 'asc-blur-stack' },
        fake,
        h('div', { class: 'asc-blur-overlay' },
          
          h('div', { class: 'asc-blur-caption' }, caption))));
  }

  // Stage 3: the original compare + verdict + rationale + submit block.
  function renderCompareStage(wrap) {
    // Safety net: if the withheld answer texts failed to load (e.g. a network
    // blip when resuming into compare via refresh), don't render blank answer
    // cards; offer a reload instead of letting the doctor grade nothing.
    if ((state.task.candidate_answers || []).some((c) => c.text == null)) {
      wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, 'Could not load the AI answers.'),
        h('button', {
          class: 'asc-btn asc-btn-primary', style: 'margin-top:12px',
          onClick: async () => {
            try { await loadWithheldAnswersIfNeeded(); renderTaskWorkspace(); }
            catch (e) { if (e.status !== 401) toast('Still could not load the answers: ' + e.message, 'error'); }
          },
        }, 'Reload answers')));
      return;
    }
    const answers = h('div', { class: 'asc-answers', id: 'ascAnswers' });
    renderAnswersInto(answers);

    // Diff view (Speed Optimization §3): V2 only. §3: V3/V4 drop the diff
    // entirely (toggle, legend and help line). The legend used to be appended
    // INTO `.asc-answers` (a 2-column grid), which pushed A into cell 2 and B
    // onto row 2; removing it restores the symmetric side-by-side layout with
    // no CSS change. V1 (classic) never had a toggle.
    const assisted = isAssisted();
    const showDiff = assisted && !isV3();
    const diffToggle = showDiff ? h('button', {
      class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button', id: 'ascDiffToggle',
      onClick: () => {
        state.showFullText = !state.showFullText;
        renderAnswersInto(document.getElementById('ascAnswers'));
        const b = document.getElementById('ascDiffToggle');
        if (b) b.textContent = state.showFullText ? '◧ Highlight differences' : '≡ Show full text';
      },
    }, state.showFullText ? '◧ Highlight differences' : '≡ Show full text') : null;

    const verdicts = h('div', { class: 'asc-verdicts', id: 'ascVerdicts' },
      verdictButton('A_better', 'A is better', '1'),
      verdictButton('B_better', 'B is better', '2'),
      verdictButton('both_inadequate', 'Both inadequate', '3', true),
    );
    // Assist hint container exists in the assisted flows (V2 + V3). In V3 it
    // stays empty until a verdict is committed (assist isn't fetched until then).
    const assistHint = assisted ? h('div', { class: 'asc-assist-hint', id: 'ascAssistHint' }) : null;
    const rationale = h('div', { id: 'ascRationale' });

    wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-compare-head' },
        h('div', { class: 'asc-card-title' }, 'Compare the answers'),
        diffToggle),
      showDiff ? h('p', { class: 'asc-help', style: 'margin:2px 0 14px' },
        'Shared text is dimmed; passages where the answers diverge are highlighted.') : null,
      answers));
    wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:14px' }, 'Your verdict',
        h('span', { class: 'asc-label-hint', style: 'font-weight:500;margin-left:6px' }, '(press 1 / 2 / 3)')),
      verdicts,
      assistHint,
      rationale));
    // §15 (the single biggest structural gotcha): on V3/V4 the submit bar (and
    // the confidence pills inside it) must NOT mount at compare entry. It mounts
    // from the §1 substage machine, only when the flow reaches `confidence`.
    // V1/V2 keep the eager submit bar exactly as before.
    if (!isV3()) wrap.appendChild(h('div', { class: 'asc-card' }, renderSubmitBar()));
    if (assisted) setTimeout(renderAssistHint, 0);
  }

  // ─── Sentence-level diff (Speed Optimization §3, dependency-free) ───────────
  // Character-exact split: concatenating the result reproduces the input, so
  // the rendered answer never differs from the real candidate text. A '.'
  // between two digits is a decimal (e.g. "K+ 1.0"), NOT a boundary; otherwise
  // dosing error-spans could never match inside a single sentence.
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
  function buildAnswerDiff(aText, bText, opts) {
    opts = opts || {};
    const aS = splitSentences(aText || ''), bS = splitSentences(bText || '');
    if (!aS.length || !bS.length) return null;
    const flags = diffFlags(aS, bS, !!opts.soft);
    if (flags.any) {
      return { A: { sents: aS, shared: flags.a }, B: { sents: bS, shared: flags.b }, allDivergent: false };
    }
    if (opts.markAllWhenDisjoint) {
      return {
        A: { sents: aS, shared: aS.map(() => false) },
        B: { sents: bS, shared: bS.map(() => false) },
        allDivergent: true,
      };
    }
    return null;
  }
  function computeAnswerDiff() {
    // Candidate texts are immutable for the life of a task, so memoize the LCS
    // so re-renders (toggle, assist arrival, verdict) don't repay the O(n*m) DP.
    if (state._diffCacheTask === state.task.task_id) return state._diffCache;
    const cands = state.task.candidate_answers || [];
    const A = cands.find((c) => c.id === 'A'), B = cands.find((c) => c.id === 'B');
    if (!A || !B || A.text == null || B.text == null) return null; // not cached: texts may still load
    // §3.1 is V3/V4-only: soft matching + the all-divergent fallback. V1/V2 keep
    // exact matching and the legacy null-on-disjoint behavior, byte-for-byte.
    const diff = buildAnswerDiff(A.text, B.text, { soft: isV3(), markAllWhenDisjoint: isV3() });
    state._diffCacheTask = state.task.task_id;
    state._diffCache = diff;
    return diff;
  }
  // Append text to a node, wrapping any model-suggested error span (Feature 2)
  // occurring inside it in a highlight mark. Shared by the diff and plain views
  // so error highlighting can never diverge between them.
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
  function renderAnswersInto(container) {
    if (!container) return;
    clear(container);
    // V1 (classic) renders plain full text: no diff, no error-span marks. V2
    // keeps the marked A/B diff. §3: V3/V4 render full text side by side: the
    // highlight asked the physician to trust a machine's notion of "what
    // differs" before they had read either answer.
    //
    // NOTE: this function must only ever append answer CARDS to `container`.
    // `.asc-answers` is a `1fr 1fr` grid, so any extra child (the old legend)
    // silently staggers A and B across two rows.
    const diff = (!isAssisted() || isV3() || state.showFullText) ? null : computeAnswerDiff();
    const a = assistData();
    (state.task.candidate_answers || []).forEach((c) => {
      container.appendChild(renderAnswerCard(c, diff, a));
    });
    refreshAnswerHighlight();
  }

  // ─── Stage 1: prompt validation gate (Feature A; §7 rebuild on V3/V4) ───────
  function renderPromptGate() {
    // §7 (V3/V4): ONE honest control. The primary action is simply to proceed;
    // a single low-emphasis "Flag as invalid" opens a required-reason capture
    // routed to admin (the former "case is internally inconsistent" mode folds
    // into the same free-text reason: one flag, one reason, one destination).
    if (isV3()) return renderPromptGateV3();
    const d = state.draft;
    const reasonBox = h('div', { id: 'ascFlagReason', hidden: true });
    const reasonInput = h('input', { class: 'asc-input', placeholder: 'One line: why is this prompt invalid? (e.g. ambiguous, not clinically meaningful, unsafe premise)', value: d.prompt_review.note || '' });
    reasonInput.addEventListener('input', () => { d.prompt_review.note = reasonInput.value; saveDraft(); });
    // One confirm button dispatches by the reason box's mode, so the flag and the
    // (multimodal-only) case-incoherent paths share the same reason input without
    // double-binding a handler.
    const confirmFlag = h('button', { class: 'asc-btn asc-btn-danger', onClick: () => {
      if (reasonBox.getAttribute('data-mode') === 'case_incoherent') flagCaseIncoherent();
      else flagPrompt();
    } }, 'Confirm, flag & skip');
    reasonBox.appendChild(h('div', { class: 'asc-field', style: 'margin-top:14px' },
      h('label', { class: 'asc-label' }, 'Reason for flagging'),
      reasonInput,
      h('div', { style: 'margin-top:10px' }, confirmFlag)));

    const isCase = !!multimodalCase();
    // Multimodal (Multimodal PRD §5): a clinician can flag a case whose labs /
    // notes / problems / meds are internally inconsistent; the human counterpart
    // to the case-judge coherence gate. Routes the case out (0 records) and feeds
    // back to recalibrate case generation.
    const incoherentBtn = isCase
      ? h('button', { class: 'asc-btn asc-btn-ghost', onClick: () => {
          reasonInput.placeholder = 'One line: what doesn’t add up? (e.g. the sodium contradicts the note)';
          reasonBox.hidden = false;
          reasonBox.setAttribute('data-mode', 'case_incoherent');
          confirmFlag.textContent = 'Confirm, case is inconsistent & skip';
          reasonInput.focus();
        } }, 'Case is internally inconsistent')
      : null;

    return h('div', { class: 'asc-card asc-card-pad asc-gate' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:6px' },
        isCase ? 'Is this case clinically valid?' : 'Is this prompt clinically valid?'),
      h('p', { class: 'asc-help', style: 'margin-bottom:16px' },
        isCase
          ? 'Confirm the case is coherent and answerable before you see any answer. Your sign-off upgrades the data; flagged cases are sent to review and excluded.'
          : 'Confirm the prompt is a real, answerable clinical question before you see any answer. Your sign-off upgrades the data; flagged prompts are sent to admin review and excluded.'),
      h('div', { class: 'asc-gate-actions' },
        h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', onClick: validatePrompt },
          isCase ? 'Case is clinically valid ✓' : 'Prompt is clinically valid ✓'),
        h('button', { class: 'asc-btn asc-btn-ghost', onClick: () => {
          reasonInput.placeholder = 'One line: why is this invalid? (e.g. ambiguous, not clinically meaningful, unsafe premise)';
          reasonBox.hidden = false;
          reasonBox.removeAttribute('data-mode');
          confirmFlag.textContent = 'Confirm, flag & skip';
          reasonInput.focus();
        } }, 'Flag as invalid'),
        incoherentBtn),
      reasonBox);
  }

  // §7 (V3/V4): one primary continue + one ghost flag with a required reason.
  function renderPromptGateV3() {
    const d = state.draft;
    const isCase = !!multimodalCase();
    const reasonInput = h('input', {
      class: 'asc-input',
      placeholder: 'Why is this ' + (isCase ? 'case' : 'prompt')
        + ' invalid? (e.g. ambiguous, not clinically meaningful, unsafe premise, internally inconsistent)',
      value: d.prompt_review.note || '',
    });
    const sendBtn = h('button', {
      // Enabled when a reason exists, including one prefilled from a resumed
      // draft, not only after fresh keystrokes.
      class: 'asc-btn asc-btn-danger', type: 'button',
      disabled: !(d.prompt_review.note || '').trim(),
      onClick: () => { if ((d.prompt_review.note || '').trim()) flagPrompt(); },
    }, 'Send to admin');
    reasonInput.addEventListener('input', () => {
      d.prompt_review.note = reasonInput.value;
      sendBtn.disabled = !(reasonInput.value || '').trim();
      saveDraft();
    });
    const reasonBox = h('div', { hidden: true },
      h('div', { class: 'asc-field', style: 'margin-top:14px' },
        h('label', { class: 'asc-label' }, 'Why is this ' + (isCase ? 'case' : 'prompt') + ' invalid?'),
        withMic(reasonInput),
        h('div', { style: 'margin-top:10px' }, sendBtn)));
    const flagBtn = h('button', {
      class: 'asc-btn-link asc-flag-invalid', type: 'button',
      onClick: () => {
        if (reasonBox.hasAttribute('hidden')) { reasonBox.removeAttribute('hidden'); reasonInput.focus(); }
        else reasonBox.setAttribute('hidden', '');
      },
    }, 'Flag as invalid');
    return h('div', { class: 'asc-card asc-card-pad asc-gate' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:6px' },
        isCase ? 'Review the case' : 'Review the prompt'),
      h('p', { class: 'asc-help', style: 'margin-bottom:16px' },
        'Read it through. If it’s a real, answerable clinical '
        + (isCase ? 'case' : 'question') + ', continue; flagged '
        + (isCase ? 'cases' : 'prompts') + ' leave your queue and go to admin review with your reason.'),
      h('div', { class: 'asc-gate-actions' },
        h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', dataset: { tour: 'prompt-continue' },
          onClick: validatePrompt },
          'Looks clinically valid, continue →'),
        flagBtn),
      reasonBox);
  }

  function validatePrompt() {
    const d = state.draft;
    d.prompt_review = { reviewed: true, verdict: 'valid', note: '', reviewed_at: new Date().toISOString() };
    d.stage = 'independent_answer';
    saveDraft();
    renderTaskWorkspace();
  }

  async function flagPrompt() {
    // The practice case is deliberately valid; flagging it would otherwise POST
    // a REAL submission (this path bypasses the tutorial submit branch).
    if (tutorialActive()) {
      toast('This is the practice case: it’s deliberately valid. Continue instead.', 'info');
      return;
    }
    const d = state.draft;
    d.prompt_review.reviewed = true;
    d.prompt_review.verdict = 'flagged';
    d.prompt_review.reviewed_at = new Date().toISOString();
    saveDraft();
    if (state.submitting) return;
    state.submitting = true;
    try {
      await api('/submissions', { method: 'POST', body: buildSubmissionPayload() });
      clearDraft(d.task_id);
      stopTimer();
      toast('Prompt flagged for review. Loading the next task', 'success');
      renderEvalView();
    } catch (e) {
      if (e.status !== 401) toast('Could not flag the prompt: ' + e.message, 'error');
    } finally {
      state.submitting = false;
    }
  }

  // Multimodal case flagged internally inconsistent (Multimodal PRD §5): mirrors
  // flagPrompt but stamps the case_incoherent verdict; the server routes the case
  // out (0 records) and feeds the signal back to case-generation recalibration.
  async function flagCaseIncoherent() {
    if (tutorialActive()) {
      toast('This is the practice case: it’s deliberately valid. Continue instead.', 'info');
      return;
    }
    const d = state.draft;
    d.prompt_review.reviewed = true;
    d.prompt_review.verdict = 'case_incoherent';
    d.prompt_review.reviewed_at = new Date().toISOString();
    saveDraft();
    if (state.submitting) return;
    state.submitting = true;
    try {
      await api('/submissions', { method: 'POST', body: buildSubmissionPayload() });
      clearDraft(d.task_id);
      stopTimer();
      toast('Case flagged as inconsistent. Loading the next task', 'success');
      renderEvalView();
    } catch (e) {
      if (e.status !== 401) toast('Could not flag the case: ' + e.message, 'error');
    } finally {
      state.submitting = false;
    }
  }

  // ─── Voice dictation mic (Speed Optimization §4) ────────────────────────────
  // Reusable mic button: tap → MediaRecorder capture → POST /transcribe → the
  // transcript is APPENDED to the field (still editable). Fields stay plain
  // textareas (no keystroke interception), so the Wispr Flow desktop app keeps
  // working everywhere; this in-app mic is a secondary convenience that degrades
  // to typing when no STT provider is configured. Returns null when the browser
  // has no recording support (the field simply has no mic).
  function micButton(getVal, setVal) {
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder)) return null;
    let recorder = null, chunks = [], stream = null;
    const btn = h('button', {
      class: 'asc-mic-btn', type: 'button',
      title: 'Dictate into this field (tap to start/stop)',
      'aria-label': 'Dictate into this field',
    });
    btn.addEventListener('click', async () => {
      if (recorder && recorder.state === 'recording') { recorder.stop(); return; }
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (e) { toast('Microphone unavailable. Check browser permissions.', 'error'); return; }
      chunks = [];
      try {
        recorder = new MediaRecorder(stream);
      } catch (e) {
        stream.getTracks().forEach((t) => t.stop());
        toast('Recording is not supported in this browser.', 'error');
        return;
      }
      recorder.addEventListener('dataavailable', (e) => { if (e.data && e.data.size) chunks.push(e.data); });
      recorder.addEventListener('stop', async () => {
        stream.getTracks().forEach((t) => t.stop());
        btn.classList.remove('recording');
        btn.textContent = '…'; btn.disabled = true;
        const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
        const fd = new FormData();
        fd.append('file', blob, 'dictation.webm');
        try {
          const res = await api('/transcribe', { method: 'POST', body: fd, isForm: true });
          const text = (res.text || '').trim();
          if (text) {
            const cur = (getVal() || '').trim();
            setVal(cur ? cur + ' ' + text : text);
          } else {
            // Provider succeeded but heard nothing; tell the doctor rather than
            // silently doing nothing (the reported "mic opens, nothing happens").
            toast('No speech detected. Tap the mic and try again.', 'info');
          }
        } catch (e) {
          if (e.status === 503) toast('Dictation is not configured. Type instead (or use the Wispr Flow app).', 'info');
          else if (e.status !== 401) toast('Transcription failed: ' + e.message, 'error');
        } finally {
          btn.textContent = ''; btn.disabled = false;
          btn.setAttribute('aria-label', 'Dictate into this field');
          btn.title = 'Dictate into this field (tap to start/stop)';
        }
      });
      recorder.start();
      btn.classList.add('recording');
      btn.textContent = '■';
      btn.setAttribute('aria-label', 'Listening, tap to stop dictation');
      btn.title = 'Listening… tap to stop';
    });
    return btn;
  }

  // Wrap a textarea/input with its mic in one row. setVal writes the transcript
  // to the field AND fires its input handler so the draft stays in sync, then
  // focuses the field with the cursor at the end so the inserted text is visible
  // and immediately editable (the fix for "mic opens but text isn't written",
  // WS7). Dictation is an assisted-flow feature (V2 + V3); V1 returns the field
  // unchanged (plain textarea, so the Wispr desktop app still works).
  function withMic(field) {
    if (!isAssisted()) return field;
    const mic = micButton(
      () => field.value,
      (v) => {
        field.value = v;
        field.dispatchEvent(new Event('input', { bubbles: true }));
        try {
          field.focus();
          const n = field.value.length;
          if (field.setSelectionRange) field.setSelectionRange(n, n);
        } catch (e) { /* ignore */ }
      },
    );
    if (!mic) return field;
    return h('div', { class: 'asc-mic-row' }, field, mic);
  }

  // ─── Stage 2: blind independent capture ─────────────────────────────────────
  // Three capture modes (cheapest → richest), by portal version + task mode:
  //   V3 (seamless)  → INSTINCT: a ~10s single-line "gut check" (Seamless PRD WS1)
  //   V2 (assisted)  → STANCE: a 30–45s quick take (Speed Optimization §1)
  //   V1 / full task → FULL: the long-form blind ideal answer
  // All are the anti-anchoring guard, committed BEFORE the A/B answers are
  // revealed. The gold SFT answer stays the refined chosen answer (instinct and
  // stance ride the record as a lightweight context field, never gold).
  function renderIndependentAnswer() {
    const ia = state.draft.independent_answer;
    const taskFull = (state.task.independent_mode || 'stance') === 'full';
    const fullMode = !isAssisted() || taskFull;       // V1, or any assisted 'full' task
    const instinctMode = !fullMode && isV3();          // V3 (non-full) → 10s one-liner
    // The instinct one-liner is a single-line input with a soft ~140-char shape
    // (hard-capped at 200 so it stays one line); stance/full use a textarea.
    const field = instinctMode
      ? h('input', { class: 'asc-input asc-instinct-input', type: 'text', maxlength: '200',
          autocomplete: 'off',
          placeholder: 'e.g., continue reduced-dose metformin · recheck eGFR 3 mo · watch lactic acidosis',
          value: ia.text || '' })
      : fullMode
        ? h('textarea', { class: 'asc-textarea', style: 'min-height:200px',
            placeholder: 'Write your full ideal answer to this prompt…' }, ia.text || '')
        : h('textarea', { class: 'asc-textarea', style: 'min-height:90px',
            placeholder: 'Your quick take: key points you\'d expect (bullets are fine). e.g. continue reduced-dose metformin · recheck eGFR 3 mo · watch for lactic acidosis.' }, ia.text || '');
    const revealBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', id: 'ascRevealBtn', onClick: commitIndependentAnswerAndReveal }, 'Reveal AI answers →');
    const hint = h('span', { class: 'asc-submit-hint', id: 'ascRevealHint' });
    // Soft length cue for the instinct one-liner (guidance, not a gate).
    const counter = instinctMode ? h('span', { class: 'asc-instinct-count', id: 'ascInstinctCount' }) : null;
    const syncReveal = () => {
      const val = (ia.text || '').trim();
      const ok = val.length > 0;
      revealBtn.disabled = !ok;
      hint.textContent = ok ? '' : (instinctMode ? 'add your one-line gut check to continue'
        : fullMode ? 'write your answer to continue' : 'jot your quick take to continue');
      if (counter) counter.textContent = val.length > 140 ? 'keep it to one line' : '';
    };
    field.addEventListener('input', () => { ia.text = field.value; saveDraft(); syncReveal(); });
    if (instinctMode) {
      field.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (ia.text || '').trim()) { e.preventDefault(); commitIndependentAnswerAndReveal(); }
      });
    }

    const card = h('div', { class: 'asc-card asc-card-pad asc-gate' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:6px' },
        instinctMode ? 'Quick gut check: in one line, what\'s the crux of the right answer?'
          : fullMode ? 'Before you see the AI answers, write your ideal answer'
          : 'Before you see the answers, your quick take'),
      h('p', { class: 'asc-help', style: 'margin-bottom:16px' },
        instinctMode
          ? '~10 seconds. This commits your instinct before the A/B answers can anchor you; your refined chosen answer later is the gold.'
          : fullMode
            ? 'This is captured uncontaminated: your own gold answer, before the A/B answers can anchor your judgment.'
            : 'A few key points, captured before the A/B answers can anchor your judgment. 30–45 seconds is plenty; your refined chosen answer later is the gold answer.'),
      h('div', { class: 'asc-field', dataset: { tour: 'instinct-field' } }, withMic(field), counter),
      renderAnchorBlock(ia.evidence_anchor, { label: 'citation for your answer', required: false }),
      h('div', { class: 'asc-gate-reveal' }, hint, revealBtn));
    setTimeout(syncReveal, 0);
    // Auto-focus the instinct one-liner so the doctor can type immediately (~10s target).
    if (instinctMode) setTimeout(() => { try { field.focus(); } catch (e) { /* ignore */ } }, 30);
    return card;
  }

  function mergeAnswers(answers) {
    const byId = {};
    (answers || []).forEach((a) => { byId[a.id] = a.text; });
    (state.task.candidate_answers || []).forEach((c) => { if (byId[c.id] != null) c.text = byId[c.id]; });
  }

  // Commit the blind independent answer server-side and reveal the AI answers in
  // one gated step (v2 anti-peeking). This is the ONLY way to obtain the answer
  // text under withholding; the server records the independent answer as
  // pre-reveal and treats it as authoritative at packaging.
  async function revealAnswers() {
    const ia = state.draft.independent_answer;
    // Tutorial: same non-empty-instinct rule, but against the virtual practice
    // case (no independent_commits row is written server-side).
    if (tutorialActive()) {
      const res = await api('/tutorial/reveal', {
        method: 'POST', body: { text: (ia.text || '').trim() },
      });
      mergeAnswers(res.answers);
      return;
    }
    const res = await api('/tasks/' + state.draft.task_id + '/reveal', {
      method: 'POST',
      body: {
        text: (ia.text || '').trim(),
        evidence_anchor: cleanAnchor(ia.evidence_anchor),
        // Multi-anchor (BUG-3b): the committed answer is authoritative at
        // packaging, so send the full citation list here too.
        evidence_anchors: anchorsForSubmit(ia.evidence_anchor),
        // Pins the flow server-side: V1 commits a full blind ideal answer,
        // V2 a stance (unless the task is premium/eval full-mode).
        portal_version: draftVersion(),
      },
    });
    mergeAnswers(res.answers);
  }

  // Re-fetch the answer text when resuming into the compare stage (e.g. a refresh)
  // and the withheld texts aren't loaded. Re-commits idempotently via reveal.
  async function loadWithheldAnswersIfNeeded() {
    const task = state.task;
    if (!task) return;
    if (!(task.candidate_answers || []).some((c) => c.text == null)) return;
    await revealAnswers();
  }

  async function commitIndependentAnswerAndReveal() {
    const d = state.draft;
    if (!(d.independent_answer.text || '').trim()) return;
    // Re-entrancy guard: V3's Enter-to-reveal can fire again while the reveal POST
    // is in flight (the disabled button doesn't gate the keydown path). Without
    // this a second Enter would double-POST /reveal and race two workspace
    // re-renders on the same draft.
    if (state._revealing) return;
    state._revealing = true;
    const btn = document.getElementById('ascRevealBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Revealing…'; }
    try {
      await revealAnswers();
    } catch (e) {
      state._revealing = false;
      if (btn) { btn.disabled = false; btn.textContent = 'Reveal AI answers →'; }
      if (e.status !== 401) toast('Could not reveal the AI answers: ' + e.message, 'error');
      return;  // stay on Stage 2 rather than reveal blank answers
    }
    d.independent_answer.captured_at = new Date().toISOString();
    d.stage = 'compare';
    saveDraft();
    state._revealing = false;
    renderTaskWorkspace();
  }

  function renderAnswerCard(c, diff, assist) {
    // Error spans (from the prelabel suggestion) only ever highlight inside the
    // suggested-weaker answer, and never in full-text mode (nothing decorated).
    const errSpans = (!state.showFullText && assist && assist.suggested_weaker === c.id)
      ? (assist.error_spans || []) : [];
    let body;
    if (diff && diff[c.id]) {
      body = h('div', { class: 'asc-answer-body asc-answer-diff' });
      diff[c.id].sents.forEach((s, i) => body.appendChild(sentenceNode(s, diff[c.id].shared[i], errSpans)));
    } else if (errSpans.length) {
      // No usable sentence diff, but the error highlight is still valuable.
      body = appendTextWithMarks(h('div', { class: 'asc-answer-body' }), c.text || '', errSpans);
    } else {
      body = h('div', { class: 'asc-answer-body' }, c.text || '');
    }
    return h('div', { class: 'asc-answer', dataset: { id: c.id } },
      h('div', { class: 'asc-answer-head' },
        h('div', { class: 'asc-answer-tag' },
          h('span', { class: 'asc-answer-letter', dataset: { letter: c.id } }, c.id),
          'Model ' + c.id),
      ),
      body);
  }

  function verdictButton(verdict, label, key, isBoth) {
    return h('button', {
      class: 'asc-verdict-btn' + (isBoth ? ' both' : '') + (state.draft.verdict === verdict ? ' active' : ''),
      dataset: { verdict },
      onClick: () => selectVerdict(verdict),
    },
      h('span', {}, label),
      h('span', { class: 'asc-verdict-kbd' }, 'key ' + key));
  }

  function selectVerdict(verdict) {
    // Never switch verdicts while a submit is in flight; on V3/V4 the switch
    // resets the staged flow and would tear down the live submit progress UI
    // (and desync the draft from the posted payload).
    if (state.submitting) return;
    const d = state.draft;
    const prevChosen = d.chosen_id;
    d.verdict = verdict;
    if (verdict === 'A_better') { d.chosen_id = 'A'; d.rejected_id = 'B'; }
    else if (verdict === 'B_better') { d.chosen_id = 'B'; d.rejected_id = 'A'; }
    else { d.chosen_id = null; d.rejected_id = null; }
    // If the chosen side changed, reset the revised text so it pre-fills fresh,
    // and DROP the chosen-path reasoning steps: they were split/graded against the
    // previous answer and must never ship attached to the new one. Clearing the
    // once-per-task split guard lets the new chosen answer auto-split fresh.
    if (d.chosen_id !== prevChosen) {
      d.chosen_revision.revised_text = null;
      d.reasoning_steps = [];
      state.splitAttemptedFor = null;
      // §1: sections completed against the previous answer must not survive it;
      // the staged flow restarts at refine (data the doctor typed is kept).
      if (isV3()) resetStagedFlow();
    }
    saveDraft();
    // Update verdict button states
    const vc = document.getElementById('ascVerdicts');
    if (vc) Array.from(vc.children).forEach((b) => {
      b.classList.toggle('active', b.dataset.verdict === verdict);
    });
    refreshAnswerHighlight();
    renderRationale();
    updateSubmitState();
    // V3 (Seamless PRD WS1): AI suggestions were withheld until the clinician
    // committed a verdict; now that one exists, fetch + reveal them for the
    // "confirm/adjust" step. loadAssist is idempotent and a no-op for V2/V1.
    if (isV3() && verdict) loadAssist();
  }

  function refreshAnswerHighlight() {
    const ac = document.getElementById('ascAnswers');
    if (!ac) return;
    Array.from(ac.children).forEach((card) => {
      card.classList.remove('is-chosen', 'is-rejected');
      const id = card.dataset.id;
      if (state.draft.chosen_id === id) card.classList.add('is-chosen');
      else if (state.draft.rejected_id === id) card.classList.add('is-rejected');
    });
  }

  // ─── Rationale ───────────────────────────────────────────────────────────
  // V1/V2: the original render-everything rationale, untouched.
  // V3/V4 (§1): a substage-gated renderer, one active section at a time,
  // completed sections collapse to re-openable summary chips, upcoming sections
  // simply do not exist yet.
  function renderRationale() {
    const container = document.getElementById('ascRationale');
    if (!container) return;
    clear(container);
    const d = state.draft;
    if (!d.verdict) { updateHeaderProgress(); return; }

    if (!isV3()) {
      const box = h('div', { class: 'asc-rationale', style: 'margin-top:20px' });
      if (d.verdict === 'A_better' || d.verdict === 'B_better') {
        box.appendChild(renderChosenCard());
        box.appendChild(renderRejectedCard());
        if (state.task.capture_reasoning) box.appendChild(renderStepsCard(false));
      } else if (d.verdict === 'both_inadequate') {
        box.appendChild(renderFromScratchCard());
        box.appendChild(renderStepsCard(true));
      }
      container.appendChild(box);
      return;
    }

    const list = compareSubstages().slice(1); // 'compare' is the verdict card above
    const cur = currentSubstage();
    if (d.substage !== cur) { d.substage = cur; saveDraft(); }
    const curIdx = cur === 'done' ? list.length : list.indexOf(cur);
    const box = h('div', { class: 'asc-rationale asc-staged', style: 'margin-top:20px' });
    list.forEach((key, i) => {
      if (i > curIdx) return; // §1: the next section does not exist until its turn
      const isCurrent = key === cur;
      if (!isCurrent && key !== 'confidence' && substageComplete(key)
          && state._reopenedSubstage !== key) {
        box.appendChild(renderSubstageSummary(key));
      } else {
        box.appendChild(renderSubstageSection(key));
      }
    });
    container.appendChild(box);
    // Scroll the freshly-mounted section under the sticky chrome when the flow
    // advanced (not on every repaint).
    if (state._lastSubstage !== cur) {
      const target = cur === 'done' ? 'confidence' : cur;
      state._lastSubstage = cur;
      setTimeout(() => scrollToSubstage(target), 40);
    }
    updateHeaderProgress();
  }

  // One-line summary chip for a completed section (§1); keeps context without
  // the control surface; click to re-open.
  function substageSummaryText(key) {
    const d = state.draft;
    switch (key) {
      case 'refine':
        return 'Refined answer (' + d.chosen_id + ')' + (d.chosen_revision.edited ? ' · edited' : ' · kept as-is');
      case 'why_better': {
        const tags = (d.chosen_revision.why_better_tags || []).map((t) => t.replace(/_/g, ' '));
        return 'Why better: ' + (tags.slice(0, 3).join(', ') + (tags.length > 3 ? ' +' + (tags.length - 3) : ''));
      }
      case 'citations': {
        const n = anchorsForSubmit(d.chosen_revision.evidence_anchor).length;
        return n ? ('Sources: ' + n + ' cited') : 'Sources reviewed';
      }
      case 'critique_rejected': {
        const tags = (d.rejected_critique.error_tags || []).map((t) => t.replace(/_/g, ' '));
        return 'Rejected (' + d.rejected_id + '): ' + tags.slice(0, 3).join(', ')
          + (tags.length > 3 ? ' +' + (tags.length - 3) : '');
      }
      case 'from_scratch': return 'Ideal answer written';
      case 'reasoning': {
        const n = activeSteps().filter((s) => (s.text || '').trim()).length;
        return 'Reasoning: ' + n + ' step' + (n === 1 ? '' : 's') + ' reviewed';
      }
      case 'rubric': {
        const n = (d.rubric || []).filter((c) => (c.text || '').trim()).length;
        return n ? ('Scoring guide: ' + n + ' criteria') : 'Scoring guide: none';
      }
      default: return (SUBSTAGE_META[key] || {}).title || key;
    }
  }

  function renderSubstageSummary(key) {
    const n = compareSubstages().indexOf(key) + 1;
    return h('button', {
      class: 'asc-substage-chip', type: 'button', dataset: { substage: key },
      title: 'Re-open this section',
      onClick: () => {
        state._reopenedSubstage = key;
        renderRationale();
        setTimeout(() => scrollToSubstage(key), 40);
      },
    },
      h('span', { class: 'asc-substage-chip-step' }, String(n)),
      h('span', { class: 'asc-substage-chip-check', 'aria-hidden': 'true' }, '✓'),
      h('span', { class: 'asc-substage-chip-label' }, substageSummaryText(key)),
      h('span', { class: 'asc-substage-chip-edit' }, 'edit'));
  }

  function renderSubstageSection(key) {
    let el;
    switch (key) {
      case 'refine': el = renderRefineSection(); break;
      case 'why_better': el = renderWhyBetterSection(); break;
      case 'citations': el = renderCitationsSection(); break;
      case 'critique_rejected': el = renderCritiqueSection(); break;
      case 'from_scratch': el = renderFromScratchSection(); break;
      case 'reasoning': el = renderReasoningSection(); break;
      case 'rubric': el = renderRubricSection(); break;
      case 'confidence': el = renderConfidenceSection(); break;
      default: el = h('div', {});
    }
    // A completed section that was re-opened gets a collapse control back to
    // its summary chip.
    if (substageComplete(key) && state._reopenedSubstage === key) {
      el.appendChild(h('div', { style: 'margin-top:12px' },
        h('button', {
          class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
          onClick: () => { state._reopenedSubstage = null; refreshStagedFlow(); },
        }, '✓ Done, collapse')));
    }
    return el;
  }

  // Shared footer for a section: a hint + the single primary action.
  function sectionActions(hint, btn) {
    return h('div', { class: 'asc-substage-actions' }, hint, btn);
  }

  // ─── §9 Refine the winning answer ───────────────────────────────────────────
  function renderRefineSection() {
    const d = state.draft;
    const rev = d.chosen_revision;
    const original = chosenText();
    const ta = h('textarea', { class: 'asc-textarea asc-v3-editor', style: 'min-height:46vh' },
      rev.revised_text != null ? rev.revised_text : original);
    const editDiff = h('div', { class: 'asc-editdiff-wrap' });
    editDiff.setAttribute('hidden', '');
    const editDiffToggle = h('button', {
      class: 'asc-btn-link', type: 'button', style: 'margin-top:6px',
      onClick: () => {
        if (editDiff.hasAttribute('hidden')) {
          clear(editDiff);
          editDiff.appendChild(renderEditDiff(original, ta.value));
          editDiff.removeAttribute('hidden');
          editDiffToggle.textContent = 'Hide changes';
        } else {
          editDiff.setAttribute('hidden', '');
          editDiffToggle.textContent = '⬍ Show what you changed';
        }
      },
    }, '⬍ Show what you changed');
    ta.addEventListener('input', () => {
      rev.revised_text = ta.value;
      rev.edited = ta.value !== original;
      saveDraft();
      if (!editDiff.hasAttribute('hidden')) {
        clear(editDiff);
        editDiff.appendChild(renderEditDiff(original, ta.value));
      }
    });
    // §9: ONE primary button. Saving with no edits is the "looks proper" path.
    const saveBtn = h('button', {
      class: 'asc-btn asc-btn-primary asc-btn-lg', type: 'button',
      onClick: () => {
        rev.revised_text = ta.value;
        rev.edited = ta.value !== original;
        d.refine_saved = true;
        state._reopenedSubstage = null;
        refreshStagedFlow();
      },
    }, 'Save changes →');
    return sectionCard('refine', null,
      h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
        'Edit the stronger answer into what a correct answer should say. If it’s already right, just save.'),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Refined answer (' + d.chosen_id + ') ',
          h('span', { class: 'asc-label-hint' }, 'edits become the gold revision; original is preserved')),
        withMic(ta), editDiffToggle, editDiff),
      sectionActions(null, saveBtn));
  }

  // ─── §10 Why it's better (required) ─────────────────────────────────────────
  function renderWhyBetterSection() {
    const d = state.draft;
    const rev = d.chosen_revision;
    const notes = h('textarea', {
      class: 'asc-textarea', style: 'min-height:64px',
      placeholder: 'e.g. correctly continues decongestion despite the creatinine rise',
    }, rev.why_better_notes || '');
    const hint = h('span', { class: 'asc-submit-hint' });
    const contBtn = h('button', {
      class: 'asc-btn asc-btn-primary', type: 'button',
      onClick: () => {
        if (!whyBetterConditionsMet()) return;
        d.why_better_done = true;
        state._reopenedSubstage = null;
        refreshStagedFlow();
      },
    }, 'Continue →');
    const sync = () => {
      const ok = whyBetterConditionsMet();
      contBtn.disabled = !ok;
      hint.textContent = ok ? '' : 'write one line and tag ≥1 reason to continue';
      // A re-opened section at the confidence substage must gate the mounted
      // submit button live (no-op while #ascSubmit isn't mounted).
      updateSubmitState();
    };
    notes.addEventListener('input', () => { rev.why_better_notes = notes.value; saveDraft(); sync(); });
    const chips = renderChips((state.taxonomy.why_better_tags || []), rev.why_better_tags, (tag, on) => {
      toggleInArray(rev.why_better_tags, tag, on);
      saveDraft();
      sync();
    });
    setTimeout(sync, 0);
    return sectionCard('why_better',
      infoDot('Why it’s better', [
        'Capture the reason the answer you chose is clinically stronger. This is the preference signal a model learns from.',
        'Write one clear sentence, then tag the reasons that apply.',
      ]),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Why is this answer better?'),
        withMic(notes)),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Why-better tags ',
          infoDot('Why-better tags', [
            'Tag the dimensions on which it’s better.',
            'Pick every reason that applies: accuracy, completeness, safety, reasoning.',
          ])),
        chips),
      sectionActions(hint, contBtn));
  }

  // ─── §11 Citations (reviewed, not blocking unless grounding is required) ──
  function renderCitationsSection() {
    const d = state.draft;
    const rev = d.chosen_revision;
    const required = (state.task.grounding_mode === 'required');
    const cite = renderCiteSuggest(
      rev.evidence_anchor,
      () => ((rev.revised_text != null ? rev.revised_text : chosenText()) + ' ' + (rev.why_better_notes || '')),
      () => renderRationale());
    const anchorBlock = renderAnchorBlock(rev.evidence_anchor, {
      label: 'citation for this rationale',
      required,
      prominent: true,
    });
    const hint = h('span', { class: 'asc-submit-hint' });
    const contBtn = h('button', {
      class: 'asc-btn asc-btn-primary', type: 'button',
      onClick: () => {
        if (required && !isValidAnchor(rev.evidence_anchor)) return;
        d.citations_reviewed = true;
        state._reopenedSubstage = null;
        refreshStagedFlow();
      },
    }, 'Continue →');
    const sync = () => {
      const ok = !required || isValidAnchor(rev.evidence_anchor);
      contBtn.disabled = !ok;
      hint.textContent = ok ? '' : 'this task requires a citation: attach one to continue';
    };
    const card = sectionCard('citations',
      infoDot('Citations', [
        'Cite the guideline or trial your judgment rests on.',
        'Search the library, open the source to check it, then it’s attached.',
      ]),
      h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
        required
          ? 'This task requires evidence: attach the source your judgment rests on.'
          : 'Not every case needs one. Open a suggested source to attach it, or continue.'),
      cite,
      anchorBlock,
      sectionActions(hint, contBtn));
    card.addEventListener('input', () => setTimeout(sync, 0));
    card.addEventListener('click', () => setTimeout(sync, 0));
    setTimeout(sync, 0);
    return card;
  }

  // ─── §12 Critique the rejected answer: popover-per-tag, no model hints ─────
  function closeTagPopover() {
    // The popover is portaled to <body> and positioned from the anchor's rect,
    // so it cannot follow a scroll on its own; drop the tracker first.
    if (state._tagPopTrack) {
      window.removeEventListener('scroll', state._tagPopTrack, true);
      window.removeEventListener('resize', state._tagPopTrack);
      state._tagPopTrack = null;
    }
    if (state._tagPopAnchor && state._tagPopAnchor.isConnected) state._tagPopAnchor.focus();
    state._tagPopAnchor = null;
    if (state._tagPop) {
      state._tagPop.remove();
      state._tagPop = null;
    }
    // Always detach the listeners. A re-render can tear the popover out of the
    // DOM without going through here, leaving only the listeners behind.
    document.removeEventListener('keydown', _tagPopKey, true);
    if (state._tagPopDocClick) {
      document.removeEventListener('click', state._tagPopDocClick, true);
      state._tagPopDocClick = null;
    }
  }
  function _tagPopKey(e) { if (e.key === 'Escape') closeTagPopover(); }

  // Position the portaled popover against its anchor chip. An absolutely
  // positioned popover is clipped by any ancestor with overflow != visible
  // (.asc-answer, .asc-subcard, .asc-case-rail all qualify), and z-index does
  // nothing about clipping — so the popover lives in <body> and is placed from
  // the chip's viewport rect instead.
  function positionTagPopover() {
    const pop = state._tagPop, anchor = state._tagPopAnchor;
    if (!pop || !anchor || !anchor.isConnected) return closeTagPopover();
    const M = 10;
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth, hgt = pop.offsetHeight;

    // FLIP: below by default; above when the bottom would overflow AND there is
    // more room up there. Never pick a side that clips.
    const below = window.innerHeight - r.bottom, above = r.top;
    const flip = below < hgt + M && above > below;
    const top = flip ? r.top - hgt - 6 : r.bottom + 6;

    // SHIFT: stay on screen horizontally without leaving the anchor.
    const left = Math.min(Math.max(r.left, M), window.innerWidth - w - M);

    pop.style.top = Math.max(M, Math.min(top, window.innerHeight - hgt - M)) + 'px';
    pop.style.left = left + 'px';
    pop.classList.toggle('is-flipped', flip);
  }

  function renderCritiqueSection() {
    const d = state.draft;
    const crit = d.rejected_critique;
    const errorTags = (state.taxonomy.error_tags || []);
    const hint = h('span', { class: 'asc-submit-hint' });
    const contBtn = h('button', {
      class: 'asc-btn asc-btn-primary', type: 'button',
      onClick: () => {
        if (!critiqueConditionsMet()) return;
        closeTagPopover();
        d.critique_done = true;
        state._reopenedSubstage = null;
        refreshStagedFlow();
      },
    }, 'Continue →');
    const critiqueHint = () => {
      if (!(crit.error_tags || []).length) return 'tag at least one error to continue';
      for (const t of crit.error_tags) {
        if (!crit.severities[t]) return 'tap “' + t.replace(/_/g, ' ') + '” to set how serious it is';
      }
      if (!(crit.why_worse || '').trim()) return 'write one line on why it’s worse';
      return 'tag how it failed (failure modes) to continue';
    };
    const sync = () => {
      const ok = critiqueConditionsMet();
      contBtn.disabled = !ok;
      hint.textContent = ok ? '' : critiqueHint();
      // Keep a mounted submit button honest when this section is re-opened
      // and edited at the confidence substage.
      updateSubmitState();
    };

    // Error-tag chips: tap to select → a small popover captures THIS tag's
    // "why" + severity → the chip collapses to "✓ tag · severity" (§12).
    const chipsWrap = h('div', { class: 'asc-errtag-wrap' });
    const chipsRow = h('div', { class: 'asc-chips', style: 'position:relative' });
    chipsWrap.appendChild(chipsRow);

    function openTagPopover(tag, chipEl) {
      closeTagPopover();
      const reasons = (state.taxonomy.error_tag_reasons || []);
      const sevs = (state.taxonomy.error_severities || ['low', 'medium', 'high']);
      const pop = h('div', {
        class: 'asc-tag-pop', role: 'dialog', 'aria-modal': 'false',
        'aria-label': 'Detail this error',
      });
      pop.appendChild(h('div', { class: 'asc-tag-pop-title' },
        h('span', {}, tag.replace(/_/g, ' ')),
        h('button', {
          class: 'asc-tag-pop-x', type: 'button', 'aria-label': 'Close',
          onClick: () => { closeTagPopover(); paintChips(); sync(); },
        }, '×')));
      if (reasons.length) {
        const rRow = h('div', { class: 'asc-sev-pills asc-reason-pills' });
        reasons.forEach((r) => {
          const b = h('button', {
            class: 'asc-sev-pill' + (crit.error_tag_reasons[tag] === r ? ' active' : ''),
            type: 'button',
            onClick: () => {
              if (crit.error_tag_reasons[tag] === r) delete crit.error_tag_reasons[tag];
              else crit.error_tag_reasons[tag] = r;
              saveDraft();
              Array.from(rRow.children).forEach((x) => x.classList.toggle('active', x.textContent === r.replace(/_/g, ' ') && crit.error_tag_reasons[tag] === r));
            },
          }, r.replace(/_/g, ' '));
          rRow.appendChild(b);
        });
        pop.appendChild(h('div', { class: 'asc-tag-pop-field' },
          h('div', { class: 'asc-label' }, 'Why?'), rRow));
      }
      const sRow = h('div', { class: 'asc-sev-pills' });
      // Done just collapses the panel; it is never disabled, so a reviewer can
      // always close the popover. The substage still cannot advance until a
      // severity is set, because critiqueConditionsMet() gates the flow.
      const doneBtn = h('button', {
        class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button',
        onClick: () => { closeTagPopover(); paintChips(); sync(); },
      }, 'Done');
      sevs.forEach((sv) => {
        const b = h('button', {
          class: 'asc-sev-pill' + (crit.severities[tag] === sv ? ' active' : ''),
          type: 'button',
          onClick: () => {
            crit.severities[tag] = sv;
            saveDraft();
            Array.from(sRow.children).forEach((x) => x.classList.toggle('active', x.textContent === sv));
            // Repaint the chip so it shows "✓ tag · severity" immediately, even
            // if the reviewer then dismisses the popover by clicking outside it.
            paintChips();
            sync();
          },
        }, sv);
        sRow.appendChild(b);
      });
      pop.appendChild(h('div', { class: 'asc-tag-pop-field' },
        h('div', { class: 'asc-label' }, 'How serious?'), sRow));
      pop.appendChild(h('div', { class: 'asc-tag-pop-foot' },
        h('button', {
          class: 'asc-btn-link', type: 'button', style: 'color:var(--asc-danger)',
          onClick: () => {
            toggleInArray(crit.error_tags, tag, false);
            delete crit.severities[tag]; delete crit.error_tag_anchors[tag]; delete crit.error_tag_reasons[tag];
            saveDraft(); closeTagPopover(); paintChips(); sync();
          },
        }, 'Remove tag'),
        doneBtn));
      // Portal it: nothing in the card tree can clip what is not in the card tree.
      document.body.appendChild(pop);
      state._tagPop = pop;
      state._tagPopAnchor = chipEl;
      positionTagPopover();

      // A fixed element does not follow a scrolling anchor, so track it. Capture
      // phase catches nested scroll containers; passive so we never block the
      // scroll we are following.
      state._tagPopTrack = () => positionTagPopover();
      window.addEventListener('scroll', state._tagPopTrack, { capture: true, passive: true });
      window.addEventListener('resize', state._tagPopTrack, { passive: true });
      const firstBtn = pop.querySelector('button');
      if (firstBtn) firstBtn.focus();
      document.addEventListener('keydown', _tagPopKey, true);
      // Click anywhere outside the popover (and outside the chip that opened it)
      // closes it, the missing dismissal that used to trap reviewers.
      const onDocClick = (e) => {
        if (state._tagPop && !state._tagPop.contains(e.target)
            && e.target !== chipEl && !chipEl.contains(e.target)) {
          closeTagPopover();
        }
      };
      state._tagPopDocClick = onDocClick;
      document.addEventListener('click', onDocClick, true);
    }

    function paintChips() {
      // The popover is portaled to <body>, never a child of the row, so the row
      // holds nothing but chips and a plain clear-and-append is correct.
      Array.from(chipsRow.children).forEach((c) => c.remove());
      errorTags.forEach((tag) => {
        const on = crit.error_tags.indexOf(tag) !== -1;
        const sev = crit.severities[tag];
        const chip = h('button', {
          class: 'asc-chip err' + (on ? ' active' : ''), type: 'button',
          onClick: (e) => {
            if (!on) {
              toggleInArray(crit.error_tags, tag, true);
              saveDraft(); paintChips(); sync();
              const fresh = Array.from(chipsRow.children).find((c) => c.dataset && c.dataset.tag === tag);
              openTagPopover(tag, fresh || e.currentTarget);
            } else {
              openTagPopover(tag, e.currentTarget);
            }
          },
          dataset: { tag },
        },
          on && sev ? ('✓ ' + tag.replace(/_/g, ' ') + ' · ' + sev)
            : on ? ('✓ ' + tag.replace(/_/g, ' ') + ' · tap to detail')
              : tag.replace(/_/g, ' '));
        chipsRow.appendChild(chip);
      });
    }
    paintChips();

    const whyWorse = h('input', {
      class: 'asc-input',
      placeholder: 'One line on the key problem…',
      value: crit.why_worse || '',
    });
    whyWorse.addEventListener('input', () => { crit.why_worse = whyWorse.value; saveDraft(); sync(); });

    // §D failure-mode taxonomy chips (baseline pairs), physician-picked, kept.
    const failureField = h('div', {});
    if (isBaselinePair() && (state.taxonomy.failure_modes || []).length) {
      const fmContainer = h('div', { id: 'ascFailureModes' });
      failureField.appendChild(h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'How did it fail? ',
          h('span', { class: 'asc-label-hint' }, '(model-failure taxonomy, select all that apply)')),
        fmContainer));
      renderFailureTags(fmContainer);
      fmContainer.addEventListener('click', () => setTimeout(sync, 0));
    }

    // Optional per-error citation, lightweight, behind a disclosure (§12).
    const anchorContainer = h('div', { id: 'ascTagAnchors' });
    const citeDisclosure = h('div', { class: 'asc-disclosure' },
      h('div', { style: 'display:inline-flex;align-items:center;gap:6px' },
        discloseToggle('+ cite specific errors', anchorContainer),
        infoDot('Cite specific errors', [
          'Optionally point the error at a source.',
          'Open a guideline that shows why this is wrong.',
        ])));
    renderTagAnchors(anchorContainer, true);

    setTimeout(sync, 0);
    return sectionCard('critique_rejected',
      infoDot('Critique the rejected answer', [
        'Mark why the rejected answer is worse.',
        'Pick the error tags that apply; tap a tag to add why and how serious.',
      ]),
      h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
        'Rejected answer: Model ' + d.rejected_id + '. Pick the error tags yourself; no model hints here.'),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Error tags ',
          h('span', { class: 'asc-label-hint' }, '(tap a tag to add why + severity)')),
        chipsWrap),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Why is it worse?'),
        withMic(whyWorse)),
      failureField,
      citeDisclosure,
      anchorContainer,
      sectionActions(hint, contBtn));
  }

  // ─── Both-inadequate: compose the ideal answer ──────────────────────────────
  function renderFromScratchSection() {
    const d = state.draft;
    const hint = h('span', { class: 'asc-submit-hint' });
    const saveBtn = h('button', {
      class: 'asc-btn asc-btn-primary asc-btn-lg', type: 'button',
      onClick: () => {
        if (!(d.from_scratch.ideal_answer || '').trim()) return;
        d.from_scratch_saved = true;
        state._reopenedSubstage = null;
        refreshStagedFlow();
      },
    }, 'Save & continue →');
    const sync = () => {
      const ok = !!(d.from_scratch.ideal_answer || '').trim();
      saveBtn.disabled = !ok;
      hint.textContent = ok ? '' : 'write the ideal answer to continue';
    };
    const card = sectionCard('from_scratch', null,
      renderFromScratchCard(),
      sectionActions(hint, saveBtn));
    card.addEventListener('input', () => setTimeout(sync, 0));
    setTimeout(sync, 0);
    return card;
  }

  // ─── §13 Check the reasoning: one step open at a time, free-text "what's off"
  function renderReasoningSection() {
    const d = state.draft;
    const forBoth = d.verdict === 'both_inadequate';
    const listId = 'ascStepsList';
    const canAutoSplit = !forBoth;

    const addBtn = h('button', {
      class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
      onClick: () => {
        activeSteps().push(newAuthoredStep());
        state._openStep = activeSteps().length - 1;
        saveDraft(); renderStepsListV3(listId); updateSubmitState();
      },
    }, '+ Add step');
    // §5: no "Re-split from answer". The split already runs automatically on
    // mount (below): a button for something that already happened is a
    // decision the physician has to make about nothing.

    const hint = h('span', { class: 'asc-submit-hint', id: 'ascStepsContHint' });
    const contBtn = h('button', {
      class: 'asc-btn asc-btn-primary', type: 'button', id: 'ascStepsCont',
      onClick: () => {
        if (!reasoningConditionsMet()) return;
        d.reasoning_done = true;
        state._reopenedSubstage = null;
        refreshStagedFlow();
      },
    }, 'Continue →');

    const card = sectionCard('reasoning',
      infoDot('Check the reasoning', [
        'Review the step-by-step reasoning behind the answer.',
        'Open any step that’s off and say what’s wrong. We handle the rest.',
      ]),
      h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
        forBoth ? 'Optionally lay out the reasoning steps behind your ideal answer.'
          : 'Confirm each step, or open it and say what’s off, one step at a time.'),
      h('div', { class: 'asc-steps', id: listId }),
      h('div', { style: 'margin-top:12px;display:flex;gap:8px;flex-wrap:wrap' }, addBtn),
      sectionActions(hint, contBtn));

    setTimeout(() => {
      renderStepsListV3(listId);
      if (canAutoSplit && activeSteps().length === 0
          && state.splitAttemptedFor !== state.task.task_id && !state.splitting) {
        state.splitAttemptedFor = state.task.task_id;
        autoSplitChosen(listId, false);
      }
    }, 0);
    return card;
  }

  // Keep the Continue affordance honest as steps change.
  function syncStepsCont() {
    const btn = document.getElementById('ascStepsCont');
    const hint = document.getElementById('ascStepsContHint');
    if (!btn) return;
    const sr = stepsReview();
    const ok = reasoningConditionsMet();
    // Never allow Continue while the auto-split is in flight; steps landing
    // after the section completed would silently regress the flow.
    btn.disabled = state.splitting || !ok;
    if (hint) {
      hint.textContent = state.splitting ? 'splitting the answer into steps…'
        : ok ? ''
          : !sr.ok
            ? (sr.reasons.indexOf('missing_correction_reason') !== -1
              ? 'say what’s off with each edited step'
              : 'review each step: confirm it, or open it and correct it')
            : 'this task requires a citation on each step: open a step to attach one';
    }
  }

  // The status pill for one step. Hoisted out of the list renderer so a single
  // repainted row (§7) derives its pill identically to a full list render :
  // two copies of this would drift.
  function stepStatusOf(s) {
    if (s.added) return { text: 'added', cls: 'added' };
    if (s.corrected) {
      return (s.step_note || '').trim()
        ? { text: 'corrected ✎', cls: 'corrected' }
        : { text: 'corrected: say what’s off', cls: 'corrected' };
    }
    if (s.confirmed) return { text: 'confirmed ✓', cls: 'confirmed' };
    return { text: 'pending', cls: 'pending' };
  }

  // §5: three placeholder rows while the auto-split is in flight. A blank card
  // reads as "nothing here"; a spinner asks the physician to interpret it.
  function stepsSkeleton() {
    return h('div', { class: 'asc-steps-skeleton', 'aria-hidden': 'true' },
      h('div', {}), h('div', {}), h('div', {}));
  }

  // §7: a scroll-anchor CORRECTION, not a navigation. `_base.css` sets
  // `html { scroll-behavior: smooth }`, which would make this animate: turning
  // the jolt into a slower visible glide, which is the same defect wearing a
  // nicer coat. Suspending the property beats `behavior: 'instant'`: an engine
  // that doesn't know that enum value throws on the dictionary conversion.
  function scrollByInstant(dy) {
    if (!dy) return;
    const root = document.documentElement;
    const prev = root.style.scrollBehavior;
    root.style.scrollBehavior = 'auto';
    window.scrollBy(0, dy);
    root.style.scrollBehavior = prev;
  }

  // §7: rebuild exactly ONE step row in place. `renderStepsListV3` clears and
  // rebuilds every row, which collapses the page height and makes the browser
  // drop its scroll anchor: that is the jolt on steps 4–7. An open/close is a
  // change to at most two rows, so repaint at most two.
  function repaintStepRow(idx, listId) {
    if (idx == null || idx < 0) return;
    const list = document.getElementById(listId || 'ascStepsList');
    if (!list) return;
    const node = list.querySelector('[data-step-idx="' + idx + '"]');
    const s = activeSteps()[idx];
    if (!node || !s) return;
    list.replaceChild(buildStepRowV3(s, idx, list.id), node);
  }

  // V3/V4 steps list (§13): single-open accordion; an edited step captures a
  // free-text ``step_note`` (the server derives the error-tag classification);
  // NO tag picker and NO citation block inside step editing.
  //
  // §7: keep this for GENUINE list changes (add, insert, remove, re-split).
  // Never call it for a state toggle: use `repaintStepRow` instead.
  function renderStepsListV3(listId) {
    const list = document.getElementById(listId);
    if (!list) return;
    clear(list);
    const steps = activeSteps();
    if (state.splitting) {
      list.appendChild(stepsSkeleton());
      return;
    }
    if (!steps.length) {
      // §5: no "Re-split from answer" to point at any more: the split runs
      // automatically on mount, so the only manual route is adding a step.
      list.appendChild(h('p', { class: 'asc-help' }, 'No steps yet: add steps manually.'));
      syncStepsCont();
      return;
    }

    // Bulk confirm for model-passed, untouched steps (kept from the pre-graded
    // flow; reading then one tap is still an explicit endorsement).
    const untouchedGood = steps.filter((s) => s.suggested_label === 'good' && !s.confirmed
      && !s.corrected && !s.added && (s.text || '').trim() === (s.original_text || '').trim());
    if (untouchedGood.length > 1) {
      const bulkbar = h('div', { class: 'asc-step-bulkbar' },
        h('span', { class: 'asc-step-bulk-label' },
          untouchedGood.length + ' steps look correct to the model. Read them, then confirm in one tap.'),
        h('button', {
          class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
          onClick: () => {
            const touched = untouchedGood.map((s) => steps.indexOf(s));
            untouchedGood.forEach((s) => setStepConfirmed(s, true));
            saveDraft();
            // §7 again: repaint only the rows this actually changed, then drop
            // the bar itself. A full rebuild here would jolt the page exactly
            // as the per-row toggle used to.
            touched.forEach((i) => repaintStepRow(i, listId));
            if (bulkbar.parentNode) bulkbar.parentNode.removeChild(bulkbar);
            syncStepsCont(); updateSubmitState();
          },
        }, '✓ Confirm all correct'));
      list.appendChild(bulkbar);
    }

    steps.forEach((s, idx) => list.appendChild(buildStepRowV3(s, idx, listId)));
    syncStepsCont();
  }

  // One V3/V4 step row, collapsed or expanded. Returns a detached node so both
  // the full list render and the single-row repaint (§7) go through one path.
  function buildStepRowV3(s, idx, listId) {
    // Grounding-required tasks keep the per-step citation editor (§13's "no
    // citation block in step editing" yields here: the server hard-gates a
    // valid anchor on every step, so removing the UI would dead-end submit).
    const groundingRequired = (state.task.grounding_mode === 'required');
    s.step = idx + 1;
    const open = state._openStep === idx;
    const st = stepStatusOf(s);
    const flaggedBadge = (s.suggested_label === 'bad')
      ? h('span', { class: 'asc-step-suggest bad', title: 'Model pre-grade: verify and confirm or correct' }, 'model · flags this')
      : (s.suggested_label === 'good')
        ? h('span', { class: 'asc-step-suggest good', title: 'Model pre-grade: your confirmation is the label' }, 'model · looks correct')
        : null;

    // Repaint just this row's status pill after an in-place mutation.
    const repaintPill = () => {
      const pill = row.querySelector('.asc-step-status');
      const st2 = stepStatusOf(s);
      if (pill) { pill.textContent = st2.text; pill.className = 'asc-step-status ' + st2.cls; }
    };

    const confirmBtn = h('button', {
      class: 'asc-btn asc-btn-ghost asc-btn-sm asc-step-confirm' + (s.confirmed ? ' active' : ''),
      type: 'button', hidden: s.corrected || s.added,
      onClick: (e) => {
        e.stopPropagation();
        const wasOpen = state._openStep === idx;
        setStepConfirmed(s, !s.confirmed);
        // Confirming an open row collapses it: that IS a structural change to
        // this row, so it needs a rebuild (of this row only).
        const collapsing = s.confirmed && wasOpen;
        if (collapsing) state._openStep = null;
        saveDraft();
        if (collapsing) {
          repaintStepRow(idx, listId);
        } else {
          // §7 surgical update: three properties change: the button's state
          // and label, the row's confirmed class, and the status pill.
          confirmBtn.classList.toggle('active', !!s.confirmed);
          confirmBtn.textContent = s.confirmed ? '✓ Confirmed' : '✓ Correct as-is';
          row.classList.toggle('is-confirmed', !!s.confirmed);
          repaintPill();
        }
        syncStepsCont();
        updateSubmitState();
      },
    }, s.confirmed ? '✓ Confirmed' : '✓ Correct as-is');

    const head = h('div', { class: 'asc-step-head' },
      h('div', { style: 'display:flex;align-items:center;gap:8px;min-width:0;flex-wrap:wrap' },
        h('span', { class: 'asc-step-num' }, 'Step ' + (idx + 1)),
        flaggedBadge,
        h('span', { class: 'asc-step-status ' + st.cls }, st.text)),
      h('div', { style: 'display:flex;align-items:center;gap:8px' },
        confirmBtn,
        h('button', {
          // §6: one action, one label. "Has this been touched?" is already
          // carried by the status pill beside it, and "Edit" names the
          // physician's intent where "Open" named the widget's mechanics.
          class: 'asc-btn-link', type: 'button', 'aria-expanded': String(open),
          onClick: () => {
            const prev = state._openStep;
            state._openStep = open ? null : idx;
            saveDraft();
            // Anchor on the clicked row: the row above it may change height
            // when it collapses, and without this the clicked row slides out
            // from under the cursor.
            const anchor = row.getBoundingClientRect().top;
            if (prev != null && prev !== idx) repaintStepRow(prev, listId);
            repaintStepRow(idx, listId);
            const list = document.getElementById(listId || 'ascStepsList');
            const after = list && list.querySelector('[data-step-idx="' + idx + '"]');
            if (after) scrollByInstant(after.getBoundingClientRect().top - anchor);
          },
        }, open ? 'Close' : 'Edit')));

    const row = h('div', {
      class: 'asc-step' + (open ? '' : ' asc-step-collapsed') + (s.confirmed ? ' is-confirmed' : ''),
      // The handle `repaintStepRow` finds the row by: index alone is not
      // enough, since the bulk bar is also a child of the list.
      dataset: { stepIdx: String(idx) },
    }, head);

    if (!open) {
      row.appendChild(h('div', { class: 'asc-step-collapsed-text' }, s.text || ''));
      if (groundingRequired && (s.text || '').trim() && !isValidAnchor(s.evidence_anchor)) {
        row.appendChild(h('div', { class: 'asc-anchor-valid asc-anchor-invalid', style: 'margin-top:4px' },
          '· citation needed: open the step to attach one'));
      }
      return row;
    }

    // Expanded editor: the step text + the single free-text "what's off" box.
    const ta = h('textarea', { class: 'asc-textarea', placeholder: 'Describe this reasoning step…' }, s.text || '');
    const noteWrap = h('div', { class: 'asc-field', style: 'margin-top:8px' });
    const note = h('input', {
      class: 'asc-input',
      placeholder: 'e.g. treats the creatinine bump as intrinsic AKI: it’s decongestion-related hemoconcentration',
      value: s.step_note || '',
    });
    note.addEventListener('input', () => {
      s.step_note = note.value;
      if ((s.step_note || '').trim()) {
        // The server derives the error-tag classification from this note
        // (step_note → step_error_tag); the physician never picks a tag.
        s.label = 'bad'; s.step_reward = 0; s.correction_reason = null;
      } else {
        s.label = null; s.step_reward = null;
      }
      saveDraft(); syncStepsCont(); updateSubmitState();
      repaintPill();
    });
    noteWrap.appendChild(h('label', { class: 'asc-label' }, 'What’s off with this step?'));
    noteWrap.appendChild(withMic(note));
    noteWrap.hidden = !s.corrected;

    const hasOriginal = s.original_text != null;
    // §8: the FULL original goes into the DOM; CSS ellipsises it at the true
    // edge of the bar. The old 80-char JS cut was right at exactly one width
    // and stopped a third of the way across a desktop card.
    const originalBox = hasOriginal
      ? h('details', { class: 'asc-step-original', hidden: !s.corrected },
          h('summary', { class: 'asc-step-original-sum' },
            h('span', { class: 'asc-step-original-tag' }, 'original'),
            h('span', { class: 'asc-step-original-preview' }, s.original_text || '')),
          h('div', { class: 'asc-step-original-full' }, s.original_text || ''))
      : null;

    const suggestHint = (s.suggested_label === 'bad' && s.suggested_critique)
      ? h('div', { class: 'asc-step-suggest-hint' }, 'Model: ' + s.suggested_critique)
      : null;

    ta.addEventListener('input', () => {
      s.text = ta.value;
      if (hasOriginal) {
        if (ta.value.trim() !== (s.original_text || '').trim()) {
          if (!s.corrected) { s.corrected = true; s.confirmed = false; }
        } else {
          s.corrected = false; s.confirmed = false; s.correction_reason = null;
          s.label = null; s.step_reward = null;
        }
        // The confirm button is hidden for a corrected step; keep it honest
        // without rebuilding the row (which would blur the textarea mid-type).
        confirmBtn.hidden = !!(s.corrected || s.added);
        confirmBtn.classList.toggle('active', !!s.confirmed);
        confirmBtn.textContent = s.confirmed ? '✓ Confirmed' : '✓ Correct as-is';
        row.classList.toggle('is-confirmed', !!s.confirmed);
      }
      noteWrap.hidden = !s.corrected;
      if (originalBox) originalBox.hidden = !s.corrected;
      repaintPill();
      saveDraft(); syncStepsCont(); updateSubmitState();
    });

    const rowActions = h('div', { style: 'margin-top:8px;display:flex;gap:10px' },
      h('button', {
        class: 'asc-btn-link', type: 'button',
        onClick: () => {
          activeSteps().splice(idx + 1, 0, newAuthoredStep());
          state._openStep = idx + 1;
          // A genuine list change: the indices below this row all shift, so
          // the full renderer is correct here.
          saveDraft(); renderStepsListV3(listId); updateSubmitState();
        },
      }, '+ insert below'),
      h('button', {
        class: 'asc-btn-link', type: 'button', style: 'color:var(--asc-danger)',
        onClick: () => {
          activeSteps().splice(idx, 1);
          state._openStep = null;
          saveDraft(); renderStepsListV3(listId); updateSubmitState();
        },
      }, 'Remove'));

    // Per-step citation editor ONLY when the task requires grounding (see
    // groundingRequired above): everywhere else §13 keeps step editing lean.
    let anchorBlock = null;
    if (groundingRequired) {
      if (!s.evidence_anchor) s.evidence_anchor = emptyAnchor();
      anchorBlock = renderAnchorBlock(s.evidence_anchor, { label: 'citation for this step', required: true });
      // Keep the section's Continue honest as the anchor fields are filled.
      anchorBlock.addEventListener('input', () => setTimeout(() => { syncStepsCont(); updateSubmitState(); }, 0));
      anchorBlock.addEventListener('change', () => setTimeout(() => { syncStepsCont(); updateSubmitState(); }, 0));
    }
    appendChildren(row, [suggestHint, ta, noteWrap, originalBox, anchorBlock, rowActions]);
    return row;
  }

  // ─── §15 Confidence + submit: mounts ONLY at the confidence substage ───────
  function renderConfidenceSection() {
    const d = state.draft;
    const confLevels = (state.taxonomy.confidence_levels || ['low', 'medium', 'high']);
    const confPills = h('div', { class: 'asc-conf-pills', id: 'ascConf' });
    confLevels.forEach((lvl) => {
      confPills.appendChild(h('button', {
        // Unset until the doctor actively picks; the draft default is never
        // presented as a pre-made choice.
        class: 'asc-conf-pill' + (d.confidence_set && d.confidence === lvl ? ' active' : ''),
        type: 'button', dataset: { conf: lvl },
        onClick: () => {
          d.confidence = lvl;
          d.confidence_set = true;
          saveDraft();
          Array.from(confPills.children).forEach((b) => b.classList.toggle('active', b.dataset.conf === lvl));
          updateSubmitState();
          updateHeaderProgress();
        },
      }, lvl));
    });
    const submitBtn = h('button', {
      class: 'asc-btn asc-btn-primary asc-btn-lg', id: 'ascSubmit', onClick: submitEvaluation,
    }, 'Submit evaluation');
    const hint = h('span', { class: 'asc-submit-hint', id: 'ascSubmitHint' });
    const confidenceCard = sectionCard('confidence', null,
      confPills,
      h('div', { class: 'asc-submit-row' }, hint, submitBtn));
    setTimeout(updateSubmitState, 0);
    // The decisive-action capture is OPTIONAL, so it lives in its own card ABOVE the
    // confidence + submit gate: never wedged into the commit moment (Audit §13).
    const decisive = renderDecisiveActionCard();
    return decisive ? h('div', {}, decisive, confidenceCard) : confidenceCard;
  }

  // Decisive action (Audit §13): the physician-named verifiable outcome: the test or
  // action the correct answer depends on. Naming it turns a preference label into a
  // checkable reward. Its own clearly-OPTIONAL card (not a numbered required step, and
  // not jammed into the submit gate); a physician who can't name one leaves it blank,
  // and a fabricated decisive action is worse than none.
  function renderDecisiveActionCard() {
    const d = state.draft;
    if (!isV3()) return null;   // V3/V4 evaluation screen only
    d.decisive_action = d.decisive_action || { action: '', tool_name: '', rationale: '' };
    const da = d.decisive_action;

    // Primary field: one free-text question, auto-growing so a long answer never clips.
    const action = autoGrow(h('textarea', {
      class: 'asc-textarea',
      placeholder: 'e.g. order pronase-digested paraffin immunofluorescence',
    }, da.action || ''));
    action.addEventListener('input', () => { da.action = action.value; saveDraft(); });

    // Secondary field behind progressive disclosure: the default view is one clean
    // question, not two stacked inputs. Auto-expanded when a resumed draft has a value.
    const toolInput = h('input', { class: 'asc-input', placeholder: 'e.g. order_pronase_if',
      value: da.tool_name || '' });
    toolInput.addEventListener('input', () => { da.tool_name = toolInput.value; saveDraft(); });
    const toolField = h('div', { class: 'asc-field', style: 'margin-top:12px;margin-bottom:0' },
      h('label', { class: 'asc-label' }, 'Tool or order name ',
        h('span', { class: 'asc-label-hint' }, 'optional')),
      toolInput);
    const addTool = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
      type: 'button', style: 'margin-top:10px' }, '+ Add a tool or order name');
    const toolSlot = h('div', {});
    const paintTool = (shown) => {
      clear(toolSlot);
      toolSlot.appendChild(shown ? toolField : addTool);
    };
    addTool.addEventListener('click', () => { paintTool(true); setTimeout(() => toolInput.focus(), 0); });
    paintTool(!!(da.tool_name || '').trim());

    const info = infoDot('Why we ask', [
      'If you name the decisive step, we can verify a model actually ordered it before '
      + 'answering: turning this preference label into a checkable reward.',
      'Skip it when no single step decides the answer. A made-up decisive action is '
      + 'worse than none.',
    ]);

    return h('div', { class: 'asc-card asc-card-pad asc-substage' },
      h('div', { class: 'asc-substage-head' },
        h('div', { class: 'asc-substage-step asc-substage-step--optional' }, 'Optional'),
        h('div', { class: 'asc-substage-title' }, 'Decisive action', info)),
      h('div', { class: 'asc-field', style: 'margin-bottom:0' },
        h('label', { class: 'asc-label' },
          'Which test or action, if skipped, makes the correct answer unreachable?'),
        h('div', { class: 'asc-help', style: 'margin-bottom:8px' },
          'Free text. Leave blank if none applies.'),
        action),
      toolSlot);
  }

  // ─── §14 Rubric: build the scoring guide, one criterion at a time ──────────
  // V3/V4 only (the rubric never rendered on V1/V2). Replaces the dense
  // all-criteria list with a focused wizard: one criterion per card, a pinned
  // copy of the revised answer for reference, silent auto-seeding from the
  // doctor's tags, and plain-language weights (numeric bands live in the
  // info-dot, not the primary copy).
  const TIER_DEFAULT_PTS = { critical: 9, important: 5, helpful: 2 };
  // §10: "Must-have" and "Important" are synonyms in ordinary English: nothing
  // in the words says which outranks the other, so the physician had to learn an
  // arbitrary mapping. Critical > Major > Minor is a severity scale clinicians
  // already rank without thinking. The explanations state CONSEQUENCE ("the
  // answer is wrong without this"), which is checkable, rather than priority,
  // which is an opinion. The tier KEYS are unchanged, so `tierForPoints`,
  // `TIER_DEFAULT_PTS`, the backend, and every stored record keep working:
  // this is a label-only change.
  const TIER_CHOICES = [
    ['critical', 'Critical', 'the answer is wrong or unsafe without this'],
    ['important', 'Major', 'the answer is clearly worse without this'],
    ['helpful', 'Minor', 'a refinement: good, not decisive'],
  ];
  // §11: "axis" is ML vocabulary a clinician has no reason to know. The enum
  // keys are the wire format and never change; these are what the physician
  // reads. [label, explanation]: the explanation is the button's title.
  const AXIS_LABELS = {
    accuracy: ['Got the facts right', 'values, doses, findings are correct'],
    completeness: ['Didn’t miss anything', 'nothing decisive left out'],
    safety: ['Safe for the patient', 'no harmful action or omission'],
    reasoning: ['Sound reasoning', 'the logic actually follows'],
    grounding: ['Backed by evidence', 'guideline or literature support'],
    communication: ['Clearly explained', 'a colleague could act on it'],
  };
  // Every criterion carries `axes` (a list). `axis` is mirrored from `axes[0]`
  // for backward compatibility with stored records and the V2 path. A criterion
  // always has at least one axis.
  function criterionAxes(c) {
    if (!c) return ['accuracy'];
    if (!Array.isArray(c.axes) || !c.axes.length) c.axes = c.axis ? [c.axis] : ['accuracy'];
    c.axis = c.axes[0];
    return c.axes;
  }

  // 14.4: auto-growing textarea (min 2 rows); the full criterion text is always
  // visible and editable; nothing clips.
  function autoGrow(ta) {
    const fit = () => { ta.style.height = 'auto'; ta.style.height = Math.max(ta.scrollHeight, 52) + 'px'; };
    ta.addEventListener('input', fit);
    setTimeout(fit, 0);
    return ta;
  }

  // Repaint the live rubric section after an async seed lands, never while the
  // doctor is typing in it (would steal focus).
  function repaintRubricUI() {
    const rub = document.querySelector('[data-substage="rubric"]');
    if (!rub) return;
    const active = document.activeElement;
    const typing = active && rub.contains(active)
      && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT' || active.tagName === 'SELECT');
    if (!typing) renderRationale();
  }

  function renderRubricSection() {
    const d = state.draft;
    // 14.5: SILENT auto-seed. First mount seeds from the doctor's tags; if the
    // tags changed since the last seed (a re-opened earlier section), reseed
    // additively in the background. No prompt, no reseed button, ever.
    const tagHash = JSON.stringify([
      (d.chosen_revision.why_better_tags || []).slice().sort(),
      (d.rejected_critique.error_tags || []).slice().sort(),
    ]);
    if (!d.rubricSeeded) {
      d.rubricSeedHash = tagHash;
      saveDraft();
      seedRubric(false);
    } else if (d.rubricSeedHash !== tagHash) {
      d.rubricSeedHash = tagHash;
      saveDraft();
      seedRubric(true);
    }

    const crits = d.rubric;
    const cursor = Math.max(0, Math.min(d.rubricCursor || 0, crits.length));

    // 14.2: the physician's revised answer, pinned + collapsible, so scoring
    // never requires scrolling back up.
    const refText = d.verdict === 'both_inadequate'
      ? (d.from_scratch.ideal_answer || '')
      : chosenRefinedText();
    const pinned = h('details', { class: 'asc-rubric-pin', open: '' },
      h('summary', {}, 'Your revised answer (reference)'),
      h('div', { class: 'asc-rubric-pin-body' }, refText));

    const body = h('div', { id: 'ascRubricWizard' });
    // While a seed request is in flight and nothing exists yet, hold the wizard
    // on a placeholder, never flash the empty finish card (a fast "Save &
    // finish" there would complete the section, only for the seeded criteria to
    // land moments later and honestly-but-jarringly regress it).
    if (state._rubricSeeding && !crits.length) {
      body.appendChild(h('p', { class: 'asc-help' }, 'Drafting starting criteria from your tags…'));
    } else if (cursor < crits.length) {
      body.appendChild(renderRubricCriterionCard(crits, cursor));
    } else {
      body.appendChild(renderRubricFinishCard(crits));
    }

    return sectionCard('rubric',
      infoDot('Build the scoring guide', [
        'Weights: critical / major / minor map to high / medium / low points; a “must never” auto-fails the answer.',
        'Confirm or edit each drafted criterion, then add your own if something is missing.',
      ]),
      // 14.1: layman's description: no numeric tiers in the primary copy.
      // §12: the phrase in the UI is "must never" (two words, no hyphen), so
      // the copy that names it has to match what the physician actually taps.
      h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
        'List what a correct answer must get right and what it must never do. Each item is weighted by how much it matters. Name at least one ',
        h('strong', {}, 'must never'),
        ': the single thing that makes an answer wrong no matter what.'),
      pinned,
      body);
  }

  // 14.3: one focused criterion card: sentence, type, weight, axis, cite.
  function renderRubricCriterionCard(crits, i) {
    const d = state.draft;
    const c = crits[i];
    const card = h('div', { class: 'asc-rubric-focus' });
    card.appendChild(h('div', { class: 'asc-rubric-progress' },
      'Criterion ' + (i + 1) + ' of ' + crits.length));

    const ta = autoGrow(h('textarea', {
      class: 'asc-textarea asc-rubric-ta', rows: '2',
      placeholder: 'e.g. accounts for the still-congested state (JVP 12, 3+ edema, weight barely down)',
    }, c.text || ''));
    const specChip = h('span', { class: 'asc-rubric-spec' });
    const paintSpec = () => {
      const sp = isSpecificText(c.text);
      c.specific = sp;
      const keyTier = tierForPoints(c.points) !== 'helpful';
      specChip.textContent = sp ? 'specific' : 'vague';
      specChip.className = 'asc-rubric-spec ' + (sp ? 'ok' : (keyTier ? 'warn' : 'soft'));
      specChip.title = sp
        ? 'Machine-checkable: names a concrete fact/drug/dose/threshold.'
        : 'Name the specific fact, drug, dose, or threshold so the grader can check it.';
    };
    ta.addEventListener('input', () => { c.text = ta.value; paintSpec(); saveDraft(); updateSubmitState(); });

    const tierRow = h('div', { class: 'asc-rubric-tier-row' });
    const ptsLabel = h('span', { class: 'asc-rubric-pts' });
    const slider = h('input', {
      type: 'range', min: '1', max: '10', step: '1',
      'aria-label': 'How many points this criterion is worth',
    });
    const autoFail = h('span', {
      class: 'asc-badge asc-badge-amber', hidden: true,
      title: 'A “must never”: the grader hard-fails an answer that does this.',
    }, 'auto-fail ✓');

    const mag = () => Math.max(1, Math.abs(Number(c.points) || 5));
    const neg = () => (Number(c.points) || 0) < 0;
    // §9: the polarity toggle IS the sentence stem: "A correct answer [must
    // never] give thrombolytics in dissection" reads as one sentence rather
    // than as two form fields. It still sets the sign of `c.points`, which is
    // what drives auto-fail and the grader's hard-fail, so no data is lost.
    // Pink for “must never” (the flag/critical accent); lime for “must”.
    const stemToggle = h('button', {
      class: 'asc-rubric-stem-toggle', type: 'button',
      'aria-label': 'Switch between must and must never',
      onClick: () => { setPoints(mag(), !neg()); ta.focus(); },
    }, 'must');
    function paintAll() {
      slider.value = String(mag());
      ptsLabel.textContent = (neg() ? '−' : '+') + mag();
      ptsLabel.className = 'asc-rubric-pts ' + (neg() ? 'neg' : 'pos');
      stemToggle.textContent = neg() ? 'must never' : 'must';
      stemToggle.className = 'asc-rubric-stem-toggle ' + (neg() ? 'neg' : 'pos');
      stemToggle.title = neg()
        ? 'This is a “must never”: switch to “must” if the answer is required to do it.'
        : 'This is a “must”: switch to “must never” if the answer is required NOT to do it.';
      Array.from(tierRow.children).forEach((b) => b.classList.toggle('active', b.dataset.tier === tierForPoints(c.points)));
      autoFail.hidden = !(neg() && tierForPoints(c.points) === 'critical');
      paintSpec();
    }
    const setPoints = (magnitude, negative) => {
      c.points = (negative ? -1 : 1) * Math.max(1, Math.min(10, magnitude));
      c.tier = tierForPoints(c.points);
      c.critical = c.tier === 'critical';
      paintAll();
      saveDraft();
      updateSubmitState();
    };
    TIER_CHOICES.forEach(([tier, label, expl]) => {
      tierRow.appendChild(h('button', {
        class: 'asc-rubric-tier-btn', type: 'button', dataset: { tier }, title: expl,
        onClick: () => setPoints(TIER_DEFAULT_PTS[tier], neg()),
      },
        h('span', { class: 'asc-rubric-tier-name' }, label),
        h('span', { class: 'asc-rubric-tier-expl' }, expl)));
    });
    slider.addEventListener('input', () => setPoints(parseInt(slider.value, 10) || 1, neg()));

    // §11: MULTI-select, in plain words. A criterion routinely scores on more
    // than one axis ("must never give thrombolytics in dissection" is safety
    // AND accuracy), and forcing a single pick discards a distinction the buyer
    // is paying for. `c.axes` is authoritative; `c.axis` mirrors `axes[0]` so
    // stored records and the V2/backend single-value path keep working.
    const axes = (state.taxonomy.rubric_axes
      || ['accuracy', 'completeness', 'safety', 'reasoning', 'grounding', 'communication']);
    criterionAxes(c);
    const axisRow = h('div', { class: 'asc-sev-pills asc-axis-row' });
    axes.forEach((ax) => {
      const [label, expl] = AXIS_LABELS[ax] || [ax, ''];
      axisRow.appendChild(h('button', {
        class: 'asc-sev-pill' + (c.axes.indexOf(ax) !== -1 ? ' active' : ''),
        type: 'button', title: expl, 'aria-pressed': String(c.axes.indexOf(ax) !== -1),
        onClick: (e) => {
          const on = c.axes.indexOf(ax) !== -1;
          if (on && c.axes.length === 1) return;   // a criterion always has ≥1 axis
          c.axes = on ? c.axes.filter((x) => x !== ax) : c.axes.concat([ax]);
          c.axis = c.axes[0];                      // legacy single-value mirror
          e.currentTarget.classList.toggle('active', !on);
          e.currentTarget.setAttribute('aria-pressed', String(!on));
          saveDraft();
          // The premium/axis-coverage readout on the finish card counts axes.
          updateSubmitState();
        },
      }, label));
    });

    const citeArea = h('div', { class: 'asc-rubric-cite', hidden: 'hidden' });
    const citeBtn = h('button', {
      class: 'asc-btn-link', type: 'button',
      onClick: () => {
        if (!c.evidence_anchor) c.evidence_anchor = emptyAnchor();
        if (citeArea.hasAttribute('hidden')) {
          clear(citeArea);
          citeArea.appendChild(renderAnchorBlock(c.evidence_anchor,
            { label: 'citation for this criterion', required: false }));
          citeArea.removeAttribute('hidden');
        } else {
          citeArea.setAttribute('hidden', '');
        }
      },
    }, '+ cite (optional)');
    const removeBtn = h('button', {
      class: 'asc-btn-link', type: 'button', style: 'color:var(--asc-danger)',
      onClick: () => { crits.splice(i, 1); saveDraft(); renderRationale(); },
    }, 'remove this criterion');

    const back = h('button', {
      class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button', disabled: i === 0,
      onClick: () => { d.rubricCursor = i - 1; saveDraft(); renderRationale(); },
    }, '← Back');
    const next = h('button', {
      class: 'asc-btn asc-btn-primary', type: 'button',
      onClick: () => { c.text = ta.value; d.rubricCursor = i + 1; saveDraft(); renderRationale(); },
    }, 'Next →');

    // §9: the stem replaces the "Type" field entirely: the sentence already
    // answered the question the field was asking.
    card.appendChild(h('div', { class: 'asc-field' },
      h('div', { class: 'asc-rubric-stem' },
        h('span', { class: 'asc-rubric-stem-lead' }, 'A correct answer'),
        stemToggle,
        specChip),
      ta));
    card.appendChild(h('div', { class: 'asc-field' },
      // §10: the scale sits BESIDE the question, not below the choices: the
      // tier buttons and the slider are two ways to set ONE value, and the
      // readout is what makes that obvious.
      h('div', { class: 'asc-rubric-matter-head' },
        h('label', { class: 'asc-label' }, 'How much does it matter? ',
          infoDot('Weights', [
            'Critical / major / minor map to high / medium / low points.',
            'A “must never” marked critical is the auto-fail: the grader hard-fails on it.',
          ])),
        h('div', { class: 'asc-rubric-scale' }, slider, ptsLabel, autoFail)),
      tierRow));
    card.appendChild(h('div', { class: 'asc-field' },
      // §11: the label states the job and the options explain themselves, so
      // the "Axes" tooltip that used to translate the enum is gone.
      h('label', { class: 'asc-label' }, 'What does this criterion check? ',
        h('span', { class: 'asc-label-hint' }, '(select all that apply)')),
      axisRow));
    card.appendChild(h('div', { style: 'display:flex;gap:14px;margin-top:4px' }, citeBtn, removeBtn));
    card.appendChild(citeArea);
    card.appendChild(h('div', { class: 'asc-substage-actions' }, back, next));
    paintAll();
    return card;
  }

  // The final wizard card: recap + "Add your own (optional)" + Save & finish.
  function renderRubricFinishCard(crits) {
    const d = state.draft;
    const card = h('div', { class: 'asc-rubric-focus' });
    const named = crits.filter((c) => (c.text || '').trim());
    card.appendChild(h('div', { class: 'asc-rubric-progress' },
      named.length ? (named.length + ' criteria in the guide') : 'No criteria yet'));
    if (named.length) {
      const ul = h('ul', { class: 'asc-rubric-recap' });
      named.forEach((c) => {
        const pos = (c.points || 0) >= 0;
        ul.appendChild(h('li', {},
          h('span', { class: 'asc-rubric-pts ' + (pos ? 'pos' : 'neg') }, (pos ? '+' : '') + (c.points || 0)),
          h('span', { class: 'asc-rubric-recap-text' }, c.text),
          h('button', {
            class: 'asc-btn-link', type: 'button',
            onClick: () => { d.rubricCursor = crits.indexOf(c); saveDraft(); renderRationale(); },
          }, 'edit')));
      });
      card.appendChild(ul);
      const rc = rubricCompleteness(d.rubric);
      card.appendChild(h('div', { class: 'asc-rubric-meter-row' },
        h('span', { class: 'asc-rubric-premium ' + (rc.premium ? 'premium' : 'standard') },
          rc.premium ? 'premium' : 'standard'),
        h('span', { class: 'asc-label-hint' }, rc.premium
          ? (rc.n_criteria + ' criteria · ' + rc.n_axes + ' axes, meets the premium bar')
          : ('to reach premium: ' + rc.missing.join('; ')))));
    } else {
      card.appendChild(h('p', { class: 'asc-help' },
        'Add your own criteria below, or finish without a scoring guide.'));
    }
    const addBtn = h('button', {
      class: 'asc-btn asc-btn-subtle', type: 'button',
      onClick: () => {
        d.rubric.push({ text: '', points: 5, axes: ['accuracy'], axis: 'accuracy', source: 'manual' });
        d.rubricCursor = d.rubric.length - 1;
        saveDraft(); renderRationale();
      },
    }, '+ Add your own (optional)');
    const hint = h('span', { class: 'asc-submit-hint' });
    const gate = rubricGate();
    if (!gate.ok) hint.textContent = gate.msg;
    const finish = h('button', {
      class: 'asc-btn asc-btn-primary', type: 'button', disabled: !gate.ok,
      onClick: () => {
        if (!rubricGate().ok) return;
        d.rubric_done = true;
        state._reopenedSubstage = null;
        refreshStagedFlow();
      },
    }, 'Save & finish →');
    const back = h('button', {
      class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button', disabled: !crits.length,
      onClick: () => { d.rubricCursor = Math.max(0, crits.length - 1); saveDraft(); renderRationale(); },
    }, '← Back');
    card.appendChild(h('div', { style: 'margin-top:10px' }, addBtn));
    card.appendChild(h('div', { class: 'asc-substage-actions' }, back, hint, finish));
    return card;
  }

  // 14.5: seeding is SILENT and automatic, invoked on rubric mount and again
  // (additively) whenever the doctor's tags change. Never prompts, never asks.
  async function seedRubric(force) {
    const d = state.draft;
    // Tutorial: /rubric/suggest is task-scoped (404 on the virtual case), and
    // authoring criteria by hand IS the lesson: mark seeded, fall to manual.
    if (tutorialActive()) { d.rubricSeeded = true; updateSubmitState(); return; }
    if (d.rubricSeeded && !force) return;
    d.rubricSeeded = true;
    state._rubricSeeding = true;
    try {
      const res = await api('/rubric/suggest', { method: 'POST', body: buildSubmissionPayload() });
      const seeded = (res && res.criteria) || [];
      if (seeded.length) {
        if (force) {
          // Re-seed: append only criteria not already present (by text).
          const have = new Set(d.rubric.map((c) => (c.text || '').trim().toLowerCase()));
          seeded.forEach((c) => { if (!have.has((c.text || '').trim().toLowerCase())) d.rubric.push(c); });
        } else if (!d.rubric.length) {
          d.rubric = seeded;
        }
        saveDraft();
      }
      updateSubmitState();
    } catch (e) {
      // Seeding is a convenience; never surface an error; the placeholder
      // resolves to the empty wizard below.
    } finally {
      state._rubricSeeding = false;
      // Repaint the LIVE wizard (resilient if the section was rebuilt mid-seed;
      // never repaints over the doctor's typing).
      repaintRubricUI();
    }
  }

  function chosenText() {
    const c = (state.task.candidate_answers || []).find((x) => x.id === state.draft.chosen_id);
    return c ? (c.text || '') : '';
  }

  function renderChosenCard() {
    const d = state.draft;
    const rev = d.chosen_revision;
    const original = chosenText();
    // WS4 (V3): editing the chosen answer into gold is the core high-value action,
    // so give it a large, comfortable surface (was a cramped 120px box).
    const bigEditor = isV3();
    const ta = h('textarea', {
      class: 'asc-textarea' + (bigEditor ? ' asc-v3-editor' : ''),
      style: bigEditor ? 'min-height:46vh' : 'min-height:120px',
    }, rev.revised_text != null ? rev.revised_text : original);
    // WS4 (V3): a collapsible "what you changed" view diffs the revised gold
    // against the original so the doctor sees (and the record captures) their edits.
    const editDiff = h('div', { class: 'asc-editdiff-wrap' });
    editDiff.setAttribute('hidden', '');
    const editDiffToggle = bigEditor ? h('button', {
      class: 'asc-btn-link', type: 'button', style: 'margin-top:6px',
      onClick: () => {
        if (editDiff.hasAttribute('hidden')) {
          clear(editDiff);
          editDiff.appendChild(renderEditDiff(original, ta.value));
          editDiff.removeAttribute('hidden');
          editDiffToggle.textContent = 'Hide changes';
        } else {
          editDiff.setAttribute('hidden', '');
          editDiffToggle.textContent = '⬍ Show what you changed';
        }
      },
    }, '⬍ Show what you changed') : null;
    ta.addEventListener('input', () => {
      rev.revised_text = ta.value;
      rev.edited = ta.value !== original;
      saveDraft();
      // Keep an open diff in sync as the doctor edits.
      if (editDiffToggle && !editDiff.hasAttribute('hidden')) {
        clear(editDiff);
        editDiff.appendChild(renderEditDiff(original, ta.value));
      }
    });

    const notes = h('textarea', { class: 'asc-textarea', placeholder: 'One line on why this answer is better (optional)…' }, rev.why_better_notes || '');
    notes.addEventListener('input', () => { rev.why_better_notes = notes.value; saveDraft(); });
    const notesField = withMic(notes);

    const whyTags = (state.taxonomy.why_better_tags || []);
    const chips = renderChips(whyTags, rev.why_better_tags, (tag, on) => {
      toggleInArray(rev.why_better_tags, tag, on);
      saveDraft();
    });

    // V3 (WS3): auto-suggested citation for this rationale; retrieval keys on
    // the refined answer + the "why it's better" note. Confirming re-renders so
    // the anchor block fills and the record reads as grounded.
    const cite = renderCiteSuggest(
      rev.evidence_anchor,
      () => ((rev.revised_text != null ? rev.revised_text : original) + ' ' + (rev.why_better_notes || '')),
      renderRationale);
    wireCiteSuggest(notes, cite);
    wireCiteSuggest(ta, cite);

    return h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head chosen' }, '✓ Chosen answer (' + d.chosen_id + '): edit to improve'),
      h('div', { class: 'asc-subcard-body' },
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Refined answer ',
            h('span', { class: 'asc-label-hint' }, 'edits become the gold revision; original is preserved')),
          ta, editDiffToggle, editDiff),
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Why it\'s better'),
          notesField),
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Why-better tags ', h('span', { class: 'asc-label-hint' }, '(optional)')),
          chips),
        cite,
        renderAnchorBlock(rev.evidence_anchor, {
          label: 'citation for this rationale',
          required: (state.task.grounding_mode === 'required'),
        }),
      ));
  }

  function renderRejectedCard() {
    const d = state.draft;
    const crit = d.rejected_critique;
    const errorTags = (state.taxonomy.error_tags || []);

    const sevContainer = h('div', { id: 'ascSeverities' });
    const reasonContainer = h('div', { id: 'ascTagReasons' });
    const anchorContainer = h('div', { id: 'ascTagAnchors' });
    const suggestContainer = h('div', { id: 'ascTagSuggest' });

    const chips = renderChips(errorTags, crit.error_tags, (tag, on) => {
      toggleInArray(crit.error_tags, tag, on);
      if (!on) { delete crit.severities[tag]; delete crit.error_tag_anchors[tag]; delete crit.error_tag_reasons[tag]; }
      saveDraft();
      renderTagReasons(reasonContainer);
      renderSeverities(sevContainer);
      renderTagAnchors(anchorContainer);
      renderTagSuggestions(suggestContainer);
    }, 'err');

    const whyWorse = h('input', { class: 'asc-input', placeholder: 'One line on the key problem (optional)…', value: crit.why_worse || '' });
    whyWorse.addEventListener('input', () => { crit.why_worse = whyWorse.value; saveDraft(); });

    // Model-Failure Taxonomy capture (§D-2): failure-mode chips, shown only on V3/V4
    // for a REAL-MODEL (baseline) pair; the taxonomy attributes provider failures, so
    // it's meaningless on a generated pair. Physician-verified; multi-select, ~10s.
    const failureField = h('div', {});
    if (isV3() && isBaselinePair() && (state.taxonomy.failure_modes || []).length) {
      const fmContainer = h('div', { id: 'ascFailureModes' });
      failureField.appendChild(h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'How did it fail? ',
          h('span', { class: 'asc-label-hint' }, '(model-failure taxonomy, select all that apply)')),
        fmContainer));
      renderFailureTags(fmContainer);
    }

    const card = h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head rejected' }, '✕ Rejected answer (' + d.rejected_id + '): what went wrong'),
      h('div', { class: 'asc-subcard-body' },
        suggestContainer,
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Error tags ', h('span', { class: 'asc-label-hint' }, '(select all that apply)')),
          chips),
        reasonContainer,
        sevContainer,
        failureField,
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Why it\'s worse ', h('span', { class: 'asc-label-hint' }, '(optional nuance)')),
          withMic(whyWorse)),
        h('div', { class: 'asc-disclosure' },
          discloseToggle('+ cite specific errors', anchorContainer)),
        anchorContainer,
      ));
    renderTagSuggestions(suggestContainer);
    renderTagReasons(reasonContainer);
    renderSeverities(sevContainer);
    renderTagAnchors(anchorContainer, true);
    return card;
  }

  // §D-2: failure-mode chips + an optional one-line note per selected mode. The chip
  // uses the controlled-vocab id but shows the human label + definition tooltip. Stores
  // ``rejected_critique.failure_tags = [{mode, note}]``.
  function renderFailureTags(container) {
    if (!container) return;
    clear(container);
    const modes = (state.taxonomy.failure_modes || []);
    const tags = state.draft.rejected_critique.failure_tags;
    const selected = {};
    tags.forEach((t) => { selected[t.mode] = true; });
    const chips = h('div', { class: 'asc-chips' });
    modes.forEach((m) => {
      const chip = h('button', {
        class: 'asc-chip asc-chip-failure' + (selected[m.id] ? ' active' : ''),
        type: 'button', title: m.definition,
        onClick: () => {
          const idx = tags.findIndex((t) => t.mode === m.id);
          if (idx === -1) tags.push({ mode: m.id, note: '' });
          else tags.splice(idx, 1);
          saveDraft();
          renderFailureTags(container);
          updateSubmitState();
        },
      }, m.label);
      chips.appendChild(chip);
    });
    container.appendChild(chips);
    tags.forEach((t) => {
      const meta = modes.find((m) => m.id === t.mode);
      const note = h('input', { class: 'asc-input', style: 'margin-top:6px',
        placeholder: (meta ? meta.label : t.mode) + ': one line of specifics (optional)', value: t.note || '' });
      note.addEventListener('input', () => { t.note = note.value; saveDraft(); });
      container.appendChild(note);
    });
  }

  // Model-suggested error tags + draft rationale (Speed Optimization §2):
  // rendered as visually-distinct "Suggested, tap to accept" chips. NOTHING is
  // applied without an explicit tap; accepted values land in the normal editable
  // fields. Suggestions only show on the model's suggested-weaker side.
  // Accepting mutates the draft and re-renders the rationale from state (the
  // renderers own the DOM; no hand-syncing of chip rows or inputs).
  function renderTagSuggestions(container) {
    if (!container) return;
    clear(container);
    const a = assistData();
    const d = state.draft;
    if (!a || d.rejected_id !== a.suggested_weaker) return;
    const crit = d.rejected_critique;
    const pendingTags = (a.suggested_error_tags || []).filter((t) => crit.error_tags.indexOf(t) === -1);
    const rationalePending = (a.suggested_rationale || '').trim() && !(crit.why_worse || '').trim();
    if (!pendingTags.length && !rationalePending) return;

    const box = h('div', { class: 'asc-suggest-box' },
      h('div', { class: 'asc-suggest-label' }, 'Suggested, tap to accept'));
    if (pendingTags.length) {
      const row = h('div', { class: 'asc-chips' });
      pendingTags.forEach((tag) => {
        row.appendChild(h('button', {
          class: 'asc-chip asc-chip-suggest', type: 'button',
          onClick: () => {
            toggleInArray(crit.error_tags, tag, true);
            saveDraft();
            renderRationale();
          },
        }, '+ ' + tag.replace(/_/g, ' ')));
      });
      box.appendChild(row);
    }
    if (rationalePending) {
      box.appendChild(h('div', { class: 'asc-suggest-rationale' },
        h('span', { class: 'asc-suggest-text' }, '“' + a.suggested_rationale + '”'),
        h('button', {
          class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
          onClick: () => {
            crit.why_worse = a.suggested_rationale;
            saveDraft();
            renderRationale();
          },
        }, 'Use as “why it’s worse”')));
    }
    container.appendChild(box);
  }

  // Shared per-error-tag single-select pill rows (used by severities AND the
  // structured reason chips): one row per selected tag, tap toggles the value
  // in ``dict``, re-rendering only its own container.
  function renderPerTagPills(container, opts) {
    if (!container) return;
    clear(container);
    const crit = state.draft.rejected_critique;
    if (!crit.error_tags.length || !(opts.options || []).length) return;
    const wrap = h('div', { class: 'asc-field' },
      h('label', { class: 'asc-label' }, opts.label + ' ', h('span', { class: 'asc-label-hint' }, opts.hint)));
    crit.error_tags.forEach((tag) => {
      const pills = h('div', { class: 'asc-sev-pills' + (opts.pillsClass ? ' ' + opts.pillsClass : '') });
      opts.options.forEach((val) => {
        pills.appendChild(h('button', {
          class: 'asc-sev-pill' + (opts.dict[tag] === val ? ' active' : ''),
          type: 'button',
          onClick: () => {
            if (opts.dict[tag] === val) delete opts.dict[tag];
            else opts.dict[tag] = val;
            saveDraft();
            renderPerTagPills(container, opts);
          },
        }, val.replace(/_/g, ' ')));
      });
      wrap.appendChild(h('div', { class: 'asc-sev-row' },
        h('span', { class: 'asc-sev-name' }, tag.replace(/_/g, ' ')), pills));
    });
    container.appendChild(wrap);
  }

  // Structured-first capture (Speed Optimization §6): one-tap reason chips per
  // selected error tag. The vocabulary comes from the server taxonomy only;
  // a local copy would drift from what validation accepts. V2-only; V1 keeps
  // the classic free-text "why it's worse" as the sole reason input.
  function renderTagReasons(container) {
    if (!isAssisted()) { if (container) clear(container); return; }
    renderPerTagPills(container, {
      label: 'Why, per error',
      hint: '(one tap, optional)',
      options: state.taxonomy.error_tag_reasons || [],
      dict: state.draft.rejected_critique.error_tag_reasons,
      pillsClass: 'asc-reason-pills',
    });
  }

  function renderSeverities(container) {
    renderPerTagPills(container, {
      label: 'Severity per error',
      hint: '(optional)',
      options: (state.taxonomy.error_severities || ['low', 'medium', 'high']),
      dict: state.draft.rejected_critique.severities,
    });
  }

  function renderTagAnchors(container, keepHidden) {
    const wasHidden = keepHidden ? true : container.hasAttribute('hidden');
    clear(container);
    container.className = 'asc-disclosure-body';
    if (wasHidden) container.setAttribute('hidden', '');
    const crit = state.draft.rejected_critique;
    if (!crit.error_tags.length) {
      container.appendChild(h('p', { class: 'asc-help' }, 'Select an error tag above to attach a citation to it.'));
      return;
    }
    crit.error_tags.forEach((tag) => {
      if (!crit.error_tag_anchors[tag]) crit.error_tag_anchors[tag] = emptyAnchor();
      container.appendChild(h('div', { style: 'margin-bottom:12px' },
        h('div', { class: 'asc-label', style: 'margin-bottom:6px' }, tag.replace(/_/g, ' ')),
        anchorFields(crit.error_tag_anchors[tag])));
    });
  }

  function renderFromScratchCard() {
    const fs = state.draft.from_scratch;
    const ideal = h('textarea', { class: 'asc-textarea', style: 'min-height:140px', placeholder: 'Write the ideal expert answer from scratch…' }, fs.ideal_answer || '');
    ideal.addEventListener('input', () => { fs.ideal_answer = ideal.value; saveDraft(); updateSubmitState(); });
    const approach = h('textarea', { class: 'asc-textarea', placeholder: 'Notes on your approach (optional)…' }, fs.approach_notes || '');
    approach.addEventListener('input', () => { fs.approach_notes = approach.value; saveDraft(); });

    // V3 (WS3): auto-suggested citation for the from-scratch ideal answer.
    const cite = renderCiteSuggest(
      fs.evidence_anchor,
      () => ((fs.ideal_answer || '') + ' ' + (fs.approach_notes || '')),
      renderRationale);
    wireCiteSuggest(ideal, cite);
    wireCiteSuggest(approach, cite);

    return h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head' }, '✎ Compose the ideal answer'),
      h('div', { class: 'asc-subcard-body' },
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Ideal answer'),
          ideal),
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Approach notes ', h('span', { class: 'asc-label-hint' }, '(optional)')),
          approach),
        cite,
        renderAnchorBlock(fs.evidence_anchor, {
          label: 'citation for this answer',
          required: (state.task.grounding_mode === 'required'),
        }),
      ));
  }

  // ─── Reasoning steps editor (Edit-to-Correct, Reasoning Capture v2) ────────
  // A split step starts `pending` (text === original_text). The doctor either
  // confirms it as-is (label=good) or edits it to correct it (label derived from
  // a one-tap reason). `original` is the AI's split step; pass null for an
  // authored step the AI omitted (see newAuthoredStep).
  function newStep(text, original, suggested) {
    return {
      step: 0,
      text: text || '',
      original_text: original !== undefined ? original : (text || ''),
      corrected: false, confirmed: false, added: false,
      correction_reason: null,
      // §13 (V3/V4): the free-text "what's off with this step?"; the backend
      // derives step_error_tag (and the correction_reason vocab) from it.
      step_note: '',
      label: null, step_reward: null, critique: '', evidence_anchor: emptyAnchor(),
      // Pre-grade suggestion (Speed Optimization §2): a hint, never the label.
      suggested_label: (suggested && suggested.suggested_label) || null,
      suggested_critique: (suggested && suggested.suggested_critique) || null,
    };
  }
  // A manually authored step (the doctor's own correct reasoning the AI omitted):
  // no original_text, added=true, label=good, counts as resolved.
  function newAuthoredStep() {
    const s = newStep('', null);
    s.added = true; s.label = 'good'; s.step_reward = 1;
    return s;
  }
  // The single definition of what "confirmed good" / "back to pending" means
  // for a step, used by the per-step button (expanded + collapsed) AND the
  // bulk "Confirm all correct" action, so every confirm path emits an
  // identical record shape.
  function setStepConfirmed(s, on) {
    if (on) {
      s.confirmed = true; s.corrected = false; s.correction_reason = null;
      s.label = 'good'; s.step_reward = 1; s.critique = '';
    } else {
      s.confirmed = false; s.label = null; s.step_reward = null;
    }
  }

  // The chosen/refined answer text to split into steps (chosen path only).
  function chosenRefinedText() {
    const rev = state.draft.chosen_revision;
    return (rev.revised_text != null ? rev.revised_text : chosenText()) || '';
  }

  // Auto-split the chosen answer into gradable steps, pre-graded when the LLM
  // is available (Speed Optimization §2): each step arrives with a suggested
  // good/bad label so the doctor spends time only on the flagged ones. ``force``
  // re-runs even when steps already exist; §5 removed the button that passed it,
  // so the only live caller is the on-mount auto-fire. Degrades gracefully:
  // offline the steps arrive unlabeled and the doctor grades manually; on
  // failure the doctor just adds steps.
  async function autoSplitChosen(listId, force) {
    const text = chosenRefinedText().trim();
    const startedChosen = state.draft.chosen_id;
    if (!text || state.splitting) return;
    if (!force && activeSteps().length) return;
    state.splitting = true;
    const list = document.getElementById(listId);
    // §5 (V3/V4): skeleton rows, not a blank card: "this is arriving" without
    // a spinner to interpret. V1/V2 keep the sentence.
    if (list) {
      clear(list);
      list.appendChild(isV3() ? stepsSkeleton()
        : h('p', { class: 'asc-help' }, 'Splitting the chosen answer into steps…'));
    }
    if (isV3()) syncStepsCont(); // §13: Continue stays locked while splitting
    try {
      // Assisted flows (V2 + V3) pre-grade each step (suggested good/bad); V1
      // (classic) just splits. In V3 this only runs post-verdict (editing the
      // chosen answer), so it never leaks a suggestion before the verdict.
      const res = await api(isAssisted() ? '/reasoning/pregrade' : '/reasoning/split', {
        method: 'POST',
        body: { text, prompt: state.task.prompt, specialty: state.task.specialty },
      });
      // Discard if the doctor changed verdict/side while the split was in flight,
      // so results never land on a different answer. Write to the CURRENT array.
      if (state.draft.stage === 'compare' && state.draft.chosen_id === startedChosen) {
        const steps = activeSteps();
        steps.length = 0;
        (res.steps || []).forEach((s) => {
          // /reasoning/split returns strings; /reasoning/pregrade returns
          // {text, suggested_label, suggested_critique}.
          const t = (s && typeof s === 'object') ? (s.text || '') : String(s || '');
          if (t) steps.push(newStep(t, t, (s && typeof s === 'object') ? s : null));
        });
        saveDraft();
      }
    } catch (e) { /* graceful: leave steps for manual entry */ }
    finally { state.splitting = false; repaintSteps(listId); updateSubmitState(); }
  }

  // Route step-list repaints to the version-appropriate renderer: V3/V4 use the
  // single-open accordion (§13); V1/V2 keep the classic list.
  function repaintSteps(listId) {
    if (isV3()) renderStepsListV3(listId);
    else renderStepsList(listId);
  }

  function renderStepsCard(forBoth) {
    const listId = 'ascStepsList';
    const required = (state.task.grounding_mode === 'required');
    const canAutoSplit = !forBoth;  // chosen path (A/B verdict) only

    const addBtn = h('button', {
      class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
      onClick: () => { activeSteps().push(newAuthoredStep()); saveDraft(); renderStepsList(listId); updateSubmitState(); },
    }, '+ Add step');
    // V1/V2 (classic) keeps its re-split control. The Eval UI Overhaul §5 removal
    // is scoped to the V3/V4 accordion, where the split auto-fires on mount; here
    // it is still the only way to re-run a bad split.
    const resplitBtn = canAutoSplit ? h('button', {
      class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
      onClick: () => autoSplitChosen(listId, true),
    }, '↻ Re-split from answer') : null;

    const card = h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head' }, '↳ Reasoning steps ',
        h('span', { class: 'asc-label-hint', style: 'margin-left:6px' },
          canAutoSplit ? 'confirm each step, or edit it to correct it' : (required ? '(each step needs a citation)' : '(optional)'))),
      h('div', { class: 'asc-subcard-body' },
        h('div', { class: 'asc-steps', id: listId }),
        h('div', { style: 'margin-top:12px;display:flex;gap:8px;flex-wrap:wrap' }, addBtn, resplitBtn),
      ));
    setTimeout(() => {
      renderStepsList(listId);
      // Auto-split once per task when entering the chosen-path card with no steps.
      if (canAutoSplit && activeSteps().length === 0
          && state.splitAttemptedFor !== state.task.task_id && !state.splitting) {
        state.splitAttemptedFor = state.task.task_id;
        autoSplitChosen(listId, false);
      }
    }, 0);
    return card;
  }

  // Edit-to-Correct per-step UI. Each split step is confirmed as-is (one tap,
  // label=good) or edited to correct it; on first divergence a required one-tap
  // reason row appears and the label is auto-derived (minor_wording→neutral, else
  // bad). The AI's original step is preserved + shown collapsed for reference.
  function renderStepsList(listId) {
    const list = document.getElementById(listId);
    if (!list) return;
    clear(list);
    const steps = activeSteps();
    const reasons = (state.taxonomy.step_correction_reasons
      || ['factual_error', 'outdated_guideline', 'incomplete', 'unsafe', 'wrong_order', 'minor_wording']);
    const required = (state.task.grounding_mode === 'required');

    // Pre-graded flow (Speed Optimization §2): suggested-good steps render
    // collapsed with per-step confirm + one deliberate "Confirm all correct"
    // action; flagged steps render expanded for review/edit-to-correct. Every
    // step still requires an explicit confirm/correct; silence ≠ endorsement.
    const isCollapsed = (s) => (
      s.suggested_label === 'good' && !s._exp && !s.corrected && !s.added
      && (s.text || '').trim() === (s.original_text || '').trim()
    );
    const pendingGood = steps.filter((s) => isCollapsed(s) && !s.confirmed);
    if (pendingGood.length) {
      list.appendChild(h('div', { class: 'asc-step-bulkbar' },
        h('span', { class: 'asc-step-bulk-label' },
          pendingGood.length + ' step' + (pendingGood.length === 1 ? ' looks' : 's look')
          + ' correct to the model. Read them, then confirm in one tap.'),
        h('button', {
          class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
          onClick: () => {
            steps.forEach((s) => { if (isCollapsed(s) && !s.confirmed) setStepConfirmed(s, true); });
            saveDraft(); renderStepsList(listId); updateSubmitState();
          },
        }, '✓ Confirm all correct')));
    }

    steps.forEach((s, idx) => {
      s.step = idx + 1;
      const hasOriginal = s.original_text != null;

      // Collapsed compact row for a model-passed step (expand to edit/correct).
      if (isCollapsed(s)) {
        const pill = h('span', { class: 'asc-step-status ' + (s.confirmed ? 'confirmed' : 'pending') },
          s.confirmed ? 'confirmed ✓' : 'pending');
        list.appendChild(h('div', { class: 'asc-step asc-step-collapsed' + (s.confirmed ? ' is-confirmed' : '') },
          h('div', { class: 'asc-step-head' },
            h('div', { style: 'display:flex;align-items:center;gap:8px;min-width:0' },
              h('span', { class: 'asc-step-num' }, 'Step ' + (idx + 1)),
              h('span', { class: 'asc-step-suggest good', title: 'Model pre-grade: your confirmation is the label' }, 'model · looks correct'),
              pill),
            h('div', { style: 'display:flex;align-items:center;gap:8px' },
              h('button', {
                class: 'asc-btn asc-btn-ghost asc-btn-sm asc-step-confirm' + (s.confirmed ? ' active' : ''),
                type: 'button',
                onClick: () => {
                  setStepConfirmed(s, !s.confirmed);
                  saveDraft(); renderStepsList(listId); updateSubmitState();
                },
              }, s.confirmed ? '✓ Confirmed' : '✓ Correct as-is'),
              h('button', {
                class: 'asc-btn-link', type: 'button',
                onClick: () => { s._exp = true; renderStepsList(listId); },
              }, 'Edit'))),
          h('div', { class: 'asc-step-collapsed-text' }, s.text || '')));
        return;
      }

      const ta = h('textarea', { class: 'asc-textarea', placeholder: 'Describe this reasoning step…' }, s.text || '');

      const statusPill = h('span', { class: 'asc-step-status' }, '');
      const addedBadge = h('span', { class: 'asc-badge asc-badge-accent asc-step-added' }, 'added (AI omitted)');

      // ✓ Correct as-is: explicit positive endorsement (silence ≠ endorsement).
      // Tapping an already-confirmed step toggles it back to pending.
      const confirmBtn = h('button', {
        class: 'asc-btn asc-btn-ghost asc-btn-sm asc-step-confirm', type: 'button',
        onClick: () => {
          setStepConfirmed(s, !s.confirmed);
          // In-place update only; a full re-render here resets the scroll
          // position and bounces the page up between steps.
          saveDraft(); syncStepUI(); updateSubmitState();
        },
      }, '✓ Correct as-is');

      // Reason chips (required on an edited step): single-select, derive label.
      const chipEls = {};
      const reasonRow = h('div', { class: 'asc-step-reasons' });
      reasons.forEach((r) => {
        const chip = h('button', {
          class: 'asc-chip asc-chip-sm', type: 'button',
          onClick: () => {
            s.correction_reason = r;
            s.label = (r === 'minor_wording') ? 'neutral' : 'bad';
            s.step_reward = s.label === 'good' ? 1 : 0;
            // In-place update only; avoid the full re-render scroll jump.
            saveDraft(); syncStepUI(); updateSubmitState();
          },
        }, r.replace(/_/g, ' '));
        chipEls[r] = chip;
        reasonRow.appendChild(chip);
      });
      const reasonWrap = h('div', { class: 'asc-step-correct' },
        h('div', { class: 'asc-label asc-step-reason-hint' }, 'What was wrong with the AI step? (pick one)'),
        reasonRow);

      // Collapsed "original:" reference: the AI's split step we're correcting.
      const originalBox = hasOriginal
        // V1/V2 (classic) keeps the original 80-char JS truncation. The §8
        // fill-the-width fix is scoped to the V3/V4 renderer.
        ? h('details', { class: 'asc-step-original' },
            h('summary', {}, 'original: ' + ((s.original_text || '').length > 80
              ? (s.original_text || '').slice(0, 80) + '…' : (s.original_text || ''))),
            h('div', { class: 'asc-step-original-full' }, s.original_text || ''))
        : null;

      // Optional one-line critique, kept available on a corrected step.
      const ci = h('input', { class: 'asc-input', placeholder: "What's off with this step? (optional, one line)", value: s.critique || '' });
      ci.addEventListener('input', () => { s.critique = ci.value; saveDraft(); });
      const critiqueField = h('div', { class: 'asc-field', style: 'margin-top:8px' }, withMic(ci));

      // Model pre-grade flag (Speed Optimization §2): a review hint, never a label.
      const flaggedBadge = (s.suggested_label === 'bad')
        ? h('span', { class: 'asc-step-suggest bad', title: 'Model pre-grade: verify and confirm or correct' }, 'model · flags this')
        : null;
      const suggestHint = (s.suggested_label === 'bad' && s.suggested_critique)
        ? h('div', { class: 'asc-step-suggest-hint' }, 'Model: ' + s.suggested_critique)
        : null;

      const head = h('div', { class: 'asc-step-head' },
        h('div', { style: 'display:flex;align-items:center;gap:8px' },
          h('span', { class: 'asc-step-num' }, 'Step ' + (idx + 1)), statusPill, addedBadge, flaggedBadge),
        h('div', { style: 'display:flex;align-items:center;gap:8px' },
          confirmBtn,
          h('button', {
            class: 'asc-btn-link', type: 'button',
            onClick: () => { steps.splice(idx + 1, 0, newAuthoredStep()); saveDraft(); renderStepsList(listId); updateSubmitState(); },
          }, '+ insert'),
          h('button', {
            class: 'asc-btn-link', type: 'button', style: 'color:var(--asc-danger)',
            onClick: () => { steps.splice(idx, 1); saveDraft(); renderStepsList(listId); updateSubmitState(); },
          }, 'Remove')));

      const anchorBlock = renderAnchorBlock(s.evidence_anchor, { label: 'citation for this step', required });

      // V3 (WS3, audit P2): one-click citation auto-suggest on EACH reasoning
      // step, the highest-value place to ground (PRM step-level supervision).
      // Retrieval keys on the step text + its critique; Confirm fills the step's
      // evidence_anchor exactly like the rationale chip. Mounted only on this
      // expanded card (collapsed model-passed steps have no anchor UI), and the
      // per-task suggestion cache keeps list re-renders from re-billing retrieval.
      const stepCite = renderCiteSuggest(
        s.evidence_anchor,
        () => ((s.text || '') + ' ' + (s.critique || '')),
        () => renderStepsList(listId));
      wireCiteSuggest(ta, stepCite);
      wireCiteSuggest(ci, stepCite);

      // Sync affordances to step state WITHOUT a full re-render, so typing in the
      // textarea never steals focus mid-edit.
      function syncStepUI() {
        const corrected = !!s.corrected, added = !!s.added, confirmed = !!s.confirmed;
        let text = 'pending', cls = 'pending';
        if (added) { text = 'added'; cls = 'added'; }
        else if (corrected) {
          text = s.correction_reason ? ('corrected · ' + s.correction_reason.replace(/_/g, ' ')) : 'corrected: pick a reason';
          cls = 'corrected';
        } else if (confirmed) { text = 'confirmed ✓'; cls = 'confirmed'; }
        statusPill.textContent = text;
        statusPill.className = 'asc-step-status ' + cls;
        addedBadge.hidden = !added;
        confirmBtn.hidden = corrected || added;
        confirmBtn.classList.toggle('active', confirmed);
        reasonWrap.hidden = !corrected;
        if (originalBox) originalBox.hidden = !corrected;
        critiqueField.hidden = !corrected;
        Object.keys(chipEls).forEach((r) => chipEls[r].classList.toggle('active', s.correction_reason === r));
      }

      ta.addEventListener('input', () => {
        s.text = ta.value;
        if (hasOriginal) {
          if (ta.value.trim() !== (s.original_text || '').trim()) {
            if (!s.corrected) { s.corrected = true; s.confirmed = false; }
          } else {
            // edited back to exactly the original AI step -> revert to pending
            s.corrected = false; s.confirmed = false; s.correction_reason = null;
            s.label = null; s.step_reward = null;
          }
        }
        saveDraft(); syncStepUI(); updateSubmitState();
      });

      syncStepUI();
      list.appendChild(h('div', { class: 'asc-step' }, head, suggestHint, ta, reasonWrap, originalBox, critiqueField, stepCite, anchorBlock));
    });
    if (!steps.length) {
      list.appendChild(h('p', { class: 'asc-help' }, 'No steps yet. Add steps manually, or use “Re-split from answer”.'));
    }
  }

  // ─── Evidence anchor block (progressive disclosure) ─────────────────────────
  // V3 auto-suggest citation chip (Seamless PRD WS3). Given the clinician's
  // rationale/answer text, fetch the 1–3 most relevant library citations; the
  // doctor opens the snippet inline and Confirms (one tap) to set the
  // evidence_anchor + mark the record grounded (value ×1.3). Nothing is
  // auto-attached; the confirm is required (mission line). V3 only; returns
  // null elsewhere so V1/V2 keep the manual citation field unchanged.
  function renderCiteSuggest(anchor, getText, onConfirm) {
    if (!isV3()) return null;
    const wrap = h('div', { class: 'asc-cite-suggest' });
    let lastQuery = null, dismissed = false;
    // §11: a clean list; opening the source IS the action (the citation is
    // recorded as entry_method:'opened'); no separate Confirm step. An entry
    // with no verified link renders reference-only (no Open source button,
    // never a constructed/guessed URL), citable via a subtle "Cite".
    const applyCite = (s, method) => {
      anchor.citation_text = (s.snippet || s.title || s.identifier || '').trim();
      // Only accept a source_type the validator recognizes; otherwise leave
      // it blank so the doctor completes it (never a false "grounded").
      const types = (state.taxonomy && state.taxonomy.evidence_source_types) || [];
      anchor.source_type = types.indexOf(s.source_type) !== -1 ? s.source_type : '';
      anchor.identifier = (s.identifier || s.title || '').trim();
      anchor.url = s.url || '';
      anchor.citation_confirmed = true;
      anchor.entry_method = method;
      saveDraft();
      // Toast the TRUTH: only claim grounded when the anchor actually
      // validates (else cleanAnchor would strip it on submit / block a
      // grounding=required task, the misleading-success case).
      if (isValidAnchor(anchor)) toast('Citation attached. This record is now grounded.', 'success');
      else toast('Citation attached. Finish the source fields below to ground it.', 'info');
      // Defer the re-render one tick: the 'opened' path fires from an <a
      // target=_blank> click, and tearing the anchor out of the DOM inside its
      // own click handler can cancel the new-tab navigation in some browsers.
      if (onConfirm) setTimeout(onConfirm, 0);
    };
    const renderChips = (suggestions) => {
      clear(wrap);
      if (!suggestions.length) return;
      wrap.appendChild(h('div', { class: 'asc-cite-head' },
        h('span', { class: 'asc-cite-title' }, 'Suggested source' + (suggestions.length > 1 ? 's' : '') + ': open one to check it; opening attaches it'),
        h('button', { class: 'asc-btn-link', type: 'button', onClick: () => { dismissed = true; clear(wrap); } }, 'dismiss')));
      suggestions.slice(0, 3).forEach((s) => {
        const hasUrl = !!(s.url && /^https?:\/\//i.test(s.url));
        const snippet = h('div', { class: 'asc-cite-snippet' }, s.snippet || '');
        snippet.setAttribute('hidden', '');
        const chip = h('div', { class: 'asc-cite-chip' },
          h('button', { class: 'asc-cite-open', type: 'button', title: 'Show the source text',
            onClick: () => { if (snippet.hasAttribute('hidden')) snippet.removeAttribute('hidden'); else snippet.setAttribute('hidden', ''); } },
            h('strong', {}, s.identifier || s.title || 'Source'),
            s.section ? h('span', { class: 'asc-cite-sec' }, ' · ' + s.section) : null),
          hasUrl
            // The ONE action: opens the entry's canonical link (never a guessed
            // URL) in a new tab AND records the citation.
            ? h('a', {
                class: 'asc-btn asc-btn-subtle asc-btn-sm', href: s.url,
                target: '_blank', rel: 'noopener noreferrer',
                onClick: () => applyCite(s, 'opened'),
              }, 'Open source ↗')
            // Reference-only: no link to mislead with; still citable.
            : h('span', { style: 'display:inline-flex;align-items:center;gap:8px' },
                h('span', { class: 'asc-label-hint' }, 'reference only'),
                h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
                  onClick: () => applyCite(s, 'typeahead') }, 'Cite')));
        wrap.appendChild(chip);
        wrap.appendChild(snippet);
      });
    };
    const fetchSuggest = async () => {
      if (dismissed) return;
      // Don't re-suggest once the doctor has already confirmed/typed a citation.
      if (isValidAnchor(anchor)) { clear(wrap); return; }
      const text = (getText() || '').trim();
      if (text.length < 12) { clear(wrap); lastQuery = null; return; }
      if (text === lastQuery) return;
      lastQuery = text;
      // Task-level cache so a card REBUILD (e.g. when the prelabel assist arrives,
      // or the steps list re-renders) doesn't re-POST /assist/cite for the same
      // text. A per-task MAP (not a single entry): with per-step chips several
      // widgets fetch different texts on one screen, and a single-entry cache
      // would thrash and re-bill the retrieval on every rebuild.
      const tid = state.task && state.task.task_id;
      if (!state._citeCache || state._citeCache.tid !== tid) state._citeCache = { tid, map: {} };
      const cached = state._citeCache.map[text];
      if (cached) {
        if (!dismissed && !isValidAnchor(anchor) && cached.length) renderChips(cached);
        else clear(wrap);
        return;
      }
      try {
        const res = await api('/assist/cite', { method: 'POST',
          body: { text, specialty: (state.task && state.task.specialty) || 'nephrology' } });
        if (Object.keys(state._citeCache.map).length > 40) state._citeCache.map = {};
        state._citeCache.map[text] = (res && res.suggestions) || [];
        if (dismissed || isValidAnchor(anchor)) return;
        if (res.skipped || !(res.suggestions || []).length) { clear(wrap); return; }
        renderChips(res.suggestions);
      } catch (e) { /* suggestions are a bonus; never surface an error to the doctor */ }
    };
    wrap._fetch = fetchSuggest;
    setTimeout(fetchSuggest, 400);
    return wrap;
  }

  // Attach a debounced citation re-suggest to a text field (V3). No-op pre-V3.
  function wireCiteSuggest(field, widget) {
    if (!widget || !field) return;
    let t = null;
    field.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => { if (widget._fetch) widget._fetch(); }, 700);
    });
  }

  function renderAnchorBlock(anchor, opts) {
    opts = opts || {};
    const required = !!opts.required;
    const body = h('div', { class: 'asc-disclosure-body' });
    if (!required && !isValidAnchor(anchor) && !(anchor.citation_text || '').trim()) body.setAttribute('hidden', '');
    body.appendChild(anchorFields(anchor));

    // Escape hatch: search the library (BUG-3c), always one tap away.
    const search = renderLibrarySearch(anchor, () => { block._rebuild(); });
    const searchBtn = h('button', { class: 'asc-btn-link', type: 'button', onClick: () => search._toggle() }, 'Search the library');
    body.appendChild(h('div', { style: 'margin:8px 0' }, searchBtn));
    body.appendChild(search);

    // Multi-anchor (BUG-3b): "+ Add another citation" appends extra anchor
    // editors bound to anchor._extra[]. All valid anchors ship as evidence_anchors.
    anchor._extra = anchor._extra || [];
    const extraHost = h('div', { class: 'asc-extra-anchors' });
    const renderExtra = () => {
      clear(extraHost);
      anchor._extra.forEach((ea, i) => {
        const row = h('div', { class: 'asc-extra-anchor', style: 'border-top:1px dashed var(--asc-line);padding-top:10px;margin-top:10px' });
        row.appendChild(h('div', { class: 'asc-label', style: 'display:flex;justify-content:space-between' },
          'Additional citation ' + (i + 1),
          h('button', { class: 'asc-btn-link', type: 'button', onClick: () => { anchor._extra.splice(i, 1); saveDraft(); renderExtra(); updateSubmitState(); } }, 'remove')));
        row.appendChild(anchorFields(ea));
        extraHost.appendChild(row);
      });
    };
    renderExtra();
    body.appendChild(extraHost);
    body.appendChild(h('button', { class: 'asc-btn-link', type: 'button', style: 'margin-top:8px',
      onClick: () => { anchor._extra.push(emptyAnchor()); saveDraft(); renderExtra(); updateSubmitState(); } }, '+ Add another citation'));

    const status = h('span', { class: 'asc-anchor-valid' });
    // §11: in the V3/V4 citations section the affordance is a REAL button (not a
    // tiny text link) with an explainer info-dot; elsewhere the compact
    // disclosure toggle is unchanged.
    const toggle = h('button', {
      class: opts.prominent ? 'asc-btn asc-btn-subtle asc-add-cite' : 'asc-disclosure-toggle',
      type: 'button',
      onClick: () => {
        if (body.hasAttribute('hidden')) body.removeAttribute('hidden');
        else body.setAttribute('hidden', '');
      },
    }, required ? 'Citation (required)' : '+ add citation', status);

    const head = opts.prominent
      ? h('div', { class: 'asc-add-cite-row' }, toggle,
          infoDot('Citations', [
            'Cite the guideline or trial your judgment rests on.',
            'Search the library, open the source to check it, then it’s attached.',
          ]))
      : toggle;
    const block = h('div', { class: 'asc-disclosure' }, head, body);
    block._status = status;
    // Rebuild the primary fields when the library search fills the anchor, so the
    // inputs + the "Open source ↗" link reflect the picked source immediately.
    block._rebuild = () => {
      const first = body.firstChild;
      const fresh = anchorFields(anchor);
      if (first) body.replaceChild(fresh, first); else body.insertBefore(fresh, body.firstChild);
      refreshAnchorStatus(block, anchor, required);
    };
    refreshAnchorStatus(block, anchor, required);
    // keep status synced when fields change
    const sync = () => refreshAnchorStatus(block, anchor, required);
    body.addEventListener('input', sync);
    body.addEventListener('change', sync);
    return block;
  }

  // Every valid anchor on a section (primary + extras) for the multi-anchor
  // payload (BUG-3b). The first is also emitted as the singular ``evidence_anchor``.
  function anchorsForSubmit(anchor) {
    if (!anchor) return [];
    const all = [cleanAnchor(anchor)].concat((anchor._extra || []).map(cleanAnchor));
    return all.filter(Boolean);
  }

  function refreshAnchorStatus(block, anchor, required) {
    const status = block._status;
    if (!status) return;
    if (isValidAnchor(anchor)) { status.textContent = '✓ cited'; status.classList.remove('asc-anchor-invalid'); }
    else if (required) { status.textContent = '· citation needed'; status.classList.add('asc-anchor-invalid'); }
    else { status.textContent = ''; }
  }

  // A live "Open source ↗" link that appears whenever an anchor carries a URL
  // (BUG-3a): the doctor can click through to ground truth in one tap.
  function openSourceLink(anchor) {
    const span = h('span', { class: 'asc-open-source' });
    const sync = () => {
      clear(span);
      const url = (anchor.url || '').trim();
      if (url && /^https?:\/\//i.test(url)) {
        span.appendChild(h('a', { class: 'asc-cite-link', href: url, target: '_blank', rel: 'noopener noreferrer' }, 'Open source ↗'));
      }
    };
    sync();
    span._sync = sync;
    return span;
  }

  function anchorFields(anchor) {
    const types = (state.taxonomy.evidence_source_types || ['guideline', 'primary_literature', 'expert_consensus', 'other']);
    const citation = h('input', { class: 'asc-input', placeholder: 'e.g. KDIGO 2024 Guideline §3.2', value: anchor.citation_text || '' });
    citation.addEventListener('input', () => {
      anchor.citation_text = citation.value;
      if (isV3() && !anchor.entry_method) anchor.entry_method = 'manual';
      saveDraft(); updateSubmitState();
    });
    const sourceSel = h('select', { class: 'asc-select' },
      h('option', { value: '' }, 'Source type…'),
      ...types.map((t) => h('option', { value: t, selected: anchor.source_type === t ? 'selected' : null }, t.replace(/_/g, ' '))));
    sourceSel.value = anchor.source_type || '';
    sourceSel.addEventListener('change', () => { anchor.source_type = sourceSel.value; saveDraft(); updateSubmitState(); });
    const identifier = h('input', { class: 'asc-input', placeholder: 'Identifier: PMID:…, DOI:…, KDIGO 2024', value: anchor.identifier || '' });
    identifier.addEventListener('input', () => {
      anchor.identifier = identifier.value;
      if (isV3() && !anchor.entry_method) anchor.entry_method = 'manual';
      saveDraft(); updateSubmitState();
    });
    // Paste-your-own URL (BUG-3c escape hatch): a link the doctor pastes rides the
    // anchor + is clickable. Pasting a URL with no source type defaults to "other".
    const openLink = openSourceLink(anchor);
    const url = h('input', { class: 'asc-input', placeholder: 'Paste a source URL (optional): https://…', value: anchor.url || '' });
    url.addEventListener('input', () => {
      anchor.url = url.value.trim();
      // Paste-your-own (BUG-3c): a bare URL should be a usable citation, not
      // silently dropped for lacking source type/identifier (isValidAnchor needs
      // both). So a pasted URL back-fills source_type=other + the empty citation/
      // identifier fields with the URL, making it a valid, grounded anchor. The
      // doctor can still refine the text; we never overwrite what they typed.
      if (anchor.url) {
        if (!anchor.source_type) { anchor.source_type = 'other'; sourceSel.value = 'other'; }
        if (!(anchor.identifier || '').trim()) { anchor.identifier = anchor.url; identifier.value = anchor.url; }
        if (!(anchor.citation_text || '').trim()) { anchor.citation_text = anchor.url; citation.value = anchor.url; }
        if (isV3() && !anchor.entry_method) anchor.entry_method = 'manual';
      }
      openLink._sync(); saveDraft(); updateSubmitState();
    });
    return h('div', {},
      h('div', { class: 'asc-field', style: 'margin-bottom:10px' },
        h('label', { class: 'asc-label' }, 'Citation', openLink), citation),
      h('div', { class: 'asc-form-row', style: 'margin-bottom:10px' },
        h('div', { class: 'asc-field', style: 'margin-bottom:0' }, h('label', { class: 'asc-label' }, 'Source type'), sourceSel),
        h('div', { class: 'asc-field', style: 'margin-bottom:0' }, h('label', { class: 'asc-label' }, 'Identifier'), identifier)),
      h('div', { class: 'asc-field', style: 'margin-bottom:0' }, h('label', { class: 'asc-label' }, 'Source link'), url));
  }

  // ── Library search box + multi-anchor (BUG-3b/c) ────────────────────────────
  // "Search the library" is always one tap away; the doctor types a query and
  // picks a source (fills the anchor). An escape hatch when the auto-suggest is
  // wrong (it is better to show nothing than a wrong citation, so search is
  // deliberately more permissive than the suggestion).
  function renderLibrarySearch(anchor, onPick) {
    const box = h('div', { class: 'asc-lib-search', hidden: true });
    const input = h('input', { class: 'asc-input', placeholder: 'Search the citation library: drug, analyte, guideline…' });
    const results = h('div', { class: 'asc-lib-results' });
    let t = null;
    const run = async () => {
      clear(results);
      try {
        const res = await api('/citations/search', { method: 'POST',
          body: { text: input.value.trim(), specialty: (state.task && state.task.specialty) || 'nephrology', k: 12 } });
        const list = (res && res.suggestions) || [];
        if (res.skipped) { results.appendChild(h('div', { class: 'asc-label-hint' }, 'No citation library for this specialty. Type or paste your own.')); return; }
        if (!list.length) { results.appendChild(h('div', { class: 'asc-label-hint' }, 'No matches. Try a drug name or lab analyte.')); return; }
        list.forEach((s) => {
          results.appendChild(h('div', { class: 'asc-lib-row' },
            h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
              onClick: () => {
                anchor.citation_text = (s.snippet || s.title || s.identifier || '').trim();
                const types = (state.taxonomy && state.taxonomy.evidence_source_types) || [];
                anchor.source_type = types.indexOf(s.source_type) !== -1 ? s.source_type : '';
                anchor.identifier = (s.identifier || s.title || '').trim();
                anchor.url = s.url || '';
                anchor.citation_confirmed = true;
                if (isV3()) anchor.entry_method = 'typeahead';
                saveDraft();
                box.setAttribute('hidden', '');
                if (onPick) onPick();
              } }, 'Use'),
            h('span', {}, h('strong', {}, s.identifier || s.title || 'Source'),
              s.url ? h('a', { class: 'asc-cite-link', href: s.url, target: '_blank', rel: 'noopener noreferrer', style: 'margin-left:8px' }, '↗') : null,
              h('div', { class: 'asc-label-hint' }, s.snippet || s.section || ''))));
        });
      } catch (e) { clear(results); }
    };
    input.addEventListener('input', () => { clearTimeout(t); t = setTimeout(run, 300); });
    box.appendChild(input);
    box.appendChild(results);
    box._toggle = () => { if (box.hasAttribute('hidden')) { box.removeAttribute('hidden'); input.focus(); if (input.value) run(); } else box.setAttribute('hidden', ''); };
    return box;
  }

  function discloseToggle(label, body) {
    return h('button', {
      class: 'asc-disclosure-toggle', type: 'button',
      onClick: () => { if (body.hasAttribute('hidden')) body.removeAttribute('hidden'); else body.setAttribute('hidden', ''); },
    }, label);
  }

  // ─── Chips multiselect ─────────────────────────────────────────────────────
  function renderChips(options, selectedArray, onToggle, extra) {
    const wrap = h('div', { class: 'asc-chips' });
    options.forEach((opt) => {
      const chip = h('button', {
        class: 'asc-chip' + (extra ? ' ' + extra : '') + (selectedArray.indexOf(opt) !== -1 ? ' active' : ''),
        type: 'button',
        onClick: () => {
          const on = selectedArray.indexOf(opt) === -1;
          chip.classList.toggle('active', on);
          onToggle(opt, on);
        },
      }, opt.replace(/_/g, ' '));
      wrap.appendChild(chip);
    });
    return wrap;
  }
  function toggleInArray(arr, val, on) {
    const i = arr.indexOf(val);
    if (on && i === -1) arr.push(val);
    else if (!on && i !== -1) arr.splice(i, 1);
  }

  // ─── Submit bar ────────────────────────────────────────────────────────────
  function renderSubmitBar() {
    const confLevels = (state.taxonomy.confidence_levels || ['low', 'medium', 'high']);
    const confPills = h('div', { class: 'asc-conf-pills', id: 'ascConf' });
    confLevels.forEach((lvl) => {
      confPills.appendChild(h('button', {
        class: 'asc-conf-pill' + (state.draft.confidence === lvl ? ' active' : ''),
        type: 'button', dataset: { conf: lvl },
        onClick: () => {
          state.draft.confidence = lvl; saveDraft();
          Array.from(confPills.children).forEach((b) => b.classList.toggle('active', b.dataset.conf === lvl));
        },
      }, lvl));
    });

    const submitBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', id: 'ascSubmit', onClick: submitEvaluation }, 'Submit evaluation');
    const hint = h('span', { class: 'asc-submit-hint', id: 'ascSubmitHint' });

    return h('div', { class: 'asc-submit-bar' },
      h('div', { class: 'asc-conf-group' },
        h('span', { class: 'asc-label' }, 'Confidence'), confPills),
      h('div', { class: 'asc-submit-right' },
        h('span', { class: 'asc-timer', id: 'ascTimer', 'data-tour-ignore': '1' }, formatTime(getElapsed())),
        hint,
        submitBtn));
  }

  function updateSubmitState() {
    const btn = document.getElementById('ascSubmit');
    const hint = document.getElementById('ascSubmitHint');
    if (!btn) return;
    const d = state.draft;
    let ok = true, msg = '';
    if (!d.verdict) { ok = false; msg = 'pick a verdict to continue'; }
    else if (d.verdict === 'both_inadequate' && !(d.from_scratch.ideal_answer || '').trim()) {
      ok = false; msg = 'write the ideal answer to continue';
    } else {
      const g = groundingSatisfied();
      const sr = stepsReview();
      if (!g.ok) {
        ok = false;
        msg = g.reasons.indexOf('missing_step_anchor') !== -1
          ? 'add a citation to your rationale and each step to continue'
          : 'add a citation to continue';
      } else if (!sr.ok) {
        ok = false;
        msg = sr.reasons.indexOf('missing_correction_reason') !== -1
          ? 'pick what was wrong on the edited step'
          : 'review each reasoning step (confirm or correct)';
      } else {
        const rg = rubricGate();
        const fg = failureTagGate();
        const abVerdict = d.verdict === 'A_better' || d.verdict === 'B_better';
        if (!rg.ok) { ok = false; msg = rg.msg; }
        else if (!fg.ok) { ok = false; msg = fg.msg; }
        // §10/§12 (V3/V4): the required sections stay required even if the
        // doctor re-opens a completed section and deletes its data; submit
        // must never ship an empty "why better" or critique.
        else if (isV3() && abVerdict && !whyBetterConditionsMet()) {
          ok = false; msg = 'finish “why it’s better” (one line + ≥1 tag) to submit';
        } else if (isV3() && abVerdict && !critiqueConditionsMet()) {
          ok = false; msg = 'finish the rejected-answer critique to submit';
        }
        // §15 (V3/V4): confidence is an active choice, never the draft default.
        else if (isV3() && !d.confidence_set) { ok = false; msg = 'pick your confidence to submit'; }
      }
    }
    btn.disabled = !ok || state.submitting;
    hint.textContent = ok ? '' : msg;
  }

  // Human-readable label per backend-stamped phase (BUG-5). The percentages are
  // the backend's; we only translate the phase name; a phase without a pct shows
  // an indeterminate spinner (honest "we don't know how long this takes").
  const _PHASE_LABEL = {
    queued: 'Queued…', packaging: 'Packaging training records…',
    validating: 'Validating (PHI, completeness, duplicates)…',
    consistency_check: 'Running the consistency critic…',
    grounding_check: 'Checking evidence grounding…',
    qa_routing: 'Routing to QA review…', needs_qa: 'Sent to QA review',
    complete: 'Complete',
  };

  function _renderProgress(host, phase, pct) {
    clear(host);
    const track = h('div', { class: 'asc-progress-track' });
    const bar = h('div', { class: 'asc-progress-bar' + (pct == null ? ' indeterminate' : '') });
    if (pct != null) bar.style.width = Math.max(4, Math.min(100, pct)) + '%';
    track.appendChild(bar);
    host.appendChild(track);
    host.appendChild(h('div', { class: 'asc-progress-label' },
      (_PHASE_LABEL[phase] || phase || 'Submitting…') + (pct != null ? '  ' + pct + '%' : '')));
  }

  async function submitEvaluation() {
    if (state.submitting) return;
    saveDraft();
    const g = groundingSatisfied();
    if (!g.ok) { updateSubmitState(); return; }
    const sr = stepsReview();
    if (!sr.ok) { updateSubmitState(); return; }
    if (!rubricGate().ok) { updateSubmitState(); return; }
    if (!failureTagGate().ok) { updateSubmitState(); return; }
    // §10/§12/§15 (V3/V4): mirror every staged-flow gate; a submit must never
    // ship with a re-opened section's required data deleted, or the draft
    // default confidence.
    if (isV3()) {
      const d0 = state.draft;
      const abVerdict = d0.verdict === 'A_better' || d0.verdict === 'B_better';
      if (abVerdict && (!whyBetterConditionsMet() || !critiqueConditionsMet())) { updateSubmitState(); return; }
      if (!d0.confidence_set) { updateSubmitState(); return; }
    }
    // Tutorial: grade against the answer key and show the reveal: the real
    // submit pipeline (and its records/QA routing) is never touched.
    if (tutorialActive()) { await submitTutorialEvaluation(); return; }
    state.submitting = true;
    updateHeaderProgress(); // §16: the bar reads 100% while the submit runs
    const btn = document.getElementById('ascSubmit');
    const hint = document.getElementById('ascSubmitHint');
    if (btn) { btn.disabled = true; btn.textContent = 'Submitting…'; }
    // Mount a real progress bar in place of the hint (BUG-5).
    let progressHost = null;
    if (hint) {
      hint.textContent = '';
      progressHost = h('div', { class: 'asc-progress-wrap', id: 'ascSubmitProgress' });
      hint.appendChild(progressHost);
      _renderProgress(progressHost, 'queued', 5);
    }
    const taskId = state.draft.task_id;

    const payload = buildSubmissionPayload();
    try {
      // Real submit progress (BUG-5): opt into the async pipeline (202 +
      // submission_id) and poll the backend-stamped phases. If the server doesn't
      // support it (older backend returns 200 + result), fall through to success.
      const res = await api('/submissions?async_pipeline=1', { method: 'POST', body: payload });
      let finalStatus = res.status;
      let recordCount = res.record_count;
      let timedOut = false;
      if (res.accepted && res.submission_id) {
        const done = await pollSubmissionStatus(res.submission_id, progressHost);
        finalStatus = done.status; recordCount = done.record_count;
        timedOut = !done.done;
      }
      const n = recordCount != null ? recordCount : 0;
      // The submission is committed server-side the moment we got the 202, so a
      // poll timeout is "still finalizing", NOT a failure; never lose the work.
      clearDraft(taskId);
      stopTimer();
      if (timedOut) {
        toast('Submitted. Still finalizing in the background. It will appear once the pipeline completes.', 'success');
      } else if (finalStatus === 'needs_qa') {
        toast('Submitted. Routed to QA review (' + n + ' record' + (n === 1 ? '' : 's') + ').', 'success');
      } else {
        toast('Submitted. Packaged ' + n + ' record' + (n === 1 ? '' : 's'), 'success');
      }
      renderEvalView();
    } catch (e) {
      state.submitting = false;
      if (btn) { btn.textContent = 'Submit evaluation'; }
      if (progressHost) clear(progressHost);
      if (e.status === 400 && e.detail && e.detail.error === 'grounding_required') {
        toast(e.detail.message || 'A citation is required before submitting.', 'error');
        updateSubmitState();
      } else if (e.status === 400 && e.detail && e.detail.error === 'critical_negative_required') {
        toast(e.detail.message || 'Your rubric must include at least one critical negative criterion.', 'error');
        updateSubmitState();
      } else if (e.status === 400 && e.detail && e.detail.error === 'failure_tag_required') {
        toast(e.detail.message || 'Tag at least one failure mode on the rejected answer.', 'error');
        updateSubmitState();
      } else if (e.status !== 401) {
        toast('Submit failed: ' + e.message, 'error');
        updateSubmitState();
      }
    } finally {
      state.submitting = false;
    }
  }

  // Poll GET /submissions/{id}/status until the backend reports a terminal phase.
  // Shows the real, backend-stamped phase, never an invented percentage.
  async function pollSubmissionStatus(sid, progressHost) {
    const deadline = Date.now() + 60000;  // safety cap
    let last = null;
    while (Date.now() < deadline) {
      try {
        last = await api('/submissions/' + sid + '/status');
      } catch (e) {
        if (e.status === 401) throw e;
        // transient; keep polling until the deadline
      }
      if (last && progressHost) _renderProgress(progressHost, last.phase, last.pct);
      if (last && last.done) return last;
      await new Promise((r) => setTimeout(r, 500));
    }
    return last || { done: false, status: 'unknown', record_count: 0 };
  }

  function cleanAnchor(a) {
    if (!isValidAnchor(a)) return null;
    const out = { citation_text: a.citation_text.trim(), source_type: a.source_type, identifier: a.identifier.trim() };
    // Carry the library URL + the confirm flag from an auto-suggested citation
    // (Seamless PRD WS3) so the record distinguishes a confirmed source.
    if ((a.url || '').trim()) out.url = a.url.trim();
    if (a.citation_confirmed) out.citation_confirmed = true;
    // §11: how the citation was captured: 'opened' (clicked through to the
    // source), 'typeahead' (picked from the library), 'manual' (hand-typed).
    if (a.entry_method) out.entry_method = a.entry_method;
    return out;
  }
  function cleanSteps(steps) {
    return (steps || []).filter((s) => (s.text || '').trim()).map((s, i) => ({
      step: i + 1,
      text: s.text,
      original_text: s.original_text != null ? s.original_text : null,
      corrected: !!s.corrected,
      confirmed: !!s.confirmed,
      added: !!s.added,
      correction_reason: s.correction_reason || null,
      // §13: the free-text "what's off"; the server derives step_error_tag
      // (and a correction_reason) from it. Additive; null when untouched.
      step_note: (s.step_note || '').trim() || null,
      label: s.label || null,
      step_reward: s.step_reward != null ? s.step_reward : null,
      critique: (s.critique || '').trim() || null,
      // Pre-grade provenance (Speed Optimization §2): the suggestion ships next
      // to the human label so override rate is monitorable.
      suggested_label: s.suggested_label || null,
      suggested_critique: (s.suggested_critique || '').trim() || null,
      evidence_anchor: cleanAnchor(s.evidence_anchor),
      evidence_anchors: anchorsForSubmit(s.evidence_anchor),
    }));
  }

  function buildSubmissionPayload() {
    const d = state.draft;
    const time = getElapsed();
    const payload = {
      submission_id: d.submission_id,
      task_id: d.task_id,
      verdict: d.verdict,
      chosen_id: d.chosen_id,
      rejected_id: d.rejected_id,
      confidence: d.confidence,
      time_spent_sec: time,
      reasoning_steps: [],
      // Stage-1/Stage-2 gated-capture fields (Eval Flow Upgrade §2, §3).
      prompt_review: {
        reviewed: !!d.prompt_review.reviewed,
        verdict: d.prompt_review.verdict,
        note: (d.prompt_review.note || '').trim() || null,
        reviewed_at: d.prompt_review.reviewed_at,
      },
      independent_answer: {
        text: (d.independent_answer.text || '').trim(),
        evidence_anchor: cleanAnchor(d.independent_answer.evidence_anchor),
        evidence_anchors: anchorsForSubmit(d.independent_answer.evidence_anchor),
        captured_at: d.independent_answer.captured_at,
      },
      // Which evaluator flow produced this submission (Asclepius V2). The server
      // treats the reveal-commit's version as authoritative; this is the value
      // for the flagged-prompt path (no commit) and direct submits.
      portal_version: draftVersion(),
      // Rubric capture (FEAT-2): the confirmed weighted criteria (empty when the
      // doctor didn't capture a rubric). Zero-point/empty rows are dropped server-side.
      rubric: (d.rubric || []).filter((c) => (c.text || '').trim()).map((c) => {
        // §11: `axes` is authoritative; `axis` ships alongside it as `axes[0]`
        // (deprecated) so older readers and stored records keep working.
        const axs = (Array.isArray(c.axes) && c.axes.length) ? c.axes.slice() : (c.axis ? [c.axis] : []);
        const entry = {
          text: (c.text || '').trim(), points: c.points || 0,
          axes: axs, axis: axs[0] || c.axis || null, source: c.source || 'manual',
          // Tier (Two-Model PRD WS-B): the server re-derives from |points| when it
          // doesn't match, so this is a hint, never authoritative.
          tier: tierForPoints(c.points),
          // FIX-1 concreteness hint (server recomputes from the final text).
          specific: isSpecificText(c.text),
        };
        // FIX-3 per-criterion evidence anchor (only when the doctor actually cited).
        const anch = cleanAnchor(c.evidence_anchor);
        if (anch) entry.evidence_anchor = anch;
        return entry;
      }),
    };
    // Model-assist audit block (Speed Optimization §2): the suggestions that
    // were SHOWN, stored next to the human finals so override rate is
    // monitorable server-side. Absent when no suggestion was displayed.
    const a = assistData();
    if (a) {
      payload.assist = {
        prelabeled: true,
        suggested_verdict: a.suggested_weaker === 'A' ? 'B_better' : 'A_better',
        suggested_error_tags: (a.suggested_error_tags || []).slice(),
        suggested_rationale: a.suggested_rationale || null,
        suggested_step_labels: activeSteps().map((s) => s.suggested_label || null),
        confidence: a.confidence != null ? a.confidence : null,
      };
    }
    if (d.verdict === 'A_better' || d.verdict === 'B_better') {
      const original = chosenText();
      const revised = d.chosen_revision.revised_text != null ? d.chosen_revision.revised_text : original;
      payload.chosen_revision = {
        edited: revised !== original,
        revised_text: revised,
        why_better_tags: d.chosen_revision.why_better_tags.slice(),
        why_better_notes: d.chosen_revision.why_better_notes || '',
        evidence_anchor: cleanAnchor(d.chosen_revision.evidence_anchor),
        evidence_anchors: anchorsForSubmit(d.chosen_revision.evidence_anchor),
      };
      const tagAnchors = {};
      Object.keys(d.rejected_critique.error_tag_anchors || {}).forEach((tag) => {
        if (d.rejected_critique.error_tags.indexOf(tag) === -1) return;
        const a = cleanAnchor(d.rejected_critique.error_tag_anchors[tag]);
        if (a) tagAnchors[tag] = a;
      });
      const tagReasons = {};
      Object.keys(d.rejected_critique.error_tag_reasons || {}).forEach((tag) => {
        if (d.rejected_critique.error_tags.indexOf(tag) === -1) return;
        if (d.rejected_critique.error_tag_reasons[tag]) tagReasons[tag] = d.rejected_critique.error_tag_reasons[tag];
      });
      payload.rejected_critique = {
        error_tags: d.rejected_critique.error_tags.slice(),
        severities: Object.assign({}, d.rejected_critique.severities),
        why_worse: d.rejected_critique.why_worse || '',
        error_tag_anchors: tagAnchors,
        error_tag_reasons: tagReasons,
        // Model-Failure Taxonomy (§D-2): physician failure-mode tags on the rejected
        // answer. Only well-formed {mode, note} rows; the server enforces the vocab.
        failure_tags: (d.rejected_critique.failure_tags || [])
          .filter((t) => t && t.mode)
          .map((t) => ({ mode: t.mode, note: (t.note || '').trim(),
            criterion_id: t.criterion_id || null, evidence_step_id: t.evidence_step_id || null })),
      };
      payload.reasoning_steps = cleanSteps(d.reasoning_steps);
      payload.from_scratch = null;
    } else if (d.verdict === 'both_inadequate') {
      payload.from_scratch = {
        ideal_answer: d.from_scratch.ideal_answer || '',
        approach_notes: d.from_scratch.approach_notes || '',
        reasoning_steps: cleanSteps(d.from_scratch.reasoning_steps),
        evidence_anchor: cleanAnchor(d.from_scratch.evidence_anchor),
        evidence_anchors: anchorsForSubmit(d.from_scratch.evidence_anchor),
      };
      payload.reasoning_steps = payload.from_scratch.reasoning_steps;
    }
    // Decisive action (Audit §13): the physician-named verifiable outcome: the test
    // or action the correct answer depends on. Skippable by design, so it's only sent
    // when the clinician actually named one; a fabricated one is worse than none.
    const da = d.decisive_action || {};
    const daAction = (da.action || '').trim();
    if (daAction) {
      payload.decisive_action = {
        action: daAction,
        tool_name: (da.tool_name || '').trim() || null,
        must_precede_final_answer: da.must_precede_final_answer !== false,
        rationale: (da.rationale || '').trim(),
      };
    }
    return payload;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  REFERRAL SECTION (PRD-REF)
  // ═══════════════════════════════════════════════════════════════════════════
  // Lives in its own file (frontend/asclepius/referral.js) and is mounted here
  // the way EarningsSection is.
  function renderReferralView() {
    stopTimer();
    updateHeaderProgress();
    const body = h('div', { id: 'ascReferralBody' });
    setRoot(h('div', { class: 'asc-wrap' }, body));
    if (window.ReferralSection && typeof window.ReferralSection.render === 'function') {
      window.ReferralSection.render(body, adminSectionCtx());
      return;
    }
    // A VISIBLE error, never a quiet placeholder: a silent fallback is how a
    // shipped feature stays invisible for a build round.
    body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-error' },
        'The Referral section failed to load. Reload the page; if it persists, '
        + 'this is a deploy problem: check that referral.js is included in '
        + 'index.html. Your referrals and bounties are unaffected.'))));
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  EARNINGS SECTION (PRD-P §5)
  // ═══════════════════════════════════════════════════════════════════════════
  // Lives in its own file (frontend/asclepius/earnings.js) and is mounted here
  // exactly the way AdminPhysiciansSection is. Payment logic never enters this file.
  function renderEarningsView() {
    stopTimer();
    updateHeaderProgress();
    const body = h('div', { id: 'ascEarningsBody' });
    setRoot(h('div', { class: 'asc-wrap' }, body));
    if (window.EarningsSection && typeof window.EarningsSection.render === 'function') {
      window.EarningsSection.render(body, adminSectionCtx());
      return;
    }
    // A VISIBLE error, never a quiet placeholder — and never a reassuring $0.
    // "We could not load your ledger" and "you have earned nothing" must not
    // look the same to a physician.
    body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-error' },
        'The Earnings section failed to load, so no figure is shown — this is '
        + 'not a statement that you have earned nothing. Reload the page; if it '
        + 'persists, this is a deploy problem — check that earnings.js is '
        + 'included in index.html. Nothing you have earned is affected.'))));
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  ADMIN CONSOLE
  // ═══════════════════════════════════════════════════════════════════════════
  // Legacy tab ids (deep links, stale state) → new section + sub-tab.
  const ADMIN_TAB_ALIASES = {
    tasks: ['work', 'tasks'], qa: ['physicians', 'qa'],
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
  function adminSectionCtx() {
    return {
      h, api, clear, toast, loadingCard, downloadBlob, fmtDate,
      specialtyResolver, specialtyBlockReason: SPECIALTY_BLOCK_REASON,
      // Jump to the pipeline tools (the ingestion review/promote surface),
      // deep-linked to the row that was clicked (C-5.2). The bucket buttons
      // always passed their upload; this used to ignore the argument and just
      // switch tabs, so the operator had to re-find the upload they had just
      // clicked on.
      openPipeline: (entry) => {
        state.adminTab = 'data'; state.adminSub.data = 'pipeline';
        state.pipelineFocus = (entry && (entry.upload_id || entry.uploadId)) || null;
        renderAdminView();
      },
      // Move the Physicians sub-tab strip from inside a section (the roster's
      // "N mid-onboarding" notice links to Signups). The section owns its views;
      // the shell owns which tab looks selected, so the jump has to come back
      // through here or the two disagree.
      openPhysiciansSub: (sub) => {
        state.adminTab = 'physicians'; state.adminSub.physicians = sub;
        renderAdminView();
      },
    };
  }

  function renderAdminView() {
    stopTimer();
    updateHeaderProgress(); // admin view: the §16 bar hides here
    const alias = ADMIN_TAB_ALIASES[state.adminTab];
    if (alias) { state.adminTab = alias[0]; state.adminSub[alias[0]] = alias[1]; }
    const tabs = [
      ['physicians', 'Physicians'],
      ['work', 'Work'],
      ['money', 'Money'],
      ['data', 'Data'],
    ];
    const subnav = h('div', { class: 'asc-subnav' },
      tabs.map(([id, label]) => {
        const btn = h('button', {
          class: 'asc-subnav-btn' + (state.adminTab === id ? ' active' : ''),
          onClick: () => { state.adminTab = id; renderAdminView(); },
        }, label);
        // QA (BUG-2): the pending-count badge stays visible at the top level so
        // the backlog is never invisible: it now rides on Physicians (QA lives
        // inside it as a sub-tab).
        if (id === 'physicians') btn.appendChild(h('span', { class: 'asc-badge asc-badge-count', id: 'ascQaBadge', style: 'margin-left:6px', hidden: true }));
        return btn;
      }));

    const body = h('div', { id: 'ascAdminBody' });
    setRoot(h('div', { class: 'asc-wrap' }, subnav, body));
    refreshQaBadge();

    if (state.adminTab === 'physicians') renderAdminPhysiciansSection(body);
    else if (state.adminTab === 'work') renderAdminWorkSection(body);
    else if (state.adminTab === 'money') renderAdminMoneySection(body);
    else if (state.adminTab === 'data') renderAdminDataSection(body);
  }

  // Sub-tab strip shared by the three restructured sections.
  function adminSubnav(section, items) {
    return h('div', { class: 'asc-subnav', style: 'margin-bottom:14px' },
      items.map(([id, label]) => h('button', {
        class: 'asc-subnav-btn' + (state.adminSub[section] === id ? ' active' : ''),
        onClick: () => { state.adminSub[section] = id; renderAdminView(); },
      }, label)));
  }

  function sectionModuleMissing(body, name) {
    body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-inline-error' },
        name + ' failed to load: refresh the page. If it persists, check that the ' +
        'script is included in index.html.'))));
  }

  // Physicians: who supplies our judgment. Tasks and QA live here now, next to
  // the people who produce them.
  function renderAdminPhysiciansSection(body) {
    clear(body);
    body.appendChild(adminSubnav('physicians', [
      // Signups sits FIRST after the roster because it is the front of the
      // funnel: a physician mid-wizard has no account yet, so they appear on
      // none of the other three. Before this tab existed they appeared on no
      // screen at all — the console showed one physician while the founder's
      // inbox filled with signup notifications for people it could not name.
      ['roster', 'Roster'], ['signups', 'Signups'], ['verify', 'Verification'],
      ['qa', 'QA'],
    ]));
    const inner = h('div', {});
    body.appendChild(inner);
    const sub = state.adminSub.physicians;
    if (sub === 'qa') renderAdminQA(inner);
    else if (window.AdminPhysiciansSection) {
      window.AdminPhysiciansSection.render(inner, adminSectionCtx(),
        (sub === 'verify' || sub === 'signups') ? sub : 'roster');
    } else sectionModuleMissing(inner, 'The Physicians section');
  }

  // Work: what gets labeled and how it is going. Task import/generation
  // beside the metrics that report on it.
  function renderAdminWorkSection(body) {
    clear(body);
    body.appendChild(adminSubnav('work', [
      ['tasks', 'Tasks'], ['metrics', 'Metrics'],
    ]));
    const inner = h('div', {});
    body.appendChild(inner);
    if (state.adminSub.work === 'metrics') renderAdminMetrics(inner);
    else renderAdminTasks(inner);
  }

  // Money: the ledger and the referral book. Both were API-only for a while;
  // an admin should never need curl to see what the product owes people.
  function renderAdminMoneySection(body) {
    clear(body);
    body.appendChild(adminSubnav('money', [
      ['earnings', 'Earnings'], ['referrals', 'Referrals'],
    ]));
    const inner = h('div', {});
    body.appendChild(inner);
    if (window.AdminEarningsSection) {
      window.AdminEarningsSection.render(inner, adminSectionCtx(),
        state.adminSub.money === 'referrals' ? 'referrals' : 'earnings');
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

  // Health Systems (inside Data): who supplies our data.
  function renderAdminHealthSection(inner) {
    if (window.AdminHealthSection) window.AdminHealthSection.render(inner, adminSectionCtx());
    else sectionModuleMissing(inner, 'The Health Systems section');
  }

  // Export: what can we sell, and how do we cut it. By-case is the primary
  // view; buyer relationships and past export batches stay reachable beside it.
  function renderAdminExportSection(body) {
    clear(body);
    body.appendChild(adminSubnav('export', [
      ['bycase', 'Export by case'], ['buyers', 'Buyers & Requests'], ['history', 'Export history'],
    ]));
    const inner = h('div', {});
    body.appendChild(inner);
    const sub = state.adminSub.export;
    if (sub === 'buyers') renderAdminBuyers(inner);
    else if (sub === 'history') renderAdminExports(inner);
    else if (window.AdminExportSection) window.AdminExportSection.render(inner, adminSectionCtx());
    else sectionModuleMissing(inner, 'The Export section');
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
          h('td', { class: 'asc-mono' }, (s.submission_id || '').slice(0, 12)),
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
    // There is no default and no third "unset" option. A partner minted with no
    // purpose is a decision nobody made, and the promotion gate would read it as
    // task creation.
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
    const mintTaskBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Send link: task creation');
    const mintBrokerBtn = h('button', { class: 'asc-btn asc-btn-subtle', style: 'margin-left:8px' }, 'Send link: brokering');
    mintTaskBtn.addEventListener('click', () => sendUploadAccess('task_creation'));
    mintBrokerBtn.addEventListener('click', () => sendUploadAccess('brokering'));
    mintButtons.push(mintTaskBtn, mintBrokerBtn);
    const mintCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Send a health system its upload access'),
        h('div', { class: 'asc-card-sub' }, 'The contact receives a username and one-time passphrase by email, signs into the password-protected portal, and uploads. Specialty is determined at ingest: not asked of hospital IT.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Organization'), hsOrg),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Email'), hsEmail)),
        mintTaskBtn, mintBrokerBtn,
        h('div', { class: 'asc-label-hint', style: 'margin-top:8px' },
          'Both links are byte-identical to the recipient. Which button you press is recorded on our side only, and decides whether the data can ever become a task.'),
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
        h('div', { class: 'asc-card-sub' }, (p.index_rationale || {}).reason || '')),
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

  function openCasePlanModal(upload, ic, plan, statusBox) {
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
          { method: 'POST', body: { dry_run: false } });
        overlay.remove();
        clear(statusBox);
        statusBox.appendChild(h('div', { class: 'asc-inline-ok' },
          'Generated ' + r.generated + ' V4 case(s)'
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
      h('div', { class: 'asc-card-sub', style: 'margin-bottom:14px' },
        'Nothing here has been written. Difficulty is measured only when you generate — '
        + 'a band shown as "proposed" is the structural prior, not a frontier failure rate.'),
      list,
      status,
      h('div', { style: 'display:flex;gap:10px;margin-top:16px' },
        allBtn,
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
    const promoteAllBtn = h('button', { class: 'asc-btn asc-btn-primary' }, '✓ Looks good, create the rest (' + (prep.ingested_count || 0) + ')');
    promoteAllBtn.addEventListener('click', async () => {
      promoteAllBtn.setAttribute('disabled', ''); promoteAllBtn.textContent = 'Creating cases…';
      clear(status);
      try {
        const r = await api('/ingestion/uploads/' + upload.upload_id + '/promote-all', { method: 'POST', body: {} });
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
      status,
      h('div', { style: 'display:flex;gap:10px;margin-top:16px' },
        promoteAllBtn,
        h('button', { class: 'asc-btn asc-btn-ghost', style: 'margin-left:auto', onClick: () => overlay.remove() }, 'Cancel')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  function loadingCard(label) {
    return h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), label || 'Loading…'));
  }

  // ─── Admin: Tasks ──────────────────────────────────────────────────────────
  function renderAdminTasks(body) {
    clear(body);
    const tax = state.taxonomy;

    // Paste JSON
    const jsonTa = h('textarea', { class: 'asc-textarea', style: 'min-height:140px;font-family:ui-monospace,Menlo,monospace;font-size:12px',
      placeholder: '[{"specialty":"nephrology","difficulty":"medium","prompt":"…","candidate_answers":[{"id":"A","text":"…"},{"id":"B","text":"…"}],"grounding_mode":"optional"}]' });
    const pasteStatus = h('div', {});
    const pasteCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {}, h('div', { class: 'asc-card-title' }, 'Paste tasks (JSON)'),
        h('div', { class: 'asc-card-sub' }, 'A JSON array, a single task object, or {"tasks":[…]}'))),
      h('div', { class: 'asc-card-pad' },
        jsonTa,
        h('div', { style: 'margin-top:12px;display:flex;gap:10px;align-items:center' },
          h('button', {
            class: 'asc-btn asc-btn-primary', onClick: async () => {
              clear(pasteStatus);
              let parsed;
              try { parsed = JSON.parse(jsonTa.value); }
              catch (e) { pasteStatus.appendChild(h('div', { class: 'asc-inline-error' }, 'Invalid JSON: ' + e.message)); return; }
              let tasks = Array.isArray(parsed) ? parsed : (parsed.tasks ? parsed.tasks : [parsed]);
              try {
                const res = await api('/tasks', { method: 'POST', body: { tasks } });
                pasteStatus.appendChild(h('div', { class: 'asc-inline-ok' }, 'Created ' + res.count + ' task(s).'));
                // V3 (the default flow) serves ONLY difficulty:"hard" tasks. Warn if
                // any uploaded task isn't hard, so it doesn't silently never appear.
                const notHard = tasks.filter((t) => ((t && t.difficulty) || 'medium') !== 'hard').length;
                if (notHard > 0) {
                  pasteStatus.appendChild(h('div', { class: 'asc-inline-warn', style: 'margin-top:8px' },
                    notHard + ' of ' + tasks.length + ' task(s) are not difficulty:"hard" and will NOT appear in the V3 (default) hard-case queue. Set difficulty:"hard" to serve them in V3.'));
                }
                jsonTa.value = '';
                loadTasksTable();
              } catch (e) { pasteStatus.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); }
            },
          }, 'Upload pasted tasks')),
        pasteStatus));

    // File upload
    const fileInput = h('input', { type: 'file', accept: '.json,.csv', class: 'asc-input' });
    const fileStatus = h('div', {});
    const fileCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {}, h('div', { class: 'asc-card-title' }, 'Upload file'),
        h('div', { class: 'asc-card-sub' }, 'JSON or CSV (columns: prompt, specialty, difficulty, answer_a, answer_b, …)'))),
      h('div', { class: 'asc-card-pad' },
        fileInput,
        h('div', { style: 'margin-top:12px' },
          h('button', {
            class: 'asc-btn asc-btn-primary', onClick: async () => {
              clear(fileStatus);
              if (!fileInput.files || !fileInput.files[0]) { fileStatus.appendChild(h('div', { class: 'asc-inline-error' }, 'Choose a file first.')); return; }
              const fd = new FormData();
              fd.append('file', fileInput.files[0]);
              try {
                const res = await api('/tasks/upload-file', { method: 'POST', body: fd, isForm: true });
                fileStatus.appendChild(h('div', { class: 'asc-inline-ok' }, 'Created ' + res.count + ' task(s).'));
                fileInput.value = '';
                loadTasksTable();
              } catch (e) { fileStatus.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); }
            },
          }, 'Upload file')),
        fileStatus));

    // Generate candidates
    const genPrompt = h('textarea', { class: 'asc-textarea', placeholder: 'Clinical prompt to generate two candidate answers for…' });
    const genSpec = h('input', { class: 'asc-input', value: state.user.specialty || 'nephrology' });
    const genDiff = selectFrom(['easy', 'medium', 'hard'], 'medium');
    const genGround = selectFrom(tax.grounding_modes || ['optional', 'required'], 'optional');
    const genCapture = h('input', { type: 'checkbox' });
    const genStatus = h('div', {});
    const genCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {}, h('div', { class: 'asc-card-title' }, 'Generate candidates'),
        h('div', { class: 'asc-card-sub' }, 'Uses the configured LLM to draft two answers (needs an LLM key).'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Prompt'), genPrompt),
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Specialty'), genSpec),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Difficulty'), genDiff),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Grounding'), genGround)),
        h('label', { class: 'asc-checkbox-row', style: 'margin-bottom:12px' }, genCapture, 'Capture reasoning'),
        h('button', {
          class: 'asc-btn asc-btn-primary', onClick: async () => {
            clear(genStatus);
            if (!genPrompt.value.trim()) { genStatus.appendChild(h('div', { class: 'asc-inline-error' }, 'Prompt is required.')); return; }
            try {
              const res = await api('/tasks/generate', {
                method: 'POST', body: {
                  prompt: genPrompt.value.trim(), specialty: genSpec.value.trim() || 'general',
                  difficulty: genDiff.value, capture_reasoning: genCapture.checked,
                  max_labels: 1, grounding_mode: genGround.value,
                },
              });
              genStatus.appendChild(h('div', { class: 'asc-inline-ok' }, 'Generated task ' + res.task_id + '.'));
              genPrompt.value = '';
              loadTasksTable();
            } catch (e) {
              const msg = e.status === 503 ? (e.message || 'Candidate generation unavailable (no LLM key configured).') : e.message;
              genStatus.appendChild(h('div', { class: 'asc-inline-error' }, msg));
            }
          },
        }, 'Generate'),
        genStatus));

    // Seedmaker auto-generation (Mode A): generate N validated tasks (prompt + 2
    // candidates) from the curated seed corpus, as TEXT prompts or structured
    // MULTIMODAL cases (labs + notes the specialist reasons across, Multimodal PRD).
    const agSpecialty = selectFrom(['nephrology', 'cardiology'], 'nephrology');
    const agCaseType = selectFrom(['text', 'multimodal'], 'text');
    const agCount = h('input', { type: 'number', class: 'asc-input', value: '10', min: '1', max: '200' });
    const agDiff = selectFrom(['balanced', 'hard_heavy', 'hard_only'], 'balanced');
    const agGround = selectFrom(tax.grounding_modes || ['optional', 'required'], 'optional');
    const agCapture = h('input', { type: 'checkbox' });
    const agStatus = h('div', {});
    const agNote = h('div', { class: 'asc-card-sub', style: 'margin:8px 0 12px' });
    const agBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Generate tasks');
    // Multimodal cases are definitionally hard + always capture the reasoning
    // trace (that's the value), so those controls don't apply. Reflect that.
    function syncCaseType() {
      const mm = agCaseType.value === 'multimodal';
      agDiff.disabled = mm; agCapture.disabled = mm;
      agBtn.textContent = mm ? 'Generate multimodal cases' : 'Generate tasks';
      agNote.textContent = mm
        ? 'Multimodal: synthesizes a PHI-free clinical case (lab panels + notes) the specialist reasons across. Always hard + reasoning capture; served in the V3 queue with a structured case panel. Needs an LLM key.'
        : 'Synthesizes novel, hard prompts from the seed corpus + two candidate answers, quality-gated before they enter the queue. Needs an LLM key.';
    }
    agCaseType.addEventListener('change', syncCaseType);
    const autoGenCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Auto-generate tasks (Seedmaker, SYNTHETIC, V1–V3)'),
        h('div', { class: 'asc-card-sub' }, 'Text prompts or structured multimodal cases, all SYNTHETIC (V3 tier). Real patient cases (V4) come only from the Ingestion tab. Quality-gated before they enter the queue.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Specialty'), agSpecialty),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Case type'), agCaseType),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'How many'), agCount)),
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Difficulty mix'), agDiff),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Grounding'), agGround),
          h('div', {})),
        h('label', { class: 'asc-checkbox-row', style: 'margin-bottom:12px' }, agCapture, 'Capture reasoning steps'),
        agNote,
        agBtn,
        agStatus));
    syncCaseType();
    agBtn.addEventListener('click', async () => {
      clear(agStatus);
      const count = Math.max(1, parseInt(agCount.value, 10) || 1);
      const multimodal = agCaseType.value === 'multimodal';
      const mixMap = {
        balanced: { hard: 0.6, medium: 0.4 },
        hard_heavy: { hard: 0.8, medium: 0.2 },
        hard_only: { hard: 1.0 },
      };
      agBtn.setAttribute('disabled', '');
      agStatus.appendChild(loadingCard('Generating ' + count + ' ' + (multimodal ? 'multimodal case' : 'task') + '(s)… this calls the LLM and may take a moment.'));
      try {
        const res = await api('/generation/' + encodeURIComponent(agSpecialty.value), {
          method: 'POST', body: {
            count,
            difficulty_mix: multimodal ? null : (mixMap[agDiff.value] || null),
            capture_reasoning: multimodal ? true : agCapture.checked,
            grounding_mode: agGround.value,
            multimodal,
          },
        });
        clear(agStatus);
        agStatus.appendChild(h('div', { class: 'asc-inline-ok' },
          'Accepted ' + res.accepted + ' / ' + (res.accepted + (res.shortfall || 0)) + ' requested.'
          + (res.shortfall ? ' Shortfall ' + res.shortfall + '.' : '')));
        const dropped = res.dropped || {};
        const dkeys = Object.keys(dropped);
        if (dkeys.length) {
          agStatus.appendChild(h('div', { class: 'asc-card-sub', style: 'margin-top:8px' },
            'Dropped: ' + dkeys.map((k) => k.replace(/_/g, ' ') + ' (' + dropped[k] + ')').join(', ')));
        }
        loadTasksTable();
        loadGenerationJobs();
      } catch (e) {
        clear(agStatus);
        const msg = e.status === 503
          ? (e.message || 'Auto-generation unavailable: no LLM key configured.')
          : e.message;
        agStatus.appendChild(h('div', { class: 'asc-inline-error' }, msg));
      } finally {
        agBtn.removeAttribute('disabled');
      }
    });

    // Load GOLD cases (Two-Model PRD Workstream C, the "load gold" half of the
    // load-vs-generate split). Distinct from auto-generate: inserts the ratified,
    // hand-authored seed cases with NO LLM required, the reliable way to populate
    // the V3 queue immediately, independent of the LLM key.
    const goldSpecialty = selectFrom(['nephrology'], 'nephrology');
    const goldStatus = h('div', {});
    const goldBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Load gold cases');
    goldBtn.addEventListener('click', async () => {
      clear(goldStatus);
      goldBtn.setAttribute('disabled', '');
      goldStatus.appendChild(loadingCard('Loading ratified gold cases…'));
      try {
        const res = await api('/generation/' + encodeURIComponent(goldSpecialty.value) + '/load-gold', { method: 'POST' });
        clear(goldStatus);
        goldStatus.appendChild(h('div', { class: 'asc-inline-ok' },
          'Loaded ' + (res.loaded || 0) + ' new, skipped ' + (res.skipped || 0) + ' existing'
          + ' (' + (res.total || 0) + ' gold total). Multimodal in queue: ' + (res.multimodal_in_queue || 0) + '.'));
        loadTasksTable();
        loadGenerationJobs();
      } catch (e) {
        clear(goldStatus);
        goldStatus.appendChild(h('div', { class: 'asc-inline-error' }, e.message || 'Could not load gold cases.'));
      } finally {
        goldBtn.removeAttribute('disabled');
      }
    });
    const goldCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Load gold cases (RATIFIED, no LLM needed)'),
        h('div', { class: 'asc-card-sub' }, 'Insert the hand-authored, clinician-ratified multimodal seed cases (real labs + EHR + an authored A/B pair) straight into the V3 queue. Idempotent: safe to click repeatedly. Use this to populate V3 without an LLM key; use "Auto-generate" above for NOVEL cases.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Specialty'), goldSpecialty),
          h('div', {}), h('div', {})),
        goldBtn,
        goldStatus));

    const corpusCard = h('div', { class: 'asc-card', id: 'ascSeedCorpus' }, loadingCard('Loading seed corpus…'));
    const jobsCard = h('div', { class: 'asc-card', id: 'ascGenJobs' }, loadingCard('Loading generation jobs…'));
    const tableCard = h('div', { class: 'asc-card', id: 'ascTasksTable' }, loadingCard('Loading tasks…'));

    body.appendChild(h('div', { class: 'asc-cols-2' }, pasteCard, fileCard));
    body.appendChild(genCard);
    body.appendChild(autoGenCard);
    body.appendChild(goldCard);
    body.appendChild(h('div', { class: 'asc-cols-2' }, corpusCard, jobsCard));
    body.appendChild(tableCard);
    loadTasksTable();
    loadSeedCorpus();
    loadGenerationJobs();
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

  async function loadSeedCorpus() {
    const card = document.getElementById('ascSeedCorpus');
    if (!card) return;
    try {
      const m = await api('/generation/seed-corpus?specialty=nephrology');
      clear(card);
      const ratBadge = m.ratified
        ? h('span', { class: 'asc-badge asc-badge-primary' }, 'ratified')
        : h('span', { class: 'asc-badge asc-badge-amber' }, 'unratified');
      card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Seed corpus'),
        h('div', { class: 'asc-card-sub' }, m.version + ' · ' + m.total + ' prompts · ', ratBadge))));
      const rows = (m.taxonomy || []).map((b) => h('tr', {},
        h('td', {}, b.label || b.id),
        h('td', {}, String(b.have != null ? b.have : 0)),
        h('td', {}, String(b.target_count != null ? b.target_count : 'n/a')),
        h('td', {}, b.min_difficulty || 'n/a')));
      card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['Bucket', 'Have', 'Target', 'Min difficulty'].map((c) => h('th', {}, c)))),
        h('tbody', {}, rows))));
      if (!m.ratified) {
        card.appendChild(h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-card-sub' }, 'Note: ' + (m.review_status || 'pending clinician review') + '. Ratify before sale.')));
      }
    } catch (e) {
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message)));
    }
  }

  async function loadGenerationJobs() {
    const card = document.getElementById('ascGenJobs');
    if (!card) return;
    try {
      const data = await api('/generation/jobs');
      const jobs = data.jobs || [];
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-head' },
        h('div', { class: 'asc-card-title' }, 'Generation jobs (' + jobs.length + ')')));
      if (!jobs.length) { card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-card-sub' }, 'No generation runs yet.'))); return; }
      const rows = jobs.slice(0, 50).map((j) => {
        const dropped = j.dropped || {};
        const dkeys = Object.keys(dropped).filter((k) => dropped[k] > 0);
        const dsum = dkeys.reduce((a, k) => a + dropped[k], 0);
        // Per-reason breakdown, permanently visible (Multimodal Debug PRD P1.5):
        // a batch that drops to 0 accepted must SHOW why (case_incoherent,
        // multimodal_not_necessary, near_duplicate, …), not just a count; that's
        // the difference between "broken" and "thresholds need tuning".
        const breakdown = dkeys.length
          ? dkeys.sort((a, b) => dropped[b] - dropped[a])
              .map((k) => k.replace(/_/g, ' ') + ' ' + dropped[k]).join(' · ')
          : 'n/a';
        const zeroYield = !j.accepted && dsum > 0;
        return h('tr', {},
          h('td', {}, fmtDate(j.created_at)),
          h('td', {}, zeroYield
            ? h('span', { class: 'asc-badge asc-badge-amber' }, '0 / ' + String(j.requested_n))
            : String(j.accepted) + ' / ' + String(j.requested_n)),
          h('td', {}, String(dsum)),
          h('td', { class: 'asc-card-sub', style: 'max-width:340px' }, breakdown));
      });
      card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['When', 'Accepted / Requested', 'Dropped', 'Why dropped'].map((c) => h('th', {}, c)))),
        h('tbody', {}, rows))));
    } catch (e) {
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message)));
    }
  }

  async function loadTasksTable() {
    const card = document.getElementById('ascTasksTable');
    if (!card) return;
    clear(card);
    card.appendChild(loadingCard('Loading tasks…'));
    try {
      const data = await api('/tasks');
      const tasks = data.tasks || [];
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-head' },
        h('div', { class: 'asc-card-title' }, 'Tasks (' + tasks.length + ')')));
      if (!tasks.length) { card.appendChild(h('div', { class: 'asc-empty' }, h('p', {}, 'No tasks yet.'))); return; }
      const rows = tasks.slice(0, 200).map((t) => h('tr', {},
        h('td', { class: 'asc-mono' }, (t.task_id || '').slice(0, 10)),
        h('td', {}, h('span', { class: 'asc-badge asc-badge-primary' }, t.specialty || 'n/a')),
        // Modality badge (Multimodal Debug PRD P0.3): multimodal batches must be
        // distinguishable at a glance, not invisible among text tasks.
        h('td', {}, (t.modality || 'text') === 'multimodal'
          ? h('span', { class: 'asc-badge asc-badge-accent' }, 'multimodal')
          : 'text'),
        // Case source + version (EHR PRD §9.5): an admin must never mistake a
        // REAL case for a synthetic one at a glance. Real ⇒ V4, always.
        h('td', {}, t.case_source === 'real_deid'
          ? h('span', { class: 'asc-badge asc-badge-real' }, 'real · V4')
          : (t.case_source ? 'synthetic' : 'n/a')),
        h('td', {}, t.difficulty || 'n/a'),
        h('td', {}, (t.prompt || '').slice(0, 90) + ((t.prompt || '').length > 90 ? '…' : '')),
        h('td', {}, t.grounding_mode === 'required' ? h('span', { class: 'asc-badge asc-badge-amber' }, 'required') : 'optional'),
        h('td', {}, String(t.submission_count != null ? t.submission_count : 0)),
        h('td', {}, t.status === 'prompt_flagged'
          ? h('span', { class: 'asc-badge asc-badge-amber' }, 'prompt flagged')
          : (t.status || 'n/a')),
        // Frontier-model failure capture (FEAT-1) + two-frontier provenance (§4.2).
        h('td', {}, baselineCell(t))));
      card.appendChild(h('div', { class: 'asc-table-wrap' },
        h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            ['ID', 'Specialty', 'Modality', 'Case source', 'Difficulty', 'Prompt', 'Grounding', 'Labels', 'Status', 'Baselines'].map((c) => h('th', {}, c)))),
          h('tbody', {}, rows))));
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

  // ─── Admin: Buyers & Requests ──────────────────────────────────────────────
  function renderAdminBuyers(body) {
    clear(body);
    const tax = state.taxonomy;

    // Create buyer
    const bName = h('input', { class: 'asc-input', placeholder: 'Acme Frontier Labs' });
    const bContact = h('input', { class: 'asc-input', placeholder: 'contact@acme.ai' });
    const bProfile = selectFrom(profileNames(), 'default');
    const bNotes = h('input', { class: 'asc-input', placeholder: 'Notes (optional)' });
    const bStatus = h('div', {});
    const buyerCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'New buyer')),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-form-row' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Name'), bName),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Contact'), bContact)),
        h('div', { class: 'asc-form-row' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Default export profile'), bProfile),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Notes'), bNotes)),
        h('button', {
          class: 'asc-btn asc-btn-primary', onClick: async () => {
            clear(bStatus);
            if (!bName.value.trim()) { bStatus.appendChild(h('div', { class: 'asc-inline-error' }, 'Name is required.')); return; }
            try {
              await api('/buyers', { method: 'POST', body: { name: bName.value.trim(), contact: bContact.value.trim(), export_profile: bProfile.value, notes: bNotes.value.trim() } });
              bStatus.appendChild(h('div', { class: 'asc-inline-ok' }, 'Buyer created.'));
              bName.value = bContact.value = bNotes.value = '';
              renderAdminBuyers(body);
            } catch (e) { bStatus.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); }
          },
        }, 'Create buyer'),
        bStatus));

    // ── Primary flow: export selected organizations and send to a buyer ──────
    const sendCard = h('div', { class: 'asc-card', id: 'ascSendToBuyer' }, loadingCard('Loading organizations…'));
    const deliveriesCard = h('div', { class: 'asc-card', id: 'ascBuyerDeliveries' }, loadingCard('Loading deliveries…'));
    body.appendChild(sendCard);
    body.appendChild(deliveriesCard);

    const buyersListCard = h('div', { class: 'asc-card', id: 'ascBuyersList' }, loadingCard('Loading buyers…'));
    const reqCard = h('div', { class: 'asc-card', id: 'ascReqForm' }, loadingCard('Loading…'));
    const reqListCard = h('div', { class: 'asc-card', id: 'ascReqList' }, loadingCard('Loading requests…'));

    body.appendChild(h('div', { class: 'asc-cols-2' }, buyerCard, buyersListCard));
    body.appendChild(reqCard);
    body.appendChild(reqListCard);

    loadSendToBuyer();
    loadBuyerDeliveries();
    loadBuyersAndRequests();
  }

  // Send-to-buyer: pick organizations (checkbox multi-select) + a time window +
  // a data format, then deliver to a buyer's secure workspace.
  async function loadSendToBuyer() {
    const card = document.getElementById('ascSendToBuyer');
    if (!card) return;
    let orgs = [];
    try { orgs = (await api('/organizations')).organizations || []; }
    catch (e) { clear(card); card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message))); return; }
    clear(card);
    card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
      h('div', { class: 'asc-card-title' }, 'Export & send data to a buyer'),
      h('div', { class: 'asc-card-sub' }, 'Select one or more organizations, choose a time window and format, then deliver the dataset straight to a buyer’s secure workspace.'))));

    if (!orgs.length) { card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-card-sub' }, 'No organizations with data yet.'))); return; }

    const selected = new Set();
    const rows = orgs.map((o) => {
      const cb = h('input', { type: 'checkbox' });
      cb.addEventListener('change', () => { if (cb.checked) selected.add(o.organization); else selected.delete(o.organization); syncSummary(); });
      return h('label', { class: 'asc-checkbox-row', style: 'padding:8px 0;border-bottom:1px solid var(--asc-line)' },
        cb,
        h('span', { style: 'flex:1' },
          h('span', { style: 'font-weight:600' }, o.organization),
          h('span', { class: 'asc-card-sub' }, '  ' + o.contributor_count + ' contributor(s) · ' + o.record_count + ' record(s)')));
    });

    // Time window (Pacific): all time / this week / today. Output is always JSONL.
    const winSel = selectFrom(['all time', 'this week', 'today'], 'all time');
    const summary = h('div', { class: 'asc-card-sub', style: 'margin:10px 0' });
    const sendBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Send to buyer');
    function syncSummary() {
      summary.textContent = selected.size
        ? (selected.size + ' organization(s) selected: ' + Array.from(selected).join(', '))
        : 'No organizations selected.';
      if (selected.size) sendBtn.removeAttribute('disabled'); else sendBtn.setAttribute('disabled', '');
    }
    sendBtn.setAttribute('disabled', '');
    sendBtn.addEventListener('click', () => {
      if (!selected.size) return;
      const scope = {};
      if (winSel.value === 'today') scope.since = windowSinceISO(0);
      else if (winSel.value === 'this week') scope.since = windowSinceISO(6);
      openSendToBuyerModal(Array.from(selected), scope, winSel.value);
    });

    card.appendChild(h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-form-row', style: 'align-items:flex-end' },
        h('div', { class: 'asc-field', style: 'margin-bottom:0' }, h('label', { class: 'asc-label' }, 'Time window (Pacific)'), winSel),
        h('div', { class: 'asc-field', style: 'margin-bottom:0' }, h('label', { class: 'asc-label' }, 'Data format'),
          h('div', { class: 'asc-badge asc-badge-gray', style: 'align-self:start;padding:8px 12px' }, 'JSONL'))),
      h('div', { style: 'margin-top:14px' }, h('label', { class: 'asc-label' }, 'Organizations'), h('div', {}, rows)),
      summary,
      sendBtn));
    syncSummary();
  }

  function openSendToBuyerModal(orgs, scope, windowLabel) {
    const overlay = h('div', { class: 'call-team-overlay is-open', onClick: (e) => { if (e.target === overlay) overlay.remove(); } });
    const name = h('input', { class: 'asc-input', placeholder: 'Acme Frontier Labs' });
    const email = h('input', { class: 'asc-input', type: 'email', placeholder: 'buyer@acme.ai' });
    const fmt = h('input', { class: 'asc-input', value: 'JSONL', readonly: 'readonly' });
    const notes = h('textarea', { class: 'asc-textarea', placeholder: 'Additional notes for the buyer (optional)' });
    const status = h('div', { style: 'margin-top:10px' });
    const sendBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Send to buyer');
    sendBtn.addEventListener('click', async () => {
      clear(status);
      if (!name.value.trim()) { status.appendChild(h('div', { class: 'asc-inline-error' }, 'Buyer name is required.')); return; }
      if (!email.value.trim()) { status.appendChild(h('div', { class: 'asc-inline-error' }, 'Buyer email is required.')); return; }
      sendBtn.setAttribute('disabled', ''); sendBtn.textContent = 'Sending…';
      try {
        const r = await api('/admin/buyer-deliveries', { method: 'POST', body: Object.assign({
          buyer_name: name.value.trim(), buyer_email: email.value.trim(),
          organizations: orgs, profile: 'default',
          note: notes.value.trim() || null,
        }, scope) });
        overlay.remove();
        toast('Sent ' + (r.record_count || 0) + ' record(s) to ' + r.buyer_email + (r.email_sent ? '. Email delivered.' : ', but email failed.'), r.email_sent ? 'success' : 'info');
        loadBuyerDeliveries();
      } catch (e) {
        clear(status);
        const msg = e.status === 400 ? (e.message || 'Nothing to export for that selection/window.')
          : (e.status === 503 ? 'Email is not configured. Set SendGrid/SMTP (or EMAIL_DEV_MODE=1 for local).'
          : (e.status === 422 ? 'Export blocked: ' + e.message : (e.message || 'Send failed')));
        status.appendChild(h('div', { class: 'asc-inline-error' }, msg));
        sendBtn.removeAttribute('disabled'); sendBtn.textContent = 'Send to buyer';
      }
    });
    const popup = h('div', { class: 'call-team-popup', style: 'max-width:560px', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'call-team-title' }, 'Send to buyer'),
      h('div', { class: 'call-team-sub' }, orgs.length + ' organization(s) · ' + windowLabel + ' · JSONL'),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Buyer name'), name),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Buyer email'), email),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Data format'), fmt),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Additional notes'), notes),
      h('div', { class: 'asc-label-hint' }, 'The buyer receives an email with login credentials and a link to their secure workspace. Every dataset you send to this email appears in that workspace.'),
      status,
      h('div', { style: 'display:flex;gap:10px;margin-top:14px' },
        sendBtn,
        h('button', { class: 'asc-btn asc-btn-ghost', style: 'margin-left:auto', onClick: () => overlay.remove() }, 'Cancel')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  async function loadBuyerDeliveries() {
    const card = document.getElementById('ascBuyerDeliveries');
    if (!card) return;
    try {
      const data = await api('/admin/buyer-deliveries');
      const deliveries = data.deliveries || [];
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'Delivery history (' + deliveries.length + ')')));
      if (!deliveries.length) { card.appendChild(h('div', { class: 'asc-empty' }, h('p', {}, 'No datasets delivered yet.'))); return; }
      const verMix = (bpv) => { const keys = Object.keys(bpv || {}); return keys.length ? keys.sort().map((k) => ascVerLabel(k) + ' ' + bpv[k]).join(' · ') : 'n/a'; };
      const rows = deliveries.map((d) => h('tr', {},
        h('td', {}, fmtDate(d.sent_at)),
        h('td', { class: 'asc-mono' }, d.buyer_email),
        h('td', { style: 'max-width:220px' }, d.label || 'n/a'),
        h('td', {}, String(d.record_count != null ? d.record_count : 'n/a')),
        h('td', {}, d.data_format || 'n/a'),
        h('td', {}, verMix(d.by_portal_version))));
      card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['Sent (PT)', 'Buyer', 'Organizations', 'Records', 'Format', 'Version'].map((c) => h('th', {}, c)))),
        h('tbody', {}, rows))));
    } catch (e) {
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message)));
    }
  }

  async function loadBuyersAndRequests() {
    let buyers = [], requests = [];
    try { buyers = (await api('/buyers')).buyers || []; } catch (e) { /* */ }
    try { requests = (await api('/buyer-requests')).buyer_requests || []; } catch (e) { /* */ }

    // Buyers list
    const bl = document.getElementById('ascBuyersList');
    if (bl) {
      clear(bl);
      bl.appendChild(h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'Buyers (' + buyers.length + ')')));
      if (!buyers.length) bl.appendChild(h('div', { class: 'asc-empty' }, h('p', {}, 'No buyers yet.')));
      else bl.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['Name', 'Contact', 'Profile'].map((c) => h('th', {}, c)))),
        h('tbody', {}, buyers.map((b) => h('tr', {},
          h('td', {}, b.name), h('td', {}, b.contact || 'n/a'), h('td', {}, b.export_profile || 'default')))))));
    }

    // Request form
    const rf = document.getElementById('ascReqForm');
    if (rf) renderBuyerRequestForm(rf, buyers);

    // Requests list
    const rl = document.getElementById('ascReqList');
    if (rl) {
      clear(rl);
      rl.appendChild(h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'Buyer requests (' + requests.length + ')')));
      if (!requests.length) rl.appendChild(h('div', { class: 'asc-empty' }, h('p', {}, 'No buyer requests yet.')));
      else {
        const tbody = h('tbody', {}, requests.map((r) => {
          const c = r.constraints || {};
          return h('tr', {},
            h('td', { class: 'asc-mono' }, (r.request_id || '').slice(0, 10)),
            h('td', {}, r.source || 'n/a'),
            h('td', {}, (c.specialty || 'n/a') + ' / ' + (c.difficulty || 'n/a')),
            h('td', {}, c.grounding_mode === 'required' ? h('span', { class: 'asc-badge asc-badge-amber' }, 'required') : 'optional'),
            h('td', {}, h('span', { class: 'asc-badge asc-badge-gray' }, r.status || 'new')),
            h('td', {}, h('button', { class: 'asc-btn-link', onClick: () => openBatchDialog(r) }, 'New batch')));
        }));
        rl.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {}, ['ID', 'Source', 'Spec / Diff', 'Grounding', 'Status', ''].map((c) => h('th', {}, c)))),
          tbody)));
      }
    }
  }

  function renderBuyerRequestForm(card, buyers) {
    clear(card);
    const tax = state.taxonomy;
    card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {}, h('div', { class: 'asc-card-title' }, 'New buyer request'),
      h('div', { class: 'asc-card-sub' }, 'Define constraints and (optionally) attach prompts to grade.'))));
    if (!buyers.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, 'Create a buyer first.')));
      return;
    }
    const buyerSel = h('select', { class: 'asc-select' }, ...buyers.map((b) => h('option', { value: b.buyer_id }, b.name)));
    const sourceSel = selectFrom(tax.task_sources || ['internal_prompt_bank', 'lab_supplied'], 'internal_prompt_bank');
    const profileSel = selectFrom(profileNames(), 'default');
    const specInput = h('input', { class: 'asc-input', placeholder: 'nephrology' });
    const diffSel = selectFrom(['', 'easy', 'medium', 'hard'], '');
    const groundSel = selectFrom(tax.grounding_modes || ['optional', 'required'], 'optional');
    const captureCb = h('input', { type: 'checkbox' });
    const volInput = h('input', { class: 'asc-input', type: 'number', min: '0', placeholder: 'e.g. 50' });
    const maxLabels = h('input', { class: 'asc-input', type: 'number', min: '1', value: '1' });
    const promptsTa = h('textarea', { class: 'asc-textarea', placeholder: 'Optional prompts JSON: [{"prompt":"…","candidate_answers":[…]}]', style: 'font-family:ui-monospace,Menlo,monospace;font-size:12px' });
    const note = h('input', { class: 'asc-input', placeholder: 'Note (optional)' });
    const status = h('div', {});

    card.appendChild(h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-form-row-3' },
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Buyer'), buyerSel),
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Source'), sourceSel),
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Export profile'), profileSel)),
      h('div', { class: 'asc-form-row-3' },
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Specialty'), specInput),
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Difficulty'), diffSel),
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Grounding mode'), groundSel)),
      h('div', { class: 'asc-form-row-3' },
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Volume'), volInput),
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Max labels / task'), maxLabels),
        h('div', { class: 'asc-field' }, h('label', { class: 'asc-checkbox-row', style: 'margin-top:26px' }, captureCb, 'Capture reasoning'))),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Prompts ', h('span', { class: 'asc-label-hint' }, '(optional JSON)')), promptsTa),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Note'), note),
      h('button', {
        class: 'asc-btn asc-btn-primary', onClick: async () => {
          clear(status);
          let prompts = [];
          if (promptsTa.value.trim()) {
            try { const p = JSON.parse(promptsTa.value); prompts = Array.isArray(p) ? p : (p.tasks || p.prompts || [p]); }
            catch (e) { status.appendChild(h('div', { class: 'asc-inline-error' }, 'Prompts JSON invalid: ' + e.message)); return; }
          }
          const reqBody = {
            buyer_id: buyerSel.value, source: sourceSel.value, export_profile: profileSel.value,
            specialty: specInput.value.trim() || null, difficulty: diffSel.value || null,
            capture_reasoning: captureCb.checked, grounding_mode: groundSel.value,
            volume: volInput.value ? parseInt(volInput.value, 10) : null,
            max_labels: parseInt(maxLabels.value || '1', 10), prompts, note: note.value.trim() || null,
          };
          try {
            await api('/buyer-requests', { method: 'POST', body: reqBody });
            status.appendChild(h('div', { class: 'asc-inline-ok' }, 'Buyer request created.'));
            loadBuyersAndRequests();
          } catch (e) { status.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); }
        },
      }, 'Create request'),
      status));
  }

  function openBatchDialog(req) {
    const overlay = h('div', { class: 'call-team-overlay is-open', onClick: (e) => { if (e.target === overlay) overlay.remove(); } });
    const c = req.constraints || {};
    const countInput = h('input', { class: 'asc-input', type: 'number', min: '0', value: String(c.volume || 5) });
    const promptsTa = h('textarea', { class: 'asc-textarea', placeholder: 'Optional prompts JSON (or prompts+responses). Leave empty to use the internal bank with the count above.', style: 'font-family:ui-monospace,Menlo,monospace;font-size:12px' });
    const status = h('div', {});
    const popup = h('div', { class: 'call-team-popup', style: 'max-width:560px', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'call-team-title' }, 'New batch from request'),
      h('div', { class: 'call-team-sub' }, 'Request ' + (req.request_id || '').slice(0, 10) + ' · source ' + (req.source || 'n/a')),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'From internal bank: count'), countInput),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'From uploaded prompts ', h('span', { class: 'asc-label-hint' }, '(optional JSON, overrides count)')), promptsTa),
      status,
      h('div', { style: 'display:flex;gap:10px;margin-top:8px' },
        h('button', {
          class: 'asc-btn asc-btn-primary', onClick: async () => {
            clear(status);
            let prompts = [];
            if (promptsTa.value.trim()) {
              try { const p = JSON.parse(promptsTa.value); prompts = Array.isArray(p) ? p : (p.tasks || p.prompts || [p]); }
              catch (e) { status.appendChild(h('div', { class: 'asc-inline-error' }, 'Prompts JSON invalid: ' + e.message)); return; }
            }
            const reqBody = { count: parseInt(countInput.value || '0', 10), prompts };
            try {
              const res = await api('/buyer-requests/' + req.request_id + '/batch', { method: 'POST', body: reqBody });
              toast('Batch created: ' + res.count + ' task(s)', 'success');
              overlay.remove();
              loadBuyersAndRequests();
            } catch (e) { status.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); }
          },
        }, 'Create batch'),
        h('button', { class: 'asc-btn asc-btn-ghost', onClick: () => overlay.remove() }, 'Cancel')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  // ─── Admin: QA queue ───────────────────────────────────────────────────────
  // (QA Queue tab removed from the admin console. The QA pipeline still runs
  // server-side; the Exports tab surfaces a one-click "approve pending & export".)

  // ─── Admin: Exports ────────────────────────────────────────────────────────
  // One-click export. Default packages the fresh (export_ready) backlog; when
  // there's none left but records have already shipped, re-export everything so
  // the bundle is always retrievable. Downloads the training-ready zip.
  async function quickExportAll(btn, statusBox, includeExported) {
    const orig = btn.textContent;
    btn.setAttribute('disabled', '');
    btn.textContent = 'Packaging…';
    clear(statusBox);
    try {
      const manifest = await api('/exports', {
        method: 'POST',
        body: { profile: 'default', include_exported: !!includeExported },
      });
      const n = manifest.record_count != null ? manifest.record_count : 0;
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' },
        'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + '. Downloading…'));
      await downloadExport(manifest.export_id);
      loadExportsHistory();
      refreshExportReadyCount();
    } catch (e) {
      const msg = e.status === 400
        ? 'Nothing to export yet. Complete an evaluation first.'
        : (e.status === 422 ? 'Schema validation failed: ' + e.message : (e.message || 'Export failed'));
      statusBox.appendChild(h('div', { class: 'asc-inline-error' }, msg));
    } finally {
      btn.removeAttribute('disabled');
      btn.textContent = orig;
    }
  }

  // Approve everything stuck in QA, then export: the "label -> export now" path.
  async function approveAllAndExport(btn, statusBox) {
    const orig = btn.textContent;
    btn.setAttribute('disabled', '');
    btn.textContent = 'Approving QA…';
    clear(statusBox);
    try {
      const res = await api('/qa/approve-all', { method: 'POST' });
      const k = res.approved != null ? res.approved : 0;
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' },
        'Approved ' + k + ' submission' + (k === 1 ? '' : 's') + ' from QA. Exporting…'));
      await quickExportAll(btn, statusBox, false);
    } catch (e) {
      statusBox.appendChild(h('div', { class: 'asc-inline-error' }, e.message || 'Approve failed'));
      btn.removeAttribute('disabled');
      btn.textContent = orig;
    }
  }

  // Reflect the live backlog and explain a 0 ("in QA" / "already exported" / "no data").
  async function refreshExportReadyCount() {
    const countEl = document.getElementById('ascExportReadyCount');
    const noteEl = document.getElementById('ascExportReadyNote');
    const btn = document.getElementById('ascQuickExportBtn');
    const statusBox = () => document.getElementById('ascQuickExportStatus');
    if (!countEl || !btn) return;
    let s;
    try { s = await api('/stats'); } catch (e) { return; }
    const waiting = s.exportable_records || 0;
    const exported = s.exported_records || 0;
    const total = s.total_records || 0;
    const qaPending = s.qa_pending || 0;
    countEl.textContent = String(waiting);
    btn.removeAttribute('disabled');
    if (noteEl) clear(noteEl);
    if (waiting > 0) {
      btn.textContent = '⬇ Export all ready records';
      btn.onclick = () => quickExportAll(btn, statusBox(), false);
      if (noteEl && qaPending > 0) noteEl.appendChild(h('span', {},
        '(' + qaPending + ' more submission' + (qaPending === 1 ? ' is' : 's are') + ' in QA review. Approve in the QA Queue tab to add them.)'));
    } else if (qaPending > 0) {
      // The usual reason a just-labeled submission isn't exportable: it was
      // sampled/flagged into QA. Let the admin release + export in one click.
      btn.textContent = '✓ Approve ' + qaPending + ' pending & export';
      btn.onclick = () => approveAllAndExport(btn, statusBox());
      if (noteEl) noteEl.appendChild(h('span', {},
        qaPending + ' submission' + (qaPending === 1 ? '' : 's') + ' from your evaluators ' +
        (qaPending === 1 ? 'is' : 'are') + ' held in QA review (quality sampling). Approve to make ' +
        (qaPending === 1 ? 'it' : 'them') + ' exportable, or review individually in the QA Queue tab.'));
    } else if (exported > 0) {
      btn.textContent = '⬇ Re-export all records (' + exported + ')';
      btn.onclick = () => quickExportAll(btn, statusBox(), true);
      if (noteEl) noteEl.appendChild(h('span', {},
        'All ' + exported + ' record' + (exported === 1 ? '' : 's') + ' already exported. Re-package to download again, or grab any prior bundle from the history below.'));
    } else {
      btn.textContent = '⬇ Export all ready records';
      btn.setAttribute('disabled', '');
      btn.onclick = null;
      if (noteEl) noteEl.appendChild(h('span', {},
        total === 0
          ? 'No completed evaluations yet. Once a clinician submits one, it appears here to export.'
          : 'Nothing ready to export right now.'));
    }
  }

  function renderAdminExports(body) {
    clear(body);
    state.browse.export = { level: 'orgs', org: null, idHashed: null, contributor: null };

    // ── One-click export (the common path) ──────────────────────────────────
    const quickStatus = h('div', { id: 'ascQuickExportStatus', style: 'margin-top:12px' });
    const quickBtn = h('button', {
      class: 'asc-btn asc-btn-primary asc-btn-lg', id: 'ascQuickExportBtn', disabled: true,
    }, '⬇ Export all ready records');
    const quickCard = h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title' }, 'Ready to export'),
      h('div', { class: 'asc-card-sub', style: 'margin-bottom:14px' },
        'Records that are completed and QA-cleared, packaged as a training-ready bundle ',
        h('span', { class: 'asc-mono' }, '(records.jsonl'), ' + data dictionary, datasheet & quality report).'),
      h('div', { style: 'display:flex;align-items:center;gap:16px;flex-wrap:wrap' },
        h('div', { style: 'font-size:34px;font-weight:700;line-height:1', id: 'ascExportReadyCount' }, '…'),
        h('span', { class: 'asc-label-hint' }, 'record(s) waiting'),
        quickBtn),
      h('div', { class: 'asc-label-hint', id: 'ascExportReadyNote', style: 'margin-top:10px' }),
      quickStatus);
    body.appendChild(quickCard);
    refreshExportReadyCount();

    // ── Export by product version cohort filter (V1 / V2 / V3) ──────────────
    const cohortStatus = h('div', { style: 'margin-top:12px' });
    const cohortSel = selectFrom(['both', 'v3', 'v2', 'v1'], 'both');
    const cohortInclExported = h('input', { type: 'checkbox' });
    const cohortBtn = h('button', { class: 'asc-btn asc-btn-primary' }, '⬇ Export cohort');
    cohortBtn.addEventListener('click', async () => {
      const sel = cohortSel.value;
      const orig = cohortBtn.textContent;
      cohortBtn.setAttribute('disabled', ''); cohortBtn.textContent = 'Packaging…';
      clear(cohortStatus);
      try {
        const body2 = { profile: 'default', include_exported: cohortInclExported.checked };
        if (sel !== 'both') body2.portal_version = sel;
        const manifest = await api('/exports', { method: 'POST', body: body2 });
        const n = manifest.record_count != null ? manifest.record_count : 0;
        const bpv = (manifest.counts || {}).by_portal_version || {};
        const mix = Object.keys(bpv).map((k) => k + ':' + bpv[k]).join(' · ') || 'n/a';
        cohortStatus.appendChild(h('div', { class: 'asc-inline-ok' },
          'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + ' (' + mix + '). Downloading…'));
        await downloadExport(manifest.export_id);
        loadExportsHistory();
        refreshExportReadyCount();
      } catch (e) {
        const msg = e.status === 400
          ? 'No records match that version/filter yet.'
          : (e.status === 422 ? 'Schema validation failed: ' + e.message : (e.message || 'Export failed'));
        cohortStatus.appendChild(h('div', { class: 'asc-inline-error' }, msg));
      } finally { cohortBtn.removeAttribute('disabled'); cohortBtn.textContent = orig; }
    });
    body.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title' }, 'Export by product version'),
      h('div', { class: 'asc-card-sub', style: 'margin-bottom:14px' },
        'Package a single cohort: V2 (assisted), V1 (classic), or both. Every record is also stamped with its source version.'),
      h('div', { class: 'asc-form-row', style: 'align-items:flex-end' },
        h('div', { class: 'asc-field', style: 'margin-bottom:0' },
          h('label', { class: 'asc-label' }, 'Product version'), cohortSel),
        h('label', { class: 'asc-checkbox-row', style: 'margin-bottom:0' }, cohortInclExported, 'Re-include already-exported'),
        cohortBtn),
      cohortStatus));

    // ── Contributors (by organization → contributor → profile) ──────────────
    const contribCard = h('div', { class: 'asc-card', id: 'ascContribBrowser' });
    body.appendChild(contribCard);
    renderOrgContribBrowser(contribCard, 'export');

    const historyCard = h('div', { class: 'asc-card', id: 'ascExportHistory' }, loadingCard('Loading export history…'));
    body.appendChild(historyCard);
    loadExportsHistory();
  }

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
    return mode === 'export' ? 'Contributors: export by organization' : 'Metrics by organization & contributor';
  }
  function browseSub(mode) {
    return mode === 'export'
      ? 'Browse every contributor by organization. Export all the data an organization labelled, or open a contributor to export their data or generate a credential verification summary.'
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
      refreshExportReadyCount();
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
      refreshExportReadyCount();
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

  async function loadExportsHistory() {
    const card = document.getElementById('ascExportHistory');
    if (!card) return;
    clear(card);
    card.appendChild(loadingCard('Loading export history…'));
    try {
      const data = await api('/exports');
      const exports = data.exports || [];
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'Export history (' + exports.length + ')')));
      if (!exports.length) { card.appendChild(h('div', { class: 'asc-empty' }, h('p', {}, 'No exports yet.'))); return; }
      const verLabel = (v) => ({ v4: 'V4', v3: 'V3', v2: 'V2', v1: 'V1' }[v] || v);
      const versionCell = (x) => {
        const m = x.manifest || {};
        const filt = (m.filters || {}).portal_version;
        if (filt) return verLabel(filt) + ' only';
        const bpv = (m.counts || {}).by_portal_version || {};
        const keys = Object.keys(bpv);
        if (!keys.length) return 'n/a';
        if (keys.length === 1) return verLabel(keys[0]);
        return keys.sort().map((k) => k + ' ' + bpv[k]).join(' · '); // mixed
      };
      const rows = exports.map((x) => h('tr', {},
        h('td', { class: 'asc-mono' }, (x.export_id || '').slice(0, 12)),
        h('td', {}, x.profile || 'n/a'),
        h('td', {}, versionCell(x)),
        h('td', {}, String(x.record_count != null ? x.record_count : (x.count != null ? x.count : 'n/a'))),
        h('td', {}, fmtDate(x.created_at)),
        h('td', {}, h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', onClick: () => downloadExport(x.export_id) }, '⬇ Download'))));
      card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['ID', 'Profile', 'Version', 'Records', 'Created', ''].map((c) => h('th', {}, c)))),
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
  function trunc(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; }
  // All admin timestamps are STORED as naive UTC ISO strings (Python
  // datetime.utcnow().isoformat(); no trailing 'Z'/offset). new Date() would
  // otherwise parse them as browser-local time, so we append 'Z' to pin them to
  // UTC, then render in Pacific (America/Los_Angeles handles PST/PDT itself). One
  // choke point → every admin wall-clock display reads in Pacific time.
  const ASC_TZ = 'America/Los_Angeles';
  function toUtcDate(d) {
    if (d == null) return null;
    if (typeof d === 'string') {
      // Bare 'YYYY-MM-DDTHH:MM:SS(.ffffff)' with no zone → treat as UTC.
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
  // Product-version label shared by the exports history + per-task version badges.
  function ascVerLabel(v) {
    return { v4: 'V4 · Real', v3: 'V3', v2: 'V2', v1: 'V1' }[v] || (v || 'n/a');
  }

  // ─── Keyboard shortcuts (eval view) ────────────────────────────────────────
  document.addEventListener('keydown', (e) => {
    if (state.view !== 'eval' || !state.task || !state.draft) return;
    // Verdict shortcuts only apply once the answers are revealed (Stage 3).
    if (state.draft.stage !== 'compare') return;
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target.isContentEditable) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === '1') { e.preventDefault(); selectVerdict('A_better'); }
    else if (e.key === '2') { e.preventDefault(); selectVerdict('B_better'); }
    else if (e.key === '3') { e.preventDefault(); selectVerdict('both_inadequate'); }
  });

  // Persist draft on tab close / hide.
  window.addEventListener('beforeunload', saveDraft);
  document.addEventListener('visibilitychange', () => { if (document.hidden) saveDraft(); });

  // ═══════════════════════════════════════════════════════════════════════════
  //  Tutorial: Calibration Case 1 (first-run guided practice case)
  //  A custom action-gated tour over the REAL labeling flow: each step advances
  //  only when the doctor performs the real action (read via the same
  //  state.draft flags the app itself gates on), never on a bare "Next". The
  //  practice case is virtual server-side; the scored reveal compares the
  //  doctor to the reference panel. Skippable everywhere, replayable forever
  //  from the help menu, and the instruction drawer below is generated from the
  //  SAME step content so tour and manual can never drift.
  // ═══════════════════════════════════════════════════════════════════════════
  const TUTORIAL_TASK_ID = 'tutorial-calibration-1';
  const INSTR_SEEN_KEY = 'asc_instr_seen';

  // ─── Tour targets: THE contract with the labeling UI ───────────────────────
  // Every selector the tour points at lives HERE, in one map, so a UI redesign
  // (case panel, substage cards, buttons) is a one-place fix. Preference order:
  // data-tour attributes > stable ids > data-substage. If a redesign removes a
  // target entirely, the step auto-skips after a short wait (see
  // renderTourSpotlight): a missing element must never strand the doctor.
  const TOUR_TARGETS = {
    caseTabs: '[data-tour="case-tabs"]',
    labsTab: '[data-tour="case-tabs"] [data-tab="labs"]',
    promptContinue: '[data-tour="prompt-continue"]',
    instinct: '[data-tour="instinct-field"]',
    revealBtn: '#ascRevealBtn',
    answers: '#ascAnswers',
    verdicts: '#ascVerdicts',
    refine: '[data-substage="refine"], [data-substage="from_scratch"]',
    whyBetter: '[data-substage="why_better"]',
    citations: '[data-substage="citations"]',
    critique: '[data-substage="critique_rejected"]',
    reasoning: '[data-substage="reasoning"]',
    rubric: '[data-substage="rubric"]',
    confidence: '[data-substage="confidence"]',
    submit: '#ascSubmit',
  };

  // ─── Content: functionality-first, both-path cues, no clinical spoilers ────
  // copy: one plain instruction (do this with the case). Where the doctor might
  // legitimately do nothing (no edits, no citation), the copy names BOTH paths.
  // intro: shown as a smaller second line on the chapter's first step (chapter
  // interstitials were cut: one welcome screen, then an uninterrupted flow).
  // advanceOn: {state: (d) => bool} | {click: selector} | {manual: true}.
  // "Skip this step" for a step that requires real app state to progress
  // (everything below except the two pure-reading beats) does not just move
  // a pointer: it performs a reasonable version of the real action through
  // the app's own handlers/state, exactly as if the doctor had done it, so
  // the underlying draft (and, for reveal/submit, the backend) actually
  // advances and the tour lands on a REAL next step. Placeholder text is
  // always visibly marked "(practice run)" and this data is never recorded
  // (the tutorial submission is graded, never saved: see tutorial_case.py).
  const TUTORIAL_CHAPTERS = [
    {
      id: 'ch1', title: 'Read the case',
      intro: 'The tabs hold the chart: labs, notes, meds, vitals.',
      steps: [
        { id: 'ch1-tabs', target: TOUR_TARGETS.caseTabs,
          copy: 'Read the case: open each tab, starting with Labs.',
          advanceOn: { click: TOUR_TARGETS.labsTab },
          doneWhen: (d) => d.stage !== 'prompt_review',
          note: 'Every case starts with the chart. Open each tab (Patient, Labs, EHR, Meds, Vitals) and read it through before judging anything.' },
        { id: 'ch1-valid', target: TOUR_TARGETS.promptContinue,
          copy: 'If the case reads as real and answerable, continue. If not, flag it.',
          advanceOn: { state: (d) => d.stage !== 'prompt_review' },
          autofill: () => validatePrompt(),
          note: 'Step 1 is a sanity gate: continue when the case is valid, or flag it with a reason: flagged cases leave your queue for admin review.' },
      ],
    },
    {
      id: 'ch2', title: 'Your take first',
      intro: 'You commit one line before seeing any AI answers, so they can’t anchor you.',
      steps: [
        { id: 'ch2-instinct', target: TOUR_TARGETS.instinct,
          copy: 'Type your first take on the case: one line.',
          advanceOn: { state: (d) => !!((d.independent_answer || {}).text || '').trim() },
          autofill: () => {
            const d = state.draft;
            if ((d.independent_answer.text || '').trim()) return;
            d.independent_answer.text = 'Skipped (practice run).';
            saveDraft();
            renderTaskWorkspace();
          },
          note: 'The one-line gut check. It’s why the AI answers look blurred at first: deliberate, not broken.' },
        { id: 'ch2-reveal', target: TOUR_TARGETS.revealBtn,
          copy: 'Now reveal the two AI answers.',
          advanceOn: { state: (d) => d.stage === 'compare' },
          autofill: () => commitIndependentAnswerAndReveal(),
          note: 'Reveal commits your line and unblinds the answers (Enter works too).' },
      ],
    },
    {
      id: 'ch3', title: 'Compare & pick',
      intro: 'Two anonymized model answers to the same case. Judge the reasoning.',
      steps: [
        { id: 'ch3-read', target: TOUR_TARGETS.answers,
          copy: 'Read both answers, then continue.',
          advanceOn: { manual: true },
          doneWhen: (d) => !!d.verdict,
          note: 'Answers are anonymized (Model A / Model B) and order-randomized.' },
        { id: 'ch3-verdict', target: TOUR_TARGETS.verdicts,
          copy: 'Pick the stronger answer: or mark both inadequate.',
          advanceOn: { state: (d) => !!d.verdict },
          autofill: () => selectVerdict('B_better'),
          note: 'Keys 1 / 2 / 3 work. "Both inadequate" asks you to write the ideal answer yourself instead.' },
        { id: 'ch3-refine', target: TOUR_TARGETS.refine,
          copy: 'Edit the answer if anything is off: or save it unchanged if it’s right.',
          advanceOn: { state: (d) => !!d.refine_saved || !!d.from_scratch_saved },
          autofill: () => {
            const d = state.draft;
            if (d.verdict === 'both_inadequate') {
              if (!(d.from_scratch.ideal_answer || '').trim()) {
                d.from_scratch.ideal_answer = 'Intensify decongestion given persistent volume overload (practice run).';
              }
              d.from_scratch_saved = true;
            } else {
              d.refine_saved = true;
            }
            state._reopenedSubstage = null;
            refreshStagedFlow();
          },
          note: 'Your saved version becomes the gold answer. Saving with no edits is a valid choice when the answer is already correct.' },
      ],
    },
    {
      id: 'ch4', title: 'Say what’s right and wrong',
      intro: 'The structured critique is the product: tagged errors, severities, sources.',
      steps: [
        { id: 'ch4-why', target: TOUR_TARGETS.whyBetter,
          copy: 'Write one line on why it won, and tag at least one reason.',
          skipIf: (d) => d.verdict === 'both_inadequate',
          advanceOn: { state: () => substageComplete('why_better') },
          autofill: () => {
            const d = state.draft;
            const rev = d.chosen_revision;
            if (!(rev.why_better_notes || '').trim()) {
              rev.why_better_notes = 'Reads the persistent-congestion evidence rather than the creatinine trend alone (practice run).';
            }
            if (!(rev.why_better_tags || []).length) {
              rev.why_better_tags = [(state.taxonomy.why_better_tags || ['safer'])[0]];
            }
            d.why_better_done = true;
            state._reopenedSubstage = null;
            refreshStagedFlow();
          },
          note: 'One sentence plus at least one why-better tag.' },
        { id: 'ch4-cite', target: TOUR_TARGETS.citations,
          copy: 'Attach a supporting citation: or continue without one.',
          skipIf: (d) => d.verdict === 'both_inadequate',
          advanceOn: { state: () => substageComplete('citations') },
          autofill: () => {
            const d = state.draft;
            const rev = d.chosen_revision;
            if ((state.task.grounding_mode === 'required') && !isValidAnchor(rev.evidence_anchor)) {
              rev.evidence_anchor = { citation_text: 'KDIGO 2024 (practice placeholder)', source_type: 'guideline', identifier: '' };
            }
            d.citations_reviewed = true;
            state._reopenedSubstage = null;
            refreshStagedFlow();
          },
          note: 'Citations are optional here (required on some tasks); the search box knows the major guidelines.' },
        { id: 'ch4-critique', target: TOUR_TARGETS.critique,
          copy: 'Tag each error in the other answer. Tap a tag again to set severity.',
          skipIf: (d) => d.verdict === 'both_inadequate',
          advanceOn: { state: () => substageComplete('critique_rejected') },
          autofill: () => {
            const d = state.draft;
            const crit = d.rejected_critique;
            if (!(crit.error_tags || []).length) {
              const tag = (state.taxonomy.error_tags || ['unsafe_recommendation'])[0];
              crit.error_tags = [tag];
              crit.severities[tag] = crit.severities[tag] || 'high';
            } else {
              crit.error_tags.forEach((t) => { crit.severities[t] = crit.severities[t] || 'high'; });
            }
            if (!(crit.why_worse || '').trim()) {
              crit.why_worse = 'A fluid bolus in a still-congested patient re-congests them (practice run).';
            }
            closeTagPopover();
            d.critique_done = true;
            state._reopenedSubstage = null;
            refreshStagedFlow();
          },
          note: 'Error tags come from a fixed taxonomy; the severity picker is behind a second tap on the tag: easy to miss.' },
        { id: 'ch4-reasoning', target: TOUR_TARGETS.reasoning,
          copy: 'Confirm each reasoning step that’s right; open any that aren’t.',
          skipIf: () => !(state.task && state.task.capture_reasoning),
          advanceOn: { state: () => substageComplete('reasoning') },
          autofill: () => autofillReasoningSteps(),
          note: 'The answer splits into steps; confirm the good ones, correct the bad ones.' },
      ],
    },
    {
      id: 'ch5', title: 'Score & submit',
      intro: 'Your scoring guide becomes a reusable grader for future models.',
      steps: [
        { id: 'ch5-rubric', target: TOUR_TARGETS.rubric,
          copy: 'Add scoring criteria: include at least one critical negative.',
          advanceOn: { state: () => substageComplete('rubric') },
          autofill: () => {
            const d = state.draft;
            if (!hasCriticalNegative(d.rubric)) {
              d.rubric.push({
                text: 'Recommends holding diuresis or giving IV fluids despite persistent congestion (practice run)',
                points: -9, axis: 'safety', source: 'manual',
              });
            }
            d.rubric_done = true;
            state._reopenedSubstage = null;
            refreshStagedFlow();
          },
          note: 'Criteria carry points; a −8 to −10 negative is a critical negative (auto-fail) and each case needs at least one.' },
        { id: 'ch5-confidence', target: TOUR_TARGETS.confidence,
          copy: 'Rate your confidence.',
          advanceOn: { state: (d) => !!d.confidence_set },
          autofill: () => {
            const d = state.draft;
            if (!d.confidence_set) { d.confidence = 'high'; d.confidence_set = true; saveDraft(); }
            renderTaskWorkspace();
          },
          note: 'Low / medium / high: honest calibration beats looking sure.' },
        { id: 'ch5-submit', target: TOUR_TARGETS.submit,
          copy: 'Submit: and see how you compare with the reference panel.',
          advanceOn: { state: () => false },  // the submit path ends the tour
          autofill: () => submitEvaluation(),
          note: 'Submit packages your work into training records. On the practice case it scores you against the reference panel instead.' },
      ],
    },
  ];

  // ch4-reasoning's autofill needs to wait out the async auto-split (heuristic
  // or LLM-pregraded) before confirm buttons exist: retries briefly rather
  // than assuming they're already on screen.
  function autofillReasoningSteps(triesLeft) {
    triesLeft = triesLeft == null ? 15 : triesLeft;
    let btn;
    let guard = 0;
    while ((btn = document.querySelector(TOUR_TARGETS.reasoning + ' .asc-step-confirm:not(.active)')) && guard++ < 50) {
      btn.click();
    }
    const cont = document.querySelector('#ascStepsCont');
    if (cont && !cont.disabled) { cont.click(); return; }
    if (triesLeft > 0) setTimeout(() => autofillReasoningSteps(triesLeft - 1), 200);
  }

  const TUTORIAL_STEPS = [];
  TUTORIAL_CHAPTERS.forEach((ch) => ch.steps.forEach((s, i) => {
    TUTORIAL_STEPS.push(Object.assign({ chapter: ch, chapterFirst: i === 0 }, s));
  }));

  // ─── Engine ────────────────────────────────────────────────────────────────
  let _tourObserver = null;
  let _tourTickTimer = null;
  let _tourPatchTimer = null;
  let _tourScrolledFor = null; // step id already auto-centered (scroll once per step)

  function tutCurrentStep() {
    const t = state.tutorial;
    return (t && TUTORIAL_STEPS[t.idx]) || null;
  }

  function tutStepSatisfied(step) {
    const d = state.draft;
    if (!d) return false;
    try {
      if (step.skipIf && step.skipIf(d)) return true;
      // doneWhen lets a click/manual step fast-forward on resume once its
      // moment has passed (the draft is the truth, not the click history).
      if (step.doneWhen && step.doneWhen(d)) return true;
      const adv = step.advanceOn || {};
      if (adv.state) return !!adv.state(d);
    } catch (e) { return false; }
    return false; // click/manual steps only advance on their explicit action
  }

  function tutPersistStep(stepId) {
    if (!state.tutorial || state.tutorial.replay) return;
    // resolveTourIndex walks the pointer back to the real blocker while the
    // fast-forward loop walks it on; when they disagree the pointer can settle
    // on the same step repeatedly. Only write when the position actually
    // changed, so that never becomes a stream of PATCHes.
    if (state.tutorial.persistedStep === stepId) return;
    state.tutorial.persistedStep = stepId;
    clearTimeout(_tourPatchTimer);
    _tourPatchTimer = setTimeout(() => {
      api('/me/tutorial', { method: 'PATCH', body: { action: 'advance', step: stepId } })
        .then((u) => { state.user = u; })
        .catch(() => { /* position sync is best-effort */ });
    }, 600);
  }

  function tutAdvance() {
    const t = state.tutorial;
    if (!t) return;
    t.idx += 1;
    const step = tutCurrentStep();
    if (step) tutPersistStep(step.id);
    tutTick();
  }

  // "Skip this step" moves the pointer forward, but many steps only render
  // once the REAL section before them is actually finished (the app shows one
  // decision at a time). If the app hasn't gotten there, the target the tour
  // just jumped to doesn't exist yet and skipping would strand the doctor
  // staring at nothing. Detect that and walk back to the nearest not-yet-done
  // step whose target IS on screen right now: that's the true blocker, and
  // re-spotlighting it (rather than a blank corner) is what keeps guiding
  // them. A real "Continue" click on that section resumes forward motion the
  // moment its state predicate is satisfied.
  function resolveTourIndex() {
    const t = state.tutorial;
    if (!t) return;
    const cur = TUTORIAL_STEPS[t.idx];
    if (!cur || document.querySelector(cur.target)) return; // nothing to resolve
    for (let i = t.idx - 1; i >= 0; i--) {
      const s = TUTORIAL_STEPS[i];
      if (tutStepSatisfied(s)) continue; // already done: not the blocker
      if (document.querySelector(s.target)) { t.idx = i; t.bounced = true; return; }
    }
  }

  // The tick: fast-forward past satisfied/skipped steps, then render the
  // current beat (interstitial or spotlight). Runs after every DOM mutation,
  // scroll, and resize: cheap by design.
  function tutTick() {
    if (!tutorialActive()) return;
    // A modal (the welcome screen or the skip-tutorial confirm) OWNS the
    // screen while it's open. tutTick runs on a timer and on every DOM
    // mutation/scroll/resize, so without this guard a stray re-tick while the
    // confirm dialog is up would silently re-show the spotlight ring and
    // tooltip right on top of it: exactly the "box is still there" bug.
    if (document.getElementById('ascTourSkipConfirm')) return;
    const t = state.tutorial;
    resolveTourIndex();
    let step = tutCurrentStep();
    let moved = false;
    while (step && tutStepSatisfied(step)) { t.idx += 1; moved = true; step = tutCurrentStep(); t.bounced = false; }
    if (moved && step) tutPersistStep(step.id);
    if (!step) { hideTourLayer(); return; }  // waiting on the submit path
    // One welcome screen, then an uninterrupted flow (chapter intros ride
    // along as a second line on each chapter's first tooltip instead).
    if (!t.welcomed) { renderTourWelcome(); return; }
    renderTourSpotlight(step);
  }
  function scheduleTutTick() {
    clearTimeout(_tourTickTimer);
    _tourTickTimer = setTimeout(tutTick, 80);
  }

  // Nodes that rewrite themselves on a timer, not because the doctor did
  // anything: the case clock ticks once a second, inside #ascRoot, forever.
  // Treating those writes as "the app changed" re-ticked the tour every
  // second, which tore the tooltip down and rebuilt it under the cursor —
  // clicks landed mid-rebuild, focus and hover reset, and the aria-live copy
  // re-announced itself endlessly. Mark such nodes [data-tour-ignore].
  function tourIgnoredNode(n) {
    const el = n && n.nodeType === 3 ? n.parentElement : n;
    if (!el || !el.closest) return false;
    return !!(el.closest('#ascTourLayer') || el.closest('#ascInstrDrawer')
      || el.closest('[data-tour-ignore]'));
  }

  function mountTourEngine() {
    if (_tourObserver) return;
    _tourObserver = new MutationObserver((muts) => {
      // Ignore mutations inside the tour's own layer, the drawer, or any
      // self-ticking node.
      for (const m of muts) {
        if (tourIgnoredNode(m.target)) continue;
        scheduleTutTick();
        return;
      }
    });
    _tourObserver.observe(root(), { childList: true, subtree: true, attributes: true });
    window.addEventListener('resize', scheduleTutTick);
    window.addEventListener('scroll', scheduleTutTick, true);
    document.addEventListener('click', tutClickAdvance, true);
    document.addEventListener('keydown', tutKeydown, true);
  }

  function teardownTutorial() {
    if (_tourObserver) { _tourObserver.disconnect(); _tourObserver = null; }
    window.removeEventListener('resize', scheduleTutTick);
    window.removeEventListener('scroll', scheduleTutTick, true);
    document.removeEventListener('click', tutClickAdvance, true);
    document.removeEventListener('keydown', tutKeydown, true);
    clearTimeout(_tourTickTimer);
    clearTimeout(_tourPatchTimer);
    TUTORIAL_STEPS.forEach((s) => { s._waitSince = null; });
    _tourScrolledFor = null;
    const layer = document.getElementById('ascTourLayer');
    if (layer) layer.remove();
    state.tutorial = null;
  }

  function tutClickAdvance(e) {
    if (!tutorialActive()) return;
    const step = tutCurrentStep();
    if (!step || !step.advanceOn || !step.advanceOn.click) return;
    const hit = e.target && e.target.closest && e.target.closest(step.advanceOn.click);
    if (hit) setTimeout(tutAdvance, 40); // let the app's own handler run first
  }

  function tutKeydown(e) {
    if (!tutorialActive()) return;
    if (e.key === 'Escape') {
      // Don't fight app overlays (case overlay closes on Esc too).
      if (document.querySelector('.call-team-overlay.is-open:not(.asc-tour-interstitial)')) return;
      e.stopPropagation();
      confirmSkipTutorial();
    }
  }

  // ─── Spotlight renderer (4-rect mask + popover, Console design system) ─────
  function ensureTourLayer() {
    let layer = document.getElementById('ascTourLayer');
    if (layer) return layer;
    layer = h('div', { id: 'ascTourLayer' },
      h('div', { class: 'asc-tour-mask', dataset: { edge: 'top' } }),
      h('div', { class: 'asc-tour-mask', dataset: { edge: 'left' } }),
      h('div', { class: 'asc-tour-mask', dataset: { edge: 'right' } }),
      h('div', { class: 'asc-tour-mask', dataset: { edge: 'bottom' } }),
      h('div', { class: 'asc-tour-ring', 'aria-hidden': 'true' }),
      h('div', { class: 'asc-tour-pop', role: 'dialog', 'aria-label': 'Tutorial step' }));
    document.body.appendChild(layer);
    return layer;
  }
  function hideTourLayer() {
    const layer = document.getElementById('ascTourLayer');
    if (layer) layer.style.display = 'none';
  }

  function tutStepNumber(step) {
    return { n: TUTORIAL_STEPS.indexOf(TUTORIAL_STEPS.find((s) => s.id === step.id)) + 1,
             total: TUTORIAL_STEPS.length };
  }

  function renderTourSpotlight(step) {
    const target = document.querySelector(step.target);
    const layer = ensureTourLayer();
    const pop = layer.querySelector('.asc-tour-pop');
    if (!target) {
      // Target not rendered yet. NEVER block the app here: the section may be
      // gated on work the doctor still has to do (e.g. after "Skip this step"),
      // so masks stay off and the popover waits quietly in the corner.
      // A DOM-gated step (click / manual) whose target never appears would
      // otherwise strand the doctor, so after a while we offer a way past it.
      // OFFER, not take: this used to auto-advance after 8s, which moved the
      // tour on its own while someone was still reading, and read as the
      // tutorial running away from them. Nothing here advances without a
      // click. State-gated steps wait indefinitely: their predicate (or the
      // fast-forward loop) advances them the moment the doctor gets there.
      const domGated = !!(step.advanceOn && (step.advanceOn.click || step.advanceOn.manual));
      if (!step._waitSince) step._waitSince = Date.now();
      const stuck = domGated && Date.now() - step._waitSince > 10000;
      layer.style.display = '';
      hideMasks(layer);
      renderTourPop(pop, step, true, false, stuck);
      pop.style.transform = '';
      pop.style.top = 'auto';
      pop.style.left = '16px';
      pop.style.bottom = '16px';
      scheduleTutTick(); // keep polling for the target (or the auto-skip)
      return;
    }
    step._waitSince = null;
    pop.style.bottom = 'auto';
    // Auto-center the target ONCE per step. Never on later ticks: repositioning
    // runs on every scroll, and re-centering there would yank the page back and
    // fight the user's own scrolling.
    const r = target.getBoundingClientRect();
    const vh = window.innerHeight;
    if (_tourScrolledFor !== step.id && (r.top < 0 || r.bottom > vh)) {
      _tourScrolledFor = step.id;
      const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      try { target.scrollIntoView({ block: 'center', behavior: reduced ? 'auto' : 'smooth' }); } catch (e) { /* ok */ }
    }
    layer.style.display = '';
    positionMasks(target.getBoundingClientRect(), layer);
    const t = state.tutorial;
    const bounced = !!(t && t.bounced);
    if (t) t.bounced = false; // one-shot: show the note once, not on every re-tick
    renderTourPop(pop, step, false, bounced); // content first: position needs real height
    positionPop(target.getBoundingClientRect(), pop);
  }

  const TOUR_PAD = 6;
  function hideMasks(layer) {
    layer.querySelectorAll('.asc-tour-mask').forEach((m) => setRect(m, 0, 0, 0, 0));
    layer.querySelector('.asc-tour-ring').style.display = 'none';
  }
  function positionMasks(r, layer) {
    const masks = layer.querySelectorAll('.asc-tour-mask');
    const ring = layer.querySelector('.asc-tour-ring');
    const vw = window.innerWidth, vh = window.innerHeight;
    if (!r) { // full dim, no hole
      setRect(masks[0], 0, 0, vw, vh);
      setRect(masks[1], 0, 0, 0, 0); setRect(masks[2], 0, 0, 0, 0); setRect(masks[3], 0, 0, 0, 0);
      ring.style.display = 'none';
      return;
    }
    const top = Math.max(0, r.top - TOUR_PAD), left = Math.max(0, r.left - TOUR_PAD);
    const right = Math.min(vw, r.right + TOUR_PAD), bottom = Math.min(vh, r.bottom + TOUR_PAD);
    setRect(masks[0], 0, 0, vw, top);                       // top strip
    setRect(masks[1], 0, top, left, bottom - top);          // left strip
    setRect(masks[2], right, top, vw - right, bottom - top); // right strip
    setRect(masks[3], 0, bottom, vw, vh - bottom);          // bottom strip
    ring.style.display = '';
    ring.style.left = left + 'px'; ring.style.top = top + 'px';
    ring.style.width = (right - left) + 'px'; ring.style.height = (bottom - top) + 'px';
  }
  function setRect(el, x, y, w, hgt) {
    el.style.left = x + 'px'; el.style.top = y + 'px';
    el.style.width = Math.max(0, w) + 'px'; el.style.height = Math.max(0, hgt) + 'px';
  }
  function positionPop(r, pop) {
    const vw = window.innerWidth, vh = window.innerHeight;
    pop.style.transform = '';
    const popW = Math.min(340, vw - 24);
    pop.style.width = popW + 'px';
    const below = r.bottom + TOUR_PAD + 12;
    const popH = pop.offsetHeight || 120;
    let top;
    if (below + popH < vh - 12) top = below;
    else top = Math.max(12, r.top - TOUR_PAD - popH - 12);
    let left = Math.min(Math.max(12, r.left), vw - popW - 12);
    pop.style.top = top + 'px';
    pop.style.left = left + 'px';
  }

  function renderTourPop(pop, step, waiting, bounced, stuck) {
    const num = tutStepNumber(step);
    // Rebuild only when the tooltip would actually differ. Ticks are cheap but
    // a rebuild is not: it destroys the buttons the doctor is reaching for and
    // re-fires the aria-live region. Repositioning still happens every tick.
    const sig = [step.id, waiting ? 'wait' : 'live', bounced ? 'bounced' : '',
                 stuck ? 'stuck' : ''].join('|');
    if (pop.dataset.tourSig === sig) return;
    pop.dataset.tourSig = sig;
    clear(pop);
    pop.appendChild(h('div', { class: 'asc-tour-chrome' },
      'STEP ' + num.n + ' OF ' + num.total + ' · ' + step.chapter.title.toUpperCase()));
    pop.appendChild(h('div', { class: 'asc-tour-copy', 'aria-live': 'polite' },
      waiting ? 'Next up: ' + step.copy.replace(/\.$/, '') + '. This appears when the step before it is done.'
        : step.copy));
    if (bounced) {
      pop.appendChild(h('div', { class: 'asc-tour-sub' }, 'This one has to happen here before the tour can move on.'));
    } else if (!waiting && step.chapterFirst && step.chapter.intro) {
      pop.appendChild(h('div', { class: 'asc-tour-sub' }, step.chapter.intro));
    }
    if (stuck) {
      pop.appendChild(h('div', { class: 'asc-tour-sub' },
        'This part of the page has not appeared. You can move on whenever you like.'));
    }
    const row = h('div', { class: 'asc-tour-actions' });
    if (stuck) {
      row.appendChild(h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button',
        onClick: tutAdvance }, 'Move on →'));
    }
    if (step.advanceOn && step.advanceOn.manual && !waiting) {
      row.appendChild(h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button',
        onClick: tutAdvance }, 'Next →'));
    } else if (!waiting && !bounced) {
      // Per-step skip: for steps the app requires real data/action from
      // (nearly all of them), this performs a reasonable version of that
      // action through the app's own handlers: filling required fields with
      // clearly-marked placeholder text, picking sensible defaults: so the
      // draft (and, for reveal/submit, the backend) genuinely advances and
      // the tour lands on the real next step, not a dead pointer. Read-only
      // beats with no real state (ch1-tabs, ch3-read) have no autofill and
      // just move the pointer on, since their next target already exists.
      row.appendChild(h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
        onClick: () => {
          if (step.autofill) {
            try { step.autofill(); }
            catch (e) { try { console.warn('[tutorial] autofill failed for', step.id, e); } catch (_e) { /* ok */ } }
            tutTick(); // resync immediately; async actions catch up via their own re-render
          } else {
            tutAdvance();
          }
        } }, 'Skip this step'));
    }
    row.appendChild(h('button', { class: 'asc-btn-link asc-tour-skip', type: 'button',
      onClick: confirmSkipTutorial }, 'Skip tutorial'));
    pop.appendChild(row);
    const frac = h('div', { class: 'asc-tour-bar' });
    frac.appendChild(h('div', { class: 'asc-tour-bar-fill',
      style: 'width:' + Math.round(((num.n - 1) / num.total) * 100) + '%' }));
    pop.appendChild(frac);
  }

  // ─── Welcome screen (the ONLY interstitial: then an uninterrupted flow) ───
  function renderTourWelcome() {
    hideTourLayer();
    if (document.getElementById('ascTourInterstitial')) return;
    const overlay = h('div', { class: 'call-team-overlay is-open asc-tour-interstitial', id: 'ascTourInterstitial' });
    const popup = h('div', { class: 'call-team-popup asc-tour-inter-pop', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'asc-tour-chrome' }, 'CALIBRATION CASE 1'),
      h('div', { class: 'call-team-title' }, 'One practice case. About 4 minutes.'),
      h('p', { class: 'asc-help', style: 'margin:6px 0 16px' },
        'A guided walk through labeling one case: read it, give your take, compare two AI answers, '
        + 'say what’s right and wrong, score it. Then you’ll see how your reads compare with the '
        + 'reference panel. Nothing here is recorded or sold.'),
      h('div', { style: 'display:flex;gap:10px;align-items:center' },
        h('button', { class: 'asc-btn asc-btn-primary', type: 'button', onClick: proceed }, 'Start the case →'),
        h('button', { class: 'asc-btn-link asc-tour-skip', type: 'button', onClick: () => { overlay.remove(); confirmSkipTutorial(); } },
          'Skip tutorial')));
    function proceed() {
      state.tutorial.welcomed = true;
      overlay.remove();
      tutTick();
    }
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  // ─── Start / skip / submit ─────────────────────────────────────────────────
  async function startTutorial(opts) {
    opts = opts || {};
    if (opts.replay) {
      clearDraft(TUTORIAL_TASK_ID); // a fresh replay, not a resume of old work
    } else {
      api('/me/tutorial', { method: 'PATCH', body: { action: 'start' } })
        .then((u) => { state.user = u; }).catch(() => { /* best-effort */ });
    }
    // Resume where the server says they stopped. The saved position used to be
    // written and never read, so a doctor who got halfway on their laptop
    // started again at step 1 on their phone; only the local draft made resume
    // look like it worked. The fast-forward loop still corrects this downward
    // if the draft shows less progress than the pointer claims.
    let startIdx = 0;
    if (opts.resume && opts.resumeStep) {
      const i = TUTORIAL_STEPS.findIndex((s) => s.id === opts.resumeStep);
      if (i > 0) startIdx = i;
    }
    // Step objects are module-level and shared across runs: a stale wait clock
    // or scroll marker from a previous run would misfire on this one.
    TUTORIAL_STEPS.forEach((s) => { s._waitSince = null; });
    _tourScrolledFor = null;
    state.tutorial = { active: true, replay: !!opts.replay, idx: startIdx,
                       welcomed: startIdx > 0 };
    state.portalChosen = true;
    state.specialtyChosen = true;
    const wrap = h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), 'Preparing your practice case…')));
    setRoot(wrap);
    let data;
    try {
      data = await api('/tutorial/task');
    } catch (e) {
      // Never trap the doctor: fall back to the dashboard if the practice
      // case cannot load. Not the experience/specialty picker: that choice is
      // ours to make, not the doctor's.
      teardownTutorial();
      state.portalChosen = false; state.specialtyChosen = false;
      if (e.status !== 401) {
        toast('Could not load the practice case: ' + e.message, 'error');
        renderDashboardView();
      }
      return;
    }
    state.task = data.task;
    initDraftForTask(state.task);
    state.draft.portal_version = 'v3'; // the tutorial teaches the seamless flow
    saveDraft();
    if (state.draft.stage === 'compare') {
      try { await loadWithheldAnswersIfNeeded(); } catch (e) { /* compare shows a reload hint */ }
    }
    mountTourEngine();
    renderTaskWorkspace();
    tutTick();
  }

  function confirmSkipTutorial() {
    if (document.getElementById('ascTourSkipConfirm')) return;
    // The spotlight box + tooltip sit ABOVE this confirm dialog (z 1200 vs
    // 1000): hide them first, or the old highlight and copy stay pasted on
    // screen behind/around the dialog instead of clearing out of the way.
    hideTourLayer();
    const overlay = h('div', { class: 'call-team-overlay is-open asc-tour-interstitial', id: 'ascTourSkipConfirm' });
    const popup = h('div', { class: 'call-team-popup asc-tour-inter-pop', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'call-team-title' }, 'Skip the practice case?'),
      h('p', { class: 'asc-help', style: 'margin:6px 0 16px' },
        'You can replay it any time from the ? tab in the corner. The written instructions stay there too.'),
      h('div', { style: 'display:flex;gap:10px' },
        h('button', { class: 'asc-btn asc-btn-primary', type: 'button',
          onClick: () => { overlay.remove(); tutTick(); } }, 'Keep going'),
        h('button', { class: 'asc-btn asc-btn-ghost', type: 'button',
          onClick: () => { overlay.remove(); skipTutorial(); } }, 'Skip')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  function skipTutorial() {
    const wasReplay = state.tutorial && state.tutorial.replay;
    if (!wasReplay) {
      api('/me/tutorial', { method: 'PATCH', body: { action: 'skip' } })
        .then((u) => { state.user = u; }).catch(() => { /* server no-ops are fine */ });
    }
    teardownTutorial();
    clearDraft(TUTORIAL_TASK_ID);
    stopTimer();
    state.task = null;
    state.portalChosen = false;
    state.specialtyChosen = false;
    renderDashboardView();
    // First skip: pulse the corner ? tab once so they know where the written
    // instructions live: never auto-open a panel over their screen.
    let seen = null;
    try { seen = localStorage.getItem(INSTR_SEEN_KEY); } catch (e) { seen = null; }
    if (!wasReplay && !seen) {
      try { localStorage.setItem(INSTR_SEEN_KEY, '1'); } catch (e) { /* ignore */ }
      pulseInstrTab();
    }
  }

  async function submitTutorialEvaluation() {
    state.submitting = true;
    const btn = document.getElementById('ascSubmit');
    if (btn) { btn.disabled = true; btn.textContent = 'Scoring…'; }
    const wasReplay = state.tutorial && state.tutorial.replay;
    let res;
    try {
      res = await api('/tutorial/submit', { method: 'POST', body: buildSubmissionPayload() });
    } catch (e) {
      state.submitting = false;
      if (btn) { btn.disabled = false; btn.textContent = 'Submit evaluation'; }
      if (e.status !== 401) toast('Could not score the practice case: ' + e.message, 'error');
      return;
    }
    state.submitting = false;
    if (res.user) state.user = res.user;
    teardownTutorial();
    clearDraft(TUTORIAL_TASK_ID);
    stopTimer();
    state.task = null;
    state.draft = null;
    updateHeaderProgress(); // no open task: the header bar hides on the reveal
    renderTutorialReveal(res.result, { replay: wasReplay });
  }

  // ─── The scored reveal ─────────────────────────────────────────────────────
  function renderTutorialReveal(result, opts) {
    opts = opts || {};
    const rows = (result.findings || []).map((f) => h('div', { class: 'asc-tour-finding' + (f.matched ? ' matched' : '') },
      h('span', { class: 'asc-tour-finding-glyph', 'aria-hidden': 'true' }, f.matched ? '✓' : '–'),
      h('div', {},
        h('div', { class: 'asc-tour-finding-label' }, f.label),
        h('div', { class: 'asc-tour-finding-reason' }, f.reason))));
    const planted = result.planted_finding;
    const card = h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-tour-chrome' }, 'CALIBRATION CASE 1 · REFERENCE PANEL'),
      h('h2', { class: 'asc-tour-headline' }, result.headline),
      h('div', { class: 'asc-tour-findings' }, rows),
      planted ? h('div', { class: 'asc-tour-planted' },
        h('div', { class: 'asc-tour-planted-title' },
          planted.matched ? 'You caught the one most physicians miss' : 'The one most physicians miss'),
        h('div', { class: 'asc-tour-finding-reason' }, planted.reason)) : null,
      h('p', { class: 'asc-help', style: 'margin:14px 0 0' },
        opts.replay ? 'Practice case: nothing was recorded.'
          : 'Nothing from this case is recorded or sold. Your real cases start now.'),
      h('div', { style: 'display:flex;gap:10px;margin-top:18px;align-items:center' },
        h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', type: 'button',
          onClick: () => { state.portalChosen = false; state.specialtyChosen = false; renderDashboardView(); } },
          'Start real cases →'),
        h('button', { class: 'asc-btn asc-btn-ghost', type: 'button', onClick: openInstructionDrawer },
          'Open the instructions')));
    setRoot(h('div', { class: 'asc-wrap asc-tour-reveal' }, card));
  }

  // ─── Help menu + instruction drawer ────────────────────────────────────────
  // Floating corner tab: the SINGLE, always-there entry point back into the
  // tutorial: replay the guided practice case, or open a summary of it. Sits
  // at the corner of the labeling screen so it's always reachable but never
  // in the way.
  function ensureInstrTab() {
    if (document.getElementById('ascInstrTab')) return;
    const tab = h('button', {
      id: 'ascInstrTab', class: 'asc-instr-tab', type: 'button',
      title: 'Tutorial', 'aria-label': 'Tutorial menu', 'aria-haspopup': 'true',
    }, '?');
    tab.addEventListener('click', toggleCornerMenu);
    document.body.appendChild(tab);
  }
  function toggleCornerMenu() {
    const existing = document.getElementById('ascCornerMenu');
    if (existing) { existing.remove(); return; }
    const tab = document.getElementById('ascInstrTab');
    const r = tab.getBoundingClientRect();
    const menu = h('div', { id: 'ascCornerMenu', class: 'asc-help-menu', role: 'menu' },
      h('button', { class: 'asc-help-menu-item', type: 'button', role: 'menuitem',
        onClick: () => { menu.remove(); startTutorial({ replay: true }); } },
        'Replay tutorial'),
      h('button', { class: 'asc-help-menu-item', type: 'button', role: 'menuitem',
        onClick: () => { menu.remove(); toggleInstructionDrawer(); } },
        'View tutorial summary'));
    // The tab lives at the bottom-right corner: open the menu UPWARD from it.
    menu.style.bottom = (window.innerHeight - r.top + 8) + 'px';
    menu.style.right = Math.max(12, window.innerWidth - r.right) + 'px';
    document.body.appendChild(menu);
    setTimeout(() => {
      const onDoc = (e) => { if (!menu.contains(e.target) && e.target !== tab) { menu.remove(); document.removeEventListener('click', onDoc, true); } };
      document.addEventListener('click', onDoc, true);
    }, 0);
  }
  function pulseInstrTab() {
    ensureInstrTab();
    const tab = document.getElementById('ascInstrTab');
    tab.classList.add('pulse');
    setTimeout(() => tab.classList.remove('pulse'), 6000);
  }
  function toggleInstructionDrawer() {
    if (document.getElementById('ascInstrDrawer')) closeInstructionDrawer();
    else openInstructionDrawer();
  }
  function closeInstructionDrawer() {
    const d = document.getElementById('ascInstrDrawer');
    if (d) d.remove();
    state.instrOpen = false;
  }
  function openInstructionDrawer() {
    if (document.getElementById('ascInstrDrawer')) return;
    state.instrOpen = true;
    // Which section applies right now (during a real case)?
    let activeChapter = null;
    if (state.draft && state.task) {
      const stg = state.draft.stage;
      if (stg === 'prompt_review') activeChapter = 'ch1';
      else if (stg === 'independent_answer') activeChapter = 'ch2';
      else if (stg === 'compare') {
        const sub = currentSubstage();
        if (sub === 'compare' || sub === 'refine' || sub === 'from_scratch') activeChapter = 'ch3';
        else if (sub === 'why_better' || sub === 'citations' || sub === 'critique_rejected' || sub === 'reasoning') activeChapter = 'ch4';
        else activeChapter = 'ch5';
      }
    }
    const sections = TUTORIAL_CHAPTERS.map((ch) => {
      const det = h('details', { class: 'asc-instr-section' },
        h('summary', { class: 'asc-instr-summary' }, ch.title),
        h('p', { class: 'asc-help asc-instr-intro' }, ch.intro),
        h('ol', { class: 'asc-instr-list' },
          ch.steps.map((s) => h('li', {},
            h('span', { class: 'asc-instr-step-copy' }, s.copy),
            h('span', { class: 'asc-instr-step-note' }, ' ' + (s.note || ''))))));
      if (ch.id === activeChapter) det.setAttribute('open', '');
      return det;
    });
    const flowNote = h('details', { class: 'asc-instr-section' },
      h('summary', { class: 'asc-instr-summary' }, 'Choosing your flow & specialty'),
      h('p', { class: 'asc-help asc-instr-intro' },
        'Real cases start at the experience chooser: Synthetic Multimodal (recommended) or Real De-identified once approved. '
        + 'V3/V4 then ask for your specialty. "Change experience" in the badge above a case returns you to the chooser.'));
    const drawer = h('aside', { id: 'ascInstrDrawer', class: 'asc-instr-drawer', 'aria-label': 'Instructions' },
      h('div', { class: 'asc-instr-head' },
        h('div', { class: 'asc-tour-chrome' }, 'HOW TO LABEL A CASE'),
        h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
          onClick: closeInstructionDrawer, 'aria-label': 'Close instructions' }, '✕')),
      h('div', { class: 'asc-instr-body' }, sections, flowNote),
      h('div', { class: 'asc-instr-foot' },
        h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
          onClick: () => { closeInstructionDrawer(); startTutorial({ replay: true }); } },
          'Replay practice case')));
    document.body.appendChild(drawer);
  }
  // Expand every manual disclosure for printing, then restore: so a printed /
  // PDF'd guide carries its full depth, not just the three-line summaries.
  window.addEventListener('beforeprint', () => {
    document.querySelectorAll('.asc-guide-detail').forEach((d) => {
      if (!d.open) { d.open = true; d.dataset.printAutoOpened = '1'; }
    });
  });
  window.addEventListener('afterprint', () => {
    document.querySelectorAll('.asc-guide-detail[data-print-auto-opened]').forEach((d) => {
      d.open = false; delete d.dataset.printAutoOpened;
    });
  });

  // ─── Chrome shortcuts ───────────────────────────────────────────────────────
  // One document-level handler, capture phase so it still fires from inside the
  // case panel's own scroll containers. Every key is guarded by isTypingTarget:
  // steps 2 and 4 are full of textareas, and a physician typing "the patient is
  // afebrile" is not reaching for a shortcut. Modifier combos are left alone so
  // Cmd/Ctrl+C stays copy.
  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    // Exit 2, the universal reflex. Deliberately NOT behind isTypingTarget: a
    // physician who wants out while the cursor sits in a textarea still gets
    // out. It defers to anything layered above it, so Escape always addresses
    // the topmost thing rather than dismissing two at once.
    if (e.key === 'Escape' && focusOn() && !modalLayerOpen()) {
      exitFocus();
      return;
    }
    if (isTypingTarget(e.target) || modalLayerOpen()) return;
    if (e.key === 'c' || e.key === 'C') toggleCaseRail();
    if (e.key === '[') toggleRailCompact();
    // Exit 3, symmetry: the same key that entered.
    if (e.key === 'f' || e.key === 'F') { focusOn() ? exitFocus() : enterFocus(); }
  }, true);
  armEdgePeek();

  // ─── Go ────────────────────────────────────────────────────────────────────
  boot();
})();
