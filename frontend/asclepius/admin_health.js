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

   AN UNSET SPECIALTY IS THE SAME KIND OF THING AS AN UNSET PURPOSE:
   a decision nobody has made, not a default. Ingest refuses to guess
   one, and both promote endpoints refuse to run without one — so a
   Promote button on a row that has none is a button that leads to a
   409 naming a control the operator cannot find. The button is dead
   until the specialty is set, the reason is inline, and the control
   that sets it sits in the same row (ctx.specialtyResolver).

   Loaded as its own file (§3.3 file ownership); asclepius.js passes
   a ctx of shared helpers {h, api, clear, toast, loadingCard,
   downloadBlob, fmtDate, openPipeline, specialtyResolver,
   specialtyBlockReason}. DOM is built exclusively with ctx.h — no
   innerHTML, no template strings.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Rendering state local to this section.
  let selectedHsId = null;

  // The two destinations, in the same order and wording the provision form
  // uses, so an operator meets one vocabulary rather than two.
  const PURPOSES = [
    { key: 'task_creation', label: 'Task creation' },
    { key: 'brokering', label: 'Brokering' },
  ];

  // The organization's onboarding state, as a chip. Four of these are the PRD's
  // chips; `declined` is the fifth because Decline is one of the two buttons on
  // the card and its outcome has to be representable. `active` is also what a
  // NULL collapses to — a health system provisioned before the state machine
  // existed — which is why the DLA chip is rendered SEPARATELY: "active, no
  // agreement on file" is a real and visible condition, not a hidden one.
  const STATE_CHIPS = {
    intake: { label: 'Intake', cls: 'asc-badge-gray' },
    submitted: { label: 'Submitted', cls: 'asc-badge-lime' },
    approved_awaiting_dla: { label: 'Awaiting DLA', cls: 'asc-badge-amber' },
    active: { label: 'Active', cls: 'asc-badge-green' },
    declined: { label: 'Declined', cls: 'asc-badge-gray' },
  };

  function stateChip(h, state) {
    const meta = STATE_CHIPS[state] || STATE_CHIPS.active;
    return h('span', { class: 'asc-badge ' + meta.cls }, meta.label);
  }

  // `DLA ✓ v1 · signed by {name} · {date}` (PRD §5.3), or the honest absence.
  // The signer and the date are RENDERED, not hidden in a tooltip: who signed
  // is the thing an operator scanning this column actually wants, and a
  // title attribute is invisible to anyone who is not holding a mouse.
  function dlaChip(h, fmtDate, agreement) {
    if (!agreement) {
      return h('span', { class: 'asc-dim asc-mono', style: 'font-size:11px' }, 'no DLA');
    }
    return h('div', {},
      h('span', { class: 'asc-badge asc-badge-green' },
        'DLA \u2713 ' + (agreement.doc_version || '')),
      h('div', { class: 'asc-dim', style: 'font-size:11px; margin-top:2px' },
        (agreement.signed_by || 'signed') +
        (agreement.signed_at ? ' \u00b7 ' + fmtDate(agreement.signed_at) : '')));
  }

  // Operator-facing labels for the intake answers. The questions themselves are
  // server-owned (the portal renders them from /hs/intake); these are just the
  // short headings we read the replies under.
  const INTAKE_LABELS = [
    ['organization', 'Who they are'],
    ['size_type', 'Size'],
    ['data_held', 'Data they hold'],
    ['licensable', 'Open to licensing'],
    ['timeline', 'Timeline'],
  ];

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
    // Self-signups waiting on a person. Rendered above everything, same rule
    // this file applies to needs-attention buckets: only when it exists, but
    // always first when it does. A hospital sitting in review cannot upload,
    // so a queue nobody sees is a partner nobody answers.
    const pendingSlot = h('div', {});
    // Two more slots filled after the systems table paints, for the same reason
    // the pending queue has one: neither should hold the page open, and neither
    // failing should take the systems list down with it.
    const leadsSlot = h('div', {});
    const requestsSlot = h('div', {});
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

    // The storage and demo-video panels are NOT about health systems and must not
    // be gated on there being any: a fresh deployment has zero partners and is
    // exactly the deployment that needs to upload the demo video and check that
    // the volume is durable. Returning early here made both panels unreachable —
    // and the demo uploader exists precisely so that job needs no terminal.
    if (!rows.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' },
          'No health systems yet. Send an organization its upload access and it will appear here.')));
      // The leads card renders here too. A deployment with zero partners is
      // exactly the one whose inbound leads matter most: they are the
      // organizations that have not become rows in this table yet.
      container.appendChild(leadsSlot);
      container.appendChild(card);
      renderPartnerLeads(leadsSlot, ctx);
      renderStoragePanel(container, ctx);
      renderDemoVideoPanel(container, ctx);
      return;
    }

    const table = h('table', { class: 'asc-table' },
      h('thead', {}, h('tr', {},
        h('th', {}, 'Health system'),
        h('th', {}, 'State'),
        h('th', {}, 'Agreement'),
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
          h('td', {}, h('strong', {}, r.name), r.active ? '' : h('span', { class: 'asc-badge asc-badge-gray', style: 'margin-left: var(--sp-2)' }, 'Inactive')),
          h('td', {}, stateChip(h, r.onboarding_state)),
          h('td', {}, dlaChip(h, fmtDate, r.agreement)),
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
    container.appendChild(pendingSlot);
    container.appendChild(leadsSlot);
    container.appendChild(requestsSlot);
    container.appendChild(card);
    renderPendingSignups(pendingSlot, ctx, container);
    renderPartnerLeads(leadsSlot, ctx);
    renderDataRequests(requestsSlot, ctx, container);
    renderStoragePanel(container, ctx);
    renderDemoVideoPanel(container, ctx);
  }

  // ─── Partner leads ────────────────────────────────────────
  // The landing forms have been writing into `lead_submissions` since they
  // shipped and nothing has ever read them back. Every submission is an
  // attestation about authority over de-identified data, which makes it a legal
  // audit trail, and an audit trail nobody can read is a file nobody keeps.
  //
  // Above the systems list because a lead is the TOP of the same pipeline that
  // list shows: these are the organizations that have not become rows yet.
  //
  // Read-only, deliberately. Replying happens in an inbox, so the control here
  // is a mailto link rather than a compose box we would then have to keep in
  // sync with a thread nobody can see from this page.
  const LEAD_CHIPS = {
    health_system_partner: 'asc-badge-green',
    provide_data: 'asc-badge-lime',
    request_data: 'asc-badge-amber',
    research_notify: 'asc-badge-gray',
  };
  const LEAD_PAGE = 8;
  // Mirrors routers/leads.py _UNANSWERED. Compared rather than reproduced: the
  // server decides what an unanswered question reads as, this side only decides
  // that it should look different from an answer.
  const UNANSWERED = 'Not answered';

  async function renderPartnerLeads(slot, ctx) {
    const { h, api, clear, fmtDate } = ctx;
    let data;
    try {
      // The lead table lives beside the public form that writes it, on
      // /api/leads rather than under the asclepius prefix, so this read names
      // its own base.
      data = await api('/leads/admin?limit=' + LEAD_PAGE, { base: '/api' });
    } catch (e) {
      clear(slot);
      slot.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          e.message || 'Could not load partner leads.'))));
      return;
    }
    clear(slot);
    const rows = data.leads || [];
    if (!rows.length) return;

    const card = h('div', { class: 'asc-card asc-hs-bucket' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Partner leads',
          h('span', { class: 'asc-badge asc-badge-count', style: 'margin-left: var(--sp-2)' },
            String(rows.length))),
        h('div', { class: 'asc-card-sub' },
          'Who wrote in through the landing forms, newest first. Every ' +
          'submission is kept; replying happens in your inbox.'))));

    rows.forEach((r) => {
      const body = h('div', { class: 'asc-card-pad asc-hs-lead' });
      body.appendChild(h('div', { class: 'asc-hs-lead-head' },
        h('span', { class: 'asc-badge ' + (LEAD_CHIPS[r.source] || 'asc-badge-gray') },
          r.source_label || r.source),
        h('strong', {}, r.email || '(no address)'),
        h('span', { class: 'asc-dim', style: 'font-size:12px' },
          r.created_at ? fmtDate(r.created_at) : '')));
      // The qualifying answers first, then the prose. These three are what the
      // Sep 1 meeting agreed the form must ask, they are the part with legal
      // weight, and an operator deciding whether this call is worth taking
      // reads them before anything the visitor typed.
      //
      // Labels come off the wire. The server owns the wording of the questions
      // and a second copy here is a second thing to reword.
      const qual = r.qualifying || [];
      if (qual.length) {
        const dl = h('dl', { class: 'asc-hs-lead-qual' });
        qual.forEach((q) => {
          dl.appendChild(h('dt', { class: 'asc-hs-lead-qual-q' }, q.label || ''));
          // "Not answered" is rendered dim rather than omitted: a question the
          // form asked and nobody answered is a fact about the submission.
          const missing = (q.answer || '') === UNANSWERED;
          dl.appendChild(h('dd', {
            class: 'asc-hs-lead-qual-a' + (missing ? ' asc-hs-lead-qual-none' : ''),
          }, q.answer || ''));
        });
        body.appendChild(dl);
      }
      // VERBATIM. It is the attestation, and a truncated attestation is not one.
      body.appendChild(h('div', { class: 'asc-hs-lead-message' }, r.message || ''));
      body.appendChild(h('a', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
                                href: 'mailto:' + (r.email || '') },
                         'Reply by email'));
      card.appendChild(body);
    });
    slot.appendChild(card);
  }

  // ─── Data requests ────────────────────────────────────────
  // "We need 100 nephrology cases", to every partner who has signed and may
  // upload. The compose form and the open list live together because the thing
  // an operator does after sending one is watch whether anyone answered.
  //
  // The delivery tally is shown next to every request for a reason: a request
  // that produced no replies has two very different explanations, and "nobody
  // had the cases" and "nobody was told" look identical from here without it.
  function requestField(h, id, label, attrs) {
    const input = h('input', Object.assign({ class: 'asc-input', id: id,
                                             placeholder: label }, attrs || {}));
    return input;
  }

  async function renderDataRequests(slot, ctx, listContainer) {
    const { h, api, clear, toast, fmtDate } = ctx;
    let data;
    try {
      data = await api('/admin/hs-requests');
    } catch (e) {
      clear(slot);
      slot.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          e.message || 'Could not load data requests.'))));
      return;
    }
    clear(slot);
    const rows = data.requests || [];
    const open = rows.filter((r) => r.status === 'open');

    const card = h('div', { class: 'asc-card asc-hs-bucket' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Data requests'),
        h('div', { class: 'asc-card-sub' },
          'What we have asked partners for. Everyone who has signed and can ' +
          'upload is emailed; several may answer and you approve what fits.'))));

    if (!open.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'Nothing open right now.')));
    }
    open.forEach((r) => {
      const body = h('div', { class: 'asc-card-pad asc-hs-request' });
      const d = r.delivery || {};
      body.appendChild(h('div', { class: 'asc-hs-request-head' },
        h('strong', {}, r.title),
        h('span', { class: 'asc-badge asc-badge-gray' }, r.specialty),
        h('span', { class: 'asc-dim', style: 'font-size:12px' },
          r.case_count + ' cases' +
          (r.due_date ? ' · by ' + r.due_date : '') +
          (r.created_at ? ' · asked ' + fmtDate(r.created_at) : ''))));
      if (r.details) {
        body.appendChild(h('div', { class: 'asc-hs-lead-message' }, r.details));
      }
      const deliveryLine = h('div', { class: 'asc-dim', style: 'font-size:12px' },
        'Emailed ' + (d.sent || 0) + ' · waiting to send ' + (d.pending || 0) +
        ' · failed ' + (d.failed || 0));
      if (d.failed > 0) {
        // Failed rows are otherwise terminal: re-broadcasting enqueues nothing
        // because every idempotency key already exists. This flips them back
        // to pending for the shared drain's next tick.
        const retryBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
                                       style: 'margin-left: var(--sp-1)' },
                           'Retry failed (' + d.failed + ')');
        retryBtn.addEventListener('click', async () => {
          retryBtn.disabled = true;
          try {
            const res = await api('/admin/hs-requests/' + encodeURIComponent(r.id) +
                                  '/retry-failed', { method: 'POST' });
            toast('Queued ' + (res.retried || 0) + ' to retry.', 'success');
            render(listContainer.parentNode, ctx);
          } catch (e) {
            retryBtn.disabled = false;
            toast(e.message || 'Could not retry those.', 'error');
          }
        });
        deliveryLine.appendChild(retryBtn);
      }
      body.appendChild(deliveryLine);

      const uploadsLine = h('div', { class: 'asc-dim', style: 'font-size:12px' }, 'Loading replies…');
      body.appendChild(uploadsLine);
      api('/admin/hs-requests/' + encodeURIComponent(r.id)).then((detail) => {
        const partners = (detail.responders || []).length;
        clear(uploadsLine);
        uploadsLine.appendChild(document.createTextNode(
          detail.uploads_count
            ? detail.uploads_count + ' upload' + (detail.uploads_count === 1 ? '' : 's') +
              ' from ' + partners + ' partner' + (partners === 1 ? '' : 's')
            : 'No uploads tagged with this request yet.'));
      }).catch(() => {
        clear(uploadsLine);
        uploadsLine.appendChild(document.createTextNode('Could not load replies.'));
      });

      const actions = h('div', { class: 'asc-hs-signup-actions' });
      ['fulfilled', 'withdrawn'].forEach((reason) => {
        actions.appendChild(btn(h, 'Close as ' + reason, 'asc-btn-subtle', async () => {
          try {
            await api('/admin/hs-requests/' + encodeURIComponent(r.id) + '/close',
                      { method: 'POST', body: { reason: reason } });
            toast('Closed.', 'success');
            render(listContainer.parentNode, ctx);
          } catch (e) { toast(e.message || 'Could not close that.', 'error'); }
        }));
      });
      body.appendChild(actions);
      card.appendChild(body);
    });

    // The compose form, at the bottom: an operator opening this page is far
    // more often checking on a request than writing a new one.
    const titleEl = requestField(h, 'ascReqTitle', 'What we need');
    const specialtyEl = requestField(h, 'ascReqSpecialty', 'Specialty');
    const countEl = requestField(h, 'ascReqCount', 'How many cases', { type: 'number', min: '1' });
    const dueEl = requestField(h, 'ascReqDue', 'Useful by', { type: 'date' });
    const detailsEl = h('textarea', { class: 'asc-input', rows: '3',
                                      placeholder: 'Anything else they should know' });
    const sendBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm' }, 'Send to every active partner');
    sendBtn.addEventListener('click', async () => {
      const count = parseInt(countEl.value, 10);
      if (!titleEl.value.trim() || !specialtyEl.value.trim()) {
        toast('A request needs a title and a specialty.', 'error');
        return;
      }
      if (!isFinite(count) || count <= 0) {
        toast('Ask for at least one case.', 'error');
        return;
      }
      try {
        const res = await api('/admin/hs-requests', { method: 'POST', body: {
          title: titleEl.value.trim(), specialty: specialtyEl.value.trim(),
          case_count: count, due_date: dueEl.value || '',
          details: (detailsEl.value || '').trim() } });
        toast('Queued for ' + (res.recipients || 0) + ' people.', 'success');
        render(listContainer.parentNode, ctx);
      } catch (e) { toast(e.message || 'Could not send that.', 'error'); }
    });
    card.appendChild(h('div', { class: 'asc-card-pad asc-hs-payout-form' },
      h('div', { class: 'asc-card-title' }, 'Ask for data'),
      h('div', { class: 'asc-hs-payout-grid' }, titleEl, specialtyEl, countEl, dueEl),
      detailsEl, sendBtn));

    slot.appendChild(card);
  }

  // ─── The onboarding demo video (Onboarding v2 §0.1) ───────
  // Swapping the demo is a recurring operation performed by the people who
  // RECORD it, not by whoever is nearest a terminal. A CLI is a fine second
  // door and a bad only one: it wants a checkout, a Python, and a password
  // typed into a shell, and the cost of all that is a stale video nobody
  // replaces. So the control lives here, next to the storage panel that says
  // whether the volume it lands on is durable.
  //
  // Deliberately NOT a generic asset browser: there is one slot, it has one
  // meaning, and the page should show what is in it and let you replace it.
  const DEMO_ACCEPT = 'video/mp4,video/webm,video/quicktime,.mp4,.webm,.mov';

  function humanBytes(n) {
    const v = Number(n) || 0;
    if (v < 1024) return v + ' B';
    if (v < 1024 * 1024) return (v / 1024).toFixed(0) + ' KB';
    if (v < 1024 * 1024 * 1024) return (v / (1024 * 1024)).toFixed(1) + ' MB';
    return (v / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
  }

  async function renderDemoVideoPanel(container, ctx) {
    const { h, api, clear, toast } = ctx;
    const card = h('div', { class: 'asc-card', style: 'margin-top: var(--sp-4)' });
    container.appendChild(card);
    const head = h('div', { class: 'asc-card-head' });
    const body = h('div', { class: 'asc-card-pad' });
    card.appendChild(head);
    card.appendChild(body);

    async function paint() {
      clear(head);
      clear(body);
      let meta = null;
      try {
        meta = await api('/assets/onboarding-demo/meta');
      } catch (e) {
        meta = null;
      }
      const installed = !!(meta && meta.available);
      // "Registered but its file is gone" is a THIRD state, and it is the one
      // that matters: it means the volume was wiped, not that nobody uploaded.
      const blobMissing = !!(meta && !meta.available && meta.reason === 'blob_missing');

      head.appendChild(h('div', {},
        h('div', { class: 'asc-card-title' }, 'Onboarding demo video',
          installed
            ? h('span', { class: 'asc-badge asc-badge-green', style: 'margin-left: var(--sp-2)' }, 'Live')
            : h('span', {
                class: 'asc-badge ' + (blobMissing ? 'asc-badge-red' : 'asc-badge-lime'),
                style: 'margin-left: var(--sp-2)',
              }, blobMissing ? 'File missing' : 'Not uploaded')),
        h('div', { class: 'asc-card-sub' },
          installed
            ? 'Physicians see this on the second stop of their first-login walkthrough. '
              + humanBytes(meta.byte_size) + ' · ' + (meta.mime || '')
            : blobMissing
              ? 'A demo is registered but its file is gone from the asset store — the '
                + 'volume was wiped. Upload it again.'
              : 'No demo uploaded yet. Until there is one, the walkthrough shows the '
                + 'practice case on its own rather than a card that plays nothing.')));

      const fileInput = h('input', { type: 'file', accept: DEMO_ACCEPT, style: 'display:none' });
      const status = h('div', { class: 'asc-dim', style: 'font-size:12px;margin-top:var(--sp-2)' });
      const bar = h('div', { class: 'asc-demo-bar' }, h('span', { class: 'asc-demo-bar-fill' }));
      const barWrap = h('div', { hidden: true }, bar);

      // XHR, not fetch: fetch cannot report upload progress, and a 73 MB upload
      // with no progress is one an operator abandons halfway convinced it hung.
      function upload(file) {
        if (!file) return;
        // Checked against the SERVER's number, so this can only ever refuse a
        // file the server would also refuse — and it refuses it before spending
        // ten minutes uploading it.
        if (maxBytes && file.size > maxBytes) {
          toast(file.name + ' is ' + humanBytes(file.size) + ', over the '
                + humanBytes(maxBytes) + ' limit. Compress it, or raise '
                + 'ASCLEPIUS_MEDIA_MAX_BYTES.', 'error');
          return;
        }
        status.textContent = 'Uploading ' + file.name + ' (' + humanBytes(file.size) + ')…';
        barWrap.removeAttribute('hidden');
        const form = new FormData();
        form.append('file', file, file.name);
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/asclepius/admin/assets/onboarding-demo');
        let token = null;
        try { token = localStorage.getItem('asclepius_token'); } catch (e) { token = null; }
        if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);
        xhr.upload.addEventListener('progress', (ev) => {
          if (!ev.lengthComputable) return;
          const pct = Math.round((ev.loaded / ev.total) * 100);
          bar.firstChild.style.width = pct + '%';
          status.textContent = 'Uploading… ' + pct + '%';
        });
        xhr.addEventListener('load', () => {
          barWrap.setAttribute('hidden', '');
          let res = null;
          try { res = JSON.parse(xhr.responseText || '{}'); } catch (e) { res = null; }
          if (xhr.status === 200) {
            toast('Demo video is live.', 'success');
            // The server warns about a .mov rather than refusing it: it stores
            // fine and plays everywhere except Firefox, so the operator gets to
            // decide. Surfaced as a toast rather than swallowed.
            if (res && res.warning) toast(res.warning, 'error');
            paint();
            return;
          }
          const detail = (res && (res.detail || res.message)) || ('HTTP ' + xhr.status);
          status.textContent = '';
          toast(typeof detail === 'string' ? detail : 'Upload failed.', 'error');
        });
        xhr.addEventListener('error', () => {
          barWrap.setAttribute('hidden', '');
          status.textContent = '';
          toast('Upload failed — check your connection and try again.', 'error');
        });
        xhr.send(form);
      }

      // A drop target AND a button. A drop-only zone is unreachable from a
      // keyboard and unusable on a tablet.
      // The cap comes from the SERVER (ASCLEPIUS_MEDIA_MAX_BYTES, 512 MB by
      // default). Stating a number this file made up would drift from the one
      // actually enforced, and the failure mode of that is telling an operator
      // their file is too big when it is not.
      const maxBytes = Number(meta && meta.max_upload_bytes) || 0;
      const zone = h('button', { class: 'asc-demo-drop', type: 'button' },
        h('div', { class: 'asc-demo-drop-lead' },
          installed ? 'Drop a new video here to replace it' : 'Drop your video here, or click to choose'),
        h('div', { class: 'asc-demo-drop-sub' },
          'MP4 (H.264 + AAC) plays everywhere. WebM and MOV are accepted; MOV does not play in Firefox.'
          + (maxBytes ? ' Up to ' + humanBytes(maxBytes) + '.' : '')));
      zone.addEventListener('click', () => fileInput.click());
      zone.addEventListener('dragover', (ev) => { ev.preventDefault(); zone.classList.add('is-over'); });
      zone.addEventListener('dragleave', () => zone.classList.remove('is-over'));
      zone.addEventListener('drop', (ev) => {
        ev.preventDefault();
        zone.classList.remove('is-over');
        upload(ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0]);
      });
      fileInput.addEventListener('change', () => {
        upload(fileInput.files && fileInput.files[0]);
        fileInput.value = '';
      });

      body.appendChild(zone);
      body.appendChild(fileInput);
      body.appendChild(barWrap);
      body.appendChild(status);

      if (installed) {
        // Watch what a physician will watch, from the page that replaced it —
        // "it uploaded" and "it plays" are different claims, and only the
        // second one matters.
        const preview = btn(h, 'Preview it', 'asc-btn-subtle', async () => {
          try {
            const t = await api('/assets/onboarding-demo/ticket', { method: 'POST' });
            window.open('/api/asclepius/assets/onboarding-demo?t='
                        + encodeURIComponent(t.ticket), '_blank', 'noopener');
          } catch (e) {
            toast(e.message || 'Could not open the demo.', 'error');
          }
        });
        body.appendChild(h('div', { style: 'margin-top: var(--sp-3)' }, preview));
      }
    }

    paint();
  }

  // ─── Self-signups awaiting a decision ─────────────────────
  async function renderPendingSignups(slot, ctx, listContainer) {
    const { h, api, clear, toast, fmtDate } = ctx;
    let rows;
    try {
      const res = await api('/admin/health-system-signups');
      rows = res.pending || [];
    } catch (e) {
      clear(slot);
      slot.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          e.message || 'Could not load pending signups.'))));
      return;
    }
    clear(slot);
    if (!rows.length) return;

    // Plain bucket, not the pink attention one. Pink in this palette means a
      // safety incident to resolve before anything else; a hospital waiting on
      // a decision is neither, and colouring it that way teaches the operator
      // to discount the colour when it does mean that. Position and the count
      // badge carry the urgency instead.
      const card = h('div', { class: 'asc-card asc-hs-bucket' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Waiting on you',
          h('span', { class: 'asc-badge asc-badge-count', style: 'margin-left: var(--sp-2)' },
            String(rows.length))),
        h('div', { class: 'asc-card-sub' },
          'Health systems that signed themselves up. They can see the portal ' +
          'and tell us about their data; they cannot upload until you approve.'))));

    rows.forEach((r) => {
      const body = h('div', { class: 'asc-card-pad asc-hs-signup' });

      body.appendChild(h('div', { class: 'asc-hs-signup-head' },
        h('strong', {}, r.organization || '(no name given)'),
        h('span', { style: 'margin-left: var(--sp-2)' }, stateChip(h, r.onboarding_state)),
        h('code', { class: 'asc-mono asc-dim', style: 'margin-left: var(--sp-2)' }, r.hs_id)));
      body.appendChild(h('div', { class: 'asc-dim' },
        (r.full_name || 'Someone') + ' · ' + (r.email || 'no email') +
        ' · signed up ' + (r.created_at ? fmtDate(r.created_at) : 'recently') +
        ' · signs in as ' + r.username));

      // The one thing an operator must not miss. Signup deliberately refuses to
      // merge by organization name, so a collision is either a second contact
      // at a partner we already have, or somebody who does not work at the
      // hospital whose name they typed. Only a person can tell those apart.
      (r.name_collisions || []).forEach((c) => {
        body.appendChild(h('div', { class: 'asc-inline-warn', style: 'margin-top: var(--sp-2)' },
          'Another health system already uses this name: ' + c.name + ' (' + c.hs_id +
          ', ' + c.uploads + ' uploads). This signup has its own id and cannot see ' +
          'their data. Check who this is before approving.'));
      });

      // THE FOUR ANSWERS, VERBATIM (PRD §4). The words they chose, in the
      // order they were asked, with the two answers that change what we are
      // allowed to do called out — an operator reading this decides whether a
      // BAA has to exist before a byte moves, and that answer must not be
      // something they have to go looking for.
      (r.applications || []).slice(0, 1).forEach((app) => {
        const dl = h('div', { class: 'asc-hs-intake' });
        (app.answers || []).forEach((a) => {
          dl.appendChild(h('div', { class: 'asc-hs-intake-row' },
            h('div', { class: 'asc-hs-intake-label' }, a.title),
            h('div', { class: 'asc-hs-intake-value' }, a.words || '—')));
        });
        if ((app.specialties || []).length) {
          dl.appendChild(h('div', { class: 'asc-hs-intake-row' },
            h('div', { class: 'asc-hs-intake-label' }, 'Specialties'),
            h('div', { class: 'asc-hs-intake-value' }, app.specialties.join(', '))));
        }
        body.appendChild(dl);
        if (app.needs_baa) {
          body.appendChild(h('div', { class: 'asc-inline-warn', style: 'margin-top: var(--sp-2)' },
            'They cannot de-identify on their side. A BAA has to be executed ' +
            'before any data moves — approving here does not create one.'));
        }
        if (app.authority_unclear) {
          body.appendChild(h('div', { class: 'asc-inline-warn', style: 'margin-top: var(--sp-2)' },
            'They are not sure they have the authority to license this data. ' +
            'That is a conversation before it is a signature.'));
        }
      });
      if (r.org_level && !(r.applications || []).length) {
        body.appendChild(h('div', { class: 'asc-dim', style: 'margin-top: var(--sp-2)' },
          'They have not submitted the four questions yet.'));
      }
      if ((r.members || []).length > 1) {
        body.appendChild(h('div', { class: 'asc-dim', style: 'margin-top: var(--sp-2)' },
          'Team: ' + r.members.map((m) => m.email || m.username).join(', ') +
          ' — all of them are emailed the agreement; one of them signs it.'));
      }

      (r.intake || []).slice(0, 1).forEach((entry) => {
        const dl = h('div', { class: 'asc-hs-intake' });
        INTAKE_LABELS.forEach((pair) => {
          const value = (entry.answers || {})[pair[0]];
          if (!value) return;
          dl.appendChild(h('div', { class: 'asc-hs-intake-row' },
            h('div', { class: 'asc-hs-intake-label' }, pair[1]),
            h('div', { class: 'asc-hs-intake-value' }, value)));
        });
        if (dl.childNodes.length) body.appendChild(dl);
      });
      if (!(r.intake || []).length) {
        body.appendChild(h('div', { class: 'asc-dim', style: 'margin-top: var(--sp-2)' },
          'They have not filled in the questions yet.'));
      }

      const actions = h('div', { class: 'asc-hs-signup-actions' });
      if (r.org_level) {
        // ONE Approve, for the whole organization. It approves every account on
        // it and mails all of them the agreement; one of them signs, and that
        // is what opens the upload door. No destination is chosen here on
        // purpose — accounts are minted with it unset so each upload is
        // resolved deliberately, on the per-upload control in the detail view.
        actions.appendChild(btn(h, 'Approve', 'asc-btn-primary', async () => {
          try {
            const res = await api('/admin/health-systems/' +
                                  encodeURIComponent(r.hs_id) + '/approve',
                                  { method: 'POST', body: {} });
            toast(r.organization + ' has been asked to sign. ' +
                  (res.emailed || 0) + ' invitation(s) sent.', 'success');
            render(listContainer.parentNode, ctx);
          } catch (e) {
            toast(e.message || 'Could not approve that.', 'error');
          }
        }));
        actions.appendChild(btn(h, 'Decline', 'asc-btn-ghost', async () => {
          const reason = window.prompt(
            'Why? Required, recorded on the row, and not sent to them: a ' +
            'refusal at this size is a conversation somebody has.');
          if (reason === null) return;
          if (!reason.trim()) {
            toast('A reason is required to decline.', 'error');
            return;
          }
          try {
            await api('/admin/health-systems/' + encodeURIComponent(r.hs_id) +
                      '/decline', { method: 'POST', body: { reason: reason } });
            toast('Recorded. No email was sent.', 'info');
            render(listContainer.parentNode, ctx);
          } catch (e) {
            toast(e.message || 'Could not record that.', 'error');
          }
        }));
      } else {
        // The pre-state-machine path, for an account on an organization that
        // predates it. Two approve buttons, one per destination, matching the
        // shape of the provision form so the operator's muscle memory
        // transfers. Approval is the only moment anyone is looking at one of
        // these, which is why it cannot be deferred.
        PURPOSES.forEach((p) => {
          actions.appendChild(btn(h, 'Approve · ' + p.label, 'asc-btn-primary', async () => {
            try {
              await api('/admin/health-systems/' + encodeURIComponent(r.hs_id) +
                        '/accounts/' + encodeURIComponent(r.username) + '/approve',
                        { method: 'POST', body: { purpose: p.key } });
              toast(r.organization + ' can upload now.', 'success');
              render(listContainer.parentNode, ctx);
            } catch (e) {
              toast(e.message || 'Could not approve that.', 'error');
            }
          }));
        });
        actions.appendChild(btn(h, 'Not a fit', 'asc-btn-ghost', async () => {
          const reason = window.prompt(
            'Why? Recorded on the account, not sent to them: a refusal at this ' +
            'size is a conversation somebody has.');
          if (reason === null) return;
          try {
            await api('/admin/health-systems/' + encodeURIComponent(r.hs_id) +
                      '/accounts/' + encodeURIComponent(r.username) + '/reject',
                      { method: 'POST', body: { reason: reason } });
            toast('Recorded. No email was sent.', 'info');
            render(listContainer.parentNode, ctx);
          } catch (e) {
            toast(e.message || 'Could not record that.', 'error');
          }
        }));
      }
      body.appendChild(actions);
      card.appendChild(body);
    });
    slot.appendChild(card);
  }

  // ─── Storage reconciliation (PRD I-0 §F4) ─────────────────
  // A non-zero missing count is an INCIDENT, not a metric: each one is a case
  // whose image 404s when a physician opens it, or that ships to a buyer as a
  // reference resolving to nothing. The healthy state says so explicitly —
  // an empty panel and a healthy panel must not look identical, or "nothing
  // shown" reads as "nothing wrong" whether or not the check even ran.
  async function renderStoragePanel(container, ctx) {
    const { h, api, fmtDate } = ctx;
    const card = h('div', { class: 'asc-card', style: 'margin-top: var(--sp-4)' });
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
    // Three states, not two. The badge used to read off `missing` alone, so a
    // deployment whose asset store was ephemeral showed a green OK directly above
    // the sentence "blobs will be lost on redeploy" — the panel contradicting
    // itself, with the reassuring half in the larger type. Data already gone is
    // red; data that WILL go is lime, which is what lime means everywhere else in
    // this palette; green is reserved for when neither is true.
    const badge = missing
      ? h('span', { class: 'asc-badge asc-badge-red', style: 'margin-left: var(--sp-2)' },
          missing + ' missing')
      : nonDurable.length
        ? h('span', { class: 'asc-badge asc-badge-lime', style: 'margin-left: var(--sp-2)' },
            'Not durable')
        : h('span', { class: 'asc-badge asc-badge-green', style: 'margin-left: var(--sp-2)' }, 'OK');
    const sub = missing
      ? missing + ' asset reference' + (missing === 1 ? '' : 's') +
        ' point at blobs that are gone from disk. This is data loss, not a warning.'
      : nonDurable.length
        ? nonDurable.length + ' of ' + (rep.storage || []).length + ' stores will not '
          + 'survive a redeploy. Nothing is lost yet; everything below will be.'
        : 'All ' + (rep.n_rows || 0) + ' asset reference' +
          ((rep.n_rows || 0) === 1 ? '' : 's') +
          ((rep.n_rows || 0) === 1 ? ' resolves. ' : ' resolve. ') +
          (rep.orphan_count || 0) + ' unreferenced blob' +
          ((rep.orphan_count || 0) === 1 ? '' : 's') + ' on disk (reported, never deleted).';
    card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
      h('div', { class: 'asc-card-title' }, 'Storage integrity', badge),
      h('div', { class: 'asc-card-sub' }, sub))));
    const body = h('div', { class: 'asc-card-pad' });
    // Every store, durable or not. Listing only the failures means the panel is
    // blank when things are fine, which reads as "not checked" rather than "safe"
    // — and leaves an operator asking "so WHERE does the demo video live?" with
    // nowhere on the page to look. The detail line names the resolved path.
    (rep.storage || []).forEach((s) => body.appendChild(
      h('div', { class: s.durable ? 'asc-hs-reason asc-hs-reason-ok' : 'asc-hs-reason' },
        h('strong', {}, s.store), ' — ', s.detail)));
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
    // The default landing place, and the operator's real queue. Download it,
    // read it, then say what it is for on the row — the controls are in the
    // Destination column. No Promote button, because there is nothing to
    // promote until that decision is made.
    { key: 'storage', title: 'Held in storage',
      sub: 'Received and stored, used for nothing. Everything arrives here. '
           + 'Read the file, then set what it is for on the row — task creation '
           + 'opens the promote controls, brokering routes it out of this '
           + 'workflow entirely.',
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

    renderApplicationCard(container, ctx, data);
    renderAgreementsCard(container, ctx, data);
    renderIntakeCard(container, ctx, data);
    // Above Payouts and Invoices deliberately: the rate is the input those two
    // are downstream of, and an operator looking at an empty ledger should meet
    // the reason before the symptom.
    renderDataRateCard(container, ctx, hsId);
    renderPayoutsCard(container, ctx, hsId, data);
    renderInvoicesCard(container, ctx, hsId, data);

    // The buckets, in workflow order. Needs attention renders only when
    // non-empty — but ALWAYS above the rest when it exists.
    BUCKETS.forEach((spec) => {
      const items = buckets[spec.key] || [];
      if (spec.key === 'needs_attention' && !items.length) return;
      const card = h('div', { class: 'asc-card asc-hs-bucket ' + spec.cls },
        h('div', { class: 'asc-card-head' }, h('div', {},
          h('div', { class: 'asc-card-title' }, spec.title,
            h('span', { class: 'asc-badge asc-badge-count', style: 'margin-left: var(--sp-2)' }, String(items.length))),
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

  // Set the destination on a row that still needs one. NULL means nobody was
  // ever asked — a row from before the column had a default — and it now behaves
  // exactly like storage: held, promotable by nothing, waiting on a person. The
  // control is here because this is where the operator is looking.
  function purposeResolver(ctx, hsId, username, container) {
    const { h, api, toast } = ctx;
    const wrap = h('span', { style: 'margin-left: var(--sp-2); white-space: nowrap' });
    [['task_creation', 'Task creation'], ['brokering', 'Brokering']].forEach((pair) => {
      const b = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm',
                              style: 'margin-left: var(--sp-1)' }, 'Set: ' + pair[1]);
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
    const wrap = h('span', { style: 'margin-left: var(--sp-2); white-space: nowrap' });
    [['task_creation', 'Task creation'], ['brokering', 'Brokering']].forEach((pair) => {
      const b = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm',
                              style: 'margin-left: var(--sp-1)' }, 'Set: ' + pair[1]);
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

  const SPECIALTY_BLOCK_FALLBACK =
    'Specialty not set — choose one to promote.';

  function bucketRow(ctx, spec, it, hsId, container) {
    const { h, toast, downloadBlob, fmtDate, openPipeline, specialtyResolver } = ctx;
    // asclepius.js owns the wording so both admin surfaces say it identically;
    // the fallback only covers a stale cached copy of that file.
    const blockReason = ctx.specialtyBlockReason || SPECIALTY_BLOCK_FALLBACK;
    const actions = [];
    actions.push(btn(h, 'Download', 'asc-btn-subtle', () =>
      downloadBlob('/ingestion/uploads/' + it.upload_id + '/download',
                   it.filename || (it.upload_id + '.zip'))));
    if (spec.actions.includes('review')) {
      actions.push(btn(h, 'Review', 'asc-btn-primary', () => openPipeline(it)));
    }
    // Ingest refuses to guess a specialty, so a hospital-portal upload arrives
    // with none and the promote endpoints 409 on it. This row sent the operator
    // into that 409 with a live-looking button and a message naming a control
    // that had no frontend caller. The button is now dead until the specialty is
    // set, and the control that sets it is right here.
    const needsSpecialty = spec.actions.includes('promote')
      && (it.specialty_undetermined_cases || 0) > 0;
    if (spec.actions.includes('promote')) {
      if (needsSpecialty) {
        actions.push(h('button', {
          class: 'asc-btn asc-btn-sm asc-btn-primary',
          style: 'margin-right: var(--sp-1)',
          disabled: true,
          title: blockReason,
        }, 'Promote to task'));
      } else {
        actions.push(btn(h, 'Promote to task', 'asc-btn-primary', () => openPipeline(it)));
      }
    }
    const notes = [];
    (it.reasons || []).forEach((r) => notes.push(h('div', { class: 'asc-hs-reason' }, r)));
    if (!notes.length && it.note) notes.push(h('div', { class: 'asc-dim' }, it.note));
    if (needsSpecialty && specialtyResolver) {
      // Stated, not implied: "Nothing here right now" and "there is one thing
      // left to decide" must never look the same to an operator.
      notes.push(h('div', { class: 'asc-promote-block' },
        h('div', { class: 'asc-promote-block-why' }, blockReason),
        specialtyResolver(it.upload_id, () => renderDetail(container, ctx, hsId))));
    }
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
        // The server decides whether this row still needs a person; the UI does
        // not re-derive it. `resolved` is about whether a VALUE is set, which is
        // a different question now that the default value is a real one.
        it.needs_decision ? uploadPurposeResolver(ctx, it.upload_id, hsId, container) : ''),
      h('td', {}, caseCountText(it), specialtyNote(h, it)),
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

  // What these cases are labeled as — or, honestly, that nothing has decided yet.
  // "Not yet determined" is a work item, so it renders lime like every other
  // unresolved decision on this screen, not as a specialty nobody chose.
  function specialtyNote(h, it) {
    const named = it.specialties || [];
    if (named.length) {
      return h('div', { class: 'asc-dim', style: 'font-size:11px' }, named.join(', '));
    }
    if (!(it.specialty_undetermined_cases || 0)) return null;
    return h('div', { style: 'margin-top:4px' },
      h('span', { class: 'asc-badge asc-badge-lime' }, 'Specialty not set'));
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
    const b = h('button', { class: 'asc-btn asc-btn-sm ' + cls, style: 'margin-right: var(--sp-1)' }, label);
    b.addEventListener('click', onClick);
    return b;
  }

  // ─── The application: the four answers, verbatim ──────────
  function renderApplicationCard(container, ctx, data) {
    const { h, fmtDate } = ctx;
    const entries = data.applications || [];
    if (!entries.length) return;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Their application',
          h('span', { style: 'margin-left: var(--sp-2)' },
            stateChip(h, data.onboarding_state))),
        h('div', { class: 'asc-card-sub' },
          'The four questions, in the order they were asked, in the words they ' +
          'chose. Newest first; every submission is kept.'))));
    entries.forEach((app, i) => {
      const body = h('div', { class: 'asc-card-pad' });
      body.appendChild(h('div', { class: 'asc-dim' },
        (i === 0 ? 'Latest · ' : '') +
        (app.submitted_at ? fmtDate(app.submitted_at) : '') +
        (app.username ? ' · answered by ' + app.username : '')));
      const dl = h('div', { class: 'asc-hs-intake' });
      (app.answers || []).forEach((a) => {
        dl.appendChild(h('div', { class: 'asc-hs-intake-row' },
          h('div', { class: 'asc-hs-intake-label' }, a.title),
          h('div', { class: 'asc-hs-intake-value' }, a.words || '—')));
      });
      if ((app.specialties || []).length) {
        dl.appendChild(h('div', { class: 'asc-hs-intake-row' },
          h('div', { class: 'asc-hs-intake-label' }, 'Specialties'),
          h('div', { class: 'asc-hs-intake-value' }, app.specialties.join(', '))));
      }
      body.appendChild(dl);
      if (i === 0 && app.needs_baa) {
        body.appendChild(h('div', { class: 'asc-inline-warn' },
          'They cannot de-identify on their side. A BAA has to be executed ' +
          'before any data moves.'));
      }
      card.appendChild(body);
    });
    container.appendChild(card);
  }

  // ─── Signed agreements: the evidence ──────────────────────
  // Every signature, with the whole E-SIGN record and a download. Nothing here
  // can be edited, and there is no control that would try: the row is
  // append-only in the database, and a UI offering an edit that the database
  // refuses is a UI teaching an operator that the record is negotiable.
  function renderAgreementsCard(container, ctx, data) {
    const { h, fmtDate } = ctx;
    const rows = data.agreements || [];
    const state = data.onboarding_state;
    // Rendered when there is something to say: a signature, or an organization
    // that is supposed to have one and does not.
    if (!rows.length && state !== 'approved_awaiting_dla') return;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Data licensing agreement'),
        h('div', { class: 'asc-card-sub' },
          rows.length
            ? 'Signed. These rows are append-only — a newer version is a new ' +
              'row, and nothing here is ever rewritten.'
            : 'Approved and waiting on a signature. Any member of this ' +
              'organization can sign; uploading opens when one of them does.'))));
    if (!rows.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'Nothing signed yet.')));
      container.appendChild(card);
      return;
    }
    rows.forEach((r) => {
      const body = h('div', { class: 'asc-card-pad' });
      body.appendChild(h('div', {},
        h('strong', {}, r.typed_name || ''),
        h('span', { class: 'asc-dim' },
          (r.typed_title ? ', ' + r.typed_title : '') +
          ' · ' + (r.signed_at ? fmtDate(r.signed_at) : '') +
          ' · version ' + (r.doc_version || ''))));
      body.appendChild(h('div', { class: 'asc-dim', style: 'font-size:12px' },
        'Signed in as ' + (r.signer_user_id || '') +
        ' (' + (r.signer_email || 'no address') + ') from ' + (r.ip || 'unknown') +
        ' · ' + (r.consent_esign ? 'E-SIGN consent recorded' : 'NO E-SIGN CONSENT') +
        ' · ' + (r.authority_affirmed ? 'authority affirmed' : 'NO AUTHORITY AFFIRMATION')));
      body.appendChild(h('div', { class: 'asc-hs-reason' },
        h('code', { class: 'asc-mono' }, 'doc ' + String(r.doc_sha256 || '').slice(0, 24) + '…')));
      const link = h('a', { class: 'asc-btn asc-btn-subtle asc-btn-sm',
                            href: r.download_url, target: '_blank', rel: 'noopener' },
                     'Download the signed PDF');
      body.appendChild(link);
      card.appendChild(body);
    });
    container.appendChild(card);
  }

  // ─── What they told us ────────────────────────────────────
  function renderIntakeCard(container, ctx, data) {
    const { h, fmtDate } = ctx;
    const entries = data.intake || [];
    // Absent for every organization we provisioned by hand, and a card reading
    // "nothing" for all of them would be noise on most of this page.
    if (!entries.length) return;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'What they told us'),
        h('div', { class: 'asc-card-sub' },
          'Their own words, newest first. Appended, never overwritten.'))));
    entries.forEach((entry, i) => {
      const body = h('div', { class: 'asc-card-pad' });
      body.appendChild(h('div', { class: 'asc-dim' },
        (i === 0 ? 'Latest · ' : '') +
        (entry.submitted_at ? fmtDate(entry.submitted_at) : '')));
      const dl = h('div', { class: 'asc-hs-intake' });
      INTAKE_LABELS.forEach((pair) => {
        const value = (entry.answers || {})[pair[0]];
        if (!value) return;
        dl.appendChild(h('div', { class: 'asc-hs-intake-row' },
          h('div', { class: 'asc-hs-intake-label' }, pair[1]),
          h('div', { class: 'asc-hs-intake-value' }, value)));
      });
      body.appendChild(dl);
      card.appendChild(body);
    });
    container.appendChild(card);
  }

  // ─── Payouts ──────────────────────────────────────────────
  function money(cents) {
    const n = (Number(cents) || 0) / 100;
    return '$' + n.toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  // ─── Data rate ────────────────────────────────────────────
  // What one accepted upload from this partner is worth. Until this card
  // existed the only way to price an organization was to POST
  // /health-systems/{id}/data-rate by hand, and the deployment default
  // (ASCLEPIUS_HS_UPLOAD_RATE_CENTS) is 0, so an unpriced partner accrues
  // NOTHING and the Invoices card below it stays empty forever with no
  // indication of why. The price is the missing input, so it belongs on the
  // page where the money is.
  //
  // Its own fetch, because the rate lives on GET .../accruals and not on the
  // detail response this view is built from. Appended synchronously and filled
  // afterwards, exactly as renderStoragePanel does, so a slow read cannot
  // reorder the cards or hold the page open.
  function renderDataRateCard(container, ctx, hsId) {
    const { h, api, toast } = ctx;
    const card = h('div', { class: 'asc-card' });
    card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
      h('div', { class: 'asc-card-title' }, 'Data rate'),
      h('div', { class: 'asc-card-sub' },
        'What one accepted upload from this organization is worth. Setting it ' +
        'reconciles their backlog immediately; nothing already accrued moves, ' +
        'because every ledger row carries the rate it was stamped with.'))));
    const bodyEl = h('div', { class: 'asc-card-pad' });
    card.appendChild(bodyEl);
    container.appendChild(card);

    api('/admin/health-systems/' + encodeURIComponent(hsId) + '/accruals')
      .then((res) => {
        // The EFFECTIVE rate, which is this organization's own figure when it
        // has one and the deployment default when it does not. Hence "in force"
        // rather than "agreed": the label must not claim somebody priced this
        // partner when what is really in force is a default.
        const rate = Number(res.rate_cents) || 0;
        const sum = res.summary || {};
        bodyEl.appendChild(h('div', { class: 'asc-hs-meta' },
          metaCell(h, 'Rate in force',
            rate ? money(rate) + ' / ' + (res.unit || 'upload') : 'Not priced'),
          metaCell(h, 'Accrued, not billed', money(sum.accrued_cents)),
          metaCell(h, 'Outstanding', money(sum.outstanding_cents)),
          metaCell(h, 'Ledger rows', String(sum.count || 0))));
        // An unpriced partner is a work item, not a default. Say what the zero
        // actually costs: their accepted uploads are earning them nothing.
        if (!rate) {
          bodyEl.appendChild(h('div', { class: 'asc-inline-warn', style: 'margin-top: var(--sp-2)' },
            'This organization is not priced, so accepted uploads accrue nothing ' +
            'at all. Agree a rate here before their data is worth anything on ' +
            'the ledger.'));
        }

        const amountEl = h('input', { class: 'asc-input', type: 'text',
                                      placeholder: 'Rate per accepted upload, e.g. 250.00' });
        const setBtn = btn(h, 'Set rate', 'asc-btn-primary', async () => {
          const dollars = parseFloat((amountEl.value || '').replace(/[$,\s]/g, ''));
          if (!isFinite(dollars) || dollars < 0) {
            toast('Enter a rate of zero or more.', 'error');
            return;
          }
          try {
            const out = await api('/admin/health-systems/' + encodeURIComponent(hsId) +
                                  '/data-rate',
                                  { method: 'POST',
                                    body: { rate_cents: Math.round(dollars * 100) } });
            const accrued = (out.reconciled || {}).accrued || 0;
            toast('Rate set. ' + accrued + ' upload(s) accrued at it.', 'success');
            render(container.parentNode, ctx);
          } catch (e) { toast(e.message || 'Could not set that rate.', 'error'); }
        });
        // Clearing is not the same as setting zero: it drops the organization
        // back to the deployment default, which is what an operator who priced
        // the wrong partner needs. It is a ghost button because unpricing a
        // partner is not the thing this card is for.
        const clearBtn = btn(h, 'Clear', 'asc-btn-ghost', async () => {
          if (!window.confirm('Clear this rate? Their accepted uploads fall back ' +
                              'to the deployment default, which accrues nothing ' +
                              'unless one is configured.')) return;
          try {
            await api('/admin/health-systems/' + encodeURIComponent(hsId) + '/data-rate',
                      { method: 'POST', body: { rate_cents: null } });
            toast('Rate cleared.', 'info');
            render(container.parentNode, ctx);
          } catch (e) { toast(e.message || 'Could not clear that rate.', 'error'); }
        });
        bodyEl.appendChild(h('div', { class: 'asc-hs-payout-form' },
          h('div', { class: 'asc-hs-payout-grid' }, amountEl),
          setBtn, rate ? clearBtn : null));
      })
      .catch((e) => {
        bodyEl.appendChild(h('div', { class: 'asc-inline-error' },
          e.message || 'Could not read this organization\u2019s rate.'));
      });
  }

  function renderPayoutsCard(container, ctx, hsId, data) {
    const { h, api, toast, fmtDate } = ctx;
    const rows = data.payouts || [];
    const sum = data.payouts_summary || {};

    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Payouts'),
        h('div', { class: 'asc-card-sub' },
          'What we have paid this organization. Recording here does not move ' +
          'money; it records that we consider it settled, and the partner sees it.'))),
      h('div', { class: 'asc-card-pad asc-hs-meta' },
        metaCell(h, 'Total recorded', money(sum.total_cents)),
        metaCell(h, 'Paid', money(sum.paid_cents)),
        metaCell(h, 'Awaiting payment', money(sum.pending_cents))));

    if (rows.length) {
      card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Recorded'), h('th', {}, 'For'), h('th', {}, 'Reference'),
          h('th', {}, 'Status'), h('th', {}, 'Amount'), h('th', {}, ''))),
        h('tbody', {}, rows.map((p) => {
          const actions = h('td', {});
          if (p.status !== 'void') {
            if (!p.paid_at) {
              actions.appendChild(btn(h, 'Mark paid', 'asc-btn-subtle', async () => {
                const ref = window.prompt('Transfer reference (optional):') || '';
                try {
                  await api('/admin/health-systems/' + encodeURIComponent(hsId) +
                            '/payouts/' + encodeURIComponent(p.payout_id) + '/mark-paid',
                            { method: 'POST', body: { payout_batch_id: ref } });
                  toast('Marked paid.', 'success');
                  render(container.parentNode, ctx);
                } catch (e) { toast(e.message || 'Could not update that.', 'error'); }
              }));
            }
            actions.appendChild(btn(h, 'Cancel', 'asc-btn-ghost', async () => {
              const reason = window.prompt('Why is this being cancelled?');
              if (!reason) return;
              try {
                await api('/admin/health-systems/' + encodeURIComponent(hsId) +
                          '/payouts/' + encodeURIComponent(p.payout_id) + '/void',
                          { method: 'POST', body: { reason: reason } });
                toast('Cancelled.', 'info');
                render(container.parentNode, ctx);
              } catch (e) { toast(e.message || 'Could not cancel that.', 'error'); }
            }));
          }
          const badgeCls = p.status === 'paid' ? 'asc-badge-green'
            : p.status === 'void' ? 'asc-badge-gray' : 'asc-badge-amber';
          return h('tr', {},
            h('td', {}, p.recorded_at ? fmtDate(p.recorded_at) : '—'),
            h('td', {}, p.description || '—'),
            h('td', {}, h('code', { class: 'asc-mono asc-dim' }, p.external_ref || '—')),
            h('td', {}, h('span', { class: 'asc-badge ' + badgeCls }, p.status),
              p.void_reason ? h('div', { class: 'asc-dim' }, p.void_reason) : ''),
            h('td', {}, h('span', { class: 'asc-mono' }, money(p.amount_cents))),
            actions);
        })))));
    } else {
      card.appendChild(h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'Nothing recorded yet.')));
    }

    // Record form. external_ref is the idempotency key, so a double-clicked
    // button records once rather than paying a hospital twice.
    const amountEl = h('input', { class: 'asc-input', type: 'text',
                                  placeholder: 'Amount, e.g. 25000.00' });
    const descEl = h('input', { class: 'asc-input', type: 'text',
                                placeholder: 'What it is for, in words they will read' });
    const refEl = h('input', { class: 'asc-input', type: 'text',
                               placeholder: 'Invoice or transfer reference' });
    const startEl = h('input', { class: 'asc-input', type: 'text', placeholder: 'Period start' });
    const endEl = h('input', { class: 'asc-input', type: 'text', placeholder: 'Period end' });
    const recordBtn = btn(h, 'Record payout', 'asc-btn-primary', async () => {
      const dollars = parseFloat((amountEl.value || '').replace(/[$,\s]/g, ''));
      if (!isFinite(dollars) || dollars <= 0) {
        toast('Enter an amount greater than zero.', 'error');
        return;
      }
      if (!(refEl.value || '').trim()) {
        toast('Give it a reference, so recording it twice cannot pay twice.', 'error');
        return;
      }
      try {
        await api('/admin/health-systems/' + encodeURIComponent(hsId) + '/payouts',
                  { method: 'POST', body: {
                    amount_cents: Math.round(dollars * 100),
                    external_ref: refEl.value.trim(),
                    description: (descEl.value || '').trim() || null,
                    period_start: (startEl.value || '').trim() || null,
                    period_end: (endEl.value || '').trim() || null } });
        toast('Recorded. They can see it now.', 'success');
        render(container.parentNode, ctx);
      } catch (e) { toast(e.message || 'Could not record that.', 'error'); }
    });
    card.appendChild(h('div', { class: 'asc-card-pad asc-hs-payout-form' },
      h('div', { class: 'asc-card-title' }, 'Record a payment'),
      h('div', { class: 'asc-hs-payout-grid' },
        amountEl, refEl, descEl, startEl, endEl),
      recordBtn));

    container.appendChild(card);
  }

  // ─── Invoices ─────────────────────────────────────────────
  // What we have BILLED, as distinct from what we have PAID them below. The
  // status is an operator's statement of fact — nothing in this release can
  // observe that money arrived, and nothing here calls a payment processor.
  // When a rail is wired it is wired behind these same three endpoints and the
  // meaning of `paid` does not change.
  const INVOICE_FLOW = { draft: 'sent', sent: 'paid' };

  function renderInvoicesCard(container, ctx, hsId, data) {
    const { h, api, toast, fmtDate } = ctx;
    const rows = data.invoices || [];
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Invoices'),
        h('div', { class: 'asc-card-sub' },
          'One per period, per organization. Amounts come from their ' +
          'agreement\u2019s Schedule A.'))));

    if (rows.length) {
      card.appendChild(h('div', { class: 'asc-table-wrap' },
        h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Period'), h('th', {}, 'For'), h('th', {}, 'Status'),
            h('th', {}, 'Amount'), h('th', {}, ''))),
          h('tbody', {}, rows.map((inv) => {
            const actions = h('td', {});
            const nextStatus = INVOICE_FLOW[inv.status];
            if (nextStatus) {
              actions.appendChild(btn(h, 'Mark ' + nextStatus, 'asc-btn-subtle', async () => {
                try {
                  await api('/admin/health-systems/' + encodeURIComponent(hsId) +
                            '/invoices/' + encodeURIComponent(inv.invoice_id) + '/status',
                            { method: 'POST', body: { status: nextStatus } });
                  toast('Marked ' + nextStatus + '.', 'success');
                  render(container.parentNode, ctx);
                } catch (e) { toast(e.message || 'Could not update that.', 'error'); }
              }));
            }
            const badgeCls = inv.status === 'paid' ? 'asc-badge-green'
              : inv.status === 'sent' ? 'asc-badge-amber' : 'asc-badge-gray';
            return h('tr', {},
              h('td', {}, h('code', { class: 'asc-mono' }, inv.period || '—')),
              h('td', {}, inv.description || '—'),
              h('td', {}, h('span', { class: 'asc-badge ' + badgeCls }, inv.status)),
              h('td', {}, h('span', { class: 'asc-mono' }, money(inv.amount_cents))),
              actions);
          })))));
    } else {
      card.appendChild(h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'No invoices for this organization yet.')));
    }

    const periodEl = h('input', { class: 'asc-input', type: 'text',
                                  placeholder: 'Period, e.g. 2026-Q1' });
    const amountEl = h('input', { class: 'asc-input', type: 'text',
                                  placeholder: 'Amount, e.g. 25000.00' });
    const descEl = h('input', { class: 'asc-input', type: 'text',
                                placeholder: 'What it is for, in words they will read' });
    const createBtn = btn(h, 'Draft invoice', 'asc-btn-primary', async () => {
      const dollars = parseFloat((amountEl.value || '').replace(/[$,\s]/g, ''));
      if (!(periodEl.value || '').trim()) {
        toast('Give it a period. One invoice per period, per organization.', 'error');
        return;
      }
      if (!isFinite(dollars) || dollars <= 0) {
        toast('Enter an amount greater than zero.', 'error');
        return;
      }
      try {
        await api('/admin/health-systems/' + encodeURIComponent(hsId) + '/invoices',
                  { method: 'POST', body: {
                    period: periodEl.value.trim(),
                    amount_cents: Math.round(dollars * 100),
                    description: (descEl.value || '').trim() || null } });
        toast('Drafted.', 'success');
        render(container.parentNode, ctx);
      } catch (e) { toast(e.message || 'Could not draft that.', 'error'); }
    });
    card.appendChild(h('div', { class: 'asc-card-pad asc-hs-payout-form' },
      h('div', { class: 'asc-card-title' }, 'Draft an invoice'),
      h('div', { class: 'asc-hs-payout-grid' }, periodEl, amountEl, descEl),
      createBtn));

    container.appendChild(card);
  }

  window.AdminHealthSection = {
    render,
    reset() { selectedHsId = null; },
    open(hsId) { selectedHsId = hsId || null; },
  };
})();
