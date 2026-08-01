/* Asclepius Expert Review portal (PRD A Phase 2).
 *
 * Standalone surface for the reviewer tier: draw a blinded labeler submission,
 * grade four dimensions (agree / disagree / cannot assess), give an overall
 * verdict, and only slow down where something is wrong. The case is collapsed
 * by default; corrections are revealed only on accept_with_edits / reject.
 *
 * All DOM is built with the h() hyperscript helper — no innerHTML, no HTML
 * string templates. The server serves a whitelisted payload with no labeler
 * identity; this file never receives it, so it cannot leak it.
 */
(function () {
  'use strict';

  var TOKEN_KEY = 'asclepius_token';
  var API_BASE = '/api/asclepius';

  var root = document.getElementById('reviewRoot');
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
  var ME = null;        // /review/me: vocab + can_review
  var VIEW = null;      // the blinded submission view being reviewed
  var STATS = null;
  var R = null;         // the review being authored
  function resetReview() {
    R = { verdict: null, dimensions: {}, notes: '', editedAnswer: '', startedAt: Date.now() };
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
      api('/review/next'),
      api('/review/stats').catch(function () { return null; }),
    ]).then(function (results) {
      VIEW = results[0].submission;
      STATS = results[1];
      resetReview();
      if (!VIEW) { renderEmpty(results[0].message); return; }
      renderReview();
    }).catch(function (err) {
      if (err.message !== 'Signed out') renderFatal(err.message);
    });
  }

  // ── screens ─────────────────────────────────────────────────────────────────
  function header() {
    var stats = STATS
      ? h('div', { class: 'rv-headstats' },
          h('span', { class: 'rv-chrome' }, 'Awaiting ' + STATS.unreviewed),
          h('span', { class: 'rv-chrome' }, 'In review ' + STATS.in_review),
          h('span', { class: 'rv-chrome' }, 'Reviewed ' + STATS.reviewed))
      : h('div', { class: 'rv-headstats' });
    return h('header', { class: 'rv-header' },
      h('h1', null, 'Expert Review'),
      stats,
      h('button', { class: 'rv-linkbtn', onclick: signOut }, 'Sign out'));
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
      h('p', null, message || 'No submissions awaiting review.'),
      h('div', { class: 'rv-actions', style: 'justify-content:center' },
        h('button', { class: 'rv-submit', onclick: loadNext }, 'Check again')));
    // Second-label pointer: reviewers are physicians too — offer the κ slice.
    api('/review/double-label/next').then(function (data) {
      if (data && data.task) {
        appendChild(card, h('p', { class: 'rv-kv', style: 'margin-top:12px' },
          'A case is waiting for an independent second label (specialty: ',
          data.task.specialty || 'any', ') — ',
          h('a', { href: data.portal_url || '/asclepius' }, 'open the evaluation portal'), '.'));
      }
    }).catch(function () { /* pointer is best-effort */ });
    mount(header(), h('div', { class: 'rv-main' }, card));
  }

  // ── case + answer rendering ─────────────────────────────────────────────────
  function pretty(value) {
    return h('pre', { class: 'rv-mono' }, JSON.stringify(value, null, 1));
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
      if (c.medications && c.medications.length) kids.push(sub('Medications', pretty(c.medications)));
      if (c.lab_panels && c.lab_panels.length) kids.push(sub('Labs', pretty(c.lab_panels)));
      if (c.notes && c.notes.length) kids.push(sub('Notes',
        h('pre', { class: 'rv-mono' }, (c.notes || []).map(function (n) {
          return (n.note_type || 'note') + ': ' + (n.text || '');
        }).join('\n\n'))));
      if (c.studies && c.studies.length) kids.push(sub('Studies', pretty(c.studies)));
    }
    var candidates = task.candidate_answers || [];
    if (candidates.length) {
      kids.push(sub('Candidate answers', candidates.map(function (cand) {
        return h('div', { style: 'margin-top:8px' },
          h('span', { class: 'rv-chip' }, 'Candidate ' + String(cand.id || '').toUpperCase()),
          h('div', { class: 'rv-answer-text' }, cand.text || ''));
      })));
    }
    return h('div', { class: 'rv-card' },
      h('details', { class: 'rv-case' },
        h('summary', null, 'The case — open only if you doubt something'),
        kids));
    function sub(title, body) {
      return h('details', { style: 'margin-top:8px' }, h('summary', { class: 'rv-kv' }, title), body);
    }
  }

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

  function answerSection(view) {
    var a = view.labeler_answer || {};
    var task = view.task || {};
    var kids = [
      h('div', null,
        h('span', { class: 'rv-chip' }, verdictLabel(a.verdict)),
        ' ',
        view.confidence ? h('span', { class: 'rv-chip' }, 'confidence: ' + view.confidence) : null,
        ' ',
        view.portal_version ? h('span', { class: 'rv-chip' }, view.portal_version) : null),
    ];
    var rev = a.chosen_revision || null;
    var finalText = null;
    if (rev && rev.edited && rev.revised_text) {
      finalText = rev.revised_text;
      kids.push(h('div', { class: 'rv-kv', style: 'margin-top:12px' }, h('b', null, 'Corrected answer (as submitted)')));
      kids.push(h('div', { class: 'rv-answer-text' }, rev.revised_text));
      if (rev.why_better_notes) kids.push(h('div', { class: 'rv-kv', style: 'margin-top:6px' },
        'Why: ' + rev.why_better_notes));
    } else if (a.verdict === 'A_better' || a.verdict === 'B_better') {
      finalText = candidateText(task, a.chosen_id);
      kids.push(h('div', { class: 'rv-kv', style: 'margin-top:12px' }, h('b', null, 'Chosen answer (unedited)')));
      kids.push(h('div', { class: 'rv-answer-text' }, finalText));
    }
    var fs = a.from_scratch || null;
    if (fs && fs.ideal_answer) {
      finalText = fs.ideal_answer;
      kids.push(h('div', { class: 'rv-kv', style: 'margin-top:12px' }, h('b', null, 'Labeler-written ideal answer')));
      kids.push(h('div', { class: 'rv-answer-text' }, fs.ideal_answer));
      if (fs.approach_notes) kids.push(h('div', { class: 'rv-kv', style: 'margin-top:6px' },
        'Approach: ' + fs.approach_notes));
    }
    var crit = a.rejected_critique || null;
    if (crit && (crit.why_worse || (crit.error_tags || []).length)) {
      kids.push(h('div', { class: 'rv-kv', style: 'margin-top:12px' }, h('b', null, 'Critique of the rejected answer')));
      if ((crit.error_tags || []).length) kids.push(h('div', null,
        crit.error_tags.map(function (t) { return h('span', { class: 'rv-chip', style: 'margin-right:6px' }, t); })));
      if (crit.why_worse) kids.push(h('div', { class: 'rv-kv', style: 'margin-top:6px' }, crit.why_worse));
    }
    var steps = a.reasoning_steps || [];
    if (steps.length) {
      kids.push(h('div', { class: 'rv-kv', style: 'margin-top:12px' }, h('b', null, 'Step annotations')));
      kids.push(steps.map(function (s, i) {
        var text = (s && (s.text || s.content || s.step)) || '';
        var note = s && (s.note || s.step_note);
        return h('div', { class: 'rv-step' },
          h('span', { class: 'rv-step-num' }, String(i + 1)),
          h('span', null, String(text), note ? h('div', { class: 'rv-kv' }, 'note: ' + note) : null));
      }));
    }
    var rubric = a.rubric || [];
    if (rubric.length) {
      kids.push(h('div', { class: 'rv-kv', style: 'margin-top:12px' }, h('b', null, 'Grading rubric (as confirmed)')));
      kids.push(rubric.map(function (cr) {
        var pts = (cr && cr.points != null) ? cr.points : '';
        return h('div', { class: 'rv-step' },
          h('span', { class: 'rv-step-num' }, String(pts)),
          h('span', null, (cr && (cr.text || cr.criterion)) || ''));
      }));
    }
    var ia = a.independent_answer || null;
    if (ia && ia.text) {
      kids.push(h('div', { class: 'rv-kv', style: 'margin-top:12px' },
        h('b', null, 'Pre-reveal independent answer' + (ia.kind ? ' (' + ia.kind + ')' : ''))));
      kids.push(h('div', { class: 'rv-answer-text' }, ia.text));
    }
    if (a.citations) kids.push(sub2('Citations', pretty(a.citations)));
    return { card: h('div', { class: 'rv-card' }, h('h3', null, "The labeler's answer"), kids),
             finalText: finalText };
    function sub2(title, body) {
      return h('details', { style: 'margin-top:8px' }, h('summary', { class: 'rv-kv' }, title), body);
    }
  }

  // ── the review controls ─────────────────────────────────────────────────────
  function dimensionRows() {
    return (ME.dimensions || []).map(function (d) {
      var key = d[0], label = d[1], hint = d[2];
      var seg = h('div', { class: 'rv-seg', dataset: { dim: key } },
        segBtn(key, 'agree', 'Agree'),
        segBtn(key, 'disagree', 'Disagree'),
        segBtn(key, 'cannot_assess', "Can't assess"));
      return h('div', { class: 'rv-dim' },
        h('div', { class: 'rv-dim-label' }, label, h('small', null, hint)),
        seg);
    });
  }
  function segBtn(dim, state, label) {
    return h('button', {
      type: 'button', dataset: { state: state },
      onclick: function (ev) {
        R.dimensions[dim] = state;
        var seg = ev.currentTarget.parentNode;
        Array.prototype.forEach.call(seg.children, function (b) { b.classList.remove('is-on'); });
        ev.currentTarget.classList.add('is-on');
        refreshSubmit();
      } }, label);
  }

  var correctionsBox = null;
  var notesArea = null;
  var editedArea = null;
  var submitBtn = null;
  var errLine = null;

  function verdictButtons(prefillEdited) {
    var defs = [
      ['accept', 'Accept', 'Good as submitted'],
      ['accept_with_edits', 'Accept with edits', 'Right call, needs corrections'],
      ['reject', 'Reject', 'Unusable — reason required'],
    ];
    var wrap = h('div', { class: 'rv-verdicts' }, defs.map(function (d) {
      return h('button', {
        type: 'button', dataset: { verdict: d[0] },
        onclick: function (ev) {
          R.verdict = d[0];
          Array.prototype.forEach.call(wrap.children, function (b) { b.classList.remove('is-on'); });
          ev.currentTarget.classList.add('is-on');
          var needsCorrections = R.verdict === 'accept_with_edits' || R.verdict === 'reject';
          correctionsBox.style.display = needsCorrections ? '' : 'none';
          if (R.verdict === 'accept_with_edits' && !editedArea.value && prefillEdited) {
            editedArea.value = prefillEdited;
          }
          refreshSubmit();
        } },
        h('div', null, h('b', null, d[1])), h('small', { class: 'rv-kv' }, d[2]));
    }));
    return wrap;
  }

  function reviewComplete() {
    if (!R.verdict) return false;
    var dims = ME.dimensions || [];
    for (var i = 0; i < dims.length; i++) {
      if (!R.dimensions[dims[i][0]]) return false;
    }
    if (R.verdict === 'reject' && !notesArea.value.trim()) return false;
    if (R.verdict === 'accept_with_edits' &&
        !notesArea.value.trim() && !editedArea.value.trim()) return false;
    return true;
  }
  function refreshSubmit() { if (submitBtn) submitBtn.disabled = !reviewComplete(); }

  function submitReview() {
    if (!reviewComplete() || !VIEW) return;
    submitBtn.disabled = true;
    var body = {
      verdict: R.verdict,
      dimensions: R.dimensions,
      reviewer_notes: notesArea.value.trim() || null,
      time_spent_sec: Math.max(1, Math.round((Date.now() - R.startedAt) / 1000)),
    };
    if (R.verdict === 'accept_with_edits' || R.verdict === 'reject') {
      var corrections = {};
      if (notesArea.value.trim()) corrections.notes = notesArea.value.trim();
      if (editedArea.value.trim()) corrections.edited_answer = editedArea.value.trim();
      if (Object.keys(corrections).length) body.corrections = corrections;
    }
    api('/review/' + encodeURIComponent(VIEW.submission_id), { method: 'POST', body: body })
      .then(function () { loadNext(); })
      .catch(function (err) {
        clear(errLine); appendChild(errLine, err.message);
        refreshSubmit();
      });
  }

  function renderReview() {
    var answer = answerSection(VIEW);
    correctionsBox = null; notesArea = null; editedArea = null; submitBtn = null; errLine = null;

    notesArea = h('textarea', { oninput: refreshSubmit,
      placeholder: 'What is wrong, and what should change? Required for reject.' });
    editedArea = h('textarea', { oninput: refreshSubmit,
      placeholder: 'Optional: the corrected answer text.' });
    correctionsBox = h('div', { style: 'display:none' },
      h('div', { class: 'rv-field' }, h('label', null, 'Corrections / reason'), notesArea),
      h('div', { class: 'rv-field' }, h('label', null, 'Edited answer'), editedArea));
    submitBtn = h('button', { class: 'rv-submit', disabled: true, onclick: submitReview }, 'Submit review');
    errLine = h('span', { class: 'rv-error' });

    mount(
      header(),
      h('div', { class: 'rv-main' },
        caseSection(VIEW.task || {}),
        answer.card,
        h('div', { class: 'rv-card' },
          h('h3', null, 'Your judgment'),
          dimensionRows(),
          h('div', { class: 'rv-kv', style: 'margin:12px 0 6px' }, h('b', null, 'Overall verdict')),
          verdictButtons(answer.finalText),
          correctionsBox,
          h('div', { class: 'rv-actions' }, submitBtn, errLine))));
  }

  boot();
})();
