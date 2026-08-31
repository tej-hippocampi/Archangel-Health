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

   EVERY STOP IS SKIPPABLE, AND A SKIP IS PERMANENT. "Skip for now" closes the
   stop the same way finishing it does; the only difference is which word is
   recorded. A stop that could reopen would make "never nags again" a promise
   the code does not keep.

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

   RE-ENTRY IS A CHIP, NEVER A MODAL AMBUSH. An unfinished checklist shows a
   quiet "Finish setup · 3 of 6" on the dashboard. Clicking it resumes; ignoring
   it costs nothing.
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

  /* ── Live state. Torn down on exit; nothing survives a sign-out. ── */
  var ctx = null;          // { h, api, toast, onUser, startTutorial, openCommunity, setPanel, exit }
  var stops = {};          // { stopId: 'done' | 'skipped' }
  var current = null;      // stop id on screen
  var demoMeta = null;     // { available, url, version } once probed
  var escHandler = null;
  var objectUrl = null;    // blob: URL for the authenticated video, revoked on close

  function h() { return ctx.h.apply(null, arguments); }

  function isClosed(id) { return !!stops[id]; }

  function openStops() { return STOPS.filter(function (s) { return !isClosed(s); }); }

  function doneCount() { return STOPS.length - openStops().length; }

  /** Record a stop as closed and move on.
   *
   *  The local map is updated FIRST and the request is fire-and-forget, on
   *  purpose: a physician clicking "Skip" must not wait on a round trip to see
   *  the next screen, and the worst case of a dropped write is that one stop
   *  reappears on their next login. The opposite trade — blocking the UI on the
   *  network — makes every stop feel broken on a hotel connection. */
  function close(id, outcome, next) {
    if (!isClosed(id)) {
      stops[id] = outcome;
      ctx.api('/me/first-run', {
        method: 'PATCH',
        body: { action: outcome === 'skipped' ? 'skip' : 'done', stop: id },
      }).then(function (user) {
        if (user && ctx.onUser) ctx.onUser(user);
      }).catch(function () { /* best-effort: see above */ });
    }
    if (typeof next === 'function') next();
  }

  /* ═══════════════════════════════════════════════════════════════════════
     Chrome: the full-screen stop shell and the persistent checklist card.
     ═══════════════════════════════════════════════════════════════════════ */

  /** One stop. `primary` is the single emphatic action; `skip` is the quiet one.
   *  There is no third slot, because §7 says one primary and one quiet skip and
   *  a third button is how that becomes two primaries. */
  function stopShell(opts) {
    var actions = h('div', { class: 'asc-fr-actions' }, opts.primary);
    if (opts.skip) actions.appendChild(opts.skip);
    return h('div', { class: 'asc-fr-stage' },
      h('div', { class: 'asc-fr-main' },
        h('div', { class: 'asc-fr-panel' + (opts.wide ? ' asc-fr-panel-wide' : '') },
          opts.eyebrow ? h('div', { class: 'asc-fr-eyebrow' }, opts.eyebrow) : null,
          opts.body,
          actions)),
      checklistCard());
  }

  function primaryBtn(label, onClick) {
    return h('button', { class: 'asc-btn asc-btn-primary asc-btn-lg', type: 'button', onClick: onClick }, label);
  }

  function skipBtn(label, onClick) {
    return h('button', { class: 'asc-fr-skip', type: 'button', onClick: onClick }, label || 'Skip for now');
  }

  /** The persistent right-side checklist — the researched activation pattern.
   *  Passive by construction: it reports, it never interrupts, and a closed stop
   *  is struck through rather than removed so progress stays visible. */
  function checklistCard() {
    var items = STOPS.map(function (id) {
      var state = stops[id];
      var cls = 'asc-fr-check-item'
        + (state ? ' is-done' : '')
        + (id === current ? ' is-current' : '');
      return h('li', { class: cls },
        h('span', { class: 'asc-fr-check-box', 'aria-hidden': 'true' }, state ? '✓' : ''),
        h('span', { class: 'asc-fr-check-label' }, STOP_LABEL[id]),
        state === 'skipped' ? h('span', { class: 'asc-fr-check-note' }, 'skipped') : null);
    });
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
    'AI is going to take real clinical work off physicians — diagnosing, '
      + 'prescribing, managing patients. That is coming either way. Whether it '
      + 'arrives safely depends on what it learns from, and we don’t believe that '
      + 'can be one company’s decision. It takes the medical community itself: the '
      + 'specialists, the people who know what good care looks like, teaching it '
      + 'deliberately rather than letting it guess.',
    'That’s why you’re here, and why we’re glad you are. Every case you take here '
      + 'is read against real clinical judgment — yours. A 70% benchmark score is '
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
      h('span', { class: 'asc-fr-choice-thumb asc-fr-choice-thumb-case', 'aria-hidden': 'true' },
        h('span', { class: 'asc-fr-choice-play' }, '→')),
      h('span', { class: 'asc-fr-choice-title' }, 'Start the practice case'),
      h('span', { class: 'asc-fr-choice-sub' }, 'A real case, in the real interface.')));

    var body = h('div', {},
      h('h1', { class: 'asc-fr-title' }, 'Where would you like to start?'),
      cards,
      h('p', { class: 'asc-fr-note' },
        'Either path works — the practice case is the real interface.'));

    ctx.setRoot(stopShell({
      eyebrow: 'Stop 2 of 6',
      body: body,
      wide: true,
      primary: primaryBtn('Start the practice case →', function () {
        close('start', 'done', runPracticeCase);
      }),
      skip: skipBtn('Skip for now', function () { close('start', 'skipped', runPracticeCase); }),
    }));
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
        previewLine('Tej', 'New nephrology cases just landed for six of you — check your queue.'),
        previewLine('Dr. Okafor', 'Happy to take the group case on the transplant workup.')),
      h('p', { class: 'asc-fr-body' },
        'This is our Slack. Medical news, the community’s best finds, and the two '
        + 'of us — message Tej or Aryaa directly any time. If you’re on a group case '
        + 'with other doctors, this is where you’ll coordinate. We’ll also ping you '
        + 'here when new cases land for you.'));
    ctx.setRoot(stopShell({
      eyebrow: 'Stop 4 of 6',
      body: body,
      primary: primaryBtn('Open the community', function () {
        // Opens in a new tab (the community is its own page), so the
        // walkthrough is still here when they come back — which is why this
        // advances rather than waiting for a return that has no event.
        ctx.openCommunity();
        close('community', 'done', renderEarnings);
      }),
      skip: skipBtn('Skip for now', function () { close('community', 'skipped', renderEarnings); }),
    }));
  }

  function previewLine(who, said) {
    return h('div', { class: 'asc-fr-preview-line' },
      h('span', { class: 'asc-fr-preview-who' }, who),
      h('span', { class: 'asc-fr-preview-said' }, said));
  }

  function renderEarnings() {
    current = 'earnings';
    var bankBtn = h('button', {
      class: 'asc-fr-bank', type: 'button', disabled: true,
      'aria-disabled': 'true',
    },
      h('span', { class: 'asc-fr-bank-title' }, 'Link your bank account'),
      h('span', { class: 'asc-fr-bank-chip' }, 'coming soon'),
      h('span', { class: 'asc-fr-bank-sub' },
        'Payouts land here once banking goes live; we’ll DM you the moment it does.'));

    var body = h('div', {},
      h('h1', { class: 'asc-fr-title' }, 'How you get paid.'),
      h('p', { class: 'asc-fr-body' },
        'Every case you complete accrues in Earnings — $75 per completed case, '
        + 'visible immediately.'),
      // Disabled and clearly labelled, per §6 stop 5. It is architecture on
      // screen: the card and the `bank_link_status` field exist now, and Stripe
      // lands on the payments track. A card that looked live and did nothing
      // would be worse than no card.
      bankBtn);

    ctx.setRoot(stopShell({
      eyebrow: 'Stop 5 of 6',
      body: body,
      primary: primaryBtn('Show me Earnings', function () {
        // Register the interest as we go past: this is the list of people to DM
        // when banking opens, and asking them to opt in twice would be asking
        // twice for something they already said yes to by reading this.
        ctx.api('/me/bank-link/interest', { method: 'POST' }).catch(function () { /* best-effort */ });
        close('earnings', 'done', function () {
          teardownChrome();
          ctx.setPanel('earnings');
        });
      }),
      skip: skipBtn('Skip for now', function () { close('earnings', 'skipped', renderManual); }),
    }));
  }

  function renderManual() {
    current = 'manual';
    var body = h('div', {},
      h('h1', { class: 'asc-fr-title' }, 'Everything else lives in the manual.'),
      h('p', { class: 'asc-fr-body' },
        'Everything about how to label well lives here. Questions beyond it? '
        + 'Message Tej or Aryaa in the community — we answer fast.'),
      h('p', { class: 'asc-fr-body' },
        'And whenever you like, take twenty minutes with us. We meet every '
        + 'physician one on one; it’s the part of this we like most.'),
      h('a', {
        class: 'asc-fr-quiet-link', href: CALENDLY, target: '_blank', rel: 'noopener noreferrer',
      }, 'Book 20 minutes with the founders →'));

    ctx.setRoot(stopShell({
      eyebrow: 'Stop 6 of 6',
      body: body,
      primary: primaryBtn('Open the manual', function () {
        close('manual', 'done', function () {
          teardownChrome();
          ctx.setPanel('guide');
        });
      }),
      skip: skipBtn('Skip for now', function () { close('manual', 'skipped', renderFinished); }),
    }));
  }

  /* ═══════════════════════════════════════════════════════════════════════
     The end. One line, no confetti (§6 stop 6: "confetti-free restraint").
     ═══════════════════════════════════════════════════════════════════════ */

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
     Entry / resume / teardown.
     ═══════════════════════════════════════════════════════════════════════ */

  function teardownChrome() {
    closeDemo();
  }

  /** The first stop that is still open, or the finish card when none is. */
  function resumeAt() {
    var open = openStops();
    if (!open.length) return renderFinished;
    var first = open[0];
    if (first === 'welcome') return renderWelcome;
    if (first === 'start') return renderChooseStart;
    if (first === 'practice') return runPracticeCase;
    if (first === 'community') return renderCommunity;
    if (first === 'earnings') return renderEarnings;
    return renderManual;
  }

  function readState(user) {
    var fr = (user && user.first_run) || {};
    var s = fr.stops || {};
    var out = {};
    STOPS.forEach(function (id) { if (s[id]) out[id] = s[id]; });
    return out;
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
      var fr = (user && user.first_run) || {};
      if (fr.dismissed_at || fr.completed_at) return false;
      var s = fr.stops || {};
      return STOPS.some(function (id) { return !s[id]; });
    },

    /** How far along, for the dashboard chip. */
    progress: function (user) {
      var s = ((user && user.first_run) || {}).stops || {};
      var done = STOPS.filter(function (id) { return !!s[id]; }).length;
      return { done: done, total: STOPS.length };
    },

    /** Open the walkthrough at the first unfinished stop. */
    start: function (context) {
      ctx = context;
      stops = readState(context.user);
      // Probe for the demo BEFORE stop 2 needs it, so the choice screen never
      // flashes a card in or out. A failed probe simply means no demo card.
      demoMeta = null;
      ctx.api('/assets/onboarding-demo/meta')
        .then(function (meta) { demoMeta = meta || null; })
        .catch(function () { demoMeta = null; });
      resumeAt()();
    },

    /** Called by the shell when the tutorial hands control back. */
    resume: function (context) {
      ctx = context;
      stops = readState(context.user);
      resumeAt()();
    },

    teardown: teardownChrome,
  };
})();
