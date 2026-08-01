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
   B owns the view, this file owns the shell. B mounts by defining
   window.renderVerificationQueue(el, ctx) — if absent, a quiet
   placeholder renders instead.

   Loaded as its own file (§3.3); DOM built exclusively with ctx.h.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  let activeChip = 'all';       // all | pending | labelers | reviewers | unassigned
  let activeView = 'roster';    // roster | verify — driven by the shell's sub-tabs
  let selectedId = null;        // physician profile view
  let cache = null;             // last /admin/physicians payload
  let rootEl = null;            // the section body we were mounted into
  let rootCtx = null;

  // ─── Vocabulary (four-state verification; tier words) ─────
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

  function renderVerifyTab(container, ctx) {
    const { h } = ctx;
    if (typeof window.renderVerificationQueue === 'function') {
      // PRD-B owns this view; hand it the mount + shared helpers.
      const mount = h('div', { id: 'ascVerifyQueueMount' });
      container.appendChild(mount);
      window.renderVerificationQueue(mount, ctx);
      return;
    }
    container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-empty' },
        'The verification queue ships with the identity-verification work. ' +
        'Once it lands, it appears here — physicians awaiting review are counted ' +
        'in the Pending chip on the Roster tab either way.'))));
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
      if (activeChip === 'unassigned') return !p.tier;
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
      h('td', {}, slackText(p.slack_joined)),
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
          ['Slack', slackText(p.slack_joined)],
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
