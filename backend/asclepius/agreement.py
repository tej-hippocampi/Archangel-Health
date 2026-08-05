"""Inter-annotator agreement (opt §1.3).

Buyers ask for it; the industry threshold is **Cohen's κ > 0.7** (substantial
agreement). This module is pure math (no I/O):

  * ``cohens_kappa(pairs)``  — Cohen's κ on the verdict over a list of rater-pair
    observations ``[(verdict_a, verdict_b), ...]`` drawn from the double-labeled
    subset of tasks. κ corrects observed agreement for chance.
  * ``jaccard(a, b)``        — set overlap on error-tag sets.
  * ``aggregate_kappa(observations)`` — overall κ plus a by-specialty breakdown,
    computed from the stored per-task agreement observations.

Cohen's κ is a *population* statistic across many items, so it is computed over
the full double-labeled subset (one observation per task), not per single task.
The pipeline stores one observation per double-labeled task; this module folds
them into the aggregate surfaced in ``quality_report.md``.
"""

from __future__ import annotations

import json
import os
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # pydantic is a hard dep; the model is optional sugar for the routing layer
    from pydantic import BaseModel, Field

    class AgreementObservation(BaseModel):
        """One double-labeled task's agreement observation (Buyer Response PRD §7 F1).

        ``blinded`` MUST be True to enter the κ computation — a second annotator shown
        the first's verdict produces agreement statistics that measure anchoring, not
        agreement."""

        task_id: str
        verdict_a: Optional[str] = None
        verdict_b: Optional[str] = None
        annotator_a_hashed: str = ""
        annotator_b_hashed: str = ""
        blinded: bool = False
        specialty: str = "unknown"
        error_tags_a: List[str] = Field(default_factory=list)
        error_tags_b: List[str] = Field(default_factory=list)
        resolution: Optional[str] = None
        resolved_by_hashed: Optional[str] = None
except Exception:  # pragma: no cover
    AgreementObservation = None  # type: ignore


def kappa_min_n() -> int:
    """Minimum double-labeled observations before κ is reportable (Buyer Response PRD
    §7 F2). Below this, κ is suppressed with a stated reason rather than emitting a
    bare null that reads as a broken pipeline."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_KAPPA_MIN_N", "30")))
    except ValueError:
        return 30


# The ONE definition of the double-label target (FIX A A-4.3). PRD A §1.3
# specifies 0.15; ``review.double_label_rate()`` delegates here rather than
# carrying a second default, so the two can never drift again.
DEFAULT_DOUBLE_LABEL_RATE = 0.15


def double_label_rate() -> float:
    """Target fraction of tasks routed to a second independent annotator (§7 F1).

    Single source of truth for ``ASCLEPIUS_DOUBLE_LABEL_RATE``. Two modules used
    to define a default for the same knob (0.20 here, 0.15 in the PRD that
    ``review.py`` implements) and silently disagreed about the target."""
    try:
        return float(os.getenv("ASCLEPIUS_DOUBLE_LABEL_RATE", str(DEFAULT_DOUBLE_LABEL_RATE)))
    except ValueError:
        return DEFAULT_DOUBLE_LABEL_RATE


def should_double_label(task: Dict[str, Any], *, current_rate: float,
                        specialty_n: Optional[int] = None) -> bool:
    """Route a second independent annotator to this task? (Buyer Response PRD §7 F1)

    Stratify, do not sample uniformly. A κ computed only on easy cases is meaningless
    and flattering: agreement is high where the answer is obvious. Always
    double-label the expensive-to-be-wrong records; then top up with random selection
    to reach the target rate.

    Always double-label:
      * declared_difficulty == 'frontier-hard'
      * V4 real_deid cases (the premium tier)
      * any case whose first annotator flagged low confidence
      * every case in a NEW specialty until 30 observations exist
    """
    t = task or {}
    case = t.get("case") or {}
    if (t.get("declared_difficulty") or case.get("declared_difficulty")) == "frontier-hard":
        return True
    if t.get("case_source") == "real_deid" or case.get("case_source") == "real_deid":
        return True
    if str(t.get("first_annotator_confidence") or "").lower() == "low":
        return True
    if specialty_n is not None and specialty_n < 30:
        return True
    # Top up with random selection to reach the target rate.
    return current_rate < double_label_rate()


def _blinded_only(observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only observations EXPLICITLY recorded as blinded (Audit §H2). A
    missing/NULL flag is a legacy row whose blinding was never verified — EXCLUDED,
    not assumed blinded. κ is the number a buyer audits; a metric that quietly absorbs
    unverifiable observations is worth less than one reporting a smaller honest n.
    Matches ``True`` and the SQLite int ``1`` only — never a NULL or a falsy 0."""
    return [o for o in observations if o.get("blinded") in (True, 1)]


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    """Jaccard similarity of two tag sets. Empty ∩ empty == 1.0 (perfect agree)."""
    sa, sb = set(a or []), set(b or [])
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return round(len(sa & sb) / len(union), 4)


