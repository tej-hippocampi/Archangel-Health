"""How a contributor is paid — the seam that does not exist yet (Advisor PRD §2.4).

There is no payment accrual in this codebase today. This module exists anyway,
and it is deliberate: the moment someone builds one, they will iterate over
completed submissions and multiply by a rate. Nothing in the data would tell
them that a medical advisor holds equity instead of a per-task rate, so the
advisor would silently accrue a payment obligation the company never agreed to.

So the column ships now (``users.compensation_model``), the predicate is named
and tested now, and the SQL fragment every future aggregate needs is written
now. When the payments feature arrives, ``accrues_payment`` is already the
obvious thing to call and ``PAYABLE_SQL`` is already the obvious thing to paste
into the WHERE clause.

Values:
  * ``per_task``     — the normal contributor: paid per completed record.
  * ``equity_only``  — appointed medical advisor: holds equity, is NOT paid per
                       task. Their labels and reviews still count everywhere
                       QUALITY is measured (PRD §5.2) — only money is excluded.
  * ``NULL``         — unset. Treated as payable, because every pre-existing
                       contributor predates this column and IS a normal
                       per-task contributor. A DEFAULT would be the same
                       assertion with less evidence; the backfill rule is
                       stated here instead of hidden in a schema default.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

PER_TASK = "per_task"
EQUITY_ONLY = "equity_only"
COMPENSATION_MODELS = (PER_TASK, EQUITY_ONLY)

# Drop into any payment aggregate's WHERE clause. NULL is payable (see above),
# which is why this cannot be written as a bare ``!=``: in SQL,
# ``NULL != 'equity_only'`` is NULL, not true, so a plain inequality would
# silently exclude every legacy contributor from their own paycheck.
PAYABLE_SQL = "(u.compensation_model IS NULL OR u.compensation_model != 'equity_only')"


def accrues_payment(user: Optional[Dict[str, Any]]) -> bool:
    """True when this contributor's completed work should generate money.

    False ONLY for an explicit ``equity_only`` model. Unknown/unset is payable
    — under-paying a physician who did the work is worse than over-counting a
    volunteer, and the advisor path always writes the column explicitly.
    """
    return (user or {}).get("compensation_model") != EQUITY_ONLY
