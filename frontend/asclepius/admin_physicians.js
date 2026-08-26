/* ═══════════════════════════════════════════════════════════
   Admin · Physicians section (PRD C Phase 3)

   Who supplies our judgment, and are they credentialed?

   Table: Name · Email · Phone · Specialty · Tier · Verification ·
   Slack · Health system — grouped by health system (Independent for
   physicians with none). Filter chips carry live counts; Pending is
   the operator's launch-week job queue and is always visible.

   Vocabulary rules (§3 of the PRD):
   - Tier renders as a WORD, never a raw token: Labeler / Reviewer /
     Unassigned.
   - Verification has FOUR states: Approved / Pending / Rejected /
     Not checked — NULL is not "no".

   PRD-B's verification queue mounts as a tab inside this section:
   B owns the view, this file owns the shell. The mount point is the
   frozen global window.AsclepiusVerification.mount(el, ctx) — if it
   is absent, a VISIBLE error renders, never a quiet placeholder.

   Loaded as its own file (§3.3); DOM built exclusively with ctx.h.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Mirrors asclepius/capabilities.py TIERS. The one list this file filters,
  // counts and labels against.
  const TIERS = ['labeler', 'reviewer'];

  let activeChip = 'all';       // all | pending | labelers | reviewers | unassigned
  let activeView = 'roster';    // roster | signups | verify — driven by the shell's sub-tabs
  let selectedId = null;        // physician profile view
  let cache = null;             // last /admin/physicians payload
  let rootEl = null;            // the section body we were mounted into
  let rootCtx = null;

  // ─── Vocabulary (four-state verification; tier words) ─────
  // A missing case here does not throw — it silently renders "Unassigned"
  // over a real physician, which is the same class of bug as the backend's
  // `tier === 'reviewer'` equality: wrong, quiet, and only discovered when
  // the person tells you. Keep this map aligned with
  // asclepius/capabilities.py TIER_WORDS.
  function tierWord(t) {
    if (t === 'labeler') return 'Labeler';
    if (t === 'reviewer') return 'Reviewer';
    return 'Unassigned';
  }
  function tierBadge(h, t) {
    if (t === 'labeler') return h('span', { class: 'asc-badge asc-badge-primary' }, 'Labeler');
    if (t === 'reviewer') return h('span', { class: 'asc-badge asc-badge-green' }, 'Reviewer');
    return h('span', { class: 'asc-badge asc-badge-gray' }, 'Unassigned');
  }
  function verificationBadge(h, v) {
    if (v === 'approved') return h('span', { class: 'asc-badge asc-badge-green' }, 'Approved');
    if (v === 'pending') return h('span', { class: 'asc-badge asc-badge-amber' }, 'Pending');
    if (v === 'rejected') return h('span', { class: 'asc-badge asc-badge-red' }, 'Rejected');
    // NULL is a different fact from "rejected" — an operator chasing the wrong
    // physicians is the failure mode this fourth state prevents.
    return h('span', { class: 'asc-badge asc-badge-gray' }, 'Not checked');
  }
  function slackText(s) {
    if (s === true || s === 1) return 'Joined';
    if (s === false || s === 0) return 'Not joined';
    return '—'; // not checked
  }

  // ─── Section entry — the shell's sub-tabs pick the view ───
  function render(body, ctx, view) {
    rootEl = body; rootCtx = ctx;
    if (view) { if (view !== activeView) selectedId = null; activeView = view; }
    const { h, clear } = ctx;
    clear(body);
    const inner = h('div', {});
    body.appendChild(inner);
    if (activeView === 'verify') renderVerifyTab(inner, ctx);
    else if (activeView === 'signups') renderSignups(inner, ctx);
    else if (selectedId) renderProfile(inner, ctx, selectedId);
    else renderRoster(inner, ctx);
  }

  function rerender() {
    if (rootEl && rootCtx) render(rootEl, rootCtx);
  }

  /* ─── PRD-CRED · model health + fairness monitor (PRD C §5.2, §6) ──────────
   * Rendered ABOVE the queue, collapsed, because it is not what an admin came
   * here to do — but it has to be somewhere they will see it weekly.
   *
   * Two things live here that nothing else in the product can tell you:
   *
   *  1. The per-batch weight delta. §8: "a model that silently stops updating
   *     looks exactly like a model that has converged." The drift column is the
   *     only cheap way to tell those apart from a screen.
   *  2. The four-fifths fairness comparison. It reads a table that carries no
   *     user ids at all — voluntary self-reported demographics under an HMAC
   *     pseudonym, with the decided tier copied in at decision time. That is
   *     what makes the monitor possible without making the model able to see it.
   *
   * Mounted here rather than as a new sub-tab because the shell's tab list
   * lives in asclepius.js, which is outside Agent C's write allowlist.
   */
  function renderModelHealth(container, ctx) {
    const { h, api } = ctx;
    const box = h('details', { class: 'asc-card' });
    const body = h('div', { class: 'asc-card-pad' });
    box.appendChild(h('summary', { class: 'asc-card-pad' },
      'Model health · learning loop + fairness monitor'));
    box.appendChild(body);
    container.appendChild(box);

    let loaded = false;
    box.addEventListener('toggle', function () {
      if (!box.open || loaded) return;
      loaded = true;
      Promise.all([
        api('/verify/tiering-weights'),
        api('/verify/fairness'),
      ]).then(function (res) {
        renderModelHealthBody(body, ctx, res[0], res[1]);
      }).catch(function (e) {
        clearNode(body);
        // AUDIT UI: `asc-inline-error` (soft, recoverable) not `asc-error` (loud, blocking).
        // The two are visually distinct and the file was using them interchangeably. The
        // split that makes them mean something: inline = "this panel failed, the page is
        // fine, try again"; asc-error = "a module did not load and physicians cannot be
        // approved until someone fixes the deploy". A failed metrics fetch is the first.
        body.appendChild(h('div', { class: 'asc-inline-error' },
          'Could not load model health: ' + e.message));
      });
    });
  }

  // AUDIT UI: a real table, not space-padded strings in <div>s.
  //
  // The previous version emitted `'PINNED  ' + w.feature + '  m=' + …` into `vq-reason`,
  // which is not a mono face — so the padding aligned nothing and the numbers did not sit in
  // a column. `.asc-table` already sets `font-variant-numeric: tabular-nums` and `.asc-mono`
  // exists. This is the screen where you prove to yourself, and to a regulator, that the
  // model is behaving; it should be the easiest screen in the product to read, not the
  // hardest.
  function table(h, headers, rows) {
    const thead = h('thead', {}, h('tr', {}, headers.map(function (t) {
      return h('th', {}, t);
    })));
    const tbody = h('tbody', {}, rows.map(function (r) {
      return h('tr', r.attrs || {}, (r.cells || r).map(function (c) {
        return h('td', c && c.attrs ? c.attrs : {}, c && c.text !== undefined ? c.text : c);
      }));
    }));
    return h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
      thead, tbody));
  }

  function num(v, digits, signed) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    const s = Number(v).toFixed(digits === undefined ? 3 : digits);
    return signed && v >= 0 ? '+' + s : s;
  }

  function renderModelHealthBody(body, ctx, weights, fairness) {
    const { h } = ctx;
    clearNode(body);

    body.appendChild(h('div', { class: 'vq-section-label' },
      'Tier weights — posterior vs the rule it started from'));
    body.appendChild(h('div', { class: 'vq-attempt' },
      (weights.pending_decisions || 0) + ' decision(s) not yet folded in · '
      + 'each batch may move any weight by at most ' + weights.max_delta_per_batch
      + ' · sd should shrink as decisions arrive; if it stops while decisions keep '
      + 'coming, the loop is dead rather than converged'));

    let pinnedMoved = false;
    const weightRows = (weights.weights || []).map(function (w) {
      // A pinned row that has drifted is the single most serious thing this page can show,
      // so it is the only thing on it painted pink. A large drift on an UNPINNED weight is
      // the system working.
      const broken = w.pinned && Math.abs(w.drift) > 0;
      if (broken) pinnedMoved = true;
      return {
        attrs: broken ? { class: 'vq-flag' } : {},
        cells: [
          { text: w.feature, attrs: { class: 'asc-mono' } },
          w.pinned ? 'pinned' : '',
          { text: num(w.m), attrs: { class: 'asc-mono' } },
          { text: num(w.drift, 3, true), attrs: { class: 'asc-mono' } },
          { text: num(w.sd), attrs: { class: 'asc-mono' } },
        ],
      };
    });
    body.appendChild(table(h, ['Feature', '', 'm', 'Drift', 'sd'], weightRows));
    if (pinnedMoved) {
      body.appendChild(h('div', { class: 'vq-flag' },
        'A PINNED weight has moved. It must be exactly 0.000 forever. Stop applying '
        + 'batches and investigate before any further tier decision is recorded.'));
    }

    body.appendChild(h('div', { class: 'vq-section-label' },
      'Reviewer selection rate by voluntary self-report — four-fifths rule'));
    const dims = fairness.dimensions || {};
    if (!Object.keys(dims).length) {
      body.appendChild(h('div', { class: 'vq-attempt' },
        'No self-reported demographics on file yet. The monitor needs volunteers, not '
        + 'inference — nothing here is derived from a name, a school, or a region.'));
    }
    Object.keys(dims).sort().forEach(function (dim) {
      body.appendChild(h('div', { class: 'vq-reason' }, humanize(dim)));
      const groups = dims[dim];
      const rows = Object.keys(groups).sort().map(function (g) {
        const r = groups[g];
        const flagged = r.counted && r.impact_ratio !== null && r.impact_ratio < 0.8;
        return {
          attrs: flagged ? { class: 'vq-flag' } : {},
          cells: [
            humanize(g),
            { text: String(r.n), attrs: { class: 'asc-mono' } },
            { text: (r.rate * 100).toFixed(0) + '%', attrs: { class: 'asc-mono' } },
            { text: r.impact_ratio === null ? '—' : num(r.impact_ratio, 2),
              attrs: { class: 'asc-mono' } },
            r.counted ? '' : 'below n=' + fairness.min_group_n + ', not alerted on',
          ],
        };
      });
      body.appendChild(table(h, ['Group', 'n', 'Reviewer rate', 'Impact ratio', ''], rows));
    });
    (fairness.alerts || []).forEach(function (a) {
      body.appendChild(h('div', { class: 'vq-flag' }, a.message));
    });

    // AUDIT H2 — the per-feature breakdown. An outcome monitor tells you a gap exists; this
    // tells you which feature opened it. structured_review_exp is the one to watch: it
    // correlates with IMG status and national origin, both of which are pinned to zero, so
    // it is the available route around the pin.
    const byFeature = fairness.by_feature || {};
    if (Object.keys(byFeature).length) {
      body.appendChild(h('div', { class: 'vq-section-label' },
        'Feature means by group — which feature carries the gap'));
      Object.keys(byFeature).sort().forEach(function (dim) {
        const groups = byFeature[dim];
        const names = {};
        Object.keys(groups).forEach(function (g) {
          Object.keys(groups[g]).forEach(function (f) { names[f] = true; });
        });
        const features = Object.keys(names).sort();
        const groupNames = Object.keys(groups).sort();
        body.appendChild(h('div', { class: 'vq-reason' }, humanize(dim)));
        body.appendChild(table(h, ['Feature'].concat(groupNames.map(humanize)),
          features.map(function (f) {
            return {
              cells: [{ text: f, attrs: { class: 'asc-mono' } }].concat(
                groupNames.map(function (g) {
                  const cell = groups[g][f];
                  return { text: cell ? num(cell.mean, 2) : '—',
                           attrs: { class: 'asc-mono' } };
                })),
            };
          })));
      });
      (fairness.feature_alerts || []).forEach(function (a) {
        body.appendChild(h('div', { class: 'vq-flag' }, a.message));
      });
    }
  }

  // Dimension and group keys arrive as stored tokens (`race_ethnicity`, `prefer_not_to_say`).
  // Rendering them raw is the same class of defect as rendering a raw tier token.
  function humanize(key) {
    return String(key || '').replace(/_/g, ' ').replace(/^./, function (c) {
      return c.toUpperCase();
    });
  }

  function renderVerifyTab(container, ctx) {
    const { h } = ctx;
    renderModelHealth(container, ctx);
    // PRD-B owns this global (Seam 1). Do not re-derive the name — an invented
    // name is the exact bug this replaced: for a whole build round this tab
    // probed a global nobody defined and quietly rendered a placeholder, while
    // the real queue sat in the same merge. Every physician who signed up was
    // locked out, because approving one had no route through the UI.
    const mount = h('div', { id: 'ascVerifyQueueMount' });
    container.appendChild(mount);
    if (window.AsclepiusVerification
        && typeof window.AsclepiusVerification.mount === 'function') {
      window.AsclepiusVerification.mount(mount, ctx);
      return;
    }
    // A visible failure, not a silent placeholder. A quiet fallback is what hid
    // this for an entire build round — an operator must be able to notice and
    // report it.
    clearNode(mount);
    mount.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-error' },
        'Verification module failed to load. Reload the page; if it persists, '
        + 'this is a deploy problem — physicians cannot be approved until it is fixed.'))));
  }

  function clearNode(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  /* ─── Signups: the half of the funnel that had no screen ──────────────────
   *
   * A physician becomes an ACCOUNT — and therefore a roster row, and therefore
   * approvable — only on the last click of the onboarding wizard. Everyone
   * still walking it lives in the tenant store, which this console never read.
   * Meanwhile the landing page emails the founder the moment someone requests a
   * link, so the inbox counted signups the screen could not: "1 physician" on a
   * roster, dozens of notifications in Gmail, and no way to tell which doctor
   * was which. That gap is what this view closes.
   *
   * Nobody here can be approved — none of them has submitted a complete
   * credential record — so this is a CHASE list, and it says so. The row that
   * matters most is the one that signed the attestations and never pressed the
   * final button: one reminder turns them into a verification-queue decision.
   */
  async function renderSignups(container, ctx) {
    const { h, api, clear, loadingCard, toast } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading signups…'));
    let data;
    try {
      data = await api('/admin/signups');
    } catch (e) {
      clear(container);
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, e.message || 'Could not load signups.'))));
      return;
    }
    clear(container);
    const rows = data.signups || [];
    const counts = data.counts || {};

    container.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Started onboarding · no account yet',
          h('span', { class: 'asc-badge asc-badge-count', style: 'margin-left:8px' },
            String(counts.total == null ? rows.length : counts.total))),
        h('div', { class: 'asc-card-sub' },
          'These physicians are mid-wizard, so they are not on the roster and '
          + 'cannot be approved yet — they have not submitted a complete credential '
          + 'record. ' + (data.awaiting_review || 0) + ' physician'
          + ((data.awaiting_review === 1) ? ' is' : 's are')
          + ' finished and waiting for your decision in Verification.'))),
      h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-phys-chips' }, [
        // Not filters — a standing read of where the funnel is losing people.
        ['Ready to finish', counts.ready_to_finish, 'good'],
        ['Idle ' + (data.stalled_after_days || 3) + '+ days', counts.stalled, 'warn'],
        ['Link expired', counts.expired, 'bad'],
      ].map(function (t) {
        return h('span', { class: 'asc-phys-chip asc-chip-stat ' + t[2] }, t[0],
          h('span', { class: 'asc-chip-count' }, String(t[1] == null ? 0 : t[1])));
      })))));

    if (!rows.length) {
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' },
          'Nobody is mid-onboarding right now. Every physician who started has '
          + 'either finished or already been decided.'))));
      return;
    }

    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Name'), h('th', {}, 'Email'), h('th', {}, 'Organization'),
          h('th', {}, 'Specialty'), h('th', {}, 'Stopped at'), h('th', {}, 'Started'),
          h('th', {}, 'Idle'), h('th', {}, 'Link'), h('th', {}, ''))),
        h('tbody', {}, rows.map(function (s) {
          return signupRow(ctx, container, s, data.can_resend !== false);
        })))));
    container.appendChild(card);

    function signupRow(c, cont, s, canResend) {
      const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' }, 'Resend link');
      if (!canResend) {
        btn.setAttribute('disabled', '');
        btn.setAttribute('title', 'Email is not configured on this server (SendGrid or SMTP).');
      }
      btn.addEventListener('click', function () {
        btn.setAttribute('disabled', '');
        btn.textContent = 'Sending…';
        api('/admin/signups/resend', {
          method: 'POST',
          body: { health_system_id: s.health_system_id, email: s.email },
        }).then(function (r) {
          if (toast) toast((r && r.message) || ('Link resent to ' + s.email));
          renderSignups(cont, c);      // re-read: the link's expiry just moved
        }).catch(function (e) {
          btn.removeAttribute('disabled');
          btn.textContent = 'Resend link';
          if (toast) toast(e.message || 'Could not resend that link.', 'error');
        });
      });
      return h('tr', {},
        h('td', {}, h('strong', {}, s.name || '—')),
        h('td', {}, s.email || '—'),
        h('td', {}, s.org_name || 'Independent'),
        h('td', {}, s.specialty || '—'),
        // The stage is the whole point of the row: it is what tells the operator
        // whether this person needs a reminder or a phone call.
        h('td', {}, stageBadge(c.h, s),
          h('span', { class: 'asc-dim', style: 'margin-left:6px' },
            'step ' + s.stage_index + '/' + s.stage_total)),
        h('td', { class: 'asc-mono', style: 'white-space:nowrap' }, dayOnly(s.started_at)),
        h('td', {}, s.days_idle == null ? '—'
          : (s.days_idle === 0 ? 'today' : s.days_idle + 'd')),
        h('td', {}, s.link_expired
          ? h('span', { class: 'asc-badge asc-badge-red' }, 'Expired')
          : h('span', { class: 'asc-badge asc-badge-gray' }, 'Live')),
        h('td', {}, btn));
    }
  }

  function stageBadge(h, s) {
    // Green for the one that converts on a single reminder; amber for a stall
    // the operator can still rescue; grey for a signup that is simply young.
    const tone = s.ready_to_finish ? 'asc-badge-green'
      : ((s.stalled || s.link_expired) ? 'asc-badge-amber' : 'asc-badge-gray');
    // asc-stage-badge undoes .asc-badge's capitalize — this label is a sentence.
    return h('span', { class: 'asc-badge asc-stage-badge ' + tone }, s.stage_word);
  }

  // Date only. fmtDate renders a full timestamp, which wrapped this column to
  // three lines and pushed the stage — the reason anyone opens this table — off
  // to the side. Nothing here turns on the minute a physician clicked.
  function dayOnly(iso) {
    const s = String(iso || '');
    return /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : (s || '—');
  }

  /* An empty roster used to read as "nobody signed up". It never meant that —
   * it meant "nobody FINISHED" — so the roster now says which of the two it is
   * and where the rest are.
   *
   * Fills a slot the roster already placed above the chips, rather than
   * inserting itself once the fetch lands: the count this roster is NOT showing
   * has to be read before the numbers that it is, and a card that appears at the
   * top a moment later shifts everything the operator was about to click. */
  async function renderSignupNotice(slot, ctx) {
    const { h, api } = ctx;
    let data;
    try {
      data = await api('/admin/signups');
    } catch (e) {
      return;  // the roster is the primary content here; never block it on this
    }
    const n = (data.counts || {}).total || 0;
    if (!n) return;
    const link = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' }, 'Open Signups →');
    link.addEventListener('click', function () {
      // The sub-tab strip belongs to the shell (asclepius.js), so ask it to move
      // — switching the view here alone would leave "Roster" looking selected
      // while the Signups table rendered under it.
      if (typeof ctx.openPhysiciansSub === 'function') ctx.openPhysiciansSub('signups');
      else { activeView = 'signups'; rerender(); }
    });
    slot.appendChild(h('div', { class: 'asc-card asc-signup-notice', id: 'ascSignupNotice' },
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-card-title' },
          n + ' physician' + (n === 1 ? '' : 's') + ' mid-onboarding, not on this roster'),
        h('div', { class: 'asc-card-sub' },
          'They requested a link and have not finished the wizard, so they have no '
          + 'account yet. They cannot be approved from here.'),
        link)));
  }

  // ─── Roster ───────────────────────────────────────────────
  async function renderRoster(container, ctx) {
    const { h, api, clear, loadingCard } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading physicians…'));
    try {
      cache = await api('/admin/physicians');
    } catch (e) {
      clear(container);
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, e.message || 'Could not load physicians.'))));
      return;
    }
    clear(container);
    const rows = cache.physicians || [];
    const counts = cache.counts || {};

    // Slot first, fill later: the roster must never wait on (or fail because
    // of) the funnel count, but the notice still has to land ABOVE the chips.
    const noticeSlot = h('div', {});
    container.appendChild(noticeSlot);
    renderSignupNotice(noticeSlot, ctx);

    // Filter chips with live counts — Pending is the launch-week job queue.
    const chips = [
      ['all', 'All', counts.all],
      ['pending', 'Pending', counts.pending],
      ['labelers', 'Labelers', counts.labelers],
      ['reviewers', 'Reviewers', counts.reviewers],
      ['unassigned', 'Unassigned', counts.unassigned],
    ];
    const chipRow = h('div', { class: 'asc-phys-chips' }, chips.map(([id, label, n]) => {
      const el = h('button', {
        class: 'asc-phys-chip' + (id === 'pending' ? ' asc-chip-pending' : '')
          + (activeChip === id ? ' active' : ''),
      }, label, h('span', { class: 'asc-chip-count' }, String(n == null ? 0 : n)));
      el.addEventListener('click', () => { activeChip = id; renderRoster(container, ctx); });
      return el;
    }));
    container.appendChild(chipRow);

    const filtered = rows.filter((p) => {
      if (activeChip === 'pending') return p.verification_status === 'pending';
      if (activeChip === 'labelers') return p.tier === 'labeler';
      if (activeChip === 'reviewers') return p.tier === 'reviewer';
      // "Unassigned" means NO tier — not "a tier this filter forgot about".
      // Written as an explicit membership test so a fourth tier shows up in
      // its own chip rather than quietly swelling this one.
      if (activeChip === 'unassigned') return !TIERS.includes(p.tier);
      return true;
    });

    if (!filtered.length) {
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'No physicians match this filter.'))));
      return;
    }

    // Group by health system; Independent for physicians with none.
    const groups = new Map();
    filtered.forEach((p) => {
      const key = p.health_system_name || 'Independent';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(p);
    });
    const names = Array.from(groups.keys()).sort((a, b) => {
      if (a === 'Independent') return 1;
      if (b === 'Independent') return -1;
      return a.localeCompare(b);
    });

    const card = h('div', { class: 'asc-card' });
    names.forEach((name) => {
      const members = groups.get(name);
      const det = h('details', { class: 'asc-phys-group', open: '' },
        h('summary', {}, h('strong', {}, name),
          h('span', { class: 'asc-dim' }, members.length + ' physician' + (members.length === 1 ? '' : 's'))),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Name'), h('th', {}, 'Email'), h('th', {}, 'Phone'),
            h('th', {}, 'Specialty'), h('th', {}, 'Tier'), h('th', {}, 'Verification'),
            h('th', {}, 'Slack'), h('th', {}, 'Health system'))),
          h('tbody', {}, members.map((p) => physicianRow(ctx, container, p))))));
      card.appendChild(det);
    });
    container.appendChild(card);
  }

  function physicianRow(ctx, container, p) {
    const { h } = ctx;
    const tr = h('tr', { class: 'asc-row-click' },
      h('td', {}, h('strong', {}, p.name || '—')),
      h('td', {}, p.email || '—'),
      h('td', {}, p.phone || '—'),
      h('td', {}, p.specialty || '—'),
      h('td', {}, tierBadge(h, p.tier)),
      h('td', {}, verificationBadge(h, p.verification_status)),
      h('td', {}, p.slack_role ? slackText(p.slack_joined) + ' · ' + p.slack_role
        : slackText(p.slack_joined)),
      h('td', {}, p.health_system_name || 'Independent'));
    tr.addEventListener('click', () => { selectedId = p.id; rerender(); });
    return tr;
  }

  // ─── Profile: everything captured at onboarding ───────────
  async function renderProfile(container, ctx, id) {
    const { h, api, clear, loadingCard, fmtDate } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading profile…'));
    let data;
    try {
      data = await api('/admin/physicians/' + encodeURIComponent(id));
    } catch (e) {
      clear(container);
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, e.message || 'Could not load this physician.'))));
      return;
    }
    clear(container);
    const p = data.physician || {};

    const backBtn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' }, '← All physicians');
    backBtn.addEventListener('click', () => { selectedId = null; rerender(); });

    container.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', {},
          h('div', { class: 'asc-card-title' }, p.name || p.email || id),
          h('div', { class: 'asc-card-sub' },
            tierWord(p.tier), ' · ', p.specialty || 'No specialty', ' · ',
            p.health_system_name || 'Independent')),
        backBtn),
      h('div', { class: 'asc-card-pad asc-phys-profile-grid' },
        kvBlock(h, 'Identity', [
          ['Email', p.email], ['Phone', p.phone],
          ['NPI', p.npi], ['NPI check', npiWord(p.npi_verified)],
          ['LinkedIn', p.linkedin_url], ['CV on file', p.cv_on_file ? 'Yes' : 'No'],
        ]),
        kvBlock(h, 'Credentials', [
          ['Board certification', p.board_cert],
          ['Years of experience', p.years_experience != null ? String(p.years_experience) : null],
          ['Email domain', p.email_domain_class],
          // The Slack ROLE beside the joined flag, so whoever sends the invite
          // sets the label correctly (§6.1). Not a Slack integration.
          ['Slack', slackText(p.slack_joined) + (p.slack_role ? ' · ' + p.slack_role : '')],
        ]),
        kvBlock(h, 'Verification', [
          ['Status', verificationWord(p.verification_status)],
          ['Score', p.tier_score != null ? String(p.tier_score) : null],
          ['Assigned', p.tier_assigned_at ? fmtDate(p.tier_assigned_at) : null],
          ['Notes', p.verification_notes],
        ]))));

    // Contributor score (PRD-SCORE): the blended rating, its component
    // breakdown and the per-case trajectory. Best-effort: an absent score is
    // an absent card, never an error over a profile that loaded fine.
    try {
      const sc = await api('/admin/scores/' + encodeURIComponent(id));
      const rows = [
        ['Current score', sc.score != null ? String(sc.score) + ' · ' + (sc.band || '') : null],
        ['Initial rating', sc.prior != null ? String(sc.prior) + ' (' + (sc.prior_source || '') + ')' : null],
        ['Graded cases', String(sc.n_cases || 0)],
      ];
      const card = h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', {},
          h('div', { class: 'asc-card-title' }, 'Contributor score'),
          h('div', { class: 'asc-card-sub' },
            'Starts from the credential rating; every QA-graded case folds in.'))),
        h('div', { class: 'asc-card-pad asc-phys-profile-grid' },
          kvBlock(h, 'Rating', rows),
          kvBlock(h, 'Latest case',
            (sc.cases && sc.cases.length)
              ? Object.entries(sc.cases[sc.cases.length - 1].components || {}).map(
                  ([k, v]) => [k.replace(/_/g, ' '), v == null ? null : String(v)])
              : [['No graded cases yet', ' ']])));
      if (sc.history && sc.history.length) {
        card.appendChild(h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-card-sub' }, 'Trajectory (newest first)'),
          h('div', {}, sc.history.slice(0, 10).map((row) =>
            h('div', { class: 'asc-score-hist-row' },
              h('span', { class: 'asc-mono' }, String(row.score)),
              h('span', {}, row.case_score != null ? 'case ' + row.case_score : 'initial'),
              h('span', { class: 'asc-mono' }, row.created_at ? fmtDate(row.created_at) : ''))))));
      }
      container.appendChild(card);
    } catch (e) { /* score endpoint unavailable: the profile stands alone */ }

    // NPPES payload (raw registry answer) — shown when the NPI was checked.
    if (data.npi_payload) {
      const pre = h('pre', { class: 'asc-mono', style: 'font-size:12px;overflow:auto;max-height:280px' });
      pre.textContent = JSON.stringify(data.npi_payload, null, 2);
      container.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', {},
          h('div', { class: 'asc-card-title' }, 'NPPES registry payload'))),
        h('div', { class: 'asc-card-pad' }, pre)));
    }

    // Task history + review history (when a reviewer).
    container.appendChild(historyCard(ctx, 'Task history',
      ['When', 'Task', 'Status'],
      (data.task_history || []).map((t) => [
        t.created_at ? fmtDate(t.created_at) : '—', t.task_id || '—', t.status || '—'])));
    // Review history renders for the tier that holds the REVIEW capability.
    if (p.tier === 'reviewer') {
      container.appendChild(historyCard(ctx, 'Review history',
        ['When', 'Submission', 'Verdict'],
        (data.review_history || []).map((r) => [
          r.created_at ? fmtDate(r.created_at) : '—', r.submission_id || '—', r.verdict || '—'])));
    }
  }

  function npiWord(v) {
    if (v === 1 || v === true) return 'Verified';
    if (v === 0 || v === false) return 'Failed';
    return 'Not checked';
  }
  function verificationWord(v) {
    if (v === 'approved') return 'Approved';
    if (v === 'pending') return 'Pending';
    if (v === 'rejected') return 'Rejected';
    return 'Not checked';
  }

  function kvBlock(h, title, pairs) {
    const dl = h('dl', { class: 'asc-phys-kv' });
    pairs.forEach(([k, v]) => {
      dl.appendChild(h('dt', {}, k));
      if (v && /^https?:\/\//.test(String(v))) {
        dl.appendChild(h('dd', {}, h('a', { href: v, target: '_blank', rel: 'noopener noreferrer' }, v)));
      } else {
        dl.appendChild(h('dd', {}, v == null || v === '' ? '—' : String(v)));
      }
    });
    return h('div', {}, h('div', { class: 'asc-card-title', style: 'font-size:14px' }, title), dl);
  }

  function historyCard(ctx, title, headers, rows) {
    const { h } = ctx;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, title,
          h('span', { class: 'asc-badge asc-badge-count', style: 'margin-left:8px' }, String(rows.length))))));
    if (!rows.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' }, h('div', { class: 'asc-empty' }, 'Nothing yet.')));
      return card;
    }
    card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
      h('thead', {}, h('tr', {}, headers.map((x) => h('th', {}, x)))),
      h('tbody', {}, rows.map((cells) => h('tr', {}, cells.map((c) => h('td', {}, c))))))));
    return card;
  }

  window.AdminPhysiciansSection = {
    render,
    reset() { selectedId = null; activeView = 'roster'; activeChip = 'all'; },
  };
})();
