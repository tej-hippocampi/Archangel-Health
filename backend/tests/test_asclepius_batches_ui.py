"""The Batches admin surface — structural assertions on the shipped client.

The rules worth pinning here are all rules about what this client must NOT do,
which is exactly the class a DOM-free check can carry: you cannot observe "the
future never reached the browser" without a browser, but you can observe that the
code which would have put it there does not exist.

Three of them matter.

  1. **The client owns no sequence authority.** It computes the implied
     predecessor set for DISPLAY — so an admin sees the real size of what they are
     about to send — and the server re-derives it and refuses a payload that omits
     any of it. That division is the whole design: a bug in this file costs an
     error message, never a stranded assignment. So the client's arithmetic must
     stay advisory, and the send must go through the endpoint that re-checks it.

  2. **The preview must not reach past the served payload.** The endpoint returns
     what ``_blind_task`` returns; the renderer must draw that and nothing else. A
     renderer that fetched the parent chart to show admin "more context" would put
     a physician's sealed future on an admin screen, and a screenshot in a Slack
     thread leaks it exactly as permanently as serving it would.

  3. **Send-to-all on a longitudinal batch says what it does.** It un-seals the
     walk. That is a legitimate choice and it must be stated before the click,
     not discovered after it.

Every CSS class the new surface emits is also asserted to have a rule behind it,
because a class with no style is invisible in review and invisible on screen.
"""
from __future__ import annotations

import pathlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
JS = (_FRONTEND / "asclepius.js").read_text()
# Task Routing moved to the console's own bundle with PRD-F. It is the same
# renderer; only the file it is read from changed.
ADMIN_JS = (_FRONTEND / "admin_shell.js").read_text()
PHYS_JS = (_FRONTEND / "admin_physicians.js").read_text()
CSS = "\n".join((_FRONTEND / f).read_text()
                for f in ("asclepius.css", "admin.css", "_base.css", "_tokens.css"))


def _fn(src: str, name: str) -> str:
    """The body of one top-level function, by brace matching."""
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unterminated {name}")


BATCHES = _fn(ADMIN_JS, "renderAdminBatches")
PREVIEW_PANEL = _fn(ADMIN_JS, "renderCasePanelReadOnly")


# ═══════════════════════════════════════════════════════════════════════════════
# The surface exists and replaced the old one
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_subnav_says_task_routing_and_routes_to_the_batch_flow():
    """PRD ADMIN-TASKS §2 renamed the LABEL and deliberately kept the STATE KEY.

    'assign' is read by the deep-link aliases, ``openBatchesFor`` and the
    physician-row route-in; renaming it would be silent breakage for zero
    benefit. So the assertion is: the operator sees "Task Routing", the code
    still says 'assign'."""
    assert "['assign', 'Task Routing']" in ADMIN_JS
    assert "['tasks', 'Data & Task Creation']" in ADMIN_JS
    assert "renderAdminBatches(inner)" in ADMIN_JS
    assert "state.adminSub.work === 'assign'" in ADMIN_JS


def test_the_three_classes_are_the_ones_the_backend_groups_by():
    for key in ("longitudinal", "real_static", "synthetic"):
        assert f"{key}:" in ADMIN_JS.split("const BATCH_META")[1][:400], key


# ═══════════════════════════════════════════════════════════════════════════════
# 1. The client holds no sequence authority
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_send_goes_through_the_endpoint_that_re_derives_predecessors():
    """If the client posted assignments directly, its own arithmetic would become
    the authority and a bug here would write unservable rows."""
    assert "'/admin/assignments/allocate'" in BATCHES
    assert "upsert_assignment" not in BATCHES


def test_the_implied_set_is_resolved_by_the_server_not_computed_here():
    """The admin screen needs to say "+5 earlier points included" before the
    commit, and the obvious implementation is a loop over sequence_index in this
    file. It is not done that way, and the reason outlives this screen: a client
    that knows how to order a walk is one somebody later trusts to enforce the
    order, and the seal is then one hand-typed task id from being defeated.

    ``test_the_client_never_enforces_the_sequence_itself`` scans the WHOLE shipped
    file, so the doctor-facing invariant and this admin surface are held to one
    standard — which is how it should be, and how it caught the first draft of
    this screen."""
    assert "resolveSelection" in BATCHES
    assert "'/admin/batches/resolve-selection'" in BATCHES
    assert "view.resolved.n_added" in BATCHES, "the count shown is the server's"
    assert "required earlier point(s) included" in BATCHES


