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

  function dossierPanel(row) {
    var d = state.dossiers[row.user_id];
    if (!d) return h('div', { class: 'vq-dossier' }, h('em', null, 'Loading dossier…'));

    var tierSelect = h('select', { class: 'vq-tier-select', 'aria-label': 'Tier' },
      h('option', { value: '' }, '— choose tier —'),
      h('option', { value: 'labeler' }, 'labeler (does cases from scratch)'),
      h('option', { value: 'reviewer' }, 'reviewer (grades submissions)'));
    var noteInput = h('textarea', {
      class: 'vq-note', rows: '2',
      placeholder: 'Note — required to reject, recommended always',
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

    return h('div', { class: 'vq-dossier' },
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
        noteInput,
        h('button', {
          class: 'vq-btn vq-btn-approve', disabled: state.busy,
          onclick: function () {
            if (!tierSelect.value) { setError('Pick an explicit tier to approve — the proposal is advice.'); return; }
            decide(row.user_id, 'approve', { tier: tierSelect.value, note: noteInput.value || null },
                   'Approved as ' + tierSelect.value + '.');
          },
        }, 'Approve'),
        h('button', {
          class: 'vq-btn vq-btn-reject', disabled: state.busy,
          onclick: function () {
            if (!noteInput.value.trim()) { setError('Rejection requires a note.'); return; }
            decide(row.user_id, 'reject', { note: noteInput.value.trim() }, 'Rejected.');
          },
        }, 'Reject')));
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
