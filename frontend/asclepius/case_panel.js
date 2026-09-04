/* ═══════════════════════════════════════════════════════════════════════════
   The clinical chart — ONE component, rendered identically to the labeler and
   to the reviewer (PRD-1 §2.2).

   window.AsclepiusCasePanel { render, SPECIALTY_UI, MODALITY_LABEL }

   This file exists because of a data-integrity bug, not a styling one. The
   labeler read a tabbed chart — Patient · Labs (trend) · Studies · EHR · Meds ·
   Vitals, with lab trajectories plotted across collection offsets — and the
   reviewer adjudicating the SAME case read `JSON.stringify(value, null, 1)`
   inside a collapsed <details>. Two physicians disagreeing because they were
   shown differently-rendered versions of one chart is not a cosmetic defect:
   it corrupts the adjudication, and every acceptance statistic computed from it.

   So the panel is extracted rather than copied. A fork would drift, and the
   first drift would be invisible — the reviewer would simply be reading a
   slightly older chart.

   CONTRACT

     render(ctx, opts) -> Element | null

     ctx  { h, clear, fetchAssetBlobUrl }
          The HOST's hyperscript and asset loader, never this module's own.
          `h` is the reason this file takes a ctx at all: two hyperscripts on
          one page is how `onClick` silently registers a listener for the event
          type "Click" on one surface and "click" on the other.
          `fetchAssetBlobUrl(assetId) -> Promise<objectURL>` is bearer-
          authenticated on both surfaces and belongs to whoever holds the token.

     opts { case, specialty, specialties, tabKey }
          `case`        the PUBLIC structured case (answer key already stripped
                        server-side on both surfaces).
          `specialty`   drives the tab strip via SPECIALTY_UI.
          `specialties` the cached /specialties listing, for the accent chip
                        only. Absent is fine — the chip falls back to green.
          `tabKey`      a stable id (the task id) the active tab is remembered
                        against, so a re-render inside one case keeps the
                        physician where they were and moving to a new case
                        starts at Patient.

   Every helper is rebuilt per call, closed over that call's ctx. The async
   image loader therefore cannot end up calling the OTHER surface's hyperscript
   after a second render — the one failure mode a module-level `var H = ctx.h`
   would have introduced, and the sort that surfaces months later as a blank
   study tab nobody can reproduce.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // Per-specialty case-panel layout: ONE code path, config not a render fork.
  // Each entry is an ordered list of tab specs; a tab appears only when its data
  // exists. ``study`` groups the case's ``studies`` by modality; ``strip``
  // renders a scannable ECG findings block; ``timeline`` renders imaging as a
  // baseline→on-treatment sequence; ``ngs`` renders molecular variants as a VAF
  // table.
  var SPECIALTY_UI = {
    nephrology: [
      { kind: 'overview' },
      { kind: 'labs', label: 'Labs (trend)' },
      { kind: 'study', key: 'studies', label: 'Studies', modalities: ['pathology', 'ct', 'mri', 'pet', 'echo', 'other'] },
      { kind: 'notes' }, { kind: 'meds' }, { kind: 'vitals' },
    ],
    cardiology: [
      { kind: 'overview' },
      { kind: 'study', key: 'ecg', label: 'ECG', modalities: ['ecg'], strip: true },
      { kind: 'study', key: 'echoimg', label: 'Echo/Imaging', modalities: ['echo', 'cath', 'ct', 'mri', 'pet'] },
      { kind: 'labs', label: 'Labs' },
      { kind: 'notes' }, { kind: 'meds' }, { kind: 'vitals' },
    ],
    oncology: [
      { kind: 'overview' },
      { kind: 'study', key: 'pathology', label: 'Pathology', modalities: ['pathology'] },
      { kind: 'study', key: 'imaging', label: 'Imaging', modalities: ['ct', 'mri', 'pet'], timeline: true },
      { kind: 'study', key: 'molecular', label: 'Molecular', modalities: ['molecular'], ngs: true },
      { kind: 'labs', label: 'Labs' },
      { kind: 'notes' }, { kind: 'meds' }, { kind: 'vitals' },
    ],
  };
  var DEFAULT_SPECIALTY_UI = SPECIALTY_UI.nephrology;

  // Modality → short chip label for the modality chips in a study tab.
  var MODALITY_LABEL = {
    ecg: 'ECG', echo: 'Echo', cath: 'Cath', ct: 'CT', mri: 'MRI', pet: 'PET',
    pathology: 'Pathology', molecular: 'Molecular', other: 'Study',
  };

  // Active tab per tabKey. Module-level ON PURPOSE — it is the one piece of
  // state that must survive a re-render, and it is keyed by case so it can
  // never carry across to a different chart.
  var TAB_STATE = {};

  function build(ctx) {
    var h = ctx.h;
    var clear = ctx.clear;

    // Lab out-of-range flag → severity class for cell highlighting.
    function labFlagClass(flag) {
      var f = String(flag || '').toUpperCase();
      if (f === 'LL' || f === 'HH') return 'asc-lab-crit';
      if (f === 'L' || f === 'H') return 'asc-lab-warn';
      return '';
    }
    function fmtOffset(off) {
      var n = parseInt(off, 10) || 0;
      if (n === 0) return 'day 0';
      return 'day ' + (n > 0 ? '+' : '') + n;
    }
    function refRange(r) {
      var lo = (r.ref_low === null || r.ref_low === undefined) ? '' : r.ref_low;
      var hi = (r.ref_high === null || r.ref_high === undefined) ? '' : r.ref_high;
      if (lo === '' && hi === '') return 'n/a';
      return lo + '-' + hi;
    }

    // A trend table across all lab panels: one row per analyte, one column per
    // distinct collection offset (oldest → newest), so a clinician reads the
    // trajectory (e.g. a falling sodium, or a GGT dropping while bilirubin
    // rises) at a glance. Cells are flag-highlighted. THIS is the function the
    // reviewer did not have; adjudicating a trajectory from a JSON dump is what
    // made review slow.
    function renderLabsTrend(panels) {
      var ps = (panels || []).slice().sort(function (a, b) {
        return (parseInt(a.collected_offset_days, 10) || 0) - (parseInt(b.collected_offset_days, 10) || 0);
      });
      if (!ps.length) return null;
      var offsets = [];
      ps.forEach(function (p) {
        var o = parseInt(p.collected_offset_days, 10) || 0;
        if (offsets.indexOf(o) === -1) offsets.push(o);
      });
      // analyte order = first-seen; carry unit + ref range + which panel.
      var order = [];
      var meta = {};
      var cell = {}; // analyte -> offset -> {value, flag}
      ps.forEach(function (p) {
        var off = parseInt(p.collected_offset_days, 10) || 0;
        (p.results || []).forEach(function (r) {
          var a = String(r.analyte || '');
          if (!a) return;
          if (order.indexOf(a) === -1) {
            order.push(a);
            meta[a] = { unit: r.unit || '', ref: refRange(r), panel: p.panel || '' };
          }
          cell[a] = cell[a] || {};
          cell[a][off] = { value: r.value, flag: r.flag };
        });
      });
      var head = h('tr', {},
        h('th', {}, 'Analyte'),
        h('th', {}, 'Ref'),
        offsets.map(function (o) { return h('th', { class: 'asc-lab-num' }, fmtOffset(o)); }));
      var rows = order.map(function (a) {
        return h('tr', {},
          h('td', { class: 'asc-lab-analyte' }, a + (meta[a].unit ? ' (' + meta[a].unit + ')' : '')),
          h('td', { class: 'asc-lab-ref' }, meta[a].ref),
          offsets.map(function (o) {
            var c = (cell[a] || {})[o];
            if (!c || c.value == null || c.value === '') return h('td', { class: 'asc-lab-num' }, '·');
            return h('td', { class: 'asc-lab-num ' + labFlagClass(c.flag) },
              String(c.value) + (c.flag ? ' ' + String(c.flag).toUpperCase() : ''));
          }));
      });
      return h('div', { class: 'asc-lab-scroll' },
        h('table', { class: 'asc-lab-table' }, h('thead', {}, head), h('tbody', {}, rows)));
    }

    // A compact measurements/variants table (reuses the lab flag classes) for a
    // study's numeric findings: EF %, valve gradient, SUVmax, molecular VAF.
    function renderMeasurements(measurements, valueHead) {
      var ms = (measurements || []).filter(function (m) { return m && m.analyte; });
      if (!ms.length) return null;
      var head = h('tr', {}, h('th', {}, valueHead || 'Measure'),
        h('th', { class: 'asc-lab-num' }, 'Value'), h('th', {}, 'Ref'));
      var rows = ms.map(function (m) {
        return h('tr', {},
          h('td', { class: 'asc-lab-analyte' }, String(m.analyte) + (m.unit ? ' (' + m.unit + ')' : '')),
          h('td', { class: 'asc-lab-num ' + labFlagClass(m.flag) },
            String(m.value == null ? '·' : m.value) + (m.flag ? ' ' + String(m.flag).toUpperCase() : '')),
          h('td', { class: 'asc-lab-ref' }, refRange(m)));
      });
      return h('div', { class: 'asc-lab-scroll' },
        h('table', { class: 'asc-lab-table' }, h('thead', {}, head), h('tbody', {}, rows)));
    }

    // The image viewer: the image renders ABOVE its structured findings, with
    // zoom (scroll / ＋－), pan (drag), fit/reset and a full-screen toggle,
    // keyboard-operable (＋/－/0, arrows, Esc). Design-tokens only, with a real
    // load-failure state — never a broken-image icon.
    function renderStudyImage(study) {
      if (!study || !study.asset || !study.asset.asset_id) return null;
      if (typeof ctx.fetchAssetBlobUrl !== 'function') return null;
      var asset = study.asset;
      var pageCount = parseInt(asset.page_count, 10) || 1;
      var shownPage = parseInt(asset.page, 10) || 1;
      var view = { scale: 1, x: 0, y: 0 };
      var wrap = h('div', { class: 'asc-img-viewer' });
      var stage = h('div', { class: 'asc-img-stage', tabindex: '0', role: 'img',
        'aria-label': (study.label || study.modality || 'clinical') + ' image' });
      var img = h('img', { class: 'asc-img',
        alt: (study.label || study.modality || 'clinical image'), draggable: 'false' });
      var skeleton = h('div', { class: 'asc-img-skeleton' }, h('div', { class: 'loading-spinner' }));
      stage.appendChild(skeleton);
      // Track the blob URL so it is revoked (no leaked decoded-image memory over
      // a long grading session). Cleaned up on load, on error/reload, on teardown.
      var objUrl = null;
      function revoke() {
        if (objUrl) { try { URL.revokeObjectURL(objUrl); } catch (e) { /* ignore */ } objUrl = null; }
      }
      function apply() {
        img.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.scale + ')';
      }
      function zoom(delta) {
        var next = Math.min(8, Math.max(1, view.scale + delta));
        view.scale = next;
        if (next === 1) { view.x = 0; view.y = 0; }
        apply();
      }
      function reset() { view.scale = 1; view.x = 0; view.y = 0; apply(); }

      // Zoom on scroll; pan on drag. The pan listeners live on `window` ONLY for
      // the duration of a drag (added on mousedown, removed on mouseup) so they
      // never accumulate across cases/tabs; the stage-scoped listeners are GC'd
      // with the node.
      stage.addEventListener('wheel', function (e) {
        e.preventDefault(); zoom(e.deltaY < 0 ? 0.25 : -0.25);
      }, { passive: false });
      var drag = null;
      function onMove(e) {
        if (!drag) return;
        view.x = e.clientX - drag.x; view.y = e.clientY - drag.y; apply();
      }
      function onUp() {
        drag = null; stage.classList.remove('asc-img-grabbing');
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
      }
      stage.addEventListener('mousedown', function (e) {
        drag = { x: e.clientX - view.x, y: e.clientY - view.y };
        stage.classList.add('asc-img-grabbing');
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
      });
      // Keyboard: +/- zoom, 0 reset, arrows pan, Esc exit full-screen. The
      // handler STOPS PROPAGATION so the reviewer's global shortcuts (A/B/N,
      // arrows) cannot fire while a physician is panning an image.
      stage.addEventListener('keydown', function (e) {
        var step = 40;
        if (e.key === '+' || e.key === '=') { zoom(0.25); }
        else if (e.key === '-' || e.key === '_') { zoom(-0.25); }
        else if (e.key === '0') { reset(); }
        else if (e.key === 'ArrowLeft') { view.x += step; apply(); }
        else if (e.key === 'ArrowRight') { view.x -= step; apply(); }
        else if (e.key === 'ArrowUp') { view.y += step; apply(); }
        else if (e.key === 'ArrowDown') { view.y -= step; apply(); }
        else if (e.key === 'Escape') { if (wrap.classList.contains('asc-img-full')) toggleFull(); return; }
        else { return; }
        e.preventDefault();
        if (typeof e.stopPropagation === 'function') e.stopPropagation();
      });
      function toggleFull() { wrap.classList.toggle('asc-img-full'); reset(); stage.focus(); }

      var toolbar = h('div', { class: 'asc-img-toolbar' },
        h('button', { class: 'asc-img-btn', type: 'button', title: 'Zoom out (−)',
          onClick: function () { zoom(-0.25); } }, '−'),
        h('button', { class: 'asc-img-btn', type: 'button', title: 'Zoom in (+)',
          onClick: function () { zoom(0.25); } }, '＋'),
        h('button', { class: 'asc-img-btn', type: 'button', title: 'Reset view (0)',
          onClick: reset }, 'Reset'),
        h('button', { class: 'asc-img-btn', type: 'button', title: 'Full screen',
          onClick: toggleFull }, '⤢'));
      // Multi-page PDF: the ingested asset is a SINGLE rendered page (page N of
      // the source), so we show an honest static indicator, never non-functional
      // nav buttons that would let a clinician believe they are viewing a page
      // that isn't loaded.
      if (pageCount > 1) {
        toolbar.appendChild(h('span', { class: 'asc-img-page' }, 'Page ' + shownPage + ' of ' + pageCount));
      }

      stage.appendChild(img);
      wrap.appendChild(toolbar);
      wrap.appendChild(stage);

      // Load the bytes. On failure show a real error state with a reload; a
      // physician must NEVER grade a case whose image did not render.
      ctx.fetchAssetBlobUrl(asset.asset_id).then(function (url) {
        objUrl = url;
        // Revoke once decoded. The browser keeps the rendered image; frees the blob.
        img.onload = function () {
          if (skeleton.parentNode) skeleton.parentNode.removeChild(skeleton);
          apply(); revoke();
        };
        img.onerror = function () { revoke(); showError(); };
        img.src = url;
      }).catch(function () { showError(); });
      function showError() {
        revoke();
        onUp();  // ensure any in-flight drag listeners are removed
        clear(stage);
        stage.appendChild(h('div', { class: 'asc-img-error' },
          h('div', {}, 'Could not load the image.'),
          h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
            onClick: function () {
              var p = wrap.parentNode;
              if (p) { var fresh = renderStudyImage(study); if (fresh) p.replaceChild(fresh, wrap); }
            } }, 'Reload')));
      }
      return wrap;
    }

    // One study inside its tab: the image first (if any), then the modality
    // chip, the structured findings report (as a scannable "rhythm strip" block
    // for an ECG), the numeric measurements table, and the impression.
    function renderStudyCard(study, opts) {
      opts = opts || {};
      var modality = String(study.modality || 'study').toLowerCase();
      var chipLabel = MODALITY_LABEL[modality] || modality.toUpperCase();
      var findings = (study.findings || '').trim();
      var isNgs = opts.ngs || modality === 'molecular';
      return h('div', { class: 'asc-study' },
        h('div', { class: 'asc-study-head' },
          h('span', { class: 'asc-chip asc-chip-modality' },
            h('span', { class: 'asc-chip-dot', 'aria-hidden': 'true' }),
            h('span', {}, chipLabel)),
          study.label ? h('span', { class: 'asc-study-label' }, study.label) : null),
        renderStudyImage(study),
        findings
          ? (opts.strip
              ? h('div', { class: 'asc-ecg-strip' }, h('div', { class: 'asc-ecg-strip-text' }, findings))
              : h('div', { class: 'asc-study-findings' }, findings))
          : null,
        renderMeasurements(study.measurements, isNgs ? 'Variant' : 'Measure'),
        study.impression
          ? h('div', { class: 'asc-study-impression' }, h('strong', {}, 'Impression: '), study.impression)
          : null);
    }

    // Build a study tab body from the case's ``studies`` filtered to
    // ``modalities``. ``timeline`` lays them out as a baseline→on-treatment
    // sequence (the temporal judgment pseudoprogression turns on).
    function renderStudyTab(studies, spec) {
      var mods = spec.modalities;
      var items = (studies || []).filter(function (s) {
        return s && (!mods || mods.indexOf(String(s.modality || '').toLowerCase()) !== -1);
      });
      if (!items.length) return null;
      if (spec.timeline && items.length) {
        return h('div', { class: 'asc-case-body' },
          h('div', { class: 'asc-timeline' }, items.map(function (s) {
            return h('div', { class: 'asc-timeline-step' },
              h('div', { class: 'asc-timeline-marker', 'aria-hidden': 'true' }),
              renderStudyCard(s, spec));
          })));
      }
      return h('div', { class: 'asc-case-body' }, items.map(function (s) {
        return renderStudyCard(s, spec);
      }));
    }

    function panel(opts) {
      opts = opts || {};
      var c = opts.case;
      if (!c || typeof c !== 'object') return null;
      var spec = String(opts.specialty || 'nephrology').toLowerCase();
      var ui = SPECIALTY_UI[spec] || DEFAULT_SPECIALTY_UI;
      var demo = c.demographics || {};
      var who = [demo.sex, demo.age_band ? ('age ' + demo.age_band) : null]
        .filter(Boolean).join(', ');
      var meds = c.medications || [];
      var vitals = c.vitals || {};
      var vkeys = Object.keys(vitals).filter(function (k) {
        return vitals[k] !== null && vitals[k] !== undefined && vitals[k] !== '';
      });
      var studies = c.studies || [];

      var tabs = [];
      ui.forEach(function (t) {
        if (t.kind === 'overview') {
          tabs.push({ key: 'overview', label: 'Patient', body: h('div', { class: 'asc-case-body' },
            h('div', { class: 'asc-case-patient' }, who ? ('Patient: ' + who) : 'Patient (de-identified)'),
            (c.problem_list && c.problem_list.length)
              ? h('div', { class: 'asc-case-sub' }, 'Active problems: ' + c.problem_list.map(function (p) {
                  return p.condition + (p.since ? ' (since ' + p.since + ')' : '');
                }).join('; '))
              : null,
            h('div', { class: 'asc-case-note-meta' }, 'De-identified · relative dates · structured studies')) });
        } else if (t.kind === 'labs' && c.lab_panels && c.lab_panels.length) {
          tabs.push({ key: 'labs', label: t.label || 'Labs',
            body: h('div', { class: 'asc-case-body' }, renderLabsTrend(c.lab_panels)) });
        } else if (t.kind === 'study') {
          var body = renderStudyTab(studies, t);
          if (body) tabs.push({ key: t.key, label: t.label, body: body });
        } else if (t.kind === 'notes' && c.notes && c.notes.length) {
          tabs.push({ key: 'notes', label: 'EHR' + (c.notes.length > 1 ? ' (' + c.notes.length + ')' : ''),
            body: h('div', { class: 'asc-case-body' }, c.notes.map(function (n) {
              return h('div', { class: 'asc-case-note' },
                h('div', { class: 'asc-case-note-meta' },
                  '[' + (n.note_type || 'Note') + ' · ' + (n.author_role || 'clinician') + ']'),
                h('div', { class: 'asc-case-note-text' }, (n.text || '').trim()));
            })) });
        } else if (t.kind === 'meds' && meds.length) {
          tabs.push({ key: 'meds', label: 'Meds', body: h('div', { class: 'asc-case-body' },
            h('ul', { class: 'asc-case-list' }, meds.map(function (m) {
              return h('li', {}, [m.drug, m.dose, m.route, m.freq].filter(Boolean).join(' '));
            }))) });
        } else if (t.kind === 'vitals' && vkeys.length) {
          tabs.push({ key: 'vitals', label: 'Vitals', body: h('div', { class: 'asc-case-body' },
            h('div', { class: 'asc-case-vitals' }, vkeys.map(function (k) {
              return h('span', { class: 'asc-vital' },
                h('span', { class: 'asc-vital-k' }, k), ' ',
                h('span', { class: 'asc-vital-v' }, String(vitals[k])));
            }))) });
        }
      });
      if (!tabs.length) return null;

      var tid = opts.tabKey || '';
      var active = TAB_STATE[tid];
      if (!active || !tabs.some(function (t) { return t.key === active; })) {
        active = tabs[0].key;
        TAB_STATE[tid] = active;
      }

      var bodyHost = h('div', { class: 'asc-case-host' });
      var tabRow = h('div', { class: 'asc-case-tabs', role: 'tablist', dataset: { tour: 'case-tabs' } });
      function paint() {
        clear(bodyHost);
        var hit = tabs.filter(function (t) { return t.key === TAB_STATE[tid]; })[0] || tabs[0];
        bodyHost.appendChild(hit.body);
        Array.prototype.forEach.call(tabRow.children, function (btn) {
          btn.classList.toggle('asc-case-tab-active', btn.getAttribute('data-tab') === TAB_STATE[tid]);
        });
      }
      tabs.forEach(function (t) {
        tabRow.appendChild(h('button', { class: 'asc-case-tab', type: 'button', role: 'tab',
          'data-tab': t.key,
          onClick: function () { TAB_STATE[tid] = t.key; paint(); } }, t.label));
      });

      // Specialty chip (deterministic colour: nephrology green, cardiology
      // orange, oncology pink). The accent comes from the cached /specialties
      // listing so a new specialty needs no frontend change.
      var specMeta = ((opts.specialties || []).filter(function (s) {
        return s.specialty === spec;
      })[0]) || {};
      var specChip = h('span', {
        class: 'asc-chip asc-chip-specialty asc-chip-' + (specMeta.accent || 'green') },
        h('span', { class: 'asc-chip-dot', 'aria-hidden': 'true' }),
        h('span', {}, spec.charAt(0).toUpperCase() + spec.slice(1)));
      var el = h('div', { class: 'asc-card asc-case-card' },
        h('div', { class: 'asc-case-head' },
          specChip,
          h('span', { class: 'asc-badge asc-badge-accent' }, 'Multimodal case'),
          h('span', { class: 'asc-case-source' },
            (c.case_source === 'real_deid' ? 'Real (de-identified)' : 'Synthetic'))),
        tabRow, bodyHost);
      paint();
      return el;
    }

    return { panel: panel };
  }

  window.AsclepiusCasePanel = {
    SPECIALTY_UI: SPECIALTY_UI,
    MODALITY_LABEL: MODALITY_LABEL,
    render: function (ctx, opts) {
      if (!ctx || typeof ctx.h !== 'function' || typeof ctx.clear !== 'function') return null;
      return build(ctx).panel(opts);
    },
  };
})();
