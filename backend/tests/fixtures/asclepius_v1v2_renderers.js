// GOLDEN — do not edit by hand.
//
// The two step renderers only V1 (classic) and V2 (assisted) can reach.
// renderRationale gates them behind `if (!isV3())`, and repaintSteps routes
// V3/V4 to renderStepsListV3, so nothing here is on a V3/V4 path.
//
// Captured from the code that was shipping BEFORE the Eval UI Overhaul, with
// comments and blank lines stripped. test_v1_v2_step_renderers_are_untouched
// diffs the live source against this. A golden rather than a `git show main:`
// comparison on purpose: once this work merges, main IS the live source, and a
// test that compares a file to itself passes forever while guarding nothing.
//
// Changing this file is changing V1/V2 behaviour. That is allowed — but it has
// to be a decision someone made on purpose, which is the whole point.

function renderStepsCard(forBoth) {
    const listId = 'ascStepsList';
    const required = (state.task.grounding_mode === 'required');
    const canAutoSplit = !forBoth;  // chosen path (A/B verdict) only
    const addBtn = h('button', {
      class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
      onClick: () => { activeSteps().push(newAuthoredStep()); saveDraft(); renderStepsList(listId); updateSubmitState(); },
    }, '+ Add step');
    const resplitBtn = canAutoSplit ? h('button', {
      class: 'asc-btn asc-btn-ghost asc-btn-sm', type: 'button',
      onClick: () => autoSplitChosen(listId, true),
    }, '↻ Re-split from answer') : null;
    const card = h('div', { class: 'asc-subcard' },
      h('div', { class: 'asc-subcard-head' }, '↳ Reasoning steps ',
        h('span', { class: 'asc-label-hint', style: 'margin-left:6px' },
          canAutoSplit ? 'confirm each step, or edit it to correct it' : (required ? '(each step needs a citation)' : '(optional)'))),
      h('div', { class: 'asc-subcard-body' },
        h('div', { class: 'asc-steps', id: listId }),
        h('div', { style: 'margin-top:12px;display:flex;gap:8px;flex-wrap:wrap' }, addBtn, resplitBtn),
      ));
    setTimeout(() => {
      renderStepsList(listId);
      if (canAutoSplit && activeSteps().length === 0
          && state.splitAttemptedFor !== state.task.task_id && !state.splitting) {
        state.splitAttemptedFor = state.task.task_id;
        autoSplitChosen(listId, false);
      }
    }, 0);
    return card;
  }

