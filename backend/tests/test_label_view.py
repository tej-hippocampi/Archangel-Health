"""Adding a labeling question can no longer make it invisible to the reviewer.

There were three independent definitions of "a label" and nothing tied them
together. The whitelist in ``review.py`` is fail-closed, which is right for
identity and is exactly why it failed closed for PRODUCT without anyone
noticing: a new field arrived invisible, with no error, no test failure and no
log line.

The first test in this file is the whole point. It walks ``SubmissionIn``
recursively and asserts every leaf is either shown or explicitly withheld with
a stated reason. Add a field to the schema without touching ``label_view`` and
this goes red, naming the field.

The rest pin the three things that were actually broken when it was written:
the phantom ``citations`` key, the withheld-by-accident fields, and the fact
that nothing was checking any of it.
"""

from __future__ import annotations

from typing import get_args, get_origin

import pytest
from pydantic import BaseModel

from asclepius import label_view as lv
from asclepius import review
from asclepius.schemas import EvidenceAnchor, ReasoningStep, SubmissionIn


def _leaf_paths(model: type, prefix: str = "", seen: frozenset = frozenset()) -> list:
    """Every leaf field of a Pydantic model, as dotted paths.

    Recurses into nested models. ``EvidenceAnchor`` is collapsed onto the
    shared ``@anchor`` prefix, because it hangs under six different parents and
    declaring it per-parent would be six copies to keep in step.
    """
    out = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        inner = annotation
        # Unwrap Optional[...] / List[...] to find a nested model.
        for _ in range(3):
            args = [a for a in get_args(inner) if a is not type(None)]
            if get_origin(inner) is not None and args:
                inner = args[0]
            else:
                break
        path = f"{prefix}{name}"
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            if inner is EvidenceAnchor:
                out.append(path)  # the anchor itself is declared at its parent
                out.extend(f"@anchor.{k}" for k in EvidenceAnchor.model_fields)
                continue
            if inner is ReasoningStep:
                # Same shape under two parents; declared once under "@step".
                out.append(path)
                out.extend(_leaf_paths(ReasoningStep, "@step.", seen | {inner}))
                continue
            if inner in seen:
                continue
            out.append(path)
            out.extend(_leaf_paths(inner, f"{path}.", seen | {inner}))
            continue
        out.append(path)
    return out


# ─── The feedback loop ───────────────────────────────────────────────────────

def test_every_field_of_a_submission_is_either_shown_or_explicitly_withheld():
    """The test whose absence let a phantom key live in the whitelist.

    If this fails, add the named path to ``label_view.FIELDS``: ``_shown`` if a
    reviewer should see it, ``_withheld`` with a reason if not. Do not delete
    the assertion.
    """
    declared = lv.declared_paths()
    missing = sorted(set(_leaf_paths(SubmissionIn)) - declared)
    assert not missing, (
        "These submission fields are not declared in label_view.FIELDS, so the "
        "reviewer would silently never see them:\n  " + "\n  ".join(missing)
    )


def test_the_map_declares_nothing_that_is_not_a_real_field():
    """A path that no longer exists is a rule about nothing, and it is how the
    phantom ``citations`` key survived."""
    real = set(_leaf_paths(SubmissionIn))
    stale = sorted(
        p for p in lv.declared_paths()
        if p not in real and not p.startswith(lv.SHARED_PREFIX)
    )
    assert not stale, (
        "These paths are declared but are not fields of SubmissionIn:\n  "
        + "\n  ".join(stale)
    )


def test_withholding_something_requires_saying_why():
    """So that withholding is a decision on the record rather than an omission
    nobody noticed."""
    unexplained = [f.path for f in lv.FIELDS if not f.visible and not f.reason.strip()]
    assert unexplained == []


def test_every_shown_field_has_a_label_a_group_and_a_known_render_kind():
    for f in lv.FIELDS:
        if not f.visible:
            continue
        assert f.label, f.path
        assert f.group in {k for k, _ in lv.GROUPS}, f.path
        assert f.render in lv.RENDER_KINDS, f.path


# ─── The three live defects ──────────────────────────────────────────────────

def test_the_phantom_citations_key_is_gone():
    """It sat in the whitelist and was rendered by the client, and no
    submission has ever carried it. Citations live nested as evidence
    anchors."""
    assert "citations" not in lv.submission_view_keys()
    assert "citations" not in review._SUBMISSION_PAYLOAD_VIEW_KEYS


