/* ═══════════════════════════════════════════════════════════════════════════
   Asclepius — Expert Evaluation Portal (vanilla SPA)
   Standalone Asclepius JWT auth. No frameworks, no build step.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const API_BASE = '/api/asclepius';
  const TOKEN_KEY = 'asclepius_token';
  const DRAFT_PREFIX = 'asclepius_draft_';
  // Contributor's chosen evaluator experience: 'v1' classic | 'v2' assisted |
  // 'v3' seamless (the recommended default). Persisted per browser; the default
  // for every new task.
  const PORTAL_VERSION_KEY = 'asclepius_portal_version';
  const DEFAULT_PORTAL_VERSION = 'v3';
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
    view: 'eval',          // 'eval' | 'admin'
    adminTab: 'tasks',     // tasks | buyers | exports | metrics
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
  const root = () => document.getElementById('ascRoot');
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function setRoot(node) { const r = root(); clear(r); r.appendChild(node); }

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
      throw { status: 0, detail: 'Network error — is the backend running?', message: 'Network error' };
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
      throw { status: res.status, detail, message: detailToMessage(detail, res.status) };
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

    const badge = document.getElementById('ascUserBadge');
    clear(badge);
    badge.appendChild(h('span', { class: 'asc-user-email' }, state.user.email));
    badge.appendChild(h('span', { class: 'asc-user-role' },
      state.user.role.replace('_', ' ') + (state.user.specialty ? ' · ' + state.user.specialty : '')));

    document.getElementById('ascLogoutBtn').onclick = logout;
  }

  function switchView(view) {
    if (view === 'admin' && state.view !== 'admin') saveDraft();
    state.view = view;
    renderHeader();
    if (view === 'eval') renderEvalView();
    else renderAdminView();
  }

  // ─── Auth / bootstrap ────────────────────────────────────────────────────--
  async function boot() {
    // 1) Resume an existing Asclepius session if the stored token is still valid.
    if (state.token) {
      try {
        // noAuthHandler: a stale/expired token must NOT short-circuit to the
        // "session expired" screen — we want to fall through to SSO below.
        state.user = await api('/auth/me', { noAuthHandler: true });
        await loadTaxonomy();
        renderHeader();
        enterApp();
        return;
      } catch (e) {
        // Stale token (or transient): drop it and try the seamless paths.
        state.token = null;
        try { localStorage.removeItem(TOKEN_KEY); } catch (_) { /* ignore */ }
      }
    }
    // 2) Already signed into the doctor portal? Exchange that session for an
    //    Asclepius one (SSO) — no second login barrier. Skipped right after an
    //    explicit sign-out so the user can choose to sign in with their
    //    onboarding (workspace) credentials instead of being pulled back into
    //    the doctor-portal identity.
    let suppressSso = false;
    try { suppressSso = localStorage.getItem(SUPPRESS_SSO_KEY) === '1'; } catch (_) { suppressSso = false; }
    if (!suppressSso && await trySsoLogin()) return;
    // 3) Otherwise, fall back to the manual login form.
    renderLogin();
  }

  // Silent SSO: trade a doctor-portal token for an Asclepius session. Uses a raw
  // fetch (not api()) so a rejected probe doesn't trip the 401 session handler —
  // an unknown/expired doctor token just means "fall back to the login form".
  async function trySsoLogin() {
    let doctorToken = '';
    try { doctorToken = localStorage.getItem(DOCTOR_TOKEN_KEY) || ''; } catch (e) { doctorToken = ''; }
    if (!doctorToken) return false;
    let res;
    try {
      res = await fetch(API_BASE + '/auth/sso', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: doctorToken }),
      });
    } catch (e) {
      return false; // network error — let renderLogin() show the form
    }
    if (!res.ok) return false; // 401 (bad token) / 403 (no evaluator account)
    let data = null;
    try { data = await res.json(); } catch (e) { return false; }
    if (!data || !data.token) return false;
    state.token = data.token;
    state.user = data.user;
    try { localStorage.setItem(TOKEN_KEY, data.token); } catch (e) { /* ignore quota */ }
    try { localStorage.removeItem(SUPPRESS_SSO_KEY); } catch (_) { /* ignore */ }
    try {
      await loadTaxonomy();
    } catch (e) {
      return false;
    }
    renderHeader();
    enterApp();
    return true;
  }

  async function loadTaxonomy() {
    if (state.taxonomy) return state.taxonomy;
    state.taxonomy = await api('/taxonomy');
    return state.taxonomy;
  }

  function enterApp() {
    const isAdmin = state.user.role === 'admin' || state.user.role === 'qa_reviewer';
    state.view = 'eval';
    renderHeader();
    if (isAdmin) renderEvalView();
    else renderEvalView();
  }

  function logout() {
    state.token = null;
    state.user = null;
    localStorage.removeItem(TOKEN_KEY);
    // Suppress the silent doctor-portal SSO on the next boot so signing out
    // actually lands on the sign-in form (otherwise an active doctor session
    // would re-exchange straight back in, trapping the user on that identity).
    try { localStorage.setItem(SUPPRESS_SSO_KEY, '1'); } catch (_) { /* ignore */ }
    stopTimer();
    renderHeader();
    renderLogin();
  }

  // ─── Login screen ────────────────────────────────────────────────────────--
  function renderLogin(errorMsg) {
    document.getElementById('ascHeader').setAttribute('hidden', '');
    // Accepts an email OR a username/id (e.g. the `mockadmin` sandbox login), so
    // it's a plain text field, not type=email (which would block a username).
    const emailInput = h('input', { class: 'asc-input', type: 'text', placeholder: 'you@hospital.org or username', autocomplete: 'username', required: 'required' });
    const pwInput = h('input', { class: 'asc-input', type: 'password', placeholder: '••••••••', autocomplete: 'current-password', required: 'required' });
    const errBox = h('div', { class: 'asc-login-error', hidden: !errorMsg }, errorMsg || '');
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
          // The user made an explicit identity choice — allow SSO again later.
          try { localStorage.removeItem(SUPPRESS_SSO_KEY); } catch (_) { /* ignore */ }
          await loadTaxonomy();
          renderHeader();
          enterApp();
        } catch (err) {
          errBox.textContent = err.message || 'Sign in failed';
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

    const body = h('div', { class: 'asc-login-body' },
      form,
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
          if (!(await trySsoLogin())) renderLogin('Could not resume your clinical session — sign in above.');
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

  // ═══════════════════════════════════════════════════════════════════════════
  //  EVALUATOR WORKSPACE
  // ═══════════════════════════════════════════════════════════════════════════
  async function renderEvalView() {
    // Home page: the evaluator picks their experience (V3 seamless — the
    // recommended default — / V2 assisted / V1 classic) before any labeling. Shown
    // on entry until a choice is made this session (and again on "Change experience").
    if (!state.portalChosen) { renderVersionHome(); return; }
    const wrap = h('div', { class: 'asc-wrap' });
    wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), 'Loading next evaluation…')));
    setRoot(wrap);
    try {
      // Declare the active flow so the server applies it: V3 serves the hard-case
      // queue (difficulty=hard only) with value-aware routing; V2 value-aware;
      // V1 classic. WITHOUT this param the server safely falls back to the classic
      // oldest-first queue — i.e. the whole V3/V2 serving path is dead unless the
      // client sends its selected version here.
      const data = await api('/tasks/next?portal_version=' + encodeURIComponent(getPortalVersion()));
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
    updateHeaderProgress(); // no open task — the §16 bar hides here
    setRoot(h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'asc-empty' },
          h('div', { class: 'asc-empty-icon' }, '✓'),
          h('h3', {}, 'Your queue is clear'),
          h('p', {}, 'No evaluation tasks are waiting for you right now. Check back soon.'),
          h('div', { style: 'margin-top:16px' },
            h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', onClick: renderEvalView }, 'Refresh queue')),
        ))));
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
      reasoning_steps: [],
      // §1 substage machine (Evaluation UX Overhaul) — V3/V4 only. ``substage``
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
    if (draft.rubricSeeded === undefined) draft.rubricSeeded = false;
    if (!draft.stage) draft.stage = 'prompt_review';
    // §1 substage machine backfill (all additive; an in-flight draft resumes at
    // the first incomplete section with its data prefilled — nothing is lost).
    if (draft.substage === undefined) draft.substage = null;
    ['refine_saved', 'why_better_done', 'citations_reviewed', 'critique_done',
      'from_scratch_saved', 'reasoning_done', 'rubric_done', 'confidence_set']
      .forEach((k) => { if (draft[k] === undefined) draft[k] = false; });
    if (draft.rubricCursor === undefined) draft.rubricCursor = 0;
    if (draft.rubricSeedHash === undefined) draft.rubricSeedHash = null;
    // §13: per-step free-text "what's off" (step_note) — backfill older steps.
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
    if (lo === '' && hi === '') return '—';
    return lo + '–' + hi;
  }

  // The tabbed case panel. Tabs are built only for sections that carry data, so a
  // labs-only case doesn't show empty Meds/Vitals tabs. State (active tab) lives
  // on ``state._caseTab`` keyed by task so it survives re-renders within a task.
  function renderCasePanel() {
    const c = multimodalCase();
    if (!c) return null;
    const demo = c.demographics || {};
    const who = [demo.sex, demo.age_band ? ('age ' + demo.age_band) : null].filter(Boolean).join(', ');

    const tabs = [];
    // Patient overview (always present).
    const overview = h('div', { class: 'asc-case-body' },
      h('div', { class: 'asc-case-patient' }, who ? ('Patient: ' + who) : 'Patient (de-identified)'),
      (c.problem_list && c.problem_list.length)
        ? h('div', { class: 'asc-case-sub' }, 'Active problems: ' + c.problem_list.map((p) => p.condition + (p.since ? ' (since ' + p.since + ')' : '')).join('; '))
        : null,
      h('div', { class: 'asc-case-note-meta' }, 'De-identified · relative dates · no imaging'));
    tabs.push({ key: 'overview', label: 'Patient', body: overview });

    if (c.lab_panels && c.lab_panels.length) {
      tabs.push({ key: 'labs', label: 'Labs', body: h('div', { class: 'asc-case-body' }, renderLabsTrend(c.lab_panels)) });
    }
    if (c.notes && c.notes.length) {
      tabs.push({ key: 'notes', label: 'EHR' + (c.notes.length > 1 ? ' (' + c.notes.length + ')' : ''),
        body: h('div', { class: 'asc-case-body' }, ...c.notes.map((n) => h('div', { class: 'asc-case-note' },
          h('div', { class: 'asc-case-note-meta' }, '[' + (n.note_type || 'Note') + ' — ' + (n.author_role || 'clinician') + ']'),
          h('div', { class: 'asc-case-note-text' }, (n.text || '').trim())))) });
    }
    const meds = c.medications || [];
    if (meds.length) {
      tabs.push({ key: 'meds', label: 'Meds', body: h('div', { class: 'asc-case-body' },
        h('ul', { class: 'asc-case-list' }, ...meds.map((m) => h('li', {},
          [m.drug, m.dose, m.route, m.freq].filter(Boolean).join(' '))))) });
    }
    const vitals = c.vitals || {};
    const vkeys = Object.keys(vitals).filter((k) => vitals[k] !== null && vitals[k] !== undefined && vitals[k] !== '');
    if (vkeys.length) {
      tabs.push({ key: 'vitals', label: 'Vitals', body: h('div', { class: 'asc-case-body' },
        h('div', { class: 'asc-case-vitals' }, ...vkeys.map((k) => h('span', { class: 'asc-vital' },
          h('span', { class: 'asc-vital-k' }, k), ' ', h('span', { class: 'asc-vital-v' }, String(vitals[k])))))) });
    }

    const tid = state.task && state.task.task_id;
    if (!state._caseTab || state._caseTabTask !== tid) { state._caseTab = tabs[0].key; state._caseTabTask = tid; }
    if (!tabs.some((t) => t.key === state._caseTab)) state._caseTab = tabs[0].key;

    const bodyHost = h('div', { class: 'asc-case-host' });
    const tabRow = h('div', { class: 'asc-case-tabs', role: 'tablist' });
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

    const panel = h('div', { class: 'asc-card asc-case-card' },
      h('div', { class: 'asc-case-head' },
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
      // §13 (V3/V4): a corrected step's "why" is the free-text step_note — the
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
  // On V3/V4, a captured rubric must name at least one CRITICAL negative — the one
  // thing a correct answer must never do. An empty rubric is allowed (optional); the
  // gate fires only once criteria exist. Scoped to isV3() (v3+v4), so V1/V2 are
  // unaffected.
  function rubricGate() {
    if (!isV3()) return { ok: true };
    const rubric = (state.draft.rubric || []).filter((c) => (c.text || '').trim());
    if (!rubric.length) return { ok: true };
    if (hasCriticalNegative(rubric)) return { ok: true };
    return { ok: false, msg: 'mark one critical “never” criterion (−8 to −10) to continue' };
  }

  // ─── Rubric Rigor (§C) + Model-Failure Taxonomy (§D) — V3/V4 only ──────────
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

  // FIX-4 completeness/premium — mirrors backend rubric.rubric_completeness exactly.
  const _PREMIUM_MIN_CRITERIA = 5;
  const _PREMIUM_MIN_AXES = 3;
  // FIX-7 (axis-coverage nudge): the three axes a defensible grader almost always
  // touches. Mirrors backend constants.RUBRIC_CORE_AXES exactly. ADVISORY only —
  // never affects premium/missing.
  const _RUBRIC_CORE_AXES = ['safety', 'accuracy', 'reasoning'];
  function rubricCompleteness(criteria) {
    const crit = (criteria || []).filter((c) => (c.text || '').trim());
    const n = crit.length;
    const nPos = crit.filter((c) => (Number(c.points) || 0) > 0).length;
    const nNeg = crit.filter((c) => (Number(c.points) || 0) < 0).length;
    const axes = new Set(crit.map((c) => c.axis).filter(Boolean));
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
    // FIX-7: advisory core-axis coverage — kept OUT of `missing` so it never gates premium.
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

  // ─── §1 Substage machine (Evaluation UX Overhaul — V3/V4 only) ──────────────
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
      // progress total is stable — picking a verdict must never make the bar
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
    return stepsReview().ok;
  }

  // Explicit completion per substage: the *_done flag (the Save/Continue click)
  // AND the section's data conditions — so deleting data from a re-opened
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

  // Reset every downstream completion when the verdict/chosen side changes —
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

  // One save + re-render + submit-state pass — every section's Save/Continue
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

  // ─── §16 Task progress — ONE source of truth ────────────────────────────────
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
    // Submit is the last increment — the bar reads 100% only while submitting.
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
    const active = !!(state.user && state.view === 'eval' && state.portalChosen
      && state.task && state.draft && state.draft.stage && isV3());
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

  // ─── §2 Info-dot — the reusable "?" explainer ───────────────────────────────
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
  // info dot — every §1 section mounts through this.
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
    // duplicate id — only the first match would update); stages 1–2 host it here.
    // V3/V4: the submit bar is DEFERRED to the confidence substage (§15), and its
    // V3 variant carries no timer — this header owns #ascTimer in every stage.
    const timer = (d.stage === 'compare' && !isV3())
      ? null
      : h('span', { class: 'asc-timer', id: 'ascTimer' }, formatTime(getElapsed()));
    // §16: the step counter reads from taskProgress() — the same single source
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
      // Shown LOCKED unless the contributor is real_data_approved — serving is
      // enforced server-side regardless; the lock is honest UI, not the gate.
      v: 'v4', label: 'Real · De-identified Cases', tag: 'Real patient data', dot: 'asc-dot-pink',
      requiresRealData: true,
      blurb: 'Work through real, de-identified patient cases — labs, notes, and a real clinical snapshot. Same task as synthetic; the data is real.',
      bullets: [
        'Read a real de-identified case — labs, notes, meds, vitals',
        'Give a 10-second first impression before you see the AI answers',
        'Pick the better of two AI answers, refine it, and say why',
        'A single point-in-time (static) case — not a timeline',
        'Requires real-data approval (BAA / training)',
      ],
    },
    {
      v: 'v3', label: 'Synthetic Multimodal Cases', tag: 'Recommended', dot: 'asc-dot-lime',
      blurb: 'Structured synthetic cases — labs, EHR notes, and meds — built to be hard.',
      bullets: [
        'Read a multimodal case — labs, EHR notes, meds, vitals',
        'Give a 10-second first impression before the AI answers appear',
        'Compare two AI answers and pick the stronger one',
        'Refine it, flag the weaker one’s errors, and check the reasoning',
      ],
    },
    {
      v: 'v5', label: 'Real Longitudinal Cases', tag: 'Coming soon', dot: 'asc-dot-faint',
      comingSoon: true,
      blurb: 'Follow one real patient across time — multiple visits, evolving labs, and what actually happened next.',
      bullets: [
        'A real patient followed across multiple timepoints',
        'Reason about how the case evolves, not a single snapshot',
        'Outcomes linked past the decision',
      ],
    },
  ];
  // The legacy flows — content untouched, tucked behind "Other flows".
  const LEGACY_VERSION_OPTS = [
    {
      v: 'v2', label: 'V2 · Assisted', tag: null, dot: 'asc-dot-orange',
      blurb: 'The assisted flow — under 10 minutes per task.',
      bullets: [
        'A 30-second quick take before you see the answers',
        'Model-suggested labels you verify (never auto-applied)',
        'Side-by-side answer diff — read only what differs',
        'Voice dictation on every field',
      ],
    },
    {
      v: 'v1', label: 'V1 · Classic', tag: null, dot: 'asc-dot-faint',
      blurb: 'The original flow — write your full ideal answer.',
      bullets: [
        'Write your complete ideal answer before reveal',
        'No AI suggestions — your judgment only',
        'Full-text answer comparison',
      ],
    },
  ];
  function chooseVersion(v) {
    setPortalVersion(v);
    state.portalChosen = true;
    renderEvalView();
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
      onClick: inert ? null : () => chooseVersion(o.v),
      onKeydown: inert ? null : (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); chooseVersion(o.v); }
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
    updateHeaderProgress(); // no open task — the §16 bar hides here
    const last = getPortalVersion();
    const approved = !!(state.user && state.user.real_data_approved);
    const cards = h('div', { class: 'asc-ver-cards' });
    VERSION_OPTS.forEach((o) => cards.appendChild(versionCard(o, last, approved)));

    // Legacy flows, folded away — exactly as they were, one click deeper.
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
          'Same clinical judgment, same training data — pick how you want to work.'),
        cards,
        legacyToggle,
        legacyCards)));
  }

  // Small read-only indicator inside the workspace: which experience this task
  // is being graded under, with a one-tap route back to the home chooser.
  function renderExperienceBadge() {
    const v = draftVersion();
    const meta = { v4: ['', 'Real · De-identified Cases'], v3: ['', 'Synthetic Multimodal'], v2: ['', 'V2 · Assisted'], v1: ['', 'V1 · Classic'] }[v] || ['', 'V1 · Classic'];
    return h('div', { class: 'asc-exp-badge' },
      h('span', { class: 'asc-exp-badge-label' }, meta[0] + meta[1]),
      h('button', {
        class: 'asc-btn-link', type: 'button',
        onClick: () => { state.portalChosen = false; renderEvalView(); },
      }, 'Change experience'));
  }

  // ─── §6 semantic case-tag chips (V3/V4) ─────────────────────────────────────
  // Consistent, meaningful color from the console palette (no blue — it left
  // with the design-system migration): a stable hue per specialty, semantic
  // difficulty (hard=pink, medium=orange, easy=green), lime=multimodal,
  // orange=reasoning (model), pink=grounding (attention). Color always pairs
  // with the text label — never the sole carrier.
  const SPECIALTY_DOT = { nephrology: 'asc-dot-green', cardiology: 'asc-dot-orange' };
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
    // rendered prompt) — no duplicated wall of serialized case text.
    const promptText = caseObj ? caseQuestion(task.prompt) : (task.prompt || '');
    const diff = (task.difficulty || 'medium');
    // §6: V3/V4 get the semantic dot chips; V1/V2 keep the muted badges as-is.
    const metaRow = isV3()
      ? h('div', { class: 'asc-meta-row' },
          metaChip(task.specialty || 'general', specialtyDot(task.specialty),
            'Specialty — same specialty, same color, always'),
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

    const wrap = h('div', { class: 'asc-wrap' }, renderExperienceBadge(), promptCard, renderCasePanel(), groundingBanner);

    if (d.stage === 'prompt_review') {
      wrap.appendChild(stageHeader('Review the prompt'));
      wrap.appendChild(renderPromptGate());
      wrap.appendChild(blurredPlaceholder('The AI answers stay hidden until you confirm the prompt is clinically valid.'));
      setRoot(wrap);
    } else if (d.stage === 'independent_answer') {
      wrap.appendChild(stageHeader('Write your answer'));
      wrap.appendChild(renderIndependentAnswer());
      wrap.appendChild(blurredPlaceholder('Write your ideal answer first — then reveal the AI answers to compare.'));
      setRoot(wrap);
    } else {
      wrap.appendChild(stageHeader('Compare & grade'));
      // §17: the linear flow scrolls a lot — keep the case one tap away with a
      // slim sticky strip (question + "View case") for the whole compare stage.
      if (isV3()) wrap.appendChild(renderCaseSticky(promptText));
      renderCompareStage(wrap);
      setRoot(wrap);
      refreshAnswerHighlight();
      renderRationale();
      updateSubmitState();
      loadAssist(); // fire-and-forget: suggestions appear when ready (Speed Opt §2)
    }
    updateHeaderProgress();
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
    function closeOverlay() { overlay.remove(); document.removeEventListener('keydown', onKey, true); }
    document.addEventListener('keydown', onKey, true);
    const panel = renderCasePanel();
    const popup = h('div', {
      class: 'call-team-popup asc-case-popup', onClick: (e) => e.stopPropagation(),
      style: 'max-width:860px;max-height:88vh;overflow:auto;text-align:left',
    },
      h('div', { class: 'call-team-title' }, 'Case reference'),
      h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'Clinical question'),
        h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, questionText || state.task.prompt || '—')),
      panel || h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, state.task.prompt || '—'),
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
    if (!isAssisted()) return;  // model assist is an assisted-flow (V2 + V3) feature
    // V3 anti-rubber-stamp guard (Seamless PRD WS1): AI suggestions are hidden
    // until the clinician commits their OWN verdict. We don't merely hide them —
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
      // The LLM call can take seconds — the doctor may already be on another
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
  // Surface freshly-arrived suggestions (BUG-4 — decoupled from the diff):
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
    // mark — otherwise the already-painted diff is unchanged, so leave it be.
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
    const a = assistData();
    if (!a) return;
    el.appendChild(h('span', { class: 'asc-assist-chip', 'aria-hidden': 'true' }));
    el.appendChild(h('span', {},
      'Model thinks ', h('strong', {}, a.suggested_weaker), ' is weaker — tap a verdict to decide.'));
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
    // cards — offer a reload instead of letting the doctor grade nothing.
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

    // Diff view (Speed Optimization §3) — assisted flows (V2 + V3). V1 (classic)
    // shows the full answer text with no diff toggle, exactly as the original.
    const assisted = isAssisted();
    const diffToggle = assisted ? h('button', {
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
      assisted ? h('p', { class: 'asc-help', style: 'margin:2px 0 14px' },
        'Shared text is dimmed; passages where the answers diverge are highlighted.') : null,
      answers));
    wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:14px' }, 'Your verdict',
        h('span', { class: 'asc-label-hint', style: 'font-weight:500;margin-left:6px' }, '(press 1 / 2 / 3)')),
      verdicts,
      assistHint,
      rationale));
    // §15 (the single biggest structural gotcha): on V3/V4 the submit bar — and
    // the confidence pills inside it — must NOT mount at compare entry. It mounts
    // from the §1 substage machine, only when the flow reaches `confidence`.
    // V1/V2 keep the eager submit bar exactly as before.
    if (!isV3()) wrap.appendChild(h('div', { class: 'asc-card' }, renderSubmitBar()));
    if (assisted) setTimeout(renderAssistHint, 0);
  }

  // ─── Sentence-level diff (Speed Optimization §3, dependency-free) ───────────
  // Character-exact split: concatenating the result reproduces the input, so
  // the rendered answer never differs from the real candidate text. A '.'
  // between two digits is a decimal (e.g. "K+ 1.0"), NOT a boundary — otherwise
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
  // Pure A/B divergence diff (§3.1) — no app state, unit-testable.
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
    // Candidate texts are immutable for the life of a task — memoize the LCS
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
  // original chosen answer highlighted (Seamless PRD WS4 — "see what you changed").
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
    // V1 (classic) renders plain full text — no diff, no error-span marks. The
    // assisted flows (V2 + V3) get the marked A/B diff.
    const diff = (!isAssisted() || state.showFullText) ? null : computeAnswerDiff();
    const a = assistData();
    // WS5 (V3): brighter, unmissable divergence marking + a one-line legend so the
    // doctor adjudicates the deltas, not the boilerplate. V2 keeps its subtler diff.
    container.classList.toggle('asc-answers-v3diff', !!(diff && isV3()));
    if (diff && isV3()) {
      container.appendChild(h('div', { class: 'asc-diff-legend' },
        h('span', { class: 'asc-diff-legend-mark' }, '⬍'),
        diff.allDivergent
          ? ' These answers share no text — they diverge throughout, so both are highlighted in full.'
          : ' Bright passages are where A and B differ — shared text is dimmed.'));
    }
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
    // into the same free-text reason — one flag, one reason, one destination).
    if (isV3()) return renderPromptGateV3();
    const d = state.draft;
    const reasonBox = h('div', { id: 'ascFlagReason', hidden: true });
    const reasonInput = h('input', { class: 'asc-input', placeholder: 'One line — why is this prompt invalid? (e.g. ambiguous, not clinically meaningful, unsafe premise)', value: d.prompt_review.note || '' });
    reasonInput.addEventListener('input', () => { d.prompt_review.note = reasonInput.value; saveDraft(); });
    // One confirm button dispatches by the reason box's mode, so the flag and the
    // (multimodal-only) case-incoherent paths share the same reason input without
    // double-binding a handler.
    const confirmFlag = h('button', { class: 'asc-btn asc-btn-danger', onClick: () => {
      if (reasonBox.getAttribute('data-mode') === 'case_incoherent') flagCaseIncoherent();
      else flagPrompt();
    } }, 'Confirm — flag & skip');
    reasonBox.appendChild(h('div', { class: 'asc-field', style: 'margin-top:14px' },
      h('label', { class: 'asc-label' }, 'Reason for flagging'),
      reasonInput,
      h('div', { style: 'margin-top:10px' }, confirmFlag)));

    const isCase = !!multimodalCase();
    // Multimodal (Multimodal PRD §5): a clinician can flag a case whose labs /
    // notes / problems / meds are internally inconsistent — the human counterpart
    // to the case-judge coherence gate. Routes the case out (0 records) and feeds
    // back to recalibrate case generation.
    const incoherentBtn = isCase
      ? h('button', { class: 'asc-btn asc-btn-ghost', onClick: () => {
          reasonInput.placeholder = 'One line — what doesn’t add up? (e.g. the sodium contradicts the note)';
          reasonBox.hidden = false;
          reasonBox.setAttribute('data-mode', 'case_incoherent');
          confirmFlag.textContent = 'Confirm — case is inconsistent & skip';
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
          reasonInput.placeholder = 'One line — why is this invalid? (e.g. ambiguous, not clinically meaningful, unsafe premise)';
          reasonBox.hidden = false;
          reasonBox.removeAttribute('data-mode');
          confirmFlag.textContent = 'Confirm — flag & skip';
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
      class: 'asc-btn asc-btn-danger', type: 'button', disabled: true,
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
        + (isCase ? 'case' : 'question') + ', continue — flagged '
        + (isCase ? 'cases' : 'prompts') + ' leave your queue and go to admin review with your reason.'),
      h('div', { class: 'asc-gate-actions' },
        h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', onClick: validatePrompt },
          'Looks clinically valid — continue →'),
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
      toast('Prompt flagged for review — loading the next task', 'success');
      renderEvalView();
    } catch (e) {
      if (e.status !== 401) toast('Could not flag the prompt: ' + e.message, 'error');
    } finally {
      state.submitting = false;
    }
  }

  // Multimodal case flagged internally inconsistent (Multimodal PRD §5) — mirrors
  // flagPrompt but stamps the case_incoherent verdict; the server routes the case
  // out (0 records) and feeds the signal back to case-generation recalibration.
  async function flagCaseIncoherent() {
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
      toast('Case flagged as inconsistent — loading the next task', 'success');
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
      } catch (e) { toast('Microphone unavailable — check browser permissions.', 'error'); return; }
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
            // Provider succeeded but heard nothing — tell the doctor rather than
            // silently doing nothing (the reported "mic opens, nothing happens").
            toast('No speech detected — tap the mic and try again.', 'info');
          }
        } catch (e) {
          if (e.status === 503) toast('Dictation is not configured — type instead (or use the Wispr Flow app).', 'info');
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
      btn.setAttribute('aria-label', 'Listening — tap to stop dictation');
      btn.title = 'Listening… tap to stop';
    });
    return btn;
  }

  // Wrap a textarea/input with its mic in one row. setVal writes the transcript
  // to the field AND fires its input handler so the draft stays in sync, then
  // focuses the field with the cursor at the end so the inserted text is visible
  // and immediately editable (the fix for "mic opens but text isn't written" —
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
  // All are the anti-anchoring guard — committed BEFORE the A/B answers are
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
            placeholder: 'Your quick take — key points you\'d expect (bullets are fine). e.g. continue reduced-dose metformin · recheck eGFR 3 mo · watch for lactic acidosis.' }, ia.text || '');
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
        instinctMode ? 'Quick gut check — in one line, what\'s the crux of the right answer?'
          : fullMode ? 'Before you see the AI answers, write your ideal answer'
          : 'Before you see the answers — your quick take'),
      h('p', { class: 'asc-help', style: 'margin-bottom:16px' },
        instinctMode
          ? '~10 seconds. This commits your instinct before the A/B answers can anchor you — your refined chosen answer later is the gold.'
          : fullMode
            ? 'This is captured uncontaminated — your own gold answer, before the A/B answers can anchor your judgment.'
            : 'A few key points, captured before the A/B answers can anchor your judgment. 30–45 seconds is plenty — your refined chosen answer later is the gold answer.'),
      h('div', { class: 'asc-field' }, withMic(field), counter),
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
  // text under withholding — the server records the independent answer as
  // pre-reveal and treats it as authoritative at packaging.
  async function revealAnswers() {
    const ia = state.draft.independent_answer;
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
    // suggested-weaker answer — and never in full-text mode (nothing decorated).
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
      // §1: sections completed against the previous answer must not survive it —
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
    // committed a verdict — now that one exists, fetch + reveal them for the
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
  // V3/V4 (§1): a substage-gated renderer — one active section at a time,
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

  // One-line summary chip for a completed section (§1) — keeps context without
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
        }, '✓ Done — collapse')));
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
            'Pick every reason that applies — accuracy, completeness, safety, reasoning.',
          ])),
        chips),
      sectionActions(hint, contBtn));
  }

  // ─── §11 Citations (reviewed, not blocking — unless grounding is required) ──
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
      hint.textContent = ok ? '' : 'this task requires a citation — attach one to continue';
    };
    const card = sectionCard('citations',
      infoDot('Citations', [
        'Cite the guideline or trial your judgment rests on.',
        'Search the library, open the source to check it, then it’s attached.',
      ]),
      h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
        required
          ? 'This task requires evidence — attach the source your judgment rests on.'
          : 'Not every case needs one — open a suggested source to attach it, or continue.'),
      cite,
      anchorBlock,
      sectionActions(hint, contBtn));
    card.addEventListener('input', () => setTimeout(sync, 0));
    card.addEventListener('click', () => setTimeout(sync, 0));
    setTimeout(sync, 0);
    return card;
  }

  // ─── §12 Critique the rejected answer — popover-per-tag, no model hints ─────
  function closeTagPopover() {
    if (state._tagPop) {
      state._tagPop.remove();
      state._tagPop = null;
    }
    // Always detach the Esc handler — a re-render can tear the popover out of
    // the DOM without going through here, leaving only the listener behind.
    document.removeEventListener('keydown', _tagPopKey, true);
  }
  function _tagPopKey(e) { if (e.key === 'Escape') closeTagPopover(); }

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
      const pop = h('div', { class: 'asc-tag-pop', role: 'dialog', 'aria-label': 'Detail this error' });
      pop.appendChild(h('div', { class: 'asc-tag-pop-title' }, tag.replace(/_/g, ' ')));
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
      const doneBtn = h('button', {
        class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button',
        disabled: !crit.severities[tag],
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
            doneBtn.disabled = false;
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
      // Anchor under the tapped chip, clamped to the row.
      pop.style.left = Math.max(0, Math.min(chipEl.offsetLeft, Math.max(0, chipsRow.offsetWidth - 330))) + 'px';
      pop.style.top = (chipEl.offsetTop + chipEl.offsetHeight + 6) + 'px';
      chipsRow.appendChild(pop);
      state._tagPop = pop;
      document.addEventListener('keydown', _tagPopKey, true);
    }

    function paintChips() {
      // A popover from a previous render is detached — drop the stale handle
      // (insertBefore with a non-child reference node would throw).
      if (state._tagPop && state._tagPop.parentNode !== chipsRow) state._tagPop = null;
      Array.from(chipsRow.children).forEach((c) => { if (c !== state._tagPop) c.remove(); });
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
        chipsRow.insertBefore(chip, state._tagPop || null);
      });
    }
    paintChips();

    const whyWorse = h('input', {
      class: 'asc-input',
      placeholder: 'One line on the key problem…',
      value: crit.why_worse || '',
    });
    whyWorse.addEventListener('input', () => { crit.why_worse = whyWorse.value; saveDraft(); sync(); });

    // §D failure-mode taxonomy chips (baseline pairs) — physician-picked, kept.
    const failureField = h('div', {});
    if (isBaselinePair() && (state.taxonomy.failure_modes || []).length) {
      const fmContainer = h('div', { id: 'ascFailureModes' });
      failureField.appendChild(h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'How did it fail? ',
          h('span', { class: 'asc-label-hint' }, '(model-failure taxonomy — select all that apply)')),
        fmContainer));
      renderFailureTags(fmContainer);
      fmContainer.addEventListener('click', () => setTimeout(sync, 0));
    }

    // Optional per-error citation — lightweight, behind a disclosure (§12).
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
        'Rejected answer: Model ' + d.rejected_id + '. Pick the error tags yourself — no model hints here.'),
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

  // ─── §13 Check the reasoning — one step open at a time, free-text "what's off"
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
    const resplitBtn = canAutoSplit ? h('button', {
      class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
      onClick: () => autoSplitChosen(listId, true),
    }, '↻ Re-split from answer') : null;

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
        'Open any step that’s off and say what’s wrong — we handle the rest.',
      ]),
      h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
        forBoth ? 'Optionally lay out the reasoning steps behind your ideal answer.'
          : 'Confirm each step, or open it and say what’s off — one step at a time.'),
      h('div', { class: 'asc-steps', id: listId }),
      h('div', { style: 'margin-top:12px;display:flex;gap:8px;flex-wrap:wrap' }, addBtn, resplitBtn),
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
    // Never allow Continue while the auto-split is in flight — steps landing
    // after the section completed would silently regress the flow.
    btn.disabled = state.splitting || !sr.ok;
    if (hint) {
      hint.textContent = state.splitting ? 'splitting the answer into steps…'
        : sr.ok ? ''
          : (sr.reasons.indexOf('missing_correction_reason') !== -1
            ? 'say what’s off with each edited step'
            : 'review each step — confirm it, or open it and correct it');
    }
  }

  // V3/V4 steps list (§13): single-open accordion; an edited step captures a
  // free-text ``step_note`` (the server derives the error-tag classification);
  // NO tag picker and NO citation block inside step editing.
  function renderStepsListV3(listId) {
    const list = document.getElementById(listId);
    if (!list) return;
    clear(list);
    const steps = activeSteps();
    if (state.splitting) {
      list.appendChild(h('p', { class: 'asc-help' }, 'Splitting the chosen answer into steps…'));
      return;
    }
    if (!steps.length) {
      list.appendChild(h('p', { class: 'asc-help' }, 'No steps yet — add steps manually' +
        (state.draft.verdict !== 'both_inadequate' ? ', or use “Re-split from answer”.' : '.')));
      syncStepsCont();
      return;
    }

    const statusOf = (s) => {
      if (s.added) return { text: 'added', cls: 'added' };
      if (s.corrected) {
        return (s.step_note || '').trim()
          ? { text: 'corrected ✎', cls: 'corrected' }
          : { text: 'corrected — say what’s off', cls: 'corrected' };
      }
      if (s.confirmed) return { text: 'confirmed ✓', cls: 'confirmed' };
      return { text: 'pending', cls: 'pending' };
    };

    // Bulk confirm for model-passed, untouched steps (kept from the pre-graded
    // flow — reading then one tap is still an explicit endorsement).
    const untouchedGood = steps.filter((s) => s.suggested_label === 'good' && !s.confirmed
      && !s.corrected && !s.added && (s.text || '').trim() === (s.original_text || '').trim());
    if (untouchedGood.length > 1) {
      list.appendChild(h('div', { class: 'asc-step-bulkbar' },
        h('span', { class: 'asc-step-bulk-label' },
          untouchedGood.length + ' steps look correct to the model — read them, then confirm in one tap.'),
        h('button', {
          class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
          onClick: () => {
            untouchedGood.forEach((s) => setStepConfirmed(s, true));
            saveDraft(); renderStepsListV3(listId); updateSubmitState();
          },
        }, '✓ Confirm all correct')));
    }

    steps.forEach((s, idx) => {
      s.step = idx + 1;
      const open = state._openStep === idx;
      const st = statusOf(s);
      const flaggedBadge = (s.suggested_label === 'bad')
        ? h('span', { class: 'asc-step-suggest bad', title: 'Model pre-grade — verify and confirm or correct' }, 'model · flags this')
        : (s.suggested_label === 'good')
          ? h('span', { class: 'asc-step-suggest good', title: 'Model pre-grade — your confirmation is the label' }, 'model · looks correct')
          : null;

      const confirmBtn = h('button', {
        class: 'asc-btn asc-btn-ghost asc-btn-sm asc-step-confirm' + (s.confirmed ? ' active' : ''),
        type: 'button', hidden: s.corrected || s.added,
        onClick: (e) => {
          e.stopPropagation();
          setStepConfirmed(s, !s.confirmed);
          if (s.confirmed && state._openStep === idx) state._openStep = null;
          saveDraft(); renderStepsListV3(listId); updateSubmitState();
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
            class: 'asc-btn-link', type: 'button',
            onClick: () => {
              state._openStep = open ? null : idx;
              renderStepsListV3(listId);
            },
          }, open ? 'Close' : (s.corrected || s.added ? 'Edit' : 'Open'))));

      const row = h('div', {
        class: 'asc-step' + (open ? '' : ' asc-step-collapsed') + (s.confirmed ? ' is-confirmed' : ''),
      }, head);

      if (!open) {
        row.appendChild(h('div', { class: 'asc-step-collapsed-text' }, s.text || ''));
        list.appendChild(row);
        return;
      }

      // Expanded editor: the step text + the single free-text "what's off" box.
      const ta = h('textarea', { class: 'asc-textarea', placeholder: 'Describe this reasoning step…' }, s.text || '');
      const noteWrap = h('div', { class: 'asc-field', style: 'margin-top:8px' });
      const note = h('input', {
        class: 'asc-input',
        placeholder: 'e.g. treats the creatinine bump as intrinsic AKI — it’s decongestion-related hemoconcentration',
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
        const pill = row.querySelector('.asc-step-status');
        const st2 = statusOf(s);
        if (pill) { pill.textContent = st2.text; pill.className = 'asc-step-status ' + st2.cls; }
      });
      noteWrap.appendChild(h('label', { class: 'asc-label' }, 'What’s off with this step?'));
      noteWrap.appendChild(withMic(note));
      noteWrap.hidden = !s.corrected;

      const hasOriginal = s.original_text != null;
      const originalBox = hasOriginal
        ? h('details', { class: 'asc-step-original', hidden: !s.corrected },
            h('summary', {}, 'original: ' + ((s.original_text || '').length > 80
              ? (s.original_text || '').slice(0, 80) + '…' : (s.original_text || ''))),
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
        }
        noteWrap.hidden = !s.corrected;
        if (originalBox) originalBox.hidden = !s.corrected;
        const pill = row.querySelector('.asc-step-status');
        const st2 = statusOf(s);
        if (pill) { pill.textContent = st2.text; pill.className = 'asc-step-status ' + st2.cls; }
        saveDraft(); syncStepsCont(); updateSubmitState();
      });

      const rowActions = h('div', { style: 'margin-top:8px;display:flex;gap:10px' },
        h('button', {
          class: 'asc-btn-link', type: 'button',
          onClick: () => {
            activeSteps().splice(idx + 1, 0, newAuthoredStep());
            state._openStep = idx + 1;
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

      appendChildren(row, [suggestHint, ta, noteWrap, originalBox, rowActions]);
      list.appendChild(row);
    });
    syncStepsCont();
  }

  // ─── §15 Confidence + submit — mounts ONLY at the confidence substage ───────
  function renderConfidenceSection() {
    const d = state.draft;
    const confLevels = (state.taxonomy.confidence_levels || ['low', 'medium', 'high']);
    const confPills = h('div', { class: 'asc-conf-pills', id: 'ascConf' });
    confLevels.forEach((lvl) => {
      confPills.appendChild(h('button', {
        // Unset until the doctor actively picks — the draft default is never
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
    const card = sectionCard('confidence', null,
      confPills,
      h('div', { class: 'asc-submit-row' }, hint, submitBtn));
    setTimeout(updateSubmitState, 0);
    return card;
  }

  // ─── §14 Rubric — build the scoring guide, one criterion at a time ──────────
  // V3/V4 only (the rubric never rendered on V1/V2). Replaces the dense
  // all-criteria list with a focused wizard: one criterion per card, a pinned
  // copy of the revised answer for reference, silent auto-seeding from the
  // doctor's tags, and plain-language weights (numeric bands live in the
  // info-dot, not the primary copy).
  const TIER_DEFAULT_PTS = { critical: 9, important: 5, helpful: 2 };
  const TIER_CHOICES = [
    ['critical', 'Must-have', 'decides correctness on its own'],
    ['important', 'Important', 'a real quality difference'],
    ['helpful', 'Nice-to-have', 'polish — good if present'],
  ];

  // 14.4: auto-growing textarea (min 2 rows) — the full criterion text is always
  // visible and editable; nothing clips.
  function autoGrow(ta) {
    const fit = () => { ta.style.height = 'auto'; ta.style.height = Math.max(ta.scrollHeight, 52) + 'px'; };
    ta.addEventListener('input', fit);
    setTimeout(fit, 0);
    return ta;
  }

  // Repaint the live rubric section after an async seed lands — never while the
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
    // additively in the background. No prompt, no reseed button — ever.
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
      h('summary', {}, 'Your revised answer — reference'),
      h('div', { class: 'asc-rubric-pin-body' }, refText));

    const body = h('div', { id: 'ascRubricWizard' });
    // While a seed request is in flight and nothing exists yet, hold the wizard
    // on a placeholder — never flash the empty finish card (a fast "Save &
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
        'Weights: must-have / important / nice-to-have map to high / medium / low points; a “must-never” auto-fails the answer.',
        'Confirm or edit each drafted criterion, then add your own if something is missing.',
      ]),
      // 14.1: layman's description — no numeric tiers in the primary copy.
      h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
        'List what a correct answer must get right and what it must never do. Each item is weighted by how much it matters. Name at least one “must-never”: the single thing that makes an answer wrong no matter what.'),
      pinned,
      body);
  }

  // 14.3: one focused criterion card — sentence, type, weight, axis, cite.
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
        ? 'Machine-checkable — names a concrete fact/drug/dose/threshold.'
        : 'Name the specific fact, drug, dose, or threshold so the grader can check it.';
    };
    ta.addEventListener('input', () => { c.text = ta.value; paintSpec(); saveDraft(); updateSubmitState(); });

    const signRow = h('div', { class: 'asc-sev-pills' });
    const tierRow = h('div', { class: 'asc-rubric-tier-row' });
    const ptsLabel = h('span', { class: 'asc-rubric-pts' });
    const slider = h('input', { type: 'range', min: '1', max: '10', step: '1', style: 'width:120px' });
    const autoFail = h('span', {
      class: 'asc-badge asc-badge-amber', hidden: true,
      title: 'A “must-never” — the grader hard-fails an answer that does this.',
    }, 'auto-fail ✓');

    const mag = () => Math.max(1, Math.abs(Number(c.points) || 5));
    const neg = () => (Number(c.points) || 0) < 0;
    function paintAll() {
      slider.value = String(mag());
      ptsLabel.textContent = (neg() ? '−' : '+') + mag();
      ptsLabel.className = 'asc-rubric-pts ' + (neg() ? 'neg' : 'pos');
      Array.from(signRow.children).forEach((b) => b.classList.toggle('active', (b.dataset.sign === 'neg') === neg()));
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
    [['pos', 'Must include ✓'], ['neg', 'Must never ✕']].forEach(([sign, label]) => {
      signRow.appendChild(h('button', {
        class: 'asc-sev-pill', type: 'button', dataset: { sign },
        onClick: () => setPoints(mag(), sign === 'neg'),
      }, label));
    });
    TIER_CHOICES.forEach(([tier, label, expl]) => {
      tierRow.appendChild(h('button', {
        class: 'asc-rubric-tier-btn', type: 'button', dataset: { tier }, title: expl,
        onClick: () => setPoints(TIER_DEFAULT_PTS[tier], neg()),
      },
        h('span', { class: 'asc-rubric-tier-name' }, label),
        h('span', { class: 'asc-rubric-tier-expl' }, expl)));
    });
    slider.addEventListener('input', () => setPoints(parseInt(slider.value, 10) || 1, neg()));

    const axes = (state.taxonomy.rubric_axes
      || ['accuracy', 'completeness', 'safety', 'reasoning', 'grounding', 'communication']);
    const axisRow = h('div', { class: 'asc-sev-pills asc-axis-row' });
    axes.forEach((ax) => {
      axisRow.appendChild(h('button', {
        class: 'asc-sev-pill' + ((c.axis || 'accuracy') === ax ? ' active' : ''), type: 'button',
        onClick: (e) => {
          c.axis = ax;
          Array.from(axisRow.children).forEach((b) => b.classList.toggle('active', b === e.currentTarget));
          saveDraft();
        },
      }, ax));
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

    card.appendChild(h('div', { class: 'asc-field' },
      h('label', { class: 'asc-label' }, 'A correct answer… ', specChip),
      ta));
    card.appendChild(h('div', { class: 'asc-field' },
      h('label', { class: 'asc-label' }, 'Type'), signRow));
    card.appendChild(h('div', { class: 'asc-field' },
      h('label', { class: 'asc-label' }, 'How much does it matter? ',
        infoDot('Weights', [
          'Must-have / important / nice-to-have map to high / medium / low points.',
          'A “must never” marked must-have is the auto-fail — the grader hard-fails on it.',
        ])),
      tierRow,
      h('div', { style: 'display:flex;align-items:center;gap:10px;margin-top:8px' },
        slider, ptsLabel, autoFail)));
    card.appendChild(h('div', { class: 'asc-field' },
      h('label', { class: 'asc-label' }, 'Which axis does it score? ',
        infoDot('Axes', [
          'Accuracy = facts right · completeness = nothing missing · safety = no harm · reasoning = sound logic.',
          'Grounding = evidence-backed · communication = clear to the reader.',
        ])),
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
          ? (rc.n_criteria + ' criteria · ' + rc.n_axes + ' axes — meets the premium bar')
          : ('to reach premium: ' + rc.missing.join('; ')))));
    } else {
      card.appendChild(h('p', { class: 'asc-help' },
        'Add your own criteria below, or finish without a scoring guide.'));
    }
    const addBtn = h('button', {
      class: 'asc-btn asc-btn-subtle', type: 'button',
      onClick: () => {
        d.rubric.push({ text: '', points: 5, axis: 'accuracy', source: 'manual' });
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

  // 14.5: seeding is SILENT and automatic — invoked on rubric mount and again
  // (additively) whenever the doctor's tags change. Never prompts, never asks.
  async function seedRubric(force) {
    const d = state.draft;
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
      // Seeding is a convenience — never surface an error; the placeholder
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

    // V3 (WS3): auto-suggested citation for this rationale — retrieval keys on
    // the refined answer + the "why it's better" note. Confirming re-renders so
    // the anchor block fills and the record reads as grounded.
    const cite = renderCiteSuggest(
      rev.evidence_anchor,
      () => ((rev.revised_text != null ? rev.revised_text : original) + ' ' + (rev.why_better_notes || '')),
      renderRationale);
    wireCiteSuggest(notes, cite);
    wireCiteSuggest(ta, cite);

    return h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head chosen' }, '✓ Chosen answer (' + d.chosen_id + ') — edit to improve'),
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
    // for a REAL-MODEL (baseline) pair — the taxonomy attributes provider failures, so
    // it's meaningless on a generated pair. Physician-verified; multi-select, ~10s.
    const failureField = h('div', {});
    if (isV3() && isBaselinePair() && (state.taxonomy.failure_modes || []).length) {
      const fmContainer = h('div', { id: 'ascFailureModes' });
      failureField.appendChild(h('div', { class: 'asc-field' },
        h('label', { class: 'asc-label' }, 'How did it fail? ',
          h('span', { class: 'asc-label-hint' }, '(model-failure taxonomy — select all that apply)')),
        fmContainer));
      renderFailureTags(fmContainer);
    }

    const card = h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head rejected' }, '✕ Rejected answer (' + d.rejected_id + ') — what went wrong'),
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
        placeholder: (meta ? meta.label : t.mode) + ' — one line of specifics (optional)', value: t.note || '' });
      note.addEventListener('input', () => { t.note = note.value; saveDraft(); });
      container.appendChild(note);
    });
  }

  // Model-suggested error tags + draft rationale (Speed Optimization §2):
  // rendered as visually-distinct "Suggested — tap to accept" chips. NOTHING is
  // applied without an explicit tap; accepted values land in the normal editable
  // fields. Suggestions only show on the model's suggested-weaker side.
  // Accepting mutates the draft and re-renders the rationale from state (the
  // renderers own the DOM — no hand-syncing of chip rows or inputs).
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
      h('div', { class: 'asc-suggest-label' }, 'Suggested — tap to accept'));
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
  // selected error tag. The vocabulary comes from the server taxonomy only —
  // a local copy would drift from what validation accepts. V2-only; V1 keeps
  // the classic free-text "why it's worse" as the sole reason input.
  function renderTagReasons(container) {
    if (!isAssisted()) { if (container) clear(container); return; }
    renderPerTagPills(container, {
      label: 'Why, per error',
      hint: '(one tap — optional)',
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
      // §13 (V3/V4): the free-text "what's off with this step?" — the backend
      // derives step_error_tag (and the correction_reason vocab) from it.
      step_note: '',
      label: null, step_reward: null, critique: '', evidence_anchor: emptyAnchor(),
      // Pre-grade suggestion (Speed Optimization §2) — a hint, never the label.
      suggested_label: (suggested && suggested.suggested_label) || null,
      suggested_critique: (suggested && suggested.suggested_critique) || null,
    };
  }
  // A manually authored step (the doctor's own correct reasoning the AI omitted):
  // no original_text, added=true, label=good — counts as resolved.
  function newAuthoredStep() {
    const s = newStep('', null);
    s.added = true; s.label = 'good'; s.step_reward = 1;
    return s;
  }
  // The single definition of what "confirmed good" / "back to pending" means
  // for a step — used by the per-step button (expanded + collapsed) AND the
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

  // Auto-split the chosen answer into gradable steps — pre-graded when the LLM
  // is available (Speed Optimization §2): each step arrives with a suggested
  // good/bad label so the doctor spends time only on the flagged ones. Force
  // re-runs even when steps already exist (the "Re-split" affordance). Degrades
  // gracefully — offline the steps arrive unlabeled and the doctor grades
  // manually; on failure the doctor just adds steps.
  async function autoSplitChosen(listId, force) {
    const text = chosenRefinedText().trim();
    const startedChosen = state.draft.chosen_id;
    if (!text || state.splitting) return;
    if (!force && activeSteps().length) return;
    state.splitting = true;
    const list = document.getElementById(listId);
    if (list) { clear(list); list.appendChild(h('p', { class: 'asc-help' }, 'Splitting the chosen answer into steps…')); }
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
  // label=good) or edited to correct it — on first divergence a required one-tap
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
    // step still requires an explicit confirm/correct — silence ≠ endorsement.
    const isCollapsed = (s) => (
      s.suggested_label === 'good' && !s._exp && !s.corrected && !s.added
      && (s.text || '').trim() === (s.original_text || '').trim()
    );
    const pendingGood = steps.filter((s) => isCollapsed(s) && !s.confirmed);
    if (pendingGood.length) {
      list.appendChild(h('div', { class: 'asc-step-bulkbar' },
        h('span', { class: 'asc-step-bulk-label' },
          pendingGood.length + ' step' + (pendingGood.length === 1 ? ' looks' : 's look')
          + ' correct to the model — read them, then confirm in one tap.'),
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
              h('span', { class: 'asc-step-suggest good', title: 'Model pre-grade — your confirmation is the label' }, 'model · looks correct'),
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

      // ✓ Correct as-is — explicit positive endorsement (silence ≠ endorsement).
      // Tapping an already-confirmed step toggles it back to pending.
      const confirmBtn = h('button', {
        class: 'asc-btn asc-btn-ghost asc-btn-sm asc-step-confirm', type: 'button',
        onClick: () => {
          setStepConfirmed(s, !s.confirmed);
          // In-place update only — a full re-render here resets the scroll
          // position and bounces the page up between steps.
          saveDraft(); syncStepUI(); updateSubmitState();
        },
      }, '✓ Correct as-is');

      // Reason chips (required on an edited step) — single-select, derive label.
      const chipEls = {};
      const reasonRow = h('div', { class: 'asc-step-reasons' });
      reasons.forEach((r) => {
        const chip = h('button', {
          class: 'asc-chip asc-chip-sm', type: 'button',
          onClick: () => {
            s.correction_reason = r;
            s.label = (r === 'minor_wording') ? 'neutral' : 'bad';
            s.step_reward = s.label === 'good' ? 1 : 0;
            // In-place update only — avoid the full re-render scroll jump.
            saveDraft(); syncStepUI(); updateSubmitState();
          },
        }, r.replace(/_/g, ' '));
        chipEls[r] = chip;
        reasonRow.appendChild(chip);
      });
      const reasonWrap = h('div', { class: 'asc-step-correct' },
        h('div', { class: 'asc-label asc-step-reason-hint' }, 'What was wrong with the AI step? (pick one)'),
        reasonRow);

      // Collapsed "original:" reference — the AI's split step we're correcting.
      const originalBox = hasOriginal
        ? h('details', { class: 'asc-step-original' },
            h('summary', {}, 'original: ' + ((s.original_text || '').length > 80
              ? (s.original_text || '').slice(0, 80) + '…' : (s.original_text || ''))),
            h('div', { class: 'asc-step-original-full' }, s.original_text || ''))
        : null;

      // Optional one-line critique — kept available on a corrected step.
      const ci = h('input', { class: 'asc-input', placeholder: "What's off with this step? (optional, one line)", value: s.critique || '' });
      ci.addEventListener('input', () => { s.critique = ci.value; saveDraft(); });
      const critiqueField = h('div', { class: 'asc-field', style: 'margin-top:8px' }, withMic(ci));

      // Model pre-grade flag (Speed Optimization §2) — a review hint, never a label.
      const flaggedBadge = (s.suggested_label === 'bad')
        ? h('span', { class: 'asc-step-suggest bad', title: 'Model pre-grade — verify and confirm or correct' }, 'model · flags this')
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
      // step — the highest-value place to ground (PRM step-level supervision).
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
          text = s.correction_reason ? ('corrected · ' + s.correction_reason.replace(/_/g, ' ')) : 'corrected — pick a reason';
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
      list.appendChild(h('p', { class: 'asc-help' }, 'No steps yet — add steps manually, or use “Re-split from answer”.'));
    }
  }

  // ─── Evidence anchor block (progressive disclosure) ─────────────────────────
  // V3 auto-suggest citation chip (Seamless PRD WS3). Given the clinician's
  // rationale/answer text, fetch the 1–3 most relevant library citations; the
  // doctor opens the snippet inline and Confirms (one tap) to set the
  // evidence_anchor + mark the record grounded (value ×1.3). Nothing is
  // auto-attached — the confirm is required (mission line). V3 only; returns
  // null elsewhere so V1/V2 keep the manual citation field unchanged.
  function renderCiteSuggest(anchor, getText, onConfirm) {
    if (!isV3()) return null;
    const wrap = h('div', { class: 'asc-cite-suggest' });
    let lastQuery = null, dismissed = false;
    // §11: a clean list — opening the source IS the action (the citation is
    // recorded as entry_method:'opened'); no separate Confirm step. An entry
    // with no verified link renders reference-only (no Open source button —
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
      // grounding=required task — the misleading-success case).
      if (isValidAnchor(anchor)) toast('Citation attached — this record is now grounded.', 'success');
      else toast('Citation attached — finish the source fields below to ground it.', 'info');
      // Defer the re-render one tick: the 'opened' path fires from an <a
      // target=_blank> click, and tearing the anchor out of the DOM inside its
      // own click handler can cancel the new-tab navigation in some browsers.
      if (onConfirm) setTimeout(onConfirm, 0);
    };
    const renderChips = (suggestions) => {
      clear(wrap);
      if (!suggestions.length) return;
      wrap.appendChild(h('div', { class: 'asc-cite-head' },
        h('span', { class: 'asc-cite-title' }, 'Suggested source' + (suggestions.length > 1 ? 's' : '') + ' — open one to check it; opening attaches it'),
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
      } catch (e) { /* suggestions are a bonus — never surface an error to the doctor */ }
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

    // Escape hatch: search the library (BUG-3c) — always one tap away.
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
    const identifier = h('input', { class: 'asc-input', placeholder: 'Identifier — PMID:…, DOI:…, KDIGO 2024', value: anchor.identifier || '' });
    identifier.addEventListener('input', () => {
      anchor.identifier = identifier.value;
      if (isV3() && !anchor.entry_method) anchor.entry_method = 'manual';
      saveDraft(); updateSubmitState();
    });
    // Paste-your-own URL (BUG-3c escape hatch): a link the doctor pastes rides the
    // anchor + is clickable. Pasting a URL with no source type defaults to "other".
    const openLink = openSourceLink(anchor);
    const url = h('input', { class: 'asc-input', placeholder: 'Paste a source URL (optional) — https://…', value: anchor.url || '' });
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
  // "Search the library" is always one tap away — the doctor types a query and
  // picks a source (fills the anchor). An escape hatch when the auto-suggest is
  // wrong (it is better to show nothing than a wrong citation, so search is
  // deliberately more permissive than the suggestion).
  function renderLibrarySearch(anchor, onPick) {
    const box = h('div', { class: 'asc-lib-search', hidden: true });
    const input = h('input', { class: 'asc-input', placeholder: 'Search the citation library — drug, analyte, guideline…' });
    const results = h('div', { class: 'asc-lib-results' });
    let t = null;
    const run = async () => {
      clear(results);
      try {
        const res = await api('/citations/search', { method: 'POST',
          body: { text: input.value.trim(), specialty: (state.task && state.task.specialty) || 'nephrology', k: 12 } });
        const list = (res && res.suggestions) || [];
        if (res.skipped) { results.appendChild(h('div', { class: 'asc-label-hint' }, 'No citation library for this specialty — type or paste your own.')); return; }
        if (!list.length) { results.appendChild(h('div', { class: 'asc-label-hint' }, 'No matches — try a drug name or lab analyte.')); return; }
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
        h('span', { class: 'asc-timer', id: 'ascTimer' }, formatTime(getElapsed())),
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
        if (!rg.ok) { ok = false; msg = rg.msg; }
        else if (!fg.ok) { ok = false; msg = fg.msg; }
        // §15 (V3/V4): confidence is an active choice, never the draft default.
        else if (isV3() && !d.confidence_set) { ok = false; msg = 'pick your confidence to submit'; }
      }
    }
    btn.disabled = !ok || state.submitting;
    hint.textContent = ok ? '' : msg;
  }

  // Human-readable label per backend-stamped phase (BUG-5). The percentages are
  // the backend's — we only translate the phase name; a phase without a pct shows
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
    // §15 (V3/V4): confidence must be an active choice — mirror the button gate
    // so a programmatic submit can never ship the draft default.
    if (isV3() && !state.draft.confidence_set) { updateSubmitState(); return; }
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
      // poll timeout is "still finalizing", NOT a failure — never lose the work.
      clearDraft(taskId);
      stopTimer();
      if (timedOut) {
        toast('Submitted — still finalizing in the background. It will appear once the pipeline completes.', 'success');
      } else if (finalStatus === 'needs_qa') {
        toast('Submitted — routed to QA review (' + n + ' record' + (n === 1 ? '' : 's') + ').', 'success');
      } else {
        toast('Submitted — packaged ' + n + ' record' + (n === 1 ? '' : 's'), 'success');
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
  // Shows the real, backend-stamped phase — never an invented percentage.
  async function pollSubmissionStatus(sid, progressHost) {
    const deadline = Date.now() + 60000;  // safety cap
    let last = null;
    while (Date.now() < deadline) {
      try {
        last = await api('/submissions/' + sid + '/status');
      } catch (e) {
        if (e.status === 401) throw e;
        // transient — keep polling until the deadline
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
    // §11: how the citation was captured — 'opened' (clicked through to the
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
      // §13: the free-text "what's off" — the server derives step_error_tag
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
        const entry = {
          text: (c.text || '').trim(), points: c.points || 0, axis: c.axis || null, source: c.source || 'manual',
          // Tier (Two-Model PRD WS-B) — the server re-derives from |points| when it
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
    return payload;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  ADMIN CONSOLE
  // ═══════════════════════════════════════════════════════════════════════════
  function renderAdminView() {
    stopTimer();
    updateHeaderProgress(); // admin view — the §16 bar hides here
    const tabs = [
      ['tasks', 'Tasks'],
      ['qa', 'QA'],
      ['ingestion', 'Ingestion'],
      ['buyers', 'Buyers & Requests'],
      ['exports', 'Exports'],
      ['metrics', 'Metrics'],
    ];
    const subnav = h('div', { class: 'asc-subnav' },
      tabs.map(([id, label]) => {
        const btn = h('button', {
          class: 'asc-subnav-btn' + (state.adminTab === id ? ' active' : ''),
          onClick: () => { state.adminTab = id; renderAdminView(); },
        }, label);
        // QA (BUG-2): a live pending-count badge so the backlog is never invisible.
        if (id === 'qa') btn.appendChild(h('span', { class: 'asc-badge asc-badge-count', id: 'ascQaBadge', style: 'margin-left:6px', hidden: true }));
        return btn;
      }));

    const body = h('div', { id: 'ascAdminBody' });
    setRoot(h('div', { class: 'asc-wrap' }, subnav, body));
    refreshQaBadge();

    if (state.adminTab === 'tasks') renderAdminTasks(body);
    else if (state.adminTab === 'qa') renderAdminQA(body);
    else if (state.adminTab === 'ingestion') renderAdminIngestion(body);
    else if (state.adminTab === 'buyers') renderAdminBuyers(body);
    else if (state.adminTab === 'exports') renderAdminExports(body);
    else if (state.adminTab === 'metrics') renderAdminMetrics(body);
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
          h('div', { class: 'asc-card-sub' }, 'Queue is clear — nothing needs QA right now.')));
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
        // Contributor identity is admin-visible (name / org / email) — it never
        // ships in a data package, but the admin is allowed to see who labelled.
        const contribCell = h('td', {},
          h('div', { style: 'font-weight:600' }, ident.name || ann.credentials || ann.specialty || '—'),
          ident.organization ? h('div', { class: 'asc-card-sub' }, ident.organization) : null,
          ident.email ? h('div', { class: 'asc-card-sub asc-mono' }, ident.email) : null);
        tbody.appendChild(h('tr', {},
          h('td', { class: 'asc-mono' }, (s.submission_id || '').slice(0, 12)),
          contribCell,
          h('td', {}, s.verdict || '—'),
          h('td', {}, reasons.length
            ? reasons.map((r) => h('span', { class: 'asc-chip asc-chip-warn', style: 'margin:2px' }, r))
            : h('span', { class: 'asc-label-hint' }, '—')),
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
        h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, task.prompt || '—')));

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
        pad.appendChild(qaField('Why worse', crit.why_worse || '—'));
        if ((crit.error_tags || []).length) {
          pad.appendChild(h('div', { class: 'asc-field' },
            h('label', { class: 'asc-label' }, 'Error tags on rejected'),
            h('div', {}, crit.error_tags.map((t) => h('span', { class: 'asc-chip', style: 'margin:2px' }, t)))));
        }
      } else if (verdict === 'both_inadequate') {
        const fs = payload.from_scratch || {};
        pad.appendChild(qaField('Ideal answer (from scratch)', fs.ideal_answer || '—'));
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
      h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, value || '—'));
  }
  function qaDiffBlock(label, original, revised) {
    const field = h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, label));
    if (revised && revised.trim() && revised.trim() !== (original || '').trim()) {
      field.appendChild(h('div', { class: 'asc-readbox' }, renderEditDiff(original || '', revised)));
      field.appendChild(h('div', { class: 'asc-label-hint', style: 'margin-top:4px' }, 'Highlighted sentences were revised by the specialist.'));
    } else {
      field.appendChild(h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, original || '—'));
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
  // synthetic — two doors, clearly signed). Mint secure partner links, watch
  // uploads land, triage quarantine, and promote ingested cases to V4 tasks.
  function renderAdminIngestion(body) {
    clear(body);

    // Mint a secure upload link
    const pid = h('input', { class: 'asc-input', placeholder: 'partner id (e.g. mercy-health)' });
    const plabel = h('input', { class: 'asc-input', placeholder: 'display label (optional)' });
    const pspec = selectFrom(['nephrology', 'cardiology'], 'nephrology');
    const phours = h('input', { type: 'number', class: 'asc-input', value: '72', min: '1', max: '720' });
    const pcontact = h('input', { type: 'email', class: 'asc-input', placeholder: 'partner contact email (optional)' });
    const ponce = h('input', { type: 'checkbox', checked: 'checked' });
    const mintStatus = h('div', {});
    const mintBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Mint secure upload link');
    mintBtn.addEventListener('click', async () => {
      clear(mintStatus);
      if (!pid.value.trim()) { mintStatus.appendChild(h('div', { class: 'asc-inline-error' }, 'Partner id is required.')); return; }
      try {
        const res = await api('/admin/upload-links', { method: 'POST', body: {
          partner_id: pid.value.trim(), partner_label: plabel.value.trim() || null,
          specialty: pspec.value, expires_hours: Math.max(1, parseInt(phours.value, 10) || 72),
          one_time: ponce.checked, contact_email: pcontact.value.trim() || null,
        } });
        const url = res.upload_url || ('/partner/upload?t=' + res.token);
        mintStatus.appendChild(h('div', { class: 'asc-inline-warn' },
          'Copy this link NOW — the token is shown once and never stored: '));
        const urlBox = h('input', { class: 'asc-input asc-mono', value: url, readonly: 'readonly', style: 'margin-top:8px' });
        urlBox.addEventListener('click', () => { urlBox.select(); });
        mintStatus.appendChild(urlBox);
        mintStatus.appendChild(h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', style: 'margin-top:8px', onClick: () => {
          navigator.clipboard.writeText(url).then(() => toast('Link copied.', 'success')).catch(() => {});
        } }, 'Copy link'));
        loadIngestionLists();
      } catch (e) { mintStatus.appendChild(h('div', { class: 'asc-inline-error' }, e.message)); }
    });
    const mintCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Secure partner upload link'),
        h('div', { class: 'asc-card-sub' }, 'Tokenized, expiring, single-purpose. The partner uploads a de-identified .zip — no app account. This is the only door that produces V4 real cases.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Partner id'), pid),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Label'), plabel),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Specialty'), pspec)),
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Expires (hours)'), phours),
          h('label', { class: 'asc-checkbox-row', style: 'align-self:end;margin-bottom:14px' }, ponce, 'Single use'),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Contact email'), pcontact)),
        h('div', { class: 'asc-card-sub', style: 'margin:-4px 0 12px' },
          'If an upload through this link fails, we email this address that it didn’t come through (no PHI, no breach) so they can re-send.'),
        mintBtn, mintStatus));

    const uploadsCard = h('div', { class: 'asc-card', id: 'ascIngestUploads' }, loadingCard('Loading uploads…'));
    const casesCard = h('div', { class: 'asc-card', id: 'ascIngestCases' }, loadingCard('Loading ingested cases…'));
    body.appendChild(mintCard);
    body.appendChild(uploadsCard);
    body.appendChild(casesCard);
    loadIngestionLists();
  }

  // Cached uploads (used to filter the promote list by partner/file search).
  let _ingestUploads = [];

  const _UPLOADS_PAGE = 50;
  let _uploadsOffset = 0;
  let _uploadsTotal = 0;

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
  // file, and — for anything that didn't cleanly ingest — Notify the sender.
  async function renderUploadsTable(offset) {
    const up = document.getElementById('ascIngestUploads');
    if (!up) return;
    try {
      const data = await api('/ingestion/uploads?limit=' + _UPLOADS_PAGE + '&offset=' + Math.max(0, offset));
      const uploads = data.uploads || [];
      _uploadsOffset = data.offset || 0;
      _uploadsTotal = data.total || 0;
      clear(up);
      up.appendChild(h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' },
        'Partner uploads (' + _uploadsTotal + ')')));
      if (!_uploadsTotal) {
        up.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-card-sub' },
          'No uploads yet — mint a link above and send it to the partner.')));
        return;
      }
      const rows = uploads.map((u) => {
        const dlBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
          onClick: () => downloadBlob('/ingestion/uploads/' + u.upload_id + '/download', u.filename || (u.upload_id + '.zip')) },
          '⬇ Download file');
        const actions = [dlBtn];
        if (u.status !== 'ingested') {
          const canNotify = !!u.contact_email;
          const nbtn = h('button', {
            class: 'asc-btn asc-btn-subtle asc-btn-sm',
            title: canNotify ? ('Email ' + u.contact_email + ' that this upload didn’t come through')
                             : 'No contact email on this upload’s link — set one when minting the link',
            onClick: () => notifySender(u),
          }, u.failure_notified ? 'Re-notify sender' : 'Notify sender');
          if (!canNotify) nbtn.disabled = true;
          actions.push(nbtn);
        }
        return h('tr', {},
          h('td', {}, fmtDate(u.created_at)),
          h('td', {}, u.partner_label || u.partner_id || '—'),
          h('td', { class: 'asc-mono' }, (u.filename || '') + ' · ' + Math.round((u.size_bytes || 0) / 1024) + 'KB'),
          h('td', {}, h('span', { class: 'asc-badge ' + (u.status === 'ingested' ? 'asc-badge-green' : (u.status === 'quarantined' ? 'asc-badge-amber' : (u.status === 'rejected' ? 'asc-badge-red' : 'asc-badge-gray'))) }, u.status)),
          h('td', {}, h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' }, actions)));
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
      toast('Sender notified' + (res && res.detail ? ' — ' + res.detail : ''), 'success');
      renderUploadsTable(_uploadsOffset);   // reflect the notified state
    } catch (e) {
      toast('Could not notify sender: ' + (e.detail || e.message || ''), 'error');
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
      h('div', { class: 'asc-card-title' }, 'Ready to promote — by partner upload'),
      h('div', { class: 'asc-card-sub' }, 'Pick a partner file and promote it to V4. We convert the real records, run automated tests, and show you one sample case (labs, notes, EHR + candidates) to review — then extend case creation to the rest of the file.'),
      h('div', { class: 'asc-field', style: 'margin-top:12px;margin-bottom:0' }, search))));
    cc.appendChild(listBox);
    renderPromoteList(listBox, query || '');
  }

  function renderPromoteList(listBox, query) {
    clear(listBox);
    const q = (query || '').trim().toLowerCase();
    const eligible = (_ingestUploads || []).filter((u) => (u.ingested_case_count || 0) > 0
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
      const promoteBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm' }, 'Promote to V4 task');
      promoteBtn.addEventListener('click', () => openPromoteReview(u, st));
      listBox.appendChild(h('div', { class: 'asc-card-pad', style: 'border-top:1px solid var(--asc-line)' },
        h('div', { style: 'display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center' },
          h('div', {},
            h('strong', {}, (u.partner_label || u.partner_id || 'partner')),
            h('div', { class: 'asc-card-sub asc-mono' }, u.filename || ''),
            h('div', { class: 'asc-card-sub' }, (u.ingested_case_count || 0) + ' case(s) ready · uploaded ' + fmtDate(u.created_at))),
          h('div', { style: 'display:flex;gap:8px;align-items:center' },
            h('span', { class: 'asc-badge-real' }, 'real · V4'),
            promoteBtn)),
        st));
    });
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
          '. You can still promote — cases that fail the gate stay ingested with the reason recorded.');

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
    const promoteAllBtn = h('button', { class: 'asc-btn asc-btn-primary' }, '✓ Looks good — create the rest (' + (prep.ingested_count || 0) + ')');
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
        promoteAllBtn.removeAttribute('disabled'); promoteAllBtn.textContent = '✓ Looks good — create the rest (' + (prep.ingested_count || 0) + ')';
      }
    });

    const popup = h('div', { class: 'call-team-popup', style: 'max-width:820px;max-height:90vh;overflow:auto;text-align:left', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'call-team-title' }, 'Review a sample case — ' + (prep.partner_label || upload.partner_id || 'partner')),
      h('div', { class: 'call-team-sub' }, (prep.filename || '') + ' · ' + (prep.ingested_count || 0) + ' case(s) in this file'),
      testBanner,
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Clinical question'),
        h('div', { class: 'asc-readbox', style: 'white-space:pre-wrap' }, s.question || '—')),
      h('div', { class: 'asc-card-sub', style: 'margin:4px 0 14px' },
        (s.specialty || '') + ' · ' + (demo.age_band || '?') + ' ' + (demo.sex || '') + ' · ' +
        (kase.lab_panels || []).length + ' lab panel(s) · ' + (kase.notes || []).length + ' note(s)'),
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

    // Seedmaker auto-generation (Mode A) — generate N validated tasks (prompt + 2
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
    // trace (that's the value), so those controls don't apply — reflect that.
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
        h('div', { class: 'asc-card-title' }, 'Auto-generate tasks (Seedmaker — SYNTHETIC, V1–V3)'),
        h('div', { class: 'asc-card-sub' }, 'Text prompts or structured multimodal cases — all SYNTHETIC (V3 tier). Real patient cases (V4) come only from the Ingestion tab. Quality-gated before they enter the queue.'))),
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
          ? (e.message || 'Auto-generation unavailable — no LLM key configured.')
          : e.message;
        agStatus.appendChild(h('div', { class: 'asc-inline-error' }, msg));
      } finally {
        agBtn.removeAttribute('disabled');
      }
    });

    // Load GOLD cases (Two-Model PRD Workstream C — the "load gold" half of the
    // load-vs-generate split). Distinct from auto-generate: inserts the ratified,
    // hand-authored seed cases with NO LLM required — the reliable way to populate
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
        h('div', { class: 'asc-card-title' }, 'Load gold cases (RATIFIED — no LLM needed)'),
        h('div', { class: 'asc-card-sub' }, 'Insert the hand-authored, clinician-ratified multimodal seed cases (real labs + EHR + an authored A/B pair) straight into the V3 queue. Idempotent — safe to click repeatedly. Use this to populate V3 without an LLM key; use "Auto-generate" above for NOVEL cases.'))),
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
        h('div', { class: 'asc-card-sub' }, 'Cases a real frontier model got wrong, with the expert correction — the artifact to put in front of a lab.'))));
      if (!summary.length && !failures.length) {
        card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-card-sub' },
          'No model failures captured yet. On a task, admins can "Grade the real models" to A/B two real frontier answers; a rejected model is recorded here.')));
        return;
      }
      const pad = h('div', { class: 'asc-card-pad' });
      // Provider rollup headline ("OpenAI failed N; Anthropic failed K") — the
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
          h('div', {}, h('strong', {}, 'Expert correction: '), (f.expert_correction || '—'))));
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
        h('td', {}, String(b.target_count != null ? b.target_count : '—')),
        h('td', {}, b.min_difficulty || '—')));
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
        // multimodal_not_necessary, near_duplicate, …), not just a count — that's
        // the difference between "broken" and "thresholds need tuning".
        const breakdown = dkeys.length
          ? dkeys.sort((a, b) => dropped[b] - dropped[a])
              .map((k) => k.replace(/_/g, ' ') + ' ' + dropped[k]).join(' · ')
          : '—';
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
        h('td', {}, h('span', { class: 'asc-badge asc-badge-primary' }, t.specialty || '—')),
        // Modality badge (Multimodal Debug PRD P0.3): multimodal batches must be
        // distinguishable at a glance, not invisible among text tasks.
        h('td', {}, (t.modality || 'text') === 'multimodal'
          ? h('span', { class: 'asc-badge asc-badge-accent' }, 'multimodal')
          : 'text'),
        // Case source + version (EHR PRD §9.5): an admin must never mistake a
        // REAL case for a synthetic one at a glance. Real ⇒ V4, always.
        h('td', {}, t.case_source === 'real_deid'
          ? h('span', { class: 'asc-badge asc-badge-real' }, 'real · V4')
          : (t.case_source ? 'synthetic' : '—')),
        h('td', {}, t.difficulty || '—'),
        h('td', {}, (t.prompt || '').slice(0, 90) + ((t.prompt || '').length > 90 ? '…' : '')),
        h('td', {}, t.grounding_mode === 'required' ? h('span', { class: 'asc-badge asc-badge-amber' }, 'required') : 'optional'),
        h('td', {}, String(t.submission_count != null ? t.submission_count : 0)),
        h('td', {}, t.status === 'prompt_flagged'
          ? h('span', { class: 'asc-badge asc-badge-amber' }, 'prompt flagged')
          : (t.status || '—')),
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

  // §4.2 two-frontier provenance (ADMIN-ONLY — ab_meta/needs_baseline exist only
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
            title: 'prompt_hash mismatch — the pair compares prompts, not models. It should have been discarded.' }, 'prompt divergence'));
      if (!meta.two_providers) {
        flags.appendChild(h('span', { class: 'asc-badge asc-badge-amber',
          title: 'Both slots came from one provider (legacy/fallback pair) — not a two-frontier comparison.' }, 'one provider'));
      }
      cell.appendChild(flags);
    }
    if (t.needs_baseline) {
      cell.appendChild(h('div', { style: 'margin-top:4px' },
        h('span', { class: 'asc-badge asc-badge-amber',
          title: 'No two-frontier pair could be assembled (e.g. OPENAI_API_KEY unset or a provider down). The task is HELD — never silently served two same-provider answers.' },
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
        toast('Swapped in ' + (res.candidate_count || 0) + ' real-model answers — this task now grades the real models.', 'success');
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
        toast('Sent ' + (r.record_count || 0) + ' record(s) to ' + r.buyer_email + (r.email_sent ? ' — email delivered.' : ' — but email failed.'), r.email_sent ? 'success' : 'info');
        loadBuyerDeliveries();
      } catch (e) {
        clear(status);
        const msg = e.status === 400 ? (e.message || 'Nothing to export for that selection/window.')
          : (e.status === 503 ? 'Email is not configured — set SendGrid/SMTP (or EMAIL_DEV_MODE=1 for local).'
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
      const verMix = (bpv) => { const keys = Object.keys(bpv || {}); return keys.length ? keys.sort().map((k) => ascVerLabel(k) + ' ' + bpv[k]).join(' · ') : '—'; };
      const rows = deliveries.map((d) => h('tr', {},
        h('td', {}, fmtDate(d.sent_at)),
        h('td', { class: 'asc-mono' }, d.buyer_email),
        h('td', { style: 'max-width:220px' }, d.label || '—'),
        h('td', {}, String(d.record_count != null ? d.record_count : '—')),
        h('td', {}, d.data_format || '—'),
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
          h('td', {}, b.name), h('td', {}, b.contact || '—'), h('td', {}, b.export_profile || 'default')))))));
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
            h('td', {}, r.source || '—'),
            h('td', {}, (c.specialty || '—') + ' / ' + (c.difficulty || '—')),
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
      h('div', { class: 'call-team-sub' }, 'Request ' + (req.request_id || '').slice(0, 10) + ' · source ' + (req.source || '—')),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'From internal bank — count'), countInput),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'From uploaded prompts ', h('span', { class: 'asc-label-hint' }, '(optional JSON — overrides count)')), promptsTa),
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
              toast('Batch created — ' + res.count + ' task(s)', 'success');
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
        'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + ' — downloading…'));
      await downloadExport(manifest.export_id);
      loadExportsHistory();
      refreshExportReadyCount();
    } catch (e) {
      const msg = e.status === 400
        ? 'Nothing to export yet — complete an evaluation first.'
        : (e.status === 422 ? 'Schema validation failed: ' + e.message : (e.message || 'Export failed'));
      statusBox.appendChild(h('div', { class: 'asc-inline-error' }, msg));
    } finally {
      btn.removeAttribute('disabled');
      btn.textContent = orig;
    }
  }

  // Approve everything stuck in QA, then export — the "label -> export now" path.
  async function approveAllAndExport(btn, statusBox) {
    const orig = btn.textContent;
    btn.setAttribute('disabled', '');
    btn.textContent = 'Approving QA…';
    clear(statusBox);
    try {
      const res = await api('/qa/approve-all', { method: 'POST' });
      const k = res.approved != null ? res.approved : 0;
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' },
        'Approved ' + k + ' submission' + (k === 1 ? '' : 's') + ' from QA — exporting…'));
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
        '(' + qaPending + ' more submission' + (qaPending === 1 ? ' is' : 's are') + ' in QA review — approve in the QA Queue tab to add them.)'));
    } else if (qaPending > 0) {
      // The usual reason a just-labeled submission isn't exportable: it was
      // sampled/flagged into QA. Let the admin release + export in one click.
      btn.textContent = '✓ Approve ' + qaPending + ' pending & export';
      btn.onclick = () => approveAllAndExport(btn, statusBox());
      if (noteEl) noteEl.appendChild(h('span', {},
        qaPending + ' submission' + (qaPending === 1 ? '' : 's') + ' from your evaluators ' +
        (qaPending === 1 ? 'is' : 'are') + ' held in QA review (quality sampling). Approve to make ' +
        (qaPending === 1 ? 'it' : 'them') + ' exportable — or review individually in the QA Queue tab.'));
    } else if (exported > 0) {
      btn.textContent = '⬇ Re-export all records (' + exported + ')';
      btn.onclick = () => quickExportAll(btn, statusBox(), true);
      if (noteEl) noteEl.appendChild(h('span', {},
        'All ' + exported + ' record' + (exported === 1 ? '' : 's') + ' already exported — re-package to download again, or grab any prior bundle from the history below.'));
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
        const mix = Object.keys(bpv).map((k) => k + ':' + bpv[k]).join(' · ') || '—';
        cohortStatus.appendChild(h('div', { class: 'asc-inline-ok' },
          'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + ' (' + mix + ') — downloading…'));
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
        'Package a single cohort — V2 (assisted), V1 (classic), or both. Every record is also stamped with its source version.'),
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
  //  Contributors browser — shared org → contributor drill-down used by both the
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
    return mode === 'export' ? 'Contributors — export by organization' : 'Metrics by organization & contributor';
  }
  function browseSub(mode) {
    return mode === 'export'
      ? 'Browse every contributor by organization. Export all the data an organization labelled, or open a contributor to export their data or generate a credential verification summary.'
      : 'Drill from overall metrics into a single organization, then a single contributor — including when they last labelled.';
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
        c.primary_specialty || c.specialty || '—',
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
          h('span', { class: 'asc-badge asc-badge-primary' }, cr.role_title || '—'),
          h('span', { class: 'asc-badge asc-badge-gray' }, (cr.ship && cr.ship.primary_specialty) || c.primary_specialty || '—'),
          h('span', { class: 'asc-badge asc-badge-gray' }, 'id ' + (c.id_hashed || '').slice(0, 12)),
          h('span', { class: 'asc-badge asc-badge-amber' }, (c.record_count || 0) + ' records')))));
    pad.appendChild(h('div', { class: 'asc-blurb' }, prof.blurb || '—'));

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
          h('div', { class: 'asc-gb-title' }, 'Sandbox account — excluded from exports'),
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
          h('td', { style: 'max-width:340px' }, s.prompt_preview || '—'),
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
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' }, 'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + ' (' + label + ') — downloading…'));
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
          h('span', { class: 'asc-badge asc-badge-primary' }, c.role_title || c.role || '—'),
          h('span', { class: 'asc-badge asc-badge-gray' }, c.primary_specialty || c.specialty || '—')))));
    pad.appendChild(h('div', { class: 'asc-stat-grid', style: 'margin-top:14px' },
      stat(c.submission_count || 0, 'Submissions', null, true),
      stat(c.record_count || 0, 'Records labelled'),
      stat(c.grounded_submissions || 0, 'Grounded subs'),
      stat((c.total_hours != null ? c.total_hours : 0) + 'h', 'Total hours'),
      stat(c.premium_submissions || 0, 'Premium subs'),
      stat(c.avg_time_sec != null ? formatTime(Math.round(c.avg_time_sec)) : '—', 'Avg time / task'),
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
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' }, 'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + ' — downloading…'));
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
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' }, 'Packaged ' + n + ' record' + (n === 1 ? '' : 's') + ' — downloading…'));
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
      || 'CONFIDENTIAL — credential verification, provided under NDA / non-circumvention.';
    const popup = h('div', { class: 'call-team-popup', style: 'max-width:720px;max-height:90vh;overflow:auto;text-align:left', onClick: (e) => e.stopPropagation() },
      h('div', { class: 'call-team-title' }, 'Further Credential Summary'),
      h('p', { class: 'asc-help' }, 'Verification dossier for ', h('strong', {}, displayName),
        ' — releases the private (Tier B) credentials under NDA. Watermarked confidential and logged for audit.'),
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
        if (!keys.length) return '—';
        if (keys.length === 1) return verLabel(keys[0]);
        return keys.sort().map((k) => k + ' ' + bpv[k]).join(' · '); // mixed
      };
      const rows = exports.map((x) => h('tr', {},
        h('td', { class: 'asc-mono' }, (x.export_id || '').slice(0, 12)),
        h('td', {}, x.profile || '—'),
        h('td', {}, versionCell(x)),
        h('td', {}, String(x.record_count != null ? x.record_count : (x.count != null ? x.count : '—'))),
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

    // Top stat tiles
    const omc = s.open_modality_counts || {};
    const tiles = h('div', { class: 'asc-stat-grid' },
      stat(s.task_count != null ? s.task_count : 0, 'Tasks', null, true),
      // Multimodal Debug PRD P3.11: always-visible count of structured cases in
      // the OPEN queue — "0" here is the tell that generation hasn't produced
      // (or the queue drained), before anyone wonders why no case panel appears.
      stat(omc.multimodal != null ? omc.multimodal : 0, 'Multimodal in queue',
        (omc.text != null ? omc.text : 0) + ' text open'),
      stat(sumValues(sc), 'Submissions'),
      stat((qpr.pass_rate != null ? Math.round(qpr.pass_rate * 100) : 0) + '%', 'QA pass rate', (qpr.passed || 0) + ' / ' + (qpr.reviewed || 0) + ' reviewed'),
      stat(fmtNum(s.average_agreement), 'Avg agreement'),
      stat(fmtNum(kappa.overall), "Cohen's κ", 'n=' + (kappa.n != null ? kappa.n : 0)),
      stat((grounded.grounded_pct != null ? grounded.grounded_pct : 0) + '%', 'Grounded', (grounded.submissions_grounded || 0) + ' / ' + (grounded.submissions_total || 0)),
      stat(flaw.rate != null ? Math.round(flaw.rate * 100) + '%' : '—', 'Flaw catch rate', (flaw.caught || 0) + ' / ' + (flaw.scored || 0) + ' generated'),
      stat(s.export_count != null ? s.export_count : 0, 'Exports'));

    body.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:14px' }, 'Overview'), tiles));

    // Model-Failure view (FEAT-1): "cases where model X failed, with the expert
    // correction" — the artifact you put in front of a lab.
    const mfCard = h('div', { class: 'asc-card', id: 'ascModelFailures' }, loadingCard('Loading model failures…'));
    body.appendChild(mfCard);
    loadModelFailures();

    // Data by product version: how many submissions came from the V1 (classic),
    // V2 (assisted), and V3 (seamless) evaluator flows.
    const pvc = s.portal_version_counts || {};
    const v1n = pvc.v1 || 0, v2n = pvc.v2 || 0, v3n = pvc.v3 || 0, v4n = pvc.v4 || 0, pvTotal = v1n + v2n + v3n + v4n;
    const pct = (n) => pvTotal ? Math.round((100 * n) / pvTotal) + '%' : '0%';
    // Position-bias QC (Seamless PRD WS6): the A/B slot is randomized 50/50 so a
    // reward model can't learn "A is better" — a rate drifting from ~50% is an alarm.
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
        stat(abRate == null ? '—' : Math.round(abRate * 100) + '%',
          (abOk ? '' : 'alert · ') + 'A-is-stronger rate',
          'target ~50% · n=' + (abb.n || 0) + ' (position-bias QC)'),
        (function () {
          // Two-frontier slot balance (A3): OpenAI-in-slot-A rate over built pairs.
          const sb = s.ab_slot_balance || {};
          const r = sb.openai_as_A_rate;
          const ok = r == null || (r >= 0.4 && r <= 0.6);
          return stat(r == null ? '—' : Math.round(r * 100) + '%',
            (ok ? '' : 'alert · ') + 'OpenAI-as-A rate',
            'target ~50% · n=' + (sb.pairs || 0) + ' (two-frontier QC)');
        })(),
        (function () {
          // Two-frontier fallback health (PRD §A3 Rung 3): a RED chip when the rolling
          // legacy-fallback rate exceeds the ceiling — a provider is likely down and new
          // pairs are being held (needs_baseline) instead of shipping mostly-legacy data.
          const fb = s.ab_fallback || {};
          const r = fb.rate;
          const alert = !!fb.alert;
          return stat(r == null ? '—' : Math.round(r * 100) + '%',
            (alert ? 'alert · ' : '') + 'Legacy-fallback rate',
            alert
              ? 'ABOVE ceiling ' + Math.round((fb.ceiling || 0) * 100) + '% — a provider looks down; new pairs held. Fix OPENAI_API_KEY / the provider.'
              : 'ceiling ' + Math.round((fb.ceiling || 0) * 100) + '% · two-frontier fallback health');
        })()),
      h('p', { class: 'asc-help', style: 'margin-top:10px' },
        'All three flows capture the same judgment and produce the same record types; every record is stamped with its source version. '
        + 'The A/B slot is randomized 50/50 so preference data carries no position bias.')));

    // Value per clinician-minute (Value-per-Minute PRD Part A): the north-star
    // metric — sellable dollars produced per minute of clinician time — reported
    // REALIZED (bankable) with the PROJECTED reuse forecast alongside, and always
    // next to κ + the assist override rate so a rising ratio with falling quality
    // reads as the regression it is.
    const vpt = s.value_per_time || {};
    const vptOverall = vpt.overall || {};
    const vTarget = (s.value_per_time_target != null) ? s.value_per_time_target : (vpt.target != null ? vpt.target : 10);
    const byVer = vpt.by_portal_version || {};
    const ovr = s.override_rate || {};
    const ratio = (v) => (v == null ? '—' : (Math.round(v * 10) / 10) + ' : 1');
    const realizedOverall = vptOverall.realized_vpm;
    const meets = (realizedOverall != null && realizedOverall >= vTarget);
    const pctOr = (o) => {
      const r = o && o.override_rate;
      return r == null ? '—' : Math.round(r * 100) + '%';
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
        h('td', {}, t.email || t.evaluator_id || '—'),
        h('td', {}, String(t.count != null ? t.count : (t.submissions != null ? t.submissions : 0))),
        h('td', {}, t.avg_time_sec != null ? formatTime(Math.round(t.avg_time_sec)) : (t.average_time_sec != null ? formatTime(Math.round(t.average_time_sec)) : '—'))));
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', { class: 'asc-card-title' }, 'Evaluator throughput')),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {}, ['Evaluator', 'Submissions', 'Avg time / task'].map((c) => h('th', {}, c)))),
          h('tbody', {}, rows)))));
    }

    // Contributor stats. (contributor_stats() returns submissions / grounded /
    // premium / total_hours — not count/approved.)
    const contrib = s.contributor_stats || [];
    if (contrib.length) {
      const rows = contrib.map((t) => h('tr', {},
        h('td', {}, t.email || t.evaluator_id || '—'),
        h('td', {}, t.specialty || '—'),
        h('td', {}, String(t.submissions != null ? t.submissions : 0)),
        h('td', {}, String(t.grounded_submissions != null ? t.grounded_submissions : 0)),
        h('td', {}, t.total_hours != null ? t.total_hours + 'h' : '—')));
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
  function fmtNum(n) { return (n == null || isNaN(n)) ? '—' : (Math.round(n * 1000) / 1000).toString(); }
  function trunc(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; }
  // All admin timestamps are STORED as naive UTC ISO strings (Python
  // datetime.utcnow().isoformat() — no trailing 'Z'/offset). new Date() would
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
    if (!d) return '—';
    const dt = toUtcDate(d);
    if (isNaN(dt.getTime())) return String(d);
    return dt.toLocaleString('en-US', { timeZone: ASC_TZ }) + ' PT';
  }
  // Product-version label shared by the exports history + per-task version badges.
  function ascVerLabel(v) {
    return { v4: 'V4 · Real', v3: 'V3', v2: 'V2', v1: 'V1' }[v] || (v || '—');
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

  // ─── Go ────────────────────────────────────────────────────────────────────
  boot();
})();