def cohens_kappa(pairs: Sequence[Tuple[Optional[str], Optional[str]]]) -> Optional[float]:
    """Cohen's κ on a list of (rater_a_label, rater_b_label) observations.

    Returns ``None`` when there are no usable observations. When both raters are
    perfectly consistent and use only one category, κ is conventionally 1.0
    (no disagreement); we return 1.0 in that degenerate case rather than the
    undefined 0/0.
    """
    obs = [(a, b) for (a, b) in pairs if a is not None and b is not None]
    n = len(obs)
    if n == 0:
        return None

    categories = sorted({c for pair in obs for c in pair})
    # Observed agreement.
    agree = sum(1 for a, b in obs if a == b)
    po = agree / n

    # Expected agreement by chance from each rater's marginal distribution.
    count_a: Dict[str, int] = {c: 0 for c in categories}
    count_b: Dict[str, int] = {c: 0 for c in categories}
    for a, b in obs:
        count_a[a] += 1
        count_b[b] += 1
    pe = sum((count_a[c] / n) * (count_b[c] / n) for c in categories)

    if abs(1.0 - pe) < 1e-12:
        # Chance agreement is total (e.g. single category) — κ undefined; if the
        # raters never disagreed treat as perfect agreement, else 0.0.
        return 1.0 if po >= 1.0 else 0.0
    return round((po - pe) / (1.0 - pe), 4)


def _bootstrap_ci(pairs: Sequence[Tuple[Optional[str], Optional[str]]], *,
                  seed: int = 7, resamples: int = 1000,
                  z_pct: Tuple[float, float] = (2.5, 97.5)) -> Optional[Tuple[float, float]]:
    """Percentile bootstrap CI for κ over the observations (Buyer Response PRD §7 F2).
    Seeded (matching the eval's seed=7 convention) so the interval is reproducible.
    Returns None when there are too few usable observations to resample."""
    usable = [p for p in pairs if p[0] is not None and p[1] is not None]
    if len(usable) < 2:
        return None
    rng = random.Random(seed)
    n = len(usable)
    estimates: List[float] = []
    for _ in range(resamples):
        sample = [usable[rng.randrange(n)] for _ in range(n)]
        k = cohens_kappa(sample)
        if k is not None:
            estimates.append(k)
    if not estimates:
        return None
    estimates.sort()

    def _pct(p: float) -> float:
        idx = min(len(estimates) - 1, max(0, int(round(p / 100.0 * (len(estimates) - 1)))))
        return round(estimates[idx], 4)

    return (_pct(z_pct[0]), _pct(z_pct[1]))


