"""Deterministic, TOTAL sharding of the backend pytest suite across CI jobs.

The suite outgrew a single job. It ran 13m05s on a good runner and was killed by
the 30-minute ceiling on a noisy one — and a timeout-killed job reports as
CANCELLED with no summary line, so it looks exactly like a real regression while
telling you nothing (see the note in ``.github/workflows/tests.yml``).

The fix that workflow asks for is a split. This is the file-assignment half of it.

Two properties matter more than balance, and both are enforced by
``tests/test_ci_sharding.py``:

* **TOTAL** — every discovered test file lands in exactly one shard. A new test
  file is picked up automatically by whichever shard the packing gives it. The
  failure mode this exists to prevent is a file that belongs to no shard: CI stays
  green while silently testing nothing, which is worse than a red build.
* **DETERMINISTIC** — the same tree produces the same assignment on every runner
  and every re-run, so a shard that fails can be reproduced locally with
  ``python3 scripts/ci_shard.py <index> <total>``.

Each shard runs pytest SERIALLY in its own job. That is deliberate and is the
reason this is a matrix rather than ``pytest-xdist``: the workflow warns that the
timing-sensitive tests (``tests/test_community.py``'s websocket cases) "get
flakier under parallelism rather than faster". Separate runners give every shard a
whole machine, so no test ever shares a CPU with another test — the wall-clock win
without the flakiness.

Balance comes from ``WEIGHTS``, measured seconds per file. It is an OPTIMISATION,
never a correctness input: an unmeasured or newly-added file gets
``DEFAULT_WEIGHT`` and is still assigned. A stale table makes shards uneven; it
can never make one incomplete.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List

# Relative to ``backend/``. Kept as a repo-relative path so the printed argv can
# be pasted straight into a local pytest run.
TESTS_DIR = "tests"

# Owned by the ``visual`` job in tests.yml — it needs a browser, and a browser
# flake must read as "the visual gate is red", not as a backend failure. Excluded
# here so it is never double-run, and asserted excluded by the sharding test.
EXCLUDED = frozenset({"tests/test_asclepius_visual.py"})

# Seconds, measured with ``pytest --durations=0`` and summed per file. Refresh
# with ``python3 scripts/ci_shard.py --measure`` after a big change if shards
# drift out of balance. Only files materially above the default are listed —
# everything else is close enough that listing it would be noise.
DEFAULT_WEIGHT = 2.0
WEIGHTS: Dict[str, float] = {
    "tests/test_paired_review.py": 80.4,
    "tests/test_verification_queue.py": 61.6,
    "tests/test_upload_scale.py": 48.6,
    "tests/test_routing_priority.py": 42.4,
    "tests/test_postop_synthetic_load.py": 39.6,
    "tests/test_hs_portal.py": 31.9,
    "tests/test_review_tier.py": 17.8,
    "tests/test_payments_session.py": 17.0,
    "tests/test_security_headers.py": 16.9,
    "tests/test_password_reset.py": 12.5,
    "tests/test_asclepius_real_case_gen.py": 11.9,
    "tests/test_asclepius_generation.py": 11.7,
    "tests/test_community.py": 10.8,
    "tests/test_asclepius_router.py": 9.9,
    "tests/test_patient_access_control.py": 9.4,
    "tests/test_payments_progress.py": 8.8,
    "tests/test_admin_launch_prd.py": 8.1,
    "tests/test_v4_promotion.py": 7.8,
    "tests/test_payments_accrual.py": 7.0,
    "tests/test_signup_links.py": 6.9,
    "tests/test_pending_verification_surface.py": 6.4,
    "tests/test_promotion_gate.py": 6.3,
    "tests/test_tiering_learning.py": 6.1,
    "tests/test_gold_router.py": 6.0,
    "tests/test_payments_integrity.py": 5.9,
    "tests/test_onboarding_team_caps.py": 5.1,
    "tests/test_admin_signups.py": 5.0,
    "tests/test_asclepius_ingestion.py": 4.9,
    "tests/test_tiering_score.py": 4.6,
    "tests/test_credentialing_audit.py": 4.6,
    "tests/test_asclepius_contributors.py": 4.3,
    "tests/test_referral_payout.py": 4.3,
    "tests/test_case_export.py": 4.2,
    "tests/test_health_systems.py": 3.8,
    "tests/test_postop_care_companion.py": 3.7,
    "tests/test_community_v2.py": 3.6,
    "tests/test_demo_and_patient_update.py": 3.6,
    "tests/test_admin_ai_compliance.py": 3.5,
    "tests/test_doctor_email_verification.py": 3.5,
    "tests/test_asclepius_rubric.py": 3.5,
    "tests/test_asclepius_v4_phase1_completeness.py": 3.2,
    "tests/test_community_social.py": 3.1,
    "tests/test_asclepius_hardcase.py": 3.1,
    "tests/test_demo_day_p1_fixes.py": 3.0,
}


def discover(root: str = ".") -> List[str]:
    """Every backend test file, sorted. Sorted so the packing below is stable."""
    tests = os.path.join(root, TESTS_DIR)
    names = [
        f"{TESTS_DIR}/{name}"
        for name in os.listdir(tests)
        if name.startswith("test_") and name.endswith(".py")
    ]
    return sorted(n for n in names if n not in EXCLUDED)


def assign(files: List[str], total: int) -> List[List[str]]:
    """Partition ``files`` into ``total`` shards, heaviest-first (LPT).

    Longest-processing-time-first is the standard greedy bin-packing heuristic and
    gets within 4/3 of optimal, which is far more than enough here. Ties break on
    the filename so the result does not depend on dict or filesystem ordering.

    This is a PARTITION: the returned lists are disjoint and their union is
    ``files``. That is the property the whole file exists to guarantee.
    """
    if total < 1:
        raise ValueError(f"total shards must be >= 1, got {total}")
    ordered = sorted(files, key=lambda f: (-WEIGHTS.get(f, DEFAULT_WEIGHT), f))
    shards: List[List[str]] = [[] for _ in range(total)]
    loads = [0.0] * total
    for f in ordered:
        i = loads.index(min(loads))
        shards[i].append(f)
        loads[i] += WEIGHTS.get(f, DEFAULT_WEIGHT)
    # Sorted within a shard so a shard's pytest invocation is stable too.
    return [sorted(s) for s in shards]


def shard(index: int, total: int, root: str = ".") -> List[str]:
    """The files for 1-based shard ``index`` of ``total``."""
    if not 1 <= index <= total:
        raise ValueError(f"shard index {index} out of range 1..{total}")
    return assign(discover(root), total)[index - 1]


def _measure(root: str = ".") -> None:
    """Print a refreshed WEIGHTS table from a --durations=0 run on stdin."""
    per_file: Dict[str, float] = {}
    for line in sys.stdin:
        parts = line.split()
        # "0.12s call     tests/test_x.py::test_y"
        if len(parts) >= 3 and parts[0].endswith("s") and "::" in parts[-1]:
            try:
                secs = float(parts[0][:-1])
            except ValueError:
                continue
            path = parts[-1].split("::", 1)[0]
            per_file[path] = per_file.get(path, 0.0) + secs
    print("WEIGHTS: Dict[str, float] = {")
    for path, secs in sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0])):
        if secs >= DEFAULT_WEIGHT * 1.5 and path not in EXCLUDED:
            print(f'    "{path}": {secs:.1f},')
    print("}")


def main(argv: List[str]) -> int:
    if "--measure" in argv:
        _measure()
        return 0
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print(f"usage: {argv[0]} <shard-index-1-based> <total-shards>", file=sys.stderr)
        print(f"       {argv[0]} --measure   < durations.txt", file=sys.stderr)
        return 2
    files = shard(int(argv[1]), int(argv[2]))
    if not files:
        # An empty shard would make ``pytest $(ci_shard.py ...)`` collect the WHOLE
        # suite from the rootdir — every shard running everything, silently. Fail
        # loudly instead: an empty shard means more shards than test files, which
        # is a misconfiguration, not a valid state.
        print(f"shard {argv[1]}/{argv[2]} is EMPTY — more shards than test files",
              file=sys.stderr)
        return 1
    print("\n".join(files))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main(sys.argv))
