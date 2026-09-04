"""The four committed patient records, and the door they go in through
(Longitudinal E2E PRD §2).

**What this exists to fix.** Trajectories are created by exactly one code path —
``POST /ingestion/cases/{id}/generate`` with ``trajectory: true`` — and that path
takes an *ingest case*, i.e. a chart that came in through ``/ingestion``. The
three "REAL · STATIC V4" cases on the routing screen are hand-authored Python
dicts in :mod:`asclepius.v4_cases`, inserted directly as tasks. They were written
*from* patients 1, 3 and 4, but the patients themselves were never uploaded — so
``ingest_cases`` for them did not exist, ``generate`` had nothing to run on, and
the Longitudinal batch read ``0 trajectories · 0 points``. Every other piece of
the machinery was built and tested. The data was never fed to it.

**The rule this module is built around.** There is no shortcut that inserts
trajectories from ``v4_cases.py``. The bundles go through the SAME door a
hospital's upload takes — mint a link, post the bytes, let the background
pipeline unpack, normalize, de-id-verify and land ``ingest_cases`` — because the
point is that the next hospital's upload works the same way. A second, friendlier
admin ingest path would have to reproduce the fail-closed encryption and
durability checks exactly, or quietly become the unsafe way in.

**Determinism, and why the pack is uncompressed.** Idempotency is keyed on the
sha256 of the bytes we submit (§2.1), which only works if packing the same tree
twice produces the same bytes. ``ZIP_DEFLATED`` does not guarantee that across
zlib versions, so the pack is ``ZIP_STORED`` with a fixed timestamp and sorted
entry names: the archive is then a pure function of the file contents. The
bundles are a few MB each and the ingest cap is 100 MB, so paying bytes for
determinism costs nothing that matters.

**The manifest.** None of the four trees carries a ``manifest.json``, and without
one ingestion resolves specialty to ``general``. One is synthesized here per
bundle, carrying the specialty from ``v4_cases.FIXTURE_BUNDLE_SPECIALTIES`` and a
``patient_key`` so each bundle lands as exactly one ingest case rather than being
split by whichever key each adapter happened to find.

See ``fixtures/patient_bundles/README.md`` for the measured yield of these four
charts and for the one that quarantines.
"""

from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.patient_fixtures")

#: Where the committed trees live. Overridable so an operator can point at a
#: volume holding bundles too large or too sensitive to commit; the shape must be
#: the same — one directory per bundle, named as the bundle is named.
_DEFAULT_ROOT = Path(__file__).resolve().parent / "fixtures" / "patient_bundles"

#: The partner identity these uploads land under. Not a real health system, and
#: deliberately labelled as a fixture: a row in Box 1 that looked like a hospital's
#: would make "which of these came from a partner" unanswerable on the screen that
#: has to answer it.
FIXTURE_PARTNER_ID = "archangel-fixture"
FIXTURE_PARTNER_LABEL = "Archangel (fixture)"

#: Fixed so the archive bytes are reproducible. Any constant would do; this is the
#: zip epoch (1980-01-01), which is what a zip writes when it has no date at all.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: Bundle documentation, excluded from the archive by NAME. Two separate reasons,
#: either one sufficient:
#:
#:   * ``ingestion._classify`` reads ``.md`` as ``note_text`` — a clinical note. A
#:     bundle README is not one. Left in, ``patient-1/README.md`` is ingested as
#:     the chart's 369th note, and its "Clinical synopsis" paragraph names the
#:     outcome ("later ICU admission for septic shock"). It never reaches a
#:     physician — ``real_cases._visible`` fails closed on an undated item, so the
#:     README is dropped from every point and only shows up as a +1 in the
#:     "omitted for unknown timing" count an admin reads — but a documentation
#:     file has no business being counted as a dropped clinical note, and the one
#:     thing standing between it and a decision point should not be a rule about
#:     timestamps.
#:   * ``.dockerignore`` strips ``*.md`` from the build context, so the deployed
#:     image packs a DIFFERENT archive from the one a checkout packs. The sha256
#:     of these bytes is the idempotency key in ``ingest_committed_bundles``, and
#:     the docstring above promises the bytes are reproducible. Excluding the
#:     documentation here makes that promise true in the image as well as on a
#:     developer's disk, rather than true in one place and quietly false in the
#:     other. (Measured: encounter counts are identical either way — 22/16/5/12 —
#:     so no published yield number moves.)
_NOT_CLINICAL_DATA = frozenset({"README.md"})

#: The authorizing link row for a fixture ingest is born expired — see the note at
#: its ``create_upload_link`` call. A fixed past timestamp rather than "now", so
#: the comparison in ``_validate_upload_token`` is unambiguous rather than a
#: same-second string compare.
_LINK_BORN_EXPIRED = "1980-01-01T00:00:00"