def test_the_count_shown_and_the_set_sent_are_the_same_derivation():
    """A screen that previewed "+5" and then posted a different set would be
    lying at the only moment the operator can still object."""
    assert "view.resolved && view.resolved.task_ids" in BATCHES


def test_the_client_never_decides_whether_a_point_may_be_OPENED():
    """The doctor-facing seal is untouched by this screen. A comparison that
    gated opening would be a second, weaker copy of the server's gate."""
    for forbidden in ("canOpen", "isServable", "unlocked", "mayOpen"):
        assert forbidden not in BATCHES, forbidden


def test_the_missing_predecessor_refusal_is_shown_with_its_points():
    """The server names which points are missing. Rendering a bare 400 would make
    the admin diff two lists by hand."""
    assert "missing_trajectory_predecessors" in BATCHES
    assert "d.missing" in BATCHES


def test_the_v4_wall_refusal_is_surfaced_by_name():
    assert "not_approved_for_real_data" in BATCHES


# ═══════════════════════════════════════════════════════════════════════════════
# 2. The preview draws the served payload and nothing else
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_preview_reads_only_the_task_it_was_given():
    """The renderer's only source is ``task.case`` — the payload the endpoint
    built with the doctor's own function. Any second fetch here would be a route
    around the truncation."""
    assert "task && task.case" in PREVIEW_PANEL
    assert "api(" not in PREVIEW_PANEL, (
        "the preview renderer must not fetch anything of its own — the payload "
        "it is handed is the whole permitted view")
    for leak in ("trajectory_points", "outcome", "held_out", "ground_truth",
                 "reveal", "self_score"):
        assert leak not in PREVIEW_PANEL, f"preview renderer reaches for {leak}"


def test_the_preview_endpoint_is_the_admin_batch_one():
    assert "'/admin/batches/preview/'" in BATCHES


def test_the_preview_labels_itself_read_only_and_names_the_truncation():
    assert "res.eyebrow" in BATCHES
    assert "truncated here exactly as the physician sees it" in BATCHES


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Send-to-all states what it does before the click
# ═══════════════════════════════════════════════════════════════════════════════
def test_sending_a_walk_to_all_warns_that_it_un_seals_it():
    assert "enter the open queue" in BATCHES
    # Gated on BOTH conditions — the warning is false on a synthetic batch.
    assert "view.mode === 'all' && view.batch === 'longitudinal'" in BATCHES


def test_the_send_controls_only_exist_when_something_is_selected():
    """§4.3 turned the send BAR into a context-sensitive PANEL, so the spelling
    changed; the property did not. With an empty selection the panel returns a
    one-line hint before constructing any control — no targeting select, no role
    radios, no Send button — because controls that cannot act are the "fluff"
    this re-cut exists to remove."""
    assert "if (!chosen.length) {" in BATCHES
    hint_at = BATCHES.index("asc-route-panel-hint")
    for control in ("'Preview send'", "asc-route-role", "flatControls()", "walkControls()"):
        assert BATCHES.index(control) > hint_at, (
            f"{control} is built before the empty-selection early return")


# ═══════════════════════════════════════════════════════════════════════════════
# §2.5 — one flow, entered from either end
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_physician_row_deep_links_into_batches_rather_than_sending():
    """Routing from a physician's row must not become a second send path. It
    pre-selects them in Batches and stops there."""
    assert "openBatchesFor" in ADMIN_JS and "openBatchesFor" in PHYS_JS
    assert "'Route cases'" in PHYS_JS
    row = PHYS_JS[PHYS_JS.index("Route cases") - 600:PHYS_JS.index("Route cases") + 400]
    assert "assignments/allocate" not in row, "the row must not send anything itself"
    assert "approved && ctx.openBatchesFor" in PHYS_JS, (
        "offered only for approved accounts — the V4 wall refuses the rest at "
        "send, and a button that always fails is worse than no button")


# ═══════════════════════════════════════════════════════════════════════════════
# Every class emitted has a rule behind it
# ═══════════════════════════════════════════════════════════════════════════════
def test_every_new_class_has_a_style():
    """A class with no rule is invisible in review and invisible on screen. The
    repo has shipped two paint-only defects through a green suite; this is the
    cheap half of preventing a third."""
    emitted = set(re.findall(r"class: '([^']+)'", BATCHES + PREVIEW_PANEL))
    names = {c for blob in emitted for c in blob.split() if c.startswith("asc-")}
    missing = [c for c in sorted(names) if f".{c}" not in CSS]
    assert not missing, f"classes with no CSS rule: {missing}"


