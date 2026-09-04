/* ═══════════════════════════════════════════════════════════
   Admin · Money and Metrics section (Admin Launch PRD §4)

   window.AdminEarningsSection { render }

   Two sub-views the shell routes into:
     'earnings'  — TWO LEVELS. Level 1 is every approved physician
                   with what we owe them; level 2 is that physician's
                   cases, each with its time, its pay, an export, and
                   the decision: Approve or Void (Export & Approval
                   PRD §1.2).
     'referrals' — the referral book: who referred whom, funnel
                   position, ledger state, source, fraud flags. A
                   separate ledger with its own bounty logic.

   The status chips and the flat company-wide ledger table are gone
   (§4.1). LEDGER_STATES in payments.py is untouched — the states
   still drive the ledger, they were simply never a navigation.

   Two rules this file exists to hold:

   1. THE SERVER OWNS THE TOTAL. Void and pay both return the
      recomputed figure and the UI renders that. Subtracting locally
      is how a displayed total drifts from the ledger, and the number
      an operator is about to wire is the worst place for a drift.

   2. NO HARDCODED RATE. Every amount comes from the row's
      amount_cents. Reviewers are paid per SESSION (tr_session_cents),
      not per case, so a hardcoded $75 would silently misreport every
      reviewer on the screen.

   3. APPROVING IS ONE ACT, NOT TWO. The Approve button hits one
      endpoint that moves the ledger AND the export gate together,
      and the message it prints says which. A screen that could
      approve payment while leaving the case unshippable is the
      three-status split this PRD closes.

   Same contract as every admin_*.js module: DOM through ctx.h only,
   zero innerHTML, section state module-local, and a load failure is a
   visible error, never a blank.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var selectedUser = null;   // level 2: the physician whose cases are open
  var batchDraft = '';
  var busy = false;
  var message = null;
  var error = null;
  var voidingId = null;      // the row with an open inline confirm
  var voidReason = '';
  var voidError = null;
  var approvingId = null;    // the row whose Approve is in flight
  var rowError = {};         // earning_id -> the message its own row shows

  function money(cents) {
    var n = Math.round(Number(cents) || 0) / 100;
    return '$' + n.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  /* Time on task. `null` renders an em dash, NEVER '0m'.
     A zero meaning "unknown" is how an operator voids honest work: it reads as
     a physician who was credited for a case they did not open. The server
     already refuses to write 0 for an unrecorded duration (see
     _enrich_case_context); this is the other half of that promise. */
  function duration(seconds) {
    if (seconds === null || seconds === undefined || seconds === '') return '-';
    var s = Math.max(0, Math.round(Number(seconds)));
    if (!s) return '-';
    var m = Math.floor(s / 60);
    var rest = s % 60;
    return m + 'm ' + (rest < 10 ? '0' : '') + rest + 's';
  }

  function errText(e) {
    return (e && (e.detail || e.message)) || 'no response';
  }

  function inlineError(h, msg) {
    return h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-inline-error' }, msg)));
  }

  function render(body, ctx, view) {
    ctx.clear(body);
    body.appendChild(ctx.loadingCard('Loading the ledger…'));
    if (view === 'referrals') renderReferrals(body, ctx);
    else if (selectedUser) renderPhysicianLedger(body, ctx);
    else renderPhysicianList(body, ctx);
  }

  function rerender(body, ctx) { render(body, ctx, 'earnings'); }

  /* ── Level 1: every approved physician, and what we owe them (§4.2) ──────
   *
   * Two sources, joined on user_id: /admin/physicians for the NAME (the ledger
   * carries an email and a tier, not a name or a specialty) and /admin/earnings
   * for the money. The ledger reconciles before serving, so the admin view and
   * the physician's own Earnings page can never be two different answers.
   *
   * The outstanding figure comes from `by_user`, which the server aggregates in
   * SQL over the whole ledger — not from summing `rows`, which is one page of it. */
  function renderPhysicianList(body, ctx) {
    var h = ctx.h;
    Promise.all([
      ctx.api('/admin/earnings'),
      ctx.api('/admin/physicians'),
    ]).then(function (res) {
      var ledger = res[0] || {};
      var byUser = ledger.by_user || {};
      var physicians = ((res[1] || {}).physicians || [])
        .filter(function (p) { return p.verification_status === 'approved'; });

      ctx.clear(body);
      if (message) body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-ref-msg' }, message))));
      message = null;

      var rows = physicians.map(function (p) {
        var agg = byUser[p.id] || {};
        return {
          user: p,
          outstanding: agg.outstanding_cents || 0,
          paid: agg.paid_cents || 0,
          nRows: agg.n_rows || 0,
        };
      }).sort(function (a, b) { return b.outstanding - a.outstanding; });

      if (!rows.length) {
        body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-ref-empty' },
            'No approved physicians yet. Money appears here the moment approved '
            + 'casework is graded.'))));
        return;
      }

      var table = h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Physician'), h('th', {}, 'Specialty'), h('th', {}, 'Tier'),
          h('th', {}, 'Cases'), h('th', {}, 'Paid to date'), h('th', {}, 'Outstanding'))),
        h('tbody', {}, rows.map(function (r) {
          var tr = h('tr', { class: 'asc-row-click' },
            h('td', {}, h('strong', {}, r.user.name || r.user.email || r.user.id)),
            h('td', {}, r.user.specialty || '-'),
            h('td', {}, r.user.tier_word || '-'),
            h('td', { class: 'asc-mono' }, String(r.nRows)),
            h('td', { class: 'asc-mono' }, money(r.paid)),
            h('td', { class: 'asc-mono' }, money(r.outstanding)));
          tr.addEventListener('click', function () {
            selectedUser = r.user; error = null; message = null;
            voidingId = null; voidReason = ''; voidError = null;
            approvingId = null; rowError = {};
            rerender(body, ctx);
          });
          return tr;
        })));
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-table-wrap' }, table)));
    }).catch(function (e) {
      ctx.clear(body);
      body.appendChild(inlineError(h,
        'The ledger could not be loaded: ' + errText(e) + '. Reload the page.'));
    });
  }

  /* ── Level 2: one physician's cases (§4.2, §4.3, §4.4, §4.5) ─────────── */
  function renderPhysicianLedger(body, ctx) {
    var h = ctx.h;
    var u = selectedUser;
    ctx.api('/admin/earnings?user_id=' + encodeURIComponent(u.id)).then(function (data) {
      ctx.clear(body);
      var rows = data.rows || [];
      var totals = (data.by_user || {})[u.id]
        || { outstanding_cents: 0, paid_cents: 0, n_rows: rows.length, n_void: 0 };

      var back = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
        '← Back');
      back.addEventListener('click', function () {
        selectedUser = null; error = null; message = null; rerender(body, ctx);
      });

      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' },
          h('div', {},
            h('div', { class: 'asc-card-title' }, u.name || u.email || u.id),
            h('div', { class: 'asc-card-sub' },
              [u.specialty || 'No specialty', u.tier_word || 'Unassigned'].join(' · '))),
          back)));

      var nVoid = totals.n_void || 0;
      var payable = rows.filter(function (r) { return r.status === 'approved'; });
      var payBtn = h('button', {
        class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button',
      }, 'Send Payment');
      if (!payable.length) {
        payBtn.setAttribute('disabled', '');
        payBtn.setAttribute('title',
          'Nothing is approved for payment yet. Accrued rows settle when their '
          + 'review lands, or after the auto-approve window.');
      }
      var batchInput = h('input', {
        class: 'asc-ref-input', type: 'text',
        placeholder: 'payout batch id, e.g. 2026-08-25-wise',
        value: batchDraft,
      });
      batchInput.addEventListener('input', function () { batchDraft = batchInput.value; });
      payBtn.addEventListener('click', function () { sendPayment(body, ctx, payable); });

      // TOTAL PAYABLE, then the counts under it. Built from classes that are
      // actually styled: the `asc-admin-money-*` and `asc-admin-table` names the
      // previous version used have no rule in any stylesheet, so every table and
      // headline on this screen rendered as bare unstyled HTML.
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' },
          h('div', {},
            h('div', { class: 'asc-card-title' },
              'TOTAL PAYABLE  ',
              h('span', { class: 'asc-mono' }, money(totals.outstanding_cents))),
            h('div', { class: 'asc-card-sub' },
              rows.length + ' case' + (rows.length === 1 ? '' : 's') + ' · '
              + nVoid + ' voided · ' + money(totals.paid_cents) + ' paid to date')),
          h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' },
            batchInput, payBtn)),
        (error || message)
          ? h('div', { class: 'asc-card-pad' },
              error ? h('div', { class: 'asc-ref-error' }, error) : null,
              message ? h('div', { class: 'asc-ref-msg' }, message) : null)
          : null));

      if (!rows.length) {
        body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-ref-empty' },
            'No ledger rows for this physician yet.'))));
        return;
      }

      var table = h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', { class: 'asc-cell-id' }, 'Case'), h('th', {}, 'Specialty'),
          h('th', {}, 'Time'), h('th', {}, 'Quality'),
          h('th', {}, 'Pay'), h('th', {}, 'Status'), h('th', {}, 'Export'),
          // ONE actions column (PRD §1.2). Approve and Void are the two answers
          // to the same question and belong side by side; a dedicated "Void"
          // column with nothing next to it is what made an accrued row look
          // like it had no decision available.
          h('th', {}, 'Actions'))),
        h('tbody', {}, rows.map(function (r) { return caseRow(body, ctx, r); })));
      body.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-table-wrap' }, table)));
    }).catch(function (e) {
      ctx.clear(body);
      body.appendChild(inlineError(h,
        'This physician’s ledger could not be loaded: ' + errText(e)
        + '. Reload the page.'));
    });
  }

  /* The internal case-quality number.

     Internal on purpose: it is not in any package and no buyer sees it. It is
     here because an operator deciding whether to pay for a case should be able
     to see how it was graded on the same row, rather than opening a second
     screen.

     `null` renders an em dash, like Time, and for the same reason: a case that
     was never graded has no number, and printing 0 for it reads as a physician
     who did bad work rather than as a case nobody has reviewed yet.

     The reasons ride along as a title, because this number will be contested
     the moment it touches pay, and "71" is not an answer to "why". */
  function qualityCell(ctx, r) {
    var h = ctx.h;
    if (r.quality === null || r.quality === undefined) {
      return h('td', {
        class: 'asc-dim',
        title: 'Not graded yet. A case gets its quality number when a reviewer '
             + 'or QA decides it.',
      }, '-');
    }
    var reasons = (r.quality_reasons || []).join('\n');
    return h('td', {
      class: 'asc-mono',
      title: reasons || 'No itemization was stored for this case.',
    }, String(Math.round(Number(r.quality))));
  }

  function caseRow(body, ctx, r) {
    var h = ctx.h;
    var isVoid = r.status === 'void';
    // A voided row STAYS LISTED, marked, and paying $0.00. Hiding it would make
    // a decision somebody made disappear from the only screen that records it.
    var pay = isVoid ? money(0) : money(r.amount_cents);

    var exportCell = h('td', {});
    if (r.kind === 'task') {
      var dl = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
        '.json');
      dl.addEventListener('click', function () {
        // ctx.downloadBlob already carries the admin bearer token; the endpoint
        // answers with Content-Disposition: attachment.
        ctx.downloadBlob('/admin/earnings/' + encodeURIComponent(r.earning_id)
                         + '/case-export', 'case-' + (r.case_id || r.earning_id) + '.json');
      });
      exportCell.appendChild(dl);
    } else {
      // A review session spans several cases and a referral bounty is not
      // casework — there is no single case to hand over.
      exportCell.appendChild(h('span', {
        class: 'asc-dim',
        title: 'Only a task row carries one case. This row does not.',
      }, '-'));
    }

    return h('tr', isVoid ? { class: 'asc-dim' } : {},
      // The FULL case id, with a copy button (PRD §1.3). It used to render as
      // `slice(0, 10) + '…'`, which meant the one id an operator needs to move
      // to the export box was the one thing this screen would not give them.
      h('td', { class: 'asc-cell-id' }, ctx.copyableId
        ? ctx.copyableId(r.case_id || r.ref_id)
        : h('span', { class: 'asc-mono' }, String(r.case_id || r.ref_id || '-'))),
      h('td', {}, r.specialty || '-'),
      h('td', { class: 'asc-mono' }, duration(r.seconds)),
      qualityCell(ctx, r),
      // NEVER a hardcoded rate. amount_cents is what the ledger says this row
      // is worth, and a reviewer's session is not a labeler's case.
      h('td', { class: 'asc-mono' }, pay),
      h('td', {}, statusWord(r)),
      exportCell,
      actionsCell(body, ctx, r));
  }

  /* ── The decision, on the row (PRD §1.2) ────────────────────────────────
   *
   *   accrued  → [ Approve ] [ Void ]
   *   approved → [ Void ]
   *   paid     → nothing (money has left; a refund is a treasury operation)
   *   void     → the VOIDED badge, and no way back
   *
   * Approve is the button that did not exist. There was no approve endpoint for
   * a labeler earning at all — only `/release` (a proposed pay cut) and `/void`
   * — so an accrued row read "Pending review" with nothing under it and waited
   * on a reviewer who might never come. And approving it now moves the EXPORT
   * gate as well as the ledger, which is the other half of the same bug: a case
   * we had paid for still could not ship. */
  function actionsCell(body, ctx, r) {
    var h = ctx.h;
    var cell = h('td', {});
    var err = rowError[r.earning_id];

    if (r.status === 'void') {
      cell.appendChild(h('span', { class: 'asc-badge asc-badge-gray' }, 'VOIDED'));
      return cell;
    }
    if (r.status === 'paid') {
      cell.appendChild(h('span', {
        class: 'asc-dim',
        title: 'Money has already left. Refunds are handled outside the ledger.',
      }, '-'));
      return cell;
    }
    if (voidingId === r.earning_id) {
      cell.appendChild(voidConfirm(body, ctx, r));
      return cell;
    }

    var wrap = h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' });
    if (r.status === 'accrued') {
      var ab = h('button', {
        class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button',
        title: r.quality_hold
          ? 'The payout algorithm proposed a reduced rate on this case. Release '
            + 'that hold first: approving here would apply the cut without the '
            + 'decision that is supposed to authorize it.'
          : 'Approve this case: pay it, and make its records exportable.',
      }, approvingId === r.earning_id ? 'Approving…' : 'Approve');
      if (busy || approvingId === r.earning_id) ab.setAttribute('disabled', '');
      ab.addEventListener('click', function () { doApprove(body, ctx, r); });
      wrap.appendChild(ab);
    }
    var vb = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      'Void');
    if (busy) vb.setAttribute('disabled', '');
    vb.addEventListener('click', function () {
      voidingId = r.earning_id; voidReason = ''; voidError = null;
      rerender(body, ctx);
    });
    wrap.appendChild(vb);
    cell.appendChild(wrap);
    if (err) cell.appendChild(h('div', { class: 'asc-inline-error' }, err));
    return cell;
  }

  function doApprove(body, ctx, r) {
    if (busy) return;
    busy = true; approvingId = r.earning_id; delete rowError[r.earning_id];
    // Optimistic: the row shows its new state immediately and the reload below
    // replaces it with the server's. A rollback is the same reload — the ledger
    // is the truth and this screen never subtracts its own numbers.
    r.status = 'approved';
    rerender(body, ctx);
    ctx.api('/admin/earnings/' + encodeURIComponent(r.earning_id) + '/approve', {
      method: 'POST', body: { note: '' },
    }).then(function (res) {
      busy = false; approvingId = null;
      var totals = (res && res.totals) || {};
      // SAY WHAT HAPPENED TO THE EXPORT GATE. An approval that pays a case but
      // leaves it unshippable is the exact silence this PRD exists to remove,
      // so when the records did not move, the operator is told here.
      message = res && res.exportable
        ? 'Approved. This case is now exportable. Total payable is '
          + money(totals.outstanding_cents || 0) + '.'
        : 'Approved for payment, but its records did NOT become exportable ('
          + ((res && res.records_outcome) || 'unknown')
          + '). It will show in the Export tab\u2019s excluded list.';
      rerender(body, ctx);
    }).catch(function (e) {
      busy = false; approvingId = null;
      r.status = 'accrued';                       // rollback
      rowError[r.earning_id] = errText(e);
      rerender(body, ctx);
    });
  }

  function statusWord(r) {
    // 'Pending review' was never true: there was no review queue behind it and
    // no reviewer coming. It sent operators looking for a person instead of a
    // button (PRD §1.2).
    var words = { accrued: 'Awaiting approval', approved: 'Approved',
                  paid: 'Paid', void: 'Not approved' };
    if (r.status === 'accrued' && r.quality_hold) return 'Held: reduced rate proposed';
    return words[r.status] || r.status || '-';
  }

  /* Inline confirm requiring a TYPED reason. Never window.confirm(): it cannot
     capture a reason, and a void with no reason cannot be audited or appealed —
     which is the whole point of storing void_reason / voided_by / voided_at. */
  function voidConfirm(body, ctx, r) {
    var h = ctx.h;
    var input = h('input', {
      class: 'asc-ref-input', type: 'text',
      placeholder: 'Why? (required)', value: voidReason,
    });
    input.addEventListener('input', function () { voidReason = input.value; });
    var go = h('button', { class: 'asc-btn asc-btn-sm asc-btn-primary', type: 'button' },
      busy ? 'Voiding…' : 'Confirm void');
    if (busy) go.setAttribute('disabled', '');
    go.addEventListener('click', function () { doVoid(body, ctx, r); });
    var cancel = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      'Cancel');
    cancel.addEventListener('click', function () {
      voidingId = null; voidReason = ''; voidError = null; rerender(body, ctx);
    });
    return h('div', {},
      input, go, cancel,
      voidError ? h('div', { class: 'asc-inline-error' }, voidError) : null);
  }

  function doVoid(body, ctx, r) {
    if (busy) return;
    var reason = (voidReason || '').trim();
    if (reason.length < 3) {
      voidError = 'A void needs a reason of at least 3 characters. It has to be '
                + 'auditable and appealable.';
      rerender(body, ctx);
      return;
    }
    busy = true;
    ctx.api('/admin/earnings/' + encodeURIComponent(r.earning_id) + '/void', {
      method: 'POST', body: { reason: reason },
    }).then(function (res) {
      busy = false; voidingId = null; voidReason = ''; voidError = null;
      // The server's recomputed total, never a local subtraction. The reload
      // below re-reads it; this line is what the operator is told happened.
      message = res && res.voided
        ? 'Voided. Total payable is now '
          + money(((res.totals || {}).outstanding_cents) || 0) + '.'
        : 'That case was already void: nothing changed.';
      rerender(body, ctx);
    }).catch(function (e) {
      busy = false;
      voidError = errText(e);
      rerender(body, ctx);
    });
  }

  /* §4.5 — Send Payment. Reuses the ledger's own mark-paid path through
     POST /admin/earnings/pay, which is thin over asc_payments.mark_paid: the
     payout_batch_id is the IDEMPOTENCY KEY, so a retried disbursement is a
     no-op rather than a second payment. */
  function sendPayment(body, ctx, payable) {
    if (busy) return;
    var batch = (batchDraft || '').trim();
    error = null; message = null;
    if (!payable.length) {
      error = 'Nothing is approved for payment yet.';
      rerender(body, ctx); return;
    }
    if (!batch) {
      error = 'Name the payout batch: it is the idempotency key.';
      rerender(body, ctx); return;
    }
    busy = true;
    ctx.api('/admin/earnings/pay', {
      method: 'POST',
      body: {
        user_id: selectedUser.id,
        earning_ids: payable.map(function (r) { return r.earning_id; }),
        payout_batch_id: batch,
      },
    }).then(function (res) {
      busy = false; batchDraft = '';
      var n = (res && res.marked != null) ? res.marked : payable.length;
      message = 'Marked ' + n + ' row(s) paid in batch ' + batch + '. Outstanding is now '
        + money(((res && res.totals) || {}).outstanding_cents || 0) + '.';
      rerender(body, ctx);
    }).catch(function (e) {
      busy = false;
      error = errText(e);
      rerender(body, ctx);
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
          + ' to the invitee, '
          // cap_cents 0 means NO CEILING, which is the default configuration.
          // Rendering it through money() would print "ceiling $0.00 per
          // referrer", which reads as a program that pays nothing.
          + (ps.cap_cents
              ? 'ceiling ' + money(ps.cap_cents) + ' per referrer.'
              : 'no ceiling.')))));
      var rows = data.rows || [];
      if (!rows.length) {
        body.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-ref-empty' }, 'No referrals yet.'))));
        return;
      }
      var table = h('table', { class: 'asc-table' },
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
        h('div', { class: 'asc-table-wrap' }, table)));
    }).catch(function (e) {
      ctx.clear(body);
      body.appendChild(inlineError(h,
        'The referral book could not be loaded: ' + errText(e) + '. Reload the page.'));
    });
  }

  window.AdminEarningsSection = {
    render: render,
    // Mirrors AdminPhysiciansSection.reset(). Level 2 is deliberately sticky
    // across re-renders — void and pay both re-render to pick up the server's
    // recomputed total, and dropping to level 1 there would throw the operator
    // out of the ledger they are working. This is the explicit way back.
    reset() {
      selectedUser = null; batchDraft = ''; busy = false;
      message = null; error = null;
      voidingId = null; voidReason = ''; voidError = null;
      approvingId = null; rowError = {};
    },
  };
})();
