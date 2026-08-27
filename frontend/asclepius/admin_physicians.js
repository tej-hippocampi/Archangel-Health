/* ═══════════════════════════════════════════════════════════
   Admin · Physicians section (Admin Launch PRD §2, §3, §6)

   window.AdminPhysiciansSection { render, reset }

   TWO tabs, carrying live counts, and nothing else:

     Approved and Labeling — the roster. Flat: no health-system
       grouping and no Verification column, because every row here
       is approved and a column whose every cell reads the same
       carries no information.
     Pending for Review — name and specialty only, because that is
       what you need to pick who to open next. Everything else is
       one click away in the decision report.

   The operator's job in this section is ONE decision loop, so the
   sub-tab strip that used to spread it over four screens (Roster /
   Signups / Verification / QA) is gone. Signups fold into Pending
   (a physician mid-wizard is a pending physician who cannot be
   decided yet, and they render as such); QA moved under Tasks,
   beside the work it grades.

   Vocabulary rules that carry meaning:
   - Tier renders as a WORD, never a raw token.
   - ADVISOR IS NOT A TIER (capabilities.py: the tier is retired).
     It lives on users.advisor_since and renders as a SECOND badge
     beside the tier, never instead of it. Rendering "Unassigned"
     over a real medical advisor is the quiet-wrong bug this file
     has warned about since its first version.
   - A ±0 reason line is grey, not green: "we could not check" is
     not a credit and is not a deduction.

   Loaded as its own file (§3.3); DOM built exclusively with ctx.h;
   zero innerHTML.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Mirrors asclepius/capabilities.py TIERS. The one list this file filters,
  // counts and labels against.
  const TIERS = ['labeler', 'reviewer'];

  let activeTab = null;         // 'approved' | 'pending' — null until counts decide
  let selectedId = null;        // approved physician → profile view
  let pendingId = null;         // pending physician → decision report
  let cache = null;             // last /admin/physicians payload
  let pendingCache = null;      // last { queue, signups } payload
  let rootEl = null;            // the section body we were mounted into
  let rootCtx = null;

  // ─── Vocabulary (four-state verification; tier words) ─────
  // A missing case here does not throw — it silently renders "Unassigned"
  // over a real physician, which is the same class of bug as the backend's
  // `tier === 'reviewer'` equality: wrong, quiet, and only discovered when
  // the person tells you. Keep this map aligned with
  // asclepius/capabilities.py TIER_WORDS.
  function tierWord(t) {
    if (t === 'labeler') return 'Labeler';
    if (t === 'reviewer') return 'Reviewer';
    return 'Unassigned';
  }
  function tierBadge(h, t) {
    if (t === 'labeler') return h('span', { class: 'asc-badge asc-badge-primary' }, 'Labeler');
    if (t === 'reviewer') return h('span', { class: 'asc-badge asc-badge-green' }, 'Reviewer');
    return h('span', { class: 'asc-badge asc-badge-gray' }, 'Unassigned');
  }
  function slackText(s) {
    if (s === true || s === 1) return 'Joined';
    if (s === false || s === 0) return 'Not joined';
    return '—'; // not checked
  }

  // ─── Section entry ───────────────────────────────────────
  // The shell no longer passes a view: this section owns its own tab strip
  // (§1.2). A legacy third argument is accepted and ignored rather than
  // throwing, so a cached older shell degrades to the default tab instead of
  // rendering nothing.
  function render(body, ctx) {
    rootEl = body; rootCtx = ctx;
    const { h, clear } = ctx;
    clear(body);
    const inner = h('div', {});
    body.appendChild(inner);
    if (pendingId) renderPendingDetail(inner, ctx, pendingId);
    else if (selectedId) renderProfile(inner, ctx, selectedId);
    else renderTabs(inner, ctx);
  }

  function rerender() {
    if (rootEl && rootCtx) render(rootEl, rootCtx);
  }

  /* ─── PRD-CRED · model health + fairness monitor (PRD C §5.2, §6) ──────────
   * Rendered ABOVE the queue, collapsed, because it is not what an admin came
   * here to do — but it has to be somewhere they will see it weekly.
   *
   * Two things live here that nothing else in the product can tell you:
   *
   *  1. The per-batch weight delta. §8: "a model that silently stops updating
   *     looks exactly like a model that has converged." The drift column is the
   *     only cheap way to tell those apart from a screen.
   *  2. The four-fifths fairness comparison. It reads a table that carries no
   *     user ids at all — voluntary self-reported demographics under an HMAC
   *     pseudonym, with the decided tier copied in at decision time. That is
   *     what makes the monitor possible without making the model able to see it.
   *
   * Mounted here rather than as a new sub-tab because the shell's tab list
   * lives in asclepius.js, which is outside Agent C's write allowlist.
   */
  function renderModelHealth(container, ctx) {
    const { h, api } = ctx;
    const box = h('details', { class: 'asc-card' });
    const body = h('div', { class: 'asc-card-pad' });
    box.appendChild(h('summary', { class: 'asc-card-pad' },
      'Model health · learning loop + fairness monitor'));
    box.appendChild(body);
    container.appendChild(box);

    let loaded = false;
    box.addEventListener('toggle', function () {
      if (!box.open || loaded) return;
      loaded = true;
      Promise.all([
        api('/verify/tiering-weights'),
        api('/verify/fairness'),
      ]).then(function (res) {
        renderModelHealthBody(body, ctx, res[0], res[1]);
      }).catch(function (e) {
        clearNode(body);
        // AUDIT UI: `asc-inline-error` (soft, recoverable) not `asc-error` (loud, blocking).
        // The two are visually distinct and the file was using them interchangeably. The
        // split that makes them mean something: inline = "this panel failed, the page is
        // fine, try again"; asc-error = "a module did not load and physicians cannot be
        // approved until someone fixes the deploy". A failed metrics fetch is the first.
        body.appendChild(h('div', { class: 'asc-inline-error' },
          'Could not load model health: ' + e.message));
      });
    });
  }

  // AUDIT UI: a real table, not space-padded strings in <div>s.
  //
  // The previous version emitted `'PINNED  ' + w.feature + '  m=' + …` into `vq-reason`,
  // which is not a mono face — so the padding aligned nothing and the numbers did not sit in
  // a column. `.asc-table` already sets `font-variant-numeric: tabular-nums` and `.asc-mono`
  // exists. This is the screen where you prove to yourself, and to a regulator, that the
  // model is behaving; it should be the easiest screen in the product to read, not the
  // hardest.
  function table(h, headers, rows) {
    const thead = h('thead', {}, h('tr', {}, headers.map(function (t) {
      return h('th', {}, t);
    })));
    const tbody = h('tbody', {}, rows.map(function (r) {
      return h('tr', r.attrs || {}, (r.cells || r).map(function (c) {
        return h('td', c && c.attrs ? c.attrs : {}, c && c.text !== undefined ? c.text : c);
      }));
    }));
    return h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
      thead, tbody));
  }

  function num(v, digits, signed) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    const s = Number(v).toFixed(digits === undefined ? 3 : digits);
    return signed && v >= 0 ? '+' + s : s;
  }

  function renderModelHealthBody(body, ctx, weights, fairness) {
    const { h } = ctx;
    clearNode(body);

    body.appendChild(h('div', { class: 'vq-section-label' },
      'Tier weights — posterior vs the rule it started from'));
    body.appendChild(h('div', { class: 'vq-attempt' },
      (weights.pending_decisions || 0) + ' decision(s) not yet folded in · '
      + 'each batch may move any weight by at most ' + weights.max_delta_per_batch
      + ' · sd should shrink as decisions arrive; if it stops while decisions keep '
      + 'coming, the loop is dead rather than converged'));

    let pinnedMoved = false;
    const weightRows = (weights.weights || []).map(function (w) {
      // A pinned row that has drifted is the single most serious thing this page can show,
      // so it is the only thing on it painted pink. A large drift on an UNPINNED weight is
      // the system working.
      const broken = w.pinned && Math.abs(w.drift) > 0;
      if (broken) pinnedMoved = true;
      return {
        attrs: broken ? { class: 'vq-flag' } : {},
        cells: [
          { text: w.feature, attrs: { class: 'asc-mono' } },
          w.pinned ? 'pinned' : '',
          { text: num(w.m), attrs: { class: 'asc-mono' } },
          { text: num(w.drift, 3, true), attrs: { class: 'asc-mono' } },
          { text: num(w.sd), attrs: { class: 'asc-mono' } },
        ],
      };
    });
    body.appendChild(table(h, ['Feature', '', 'm', 'Drift', 'sd'], weightRows));
    if (pinnedMoved) {
      body.appendChild(h('div', { class: 'vq-flag' },
        'A PINNED weight has moved. It must be exactly 0.000 forever. Stop applying '
        + 'batches and investigate before any further tier decision is recorded.'));
    }

    body.appendChild(h('div', { class: 'vq-section-label' },
      'Reviewer selection rate by voluntary self-report — four-fifths rule'));
    const dims = fairness.dimensions || {};
    if (!Object.keys(dims).length) {
      body.appendChild(h('div', { class: 'vq-attempt' },
        'No self-reported demographics on file yet. The monitor needs volunteers, not '
        + 'inference — nothing here is derived from a name, a school, or a region.'));
    }
    Object.keys(dims).sort().forEach(function (dim) {
      body.appendChild(h('div', { class: 'vq-reason' }, humanize(dim)));
      const groups = dims[dim];
      const rows = Object.keys(groups).sort().map(function (g) {
        const r = groups[g];
        const flagged = r.counted && r.impact_ratio !== null && r.impact_ratio < 0.8;
        return {
          attrs: flagged ? { class: 'vq-flag' } : {},
          cells: [
            humanize(g),
            { text: String(r.n), attrs: { class: 'asc-mono' } },
            { text: (r.rate * 100).toFixed(0) + '%', attrs: { class: 'asc-mono' } },
            { text: r.impact_ratio === null ? '—' : num(r.impact_ratio, 2),
              attrs: { class: 'asc-mono' } },
            r.counted ? '' : 'below n=' + fairness.min_group_n + ', not alerted on',
          ],
        };
      });
      body.appendChild(table(h, ['Group', 'n', 'Reviewer rate', 'Impact ratio', ''], rows));
    });
    (fairness.alerts || []).forEach(function (a) {
      body.appendChild(h('div', { class: 'vq-flag' }, a.message));
    });

    // AUDIT H2 — the per-feature breakdown. An outcome monitor tells you a gap exists; this
    // tells you which feature opened it. structured_review_exp is the one to watch: it
    // correlates with IMG status and national origin, both of which are pinned to zero, so
    // it is the available route around the pin.
    const byFeature = fairness.by_feature || {};
    if (Object.keys(byFeature).length) {
      body.appendChild(h('div', { class: 'vq-section-label' },
        'Feature means by group — which feature carries the gap'));
      Object.keys(byFeature).sort().forEach(function (dim) {
        const groups = byFeature[dim];
        const names = {};
        Object.keys(groups).forEach(function (g) {
          Object.keys(groups[g]).forEach(function (f) { names[f] = true; });
        });
        const features = Object.keys(names).sort();
        const groupNames = Object.keys(groups).sort();
        body.appendChild(h('div', { class: 'vq-reason' }, humanize(dim)));
        body.appendChild(table(h, ['Feature'].concat(groupNames.map(humanize)),
          features.map(function (f) {
            return {
              cells: [{ text: f, attrs: { class: 'asc-mono' } }].concat(
                groupNames.map(function (g) {
                  const cell = groups[g][f];
                  return { text: cell ? num(cell.mean, 2) : '—',
                           attrs: { class: 'asc-mono' } };
                })),
            };
          })));
      });
      (fairness.feature_alerts || []).forEach(function (a) {
        body.appendChild(h('div', { class: 'vq-flag' }, a.message));
      });
    }
  }

  // Dimension and group keys arrive as stored tokens (`race_ethnicity`, `prefer_not_to_say`).
  // Rendering them raw is the same class of defect as rendering a raw tier token.
  function humanize(key) {
    return String(key || '').replace(/_/g, ' ').replace(/^./, function (c) {
      return c.toUpperCase();
    });
  }

  function clearNode(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  /* ─── The two tabs (§2) ───────────────────────────────────────────────────
   *
   * Both counts are loaded before either tab renders, because the DEFAULT
   * depends on them: Pending opens first whenever anything is waiting, since
   * deciding a physician is the only thing in this section that is urgent.
   * Rendering the roster first and switching once the pending count arrived
   * would move the tab under the operator's cursor.
   */
  async function renderTabs(container, ctx) {
    const { h, api, clear, loadingCard } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading physicians…'));

    let approvedErr = null;
    let pendingErr = null;
    // Both in flight together. Each failure is contained to its own tab: an
    // operator must never lose the roster because the verification queue is
    // down, or lose the queue because the roster is.
    const [approvedRes, pendingRes] = await Promise.all([
      api('/admin/physicians').catch((e) => { approvedErr = errText(e); return null; }),
      loadPending(api).catch((e) => { pendingErr = errText(e); return null; }),
    ]);
    if (approvedRes) cache = approvedRes;
    if (pendingRes) pendingCache = pendingRes;

    const approved = (((cache || {}).physicians) || [])
      .filter((p) => p.verification_status === 'approved');
    const pendingRows = pendingList();

    // Default to Pending when its count > 0 — and only on the FIRST render, so
    // a deliberate switch to the roster is not undone by the next reload.
    //
    // A queue that FAILED to load also opens Pending, and that is the whole
    // point of the branch: "we could not read the queue" is not "the queue is
    // empty". Defaulting to the roster on an error would land the operator on a
    // healthy-looking screen while physicians waited unseen — which is exactly
    // the failure that once hid the entire verification queue for a build round.
    if (activeTab === null) {
      activeTab = (pendingRows.length || pendingErr) ? 'pending' : 'approved';
    }

    clear(container);
    container.appendChild(tabStrip(ctx, approved.length, pendingRows.length,
                                   approvedErr, pendingErr));

    if (activeTab === 'pending') {
      if (pendingErr) {
        container.appendChild(errorCard(h, 'The pending queue could not be loaded: ' + pendingErr));
        return;
      }
      renderPendingTab(container, ctx, pendingRows);
      // The learning loop's only window. Below the queue, collapsed: it is not
      // what an admin came here to do, but it has to be somewhere they see it.
      renderModelHealth(container, ctx);
      return;
    }
    if (approvedErr) {
      container.appendChild(errorCard(h, 'The roster could not be loaded: ' + approvedErr));
      return;
    }
    renderApprovedTab(container, ctx, approved);
  }

  function errText(e) {
    return (e && (e.detail || e.message)) || 'no response';
  }

  // AUDIT UI: `asc-inline-error` (soft, recoverable) — this panel failed, the
  // page is fine, try again. Distinct from `asc-error`, which is reserved for
  // the one blocking case: a module that did not load at all.
  function errorCard(h, message) {
    return h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-inline-error' }, message)));
  }

  function tabStrip(ctx, nApproved, nPending, approvedErr, pendingErr) {
    const { h } = ctx;
    return h('div', { class: 'asc-phys-chips' }, [
      ['approved', 'Approved and Labeling', nApproved, false, approvedErr],
      ['pending', 'Pending for Review', nPending, true, pendingErr],
    ].map(([id, label, n, urgent, failed]) => {
      const el = h('button', {
        type: 'button',
        class: 'asc-phys-chip' + (urgent && n ? ' asc-chip-pending' : '')
          + (activeTab === id ? ' active' : ''),
        // A count we could not read is '—', never '0'. A zero here would be a
        // confident claim that nobody is waiting, made on no information.
      }, label, h('span', { class: 'asc-chip-count' }, failed ? '—' : String(n)));
      el.addEventListener('click', () => {
        activeTab = id; selectedId = null; pendingId = null; rerender();
      });
      return el;
    }));
  }

  /* Tab B's source is a UNION of two populations that look identical to an
   * operator and are completely different to the system: physicians who
   * finished the wizard and hold an account (decidable) and physicians still
   * walking it (not decidable — there is no account to approve). The signups
   * half is best-effort: it is the secondary population, and a failure to load
   * it must not hide the physicians who ARE waiting for a decision. */
  async function loadPending(api) {
    const queue = await api('/verify/queue?status=pending');
    let signups = null;
    try {
      signups = await api('/admin/signups');
    } catch (e) {
      signups = null;
    }
    return { queue: queue, signups: signups };
  }

  function pendingList() {
    const pc = pendingCache || {};
    const queue = ((pc.queue || {}).queue) || [];
    const signups = ((pc.signups || {}).signups) || [];
    const rows = queue.map((q) => ({
      kind: 'queued',
      user_id: q.user_id,
      name: q.full_name || q.email || q.user_id,
      specialty: q.specialty || null,
    }));
    // Mid-wizard physicians, after the decidable ones: the queue is the job,
    // the funnel is the chase list.
    signups.forEach((s) => {
      rows.push({
        kind: 'signup',
        user_id: null,
        name: s.name || s.email || '—',
        specialty: s.specialty || null,
        signup: s,
      });
    });
    return rows;
  }

  /* ─── Tab A — Approved and Labeling (§2.1) ────────────────────────────────
   * Flat. The health-system grouping is gone (it split one short roster into
   * accordions) and so is the Verification column (every row here is approved,
   * so the column said "Approved" on every line). */
  function renderApprovedTab(container, ctx, rows) {
    const { h } = ctx;
    if (!rows.length) {
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' },
          'No approved physicians yet. Decide the pending queue and they appear here.'))));
      return;
    }
    container.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Name'), h('th', {}, 'Email'), h('th', {}, 'Phone'),
          h('th', {}, 'Specialty'), h('th', {}, 'Tier'), h('th', {}, 'Community'),
          h('th', {}, ''))),
        h('tbody', {}, rows.map((p) => approvedRow(ctx, p)))))));
  }

  /* Advisor renders BESIDE the tier, never instead of it (§2.2).
   * `tierBadge` alone renders "Unassigned" over a real medical advisor,
   * because advisor is not a tier and never appears in users.tier. */
  function tierCell(h, row) {
    const badges = [tierBadge(h, row.tier)];
    if (row.is_advisor) {
      badges.push(h('span', { class: 'asc-badge asc-badge-lime' }, 'Advisor'));
    }
    return h('div', { style: 'display:flex;gap:6px;align-items:center' }, badges);
  }

  function approvedRow(ctx, p) {
    const { h } = ctx;
    const communityCell = h('td', {});
    const actionCell = h('td', {});
    paintCommunity(ctx, p, communityCell, actionCell);

    const tr = h('tr', { class: 'asc-row-click' },
      h('td', {}, h('strong', {}, p.name || '—')),
      h('td', {}, p.email || '—'),
      h('td', {}, p.phone || '—'),
      h('td', {}, p.specialty || '—'),
      h('td', {}, tierCell(h, p)),
      communityCell,
      actionCell);
    tr.addEventListener('click', (ev) => {
      // The invite button lives inside the row; a click on it must not also
      // open the profile underneath it.
      if (ev.target && typeof ev.target.closest === 'function' && ev.target.closest('button')) return;
      selectedId = p.id; pendingId = null; rerender();
    });
    return tr;
  }

  /* The Send Invite flow (§2.1, §5.1). On success the ROW updates in place —
   * no full re-render, because re-rendering the table under an operator who is
   * working down it moves every other row they were about to click. */
  function paintCommunity(ctx, p, cell, actionCell) {
    const { h, api } = ctx;
    clearNode(cell);
    clearNode(actionCell);
    if (p.slack_joined === true || p.slack_joined === 1) {
      cell.appendChild(h('span', {}, 'Joined'));
      return;
    }
    if (p._invited_at) {
      cell.appendChild(h('span', {}, 'Invited · ' + shortTime(p._invited_at)));
      return;
    }
    cell.appendChild(h('span', {}, p.slack_joined === false || p.slack_joined === 0
      ? 'Not joined' : '—'));

    const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      'Send Invite');
    btn.addEventListener('click', () => {
      btn.setAttribute('disabled', '');
      btn.textContent = 'Sending…';
      api('/admin/community/invite', { method: 'POST', body: { user_id: p.id } })
        .then((res) => {
          if (res && res.already_joined) {
            p.slack_joined = true;
          } else {
            p._invited_at = (res && res.invited_at) || new Date().toISOString();
          }
          paintCommunity(ctx, p, cell, actionCell);
        })
        .catch((e) => {
          btn.removeAttribute('disabled');
          btn.textContent = 'Send Invite';
          // Inline, on the row, never a toast: a toast about one row in a table
          // of thirty does not say WHICH row failed.
          clearNode(actionCell);
          actionCell.appendChild(btn);
          actionCell.appendChild(h('div', { class: 'asc-inline-error' }, errText(e)));
        });
    });
    actionCell.appendChild(btn);
  }

  function shortTime(iso) {
    const s = String(iso || '');
    const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(s);
    return m ? m[1] + ' ' + m[2] : (s.slice(0, 16) || '—');
  }

  /* ─── Tab B — Pending for Review (§2.3) ───────────────────────────────────
   * Two fields render: name and specialty. Everything else about a candidate
   * is one click away, and putting it here made the queue a table nobody could
   * scan. */
  function renderPendingTab(container, ctx, rows) {
    const { h } = ctx;
    if (!rows.length) {
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' },
          'Queue is clear — nobody is waiting for a decision, and nobody is '
          + 'mid-onboarding.'))));
      return;
    }
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Name'), h('th', {}, 'Specialty'), h('th', {}, ''))),
        h('tbody', {}, rows.map((r) => pendingRow(ctx, r))))));
    container.appendChild(card);
  }

  function pendingRow(ctx, r) {
    const { h } = ctx;
    if (r.kind === 'signup') {
      // No account exists yet, so there is nothing to approve and this row is
      // NOT clickable into a decision. It keeps the resend action, which is the
      // only thing that can actually move this physician forward — and this is
      // the only screen a half-finished physician appears on at all.
      return h('tr', {},
        h('td', {}, h('strong', {}, r.name),
          h('span', { class: 'asc-phys-chip asc-chip-stat', style: 'margin-left:8px',
                       // Two fields render (§2.3), so where they stopped rides on
                       // the chip rather than taking a third column — it is what
                       // tells an operator whether Resend is worth pressing.
                       title: r.signup.stage_word || 'Onboarding not finished' },
            'Signup incomplete'),
          h('span', { class: 'asc-dim', style: 'margin-left:6px' },
            'step ' + r.signup.stage_index + '/' + r.signup.stage_total)),
        h('td', {}, r.specialty || '—'),
        h('td', {}, resendBtn(ctx, r.signup)));
    }
    const tr = h('tr', { class: 'asc-row-click' },
      h('td', {}, h('strong', {}, r.name)),
      h('td', {}, r.specialty || '—'),
      h('td', {}, '→'));
    tr.addEventListener('click', () => { pendingId = r.user_id; rerender(); });
    return tr;
  }

  function resendBtn(ctx, s) {
    const { h, api, toast } = ctx;
    const canResend = ((pendingCache || {}).signups || {}).can_resend !== false;
    const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      'Resend link');
    if (!canResend) {
      btn.setAttribute('disabled', '');
      btn.setAttribute('title', 'Email is not configured on this server (SendGrid or SMTP).');
    }
    btn.addEventListener('click', () => {
      btn.setAttribute('disabled', '');
      btn.textContent = 'Sending…';
      api('/admin/signups/resend', {
        method: 'POST',
        body: { health_system_id: s.health_system_id, email: s.email },
      }).then((res) => {
        btn.textContent = 'Link resent';
        if (toast) toast((res && res.message) || ('Link resent to ' + s.email));
      }).catch((e) => {
        btn.removeAttribute('disabled');
        btn.textContent = 'Resend link';
        if (toast) toast(errText(e), 'error');
      });
    });
    return btn;
  }

  /* ═══ §3 — The pending decision report ════════════════════════════════════
   *
   * ONE API call. GET /verify/queue/{user_id} already embeds the tiering
   * proposal, the NPPES payload, the duplicate claimants and the tier
   * vocabulary; a second round trip for the recommendation would be a second
   * chance for the screen to disagree with itself.
   *
   * `case_domain` is deliberately not passed. It defaults to the physician's
   * own declared specialty, which is the honest question at signup: could this
   * person adjudicate in their own domain?
   */
  async function renderPendingDetail(container, ctx, userId) {
    const { h, api, clear, loadingCard, fmtDate } = ctx;
    clear(container);
    container.appendChild(backBar(ctx, 'pending'));
    const slot = h('div', {});
    container.appendChild(slot);
    slot.appendChild(loadingCard('Loading the report…'));

    let d;
    try {
      d = await api('/verify/queue/' + encodeURIComponent(userId));
    } catch (e) {
      clearNode(slot);
      slot.appendChild(errorCard(h, 'This physician could not be loaded: ' + errText(e)));
      return;
    }
    clearNode(slot);

    const words = d.tier_words || {};
    const proposed = d.proposed_tier || null;

    slot.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, d.full_name || d.email || userId),
        h('div', { class: 'asc-card-sub' }, d.specialty || 'No specialty declared')))));

    // ── WHAT THEY SUBMITTED ──
    slot.appendChild(sectionCard(ctx, 'What they submitted',
      kvBlock(h, '', [
        ['Email', d.email],
        ['Phone', d.phone],
        ['NPI', npiLineText(d)],
        ['Specialty', d.specialty],
        ['Clinical role', (d.clinical_role || '').replace(/_/g, ' ') || null],
        ['Organisation', d.org_name],
        ['LinkedIn', d.linkedin_url],
        ['Submitted', d.created_at ? fmtDate(d.created_at) : null],
      ]),
      d.has_cv ? cvLink(ctx, userId, d) : h('div', { class: 'asc-dim' }, 'No CV uploaded')));

    // ── RECOMMENDATION ──
    slot.appendChild(recommendationCard(ctx, d, proposed, words));

    // ── YOUR DECISION ──
    slot.appendChild(decisionCard(ctx, userId, d, proposed, words));

    // ── Background research, BELOW the buttons and never in the reasons ──
    const research = d.agent_research || [];
    if (research.length) slot.appendChild(researchCard(ctx, research));
  }

  function backBar(ctx, tab) {
    const { h } = ctx;
    const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      tab === 'pending' ? '← Back to pending' : '← All physicians');
    btn.addEventListener('click', () => { pendingId = null; selectedId = null; rerender(); });
    return h('div', { style: 'margin-bottom:12px' }, btn);
  }

  function sectionCard(ctx, title) {
    const { h } = ctx;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', { class: 'asc-card-title' }, title)));
    const pad = h('div', { class: 'asc-card-pad' });
    for (let i = 2; i < arguments.length; i++) {
      if (arguments[i]) pad.appendChild(arguments[i]);
    }
    card.appendChild(pad);
    return card;
  }

  function npiLineText(d) {
    const npi = d.npi || {};
    if (!npi.npi) return null;
    // The verification STATE travels with the number. An NPI with no state
    // beside it reads as verified to every operator who has ever seen one.
    const state = npi.result ? String(npi.result).replace(/_/g, ' ') : 'not checked';
    return npi.npi + ' · ' + state;
  }

  function cvLink(ctx, userId, d) {
    const { h } = ctx;
    const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      d.cv_ok ? 'View CV' : 'View CV (could not parse — read the file)');
    btn.addEventListener('click', () => { openCv(ctx, userId); });
    return h('div', {}, btn);
  }

  function openCv(ctx, userId) {
    // The CV is bytes behind an admin bearer token, so it cannot be a plain
    // href. ctx.downloadBlob already carries the token and the filename.
    if (typeof ctx.downloadBlob === 'function') {
      ctx.downloadBlob('/verify/queue/' + encodeURIComponent(userId) + '/cv',
                       'cv-' + userId);
    }
  }

  /* Reasons render VERBATIM and in order — they are already written for a
   * human, and re-sorting or re-wording them is how an admin ends up reading a
   * different explanation from the one the model recorded.
   *
   * §6: a `±0` line is grey. It means "we could not check" (NPPES unavailable)
   * or "checked and found nothing", neither of which is a credit. Painting it
   * like a `+n` line is how "we could not check" turns into "this person is
   * verified" in an operator's head — and collapsing UNAVAILABLE into NOT_FOUND
   * has already shipped and been caught once in this codebase. */
  function reasonLine(h, text) {
    const s = String(text || '');
    const neutral = s.indexOf('±0') === 0 || s.indexOf('±0') === 0;
    return h('div', { class: neutral ? 'vq-attempt' : 'vq-reason' }, s);
  }

  function recommendationCard(ctx, d, proposed, words) {
    const { h } = ctx;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', {}, h('div', { class: 'asc-card-title' }, 'Recommendation')),
        h('div', { class: 'asc-mono' },
          d.score == null ? 'score —' : 'score ' + d.score)));
    const pad = h('div', { class: 'asc-card-pad' });
    pad.appendChild(h('div', { class: 'vq-section-label' },
      proposed ? (words[proposed] || proposed)
        : 'No tier proposed — the decision is entirely yours'));
    (d.reasons || []).forEach((r) => { pad.appendChild(reasonLine(h, r)); });

    const blockers = d.blockers || [];
    pad.appendChild(h('div', { class: 'vq-section-label' },
      'Blockers' + (blockers.length ? '' : '   (none)')));
    // Amber, and BOTH approve buttons stay live below. A blocker is a review
    // flag, not a rejection (credentialing.py) — suppressing the buttons would
    // turn a flag into a veto that nobody chose to give it.
    blockers.forEach((b) => {
      pad.appendChild(h('div', { class: 'asc-badge asc-badge-amber', style: 'display:block;margin:4px 0' },
        String(b)));
    });
    gateNote(h, pad, d, proposed, words);
    card.appendChild(pad);
    return card;
  }

  /* ─── What the GATE LAYER says, when it does not say the same thing ───────
   *
   * Two different things compute a recommendation for this physician:
   *
   *   `score` / `proposed_tier` / `reasons` — credentialing.propose_tier(), the
   *     credential heuristic. That is what §3.2 lays out above, and it is what
   *     an operator reads.
   *   `tiering` — the Bayesian model with hard_gates() in front of it. Those
   *     gates are POLICY, never learned, and it is the model this screen's
   *     decision trains.
   *
   * They can disagree, and on a real signup they routinely do: the heuristic
   * scores credentials while the gates ask whether a licence number is on file
   * and whether the OIG exclusion list was reachable. A screen that showed only
   * the heuristic would offer "Approve as Reviewer" as the primary action over
   * a physician the gate layer is still holding — promoting someone quietly,
   * which is the exact failure this codebase keeps writing tests against.
   *
   * So it is SHOWN, not enforced. Blockers are review flags, and so is this: the
   * buttons stay live and the admin decides. What changes is that they decide
   * knowing both answers.
   */
  function gateNote(h, pad, d, proposed, words) {
    const t = d.tiering || {};
    if (t.error) {
      pad.appendChild(h('div', { class: 'asc-inline-error' }, String(t.error)));
      return;
    }
    const missing = t.tr_missing || [];
    if (t.tr_eligible === false && missing.length) {
      pad.appendChild(h('div', { class: 'vq-attempt' },
        'Not yet reviewer-eligible: ' + missing.join(', ')
        + '. This is the gate layer, which is policy and is never learned — it is '
        + 'not a veto, and both approve buttons stay live.'));
    }
    // The tier the LEARNING model would have proposed, when it differs from the
    // one above. This is what /decide compares the decision against, so an admin
    // agreeing with the screen should be able to see when the model disagrees.
    const modelTier = t.proposed_tier || null;
    if (modelTier !== proposed) {
      pad.appendChild(h('div', { class: 'vq-attempt' },
        'The learning model proposes '
        + (modelTier ? (words[modelTier] || modelTier) : 'no tier yet')
        + (t.score == null ? '' : ' (score ' + Number(t.score).toFixed(2) + ')')
        + ' — a different question from the credential score above, and the one '
        + 'your decision is recorded against.'));
    }
  }

  /* ═══ §3.3 — THE DECISION IS THE TRAINING SIGNAL ══════════════════════════
   *
   * POST /verify/tiering/{id}/decide fires on EVERY decision, including
   * agreement with the recommendation, and it fires BEFORE /approve.
   *
   * Both halves of that matter:
   *
   *   Agreement too — apply_decision_batch() learns from agreement and
   *   disagreement equally. A UI that posted only on override would hand the
   *   model a training set made entirely of its own mistakes, and it would
   *   drift badly while every screen kept looking healthy.
   *
   *   Decide FIRST — decide records the observation without re-approving, so it
   *   is safe to run first. The other order is not: if approve succeeded and
   *   decide then failed, the physician would be live with no training signal
   *   and nothing in the system would ever reconcile it.
   */
  function decisionCard(ctx, userId, d, proposed, words) {
    const { h, api } = ctx;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', { class: 'asc-card-title' }, 'Your decision')));
    const pad = h('div', { class: 'asc-card-pad' });

    const note = h('textarea', {
      class: 'vq-note', rows: '2',
      placeholder: 'Note — required to reject, recommended always',
    });
    const status = h('div', {});
    const actions = h('div', { class: 'vq-actions' });

    const buttons = [];
    function setBusy(on) {
      buttons.forEach((b) => {
        if (on) b.setAttribute('disabled', '');
        else b.removeAttribute('disabled');
      });
    }

    TIERS.forEach((tier) => {
      // The recommended tier is primary; the other stays ghost. Both are always
      // present and always enabled — see the blocker note above.
      const isProposed = tier === proposed;
      const btn = h('button', {
        type: 'button',
        class: 'asc-btn asc-btn-sm ' + (isProposed ? 'asc-btn-primary' : 'asc-btn-ghost'),
      }, 'Approve as ' + (words[tier] || tier));
      btn.addEventListener('click', () => {
        setBusy(true);
        clearNode(status);
        status.appendChild(h('div', { class: 'asc-dim' }, 'Recording…'));
        approveWithSignal(ctx, userId, tier, note.value || null)
          .then((line) => {
            clearNode(status);
            status.appendChild(h('div', { class: 'vq-reason' }, line));
            // The physician has left the pending queue, so the cached list is
            // stale. Drop it and go back — the queue reloads on the way.
            pendingCache = null;
            setTimeout(() => { pendingId = null; rerender(); }, 900);
          })
          .catch((e) => {
            setBusy(false);
            clearNode(status);
            status.appendChild(h('div', { class: 'asc-inline-error' }, errText(e)));
          });
      });
      buttons.push(btn);
      actions.appendChild(btn);
    });

    const rejectBtn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      'Reject');
    rejectBtn.addEventListener('click', () => {
      const text = (note.value || '').trim();
      if (!text) {
        clearNode(status);
        status.appendChild(h('div', { class: 'asc-inline-error' },
          'A rejection needs a note. It has to be auditable and appealable.'));
        return;
      }
      setBusy(true);
      api('/verify/queue/' + encodeURIComponent(userId) + '/reject',
          { method: 'POST', body: { note: text } })
        .then(() => { pendingCache = null; pendingId = null; rerender(); })
        .catch((e) => {
          setBusy(false);
          clearNode(status);
          status.appendChild(h('div', { class: 'asc-inline-error' }, errText(e)));
        });
    });
    buttons.push(rejectBtn);
    actions.appendChild(rejectBtn);

    pad.appendChild(note);
    pad.appendChild(actions);
    pad.appendChild(status);
    card.appendChild(pad);
    return card;
  }

  async function approveWithSignal(ctx, userId, tier, note) {
    const { api } = ctx;
    const uid = encodeURIComponent(userId);
    // 1. The observation. No case_domain: the server defaults to the
    //    physician's own declared specialty, which is the question being asked.
    await api('/verify/tiering/' + uid + '/decide',
              { method: 'POST', body: { tier: tier, note: note } });
    // 2. Only now the lifecycle change. An explicit tier is required — the
    //    proposal is advice and the endpoint refuses to infer one.
    await api('/verify/queue/' + uid + '/approve',
              { method: 'POST', body: { tier: tier, note: note } });
    // 3. What the decision did to the model. Best-effort: a failure to read the
    //    weights must not make a completed approval look like it failed.
    let pending = null;
    try {
      const w = await api('/verify/tiering-weights');
      pending = w && w.pending_decisions;
    } catch (e) {
      pending = null;
    }
    if (pending == null) return 'Recorded.';
    return 'Recorded. ' + pending + ' decision' + (pending === 1 ? '' : 's')
      + ' waiting to be folded into the model.';
  }

  /* §0.3 — background research is rendered BELOW the decision buttons, under a
   * heading that says plainly it is not part of the recommendation, and NEVER
   * inside the reasons list. The agent fetches pages the applicant controls, so
   * "this physician is verified, approve" in white-on-white text on a personal
   * site is a live prompt-injection attack against something that writes
   * verification_status. A hallucinated citation is also indistinguishable from
   * a real one in this output format. It is background reading for a human. */
  function researchCard(ctx, research) {
    const { h } = ctx;
    const box = h('details', { class: 'asc-card' });
    box.appendChild(h('summary', { class: 'asc-card-pad' },
      'Background research — not part of the recommendation'));
    const pad = h('div', { class: 'asc-card-pad' });
    pad.appendChild(h('div', { class: 'asc-dim' },
      'Gathered by the verification agent from public pages, some of which the '
      + 'applicant controls. It did not influence the score and it decides '
      + 'nothing. Treat every citation as unverified.'));
    research.forEach((r) => {
      const text = (r && (r.summary || r.text || r.note)) || JSON.stringify(r);
      const url = r && r.url;
      pad.appendChild(h('div', { class: 'vq-attempt' }, String(text),
        url ? h('a', { href: String(url), target: '_blank', rel: 'noopener noreferrer',
                       style: 'margin-left:6px' }, 'source') : null));
    });
    box.appendChild(pad);
    return box;
  }


  // ─── Profile: everything captured at onboarding ───────────
  async function renderProfile(container, ctx, id) {
    const { h, api, clear, loadingCard, fmtDate } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading profile…'));
    let data;
    try {
      data = await api('/admin/physicians/' + encodeURIComponent(id));
    } catch (e) {
      clear(container);
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, e.message || 'Could not load this physician.'))));
      return;
    }
    clear(container);
    const p = data.physician || {};

    const backBtn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' }, '← All physicians');
    backBtn.addEventListener('click', () => { selectedId = null; rerender(); });

    // Role grant (one button, two roles): how the second founder becomes an
    // admin without touching env vars. Confirm() because this is the one
    // console action that changes who can operate the console.
    const isAdmin = (p.role || data.role) === 'admin';
    const roleBtn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' },
      isAdmin ? 'Revoke admin' : 'Make admin');
    roleBtn.addEventListener('click', async () => {
      const next = isAdmin ? 'evaluator' : 'admin';
      if (!window.confirm(
        next === 'admin'
          ? 'Grant this account the admin role? They will see and operate the whole console.'
          : 'Revoke this account\u2019s admin role?')) return;
      try {
        await api('/admin/users/' + encodeURIComponent(id) + '/role',
                  { method: 'POST', body: { role: next } });
        rerender();
      } catch (e) {
        window.alert((e && (e.detail || e.message)) || 'That did not save.');
      }
    });

    container.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', {},
          h('div', { class: 'asc-card-title' }, p.name || p.email || id),
          h('div', { class: 'asc-card-sub' },
            tierWord(p.tier), ' · ', p.specialty || 'No specialty', ' · ',
            p.health_system_name || 'Independent')),
        h('div', { style: 'display:flex;gap:8px' }, roleBtn, backBtn)),
      h('div', { class: 'asc-card-pad asc-phys-profile-grid' },
        kvBlock(h, 'Identity', identityRows(p)),
        kvBlock(h, 'Credentials', [
          ['Board certification', p.board_cert],
          ['Years of experience', p.years_experience != null ? String(p.years_experience) : null],
          ['Email domain', p.email_domain_class],
          // The Slack ROLE beside the joined flag, so whoever sends the invite
          // sets the label correctly (§6.1). Not a Slack integration.
          ['Slack', slackText(p.slack_joined) + (p.slack_role ? ' · ' + p.slack_role : '')],
        ]),
        kvBlock(h, 'Verification', [
          ['Status', verificationWord(p.verification_status)],
          ['Score', p.tier_score != null ? String(p.tier_score) : null],
          ['Assigned', p.tier_assigned_at ? fmtDate(p.tier_assigned_at) : null],
          ['Notes', p.verification_notes],
        ]))));

    // What the doctor actually typed. All of this was captured at signup and
    // rendered nowhere, so "check their credentials" could not be done from
    // the credentials page: the licence number, the training, the practice
    // status and the initials they signed with were invisible on every admin
    // surface.
    if (data.flags && data.flags.length) container.appendChild(flagsCard(h, data.flags));
    const asTyped = credentialsAsTyped(h, data.credentials || {}, data.attestations || {});
    if (asTyped) container.appendChild(asTyped);

    // Contributor score (PRD-SCORE): the blended rating, its component
    // breakdown and the per-case trajectory. Best-effort: an absent score is
    // an absent card, never an error over a profile that loaded fine.
    try {
      const sc = await api('/admin/scores/' + encodeURIComponent(id));
      const rows = [
        ['Current score', sc.score != null ? String(sc.score) + ' · ' + (sc.band || '') : null],
        ['Initial rating', sc.prior != null ? String(sc.prior) + ' (' + (sc.prior_source || '') + ')' : null],
        ['Graded cases', String(sc.n_cases || 0)],
      ];
      const card = h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', {},
          h('div', { class: 'asc-card-title' }, 'Contributor score'),
          h('div', { class: 'asc-card-sub' },
            'Starts from the credential rating; every QA-graded case folds in.'))),
        h('div', { class: 'asc-card-pad asc-phys-profile-grid' },
          kvBlock(h, 'Rating', rows),
          kvBlock(h, 'Latest case',
            (sc.cases && sc.cases.length)
              ? Object.entries(sc.cases[sc.cases.length - 1].components || {}).map(
                  ([k, v]) => [k.replace(/_/g, ' '), v == null ? null : String(v)])
              : [['No graded cases yet', ' ']])));
      if (sc.history && sc.history.length) {
        card.appendChild(h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-card-sub' }, 'Trajectory (newest first)'),
          h('div', {}, sc.history.slice(0, 10).map((row) =>
            h('div', { class: 'asc-score-hist-row' },
              h('span', { class: 'asc-mono' }, String(row.score)),
              h('span', {}, row.case_score != null ? 'case ' + row.case_score : 'initial'),
              h('span', { class: 'asc-mono' }, row.created_at ? fmtDate(row.created_at) : ''))))));
      }
      container.appendChild(card);
    } catch (e) { /* score endpoint unavailable: the profile stands alone */ }

    // NPPES payload (raw registry answer) — shown when the NPI was checked.
    if (data.npi_payload) {
      const pre = h('pre', { class: 'asc-mono', style: 'font-size:12px;overflow:auto;max-height:280px' });
      pre.textContent = JSON.stringify(data.npi_payload, null, 2);
      container.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', {},
          h('div', { class: 'asc-card-title' }, 'NPPES registry payload'))),
        h('div', { class: 'asc-card-pad' }, pre)));
    }

    // Task history + review history (when a reviewer).
    container.appendChild(historyCard(ctx, 'Task history',
      ['When', 'Task', 'Status'],
      (data.task_history || []).map((t) => [
        t.created_at ? fmtDate(t.created_at) : '—', t.task_id || '—', t.status || '—'])));
    // Review history renders for the tier that holds the REVIEW capability.
    if (p.tier === 'reviewer') {
      container.appendChild(historyCard(ctx, 'Review history',
        ['When', 'Submission', 'Verdict'],
        (data.review_history || []).map((r) => [
          r.created_at ? fmtDate(r.created_at) : '—', r.submission_id || '—', r.verdict || '—'])));
    }
  }

  function npiWord(v) {
    if (v === 1 || v === true) return 'Verified';
    if (v === 0 || v === false) return 'Failed';
    return 'Not checked';
  }
  function verificationWord(v) {
    if (v === 'approved') return 'Approved';
    if (v === 'pending') return 'Pending';
    if (v === 'rejected') return 'Rejected';
    return 'Not checked';
  }

  function kvBlock(h, title, pairs) {
    const dl = h('dl', { class: 'asc-phys-kv' });
    pairs.forEach(([k, v]) => {
      dl.appendChild(h('dt', {}, k));
      if (v && /^https?:\/\//.test(String(v))) {
        dl.appendChild(h('dd', {}, h('a', { href: v, target: '_blank', rel: 'noopener noreferrer' }, v)));
      } else {
        dl.appendChild(h('dd', {}, v == null || v === '' ? '—' : String(v)));
      }
    });
    return h('div', {}, h('div', { class: 'asc-card-title', style: 'font-size:14px' }, title), dl);
  }

  /* Identity reads differently depending on which registry answers for this
     doctor. Showing "NPI: —" for a physician registered with SCFHS is a blank
     where the actual credential should be. */
  function identityRows(p) {
    const rows = [['Email', p.email], ['Phone', p.phone]];
    const licensure = (p.country_of_licensure || '').toUpperCase();
    if (!licensure || licensure === 'US') {
      rows.push(['NPI', p.npi], ['NPI check', npiWord(p.npi_verified)]);
    } else {
      rows.push([p.registry_name || 'Registration', p.registry_id]);
      rows.push(['Registry check', npiWord(p.registry_verified)]);
      if (p.registry_lookup_url) rows.push(['Check by hand', p.registry_lookup_url]);
      rows.push(['Licensed in', licensure]);
      if (p.country_of_practice && p.country_of_practice !== licensure) {
        rows.push(['Practising in', p.country_of_practice]);
      }
    }
    rows.push(['LinkedIn', p.linkedin_url], ['CV on file', p.cv_on_file ? 'Yes' : 'No']);
    return rows;
  }

  /* Severity to class, spelled out rather than concatenated. Building a class
     name out of a data field puts whatever that field contains into the DOM
     and leaves the stylesheet's own classes unsearchable, which is what
     test_no_new_component_vocabulary_beyond_the_prd exists to catch. */
  const FLAG_SEVERITY_CLASS = {
    high: 'asc-phys-flag-high',
    medium: 'asc-phys-flag-medium',
    low: 'asc-phys-flag-low',
  };

  /* What did not hold together about this signup. Review flags, never
     rejections: the point is that a person looks. */
  function flagsCard(h, flags) {
    const card = h('div', { class: 'asc-card' });
    card.appendChild(h('div', { class: 'asc-card-head' },
      h('div', { class: 'asc-card-title' }, 'Worth a look (' + flags.length + ')')));
    const pad = h('div', { class: 'asc-card-pad' });
    flags.forEach((f) => {
      pad.appendChild(h('div', { class: 'asc-phys-flag' },
        h('span', {
          class: 'asc-phys-flag-sev '
            + (FLAG_SEVERITY_CLASS[f.severity] || FLAG_SEVERITY_CLASS.low),
        }, (f.severity || 'low').toUpperCase()),
        h('span', {}, String(f.field || '').replace(/_/g, ' ') + ': '
          + String(f.issue || '').replace(/_/g, ' ')
          + (f.detail ? ': ' + f.detail : ''))));
    });
    card.appendChild(pad);
    return card;
  }

  /* The terms a physician signed, by name. An admin declining to pay for a
     case should be able to see that the doctor agreed to the quality term
     before they did the work, without opening a JSON blob. */
  const ATTESTATION_LABELS = {
    consentCredentialShare: 'credential sharing',
    attestIndependentJudgment: 'independent judgment',
    ipAssignment: 'IP assignment',
    noPhi: 'no PHI',
    attestWorkQuality: 'paid only if it meets the rubric',
    attestConfidentiality: 'confidentiality',
    attestNoDisciplinaryAction: 'no disciplinary action',
  };

  function signedTerms(a) {
    const names = Object.keys(ATTESTATION_LABELS)
      .filter(function (k) { return a[k] === true; })
      .map(function (k) { return ATTESTATION_LABELS[k]; });
    return names.length ? names.join(', ') : null;
  }

  function credentialsAsTyped(h, c, a) {
    const rows = [];
    const push = (k, v) => { if (v != null && v !== '') rows.push([k, String(v)]); };
    push('Full legal name', c.fullLegalName);
    push('Qualification', c.qualification || c.degree);
    push('Primary specialty', c.primarySpecialty);
    push('Licence number', c.licenseNumber);
    push('Licence state', c.licenseState);
    push('Registration number', c.registrationNumber);
    Object.keys(c.registryExtras || {}).forEach((k) => {
      push(k.replace(/([A-Z])/g, ' $1').toLowerCase(), c.registryExtras[k]);
    });
    const one = (row) => row && (row.institution || row.year)
      ? [row.institution, row.year].filter(Boolean).join(', ') : null;
    push('Residency', one(Array.isArray(c.residency) ? c.residency[0] : c.residency));
    push('Fellowship', one(Array.isArray(c.fellowship) ? c.fellowship[0] : c.fellowship));
    push('Practice status', c.practiceStatus);
    push('Clinical half-days / month', c.clinicalHalfDaysPerMonth);
    push('Languages', (c.languages || []).join(', '));
    (c.boardCertifications || []).forEach((b, i) => {
      if (!b || !b.board) return;
      push('Board certification' + (i ? ' ' + (i + 1) : ''),
           [b.board, b.specialty, b.subspecialty].filter(Boolean).join(', '));
    });
    // The signature. Collected since the first version of this form and shown
    // to nobody, including the person who signed it.
    push('Signed with', a.signedInitials);
    push('Terms signed', signedTerms(a));
    if (!rows.length) return null;
    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', { class: 'asc-card-title' }, 'Credentials as typed')),
      h('div', { class: 'asc-card-pad' }, kvBlock(h, '', rows)));
  }

  function historyCard(ctx, title, headers, rows) {
    const { h } = ctx;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, title,
          h('span', { class: 'asc-badge asc-badge-count', style: 'margin-left:8px' }, String(rows.length))))));
    if (!rows.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-empty' }, 'Nothing yet.')));
      return card;
    }
    card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
      h('thead', {}, h('tr', {}, headers.map((x) => h('th', {}, x)))),
      h('tbody', {}, rows.map((cells) => h('tr', {}, cells.map((c) => h('td', {}, c))))))));
    return card;
  }

  window.AdminPhysiciansSection = {
    render,
    reset() { selectedId = null; pendingId = null; activeTab = null; },
  };
})();
