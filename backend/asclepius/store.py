"""AsclepiusStore — SQLite persistence for the Expert Evaluation Portal.

Follows the ``team_store.py`` pattern exactly (``_conn()`` + ``row_factory``,
``_init_schema()`` via ``executescript``, parameterized SQL, JSON columns
deserialized on read) but writes to its OWN database file
(``ASCLEPIUS_DB_PATH``, default ``backend/asclepius.db``). It never touches
``team.db`` (PRD §0, §10).

Tables
  users         standalone Asclepius accounts (evaluator/admin/qa_reviewer)
  tasks         admin-loaded prompts + blinded candidate answers
  submissions   raw doctor output + lifecycle status + verification artifacts
  records       packaged training records (preference / ideal_answer / trace)
  events        append-only provenance log (mirrors team_store.event_logs)
  exports       delivery-batch manifests

A process-wide singleton is exposed via ``get_store()`` so the FastAPI router,
the auth dependencies, and the verification pipeline all share one instance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from passlib.context import CryptContext

_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _iso_minus_seconds(seconds: int) -> str:
    """ISO timestamp ``seconds`` in the past — the cutoff for age-based sweeps
    (e.g. reconciling sealed keys unbound longer than an hour)."""
    return (datetime.utcnow() - timedelta(seconds=max(0, seconds))).replace(
        microsecond=0).isoformat()


def _needs_naming(raw: Optional[str]) -> str:
    """Label a backfilled health system that has no real organization name.

    A historical partner row may carry only an internal user id (``u_abc123``)
    or a contact email. Neither is an organization, and rendering one in the
    Health Systems table as though it were makes the operator believe a hospital
    is called that. Mark it instead, so it is obviously waiting to be named."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    looks_internal = raw.lower().startswith(("u_", "u-")) or "@" in raw
    return f"Unnamed partner ({raw})" if looks_internal else raw


def _legacy_partner_name(label: str) -> Optional[str]:
    """The raw name a previous release would have used for this label, or None.

    Only meaningful for the ``Unnamed partner (…)`` form: it lets the boot
    migration find and rename a row created before C-5.6 instead of inserting a
    duplicate beside it."""
    m = re.fullmatch(r"Unnamed partner \((.+)\)", label or "")
    return m.group(1) if m else None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _empirical_difficulty_fields(ed):
    """Map a generation ``empirical_difficulty`` block → (value, measured_int) for the
    first-class task columns (PRD §9). Accepts a dict {value, declared, measured} or a
    bare number. The stored value is the MEASURED value when measured, else the declared
    value (for display); ``measured`` is 1 only for a live frontier measurement."""
    if isinstance(ed, (int, float)):
        return float(ed), 0
    if not isinstance(ed, dict):
        return None, 0
    measured = 1 if ed.get("measured") else 0
    val = ed.get("value")
    if val is None:
        val = ed.get("declared")
    try:
        val = float(val) if val is not None else None
    except (TypeError, ValueError):
        val = None
    return val, measured


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd.verify(plain, hashed)
    except Exception:
        return False


# ─── Credential vault sealing (Tier B at rest) ────────────────────────────────
# The private credential vault (Tier B: name, NPI, license, education) is sealed
# with Fernet when ``ASCLEPIUS_VAULT_KEY`` is set, so PHI-adjacent identifiers are
# encrypted at rest. Without a key (dev) we store JSON plaintext but flag it, so a
# deployment can tell whether the vault is actually encrypted.
import logging as _logging

_vault_log = _logging.getLogger("asclepius.vault")


def _vault_key() -> Optional[str]:
    raw = (os.getenv("ASCLEPIUS_VAULT_KEY") or "").strip()
    return raw or None


def seal_vault(data: Dict[str, Any]) -> tuple:
    """Serialize + (optionally) encrypt a Tier B credential dict. Returns
    ``(blob, encrypted_flag)``."""
    plain = json.dumps(data or {}, ensure_ascii=False)
    key = _vault_key()
    if key:
        try:
            from cryptography.fernet import Fernet

            token = Fernet(key.encode("utf-8")).encrypt(plain.encode("utf-8")).decode("utf-8")
            return token, 1
        except Exception:
            _vault_log.warning(
                "ASCLEPIUS_VAULT_KEY is set but Fernet sealing failed; storing the "
                "credential vault unencrypted. Verify the key is a valid urlsafe-base64 "
                "32-byte Fernet key.",
                exc_info=True,
            )
    return plain, 0


def open_vault(blob: Optional[str], encrypted: int) -> Dict[str, Any]:
    """Inverse of :func:`seal_vault`. Returns ``{}`` (and logs) if an encrypted
    blob cannot be opened (e.g. the key was rotated away)."""
    if not blob:
        return {}
    if encrypted:
        key = _vault_key()
        if not key:
            _vault_log.error("Encrypted credential vault present but ASCLEPIUS_VAULT_KEY is not set.")
            return {}
        try:
            from cryptography.fernet import Fernet

            plain = Fernet(key.encode("utf-8")).decrypt(blob.encode("utf-8")).decode("utf-8")
            return json.loads(plain)
        except Exception:
            _vault_log.error("Failed to open the encrypted credential vault.", exc_info=True)
            return {}
    try:
        return json.loads(blob)
    except Exception:
        return {}


# ═══ PRD-R ROUTING SQL — owned by Agent R, do not edit from other PRDs ═══════
# The labeler queue's eligibility and sort, as SQL fragments, defined ONCE so the
# classic queue (``next_task_for_evaluator``) and the value-aware candidate set
# (``eligible_tasks_for_evaluator``) cannot drift apart. Two copies of this rule
# is the same defect shape PRD R §7 names: two representations of one fact, one
# of which goes stale.

# Per-task submission counts, MATERIALIZED ONCE (Audit R H4).
#
# This used to be a correlated scalar subquery, textually inlined three times per
# query and joined by two more — five correlated scans per candidate row, on an
# unbounded fetch, on the single SQLite writer that labeler SUBMISSIONS also
# need. Measured at 20,000 open tasks: 0.167 s and 98.5 MB per labeler draw. No
# index can serve a sort over a computed expression, so PRD R §1.2's "add the
# index the sort needs" was not satisfiable as written; a grouped join is.
#
# ``n_labels`` counts VERDICT-BEARING rows only. A row without a verdict is a
# prompt flag or a draft, not a label, and must not count toward the pair —
# capacity, eligibility and the priority sort now all read this one number
# (Audit R M1: capacity used to count every row, so a single verdict-less write
# could wedge a case at "awaiting second" with nobody able to take it).
_PRD_R_COUNTS_JOIN = (
    "LEFT JOIN (SELECT task_id, COUNT(*) AS n_all, "
    "                  SUM(CASE WHEN verdict IS NOT NULL THEN 1 ELSE 0 END) AS n_labels "
    "             FROM submissions GROUP BY task_id) c ON c.task_id = t.task_id"
)
_PRD_R_LABEL_COUNT = "COALESCE(c.n_labels, 0)"

# The same count, as a correlated subquery, for the two queries that do not (and
# should not) carry the join: ``next_review_pair_for`` returns ONE task and is
# already windowed, and ``review_pair_queue_stats`` aggregates over its own
# derived table. Named separately so nobody reintroduces the H4 shape by reaching
# for the wrong constant in the hot path.
_PRD_R_LABEL_COUNT_CORRELATED = (
    "(SELECT COUNT(*) FROM submissions sv "
    " WHERE sv.task_id = t.task_id AND sv.verdict IS NOT NULL)"
)

# How many eligible candidates a single draw will consider. Applied AFTER every
# per-labeler exclusion is resolved in SQL, which is what makes it safe: the
# window counts only work this labeler could actually take, so it can never
# produce the empty queue the unbounded scan existed to avoid.
_PRD_R_SCAN_WINDOW = 300

# A task is servable to a labeler when it is open, OR when it is 'done' but
# carries exactly one label and has never been lifted to a pair. That second
# branch is the load-bearing one: ``refresh_task_status`` closes a max_labels=1
# task on its first submission (it is on the hot submit path and PRD R §7 forbids
# editing it), so between "TL#1 submits" and "something lifts max_labels" the
# case is 'done' and invisible. Deriving eligibility means the queue is correct
# on the very next draw instead of waiting on a sweep. The Python-side capacity
# check (``routing.effective_capacity``) is what decides whether the policy
# actually wants that second label — this clause only widens the candidate set.
_PRD_R_SERVABLE = (
    "(t.status = 'open' OR (t.status = 'done' AND COALESCE(t.max_labels, 1) < 2 "
    f"AND {_PRD_R_LABEL_COUNT} = 1))"
)

# PRD R §1.2 — a singly-labelled task outranks a fresh one, so cases finish
# instead of accumulating half-done and κ never filling. A DESC SORT, NOT A
# FILTER: an awaiting-second task this labeler cannot take (they wrote the first
# label) simply loses its place at the head, and the scan falls through to fresh
# work. The moment this becomes a WHERE clause, a labeler with no eligible
# second-label work sees an empty queue and stops working (PRD R §7).
_PRD_R_PRIORITY_ORDER = f"ORDER BY {_PRD_R_LABEL_COUNT} DESC, t.created_at ASC"
# ═══ END PRD-R ═══════════════════════════════════════════════════════════════


# ═══ PRD-2 §9.1 — the sequence gate (BLOCKER) ════════════════════════════════
# A trajectory point is servable to THIS evaluator only when every earlier point in
# its trajectory already carries a submission FROM THIS EVALUATOR.
#
# WHY THIS IS A WHERE CLAUSE AND NOT A SORT. The priority order directly above
# sorts on LABEL COUNT FIRST — a task carrying one label is offered before every
# unlabelled task, and ``created_at`` only breaks ties. Put patient-1's 13 decision
# points in that queue in sequence order and the ordinary behaviour of the sort
# breaks the seal the moment two physicians touch the same chart:
#
#   1. Physician A labels point 0.                     → point 0 has label_count 1
#   2. Physician B labels point 5, for any reason.     → point 5 has label_count 1
#   3. Physician A returns. Point 0 is excluded (they wrote it), so **point 5 sorts
#      first**, ahead of points 1–4, which are all still at 0.
#   4. Physician A is served encounter 5 — whose visible state block contains
#      encounters 1–4, the outcomes of the four decisions they were about to be
#      asked to predict.
#
# That is not a race condition and not an edge case. And it is unrecoverable: a
# physician cannot un-read a future, so the RLVR claim for their whole trajectory is
# gone the first time it happens. Sequence is therefore a CORRECTNESS property of
# the task, and it belongs in the query that decides servability — never in the
# frontend, which cannot enforce it against a hand-typed task id or a second tab.
# ``routers/asclepius`` applies the same rule to the direct-open path (409); both
# read ``trajectory.blocks_out_of_order`` for the sentence itself.
#
# ``t.trajectory_id IS NULL`` comes FIRST so every existing V1–V4 task short-circuits
# out of the correlated subquery entirely: unaffected by construction, and free.
#
# ``t.sequence_index IS NOT NULL`` is the second half of the same guarantee, and it
# is not defensive noise. SQL three-valued logic would make ``p.sequence_index <
# NULL`` evaluate to NULL for every earlier point, the NOT EXISTS would come back
# true, and a trajectory row with no readable position would be SERVED — while
# ``trajectory.blocks_out_of_order`` refuses that same row on the direct-open path.
# Two enforcements of one rule that disagree is worse than either alone, so the row
# is unservable here too. ``insert_task`` already refuses to create one.
_PRD_2_SEQUENCE_GATE = """(
              t.trajectory_id IS NULL
              OR (t.sequence_index IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM tasks p
                WHERE p.trajectory_id = t.trajectory_id
                  AND p.sequence_index < t.sequence_index
                  AND NOT EXISTS (
                    SELECT 1 FROM submissions s
                    WHERE s.task_id = p.task_id AND s.evaluator_id = ?
                  )
              ))
            )"""
# ═══ END PRD-2 ═══════════════════════════════════════════════════════════════


class AsclepiusStore:
    def __init__(self, db_path: Optional[str] = None):
        base_dir = os.path.dirname(__file__)
        # default lives next to the package, i.e. backend/asclepius.db
        default_path = os.path.join(os.path.dirname(base_dir), "asclepius.db")
        self.db_path = db_path or os.getenv("ASCLEPIUS_DB_PATH") or default_path
        # Create the parent dir so ASCLEPIUS_DB_PATH can point straight into a
        # mounted persistent volume (e.g. /data/asclepius.db) on first boot.
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # WAL = concurrent readers alongside a writer + writes that survive a
        # process crash / redeploy mid-request, so the labeled-data product is
        # never lost or corrupted. journal_mode persists on the file itself.
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    # ─── Connection ──────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        # busy_timeout: wait (don't error) if another request holds the write
        # lock — FastAPI serves requests from a threadpool against one file.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id              TEXT PRIMARY KEY,
                    email           TEXT NOT NULL UNIQUE,
                    password_hash   TEXT NOT NULL,
                    role            TEXT NOT NULL DEFAULT 'evaluator',
                    specialty       TEXT,
                    specialty_niche TEXT,
                    board_cert      TEXT,
                    years_experience INTEGER,
                    organization    TEXT,
                    id_hashed       TEXT,
                    active          INTEGER NOT NULL DEFAULT 1,
                    full_name       TEXT,
                    org_name        TEXT,
                    clinical_role   TEXT,
                    npi             TEXT,
                    credentials_json TEXT,
                    attestations_json TEXT,
                    created_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id         TEXT PRIMARY KEY,
                    specialty       TEXT NOT NULL DEFAULT 'general',
                    difficulty      TEXT NOT NULL DEFAULT 'medium',
                    capture_reasoning INTEGER NOT NULL DEFAULT 0,
                    source          TEXT NOT NULL DEFAULT 'lab_supplied',
                    prompt          TEXT NOT NULL,
                    candidate_answers_json TEXT NOT NULL DEFAULT '[]',
                    max_labels      INTEGER NOT NULL DEFAULT 1,
                    grounding_mode  TEXT NOT NULL DEFAULT 'optional',
                    independent_mode TEXT NOT NULL DEFAULT 'stance',
                    buyer_request_id TEXT,
                    generation_json TEXT,
                    value_tier      TEXT,
                    modality        TEXT NOT NULL DEFAULT 'text',
                    case_json       TEXT,
                    status          TEXT NOT NULL DEFAULT 'open',
                    created_by      TEXT,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_specialty ON tasks(specialty);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_buyer_req ON tasks(buyer_request_id);

                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id   TEXT PRIMARY KEY,
                    task_id         TEXT NOT NULL,
                    evaluator_id    TEXT NOT NULL,
                    verdict         TEXT,
                    chosen_id       TEXT,
                    rejected_id     TEXT,
                    confidence      TEXT,
                    time_spent_sec  INTEGER NOT NULL DEFAULT 0,
                    status          TEXT NOT NULL DEFAULT 'submitted',
                    dedupe_hash     TEXT,
                    grounded        INTEGER NOT NULL DEFAULT 0,
                    grounding_mode  TEXT NOT NULL DEFAULT 'optional',
                    portal_version  TEXT NOT NULL DEFAULT 'v2',
                    caught_flaw     INTEGER,
                    payload_json    TEXT NOT NULL DEFAULT '{}',
                    validation_json TEXT,
                    critic_json     TEXT,
                    qa_json         TEXT,
                    qa_reason       TEXT,
                    agreement_score REAL,
                    annotator_json  TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sub_task ON submissions(task_id);
                CREATE INDEX IF NOT EXISTS idx_sub_status ON submissions(status);
                CREATE INDEX IF NOT EXISTS idx_sub_evaluator ON submissions(evaluator_id);
                CREATE INDEX IF NOT EXISTS idx_sub_dedupe ON submissions(dedupe_hash);

                CREATE TABLE IF NOT EXISTS records (
                    record_id       TEXT PRIMARY KEY,
                    submission_id   TEXT NOT NULL,
                    task_id         TEXT NOT NULL,
                    type            TEXT NOT NULL,
                    specialty       TEXT,
                    status          TEXT NOT NULL DEFAULT 'submitted',
                    payload_json    TEXT NOT NULL,
                    export_id       TEXT,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rec_submission ON records(submission_id);
                CREATE INDEX IF NOT EXISTS idx_rec_status ON records(status);
                CREATE INDEX IF NOT EXISTS idx_rec_type ON records(type);

                CREATE TABLE IF NOT EXISTS events (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type     TEXT NOT NULL,
                    entity_id       TEXT,
                    event_type      TEXT NOT NULL,
                    actor           TEXT,
                    occurred_at     TEXT NOT NULL,
                    payload_json    TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id);
                CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at);

                CREATE TABLE IF NOT EXISTS exports (
                    export_id       TEXT PRIMARY KEY,
                    created_by      TEXT,
                    created_at      TEXT NOT NULL,
                    record_count    INTEGER NOT NULL DEFAULT 0,
                    filters_json    TEXT,
                    dir_path        TEXT,
                    manifest_json   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_exports_created ON exports(created_at);

                CREATE TABLE IF NOT EXISTS buyers (
                    buyer_id        TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    contact         TEXT,
                    export_profile  TEXT NOT NULL DEFAULT 'default',
                    notes           TEXT,
                    created_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS buyer_requests (
                    request_id      TEXT PRIMARY KEY,
                    buyer_id        TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'draft',
                    source          TEXT NOT NULL DEFAULT 'internal_prompt_bank',
                    export_profile  TEXT NOT NULL DEFAULT 'default',
                    constraints_json TEXT NOT NULL DEFAULT '{}',
                    uploaded_json   TEXT NOT NULL DEFAULT '[]',
                    note            TEXT,
                    created_by      TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_buyer_requests_buyer ON buyer_requests(buyer_id);
                CREATE INDEX IF NOT EXISTS idx_buyer_requests_status ON buyer_requests(status);

                -- Per-task inter-annotator agreement observation (opt §1.3). One
                -- row per double-labeled task; the aggregate Cohen's kappa is
                -- computed across these observations.
                CREATE TABLE IF NOT EXISTS agreement (
                    task_id         TEXT PRIMARY KEY,
                    specialty       TEXT,
                    sub_a           TEXT,
                    sub_b           TEXT,
                    verdict_a       TEXT,
                    verdict_b       TEXT,
                    tags_a_json     TEXT,
                    tags_b_json     TEXT,
                    jaccard_tags    REAL,
                    verdict_agree   INTEGER NOT NULL DEFAULT 0,
                    n_labels        INTEGER NOT NULL DEFAULT 0,
                    flagged         INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agreement_specialty ON agreement(specialty);

                -- Seedmaker auto-generation jobs (PRD §9.2): one row per
                -- ``generate_tasks`` run for the admin dashboard + auditing.
                CREATE TABLE IF NOT EXISTS generation_jobs (
                    job_id          TEXT PRIMARY KEY,
                    specialty       TEXT NOT NULL,
                    requested_n     INTEGER NOT NULL DEFAULT 0,
                    accepted        INTEGER NOT NULL DEFAULT 0,
                    dropped_json    TEXT NOT NULL DEFAULT '{}',
                    params_json     TEXT NOT NULL DEFAULT '{}',
                    created_by      TEXT,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_genjobs_specialty ON generation_jobs(specialty);
                CREATE INDEX IF NOT EXISTS idx_genjobs_created ON generation_jobs(created_at);

                -- V4 image asset index (V4 Image Embedding PRD §4). Resolves an
                -- asset_id → sha256/mime/owning-task in ONE indexed lookup so serving
                -- an image never scans the tasks table. The image BYTES live in the
                -- content-addressed asset store, never here — only the reference.
                CREATE TABLE IF NOT EXISTS study_assets (
                    asset_id    TEXT PRIMARY KEY,
                    sha256      TEXT NOT NULL,
                    mime        TEXT NOT NULL,
                    task_id     TEXT,
                    case_source TEXT,
                    created_at  TEXT NOT NULL
                );

                -- Contributor credential vault (Contributors view + tiered export).
                -- Keyed by the same hashed annotator id that stamps every record, so
                -- a dossier (Tier B) matches the exact shipped records (Tier A).
                --   ship_json   = Tier A attributes (buyer-facing; safe to ship)
                --   verify_blob = Tier B identifying credentials (the private vault;
                --                 Fernet-sealed when ASCLEPIUS_VAULT_KEY is set)
                CREATE TABLE IF NOT EXISTS contributor_credentials (
                    id_hashed            TEXT PRIMARY KEY,
                    user_id              TEXT,
                    organization         TEXT,
                    role_title           TEXT,
                    blurb                TEXT,
                    credentials_verified INTEGER NOT NULL DEFAULT 0,
                    ship_json            TEXT NOT NULL DEFAULT '{}',
                    verify_blob          TEXT,
                    verify_enc           INTEGER NOT NULL DEFAULT 0,
                    created_at           TEXT NOT NULL,
                    updated_at           TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cred_org ON contributor_credentials(organization);
                CREATE INDEX IF NOT EXISTS idx_cred_user ON contributor_credentials(user_id);

                -- Independent-answer reveal gate (Eval Flow Upgrade §1, v2 anti-
                -- peeking). One row per (task, evaluator) proving the evaluator
                -- committed their blind independent answer BEFORE any candidate
                -- answer text was revealed. The reveal endpoints refuse to return
                -- answer text without this row, and the committed answer is the
                -- authoritative one packaged — so it is provably pre-reveal.
                CREATE TABLE IF NOT EXISTS independent_commits (
                    task_id       TEXT NOT NULL,
                    evaluator_id  TEXT NOT NULL,
                    payload_json  TEXT NOT NULL DEFAULT '{}',
                    created_at    TEXT NOT NULL,
                    PRIMARY KEY (task_id, evaluator_id)
                );
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """Additive column migrations for existing ``asclepius.db`` files so the
        data-optimization fields land without dropping prior data."""
        with self._conn() as conn:
            def cols(table: str) -> set:
                return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

            # ── Real EHR ingestion (EHR PRD §4, §5, §8) — new tables (idempotent).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_upload_links (
                    link_id       TEXT PRIMARY KEY,
                    token_hash    TEXT NOT NULL UNIQUE,   -- SHA-256; raw token never stored
                    partner_id    TEXT NOT NULL,
                    partner_label TEXT,
                    specialty     TEXT NOT NULL DEFAULT 'nephrology',
                    expires_at    TEXT NOT NULL,
                    one_time      INTEGER NOT NULL DEFAULT 1,
                    max_bytes     INTEGER NOT NULL DEFAULT 104857600,
                    used_count    INTEGER NOT NULL DEFAULT 0,
                    revoked       INTEGER NOT NULL DEFAULT 0,
                    created_by    TEXT,
                    created_at    TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                -- Data-provider ACCOUNTS (email + password door, EHR PRD §4 —
                -- complementary to the magic-link door). The account itself lives
                -- in ``users`` (role='data_partner'); this row carries the invite /
                -- upload lifecycle + relationship metadata the admin sees. Uploads
                -- still flow through the shared ingest_uploads pipeline (partner_id
                -- = this provider_id), so there is ONE inbox for both doors.
                CREATE TABLE IF NOT EXISTS data_providers (
                    provider_id         TEXT PRIMARY KEY,   -- = users.id
                    email               TEXT NOT NULL UNIQUE,
                    org_name            TEXT,
                    specialty           TEXT,
                    note                TEXT,
                    status              TEXT NOT NULL DEFAULT 'invited', -- invited|active|revoked
                    must_reset_password INTEGER NOT NULL DEFAULT 1,
                    invited_by          TEXT,
                    invited_at          TEXT,
                    invite_expires_at   TEXT,
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL
                )
                """
            )
            # PRD-I §2.2 applied to the FOURTH upload door. Every other door
            # resolves an upload's purpose from the row that authorized it — a
            # portal account, a chunked session, a magic link. The provider
            # account door had no such column to join, so it called
            # attach_upload_provenance not at all and every byte it accepted
            # landed with purpose NULL, which the promotion gate reads as task
            # creation. Nullable, like the others: NULL is "nobody has decided",
            # an admin work item, not a default.
            if "purpose" not in cols("data_providers"):
                conn.execute("ALTER TABLE data_providers ADD COLUMN purpose TEXT")
            conn.execute(
                """
                -- Buyer ACCOUNTS for the secure data workspace. The account itself
                -- lives in ``users`` (role='buyer'); this row carries the invite /
                -- workspace lifecycle metadata the admin sees. Data delivered to a
                -- buyer is recorded in ``buyer_deliveries`` and always appears in
                -- their workspace when they sign in.
                CREATE TABLE IF NOT EXISTS buyer_accounts (
                    buyer_account_id    TEXT PRIMARY KEY,   -- = users.id
                    email               TEXT NOT NULL UNIQUE,
                    buyer_name          TEXT,
                    note                TEXT,
                    status              TEXT NOT NULL DEFAULT 'invited', -- invited|active|revoked
                    must_reset_password INTEGER NOT NULL DEFAULT 1,
                    invited_by          TEXT,
                    invited_at          TEXT,
                    invite_expires_at   TEXT,
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                -- One row per dataset delivered to a buyer. Joins a built export
                -- (exports.export_id) to a buyer account, so "data sent to that
                -- email always appears in their workspace" falls out of a lookup.
                CREATE TABLE IF NOT EXISTS buyer_deliveries (
                    delivery_id       TEXT PRIMARY KEY,
                    buyer_account_id  TEXT NOT NULL,
                    buyer_email       TEXT NOT NULL,
                    export_id         TEXT NOT NULL,
                    label             TEXT,
                    data_format       TEXT,
                    record_count      INTEGER NOT NULL DEFAULT 0,
                    note              TEXT,
                    sent_by           TEXT,
                    sent_at           TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_buyer_deliveries_acct ON buyer_deliveries(buyer_account_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_buyer_deliveries_email ON buyer_deliveries(buyer_email)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_uploads (
                    upload_id   TEXT PRIMARY KEY,
                    link_id     TEXT NOT NULL,
                    partner_id  TEXT NOT NULL,
                    filename    TEXT,
                    sha256      TEXT,
                    size_bytes  INTEGER,
                    status      TEXT NOT NULL DEFAULT 'received',
                    reason      TEXT,
                    files_json  TEXT,           -- per-entry classification/outcome
                    raw_path    TEXT,           -- encrypted quarantine blob on disk
                    source_ip   TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_cases (
                    ingest_case_id TEXT PRIMARY KEY,
                    upload_id      TEXT NOT NULL,
                    patient_key    TEXT,
                    specialty      TEXT,
                    case_json      TEXT,
                    status         TEXT NOT NULL DEFAULT 'ingested',
                    report_json    TEXT,        -- timeline + verify findings (masked)
                    override_reason TEXT,
                    task_id        TEXT,        -- set on promote
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_cases_upload ON ingest_cases(upload_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_cases_status ON ingest_cases(status)")
            # Serving hot path (Audit §21.6): next_task_for_evaluator /
            # eligible_tasks_for_evaluator run a NOT EXISTS correlated on ic.task_id to
            # exclude blocking-review cases. Index task_id so that stays O(log n).
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_cases_task ON ingest_cases(task_id)")

            # ── Sealed ground truth (Buyer Response PRD §3 B1) ───────────────
            # The partner's adjudicated answer key, held SEPARATELY from the case,
            # ENCRYPTED at rest (field_crypto), keyed to the ingest case, readable
            # only by the adjudication surface, audited on ingest and on every read.
            # It must never enter the case body, task.prompt, or any export profile.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sealed_ground_truth (
                    sealed_id      TEXT PRIMARY KEY,
                    -- NULLABLE on purpose (Audit §H1): the key is STAGED first, keyed
                    -- on (upload_id, patient_key), then bound to the case row once it
                    -- exists. A crash between the two leaves the key on disk unbound,
                    -- never an ingested case with no answer key.
                    ingest_case_id TEXT,
                    upload_id      TEXT,
                    patient_key    TEXT,            -- staging key before binding
                    payload_enc    TEXT NOT NULL,   -- field_crypto-encrypted JSON
                    created_at     TEXT NOT NULL,
                    bound_at       TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sealed_case ON sealed_ground_truth(ingest_case_id)")
            # NOTE: the (upload_id, patient_key) staging index is created in _migrate,
            # AFTER the guarded rebuild adds patient_key — an existing DB has not been
            # migrated yet at this point, so the column may not exist here.

            # ── Frontier-model failure capture (FEAT-1) ──────────────────────
            # ``baseline_runs``: a frontier model's VERBATIM cold answer to a case,
            # the on-policy artifact that proves a case is hard.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS baseline_runs (
                    run_id        TEXT PRIMARY KEY,
                    task_id       TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    provider      TEXT,
                    prompt_hash   TEXT,
                    response_text TEXT,
                    error         TEXT,
                    latency_ms    INTEGER,
                    tokens_in     INTEGER,
                    tokens_out    INTEGER,
                    created_at    TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_baseline_runs_task ON baseline_runs(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_baseline_runs_model ON baseline_runs(model)")
            _br_cols = cols("baseline_runs")
            if "provider" not in _br_cols:
                conn.execute("ALTER TABLE baseline_runs ADD COLUMN provider TEXT")
            if "prompt_hash" not in _br_cols:
                conn.execute("ALTER TABLE baseline_runs ADD COLUMN prompt_hash TEXT")
            # ``model_failures``: the per-model failure record computed AFTER a
            # specialist grades a real-model A/B pair — which model was rejected,
            # which error tags applied, which steps were wrong, + the correction.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_failures (
                    failure_id      TEXT PRIMARY KEY,
                    task_id         TEXT NOT NULL,
                    submission_id   TEXT NOT NULL,
                    model           TEXT NOT NULL,
                    provider        TEXT,
                    verdict         TEXT,
                    error_tags_json TEXT NOT NULL DEFAULT '[]',
                    corrected_steps_json TEXT NOT NULL DEFAULT '[]',
                    expert_correction    TEXT,
                    prompt          TEXT,
                    created_at      TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model_failures_model ON model_failures(model)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model_failures_task ON model_failures(task_id)")
            if "provider" not in cols("model_failures"):
                conn.execute("ALTER TABLE model_failures ADD COLUMN provider TEXT")

            # ── V5 Clinical RL Environments (Clinical RL Environments PRD §10).
            # ONE row per environment OR per rollout run, keyed by run_id. A
            # ``mode='generated'`` row is a compiled environment (compiled_json set,
            # no trajectory); a ``mode='rollout'`` row is one agent trajectory over
            # that environment (provider + trajectory_json set), sharing task_id.
            # Fully additive; never touches the V1–V4 single-turn task queue.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS env_runs (
                    run_id                   TEXT PRIMARY KEY,
                    task_id                  TEXT NOT NULL,   -- the environment id (shared across providers)
                    specialty                TEXT NOT NULL DEFAULT 'general',
                    task_type                TEXT NOT NULL DEFAULT 'diagnostic_workup',
                    case_id                  TEXT,
                    case_source              TEXT NOT NULL DEFAULT 'gold',
                    provider                 TEXT,
                    model                    TEXT,
                    ab_source                TEXT,
                    mode                     TEXT NOT NULL DEFAULT 'generated',
                    compiled_json            TEXT,            -- the compiled environment spec (§8.4)
                    trajectory_json          TEXT,            -- the §1 record's trajectory
                    verification_json        TEXT,            -- §5 reward block
                    provenance_json          TEXT,
                    physician_annotation_json TEXT,           -- §7 latest/primary annotation
                    annotations_json         TEXT,            -- ALL annotator submissions (κ subset)
                    empirical_difficulty     REAL,
                    difficulty_measured      INTEGER NOT NULL DEFAULT 0,
                    passes_difficulty_gate   INTEGER,
                    created_at               TEXT NOT NULL,
                    updated_at               TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_env_runs_task ON env_runs(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_env_runs_specialty ON env_runs(specialty)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_env_runs_mode ON env_runs(mode)")
            # annotations_json added after the table shipped — guard for existing DBs.
            if "annotations_json" not in cols("env_runs"):
                conn.execute("ALTER TABLE env_runs ADD COLUMN annotations_json TEXT")

            task_cols = cols("tasks")
            if "grounding_mode" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN grounding_mode TEXT NOT NULL DEFAULT 'optional'")
            if "buyer_request_id" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN buyer_request_id TEXT")
            if "generation_json" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN generation_json TEXT")
            if "independent_mode" not in task_cols:
                # Speed Optimization §1: ``independent_mode`` is the ADMIN's
                # per-task intent — 'stance' (quick take, the default) or 'full'
                # (long-form blind ideal, premium/eval batches). Pre-existing
                # rows default to 'stance' BY DESIGN: the product requirement is
                # that legacy tasks read as stance in V2. This is not a silent
                # data loss — a premium blind ideal answer is still produced
                # whenever the contributor selects the V1 (classic) experience
                # (``_independent_kind`` forces 'full' for v1) OR the admin marks
                # the task ``independent_mode='full'`` (honored in V2). Only the
                # DEFAULT capture on an unmarked task in the DEFAULT (v2)
                # experience is the quick stance.
                conn.execute("ALTER TABLE tasks ADD COLUMN independent_mode TEXT NOT NULL DEFAULT 'stance'")

            sub_cols = cols("submissions")
            if "portal_version" not in sub_cols:
                # Asclepius V2 launch: which evaluator flow produced the row
                # (v1 classic | v2 assisted). Rows written before this column
                # existed were all the classic flow, so backfill them to 'v1'.
                conn.execute("ALTER TABLE submissions ADD COLUMN portal_version TEXT NOT NULL DEFAULT 'v2'")
                conn.execute("UPDATE submissions SET portal_version = 'v1'")

            user_cols = cols("users")
            if "organization" not in user_cols:
                conn.execute("ALTER TABLE users ADD COLUMN organization TEXT")

            sub_cols = cols("submissions")
            if "grounded" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN grounded INTEGER NOT NULL DEFAULT 0")
            if "grounding_mode" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN grounding_mode TEXT NOT NULL DEFAULT 'optional'")
            if "caught_flaw" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN caught_flaw INTEGER")

            # Value-per-Minute (PRD Part A): the estimated sellable value of a
            # judgment + the clinician-minutes it took, persisted per submission
            # so V/T is reported next to κ. Purely additive measurement columns —
            # no existing flow (v1 or v2) reads them; NULL on legacy rows means
            # "not yet estimated" and the metrics endpoint skips them.
            if "value_estimate_usd" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN value_estimate_usd REAL")
            if "value_estimate_projected_usd" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN value_estimate_projected_usd REAL")
            if "clinician_review_seconds" not in sub_cols:
                conn.execute("ALTER TABLE submissions ADD COLUMN clinician_review_seconds INTEGER")
            if "progress_json" not in sub_cols:
                # Real submit progress (BUG-5): the backend stamps {phase, pct,
                # detail} onto the row as each pipeline stage ACTUALLY starts, so
                # the client polls a truthful phase — never an invented percentage.
                conn.execute("ALTER TABLE submissions ADD COLUMN progress_json TEXT")

            if "value_tier" not in task_cols:
                # Optional admin routing hint (Value-per-Minute PRD B3). Additive;
                # NULL means "unspecified" and routing scores from attributes.
                conn.execute("ALTER TABLE tasks ADD COLUMN value_tier TEXT")
            if "modality" not in task_cols:
                # Multimodal clinical cases (Synthetic Multimodal Cases PRD). Additive;
                # 'text' (default) is today's one-line prompt, 'multimodal' carries a case.
                conn.execute("ALTER TABLE tasks ADD COLUMN modality TEXT NOT NULL DEFAULT 'text'")
            if "case_json" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN case_json TEXT")
            # Decisive action (Buyer Response PRD §9.2 / Audit §13): the physician-named
            # verifiable outcome that turns a preference label into an RLVR reward.
            # supervision.DecisiveAction + packaging read it, but nothing wrote it —
            # so has_verifiable_outcome was false on every record. Persisted from the
            # submission, keyed to the task.
            if "decisive_action_json" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN decisive_action_json TEXT")
            # Buyer Response PRD §7 F1: an agreement observation records whether the
            # second annotator was BLINDED. Only blinded observations enter the κ
            # computation (an unblinded second rater measures anchoring, not
            # agreement). NULLABLE with NO DEFAULT (Audit §H2): a legacy row whose
            # blinding was never verified stays NULL and is EXCLUDED from κ — not
            # silently asserted blinded. Every new observation is written with an
            # explicit 0/1 by insert_agreement, so only pre-flag rows are NULL.
            if "blinded" not in cols("agreement"):
                conn.execute("ALTER TABLE agreement ADD COLUMN blinded INTEGER")

            # Sealed-key ordering (Audit §H1). The original table declared
            # ingest_case_id NOT NULL, which forced "insert case → then store key" and
            # left a crash-window where an ingested case had no answer key. Relax it to
            # nullable + add the (upload_id, patient_key) staging columns by rebuilding
            # the table (SQLite can't ALTER a NOT NULL away).
            #
            # CRASH-IDEMPOTENT by construction: DDL auto-commits in sqlite3 (it does NOT
            # roll back with the surrounding transaction), so a rebuild interrupted at
            # boot must self-heal on the next boot without ever losing a sealed key. We
            # build a ``_new`` copy (a COMPLETE copy — CREATE + INSERT run back-to-back),
            # then drop+rename. Recovery invariant: whichever of the two tables HAS ROWS
            # is authoritative. (Note ``_init_schema`` runs before this and recreates a
            # missing ``sealed_ground_truth`` as an empty new-schema table, so "original
            # gone" presents as "main table empty" — hence the row-count test, not a
            # table-existence test.)
            tbls = {r["name"] for r in
                    conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "sealed_ground_truth_new" in tbls:
                main_n = (conn.execute("SELECT COUNT(*) FROM sealed_ground_truth").fetchone()[0]
                          if "sealed_ground_truth" in tbls else 0)
                if main_n == 0:
                    # Main is empty — either a fresh recreation (the keys are in _new)
                    # or there were never any keys; swap _new in, losing nothing.
                    conn.execute("DROP TABLE IF EXISTS sealed_ground_truth")
                    conn.execute("ALTER TABLE sealed_ground_truth_new RENAME TO sealed_ground_truth")
                else:
                    # Main holds the pre-migration rows → it is authoritative; _new is a
                    # partial/stale copy. Discard it and let the rebuild below redo it.
                    conn.execute("DROP TABLE sealed_ground_truth_new")
            if "patient_key" not in cols("sealed_ground_truth"):
                conn.execute(
                    """
                    CREATE TABLE sealed_ground_truth_new (
                        sealed_id      TEXT PRIMARY KEY,
                        ingest_case_id TEXT,
                        upload_id      TEXT,
                        patient_key    TEXT,
                        payload_enc    TEXT NOT NULL,
                        created_at     TEXT NOT NULL,
                        bound_at       TEXT
                    )
                    """
                )
                conn.execute(
                    """INSERT INTO sealed_ground_truth_new
                       (sealed_id, ingest_case_id, upload_id, patient_key, payload_enc,
                        created_at, bound_at)
                       SELECT sealed_id, ingest_case_id, upload_id, NULL, payload_enc,
                              created_at, created_at
                         FROM sealed_ground_truth"""
                )
                # Drop-then-rename: if a crash lands between these two, the recovery
                # clause above finishes the rename next boot (keys are safe in _new).
                conn.execute("DROP TABLE sealed_ground_truth")
                conn.execute("ALTER TABLE sealed_ground_truth_new RENAME TO sealed_ground_truth")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sealed_case ON sealed_ground_truth(ingest_case_id)")
            # The staging index is created here (not in _init_schema) so it lands after
            # the column exists on both fresh (created above) and migrated DBs. Idempotent.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sealed_stage ON sealed_ground_truth(upload_id, patient_key)")

            # Admin review queue (Audit PRD §21). review_status is NULLABLE on purpose:
            # NULL means "no check raised anything", which is NOT "reviewed and cleared"
            # — the same fail-open lesson as the blinded flag; never backfill a verdict
            # onto rows that were never assessed.
            if "review_status" not in cols("ingest_cases"):
                conn.execute("ALTER TABLE ingest_cases ADD COLUMN review_status TEXT")     # NULL|needs_review|cleared|rejected
                conn.execute("ALTER TABLE ingest_cases ADD COLUMN review_json TEXT")       # [{reason,severity,detail,raised_at}]
                conn.execute("ALTER TABLE ingest_cases ADD COLUMN reviewed_by_hashed TEXT")
                conn.execute("ALTER TABLE ingest_cases ADD COLUMN reviewed_at TEXT")

            if "case_source" not in task_cols:
                # Real EHR Ingestion PRD §9.5: 'synthetic' | 'real_deid' as a first-
                # class COLUMN so the V4 routing wall filters in SQL (a real case is
                # only ever served to a v4 session). NULL = text task (no case).
                # Backfill existing multimodal rows from their stored case.
                conn.execute("ALTER TABLE tasks ADD COLUMN case_source TEXT")
                for r in conn.execute(
                    "SELECT task_id, case_json FROM tasks WHERE case_json IS NOT NULL"
                ).fetchall():
                    try:
                        cs = (json.loads(r["case_json"]) or {}).get("case_source") or "synthetic"
                    except Exception:
                        cs = "synthetic"
                    conn.execute("UPDATE tasks SET case_source = ? WHERE task_id = ?", (cs, r["task_id"]))
            # Specialty Hyper-Personalization PRD §9: the frontier-model failure rate
            # (wrong answer OR wrong reasoning). First-class columns so the serving gate
            # filters in SQL and admin/export can surface it. ``difficulty_measured`` =
            # 1 only when LIVE-measured; a declared/authored value carries the numeric
            # for display but 0 here. Each ALTER is guarded independently so a crash
            # between the two can't leave insert_task referencing a missing column.
            if "empirical_difficulty" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN empirical_difficulty REAL")
            if "difficulty_measured" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN difficulty_measured INTEGER NOT NULL DEFAULT 0")
            if "empirical_difficulty" not in task_cols or "difficulty_measured" not in task_cols:
                for r in conn.execute(
                    "SELECT task_id, generation_json FROM tasks WHERE generation_json IS NOT NULL"
                ).fetchall():
                    try:
                        ed = (json.loads(r["generation_json"]) or {}).get("empirical_difficulty")
                        val, measured = _empirical_difficulty_fields(ed)
                    except Exception:
                        val, measured = None, 0
                    if val is not None or measured:
                        conn.execute(
                            "UPDATE tasks SET empirical_difficulty = ?, difficulty_measured = ? WHERE task_id = ?",
                            (val, measured, r["task_id"]),
                        )

            # Rich credential record provisioned by the Asclepius onboarding flow.
            user_cols = cols("users")
            for col, decl in (
                ("full_name", "TEXT"),
                ("org_name", "TEXT"),
                ("clinical_role", "TEXT"),
                ("npi", "TEXT"),
                # Optional free-text specialty niche / case-type description
                # captured in onboarding. Descriptive metadata only (same
                # treatment as subspecialties), never a tiering/scoring input.
                ("specialty_niche", "TEXT"),
                ("credentials_json", "TEXT"),
                ("attestations_json", "TEXT"),
                # Mock/sandbox contributor (internal demo tool): submissions are
                # HARD-EXCLUDED from real exports by default so a demo can exercise
                # the live portal without contaminating a shipped training batch.
                ("is_mock", "INTEGER NOT NULL DEFAULT 0"),
                # Real-data access gate (EHR PRD §9.5): V4 (real de-identified
                # cases) is served ONLY to contributors flagged approved (BAA /
                # training complete). Default off for everyone.
                ("real_data_approved", "INTEGER NOT NULL DEFAULT 0"),
                # WHO decided that flag, which the flag alone cannot say.
                # ``real_data_approved`` is NOT NULL DEFAULT 0, so "never
                # considered" and "an admin revoked this" are the same 0 — and an
                # auto-grant that cannot tell them apart would re-grant access to
                # someone a human deliberately revoked, on every sync.
                #   NULL              — nobody has decided
                #   'admin'           — a human decided; auto-grant must not touch it
                #   'auto:<policy>'   — derived; a later sync may revise it
                ("real_data_approval_source", "TEXT"),
                # First-run tutorial ("Calibration Case 1") state. JSON blob —
                # {status, step, version, started_at, completed_at, skipped_at,
                # score} — because the shape evolves and nothing filters on it
                # in SQL (same reasoning as credentials_json above).
                ("tutorial_json", "TEXT"),
            ):
                if col not in user_cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")

            # ── Organization backfill (BUG-6) ────────────────────────────────
            # A contributor whose ``organization`` is NULL fell out of EVERY org
            # grouping in Exports/Metrics — their labeled records existed but
            # appeared nowhere (the worst admin failure mode). Backfill a stable
            # org for every existing contributor so their historical submissions
            # (resolved via this users row at read time) group correctly:
            #   * the mock/demo account collapses to 'mockadmin';
            #   * else the onboarding-collected org_name;
            #   * else the account email's local-part (a stable, non-null bucket).
            # Idempotent: only touches NULL/blank organizations, so it no-ops on
            # every boot after the first.
            conn.execute(
                "UPDATE users SET organization = 'mockadmin' "
                "WHERE is_mock = 1 AND (organization IS NULL OR TRIM(organization) = '')"
            )
            conn.execute(
                """
                UPDATE users SET organization = CASE
                    WHEN org_name IS NOT NULL AND TRIM(org_name) != '' THEN org_name
                    WHEN instr(email, '@') > 1 THEN substr(email, 1, instr(email, '@') - 1)
                    ELSE email
                END
                WHERE organization IS NULL OR TRIM(organization) = ''
                """
            )

            # EHR ingestion: an optional sender contact email on a link, so a
            # failed upload can notify the partner who sent it; plus a stamp on the
            # upload to dedupe the auto-notification. Both additive/nullable.
            if "contact_email" not in cols("ingest_upload_links"):
                conn.execute("ALTER TABLE ingest_upload_links ADD COLUMN contact_email TEXT")
            if "failure_notified_at" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN failure_notified_at TEXT")
            # Retain-raw (Audit §9.4): when every entry in a bundle failed to parse,
            # the raw blob is kept past the normal retention window so it can be re-run
            # after a parser fix rather than purged on schedule.
            if "retain_raw" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN retain_raw INTEGER NOT NULL DEFAULT 0")

            # ═══ PRD-B IDENTITY SCHEMA — owned by Agent 2, do not edit from other PRDs ═══
            # Verification / credentialing / tiering columns (§4 shared contract).
            # All nullable and deliberately WITHOUT defaults: on every status
            # column NULL means "not yet checked / not yet decided" and must stay
            # distinguishable from a decided value — a DEFAULT here would mark
            # every pre-existing user as pending/approved, which is wrong.
            for _col, _ddl in (
                # Stamped whenever the password hash is deliberately rewritten
                # (chosen at onboarding, reset, or changed). NULL for every
                # account that predates the chosen-password flow, which is what
                # makes the token-revocation check below a no-op for them
                # rather than a mass logout on deploy.
                ("password_changed_at", "TEXT"),
                ("phone",               "TEXT"),
                # Enumerated ONCE in asclepius/capabilities.py (TIERS). A stale
                # comment here is how the next person reintroduces a two-state
                # check, so it is kept in sync deliberately.
                ("tier",                "TEXT"),     # labeler | reviewer | advisor | NULL(unassigned)
                ("tier_score",          "REAL"),
                ("tier_assigned_at",    "TEXT"),
                ("tier_assigned_by",    "TEXT"),
                ("verification_status", "TEXT"),     # pending | approved | rejected | NULL
                ("verification_notes",  "TEXT"),
                ("verified_by",         "TEXT"),
                ("verified_at",         "TEXT"),
                ("npi_verified",        "INTEGER"),  # 1 | 0 | NULL(not checked)
                ("npi_payload_json",    "TEXT"),
                ("npi_checked_at",      "TEXT"),
                ("email_domain_class",  "TEXT"),     # academic | business | consumer
                ("linkedin_url",        "TEXT"),
                ("cv_asset_sha",        "TEXT"),
                # PRD-B extension beyond the §4 contract: cache of the parsed-CV
                # suggestions so the admin dossier doesn't re-parse (and possibly
                # re-OCR) the document on every view. Advisory data only.
                ("cv_parsed_json",      "TEXT"),
                ("health_system_id",    "TEXT"),     # FK -> health_systems (PRD-C table)
                ("slack_joined",        "INTEGER"),
                ("slack_checked_at",    "TEXT"),
                # F6: a non-definitive NPI check is an ATTEMPT, not a result.
                # It is logged here instead of overwriting npi_payload_json /
                # npi_verified, so a failed recheck can never erase evidence we
                # already hold. Also drives the retry sweep.
                ("npi_last_attempt_json", "TEXT"),
                ("npi_last_attempt_at",   "TEXT"),
                # ── International credentials ────────────────────────────────
                # NULL means a row written before signup asked, and everyone
                # who signed up then was a US physician. Every reader treats
                # NULL as "US" rather than backfilling, so no migration has to
                # guess at a country it was never told.
                ("country_of_practice",   "TEXT"),   # ISO 3166-1 alpha-2
                ("country_of_licensure",  "TEXT"),
                # The non-US twin of ``npi``: SCFHS number, state council
                # registration, GMC reference. Kept in its own column so the
                # NPI column keeps meaning exactly one thing.
                ("registry_id",           "TEXT"),
                ("registry_verified",     "INTEGER"),  # 1 | 0 | NULL(not checked)
                ("registry_payload_json", "TEXT"),
                ("registry_checked_at",   "TEXT"),
                # Same rule as the NPI attempt columns: a non-definitive check
                # is an attempt, and must never overwrite evidence we hold.
                ("registry_last_attempt_json", "TEXT"),
                ("registry_last_attempt_at",   "TEXT"),
                # The registration certificate, for the countries whose
                # registers we cannot query at all.
                ("license_doc_sha",       "TEXT"),
                ("license_doc_review_json", "TEXT"),
                # ── Signup review flags ──────────────────────────────────────
                # Set when the signup does not hold together: gibberish in the
                # free-text fields, a timeline that cannot happen, a licence
                # number in no recognizable shape. A flag routes to a human and
                # is never, on its own, a rejection.
                ("flagged",               "INTEGER"),  # 1 | 0 | NULL(not assessed)
                ("flags_json",            "TEXT"),
                # What KIND of account this is, from the link they signed up
                # through. NULL is a physician, which is who everyone was
                # before there was more than one door. 'advisor' is a
                # non-clinical supporter; 'referrer' holds a referral link and
                # nothing else. Read by capabilities.surfaces().
                ("account_kind",          "TEXT"),
            ):
                if _col not in cols("users"):
                    conn.execute(f"ALTER TABLE users ADD COLUMN {_col} {_ddl}")

            # ── Tier backfill for pre-tiering accounts ───────────────────────
            # ``capabilities.LABEL`` is now ENFORCED at /tasks/next and
            # /submissions. It was defined and never checked, so those endpoints
            # gated on authentication alone and a NULL-tier account could draw
            # and submit — the capability table decided nothing.
            #
            # Every account approved through the verification queue is assigned a
            # tier at the moment of approval, so NULL tier means one of two
            # things: not yet decided (already blocked by the verification gate,
            # so enforcement changes nothing for them), or an account that
            # predates tiering entirely — verification_status NULL, passing the
            # gate untouched, labeling today. Turning enforcement on without this
            # would lock that second group out of work they are doing right now,
            # which is a data-supply outage dressed as a security fix.
            #
            # They are granted LABELER: exactly the capability they already
            # exercise, and nothing more. Scoped to roles a tier can mean
            # anything for; a data_partner or buyer row carrying one is
            # meaningless (see capabilities._CAPABLE_ROLES).
            #
            # ``tier_assigned_at IS NULL`` is the load-bearing clause, because
            # THIS RUNS ON EVERY BOOT. Both real assignment paths
            # (record_verification_decision, appoint_advisor) stamp that column
            # in the same statement that writes the tier, so a NULL there means
            # no tier has ever been assigned to this account by anyone. Without
            # it, a tier deliberately cleared to revoke someone's ability to
            # label would be handed straight back on the next redeploy — a
            # migration that silently re-grants a revoked capability is worse
            # than the gap it was written to close.
            #
            # It also makes the rule safe for an ``approved`` account: approval
            # cannot happen without a tier (both doors 400 without one), so an
            # approved row with no tier and no assignment stamp predates tiering
            # and is exactly who this is for.
            conn.execute(
                "UPDATE users SET tier = 'labeler', tier_assigned_at = ?, "
                "tier_assigned_by = 'migration:tier_backfill' "
                "WHERE tier IS NULL AND tier_assigned_at IS NULL "
                "AND role IN ('evaluator', 'qa_reviewer') "
                # Pending and rejected are excluded. Neither can label anyway —
                # the verification gate denies them — so a tier would change no
                # access, and it would make the admin roster's "unassigned" chip
                # report a decision nobody has made.
                "AND (verification_status IS NULL OR verification_status = 'approved')",
                (_utcnow_iso(),),
            )
            # ═══ END PRD-B ═══
            # ═══ PRD-A REVIEW SCHEMA — owned by Agent 1, do not edit from other PRDs ═══
            # Two-tier review product (PRD A §1): senior reviewers grade a labeler's
            # completed submission. One row per review; a submission may carry several.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS case_reviews (
                    review_id          TEXT PRIMARY KEY,
                    task_id            TEXT NOT NULL,
                    submission_id      TEXT NOT NULL,        -- the labeler submission under review
                    reviewer_user_id   TEXT NOT NULL,
                    reviewer_id_hashed TEXT NOT NULL,
                    verdict            TEXT NOT NULL,        -- accept | accept_with_edits | reject
                    dimension_json     TEXT,                 -- per-dimension scores
                    corrections_json   TEXT,                 -- reviewer's edits
                    reviewer_notes     TEXT,
                    time_spent_sec     INTEGER,
                    blinded            INTEGER,              -- 1 only when the reviewer saw no labeler identity
                    created_at         TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_task ON case_reviews(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_sub  ON case_reviews(submission_id)")
            # Review lifecycle on the labeler submission. NULL = not yet routed /
            # unreviewed; 'in_review' = claimed by a reviewer; 'reviewed' = at least
            # one review submitted. Deliberately NO DEFAULT — NULL ("not yet decided")
            # must stay distinguishable from any decided value (START_HERE §4).
            if "review_status" not in cols("submissions"):
                conn.execute("ALTER TABLE submissions ADD COLUMN review_status TEXT")
            # Review CLAIM state (FIX A Phases 2/3). Three separate columns, all
            # nullable, none defaulted:
            #   review_claimed_by  — who holds the lease, so a second reviewer
            #     cannot silently evict in-flight work by POSTing a guessed id.
            #   review_claimed_at  — the lease clock. Dedicated on purpose:
            #     ``updated_at`` is bumped by ANY write, so a background pipeline
            #     touching the submission used to silently extend a reviewer's lease.
            #   review_blinded     — the blinding DERIVED from the payload actually
            #     served at draw time (1/0/NULL = never asserted). Read back at
            #     submit; never recomputed from a payload we are no longer serving.
            if "review_claimed_by" not in cols("submissions"):
                conn.execute("ALTER TABLE submissions ADD COLUMN review_claimed_by TEXT")
            if "review_claimed_at" not in cols("submissions"):
                conn.execute("ALTER TABLE submissions ADD COLUMN review_claimed_at TEXT")
            if "review_blinded" not in cols("submissions"):
                conn.execute("ALTER TABLE submissions ADD COLUMN review_blinded INTEGER")
            # next_review_for filters on review_status and review_queue_stats runs
            # four COUNT(*)s over it on every draw — unindexed, that is four full
            # table scans per reviewer per case (FIX A A-3.5).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sub_review_status ON submissions(review_status)")
            # Safe-Harbor identifier kinds found in the reviewer's own free text
            # at insert time (FIX A A-5.3). Tri-state: NULL = never scanned
            # (legacy row), '[]' = scanned clean, '["name",...]' = flagged, in
            # which case the free text is withheld from the buyer-facing block.
            if "identifier_flags" not in cols("case_reviews"):
                conn.execute("ALTER TABLE case_reviews ADD COLUMN identifier_flags TEXT")
            # ═══ END PRD-A ═══
            # ═══ PRD-R ROUTING / PAIRED REVIEW SCHEMA — owned by Agent R ═══════
            # The review unit moves from a SUBMISSION to a TASK (PRD R §2.1): a
            # reviewer draws the PAIR and adjudicates the case, so the claim,
            # the lease and the blinding assertion all belong on the task row.
            # All nullable, none defaulted — NULL means "no draw has asserted
            # this yet", which must stay distinguishable from a decided value
            # (context pack §6). The submission-level columns above are left
            # exactly as they are: single-submission review still exists for a
            # task that will only ever carry one label.
            for _col, _ddl in (
                ("review_status",     "TEXT"),     # NULL | in_review | reviewed
                ("review_claimed_by", "TEXT"),
                ("review_claimed_at", "TEXT"),     # the lease clock, never updated_at
                ("review_blinded",    "INTEGER"),  # 1 | 0 | NULL(never asserted)
            ):
                if _col not in cols("tasks"):
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {_col} {_ddl}")
            # The paired verdict, alongside the existing single-submission one.
            #   pair_sub_a / pair_sub_b — the pair in CANONICAL (oldest-first)
            #     order, so two reviewers' rows on the same case are comparable.
            #     NEVER the per-reviewer A/B order, which is a presentation fact.
            #   stronger        — 'A' | 'B' | 'equivalent', in the REVIEWER's
            #     positions, resolved to a submission id in accepted_submission_id.
            #   accepted_submission_id — which physician's work the verdict
            #     accepts; NULL for 'reject both'.
            # ``verdict`` deliberately keeps the existing three-value vocabulary
            # so ``agreement.review_acceptance`` — the ONE definition of expert
            # acceptance — keeps counting it. A fourth verdict token would land
            # in n_unclassified and silently zero the headline rate (PRD R §2.4:
            # extend the inputs, never the names).
            for _col, _ddl in (
                ("pair_sub_a",             "TEXT"),
                ("pair_sub_b",             "TEXT"),
                ("stronger",               "TEXT"),
                # Audit R H1. ``stronger`` is a POSITION, and a position only
                # means something next to the frame it was measured in. It is
                # now written in CANONICAL terms (the same frame as pair_sub_a /
                # pair_sub_b, which sit beside it) and this column carries the
                # unambiguous form so no reader has to know that. NULL when the
                # reviewer answered 'equivalent' — there is no stronger side.
                ("stronger_submission_id", "TEXT"),
                ("accepted_submission_id", "TEXT"),
            ):
                if _col not in cols("case_reviews"):
                    conn.execute(f"ALTER TABLE case_reviews ADD COLUMN {_col} {_ddl}")
            # The priority sort (PRD R §1.2) counts verdict-bearing submissions
            # per task on every labeler draw, and the pair query joins the same
            # two columns. Unindexed that is a full scan of ``submissions`` per
            # draw, per labeler.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sub_task_verdict "
                "ON submissions(task_id, verdict)")
            # The independence check — "has THIS labeler already submitted here?"
            # — runs as a NOT EXISTS on every labeler draw and on every reviewer
            # pair draw. Without this it resolves through idx_sub_task_verdict
            # and then filters, which is a scan of the task's submissions rather
            # than a point lookup (Audit R H4).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sub_task_evaluator "
                "ON submissions(task_id, evaluator_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_review_status "
                "ON tasks(review_status)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_review_task_reviewer "
                "ON case_reviews(task_id, reviewer_user_id)")
            # ═══ END PRD-R ═══
            # ═══ PRD-C HEALTH SYSTEM SCHEMA — owned by Agent 3, do not edit from other PRDs ═══
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS health_systems (
                    hs_id         TEXT PRIMARY KEY,          -- hs-<slug>-<6hex>
                    name          TEXT NOT NULL,
                    contact_email TEXT,
                    notes         TEXT,
                    active        INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hs_portal_users (
                    username      TEXT PRIMARY KEY,
                    hs_id         TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    must_reset    INTEGER NOT NULL DEFAULT 1,
                    email         TEXT,
                    last_login    TEXT,
                    active        INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT NOT NULL
                )
                """
            )
            # Login-lockout bookkeeping — the portal is public on launch day, so a
            # per-account lock (not just per-IP throttling) is required. Guarded
            # ALTERs so a DB created from the bare contract table also gains them.
            if "failed_logins" not in cols("hs_portal_users"):
                conn.execute("ALTER TABLE hs_portal_users ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0")
            if "locked_until" not in cols("hs_portal_users"):
                conn.execute("ALTER TABLE hs_portal_users ADD COLUMN locked_until TEXT")
            if "health_system_id" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN health_system_id TEXT")
            # Session invalidation (FIX-C C-2.3): a token minted before the last
            # password change must stop working immediately. Stamped on every
            # password write and carried as a token claim.
            if "password_changed_at" not in cols("hs_portal_users"):
                conn.execute("ALTER TABLE hs_portal_users ADD COLUMN password_changed_at TEXT")
            # The session binding is this COUNTER, not the timestamp beside it:
            # ``_utcnow_iso()`` truncates to whole seconds, so a password change
            # within the same second as a session's issuance would produce an
            # identical stamp and silently fail to invalidate that session. A
            # monotonic epoch cannot collide no matter how fast the clock ticks.
            if "session_epoch" not in cols("hs_portal_users"):
                conn.execute("ALTER TABLE hs_portal_users ADD COLUMN "
                             "session_epoch INTEGER NOT NULL DEFAULT 0")
            # Brute-force bookkeeping keyed on (username, ip) — ONE table for
            # known and unknown usernames alike (FIX-C C-2.1/C-2.2). Two separate
            # mechanisms is what produced the enumeration oracle: the in-memory
            # unknown-user path recorded a failure AFTER checking the threshold
            # while the DB path recorded BEFORE, so the 5th attempt returned 429
            # for a real account and 401 for a fake one. A single code path
            # cannot drift like that. Scoping the hard lock to the IP as well as
            # the username also stops a remote attacker from locking a hospital
            # out of its own portal with five wrong guesses.
            #
            # DB-backed rather than a process dict so the threshold does not
            # silently become N x the intended value when the app scales out to
            # N workers — which would re-open C-2.1.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hs_login_attempts (
                    attempt_key   TEXT PRIMARY KEY,   -- username|sha256(ip)[:16]
                    username      TEXT NOT NULL,
                    fails         INTEGER NOT NULL DEFAULT 0,
                    first_fail_at TEXT,
                    last_fail_at  TEXT,
                    locked_until  TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hs_login_attempts_user "
                         "ON hs_login_attempts(username)")
            # Logout must actually end the session on a shared hospital
            # workstation, so the token's jti goes on a denylist until it would
            # have expired anyway (FIX-C C-2.3).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hs_revoked_tokens (
                    jti        TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT NOT NULL
                )
                """
            )
            self._backfill_health_systems(conn)
            # ═══ END PRD-C ═══
            # ═══ PROFILE PICTURE — owned by the own-profile surface ═══════════
            # Its own fenced block rather than an edit to PRD-B or PRD-D above,
            # per the convention those fences establish.
            #
            # The SHA is the reference, mirroring ``cv_asset_sha``: bytes live
            # in the content-addressed asset store and the client never gets to
            # set this column, because a client-settable sha would be an
            # unvalidated pointer into a store that also holds de-identified
            # clinical images.
            for _col, _ddl in (
                ("avatar_asset_sha",  "TEXT"),
                ("avatar_mime",       "TEXT"),
                ("avatar_updated_at", "TEXT"),
            ):
                if _col not in cols("users"):
                    conn.execute(f"ALTER TABLE users ADD COLUMN {_col} {_ddl}")

            # ═══ PRD-D ADVISOR SCHEMA — owned by the advisor tier, do not edit from other PRDs ═══
            # The third physician tier (Advisor PRD). Guarded ALTERs, and — the
            # rule that holds everywhere in this file — NO DEFAULT on any status
            # column: NULL means "not decided / not applicable" and must stay
            # distinguishable from a decided value.
            for _col, _ddl in (
                # per_task | equity_only | NULL(unset). NULL is payable — see
                # asclepius/compensation.py for why that is not a default.
                ("compensation_model",    "TEXT"),
                ("advisor_since",         "TEXT"),
                ("advisor_agreement_ref", "TEXT"),  # signed advisor agreement on file
                ("referral_code",         "TEXT"),  # unique, minted on appointment
                ("slack_role",            "TEXT"),  # 'Medical Advisor' — the label, not a bool
            ):
                if _col not in cols("users"):
                    conn.execute(f"ALTER TABLE users ADD COLUMN {_col} {_ddl}")
            # A referral code is a claim on attribution, so two advisors must
            # never hold the same one. Partial index: NULL is the normal state
            # for the ~50 physicians who are not advisors, and SQLite would
            # otherwise treat every one of those NULLs as distinct anyway — the
            # WHERE clause states the intent rather than relying on that.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code "
                "ON users(referral_code) WHERE referral_code IS NOT NULL")

            # Referrals (Advisor PRD §3.1). One row per invited physician.
            # ``status`` is nullable with no DEFAULT: NULL means "we have not
            # heard back", which is emphatically not "declined".
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS referrals (
                    referral_id     TEXT PRIMARY KEY,
                    referrer_id     TEXT NOT NULL,   -- users.id of the advisor
                    referral_code   TEXT NOT NULL,
                    invitee_email   TEXT,
                    invitee_name    TEXT,
                    note            TEXT,            -- "knows her from Stanford ortho"
                    status          TEXT,            -- invited|signed_up|verified|approved|declined|NULL
                    invited_at      TEXT NOT NULL,
                    user_id         TEXT,            -- set when the invitee actually signs up
                    resolved_at     TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_referrals_code ON referrals(referral_code)")
            # Resolution is by invitee email at provision time, so that lookup
            # must not be a full scan of a growing table.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_referrals_email ON referrals(invitee_email)")

            # Advisory sign-offs (Advisor PRD §4.1). ONE mechanism, four
            # artifacts, discriminated by ``artifact_type`` — not four features
            # with four UIs and four permission checks to forget to update.
            #
            # ``relationship`` is written by the SERVER and never accepted from
            # the client (§0.2): an advisor holding equity who attests that a
            # batch is good enough to ship is a related-party attestation, and
            # recording the relationship next to the verdict converts something
            # a buyer could discover into something we disclosed.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS advisory_signoffs (
                    signoff_id     TEXT PRIMARY KEY,
                    artifact_type  TEXT NOT NULL,  -- task_batch|export_bundle|inbound_upload|product_spec
                    artifact_id    TEXT NOT NULL,
                    advisor_id     TEXT NOT NULL,
                    verdict        TEXT NOT NULL,  -- approved|approved_with_comments|changes_requested
                    comments       TEXT,
                    relationship   TEXT NOT NULL,  -- 'advisor_equity' — see PRD §0.2
                    created_at     TEXT NOT NULL
                )
                """
            )
            # What the advisor actually signed off ON (audit M3). A task_batch
            # id is a DERIVED key (specialty:YYYY-MM-DD over status='open'), so
            # its membership changes after the fact: tasks generated later the
            # same day join the batch and silently inherit an approval nobody
            # gave them. Resolving the member ids at write time makes the
            # subject of an attestation reconstructible, which is the whole
            # point of recording one.
            if "subject_ids" not in cols("advisory_signoffs"):
                conn.execute("ALTER TABLE advisory_signoffs ADD COLUMN subject_ids TEXT")
            if "subject_n" not in cols("advisory_signoffs"):
                conn.execute("ALTER TABLE advisory_signoffs ADD COLUMN subject_n INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signoff_artifact "
                "ON advisory_signoffs(artifact_type, artifact_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signoff_advisor "
                "ON advisory_signoffs(advisor_id)")

            # Product specs (Advisor PRD §4.2, artifact_type='product_spec'): a
            # markdown document an admin puts up for advisory comment. Kept
            # deliberately thin — it is a document with a title, not a CMS.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS product_specs (
                    spec_id     TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    body_md     TEXT NOT NULL,
                    created_by  TEXT,
                    created_at  TEXT NOT NULL
                )
                """
            )

            # Sign-off state, surfaced next to the thing it describes. Recorded
            # and shown in admin — it NEVER blocks a build or a shipment. One
            # advisor with a day job must not sit on the revenue path, so an
            # export always builds and always ships; the advisor's verdict and
            # comments ride alongside it as feedback.
            # Tri-state and no DEFAULT: NULL = nobody has looked yet, which is a
            # different fact from 'approved' and from 'changes_requested'.
            if "signoff_status" not in cols("exports"):
                conn.execute("ALTER TABLE exports ADD COLUMN signoff_status TEXT")
            if "signoff_status" not in cols("tasks"):
                conn.execute("ALTER TABLE tasks ADD COLUMN signoff_status TEXT")
            if "signoff_status" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN signoff_status TEXT")
            # Launch-week fan-out (V4 PRD §4). A task marked
            # ``open_to_all_specialties`` bypasses specialty routing for
            # VISIBILITY only — it appears in every approved physician's queue
            # regardless of their specialty. It does NOT touch ``max_labels``, so
            # it does not change how many labels we pay for; those are two
            # different things and conflating them is how "show everyone this
            # case" became a $4,500 line item.
            #
            # Off by default and NOT NULL DEFAULT 0: specialty routing is a
            # quality control, and this flag suspends it deliberately and
            # visibly rather than by accident on a legacy row.
            if "open_to_all_specialties" not in cols("tasks"):
                conn.execute("ALTER TABLE tasks ADD COLUMN "
                             "open_to_all_specialties INTEGER NOT NULL DEFAULT 0")
            # ═══ PRD-2: longitudinal trajectories (Longitudinal Cases §4.2.2) ═══
            # One chart walked in order becomes many tasks, and the ORDER IS THE
            # WHOLE POINT: task n's visible chart contains the outcomes of tasks
            # 0…n−1, so serving them out of order hands a physician the answers to
            # decisions they have not made yet.
            #
            #   trajectory_id   — shared by every decision point from one chart walk
            #   sequence_index  — 0-based position within that walk
            #
            # Additive ALTER and **no DEFAULT**, the house rule for a decision
            # column: NULL means "this task is not part of a trajectory", which is
            # what every existing V1–V4 row genuinely is. A DEFAULT of 0 on
            # ``sequence_index`` would back-stamp forty thousand ordinary tasks as
            # "step 1 of something", and the sequence gate below reads that column.
            #
            # These are NOT put in ``env_runs``. That table already carries
            # trajectory vocabulary — "a mode='rollout' row is one agent trajectory
            # over that environment, sharing task_id" — but it holds AGENT ROLLOUTS
            # for V5, not physician sessions. Same word, different actor, and
            # merging them makes "trajectory" ambiguous in exactly the table a buyer
            # audits.
            for _col, _ddl in (("trajectory_id", "TEXT"), ("sequence_index", "INTEGER")):
                if _col not in cols("tasks"):
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {_col} {_ddl}")
            # The sequence gate (§9.1) runs a correlated NOT EXISTS over
            # (trajectory_id, sequence_index) on every labeler draw. Unindexed that
            # is a full tasks scan per candidate row, on the hot queue path.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_trajectory "
                         "ON tasks(trajectory_id, sequence_index)")
            # §4.2.3 — the physician's sealed prediction, and their own grading of
            # it once the next encounter is revealed. Genuinely new: no existing
            # submission field is a variant of ``expected_trajectory``, and without
            # a column of its own the falsifier corpus (§7) — the part of this
            # product nobody else can sell — ships invisible.
            #
            # On the SUBMISSION, not the task: every physician who walks this
            # decision point writes their own falsifier, and the whole claim is that
            # a named specialist authored this one.
            for _col in ("expected_trajectory_json", "trajectory_self_score_json"):
                if _col not in cols("submissions"):
                    conn.execute(f"ALTER TABLE submissions ADD COLUMN {_col} TEXT")
            # §4.2.4 — why an agreement observation is out of the κ pool. Stored
            # rather than re-derived at read time so the exclusion is auditable in
            # the database a buyer's methodologist asks to see, and so a row whose
            # task is later deleted still says why it was excluded.
            if "kappa_excluded_reason" not in cols("agreement"):
                conn.execute("ALTER TABLE agreement ADD COLUMN kappa_excluded_reason TEXT")
            # ═══ END PRD-2 ═══
            # The advisor tier is retired: advisors are ordinary users now.
            # Remaining rows migrate to reviewer (advisor was a strict superset
            # of reviewer, so nothing they could do is lost) and become payable
            # per-task like everyone else (equity_only is cleared). Guarded on
            # the values themselves, so this is idempotent-quiet: a store with
            # no advisor rows left runs zero writes here. The advisory columns
            # and tables stay as dead schema, which is this store's convention
            # for retirement (additive migrations only, no destructive DDL).
            migrated = conn.execute(
                "UPDATE users SET tier = 'reviewer', "
                "tier_assigned_by = 'migration:advisor_retired' "
                "WHERE tier = 'advisor'").rowcount
            cleared = conn.execute(
                "UPDATE users SET compensation_model = NULL "
                "WHERE compensation_model = 'equity_only'").rowcount
            if migrated or cleared:
                conn.execute(
                    "INSERT INTO events (entity_type, entity_id, event_type, actor, "
                    "occurred_at, payload_json) VALUES ('store', 'users', "
                    "'advisor_tier_retired', 'migration', ?, ?)",
                    (_utcnow_iso(),
                     json.dumps({"tiers_migrated": migrated,
                                 "compensation_cleared": cleared})),
                )
            # ═══ END PRD-D ═══
            # ═══ PRD-I INGESTION SCHEMA — owned by Agent I, do not edit from other PRDs ═══
            # Purpose (PRD-I §2.1). 'task_creation' | 'brokering' | NULL.
            #
            # NO DEFAULT, deliberately, and this is the one column where that rule
            # carries real consequence rather than tidiness: NULL means "minted
            # before purpose existed", and the ONLY place it may be read as
            # task_creation is the promotion gate (§4.1). Everywhere else it renders
            # as an unresolved work item, because a legacy link silently defaulting
            # to task_creation is exactly how brokering data would become a task.
            #
            # Purpose lives on the row that AUTHORIZES an upload, and there are two
            # such rows because there are two doors: the magic-link door authorizes
            # on ingest_upload_links, and the health-system portal authorizes on
            # hs_portal_users (it has no link row at all — it carries the 'hs-portal'
            # sentinel link_id). PRD-I §2.1 names only the link table; putting it
            # solely there would leave every hospital-portal upload with no purpose,
            # which is the door the PRD's own §1 handshake is built on.
            for _tbl in ("ingest_upload_links", "hs_portal_users", "ingest_uploads",
                         "ingest_cases"):
                if "purpose" not in cols(_tbl):
                    conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN purpose TEXT")
            # The chain-of-custody triple (PRD-I §1.1). sha256/size_bytes already
            # exist on ingest_uploads; verified_at is what makes them a CLAIM rather
            # than two numbers — it is stamped only after the whole-file digest was
            # recomputed over the assembled bytes and matched the declared value.
            if "verified_at" not in cols("ingest_uploads"):
                conn.execute("ALTER TABLE ingest_uploads ADD COLUMN verified_at TEXT")
            # Resumable chunked upload sessions (PRD-I §1.1). A session is NOT an
            # upload: no ingest_uploads row exists until `complete` verifies the
            # whole-file digest, which is what makes "an assembled file with no
            # verified row is invisible to the application" true by construction
            # rather than by convention.
            #
            # Per-part progress is deliberately NOT stored here — it is derived from
            # the parts on disk. Writing a row per chunk would put a single-writer
            # SQLite under a tight write loop for state the filesystem already holds.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_upload_sessions (
                    session_id      TEXT PRIMARY KEY,
                    owner_kind      TEXT NOT NULL,   -- 'health_system'
                    owner_id        TEXT NOT NULL,
                    actor           TEXT,            -- portal username, for audit
                    filename        TEXT,
                    content_type    TEXT,
                    declared_sha256 TEXT NOT NULL,
                    declared_size   INTEGER NOT NULL,
                    chunk_size      INTEGER NOT NULL,
                    part_count      INTEGER NOT NULL,
                    storage_dir     TEXT NOT NULL,
                    -- No purpose column, deliberately. What an upload is FOR is a
                    -- mutable admin decision, and a session outlives it: 24 h,
                    -- resumable. A snapshot taken at declare is stale for every
                    -- byte that arrives after an admin corrects the mint, and the
                    -- single-request door resolves live — so the two doors would
                    -- record the same bytes differently. ``actor`` is stored and
                    -- everything derived is joined through it at completion.
                    status          TEXT,            -- NULL(open) | completing | verified | failed | aborted
                    upload_id       TEXT,            -- set at complete
                    verified_at     TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                )
                """
            )
            # Idempotency key (PRD-I §1.1): re-declaring the same bytes from the same
            # ACCOUNT returns the EXISTING session rather than starting a second one,
            # so "the contact refreshed the tab at 3.2 GB" is a non-event. Partial
            # index over OPEN sessions only — a failed session must not block a
            # genuine retry of the same file.
            #
            # ``actor`` is in the key, and that is a correctness requirement rather
            # than a refinement. Purpose lives on the ACCOUNT, and an organization
            # may legitimately hold one account of each kind. Keyed on the health
            # system alone, a brokering account re-declaring the same bytes as a
            # task-creation account would be handed that account's session — and
            # the completed upload would inherit its purpose. A brokering bundle
            # stamped task_creation and filed under Ready to promote: fail-open on
            # the one invariant this whole release exists to hold.
            #
            # The v1 index is dropped rather than left in place: it is UNIQUE, so
            # leaving it would keep enforcing the weaker key and still collapse the
            # two accounts onto one session.
            conn.execute("DROP INDEX IF EXISTS idx_ingest_sessions_idem")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_sessions_idem_v2 "
                "ON ingest_upload_sessions(owner_kind, owner_id, actor, "
                "declared_sha256, declared_size) WHERE status IS NULL"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_sessions_owner "
                         "ON ingest_upload_sessions(owner_kind, owner_id)")
            # The reaper scans on (status, updated_at); without this it is a full
            # table scan on every declare.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingest_sessions_reap "
                         "ON ingest_upload_sessions(status, updated_at)")
            # A DB created before purpose became live-resolved still carries the
            # snapshot column. Drop it rather than leave a stale value that reads
            # as authoritative — a dead column holding a plausible-looking answer
            # is worse than no column. Guarded: DROP COLUMN needs SQLite >= 3.35,
            # and where it is unavailable the column simply stays NULL and unread.
            if "purpose" in cols("ingest_upload_sessions"):
                try:
                    conn.execute("ALTER TABLE ingest_upload_sessions DROP COLUMN purpose")
                except sqlite3.OperationalError:
                    conn.execute("UPDATE ingest_upload_sessions SET purpose = NULL")
            # ═══ END PRD-I ═══
            # ═══ PRD-CRED TIERING SCHEMA — owned by Agent C, do not edit from other PRDs ═══
            # The context pack calls this Agent C's "PRD-C sentinel". That label is already
            # taken, at :1033 and :4808, by the HEALTH-SYSTEM schema from an earlier release
            # owned by a different agent. Reusing it would make the expected four-way merge
            # conflict unresolvable by inspection, so this block is PRD-CRED and sits at the
            # same insertion point. Nothing above is touched.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tiering_weights (
                    feature    TEXT PRIMARY KEY,
                    m          REAL NOT NULL,
                    q          REAL NOT NULL,
                    pinned     INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tiering_decisions (
                    decision_id     TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    case_domain     TEXT,
                    features_json   TEXT NOT NULL,
                    proposed_tier   TEXT,
                    admin_tier      TEXT,
                    was_flip        INTEGER,
                    was_exploration INTEGER,
                    -- 'admin' (an opinion label) | 'shadow_tr' (a REAL outcome label, scored
                    -- against a settled gold adjudication). The distinction is the only thing
                    -- that breaks the circularity of learning from the system's own routing,
                    -- so it is a column rather than something inferred later.
                    outcome_source  TEXT,
                    score           REAL,
                    decided_by      TEXT,
                    decided_at      TEXT NOT NULL,
                    -- NULL until folded into the weights, exactly once. This is the guard
                    -- against replaying old decisions: each event contributes precision once,
                    -- or you fabricate confidence.
                    applied_at      TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tiering_dec_user "
                         "ON tiering_decisions(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tiering_dec_unapplied "
                         "ON tiering_decisions(applied_at)")

            # ── Calibration exam (PRD C §4) ─────────────────────────────────────────────
            # key_json is the AGGREGATE of a reference panel, never one person's opinion.
            # active is nullable-free but responses are raw: an item can be re-keyed and every
            # past attempt rescored without re-testing anyone.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration_items (
                    item_id        TEXT PRIMARY KEY,
                    specialty      TEXT NOT NULL,
                    source_task_id TEXT,
                    vignette_json  TEXT NOT NULL,
                    key_json       TEXT NOT NULL,
                    panel_n        INTEGER,
                    active         INTEGER NOT NULL DEFAULT 1,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_calib_items_spec "
                         "ON calibration_items(specialty)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration_attempts (
                    attempt_id     TEXT PRIMARY KEY,
                    user_id        TEXT NOT NULL,
                    specialty      TEXT NOT NULL,
                    item_ids_json  TEXT NOT NULL,
                    -- The RAW per-item responses, always. Storing only the score would make
                    -- every future re-key a re-recruitment (PRD C §4).
                    responses_json TEXT,
                    scores_json    TEXT,
                    composite      REAL,
                    -- Deliberately no DEFAULT on either gate column: NULL means "not yet
                    -- graded" and must stay distinguishable from a graded fail.
                    tr_gate_passed INTEGER,
                    tl_gate_passed INTEGER,
                    started_at     TEXT NOT NULL,
                    submitted_at   TEXT,
                    rescored_at    TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_calib_att_user "
                         "ON calibration_attempts(user_id)")

            # ── OIG LEIE exclusions (gate A5) ───────────────────────────────────────────
            # Loaded from the monthly CSV. leie_meta records WHEN, so a stale or never-loaded
            # list resolves to UNKNOWN rather than silently to "clear" — an exclusion check
            # that fails open is not a check.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leie_exclusions (
                    npi        TEXT PRIMARY KEY,
                    excl_type  TEXT,
                    excl_date  TEXT,
                    loaded_at  TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leie_meta (
                    id          INTEGER PRIMARY KEY CHECK (id = 1),
                    loaded_at   TEXT NOT NULL,
                    row_count   INTEGER NOT NULL,
                    source_note TEXT
                )
                """
            )

            # ── Fairness monitor (PRD C §6) ─────────────────────────────────────────────
            # Voluntary, self-reported demographics, in a table that is NOT joinable into the
            # feature store — and the non-joinability is structural, not a naming convention:
            # the row is keyed by an HMAC of the user id under a secret the scorer never
            # holds, and the tier is COPIED IN at decision time. The monitor therefore needs
            # no join at all, and possession of this table does not re-identify anyone.
            # Collecting it separately is exactly what makes the monitor possible without
            # making the model able to see it.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fairness_observations (
                    obs_id       TEXT PRIMARY KEY,
                    subject_key  TEXT NOT NULL,
                    demographics_json TEXT NOT NULL,
                    decided_tier TEXT,
                    -- AUDIT H2: the FEATURE VECTOR, copied in at decision time alongside the
                    -- tier. structured_review_exp reaches the score at 0.70 and correlates
                    -- with IMG status and national origin — both of which are pinned to zero,
                    -- so the model cannot use them directly but this feature can route around
                    -- the pin. It cannot itself be pinned without gutting a real criterion, so
                    -- it is MONITORED instead, which means the monitor has to be able to see
                    -- it. Copied rather than joined for the same reason the tier is: a join
                    -- back to `users` would make the non-joinability of this table a naming
                    -- convention again.
                    features_json TEXT,
                    decided_at   TEXT NOT NULL
                )
                """
            )
            if "features_json" not in cols("fairness_observations"):
                conn.execute("ALTER TABLE fairness_observations ADD COLUMN features_json TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fairness_subject "
                         "ON fairness_observations(subject_key)")
            # ═══ END PRD-CRED ═══
            # ═══ PRD-P PAYMENTS SCHEMA — owned by Agent P, do not edit from other PRDs ═══
            # Three tables and one rule: money is a LEDGER, never a computed
            # aggregate. ``earnings`` rows carry the rate they were accrued at, so
            # changing ASCLEPIUS_TL_RATE_CENTS is a redeploy and never a restatement
            # of what a physician already earned.
            #
            # No DEFAULT on any status column (``qualified``, ``status``,
            # ``end_reason``): NULL means "not yet decided" and must stay
            # distinguishable from a decided value. ``credited_seconds`` DOES carry
            # a DEFAULT because zero seconds is a decided fact, not an absence.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS work_sessions (
                    session_id       TEXT PRIMARY KEY,
                    user_id          TEXT NOT NULL,
                    kind             TEXT NOT NULL,          -- 'review'
                    started_at       TEXT NOT NULL,
                    last_beat_at     TEXT,
                    ended_at         TEXT,
                    end_reason       TEXT,                   -- closed | expired | abandoned
                    credited_seconds INTEGER NOT NULL DEFAULT 0,
                    qualified        INTEGER,                -- NULL until closed; 1|0 after
                    nonce            TEXT NOT NULL,
                    min_seconds      INTEGER NOT NULL,
                    rate_cents       INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ws_user_open "
                "ON work_sessions(user_id, kind, ended_at)")
            # ``open_session`` is specified as idempotent — an open session for
            # this user+kind is RETURNED, never duplicated. The application check
            # is the friendly path; this partial unique index is the guarantee,
            # for the same reason UNIQUE(kind, ref_id) is on the ledger: two
            # concurrent opens that both read "no open session" would otherwise
            # both insert, and the reviewer would accumulate time against a
            # session the client is not beating.
            #
            # Non-fatal by design. A UNIQUE index over a table that somehow
            # already violates it fails to create, and a payments feature must
            # not be able to prevent the whole portal from booting. Log loudly
            # and carry on — ``open_session`` closes stragglers either way.
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ws_one_open_per_kind "
                    "ON work_sessions(user_id, kind) WHERE ended_at IS NULL")
            except sqlite3.DatabaseError:
                _logging.getLogger("asclepius.payments").warning(
                    "asclepius.store: could not create idx_ws_one_open_per_kind — "
                    "more than one open work_session already exists for some "
                    "(user_id, kind). Sessions still close correctly; the "
                    "duplicate-open guarantee is degraded until this is resolved.",
                    exc_info=True)
            # The QUALIFYING measure, kept separate from the RECORD.
            # ``credited_seconds`` is every paid-eligible second across the whole
            # session (PRD §1.4.1 — never delete a sub-threshold session, and never
            # lose the seconds it worked). ``continuous_seconds`` is the longest
            # single unbroken run, which is what "20 CONTINUOUS minutes" actually
            # tests. With clean beats they are identical; they diverge exactly when
            # a reviewer walked away and came back, which is the case the cliff is
            # about. Added as a guarded ALTER rather than folded into the CREATE so
            # a database written by an earlier build of this block still upgrades.
            if "continuous_seconds" not in cols("work_sessions"):
                conn.execute(
                    "ALTER TABLE work_sessions ADD COLUMN continuous_seconds INTEGER NOT NULL DEFAULT 0")
            # Standard deviation of beat intervals, in ms (PRD §3). Near-zero jitter
            # is machine-generated. NULL = never computed. This column is a SIGNAL
            # and nothing reads it to decide a payout — a false positive here means
            # not paying a physician $100.
            if "jitter_ms" not in cols("work_sessions"):
                conn.execute("ALTER TABLE work_sessions ADD COLUMN jitter_ms REAL")
            # Count of beats whose wall-clock delta disagreed with the process
            # monotonic clock by more than the tolerance. Recorded, logged, and
            # deliberately NOT applied to the ledger — see payments.py §clock.
            if "clock_skew_beats" not in cols("work_sessions"):
                conn.execute(
                    "ALTER TABLE work_sessions ADD COLUMN clock_skew_beats INTEGER NOT NULL DEFAULT 0")
            # How many distinct pieces of work this session's beats named (audit
            # C2). A count, not a gate: one hard case adjudicated for twenty
            # honest minutes is exactly one key, and refusing to pay that is the
            # false positive this whole feature is built to avoid. Recorded so the
            # threshold in ASCLEPIUS_TR_MIN_PROGRESS_KEYS can eventually be set
            # from data rather than from a guess.
            if "distinct_progress_keys" not in cols("work_sessions"):
                conn.execute(
                    "ALTER TABLE work_sessions ADD COLUMN distinct_progress_keys "
                    "INTEGER NOT NULL DEFAULT 0")
            # How many times a client asked for a fresh nonce mid-session (audit
            # H1). One or two is a page reload; dozens is a script that never
            # reads a heartbeat response.
            if "resume_count" not in cols("work_sessions"):
                conn.execute(
                    "ALTER TABLE work_sessions ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0")

            # One durable row per accepted heartbeat. There is no server-side
            # stopwatch and no in-memory session state anywhere in this feature:
            # credited time is recomputed from these rows every time it is asked
            # for, which is what makes a redeploy mid-session lose nothing.
            #
            # UNIQUE(session_id, seq) is the replay guard at the database level.
            # The application also checks that seq increases; the constraint is the
            # guarantee, the check is the friendly error.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_beats (
                    beat_id      TEXT PRIMARY KEY,
                    session_id   TEXT NOT NULL,
                    server_ts    TEXT NOT NULL,
                    seq          INTEGER NOT NULL,
                    active       INTEGER NOT NULL,
                    progress_key TEXT,
                    client_ts    TEXT,
                    UNIQUE(session_id, seq)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_beat_session ON session_beats(session_id, seq)")

            # The ledger. ``UNIQUE(kind, ref_id)`` is the double-payment guard —
            # not a nicety. Every double-payment story starts with an application
            # check that raced; this one cannot, because two concurrent finalizers
            # cannot both win an INSERT.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS earnings (
                    earning_id   TEXT PRIMARY KEY,
                    user_id      TEXT NOT NULL,
                    kind         TEXT NOT NULL,             -- 'task' | 'review_session'
                    ref_id       TEXT NOT NULL,             -- submission_id | session_id
                    amount_cents INTEGER NOT NULL,
                    rate_cents   INTEGER NOT NULL,
                    status       TEXT NOT NULL,             -- accrued | approved | void | paid
                    accrued_at   TEXT NOT NULL,
                    resolved_at  TEXT,
                    note         TEXT,
                    UNIQUE(kind, ref_id)
                )
                """
            )
            # The disbursement batch this row was paid in (audit H2). NULL until
            # money actually leaves — which makes it, not the status alone, the
            # thing that says a payment really happened. No DEFAULT, same rule as
            # every other decision column in this file.
            if "payout_batch_id" not in cols("earnings"):
                conn.execute("ALTER TABLE earnings ADD COLUMN payout_batch_id TEXT")
            # Admin Launch PRD §4.4 — voiding one case. Three columns rather than
            # one, because "we did not pay for this" has to carry WHO decided and
            # WHEN alongside the reason: voiding a physician's pay is
            # consequential and an unattributable void cannot be appealed.
            # Additive ALTER, no DEFAULT — same rule as every other decision
            # column in this file, so an existing row reads NULL ("never voided")
            # rather than being back-stamped with a decision nobody made.
            for _col in ("void_reason", "voided_by", "voided_at"):
                if _col not in cols("earnings"):
                    conn.execute(f"ALTER TABLE earnings ADD COLUMN {_col} TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_earnings_user ON earnings(user_id, status)")
            # Reconciling a disbursement against the ledger is the first thing
            # anyone does after running one.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_earnings_batch ON earnings(payout_batch_id)")
            # The auto-approve sweep scans by (status, accrued_at). Unindexed that
            # is a full ledger scan on every Earnings page load, forever.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_earnings_sweep ON earnings(status, accrued_at)")
            # ═══ END PRD-P ═══

            # ═══ Community invites (Admin Launch PRD §5.1) ═════════════════
            # A one-time, expiring link into Asclepius Community, mailed to an
            # already-APPROVED physician. Same shape as the onboarding member
            # token in team_store: only the SHA-256 of the token is stored, so a
            # database read cannot mint a working link, and the raw token exists
            # exactly once — in the email.
            #
            # ``redeemed_at`` is what makes it one-time; the join flag itself
            # lives on users.slack_joined and is claimed through
            # ``mark_community_welcomed``, which is the concurrency arbiter. This
            # table never decides membership, it only carries the link.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS community_invites (
                    token_hash  TEXT PRIMARY KEY,
                    user_id     TEXT NOT NULL,
                    email       TEXT NOT NULL,
                    expires_at  TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    created_by  TEXT,
                    redeemed_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_community_invites_user "
                "ON community_invites(user_id, created_at)")

            # ═══ Verification jobs ═════════════════════════════════════
            # One row per signup, drained by an in-process loop (main.py). There
            # is no scheduler in this repo and this is not the change that
            # introduces one: same durable-outbox shape as task_notify_outbox.
            #
            # UNIQUE(user_id) so a re-onboard cannot queue a second live job.
            # Claiming is a single guarded UPDATE with the rowcount as arbiter,
            # never SELECT-then-UPDATE, which is what makes it correct if this
            # ever runs on more than one worker AND what lets a crashed job be
            # reclaimed once its claim ages out.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_jobs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      TEXT NOT NULL UNIQUE,
                    status       TEXT NOT NULL DEFAULT 'queued',
                    attempts     INTEGER NOT NULL DEFAULT 0,
                    last_error   TEXT,
                    outcome      TEXT,
                    dossier_json TEXT,
                    claimed_at   TEXT,
                    queued_at    TEXT NOT NULL,
                    finished_at  TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_verification_jobs_status "
                "ON verification_jobs(status, queued_at)"
            )

            # ═══ Admin notifications ═══════════════════════════════════════
            # Same shape as task_notify_outbox so it drains in the SAME loop
            # rather than adding a second thing that can silently stop.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_notify_outbox (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    kind            TEXT NOT NULL,
                    subject         TEXT NOT NULL,
                    body_html       TEXT NOT NULL,
                    recipient_email TEXT NOT NULL,
                    send_after      TEXT,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    send_attempts   INTEGER NOT NULL DEFAULT 0,
                    last_error      TEXT,
                    sent_at         TEXT,
                    created_at      TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_admin_notify_status "
                "ON admin_notify_outbox(status, send_after)"
            )

            # ═══ Password resets ═══════════════════════════════════════
            # A reset token is a bearer credential for the account, so only its
            # sha256 lands here: a database read cannot be replayed against the
            # reset endpoint. Same choice already made for
            # ``ingest_upload_links.token_hash``.
            #
            # ``consumed_at`` is the single-use arbiter and ``invalidated_at``
            # the supersede/rotate one. Both are timestamps rather than a status
            # column so "when" survives for an audit, and both are checked in the
            # same guarded UPDATE that consumes the row, which is what makes the
            # single-use guarantee hold under more than one worker.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS password_resets (
                    id             TEXT PRIMARY KEY,
                    user_id        TEXT NOT NULL,
                    token_hash     TEXT NOT NULL UNIQUE,
                    requested_ip   TEXT,
                    requested_at   TEXT NOT NULL,
                    expires_at     TEXT NOT NULL,
                    consumed_at    TEXT,
                    invalidated_at TEXT,
                    created_via    TEXT NOT NULL DEFAULT 'self'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_password_resets_user "
                "ON password_resets(user_id, requested_at)"
            )

            # ═══ PRD-REF — the referral bounty (physician referrals from Earnings) ═══
            # NO NEW TABLE. ``referrals`` already exists (PRD-D above) and the
            # ledger already carries UNIQUE(kind, ref_id), which is the whole
            # double-pay guard. What is missing is a place to record the MONEY
            # state of a referral, and that is deliberately NOT ``referrals.status``.
            #
            # ``status`` is a state machine about a PERSON — invited, signed up,
            # verified, approved — and it is driven from the verification routes
            # via ``advance_referral_for_user``. Writing 'paid_out' into it would
            # mean a later NPI recheck or a re-onboard calling
            # ``advance_referral_for_user(uid, 'approved')`` walks a PAID referral
            # back to 'approved': ``advance_referral``'s monotonicity guard only
            # covers values that are IN the ladder, so an out-of-ladder value is
            # overwritten silently. A row that has been paid must never be
            # rewritable by an event about credentialing, so the money gets its
            # own columns and the funnel keeps its own.
            #
            # No DEFAULT on any of them — the rule that holds everywhere in this
            # file. NULL on ``bounty_state`` means "nothing has been decided yet",
            # which is exactly the pending state the Earnings page must render as
            # "+$150 pending" rather than as absence.
            for _col, _ddl in (
                # NULL | earned | duplicate | expired | ineligible
                ("bounty_state",       "TEXT"),
                ("bounty_earning_id",  "TEXT"),   # the earnings row that paid it
                ("bounty_resolved_at", "TEXT"),
                # When the invitee's first accepted case settled the bounty.
                # Display/admin truth only; the money truth stays in bounty_*.
                ("first_case_at",      "TEXT"),
                # 'email' (typed into the composer) | 'link' (arrived via
                # /join?ref=CODE) | NULL for rows minted before the column.
                ("source",             "TEXT"),
                # The IP the link-signup arrived from, for the same-IP fraud
                # heuristic. Never rendered to the referrer.
                ("signup_ip",          "TEXT"),
                # NULL | 'same_ip'. Display-only for admins; blocks nothing by
                # itself — QA acceptance is the gate that actually guards money.
                ("fraud_flag",         "TEXT"),
            ):
                if _col not in cols("referrals"):
                    conn.execute(f"ALTER TABLE referrals ADD COLUMN {_col} {_ddl}")
            # The bounty resolves FROM the invitee: "this physician's first task
            # was approved — who referred them?". Unindexed that is a full scan of
            # the referrals table on every task approval, forever.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_referrals_user ON referrals(user_id)")
            # ═══ END PRD-REF ═══

            # ═══ PRD-SCORE: the contributor score ════════════════════════════
            # One current row per physician plus an append-ish history (one row
            # per graded submission, replaced on re-grade). The score is always
            # recomputable from submissions + reviews; these tables exist so
            # the dashboard and the admin roster read one row instead of
            # replaying a physician's whole record on every page load.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contributor_scores (
                    user_id         TEXT PRIMARY KEY,
                    score           REAL NOT NULL,
                    n_cases         INTEGER NOT NULL,
                    components_json TEXT,
                    updated_at      TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contributor_score_history (
                    id              TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    score           REAL NOT NULL,
                    prev_score      REAL,
                    case_score      REAL,
                    submission_id   TEXT,
                    components_json TEXT,
                    created_at      TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cscore_hist_user "
                "ON contributor_score_history(user_id, created_at)")
            # A re-graded submission replaces its own history entry rather than
            # stacking a second one (partial: NULL submission_id rows are the
            # initial-rating markers and there is at most one by code).
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_cscore_hist_sub "
                "ON contributor_score_history(user_id, submission_id) "
                "WHERE submission_id IS NOT NULL")
            # ═══ END PRD-SCORE ═══

            # ── Specialty-tagged task notifications (outbox + drain) ─────────
            # One row per (recipient, specialty, upload batch): the admin request
            # enqueues these synchronously (fast, transactional) and a background
            # drain sends the emails, so a crash mid-send leaves rows `pending`
            # for the manual re-drain endpoint rather than losing the tail.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_notify_outbox (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key   TEXT NOT NULL UNIQUE,
                    batch_id          TEXT NOT NULL,
                    specialty         TEXT NOT NULL,
                    task_count        INTEGER NOT NULL,
                    recipient_user_id TEXT NOT NULL,
                    recipient_email   TEXT NOT NULL,
                    status            TEXT NOT NULL DEFAULT 'pending',
                    send_attempts     INTEGER NOT NULL DEFAULT 0,
                    last_error        TEXT,
                    sent_at           TEXT,
                    created_at        TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_notify_status ON task_notify_outbox(status)"
            )

    # ─── Users ────────────────────────────────────────────────────────────────
    def create_user(
        self,
        *,
        email: str,
        password: str,
        role: str = "evaluator",
        specialty: Optional[str] = None,
        board_cert: Optional[str] = None,
        years_experience: Optional[int] = None,
        organization: Optional[str] = None,
        is_mock: bool = False,
        tier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``tier`` defaults to NULL — "not yet assigned", which now denies the
        LABEL capability at /tasks/next and /submissions. Real signups get theirs
        from the verification queue at the moment of approval; only a caller that
        legitimately provisions an already-decided contributor passes one here."""
        email = email.lower().strip()
        uid = _new_id("u")
        id_hashed = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, password_hash, role, specialty, board_cert,
                                   years_experience, organization, id_hashed, active, is_mock,
                                   tier, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    uid,
                    email,
                    hash_password(password),
                    role,
                    specialty,
                    board_cert,
                    years_experience,
                    organization,
                    id_hashed,
                    1 if is_mock else 0,
                    tier,
                    _utcnow_iso(),
                ),
            )
        return self.get_user_by_id(uid)  # type: ignore[return-value]

    def ensure_admin(self, *, email: str, password: str) -> Dict[str, Any]:
        """Idempotently guarantee a bootstrap admin exists for ``email``.

        Unlike ``seed_default_admin`` (which only runs when the user table is
        empty), this runs on every boot when ``ASCLEPIUS_ADMIN_EMAIL`` /
        ``ASCLEPIUS_ADMIN_PASSWORD`` are set, so an operator can always (re)gain
        access by setting those env vars and redeploying:

          * missing        -> create the account with role='admin', active=1
          * exists, drifted-> force role='admin', active=1, and reset the
                              password to match the env value
          * exists, matches-> no-op (no write, password already correct)

        Only touches this one account; other users are never modified.
        """
        email = email.lower().strip()
        existing = self.get_user_by_email(email)
        if not existing:
            return self.create_user(email=email, password=password, role="admin")
        # Only write when something actually needs to change, so a redeploy with
        # unchanged credentials doesn't churn the row (or revert a matching pw).
        needs_update = (
            existing.get("role") != "admin"
            or not existing.get("active")
            or not verify_password(password, existing["password_hash"])
        )
        if needs_update:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ?, role = 'admin', active = 1 "
                    "WHERE email = ?",
                    (hash_password(password), email),
                )
            return self.get_user_by_email(email)  # type: ignore[return-value]
        return existing

    def provision_user(
        self,
        *,
        email: str,
        password: Optional[str] = None,
        password_hash: Optional[str] = None,
        role: str = "evaluator",
        full_name: Optional[str] = None,
        org_name: Optional[str] = None,
        clinical_role: Optional[str] = None,
        specialty: Optional[str] = None,
        specialty_niche: Optional[str] = None,
        board_cert: Optional[str] = None,
        npi: Optional[str] = None,
        years_experience: Optional[int] = None,
        credentials: Optional[Dict[str, Any]] = None,
        attestations: Optional[Dict[str, Any]] = None,
        account_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Idempotent upsert used by the Asclepius onboarding flow.

        Creates the portal account (or updates it if the person re-onboards),
        carrying the full credential + attestation record collected during
        onboarding.

        Pass ``password_hash`` when the physician already chose a password
        earlier in the wizard (it was hashed at that step), or ``password`` to
        have one hashed here.

        Passing NEITHER on a re-onboard leaves the existing hash alone. That is
        the important case: this used to rewrite ``password_hash``
        unconditionally, so any re-run of onboarding silently replaced a live
        physician's chosen password with one nobody knew, locking them out of
        an account they were actively working in.
        """
        email = email.lower().strip()
        if password_hash is None and password is not None:
            password_hash = hash_password(password)
        creds_json = json.dumps(credentials or {})
        atts_json = json.dumps(attestations or {})
        existing = self.get_user_by_email(email)
        with self._conn() as conn:
            if existing:
                # password_hash is set in its own clause, and only when supplied,
                # so a re-onboard that carries no password cannot blank or
                # replace the one the physician is signing in with today.
                pw_clause = "password_hash = ?, " if password_hash is not None else ""
                pw_param = (password_hash,) if password_hash is not None else ()
                pw_stamp = ", password_changed_at = ?" if password_hash is not None else ""
                pw_stamp_param = (
                    (datetime.utcnow().replace(microsecond=0).isoformat(),)
                    if password_hash is not None
                    else ()
                )
                conn.execute(
                    f"""
                    UPDATE users SET
                        {pw_clause}role = ?, specialty = ?, specialty_niche = ?,
                        board_cert = ?,
                        years_experience = ?, active = 1, full_name = ?, org_name = ?,
                        -- Keep the canonical organization in sync with the
                        -- health-system name, but never wipe a previously-set org
                        -- if a re-onboard omits it (COALESCE keeps the old value).
                        organization = COALESCE(?, organization), clinical_role = ?,
                        npi = ?, credentials_json = ?, attestations_json = ?,
                        -- COALESCE for the same reason organization uses it: a
                        -- re-onboard that omits the kind must not silently
                        -- promote a referral-only account into a physician.
                        account_kind = COALESCE(?, account_kind){pw_stamp}
                    WHERE email = ?
                    """,
                    (
                        *pw_param, role, specialty, specialty_niche, board_cert,
                        years_experience, full_name, org_name, org_name, clinical_role, npi,
                        creds_json, atts_json, account_kind, *pw_stamp_param, email,
                    ),
                )
                return self.get_user_by_email(email)  # type: ignore[return-value]
            uid = _new_id("u")
            id_hashed = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]
            conn.execute(
                """
                INSERT INTO users (id, email, password_hash, role, specialty, specialty_niche,
                                   board_cert, years_experience, organization, id_hashed, active,
                                   full_name, org_name, clinical_role, npi, credentials_json,
                                   attestations_json, account_kind, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid, email, password_hash or hash_password(secrets.token_urlsafe(32)), role, specialty, specialty_niche,
                    board_cert, years_experience, org_name, id_hashed, full_name, org_name,
                    clinical_role, npi, creds_json, atts_json, account_kind, _utcnow_iso(),
                ),
            )
        return self.get_user_by_id(uid)  # type: ignore[return-value]

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def set_user_password(self, user_id: str, new_password: str, *, stamp_changed: bool = True) -> None:
        """Set a user's password hash.

        ``stamp_changed`` writes ``password_changed_at``, which every token
        minted before that instant is checked against (asclepius/auth.py). That
        is what makes a reset actually end an attacker's existing session
        instead of merely changing what they would have to type next time.
        """
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with self._conn() as conn:
            if stamp_changed:
                conn.execute(
                    "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                    (hash_password(new_password), now, user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (hash_password(new_password), user_id),
                )

    # ── Password resets ──────────────────────────────────────────────────────

    def create_password_reset(
        self,
        *,
        user_id: str,
        token_hash: str,
        expires_at: str,
        requested_ip: Optional[str] = None,
        created_via: str = "self",
    ) -> Dict[str, Any]:
        rid = "pr_" + uuid.uuid4().hex
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO password_resets (id, user_id, token_hash, requested_ip, "
                "requested_at, expires_at, created_via) VALUES (?,?,?,?,?,?,?)",
                (rid, user_id, token_hash, requested_ip, now, expires_at, created_via),
            )
            row = conn.execute("SELECT * FROM password_resets WHERE id = ?", (rid,)).fetchone()
        return dict(row)

    def get_password_reset_by_token_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM password_resets WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            return dict(row) if row else None

    def consume_password_reset(self, reset_id: str) -> bool:
        """Claim a reset exactly once. The rowcount IS the arbiter.

        One guarded UPDATE, never SELECT-then-UPDATE: expiry, prior use and
        supersede are all checked in the same statement, so two workers racing
        the same link cannot both win. Same discipline as ``consume_upload_link``.
        """
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE password_resets SET consumed_at = ? "
                "WHERE id = ? AND consumed_at IS NULL AND invalidated_at IS NULL "
                "AND expires_at > ?",
                (now, reset_id, now),
            )
            return cur.rowcount > 0

    def invalidate_password_resets_for_user(self, user_id: str) -> int:
        """Kill every live reset for a user. Called after any password write, so
        a second link mailed earlier cannot be used to take the account back."""
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE password_resets SET invalidated_at = ? "
                "WHERE user_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
                (now, user_id),
            )
            return cur.rowcount

    # ── Verification jobs ────────────────────────────────────────────────

    def enqueue_verification_job(self, user_id: str) -> bool:
        """Queue a signup for the agent. Idempotent per user: a re-onboard
        re-queues only if the previous job already finished."""
        now = _utcnow_iso()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO verification_jobs (user_id, status, queued_at) "
                "VALUES (?, 'queued', ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "  status='queued', queued_at=excluded.queued_at, claimed_at=NULL, "
                "  attempts=0, last_error=NULL, outcome=NULL, finished_at=NULL "
                "WHERE verification_jobs.status IN ('done','failed')",
                (user_id, now),
            )
            return cur.rowcount > 0

    def claim_verification_job(self, *, stale_after_seconds: int = 900) -> Optional[Dict[str, Any]]:
        """Claim the next job in ONE guarded UPDATE.

        Also reclaims a job whose worker died mid-run: its row is left 'running'
        with a claimed_at that ages out, so a restart picks it up instead of
        leaving a physician's signup unverified forever.
        """
        now = datetime.utcnow().replace(microsecond=0)
        stale = (now - timedelta(seconds=max(1, stale_after_seconds))).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "UPDATE verification_jobs SET status='running', claimed_at=?, "
                "attempts=attempts+1 "
                "WHERE id = (SELECT id FROM verification_jobs "
                "            WHERE status='queued' "
                "               OR (status='running' AND claimed_at <= ?) "
                "            ORDER BY queued_at LIMIT 1) "
                "RETURNING *",
                (now.isoformat(), stale),
            ).fetchone()
            return dict(row) if row else None

    def finish_verification_job(self, job_id: int, *, outcome: str, dossier: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE verification_jobs SET status='done', outcome=?, dossier_json=?, "
                "finished_at=? WHERE id = ?",
                (outcome, json.dumps(dossier or {}), _utcnow_iso(), job_id),
            )

    def fail_verification_job(self, job_id: int, error: str, *, max_attempts: int = 3) -> None:
        """Mark a run failed. After max_attempts it stops retrying and is
        referred to a human, because an agent that cannot decide is exactly the
        case the admin queue exists for."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE verification_jobs SET "
                "  status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END, "
                "  last_error = ?, claimed_at = NULL, "
                "  finished_at = CASE WHEN attempts >= ? THEN ? ELSE NULL END "
                "WHERE id = ?",
                (max_attempts, (error or "")[:500], max_attempts, _utcnow_iso(), job_id),
            )

    def get_verification_job(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM verification_jobs WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── Admin notifications ──────────────────────────────────────────────

    def enqueue_admin_notification(
        self, *, idempotency_key: str, kind: str, subject: str, body_html: str,
        recipient_email: str, send_after: Optional[str] = None,
    ) -> Optional[int]:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO admin_notify_outbox "
                "(idempotency_key, kind, subject, body_html, recipient_email, "
                " send_after, status, created_at) VALUES (?,?,?,?,?,?,'pending',?)",
                (idempotency_key, kind, subject, body_html, recipient_email,
                 send_after, _utcnow_iso()),
            )
            return int(cur.lastrowid) if cur.rowcount else None

    def update_pending_admin_notification(
        self, idempotency_key: str, *, subject: str, body_html: str
    ) -> bool:
        """Enrich a queued alert in place.

        The alert is queued at signup with a short grace window and a plain
        body; when the agent reports, it rewrites that same row. One email per
        signup either way, so a broken agent costs detail rather than the
        notification itself.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE admin_notify_outbox SET subject = ?, body_html = ? "
                "WHERE idempotency_key = ? AND status = 'pending'",
                (subject, body_html, idempotency_key),
            )
            return cur.rowcount > 0

    def due_admin_notifications(self, limit: int = 50) -> List[Dict[str, Any]]:
        now = _utcnow_iso()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM admin_notify_outbox WHERE status='pending' "
                "AND (send_after IS NULL OR send_after <= ?) "
                "ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_admin_notification_sent(self, row_id: int, *, ok: bool, error: str = "") -> None:
        with self._conn() as conn:
            if ok:
                conn.execute(
                    "UPDATE admin_notify_outbox SET status='sent', sent_at=?, "
                    "send_attempts=send_attempts+1 WHERE id=?",
                    (_utcnow_iso(), row_id),
                )
            else:
                conn.execute(
                    "UPDATE admin_notify_outbox SET send_attempts=send_attempts+1, "
                    "last_error=? WHERE id=?",
                    ((error or "")[:500], row_id),
                )

    def count_live_password_resets(self, user_id: str) -> int:
        now = datetime.utcnow().replace(microsecond=0).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM password_resets "
                "WHERE user_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL "
                "AND expires_at > ?",
                (user_id, now),
            ).fetchone()
            return int(row["n"] if row else 0)

    # ── Data-provider accounts (email+password door, EHR PRD §4) ────────────
    @staticmethod
    def _data_provider_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["must_reset_password"] = bool(rec.get("must_reset_password"))
        return rec

    def provision_data_provider(
        self, *, email: str, password: str, org_name: Optional[str] = None,
        specialty: Optional[str] = None, note: Optional[str] = None,
        invited_by: Optional[str] = None, invite_expires_at: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create (or rotate) a ``data_partner`` account + its provider record.
        Idempotent: an existing provider gets a fresh password, is re-activated,
        ``must_reset_password`` is re-armed, and the invite window resets — so
        Resend rotates credentials rather than duplicating. The account is
        provisioned via the shared ``provision_user`` path with role='data_partner'."""
        user = self.provision_user(
            email=email, password=password, role="data_partner",
            org_name=org_name, specialty=specialty,
        )
        pid = user["id"]
        now = _utcnow_iso()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT provider_id FROM data_providers WHERE provider_id = ?", (pid,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE data_providers SET status='invited', must_reset_password=1,
                       org_name=COALESCE(?, org_name), specialty=COALESCE(?, specialty),
                       note=COALESCE(?, note), purpose=COALESCE(?, purpose),
                       invited_by=?, invited_at=?,
                       invite_expires_at=?, updated_at=? WHERE provider_id=?""",
                    (org_name, specialty, note, purpose, invited_by, now,
                     invite_expires_at, now, pid),
                )
            else:
                conn.execute(
                    """INSERT INTO data_providers
                       (provider_id, email, org_name, specialty, note, purpose, status,
                        must_reset_password, invited_by, invited_at, invite_expires_at,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'invited', 1, ?, ?, ?, ?, ?)""",
                    (pid, email.lower().strip(), org_name, specialty, note, purpose,
                     invited_by, now, invite_expires_at, now, now),
                )
        return self.get_data_provider(pid)  # type: ignore[return-value]

    def get_data_provider(self, provider_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM data_providers WHERE provider_id = ?", (provider_id,)
            ).fetchone()
        return self._data_provider_row(row) if row else None

    def list_data_providers(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM data_providers ORDER BY created_at DESC"
            ).fetchall()
        return [self._data_provider_row(r) for r in rows]

    def revoke_data_provider(self, provider_id: str) -> Optional[Dict[str, Any]]:
        """Revoke access: mark revoked AND deactivate the account so its token
        stops authenticating (deny-by-default)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE data_providers SET status='revoked', updated_at=? WHERE provider_id=?",
                (_utcnow_iso(), provider_id),
            )
            conn.execute("UPDATE users SET active = 0 WHERE id = ?", (provider_id,))
        return self.get_data_provider(provider_id)

    def clear_provider_password_reset(self, provider_id: str) -> None:
        """First-login forced reset done: drop the reset flag + activate."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE data_providers SET must_reset_password=0, status='active', "
                "updated_at=? WHERE provider_id=?",
                (_utcnow_iso(), provider_id),
            )

    def provider_quality_score(self, provider_id: str) -> Dict[str, Any]:
        """% of a provider's upload bundles that ingested clean — the early-warning
        that a partner's de-id is drifting. Reads the shared ingest_uploads inbox
        filtered to this provider (partner_id)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status FROM ingest_uploads WHERE partner_id = ?", (provider_id,)
            ).fetchall()
        total = len(rows)
        clean = sum(1 for r in rows if r["status"] == "ingested")
        return {
            "total_uploads": total,
            "clean_uploads": clean,
            "clean_pct": round(100.0 * clean / total, 1) if total else None,
        }

    # ── Buyer accounts + deliveries (secure data workspace) ─────────────────
    @staticmethod
    def _buyer_account_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["must_reset_password"] = bool(rec.get("must_reset_password"))
        return rec

    def provision_buyer(
        self, *, email: str, password: str, buyer_name: Optional[str] = None,
        note: Optional[str] = None, invited_by: Optional[str] = None,
        invite_expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create (or rotate) a ``buyer`` account + its workspace record. Idempotent:
        an existing buyer keeps their delivery history but gets a fresh password and
        a re-armed forced reset when re-provisioned. The login lives in ``users``
        (role='buyer') via the shared ``provision_user`` path."""
        user = self.provision_user(
            email=email, password=password, role="buyer",
            full_name=buyer_name, org_name=buyer_name,
        )
        bid = user["id"]
        now = _utcnow_iso()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT buyer_account_id FROM buyer_accounts WHERE buyer_account_id = ?", (bid,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE buyer_accounts SET status='invited', must_reset_password=1,
                       buyer_name=COALESCE(?, buyer_name), note=COALESCE(?, note),
                       invited_by=?, invited_at=?, invite_expires_at=?, updated_at=?
                       WHERE buyer_account_id=?""",
                    (buyer_name, note, invited_by, now, invite_expires_at, now, bid),
                )
            else:
                conn.execute(
                    """INSERT INTO buyer_accounts
                       (buyer_account_id, email, buyer_name, note, status,
                        must_reset_password, invited_by, invited_at, invite_expires_at,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'invited', 1, ?, ?, ?, ?, ?)""",
                    (bid, email.lower().strip(), buyer_name, note,
                     invited_by, now, invite_expires_at, now, now),
                )
        return self.get_buyer_account(bid)  # type: ignore[return-value]

    def get_buyer_account(self, buyer_account_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM buyer_accounts WHERE buyer_account_id = ?", (buyer_account_id,)
            ).fetchone()
        return self._buyer_account_row(row) if row else None

    def get_buyer_account_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM buyer_accounts WHERE email = ?", (email.lower().strip(),)
            ).fetchone()
        return self._buyer_account_row(row) if row else None

    def list_buyer_accounts(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM buyer_accounts ORDER BY created_at DESC"
            ).fetchall()
        return [self._buyer_account_row(r) for r in rows]

    def clear_buyer_password_reset(self, buyer_account_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE buyer_accounts SET must_reset_password=0, status='active', "
                "updated_at=? WHERE buyer_account_id=?",
                (_utcnow_iso(), buyer_account_id),
            )

    def record_buyer_delivery(
        self, *, buyer_account_id: str, buyer_email: str, export_id: str,
        label: Optional[str] = None, data_format: Optional[str] = None,
        record_count: int = 0, note: Optional[str] = None, sent_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        did = _new_id("del")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO buyer_deliveries
                   (delivery_id, buyer_account_id, buyer_email, export_id, label,
                    data_format, record_count, note, sent_by, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (did, buyer_account_id, buyer_email.lower().strip(), export_id, label,
                 data_format, int(record_count or 0), note, sent_by, now),
            )
        return self.get_buyer_delivery(did)  # type: ignore[return-value]

    def get_buyer_delivery(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM buyer_deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_buyer_deliveries(
        self, *, buyer_account_id: Optional[str] = None, export_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if buyer_account_id:
            clauses.append("buyer_account_id = ?")
            params.append(buyer_account_id)
        if export_id:
            clauses.append("export_id = ?")
            params.append(export_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM buyer_deliveries {where} ORDER BY sent_at DESC", tuple(params)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_user_by_id_hashed(self, id_hashed: str) -> Optional[Dict[str, Any]]:
        """Resolve the user (incl. onboarding-collected credential fields) from the
        hashed annotator id that stamps every record."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id_hashed = ?", (id_hashed,)).fetchone()
            return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def count_users(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def list_evaluators_by_specialty(
        self, specialty: str, *, include_provisional: bool = False
    ) -> List[Dict[str, Any]]:
        """Active evaluators whose specialty matches, for task-notify recipient
        resolution. Mirrors ``next_task_for_evaluator``'s match (equality on
        ``specialty``), just inverted: users for a specialty instead of a task
        for a user.

        Excludes physicians still awaiting verification by default. They cannot
        draw the work (``require_label``), so mailing them "12 new Nephrology
        tasks are ready" is an invitation to a 403. The kwarg exists so the
        exclusion is stated at the call site rather than discovered later.

        NULL verification_status is INCLUDED: those are pre-verification-era
        accounts that have always been able to work, not pending ones.
        """
        clause = (
            ""
            if include_provisional
            else "AND (verification_status IS NULL OR verification_status = 'approved') "
        )
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE role = 'evaluator' AND active = 1 "
                f"{clause}"
                "AND lower(trim(specialty)) = lower(trim(?))",
                (specialty,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── Task-notify outbox (specialty-tagged task notifications) ───────────
    def enqueue_task_notification(
        self, *, idempotency_key: str, batch_id: str, specialty: str, task_count: int,
        recipient_user_id: str, recipient_email: str,
    ) -> Optional[int]:
        """Insert a pending outbox row, deduped on ``idempotency_key`` (one email
        per clinician per specialty per upload batch). Returns the new row id, or
        None if this (recipient, specialty, batch) was already enqueued."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO task_notify_outbox
                  (idempotency_key, batch_id, specialty, task_count,
                   recipient_user_id, recipient_email, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    idempotency_key, batch_id, specialty, task_count,
                    recipient_user_id, recipient_email, _utcnow_iso(),
                ),
            )
            return int(cur.lastrowid) if cur.rowcount else None

    def list_pending_task_notifications(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM task_notify_outbox WHERE status = 'pending' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_task_notification_sent(self, notification_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE task_notify_outbox SET status = 'sent', sent_at = ?, "
                "send_attempts = send_attempts + 1 WHERE id = ?",
                (_utcnow_iso(), notification_id),
            )

    def mark_task_notification_failed(self, notification_id: int, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE task_notify_outbox SET status = 'failed', last_error = ?, "
                "send_attempts = send_attempts + 1 WHERE id = ?",
                (error, notification_id),
            )

    # ─── Real EHR ingestion (EHR PRD §4, §5, §8) ─────────────────────────────
    def create_upload_link(
        self, *, token_hash: str, partner_id: str, partner_label: Optional[str],
        specialty: str, expires_at: str, one_time: bool, max_bytes: int,
        created_by: Optional[str], contact_email: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> Dict[str, Any]:
        lid = _new_id("lnk")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ingest_upload_links
                   (link_id, token_hash, partner_id, partner_label, specialty,
                    expires_at, one_time, max_bytes, created_by, contact_email,
                    purpose, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (lid, token_hash, partner_id, partner_label, specialty, expires_at,
                 1 if one_time else 0, int(max_bytes), created_by,
                 (contact_email or None), purpose, _utcnow_iso()),
            )
        return self.get_upload_link(lid)  # type: ignore[return-value]

    def get_upload_link(self, link_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_upload_links WHERE link_id = ?", (link_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_upload_link_by_token_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_upload_links WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def list_upload_links(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ingest_upload_links ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_upload_link_used(self, link_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_upload_links SET used_count = used_count + 1 WHERE link_id = ?",
                (link_id,),
            )

    def consume_upload_link(self, link_id: str, *, one_time: bool) -> bool:
        """ATOMIC use-claim (security review: closes the one-time TOCTOU race —
        two concurrent uploads both passing the used_count==0 read). For a
        one-time link the conditional UPDATE succeeds for exactly one caller;
        multi-use links just increment. Returns False when the claim lost."""
        with self._conn() as conn:
            if one_time:
                cur = conn.execute(
                    "UPDATE ingest_upload_links SET used_count = used_count + 1 "
                    "WHERE link_id = ? AND used_count = 0 AND revoked = 0",
                    (link_id,),
                )
            else:
                cur = conn.execute(
                    "UPDATE ingest_upload_links SET used_count = used_count + 1 "
                    "WHERE link_id = ? AND revoked = 0",
                    (link_id,),
                )
            return cur.rowcount == 1

    def revoke_upload_link(self, link_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE ingest_upload_links SET revoked = 1 WHERE link_id = ?", (link_id,))

    def new_upload_id(self) -> str:
        """Mint an upload id up front so the raw blob can be written to durable
        storage BEFORE the row is inserted (the row then always carries a valid
        raw_path — no None window where the file is on disk but unreachable)."""
        return _new_id("upl")

    def insert_ingest_upload(
        self, *, link_id: str, partner_id: str, filename: Optional[str],
        sha256: Optional[str], size_bytes: Optional[int], raw_path: Optional[str],
        source_ip: Optional[str], upload_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = upload_id or _new_id("upl")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ingest_uploads
                   (upload_id, link_id, partner_id, filename, sha256, size_bytes,
                    status, raw_path, source_ip, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'received', ?, ?, ?, ?)""",
                (uid, link_id, partner_id, filename, sha256, size_bytes,
                 raw_path, source_ip, now, now),
            )
        return self.get_ingest_upload(uid)  # type: ignore[return-value]

    def update_ingest_upload(self, upload_id: str, **fields: Any) -> None:
        allowed = {"status", "reason", "files_json", "raw_path", "retain_raw"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "files_json" and not isinstance(v, (str, type(None))):
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_utcnow_iso(), upload_id])
        with self._conn() as conn:
            conn.execute(f"UPDATE ingest_uploads SET {', '.join(sets)} WHERE upload_id = ?", tuple(params))

    def get_ingest_upload(self, upload_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM ingest_uploads WHERE upload_id = ?", (upload_id,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["files"] = json.loads(rec.pop("files_json") or "[]")
        return rec

    def list_ingest_uploads(self, *, limit: int = 200, offset: int = 0,
                            status: Optional[str] = None) -> List[Dict[str, Any]]:
        where, params = "", []
        if status:
            where = "WHERE status = ? "
            params.append(status)
        params += [max(1, limit), max(0, offset)]
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM ingest_uploads {where}ORDER BY created_at DESC LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["files"] = json.loads(rec.pop("files_json") or "[]")
            out.append(rec)
        return out

    def count_ingest_uploads(self, *, status: Optional[str] = None) -> int:
        """Total upload rows — lets the admin UI paginate over full history.
        With ``status`` set, counts only rows in that state (drives the filter chips)."""
        with self._conn() as conn:
            if status:
                return int(conn.execute(
                    "SELECT COUNT(*) FROM ingest_uploads WHERE status = ?", (status,)
                ).fetchone()[0])
            return int(conn.execute("SELECT COUNT(*) FROM ingest_uploads").fetchone()[0])

    def mark_upload_failure_notified(self, upload_id: str) -> None:
        """Stamp the moment we emailed the sender that their upload failed, so the
        auto-notifier fires at most once per upload (manual re-sends are allowed)."""
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_uploads SET failure_notified_at = ?, updated_at = ? "
                "WHERE upload_id = ?",
                (now, now, upload_id),
            )

    def insert_ingest_case(
        self, *, upload_id: str, patient_key: Optional[str], specialty: Optional[str],
        case: Optional[Dict[str, Any]], status: str, report: Optional[Dict[str, Any]],
        review_status: Optional[str] = None, review_json: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        cid = _new_id("icase")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO ingest_cases
                   (ingest_case_id, upload_id, patient_key, specialty, case_json,
                    status, report_json, review_status, review_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, upload_id, patient_key, specialty,
                 json.dumps(case) if case else None, status,
                 json.dumps(report) if report else None, review_status,
                 json.dumps(review_json) if review_json else None, now, now),
            )
        return self.get_ingest_case(cid)  # type: ignore[return-value]

    def update_ingest_case(self, ingest_case_id: str, **fields: Any) -> None:
        allowed = {"status", "case_json", "report_json", "task_id", "override_reason",
                   "review_status", "review_json", "reviewed_by_hashed", "reviewed_at"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("case_json", "report_json", "review_json") and not isinstance(v, (str, type(None))):
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_utcnow_iso(), ingest_case_id])
        with self._conn() as conn:
            conn.execute(
                f"UPDATE ingest_cases SET {', '.join(sets)} WHERE ingest_case_id = ?", tuple(params)
            )

    # ─── Sealed ground truth (Buyer Response PRD §3 B1) ──────────────────────
    def insert_sealed_ground_truth(
        self, *, ingest_case_id: str, upload_id: Optional[str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Store the adjudicated answer key ENCRYPTED at rest, keyed to the ingest
        case. Same field_crypto pattern as the raw partner blob — the payload is
        never written in cleartext when a key is configured.

        NOTE (Audit §H1): the crash-safe ingest path stages then binds
        (``stage_sealed_ground_truth`` → ``bind_sealed_ground_truth``). This
        one-shot insert is retained for callers that already have a case id."""
        from field_crypto import encrypt_field
        sid = _new_id("sealed")
        now = _utcnow_iso()
        blob = encrypt_field(json.dumps(payload))
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO sealed_ground_truth
                   (sealed_id, ingest_case_id, upload_id, payload_enc, created_at, bound_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, ingest_case_id, upload_id, blob, now, now),
            )
        return {"sealed_id": sid, "ingest_case_id": ingest_case_id}

    def stage_sealed_ground_truth(
        self, *, upload_id: Optional[str], patient_key: Optional[str],
        payload: Dict[str, Any],
    ) -> str:
        """Stage the sealed answer key BEFORE the case row exists (Audit §H1), keyed
        on (upload_id, patient_key) with a NULL ingest_case_id. Encrypted at rest,
        exactly like the bound path. Returns the ``sealed_id`` to bind once the case
        is inserted. A crash after this and before binding leaves the key on disk,
        unbound — the strictly better failure than an ingested case with no key."""
        from field_crypto import encrypt_field
        sid = _new_id("sealed")
        now = _utcnow_iso()
        blob = encrypt_field(json.dumps(payload))
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO sealed_ground_truth
                   (sealed_id, ingest_case_id, upload_id, patient_key, payload_enc,
                    created_at, bound_at)
                   VALUES (?, NULL, ?, ?, ?, ?, NULL)""",
                (sid, upload_id, patient_key, blob, now),
            )
        return sid

    def bind_sealed_ground_truth(self, sealed_id: str, ingest_case_id: str) -> None:
        """Bind a staged key to its case row (Audit §H1). Idempotent; a bind that
        never lands leaves the row unbound for reconciliation to catch."""
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE sealed_ground_truth SET ingest_case_id = ?, bound_at = ? "
                "WHERE sealed_id = ?",
                (ingest_case_id, now, sealed_id),
            )

    def hold_ingest_case_for_review(self, ingest_case_id: str, reason: str,
                                    detail: str, *, severity: str = "blocking") -> bool:
        """Add a review reason to an already-terminal case and hold it out of the
        annotation queue (Audit §9.3). Idempotent per reason code; returns True if a
        new reason was added. Used by post-hoc reconciliation (unbound sealed key,
        missing asset blob) to flag a case that reached a terminal state but is
        internally inconsistent — a defect the ingest-time checks could not see."""
        existing = self.get_ingest_case(ingest_case_id)
        if not existing:
            return False
        reasons = list(existing.get("review") or [])
        if any(x.get("reason") == reason for x in reasons):
            return False
        reasons.append({"reason": reason, "severity": severity, "detail": detail,
                        "raised_at": _utcnow_iso()})
        fields: Dict[str, Any] = {"review_status": "needs_review", "review_json": reasons}
        # A blocking reason also flips the case status so the queue-hold SQL excludes
        # it; an advisory one leaves the case where it is.
        if severity == "blocking" and existing.get("status") == "ingested":
            fields["status"] = "needs_review"
        self.update_ingest_case(ingest_case_id, **fields)
        return True

    def reconcile_sealed_ground_truth(self, *, older_than_seconds: int = 3600) -> Dict[str, Any]:
        """Recover sealed keys left UNBOUND by a crash between staging and binding
        (Audit §H1). For each unbound row older than the threshold, try to bind it to
        its case by (upload_id, patient_key); if the case is found, bind it and raise a
        blocking ``sealed_key_unbound`` review reason on that case (a human confirms the
        recovered key before the case is annotated). A row with no matching case is
        reported as an orphan — the key exists but its case never landed."""
        cutoff = _iso_minus_seconds(older_than_seconds)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT sealed_id, upload_id, patient_key, created_at "
                "FROM sealed_ground_truth "
                "WHERE ingest_case_id IS NULL AND created_at <= ?",
                (cutoff,),
            ).fetchall()
        bound, orphans = 0, []
        for r in rows:
            rec = dict(r)
            case = None
            with self._conn() as conn:
                crow = conn.execute(
                    "SELECT ingest_case_id, status FROM ingest_cases "
                    "WHERE upload_id = ? AND patient_key = ? "
                    "ORDER BY created_at ASC LIMIT 1",
                    (rec.get("upload_id"), rec.get("patient_key")),
                ).fetchone()
                if crow:
                    case = dict(crow)
            if case:
                self.bind_sealed_ground_truth(rec["sealed_id"], case["ingest_case_id"])
                # Hold the recovered case for review — a key that had to be reconciled
                # is a blocking signal until a human confirms the adjudication.
                self.hold_ingest_case_for_review(
                    case["ingest_case_id"], "sealed_key_unbound",
                    "sealed answer key was recovered by reconciliation after an "
                    "interrupted ingest")
                bound += 1
            else:
                orphans.append({"sealed_id": rec["sealed_id"], "upload_id": rec.get("upload_id"),
                                "reason": "sealed_key_unbound", "severity": "blocking",
                                "detail": "staged answer key has no matching ingest case"})
        return {"checked": len(rows), "bound": bound, "orphans": orphans}

    def get_sealed_ground_truth(
        self, ingest_case_id: str, *, actor: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Decrypt + return the sealed answer key for an ingest case, emitting an
        audit event on EVERY read (Buyer Response PRD §3 B1). Only the adjudication
        surface should call this — never render_case_prompt, an export profile, or a
        baseline runner."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sealed_ground_truth WHERE ingest_case_id = ? "
                "ORDER BY created_at DESC LIMIT 1", (ingest_case_id,),
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        from field_crypto import decrypt_field
        payload = json.loads(decrypt_field(rec.get("payload_enc")) or "null")
        self.log_event(entity_type="sealed_ground_truth", entity_id=ingest_case_id,
                       event_type="sealed_ground_truth_read", actor=actor,
                       payload={"sealed_id": rec.get("sealed_id")})
        return {"sealed_id": rec.get("sealed_id"), "ingest_case_id": ingest_case_id,
                "upload_id": rec.get("upload_id"), "payload": payload}

    def get_sealed_ground_truth_raw(self, ingest_case_id: str) -> Optional[str]:
        """Return the ON-DISK (still-encrypted) payload token WITHOUT decrypting or
        auditing — used by tests to assert the content is unreadable at rest."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload_enc FROM sealed_ground_truth WHERE ingest_case_id = ? "
                "ORDER BY created_at DESC LIMIT 1", (ingest_case_id,),
            ).fetchone()
        return dict(row)["payload_enc"] if row else None

    def get_ingest_case(self, ingest_case_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_cases WHERE ingest_case_id = ?", (ingest_case_id,)
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["case"] = json.loads(rec.pop("case_json") or "null")
        rec["report"] = json.loads(rec.pop("report_json") or "null")
        rec["review"] = json.loads((rec.get("review_json") or "null") or "null")
        return rec

    def list_ingest_cases(
        self, *, upload_id: Optional[str] = None, status: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if upload_id:
            clauses.append("upload_id = ?")
            params.append(upload_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM ingest_cases {where} ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["case"] = json.loads(rec.pop("case_json") or "null")
            rec["report"] = json.loads(rec.pop("report_json") or "null")
            rec["review"] = json.loads((rec.get("review_json") or "null") or "null")
            out.append(rec)
        return out

    def delete_unpromoted_ingest_cases(self, upload_id: str) -> int:
        """Remove an upload's cases that have NOT been promoted to a task
        (``task_id`` still null). Lets a reprocess (startup recovery of an
        upload interrupted by a redeploy) start from a clean slate without
        creating duplicate cases — while never touching promoted work."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM ingest_cases WHERE upload_id = ? "
                "AND (task_id IS NULL OR task_id = '')",
                (upload_id,),
            )
            return cur.rowcount

    def list_uploads_with_retained_raw(self) -> List[Dict[str, Any]]:
        """Uploads whose raw blob is retained past the normal window (Audit §9.4) —
        every entry failed to parse, so the file is kept for re-run after a fix. The
        retention purge skips these paths."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT upload_id, raw_path FROM ingest_uploads WHERE retain_raw = 1"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_uploads_in_status(self, statuses: List[str]) -> List[Dict[str, Any]]:
        """Uploads currently sitting in any of ``statuses`` — used by startup
        recovery to find work interrupted mid-pipeline (received/scanning/parsing)."""
        if not statuses:
            return []
        qs = ",".join("?" for _ in statuses)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM ingest_uploads WHERE status IN ({qs}) "
                "ORDER BY created_at ASC", tuple(statuses),
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["files"] = json.loads(rec.pop("files_json") or "[]")
            out.append(rec)
        return out

    def set_real_data_approved(self, user_id: str, approved: bool, *,
                               source: str = "admin") -> None:
        """Grant/revoke V4 real-case access (EHR PRD §9.5).

        ``source`` records WHO decided, which the boolean cannot. It defaults to
        ``'admin'`` because every caller that passes nothing is a human action
        (the admin control, the API), and a human decision must survive every
        later automatic sync — including a REVOKE, which is otherwise
        indistinguishable from "never considered"."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET real_data_approved = ?, real_data_approval_source = ? "
                "WHERE id = ?",
                (1 if approved else 0, source, user_id),
            )

    def sync_real_data_approval(self) -> Dict[str, Any]:
        """Grant real-data access to every APPROVED, LABELING physician.

        The product's own answer to "who may see real patient data": a physician
        whose credentials we verified and who we have cleared to label is, by that
        same decision, cleared for the real cases. Keeping the two separate meant
        the real queue was gated on a flag nobody could set, so it sat unlabelled.

        Two rules keep this honest:

          * a HUMAN decision is never overridden. ``real_data_approval_source =
            'admin'`` — a deliberate grant OR a deliberate revoke — is left
            exactly as it is. This is why the source column exists: without it a
            revoke reads as 0, the same as never-considered, and every sync would
            silently hand access back.
          * it only ever grants to someone who qualifies RIGHT NOW, and revokes
            the auto-grant from someone who no longer does (tier removed,
            verification withdrawn). An auto-grant that could not be undone would
            outlive the approval it was derived from.

        Idempotent. Returns ``{granted, revoked, eligible}``."""
        from asclepius import capabilities as asc_caps

        granted, revoked = [], []
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, tier, verification_status, real_data_approved, "
                "       real_data_approval_source FROM users"
            ).fetchall()]
        eligible = 0
        for u in rows:
            qualifies = (u.get("verification_status") == "approved"
                         and asc_caps.can(u, asc_caps.LABEL))
            eligible += 1 if qualifies else 0
            if (u.get("real_data_approval_source") or "") == "admin":
                continue                       # a human decided; leave it alone
            has = bool(u.get("real_data_approved"))
            if qualifies and not has:
                self.set_real_data_approved(u["id"], True, source="auto:approved_labeler")
                granted.append(u["id"])
            elif not qualifies and has and (u.get("real_data_approval_source") or "").startswith("auto:"):
                # Only ever withdraws what THIS policy gave. A grant with no
                # recorded source predates the policy and is left to a human.
                self.set_real_data_approved(u["id"], False, source="auto:approved_labeler")
                revoked.append(u["id"])
        return {"granted": len(granted), "revoked": len(revoked), "eligible": eligible}

    def get_tutorial_state(self, user_id: str) -> Dict[str, Any]:
        """The user's first-run tutorial state; a default not_started shape when
        unset or unparseable (a corrupt blob must never lock someone out)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT tutorial_json FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        raw = row[0] if row else None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("status"):
                    return parsed
            except (ValueError, TypeError):
                pass
        return {"status": "not_started", "version": None}

    def set_tutorial_state(self, user_id: str, state: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET tutorial_json = ? WHERE id = ?",
                (json.dumps(state), user_id),
            )

    def mock_annotator_id_hashes(self) -> set:
        """The ``id_hashed`` of every mock/sandbox contributor. Records carry the
        annotator's ``id_hashed``; export hard-excludes these by default and the
        admin labels them, so a demo never contaminates a shipped batch."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id_hashed FROM users WHERE is_mock = 1 AND id_hashed IS NOT NULL"
            ).fetchall()
        return {r[0] for r in rows if r[0]}

    def ensure_mock_user(
        self,
        *,
        email: str,
        password: str,
        specialty: Optional[str] = None,
        board_cert: Optional[str] = None,
        years_experience: Optional[int] = None,
        organization: Optional[str] = None,
        real_data_approved: bool = False,
    ) -> Dict[str, Any]:
        """Idempotently guarantee the mock/sandbox contributor exists (internal demo
        tool). Runs on every boot: creates the account if missing, else forces it to
        role='evaluator', active, is_mock=1, and resets the password to match the
        configured value (so an operator can always regain the sandbox login). Only
        touches this one account.

        ``real_data_approved`` is DECIDED BY THE CALLER (auth.ensure_mock_contributor):
        the sandbox may demo V4 real cases only when its password is NOT the known
        default in production — a default-credential account must never grant read
        access to real patient data (security review finding)."""
        email = email.lower().strip()
        approved = 1 if real_data_approved else 0
        existing = self.get_user_by_email(email)
        if not existing:
            u = self.create_user(
                email=email, password=password, role="evaluator",
                specialty=specialty, board_cert=board_cert,
                years_experience=years_experience, organization=organization,
                is_mock=True,
                # The sandbox account exists to DEMO labeling, and LABEL is now
                # enforced at /tasks/next and /submissions. A NULL tier here
                # would leave the demo login unable to draw a case.
                tier="labeler",
            )
            self.set_real_data_approved(u["id"], bool(real_data_approved))
            return self.get_user_by_id(u["id"])  # type: ignore[return-value]
        with self._conn() as conn:
            conn.execute(
                """UPDATE users SET password_hash = ?, role = 'evaluator', active = 1,
                       is_mock = 1, real_data_approved = ?,
                       tier = COALESCE(tier, 'labeler'),
                       specialty = COALESCE(specialty, ?),
                       board_cert = COALESCE(board_cert, ?),
                       years_experience = COALESCE(years_experience, ?),
                       organization = COALESCE(organization, ?)
                   WHERE email = ?""",
                (hash_password(password), approved, specialty, board_cert,
                 years_experience, organization, email),
            )
        return self.get_user_by_email(email)  # type: ignore[return-value]

    def annotator_block(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """The credential block copied onto every emitted record (PRD §6.2).

        Emits the credential under the canonical key ``credential`` (singular) AND
        the deprecated alias ``credentials`` (plural) for one release. Two names for
        one concept is exactly the mechanism that produced the buyer's finding
        (Buyer Response PRD §6 E1: ``store.annotator_block`` wrote ``credentials``
        while the contributor rollup wrote ``credential``), so both are emitted here
        during the migration and packaging reads either."""
        cred = user.get("board_cert") or (
            f"board_certified_{user.get('specialty')}" if user.get("specialty") else "unspecified"
        )
        return {
            "id_hashed": user.get("id_hashed") or "",
            "credential": cred,       # canonical (Buyer Response PRD §6 E1)
            "credentials": cred,      # deprecated alias — kept for one release
            "specialty": user.get("specialty"),
            "years_experience": user.get("years_experience"),
            # Advisor PRD §5.1: the tier rides along so packaging can stamp
            # ``related_party`` from the SAME resolution that produced the
            # credential. Resolving them separately is how one of them ends up
            # hydrated and the other silently null on a record that ships.
            "tier": user.get("tier"),
        }

    # ─── Tasks ──────────────────────────────────────────────────────────────--
    def insert_task(
        self,
        *,
        prompt: str,
        specialty: str = "general",
        difficulty: str = "medium",
        capture_reasoning: bool = False,
        source: str = "lab_supplied",
        candidate_answers: Optional[List[Dict[str, Any]]] = None,
        max_labels: int = 1,
        grounding_mode: str = "optional",
        independent_mode: str = "stance",
        buyer_request_id: Optional[str] = None,
        generation: Optional[Dict[str, Any]] = None,
        value_tier: Optional[str] = None,
        modality: str = "text",
        case: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        created_by: Optional[str] = None,
        # Launch-week fan-out (V4 PRD §4): VISIBILITY only. See the migration note
        # on the column. Never derived from anything — an explicit caller decision.
        open_to_all_specialties: bool = False,
        # Longitudinal trajectory (PRD 2 §4.2.2): this task's chart walk and its
        # 0-based position in it. ALL-OR-NOTHING and never derived — a caller that
        # knows it is building step 4 of a walk passes both; everyone else passes
        # neither and gets NULLs, which is what an ordinary V1–V4 task is.
        trajectory_id: Optional[str] = None,
        sequence_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        from asclepius.constants import normalize_independent_mode

        tid = task_id or _new_id("t")
        # Half a trajectory identity is worse than none: a row with an id and no
        # index cannot be ordered, and a row with an index and no id belongs to no
        # walk. The sequence gate reads both, so an inconsistent pair would either
        # block a task forever or wave an out-of-order one through. Fail loudly at
        # the write rather than silently at the draw.
        if (trajectory_id is None) != (sequence_index is None):
            raise ValueError(
                "trajectory_id and sequence_index must be set together: a "
                "trajectory point needs both a walk to belong to and a position "
                "within it (PRD 2 §4.2.2)")
        if sequence_index is not None:
            try:
                sequence_index = int(sequence_index)
            except (TypeError, ValueError):
                raise ValueError("sequence_index must be an integer")
            if sequence_index < 0:
                raise ValueError("sequence_index is 0-based and cannot be negative")
        elif task_id:
            # This statement is INSERT OR REPLACE, so re-inserting an existing
            # ``task_id`` without the trajectory columns would NULL them — and a
            # trajectory point that loses its position stops being one. The
            # sequence gate would then wave it through as an ordinary task, which
            # is the §9.1 blocker returning through a side door, silently, on an
            # admin's task upload.
            #
            # One indexed lookup, only on the explicit-id path (generation always
            # mints its own id and skips this entirely).
            with self._conn() as conn:
                prior = conn.execute(
                    "SELECT trajectory_id FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            if prior and prior["trajectory_id"]:
                raise ValueError(
                    f"task {task_id!r} is decision point in trajectory "
                    f"{prior['trajectory_id']!r}; re-inserting it without its "
                    "trajectory_id and sequence_index would strip its position in "
                    "the chart walk and let it be served out of order")
        gm = grounding_mode if grounding_mode in ("optional", "required") else "optional"
        im = normalize_independent_mode(independent_mode)
        # Multimodal (Synthetic Multimodal Cases PRD): modality is DERIVED from case
        # presence — a task is multimodal iff it carries a structured case. We do
        # NOT honor a bare modality='multimodal' label with no case: that would
        # stamp records multimodal + grant the value premium with no case data
        # behind it (a mislabel from a hand-built upload). Case is the single source
        # of truth; the ``modality`` param is advisory. The FULL case (incl. internal
        # ground_truth) is stored server-side; blinding/packaging strip the answer
        # key downstream — the same contract as the server-side ``intended_flawed_id``.
        # Modality is derived from CONTENT, not presence (BUG-1 §3): a case dict
        # that carries no labs AND no notes is an empty case and can never be
        # stamped multimodal (which would grant the value premium + a multimodal
        # label with no data behind it). An empty ``case={}`` is treated as text.
        md = "multimodal" if (case and (case.get("lab_panels") or case.get("notes") or case.get("studies"))) else "text"
        # case_source is DERIVED from the case (EHR PRD §9.5): 'real_deid' only
        # when the case itself says so; any other case is 'synthetic'; a text
        # task has none. First-class column so the V4 routing wall is pure SQL.
        cs = ((case.get("case_source") or "synthetic") if case else None)
        # Empirical difficulty (PRD §9) rides in the generation block; lift it to the
        # first-class columns for the serving gate + admin/export.
        ed_val, ed_measured = _empirical_difficulty_fields(
            (generation or {}).get("empirical_difficulty")
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks
                  (task_id, specialty, difficulty, capture_reasoning, source, prompt,
                   candidate_answers_json, max_labels, grounding_mode, independent_mode,
                   buyer_request_id, generation_json, value_tier, modality, case_json,
                   case_source, empirical_difficulty, difficulty_measured,
                   open_to_all_specialties, trajectory_id, sequence_index,
                   status, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    tid,
                    specialty,
                    difficulty,
                    1 if capture_reasoning else 0,
                    source,
                    prompt,
                    json.dumps(candidate_answers or []),
                    max(1, int(max_labels or 1)),
                    gm,
                    im,
                    buyer_request_id,
                    json.dumps(generation) if generation else None,
                    (value_tier or None),
                    md,
                    json.dumps(case) if case else None,
                    cs,
                    ed_val,
                    ed_measured,
                    1 if open_to_all_specialties else 0,
                    trajectory_id,
                    sequence_index,
                    created_by,
                    _utcnow_iso(),
                ),
            )
        return self.get_task(tid)  # type: ignore[return-value]

    def update_task_case(self, task_id: str, case: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Replace a task's stored case (V4 Image Embedding PRD §3.5 — attach an image
        asset to a study). Re-derives modality + case_source from the new case. Used by
        the image-ingest path; the image BYTES live in the asset store, only the
        StudyAsset reference is written here."""
        md = "multimodal" if (case and (case.get("lab_panels") or case.get("notes") or case.get("studies"))) else "text"
        cs = ((case.get("case_source") or "synthetic") if case else None)
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET case_json = ?, modality = ?, case_source = ? WHERE task_id = ?",
                (json.dumps(case) if case else None, md, cs, task_id),
            )
        return self.get_task(task_id)

    def insert_asset_ref(self, *, asset_id: str, sha256: str, mime: str,
                         task_id: Optional[str], case_source: Optional[str]) -> None:
        """Index a V4 image asset (V4 Image PRD §4) so serving resolves it in one
        indexed lookup instead of scanning tasks. Idempotent on ``asset_id`` (dedupe:
        the same content → same id → last owning task wins, which is fine — all image
        assets are on real_deid cases)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO study_assets "
                "(asset_id, sha256, mime, task_id, case_source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (asset_id, sha256, mime, task_id, case_source, _utcnow_iso()),
            )

    def get_asset_ref(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Resolve an ``asset_id`` → {sha256, mime, task_id, case_source} via the
        index (O(1)). None if unknown."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT asset_id, sha256, mime, task_id, case_source FROM study_assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return self._task_row(row)

    def set_task_decisive_action(self, task_id: str, action: Dict[str, Any]) -> None:
        """Persist the physician-named verifiable outcome (Audit §13), written from the
        submission — never by an admin or a model: only the clinician who reasoned
        through the case can say which step the answer depends on."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET decisive_action_json = ? WHERE task_id = ?",
                (json.dumps(action) if action else None, task_id),
            )

    @staticmethod
    def _task_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["capture_reasoning"] = bool(rec.get("capture_reasoning"))
        # Stored as an INTEGER; every reader treats it as the boolean it is.
        rec["open_to_all_specialties"] = bool(rec.get("open_to_all_specialties"))
        rec["candidate_answers"] = json.loads(rec.pop("candidate_answers_json", "[]") or "[]")
        rec["generation"] = json.loads(rec.pop("generation_json", "null") or "null")
        # Multimodal case (may be absent on legacy rows / text tasks).
        rec["case"] = json.loads(rec.pop("case_json", "null") or "null")
        # Decisive action (Audit §13): deserialize so packaging/export see a dict,
        # not a JSON string. Absent on legacy rows / tasks nobody named one for.
        rec["decisive_action"] = json.loads(rec.pop("decisive_action_json", "null") or "null")
        return rec

    def list_tasks(
        self, *, specialty: Optional[str] = None, status: Optional[str] = None, limit: int = 500
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
            ).fetchall()
        return [self._task_row(r) for r in rows]

    def submission_count_for_task(self, task_id: str) -> int:
        with self._conn() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM submissions WHERE task_id = ?", (task_id,)
                ).fetchone()[0]
            )

    # ═══ PRD-2: longitudinal trajectories ════════════════════════════════════
    def trajectory_points(self, trajectory_id: str) -> List[Dict[str, Any]]:
        """Every decision point in one chart walk, in sequence order.

        Ordered by ``sequence_index``, with ``created_at`` only breaking ties —
        insertion order is not the walk's order and must never be mistaken for it.
        A regenerated point gets a later ``created_at`` and keeps its position.
        """
        if not trajectory_id:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE trajectory_id = ? "
                "ORDER BY sequence_index ASC, created_at ASC",
                (trajectory_id,),
            ).fetchall()
        return [self._task_row(r) for r in rows]

    def unanswered_earlier_points(
        self, *, trajectory_id: str, sequence_index: int, evaluator_id: str
    ) -> List[Dict[str, Any]]:
        """Earlier points in this walk that THIS evaluator has not submitted.

        The direct-open half of the §9.1 sequence gate. The queue enforces the
        same rule in its WHERE clause (``_PRD_2_SEQUENCE_GATE``); this exists
        because **a queue-only fix is not a fix** — the physician has the task id
        in the URL, the dashboard renders cards that open by id, and a second tab
        is a second draw. Both halves ask ``trajectory.blocks_out_of_order`` for
        the verdict so they cannot drift into two different rules.

        Returns the rows, not a count, so the caller can say which points are
        outstanding rather than only that some are.
        """
        if not trajectory_id:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT p.task_id, p.sequence_index
                  FROM tasks p
                 WHERE p.trajectory_id = ?
                   AND p.sequence_index < ?
                   AND NOT EXISTS (
                       SELECT 1 FROM submissions s
                        WHERE s.task_id = p.task_id AND s.evaluator_id = ?
                   )
                 ORDER BY p.sequence_index ASC
                """,
                (trajectory_id, int(sequence_index), evaluator_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def evaluator_trajectory_progress(
        self, *, trajectory_id: str, evaluator_id: str
    ) -> Dict[str, Any]:
        """How far THIS evaluator has walked this chart — for the session header.

        Progress is per-evaluator on purpose: two physicians walking the same
        chart are two independent trajectories that happen to share a case set,
        and a shared "7 of 13" would be a lie to both of them.
        """
        points = self.trajectory_points(trajectory_id)
        if not points:
            return {"trajectory_id": trajectory_id, "n_points": 0, "n_answered": 0,
                    "next_task_id": None, "complete": False}
        ids = [p["task_id"] for p in points]
        placeholders = ",".join("?" for _ in ids)
        with self._conn() as conn:
            answered = {
                r["task_id"] for r in conn.execute(
                    f"SELECT DISTINCT task_id FROM submissions "
                    f"WHERE evaluator_id = ? AND task_id IN ({placeholders})",
                    tuple([evaluator_id] + ids),
                ).fetchall()
            }
        remaining = [p for p in points if p["task_id"] not in answered]
        return {
            "trajectory_id": trajectory_id,
            "n_points": len(points),
            "n_answered": len(answered),
            # The next point this evaluator may open — which, under the sequence
            # gate, is always the earliest unanswered one.
            "next_task_id": remaining[0]["task_id"] if remaining else None,
            "next_sequence_index": remaining[0].get("sequence_index") if remaining else None,
            "complete": not remaining,
        }

    def set_submission_expected_trajectory(
        self, submission_id: str, expected: Optional[Dict[str, Any]]
    ) -> None:
        """Persist the physician's sealed prediction (§3.3 field 3, §4.2.3).

        Written from the SUBMISSION and only from it — never by an admin, never by
        a model. The whole value of the falsifier corpus is that a named,
        board-certified specialist wrote this before seeing what happened."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE submissions SET expected_trajectory_json = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (json.dumps(expected) if expected else None, _utcnow_iso(), submission_id),
            )

    def set_submission_trajectory_self_score(
        self, submission_id: str, score: Optional[Dict[str, Any]]
    ) -> None:
        """Persist the physician's grading of their own prediction (Phase 4).

        Their own falsifier is the rubric, so this is the one grading step in the
        product with no reviewer in it. Idempotent by overwrite: a physician may
        revise a mark while the revealed encounter is in front of them."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE submissions SET trajectory_self_score_json = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (json.dumps(score) if score else None, _utcnow_iso(), submission_id),
            )

    def trajectory_verification_points(
        self, *, trajectory_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """``[{task_id, trajectory_id, sequence_index, expected_trajectory,
        self_score}, ...]`` — the input to ``trajectory.outcome_verification``.

        Every trajectory submission is returned, INCLUDING those with no
        prediction and those with a prediction but no self-score. The metric's
        denominators are the honest part of it; filtering the unverified rows out
        here would make ``anticipation_rate`` look like a property of the corpus
        rather than of the slice that was actually checked."""
        clauses = ["t.trajectory_id IS NOT NULL"]
        params: List[Any] = []
        if trajectory_id:
            clauses.append("t.trajectory_id = ?")
            params.append(trajectory_id)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT s.submission_id, s.task_id, s.evaluator_id,
                       s.expected_trajectory_json, s.trajectory_self_score_json,
                       t.trajectory_id, t.sequence_index, t.specialty
                  FROM submissions s
                  JOIN tasks t ON t.task_id = s.task_id
                 WHERE {' AND '.join(clauses)}
                 ORDER BY t.trajectory_id ASC, t.sequence_index ASC, s.created_at ASC
                """,
                tuple(params),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            rec = dict(r)
            rec["expected_trajectory"] = json.loads(
                rec.pop("expected_trajectory_json", "null") or "null")
            rec["self_score"] = json.loads(
                rec.pop("trajectory_self_score_json", "null") or "null")
            out.append(rec)
        return out
    # ═══ END PRD-2 ═══════════════════════════════════════════════════════════

    # ─── PRD-R: ONE labeler-queue builder (Audit R H4) ────────────────────────
    def labeler_queue_sql(
        self, *, evaluator_id: str, specialty: Optional[str], hard_only: bool = False,
        real_only: bool = False, multimodal_only: bool = False,
        require_measured_difficulty: bool = False, min_empirical_difficulty: float = 0.0,
        window: int = _PRD_R_SCAN_WINDOW,
    ) -> tuple:
        """``(sql, params)`` for the labeler queue — the ONE definition, shared by
        the classic draw and the value-aware candidate set so they cannot drift.

        Three things make this cheap enough to sit on the submit-path writer:

        * the per-task counts are materialized ONCE by a grouped join, not by a
          correlated scalar subquery inlined five times per row (H4);
        * ``not mine`` is resolved IN SQL, so the window below counts only work
          this labeler could actually take;
        * a lean projection — the full task row is fetched only for candidates
          actually considered, which under the priority sort is normally one.

        The window is therefore safe in the way the unbounded scan was protecting
        against: it cannot hide eligible work behind ineligible work, because
        ineligible work is already gone.
        """
        clauses = [
            _PRD_R_SERVABLE,
            # Independence, in SQL rather than by caller discipline.
            "NOT EXISTS (SELECT 1 FROM submissions sm WHERE sm.task_id = t.task_id"
            " AND sm.evaluator_id = ?)",
            # PRD 2 §9.1 — the sealed future. A correctness property of the task,
            # so it is a WHERE clause here rather than a sort or a UI rule. See
            # ``_PRD_2_SEQUENCE_GATE`` for the four steps by which the priority
            # sort above otherwise serves a physician the outcomes of decisions
            # they have not made yet.
            _PRD_2_SEQUENCE_GATE,
            # An exact NECESSARY condition for remaining capacity: no policy can
            # raise a task's effective capacity above max(max_labels, 2). The
            # exact test still runs in Python, against ``routing`` — one policy,
            # one place — but this keeps the fleet's finished cases out of the
            # window entirely.
            f"{_PRD_R_LABEL_COUNT} < MAX(COALESCE(t.max_labels, 1), 2)",
        ]
        # One entry per ``?`` above, in clause order: the independence NOT EXISTS,
        # then the sequence gate's.
        params: List[Any] = [evaluator_id, evaluator_id]
        if specialty:
            # Launch-week fan-out (V4 PRD §4). ``open_to_all_specialties`` widens
            # VISIBILITY and nothing else: the task appears in this labeler's queue
            # even though its specialty is not theirs. Capacity, independence,
            # ``max_labels`` and the V4 real-data wall below are all untouched — a
            # flagged task is still gated by every one of them, and still pays for
            # exactly the labels it was promoted with.
            #
            # To be exact about what is being widened: this clause is a MATCHING
            # control, not a credential boundary. The specialty picker already lets
            # any labeler request any enabled specialty's queue
            # (``_query_next``: ``serve_specialty = chosen or user.specialty``), so
            # flipping this flag does not defeat an access check — there is none on
            # this axis. What it changes is that a case reaches a pool that did not
            # ask for it, and the annotator's own specialty still ships on the
            # record, so the mismatch is visible to a buyer rather than hidden. The
            # real access boundaries are ``require_label`` (tier) and the
            # ``real_deid`` wall below, and neither is touched here.
            clauses.append("(t.specialty = ? OR COALESCE(t.open_to_all_specialties, 0) = 1)")
            params.append(specialty)
        if hard_only:
            clauses.append("t.difficulty = 'hard'")
        # V3 multimodal-only queue (default): serve structured cases only.
        if multimodal_only:
            clauses.append("t.modality = 'multimodal'")
        # Empirical-difficulty gate (PRD §9): when required, serve only cases whose
        # frontier-failure rate was LIVE-measured above the floor. OFF by default so
        # declared/authored seeds still serve in dev without live frontier keys.
        if require_measured_difficulty:
            clauses.append("(t.difficulty_measured = 1 AND t.empirical_difficulty >= ?)")
            params.append(float(min_empirical_difficulty))
        # The V4 wall (EHR PRD §9.5), enforced in SQL: real_only serves ONLY
        # case_source='real_deid'; otherwise they are excluded entirely, so a real
        # patient case can never reach a v1/v2/v3 session.
        clauses.append(
            "t.case_source = 'real_deid'" if real_only
            else "(t.case_source IS NULL OR t.case_source != 'real_deid')"
        )
        # Admin review queue (Audit PRD §21.6): a task whose ingest case still carries
        # an unresolved BLOCKING review reason (ingest_cases.status = 'needs_review')
        # must never be served for annotation — a physician must not label a case whose
        # image may still carry burned-in PHI. Clearing/rejecting flips that status, so
        # this releases the case the moment a human resolves it.
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM ingest_cases ic WHERE ic.task_id = t.task_id "
            "AND ic.status = 'needs_review')"
        )
        sql = f"""
            SELECT t.task_id, {_PRD_R_LABEL_COUNT} AS label_count,
                   COALESCE(c.n_all, 0) AS sub_count
            FROM tasks t
            {_PRD_R_COUNTS_JOIN}
            WHERE {' AND '.join(clauses)}
            {_PRD_R_PRIORITY_ORDER}
            LIMIT ?
            """
        params.append(int(window))
        return sql, tuple(params)

    def _labeler_candidates(self, **kw) -> List[Dict[str, Any]]:
        sql, params = self.labeler_queue_sql(**kw)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def next_task_for_evaluator(
        self, *, evaluator_id: str, specialty: Optional[str], hard_only: bool = False,
        real_only: bool = False, multimodal_only: bool = False,
        require_measured_difficulty: bool = False, min_empirical_difficulty: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """Oldest servable task in the evaluator's specialty that they have not
        already submitted and that still has label capacity.

        PRD R §1.2: the candidate set also admits a 'done' task carrying a single
        label (see ``_PRD_R_SERVABLE``) and is sorted so a task awaiting its
        second label is offered before a fresh one.
        """
        from asclepius import routing as asc_routing   # pure module; no cycle
        rows = self._labeler_candidates(
            evaluator_id=evaluator_id, specialty=specialty, hard_only=hard_only,
            real_only=real_only, multimodal_only=multimodal_only,
            require_measured_difficulty=require_measured_difficulty,
            min_empirical_difficulty=min_empirical_difficulty,
        )
        for r in rows:
            task = self.get_task(r["task_id"])
            if task is None:
                continue
            label_count = int(r["label_count"] or 0)
            # PRD R §1.2: capacity is DERIVED, so a case not yet flagged for its
            # second label is still servable. Counted in LABELS (Audit R M1) —
            # the same number eligibility and the priority sort use.
            if label_count >= asc_routing.effective_capacity(task):
                continue
            # Catch the stored capacity up to the derived one, on the same
            # request, for the ONE task we are about to serve.
            if self._prd_r_lift_capacity([(task, label_count)]):
                return self.get_task(task["task_id"]) or task
            return task
        return None

    # ─── PRD-R capacity catch-up ──────────────────────────────────────────────
    def _prd_r_lift_capacity(self, pairs: List[tuple]) -> List[str]:
        """Persist ``max_labels = 2`` (+ reopen) for singly-labelled candidates
        the policy wants double-labelled, in one batched UPDATE. Returns the
        task_ids actually changed.

        ``pairs`` is ``[(task_dict, verdict_bearing_label_count), ...]``.

        PRD R §1.1 asks for this on the submit path. The submit route lives in
        ``routers/asclepius.py``, which Agent R does not own (context pack §2),
        and ``refresh_task_status`` — the one hook Agent R can reach on that path
        — is explicitly read-only. So the write happens at the next moment it can
        possibly matter: the draw that is about to serve the case. The queue
        itself never depends on it having happened (eligibility and capacity are
        derived), which makes this bookkeeping rather than a race — and leaves
        the existing background sweep as a second, independent path to the same
        state.

        Only tasks that ALREADY carry a label are lifted. Pre-flagging an
        unlabelled task would be defensible under a 1.0 rate, but ``max_labels``
        means "capacity we have committed to", and committing before a first
        label exists would silently re-price every task in the queue.
        """
        from asclepius import routing as asc_routing
        want = [t["task_id"] for (t, n) in pairs
                if int(n or 0) >= 1
                and asc_routing.target_labels(t) < asc_routing.PAIR_LABELS
                and asc_routing.wants_second_label(t)]
        if not want:
            return []
        return self.flag_tasks_for_double_label(
            [{"task_id": tid, "specialty": None, "current_rate": None} for tid in want])

    def eligible_tasks_for_evaluator(
        self, *, evaluator_id: str, specialty: Optional[str], limit: Optional[int] = None,
        hard_only: bool = False, real_only: bool = False, multimodal_only: bool = False,
        require_measured_difficulty: bool = False, min_empirical_difficulty: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Tasks this evaluator may take, priority-ordered — the candidate set
        value-aware routing (Value-per-Minute PRD B3) ranks by value-per-minute.

        Shares ``labeler_queue_sql`` with the classic draw, so the two cannot
        disagree about who may take what.

        On the window (Audit R H4): this scan used to be deliberately UNBOUNDED,
        because a ``LIMIT`` applied BEFORE the not-mine/capacity filter starves
        the ranker — if the N oldest candidates are all ineligible, a capped
        fetch returns nothing while eligible work sits further down. Those
        filters now run IN SQL, so the window contains only work this evaluator
        can actually take and that objection no longer holds. What the window
        does cost is reach: with more eligible cases than the window, the ranker
        sees the highest-PRIORITY ones rather than every one. That is the right
        trade — the priority sort is the throughput rule — and it is stated here
        rather than discovered later.

        ``limit`` caps the returned candidates after the exact capacity check.

        PRD R §1.2: lifting an awaiting-second task to ``max_labels = 2`` also
        turns on ``value._tier_mult``'s double-labeled-credentialed multiplier,
        so the case the queue wants finished scores higher on its own merits and
        the priority survives the re-rank rather than only breaking ties."""
        from asclepius import routing as asc_routing   # pure module; no cycle
        rows = self._labeler_candidates(
            evaluator_id=evaluator_id, specialty=specialty, hard_only=hard_only,
            real_only=real_only, multimodal_only=multimodal_only,
            require_measured_difficulty=require_measured_difficulty,
            min_empirical_difficulty=min_empirical_difficulty,
        )
        out: List[Dict[str, Any]] = []
        lift: List[tuple] = []
        for r in rows:
            task = self.get_task(r["task_id"])
            if task is None:
                continue
            label_count = int(r["label_count"] or 0)
            if label_count >= asc_routing.effective_capacity(task):
                continue
            lift.append((task, label_count))
            out.append(task)
            if limit is not None and len(out) >= limit:
                break
        # One batched catch-up over the candidate window (see
        # ``_prd_r_lift_capacity``). Bounded by the window, idempotent, and empty
        # in steady state — the UPDATE carries ``AND max_labels < 2``, so a task
        # is only ever written once. Re-read the lifted rows so the caller ranks
        # on the capacity it will actually be served with.
        lifted = set(self._prd_r_lift_capacity(lift))
        if lifted:
            out = [(self.get_task(t["task_id"]) or t) if t["task_id"] in lifted else t
                   for t in out]
        return out

    def evaluator_median_seconds(self, evaluator_id: str) -> Optional[float]:
        """The contributor's rolling median seconds-per-task (Value-per-Minute
        PRD B3 routing denominator). Median, not mean, so one slow outlier task
        doesn't distort routing. None until they have any timed submission."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT time_spent_sec FROM submissions "
                "WHERE evaluator_id = ? AND time_spent_sec > 0 ORDER BY time_spent_sec ASC",
                (evaluator_id,),
            ).fetchall()
        vals = [int(r["time_spent_sec"]) for r in rows]
        if not vals:
            return None
        n = len(vals)
        mid = n // 2
        return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0

    def mark_task_status(self, task_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (status, task_id))

    def set_task_candidates(
        self, task_id: str, candidates: List[Dict[str, Any]], *, generation_patch: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Replace a task's candidate answers in place (FEAT-1 "grade the real
        models" mode swaps in a baseline A/B pair). Optionally merge a patch into
        the task's generation provenance block. Does not touch status/created_at."""
        task = self.get_task(task_id)
        if not task:
            return None
        gen = task.get("generation") or {}
        if generation_patch:
            gen = {**gen, **generation_patch}
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET candidate_answers_json = ?, generation_json = ? WHERE task_id = ?",
                (json.dumps(candidates or []), json.dumps(gen) if gen else None, task_id),
            )
        return self.get_task(task_id)

    def refresh_task_status(self, task_id: str) -> None:
        """Close a task once it has reached its label capacity.

        A task a clinician flagged as having an invalid prompt (Eval Flow Upgrade
        §2) is terminal — never reopen/close it back to a normal status, so it
        stays out of the queue and visible in the admin flagged list even if a
        concurrent normal submission also lands on it."""
        task = self.get_task(task_id)
        if not task:
            return
        # Terminal Stage-1 flags never reopen/close back to a normal status, so
        # they stay out of the queue even if a concurrent normal submission lands:
        #   prompt_flagged — clinically invalid prompt (Eval Flow Upgrade §2)
        #   not_hard       — valid but not a hard case (Seamless PRD WS2)
        #   case_incoherent— internally inconsistent multimodal case (Multimodal §5)
        if task.get("status") in ("prompt_flagged", "not_hard", "case_incoherent"):
            return
        count = self.submission_count_for_task(task_id)
        new_status = "done" if count >= int(task.get("max_labels") or 1) else "open"
        self.mark_task_status(task_id, new_status)

    # ─── Independent-answer reveal gate (Eval Flow Upgrade §1) ──────────────────
    def commit_independent_answer(
        self, *, task_id: str, evaluator_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Record (idempotently) that ``evaluator_id`` committed a blind independent
        answer for ``task_id`` BEFORE the candidate answers were revealed. The FIRST
        commit wins (``INSERT OR IGNORE``) — a later re-reveal never overwrites the
        original pre-reveal answer or timestamp. ``captured_at`` is forced to server
        time, never trusted from the client."""
        now = _utcnow_iso()
        payload = dict(payload or {})
        payload["captured_at"] = now
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO independent_commits "
                "(task_id, evaluator_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, evaluator_id, json.dumps(payload), now),
            )
        return self.get_independent_commit(task_id, evaluator_id)  # type: ignore[return-value]

    def get_independent_commit(
        self, task_id: str, evaluator_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM independent_commits WHERE task_id = ? AND evaluator_id = ?",
                (task_id, evaluator_id),
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["payload"] = json.loads(rec.pop("payload_json", "{}") or "{}")
        return rec

    # ─── Submissions ──────────────────────────────────────────────────────────
    def get_submission(self, submission_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)
            ).fetchone()
        return self._submission_row(row) if row else None

    @staticmethod
    def _submission_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["payload"] = json.loads(rec.pop("payload_json", "{}") or "{}")
        rec["validation"] = json.loads(rec.pop("validation_json", "null") or "null")
        rec["critic"] = json.loads(rec.pop("critic_json", "null") or "null")
        rec["qa"] = json.loads(rec.pop("qa_json", "null") or "null")
        rec["annotator"] = json.loads(rec.pop("annotator_json", "null") or "null")
        rec["progress"] = json.loads(rec.pop("progress_json", "null") or "null")
        # PRD 2 §4.2.3 — the sealed prediction and the physician's own grading of
        # it. Deserialized here so packaging and export see dicts, not JSON
        # strings; absent (None) on every non-trajectory submission, which is all
        # of them until a trajectory is generated.
        rec["expected_trajectory"] = json.loads(
            rec.pop("expected_trajectory_json", "null") or "null")
        rec["trajectory_self_score"] = json.loads(
            rec.pop("trajectory_self_score_json", "null") or "null")
        return rec

    def set_submission_progress(
        self, submission_id: str, *, phase: str, pct: int, detail: Optional[str] = None
    ) -> None:
        """Stamp the real, backend-observed pipeline phase onto a submission (BUG-5).
        Called by the pipeline when each stage ACTUALLY starts — the client polls
        ``GET /submissions/{id}/status`` and shows this exact phase + pct."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE submissions SET progress_json = ?, updated_at = ? WHERE submission_id = ?",
                (json.dumps({"phase": phase, "pct": int(pct), "detail": detail}),
                 _utcnow_iso(), submission_id),
            )

    def insert_submission(
        self,
        *,
        submission_id: str,
        task_id: str,
        evaluator_id: str,
        verdict: Optional[str],
        chosen_id: Optional[str],
        rejected_id: Optional[str],
        confidence: Optional[str],
        time_spent_sec: int,
        payload: Dict[str, Any],
        annotator: Dict[str, Any],
        dedupe_hash: Optional[str],
        grounded: bool = False,
        grounding_mode: str = "optional",
        portal_version: str = "v2",
        status: str = "submitted",
    ) -> Dict[str, Any]:
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO submissions
                  (submission_id, task_id, evaluator_id, verdict, chosen_id, rejected_id,
                   confidence, time_spent_sec, status, dedupe_hash, grounded, grounding_mode,
                   portal_version, payload_json, annotator_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    task_id,
                    evaluator_id,
                    verdict,
                    chosen_id,
                    rejected_id,
                    confidence,
                    int(time_spent_sec or 0),
                    status,
                    dedupe_hash,
                    1 if grounded else 0,
                    grounding_mode,
                    portal_version,
                    json.dumps(payload),
                    json.dumps(annotator),
                    now,
                    now,
                ),
            )
        return self.get_submission(submission_id)  # type: ignore[return-value]

    def update_submission(self, submission_id: str, **fields: Any) -> None:
        if not fields:
            return
        json_cols = {"validation", "critic", "qa"}
        sets, params = [], []
        for key, value in fields.items():
            if key in json_cols:
                sets.append(f"{key}_json = ?")
                params.append(json.dumps(value) if value is not None else None)
            else:
                sets.append(f"{key} = ?")
                params.append(value)
        sets.append("updated_at = ?")
        params.append(_utcnow_iso())
        params.append(submission_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE submissions SET {', '.join(sets)} WHERE submission_id = ?",
                tuple(params),
            )

    def list_submissions(
        self,
        *,
        status: Optional[str] = None,
        specialty: Optional[str] = None,
        evaluator_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("s.status = ?")
            params.append(status)
        if evaluator_id:
            clauses.append("s.evaluator_id = ?")
            params.append(evaluator_id)
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT s.* FROM submissions s
                JOIN tasks t ON t.task_id = s.task_id
                {where}
                ORDER BY s.created_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._submission_row(r) for r in rows]

    def submissions_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM submissions WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [self._submission_row(r) for r in rows]

    # ─── Records ──────────────────────────────────────────────────────────────
    def insert_record(
        self,
        *,
        submission_id: str,
        task_id: str,
        rtype: str,
        specialty: Optional[str],
        payload: Dict[str, Any],
        status: str = "submitted",
    ) -> str:
        rid = _new_id("rec")
        payload = dict(payload)
        payload.setdefault("record_id", rid)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO records
                  (record_id, submission_id, task_id, type, specialty, status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    submission_id,
                    task_id,
                    rtype,
                    specialty,
                    status,
                    json.dumps(payload),
                    _utcnow_iso(),
                ),
            )
        return rid

    @staticmethod
    def _record_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["payload"] = json.loads(rec.pop("payload_json", "{}") or "{}")
        return rec

    def records_for_submission(self, submission_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM records WHERE submission_id = ? ORDER BY created_at ASC",
                (submission_id,),
            ).fetchall()
        return [self._record_row(r) for r in rows]

    def update_records_status_for_submission(self, submission_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE records SET status = ? WHERE submission_id = ?",
                (status, submission_id),
            )

    def patch_record_payload(self, record_id: str, patch: Dict[str, Any]) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if not row:
                return
            payload = json.loads(row["payload_json"] or "{}")
            payload.update(patch)
            conn.execute(
                "UPDATE records SET payload_json = ? WHERE record_id = ?",
                (json.dumps(payload), record_id),
            )

    def list_records(
        self,
        *,
        status: Optional[str] = None,
        rtype: Optional[str] = None,
        specialty: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100000,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if rtype:
            clauses.append("type = ?")
            params.append(rtype)
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM records {where} ORDER BY created_at ASC LIMIT ?", tuple(params)
            ).fetchall()
        return [self._record_row(r) for r in rows]

    def mark_records_exported(self, record_ids: List[str], export_id: str) -> None:
        if not record_ids:
            return
        with self._conn() as conn:
            conn.executemany(
                "UPDATE records SET status = 'exported', export_id = ? WHERE record_id = ?",
                [(export_id, rid) for rid in record_ids],
            )

    # ─── Events (provenance) ────────────────────────────────────────────────--
    def log_event(
        self,
        *,
        entity_type: str,
        event_type: str,
        entity_id: Optional[str] = None,
        actor: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[str] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO events (entity_type, entity_id, event_type, actor, occurred_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_type,
                    entity_id,
                    event_type,
                    actor,
                    occurred_at or _utcnow_iso(),
                    json.dumps(payload or {}),
                ),
            )

    def list_events(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", tuple(params)
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["payload"] = json.loads(rec.pop("payload_json", "{}") or "{}")
            out.append(rec)
        return out

    # ─── Exports ────────────────────────────────────────────────────────────--
    def insert_export(
        self,
        *,
        export_id: str,
        created_by: Optional[str],
        record_count: int,
        filters: Dict[str, Any],
        dir_path: str,
        manifest: Dict[str, Any],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO exports
                  (export_id, created_by, created_at, record_count, filters_json, dir_path, manifest_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    export_id,
                    created_by,
                    _utcnow_iso(),
                    record_count,
                    json.dumps(filters),
                    dir_path,
                    json.dumps(manifest),
                ),
            )

    def get_export(self, export_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM exports WHERE export_id = ?", (export_id,)
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["filters"] = json.loads(rec.pop("filters_json", "{}") or "{}")
        rec["manifest"] = json.loads(rec.pop("manifest_json", "{}") or "{}")
        return rec

    def list_exports(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM exports ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["filters"] = json.loads(rec.pop("filters_json", "{}") or "{}")
            rec["manifest"] = json.loads(rec.pop("manifest_json", "{}") or "{}")
            out.append(rec)
        return out

    # ─── Stats (admin dashboard, PRD §7.6) ────────────────────────────────────
    def status_counts(self) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM submissions GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def set_submission_value(
        self,
        submission_id: str,
        *,
        realized: Optional[float],
        projected: Optional[float],
        clinician_review_seconds: Optional[int],
    ) -> None:
        """Persist the value estimate + clinician-minutes for a submission
        (Value-per-Minute PRD A4). Measurement only — never touches records,
        status, or any v1/v2 behavior."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE submissions SET value_estimate_usd = ?, "
                "value_estimate_projected_usd = ?, clinician_review_seconds = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (
                    None if realized is None else round(float(realized), 2),
                    None if projected is None else round(float(projected), 2),
                    None if clinician_review_seconds is None else int(clinician_review_seconds),
                    _utcnow_iso(),
                    submission_id,
                ),
            )

    @staticmethod
    def _median(vals: List[float]) -> Optional[float]:
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        n = len(vals)
        mid = n // 2
        return round(float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0, 2)

    def value_per_time_rows(self) -> List[Dict[str, Any]]:
        """Raw per-submission value + time + segmenting attributes for the V/T
        report (Value-per-Minute PRD A4). Only rows with a value estimate AND
        positive time contribute a ratio (an un-timed row has no defined V/T)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.evaluator_id,
                       u.email                                   AS evaluator_email,
                       s.portal_version                          AS portal_version,
                       t.difficulty                              AS difficulty,
                       t.source                                  AS source,
                       s.grounded                                AS grounded,
                       s.value_estimate_usd                      AS realized,
                       s.value_estimate_projected_usd            AS projected,
                       COALESCE(s.clinician_review_seconds, s.time_spent_sec) AS seconds
                FROM submissions s
                JOIN tasks t ON t.task_id = s.task_id
                LEFT JOIN users u ON u.id = s.evaluator_id
                WHERE s.value_estimate_usd IS NOT NULL
                  AND COALESCE(s.clinician_review_seconds, s.time_spent_sec) > 0
                  AND s.status != 'rejected'
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def value_per_time_stats(self) -> Dict[str, Any]:
        """Median realized + projected value-per-minute, split by portal_version
        (v1 vs v2), difficulty, grounded vs plain, Mode A vs B, and per
        contributor (Value-per-Minute PRD A4). Medians (robust to outliers).
        Realized is what the team is held to; projected is the reuse forecast."""
        rows = self.value_per_time_rows()

        def vpm(r: Dict[str, Any], key: str) -> Optional[float]:
            secs = r.get("seconds") or 0
            val = r.get(key)
            if not secs or val is None:
                return None
            return float(val) / (secs / 60.0)

        def summarize(subset: List[Dict[str, Any]]) -> Dict[str, Any]:
            realized = [x for x in (vpm(r, "realized") for r in subset) if x is not None]
            projected = [x for x in (vpm(r, "projected") for r in subset) if x is not None]
            return {
                "n": len(subset),
                "realized_vpm": self._median(realized),
                "projected_vpm": self._median(projected),
                "realized_value_median": self._median([float(r["realized"]) for r in subset if r.get("realized") is not None]),
            }

        def group_by(fn) -> Dict[str, Any]:
            buckets: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                buckets.setdefault(str(fn(r)), []).append(r)
            return {k: summarize(v) for k, v in buckets.items()}

        return {
            "overall": summarize(rows),
            "by_portal_version": group_by(lambda r: r.get("portal_version") or "v2"),
            "by_difficulty": group_by(lambda r: r.get("difficulty") or "medium"),
            "by_grounded": group_by(lambda r: "grounded" if r.get("grounded") else "plain"),
            "by_mode": group_by(lambda r: "mode_b" if (r.get("source") == "lab_supplied") else "mode_a"),
            "by_contributor": group_by(lambda r: r.get("evaluator_email") or r.get("evaluator_id") or "—"),
            "target": None,  # filled by the router from constants (keeps store I/O-free)
        }

    def override_rate_stats(self, *, portal_version: Optional[str] = "v2") -> Dict[str, Any]:
        """Model-assist override rate (Value-per-Minute PRD Part D quality gate):
        of the assisted submissions where a suggestion existed, how often did the
        clinician's FINAL differ from the machine SUGGESTION? A near-zero rate
        flags rubber-stamping. Scoped to v2 (only the assisted flow pre-labels).

        Verdict override: final verdict != assist.suggested_verdict.
        Step override: any reasoning step whose final label != suggested_label."""
        params: List[Any] = []
        pv_clause = ""
        if portal_version:
            pv_clause = "WHERE s.portal_version = ?"
            params.append(portal_version)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT s.verdict, s.payload_json FROM submissions s {pv_clause}",
                tuple(params),
            ).fetchall()
        verdict_total = verdict_overrides = 0
        step_total = step_overrides = 0
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except (ValueError, TypeError):
                continue
            assist = payload.get("assist") or {}
            if assist.get("prelabeled") and assist.get("suggested_verdict"):
                verdict_total += 1
                if (r["verdict"] or None) != assist.get("suggested_verdict"):
                    verdict_overrides += 1
            for step in payload.get("reasoning_steps") or []:
                sug = step.get("suggested_label")
                if sug is None:
                    continue
                step_total += 1
                if (step.get("label") or None) != sug:
                    step_overrides += 1
            for src in ("from_scratch",):
                for step in (payload.get(src) or {}).get("reasoning_steps") or []:
                    sug = step.get("suggested_label")
                    if sug is None:
                        continue
                    step_total += 1
                    if (step.get("label") or None) != sug:
                        step_overrides += 1
        return {
            "portal_version": portal_version,
            "verdict": {
                "assisted": verdict_total,
                "overrides": verdict_overrides,
                "override_rate": round(verdict_overrides / verdict_total, 3) if verdict_total else None,
            },
            "steps": {
                "assisted": step_total,
                "overrides": step_overrides,
                "override_rate": round(step_overrides / step_total, 3) if step_total else None,
            },
        }

    def portal_version_counts(self) -> Dict[str, int]:
        """Submissions by evaluator product version — lets the admin dashboard
        show how much data came from V1 (classic) vs V2 (assisted) vs V3
        (seamless)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT portal_version, COUNT(*) AS n FROM submissions GROUP BY portal_version"
            ).fetchall()
        return {(r["portal_version"] or "v2"): int(r["n"]) for r in rows}

    def open_multimodal_count(self, specialty: Optional[str] = None) -> int:
        """Count OPEN (servable, not-yet-fully-labeled) V3 multimodal cases — the
        unlabeled pool a top-up fills to ``target_pool_size`` (PRD §B4). Optionally
        scoped to a specialty."""
        clauses = ["status = 'open'", "COALESCE(modality, 'text') = 'multimodal'"]
        params: List[Any] = []
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM tasks WHERE {' AND '.join(clauses)}", tuple(params)
            ).fetchone()
        return int(row["n"] or 0)

    def open_modality_counts(self) -> Dict[str, int]:
        """OPEN (servable) tasks by modality (Multimodal Debug PRD P3.11) — the
        admin dashboard shows "multimodal in queue: N" so an operator always knows
        whether structured cases exist without inspecting the tasks table."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT COALESCE(modality, 'text') AS m, COUNT(*) AS n "
                "FROM tasks WHERE status = 'open' GROUP BY m"
            ).fetchall()
        counts = {r["m"]: int(r["n"]) for r in rows}
        counts.setdefault("text", 0)
        counts.setdefault("multimodal", 0)
        return counts

    def ab_balance_stats(self) -> Dict[str, Any]:
        """Position-bias QC (Seamless PRD WS6). The stronger/weaker A-B slot is
        randomized 50/50 at candidate build (``critic.generate_candidates_ex``)
        so a reward model can't learn "A is better" instead of "the better answer
        is better". Over generated tasks carrying a server-side
        ``intended_flawed_id`` (never shown to the blinded evaluator), report the
        fraction whose STRONGER answer landed in slot A — a rate that drifts from
        ~0.5 is a QC alarm a competent buyer would also detect."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT generation_json FROM tasks WHERE generation_json IS NOT NULL"
            ).fetchall()
        n = 0
        a_stronger = 0
        for r in rows:
            try:
                gen = json.loads(r["generation_json"] or "null")
            except (ValueError, TypeError):
                continue
            if not isinstance(gen, dict):
                continue
            fid = gen.get("intended_flawed_id")
            if fid not in ("A", "B"):
                continue
            n += 1
            if fid == "B":  # flawed answer in B ⇒ the stronger answer is in A
                a_stronger += 1
        return {
            "n": n,
            "a_stronger": a_stronger,
            "a_stronger_rate": round(a_stronger / n, 3) if n else None,
        }

    def evaluator_throughput(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.evaluator_id,
                       u.email,
                       u.specialty,
                       COUNT(*) AS submissions,
                       AVG(s.time_spent_sec) AS avg_time_sec,
                       SUM(CASE WHEN s.status IN ('export_ready','exported') THEN 1 ELSE 0 END) AS export_ready
                FROM submissions s
                LEFT JOIN users u ON u.id = s.evaluator_id
                GROUP BY s.evaluator_id
                ORDER BY submissions DESC
                """
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["avg_time_sec"] = round(rec["avg_time_sec"], 1) if rec["avg_time_sec"] is not None else None
            out.append(rec)
        return out

    def qa_pass_rate(self) -> Dict[str, Any]:
        counts = self.status_counts()
        reviewed = counts.get("export_ready", 0) + counts.get("exported", 0) + counts.get("rejected", 0)
        passed = counts.get("export_ready", 0) + counts.get("exported", 0)
        rate = round(passed / reviewed, 3) if reviewed else None
        return {"reviewed": reviewed, "passed": passed, "pass_rate": rate}

    def average_agreement(self) -> Optional[float]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT AVG(agreement_score) AS a FROM submissions WHERE agreement_score IS NOT NULL"
            ).fetchone()
        return round(row["a"], 3) if row and row["a"] is not None else None

    def grounded_counts(self) -> Dict[str, int]:
        """Grounded vs total submissions + records (opt §1.2 premium tier)."""
        with self._conn() as conn:
            sub_total = int(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])
            sub_grounded = int(
                conn.execute("SELECT COUNT(*) FROM submissions WHERE grounded = 1").fetchone()[0]
            )
        return {"submissions_total": sub_total, "submissions_grounded": sub_grounded}

    def contributor_stats(self) -> List[Dict[str, Any]]:
        """Per-evaluator credential mix, hours, counts, and PREMIUM (grounded /
        grounding_mode=required) completion tracked separately (opt §1.2, §1.4)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.evaluator_id,
                       u.email,
                       u.specialty,
                       u.board_cert,
                       u.years_experience,
                       COUNT(*)                                  AS submissions,
                       SUM(s.time_spent_sec)                     AS total_time_sec,
                       AVG(s.time_spent_sec)                     AS avg_time_sec,
                       SUM(CASE WHEN s.grounding_mode = 'required' THEN 1 ELSE 0 END) AS premium_submissions,
                       SUM(CASE WHEN s.grounding_mode = 'required' THEN s.time_spent_sec ELSE 0 END) AS premium_time_sec,
                       SUM(CASE WHEN s.grounded = 1 THEN 1 ELSE 0 END) AS grounded_submissions
                FROM submissions s
                LEFT JOIN users u ON u.id = s.evaluator_id
                GROUP BY s.evaluator_id
                ORDER BY submissions DESC
                """
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["avg_time_sec"] = round(rec["avg_time_sec"], 1) if rec["avg_time_sec"] is not None else None
            rec["total_hours"] = round((rec.get("total_time_sec") or 0) / 3600.0, 2)
            rec["premium_hours"] = round((rec.get("premium_time_sec") or 0) / 3600.0, 2)
            credential = rec.get("board_cert") or (
                f"board_certified_{rec.get('specialty')}" if rec.get("specialty") else "unspecified"
            )
            rec["credential"] = credential
            out.append(rec)
        return out

    def evaluator_self_stats(self, evaluator_id: str) -> Dict[str, Any]:
        """Real, personal counts for the dashboard's own tracking widget: total
        cases this evaluator has completed, how many in the last 7 days, and
        when they last submitted one. No earnings/streak data exists anywhere
        in this schema, so this stays limited to what's actually true."""
        week_cutoff = (datetime.utcnow().replace(microsecond=0) - timedelta(days=7)).isoformat()
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE evaluator_id = ?",
                (evaluator_id,),
            ).fetchone()["n"]
            this_week = conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE evaluator_id = ? AND created_at >= ?",
                (evaluator_id, week_cutoff),
            ).fetchone()["n"]
            last_at = conn.execute(
                "SELECT MAX(created_at) AS t FROM submissions WHERE evaluator_id = ?",
                (evaluator_id,),
            ).fetchone()["t"]
        return {
            "submissions_total": total,
            "submissions_this_week": this_week,
            "last_submission_at": last_at,
        }

    # ─── Buyers & buyer requests (opt §2.5) ──────────────────────────────────
    def create_buyer(
        self, *, name: str, contact: Optional[str] = None,
        export_profile: str = "default", notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        bid = _new_id("buyer")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO buyers (buyer_id, name, contact, export_profile, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (bid, name, contact, export_profile or "default", notes, _utcnow_iso()),
            )
        return self.get_buyer(bid)  # type: ignore[return-value]

    def get_buyer(self, buyer_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM buyers WHERE buyer_id = ?", (buyer_id,)).fetchone()
        return dict(row) if row else None

    def list_buyers(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM buyers ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def create_buyer_request(
        self, *, buyer_id: str, source: str, export_profile: str,
        constraints: Dict[str, Any], uploaded: List[Dict[str, Any]],
        note: Optional[str] = None, created_by: Optional[str] = None,
        status: str = "draft",
    ) -> Dict[str, Any]:
        rid = _new_id("req")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO buyer_requests
                  (request_id, buyer_id, status, source, export_profile, constraints_json,
                   uploaded_json, note, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid, buyer_id, status, source, export_profile or "default",
                    json.dumps(constraints or {}), json.dumps(uploaded or []),
                    note, created_by, now, now,
                ),
            )
        return self.get_buyer_request(rid)  # type: ignore[return-value]

    @staticmethod
    def _buyer_request_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["constraints"] = json.loads(rec.pop("constraints_json", "{}") or "{}")
        rec["uploaded"] = json.loads(rec.pop("uploaded_json", "[]") or "[]")
        return rec

    def get_buyer_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM buyer_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return self._buyer_request_row(row) if row else None

    def list_buyer_requests(self, *, buyer_id: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if buyer_id:
            clauses.append("buyer_id = ?")
            params.append(buyer_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM buyer_requests {where} ORDER BY created_at DESC", tuple(params)
            ).fetchall()
        return [self._buyer_request_row(r) for r in rows]

    def update_buyer_request_status(self, request_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE buyer_requests SET status = ?, updated_at = ? WHERE request_id = ?",
                (status, _utcnow_iso(), request_id),
            )

    # ─── Contributor credentials + organizations (tiered export) ─────────────
    def upsert_contributor_credentials(
        self,
        *,
        id_hashed: str,
        user_id: Optional[str] = None,
        organization: Optional[str] = None,
        role_title: Optional[str] = None,
        blurb: Optional[str] = None,
        credentials_verified: bool = False,
        ship: Optional[Dict[str, Any]] = None,
        verify: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create/replace a contributor's credential profile. ``ship`` is the Tier
        A (buyer-facing) attribute block; ``verify`` is the Tier B private vault
        (sealed at rest)."""
        now = _utcnow_iso()
        verify_blob, verify_enc = seal_vault(verify or {})
        existing = self.get_contributor_credentials(id_hashed)
        created_at = existing["created_at"] if existing else now
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO contributor_credentials
                  (id_hashed, user_id, organization, role_title, blurb,
                   credentials_verified, ship_json, verify_blob, verify_enc,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id_hashed,
                    user_id,
                    organization,
                    role_title,
                    blurb,
                    1 if credentials_verified else 0,
                    json.dumps(ship or {}, ensure_ascii=False),
                    verify_blob,
                    verify_enc,
                    created_at,
                    now,
                ),
            )
        return self.get_contributor_credentials(id_hashed, include_verify=True)  # type: ignore[return-value]

    def get_contributor_credentials(
        self, id_hashed: str, *, include_verify: bool = False
    ) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM contributor_credentials WHERE id_hashed = ?", (id_hashed,)
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["credentials_verified"] = bool(rec.get("credentials_verified"))
        rec["ship"] = json.loads(rec.pop("ship_json", "{}") or "{}")
        verify_blob = rec.pop("verify_blob", None)
        verify_enc = rec.pop("verify_enc", 0)
        rec["verify_encrypted"] = bool(verify_enc)
        if include_verify:
            rec["verify"] = open_vault(verify_blob, verify_enc)
        return rec

    def list_contributor_credentials(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id_hashed FROM contributor_credentials ORDER BY updated_at DESC"
            ).fetchall()
        return [self.get_contributor_credentials(r["id_hashed"]) for r in rows if r]  # type: ignore[misc]

    def _record_counts_by_evaluator(self) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.evaluator_id AS eid, COUNT(*) AS n
                FROM records r
                JOIN submissions s ON s.submission_id = r.submission_id
                GROUP BY s.evaluator_id
                """
            ).fetchall()
        return {r["eid"]: int(r["n"]) for r in rows}

    def contributor_directory(self) -> List[Dict[str, Any]]:
        """One row per contributor (a user who has labeled OR has a credential
        profile): internal display name, hashed id, organization, role, primary
        specialty, # records labeled, verified status, and last-labeled time."""
        rec_counts = self._record_counts_by_evaluator()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT u.id, u.id_hashed, u.email, u.role, u.specialty, u.is_mock,
                       -- The onboarding flow historically wrote the health-system
                       -- name to org_name; the canonical column is organization.
                       -- COALESCE both so existing onboarded users (organization
                       -- NULL, org_name set) resolve to their real org, not
                       -- "Unaffiliated".
                       COALESCE(u.organization, u.org_name) AS user_org,
                       COUNT(DISTINCT s.submission_id) AS submission_count,
                       MAX(s.created_at) AS last_labeled_at
                FROM users u
                LEFT JOIN submissions s ON s.evaluator_id = u.id
                GROUP BY u.id
                ORDER BY u.created_at ASC
                """
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            cred = self.get_contributor_credentials(r["id_hashed"]) if r["id_hashed"] else None
            submission_count = int(r["submission_count"] or 0)
            has_cred = cred is not None
            # Skip accounts that have neither labeled nor been credentialed (e.g.
            # the bootstrap admin) — they are not "contributors".
            if submission_count == 0 and not has_cred:
                continue
            ship = (cred or {}).get("ship") or {}
            from asclepius.constants import UNASSIGNED_ORG
            organization = (
                (cred or {}).get("organization")
                or r["user_org"]
                or UNASSIGNED_ORG
            )
            primary_specialty = ship.get("primary_specialty") or r["specialty"]
            out.append(
                {
                    "user_id": r["id"],
                    "id_hashed": r["id_hashed"],
                    "display_name": r["email"],   # internal-only display label
                    "email": r["email"],
                    "role": r["role"],
                    "organization": organization,
                    "role_title": (cred or {}).get("role_title"),
                    "primary_specialty": primary_specialty,
                    "specialty": r["specialty"],
                    "degree": ship.get("degree"),
                    "credentials_verified": bool((cred or {}).get("credentials_verified")),
                    "has_credentials": has_cred,
                    # Mock/sandbox contributor — labeled in the admin and hard-
                    # excluded from real exports (internal demo tool).
                    "is_mock": bool(r["is_mock"]),
                    "record_count": int(rec_counts.get(r["id"], 0)),
                    "submission_count": submission_count,
                    "last_labeled_at": r["last_labeled_at"],
                }
            )
        return out

    def get_contributor(self, id_hashed: str) -> Optional[Dict[str, Any]]:
        for c in self.contributor_directory():
            if c["id_hashed"] == id_hashed:
                return c
        return None

    def organization_directory(self) -> List[Dict[str, Any]]:
        """Aggregate the contributor directory by organization for the top-level
        Contributors view (list by org, click in)."""
        from asclepius.constants import UNASSIGNED_ORG
        orgs: Dict[str, Dict[str, Any]] = {}
        for c in self.contributor_directory():
            org = c["organization"] or UNASSIGNED_ORG
            agg = orgs.setdefault(
                org,
                {
                    "organization": org,
                    "contributor_count": 0,
                    "verified_count": 0,
                    "record_count": 0,
                    "submission_count": 0,
                    "last_labeled_at": None,
                },
            )
            agg["contributor_count"] += 1
            agg["verified_count"] += 1 if c["credentials_verified"] else 0
            agg["record_count"] += c["record_count"]
            agg["submission_count"] += c["submission_count"]
            ll = c.get("last_labeled_at")
            if ll and (agg["last_labeled_at"] is None or ll > agg["last_labeled_at"]):
                agg["last_labeled_at"] = ll
        return sorted(orgs.values(), key=lambda o: o["organization"].lower())

    def hashed_ids_for_organization(self, organization: str) -> List[str]:
        from asclepius.constants import UNASSIGNED_ORG
        return [
            c["id_hashed"]
            for c in self.contributor_directory()
            if (c["organization"] or UNASSIGNED_ORG) == organization and c["id_hashed"]
        ]

    # ─── Contributor record diagnostics & re-attribution (ops tooling) ────────
    def contributor_record_diagnostics(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """Explain whether a per-contributor "Export Data" will work for ``user``:
        submission counts by status, record counts by status, and — among the
        records a scoped export can ship (status export_ready | exported) — how
        many actually carry this user's hashed-annotator id (the export match
        key) vs a mismatched/blank id. A non-zero ``annotator_mismatch`` is the
        usual reason an export of a contributor with records still ships nothing."""
        uid = user["id"]
        idh = user.get("id_hashed")
        with self._conn() as conn:
            sub_by_status = {
                r["status"]: int(r["n"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM submissions WHERE evaluator_id = ? GROUP BY status",
                    (uid,),
                ).fetchall()
            }
            rec_by_status = {
                r["status"]: int(r["n"])
                for r in conn.execute(
                    "SELECT r.status, COUNT(*) AS n FROM records r "
                    "JOIN submissions s ON s.submission_id = r.submission_id "
                    "WHERE s.evaluator_id = ? GROUP BY r.status",
                    (uid,),
                ).fetchall()
            }
            shippable_rows = conn.execute(
                "SELECT r.payload_json FROM records r "
                "JOIN submissions s ON s.submission_id = r.submission_id "
                "WHERE s.evaluator_id = ? AND r.status IN ('export_ready', 'exported')",
                (uid,),
            ).fetchall()
        annotator_match = annotator_mismatch = 0
        for row in shippable_rows:
            payload = json.loads(row["payload_json"] or "{}")
            if payload.get("annotator_id_hashed") == idh:
                annotator_match += 1
            else:
                annotator_mismatch += 1
        return {
            "user_id": uid,
            "id_hashed": idh,
            "email": user.get("email"),
            "active": bool(user.get("active")),
            "submissions_by_status": sub_by_status,
            "records_by_status": rec_by_status,
            "submissions_total": sum(sub_by_status.values()),
            "records_total": sum(rec_by_status.values()),
            # What the contributor "Export Data" button would actually emit
            # (now that scoped exports include already-exported records):
            "exportable_records": annotator_match,
            "annotator_id_mismatch_records": annotator_mismatch,
        }

    def reattribute_contributor(
        self, *, source_user: Dict[str, Any], target_user: Dict[str, Any],
        deactivate_source: bool = True,
    ) -> Dict[str, Any]:
        """Move every submission, packaged record, and independent-answer commit
        from ``source_user`` to ``target_user`` and rewrite the annotator
        provenance (hashed id + credential attributes) on both the submissions and
        the shipped records, so a contributor-scoped export of the target now
        includes this work. Optionally deactivates the now-empty source account.

        Atomic (single transaction). Returns a summary of what changed."""
        source_id = source_user["id"]
        target_id = target_user["id"]
        if source_id == target_id:
            raise ValueError("source and target are the same account")
        block = self.annotator_block(target_user)
        # The exact provenance fields packaging stamps onto every record.
        prov_patch = {
            "annotator_id_hashed": block.get("id_hashed"),
            "annotator_credential": block.get("credentials"),
            "annotator_specialty": block.get("specialty"),
            "annotator_years_experience": block.get("years_experience"),
        }
        with self._conn() as conn:
            submission_ids = [
                r["submission_id"]
                for r in conn.execute(
                    "SELECT submission_id FROM submissions WHERE evaluator_id = ?", (source_id,)
                ).fetchall()
            ]
            records_rewritten = 0
            for sid in submission_ids:
                for rec in conn.execute(
                    "SELECT record_id, payload_json FROM records WHERE submission_id = ?", (sid,)
                ).fetchall():
                    payload = json.loads(rec["payload_json"] or "{}")
                    payload.update(prov_patch)
                    conn.execute(
                        "UPDATE records SET payload_json = ? WHERE record_id = ?",
                        (json.dumps(payload), rec["record_id"]),
                    )
                    records_rewritten += 1
            conn.execute(
                "UPDATE submissions SET evaluator_id = ?, annotator_json = ? WHERE evaluator_id = ?",
                (target_id, json.dumps(block), source_id),
            )
            # PK is (task_id, evaluator_id); OR IGNORE skips a commit the target
            # already has for the same task (the source row is simply left behind).
            conn.execute(
                "UPDATE OR IGNORE independent_commits SET evaluator_id = ? WHERE evaluator_id = ?",
                (target_id, source_id),
            )
            if deactivate_source:
                conn.execute("UPDATE users SET active = 0 WHERE id = ?", (source_id,))
        return {
            "source_email": source_user.get("email"),
            "target_email": target_user.get("email"),
            "submissions_moved": len(submission_ids),
            "records_rewritten": records_rewritten,
            "target_id_hashed": block.get("id_hashed"),
            "source_deactivated": bool(deactivate_source),
        }

    # ─── Inter-annotator agreement observations (opt §1.3) ───────────────────
    def upsert_agreement(
        self, *, task_id: str, specialty: Optional[str], sub_a: str, sub_b: str,
        verdict_a: Optional[str], verdict_b: Optional[str],
        tags_a: List[str], tags_b: List[str], jaccard_tags: float,
        verdict_agree: bool, n_labels: int, flagged: bool,
        blinded: Optional[bool] = None,
        kappa_excluded_reason: Optional[str] = None,
    ) -> None:
        # ``blinded`` (Buyer Response PRD §7 F1): whether the second annotator was
        # blind to the first's verdict. Only blinded observations enter κ.
        #
        # TRI-STATE, and the default is None (Audit R C2). This used to default
        # to ``True`` while its only caller never passed it, so every observation
        # was stamped blind, ``agreement._blinded_only`` was a permanent no-op,
        # and two buyer-facing artifacts asserted a property nobody measured:
        # quality_report.md's "unblinded observations excluded" was always 0, and
        # every packaged record claimed ``independent_second_label`` on the
        # strength of an observation merely existing. A default of True on a
        # column whose whole purpose is to record a MEASUREMENT is the same
        # defect ``payload_is_blinded`` was built to remove — one layer down.
        # None means "not verified", which ``aggregate_kappa`` reports separately
        # from a measured False and excludes from κ either way.
        #
        # ``kappa_excluded_reason`` (PRD 2 §4.2.4) is a SEPARATE axis from
        # ``blinded``, and that separation is the whole point: a trajectory
        # observation is genuinely blinded and still must not enter κ, because
        # blinding is about not seeing the other labeler's identity and says
        # nothing about temporal independence. The observation is still RECORDED —
        # discarding it would leave no evidence the exclusion happened — it is just
        # kept out of the pool, with the reason stored next to it so a buyer's
        # methodologist can audit the decision instead of taking our word for it.
        #
        # DERIVED HERE, not passed by the caller. "Excluded by construction" has to
        # mean by construction: the pipeline that writes this row is not the place
        # where somebody should have to remember a κ rule, and a rule that depends
        # on being remembered is a rule that will be forgotten by the second caller.
        # The parameter stays for an explicit override.
        if kappa_excluded_reason is None:
            from asclepius import trajectory as asc_trajectory
            kappa_excluded_reason = asc_trajectory.kappa_exclusion_reason(
                self.get_task(task_id) or {})
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agreement
                  (task_id, specialty, sub_a, sub_b, verdict_a, verdict_b, tags_a_json,
                   tags_b_json, jaccard_tags, verdict_agree, n_labels, flagged, blinded,
                   kappa_excluded_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, specialty, sub_a, sub_b, verdict_a, verdict_b,
                    json.dumps(tags_a or []), json.dumps(tags_b or []),
                    jaccard_tags, 1 if verdict_agree else 0, int(n_labels),
                    1 if flagged else 0,
                    None if blinded is None else (1 if blinded else 0),
                    kappa_excluded_reason or None,
                    _utcnow_iso(),
                ),
            )

    def external_adjudication_pairs(self) -> List[tuple]:
        """(partner_verdict, physician_verdict) pairs for external-adjudication
        agreement (Buyer Response PRD §7 F3). Reads the ``external_adjudication`` table
        when present; returns [] otherwise so the export degrades to a null-with-reason
        stat rather than failing. The table is populated by the adjudication surface
        as physicians answer cases that carry a sealed partner adjudication."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT partner_verdict, physician_verdict FROM external_adjudication "
                    "WHERE partner_verdict IS NOT NULL AND physician_verdict IS NOT NULL"
                ).fetchall()
        except Exception:
            return []
        return [(dict(r)["partner_verdict"], dict(r)["physician_verdict"]) for r in rows]

    def record_external_adjudication(
        self, *, ingest_case_id: str, partner_verdict: Optional[str],
        physician_verdict: Optional[str], physician_hashed: Optional[str] = None,
    ) -> None:
        """Record a physician's independent verdict against the partner's sealed
        adjudication for a case (Buyer Response PRD §7 F3)."""
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS external_adjudication (
                    ingest_case_id   TEXT PRIMARY KEY,
                    partner_verdict  TEXT,
                    physician_verdict TEXT,
                    physician_hashed TEXT,
                    created_at       TEXT NOT NULL
                )""")
            conn.execute(
                """INSERT INTO external_adjudication
                   (ingest_case_id, partner_verdict, physician_verdict, physician_hashed, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(ingest_case_id) DO UPDATE SET
                     partner_verdict=excluded.partner_verdict,
                     physician_verdict=excluded.physician_verdict,
                     physician_hashed=excluded.physician_hashed""",
                (ingest_case_id, partner_verdict, physician_verdict, physician_hashed, now))

    def list_agreement_observations(self, *, specialty: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM agreement {where} ORDER BY created_at ASC", tuple(params)
            ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["tags_a"] = json.loads(rec.pop("tags_a_json", "[]") or "[]")
            rec["tags_b"] = json.loads(rec.pop("tags_b_json", "[]") or "[]")
            out.append(rec)
        return out

    # ─── Generation jobs (Seedmaker, PRD §9.2) ───────────────────────────────
    def insert_generation_job(
        self, *, specialty: str, requested_n: int, accepted: int,
        dropped_by_reason: Dict[str, int], params: Dict[str, Any],
        created_by: Optional[str] = None,
    ) -> str:
        job_id = _new_id("genjob")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO generation_jobs
                  (job_id, specialty, requested_n, accepted, dropped_json, params_json,
                   created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, specialty, int(requested_n or 0), int(accepted or 0),
                    json.dumps(dropped_by_reason or {}), json.dumps(params or {}),
                    created_by, _utcnow_iso(),
                ),
            )
        return job_id

    @staticmethod
    def _generation_job_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["dropped"] = json.loads(rec.pop("dropped_json", "{}") or "{}")
        rec["params"] = json.loads(rec.pop("params_json", "{}") or "{}")
        return rec

    def get_generation_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._generation_job_row(row) if row else None

    def list_generation_jobs(
        self, *, specialty: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if specialty:
            clauses.append("specialty = ?")
            params.append(specialty)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM generation_jobs {where} ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._generation_job_row(r) for r in rows]

    # ─── Frontier-model baselines + failure capture (FEAT-1) ─────────────────
    def insert_baseline_run(
        self, *, task_id: str, model: str, response_text: Optional[str],
        error: Optional[str] = None, latency_ms: Optional[int] = None,
        tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
        provider: Optional[str] = None, prompt_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        rid = _new_id("bl")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO baseline_runs
                   (run_id, task_id, model, provider, prompt_hash, response_text, error,
                    latency_ms, tokens_in, tokens_out, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, task_id, model, provider, prompt_hash, response_text, error,
                 latency_ms, tokens_in, tokens_out, _utcnow_iso()),
            )
        return self.get_baseline_run(rid)  # type: ignore[return-value]

    def get_baseline_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM baseline_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_baseline_runs(
        self, *, task_id: Optional[str] = None, model: Optional[str] = None, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if task_id:
            clauses.append("task_id = ?"); params.append(task_id)
        if model:
            clauses.append("model = ?"); params.append(model)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM baseline_runs {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_model_failure(
        self, *, task_id: str, submission_id: str, model: str, verdict: Optional[str],
        error_tags: List[str], corrected_steps: List[Dict[str, Any]],
        expert_correction: Optional[str], prompt: Optional[str], provider: Optional[str] = None,
    ) -> str:
        fid = _new_id("mf")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO model_failures
                   (failure_id, task_id, submission_id, model, provider, verdict, error_tags_json,
                    corrected_steps_json, expert_correction, prompt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fid, task_id, submission_id, model, provider, verdict, json.dumps(error_tags or []),
                 json.dumps(corrected_steps or []), expert_correction, prompt, _utcnow_iso()),
            )
        return fid

    @staticmethod
    def _model_failure_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["error_tags"] = json.loads(rec.pop("error_tags_json", "[]") or "[]")
        rec["corrected_steps"] = json.loads(rec.pop("corrected_steps_json", "[]") or "[]")
        return rec

    def list_model_failures(
        self, *, model: Optional[str] = None, error_tag: Optional[str] = None, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if model:
            clauses.append("model = ?"); params.append(model)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM model_failures {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
            ).fetchall()
        out = [self._model_failure_row(r) for r in rows]
        if error_tag:
            out = [f for f in out if error_tag in (f.get("error_tags") or [])]
        return out

    def model_failure_summary(self) -> List[Dict[str, Any]]:
        """Per-model failure counts + the error-tag mix — the datasheet/admin
        headline ("GPT-5.5 failed N cases; top tags …"). Includes ``provider``."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT model, MAX(provider) AS provider, COUNT(*) AS n "
                "FROM model_failures GROUP BY model ORDER BY n DESC"
            ).fetchall()
        out = []
        for r in rows:
            tags: Dict[str, int] = {}
            for f in self.list_model_failures(model=r["model"]):
                for t in f.get("error_tags") or []:
                    tags[t] = tags.get(t, 0) + 1
            out.append({"model": r["model"], "provider": r["provider"],
                        "failures": int(r["n"]), "error_tags": tags})
        return out

    def ab_slot_balance(self) -> Dict[str, Any]:
        """Durable QC metric (A3): over all two-frontier A/B pairs actually built (a
        candidate with source='baseline' + a provider), how often is OpenAI in slot A?
        Must converge to ~0.5. Computed from stored candidates so it survives restart."""
        n_pairs = 0
        openai_in_A = 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT candidate_answers_json FROM tasks WHERE candidate_answers_json LIKE '%\"baseline\"%'"
            ).fetchall()
        for row in rows:
            try:
                cands = json.loads(row["candidate_answers_json"] or "[]")
            except (ValueError, TypeError):
                continue
            by_id = {c.get("id"): c for c in cands if isinstance(c, dict)}
            a, b = by_id.get("A"), by_id.get("B")
            if not a or not b:
                continue
            provs = {a.get("provider"), b.get("provider")}
            if provs != {"openai", "anthropic"}:
                continue
            n_pairs += 1
            if a.get("provider") == "openai":
                openai_in_A += 1
        rate = round(openai_in_A / n_pairs, 3) if n_pairs else None
        return {"pairs": n_pairs, "openai_in_A": openai_in_A, "openai_as_A_rate": rate}

    def ab_fallback_rate(self, *, window: int = 50) -> Optional[float]:
        """Rolling ``legacy_fallback / (two_frontier + legacy_fallback)`` over the last
        ``window`` assembled A/B pairs (PRD §A3 Rung 3). Reads the durable ``ab_source``
        stamped onto each task's generation block, newest first, so it survives restart
        and is worker-independent. ``anthropic_only_v4`` (the intended V4 path) is NOT a
        fallback and is excluded. Returns ``None`` when there is no pairing history yet
        (so a cold start never trips the guard)."""
        counted = ("two_frontier", "legacy_fallback")
        want = max(1, int(window))
        total = 0
        fallback = 0
        with self._conn() as conn:
            # Fetch generously (window may be diluted by interleaved anthropic_only_v4 /
            # needs_baseline rows) and take the newest ``want`` COUNTED pairs in Python.
            rows = conn.execute(
                "SELECT generation_json FROM tasks "
                "WHERE generation_json LIKE '%\"ab_source\"%' "
                "ORDER BY created_at DESC LIMIT ?",
                (want * 5,),
            ).fetchall()
        for row in rows:
            try:
                gen = json.loads(row["generation_json"] or "null") or {}
            except (ValueError, TypeError):
                continue
            src = gen.get("ab_source")
            if src not in counted:
                continue
            total += 1
            if src == "legacy_fallback":
                fallback += 1
            if total >= want:
                break
        if not total:
            return None
        return round(fallback / total, 3)

    def flaw_catch_rate(self) -> Dict[str, Any]:
        """How often evaluators reject the intended-flawed candidate on generated
        tasks (PRD §16). Only counts graded A/B submissions where the task carried
        an ``intended_flawed_id`` (caught_flaw IS NOT NULL)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS scored, SUM(caught_flaw) AS caught
                FROM submissions WHERE caught_flaw IS NOT NULL
                """
            ).fetchone()
        scored = int(row["scored"] or 0)
        caught = int(row["caught"] or 0)
        rate = round(caught / scored, 3) if scored else None
        return {"scored": scored, "caught": caught, "rate": rate}

    # ─── V5 Clinical RL Environments (env_runs, PRD §10) ─────────────────────
    def insert_env_run(
        self, *, task_id: str, specialty: str, task_type: str,
        case_id: Optional[str] = None, case_source: str = "gold",
        provider: Optional[str] = None, model: Optional[str] = None,
        ab_source: Optional[str] = None, mode: str = "generated",
        compiled: Optional[Dict[str, Any]] = None,
        trajectory: Optional[List[Dict[str, Any]]] = None,
        verification: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, Any]] = None,
        physician_annotation: Optional[Dict[str, Any]] = None,
        annotations: Optional[List[Dict[str, Any]]] = None,
        empirical_difficulty: Optional[float] = None,
        difficulty_measured: bool = False,
        passes_difficulty_gate: Optional[bool] = None,
    ) -> Dict[str, Any]:
        rid = _new_id("env")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO env_runs
                   (run_id, task_id, specialty, task_type, case_id, case_source, provider,
                    model, ab_source, mode, compiled_json, trajectory_json, verification_json,
                    provenance_json, physician_annotation_json, annotations_json, empirical_difficulty,
                    difficulty_measured, passes_difficulty_gate, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, task_id, specialty, task_type, case_id, case_source, provider, model,
                 ab_source, mode,
                 json.dumps(compiled) if compiled is not None else None,
                 json.dumps(trajectory) if trajectory is not None else None,
                 json.dumps(verification) if verification is not None else None,
                 json.dumps(provenance) if provenance is not None else None,
                 json.dumps(physician_annotation) if physician_annotation is not None else None,
                 json.dumps(annotations) if annotations is not None else None,
                 empirical_difficulty, 1 if difficulty_measured else 0,
                 (None if passes_difficulty_gate is None else (1 if passes_difficulty_gate else 0)),
                 now, now),
            )
        return self.get_env_run(rid)  # type: ignore[return-value]

    @staticmethod
    def _env_run_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        for col in ("compiled", "trajectory", "verification", "provenance",
                    "physician_annotation", "annotations"):
            raw = rec.pop(f"{col}_json", None)
            rec[col] = json.loads(raw) if raw else None
        rec["difficulty_measured"] = bool(rec.get("difficulty_measured"))
        pg = rec.get("passes_difficulty_gate")
        rec["passes_difficulty_gate"] = None if pg is None else bool(pg)
        return rec

    def get_env_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM env_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._env_run_row(row) if row else None

    def get_environment(self, task_id: str) -> Optional[Dict[str, Any]]:
        """The compiled-environment row (mode='generated') for a task_id."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM env_runs WHERE task_id = ? AND mode = 'generated' "
                "ORDER BY created_at ASC LIMIT 1", (task_id,)
            ).fetchone()
        return self._env_run_row(row) if row else None

    def list_env_runs(
        self, *, task_id: Optional[str] = None, specialty: Optional[str] = None,
        mode: Optional[str] = None, case_source: Optional[str] = None,
        has_annotation: Optional[bool] = None, limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if task_id:
            clauses.append("task_id = ?"); params.append(task_id)
        if specialty:
            clauses.append("specialty = ?"); params.append(specialty)
        if mode:
            clauses.append("mode = ?"); params.append(mode)
        if case_source:
            clauses.append("case_source = ?"); params.append(case_source)
        if has_annotation is True:
            clauses.append("physician_annotation_json IS NOT NULL")
        elif has_annotation is False:
            clauses.append("physician_annotation_json IS NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM env_runs {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
            ).fetchall()
        return [self._env_run_row(r) for r in rows]

    def update_env_run(self, run_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get_env_run(run_id)
        json_cols = {"compiled", "trajectory", "verification", "provenance",
                     "physician_annotation", "annotations"}
        sets, params = [], []
        for key, val in fields.items():
            if key in json_cols:
                sets.append(f"{key}_json = ?")
                params.append(json.dumps(val) if val is not None else None)
            elif key == "difficulty_measured":
                sets.append("difficulty_measured = ?"); params.append(1 if val else 0)
            elif key == "passes_difficulty_gate":
                sets.append("passes_difficulty_gate = ?")
                params.append(None if val is None else (1 if val else 0))
            else:
                sets.append(f"{key} = ?"); params.append(val)
        sets.append("updated_at = ?"); params.append(_utcnow_iso())
        params.append(run_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE env_runs SET {', '.join(sets)} WHERE run_id = ?", tuple(params))
        return self.get_env_run(run_id)

    def env_annotation_records(self, *, specialty: Optional[str] = None) -> List[Dict[str, Any]]:
        """Rollout rows that carry a physician annotation — the PRM training set
        (PRD §7.5). Each dict has ``trajectory`` + ``physician_annotation``."""
        return self.list_env_runs(specialty=specialty, mode="rollout", has_annotation=True)

    # ═══ PRD-B IDENTITY METHODS — owned by Agent 2, do not edit from other PRDs ═══
    # NPIs are normalized on WRITE, but rows created before that fix can hold
    # "1234-567893". Comparing the raw column would leave those legacy rows
    # invisible to duplicate detection and to the cache — the exact defect the
    # write-side fix closed, still open for everyone already in the database.
    # Normalizing in SQL fixes them without rewriting stored data. Mirrors
    # ``credentialing.clean_npi`` (strips ``[\s\-\.]``).
    _NPI_NORM = ("REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(npi, '-', ''), '.', ''), "
                 "' ', ''), CHAR(9), ''), CHAR(10), '')")

    def set_npi_result(self, user_id: str, result: Dict[str, Any]) -> None:
        """Persist an NPI check outcome onto the user row.

        Three states, never collapsed:
          verified              -> npi_verified = 1
          mismatch / not_found  -> npi_verified = 0   (definitive negative)
          unavailable / other   -> npi_verified = NULL (could NOT check)

        ``npi_checked_at`` is stamped ONLY on definitive outcomes, so an
        UNAVAILABLE attempt never satisfies the 30-day NPI cache and never
        suppresses a retry.
        """
        outcome = (result or {}).get("result")
        if outcome == "verified":
            flag: Optional[int] = 1
        elif outcome in ("mismatch", "not_found"):
            flag = 0
        else:
            flag = None

        if flag is None:
            # F6: a failed check is an EVENT, not a result. Writing it through
            # would set npi_verified back to NULL, replace the NPPES record
            # with {"result":"unavailable","record":null}, and clear
            # npi_checked_at — erasing evidence we already have, dropping the
            # user's score by 25, making 'reviewer' unproposable, and evicting
            # the 30-day cache entry FOR EVERY user of that NPI. That happens
            # on the admin's Recheck click while NPPES is rate-limiting, i.e.
            # at exactly the moment the button gets used, and on any
            # re-onboard (provision_user is an idempotent upsert).
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET npi_last_attempt_json = ?, "
                    "npi_last_attempt_at = ? WHERE id = ?",
                    (json.dumps(result or {}), _utcnow_iso(), user_id),
                )
            return

        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET npi_verified = ?, npi_payload_json = ?, "
                "npi_checked_at = ?, npi_last_attempt_json = NULL, "
                "npi_last_attempt_at = NULL WHERE id = ?",
                (flag, json.dumps(result or {}), _utcnow_iso(), user_id),
            )

    def update_own_profile(
        self, user_id: str, *, full_name: Optional[str] = None,
        phone: Optional[str] = None, linkedin_url: Optional[str] = None,
        specialty_niche: Optional[str] = None,
    ) -> None:
        """The fields a physician may correct about themselves.

        Deliberately short. Everything a credential decision rests on -- the
        registration number, the country, the degree, the board certification,
        the verification status and the tier -- is NOT here: those were checked
        against a registry and attested to, and a surface that let someone edit
        them after approval would make the check meaningless. Correcting one is
        a conversation with an admin, which is the point.

        ``None`` means "not submitted" and leaves the column alone; an empty
        string means "clear this", which is a thing someone may legitimately
        want to do with a LinkedIn URL.
        """
        sets: List[str] = []
        params: List[Any] = []
        for column, value in (
            ("full_name", full_name),
            ("phone", phone),
            ("linkedin_url", linkedin_url),
            ("specialty_niche", specialty_niche),
        ):
            if value is None:
                continue
            cleaned = value.strip()
            sets.append(f"{column} = ?")
            params.append(cleaned or None)
        if not sets:
            return
        params.append(user_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(params))

    def set_user_avatar(
        self, user_id: str, *, sha256: Optional[str], mime: Optional[str], at: str,
    ) -> None:
        """Point a physician's profile at an already-stored image, or clear it.

        Deliberately NOT part of ``update_own_profile``: that one takes free
        text from a PATCH body, and a sha is not free text. The bytes are
        written to the asset store and hashed server-side first, and this is
        called with the hash that came back -- the same rule ``cv_asset_sha``
        follows, and for the same reason: a client-settable sha is an
        unvalidated pointer into a store that also holds de-identified clinical
        images.

        ``sha256=None`` clears all three columns, which is what "remove photo"
        does. The blob itself is left in the content-addressed store: it is
        shared by hash, so deleting it here could pull an identical image out
        from under somebody else.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET avatar_asset_sha = ?, avatar_mime = ?, "
                "avatar_updated_at = ? WHERE id = ?",
                (sha256 or None, (mime or None) if sha256 else None,
                 at if sha256 else None, user_id),
            )

    def set_registry_country(
        self, user_id: str, *, practice: Optional[str], licensure: Optional[str],
        registry_id: Optional[str],
    ) -> None:
        """Where this doctor practises, where they are licensed, and the number
        that licence carries. Written once at signup; NULL keeps meaning "US"
        for every row that predates the question."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET country_of_practice = ?, country_of_licensure = ?, "
                "registry_id = ? WHERE id = ?",
                ((practice or None), (licensure or None), (registry_id or None), user_id),
            )

    def set_registry_result(self, user_id: str, result: Dict[str, Any]) -> None:
        """Persist a national-registry check, on the same three-state rule as
        ``set_npi_result``.

          verified              -> registry_verified = 1
          mismatch / not_found  -> registry_verified = 0   (definitive)
          anything else         -> registry_verified = NULL, recorded as an
                                   ATTEMPT so it cannot erase evidence

        The non-definitive set is wider here than it is for NPPES: it holds
        ``inconclusive`` (searched a register that admits it is incomplete) and
        ``document_only`` (that country has no register to search). Neither is
        a finding about the doctor, and writing either one through would clear
        a real verification on the next retry.
        """
        outcome = (result or {}).get("result")
        if outcome == "verified":
            flag: Optional[int] = 1
        elif outcome in ("mismatch", "not_found"):
            flag = 0
        else:
            flag = None

        if flag is None:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET registry_last_attempt_json = ?, "
                    "registry_last_attempt_at = ? WHERE id = ?",
                    (json.dumps(result or {}), _utcnow_iso(), user_id),
                )
                # ...but a doctor whose country has no register at all is not
                # waiting on a retry, and the queue has to be able to say so.
                if outcome in ("document_only", "queued"):
                    conn.execute(
                        "UPDATE users SET registry_payload_json = ? WHERE id = ?",
                        (json.dumps(result or {}), user_id),
                    )
            return

        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET registry_verified = ?, registry_payload_json = ?, "
                "registry_checked_at = ?, registry_last_attempt_json = NULL, "
                "registry_last_attempt_at = NULL WHERE id = ?",
                (flag, json.dumps(result or {}), _utcnow_iso(), user_id),
            )

    def set_signup_flags(self, user_id: str, findings: List[Dict[str, str]]) -> None:
        """Record what does not hold together about a signup.

        A flag routes to a human; it is not a rejection and nothing downstream
        may treat it as one. Always written, including the empty case, so
        "assessed and clean" is distinguishable from "never assessed".
        """
        from asclepius import plausibility

        flagged = 1 if plausibility.should_flag(findings or []) else 0
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET flagged = ?, flags_json = ? WHERE id = ?",
                (flagged, json.dumps(findings or []), user_id),
            )

    def find_users_by_registry_id(
        self, registry_id: str, country: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Other accounts claiming this registration number.

        Scoped per country: a PMDC number and an NMC number that happen to
        share digits are not the same credential, and treating them as a
        duplicate would accuse two unrelated doctors.
        """
        value = (registry_id or "").strip()
        if not value:
            return []
        sql = ("SELECT * FROM users WHERE registry_id IS NOT NULL "
               "AND UPPER(TRIM(registry_id)) = UPPER(?)")
        params: List[Any] = [value]
        if country:
            sql += " AND UPPER(COALESCE(country_of_licensure, '')) = UPPER(?)"
            params.append(country)
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def get_cached_npi_fetch(self, npi: str, max_age_days: int = 30) -> Optional[Dict[str, Any]]:
        """A fresh definitive NPPES answer for this NPI, if any user row holds
        one — shaped like ``credentialing.fetch_npi_record`` output so the
        caller can recompute the name match for the CURRENT signup (a cache
        keyed by NPI alone must never cache the verdict).

        Returns None when there is no definitive check within the window;
        UNAVAILABLE attempts never populate the cache (``npi_checked_at`` stays
        NULL for them).
        """
        npi = (npi or "").strip()
        if not npi:
            return None
        cutoff = (datetime.utcnow() - timedelta(days=max(0, max_age_days))).replace(
            microsecond=0).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT npi_payload_json FROM users "
                f"WHERE {self._NPI_NORM} = ? AND npi_checked_at IS NOT NULL "
                "AND npi_checked_at >= ? "
                "AND npi_payload_json IS NOT NULL "
                "ORDER BY npi_checked_at DESC LIMIT 1",
                (npi, cutoff),
            ).fetchone()
        if not row:
            return None
        try:
            stored = json.loads(row["npi_payload_json"] or "{}")
        except (TypeError, ValueError):
            return None
        outcome = stored.get("result")
        if outcome == "not_found":
            return {"status": "not_found", "record": None, "reason": "cached"}
        if outcome in ("verified", "mismatch") and stored.get("record"):
            return {"status": "found", "record": stored["record"], "reason": "cached"}
        return None

    def set_verification_status(
        self, user_id: str, status: Optional[str], *, notes: Optional[str] = None
    ) -> None:
        """Set the human-review lifecycle state (pending | approved | rejected).
        Notes are only overwritten when provided."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET verification_status = ?, "
                "verification_notes = COALESCE(?, verification_notes) WHERE id = ?",
                (status, notes, user_id),
            )

    def update_identity_capture(
        self,
        user_id: str,
        *,
        phone: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        email_domain_class: Optional[str] = None,
    ) -> None:
        """Signup-time identity fields (PRD-B Phase 4). Only overwrites a field
        when a value is provided, so a sparse re-onboard never wipes data."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET phone = COALESCE(?, phone), "
                "linkedin_url = COALESCE(?, linkedin_url), "
                "email_domain_class = COALESCE(?, email_domain_class) WHERE id = ?",
                (phone, linkedin_url, email_domain_class, user_id),
            )

    def set_cv(
        self, user_id: str, asset_sha: Optional[str],
        parsed: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Attach an uploaded CV (content-addressed sha) and its best-effort
        parse to the user row. The parse is advisory dossier data only."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET cv_asset_sha = ?, cv_parsed_json = ? WHERE id = ?",
                (asset_sha, json.dumps(parsed) if parsed is not None else None, user_id),
            )

    def find_users_by_npi(self, npi: str) -> List[Dict[str, Any]]:
        """All user rows claiming this NPI — duplicate detection for the tier
        scorer's blocker list."""
        npi = (npi or "").strip()
        if not npi:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM users WHERE {self._NPI_NORM} = ? ORDER BY created_at ASC",
                (npi,),
            ).fetchall()
        return [dict(r) for r in rows]

    def users_pending_npi_recheck(
        self, *, older_than_minutes: int = 60, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Rows whose NPI check never reached a definitive answer — the retry
        list PRD §1.2 asked for ("UNAVAILABLE routes to manual review AND
        schedules a retry").

        Deliberately a list an admin can bulk-run rather than a scheduler: no
        job framework, no background thread, and the work is visible and
        auditable. A row qualifies when it claims an NPI, has no definitive
        result (``npi_checked_at IS NULL``), and either has never been
        attempted or was last attempted longer than ``older_than_minutes`` ago
        — so a sweep cannot hot-loop against a rate-limiting registry.
        """
        cutoff = (datetime.utcnow() - timedelta(minutes=max(0, older_than_minutes))).replace(
            microsecond=0).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users "
                "WHERE npi IS NOT NULL AND TRIM(npi) != '' "
                "  AND npi_checked_at IS NULL "
                "  AND (npi_last_attempt_at IS NULL OR npi_last_attempt_at <= ?) "
                "ORDER BY COALESCE(npi_last_attempt_at, created_at) ASC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def npi_claim_counts(self) -> Dict[str, int]:
        """{normalized npi: number of accounts claiming it} — one grouped query
        so the queue does not run a full-table scan per row (B-5.8). Keyed by
        the NORMALIZED value so callers can look up with ``clean_npi(...)`` and
        so a legacy "1234-567893" row groups with its clean twin. Only NPIs
        with more than one claimant are returned; everything else is 1 by
        absence."""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {self._NPI_NORM} AS norm_npi, COUNT(*) AS n FROM users "
                "WHERE npi IS NOT NULL AND TRIM(npi) != '' "
                "GROUP BY norm_npi HAVING n > 1"
            ).fetchall()
        return {r["norm_npi"]: r["n"] for r in rows}

    def list_verification_queue(self, status: str = "pending") -> List[Dict[str, Any]]:
        """User rows in one verification state, newest signup first (the admin
        works the top of the queue). ``status`` ∈ pending|approved|rejected."""
        with self._conn() as conn:
            rows = conn.execute(
                # rowid tiebreak: created_at has second granularity, and two
                # launch-day signups in the same second must still order
                # deterministically (newest insertion first).
                "SELECT * FROM users WHERE verification_status = ? "
                "ORDER BY created_at DESC, rowid DESC",
                (status,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_verification_decision(
        self,
        user_id: str,
        *,
        status: str,
        decided_by: str,
        tier: Optional[str] = None,
        tier_score: Optional[float] = None,
        note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """The human decision (PRD-B Phase 5). Stamps verified_by/verified_at on
        EVERY decision; tier fields are written only on approval — the tier is
        a decision, not a computation, so it arrives only from this method."""
        now = _utcnow_iso()
        with self._conn() as conn:
            if status == "approved":
                conn.execute(
                    "UPDATE users SET verification_status = 'approved', "
                    "verification_notes = COALESCE(?, verification_notes), "
                    "verified_by = ?, verified_at = ?, tier = ?, tier_score = ?, "
                    "tier_assigned_at = ?, tier_assigned_by = ? WHERE id = ?",
                    (note, decided_by, now, tier, tier_score, now, decided_by, user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET verification_status = ?, "
                    "verification_notes = COALESCE(?, verification_notes), "
                    "verified_by = ?, verified_at = ? WHERE id = ?",
                    (status, note, decided_by, now, user_id),
                )
        return self.get_user_by_id(user_id)

    def mark_community_welcomed(self, user_id: str) -> bool:
        """Community v2: idempotency flag for the one-time community welcome
        (repurposes the reserved ``slack_joined`` column — the community IS
        our Slack). Set BEFORE posting: the safe failure is a missed welcome,
        never a double-post. The guarded UPDATE is the arbiter — under
        multi-worker deploys a concurrent queue-approval + credential-PUT for
        the same user must not both win. Returns True when THIS call claimed
        the welcome."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET slack_joined = 1, slack_checked_at = ? "
                "WHERE id = ? AND COALESCE(slack_joined, 0) = 0",
                (_utcnow_iso(), user_id),
            )
            return bool(cur.rowcount)
    # ═══ END PRD-B ═══
    # ═══ PRD-A REVIEW STORE METHODS — owned by Agent 1, do not edit from other PRDs ═══
    # Two-tier review product (PRD A): reviewer queue, case_reviews CRUD, and
    # double-label routing. Appended per START_HERE §3.2 — existing methods above
    # are never modified.

    @staticmethod
    def _case_review_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["dimensions"] = json.loads(rec.pop("dimension_json", "{}") or "{}")
        rec["corrections"] = json.loads(rec.pop("corrections_json", "null") or "null")
        raw_flags = rec.pop("identifier_flags", None)
        # None (never scanned) stays distinct from [] (scanned clean).
        rec["identifier_flags"] = json.loads(raw_flags) if raw_flags else (
            [] if raw_flags == "[]" else None)
        return rec

    def insert_case_review(
        self,
        *,
        task_id: str,
        submission_id: str,
        reviewer_user_id: str,
        reviewer_id_hashed: str,
        verdict: str,
        dimensions: Optional[Dict[str, str]] = None,
        corrections: Optional[Dict[str, Any]] = None,
        reviewer_notes: Optional[str] = None,
        time_spent_sec: Optional[int] = None,
        blinded: Optional[bool] = True,
        identifier_flags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """One senior-reviewer verdict on a labeler submission (PRD A §2).

        ``blinded`` is tri-state on purpose (START_HERE §5 rule 4): 1 = the payload
        served to the reviewer verifiably carried no labeler identity, 0 = identity
        was (or may have been) visible, NULL = not asserted either way."""
        review_id = _new_id("rev")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO case_reviews
                  (review_id, task_id, submission_id, reviewer_user_id, reviewer_id_hashed,
                   verdict, dimension_json, corrections_json, reviewer_notes,
                   time_spent_sec, blinded, identifier_flags, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    task_id,
                    submission_id,
                    reviewer_user_id,
                    reviewer_id_hashed,
                    verdict,
                    json.dumps(dimensions or {}),
                    json.dumps(corrections) if corrections is not None else None,
                    reviewer_notes,
                    int(time_spent_sec) if time_spent_sec is not None else None,
                    None if blinded is None else (1 if blinded else 0),
                    None if identifier_flags is None else json.dumps(sorted(identifier_flags)),
                    _utcnow_iso(),
                ),
            )
        return self.get_case_review(review_id)  # type: ignore[return-value]

    def get_case_review(self, review_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM case_reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
        return self._case_review_row(row) if row else None

    def reviews_for_submission(self, submission_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM case_reviews WHERE submission_id = ? ORDER BY created_at ASC",
                (submission_id,),
            ).fetchall()
        return [self._case_review_row(r) for r in rows]

    def reviews_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM case_reviews WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        return [self._case_review_row(r) for r in rows]

    def has_review_by(self, submission_id: str, reviewer_user_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM case_reviews WHERE submission_id = ? AND reviewer_user_id = ? LIMIT 1",
                (submission_id, reviewer_user_id),
            ).fetchone()
        return row is not None

    def next_review_for(
        self,
        user_id: str,
        *,
        specialty: Optional[str] = None,
        lease_minutes: int = 45,
        predicate: Any = None,
        scan_limit: int = 200,
        persist_routing_decision: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Oldest reviewable submission for this reviewer (PRD A §1.4).

        Self-review is impossible BY QUERY (``s.evaluator_id != ?``), not by caller
        discipline. Serves submissions never routed (``review_status IS NULL``) plus
        stale ``in_review`` claims past the lease, so an abandoned draw re-queues
        instead of vanishing forever. Excludes submissions this reviewer already
        reviewed and tasks held for blocking ingest review (Audit PRD §21.6 —
        a reviewer must not see a case whose image may carry burned-in PHI).

        ``predicate(task, submission) -> bool`` filters candidates in Python (the
        rate-based ``needs_review`` policy lives in ``asclepius.review``, not in SQL).
        """
        cutoff = _iso_minus_seconds(max(1, int(lease_minutes)) * 60)
        clauses = [
            "s.evaluator_id != ?",
            "s.verdict IS NOT NULL",
            # NULL = never routed; an 'in_review' row past its lease re-queues.
            # The lease clock is review_claimed_at, NOT updated_at — any unrelated
            # write (a pipeline re-value, a status change) bumps updated_at and
            # used to silently extend a reviewer's claim (FIX A A-3.7).
            # 'reviewed', 'orphaned' and 'not_routed' are all terminal here.
            "(s.review_status IS NULL OR (s.review_status = 'in_review'"
            " AND (s.review_claimed_at IS NULL OR s.review_claimed_at < ?)))",
            "NOT EXISTS (SELECT 1 FROM case_reviews cr WHERE cr.submission_id = s.submission_id"
            " AND cr.reviewer_user_id = ?)",
            "NOT EXISTS (SELECT 1 FROM ingest_cases ic WHERE ic.task_id = s.task_id"
            " AND ic.status = 'needs_review')",
            # PRD R §1, defect 1: this queue gated on a SINGLE submission, so a
            # reviewer drew one labeler's work and never saw the second — which
            # both double-serves the case and destroys the comparison. A task
            # that is or will be a PAIR belongs to next_review_pair_for; only a
            # task that will never carry a second label is served here.
            "(SELECT COUNT(*) FROM submissions sc WHERE sc.task_id = s.task_id"
            " AND sc.verdict IS NOT NULL) = 1",
            "COALESCE(t.max_labels, 1) < 2",
        ]
        params: List[Any] = [user_id, cutoff, user_id]
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        params.append(int(scan_limit))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT s.* FROM submissions s
                JOIN tasks t ON t.task_id = s.task_id
                WHERE {' AND '.join(clauses)}
                ORDER BY s.created_at ASC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        declined: List[str] = []
        orphaned: List[str] = []
        chosen: Optional[Dict[str, Any]] = None
        for r in rows:
            sub = self._submission_row(r)
            if predicate is not None:
                task = self.get_task(sub["task_id"])
                if task is None:
                    # A missing task is an ORPHAN, not a routing decline. Two
                    # different terminal states because they mean two different
                    # things: 'not_routed' is a decision the policy made, and
                    # 'orphaned' is data damage someone has to look at.
                    orphaned.append(sub["submission_id"])
                    continue
                if not predicate(task, sub):
                    declined.append(sub["submission_id"])
                    continue
            chosen = sub
            break
        # Persist the routing decision (FIX A A-3.3). Without this, a submission
        # the policy declines stays review_status IS NULL forever and re-occupies
        # a slot in this LIMITed window on every single draw. Once `scan_limit`
        # declined rows accumulate at the head, the portal reports "no submissions
        # awaiting review" permanently while real work sits behind them. Masked at
        # launch only because ASCLEPIUS_REVIEW_RATE defaults to 1.0.
        if persist_routing_decision and (declined or orphaned):
            with self._conn() as conn:
                if declined:
                    conn.executemany(
                        "UPDATE submissions SET review_status = 'not_routed' "
                        "WHERE submission_id = ? AND review_status IS NULL",
                        [(sid,) for sid in declined],
                    )
                if orphaned:
                    conn.executemany(
                        "UPDATE submissions SET review_status = 'orphaned' "
                        "WHERE submission_id = ? AND review_status IS NULL",
                        [(sid,) for sid in orphaned],
                    )
        return chosen

    def requeue_not_routed(self) -> int:
        """Clear every ``not_routed`` decision back to NULL (undecided).

        The escape hatch for raising ``ASCLEPIUS_REVIEW_RATE`` after launch:
        routing decisions are persisted, so without this a submission declined
        under a 0.5 rate would stay declined forever under a 1.0 rate. Returns
        the number of submissions returned to the queue."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE submissions SET review_status = NULL WHERE review_status = 'not_routed'")
            return int(cur.rowcount)

    def agreement_observation_count(self, *, specialty: Optional[str] = None) -> int:
        """COUNT(*) of stored agreement observations. Exists because the routing
        sweep only ever needed the count and used to materialize every row to
        call len() on it, once per candidate (FIX A A-3.4)."""
        sql = "SELECT COUNT(*) FROM agreement"
        params: tuple = ()
        if specialty:
            sql += " WHERE specialty = ?"
            params = (specialty,)
        with self._conn() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def claim_submission_for_review(
        self, submission_id: str, *, reviewer_id: str, blinded: Optional[bool] = None,
        lease_minutes: int = 45,
    ) -> bool:
        """Atomically claim a submission for review (``review_status='in_review'``).

        Compare-and-set: the UPDATE only wins when the row is still unclaimed or its
        prior claim's lease has expired, so two reviewers drawing concurrently cannot
        both claim the same submission. Returns True when this caller won the claim.

        The claim also records WHO holds it and the blinding DERIVED from the
        payload served to them (FIX A F2). ``review_claimed_at`` is the lease
        clock rather than ``updated_at``, which any unrelated write bumps."""
        now = _utcnow_iso()
        cutoff = _iso_minus_seconds(max(1, int(lease_minutes)) * 60)
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE submissions
                   SET review_status = 'in_review', review_claimed_by = ?,
                       review_claimed_at = ?, review_blinded = ?, updated_at = ?
                WHERE submission_id = ?
                  AND (review_status IS NULL
                       OR (review_status = 'in_review'
                           AND (review_claimed_at IS NULL OR review_claimed_at < ?)))
                """,
                (reviewer_id, now, None if blinded is None else (1 if blinded else 0),
                 now, submission_id, cutoff),
            )
            return cur.rowcount > 0

    def review_claim(self, submission_id: str, *, lease_minutes: int = 45) -> Dict[str, Any]:
        """The current claim on a submission: ``{holder, claimed_at, blinded,
        expired, status}``. ``blinded`` is tri-state (True/False/None) — None
        means no draw ever asserted it, which is NOT the same as unblinded."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT review_status, review_claimed_by, review_claimed_at, review_blinded "
                "FROM submissions WHERE submission_id = ?", (submission_id,)
            ).fetchone()
        if row is None:
            return {"holder": None, "claimed_at": None, "blinded": None,
                    "expired": True, "status": None}
        rec = dict(row)
        claimed_at = rec.get("review_claimed_at")
        cutoff = _iso_minus_seconds(max(1, int(lease_minutes)) * 60)
        blinded = rec.get("review_blinded")
        return {
            "holder": rec.get("review_claimed_by"),
            "claimed_at": claimed_at,
            "blinded": None if blinded is None else bool(blinded),
            "expired": (claimed_at is None) or (claimed_at < cutoff),
            "status": rec.get("review_status"),
        }

    def review_queue_stats(self) -> Dict[str, Any]:
        """Counts for the review portal header, in ONE pass over an indexed
        column (four separate COUNT(*) full scans used to fire on every draw —
        FIX A A-3.5).

        ``unreviewed`` counts only genuinely undecided rows (review_status NULL).
        Declined ('not_routed') and orphaned rows are reported separately rather
        than folded in: a header claiming work exists that the draw cannot serve
        is how A-3.3 stayed invisible."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT review_status AS st, COUNT(*) AS n FROM submissions "
                "WHERE verdict IS NOT NULL GROUP BY review_status"
            ).fetchall()
            n_reviews = conn.execute("SELECT COUNT(*) FROM case_reviews").fetchone()[0]
        counts = {(dict(r)["st"] or "__null__"): int(dict(r)["n"]) for r in rows}
        return {
            "unreviewed": counts.get("__null__", 0),
            "in_review": counts.get("in_review", 0),
            "reviewed": counts.get("reviewed", 0),
            "not_routed": counts.get("not_routed", 0),
            "orphaned": counts.get("orphaned", 0),
            "n_reviews": int(n_reviews),
        }

    def flag_task_for_double_label(self, task_id: str) -> bool:
        """Route a task to a second INDEPENDENT labeler (PRD A §1.3) by lifting its
        label capacity to 2 AND reopening it.

        The reopen is the load-bearing half. A ``max_labels=1`` task is ALREADY
        ``'done'`` by the time a first label exists — ``refresh_task_status``
        closes it on the first submission, on the hot submit path. So routing a
        second independent label is always a REOPEN, never a flag on an open
        task; lifting ``max_labels`` without restoring ``status='open'`` leaves a
        task that neither ``next_double_label_for`` nor the ordinary labeler
        queue can ever serve, which is what made the whole κ deliverable dead
        code in the first round.

        Terminal statuses are NOT reopened: ``prompt_flagged`` / ``not_hard`` /
        ``case_incoherent`` mean a clinician rejected the prompt itself, and
        dragging one back into the labeler queue for a second opinion would
        re-serve work a physician already ruled out. Idempotent."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET max_labels = 2, status = 'open' "
                "WHERE task_id = ? AND status IN ('open', 'done') AND max_labels < 2",
                (task_id,),
            )
            return cur.rowcount > 0

    def flag_tasks_for_double_label(
        self, decisions: List[Dict[str, Any]]
    ) -> List[str]:
        """Batch form of :meth:`flag_task_for_double_label` (FIX A A-3.4).

        One connection, one UPDATE per task and one event INSERT per flag,
        instead of two fresh connections (each paying two PRAGMAs) per candidate.
        This runs on the same single SQLite writer that labeler submissions need,
        so its statement count must not scale with the sweep window.

        ``decisions`` is ``[{task_id, specialty, current_rate}, ...]``. Returns
        the task_ids actually flagged (the UPDATE is the arbiter, so this stays
        idempotent under concurrent sweeps)."""
        if not decisions:
            return []
        flagged: List[str] = []
        now = _utcnow_iso()
        with self._conn() as conn:
            for d in decisions:
                cur = conn.execute(
                    "UPDATE tasks SET max_labels = 2, status = 'open' "
                    "WHERE task_id = ? AND status IN ('open', 'done') AND max_labels < 2",
                    (d["task_id"],),
                )
                if cur.rowcount > 0:
                    flagged.append(d["task_id"])
            if flagged:
                by_id = {d["task_id"]: d for d in decisions}
                conn.executemany(
                    "INSERT INTO events (entity_type, entity_id, event_type, actor, "
                    "occurred_at, payload_json) VALUES ('task', ?, 'double_label_flagged', "
                    "NULL, ?, ?)",
                    [(tid, now, json.dumps({
                        "specialty": by_id[tid].get("specialty"),
                        "current_rate": by_id[tid].get("current_rate"),
                    })) for tid in flagged],
                )
        return flagged

    def tasks_awaiting_double_label_decision(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Single-label tasks that already carry at least one verdict-bearing
        submission — the candidate set the double-label router decides over.

        Accepts ``'done'`` as well as ``'open'``: on the real submit route a
        singly-labeled task is closed by the time this runs, so a status='open'
        filter here matches nothing, by construction. Terminal statuses stay
        excluded — a rejected prompt is not a double-label candidate."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT t.*,
                       (SELECT s.submission_id FROM submissions s
                         WHERE s.task_id = t.task_id AND s.verdict IS NOT NULL
                         ORDER BY s.created_at ASC LIMIT 1) AS first_submission_id,
                       -- Carried here so the sweep does not re-open a connection
                       -- per candidate just to read one column (FIX A A-3.4).
                       (SELECT s2.confidence FROM submissions s2
                         WHERE s2.task_id = t.task_id AND s2.verdict IS NOT NULL
                         ORDER BY s2.created_at ASC LIMIT 1) AS first_confidence
                FROM tasks t
                WHERE t.status IN ('open', 'done') AND t.max_labels < 2
                  AND EXISTS (SELECT 1 FROM submissions sf
                               WHERE sf.task_id = t.task_id AND sf.verdict IS NOT NULL)
                ORDER BY t.created_at ASC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        out = []
        for r in rows:
            rec = self._task_row(r)
            out.append(rec)
        return out

    def double_label_counts(self) -> tuple:
        """``(n_labeled_tasks, n_flagged_for_double_label)`` in ONE pass.

        The sweep needs both numbers once per run, not a recomputed ratio per
        candidate (FIX A A-3.4). Returning the raw counts lets the caller advance
        the rate locally as it flags."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS labeled, "
                "       SUM(CASE WHEN t.max_labels >= 2 THEN 1 ELSE 0 END) AS flagged "
                "FROM tasks t WHERE EXISTS "
                "(SELECT 1 FROM submissions s WHERE s.task_id = t.task_id)"
            ).fetchone()
        rec = dict(row) if row else {}
        return int(rec.get("labeled") or 0), int(rec.get("flagged") or 0)

    def double_label_flag_rate(self) -> float:
        """Share of labeled tasks currently routed for a second independent label —
        the ``current_rate`` input to the stratified top-up policy (PRD A §1.3)."""
        labeled, flagged = self.double_label_counts()
        return (flagged / labeled) if labeled else 0.0

    def next_double_label_for(
        self,
        user_id: str,
        *,
        specialty: Optional[str] = None,
        allow_real: bool = False,
        scan_limit: int = 200,
    ) -> Optional[Dict[str, Any]]:
        """Oldest task flagged for a second independent label that this user may
        take (PRD A §1.4). The first labeler — and anyone who already submitted —
        is excluded BY QUERY (``NOT EXISTS`` on their submissions), never by caller
        discipline: the second observation must be independent or κ is fiction.
        ``allow_real`` is the V4 wall (EHR PRD §9.5): real_deid tasks are excluded
        unless the caller verified ``real_data_approved``."""
        clauses = [
            "t.status = 'open'",
            "t.max_labels >= 2",
            "EXISTS (SELECT 1 FROM submissions sf WHERE sf.task_id = t.task_id"
            " AND sf.verdict IS NOT NULL)",
            "NOT EXISTS (SELECT 1 FROM submissions sm WHERE sm.task_id = t.task_id"
            " AND sm.evaluator_id = ?)",
            "NOT EXISTS (SELECT 1 FROM ingest_cases ic WHERE ic.task_id = t.task_id"
            " AND ic.status = 'needs_review')",
            # PRD 2 §9.1 — the sealed future applies to the SECOND-label draw too.
            # A trajectory point only reaches this query when an admin explicitly
            # set ``max_labels >= 2`` on it (§9.6 makes 1 the default), so this
            # branch is rare — and rare is exactly how an ordering bug survives to
            # production. The second walker is a physician reading the same chart
            # forward, and out-of-order serving breaks the seal for them
            # identically.
            _PRD_2_SEQUENCE_GATE,
        ]
        # One entry per ``?`` above, in clause order: the independence NOT EXISTS,
        # then the sequence gate's.
        params: List[Any] = [user_id, user_id]
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        if not allow_real:
            clauses.append("(t.case_source IS NULL OR t.case_source != 'real_deid')")
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT t.*,
                       (SELECT COUNT(*) FROM submissions s WHERE s.task_id = t.task_id) AS sub_count
                FROM tasks t
                WHERE {' AND '.join(clauses)}
                ORDER BY t.created_at ASC LIMIT ?
                """,
                tuple(params + [int(scan_limit)]),
            ).fetchall()
        for r in rows:
            rec = self._task_row(r)
            if int(rec.get("sub_count") or 0) >= int(rec.get("max_labels") or 1):
                continue  # already at capacity — second label landed
            rec.pop("sub_count", None)
            return rec
        return None

    def get_agreement_observation(self, task_id: str) -> Optional[Dict[str, Any]]:
        """The double-label agreement observation for one task, if it exists —
        the source of truth for a record's ``independent_second_label`` flag
        (PRD A Phase 3)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM agreement WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        rec = dict(row)
        rec["tags_a"] = json.loads(rec.pop("tags_a_json", "[]") or "[]")
        rec["tags_b"] = json.loads(rec.pop("tags_b_json", "[]") or "[]")
        return rec
    # ═══ END PRD-A ═══
    # ═══ PRD-R PAIRED REVIEW STORE METHODS — owned by Agent R ════════════════
    # The review unit is the TASK (PRD R §2.1). The lease mechanics and the
    # compare-and-swap are the PRD-A ones verbatim — they work — moved from the
    # submission row to the task row.

    def next_review_pair_for(
        self,
        user_id: str,
        *,
        specialty: Optional[str] = None,
        lease_minutes: int = 45,
        scan_limit: int = 200,
    ) -> Optional[Dict[str, Any]]:
        """Oldest ``review_ready`` task this reviewer may adjudicate, or None.

        Every requirement is enforced IN SQL, never by caller discipline:

          * ``review_ready`` — at least two verdict-bearing submissions;
          * the reviewer authored neither submission (``NOT EXISTS`` on their
            own submissions for the task) — a physician grading their own work
            is not a review, and κ's blinding claim collapses with it;
          * the reviewer has not already reviewed this task;
          * unclaimed, or holding a claim whose lease has expired, so an
            abandoned draw re-queues instead of vanishing forever;
          * the specialty matches;
          * the task is not held for blocking ingest review (Audit §21.6) — a
            reviewer must not see a case whose image may carry burned-in PHI.

        Returns the task row; the caller pairs it with
        ``submissions_for_task`` and claims it with ``claim_task_for_review``.
        """
        cutoff = _iso_minus_seconds(max(1, int(lease_minutes)) * 60)
        clauses = [
            # ``review_ready`` in the SQL exactly as ``routing.phase`` derives it:
            # EXACTLY two labels, and the task has reached the capacity it was
            # provisioned for.
            #
            # ``= 2``, not ``>= 2`` (Audit R H3). A paired review is defined over
            # two labels; ``routing.ab_pair`` truncates to two in Python, so a
            # three-label case used to be served with one physician's work simply
            # absent — and then retired as 'reviewed' by a reviewer who never saw
            # it. Paid work, adjudicated never, invisible to both queues. A case
            # that carries more than two labels is not review_ready; it is a data
            # condition, and ``review_pair_queue_stats`` counts it so a human can
            # see it rather than it vanishing.
            #
            # The capacity clause is the other half: an admin-set max_labels of 3
            # with two labels in is awaiting_second, and if only the state machine
            # knew that, the SQL and ``routing.phase`` would be two truths.
            f"{_PRD_R_LABEL_COUNT_CORRELATED} = 2",
            f"{_PRD_R_LABEL_COUNT_CORRELATED} >= COALESCE(t.max_labels, 1)",
            "NOT EXISTS (SELECT 1 FROM submissions sm WHERE sm.task_id = t.task_id"
            " AND sm.evaluator_id = ?)",
            # ONE ADJUDICATION PER CASE, from EITHER queue (Audit R H2). This was
            # scoped to ``cr.reviewer_user_id = ?``, which let a case that had
            # already been reviewed through the single-submission flow be drawn
            # again as a pair — two ``case_reviews`` rows for one case, and an
            # expert-acceptance rate that counts it twice. The per-reviewer
            # exclusion PRD R §2.1 asks for is subsumed by this stricter one.
            "NOT EXISTS (SELECT 1 FROM case_reviews cr WHERE cr.task_id = t.task_id)",
            # NULL = never drawn; an 'in_review' claim past its lease re-queues;
            # and a reviewer is always re-served the case THEY are holding, so a
            # page reload resumes their work instead of telling them the queue is
            # empty while their own claim blocks it. 'reviewed' is terminal: one
            # adjudication per case is the product, and the per-reviewer
            # exclusion above is the belt to that brace.
            "(t.review_status IS NULL OR (t.review_status = 'in_review'"
            " AND (t.review_claimed_at IS NULL OR t.review_claimed_at < ?"
            "      OR t.review_claimed_by = ?)))",
            "NOT EXISTS (SELECT 1 FROM ingest_cases ic WHERE ic.task_id = t.task_id"
            " AND ic.status = 'needs_review')",
        ]
        params: List[Any] = [user_id, cutoff, user_id]
        if specialty:
            clauses.append("t.specialty = ?")
            params.append(specialty)
        params.append(int(scan_limit))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT t.* FROM tasks t
                WHERE {' AND '.join(clauses)}
                ORDER BY t.created_at ASC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return self._task_row(rows[0]) if rows else None

    def claim_task_for_review(
        self, task_id: str, *, reviewer_id: str, blinded: Optional[bool] = None,
        lease_minutes: int = 45,
    ) -> bool:
        """Atomically claim a TASK for paired review. True when this caller won.

        Compare-and-set: the UPDATE only matches while the row is unclaimed or
        its prior claim's lease has expired, so two reviewers drawing
        concurrently can never both hold the same case.

        ``review_claimed_at`` is the lease clock rather than ``updated_at`` —
        any unrelated write bumps ``updated_at`` and would silently extend a
        reviewer's claim (the defect FIX A A-3.7 named on the submission row).
        ``review_blinded`` records the blinding DERIVED from the payload we are
        actually about to serve; an asserted constant is the defect F2 named.
        """
        now = _utcnow_iso()
        cutoff = _iso_minus_seconds(max(1, int(lease_minutes)) * 60)
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET review_status = 'in_review', review_claimed_by = ?,
                       review_claimed_at = ?, review_blinded = ?
                WHERE task_id = ?
                  AND (review_status IS NULL
                       OR (review_status = 'in_review'
                           AND (review_claimed_at IS NULL OR review_claimed_at < ?
                                OR review_claimed_by = ?)))
                """,
                (reviewer_id, now, None if blinded is None else (1 if blinded else 0),
                 task_id, cutoff, reviewer_id),
            )
            return cur.rowcount > 0

    def task_review_claim(self, task_id: str, *, lease_minutes: int = 45) -> Dict[str, Any]:
        """The current review claim on a task: ``{holder, claimed_at, blinded,
        expired, status}``. ``blinded`` is tri-state — None means no draw ever
        asserted it, which is NOT the same as unblinded."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT review_status, review_claimed_by, review_claimed_at, "
                "review_blinded FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return {"holder": None, "claimed_at": None, "blinded": None,
                    "expired": True, "status": None}
        rec = dict(row)
        claimed_at = rec.get("review_claimed_at")
        cutoff = _iso_minus_seconds(max(1, int(lease_minutes)) * 60)
        blinded = rec.get("review_blinded")
        return {
            "holder": rec.get("review_claimed_by"),
            "claimed_at": claimed_at,
            "blinded": None if blinded is None else bool(blinded),
            "expired": (claimed_at is None) or (claimed_at < cutoff),
            "status": rec.get("review_status"),
        }

    def has_task_review_by(self, task_id: str, reviewer_user_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM case_reviews WHERE task_id = ? AND reviewer_user_id = ? LIMIT 1",
                (task_id, reviewer_user_id),
            ).fetchone()
        return row is not None

    def mark_task_reviewed(self, task_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET review_status = 'reviewed' WHERE task_id = ?", (task_id,))

    def insert_pair_review(
        self,
        *,
        task_id: str,
        reviewer_user_id: str,
        reviewer_id_hashed: str,
        verdict: str,
        stronger: str,
        stronger_submission_id: Optional[str],
        pair_sub_a: Optional[str],
        pair_sub_b: Optional[str],
        accepted_submission_id: Optional[str],
        dimensions: Optional[Dict[str, str]] = None,
        corrections: Optional[Dict[str, Any]] = None,
        reviewer_notes: Optional[str] = None,
        time_spent_sec: Optional[int] = None,
        blinded: Optional[bool] = None,
        identifier_flags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """One senior-reviewer adjudication of a PAIR (PRD R §2.3).

        Writes through :meth:`insert_case_review` rather than issuing a second
        INSERT against the same table: one row shape, one place that knows how a
        review is written, and every existing reader (``reviews_for_task``,
        ``agreement.review_acceptance``, the export block) keeps working
        unchanged. The pair columns are then stamped on.

        ``submission_id`` — a NOT NULL column, and the anchor
        ``reviews_for_submission`` reads — is set to the ACCEPTED submission when
        there is one, and to the canonical first otherwise, so a review always
        resolves to a real row. ``accepted_submission_id`` is the field that
        carries the meaning: NULL there means "no side was accepted", which
        ``submission_id`` cannot express.

        ``pair_sub_a``/``pair_sub_b`` are CANONICAL (oldest-first), never this
        reviewer's shuffled positions — two reviewers' rows on the same case have
        to be comparable to each other. ``stronger`` is canonical for the same
        reason and is stored alongside ``stronger_submission_id``, which is the
        form no reader can misinterpret (Audit R H1).

        ONE TRANSACTION (Audit R M2). An adjudication is four writes — the review
        row, its pair columns, the task's review status, and retiring the two
        labels — and they used to happen across FIVE independent connections,
        two here and three in the router. A crash in the middle left a
        ``case_reviews`` row that COUNTS in ``review_acceptance`` with NULL pair
        columns, beside a task still ``in_review`` that reproduces H2 the moment
        its lease expires. They now commit together or not at all.

        The cost of that is a second writer for ``case_reviews``: this issues the
        INSERT itself rather than routing through :meth:`insert_case_review`,
        because that method opens its own connection and there is no way to join
        it to this transaction. Atomicity on a row that feeds a buyer-facing
        statistic is worth more than a single INSERT site, and
        ``test_both_review_writers_produce_the_same_row_shape`` is the guard that
        keeps the two honest.
        """
        anchor = accepted_submission_id or pair_sub_a or pair_sub_b or ""
        review_id = _new_id("rev")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO case_reviews
                  (review_id, task_id, submission_id, reviewer_user_id, reviewer_id_hashed,
                   verdict, dimension_json, corrections_json, reviewer_notes,
                   time_spent_sec, blinded, identifier_flags, created_at,
                   pair_sub_a, pair_sub_b, stronger, stronger_submission_id,
                   accepted_submission_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id, task_id, anchor, reviewer_user_id, reviewer_id_hashed,
                    verdict,
                    json.dumps(dimensions or {}),
                    json.dumps(corrections) if corrections is not None else None,
                    reviewer_notes,
                    int(time_spent_sec) if time_spent_sec is not None else None,
                    None if blinded is None else (1 if blinded else 0),
                    None if identifier_flags is None else json.dumps(sorted(identifier_flags)),
                    now,
                    pair_sub_a, pair_sub_b, stronger, stronger_submission_id,
                    accepted_submission_id,
                ),
            )
            conn.execute(
                "UPDATE tasks SET review_status = 'reviewed' WHERE task_id = ?", (task_id,))
            # Retire EXACTLY the two labels this adjudication covers (Audit R H3).
            for sid in (pair_sub_a, pair_sub_b):
                if sid:
                    conn.execute(
                        "UPDATE submissions SET review_status = 'reviewed', updated_at = ? "
                        "WHERE submission_id = ?", (now, sid))
        return self.get_case_review(review_id)  # type: ignore[return-value]

    def review_pair_queue_stats(self) -> Dict[str, Any]:
        """Counts for the TR page header, in one pass per phase.

        Reported as the lifecycle phases the reviewer actually cares about —
        how many cases are waiting for a second label, how many pairs are ready,
        how many are adjudicated — rather than the submission-level review states,
        which under the paired flow no longer describe anything a TR can act on.

        Audit R M4: this ran a correlated subquery per task over the whole task
        table, unbounded, on a header refreshed by every draw. It now shares the
        SAME grouped-join shape as the labeler queue — one pass over an index —
        so a header can no longer cost more than the work it describes.

        ``parked`` (Audit R M5) counts cases a draw refused as not-independent
        and set terminal. Two physicians' paid labels are stranded in each one,
        and until this they existed only in an ERROR log nothing reads.
        """
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT
                  SUM(CASE WHEN lc = 1 THEN 1 ELSE 0 END) AS awaiting_second,
                  SUM(CASE WHEN lc = 2 AND rc = 0 THEN 1 ELSE 0 END) AS review_ready,
                  SUM(CASE WHEN rc > 0 THEN 1 ELSE 0 END) AS adjudicated,
                  -- Audit R H3: a case carrying more than two labels cannot be
                  -- adjudicated by a PAIRED review. Counted, not dropped — the
                  -- whole failure was that this work was invisible.
                  SUM(CASE WHEN lc > 2 AND rc = 0 THEN 1 ELSE 0 END) AS over_labelled,
                  -- Audit R M5: terminal, never adjudicated, nobody notified.
                  SUM(CASE WHEN rs = 'reviewed' AND rc = 0 THEN 1 ELSE 0 END) AS parked
                FROM (
                  SELECT COALESCE(c.n_labels, 0) AS lc,
                         COALESCE(r.n_reviews, 0) AS rc,
                         t.review_status AS rs
                  FROM tasks t
                  {_PRD_R_COUNTS_JOIN}
                  LEFT JOIN (SELECT task_id, COUNT(*) AS n_reviews
                               FROM case_reviews GROUP BY task_id) r
                         ON r.task_id = t.task_id
                  WHERE t.status IN ('open', 'done')
                )
                """
            ).fetchone()
        rec = dict(row) if row else {}
        return {
            "awaiting_second": int(rec.get("awaiting_second") or 0),
            "review_ready": int(rec.get("review_ready") or 0),
            "adjudicated": int(rec.get("adjudicated") or 0),
            "over_labelled": int(rec.get("over_labelled") or 0),
            "parked": int(rec.get("parked") or 0),
        }
    # ═══ END PRD-R ═══
    # ═══ PRD-C HEALTH SYSTEM STORE METHODS — owned by Agent 3, do not edit from other PRDs ═══
    @staticmethod
    def hs_id_for_name(name: str) -> str:
        """Deterministic ``hs-<slug>-<6hex>`` from the organization name, so the
        boot-time backfill mints the same id on every run (idempotent)."""
        norm = " ".join((name or "").lower().split())
        words = re.findall(r"[a-z0-9]+", norm)
        slug = "-".join(words)[:24].strip("-") or "org"
        hexpart = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:6]
        return f"hs-{slug}-{hexpart}"

    def _backfill_health_systems(self, conn: sqlite3.Connection) -> None:
        """One ``health_systems`` row per distinct historical partner label, and a
        ``health_system_id`` stamp on that partner's uploads — so no pre-portal
        upload is orphaned. Idempotent: matches by (case-insensitive) name and
        only touches uploads still missing a health_system_id."""
        labels: Dict[str, str] = {}
        for r in conn.execute("SELECT provider_id, org_name, email FROM data_providers").fetchall():
            # An email address is not an organization name (C-5.6). Where
            # org_name is blank there is nothing human to show, so the row is
            # named for manual correction rather than rendered as if a hospital
            # were literally called "it@mercy.org".
            label = (r["org_name"] or "").strip() or _needs_naming(r["email"])
            if label:
                labels[r["provider_id"]] = label
        for r in conn.execute("SELECT DISTINCT partner_id, partner_label FROM ingest_upload_links").fetchall():
            label = (r["partner_label"] or "").strip() or _needs_naming(r["partner_id"])
            if label:
                labels.setdefault(r["partner_id"], label)
        for r in conn.execute(
            "SELECT DISTINCT partner_id FROM ingest_uploads WHERE health_system_id IS NULL"
        ).fetchall():
            pid = (r["partner_id"] or "").strip()
            # 'account' is the LINK_ID sentinel and never a partner_id, so the
            # old guard here was dead — which is how an internal user id such as
            # 'u_abc123' ended up rendering in the Health Systems table as an
            # organization.
            if pid:
                labels.setdefault(pid, _needs_naming(pid))
        for partner_id, label in labels.items():
            row = conn.execute(
                "SELECT hs_id FROM health_systems WHERE LOWER(name) = LOWER(?)", (label,)
            ).fetchone()
            if not row:
                # UPGRADE PATH: a database written by the previous release may
                # already hold this partner under its raw internal id or email
                # (the name C-5.6 stopped producing). Rename that row in place
                # rather than inserting a second one — otherwise the operator
                # sees the same hospital twice with its upload history split
                # between them, and only on deployments that have run before.
                legacy = _legacy_partner_name(label)
                if legacy:
                    row = conn.execute(
                        "SELECT hs_id FROM health_systems WHERE LOWER(name) = LOWER(?)",
                        (legacy,),
                    ).fetchone()
                    if row:
                        conn.execute("UPDATE health_systems SET name = ? WHERE hs_id = ?",
                                     (label, row["hs_id"]))
            hs_id = row["hs_id"] if row else self.hs_id_for_name(label)
            if not row:
                conn.execute(
                    "INSERT OR IGNORE INTO health_systems (hs_id, name, active, created_at) "
                    "VALUES (?, ?, 1, ?)",
                    (hs_id, label, _utcnow_iso()),
                )
            conn.execute(
                "UPDATE ingest_uploads SET health_system_id = ? "
                "WHERE partner_id = ? AND health_system_id IS NULL",
                (hs_id, partner_id),
            )

    def ensure_health_system(self, name: str, *, contact_email: Optional[str] = None,
                             notes: Optional[str] = None) -> Dict[str, Any]:
        """Create-or-reuse by (case-insensitive) organization name."""
        clean = " ".join((name or "").split())
        if not clean:
            raise ValueError("health system name is required")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM health_systems WHERE LOWER(name) = LOWER(?)", (clean,)
            ).fetchone()
            if row:
                if contact_email and not row["contact_email"]:
                    conn.execute("UPDATE health_systems SET contact_email = ? WHERE hs_id = ?",
                                 (contact_email, row["hs_id"]))
                    # Return the UPDATED values from this connection rather than
                    # re-reading (C-5.5): get_health_system opens a SECOND
                    # connection, and from inside this still-uncommitted block it
                    # read the pre-update row — so the caller got contact_email
                    # None while the committed row held the real address.
                    merged = dict(row)
                    merged["contact_email"] = contact_email
                    return merged
                return dict(row)
            hs_id = self.hs_id_for_name(clean)
            now = _utcnow_iso()
            conn.execute(
                "INSERT INTO health_systems (hs_id, name, contact_email, notes, active, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (hs_id, clean, contact_email, notes, now),
            )
            return {"hs_id": hs_id, "name": clean, "contact_email": contact_email,
                    "notes": notes, "active": 1, "created_at": now}

    def get_health_system(self, hs_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM health_systems WHERE hs_id = ?", (hs_id,)).fetchone()
        return dict(row) if row else None

    def list_health_systems(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM health_systems ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _hs_user_public(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d.pop("password_hash", None)
        return d

    def create_hs_portal_user(self, *, username: str, hs_id: str, password: str,
                              email: Optional[str] = None) -> Dict[str, Any]:
        uname = (username or "").strip().lower()
        if not uname:
            raise ValueError("username is required")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO hs_portal_users (username, hs_id, password_hash, must_reset, "
                "email, active, created_at) VALUES (?, ?, ?, 1, ?, 1, ?)",
                (uname, hs_id, hash_password(password), email, _utcnow_iso()),
            )
        return self.get_hs_portal_user_public(uname)  # type: ignore[return-value]

    def get_hs_portal_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Full row including password_hash — internal auth use only."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM hs_portal_users WHERE username = ?", ((username or "").lower(),)
            ).fetchone()
        return dict(row) if row else None

    def get_hs_portal_user_public(self, username: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM hs_portal_users WHERE username = ?", ((username or "").lower(),)
            ).fetchone()
        return self._hs_user_public(row) if row else None

    def list_hs_portal_users(self, hs_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            if hs_id:
                rows = conn.execute(
                    "SELECT * FROM hs_portal_users WHERE hs_id = ? ORDER BY created_at DESC", (hs_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM hs_portal_users ORDER BY created_at DESC").fetchall()
        return [self._hs_user_public(r) for r in rows]

    def hs_username_exists(self, username: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM hs_portal_users WHERE username = ?", ((username or "").lower(),)
            ).fetchone()
        return row is not None

    def set_hs_portal_password(self, username: str, new_password: str, *,
                               must_reset: bool = False) -> None:
        """Set the password and stamp ``password_changed_at``. The stamp is what
        invalidates outstanding session cookies (FIX-C C-2.3) — without it a
        leaked cookie outlived the victim's own password reset by up to the full
        12-hour session TTL."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE hs_portal_users SET password_hash = ?, must_reset = ?, "
                "failed_logins = 0, locked_until = NULL, password_changed_at = ?, "
                "session_epoch = session_epoch + 1 WHERE username = ?",
                (hash_password(new_password), 1 if must_reset else 0, _utcnow_iso(),
                 (username or "").lower()),
            )

    def set_hs_portal_active(self, username: str, active: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE hs_portal_users SET active = ? WHERE username = ?",
                         (1 if active else 0, (username or "").lower()))

    def set_health_system_active(self, hs_id: str, active: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE health_systems SET active = ? WHERE hs_id = ?",
                         (1 if active else 0, hs_id))

    # ─── Brute-force bookkeeping, keyed on (username, ip) ────────────────────
    # ONE path for known and unknown usernames — see the schema note. Every
    # method here behaves identically whether or not the username exists, which
    # is the property that closes the enumeration oracle.
    @staticmethod
    def _hs_attempt_key(username: str, ip: str) -> str:
        ip_hash = hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()[:16]
        return f"{(username or '').lower()}|{ip_hash}"

    def hs_login_locked(self, username: str, ip: str) -> bool:
        """True while (username, ip) is inside its lock window."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT locked_until FROM hs_login_attempts WHERE attempt_key = ?",
                (self._hs_attempt_key(username, ip),),
            ).fetchone()
        if not row or not row["locked_until"]:
            return False
        try:
            until = datetime.fromisoformat(row["locked_until"])
        except (TypeError, ValueError):
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)

    def record_hs_login_failure(self, username: str, ip: str, *, lock_threshold: int = 5,
                                lock_minutes: int = 15) -> Dict[str, Any]:
        """Count a failed sign-in for (username, ip). Returns
        ``{"fails": n, "locked": bool}``.

        Counting happens BEFORE the caller decides the status code, and the
        decay window runs from the LAST failure for both the known and unknown
        case — the old split (record-after-check on one path, record-before on
        the other, decay from first vs fifth failure) is what leaked account
        existence on the 5th attempt.
        """
        uname = (username or "").lower()
        key = self._hs_attempt_key(uname, ip)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        window_start = now - timedelta(minutes=lock_minutes)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT fails, last_fail_at FROM hs_login_attempts WHERE attempt_key = ?", (key,)
            ).fetchone()
            fails = 0
            if row:
                try:
                    last = datetime.fromisoformat(row["last_fail_at"] or "")
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    last = None
                # A stale streak decays; a live one accumulates.
                fails = int(row["fails"] or 0) if (last and last > window_start) else 0
            fails += 1
            locked = fails >= lock_threshold
            until = (now + timedelta(minutes=lock_minutes)).isoformat() if locked else None
            conn.execute(
                "INSERT INTO hs_login_attempts (attempt_key, username, fails, first_fail_at, "
                "last_fail_at, locked_until) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(attempt_key) DO UPDATE SET fails = excluded.fails, "
                "last_fail_at = excluded.last_fail_at, locked_until = excluded.locked_until",
                (key, uname, fails, now_iso, now_iso, until),
            )
            # Opportunistic sweep: a username-spray attack mints one row per
            # (guessed name, ip) and nothing else would ever remove them, so the
            # table would grow without bound on a public endpoint. Rows past
            # their window carry no signal — they neither lock nor count.
            conn.execute(
                "DELETE FROM hs_login_attempts WHERE last_fail_at < ? "
                "AND (locked_until IS NULL OR locked_until < ?)",
                (window_start.isoformat(), now_iso),
            )
            # Username-scoped signal for the admin ("this account is being
            # attacked"). Deliberately NOT a gate: making it one is what turned
            # the lockout into a remote kill switch for a whole hospital.
            conn.execute(
                "UPDATE hs_portal_users SET failed_logins = failed_logins + 1 WHERE username = ?",
                (uname,),
            )
        return {"fails": fails, "locked": locked}

    def clear_hs_login_attempts(self, username: str, *, ip: Optional[str] = None) -> int:
        """Clear lock state for a username (optionally just from one ip).
        Returns the number of attempt rows cleared."""
        uname = (username or "").lower()
        with self._conn() as conn:
            if ip is not None:
                cur = conn.execute("DELETE FROM hs_login_attempts WHERE attempt_key = ?",
                                   (self._hs_attempt_key(uname, ip),))
            else:
                cur = conn.execute("DELETE FROM hs_login_attempts WHERE username = ?", (uname,))
            n = cur.rowcount or 0
            conn.execute(
                "UPDATE hs_portal_users SET failed_logins = 0, locked_until = NULL "
                "WHERE username = ?", (uname,),
            )
        return n

    def hs_login_failure_signal(self, username: str) -> int:
        """Live failure count for a username across all IPs — the admin-facing
        'under attack' signal, and the input to the progressive delay."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(fails), 0) FROM hs_login_attempts WHERE username = ?",
                ((username or "").lower(),),
            ).fetchone()
        return int(row[0] or 0)

    def mark_hs_login_success(self, username: str, ip: Optional[str] = None) -> None:
        self.clear_hs_login_attempts(username, ip=ip)
        with self._conn() as conn:
            conn.execute(
                "UPDATE hs_portal_users SET last_login = ? WHERE username = ?",
                (_utcnow_iso(), (username or "").lower()),
            )

    # ─── Session revocation ──────────────────────────────────────────────────
    def revoke_hs_token(self, jti: str, expires_at: str) -> None:
        """Denylist a session token until it would have expired anyway, so
        'Sign out' on a shared hospital workstation is real, not cosmetic."""
        if not jti:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO hs_revoked_tokens (jti, expires_at, revoked_at) "
                "VALUES (?, ?, ?)", (jti, expires_at, _utcnow_iso()),
            )
            # Opportunistic sweep — the denylist only needs to hold unexpired
            # tokens, so it stays small without a scheduled job.
            conn.execute("DELETE FROM hs_revoked_tokens WHERE expires_at < ?",
                         (datetime.now(timezone.utc).isoformat(),))

    def hs_token_revoked(self, jti: str) -> bool:
        if not jti:
            return False
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM hs_revoked_tokens WHERE jti = ?", (jti,)).fetchone()
        return row is not None

    def hs_upload_bytes_since(self, hs_id: str, since_iso: str) -> int:
        """Bytes this health system has uploaded since ``since_iso`` — the input
        to the per-account quota (FIX-C C-2.6)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM ingest_uploads "
                "WHERE health_system_id = ? AND created_at >= ?", (hs_id, since_iso),
            ).fetchone()
        return int(row[0] or 0)

    def set_ingest_specialty_for_upload(self, upload_id: str, specialty: str) -> int:
        """Operator-assigned specialty for every case from one upload.

        Ingest leaves specialty NULL when nothing declared it (FIX-C C-3.2);
        promotion downstream still falls back to a literal, so the operator has
        to be able to set the real value BEFORE promoting rather than after a
        mislabeled case has shipped."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE ingest_cases SET specialty = ?, updated_at = ? WHERE upload_id = ?",
                (specialty, _utcnow_iso(), upload_id),
            )
            return cur.rowcount or 0

    def list_ingest_cases_for_uploads(self, upload_ids: List[str]) -> List[Dict[str, Any]]:
        """Every ingest case for a set of uploads in ONE query (FIX-C C-5.4).
        The health-system detail page issued one query per upload, up to 500."""
        if not upload_ids:
            return []
        out: List[Dict[str, Any]] = []
        with self._conn() as conn:
            # Chunked to stay under SQLite's variable limit on a large page.
            for i in range(0, len(upload_ids), 400):
                chunk = upload_ids[i:i + 400]
                qs = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM ingest_cases WHERE upload_id IN ({qs}) "
                    "ORDER BY created_at DESC", tuple(chunk),
                ).fetchall()
                for r in rows:
                    rec = dict(r)
                    rec["case"] = json.loads(rec.pop("case_json") or "null")
                    rec["report"] = json.loads(rec.pop("report_json") or "null")
                    rec["review"] = json.loads((rec.get("review_json") or "null") or "null")
                    out.append(rec)
        return out

    def upload_specialties(self, upload_id: str) -> List[Optional[str]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT specialty FROM ingest_cases WHERE upload_id = ?", (upload_id,)
            ).fetchall()
        return [r["specialty"] for r in rows]

    def set_upload_health_system(self, upload_id: str, hs_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_uploads SET health_system_id = ? WHERE upload_id = ?",
                (hs_id, upload_id),
            )

    def list_case_reviews_for_reviewer(self, user_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
        """A reviewer's case_reviews rows (PRD-A table, read-only from PRD-C).
        Defensive: before PRD-A merges the table does not exist — return []
        rather than 500 the physicians page."""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT review_id, task_id, submission_id, verdict, created_at "
                    "FROM case_reviews WHERE reviewer_user_id = ? "
                    "ORDER BY created_at DESC LIMIT ?", (user_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    # Metrics — the four questions (PRD-C Phase 6). All reads, all defensive:
    # PRD-A's case_reviews may not exist yet, and a missing table must read as
    # "no data", never a 500 on the metrics page.
    _METRIC_SPARK_SOURCES = {
        # kind → (table, timestamp column, extra WHERE)
        "submissions": ("submissions", "created_at", ""),
        "reviews": ("case_reviews", "created_at", ""),
        "uploads": ("ingest_uploads", "created_at", ""),
        "exports": ("exports", "created_at", ""),
    }

    def metrics_daily_counts(self, kind: str, *, days: int = 14) -> List[int]:
        """Per-day row counts over the trailing window, oldest first — the
        sparkline series. Unknown/missing tables yield a flat zero series."""
        src = self._METRIC_SPARK_SOURCES.get(kind)
        if not src:
            return [0] * days
        table, col, extra = src
        start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
        counts = {i: 0 for i in range(days)}
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT DATE({col}) AS d, COUNT(*) AS n FROM {table} "
                    f"WHERE DATE({col}) >= ? {extra} GROUP BY DATE({col})", (start,),
                ).fetchall()
        except sqlite3.OperationalError:
            return [0] * days
        base = datetime.now(timezone.utc).date()
        for r in rows:
            try:
                offset = (base - datetime.fromisoformat(r["d"]).date()).days
            except (TypeError, ValueError):
                continue
            if 0 <= offset < days:
                counts[days - 1 - offset] = int(r["n"])
        return [counts[i] for i in range(days)]

    def metrics_four_questions(self) -> Dict[str, Any]:
        """SUPPLY · QUALITY · PIPELINE · DEMAND — one rollup for the admin
        metrics header. Expert acceptance and Cohen's κ are computed and
        returned SEPARATELY (κ belongs to the caller's /stats agreement slice;
        acceptance comes from PRD-A's review verdicts here)."""
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with self._conn() as conn:
            active_week = conn.execute(
                "SELECT COUNT(DISTINCT s.evaluator_id) FROM submissions s "
                "JOIN users u ON u.id = s.evaluator_id "
                "WHERE s.created_at >= ? AND u.is_mock = 0", (week_ago,),
            ).fetchone()[0]
            labeled_total = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
            uploads_received = conn.execute("SELECT COUNT(*) FROM ingest_uploads").fetchone()[0]
            awaiting_review = conn.execute(
                "SELECT COUNT(*) FROM ingest_uploads WHERE status IN "
                "('received', 'scanning', 'parsing', 'needs_review')").fetchone()[0]
            promoted = conn.execute(
                "SELECT COUNT(*) FROM ingest_cases WHERE status = 'promoted'").fetchone()[0]
            buyer_requests = conn.execute("SELECT COUNT(*) FROM buyer_requests").fetchone()[0]
            exports_n = conn.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
            shipped = conn.execute(
                "SELECT COUNT(*) FROM records WHERE status = 'exported'").fetchone()[0]
        # ONE definition of expert acceptance (Seam 3): PRD-A's
        # ``agreement.review_acceptance``. This block used to run its own SQL
        # that also counted edited-accept verdicts, while agreement.py counted
        # strict accepts only — the same word, over the same table, at two
        # numbers (~97% on the dashboard, ~84% in quality_report.md). It is the
        # figure a buyer audits most closely, so it gets one owner. The combined
        # figure is still available, under a DIFFERENT name ("not rejected").
        reviews: List[Dict[str, Any]] = []
        try:
            with self._conn() as conn:
                reviews = [dict(r) for r in conn.execute(
                    "SELECT verdict, dimension_json FROM case_reviews").fetchall()]
        except sqlite3.OperationalError:
            pass  # PRD-A not merged — reviews read as "no data", not zero-rate
        from asclepius import agreement as asc_agreement
        _review_acceptance = getattr(asc_agreement, "review_acceptance", None)
        if _review_acceptance is None:
            # The owner of the definition is not present (PRD-A not merged).
            # Report "unknown", never a locally-computed substitute — a second
            # definition is the defect, and a wrong number is worse than none.
            acc = {"n": len(reviews), "accept_rate": None, "edit_rate": None}
        else:
            acc = _review_acceptance(reviews)
        reviews_total = int(acc.get("n") or 0)
        acceptance = acc.get("accept_rate")
        edit_rate = acc.get("edit_rate")
        # "Not rejected" is a different number and therefore a different word.
        not_rejected = (None if (not reviews_total or acceptance is None)
                        else round((acceptance or 0) + (edit_rate or 0), 4))
        return {
            "supply": {
                "physicians_active_week": int(active_week or 0),
                "cases_labeled": int(labeled_total or 0),
                "cases_reviewed": int(reviews_total or 0),
                "spark": self.metrics_daily_counts("submissions"),
            },
            "quality": {
                # Tri-state: a float when reviews exist, null when none — "no
                # reviews yet" must never render as a 0% acceptance rate.
                # Strict accepts only, exactly as agreement.review_acceptance
                # defines it and as quality_report.md reports it.
                "expert_acceptance": acceptance,
                "edit_rate": edit_rate,
                "not_rejected": not_rejected,
                "reviews_scored": reviews_total,
                "spark": self.metrics_daily_counts("reviews"),
            },
            "pipeline": {
                "uploads_received": int(uploads_received or 0),
                "awaiting_review": int(awaiting_review or 0),
                "promoted_to_task": int(promoted or 0),
                "spark": self.metrics_daily_counts("uploads"),
            },
            "demand": {
                "buyer_requests": int(buyer_requests or 0),
                "exports": int(exports_n or 0),
                "records_shipped": int(shipped or 0),
                "spark": self.metrics_daily_counts("exports"),
            },
        }

    def count_case_reviews_for_tasks(self, task_ids: List[str]) -> int:
        """How many PRD-A case_reviews cover these tasks (read-only). Defensive:
        0 when the table does not exist yet (pre-merge-A)."""
        if not task_ids:
            return 0
        qs = ",".join("?" for _ in task_ids)
        try:
            with self._conn() as conn:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM case_reviews WHERE task_id IN ({qs})",
                    tuple(task_ids),
                ).fetchone()
            return int(row[0] or 0)
        except sqlite3.OperationalError:
            return 0

    def list_uploads_for_health_system(self, hs_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ingest_uploads WHERE health_system_id = ? "
                "ORDER BY created_at DESC LIMIT ?", (hs_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["files"] = json.loads(d.pop("files_json") or "[]")
            out.append(d)
        return out
    # ═══ END PRD-C STORE METHODS ═══
    # ═══ REFERRAL STORE METHODS (PRD-REF) ═══
    # The referral spine. Shipped with the retired advisor tier and kept:
    # every verified physician refers now.

    # ─── Appointment ─────────────────────────────────────────────────────────
    def _mint_referral_code(self, conn: sqlite3.Connection) -> str:
        """A short, speakable, collision-checked code. Advisors read these to
        people over the phone, so no ambiguous glyphs (0/O, 1/I/l).

        The SELECT is an optimisation, not the guarantee — the partial unique
        index on ``users(referral_code)`` is (audit L7). Two concurrent
        appointments could both see a free code and race; the loser used to
        surface as a 500. Retried here instead, so a race is invisible to the
        caller rather than an error page on the one action that mints an
        advisor.
        """
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        for _ in range(12):
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            row = conn.execute(
                "SELECT 1 FROM users WHERE referral_code = ?", (code,)).fetchone()
            if row is None:
                return code
        # 31^8 with a dozen tries: reaching here means something is very wrong
        # (a code column full of one value), and a silent duplicate would break
        # referral attribution invisibly.
        raise RuntimeError("could not mint a unique referral code")

    def get_user_by_referral_code(self, code: str) -> Optional[Dict[str, Any]]:
        code = (code or "").strip().upper()
        if not code:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE referral_code = ?", (code,)).fetchone()
        return dict(row) if row else None

    # ─── Referrals (PRD §3) ──────────────────────────────────────────────────
    def insert_referral(
        self,
        *,
        referrer_id: str,
        referral_code: str,
        invitee_email: Optional[str] = None,
        invitee_name: Optional[str] = None,
        note: Optional[str] = None,
        status: Optional[str] = "invited",
        source: Optional[str] = None,
        signup_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        rid = _new_id("ref")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO referrals (referral_id, referrer_id, referral_code,
                                       invitee_email, invitee_name, note, status,
                                       source, signup_ip, invited_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rid, referrer_id, referral_code,
                 (invitee_email or "").lower().strip() or None,
                 invitee_name, note, status, source,
                 (signup_ip or "").strip() or None, _utcnow_iso()),
            )
        return self.get_referral(rid)  # type: ignore[return-value]

    def set_referral_fraud_flag(self, referral_id: str, flag: str) -> None:
        """Stamp a review flag on one referral. Additive and display-only:
        nothing reads it on a money path, so a wrong flag costs an admin a
        glance rather than a physician a bounty."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE referrals SET fraud_flag = ? WHERE referral_id = ?",
                (flag, referral_id))

    def stamp_referral_first_case(self, referral_id: str, *, at: str) -> None:
        """Record when the invitee's first accepted case settled the bounty.
        First writer wins: the stamp is a historical fact, not a live status."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE referrals SET first_case_at = ? "
                "WHERE referral_id = ? AND first_case_at IS NULL",
                (at, referral_id))

    def count_same_ip_referrals(self, referrer_id: str, signup_ip: str) -> int:
        """How many of this referrer's link signups already arrived from this
        IP. Feeds the same-IP fraud heuristic; a hospital NAT will trip it,
        which is exactly why the flag is a review cue and never a block."""
        ip = (signup_ip or "").strip()
        if not ip:
            return 0
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM referrals "
                "WHERE referrer_id = ? AND signup_ip = ?",
                (referrer_id, ip)).fetchone()
        return int(row["n"] or 0)

    def referral_earned_cents(self, referrer_id: str) -> int:
        """Everything the ledger has paid this referrer in referral bounties,
        void rows excluded. The cap check reads THIS, never a count of rows,
        so a historical rate change cannot bend the ceiling."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM earnings "
                "WHERE kind = 'referral' AND user_id = ? AND status != 'void'",
                (referrer_id,)).fetchone()
        return int(row["total"] or 0)

    def list_all_referrals(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        """Every referral with its referrer joined on, newest first: the admin
        overview. Bounded like every list in this file."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT r.*, u.full_name AS referrer_name, u.email AS referrer_email
                FROM referrals r
                LEFT JOIN users u ON u.id = r.referrer_id
                ORDER BY r.invited_at DESC LIMIT ?
                """,
                (max(1, limit),)).fetchall()
        return [dict(r) for r in rows]

    def set_user_role(self, user_id: str, role: str) -> Optional[Dict[str, Any]]:
        """Write one account's role. The caller owns policy (which roles, who
        may grant them, self-demotion refusal); this is only the write."""
        with self._conn() as conn:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        return self.get_user_by_id(user_id)

    # ─── Contributor scores (PRD-SCORE) ──────────────────────────────────────
    def get_contributor_score(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM contributor_scores WHERE user_id = ?",
                (user_id,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["components"] = json.loads(rec.pop("components_json", "null") or "null")
        return rec

    def upsert_contributor_score(
        self, *, user_id: str, score: float, n_cases: int,
        components: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO contributor_scores (user_id, score, n_cases, "
                "components_json, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET score = excluded.score, "
                "n_cases = excluded.n_cases, components_json = excluded.components_json, "
                "updated_at = excluded.updated_at",
                (user_id, float(score), int(n_cases),
                 json.dumps(components or {}), _utcnow_iso()))

    def record_contributor_score_history(
        self, *, user_id: str, score: float, prev_score: Optional[float],
        case_score: Optional[float], submission_id: Optional[str],
        components: Optional[Dict[str, Any]] = None,
    ) -> None:
        """One trajectory row per graded submission; a re-grade replaces its
        own entry (the partial unique index is the guard). With no submission
        (the initial rating) at most one marker row is written, ever."""
        with self._conn() as conn:
            if submission_id is None:
                exists = conn.execute(
                    "SELECT 1 FROM contributor_score_history "
                    "WHERE user_id = ? AND submission_id IS NULL LIMIT 1",
                    (user_id,)).fetchone()
                if exists:
                    return
            else:
                conn.execute(
                    "DELETE FROM contributor_score_history "
                    "WHERE user_id = ? AND submission_id = ?",
                    (user_id, submission_id))
            conn.execute(
                "INSERT INTO contributor_score_history "
                "(id, user_id, score, prev_score, case_score, submission_id, "
                " components_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_new_id("csh"), user_id, float(score),
                 None if prev_score is None else float(prev_score),
                 None if case_score is None else float(case_score),
                 submission_id, json.dumps(components or {}), _utcnow_iso()))

    def contributor_score_history(self, user_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM contributor_score_history WHERE user_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (user_id, max(1, limit))).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["components"] = json.loads(rec.pop("components_json", "null") or "null")
            out.append(rec)
        return out

    def insert_referee_bonus(
        self, *, earning_id: str, user_id: str, referral_id: str,
        amount_cents: int, accrued_at: str, note: Optional[str] = None,
    ) -> Optional[str]:
        """The invitee's own first-case bonus, at most once per referral row.

        Same shape as the referrer bounty: ``UNIQUE(kind, ref_id)`` with
        ``ref_id`` = the referral_id makes a second insert a database-level
        no-op, and the caller detects whether ITS insert won by comparing the
        earning id read back. Returns the earning id on the ledger (this
        call's or a predecessor's), or None if nothing could be written.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO earnings "
                "(earning_id, user_id, kind, ref_id, amount_cents, rate_cents, "
                " status, accrued_at, resolved_at, note) "
                "VALUES (?, ?, 'referee_first_case', ?, ?, ?, 'approved', ?, ?, ?)",
                (earning_id, user_id, referral_id, int(amount_cents),
                 int(amount_cents), accrued_at, accrued_at, note))
            row = conn.execute(
                "SELECT earning_id FROM earnings "
                "WHERE kind = 'referee_first_case' AND ref_id = ?",
                (referral_id,)).fetchone()
        return row["earning_id"] if row else None

    def get_referral(self, referral_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM referrals WHERE referral_id = ?", (referral_id,)).fetchone()
        return dict(row) if row else None

    def list_referrals_by_referrer(self, referrer_id: str,
                                   *, limit: int = 500) -> List[Dict[str, Any]]:
        """Every referral THIS advisor made. Scoped by the caller's session id —
        never by a query parameter (the IDOR rule from the portal work).

        Bounded (audit L6): every other list method in this file takes a limit,
        and an unbounded SELECT on a table that only grows is a slow leak rather
        than a bug you notice.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM referrals WHERE referrer_id = ? "
                "ORDER BY invited_at DESC LIMIT ?",
                (referrer_id, max(1, limit))).fetchall()
        return [dict(r) for r in rows]

    def referral_counts_by_referrer(self, referrer_ids: List[str]) -> Dict[str, Dict[str, int]]:
        """{referrer_id: {total, active}} for a page of advisors in ONE query.

        The admin roster previously ran two queries per advisor inside a loop
        over a full user scan (audit L6), each on a fresh SQLite connection.
        """
        ids = [i for i in (referrer_ids or []) if i]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT referrer_id, "
                f"       COUNT(*) AS total, "
                f"       SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS active "
                f"FROM referrals WHERE referrer_id IN ({placeholders}) "
                f"GROUP BY referrer_id",
                tuple(ids)).fetchall()
        return {r["referrer_id"]: {"total": int(r["total"] or 0),
                                   "active": int(r["active"] or 0)} for r in rows}

    def find_open_referral_for_email(self, email: str) -> Optional[Dict[str, Any]]:
        """The newest referral for this email that has not yet been claimed by a
        signup. Resolution is by email because that is the identifier the invite
        was addressed to and the one the invitee signs up with; a code that has
        to survive a React landing app, a tenant-store wizard and two redirects
        is a code that arrives NULL and attributes the referral to nobody."""
        email = (email or "").lower().strip()
        if not email:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM referrals WHERE invitee_email = ? AND user_id IS NULL "
                "ORDER BY invited_at DESC LIMIT 1",
                (email,)).fetchone()
        return dict(row) if row else None

    def move_open_referrals(self, old_email: str, new_email: str) -> int:
        """Re-key unclaimed referrals when an invitee changes their address
        mid-signup, and return how many moved.

        Attribution is email-keyed (see ``find_open_referral_for_email``), and
        the address it is keyed on is the one typed on ``/join`` -- which the
        very next screen of the wizard lets them edit. A doctor who opens a
        colleague's link with their personal address and then corrects it to
        their hospital one is doing something completely reasonable, and it
        silently cost the referrer the credit: the row still pointed at the
        address nobody would ever sign up with, so ``claim_referral_for_signup``
        found nothing at provisioning time and the referral sat at ``invited``
        forever.

        Only rows with no ``user_id`` move. A referral already attached to an
        account is settled history and is never rewritten.
        """
        old = (old_email or "").lower().strip()
        new = (new_email or "").lower().strip()
        if not old or not new or old == new:
            return 0
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE referrals SET invitee_email = ? "
                "WHERE invitee_email = ? AND user_id IS NULL",
                (new, old))
            return int(cur.rowcount or 0)

    def count_recent_referrals_for_email(self, email: str, *, hours: int = 24) -> int:
        """How many times this address has been invited recently, by ANYONE.

        Mirrors the public onboarding path's per-email pending cap (audit H4):
        without it, the same inbox can be mailed without bound by rotating the
        referring advisor, which is both a spam vector from a verified sending
        domain and a way to bury a real invite.
        """
        email = (email or "").lower().strip()
        if not email:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
                  ).replace(tzinfo=None).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM referrals WHERE invitee_email = ? "
                "AND invited_at >= ?", (email, cutoff)).fetchone()
        return int(row["n"] if row else 0)

    def has_referral_for_email(self, referrer_id: str, email: str) -> bool:
        """True when this advisor already invited this address — so a second
        click reports 'already invited' rather than minting a duplicate row."""
        email = (email or "").lower().strip()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM referrals WHERE referrer_id = ? AND invitee_email = ?",
                (referrer_id, email)).fetchone()
        return row is not None

    # The funnel, in order. Status only ever moves FORWARD: a later NPI recheck
    # or a re-onboard must never walk an 'approved' referral back to
    # 'signed_up'. NULL ("we have not heard back") sorts before 'invited'.
    _REFERRAL_LADDER = ("invited", "signed_up", "verified", "approved")

    def advance_referral(self, referral_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Move a referral forward along the funnel, or to a terminal
        'declined'. Monotonic by design — see ``_REFERRAL_LADDER``."""
        ref = self.get_referral(referral_id)
        if ref is None:
            return None
        current = ref.get("status")
        if status != "declined" and current in self._REFERRAL_LADDER and status in self._REFERRAL_LADDER:
            if self._REFERRAL_LADDER.index(status) <= self._REFERRAL_LADDER.index(current):
                return ref
        terminal = status in ("approved", "declined")
        with self._conn() as conn:
            conn.execute(
                "UPDATE referrals SET status = ?, resolved_at = CASE WHEN ? THEN ? "
                "ELSE resolved_at END WHERE referral_id = ?",
                (status, 1 if terminal else 0, _utcnow_iso(), referral_id),
            )
        return self.get_referral(referral_id)

    def claim_referral_for_signup(self, *, email: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Attach a fresh signup to the referral(s) that invited them.

        ALL open referrals for the address resolve, not just the first (audit
        M5). Two advisors can both invite the same physician — the interesting
        case, since a well-connected candidate is exactly who gets referred
        twice — and claiming only one left the other sitting at ``invited``
        forever, rendering in that advisor's funnel as still pending long after
        the person joined. Attribution is not exclusive: both advisors did in
        fact refer them, and the funnel should say so.
        """
        email = (email or "").lower().strip()
        if not email:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT referral_id FROM referrals WHERE invitee_email = ? "
                "AND user_id IS NULL ORDER BY invited_at ASC", (email,)).fetchall()
            if not rows:
                return None
            conn.execute(
                "UPDATE referrals SET user_id = ?, status = 'signed_up' "
                "WHERE invitee_email = ? AND user_id IS NULL",
                (user_id, email),
            )
        # The earliest invite is returned as "the" referral for logging; every
        # one of them is now resolved.
        return self.get_referral(rows[0]["referral_id"])

    def advance_referral_for_user(self, user_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Move EVERY referral that claimed this user along the funnel.

        Used by the verification decision points, which know a user id and not a
        referral id. Plural for the same reason ``claim_referral_for_signup`` is
        (audit M5): two advisors may have referred the same physician, and
        advancing only one leaves the other's funnel permanently wrong.
        """
        if not user_id:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT referral_id FROM referrals WHERE user_id = ? "
                "ORDER BY invited_at ASC", (user_id,)).fetchall()
        out = None
        for row in rows:
            out = self.advance_referral(row["referral_id"], status) or out
        return out

    def submissions_for_payment(self, *, limit: int = 1000) -> List[Dict[str, Any]]:
        """Completed submissions that should accrue money.

        There is NO payment feature today. This exists so that when one is
        built, the person building it finds a query that already excludes
        equity-only advisors instead of writing a fresh ``SELECT * FROM
        submissions`` that quietly bills the company for volunteer work.
        See ``asclepius/compensation.py`` — NULL is payable on purpose.
        """
        from asclepius.compensation import PAYABLE_SQL

        # LEFT, not INNER (audit M6). compensation.py argues at length that
        # under-paying a physician who did the work is worse than over-counting
        # a volunteer — and an INNER JOIN silently drops any submission whose
        # user row is missing, which is under-payment by a one-word typo. The
        # NULL a LEFT JOIN produces is then handled correctly by PAYABLE_SQL,
        # written `IS NULL OR != 'equity_only'` precisely so three-valued logic
        # cannot swallow it.
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT s.submission_id, s.evaluator_id, s.task_id, s.status, s.created_at
                FROM submissions s
                LEFT JOIN users u ON u.id = s.evaluator_id
                WHERE s.status != 'rejected' AND {PAYABLE_SQL}
                ORDER BY s.created_at DESC LIMIT ?
                """,
                (limit,)).fetchall()
        return [dict(r) for r in rows]
    # ═══ END PRD-D STORE METHODS ═══

    # ═══ PRD-I INGESTION STORE METHODS — owned by Agent I, do not edit elsewhere ═══
    # ─── Purpose (PRD-I §2) ──────────────────────────────────────────────────
    def set_upload_link_purpose(self, link_id: str, purpose: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE ingest_upload_links SET purpose = ? WHERE link_id = ?",
                         (purpose, link_id))

    def set_hs_portal_purpose(self, username: str, purpose: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE hs_portal_users SET purpose = ? WHERE username = ?",
                         (purpose, (username or "").lower()))

    def hs_portal_account_has_activity(self, username: str) -> bool:
        """Has this account sent anything, or started to?

        Purpose is resolved LIVE at completion so the two upload doors always
        agree — which also means changing an account's purpose reaches bytes that
        are already in flight. This is how a caller tells "correcting a fresh
        mis-click" (no activity) from "converting a partner's data" (any)."""
        uname = (username or "").lower()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ("
                "  (SELECT COUNT(*) FROM ingest_upload_sessions "
                "     WHERE actor = ? AND status IS NOT 'aborted') + "
                "  (SELECT COUNT(*) FROM events "
                "     WHERE actor = ? AND event_type = 'upload_received')"
                ") AS n", (uname, uname)).fetchone()
        return bool(row and int(row["n"] or 0) > 0)

    def hs_purposes_for(self, hs_id: str) -> List[Optional[str]]:
        """Every distinct purpose across a health system's ACTIVE portal accounts.

        For ADMIN DISPLAY only. Uploads are stamped from the specific account that
        sent them (``attach_upload_provenance``), never from this aggregate — an
        organization may legitimately hold one account of each kind, and picking a
        winner between them is how a brokering upload would acquire a
        task_creation stamp."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT purpose FROM hs_portal_users "
                "WHERE hs_id = ? AND active = 1", (hs_id,)).fetchall()
        return [r["purpose"] for r in rows]

    def attach_upload_provenance(
        self, upload_id: str, *, portal_username: Optional[str] = None,
        session_id: Optional[str] = None, link_id: Optional[str] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """Record where an upload came from, and copy forward what that implies.

        The caller names the ORIGIN — the portal account, the chunked session, or
        the magic link — and this resolves everything derived from it by joining
        server-side, LIVE, at this instant. Deliberately not a
        ``set_purpose(value)`` call: the upload doors have no business holding a
        purpose value, so they are given no way to express one. Nothing a provider
        sends reaches this.

        All FOUR doors resolve through this one function, which is what makes
        "the same bytes are recorded the same way whichever door they came in"
        true by construction rather than by four implementations agreeing. The
        provider-account door was the one that did not call this at all, so its
        uploads landed with purpose NULL and the gate read them as promotable."""
        now = _utcnow_iso()
        with self._conn() as conn:
            if session_id:
                # Resolved LIVE from the account the session belongs to, never
                # from a value snapshotted when the session opened. A session
                # lives 24 h and resumes across that window, so a snapshot taken
                # at declare is stale for every byte that arrives after an admin
                # corrects the mint — and the multipart door, which resolves live,
                # would record the same bytes differently. Two doors that disagree
                # about what an upload is for is the same defect class as the
                # cross-account hijack: the answer must not depend on which door
                # or which moment.
                conn.execute(
                    "UPDATE ingest_uploads SET purpose = (SELECT p.purpose FROM "
                    "hs_portal_users p JOIN ingest_upload_sessions s "
                    "ON s.actor = p.username WHERE s.session_id = ?), "
                    "updated_at = ? WHERE upload_id = ?",
                    (session_id, now, upload_id))
            elif portal_username:
                conn.execute(
                    "UPDATE ingest_uploads SET purpose = (SELECT purpose FROM "
                    "hs_portal_users WHERE username = ?), updated_at = ? "
                    "WHERE upload_id = ?",
                    ((portal_username or "").lower(), now, upload_id))
            elif link_id:
                # The magic-link door. Same shape as the other two: the caller
                # names the authorizing ROW and the value is joined here, so the
                # door itself never handles a purpose.
                conn.execute(
                    "UPDATE ingest_uploads SET purpose = (SELECT purpose FROM "
                    "ingest_upload_links WHERE link_id = ?), updated_at = ? "
                    "WHERE upload_id = ?", (link_id, now, upload_id))
            elif provider_id:
                # The data-provider account door. Same shape again — the account
                # row is the authorizing row.
                conn.execute(
                    "UPDATE ingest_uploads SET purpose = (SELECT purpose FROM "
                    "data_providers WHERE provider_id = ?), updated_at = ? "
                    "WHERE upload_id = ?", (provider_id, now, upload_id))

    def set_upload_purpose(self, upload_id: str, purpose: Optional[str]) -> None:
        """Admin-side correction only (resolving a legacy row). The upload doors
        do not call this — they call ``attach_upload_provenance``."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_uploads SET purpose = ?, updated_at = ? WHERE upload_id = ?",
                (purpose, _utcnow_iso(), upload_id))

    def set_data_provider_purpose(self, provider_id: str, purpose: Optional[str]) -> int:
        """Admin-side assignment of what a provider account's uploads are FOR.

        The provider door never sets this — it names its own account row and
        ``attach_upload_provenance`` joins the value forward (PRD-I §3.3: a door
        that can name the distinction is a door that can leak it). This is the
        admin surface that decides it, exactly like ``set_upload_purpose`` is the
        admin surface that corrects a single upload.

        Returns the number of provider rows updated (0 if there is no such
        provider), so the caller can 404 rather than report a silent success."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE data_providers SET purpose = ?, updated_at = ? WHERE provider_id = ?",
                (purpose, _utcnow_iso(), provider_id))
            return int(cur.rowcount or 0)

    def set_ingest_case_purpose(self, ingest_case_id: str, purpose: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_cases SET purpose = ?, updated_at = ? WHERE ingest_case_id = ?",
                (purpose, _utcnow_iso(), ingest_case_id))

    def propagate_purpose_to_cases(self, upload_id: str) -> int:
        """Copy the upload's purpose onto every case it produced, server-side.

        A JOIN from the authorizing row, never a value a client sent: the purpose
        is not accepted from a request body anywhere, so there is no path by which
        a provider could influence it."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE ingest_cases SET purpose = "
                "(SELECT purpose FROM ingest_uploads WHERE upload_id = ?), "
                "updated_at = ? WHERE upload_id = ?",
                (upload_id, _utcnow_iso(), upload_id))
            return int(cur.rowcount or 0)

    def ingest_case_effective_purpose(self, ingest_case_id: str) -> Optional[str]:
        """The purpose the PROMOTION GATE must read: the case's own, falling back
        to its upload's.

        The fallback is the point. Purpose is copied onto cases at the end of
        ingest, and that copy is best-effort — it must never strand an upload, so
        a failure there is logged and swallowed. Reading only the case column would
        turn that swallowed failure into a brokering case with a NULL purpose,
        which the gate resolves as task_creation. Fail-open on the one check whose
        whole job is to fail closed. COALESCE removes the possibility rather than
        relying on the copy having happened."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(c.purpose, u.purpose) AS purpose FROM ingest_cases c "
                "LEFT JOIN ingest_uploads u ON u.upload_id = c.upload_id "
                "WHERE c.ingest_case_id = ?", (ingest_case_id,)).fetchone()
        return row["purpose"] if row else None

    def ingest_case_purposes_for_upload(self, upload_id: str) -> Dict[str, Optional[str]]:
        """``{ingest_case_id: effective purpose}`` for a whole upload — one query
        for the batch promote, same COALESCE semantics as above."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT c.ingest_case_id, COALESCE(c.purpose, u.purpose) AS purpose "
                "FROM ingest_cases c LEFT JOIN ingest_uploads u "
                "ON u.upload_id = c.upload_id WHERE c.upload_id = ?",
                (upload_id,)).fetchall()
        return {r["ingest_case_id"]: r["purpose"] for r in rows}

    def mark_upload_verified(self, upload_id: str, *, verified_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingest_uploads SET verified_at = ?, updated_at = ? WHERE upload_id = ?",
                (verified_at, _utcnow_iso(), upload_id))

    # ─── Chunked upload sessions (PRD-I §1.1) ────────────────────────────────
    def create_upload_session(
        self, *, owner_kind: str, owner_id: str, actor: Optional[str],
        filename: Optional[str], content_type: Optional[str],
        declared_sha256: str, declared_size: int, chunk_size: int,
        part_count: int, storage_root: str, portal_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open a session. ``portal_username`` names the ACCOUNT that authorized it;
        anything derived from that account is resolved here by a server-side join,
        so the upload door never handles the derived value (PRD-I §3.1)."""
        sid = _new_id("ups")
        now = _utcnow_iso()
        # Derived from the SERVER-minted session id, so no component of the parts
        # directory is ever client-controlled.
        storage_dir = os.path.join(storage_root, sid)
        # ``actor`` is the ONLY record of who authorized this session, and it is
        # what everything derived is joined through at completion. Nothing is
        # snapshotted here: a session lives 24 h and a stored copy of a mutable
        # admin decision is stale the moment the admin changes it.
        actor = (portal_username or actor or "").lower() or None
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO ingest_upload_sessions
                       (session_id, owner_kind, owner_id, actor, filename, content_type,
                        declared_sha256, declared_size, chunk_size, part_count,
                        storage_dir, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sid, owner_kind, owner_id, actor, filename, content_type,
                     declared_sha256, int(declared_size), int(chunk_size),
                     int(part_count), storage_dir, now, now))
        except sqlite3.IntegrityError:
            # Two declares for the same bytes raced. The idempotency index did its
            # job — return the session that won rather than surfacing a 500 for
            # what is, from the partner's side, one upload they asked for twice.
            existing = self.find_open_upload_session(
                owner_kind=owner_kind, owner_id=owner_id, actor=actor,
                declared_sha256=declared_sha256, declared_size=int(declared_size))
            if existing:
                return existing
            raise
        return self.get_upload_session(sid)  # type: ignore[return-value]

    def get_upload_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_upload_sessions WHERE session_id = ?",
                (session_id,)).fetchone()
        return dict(row) if row else None

    def find_open_upload_session(
        self, *, owner_kind: str, owner_id: str, actor: Optional[str],
        declared_sha256: str, declared_size: int,
    ) -> Optional[Dict[str, Any]]:
        """The idempotency lookup. Matches an OPEN session first (a resume), then a
        VERIFIED one (a duplicate declare of bytes already ingested — returning the
        existing upload_id is both idempotent and the only answer that does not
        ingest the same content twice).

        Scoped to the ACCOUNT, not just the health system. See the note on
        ``idx_ingest_sessions_idem_v2``: purpose lives on the account, so matching
        on the organization alone would hand a brokering account a task-creation
        account's session and let the completed upload inherit its purpose."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_upload_sessions "
                "WHERE owner_kind = ? AND owner_id = ? AND actor IS ? "
                "AND declared_sha256 = ? "
                "AND declared_size = ? AND (status IS NULL OR status = 'verified') "
                "ORDER BY CASE WHEN status IS NULL THEN 0 ELSE 1 END, created_at DESC "
                "LIMIT 1",
                (owner_kind, owner_id, actor, declared_sha256,
                 int(declared_size))).fetchone()
        return dict(row) if row else None

    def claim_upload_session_for_completion(self, session_id: str) -> bool:
        """ATOMIC claim on the assembly step. True for exactly one caller.

        Two concurrent ``complete`` calls on one session would otherwise both pass
        the "is it still open" read and both assemble: two ``ingest_uploads`` rows
        and two pipeline runs for the same bytes, and — because the winner's
        ``finalize`` deletes the parts while the loser is still reading them — an
        unhandled FileNotFoundError from inside the loser. A conditional UPDATE
        settles it in one statement, the same pattern ``consume_upload_link``
        already uses for the one-time link race."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE ingest_upload_sessions SET status = 'completing', "
                "updated_at = ? WHERE session_id = ? AND status IS NULL",
                (_utcnow_iso(), session_id))
            return cur.rowcount == 1

    def release_upload_session_claim(self, session_id: str) -> str:
        """Hand a claimed session back after a failed assembly, so the partner can
        retry rather than being locked out by our own crash.

        Returns ``"open"`` or ``"aborted"``. **Never raises** — this is called from
        inside an ``except`` block, so an exception here would replace the real
        assembly failure with a database error the operator cannot act on.

        The subtlety: the idempotency index is partial over ``status IS NULL``, and
        a partner who gives up on a stuck assembly re-declares — producing a fresh
        OPEN session for the same bytes. Setting the stuck one back to NULL then
        collides with the live one. When that happens the stuck session is
        genuinely obsolete (its replacement already exists and is being uploaded
        to), so it is retired instead. Blindly retrying the NULL write is what let
        the reaper abort the live retry and leave the stuck one behind."""
        now = _utcnow_iso()
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE ingest_upload_sessions SET status = NULL, updated_at = ? "
                    "WHERE session_id = ? AND status = 'completing'", (now, session_id))
            return "open"
        except sqlite3.IntegrityError:
            pass
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE ingest_upload_sessions SET status = 'aborted', "
                    "updated_at = ? WHERE session_id = ? AND status = 'completing'",
                    (now, session_id))
        except sqlite3.Error:  # pragma: no cover - defensive; never mask the caller
            return "open"
        return "aborted"

    def update_upload_session(self, session_id: str, **fields: Any) -> None:
        allowed = {"status", "upload_id", "verified_at"}
        # 'completing' is a claim, not a terminal state — release_upload_session_claim
        # is the only way back to NULL.
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(v)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_utcnow_iso(), session_id])
        with self._conn() as conn:
            conn.execute(
                f"UPDATE ingest_upload_sessions SET {', '.join(sets)} "
                "WHERE session_id = ?", tuple(params))

    def list_stale_upload_sessions(self, *, older_than_iso: str) -> List[Dict[str, Any]]:
        """Sessions past the reaper cutoff (PRD-I §1.1: unverified parts are deleted
        after 24 h).

        Four states, three reasons:

        * ``NULL`` — open and possibly abandoned; parts are deleted if idle.
        * ``completing`` — a claim that outlived the process that took it. A hard
          crash during assembly would otherwise lock a partner out of an upload
          they could still finish, so the reaper hands these back.
        * ``aborted`` / ``failed`` — already retired, but their parts may still be
          on disk if whichever path retired them did not purge. Included so part
          cleanup CONVERGES rather than depending on every caller remembering;
          the reaper only releases their disk, never touches the row.

        ``verified`` is never a candidate: its parts are gone and its row is
        chain-of-custody history."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ingest_upload_sessions "
                "WHERE (status IS NULL OR status IN ('completing', 'aborted', 'failed')) "
                "AND updated_at < ?",
                (older_than_iso,)).fetchall()
        return [dict(r) for r in rows]

    def hs_uploads_bytes_in_open_sessions(self, hs_id: str) -> int:
        """Bytes already committed to open sessions for this health system. Counted
        against the quota at DECLARE time — otherwise a partner could declare
        unlimited concurrent multi-GB sessions and only trip the quota at complete,
        after the disk was already spent."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(declared_size), 0) FROM ingest_upload_sessions "
                "WHERE owner_kind = 'health_system' AND owner_id = ? AND status IS NULL",
                (hs_id,)).fetchone()
        return int(row[0] or 0)
    # ═══ END PRD-I STORE METHODS ═══
    # ═══ PRD-CRED TIERING STORE METHODS — owned by Agent C, do not edit from other PRDs ═══

    # ─── Weights ──────────────────────────────────────────────────────────────
    def get_tiering_weights(self) -> Dict[str, Dict[str, float]]:
        """The current posterior, seeded from the priors on first read.

        Seeding on read rather than in ``_init_schema`` keeps the priors in
        ``tiering.py`` where they are documented and reviewable, instead of
        duplicating nine numbers into a migration where they would drift.
        """
        from asclepius import tiering

        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM tiering_weights").fetchall()
            existing = {r["feature"]: {"m": float(r["m"]), "q": float(r["q"]),
                                       "pinned": int(r["pinned"] or 0)} for r in rows}
            defaults = tiering.default_weights()
            missing = {k: v for k, v in defaults.items() if k not in existing}
            if missing:
                now = _utcnow_iso()
                conn.executemany(
                    "INSERT OR IGNORE INTO tiering_weights (feature, m, q, pinned, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(k, v["m"], v["q"], int(v["pinned"]), now) for k, v in missing.items()],
                )
                existing.update({k: dict(v) for k, v in missing.items()})
        return existing

    def record_tiering_decision(
        self,
        *,
        user_id: str,
        case_domain: Optional[str],
        features: Dict[str, float],
        proposed_tier: Optional[str],
        admin_tier: Optional[str] = None,
        was_exploration: bool = False,
        outcome_source: str = "admin",
        score: Optional[float] = None,
        decided_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """One row per decision the model made or a human overrode.

        ``was_flip`` is computed here rather than passed in: it is a fact about the two
        columns beside it, and a caller that computes it separately is a caller that can get
        it wrong. NULL when there is no admin decision yet — "not yet decided" is not "the
        admin agreed".
        """
        decision_id = _new_id("tdec")
        flip: Optional[int] = None
        if admin_tier:
            flip = 1 if admin_tier != proposed_tier else 0
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tiering_decisions (decision_id, user_id, case_domain, "
                "features_json, proposed_tier, admin_tier, was_flip, was_exploration, "
                "outcome_source, score, decided_by, decided_at, applied_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (decision_id, user_id, case_domain, json.dumps(features), proposed_tier,
                 admin_tier, flip, 1 if was_exploration else 0, outcome_source, score,
                 decided_by, _utcnow_iso()),
            )
        return self.get_tiering_decision(decision_id) or {"decision_id": decision_id}

    def get_tiering_decision(self, decision_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tiering_decisions WHERE decision_id = ?",
                               (decision_id,)).fetchone()
        return dict(row) if row else None

    def pending_tiering_decisions(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        """The un-applied batch. ``applied_at IS NULL`` is the whole guard against replay —
        rows carrying no admin decision yet are excluded because an unlabelled row is not an
        observation."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tiering_decisions WHERE applied_at IS NULL "
                "AND admin_tier IS NOT NULL ORDER BY decided_at ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_unapplied_tiering_decision(self, user_id: str, admin_tier: str) -> bool:
        """Is this exact judgment already queued as an un-folded observation?

        Admin Launch PRD §3.3 has the console POST ``/tiering/{id}/decide`` and
        then ``/approve``. ``approve`` records a tiering decision of its own, so
        without this check one admin click would enter the training set TWICE —
        the same physician, the same features, the same tier, counted as two
        independent observations. ``apply_decision_batch`` would then fold a
        doubled likelihood and the console's "N decisions until the next weight
        update" would advance two per approval. Both are wrong, and both are
        invisible from the outside.

        Scoped to ``applied_at IS NULL`` deliberately: once a batch is folded in,
        a later re-decision on the same physician IS a new observation.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM tiering_decisions WHERE user_id = ? AND admin_tier = ? "
                "AND applied_at IS NULL LIMIT 1", (user_id, admin_tier)).fetchone()
        return row is not None

    def apply_tiering_batch(self, decision_ids: List[str],
                            *, weights: Optional[Dict[str, Dict[str, float]]]) -> int:
        """Stamp ``applied_at`` and write the new weights in ONE transaction.

        Atomicity is the point. Written as two statements, a crash between them either
        double-counts a batch (fabricated confidence) or loses it (silent non-learning), and
        both failures look exactly like a healthy system from the outside.
        """
        if not decision_ids and weights is None:
            return 0
        now = _utcnow_iso()
        with self._conn() as conn:
            if weights:
                conn.executemany(
                    "INSERT INTO tiering_weights (feature, m, q, pinned, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(feature) DO UPDATE SET "
                    "m = excluded.m, q = excluded.q, pinned = excluded.pinned, "
                    "updated_at = excluded.updated_at",
                    [(k, float(v["m"]), float(v["q"]), int(v.get("pinned") or 0), now)
                     for k, v in weights.items()],
                )
            if decision_ids:
                marks = ",".join("?" for _ in decision_ids)
                conn.execute(
                    f"UPDATE tiering_decisions SET applied_at = ? "
                    f"WHERE applied_at IS NULL AND decision_id IN ({marks})",
                    [now, *decision_ids],
                )
        return len(decision_ids)

    def tiering_decision_history(self, *, user_id: Optional[str] = None,
                                 limit: int = 100) -> List[Dict[str, Any]]:
        clause, params = "", []
        if user_id:
            clause, params = "WHERE user_id = ?", [user_id]
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM tiering_decisions {clause} ORDER BY decided_at DESC LIMIT ?",
                [*params, max(1, int(limit))],
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Calibration ──────────────────────────────────────────────────────────
    @staticmethod
    def _calibration_item_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["vignette"] = json.loads(rec.pop("vignette_json", "{}") or "{}")
        rec["key"] = json.loads(rec.pop("key_json", "{}") or "{}")
        return rec

    def upsert_calibration_item(self, *, specialty: str, vignette: Dict[str, Any],
                                key: Dict[str, Any], source_task_id: Optional[str] = None,
                                item_id: Optional[str] = None,
                                active: bool = True) -> Dict[str, Any]:
        item_id = item_id or _new_id("cal")
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO calibration_items (item_id, specialty, source_task_id, "
                "vignette_json, key_json, panel_n, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET "
                "vignette_json = excluded.vignette_json, key_json = excluded.key_json, "
                "panel_n = excluded.panel_n, active = excluded.active, "
                "updated_at = excluded.updated_at",
                (item_id, (specialty or "").strip().lower(), source_task_id,
                 json.dumps(vignette), json.dumps(key), int(key.get("panel_n") or 0),
                 1 if active else 0, now, now),
            )
            row = conn.execute("SELECT * FROM calibration_items WHERE item_id = ?",
                               (item_id,)).fetchone()
        return self._calibration_item_row(row)

    def list_calibration_items(self, *, specialty: Optional[str] = None,
                               active_only: bool = True) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if specialty:
            clauses.append("specialty = ?")
            params.append((specialty or "").strip().lower())
        if active_only:
            clauses.append("active = 1")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM calibration_items {where} ORDER BY created_at ASC",
                params).fetchall()
        return [self._calibration_item_row(r) for r in rows]

    def get_calibration_items(self, item_ids: List[str]) -> List[Dict[str, Any]]:
        if not item_ids:
            return []
        marks = ",".join("?" for _ in item_ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM calibration_items WHERE item_id IN ({marks})", item_ids
            ).fetchall()
        by_id = {r["item_id"]: self._calibration_item_row(r) for r in rows}
        return [by_id[i] for i in item_ids if i in by_id]

    def start_calibration_attempt(self, *, user_id: str, specialty: str,
                                  item_ids: List[str]) -> Dict[str, Any]:
        attempt_id = _new_id("catt")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO calibration_attempts (attempt_id, user_id, specialty, "
                "item_ids_json, started_at) VALUES (?, ?, ?, ?, ?)",
                (attempt_id, user_id, (specialty or "").strip().lower(),
                 json.dumps(item_ids), _utcnow_iso()),
            )
        return {"attempt_id": attempt_id, "user_id": user_id, "specialty": specialty,
                "item_ids": item_ids}

    @staticmethod
    def _calibration_attempt_row(row: sqlite3.Row) -> Dict[str, Any]:
        rec = dict(row)
        rec["item_ids"] = json.loads(rec.pop("item_ids_json", "[]") or "[]")
        rec["responses"] = json.loads(rec.pop("responses_json", "null") or "null") or {}
        rec["scores"] = json.loads(rec.pop("scores_json", "null") or "null")
        # Tri-state preserved on the way out: NULL ("not yet graded") must not become False.
        for gate in ("tr_gate_passed", "tl_gate_passed"):
            rec[gate] = None if rec.get(gate) is None else bool(rec[gate])
        return rec

    def get_calibration_attempt(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM calibration_attempts WHERE attempt_id = ?",
                               (attempt_id,)).fetchone()
        return self._calibration_attempt_row(row) if row else None

    def record_calibration_responses(self, attempt_id: str,
                                     responses: Dict[str, Any]) -> None:
        """The RAW responses, exactly as submitted. Never the graded form — see PRD C §4."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE calibration_attempts SET responses_json = ?, submitted_at = ? "
                "WHERE attempt_id = ?",
                (json.dumps(responses), _utcnow_iso(), attempt_id),
            )

    def record_calibration_score(self, attempt_id: str, result: Dict[str, Any],
                                 *, rescored: bool = False) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE calibration_attempts SET scores_json = ?, composite = ?, "
                "tr_gate_passed = ?, tl_gate_passed = ?, rescored_at = COALESCE(?, "
                "rescored_at) WHERE attempt_id = ?",
                (json.dumps(result), result.get("composite"),
                 1 if result.get("tr_gate_passed") else 0,
                 1 if result.get("tl_gate_passed") else 0,
                 _utcnow_iso() if rescored else None, attempt_id),
            )

    def latest_calibration_for_user(self, user_id: str,
                                    specialty: Optional[str] = None) -> Optional[Dict[str, Any]]:
        clauses, params = ["user_id = ?", "submitted_at IS NOT NULL"], [user_id]
        if specialty:
            clauses.append("specialty = ?")
            params.append((specialty or "").strip().lower())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM calibration_attempts WHERE " + " AND ".join(clauses) +
                " ORDER BY submitted_at DESC LIMIT 1", params
            ).fetchone()
        return self._calibration_attempt_row(row) if row else None

    def calibration_attempts_for_user(self, user_id: str,
                                      specialty: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = ["user_id = ?"], [user_id]
        if specialty:
            clauses.append("specialty = ?")
            params.append((specialty or "").strip().lower())
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM calibration_attempts WHERE " + " AND ".join(clauses) +
                " ORDER BY started_at ASC", params).fetchall()
        return [self._calibration_attempt_row(r) for r in rows]

    def open_calibration_attempt(self, user_id: str,
                                 specialty: str) -> Optional[Dict[str, Any]]:
        """An attempt this candidate started and never submitted.

        Returned instead of minting a second one, so a reload or a dropped connection does not
        silently spend one of two attempts — and so a candidate cannot reroll the item sample
        by refreshing until an easier draw comes up.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM calibration_attempts WHERE user_id = ? AND specialty = ? "
                "AND submitted_at IS NULL ORDER BY started_at DESC LIMIT 1",
                (user_id, (specialty or "").strip().lower())).fetchone()
        return self._calibration_attempt_row(row) if row else None

    def calibration_population(self, specialty: str) -> List[float]:
        """Every graded composite in a specialty — the population ``calibration_z``
        standardizes against once there are enough of them to mean anything."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT composite FROM calibration_attempts WHERE specialty = ? "
                "AND composite IS NOT NULL", ((specialty or "").strip().lower(),)
            ).fetchall()
        return [float(r["composite"]) for r in rows]

    # ─── OIG LEIE (gate A5) ───────────────────────────────────────────────────
    def replace_leie_exclusions(self, rows: List[Dict[str, Any]],
                                *, source_note: Optional[str] = None) -> int:
        """Swap in a freshly downloaded LEIE snapshot, atomically.

        DELETE-then-INSERT inside one transaction, so a reader never observes an empty table
        and concludes that every physician is clear. That window is the difference between a
        hard gate and a formality.
        """
        now = _utcnow_iso()
        with self._conn() as conn:
            conn.execute("DELETE FROM leie_exclusions")
            conn.executemany(
                "INSERT OR REPLACE INTO leie_exclusions (npi, excl_type, excl_date, loaded_at) "
                "VALUES (?, ?, ?, ?)",
                [(str(r.get("npi") or "").strip(), r.get("excl_type"), r.get("excl_date"), now)
                 for r in rows if str(r.get("npi") or "").strip()],
            )
            n = conn.execute("SELECT COUNT(*) AS n FROM leie_exclusions").fetchone()["n"]
            conn.execute(
                "INSERT INTO leie_meta (id, loaded_at, row_count, source_note) "
                "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET loaded_at = excluded.loaded_at, "
                "row_count = excluded.row_count, source_note = excluded.source_note",
                (now, int(n), source_note),
            )
        return int(n)

    def leie_loaded_at(self) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT loaded_at, row_count FROM leie_meta WHERE id = 1"
                               ).fetchone()
        return row["loaded_at"] if row else None

    def leie_status(self, npi: str) -> str:
        """``excluded`` | ``clear`` | ``unknown``.

        ``unknown`` when no snapshot has ever been loaded. A never-loaded exclusion list must
        not answer "clear" — that is a check that fails open, which is not a check. Gate A5
        reads this and routes UNKNOWN to the admin, exactly as an unreachable NPPES does.
        """
        npi = (npi or "").strip()
        if not npi:
            return "unknown"
        if not self.leie_loaded_at():
            return "unknown"
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM leie_exclusions WHERE npi = ?", (npi,)).fetchone()
        return "excluded" if row else "clear"

    # ─── Fairness monitor (PRD C §6) ──────────────────────────────────────────
    @staticmethod
    def _fairness_subject_key(user_id: str) -> str:
        """A per-purpose pseudonym, not the user id.

        The demographics table must not be joinable into the feature store. Storing
        ``user_id`` would make that a naming convention enforced by good intentions; an HMAC
        under a secret the scorer never reads makes it a property of the data. The monitor
        needs no join because the decided tier is copied onto the row at decision time.
        """
        secret = (os.getenv("ASCLEPIUS_FAIRNESS_SALT")
                  or os.getenv("ASCLEPIUS_AUTH_SECRET") or "asclepius-fairness")
        return hashlib.blake2b(f"{user_id}".encode("utf-8"),
                               key=secret.encode("utf-8")[:64], digest_size=16).hexdigest()

    def record_fairness_observation(self, *, user_id: str, demographics: Dict[str, Any],
                                    decided_tier: Optional[str] = None) -> Optional[str]:
        """Voluntary and self-reported, written STRAIGHT here at signup.

        Deliberately not a column on ``users``. Every feature path loads a physician with
        ``SELECT * FROM users``, so a demographics column there would be a column the model
        can reach — and "we remembered not to read it" is not the guarantee §6 asks for. No
        demographics ⇒ no row, never an inferred one.

        One row per subject: re-submitting replaces the answers rather than accumulating
        duplicates that would each be counted by the monitor.
        """
        if not demographics:
            return None
        key = self._fairness_subject_key(user_id)
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT obs_id, decided_tier FROM fairness_observations WHERE subject_key = ?",
                (key,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE fairness_observations SET demographics_json = ?, "
                    "decided_tier = COALESCE(?, decided_tier) WHERE obs_id = ?",
                    (json.dumps(demographics), decided_tier, existing["obs_id"]))
                return existing["obs_id"]
            obs_id = _new_id("fair")
            conn.execute(
                "INSERT INTO fairness_observations (obs_id, subject_key, demographics_json, "
                "decided_tier, decided_at) VALUES (?, ?, ?, ?, ?)",
                (obs_id, key, json.dumps(demographics), decided_tier, _utcnow_iso()),
            )
        return obs_id

    def stamp_fairness_tier(self, user_id: str, tier: Optional[str],
                            features: Optional[Dict[str, Any]] = None) -> None:
        """Copy the decided tier — and the feature vector — onto the pseudonymous row, so the
        monitor needs no join.

        A no-op when the physician declined to supply demographics, which is most of them and
        is fine: the four-fifths rule is a rate comparison, not a census.

        ``features`` (AUDIT H2) is what lets the monitor report *why* a group's selection rate
        differs, not only *that* it does. An outcome monitor with no view of the mechanism can
        tell you a gap exists and never which feature opened it.
        """
        payload = None
        if features:
            payload = json.dumps({k: float(v) for k, v in features.items()
                                  if isinstance(v, (int, float))
                                  and not isinstance(v, bool)})
        with self._conn() as conn:
            conn.execute(
                "UPDATE fairness_observations SET decided_tier = ?, "
                "features_json = COALESCE(?, features_json) WHERE subject_key = ?",
                (tier, payload, self._fairness_subject_key(user_id)))

    def fairness_selection_rates(self, *, since: Optional[str] = None) -> Dict[str, Any]:
        """TR selection rate by self-reported group, with the four-fifths comparison.

        The four-fifths (80%) rule: if any group's selection rate falls below 80% of the
        highest group's, that is the EEOC's rule-of-thumb threshold for adverse impact. It is
        a screening signal, not a verdict — small groups swing wildly, so ``n`` is reported
        beside every rate and a group under ``MIN_GROUP_N`` is listed but never triggers the
        alert on its own.
        """
        MIN_GROUP_N = 5
        rows = self.fairness_observations(since=since)
        buckets: Dict[str, Dict[str, Dict[str, int]]] = {}
        # AUDIT H2: feature means per group, so the monitor can name the MECHANISM and not
        # only the outcome. {dimension: {group: {feature: [sum, n]}}}
        feats: Dict[str, Dict[str, Dict[str, List[float]]]] = {}
        for row in rows:
            if not row.get("decided_tier"):
                continue        # not yet decided is not a negative outcome
            for dimension, value in (row.get("demographics") or {}).items():
                if value in (None, "", "prefer_not_to_say"):
                    continue
                slot = buckets.setdefault(dimension, {}).setdefault(
                    str(value), {"n": 0, "tr": 0})
                slot["n"] += 1
                slot["tr"] += 1 if row["decided_tier"] == "reviewer" else 0
                fslot = feats.setdefault(dimension, {}).setdefault(str(value), {})
                for feature, fval in (row.get("features") or {}).items():
                    if not isinstance(fval, (int, float)):
                        continue
                    acc = fslot.setdefault(feature, [0.0, 0.0])
                    acc[0] += float(fval)
                    acc[1] += 1.0

        out: Dict[str, Any] = {"dimensions": {}, "alerts": [], "min_group_n": MIN_GROUP_N,
                               "by_feature": {}, "feature_alerts": []}
        for dimension, groups in buckets.items():
            rates = {g: (v["tr"] / v["n"] if v["n"] else 0.0) for g, v in groups.items()}
            eligible = {g: r for g, r in rates.items() if groups[g]["n"] >= MIN_GROUP_N}
            best = max(eligible.values(), default=0.0)
            detail = {}
            for g, v in groups.items():
                ratio = (rates[g] / best) if best > 0 else None
                detail[g] = {"n": v["n"], "tr": v["tr"], "rate": round(rates[g], 4),
                             "impact_ratio": None if ratio is None else round(ratio, 4),
                             "counted": groups[g]["n"] >= MIN_GROUP_N}
                if detail[g]["counted"] and ratio is not None and ratio < 0.8:
                    out["alerts"].append(
                        {"dimension": dimension, "group": g, "impact_ratio": round(ratio, 4),
                         "n": v["n"],
                         "message": f"{dimension}={g} TR selection rate is "
                                    f"{round(ratio * 100)}% of the highest group — below the "
                                    f"four-fifths threshold."})
            out["dimensions"][dimension] = detail

        # AUDIT H2 — per-feature breakdown, and an alert on the same four-fifths shape.
        #
        # Applied to the feature MEAN rather than a selection rate, because that is what
        # answers the question the audit actually asked: is a demographic-adjacent feature
        # doing the work that the pinned weights were meant to prevent? A feature whose mean
        # is level across groups cannot be routing around a pin, however heavy its weight —
        # and a monitor that flags every feature is a monitor nobody reads.
        for dimension, groups in feats.items():
            names = sorted({f for g in groups.values() for f in g})
            per_group: Dict[str, Dict[str, Any]] = {}
            for group, acc in groups.items():
                per_group[group] = {
                    f: {"mean": round(acc[f][0] / acc[f][1], 4), "n": int(acc[f][1])}
                    for f in acc if acc[f][1]
                }
            out["by_feature"][dimension] = per_group
            counted = {g for g in groups
                       if (buckets.get(dimension, {}).get(g, {}).get("n", 0)) >= MIN_GROUP_N}
            for feature in names:
                means = {g: per_group[g][feature]["mean"] for g in counted
                         if feature in per_group.get(g, {})}
                if len(means) < 2:
                    continue
                best = max(means.values())
                worst_group = min(means, key=lambda g: means[g])
                if best <= 0:
                    continue
                ratio = means[worst_group] / best
                if ratio < 0.8:
                    out["feature_alerts"].append({
                        "dimension": dimension, "feature": feature, "group": worst_group,
                        "mean": means[worst_group], "best_mean": round(best, 4),
                        "ratio": round(ratio, 4),
                        "message": f"{feature} averages {means[worst_group]} for "
                                   f"{dimension}={worst_group} vs {round(best, 4)} for the "
                                   f"highest group — this feature may be carrying a "
                                   f"group difference into the score.",
                    })
        out["total_observations"] = len(rows)
        return out

    def fairness_observations(self, *, since: Optional[str] = None) -> List[Dict[str, Any]]:
        clause, params = "", []
        if since:
            clause, params = "WHERE decided_at >= ?", [since]
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM fairness_observations {clause} ORDER BY decided_at DESC",
                params).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            rec["demographics"] = json.loads(rec.pop("demographics_json", "{}") or "{}")
            rec["features"] = json.loads(rec.pop("features_json", "null") or "null") or {}
            rec.pop("subject_key", None)   # never leaves the store
            out.append(rec)
        return out

    # ─── Measured quality inputs (PRD C §5.4) ─────────────────────────────────
    def completed_task_count(self, user_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE evaluator_id = ? "
                "AND status != 'rejected'", (user_id,)).fetchone()
        return int(row["n"] if row else 0)

    def paired_label_observations(self, *, specialty: Optional[str] = None,
                                  limit: int = 5000) -> Dict[str, Any]:
        """``(task_id, labeler_id, chosen_id)`` for every task carrying at least two
        independent labels, plus the TR adjudication as near-gold where one exists.

        This is exactly the input One-Coin Dawid–Skene needs. The structure is Agent R's; this
        only reads it, and degrades to an empty result rather than failing when the paired path
        has not produced anything yet — a physician with no measured work is not a physician
        with measured-zero work.
        """
        params: List[Any] = []
        spec = ""
        if specialty:
            spec = "AND t.specialty = ?"
            params.append((specialty or "").strip().lower())
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT s.task_id, s.evaluator_id, s.chosen_id, s.submission_id
                FROM submissions s JOIN tasks t ON t.task_id = s.task_id
                WHERE s.status != 'rejected' AND s.chosen_id IS NOT NULL {spec}
                  AND s.task_id IN (
                      SELECT task_id FROM submissions WHERE status != 'rejected'
                        AND chosen_id IS NOT NULL
                      GROUP BY task_id HAVING COUNT(DISTINCT evaluator_id) >= 2)
                ORDER BY s.created_at ASC LIMIT ?
                """, [*params, int(limit)]).fetchall()
            observations = [(r["task_id"], r["evaluator_id"], r["chosen_id"]) for r in rows]
            gold: Dict[str, str] = {}
            if observations:
                task_ids = sorted({o[0] for o in observations})
                marks = ",".join("?" for _ in task_ids)
                grows = conn.execute(
                    f"""SELECT cr.task_id, s.chosen_id, cr.verdict FROM case_reviews cr
                        JOIN submissions s ON s.submission_id = cr.submission_id
                        WHERE cr.task_id IN ({marks}) AND cr.verdict IS NOT NULL
                        ORDER BY cr.created_at ASC""", task_ids).fetchall()
                for r in grows:
                    # NOT-REJECTED, deliberately — not "accepted" (context pack Seam 3).
                    # ``agreement.review_acceptance`` is the single definition of expert
                    # acceptance, and re-deriving 'accept OR accept_with_edits' here would be
                    # exactly the rival number that once had the dashboard reading 97% while
                    # quality_report.md read 84%. What Dawid–Skene needs as a near-gold label
                    # is weaker and different: a TR who did not reject a submission has
                    # endorsed its chosen answer well enough to anchor the EM. First
                    # non-rejection wins; later reviews cannot flip it.
                    if r["verdict"] == "reject":
                        continue
                    gold.setdefault(r["task_id"], r["chosen_id"])
        return {"observations": observations, "gold": gold}
    # ═══ END PRD-CRED STORE METHODS ═══
    # ═══ PRD-P PAYMENT STORE METHODS — owned by Agent P, do not edit from other PRDs ═══
    # Persistence only. Every policy number (rate, threshold, gap tolerances) and
    # every arithmetic decision lives in ``asclepius/payments.py`` — this block
    # owns the transaction boundary and nothing else. The two methods that mutate
    # a session take a ``credit_fn`` so the arithmetic runs against rows read
    # INSIDE the write transaction: computing outside it and writing after would
    # let a beat land in between and silently pay the wrong number.

    @staticmethod
    def _immediate(conn: sqlite3.Connection) -> None:
        """Take SQLite's single write lock at transaction START.

        ``sqlite3`` defaults to a DEFERRED transaction, which acquires a SHARED
        lock on first read and tries to UPGRADE on first write. Two finalizers
        that both read first can then deadlock on the upgrade and one gets
        SQLITE_BUSY — under a busy_timeout of 30 s that is a 30-second hang on a
        payout. BEGIN IMMEDIATE takes the write lock up front, so the second
        caller waits cleanly at the door instead."""
        conn.isolation_level = None      # we drive BEGIN/COMMIT ourselves
        conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _session_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def get_work_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM work_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return self._session_row(row)

    def open_work_session_row(
        self, *, user_id: str, kind: str
    ) -> Optional[Dict[str, Any]]:
        """The one open (never-ended) session for this user+kind, if any.

        ``ended_at IS NULL`` is the open predicate — which is why ``end_reason``
        carries no DEFAULT. Ordered newest-first so that if a historic bug ever
        left two open, the caller sees the live one; ``open_session`` closes the
        stragglers rather than pretending they are not there."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM work_sessions WHERE user_id = ? AND kind = ? "
                "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
                (user_id, kind)).fetchone()
        return self._session_row(row)

    def list_open_work_sessions(self, *, user_id: str, kind: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM work_sessions WHERE user_id = ? AND kind = ? "
                "AND ended_at IS NULL ORDER BY started_at ASC", (user_id, kind)).fetchall()
        return [dict(r) for r in rows]

    def insert_work_session(
        self, *, session_id: str, user_id: str, kind: str, started_at: str,
        nonce: str, min_seconds: int, rate_cents: int,
    ) -> Dict[str, Any]:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO work_sessions
                    (session_id, user_id, kind, started_at, last_beat_at, ended_at,
                     end_reason, credited_seconds, qualified, nonce, min_seconds,
                     rate_cents, continuous_seconds, jitter_ms, clock_skew_beats)
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, 0, NULL, ?, ?, ?, 0, NULL, 0)
                """,
                (session_id, user_id, kind, started_at, nonce, min_seconds, rate_cents),
            )
        return self.get_work_session(session_id)  # type: ignore[return-value]

    def rotate_session_nonce(self, *, session_id: str, nonce: str) -> bool:
        """Mint a fresh nonce onto an OPEN session and count the resume.

        Separate from ``record_session_beat`` because resuming is not beating: it
        credits no time and is rate-limited far harder. The ``ended_at IS NULL``
        predicate lives in the UPDATE rather than in a preceding read, so a
        session that closed underneath the caller cannot be handed a live nonce by
        a racing resume."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE work_sessions SET nonce = ?, resume_count = resume_count + 1 "
                "WHERE session_id = ? AND ended_at IS NULL",
                (nonce, session_id))
            return cur.rowcount > 0

    def session_beats(self, session_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_beats WHERE session_id = ? ORDER BY seq ASC",
                (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def record_session_beat(
        self, *, session_id: str, nonce: str, seq: int, active: bool,
        progress_key: Optional[str], client_ts: Optional[str], server_ts: str,
        next_nonce: str, credit_fn, beat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate → append → recompute → rotate the nonce, in ONE transaction.

        Returns ``{"ok": bool, "error": str|None, ...credit fields}``. The three
        rejections (session ended, stale nonce, replayed seq) are returned rather
        than raised so the router decides the HTTP shape.

        Splitting this across transactions is the whole vulnerability: two
        concurrent beats presenting the same nonce would both validate against
        the pre-rotation value and both be accepted. Under BEGIN IMMEDIATE the
        second waits for the first to rotate, then fails the nonce check — which
        is exactly what the rotating nonce is for."""
        conn = self._conn()
        try:
            self._immediate(conn)
            row = conn.execute(
                "SELECT * FROM work_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return {"ok": False, "error": "not_found"}
            session = dict(row)
            if session.get("ended_at"):
                conn.execute("ROLLBACK")
                return {"ok": False, "error": "ended", "session": session}
            if nonce != session.get("nonce"):
                conn.execute("ROLLBACK")
                return {"ok": False, "error": "stale_nonce", "session": session}
            last_seq = conn.execute(
                "SELECT MAX(seq) AS m FROM session_beats WHERE session_id = ?",
                (session_id,)).fetchone()["m"]
            if last_seq is not None and seq <= int(last_seq):
                conn.execute("ROLLBACK")
                return {"ok": False, "error": "replayed_seq", "session": session}
            conn.execute(
                "INSERT INTO session_beats "
                "(beat_id, session_id, server_ts, seq, active, progress_key, client_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (beat_id or _new_id("beat"), session_id, server_ts, int(seq),
                 1 if active else 0, progress_key, client_ts),
            )
            beats = [dict(r) for r in conn.execute(
                "SELECT * FROM session_beats WHERE session_id = ? ORDER BY seq ASC",
                (session_id,)).fetchall()]
            credit = credit_fn(beats, session)
            conn.execute(
                "UPDATE work_sessions SET last_beat_at = ?, credited_seconds = ?, "
                "continuous_seconds = ?, jitter_ms = ?, clock_skew_beats = ?, "
                "distinct_progress_keys = ?, nonce = ? WHERE session_id = ?",
                (server_ts, int(credit["credited_seconds"]), int(credit["continuous_seconds"]),
                 credit.get("jitter_ms"),
                 int(session.get("clock_skew_beats") or 0) + (1 if credit.get("clock_skew") else 0),
                 int(credit.get("distinct_progress_keys") or 0),
                 next_nonce, session_id),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        # ``next_nonce`` is returned from INSIDE the committed transaction rather
        # than re-read afterwards: a re-read could observe a nonce a concurrent
        # beat had already rotated past, and hand the client one that is stale
        # before it is ever used.
        # ``seq`` goes back to the client so a tab that RESUMED a session — and so
        # let the server derive the first sequence number — learns where the
        # sequence is and can number its own beats from here. Without it, such a
        # tab would defer to the server forever and permanently give up the
        # replay guard's depth behind the rotating nonce.
        return {"ok": True, "error": None, "next_nonce": next_nonce,
                "seq": int(seq), **credit}

    def finalize_work_session(
        self, *, session_id: str, end_reason: str, ended_at: str, credit_fn,
        earning: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Close a session and, if it qualified, write its earnings row — atomically.

        Idempotent by construction: an already-ended session is returned as it was
        stored, without recomputation, so calling this five times (or twice
        concurrently) yields one row and one answer. ``earning`` is the ledger row
        to write WHEN the session qualifies AND the user accrues payment; pass
        None for a contributor who does not (an advisor), and the session still
        closes with its seconds recorded — the record survives, only the money
        does not.

        ``INSERT OR IGNORE`` leans on ``UNIQUE(kind, ref_id)``: if a concurrent
        finalizer already wrote the row, this one is a no-op rather than an
        IntegrityError surfacing as a 500 on a payout."""
        conn = self._conn()
        try:
            self._immediate(conn)
            row = conn.execute(
                "SELECT * FROM work_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return {"ok": False, "error": "not_found"}
            session = dict(row)
            if session.get("ended_at"):
                existing = conn.execute(
                    "SELECT * FROM earnings WHERE kind = ? AND ref_id = ?",
                    ((earning or {}).get("kind") or "review_session", session_id)).fetchone()
                conn.execute("COMMIT")
                paid = dict(existing) if existing is not None else None
                return {
                    "ok": True, "error": None, "already_ended": True,
                    "credited_seconds": int(session.get("credited_seconds") or 0),
                    "continuous_seconds": int(session.get("continuous_seconds") or 0),
                    "qualified": bool(session.get("qualified")),
                    "end_reason": session.get("end_reason"),
                    "ended_at": session.get("ended_at"),
                    "earning": paid,
                }
            beats = [dict(r) for r in conn.execute(
                "SELECT * FROM session_beats WHERE session_id = ? ORDER BY seq ASC",
                (session_id,)).fetchall()]
            credit = credit_fn(beats, session)
            qualified = bool(credit["qualified"])
            conn.execute(
                "UPDATE work_sessions SET ended_at = ?, end_reason = ?, credited_seconds = ?, "
                "continuous_seconds = ?, qualified = ?, jitter_ms = ?, "
                "distinct_progress_keys = ? WHERE session_id = ?",
                (ended_at, end_reason, int(credit["credited_seconds"]),
                 int(credit["continuous_seconds"]), 1 if qualified else 0,
                 credit.get("jitter_ms"), int(credit.get("distinct_progress_keys") or 0),
                 session_id),
            )
            written = None
            if qualified and earning:
                conn.execute(
                    "INSERT OR IGNORE INTO earnings "
                    "(earning_id, user_id, kind, ref_id, amount_cents, rate_cents, "
                    " status, accrued_at, resolved_at, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (earning["earning_id"], earning["user_id"], earning["kind"],
                     earning["ref_id"], int(earning["amount_cents"]), int(earning["rate_cents"]),
                     earning["status"], earning["accrued_at"], earning.get("resolved_at"),
                     earning.get("note")),
                )
                got = conn.execute(
                    "SELECT * FROM earnings WHERE kind = ? AND ref_id = ?",
                    (earning["kind"], earning["ref_id"])).fetchone()
                written = dict(got) if got is not None else None
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return {
            "ok": True, "error": None, "already_ended": False,
            "credited_seconds": int(credit["credited_seconds"]),
            "continuous_seconds": int(credit["continuous_seconds"]),
            "qualified": qualified, "end_reason": end_reason, "ended_at": ended_at,
            "earning": written,
            # Carried out of the transaction so the caller can decide whether this
            # payout deserves a human look WITHOUT re-reading the session it just
            # wrote — the values it would read back are exactly these.
            "jitter_ms": credit.get("jitter_ms"),
            "distinct_progress_keys": int(credit.get("distinct_progress_keys") or 0),
            "work_named": bool(credit.get("work_named")),
            "skipped_beats": int(credit.get("skipped_beats") or 0),
            # Set only when the ratchet bound — the caller turns it into a
            # payout-review event. Carried out rather than re-derived, because
            # once the floored value is written there is nothing left to compare.
            "regressed": credit.get("regressed"),
        }

    # ─── Ledger ───────────────────────────────────────────────────────────────
    def insert_earning(
        self, *, earning_id: str, user_id: str, kind: str, ref_id: str,
        amount_cents: int, rate_cents: int, status: str, accrued_at: str,
        resolved_at: Optional[str] = None, note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Write one ledger row. Returns None when ``UNIQUE(kind, ref_id)`` already
        holds a row — the caller learns "already accrued" without an exception, and
        without a check-then-insert race in between."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO earnings "
                "(earning_id, user_id, kind, ref_id, amount_cents, rate_cents, "
                " status, accrued_at, resolved_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (earning_id, user_id, kind, ref_id, int(amount_cents), int(rate_cents),
                 status, accrued_at, resolved_at, note),
            )
            if cur.rowcount == 0:
                return None
        return self.get_earning(kind=kind, ref_id=ref_id)

    def get_earning(self, *, kind: str, ref_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM earnings WHERE kind = ? AND ref_id = ?", (kind, ref_id)).fetchone()
        return dict(row) if row is not None else None

    def resolve_earning(
        self, *, kind: str, ref_id: str, status: str, resolved_at: str,
        note: Optional[str] = None, only_from: Optional[List[str]] = None,
    ) -> bool:
        """Move a ledger row to a decided state. ``only_from`` is a compare-and-set
        on the current status, so a transition can be expressed as a fact about
        what it is allowed to follow rather than as a read followed by a hopeful
        write. Returns True when a row actually moved."""
        sql = ("UPDATE earnings SET status = ?, resolved_at = ?, "
               "note = COALESCE(?, note) WHERE kind = ? AND ref_id = ?")
        params: List[Any] = [status, resolved_at, note, kind, ref_id]
        if only_from:
            sql += " AND status IN (%s)" % ",".join("?" * len(only_from))
            params.extend(only_from)
        with self._conn() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount > 0

    def mark_earnings_paid(
        self, *, payout_batch_id: str, paid_at: str,
        earning_ids: Optional[List[str]] = None, user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move ``approved`` rows to ``paid`` under one batch id, atomically.

        The batch id is the IDEMPOTENCY KEY, and that is the whole design. A
        disbursement job that times out and retries is the normal case, so
        replaying a batch must be a no-op rather than a second payment — which is
        why the UPDATE carries ``status = 'approved' AND payout_batch_id IS NULL``
        as a compare-and-set instead of reading first and hoping.

        Everything runs inside one BEGIN IMMEDIATE, for the same reason the
        session finalizer does: two disbursement runs must not be able to
        interleave a read and a write and pay the same physician twice.

        Returns counts rather than rows: ``marked`` is what this call changed,
        ``already_in_batch`` is what a previous identical call changed, and
        ``skipped`` is everything that was not eligible. A retry is the case where
        ``marked`` is 0 and ``already_in_batch`` is not."""
        conn = self._conn()
        try:
            self._immediate(conn)
            where, params = [], []
            if earning_ids:
                where.append("earning_id IN (%s)" % ",".join("?" * len(earning_ids)))
                params.extend(earning_ids)
            if user_id:
                where.append("user_id = ?")
                params.append(user_id)
            scope = " AND ".join(where)

            candidates = [dict(r) for r in conn.execute(
                f"SELECT * FROM earnings WHERE {scope}", params).fetchall()]
            already = [r for r in candidates
                       if r["status"] == "paid" and r["payout_batch_id"] == payout_batch_id]
            eligible = [r for r in candidates
                        if r["status"] == "approved" and r["payout_batch_id"] is None]

            marked = []
            for row in eligible:
                cur = conn.execute(
                    "UPDATE earnings SET status = 'paid', payout_batch_id = ?, "
                    "resolved_at = ? "
                    "WHERE earning_id = ? AND status = 'approved' "
                    "  AND payout_batch_id IS NULL",
                    (payout_batch_id, paid_at, row["earning_id"]))
                if cur.rowcount:
                    marked.append(row)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return {
            "marked": marked,
            "already_in_batch": len(already),
            "skipped": len(candidates) - len(marked) - len(already),
        }

    def earnings_for_user(
        self, user_id: str, *, limit: int = 200
    ) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM earnings WHERE user_id = ? "
                "ORDER BY accrued_at DESC, earning_id DESC LIMIT ?",
                (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def earnings_totals_for_user(self, user_id: str) -> Dict[str, Dict[str, int]]:
        """``{status: {kind: {"n": int, "cents": int}}}`` — the Earnings headline
        without pulling the whole ledger into Python to add it up."""
        out: Dict[str, Dict[str, Any]] = {}
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, kind, COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents "
                "FROM earnings WHERE user_id = ? GROUP BY status, kind", (user_id,)).fetchall()
        for r in rows:
            out.setdefault(r["status"], {})[r["kind"]] = {
                "n": int(r["n"]), "cents": int(r["cents"])}
        return out

    def list_earnings(
        self, *, user_id: Optional[str] = None, status: Optional[str] = None,
        payout_batch_id: Optional[str] = None, limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Admin ledger view, joined to the identity so the console can name the
        physician without a second round trip per row."""
        sql = ("SELECT e.*, u.email AS user_email, u.tier AS user_tier, "
               "       u.compensation_model AS compensation_model "
               "FROM earnings e LEFT JOIN users u ON u.id = e.user_id WHERE 1 = 1")
        params: List[Any] = []
        if user_id:
            sql += " AND e.user_id = ?"
            params.append(user_id)
        if status:
            sql += " AND e.status = ?"
            params.append(status)
        if payout_batch_id:
            sql += " AND e.payout_batch_id = ?"
            params.append(payout_batch_id)
        sql += " ORDER BY e.accrued_at DESC, e.earning_id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def earnings_by_status(self) -> Dict[str, Dict[str, int]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents "
                "FROM earnings GROUP BY status").fetchall()
        return {r["status"]: {"n": int(r["n"]), "cents": int(r["cents"])} for r in rows}

    # ─── Admin Launch PRD §4 — per-physician ledger reads and the void ────────
    #
    # Every number the Money screen shows comes from one of these, in SQL, over
    # the WHOLE ledger. The alternative — summing the rows a page happened to
    # fetch — is wrong the moment the ledger outgrows the page limit, and wrong
    # silently: the operator sees a smaller total and pays it.
    def get_earning_by_id(self, earning_id: str) -> Optional[Dict[str, Any]]:
        """One ledger row by its primary key, joined to the physician.

        Named apart from ``get_earning(kind=, ref_id=)`` deliberately — that one
        is keyed by the UNIQUE double-payment guard, this one by ``earning_id``
        (which IS the ledger PK; there is no ``ledger_id``)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT e.*, u.email AS user_email, u.full_name AS user_full_name, "
                "       u.tier AS user_tier, u.specialty AS user_specialty, "
                "       u.compensation_model AS compensation_model "
                "FROM earnings e LEFT JOIN users u ON u.id = e.user_id "
                "WHERE e.earning_id = ?", (earning_id,)).fetchone()
        return dict(row) if row is not None else None

    def earnings_outstanding_by_user(self) -> Dict[str, Dict[str, int]]:
        """Per-physician outstanding totals — the Money level-1 list.

        "Outstanding" is ``accrued`` + ``approved``: work we owe for and have not
        disbursed. ``paid`` is settled and ``void`` is not owed, so neither
        belongs in a payable figure — a total that included them would be a
        number no one should ever wire."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, "
                "  COALESCE(SUM(CASE WHEN status IN ('accrued','approved') "
                "                    THEN amount_cents ELSE 0 END), 0) AS outstanding_cents, "
                "  COALESCE(SUM(CASE WHEN status = 'paid' "
                "                    THEN amount_cents ELSE 0 END), 0) AS paid_cents, "
                "  COUNT(*) AS n_rows, "
                "  COALESCE(SUM(CASE WHEN status = 'void' THEN 1 ELSE 0 END), 0) AS n_void "
                "FROM earnings GROUP BY user_id").fetchall()
        return {
            r["user_id"]: {
                "outstanding_cents": int(r["outstanding_cents"]),
                "paid_cents": int(r["paid_cents"]),
                "n_rows": int(r["n_rows"]),
                "n_void": int(r["n_void"]),
            }
            for r in rows
        }

    def earnings_payable_for_user(self, user_id: str) -> Dict[str, int]:
        """The one physician's payable totals, recomputed from the ledger.

        This is what a void and a payment return to the client. The UI never
        subtracts locally: a client-side figure that drifts from the ledger is a
        wrong number in front of the person deciding what to pay.

        NOT ``earnings_totals_for_user`` — that name is already taken above by
        the doctor-facing ``{status: {kind: …}}`` breakdown, and a second
        definition of it would have silently shadowed the first for the whole
        class, breaking the physician's own Earnings page with no error."""
        agg = self.earnings_outstanding_by_user().get(user_id)
        if agg is None:
            return {"outstanding_cents": 0, "paid_cents": 0, "n_rows": 0, "n_void": 0}
        return agg

    def void_earning(self, earning_id: str, *, reason: str,
                     voided_by: str) -> Dict[str, Any]:
        """Void one ledger row. Idempotent on ``earning_id``.

        The guarded UPDATE is the arbiter, not a read-then-write: a double-click
        (or two admins on the same row) must decrement the total exactly once.
        ``status IN ('accrued','approved')`` in the WHERE clause is what makes
        the second call a no-op rather than a second decrement.

        Returns ``{"row", "changed", "reason_code"}``. ``changed`` is False both
        when the row was already void (fine, idempotent) and when it is ``paid``
        (not fine — the caller turns that into a 409). ``reason_code`` says
        which, because the two must not be reported to an operator as the same
        thing.
        """
        now = _utcnow_iso()
        conn = self._conn()
        try:
            self._immediate(conn)
            row = conn.execute("SELECT * FROM earnings WHERE earning_id = ?",
                               (earning_id,)).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return {"row": None, "changed": False, "reason_code": "not_found"}
            row = dict(row)
            if row["status"] == "paid":
                conn.execute("COMMIT")
                return {"row": row, "changed": False, "reason_code": "already_paid"}
            cur = conn.execute(
                "UPDATE earnings SET status = 'void', void_reason = ?, voided_by = ?, "
                "voided_at = ?, resolved_at = ? "
                "WHERE earning_id = ? AND status IN ('accrued', 'approved')",
                (reason, voided_by, now, now, earning_id))
            changed = bool(cur.rowcount)
            after = dict(conn.execute("SELECT * FROM earnings WHERE earning_id = ?",
                                      (earning_id,)).fetchone())
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return {"row": after, "changed": changed,
                "reason_code": "voided" if changed else "already_void"}

    # ─── Community invites (Admin Launch PRD §5.1) ───────────────────────────
    def create_community_invite(self, *, user_id: str, email: str, token_hash: str,
                                expires_at: str, created_by: Optional[str]) -> None:
        """Store the HASH of a freshly minted invite token. The raw token is
        never persisted — it exists only in the email we just sent."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO community_invites "
                "(token_hash, user_id, email, expires_at, created_at, created_by, redeemed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (token_hash, user_id, email, expires_at, _utcnow_iso(), created_by))

    def get_community_invite(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_invites WHERE token_hash = ?",
                (token_hash,)).fetchone()
        return dict(row) if row is not None else None

    def redeem_community_invite(self, token_hash: str) -> bool:
        """Stamp the invite consumed. Guarded UPDATE, so exactly one call wins —
        the same shape as ``mark_community_welcomed`` and for the same reason."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE community_invites SET redeemed_at = ? "
                "WHERE token_hash = ? AND redeemed_at IS NULL",
                (_utcnow_iso(), token_hash))
            return bool(cur.rowcount)

    def latest_community_invite_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """The most recent invite sent to this physician — what the roster row
        renders as ``Invited · {time}`` after a send."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM community_invites WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row is not None else None

    def work_sessions_for_user(
        self, user_id: str, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM work_sessions WHERE user_id = ? "
                "ORDER BY started_at DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # ─── Accrual reconciliation inputs (read-only over other PRDs' tables) ─────
    #
    # Two queries, and the split is deliberate. Reconciliation runs on EVERY
    # Earnings page load, so it must not scan a table that grows forever: each
    # query returns a set that is bounded by outstanding work rather than by
    # total history.
    #
    #   unaccrued_submissions  — terminal-submitted work with no ledger row yet.
    #                            Drains to ~empty in steady state.
    #   unresolved_task_earnings — ledger rows still awaiting a verdict.
    #                            Bounded by the review backlog.
    #
    # Both are read-only over other PRDs' tables. PRD-P §4 forbids a callback
    # into the review module; reading ``case_reviews`` is a contract-free
    # dependency where a callback is not.
    #
    # A PAIRED adjudication (PRD-R) writes ONE case_reviews row for the whole
    # case, anchored on the accepted submission, with the two labels recorded in
    # ``pair_sub_a``/``pair_sub_b``. Matching on ``submission_id`` alone therefore
    # resolves only ONE of the two physicians and leaves the other's $75 sitting
    # accrued until the fourteen-day sweep — money a reviewer already approved,
    # paid a fortnight late, for no reason the physician could discover.
    #
    # So a verdict is matched against all three columns. This reads two more
    # column NAMES off a table payments already reads; it does not call into the
    # review module and does not teach payments what a pair means beyond "these
    # two submissions were adjudicated together".
    #
    # ``review_verdicts`` is the raw comma-joined verdict list, classified by
    # ``payments._verdict_status``. Deliberately raw: filtering the accepting
    # verdicts here would put a second definition of "the work was accepted" into
    # a file that does not own the concept, which is what Seam 3 forbids and what
    # ``test_only_one_definition_of_expert_acceptance_exists`` catches. SQL counts
    # rows; the payments module decides what they mean.

    # A submission is worth money once it has reached a terminal SUBMITTED state.
    # ``rejected`` and the Stage-1 flag states (``prompt_flagged``, ``not_hard``,
    # ``case_incoherent``) are absent on purpose: they produce no records.
    _ACCRUABLE_STATUSES = (
        "submitted", "auto_validated", "qa_checked", "export_ready", "needs_qa", "exported",
    )

    def unaccrued_submissions(
        self, *, user_id: Optional[str] = None, limit: int = 2000
    ) -> List[Dict[str, Any]]:
        """Payable submissions that have no ledger row yet, oldest first.

        The LEFT JOIN to ``users`` mirrors the audit-M6 reasoning next to
        ``PAYABLE_SQL`` — an INNER JOIN would silently under-pay a physician whose
        user row went missing. Unpayable authors are excluded IN SQL rather than
        in Python, so an equity-holding advisor's submissions never occupy the
        scan window forever (they will never gain a ledger row, by design).

        The mock/sandbox contributor is excluded too: it exists to exercise the
        live portal and its submissions must never become a cash obligation.

        Oldest first so a backlog drains deterministically rather than starving
        its own tail against the limit."""
        from asclepius.compensation import PAYABLE_SQL
        placeholders = ",".join("?" * len(self._ACCRUABLE_STATUSES))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT s.submission_id,
                       s.evaluator_id,
                       s.task_id,
                       s.status,
                       s.created_at,
                       (SELECT GROUP_CONCAT(cr.verdict) FROM case_reviews cr
                         WHERE cr.submission_id = s.submission_id
                            OR cr.pair_sub_a   = s.submission_id
                            OR cr.pair_sub_b   = s.submission_id) AS review_verdicts,
                       (SELECT cr.reviewer_notes FROM case_reviews cr
                         WHERE (cr.submission_id = s.submission_id
                                OR cr.pair_sub_a = s.submission_id
                                OR cr.pair_sub_b = s.submission_id)
                           AND cr.verdict = 'reject'
                         ORDER BY cr.created_at DESC LIMIT 1) AS reject_note
                FROM submissions s
                LEFT JOIN users u ON u.id = s.evaluator_id
                WHERE s.status IN ({placeholders})
                  AND COALESCE(u.is_mock, 0) = 0
                  AND {PAYABLE_SQL}
                  AND NOT EXISTS (SELECT 1 FROM earnings e
                                   WHERE e.kind = 'task' AND e.ref_id = s.submission_id)
                  AND (? IS NULL OR s.evaluator_id = ?)
                ORDER BY s.created_at ASC
                LIMIT ?
                """,
                (*self._ACCRUABLE_STATUSES, user_id, user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def unresolved_task_earnings(
        self, *, user_id: Optional[str] = None, limit: int = 2000
    ) -> List[Dict[str, Any]]:
        """Task ledger rows not yet in a decided state, with their verdicts.

        ``accrued`` is "awaiting a verdict"; ``void`` is included because a later
        accepting verdict may restore money (PRD-P §1.2 — money may go up, never
        down). ``approved`` and ``paid`` are terminal and are never re-examined."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT e.ref_id AS submission_id,
                       e.user_id,
                       e.status,
                       (SELECT GROUP_CONCAT(cr.verdict) FROM case_reviews cr
                         WHERE cr.submission_id = e.ref_id
                            OR cr.pair_sub_a   = e.ref_id
                            OR cr.pair_sub_b   = e.ref_id) AS review_verdicts,
                       (SELECT cr.reviewer_notes FROM case_reviews cr
                         WHERE (cr.submission_id = e.ref_id
                                OR cr.pair_sub_a = e.ref_id
                                OR cr.pair_sub_b = e.ref_id)
                           AND cr.verdict = 'reject'
                         ORDER BY cr.created_at DESC LIMIT 1) AS reject_note
                FROM earnings e
                WHERE e.kind = 'task' AND e.status IN ('accrued', 'void')
                  AND (? IS NULL OR e.user_id = ?)
                ORDER BY e.accrued_at ASC
                LIMIT ?
                """,
                (user_id, user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def submission_specialties(self, submission_ids: List[str]) -> Dict[str, str]:
        """``{submission_id: specialty}`` in one query, so the Earnings ledger can
        say "Task · nephrology case" without an N+1 walk over the ledger."""
        out: Dict[str, str] = {}
        if not submission_ids:
            return out
        with self._conn() as conn:
            for i in range(0, len(submission_ids), 400):
                chunk = submission_ids[i:i + 400]
                rows = conn.execute(
                    "SELECT s.submission_id AS sid, t.specialty AS specialty "
                    "FROM submissions s LEFT JOIN tasks t ON t.task_id = s.task_id "
                    "WHERE s.submission_id IN (%s)" % ",".join("?" * len(chunk)),
                    chunk).fetchall()
                for r in rows:
                    if r["specialty"]:
                        out[r["sid"]] = r["specialty"]
        return out

    def accrued_earnings_before(
        self, cutoff_iso: str, *, user_id: Optional[str] = None, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """``accrued`` rows older than the cutoff — the auto-approve sweep's input
        (PRD-P §1.2: a labeler is never held hostage by a review backlog).

        ``user_id`` scopes it to one physician's backlog, which is what a doctor's
        own Earnings read is entitled to touch. The unscoped form is the admin
        sweep, and it has to keep existing: the fourteen-day promise cannot depend
        on the physician remembering to open the page."""
        sql = "SELECT * FROM earnings WHERE status = 'accrued' AND accrued_at < ?"
        params: List[Any] = [cutoff_iso]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY accrued_at ASC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    # ═══ END PRD-P STORE METHODS ═══

    # ═══ PRD-REF STORE METHODS — the referral bounty ═════════════════════════
    # Appended per START_HERE §3.2: nothing above is modified. Everything here
    # operates on the EXISTING ``referrals`` table and the EXISTING ``earnings``
    # ledger. There is no second referral store, because two referral tables is
    # how a bounty gets paid twice.

    #: Bounty states. NULL is the fifth and it is the important one: "not
    #: decided yet", which the Earnings page renders as pending money rather
    #: than as silence.
    BOUNTY_EARNED = "earned"
    BOUNTY_DUPLICATE = "duplicate"
    BOUNTY_EXPIRED = "expired"
    BOUNTY_INELIGIBLE = "ineligible"
    BOUNTY_STATES = (BOUNTY_EARNED, BOUNTY_DUPLICATE, BOUNTY_EXPIRED, BOUNTY_INELIGIBLE)

    def ensure_referral_code(self, user_id: str) -> Optional[str]:
        """This physician's referral code, minting one if they have none.

        Every advisor gets a code at appointment. An ordinary physician does not,
        and ``referrals.referral_code`` is NOT NULL — so generalising referrals
        beyond the advisor tier needs a code minted on first use rather than an
        error page on the one action that recruits a colleague.

        Idempotent, and safe under a race: the partial unique index on
        ``users(referral_code)`` is the guarantee, the SELECT inside
        ``_mint_referral_code`` is only an optimisation, and a lost race is
        retried rather than surfaced as a 500 (the same reasoning as
        ``appoint_advisor``).
        """
        if not user_id:
            return None
        for _ in range(3):
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT referral_code FROM users WHERE id = ?", (user_id,)).fetchone()
                if row is None:
                    return None
                if row["referral_code"]:
                    return str(row["referral_code"])
                code = self._mint_referral_code(conn)
                try:
                    conn.execute(
                        "UPDATE users SET referral_code = ? "
                        "WHERE id = ? AND referral_code IS NULL", (code, user_id))
                except sqlite3.IntegrityError:
                    continue
            fresh = self.get_user_by_id(user_id)
            if fresh and fresh.get("referral_code"):
                return str(fresh["referral_code"])
        return None

    def referrals_for_invitee(self, user_id: str) -> List[Dict[str, Any]]:
        """Every referral that claimed this physician, earliest invite first.

        Plural because ``claim_referral_for_signup`` is plural: two physicians
        can both refer the same colleague, and a well-connected candidate is
        exactly who gets referred twice. Ordering is (invited_at, referral_id) so
        "who referred them first" is a total order and not a coin toss — the
        bounty winner is picked off the front of this list.
        """
        if not user_id:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM referrals WHERE user_id = ? "
                "ORDER BY invited_at ASC, referral_id ASC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def referral_bounty_amounts(self, referral_ids: List[str]) -> Dict[str, int]:
        """``{referral_id: amount_cents}`` for bounties that were actually paid.

        The funnel must report what the LEDGER says a referral earned, not what
        the current rate would pay for it today. Every rate in this system is
        stamped on the row at accrual precisely so a change to
        ``ASCLEPIUS_REFERRAL_BOUNTY_CENTS`` can never restate a past earning —
        and a funnel that multiplied its earned count by the live constant would
        restate exactly that, on the one surface where the doctor reads it.

        One query for the page rather than one per row.
        """
        ids = [i for i in (referral_ids or []) if i]
        out: Dict[str, int] = {}
        if not ids:
            return out
        with self._conn() as conn:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                rows = conn.execute(
                    "SELECT ref_id, amount_cents FROM earnings WHERE kind = 'referral' "
                    "AND ref_id IN (%s)" % ",".join("?" * len(chunk)), chunk).fetchall()
                for r in rows:
                    out[r["ref_id"]] = int(r["amount_cents"])
        return out

    def has_approved_task_earning(self, user_id: str) -> bool:
        """Has this physician had at least one TASK earning reach a settled,
        earned state? ``paid`` counts: money that has already left the building
        is not less approved than money that has not.

        This is the bounty's trigger read, and it deliberately asks the LEDGER
        rather than re-deriving "approved" from review verdicts. ``payments``
        owns exactly one definition of when a task is worth money
        (``_verdict_status`` plus the auto-approve sweep), and a second one here
        would drift from it the first time either changes.
        """
        if not user_id:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM earnings WHERE user_id = ? AND kind = 'task' "
                "AND status IN ('approved', 'paid') LIMIT 1", (user_id,)).fetchone()
        return row is not None

    def expire_stale_referrals(
        self, *, referrer_id: str, cutoff: str, resolved_at: str,
    ) -> int:
        """Retire invitations that were never taken up. Returns the number moved.

        Without this a physician's funnel becomes a graveyard of two-year-old
        invitations and the page stops meaning anything — which is the same
        failure as showing nothing, arrived at from the other direction.

        Scoped to ONE referrer (a page load must not sweep the company) and
        narrow on purpose: only a row that nobody ever signed up against
        (``user_id IS NULL``), that is still sitting at the first rung of the
        funnel, and whose bounty has not already been decided. An expired row
        keeps its ``invited_at`` — the record of the invitation is not deleted,
        it is settled.
        """
        if not referrer_id:
            return 0
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE referrals
                   SET status = 'expired',
                       bounty_state = 'expired',
                       bounty_resolved_at = ?,
                       resolved_at = COALESCE(resolved_at, ?)
                 WHERE referrer_id = ?
                   AND user_id IS NULL
                   AND bounty_state IS NULL
                   AND (status IS NULL OR status = 'invited')
                   AND invited_at < ?
                """,
                (resolved_at, resolved_at, referrer_id, cutoff))
            return int(cur.rowcount or 0)

    def set_referral_bounty_state(
        self, referral_id: str, state: str, *, resolved_at: str,
    ) -> bool:
        """Record a terminal bounty decision that is NOT a payment — a
        self-referral, or a referrer who holds equity instead of a cash rate.

        Compare-and-set on ``bounty_state IS NULL`` so a decision can never
        overwrite a payment that already happened. Returns True when a row moved.
        """
        if not referral_id or state not in self.BOUNTY_STATES:
            return False
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE referrals SET bounty_state = ?, bounty_resolved_at = ? "
                "WHERE referral_id = ? AND bounty_state IS NULL",
                (state, resolved_at, referral_id))
            return bool(cur.rowcount)

    def settle_referral_bounty(
        self,
        *,
        invitee_user_id: str,
        earning_id: str,
        amount_cents: int,
        accrued_at: str,
        eligible_ids: Optional[List[str]] = None,
        note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Pay AT MOST ONE bounty for this physician, ever — atomically.

        ``UNIQUE(kind, ref_id)`` on the ledger already makes a second payment
        against the SAME referral row impossible. It does nothing about two
        DIFFERENT referral rows for the same invitee, which is the case that
        actually costs money: two physicians both refer Dr Chen, both rows are
        claimed by ``claim_referral_for_signup`` (correctly — they both did refer
        her), and a naive per-row accrual pays $300 for one physician.

        So the whole decision — which row wins, which rows are duplicates,
        whether a bounty already exists — happens inside one BEGIN IMMEDIATE.
        Five concurrent callers cannot interleave a read and a write here; four
        of them find the winner already settled and return it unchanged.

        ``eligible_ids`` is the caller's whitelist of referral rows that may be
        paid, in preference order — resolved by ``payments`` BEFORE this call,
        because deciding it needs the referrers' compensation models and this
        transaction must not issue a second connection's read while it holds the
        write lock. A stale whitelist is acceptable and a deadlock is not: the
        window is microseconds and the worst case is a bounty that settles on the
        next pass. ``None`` means "any unsettled row".

        Returns the winning referral row (with its bounty state) or None when no
        row was eligible. Idempotent: a second call returns the same row.
        """
        if not self.referrals_for_invitee(invitee_user_id):
            return None
        allowed = None if eligible_ids is None else set(eligible_ids)
        order = {rid: i for i, rid in enumerate(eligible_ids or [])}

        conn = self._conn()
        try:
            self._immediate(conn)
            fresh = [dict(r) for r in conn.execute(
                "SELECT * FROM referrals WHERE user_id = ? "
                "ORDER BY invited_at ASC, referral_id ASC", (invitee_user_id,)).fetchall()]

            # Already settled? Return the row that carries the payment. Checked
            # against the LEDGER as well as the column, because the ledger is the
            # authority on whether money exists and the column is a projection of
            # it — if they ever disagree, the ledger wins.
            paid = None
            for r in fresh:
                if r.get("bounty_state") == self.BOUNTY_EARNED:
                    paid = r
                    break
                existing = conn.execute(
                    "SELECT earning_id FROM earnings WHERE kind = 'referral' AND ref_id = ?",
                    (r["referral_id"],)).fetchone()
                if existing is not None:
                    conn.execute(
                        "UPDATE referrals SET bounty_state = ?, bounty_earning_id = ?, "
                        "bounty_resolved_at = COALESCE(bounty_resolved_at, ?) "
                        "WHERE referral_id = ?",
                        (self.BOUNTY_EARNED, existing["earning_id"], accrued_at,
                         r["referral_id"]))
                    paid = dict(r, bounty_state=self.BOUNTY_EARNED,
                                bounty_earning_id=existing["earning_id"])
                    break

            if paid is None:
                candidates = [r for r in fresh
                              if r.get("bounty_state") is None
                              and (allowed is None or r["referral_id"] in allowed)]
                # The caller's preference order wins where it expressed one;
                # (invited_at, referral_id) breaks any remaining tie, so "who
                # referred them first" is a total order and not a coin toss.
                candidates.sort(key=lambda r: order.get(r["referral_id"], len(order)))
                if not candidates:
                    conn.execute("COMMIT")
                    return None
                winner = candidates[0]
                conn.execute(
                    "INSERT OR IGNORE INTO earnings "
                    "(earning_id, user_id, kind, ref_id, amount_cents, rate_cents, "
                    " status, accrued_at, resolved_at, note) "
                    "VALUES (?, ?, 'referral', ?, ?, ?, 'approved', ?, ?, ?)",
                    (earning_id, winner["referrer_id"], winner["referral_id"],
                     int(amount_cents), int(amount_cents), accrued_at, accrued_at, note))
                # Read the id back rather than trusting the INSERT: OR IGNORE
                # means the row may predate this call.
                written = conn.execute(
                    "SELECT earning_id FROM earnings WHERE kind = 'referral' AND ref_id = ?",
                    (winner["referral_id"],)).fetchone()
                conn.execute(
                    "UPDATE referrals SET bounty_state = ?, bounty_earning_id = ?, "
                    "bounty_resolved_at = ? WHERE referral_id = ?",
                    (self.BOUNTY_EARNED, written["earning_id"] if written else earning_id,
                     accrued_at, winner["referral_id"]))
                paid = dict(winner, bounty_state=self.BOUNTY_EARNED,
                            bounty_earning_id=written["earning_id"] if written else earning_id)

            # Everyone else is a DUPLICATE, not a row left at 'invited' forever.
            # That stranding is the advisor bug repeating: a referrer whose
            # colleague demonstrably joined and worked should never be looking at
            # a funnel that still says "awaiting their first case".
            #
            # ONLY rows the caller said were payable. A row outside the whitelist
            # lost for a different reason — an equity-holding referrer, a
            # self-referral — and the caller has already stamped it with the
            # reason that is true of it. Marking it 'duplicate' here would
            # overwrite an accurate answer with a plausible one.
            for r in fresh:
                if r["referral_id"] == paid["referral_id"]:
                    continue
                if r.get("bounty_state") is not None:
                    continue
                if allowed is not None and r["referral_id"] not in allowed:
                    continue
                conn.execute(
                    "UPDATE referrals SET bounty_state = ?, bounty_resolved_at = ? "
                    "WHERE referral_id = ? AND bounty_state IS NULL",
                    (self.BOUNTY_DUPLICATE, accrued_at, r["referral_id"]))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return self.get_referral(paid["referral_id"])
    # ═══ END PRD-REF STORE METHODS ═══


# ─── PRD-I §F3: database durability ───────────────────────────────────────────
def _db_storage_durable() -> tuple:
    """(ok, detail) — will the SQLite database survive a redeploy, and can we
    actually write to it? (PRD I-0 §F3)

    Every durability check that existed covered BLOBS. Nothing checked the database,
    which is the one loss that is unrecoverable in kind rather than degree: losing
    image blobs degrades cases, losing ``asclepius.db`` destroys every user, task,
    submission, review and payout record at once.

    Two questions, because they fail differently. Ephemerality is a configuration
    mistake you can see. Writability is a mount that ATTACHED WRONG — a read-only
    volume, or a failed attach leaving a bare directory where the mount should be —
    which looks completely healthy until the first write. So we probe it."""
    from asclepius.constants import path_is_ephemeral

    db_path = os.getenv("ASCLEPIUS_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "asclepius.db")
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "/"
    if path_is_ephemeral(db_dir):
        return False, (
            f"database directory {db_dir} is on ephemeral storage; a redeploy "
            "destroys every user, task, submission and payout row. Set "
            "ASCLEPIUS_DB_PATH to a path on your persistent volume.")
    if not os.getenv("ASCLEPIUS_DB_PATH", "").strip():
        return False, (
            f"ASCLEPIUS_DB_PATH is not set, so the database lives beside the code "
            f"at {db_path} and is replaced on every redeploy. Set it to a path on "
            "your persistent volume.")
    try:
        os.makedirs(db_dir, exist_ok=True)
        probe = os.path.join(db_dir, f".durability-probe-{os.getpid()}")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        return False, f"database directory {db_dir} is not writable: {exc}"
    return True, f"database directory {db_dir} is durable and writable"


# ─── Process-wide singleton ───────────────────────────────────────────────────
_STORE: Optional[AsclepiusStore] = None


def get_store() -> AsclepiusStore:
    global _STORE
    if _STORE is None:
        _STORE = AsclepiusStore()
    return _STORE


def reset_store_for_tests(db_path: Optional[str] = None) -> AsclepiusStore:
    """Rebuild the singleton against a fresh DB path (test helper only)."""
    global _STORE
    _STORE = AsclepiusStore(db_path=db_path)
    return _STORE
