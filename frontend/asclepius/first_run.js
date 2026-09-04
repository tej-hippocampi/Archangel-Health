/* ═══════════════════════════════════════════════════════════════════════════
   First-login walkthrough (Onboarding v2 §6)

   Six stops, checklist-driven. What a physician meets the first time they get
   in, instead of a dashboard they have to reverse-engineer.

     1  the welcome letter    (full screen, the serif moment)
     2  choose your start     (watch the demo · or start the practice case)
     3  the practice case     (the EXISTING tutorial, untouched)
     4  community             (skippable)
     5  earnings              (skippable)
     6  the manual            (skippable)

   ── The rules this file exists to hold ────────────────────────────────────

   STATE IS SERVER-SIDE. `users.first_run_json`, via PATCH /me/first-run — not
   localStorage. Doctors switch devices, and a checklist that resets on the
   phone is a checklist that nags. It is also why closing a stop is a network
   call and not a variable.

   THREE STOPS ARE REQUIRED, THREE ARE OPTIONAL (Welcome package v2 §1). The
   welcome, choosing a start and the practice case render NO skip control, and
   the server 400s a defer against them. The other three offer "Do this later",
   which writes `deferred` — "asked, declined this session" — and is deliberately
   NOT terminal. The previous model made every skip permanent, which is why the
   walkthrough asked about the community exactly once and then went silent
   forever; `deferred` is what lets §2's cadence ask twice and then go quiet.

   ONE FUNCTION DECIDES WHAT A LOGIN SEES. `mode(user)` returns 'walkthrough',
   'reentry', 'banner' or 'none', and the shell branches on it. It mirrors
   `first_run.mode()` in Python line for line — the server is the authority, the
   client cannot paint a screen without answering the same question first, and
   the test suite checks the two against each other rather than trusting them to
   stay in step.

   ONE PRIMARY ACTION, ONE QUIET SKIP (§7). Never two primaries. The checklist
   card is persistent and passive; it never grabs focus and never blocks.

   THE PRACTICE CASE IS NOT REIMPLEMENTED. Stop 3 calls `startTutorial()` and
   gets out of the way. Its completion is checked off by the SERVER, from the
   tutorial's own PATCH /me/tutorial transition, so a doctor who finishes it
   later — from the help menu, on another device — still finds the box ticked.
   There is deliberately no client-side tutorial tracker here to disagree with
   that one.

   THE DEMO EXPANDS IN PLACE. Never a route change. The card grows into a
   centred player over a dimmed backdrop; Esc or ✕ closes it and the stop is
   still on screen behind. It plays a native <video controls> against the Range
   endpoint (§0.1), so the timeline actually scrubs.

   RE-ENTRY ASKS TWICE, THEN GOES QUIET, AND NEVER AMBUSHES (§2, §3). Logins 2
   and 3 with optional stops left open get the re-entry page — one column, the
   remaining stops, and "Go to my cases →" as the PRIMARY, because on re-entry
   the default is leaving. From login 4 there is no page at all, only a passive
   dashboard banner. The chip stays on every other screen. Nothing here is ever
   a modal, and after the practice case a physician is never more than one click
   from Start new case, on any login, forever.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /** The six stops, in order. Mirrors `FIRST_RUN_STOPS` in asclepius/schemas.py;
   *  the server rejects an id it has never heard of, so these two lists cannot
   *  drift into a checklist whose "3 of 6" nobody can reproduce. */
  var STOPS = ['welcome', 'start', 'practice', 'community', 'earnings', 'manual'];

  var STOP_LABEL = {
    welcome: 'Read the welcome',
    start: 'Choose your start',
    practice: 'Do the practice case',
    community: 'Join the community',
    earnings: 'See how you get paid',
    manual: 'Skim the manual',
  };

  /** Where the founders' 20-minute intro lives. Same link as email §4.4 §4. */
  var CALENDLY = 'https://calendly.com/tejpatel-berkeley/intro-with-tej-patel';

  var DEMO_URL = '/api/asclepius/assets/onboarding-demo';   // absolute: it is a <video src>, not an api() call

  /** §1's split. REQUIRED renders no skip control anywhere and the server 400s
   *  a defer against any of them; OPTIONAL may be put off, as often as asked.
   *  Mirrors REQUIRED_STOPS / OPTIONAL_STOPS in asclepius/first_run.py. */
  var REQUIRED = ['welcome', 'start', 'practice'];
  var OPTIONAL = ['community', 'earnings', 'manual'];

  /** Honest time estimates for the re-entry rows (§4.2). Honest is the point:
   *  a "2 min" that takes ten is the last time anybody believes one of these. */
  var STOP_MINUTES = { community: '2 min', earnings: '1 min', manual: '3 min' };

  /** The short names the banner lists as remaining (§4.3). */
  var STOP_SHORT = { community: 'Community', earnings: 'Payouts', manual: 'Manual' };

  var DONE = 'done';
  var DEFERRED = 'deferred';

  /** Logins 2 and 3 get the re-entry page; the 4th onwards gets the banner.
   *  Mirrors REENTRY_THROUGH_SESSION in asclepius/first_run.py — named on both
   *  sides so the number is a decision in two places rather than a literal in
   *  one of them. The suite pins the two cadences against each other. */
  var REENTRY_THROUGH_SESSION = 3;

  /* ── Live state. Torn down on exit; nothing survives a sign-out. ── */
  var ctx = null;          // { h, api, toast, onUser, startTutorial, openCommunity, setPanel, exit }
  var stops = {};          // { stopId: 'done' | 'deferred' }
  var current = null;      // stop id on screen
  var demoMeta = null;     // { available, url, version } once probed
  var demoProbe = null;    // the in-flight probe, so stop 2 can wait on it
  var escHandler = null;
  var reentryEsc = null;   // §3: Esc leaves the re-entry page
  var returnTo = null;     // set while ONE stop was opened from the re-entry page
  var objectUrl = null;    // blob: URL for the authenticated video, revoked on close

  function h() { return ctx.h.apply(null, arguments); }

  function isRequired(id) { return REQUIRED.indexOf(id) !== -1; }

  /** Finished for good. A deferred stop is NOT closed — that is the §1 change. */
  function isDone(id) { return stops[id] === DONE; }

  /** Stops still owed, in order. Deferred ones are in here: deferred is "not
   *  now", not "no", and the whole cadence depends on the difference. */
  function openStops() { return STOPS.filter(function (s) { return !isDone(s); }); }

  function doneCount() { return STOPS.length - openStops().length; }

  function requiredOpen() { return REQUIRED.filter(function (s) { return !isDone(s); }); }

  function optionalRemaining() { return OPTIONAL.filter(function (s) { return !isDone(s); }); }

  /** Read the stops map off a user payload, migrating the previous vocabulary.
   *
   *  The server migrates on read too, and this is not redundant with it: a
   *  physician can be holding a page rendered from a payload fetched BEFORE the
   *  deploy, and 'skipped' arriving from that cache must not render as a fourth
   *  state nothing styles. Same rule as the server's, deliberately: required
   *  skips vanish (they must actually do it), optional skips become deferred. */
  function readState(user) {
    var fr = (user && user.first_run) || {};
    var raw = fr.stops || {};
    var out = {};
    STOPS.forEach(function (id) {
      var v = raw[id];
      if (v === DONE) out[id] = DONE;
      else if (v && !isRequired(id)) out[id] = DEFERRED;
    });
    return out;
  }

  function sessionsSeen(user) {
    var n = parseInt(((user && user.first_run) || {}).sessions_seen, 10);
    return (isFinite(n) && n >= 1) ? n : 1;
  }

  /** Record a transition and move on.
   *
   *  The local map is updated FIRST and the request is fire-and-forget, on
   *  purpose: a physician clicking through must not wait on a round trip to see
   *  the next screen, and the worst case of a dropped write is that one stop
   *  reappears on their next login. The opposite trade — blocking the UI on the
   *  network — makes every stop feel broken on a hotel connection. It is also
   *  why the server's required-stop gate refuses only what a correct client
   *  would never send: a dropped PATCH must cost one extra screen, never access
   *  to work.
   *
   *  `done` is monotonic on both sides. `deferred` is rewritten every session
   *  it is offered and declined, so unlike the old `skipped` it does NOT guard
   *  on "already closed" — only on "already done", which a defer must never
   *  undo. */
  function write(id, outcome, next) {
    if (isRequired(id) && outcome !== DONE) {
      // Unreachable through the UI — required stops render no skip control —
      // and refused by the server if it ever were. Guarded here too so a future
      // caller cannot quietly re-open the hole §1 was written to close.
      outcome = DONE;
    }
    if (!isDone(id)) {
      stops[id] = outcome;
      ctx.api('/me/first-run', {
        method: 'PATCH',
        body: { action: outcome === DEFERRED ? 'defer' : 'done', stop: id },
      }).then(function (user) {
        if (user && ctx.onUser) ctx.onUser(user);
      }).catch(function () { /* best-effort: see above */ });
    }
    if (typeof next === 'function') next();
  }

  /** Kept as the old name so every call site below reads unchanged. */
  function close(id, outcome, next) { write(id, outcome, next); }

  /** Defer every optional stop still open, in ONE request (§4.2).
   *
   *  Three separate PATCHes would each read the stored blob, each write their
   *  own stop, and the last one home would erase the other two — so leaving the
   *  re-entry page would reliably record one stop deferred out of three. */
  function deferAll(next) {
    var remaining = optionalRemaining();
    if (remaining.length) {
      remaining.forEach(function (id) { stops[id] = DEFERRED; });
      ctx.api('/me/first-run', { method: 'PATCH', body: { action: 'defer_all' } })
        .then(function (user) { if (user && ctx.onUser) ctx.onUser(user); })
        .catch(function () { /* best-effort: see above */ });
    }
    if (typeof next === 'function') next();
  }

  /** Where a stop goes when it finishes.
   *
   *  Inside the walkthrough that is the next stop. Opened as a single stop from
   *  the re-entry page it is that page again — a physician who clicked "Do it →"
   *  on the manual asked for the manual, not to be re-enrolled in the rest of an
   *  onboarding they had already put down. `returnTo` is consumed once, so a
   *  later walkthrough run is not permanently pinned back to re-entry. */
  function nextAfter(fallback) {
    return function () {
      if (returnTo) { var back = returnTo; returnTo = null; back(); return; }
      if (typeof fallback === 'function') fallback();
    };
  }

  function renderOptional(id) {
    if (id === 'community') { renderCommunity(); return; }
    if (id === 'earnings') { renderEarnings(); return; }
    renderManual();
  }

  /** The walkthrough's advance, from one optional stop to the next one still
   *  OPEN — not simply to the next in the list.
   *
   *  The difference shows up on "Finish these now": a physician who did the
   *  community months ago and deferred the other two would otherwise be walked
   *  back through the community stop before reaching the ones they asked for.
   *  Inside a first run the two readings are identical, because nothing is done
   *  yet. */
  function afterOptional(id) {
    return function () {
      var rest = OPTIONAL.slice(OPTIONAL.indexOf(id) + 1)
        .filter(function (s) { return !isDone(s); });
      if (rest.length) { renderOptional(rest[0]); return; }
      finishWalkthrough();
    };
  }

  /* ═══════════════════════════════════════════════════════════════════════
     Chrome: the full-screen stop shell and the persistent checklist card.
     ═══════════════════════════════════════════════════════════════════════ */

  /** One stop. `primary` is the single emphatic action; `skip` is the quiet one.
   *  There is no third slot, because §7 says one primary and one quiet skip and
   *  a third button is how that becomes two primaries. `primary` may be absent
   *  on a stop whose body already IS the choice (stop 2).
   *
   *  No "Stop 4 of 6" eyebrow: the checklist sits ~400px to the right counting
   *  the same six things and reads "3 of 6", because one counts POSITION and the
   *  other counts COMPLETED. Both true, both in the same visual register, and a
   *  reader has to stop and work out why they disagree. The checklist is the
   *  richer of the two — it names every stop and marks the skips — so it is the
   *  one that stays. */
  function stopShell(opts) {
    var actions = h('div', { class: 'asc-fr-actions' }, opts.primary);
    if (opts.skip) actions.appendChild(opts.skip);
    return h('div', { class: 'asc-fr-stage' },
      h('div', { class: 'asc-fr-main' },
        h('div', { class: 'asc-fr-panel' + (opts.wide ? ' asc-fr-panel-wide' : '') },
          opts.body,
          actions)),
      checklistCard());
  }

  function primaryBtn(label, onClick) {
    return h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', type: 'button', onClick: onClick }, label);
  }

  /** The quiet second action. §3: "The skip is a real action" — a visible
   *  secondary button with the same hit target as the primary, not 11px of grey
   *  text. It appears ONLY on the three optional stops. */
  function skipBtn(label, onClick) {
    return h('button', { class: 'asc-fr-skip', type: 'button', onClick: onClick }, label || 'Do this later');
  }

  /** The persistent right-side checklist — the researched activation pattern.
   *  Passive by construction: it reports, it never interrupts, and a closed stop
   *  is dimmed rather than removed so progress stays visible.
   *
   *  §6 stop 6: once every stop is closed the card collapses to a single line.
   *  It is not removed mid-flow, because a checklist that vanishes at the moment
   *  you finish it takes the sense of having finished with it. */
  function checklistCard() {
    if (!openStops().length) {
      return h('aside', { class: 'asc-fr-checklist asc-fr-checklist-done' },
        h('span', { class: 'asc-fr-check-box', 'aria-hidden': 'true' }, '✓'),
        h('span', { class: 'asc-fr-check-title' }, 'You’re all set'));
    }
    // §4.1: required first, a hairline divider, then the optional three under a
    // tiny OPTIONAL eyebrow. The grouping IS the explanation of why three of
    // these screens have no skip control and three do — without it a physician
    // meets a stop with no way past and has to guess whether that is a bug.
    function row(id) {
      var state = stops[id];
      var cls = 'asc-fr-check-item'
        + (state === DONE ? ' is-done' : '')
        + (state === DEFERRED ? ' is-later' : '')
        + (id === current ? ' is-current' : '');
      return h('li', { class: cls },
        h('span', { class: 'asc-fr-check-box', 'aria-hidden': 'true' }, state === DONE ? '✓' : ''),
        h('span', { class: 'asc-fr-check-label' }, STOP_LABEL[id]),
        // "later", not "skipped". The word is the model: this is a thing they
        // have not done yet, not a thing they declined for good.
        state === DEFERRED ? h('span', { class: 'asc-fr-later' }, 'later') : null);
    }
    var items = REQUIRED.map(row);
    items.push(h('li', { class: 'asc-fr-eyebrow', 'aria-hidden': 'true' }, 'OPTIONAL'));
    OPTIONAL.forEach(function (id) { items.push(row(id)); });
    return h('aside', { class: 'asc-fr-checklist', 'aria-label': 'Setup checklist' },
      h('div', { class: 'asc-fr-check-head' },
        h('span', { class: 'asc-fr-check-title' }, 'Getting set up'),
        h('span', { class: 'asc-fr-check-count' }, doneCount() + ' of ' + STOPS.length)),
      h('ul', { class: 'asc-fr-check-list' }, items),
      h('button', {
        class: 'asc-fr-check-exit', type: 'button',
        // Leaving is not skipping every remaining stop: the open ones stay open
        // and the dashboard chip brings them back. Dismiss (the "never show me
        // this again" action) is offered only at the END, on the finish card.
        onClick: function () { ctx.exit(); },
      }, 'Finish this later'));
  }

  /* ═══════════════════════════════════════════════════════════════════════
     Stop 1 — the welcome letter.
     ═══════════════════════════════════════════════════════════════════════ */

  var LETTER = [
    'Doctors earn from their judgment. Models learn from it. The hardest cases '
      + 'become the most valuable data.',
    'AI is going to take real clinical work off physicians: diagnosing, '
      + 'prescribing, managing patients. That is coming either way. Whether it '
      + 'arrives safely depends on what it learns from, and we don’t believe that '
      + 'can be one company’s decision. It takes the medical community itself: the '
      + 'specialists, the people who know what good care looks like, teaching it '
      + 'deliberately rather than letting it guess.',
    'That’s why you’re here, and why we’re glad you are. Every case you take here '
      + 'is read against real clinical judgment, yours. A 70% benchmark score is '
      + 'irrelevant when a patient is downstream. The people who carry the '
      + 'consequences should define what correct means.',
    'Welcome aboard.',
  ];

  function renderWelcome() {
    current = 'welcome';
    var body = h('div', { class: 'asc-fr-letter' },
      h('h1', { class: 'asc-fr-letter-title' }, 'Welcome to Archangel Health.'),
      // The first line is the mission, and it carries the weight of a pull
      // quote rather than being one more paragraph.
      h('p', { class: 'asc-fr-letter-lead' }, LETTER[0]),
      h('p', { class: 'asc-fr-letter-body' }, LETTER[1]),
      h('p', { class: 'asc-fr-letter-body' }, LETTER[2]),
      h('p', { class: 'asc-fr-letter-body' }, LETTER[3]),
      h('div', { class: 'asc-fr-letter-sign' },
        h('strong', {}, 'Tej Patel & Aryaa Bhatia'),
        h('span', { class: 'asc-fr-letter-role' }, 'Co-founders')));
    // One button. Deliberately no skip on this stop: it is four paragraphs and
    // a button, it is the only thing on screen, and skipping past the reason
    // the product exists is not a shortcut anyone benefits from.
    ctx.setRoot(stopShell({
      body: body,
      wide: true,
      primary: primaryBtn('Let’s get you started →', function () {
        close('welcome', 'done', renderChooseStart);
      }),
    }));
  }

  /* ═══════════════════════════════════════════════════════════════════════
     Stop 2 — choose your start (demo · or straight into the practice case).
     ═══════════════════════════════════════════════════════════════════════ */

  function renderChooseStart() {
    current = 'start';
    // The probe is started at entry so it is almost always finished by the time
    // the physician has read the welcome letter. Almost always is not always
    // though, and rendering "no demo" because a request had not landed yet
    // would silently hide the video on a slow connection — so if it is still in
    // flight, wait for it. There is nothing to show underneath either way.
    if (demoMeta === null && demoProbe) {
      demoProbe.then(function () { if (current === 'start') renderChooseStart(); });
      ctx.setRoot(h('div', { class: 'asc-fr-stage' },
        h('div', { class: 'asc-fr-main' },
          h('div', { class: 'asc-fr-panel' },
            h('p', { class: 'asc-fr-body' }, 'One moment…'))),
        checklistCard()));
      return;
    }
    var cards = h('div', { class: 'asc-fr-choice-row' });

    // The demo card appears only when a video is actually installed. A
    // deployment that has not had the file uploaded yet shows the practice
    // case on its own rather than a card that plays a 404.
    if (demoMeta && demoMeta.available) {
      cards.appendChild(h('button', {
        class: 'asc-fr-choice', type: 'button',
        onClick: function () { openDemo(); },
      },
        h('span', { class: 'asc-fr-choice-thumb', 'aria-hidden': 'true' },
          h('span', { class: 'asc-fr-choice-play' }, '▶')),
        h('span', { class: 'asc-fr-choice-title' }, 'Watch the 3-minute demo'),
        h('span', { class: 'asc-fr-choice-sub' }, 'The whole product, start to finish.')));
    }

    cards.appendChild(h('button', {
      class: 'asc-fr-choice', type: 'button',
      onClick: function () { close('start', 'done', runPracticeCase); },
    },
      h('span', { class: 'asc-fr-choice-thumb', 'aria-hidden': 'true' },
        h('span', { class: 'asc-fr-choice-play' }, '→')),
      h('span', { class: 'asc-fr-choice-title' }, 'Start the practice case'),
      h('span', { class: 'asc-fr-choice-sub' }, 'A real case, in the real interface.')));

    var body = h('div', {},
      h('h1', { class: 'asc-fr-title' }, 'Where would you like to start?'),
      cards,
      h('p', { class: 'asc-fr-note' },
        'Either path works: the practice case is the real interface.'));

    // No primary button. This screen asks a question and offers exactly two
    // answers; a black "Start the practice case →" underneath them repeated the
    // right-hand card verbatim — same words, same destination — and, being the
    // heaviest thing on the screen, answered the question on the physician's
    // behalf. The two cards ARE the primary action, which is why the whole card
    // is the button.
    //
    // And no skip either, as of Welcome package v2 §4.1. There WAS a third
    // answer here — "Skip for now", writing a terminal `skipped` — and it is how
    // real accounts reached the dashboard having seen neither the demo nor a
    // case. "Choose your start" is a required stop now; the two cards are the
    // only two answers.
    ctx.setRoot(stopShell({ body: body, wide: true }));
  }

  /* ── The player: expands IN PLACE, never a route change. ── */

  function closeDemo() {
    var overlay = document.getElementById('ascFrDemo');
    if (overlay) overlay.remove();
    if (escHandler) { document.removeEventListener('keydown', escHandler); escHandler = null; }
    if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
  }

  function openDemo() {
    if (document.getElementById('ascFrDemo')) return;

    var video = h('video', {
      class: 'asc-fr-video',
      controls: 'controls',
      preload: 'metadata',
      playsinline: 'playsinline',
      autoplay: 'autoplay',
    });

    var overlay = h('div', { class: 'asc-fr-demo-overlay', id: 'ascFrDemo' },
      h('div', { class: 'asc-fr-demo-frame' },
        h('button', {
          class: 'asc-fr-demo-close', type: 'button', 'aria-label': 'Close the demo',
          onClick: closeDemo,
        }, '✕'),
        video,
        h('div', { class: 'asc-fr-demo-after', id: 'ascFrDemoAfter', hidden: true },
          h('span', {}, 'Ready to try one?'),
          h('button', {
            class: 'asc-btn asc-btn-primary', type: 'button',
            onClick: function () { closeDemo(); close('start', 'done', runPracticeCase); },
          }, 'Start the practice case →'))));

    // Clicking the dim closes, the frame does not. Same behaviour as every
    // other overlay in this portal.
    overlay.addEventListener('click', function (e) { if (e.target === overlay) closeDemo(); });
    escHandler = function (e) { if (e.key === 'Escape') closeDemo(); };
    document.addEventListener('keydown', escHandler);

    video.addEventListener('ended', function () {
      var after = document.getElementById('ascFrDemoAfter');
      if (after) after.removeAttribute('hidden');
    });

    document.body.appendChild(overlay);
    attachDemoSource(video);
  }

  /** Point the <video> at the demo.
   *
   *  The endpoint is authenticated and a `<video src>` cannot carry an
   *  Authorization header, so the element uses a MEDIA TICKET: a 30-minute
   *  token that can fetch this one asset and nothing else. Two rejected
   *  alternatives, and why:
   *
   *    * the session token in the query string — a credential good for every
   *      endpoint, written into access logs, referrers and history;
   *    * fetch the file with a header and play a blob: — 73 MB in the tab
   *      before the first frame, and it throws away the Range support that
   *      makes the timeline scrub, which is the whole reason the endpoint
   *      implements it.
   *
   *  The blob path survives as a FALLBACK only, for a deployment whose proxy
   *  strips the query: a player that seeks locally beats one showing an error.
   */
  function attachDemoSource(video) {
    var url = (demoMeta && demoMeta.url) || DEMO_URL;
    var version = (demoMeta && demoMeta.version)
      ? ('&v=' + encodeURIComponent(demoMeta.version)) : '';

    video.addEventListener('error', function onErr() {
      video.removeEventListener('error', onErr);
      ctx.api('/assets/onboarding-demo', { raw: true }).then(function (res) {
        if (!res.ok) throw new Error('demo unavailable');
        return res.blob();
      }).then(function (blob) {
        objectUrl = URL.createObjectURL(blob);
        video.src = objectUrl;
        video.play().catch(function () { /* autoplay policy — the controls are there */ });
      }).catch(function () {
        var frame = video.parentNode;
        if (!frame) return;
        video.remove();
        frame.appendChild(h('p', { class: 'asc-fr-demo-error' },
          'The demo isn’t available right now. The practice case is the real '
          + 'interface, and it’s one click away.'));
      });
    });

    ctx.api('/assets/onboarding-demo/ticket', { method: 'POST' })
      .then(function (res) {
        video.src = url + '?t=' + encodeURIComponent(res.ticket) + version;
      })
      .catch(function () {
        // No ticket: go straight to the blob fallback rather than setting a src
        // we know will 401 just to trigger the error handler.
        video.dispatchEvent(new Event('error'));
      });
  }

  /* ═══════════════════════════════════════════════════════════════════════
     Stop 3 — the practice case. The EXISTING flow, untouched.
     ═══════════════════════════════════════════════════════════════════════ */

  function runPracticeCase() {
    // No wrapper, no tracker, no parallel state. The tutorial owns the screen
    // from here; the server checks this stop off from the tutorial's own
    // complete/skip transition, and the walkthrough resumes from the dashboard
    // chip afterwards.
    teardownChrome();
    ctx.startTutorial();
  }

  /* ═══════════════════════════════════════════════════════════════════════
     Stops 4–6 — community, earnings, the manual. All skippable.
     ═══════════════════════════════════════════════════════════════════════ */

  function renderCommunity() {
    current = 'community';
    var body = h('div', {},
      h('h1', { class: 'asc-fr-title' }, 'The people you’ll be working alongside.'),
      h('div', { class: 'asc-fr-preview asc-fr-preview-community', 'aria-hidden': 'true' },
        previewLine('Dr. Chen', 'Anyone seen the new KDIGO draft? The albuminuria cutoff moved.'),
        previewLine('Tej', 'New nephrology cases just landed for six of you, check your queue.'),
        previewLine('Dr. Okafor', 'Happy to take the group case on the transplant workup.')),
      h('p', { class: 'asc-fr-body' },
        'This is our Slack. Medical news, the community’s best finds, and the two '
        + 'of us: message Tej or Aryaa directly any time. If you’re on a group case '
        + 'with other doctors, this is where you’ll coordinate. We’ll also ping you '
        + 'here when new cases land for you.'));
    ctx.setRoot(stopShell({
      body: body,
      primary: primaryBtn('Open the community', function () {
        // Opens in a new tab (the community is its own page), so the
        // walkthrough is still here when they come back — which is why this
        // advances rather than waiting for a return that has no event.
        ctx.openCommunity();
        close('community', DONE, nextAfter(afterOptional('community')));
      }),
      skip: skipBtn('Do this later', function () { close('community', DEFERRED, nextAfter(afterOptional('community'))); }),
    }));
  }

  function previewLine(who, said) {
    return h('div', { class: 'asc-fr-preview-line' },
      h('span', { class: 'asc-fr-preview-who' }, who),
      h('span', { class: 'asc-fr-preview-said' }, said));
  }

  /** Is the payout rail live for this deployment?
   *
   *  Read off the session payload, which carries the key ONLY when the server's
   *  flag is on. Absent means dark, which is also what an older server that has
   *  never heard of the rail sends, so the placeholder is what a client falls
   *  back to in every uncertain case, and the live card can only appear when a
   *  server has positively said so. */
  function bankRailLive() { return !!(ctx.user && ctx.user.bank_link_enabled); }

  /** The pre-rail card: disabled, clearly labelled, promising a DM. */
  function comingSoonBankCard() {
    return h('button', {
      class: 'asc-fr-bank', type: 'button', disabled: true,
      'aria-disabled': 'true',
    },
      h('span', { class: 'asc-fr-bank-title' }, 'Link your bank account'),
      h('span', { class: 'asc-fr-bank-chip' }, 'coming soon'),
      h('span', { class: 'asc-fr-bank-sub' },
        'Payouts land here once banking goes live; we’ll DM you the moment it does.'));
  }

  /** The live card. Same words, now a control that does the thing they say.
   *
   *  It navigates rather than opening a tab: Stripe hosts the onboarding form
   *  and sends the physician back to the portal when they are done, so a full
   *  navigation is the flow Stripe designed and it survives the popup blockers
   *  that would eat a window.open fired after a network round trip.
   *
   *  The subtitle is not decoration. A doctor is about to type a bank account
   *  number and a tax id into a form, and saying whose form it is, before they
   *  leave, is the honest version of asking. */
  function liveBankCard() {
    var btn = h('button', {
      class: 'asc-fr-bank asc-fr-bank-live', type: 'button',
      onclick: function () {
        btn.setAttribute('disabled', '');
        ctx.api('/me/bank-link/start', { method: 'POST' }).then(function (res) {
          if (res && res.url) { window.location.href = res.url; return; }
          btn.removeAttribute('disabled');
          ctx.toast('Could not open bank linking just now. Try again in a moment.');
        }).catch(function () {
          btn.removeAttribute('disabled');
          ctx.toast('Could not open bank linking just now. Try again in a moment.');
        });
      },
    },
      h('span', { class: 'asc-fr-bank-title' }, 'Link your bank account'),
      h('span', { class: 'asc-fr-bank-sub' },
        'Stripe collects your bank and tax details and files your 1099. '
        + 'We never see them.'));
    return btn;
  }

  function renderEarnings() {
    current = 'earnings';
    var live = bankRailLive();
    var bankBtn = live ? liveBankCard() : comingSoonBankCard();

    var body = h('div', {},
      h('h1', { class: 'asc-fr-title' }, 'How you get paid.'),
      h('p', { class: 'asc-fr-body' },
        'Every case you complete accrues in Earnings, $75 per completed case, '
        + 'visible immediately.'),
      // Disabled and clearly labelled, per §6 stop 5, until the payments rail is
      // live. It is architecture on screen: the card and the `bank_link_status`
      // field exist now, and Stripe lands on the payments track. A card that
      // looked live and did nothing would be worse than no card.
      bankBtn);

    ctx.setRoot(stopShell({
      body: body,
      primary: primaryBtn('Show me Earnings', function () {
        // Stamp that this physician has SEEN the coming-soon card. Not an
        // opt-in — the button says "Show me Earnings", and reading a card is
        // not consent to anything — but it is what the payments track reads to
        // find who has been told banking is coming and is waiting on it.
        // Pointless once the rail is live: there is nothing left to wait for,
        // and the waiting list is what this endpoint exists to fill.
        if (!live) {
          ctx.api('/me/bank-link/interest', { method: 'POST' }).catch(function () { /* best-effort */ });
        }
        // Terminal by destination: this lands the physician ON the Earnings
        // panel, which is a place to be rather than a step to come back from,
        // so any pending return to the re-entry page is dropped.
        close('earnings', DONE, function () {
          returnTo = null;
          teardownChrome();
          ctx.setPanel('earnings');
        });
      }),
      skip: skipBtn('Do this later', function () { close('earnings', DEFERRED, nextAfter(afterOptional('earnings'))); }),
    }));
  }

  function renderManual() {
    current = 'manual';
    var body = h('div', {},
      h('h1', { class: 'asc-fr-title' }, 'Everything else lives in the manual.'),
      h('p', { class: 'asc-fr-body' },
        'Everything about how to label well lives here. Questions beyond it? '
        + 'Message Tej or Aryaa in the community, we answer fast.'),
      h('p', { class: 'asc-fr-body' },
        'And whenever you like, take twenty minutes with us. We meet every '
        + 'physician one on one; it’s the part of this we like most.'),
      h('a', {
        class: 'asc-fr-quiet-link', href: CALENDLY, target: '_blank', rel: 'noopener noreferrer',
      }, 'Book 20 minutes with the founders →'));

    ctx.setRoot(stopShell({
      body: body,
      primary: primaryBtn('Open the manual', function () {
        // Terminal by destination, exactly as Earnings above: the manual IS
        // the guide panel.
        close('manual', DONE, function () {
          returnTo = null;
          teardownChrome();
          ctx.setPanel('guide');
        });
      }),
      skip: skipBtn('Do this later', function () { close('manual', DEFERRED, nextAfter(afterOptional('manual'))); }),
    }));
  }

  /* ═══════════════════════════════════════════════════════════════════════
     The end. One line, no confetti (§6 stop 6: "confetti-free restraint").
     ═══════════════════════════════════════════════════════════════════════ */

  /** The end of a walkthrough RUN, which is not the same as the end of the
   *  checklist.
   *
   *  "You're all set" carries a dismiss, and dismiss is permanent — it is the
   *  one control that stops the product ever mentioning onboarding again. Show
   *  it only when all six stops are genuinely `done`. A physician who reached
   *  the last screen by deferring the optional three has not finished anything;
   *  congratulating them and then quietly switching off the re-entry cadence
   *  they were promised would defeat §2 on the very first login. They simply
   *  leave, and login 2 brings back the re-entry page. */
  function finishWalkthrough() {
    if (!openStops().length) { renderFinished(); return; }
    teardownChrome();
    ctx.exit();
  }

  function renderFinished() {
    current = null;
    var body = h('div', {},
      h('h1', { class: 'asc-fr-title' }, 'You’re all set.'),
      h('p', { class: 'asc-fr-body' },
        'Your dashboard is where the work is. We’ll email you when new cases '
        + 'land for your specialty.'));
    ctx.setRoot(stopShell({
      body: body,
      primary: primaryBtn('Go to my dashboard →', function () {
        dismiss();
        ctx.exit();
      }),
    }));
  }

  /** Collapse the checklist for good. Only offered at the end, and only ever by
   *  a deliberate click — nothing dismisses this on the physician's behalf. */
  function dismiss() {
    ctx.api('/me/first-run', { method: 'PATCH', body: { action: 'dismiss' } })
      .then(function (user) { if (user && ctx.onUser) ctx.onUser(user); })
      .catch(function () { /* best-effort */ });
  }

  /* ═══════════════════════════════════════════════════════════════════════
     The re-entry page (§4.2) — logins 2 and 3, once the required stops are done.

     A short interstitial, NOT the walkthrough: one centred column at 560px on
     the same stage grid, no checklist rail, and nothing animates because the
     physician has seen this before (§3).

     The buttons are deliberately INVERTED from every walkthrough stop. There,
     the primary carries the physician forward and the quiet action leaves;
     here "Go to my cases →" is the filled primary and "Finish these now" is the
     secondary. On re-entry the default is leaving, and a product that makes the
     specialist argue with it for the third login running has stopped being
     worth their time.
     ═══════════════════════════════════════════════════════════════════════ */

  function renderReentry() {
    current = null;
    var remaining = optionalRemaining();
    // Nothing left to offer: this screen would be an empty box with a button
    // that says "go to my cases", which is a worse way of going to their cases
    // than going to their cases.
    if (!remaining.length) { leaveReentry(); return; }

    var rows = h('div', { class: 'asc-fr-reentry-rows' });
    remaining.forEach(function (id) {
      rows.appendChild(h('button', {
        class: 'asc-fr-reentry-row', type: 'button',
        onClick: function () { openSingleStop(id); },
      },
        h('span', { class: 'asc-fr-reentry-ring', 'aria-hidden': 'true' }),
        h('span', { class: 'asc-fr-reentry-label' }, STOP_LABEL[id]),
        h('span', { class: 'asc-fr-reentry-time' }, STOP_MINUTES[id] || ''),
        h('span', { class: 'asc-fr-reentry-go', 'aria-hidden': 'true' }, 'Do it →')));
    });

    var goToCases = h('button', {
      class: 'asc-btn asc-btn-primary asc-btn-lg', type: 'button',
      onClick: function () { leaveReentry(); },
    }, 'Go to my cases →');

    var finishNow = h('button', {
      class: 'asc-fr-skip', type: 'button',
      // §4.2: "it starts the remaining optional stops IN ORDER" — so no
      // `returnTo`. A row's "Do it →" opens one stop and comes back; this is the
      // other request, and running it one-stop-at-a-time would silently turn
      // "finish these" into "finish this".
      onClick: function () { startRemaining(); },
    }, 'Finish these now');

    // §3, keyboard first: the primary is FIRST in the DOM, so Tab lands on
    // "Go to my cases" before anything else and Enter leaves. Esc leaves too.
    var actions = h('div', { class: 'asc-fr-actions' }, goToCases, finishNow);

    ctx.setRoot(h('div', { class: 'asc-fr-stage asc-fr-stage-solo' },
      h('div', { class: 'asc-fr-main' },
        h('div', { class: 'asc-fr-panel asc-fr-reentry' },
          h('h1', { class: 'asc-fr-title' }, 'Finish your onboarding'),
          h('p', { class: 'asc-fr-body' },
            reentryLede(remaining.length)),
          rows,
          actions))));

    bindReentryEsc();
    // No close ✕ anywhere on this page: the primary IS the close (§4.2). A
    // dismiss control would also be a second way to leave, competing with the
    // one that is already the biggest thing on the screen.
    setTimeout(function () { try { goToCases.focus(); } catch (e) { /* headless */ } }, 30);
  }

  /** "Three quick things you set aside" — and two, and one, correctly. */
  function reentryLede(n) {
    var count = n === 1 ? 'One quick thing' : (n === 2 ? 'Two quick things' : 'Three quick things');
    return count + ' you set aside. '
      + (n === 1 ? 'A couple of minutes' : 'Two minutes') + ', or later: your call.';
  }

  function bindReentryEsc() {
    unbindReentryEsc();
    reentryEsc = function (e) {
      // Never while the demo player is up: Esc belongs to the topmost thing on
      // screen, and closing the page out from under an open overlay would leave
      // the overlay orphaned over the dashboard.
      if (e.key !== 'Escape' || document.getElementById('ascFrDemo')) return;
      leaveReentry();
    };
    document.addEventListener('keydown', reentryEsc);
  }

  function unbindReentryEsc() {
    if (reentryEsc) { document.removeEventListener('keydown', reentryEsc); reentryEsc = null; }
  }

  /** Leaving writes `deferred` on every remaining optional stop and goes to the
   *  dashboard. It increments nothing — `sessions_seen` already ticked at login,
   *  server-side, and a client that also counted would run the cadence at the
   *  speed of page loads. */
  function leaveReentry() {
    unbindReentryEsc();
    deferAll(function () { ctx.exit(); });
  }

  /** Open ONE optional stop from the re-entry page, and come back here after.
   *
   *  The walkthrough's own stop renderers advance to the NEXT stop when they
   *  finish, which is right inside the walkthrough and wrong here — a physician
   *  who clicked "Do it →" on the manual asked for the manual, not for the rest
   *  of an onboarding they already put down. So `returnTo` is set for exactly
   *  one stop and the renderers consult it instead of advancing. */
  function openSingleStop(id) {
    unbindReentryEsc();
    returnTo = renderReentryOrLeave;
    renderOptional(id);
  }

  /** Run every remaining optional stop, in order, and finish. */
  function startRemaining() {
    unbindReentryEsc();
    returnTo = null;
    var remaining = optionalRemaining();
    if (!remaining.length) { leaveReentry(); return; }
    renderOptional(remaining[0]);
  }

  /** Back to the re-entry page, or straight out if that was the last one. */
  function renderReentryOrLeave() {
    returnTo = null;
    if (optionalRemaining().length) renderReentry();
    else ctx.exit();
  }

  /* ═══════════════════════════════════════════════════════════════════════
     Entry / resume / teardown.
     ═══════════════════════════════════════════════════════════════════════ */

  function teardownChrome() {
    closeDemo();
    unbindReentryEsc();
  }

  /** The first stop that is still open, or the finish card when none is. */
  function resumeAt() {
    var open = openStops();
    if (!open.length) return renderFinished;
    // Required all done and only deferred optional stops left: this is not a
    // walkthrough to resume, it is the re-entry cadence's job. The shell asks
    // `mode()` and routes there, but the dashboard chip lands here directly, so
    // answer it correctly rather than replaying stops they already declined.
    if (!requiredOpen().length && optionalRemaining().length
        && OPTIONAL.every(function (id) { return stops[id] !== undefined; })) {
      return renderReentry;
    }
    var first = open[0];
    if (first === 'welcome') return renderWelcome;
    if (first === 'start') return renderChooseStart;
    if (first === 'practice') return runPracticeCase;
    if (first === 'community') return renderCommunity;
    if (first === 'earnings') return renderEarnings;
    return renderManual;
  }

  /** Ask once whether a demo is installed. Never throws, and a failure resolves
   *  to "no demo" rather than leaving the probe pending forever — stop 2 waits
   *  on this promise, so a probe that never settles would hang the screen. */
  function probeDemo() {
    demoMeta = null;
    demoProbe = ctx.api('/assets/onboarding-demo/meta')
      .then(function (meta) { demoMeta = meta || { available: false }; })
      .catch(function () { demoMeta = { available: false }; });
    return demoProbe;
  }

  window.FirstRunWalkthrough = {
    STOPS: STOPS,

    /** Should the walkthrough open on this login?
     *
     *  Only for a physician who has not dismissed it and still has an open stop.
     *  Admins, QA reviewers and referral-only accounts are excluded by the
     *  caller, which knows the role; this answers the checklist question only.
     */
    shouldRun: function (user) {
      return this.mode(user) !== 'none';
    },

    /** §2's cadence, and the ONLY place the question is answered.
     *
     *      dismissed                → 'none'
     *      required unfinished      → 'walkthrough'   (whatever the session count)
     *      no optional remaining    → 'none'
     *      sessions_seen <= 3       → 'reentry'       (logins 2 and 3)
     *      otherwise                → 'banner'
     *
     *  Mirrors `first_run.mode()` in Python, which is the authority — the server
     *  gates real work on the same reading, and the test suite pins the two
     *  against each other rather than trusting them to stay in step.
     *
     *  `dismissed` is checked FIRST, and that is not a detail. The store's
     *  one-time backfill stamped every already-approved account with
     *  `dismissed_at` and an EMPTY stops map, which is how physicians who had
     *  been labeling for months were kept out of "Welcome to Archangel Health".
     *  Those rows have three required stops open and always will, so testing
     *  the required stops first would drop the entire existing roster into an
     *  onboarding they finished long before it was written. */
    mode: function (user) {
      var fr = (user && user.first_run) || {};
      if (fr.dismissed_at) return 'none';
      var s = readState(user);
      var reqOpen = REQUIRED.some(function (id) { return s[id] !== DONE; });
      if (reqOpen) return 'walkthrough';
      var optLeft = OPTIONAL.some(function (id) { return s[id] !== DONE; });
      if (!optLeft) return 'none';
      return sessionsSeen(user) <= REENTRY_THROUGH_SESSION ? 'reentry' : 'banner';
    },

    /** How far along, for the dashboard chip and the banner's six dots.
     *  Counts DONE only — a deferred stop is progress nobody has made yet. */
    progress: function (user) {
      var s = readState(user);
      var done = STOPS.filter(function (id) { return s[id] === DONE; }).length;
      return { done: done, total: STOPS.length };
    },

    /** True while any stop has NO outcome at all — the first pass through the
     *  tour is genuinely unfinished.
     *
     *  Distinct from `shouldRun`, and the difference matters at exactly one
     *  place: when the practice case hands control back. A physician on their
     *  first login should carry on to community/earnings/manual; one who
     *  replayed the tutorial months later, having already deferred all three,
     *  should land on their dashboard rather than be walked through an
     *  onboarding they finished with. `deferred` is "asked and declined", so it
     *  counts as touched here even though it is still open for the cadence. */
    tourPending: function (user) {
      var s = readState(user);
      return STOPS.some(function (id) { return s[id] === undefined; });
    },

    /** The optional stops still remaining, for the banner's "… remaining". */
    remaining: function (user) {
      var s = readState(user);
      return OPTIONAL.filter(function (id) { return s[id] !== DONE; })
        .map(function (id) { return STOP_SHORT[id] || STOP_LABEL[id]; });
    },

    /** Open the walkthrough at the first unfinished stop. */
    start: function (context) {
      ctx = context;
      stops = readState(context.user);
      returnTo = null;
      // Probe for the demo BEFORE stop 2 needs it, so the choice screen never
      // flashes a card in or out. A failed probe simply means no demo card.
      probeDemo();
      resumeAt()();
    },

    /** Called by the shell when the tutorial hands control back, and by the
     *  dashboard chip. Re-probes, because a resume can be minutes or days after
     *  the start and the demo may have been uploaded in between. */
    resume: function (context) {
      ctx = context;
      stops = readState(context.user);
      returnTo = null;
      probeDemo();
      resumeAt()();
    },

    /** §4.2 — the re-entry page, and what the banner's button opens.
     *
     *  Never decides for itself whether it is the right screen; the shell asks
     *  `mode()` and calls this or `resume()`. One question, one answer, one
     *  place. */
    reentry: function (context) {
      ctx = context;
      stops = readState(context.user);
      returnTo = null;
      probeDemo();
      renderReentry();
    },

    teardown: teardownChrome,
  };
})();