def aggregate_kappa(observations: List[Dict[str, Any]], *,
                    min_n: Optional[int] = None) -> Dict[str, Any]:
    """Fold stored per-task agreement observations into aggregate κ, honestly
    (Buyer Response PRD §7 F2).

    * Only BLINDED observations enter the computation (§7 F1).
    * Below ``min_n`` (default 30) usable observations, ``overall`` is ``None`` WITH a
      stated ``reason`` string rather than a bare null — a null with no explanation
      reads as a broken pipeline; a null with a stated threshold reads as discipline.
    * A seeded bootstrap CI is reported when reportable, so a point estimate from a
      handful of observations does not hide its own uncertainty.
    * Per-specialty κ uses the same gate.

    Returns the existing keys (``overall``/``by_specialty``/``n``/
    ``observed_agreement``) plus ``reason``, ``ci``, and ``min_n``.
    """
    min_n = min_n or kappa_min_n()
    blinded = _blinded_only(observations)
    # Report the two exclusion reasons SEPARATELY (Audit §H2): an explicit unblinded
    # observation (measured anchoring) is a different, honest exclusion from a legacy
    # row whose blinding was never verified. Collapsing them hides how much of the
    # dropped n is unverifiable rather than genuinely unblinded.
    excluded_unblinded = sum(1 for o in observations if o.get("blinded") in (False, 0))
    excluded_unverified = sum(1 for o in observations if o.get("blinded") is None)
    pairs = [(o.get("verdict_a"), o.get("verdict_b")) for o in blinded]
    usable = [p for p in pairs if p[0] is not None and p[1] is not None]
    n = len(usable)

    reason: Optional[str] = None
    if n < min_n:
        overall = None
        ci = None
        reason = (f"only {n} double-labeled observations; kappa is not reportable "
                  f"below {min_n}")
    else:
        overall = cohens_kappa(pairs)
        ci = _bootstrap_ci(pairs)

    by_specialty: Dict[str, Optional[float]] = {}
    by_specialty_meta: Dict[str, Dict[str, Any]] = {}
    spec_groups: Dict[str, List[Tuple[Optional[str], Optional[str]]]] = {}
    for o in blinded:
        sp = o.get("specialty") or "unknown"
        spec_groups.setdefault(sp, []).append((o.get("verdict_a"), o.get("verdict_b")))
    for sp, ps in spec_groups.items():
        sp_usable = [p for p in ps if p[0] is not None and p[1] is not None]
        if len(sp_usable) < min_n:
            by_specialty[sp] = None
            by_specialty_meta[sp] = {
                "kappa": None, "n": len(sp_usable),
                "reason": f"only {len(sp_usable)} observations; below {min_n}"}
        else:
            by_specialty[sp] = cohens_kappa(ps)
            by_specialty_meta[sp] = {"kappa": by_specialty[sp], "n": len(sp_usable),
                                     "ci": _bootstrap_ci(ps)}

    observed = (
        round(sum(1 for a, b in usable if a == b) / len(usable), 4) if usable else None
    )
    return {
        "overall": overall,
        "kappa": overall,                    # alias (Buyer Response PRD §7 F2 shape)
        "reason": reason,
        "ci": ci,
        "min_n": min_n,
        "by_specialty": by_specialty,
        "by_specialty_meta": by_specialty_meta,
        "n": n,
        "excluded_unblinded": excluded_unblinded,
        "excluded_unverified": excluded_unverified,
        "observed_agreement": observed,
    }


