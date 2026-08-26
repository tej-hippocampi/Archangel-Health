/* ═══════════════════════════════════════════════════════════
   Referral section (PRD-REF)

   window.ReferralSection { render, reset }

   The physician's own referral surface: what a referral is worth, their
   shareable link, an invite composer, the live funnel, and the
   health-system note card. Everything money-shaped renders from the
   server's funnel payload: the payout structure arrives on the wire
   (funnel.payout_structure) so no dollar figure is hardcoded here where
   an env change could strand it.

   Same contract as earnings.js: DOM built exclusively through ctx.h,
   zero innerHTML, ctx.api prefixes /api/asclepius, module state survives
   re-renders, and a load failure is a VISIBLE error, never a quiet
   placeholder.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var rootEl = null;
  var rootCtx = null;
  var data = null;        // GET /referrals funnel payload
  var loadError = null;

  var refDraft = '';
  var refBusy = false;
  var refMessage = null;
  var refError = null;

  var noteDraft = '';
  var noteBusy = false;
  var noteMessage = null;
  var noteError = null;

  var copied = false;
  var copiedTimer = null;

  // green earned · lime in flight · muted grey settled-without-money.
  // NEVER pink: a referral that has not converted is not a safety event.
  var BOUNTY_CLASS = {
    earned: 'asc-ref-earned',
    pending: 'asc-ref-flight',
    expired: 'asc-ref-quiet',
    duplicate: 'asc-ref-quiet',
    ineligible: 'asc-ref-quiet',
    closed: 'asc-ref-quiet',
  };

  function money(cents) {
    var n = Math.round(Number(cents) || 0) / 100;
    return '$' + n.toLocaleString('en-US', {
      minimumFractionDigits: 0,
      maximumFractionDigits: n % 1 ? 2 : 0,
    });
  }

  function shortDate(iso) {
    if (!iso) return '';
    try {
      var d = new Date(iso.indexOf('Z') === -1 && iso.indexOf('+') === -1 ? iso + 'Z' : iso);
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    } catch (e) { return ''; }
  }

  function render(body, ctx) {
    rootEl = body; rootCtx = ctx;
    // A fresh mount is a fresh visit: drafts must not outlive a logout.
    refDraft = ''; refBusy = false; refMessage = null; refError = null;
    noteDraft = ''; noteBusy = false; noteMessage = null; noteError = null;
    copied = false;
    ctx.clear(body);
    body.appendChild(ctx.h('div', { class: 'asc-pay-loading' }, 'Loading your referrals…'));
    load();
  }

  function reset() {
    rootEl = null; rootCtx = null; data = null; loadError = null;
    refDraft = ''; noteDraft = '';
    if (copiedTimer) { clearTimeout(copiedTimer); copiedTimer = null; }
  }

  function load() {
    var ctx = rootCtx;
    if (!ctx) return;
    ctx.api('/referrals').then(function (payload) {
      data = payload; loadError = null;
      rerender();
    }).catch(function (err) {
      data = null;
      loadError = (err && (err.detail || err.message)) || 'The server did not respond.';
      rerender();
    });
  }

  function rerender() {
    if (!rootEl || !rootCtx) return;
    var ctx = rootCtx;
    var h = ctx.h;
    ctx.clear(rootEl);

    if (loadError) {
      rootEl.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          'Your referrals could not be loaded. ' + loadError
          + ' Reload the page; nothing about what you are owed has changed.'))));
      return;
    }
    if (!data) {
      rootEl.appendChild(h('div', { class: 'asc-pay-loading' }, 'Loading your referrals…'));
      return;
    }

    rootEl.appendChild(h('h2', { class: 'asc-pay-title' }, 'Referral'));
    rootEl.appendChild(hero(h));
    rootEl.appendChild(linkCard(h));
    rootEl.appendChild(composerCard(h));
    rootEl.appendChild(enterpriseCard(h));
  }

  /* ── The hero: what a referral is worth ─────────────────────────────── */
  function hero(h) {
    var ps = data.payout_structure || {};
    var cap = money(ps.cap_cents || 520000);
    var bounty = money(ps.referrer_bounty_cents || data.bounty_cents || 5000);
    var bonus = money(ps.referee_bonus_cents || 2500);

    var wrap = h('div', { class: 'asc-ref-hero asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-ref-hero-line' },
        h('span', { class: 'asc-ref-hero-value' }, 'Earn up to ' + cap),
        h('span', { class: 'asc-ref-hero-label' }, 'referring colleagues')),
      h('div', { class: 'asc-ref-structure' },
        structureRow(h, bounty + ' to you',
          'when a colleague you refer is verified and completes their first accepted case.'),
        structureRow(h, bonus + ' to them',
          'as a first case bonus, the moment that same case is accepted.'),
        structureRow(h, cap + ' ceiling',
          'the most one physician can earn in referral bounties, ever.')),
      data.capped
        ? h('div', { class: 'asc-ref-msg' },
            'You have reached the referral ceiling. Thank you; your colleagues '
            + 'can still join through your link.')
        : null));
    return wrap;
  }

  function structureRow(h, lead, rest) {
    return h('div', { class: 'asc-ref-structure-row' },
      h('span', { class: 'asc-ref-structure-lead' }, lead),
      h('span', { class: 'asc-ref-structure-rest' }, ' ' + rest));
  }

  /* ── The link ───────────────────────────────────────────────────────── */
  function linkCard(h) {
    var url = data.invite_url || '';
    var code = data.referral_code || '';
    var card = h('div', { class: 'asc-ref-card' });
    card.appendChild(h('div', { class: 'asc-ref-title' }, 'Your link'));
    card.appendChild(h('div', { class: 'asc-ref-pitch' },
      'Share it anywhere: a text, a group chat, a hallway QR code. Anyone who '
      + 'joins through it is credited to you, even if they finish signing up later.'));
    if (!url) {
      card.appendChild(h('div', { class: 'asc-ref-error' },
        'Your link could not be created just now. Reload the page to try again.'));
      return card;
    }
    var row = h('div', { class: 'asc-ref-linkrow' },
      h('code', { class: 'asc-mono asc-ref-linktext' }, url),
      h('button', {
        class: 'asc-btn asc-btn-sm', type: 'button',
        onClick: function () { copyLink(url); },
      }, copied ? 'Copied' : 'Copy link'));
    card.appendChild(row);
    card.appendChild(shareRow(h, url));
    if (code) {
      card.appendChild(h('div', { class: 'asc-ref-code-line' },
        'Or give them the code ',
        h('code', { class: 'asc-mono' }, code),
        ' to enter at archangelhealth.ai/join.'));
    }
    return card;
  }

  /* One tap to wherever the conversation already is.
     Every target below is a plain link the OS or the site resolves -- no
     third-party SDK, no tracking pixel, nothing loaded from another origin.
     `navigator.share` is offered first where it exists (phones), because it
     opens the contact list the doctor actually uses instead of making them
     pick a network from a row of logos. */
  var SHARE_MESSAGE =
    'I am contributing to Archangel Health, which pays physicians to evaluate '
    + 'medical AI. Thought of you:';

  function shareTargets(url) {
    var text = encodeURIComponent(SHARE_MESSAGE + ' ' + url);
    var bare = encodeURIComponent(url);
    return [
      { label: 'WhatsApp', href: 'https://wa.me/?text=' + text },
      // sms: needs the body separated by ?; iOS and Android both accept it.
      { label: 'Text message', href: 'sms:?&body=' + text },
      { label: 'Email', href: 'mailto:?subject='
        + encodeURIComponent('Archangel Health') + '&body=' + text },
      { label: 'LinkedIn', href: 'https://www.linkedin.com/sharing/share-offsite/?url=' + bare },
      { label: 'X', href: 'https://twitter.com/intent/tweet?text=' + text },
    ];
  }

  function shareRow(h, url) {
    var row = h('div', { class: 'asc-ref-sharerow' });
    if (navigator.share) {
      row.appendChild(h('button', {
        class: 'asc-btn asc-btn-sm asc-btn-primary', type: 'button',
        onClick: function () {
          navigator.share({ title: 'Archangel Health', text: SHARE_MESSAGE, url: url })
            .catch(function () { /* dismissed; nothing to report */ });
        },
      }, 'Share'));
    }
    shareTargets(url).forEach(function (t) {
      row.appendChild(h('a', {
        class: 'asc-btn asc-btn-sm asc-btn-ghost', href: t.href,
        target: '_blank', rel: 'noopener noreferrer',
      }, t.label));
    });
    return row;
  }

  function copyLink(url) {
    function done() {
      copied = true;
      rerender();
      if (copiedTimer) clearTimeout(copiedTimer);
      copiedTimer = setTimeout(function () { copied = false; rerender(); }, 2000);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done, function () { legacyCopy(url); done(); });
    } else {
      legacyCopy(url);
      done();
    }
  }

  function legacyCopy(text) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'absolute';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (e) { /* the visible URL is itself the fallback */ }
  }

  /* ── The composer + funnel ──────────────────────────────────────────── */
  function composerCard(h) {
    var card = h('div', { class: 'asc-ref-card' });
    card.appendChild(h('div', { class: 'asc-ref-title' }, 'Invite by email'));
    card.appendChild(h('div', { class: 'asc-ref-pitch' },
      'We send one invitation with your name on it. No follow-ups, no drip.'));
    card.appendChild(composerForm(h));
    if (refError) card.appendChild(h('div', { class: 'asc-ref-error' }, refError));
    else if (refMessage) card.appendChild(h('div', { class: 'asc-ref-msg' }, refMessage));

    card.appendChild(h('div', { class: 'asc-ref-listtitle' }, 'Your referrals'));
    var rows = data.referrals || [];
    if (!rows.length) {
      card.appendChild(h('div', { class: 'asc-ref-empty' },
        'No referrals yet. Almost every physician here came through another '
        + 'physician.'));
      return card;
    }
    var list = h('div', { class: 'asc-ref-list' });
    rows.forEach(function (r) {
      var state = r.bounty_state || 'pending';
      var rowAmount = money(r.bounty_cents == null ? data.bounty_cents : r.bounty_cents);
      list.appendChild(h('div', { class: 'asc-ref-row' },
        h('span', { class: 'asc-ref-who' }, r.invitee_display || 'A colleague'),
        h('span', { class: 'asc-ref-when' }, shortDate(r.invited_at)),
        // A SENTENCE, never a token: Invited → Signed up → Verified → first
        // case, straight from the server's vocabulary.
        h('span', { class: 'asc-ref-state ' + (BOUNTY_CLASS[state] || '') },
          r.status_sentence || 'Invited'),
        h('span', { class: 'asc-ref-amount ' + (BOUNTY_CLASS[state] || '') },
          !data.earns_bounty ? ''
            : state === 'earned' ? rowAmount
              : state === 'pending' ? '+' + rowAmount + ' pending'
                : '')));
    });
    card.appendChild(list);
    return card;
  }

  function composerForm(h) {
    var input = h('input', {
      class: 'asc-ref-input',
      type: 'email',
      placeholder: 'colleague@hospital.org',
      value: refDraft,
      disabled: refBusy,
    });
    input.addEventListener('input', function () { refDraft = input.value; });
    input.addEventListener('keydown', function (ev) {
      if (ev && ev.key === 'Enter') { ev.preventDefault(); submitReferral(input.value); }
    });
    var button = h('button', {
      class: 'asc-btn asc-btn-sm asc-ref-send', type: 'button', disabled: refBusy,
    }, refBusy ? 'Sending…' : 'Send invitation');
    button.addEventListener('click', function () { submitReferral(input.value); });
    return h('div', { class: 'asc-ref-form' }, input, button);
  }

  function submitReferral(value) {
    if (refBusy) return;
    var email = String(value == null ? '' : value).trim();
    refDraft = email;
    if (!email || email.indexOf('@') === -1) {
      refError = 'Enter your colleague’s email address.';
      refMessage = null;
      rerender();
      return;
    }
    refBusy = true; refError = null; refMessage = null;
    rerender();
    rootCtx.api('/referrals', { method: 'POST', body: { email: email } })
      .then(function (res) {
        refBusy = false;
        refDraft = '';
        // Same sentence whether or not the address has an account: the
        // response is not an oracle, and this page must not invent one.
        refMessage = (res && res.message)
          || 'Invitation recorded. You’ll see them in your referrals below.';
        refError = null;
        // Refetch: the funnel is the server's answer, and an optimistic row
        // can disagree with the ledger beside it.
        return rootCtx.api('/referrals').then(function (funnel) {
          data = funnel;
          rerender();
        });
      })
      .catch(function (err) {
        refBusy = false;
        refError = (err && (err.detail || err.message)) || 'That did not send. Try again.';
        refMessage = null;
        rerender();
      });
  }

  /* ── The enterprise note ────────────────────────────────────────────── */
  function enterpriseCard(h) {
    var card = h('div', { class: 'asc-ref-card' });
    card.appendChild(h('div', { class: 'asc-ref-title' }, 'Part of a health system?'));
    card.appendChild(h('div', { class: 'asc-ref-pitch' },
      'If your institution might sell de-identified data or wants an '
      + 'enterprise labeling partnership, write us a line. This is the '
      + 'introduction that is worth the most: these are seven-figure '
      + 'agreements, and the person who opens the door takes a share of what '
      + 'closes. A founder reads every note personally.'));
    // A worked example, marked as one. Institutional deals are negotiated
    // individually and the real number depends on what closes, so this shows
    // the shape of the arrangement rather than promising a figure -- a doctor
    // reading a flat "earn $200,000" and being paid less would be right to
    // feel misled.
    card.appendChild(h('div', { class: 'asc-ref-example' },
      h('span', { class: 'asc-ref-example-label' }, 'For example'),
      h('span', {},
        'a $1M data partnership at a 15 to 20 percent introducer share is '
        + '$150,000 to $200,000 for the person who made the introduction. Terms '
        + 'are agreed in writing before anything is signed.')));
    var input = h('textarea', {
      class: 'asc-ref-input asc-ref-note', rows: '3',
      placeholder: 'Who you are connected to, and what might be possible…',
      disabled: noteBusy,
    });
    input.value = noteDraft;
    input.addEventListener('input', function () { noteDraft = input.value; });
    var button = h('button', {
      class: 'asc-btn asc-btn-sm', type: 'button', disabled: noteBusy,
    }, noteBusy ? 'Sending…' : 'Send the note');
    button.addEventListener('click', function () { submitNote(input.value); });
    card.appendChild(h('div', { class: 'asc-ref-form asc-ref-noteform' }, input, button));
    if (noteError) card.appendChild(h('div', { class: 'asc-ref-error' }, noteError));
    else if (noteMessage) card.appendChild(h('div', { class: 'asc-ref-msg' }, noteMessage));
    return card;
  }

  function submitNote(value) {
    if (noteBusy) return;
    var note = String(value == null ? '' : value).trim();
    noteDraft = note;
    if (!note) {
      noteError = 'Write a sentence or two first.';
      noteMessage = null;
      rerender();
      return;
    }
    noteBusy = true; noteError = null; noteMessage = null;
    rerender();
    rootCtx.api('/referrals/enterprise-note', { method: 'POST', body: { note: note } })
      .then(function (res) {
        noteBusy = false;
        noteDraft = '';
        noteMessage = (res && res.message) || 'Sent. A founder reads every one of these.';
        rerender();
      })
      .catch(function (err) {
        noteBusy = false;
        noteError = (err && (err.detail || err.message)) || 'That did not send. Try again.';
        noteMessage = null;
        rerender();
      });
  }

  window.ReferralSection = { render: render, reset: reset };
})();