def test_evidence_anchors_are_actually_declared_somewhere():
    """The counterpart of the test above: killing the phantom key is only a fix
    if the real citations are surfaced."""
    anchor_paths = [f.path for f in lv.FIELDS if f.visible and f.render == "anchors"]
    assert len(anchor_paths) >= 5
    parents = {p.rsplit(".", 1)[0].lstrip("@") for p in anchor_paths}
    # The six places a labeler can attach a citation.
    assert "chosen_revision" in parents
    assert "from_scratch" in parents
    assert "independent_answer" in parents
    assert "step" in parents
    assert "rubric" in parents
    assert "rejected_critique" in parents


def test_the_failure_taxonomy_reaches_the_reviewer():
    """A named export SKU that was captured and never shown."""
    assert lv.is_visible("rejected_critique.failure_tags")
    assert lv.is_visible("rejected_critique.severities")
    assert lv.is_visible("rejected_critique.error_tag_reasons")


def test_the_stage_one_signoff_reaches_the_reviewer():
    """prompt_review was never whitelisted at all, though it upgrades a
    record's provenance from AI-drafted to clinician-reviewed."""
    assert "prompt_review" in lv.submission_view_keys()
    assert lv.is_visible("prompt_review.verdict")


def test_the_reviewer_can_tell_a_ten_second_stance_from_a_full_blind_answer():
    """Without ``kind`` the two render identically, and they are not the same
    piece of work."""
    assert lv.is_visible("independent_answer.kind")


def test_the_step_level_corrections_reach_the_reviewer():
    for leaf in ("original_text", "corrected", "confirmed", "added",
                 "correction_reason", "step_error_tag", "critique"):
        assert lv.is_visible(f"@step.{leaf}"), leaf


def test_the_rubric_axes_and_criticality_reach_the_reviewer():
    for leaf in ("axes", "tier", "critical", "specific"):
        assert lv.is_visible(f"rubric.{leaf}"), leaf


# ─── Anti-bias withholding stays withheld ────────────────────────────────────

def test_the_models_own_suggestions_never_reach_the_reviewer():
    """Showing a reviewer what the model proposed anchors them to it, which is
    the bias the blind capture upstream exists to avoid."""
    assert not lv.is_visible("assist")
    assert not lv.is_visible("@step.suggested_label")
    assert not lv.is_visible("@step.suggested_critique")


def test_ordering_tells_stay_withheld():
    """Blinding is the other half of this surface and it is not being traded
    away for richness."""
    assert not lv.is_visible("independent_answer.captured_at")
    assert not lv.is_visible("prompt_review.reviewed_at")
    assert not lv.is_visible("submission_id")
    assert not lv.is_visible("portal_version")


def test_how_long_they_took_is_not_shown_to_the_person_judging():
    """It becomes a competence prior applied before the reviewer has read the
    answer, and in a pair it is a first/second tell. It still reaches the
    quality metric and the admin console."""
    assert not lv.is_visible("time_spent_sec")


# ─── The pruner ──────────────────────────────────────────────────────────────

def test_pruning_drops_a_withheld_leaf_wherever_it_appears():
    """reasoning_steps appear at the top level AND nested inside from_scratch;
    the same leaf has to be withheld in both."""
    served = lv.prune({
        "reasoning_steps": [{"text": "keep", "suggested_label": "drop"}],
        "from_scratch": {
            "ideal_answer": "keep",
            "reasoning_steps": [{"text": "keep", "suggested_label": "drop"}],
        },
    })
    assert served["reasoning_steps"][0] == {"text": "keep"}
    assert served["from_scratch"]["reasoning_steps"][0] == {"text": "keep"}


def test_pruning_leaves_shown_leaves_alone():
    served = lv.prune({"chosen_revision": {"revised_text": "x", "why_better_tags": ["a"]}})
    assert served["chosen_revision"] == {"revised_text": "x", "why_better_tags": ["a"]}


# ─── The client contract ─────────────────────────────────────────────────────

def test_the_render_spec_is_ordered_by_how_a_reviewer_reads():
    spec = lv.render_spec()
    groups = [r["group"] for r in spec]
    order = [k for k, _ in lv.GROUPS]
    positions = [order.index(g) for g in groups]
    assert positions == sorted(positions)


def test_the_render_spec_never_ships_a_withheld_field():
    shipped = {r["path"] for r in lv.render_spec()}
    withheld = {f.path for f in lv.FIELDS if not f.visible}
    assert not (shipped & withheld)


def test_the_server_whitelist_is_derived_from_the_map_not_hand_written():
    assert review._SUBMISSION_PAYLOAD_VIEW_KEYS == lv.submission_view_keys()
