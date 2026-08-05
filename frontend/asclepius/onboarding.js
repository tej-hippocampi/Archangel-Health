/* Physician verification queue (PRD B, Phase 5) — owned by PRD-B.
 *
 * Renders the admin approval queue: one row per pending physician with the
 * tier score, its reasons, and any blockers as pink flags; one click expands
 * the dossier; approve (explicit tier + note) / reject (note required) are two
 * buttons. Built to be workable at 50 signups/day — a review is one expand,
 * one glance, one click.
 *
 * ── Seam 1 — binding contract with PRD-C (context pack §2) ──────────────────
 * PRD-B owns this global. The name and shape must not change:
 *
 *     window.AsclepiusVerification = {
 *       mount:   function (containerEl, ctx) { ... },
 *       refresh: function () { ... },
 *     };
 *
 * PRD-C's admin_physicians.js calls `mount(el, ctx)`. Last round B exported
 * this while C probed for `renderVerificationQueue`; neither name existed in
 * the other's file, so the Physicians → Verification tab rendered a
 * placeholder claiming the queue "ships with the identity-verification work"
 * for a whole round — while the queue sat in the same merge. Renaming either
 * side reproduces exactly that bug.
 *
 * `ctx` is optional; unknown keys are ignored. The view otherwise manages
 * itself and fetches with the shared `asclepius_token` bearer. The
 * `ascVerifyRoot` auto-mount is kept as a fallback for direct embedding.
 */
