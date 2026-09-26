"""Prepare immutable onboarding pairs in isolated CI; never connects to live data.

Download the resulting artifacts, validate them with --check, and commit the
approved files into backend/asclepius/onboarding_material/cases before release.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def matrix_specialties(value: str) -> list[str]:
    """A bounded subset lets a recovery build retry only unfinished specialties."""
    from asclepius.onboarding_catalog import SPECIALTIES
    from asclepius.onboarding_specialties import canonical
    choices = list(SPECIALTIES) if value == 'all' else [canonical(s.strip()) for s in value.split(',')]
    if any(s not in SPECIALTIES for s in choices) or len(choices) != len(set(choices)):
        raise ValueError('Choose unique supported launch specialties or all')
    priority = ('pathology', 'dermatology', 'neurology')
    return sorted(choices, key=lambda s: priority.index(s) if s in priority else len(priority))


async def build(specialty: str, output: Path, diagnostics: Path | None = None):
    from asclepius import onboarding_cases as bank

    def retain_rejection(document):
        diagnostics.mkdir(parents=True, exist_ok=True)
        # Separate namespace/directory; this can never masquerade as a ready
        # library file or overwrite an earlier rejected attempt.
        path = diagnostics / ("rejected-" + document["task_id"] + "-" + uuid.uuid4().hex + ".json")
        with path.open("x") as stream:
            stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    token = bank.REVIEW_DIAGNOSTICS.set(retain_rejection if diagnostics else None)
    try:
        await _build(specialty, output)
    finally:
        bank.REVIEW_DIAGNOSTICS.reset(token)


async def _build(specialty: str, output: Path):
    from asclepius import onboarding_cases as bank, onboarding_library as library
    from asclepius.store import get_store
    from scripts.smoke_onboarding_cases import prepare
    from scripts.check_onboarding_reviewer import check_reviewer, ReviewerUnavailable
    store = get_store()
    output.mkdir(parents=True, exist_ok=True)
    failures = []
    for kind in ("practice", "examination"):
        try:
            ident = bank.task_id(specialty, kind)
            row = library.row_for(ident)
            reused = row is not None
            if reused:
                attempts, rejected = 0, []
            else:
                ident, row, attempts, rejected = await prepare(store, bank, specialty, kind, use_library=True,
                                                               before_attempt=check_reviewer)
            document = {"task_id": ident, "specialty": specialty, "kind": kind, "slot": 1,
                        "entry": json.loads(row["entry_json"]), "validation": json.loads(row["validation_json"])}
            library.validate(document)
            path = output / (ident + ".json")
            content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
            if path.exists() and path.read_text() != content:
                raise ValueError("Never overwrite a published case identity")
            path.write_text(content)
            print(json.dumps({"specialty": specialty, "kind": kind, "ready": True,
                              "attempts": attempts, "reused_release_case": reused, "rejections": rejected}), flush=True)
        except ReviewerUnavailable:
            # Already written passing companions remain uploadable. Repeating
            # authorship or moving on to the exam cannot repair missing credits.
            raise
        except Exception as exc:
            failures.append({"specialty": specialty, "kind": kind, "error": str(exc)})
            print(json.dumps(failures[-1]), flush=True)
    with store._conn() as conn:
        for table in ("tasks", "submissions", "records"):
            if conn.execute("SELECT count(*) FROM " + table).fetchone()[0]:
                raise RuntimeError("Library build touched paid inventory")
    if failures:
        raise RuntimeError(f"{len(failures)} cases failed review; no unreviewed material was published")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--specialty")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--diagnostics", type=Path)
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    from asclepius.onboarding_catalog import SPECIALTIES
    from asclepius.onboarding_specialties import canonical
    if args.matrix:
        value = args.specialty or os.getenv("SMOKE_SPECIALTY", "all")
        try:
            print(json.dumps(matrix_specialties(value)))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    elif args.check:
        from asclepius import onboarding_library
        report = onboarding_library.coverage()
        print(json.dumps(report, indent=2))
        if len(report) != 2 * len(SPECIALTIES) or not all(r["ready"] for r in report):
            raise SystemExit("The specialty release library is incomplete")
    else:
        if os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("ASCLEPIUS_LLM_PROVIDER") == "fake":
            raise SystemExit("Real generation runs only in GitHub Actions")
        if not all(os.getenv(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ASCLEPIUS_DB_PATH")):
            raise SystemExit("Both provider keys and isolated storage are required")
        if canonical(args.specialty) not in SPECIALTIES or not args.output:
            raise SystemExit("A launch specialty and output directory are required")
        asyncio.run(build(canonical(args.specialty), args.output, args.diagnostics))
