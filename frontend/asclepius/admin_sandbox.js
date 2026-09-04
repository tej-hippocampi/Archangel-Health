/*
   Admin · Sandbox section (Sandbox PRD §3.2 Accounts, §3.3 Outbox, §4 copy)

   Mounted by admin_shell.js as the fifth admin tab, and ONLY when the page is
   the sandbox realm (window.__REALM === 'sandbox'). The live console never
   shows it. Same design system, same `ctx` contract as the other sections
   (admin_health.js et al.): { h, api, clear, toast, loadingCard, fmtDate,
   copyableId, copyTextToClipboard }.

   Accounts: the ten seeded physicians with their shared password (copy
   buttons — the password is fetched from the server, never in this file),
   the sandbox admin's own login, the fake-onboarding entry links, `Seed
   sandbox`, `Seed fresh doctor`, and `Reset sandbox` behind a typed
   confirmation. Snapshot copy (§4) lives here too: a live health system is
   copied read-only into the sandbox so task creation and routing can be
   re-run from raw data.

   Outbox: every email the sandbox "sent" — to, subject, the OTP code / magic
   link / DLA link extracted and clickable in place, and the rendered HTML in
   a sandboxed iframe. Walking a fake onboarding end to end never leaves the
   product.
*/
(function () {
  'use strict';

  const SANDBOX_API = '/sandbox';   // relative to ctx.api's /api/asclepius base
  let selectedMessageId = null;

  function render(body, ctx, sub) {
    const { h, clear } = ctx;
    clear(body);
    const container = h('div', {});
    body.appendChild(container);
    if (sub === 'outbox') renderOutbox(container, ctx);
    else renderAccounts(container, ctx);
  }

  // ─── helpers ───────────────────────────────────────────────
  function copyBtn(ctx, value, label) {
    const { h, toast, copyTextToClipboard } = ctx;
    const btn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button' }, label || 'Copy');
    btn.addEventListener('click', async () => {
      try { await copyTextToClipboard(value); toast('Copied.', 'ok'); }
      catch (e) { toast('Could not copy.', 'error'); }
    });
    return btn;
  }

  function errorCard(ctx, msg) {
    const { h } = ctx;
    return h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-inline-error' }, msg)));
  }

  // ─── Accounts (§3.2) ───────────────────────────────────────
  async function renderAccounts(container, ctx) {
    const { h, api, clear, toast, loadingCard } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading sandbox accounts…'));
    let data;
    try {
      data = await api(SANDBOX_API + '/accounts');
    } catch (e) {
      clear(container);
      container.appendChild(errorCard(ctx, e.message || 'Could not load the sandbox accounts.'));
      return;
    }
    clear(container);

    // ── Who you are, and the doors into the sandbox
    const links = data.links || {};
    const doors = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Sandbox admin'),
        h('div', { class: 'asc-card-sub' },
          'Every account below exists only in the sandbox databases. The same credentials without ' +
          '?realm=sandbox are a plain 401 on the live site, by construction, not by filtering.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-row', style: 'align-items:center; gap: var(--sp-2); flex-wrap: wrap' },
          h('code', { class: 'asc-mono' }, data.admin.email), copyBtn(ctx, data.admin.email, 'Copy email'),
          h('code', { class: 'asc-mono' }, '••••••••'), copyBtn(ctx, data.admin.password || '', 'Copy password')),
        h('div', { class: 'asc-card-sub', style: 'margin-top: var(--sp-3)' }, 'Fake onboarding entries (§5): the real flows, in the sandbox realm:'),
        h('div', { class: 'asc-row', style: 'gap: var(--sp-2); flex-wrap: wrap; margin-top: var(--sp-2)' },
          h('a', { class: 'asc-btn asc-btn-primary asc-btn-sm', href: links.physician_onboarding, target: '_blank', rel: 'noopener' }, 'Start a fake physician onboarding →'),
          h('a', { class: 'asc-btn asc-btn-primary asc-btn-sm', href: links.org_onboarding, target: '_blank', rel: 'noopener' }, 'Start a fake org onboarding →'),
          h('a', { class: 'asc-btn asc-btn-subtle asc-btn-sm', href: links.sign_in, target: '_blank', rel: 'noopener' }, 'Landing sign-in (sandbox)'),
          h('a', { class: 'asc-btn asc-btn-subtle asc-btn-sm', href: links.provider, target: '_blank', rel: 'noopener' }, 'Provider portal'),
          h('a', { class: 'asc-btn asc-btn-subtle asc-btn-sm', href: links.buyer, target: '_blank', rel: 'noopener' }, 'Buyer workspace'),
          h('a', { class: 'asc-btn asc-btn-subtle asc-btn-sm', href: links.community, target: '_blank', rel: 'noopener' }, 'Community'))));
    container.appendChild(doors);

    // ── The ten physicians
    const rows = data.physicians || [];
    const seededCount = rows.filter((r) => r.seeded).length;
    const seedBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button' },
      seededCount ? 'Re-seed (idempotent)' : 'Seed sandbox');
    seedBtn.addEventListener('click', async () => {
      seedBtn.disabled = true;
      try {
        const res = await api(SANDBOX_API + '/seed', { method: 'POST' });
        toast('Seeded ' + (res.physicians || []).length + ' physicians.', 'ok');
        renderAccounts(container, ctx);
      } catch (e) { toast(e.message || 'Seed failed.', 'error'); seedBtn.disabled = false; }
    });
    const freshBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button' }, 'Seed fresh doctor');
    freshBtn.addEventListener('click', async () => {
      freshBtn.disabled = true;
      try {
        const res = await api(SANDBOX_API + '/accounts/fresh', { method: 'POST' });
        toast('Fresh doctor: ' + res.email + ' (walkthrough not started).', 'ok');
        renderAccounts(container, ctx);
      } catch (e) { toast(e.message || 'Could not seed a fresh doctor.', 'error'); freshBtn.disabled = false; }
    });

    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Physicians'),
        h('div', { class: 'asc-card-sub' },
          'Ten fake doctors, specialty-spread so routing is testable. One shared password' +
          (data.doctor_password_set ? '.' : ', NOT SET: add ASCLEPIUS_SANDBOX_DOCTOR_PASSWORD to seed them.'))),
        h('div', { class: 'asc-row', style: 'gap: var(--sp-2)' }, seedBtn, freshBtn)));
    if (!rows.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-empty' }, 'Not seeded yet.')));
    } else {
      const table = h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, '#'), h('th', {}, 'Name'), h('th', {}, 'Email'), h('th', {}, 'Tier'),
          h('th', {}, 'Specialty'), h('th', {}, 'Password'), h('th', {}, 'State'), h('th', {}, ''))),
        h('tbody', {}, rows.map((r) => h('tr', {},
          h('td', {}, r.n == null ? '-' : String(r.n)),
          h('td', {}, h('strong', {}, r.name)),
          h('td', {}, h('code', { class: 'asc-mono' }, r.email), ' ', copyBtn(ctx, r.email, 'Copy')),
          h('td', {}, r.tier || '-'),
          h('td', {}, r.specialty || '-'),
          h('td', {}, r.password ? copyBtn(ctx, r.password, 'Copy password') : h('span', { class: 'asc-dim' }, 'unset')),
          h('td', {}, !r.seeded
            ? h('span', { class: 'asc-badge asc-badge-gray' }, 'not seeded')
            : (r.onboarded
              ? h('span', { class: 'asc-badge asc-badge-green' }, 'on the dashboard')
              : h('span', { class: 'asc-badge asc-badge-amber' }, 'walkthrough pending'))),
          h('td', {}, h('a', { class: 'asc-btn asc-btn-subtle asc-btn-sm', href: links.sign_in, target: '_blank', rel: 'noopener' }, 'Sign in'))))));
      card.appendChild(h('div', { class: 'asc-table-wrap' }, table));
    }
    container.appendChild(card);

    // ── Snapshot copy (§4)
    renderCopyPanel(container, ctx);

    // ── Reset (§3.2) — typed confirmation, sandbox-only on the server too
    const resetBtn = h('button', { class: 'asc-btn asc-btn-danger asc-btn-sm', type: 'button' }, 'Reset sandbox');
    resetBtn.addEventListener('click', async () => {
      const typed = window.prompt('This drops the three sandbox databases and the sandbox asset directory, ' +
        'then reseeds. Live data is never touched (the server refuses any non-sandbox path).\n\nType RESET SANDBOX to continue.');
      if (typed == null) return;
      resetBtn.disabled = true;
      try {
        const res = await api(SANDBOX_API + '/reset', { method: 'POST', body: { confirm: typed } });
        toast('Sandbox reset: ' + (res.reset && res.reset.removed ? res.reset.removed.length : 0) + ' file(s) removed, reseeded.', 'ok');
        selectedMessageId = null;
        renderAccounts(container, ctx);
      } catch (e) { toast(e.message || 'Reset refused.', 'error'); resetBtn.disabled = false; }
    });
    container.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Reset'),
        h('div', { class: 'asc-card-sub' },
          'Drops and reseeds asclepius_sandbox.db, community_sandbox.db, team_sandbox.db and the sandbox asset directory. ' +
          'Sandbox only: the server checks the realm and every path before touching a file.')),
        resetBtn)));
  }

  // ─── Snapshot copy (§4) ────────────────────────────────────
  async function renderCopyPanel(container, ctx) {
    const { h, api, toast, fmtDate } = ctx;
    const card = h('div', { class: 'asc-card' });
    container.appendChild(card);
    let data;
    try { data = await api(SANDBOX_API + '/copy-sources'); }
    catch (e) {
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-inline-warn' }, e.message || 'Could not list live health systems.')));
      return;
    }
    const sources = data.sources || [];
    card.appendChild(h('div', { class: 'asc-card-head' }, h('div', {},
      h('div', { class: 'asc-card-title' }, 'Copy real data into the sandbox'),
      h('div', { class: 'asc-card-sub' },
        'A read-only snapshot of a live health system, its row, uploads (+ files), ingest cases and purpose ' +
        'resolutions, stamped “production copy”. Tasks, submissions and physicians are NOT copied: the point is to ' +
        're-run task creation and routing from raw data. Re-copy replaces the sandbox copy. Live rows are never written.'))));
    if (!sources.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-empty' }, 'No live health systems to copy.')));
      return;
    }
    const table = h('table', { class: 'asc-table' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Source'), h('th', {}, 'Live ID'), h('th', {}, 'Uploads'), h('th', {}, 'Cases'), h('th', {}, 'In sandbox'), h('th', {}, ''))),
      h('tbody', {}, sources.map((s) => {
        const btn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button' }, s.copied_at ? 'Re-copy' : 'Copy');
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          try {
            const res = await api(SANDBOX_API + '/copy-health-system/' + encodeURIComponent(s.hs_id), { method: 'POST' });
            toast('Copied ' + s.name + ': ' + res.uploads + ' upload(s), ' + res.ingest_cases + ' case(s), ' + res.assets + ' file(s).', 'ok');
            renderAccounts(container, ctx);
          } catch (e) { toast(e.message || 'Copy failed.', 'error'); btn.disabled = false; }
        });
        return h('tr', {},
          h('td', {}, h('strong', {}, s.name), s.fixture ? h('span', { class: 'asc-badge asc-badge-lime', style: 'margin-left: var(--sp-2)' }, 'fixture') : ''),
          h('td', {}, h('code', { class: 'asc-mono asc-dim' }, s.hs_id)),
          h('td', {}, String(s.uploads || 0)),
          h('td', {}, String(s.ingest_cases || 0)),
          h('td', {}, s.copied_at ? ('copied ' + fmtDate(s.copied_at)) : '-'),
          h('td', {}, btn));
      })));
    card.appendChild(h('div', { class: 'asc-table-wrap' }, table));
  }

  // ─── Outbox (§3.3) ─────────────────────────────────────────
  async function renderOutbox(container, ctx) {
    const { h, api, clear, toast, loadingCard, fmtDate } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading the sandbox outbox…'));
    let data;
    try { data = await api(SANDBOX_API + '/outbox'); }
    catch (e) {
      clear(container);
      container.appendChild(errorCard(ctx, e.message || 'Could not load the outbox.'));
      return;
    }
    clear(container);
    const msgs = data.messages || [];
    const clearBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button' }, 'Clear outbox');
    clearBtn.addEventListener('click', async () => {
      try {
        const res = await api(SANDBOX_API + '/outbox', { method: 'DELETE' });
        toast('Cleared ' + res.cleared + ' message(s).', 'ok');
        selectedMessageId = null;
        renderOutbox(container, ctx);
      } catch (e) { toast(e.message || 'Could not clear.', 'error'); }
    });
    const refreshBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button' }, 'Refresh');
    refreshBtn.addEventListener('click', () => renderOutbox(container, ctx));
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Outbox'),
        h('div', { class: 'asc-card-sub' },
          'Every email the sandbox “sent”. Nothing here was delivered anywhere: the OTP code, magic link or DLA link ' +
          'is extracted so a fake onboarding can be walked end to end without leaving this page.')),
        h('div', { class: 'asc-row', style: 'gap: var(--sp-2)' }, refreshBtn, clearBtn)));
    if (!msgs.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-empty' }, 'No messages yet. Start a fake onboarding from the Accounts tab.')));
      container.appendChild(card);
      return;
    }
    const table = h('table', { class: 'asc-table' },
      h('thead', {}, h('tr', {}, h('th', {}, 'When'), h('th', {}, 'To'), h('th', {}, 'Subject'), h('th', {}, 'Codes'), h('th', {}, 'Links'), h('th', {}, ''))),
      h('tbody', {}, msgs.map((m) => {
        const openBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button' }, 'Open');
        openBtn.addEventListener('click', () => { selectedMessageId = m.id; renderMessage(detail, ctx, m.id); });
        return h('tr', { class: selectedMessageId === m.id ? 'asc-row-selected' : '' },
          h('td', {}, fmtDate(m.created_at)),
          h('td', {}, h('code', { class: 'asc-mono' }, m.to_email)),
          h('td', {}, m.subject),
          h('td', {}, (m.codes || []).length
            ? (m.codes || []).map((c) => h('span', { style: 'margin-right: var(--sp-2)' }, h('code', { class: 'asc-mono' }, c), ' ', copyBtn(ctx, c, 'Copy')))
            : h('span', { class: 'asc-dim' }, '-')),
          h('td', {}, (m.links || []).length
            ? (m.links || []).map((l) => h('div', {}, h('a', { href: l, target: '_blank', rel: 'noopener', class: 'asc-link' }, linkLabel(l))))
            : h('span', { class: 'asc-dim' }, '-')),
          h('td', {}, openBtn));
      })));
    card.appendChild(h('div', { class: 'asc-table-wrap' }, table));
    container.appendChild(card);
    const detail = h('div', {});
    container.appendChild(detail);
    if (selectedMessageId && msgs.some((m) => m.id === selectedMessageId)) renderMessage(detail, ctx, selectedMessageId);
  }

  function linkLabel(url) {
    try {
      const u = new URL(url);
      const path = u.pathname.length > 40 ? u.pathname.slice(0, 37) + '…' : u.pathname;
      const kind = /token=|asc_handoff|\/join|reset|verify|sign|dla|agreement/i.test(url) ? 'open link' : 'link';
      return kind + ' · ' + u.host + path;
    } catch (e) { return url; }
  }

  async function renderMessage(host, ctx, id) {
    const { h, api, clear, fmtDate } = ctx;
    clear(host);
    let m;
    try { m = await api(SANDBOX_API + '/outbox/' + encodeURIComponent(id)); }
    catch (e) { host.appendChild(errorCard(ctx, e.message || 'Could not open the message.')); return; }
    const frame = h('iframe', {
      title: 'Rendered email', sandbox: '',   // no scripts, no same-origin: it is untrusted HTML
      style: 'width:100%; min-height: 520px; border: 1px solid var(--line, #ddd); border-radius: 8px; background: #fff',
    });
    frame.srcdoc = m.html;
    host.appendChild(h('div', { class: 'asc-card', style: 'margin-top: var(--sp-3)' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, m.subject),
        h('div', { class: 'asc-card-sub' }, 'To ' + m.to_email + ' · ' + fmtDate(m.created_at) +
          ((m.attachments || []).length ? ' · ' + m.attachments.map((a) => a.name + ' (' + a.bytes + ' B)').join(', ') : '')))),
      h('div', { class: 'asc-card-pad' },
        (m.links || []).length ? h('div', { style: 'margin-bottom: var(--sp-3)' },
          (m.links || []).map((l) => h('div', {}, h('a', { href: l, target: '_blank', rel: 'noopener', class: 'asc-link' }, l)))) : '',
        frame)));
  }

  window.AdminSandboxSection = { render };
})();
