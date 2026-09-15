"""Assignment permissions for contributor work; onboarding never grants a case.

The former open pool remains an explicit deployment opt-in for legacy workflows.
The shipped default requires individual, role-specific routing in both realms.
"""
from __future__ import annotations

import os


def open_pool_enabled() -> bool:
    return os.getenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED", "0").strip() == "1"


def review_lease_minutes() -> int:
    """One claim lifetime for review queues and permission to start paid work."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_REVIEW_LEASE_MIN", "45")))
    except ValueError:
        return 45


def assignment_required(user: dict) -> bool:
    if open_pool_enabled() or user.get("is_mock") or user.get("role") == "admin":
        return False
    # Legacy QA accounts without a contributor tier inspect the product only.
    # A QA account carrying a physician tier has the same work permissions as
    # any other contributor and must not bypass assignment through its role.
    return not (user.get("role") == "qa_reviewer" and not user.get("tier"))


def can_start_review_session(store, user: dict) -> bool:
    """Resolve work admission here, keeping review queue details out of billing.

    Both review formats use the same configured claim lifetime. An assignment
    or a live claim can admit new paid work; session continuation is handled by
    the payments module before consulting this policy.
    """
    if not assignment_required(user):
        return True
    options = {"specialty": user.get("specialty"), "lease_minutes": review_lease_minutes()}
    return (store.next_review_pair_for(user["id"], **options) is not None
            or store.next_review_for(user["id"], **options) is not None)


_LIVE = ("a.status IN ('offered','claimed') AND "
         "(a.expires_at IS NULL OR datetime(a.expires_at) > datetime('now'))")


def active_assignment_sql(role: str, task_alias: str = "t") -> str:
    assert role in ("label", "review") and task_alias in ("t", "s")
    return (f"EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = {task_alias}.task_id "
            f"AND a.user_id = ? AND a.role = '{role}' AND {_LIVE})")


def label_access_sql() -> str:
    """One user placeholder, including non-exam work already committed.

    Exam reveals also create independent commits against gold task IDs. Those
    commits are never permission to turn an examination into paid casework.
    """
    return f"""EXISTS (SELECT 1 FROM users access_u WHERE access_u.id = ? AND (
        EXISTS (SELECT 1 FROM assignments a WHERE a.task_id = t.task_id
                AND a.user_id = access_u.id AND a.role = 'label' AND {_LIVE})
        OR EXISTS (SELECT 1 FROM independent_commits ic
                   WHERE ic.task_id = t.task_id AND ic.evaluator_id = access_u.id
                   AND COALESCE(json_extract(ic.payload_json, '$.purpose'), '') != 'credentialing_exam'
                   AND (json_extract(ic.payload_json, '$.purpose') = 'labeling'
                        OR access_u.verified_at IS NULL
                        OR datetime(ic.created_at) >= datetime(access_u.verified_at)
                        OR EXISTS (SELECT 1 FROM events approval
                                   WHERE approval.entity_type = 'user' AND approval.entity_id = access_u.id
                                   AND approval.event_type = 'verification_approved'
                                   AND datetime(approval.occurred_at) <= datetime(ic.created_at)))
                   AND NOT EXISTS (SELECT 1 FROM credentialing_exams ce
                                   WHERE ce.user_id = access_u.id AND ce.task_id = t.task_id)
                   AND t.task_id != COALESCE(json_extract(access_u.tutorial_json, '$.exam.task_id'), ''))
    ))"""


def has_assignment(store, task_id: str, user_id: str, roles=("label",)) -> bool:
    assert roles and all(role in ("label", "review") for role in roles)
    marks = ",".join("?" for _ in roles)
    with store._conn() as conn:
        return conn.execute(
            f"SELECT 1 FROM assignments a WHERE a.task_id = ? AND a.user_id = ? "
            f"AND a.role IN ({marks}) AND {_LIVE} LIMIT 1",
            (task_id, user_id, *roles),
        ).fetchone() is not None


def has_started_label(store, task_id: str, user_id: str) -> bool:
    with store._conn() as conn:
        return conn.execute(
            f"SELECT 1 FROM tasks t WHERE t.task_id = ? AND {label_access_sql()}",
            (task_id, user_id),
        ).fetchone() is not None


def _live_review_claim_sql() -> str:
    """One user placeholder for an actually drawn single or paired review."""
    cutoff = f"datetime('now', '-{review_lease_minutes()} minutes')"
    return f"""EXISTS (SELECT 1 FROM users claim_u WHERE claim_u.id = ? AND (
        (t.review_status = 'in_review' AND t.review_claimed_by = claim_u.id
         AND datetime(t.review_claimed_at) >= {cutoff})
        OR EXISTS (SELECT 1 FROM submissions claim_s WHERE claim_s.task_id = t.task_id
                   AND claim_s.review_status = 'in_review'
                   AND claim_s.review_claimed_by = claim_u.id
                   AND datetime(claim_s.review_claimed_at) >= {cutoff})
    ))"""


def has_live_review_claim(store, task_id: str, user_id: str) -> bool:
    with store._conn() as conn:
        return conn.execute(
            f"SELECT 1 FROM tasks t WHERE t.task_id = ? AND {_live_review_claim_sql()}",
            (task_id, user_id),
        ).fetchone() is not None


def accessible_asset_task_ids(store, asset_id: str, sha256: str, user_id: str) -> list[str]:
    """An image can belong to several chart snapshots; its index stores only one.

    Search the user's authorized cases, matching the actual retained StudyAsset,
    rather than treating that last index owner as the image's only permission.
    The caller must still enforce each matching case's sequence and privacy gates.
    """
    with store._conn() as conn:
        rows = conn.execute(
            f"""SELECT t.task_id FROM tasks t
                WHERE ({label_access_sql()} OR {active_assignment_sql('review')}
                       OR {_live_review_claim_sql()})
                AND EXISTS (SELECT 1 FROM json_each(t.case_json, '$.studies') study
                            WHERE json_extract(study.value, '$.asset.asset_id') = ?
                            AND json_extract(study.value, '$.asset.sha256') = ?)""",
            (user_id, user_id, user_id, asset_id, sha256),
        ).fetchall()
    return [row["task_id"] for row in rows]
