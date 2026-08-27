"""The CI matrix must run every test file — exactly once.

``scripts/ci_shard.py`` decides which test files each CI job runs. The failure
mode it exists to prevent is silent: a file that belongs to NO shard is never
executed, CI stays green, and the suite quietly stops testing whatever that file
covered. A green build that checks nothing is worse than a red one.

So the partition property is asserted here, in the suite itself, on every run —
which means the guard travels with the thing it guards. Add a test file and this
test proves it reached a shard; change the packing and this test proves the
partition survived.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import ci_shard  # noqa: E402

BACKEND = str(Path(__file__).resolve().parent.parent)

# The shard count the workflow uses. Kept here so a change to the matrix without a
# corresponding thought about balance shows up as a diff on this line.
CI_SHARDS = 4


def _discover():
    return ci_shard.discover(BACKEND)


def test_every_test_file_is_discovered():
    """Discovery must see the real directory, not an empty list — an empty
    discovery would make every property below vacuously true."""
    found = _discover()
    on_disk = {
        f"tests/{p.name}" for p in (Path(BACKEND) / "tests").glob("test_*.py")
    }
    assert found, "discovery returned nothing"
    assert set(found) == on_disk - set(ci_shard.EXCLUDED)
    assert len(found) > 100, f"only {len(found)} test files discovered — suspicious"


@pytest.mark.parametrize("total", [1, 2, 3, 4, 5, 8, 16])
def test_the_shards_are_a_partition_of_the_suite(total):
    """Union == everything, and no file appears twice. This is THE property."""
    files = _discover()
    shards = ci_shard.assign(files, total)
    assert len(shards) == total

    union = [f for s in shards for f in s]
    assert sorted(union) == sorted(files), "a test file fell out of the matrix"
    assert len(union) == len(set(union)), "a test file is in two shards"


def test_no_shard_is_empty_at_the_configured_width():
    """An empty shard makes ``pytest $(ci_shard.py ...)`` collect the whole suite
    from the rootdir — every job running everything, silently."""
    shards = ci_shard.assign(_discover(), CI_SHARDS)
    assert all(shards), f"an empty shard at width {CI_SHARDS}"


def test_the_cli_refuses_an_empty_shard_rather_than_running_everything():
    """With more shards than files, LPT fills the low-numbered bins first, so the
    empty ones are at the END — shard 1 of an over-wide matrix still has work.
    The guard is per-shard for exactly that reason: 'shard 1 is fine' proves
    nothing about shard 400."""
    n_files = len(_discover())
    too_many = n_files + 5
    assert ci_shard.main(["ci_shard.py", str(too_many), str(too_many)]) == 1
    # …and the early shard of that same over-wide matrix is NOT empty, which is
    # why a single spot-check would have missed this.
    assert ci_shard.main(["ci_shard.py", "1", str(too_many)]) == 0


def test_assignment_is_deterministic():
    """A shard that fails in CI must be reproducible locally, and a re-run must
    not silently test a different set."""
    first = ci_shard.assign(_discover(), CI_SHARDS)
    second = ci_shard.assign(list(reversed(_discover())), CI_SHARDS)
    assert first == second


def test_the_visual_gate_is_excluded_and_owned_by_its_own_job():
    """It needs a browser; a browser flake must read as 'the visual gate is red',
    not as a backend failure. It must be in neither the backend shards nor twice."""
    assert "tests/test_asclepius_visual.py" in ci_shard.EXCLUDED
    assert (Path(BACKEND) / "tests/test_asclepius_visual.py").exists(), (
        "the excluded file no longer exists — drop it from EXCLUDED")
    for s in ci_shard.assign(_discover(), CI_SHARDS):
        assert "tests/test_asclepius_visual.py" not in s


def test_an_unweighted_file_is_still_assigned():
    """WEIGHTS is an optimisation, never a correctness input: a file nobody has
    measured still lands in a shard. This is what makes adding a test file safe."""
    files = _discover() + ["tests/test_a_brand_new_unmeasured_file.py"]
    shards = ci_shard.assign(files, CI_SHARDS)
    union = [f for s in shards for f in s]
    assert "tests/test_a_brand_new_unmeasured_file.py" in union
    assert sorted(union) == sorted(files)


def test_the_shards_are_reasonably_balanced():
    """Not a correctness property — a warning that the weights have gone stale.
    The ceiling is loose on purpose: balance only costs wall-clock, and a tight
    assertion here would fail on every ordinary test-file addition."""
    shards = ci_shard.assign(_discover(), CI_SHARDS)
    loads = [sum(ci_shard.WEIGHTS.get(f, ci_shard.DEFAULT_WEIGHT) for f in s)
             for s in shards]
    assert min(loads) > 0
    assert max(loads) <= 2.0 * (sum(loads) / len(loads)), (
        f"shard loads {[round(x) for x in loads]} are badly skewed — refresh "
        f"WEIGHTS with: pytest --durations=0 | python3 scripts/ci_shard.py --measure")


def test_the_weights_table_names_only_real_files():
    """A weight for a deleted file is dead config that quietly stops balancing."""
    on_disk = {f"tests/{p.name}" for p in (Path(BACKEND) / "tests").glob("test_*.py")}
    stale = sorted(set(ci_shard.WEIGHTS) - on_disk)
    assert not stale, f"WEIGHTS names files that no longer exist: {stale}"


def test_the_workflow_matrix_matches_the_configured_width():
    """The workflow and this module must agree on the shard count, or a shard's
    worth of tests silently stops running."""
    workflow = Path(BACKEND).parent / ".github/workflows/tests.yml"
    text = workflow.read_text(encoding="utf-8")
    assert f"CI_TOTAL_SHARDS: {CI_SHARDS}" in text, (
        f"tests.yml does not declare CI_TOTAL_SHARDS: {CI_SHARDS}")
    # Every 1..N index must appear in the matrix list.
    for i in range(1, CI_SHARDS + 1):
        assert f"shard: [" in text
    matrix_line = next(l for l in text.splitlines() if "shard: [" in l)
    declared = [int(x) for x in matrix_line.split("[", 1)[1].split("]")[0].split(",")]
    assert declared == list(range(1, CI_SHARDS + 1)), (
        f"matrix declares shards {declared}, expected 1..{CI_SHARDS}")
