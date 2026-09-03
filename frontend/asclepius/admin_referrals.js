/* ═══════════════════════════════════════════════════════════════════════════
   Admin · Health-system introductions (PRD-F U13)

   A physician introduces us to the health system they work in. The funnel has
   six stages and the first four stamp themselves: the invite email SENDS, the
   landing page is OPENED, the partner form is SUBMITTED, a call is BOOKED. The
   last two cannot stamp themselves, because they happen in a meeting and in a
   contract. A human records them.

   That human had no screen. `/admin/hs-referrals`, `/advance` and `/reward`
   shipped with no client at all, so `met` and `signed` were unreachable
   without curl, and every reward an admin had already paid was invisible
   afterwards. This file is those three endpoints, and nothing else: it adds no
   rule the server does not already enforce.

   TWO THINGS IT REFUSES TO DECIDE. The stage order is forward-only and the
   store enforces it, so this offers only stages AHEAD of the current one
   rather than a free dropdown that posts a 422 back at the operator. And the
   reward amount is typed every time: institutional terms are negotiated one
   deal at a time, there is no rate to derive, and a prefilled number in this
   box would be this file inventing one.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Same tuple as store.HS_REFERRAL_STAGES, in the same order. Duplicated here
  // as LABELS, not as authority: the server re-checks membership and ordering
  // on every advance, and a stage this list got wrong is refused there.
  const STAGES = [
    ['sent', 'Invite sent'],
    ['opened', 'Page opened'],
    ['submitted', 'Form submitted'],
    ['booked', 'Call booked'],
    ['met', 'Met'],
    ['signed', 'Signed'],
  ];
  const ORDER = STAGES.map((s) => s[0]);

  let view = { rows: null, err: null, busy: false, open: null };

  function label(status) {
    const hit = STAGES.filter((s) => s[0] === status)[0];
    return hit ? hit[1] : 'Not started';
  }

  function render(body, ctx) {
    const { h, clear } = ctx;
    clear(body);
    const host = h('div', {});
    body.appendChild(host);
    paint(host, ctx);
    if (!view.rows && !view.err) load(host, ctx);
  }

  function load(host, ctx) {
    view.busy = true;
    paint(host, ctx);
    ctx.api('/admin/hs-referrals').then((d) => {
      view.rows = (d && d.referrals) || [];
      view.err = null;
    }).catch((e) => {
      view.rows = null;
      view.err = (e && e.message) || 'Could not load the introductions.';
    }).then(() => {
      view.busy = false;
      paint(host, ctx);
    });
  }

  function paint(host, ctx) {
    const { h, clear } = ctx;
    clear(host);
    if (view.busy && !view.rows) { host.appendChild(ctx.loadingCard('Loading introductions…')); return; }
    if (view.err) {
      host.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, view.err))));
      return;
    }
    const rows = view.rows || [];
    host.appendChild(funnelCard(ctx, rows));
    if (!rows.length) {
      host.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty-line' },
          'No physician has introduced a health system yet. The invite lives on '
          + 'their Referral tab.'))));
      return;
    }
    host.appendChild(h('div', { class: 'asc-hsref-list' },
      rows.map((r) => introRow(ctx, r, host))));
  }

  /* The funnel, counted. Cumulative rather than per-stage: a deal that SIGNED
   * was also met and also booked, and a bar chart that drops it out of the
   * earlier stages makes a healthy funnel look like a leaking one. */
  function funnelCard(ctx, rows) {
    const { h } = ctx;
    const reached = ORDER.map((stage, i) => rows.filter((r) => {
      const at = ORDER.indexOf(r.status || '');
      return at >= i;
    }).length);
    const top = Math.max(1, reached[0] || 0);
    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Introduction funnel'),
        h('div', { class: 'asc-card-sub' },
          'Every health system a physician has introduced us to. The last two '
          + 'stages are recorded by a person, because they happen in a meeting '
          + 'and in a contract.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-hsref-funnel' },
          STAGES.map(([stage, text], i) => h('div', { class: 'asc-hsref-stage' },
            h('div', { class: 'asc-hsref-stage-n doto' }, String(reached[i])),
            h('div', { class: 'asc-hsref-stage-label chrome' }, text),
            h('div', { class: 'asc-hsref-stage-bar' },
              h('span', {
                class: 'asc-hsref-stage-fill',
                style: 'width:' + Math.round((reached[i] / top) * 100) + '%',
              })))))));
  }

  function introRow(ctx, r, host) {
    const { h } = ctx;
    const at = ORDER.indexOf(r.status || '');
    const paid = (r.reward_state || '') === 'paid';
    const head = h('div', { class: 'asc-hsref-head' },
      h('div', { class: 'asc-hsref-who' },
        h('div', { class: 'asc-hsref-hs' }, r.hs_name || 'Unnamed health system'),
        h('div', { class: 'asc-hsref-contact' },
          [r.contact_name, r.contact_role, r.contact_email].filter(Boolean).join(' · '))),
      h('div', { class: 'asc-hsref-meta' },
        h('span', { class: 'asc-badge ' + (at >= 4 ? 'asc-badge-green' : 'asc-badge-gray') },
          label(r.status)),
        // A reward that was paid is the whole reason an operator opens this
        // screen twice. It was recorded and then invisible.
        paid ? h('span', { class: 'asc-badge asc-badge-lime' }, 'Reward paid') : null,
        r.fraud_flag ? h('span', { class: 'asc-badge asc-badge-red' }, 'Flagged') : null));

    const referrer = h('div', { class: 'asc-hsref-referrer' },
      h('span', { class: 'chrome' }, 'Introduced by'), ' ',
      (r.referrer_name || r.referrer_email || 'a physician'),
      r.relationship ? h('span', { class: 'asc-hsref-rel' }, ' · ' + r.relationship) : null,
      h('span', { class: 'asc-hsref-when' }, ' · ' + ctx.fmtDate(r.invited_at)));

    const status = h('div', { class: 'asc-hsref-status' });
    const actions = h('div', { class: 'asc-hsref-actions' });

    // Only stages ahead of where the deal already is. The store refuses a
    // backward move, so offering one would be a control that always fails.
    const ahead = ORDER.slice(at + 1);
    if (ahead.length) {
      const sel = h('select', { class: 'asc-input asc-hsref-select', 'aria-label': 'Next stage' },
        ahead.map((s) => h('option', { value: s }, label(s))));
      const go = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' }, 'Record stage');
      go.addEventListener('click', () => {
        go.setAttribute('disabled', '');
        ctx.clear(status);
        ctx.api('/admin/hs-referrals/' + encodeURIComponent(r.hs_referral_id) + '/advance',
                { method: 'POST', body: { status: sel.value } })
          .then(() => { ctx.toast('Recorded: ' + label(sel.value) + '.', 'success'); load(host, ctx); })
          .catch((e) => {
            go.removeAttribute('disabled');
            status.appendChild(h('div', { class: 'asc-inline-error' },
              (e && e.message) || 'Could not record that stage.'));
          });
      });
      actions.appendChild(sel);
      actions.appendChild(go);
    } else {
      actions.appendChild(h('span', { class: 'asc-hsref-done chrome' }, 'Funnel complete'));
    }

    if (!paid) {
      const amount = h('input', {
        class: 'asc-input asc-hsref-amount', type: 'number', min: '1', step: '1',
        placeholder: 'Amount in dollars', 'aria-label': 'Reward amount in dollars',
      });
      const note = h('input', {
        class: 'asc-input asc-hsref-note', type: 'text',
        placeholder: 'What this is for (optional)', 'aria-label': 'Reward note',
      });
      const pay = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm' }, 'Record reward');
      pay.addEventListener('click', () => {
        const dollars = Number(amount.value);
        if (!dollars || dollars <= 0) {
          ctx.clear(status);
          status.appendChild(h('div', { class: 'asc-inline-error' },
            'Enter the amount agreed for this introduction. There is no rate to '
            + 'default to: institutional terms are negotiated one deal at a time.'));
          return;
        }
        pay.setAttribute('disabled', '');
        ctx.clear(status);
        ctx.api('/admin/hs-referrals/' + encodeURIComponent(r.hs_referral_id) + '/reward',
                { method: 'POST', body: { amount_cents: Math.round(dollars * 100), note: note.value } })
          .then((res) => {
            ctx.toast(res && res.already ? 'Already rewarded.' : 'Reward recorded on the ledger.',
                      'success');
            load(host, ctx);
          })
          .catch((e) => {
            pay.removeAttribute('disabled');
            status.appendChild(h('div', { class: 'asc-inline-error' },
              (e && e.message) || 'Could not record the reward.'));
          });
      });
      actions.appendChild(amount);
      actions.appendChild(note);
      actions.appendChild(pay);
    }

    return h('div', { class: 'asc-card asc-hsref-row' },
      h('div', { class: 'asc-card-pad' },
        head, referrer,
        r.note ? h('div', { class: 'asc-hsref-note-text' }, r.note) : null,
        actions, status));
  }

  window.AdminReferralsSection = {
    render: render,
    reset() { view = { rows: null, err: null, busy: false, open: null }; },
  };
})();