def bundles_root() -> Path:
    override = (os.getenv("ASCLEPIUS_PATIENT_FIXTURE_DIR") or "").strip()
    return Path(override) if override else _DEFAULT_ROOT


def available_bundles() -> List[str]:
    """The bundle names present on disk, sorted. Empty is a legitimate state (an
    operator pointed the override at a volume that is not mounted yet), and the
    caller reports it rather than treating it as a failure."""
    root = bundles_root()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))


def specialty_for(bundle: str) -> Optional[str]:
    """The declared specialty for a committed bundle, or None for an unknown one.

    Read through ``v4_cases`` rather than duplicated here — see the note on
    ``FIXTURE_BUNDLE_SPECIALTIES`` for why the map lives there.
    """
    from asclepius import v4_cases

    return v4_cases.FIXTURE_BUNDLE_SPECIALTIES.get(bundle)


def pack_bundle(bundle: str, *, root: Optional[Path] = None) -> bytes:
    """One committed tree → the exact bytes a partner would have posted.

    Deterministic: sorted names, fixed timestamps, stored (uncompressed). Two
    calls on an unchanged tree return identical bytes, which is what makes the
    sha256 idempotency key in ``ingest_committed_bundles`` mean anything.

    Bundle documentation (``_NOT_CLINICAL_DATA``) is excluded — see the note
    there. That is also what makes "identical bytes" hold across ENVIRONMENTS and
    not just across two calls on one disk.
    """
    base = (root or bundles_root()) / bundle
    if not base.is_dir():
        raise FileNotFoundError(f"no committed bundle named {bundle!r} under {base.parent}")
    specialty = specialty_for(bundle)
    if not specialty:
        # Refused rather than defaulted. ``general`` is what ingestion falls back
        # to with no manifest, and shipping a hepatology chart as ``general`` is
        # the invisible mislabel this whole map exists to prevent — so an unmapped
        # bundle stops here, where the message can name the file to edit.
        raise ValueError(
            f"{bundle!r} has no entry in v4_cases.FIXTURE_BUNDLE_SPECIALTIES. Add one "
            "before ingesting it: without a manifest specialty, ingestion resolves the "
            "chart to 'general' and it routes to the wrong pool.")

    files = sorted((p for p in base.rglob("*")
                    if p.is_file() and p.name not in _NOT_CLINICAL_DATA),
                   key=lambda p: str(p.relative_to(base)).replace(os.sep, "/"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        manifest = {
            "specialty": specialty,
            # One bundle is one patient, and saying so is not a convenience: the
            # manifest key is AUTHORITATIVE in ``_patient_key_and_source``, so
            # without it a chart whose HL7, FHIR and CSV disagree about the
            # patient id splits into three ingest cases and each one carries a
            # slice of the narrative.
            "patient_key": bundle,
            "source": "archangel-committed-fixture",
        }
        info = zipfile.ZipInfo("manifest.json", date_time=_ZIP_EPOCH)
        info.external_attr = 0o600 << 16
        z.writestr(info, json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        for p in files:
            arc = str(p.relative_to(base)).replace(os.sep, "/")
            info = zipfile.ZipInfo(arc, date_time=_ZIP_EPOCH)
            info.external_attr = 0o600 << 16
            z.writestr(info, p.read_bytes())
    return buf.getvalue()


def bundle_description(bundle: str) -> str:
    """What the row says this data is, in Box 1 (§2.1)."""
    return f"Committed de-identified fixture · {bundle}"


def _unusable_token_hash(bundle: str, digest: str) -> str:
    """A token hash no token can ever produce.

    ``ingest_upload_links.token_hash`` is the lookup key the partner door
    validates against, and it is UNIQUE-ish by construction. This path posts the
    bytes in-process and never hands a URL to anyone, so minting a real token
    would create a live credential for a chart that is already ingested. Instead
    the column carries a domain-separated digest of the bundle identity: it is the
    right shape, it is stable, and ``sha256(token_urlsafe(32))`` cannot collide
    with it because the prefix is not part of any token's preimage.
    """
    import hashlib

    return hashlib.sha256(f"asclepius:fixture-link:{bundle}:{digest}".encode()).hexdigest()


def ingest_committed_bundles(
    store: Any, *, actor: str, bundles: Optional[List[str]] = None,
    on_ingested: Optional[Any] = None,
) -> Dict[str, Any]:
    """Submit each committed bundle through the real ingestion door.

    Returns a per-bundle report. **Idempotent**: a bundle whose exact bytes are
    already on an ``ingest_uploads`` row is skipped with a notice rather than
    ingested twice, so a second click is a no-op and not four duplicate charts.

    ``purpose`` is deliberately left unset. These land in Box 1 exactly like a
    hospital's upload and an admin says what they are for — which is the flow this
    PRD is about, and the one place a shortcut would have hidden the front door
    all over again.

    ``on_ingested`` is called with each new ``upload_id`` once its row exists; the
    router passes ``BackgroundTasks.add_task`` so unpacking never runs in the
    request path (a 6 MB chart takes seconds of CPU to normalize).
    """
    from asclepius import ingestion as asc_ingestion

    names = bundles if bundles is not None else available_bundles()
    root = bundles_root()
    results: List[Dict[str, Any]] = []
    ingested = skipped = failed = 0

    for name in names:
        row: Dict[str, Any] = {"bundle": name}
        try:
            data = pack_bundle(name, root=root)
        except (FileNotFoundError, ValueError) as exc:
            row.update({"status": "failed", "error": str(exc)})
            results.append(row)
            failed += 1
            continue

        digest = asc_ingestion.sha256_hex(data)
        row.update({"sha256": digest, "size_bytes": len(data),
                    "specialty": specialty_for(name)})

        existing = store.find_ingest_upload_by_sha256(digest)
        if existing:
            row.update({"status": "skipped", "upload_id": existing["upload_id"],
                        "message": "Already ingested — same bytes, same sha256."})
            results.append(row)
            skipped += 1
            continue
        # Second key, by name (Case Generation Fix PRD §A5). The synthesized
        # manifest is part of the packed bytes, so a change to a bundle's declared
        # specialty (patient-3 moved from nephrology to hepatology) changes the
        # digest — and without this check the next click would land the same chart
        # a second time under the new label. One chart, one row; an admin changes
        # the specialty of the row that exists rather than ingesting a twin.
        earlier = store.find_ingest_upload_by_partner_filename(FIXTURE_PARTNER_ID, f"{name}.zip")
        if earlier:
            row.update({"status": "skipped", "upload_id": earlier["upload_id"],
                        "message": ("Already ingested under an earlier packing of this "
                                    "bundle — set the specialty on the existing row "
                                    "rather than ingesting it twice.")})
            results.append(row)
            skipped += 1
            continue

        # The link is minted per bundle and one-time, exactly as the admin upload
        # modal does it, so the bytes travel the authorized path rather than an
        # admin-only side entrance. purpose=None on purpose (see the docstring).
        #
        # The token is minted and immediately consumed inside this call; it is
        # never returned to anyone, so it cannot be replayed. It exists because
        # ``ingest_upload_links`` is the row that AUTHORIZES an upload, and an
        # upload with no authorizing row has no provenance to attach.
        link = store.create_upload_link(
            token_hash=_unusable_token_hash(name, digest),
            partner_id=FIXTURE_PARTNER_ID, partner_label=FIXTURE_PARTNER_LABEL,
            specialty=specialty_for(name),
            # Already expired, and already used one line below. Two independent
            # reasons ``_validate_upload_token`` refuses it, so even if the row
            # were somehow reachable it authorizes nothing.
            expires_at=_LINK_BORN_EXPIRED, one_time=True,
            max_bytes=asc_ingestion.max_zip_bytes(),
            created_by=actor, purpose=None)

        upload_id = store.new_upload_id()
        try:
            raw_path = asc_ingestion.store_raw(upload_id, data)
        except Exception as exc:  # disk full, permissions, encryption not configured
            row.update({"status": "failed", "error": f"could not store the bundle: {exc}"})
            results.append(row)
            failed += 1
            continue
        if not store.consume_upload_link(link["link_id"], one_time=True):
            # Cannot happen for a link minted one line ago, but the orphan blob is
            # deleted rather than left behind if it ever does.
            asc_ingestion.delete_raw(raw_path)
            row.update({"status": "failed", "error": "the minted upload link was already used"})
            results.append(row)
            failed += 1
            continue

        upload = store.insert_ingest_upload(
            upload_id=upload_id, link_id=link["link_id"], partner_id=FIXTURE_PARTNER_ID,
            filename=f"{name}.zip", sha256=digest, size_bytes=len(data),
            raw_path=raw_path, source_ip=None)
        store.attach_upload_provenance(upload["upload_id"], link_id=link["link_id"])
        store.set_upload_description(upload["upload_id"], bundle_description(name))
        store.log_event(
            entity_type="ingest_upload", entity_id=upload["upload_id"],
            event_type="upload_received", actor=actor,
            payload={"partner_id": FIXTURE_PARTNER_ID, "sha256": digest,
                     "bytes": len(data), "fixture_bundle": name})
        if on_ingested is not None:
            on_ingested(asc_ingestion.process_upload, store, upload["upload_id"])
        row.update({"status": "ingested", "upload_id": upload["upload_id"]})
        results.append(row)
        ingested += 1

    return {"root": str(root), "bundles": results,
            "ingested": ingested, "skipped": skipped, "failed": failed,
            "available": names}
