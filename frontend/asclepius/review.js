/* Asclepius Expert Review portal — the PAIRED review surface (PRD R Phase 4).
 *
 * A senior physician draws a CASE with two independent labels side by side,
 * decides which is stronger and whether either is right, and submits. The whole
 * product thesis is that a good pair is accepted in under 60 seconds, so the
 * case is folded away until doubted and the judgment controls are one glance
 * from the answers.
 *
 * Design decisions that are load-bearing, not cosmetic:
 *
 *   - BOTH CARDS ARE GREEN. On the labeler page the two side-by-side cards are
 *     frontier model answers and are orange; here they are two physicians' work.
 *     The accent carries meaning in this product.
 *   - A AND B ARE NOT DIFFERENT COLOURS. They are told apart by the mono eyebrow
 *     and by position. A reviewer who reads hue as "which doctor" starts reading
 *     hue as "which is better", and that biases the adjudication.
 *   - `.asc-answers` CONTAINS EXACTLY TWO CHILDREN. A third lands in cell 2 and
 *     pushes B to row 2. Chrome goes outside the grid.
 *   - CORRECTIONS ARE REVEALED, NOT ALWAYS PRESENT. An empty textarea under
 *     every review invites the reviewer to feel they owe prose on an accept.
 *     They don't.
 *   - THE COUNTDOWN IS LIME AND ITS VALUE COMES FROM THE SERVER. Lime means
 *     "needs attention"; pink means critical, and a running clock is not an
 *     emergency. Under two minutes the COPY changes, not the colour.
 *
 * All DOM is built with the h() hyperscript. No HTML string assignment anywhere
 * — every server string reaches the page as a text node. (test_review_dom.py
 * greps this file for those APIs, so this note has to paraphrase them.)
 *
 * The session is Agent P's from end to end. The draw response carries P's
 * session block, this page hands it to P's heartbeat client
 * (`window.AsclepiusSession`), and every second it renders is read back from
 * that client's server-attested state. It computes no time of its own: a
 * client-side clock that disagreed with the server's would be telling a
 * physician they had earned money they had not. When no heartbeat client is on
 * the page, the clock says so in words rather than showing a number.
 */
