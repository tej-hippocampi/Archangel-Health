#!/usr/bin/env python3
"""Audit an export bundle before it reaches a buyer. Harness PRD H3.

The Centaur sample went out as an earnings bundle, under an NC license, with an
eight-row contributor roster that did not match the records. A bundle is the only
artefact a buyer ever sees, so everything wrong with it is wrong in public.

Seven assertions, each from something that has already gone wrong or would be
unrecoverable if it did. Exit 2 names every failure (not just the first) — a
bundle is rebuilt once, so you want the whole list.

Usage:  python3 scripts/export_audit.py BUNDLE.zip [--names NAME [NAME ...]]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import zipfile

# Payment fields belong to us, never to the buyer. Their presence means the
# bundle was built from the earnings path rather than the export path.
FORBIDDEN_KEYS = ("amount_cents", "earning_id", "answer_key")
NC_LICENSE = re.compile(r"\bNC\b|non-?commercial", re.I)


def _jsonl(raw: bytes, name: str, problems: list[str]) -> list[dict]:
    rows = []
    for i, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            problems.append(f"{name}:{i} is not valid JSON ({exc})")
    return rows


def audit(path: pathlib.Path, names: list[str]) -> list[str]:
    problems: list[str] = []
    if not zipfile.is_zipfile(path):
        return [f"{path} is not a zip archive"]

    with zipfile.ZipFile(path) as z:
        members = z.namelist()
        blobs = {n: z.read(n) for n in members if not n.endswith("/")}

    text_all = "\n".join(v.decode("utf-8", "replace") for v in blobs.values())

    # (7) every .jsonl parses
    records: list[dict] = []
    for name, raw in blobs.items():
        if name.endswith(".jsonl"):
            rows = _jsonl(raw, name, problems)
            if pathlib.Path(name).name == "records.jsonl":
                records = rows

    # (6) scope recorded in batch.json
    batch_name = next((n for n in blobs if pathlib.Path(n).name == "batch.json"), None)
    batch: dict = {}
    if batch_name is None:
        problems.append("no batch.json — the bundle's scope is not recorded, so it "
                        "cannot be reproduced or corrected later")
    else:
        try:
            batch = json.loads(blobs[batch_name])
        except ValueError as exc:
            problems.append(f"batch.json is not valid JSON ({exc})")
        # A bundle spanning two specialties has no single ``specialty`` and
        # correctly records None there — but it does record its specialties, in
        # the plural key. Reading only the singular key reported "records no
        # specialty" about a bundle that records them precisely: a false red on a
        # normal, sellable cut.
        #
        # The plural key ONLY. Never ``counts.by_*``: those buckets carry display
        # sentinels ("unknown" for a missing specialty, "v1" for a missing portal
        # version) and are non-empty for every bundle the exporter can produce, so
        # accepting them would make this check incapable of failing. A gate that
        # cannot fail is worse than no gate — it reads green over the exact bundle
        # it exists to stop.
        plurals = {"specialty": "specialties", "portal_version": "portal_versions"}
        for field in ("specialty", "portal_version", "scope"):
            recorded = (batch.get(field)
                        or batch.get("filters", {}).get(field)
                        or batch.get(plurals.get(field, "")))
            if not recorded:
                problems.append(f"batch.json records no {field!r}")

    # (1) license is not NC
    lic = " ".join(str(batch.get(k, "")) for k in ("license", "license_id", "terms"))
    lic_files = "\n".join(v.decode("utf-8", "replace") for n, v in blobs.items()
                          if "licen" in n.lower())
    if NC_LICENSE.search(lic) or NC_LICENSE.search(lic_files):
        problems.append("license looks NON-COMMERCIAL — a bundle sold to a lab "
                        "cannot ship under an NC license, and this cannot be walked "
                        "back after delivery")
    elif not (lic.strip() or lic_files.strip()):
        problems.append("no license recorded anywhere in the bundle")

    # (4)(5) forbidden keys anywhere
    for name, raw in blobs.items():
        body = raw.decode("utf-8", "replace")
        for key in FORBIDDEN_KEYS:
            if f'"{key}"' in body:
                problems.append(f"{name} contains {key!r} — "
                                + ("shipping the key destroys the eval"
                                   if key == "answer_key"
                                   else "payment data must never leave with a bundle"))

    # (2) contributor roster == annotators present in records.jsonl
    if records:
        in_records = {str(r.get("contributor_id") or r.get("annotator_id") or "")
                      for r in records}
        in_records.discard("")
        roster = batch.get("contributors") or batch.get("contributor_table") or []
        roster_ids = {str(c.get("contributor_id") or c.get("id") or c)
                      for c in roster} if roster else set()
        if roster_ids:
            extra = sorted(roster_ids - in_records)
            missing = sorted(in_records - roster_ids)
            if extra:
                problems.append(f"contributor roster lists {len(extra)} id(s) absent "
                                f"from records.jsonl: {extra[:8]}")
            if missing:
                problems.append(f"records.jsonl has {len(missing)} annotator(s) absent "
                                f"from the roster: {missing[:8]}")
    elif blobs:
        problems.append("no records.jsonl found — nothing to check the roster against")

    # (3) no physician name string anywhere
    for n in names:
        if n and re.search(rf"\b{re.escape(n)}\b", text_all, re.I):
            problems.append(f"physician name {n!r} appears in the bundle — "
                            f"contributors ship as ids, never names")

    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle")
    ap.add_argument("--names", nargs="*", default=[],
                    help="physician names that must not appear (defaults to none)")
    args = ap.parse_args(argv[1:])

    path = pathlib.Path(args.bundle)
    if not path.exists():
        print(f"{path}: not found", file=sys.stderr)
        return 2

    problems = audit(path, args.names)
    if problems:
        print(f"EXPORT AUDIT FAILED — {path.name}", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nFix the export path and REBUILD. Never hand-edit a bundle: a "
              "patched zip is unreproducible and the next one has the same bug.",
              file=sys.stderr)
        return 2
    print(f"export audit passed — {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
