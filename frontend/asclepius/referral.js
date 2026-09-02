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

  // The health-system card is a real form now, so its fields are module state
  // like every other draft here: a re-render must not wipe half-typed input.
  var hsDraft = {
    contact_name: '', contact_email: '', contact_role: '',
    hs_name: '', relationship: '', note: '',
  };
  var hsConsent = false;

  // WHICH button says "Copied", not WHETHER one does. There are four copy
  // controls on this page now; a shared boolean lit them all at once, so
  // copying the physician link also told you the health-system blurb was on
  // your clipboard when it was not.
  var copiedKey = null;
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
    hsDraft = { contact_name: '', contact_email: '', contact_role: '',
                hs_name: '', relationship: '', note: '' };
    hsConsent = false;
    copiedKey = null;
    ctx.clear(body);
    body.appendChild(ctx.h('div', { class: 'asc-pay-loading' }, 'Loading your referrals…'));
    load();
  }

  function reset() {
    rootEl = null; rootCtx = null; data = null; loadError = null;
    refDraft = ''; noteDraft = '';
    hsDraft = { contact_name: '', contact_email: '', contact_role: '',
                hs_name: '', relationship: '', note: '' };
    hsConsent = false; copiedKey = null;
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
    // The split is the design: a link you paste into a group chat on the left,
    // a note a founder reads on the right. Stacked, the second one read as a
    // footnote to the first, and it is the larger of the two asks.
    rootEl.appendChild(h('div', { class: 'asc-ref-split' },
      physicianCol(h), systemCol(h)));
  }

  /* ── The hero: what a referral is worth ─────────────────────────────── */
  /* One line, two terms, then the link. Six prose blocks used to stand between
     a physician and the copy button, and every one of them was explaining a
     deal the two terms state in nine words. A doctor who opened this tab had
     already decided to refer someone; the page's only job is to hand them the
     link before they change their mind.

     No ceiling figure either. This used to lead with "Earn up to $5,200" in the
     largest type on the page, which put a limit in front of the one physician
     we most want introducing us to a hundred colleagues.

     The two live rates still come off the WIRE (payout_structure): an env
     change has to move this page with no frontend edit, and that contract
     predates the redesign. */
  function hero(h) {
    var ps = data.payout_structure || {};
    var bounty = money(ps.referrer_bounty_cents || data.bounty_cents || 2500);
    var bonus = money(ps.referee_bonus_cents || 5000);
    var earns = data.earns_bounty !== false;

    return h('div', { class: 'asc-ref-hero asc-card' },
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-ref-hero-line' },
          h('span', { class: 'asc-ref-hero-value' }, 'Earn thousands'),
          h('span', { class: 'asc-ref-hero-label' },
            'referring physicians, hospitals and health systems')),
        h('div', { class: 'asc-ref-terms' },
          term(h, bounty, 'to you',
            'when a physician you refer has their first case accepted.'),
          term(h, bonus, 'to them', 'on that same case.')),
        // An equity-compensated account still refers, and the funnel already
        // blanks their amounts. One line, but it cannot go: without it the two
        // terms above read as a promise to an account that accrues nothing.
        earns ? null : h('div', { class: 'asc-ref-foot' },
          'Your account is paid in equity, so referrals are recorded here but '
          + 'no bounty accrues.')));
  }

  function term(h, value, unit, rest) {
    // The unit carries no leading space: the gap is CSS (.asc-ref-term-unit
    // margin-left). Significant whitespace inside a string is invisible to
    // anyone editing the copy later and doubles up under any text extractor
    // that joins child nodes.
    return h('div', { class: 'asc-ref-term' },
      h('div', { class: 'asc-ref-term-value' }, value,
        unit ? h('span', { class: 'asc-ref-term-unit' }, unit) : null),
      h('div', { class: 'asc-ref-term-rest' }, rest));
  }

  /* ── Left: invite a physician ───────────────────────────────────────── */
  function physicianCol(h) {
    var col = h('div', { class: 'asc-ref-card asc-ref-col' });
    // No pitch paragraph. It explained that a link credits the person who
    // shared it, which is the only thing a link could possibly do, and it cost
    // the copy button its place at the top of the column.
    col.appendChild(h('div', { class: 'asc-ref-title' }, 'Invite a physician'));

    var url = data.invite_url || '';
    if (!url) {
      col.appendChild(h('div', { class: 'asc-ref-error' },
        'Your link could not be created just now. Reload the page to try again.'));
    } else {
      col.appendChild(h('div', { class: 'asc-ref-linkrow' },
        h('code', { class: 'asc-mono asc-ref-linktext' }, url),
        h('button', {
          class: 'asc-btn asc-btn-sm asc-btn-go asc-ref-copy', type: 'button',
          onClick: function () { copyText('phys-link', url); },
        }, copiedKey === 'phys-link' ? 'Copied' : 'Copy link')));
      col.appendChild(shareRow(h, url));
      col.appendChild(copyBlock(h, 'phys-msg', physicianMessage(url),
        'Copy the message'));
    }

    col.appendChild(composerForm(h));
    if (refError) col.appendChild(h('div', { class: 'asc-ref-error' }, refError));
    else if (refMessage) col.appendChild(h('div', { class: 'asc-ref-msg' }, refMessage));
    col.appendChild(funnelBlock(h));
    return col;
  }

  /* The funnel scrolls INSIDE the column so the health-system half beside it
     stays above the fold. The count sits outside the scroll box, because a
     physician with eleven referrals must be able to see that it is eleven
     without scrolling to find out. */
  function funnelBlock(h) {
    var rows = data.referrals || [];
    var wrap = h('div', { class: 'asc-ref-funnel' });
    wrap.appendChild(h('div', { class: 'asc-ref-listhead' },
      h('span', { class: 'asc-ref-listtitle' }, 'Your referrals'),
      h('span', { class: 'asc-ref-listcount' },
        rows.length
          ? String(rows.length) + (data.pending_count
              ? ' · ' + data.pending_count + ' open' : '')
          : '')));
    if (!rows.length) {
      wrap.appendChild(h('div', { class: 'asc-ref-empty' },
        'No referrals yet. Almost every physician here came through another '
        + 'physician.'));
      return wrap;
    }
    var list = h('div', { class: 'asc-ref-list' });
    rows.forEach(function (r) {
      var state = r.bounty_state || 'pending';
      var cls = BOUNTY_CLASS[state] || '';
      var amt = money(r.bounty_cents == null ? data.bounty_cents : r.bounty_cents);
      list.appendChild(h('div', { class: 'asc-ref-row' },
        h('span', { class: 'asc-ref-who' }, r.invitee_display || 'A colleague'),
        h('span', { class: 'asc-ref-when' }, shortDate(r.invited_at)),
        h('span', { class: 'asc-ref-state ' + cls }, r.status_sentence || 'Invited'),
        h('span', { class: 'asc-ref-amount ' + cls },
          !data.earns_bounty ? ''
            : state === 'earned' ? amt
              : state === 'pending' ? '+' + amt + ' pending'
                : '')));
    });
    wrap.appendChild(h('div', { class: 'asc-ref-listwrap' }, list));
    return wrap;
  }

  var SHARE_MESSAGE =
    'I am contributing to Archangel Health, which pays physicians to evaluate '
    + 'medical AI. Thought of you:';

  /* The copyable messages. Two of them, because the two asks are not the same
     ask: a colleague is being invited to do paid work, and a health system is
     being invited into a commercial conversation. One blurb covering both
     would be vague about each.

     No figure appears in the health-system one. That is the same rule the card
     below follows and REFERRALS.md records: institutional terms are negotiated
     one deal at a time, so a number pasted into a group chat becomes a promise
     the negotiation then has to keep. */
  function physicianMessage(url) {
    return 'I have been doing evaluation work for Archangel Health. You compare '
      + 'two model answers on a case, write the ideal one, and grade the '
      + 'reasoning step by step, in your own specialty. It is paid, remote and '
      + 'async, and you set your own load. Thought of you:\n' + url;
  }

  function healthSystemMessage() {
    return 'I work with Archangel Health, who build the physician-graded data '
      + 'frontier AI labs use to evaluate medical models. They work with health '
      + 'systems two ways: licensing properly de-identified records (Expert '
      + 'Determination, no PHI on their side, DUA and BAA ready), and paying '
      + 'your physicians for evaluation work in their own specialty. Worth a '
      + 'short call?\n' + (data.partner_url || '');
  }

  /* Brand marks. Single-path glyphs where the company publishes one
     (WhatsApp, LinkedIn, X) and house-drawn strokes where none applies: an
     sms: and a mailto: link open whatever the doctor's device decides, so
     borrowing one messaging app's logo would be wrong on half of them.
     Colour comes from CSS through currentColor, see --brand-* in _tokens.css. */
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var BRAND_MARKS = {
    whatsapp: {
      viewBox: '0 0 24 24',
      paths: [{ d: 'M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.872.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 0 0-3.48-8.413z' }],
    },
    linkedin: {
      viewBox: '0 0 24 24',
      paths: [{ d: 'M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z' }],
    },
    x: {
      viewBox: '0 0 24 24',
      paths: [{ d: 'M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932zM17.61 20.644h2.039L6.486 3.24H4.298z' }],
    },
    email: {
      viewBox: '0 0 20 20', stroke: true,
      paths: [
        { d: 'M3.4 4.9h13.2a1.65 1.65 0 0 1 1.65 1.65v6.9a1.65 1.65 0 0 1-1.65 1.65H3.4a1.65 1.65 0 0 1-1.65-1.65v-6.9A1.65 1.65 0 0 1 3.4 4.9Z', w: '1.5' },
        { d: 'm2.1 6.2 7.02 4.34a1.65 1.65 0 0 0 1.76 0L17.9 6.2', w: '1.5' },
      ],
    },
    sms: {
      viewBox: '0 0 20 20', stroke: true,
      paths: [
        { d: 'M4 15V6a1.5 1.5 0 0 1 1.5-1.5h9A1.5 1.5 0 0 1 16 6v5.5a1.5 1.5 0 0 1-1.5 1.5H7l-3 2.5z', w: '1.5' },
        { d: 'M7.5 8.5h5', w: '1.4' },
        { d: 'M7.5 10.5h3', w: '1.4' },
      ],
    },
  };

  /* Built node by node. This module's contract is zero innerHTML, enforced by
     test_no_innerhtml_and_no_long_dashes_in_the_copy, so a brand mark cannot
     arrive as a string of markup the way the rail icons do. */
  function svgIcon(spec) {
    var make = document.createElementNS
      ? function (t) { return document.createElementNS(SVG_NS, t); }
      : function (t) { return document.createElement(t); };
    var svg = make('svg');
    // setAttribute, not .className: on an SVGElement className is a read-only
    // SVGAnimatedString and assigning to it silently does nothing.
    svg.setAttribute('class', 'asc-ref-share-ico');
    svg.setAttribute('viewBox', spec.viewBox);
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    spec.paths.forEach(function (p) {
      var node = make('path');
      node.setAttribute('d', p.d);
      if (spec.stroke) {
        node.setAttribute('fill', 'none');
        node.setAttribute('stroke', 'currentColor');
        node.setAttribute('stroke-width', p.w || '1.5');
        node.setAttribute('stroke-linecap', 'round');
        node.setAttribute('stroke-linejoin', 'round');
      } else {
        node.setAttribute('fill', 'currentColor');
      }
      svg.appendChild(node);
    });
    return svg;
  }

  function shareTargets(url) {
    var text = encodeURIComponent(SHARE_MESSAGE + ' ' + url);
    var bare = encodeURIComponent(url);
    return [
      // `cls` written out rather than concatenated from `key`: a class name
      // built at runtime is invisible to a grep and to the repo's
      // styled-but-never-emitted CSS scanner.
      { key: 'whatsapp', cls: 'asc-ref-share-whatsapp', label: 'WhatsApp',
        href: 'https://wa.me/?text=' + text },
      // sms: needs the body separated by ?; iOS and Android both accept it.
      { key: 'sms', cls: 'asc-ref-share-sms', label: 'Text message',
        href: 'sms:?&body=' + text },
      { key: 'email', cls: 'asc-ref-share-email', label: 'Email',
        href: 'mailto:?subject=' + encodeURIComponent('Archangel Health') + '&body=' + text },
      { key: 'linkedin', cls: 'asc-ref-share-linkedin', label: 'LinkedIn',
        href: 'https://www.linkedin.com/sharing/share-offsite/?url=' + bare },
      { key: 'x', cls: 'asc-ref-share-x', label: 'X',
        href: 'https://twitter.com/intent/tweet?text=' + text },
    ];
  }

  /* One tap to wherever the conversation already is. Every target is a plain
     link the OS or the site resolves: no third-party SDK, no tracking pixel,
     nothing loaded from another origin. navigator.share goes first where it
     exists, because it opens the contact list the doctor actually uses instead
     of making them pick a network from a row of logos. */
  function shareRow(h, url) {
    var row = h('div', { class: 'asc-ref-sharerow' });
    if (navigator.share) {
      row.appendChild(h('button', {
        class: 'asc-btn asc-btn-sm asc-btn-ghost asc-ref-share', type: 'button',
        onClick: function () {
          navigator.share({ title: 'Archangel Health', text: SHARE_MESSAGE, url: url })
            .catch(function () { /* dismissed; nothing to report */ });
        },
      }, 'Share'));
    }
    shareTargets(url).forEach(function (t) {
      row.appendChild(h('a', {
        class: 'asc-btn asc-btn-sm asc-btn-ghost asc-ref-share ' + t.cls,
        href: t.href, target: '_blank', rel: 'noopener noreferrer',
      }, svgIcon(BRAND_MARKS[t.key]), h('span', {}, t.label)));
    });
    return row;
  }

  function copyText(key, text) {
    function done() {
      copiedKey = key;
      rerender();
      if (copiedTimer) clearTimeout(copiedTimer);
      copiedTimer = setTimeout(function () { copiedKey = null; rerender(); }, 2000);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { legacyCopy(text); done(); });
    } else {
      legacyCopy(text);
      done();
    }
  }

  /* A link with the context already attached.

     "Copy link" hands over a URL and nothing else, so the physician has to
     write the explanation themselves every time, and the version they write
     at 11pm is not the version we would write. This copies the sentence AND
     the link together, which is what actually gets pasted into a group chat.

     Rendered as a read-only preview rather than a bare button: a doctor is
     about to send this to a colleague in their own name, so they get to read
     it first. */
  function copyBlock(h, key, text, label) {
    var wrap = h('div', { class: 'asc-ref-copyblock' });
    var pre = h('div', { class: 'asc-ref-copypreview' }, text);
    wrap.appendChild(pre);
    var btn = h('button', {
      class: 'asc-btn asc-btn-sm asc-ref-copy', type: 'button',
    }, copiedKey === key ? 'Copied' : label);
    btn.addEventListener('click', function () { copyText(key, text); });
    wrap.appendChild(btn);
    return wrap;
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

  /* ── The invite composer. Its card wrapper and its copy of the funnel
     both moved into physicianCol; this is just the form now. ──────── */
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
      class: 'asc-btn asc-btn-sm asc-btn-go asc-ref-send', type: 'button',
      disabled: refBusy,
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

  /* ── Right: introduce a health system ───────────────────────────────────
     An interest form that now knows WHO it is introducing.

     What this card used to be: one textarea that emailed a founder. The
     physician typed a paragraph, we replied to them, and the person they
     actually wanted us to meet never heard from anybody. The hero above named
     health systems as one of the things worth referring, which made the dead
     end worse rather than smaller.

     What did NOT change, and must not: no dollar figure, no percentage, no
     worked example. This card once carried "a $1M data partnership at a 15 to
     20 percent introducer share is $150,000 to $200,000", and that number was
     removed because institutional terms are negotiated one deal at a time, so
     a figure printed here becomes a promise the negotiation then has to keep.
     A physician who read $200,000 and was paid a fraction of it would be right
     to feel misled. Capturing a contact does not change that reasoning, see
     docs/asclepius/REFERRALS.md. ──────────────────────────────────────────── */
  var NOTE_PLACEHOLDER =
    'Anything that would help: what they do, how big the system is, what they '
    + 'might be open to.';

  function hsField(h, key, label, placeholder, opts) {
    opts = opts || {};
    var input = h('input', {
      class: 'asc-ref-input',
      type: opts.type || 'text',
      placeholder: placeholder,
      value: hsDraft[key],
      disabled: noteBusy,
    });
    input.addEventListener('input', function () { hsDraft[key] = input.value; });
    return h('label', { class: 'asc-ref-field' },
      h('span', { class: 'asc-ref-fieldlabel' }, label), input);
  }

  function systemCol(h) {
    var col = h('div', { class: 'asc-ref-card asc-ref-col' });
    col.appendChild(h('div', { class: 'asc-ref-title' }, 'Introduce a health system'));
    col.appendChild(h('div', { class: 'asc-ref-pitch' },
      'Tell us who to write to and we write in your name.'));

    col.appendChild(hsField(h, 'contact_name', 'Their name', 'James Okoye'));
    col.appendChild(hsField(h, 'contact_email', 'Their email',
      'j.okoye@meridianhealth.org', { type: 'email' }));
    col.appendChild(hsField(h, 'contact_role', 'Their role (optional)',
      'Chief Operating Officer'));
    col.appendChild(hsField(h, 'hs_name', 'Health system', 'Meridian Health'));
    col.appendChild(hsField(h, 'relationship', 'How you know them',
      'We were at college together'));

    var note = h('textarea', {
      class: 'asc-ref-input asc-ref-note', rows: '3',
      placeholder: NOTE_PLACEHOLDER, disabled: noteBusy,
    });
    note.value = hsDraft.note;
    note.addEventListener('input', function () { hsDraft.note = note.value; });
    col.appendChild(h('label', { class: 'asc-ref-field' },
      h('span', { class: 'asc-ref-fieldlabel' }, 'Anything we should know (optional)'),
      note));

    /* The consent checkbox is not chrome. We send this email in the
       physician's name, with their address on the reply-to, so the claim it
       makes to the recipient is that somebody they know asked us to write.
       This is the cheapest possible place for the physician to assert that is
       true before we say it on their behalf. */
    var check = h('input', { type: 'checkbox', class: 'asc-ref-check', disabled: noteBusy });
    check.checked = hsConsent;
    check.addEventListener('change', function () { hsConsent = check.checked; });
    col.appendChild(h('label', { class: 'asc-ref-consent' }, check,
      h('span', {},
        'I know this person and they’re OK hearing from us. We’ll write in '
        + 'your name, and their reply comes to you.')));

    var button = h('button', {
      class: 'asc-btn asc-btn-sm asc-btn-go', type: 'button', disabled: noteBusy,
    }, noteBusy ? 'Sending…' : 'Send the introduction');
    button.addEventListener('click', submitHsReferral);
    col.appendChild(h('div', { class: 'asc-ref-form asc-ref-noteform' }, button));

    if (noteError) col.appendChild(h('div', { class: 'asc-ref-error' }, noteError));
    else if (noteMessage) col.appendChild(h('div', { class: 'asc-ref-msg' }, noteMessage));

    col.appendChild(copyBlock(h, 'hs-msg', healthSystemMessage(),
      'Copy an intro to forward'));

    col.appendChild(hsFunnelBlock(h));
    // The "a founder reads every one of these" footer is gone. The consent
    // line already tells the physician what we do with the form, and the
    // copyable blurb sitting above it is the offer to send it themselves.
    return col;
  }

  /* The health-system funnel. Mirrors the physician one on purpose, same
     scroll box, same count outside it, with one deliberate difference: there
     is no amount column, and no empty space where one would go.

     These rows also live far longer than a physician's. An institutional deal
     resolves over months, so a row that said nothing for a quarter would read
     as broken; every state has a sentence, and the server supplies it. */
  function hsFunnelBlock(h) {
    var rows = data.health_systems || [];
    var wrap = h('div', { class: 'asc-ref-funnel' });
    wrap.appendChild(h('div', { class: 'asc-ref-listhead' },
      h('span', { class: 'asc-ref-listtitle' }, 'Your introductions'),
      h('span', { class: 'asc-ref-listcount' }, rows.length ? String(rows.length) : '')));
    if (!rows.length) {
      wrap.appendChild(h('div', { class: 'asc-ref-empty' }, 'No introductions yet.'));
      return wrap;
    }
    var list = h('div', { class: 'asc-ref-list' });
    rows.forEach(function (r) {
      list.appendChild(h('div', { class: 'asc-ref-row' },
        h('span', { class: 'asc-ref-who' }, r.hs_name || 'A health system'),
        h('span', { class: 'asc-ref-when' }, shortDate(r.invited_at)),
        h('span', { class: 'asc-ref-state' },
          r.status_sentence || 'Introduction recorded.')));
    });
    wrap.appendChild(h('div', { class: 'asc-ref-listwrap' }, list));
    return wrap;
  }

  function submitHsReferral() {
    if (noteBusy) return;
    var body = {
      contact_name: String(hsDraft.contact_name || '').trim(),
      contact_email: String(hsDraft.contact_email || '').trim(),
      contact_role: String(hsDraft.contact_role || '').trim(),
      hs_name: String(hsDraft.hs_name || '').trim(),
      relationship: String(hsDraft.relationship || '').trim(),
      note: String(hsDraft.note || '').trim(),
      consent: !!hsConsent,
    };

    // Checked here as well as on the server, because the round trip is the
    // slow part and "you missed a field" does not need one.
    if (!body.contact_name || !body.contact_email || !body.hs_name
        || !body.relationship) {
      noteError = 'Their name, their email, the health system, and how you know '
        + 'them are all needed.';
      noteMessage = null;
      rerender();
      return;
    }
    if (!body.consent) {
      noteError = 'Please confirm you know this person and they’re OK hearing '
        + 'from us.';
      noteMessage = null;
      rerender();
      return;
    }

    noteBusy = true; noteError = null; noteMessage = null;
    rerender();
    rootCtx.api('/referrals/health-system', { method: 'POST', body: body })
      .then(function (res) {
        noteBusy = false;
        hsDraft = { contact_name: '', contact_email: '', contact_role: '',
                    hs_name: '', relationship: '', note: '' };
        hsConsent = false;
        noteMessage = (res && res.message)
          || 'Introduction recorded. We’ll reach out and you’ll see it below.';
        rerender();
        // Refetch so the new row appears with the status the SERVER gave it,
        // rather than being optimistically drawn here in a state the server
        // has not agreed to yet.
        load();
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