(function () {
  'use strict';

  var API_BASE = '/api/asclepius';
  var TOKEN_KEY = 'asclepius_token';

  // ─── tiny DOM helper (mirror of asclepius.js h(); no innerHTML, ever) ─────
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        var v = attrs[k];
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'disabled') { if (v) el.setAttribute('disabled', ''); }
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') {
          el.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (k === 'value') { el.value = v; }
        else el.setAttribute(k, v);
      }
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { for (var i = 0; i < c.length; i++) append(el, c[i]); }
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  var _QUEUE_STATUSES = ['pending', 'approved', 'rejected'];
  var overrideToken = '';   // optionally supplied by the admin shell via mount(el, ctx)

  function token() {
    if (overrideToken) return overrideToken;
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = { 'Authorization': 'Bearer ' + token() };
    var body;
    if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.body);
    }
    return fetch(API_BASE + path, { method: opts.method || 'GET', headers: headers, body: body })
      .then(function (res) {
        return res.json().catch(function () { return null; }).then(function (data) {
          if (!res.ok) {
            var detail = data && data.detail ? data.detail : ('HTTP ' + res.status);
            throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
          }
          return data;
        });
      });
  }

  // ─── state ─────────────────────────────────────────────────────────────────
  var PAGE = 50;
  var state = {
    container: null,
    status: 'pending',
    queue: [],
    openId: null,        // expanded dossier user_id
    dossiers: {},        // user_id -> dossier payload
    busy: false,
    error: null,
    notice: null,
    offset: 0,
    total: 0,
    hasMore: false,
    stuck: 0,            // rows whose NPI check never reached an answer
  };

  function setError(msg) { state.error = msg; state.notice = null; render(); }
  function setNotice(msg) { state.notice = msg; state.error = null; render(); }

  function load() {
    state.busy = true; render();
    api('/verify/queue?status=' + encodeURIComponent(state.status) +
        '&limit=' + PAGE + '&offset=' + state.offset)
      .then(function (data) {
        state.queue = (data && data.queue) || [];
        state.total = (data && data.total) || state.queue.length;
        state.hasMore = !!(data && data.has_more);
        state.busy = false; state.error = null; render();
        return api('/verify/recheck-pending');
      })
      .then(function (data) {
        state.stuck = (data && data.count) || 0;
        render();
      })
      .catch(function (e) { state.busy = false; setError(e.message); });
  }

  function runPendingRechecks() {
    state.busy = true; render();
    api('/verify/recheck-pending', { method: 'POST', body: {} })
      .then(function (data) {
        state.busy = false;
        state.dossiers = {};
        setNotice('Rechecked ' + ((data && data.attempted) || 0) + ' record(s).');
        load();
      })
      .catch(function (e) { state.busy = false; setError(e.message); });
  }

  function loadDossier(userId) {
    api('/verify/queue/' + encodeURIComponent(userId))
      .then(function (d) { state.dossiers[userId] = d; render(); })
      .catch(function (e) { setError(e.message); });
  }

  function toggle(userId) {
    state.openId = state.openId === userId ? null : userId;
    if (state.openId && !state.dossiers[userId]) loadDossier(userId);
    render();
  }

  function decide(userId, action, payload, label) {
    state.busy = true; render();
    api('/verify/queue/' + encodeURIComponent(userId) + '/' + action,
        { method: 'POST', body: payload })
      .then(function () {
        state.busy = false;
        state.openId = null;
        delete state.dossiers[userId];
        setNotice(label);
        load();
      })
      .catch(function (e) { state.busy = false; setError(e.message); });
  }

  function recheckNpi(userId) {
    state.busy = true; render();
    api('/verify/queue/' + encodeURIComponent(userId) + '/recheck-npi', { method: 'POST', body: {} })
      .then(function () {
        state.busy = false;
        delete state.dossiers[userId];
        setNotice('NPI rechecked.');
        loadDossier(userId);
        load();
      })
      .catch(function (e) { state.busy = false; setError(e.message); });
  }

  function openCv(userId) {
    fetch(API_BASE + '/verify/queue/' + encodeURIComponent(userId) + '/cv',
          { headers: { 'Authorization': 'Bearer ' + token() } })
      .then(function (res) {
        if (!res.ok) throw new Error('CV not available (HTTP ' + res.status + ')');
        return res.blob();
      })
      .then(function (blob) { window.open(URL.createObjectURL(blob), '_blank'); })
      .catch(function (e) { setError(e.message); });
  }

  // ─── render ────────────────────────────────────────────────────────────────
  function scoreChip(row) {
    var cls = row.score >= 70 ? 'vq-score-high' : row.score >= 30 ? 'vq-score-mid' : 'vq-score-low';
    return h('span', { class: 'vq-score ' + cls, title: 'Tier score (advice, not a decision)' },
             String(row.score));
  }

  function tierBadge(row) {
    if (row.blockers && row.blockers.length) {
      return h('span', { class: 'vq-badge vq-badge-blocked' }, 'needs review');
    }
    if (!row.proposed_tier) return h('span', { class: 'vq-badge vq-badge-none' }, 'no proposal');
    return h('span', { class: 'vq-badge vq-badge-' + row.proposed_tier },
             'proposes ' + row.proposed_tier);
  }

  function npiLine(npi) {
    if (!npi || !npi.npi) return h('div', { class: 'vq-npi' }, 'No NPI provided');
    var res = npi.result || 'unchecked';
    return h('div', { class: 'vq-npi vq-npi-' + res },
      h('strong', null, 'NPI ' + npi.npi + ' — ' + res),
      npi.reason ? ' (' + npi.reason + ')' : '',
      npi.registry_name
        ? h('span', null, ' · NPPES: ' + npi.registry_name +
            (npi.credential ? ', ' + npi.credential : '') +
            (npi.taxonomy ? ' · ' + npi.taxonomy : ''))
        : null,
      // A failed check no longer overwrites the result, so it is reported
      // beside it. Hiding "we tried and could not reach NPPES" would leave
      // the admin unable to tell a never-checked record from a stuck one.
      npi.last_attempt
        ? h('div', { class: 'vq-attempt' },
            'Last attempt failed: ' + npi.last_attempt +
            (npi.last_attempt_at ? ' (' + npi.last_attempt_at + ')' : ''))
        : null);
  }

  /* ─── PRD-CRED · tiering panel (PRD C §6) ──────────────────────────────────
   * An admin who cannot see WHY will ignore the score, and then the scoring
   * system is decoration. So this renders four things and nothing else:
   * the proposed tier, the reasons ranked by contribution, where the score fell
   * relative to the two thresholds, and an explicit override — because the
   * override IS the training signal.
   *
   * Colour is meaning, not decoration (design system §5). PINK is reserved for a
   * genuine blocker: a failed hard gate, a duplicate NPI, an OIG hit. A merely
   * LOW score is not pink — it is the muted default. An UNRESOLVED gate is not
   * pink either: "we could not check" is not "this person failed", and painting
   * it as a blocker is how an admin learns to distrust the colour.
   *
   * No new CSS. asclepius.css is outside Agent C's write allowlist (context pack
   * §2), so every class below already exists in the PRD-B and PRD-C blocks.
   */
  function gateLine(gid, gate) {
    var cls = gate.state === 'fail' ? 'vq-flag'
            : gate.state === 'pass' ? 'vq-reason' : 'vq-attempt';
    var mark = gate.state === 'pass' ? '✓' : gate.state === 'fail' ? '✕' : '?';
    return h('div', { class: cls }, mark + '  ' + gid + ' · ' + gate.label
                                    + ' — ' + gate.detail);
  }

  function thresholdLine(t) {
    // Where the score fell, in words. A bare number tells an admin nothing about
    // whether it was close.
    var s = t.score, tr = t.thresholds.tr, tl = t.thresholds.tl;
    if (s >= tr) {
      return 'score ' + s.toFixed(2) + ' — ' + (s - tr).toFixed(2)
             + ' above the reviewer threshold (' + tr.toFixed(1) + ')';
    }
    if (s <= tl) {
      return 'score ' + s.toFixed(2) + ' — ' + (tl - s).toFixed(2)
             + ' below the labeler threshold (' + tl.toFixed(1) + ')';
    }
    return 'score ' + s.toFixed(2) + ' — between the thresholds ('
           + tl.toFixed(1) + ' … ' + tr.toFixed(1) + '), so the model defers to you';
  }

  function tieringPanel(row, d) {
    var t = d.tiering;
    if (!t) return null;
    if (t.error) {
      // A visible failure, never a silent placeholder.
      return h('div', { class: 'vq-error', role: 'alert' }, t.error);
    }

    var domainSelect = h('select', {
      class: 'vq-tier-select', 'aria-label': 'Score against which case domain',
      onchange: function () { reloadTiering(row.user_id, domainSelect.value); },
    });
    var domains = ['nephrology', 'cardiology', 'oncology'];
    if (t.case_domain && domains.indexOf(t.case_domain) === -1) domains.unshift(t.case_domain);
    domains.forEach(function (dom) {
      domainSelect.appendChild(h('option', { value: dom }, dom + ' case'));
    });
    // Set .value explicitly rather than relying on a `selected` attribute being
    // reflected back into it. Both work in a browser; only this one is readable
    // by the code below without a round-trip through the option list.
    domainSelect.value = t.case_domain || domains[0];

    var tierChip;
    if (t.gates_failed && t.gates_failed.length) {
      tierChip = h('span', { class: 'vq-badge vq-badge-blocked' }, 'ineligible — hard gate');
    } else if (t.proposed_tier) {
      tierChip = h('span', { class: 'vq-badge vq-badge-' + t.proposed_tier },
                   'proposes ' + t.proposed_tier);
    } else {
      tierChip = h('span', { class: 'vq-badge vq-badge-none' }, 'admin decision');
    }

    var decideSelect = h('select', { class: 'vq-tier-select', 'aria-label': 'Record tier' },
      h('option', { value: '' }, '— record a tier decision —'),
      h('option', { value: 'labeler' }, 'labeler'),
      h('option', { value: 'reviewer' }, 'reviewer'));

    return h('div', { class: 'vq-dossier' },
      h('div', { class: 'vq-section-label' }, 'Tier proposal'),
      h('div', { class: 'vq-facts' },
        tierChip,
        ' ',
        // Lime is "new / needs attention" in the locked palette — exactly right for
        // "this proposal was a deliberate probe, not the model's best guess".
        t.was_exploration
          ? h('span', { class: 'asc-badge asc-badge-lime',
                        title: 'Thompson-sampled: the model is uncertain here and is '
                               + 'deliberately probing. Your decision is worth more than usual.' },
              'Exploration')
          : null,
        ' ', domainSelect),
      h('div', { class: 'vq-reason' }, thresholdLine(t)),
      t.domain_match_why ? h('div', { class: 'vq-attempt' }, 'domain: ' + t.domain_match_why)
                         : null,

      h('div', { class: 'vq-section-label' }, 'Why — ranked by contribution'),
      (t.reasons || []).map(function (r) {
        var blocking = r.indexOf('BLOCKER') === 0;
        return h('div', { class: blocking ? 'vq-flag' : 'vq-reason' }, r);
      }),

      h('div', { class: 'vq-section-label' }, 'Hard gates — a score cannot open one'),
      Object.keys(t.gates || {}).sort().map(function (gid) {
        return gateLine(gid, t.gates[gid]);
      }),

      (t.tr_missing && t.tr_missing.length)
        ? h('div', { class: 'vq-attempt' },
            'Not yet reviewer-eligible: ' + t.tr_missing.join(' · '))
        : null,
      t.calibration
        ? h('div', { class: 'vq-attempt' },
            'Calibration exam ' + (t.calibration.composite * 100).toFixed(0) + '% agreement'
            + (t.calibration.tr_gate_passed ? ' — at the reviewer gate' : ' — below the gate'))
        : h('div', { class: 'vq-attempt' }, 'Calibration exam not sat'),
      t.leie_loaded_at
        ? null
        : h('div', { class: 'vq-attempt' },
            'OIG exclusion list has never been loaded, so gate A5 is unresolved for every '
            + 'physician. Load the monthly LEIE snapshot to close it.'),

      h('div', { class: 'vq-actions' },
        decideSelect,
        h('button', {
          class: 'vq-btn', disabled: state.busy,
          title: 'Records your judgment as a training observation without changing the '
                 + 'approval lifecycle. The override IS the training signal.',
          onclick: function () {
            if (!decideSelect.value) { setError('Pick a tier to record.'); return; }
            recordTierDecision(row.user_id, decideSelect.value, domainSelect.value);
          },
        }, 'Record decision')));
  }

  function reloadTiering(userId, domain) {
    api('/verify/queue/' + encodeURIComponent(userId)
        + '?case_domain=' + encodeURIComponent(domain))
      .then(function (d) { state.dossiers[userId] = d; render(); })
      .catch(function (e) { setError(e.message); });
  }

  function recordTierDecision(userId, tier, domain) {
    state.busy = true; render();
    api('/verify/tiering/' + encodeURIComponent(userId) + '/decide',
        { method: 'POST', body: { tier: tier, case_domain: domain || null } })
      .then(function (out) {
        state.busy = false;
        var flipped = out && out.decision && out.decision.was_flip;
        setNotice('Recorded ' + tier + (flipped ? ' — an override of the proposal, which is '
                                                  + 'the most informative kind.' : '.'));
        reloadTiering(userId, domain);
      })
      .catch(function (e) { state.busy = false; setError(e.message); });
  }

  function dossierPanel(row) {
    var d = state.dossiers[row.user_id];
    if (!d) return h('div', { class: 'vq-dossier' }, h('em', null, 'Loading dossier…'));

    var tierSelect = h('select', { class: 'vq-tier-select', 'aria-label': 'Tier' },
      h('option', { value: '' }, '— choose tier —'),
      h('option', { value: 'labeler' }, 'labeler (does cases from scratch)'),
      h('option', { value: 'reviewer' }, 'reviewer (grades submissions)'),
      // The third tier. Advisors are APPOINTED, never proposed by the score —
      // this option exists so the queue is not a dead end when the person in
      // front of you is already an agreed advisor, and it is why the agreement
      // field below appears the moment it is picked.
      h('option', { value: 'advisor' }, 'advisor (equity, not paid per task)'));
    var noteInput = h('textarea', {
      class: 'vq-note', rows: '2',
      placeholder: 'Note — required to reject, recommended always',
    });
    // Required only for 'advisor', and the server enforces it either way (a
    // 400 from POST /approve). Shown conditionally so the normal labeler /
    // reviewer path gains no extra field.
    var agreementInput = h('input', {
      class: 'vq-note', type: 'text', hidden: true,
      placeholder: 'Signed advisor agreement reference — required',
      'aria-label': 'Advisor agreement reference',
    });
    tierSelect.addEventListener('change', function () {
      if (tierSelect.value === 'advisor') agreementInput.removeAttribute('hidden');
      else agreementInput.setAttribute('hidden', '');
    });

    var cvBits = [];
    if (d.has_cv) {
      cvBits.push(h('button', { class: 'vq-link', onclick: function () { openCv(row.user_id); } },
                    'Open raw CV'));
      var cv = d.cv_parsed || {};
      if (cv.ok) {
        if (cv.years_in_practice != null) cvBits.push(h('span', null, ' · ~' + cv.years_in_practice + 'y in practice'));
        if (cv.board_certifications && cv.board_certifications.length) {
          cvBits.push(h('span', null, ' · boards: ' + cv.board_certifications.join('; ')));
        }
        if (cv.institutions && cv.institutions.length) {
          cvBits.push(h('div', { class: 'vq-cv-inst' },
                        'Training: ' + cv.institutions.slice(0, 4).join(' · ')));
        }
      } else {
        cvBits.push(h('span', null, ' · could not parse — review the raw file'));
      }
    } else {
      cvBits.push(h('span', null, 'No CV uploaded'));
    }

    return h('div', null,
      tieringPanel(row, d),
      h('div', { class: 'vq-dossier' },
      npiLine(d.npi),
      // Offer the retry whenever there is no definitive answer yet — not only
      // when the stored result literally reads 'unavailable', which is no
      // longer how a failed check is recorded.
      (d.npi && d.npi.npi && d.npi.recheck_pending)
        ? h('button', { class: 'vq-btn vq-btn-ghost', disabled: state.busy,
                        onclick: function () { recheckNpi(row.user_id); } }, 'Recheck NPI')
        : null,
      h('div', { class: 'vq-facts' },
        h('span', null, (d.email_domain_class || 'unclassified') + ' email'),
        d.phone ? h('span', null, ' · ' + d.phone) : null,
        d.linkedin_url
          ? h('span', null, ' · ', h('a', { href: d.linkedin_url, target: '_blank',
                                            rel: 'noopener noreferrer' }, 'LinkedIn'))
          : null,
        d.years_experience != null ? h('span', null, ' · ' + d.years_experience + 'y stated') : null,
        d.board_cert ? h('span', null, ' · ' + d.board_cert) : null),
      h('div', { class: 'vq-cv' }, cvBits),
      h('div', { class: 'vq-reasons' },
        h('div', { class: 'vq-section-label' }, 'Why this score'),
        (d.reasons || []).map(function (r) { return h('div', { class: 'vq-reason' }, r); })),
      (d.blockers && d.blockers.length)
        ? h('div', { class: 'vq-blockers' },
            (d.blockers || []).map(function (b) { return h('div', { class: 'vq-flag' }, b); }))
        : null,
      (d.duplicate_claims && d.duplicate_claims.length)
        ? h('div', { class: 'vq-dupes' },
            'Also claiming this NPI: ' +
            d.duplicate_claims.map(function (x) { return x.email; }).join(', '))
        : null,
      h('div', { class: 'vq-actions' },
        tierSelect,
        agreementInput,
        noteInput,
        h('button', {
          class: 'vq-btn vq-btn-approve', disabled: state.busy,
          onclick: function () {
            if (!tierSelect.value) { setError('Pick an explicit tier to approve — the proposal is advice.'); return; }
            var agreement = (agreementInput.value || '').trim();
            if (tierSelect.value === 'advisor' && !agreement) {
              setError('An advisor holds equity — record the signed agreement reference.');
              return;
            }
            decide(row.user_id, 'approve',
                   { tier: tierSelect.value, note: noteInput.value || null,
                     agreement_ref: agreement || null },
                   'Approved as ' + tierSelect.value + '.');
          },
        }, 'Approve'),
        h('button', {
          class: 'vq-btn vq-btn-reject', disabled: state.busy,
          onclick: function () {
            if (!noteInput.value.trim()) { setError('Rejection requires a note.'); return; }
            decide(row.user_id, 'reject', { note: noteInput.value.trim() }, 'Rejected.');
          },
        }, 'Reject'))));
  }

  function rowEl(row) {
    var open = state.openId === row.user_id;
    return h('div', { class: 'vq-row' + (open ? ' vq-row-open' : '') },
      h('button', { class: 'vq-row-head', onclick: function () { toggle(row.user_id); } },
        h('div', { class: 'vq-who' },
          h('strong', null, row.full_name || row.email),
          h('span', { class: 'vq-sub' },
            [row.specialty, row.org_name, row.email].filter(Boolean).join(' · '))),
        h('div', { class: 'vq-row-side' },
          (row.blockers || []).length
            ? h('span', { class: 'vq-flag vq-flag-mini' },
                (row.blockers.length === 1 ? row.blockers[0] : row.blockers.length + ' flags'))
            : null,
          tierBadge(row),
          scoreChip(row))),
      open ? dossierPanel(row) : null);
  }

  function render() {
    var el = state.container;
    if (!el) return;
    clear(el);
    el.appendChild(
      h('div', { class: 'vq' },
        h('div', { class: 'vq-header' },
          h('div', null,
            h('h2', { class: 'vq-title' }, 'Physician verification'),
            h('span', { class: 'vq-count' },
              state.queue.length + ' ' + state.status)),
          h('div', { class: 'vq-controls' },
            ['pending', 'approved', 'rejected'].map(function (s) {
              return h('button', {
                class: 'vq-tab' + (state.status === s ? ' vq-tab-on' : ''),
                onclick: function () {
                  state.status = s; state.openId = null; state.offset = 0; load();
                },
              }, s);
            }),
            h('button', { class: 'vq-btn vq-btn-ghost', disabled: state.busy,
                          onclick: load }, state.busy ? 'Loading…' : 'Refresh'))),
        state.error ? h('div', { class: 'vq-error', role: 'alert' }, state.error) : null,
        state.notice ? h('div', { class: 'vq-notice', role: 'status' }, state.notice) : null,
        // The retry list: records whose NPI check never reached an answer.
        // Without this they sit invisible until someone opens each dossier.
        state.stuck
          ? h('div', { class: 'vq-stuck' },
              state.stuck + ' record(s) awaiting an NPI recheck — the registry could '
              + 'not be reached. ',
              h('button', { class: 'vq-link', disabled: state.busy,
                            onclick: runPendingRechecks }, 'Recheck them now'))
          : null,
        state.queue.length
          ? state.queue.map(rowEl)
          : h('div', { class: 'vq-empty' },
              state.busy ? 'Loading…' : 'Queue is clear — nothing awaiting review.'),
        (state.offset > 0 || state.hasMore)
          ? h('div', { class: 'vq-pager' },
              h('button', {
                class: 'vq-btn vq-btn-ghost', disabled: state.busy || state.offset === 0,
                onclick: function () {
                  state.offset = Math.max(0, state.offset - PAGE);
                  state.openId = null; load();
                },
              }, '← Newer'),
              h('span', { class: 'vq-count' },
                (state.offset + 1) + '–' + (state.offset + state.queue.length) +
                ' of ' + state.total),
              h('button', {
                class: 'vq-btn vq-btn-ghost', disabled: state.busy || !state.hasMore,
                onclick: function () {
                  state.offset += PAGE; state.openId = null; load();
                },
              }, 'Older →'))
          : null));
  }

  /** Seam 1 (context pack §2). PRD-B owns this global; PRD-C's
   *  admin_physicians.js calls `window.AsclepiusVerification.mount(el, ctx)`.
   *  The name and this two-argument shape must not change — last round B
   *  exported `AsclepiusVerification` while C probed for
   *  `renderVerificationQueue`, so the tab rendered a placeholder saying the
   *  queue "ships with the identity-verification work" for the entire round
   *  while the queue sat in the same merge.
   *
   *  `ctx` is optional and accepted defensively: an admin shell may pass a
   *  token or a status to open on, and ignoring extra keys is what keeps this
   *  callable before C decides what to send. */
  function mount(container, ctx) {
    if (!container) return;
    state.container = container;
    ctx = ctx || {};
    if (typeof ctx.token === 'string' && ctx.token) overrideToken = ctx.token;
    if (typeof ctx.status === 'string' && _QUEUE_STATUSES.indexOf(ctx.status) !== -1) {
      state.status = ctx.status;
    }
    state.offset = 0;
    render();
    load();
  }

  window.AsclepiusVerification = { mount: mount, refresh: load };

  document.addEventListener('DOMContentLoaded', function () {
    var auto = document.getElementById('ascVerifyRoot');
    if (auto) mount(auto);
  });
})();
