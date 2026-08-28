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
  var DIVERGENCE = [];  // the aligned reasoning-step forks (§2.3 / §3)
  var PREVIEW = false;  // admin sightseeing; nothing is recorded (§4.1)
  var clockTimer = null;
  var keyHandler = null;
  var focusedDim = 0;   // which dimension the 1-4 / arrow keys are aimed at

  function h() { return CTX.h.apply(null, arguments); }
  function clear(el) { return CTX.clear(el); }
  function api(path, opts) { return CTX.api(path, opts); }
  function errText(e) {
    return (e && (e.message || e.detail)) || 'Something went wrong.';
  }

  function mount() {
    clear(HOST);
    for (var i = 0; i < arguments.length; i++) {
      var kid = arguments[i];
      if (kid) HOST.appendChild(kid);
    }
  }

  function resetReview() {
    R = { verdict: null, stronger: null, acceptedSide: null, dimensions: {},
          startedAt: Date.now() };
    DIVERGENCE = [];
    focusedDim = 0;
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
    renderLoading();
    api('/review/me').then(function (me) {
      ME = me;
      if (!me.can_review) { renderNotReviewer(); return; }
      // §4.1. The SERVER decides whether this account is a real reviewer or an
      // operator reaching the surface through the admin override — that is a
      // tier question, and the tier table is not this module's to interpret. An
      // explicit preview (the Evaluate chooser) can only ever turn preview ON.
      if (me.preview_only) PREVIEW = true;
      loadNext();
    }).catch(function (err) {
      if (err && err.status === 401) return;   // the shell owns the bounce
      renderFatal(errText(err));
    });
  }

  function loadNext() {
    renderLoading();
    Promise.all([
      api('/review/pair/next' + (PREVIEW ? '?preview=true' : '')),
      api('/review/stats').catch(function () { return null; }),
    ]).then(function (results) {
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
    api('/review/double-label/next').then(function (data) {
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
  function stepsOf(entry) {
    var a = (entry && entry.answer) || {};
    var raw = a.reasoning_steps;
    if (!Array.isArray(raw)) {
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

  /* One physician's answer. Identical structure for A and B, identical accent,
     distinguished ONLY by the mono eyebrow and by which column it is in. */
  function answerCard(entry, task, forks) {
    var a = entry.answer || {};
    var body = [];

    body.push(h('div', {},
      h('span', { class: 'asc-chip' }, verdictLabel(a.verdict)),
      ' ',
      entry.confidence ? h('span', { class: 'asc-chip' }, 'confidence: ' + entry.confidence) : null));

    var rev = a.chosen_revision || null;
    var finalText = null;
    if (rev && rev.edited && rev.revised_text) {
      finalText = rev.revised_text;
      body.push(section('Corrected answer (as submitted)',
        h('div', { class: 'asc-rv-answer-text' }, rev.revised_text)));
      if (rev.why_better_notes) body.push(h('div', { class: 'asc-rv-kv' }, 'Why: ' + rev.why_better_notes));
    } else if (a.verdict === 'A_better' || a.verdict === 'B_better') {
      finalText = candidateText(task, a.chosen_id);
      body.push(section('Chosen answer (unedited)',
        h('div', { class: 'asc-rv-answer-text' }, finalText)));
    }

    var fs = a.from_scratch || null;
    if (fs && fs.ideal_answer) {
      finalText = fs.ideal_answer;
      body.push(section('Written from scratch',
        h('div', { class: 'asc-rv-answer-text' }, fs.ideal_answer)));
      if (fs.approach_notes) body.push(h('div', { class: 'asc-rv-kv' }, 'Approach: ' + fs.approach_notes));
    }

    var crit = a.rejected_critique || null;
    if (crit && (crit.why_worse || (crit.error_tags || []).length)) {
      var critKids = [];
      if ((crit.error_tags || []).length) critKids.push(h('div', {},
        crit.error_tags.map(function (t) {
          return h('span', { class: 'asc-chip asc-rv-chip-gap' }, t);
        })));
      if (crit.why_worse) critKids.push(h('div', { class: 'asc-rv-kv' }, crit.why_worse));
      body.push(section('Critique of the rejected answer', critKids));
    }

    var steps = stepsOf(entry);
    if (steps.length) {
      body.push(section('Reasoning steps', steps.map(function (s, i) {
        var text = stepText(s);
        var note = s && (s.note || s.step_note);
        // The fork mark. Same class on both columns — it says "A and B parted
        // company here", never "this column is the wrong one".
        var diverges = !!forks[i];
        return h('div', {
          class: 'asc-rv-step' + (diverges ? ' asc-rv-step-fork' : ''),
          dataset: { stepIdx: String(i) },
        },
          h('span', { class: 'asc-rv-step-num' }, String(i + 1)),
          h('span', {}, String(text),
            note ? h('div', { class: 'asc-rv-kv' }, 'note: ' + note) : null));
      })));
    }

    var rubric = a.rubric || [];
    if (rubric.length) {
      body.push(section('Grading rubric (as confirmed)', rubric.map(function (cr) {
        var pts = (cr && cr.points != null) ? cr.points : '';
        return h('div', { class: 'asc-rv-step' },
          h('span', { class: 'asc-rv-step-num' }, String(pts)),
          h('span', {}, (cr && (cr.text || cr.criterion)) || ''));
      })));
    }

    var ia = a.independent_answer || null;
    if (ia && ia.text) {
      body.push(section('Pre-reveal independent answer',
        h('div', { class: 'asc-rv-answer-text' }, ia.text)));
    }
    if (a.citations) {
      body.push(fold('Citations', h('div', { class: 'asc-rv-citations' },
        (Array.isArray(a.citations) ? a.citations : [a.citations]).map(function (cit) {
          if (!cit || typeof cit !== 'object') return h('div', { class: 'asc-rv-kv' }, String(cit || ''));
          return h('div', { class: 'asc-rv-kv' },
            [cit.citation_text || cit.text || '', cit.source_type || '', cit.identifier || '']
              .filter(Boolean).join(' · '));
        }))));
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

  function segmented(values, labels, onPick, datasetKey) {
    var wrap = h('div', { class: 'asc-rv-seg' });
    values.forEach(function (v, i) {
      var d = {}; d[datasetKey] = v;
      wrap.appendChild(h('button', {
        type: 'button', dataset: d,
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
    return wrap;
  }
  function pick(wrap, btn, value, onPick) {
    Array.prototype.forEach.call(wrap.children, function (b) { b.classList.remove('is-on'); });
    btn.classList.add('is-on');
    onPick(value);
    refreshSubmit();
  }

  function strongerRow() {
    var choices = (ME && ME.strength_choices) || ['A', 'B', 'equivalent'];
    // Keyboard: A / B / N. Displayed on the control, because unlike the case
    // toggle this one is the reviewer's main loop and they use it twenty times
    // an hour.
    var labels = choices.map(function (c) {
      return c === 'equivalent' ? 'Neither (N)' : c;
    });
    strongerSeg = segmented(choices, labels, function (v) {
      R.stronger = v;
      // An edited accept follows the comparison. Changing the answer above
      // after choosing 'Accept with edits' must move it too, or the row
      // records an edit against a physician the reviewer did not name.
      if (R.verdict === 'accept_with_edits') {
        R.acceptedSide = (v === 'A' || v === 'B') ? v : null;
      }
    }, 'state');
    return h('div', { class: 'asc-rv-dim' },
      h('div', { class: 'asc-rv-dim-label' }, 'Which is stronger?',
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
      var seg = segmented(states, labels, function (v) { R.dimensions[key] = v; }, 'state');
      dimSegs.push(seg);
      var row = h('div', {
        class: 'asc-rv-dim',
        dataset: { dimIdx: String(i) },
      },
        h('div', { class: 'asc-rv-dim-label' },
          h('span', { class: 'asc-rv-dim-key' }, String(i + 1)), ' ', label,
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
      var seg = segmented(['A', 'B', 'neither'], ['A', 'B', 'Neither'], function (v) {
        d.judged = v;
      }, 'fork');
      return h('div', { class: 'asc-rv-dim asc-rv-fork-row', dataset: { forkIdx: String(d.index) } },
        h('div', { class: 'asc-rv-dim-label' },
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
    verdictWrap = h('div', { class: 'asc-rv-verdicts' }, OVERALL.map(function (d) {
      return h('button', {
        // The button's identity is the verdict plus the side it FIXES. A side of
        // 'stronger' is deferred to the control above, so it names no column
        // here and the key stays the bare verdict.
        type: 'button',
        dataset: { verdict: d[0] + ((d[1] === 'A' || d[1] === 'B') ? ':' + d[1] : '') },
        onClick: function (ev) { chooseVerdict(ev.currentTarget, d, prefill); },
      },
        h('div', {}, h('b', {}, d[2])), h('small', { class: 'asc-rv-kv' }, d[3]));
    }));
    return verdictWrap;
  }
  function chooseVerdict(btn, d, prefill) {
    R.verdict = d[0];
    // 'stronger' defers to the comparison the reviewer already made;
    // 'equivalent' names no side, which the server accepts for an edit.
    R.acceptedSide = d[1] === 'stronger'
      ? ((R.stronger === 'A' || R.stronger === 'B') ? R.stronger : null)
      : d[1];
    Array.prototype.forEach.call(verdictWrap.children, function (b) { b.classList.remove('is-on'); });
    btn.classList.add('is-on');
    var needs = R.verdict === 'accept_with_edits' || R.verdict === 'reject';
    correctionsBox.style.display = needs ? '' : 'none';
    if (R.verdict === 'accept_with_edits' && !editedArea.value && prefill) {
      editedArea.value = prefill;
    }
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
    api('/review/pair/' + encodeURIComponent(PAIR.task_id), { method: 'POST', body: body })
      .then(function (res) {
        // The server withholds free text that carries a Safe-Harbor identifier
        // from the buyer-facing block, and says so in the response SPECIFICALLY
        // so the reviewer can rewrite it. Advancing here — which this used to do
        // — means they find out months later, or never.
        if (res && res.corrections_withheld) {
          renderWithheld(res.identifier_flags || []);
          return;
        }
        loadNext();
      })
      .catch(function (err) {
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
    var tag = String(el.tagName).toUpperCase();
    return tag === 'BUTTON' || tag === 'SUMMARY' || tag === 'A' || tag === 'DETAILS';
  }
  function isTypingTarget(el) {
    if (!el || !el.tagName) return false;
    var tag = String(el.tagName).toUpperCase();
    return tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT'
      || el.isContentEditable === true;
  }
  function aimDimension(i) {
    var dims = (ME && ME.dimensions) || [];
    if (i < 0 || i >= dims.length) return;
    focusedDim = i;
    dimSegs.forEach(function (seg, j) {
      if (seg._row) seg._row.classList.toggle('asc-rv-dim-aimed', j === i);
    });
  }
  function setAimedDimension(stateValue) {
    var seg = dimSegs[focusedDim];
    if (!seg || typeof seg._pick !== 'function') return;
    if (!seg._pick(stateValue)) return;
    // Advance, so four arrow presses answer four dimensions. Stops at the last
    // one rather than wrapping: wrapping would silently overwrite dimension 1
    // on a fifth press.
    if (focusedDim < dimSegs.length - 1) aimDimension(focusedDim + 1);
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
    else if (key >= '1' && key <= '4') { aimDimension(parseInt(key, 10) - 1); }
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

    notesArea = h('textarea', { class: 'asc-textarea', onInput: refreshSubmit,
      placeholder: 'What is wrong, and what should change? Required on edits and on reject.' });
    editedArea = h('textarea', { class: 'asc-textarea', onInput: refreshSubmit,
      placeholder: 'Optional: the corrected answer text.' });
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
      unbindKeys();
      stopClock();
      stopSession('left_review');
      SESSION = null; PAIR = null; R = null; DIVERGENCE = [];
      HOST = null;
    },
  };
})();
