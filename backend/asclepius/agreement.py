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

from typing import Any, Dict, List, Optional, Sequence, Tuple


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


def aggregate_kappa(observations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold stored per-task agreement observations into aggregate κ.

    ``observations`` rows carry ``verdict_a``, ``verdict_b``, and ``specialty``.
    Returns ``{overall, by_specialty, n, observed_agreement}``.
    """
    pairs = [(o.get("verdict_a"), o.get("verdict_b")) for o in observations]
    overall = cohens_kappa(pairs)

    by_specialty: Dict[str, Optional[float]] = {}
    spec_groups: Dict[str, List[Tuple[Optional[str], Optional[str]]]] = {}
    for o in observations:
        sp = o.get("specialty") or "unknown"
        spec_groups.setdefault(sp, []).append((o.get("verdict_a"), o.get("verdict_b")))
    for sp, ps in spec_groups.items():
        by_specialty[sp] = cohens_kappa(ps)

    usable = [p for p in pairs if p[0] is not None and p[1] is not None]
    observed = (
        round(sum(1 for a, b in usable if a == b) / len(usable), 4) if usable else None
    )
    return {
        "overall": overall,
        "by_specialty": by_specialty,
        "n": len(usable),
        "observed_agreement": observed,
    }
