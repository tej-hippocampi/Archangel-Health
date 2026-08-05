/* ═══════════════════════════════════════════════════════════
   Advisor section (Advisor PRD §6.2)

   The medical advisor's own surface inside the evaluator portal.
   Everything else an advisor does — labeling, reviewing — happens
   on the existing evaluator and reviewer surfaces, gated on
   CAPABILITY rather than tier. This section is the part that has
   no home elsewhere:

     Your referrals        [ Invite a physician ]   3 invited · 2 active
     Awaiting your review  Task batches · Outbound bundles · Hospital intake
     Your sign-offs        history, newest first

   Visibility is decided by the SERVER: asclepius.js mounts this
   only when the session payload's `capabilities` array contains
   'refer'. The client never re-derives that from a tier string —
   pushing the two-state check into the frontend is the same bug
   one layer out.

   Design system as locked: canvas #eef0ef, card #fbfcfa, ink
   #1a1b1a, green for physician-authored, orange for model output,
   pink for flags, lime for new. Air is the design · scale not
   boldness · zero black fills · mono for wayfinding.

   Loaded as its own file (§6.2 — do not add 400 lines to an
   8,000-line file); DOM built exclusively through ctx.h.
   ═══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var activeTab = 'referrals';   // referrals | queue | signoffs
  var openArtifact = null;       // { type, id } while reviewing one
  var rootEl = null;
  var rootCtx = null;

  var ARTIFACT_LABELS = {
    task_batch: 'Task batches',
    export_bundle: 'Outbound bundles',
    inbound_upload: 'Hospital intake',
    product_spec: 'Product specs',
  };
  var ARTIFACT_ORDER = ['task_batch', 'export_bundle', 'inbound_upload', 'product_spec'];

  var VERDICT_LABELS = {
    approved: 'Approve',
    approved_with_comments: 'Approve with comments',
    changes_requested: 'Request changes',
  };

  // ─── Entry ────────────────────────────────────────────────
  function render(body, ctx) {
    rootEl = body; rootCtx = ctx;
    var h = ctx.h;
    ctx.clear(body);

    var tabs = [
      ['referrals', 'Your referrals'],
      ['queue', 'Awaiting your review'],
      ['signoffs', 'Your sign-offs'],
    ];
    body.appendChild(h('div', { class: 'asc-subnav', style: 'margin-bottom:14px' },
      tabs.map(function (t) {
        var btn = h('button', {
          class: 'asc-subnav-btn' + (activeTab === t[0] ? ' active' : ''),
        }, t[1]);
        btn.addEventListener('click', function () {
          activeTab = t[0]; openArtifact = null; render(body, ctx);
        });
        return btn;
      })));

    var inner = h('div', {});
    body.appendChild(inner);
    if (activeTab === 'referrals') renderReferrals(inner, ctx);
    else if (activeTab === 'queue') {
      if (openArtifact) renderArtifact(inner, ctx, openArtifact);
      else renderQueue(inner, ctx);
    } else renderSignoffs(inner, ctx);
  }

  function rerender() {
    if (rootEl && rootCtx) render(rootEl, rootCtx);
  }

  function errorCard(h, message) {
    return h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
      h('div', { class: 'asc-inline-error' }, message)));
  }

  // ─── Referrals ────────────────────────────────────────────
  async function renderReferrals(container, ctx) {
    var h = ctx.h;
    ctx.clear(container);
    container.appendChild(ctx.loadingCard('Loading your referrals…'));
    var data;
    try {
      data = await ctx.api('/advisor/referrals');
    } catch (e) {
      ctx.clear(container);
      container.appendChild(errorCard(h, e.message || 'Could not load your referrals.'));
      return;
    }
    ctx.clear(container);

    var rows = data.referrals || [];
    var head = h('div', { class: 'asc-card-head' },
      h('div', {},
        h('div', { class: 'asc-card-title' }, 'Your referrals'),
        h('div', { class: 'asc-card-sub' },
          (data.total || 0) + ' invited · ' + (data.active || 0) + ' active')));
    var card = h('div', { class: 'asc-card' }, head);

    // Invite form.
    var nameInput = h('input', { class: 'asc-input', type: 'text', placeholder: 'Name' });
    var emailInput = h('input', { class: 'asc-input', type: 'email', placeholder: 'Email' });
    var noteInput = h('input', {
      class: 'asc-input', type: 'text',
      placeholder: 'Note — optional, e.g. "knows her from Stanford ortho"',
    });
    var status = h('div', { class: 'asc-card-sub' }, '');
    var sendBtn = h('button', { class: 'asc-btn asc-btn-primary' }, 'Invite a physician');
    sendBtn.addEventListener('click', async function () {
      var email = (emailInput.value || '').trim();
      if (!email) { status.textContent = 'An email address is required.'; return; }
      sendBtn.disabled = true;
      status.textContent = 'Sending…';
      try {
        var res = await ctx.api('/advisor/referrals', {
          method: 'POST',
          body: {
            email: email,
            name: (nameInput.value || '').trim() || null,
            note: (noteInput.value || '').trim() || null,
          },
        });
        if (res.already === 'member') {
          status.textContent = 'That physician is already a member.';
        } else if (res.already === 'invited') {
          status.textContent = 'You already invited this physician.';
        } else if (res.email_sent) {
          status.textContent = 'Invitation sent.';
        } else {
          // Email not configured is a fact, not a silent success: hand back the
          // link so the advisor can send it themselves.
          status.textContent = 'Referral recorded, but the email could not be sent. '
            + 'Share this link: ' + (res.invite_url || '');
        }
        nameInput.value = ''; emailInput.value = ''; noteInput.value = '';
        rerender();
      } catch (e) {
        status.textContent = e.message || 'Could not send that invitation.';
      } finally {
        sendBtn.disabled = false;
      }
    });

    var inviteBits = [
      h('div', { class: 'asc-form-row' }, nameInput, emailInput),
      noteInput,
      h('div', { class: 'asc-form-row' }, sendBtn, status),
    ];
    if (data.invite_url) {
      inviteBits.push(h('div', { class: 'asc-card-sub' },
        'Or share your link: ', h('code', { class: 'asc-mono' }, data.invite_url)));
    }
    card.appendChild(h('div', { class: 'asc-card-pad' }, inviteBits));

    if (!rows.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'No referrals yet.')));
      container.appendChild(card);
      return;
    }
    // Name, date, plain-words status, and nothing else. A referrer is not
    // entitled to the credentialing file of the person they referred — no NPI,
    // no score, no verification internals (§3.3). The server does not send
    // them; this table could not render them if it wanted to.
    card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
      h('thead', {}, h('tr', {},
        h('th', {}, 'Name'), h('th', {}, 'Invited'), h('th', {}, 'Status'), h('th', {}, 'Note'))),
      h('tbody', {}, rows.map(function (r) {
        return h('tr', {},
          h('td', {}, h('strong', {}, r.invitee_name || r.invitee_email || '—')),
          h('td', {}, r.invited_at ? ctx.fmtDate(r.invited_at) : '—'),
          h('td', {}, statusBadge(h, r)),
          h('td', {}, r.note || '—'));
      })))));
    container.appendChild(card);
  }

  function statusBadge(h, r) {
    var word = r.status_word || 'Invited';
    var cls = 'asc-badge asc-badge-gray';
    if (r.status === 'approved') cls = 'asc-badge asc-badge-green';
    else if (r.status === 'verified' || r.status === 'signed_up') cls = 'asc-badge asc-badge-amber';
    else if (r.status === 'declined') cls = 'asc-badge asc-badge-red';
    return h('span', { class: cls }, word);
  }

  // ─── Awaiting your review ─────────────────────────────────
  async function renderQueue(container, ctx) {
    var h = ctx.h;
    ctx.clear(container);
    container.appendChild(ctx.loadingCard('Loading your queue…'));
    var data;
    try {
      data = await ctx.api('/advisor/queue');
    } catch (e) {
      ctx.clear(container);
      container.appendChild(errorCard(h, e.message || 'Could not load your queue.'));
      return;
    }
    ctx.clear(container);
    var queue = data.queue || {};
    var any = false;

    ARTIFACT_ORDER.forEach(function (type) {
      var items = queue[type];
      // Absent means "you do not hold that capability", which is a different
      // fact from an empty list — render nothing rather than a misleading
      // "0 awaiting".
      if (!items) return;
      any = true;
      var card = h('div', { class: 'asc-card' },
        h('div', { class: 'asc-card-head' }, h('div', {},
          h('div', { class: 'asc-card-title' }, ARTIFACT_LABELS[type],
            h('span', { class: 'asc-badge asc-badge-count', style: 'margin-left:8px' },
              String(items.length))))));
      if (!items.length) {
        card.appendChild(h('div', { class: 'asc-card-pad' },
          h('div', { class: 'asc-empty' }, 'Nothing awaiting you here.')));
        container.appendChild(card);
        return;
      }
      card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, 'Artifact'), h('th', {}, 'Detail'),
          h('th', {}, 'Sign-offs'), h('th', {}, 'Latest'))),
        h('tbody', {}, items.map(function (it) {
          var id = artifactId(type, it);
          var tr = h('tr', { class: 'asc-row-click' },
            h('td', {}, h('code', { class: 'asc-mono' }, id)),
            h('td', {}, artifactDetail(type, it)),
            h('td', {}, String(it.signoffs || 0)),
            h('td', {}, it.latest_verdict ? verdictBadge(h, it.latest_verdict) : '—'));
          tr.addEventListener('click', function () {
            openArtifact = { type: type, id: id }; rerender();
          });
          return tr;
        })))));
      container.appendChild(card);
    });

    if (!any) {
      container.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'Nothing is awaiting your review.'))));
    }
  }

  function artifactId(type, it) {
    if (type === 'task_batch') return it.batch_key;
    if (type === 'export_bundle') return it.export_id;
    if (type === 'inbound_upload') return it.upload_id;
    return it.spec_id;
  }

  function artifactDetail(type, it) {
    if (type === 'task_batch') return (it.n_tasks || 0) + ' cases · ' + (it.specialty || '—');
    if (type === 'export_bundle') return (it.record_count || 0) + ' records · ' + (it.profile || '—');
    if (type === 'inbound_upload') return it.status || '—';
    return it.title || '—';
  }

  function verdictBadge(h, v) {
    if (v === 'approved') return h('span', { class: 'asc-badge asc-badge-green' }, 'Approved');
    if (v === 'approved_with_comments') {
      return h('span', { class: 'asc-badge asc-badge-amber' }, 'Approved with comments');
    }
    if (v === 'changes_requested') {
      return h('span', { class: 'asc-badge asc-badge-red' }, 'Changes requested');
    }
    return h('span', { class: 'asc-badge asc-badge-gray' }, v || '—');
  }

  // ─── One artifact, and the verdict form ───────────────────
  async function renderArtifact(container, ctx, ref) {
    var h = ctx.h;
    ctx.clear(container);
    container.appendChild(ctx.loadingCard('Loading…'));
    var data;
    try {
      data = await ctx.api('/advisor/artifacts/' + encodeURIComponent(ref.type)
                           + '/' + encodeURIComponent(ref.id));
    } catch (e) {
      ctx.clear(container);
      container.appendChild(errorCard(h, e.message || 'Could not load that artifact.'));
      return;
    }
    ctx.clear(container);

    var back = h('button', { class: 'asc-btn asc-btn-ghost asc-btn-sm' }, '← Back to queue');
    back.addEventListener('click', function () { openArtifact = null; rerender(); });
    container.appendChild(h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', {},
          h('div', { class: 'asc-card-title' }, ARTIFACT_LABELS[ref.type] || ref.type),
          h('div', { class: 'asc-card-sub' }, h('code', { class: 'asc-mono' }, ref.id))),
        back),
      h('div', { class: 'asc-card-pad' }, artifactBody(h, ref.type, data))));

    container.appendChild(verdictCard(ctx, ref, data));

    var prior = data.signoffs || [];
    if (prior.length) {
      container.appendChild(signoffTable(ctx, 'Sign-offs on this artifact', prior));
    }
  }

  function artifactBody(h, type, d) {
    if (type === 'product_spec') {
      var pre = h('pre', { class: 'asc-mono', style: 'white-space:pre-wrap;font-size:13px' });
      pre.textContent = d.body_md || '';
      return h('div', {}, h('div', { class: 'asc-card-title', style: 'font-size:15px' },
        d.title || ''), pre);
    }
    if (type === 'export_bundle') {
      var fields = (d.field_list || []).join(', ');
      var sample = h('pre', { class: 'asc-mono', style: 'font-size:12px;overflow:auto;max-height:340px' });
      sample.textContent = JSON.stringify(d.sample || [], null, 2);
      return h('div', {},
        h('div', { class: 'asc-card-sub' },
          (d.record_count || 0) + ' records in the bundle · showing ' + (d.sample_n || 0)
          + ' sampled'),
        h('div', { class: 'asc-card-title', style: 'font-size:14px;margin-top:12px' }, 'Fields'),
        h('div', { class: 'asc-mono', style: 'font-size:12px' }, fields || '—'),
        h('div', { class: 'asc-card-title', style: 'font-size:14px;margin-top:12px' },
          'Sampled records'),
        sample);
    }
    if (type === 'inbound_upload') {
      var cases = d.cases || [];
      var withheld = d.n_bodies_withheld || 0;
      // Say what is actually being shown. A case that failed de-identification
      // is stored with its RAW body, so its body is withheld — the findings
      // still tell the advisor what went wrong, which is the review. Claiming
      // "N de-identified cases" over a set that includes withheld ones would be
      // the kind of imprecise label this product exists not to print.
      var summary = (d.n_cases || 0) + ' case' + ((d.n_cases === 1) ? '' : 's')
        + ' · upload status ' + ((d.upload || {}).status || '—');
      if (withheld) {
        summary += ' · ' + withheld + ' body' + (withheld === 1 ? '' : 'ies')
          + ' withheld (failed de-identification)';
      }
      return h('div', {},
        h('div', { class: 'asc-card-sub' }, summary),
        withheld
          ? h('div', { class: 'asc-card-sub' },
              'A case that fails the identifier scan is stored exactly as the '
              + 'hospital sent it, so its body is not shown to anyone outside '
              + 'the admin team. The findings below are masked and are what the '
              + 'review is for.')
          : null,
        h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
          h('thead', {}, h('tr', {},
            h('th', {}, 'Case'), h('th', {}, 'Specialty'), h('th', {}, 'Status'),
            h('th', {}, 'Case body'), h('th', {}, 'Intake findings'))),
          h('tbody', {}, cases.map(function (c) {
            return h('tr', {},
              h('td', {}, h('code', { class: 'asc-mono' }, c.ingest_case_id || '—')),
              h('td', {}, c.specialty || '—'),
              h('td', {}, c.status || '—'),
              h('td', {}, c.body_withheld
                ? h('span', { class: 'asc-badge asc-badge-amber' },
                    c.body_withheld_reason === 'residual_identifiers'
                      ? 'Withheld · identifiers found'
                      : 'Withheld')
                : h('span', { class: 'asc-badge asc-badge-green' }, 'Shown')),
              h('td', {}, findingsText(c.report)));
          })))));
    }
    // task_batch
    var tasks = d.tasks || [];
    return h('div', {},
      h('div', { class: 'asc-card-sub' }, (d.n_tasks || 0) + ' cases in this batch'),
      h('div', {}, tasks.map(function (t) {
        return h('details', { class: 'asc-phys-group' },
          h('summary', {}, h('strong', {}, t.specialty || 'case'),
            h('span', { class: 'asc-dim' }, t.difficulty || '')),
          h('div', { class: 'asc-card-pad' },
            h('div', { class: 'asc-card-title', style: 'font-size:14px' }, 'Prompt'),
            h('div', {}, t.prompt || '—'),
            h('div', { class: 'asc-card-title', style: 'font-size:14px;margin-top:10px' },
              'Candidate answers'),
            h('div', {}, (t.candidate_answers || []).map(function (c) {
              return h('div', { style: 'margin-bottom:8px' },
                h('code', { class: 'asc-mono' }, String(c.id)), ' ', c.text || '');
            }))));
      })));
  }

  // The intake report's real shape, as written by the ingestion pipeline:
  //   verification: { status: 'pass'|'flagged', verifier, findings: [...] }
  //   completeness / missing_modalities / quarantine_reason / unresolved
  // Read the keys that actually exist. Guessing the shape renders a tidy '—'
  // for every row and the whole point of the intake review — the residual
  // identifier scan — silently disappears while the page still looks fine.
  function findingsText(report) {
    if (!report || typeof report !== 'object') return '—';
    var bits = [];
    var v = report.verification || {};
    if (v.status) {
      var n = (v.findings || []).length;
      bits.push(v.status === 'flagged'
        ? 'residual identifiers: ' + n + ' flagged'
        : 'de-id verified (' + (v.verifier || 'scan') + ')');
    }
    if (report.completeness != null) bits.push('completeness ' + report.completeness);
    if (report.missing_modalities && report.missing_modalities.length) {
      bits.push('missing: ' + report.missing_modalities.join(', '));
    }
    if (report.unresolved && report.unresolved.length) {
      bits.push('unresolved: ' + report.unresolved.length);
    }
    if (report.quarantine_reason) bits.push('quarantined: ' + report.quarantine_reason);
    return bits.length ? bits.join(' · ') : '—';
  }

  function verdictCard(ctx, ref, data) {
    var h = ctx.h;
    var select = h('select', { class: 'asc-input', 'aria-label': 'Verdict' },
      h('option', { value: '' }, '— choose a verdict —'),
      h('option', { value: 'approved' }, VERDICT_LABELS.approved),
      h('option', { value: 'approved_with_comments' }, VERDICT_LABELS.approved_with_comments),
      h('option', { value: 'changes_requested' }, VERDICT_LABELS.changes_requested));
    var comments = h('textarea', {
      class: 'asc-input', rows: '4',
      placeholder: 'Comments — required to request changes, and to approve with comments',
    });
    var status = h('div', { class: 'asc-card-sub' }, '');
    var submit = h('button', { class: 'asc-btn asc-btn-primary' }, 'Record sign-off');
    submit.addEventListener('click', async function () {
      var verdict = select.value;
      if (!verdict) { status.textContent = 'Choose a verdict.'; return; }
      var text = (comments.value || '').trim();
      if (verdict === 'changes_requested' && !text) {
        status.textContent = 'Requesting changes needs comments — an unexplained '
          + 'rejection is unusable to everyone downstream.';
        return;
      }
      if (verdict === 'approved_with_comments' && !text) {
        status.textContent = 'Add the comments, or choose a plain approval.';
        return;
      }
      submit.disabled = true;
      status.textContent = 'Recording…';
      try {
        await ctx.api('/advisor/signoffs', {
          method: 'POST',
          body: {
            artifact_type: ref.type, artifact_id: ref.id,
            verdict: verdict, comments: text || null,
          },
        });
        ctx.toast('Sign-off recorded.', 'success');
        openArtifact = null;
        rerender();
      } catch (e) {
        status.textContent = e.message || 'Could not record that sign-off.';
        submit.disabled = false;
      }
    });

    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Your verdict'),
        // Says plainly what this does and does not do. An advisor should not
        // believe they are holding up a shipment when they are not.
        h('div', { class: 'asc-card-sub' },
          'Recorded as feedback and shown in admin. It does not block the '
          + 'export or the batch from shipping.'))),
      h('div', { class: 'asc-card-pad' },
        select, comments,
        h('div', { class: 'asc-form-row' }, submit, status),
        h('div', { class: 'asc-card-sub', style: 'margin-top:10px' },
          'Your sign-off is recorded as a related-party attestation — you hold '
          + 'an advisory relationship with Archangel Health, and that is stated '
          + 'alongside your verdict so a buyer never has to ask.')));
  }

  // ─── Your sign-offs ───────────────────────────────────────
  async function renderSignoffs(container, ctx) {
    var h = ctx.h;
    ctx.clear(container);
    container.appendChild(ctx.loadingCard('Loading your sign-offs…'));
    var data;
    try {
      data = await ctx.api('/advisor/signoffs');
    } catch (e) {
      ctx.clear(container);
      container.appendChild(errorCard(h, e.message || 'Could not load your sign-offs.'));
      return;
    }
    ctx.clear(container);
    container.appendChild(signoffTable(ctx, 'Your sign-offs', data.signoffs || []));
  }

  function signoffTable(ctx, title, rows) {
    var h = ctx.h;
    var card = h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, title,
          h('span', { class: 'asc-badge asc-badge-count', style: 'margin-left:8px' },
            String(rows.length))))));
    if (!rows.length) {
      card.appendChild(h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-empty' }, 'Nothing yet.')));
      return card;
    }
    card.appendChild(h('div', { class: 'asc-table-wrap' }, h('table', { class: 'asc-table' },
      h('thead', {}, h('tr', {},
        h('th', {}, 'When'), h('th', {}, 'Artifact'), h('th', {}, 'Verdict'),
        h('th', {}, 'Relationship'), h('th', {}, 'Comments'))),
      h('tbody', {}, rows.map(function (r) {
        return h('tr', {},
          h('td', {}, r.created_at ? ctx.fmtDate(r.created_at) : '—'),
          h('td', {}, (ARTIFACT_LABELS[r.artifact_type] || r.artifact_type) + ' · ',
            h('code', { class: 'asc-mono' }, r.artifact_id || '—')),
          h('td', {}, verdictBadge(h, r.verdict)),
          // Shown, not hidden. The disclosure is the point.
          h('td', {}, h('code', { class: 'asc-mono' }, r.relationship || '—')),
          h('td', {}, r.comments || '—'));
      })))));
    return card;
  }

  window.AdvisorSection = {
    render: render,
    reset: function () { activeTab = 'referrals'; openArtifact = null; },
  };
})();