# ═══════════════════════════════════════════════════════════════════════════════
# §8.4 — the handoff renders the commitment and cannot reach for the outcome
# ═══════════════════════════════════════════════════════════════════════════════
HANDOFF = _fn(JS, "renderRelayHandoff")


def test_the_handoff_renders_only_what_the_server_sent():
    """The predecessor's outcome is what THIS physician is being asked to predict.
    The server does not send it; this function must never grow a fetch that goes
    looking for it."""
    assert "state.task && state.task.relay_handoff" in HANDOFF
    assert "api(" not in HANDOFF
    for leak in ("outcome", "self_score", "self-score", "revealed", "trajectory-outcome"):
        assert leak not in HANDOFF, f"the handoff renderer reaches for {leak}"


def test_the_handoff_is_absent_rather_than_empty_when_there_is_none():
    """Point 0, a solo walk and an ordinary case all render NOTHING — not an empty
    box captioned "Handoff", which reads as a missing colleague."""
    assert "if (!ho) return null;" in HANDOFF


def test_the_handoff_sits_above_the_clinical_question():
    """Context read before deciding, exactly as a verbal handoff precedes the
    round. Below the question it would be read after the physician had already
    committed to a line."""
    ws = _fn(JS, "renderTaskWorkspace")
    assert ws.index("renderRelayHandoff()") < ws.index("'Clinical question'")


def test_the_handoff_is_visually_separated_from_the_case():
    """A physician must never be unsure which words are the chart and which are a
    colleague's read of it."""
    assert ".asc-handoff" in CSS and "border-left" in CSS


# ═══════════════════════════════════════════════════════════════════════════════
# §8.3 / §8.7 — relay send and the chain, in the admin client
# ═══════════════════════════════════════════════════════════════════════════════
def test_relay_is_offered_only_for_one_whole_trajectory():
    """A relay is defined over a walk. Half a chart split between five doctors is
    neither a solo walk nor a handoff chain, and the server refuses it anyway."""
    assert "wholeWalk" in BATCHES
    assert "chosen.length === chosenWalks[walkKeys[0]]" in BATCHES
    assert "Send as relay" in BATCHES


def test_the_relay_seed_is_held_so_preview_equals_commit():
    """Reshuffling picks a NEW seed deliberately; committing must reuse the one
    that produced the mapping on screen."""
    assert "view.relaySeed" in BATCHES
    assert "seed: view.relaySeed" in BATCHES
    assert "view.relaySeed = Math.floor" in BATCHES


def test_the_chain_marks_only_the_waiting_point_and_flags_a_late_one():
    assert "'waiting'" in BATCHES and "is-late" in BATCHES
    assert "waiting_hours || 0) >= 24" in BATCHES


def test_the_late_marker_is_not_colour_alone():
    """The one cell on the screen that has to be found without reading."""
    late = CSS[CSS.index(".asc-chain-cell.is-late"):][:260]
    assert "border-width" in late and "font-weight" in late


def test_the_chain_loads_for_a_walk_that_is_already_out():
    """A stalled walk should be visible on the screen an admin is already looking
    at, not only to somebody who thinks to go looking for it."""
    assert "loadChain" in BATCHES and "'/admin/batches/relay/'" in BATCHES


# ═══════════════════════════════════════════════════════════════════════════════
# The send controls say why they cannot act
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_send_button_states_the_step_it_is_waiting_on():
    """WHY: "the Send button isn't working" was literally true, and silent.

    The gate is right and is kept -- nothing goes to a physician that nobody
    here has read. What was wrong is that it rendered ``disabled: seen ? null :
    ''``, and ``h()`` skips a falsy ``disabled``, so the attribute never landed:
    the button looked live, took the click, and the handler's own ``if (seen)``
    dropped it on the floor. One paragraph of prose further down the panel was
    the only clue.

    A control that cannot act must either look inert or do the thing that puts
    it back in action. This does the second.
    """
    assert "seen ? 'Send' : 'Preview a case first'" in BATCHES
    # The button's own attribute object, not the comment that explains it.
    start = BATCHES.index("'asc-btn asc-btn-primary' + (seen")
    attrs = BATCHES[start:BATCHES.index("'Preview a case first'", start)]
    assert "disabled" not in attrs, (
        "the attribute that never landed is gone, not restored")
    assert "asc-btn-blocked" in attrs and ".asc-btn.asc-btn-blocked" in CSS


