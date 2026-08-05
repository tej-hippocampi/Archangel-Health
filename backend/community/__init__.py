"""Asclepius Community — in-house Slack for contributor physicians (Community PRD).

A private, credential-gated, real-time messaging workspace reached only from
inside the doctor portal. Standalone module: its own SQLite store
(``COMMUNITY_DB_PATH``), the existing Asclepius JWT for auth, the shared PHI
scanner stack for the block-at-send gate, and the hash-chained audit log for
accountability. Never touches the evaluation flow (PRD §0).
"""
