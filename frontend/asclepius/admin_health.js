/* ═══════════════════════════════════════════════════════════
   Admin · Health Systems section (PRD C Phase 2)

   One row per health system — who supplies our data — with a
   click-through detail view showing that system's pipeline in
   explicit workflow buckets:

     NEEDS ATTENTION   safety-held cases (PHI, missing blobs) — never buried
     NEEDS REVIEW      uploaded, not yet examined
     READY TO PROMOTE  reviewed and clean, not yet a task
     IN PRODUCTION     promoted; cases live

   Every bucket downloads, including In Production: you must be able
   to retrieve anything you have already shipped.

   Loaded as its own file (§3.3 file ownership); asclepius.js passes
   a ctx of shared helpers {h, api, clear, toast, loadingCard,
   downloadBlob, fmtDate, openPipeline}. DOM is built exclusively
   with ctx.h — no innerHTML, no template strings.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Rendering state local to this section.
  let selectedHsId = null;

  function render(body, ctx) {
    const { h, clear } = ctx;
    clear(body);
    const container = h('div', {});
    body.appendChild(container);
    if (selectedHsId) renderDetail(container, ctx, selectedHsId);
    else renderList(container, ctx);
  }

  // ─── List: one row per health system ──────────────────────
  async function renderList(container, ctx) {
    const { h, api, clear, toast, loadingCard, fmtDate } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading health systems…'));
    let rows;
    try {
      const res = await api('/admin/health-systems');
      rows = res.health_systems || [];
    } catch (e) {
      clear(container);
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, e.message || 'Could not load health systems.'))));
      return;
    }
    clear(container);

    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Health systems'),
        h('div', { class: 'asc-card-sub' },
          'Who supplies our data, and where each batch is in the pipeline. ' +
          'Send new upload access from the Pipeline tools tab.'))));

    if (!rows.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' },
          'No health systems yet. Send an organization its upload access and it will appear here.')));
      container.appendChild(card);
      return;
    }

    const table = h('table', { class: 'asc-table' },
      h('thead', {}, h('tr', {},
        h('th', {}, 'Health system'),
        h('th', {}, 'ID'),
        h('th', {}, 'Physicians'),
        h('th', {}, 'Uploads'),
        h('th', {}, 'Portal accounts'),
        h('th', {}, 'Last activity'),
        h('th', {}, ''))),
      h('tbody', {}, rows.map((r) => {
        const openBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm' }, 'Open');
        openBtn.addEventListener('click', () => { selectedHsId = r.hs_id; render(container.parentNode, ctx); });
        const tr = h('tr', { class: 'asc-row-click' },
          h('td', {}, h('strong', {}, r.name), r.active ? '' : h('span', { class: 'asc-badge asc-badge-gray', style: 'margin-left:8px' }, 'Inactive')),
          h('td', {}, h('code', { class: 'asc-mono asc-dim' }, r.hs_id)),
          h('td', {}, String(r.physicians_linked || 0)),
          h('td', {}, String(r.uploads_count || 0)),
          h('td', {}, String((r.portal_users || []).length)),
          h('td', {}, r.last_activity ? fmtDate(r.last_activity) : '—'),
          h('td', {}, openBtn));
        tr.addEventListener('click', (ev) => {
          if (ev.target === openBtn) return;
          selectedHsId = r.hs_id; render(container.parentNode, ctx);
        });
        return tr;
      })));
    card.appendChild(h('div', { class: 'asc-table-wrap' }, table));
    container.appendChild(card);
  }

  // ─── Detail: the pipeline buckets for one system ──────────
  const BUCKETS = [
    { key: 'needs_attention', title: 'Needs attention',
      sub: 'Held for a safety reason (unverified burned-in PHI, missing asset blob). Resolve before anything else.',
      cls: 'asc-hs-bucket-attention', actions: ['download', 'review'] },
    { key: 'needs_review', title: 'Needs review',
      sub: 'Uploaded, not yet examined.',
      cls: '', actions: ['download', 'review'] },
    { key: 'ready_to_promote', title: 'Ready to promote',
      sub: 'Reviewed and clean, not yet a task.',
      cls: '', actions: ['download', 'promote'] },
    { key: 'in_production', title: 'In production',
      sub: 'Promoted; cases live. Still downloadable — you can always retrieve what you have shipped.',
      cls: '', actions: ['download'] },
  ];

  async function renderDetail(container, ctx, hsId) {
    const { h, api, clear, toast, loadingCard, downloadBlob, fmtDate, openPipeline } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading health system…'));
    let data;
    try {
      data = await api('/admin/health-systems/' + encodeURIComponent(hsId));
    } catch (e) {
      clear(container);
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, e.message || 'Could not load this health system.'))));
      return;
    }
    clear(container);
    const hs = data.health_system || {};
    const buckets = data.buckets || {};

    const backBtn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' }, '← All health systems');
    backBtn.addEventListener('click', () => { selectedHsId = null; render(container.parentNode, ctx); });

    const head = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', {},
          h('div', { class: 'asc-card-title' }, hs.name || hsId),
          h('div', { class: 'asc-card-sub' },
            h('code', { class: 'asc-mono asc-dim' }, hs.hs_id || hsId),
            hs.contact_email ? ' · ' + hs.contact_email : '')),
        backBtn),
      h('div', { class: 'asc-card-pad asc-hs-meta' },
        metaCell(h, 'Physicians linked', String(data.physicians_linked || 0)),
        metaCell(h, 'Uploads', String((data.uploads_total || 0))),
        metaCell(h, 'Portal accounts', String((data.portal_users || []).length)),
        metaCell(h, 'Last activity', data.last_activity ? fmtDate(data.last_activity) : '—')));
    container.appendChild(head);

    // Portal accounts strip (who can sign in for this system).
    const users = data.portal_users || [];
    if (users.length) {
      container.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', {},
          h('div', { class: 'asc-card-title' }, 'Portal accounts'))),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Username'), h('th', {}, 'Email'),
            h('th', {}, 'Last sign-in'), h('th', {}, 'Status'))),
          h('tbody', {}, users.map((u) => h('tr', {},
            h('td', {}, h('code', { class: 'asc-mono' }, u.username)),
            h('td', {}, u.email || '—'),
            h('td', {}, u.last_login ? fmtDate(u.last_login) : 'Never'),
            h('td', {}, u.active
              ? h('span', { class: 'asc-badge asc-badge-green' }, 'Active')
              : h('span', { class: 'asc-badge asc-badge-gray' }, 'Disabled')))))))));
    }

    // The buckets, in workflow order. Needs attention renders only when
    // non-empty — but ALWAYS above the rest when it exists.
    BUCKETS.forEach((spec) => {
      const items = buckets[spec.key] || [];
      if (spec.key === 'needs_attention' && !items.length) return;
      const card = h('div', { class: 'asc-card asc-hs-bucket ' + spec.cls },
        h('div', { class: 'asc-card-head' }, h('div', {},
          h('div', { class: 'asc-card-title' }, spec.title,
            h('span', { class: 'asc-badge asc-badge-count', style: 'margin-left:8px' }, String(items.length))),
          h('div', { class: 'asc-card-sub' }, spec.sub))));
      if (!items.length) {
        card.appendChild(h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-empty' }, 'Nothing here right now.')));
      } else {
        card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Received'), h('th', {}, 'File'), h('th', {}, 'Cases'),
            h('th', {}, 'Notes'), h('th', {}, ''))),
          h('tbody', {}, items.map((it) => bucketRow(ctx, spec, it))))));
      }
      container.appendChild(card);
    });
  }

  function metaCell(h, label, value) {
    return h('div', { class: 'asc-hs-meta-cell' },
      h('div', { class: 'asc-hs-meta-value' }, value),
      h('div', { class: 'asc-hs-meta-label' }, label));
  }

  function bucketRow(ctx, spec, it) {
    const { h, toast, downloadBlob, fmtDate, openPipeline } = ctx;
    const actions = [];
    actions.push(btn(h, 'Download', 'asc-btn-subtle', () =>
      downloadBlob('/ingestion/uploads/' + it.upload_id + '/download',
                   it.filename || (it.upload_id + '.zip'))));
    if (spec.actions.includes('review')) {
      actions.push(btn(h, 'Review', 'asc-btn-primary', () => openPipeline(it)));
    }
    if (spec.actions.includes('promote')) {
      actions.push(btn(h, 'Promote to task', 'asc-btn-primary', () => openPipeline(it)));
    }
    const notes = [];
    (it.reasons || []).forEach((r) => notes.push(h('div', { class: 'asc-hs-reason' }, r)));
    if (!notes.length && it.note) notes.push(h('div', { class: 'asc-dim' }, it.note));
    return h('tr', {},
      h('td', {}, it.received_at ? fmtDate(it.received_at) : '—'),
      h('td', {},
        h('div', {}, it.filename || '—'),
        h('div', { class: 'asc-dim asc-mono', style: 'font-size:11px' }, it.upload_id)),
      h('td', {}, caseCountText(it)),
      h('td', { class: 'asc-hs-notes' }, notes.length ? notes : '—'),
      h('td', { class: 'asc-hs-actions' }, actions));
  }

  function caseCountText(it) {
    const c = it.case_counts || {};
    const parts = [];
    if (c.held) parts.push(c.held + ' held');
    if (c.clean) parts.push(c.clean + ' ready');
    if (c.promoted) parts.push(c.promoted + ' live');
    if (!parts.length) parts.push((it.case_total || 0) + ' cases');
    return parts.join(' · ');
  }

  function btn(h, label, cls, onClick) {
    const b = h('button', { class: 'asc-btn asc-btn-sm ' + cls, style: 'margin-right:6px' }, label);
    b.addEventListener('click', onClick);
    return b;
  }

  window.AdminHealthSection = {
    render,
    reset() { selectedHsId = null; },
    open(hsId) { selectedHsId = hsId || null; },
  };
})();
