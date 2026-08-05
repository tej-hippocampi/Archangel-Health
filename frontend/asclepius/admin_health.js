/* ═══════════════════════════════════════════════════════════
   Admin · Health Systems section (PRD C Phase 2)

   One row per health system — who supplies our data — with a
   click-through detail view showing that system's pipeline in
   explicit workflow buckets:

     NEEDS ATTENTION   safety-held cases (PHI, missing blobs) — never buried
     NEEDS REVIEW      uploaded, not yet examined
     READY TO PROMOTE  reviewed and clean, not yet a task
     IN PRODUCTION     promoted; cases live
     BROKERING         held for brokering, never promoted

   Every bucket downloads, including In Production: you must be able
   to retrieve anything you have already shipped.

   BROKERING IS ITS OWN BUCKET, not a badge inside another one (PRD-I
   §5). It has a different lifecycle — it is never promoted — and the
   server refuses to promote it. But a design that relies only on the
   server refusing is a design where an operator keeps trying, so the
   bucket has no Promote button at all: brokering data never sits next
   to the control that would send it into the task pipeline.

   Purpose renders as a plain word with a mono chip. Green for task
   creation (it becomes physician-authored work), muted grey for
   brokering — NOT pink. Brokering is a normal business line, and pink
   in this palette means flag / PHI / critical. An unresolved purpose
   is lime, because lime means "needs attention" and an unset purpose
   is a work item rather than a default.

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
        h('th', {}, 'Purpose'),
        h('th', {}, 'Physicians'),
        h('th', {}, 'Uploads'),
        h('th', {}, 'Portal accounts'),
        h('th', {}, 'Last activity'),
        h('th', {}, ''))),
      h('tbody', {}, rows.map((r) => {
        const openBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm' }, 'Open');
        openBtn.addEventListener('click', () => { selectedHsId = r.hs_id; render(container.parentNode, ctx); });
        // One chip per account, because an organization may legitimately hold one
        // of each and collapsing them to a single value would have to pick a winner.
        const chips = (r.purposes || []).map((p) => purposeChip(h, p));
        const tr = h('tr', { class: 'asc-row-click' },
          h('td', {}, h('strong', {}, r.name), r.active ? '' : h('span', { class: 'asc-badge asc-badge-gray', style: 'margin-left:8px' }, 'Inactive')),
          h('td', {}, h('code', { class: 'asc-mono asc-dim' }, r.hs_id)),
          h('td', {}, chips.length ? chips : '—'),
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
    renderStoragePanel(container, ctx);
  }

  // ─── Storage reconciliation (PRD I-0 §F4) ─────────────────
  // A non-zero missing count is an INCIDENT, not a metric: each one is a case
  // whose image 404s when a physician opens it, or that ships to a buyer as a
  // reference resolving to nothing. The healthy state says so explicitly —
  // an empty panel and a healthy panel must not look identical, or "nothing
  // shown" reads as "nothing wrong" whether or not the check even ran.
  async function renderStoragePanel(container, ctx) {
    const { h, api, fmtDate } = ctx;
    const card = h('div', { class: 'asc-card', style: 'margin-top:16px' });
    container.appendChild(card);
    let rep;
    try {
      rep = await api('/admin/storage/reconcile');
    } catch (e) {
      card.appendChild(h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          e.message || 'Could not check storage integrity.')));
      return;
    }
    const missing = rep.missing_count || 0;
    const nonDurable = (rep.storage || []).filter((s) => !s.durable);
    card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
      h('div', { class: 'asc-card-title' }, 'Storage integrity',
        missing
          // Pink: this one IS critical — it is data that is gone.
          ? h('span', { class: 'asc-badge asc-badge-red', style: 'margin-left:8px' },
              missing + ' missing')
          : h('span', { class: 'asc-badge asc-badge-green', style: 'margin-left:8px' }, 'OK')),
      h('div', { class: 'asc-card-sub' },
        missing
          ? missing + ' asset reference' + (missing === 1 ? '' : 's') +
            ' point at blobs that are gone from disk. This is data loss, not a warning.'
          : 'All ' + (rep.n_rows || 0) + ' asset references resolve. ' +
            (rep.orphan_count || 0) + ' unreferenced blob' +
            ((rep.orphan_count || 0) === 1 ? '' : 's') + ' on disk (reported, never deleted).'))));
    const body = h('div', { class: 'asc-card-pad' });
    if (nonDurable.length) {
      nonDurable.forEach((s) => body.appendChild(
        h('div', { class: 'asc-hs-reason' }, s.store + ' — ' + s.detail)));
    }
    (rep.missing_blobs || []).slice(0, 20).forEach((m) => body.appendChild(
      h('div', { class: 'asc-hs-reason' },
        h('code', { class: 'asc-mono' }, String(m.sha256 || '').slice(0, 12)),
        ' · case ', String(m.case_id || '—'), ' · ', String(m.detail || m.study_id || ''))));
    if (rep.checked_at) {
      body.appendChild(h('div', { class: 'asc-dim', style: 'font-size:12px' },
        'Checked ' + fmtDate(rep.checked_at)));
    }
    card.appendChild(body);
  }

  // ─── Purpose chip ─────────────────────────────────────────
  // Accent carries meaning in this palette, so it is read off the server's
  // verdict rather than re-derived here — one place decides what a purpose
  // means, and it is the same place the promotion gate reads.
  const PURPOSE_BADGE = { green: 'asc-badge-green', grey: 'asc-badge-gray',
                          lime: 'asc-badge-lime' };

  function purposeChip(h, row) {
    const cls = PURPOSE_BADGE[row.accent] || 'asc-badge-gray';
    return h('span', { class: 'asc-badge ' + cls }, row.label || '—');
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
    // No 'promote' action, deliberately. See the header note.
    { key: 'brokering', title: 'Brokering',
      sub: 'Held for brokering. These are never promoted to tasks — the server refuses, and there is no button here that would try.',
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
            h('th', {}, 'Uploads are for'),
            h('th', {}, 'Last sign-in'), h('th', {}, 'Status'))),
          h('tbody', {}, users.map((u) => h('tr', {},
            h('td', {}, h('code', { class: 'asc-mono' }, u.username)),
            h('td', {}, u.email || '—'),
            h('td', {}, purposeChip(h, u),
              u.resolved ? '' : purposeResolver(ctx, hsId, u.username, container)),
            h('td', {}, u.last_login ? fmtDate(u.last_login) : 'Never'),
            h('td', {}, u.active
              ? h('span', { class: 'asc-badge asc-badge-green' }, 'Active')
              : h('span', { class: 'asc-badge asc-badge-gray' }, 'Disabled')))))))));
    }
    if (data.link_purpose_note) {
      container.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-dim' }, data.link_purpose_note))));
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
            h('th', {}, 'Received'), h('th', {}, 'File'), h('th', {}, 'Purpose'),
            h('th', {}, 'Cases'), h('th', {}, 'Notes'), h('th', {}, ''))),
          h('tbody', {}, items.map((it) => bucketRow(ctx, spec, it, hsId, container))))));
      }
      container.appendChild(card);
    });
  }

  function metaCell(h, label, value) {
    return h('div', { class: 'asc-hs-meta-cell' },
      h('div', { class: 'asc-hs-meta-value' }, value),
      h('div', { class: 'asc-hs-meta-label' }, label));
  }

  // Set the purpose on a row the admin has to resolve. A "Purpose not set" row is
  // a WORK ITEM, not a default — the promotion gate reads NULL as task creation,
  // so leaving it is a decision, just not one anybody made deliberately.
  function purposeResolver(ctx, hsId, username, container) {
    const { h, api, toast } = ctx;
    const wrap = h('span', { style: 'margin-left:8px;white-space:nowrap' });
    [['task_creation', 'Task creation'], ['brokering', 'Brokering']].forEach((pair) => {
      const b = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm',
                              style: 'margin-left:4px' }, 'Set: ' + pair[1]);
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const path = username
            ? '/admin/health-systems/' + encodeURIComponent(hsId) + '/accounts/' +
              encodeURIComponent(username) + '/purpose'
            : null;
          const res = await api(path, { method: 'POST', body: { purpose: pair[0] } });
          toast(res.message || 'Purpose set.', 'success');
          renderDetail(container, ctx, hsId);
        } catch (e) {
          b.disabled = false;
          toast(e.message || 'Could not set the purpose.', 'error');
        }
      });
      wrap.appendChild(b);
    });
    return wrap;
  }

  function uploadPurposeResolver(ctx, uploadId, hsId, container) {
    const { h, api, toast } = ctx;
    const wrap = h('span', { style: 'margin-left:8px;white-space:nowrap' });
    [['task_creation', 'Task creation'], ['brokering', 'Brokering']].forEach((pair) => {
      const b = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm',
                              style: 'margin-left:4px' }, 'Set: ' + pair[1]);
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const res = await api('/admin/uploads/' + encodeURIComponent(uploadId) + '/purpose',
                                { method: 'POST', body: { purpose: pair[0] } });
          toast(res.message || 'Purpose set.', 'success');
          renderDetail(container, ctx, hsId);
        } catch (e) {
          b.disabled = false;
          toast(e.message || 'Could not set the purpose.', 'error');
        }
      });
      wrap.appendChild(b);
    });
    return wrap;
  }

  function bucketRow(ctx, spec, it, hsId, container) {
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
    // Chain of custody, on every row: what we hold, how much of it, and when we
    // proved it. This is what you show a partner who asks "did you get everything".
    const custody = h('div', { class: 'asc-dim asc-mono', style: 'font-size:11px' },
      it.sha256_short ? 'sha ' + it.sha256_short : 'sha —',
      ' · ', formatBytes(it.size_bytes),
      ' · ', it.verified_at ? 'verified ' + fmtDate(it.verified_at) : 'not verified');
    return h('tr', {},
      h('td', {}, it.received_at ? fmtDate(it.received_at) : '—'),
      h('td', {},
        h('div', {}, it.filename || '—'),
        h('div', { class: 'asc-dim asc-mono', style: 'font-size:11px' }, it.upload_id),
        custody),
      h('td', {}, purposeChip(h, it),
        it.resolved ? '' : uploadPurposeResolver(ctx, it.upload_id, hsId, container)),
      h('td', {}, caseCountText(it)),
      h('td', { class: 'asc-hs-notes' }, notes.length ? notes : '—'),
      h('td', { class: 'asc-hs-actions' }, actions));
  }

  function formatBytes(n) {
    if (!n && n !== 0) return '—';
    if (n < 1024) return n + ' B';
    const units = ['KB', 'MB', 'GB', 'TB'];
    let v = n / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return v.toFixed(v >= 10 ? 0 : 1) + ' ' + units[i];
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
