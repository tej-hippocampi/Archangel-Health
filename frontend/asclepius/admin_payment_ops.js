/* Manually approved batches, bank payout status and annual tax cross-checks. */
(function () {
  'use strict';
  var base = '/admin/payment-ops';
  function money(c) { return c == null ? 'Not supplied' : '$' + (c / 100).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
  function err(e) { return typeof e.detail === 'string' ? e.detail : (e.message || 'Request failed.'); }
  function button(h, label, action) {
    var b = h('button', {type: 'button', class: 'asc-btn asc-btn-sm'}, label);
    b.addEventListener('click', function () { action(b); }); return b;
  }
  function card(h, title, children) {
    return h('section', {class: 'asc-card'}, h('div', {class: 'asc-card-pad'}, h('h3', {}, title), children));
  }
  function table(h, columns, rows) {
    return h('div', {class: 'asc-table-wrap'}, h('table', {class: 'asc-table'},
      h('thead', {}, h('tr', {}, columns.map(function (c) { return h('th', {}, c); }))),
      h('tbody', {}, rows.map(function (r) { return h('tr', {}, r.map(function (c) { return h('td', {}, c); })); }))));
  }
  function render(body, ctx) {
    var h = ctx.h;
    ctx.clear(body); body.appendChild(ctx.loadingCard('Loading payments…'));
    Promise.all([ctx.api(base), ctx.api(base + '/readiness')]).then(function (data) {
      ctx.clear(body);
      var d = data[0], ready = data[1];
      var feedback = h('div', {role: 'status', 'aria-live': 'polite'});
      body.appendChild(h('h2', {}, 'Payment operations'));
      body.appendChild(h('p', {}, 'Review and approve each batch. Only approved payments enter the processing queue.'));
      body.appendChild(feedback);
      function post(path, payload, b, done) {
        b.disabled = true; feedback.textContent = '';
        ctx.api(base + path, {method: 'POST', body: JSON.stringify(payload || {})})
          .then(function (r) { if (done) done(r); else render(body, ctx); })
          .catch(function (e) { feedback.textContent = err(e); feedback.className = 'asc-inline-error'; })
          .finally(function () { b.disabled = false; });
      }
      body.appendChild(card(h, 'Stripe setup', [
        table(h, ['Check', 'Status'], [
          ['Environment', ready.mode], ['Stripe connection', ready.stripe_check],
          ['Available USD balance', money(ready.available_usd_cents)],
          ['Approved-batch processing', ready.worker_enabled ? 'Enabled' : 'Disabled'],
          ['Live batch payments', ready.live_execution_enabled ? 'Enabled' : 'Disabled'],
          ['Webhook signing secret', ready.webhook_configured ? 'Configured; delivery still needs verification' : 'Missing'],
          ['Connected-account webhook', ready.connect_webhook_configured ? 'Configured; delivery still needs verification' : 'Missing'],
          ['US tax information during onboarding', ready.us_tax_collection_enabled ? 'Requested for US accounts' : 'Not enabled'],
          ['Tax settings review', ready.tax_review ? 'Recorded by an administrator on ' + ready.tax_review.reviewed_at : 'Pending'],
        ]),
        h('p', {}, 'Verify Connect approval, funding, W-9 collection, 1099 settings and delivery directly in Stripe.'),
        h('a', {href: 'https://dashboard.stripe.com/settings/connect/tax_forms', target: '_blank', rel: 'noopener noreferrer'}, 'Open Stripe tax settings'),
        h('p', {}, 'Recording a review here does not change Stripe settings or file tax forms.'),
      ]));
      if (ready.tax_review) {
        var reviewed = ready.tax_review.settings;
        body.appendChild(card(h, 'Recorded tax settings · ' + ready.tax_review.stripe_mode, table(h, ['Setting', 'Administrator review'], [
          ['Calculation method', reviewed.calculation_method.replace(/_/g, ' ')],
          ['Platform and funding', reviewed.platform_and_funding_checked ? 'Checked' : 'Pending'],
          ['Tax identity and W-9', reviewed.tax_identity_and_w9_checked ? 'Checked' : 'Pending'],
          ['Delivery and consent', reviewed.delivery_and_consent_checked ? 'Checked' : 'Pending'],
          ['State filing', reviewed.state_filing_checked ? 'Checked' : 'Pending'],
        ])));
      }
      var selected = new Set();
      var choose = d.eligible.map(function (r) {
        var c = h('input', {type: 'checkbox', 'aria-label': 'Select ' + r.earning_id});
        c.addEventListener('change', function () { if (c.checked) selected.add(r.earning_id); else selected.delete(r.earning_id); });
        return [c, r.email || r.user_id, r.earning_id, money(r.amount_cents)];
      });
      body.appendChild(card(h, 'Prepare a batch', [
        h('p', {}, 'Select approved earnings to preview. Up to 200 are shown; drafting does not send money.'),
        table(h, ['Select', 'Physician', 'Earning', 'Amount'], choose),
        button(h, 'Create draft for review', function (b) {
          if (!selected.size) { feedback.textContent = 'Select at least one earning.'; return; }
          post('/batches', {earning_ids: Array.from(selected)}, b);
        }),
      ]));
      d.batches.forEach(function (batch) {
        var children = [h('p', {}, batch.stripe_mode.toUpperCase() + ' · ' + batch.status + ' · Total ' + money(batch.total_cents)),
          table(h, ['Physician', 'Stripe account', 'Amount', 'Transfer', 'Action needed'], batch.items.map(function (i) {
            return [i.email || i.user_id, i.destination, money(i.amount_cents), i.transfer_status || i.status, i.last_error || '—'];
          }))];
        if (batch.status === 'draft') {
          var accept = h('input', {type: 'checkbox'});
          var approval = button(h, 'Approve ' + money(batch.total_cents) + ' for payment', function (b) {
            post('/batches/' + encodeURIComponent(batch.batch_id) + '/approve', {fingerprint: batch.fingerprint}, b);
          });
          approval.disabled = true;
          accept.addEventListener('change', function () { approval.disabled = !accept.checked; });
          children.push(h('label', {}, accept, ' I reviewed these recipients and authorize this exact total.'));
          children.push(approval);
          children.push(button(h, 'Cancel draft', function (b) { post('/batches/' + encodeURIComponent(batch.batch_id) + '/cancel', {}, b); }));
        } else if (batch.status === 'approved' && batch.items.some(function (i) { return i.status === 'needs_attention'; })) {
          children.push(h('p', {}, 'Resolve the reported issue first. Retrying keeps the original recipients and amounts; expired or ambiguous intents may still require Stripe reconciliation.'));
          children.push(button(h, 'Retry unresolved approved payments', function (b) {
            post('/batches/' + encodeURIComponent(batch.batch_id) + '/retry', {fingerprint: batch.fingerprint}, b);
          }));
        }
        body.appendChild(card(h, 'Batch ' + batch.batch_id.slice(3, 11), children));
      });
      body.appendChild(button(h, 'Refresh payment status', function () { render(body, ctx); }));
      body.appendChild(card(h, 'Bank payouts', [
        h('p', {}, d.mode.toUpperCase() + ' environment. These show Stripe-to-bank deposits. A deposit may combine several earnings. A failed bank deposit must be resolved in Stripe; do not create another transfer.'),
        table(h, ['Stripe account', 'Payout', 'Amount', 'Currency', 'Bank status', 'Failure'], d.bank_payouts.map(function (p) {
          return [p.account_id, p.payout_id, (p.amount_cents / 100).toFixed(2), p.currency.toUpperCase(), p.status, p.failure_code || '—'];
        })),
        !d.bank_payouts.length ? h('p', {}, 'No bank payout events received yet. This does not establish that no payouts occurred.') : null,
      ]));
      body.appendChild(card(h, 'Tax information for connected physicians', [
        h('p', {}, 'Request missing 1099 information for a US connected account. Physicians complete the details in Stripe. This does not certify a W-9 or enable annual filing.'),
        table(h, ['Physician', 'Stripe account', 'Tax information'], (d.tax_accounts || []).map(function (a) {
          return [a.email, a.stripe_account_id, button(h, 'Request US tax details', function (b) {
            post('/physicians/' + encodeURIComponent(a.id) + '/tax-collection', {}, b, function () {
              feedback.textContent = 'Tax information requested. The physician can return to Set up payments to complete Stripe onboarding.';
            });
          })];
        })),
      ]));
      var year = h('input', {type: 'number', min: '2020', max: '2100', value: String(ready.tax_year), 'aria-label': 'Tax year'});
      var method = h('select', {'aria-label': 'Stripe calculation method'},
        h('option', {value: 'payments_including_fees'}, 'Payments including fees'),
        h('option', {value: 'payments_excluding_fees'}, 'Payments excluding fees'),
        h('option', {value: 'payouts_only'}, 'Payouts only'));
      var reviewFields = [
        ['platform_and_funding_checked', 'I verified platform approval and bank funding.'],
        ['tax_identity_and_w9_checked', 'I verified tax identity and W-9 collection.'],
        ['delivery_and_consent_checked', 'I verified electronic consent and postal delivery fallback.'],
        ['state_filing_checked', 'I verified applicable state filing settings.'],
      ];
      var checks = {};
      var labels = reviewFields.map(function (r) { var input = h('input', {type: 'checkbox'}); checks[r[0]] = input; return h('p', {}, h('label', {}, input, ' ' + r[1])); });
      body.appendChild(card(h, 'Annual setup review', [h('label', {}, 'Tax year ', year),
        h('p', {}, 'Form: 1099-NEC. Have your accountant confirm which payees require reporting.'),
        h('label', {}, 'Calculation method selected in Stripe ', method), labels,
        button(h, 'Record my review', function (b) {
          var payload = {tax_year: Number(year.value), form_type: '1099-NEC', calculation_method: method.value};
          reviewFields.forEach(function (r) { payload[r[0]] = checks[r[0]].checked; });
          post('/tax-review', payload, b);
        }),
      ]));
      var recYear = h('input', {type: 'number', min: '2020', max: '2100', value: String(ready.tax_year), 'aria-label': 'Reconciliation tax year'});
      var csv = h('textarea', {rows: '5', class: 'asc-ref-input', 'aria-label': 'Stripe form totals CSV', placeholder: 'stripe_account_id,amount_usd\nacct_example,1500.00'});
      var report = h('div', {role: 'status'});
      body.appendChild(card(h, 'Compare annual totals', [
        h('p', {}, 'From your Stripe 1099 drafts, prepare a two-column CSV of connected account IDs and form totals in USD. Exclude names, addresses and tax IDs.'),
        h('label', {}, 'Tax year ', recYear), csv,
        h('p', {}, 'The comparison uses the year Archangel recorded the payment decision. Review Stripe recognition dates, especially December/January, before filing.'),
        button(h, 'Compare and save report', function (b) {
          post('/reconciliation', {tax_year: Number(recYear.value), totals_csv: csv.value}, b, function (r) {
            ctx.clear(report);
            report.appendChild(h('p', {}, 'Report saved: ' + r.report_id + '. This comparison does not approve or file tax forms.'));
            report.appendChild(table(h, ['Account', 'Confirmed transfers', 'Unconfirmed / external', 'Reversed', 'Stripe 1099', 'Difference', 'Result'], r.rows.map(function (x) {
              return [x.stripe_account_id, money(x.confirmed_transfer_cents), money(x.unconfirmed_or_external_cents), money(x.reversed_cents), money(x.stripe_form_cents), money(x.difference_cents), x.status.replace(/_/g, ' ')];
            })));
            var exportButton = button(h, 'Download report', function () {
              var blob = new Blob([JSON.stringify(r, null, 2)], {type: 'application/json'});
              var url = URL.createObjectURL(blob); var a = document.createElement('a'); a.href = url; a.download = r.report_id + '.json'; a.click(); URL.revokeObjectURL(url);
            });
            report.appendChild(exportButton);
          });
        }), report,
      ]));
    }).catch(function (e) { ctx.clear(body); body.appendChild(card(ctx.h, 'Payments unavailable', ctx.h('p', {class: 'asc-inline-error'}, err(e)))); });
  }
  window.AdminPaymentOpsSection = {render: render};
}());
