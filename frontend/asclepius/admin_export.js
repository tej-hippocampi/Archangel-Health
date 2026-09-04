/* ═══════════════════════════════════════════════════════════
   Admin · Export — ONE TAB (Export & Approval PRD §2)

   Data → Export used to be three sub-tabs: "Export by case", a
   buyer CRM, and an export history that also hid a quick-export
   button, a version-cohort export and a contributor browser. Four
   ways to cut a bundle, in three places, none of which agreed about
   what was going to ship.

   Now: one page, top to bottom.

     Scope   (•) Case  ( ) Specialty  ( ) Version  ( ) Physician  ( ) All
     [ scope-specific picker ]

     Preview: 3 cases · 7 labeler submissions · 2 reviews · 1 specialty
              ⚠ 4 submissions on these cases are not approved and will
                not ship — [ Approve all 4 ]  [ show ]
     Estimated bundle 412 KB    [ Export bundle ]

     HISTORY …

   THE PART THAT MATTERS IS THE WARNING LINE. The old preview said
   "1 case" and stopped. It could not say that the case you actually
   wanted had four unapproved submissions on it and was never going to
   ship — so the export "shipped the wrong case", and the UI never told
   you why. Every count on this page, the excluded list included, comes
   from ONE server call (`_resolve_case_slice`), which is also the call
   the bundle builder makes. The preview and the bundle cannot disagree.

   Structure note: the shell is built ONCE and only the host that
   changed is re-rendered. A full re-render on every keystroke would
   blow away the case input the operator is typing into — the exact
   sort of small breakage that makes a console feel hostile.

   Loaded as its own file (§3.3); DOM built exclusively with ctx.h.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var SCOPES = [
    ['case', 'Case'],
    ['specialty', 'Specialty'],
    ['version', 'Version'],
    ['physician', 'Physician'],
    ['all', 'All'],
  ];

  // Filters persist across re-renders on purpose: an operator who approves four
  // submissions and comes back should land on the same slice they were cutting.
  var filters = {
    scope: 'case', case_ids: '', specialty: '', version: '',
    annotator_id_hashed: '',
  };
  // Licensing terms for the NEXT cut (audit U5). Kept out of `filters` because
  // they do not narrow the slice, they say what we are promising about it.
  var migration = null;      // /admin/export/migration-report, fetched once
  var options = null;        // /admin/export/case-options, fetched once
  var preview = null;        // null | 'loading' | slice | {error}
  var showExcluded = false;
  var busy = false;

  // Live DOM hosts, so only what changed is rebuilt.
  var ui = null;             // {ctx, scopeRow, picker, preview, actions, status, history}
  var previewSeq = 0;        // drops the response of a preview the user moved past

  // What each version IS, one line, beside the name. An operator picks a version
  // and ships it to a buyer; "V4" on its own does not say whether that is real
  // data, and the wrong slice is not recoverable once it has been sent. Served by
  // /admin/export/case-options; this is the fallback when that call fails.
  //
  // ENV (the agentic RL tier) is deliberately absent. It is not a version of the
  // single-turn portal — its rollouts live in env_runs, not records — and it
  // ships from the environments section instead.
  var VERSION_FALLBACK_DESCRIPTIONS = {
    V3: 'synthetic multimodal', V4: 'real static', V5: 'real longitudinal',
  };

  // The server accepts at most this many submissions per Approve-all call, and
  // renders at most this many rows in the excluded list. Both are bounds on the
  // same failure: a screen that happily posts ten thousand ids in one body works
  // on a demo database and falls over on the real one.
  var APPROVE_BATCH_MAX = 500;

  function fmtSize(bytes) {
    if (!bytes) return '—';
    var kb = bytes / 1024;
    if (kb < 1) return bytes + ' B';
    var mb = kb / 1024;
    if (mb < 0.1) return Math.round(kb) + ' KB';
    return (mb >= 10 ? Math.round(mb) : mb.toFixed(1)) + ' MB';
  }

  function plural(n, one, many) {
    return String(n) + ' ' + (n === 1 ? one : (many || one + 's'));
  }

  function errText(e) {
    // The exclusivity refusal (409) carries an object detail {message,
    // conflicts} naming the licence that blocked the cut. Show the message in
    // full: "export failed" would send the operator hunting for a bug instead
    // of to a contract.
    if (e && e.detail && typeof e.detail === 'object') return e.message || 'Request failed';
    return (e && (e.detail || e.message)) || 'no response';
  }

  function caseIdList() {
    return filters.case_ids.split(/[\s,]+/).filter(function (x) { return x; });
  }

  /* The query the preview and the bundle are both cut from. One builder, so a
     scope cannot mean one thing to the preview and another to the export. */
  function scopeParams() {
    var p = { scope: filters.scope };
    if (filters.scope === 'case') p.case_ids = caseIdList().join(',');
    if (filters.scope === 'specialty') p.specialty = filters.specialty;
    if (filters.scope === 'version') p.version = filters.version;
    if (filters.scope === 'physician') p.annotator_id_hashed = filters.annotator_id_hashed;
    return p;
  }

  function scopeIsChosen() {
    if (filters.scope === 'case') return caseIdList().length > 0;
    if (filters.scope === 'specialty') return !!filters.specialty;
    if (filters.scope === 'version') return !!filters.version;
    if (filters.scope === 'physician') return !!filters.annotator_id_hashed;
    return true;  // 'all' needs no selector
  }

  // ── The shell, built once ─────────────────────────────────────────────────
  function render(body, ctx) {
    var h = ctx.h;
    ctx.clear(body);

    ui = {
      ctx: ctx,
      migration: h('div', {}),
      scopeRow: h('div', { class: 'asc-scope-row' }),
      picker: h('div', {}),
      preview: h('div', { class: 'asc-export-preview', style: 'margin-top:14px' }),
      actions: h('div', { class: 'asc-export-actions' }),
      status: h('div', { style: 'width:100%' }),
      history: h('div', { class: 'asc-card', id: 'ascExportHistory' }),
    };

    body.appendChild(ui.migration);
    body.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Export'),
        h('div', { class: 'asc-card-sub' },
          'Pick a scope. The preview below is what will ship — and what will '
          + 'not, with the reason. One button builds the bundle; buyer '
          + 'deliveries happen from the buyer portal.'))),
      h('div', { class: 'asc-card-pad' },
        ui.scopeRow, ui.picker, ui.preview, ui.actions, ui.status)));

    ui.history.appendChild(ctx.loadingCard('Loading export history…'));
    body.appendChild(ui.history);
    loadHistory();

    // The contributor / credential browser. NOT a second export surface — the
    // Physician scope above is that — but the only place a Further Credential
    // Summary (the NDA dossier a buyer asks for when they want to verify who
    // labelled their data) can be generated. It kept a home when the old
    // Export-history sub-tab that housed it went away.
    if (ctx.contributorBrowser) {
      var contribCard = h('div', { class: 'asc-card' });
      body.appendChild(contribCard);
      ctx.contributorBrowser(contribCard);
    }

    drawScopes();
    drawPicker();
    drawPreview();
    drawActions();
    drawMigration();
    if (!options) loadOptions();
    loadMigration();
    refreshPreview();
  }

  /* ── The migration verdict, shown rather than looked up ───────────────────
   *
   * "Did the migration lose anything?" should not require reading a deploy log
   * or calling an endpoint by hand. The boot sweep already takes an id-set
   * snapshot around itself; this puts its answer on the screen an operator is
   * already on, once, in one line — and turns it into something impossible to
   * miss if it ever says no.
   *
   * Nothing renders when the sweep found nothing to recover and lost nothing:
   * a permanent green badge saying "all clear" is a badge people stop seeing,
   * and there is no news in it. */
  function loadMigration() {
    var ctx = ui.ctx;
    ctx.api('/admin/export/migration-report').then(function (res) {
      migration = res || null;
      drawMigration();
    }).catch(function () {
      // A missing report is not a failure to report: an older build, or a
      // process that has not finished booting. Say nothing rather than alarm.
      migration = null;
      drawMigration();
    });
  }

  function drawMigration() {
    var ctx = ui.ctx, h = ctx.h;
    ctx.clear(ui.migration);
    var m = migration;
    if (!m || !m.ran) return;
    var loss = m.no_data_loss || {};

    if (loss.checked && loss.ok === false) {
      // The one thing on this page that must never be missed.
      ui.migration.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-inline-error' },
            'THE NO-DATA-LOSS CHECK FAILED during the export migration. Rows '
            + 'that existed before it do not exist now. Do not export or delete '
            + 'anything — restore from a backup of the volume first.'),
          h('ul', {}, (loss.problems || []).map(function (p) {
            return h('li', {}, p);
          })))));
      return;
    }

    var recovered = m.cases_now_exportable || 0;
    var stranded = m.cases_stranded || 0;
    if (!stranded && !m.voided_left_untouched) return;   // no news

    var bits = [];
    if (recovered) {
      bits.push(plural(recovered, 'case') + ' that had already been approved or '
        + 'paid for could not ship, and now can.');
    } else if (stranded) {
      bits.push(plural(stranded, 'case')
        + ' were approved or paid but could not ship; none could be recovered '
        + 'automatically.');
    }
    if (m.voided_left_untouched) {
      bits.push(plural(m.voided_left_untouched, 'voided earning')
        + ' still ' + (m.voided_left_untouched === 1 ? 'has' : 'have')
        + ' live records — a void may have been a payment decision, not a '
        + 'quality one, so they were left for you rather than rejected.');
    }
    if (loss.checked && loss.ok) {
      bits.push('No rows were lost: every id set is identical across the '
        + 'migration.');
    }
    ui.migration.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-card-sub' }, 'Export migration'),
        h('div', {}, bits.join(' ')))));
  }

  function loadHistory() {
    var ctx = ui.ctx;
    if (ctx.exportHistory) ctx.exportHistory(ui.history);
    else {
      ctx.clear(ui.history);
      ui.history.appendChild(ctx.h('div', { class: 'asc-card-pad' },
        ctx.h('div', { class: 'asc-inline-error' },
          'Export history is unavailable in this build.')));
    }
  }

  // ── Scope chips ───────────────────────────────────────────────────────────
  function drawScopes() {
    var ctx = ui.ctx, h = ctx.h;
    ctx.clear(ui.scopeRow);
    SCOPES.forEach(function (s) {
      var btn = h('button', {
        type: 'button',
        class: 'asc-scope-btn' + (filters.scope === s[0] ? ' is-active' : ''),
        'aria-pressed': filters.scope === s[0] ? 'true' : 'false',
      }, s[1]);
      btn.addEventListener('click', function () {
        if (filters.scope === s[0]) return;
        filters.scope = s[0];
        preview = null; showExcluded = false;
        ctx.clear(ui.status);
        drawScopes(); drawPicker(); drawPreview(); drawActions();
        refreshPreview();
      });
      ui.scopeRow.appendChild(btn);
    });
  }

  /* One picker per scope. Never all of them at once: the scopes are exclusive
     server-side, so showing four inputs would invite an operator to fill in
     three that are then ignored. */
  function drawPicker() {
    var ctx = ui.ctx, h = ctx.h;
    ctx.clear(ui.picker);
    var wrap = h('div', { class: 'asc-field', style: 'margin-bottom:6px' });
    ui.picker.appendChild(wrap);

    if (filters.scope === 'all') {
      wrap.appendChild(h('div', { class: 'asc-card-sub' },
        'Every exportable record, across every specialty, version and physician.'));
      return;
    }

    if (filters.scope === 'case') {
      var listId = 'ascExportCaseOptions';
      var input = h('input', {
        class: 'asc-input', list: listId,
        placeholder: 'paste a case id — e.g. v4real-v4-neph-001 (comma-separate for several)',
        value: filters.case_ids,
      });
      var dl = h('datalist', { id: listId },
        ((options && options.cases) || []).slice(0, 500).map(function (c) {
          return h('option', { value: c.case_id },
            [c.specialty, c.portal_version,
             c.shippable ? (c.shippable + ' ready') : 'none approved']
              .filter(Boolean).join(' · '));
        }));
      var debounce = null;
      input.addEventListener('input', function () {
        filters.case_ids = input.value;
        if (debounce) clearTimeout(debounce);
        debounce = setTimeout(refreshPreview, 300);
      });
      wrap.appendChild(h('label', { class: 'asc-label' }, 'Case id'));
      wrap.appendChild(input);
      wrap.appendChild(dl);
      wrap.appendChild(h('div', { class: 'asc-label-hint' },
        'Copy one from the Money & Metrics ledger or Task Routing — every id '
        + 'there has a copy button.'));
      return;
    }

    if (filters.scope === 'specialty') {
      var sel = h('select', { class: 'asc-input' },
        h('option', { value: '' }, 'Choose a specialty…'),
        ((options && options.specialties) || []).map(function (sp) {
          return h('option', { value: sp }, sp);
        }));
      sel.value = filters.specialty;
      sel.addEventListener('change', function () {
        filters.specialty = sel.value; refreshPreview();
      });
      wrap.appendChild(h('label', { class: 'asc-label' }, 'Specialty'));
      wrap.appendChild(sel);
      return;
    }

    if (filters.scope === 'version') {
      var descs = (options && options.version_descriptions)
        || VERSION_FALLBACK_DESCRIPTIONS;
      var vsel = h('select', { class: 'asc-input' },
        h('option', { value: '' }, 'Choose a version…'),
        ((options && options.versions) || ['V3', 'V4', 'V5']).map(function (v) {
          return h('option', { value: v }, descs[v] ? (v + ' · ' + descs[v]) : v);
        }));
      vsel.value = filters.version;
      vsel.addEventListener('change', function () {
        filters.version = vsel.value; refreshPreview();
      });
      wrap.appendChild(h('label', { class: 'asc-label' }, 'Product version'));
      wrap.appendChild(vsel);
      wrap.appendChild(h('div', { class: 'asc-label-hint' },
        'V5 is the real longitudinal chart walk: selecting one of its cases '
        + 'exports the WHOLE trajectory, because a walk delivered one point at '
        + 'a time cannot be reassembled. The agentic RL environments are a '
        + 'separate tier (ENV) and ship from the environments section, not here.'));
      return;
    }

    // physician
    var psel = h('select', { class: 'asc-input' },
      h('option', { value: '' }, 'Choose a physician…'),
      ((options && options.physicians) || []).map(function (p) {
        return h('option', { value: p.annotator_id_hashed },
          [p.name, p.specialty || 'no specialty', plural(p.cases, 'case')].join(' · '));
      }));
    psel.value = filters.annotator_id_hashed;
    psel.addEventListener('change', function () {
      filters.annotator_id_hashed = psel.value; refreshPreview();
    });
    wrap.appendChild(h('label', { class: 'asc-label' }, 'Physician'));
    wrap.appendChild(psel);
    // The promise, stated where the operator makes the choice rather than
    // buried in a datasheet: you pick a person, the bundle carries a hash.
    wrap.appendChild(h('div', { class: 'asc-label-hint' },
      'You pick a name; the bundle carries only annotator_id_hashed. The '
      + 'physician’s name never enters records.jsonl, the datasheet, or the '
      + 'filename.'));
  }

  // ── Preview ───────────────────────────────────────────────────────────────
  function drawPreview() {
    var ctx = ui.ctx, h = ctx.h;
    ctx.clear(ui.preview);

    if (!scopeIsChosen()) {
      ui.preview.appendChild(h('div', { class: 'asc-dim' },
        'Choose a ' + filters.scope + ' to size the bundle.'));
      return;
    }
    if (preview === 'loading') {
      ui.preview.appendChild(h('div', { class: 'asc-dim' }, 'Sizing this slice…'));
      return;
    }
    if (!preview) return;
    if (preview.error) {
      ui.preview.appendChild(h('div', { class: 'asc-inline-error' }, preview.error));
      return;
    }

    var p = preview;
    ui.preview.appendChild(h('div', { class: 'asc-export-headline' },
      [plural(p.cases || 0, 'case'),
       plural(p.labeler_submissions || 0, 'labeler submission'),
       plural(p.reviews || 0, 'review'),
       plural(p.specialty_count || 0, 'specialty', 'specialties')].join(' · ')));

    var ex = p.excluded || {};
    if (ex.unapproved_count) ui.preview.appendChild(excludedBlock(ex));

    // Two exclusions that were always computed and never shown.
    var quiet = [];
    if (ex.dropped) {
      quiet.push(plural(ex.dropped, 'record')
        + ' cannot be mapped to the buyer profile and will not ship.');
    }
    if (ex.mock) {
      quiet.push(plural(ex.mock, 'mock-annotator record')
        + ' excluded (sandbox data never ships).');
    }
    if (quiet.length) {
      ui.preview.appendChild(h('div', { class: 'asc-label-hint', style: 'margin-top:8px' },
        quiet.join(' ')));
    }
    if (p.note) {
      ui.preview.appendChild(
        h('div', { class: 'asc-inline-warn', style: 'margin-top:8px' }, p.note));
    }
    ui.preview.appendChild(h('div', { class: 'asc-dim', style: 'margin-top:8px' },
      'Estimated bundle ' + fmtSize(p.estimated_bytes)));
  }

  /* THE SENTENCE THAT IS THE WHOLE FIX (§2.2).

     "1 case ships. 1 submission on v4real-v4-neph-001 is awaiting approval and
     will not ship." — said before the export, next to the button that fixes it. */
  function excludedBlock(ex) {
    var ctx = ui.ctx, h = ctx.h;
    var n = ex.unapproved_count;
    var approvable = ex.approvable_count || 0;
    var block = h('div', { class: 'asc-export-excluded is-warn' });
    block.appendChild(h('div', {},
      '⚠ ' + plural(n, 'submission') + ' on these cases '
      + (n === 1 ? 'is' : 'are') + ' not approved and will not ship.'));
    if (ex.truncated) {
      // The COUNT above is exact; the LIST below is capped. Say so, rather than
      // letting an operator conclude they have seen all of them.
      block.appendChild(h('div', { class: 'asc-label-hint' },
        'Showing the first ' + (ex.listed || 0) + '. Approve in batches, or '
        + 'narrow the scope.'));
    }

    var row = h('div', { class: 'asc-export-excluded-actions' });
    if (approvable) {
      var batch = Math.min(approvable, APPROVE_BATCH_MAX);
      var ab = h('button', { class: 'asc-btn asc-btn-primary asc-btn-sm', type: 'button' },
        busy ? 'Approving…' : ('Approve all ' + batch));
      if (busy) ab.setAttribute('disabled', '');
      ab.addEventListener('click', function () { approveAll(ex); });
      row.appendChild(ab);
    }
    var toggle = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button' },
      showExcluded ? 'hide' : 'show');
    toggle.addEventListener('click', function () {
      showExcluded = !showExcluded; drawPreview();
    });
    row.appendChild(toggle);
    if (approvable < n) {
      row.appendChild(h('span', { class: 'asc-label-hint' },
        (n - approvable) + ' of them cannot be approved from here — open the '
        + 'list for the reason.'));
    }
    block.appendChild(row);

    if (showExcluded) {
      var table = h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', { class: 'asc-cell-id' }, 'Case'),
          h('th', { class: 'asc-cell-id' }, 'Submission'),
          h('th', {}, 'Status'), h('th', {}, 'Why'))),
        h('tbody', {}, (ex.unapproved || []).map(function (r) {
          return h('tr', r.approvable ? {} : { class: 'asc-dim' },
            h('td', { class: 'asc-cell-id' }, ctx.copyableId(r.case_id)),
            h('td', { class: 'asc-cell-id' }, ctx.copyableId(r.submission_id)),
            h('td', {}, statusWord(r)),
            h('td', {}, r.reason || ''));
        })));
      block.appendChild(h('div', { class: 'asc-table-wrap asc-export-excluded-list' }, table));
    }
    return block;
  }

  function statusWord(r) {
    var words = { accrued: 'Awaiting approval', approved: 'Approved',
                  paid: 'Paid', void: 'Not approved' };
    var ledger = words[r.ledger_status]
      || (r.ledger_status ? r.ledger_status : 'No ledger row');
    return ledger + ' · ' + (r.status || '—');
  }

  // ── Actions ───────────────────────────────────────────────────────────────
  function drawActions() {
    var ctx = ui.ctx, h = ctx.h;
    ctx.clear(ui.actions);
    var ready = !!(preview && preview !== 'loading' && !preview.error && preview.exportable);
    var blocked = !ready || busy;

    var exportBtn = h('button', { class: 'asc-btn asc-btn-primary', type: 'button' },
      busy ? 'Building bundle…' : 'Export bundle');
    if (blocked) exportBtn.setAttribute('disabled', '');
    exportBtn.addEventListener('click', function () { runExport(null); });
    ui.actions.appendChild(exportBtn);

  }

  // ── Data ──────────────────────────────────────────────────────────────────
  function loadOptions() {
    var ctx = ui.ctx;
    ctx.api('/admin/export/case-options').then(function (res) {
      options = res || {};
      drawPicker();
    }).catch(function () {
      // The pickers degrade to "type an id" rather than blocking the page: a
      // broken options call must not make the export tab unusable.
      options = { specialties: [], versions: ['V3', 'V4', 'V5'], cases: [], physicians: [] };
      drawPicker();
    });
  }

  function refreshPreview() {
    if (!ui) return;
    var ctx = ui.ctx;
    if (!scopeIsChosen()) {
      preview = null; drawPreview(); drawActions(); return;
    }
    var seq = ++previewSeq;
    preview = 'loading';
    drawPreview(); drawActions();
    var p = scopeParams();
    var qs = new URLSearchParams();
    Object.keys(p).forEach(function (k) { if (p[k]) qs.set(k, p[k]); });
    ctx.api('/admin/export/case-preview?' + qs.toString()).then(function (res) {
      if (seq !== previewSeq) return;   // the operator moved on; this answer is stale
      preview = res;
      drawPreview(); drawActions();
    }).catch(function (e) {
      if (seq !== previewSeq) return;
      preview = { error: 'Could not size this slice: ' + errText(e) };
      drawPreview(); drawActions();
    });
  }

  function approveAll(ex) {
    if (busy) return;
    var ctx = ui.ctx;
    var ids = (ex.unapproved || [])
      .filter(function (r) { return r.approvable; })
      .map(function (r) { return r.submission_id; })
      .slice(0, APPROVE_BATCH_MAX);
    if (!ids.length) return;
    busy = true;
    drawPreview(); drawActions();
    ctx.api('/admin/export/approve', { method: 'POST', body: { submission_ids: ids } })
      .then(function (res) {
        busy = false;
        var n = (res && res.approved) || 0;
        ctx.toast('Approved ' + plural(n, 'submission') + '.', n ? 'success' : 'error');
        if (n < ids.length) {
          ctx.clear(ui.status);
          ui.status.appendChild(ctx.h('div', { class: 'asc-inline-warn' },
            (ids.length - n) + ' of ' + ids.length + ' could not be approved. '
            + 'Open the excluded list — the reason is on each row.'));
        }
        // RE-PREVIEW, always. The number on this page after an approval is the
        // server's, never this file's arithmetic.
        refreshPreview();
      })
      .catch(function (e) {
        busy = false;
        ctx.toast('Could not approve: ' + errText(e), 'error');
        drawPreview(); drawActions();
      });
  }

  function runExport(buyerEmail) {
    if (busy) return;
    var ctx = ui.ctx, h = ctx.h;
    busy = true;
    ctx.clear(ui.status);
    drawActions();
    var p = scopeParams();
    ctx.api('/admin/export/case-bundle', { method: 'POST', body: {
      scope: p.scope,
      case_ids: p.case_ids ? p.case_ids.split(',') : null,
      specialty: p.specialty || null,
      version: p.version || null,
      annotator_id_hashed: p.annotator_id_hashed || null,
      buyer_email: buyerEmail || null,
    } })
      .then(function (res) {
        busy = false;
        drawActions();
        var n = res.record_count || 0;
        ctx.toast('Export built — ' + plural(n, 'record') + '.', 'success');
        ctx.clear(ui.status);
        ui.status.appendChild(h('div', { class: 'asc-inline-ok' },
          'Ready: ' + plural(n, 'record')
          + (res.case_count != null ? ' across ' + plural(res.case_count, 'case') : '')
          + '.'));
        var label = res.filename || (res.export_id + '.zip');
        var dl = h('button', {
          class: 'asc-btn asc-btn-subtle asc-btn-sm', style: 'margin:10px 6px 0 0',
          type: 'button',
        }, '⬇ Download ' + label);
        dl.addEventListener('click', function () {
          ctx.downloadBlob('/exports/' + res.export_id + '/download', label);
        });
        ui.status.appendChild(dl);
        if (res.delivery) {
          ui.status.appendChild(h('div', { class: 'asc-inline-ok' },
            'Delivered to ' + res.delivery.buyer_email
            + (res.delivery.email_sent ? ' — credentials emailed.'
                                       : ' — the notification email did NOT send.')));
        }
        loadHistory();
        // The records just shipped are now `exported`, so the slice changed.
        refreshPreview();
      })
      .catch(function (e) {
        busy = false;
        drawActions();
        ctx.clear(ui.status);
        ui.status.appendChild(h('div', { class: 'asc-inline-error' }, errText(e)));
      });
  }

  window.AdminExportSection = {
    render: render,
    reset: function () { /* filters persist intentionally */ },
  };
})();
