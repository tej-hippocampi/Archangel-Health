/* ═══════════════════════════════════════════════════════════════════════════
   Asclepius — Expert Evaluation Portal (vanilla SPA)
   Standalone Asclepius JWT auth. No frameworks, no build step.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const API_BASE = '/api/asclepius';
  const TOKEN_KEY = 'asclepius_token';
  const DRAFT_PREFIX = 'asclepius_draft_';
  // Doctor-portal session token (same origin). If present, we silently exchange
  // it for an Asclepius session so affiliated clinicians skip the login form.
  const DOCTOR_TOKEN_KEY = 'archangel_doctor_auth_token';

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
    //    Asclepius one (SSO) — no second login barrier.
    if (await trySsoLogin()) return;
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
    stopTimer();
    renderHeader();
    renderLogin();
  }

  // ─── Login screen ────────────────────────────────────────────────────────--
  function renderLogin(errorMsg) {
    document.getElementById('ascHeader').setAttribute('hidden', '');
    const emailInput = h('input', { class: 'asc-input', type: 'email', placeholder: 'you@hospital.org', autocomplete: 'username', required: 'required' });
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
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Email'), emailInput),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Password'), pwInput),
      submitBtn,
    );

    const card = h('div', { class: 'asc-login-card' },
      h('div', { class: 'asc-login-head' },
        h('div', { class: 'asc-login-mark' }, '⚕'),
        h('h1', {}, 'Asclepius'),
        h('p', {}, 'Expert Evaluation Portal'),
      ),
      h('div', { class: 'asc-login-body' },
        form,
        h('p', { class: 'asc-login-hint' }, 'Board-certified clinician access only. Contact your program administrator for credentials.'),
      ),
    );
    setRoot(h('div', { class: 'asc-login-wrap' }, card));
    setTimeout(() => emailInput.focus(), 30);
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  EVALUATOR WORKSPACE
  // ═══════════════════════════════════════════════════════════════════════════
  async function renderEvalView() {
    const wrap = h('div', { class: 'asc-wrap' });
    wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'loading-state' }, h('div', { class: 'loading-spinner' }), 'Loading next evaluation…')));
    setRoot(wrap);
    try {
      const data = await api('/tasks/next');
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
    setRoot(h('div', { class: 'asc-wrap' },
      h('div', { class: 'asc-card asc-card-pad' },
        h('div', { class: 'asc-empty' },
          h('div', { class: 'asc-empty-icon' }, '✅'),
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
      prompt_review: { reviewed: false, verdict: null, note: '', reviewed_at: null },
      independent_answer: { text: '', evidence_anchor: emptyAnchor(), captured_at: null },
      verdict: null,
      chosen_id: null,
      rejected_id: null,
      chosen_revision: { edited: false, revised_text: null, why_better_tags: [], why_better_notes: '', evidence_anchor: emptyAnchor() },
      rejected_critique: { error_tags: [], severities: {}, why_worse: '', error_tag_anchors: {} },
      from_scratch: { ideal_answer: '', approach_notes: '', reasoning_steps: [], evidence_anchor: emptyAnchor() },
      reasoning_steps: [],
      confidence: 'medium',
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
    if (!draft.prompt_review) draft.prompt_review = { reviewed: false, verdict: null, note: '', reviewed_at: null };
    if (!draft.independent_answer) draft.independent_answer = { text: '', evidence_anchor: emptyAnchor(), captured_at: null };
    if (!draft.independent_answer.evidence_anchor) draft.independent_answer.evidence_anchor = emptyAnchor();
    if (!draft.stage) draft.stage = 'prompt_review';
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
    // The compare stage's submit bar owns the live #ascTimer; avoid a duplicate
    // id here (only the first match would update). Stages 1–2 host it instead.
    const timer = d.stage === 'compare'
      ? null
      : h('span', { class: 'asc-timer', id: 'ascTimer' }, formatTime(getElapsed()));
    return h('div', { class: 'asc-stage-head' },
      h('div', { class: 'asc-stage-meta' },
        h('span', { class: 'asc-stage-step' }, 'Step ' + n + ' of 3'),
        h('span', { class: 'asc-stage-label' }, label)),
      h('div', { class: 'asc-stage-right' }, dots, timer));
  }

  function renderTaskWorkspace() {
    const task = state.task;
    const d = state.draft;
    const required = (task.grounding_mode || 'optional') === 'required';

    const promptCard = h('div', { class: 'asc-card asc-prompt-card' },
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-meta-row' },
          h('span', { class: 'asc-badge asc-badge-primary' }, task.specialty || 'general'),
          h('span', { class: 'asc-badge asc-badge-gray' }, 'Difficulty: ' + (task.difficulty || 'medium')),
          task.capture_reasoning ? h('span', { class: 'asc-badge asc-badge-accent' }, 'Reasoning capture') : null,
          required ? h('span', { class: 'asc-badge asc-badge-amber' }, 'Grounding required') : null,
        ),
        h('div', { class: 'asc-prompt-label' }, 'Clinical prompt'),
        h('div', { class: 'asc-prompt-text' }, task.prompt || ''),
      ));

    // Grounding disclaimer banner (required mode only)
    let groundingBanner = null;
    if (required && task.grounding_disclaimer) {
      groundingBanner = h('div', { class: 'asc-grounding-banner' },
        h('div', { class: 'asc-gb-icon' }, '📎'),
        h('div', {},
          h('div', { class: 'asc-gb-title' }, 'Evidence required for this task'),
          h('div', { class: 'asc-gb-text' }, task.grounding_disclaimer),
        ));
    }

    const wrap = h('div', { class: 'asc-wrap' }, promptCard, groundingBanner);

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
      renderCompareStage(wrap);
      setRoot(wrap);
      refreshAnswerHighlight();
      renderRationale();
      updateSubmitState();
    }
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
          h('div', { class: 'asc-blur-lock' }, '🔒'),
          h('div', { class: 'asc-blur-caption' }, caption))));
  }

  // Stage 3: the original compare + verdict + rationale + submit block.
  function renderCompareStage(wrap) {
    const answers = h('div', { class: 'asc-answers', id: 'ascAnswers' });
    (state.task.candidate_answers || []).forEach((c) => answers.appendChild(renderAnswerCard(c)));

    const verdicts = h('div', { class: 'asc-verdicts', id: 'ascVerdicts' },
      verdictButton('A_better', 'A is better', '1'),
      verdictButton('B_better', 'B is better', '2'),
      verdictButton('both_inadequate', 'Both inadequate', '3', true),
    );
    const rationale = h('div', { id: 'ascRationale' });
    const submitBar = renderSubmitBar();

    wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:14px' }, 'Compare the answers'),
      answers));
    wrap.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:14px' }, 'Your verdict',
        h('span', { class: 'asc-label-hint', style: 'font-weight:500;margin-left:6px' }, '(press 1 / 2 / 3)')),
      verdicts,
      rationale));
    wrap.appendChild(h('div', { class: 'asc-card' }, submitBar));
  }

  // ─── Stage 1: prompt validation gate (Feature A) ───────────────────────────
  function renderPromptGate() {
    const d = state.draft;
    const reasonBox = h('div', { id: 'ascFlagReason', hidden: true });
    const reasonInput = h('input', { class: 'asc-input', placeholder: 'One line — why is this prompt invalid? (e.g. ambiguous, not clinically meaningful, unsafe premise)', value: d.prompt_review.note || '' });
    reasonInput.addEventListener('input', () => { d.prompt_review.note = reasonInput.value; saveDraft(); });
    const confirmFlag = h('button', { class: 'asc-btn asc-btn-danger', onClick: flagPrompt }, 'Confirm — flag & skip');
    reasonBox.appendChild(h('div', { class: 'asc-field', style: 'margin-top:14px' },
      h('label', { class: 'asc-label' }, 'Reason for flagging'),
      reasonInput,
      h('div', { style: 'margin-top:10px' }, confirmFlag)));

    return h('div', { class: 'asc-card asc-card-pad asc-gate' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:6px' }, 'Is this prompt clinically valid?'),
      h('p', { class: 'asc-help', style: 'margin-bottom:16px' },
        'Confirm the prompt is a real, answerable clinical question before you see any answer. Your sign-off upgrades the data; flagged prompts are sent to admin review and excluded.'),
      h('div', { class: 'asc-gate-actions' },
        h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', onClick: validatePrompt }, 'Prompt is clinically valid ✓'),
        h('button', { class: 'asc-btn asc-btn-ghost', onClick: () => {
          reasonBox.hidden = false;
          reasonInput.focus();
        } }, 'Flag as invalid')),
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

  // ─── Stage 2: blind independent answer (Feature B) ─────────────────────────
  function renderIndependentAnswer() {
    const ia = state.draft.independent_answer;
    const ta = h('textarea', { class: 'asc-textarea', style: 'min-height:200px',
      placeholder: 'Write your full ideal answer to this prompt…' }, ia.text || '');
    const revealBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', id: 'ascRevealBtn', onClick: commitIndependentAnswerAndReveal }, 'Reveal AI answers →');
    const hint = h('span', { class: 'asc-submit-hint', id: 'ascRevealHint' });
    const syncReveal = () => {
      const ok = (ia.text || '').trim().length > 0;
      revealBtn.disabled = !ok;
      hint.textContent = ok ? '' : 'write your answer to continue';
    };
    ta.addEventListener('input', () => { ia.text = ta.value; saveDraft(); syncReveal(); });

    const card = h('div', { class: 'asc-card asc-card-pad asc-gate' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:6px' }, 'Before you see the AI answers, write your ideal answer'),
      h('p', { class: 'asc-help', style: 'margin-bottom:16px' },
        'This is captured uncontaminated — your own gold answer, before the A/B answers can anchor your judgment.'),
      h('div', { class: 'asc-field' }, ta),
      renderAnchorBlock(ia.evidence_anchor, { label: 'citation for your answer', required: false }),
      h('div', { class: 'asc-gate-reveal' }, hint, revealBtn));
    setTimeout(syncReveal, 0);
    return card;
  }

  // Fetch the withheld candidate answer texts (v2 anti-peeking) and merge them
  // into the in-memory task. No-op when the server already inlined the text
  // (ASCLEPIUS_WITHHOLD_ANSWERS=0) or they're already loaded.
  async function loadWithheldAnswersIfNeeded() {
    const task = state.task;
    if (!task) return;
    const missing = (task.candidate_answers || []).some((c) => c.text == null);
    if (!missing) return;
    const res = await api('/tasks/' + task.task_id + '/answers');
    const byId = {};
    (res.answers || []).forEach((a) => { byId[a.id] = a.text; });
    (task.candidate_answers || []).forEach((c) => { if (byId[c.id] != null) c.text = byId[c.id]; });
  }

  async function commitIndependentAnswerAndReveal() {
    const d = state.draft;
    if (!(d.independent_answer.text || '').trim()) return;
    const btn = document.getElementById('ascRevealBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Revealing…'; }
    try {
      await loadWithheldAnswersIfNeeded();
    } catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Reveal AI answers →'; }
      if (e.status !== 401) toast('Could not load the AI answers: ' + e.message, 'error');
      return;  // stay on Stage 2 rather than reveal blank answers
    }
    d.independent_answer.captured_at = new Date().toISOString();
    d.stage = 'compare';
    saveDraft();
    renderTaskWorkspace();
  }

  function renderAnswerCard(c) {
    return h('div', { class: 'asc-answer', dataset: { id: c.id } },
      h('div', { class: 'asc-answer-head' },
        h('div', { class: 'asc-answer-tag' },
          h('span', { class: 'asc-answer-letter', dataset: { letter: c.id } }, c.id),
          'Answer ' + c.id),
      ),
      h('div', { class: 'asc-answer-body' }, c.text || ''));
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
    // If the chosen side changed, reset the revised text so it pre-fills fresh.
    if (d.chosen_id !== prevChosen) d.chosen_revision.revised_text = null;
    saveDraft();
    // Update verdict button states
    const vc = document.getElementById('ascVerdicts');
    if (vc) Array.from(vc.children).forEach((b) => {
      b.classList.toggle('active', b.dataset.verdict === verdict);
    });
    refreshAnswerHighlight();
    renderRationale();
    updateSubmitState();
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

  // ─── Rationale (chosen / rejected / from-scratch) ───────────────────────────
  function renderRationale() {
    const container = document.getElementById('ascRationale');
    if (!container) return;
    clear(container);
    const d = state.draft;
    if (!d.verdict) return;

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
  }

  function chosenText() {
    const c = (state.task.candidate_answers || []).find((x) => x.id === state.draft.chosen_id);
    return c ? (c.text || '') : '';
  }

  function renderChosenCard() {
    const d = state.draft;
    const rev = d.chosen_revision;
    const original = chosenText();
    const ta = h('textarea', { class: 'asc-textarea', style: 'min-height:120px' },
      rev.revised_text != null ? rev.revised_text : original);
    ta.addEventListener('input', () => {
      rev.revised_text = ta.value;
      rev.edited = ta.value !== original;
      saveDraft();
    });

    const notes = h('textarea', { class: 'asc-textarea', placeholder: 'One line on why this answer is better (optional)…' }, rev.why_better_notes || '');
    notes.addEventListener('input', () => { rev.why_better_notes = notes.value; saveDraft(); });

    const whyTags = (state.taxonomy.why_better_tags || []);
    const chips = renderChips(whyTags, rev.why_better_tags, (tag, on) => {
      toggleInArray(rev.why_better_tags, tag, on);
      saveDraft();
    });

    return h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head chosen' }, '✓ Chosen answer (' + d.chosen_id + ') — edit to improve'),
      h('div', { class: 'asc-subcard-body' },
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Refined answer ',
            h('span', { class: 'asc-label-hint' }, 'edits become the gold revision; original is preserved')),
          ta),
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Why it\'s better'),
          notes),
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Why-better tags ', h('span', { class: 'asc-label-hint' }, '(optional)')),
          chips),
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
    const anchorContainer = h('div', { id: 'ascTagAnchors' });

    const chips = renderChips(errorTags, crit.error_tags, (tag, on) => {
      toggleInArray(crit.error_tags, tag, on);
      if (!on) { delete crit.severities[tag]; delete crit.error_tag_anchors[tag]; }
      saveDraft();
      renderSeverities(sevContainer);
      renderTagAnchors(anchorContainer);
    }, 'err');

    const whyWorse = h('input', { class: 'asc-input', placeholder: 'One line on the key problem (optional)…', value: crit.why_worse || '' });
    whyWorse.addEventListener('input', () => { crit.why_worse = whyWorse.value; saveDraft(); });

    const card = h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head rejected' }, '✕ Rejected answer (' + d.rejected_id + ') — what went wrong'),
      h('div', { class: 'asc-subcard-body' },
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Error tags ', h('span', { class: 'asc-label-hint' }, '(select all that apply)')),
          chips),
        sevContainer,
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Why it\'s worse'),
          whyWorse),
        h('div', { class: 'asc-disclosure' },
          discloseToggle('+ cite specific errors', anchorContainer)),
        anchorContainer,
      ));
    renderSeverities(sevContainer);
    renderTagAnchors(anchorContainer, true);
    return card;
  }

  function renderSeverities(container) {
    clear(container);
    const crit = state.draft.rejected_critique;
    if (!crit.error_tags.length) return;
    const sevs = (state.taxonomy.error_severities || ['low', 'medium', 'high']);
    const wrap = h('div', { class: 'asc-field' },
      h('label', { class: 'asc-label' }, 'Severity per error ', h('span', { class: 'asc-label-hint' }, '(optional)')));
    crit.error_tags.forEach((tag) => {
      const pills = h('div', { class: 'asc-sev-pills' });
      sevs.forEach((sev) => {
        pills.appendChild(h('button', {
          class: 'asc-sev-pill' + (crit.severities[tag] === sev ? ' active' : ''),
          type: 'button',
          onClick: (e) => {
            if (crit.severities[tag] === sev) delete crit.severities[tag];
            else crit.severities[tag] = sev;
            saveDraft();
            renderSeverities(container);
          },
        }, sev));
      });
      wrap.appendChild(h('div', { class: 'asc-sev-row' },
        h('span', { class: 'asc-sev-name' }, tag.replace(/_/g, ' ')), pills));
    });
    container.appendChild(wrap);
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

    return h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head' }, '✎ Compose the ideal answer'),
      h('div', { class: 'asc-subcard-body' },
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Ideal answer'),
          ideal),
        h('div', { class: 'asc-field' },
          h('label', { class: 'asc-label' }, 'Approach notes ', h('span', { class: 'asc-label-hint' }, '(optional)')),
          approach),
        renderAnchorBlock(fs.evidence_anchor, {
          label: 'citation for this answer',
          required: (state.task.grounding_mode === 'required'),
        }),
      ));
  }

  // ─── Reasoning steps editor (tap-to-grade, Feature C) ──────────────────────
  function newStep(text) {
    return { step: 0, text: text || '', label: null, step_reward: null, critique: '', evidence_anchor: emptyAnchor() };
  }
  // The chosen/refined answer text to split into steps (chosen path only).
  function chosenRefinedText() {
    const rev = state.draft.chosen_revision;
    return (rev.revised_text != null ? rev.revised_text : chosenText()) || '';
  }

  // Auto-split the chosen answer into gradable steps. Force re-runs even when
  // steps already exist (the "Re-split" affordance). Degrades gracefully — on
  // failure the doctor just adds steps manually.
  async function autoSplitChosen(listId, force) {
    const text = chosenRefinedText().trim();
    const steps = activeSteps();
    if (!text || state.splitting) return;
    if (!force && steps.length) return;
    state.splitting = true;
    const list = document.getElementById(listId);
    if (list) { clear(list); list.appendChild(h('p', { class: 'asc-help' }, '✨ Splitting the chosen answer into steps…')); }
    try {
      const res = await api('/reasoning/split', {
        method: 'POST',
        body: { text, prompt: state.task.prompt, specialty: state.task.specialty },
      });
      steps.length = 0;
      (res.steps || []).forEach((t) => steps.push(newStep(t)));
      saveDraft();
    } catch (e) { /* graceful: leave steps for manual entry */ }
    finally { state.splitting = false; renderStepsList(listId); updateSubmitState(); }
  }

  function renderStepsCard(forBoth) {
    const listId = 'ascStepsList';
    const required = (state.task.grounding_mode === 'required');
    const canAutoSplit = !forBoth;  // chosen path (A/B verdict) only

    const addBtn = h('button', {
      class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
      onClick: () => { activeSteps().push(newStep('')); saveDraft(); renderStepsList(listId); updateSubmitState(); },
    }, '+ Add step');
    const resplitBtn = canAutoSplit ? h('button', {
      class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
      onClick: () => autoSplitChosen(listId, true),
    }, '↻ Re-split from answer') : null;

    const card = h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head' }, '↳ Reasoning steps ',
        h('span', { class: 'asc-label-hint', style: 'margin-left:6px' },
          canAutoSplit ? 'tap good / neutral / bad on each step' : (required ? '(each step needs a citation)' : '(optional)'))),
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

  function renderStepsList(listId) {
    const list = document.getElementById(listId);
    if (!list) return;
    clear(list);
    const steps = activeSteps();
    const labels = (state.taxonomy.reasoning_step_labels || ['good', 'neutral', 'bad']);
    const required = (state.task.grounding_mode === 'required');
    steps.forEach((s, idx) => {
      s.step = idx + 1;
      const ta = h('textarea', { class: 'asc-textarea', placeholder: 'Describe this reasoning step…' }, s.text || '');
      ta.addEventListener('input', () => { s.text = ta.value; saveDraft(); });

      const labelBtns = h('div', { class: 'asc-step-labels' });
      labels.forEach((lab) => {
        labelBtns.appendChild(h('button', {
          class: 'asc-step-label ' + lab + (s.label === lab ? ' active' : ''),
          type: 'button',
          onClick: () => {
            s.label = (s.label === lab) ? null : lab;
            // PRM convention: good=1, neutral/bad=0 (a bad step earns no reward).
            // A lab that wants ±1 (bad=-1) sets it via the buyer export profile;
            // we keep the capture convention non-negative here.
            s.step_reward = s.label === 'good' ? 1 : (s.label === 'neutral' || s.label === 'bad') ? 0 : null;
            if (s.label === 'good' || s.label == null) s.critique = '';
            saveDraft();
            renderStepsList(listId);
          },
        }, lab));
      });

      const head = h('div', { class: 'asc-step-head' },
        h('span', { class: 'asc-step-num' }, 'Step ' + (idx + 1)),
        h('div', { style: 'display:flex;align-items:center;gap:8px' },
          labelBtns,
          h('button', {
            class: 'asc-btn-link', type: 'button',
            onClick: () => { steps.splice(idx + 1, 0, newStep('')); saveDraft(); renderStepsList(listId); updateSubmitState(); },
          }, '+ insert'),
          h('button', {
            class: 'asc-btn-link', type: 'button', style: 'color:var(--asc-danger)',
            onClick: () => { steps.splice(idx, 1); saveDraft(); renderStepsList(listId); updateSubmitState(); },
          }, 'Remove')));

      // Critique field — surfaced only on a flagged (non-good) step.
      let critique = null;
      if (s.label && s.label !== 'good') {
        const ci = h('input', { class: 'asc-input', placeholder: "What's off with this step? (one line)", value: s.critique || '' });
        ci.addEventListener('input', () => { s.critique = ci.value; saveDraft(); });
        critique = h('div', { class: 'asc-field', style: 'margin-top:8px' }, ci);
      }

      const anchorBlock = renderAnchorBlock(s.evidence_anchor, { label: 'citation for this step', required });

      list.appendChild(h('div', { class: 'asc-step' }, head, ta, critique, anchorBlock));
    });
    if (!steps.length) {
      list.appendChild(h('p', { class: 'asc-help' }, 'No steps yet — add steps manually, or use “Re-split from answer”.'));
    }
  }

  // ─── Evidence anchor block (progressive disclosure) ─────────────────────────
  function renderAnchorBlock(anchor, opts) {
    opts = opts || {};
    const required = !!opts.required;
    const body = h('div', { class: 'asc-disclosure-body' });
    if (!required && !isValidAnchor(anchor) && !(anchor.citation_text || '').trim()) body.setAttribute('hidden', '');
    body.appendChild(anchorFields(anchor));

    const status = h('span', { class: 'asc-anchor-valid' });
    const toggle = h('button', {
      class: 'asc-disclosure-toggle', type: 'button',
      onClick: () => {
        if (body.hasAttribute('hidden')) body.removeAttribute('hidden');
        else body.setAttribute('hidden', '');
      },
    }, required ? '📎 Citation (required)' : '+ add citation', status);

    const block = h('div', { class: 'asc-disclosure' }, toggle, body);
    block._status = status;
    refreshAnchorStatus(block, anchor, required);
    // keep status synced when fields change
    const sync = () => refreshAnchorStatus(block, anchor, required);
    body.addEventListener('input', sync);
    body.addEventListener('change', sync);
    return block;
  }

  function refreshAnchorStatus(block, anchor, required) {
    const status = block._status;
    if (!status) return;
    if (isValidAnchor(anchor)) { status.textContent = '✓ cited'; status.classList.remove('asc-anchor-invalid'); }
    else if (required) { status.textContent = '· citation needed'; status.classList.add('asc-anchor-invalid'); }
    else { status.textContent = ''; }
  }

  function anchorFields(anchor) {
    const types = (state.taxonomy.evidence_source_types || ['guideline', 'primary_literature', 'expert_consensus', 'other']);
    const citation = h('input', { class: 'asc-input', placeholder: 'e.g. KDIGO 2024 Guideline §3.2', value: anchor.citation_text || '' });
    citation.addEventListener('input', () => { anchor.citation_text = citation.value; saveDraft(); updateSubmitState(); });
    const sourceSel = h('select', { class: 'asc-select' },
      h('option', { value: '' }, 'Source type…'),
      ...types.map((t) => h('option', { value: t, selected: anchor.source_type === t ? 'selected' : null }, t.replace(/_/g, ' '))));
    sourceSel.value = anchor.source_type || '';
    sourceSel.addEventListener('change', () => { anchor.source_type = sourceSel.value; saveDraft(); updateSubmitState(); });
    const identifier = h('input', { class: 'asc-input', placeholder: 'Identifier — PMID:…, DOI:…, KDIGO 2024', value: anchor.identifier || '' });
    identifier.addEventListener('input', () => { anchor.identifier = identifier.value; saveDraft(); updateSubmitState(); });
    return h('div', {},
      h('div', { class: 'asc-field', style: 'margin-bottom:10px' }, h('label', { class: 'asc-label' }, 'Citation'), citation),
      h('div', { class: 'asc-form-row', style: 'margin-bottom:0' },
        h('div', { class: 'asc-field', style: 'margin-bottom:0' }, h('label', { class: 'asc-label' }, 'Source type'), sourceSel),
        h('div', { class: 'asc-field', style: 'margin-bottom:0' }, h('label', { class: 'asc-label' }, 'Identifier'), identifier)));
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
      if (!g.ok) {
        ok = false;
        msg = g.reasons.indexOf('missing_step_anchor') !== -1
          ? 'add a citation to your rationale and each step to continue'
          : 'add a citation to continue';
      }
    }
    btn.disabled = !ok || state.submitting;
    hint.textContent = ok ? '' : msg;
  }

  async function submitEvaluation() {
    if (state.submitting) return;
    saveDraft();
    const g = groundingSatisfied();
    if (!g.ok) { updateSubmitState(); return; }
    state.submitting = true;
    const btn = document.getElementById('ascSubmit');
    if (btn) { btn.disabled = true; btn.textContent = 'Submitting…'; }

    const payload = buildSubmissionPayload();
    try {
      const res = await api('/submissions', { method: 'POST', body: payload });
      const n = res.record_count != null ? res.record_count : 0;
      clearDraft(state.draft.task_id);
      stopTimer();
      toast('Submitted — packaged ' + n + ' record' + (n === 1 ? '' : 's'), 'success');
      renderEvalView();
    } catch (e) {
      state.submitting = false;
      if (btn) { btn.textContent = 'Submit evaluation'; }
      if (e.status === 400 && e.detail && e.detail.error === 'grounding_required') {
        toast(e.detail.message || 'A citation is required before submitting.', 'error');
        updateSubmitState();
      } else if (e.status !== 401) {
        toast('Submit failed: ' + e.message, 'error');
        updateSubmitState();
      }
    } finally {
      state.submitting = false;
    }
  }

  function cleanAnchor(a) { return isValidAnchor(a) ? { citation_text: a.citation_text.trim(), source_type: a.source_type, identifier: a.identifier.trim() } : null; }
  function cleanSteps(steps) {
    return (steps || []).filter((s) => (s.text || '').trim()).map((s, i) => ({
      step: i + 1,
      text: s.text,
      label: s.label || null,
      step_reward: s.step_reward != null ? s.step_reward : null,
      critique: (s.critique || '').trim() || null,
      evidence_anchor: cleanAnchor(s.evidence_anchor),
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
        captured_at: d.independent_answer.captured_at,
      },
    };
    if (d.verdict === 'A_better' || d.verdict === 'B_better') {
      const original = chosenText();
      const revised = d.chosen_revision.revised_text != null ? d.chosen_revision.revised_text : original;
      payload.chosen_revision = {
        edited: revised !== original,
        revised_text: revised,
        why_better_tags: d.chosen_revision.why_better_tags.slice(),
        why_better_notes: d.chosen_revision.why_better_notes || '',
        evidence_anchor: cleanAnchor(d.chosen_revision.evidence_anchor),
      };
      const tagAnchors = {};
      Object.keys(d.rejected_critique.error_tag_anchors || {}).forEach((tag) => {
        if (d.rejected_critique.error_tags.indexOf(tag) === -1) return;
        const a = cleanAnchor(d.rejected_critique.error_tag_anchors[tag]);
        if (a) tagAnchors[tag] = a;
      });
      payload.rejected_critique = {
        error_tags: d.rejected_critique.error_tags.slice(),
        severities: Object.assign({}, d.rejected_critique.severities),
        why_worse: d.rejected_critique.why_worse || '',
        error_tag_anchors: tagAnchors,
      };
      payload.reasoning_steps = cleanSteps(d.reasoning_steps);
      payload.from_scratch = null;
    } else if (d.verdict === 'both_inadequate') {
      payload.from_scratch = {
        ideal_answer: d.from_scratch.ideal_answer || '',
        approach_notes: d.from_scratch.approach_notes || '',
        reasoning_steps: cleanSteps(d.from_scratch.reasoning_steps),
        evidence_anchor: cleanAnchor(d.from_scratch.evidence_anchor),
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
    const tabs = [
      ['tasks', 'Tasks'],
      ['buyers', 'Buyers & Requests'],
      ['exports', 'Exports'],
      ['metrics', 'Metrics'],
    ];
    // QA Queue was removed from the nav; bounce any stale selection.
    if (state.adminTab === 'qa') state.adminTab = 'exports';
    const subnav = h('div', { class: 'asc-subnav' },
      tabs.map(([id, label]) => h('button', {
        class: 'asc-subnav-btn' + (state.adminTab === id ? ' active' : ''),
        onClick: () => { state.adminTab = id; renderAdminView(); },
      }, label)));

    const body = h('div', { id: 'ascAdminBody' });
    setRoot(h('div', { class: 'asc-wrap' }, subnav, body));

    if (state.adminTab === 'tasks') renderAdminTasks(body);
    else if (state.adminTab === 'buyers') renderAdminBuyers(body);
    else if (state.adminTab === 'exports') renderAdminExports(body);
    else if (state.adminTab === 'metrics') renderAdminMetrics(body);
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

    // Seedmaker auto-generation (Mode A, nephrology) — generate N validated
    // tasks (prompt + 2 candidates) grounded in the curated seed corpus.
    const agCount = h('input', { type: 'number', class: 'asc-input', value: '10', min: '1', max: '200' });
    const agDiff = selectFrom(['balanced', 'hard_heavy', 'hard_only'], 'balanced');
    const agGround = selectFrom(tax.grounding_modes || ['optional', 'required'], 'optional');
    const agCapture = h('input', { type: 'checkbox' });
    const agStatus = h('div', {});
    const agBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Generate nephrology tasks');
    const autoGenCard = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Auto-generate tasks (nephrology Seedmaker)'),
        h('div', { class: 'asc-card-sub' }, 'Synthesizes novel, hard nephrology prompts from the seed corpus + two candidate answers, quality-gated before they enter the queue. Needs an LLM key.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'How many'), agCount),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Difficulty mix'), agDiff),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Grounding'), agGround)),
        h('label', { class: 'asc-checkbox-row', style: 'margin-bottom:12px' }, agCapture, 'Capture reasoning steps'),
        agBtn,
        agStatus));
    agBtn.addEventListener('click', async () => {
      clear(agStatus);
      const count = Math.max(1, parseInt(agCount.value, 10) || 1);
      const mixMap = {
        balanced: { hard: 0.6, medium: 0.4 },
        hard_heavy: { hard: 0.8, medium: 0.2 },
        hard_only: { hard: 1.0 },
      };
      agBtn.setAttribute('disabled', '');
      agStatus.appendChild(loadingCard('Generating ' + count + ' task(s)… this calls the LLM and may take a moment.'));
      try {
        const res = await api('/generation/nephrology', {
          method: 'POST', body: {
            count, difficulty_mix: mixMap[agDiff.value] || null,
            capture_reasoning: agCapture.checked, grounding_mode: agGround.value,
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

    const corpusCard = h('div', { class: 'asc-card', id: 'ascSeedCorpus' }, loadingCard('Loading seed corpus…'));
    const jobsCard = h('div', { class: 'asc-card', id: 'ascGenJobs' }, loadingCard('Loading generation jobs…'));
    const tableCard = h('div', { class: 'asc-card', id: 'ascTasksTable' }, loadingCard('Loading tasks…'));

    body.appendChild(h('div', { class: 'asc-cols-2' }, pasteCard, fileCard));
    body.appendChild(genCard);
    body.appendChild(autoGenCard);
    body.appendChild(h('div', { class: 'asc-cols-2' }, corpusCard, jobsCard));
    body.appendChild(tableCard);
    loadTasksTable();
    loadSeedCorpus();
    loadGenerationJobs();
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
        const dsum = Object.keys(dropped).reduce((a, k) => a + (dropped[k] || 0), 0);
        return h('tr', {},
          h('td', {}, fmtDate(j.created_at)),
          h('td', {}, String(j.accepted) + ' / ' + String(j.requested_n)),
          h('td', {}, String(dsum)));
      });
      card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['When', 'Accepted / Requested', 'Dropped'].map((c) => h('th', {}, c)))),
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
        h('td', {}, t.difficulty || '—'),
        h('td', {}, (t.prompt || '').slice(0, 90) + ((t.prompt || '').length > 90 ? '…' : '')),
        h('td', {}, t.grounding_mode === 'required' ? h('span', { class: 'asc-badge asc-badge-amber' }, 'required') : 'optional'),
        h('td', {}, String(t.submission_count != null ? t.submission_count : 0)),
        h('td', {}, t.status === 'prompt_flagged'
          ? h('span', { class: 'asc-badge asc-badge-amber' }, '⚑ prompt flagged')
          : (t.status || '—'))));
      card.appendChild(h('div', { class: 'asc-table-wrap' },
        h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            ['ID', 'Specialty', 'Difficulty', 'Prompt', 'Grounding', 'Labels', 'Status'].map((c) => h('th', {}, c)))),
          h('tbody', {}, rows))));
    } catch (e) {
      clear(card);
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-error' }, e.message)));
    }
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

    const buyersListCard = h('div', { class: 'asc-card', id: 'ascBuyersList' }, loadingCard('Loading buyers…'));
    const reqCard = h('div', { class: 'asc-card', id: 'ascReqForm' }, loadingCard('Loading…'));
    const reqListCard = h('div', { class: 'asc-card', id: 'ascReqList' }, loadingCard('Loading requests…'));

    body.appendChild(h('div', { class: 'asc-cols-2' }, buyerCard, buyersListCard));
    body.appendChild(reqCard);
    body.appendChild(reqListCard);

    loadBuyersAndRequests();
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
          h('div', { class: 'asc-browse-name' }, '🏥 ' + o.organization),
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
      h('div', { class: 'asc-card-title' }, '🏥 ' + org)));
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
            '👩‍⚕️ ' + (c.display_name || c.id_hashed),
            c.credentials_verified ? h('span', { class: 'asc-badge asc-badge-green', style: 'margin-left:8px' }, 'verified ✓') : null),
          h('div', { class: 'asc-browse-meta' }, meta.join(' · '))),
        h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', onClick: open }, 'Open →')));
    });
  }

  function orgMetricTiles(contributors, org) {
    const sum = (k) => contributors.reduce((a, c) => a + (Number(c[k]) || 0), 0);
    const last = contributors.reduce((a, c) => (c.last_labeled_at && (!a || c.last_labeled_at > a) ? c.last_labeled_at : a), null);
    return h('div', { class: 'asc-stat-grid', style: 'margin-bottom:16px' },
      stat(contributors.length, 'Contributors'),
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
    pad.appendChild(h('div', { class: 'asc-profile-head' },
      h('div', { class: 'asc-profile-avatar' }, '👩‍⚕️'),
      h('div', {},
        h('div', { class: 'asc-profile-name' }, c.display_name || idHashed,
          cr.credentials_verified ? h('span', { class: 'asc-badge asc-badge-green', style: 'margin-left:10px' }, 'verified ✓') : null),
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

    const statusBox = h('div', { style: 'margin-top:14px' });
    pad.appendChild(h('div', { class: 'asc-profile-actions' },
      h('button', { class: 'asc-btn asc-btn-primary',
        onClick: (ev) => exportContributor(idHashed, statusBox, ev.target) }, '⬇ Export Data'),
      h('button', { class: 'asc-btn asc-btn-secondary',
        onClick: () => openCredentialSummaryModal(idHashed, c.display_name || idHashed) }, '🔒 Further Credential Summary')));
    pad.appendChild(h('p', { class: 'asc-label-hint', style: 'margin-top:8px' },
      'Export Data ships credential attributes only (no identifying info). Further Credential Summary releases the full verification dossier under NDA / non-circumvention.'));
    pad.appendChild(statusBox);
  }

  function renderContributorMetrics(pad, c) {
    clear(pad);
    if (!c) { pad.appendChild(h('div', { class: 'asc-inline-error' }, 'No contributor selected.')); return; }
    pad.appendChild(h('div', { class: 'asc-profile-head' },
      h('div', { class: 'asc-profile-avatar' }, '👩‍⚕️'),
      h('div', {},
        h('div', { class: 'asc-profile-name' }, c.display_name || c.id_hashed,
          c.credentials_verified ? h('span', { class: 'asc-badge asc-badge-green', style: 'margin-left:10px' }, 'verified ✓') : null),
        h('div', { class: 'asc-meta-row', style: 'margin-top:6px' },
          h('span', { class: 'asc-badge asc-badge-primary' }, c.role_title || c.role || '—'),
          h('span', { class: 'asc-badge asc-badge-gray' }, c.primary_specialty || c.specialty || '—')))));
    pad.appendChild(h('div', { class: 'asc-stat-grid', style: 'margin-top:14px' },
      stat(c.submission_count || 0, 'Submissions'),
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
      h('div', { class: 'call-team-title' }, '🔒 Further Credential Summary'),
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
      if (!res.ok) { toast('Download failed (' + res.status + ')', 'error'); return; }
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
      const rows = exports.map((x) => h('tr', {},
        h('td', { class: 'asc-mono' }, (x.export_id || '').slice(0, 12)),
        h('td', {}, x.profile || '—'),
        h('td', {}, String(x.record_count != null ? x.record_count : (x.count != null ? x.count : '—'))),
        h('td', {}, fmtDate(x.created_at)),
        h('td', {}, h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', onClick: () => downloadExport(x.export_id) }, '⬇ Download'))));
      card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {}, ['ID', 'Profile', 'Records', 'Created', ''].map((c) => h('th', {}, c)))),
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
    const tiles = h('div', { class: 'asc-stat-grid' },
      stat(s.task_count != null ? s.task_count : 0, 'Tasks'),
      stat(sumValues(sc), 'Submissions'),
      stat((qpr.pass_rate != null ? Math.round(qpr.pass_rate * 100) : 0) + '%', 'QA pass rate', (qpr.passed || 0) + ' / ' + (qpr.reviewed || 0) + ' reviewed'),
      stat(fmtNum(s.average_agreement), 'Avg agreement'),
      stat(fmtNum(kappa.overall), "Cohen's κ", 'n=' + (kappa.n != null ? kappa.n : 0)),
      stat((grounded.grounded_pct != null ? grounded.grounded_pct : 0) + '%', 'Grounded', (grounded.submissions_grounded || 0) + ' / ' + (grounded.submissions_total || 0)),
      stat(flaw.rate != null ? Math.round(flaw.rate * 100) + '%' : '—', 'Flaw catch rate', (flaw.caught || 0) + ' / ' + (flaw.scored || 0) + ' generated'),
      stat(s.export_count != null ? s.export_count : 0, 'Exports'));

    body.appendChild(h('div', { class: 'asc-card asc-card-pad' },
      h('div', { class: 'asc-card-title', style: 'margin-bottom:14px' }, 'Overview'), tiles));

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

  function stat(value, label, sub) {
    return h('div', { class: 'asc-stat' },
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
  function fmtDate(d) {
    if (!d) return '—';
    const dt = new Date(d);
    if (isNaN(dt.getTime())) return String(d);
    return dt.toLocaleString();
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