(function () {
  'use strict';

  var TOKEN_KEY = 'asclepius_token';
  var API_BASE = '/api/asclepius';

  var root = document.getElementById('reviewRoot');
  if (!root) return;
  var token = null;
  try { token = localStorage.getItem(TOKEN_KEY) || null; } catch (e) { token = null; }

  // ── hyperscript ────────────────────────────────────────────────────────────
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') el.className = v;
        else if (k === 'dataset') Object.keys(v).forEach(function (d) { el.dataset[d] = v[d]; });
        else if (k.indexOf('on') === 0 && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else if (k === 'value') el.value = v;
        else if (k === 'checked' || k === 'disabled' || k === 'open') el[k] = !!v;
        else el.setAttribute(k, v === true ? '' : String(v));
      });
    }
    for (var i = 2; i < arguments.length; i++) appendChild(el, arguments[i]);
    return el;
  }
  function appendChild(el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { appendChild(el, c); }); return; }
    if (child instanceof Node) { el.appendChild(child); return; }
    el.appendChild(document.createTextNode(String(child)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function mount() {
    clear(root);
    for (var i = 0; i < arguments.length; i++) appendChild(root, arguments[i]);
  }

  // ── API ─────────────────────────────────────────────────────────────────────
  function api(path, opts) {
    opts = opts || {};
    var headers = { 'Content-Type': 'application/json' };
    if (token) headers.Authorization = 'Bearer ' + token;
    return fetch(API_BASE + path, {
      method: opts.method || 'GET',
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (res) {
      if (res.status === 401) { signOut(); throw new Error('Signed out'); }
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var detail = data && data.detail;
          var msg = typeof detail === 'string' ? detail
            : (detail && detail.errors) ? detail.errors.join(' · ')
            : 'Request failed (' + res.status + ')';
          var err = new Error(msg);
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  function signOut() {
    token = null;
    try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
    renderLogin();
  }

  // ── state ───────────────────────────────────────────────────────────────────
  var ME = null;        // /review/me: vocabulary + can_review
  var PAIR = null;      // the blinded pair being adjudicated
  var SESSION = null;   // Agent P's session block, opaque, server-owned
  var STATS = null;
  var R = null;         // the adjudication being authored
  var clockTimer = null;

  function resetReview() {
    R = { verdict: null, stronger: null, acceptedSide: null, dimensions: {},
          startedAt: Date.now() };
  }

  // ── the session countdown ───────────────────────────────────────────────────
  //
  // THIS PAGE COMPUTES NO TIME. Every number below comes from Agent P's
  // heartbeat client (`window.AsclepiusSession`), which is the only thing that
  // talks to the server about a session and the only thing entitled to say how
  // many seconds have been counted.
  //
  // It used to seed from the draw response and then add wall-clock drift, with
  // no heartbeat ever sent. At 20:00 it rendered "this session has met its
  // minimum" while the server had counted ZERO seconds. Under a "20 continuous
  // minutes or $0" structure that is not a display bug — it is the page telling
  // a physician they have been paid for work that will not be paid for. A clock
  // this page can advance on its own IS that bug, whatever it is seeded with.
  function sessionClient() {
    var S = (typeof window !== 'undefined') && window.AsclepiusSession;
    return (S && typeof S.state === 'function') ? S : null;
  }
  function sessionState() {
    var S = sessionClient();
    if (!S) return null;
    try { return S.state() || null; } catch (e) { return null; }
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
      if (text !== null) { clear(clockEl); appendChild(clockEl, text); }
    }
    if (clockNoteEl) { clear(clockNoteEl); appendChild(clockNoteEl, clockNote() || ''); }
  }
  function startClock() {
    if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
    // A REPAINT, not a clock: it re-reads the heartbeat client's state. If that
    // state stops moving because the server stopped counting, so do the digits.
    if (SESSION) clockTimer = setInterval(paintClock, 1000);
  }

  // ── boot ────────────────────────────────────────────────────────────────────
  function boot() {
    if (!token) { renderLogin(); return; }
    api('/review/me').then(function (me) {
      ME = me;
      if (!me.can_review) { renderNotReviewer(); return; }
      loadNext();
    }).catch(function (err) {
      if (err.message !== 'Signed out') renderFatal(err.message);
    });
  }

  function loadNext() {
    renderLoading();
    Promise.all([
      api('/review/pair/next'),
      api('/review/stats').catch(function () { return null; }),
    ]).then(function (results) {
      PAIR = results[0].pair;
      SESSION = results[0].session || null;
      // Hand the server's session to Agent P's client and then leave it alone.
      // R decides what a reviewer sees; P decides whether the time is paid for.
      if (SESSION) {
        var S = sessionClient();
        if (S && typeof S.start === 'function') {
          try { S.start(SESSION); } catch (e) { /* never block the review */ }
        }
      }
      STATS = results[1];
      resetReview();
      if (!PAIR) { renderEmpty(results[0].message); return; }
      renderReview();
      startClock();
    }).catch(function (err) {
      if (err.message !== 'Signed out') renderFatal(err.message);
    });
  }

  // ── chrome ──────────────────────────────────────────────────────────────────
  function header() {
    var specialty = (ME && ME.user && ME.user.specialty) || null;
    var kids = [h('h1', null, 'Review'), specialty ? h('span', { class: 'rv-chrome' }, specialty) : null];
    var text = clockText();
    if (text !== null) {
      clockEl = h('span', { class: 'asc-session-clock' }, text);
      kids.push(clockEl);
    } else {
      clockEl = null;
    }
    if (STATS) {
      kids.push(h('div', { class: 'rv-headstats' },
        h('span', { class: 'rv-chrome' }, 'Pairs ready ' + (STATS.review_ready || 0)),
        h('span', { class: 'rv-chrome' }, 'Awaiting 2nd ' + (STATS.awaiting_second || 0)),
        h('span', { class: 'rv-chrome' }, 'Adjudicated ' + (STATS.adjudicated || 0))));
    } else {
      kids.push(h('div', { class: 'rv-headstats' }));
    }
    kids.push(h('button', { class: 'rv-linkbtn', onclick: signOut }, 'Sign out'));
    return h('header', { class: 'rv-header' }, kids);
  }

  function renderLogin(message) {
    var email = h('input', { type: 'email', placeholder: 'Email', autocomplete: 'username' });
    var pass = h('input', { type: 'password', placeholder: 'Password', autocomplete: 'current-password' });
    var errBox = h('div', { class: 'rv-error' }, message || '');
    var form = h('form', {
      onsubmit: function (ev) {
        ev.preventDefault();
        api('/auth/login', { method: 'POST', body: { email: email.value, password: pass.value } })
          .then(function (data) {
            token = data.token;
            try { localStorage.setItem(TOKEN_KEY, token); } catch (e) { /* ignore */ }
            boot();
          })
          .catch(function (err) { clear(errBox); appendChild(errBox, err.message); });
      } },
      h('div', { class: 'rv-card' },
        h('h3', null, 'Reviewer sign in'),
        email, pass, errBox,
        h('div', { class: 'rv-actions' },
          h('button', { class: 'rv-submit', type: 'submit' }, 'Sign in'))));
    mount(h('div', { class: 'rv-login' },
      h('div', { class: 'rv-chrome', style: 'margin-bottom:8px' }, 'Asclepius · Expert Review'),
      form));
  }

  function renderNotReviewer() {
    mount(header(), h('div', { class: 'rv-main' },
      h('div', { class: 'rv-card rv-empty' },
        h('p', null, 'This account does not have the reviewer tier yet.'),
        h('p', { class: 'rv-kv' },
          'Reviewer access is assigned after verification. You can keep labeling in the ',
          h('a', { href: '/asclepius' }, 'evaluation portal'), '.'))));
  }

  function renderLoading() {
    mount(header(), h('div', { class: 'rv-main' },
      h('div', { class: 'rv-empty' }, 'Loading…')));
  }

  function renderFatal(message) {
    mount(header(), h('div', { class: 'rv-main' },
      h('div', { class: 'rv-card' },
        h('div', { class: 'rv-error' }, message || 'Something went wrong.'),
        h('div', { class: 'rv-actions' },
          h('button', { class: 'rv-submit', onclick: loadNext }, 'Retry')))));
  }

  function renderEmpty(message) {
    var card = h('div', { class: 'rv-card rv-empty' },
      h('p', null, message || 'No cases awaiting review.'),
      h('div', { class: 'rv-actions', style: 'justify-content:center' },
        h('button', { class: 'rv-submit', onclick: loadNext }, 'Check again')));
    // Reviewers are physicians too — a case waiting for its second label is
    // work they can take, and the whole point of the priority rule is that it
    // gets taken.
    api('/review/double-label/next').then(function (data) {
      if (data && data.task) {
        appendChild(card, h('p', { class: 'rv-kv', style: 'margin-top:12px' },
          'A case is waiting for its second independent label (specialty: ',
          data.task.specialty || 'any', ') — ',
          h('a', { href: data.portal_url || '/asclepius' }, 'open the evaluation portal'), '.'));
      }
    }).catch(function () { /* pointer is best-effort */ });
    mount(header(), h('div', { class: 'rv-main' }, card));
  }

  // ── the case, collapsed by default ──────────────────────────────────────────
  function pretty(value) {
    return h('pre', { class: 'rv-mono' }, JSON.stringify(value, null, 1));
  }
  function fold(title, body) {
    return h('details', { style: 'margin-top:8px' },
      h('summary', { class: 'rv-kv' }, title), body);
  }

  function caseSection(task) {
    var kids = [];
    var c = task.case || null;
    kids.push(h('div', { class: 'rv-answer-text' }, task.prompt || ''));
    if (c) {
      if (c.demographics) kids.push(h('div', { class: 'rv-kv', style: 'margin-top:8px' },
        h('b', null, (c.demographics.age_band || 'adult') + ' ' + (c.demographics.sex || ''))));
      if (c.problem_list && c.problem_list.length) kids.push(h('div', { class: 'rv-kv' },
        (c.problem_list || []).map(function (p) {
          return h('div', null, '• ' + (p.condition || ''));
        })));
      if (c.medications && c.medications.length) kids.push(fold('Medications', pretty(c.medications)));
      if (c.lab_panels && c.lab_panels.length) kids.push(fold('Labs', pretty(c.lab_panels)));
      if (c.notes && c.notes.length) kids.push(fold('Notes',
        h('pre', { class: 'rv-mono' }, (c.notes || []).map(function (n) {
          return (n.note_type || 'note') + ': ' + (n.text || '');
        }).join('\n\n'))));
      if (c.studies && c.studies.length) kids.push(fold('Studies', pretty(c.studies)));
    }
    var candidates = task.candidate_answers || [];
    if (candidates.length) {
      kids.push(fold('Candidate answers shown to both physicians', candidates.map(function (cand) {
        return h('div', { style: 'margin-top:8px' },
          h('span', { class: 'rv-chip' }, 'Candidate ' + String(cand.id || '').toUpperCase()),
          h('div', { class: 'rv-answer-text' }, cand.text || ''));
      })));
    }
    return h('div', { class: 'rv-card' },
      h('details', { class: 'rv-case' },
        h('summary', null, 'The case — open only if you doubt something'),
        kids));
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
      h('div', { class: 'rv-kv' }, h('b', null, title)), body);
  }

  /* One physician's answer. Identical structure for A and B, identical accent,
     distinguished ONLY by the mono eyebrow and by which column it is in. */
  function answerCard(entry, task) {
    var a = entry.answer || {};
    var body = [];

    body.push(h('div', null,
      h('span', { class: 'rv-chip' }, verdictLabel(a.verdict)),
      ' ',
      entry.confidence ? h('span', { class: 'rv-chip' }, 'confidence: ' + entry.confidence) : null));

    var rev = a.chosen_revision || null;
    var finalText = null;
    if (rev && rev.edited && rev.revised_text) {
      finalText = rev.revised_text;
      body.push(section('Corrected answer (as submitted)',
        h('div', { class: 'rv-answer-text' }, rev.revised_text)));
      if (rev.why_better_notes) body.push(h('div', { class: 'rv-kv' }, 'Why: ' + rev.why_better_notes));
    } else if (a.verdict === 'A_better' || a.verdict === 'B_better') {
      finalText = candidateText(task, a.chosen_id);
      body.push(section('Chosen answer (unedited)',
        h('div', { class: 'rv-answer-text' }, finalText)));
    }

    var fs = a.from_scratch || null;
    if (fs && fs.ideal_answer) {
      finalText = fs.ideal_answer;
      body.push(section('Written from scratch',
        h('div', { class: 'rv-answer-text' }, fs.ideal_answer)));
      if (fs.approach_notes) body.push(h('div', { class: 'rv-kv' }, 'Approach: ' + fs.approach_notes));
    }

    var crit = a.rejected_critique || null;
    if (crit && (crit.why_worse || (crit.error_tags || []).length)) {
      var critKids = [];
      if ((crit.error_tags || []).length) critKids.push(h('div', null,
        crit.error_tags.map(function (t) {
          return h('span', { class: 'rv-chip', style: 'margin-right:6px' }, t);
        })));
      if (crit.why_worse) critKids.push(h('div', { class: 'rv-kv', style: 'margin-top:6px' }, crit.why_worse));
      body.push(section('Critique of the rejected answer', critKids));
    }

    var steps = a.reasoning_steps || [];
    if (steps.length) {
      body.push(section('Step annotations', steps.map(function (s, i) {
        var text = (s && (s.text || s.content || s.step)) || '';
        var note = s && (s.note || s.step_note);
        return h('div', { class: 'rv-step' },
          h('span', { class: 'rv-step-num' }, String(i + 1)),
          h('span', null, String(text), note ? h('div', { class: 'rv-kv' }, 'note: ' + note) : null));
      })));
    }

    var rubric = a.rubric || [];
    if (rubric.length) {
      body.push(section('Grading rubric (as confirmed)', rubric.map(function (cr) {
        var pts = (cr && cr.points != null) ? cr.points : '';
        return h('div', { class: 'rv-step' },
          h('span', { class: 'rv-step-num' }, String(pts)),
          h('span', null, (cr && (cr.text || cr.criterion)) || ''));
      })));
    }

    var ia = a.independent_answer || null;
    if (ia && ia.text) {
      body.push(section('Pre-reveal independent answer',
        h('div', { class: 'rv-answer-text' }, ia.text)));
    }
    if (a.citations) body.push(fold('Citations', pretty(a.citations)));

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

  function segmented(values, labels, onPick, datasetKey) {
    var wrap = h('div', { class: 'rv-seg' });
    values.forEach(function (v, i) {
      appendChild(wrap, h('button', {
        type: 'button', dataset: (function () { var d = {}; d[datasetKey] = v; return d; })(),
        onclick: function (ev) {
          Array.prototype.forEach.call(wrap.children, function (b) { b.classList.remove('is-on'); });
          ev.currentTarget.classList.add('is-on');
          onPick(v);
          refreshSubmit();
        } }, labels[i]));
    });
    return wrap;
  }

  function strongerRow() {
    var choices = (ME && ME.strength_choices) || ['A', 'B', 'equivalent'];
    var labels = choices.map(function (c) {
      return c === 'equivalent' ? 'Equivalent' : c;
    });
    return h('div', { class: 'rv-dim' },
      h('div', { class: 'rv-dim-label' }, 'Which is stronger?',
        h('small', null, 'the comparison a single-label review could not ask')),
      segmented(choices, labels, function (v) {
        R.stronger = v;
        // An edited accept follows the comparison. Changing the answer above
        // after choosing 'Accept with edits' must move it too, or the row
        // records an edit against a physician the reviewer did not name.
        if (R.verdict === 'accept_with_edits') {
          R.acceptedSide = (v === 'A' || v === 'B') ? v : null;
        }
      }, 'state'));
  }

  function dimensionRows() {
    return (ME.dimensions || []).map(function (d) {
      var key = d[0], label = d[1], hint = d[2];
      var states = (ME.dimension_states || ['agree', 'disagree', 'cannot_assess']);
      var labels = states.map(function (s) {
        return s === 'agree' ? 'Agree' : s === 'disagree' ? 'Disagree' : "Can't assess";
      });
      return h('div', { class: 'rv-dim' },
        h('div', { class: 'rv-dim-label' }, label, h('small', null, hint)),
        segmented(states, labels, function (v) { R.dimensions[key] = v; }, 'state'));
    });
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

  function overallButtons(prefill) {
    var wrap = h('div', { class: 'rv-verdicts' }, OVERALL.map(function (d) {
      return h('button', {
        // The button's identity is the verdict plus the side it FIXES. A side of
        // 'stronger' is deferred to the control above, so it names no column
        // here and the key stays the bare verdict.
        type: 'button',
        dataset: { verdict: d[0] + ((d[1] === 'A' || d[1] === 'B') ? ':' + d[1] : '') },
        onclick: function (ev) {
          R.verdict = d[0];
          // 'stronger' defers to the comparison the reviewer already made;
          // 'equivalent' names no side, which the server accepts for an edit.
          R.acceptedSide = d[1] === 'stronger'
            ? ((R.stronger === 'A' || R.stronger === 'B') ? R.stronger : null)
            : d[1];
          Array.prototype.forEach.call(wrap.children, function (b) { b.classList.remove('is-on'); });
          ev.currentTarget.classList.add('is-on');
          var needs = R.verdict === 'accept_with_edits' || R.verdict === 'reject';
          correctionsBox.style.display = needs ? '' : 'none';
          if (R.verdict === 'accept_with_edits' && !editedArea.value && prefill) {
            editedArea.value = prefill;
          }
          refreshSubmit();
        } },
        h('div', null, h('b', null, d[2])), h('small', { class: 'rv-kv' }, d[3]));
    }));
    return wrap;
  }

  function reviewComplete() {
    if (!R.verdict || !R.stronger) return false;
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
  function refreshSubmit() { if (submitBtn) submitBtn.disabled = !reviewComplete(); }

  function submitReview() {
    if (!reviewComplete() || !PAIR) return;
    submitBtn.disabled = true;
    var body = {
      verdict: R.verdict,
      stronger: R.stronger,
      accepted_side: R.acceptedSide,
      dimensions: R.dimensions,
      reviewer_notes: notesArea.value.trim() || null,
      time_spent_sec: Math.max(1, Math.round((Date.now() - R.startedAt) / 1000)),
    };
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
        clear(errLine); appendChild(errLine, err.message);
        refreshSubmit();
      });
  }

  function renderWithheld(flags) {
    mount(header(), h('div', { class: 'rv-main' },
      h('div', { class: 'rv-card' },
        h('h3', null, 'Your correction was withheld from the buyer bundle'),
        h('p', { class: 'rv-kv' },
          'Your adjudication is recorded. The free text is not being shipped, '
          + 'because it looks like it contains patient identifiers: ',
          h('b', null, (flags || []).join(', ') || 'unspecified'),
          '. Rewrite it without them on the next case, or tell us if this is '
          + 'a false positive.'),
        h('div', { class: 'rv-actions' },
          h('button', { class: 'rv-submit', onclick: loadNext }, 'Next case')))));
  }

  function renderReview() {
    var answers = PAIR.answers || [];
    var task = PAIR.task || {};
    var a = answerCard(answers[0] || { label: 'A' }, task);
    var b = answerCard(answers[1] || { label: 'B' }, task);

    correctionsBox = null; notesArea = null; editedArea = null; submitBtn = null; errLine = null;
    clockNoteEl = h('span', { class: 'asc-session-note' }, clockNote() || '');

    notesArea = h('textarea', { oninput: refreshSubmit,
      placeholder: 'What is wrong, and what should change? Required on edits and on reject.' });
    editedArea = h('textarea', { oninput: refreshSubmit,
      placeholder: 'Optional: the corrected answer text.' });
    correctionsBox = h('div', { style: 'display:none' },
      h('div', { class: 'rv-field' }, h('label', null, 'Corrections / reason'), notesArea),
      h('div', { class: 'rv-field' }, h('label', null, 'Edited answer'), editedArea));
    submitBtn = h('button', { class: 'rv-submit', disabled: true, onclick: submitReview },
      'Submit adjudication');
    errLine = h('span', { class: 'rv-error' });

    mount(
      header(),
      h('div', { class: 'rv-main rv-pair' },
        caseSection(task),
        // Chrome OUTSIDE the grid — see the PRD-R note in asclepius.css.
        h('div', { class: 'rv-pairbar' },
          h('span', { class: 'rv-chrome' }, 'Two independent labels · same case · no identity'),
          clockNoteEl),
        // EXACTLY two children. Nothing else may be added here.
        h('div', { class: 'asc-answers' }, a.card, b.card),
        h('div', { class: 'rv-card' },
          h('h3', null, 'Your judgment'),
          strongerRow(),
          dimensionRows(),
          h('div', { class: 'rv-kv', style: 'margin:12px 0 6px' }, h('b', null, 'Overall')),
          overallButtons(a.finalText || b.finalText),
          correctionsBox,
          h('div', { class: 'rv-actions' }, submitBtn, errLine))));
  }

  boot();
})();
