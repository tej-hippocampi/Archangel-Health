/* ═══════════════════════════════════════════════════════════
   Admin · Physicians section (PRD C Phase 3)

   Who supplies our judgment, and are they credentialed?

   Table: Name · Email · Phone · Specialty · Tier · Verification ·
   Slack · Health system — grouped by health system (Independent for
   physicians with none). Filter chips carry live counts; Pending is
   the operator's launch-week job queue and is always visible.

   Vocabulary rules (§3 of the PRD):
   - Tier renders as a WORD, never a raw token: Labeler / Reviewer /
     Unassigned.
   - Verification has FOUR states: Approved / Pending / Rejected /
     Not checked — NULL is not "no".

   PRD-B's verification queue mounts as a tab inside this section:
   B owns the view, this file owns the shell. The mount point is the
   frozen global window.AsclepiusVerification.mount(el, ctx) — if it
   is absent, a VISIBLE error renders, never a quiet placeholder.

   Loaded as its own file (§3.3); DOM built exclusively with ctx.h.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Mirrors asclepius/capabilities.py TIERS. The one list this file filters,
  // counts and labels against.
  const TIERS = ['labeler', 'reviewer', 'advisor'];

  let activeChip = 'all';       // all | pending | labelers | reviewers | advisors | unassigned
  let activeView = 'roster';    // roster | verify — driven by the shell's sub-tabs
  let selectedId = null;        // physician profile view
  let cache = null;             // last /admin/physicians payload
  let rootEl = null;            // the section body we were mounted into
  let rootCtx = null;

  // ─── Vocabulary (four-state verification; tier words) ─────
  // THREE tiers, not two. A missing case here does not throw — it silently
  // renders "Unassigned" over a real advisor, which is the same class of bug
  // as the backend's `tier === 'reviewer'` equality: wrong, quiet, and only
  // discovered when the person tells you. Keep this map aligned with
  // asclepius/capabilities.py TIER_WORDS.
  function tierWord(t) {
    if (t === 'labeler') return 'Labeler';
    if (t === 'reviewer') return 'Reviewer';
    if (t === 'advisor') return 'Medical Advisor';
    return 'Unassigned';
  }
  function tierBadge(h, t) {
    if (t === 'labeler') return h('span', { class: 'asc-badge asc-badge-primary' }, 'Labeler');
    if (t === 'reviewer') return h('span', { class: 'asc-badge asc-badge-green' }, 'Reviewer');
    // Advisors are three people among fifty and you want to find them in one
    // glance — lime is the "new/notable" token from the locked design system.
    if (t === 'advisor') return h('span', { class: 'asc-badge asc-badge-lime' }, 'Advisor');
    return h('span', { class: 'asc-badge asc-badge-gray' }, 'Unassigned');
  }
  function verificationBadge(h, v) {
    if (v === 'approved') return h('span', { class: 'asc-badge asc-badge-green' }, 'Approved');
    if (v === 'pending') return h('span', { class: 'asc-badge asc-badge-amber' }, 'Pending');
    if (v === 'rejected') return h('span', { class: 'asc-badge asc-badge-red' }, 'Rejected');
    // NULL is a different fact from "rejected" — an operator chasing the wrong
    // physicians is the failure mode this fourth state prevents.
    return h('span', { class: 'asc-badge asc-badge-gray' }, 'Not checked');
  }
  function slackText(s) {
    if (s === true || s === 1) return 'Joined';
    if (s === false || s === 0) return 'Not joined';
    return '—'; // not checked
  }

  // ─── Section entry — the shell's sub-tabs pick the view ───
  function render(body, ctx, view) {
    rootEl = body; rootCtx = ctx;
    if (view) { if (view !== activeView) selectedId = null; activeView = view; }
    const { h, clear } = ctx;
    clear(body);
    const inner = h('div', {});
    body.appendChild(inner);
    if (activeView === 'verify') renderVerifyTab(inner, ctx);
    else if (selectedId) renderProfile(inner, ctx, selectedId);
    else renderRoster(inner, ctx);
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
        body.appendChild(h('div', { class: 'asc-error' },
          'Could not load model health: ' + e.message));
      });
    });
  }

  function renderModelHealthBody(body, ctx, weights, fairness) {
    const { h } = ctx;
    clearNode(body);

    body.appendChild(h('div', { class: 'vq-section-label' },
      'Tier weights — posterior vs the rule it started from'));
    body.appendChild(h('div', { class: 'vq-attempt' },
      (weights.pending_decisions || 0) + ' decision(s) not yet folded in · '
      + 'each batch may move any weight by at most ' + weights.max_delta_per_batch));

    (weights.weights || []).forEach(function (w) {
      // A pinned row that has drifted is the single most serious thing this page
      // can show, so it is the only thing on it painted pink.
      const broken = w.pinned && Math.abs(w.drift) > 0;
      body.appendChild(h('div', { class: broken ? 'vq-flag' : 'vq-reason' },
        (w.pinned ? 'PINNED  ' : '        ')
        + w.feature + '  m=' + w.m.toFixed(3)
        + '  drift=' + (w.drift >= 0 ? '+' : '') + w.drift.toFixed(3)
        + '  sd=' + w.sd.toFixed(3)
        + (broken ? '  ← PINNED WEIGHT MOVED. Stop writing weights and investigate.' : '')));
    });

    body.appendChild(h('div', { class: 'vq-section-label' },
      'Reviewer selection rate by voluntary self-report — four-fifths rule'));
    const dims = fairness.dimensions || {};
    if (!Object.keys(dims).length) {
      body.appendChild(h('div', { class: 'vq-attempt' },
        'No self-reported demographics on file yet. The monitor needs volunteers, not '
        + 'inference — nothing here is derived from a name, a school, or a region.'));
    }
    Object.keys(dims).sort().forEach(function (dim) {
      body.appendChild(h('div', { class: 'vq-reason' }, dim));
      const groups = dims[dim];
      Object.keys(groups).sort().forEach(function (g) {
        const row = groups[g];
        const flagged = row.counted && row.impact_ratio !== null && row.impact_ratio < 0.8;
        body.appendChild(h('div', { class: flagged ? 'vq-flag' : 'vq-attempt' },
          '  ' + g + ': ' + (row.rate * 100).toFixed(0) + '% reviewer'
          + ' (n=' + row.n + ')'
          + (row.impact_ratio === null ? ''
             : '  impact ratio ' + row.impact_ratio.toFixed(2))
          + (row.counted ? '' : '  — below n=' + fairness.min_group_n
                                + ', reported but not alerted on')));
      });
    });
    (fairness.alerts || []).forEach(function (a) {
      body.appendChild(h('div', { class: 'vq-flag' }, a.message));
    });
  }

  function renderVerifyTab(container, ctx) {
    const { h } = ctx;
    renderModelHealth(container, ctx);
    // PRD-B owns this global (Seam 1). Do not re-derive the name — an invented
    // name is the exact bug this replaced: for a whole build round this tab
    // probed a global nobody defined and quietly rendered a placeholder, while
    // the real queue sat in the same merge. Every physician who signed up was
    // locked out, because approving one had no route through the UI.
    const mount = h('div', { id: 'ascVerifyQueueMount' });
    container.appendChild(mount);
    if (window.AsclepiusVerification
        && typeof window.AsclepiusVerification.mount === 'function') {
      window.AsclepiusVerification.mount(mount, ctx);
      return;
    }
    // A visible failure, not a silent placeholder. A quiet fallback is what hid
    // this for an entire build round — an operator must be able to notice and
    // report it.
    clearNode(mount);
    mount.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-error' },
        'Verification module failed to load. Reload the page; if it persists, '
        + 'this is a deploy problem — physicians cannot be approved until it is fixed.'))));
  }

  function clearNode(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  // ─── Roster ───────────────────────────────────────────────
  async function renderRoster(container, ctx) {
    const { h, api, clear, loadingCard } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading physicians…'));
    try {
      cache = await api('/admin/physicians');
    } catch (e) {
      clear(container);
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, e.message || 'Could not load physicians.'))));
      return;
    }
    clear(container);
    const rows = cache.physicians || [];
    const counts = cache.counts || {};

    // Filter chips with live counts — Pending is the launch-week job queue.
    const chips = [
      ['all', 'All', counts.all],
      ['pending', 'Pending', counts.pending],
      ['labelers', 'Labelers', counts.labelers],
      ['reviewers', 'Reviewers', counts.reviewers],
      ['advisors', 'Advisors', counts.advisors],
      ['unassigned', 'Unassigned', counts.unassigned],
    ];
    const chipRow = h('div', { class: 'asc-phys-chips' }, chips.map(([id, label, n]) => {
      const el = h('button', {
        class: 'asc-phys-chip' + (id === 'pending' ? ' asc-chip-pending' : '')
          + (activeChip === id ? ' active' : ''),
      }, label, h('span', { class: 'asc-chip-count' }, String(n == null ? 0 : n)));
      el.addEventListener('click', () => { activeChip = id; renderRoster(container, ctx); });
      return el;
    }));
    container.appendChild(chipRow);

    const filtered = rows.filter((p) => {
      if (activeChip === 'pending') return p.verification_status === 'pending';
      if (activeChip === 'labelers') return p.tier === 'labeler';
      if (activeChip === 'reviewers') return p.tier === 'reviewer';
      if (activeChip === 'advisors') return p.tier === 'advisor';
      // "Unassigned" means NO tier — not "a tier this filter forgot about".
      // Written as an explicit membership test so a fourth tier shows up in
      // its own chip rather than quietly swelling this one.
      if (activeChip === 'unassigned') return !TIERS.includes(p.tier);
      return true;
    });

    if (!filtered.length) {
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'No physicians match this filter.'))));
      return;
    }

    // Group by health system; Independent for physicians with none.
    const groups = new Map();
    filtered.forEach((p) => {
      const key = p.health_system_name || 'Independent';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(p);
    });
    const names = Array.from(groups.keys()).sort((a, b) => {
      if (a === 'Independent') return 1;
      if (b === 'Independent') return -1;
      return a.localeCompare(b);
    });

    const card = h('div', { class: 'asc-card' });
    names.forEach((name) => {
      const members = groups.get(name);
      const det = h('details', { class: 'asc-phys-group', open: '' },
        h('summary', {}, h('strong', {}, name),
          h('span', { class: 'asc-dim' }, members.length + ' physician' + (members.length === 1 ? '' : 's'))),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Name'), h('th', {}, 'Email'), h('th', {}, 'Phone'),
            h('th', {}, 'Specialty'), h('th', {}, 'Tier'), h('th', {}, 'Verification'),
            h('th', {}, 'Slack'), h('th', {}, 'Health system'))),
          h('tbody', {}, members.map((p) => physicianRow(ctx, container, p))))));
      card.appendChild(det);
    });
    container.appendChild(card);
  }

  function physicianRow(ctx, container, p) {
    const { h } = ctx;
    const tr = h('tr', { class: 'asc-row-click' },
      h('td', {}, h('strong', {}, p.name || '—')),
      h('td', {}, p.email || '—'),
      h('td', {}, p.phone || '—'),
      h('td', {}, p.specialty || '—'),
      h('td', {}, tierBadge(h, p.tier)),
      h('td', {}, verificationBadge(h, p.verification_status)),
      // A former advisor's Slack role still reads "Medical Advisor" while their
      // tier reads "Reviewer" — because the equity and the agreement survive a
      // tier change by design. Say so, rather than showing two fields that look
      // like a bug.
      h('td', {}, p.slack_role ? slackText(p.slack_joined) + ' · ' + p.slack_role
        : slackText(p.slack_joined),
        p.former_advisor
          ? h('span', { class: 'asc-badge asc-badge-amber', style: 'margin-left:6px' },
              'Former advisor · equity on file')
          : null),
      h('td', {}, p.health_system_name || 'Independent'));
    tr.addEventListener('click', () => { selectedId = p.id; rerender(); });
    return tr;
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

    container.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', {},
          h('div', { class: 'asc-card-title' }, p.name || p.email || id),
          h('div', { class: 'asc-card-sub' },
            tierWord(p.tier), ' · ', p.specialty || 'No specialty', ' · ',
            p.health_system_name || 'Independent')),
        backBtn),
      h('div', { class: 'asc-card-pad asc-phys-profile-grid' },
        kvBlock(h, 'Identity', [
          ['Email', p.email], ['Phone', p.phone],
          ['NPI', p.npi], ['NPI check', npiWord(p.npi_verified)],
          ['LinkedIn', p.linkedin_url], ['CV on file', p.cv_on_file ? 'Yes' : 'No'],
        ]),
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
    // Advisors review too (they hold the REVIEW capability), so gating this on
    // `tier === 'reviewer'` would hide a real advisor's review history from the
    // admin who needs it — the two-state check, one more time.
    if (p.tier === 'reviewer' || p.tier === 'advisor') {
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
    reset() { selectedId = null; activeView = 'roster'; activeChip = 'all'; },
  };
})();
