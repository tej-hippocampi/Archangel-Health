/* ═══════════════════════════════════════════════════════════════════════════
   Asclepius Expert Review — the PAIRED adjudication surface.

   window.AsclepiusReview { render, teardown }

   A senior physician draws a CASE with two independent labels side by side,
   decides which is stronger and whether either is right, and submits. The whole
   product thesis is that a good pair is accepted in under sixty seconds, so the
   case is folded away until doubted and the judgment controls are pinned one
   glance from the answers.

   THIS IS A MODULE, NOT A PAGE (PRD-1 §2.1). It used to be a standalone
   `review.html` opened with `window.open(..., '_blank')`: its own hyperscript,
   its own token read, its own sign-in form, its own class namespace, no rail,
   and no way back. A reviewer landed in a second application that happened to
   share a stylesheet. It now renders inside the shell, through the same
   `render(el, ctx)` contract the admin sections use, so the rail — Community,
   Guide, Earnings — stays one click away and a finished pair lands the reviewer
   back on their dashboard instead of a dead tab.

   Everything structural comes from the host through `ctx`:
     h, clear      the SHELL's hyperscript. This file no longer has its own.
                   Two hyperscripts on one page is how `onClick` registers a
                   listener for the event type "Click" on one surface and
                   "click" on the other.
     api           the shell's fetch helper, which owns the token and the 401.
     casePanelCtx  what `case_panel.js` needs to draw the clinical chart — the
                   SAME component the labeler reads (§2.2).
     preview       true when an admin is sightseeing (§4.1). Nothing is recorded.
     goHome        where "Back to dashboard" goes.

   Design decisions that are load-bearing, not cosmetic:

     - BOTH CARDS ARE GREEN. On the labeler page the two side-by-side cards are
       frontier model answers and are orange; here they are two physicians' work.
       The accent carries meaning in this product.
     - A AND B ARE NOT DIFFERENT COLOURS. They are told apart by the mono eyebrow
       and by position. A reviewer who reads hue as "which doctor" starts reading
       hue as "which is better", and that biases the adjudication.
     - `.asc-answers` CONTAINS EXACTLY TWO CHILDREN. A third lands in cell 2 and
       pushes B to row 2. Chrome goes outside the grid.
     - CORRECTIONS ARE REVEALED, NOT ALWAYS PRESENT. An empty textarea under
       every review invites the reviewer to feel they owe prose on an accept.
       They don't.
     - THE COUNTDOWN IS LIME AND ITS VALUE COMES FROM THE SERVER. Lime means
       "needs attention"; pink means critical, and a running clock is not an
       emergency. Under two minutes the COPY changes, not the colour.
     - THE CASE IS THE CHART, NOT A JSON DUMP. Adjudicating a trajectory (a GGT
       falling 1361 → 237 → 123 → 62 against a bilirubin that rose) out of
       `JSON.stringify` is why review used to be slow. Same panel as the labeler,
       same module, never a fork.

   All DOM is built with the host's hyperscript. No HTML string assignment
   anywhere — every server string reaches the page as a text node.

   The session is Agent P's from end to end. The draw response carries P's
   session block, this module hands it to P's heartbeat client
   (`window.AsclepiusSession`), and every second it renders is read back from
   that client's server-attested state. It computes no time of its own: a
   client-side clock that disagreed with the server's would be telling a
   physician they had earned money they had not. When no heartbeat client is on
   the page, the clock says so in words rather than showing a number.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // ── module state ────────────────────────────────────────────────────────────
  var CTX = null;       // the host's { h, clear, api, ... }
  var HOST = null;      // the element we own
  var ME = null;        // /review/me: vocabulary + can_review
  var PAIR = null;      // the blinded pair being adjudicated
  var SESSION = null;   // Agent P's session block, opaque, server-owned
  var STATS = null;
  var R = null;         // the adjudication being authored
  var RESTORED = null;  // a judgment recovered from a previous sitting
  var DIVERGENCE = [];  // the aligned reasoning-step forks (§2.3 / §3)
  var PREVIEW = false;  // admin sightseeing; nothing is recorded (§4.1)
  var clockTimer = null;
  var keyHandler = null;
  var focusedDim = 0;   // which dimension the 1-4 / arrow keys are aimed at

  /* THE GENERATION. Every draw is asynchronous and every render replaces the
     host element, so a response can arrive for a screen that no longer exists.
     Bumped by render() and by teardown(); every `.then` checks it and returns.

     Without it: navigate to the Guide mid-draw and `teardown` has already set
     HOST to null, so the resolving promise calls clear(null) and throws inside
     a handler nobody is listening to. And two overlapping draws would each
     CLAIM a pair server-side, stranding the first one behind a 45-minute lease
     while the reviewer looks at the second.

     `drawing` is the same idea one level down: it stops a double-click on Retry
     or "Check again" from issuing two draws in the first place. */
  var GENERATION = 0;
  var drawing = false;
  function stale(gen) { return gen !== GENERATION; }

  function h() { return CTX.h.apply(null, arguments); }
  function clear(el) { return CTX.clear(el); }
  function api(path, opts) { return CTX.api(path, opts); }
  function errText(e) {
    return (e && (e.message || e.detail)) || 'Something went wrong.';
  }

  function mount() {
    // Belt to the generation's braces. A render that has been torn down owns no
    // element, and clearing null is a TypeError inside a promise handler.
    if (!HOST) return;
    clear(HOST);
    for (var i = 0; i < arguments.length; i++) {
      var kid = arguments[i];
      if (kid) HOST.appendChild(kid);
    }
  }

  /* ── the judgment survives a refresh ──────────────────────────────────────
     R lived only in memory, so a stray reload mid-adjudication threw away a
     senior physician's reading of a hard pair. That is tolerable for four
     segmented controls and stops being tolerable the moment the form is worth
     filling in, which is what the richer card above makes it.

     Storage belongs to the SHELL, not here: this module having its own
     localStorage read was one of the reasons review was structurally a
     different application, and a test pins that it has not grown one back. So
     it asks through the ctx, exactly as it asks for `h` and `api`. A shell
     that does not provide `drafts` simply gets the old in-memory behaviour. */
  function drafts() {
    return (CTX && CTX.drafts) || null;
  }
  function saveJudgment() {
    var d = drafts();
    if (!d || !PAIR || !PAIR.task_id || !R) return;
    // Never persist a preview: an operator sightseeing must leave no trace a
    // real reviewer could resume into.
    if (PREVIEW) return;
    d.save(PAIR.task_id, {
      verdict: R.verdict, stronger: R.stronger, acceptedSide: R.acceptedSide,
      dimensions: R.dimensions, startedAt: R.startedAt,
      notes: notesArea ? notesArea.value : '',
      edited: editedArea ? editedArea.value : '',
    });
  }
  function clearJudgment(taskId) {
    var d = drafts();
    if (d && taskId) d.clear(taskId);
  }
  function resetReview() {
    R = { verdict: null, stronger: null, acceptedSide: null, dimensions: {},
          startedAt: Date.now() };
    DIVERGENCE = [];
    focusedDim = 0;
    RESTORED = null;
    var d = drafts();
    if (!PREVIEW && d && PAIR && PAIR.task_id) {
      var saved = d.load(PAIR.task_id);
      if (saved && typeof saved === 'object') {
        R.verdict = saved.verdict || null;
        R.stronger = saved.stronger || null;
        R.acceptedSide = saved.acceptedSide || null;
        R.dimensions = (saved.dimensions && typeof saved.dimensions === 'object')
          ? saved.dimensions : {};
        // startedAt is NOT restored. It measures time on this case in this
        // sitting, and a resumed draft from yesterday would bill the gap.
        RESTORED = saved;
      }
    }
  }

  // ── the session countdown ───────────────────────────────────────────────────
  //
  // THIS MODULE COMPUTES NO TIME. Every number below comes from Agent P's
  // heartbeat client (`window.AsclepiusSession`), which is the only thing that
  // talks to the server about a session and the only thing entitled to say how
  // many seconds have been counted.
  //
  // It used to seed from the draw response and then add wall-clock drift, with
  // no heartbeat ever sent. At 20:00 it rendered "this session has met its
  // minimum" while the server had counted ZERO seconds. Under a "20 continuous
  // minutes or $0" structure that is not a display bug — it is the page telling
  // a physician they have been paid for work that will not be paid for. A clock
  // this module can advance on its own IS that bug, whatever it is seeded with.
  function sessionClient() {
    var S = (typeof window !== 'undefined') && window.AsclepiusSession;
    return (S && typeof S.state === 'function') ? S : null;
  }
  function sessionState() {
    var S = sessionClient();
    if (!S) return null;
    try { return S.state() || null; } catch (e) { return null; }
  }

  // Tell Agent P's client there is no longer any work on screen.
  //
  // P's client beats until it is told to stop or the tab hides. THE EMPTY QUEUE
  // IS SOMETHING ONLY THIS SURFACE KNOWS: when the queue drains we drop SESSION
  // and hide the clock, and a reviewer left idling on that screen went on
  // accruing paid time while — correctly — being unable to see it. Twenty
  // continuous minutes of that is $100 nobody worked for.
  //
  // Called at every no-work transition, not just the empty queue: an error
  // screen, a teardown and a signed-out page are the same shape. ``stop`` is
  // idempotent and settles through the normal close path, so a session that
  // already earned its minimum is still paid. Feature-detected like every call
  // across this seam — this boundary has already produced one silent failure
  // from a method that was guarded, present in the guard, and simply not there.
  function stopSession(reason) {
    var S = (typeof window !== 'undefined') && window.AsclepiusSession;
    if (!S || typeof S.stop !== 'function') return;
    try { S.stop(reason); } catch (e) { /* never block the reviewer */ }
  }
  function mmss(total) {
    var s = Math.max(0, Math.floor(total));
    var m = Math.floor(s / 60);
    return String(m) + ':' + (s % 60 < 10 ? '0' : '') + String(s % 60);
  }

  // The state a reviewer must never be left to guess at: a session is open
  // server-side, and nothing on this page is reporting time to it.
  var UNTIMED_CLOCK = 'Session · not being timed';
  var UNTIMED_NOTE =
    'This review is not accruing paid time. Reload the page; if it persists, ' +
    'tell us before you carry on.';

  function clockText() {
    if (!SESSION) return null;            // no session opened: nothing to show
    var st = sessionState();
    if (!st) return UNTIMED_CLOCK;
    var elapsed = Number(st.continuous_seconds);
    var target = Number(st.min_seconds);
    if (!isFinite(elapsed) || !isFinite(target)) return UNTIMED_CLOCK;
    return 'Session · ' + mmss(elapsed) + ' of ' + mmss(target);
  }
  function clockNote() {
    if (!SESSION) return null;
    var st = sessionState();
    if (!st) return UNTIMED_NOTE;
    var elapsed = Number(st.continuous_seconds);
    var target = Number(st.min_seconds);
    if (!isFinite(elapsed) || !isFinite(target)) return UNTIMED_NOTE;
    // Only the SERVER says a session has met its minimum. Elapsed time reaching
    // the target is a different claim — a session can run past its minimum and
    // still not count — and guessing here is the original defect in miniature.
    if (st.qualified) return 'This session has met its minimum.';
    var left = target - elapsed;
    // Under two minutes the COPY changes. The colour does not — pink means
    // critical and blocking, and a running clock is neither.
    if (left > 0 && left <= 120) return mmss(left) + ' left before this session qualifies for payment.';
    return null;
  }

  var clockEl = null;
  var clockNoteEl = null;
  function paintClock() {
    if (clockEl) {
      var text = clockText();
      if (text !== null) { clear(clockEl); clockEl.appendChild(document.createTextNode(text)); }
    }
    if (clockNoteEl) {
      clear(clockNoteEl);
      clockNoteEl.appendChild(document.createTextNode(clockNote() || ''));
    }
  }
  function startClock() {
    stopClock();
    // A REPAINT, not a clock: it re-reads the heartbeat client's state. If that
    // state stops moving because the server stopped counting, so do the digits.
    if (SESSION) clockTimer = setInterval(paintClock, 1000);
  }
  function stopClock() {
    if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
  }

  // ── boot ────────────────────────────────────────────────────────────────────
  function boot() {
    var gen = GENERATION;
    renderLoading();
    api('/review/me').then(function (me) {
      if (stale(gen)) return;
      ME = me;
      if (!me.can_review) { renderNotReviewer(); return; }
      // §4.1. The SERVER decides whether this account is a real reviewer or an
      // operator reaching the surface through the admin override — that is a
      // tier question, and the tier table is not this module's to interpret. An
      // explicit preview (the Evaluate chooser) can only ever turn preview ON.
      if (me.preview_only) PREVIEW = true;
      loadNext();
    }).catch(function (err) {
      if (stale(gen)) return;
      if (err && err.status === 401) return;   // the shell owns the bounce
      renderFatal(errText(err));
    });
  }

  function loadNext() {
    // One draw at a time. A second click on Retry / "Check again" would
    // otherwise claim a second pair and abandon the first for its whole lease.
    if (drawing) return;
    drawing = true;
    var gen = GENERATION;
    renderLoading();
    Promise.all([
      api('/review/pair/next' + (PREVIEW ? '?preview=true' : '')),
      api('/review/stats').catch(function () { return null; }),
    ]).then(function (results) {
      // AFTER the staleness check, not before. `drawing` belongs to whichever
      // generation is currently drawing; a stale reply clearing it would unlock
      // the flag while the CURRENT draw is still in flight, and the next click
      // would claim a second pair — the exact thing the flag exists to stop.
      // A generation that ends without its reply has `drawing` cleared by
      // render() or teardown(), which is where that responsibility belongs.
      if (stale(gen)) return;
      drawing = false;
      PAIR = results[0].pair;
      // The server is the authority on whether this draw is a preview. The
      // client ASKED for one; only the response says it got one, and the banner
      // has to describe what actually happened.
      //
      // STICKY. Preview is only ever turned ON — by the chooser, by the server's
      // `preview_only`, or by the served pair. Recomputing it from each response
      // would clear it on an empty queue, and the NEXT draw would go out without
      // `?preview=true`.
      if ((PAIR && PAIR.preview) || results[0].preview) PREVIEW = true;
      SESSION = results[0].session || null;
      // Hand the server's session to Agent P's client and then leave it alone.
      // R decides what a reviewer sees; P decides whether the time is paid for.
      //
      // The second argument NAMES THE WORK. A heartbeat that names nothing is
      // not evidence of anything, and only this surface knows what a unit of
      // review work is — if payments had to know what a review pair is, the
      // boundary has slipped (PRD-P §8). Without it every beat reads
      // `session:<id>`, per-case accounting becomes unanswerable, and the
      // per-key credit ceiling has nothing to bind to.
      if (SESSION) {
        var S = sessionClient();
        if (S && typeof S.start === 'function') {
          // Idempotent on P's side, so calling it on every draw is how the key
          // follows the reviewer from one case to the next.
          try { S.start(SESSION, PAIR && PAIR.task_id); } catch (e) { /* never block the review */ }
        }
      }
      STATS = results[1];
      resetReview();
      if (!PAIR) { stopSession('queue_empty'); renderEmpty(results[0].message); return; }
      renderReview();
      startClock();
    }).catch(function (err) {
      if (stale(gen)) return;
      drawing = false;
      if (err && err.status === 401) return;
      renderFatal(errText(err));
    });
  }

  // ── chrome ──────────────────────────────────────────────────────────────────
  function header() {
    var kids = [h('h2', { class: 'asc-dash-hello' }, 'Review')];
    var specialty = (ME && ME.user && ME.user.specialty) || null;
    if (specialty) kids.push(h('span', { class: 'asc-rv-chrome' }, specialty));
    var text = clockText();
    if (text !== null) {
      clockEl = h('span', { class: 'asc-session-clock' }, text);
      kids.push(clockEl);
    } else {
      clockEl = null;
    }
    if (STATS) {
      kids.push(h('div', { class: 'asc-rv-headstats' },
        h('span', { class: 'asc-rv-chrome' }, 'Pairs ready ' + (STATS.review_ready || 0)),
        h('span', { class: 'asc-rv-chrome' }, 'Awaiting 2nd ' + (STATS.awaiting_second || 0)),
        h('span', { class: 'asc-rv-chrome' }, 'Adjudicated ' + (STATS.adjudicated || 0))));
    } else {
      kids.push(h('div', { class: 'asc-rv-headstats' }));
    }
    return h('div', { class: 'asc-rv-header' }, kids);
  }

  /* §4.1. An admin previewing the reviewer surface must be told, PERSISTENTLY,
     that nothing they do here is recorded — so it rides on every screen this
     module can be on, not only the one with a pair on it. The server refuses a
     preview submit with a 409 regardless; this banner is so the refusal is never
     a surprise. */
  function previewBanner() {
    if (!PREVIEW) return null;
    return h('div', { class: 'asc-rv-preview' },
      h('span', { class: 'asc-rv-chrome' }, 'Preview'),
      h('span', {}, 'Preview — nothing you submit here is recorded.'));
  }

  function renderNotReviewer() {
    mount(header(), previewBanner(), h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('p', {}, 'This account does not have the reviewer tier yet.'),
      h('p', { class: 'asc-rv-kv' },
        'Reviewer access is assigned after verification. You can keep labeling '
        + 'from your dashboard.'))));
  }

  function renderLoading() {
    mount(header(), previewBanner(), h('div', { class: 'asc-empty' }, 'Loading…'));
  }

  function renderFatal(message) {
    // An error screen is not work either.
    stopSession('error');
    stopClock();
    mount(header(), previewBanner(), h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-inline-error' }, message || 'Something went wrong.'),
      h('div', { class: 'asc-rv-actions' },
        h('button', { class: 'asc-btn asc-btn-primary', type: 'button', onClick: loadNext },
          'Retry')))));
  }

  function renderEmpty(message) {
    stopClock();
    var pad = h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-empty' },
        h('p', {}, message || 'No cases awaiting review.'),
        h('div', { class: 'asc-rv-actions asc-rv-actions-center' },
          h('button', { class: 'asc-btn asc-btn-primary', type: 'button', onClick: loadNext },
            'Check again'))));
    var card = h('div', { class: 'asc-card' }, pad);
    // Reviewers are physicians too — a case waiting for its second label is
    // work they can take, and the whole point of the priority rule is that it
    // gets taken.
    var gen = GENERATION;
    api('/review/double-label/next').then(function (data) {
      if (stale(gen)) return;
      if (data && data.task && HOST && HOST.contains && HOST.contains(pad)) {
        pad.appendChild(h('p', { class: 'asc-rv-kv asc-rv-kv-lead' },
          'A case is waiting for its second independent label (specialty: '
          + (data.task.specialty || 'any') + ') — open it from your dashboard.'));
      }
    }).catch(function () { /* pointer is best-effort */ });
    mount(header(), previewBanner(), card);
  }

  // ── the case, collapsed by default ──────────────────────────────────────────
  //
  // The fold-away default is kept: the case is folded until doubted. What is
  // behind the fold changed completely. It used to be `JSON.stringify(value,
  // null, 1)` for labs, studies and medications — you cannot adjudicate a lab
  // TRAJECTORY out of a JSON dump, and asking a senior physician to try is most
  // of the reason review took longer than labeling. It is now the labeler's own
  // tabbed chart, from the shared module, so the two physicians provably read
  // the same document.
  function caseSection(task) {
    var kids = [];
    var mod = window.AsclepiusCasePanel;
    var c = task.case || null;
    if (c && mod && typeof mod.render === 'function' && CTX.casePanelCtx) {
      var panel = mod.render(CTX.casePanelCtx(), {
        case: c,
        specialty: (c.specialty || task.specialty || 'nephrology'),
        specialties: CTX.specialties || null,
        tabKey: 'review:' + (task.task_id || ''),
      });
      if (panel) kids.push(panel);
    }
    if (c && !kids.length) {
      // A structured case we could not draw is a VISIBLE failure, never a
      // silent omission: a missing chart looks exactly like a text-only task.
      kids.push(h('div', { class: 'asc-inline-error' },
        'The clinical chart failed to load. Reload before adjudicating this pair.'));
    }
    var candidates = task.candidate_answers || [];
    if (candidates.length) {
      kids.push(h('div', { class: 'asc-rv-candidates' },
        h('div', { class: 'asc-rv-kv' },
          h('b', {}, 'Candidate answers shown to both physicians')),
        candidates.map(function (cand) {
          return h('div', { class: 'asc-rv-candidate' },
            h('span', { class: 'asc-chip' }, 'Candidate ' + String(cand.id || '').toUpperCase()),
            h('div', { class: 'asc-rv-answer-text' }, cand.text || ''));
        })));
    }
    if (!kids.length) return null;
    return h('details', { class: 'asc-rv-case' },
      h('summary', {}, 'The case — open only if you doubt something'),
      h('div', { class: 'asc-rv-case-body' }, kids));
  }

  // ── reasoning-step divergence (§2.3 / §3) ───────────────────────────────────
  //
  // The reviewer's attention belongs at the FORK, not at the eight steps where A
  // and B agree. Today the labeler produces step-level signal and the reviewer
  // produces one verdict, so a senior physician's judgment lands as a single bit
  // on a record carrying dozens. Marking the divergences — and which side was
  // right at each — makes the reviewer a second source of PROCESS-level
  // supervision, which is the signal a process-reward buyer is paying for.
  //
  // The comparison is deliberately dumb and explainable: normalise whitespace,
  // case and terminal punctuation, then compare position by position. A fuzzy
  // aligner would be more impressive and would silently call two different
  // clinical steps "the same". Where one physician wrote more steps than the
  // other, the surplus positions are forks too — a step only one of them took is
  // exactly a divergence.
  function stepText(s) {
    if (s === null || s === undefined) return '';
    if (typeof s === 'string') return s;
    return String((s.text || s.content || s.step || ''));
  }
  function normalizeStep(text) {
    return String(text || '')
      .toLowerCase()
      .replace(/[\s]+/g, ' ')
      .replace(/[.,;:!?]+$/g, '')
      .trim();
  }
  /* The steps a labeler actually submitted, from either shape.

     An EMPTY array falls through to `from_scratch`, exactly as
     `review.submission_reasoning_steps` does server-side. The two must agree
     about what "this physician produced reasoning" means, or the client offers
     forks the server then refuses as a fabrication — or, worse, stays silent
     about a divergence the server would have accepted. */
  function stepsOf(entry) {
    var a = (entry && entry.answer) || {};
    var raw = a.reasoning_steps;
    if (!Array.isArray(raw) || !raw.length) {
      raw = (a.from_scratch && a.from_scratch.reasoning_steps) || null;
    }
    return Array.isArray(raw) ? raw : [];
  }
  /* The forks between two step lists, as
     [{ index, a, b, judged }] — `judged` is filled in by the reviewer.
     Returns [] when both sides carried steps and none of them diverged (a real
     measurement), and null when either side carried none (nothing was
     compared, so there is nothing honest to report). */
  function computeDivergence(stepsA, stepsB) {
    if (!stepsA.length || !stepsB.length) return null;
    var out = [];
    var n = Math.max(stepsA.length, stepsB.length);
    for (var i = 0; i < n; i++) {
      var ta = i < stepsA.length ? stepText(stepsA[i]) : null;
      var tb = i < stepsB.length ? stepText(stepsB[i]) : null;
      if (ta === null || tb === null || normalizeStep(ta) !== normalizeStep(tb)) {
        out.push({ index: i, a: ta, b: tb, judged: null });
      }
    }
    return out;
  }
  function divergentIndices() {
    var set = {};
    (DIVERGENCE || []).forEach(function (d) { set[d.index] = true; });
    return set;
  }

  // ── one physician's card ────────────────────────────────────────────────────
  function verdictLabel(v) {
    if (v === 'A_better') return 'Chose candidate A';
    if (v === 'B_better') return 'Chose candidate B';
    if (v === 'both_inadequate') return 'Both inadequate — wrote from scratch';
    return v || 'no verdict';
  }
  function candidateText(task, id) {
    var found = (task.candidate_answers || []).filter(function (c) { return c.id === id; })[0];
    return found ? (found.text || '') : '';
  }
  function section(title, body) {
    return h('div', { class: 'asc-answer-section' },
      h('div', { class: 'asc-rv-kv' }, h('b', {}, title)), body);
  }
  function fold(title, body) {
    return h('details', { class: 'asc-rv-fold' },
      h('summary', { class: 'asc-rv-kv' }, title), body);
  }

  /* ── evidence anchors ────────────────────────────────────────────────────
     A citation divorced from the claim it supports is not reviewable, so
     anchors are rendered UNDER the thing they ground rather than collected in
     a "Citations" fold at the bottom of the card.

     There was such a fold, keyed on a top-level `citations` field. No
     submission has ever carried one: citations live nested as
     `evidence_anchor` (the back-compat singular alias) and `evidence_anchors`
     on the revision, the from-scratch answer, the blind answer, every
     reasoning step, every rubric row, and per error tag. So a reviewer had
     never seen a single citation a labeler entered, while one of the four
     things they grade is rubric quality. */
  function anchorsOf(obj) {
    if (!obj || typeof obj !== 'object') return [];
    var out = [];
    if (Array.isArray(obj.evidence_anchors)) out = out.concat(obj.evidence_anchors);
    if (obj.evidence_anchor) out.push(obj.evidence_anchor);
    // The singular is an alias for [0]; drop the duplicate it creates.
    var seen = {}, uniq = [];
    out.forEach(function (an) {
      if (!an || typeof an !== 'object') return;
      var key = [an.citation_text || '', an.identifier || '', an.url || ''].join('|');
      if (key === '||' || seen[key]) return;
      seen[key] = true;
      uniq.push(an);
    });
    return uniq;
  }
  function anchorLine(an) {
    var bits = [an.citation_text || '', an.identifier || '', an.source_type || '']
      .filter(Boolean).join(' \u00b7 ');
    var kids = [bits || (an.url || '')];
    if (an.citation_confirmed) kids.push(h('span', { class: 'asc-chip asc-rv-chip-gap' }, 'confirmed'));
    return h('div', { class: 'asc-rv-kv' }, kids);
  }
  /* The claim, then its sources directly beneath it. */
  function grounded(node, obj) {
    var anchors = anchorsOf(obj);
    if (!anchors.length) return node;
    return h('div', {}, node,
      h('div', { class: 'asc-rv-citations' }, anchors.map(anchorLine)));
  }
  function chips(values) {
    return h('div', {}, (values || []).map(function (t) {
      return h('span', { class: 'asc-chip asc-rv-chip-gap' }, String(t));
    }));
  }
  /* {key: value} maps -- severities, error_tag_reasons. Rendered as
     "tag: value" pairs rather than raw JSON, which is what a reviewer can
     actually read at speed. */
  function pairs(map) {
    if (!map || typeof map !== 'object') return null;
    var keys = Object.keys(map);
    if (!keys.length) return null;
    return h('div', {}, keys.map(function (k) {
      return h('span', { class: 'asc-chip asc-rv-chip-gap' }, k + ': ' + String(map[k]));
    }));
  }

  /* The sealed prediction (PRD 2 §3.3 field 3): what they expect to see, and
     what would tell them they were wrong.

     Its own renderer rather than `pairs`, which would print the expectations
     list as "[object Object]" — technically visible, actually unread, which is
     the exact failure mode label_view exists to prevent. The falsifier gets its
     own line because it is the part a reviewer is really grading: anyone can
     predict improvement, and naming what would refute you is the skill. */
  function trajectory(et) {
    if (!et || typeof et !== 'object') return null;
    var kids = [];
    var exps = et.expectations || [];
    if (exps.length) {
      kids.push(h('div', {}, exps.map(function (e) {
        var txt = (e && e.expectation) || String(e || '');
        var days = e && e.horizon_days;
        return h('div', { class: 'asc-rv-kv' },
          txt + (days ? ' — within ' + days + ' day' + (days === 1 ? '' : 's') : ''));
      })));
    }
    var fals = et.falsifiers || [];
    if (fals.length) {
      kids.push(h('div', { class: 'asc-rv-eyebrow' }, 'Would change their mind'));
      kids.push(h('div', {}, fals.map(function (f) {
        return h('div', { class: 'asc-rv-kv' }, String(f));
      })));
    }
    return kids.length ? h('div', {}, kids) : null;
  }

  /* One physician's answer. Identical structure for A and B, identical accent,
     distinguished ONLY by the mono eyebrow and by which column it is in. */
  function answerCard(entry, task, forks) {
    var a = entry.answer || {};
    var body = [];

    body.push(h('div', {},
      h('span', { class: 'asc-chip' }, verdictLabel(a.verdict)),
      ' ',
      entry.confidence ? h('span', { class: 'asc-chip' }, 'confidence: ' + entry.confidence) : null));

    // Stage 1: their read on the QUESTION. Never reached the reviewer before,
    // though a `valid` verdict is what upgrades a record's provenance from
    // AI-drafted to clinician-reviewed.
    var pr = a.prompt_review || null;
    if (pr && (pr.verdict || pr.note)) {
      var prKids = [];
      if (pr.verdict) prKids.push(h('span', { class: 'asc-chip' }, 'question: ' + pr.verdict));
      if (pr.note) prKids.push(h('div', { class: 'asc-rv-kv' }, pr.note));
      body.push(section('Their read on the question', prKids));
    }

    var rev = a.chosen_revision || null;
    var finalText = null;
    if (rev && rev.edited && rev.revised_text) {
      finalText = rev.revised_text;
      body.push(section('Corrected answer (as submitted)',
        grounded(h('div', { class: 'asc-rv-answer-text' }, rev.revised_text), rev)));
    } else if (a.verdict === 'A_better' || a.verdict === 'B_better') {
      finalText = candidateText(task, a.chosen_id);
      body.push(section('Chosen answer (unedited)',
        h('div', { class: 'asc-rv-answer-text' }, finalText)));
    }
    var et = trajectory(a.expected_trajectory);
    if (et) body.push(section('What they expect to happen next', et));

    if (rev && ((rev.why_better_tags || []).length || rev.why_better_notes)) {
      var whyKids = [];
      if ((rev.why_better_tags || []).length) whyKids.push(chips(rev.why_better_tags));
      if (rev.why_better_notes) whyKids.push(h('div', { class: 'asc-rv-kv' }, rev.why_better_notes));
      body.push(section('Why it is better', whyKids));
    }

    var fs = a.from_scratch || null;
    if (fs && fs.ideal_answer) {
      finalText = fs.ideal_answer;
      body.push(section('Written from scratch',
        grounded(h('div', { class: 'asc-rv-answer-text' }, fs.ideal_answer), fs)));
      if (fs.approach_notes) body.push(h('div', { class: 'asc-rv-kv' }, 'Approach: ' + fs.approach_notes));
    }

    var crit = a.rejected_critique || null;
    if (crit && (crit.why_worse || (crit.error_tags || []).length
                 || (crit.failure_tags || []).length)) {
      var critKids = [];
      if ((crit.error_tags || []).length) critKids.push(chips(crit.error_tags));
      // How bad, and why. Captured all along, never shown.
      var sev = pairs(crit.severities);
      if (sev) critKids.push(sev);
      var reasons = pairs(crit.error_tag_reasons);
      if (reasons) critKids.push(reasons);
      if (crit.why_worse) critKids.push(h('div', { class: 'asc-rv-kv' }, crit.why_worse));
      // The Model-Failure Taxonomy: a named export SKU, invisible here until now.
      (crit.failure_tags || []).forEach(function (ft) {
        if (!ft || typeof ft !== 'object') return;
        critKids.push(h('div', { class: 'asc-rv-kv' },
          h('span', { class: 'asc-chip asc-rv-chip-gap' }, String(ft.mode || 'other')),
          ft.tier ? h('span', { class: 'asc-chip asc-rv-chip-gap' }, String(ft.tier)) : null,
          ft.note ? h('span', {}, ' ' + String(ft.note)) : null));
      });
      // Anchors keyed per error tag.
      var tagAnchors = crit.error_tag_anchors || {};
      Object.keys(tagAnchors).forEach(function (tag) {
        var an = tagAnchors[tag];
        if (an) critKids.push(h('div', { class: 'asc-rv-citations' },
          h('div', { class: 'asc-rv-kv' }, tag), anchorLine(an)));
      });
      body.push(section('Critique of the rejected answer', critKids));
    }

    var steps = stepsOf(entry);
    if (steps.length) {
      body.push(section('Reasoning steps', steps.map(function (s, i) {
        var text = stepText(s);
        var note = s && (s.note || s.step_note);
        // The fork mark. Same class on both columns: it says "A and B parted
        // company here", never "this column is the wrong one".
        var diverges = !!forks[i];
        var meta = [];
        // What the physician DID to this step, which is the difference between
        // endorsing the model and rewriting it.
        if (s && s.added) meta.push('added');
        else if (s && s.corrected) meta.push('corrected');
        else if (s && s.confirmed) meta.push('confirmed');
        if (s && s.label) meta.push(String(s.label));
        if (s && s.step_error_tag) meta.push(String(s.step_error_tag));
        if (s && s.correction_reason) meta.push(String(s.correction_reason));
        var detail = [];
        if (meta.length) detail.push(chips(meta));
        // The model's original, so the reviewer sees what was corrected rather
        // than only the corrected result.
        if (s && s.corrected && s.original_text) {
          detail.push(h('div', { class: 'asc-rv-kv' }, 'was: ' + String(s.original_text)));
        }
        if (note) detail.push(h('div', { class: 'asc-rv-kv' }, 'note: ' + note));
        if (s && s.critique) detail.push(h('div', { class: 'asc-rv-kv' }, String(s.critique)));
        return h('div', {
          class: 'asc-rv-step' + (diverges ? ' asc-rv-step-fork' : ''),
          dataset: { stepIdx: String(i) },
        },
          h('span', { class: 'asc-rv-step-num' }, String(i + 1)),
          h('span', {}, String(text), grounded(h('div', {}, detail), s)));
      })));
    }

    var rubric = a.rubric || [];
    if (rubric.length) {
      body.push(section('Grading rubric (as confirmed)', rubric.map(function (cr) {
        var pts = (cr && cr.points != null) ? cr.points : '';
        var tags = [];
        // Criticality and axes decide how the rubric SCORES, and the grader
        // hard-fails on a critical negative. A reviewer grading "rubric
        // quality" was shown neither.
        if (cr && cr.tier) tags.push(String(cr.tier));
        (cr && cr.axes ? cr.axes : []).forEach(function (ax) { tags.push(String(ax)); });
        if (cr && cr.specific === false) tags.push('not machine-checkable');
        return h('div', { class: 'asc-rv-step' },
          h('span', { class: 'asc-rv-step-num' }, String(pts)),
          h('span', {}, (cr && (cr.text || cr.criterion)) || '',
            tags.length ? chips(tags) : null,
            grounded(h('div', {}), cr)));
      })));
    }

    var ia = a.independent_answer || null;
    if (ia && ia.text) {
      // `kind` matters: a ten-second `stance` and a `full` blind ideal answer
      // render identically without it, and they are not the same work.
      body.push(section('Pre-reveal independent answer',
        h('div', {},
          ia.kind ? h('span', { class: 'asc-chip' }, String(ia.kind)) : null,
          grounded(h('div', { class: 'asc-rv-answer-text' }, ia.text), ia))));
    }

    // GREEN, because this is physician-authored. Same class for A and B.
    return {
      card: h('div', { class: 'asc-answer-physician' },
        h('div', { class: 'asc-answer-head' },
          h('span', { class: 'asc-answer-eyebrow' }, 'Physician ' + entry.label)),
        h('div', { class: 'asc-answer-body' }, body)),
      finalText: finalText,
    };
  }

  // ── the judgment ────────────────────────────────────────────────────────────
  var correctionsBox = null;
  var notesArea = null;
  var editedArea = null;
  var submitBtn = null;
  var errLine = null;
  var dimSegs = [];      // the four dimension segment wrappers, in order
  var strongerSeg = null;

  /* One of N, and the platform is told so.

     These are RADIOGROUPS, not three loose buttons. That is not decoration: it
     is what makes `aria-checked` meaningful, what makes the group one tab stop
     instead of three, and what lets a screen reader say "Reasoning holds,
     Agree, selected, 1 of 3" when the aim lands here. Built with buttons rather
     than <input type=radio> because the visual control is a segmented bar and
     the native widget cannot be styled into one — so every state the native
     element would have carried has to be carried explicitly. */
  function segmented(values, labels, onPick, datasetKey, opts) {
    opts = opts || {};
    var wrap = h('div', {
      class: 'asc-rv-seg',
      role: 'radiogroup',
      // Absent when the caller has no label element; h() drops null attrs.
      'aria-labelledby': opts.labelledBy || null,
      'aria-label': opts.label || null,
    });
    values.forEach(function (v, i) {
      var d = {}; d[datasetKey] = v;
      wrap.appendChild(h('button', {
        type: 'button', dataset: d,
        role: 'radio',
        'aria-checked': 'false',
        // ROVING TABINDEX. Tab reaches the group once and lands on whatever is
        // selected; the arrows move within it. Three tab stops per dimension
        // would be twelve on this screen before the verdict.
        tabindex: i === 0 ? '0' : '-1',
        onClick: function (ev) { pick(wrap, ev.currentTarget, v, onPick); },
      }, labels[i]));
    });
    wrap._pick = function (value) {
      var hit = null;
      Array.prototype.forEach.call(wrap.children, function (b) {
        if (b.dataset[datasetKey] === value) hit = b;
      });
      if (hit) pick(wrap, hit, value, onPick);
      return !!hit;
    };
    /* Put real DOM focus on this group — on its selected option, or its first.
       This is the whole reason the aim is perceivable to anyone who cannot see
       the highlight: focus is the one signal every assistive technology already
       follows, so nothing has to be announced by hand. */
    wrap._focus = function () {
      var target = null;
      Array.prototype.forEach.call(wrap.children, function (b) {
        if (!target && b.getAttribute('aria-checked') === 'true') target = b;
      });
      if (!target) target = wrap.children[0];
      if (target && typeof target.focus === 'function') {
        try { target.focus(); } catch (e) { /* never block the reviewer */ }
      }
      return !!target;
    };
    return wrap;
  }
  function pick(wrap, btn, value, onPick) {
    Array.prototype.forEach.call(wrap.children, function (b) {
      b.classList.remove('is-on');
      // The class is the paint; aria-checked is the fact. Both, always, or a
      // screen reader reads a control that looks answered as unanswered.
      b.setAttribute('aria-checked', 'false');
      b.setAttribute('tabindex', '-1');
    });
    btn.classList.add('is-on');
    btn.setAttribute('aria-checked', 'true');
    btn.setAttribute('tabindex', '0');
    onPick(value);
    refreshSubmit();
  }

  // Ids for the label→group wiring. A counter rather than the dimension key so
  // two mounts on one document can never collide.
  var uidSeq = 0;
  function nextId(prefix) { uidSeq += 1; return 'ascrv-' + prefix + '-' + uidSeq; }

  function strongerRow() {
    var choices = (ME && ME.strength_choices) || ['A', 'B', 'equivalent'];
    // Keyboard: A / B / N. Displayed on the control, because unlike the case
    // toggle this one is the reviewer's main loop and they use it twenty times
    // an hour.
    var labels = choices.map(function (c) {
      return c === 'equivalent' ? 'Neither (N)' : c;
    });
    var labelId = nextId('stronger');
    strongerSeg = segmented(choices, labels, function (v) {
      R.stronger = v;
      // An edited accept follows the comparison. Changing the answer above
      // after choosing 'Accept with edits' must move it too, or the row
      // records an edit against a physician the reviewer did not name.
      if (R.verdict === 'accept_with_edits') {
        R.acceptedSide = (v === 'A' || v === 'B') ? v : null;
      }
      saveJudgment();
    }, 'state', { labelledBy: labelId });
    return h('div', { class: 'asc-rv-dim' },
      h('div', { class: 'asc-rv-dim-label', id: labelId }, 'Which is stronger?',
        h('small', {}, 'the comparison a single-label review could not ask')),
      strongerSeg);
  }

  function dimensionRows() {
    dimSegs = [];
    return ((ME && ME.dimensions) || []).map(function (d, i) {
      var key = d[0], label = d[1], hint = d[2];
      var states = (ME.dimension_states || ['agree', 'disagree', 'cannot_assess']);
      var labels = states.map(function (s) {
        return s === 'agree' ? 'Agree' : s === 'disagree' ? 'Disagree' : "Can't assess";
      });
      var labelId = nextId('dim');
      var seg = segmented(states, labels, function (v) {
        R.dimensions[key] = v;
        saveJudgment();
      }, 'state', { labelledBy: labelId });
      dimSegs.push(seg);
      var row = h('div', {
        class: 'asc-rv-dim',
        dataset: { dimIdx: String(i) },
      },
        h('div', { class: 'asc-rv-dim-label', id: labelId },
          // The digit is a sighted affordance for the 1-4 shortcut. Hidden from
          // assistive tech on purpose: prefixing every announcement with "1"
          // is noise, and the shortcut itself is documented in the Guide, which
          // is where every other shortcut on this product lives.
          h('span', { class: 'asc-rv-dim-key', 'aria-hidden': 'true' }, String(i + 1)), ' ', label,
          h('small', {}, hint)),
        seg);
      seg._row = row;
      return row;
    });
  }

  /* The reasoning forks, as their own control. One row per divergent step; the
     reviewer says which side was right there, or leaves it blank. Blank is a
     real answer — a reviewer who cannot tell should not be made to guess, which
     is the same rule `cannot_assess` encodes one control down. */
  function divergenceRows() {
    if (!DIVERGENCE || !DIVERGENCE.length) return null;
    var rows = DIVERGENCE.map(function (d) {
      var labelId = nextId('fork');
      var seg = segmented(['A', 'B', 'neither'], ['A', 'B', 'Neither'], function (v) {
        d.judged = v;
      }, 'fork', { labelledBy: labelId });
      return h('div', { class: 'asc-rv-dim asc-rv-fork-row', dataset: { forkIdx: String(d.index) } },
        h('div', { class: 'asc-rv-dim-label', id: labelId },
          'Step ' + (d.index + 1) + ' — they diverge',
          h('small', {}, d.a === null ? 'only B took this step'
            : d.b === null ? 'only A took this step'
              : 'which side is right here?')),
        seg);
    });
    return h('div', { class: 'asc-rv-forks' },
      h('div', { class: 'asc-rv-kv asc-rv-kv-lead' },
        h('b', {}, 'Where the reasoning forks'),
        ' — ' + DIVERGENCE.length + ' of ' + forkDenominator() + ' steps'),
      rows);
  }
  function forkDenominator() {
    var answers = (PAIR && PAIR.answers) || [];
    return Math.max(stepsOf(answers[0]).length, stepsOf(answers[1]).length);
  }

  /* Four buttons, three stored verdicts. "Accept A" and "Accept B" are ONE
     verdict plus a side — the server's acceptance statistic counts three tokens
     and a fourth would silently fall out of its denominator. */
  //
  // The third entry's side is 'stronger', not null: an edited accept still has
  // to name WHOSE answer was edited, or the row anchors to the canonical first
  // submission and the per-labeler signal is lost on every corrected accept.
  // The reviewer already answered that question one control up.
  var OVERALL = [
    ['accept', 'A', 'Accept A', "Physician A's answer is right as submitted"],
    ['accept', 'B', 'Accept B', "Physician B's answer is right as submitted"],
    ['accept_with_edits', 'stronger', 'Accept with edits', 'Right call, needs corrections'],
    ['reject', null, 'Reject both', 'Neither is usable — reason required'],
  ];

  var verdictWrap = null;
  function overallButtons(prefill) {
    // Four controls, exactly one of which can hold. Same radiogroup treatment as
    // the rows above, for the same reason: `.is-on` is paint, and paint is not
    // a state any assistive technology can read.
    verdictWrap = h('div', {
      class: 'asc-rv-verdicts', role: 'radiogroup', 'aria-label': 'Overall verdict',
    }, OVERALL.map(function (d, i) {
      return h('button', {
        // The button's identity is the verdict plus the side it FIXES. A side of
        // 'stronger' is deferred to the control above, so it names no column
        // here and the key stays the bare verdict.
        type: 'button',
        dataset: { verdict: d[0] + ((d[1] === 'A' || d[1] === 'B') ? ':' + d[1] : '') },
        role: 'radio',
        'aria-checked': 'false',
        tabindex: i === 0 ? '0' : '-1',
        onClick: function (ev) { chooseVerdict(ev.currentTarget, d, prefill); },
      },
        h('div', {}, h('b', {}, d[2])), h('small', { class: 'asc-rv-kv' }, d[3]));
    }));
    return verdictWrap;
  }
  /* Re-aim the controls from a judgment recovered on the previous line.
     Done AFTER mount because the segmented controls and verdict buttons have
     to exist before they can be aimed. Deliberately drives the same handlers a
     click drives rather than setting classes directly: a restored judgment
     that looks selected but did not run the side effects (the accept-with-
     edits box, the acceptedSide coupling) is worse than no restore. */
  function restoreJudgment(prefill) {
    if (!RESTORED) return;
    if (RESTORED.stronger && strongerSeg && strongerSeg._pick) {
      strongerSeg._pick(RESTORED.stronger);
    }
    var dims = (ME && ME.dimensions) || [];
    dims.forEach(function (d, i) {
      var value = RESTORED.dimensions && RESTORED.dimensions[d[0]];
      if (value && dimSegs[i] && dimSegs[i]._pick) dimSegs[i]._pick(value);
    });
    if (RESTORED.verdict) {
      // The button's identity is the verdict plus any side it FIXES, which is
      // exactly how it was stored.
      var side = RESTORED.acceptedSide;
      var key = RESTORED.verdict
        + ((RESTORED.verdict === 'accept' && (side === 'A' || side === 'B')) ? ':' + side : '');
      var spec = null;
      OVERALL.forEach(function (v) {
        if (!spec && v[0] === RESTORED.verdict
            && (v[1] === side || v[1] === 'stronger' || (!v[1] && !side))) spec = v;
      });
      var btn = verdictButtonFor(key);
      if (btn && spec) chooseVerdict(btn, spec, prefill);
    }
  }
  function chooseVerdict(btn, d, prefill) {
    R.verdict = d[0];
    // 'stronger' defers to the comparison the reviewer already made;
    // 'equivalent' names no side, which the server accepts for an edit.
    R.acceptedSide = d[1] === 'stronger'
      ? ((R.stronger === 'A' || R.stronger === 'B') ? R.stronger : null)
      : d[1];
    Array.prototype.forEach.call(verdictWrap.children, function (b) {
      b.classList.remove('is-on');
      b.setAttribute('aria-checked', 'false');
      b.setAttribute('tabindex', '-1');
    });
    btn.classList.add('is-on');
    btn.setAttribute('aria-checked', 'true');
    btn.setAttribute('tabindex', '0');
    var needs = R.verdict === 'accept_with_edits' || R.verdict === 'reject';
    correctionsBox.style.display = needs ? '' : 'none';
    if (R.verdict === 'accept_with_edits' && !editedArea.value && prefill) {
      editedArea.value = prefill;
    }
    saveJudgment();
    refreshSubmit();
  }
  function verdictButtonFor(key) {
    var hit = null;
    if (!verdictWrap) return null;
    Array.prototype.forEach.call(verdictWrap.children, function (b) {
      if (b.dataset.verdict === key) hit = b;
    });
    return hit;
  }

  function reviewComplete() {
    if (!R || !R.verdict || !R.stronger) return false;
    var dims = (ME && ME.dimensions) || [];
    for (var i = 0; i < dims.length; i++) {
      if (!R.dimensions[dims[i][0]]) return false;
    }
    // Same rule the server enforces, so the button state and the 400 can never
    // disagree about what a complete review is.
    if (R.verdict === 'accept_with_edits' || R.verdict === 'reject') {
      if (!notesArea.value.trim() && !editedArea.value.trim()) return false;
    }
    return true;
  }
  function allDimensionsAgree() {
    var dims = (ME && ME.dimensions) || [];
    if (!dims.length) return false;
    for (var i = 0; i < dims.length; i++) {
      if (R.dimensions[dims[i][0]] !== 'agree') return false;
    }
    return true;
  }
  function refreshSubmit() { if (submitBtn) submitBtn.disabled = !reviewComplete(); }

  /* The reviewer's step-level supervision, as the server stores it (§3).

     NULL — not an empty array — when either side carried no reasoning steps.
     Nothing was compared, so there is nothing to report, and a fabricated `[]`
     would claim a measurement that was never made. An empty array when both
     sides DID carry steps is a real finding: they agreed at every step. */
  function divergencePayload() {
    if (DIVERGENCE === null || DIVERGENCE === undefined) return null;
    return DIVERGENCE.map(function (d) {
      return { index: d.index, judged: d.judged || null };
    });
  }

  function submitReview() {
    if (!reviewComplete() || !PAIR) return;
    if (PREVIEW) {
      // The server 409s a preview submit and that is the authority. Saying so
      // here first is not a second gate — it is not making an admin discover
      // the rule from an error code.
      clear(errLine);
      errLine.appendChild(document.createTextNode(
        'Preview — nothing you submit here is recorded.'));
      return;
    }
    submitBtn.disabled = true;
    var body = {
      // Echoed straight back from the draw. A preview token is REFUSED with a
      // 409 — the server is the authority on what is recorded, and this is what
      // gives it the fact it needs to refuse.
      draw_token: (PAIR && PAIR.draw_token) || null,
      verdict: R.verdict,
      stronger: R.stronger,
      accepted_side: R.acceptedSide,
      dimensions: R.dimensions,
      reviewer_notes: notesArea.value.trim() || null,
      time_spent_sec: Math.max(1, Math.round((Date.now() - R.startedAt) / 1000)),
    };
    var divergence = divergencePayload();
    if (divergence !== null) body.step_divergence = divergence;
    if (R.verdict === 'accept_with_edits' || R.verdict === 'reject') {
      var corrections = {};
      if (notesArea.value.trim()) corrections.notes = notesArea.value.trim();
      if (editedArea.value.trim()) corrections.edited_answer = editedArea.value.trim();
      body.corrections = corrections;
    }
    var gen = GENERATION;
    var submittedTaskId = PAIR.task_id;
    api('/review/pair/' + encodeURIComponent(PAIR.task_id), { method: 'POST', body: body })
      .then(function (res) {
        if (stale(gen)) return;
        // The server withholds free text that carries a Safe-Harbor identifier
        // from the buyer-facing block, and says so in the response SPECIFICALLY
        // so the reviewer can rewrite it. Advancing here — which this used to do
        // — means they find out months later, or never.
        // The judgment is the server's now, so the local copy stops being a
        // recovery aid and starts being a stale one.
        clearJudgment(submittedTaskId);
        if (res && res.corrections_withheld) {
          renderWithheld(res.identifier_flags || []);
          return;
        }
        loadNext();
      })
      .catch(function (err) {
        if (stale(gen)) return;
        clear(errLine);
        errLine.appendChild(document.createTextNode(errText(err)));
        refreshSubmit();
      });
  }

  function renderWithheld(flags) {
    stopClock();
    mount(header(), previewBanner(), h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('h3', {}, 'Your correction was withheld from the buyer bundle'),
      h('p', { class: 'asc-rv-kv' },
        'Your adjudication is recorded. The free text is not being shipped, '
        + 'because it looks like it contains patient identifiers: ',
        h('b', {}, (flags || []).join(', ') || 'unspecified'),
        '. Rewrite it without them on the next case, or tell us if this is '
        + 'a false positive.'),
      h('div', { class: 'asc-rv-actions' },
        h('button', { class: 'asc-btn asc-btn-primary', type: 'button', onClick: loadNext },
          'Next case')))));
  }

  // ── keyboard (§2.3) ─────────────────────────────────────────────────────────
  //
  // A reviewer doing twenty pairs should never touch the mouse. Documented in
  // the Guide, not on the screen — the same rule the case toggle follows —
  // except for the two labels the main loop actually needs (the A/B/N row and
  // the dimension numbers), which ride on the controls themselves.
  //
  //   A / B / N   which is stronger
  //   1 – 4       aim at a dimension
  //   ← →         agree / disagree, then advance
  //   C           can't assess, then advance
  //   Enter       submit
  //
  // C is not in the PRD's list and is here on purpose: `cannot_assess` is a
  // first-class state, and a keyboard flow that can only reach two of three
  // states would push a physician toward answering a dimension outside their
  // subspecialty rather than saying so. It is a LETTER rather than the obvious
  // third arrow because ↓ is how a reviewer scrolls the case, and a shortcut
  // that eats the scroll key costs more than it saves.
  function ownsItsOwnEnter(el) {
    if (!el || !el.tagName) return false;
    // A radio activates on SPACE, not Enter (that is the ARIA pattern, and it is
    // what a native radio does). So Enter with the aim sitting on a judgment
    // control means what Enter means everywhere else on this screen: submit.
    //
    // This matters BECAUSE the aim now moves focus. Without it the six-keystroke
    // accept — A, four arrows, Enter — would end by re-selecting the radio the
    // last arrow had already selected, and never submit.
    if (typeof el.getAttribute === 'function' && el.getAttribute('role') === 'radio') {
      return false;
    }
    var tag = String(el.tagName).toUpperCase();
    return tag === 'BUTTON' || tag === 'SUMMARY' || tag === 'A' || tag === 'DETAILS';
  }
  function isTypingTarget(el) {
    if (!el || !el.tagName) return false;
    var tag = String(el.tagName).toUpperCase();
    return tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT'
      || el.isContentEditable === true;
  }
  /* Aim the 1-4 / arrow keys at a dimension.

     ``moveFocus`` is the accessibility of this whole control. The aim used to be
     a left-edge highlight and NOTHING else — perceivable if you can see it, and
     completely silent otherwise. A reviewer on a screen reader pressed 3, heard
     nothing, pressed the left arrow, and had no way to know which dimension they
     had just answered.

     Moving real DOM focus fixes that without inventing anything: focus is the
     signal every assistive technology already follows, so the platform announces
     the group's name, the selected option and its position for free. No live
     region, no bespoke announcement to keep in sync with the visible state.

     It is FALSE on the initial render — stealing focus on arrival is its own
     accessibility problem, and would yank the viewport into the judgment panel
     before the reviewer has read the pair. Only a deliberate keystroke moves it. */
  function aimDimension(i, moveFocus) {
    var dims = (ME && ME.dimensions) || [];
    if (i < 0 || i >= dims.length) return;
    focusedDim = i;
    dimSegs.forEach(function (seg, j) {
      if (seg._row) seg._row.classList.toggle('asc-rv-dim-aimed', j === i);
    });
    if (moveFocus && dimSegs[i] && typeof dimSegs[i]._focus === 'function') {
      dimSegs[i]._focus();
    }
  }
  function setAimedDimension(stateValue) {
    var seg = dimSegs[focusedDim];
    if (!seg || typeof seg._pick !== 'function') return;
    if (!seg._pick(stateValue)) return;
    // Advance, so four arrow presses answer four dimensions. Stops at the last
    // one rather than wrapping: wrapping would silently overwrite dimension 1
    // on a fifth press.
    if (focusedDim < dimSegs.length - 1) {
      aimDimension(focusedDim + 1, true);
    } else if (seg._focus) {
      // The last one still takes focus, so the answer just recorded is
      // announced rather than swallowed.
      seg._focus();
    }
  }
  function onKeyDown(e) {
    if (!PAIR || !R) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (isTypingTarget(e.target)) return;
    var key = e.key;
    var handled = true;
    if (key === 'a' || key === 'A') { if (strongerSeg) strongerSeg._pick('A'); }
    else if (key === 'b' || key === 'B') { if (strongerSeg) strongerSeg._pick('B'); }
    else if (key === 'n' || key === 'N') { if (strongerSeg) strongerSeg._pick('equivalent'); }
    else if (key >= '1' && key <= '4') { aimDimension(parseInt(key, 10) - 1, true); }
    else if (key === 'ArrowLeft') { setAimedDimension('agree'); }
    else if (key === 'ArrowRight') { setAimedDimension('disagree'); }
    else if (key === 'c' || key === 'C') { setAimedDimension('cannot_assess'); }
    else if (key === 'Enter') {
      // A focused button or a focused <summary> owns its own Enter. Stealing it
      // would mean tabbing to "open the case" and submitting the adjudication
      // instead, which is the worst possible misfire on this screen.
      if (ownsItsOwnEnter(e.target)) return;
      onEnter();
    }
    else handled = false;
    if (handled && typeof e.preventDefault === 'function') e.preventDefault();
  }
  /* ONE-KEY ACCEPT (§2.3). The thesis is that a good pair is accepted in under
     sixty seconds, so the good path is the shortest one: when every dimension
     reads `agree` and the reviewer has named a stronger side, Enter fills in the
     accept for that side and submits.

     It fires ONLY on that path. If any dimension is not `agree`, or no side is
     named, Enter does nothing until the reviewer picks a verdict explicitly —
     an Enter that could produce a `reject` or an unexamined accept is a
     keystroke that adjudicates a physician's work by accident. */
  function onEnter() {
    if (reviewComplete()) { submitReview(); return; }
    if (R.verdict) return;
    if (!allDimensionsAgree()) return;
    if (R.stronger !== 'A' && R.stronger !== 'B') return;
    var btn = verdictButtonFor('accept:' + R.stronger);
    if (!btn) return;
    chooseVerdict(btn, ['accept', R.stronger], null);
    if (reviewComplete()) submitReview();
  }
  function bindKeys() {
    unbindKeys();
    keyHandler = onKeyDown;
    document.addEventListener('keydown', keyHandler);
  }
  function unbindKeys() {
    if (keyHandler) { document.removeEventListener('keydown', keyHandler); keyHandler = null; }
  }

  // ── the review screen ───────────────────────────────────────────────────────
  function renderReview() {
    var answers = PAIR.answers || [];
    var task = PAIR.task || {};
    DIVERGENCE = computeDivergence(stepsOf(answers[0]), stepsOf(answers[1]));
    var forks = divergentIndices();
    var a = answerCard(answers[0] || { label: 'A' }, task, forks);
    var b = answerCard(answers[1] || { label: 'B' }, task, forks);

    correctionsBox = null; notesArea = null; editedArea = null; submitBtn = null; errLine = null;
    clockNoteEl = h('span', { class: 'asc-session-note' }, clockNote() || '');

    function onText() { saveJudgment(); refreshSubmit(); }
    notesArea = h('textarea', { class: 'asc-textarea', onInput: onText,
      placeholder: 'What is wrong, and what should change? Required on edits and on reject.' });
    editedArea = h('textarea', { class: 'asc-textarea', onInput: onText,
      placeholder: 'Optional: the corrected answer text.' });
    // A judgment recovered from a previous sitting. The prose goes back into
    // the boxes here; the segmented controls are re-aimed after they mount.
    if (RESTORED) {
      if (RESTORED.notes) notesArea.value = RESTORED.notes;
      if (RESTORED.edited) editedArea.value = RESTORED.edited;
    }
    correctionsBox = h('div', { style: 'display:none' },
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Corrections / reason'), notesArea),
      h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Edited answer'), editedArea));
    submitBtn = h('button', { class: 'asc-btn asc-btn-primary', type: 'button',
      disabled: true, onClick: submitReview }, 'Submit adjudication');
    errLine = h('span', { class: 'asc-inline-error' });

    // The clinical question, above everything. It is the one thing the reviewer
    // reads first and the labeler's prompt card is where they already read it.
    var questionCard = h('div', { class: 'asc-card asc-prompt-card' },
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-prompt-label' }, 'Clinical question'),
        h('div', { class: 'asc-prompt-text' }, task.prompt || '')));

    var judgment = h('div', { class: 'asc-card asc-rv-judgment' },
      h('div', { class: 'asc-card-pad' },
        h('h3', {}, 'Your judgment'),
        strongerRow(),
        dimensionRows(),
        divergenceRows(),
        h('div', { class: 'asc-rv-kv asc-rv-kv-lead' }, h('b', {}, 'Overall')),
        overallButtons(a.finalText || b.finalText),
        correctionsBox,
        h('div', { class: 'asc-rv-actions' }, submitBtn, errLine)));

    mount(
      header(),
      previewBanner(),
      h('div', { class: 'asc-rv-main' },
        questionCard,
        // Chrome OUTSIDE the grid — see the PRD-R note in asclepius.css.
        h('div', { class: 'asc-rv-pairbar' },
          h('span', { class: 'asc-rv-chrome' }, 'Two independent labels · same case · no identity'),
          clockNoteEl),
        // EXACTLY two children. Nothing else may be added here.
        h('div', { class: 'asc-answers' }, a.card, b.card),
        caseSection(task),
        judgment));

    restoreJudgment(a.finalText || b.finalText);
    aimDimension(0);
    // The button is built with `disabled: true`, which the shell's hyperscript
    // writes as an ATTRIBUTE. Settle the PROPERTY here so there is one authority
    // on whether submit is live, rather than an attribute set at construction
    // and a property set on every keystroke afterwards.
    refreshSubmit();
    bindKeys();
  }

  // ── the module contract ─────────────────────────────────────────────────────
  window.AsclepiusReview = {
    render: function (el, ctx) {
      if (!el || !ctx || typeof ctx.h !== 'function') return;
      // Bump FIRST: anything still in flight for the previous mount is now
      // answering a question nobody asked, and must not paint over this one.
      GENERATION += 1;
      drawing = false;
      unbindKeys();
      stopClock();
      CTX = ctx;
      HOST = el;
      PREVIEW = !!ctx.preview;
      ME = null; PAIR = null; SESSION = null; STATS = null;
      resetReview();
      boot();
    },
    /* Leaving the review surface is a no-work transition, exactly like an empty
       queue: the reviewer is no longer looking at a pair, so the beats must
       stop. Without this, switching to the Guide left a session accruing paid
       time against a screen with no work on it. */
    teardown: function () {
      GENERATION += 1;
      drawing = false;
      unbindKeys();
      stopClock();
      stopSession('left_review');
      SESSION = null; PAIR = null; R = null; DIVERGENCE = [];
      HOST = null;
    },
  };
})();