function renderStepsList(listId) {
    const list = document.getElementById(listId);
    if (!list) return;
    clear(list);
    const steps = activeSteps();
    const reasons = (state.taxonomy.step_correction_reasons
      || ['factual_error', 'outdated_guideline', 'incomplete', 'unsafe', 'wrong_order', 'minor_wording']);
    const required = (state.task.grounding_mode === 'required');
    const isCollapsed = (s) => (
      s.suggested_label === 'good' && !s._exp && !s.corrected && !s.added
      && (s.text || '').trim() === (s.original_text || '').trim()
    );
    const pendingGood = steps.filter((s) => isCollapsed(s) && !s.confirmed);
    if (pendingGood.length) {
      list.appendChild(h('div', { class: 'asc-step-bulkbar' },
        h('span', { class: 'asc-step-bulk-label' },
          pendingGood.length + ' step' + (pendingGood.length === 1 ? ' looks' : 's look')
          + ' correct to the model — read them, then confirm in one tap.'),
        h('button', {
          class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button',
          onClick: () => {
            steps.forEach((s) => { if (isCollapsed(s) && !s.confirmed) setStepConfirmed(s, true); });
            saveDraft(); renderStepsList(listId); updateSubmitState();
          },
        }, '✓ Confirm all correct')));
    }
    steps.forEach((s, idx) => {
      s.step = idx + 1;
      const hasOriginal = s.original_text != null;
      if (isCollapsed(s)) {
        const pill = h('span', { class: 'asc-step-status ' + (s.confirmed ? 'confirmed' : 'pending') },
          s.confirmed ? 'confirmed ✓' : 'pending');
        list.appendChild(h('div', { class: 'asc-step asc-step-collapsed' + (s.confirmed ? ' is-confirmed' : '') },
          h('div', { class: 'asc-step-head' },
            h('div', { style: 'display:flex;align-items:center;gap:8px;min-width:0' },
              h('span', { class: 'asc-step-num' }, 'Step ' + (idx + 1)),
              h('span', { class: 'asc-step-suggest good', title: 'Model pre-grade — your confirmation is the label' }, 'model · looks correct'),
              pill),
            h('div', { style: 'display:flex;align-items:center;gap:8px' },
              h('button', {
                class: 'asc-btn asc-btn-ghost asc-btn-sm asc-step-confirm' + (s.confirmed ? ' active' : ''),
                type: 'button',
                onClick: () => {
                  setStepConfirmed(s, !s.confirmed);
                  saveDraft(); renderStepsList(listId); updateSubmitState();
                },
              }, s.confirmed ? '✓ Confirmed' : '✓ Correct as-is'),
              h('button', {
                class: 'asc-btn-link', type: 'button',
                onClick: () => { s._exp = true; renderStepsList(listId); },
              }, 'Edit'))),
          h('div', { class: 'asc-step-collapsed-text' }, s.text || '')));
        return;
      }
      const ta = h('textarea', { class: 'asc-textarea', placeholder: 'Describe this reasoning step…' }, s.text || '');
      const statusPill = h('span', { class: 'asc-step-status' }, '');
      const addedBadge = h('span', { class: 'asc-badge asc-badge-accent asc-step-added' }, 'added (AI omitted)');
      const confirmBtn = h('button', {
        class: 'asc-btn asc-btn-ghost asc-btn-sm asc-step-confirm', type: 'button',
        onClick: () => {
          setStepConfirmed(s, !s.confirmed);
          saveDraft(); syncStepUI(); updateSubmitState();
        },
      }, '✓ Correct as-is');
      const chipEls = {};
      const reasonRow = h('div', { class: 'asc-step-reasons' });
      reasons.forEach((r) => {
        const chip = h('button', {
          class: 'asc-chip asc-chip-sm', type: 'button',
          onClick: () => {
            s.correction_reason = r;
            s.label = (r === 'minor_wording') ? 'neutral' : 'bad';
            s.step_reward = s.label === 'good' ? 1 : 0;
            saveDraft(); syncStepUI(); updateSubmitState();
          },
        }, r.replace(/_/g, ' '));
        chipEls[r] = chip;
        reasonRow.appendChild(chip);
      });
      const reasonWrap = h('div', { class: 'asc-step-correct' },
        h('div', { class: 'asc-label asc-step-reason-hint' }, 'What was wrong with the AI step? (pick one)'),
        reasonRow);
      const originalBox = hasOriginal
        ? h('details', { class: 'asc-step-original' },
            h('summary', {}, 'original: ' + ((s.original_text || '').length > 80
              ? (s.original_text || '').slice(0, 80) + '…' : (s.original_text || ''))),
            h('div', { class: 'asc-step-original-full' }, s.original_text || ''))
        : null;
      const ci = h('input', { class: 'asc-input', placeholder: "What's off with this step? (optional, one line)", value: s.critique || '' });
      ci.addEventListener('input', () => { s.critique = ci.value; saveDraft(); });
      const critiqueField = h('div', { class: 'asc-field', style: 'margin-top:8px' }, withMic(ci));
      const flaggedBadge = (s.suggested_label === 'bad')
        ? h('span', { class: 'asc-step-suggest bad', title: 'Model pre-grade — verify and confirm or correct' }, 'model · flags this')
        : null;
      const suggestHint = (s.suggested_label === 'bad' && s.suggested_critique)
        ? h('div', { class: 'asc-step-suggest-hint' }, 'Model: ' + s.suggested_critique)
        : null;
      const head = h('div', { class: 'asc-step-head' },
        h('div', { style: 'display:flex;align-items:center;gap:8px' },
          h('span', { class: 'asc-step-num' }, 'Step ' + (idx + 1)), statusPill, addedBadge, flaggedBadge),
        h('div', { style: 'display:flex;align-items:center;gap:8px' },
          confirmBtn,
          h('button', {
            class: 'asc-btn-link', type: 'button',
            onClick: () => { steps.splice(idx + 1, 0, newAuthoredStep()); saveDraft(); renderStepsList(listId); updateSubmitState(); },
          }, '+ insert'),
          h('button', {
            class: 'asc-btn-link', type: 'button', style: 'color:var(--asc-danger)',
            onClick: () => { steps.splice(idx, 1); saveDraft(); renderStepsList(listId); updateSubmitState(); },
          }, 'Remove')));
      const anchorBlock = renderAnchorBlock(s.evidence_anchor, { label: 'citation for this step', required });
      const stepCite = renderCiteSuggest(
        s.evidence_anchor,
        () => ((s.text || '') + ' ' + (s.critique || '')),
        () => renderStepsList(listId));
      wireCiteSuggest(ta, stepCite);
      wireCiteSuggest(ci, stepCite);
      function syncStepUI() {
        const corrected = !!s.corrected, added = !!s.added, confirmed = !!s.confirmed;
        let text = 'pending', cls = 'pending';
        if (added) { text = 'added'; cls = 'added'; }
        else if (corrected) {
          text = s.correction_reason ? ('corrected · ' + s.correction_reason.replace(/_/g, ' ')) : 'corrected — pick a reason';
          cls = 'corrected';
        } else if (confirmed) { text = 'confirmed ✓'; cls = 'confirmed'; }
        statusPill.textContent = text;
        statusPill.className = 'asc-step-status ' + cls;
        addedBadge.hidden = !added;
        confirmBtn.hidden = corrected || added;
        confirmBtn.classList.toggle('active', confirmed);
        reasonWrap.hidden = !corrected;
        if (originalBox) originalBox.hidden = !corrected;
        critiqueField.hidden = !corrected;
        Object.keys(chipEls).forEach((r) => chipEls[r].classList.toggle('active', s.correction_reason === r));
      }
      ta.addEventListener('input', () => {
        s.text = ta.value;
        if (hasOriginal) {
          if (ta.value.trim() !== (s.original_text || '').trim()) {
            if (!s.corrected) { s.corrected = true; s.confirmed = false; }
          } else {
            s.corrected = false; s.confirmed = false; s.correction_reason = null;
            s.label = null; s.step_reward = null;
          }
        }
        saveDraft(); syncStepUI(); updateSubmitState();
      });
      syncStepUI();
      list.appendChild(h('div', { class: 'asc-step' }, head, suggestHint, ta, reasonWrap, originalBox, critiqueField, stepCite, anchorBlock));
    });
    if (!steps.length) {
      list.appendChild(h('p', { class: 'asc-help' }, 'No steps yet — add steps manually, or use “Re-split from answer”.'));
    }
  }
