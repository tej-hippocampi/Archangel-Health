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
  // Companion header on the practice-case 403 (routers/asclepius.py
  // PRACTICE_GATE_HEADER): 'not_started' | 'in_progress' | 'failed' |
  // 'stale_version'. Same reason as the one above: the caller picks a screen
  // from this rather than from the message, so rewording the copy can never
  // break the routing.
  const PRACTICE_GATE_HEADER = 'X-Asclepius-Practice-Gate';
  // Companion header on the contributor-agreement 403 (routers/asclepius.py
  // AGREEMENT_GATE_HEADER): 'never_signed' | 'superseded'. Read for the same
  // reason as the two above, and it is what routes a refused draw to the
  // signature screen instead of to an error card.
  const AGREEMENT_GATE_HEADER = 'X-Asclepius-Agreement-Gate';
  const TOKEN_KEY = 'asclepius_token';
  // In-progress evaluation drafts, one key per task. Deliberately per-browser
  // and client-only: the drafts are not mirrored to the server.
  //
  // That is a decided limit, not an oversight. A physician who starts a case on
  // one machine and opens the portal on another sees "Start new case" and their
  // work stays on the first machine — the same is true of a different browser,
  // a cleared site-data, or a private window. The portal is a desktop workflow
  // today, so that trade was taken on purpose. If a doctor is ever expected to
  // move between devices mid-case, the answer is a server-side draft
  // (PUT/GET/DELETE per (user_id, task_id), assignee-only, debounced, newer
  // savedAt wins on conflict and NEVER a merge) — not a wider localStorage.
  const DRAFT_PREFIX = 'asclepius_draft_';
  // Contributor's chosen evaluator experience: 'v1' classic | 'v2' assisted |
  // 'v3' seamless (the recommended default). Persisted per browser; the default
  // for every new task.
  const PORTAL_VERSION_KEY = 'asclepius_portal_version';
  // Written the first time a contributor picks an experience from a menu that
  // HAD the real cases on it. A stored version without this marker predates V4
  // entirely, so it cannot be a choice between real and synthetic — see
  // getPortalVersion(), which is what makes an approved physician who used this
  // portal before V4 shipped land on the real cases instead of staying pinned to
  // the synthetic queue forever.
  const PORTAL_VERSION_PICKED_KEY = 'asclepius_portal_version_picked_v4';
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
    view: 'eval',          // 'eval' | 'home' | 'review'  (the console is its own page)
    panel: 'tasks',        // side-panel destination: 'tasks' | 'guide' (Community is external nav)
    // PRD M: which of the three manuals the Guide is showing. NULL means "not
    // chosen yet", which resolves to the most senior manual the session holds:
    // distinct from a physician having actively picked one.
    manualRole: null,
    // Community integration state (Community PRD boundary: every field optional,
    // degrades silently if the community backend is unbuilt). unread drives the
    // badge; handoffToken is pre-minted so the new tab can open synchronously.
    community: { unread: 0, handoffToken: null, unavailable: false, unreadUnavailable: false, pollTimer: null },
    // The admin console's tab state used to live here. It moved to
    // admin_shell.js with the console itself (PRD-F R3): a physician's browser
    // has no use for a field naming which admin sub-tab is open, and leaving
    // the keys behind would invite a renderer back in after them.
    task: null,            // current blinded task
    draft: null,           // in-progress submission draft
    timerStart: null,   // null = the clock is stopped (see getElapsed/stopTimer)
    baseElapsed: 0,
    timerInterval: null,
    submitting: false,
    assistLoadingFor: null, // task_id of the /assist/prelabel fetch in flight
    assistFailedFor: null,  // task_id whose assist fetch failed (retry next load)
    showFullText: false,    // compare view: full text vs highlighted diff
    portalChosen: false,    // has the evaluator picked V1/V2 on the home page yet
    // The version the SERVER served this task under, which is what the record is
    // stamped with. Differs from the picked one when a physician who finished the
    // real cases is continued onto the synthetic queue (state.continuedFrom then
    // names the flow they picked, so the UI can say what happened).
    servedVersion: null,
    continuedFrom: null,
    specialtyChosen: false, // has the evaluator picked a specialty this session (V3/V4)
    specialties: null,      // cached GET /specialties listing (drives the picker)
    // First-run tutorial (Calibration Case 1). Null when not running; set by
    // startTutorial to {active, replay, idx}. Server state (user.tutorial) is
    // the launch authority; this is only the live tour position.
    tutorial: null,
    instrOpen: false,       // instruction drawer visibility
    // Admin-only (PRD-1 §4.1): the reviewer surface is being PREVIEWED, so the
    // draw is unclaimed, no session opens, and a submit is refused with a 409.
    // Never true for a real reviewer — reviewPreviewMode() re-checks the role
    // rather than trusting this flag on its own.
    reviewPreview: false,
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

  // Copyable ids (Export & Approval PRD 1.3) live in the ADMIN bundle now.
  // `copyableId` and its clipboard fallback moved to admin_shell.js with the
  // rest of the console, and reach the section modules on the section ctx. Ids
  // render only on the console, so the physician bundle carried an unused copy
  // of the same three functions after the move. It is deleted rather than kept
  // in step: the PRD asks for ONE id renderer, and two copies of it that
  // nothing in this file calls are two implementations waiting to drift.

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
      // `opts.base` overrides the asclepius prefix for the handful of admin
      // reads that live outside it (the landing lead table is on /api/leads,
      // beside the public form that writes it). Everything else defaults to
      // API_BASE, so no caller has to know the prefix exists.
      res = await fetch((opts.base || API_BASE) + path,
                        { method: opts.method || 'GET', headers, body });
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
        practiceGate: res.headers.get(PRACTICE_GATE_HEADER),
        agreementGate: res.headers.get(AGREEMENT_GATE_HEADER),
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
    // Same reasoning as logout(): a session that expired mid-case did not
    // un-work the case. Persist before the clock stops and the chrome goes.
    saveDraft();
    state.token = null;
    state.user = null;
    localStorage.removeItem(TOKEN_KEY);
    // A signed-out session is a no-work transition like any other: stop Agent
    // P's beats and take the review keyboard handler off the document, or it
    // outlives the screen it belongs to and keys keep mutating a detached DOM.
    teardownReview();
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
    // TASKS AND DASHBOARD WERE THE SAME SCREEN UNDER TWO NAMES.
    //
    // A "Dashboard" button lived here and a "Tasks" item lived in the rail, and
    // both called renderDashboardView(). A physician read that as two places,
    // pressed both, and got the same page twice: the product looked like it had
    // lost their work rather than like it had one home.
    //
    // The rail item stays, because the rail is where every other destination
    // is. What is removed is the duplicate, and removing it is what surfaced
    // the bug fixed in setPanel below: the header button was the only control
    // that actually worked from inside a case.
    const isAdmin = state.user.role === 'admin' || state.user.role === 'qa_reviewer';
    if (isAdmin) {
      // Admin only. Evaluate opens a two-way chooser so an operator can see BOTH
      // physician experiences without holding two accounts. It changes what is
      // rendered, never what is recorded — the reviewer side draws in PREVIEW
      // mode, which opens no session and is refused at submit (§4.1).
      //
      // ...but only when the session actually holds `review`. A qa_reviewer is
      // an admin for this button and NOT an admin for the capability table
      // (`capabilities.granted` overrides for role 'admin' alone), so offering
      // them the chooser would offer a door that bounces them straight back to
      // the dashboard. Without the capability the button stays what it was.
      const canChoose = sessionCan('review');
      nav.appendChild(h('button', {
        class: 'asc-nav-btn'
          + ((state.view === 'eval' || state.view === 'review') ? ' active' : ''),
        'aria-haspopup': canChoose ? 'menu' : null,
        onClick: (e) => (canChoose ? openEvaluateChooser(e.currentTarget) : switchView('eval')),
      }, 'Evaluate'));
      // A LINK, not a view (PRD-F R3/F6). The console is its own page now, so
      // there is no admin view left in this shell to switch to — and an anchor
      // is what lets an operator open it in a second tab beside the physician
      // surface they were checking, which is most of what they were doing with
      // it anyway.
      nav.appendChild(h('a', {
        class: 'asc-nav-btn asc-nav-link', href: '/asclepius/admin',
      }, 'Admin console'));
    }
    // Community entry lives in the persistent SIDE PANEL (per the Community
    // PRD §1 and the Side Panel PRD), not the header: see renderSidePanel().

    // No identity in the header. It is at the foot of the rail, once, and this
    // used to be a second copy of it: email, role word, specialty, and its own
    // Sign out beside the rail's own Sign out.
    //
    // The role word is gone with it and is not missed. Every non-physician
    // account is provisioned role="evaluator" so the rest of the portal keeps
    // working, which meant an advisor's own header called them an "evaluator",
    // and a physician's called them one too: a routing word, printed at
    // somebody as though it were their job title.

    // The corner ? tab (below) is the single help entry point: replay the
    // practice case or view a summary of it. No separate header control.
  }

  function switchView(view) {
    // Leaving the labeling view for review is a draft hazard: setRoot() is
    // about to wipe an in-progress case, and an admin using the Evaluate
    // chooser to look at the other surface must not lose a draft to it.
    if (view === 'review' && state.view === 'eval') saveDraft();
    // Leaving review is a NO-WORK transition, exactly like an empty queue: the
    // reviewer is no longer looking at a pair, so Agent P's beats must stop and
    // the module's keyboard handler must come off the document. Without this a
    // session went on accruing paid time against the Guide.
    if (state.view === 'review' && view !== 'review') teardownReview();
    state.view = view;
    // The header nav (Evaluate / Admin console) always lands back inside the
    // Tasks side-panel destination: the Guide is a peer, not a sub-view of it.
    // Leaving the Guide this way must also stop its scroll-spy observer.
    if (state.panel === 'guide' && guideObserver) { guideObserver.disconnect(); guideObserver = null; }
    state.panel = 'tasks';
    renderHeader();
    renderSidePanel();
    if (view === 'home') renderDashboardView();
    else if (view === 'review') renderReviewView();
    else renderEvalView();
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     REVIEW, INSIDE THE SHELL (PRD-1 §2.1)

     Review used to be `window.open('/asclepius/review', '_blank')` — a second
     application with its own hyperscript, its own token read, its own class
     namespace, no rail and no way back. It is now a view in this shell, so a
     reviewer keeps Community, Guide and Earnings one click away and a finished
     pair lands them on their dashboard rather than on a dead tab.

     `review.js` is mounted through the same `render(el, ctx)` contract the
     admin sections use. The ctx it gets is the shell's own hyperscript, fetch
     helper and case-panel context — that sharing IS the fix, not a convenience.
     ═══════════════════════════════════════════════════════════════════════════ */
  // Preview mode is an ADMIN-ONLY rendering choice, remembered for the tab (§4):
  // sessionStorage, never localStorage, so a reload keeps context but a new
  // session starts neutral.
  const EVAL_CHOICE_KEY = 'asclepius_eval_surface';
  function reviewPreviewMode() {
    // Only an admin can be previewing. A real reviewer drawing a real pair must
    // never inherit a stale flag from anything the browser remembers.
    return isAdminUser() && state.reviewPreview === true;
  }
  function isAdminUser() {
    return !!state.user && (state.user.role === 'admin' || state.user.role === 'qa_reviewer');
  }
  /* Draft storage for a section module.
   *
   * The shell owns localStorage. review.js deliberately has none of its own —
   * its own hyperscript and its own token read were two of the four reasons
   * review was structurally a different application, and a test pins that it
   * has not grown them back. So a module that needs to survive a refresh asks
   * for it through the ctx, exactly as it asks for `h` and `api`.
   */
  const SECTION_DRAFT_PREFIX = 'asclepius_section_draft_';
  function sectionDraftStore(namespace) {
    const keyFor = (id) => SECTION_DRAFT_PREFIX + namespace + ':' + id;
    return {
      save(id, value) {
        if (!id) return;
        try { localStorage.setItem(keyFor(id), JSON.stringify(value)); } catch (e) { /* quota */ }
      },
      load(id) {
        if (!id) return null;
        try {
          const raw = localStorage.getItem(keyFor(id));
          return raw ? JSON.parse(raw) : null;
        } catch (e) { return null; }
      },
      clear(id) {
        if (!id) return;
        try { localStorage.removeItem(keyFor(id)); } catch (e) { /* ignore */ }
      },
    };
  }

  /* The ctx a plain physician-facing section module gets (Referral, Earnings).
   *
   * It used to be `sectionCtx()`, borrowed from the console because the
   * console lived in this file. The console is its own page now, and a
   * physician's referral card has no business being handed an upload-specialty
   * resolver or a jump into Task Routing. */
  function sectionCtx() {
    return { h, api, clear, toast, loadingCard, fmtDate, avatarBlob: loadAvatarBlob };
  }

  function reviewSectionCtx() {
    return {
      h, api, clear, toast, loadingCard, fmtDate,
      // Survives a refresh mid-adjudication. Judgment on a hard pair is
      // minutes of a senior physician's reading, and losing it to a stray
      // reload is how a reviewer learns not to trust the surface.
      drafts: sectionDraftStore('review'),
      // The clinical chart, from the shared module — the SAME component the
      // labeler reads. Handing over the ctx rather than a rendered panel is what
      // keeps the reviewer's chart from becoming a second implementation.
      casePanelCtx,
      specialties: state.specialties,
      preview: reviewPreviewMode(),
      goHome: () => switchView('home'),
    };
  }
  function teardownReview() {
    if (window.AsclepiusReview && typeof window.AsclepiusReview.teardown === 'function') {
      try { window.AsclepiusReview.teardown(); } catch (e) { /* never block navigation */ }
    }
  }
  function renderReviewView() {
    stopTimer();
    updateHeaderProgress();
    // Re-checked here, not only hidden in the rail: a hand-typed state change
    // must not open a surface the session was never granted. (The API 403s
    // regardless — this is so the operator sees a sentence, not a stack trace.)
    if (!sessionCan('review')) { switchView('home'); return; }
    const host = h('div', { class: 'asc-wrap asc-wrap-review' });
    setRoot(host);
    if (window.AsclepiusReview && typeof window.AsclepiusReview.render === 'function') {
      window.AsclepiusReview.render(host, reviewSectionCtx());
    } else {
      sectionModuleMissing(host, 'The review console');
    }
  }

  /* ═══ §4 — the admin Evaluate chooser ══════════════════════════════════════
     An operator needs to see BOTH physician experiences without holding two
     accounts. Two rows, anchored to the button: Labeler builds an answer,
     Reviewer adjudicates a pair. Dismisses on outside click and on Escape, and
     returns focus to the button it came from. */
  let evalChooser = null;
  function closeEvaluateChooser(refocus) {
    if (!evalChooser) return;
    const { pop, onDocClick, onKey, anchor } = evalChooser;
    document.removeEventListener('mousedown', onDocClick, true);
    document.removeEventListener('keydown', onKey, true);
    if (pop.parentNode) pop.parentNode.removeChild(pop);
    evalChooser = null;
    if (refocus && anchor && typeof anchor.focus === 'function') anchor.focus();
  }
  function openEvaluateChooser(anchor) {
    if (evalChooser) { closeEvaluateChooser(true); return; }
    const choose = (surface) => {
      try { sessionStorage.setItem(EVAL_CHOICE_KEY, surface); } catch (e) { /* private mode */ }
      closeEvaluateChooser(false);
      if (surface === 'reviewer') {
        // The preview is what makes this safe to click: it draws a pair without
        // claiming it, opens no session, and is refused at submit with a 409.
        state.reviewPreview = true;
        switchView('review');
      } else {
        state.reviewPreview = false;
        switchView('eval');
      }
    };
    const row = (surface, title, sub) => h('button', {
      class: 'asc-help-menu-item', type: 'button', role: 'menuitem',
      dataset: { evalSurface: surface },
      onClick: () => choose(surface),
    }, h('b', {}, title), ' ', h('span', { class: 'asc-rv-chrome' }, sub));
    const pop = h('div', { class: 'asc-help-menu asc-eval-chooser', role: 'menu' },
      row('labeler', 'Labeler', 'build an answer'),
      row('reviewer', 'Reviewer', 'adjudicate a pair'));
    // Anchored to the button, portaled to <body> so a re-render of #ascRoot
    // cannot leave it orphaned mid-air. Viewport coordinates, because
    // `.asc-help-menu` is `position: fixed` — the shared primitive keeps its own
    // positioning model and its own stacking order rather than being talked out
    // of either one property at a time.
    const rect = anchor.getBoundingClientRect();
    pop.style.top = (rect.bottom + 6) + 'px';
    pop.style.left = rect.left + 'px';
    const onDocClick = (e) => {
      if (pop.contains && pop.contains(e.target)) return;
      if (anchor.contains && anchor.contains(e.target)) return;
      closeEvaluateChooser(false);
    };
    const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); closeEvaluateChooser(true); } };
    document.body.appendChild(pop);
    document.addEventListener('mousedown', onDocClick, true);
    document.addEventListener('keydown', onKey, true);
    evalChooser = { pop, onDocClick, onKey, anchor };
    const first = pop.children[0];
    if (first && typeof first.focus === 'function') first.focus();
  }

  // ─── Side panel destination router (Tasks / Guide; Community is external) ────
  // Tasks re-enters the existing header view (eval or admin); Guide renders the
  // in-portal Instruction Manual. Community never routes here: it opens a tab.
  function setPanel(dest) {
    // The walkthrough and the re-entry page hold DOCUMENT-level key handlers
    // (Esc closes the demo player; Esc leaves the re-entry page). Navigating away
    // through the rail replaces the screen without telling the module, so those
    // handlers would outlive the screen they belong to — and a stray Esc on the
    // dashboard would then defer a physician's remaining stops and bounce them
    // somewhere they did not ask to go. Teardown is idempotent, so calling it on
    // every navigation is cheaper than reasoning about which ones need it.
    teardownFirstRun();
    if (dest === 'community') { openCommunity(); return; }
    // PRD-1 §2.1: the expert review console is a VIEW IN THIS SHELL, not a
    // second page in a new tab. It keeps the rail, the session and the design
    // system. Still gated on the SERVER's capability list, never on a tier
    // string — the rail hides it and this re-checks it.
    if (dest === 'review') {
      if (!sessionCan('review')) return;
      state.reviewPreview = false;   // a real reviewer, never an admin preview
      switchView('review');
      return;
    }
    if (dest !== 'tasks' && dest !== 'guide' && dest !== 'earnings'
        && dest !== 'referral' && dest !== 'profile' && dest !== 'verification') return;
    // Server-gated destinations are re-checked here, not only hidden in the
    // rail: a stale deep link or a hand-typed state change must not open a
    // section the session was never granted. (The API 403s regardless.)
    if (dest === 'referral' && !sessionHasSurface('referral')) return;
    // Tasks opens for anyone who has work to do there OR a practice case to run
    // in it. A referral-only account has neither, and a hand-typed hash must not
    // land them on a dashboard built for physicians. (The API 403s regardless.)
    if (dest === 'tasks' && !sessionHasSurface('real_work')
        && !sessionHasSurface('tutorial')) return;
    if (dest === 'verification') { state.panel = dest; renderVerificationPanel(); return; }
    // TASKS FROM INSIDE A CASE.
    //
    // A physician in a case already has state.panel === 'tasks', so the
    // early return below made the rail's Tasks button do nothing at all. The
    // header's Dashboard button was masking it, and removing that duplicate is
    // what made it reachable: the only way out of a case would have been the
    // browser's Back button.
    //
    // Handled BEFORE the early return rather than by weakening it, so every
    // other destination keeps its "already here, do not refetch" behaviour.
    if (dest === 'tasks' && state.panel === 'tasks' && state.view !== 'home') {
      saveDraft();
      if (state.view === 'review') teardownReview();
      state.view = 'home';
      renderSidePanel();
      renderDashboardView();
      return;
    }
    if (dest === state.panel) return; // already here: no needless re-render/refetch
    // Leaving the review surface for another rail destination is a no-work
    // transition: Agent P's beats must stop and the keyboard handler must come
    // off the document, or a session accrues paid time against the Guide.
    // Coming back re-mounts and draws again; the abandoned claim expires on its
    // lease, which is what the lease is for.
    if (state.view === 'review' && dest !== 'tasks') teardownReview();
    saveDraft(); // preserve any in-progress eval draft before setRoot() wipes it
    // Leaving the Guide: stop the scroll-spy observer so it never watches the
    // detached section nodes that setRoot() is about to replace.
    if (dest !== 'guide' && guideObserver) { guideObserver.disconnect(); guideObserver = null; }
    state.panel = dest;
    // §6: state.view is sticky, so a physician who was inside a case, went to
    // Earnings, and clicked Tasks landed back INSIDE the case — renderEvalView
    // resumes whatever state.view still said. Clicking Tasks means "take me to
    // my dashboard", never "resume whatever I had open": Continue is a
    // deliberate choice made ON the dashboard (§4.1), not a side effect of
    // navigating. saveDraft() already ran above, so nothing is lost.
    //
    // The 'admin' exemption that used to sit here went with the console: Tasks
    // now means the same thing for every session, because the console is a
    // different page and nobody is inside it while they are inside this one.
    if (dest === 'tasks') state.view = 'home';
    renderSidePanel();
    if (dest === 'referral') {
      renderReferralView();
    } else if (dest === 'profile') {
      renderProfileView();
    } else if (dest === 'earnings') {
      renderEarningsView();
    } else if (dest === 'guide') {
      renderGuide();
    } else if (state.view === 'review') {
      renderReviewView();
    } else if (state.view === 'home') {
      // The branch the line above depends on. Without it 'home' fell through to
      // renderEvalView() and the reset had no destination.
      renderDashboardView();
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

  //: The per-destination styling hook, written out rather than built from
  //: item.dest, so a grep for a class finds it and the CSS scanner sees it.
  //:
  //: Referral used to be filled green here, as "the tab that pays". Beside four
  //: plain tabs that reads as the one that needs attention, and for a physician
  //: waiting on credentials it was the ONLY tab they could act on, so the rail
  //: pointed them at referring colleagues rather than at their own application.
  //: The class stays wired; the fill is gone from the stylesheet.
  const RAIL_ITEM_CLASS = {
    tasks: 'asc-rail-item-tasks',
    community: 'asc-rail-item-community',
    referral: 'asc-rail-item-referral',
    earnings: 'asc-rail-item-earnings',
    guide: 'asc-rail-item-guide',
  };

  const RAIL_ITEMS = [
    // `surface` is the access-level axis (see backend asclepius/capabilities.py).
    // It behaves differently from `capability` on purpose: a capability the
    // session lacks HIDES the item (a labeler should never see a Review tab they
    // will not get), whereas a surface it lacks SHOWS IT LOCKED. A physician
    // waiting on credentials is going to get these in a day or two, and hiding
    // them makes the product look empty at exactly the moment we are trying to
    // show them what they joined.
    // TASKS is gated on `tutorial`, not on `real_work`. It is the way to the
    // practice case and to the examination, which are the only things an
    // applicant is asked to do, so locking it locked the one door we want them
    // to walk through. What is behind it changes with access: an approved
    // physician gets their case queue, an applicant gets the credentialing
    // path. See renderDashboardView.
    { dest: 'tasks',     label: 'Tasks', surface: 'tutorial' },
    // COMMUNITY has no `surface` any more, so it never locks. An applicant is
    // not admitted to the real rooms and that has not changed: they are sent to
    // a PREVIEW instead, rendered from a fixture through this same interface,
    // so they can see what they are applying to without a single real colleague
    // or real message reaching an account nobody has checked yet.
    { dest: 'community', label: 'Community', external: true },
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
    // Profile is NOT a rail item. It is reached from the identity chip at the
    // foot, which is where somebody looks for it: it is the thing behind your
    // own name rather than a place to go. It stays ungated there for the same
    // reason it was ungated here -- a physician can always read what we hold
    // about them, including while they wait, because they are the people most
    // likely to have just noticed a typo in what they submitted.
    //
    // setPanel validates against its own allowlist and never consults this
    // array, so the route is unaffected.
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
  /* A referral-only account holds a link and nothing else. Showing them the
     rail every physician sees, with four locked doors and a hint about
     credentials clearing, promises a product they were never given and cannot
     get. They see what they actually have. */
  function isReferralOnly() {
    return !!(state.user && state.user.account_kind === 'referrer');
  }

  /* An advisor: the whole product, view-only, with the referral page as the one
     thing they can act on. They are not waiting for anything -- no credentials
     are being checked -- so every "opens when your credentials clear" affordance
     is wrong for them and is replaced rather than reused. */
  function isAdvisor() {
    return !!(state.user && state.user.account_kind === 'advisor');
  }

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
    const items = RAIL_ITEMS
      .filter((it) => !it.capability || sessionCan(it.capability))
      .map((it) => Object.assign({}, it, {
        locked: !!it.surface && !sessionHasSurface(it.surface),
      }));
    // A referral-only account gets the rail it can use, not the physician's
    // rail with four locked doors on it. Guide is out too: it is the manual
    // for labeling cases, and this account will never see one -- handing them
    // instructions for work they cannot do is the same broken promise as a
    // locked Tasks tab, just quieter.
    if (isReferralOnly()) {
      // Profile left this list with the rail tab, not with their access: the
      // chip at the foot is on every screen and opens it for them too.
      return items.filter((it) => it.dest === 'referral');
    }
    // An advisor's Tasks tab is not a locked door either. It has something
    // behind it -- the practice case, which is the whole point of showing them
    // around -- and a padlock with "opens when your credentials clear" would
    // promise something that is never going to happen to them.
    if (isAdvisor()) {
      return items.map((it) => (it.dest === 'tasks'
        ? Object.assign({}, it, { locked: false, lockedHint: null })
        : it));
    }
    return items;
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

  /** Best-effort human display name.
   *
   *  The server-side name is the answer whenever there is one, and after the
   *  signup rework there almost always is: the wizard writes first and last
   *  name onto the account at provisioning.
   *
   *  The fallback is what this function is really about, because it used to be
   *  wrong in a way that was worse than saying nothing. It title-cased the
   *  email local part and prefixed "Dr.", so `angad18.bhatia@gmail.com` was
   *  greeted as "Dr. Angad18 Bhatia" on his own dashboard: a typo we appeared
   *  to have made about a physician's name, on the first screen he saw.
   *
   *  Two rules now. Digits come out, and any token that was nothing but digits
   *  is dropped, because a mailbox number is not part of anyone's name. And no
   *  honorific: an email address is not evidence that this person is a doctor,
   *  and calling an unverified applicant "Dr." is a claim we have not checked.
   *  When the server knows the name it carries whatever honorific it was given,
   *  which is the only place one belongs.
   */
  function railDisplayName() {
    const u = state.user || {};
    const explicit = String(u.name || u.full_name || u.display_name || '').trim();
    if (explicit) return explicit;
    const local = String(u.email || '').split('@')[0] || '';
    const words = local
      .replace(/[._+-]+/g, ' ')
      .split(/\s+/)
      .map((w) => w.replace(/\d+/g, ''))
      .filter((w) => w.length >= 2);
    if (!words.length) return 'Clinician';
    return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  }

  /** The circle of initials (or the physician's photo) in the rail foot.
   *
   *  Decorative: the name it stands for is the very next element, so a screen
   *  reader announcing "TP" before "Dr. Tej Patel" would be noise.
   *
   *  `railDisplayName()` returns `Dr. Tej Patel` for an email-derived name, and
   *  fallbackInitials would read that as first+last and return `DP`. Strip the
   *  honorific first — the initials are the physician's, not the title's.
   *
   *  If they have uploaded a photo, prefer it, through the SAME authenticated
   *  blob path the profile page uses (avatarImgEl) rather than a second image
   *  implementation: an <img src> cannot send the bearer header the avatar
   *  endpoint requires, and the initials stay put if that fetch fails.
   */
  function railAvatarEl(specColor) {
    const initials = fallbackInitials(railDisplayName().replace(/^Dr\.?\s*/i, ''));
    const url = (state.user && state.user.avatar_url) || null;
    return h('span', {
      class: 'asc-rail-avatar acc-' + String(specColor).replace('dot-', '')
        + (url ? ' has-img' : ''),
      'aria-hidden': 'true',
    }, url ? avatarImgEl(url, initials) : initials);
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
      if (item.dest === 'community' && sessionHasSurface('community_read')) {
        children.push(communityBadgeEl());
      }
      if (sessionIsProvisional() && !isAdvisor()
          && VIEW_ONLY_DESTS.indexOf(item.dest) !== -1) {
        children.push(viewOnlyBadgeEl());
      }
      if (item.locked) children.push(h('span', { class: 'asc-rail-lock', 'aria-hidden': 'true' }, '\u00b7'));
      nav.appendChild(h('button', {
        type: 'button',
        // The per-destination class is a STYLING HOOK and nothing else: the
        // rail carried only active/locked, so there was no way to say "the
        // Referral tab is the green one" without one. Do not gate behaviour on
        // it; behaviour reads item.dest.
        class: 'asc-rail-item ' + (RAIL_ITEM_CLASS[item.dest] || '')
          + (active ? ' active' : '') + (item.locked ? ' locked' : ''),
        'aria-current': active ? 'page' : null,
        'aria-disabled': item.locked ? 'true' : null,
        // aria-label carries the accessible name even in the icon-collapsed rail,
        // where the visible label span is display:none (and thus off the a11y tree).
        //
        // lockedHint was authored, propagated through visibleRailItems, and
        // then rendered by nothing: a locked tab showed a bare middot and no
        // explanation. "Opens when your credentials clear" is the one sentence
        // a waiting physician most needs, and it was already written.
        'aria-label': item.locked && item.lockedHint
          ? item.label + ': ' + item.lockedHint
          : (item.external ? item.label + ' (opens in a new tab)' : item.label),
        title: item.locked && item.lockedHint
          ? item.lockedHint
          : (item.external ? item.label + ' (opens in a new tab)' : item.label),
        onClick: () => {
          // A locked surface opens the explanation, not a 403.
          if (item.locked) { setPanel('verification'); return; }
          setPanel(item.dest);
        },
      }, children));
    });

    // The one identity in the product: a name, and the two things you can do
    // with it.
    //
    // The specialty WORD is gone. A physician does not need their own
    // specialty printed back at them, and it was the second half of a chip
    // that already had their name on it. The specialty HUE stays on the
    // avatar, because that is a colour rather than a label.
    //
    // Profile sits here rather than in the rail: it is not a destination in
    // the way Tasks and Earnings are, it is the thing behind your own name,
    // and putting it here is what let the rail lose a tab.
    const specColor = specialtyDotColor(state.user.specialty);
    const foot = h('div', { class: 'asc-rail-foot' },
      // The row itself opens Profile. On the mobile tab bar the text links
      // below are hidden and the avatar is the only handle there is, so the
      // affordance has to be on the thing that survives.
      h('div', { class: 'asc-rail-user', role: 'button', tabindex: '0',
                 title: 'Profile', 'aria-label': 'Profile',
                 onClick: () => setPanel('profile') },
        railAvatarEl(specColor),
        h('div', { class: 'asc-rail-usertext' },
          h('span', { class: 'asc-rail-name', title: railDisplayName() }, railDisplayName()))),
      h('div', { class: 'asc-rail-footlinks' },
        h('button', { type: 'button', class: 'asc-rail-signout',
                      onClick: () => setPanel('profile') }, 'Profile'),
        h('button', { type: 'button', class: 'asc-rail-signout',
                      onClick: logout }, 'Sign out')));

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

  //: Which rail destinations are LOOK-ONLY while an application is under review.
  //:
  //: Not "locked". A locked tab says "you cannot go here"; these open, show the
  //: real interface, and cannot be acted on. The distinction is the point of
  //: the change: an applicant should be able to see what they are joining, and
  //: a padlock on every tab told them nothing except to wait.
  //:
  //: Tasks is absent on purpose. The practice case and the examination ARE
  //: their work, and marking the one tab they can act on "view only" would be
  //: the exact opposite of true.
  const VIEW_ONLY_DESTS = ['community', 'referral', 'earnings'];

  function viewOnlyBadgeEl() {
    // The full sentence goes in title/aria-label; the visible chip is two words
    // because the icon-collapsed rail hides .asc-rail-label and a long chip
    // would be clipped rather than read.
    return h('span', {
      class: 'asc-rail-badge asc-rail-badge-viewonly',
      title: 'View only until your application is approved',
      'aria-label': 'View only until your application is approved',
    },
      h('span', { class: 'dot dot-orange', 'aria-hidden': 'true' }),
      h('span', { class: 'asc-rail-badge-n' }, 'View only'));
  }

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
    // An account that cannot read the real community opens the PREVIEW: the
    // same interface, rendered from a fixture, so an applicant can see what
    // they are applying to. Sending them to the real page would open a tab
    // that 403s, which is what used to happen from the dashboard's "Meet the
    // community" button.
    if (!sessionHasSurface('community_read')) {
      window.open('/community?preview=1', '_blank', 'noopener');
      return;
    }
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

  // The emailed sign-in link lands here as ?signin=<token>. Same shape as the
  // handoff above, and stripped from the address bar the same way: the token is
  // single use and fifteen minutes long, but a URL sitting in a shared browser's
  // history is a URL somebody pastes into a bug report.
  async function consumeSigninLinkFromUrl() {
    const url = new URL(window.location.href);
    const token = (url.searchParams.get('signin') || '').trim();
    if (!token) return;
    url.searchParams.delete('signin');
    const qs = url.searchParams.toString();
    window.history.replaceState(null, '', url.pathname + (qs ? '?' + qs : '') + url.hash);
    try {
      const res = await fetch(API_BASE + '/auth/signin-link/exchange', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token }),
      });
      if (!res.ok) return;
      const data = await res.json().catch(() => null);
      if (data && data.token) {
        state.token = data.token;
        state.user = data.user || null;
        try { localStorage.setItem(TOKEN_KEY, data.token); } catch (_) { /* ignore quota */ }
        // An applicant who asked for this link means to be THIS account, not
        // whichever clinical session the browser happens to hold.
        try { localStorage.setItem(SUPPRESS_SSO_KEY, '1'); } catch (_) { /* ignore */ }
      }
    } catch (_) { /* network error: fall through to the normal boot sequence */ }
  }

  async function boot() {
    await consumeHandoffFromUrl();
    await consumeSigninLinkFromUrl();
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

  /* True when the URL asks for the review surface, consuming the hash so the
     request is honoured exactly once. Kept narrow: only the bare `#review`,
     never a prefix match, so the Guide's `#section-id` deep links and the
     onboarding module's `#/calibration` route are untouched. */
  function readReviewHash() {
    let hash = '';
    try { hash = (location.hash || '').replace(/^#/, ''); } catch (e) { hash = ''; }
    if (hash !== 'review') return false;
    try { history.replaceState(null, '', location.pathname + location.search); }
    catch (e) { /* a browser that refuses is not a reason to lose the route */ }
    return true;
  }

  function enterApp() {
    // Land on the dashboard (home), not straight into a case. The dashboard
    // routes into the existing eval flow when the doctor picks or starts a case.
    state.view = 'home';
    state.panel = 'tasks';
    // §4: an ADMIN who chose the Reviewer surface in this tab keeps it across a
    // reload. sessionStorage, so a new session starts neutral, and re-gated on
    // the role AND the capability so a stale key can never put a non-admin on a
    // preview draw.
    state.reviewPreview = false;
    if (isAdminUser() && sessionCan('review')) {
      let stored = null;
      try { stored = sessionStorage.getItem(EVAL_CHOICE_KEY); } catch (e) { stored = null; }
      if (stored === 'reviewer') state.reviewPreview = true;
    }
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
    if (isReferralOnly()) { setPanel('referral'); return; }
    // ── Onboarding v2 §0.1 decision 1 ────────────────────────────────────────
    // This account is signed in on the TEMPORARY password from the welcome
    // email. Before anything else, they choose their own. One screen, and it is
    // the first thing after sign-in rather than a banner they can walk past:
    // the credential in their inbox stops working the moment they do this, and
    // that is the whole reason it is temporary.
    if (state.user.must_change_password) { renderRotateTempPassword(); return; }
    // `/asclepius/review` redirects here with `#review` (PRD-1 §2.1). The old
    // standalone URL is in bookmarks and in email we have already sent, so it
    // has to land a reviewer on their work rather than on a generic dashboard.
    // Consumed once — the hash is cleared so a later reload is not permanently
    // pinned to review — and gated on the capability like every other route in.
    if (readReviewHash() && sessionCan('review')) { setPanel('review'); return; }
    // §6: the first-login walkthrough. A newly approved physician lands in the
    // welcome letter, not on a dashboard they have to reverse-engineer.
    //
    // AFTER the #review hash, deliberately. That hash arrives from a link we
    // already emailed, and returning before it is read would both ignore the
    // link and leave it in the URL to fire on some later reload. Admins, QA
    // reviewers and advisors are excluded here rather than inside the module:
    // the role is the shell's knowledge, the checklist is the module's.
    // Welcome package v2 §2: ONE function decides what a login sees, and the
    // shell branches on its answer instead of on a yes/no. 'walkthrough' opens
    // the stops; 'reentry' is the short interstitial logins 2 and 3 get once the
    // required stops are done; 'banner' and 'none' put nothing in the way at all
    // — a physician who has done the practice case reaches Start new case in one
    // click, on every login, forever.
    const frMode = firstRunMode();
    if (frMode === 'walkthrough') { startFirstRun(); return; }
    if (frMode === 'reentry') { openFirstRunReentry(); return; }
    // An advisor lands on the dashboard, not inside the tutorial. Being dropped
    // straight into a case is right for a physician whose first job is to learn
    // the interface; someone here to look around should be shown the product
    // and offered the practice case, not started in the middle of one.
    // Launched off the GATE, not off `status`. The two answer different
    // questions now: status is what the physician did, gate_state is whether
    // real work is open to them. Reading status is how a legacy `skipped`
    // account would never be relaunched, and since skipping no longer grants
    // anything, that account would sit on a dashboard where every button 403s
    // with no route back to the one thing that unlocks it.
    const tut = (state.user && state.user.tutorial) || {};
    const gateOpen = tut.gate_state === 'passed' || tut.gate_state === 'grandfathered';
    if (state.user.role === 'evaluator' && !isAdvisor() && !gateOpen) {
      startTutorial({});
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
    // §5.2: the elapsed on record must be the elapsed actually worked. Save
    // before anything is torn down — saveDraft() reads state.draft and a live
    // getElapsed() — then stop, so the clock cannot run on past the session.
    saveDraft();
    stopTimer();
    state.token = null;
    state.user = null;
    teardownReview();
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

  // ═══════════════════════════════════════════════════════════════════════════
  //  Onboarding v2 §0.1 — rotate the temporary password.
  //
  //  The welcome email (§4.4) carries a credential the physician did not choose.
  //  That is the one place this build departs from the ask, and it departs in
  //  their favour: their experience is identical — credentials in the email,
  //  sign in from the website, works first time — but a permanent plaintext
  //  password sits in an inbox forever and survives an inbox breach.
  //
  //  This screen is the price of that, and it is one screen. It is also not
  //  skippable, which is the point: `must_change_password` is retired
  //  server-side by set_user_password and by nothing else, so a client that
  //  routed past this would land on a portal still flagged for rotation.
  // ═══════════════════════════════════════════════════════════════════════════
  const ROTATE_MIN = 12;

  function renderRotateTempPassword() {
    const tempInput = h('input', { class: 'asc-input', type: 'password',
      placeholder: 'The password from your welcome email',
      autocomplete: 'current-password' });
    const nextInput = h('input', { class: 'asc-input', type: 'password',
      placeholder: 'Choose a password', autocomplete: 'new-password' });
    const confirmInput = h('input', { class: 'asc-input', type: 'password',
      placeholder: 'Type it again', autocomplete: 'new-password' });
    const errBox = h('div', { class: 'asc-login-error', hidden: true });
    const submitBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-block asc-btn-lg',
      type: 'submit' }, 'Set my password');

    // A live checklist rather than a regex error after the fact, and no
    // composition rules: "one symbol and one digit" reliably produces
    // Password1! and nothing safer. Length is the requirement that helps.
    const rules = [
      { id: 'len', label: 'At least ' + ROTATE_MIN + ' characters',
        ok: (v) => v.length >= ROTATE_MIN },
      { id: 'diff', label: 'Different from the emailed one',
        ok: (v) => v.length > 0 && v !== tempInput.value },
      { id: 'match', label: 'Both entries match',
        ok: (v) => v.length > 0 && v === confirmInput.value },
    ];
    const ruleEls = rules.map((r) => h('li', { class: 'asc-fr-rotate-rule' },
      h('span', { class: 'asc-fr-rotate-dot', 'aria-hidden': 'true' }),
      h('span', {}, r.label)));

    function refresh() {
      const v = nextInput.value;
      let allOk = true;
      rules.forEach((r, i) => {
        const met = r.ok(v);
        allOk = allOk && met;
        ruleEls[i].classList.toggle('is-met', met);
      });
      if (allOk) submitBtn.removeAttribute('disabled');
      else submitBtn.setAttribute('disabled', '');
    }
    [tempInput, nextInput, confirmInput].forEach((el) =>
      el.addEventListener('input', refresh));

    const form = h('form', {
      onSubmit: async (e) => {
        e.preventDefault();
        errBox.setAttribute('hidden', '');
        submitBtn.setAttribute('disabled', '');
        submitBtn.textContent = 'Saving…';
        try {
          const res = await api('/auth/password/change', {
            method: 'POST',
            body: { current_password: tempInput.value, new_password: nextInput.value },
          });
          // The write invalidated the token this session is holding (every token
          // is checked against password_changed_at), so take the fresh one the
          // endpoint hands back or the next call 401s straight to the login form.
          if (res && res.token) {
            state.token = res.token;
            try { localStorage.setItem(TOKEN_KEY, res.token); } catch (_) { /* ignore */ }
          }
          state.user = await api('/auth/me');
          toast('Password set. Welcome aboard.', 'success');
          enterApp();
        } catch (err) {
          errBox.textContent = err.message || 'Could not set that password.';
          errBox.removeAttribute('hidden');
          submitBtn.textContent = 'Set my password';
          refresh();
        }
      },
    },
      errBox,
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Password from your welcome email'), tempInput),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Your new password'), nextInput),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Confirm it'), confirmInput),
      h('ul', { class: 'asc-fr-rotate-rules' }, ruleEls),
      submitBtn);

    refresh();
    const card = h('div', { class: 'asc-login-card' },
      h('div', { class: 'asc-login-head' },
        h('div', { class: 'asc-login-mark', 'aria-hidden': 'true' }),
        h('h1', {}, 'Choose your password'),
        h('p', {}, 'One screen, then you are in')),
      h('div', { class: 'asc-login-body' },
        h('p', { class: 'asc-fr-rotate-lede' },
          'The password we emailed you is temporary, it stops working as soon '
          + 'as you pick your own. Nothing else about your account changes.'),
        form));
    setRoot(h('div', { class: 'asc-login-wrap' }, card));
    setTimeout(() => tempInput.focus(), 30);
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  Onboarding v2 §6 — mounting the first-login walkthrough.
  //
  //  The module (first_run.js) owns the screens; the shell owns the session, the
  //  rail and the routes it hands off to. This is the whole seam between them.
  // ═══════════════════════════════════════════════════════════════════════════
  function firstRunCtx() {
    return {
      h, api, toast, setRoot,
      user: state.user,
      // Every stop transition returns the refreshed user, so the session's copy
      // of the checklist stays current without a second fetch — which is what
      // makes the dashboard chip below show the right count the moment the
      // walkthrough is left.
      onUser: (user) => { if (user) state.user = user; },
      startTutorial: () => startTutorial({ resume: false }),
      openCommunity,
      setPanel,
      // Leaving the walkthrough is a normal navigation, not a dismissal: the
      // open stops stay open and the dashboard chip brings them back. Same
      // teardown as setPanel, and for the same reason — this is the other way
      // out of a screen that holds document-level key handlers.
      exit: () => {
        teardownFirstRun();
        state.view = 'home'; state.panel = 'tasks'; renderDashboardView();
      },
    };
  }

  function startFirstRun() {
    if (!window.FirstRunWalkthrough) { renderDashboardView(); return; }
    window.FirstRunWalkthrough.start(firstRunCtx());
  }

  /** Re-open the walkthrough at its first unfinished stop.
   *
   *  Called when the practice case hands control back, and from the dashboard
   *  chip. Deliberately re-reads state.user rather than any local memory: the
   *  practice-case stop is checked off by the SERVER (from the tutorial's own
   *  transition), so the client's idea of the checklist is stale by definition
   *  at exactly this moment. */
  function resumeFirstRun() {
    if (!window.FirstRunWalkthrough) { renderDashboardView(); return; }
    window.FirstRunWalkthrough.resume(firstRunCtx());
  }

  /** What this login should see: 'walkthrough' | 'reentry' | 'banner' | 'none'.
   *
   *  The module owns the rule (§2) and this owns the ROLE question — admins, QA
   *  reviewers and advisors see none of these screens, and that is the shell's
   *  knowledge, not the checklist's. Same split as before; only the vocabulary
   *  got richer than a boolean. */
  function firstRunMode() {
    if (!window.FirstRunWalkthrough || !state.user) return 'none';
    if (state.user.role !== 'evaluator' || isAdvisor()) return 'none';
    // THE WELCOME PACKAGE IS FOR AN APPROVED COLLEAGUE.
    //
    // It is six stops of welcome letter, community, earnings and manual, and an
    // applicant was walked through all of it on their first sign-in, before
    // anybody had checked who they are. Half of it described things they cannot
    // reach, and it buried the two things they actually have to do.
    //
    // Suppressing it here rather than in six places also closes the practice
    // case's leave loop at the source: firstRunTourPending() reads this, and it
    // was what pulled a physician straight back into the case they had just
    // left. The package moves to approval, where it reads as a welcome instead
    // of as an obstacle.
    if (sessionIsProvisional()) return 'none';
    return window.FirstRunWalkthrough.mode(state.user);
  }

  /** True when this session still has walkthrough stops open — i.e. any stop not
   *  `done`. Kept for the chip, which reports progress rather than deciding
   *  which screen to paint; `firstRunMode()` is what routes. */
  function firstRunPending() {
    return firstRunMode() !== 'none';
  }

  /** Drop the walkthrough's document-level listeners. Safe to call at any time,
   *  including before the module has ever been started. */
  function teardownFirstRun() {
    if (!window.FirstRunWalkthrough) return;
    try { window.FirstRunWalkthrough.teardown(); } catch (e) { /* never block navigation */ }
  }

  /** True while the first pass through the tour is unfinished — some stop with
   *  no outcome at all. What the practice case's hand-back asks, because after
   *  passing it a physician should carry on through the tour on their FIRST
   *  login and land on their dashboard on any later replay. */
  function firstRunTourPending() {
    if (!window.FirstRunWalkthrough || !state.user) return false;
    if (state.user.role !== 'evaluator' || isAdvisor()) return false;
    if (window.FirstRunWalkthrough.mode(state.user) === 'none') return false;
    return window.FirstRunWalkthrough.tourPending(state.user);
  }

  /** §4.2 — the re-entry page. Reached on logins 2 and 3, and from the banner's
   *  button, so the two are the same flow at different volumes. */
  function openFirstRunReentry() {
    if (!window.FirstRunWalkthrough) { renderDashboardView(); return; }
    window.FirstRunWalkthrough.reentry(firstRunCtx());
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
            || "If that email has an Archangel Health account, we've sent a reset link.";
        } catch (_) {
          errBox.classList.add('asc-login-notice');
          errBox.textContent = 'Could not reach the server. Try again in a moment.';
        }
      },
    }, 'Forgot your password?');

    // THE "EMAIL ME A SIGN IN LINK" BUTTON IS GONE.
    //
    // It existed because onboarding v2 had no password step: an applicant who
    // closed the tab before a decision held no credential at all, so the only
    // route back to their own practice case was a magic link. The wizard now
    // takes a password on screen 1, so every new physician has an ordinary
    // credential and the ordinary door works.
    //
    // The BACKEND is deliberately untouched. /auth/signin-link and its exchange
    // both still work, so every link already sitting in a mailbox redeems for
    // its full fifteen minutes, and the legacy accounts that finished the
    // wizard during the passwordless window can still be issued one by hand.
    //
    // Those accounts do not need one, though, and that is worth stating because
    // it is not obvious: forgot_password mints a reset for ANY active user
    // without consulting password_is_unset, and set_user_password clears
    // must_change_password in the same statement. So "Forgot your password?"
    // below turns a legacy passwordless applicant into a normal account holder
    // with no admin, no magic link, and no new code. Hence the line under the
    // password field pointing them at it.

    // New here? Until this existed the sign-in screen was a closed door: a
    // physician who had never signed up had no route anywhere from it, and the
    // only hint was a sentence telling them to contact an administrator. The
    // signup door is on the landing app, which is a different origin in
    // production, so the URL is handed over in the shell (`asc-signup-url`).
    const signupUrl = (function () {
      const m = document.querySelector('meta[name="asc-signup-url"]');
      const v = m && m.getAttribute('content');
      return (v && v.trim()) || '';
    }());
    const signup = signupUrl
      ? h('p', { class: 'asc-login-signup' },
          document.createTextNode('New to Archangel Health? '),
          h('a', { href: signupUrl, class: 'asc-linkish' }, 'Apply to contribute'))
      : null;

    // The one sentence a legacy passwordless applicant needs. They applied
    // during the window when finishing the wizard minted no credential, so
    // "forgot your password" reads as the wrong door to them: they never had
    // one to forget. It is the right door anyway, because a reset SETS a
    // password rather than replacing one.
    const legacyHint = h('p', { class: 'asc-login-hint asc-login-legacy' },
      'Applied before and never set a password? Use Forgot your password.');

    const body = h('div', { class: 'asc-login-body' },
      form,
      h('div', { class: 'asc-login-forgot' }, forgot),
      legacyHint,
      // Was "Board-certified clinician access only", which is narrower than
      // the policy and is the last thing a retired physician, a fellow, or a
      // doctor licensed outside the US reads before deciding they were not
      // invited. The registry covers 15 countries and falls back to document
      // review; the sign-in screen should not disagree with the wizard.
      h('p', { class: 'asc-login-hint' }, 'For credentialed physicians working with Archangel Health. Contact your program administrator if you need access.'),
    );
    if (signup) body.insertBefore(signup, body.querySelector('.asc-login-hint'));
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
        h('h1', {}, 'Archangel Health'),
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
          h('p', {}, 'Archangel Health'),
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

  /** The dashboard's one card when the practice case is still owed.
   *
   *  Not an error state: nothing has gone wrong and there is exactly one thing
   *  to do, so it says that and offers the button. */
  function renderPracticeGateCard() {
    return h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-dash-widget-title' }, 'One practice case first'),
      h('p', { class: 'asc-help', style: 'margin:8px 0 16px' },
        'It takes about four minutes and runs on a real case where we already '
        + 'know the answer. Real cases open once you have passed it, and you '
        + 'can retake it as many times as you like.'),
      h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', type: 'button',
        onClick: () => startTutorial({}) }, 'Open the practice case'));
  }

  /** True when this error is the practice-case gate rather than a real refusal.
   *
   *  Matched on the structured `error` code, never on the message: the copy is
   *  going to get reworded and routing must not break when it does. */
  function isPracticeGate(err) {
    if (!err || err.status !== 403) return false;
    if (err.practiceGate) return true;   // the header, when the response carried it
    const d = err.detail;
    return !!(d && typeof d === 'object' && d.error === 'practice_case_required');
  }

  /** Send them to the one thing that opens their queue.
   *
   *  A gate is not an outage and must never be rendered as one, and it must
   *  never cost them work: every caller of this keeps its draft. */
  function goToPracticeCase() {
    toast('Finish the practice case to open real cases.', 'info');
    startTutorial({});
  }

  // ─── The contributor agreement ──────────────────────────────────────────────
  // The screen the fourth gate points at. It is the reason that gate may be
  // armed at all: ASCLEPIUS_AGREEMENT_GATE refuses a draw with
  // {"action": {"kind": "sign_agreement"}}, and until this existed that refusal
  // named a door nobody had built, so every physician's queue would have locked on
  // the deploy that flipped the flag, with nothing on screen to unlock it.
  //
  // Reachable two ways ON PURPOSE. The gate routes here when it is armed; the
  // Profile page links here whether or not it is, because the rollout order in
  // `physician_agreement.gate_enabled` is "ask the physicians already here to
  // sign, THEN arm it", and that first step needs a door that does not depend on
  // the flag.

  /** True when this error is the contributor-agreement gate, not a real refusal.
   *
   *  Structured `error` code and the header, never the message: same discipline
   *  as isPracticeGate, and for the same reason. */
  function isAgreementGate(err) {
    if (!err || err.status !== 403) return false;
    if (err.agreementGate) return true;  // the header, when the response carried it
    const d = err.detail;
    return !!(d && typeof d === 'object' && d.error === 'agreement_required');
  }

  /** The dashboard's one card when the terms in force have not been signed.
   *
   *  A card with the action on it, not an error: nothing has gone wrong, the
   *  physician has done nothing wrong, and there is exactly one thing to do. */
  function renderAgreementGateCard(reason) {
    return h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-dash-widget-title' },
        reason === 'superseded' ? 'The contributor agreement has changed'
          : 'Read and sign the contributor agreement'),
      h('p', { class: 'asc-help', style: 'margin:8px 0 16px' },
        reason === 'superseded'
          ? 'The terms were updated since you last signed. Read the new version '
            + 'and sign it to open your queue again. Anything you have already '
            + 'submitted is unaffected.'
          : 'It is one page and it takes a minute. Your queue opens as soon as '
            + 'it is signed.'),
      h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', type: 'button',
        onClick: () => renderAgreementView({}) }, 'Read the agreement'));
  }

  /** Read the agreement and, when one is owed, sign it.
   *
   *  The FULL TEXT is on screen, from GET /me/agreement, because a clickwrap
   *  that makes you download something to read it is a clickwrap whose "I have
   *  read this" is provably false. The endpoint serves the text for exactly
   *  that reason and this renders what it serves.
   *
   *  `doc_sha256` is echoed back on signature so the signature can only be taken
   *  against the document that was on this screen. A deploy landing mid-read
   *  answers 409, which is why every 409 here re-renders: whether the text moved
   *  underneath them or they had already signed, the correct next screen is this
   *  one, freshly loaded. */
  async function renderAgreementView(opts) {
    opts = opts || {};
    const back = opts.onBack || renderDashboardView;
    stopTimer();
    updateHeaderProgress();
    const body = h('div', {});
    setRoot(h('div', { class: 'asc-wrap' }, body));
    body.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }),
        'Loading the agreement…')));
    let doc;
    try {
      doc = await api('/me/agreement');
    } catch (e) {
      if (e.status === 401) return;  // handleUnauthorized already took the screen
      clear(body);
      body.appendChild(h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          'The agreement could not be loaded: ' + e.message),
        h('div', { style: 'margin-top:16px' },
          h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
            onClick: () => renderAgreementView(opts) }, 'Try again'),
          h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm',
            type: 'button', style: 'margin-left:8px',
            onClick: () => back() }, 'Back'))));
      return;
    }
    clear(body);
    // The server's own answer about whether a signature is owed, never
    // re-derived here: `/me/agreement` and the gate that enforces it must not be
    // able to reach two different conclusions from the same row.
    const required = doc.signature_required || null;
    const signed = doc.signed || null;

    const card = h('div', { class: 'asc-card' });
    card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
      h('div', { class: 'asc-card-title' }, 'Physician contributor agreement'),
      h('div', { class: 'asc-card-sub' },
        'Version ' + (doc.doc_version || '') + (doc.interim ? ' · interim text' : '')))));
    // Scrolls inside its own box rather than the page, so the signature controls
    // below it are never pushed off the bottom of a laptop screen.
    card.appendChild(h('div', { class: 'asc-card-pad' },
      h('div', {
        style: 'max-height:420px; overflow-y:auto; white-space:pre-wrap; '
          + 'font-size:13px; line-height:1.65; padding-right:8px',
        tabindex: '0', role: 'region', 'aria-label': 'Agreement text',
      }, doc.text || '')));
    body.appendChild(card);

    if (!required) {
      // Nothing is owed. Say when it was signed and get out of the way: this
      // screen is also the place somebody comes to re-read what they agreed to.
      body.appendChild(h('div', { class: 'asc-card asc-card-pad', style: 'margin-top:16px' },
        h('div', { class: 'asc-dash-widget-title' }, 'You have signed this agreement'),
        h('p', { class: 'asc-help', style: 'margin:8px 0 16px' },
          signed
            ? 'Signed as ' + (signed.typed_name || '') + ' on '
              + fmtDate(signed.signed_at) + ' · version ' + (signed.doc_version || '')
            : 'Your signature is on file.'),
        h('button', { class: 'asc-btn asc-btn-ghost', type: 'button',
          onClick: () => back() }, 'Back')));
      return;
    }

    const nameInput = h('input', { class: 'asc-input', type: 'text', id: 'ascAgreementName',
      placeholder: 'Type your full name', autocomplete: 'name',
      value: doc.signer_name_prefill || '' });
    const initialsInput = h('input', { class: 'asc-input', type: 'text',
      id: 'ascAgreementInitials',
      placeholder: 'Initials', maxlength: '8', style: 'max-width:140px' });
    const consent = h('input', { type: 'checkbox', id: 'ascAgreementConsent' });
    const errBox = h('div', { class: 'asc-inline-error', hidden: true });
    const signBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg',
      type: 'submit' }, 'Sign and continue');

    const form = h('form', {
      onSubmit: async (e) => {
        e.preventDefault();
        errBox.setAttribute('hidden', '');
        // Checked here as well as on the server so the common mistakes are
        // answered without a round trip. The server still refuses each of them:
        // this is a courtesy, not the rule.
        if (!consent.checked) {
          errBox.textContent = 'Confirm you agree to sign electronically before you sign.';
          errBox.removeAttribute('hidden');
          return;
        }
        signBtn.setAttribute('disabled', '');
        signBtn.textContent = 'Signing…';
        try {
          await api('/me/agreement/sign', { method: 'POST', body: {
            typed_name: nameInput.value,
            signed_initials: initialsInput.value,
            consent_esign: true,
            // What was ON THIS SCREEN. The server compares it and refuses a
            // signature against text nobody read.
            doc_sha256: doc.doc_sha256 || '',
          } });
          toast('Signed. Your copy is on file.', 'success');
          (opts.onSigned || back)();
          return;
        } catch (err) {
          if (err.status === 401) return;
          if (err.status === 409) {
            // Either the text moved underneath them or they had already signed
            // this version. Both are answered by the same screen, reloaded.
            toast(err.message || 'Reloading the agreement.', 'info');
            renderAgreementView(opts);
            return;
          }
          errBox.textContent = err.message || 'That could not be signed just now.';
          errBox.removeAttribute('hidden');
          signBtn.removeAttribute('disabled');
          signBtn.textContent = 'Sign and continue';
        }
      },
    },
      h('div', { class: 'asc-dash-widget-title' },
        required === 'superseded' ? 'Sign the updated agreement' : 'Sign the agreement'),
      h('p', { class: 'asc-help', style: 'margin:8px 0 16px' },
        required === 'superseded'
          ? 'The terms above were updated since you last signed. Your earlier '
            + 'signature and everything you have submitted stay exactly as they are.'
          : 'Typing your name and initials is your electronic signature. We keep '
            + 'a copy of the exact text you signed.'),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label', for: 'ascAgreementName' }, 'Full name'),
        nameInput),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label', for: 'ascAgreementInitials' }, 'Initials'),
        initialsInput),
      h('label', { class: 'asc-checkbox-row', style: 'margin-bottom:16px' },
        consent,
        h('span', {}, 'I agree to sign this electronically.')),
      errBox,
      h('div', { style: 'margin-top:16px' },
        signBtn,
        h('button', { class: 'asc-btn asc-btn-ghost', type: 'button',
          style: 'margin-left:8px', onClick: () => back() }, 'Not now')));
    body.appendChild(h('div', { class: 'asc-card asc-card-pad', style: 'margin-top:16px' },
      form));
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  EVALUATOR WORKSPACE
  // ═══════════════════════════════════════════════════════════════════════════
  async function renderEvalView() {
    // Home page: the evaluator picks their experience (V3 seamless (the
    // recommended default) / V2 assisted / V1 classic) before any labeling. Shown
    // on entry until a choice is made this session (and again on "Change experience").
    // AWAITED. ``renderVersionHome`` became async when the V5 card started
    // asking the server whether this physician has a chart walk routed to them.
    // Called without await, a throw inside it stops propagating to this
    // function's caller and becomes an unobserved promise rejection instead —
    // the physician gets a blank screen and the console gets nothing anyone is
    // watching. Awaiting restores exactly the error path it had when it was
    // synchronous.
    if (!state.portalChosen) { await renderVersionHome(); return; }
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
      // The SERVED version, not the picked one. There are a finite number of real
      // charts, so a physician who finishes them is continued onto the synthetic
      // multimodal queue — and the record has to say what the work actually was.
      // The server refuses a v4 claim on a synthetic task outright (a mislabel is
      // a 400, never a silent normalise), so stamping from the picker here would
      // hand the doctor a task their own submission would be rejected for.
      state.servedVersion = data.served_portal_version || null;
      state.continuedFrom = data.continued_from || null;
      // Reset first, then hydrate: a stale walk count carried over from the
      // previous case would put "Decision 4 of 13" on an unrelated chart, which
      // is worse than no banner at all.
      state.trajectoryProgress = null;
      if (state.task.trajectory_id) {
        try {
          const walk = await api('/trajectories/' + encodeURIComponent(state.task.trajectory_id));
          state.trajectoryProgress = walk.progress || null;
        } catch (e) { /* the banner degrades to "Longitudinal case" */ }
      }
      initDraftForTask(state.task);
      // Resuming straight into the compare stage (e.g. mid-task refresh) needs the
      // withheld answer texts loaded before they're rendered.
      if (state.draft.stage === 'compare') {
        try { await loadWithheldAnswersIfNeeded(); } catch (e) { /* compare shows a reload hint */ }
      }
      renderTaskWorkspace();
    } catch (e) {
      // Welcome package v2 §5: the required stops are enforced on the SERVER, and
      // its refusal names the one thing left to do. Send them there rather than
      // rendering "Could not load the next task", which is a dead end for a
      // physician who has done nothing wrong — the same reason the practice
      // gate's 403 renders a card with an action instead of an error string.
      if (e.status === 409 && e.detail && e.detail.error === 'first_run_incomplete') {
        resumeFirstRun();
        return;
      }
      // Same rule for the agreement gate, one gate later: the refusal names the
      // one thing left to do, so go there. Rendering "Could not load the next
      // task" instead would be an outage message for a queue that is working,
      // and it is the screen every physician would have hit on the deploy that
      // armed the flag.
      if (isAgreementGate(e)) {
        renderAgreementView({ onSigned: renderEvalView });
        return;
      }
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
    // An advisor sees a persistent notice instead: same slot, different fact.
    // Theirs is not "wait a day", it is "this is what the product looks like,
    // and you are reading it rather than working in it".
    if (isAdvisor()) {
      return h('div', { class: 'asc-provisional-banner', role: 'status' },
        h('span', { class: 'asc-provisional-dot', 'aria-hidden': 'true' }),
        h('div', { class: 'asc-provisional-copy' },
          h('strong', {}, 'You have view-only access.'),
          ' Look around anything here: the community, the guide, a practice '
          + 'case with the real interface. Nothing you do is saved and no real '
          + 'patient case is ever shown. Your referral page is the one part '
          + 'that is fully yours to use.'));
    }
    if (!sessionIsProvisional()) return null;
    return h('div', { class: 'asc-provisional-banner', role: 'status' },
      h('span', { class: 'asc-provisional-dot', 'aria-hidden': 'true' }),
      h('div', { class: 'asc-provisional-copy' },
        h('strong', {}, 'We are verifying your credentials.'),
        ' Usually one to two business days. Real cases open as soon as we '
        + 'are done.'),
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
      // No "Open to you now" list. The rail already answers that question by
      // being a rail: what is open is not locked. This was the third statement
      // of it on one screen, after the banner's own clause and the manual rows
      // above.
      ));
    setRoot(h('div', { class: 'asc-wrap' }, card));
  }

  // ─── Dashboard (home) ───────────────────────────────────────────────────────
  // The landing surface after login: shows the cases this reviewer can pick right
  // now, a "start next case" CTA, or a reassuring empty state. It routes into the
  // EXISTING case flow (renderEvalView / the task workspace) without changing it.
  /** Where an applicant is in the credentialing path.
   *
   *  Read off the session's tutorial blob, which the server projects with the
   *  score and the pass flag stripped out: this decides which BUTTON to show,
   *  and it must never be able to tell a physician how they did. That is the
   *  admin's call and nobody else's.
   */
  function credentialingStage() {
    const t = (state.user && state.user.tutorial) || {};
    const exam = t.exam || {};
    if (exam.state === 'submitted') return 'exam_submitted';
    if (exam.state === 'in_progress') return 'exam_in_progress';
    if (t.resources_seen_at) return 'exam_ready';
    if (t.status === 'in_progress') return 'practice_in_progress';
    return 'resources';
  }

  /** The one action on the credentialing dashboard.
   *
   *  For now it opens the practice case, which is the piece that exists. The
   *  resources screen and the examination land on this same seam, so the
   *  dashboard does not have to change again when they do.
   */
  function startCredentialing() {
    // Never `replay`. Replay clears the saved draft, and this button is the
    // one a physician presses to CONTINUE a practice case they left part way
    // through, which is exactly the work that must survive.
    startTutorial({ replay: false });
  }

  /** What an applicant sees where the case queue will be.
   *
   *  Before this they got the approved physician's dashboard with most of it
   *  crossed out: a specialty header for cases they cannot draw, a review card,
   *  queue errors from a /tasks/next call that is a guaranteed 403 for them,
   *  and a "Meet the community" button that opened a tab and 403'd there too.
   *
   *  Three things now, and nothing else. Where their application stands, what
   *  is being asked of them, and the one button that does it.
   */
  function renderCredentialingDashboard() {
    const stage = credentialingStage();
    const cta = {
      resources: ['Start', 'Two short things before your examination.'],
      practice_in_progress: ['Continue practice case', 'You left one part way through. It is where you left it.'],
      exam_ready: ['Take my examination', 'The practice case is done. This is the one we read.'],
      exam_in_progress: ['Resume my examination', 'Your answers are saved.'],
      exam_submitted: ['Your examination is with us', 'Nothing more to do. We will email you either way.'],
    }[stage] || ['Start', ''];

    setRoot(h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-card asc-card-pad asc-credentialing' },
        h('div', { class: 'chrome' }, 'YOUR APPLICATION'),
        h('h2', {}, 'We are checking your credentials.'),
        h('p', { class: 'asc-dim' },
          'Usually one to two business days. You do not need to do anything for '
          + 'that part, and we will email you either way.'),
        // Said plainly and in one place, because "what is actually being asked
        // of me" was the question the old screen never answered.
        h('p', {},
          'Before we can verify you there are two pieces of case work here: a '
          + 'practice case, and one examination case in the real interface. The '
          + 'practice case is optional and it is there to help. The examination '
          + 'is the one we read.'),
        h('button', {
          class: 'asc-btn asc-btn-primary',
          disabled: stage === 'exam_submitted' ? 'disabled' : null,
          onClick: () => { if (stage !== 'exam_submitted') startCredentialing(); },
        }, cta[0]),
        cta[1] ? h('p', { class: 'asc-dim asc-small' }, cta[1]) : null)));
  }

  async function renderDashboardView() {
    state.view = 'home';
    stopTimer();
    updateHeaderProgress();
    renderHeader();
    // Before the queue fetch, which for an applicant is a guaranteed 403 and
    // used to paint a "could not load your queue" error onto the one screen
    // that is supposed to be telling them their application is fine.
    if (sessionIsProvisional() && !isAdvisor()) {
      renderSidePanel();
      renderCredentialingDashboard();
      return;
    }
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
    let queueError = null;
    let practiceGate = null;
    let agreementGate = null;
    // No real queue and no earnings, and BOTH endpoints below are on the
    // real-work surface. Asking anyway would produce two 403s and render "we
    // could not load your queue", which is a bug report, not the truth.
    //
    // Keyed on the SURFACE rather than on `sessionIsProvisional()`, which is
    // what it used to read. Those were the same set while a physician under
    // review was the only person who could be in here without real work; an
    // advisor is the second, and they are not provisional -- nothing about
    // their account is pending -- so the old test would have sent them straight
    // into two 403s on their first screen.
    const noRealWork = !sessionHasSurface('real_work');
    const provisional = sessionIsProvisional();
    // The score is browse-gated on purpose: a provisional physician's "in
    // review" state renders FROM it, so it is fetched outside the real-work
    // try below and never blocks anything.
    // One Tasks surface for every kind of work: a reviewer's queue arrives
    // here as a distinct card instead of a separate nav tab. The card is the
    // console's route now, so it renders for every reviewer, count or no
    // count, and the stats fetch is best-effort.
    const reviewPromise = sessionCan('review')
      ? api('/review/stats').catch(() => null)
      : Promise.resolve(null);
    try {
      if (noRealWork) throw { __provisional: true };
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
      // A gate is not an outage. renderDashboardError says "your queue could
      // not be loaded, so nothing below is a real number", which is true of a
      // 500 and a lie about a physician who simply has not done the practice
      // case yet.
      //
      // A CARD, not a relaunch. Auto-starting the tour from here would make
      // "Leave for now" impossible: leaving renders the dashboard, the
      // dashboard 403s, and the physician is put straight back into the tour
      // they just left. boot() is the one place that opens it unasked.
      //
      // The agreement gate reaches here as a 403 of its own shape, and it is
      // separated out FIRST: rendering "one practice case first" at a physician
      // who has passed the practice case and owes a signature is a dead end,
      // because the button on that card cannot clear the thing blocking them.
      //
      // And the practice gate is claimed by the SIGNAL, never by elimination.
      // `else practiceGate = e` sent every remaining failure to that card, so a
      // 500, a timeout or a dropped connection told an approved physician who
      // passed the practice case weeks ago that their real cases were locked
      // behind a practice case: a false explanation, with a button that cannot
      // fix anything, for an outage on our side. isPracticeGate reads the
      // PRACTICE_GATE_HEADER and the structured `practice_case_required` code,
      // which is the same test every other caller of that card uses; anything
      // else is an error and renders as one.
      if (isAgreementGate(e)) agreementGate = e;
      else if (isPracticeGate(e)) practiceGate = e;
      else queueError = e;
      }
    }
    const tasks = data.tasks || [];
    const reviewStats = await reviewPromise;

    const wrap = h('div', { class: 'asc-wrap' });
    const banner = provisionalBannerEl();
    if (banner) wrap.appendChild(banner);
    // §6 re-entry: a quiet chip, never a modal ambush. It reports and it waits;
    // ignoring it costs nothing, and it disappears the moment the last stop
    // closes or the physician dismisses the checklist on the finish card.
    // §4.3: on the dashboard, from login 4 onwards, the chip is replaced by the
    // banner — one door, not two, and the banner is the one that can carry the
    // progress. On every other screen, and in every earlier mode, the chip is
    // still the chip.
    const frEntry = firstRunMode() === 'banner' ? firstRunBannerEl() : firstRunChipEl();
    if (frEntry) wrap.appendChild(frEntry);
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

    if (agreementGate) {
      main.appendChild(renderAgreementGateCard(
        (agreementGate.detail && agreementGate.detail.reason) || agreementGate.agreementGate));
    } else if (practiceGate) {
      main.appendChild(renderPracticeGateCard());
    } else if (queueError) {
      main.appendChild(renderDashboardError(queueError));
    } else if (noRealWork) {
      main.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('h3', {}, provisional
          ? 'Your first real case is waiting on us, not on you.'
          : 'This is where the work happens.'),
        h('p', {}, provisional
          ? 'Real ' + specLabel + ' cases appear here the moment your credentials '
            + 'clear. In the meantime the practice case is the real interface with '
            + 'a case we wrote for it, so nothing about it is a mock-up.'
          : 'A verified physician sees their queue of real ' + specLabel + ' cases '
            + 'here. The practice case below is the same interface they work in, '
            + 'running against a case we wrote for it, so what you see is what '
            + 'they see.'),
        h('div', { class: 'asc-dash-cta' },
          h('button', {
            class: 'asc-btn asc-btn-primary',
            onClick: () => startTutorial({ replay: true }),
          }, 'Open the practice case'),
          h('button', {
            class: 'asc-btn asc-btn-ghost',
            onClick: () => setPanel('community'),
          }, provisional ? 'Meet the community' : 'Read the community')))));
    } else if (!tasks.length) {
      main.appendChild(renderDashboardEmpty(specLabel));
    } else {
      // Name the queue these cases came from. The count alone is ambiguous the
      // moment V4 continues onto V3: a physician cleared for real patient data
      // would read "18 cases available", start one, and only then discover from
      // the badge inside the case that it was synthetic. The server tells us
      // which queue answered; say it here, before the click.
      const servedVer = data.served_portal_version || ver;
      const QUEUE_LABEL = { v4: 'Real de-identified cases', v3: 'Synthetic multimodal cases',
                            v2: 'Assisted evaluation', v1: 'Classic evaluation' };
      const queueLine = QUEUE_LABEL[servedVer] || null;
      const continuedFromV4 = data.continued_from === 'v4' && servedVer !== 'v4';
      // §4.1 — ONE button. Routing decides which case a physician sees, so a
      // queue of cases to choose between is a decision we are supposed to be
      // making for them; and the case COUNT is queue depth, which is exactly
      // the information this change exists to hide.
      //
      // A draft in localStorage means work in progress: Continue takes them
      // back to where they stopped, Start hands them the next case routing
      // chose. Same button, two honest labels.
      const resumable = findResumableDraft(tasks);
      main.appendChild(h('div', { class: 'asc-dash-hero' },
        h('div', { class: 'asc-dash-hero-main' },
          h('span', { class: 'asc-dash-hero-icon', 'aria-hidden': 'true' }, '→'),
          h('div', {},
            h('div', { class: 'asc-dash-hero-title' },
              resumable ? 'Continue case' : 'Start new case'),
            h('div', { class: 'asc-dash-hero-sub' },
              resumable
                ? 'Picks up where you left off · ' + formatTime(resumable.elapsedSec || 0) + ' so far'
                : (queueLine || 'Real de-identified cases')),
            continuedFromV4
              ? h('div', { class: 'asc-dash-hero-note' },
                  'You have completed every real de-identified case available to you '
                  + 'right now. New ones appear here as charts are promoted.')
              : null)),
        h('button', {
          class: 'asc-btn asc-btn-primary asc-btn-lg',
          onClick: () => {
            state.view = 'eval';
            renderHeader();
            if (resumable) openTaskById(resumable.task_id);
            else renderEvalView();
          },
        }, resumable ? 'Continue →' : 'Start →')));
    }
    cols.appendChild(main);
    const side = h('div', { class: 'asc-dash-side' });
    // No rating widget. A physician's contributor score is an internal
    // instrument for routing and pay, and putting it on their dashboard turned
    // it into a number they were managing rather than a measurement of the
    // work. It is still computed, and the admin still reads it.
    side.appendChild(renderDashboardWidget(stats));
    cols.appendChild(side);
    wrap.appendChild(cols);
    setRoot(wrap);
  }

  /** §4.3 — the quiet dashboard banner. Login 4 onwards, optional stops left.
   *
   *  Six dots rather than a bar: it reads at a glance and there is no percentage
   *  to do arithmetic on. Tabular figures on the count so the digits do not
   *  jitter as it climbs. It is 56px tall, it is NOT a modal, and it is not
   *  dismissible — it goes away by being finished. A physician who never wants
   *  it will never notice it; one who does has a door.
   *
   *  Its button opens the re-entry page, so the banner and the page are the same
   *  flow at different volumes rather than two competing onboarding surfaces. */
  function firstRunBannerEl() {
    const p = window.FirstRunWalkthrough.progress(state.user);
    const remaining = window.FirstRunWalkthrough.remaining(state.user);
    const dots = h('span', { class: 'asc-fr-banner-dots', 'aria-hidden': 'true' });
    for (let i = 0; i < p.total; i += 1) {
      dots.appendChild(h('span', {
        class: 'asc-fr-banner-dot' + (i < p.done ? ' is-done' : ''),
      }));
    }
    return h('div', {
      class: 'asc-fr-banner',
      // A region, not an alert: it must never steal focus or interrupt a screen
      // reader mid-sentence. It is the "quiet" §3 asks for.
      role: 'region', 'aria-label': 'Onboarding progress',
    },
      h('div', { class: 'asc-fr-banner-main' },
        dots,
        h('span', { class: 'asc-fr-banner-count' },
          'Onboarding · ' + p.done + ' of ' + p.total),
        h('span', { class: 'asc-fr-banner-rest' },
          remaining.length ? remaining.join(' · ') + ' remaining' : '')),
      h('button', {
        class: 'asc-btn asc-btn-primary', type: 'button',
        onClick: () => openFirstRunReentry(),
      }, 'Finish onboarding'));
  }

  /** "Finish setup · 3 of 6", or null when there is nothing left to finish. */
  function firstRunChipEl() {
    if (!firstRunPending()) return null;
    const p = window.FirstRunWalkthrough.progress(state.user);
    return h('button', {
      class: 'asc-fr-chip', type: 'button',
      onClick: () => resumeFirstRun(),
    },
      h('span', {}, 'Finish setup'),
      h('span', { class: 'asc-fr-chip-count' }, p.done + ' of ' + p.total));
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
        h('span', { class: 'asc-dash-widget-n' }, total == null ? '-' : String(total))),
      h('div', { class: 'asc-dash-widget-row' },
        h('span', { class: 'asc-dash-widget-label' }, 'This week'),
        h('span', { class: 'asc-dash-widget-n asc-dash-widget-n-sm' }, week == null ? '-' : String(week))),
      h('div', { class: 'asc-dash-widget-row asc-dash-widget-row-last' },
        h('span', { class: 'asc-dash-widget-label' }, 'Last submission'),
        h('span', { class: 'asc-dash-widget-meta' }, formatRelativeTime(lastAt))));
  }

  async function openTaskById(id) {
    setRoot(h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), 'Opening case…'))));
    try {
      const data = await api('/tasks/' + encodeURIComponent(id));
      // A 200 carrying no task is the same fact as a 404 — the server has
      // nothing under this id — so it gets the same treatment. Returning to the
      // dashboard while leaving the draft in place would loop the physician
      // between Continue and the dashboard with no error to explain it, which is
      // worse than the stranded loading card: at least that one looks broken.
      if (!data || !data.task) {
        clearDraft(id);
        toast('That case is no longer available.', 'error');
        renderDashboardView();
        return;
      }
      state.view = 'eval';
      state.portalChosen = true;
      state.specialtyChosen = true;
      state.task = data.task;
      // Opening a card skips /tasks/next, so the served version has to come from
      // THIS response or the draft gets stamped from the picker instead of from
      // the case. A v4 picker on a synthetic card would then build a draft whose
      // own submission the server rejects with a 400.
      state.servedVersion = data.served_portal_version || null;
      state.continuedFrom = null;
      // Trajectory progress for the banner. Best-effort and non-blocking: the
      // case must open whether or not the walk metadata resolves, because the
      // banner is context and the case is the work.
      state.trajectoryProgress = null;
      if (state.task.trajectory_id) {
        try {
          const walk = await api('/trajectories/' + encodeURIComponent(state.task.trajectory_id));
          state.trajectoryProgress = walk.progress || null;
        } catch (e) { /* the banner degrades to "Longitudinal case" */ }
      }
      renderHeader();
      initDraftForTask(state.task);
      if (state.draft.stage === 'compare') {
        try { await loadWithheldAnswersIfNeeded(); } catch (e) { /* compare shows a reload hint */ }
      }
      renderTaskWorkspace();
    } catch (e) {
      // 409 trajectory_out_of_order (PRD-2 §9.1): the physician is entitled to
      // this case, just not yet — its history contains the outcomes of decisions
      // they have not made. Say that, and hand them the one they may open, rather
      // than reporting a generic failure for a rule that is working correctly.
      // Handled ahead of the terminal-status branch below deliberately: a 409 here
      // is the queue working, so the draft must SURVIVE it. Falling through would
      // not clear it (409 is not in the terminal list) but would strand the
      // physician on the dashboard with no explanation of a refusal that has a
      // precise remedy — open the earlier point, which this does for them.
      if (e.status === 409 && e.detail && e.detail.error === 'trajectory_out_of_order') {
        toast(e.detail.message || 'Answer the earlier decisions in this chart first.', 'error');
        if (e.detail.next_task_id) { openTaskById(e.detail.next_task_id); return; }
        renderDashboardView();
        return;
      }
      // 401 already took the screen (handleUnauthorized renders the login form).
      if (e.status === 401) return;
      // Everything else used to leave the physician looking at "Opening case…"
      // forever: the loading card had already replaced the whole root, so a
      // toast with nothing behind it is a dead end. With §4 this is the
      // dashboard's only button, so it has to land somewhere.
      //
      // Gone (404), refused (403) or withdrawn (410) is TERMINAL for this case:
      // drop its draft, or the dashboard reads it straight back and offers the
      // same Continue that just failed — an inescapable loop on the one control
      // the physician has. "Prefer a clean Start over a broken resume" (§4.1)
      // is exactly this rule, applied one step later.
      //
      // Any other failure (5xx, offline, a client-side bug) may well be
      // transient, so the draft is KEPT and only the screen is restored: a
      // flaky network must never cost somebody an hour of clinical reasoning.
      // ...EXCEPT the practice-case gate, which is not an answer about this
      // case at all. It is "not yet, and here is the thing to do first", and
      // treating it as terminal would delete a draft the physician will come
      // straight back to once the gate opens.
      if (isPracticeGate(e)) { goToPracticeCase(); return; }
      if (e.status === 403 || e.status === 404 || e.status === 410) clearDraft(id);
      toast('Could not open that case: ' + e.message, 'error');
      renderDashboardView();
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
      prompt_review: { reviewed: false, verdict: null, note: '', reviewed_at: null, attest_clinically_valid: null },
      independent_answer: { text: '', evidence_anchor: emptyAnchor(), captured_at: null },
      verdict: null,
      chosen_id: null,
      rejected_id: null,
      chosen_revision: { edited: false, revised_text: null, why_better_tags: [], why_better_notes: '', evidence_anchor: emptyAnchor() },
      rejected_critique: { error_tags: [], severities: {}, why_worse: '', error_tag_anchors: {}, error_tag_reasons: {}, failure_tags: [] },
      from_scratch: { ideal_answer: '', approach_notes: '', reasoning_steps: [], evidence_anchor: emptyAnchor() },
      // Decisive action (Audit §13): physician-named verifiable outcome, skippable.
      decisive_action: { action: '', tool_name: '', rationale: '', must_precede_final_answer: true },
      // Expected trajectory (Longitudinal Cases §3.3 field 3): what should happen
      // next if this assessment is right, and what would say it is wrong. Skippable
      // — a fabricated falsifier is worth less than none, because it gets scored
      // against a real chart and the score means nothing.
      expected_trajectory: { expectations: [{ expectation: '', horizon_days: '' }], falsifiers: [''], note: '' },
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
    // ── Structural backfill, BEFORE anything reaches inside these objects ──
    // Every member backfill below dereferences one of them, so a draft missing
    // one threw a TypeError right here — and openTaskById caught it, leaving
    // the physician on "Opening case…" with a toast and no way back. §4 made
    // that reachable from the ONLY button on the dashboard, so it is no longer
    // an obscure path.
    //
    // A draft can arrive shaped like this: written before one of these fields
    // existed, or truncated by a quota failure part-way through a write. Repair
    // it in place rather than discarding it — whatever real work it holds is in
    // the fields that DID survive, and this is where every other backfill lives.
    const skeleton = newDraft(task);
    ['prompt_review', 'independent_answer', 'chosen_revision', 'rejected_critique',
      'from_scratch', 'decisive_action'].forEach((k) => {
      if (!draft[k] || typeof draft[k] !== 'object' || Array.isArray(draft[k])) {
        draft[k] = skeleton[k];
      }
    });
    ['reasoning_steps', 'rubric'].forEach((k) => {
      if (!Array.isArray(draft[k])) draft[k] = [];
    });
    // Backfill any newly-added fields for older drafts.
    if (!draft.chosen_revision.evidence_anchor) draft.chosen_revision.evidence_anchor = emptyAnchor();
    if (!draft.from_scratch.evidence_anchor) draft.from_scratch.evidence_anchor = emptyAnchor();
    if (!draft.rejected_critique.error_tag_anchors) draft.rejected_critique.error_tag_anchors = {};
    if (!draft.rejected_critique.error_tag_reasons) draft.rejected_critique.error_tag_reasons = {};
    if (!Array.isArray(draft.rejected_critique.failure_tags)) draft.rejected_critique.failure_tags = [];
    if (draft.assist === undefined) draft.assist = null;
    // Served version wins over the picker — see the note in the fetch above.
    if (!draft.portal_version) draft.portal_version = state.servedVersion || getPortalVersion();
    if (!draft.prompt_review) draft.prompt_review = { reviewed: false, verdict: null, note: '', reviewed_at: null };
    if (!draft.independent_answer) draft.independent_answer = { text: '', evidence_anchor: emptyAnchor(), captured_at: null };
    if (!draft.independent_answer.evidence_anchor) draft.independent_answer.evidence_anchor = emptyAnchor();
    if (!Array.isArray(draft.rubric)) draft.rubric = [];
    if (!draft.decisive_action) draft.decisive_action = { action: '', tool_name: '', rationale: '', must_precede_final_answer: true };
    if (!draft.expected_trajectory) draft.expected_trajectory = { expectations: [{ expectation: '', horizon_days: '' }], falsifiers: [''], note: '' };
    if (!Array.isArray(draft.expected_trajectory.expectations) || !draft.expected_trajectory.expectations.length) {
      draft.expected_trajectory.expectations = [{ expectation: '', horizon_days: '' }];
    }
    if (!Array.isArray(draft.expected_trajectory.falsifiers) || !draft.expected_trajectory.falsifiers.length) {
      draft.expected_trajectory.falsifiers = [''];
    }
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
  // Stopping FREEZES the clock: the elapsed at this instant is folded into
  // baseElapsed and timerStart is cleared. Without that fold, timerStart would
  // still hold the moment the timer last STARTED, so any later getElapsed() —
  // a beforeunload save fired from an already-hidden tab, the blur save below,
  // a re-render of the header timer — would silently add back the whole away
  // period. That is the exact overcount §5.1 exists to remove, and it is
  // submitted as time_spent_sec, so it is not cosmetic.
  function stopTimer() {
    if (state.timerInterval) { clearInterval(state.timerInterval); state.timerInterval = null; }
    if (state.timerStart != null) {
      state.baseElapsed = getElapsed();
      state.timerStart = null;
    }
  }
  function getElapsed() {
    // Stopped clock: baseElapsed IS the elapsed. Never measure against a
    // timerStart that no longer refers to a running interval.
    if (state.timerStart == null) return Math.floor(state.baseElapsed);
    return Math.floor(state.baseElapsed + (Date.now() - state.timerStart) / 1000);
  }
  function formatTime(sec) {
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + String(s).padStart(2, '0');
  }
  function saveDraft() {
    if (!state.draft) return;
    state.draft.elapsedSec = getElapsed();
    // §4.1 needs "the newest draft" to be an answerable question: findResumableDraft
    // picks between several stored drafts by this stamp.
    state.draft.savedAt = Date.now();
    try { localStorage.setItem(draftKey(state.draft.task_id), JSON.stringify(state.draft)); } catch (e) { /* ignore quota */ }
  }

  /** The newest draft whose task is still in the served queue, or null.
   *
   *  A draft for a task that has since been claimed, expired, or fell out of
   *  the queue must NOT offer a Continue that dead-ends — openTaskById would
   *  bounce them back to the dashboard. Prefer a clean Start over a broken
   *  resume, so the queue is the filter.
   *
   *  The tutorial's own draft is excluded: the practice case is replayed from
   *  the help menu, never resumed as if it were paid work. It is not in the
   *  served queue either, so this is belt and braces.
   *
   *  Known bound: `tasks` is the dashboard's own /tasks/available page, which
   *  the server caps (default 50, priority-ordered). A physician with a deeper
   *  queue than that whose in-progress task ranks below the page is offered
   *  Start rather than Continue. Nothing is lost — the draft stays on disk and
   *  resumes the moment that task is opened again — and the alternative, a
   *  second request per dashboard load to re-check one task id, is a cost every
   *  physician pays for a case very few will hit. */
  function findResumableDraft(tasks) {
    const ids = new Set((tasks || []).map((t) => t && t.task_id).filter(Boolean));
    let best = null;
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || k.indexOf(DRAFT_PREFIX) !== 0) continue;
        const id = k.slice(DRAFT_PREFIX.length);
        if (id === TUTORIAL_TASK_ID || !ids.has(id)) continue;
        let d = null;
        try { d = JSON.parse(localStorage.getItem(k) || 'null'); } catch (_) { d = null; }
        // A corrupt or foreign entry under our prefix is skipped, not fatal:
        // one unparseable key must not cost the physician a resumable case.
        if (!d || d.task_id !== id) continue;
        if (!best || (d.savedAt || 0) > (best.savedAt || 0)) best = d;
      }
    } catch (_) { return null; }
    return best;
  }
  function clearDraft(taskId) {
    try { localStorage.removeItem(draftKey(taskId)); } catch (e) { /* ignore */ }
    // …and drop the in-memory copy, or the next saveDraft() writes the key
    // straight back. After a submit, state.draft still points at the finished
    // draft until the next task replaces it, and three things fire in that
    // window: the blur save, the tab-hide save, and beforeunload. A submitted
    // case resurrected as a stored draft would then be offered as a Continue
    // (§4.1) and leave a key behind that nothing ever cleans up.
    if (state.draft && state.draft.task_id === taskId) state.draft = null;
  }

  // ─── Portal version (V1 classic · V2 assisted · V3 seamless) ────────────────
  const PORTAL_VERSIONS = ['v1', 'v2', 'v3', 'v4'];
  function portalVersionWasPicked() {
    try { return localStorage.getItem(PORTAL_VERSION_PICKED_KEY) === '1'; } catch (e) { return false; }
  }
  function getPortalVersion() {
    let v = null;
    try { v = localStorage.getItem(PORTAL_VERSION_KEY); } catch (e) { v = null; }
    const approved = !!(state.user && state.user.real_data_approved);
    const stored = PORTAL_VERSIONS.indexOf(v) !== -1 ? v : null;
    // A stored 'v4' outlives the approval that earned it. Approval can be revoked,
    // and the browser would keep asking for a queue the server now answers with a
    // hard empty — a physician staring at "no cases available" while eighteen sit
    // one flow away. Read the flag, not the leftover.
    if (stored === 'v4' && !approved) return DEFAULT_PORTAL_VERSION;
    // ═══ The pre-V4 stored default ═══
    // This is the bug an approved physician actually hit: V3 was the only
    // recommended flow for months, so every browser that ever opened the portal
    // has 'v3' sitting in localStorage. That value won over the V4 default below,
    // the client asked for the synthetic queue, and the server — correctly —
    // served synthetic multimodal cases. The doctor never saw a real chart and
    // there was nothing on screen to tell them why.
    //
    // ANY stored value with no pick marker predates V4, because the marker is
    // written by the only menu that has ever listed the real cases. So none of
    // them can have been a choice BETWEEN real and synthetic, and none of them
    // gets to outrank the real work.
    //
    // This used to rescue only 'v3', on the theory that 'v1'/'v2' were deliberate
    // departures worth honouring. That was wrong, and it cost a round: the same
    // physician turned up pinned to V2 · ASSISTED instead, with the migration
    // stepping over it for a reason that never applied — the choice, whichever
    // version it landed on, was made when V4 did not exist. A pick made from
    // today's menu writes PORTAL_VERSION_PICKED_KEY and still sticks.
    //
    // Nothing is lost by moving them: /tasks/next continues a V4 physician onto
    // the synthetic queue the moment the real cases run out, and stamps the
    // record with the version actually served.
    if (approved && !portalVersionWasPicked()) return 'v4';
    if (stored) return stored;
    // No stored choice at all. The marker can outlive the version it was written
    // beside (a cleared key, a storage write that partly failed), and falling
    // through to the synthetic default here would strand an approved physician on
    // v3 with no choice on file to justify it. Absent a choice, an approved
    // contributor belongs on the real cases.
    return approved ? 'v4' : DEFAULT_PORTAL_VERSION;
  }
  function setPortalVersion(v) {
    v = PORTAL_VERSIONS.indexOf(v) !== -1 ? v : DEFAULT_PORTAL_VERSION;
    try {
      localStorage.setItem(PORTAL_VERSION_KEY, v);
      // Only a real pick from the picker reaches here, and today's picker lists
      // the real cases. Recording that makes the migration above a one-time
      // event rather than a rule that keeps overriding the physician.
      localStorage.setItem(PORTAL_VERSION_PICKED_KEY, '1');
    } catch (e) { /* ignore quota */ }
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

  // The tabbed case panel — DELEGATED, not implemented here (PRD-1 §2.2).
  //
  // The chart itself lives in ``case_panel.js`` and is rendered by the SAME
  // component on the reviewer's surface. It used to live in this file, which is
  // why review had to invent its own — a `JSON.stringify` inside a collapsed
  // <details>. A reviewer and a labeler disagreeing because they read
  // differently-rendered versions of one chart is a data-integrity bug, so the
  // panel is shared rather than forked.
  //
  // Active-tab memory moved into the module with it, keyed by task id.
  function casePanelCtx() {
    return { h, clear, fetchAssetBlobUrl };
  }
  function renderCasePanel() {
    const c = multimodalCase();
    if (!c) return null;
    const mod = window.AsclepiusCasePanel;
    if (!mod || typeof mod.render !== 'function') {
      // A missing module is a VISIBLE failure. A physician must never grade a
      // multimodal case whose chart silently did not render — the case panel is
      // the evidence, and its absence looks identical to a text-only task.
      return h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          'The clinical chart failed to load: refresh the page. Do not grade '
          + 'this case from the question alone.')));
    }
    return mod.render(casePanelCtx(), {
      case: c,
      specialty: caseSpecialty(),
      specialties: state.specialties,
      tabKey: (state.task && state.task.task_id) || '',
    });
  }

  // A physician mid-walk is looking at the SAME PATIENT they saw a moment ago, and
  // saying so is not decoration: it is why the case does not read as a fourth
  // unrelated chart, and it is what makes "context loads once, amortised over
  // several decisions" (§5) true rather than merely claimed.
  //
  // Rendered from ``state.trajectoryProgress``, hydrated by ``openTaskById`` on the
  // way in. Absent on every ordinary case, so V1–V4 render byte-for-byte as before.
  function renderTrajectoryBanner() {
    const task = state.task;
    if (!task || !task.trajectory_id) return null;
    const prog = state.trajectoryProgress;
    const step = (task.sequence_index == null) ? null : (task.sequence_index + 1);
    const of = prog && prog.n_points ? (' of ' + prog.n_points) : '';
    return h('div', { class: 'asc-meta-row', style: 'margin-top:6px' },
      h('span', { class: 'asc-badge asc-badge-accent' },
        step ? ('Decision ' + step + of) : 'Longitudinal case'),
      h('span', { class: 'asc-case-note-meta' },
        'One patient, in order. You are seeing this chart as it stood at this '
        + 'moment; what happened afterwards is sealed until you submit.'));
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  LONGITUDINAL: the reveal and the self-score (Longitudinal Cases §4, Phase 4)
  // ═══════════════════════════════════════════════════════════════════════════
  // Nothing else in this product tells a physician whether their judgment held.
  // This does — from the chart, not from a reviewer's opinion — and it is the
  // single most requested thing in expert annotation work.
  //
  // It renders only AFTER the submission is committed. The seal is not a UI rule
  // here: the server refuses this endpoint without a stored submission (409
  // commitment_required), so a client bug cannot open the future early.

  const SELF_SCORE_CHOICES = [
    ['held', 'Held', 'the record shows what you expected'],
    ['did_not_hold', 'Did not hold', 'the record shows otherwise'],
    ['not_assessable', 'Not assessable', 'this encounter does not say either way'],
  ];

  async function renderTrajectoryOutcomeView(task) {
    state.view = 'trajectory_outcome';
    stopTimer();
    renderHeader();
    setRoot(h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }),
          'Opening what happened next…'))));
    let data;
    try {
      data = await api('/tasks/' + encodeURIComponent(task.task_id) + '/trajectory-outcome');
    } catch (e) {
      if (e.status === 401) return;
      // A VISIBLE failure, and the work is safe: the submission is already
      // committed server-side. Never a silent fall-through to the next case,
      // which would look like the reveal simply does not exist.
      toast('Your answer is saved. The next encounter could not be loaded: '
        + (e.message || 'unknown error'), 'error');
      renderEvalView();
      return;
    }
    paintTrajectoryOutcome(task, data);
  }

  function paintTrajectoryOutcome(task, data) {
    const outcome = data.outcome;
    const expected = (data.expected_trajectory || {}).expectations || [];
    const falsifiers = (data.expected_trajectory || {}).falsifiers || [];
    const step = (data.sequence_index == null) ? null : (data.sequence_index + 1);

    const head = h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-substage-head' },
        h('div', { class: 'asc-substage-step' }, step ? ('Step ' + step) : 'Outcome'),
        h('div', { class: 'asc-substage-title' }, 'What happened next')),
      h('div', { class: 'asc-help' },
        outcome
          ? 'This is the same patient’s record, ' + outcome.days_after_decision
            + ' day' + (outcome.days_after_decision === 1 ? '' : 's')
            + ' after the moment you decided. You could not see it when you answered.'
          : (data.reason || 'There is no later encounter in this record.')));

    const wrap = h('div', { class: 'asc-wrap' }, head);
    if (outcome) wrap.appendChild(renderOutcomePanel(outcome));

    if (expected.length) {
      wrap.appendChild(renderSelfScoreCard(task, data, expected, falsifiers));
    } else {
      // No prediction was recorded, so there is nothing to grade. Say that
      // plainly rather than showing an empty scoring card.
      wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'asc-help' },
          'You did not record an expected trajectory on this case, so there is '
          + 'nothing here to check against the record.'),
        h('div', { style: 'margin-top:16px' }, trajectoryContinueButton(data))));
    }
    // §6, in front of the physician at the moment they grade — not only in the
    // data dictionary a buyer reads.
    if ((data.limitations || []).length) {
      wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'asc-substage-title' }, 'What this check can and cannot show'),
        h('ul', { class: 'asc-case-list', style: 'margin-top:10px' },
          ...data.limitations.map((l) => h('li', {}, l.detail)))));
    }
    setRoot(wrap);
  }

  function renderOutcomePanel(outcome) {
    const parts = [];
    if ((outcome.lab_panels || []).length) {
      parts.push(h('div', { class: 'asc-case-body' }, renderLabsTrend(outcome.lab_panels)));
    }
    (outcome.notes || []).forEach((n) => {
      parts.push(h('div', { class: 'asc-case-note' },
        h('div', { class: 'asc-case-note-meta' },
          '[' + (n.note_type || 'Note') + ' · ' + (n.author_role || 'clinician')
          + ' · day +' + n.collected_offset_days + ']'),
        h('div', { class: 'asc-case-note-text' }, (n.text || '').trim())));
    });
    (outcome.studies || []).forEach((st) => {
      // ``study_findings_policy`` is computed per truncation and legitimately
      // varies across one walk (§9.5): a window with no imaging is 'visible', a
      // later one carrying an image asset is 'hidden'. Honoured here rather than
      // assumed, so the reveal never prints findings the case is holding back.
      const showFindings = (outcome.study_findings_policy || 'visible') === 'visible';
      parts.push(h('div', { class: 'asc-case-note' },
        h('div', { class: 'asc-case-note-meta' },
          '[' + (st.label || st.modality || 'Study') + ' · day +' + st.collected_offset_days + ']'),
        showFindings && st.findings
          ? h('div', { class: 'asc-case-note-text' }, st.findings)
          : h('div', { class: 'asc-case-note-text' }, 'Findings withheld for this window.')));
    });
    if ((outcome.medications || []).length) {
      parts.push(h('div', { class: 'asc-case-body' },
        h('div', { class: 'asc-case-sub' }, 'Started after your decision'),
        h('ul', { class: 'asc-case-list' }, ...outcome.medications.map((m) => h('li', {},
          [m.drug, m.dose, m.route, m.freq].filter(Boolean).join(' ')
          + ' (day +' + m.collected_offset_days + ')')))));
    }
    if ((outcome.problem_list || []).length) {
      parts.push(h('div', { class: 'asc-case-body' },
        h('div', { class: 'asc-case-sub' }, 'Added to the problem list'),
        h('ul', { class: 'asc-case-list' }, ...outcome.problem_list.map((pr) => h('li', {},
          pr.condition + ' (day +' + pr.collected_offset_days + ')')))));
    }
    if (!parts.length) {
      // A window with nothing in it is a real answer about the record — an
      // encounter can genuinely add nothing this physician's expectations touch —
      // and it must not render as a broken panel.
      parts.push(h('div', { class: 'asc-help' },
        'The record adds nothing between your decision and the next one.'));
    }
    return h('div', { class: 'asc-card asc-case-card' },
      h('div', { class: 'asc-case-head' },
        h('span', { class: 'asc-badge asc-badge-accent' }, 'The record, after your decision')),
      h('div', { class: 'asc-case-host' }, ...parts));
  }

  function renderSelfScoreCard(task, data, expected, falsifiers) {
    // The physician's own falsifier is the rubric. No reviewer grades this.
    const marks = expected.map((_, i) => ({ index: i, state: null, note: '' }));
    let falsifierFired = false;
    const rows = h('div', {});

    expected.forEach((exp, i) => {
      // The confidence pills' styling, reused rather than re-invented: a class
      // with no CSS behind it renders as an unstyled button and is invisible to
      // every source assertion, which is the exact defect class the rendered-
      // appearance gate exists to catch.
      const pills = h('div', { class: 'asc-conf-pills', style: 'margin-top:8px' });
      SELF_SCORE_CHOICES.forEach(([key, label, why]) => {
        const btn = h('button', { class: 'asc-conf-pill', type: 'button', title: why }, label);
        btn.addEventListener('click', () => {
          marks[i].state = key;
          Array.prototype.forEach.call(pills.children, (b) => b.classList.remove('active'));
          btn.classList.add('active');
          refresh();
        });
        pills.appendChild(btn);
      });
      const note = h('input', { class: 'asc-input', style: 'margin-top:8px',
        placeholder: 'What in the record shows that? (optional)' });
      note.addEventListener('input', () => { marks[i].note = note.value; });
      rows.appendChild(h('div', { class: 'asc-field', style: i ? 'margin-top:18px' : '' },
        h('div', { class: 'asc-prompt-text' },
          exp.expectation
          + (exp.horizon_days ? ' (within ' + exp.horizon_days + ' days)' : '')),
        pills, note));
    });

    let falsifierBlock = null;
    if (falsifiers.length) {
      const box = h('input', { type: 'checkbox', id: 'ascFalsifierFired' });
      box.addEventListener('change', () => { falsifierFired = box.checked; });
      falsifierBlock = h('div', { class: 'asc-field', style: 'margin-top:22px' },
        h('label', { class: 'asc-label' }, 'You said you would be wrong if:'),
        h('ul', { class: 'asc-case-list' }, ...falsifiers.map((f) => h('li', {}, f))),
        h('label', { class: 'asc-submit-row', style: 'margin-top:10px;cursor:pointer' },
          box, h('span', {}, 'That happened.')));
    }

    const save = h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg' }, 'Save and continue');
    const hint = h('span', { class: 'asc-submit-hint' });
    function refresh() {
      const marked = marks.filter((m) => m.state).length;
      save.disabled = marked === 0;
      hint.textContent = marked
        ? ''
        : 'Mark at least one expectation before continuing.';
    }
    save.addEventListener('click', async () => {
      save.disabled = true;
      save.textContent = 'Saving…';
      try {
        await api('/tasks/' + encodeURIComponent(task.task_id) + '/trajectory-self-score', {
          method: 'POST',
          body: {
            marks: marks.filter((m) => m.state),
            falsifier_fired: falsifierFired,
          },
        });
        toast('Recorded. Your own expectations, checked against the record.', 'success');
        continueTrajectory(data);
      } catch (e) {
        if (e.status === 401) return;
        save.disabled = false;
        save.textContent = 'Save and continue';
        toast('Could not save that: ' + (e.message || 'unknown error'), 'error');
      }
    });
    refresh();

    return h('div', { class: 'asc-card asc-card-pad asc-substage' },
      h('div', { class: 'asc-substage-head' },
        h('div', { class: 'asc-substage-step' }, 'Your call'),
        h('div', { class: 'asc-substage-title' }, 'Which of your expectations held?')),
      h('div', { class: 'asc-help', style: 'margin-bottom:12px' },
        'Judge against what this record actually shows. It reflects the treatment '
        + 'that was actually given: where you proposed something different, this '
        + 'does not test your plan.'),
      rows, falsifierBlock,
      h('div', { class: 'asc-submit-row', style: 'margin-top:22px' }, hint, save));
  }

  function trajectoryContinueButton(data) {
    const btn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Continue');
    btn.addEventListener('click', () => continueTrajectory(data));
    return btn;
  }

  // Continue the walk. The next point is opened by id rather than by drawing from
  // the queue, so a physician mid-chart stays on that patient — reading a new
  // chart is the expensive part of a task, and the whole per-decision time saving
  // (§5) comes from paying it once.
  function continueTrajectory(data) {
    const next = (data.progress || {}).next_task_id;
    if (next) { openTaskById(next); return; }
    renderEvalView();
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
        'Give a 10-second first impression before you see the AI answers',
        'A single point-in-time (static) case, not a timeline',
        'Requires real-data approval (BAA / training)',
      ],
    },
    {
      v: 'v3', label: 'Synthetic Multimodal Cases', tag: 'Recommended', dot: 'asc-dot-lime',
      blurb: 'Structured synthetic cases (labs, EHR notes, and meds) built to be hard.',
      bullets: [
        'Give a 10-second first impression before the AI answers appear',
        'Compare two AI answers and pick the stronger one',
        'Refine it, flag the weaker one’s errors, and check the reasoning',
      ],
    },
    {
      // V5 (Longitudinal E2E PRD §5.1 Group B): one point of a real chart walk.
      // Unlike the ENV tier below, this IS a single-turn portal version and it
      // DOES go through setPortalVersion() — case → commit → reveal, the V3/V4
      // flow over a truncated chart.
      //
      // `assignedOnly` renders it only when the physician actually has routed
      // points (`longitudinal_available` from /tasks/available). A chart walk reaches a
      // doctor exactly one way — an admin pressing Send — so a tab that showed
      // for everyone would be empty for almost everyone, which reads as the
      // product being broken rather than as the rule it is.
      v: 'v5', label: 'Longitudinal Chart Walks', tag: 'Real patient data', dot: 'asc-dot-lime',
      requiresRealData: true,
      assignedOnly: true,
      blurb: 'Walk one real patient forward in time: decide at each encounter, then see what the chart did next.',
      bullets: [
        'Read the chart truncated at one decision point, nothing after it exists',
        'Commit an assessment, a plan, and what you expect to see next',
        'Say what would tell you that you were wrong',
        'Then the record’s own next encounter is revealed and you score yourself',
        'Points are answered in order; you cannot read ahead',
      ],
    },
    {
      // ENV: the AGENTIC tier (Clinical RL Environments PRD). A different KIND of
      // task, not a variant of the single-turn flow, so selecting it navigates to
      // its own surface instead of calling chooseVersion(): the single-turn queue,
      // submit path, and portal_version stamping are never touched by it.
      //
      // Its `v` is 'env', not 'v5'. That literal now means longitudinal (above),
      // and while this card never calls setPortalVersion() — so the old value
      // could not have leaked into a queue param — leaving two different products
      // sharing one identifier is how the next person wires them together.
      v: 'env', label: 'Clinical RL Environment', tag: 'New', dot: 'asc-dot-orange',
      route: '/asclepius/env/annotate',
      blurb: 'Review an AI agent working a case step by step: label each move and write what it should have done instead.',
      bullets: [
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
  // tier that is a different KIND of task (ENV, agentic), navigate to its own
  // surface. ENV deliberately does NOT go through setPortalVersion(), so 'env' can
  // never end up in the single-turn queue params or on a single-turn submission.
  // V5 (longitudinal) is the opposite case: it IS a single-turn version and takes
  // the ordinary path.
  function selectVersion(o) {
    if (o.route) { window.location.href = o.route; return; }
    chooseVersion(o.v);
  }
  function versionCard(o, last, approved, nWalks) {
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
      // The count, on the card that exists BECAUSE the count is non-zero. A card
      // whose whole reason for being visible is "you have work" should say how
      // much, rather than making the physician click to find out.
      (o.assignedOnly && nWalks)
        ? h('div', { class: 'asc-ver-card-blurb' },
            nWalks + (nWalks === 1 ? ' decision point is' : ' decision points are')
            + ' routed to you.')
        : null,
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
  /* How many longitudinal points are ROUTED to this physician right now.
   *
   * Asked once and cached on `state`, because the answer decides whether a card
   * exists at all and the home page must not flicker a card in after paint. A
   * failure resolves to 0: a doctor with no chart walk sees exactly what they saw
   * before this feature existed, which is the right failure direction for a tab
   * that is empty for almost everyone. */
  async function longitudinalAvailable() {
    // Asked fresh each time rather than cached on `state`. The home screen is
    // rendered rarely — on sign-in and on "change flow" — and a cached zero would
    // hide the tab for a physician who was routed a walk five minutes ago, for
    // the rest of their session, with nothing on screen to explain it.
    if (!(state.user && state.user.real_data_approved)) return 0;
    try {
      // /tasks/available, NOT a /dashboard route: there is no such endpoint, so
      // this read 404ed for every physician and the catch below turned that into
      // a 0, which is the one value that permanently hides the card. The field
      // is the one the queue itself returns, from the same response the
      // dashboard already reads.
      //
      // The specialty is resolved exactly as renderDashboardView resolves it,
      // because `longitudinal_available` is counted THROUGH the specialty the
      // request names. Sending none would count against a different set from
      // the one the V5 queue will serve, and the card and the queue behind it
      // must not disagree about whether there is work.
      const spec = ((state.user && state.user.specialty) || getPortalSpecialty()).trim().toLowerCase();
      const d = await api('/tasks/available?portal_version=v5&limit=1'
        + '&specialty=' + encodeURIComponent(spec));
      return Math.max(0, (d && d.longitudinal_available) || 0);
    } catch (e) {
      // A failure resolves to 0: the physician sees exactly what they saw before
      // this feature existed, which is the right failure direction for a tab that
      // is empty for almost everyone.
      return 0;
    }
  }

  async function renderVersionHome() {
    stopTimer();
    updateHeaderProgress(); // no open task, so the §16 bar hides here
    const last = getPortalVersion();
    const approved = !!(state.user && state.user.real_data_approved);
    // Resolved BEFORE the first paint, so an `assignedOnly` card is either there
    // or it never was — never inserted a moment later under the cursor.
    const nWalks = await longitudinalAvailable();
    const cards = h('div', { class: 'asc-ver-cards' });
    VERSION_OPTS
      .filter((o) => !o.assignedOnly || nWalks > 0)
      .forEach((o) => cards.appendChild(versionCard(o, last, approved, nWalks)));

    // Legacy flows, folded away, exactly as they were, one click deeper.
    const legacyCards = h('div', { class: 'asc-ver-cards', hidden: true });
    LEGACY_VERSION_OPTS.forEach((o) => legacyCards.appendChild(versionCard(o, last, approved, nWalks)));
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
    // A physician who picked the real cases and finished them is continued onto
    // the synthetic queue. The badge above already flips to the served version,
    // which is honest but silent — someone who chose "Real patient data" deserves
    // to be told why the label changed rather than left to notice it.
    const continued = state.continuedFrom === 'v4' && v !== 'v4'
      ? h('span', { class: 'asc-exp-badge-note', title:
            'You have completed every real de-identified case available to you right '
            + 'now. New ones appear here as charts are promoted.' },
          '· continuing from the real cases')
      : null;
    return h('div', { class: 'asc-exp-badge' },
      h('span', { class: 'asc-exp-badge-label' }, meta),
      continued,
      toggle
        ? h('button', {
            class: 'asc-btn asc-btn-ghost asc-btn-sm asc-case-toggle',
            type: 'button', title: isOpen ? 'Hide case' : 'Open case', onClick: toggle,
          }, isOpen ? 'Hide case' : 'Open case')
        : null);
  }

  // ─── Specialty accent dots ──────────────────────────────────────────────────
  // A stable hue per specialty from the console palette (no blue, it left with
  // the design-system migration). Color always pairs with the text label, never
  // the sole carrier. This used to also drive the case-tag chip bar above the
  // clinical question; that bar is gone (§7) and the specialty picker is the
  // remaining consumer.
  // Kept in step with SpecialtyConfig.accent in backend/asclepius/specialties.py.
  // Hepatology shares nephrology's green — the palette has no fifth token and
  // the dot never carries the meaning alone; it always sits beside the label.
  const SPECIALTY_DOT = { nephrology: 'asc-dot-green', cardiology: 'asc-dot-orange',
    oncology: 'asc-dot-pink', hepatology: 'asc-dot-green' };
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

  /* The care-team handoff (§8.4): what the physician before you committed.
   *
   * Their assessment and what they expected — never what actually happened. The
   * outcome of their decision is precisely what THIS physician is being asked to
   * predict, and the server does not send it; this function must never grow a
   * fetch that goes looking for it. It reads state.task and stops. */
  function renderRelayHandoff() {
    const ho = state.task && state.task.relay_handoff;
    if (!ho) return null;                 // point 0, a solo walk, or an ordinary case
    const line = (label, value) => (value
      ? h('div', { class: 'asc-handoff-row' },
          h('span', { class: 'asc-handoff-k' }, label),
          h('span', { class: 'asc-handoff-v' }, value))
      : null);
    const list = (label, items) => ((items && items.length)
      ? h('div', { class: 'asc-handoff-row' },
          h('span', { class: 'asc-handoff-k' }, label),
          h('span', { class: 'asc-handoff-v' }, items.join('; ')))
      : null);
    return h('div', { class: 'asc-handoff' },
      h('div', { class: 'asc-handoff-h' },
        'HANDOFF · ' + (ho.from_label || 'the previous physician')),
      line('Assessment', ho.assessment),
      list('Expecting', ho.expectations),
      list('Would change my mind', ho.falsifiers));
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
    // §7: no metadata chip bar above the question. Specialty, difficulty,
    // modality and capture mode are OUR routing vocabulary, not the
    // physician's. They arrived because a physician chose from a queue and
    // needed to compare cases; with one-button entry (§4) there is nothing to
    // compare, and telling a specialist "Difficulty: hard" before they read the
    // chart primes the answer.
    const promptCard = h('div', { class: 'asc-card asc-prompt-card' },
      h('div', { class: 'asc-card-pad' },
        // §7 removed the metadata chip row (difficulty/modality prime the answer
        // before the chart is read). The trajectory banner is NOT that: it says
        // which decision point of which chart walk this is, which a physician
        // needs to orient at all — the same fact the dashboard card used to carry
        // before one-button entry replaced the card list.
        renderTrajectoryBanner(),
        // §8.4 — the handoff sits ABOVE the question, because it is context the
        // physician reads before deciding anything, exactly as a verbal handoff
        // precedes the ward round. Rendered from the served payload only: the
        // server sends the predecessor's COMMITMENT and never their reveal or
        // self-score, so there is nothing here to hide and nothing to fetch.
        renderRelayHandoff(),
        h('div', { class: 'asc-prompt-label' }, caseObj ? 'Clinical question' : 'Clinical prompt'),
        h('div', { class: 'asc-prompt-text' }, promptText),
      ));

    // Grounding disclaimer banner (required mode only).
    //
    // `Grounding required` was one of the chips deleted above, which makes this
    // banner the ONLY warning a physician gets before submit refuses them. It
    // was gated on `task.grounding_disclaimer` as well as on the requirement —
    // so the warning depended on a field the client does not control.
    //
    // Today that field is safe: routers/asclepius.py sends the
    // GROUNDED_PREMIUM_DISCLAIMER constant for every required task, so it is
    // never empty on the wire and this gate has nothing to catch yet. It is
    // widened anyway, because "the only warning" must not be one server-side
    // refactor away from disappearing — the day the disclaimer becomes
    // per-task or nullable, the warning stays.
    //
    // The requirement itself is unchanged: groundingSatisfied() still enforces
    // it at submit, so neither the chip nor this banner could ever let a bad
    // submission through — this is about warning them in time, not the gate.
    let groundingBanner = null;
    if (required) {
      groundingBanner = h('div', { class: 'asc-grounding-banner' },
        h('div', { class: 'asc-gb-icon', 'aria-hidden': 'true' }),
        h('div', {},
          h('div', { class: 'asc-gb-title' }, 'Evidence required for this task'),
          h('div', { class: 'asc-gb-text' },
            task.grounding_disclaimer
              || 'Every claim in your answer needs a citation anchored to the case.'),
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
    // The expected-trajectory card sits above BOTH, because on a longitudinal case
    // it is the highest-value thing the physician writes and must not read as an
    // afterthought bolted to the submit button.
    // The clinical-validity attestation sits ABOVE the confidence card for the
    // same reason the optional cards do: it is a statement about the case, made
    // before committing to a label, and wedging it into the commit moment would
    // turn a signed assertion into a checkbox somebody clears on the way past.
    const parts = [renderExpectedTrajectoryCard(), renderDecisiveActionCard(),
      renderClinicalValidityCard(), confidenceCard].filter(Boolean);
    return parts.length > 1 ? h('div', {}, ...parts) : confidenceCard;
  }

  // ── Clinical-validity attestation (Gap U2) ─────────────────────────────────
  // The physician says this case could occur in practice and is internally
  // consistent, BEFORE they label it. Under section 3 of the contributor
  // agreement that is an attestation with a consequence, which is why the copy
  // says so plainly rather than reading as one more box.
  //
  // REJECTING IS AS EASY AS ATTESTING, and is the second control here rather
  // than something buried elsewhere. A physician who has to hunt for the honest
  // path takes the dishonest one, and the reject button is the whole reason it
  // is fair to hold them to the attestation at all. It routes through the
  // Stage-1 flag the backend already has, so a rejected case leaves the queue
  // and lands on the admin flagged list exactly as it always did.
  function renderClinicalValidityCard() {
    const d = state.draft;
    if (!isV3()) return null;
    d.prompt_review = d.prompt_review || {};
    const pr = d.prompt_review;

    const box = h('input', { type: 'checkbox', class: 'asc-validity-check' });
    box.checked = pr.attest_clinically_valid === true;
    // The Stage-1 verdict as it stood before this checkbox touched it, so an
    // uncheck restores the prompt-gate answer instead of erasing it.
    const verdictBeforeAttest = pr.verdict;
    box.addEventListener('change', () => {
      // Unchecking is "I am no longer asserting", not "I assert the opposite":
      // an explicit false is a statement a finding could be made against, and
      // the physician never made it. Null keeps the tri-state honest.
      pr.attest_clinically_valid = box.checked ? true : null;
      pr.reviewed = true;
      pr.verdict = box.checked ? 'valid' : verdictBeforeAttest;
      pr.reviewed_at = new Date().toISOString();
      saveDraft();
      updateSubmitState();
    });

    const reject = h('button', {
      class: 'asc-btn asc-btn-subtle asc-btn-sm asc-validity-reject', type: 'button',
    }, 'This case is not clinically valid');
    reject.addEventListener('click', rejectCaseAsInvalid);

    const info = infoDot('Why we ask', [
      'Cases may be modified, and your attestation is what lets us treat a modified '
      + 'case as clinically sound. If you say a case is valid when it is not, that is '
      + 'on you and the case is not paid.',
      'Rejecting costs you nothing. It is the right answer for a case that is wrong, '
      + 'it never counts against your standing or your pay, and it moves you straight '
      + 'to the next case.',
    ]);

    return h('div', { class: 'asc-card asc-card-pad asc-substage' },
      h('div', { class: 'asc-substage-head' },
        h('div', { class: 'asc-substage-step' }, 'Required'),
        h('div', { class: 'asc-substage-title' }, 'Clinical validity', info)),
      h('label', { class: 'asc-validity-row' }, box,
        h('span', {},
          'I attest that this case is clinically valid: it could occur in practice '
          + 'and it holds together as a clinical picture.')),
      h('div', { class: 'asc-validity-out' }, reject));
  }

  async function rejectCaseAsInvalid() {
    // The practice case is deliberately valid; rejecting it would otherwise
    // POST a REAL submission (this path bypasses the tutorial submit branch).
    if (tutorialActive()) {
      toast('This is the practice case: it’s deliberately valid. Attest and continue instead.', 'info');
      return;
    }
    const d = state.draft;
    d.prompt_review = d.prompt_review || {};
    d.prompt_review.attest_clinically_valid = false;
    d.prompt_review.reviewed = true;
    d.prompt_review.verdict = 'flagged';
    d.prompt_review.reviewed_at = new Date().toISOString();
    saveDraft();
    if (state.submitting) return;
    state.submitting = true;
    try {
      // Straight to POST /submissions, mirroring flagPrompt. The gated submit
      // path would swallow the rejection: this card mounts exactly where the
      // staged flow's required state (confidence, the attestation itself) is
      // still unset, and those client gates early-return without a request.
      // The backend's Stage-1 branch reads the flagged verdict before it
      // validates a verdict or a rubric, so a half-filled case rejects cleanly
      // and produces zero records.
      await api('/submissions', { method: 'POST', body: buildSubmissionPayload() });
      clearDraft(d.task_id);
      stopTimer();
      toast('Case rejected as not clinically valid. Loading the next task', 'success');
      renderEvalView();
    } catch (e) {
      if (e.status !== 401) toast('Could not reject the case: ' + e.message, 'error');
    } finally {
      state.submitting = false;
    }
  }

  // ── Expected trajectory (Longitudinal Cases §3.3, field 3) ─────────────────
  // "What should happen next if this assessment is right, and what would tell me I
  // am wrong." Assessment and plan are opinions; this is a PREDICTION, and a
  // prediction is the only thing an outcome can verify. On a trajectory case the
  // chart's own next encounter checks it — nobody grades it, the record does.
  //
  // Clearly OPTIONAL, in its own card, never a required step and never wedged into
  // the submit gate. A physician who cannot name a falsifier for this decision must
  // be able to say so: a fabricated one is worse than none, because it will be
  // scored against a real chart and the score will mean nothing.
  function renderExpectedTrajectoryCard() {
    const d = state.draft;
    if (!isV3()) return null;
    const et = d.expected_trajectory;
    const onTrajectory = !!(state.task && state.task.trajectory_id);

    const rows = h('div', {});
    const paintRows = () => {
      clear(rows);
      et.expectations.forEach((exp, i) => {
        const text = autoGrow(h('textarea', {
          class: 'asc-textarea',
          placeholder: i === 0
            ? 'e.g. enzymes stay down and bilirubin falls over 2–3 weeks'
            : 'another thing you expect to see',
        }, exp.expectation || ''));
        text.addEventListener('input', () => { exp.expectation = text.value; saveDraft(); });
        const days = h('input', {
          class: 'asc-input', type: 'number', min: '1', max: '1825',
          style: 'max-width:150px', placeholder: 'within … days',
          value: exp.horizon_days === '' || exp.horizon_days == null ? '' : String(exp.horizon_days),
        });
        // A prediction with no horizon is not falsifiable — "bilirubin will fall"
        // is true eventually. Optional, because a specialist may genuinely not want
        // to commit to a window, but asked for every time.
        days.addEventListener('input', () => { exp.horizon_days = days.value; saveDraft(); });
        const remove = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button' }, 'Remove');
        remove.addEventListener('click', () => {
          et.expectations.splice(i, 1);
          if (!et.expectations.length) et.expectations.push({ expectation: '', horizon_days: '' });
          saveDraft(); paintRows();
        });
        rows.appendChild(h('div', { class: 'asc-field', style: i ? 'margin-top:14px' : '' },
          text,
          h('div', { class: 'asc-submit-row', style: 'margin-top:8px' },
            days, et.expectations.length > 1 ? remove : null)));
      });
    };
    paintRows();
    const addExp = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button', style: 'margin-top:10px' },
      '+ Add another expectation');
    addExp.addEventListener('click', () => {
      et.expectations.push({ expectation: '', horizon_days: '' }); saveDraft(); paintRows();
    });

    const falsRows = h('div', {});
    const paintFals = () => {
      clear(falsRows);
      et.falsifiers.forEach((f, i) => {
        const ta = autoGrow(h('textarea', {
          class: 'asc-textarea',
          placeholder: 'e.g. if GGT climbs again, the stent has occluded',
        }, f || ''));
        ta.addEventListener('input', () => { et.falsifiers[i] = ta.value; saveDraft(); });
        falsRows.appendChild(h('div', { class: 'asc-field', style: i ? 'margin-top:12px' : '' }, ta));
      });
    };
    paintFals();
    const addFals = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button', style: 'margin-top:10px' },
      '+ Add another');
    addFals.addEventListener('click', () => { et.falsifiers.push(''); saveDraft(); paintFals(); });

    const info = infoDot('Why we ask', [
      onTrajectory
        ? 'This chart continues. Once you submit, we show you what actually happened '
          + 'next and you mark which of your expectations held, from the record, not '
          + 'from a reviewer’s opinion.'
        : 'A stated expectation with a stated falsifier is a prediction rather than an '
          + 'opinion, and a prediction is the only thing an outcome can check.',
      'Skip it when you cannot name one. A made-up falsifier is worse than none: it '
      + 'gets checked against a real chart, and the check means nothing.',
      'We score anticipation of what the record shows, never a counterfactual. What '
      + 'happened next reflects the treatment actually given, not the plan you propose.',
    ]);

    return h('div', { class: 'asc-card asc-card-pad asc-substage' },
      h('div', { class: 'asc-substage-head' },
        h('div', { class: 'asc-substage-step asc-substage-step--optional' }, 'Optional'),
        h('div', { class: 'asc-substage-title' }, 'Expected trajectory', info)),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' },
          'If your assessment is right, what should happen next?'),
        h('div', { class: 'asc-help', style: 'margin-bottom:8px' },
          'One expectation per box. Add a time window where you can commit to one.'),
        rows, addExp),
      h('div', { class: 'asc-field', style: 'margin-top:18px;margin-bottom:0' },
        h('label', { class: 'asc-label' }, 'What would tell you that you are wrong?'),
        h('div', { class: 'asc-help', style: 'margin-bottom:8px' },
          'The observation that would make you abandon this assessment.'),
        falsRows, addFals));
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
      // The practice case is virtual, and this is the one LLM-spend call in the
      // evaluation form that was never fenced off during it: every other
      // task-scoped and assist call already checks (see /assist/prelabel).
      // Typing into a tour is not worth billing a retrieval for, and it is the
      // only spend a visitor with no real-work surface could have triggered.
      if (tutorialActive()) { clear(wrap); return; }
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
        // Gap U2: the attestation gates the LABEL, never the rejection. The
        // reject button is its own control and is never disabled by this, which
        // is the whole point of putting it beside the checkbox.
        else if (isV3() && (d.prompt_review || {}).attest_clinically_valid !== true) {
          ok = false; msg = 'attest the case is clinically valid, or reject it, to submit';
        }
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
      // Longitudinal reveal (§4 Phase 4). Captured BEFORE the draft is cleared and
      // the view is re-rendered: the seal has just been honoured — the action is
      // committed — so this is the first legal moment to show what happened next.
      const revealTask = (state.task && state.task.trajectory_id
                          && payload.expected_trajectory) ? state.task : null;
      // The submission is committed server-side the moment we got the 202, so a
      // poll timeout is "still finalizing", NOT a failure; never lose the work.
      clearDraft(taskId);
      stopTimer();
      if (revealTask) {
        // Straight to the reveal, not through a toast and a fresh queue draw. The
        // physician has just made a prediction about a real patient; making them
        // click back to find out whether it held is the single most disengaging
        // thing this product could do with it.
        state.submitting = false;
        renderTrajectoryOutcomeView(revealTask);
        return;
      }
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
      } else if (isPracticeGate(e)) {
        // The draft is NOT touched. This is a completed clinical evaluation
        // that the gate refused at the last step, and losing it here would be
        // the worst thing this whole change could do to somebody. saveDraft
        // has already run; they come back to it after passing.
        saveDraft();
        goToPracticeCase();
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
        // Gap U2. Sent as a tri-state, never coerced to a boolean: undefined
        // means this client asserted nothing, which the server keeps distinct
        // from an explicit false. A `!!` here would turn "did not say" into
        // "said no" on every legacy draft resumed after this shipped.
        attest_clinically_valid:
          typeof d.prompt_review.attest_clinically_valid === 'boolean'
            ? d.prompt_review.attest_clinically_valid : null,
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
    // Expected trajectory (Longitudinal Cases §3.3 field 3). Sent only when the
    // physician actually wrote an expectation; the server normalizes and stores
    // None for anything that is not a usable prediction, so an empty shell here
    // would only inflate the falsifier corpus's count with nothing behind it.
    const et = d.expected_trajectory || {};
    const expectations = (et.expectations || [])
      .filter((e) => e && (e.expectation || '').trim())
      .map((e) => {
        const days = parseInt(e.horizon_days, 10);
        return {
          expectation: e.expectation.trim(),
          horizon_days: Number.isFinite(days) ? days : null,
        };
      });
    if (expectations.length) {
      payload.expected_trajectory = {
        expectations,
        falsifiers: (et.falsifiers || []).map((f) => (f || '').trim()).filter(Boolean),
        note: (et.note || '').trim(),
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
      window.ReferralSection.render(body, sectionCtx());
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
  //  PROFILE
  //
  //  Everything we hold about a physician, and the short list of it that is
  //  theirs to change. Until this existed a doctor could not see their own
  //  record at all: it was visible to admins and to nobody else, including
  //  them, and a mistyped phone number could only be fixed by writing to us.
  //
  //  The page opens with a CARD rather than a form. Someone who comes here to
  //  correct one phone number should still leave knowing their verification
  //  state, their tier and their score, because nothing else in the product
  //  tells them and they are the three facts that decide what they may do.
  //
  //  Credential fields render read-only rather than being withheld. Someone
  //  should be able to read what they submitted and see plainly which parts
  //  are settled -- and settled is the right word, because those were checked
  //  against a registry or attested to, and a form that let their holder edit
  //  them afterwards would make the check meaningless.
  //
  //  Class prefix is asc-me-, NOT asc-profile-: that one already belongs to
  //  the admin contributor browser, which is a different object entirely (a
  //  stranger's record seen by staff, versus a physician's own).
  // ═══════════════════════════════════════════════════════════════════════════
  function renderProfileView() {
    stopTimer();
    updateHeaderProgress();
    const body = h('div', { id: 'ascProfileBody' });
    setRoot(h('div', { class: 'asc-wrap' }, body));
    body.appendChild(h('div', { class: 'asc-pay-loading' }, 'Loading your profile…'));
    api('/me/profile').then((data) => {
      clear(body);
      body.appendChild(h('h2', { class: 'asc-pay-title' }, 'Profile'));
      body.appendChild(meIdentityCard(data));
      body.appendChild(h('div', { class: 'asc-me-grid' },
        meDetailsPanel(data.editable || {}, data.standing || {}),
        meCredentialsPanel(data.credentials || {}),
        meTrainingPanel(data.training_and_practice),
        meCompletenessPanel(data.completeness),
        meCardPanel(data.standing || {}),
        meHistoryPanel(),
        mePasswordPanel(),
        meAgreementPanel(),
        meReferralPanel(data.standing || {})));
      // Sign out lives on a destination, not only in chrome. It is what lets
      // the rail foot collapse to an avatar on a narrow screen without
      // stranding anybody, and it is where somebody looks for it anyway.
      body.appendChild(h('div', { class: 'asc-me-card asc-me-signout' },
        h('button', { class: 'asc-btn asc-btn-ghost', type: 'button', onClick: logout },
          'Sign out')));
    }).catch((err) => {
      clear(body);
      body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          'Your profile could not be loaded. '
          + ((err && (err.detail || err.message)) || 'The server did not respond.')
          + ' Reload the page.'))));
    });
  }

  function profileRow(label, value) {
    return h('div', { class: 'asc-prof-row' },
      h('span', { class: 'asc-prof-label' }, label),
      h('span', { class: 'asc-prof-value' }, value || '-'));
  }

  /* ── The identity card ──────────────────────────────────────────────────── */
  function meIdentityCard(data) {
    const ed = data.editable || {};
    const cr = data.credentials || {};
    const st = data.standing || {};
    const av = data.avatar || {};
    const name = String(ed.full_name || '').trim() || 'Your profile';
    const spec = [cr.specialty, ed.specialty_niche]
      .map((s) => String(s || '').trim()).filter(Boolean).join(' · ');

    return h('div', { class: 'asc-me-card asc-card' },
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-me-id' },
          meAvatar(av, name),
          h('div', { class: 'asc-me-idtext' },
            h('div', { class: 'asc-me-name' }, name),
            spec ? h('div', { class: 'asc-me-sub' }, spec) : null,
            cr.organization ? h('div', { class: 'asc-me-org' }, cr.organization) : null,
            ed.linkedin_url
              ? h('a', {
                  class: 'asc-me-link', href: linkedinHref(ed.linkedin_url),
                  target: '_blank', rel: 'noopener noreferrer',
                }, prettyLinkedin(ed.linkedin_url))
              : null)),
        meStanding(st)));
  }

  function linkedinHref(url) {
    const v = String(url || '').trim();
    return /^https?:\/\//i.test(v) ? v : 'https://' + v.replace(/^\/+/, '');
  }

  function prettyLinkedin(url) {
    return String(url || '').trim().replace(/^https?:\/\//i, '').replace(/\/+$/, '');
  }

  function meStanding(st) {
    const status = st.verification_status || 'pending';
    // Full class names, not a suffix concatenated on. Built by hand they are
    // invisible both to a grep and to the repo's styled-but-never-emitted
    // scanner, which is how dead CSS accumulates unnoticed.
    const V = {
      approved: { word: 'Verified', dot: 'asc-me-dot-ok',
                  rest: 'your record was checked and approved.' },
      pending: { word: 'In review', dot: 'asc-me-dot-wait',
                 rest: 'a person is reading your file. You can use the portal '
                       + 'while they do.' },
      rejected: { word: 'Not approved', dot: 'asc-me-dot-off',
                  rest: 'write to us and a person will look again.' },
    };
    const v = V[status] || V.pending;

    // One stat, not three.
    //
    // The contributor score is gone: it is an internal instrument for routing
    // and for pay, and showing it to the physician it measures turned it into
    // a number they were managing. The tier stat went with it -- it is the same
    // measurement wearing a permission label, and "Labeler" reads as a rank to
    // a consultant with twenty years in practice. What the tier actually grants
    // is visible in the rail, where the surfaces they can open either are or
    // are not there.
    return h('div', { class: 'asc-me-standing' },
      meStat('Verification', null,
        h('span', { class: 'asc-me-stat-value' },
          h('span', { class: 'asc-me-dot ' + v.dot, 'aria-hidden': 'true' }),
          v.word),
        v.rest));
  }

  function meStat(label, dot, valueNode, rest) {
    return h('div', { class: 'asc-me-stat' },
      h('div', { class: 'asc-me-stat-head' },
        h('span', { class: 'asc-me-stat-label' }, label), dot || null),
      valueNode,
      // What the number MEANS renders inline. The dot beside the label is
      // optional depth; a fact whose only home is a popover is a fact nobody
      // who does not click has.
      h('div', { class: 'asc-me-stat-rest' }, rest));
  }

  /* ── The avatar ─────────────────────────────────────────────────────────── */
  // Empty is the physician's INITIALS on their specialty's colour, the same two
  // letters and the same hue their colleagues already see in the community. Not
  // a grey silhouette: a silhouette says "no record of you" on the one screen
  // whose whole job is to show that there is one.
  function meAvatar(av, name) {
    const initials = String(av.initials || '').trim() || fallbackInitials(name);
    const accent = av.accent || 'green';

    const face = h('div', {
      class: 'asc-me-avatar acc-' + accent + (av.url ? ' has-img' : ''),
      // Decorative: the name it stands for is the very next element, and
      // "Photo of Ahmed Al Otaibi" read immediately before "Ahmed Al Otaibi"
      // is noise.
      'aria-hidden': 'true',
    }, av.url
        ? avatarImgEl(av.url, initials)
        : h('span', { class: 'asc-me-avatar-initials' }, initials));

    // A real file input plus a <label for>: keyboard reachable, announced by a
    // screen reader, and the label doubles as the hover overlay. Visually
    // hidden rather than display:none, which would take the input out of the
    // accessibility tree and break the label with it.
    const file = h('input', {
      class: 'asc-me-file', type: 'file', id: 'ascMeAvatarFile',
      accept: 'image/png,image/jpeg,image/webp',
    });
    const edit = h('label', { class: 'asc-me-avatar-edit', for: 'ascMeAvatarFile' },
      av.url ? 'Change' : 'Add photo');
    const wrap = h('div', { class: 'asc-me-avatarwrap' }, face, file, edit);
    const status = h('div', {
      class: 'asc-me-avatar-status', role: 'status', 'aria-live': 'polite',
    });
    const tools = h('div', { class: 'asc-me-avatar-tools' });
    if (av.url) {
      const remove = h('button', {
        class: 'asc-me-avatar-remove', type: 'button',
      }, 'Remove photo');
      remove.addEventListener('click', () => removeAvatar(wrap, status));
      tools.appendChild(remove);
    }
    file.addEventListener('change', () => {
      const picked = file.files && file.files[0];
      if (picked) uploadAvatar(wrap, status, picked);
    });
    return h('div', { class: 'asc-me-avatarcol' }, wrap, tools, status);
  }

  /* The avatar endpoint is bearer-authenticated, and an <img src> cannot send
     an Authorization header. So: fetch it with the session token and hand the
     element a blob: URL, exactly as community.js does for attachments.

     The initials render underneath in the meantime and stay put if the fetch
     fails, which is the right fallback -- a broken-image glyph on somebody's
     own face is a worse outcome than the two letters they started with. */
  const avatarBlobCache = {};
  // Requests still in flight, keyed by url. Before §3 this path ran once, when
  // the profile page opened. It now runs on every renderSidePanel() — i.e. on
  // every navigation — so without in-flight de-duplication a physician clicking
  // through the rail fires several concurrent fetches for the same picture, and
  // every one that resolves mints another object URL that nothing revokes.
  const avatarBlobPending = {};
  function avatarImgEl(url, initials) {
    const img = h('img', { class: 'asc-me-avatar-img', alt: '' });
    const fallback = h('span', { class: 'asc-me-avatar-initials' }, initials);
    const box = h('span', { class: 'asc-me-avatar-slot' }, fallback);
    loadAvatarBlob(url).then((objectUrl) => {
      if (!objectUrl) return;
      img.src = objectUrl;
      clear(box);
      box.appendChild(img);
    });
    return box;
  }

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

  function fallbackInitials(name) {
    const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return '?';
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  /** Carry an avatar change from the profile page into the session, then
   *  repaint the rail so both surfaces agree immediately. Both /me/avatar
   *  responses return the same `avatar` block; a missing url means "cleared". */
  function syncSessionAvatar(res) {
    if (!state.user) return;
    state.user.avatar_url = (res && res.avatar && res.avatar.url) || null;
    renderSidePanel();
  }

  function uploadAvatar(wrap, status, blob) {
    // The picture they already have stays on screen for the whole upload.
    // Swapping it for a spinner means a failed upload also loses them the
    // sight of what they had.
    wrap.classList.add('is-busy');
    wrap.setAttribute('aria-busy', 'true');
    status.className = 'asc-me-avatar-status';
    status.textContent = 'Uploading…';
    const form = new FormData();
    form.append('file', blob);
    // isForm: the api() helper JSON-stringifies a body and stamps a JSON
    // Content-Type otherwise, which would destroy the multipart boundary.
    api('/me/avatar', { method: 'POST', body: form, isForm: true }).then((res) => {
      // The rail avatar (§3) reads state.user, which the SESSION payload filled
      // at sign-in. Without this the physician would see their new photo on the
      // profile and their old initials in the rail until the next page load.
      syncSessionAvatar(res);
      renderProfileView();
    }).catch((err) => {
      wrap.classList.remove('is-busy');
      wrap.removeAttribute('aria-busy');
      // Inline, as a sentence, next to the thing that failed. Never a title
      // attribute and never only a colour: a failure a doctor cannot read is
      // one they retry with the same file until they give up on the feature.
      status.className = 'asc-me-avatar-status is-error';
      status.textContent = (err && (err.detail || err.message))
        || 'That did not upload. PNG or JPEG, under 5 MB.';
    });
  }

  function removeAvatar(wrap, status) {
    wrap.classList.add('is-busy');
    status.className = 'asc-me-avatar-status';
    status.textContent = 'Removing…';
    api('/me/avatar', { method: 'DELETE' }).then((res) => {
      syncSessionAvatar(res);
      renderProfileView();
    }).catch((err) => {
      wrap.classList.remove('is-busy');
      status.className = 'asc-me-avatar-status is-error';
      status.textContent = (err && (err.detail || err.message))
        || 'That did not work. Try again.';
    });
  }

  /* ── Yours to change ────────────────────────────────────────────────────── */
  // Mirrors backend/asclepius/plausibility.py::_LINKEDIN_RE. Two copies of one
  // rule: this one is advisory and the server's decides, so a drift costs a
  // slightly wrong hint rather than a wrong score.
  const LINKEDIN_RE =
    /^(?:https?:\/\/)?(?:[a-z]{2,3}\.)?linkedin\.com\/(?:in|pub|profile)\/[^/\s]{2,}/i;

  /* What is TRUE about adding a LinkedIn URL, which is not what you would
     guess. It is worth 3 points of 100 in credentialing.TIER_WEIGHTS, it only
     scores if the URL actually parses as LinkedIn, and users.tier_score has
     exactly one writer in the codebase: the admin approval path. Nothing
     recomputes when a physician edits their own profile.

     So "adding LinkedIn increases your payouts" would be false twice over for
     an approved account, and a doctor who added it and watched nothing change
     would have learned that this product tells them things that are not so. */
  function linkedinInfo(status) {
    if (status === 'approved') {
      return {
        title: 'Why add LinkedIn',
        lines: [
          'It will not change your tier or what you are paid. Your tier was set '
          + 'by a person when your record was approved, and editing your profile '
          + 'does not recalculate it.',
          'It has to be your actual profile address, the one starting '
          + 'linkedin.com/in/. Anything else gets flagged for a person to look at.',
        ],
      };
    }
    return {
      title: 'Why add LinkedIn',
      lines: [
        'Your record is still being read, and this is one of the few things on '
        + 'your file you can still add while a person reviews it.',
        'It has to be your actual profile address, the one starting '
        + 'linkedin.com/in/. Anything else raises a flag for a person to check.',
      ],
    };
  }

  function linkedinHint(status) {
    return status === 'approved'
      ? 'Shown on your record to the Archangel team. It does not change your '
        + 'tier or your pay.'
      : 'Shown on your record to the person reviewing it.';
  }

  function meDetailsPanel(editable, standing) {
    const panel = h('div', { class: 'asc-me-panel' });
    panel.appendChild(h('div', { class: 'asc-ref-title' }, 'Yours to change'));

    const fields = [
      { key: 'full_name', label: 'Full name', ph: 'As it appears on your licence' },
      { key: 'phone', label: 'Mobile',
        ph: 'Only used if we need to reach you about your work' },
      { key: 'linkedin_url', label: 'LinkedIn', ph: 'linkedin.com/in/…',
        info: linkedinInfo(standing.verification_status),
        hint: linkedinHint(standing.verification_status) },
      { key: 'specialty_niche', label: 'What you focus on',
        ph: 'Transplant nephrology, interventional…' },
    ];

    const inputs = {};
    fields.forEach((f) => {
      const input = h('input', {
        class: 'asc-ref-input', type: 'text', placeholder: f.ph,
        id: 'ascMe_' + f.key,
      });
      input.value = editable[f.key] || '';
      inputs[f.key] = input;
      const hint = h('div', { class: 'asc-me-hint' }, f.hint || '');
      if (!f.hint) hint.style.display = 'none';
      panel.appendChild(h('div', { class: 'asc-me-field' },
        h('div', { class: 'asc-me-fieldhead' },
          h('label', { class: 'asc-prof-label', for: 'ascMe_' + f.key }, f.label),
          f.info ? infoDot(f.info.title, f.info.lines) : null),
        input, hint));
      if (f.key === 'linkedin_url') {
        // Live, inline, and the same shape the server's plausibility check
        // uses. A malformed URL there raises a review flag on this physician's
        // own file, so telling them here is worth more than telling a reviewer.
        const check = () => {
          const bad = input.value.trim() && !LINKEDIN_RE.test(input.value.trim());
          hint.className = 'asc-me-hint' + (bad ? ' is-warn' : '');
          hint.textContent = bad
            ? 'That is not a LinkedIn profile URL. Use the address of your own '
              + 'profile page, the one starting linkedin.com/in/.'
            : f.hint;
        };
        input.addEventListener('input', check);
        check();
      }
    });

    const note = h('div', { class: 'asc-ref-msg', style: 'display:none' });
    const save = h('button', {
      class: 'asc-btn asc-btn-sm asc-btn-go', type: 'button',
    }, 'Save');
    save.addEventListener('click', () => {
      save.disabled = true;
      save.textContent = 'Saving…';
      const payload = {};
      fields.forEach((f) => { payload[f.key] = inputs[f.key].value; });
      api('/me/profile', { method: 'PATCH', body: payload }).then(() => {
        // Success and failure used to land in the same green node, so a save
        // that failed looked exactly like one that worked.
        note.className = 'asc-ref-msg';
        note.textContent = 'Saved.';
        note.style.display = '';
      }).catch((err) => {
        note.className = 'asc-ref-error';
        note.textContent = 'Could not save. '
          + ((err && (err.detail || err.message)) || 'Try again.');
        note.style.display = '';
      }).then(() => {
        save.disabled = false;
        save.textContent = 'Save';
      });
    });
    panel.appendChild(h('div', { class: 'asc-ref-form asc-me-actions' }, save));
    panel.appendChild(note);
    return panel;
  }

  /* ── Settled ────────────────────────────────────────────────────────────── */
  // Training and practice: everything the physician typed at signup that the
  // admin could read back and they could not. Absent stays absent, so a doctor
  // who listed no subspecialty sees no subspecialty heading rather than an
  // empty row that reads like deleted data.
  function meTrainingPanel(detail) {
    const d = detail || {};
    if (!Object.keys(d).length) return null;
    const panel = h('div', { class: 'asc-me-panel' });
    panel.appendChild(h('div', { class: 'asc-ref-title' }, 'Training and practice'));
    const rows = h('div', { class: 'asc-me-rows' });
    const list = (v) => Array.isArray(v) ? v.filter(Boolean).join(', ') : v;
    const certs = (v) => (Array.isArray(v) ? v : []).map((b) => {
      if (!b || typeof b !== 'object') return String(b || '');
      return [b.board, b.subspecialty].filter(Boolean).join(' · ');
    }).filter(Boolean).join('; ');
    const trainingRows = (v) => (Array.isArray(v) ? v : []).map((r) => {
      if (!r || typeof r !== 'object') return String(r || '');
      return [r.program, r.year].filter(Boolean).join(', ');
    }).filter(Boolean).join('; ');
    const add = (l, v) => { if (v) rows.appendChild(profileRow(l, v)); };
    add('Languages', list(d.languages));
    add('Subspecialties', list(d.subspecialties));
    add('Board certifications', certs(d.board_certifications));
    add('Residency', trainingRows(d.residency));
    add('Fellowship', trainingRows(d.fellowship));
    add('Practice settings', list(d.practice_settings));
    add('Years in active practice',
        d.years_in_active_practice == null ? '' : String(d.years_in_active_practice));
    add('Review experience', list(d.structured_review_experience));
    if (!rows.childNodes.length) return null;
    panel.appendChild(rows);
    return panel;
  }

  // A meter, not a demand. Everything it counts is optional and none of it
  // gates anything; it exists because a fuller profile routes better work.
  function meCompletenessPanel(comp) {
    const c = comp || {};
    if (!c.missing) return null;
    const panel = h('div', { class: 'asc-me-panel' });
    panel.appendChild(h('div', { class: 'asc-ref-title' }, 'Your profile'));
    const pct = Math.max(0, Math.min(100, Number(c.percent) || 0));
    panel.appendChild(h('div', { class: 'asc-me-meter' },
      h('div', { class: 'asc-me-meter-fill', style: 'width:' + pct + '%' })));
    if (c.complete) {
      panel.appendChild(h('div', { class: 'asc-ref-pitch' },
        'Complete. Nothing else needed from you.'));
      return panel;
    }
    panel.appendChild(h('div', { class: 'asc-ref-pitch' },
      pct + '% complete. Adding these helps us route work that fits you:'));
    const rows = h('div', { class: 'asc-me-rows' });
    (c.missing || []).slice(0, 4).forEach((m) => {
      rows.appendChild(h('div', { class: 'asc-me-missing' }, m.label));
    });
    panel.appendChild(rows);
    return panel;
  }

  // The card, minted on request. Nothing is minted automatically: a public page
  // about somebody is theirs to create.
  function meCardPanel(standing) {
    const panel = h('div', { class: 'asc-me-panel' });
    panel.appendChild(h('div', { class: 'asc-ref-title' }, 'Your verified card'));
    if ((standing || {}).verification_status !== 'approved') {
      panel.appendChild(h('div', { class: 'asc-ref-pitch' },
        'Once your credentials are verified you can create a public card that '
        + 'shows you are a verified physician here.'));
      return panel;
    }
    panel.appendChild(h('div', { class: 'asc-ref-pitch' },
      'A public page showing your name, specialty and that you are verified. '
      + 'No case work and no ratings appear on it. Share it or take it down '
      + 'whenever you like.'));
    const out = h('div', { class: 'asc-me-rows' });
    const mint = h('button', { class: 'asc-btn asc-btn-primary', type: 'button',
      onClick: () => {
        mint.disabled = true;
        api('/me/card', { method: 'POST' }).then((r) => {
          clear(out);
          out.appendChild(profileRow('Card link', r.url));
          const copy = h('button', { class: 'asc-btn asc-btn-ghost', type: 'button',
            onClick: () => {
              // Say it worked. A copy button that reports nothing leaves
              // somebody pasting to find out whether it did.
              const done = () => { copy.textContent = 'Copied'; };
              if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(r.url).then(done, () => {});
              }
            } }, 'Copy link');
          out.appendChild(copy);
          out.appendChild(h('button', { class: 'asc-btn asc-btn-ghost', type: 'button',
            onClick: () => api('/me/card', { method: 'DELETE' }).then(() => {
              clear(out);
              out.appendChild(h('div', { class: 'asc-ref-pitch' },
                'Card taken down. The link no longer works.'));
            }) }, 'Take it down'));
          mint.disabled = false;
          mint.textContent = 'Create a new link';
        }).catch((err) => {
          mint.disabled = false;
          clear(out);
          out.appendChild(h('div', { class: 'asc-inline-error' },
            (err && (err.detail || err.message)) || 'The card could not be created.'));
        });
      } }, 'Create my card');
    panel.appendChild(mint);
    panel.appendChild(out);
    return panel;
  }

  // Counts and dates. Deliberately nothing derived from grading: this is the
  // history a physician reads about themselves.
  function meHistoryPanel() {
    const panel = h('div', { class: 'asc-me-panel' });
    panel.appendChild(h('div', { class: 'asc-ref-title' }, 'Your case history'));
    const body = h('div', { class: 'asc-me-rows' });
    panel.appendChild(body);
    api('/me/stats').then((s) => {
      clear(body);
      const total = s.total_cases != null ? s.total_cases : s.total;
      if (total != null) body.appendChild(profileRow('Cases completed', String(total)));
      if (s.last_7_days != null) body.appendChild(profileRow('Last 7 days', String(s.last_7_days)));
      // Only when it is running. A permanent "Day streak 0" turns a count of
      // what somebody did into a reproach for what they did not.
      if (s.day_streak) {
        body.appendChild(profileRow('Day streak',
          String(s.day_streak) + (s.day_streak === 1 ? ' day' : ' days')));
      }
      if (s.last_submission_at) {
        body.appendChild(profileRow('Most recent', String(s.last_submission_at).slice(0, 10)));
      }
      const months = (s.monthly || []).filter((m) => m && m.month);
      if (months.length) {
        const peak = Math.max.apply(null, months.map((m) => Number(m.count) || 0)) || 1;
        const bars = h('div', { class: 'asc-me-spark' });
        months.forEach((m) => {
          const pct = Math.round(100 * (Number(m.count) || 0) / peak);
          bars.appendChild(h('div', { class: 'asc-me-spark-bar',
            title: m.month + ': ' + m.count,
            style: 'height:' + Math.max(3, pct) + '%' }));
        });
        body.appendChild(bars);
      }
    }).catch(() => {
      clear(body);
      body.appendChild(h('div', { class: 'asc-ref-pitch' },
        'Your history could not be loaded just now.'));
    });
    return panel;
  }

  function meCredentialsPanel(c) {
    const panel = h('div', { class: 'asc-me-panel' });
    panel.appendChild(h('div', { class: 'asc-ref-title' }, 'Credentials on file'));
    // The page has to go on saying WHY these are locked. Silence reads as a
    // form that is broken rather than a record that is settled, and that
    // difference matters a great deal to somebody whose licence number is
    // wrong on it.
    panel.appendChild(h('div', { class: 'asc-ref-pitch asc-me-locked' },
      'Checked against a registry at signup, or attested to by you, so they '
      + 'are not editable here. If something is wrong, write to us and a '
      + 'person will correct it.'));
    const rows = h('div', { class: 'asc-me-rows' });
    const add = (l, v) => rows.appendChild(profileRow(l, v));
    add('Email', c.email);
    add('Specialty', c.specialty);
    add('Qualification', c.qualification);
    add('Board certification', c.board_cert);
    add('Years in practice', c.years_experience == null ? '' : String(c.years_experience));
    add('Institution', c.organization);
    if (c.npi) add('NPI', c.npi);
    else add(c.registry_name || 'Registration', c.registration_number);
    if (c.country_of_practice) add('Practising in', c.country_of_practice);
    if (c.signed_initials) add('Signed attestations', c.signed_initials);
    panel.appendChild(rows);
    return panel;
  }

  function mePasswordPanel() {
    const panel = h('div', { class: 'asc-me-panel' });
    panel.appendChild(h('div', { class: 'asc-ref-title' }, 'Password'));
    const current = h('input', { class: 'asc-ref-input', type: 'password',
                                 placeholder: 'Current password' });
    const next = h('input', { class: 'asc-ref-input', type: 'password',
                              placeholder: 'New password (at least 8 characters)' });
    const note = h('div', { class: 'asc-ref-msg', style: 'display:none' });
    // Ghost, not green: changing a password is maintenance, and only one
    // control on this page is the thing we are asking them to do.
    const button = h('button', { class: 'asc-btn asc-btn-sm asc-btn-ghost', type: 'button' },
      'Change password');
    button.addEventListener('click', () => {
      note.style.display = 'none';
      button.disabled = true;
      button.textContent = 'Changing…';
      api('/me/password', { method: 'POST', body: {
        current_password: current.value, new_password: next.value,
      } }).then(() => {
        note.className = 'asc-ref-msg';
        note.textContent = 'Password changed.';
        note.style.display = '';
        current.value = ''; next.value = '';
      }).catch((err) => {
        note.className = 'asc-ref-error';
        note.textContent = (err && (err.detail || err.message)) || 'Could not change it.';
        note.style.display = '';
      }).then(() => {
        button.disabled = false;
        button.textContent = 'Change password';
      });
    });
    panel.appendChild(h('div', { class: 'asc-me-field' }, current));
    panel.appendChild(h('div', { class: 'asc-me-field' }, next));
    panel.appendChild(h('div', { class: 'asc-ref-form asc-me-actions' }, button));
    panel.appendChild(note);
    return panel;
  }

  /* The standing door to the contributor agreement, open whether or not the
     queue gate is armed.

     This is the half of the rollout the gate depends on: the terms have to be
     readable and signable by the physicians who are already here BEFORE anyone
     arms ASCLEPIUS_AGREEMENT_GATE, or arming it locks a roster that never had a
     chance to sign. Nothing is fetched until it is clicked.

     Not shown to a non-physician account. An advisor and a referrer are not
     contributors, there is nothing for them to sign, and offering it would
     invite a signature on a document that does not describe them. */
  function meAgreementPanel() {
    if (isAdvisor() || isReferralOnly()) return null;
    const panel = h('div', { class: 'asc-me-panel' });
    panel.appendChild(h('div', { class: 'asc-ref-title' }, 'Contributor agreement'));
    panel.appendChild(h('div', { class: 'asc-ref-pitch' },
      'The terms you work under, in full. Read them any time, and sign here if '
      + 'you have not yet.'));
    const button = h('button', { class: 'asc-btn asc-btn-sm asc-btn-ghost', type: 'button' },
      'Open the agreement');
    button.addEventListener('click', () => renderAgreementView({ onBack: renderProfileView }));
    panel.appendChild(h('div', { class: 'asc-ref-form asc-me-actions' }, button));
    return panel;
  }

  function meReferralPanel(st) {
    if (!st.referral_code) return null;
    const panel = h('div', { class: 'asc-me-panel' });
    panel.appendChild(h('div', { class: 'asc-ref-title' }, 'Your referral code'));
    panel.appendChild(h('div', { class: 'asc-me-code asc-mono' }, st.referral_code));
    panel.appendChild(h('div', { class: 'asc-ref-pitch' },
      'Your link and everyone you have referred live on the Referral tab.'));
    return panel;
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
      window.EarningsSection.render(body, sectionCtx());
      return;
    }
    // A VISIBLE error, never a quiet placeholder — and never a reassuring $0.
    // "We could not load your ledger" and "you have earned nothing" must not
    // look the same to a physician.
    body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-error' },
        'The Earnings section failed to load, so no figure is shown, this is '
        + 'not a statement that you have earned nothing. Reload the page; if it '
        + 'persists, this is a deploy problem, check that earnings.js is '
        + 'included in index.html. Nothing you have earned is affected.'))));
  }

  function sectionModuleMissing(body, name) {
    body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-inline-error' },
        name + ' failed to load: refresh the page. If it persists, check that the ' +
        'script is included in index.html.'))));
  }

  function loadingCard(label) {
    return h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), label || 'Loading…'));
  }
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

  // ─── Session continuity: the clock only runs while somebody is there ───────
  //
  // The timer used to keep ticking on a hidden tab, and getElapsed() is what
  // becomes `time_spent_sec` on the submission and what the admin per-case view
  // reads. A case left open overnight billed the night.
  //
  // Tab hidden is the honest "not working" signal. Window BLUR is not: a
  // physician clicking to a second monitor, a PDF or a reference is still
  // working, and pausing there would undercount real effort. So blur saves and
  // nothing else.
  window.addEventListener('beforeunload', saveDraft);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      // Order matters: save FIRST (getElapsed is still measuring a running
      // clock), then stop. Stopping first would be harmless now that stopTimer
      // folds the elapsed into baseElapsed, but this order keeps the intent
      // legible and does not depend on that.
      saveDraft();
      stopTimer();
    } else if (state.draft && state.task && state.view === 'eval') {
      // Rebase from the SAVED value, which is what makes the away time vanish.
      // Gated on an actually-open task: a leftover draft on the dashboard or on
      // the empty-queue screen must not start a clock nobody is watching, which
      // would then have its 5s autosave inflate that draft's elapsed.
      startTimer(state.draft.elapsedSec || 0);
    }
  });
  window.addEventListener('blur', () => { if (state.draft) saveDraft(); });

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
          // Its own completion, and nothing else's. This used to read
          // `d.stage !== 'prompt_review'`, which is ALSO ch1-valid's advance
          // predicate: one stale draft bit satisfied both in a single pass of
          // tutTick's fast-forward loop, so the tour opened on step 3 of 14.
          // "The Labs tab was opened" is interaction state with no draft
          // representation, so it gets a tour-local ledger rather than an
          // invented draft field (tour bookkeeping must never ride in the
          // submitted payload).
          doneWhen: () => tutDone('ch1-tabs') },
        { id: 'ch1-valid', target: TOUR_TARGETS.promptContinue,
          copy: 'If the case reads as real and answerable, continue. If not, flag it.',
          // The exact field validatePrompt() writes, which belongs to this step
          // alone. `d.stage !== 'prompt_review'` was a downstream consequence
          // shared by every step after it, which is what made it collide.
          advanceOn: { state: (d) => !!(d.prompt_review && d.prompt_review.reviewed) },
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
          autofill: () => commitIndependentAnswerAndReveal() },
      ],
    },
    {
      id: 'ch3', title: 'Compare & pick',
      intro: 'Two anonymized model answers to the same case. Judge the reasoning.',
      steps: [
        { id: 'ch3-read', target: TOUR_TARGETS.answers,
          copy: 'Read both answers, then continue.',
          advanceOn: { manual: true },
          // Same collision as ch1-tabs: `!!d.verdict` is ch3-verdict's advance
          // predicate too, so one restored verdict cleared both steps at once.
          doneWhen: () => tutDone('ch3-read'),
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
          } },
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
          autofill: () => autofillReasoningSteps() },
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
          } },
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

  // Steps whose completion is an INTERACTION rather than a draft state get
  // recorded here instead of being inferred from the draft. Tour-local and
  // never persisted: it exists so a step's predicate can identify its own
  // completion and nothing else's, which is the fix for the tour opening
  // mid-flow.
  function tutMarkDone(stepId) {
    const t = state.tutorial;
    if (!t) return;
    t.done = t.done || {};
    t.done[stepId] = true;
  }

  function tutDone(stepId) {
    const t = state.tutorial;
    return !!(t && t.done && t.done[stepId]);
  }

  // Steps the physician advanced with "Skip this step" rather than by doing
  // them. Declared to the server on submit; it decides which of them are
  // graded and therefore disqualifying.
  function tutMarkAssisted(stepId) {
    const t = state.tutorial;
    if (!t) return;
    t.assisted = t.assisted || {};
    t.assisted[stepId] = true;
  }

  function tutAssistedList() {
    const t = state.tutorial;
    return t && t.assisted ? Object.keys(t.assisted) : [];
  }

  function tutAdvance() {
    const t = state.tutorial;
    if (!t) return;
    const cur = tutCurrentStep();
    if (cur) tutMarkDone(cur.id);
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
    // One welcome screen, then an uninterrupted flow (chapter intros ride
    // along as a second line on each chapter's first tooltip instead).
    //
    // Checked BEFORE the fast-forward loop below, not after it. When it ran
    // after, the loop advanced the pointer while the welcome screen was still
    // the thing on screen, so dismissing it dropped the physician into the
    // middle of the tour. Now a fresh start cannot move off step 1 no matter
    // what any predicate says.
    if (!t.welcomed) { renderTourWelcome(); return; }
    resolveTourIndex();
    let step = tutCurrentStep();
    let moved = false;
    while (step && tutStepSatisfied(step)) { t.idx += 1; moved = true; step = tutCurrentStep(); t.bounced = false; }
    if (moved && step) tutPersistStep(step.id);
    if (!step) { hideTourLayer(); return; }  // waiting on the submit path
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

  // The steps THIS physician will actually see. skipIf prunes up to four (the
  // three both_inadequate branches and ch4-reasoning when the task does not
  // capture reasoning), and counting them anyway reported a tour longer than
  // the one being taken: "step 9 of 14" on a run that ends at 10.
  function tutVisibleSteps() {
    const d = state.draft || {};
    return TUTORIAL_STEPS.filter((s) => {
      try { return !(s.skipIf && s.skipIf(d)); } catch (e) { return true; }
    });
  }

  function tutStepNumber(step) {
    const visible = tutVisibleSteps();
    const i = visible.findIndex((s) => s.id === step.id);
    // A step can be pruned out from under the pointer mid-tour; fall back to
    // the full list rather than rendering "step 0".
    if (i < 0) return { n: TUTORIAL_STEPS.findIndex((s) => s.id === step.id) + 1,
                        total: TUTORIAL_STEPS.length };
    return { n: i + 1, total: visible.length };
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
                 stuck ? 'stuck' : '',
                 // The counter is part of what this tooltip renders, so it has
                 // to be part of what decides whether to rebuild it. Without
                 // it, a skipIf flipping mid-tour changes the denominator and
                 // the bar keeps showing the old one.
                 num.n + '/' + num.total].join('|');
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
          // Recorded, then sent with the submission. The placeholders above
          // were written to be CLINICALLY reasonable so the draft genuinely
          // advances, and clinically reasonable is exactly what the answer key
          // checks: without this ledger, fourteen clicks of this button
          // matched all four reference findings and, now that the practice
          // case gates real work, would have unlocked every paid case.
          //
          // The affordance itself stays, because being stranded by a target
          // that never renders is a real failure. It just stops counting as a
          // pass. Only the graded steps matter; the server holds that list.
          tutMarkAssisted(step.id);
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
      onClick: confirmSkipTutorial }, 'Leave for now'));
    pop.appendChild(row);
    const frac = h('div', { class: 'asc-tour-bar' });
    frac.appendChild(h('div', { class: 'asc-tour-bar-fill',
      style: 'width:' + Math.round((num.n / num.total) * 100) + '%' }));
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
          'Leave for now')));
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
    // ALWAYS from the top, on a clean draft.
    //
    // Mid-tour resume is gone. It read a saved server position and skipped the
    // welcome screen whenever it fired, and the local draft it resumed onto
    // was never cleared on abandon: not on the error path below, not in
    // logout() (which SAVES the draft), and not on a reload. A single stale
    // `draft.stage` was then enough to satisfy the first two steps at once and
    // open the tour on step 3 of 14 with no orientation.
    //
    // The practice case is four minutes and is now mandatory, so
    // cross-device resume was never worth the bug budget it was costing.
    clearDraft(TUTORIAL_TASK_ID);
    if (!opts.replay) {
      api('/me/tutorial', { method: 'PATCH', body: { action: 'start' } })
        .then((u) => { state.user = u; }).catch(() => { /* best-effort */ });
    }
    // Step objects are module-level and shared across runs: a stale wait clock
    // or scroll marker from a previous run would misfire on this one.
    TUTORIAL_STEPS.forEach((s) => { s._waitSince = null; });
    _tourScrolledFor = null;
    state.tutorial = { active: true, replay: !!opts.replay, idx: 0,
                       welcomed: false, done: {}, assisted: {} };
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
      clearDraft(TUTORIAL_TASK_ID);  // don't leave a half-built draft to resume onto
      state.portalChosen = false; state.specialtyChosen = false;
      if (e.status !== 401) {
        toast('Could not load the practice case: ' + e.message, 'error');
        renderDashboardView();
      }
      return;
    }
    state.task = data.task;
    initDraftForTask(state.task);
    // initDraftForTask restores from localStorage, and the whole class of
    // stale-draft failure above lives in what it can hand back. The tour's
    // invariant is that a run begins at the first stage, so assert it here
    // rather than hoping the clear above was enough.
    if (state.draft.stage !== 'prompt_review') state.draft = newDraft(state.task);
    state.draft.portal_version = 'v3'; // the tutorial teaches the seamless flow
    saveDraft();
    mountTourEngine();
    renderTaskWorkspace();
    tutTick();
  }

  // "Leave", not "skip". Skipping is retired: the practice case gates all real
  // work, so a skip grants nothing, and a button that appears to let somebody
  // out while leaving every other button 403ing is worse than no button.
  //
  // But nobody should be trapped in a modal either, so this still exists and
  // still lets them out. It just tells the truth about what leaving means, and
  // it does NOT clear the draft: they can come back to the work they did.
  function confirmSkipTutorial() {
    if (document.getElementById('ascTourSkipConfirm')) return;
    // The spotlight box + tooltip sit ABOVE this confirm dialog (z 1200 vs
    // 1000): hide them first, or the old highlight and copy stay pasted on
    // screen behind/around the dialog instead of clearing out of the way.
    hideTourLayer();
    const overlay = h('div', { class: 'call-team-overlay is-open asc-tour-interstitial', id: 'ascTourSkipConfirm' });
    const popup = h('div', { class: 'call-team-popup asc-tour-inter-pop', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'call-team-title' }, 'Leave the practice case?'),
      h('p', { class: 'asc-help', style: 'margin:6px 0 16px' },
        'Real cases open once you have passed it. Your work here is kept, so you can pick up where you left off.'),
      h('div', { style: 'display:flex;gap:10px' },
        h('button', { class: 'asc-btn asc-btn-primary', type: 'button',
          onClick: () => { overlay.remove(); tutTick(); } }, 'Keep going'),
        h('button', { class: 'asc-btn asc-btn-ghost', type: 'button',
          onClick: () => { overlay.remove(); leaveTutorial(); } }, 'Leave')));
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
  }

  function leaveTutorial() {
    const wasReplay = state.tutorial && state.tutorial.replay;
    // No PATCH. There is nothing to record: leaving grants nothing and takes
    // nothing away, and the old `action: 'skip'` wrote a terminal state that
    // then suppressed relaunch.
    teardownTutorial();
    // The draft SURVIVES on a real run, so the work they did is still there
    // when they come back. Only a replay clears, because a replay is a fresh
    // attempt by definition.
    if (wasReplay) clearDraft(TUTORIAL_TASK_ID);
    stopTimer();
    state.task = null;
    state.portalChosen = false;
    state.specialtyChosen = false;
    // §6: a skipped practice case is a CLOSED stop, not an abandoned
    // walkthrough. The server closes it from this same PATCH /me/tutorial
    // 'skip', so the checklist shows it skipped and never asks again — but the
    // remaining stops still have things to say, so resume rather than exit.
    // state.user is refreshed by the PATCH above; re-read it there.
    if (!wasReplay && window.FirstRunWalkthrough) {
      api('/auth/me').then((u) => {
        if (u) state.user = u;
        // Carry on through the tour only while it is genuinely unfinished — some
        // stop with no outcome at all. `firstRunPending()` is the wrong question
        // here now that `deferred` keeps a stop open: it would walk a physician
        // who replayed the practice case months later back through an onboarding
        // they already declined, which is the nagging §2 removed.
        if (firstRunTourPending()) resumeFirstRun(); else renderDashboardView();
      }).catch(() => renderDashboardView());
    } else {
      renderDashboardView();
    }
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
      // Merged HERE, not in buildSubmissionPayload: that function is shared
      // with the real submit path, which has no tour and must not grow a
      // field about one.
      const body = Object.assign(buildSubmissionPayload(), { assisted: tutAssistedList() });
      res = await api('/tutorial/submit', { method: 'POST', body: body });
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
    const passed = !!result.passed;
    const mustAck = (result.must_acknowledge || []).slice();
    // A miss the physician never opened is a miss they never read. The primary
    // button waits on them opening each one: one click per miss, not a quiz.
    const acked = {};
    let primaryBtn = null;

    function primaryEnabled() {
      return mustAck.every((id) => acked[id]);
    }
    function syncPrimary() {
      if (!primaryBtn) return;
      const ok = primaryEnabled();
      primaryBtn.disabled = !ok;
      primaryBtn.title = ok ? '' : 'Open what you missed first.';
    }

    const rows = (result.findings || []).map((f) => {
      const needsAck = mustAck.indexOf(f.id) !== -1;
      const body = h('div', {},
        h('div', { class: 'asc-tour-finding-label' }, f.label),
        h('div', { class: 'asc-tour-finding-reason' }, f.reason),
        // What THEY put where the key looked, in their own words. "You missed
        // the congestion evidence" teaches nothing next to the sentence they
        // actually wrote.
        f.your_answer
          ? h('div', { class: 'asc-tour-finding-yours' }, f.your_answer)
          : null);
      if (!needsAck) {
        return h('div', { class: 'asc-tour-finding' + (f.matched ? ' matched' : '') },
          h('span', { class: 'asc-tour-finding-glyph', 'aria-hidden': 'true' }, f.matched ? '✓' : '-'),
          body);
      }
      const det = h('details', { class: 'asc-tour-finding asc-tour-finding-ack' },
        h('summary', {},
          h('span', { class: 'asc-tour-finding-glyph', 'aria-hidden': 'true' }, '-'),
          h('span', { class: 'asc-tour-finding-label' }, f.label)),
        body);
      det.addEventListener('toggle', () => {
        if (det.open) { acked[f.id] = true; syncPrimary(); }
      });
      return det;
    });

    const planted = result.planted_finding;
    const teach = result.teaching || {};
    const keyData = teach.key_data || [];

    // Why the trap works. Every field here is stripped from the task by
    // _blind_task and has never been visible: it opens only now, once an
    // answer is committed and they can no longer be anchored by it.
    const teachingCard = (teach.reference_answer || keyData.length)
      ? h('details', { class: 'asc-tour-teaching' },
          h('summary', {}, 'What the reference panel read'),
          keyData.length
            ? h('ul', { class: 'asc-tour-keydata' },
                keyData.map((k) => h('li', {}, k)))
            : null,
          teach.reference_answer
            ? h('p', { class: 'asc-tour-finding-reason' }, teach.reference_answer)
            : null,
          teach.reasoning_divergence
            ? h('p', { class: 'asc-tour-finding-reason' }, teach.reasoning_divergence)
            : null)
      : null;

    const closing = isAdvisor()
      ? 'A physician who finished this would be graded exactly the way you '
        + 'just were, and their next case would be a real one. Nothing from '
        + 'this run was recorded.'
      : passed
        ? (opts.replay ? 'Practice case: nothing was recorded.'
                       : 'Nothing from this case is recorded or sold. Your real cases start now.')
        : 'Nothing from this case is recorded. Take it again when you are ready: '
          + 'there is no limit on attempts.';

    primaryBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', type: 'button',
      onClick: () => {
        if (!primaryEnabled()) return;
        if (!passed && !isAdvisor()) { startTutorial({ replay: true }); return; }
        state.portalChosen = false; state.specialtyChosen = false;
        // Onboarding v2 §6 stop 3: the practice case is a STOP, so passing it
        // hands control back to the walkthrough rather than dropping the
        // physician on a dashboard halfway through being introduced to the
        // product. Only on a PASS: a failed attempt has not finished the stop,
        // and resuming would move them past the one thing they still owe.
        if (firstRunTourPending()) { resumeFirstRun(); return; }
        renderDashboardView();
      } },
      isAdvisor() ? 'Back to the dashboard'
        : !passed ? 'Take it again'
          : firstRunTourPending() ? 'Keep going →' : 'Start real cases →');

    const card = h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-tour-chrome' },
        'CALIBRATION CASE 1 · ' + (passed ? 'PASSED' : 'NOT YET')),
      h('h2', { class: 'asc-tour-headline' }, result.headline),
      h('div', { class: 'asc-tour-findings' }, rows),
      planted ? h('div', { class: 'asc-tour-planted' },
        h('div', { class: 'asc-tour-planted-title' },
          planted.matched ? 'You caught the one most physicians miss' : 'The one most physicians miss'),
        h('div', { class: 'asc-tour-finding-reason' }, planted.reason)) : null,
      teachingCard,
      h('p', { class: 'asc-help', style: 'margin:14px 0 0' }, closing),
      h('div', { style: 'display:flex;gap:10px;margin-top:18px;align-items:center' },
        primaryBtn,
        h('button', { class: 'asc-btn asc-btn-ghost', type: 'button', onClick: openInstructionDrawer },
          'Open the instructions')));
    syncPrimary();
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
