"""What the reviewer sees of a label, declared once.

There were three independent definitions of "a label": ``SubmissionIn`` in
``schemas.py`` (the truth), a hand-maintained ten-string tuple in ``review.py``
(the server whitelist), and a run of hand-written ``if (a.X)`` branches in
``review.js`` (the client renderer). Nothing tied them together, and the
whitelist is deliberately fail-closed for privacy: a new identity field added
upstream stays invisible by default instead of leaking by default.

That is the right default for identity. It is also why the same mechanism
failed closed for PRODUCT, silently. Adding a question to the labeling flow
made it invisible to the reviewer with no error, no test failure and no log
line. Three consequences were live when this file was written:

* ``citations`` was a phantom key. It sat in the whitelist and was rendered by
  the client, and no submission has ever carried a top-level ``citations``.
  Citations live nested as ``evidence_anchor``/``evidence_anchors`` in six
  different places. A reviewer had never seen a single citation a labeler
  entered, while one of the four dimensions they grade is ``rubric_quality``
  and the premium export SKU is literally "grounded".
* The entire Model-Failure Taxonomy capture (``failure_tags``, ``severities``,
  ``error_tag_reasons``), a named export SKU, was whitelisted or nested and
  never rendered.
* ``prompt_review``, the Stage-1 clinician sign-off that upgrades a record's
  provenance, was never whitelisted at all.

So: one declaration, here. The server whitelist derives from it, the client
render order derives from it (served over ``/review/me``, the same
server-to-client vocabulary channel that has kept the four review dimensions
from ever drifting), and ``test_label_view.py`` walks ``SubmissionIn``
recursively and fails if a field is neither shown nor explicitly withheld with
a stated reason.

**Visibility is declared here; it is never enforced here.** Every value still
passes ``review._scrub_metadata`` and the Safe-Harbor identity walk on its way
out. This file decides what is *asked for*, not what is *allowed*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# ─── Display groups, in the order a reviewer reads them ──────────────────────
#
# Ordering is a product decision and it lives with the fields rather than in
# the client, so a new field arrives in the right place instead of at the
# bottom of whatever function rendered last.
GROUPS: Tuple[Tuple[str, str], ...] = (
    ("signoff", "The physician's read on the question"),
    ("blind", "Their answer before seeing the candidates"),
    ("answer", "The answer they stand behind"),
    ("rationale", "Why it is better"),
    ("critique", "What is wrong with the other one"),
    ("reasoning", "Step by step"),
    ("rubric", "How they would score it"),
)

#: How a value is drawn. The client maps these to renderers; a kind it does not
#: know is rendered as prose rather than dropped, so a new kind degrades to
#: readable instead of invisible.
RENDER_KINDS = (
    "prose",     # free text
    "chips",     # a list of controlled-vocabulary tokens
    "pairs",     # {key: value} shown as label/value chips
    "flag",      # a boolean worth stating only when true
    "eyebrow",   # a short qualifier shown above the value it qualifies
    "anchors",   # evidence anchors, rendered attached to the claim they support
    "steps",     # the reasoning-step table
    "rubric",    # the rubric table
)


@dataclass(frozen=True)
class Field:
    """One leaf of a submission, and what the reviewer does with it."""

    path: str
    visible: bool
    label: str = ""
    group: str = ""
    render: str = "prose"
    #: Required when ``visible`` is False. Stated so that withholding is a
    #: decision on the record rather than an omission nobody noticed.
    reason: str = ""

    @property
    def top(self) -> str:
        return self.path.split(".", 1)[0]


def _shown(path: str, label: str, group: str, render: str = "prose") -> Field:
    return Field(path=path, visible=True, label=label, group=group, render=render)


def _withheld(path: str, reason: str) -> Field:
    return Field(path=path, visible=False, reason=reason)


# ─── The map ─────────────────────────────────────────────────────────────────
FIELDS: Tuple[Field, ...] = (
    # Plumbing. Not judgments.
    _withheld("submission_id", "Plumbing, and a first/second tell in a pair."),
    _withheld("task_id", "The reviewer already has the task."),
    _withheld(
        "portal_version",
        "Which client captured it. A version tell, and not something a "
        "reviewer should weigh when judging the medicine.",
    ),
    _withheld(
        "assisted",
        "Practice-case bookkeeping: which tour steps the physician advanced "
        "with 'Skip this step'. It is read only by the practice case's own "
        "grader, is always empty on a real submission, and would be noise to a "
        "reviewer judging the medicine.",
    ),
    _withheld(
        "time_spent_sec",
        "How long the labeler took. Told to a reviewer it becomes a competence "
        "prior applied before they have read the answer, and in a pair it is a "
        "first/second tell. It reaches the quality metric and the admin "
        "console; it does not reach the person judging.",
    ),
    _withheld(
        "assist",
        "The model's pre-label suggestions. Showing a reviewer what the model "
        "proposed anchors them to it, which is the specific bias the blind "
        "capture upstream exists to avoid. It is audit provenance, not a "
        "judgment.",
    ),
    _withheld(
        "decisive_action",
        "Persisted onto the TASK, not the submission, and served in the task "
        "view. Declared here so the walk over SubmissionIn accounts for it.",
    ),

    # The verdict.
    _shown("verdict", "Their verdict", "answer", "eyebrow"),
    # Which candidate they picked. This IS the judgment, not a tell: the task
    # view already ships the candidates as {id, text}, the reviewer's A/B
    # presentation order is resolved per-reviewer server-side, and the client
    # resolves the final answer text through this id. Withholding it blanks the
    # answer the reviewer is there to grade.
    _shown("chosen_id", "The answer they picked", "answer", "eyebrow"),
    _shown("rejected_id", "The one they rejected", "critique", "eyebrow"),
    _shown("confidence", "How confident they were", "answer", "eyebrow"),

    # Stage 1: the sign-off on the question itself.
    _shown("prompt_review", "The physician's read on the question", "signoff", "prose"),
    _shown("prompt_review.reviewed", "Reviewed the question", "signoff", "flag"),
    _shown("prompt_review.verdict", "Verdict on the question", "signoff", "eyebrow"),
    _shown("prompt_review.note", "Note on the question", "signoff", "prose"),
    _withheld("prompt_review.reviewed_at", "A timestamp, so a first/second tell in a pair."),

    # Stage 2: the blind answer.
    _shown("independent_answer", "Their answer before the reveal", "blind", "prose"),
    _shown("independent_answer.text", "Their blind answer", "blind", "prose"),
    _shown(
        "independent_answer.kind", "Captured as", "blind", "eyebrow",
    ),
    _shown("independent_answer.evidence_anchors", "Sources", "blind", "anchors"),
    _shown("independent_answer.evidence_anchor", "Source", "blind", "anchors"),
    _withheld("independent_answer.captured_at",
              "The pre-reveal commit timestamp: a direct who-went-first tell."),
    _withheld("independent_answer.portal_version", "A version tell."),

    # Stage 3: the answer they stand behind.
    _shown("chosen_revision", "The answer they stand behind", "answer", "prose"),
    _shown("chosen_revision.edited", "They edited it", "answer", "flag"),
    _shown("chosen_revision.revised_text", "Their corrected answer", "answer", "prose"),
    _shown("chosen_revision.why_better_tags", "Why it is better", "rationale", "chips"),
    _shown("chosen_revision.why_better_notes", "In their words", "rationale", "prose"),
    _shown("chosen_revision.evidence_anchors", "Sources", "rationale", "anchors"),
    _shown("chosen_revision.evidence_anchor", "Source", "rationale", "anchors"),

    # The critique of the rejected answer.
    _shown("rejected_critique", "What is wrong with the other one", "critique", "prose"),
    _shown("rejected_critique.error_tags", "What is wrong with it", "critique", "chips"),
    _shown("rejected_critique.severities", "How bad", "critique", "pairs"),
    _shown("rejected_critique.error_tag_reasons", "Why", "critique", "pairs"),
    _shown("rejected_critique.why_worse", "In their words", "critique", "prose"),
    _shown("rejected_critique.error_tag_anchors", "Sources", "critique", "anchors"),
    _shown("rejected_critique.failure_tags", "How the model failed", "critique", "chips"),
    _shown("rejected_critique.failure_tags.mode", "Failure mode", "critique", "chips"),
    _shown("rejected_critique.failure_tags.note", "What went wrong", "critique", "prose"),
    _shown("rejected_critique.failure_tags.tier", "Criticality", "critique", "eyebrow"),
    _withheld("rejected_critique.failure_tags.evidence_step_id",
              "An internal slot id; the step it points at is rendered instead."),
    _withheld("rejected_critique.failure_tags.criterion_id",
              "An internal slot id; the criterion it points at is rendered instead."),

    # The ideal answer, when both candidates were inadequate.
    _shown("from_scratch", "The answer they wrote themselves", "answer", "prose"),
    _shown("from_scratch.reasoning_steps", "Their reasoning", "reasoning", "steps"),
    _shown("from_scratch.ideal_answer", "The answer they wrote", "answer", "prose"),
    _shown("from_scratch.approach_notes", "How they approached it", "rationale", "prose"),
    _shown("from_scratch.evidence_anchors", "Sources", "answer", "anchors"),
    _shown("from_scratch.evidence_anchor", "Source", "answer", "anchors"),

    # The reasoning trace. The step SHAPE is declared once under "@step"
    # because it hangs in two places (the top level and inside from_scratch),
    # and two copies would drift the moment a step field is added.
    _shown("reasoning_steps", "Their reasoning", "reasoning", "steps"),
    _shown("@step.step", "Step", "reasoning", "eyebrow"),
    _shown("@step.text", "The step they stand behind", "reasoning", "prose"),
    _shown("@step.original_text", "What the model said", "reasoning", "prose"),
    _shown("@step.corrected", "Corrected", "reasoning", "flag"),
    _shown("@step.confirmed", "Confirmed as written", "reasoning", "flag"),
    _shown("@step.added", "Added by the physician", "reasoning", "flag"),
    _shown("@step.correction_reason", "Why it was wrong", "reasoning", "chips"),
    _shown("@step.step_note", "In their words", "reasoning", "prose"),
    _shown("@step.step_error_tag", "Error", "reasoning", "chips"),
    _shown("@step.label", "Label", "reasoning", "eyebrow"),
    _shown("@step.critique", "What is off with it", "reasoning", "prose"),
    _shown("@step.evidence_anchors", "Sources", "reasoning", "anchors"),
    _shown("@step.evidence_anchor", "Source", "reasoning", "anchors"),
    _withheld("@step.step_reward", "An internal numeric weight, not a judgment."),
    _withheld("@step.suggested_label",
              "The model's suggested label. Shown to a reviewer it anchors them "
              "to the model's read of a step the physician already judged."),
    _withheld("@step.suggested_critique", "Same."),
    _withheld("@step.tag", "Superseded by ``label``; kept for old rows only."),

    # The rubric.
    _shown("rubric", "How they would score it", "rubric", "rubric"),
    _shown("rubric.text", "Criterion", "rubric", "prose"),
    _shown("rubric.points", "Weight", "rubric", "eyebrow"),
    _shown("rubric.axes", "Axes", "rubric", "chips"),
    _shown("rubric.tier", "Criticality", "rubric", "eyebrow"),
    _shown("rubric.critical", "Critical", "rubric", "flag"),
    _shown("rubric.specific", "Machine-checkable", "rubric", "flag"),
    _shown("rubric.evidence_anchor", "Source", "rubric", "anchors"),
    _withheld("rubric.axis", "The deprecated single-value mirror of ``axes[0]``."),
    _withheld("rubric.source",
              "How the criterion was seeded (e.g. ``error_tag:dosing_error``). "
              "Buyer-facing provenance; it tells the reviewer nothing about "
              "whether the criterion is right."),

    # The shape of ONE evidence anchor, wherever it hangs. Prefixed with "@"
    # because it is a shared nested type rather than a field of SubmissionIn:
    # anchors appear under six different parents, and declaring them per-parent
    # would be six copies to keep in step. The prefix keeps it out of the
    # derived top-level whitelist.
    _shown("@anchor.citation_text", "Citation", "rubric", "prose"),
    _shown("@anchor.identifier", "Identifier", "rubric", "eyebrow"),
    _shown("@anchor.source_type", "Kind of source", "rubric", "eyebrow"),
    _shown("@anchor.url", "Link", "rubric", "prose"),
    _shown("@anchor.citation_confirmed", "Confirmed from the library", "rubric", "flag"),
    _withheld("@anchor.entry_method",
              "How it was typed in. Capture provenance, not evidence."),
)

#: Path prefix for shared nested types (currently only the evidence anchor).
#: A path starting with this describes a SHAPE, not a field of SubmissionIn, so
#: it never contributes a top-level whitelist key.
SHARED_PREFIX = "@"


_BY_PATH: Dict[str, Field] = {f.path: f for f in FIELDS}


def declared_paths() -> set:
    return set(_BY_PATH)


def get(path: str) -> Optional[Field]:
    return _BY_PATH.get(path)


def is_visible(path: str) -> bool:
    f = _BY_PATH.get(path)
    return bool(f and f.visible)


def submission_view_keys() -> Tuple[str, ...]:
    """The server whitelist, derived.

    A top-level key is served when ANY leaf under it is visible. Nested
    withholding is done by ``prune`` rather than by dropping the parent, so a
    field like ``independent_answer`` can ship its text while withholding its
    pre-reveal timestamp.
    """
    tops: List[str] = []
    for f in FIELDS:
        if f.path.startswith(SHARED_PREFIX):
            continue
        if f.visible and f.top not in tops:
            tops.append(f.top)
    return tuple(tops)


def withheld_paths_under(top: str) -> Tuple[str, ...]:
    """Leaf paths under ``top`` that must be pruned from a served value."""
    prefix = f"{top}."
    return tuple(
        f.path[len(prefix):] for f in FIELDS
        if not f.visible and f.path.startswith(prefix)
    )


#: Leaf NAMES (not paths) that are withheld wherever they appear. Used by the
#: pruner, which walks a served structure without knowing which schema class it
#: came from: ``reasoning_steps`` appear both at the top level and nested inside
#: ``from_scratch``, and the same leaf must be withheld in both.
def withheld_leaf_names() -> frozenset:
    names = set()
    for f in FIELDS:
        if not f.visible and "." in f.path:
            names.add(f.path.rsplit(".", 1)[1])
    return frozenset(names)


def prune(value: Any, *, withheld: Optional[frozenset] = None) -> Any:
    """Drop withheld leaves from anywhere inside a served value.

    Name-based rather than path-based, for the reason in
    ``withheld_leaf_names``. The name set is derived from the map, so adding a
    ``_withheld`` entry is the only action needed to stop serving something.
    """
    names = withheld_leaf_names() if withheld is None else withheld
    if isinstance(value, dict):
        return {k: prune(v, withheld=names) for k, v in value.items()
                if not (isinstance(k, str) and k in names)}
    if isinstance(value, list):
        return [prune(v, withheld=names) for v in value]
    return value


def render_spec() -> List[Dict[str, Any]]:
    """What ships to the client over ``/review/me``.

    The client renders from this rather than from its own hand-written
    branches, so a field added to the map appears on the page without a
    matching JS edit, and cannot silently fail to.
    """
    order = {name: i for i, (name, _label) in enumerate(GROUPS)}
    rows = [
        {"path": f.path, "label": f.label, "group": f.group, "render": f.render}
        for f in FIELDS if f.visible
    ]
    rows.sort(key=lambda r: (order.get(r["group"], len(order)), r["path"]))
    return rows


def group_labels() -> List[Dict[str, str]]:
    return [{"key": key, "label": label} for key, label in GROUPS]