def test_pressing_the_blocked_send_opens_the_preview_it_is_waiting_for():
    """The click does the operator's next step rather than nothing. It previews
    the FIRST selected case, which is also what the gate accepts: at least one,
    not all -- an admin sending a thirteen-point walk should not have to open
    thirteen cases."""
    go = BATCHES[BATCHES.index("seen ? 'Send' : 'Preview a case first'"):][:600]
    assert "preview(chosen[0])" in go
    assert "if (seen) { send(false); return; }" in go


def test_the_gate_itself_is_untouched():
    """The point of the change is legibility, not permission. Send still refuses
    to send until a human has opened a case in THIS session."""
    assert "const seen = chosen.some(function (id) { return view.previewed[id]; });" in BATCHES
    assert "Nothing goes to a physician that nobody" in BATCHES


# ═══════════════════════════════════════════════════════════════════════════════
# A role the account cannot take is not offered
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_reviewer_radio_is_disabled_without_the_tier():
    """WHY: naming a labeler as Reviewer 400s the WHOLE send atomically at the
    server's ``not_a_reviewer`` guard -- not that one name, all of it. The row
    now carries the eligibility and the radio cannot be clicked, so the refusal
    happens where the admin can still fix it."""
    picker = _fn(ADMIN_JS, "doctorPicker")
    assert "const noReviewTier = d.can_review === false;" in picker
    assert "const off = role === 'review' && noReviewTier;" in picker
    assert "disabled: off ? true : null," in picker


def test_the_reason_is_on_the_row_not_only_in_a_tooltip():
    """A greyed control with the reason in a ``title`` is a reason nobody reads.
    It has to survive a screenshot."""
    picker = _fn(ADMIN_JS, "doctorPicker")
    assert "'no reviewer tier'" in picker
    assert "asc-route-why" in picker
    assert ".asc-route-role.is-off" in CSS


def test_the_row_shows_eligibility_where_there_is_no_specialty():
    """The label used to read ``name · specialty`` and print a bare dash for the
    staff accounts that have no specialty, spending the column on nothing."""
    picker = _fn(ADMIN_JS, "doctorPicker")
    assert "(d.specialty || eligibility || 'no specialty on file')" in picker
    assert "' · ' + (d.specialty || '—')" not in picker


def test_the_roster_fetch_carries_the_review_eligibility():
    """Both send-time refusals are now visible in the picker.
    ``real_data_approved`` was already filtered on; ``can_review`` was not, and
    it is the same class of fact."""
    loader = _fn(ADMIN_JS, "loadDoctors")
    assert "'/admin/physicians'" in loader
    assert "d.real_data_approved" in loader
    assert "can_review" in loader
    assert "d.can_review === undefined || d.can_review === null" in loader, (
        "an older server that does not send the field must read as unknown, "
        "not as no -- disabling every Reviewer radio would be worse")


# ═══════════════════════════════════════════════════════════════════════════════
# Every refusal the server can produce is decoded
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_decoder_handles_the_pydantic_422_shape():
    """``_roles_are_a_known_vocabulary`` and ``_one_targeting_mode`` are model
    validators, so their refusals arrive as ``{"detail": [{loc, msg, type}]}`` --
    a different SHAPE, not a different error code. Both were written to explain
    themselves, and both were being flattened to "Send failed."."""
    send = _fn(ADMIN_JS, "send")
    assert "Array.isArray(d)" in send
    assert "item.msg" in send
    assert "Value error" in send, "the pydantic prefix is stripped, not shown"


def test_an_unrecognised_named_refusal_prints_the_servers_own_message():
    """Every structured refusal here carries a ``message`` written for an
    operator. Swallowing it to say "Send failed." is the product knowing exactly
    what is wrong and declining to say."""
    send = _fn(ADMIN_JS, "send")
    assert "} else if (d && d.error) {" in send
    assert "view.err = d.message ||" in send


# ═══════════════════════════════════════════════════════════════════════════════
# What actually went out
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_panel_reports_what_the_send_delivered():
    """``notified`` has always been in the allocate response and was never
    rendered. ``notify_routed`` swallows its own failures by design -- a
    community outage must not roll back routing the queue is already honouring --
    so counting what went out is the only way anybody learns it did not."""
    block = _fn(ADMIN_JS, "deliveryBlock")
    assert "view.sent.notified" in block
    assert "' DMs'" in block and "' case rooms'" in block
    assert "Nobody was notified" in block, "zero DMs must not look like success"
    assert "errs.map" in block, "server errors are printed verbatim"
    assert "view.sent = {" in BATCHES, "held past the selection reset that follows a send"
