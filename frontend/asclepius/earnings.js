/* ═══════════════════════════════════════════════════════════
   Earnings section + the billable-session client (PRD-P §5)

   Two exports, one file, because they are one contract:

     window.EarningsSection    { render, reset }
       The doctor's Earnings surface inside the portal. What they
       have made, and why.

     window.AsclepiusSession
       { start, attach, open, setProgress, stop, state, subscribe }
       The heartbeat client for a billable session. PRD-P owns the
       billable session; the review surface (PRD-R) calls
       open_session server-side, forwards the block to `start()`, and
       does nothing else. Keeping the beat loop HERE is what stops a
       second implementation of the payment clock from appearing in
       a file that does not own money.

       `start` is the seam's canonical entry point and `attach` is
       its alias. Both names are load-bearing: the two sides of this
       seam were written against different ones, at different times,
       by agents who could not see each other's files. Removing
       either is a silent no-op on the other side — which is exactly
       how this seam failed the first time.

   ── The rules this file exists to hold ──────────────────────

   The countdown is SERVER-AUTHORITATIVE. Every number rendered
   comes out of a response body. There is no local stopwatch
   counting toward 20:00 anywhere in this file — a client-side
   timer would drift away from the number that decides the payout
   and the first a reviewer would know is a missing $100.

   A HIDDEN TAB IS NOT WORKING, by policy. On visibilitychange we
   emit one beat marking the pause, then stop. Chrome throttles
   setInterval to once a minute in a hidden tab, which would turn
   a 15 s beat into a 60 s+ beat and blow past MAX_GAP on every
   gap — but that stops mattering once the beats are meant to
   stop. We deliberately do NOT move the timer into a Web Worker
   to keep beats flowing while hidden: that is paying for a tab
   nobody is looking at.

   NEVER beforeunload/unload. They are unreliable on mobile and
   unload disables the bfcache. visibilitychange + pagehide only.

   The close beat uses fetch(..., {keepalive: true}) rather than
   navigator.sendBeacon. sendBeacon cannot carry an Authorization
   header, and this API is bearer-token authenticated — the only
   way to use it would be to put a JWT in a query string, which
   writes the token into every access log between here and the
   server. keepalive is the same guarantee (the request outlives
   the page) with headers intact. Delivery is roughly 91% either
   way, and that is fine: the gap-cap already handles the missing
   9%. The final beat makes the last few seconds accurate; it is
   not load-bearing.

   Design system as locked: canvas #eef0ef, card #fbfcfa, ink
   #1a1b1a. Money is the one place Doto earns its keep — the
   headline figure only. Everything else Instrument Sans, with
   IBM Plex Mono for amounts and statuses so the column aligns.
   Zero black fills; a status is a word in colour, not a filled
   chip. Green approved, lime pending, muted grey not approved —
   never pink. Pink means critical, and a task that did not pass
   review is not a safety event.

   DOM built exclusively through ctx.h. Zero innerHTML.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ═══════════════════════════════════════════════════════════
     THE BILLABLE-SESSION CLIENT
     ═══════════════════════════════════════════════════════════ */

  // Everything about cadence comes from the SERVER (the open_session
  // response's `params`). These are only the values used before the
  // first response lands, and they must never be the ones that win.
  // No rate here. Nothing on this page renders one any more, and a hardcoded
  // 10000 sitting in a payments module is the exact hazard test_admin_earnings_dom
  // was written about: the day the real rate moves, the stale literal is what a
  // physician reads while the server says something else.
  var FALLBACK = {
    beat_interval_seconds: 15,
    min_seconds: 1200,
  };

  var API_BASE = '/api/asclepius';
  // Sandbox PRD §1.3: one token per realm, so a live and a sandbox session
  // coexist in one browser (the /sandbox/* shell sets window.__REALM).
  var TOKEN_KEY = (window.__REALM === 'sandbox') ? 'asclepius_token_sandbox' : 'asclepius_token';

  /** The session client's own transport.
   *
   *  Deliberately NOT ctx.api: this client is called from the review surface
   *  (review.js, which already reads the same token key directly and has no ctx)
   *  as well as from this section, and the close beat needs `keepalive`, which
   *  the portal's shared helper does not pass through. Owning the transport here
   *  keeps every payments request in the file that owns payments, and keeps this
   *  module from needing an edit to a file it does not own. */
  function request(path, opts) {
    opts = opts || {};
    var headers = { 'Content-Type': 'application/json' };
    var token = null;
    try { token = localStorage.getItem(TOKEN_KEY); } catch (e) { token = null; }
    if (token) headers.Authorization = 'Bearer ' + token;
    return fetch(API_BASE + path, {
      method: opts.method || 'GET',
      headers: headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      // The page may be going away. keepalive lets the request outlive it —
      // sendBeacon cannot carry the Authorization header this API requires.
      keepalive: !!opts.keepalive,
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (payload) {
        if (!res.ok) {
          throw {
            status: res.status,
            detail: (payload && payload.detail) || res.statusText,
          };
        }
        return payload;
      });
    });
  }

  var session = {
    id: null,
    nonce: null,
    progressKey: null,
    seq: 0,
    params: FALLBACK,
    timer: null,
    bound: false,
    paused: false,
    stopping: false,
    // The last SERVER-REPORTED state. Every rendered number reads from here.
    state: null,
    error: null,
    listeners: [],
  };

  function notify() {
    session.listeners.slice().forEach(function (fn) {
      try { fn(publicState()); } catch (e) { /* a bad listener must not stop the clock */ }
    });
  }

  function publicState() {
    // An error with no session is still state worth reporting: a caller that
    // failed to start needs to be able to see WHY, and returning null there is
    // how "your time is not being counted" becomes invisible.
    if (!session.id && !session.error) return null;
    var s = session.state || {};
    return {
      session_id: session.id,
      credited_seconds: s.credited_seconds || 0,
      continuous_seconds: s.continuous_seconds || 0,
      min_seconds: s.min_seconds || session.params.min_seconds,
      remaining_seconds: s.remaining_seconds == null
        ? (s.min_seconds || session.params.min_seconds) : s.remaining_seconds,
      qualified: !!s.qualified,
      ended: !!s.ended,
      paused: session.paused,
      error: session.error,
      beating: !!session.timer,
    };
  }

  function subscribe(fn) {
    session.listeners.push(fn);
    return function () {
      var i = session.listeners.indexOf(fn);
      if (i !== -1) session.listeners.splice(i, 1);
    };
  }

  /** Open (or resume) a billable session and start beating for it.
   *
   *  PRD-R calls this when a reviewer draws their first pair, passing the pair's
   *  identity as `progressKey`, and then does nothing else — this client takes it
   *  from there. Idempotent server-side: opening twice resumes the same session.
   *
   *  `progressKey` is REQUIRED. Every beat must name the work it is a beat for,
   *  or the server refuses it: credit used to be a function of elapsed time with
   *  a request attached, which made 28 blind POSTs worth $100. */
  function open(kind, progressKey) {
    return request('/sessions', { method: 'POST', body: { kind: kind || 'review' } })
      .then(function (payload) { attach(payload, progressKey); return payload; });
  }

  /** Start beating for an already-opened session.
   *
   *  `start` is the name the review surface calls, and it is the canonical entry
   *  point across the seam. It was missing once: PRD-R built against this client
   *  without ever seeing it, feature-detected `typeof S.start === 'function'`,
   *  found nothing, and silently never beat — server-side session open, zero
   *  heartbeats, a physician reviewing unpaid with nothing thrown or logged.
   *  `attach` is kept as an alias so both names work forever; removing either
   *  breaks a seam whose two halves merge at different times.
   *
   *  IDEMPOTENT. The review surface calls this on every pair draw, because the
   *  server call behind it is idempotent too. Six adjudications must not leave
   *  six interval timers beating in parallel and racing each other for the nonce.
   *
   *  `progressKey` names the work this session is beating for. A caller that does
   *  not pass one gets a session-scoped fallback rather than a refusal — see
   *  below. */
  // ── FOR THE REVIEW SURFACE (PRD-R) ─────────────────────────────────────────
  //
  // Two things this client cannot do for you, because doing them would mean
  // payments knowing what a review pair is:
  //
  //   1. NAME THE WORK.  start(session, pair.task_id)
  //      Without a key every beat carries `session:<id>`, which is honest but
  //      blind: per-case accounting is impossible, and "how long did this pair
  //      take" becomes unanswerable. You already hold the id at the call site.
  //
  //   2. STOP WHEN THERE IS NO WORK.  if (!pair) AsclepiusSession.stop('closed')
  //      This client beats until it is told to stop or the tab hides. When your
  //      queue empties you hide the clock — but the beats keep going, so a
  //      reviewer sitting on an empty queue with the tab open still accrues paid
  //      time and cannot see that they are. Only you know the queue is empty.
  //
  // Neither is a defect in your code; both are consequences of a seam that was
  // built from both ends without either side reading the other.
  function start(payload, progressKey) {
    if (!payload || !payload.session_id) return null;
    // Already beating for this session: nothing to do, and doing it anyway would
    // stack a second timer.
    if (session.id === payload.session_id && session.timer) return publicState();
    return attach(payload, progressKey);
  }

  function attach(payload, progressKey) {
    if (!payload || !payload.session_id) return null;
    if (!progressKey) {
      // Every beat must name the work it is a beat for, or the server refuses it.
      // Refusing to beat here would be defensible and is the wrong call: a
      // physician would lose $100 because an argument went unpassed across a
      // seam. So fall back to a session-scoped key and make it OBVIOUS in the
      // record — `session:<id>` reads, to anyone auditing a payout, as "the
      // client never named the work". The server contract is untouched; this is
      // only this client declining to violate it.
      progressKey = 'session:' + payload.session_id;
    }
    stopTimer();
    session.progressKey = progressKey;
    session.id = payload.session_id;
    session.nonce = payload.nonce;
    session.params = payload.params || FALLBACK;
    session.paused = false;
    session.stopping = false;
    session.error = null;
    // A resumed open deliberately carries NO nonce — otherwise re-opening would
    // be an unlimited supply of beating credentials for a client that never
    // reads a response. Ask for one, once, through the endpoint that exists for
    // it: `seq` comes back with it, so this tab knows where the sequence got to.
    session.seq = payload.resumed ? (payload.seq || 0) : 0;
    session.state = {
      credited_seconds: payload.credited_seconds || 0,
      continuous_seconds: payload.continuous_seconds || 0,
      min_seconds: payload.min_seconds,
      remaining_seconds: payload.remaining_seconds,
      qualified: !!payload.qualified,
      ended: false,
    };
    bindLifecycle();
    if (!session.nonce && payload.resumed) {
      resume().then(function (ok) { if (ok) { beat(true); startTimer(); } });
    } else {
      beat(true);
      startTimer();
    }
    notify();
    return publicState();
  }

  /** Ask for a fresh beating credential. Used on a resumed session, and after a
   *  409 says this tab's nonce is no longer the live one. Hard rate-limited
   *  server-side, so this is a recovery path and never a loop. */
  function resume() {
    if (!session.id) return Promise.resolve(false);
    return request('/sessions/' + encodeURIComponent(session.id) + '/resume', {
      method: 'POST', body: {},
    }).then(function (res) {
      session.nonce = res.nonce;
      session.seq = res.seq || 0;
      session.error = null;
      notify();
      return true;
    }).catch(function (err) {
      session.error = (err && err.status === 429)
        ? 'Session tracking is reconnecting. Your credited time is safe on the server.'
        : 'This review session is no longer being tracked here. Reload the page.';
      notify();
      return false;
    });
  }

  function startTimer() {
    stopTimer();
    var every = (session.params.beat_interval_seconds || 15) * 1000;
    session.timer = setInterval(function () { beat(true); }, every);
  }

  function stopTimer() {
    if (session.timer) { clearInterval(session.timer); session.timer = null; }
  }

  function nextSeq() {
    // null means "let the server derive it" — used on a resumed session whose
    // beat history this tab has never seen.
    if (session.seq == null) return null;
    session.seq += 1;
    return session.seq;
  }

  function beat(active, keepalive) {
    if (!session.id || !session.nonce) return Promise.resolve(null);
    var body = {
      nonce: session.nonce,
      active: !!active,
      // What the reviewer is on right now. Opaque to payments — it counts and
      // caps keys, it never resolves them — so R is free to change its shape.
      progress_key: session.progressKey,
    };
    var seq = nextSeq();
    if (seq != null) body.seq = seq;
    return request('/sessions/' + encodeURIComponent(session.id) + '/heartbeat', {
      method: 'POST', body: body, keepalive: !!keepalive,
    }).then(function (res) {
      session.error = null;
      session.nonce = res.next_nonce || session.nonce;
      // A resumed session let the server derive the first sequence number
      // (this tab has never seen the beats behind it). The response says which
      // number it used, so from here this tab numbers its own beats and the
      // replay guard keeps its depth behind the rotating nonce.
      if (typeof res.seq === 'number') session.seq = res.seq;
      session.state = {
        credited_seconds: res.credited_seconds,
        continuous_seconds: res.continuous_seconds,
        min_seconds: res.min_seconds,
        remaining_seconds: res.remaining_seconds,
        qualified: !!res.qualified,
        ended: !!res.ended,
      };
      if (res.ended) { stopTimer(); }
      notify();
      return res;
    }).catch(function (err) {
      // A 409 is a lost nonce race or a replay: this tab is no longer the one
      // holding the session, and beating on is pointless. Anything else is
      // transient — keep beating, because a network blip must not end a session
      // the reviewer is still working.
      if (err && err.status === 409) {
        // Lost the nonce race — another tab, or a resume elsewhere. Stop, ask
        // once for a fresh credential, and pick up where the server is. Do NOT
        // retry the beat: the whole point of rotation is that a stale nonce
        // stays stale.
        stopTimer();
        session.error = 'This session is being tracked in another tab.';
        resume().then(function (ok) { if (ok) { beat(true); startTimer(); } });
      } else if (err && err.status === 404) {
        stopTimer();
        session.error = 'Session not found.';
      } else if (err && err.status === 422) {
        // Malformed and it will stay malformed — retrying forever would burn
        // the whole session while looking like it was working.
        stopTimer();
        session.error = 'This review session cannot be tracked. Your time is not '
          + 'being counted: reload the page.';
      } else {
        session.error = 'Session time is not syncing. Your credited time is safe on '
          + 'the server; it will catch up when the connection returns.';
      }
      notify();
      return null;
    });
  }

  /** Close the session and settle it. Safe to call repeatedly. */
  function stop(reason, keepalive) {
    if (!session.id) return Promise.resolve(null);
    stopTimer();
    session.stopping = true;
    var id = session.id;
    return request('/sessions/' + encodeURIComponent(id) + '/close', {
      method: 'POST', body: { reason: reason || 'closed' }, keepalive: !!keepalive,
    }).then(function (res) {
      session.state = {
        credited_seconds: res.credited_seconds,
        continuous_seconds: res.continuous_seconds,
        min_seconds: res.min_seconds,
        remaining_seconds: 0,
        qualified: !!res.qualified,
        ended: true,
      };
      notify();
      return res;
    }).catch(function () { return null; });
  }

  // visibilitychange + pagehide. NEVER beforeunload/unload.
  function bindLifecycle() {
    if (session.bound) return;
    session.bound = true;

    document.addEventListener('visibilitychange', function () {
      if (!session.id || session.stopping) return;
      if (document.visibilityState === 'hidden') {
        // One beat marking the pause, then stop. Throttling becomes irrelevant
        // because we WANT the beats to stop.
        session.paused = true;
        stopTimer();
        beat(false, true);
        notify();
      } else {
        session.paused = false;
        beat(true);
        startTimer();
        notify();
      }
    });

    window.addEventListener('pagehide', function () {
      if (!session.id || session.stopping) return;
      stop('closed', true);
    });
  }

  /** Tell the client the reviewer has moved to a different piece of work.
   *  PRD-R calls this when the next pair is drawn. */
  function setProgress(progressKey) {
    if (!progressKey) return;
    session.progressKey = progressKey;
    if (session.id && !session.stopping) beat(true);
  }

  window.AsclepiusSession = {
    open: open,
    // `start` and `attach` are the same function under the two names the two
    // sides of this seam were each written against. Keep both.
    start: start,
    attach: attach,
    setProgress: setProgress,
    stop: stop,
    beat: beat,
    subscribe: subscribe,
    state: publicState,
    // Test seam: reset every scrap of module state between cases.
    _reset: function () {
      stopTimer();
      session.id = null; session.nonce = null; session.seq = 0;
      session.progressKey = null;
      session.state = null; session.error = null; session.paused = false;
      session.stopping = false; session.listeners = [];
    },
  };

  /* ═══════════════════════════════════════════════════════════
     THE EARNINGS SECTION
     ═══════════════════════════════════════════════════════════ */

  var rootEl = null;
  var rootCtx = null;
  var data = null;
  var loadError = null;
  var unsubscribe = null;
  var pollTimer = null;

  function money(cents) {
    var n = Math.round(Number(cents || 0)) / 100;
    return '$' + n.toLocaleString('en-US', {
      minimumFractionDigits: n % 1 === 0 ? 0 : 2,
      maximumFractionDigits: 2,
    });
  }

  function clock(seconds) {
    var s = Math.max(0, Math.round(Number(seconds || 0)));
    var m = Math.floor(s / 60);
    var r = s % 60;
    return String(m) + ':' + (r < 10 ? '0' : '') + String(r);
  }

  // green approved · lime pending · muted grey not approved. NEVER pink.
  var STATUS_CLASS = {
    approved: 'asc-pay-approved',
    paid: 'asc-pay-approved',
    accrued: 'asc-pay-pending',
    void: 'asc-pay-void',
  };

  function render(body, ctx) {
    rootEl = body; rootCtx = ctx;
    ctx.clear(body);
    body.appendChild(ctx.h('div', { class: 'asc-pay-loading' }, 'Loading your earnings…'));
    load();
  }

  function rerender() {
    if (!rootEl || !rootCtx) return;
    var ctx = rootCtx;
    var h = ctx.h;
    ctx.clear(rootEl);

    if (loadError) {
      // A VISIBLE error, never a silent placeholder or a reassuring zero. A $0
      // that is actually "we could not load your ledger" is the single worst
      // thing this page can show a physician.
      rootEl.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' },
          'Your earnings could not be loaded, so nothing below is a real number. '
          + loadError + ' Reload the page; if it persists this is a deploy problem, '
          + 'not a change to what you are owed.'))));
      return;
    }
    if (!data) {
      rootEl.appendChild(h('div', { class: 'asc-pay-loading' }, 'Loading your earnings…'));
      return;
    }

    rootEl.appendChild(h('h2', { class: 'asc-pay-title' }, 'Earnings'));
    rootEl.appendChild(headline(h));
    // The countdown is about TIME, which an unpaid account works like anyone
    // else — so the session widget shows for them too.
    var live = sessionWidget(h);
    if (live) rootEl.appendChild(live);
    // The rate breakdown and the ledger are about MONEY, and for a
    // non-accruing account there is none. "Tasks labeled 0 × $75 · $0" under a
    // sentence saying work does not accrue reads as a contradiction.
    if (data.accrues_payment) {
      rootEl.appendChild(lines(h));
      // Between the breakdown and the ledger, and the seam is the point. Not at
      // the top: a growth ask above a physician's own money reads as the
      // company's priority displacing theirs. Not at the bottom: below the
      // ledger is below the fold and it will never be seen. Here, the doctor has
      // just read WHAT they earned and is about to read HOW — "there is another
      // way to earn here" is a continuation of the sentence they are already
      // reading rather than an interruption of it.
      var refCard = referralCard(h);
      if (refCard) rootEl.appendChild(refCard);
      rootEl.appendChild(recent(h));
      // Always, session or not. The sentence used to live inside the session
      // widget, which renders nothing when no session is open — so a physician
      // who had never started a review could not find the rule at all, and one
      // who had was looking at a different tab. A rule you can only read while
      // you are already exposed to it is not a warning.
      rootEl.appendChild(ruleCard(h));
    }
  }

  function headline(h) {
    var wrap = h('div', { class: 'asc-pay-head' });
    if (!data.accrues_payment) {
      // A legacy equity-only account does not accrue a per-task rate. Saying
      // so is much better than an unexplained $0 next to work they know they
      // did.
      wrap.appendChild(h('div', { class: 'asc-pay-noaccrual' },
        'Your account is on an equity arrangement rather than a per-task '
        + 'rate, so labeling and review work does not accrue a payment. '
        + 'Your work still counts everywhere quality is measured.'));
      return wrap;
    }

    // The one Doto numeral on this view. Money is where it earns its keep.
    wrap.appendChild(h('div', { class: 'asc-pay-hero' },
      h('span', { class: 'asc-pay-hero-value' }, money(data.approved_cents)),
      h('span', { class: 'asc-pay-hero-label' }, 'approved')));

    var subParts = [];
    // Both halves of the headline, because "you have been paid $75" and "we owe
    // you $75" are different sentences and the figure above sums them.
    if (data.paid_cents) subParts.push(money(data.paid_cents) + ' paid');
    if (data.unpaid_cents) subParts.push(money(data.unpaid_cents) + ' to come');
    if (data.pending_cents) subParts.push(money(data.pending_cents) + ' pending review');
    if (data.void_cents) subParts.push(money(data.void_cents) + ' not approved');
    if (subParts.length) {
      var sub = h('div', { class: 'asc-pay-sub' });
      subParts.forEach(function (text, i) {
        if (i) sub.appendChild(h('span', { class: 'asc-pay-dot' }, '·'));
        sub.appendChild(h('span', {}, text));
      });
      wrap.appendChild(sub);
    }
    return wrap;
  }

  /* What decides what you are paid, in the order a physician asks it.
   *
   *  Every sentence is true with ASCLEPIUS_PAYOUT_QUALITY_ENABLED off, which is
   *  how it ships (payout.py::enabled). The second sentence is borrowed from
   *  that module's FIRST commitment rather than from its multiplier: pay takes
   *  no physician attribute as input. That is true today and stays true the day
   *  the multiplier is armed, because the multiplier is about the case. A
   *  sentence that has to be rewritten when a flag flips is a sentence that
   *  will not be.
   *
   *  What a review session pays is stated once, in the reviewer manual, which
   *  is where somebody looks BEFORE committing twenty minutes to one. It is
   *  deliberately not restated here. */
  function ruleCard(h) {
    var p = data.params || {};
    var min = clock(p.min_seconds || 1200);
    return h('div', { class: 'asc-pay-rule' },
      h('div', { class: 'asc-pay-rule-title' }, 'How this is paid'),
      h('div', { class: 'asc-pay-rule-body' },
        'Your credentials decide what work you can draw. What a piece of work '
        + 'pays is set by the kind of work it is, and does not change with who '
        + 'you are.'),
      h('div', { class: 'asc-pay-rule-body' },
        'Submitted work is pending until a reviewer signs it off. Work a '
        + 'reviewer does not approve is not paid, and the reason is on the row.'),
      h('div', { class: 'asc-pay-rule-body' },
        'A review session has to run ' + min + ' continuously to be payable. '
        + 'Leaving before ' + min + ' ends the session unpaid. Time below '
        + min + ' is still recorded.'));
  }

  function lines(h) {
    var wrap = h('div', { class: 'asc-pay-lines' });
    (data.lines || []).forEach(function (l) {
      // Referrals render GREEN. Green means physician-authored value here, and a
      // referral is the one line on this page earned by a physician's judgment
      // about another physician rather than by an hour of their labour.
      wrap.appendChild(h('div', {
        class: 'asc-pay-line' + (l.kind === 'referral' ? ' asc-pay-line-referral' : ''),
      },
        h('span', { class: 'asc-pay-line-label' }, l.label),
        // The count only. This used to read "33 × $75", which is a
        // forward-looking price on a page whose job is to report what was
        // actually earned. The totals beside it are facts; a posted rate is a
        // promise, and this is not where promises are made.
        h('span', { class: 'asc-pay-line-count' }, String(l.count)),
        h('span', { class: 'asc-pay-line-total' }, money(l.cents))));
      // Submitted work awaiting review is real and countable, and it is what
      // the pending total above is made of. Without this row a labeler's first
      // ever visit read "$75 pending" beside "Tasks labeled: 0" — the page
      // contradicting itself about the only thing it exists to report.
      if (l.pending_count) {
        wrap.appendChild(h('div', { class: 'asc-pay-line asc-pay-line-pending' },
          h('span', { class: 'asc-pay-line-label' },
            // The server supplies the sentence when the wait is about a PERSON
            // rather than a review queue — "1 invited, awaiting their first
            // case". A referral that has not converted must never render as a
            // bare count the doctor has to interpret.
            '· ' + (l.pending_label
              || (String(l.pending_count) + ' awaiting review'))),
          h('span', { class: 'asc-pay-line-count' }, ''),
          h('span', { class: 'asc-pay-line-total' },
            (l.kind === 'referral' ? '+' : '') + money(l.pending_cents || 0))));
      }
    });
    return wrap;
  }

  /* ─── The referral card ────────────────────────────────────
     Between the breakdown and the ledger. Zero innerHTML, every
     string through h() children or textContent.

     Deliberately NOT built: share links, social buttons, a
     leaderboard, "you're 2 referrals from a bonus". This is a
     physician checking their pay. A referral mechanic that feels
     like a growth loop makes the product feel like one, and the
     entire competitive argument is that we are not that. */
  function referralCard(h) {
    // The full referral surface (link, composer, funnel, enterprise note)
    // lives on the Referral tab now. This card is a pointer, kept here
    // because Earnings is where the money question naturally arises.
    var block = data.referrals;
    if (block && block.can_refer === false) return null;
    var card = h('div', { class: 'asc-ref-card' });
    card.appendChild(h('div', { class: 'asc-ref-title' }, 'Refer a physician'));
    card.appendChild(h('div', { class: 'asc-ref-pitch' },
      'Your link, your invitations and what each referral is worth moved to '
      + 'the Referral tab in the side panel.'));
    return card;
  }

  function recent(h) {
    var wrap = h('div', { class: 'asc-pay-recent' });
    wrap.appendChild(h('div', { class: 'asc-section-title' }, 'Recent'));
    var rows = data.recent || [];
    if (!rows.length) {
      wrap.appendChild(h('div', { class: 'asc-pay-empty' },
        'Nothing yet. Completed tasks appear here as soon as you submit them.'));
      return wrap;
    }
    rows.forEach(function (r) {
      var label = r.kind_label + (r.detail ? ' · ' + r.detail : '');
      var row = h('div', { class: 'asc-pay-row' },
        h('span', { class: 'asc-pay-when' }, shortDate(r.accrued_at)),
        h('span', { class: 'asc-pay-what' }, label),
        h('span', { class: 'asc-pay-amount' }, money(r.amount_cents)),
        h('span', { class: 'asc-pay-status ' + (STATUS_CLASS[r.status] || '') },
          r.status_word));
      wrap.appendChild(row);
      // Never show a number that went down without the explanation next to it.
      if (r.status === 'void' && r.note) {
        wrap.appendChild(h('div', { class: 'asc-pay-note' }, r.note));
      }
    });
    return wrap;
  }

  function shortDate(iso) {
    if (!iso) return '-';
    var d = new Date(String(iso).indexOf('Z') === -1 && String(iso).indexOf('+') === -1
      ? String(iso) + 'Z' : String(iso));
    if (isNaN(d.getTime())) return '-';
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }

  /* ─── The reviewer's live session widget ───────────────────
     Every number here comes from a server response body. There is
     no local timer counting toward 20:00 in this function or
     anywhere else in this file. */
  function sessionWidget(h) {
    var live = window.AsclepiusSession && window.AsclepiusSession.state();
    // This tab is beating -> use what the heartbeat responses said. Otherwise
    // fall back to the read-only open-session snapshot the earnings payload
    // carries (the review tab is the one beating; this tab must not).
    var s = (live && !live.ended) ? live : data.open_session;
    if (!s) return null;

    var min = s.min_seconds || (data.params && data.params.min_seconds) || 1200;
    var elapsed = s.continuous_seconds || 0;
    var qualified = !!s.qualified;

    var card = h('div', {
      class: 'asc-pay-session' + (qualified ? ' is-qualified' : ''),
    });
    card.appendChild(h('div', { class: 'asc-pay-session-clock' },
      'Session · ',
      h('span', { class: 'asc-pay-session-elapsed' }, clock(elapsed)),
      ' of ' + clock(min)));

    if (live && live.error) {
      card.appendChild(h('div', { class: 'asc-pay-session-warn' }, live.error));
    }
    if (live && live.paused) {
      card.appendChild(h('div', { class: 'asc-pay-session-warn' },
        'Paused, this tab is in the background and is not counting.'));
    }

    card.appendChild(h('div', { class: 'asc-pay-session-rule' }));
    // Say it in exactly these words. It is the one sentence that stops a doctor
    // discovering the rule by losing $100.
    card.appendChild(h('div', { class: 'asc-pay-session-warning' },
      qualified
        ? 'This session has passed ' + clock(min) + ' and is payable.'
        : 'Leaving before ' + clock(min) + ' ends the session unpaid.'));
    return card;
  }

  /* ─── Data ─────────────────────────────────────────────────── */
  function load() {
    var ctx = rootCtx;
    if (!ctx) return;
    // ctx.api already prefixes API_BASE = '/api/asclepius' (asclepius.js). The
    // path here is RELATIVE to that — writing '/asclepius/earnings' doubled the
    // segment into /api/asclepius/asclepius/earnings, which matches no route, so
    // every physician's Earnings tab hard-404'd.
    ctx.api('/earnings').then(function (payload) {
      data = payload; loadError = null;
      rerender();
      watchSession();
    }).catch(function (err) {
      data = null;
      loadError = (err && (err.detail || err.message)) || 'The server did not respond.';
      rerender();
    });
  }

  /** Keep the widget honest while this section is on screen.
   *
   *  If this tab holds the session, re-render on every heartbeat response. If
   *  another tab holds it, poll the READ-ONLY session endpoint — polling does
   *  not credit time, so looking at your earnings can never earn you any. */
  function watchSession() {
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (!data) return;

    if (window.AsclepiusSession) {
      unsubscribe = window.AsclepiusSession.subscribe(function () { rerender(); });
    }
    var mine = window.AsclepiusSession && window.AsclepiusSession.state();
    if (mine && !mine.ended) return;             // this tab is the one beating
    if (!data.open_session) return;

    var id = data.open_session.session_id;
    var every = ((data.params && data.params.beat_interval_seconds) || 15) * 1000;
    pollTimer = setInterval(function () {
      rootCtx.api('/sessions/' + encodeURIComponent(id)).then(function (s) {
        data.open_session = s.ended ? null : s;
        rerender();
        if (s.ended) { clearInterval(pollTimer); pollTimer = null; load(); }
      }).catch(function () { /* transient; the next tick tries again */ });
    }, every);
  }

  function teardown() {
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  window.EarningsSection = {
    render: render,
    reset: function () {
      teardown();
      data = null; loadError = null;
      // The referral form's draft is per-visit state. Leaving it behind would
      // put one physician's half-typed colleague in front of the next render.
      },
  };
})();