def review_acceptance(reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Expert review outcome. NOT inter-rater reliability — the reviewer sees the
    labeler's answer, so the two observations are not independent and kappa does
    not apply (PRD A §0). Reported as its own metric with its own name; putting
    this number under a κ label would be claiming a statistic nobody measured.

    ``cannot_assess`` dimension states are counted separately, never folded into
    agreement or disagreement — forcing a binary there manufactures agreement
    (START_HERE §5 rule 4).

    THE SINGLE DEFINITION of expert acceptance in this product (context pack
    Seam 3). Two definitions used to ship in the same build reading the same
    table with the same word — this one, and an inline
    ``verdict IN ('accept','accept_with_edits')`` in the admin metrics — so the
    dashboard read ~97% while quality_report.md read ~84%. A combined figure is
    a legitimate thing to want, but it is a different number and must be labeled
    "not rejected", never "accepted". Do not fork this function; call it.

    Returns ``{n, accept_rate, edit_rate, reject_rate, by_dimension,
    n_cannot_assess}`` with None rates at n=0 (no reviews is not 0% accepted),
    plus ``n_unclassified`` / ``n_total``.

    ``n`` is the denominator the rates are actually computed over — reviews
    carrying a recognized verdict. A row with any other verdict used to shrink
    all three rates while appearing nowhere, so they silently failed to sum to 1
    (FIX A A-4.1). It is now excluded from the denominator and counted in
    ``n_unclassified``. With clean data the two are identical.
    """
    reviews = reviews or []
    n_total = len(reviews)
    verdicts = {"accept": 0, "accept_with_edits": 0, "reject": 0}
    by_dimension: Dict[str, Dict[str, int]] = {}
    n_cannot = 0
    for r in reviews:
        v = r.get("verdict")
        if v in verdicts:
            verdicts[v] += 1
        dims = r.get("dimensions")
        if not isinstance(dims, dict):
            try:
                dims = json.loads(r.get("dimension_json") or "{}") or {}
            except (TypeError, ValueError):
                dims = {}
        for key, state in dims.items():
            bucket = by_dimension.setdefault(
                key, {"agree": 0, "disagree": 0, "cannot_assess": 0})
            if state in bucket:
                bucket[state] += 1
            if state == "cannot_assess":
                n_cannot += 1
    n = sum(verdicts.values())          # classified reviews only — the honest denominator
    n_unclassified = n_total - n
    if n == 0:
        return {"n": 0, "accept_rate": None, "edit_rate": None, "reject_rate": None,
                "by_dimension": by_dimension, "n_cannot_assess": n_cannot,
                "n_unclassified": n_unclassified, "n_total": n_total}
    return {
        "n": n,
        "accept_rate": round(verdicts["accept"] / n, 4),
        "edit_rate": round(verdicts["accept_with_edits"] / n, 4),
        "reject_rate": round(verdicts["reject"] / n, 4),
        "by_dimension": by_dimension,
        "n_cannot_assess": n_cannot,
        # Reported, never absorbed: an unrecognized verdict must be visible
        # rather than quietly deflating all three rates.
        "n_unclassified": n_unclassified,
        "n_total": n_total,
    }


def independent_kappa(observations: List[Dict[str, Any]], *,
                      min_n: Optional[int] = None) -> Dict[str, Any]:
    """TRUE Cohen's kappa, over the double-labeled slice only: two labelers, same
    case, neither shown the other's answer (PRD A §0). Delegates to
    ``aggregate_kappa`` → ``cohens_kappa``, which already excludes any
    observation not explicitly blinded and returns None WITH a stated reason
    below the min-n gate (default 30) rather than a number nobody should trust.

    Exists as its own correctly-named entry point so the export can report
    "Cohen's κ" and "expert review acceptance" as two different statistics
    answering two different buyer questions — never interchangeably."""
    return aggregate_kappa(observations or [], min_n=min_n)


def external_adjudication_agreement(
    pairs: Sequence[Tuple[Optional[str], Optional[str]]], *,
    min_n: Optional[int] = None,
) -> Dict[str, Any]:
    """Agreement between the partner's adjudicated answer and our physician's
    independent answer (Buyer Response PRD §7 F3) — a SECOND, cross-institution
    inter-rater signal at zero marginal cost. Same 30-observation gate as internal κ.

    ``pairs`` are ``(partner_verdict, physician_verdict)`` over the cases where both a
    sealed partner adjudication and an independent physician answer exist."""
    min_n = min_n or kappa_min_n()
    usable = [(a, b) for (a, b) in pairs if a is not None and b is not None]
    n = len(usable)
    agree = sum(1 for a, b in usable if a == b)
    if n < min_n:
        return {"agreement": None, "kappa": None, "n": n, "n_agree": agree,
                "reason": f"only {n} paired adjudications; not reportable below {min_n}"}
    return {
        "agreement": round(agree / n, 4),
        "kappa": cohens_kappa(usable),
        "n": n, "n_agree": agree,
        "ci": _bootstrap_ci(usable),
    }
