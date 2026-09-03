/* ═══════════════════════════════════════════════════════════════════════════
   Admin · Community (PRD-F, and U11 from the founder meeting)

   The meeting asked for two things here: that we can post and answer
   questions, and that "we get a summary of what's going on". This tab is the
   second, and it is a COUNT, not a chat.

   THERE IS NO ASSISTANT ON THIS PAGE, deliberately (R10). Every figure below
   comes from GET /admin/community/summary, which is SQL over the community
   tables. An operator can check any number on this screen by opening the room
   it names. A paraphrase cannot be checked, and a paraphrase of how a
   community is doing is exactly the kind of claim that is wrong quietly.

   THE COMPOSER IS LINKED, NOT REBUILT. Posting as the Archangel persona lives
   in community.js, in one modal, against one endpoint, with the channel
   allow-list and the announce rule already right. A second implementation here
   would be a second place to get the fan-out rule wrong, so the button opens
   the community surface instead.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const COMMUNITY_URL = '/asclepius/community';

  let view = { data: null, err: null, busy: false };

  function render(body, ctx) {
    const { h, clear } = ctx;
    clear(body);
    const host = h('div', {});
    body.appendChild(host);
    paint(host, ctx);
    if (!view.data && !view.err) load(host, ctx);
  }

  function load(host, ctx) {
    view.busy = true;
    paint(host, ctx);
    ctx.api('/admin/community/summary').then((d) => {
      view.data = d; view.err = null;
    }).catch((e) => {
      view.data = null;
      view.err = (e && e.message) || 'Could not read the community.';
    }).then(() => {
      view.busy = false;
      paint(host, ctx);
    });
  }

  function paint(host, ctx) {
    const { h, clear } = ctx;
    clear(host);
    if (view.busy && !view.data) { host.appendChild(ctx.loadingCard('Reading the community…')); return; }
    if (view.err) {
      host.appendChild(h('div', { class: 'asc-card' }, h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-inline-error' }, view.err))));
      return;
    }
    const d = view.data || {};
    host.appendChild(headerCard(ctx, d));
    host.appendChild(unansweredCard(ctx, d));
    host.appendChild(channelsCard(ctx, d));
    host.appendChild(recentCard(ctx, d));
    if ((d.rooms || []).length) host.appendChild(roomsCard(ctx, d));
  }

  /* The four numbers, and the way out to the composer.
   *
   * "Voices" rather than "active members": it is the count of people who
   * actually posted, and calling that active would flatter a week in which one
   * person wrote forty messages. */
  function headerCard(ctx, d) {
    const { h } = ctx;
    const t = d.totals || {};
    const days = d.window_days || 7;
    const tile = (n, label, sub) => h('div', { class: 'asc-comm-tile' },
      h('div', { class: 'asc-comm-tile-n doto' }, String(n == null ? 0 : n)),
      h('div', { class: 'asc-comm-tile-label chrome' }, label),
      sub ? h('div', { class: 'asc-comm-tile-sub' }, sub) : null);
    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' },
        h('div', {},
          h('div', { class: 'asc-card-title' }, 'The last ' + days + ' days'),
          h('div', { class: 'asc-card-sub' },
            'Counted from the community tables, not summarised. Open any room to '
            + 'check a number.')),
        h('a', { class: 'asc-btn asc-btn-primary asc-btn-sm', href: COMMUNITY_URL },
          'Open the community')),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-comm-tiles' },
          tile(t.posts, 'Posts'),
          tile(t.replies, 'Replies'),
          tile(t.voices, 'Voices', 'people who posted'),
          tile(t.reactions, 'Reactions'),
          tile(t.members, 'Members'),
          tile(t.case_rooms, 'Case rooms'))));
  }

  /* The only list here that is a job rather than a statistic. A member asked
   * something and nobody answered; the community is a promise we made to
   * physicians, and an unanswered question is that promise going unkept in
   * public. Reactions deliberately do not clear a row. */
  function unansweredCard(ctx, d) {
    const { h } = ctx;
    const rows = d.unanswered || [];
    const body = h('div', { class: 'asc-card-pad' });
    if (!rows.length) {
      body.appendChild(h('div', { class: 'asc-empty-line' },
        'Every question in the last month has a reply under it.'));
    } else {
      body.appendChild(h('div', { class: 'asc-comm-qs' }, rows.map((m) => h('a', {
        class: 'asc-comm-q', href: COMMUNITY_URL + '#' + (m.channel_slug || ''),
      },
        h('div', { class: 'asc-comm-q-body' }, m.body),
        h('div', { class: 'asc-comm-q-meta chrome' },
          (m.author || '') + ' · #' + (m.channel_slug || '') + ' · ' + ctx.fmtDate(m.created_at))))));
    }
    return h('div', { class: 'asc-card asc-comm-unanswered' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' },
          'Unanswered questions', rows.length
            ? h('span', { class: 'asc-badge asc-badge-count asc-comm-count' }, String(rows.length))
            : null),
        h('div', { class: 'asc-card-sub' },
          'Top-level posts asking something, with no reply under them. A reaction '
          + 'is not an answer.'))),
      body);
  }

  function channelsCard(ctx, d) {
    const { h } = ctx;
    const rows = d.channels || [];
    const busiest = Math.max(1, ...rows.map((c) => c.posts || 0));
    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Where the talking happened'),
        h('div', { class: 'asc-card-sub' },
          'A room that is silent for a week is not necessarily broken; a room '
          + 'that is silent every week is.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-comm-chans' }, rows.map((c) => h('div', {
          class: 'asc-comm-chan' + ((c.posts || 0) ? '' : ' is-quiet'),
        },
          h('div', { class: 'asc-comm-chan-name' }, '#' + c.slug),
          h('div', { class: 'asc-comm-chan-bar' },
            h('span', {
              class: 'asc-comm-chan-fill',
              style: 'width:' + Math.round(((c.posts || 0) / busiest) * 100) + '%',
            })),
          h('div', { class: 'asc-comm-chan-n' },
            (c.posts || 0) + (c.posts === 1 ? ' post' : ' posts')),
          h('div', { class: 'asc-comm-chan-sub chrome' },
            (c.voices || 0) + ' ' + ((c.voices === 1) ? 'voice' : 'voices')))))));
  }

  function recentCard(ctx, d) {
    const { h } = ctx;
    const rows = d.recent || [];
    const body = h('div', { class: 'asc-card-pad' });
    if (!rows.length) {
      body.appendChild(h('div', { class: 'asc-empty-line' }, 'Nothing has been posted yet.'));
    } else {
      body.appendChild(h('div', { class: 'asc-comm-feed' }, rows.map((m) => h('div', {
        class: 'asc-comm-post',
      },
        h('div', { class: 'asc-comm-post-meta chrome' },
          (m.author || '') + ' · #' + (m.channel_slug || '')
          + (m.is_reply ? ' · reply' : '') + ' · ' + ctx.fmtDate(m.created_at)),
        h('div', { class: 'asc-comm-post-body' }, m.body)))));
    }
    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Latest posts'),
        h('div', { class: 'asc-card-sub' },
          'Public channels only. A case room is a private conversation about one '
          + 'patient record and does not belong in a feed.'))),
      body);
  }

  function roomsCard(ctx, d) {
    const { h } = ctx;
    return h('div', { class: 'asc-card' },
      h('div', { class: 'asc-card-head' }, h('div', {},
        h('div', { class: 'asc-card-title' }, 'Case rooms'),
        h('div', { class: 'asc-card-sub' },
          'One room per routed case, opened automatically when a case goes to '
          + 'more than one physician.'))),
      h('div', { class: 'asc-card-pad' },
        h('div', { class: 'asc-comm-rooms' }, (d.rooms || []).map((r) => h('div', {
          class: 'asc-comm-room',
        },
          h('div', { class: 'asc-comm-room-title' }, r.title || 'Case room'),
          h('div', { class: 'asc-comm-room-meta chrome' },
            r.participants + (r.participants === 1 ? ' participant' : ' participants')
            + ' · opened ' + ctx.fmtDate(r.created_at)))))));
  }

  window.AdminCommunitySection = {
    render: render,
    reset() { view = { data: null, err: null, busy: false }; },
  };
})();
