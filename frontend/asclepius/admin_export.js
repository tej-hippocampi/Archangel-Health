/* ═══════════════════════════════════════════════════════════
   Admin · Export section (PRD C Phase 4)

   What can we sell, and how do we cut it? Export BY CASE, not by
   physician. Three combinable filters — Case ID (exact) · Specialty ·
   Version — and a preview of exactly what the bundle will contain
   BEFORE the export is built:

     142 cases · 168 labeler submissions · 118 reviews · 3 specialties
     Estimated bundle 24 MB                        [ Export bundle ]

   A preview before an irreversible action is worth more than a
   progress bar after it: exporting the wrong slice to a buyer is not
   recoverable.

   Loaded as its own file (§3.3); DOM built exclusively with ctx.h.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  let rootEl = null;
  let rootCtx = null;
  let commitmentsEl = null;
  const filters = { case_id: '', specialty: '', version: '' };
  // Licensing terms for the NEXT cut (audit U5). Kept out of `filters` because
  // they do not narrow the slice, they say what we are promising about it.
  const licence = { licensed_to: '', exclusive: false, expires_at: '' };

  function render(body, ctx) {
    rootEl = body; rootCtx = ctx;
    const { h, clear } = ctx;
    clear(body);

    const caseInput = h('input', { class: 'asc-input', placeholder: 'e.g. t-4f2a91 (exact)', value: filters.case_id });
    const specSelect = h('select', { class: 'asc-input' }, h('option', { value: '' }, 'All specialties'));
    const verSelect = h('select', { class: 'asc-input' },
      h('option', { value: '' }, 'All versions'),
      h('option', { value: 'V3' }, 'V3'),
      h('option', { value: 'V4' }, 'V4'),
      h('option', { value: 'V5' }, 'V5'));
    verSelect.value = filters.version;

    const previewBox = h('div', { id: 'ascExportCasePreview' });
    const exportBtn = h('button', { class: 'asc-btn asc-btn-primary', disabled: '' }, 'Export bundle');
    const statusBox = h('div', {});

    // Licensing sits in the same card as the filters and above the button, so the
    // terms are decided while the slice is, not remembered afterwards.
    const buyerInput = h('input', { class: 'asc-input', placeholder: 'buyer email or account key', value: licence.licensed_to });
    const exclusiveBox = h('input', { type: 'checkbox', class: 'asc-checkbox' });
    exclusiveBox.checked = licence.exclusive;
    const expiryInput = h('input', { class: 'asc-input', type: 'date', value: licence.expires_at });
    const licenceNote = h('div', { class: 'asc-dim' },
      'Exclusive means these exact records cannot go to anyone else until the ' +
      'commitment is released or expires. Leave it off for an ordinary ' +
      'non-exclusive sale, which is the default and blocks nothing.');

    const card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Export by case'),
        h('div', { class: 'asc-card-sub' },
          'Cut a bundle by case, not by physician. The preview below always shows ' +
          'exactly what will ship before you build it.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Case ID'), caseInput),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Specialty'), specSelect),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Version'), verSelect)),
        h('div', { class: 'asc-form-row-3' },
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Licensed to'), buyerInput),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Exclusive'),
            h('label', { class: 'asc-checkbox-row' }, exclusiveBox,
              h('span', {}, 'Sold exclusively to this buyer'))),
          h('div', { class: 'asc-field' }, h('label', { class: 'asc-label' }, 'Exclusive until'), expiryInput)),
        licenceNote,
        previewBox,
        exportBtn, statusBox));
    body.appendChild(card);

    const onLicenceChange = () => {
      licence.licensed_to = buyerInput.value.trim();
      licence.exclusive = exclusiveBox.checked;
      licence.expires_at = expiryInput.value;
    };
    buyerInput.addEventListener('input', onLicenceChange);
    exclusiveBox.addEventListener('change', onLicenceChange);
    expiryInput.addEventListener('change', onLicenceChange);

    commitmentsEl = h('div', {});
    body.appendChild(commitmentsEl);
    refreshCommitments(ctx, commitmentsEl);

    // Filter changes re-preview (the preview is the safety mechanism — it must
    // always reflect the CURRENT filters before the button does anything).
    let debounce = null;
    const onChange = () => {
      filters.case_id = caseInput.value.trim();
      filters.specialty = specSelect.value;
      filters.version = verSelect.value;
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => refreshPreview(ctx, previewBox, exportBtn), 250);
    };
    caseInput.addEventListener('input', onChange);
    specSelect.addEventListener('change', onChange);
    verSelect.addEventListener('change', onChange);

    exportBtn.addEventListener('click', () => runExport(ctx, exportBtn, statusBox));

    populateSpecialties(ctx, specSelect);
    refreshPreview(ctx, previewBox, exportBtn);
  }

  async function populateSpecialties(ctx, select) {
    const { h, api } = ctx;
    try {
      const res = await api('/admin/export/case-options');
      (res.specialties || []).forEach((s) => select.appendChild(h('option', { value: s }, s)));
      select.value = filters.specialty;
    } catch (_e) { /* dropdown stays "All specialties" — filter still typable via case id */ }
  }

  function fmtMB(bytes) {
    if (!bytes) return '—';
    const mb = bytes / (1024 * 1024);
    if (mb < 0.1) return Math.max(1, Math.round(bytes / 1024)) + ' KB';
    return (mb >= 10 ? Math.round(mb) : mb.toFixed(1)) + ' MB';
  }

  async function refreshPreview(ctx, box, exportBtn) {
    const { h, api, clear } = ctx;
    clear(box);
    box.appendChild(h('div', { class: 'asc-dim', style: 'padding:14px 0' }, 'Sizing this slice…'));
    let p;
    try {
      const qs = new URLSearchParams();
      if (filters.case_id) qs.set('case_id', filters.case_id);
      if (filters.specialty) qs.set('specialty', filters.specialty);
      if (filters.version) qs.set('version', filters.version);
      p = await api('/admin/export/case-preview' + (qs.toString() ? '?' + qs.toString() : ''));
    } catch (e) {
      clear(box);
      box.appendChild(h('div', { class: 'asc-inline-error', style: 'margin:12px 0' },
        e.message || 'Could not size this slice.'));
      exportBtn.setAttribute('disabled', '');
      return;
    }
    clear(box);
    const line = [
      String(p.cases || 0) + ' case' + (p.cases === 1 ? '' : 's'),
      String(p.labeler_submissions || 0) + ' labeler submission' + (p.labeler_submissions === 1 ? '' : 's'),
      String(p.reviews || 0) + ' review' + (p.reviews === 1 ? '' : 's'),
      String(p.specialty_count || 0) + ' specialt' + (p.specialty_count === 1 ? 'y' : 'ies'),
    ].join(' · ');
    box.appendChild(h('div', { class: 'asc-export-preview' },
      h('div', {}, line),
      h('div', { class: 'asc-dim' }, 'Estimated bundle ' + fmtMB(p.estimated_bytes)),
      p.note ? h('div', { class: 'asc-inline-warn', style: 'margin-top:8px' }, p.note) : ''));
    if (p.cases > 0 && p.exportable !== false) exportBtn.removeAttribute('disabled');
    else exportBtn.setAttribute('disabled', '');
  }

  async function runExport(ctx, exportBtn, statusBox) {
    const { h, api, clear, toast, downloadBlob } = ctx;
    clear(statusBox);
    exportBtn.setAttribute('disabled', '');
    exportBtn.textContent = 'Building bundle…';
    try {
      const res = await api('/admin/export/case-bundle', { method: 'POST', body: {
        case_id: filters.case_id || null,
        specialty: filters.specialty || null,
        version: filters.version || null,
        licensed_to: licence.licensed_to || null,
        exclusive: !!licence.exclusive,
        license_expires_at: licence.expires_at || null,
      } });
      const bundles = (res.exports && res.exports.length)
        ? res.exports
        : [{ export_id: res.export_id, filename: res.filename, record_count: res.record_count }];
      toast('Export built — ' + (res.record_count || 0) + ' records in '
        + bundles.length + ' bundle' + (bundles.length === 1 ? '' : 's') + '.', 'success');
      statusBox.appendChild(h('div', { class: 'asc-inline-ok' },
        'Ready: ' + (res.record_count || 0) + ' records'
        + (bundles.length > 1 ? ' across ' + bundles.length + ' bundles (one per labeler submission of this case)' : '') + '.'));
      bundles.forEach((b) => {
        const label = b.filename || (b.export_id + '.zip');
        const dlBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', style: 'margin:10px 6px 0 0' },
          'Download ' + label + (b.record_count != null ? ' (' + b.record_count + ' records)' : ''));
        dlBtn.addEventListener('click', () =>
          downloadBlob('/exports/' + b.export_id + '/download', label));
        statusBox.appendChild(dlBtn);
      });
      if (res.licensing) {
        statusBox.appendChild(h('div', { class: 'asc-dim', style: 'margin-top:8px' },
          (res.licensing.exclusivity === 'exclusive' ? 'Exclusive' : 'Non-exclusive')
          + ' licence ' + res.licensing.license_id + ' to ' + res.licensing.licensed_to
          + (res.licensing.expires_at ? ', until ' + res.licensing.expires_at : '') + '.'));
      }
      // A cut that just took records off the market has to show that in the same
      // breath, or the register on screen is already wrong.
      if (commitmentsEl && rootCtx) refreshCommitments(rootCtx, commitmentsEl);
    } catch (e) {
      // A 409 here is the exclusivity refusal, and its message names the licence
      // that blocked the cut. Shown in full: "export failed" would send the
      // operator hunting for a bug instead of to a contract.
      statusBox.appendChild(h('div', { class: 'asc-inline-error' }, e.message || 'Export failed.'));
    } finally {
      exportBtn.textContent = 'Export bundle';
      exportBtn.removeAttribute('disabled');
    }
  }

  // ─── Exclusive commitments (audit U5) ──────────────────────────────────────
  async function refreshCommitments(ctx, box) {
    const { h, api, clear } = ctx;
    clear(box);
    let data;
    try {
      data = await api('/admin/export/exclusivity');
    } catch (e) {
      box.appendChild(h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-inline-error' },
            e.message || 'Could not load exclusive commitments.'))));
      return;
    }
    const active = data.active || [];
    const ended = data.ended || [];
    const pad = h('div', { class: 'asc-card-pad' });
    if (!active.length) {
      pad.appendChild(h('div', { class: 'asc-empty' },
        'Nothing is committed exclusively. Every record is free to sell.'));
    } else {
      const table = h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Buyer'), h('th', {}, 'Records'), h('th', {}, 'Cases'),
          h('th', {}, 'Until'), h('th', {}, 'Licence'), h('th', {}, ''))));
      const tbody = h('tbody', {});
      active.forEach((lic) => {
        const relBtn = h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm' }, 'Release');
        relBtn.addEventListener('click', () => releaseCommitment(ctx, box, lic, relBtn));
        tbody.appendChild(h('tr', {},
          h('td', {}, h('span', { class: 'asc-badge asc-badge-lime' }, 'Exclusive'),
            h('span', {}, ' ' + lic.buyer)),
          h('td', {}, String(lic.record_count || 0)),
          h('td', {}, String(lic.case_count || 0)),
          h('td', {}, lic.expires_at || 'No end date'),
          h('td', {}, h('span', { class: 'asc-tag' }, lic.license_id)),
          h('td', {}, relBtn)));
      });
      table.appendChild(tbody);
      pad.appendChild(table);
      pad.appendChild(h('div', { class: 'asc-dim', style: 'margin-top:10px' },
        String(data.committed_record_count || 0) + ' record'
        + ((data.committed_record_count === 1) ? '' : 's')
        + ' cannot be sold to anyone else while these stand.'));
    }
    if (ended.length) {
      pad.appendChild(h('div', { class: 'asc-dim', style: 'margin-top:10px' },
        String(ended.length) + ' ended commitment'
        + (ended.length === 1 ? '' : 's') + ' kept on record: '
        + ended.map((l) => l.buyer + ' (' + l.license_id + ')').join(', ') + '.'));
    }
    box.appendChild(h('div', { class: 'asc-card', style: 'margin-top:16px' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Exclusive commitments'),
        h('div', { class: 'asc-card-sub' },
          'What we have promised to one buyer and cannot sell again. An export ' +
          'that overlaps any of these is refused and names the licence.'))),
      pad));
  }

  async function releaseCommitment(ctx, box, lic, btn) {
    const { api, toast } = ctx;
    const ok = window.confirm(
      'Release the exclusive commitment to ' + lic.buyer + '? '
      + (lic.record_count || 0) + ' record(s) become sellable to anyone again. '
      + 'Do this only when the agreement actually allows it.');
    if (!ok) return;
    btn.setAttribute('disabled', '');
    btn.textContent = 'Releasing…';
    try {
      await api('/admin/export/exclusivity/' + lic.license_id + '/release',
        { method: 'POST', body: { reason: 'Released from the admin export view' } });
      toast('Released: ' + (lic.record_count || 0) + ' record(s) freed.', 'success');
    } catch (e) {
      toast(e.message || 'Could not release that commitment.', 'error');
    }
    refreshCommitments(ctx, box);
  }

  window.AdminExportSection = { render, reset() { /* filters persist intentionally */ } };
})();
