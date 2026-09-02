/* ═══════════════════════════════════════════════════════════
   Admin · Physicians section (Admin Launch PRD §2, §3, §6)

   window.AdminPhysiciansSection { render, reset }

   TWO tabs, carrying live counts, and nothing else:

     Approved and Labeling — the roster. Flat: no health-system
       grouping and no Verification column, because every row here
       is approved and a column whose every cell reads the same
       carries no information.
     Pending for Review — name and specialty only, because that is
       what you need to pick who to open next. Everything else is
       one click away in the decision report.

   The operator's job in this section is ONE decision loop, so the
   sub-tab strip that used to spread it over four screens (Roster /
   Signups / Verification / QA) is gone. Signups fold into Pending
   (a physician mid-wizard is a pending physician who cannot be
   decided yet, and they render as such); QA moved under Tasks,
   beside the work it grades.

   Vocabulary rules that carry meaning:
   - Tier renders as a WORD, never a raw token.
   - ADVISOR IS NOT A TIER (capabilities.py: the tier is retired).
     It lives on users.advisor_since and renders as a SECOND badge
     beside the tier, never instead of it. Rendering "Unassigned"
     over a real medical advisor is the quiet-wrong bug this file
     has warned about since its first version.
   - A ±0 reason line is grey, not green: "we could not check" is
     not a credit and is not a deduction.

   Loaded as its own file (§3.3); DOM built exclusively with ctx.h;
   zero innerHTML.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Mirrors asclepius/capabilities.py TIERS. The one list this file filters,
  // counts and labels against.
  const TIERS = ['labeler', 'reviewer'];

  let activeTab = null;         // 'approved' | 'pending' — null until counts decide
  let selectedId = null;        // approved physician → profile view
  let pendingId = null;         // pending physician → decision report
  let cache = null;             // last /admin/physicians payload
  let pendingCache = null;      // last { queue, signups } payload
  let rootEl = null;            // the section body we were mounted into
  let rootCtx = null;

  // ─── Vocabulary (four-state verification; tier words) ─────
  // A missing case here does not throw — it silently renders "Unassigned"
  // over a real physician, which is the same class of bug as the backend's
  // `tier === 'reviewer'` equality: wrong, quiet, and only discovered when
  // the person tells you. Keep this map aligned with
  // asclepius/capabilities.py TIER_WORDS.
  /* Bearer-authenticated like every other avatar read, so the bytes are
     fetched with the session token and handed over as a blob: URL. Returns
     null when there is no picture rather than an empty circle: a placeholder
     ring on a dossier is noise on a page that is already dense. */
  function dossierAvatar(h, p) {
    if (!p || !p.avatar_url) return null;
    var box = h('div', {
      style: 'width:52px;height:52px;border-radius:50%;overflow:hidden;flex:0 0 52px;'
        + 'background:var(--card-in);border:1px solid var(--hairline)',
    });
    var load = rootCtx && rootCtx.avatarBlob;
    if (!load) return null;
    load(p.avatar_url).then(function (objectUrl) {
      if (!objectUrl) return;
      box.appendChild(h('img', {
        src: objectUrl, alt: '',
        style: 'width:100%;height:100%;object-fit:cover;display:block',
      }));
    });
    return box;
  }

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
  function slackText(s) {
    if (s === true || s === 1) return 'Joined';
    if (s === false || s === 0) return 'Not joined';
    return '—'; // not checked
  }

  // ─── Section entry ───────────────────────────────────────
  // The shell no longer passes a view: this section owns its own tab strip
  // (§1.2). A legacy third argument is accepted and ignored rather than
  // throwing, so a cached older shell degrades to the default tab instead of
  // rendering nothing.
  function render(body, ctx) {
    rootEl = body; rootCtx = ctx;
    const { h, clear } = ctx;
    clear(body);
    const inner = h('div', {});
    body.appendChild(inner);
    if (pendingId) renderPendingDetail(inner, ctx, pendingId);
    else if (selectedId) renderProfile(inner, ctx, selectedId);
    else renderTabs(inner, ctx);
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

  function clearNode(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  /* ─── The two tabs (§2) ───────────────────────────────────────────────────
   *
   * Both counts are loaded before either tab renders, because the DEFAULT
   * depends on them: Pending opens first whenever anything is waiting, since
   * deciding a physician is the only thing in this section that is urgent.
   * Rendering the roster first and switching once the pending count arrived
   * would move the tab under the operator's cursor.
   */
  async function renderTabs(container, ctx) {
    const { h, api, clear, loadingCard } = ctx;
    clear(container);
    container.appendChild(loadingCard('Loading physicians…'));

    let approvedErr = null;
    let pendingErr = null;
    // Both in flight together. Each failure is contained to its own tab: an
    // operator must never lose the roster because the verification queue is
    // down, or lose the queue because the roster is.
    const [approvedRes, pendingRes] = await Promise.all([
      api('/admin/physicians').catch((e) => { approvedErr = errText(e); return null; }),
      loadPending(api).catch((e) => { pendingErr = errText(e); return null; }),
    ]);
    if (approvedRes) cache = approvedRes;
    if (pendingRes) pendingCache = pendingRes;

    const approved = (((cache || {}).physicians) || [])
      .filter((p) => p.verification_status === 'approved');
    const pendingRows = pendingList();

    // Default to Pending when its count > 0 — and only on the FIRST render, so
    // a deliberate switch to the roster is not undone by the next reload.
    //
    // A queue that FAILED to load also opens Pending, and that is the whole
    // point of the branch: "we could not read the queue" is not "the queue is
    // empty". Defaulting to the roster on an error would land the operator on a
    // healthy-looking screen while physicians waited unseen — which is exactly
    // the failure that once hid the entire verification queue for a build round.
    if (activeTab === null) {
      activeTab = (pendingRows.length || pendingErr) ? 'pending' : 'approved';
    }

    clear(container);
    // The DECIDABLE count, not the union. This chip is the one number an admin
    // reads before deciding whether to open the tab at all, and counting
    // mid-wizard signups in it made it say "7 waiting" when two accounts could
    // actually be decided: false urgency on the only signal there is.
    const decidableCount = pendingRows.filter((r) => r.kind === 'queued').length;
    container.appendChild(tabStrip(ctx, approved.length, decidableCount,
                                   approvedErr, pendingErr));
    // Above the tabs on purpose: an account in here is invisible in BOTH of
    // them, so a banner inside a tab would be hidden by the same bug it reports.
    const misfiled = misfiledCard(ctx, container);
    if (misfiled) container.appendChild(misfiled);

    if (activeTab === 'pending') {
      if (pendingErr) {
        container.appendChild(errorCard(h, 'The pending queue could not be loaded: ' + pendingErr));
        return;
      }
      renderPendingTab(container, ctx, pendingRows);
      // The learning loop's only window. Below the queue, collapsed: it is not
      // what an admin came here to do, but it has to be somewhere they see it.
      renderModelHealth(container, ctx);
      return;
    }
    if (approvedErr) {
      container.appendChild(errorCard(h, 'The roster could not be loaded: ' + approvedErr));
      return;
    }
    renderApprovedTab(container, ctx, approved);
  }

  /* ─── Physician credentials filed under an operator role ──────────────────
   *
   * The roster is `role === 'evaluator'`. An account whose row says
   * `role = 'admin'` is therefore not mislabelled, it is ABSENT — and so is it
   * from the verification queue and the tier backfill, which filter the same
   * way. A doctor in that state cannot be approved, tiered, or moved back,
   * because every control that would do it lives on a row that is not rendered.
   * They also never get real-data approval (it follows APPROVED + LABELING),
   * so the portal quietly serves them synthetic cases forever.
   *
   * The self-serve director onboarding provisioned `role="admin"` until it was
   * changed to `"evaluator"`; the code fix did not repair the rows it had
   * already written. This card is where those rows become visible, with the one
   * action that fixes them.  */
  function misfiledCard(ctx, container) {
    const { h } = ctx;
    const rows = ((cache || {}).misfiled_physicians) || [];
    if (!rows.length) return null;
    const list = h('div', { class: 'asc-misfiled-rows' });
    rows.forEach((p) => list.appendChild(misfiledRow(ctx, p, container)));
    return h('div', { class: 'asc-card asc-misfiled' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-chrome' }, 'Filed under the wrong role'),
      h('h3', {}, rows.length === 1
        ? 'One account has a physician\u2019s credentials and an operator\u2019s role.'
        : rows.length + ' accounts have physician credentials and an operator role.'),
      h('p', {},
        'They do not appear in the roster or the verification queue, so they '
        + 'cannot be approved, tiered, or given real cases from this console. '
        + 'Moving one to \u201cphysician\u201d puts it back in the roster below, '
        + 'where it can be verified and tiered like any other doctor.'),
      list));
  }

  function misfiledRow(ctx, p, container) {
    const { h, api } = ctx;
    const status = h('span', { class: 'asc-misfiled-status' }, '');
    const btn = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button' },
      'Move to physician');
    btn.onclick = async () => {
      btn.disabled = true;
      status.textContent = 'Moving\u2026';
      try {
        await api('/admin/users/' + encodeURIComponent(p.id) + '/role',
                  { method: 'POST', body: { role: 'evaluator' } });
        // Re-render rather than patch the row: it now belongs in the roster
        // below, and a half-updated screen is how an operator ends up trusting a
        // stale one. Same entry point the tabs themselves use.
        await renderTabs(container, ctx);
      } catch (e) {
        btn.disabled = false;
        // Self-demotion is refused by the server on purpose; say which case this
        // is rather than showing a bare failure.
        status.textContent = errText(e);
        status.classList.add('asc-inline-error');
      }
    };
    const bits = [p.role ? 'role: ' + p.role : null,
                  p.specialty || null,
                  p.verification_status ? 'verification: ' + p.verification_status
                                        : 'never verified',
                  p.tier ? 'tier: ' + p.tier : 'no tier'].filter(Boolean);
    return h('div', { class: 'asc-misfiled-row' },
      h('div', {},
        h('div', { class: 'asc-misfiled-email' }, p.email || p.name || p.id),
        h('div', { class: 'asc-misfiled-meta' }, bits.join(' \u00b7 '))),
      h('div', { class: 'asc-misfiled-act' }, status, btn));
  }

  function errText(e) {
    return (e && (e.detail || e.message)) || 'no response';
  }

  // AUDIT UI: `asc-inline-error` (soft, recoverable) — this panel failed, the
  // page is fine, try again. Distinct from `asc-error`, which is reserved for
  // the one blocking case: a module that did not load at all.
  function errorCard(h, message) {
    return h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-inline-error' }, message)));
  }

  function tabStrip(ctx, nApproved, nPending, approvedErr, pendingErr) {
    const { h } = ctx;
    return h('div', { class: 'asc-phys-chips' }, [
      ['approved', 'Approved and Labeling', nApproved, false, approvedErr],
      ['pending', 'Pending for Review', nPending, true, pendingErr],
    ].map(([id, label, n, urgent, failed]) => {
      const el = h('button', {
        type: 'button',
        class: 'asc-phys-chip' + (urgent && n ? ' asc-chip-pending' : '')
          + (activeTab === id ? ' active' : ''),
        // A count we could not read is '—', never '0'. A zero here would be a
        // confident claim that nobody is waiting, made on no information.
      }, label, h('span', { class: 'asc-chip-count' }, failed ? '—' : String(n)));
      el.addEventListener('click', () => {
        activeTab = id; selectedId = null; pendingId = null; rerender();
      });
      return el;
    }));
  }

  /* Tab B's source is a UNION of two populations that look identical to an
   * operator and are completely different to the system: physicians who
   * finished the wizard and hold an account (decidable) and physicians still
   * walking it (not decidable — there is no account to approve). The signups
   * half is best-effort: it is the secondary population, and a failure to load
   * it must not hide the physicians who ARE waiting for a decision. */
  async function loadPending(api) {
    const queue = await api('/verify/queue?status=pending');
    let signups = null;
    try {
      signups = await api('/admin/signups');
    } catch (e) {
      signups = null;
    }
    return { queue: queue, signups: signups };
  }

  function pendingList() {
    const pc = pendingCache || {};
    const queue = ((pc.queue || {}).queue) || [];
    const signups = ((pc.signups || {}).signups) || [];
    const rows = queue.map((q) => ({
      kind: 'queued',
      user_id: q.user_id,
      name: q.full_name || q.email || q.user_id,
      specialty: q.specialty || null,
      created_at: q.created_at || null,
      proposed_tier_word: q.proposed_tier_word || null,
      // One merged number. An admin triaging needs to know this row is not a
      // skim, not which KIND of not-a-skim it is; that is on the decision
      // screen, where they are already looking at it.
      look: (q.blockers || []).length + (q.flag_count || 0),
    }));
    /* Oldest first. The store returns created_at DESC because that ordering is
       shared with the approved roster, and newest-first is exactly wrong for a
       queue whose promise is "within 24 hours": it puts the person who has
       waited longest at the bottom. Sorted here rather than in the store, so
       the roster is untouched. */
    rows.sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
    // Mid-wizard physicians, after the decidable ones: the queue is the job,
    // the funnel is the chase list.
    signups.forEach((s) => {
      rows.push({
        kind: 'signup',
        user_id: null,
        name: s.name || s.email || '—',
        specialty: s.specialty || null,
        signup: s,
      });
    });
    return rows;
  }

  /* ─── Tab A — Approved and Labeling (§2.1) ────────────────────────────────
   * Flat. The health-system grouping is gone (it split one short roster into
   * accordions) and so is the Verification column (every row here is approved,
   * so the column said "Approved" on every line). */
  function renderApprovedTab(container, ctx, rows) {
    const { h } = ctx;
    if (!rows.length) {
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' },
          'No approved physicians yet. Decide the pending queue and they appear here.'))));
      return;
    }
    container.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Name'), h('th', {}, 'Email'), h('th', {}, 'Phone'),
          h('th', {}, 'Specialty'), h('th', {}, 'Tier'),
          /* The running contributor score. Internal: it is in no package and
           * no buyer sees it. It is on the roster because "who is doing good
           * work" was answerable only by opening physicians one at a time. */
          h('th', { title: 'Running quality score across graded cases. Internal.' },
            'Score'),
          /* Real-data approval was API-only: the flag gates the entire V4 real
           * de-identified queue, and the only way to grant it was curl. So the
           * real cases sat in the queue while every physician's picker showed
           * "Requires real-data approval" and nobody could clear it. */
          h('th', { title: 'BAA + training cleared: unlocks the real de-identified case queue' },
            'Real data'),
          h('th', {}, 'Community'),
          h('th', {}, ''))),
        h('tbody', {}, rows.map((p) => approvedRow(ctx, p)))))));
  }

  /* Advisor renders BESIDE the tier, never instead of it (§2.2).
   * `tierBadge` alone renders "Unassigned" over a real medical advisor,
   * because advisor is not a tier and never appears in users.tier. */
  function tierCell(h, row) {
    const badges = [tierBadge(h, row.tier)];
    if (row.is_advisor) {
      badges.push(h('span', { class: 'asc-badge asc-badge-lime' }, 'Advisor'));
    }
    return h('div', { style: 'display:flex;gap:6px;align-items:center' }, badges);
  }

  function approvedRow(ctx, p) {
    const { h } = ctx;
    const communityCell = h('td', {});
    const actionCell = h('td', {});
    paintCommunity(ctx, p, communityCell, actionCell);
    const realDataCell = h('td', {});
    paintRealData(ctx, p, realDataCell);

    const tr = h('tr', { class: 'asc-row-click' },
      h('td', {}, h('strong', {}, p.name || '—')),
      h('td', {}, p.email || '—'),
      h('td', {}, p.phone || '—'),
      h('td', {}, p.specialty || '—'),
      h('td', {}, tierCell(h, p)),
      /* An em dash, never a 0. Nobody has graded them yet is not the same
       * claim as they scored zero, and on a roster the two read identically. */
      h('td', { class: 'asc-mono' },
        (p.contributor_score === null || p.contributor_score === undefined)
          ? '—' : String(Math.round(Number(p.contributor_score)))),
      realDataCell,
      communityCell,
      actionCell);
    tr.addEventListener('click', (ev) => {
      // The invite button lives inside the row; a click on it must not also
      // open the profile underneath it.
      if (ev.target && typeof ev.target.closest === 'function' && ev.target.closest('button')) return;
      selectedId = p.id; pendingId = null; rerender();
    });
    return tr;
  }

  /* The Send Invite flow (§2.1, §5.1). On success the ROW updates in place —
   * no full re-render, because re-rendering the table under an operator who is
   * working down it moves every other row they were about to click. */
  function paintCommunity(ctx, p, cell, actionCell) {
    const { h, api } = ctx;
    clearNode(cell);
    clearNode(actionCell);
    if (p.slack_joined === true || p.slack_joined === 1) {
      cell.appendChild(h('span', {}, 'Joined'));
      return;
    }
    if (p._invited_at) {
      cell.appendChild(h('span', {}, 'Invited · ' + shortTime(p._invited_at)));
      return;
    }
    cell.appendChild(h('span', {}, p.slack_joined === false || p.slack_joined === 0
      ? 'Not joined' : '—'));

    const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      'Send Invite');
    btn.addEventListener('click', () => {
      btn.setAttribute('disabled', '');
      btn.textContent = 'Sending…';
      api('/admin/community/invite', { method: 'POST', body: { user_id: p.id } })
        .then((res) => {
          if (res && res.already_joined) {
            p.slack_joined = true;
          } else {
            p._invited_at = (res && res.invited_at) || new Date().toISOString();
          }
          paintCommunity(ctx, p, cell, actionCell);
        })
        .catch((e) => {
          btn.removeAttribute('disabled');
          btn.textContent = 'Send Invite';
          // Inline, on the row, never a toast: a toast about one row in a table
          // of thirty does not say WHICH row failed.
          clearNode(actionCell);
          actionCell.appendChild(btn);
          actionCell.appendChild(h('div', { class: 'asc-inline-error' }, errText(e)));
        });
    });
    actionCell.appendChild(btn);
  }

  /* Real-data approval (EHR PRD §9.5). The flag records the admin's decision that
   * this contributor's BAA and training are done; the attestation itself lives
   * outside the system. Serving enforces it on EVERY draw, so this control is the
   * honest surface for a decision the server was already making — not the gate.
   *
   * Grant and revoke both go through the same button, and the row repaints in
   * place on success, matching Send Invite: re-rendering the table under an
   * operator working down it moves every other row they were about to click.
   */
  function paintRealData(ctx, p, cell) {
    const { h, api } = ctx;
    clearNode(cell);
    const approved = !!p.real_data_approved;
    cell.appendChild(h('span', {
      class: 'asc-badge ' + (approved ? 'asc-badge-lime' : 'asc-badge-gray'),
      style: 'margin-right:8px',
    }, approved ? 'Approved' : 'Not approved'));

    /* PRD CASE-BATCHES §2.5 — route cases from the physician's own row.
     * Only offered once they are approved for real data: routing to an
     * unapproved account is refused at send anyway (the V4 wall), and a button
     * that always fails is worse than no button. This does not send anything —
     * it opens Batches with this doctor pre-picked, so there is exactly one
     * screen where "who gets what" is decided. */
    if (approved && ctx.openBatchesFor) {
      const route = h('button', {
        class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
        style: 'margin-right:8px',
      }, 'Route cases');
      route.addEventListener('click', () => ctx.openBatchesFor(p));
      cell.appendChild(route);
    }

    const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      approved ? 'Revoke' : 'Approve');
    btn.addEventListener('click', () => {
      btn.setAttribute('disabled', '');
      btn.textContent = approved ? 'Revoking…' : 'Approving…';
      api('/users/' + encodeURIComponent(p.id) + '/real-data-approval',
          { method: 'POST', body: { approved: !approved } })
        .then((updated) => {
          // Trust the server's echo of the user, not the optimistic guess.
          p.real_data_approved = !!(updated && updated.real_data_approved);
          paintRealData(ctx, p, cell);
        })
        .catch((e) => {
          btn.removeAttribute('disabled');
          btn.textContent = approved ? 'Revoke' : 'Approve';
          // Inline, on the row, never a toast: a toast about one row in a table
          // of thirty does not say WHICH row failed.
          cell.appendChild(h('div', { class: 'asc-inline-error' }, errText(e)));
        });
    });
    cell.appendChild(btn);
  }

  function shortTime(iso) {
    const s = String(iso || '');
    const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(s);
    return m ? m[1] + ' ' + m[2] : (s.slice(0, 16) || '—');
  }

  /* ─── Tab B — Pending for Review (§2.3) ───────────────────────────────────
   * Two fields render: name and specialty. Everything else about a candidate
   * is one click away, and putting it here made the queue a table nobody could
   * scan. */
  function renderPendingTab(container, ctx, rows) {
    const { h } = ctx;
    if (!rows.length) {
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' },
          'Queue is clear — nobody is waiting for a decision, and nobody is '
          + 'mid-onboarding.'))));
      return;
    }
    /* Two headed tables, one tab. They are two different jobs -- a decision and
       a chase -- and they were interleaved in one list distinguished only by a
       chip. A third TAB would have buried the chase list behind a tab nobody
       opens and split one operator loop across three screens, which is what
       this file's header comment says was removed once already. */
    const decidable = rows.filter((r) => r.kind === 'queued');
    const signups = rows.filter((r) => r.kind !== 'queued');

    if (decidable.length) {
      container.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' },
          h('div', { class: 'asc-card-title' },
            'Waiting on your decision (' + decidable.length + ')')),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Name'), h('th', {}, 'Specialty'), h('th', {}, 'Waiting'),
            h('th', {}, 'Proposed'), h('th', {}, 'Look'), h('th', {}, ''))),
          h('tbody', {}, decidable.map((r) => pendingRow(ctx, r)))))));
    } else {
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'Nobody is waiting on a decision.'))));
    }

    if (signups.length) {
      container.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' },
          h('div', { class: 'asc-card-title' },
            'Still filling in the wizard (' + signups.length + ')')),
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Name'), h('th', {}, 'Specialty'), h('th', {}, ''))),
          h('tbody', {}, signups.map((r) => pendingRow(ctx, r)))))));
    }
  }

  /* How long they have been waiting, against the promise the workspace-ready
     email makes ("within 24 hours"). Amber past a day, because the number is
     only useful if the overdue ones are visible without reading every row. */
  function waitingCell(h, iso) {
    if (!iso) return h('td', {}, '—');
    const then = new Date(String(iso).endsWith('Z') || String(iso).includes('+')
      ? iso : iso + 'Z');
    const days = Math.floor((Date.now() - then.getTime()) / 86400000);
    if (!isFinite(days) || days < 0) return h('td', {}, '—');
    const text = days < 1 ? 'today' : days + 'd';
    if (days < 1) return h('td', {}, text);
    return h('td', {}, h('span', { class: 'asc-badge asc-badge-amber' }, text));
  }

  function pendingRow(ctx, r) {
    const { h } = ctx;
    if (r.kind === 'signup') {
      // No account exists yet, so there is nothing to approve and this row is
      // NOT clickable into a decision. It keeps the resend action, which is the
      // only thing that can actually move this physician forward — and this is
      // the only screen a half-finished physician appears on at all.
      return h('tr', {},
        h('td', {}, h('strong', {}, r.name),
          h('span', { class: 'asc-phys-chip asc-chip-stat', style: 'margin-left:8px',
                       // Two fields render (§2.3), so where they stopped rides on
                       // the chip rather than taking a third column — it is what
                       // tells an operator whether Resend is worth pressing.
                       title: r.signup.stage_word || 'Onboarding not finished' },
            'Signup incomplete'),
          h('span', { class: 'asc-dim', style: 'margin-left:6px' },
            'step ' + r.signup.stage_index + '/' + r.signup.stage_total)),
        h('td', {}, r.specialty || '—'),
        h('td', {}, resendBtn(ctx, r.signup)));
    }
    const tr = h('tr', { class: 'asc-row-click' },
      h('td', {}, h('strong', {}, r.name)),
      h('td', {}, r.specialty || '—'),
      waitingCell(h, r.created_at),
      // The proposal, not the score. A number in a queue invites deciding on
      // the number; the tier word says which rows need real thought.
      h('td', {}, r.proposed_tier_word || '—'),
      // Rendered only when non-zero: an always-present count makes every row
      // look flagged, which is the same as none of them being flagged.
      h('td', {}, r.look
        ? h('span', { class: 'asc-badge asc-badge-amber' }, String(r.look))
        : ''),
      h('td', {}, '→'));
    tr.addEventListener('click', () => { pendingId = r.user_id; rerender(); });
    return tr;
  }

  function resendBtn(ctx, s) {
    const { h, api, toast } = ctx;
    const canResend = ((pendingCache || {}).signups || {}).can_resend !== false;
    const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      'Resend link');
    if (!canResend) {
      btn.setAttribute('disabled', '');
      btn.setAttribute('title', 'Email is not configured on this server (SendGrid or SMTP).');
    }
    btn.addEventListener('click', () => {
      btn.setAttribute('disabled', '');
      btn.textContent = 'Sending…';
      api('/admin/signups/resend', {
        method: 'POST',
        body: { health_system_id: s.health_system_id, email: s.email },
      }).then((res) => {
        btn.textContent = 'Link resent';
        if (toast) toast((res && res.message) || ('Link resent to ' + s.email));
      }).catch((e) => {
        btn.removeAttribute('disabled');
        btn.textContent = 'Resend link';
        if (toast) toast(errText(e), 'error');
      });
    });
    return btn;
  }

  /* ═══ §3 — The pending decision report ════════════════════════════════════
   *
   * ONE API call. GET /verify/queue/{user_id} already embeds the tiering
   * proposal, the NPPES payload, the duplicate claimants and the tier
   * vocabulary; a second round trip for the recommendation would be a second
   * chance for the screen to disagree with itself.
   *
   * `case_domain` is deliberately not passed. It defaults to the physician's
   * own declared specialty, which is the honest question at signup: could this
   * person adjudicate in their own domain?
   */
  async function renderPendingDetail(container, ctx, userId) {
    const { h, api, clear, loadingCard, fmtDate } = ctx;
    clear(container);
    container.appendChild(backBar(ctx, 'pending'));
    const slot = h('div', {});
    container.appendChild(slot);
    slot.appendChild(loadingCard('Loading the report…'));

    let d;
    try {
      d = await api('/verify/queue/' + encodeURIComponent(userId));
    } catch (e) {
      clearNode(slot);
      slot.appendChild(errorCard(h, 'This physician could not be loaded: ' + errText(e)));
      return;
    }
    clearNode(slot);

    const words = d.tier_words || {};
    const proposed = d.proposed_tier || null;

    slot.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, d.full_name || d.email || userId),
        h('div', { class: 'asc-card-sub' }, d.specialty || 'No specialty declared')))));

    /* The order below follows what an admin actually does, and the rule is:
     * facts that can change the decision are OPEN, raw material you consult
     * when a fact looks wrong is COLLAPSED, and anything whose text the
     * applicant controls is collapsed, below the buttons, and labelled.
     *
     * The buttons therefore sit below the evidence rather than above it, so a
     * decision is a longer scroll than it used to be. That is deliberate and it
     * is the point of this screen: the failure being fixed is deciding without
     * looking. If per-decision speed becomes the complaint, the answer is
     * queue-level triage, not moving the buttons back above the evidence.
     *
     * Every renderer here is the one the approved-physician profile already
     * used. They were written, and then this screen kept its own smaller
     * version, which is how a non-US doctor's card came to show a blank where
     * their registration number belongs. */

    // ── WORTH A LOOK — first, open, and only when there is something ──
    const flags = d.flags || [];
    if (flags.length) slot.appendChild(flagsCard(h, flags));

    // ── IDENTITY ──
    const idRows = identityRows(dossierIdentity(d));
    idRows.push(['Specialty', d.specialty]);
    idRows.push(['Clinical role', (d.clinical_role || '').replace(/_/g, ' ') || null]);
    idRows.push(['Organisation', d.org_name]);
    idRows.push(['Submitted', d.created_at ? fmtDate(d.created_at) : null]);
    const idCard = sectionCard(ctx, 'Identity', kvBlock(h, '', idRows));
    const reg = d.registry || {};
    if (reg.note) {
      // The instruction for a regulator we cannot query by API. Computed,
      // serialized, and rendered nowhere until now.
      idCard.querySelector('.asc-card-pad').appendChild(
        h('div', { class: 'asc-dim' }, reg.note));
    }
    const dupes = d.duplicate_claims || [];
    if (dupes.length) {
      // Two accounts on one NPI is decision-changing, and it was invisible here.
      const pad = idCard.querySelector('.asc-card-pad');
      pad.appendChild(h('div', { class: 'vq-section-label' },
        'Also claiming this number (' + dupes.length + ')'));
      dupes.forEach((c) => {
        pad.appendChild(h('div', { class: 'asc-badge asc-badge-amber' },
          (c.email || 'unknown') + ' · ' + (c.verification_status || 'no decision')));
      });
    }
    slot.appendChild(idCard);

    // ── CREDENTIALS AS TYPED — the thing the decision is nominally about ──
    const creds = credentialsAsTyped(h, d.credentials || {}, d.attestations || {}, {
      years_experience: d.years_experience,
      board_cert: d.board_cert,
      cv_conflicts: d.cv_conflicts || [],
    });
    if (creds) slot.appendChild(creds);

    // ── RECOMMENDATION ──
    slot.appendChild(recommendationCard(ctx, d, proposed, words));

    // ── YOUR DECISION ──
    slot.appendChild(decisionCard(ctx, userId, d, proposed, words));

    // ── Raw material, collapsed, below the decision ──
    slot.appendChild(cvCard(ctx, userId, d));
    if (d.npi_payload) slot.appendChild(npiPayloadCard(h, d.npi_payload));

    // ── Background research, BELOW the buttons and never in the reasons ──
    const research = d.agent_research || [];
    if (research.length) slot.appendChild(researchCard(ctx, research));
  }

  /* The dossier and the profile carry the same identity facts in two shapes.
     ONE renderer reads them, because the reason a non-US doctor's card was
     blank on this screen is that this screen had its own. */
  function dossierIdentity(d) {
    const reg = d.registry || {};
    const npi = d.npi || {};
    const out = {
      email: d.email,
      phone: d.phone,
      linkedin_url: d.linkedin_url,
      cv_on_file: !!d.has_cv,
      country_of_practice: d.country_of_practice,
    };
    if (reg && reg.is_us === false) {
      out.country_of_licensure = reg.country;
      out.registry_name = reg.registry_name || reg.id_label;
      out.registry_id = reg.identifier;
      out.registry_verified = reg.verified;
      out.registry_lookup_url = reg.lookup_url;
    } else {
      out.npi = npi.npi;
      out.npi_verified = npi.result;
      /* Deliberately NOT collapsed through npiWord's tri-state. The result has
         five values, and folding UNAVAILABLE into NOT_FOUND has already
         shipped and been caught once in this codebase. identityRows renders
         this string when it is present and falls back to npiWord otherwise, so
         the profile screen is untouched and this one keeps the richer answer. */
      out.npi_check_text = npiLineText(d);
    }
    return out;
  }

  /* The parsed CV, collapsed. The typed record is primary and the parse is a
     cross-check; where they DISAGREE is already open, in the credentials card. */
  function cvCard(ctx, userId, d) {
    const { h } = ctx;
    const det = h('details', { class: 'asc-card' });
    det.appendChild(h('summary', { class: 'asc-card-head' },
      h('div', { class: 'asc-card-title' }, 'CV')));
    const pad = h('div', { class: 'asc-card-pad' });
    if (d.has_cv) {
      pad.appendChild(cvLink(ctx, userId, d));
      const parsed = d.cv_parsed || {};
      const rows = Object.keys(parsed)
        .filter((k) => k !== 'ok' && typeof parsed[k] !== 'object')
        .map((k) => [k.replace(/_/g, ' '), String(parsed[k])]);
      if (rows.length) pad.appendChild(kvBlock(h, '', rows));
    } else {
      pad.appendChild(h('div', { class: 'asc-dim' }, 'No CV uploaded'));
    }
    det.appendChild(pad);
    return det;
  }

  function backBar(ctx, tab) {
    const { h } = ctx;
    const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      tab === 'pending' ? '← Back to pending' : '← All physicians');
    btn.addEventListener('click', () => { pendingId = null; selectedId = null; rerender(); });
    return h('div', { style: 'margin-bottom:12px' }, btn);
  }

  function sectionCard(ctx, title) {
    const { h } = ctx;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', { class: 'asc-card-title' }, title)));
    const pad = h('div', { class: 'asc-card-pad' });
    for (let i = 2; i < arguments.length; i++) {
      if (arguments[i]) pad.appendChild(arguments[i]);
    }
    card.appendChild(pad);
    return card;
  }

  function npiLineText(d) {
    const npi = d.npi || {};
    if (!npi.npi) return null;
    // The verification STATE travels with the number. An NPI with no state
    // beside it reads as verified to every operator who has ever seen one.
    const state = npi.result ? String(npi.result).replace(/_/g, ' ') : 'not checked';
    return npi.npi + ' · ' + state;
  }

  function cvLink(ctx, userId, d) {
    const { h } = ctx;
    const btn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      d.cv_ok ? 'View CV' : 'View CV (could not parse — read the file)');
    btn.addEventListener('click', () => { openCv(ctx, userId); });
    return h('div', {}, btn);
  }

  function openCv(ctx, userId) {
    // The CV is bytes behind an admin bearer token, so it cannot be a plain
    // href. ctx.downloadBlob already carries the token and the filename.
    if (typeof ctx.downloadBlob === 'function') {
      ctx.downloadBlob('/verify/queue/' + encodeURIComponent(userId) + '/cv',
                       'cv-' + userId);
    }
  }

  /* Reasons render VERBATIM and in order — they are already written for a
   * human, and re-sorting or re-wording them is how an admin ends up reading a
   * different explanation from the one the model recorded.
   *
   * §6: a `±0` line is grey. It means "we could not check" (NPPES unavailable)
   * or "checked and found nothing", neither of which is a credit. Painting it
   * like a `+n` line is how "we could not check" turns into "this person is
   * verified" in an operator's head — and collapsing UNAVAILABLE into NOT_FOUND
   * has already shipped and been caught once in this codebase. */
  function reasonLine(h, text) {
    const s = String(text || '');
    // Anything that is not a `+n` credit renders muted. That is `±0` ("we could
    // not check" / "checked, nothing there") AND the negative lines, which
    // credentialing emits WITHOUT a leading plus — `-4 consumer email domain
    // (not disqualifying)`. Keying on the plus rather than on `±0` alone is what
    // stops a deduction from rendering with the same weight as a credit.
    const credit = s.charAt(0) === '+';
    return h('div', { class: credit ? 'vq-reason' : 'vq-attempt' }, s);
  }

  function recommendationCard(ctx, d, proposed, words) {
    const { h } = ctx;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', {}, h('div', { class: 'asc-card-title' }, 'Recommendation')),
        h('div', { class: 'asc-mono' },
          d.score == null ? 'score —' : 'score ' + d.score)));
    const pad = h('div', { class: 'asc-card-pad' });
    pad.appendChild(h('div', { class: 'vq-section-label' },
      proposed ? (words[proposed] || proposed)
        : 'No tier proposed — the decision is entirely yours'));
    (d.reasons || []).forEach((r) => { pad.appendChild(reasonLine(h, r)); });

    const blockers = d.blockers || [];
    pad.appendChild(h('div', { class: 'vq-section-label' },
      'Blockers' + (blockers.length ? '' : '   (none)')));
    // Amber, and BOTH approve buttons stay live below. A blocker is a review
    // flag, not a rejection (credentialing.py) — suppressing the buttons would
    // turn a flag into a veto that nobody chose to give it.
    blockers.forEach((b) => {
      pad.appendChild(h('div', { class: 'asc-badge asc-badge-amber', style: 'display:block;margin:4px 0' },
        String(b)));
    });
    gateNote(h, pad, d, proposed, words);
    card.appendChild(pad);
    return card;
  }

  /* ─── What the GATE LAYER says, when it does not say the same thing ───────
   *
   * Two different things compute a recommendation for this physician:
   *
   *   `score` / `proposed_tier` / `reasons` — credentialing.propose_tier(), the
   *     credential heuristic. That is what §3.2 lays out above, and it is what
   *     an operator reads.
   *   `tiering` — the Bayesian model with hard_gates() in front of it. Those
   *     gates are POLICY, never learned, and it is the model this screen's
   *     decision trains.
   *
   * They can disagree, and on a real signup they routinely do: the heuristic
   * scores credentials while the gates ask whether a licence number is on file
   * and whether the OIG exclusion list was reachable. A screen that showed only
   * the heuristic would offer "Approve as Reviewer" as the primary action over
   * a physician the gate layer is still holding — promoting someone quietly,
   * which is the exact failure this codebase keeps writing tests against.
   *
   * So it is SHOWN, not enforced. Blockers are review flags, and so is this: the
   * buttons stay live and the admin decides. What changes is that they decide
   * knowing both answers.
   */
  function gateNote(h, pad, d, proposed, words) {
    const t = d.tiering || {};
    if (t.error) {
      pad.appendChild(h('div', { class: 'asc-inline-error' }, String(t.error)));
      return;
    }
    const missing = t.tr_missing || [];
    if (t.tr_eligible === false && missing.length) {
      pad.appendChild(h('div', { class: 'vq-attempt' },
        'Not yet reviewer-eligible: ' + missing.join(', ')
        + '. This is the gate layer, which is policy and is never learned — it is '
        + 'not a veto, and both approve buttons stay live.'));
    }
    // The tier the LEARNING model would have proposed, when it differs from the
    // one above. This is what /decide compares the decision against, so an admin
    // agreeing with the screen should be able to see when the model disagrees.
    const modelTier = t.proposed_tier || null;
    if (modelTier !== proposed) {
      pad.appendChild(h('div', { class: 'vq-attempt' },
        'The learning model proposes '
        + (modelTier ? (words[modelTier] || modelTier) : 'no tier yet')
        + (t.score == null ? '' : ' (score ' + Number(t.score).toFixed(2) + ')')
        + ' — a different question from the credential score above, and the one '
        + 'your decision is recorded against.'));
    }
  }

  /* ═══ §3.3 — THE DECISION IS THE TRAINING SIGNAL ══════════════════════════
   *
   * POST /verify/tiering/{id}/decide fires on EVERY decision, including
   * agreement with the recommendation, and it fires BEFORE /approve.
   *
   * Both halves of that matter:
   *
   *   Agreement too — apply_decision_batch() learns from agreement and
   *   disagreement equally. A UI that posted only on override would hand the
   *   model a training set made entirely of its own mistakes, and it would
   *   drift badly while every screen kept looking healthy.
   *
   *   Decide FIRST — decide records the observation without re-approving, so it
   *   is safe to run first. The other order is not: if approve succeeded and
   *   decide then failed, the physician would be live with no training signal
   *   and nothing in the system would ever reconcile it.
   */
  function decisionCard(ctx, userId, d, proposed, words) {
    const { h, api } = ctx;
    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', { class: 'asc-card-title' }, 'Your decision')));
    const pad = h('div', { class: 'asc-card-pad' });

    const note = h('textarea', {
      class: 'vq-note', rows: '2',
      placeholder: 'Note — required to reject, recommended always',
    });
    const status = h('div', {});
    const actions = h('div', { class: 'vq-actions' });

    const buttons = [];
    // Two guards, deliberately. `disabled` is what a browser honours — a real
    // Chromium drops every click after the first, verified. `inFlight` is what
    // makes that true independently of the DOM: it costs one boolean and it
    // holds if a button ever becomes a div, if a handler is invoked
    // programmatically, or under a test harness that fires on disabled nodes.
    // This surface writes verification_status AND feeds the training set, so a
    // second submission is a second approval and a second observation.
    let inFlight = false;
    function setBusy(on) {
      inFlight = on;
      buttons.forEach((b) => {
        if (on) b.setAttribute('disabled', '');
        else b.removeAttribute('disabled');
      });
    }

    TIERS.forEach((tier) => {
      // The recommended tier is primary; the other stays ghost. Both are always
      // present and always enabled — see the blocker note above.
      const isProposed = tier === proposed;
      const btn = h('button', {
        type: 'button',
        class: 'asc-btn asc-btn-sm ' + (isProposed ? 'asc-btn-primary' : 'asc-btn-ghost'),
      }, 'Approve as ' + (words[tier] || tier));
      btn.addEventListener('click', () => {
        if (inFlight) return;
        setBusy(true);
        clearNode(status);
        status.appendChild(h('div', { class: 'asc-dim' }, 'Recording…'));
        approveWithSignal(ctx, userId, tier, note.value || null)
          .then((line) => {
            clearNode(status);
            status.appendChild(h('div', { class: 'vq-reason' }, line));
            // The physician has left the pending queue, so the cached list is
            // stale. Drop it and go back — the queue reloads on the way.
            pendingCache = null;
            setTimeout(() => { pendingId = null; rerender(); }, 900);
          })
          .catch((e) => {
            setBusy(false);
            clearNode(status);
            status.appendChild(h('div', { class: 'asc-inline-error' }, errText(e)));
          });
      });
      buttons.push(btn);
      actions.appendChild(btn);
    });

    const rejectBtn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      'Reject');
    rejectBtn.addEventListener('click', () => {
      if (inFlight) return;
      const text = (note.value || '').trim();
      if (!text) {
        clearNode(status);
        status.appendChild(h('div', { class: 'asc-inline-error' },
          'A rejection needs a note. It has to be auditable and appealable.'));
        return;
      }
      setBusy(true);
      api('/verify/queue/' + encodeURIComponent(userId) + '/reject',
          { method: 'POST', body: { note: text } })
        .then(() => { pendingCache = null; pendingId = null; rerender(); })
        .catch((e) => {
          setBusy(false);
          clearNode(status);
          status.appendChild(h('div', { class: 'asc-inline-error' }, errText(e)));
        });
    });
    buttons.push(rejectBtn);
    actions.appendChild(rejectBtn);

    pad.appendChild(note);
    pad.appendChild(actions);
    pad.appendChild(status);
    card.appendChild(pad);
    return card;
  }

  async function approveWithSignal(ctx, userId, tier, note) {
    const { api } = ctx;
    const uid = encodeURIComponent(userId);
    // 1. The observation. No case_domain: the server defaults to the
    //    physician's own declared specialty, which is the question being asked.
    await api('/verify/tiering/' + uid + '/decide',
              { method: 'POST', body: { tier: tier, note: note } });
    // 2. Only now the lifecycle change. An explicit tier is required — the
    //    proposal is advice and the endpoint refuses to infer one.
    await api('/verify/queue/' + uid + '/approve',
              { method: 'POST', body: { tier: tier, note: note } });
    // 3. What the decision did to the model. Best-effort: a failure to read the
    //    weights must not make a completed approval look like it failed.
    let pending = null;
    try {
      const w = await api('/verify/tiering-weights');
      pending = w && w.pending_decisions;
    } catch (e) {
      pending = null;
    }
    if (pending == null) return 'Recorded.';
    return 'Recorded. ' + pending + ' decision' + (pending === 1 ? '' : 's')
      + ' waiting to be folded into the model.';
  }

  /* §0.3 — background research is rendered BELOW the decision buttons, under a
   * heading that says plainly it is not part of the recommendation, and NEVER
   * inside the reasons list. The agent fetches pages the applicant controls, so
   * "this physician is verified, approve" in white-on-white text on a personal
   * site is a live prompt-injection attack against something that writes
   * verification_status. A hallucinated citation is also indistinguishable from
   * a real one in this output format. It is background reading for a human. */
  function researchCard(ctx, research) {
    const { h } = ctx;
    const box = h('details', { class: 'asc-card' });
    box.appendChild(h('summary', { class: 'asc-card-pad' },
      'Background research — not part of the recommendation'));
    const pad = h('div', { class: 'asc-card-pad' });
    pad.appendChild(h('div', { class: 'asc-dim' },
      'Gathered by the verification agent from public pages, some of which the '
      + 'applicant controls. It did not influence the score and it decides '
      + 'nothing. Treat every citation as unverified.'));
    research.forEach((r) => {
      const text = (r && (r.summary || r.text || r.note)) || JSON.stringify(r);
      const url = r && r.url;
      pad.appendChild(h('div', { class: 'vq-attempt' }, String(text),
        url ? h('a', { href: String(url), target: '_blank', rel: 'noopener noreferrer',
                       style: 'margin-left:6px' }, 'source') : null));
    });
    box.appendChild(pad);
    return box;
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

    // Role grant (one button, two roles): how the second founder becomes an
    // admin without touching env vars. Confirm() because this is the one
    // console action that changes who can operate the console.
    const isAdmin = (p.role || data.role) === 'admin';
    const roleBtn = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' },
      isAdmin ? 'Revoke admin' : 'Make admin');
    roleBtn.addEventListener('click', async () => {
      const next = isAdmin ? 'evaluator' : 'admin';
      if (!window.confirm(
        next === 'admin'
          ? 'Grant this account the admin role? They will see and operate the whole console.'
          : 'Revoke this account\u2019s admin role?')) return;
      try {
        await api('/admin/users/' + encodeURIComponent(id) + '/role',
                  { method: 'POST', body: { role: next } });
        rerender();
      } catch (e) {
        window.alert((e && (e.detail || e.message)) || 'That did not save.');
      }
    });

    container.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        // The face beside the name, when there is one. This is the surface it
        // is arguably most useful on: an admin here is the person answering
        // "is this the same doctor as the registry entry I have open?"
        h('div', { style: 'display:flex;align-items:center;gap:14px;min-width:0' },
          dossierAvatar(h, p),
          h('div', { style: 'min-width:0' },
            h('div', { class: 'asc-card-title' }, p.name || p.email || id),
            h('div', { class: 'asc-card-sub' },
              tierWord(p.tier), ' · ', p.specialty || 'No specialty', ' · ',
              p.health_system_name || 'Independent'))),
        h('div', { style: 'display:flex;gap:8px' }, roleBtn, backBtn)),
      h('div', { class: 'asc-card-pad asc-phys-profile-grid' },
        kvBlock(h, 'Identity', identityRows(p)),
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

    // What the doctor actually typed. All of this was captured at signup and
    // rendered nowhere, so "check their credentials" could not be done from
    // the credentials page: the licence number, the training, the practice
    // status and the initials they signed with were invisible on every admin
    // surface.
    if (data.flags && data.flags.length) container.appendChild(flagsCard(h, data.flags));
    const asTyped = credentialsAsTyped(h, data.credentials || {}, data.attestations || {});
    if (asTyped) container.appendChild(asTyped);

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
    if (data.npi_payload) container.appendChild(npiPayloadCard(h, data.npi_payload));

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

  /* The raw registry answer. Collapsed on the decision screen and open on the
     profile, because on the decision screen it is what you consult when a fact
     looks wrong rather than a fact you read. */
  function npiPayloadCard(h, payload) {
    const pre = h('pre', { class: 'asc-mono', style: 'font-size:12px;overflow:auto;max-height:280px' });
    pre.textContent = JSON.stringify(payload, null, 2);
    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'NPPES registry payload'))),
      h('div', { class: 'asc-card-pad' }, pre));
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

  /* Identity reads differently depending on which registry answers for this
     doctor. Showing "NPI: —" for a physician registered with SCFHS is a blank
     where the actual credential should be. */
  function identityRows(p) {
    const rows = [['Email', p.email], ['Phone', p.phone]];
    const licensure = (p.country_of_licensure || '').toUpperCase();
    if (!licensure || licensure === 'US') {
      // npi_check_text carries the five-state answer when the caller has it
      // (the decision screen does). npiWord's tri-state is the fallback, and
      // folding UNAVAILABLE into NOT_FOUND has shipped once already.
      rows.push(['NPI', p.npi],
                ['NPI check', p.npi_check_text || npiWord(p.npi_verified)]);
    } else {
      rows.push([p.registry_name || 'Registration', p.registry_id]);
      rows.push(['Registry check', npiWord(p.registry_verified)]);
      if (p.registry_lookup_url) rows.push(['Check by hand', p.registry_lookup_url]);
      rows.push(['Licensed in', licensure]);
      if (p.country_of_practice && p.country_of_practice !== licensure) {
        rows.push(['Practising in', p.country_of_practice]);
      }
    }
    rows.push(['LinkedIn', p.linkedin_url], ['CV on file', p.cv_on_file ? 'Yes' : 'No']);
    return rows;
  }

  /* Severity to class, spelled out rather than concatenated. Building a class
     name out of a data field puts whatever that field contains into the DOM
     and leaves the stylesheet's own classes unsearchable, which is what
     test_no_new_component_vocabulary_beyond_the_prd exists to catch. */
  const FLAG_SEVERITY_CLASS = {
    high: 'asc-phys-flag-high',
    medium: 'asc-phys-flag-medium',
    low: 'asc-phys-flag-low',
  };

  /* What did not hold together about this signup. Review flags, never
     rejections: the point is that a person looks. */
  function flagsCard(h, flags) {
    const card = h('div', { class: 'asc-card' });
    card.appendChild(h('div', { class: 'asc-card-head' },
      h('div', { class: 'asc-card-title' }, 'Worth a look (' + flags.length + ')')));
    const pad = h('div', { class: 'asc-card-pad' });
    flags.forEach((f) => {
      pad.appendChild(h('div', { class: 'asc-phys-flag' },
        h('span', {
          class: 'asc-phys-flag-sev '
            + (FLAG_SEVERITY_CLASS[f.severity] || FLAG_SEVERITY_CLASS.low),
        }, (f.severity || 'low').toUpperCase()),
        h('span', {}, String(f.field || '').replace(/_/g, ' ') + ': '
          + String(f.issue || '').replace(/_/g, ' ')
          + (f.detail ? ': ' + f.detail : ''))));
    });
    card.appendChild(pad);
    return card;
  }

  /* The terms a physician signed, by name. An admin declining to pay for a
     case should be able to see that the doctor agreed to the quality term
     before they did the work, without opening a JSON blob. */
  const ATTESTATION_LABELS = {
    consentCredentialShare: 'credential sharing',
    attestIndependentJudgment: 'independent judgment',
    ipAssignment: 'IP assignment',
    noPhi: 'no PHI',
    attestWorkQuality: 'paid only if it meets the rubric',
    attestConfidentiality: 'confidentiality',
    attestNoDisciplinaryAction: 'no disciplinary action',
  };

  function signedTerms(a) {
    const names = Object.keys(ATTESTATION_LABELS)
      .filter(function (k) { return a[k] === true; })
      .map(function (k) { return ATTESTATION_LABELS[k]; });
    return names.length ? names.join(', ') : null;
  }

  /* `extra` is optional and defaults to {} so the profile's two-argument call is
     byte-identical. It carries the three things the DECISION screen needs that
     the credentials blob does not hold: years in practice, a board-cert
     fallback for legacy accounts whose blob predates the array, and where the
     CV disagrees with what was typed. */
  function credentialsAsTyped(h, c, a, extra) {
    extra = extra || {};
    const rows = [];
    const push = (k, v) => { if (v != null && v !== '') rows.push([k, String(v)]); };
    push('Full legal name', c.fullLegalName);
    push('Qualification', c.qualification || c.degree);
    push('Primary specialty', c.primarySpecialty);
    push('Licence number', c.licenseNumber);
    push('Licence state', c.licenseState);
    push('Registration number', c.registrationNumber);
    Object.keys(c.registryExtras || {}).forEach((k) => {
      push(k.replace(/([A-Z])/g, ' $1').toLowerCase(), c.registryExtras[k]);
    });
    const one = (row) => row && (row.institution || row.year)
      ? [row.institution, row.year].filter(Boolean).join(', ') : null;
    push('Residency', one(Array.isArray(c.residency) ? c.residency[0] : c.residency));
    push('Fellowship', one(Array.isArray(c.fellowship) ? c.fellowship[0] : c.fellowship));
    push('Practice status', c.practiceStatus);
    push('Clinical half-days / month', c.clinicalHalfDaysPerMonth);
    push('Languages', (c.languages || []).join(', '));
    push('Years in practice', extra.years_experience);
    const certs = (c.boardCertifications || []).filter((b) => b && b.board);
    certs.forEach((b, i) => {
      push('Board certification' + (i ? ' ' + (i + 1) : ''),
           [b.board, b.specialty, b.subspecialty].filter(Boolean).join(', '));
    });
    // A legacy account whose blob predates the array would otherwise render
    // blank on the one line the decision is nominally about.
    if (!certs.length) push('Board certification', extra.board_cert);
    // The signature. Collected since the first version of this form and shown
    // to nobody, including the person who signed it.
    push('Signed with', a.signedInitials);
    push('Terms signed', signedTerms(a));
    const conflicts = extra.cv_conflicts || [];
    if (!rows.length && !conflicts.length) return null;
    const pad = h('div', { class: 'asc-card-pad' }, kvBlock(h, '', rows));
    if (conflicts.length) {
      // Open and amber, not buried in the collapsed CV card. "The CV says a
      // different residency year than the form" is decision-changing, and it
      // is about the typed record, so it belongs beside it.
      pad.appendChild(h('div', { class: 'vq-section-label' },
        'The CV disagrees (' + conflicts.length + ')'));
      conflicts.forEach((cf) => {
        pad.appendChild(h('div', { class: 'asc-badge asc-badge-amber' },
          String(cf.field || 'Field') + ': CV says ' + String(cf.cv || 'nothing')
          + ', the form says ' + String(cf.stated || 'nothing')));
      });
    }
    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', { class: 'asc-card-title' }, 'Credentials as typed')),
      pad);
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
    reset() { selectedId = null; pendingId = null; activeTab = null; },
  };
})();
