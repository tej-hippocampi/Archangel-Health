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
    tierWords: null,     // server-supplied {token: word} — never written in this file
  };

  function setError(msg) { state.error = msg; state.notice = null; render(); }
  function setNotice(msg) { state.notice = msg; state.error = null; render(); }

  function load() {
    state.busy = true; render();
    api('/verify/queue?status=' + encodeURIComponent(state.status) +
        '&limit=' + PAGE + '&offset=' + state.offset)
      .then(function (data) {
        state.queue = (data && data.queue) || [];
        state.tierWords = (data && data.tier_words) || state.tierWords;
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
    // AUDIT UI: the WORD. This badge predates the tiering panel and rendered
    // `proposes labeler` — the same violation, one line higher up the page.
    return h('span', { class: 'vq-badge vq-badge-' + row.proposed_tier },
             'proposes ' + (row.proposed_tier_word || tierWord(row.proposed_tier)));
  }

  // Fallback only. The server sends `tier_words` on every queue and dossier payload
  // (capabilities.TIER_WORDS is the single source); this exists so a stale cached
  // response degrades to a capitalised word rather than a raw token.
  function tierWord(t) {
    if (state.tierWords && state.tierWords[t]) return state.tierWords[t];
    if (!t) return 'Unassigned';
    return t.charAt(0).toUpperCase() + t.slice(1);
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

    // AUDIT UI: the domain list is SERVER-SUPPLIED. A hardcoded
    // ['nephrology','cardiology','oncology'] here is wrong the week a specialty is
    // enabled — and wrong silently, because the picker simply lacks an entry and
    // nobody notices they cannot score against the new domain.
    var domainSelect = h('select', {
      class: 'vq-tier-select', 'aria-label': 'Score against which case domain',
      onchange: function () { reloadTiering(row.user_id, domainSelect.value); },
    });
    var domains = (t.available_domains || []).slice();
    if (t.case_domain && !domains.some(function (d) { return d.value === t.case_domain; })) {
      domains.unshift({ value: t.case_domain, word: t.case_domain });
    }
    domains.forEach(function (dom) {
      domainSelect.appendChild(h('option', { value: dom.value }, dom.word + ' case'));
    });
    // Set .value explicitly rather than relying on a `selected` attribute being
    // reflected back into it. Both work in a browser; only this one is readable
    // by the code below without a round-trip through the option list.
    domainSelect.value = t.case_domain || (domains[0] && domains[0].value) || '';

    var tierChip;
    if (t.gates_failed && t.gates_failed.length) {
      tierChip = h('span', { class: 'vq-badge vq-badge-blocked' }, 'ineligible — hard gate');
    } else if (t.proposed_tier) {
      // AUDIT UI: the WORD, from the server. capabilities.tier_word() is the single place
      // that knows these, and the context pack states twice that a raw token must never
      // reach a human.
      tierChip = h('span', { class: 'vq-badge vq-badge-' + t.proposed_tier },
                   'proposes ' + (t.proposed_tier_word || t.proposed_tier));
    } else {
      tierChip = h('span', { class: 'vq-badge vq-badge-none' }, 'admin decision');
    }

    var decideSelect = h('select', { class: 'vq-tier-select', 'aria-label': 'Record tier' },
      h('option', { value: '' }, '— record a tier decision —'));
    // Server-supplied, worded. Never a hand-written list of tier strings (context pack §8:
    // capabilities.TIERS is the single enumeration).
    (t.tier_options || []).forEach(function (opt) {
      decideSelect.appendChild(h('option', { value: opt.value }, opt.word));
    });
    function tierWordOf(value) {
      var hit = (t.tier_options || []).filter(function (o) { return o.value === value; })[0];
      return hit ? hit.word : value;
    }

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
            recordTierDecision(row.user_id, decideSelect.value, domainSelect.value,
                               tierWordOf(decideSelect.value));
          },
        }, 'Record decision')));
  }

  function reloadTiering(userId, domain) {
    api('/verify/queue/' + encodeURIComponent(userId)
        + '?case_domain=' + encodeURIComponent(domain))
      .then(function (d) { state.dossiers[userId] = d; render(); })
      .catch(function (e) { setError(e.message); });
  }

  function recordTierDecision(userId, tier, domain, tierWord) {
    state.busy = true; render();
    api('/verify/tiering/' + encodeURIComponent(userId) + '/decide',
        { method: 'POST', body: { tier: tier, case_domain: domain || null } })
      .then(function (out) {
        state.busy = false;
        var flipped = out && out.decision && out.decision.was_flip;
        setNotice('Recorded ' + (tierWord || tier)
                  + (flipped ? ' — an override of the proposal, which is '
                               + 'the most informative kind.' : '.'));
        reloadTiering(userId, domain);
      })
      .catch(function (e) { state.busy = false; setError(e.message); });
  }

  /* The non-US identity line. Same shape as npiLine, different registry, and
     one extra job: for the countries whose registers are captcha-walled,
     priced or absent, it has to tell the admin what to actually do instead. */
  function registryLine(r) {
    var result = r.result || 'pending';
    var head = r.identifier
      ? r.id_label + ' ' + r.identifier + ': ' + result
      : 'No registration number on file';
    return h('div', { class: 'vq-npi vq-npi-' + result },
      h('strong', null, head),
      r.reason ? ' (' + String(r.reason).replace(/_/g, ' ') + ')' : '',
      h('div', { class: 'vq-attempt' },
        r.registry_name + (r.country_name ? ' · ' + r.country_name : '')),
      (r.record && (r.record.full_name || r.record.status))
        ? h('div', { class: 'vq-attempt' },
            'Registry says: ' + [r.record.full_name, r.record.council,
                                 r.record.status, r.record.valid_until]
              .filter(Boolean).join(' · '))
        : null,
      r.note ? h('div', { class: 'vq-attempt' }, r.note) : null,
      r.lookup_url
        ? h('div', null, h('a', { href: r.lookup_url, target: '_blank',
                                  rel: 'noopener noreferrer' },
            'Open the official register'))
        : null);
  }

  /* What did not hold together about this signup. Never a rejection: these
     suppress the automatic proposal and put a person in front of it. */
  var FLAG_SEVERITY_CLASS = {
    high: 'vq-flag-high', medium: 'vq-flag-medium', low: 'vq-flag-low',
  };

  function flagsBlock(flags) {
    if (!flags || !flags.length) return null;
    var box = h('div', { class: 'vq-flags' });
    box.appendChild(h('div', { class: 'vq-flags-head' },
      'Worth a look (' + flags.length + ')'));
    flags.forEach(function (f) {
      box.appendChild(h('div', { class: 'vq-flag' },
        h('span', {
          class: 'vq-flag-sev '
            + (FLAG_SEVERITY_CLASS[f.severity] || FLAG_SEVERITY_CLASS.low),
        }, (f.severity || 'low').toUpperCase()),
        ' ' + String(f.field || '').replace(/_/g, ' ') + ': '
        + String(f.issue || '').replace(/_/g, ' ')
        + (f.detail ? ' ' + f.detail : '')));
    });
    return box;
  }

  function credentialsAsTyped(c, a) {
    var rows = [];
    function push(k, v) { if (v !== null && v !== undefined && v !== '') rows.push([k, String(v)]); }
    push('Full legal name', c.fullLegalName);
    push('Qualification', c.qualification || c.degree);
    push('Primary specialty', c.primarySpecialty);
    push('Licence number', c.licenseNumber);
    push('Licence state', c.licenseState);
    push('Registration number', c.registrationNumber);
    Object.keys(c.registryExtras || {}).forEach(function (k) {
      push(k.replace(/([A-Z])/g, ' $1').toLowerCase(), c.registryExtras[k]);
    });
    function one(row) {
      if (!row) return null;
      var r = Array.isArray(row) ? row[0] : row;
      if (!r) return null;
      return [r.institution, r.year].filter(Boolean).join(', ') || null;
    }
    push('Residency', one(c.residency));
    push('Fellowship', one(c.fellowship));
    push('Practice status', c.practiceStatus);
    push('Clinical half-days / month', c.clinicalHalfDaysPerMonth);
    push('Languages', (c.languages || []).join(', '));
    (c.boardCertifications || []).forEach(function (b, i) {
      if (!b || !b.board) return;
      push('Board certification' + (i ? ' ' + (i + 1) : ''),
           [b.board, b.specialty, b.subspecialty].filter(Boolean).join(', '));
    });
    push('Signed with', a.signedInitials);
    if (!rows.length) return null;
    var box = h('div', { class: 'vq-typed' });
    box.appendChild(h('div', { class: 'vq-section-label' }, 'Credentials as typed'));
    rows.forEach(function (kv) {
      box.appendChild(h('div', { class: 'vq-typed-row' },
        h('span', { class: 'vq-typed-key' }, kv[0]),
        h('span', { class: 'vq-typed-val' }, kv[1])));
    });
    return box;
  }

  function dossierPanel(row) {
    var d = state.dossiers[row.user_id];
    if (!d) return h('div', { class: 'vq-dossier' }, h('em', null, 'Loading dossier…'));

    // AUDIT UI: worded options, from the server's tier_words map. What each tier DOES stays
    // in the option text — that is the part an admin actually needs — but the tier itself is
    // never rendered as a raw token.
    var TIER_BLURB = {
      labeler: 'builds a case answer from scratch',
      reviewer: 'grades what two labelers produced',
    };
    var words = (d && d.tier_words) || state.tierWords || {};
    var tierSelect = h('select', { class: 'vq-tier-select', 'aria-label': 'Tier' },
      h('option', { value: '' }, '— choose tier —'));
    Object.keys(words).forEach(function (t) {
      tierSelect.appendChild(h('option', { value: t },
        words[t] + (TIER_BLURB[t] ? ' (' + TIER_BLURB[t] + ')' : '')));
    });
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

    return h('div', null,
      tieringPanel(row, d),
      h('div', { class: 'vq-dossier' },
      // The identity line for whichever registry answers for this doctor.
      // Showing "No NPI provided" over a physician registered with SCFHS
      // reports an absence that was never expected in the first place.
      (d.registry && d.registry.is_us === false)
        ? registryLine(d.registry)
        : npiLine(d.npi),
      flagsBlock(d.flags),
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
      // The credentials blob has been on this payload since the queue was
      // built and was rendered by nothing, so the licence number, the
      // training, the practice status and the initials someone signed with
      // were invisible on the one screen where a person decides whether to
      // believe them.
      credentialsAsTyped(d.credentials || {}, d.attestations || {}),
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
            decide(row.user_id, 'approve',
                   { tier: tierSelect.value, note: noteInput.value || null },
                   'Approved as ' + (words[tierSelect.value] || tierSelect.value) + '.');
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

  /* ═══════════════════════════════════════════════════════════════════════════
   * AUDIT M2 — the candidate-facing screens
   *
   * `grep -rn "calibration/exam\|verify/demographics" frontend/ landing/` returned
   * NO MATCHES before this. The exam gate was reachable only by a raw API call, so
   * even with a fully keyed item bank **no physician could sit it through the
   * product** — and the fairness monitor, which is structurally correct and
   * correctly pseudonymised, was permanently empty because nothing collected the
   * input. A gate nobody can attempt is a gate nobody passes.
   *
   * Both live here rather than in asclepius.js because asclepius.js is outside
   * Agent C's write allowlist (context pack §2) — and because this file is already
   * loaded by index.html on every portal page, which is what makes a self-mounting
   * hash route work with no other file changing:
   *
   *     /asclepius#/calibration     sit the calibration exam
   *     /asclepius#/demographics    voluntary self-report for the fairness monitor
   *
   * `mount(el, ctx)` is exported too, so when the portal shell gains a proper entry
   * point it is a one-line change there and nothing here.
   * ═══════════════════════════════════════════════════════════════════════════ */

  function sheet(title, children, onClose) {
    var card = h('div', { class: 'asc-sheet-card' },
      h('h2', { class: 'asc-sheet-title' }, title));
    append(card, children);
    if (onClose) {
      card.appendChild(h('div', { class: 'vq-actions' },
        h('button', { class: 'vq-btn vq-btn-ghost', onclick: onClose }, 'Close')));
    }
    return h('div', { class: 'asc-sheet' }, card);
  }

  // ─── Calibration exam ──────────────────────────────────────────────────────
  var cal = { container: null, exam: null, i: 0, answers: {}, busy: false,
              error: null, notReady: null, result: null };

  function calRender() {
    var el = cal.container;
    if (!el) return;
    clear(el);

    if (cal.notReady) {
      // 503 is "not ready", NOT "you failed". A physician who reads it as a rejection
      // is a physician we lose for a reason that is entirely our own.
      el.appendChild(sheet('Calibration exam', [
        h('div', { class: 'asc-inline-error' },
          'The calibration exam is not yet open for your specialty. This is not a '
          + 'result and it is not about you — we are still assembling the reference '
          + 'panel that scores it. You will be emailed the moment it opens.'),
        h('div', { class: 'vq-attempt' }, cal.notReady),
      ], calClose));
      return;
    }
    if (cal.result) {
      var r = cal.result;
      el.appendChild(sheet('Calibration exam — submitted', [
        h('div', { class: 'vq-reason' },
          'Agreement with the reference panel: '
          + Math.round((r.composite || 0) * 100) + '%'),
        h('div', { class: 'vq-attempt' },
          r.tr_gate_passed
            ? 'That clears the reviewer gate. An admin still makes the final call.'
            : 'That is below the reviewer gate. It does not affect your labeling work.'),
        h('div', { class: 'vq-attempt' },
          'Your individual answers are stored so the exam can be re-scored if the '
          + 'panel re-keys an item — you will never be asked to sit it again for that.'),
      ], calClose));
      return;
    }
    if (!cal.exam) {
      el.appendChild(sheet('Calibration exam',
        [h('div', { class: 'vq-attempt' }, cal.error || 'Loading…'),
         cal.error ? h('div', { class: 'asc-inline-error' }, cal.error) : null],
        calClose));
      return;
    }

    var item = cal.exam.items[cal.i];
    var total = cal.exam.items.length;
    var mine = cal.answers[item.item_id] || {};

    function radioRow(name, value, label, current, onPick) {
      var input = h('input', { type: 'radio', name: name, value: value,
                               id: name + '-' + value });
      if (current === value) input.checked = true;
      input.addEventListener('change', function () { onPick(value); });
      return h('label', { class: 'vq-reason', for: name + '-' + value }, input, ' ', label);
    }

    function setAnswer(patch) {
      var next = {};
      for (var k in mine) next[k] = mine[k];
      for (var j in patch) next[j] = patch[j];
      cal.answers[item.item_id] = next;
      calRender();
    }

    var body = [
      h('div', { class: 'vq-attempt' }, 'Case ' + (cal.i + 1) + ' of ' + total),
      h('div', { class: 'vq-reason' }, item.stem),

      h('div', { class: 'vq-section-label' }, 'Which answer is better?'),
      (item.answers || []).map(function (a) {
        return radioRow('answer-' + item.item_id, a.id, a.text, mine.better_answer_id,
                        function (v) { setAnswer({ better_answer_id: v }); });
      }),

      (item.steps || []).length
        ? [h('div', { class: 'vq-section-label' },
             'Where does the weaker answer’s reasoning first break?'),
           (item.steps || []).map(function (st) {
             return radioRow('step-' + item.item_id, st.id, st.text, mine.break_step,
                             function (v) { setAnswer({ break_step: v }); });
           })]
        : null,

      (item.criteria || []).length
        ? [h('div', { class: 'vq-section-label' },
             'Which criteria are load-bearing here? (choose any)'),
           (item.criteria || []).map(function (c) {
             var chosen = (mine.load_bearing || []).indexOf(c) !== -1;
             var box = h('input', { type: 'checkbox', id: 'lb-' + item.item_id + '-' + c });
             if (chosen) box.checked = true;
             box.addEventListener('change', function () {
               var list = (mine.load_bearing || []).slice();
               var at = list.indexOf(c);
               if (at === -1) list.push(c); else list.splice(at, 1);
               setAnswer({ load_bearing: list });
             });
             return h('label', { class: 'vq-reason',
                                 for: 'lb-' + item.item_id + '-' + c }, box, ' ', c);
           })]
        : null,

      cal.error ? h('div', { class: 'asc-inline-error' }, cal.error) : null,

      h('div', { class: 'vq-actions' },
        cal.i > 0
          ? h('button', { class: 'vq-btn vq-btn-ghost', disabled: cal.busy,
                          onclick: function () { cal.i -= 1; calRender(); } }, 'Back')
          : null,
        cal.i < total - 1
          ? h('button', { class: 'vq-btn', disabled: cal.busy,
                          onclick: function () { cal.i += 1; calRender(); } }, 'Next')
          : h('button', { class: 'vq-btn vq-btn-approve', disabled: cal.busy,
                          onclick: calSubmit }, cal.busy ? 'Submitting…' : 'Submit')),
    ];
    el.appendChild(sheet('Calibration exam', body, calClose));
  }

  function calSubmit() {
    cal.busy = true; cal.error = null; calRender();
    api('/verify/calibration/' + encodeURIComponent(cal.exam.attempt_id) + '/submit',
        { method: 'POST', body: { responses: cal.answers } })
      .then(function (out) { cal.busy = false; cal.result = out; calRender(); })
      .catch(function (e) { cal.busy = false; cal.error = e.message; calRender(); });
  }

  function calClose() {
    if (cal.container && cal.container.parentNode === document.body) {
      document.body.removeChild(cal.container);
    }
    if (window.location && window.location.hash) window.location.hash = '';
  }

  function calMount(container, ctx) {
    if (!container) return;
    ctx = ctx || {};
    if (typeof ctx.token === 'string' && ctx.token) overrideToken = ctx.token;
    cal.container = container;
    cal.exam = null; cal.i = 0; cal.answers = {}; cal.result = null;
    cal.error = null; cal.notReady = null;
    calRender();
    var path = '/verify/calibration/exam'
      + (ctx.specialty ? '?specialty=' + encodeURIComponent(ctx.specialty) : '');
    api(path)
      .then(function (exam) {
        cal.exam = exam;
        // A resumed attempt is the server refusing to reroll the item sample. Say so,
        // rather than letting it look like the exam forgot what they answered.
        if (exam && exam.resumed) {
          cal.error = 'Resuming the attempt you already started — the same cases, so the '
                    + 'exam cannot be re-rolled for an easier draw.';
        }
        calRender();
      })
      .catch(function (e) {
        var msg = e.message || '';
        if (msg.indexOf('admissible keyed items') !== -1 || msg.indexOf('not ready') !== -1) {
          cal.notReady = msg;
        } else {
          cal.error = msg;
        }
        calRender();
      });
  }

  // ─── Voluntary demographics (fairness monitor input) ───────────────────────
  // Deliberately shaped so the honest answer is always available: every dimension
  // offers "Prefer not to say", nothing is required, and the page states plainly
  // what the data is and is not used for. A form that coerces produces a monitor
  // that measures who felt coerced.
  var DEMOGRAPHIC_DIMENSIONS = [
    { key: 'gender', label: 'Gender', options: [
      ['woman', 'Woman'], ['man', 'Man'], ['non_binary', 'Non-binary'],
      ['self_describe', 'Prefer to self-describe'] ] },
    { key: 'race_ethnicity', label: 'Race / ethnicity', options: [
      ['american_indian', 'American Indian or Alaska Native'],
      ['asian', 'Asian'], ['black', 'Black or African American'],
      ['hispanic_latino', 'Hispanic or Latino'],
      ['native_hawaiian', 'Native Hawaiian or Pacific Islander'],
      ['white', 'White'], ['two_or_more', 'Two or more'] ] },
    { key: 'img', label: 'Trained outside the United States', options: [
      ['yes', 'Yes'], ['no', 'No'] ] },
    { key: 'disability', label: 'Disability status', options: [
      ['yes', 'Yes'], ['no', 'No'] ] },
  ];

  var demo = { container: null, userId: null, answers: {}, busy: false,
               error: null, done: false };

  function demoRender() {
    var el = demo.container;
    if (!el) return;
    clear(el);
    if (demo.done) {
      el.appendChild(sheet('Thank you', [
        h('div', { class: 'vq-reason' },
          'Recorded. It is stored separately from your account under a one-way '
          + 'pseudonym, and it is never used to decide anything about you.'),
      ], demoClose));
      return;
    }

    var body = [
      h('div', { class: 'vq-reason' },
        'This is voluntary, and every question can be skipped.'),
      h('div', { class: 'vq-attempt' },
        'We use it for one thing: a weekly fairness check on how reviewer roles are '
        + 'assigned across the whole contributor pool. It is stored in a separate table '
        + 'under a one-way pseudonym, it is never joined back to your record, and the '
        + 'model that proposes your tier can never read it. Nothing here is inferred '
        + 'from your name, your school, or where you practise — if you skip a '
        + 'question, we simply do not have the answer.'),
    ];

    DEMOGRAPHIC_DIMENSIONS.forEach(function (dim) {
      var sel = h('select', { class: 'vq-tier-select', 'aria-label': dim.label },
        h('option', { value: '' }, '— skip —'));
      dim.options.forEach(function (o) {
        sel.appendChild(h('option', { value: o[0] }, o[1]));
      });
      // Offered on EVERY dimension, always last: a form without it coerces an answer.
      sel.appendChild(h('option', { value: 'prefer_not_to_say' }, 'Prefer not to say'));
      sel.value = demo.answers[dim.key] || '';
      sel.addEventListener('change', function () {
        if (sel.value) demo.answers[dim.key] = sel.value;
        else delete demo.answers[dim.key];
      });
      body.push(h('div', { class: 'vq-facts' },
        h('div', { class: 'vq-section-label' }, dim.label), sel));
    });

    if (demo.error) body.push(h('div', { class: 'asc-inline-error' }, demo.error));
    body.push(h('div', { class: 'vq-actions' },
      h('button', { class: 'vq-btn vq-btn-approve', disabled: demo.busy,
                    onclick: demoSubmit }, demo.busy ? 'Saving…' : 'Submit'),
      h('button', { class: 'vq-btn vq-btn-ghost', onclick: demoClose }, 'Skip all')));
    el.appendChild(sheet('Help us check our own fairness', body, null));
  }

  function demoSubmit() {
    demo.busy = true; demo.error = null; demoRender();
    // Only what was actually answered. A blank is an absent key, never an empty
    // string — the monitor must be able to tell "skipped" from "answered blank".
    api('/verify/demographics/' + encodeURIComponent(demo.userId),
        { method: 'POST', body: { demographics: demo.answers } })
      .then(function () { demo.busy = false; demo.done = true; demoRender(); })
      .catch(function (e) { demo.busy = false; demo.error = e.message; demoRender(); });
  }

  function demoClose() {
    if (demo.container && demo.container.parentNode === document.body) {
      document.body.removeChild(demo.container);
    }
    if (window.location && window.location.hash) window.location.hash = '';
  }

  function demoMount(container, ctx) {
    if (!container) return;
    ctx = ctx || {};
    if (typeof ctx.token === 'string' && ctx.token) overrideToken = ctx.token;
    demo.container = container;
    demo.userId = ctx.userId || ctx.user_id || '';
    demo.answers = {}; demo.done = false; demo.error = null;
    demoRender();
  }

  // ─── Hash routing, so both are reachable with no other file changing ───────
  function openIfRouted(hash, prefix, mounter, ctx) {
    if ((hash || '').indexOf(prefix) !== 0) return false;
    var overlay = document.createElement('div');
    document.body.appendChild(overlay);
    mounter(overlay, ctx || {});
    return true;
  }

  window.AsclepiusCalibration = {
    mount: calMount,
    openIfRouted: function (hash) {
      return openIfRouted(hash === undefined
        ? (window.location && window.location.hash) : hash,
        '#/calibration', calMount, {});
    },
  };
  window.AsclepiusDemographics = {
    mount: demoMount,
    openIfRouted: function (hash, ctx) {
      return openIfRouted(hash === undefined
        ? (window.location && window.location.hash) : hash,
        '#/demographics', demoMount, ctx || {});
    },
  };

  document.addEventListener('DOMContentLoaded', function () {
    var auto = document.getElementById('ascVerifyRoot');
    if (auto) mount(auto);
    window.AsclepiusCalibration.openIfRouted();
    window.AsclepiusDemographics.openIfRouted();
  });
})();
