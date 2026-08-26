/* ═══════════════════════════════════════════════════════════
   Admin · Money section (PRD-P admin ledger + PRD-REF overview)

   window.AdminEarningsSection { render }

   Two sub-views the shell routes into:
     'earnings'  — the whole ledger, filterable by status, with the
                   mark-paid flow (select rows, name a payout batch,
                   POST once; the batch id is the idempotency key).
     'referrals' — the referral book: who referred whom, funnel
                   position, ledger state, source, fraud flags.

   Same contract as every admin_*.js module: DOM through ctx.h only,
   zero innerHTML, section state module-local, and a load failure is a
   visible error, never a blank.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var statusFilter = '';
  var selected = {};      // earning_id -> true
  var batchDraft = '';
  var busy = false;
  var message = null;
  var error = null;

  function money(cents) {
    var n = Math.round(Number(cents) || 0) / 100;
    return '$' + n.toLocaleString('en-US', {
      minimumFractionDigits: 0,
      maximumFractionDigits: n % 1 ? 2 : 0,
    });
  }

  function render(body, ctx, view) {
    ctx.clear(body);
    body.appendChild(ctx.loadingCard('Loading the ledger…'));
    if (view === 'referrals') renderReferrals(body, ctx);
    else renderEarnings(body, ctx);
  }

  /* ── The ledger ─────────────────────────────────────────────────────── */
  function renderEarnings(body, ctx) {
    var h = ctx.h;
    var query = statusFilter ? '?status=' + encodeURIComponent(statusFilter) : '';
    ctx.api('/admin/earnings' + query).then(function (data) {
      ctx.clear(body);
      message = null;

      var totals = data.totals || {};
      var strip = h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad asc-admin-money-strip' },
        ['accrued', 'approved', 'paid', 'void'].map(function (st) {
          var t = totals[st] || {};
          return h('div', { class: 'asc-admin-money-cell' },
            h('div', { class: 'asc-admin-money-n asc-mono' }, money(t.cents || 0)),
            h('div', { class: 'asc-admin-money-label' },
              st + (t.count ? ' · ' + t.count : '')));
        })));
      body.appendChild(strip);

      var filters = h('div', { class: 'asc-admin-money-filters' },
        ['', 'accrued', 'approved', 'paid', 'void'].map(function (st) {
          var btn = h('button', {
            class: 'asc-phys-chip' + (statusFilter === st ? ' active' : ''),
            type: 'button',
          }, st || 'All');
          btn.addEventListener('click', function () {
            statusFilter = st; selected = {}; render(body, ctx, 'earnings');
          });
          return btn;
        }));
      body.appendChild(filters);

      var rows = data.rows || [];
      if (!rows.length) {
        body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-ref-empty' },
            'No ledger rows' + (statusFilter ? ' with status ' + statusFilter : '')
            + ' yet. Money appears here the moment casework is graded.'))));
        return;
      }

      var table = h('table', { class: 'asc-admin-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, ''), h('th', {}, 'Physician'), h('th', {}, 'Kind'),
          h('th', {}, 'Amount'), h('th', {}, 'Status'), h('th', {}, 'Accrued'),
          h('th', {}, 'Batch'))),
        h('tbody', {}, rows.map(function (r) {
          var canPay = r.status === 'approved';
          var box = h('input', { type: 'checkbox' });
          if (!canPay) box.setAttribute('disabled', '');
          if (selected[r.earning_id]) box.setAttribute('checked', '');
          box.addEventListener('change', function () {
            if (box.checked) selected[r.earning_id] = true;
            else delete selected[r.earning_id];
            paintBar();
          });
          return h('tr', {},
            h('td', {}, box),
            h('td', {}, r.user_email || r.user_id || ''),
            h('td', {}, r.kind || ''),
            h('td', { class: 'asc-mono' }, money(r.amount_cents)),
            h('td', {}, r.status || ''),
            h('td', { class: 'asc-mono' }, (r.accrued_at || '').slice(0, 10)),
            h('td', { class: 'asc-mono' }, r.payout_batch_id || ''));
        })));
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-pad', style: 'overflow-x:auto' }, table)));

      // The mark-paid bar: rows are selected above, the batch id is typed
      // once, and the POST is idempotent on it, so a retried disbursement
      // job cannot pay twice.
      var input = h('input', {
        class: 'asc-ref-input', type: 'text',
        placeholder: 'payout batch id, e.g. 2026-08-25-wise',
        value: batchDraft,
      });
      input.addEventListener('input', function () { batchDraft = input.value; });
      var payBtn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button' },
        'Mark selected paid');
      payBtn.addEventListener('click', function () { markPaid(body, ctx); });
      var bar = h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad asc-admin-payba' },
        h('div', { class: 'asc-admin-paycount', id: 'ascPayCount' }, countLabel()),
        input, payBtn,
        error ? h('div', { class: 'asc-ref-error' }, error) : null,
        message ? h('div', { class: 'asc-ref-msg' }, message) : null));
      body.appendChild(bar);

      function paintBar() {
        var el = document.getElementById('ascPayCount');
        if (el) el.textContent = countLabel();
      }
    }).catch(function (e) {
      ctx.clear(body);
      body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          'The ledger could not be loaded: '
          + ((e && (e.detail || e.message)) || 'no response') + '. Reload the page.'))));
    });
  }

  function countLabel() {
    var n = Object.keys(selected).length;
    return n === 0 ? 'Select approved rows to pay'
      : n === 1 ? '1 row selected' : n + ' rows selected';
  }

  function markPaid(body, ctx) {
    if (busy) return;
    var ids = Object.keys(selected);
    var batch = (batchDraft || '').trim();
    error = null; message = null;
    if (!ids.length) { error = 'Select at least one approved row first.'; }
    else if (!batch) { error = 'Name the payout batch: it is the idempotency key.'; }
    if (error) { render(body, ctx, 'earnings'); return; }
    busy = true;
    ctx.api('/admin/earnings/mark-paid', {
      method: 'POST',
      body: { payout_batch_id: batch, earning_ids: ids },
    }).then(function (res) {
      busy = false; selected = {}; batchDraft = '';
      message = 'Marked ' + ((res && res.marked != null) ? res.marked : ids.length)
        + ' row(s) paid in batch ' + batch + '.';
      render(body, ctx, 'earnings');
    }).catch(function (e) {
      busy = false;
      error = (e && (e.detail || e.message)) || 'That did not save. Try again.';
      render(body, ctx, 'earnings');
    });
  }

  /* ── The referral book ──────────────────────────────────────────────── */
  function renderReferrals(body, ctx) {
    var h = ctx.h;
    ctx.api('/admin/referrals').then(function (data) {
      ctx.clear(body);
      var ps = data.payout_structure || {};
      body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-card-sub' },
          'Structure: ' + money(ps.referrer_bounty_cents) + ' to the referrer at '
          + 'verified + first accepted case, ' + money(ps.referee_bonus_cents)
          + ' to the invitee, ceiling ' + money(ps.cap_cents) + ' per referrer.'))));
      var rows = data.rows || [];
      if (!rows.length) {
        body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-ref-empty' }, 'No referrals yet.'))));
        return;
      }
      var table = h('table', { class: 'asc-admin-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Referrer'), h('th', {}, 'Invitee'), h('th', {}, 'Where it stands'),
          h('th', {}, 'Money'), h('th', {}, 'Source'), h('th', {}, 'Flag'),
          h('th', {}, 'Invited'))),
        h('tbody', {}, rows.map(function (r) {
          return h('tr', {},
            h('td', {}, r.referrer_name || r.referrer_email || ''),
            h('td', {}, r.invitee_name || r.invitee_email || ''),
            h('td', {}, r.status_sentence || ''),
            h('td', {}, r.bounty_state || ''),
            h('td', {}, r.source || ''),
            h('td', {}, r.fraud_flag
              ? h('span', { class: 'asc-badge asc-badge-amber' }, r.fraud_flag)
              : ''),
            h('td', { class: 'asc-mono' }, (r.invited_at || '').slice(0, 10)));
        })));
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-pad', style: 'overflow-x:auto' }, table)));
    }).catch(function (e) {
      ctx.clear(body);
      body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          'The referral book could not be loaded: '
          + ((e && (e.detail || e.message)) || 'no response') + '. Reload the page.'))));
    });
  }

  window.AdminEarningsSection = { render: render };
})();
