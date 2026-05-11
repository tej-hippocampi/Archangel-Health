"""Seed data for the Archangel Triage Escalation demo tenant (TRIAGEDM).

Idempotent: safe to call on every startup. Does not touch CDRSNAI1 patients.
"""

from __future__ import annotations

import html
import uuid
from datetime import date, datetime, time, timedelta, timezone as tz
from typing import Any, Callable, Dict, List, Optional, Tuple

from tenant_constants import (
    ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
    TRIAGEDM_CLINIC_CODE,
    TRIAGE_DEMO_RN_EMAIL,
    TRIAGE_DEMO_SLUG,
    TRIAGE_DEMO_SURGEON_EMAIL,
    TRIAGE_DEMO_SURGEON_PASSWORD,
    TRIAGE_DEMO_RN_PASSWORD,
)

TRIAGE_HS_NAME = "Archangel Triage Demo Clinic"
TRIAGE_DEMO_SURGEON_NAME = "Dr. Eleanor Thompson, MD"
TRIAGE_DEMO_RN_NAME = "Maria Castillo, RN"

DemoBattlecardFn = Callable[[str, List[str]], str]


def _iso(d: date) -> str:
    return d.isoformat()


def _dt_combine(d: date) -> str:
    return datetime.combine(d, datetime.utcnow().time()).replace(microsecond=0).isoformat()


def triage_demo_patient_ids() -> List[str]:
    return [r["id"] for r in triage_patient_blueprint()]


def triage_patient_blueprint() -> List[Dict[str, Any]]:
    """Ten curated patients (PRD §3)."""
    return [
        {
            "id": "triage_robert_chen",
            "name": "Robert Chen",
            "phase": "pre_op",
            "family": "LEJR",
            "procedure": "Total Knee Arthroplasty",
            "cpt": "27447",
            "tier": "TIER_1",
            "score": 1,
            "hard": False,
            "surgery_offset_days": 10,
            "reasons": [
                {"kind": "BASE", "code": "LEJR_BASE", "label": "LEJR base risk (1)", "weight": 1},
            ],
            "risk_blurb": "Clean — age 64, BMI 27, no escalators",
            "preop_retier": False,
        },
        {
            "id": "triage_patricia_alvarez",
            "name": "Patricia Alvarez",
            "phase": "pre_op",
            "family": "LEJR",
            "procedure": "Total Hip Arthroplasty",
            "cpt": "27130",
            "tier": "TIER_2",
            "current_tier": "TIER_2",
            "initial_tier": "TIER_1",
            "initial_tier_score": 1,
            "score": 1,
            "hard": False,
            "surgery_offset_days": 2,
            "procedure_at_noon_utc": True,
            "initial_reasons": [
                {"kind": "BASE", "code": "LEJR_BASE", "label": "LEJR base risk", "weight": 1},
            ],
            "reasons": [
                {"kind": "BASE", "code": "LEJR_BASE", "label": "LEJR base risk", "weight": 1},
            ],
            "risk_blurb": (
                "Re-tiered T-96 → T-48 from T-1 to T-2 (low PAM, low readiness survey, BMI/smoker on intake)"
            ),
            "preop_retier": False,
        },
        {
            "id": "triage_michael_obrien",
            "name": "Michael O'Brien",
            "phase": "pre_op",
            "family": "SPINAL_FUSION",
            "procedure": "Spinal Fusion L4–L5",
            "cpt": "22612",
            "tier": "TIER_2",
            "score": 6,
            "hard": False,
            "surgery_offset_days": 7,
            "reasons": [
                {"kind": "BASE", "code": "SPINE_BASE", "label": "Spinal fusion base risk", "weight": 2},
                {"kind": "SOFT", "code": "OPIOID_ACTIVE", "label": "Active opioid use", "weight": 2},
                {"kind": "SOFT", "code": "LIVES_ALONE", "label": "Lives alone, no caregiver", "weight": 2},
            ],
            "risk_blurb": "Active opioid use + lives alone, no caregiver",
            "preop_retier": True,
        },
        {
            "id": "triage_linda_whitfield",
            "name": "Linda Whitfield",
            "phase": "pre_op",
            "family": "CABG",
            "procedure": "CABG x3",
            "cpt": "33533",
            "tier": "TIER_3",
            "score": None,
            "hard": True,
            "surgery_offset_days": 5,
            "reasons": [
                {
                    "kind": "HARD",
                    "code": "CHF_WITHIN_30D",
                    "label": "CHF within 30 days — automatic TIER_3",
                    "weight": None,
                },
            ],
            "risk_blurb": "HARD: CHF within 30 days",
            "preop_retier": True,
        },
        {
            "id": "triage_david_mensah",
            "name": "David Mensah",
            "phase": "pre_op",
            "family": "MAJOR_BOWEL",
            "procedure": "Sigmoidectomy",
            "cpt": "44140",
            "tier": "TIER_2",
            "score": 7,
            "hard": False,
            "surgery_offset_days": 14,
            "reasons": [
                {"kind": "BASE", "code": "BOWEL_BASE", "label": "Major bowel base risk", "weight": 2},
                {"kind": "SOFT", "code": "FUNC_PARTIAL", "label": "Functional status: PARTIALLY_DEPENDENT", "weight": 3},
                {"kind": "SOFT", "code": "DM_UNCONTROLLED", "label": "Diabetes uncontrolled (HbA1c 9.8)", "weight": 2},
            ],
            "risk_blurb": "Functional status: PARTIALLY_DEPENDENT + diabetes uncontrolled (HbA1c 9.8)",
            "preop_retier": True,
        },
        {
            "id": "triage_helen_park",
            "name": "Helen Park",
            "phase": "post_op",
            "family": "LEJR",
            "procedure": "Total Knee Arthroplasty",
            "cpt": "27447",
            "episode_day": 2,
            "tier": "TIER_1",
            "score": 1,
            "hard": False,
            "reasons": [
                {"kind": "BASE", "code": "LEJR_BASE", "label": "LEJR base risk (1)", "weight": 1},
            ],
            "risk_blurb": "Day 2 post-op, on track",
            "preop_retier": False,
        },
        {
            "id": "triage_jamal_carter",
            "name": "Jamal Carter",
            "phase": "post_op",
            "family": "CABG",
            "procedure": "CABG x4",
            "cpt": "33533",
            "episode_day": 4,
            "tier": "TIER_2",
            "score": 5,
            "hard": False,
            "reasons": [
                {"kind": "BASE", "code": "CABG_BASE", "label": "CABG base risk", "weight": 2},
                {"kind": "SOFT", "code": "LOW_EF", "label": "Low EF (35%)", "weight": 2},
                {"kind": "SOFT", "code": "ALCOHOL_RISK", "label": "At-risk alcohol use", "weight": 1},
            ],
            "risk_blurb": "Day 4 post-op, low EF (35%) + at-risk alcohol use",
            "preop_retier": False,
            "checkin_bp_spike": True,
        },
        {
            "id": "triage_sandra_reyes",
            "name": "Sandra Reyes",
            "phase": "post_op",
            "family": "HIP_FEMUR_FRACTURE",
            "procedure": "Hip Femur Fracture ORIF",
            "cpt": "27244",
            "episode_day": 17,
            "tier": "TIER_3",
            "current_tier": "TIER_3",
            "initial_tier": "TIER_1",
            "initial_tier_score": 1,
            "score": 1,
            "hard": False,
            "initial_reasons": [
                {"kind": "BASE", "code": "HIP_BASE", "label": "Hip/femur fracture base risk", "weight": 1},
            ],
            "reasons": [
                {"kind": "BASE", "code": "HIP_BASE", "label": "Hip/femur fracture base risk", "weight": 1},
            ],
            "risk_blurb": "Day 17 post-op — escalated from T-1 to T-3 (intra-op event + Day 7 RED survey)",
            "preop_retier": False,
            "post_intraop_tier": "TIER_2",
        },
        {
            "id": "triage_gregory_tate",
            "name": "Gregory Tate",
            "phase": "post_op",
            "family": "SPINAL_FUSION",
            "procedure": "Spinal Fusion T11–L1",
            "cpt": "22612",
            "episode_day": 8,
            "tier": "TIER_2",
            "score": 6,
            "hard": False,
            "reasons": [
                {"kind": "BASE", "code": "SPINE_BASE", "label": "Spinal fusion base", "weight": 2},
                {"kind": "SOFT", "code": "ASA3", "label": "ASA 3", "weight": 2},
                {"kind": "SOFT", "code": "RECENT_FALL", "label": "Recent fall", "weight": 1},
                {"kind": "SOFT", "code": "HOUSING_UNSTABLE", "label": "Housing UNSTABLE", "weight": 1},
            ],
            "risk_blurb": "ASA 3 + recent fall + housing UNSTABLE",
            "preop_retier": False,
        },
        {
            "id": "triage_yolanda_brooks",
            "name": "Yolanda Brooks",
            "phase": "post_op",
            "family": "MAJOR_BOWEL",
            "procedure": "Colectomy",
            "cpt": "44110",
            "episode_day": 10,
            "tier": "TIER_2",
            "score": 4,
            "hard": False,
            "reasons": [
                {"kind": "BASE", "code": "BOWEL_BASE", "label": "Major bowel base", "weight": 2},
                {"kind": "SOFT", "code": "AGE_ELDERLY", "label": "Age 77", "weight": 1},
                {"kind": "SOFT", "code": "BMI_ELEVATED", "label": "BMI 32", "weight": 1},
                {"kind": "SOFT", "code": "LIVES_ALONE", "label": "Lives alone", "weight": 1},
            ],
            "risk_blurb": "Age 77 + BMI 32 + lives alone",
            "preop_retier": False,
        },
    ]


def _battlecard_html(title: str, bullets: List[str]) -> str:
    esc = html.escape
    lis = "".join(f"<li>{esc(item)}</li>" for item in bullets)
    return (
        "<div style='font-family:Inter,Arial,sans-serif;max-width:700px;margin:0 auto;"
        "border:1px solid #dbe5ec;border-radius:12px;overflow:hidden;background:#fff;'>"
        f"<div style='background:#0ea5b3;color:#fff;padding:12px 14px;font-weight:700;'>{esc(title)}</div>"
        f"<ul style='margin:0;padding:14px 20px 16px 32px;color:#1f2937;font-size:14px;line-height:1.55;'>{lis}</ul>"
        "</div>"
    )


def _resource_entry(
    battlecard_fn: DemoBattlecardFn,
    title: str,
    bullets: List[str],
    first_name: str,
    script_suffix: str,
) -> Dict[str, Any]:
    return {
        "voice_script": f"[reassuring] {first_name}, {script_suffix}",
        "battlecard_html": battlecard_fn(title, bullets),
        "voice_audio_url": None,
    }


def build_patient_blob(
    row: Dict[str, Any],
    *,
    today: date,
    hs_id: str,
    battlecard_fn: DemoBattlecardFn,
    idx: int,
) -> Dict[str, Any]:
    first = row["name"].split()[0]
    proc = row["procedure"]
    family = row["family"]
    initial_tier = row.get("initial_tier", row["tier"])
    current_tier = row.get("current_tier", row["tier"])
    initial_reasons = list(row.get("initial_reasons", row["reasons"]))
    phase = row["phase"]
    pid = row["id"]
    phone = f"+1 (310) 555-{2100 + idx:04d}"
    email = f"{first.lower()}.{row['name'].split()[-1].lower()}@triage-demo.email".replace("'", "")
    resource_code = f"TG{idx + 1:05d}"[-8:]

    if phase == "pre_op":
        proc_d = today + timedelta(days=int(row["surgery_offset_days"]))
        if row.get("procedure_at_noon_utc"):
            proc_dt = datetime.combine(proc_d, time(12, 0), tzinfo=tz.utc)
            proc_date_iso = proc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            proc_date_iso = _iso(proc_d)
        pipeline = "pre_op"
        open_d = today
        discharge_at = None
        or_started = None
        or_ended = None
        proc_date = proc_d
    else:
        dday = int(row["episode_day"])
        open_d = today - timedelta(days=max(0, dday - 1))
        proc_date = open_d - timedelta(days=1)
        proc_date_iso = _iso(proc_date)
        pipeline = "post_op"
        discharge_at = datetime.combine(open_d, datetime.min.time()).replace(microsecond=0).isoformat()
        or_started = None
        or_ended = None

    preop_res = battlecard_fn(
        f"{proc} - Pre-Op Preparation Card",
        [
            "What to expect from check-in through recovery",
            "Day before surgery checklist and fasting plan",
            "Day of surgery arrival instructions",
            "Warning signs to report",
        ],
    )
    diag = battlecard_fn(
        f"{proc} - Diagnosis Summary",
        [
            "Procedure completed",
            "Clinical findings explained",
            "Expected recovery milestones outlined",
        ],
    )
    treat = battlecard_fn(
        f"{proc} - Recovery Plan",
        [
            "Medication and pain-control plan",
            "Diet and mobility progression",
            "When to call your care team",
        ],
    )

    resources = {"preop": _resource_entry(battlecard_fn, f"{proc} — Pre-Op", ["Prep checklist", "Fasting plan"], first, "your surgery prep is straightforward.")} if pipeline == "pre_op" else {
        "diagnosis": _resource_entry(battlecard_fn, f"{proc} — Diagnosis", ["Post-operative course"], first, "your procedure was completed and findings were reviewed."),
        "treatment": _resource_entry(battlecard_fn, f"{proc} — Treatment", ["Recovery milestones"], first, "follow your medication timing, wound care, and activity limits."),
    }
    if pipeline == "pre_op":
        main_html = resources["preop"]["battlecard_html"]
        main_script = resources["preop"]["voice_script"]
    else:
        main_html = resources["diagnosis"]["battlecard_html"]
        main_script = resources["diagnosis"]["voice_script"]

    score = row.get("score")
    init_score = row.get("initial_tier_score", score)
    blob: Dict[str, Any] = {
        "name": row["name"],
        "health_system_id": hs_id,
        "phone": phone,
        "email": email,
        "pipeline_type": pipeline,
        "phase": phase,
        "voice_audio_url": None,
        "battlecard_html": main_html,
        "avatar_url": None,
        "voice_script": main_script,
        "specialty": "Orthopedics" if family in ("LEJR", "SPINAL_FUSION", "HIP_FEMUR_FRACTURE") else "Cardiac" if family == "CABG" else "General Surgery",
        "structured_data": {
            "patient_name": row["name"],
            "procedure_name": proc,
            "procedure_date": proc_date_iso,
            "surgeon_name": TRIAGE_DEMO_SURGEON_NAME,
            "status": "scheduled" if pipeline == "pre_op" else "completed",
            "pcp_referral_sent": idx % 3 != 0,
            "pcp_name": None,
        },
        "clinic_code": TRIAGEDM_CLINIC_CODE,
        "resource_code": resource_code,
        "office_phone": "(310) 555-0200",
        "tenant_slug": TRIAGE_DEMO_SLUG,
        "eligibility_status": "ELIGIBLE",
        "anchor_procedure_family": family,
        "initial_tier": initial_tier,
        "initial_tier_score": init_score,
        "initial_tier_was_hard_escalator": bool(row.get("hard")),
        "initial_tier_reasons": initial_reasons,
        "initial_tier_assigned_at": _dt_combine(open_d),
        "current_tier": current_tier,
        "tier_last_changed": _dt_combine(today),
        "resources": resources,
        "pcp_referral_sent": idx % 3 != 0,
        "pcp_name": None,
    }
    if discharge_at:
        blob["discharge_at"] = discharge_at
        if row.get("post_intraop_tier"):
            blob["post_intraop_tier"] = row["post_intraop_tier"]
            blob["post_intraop_tier_at"] = discharge_at
        else:
            blob["post_intraop_tier"] = current_tier if not row.get("hard") else "TIER_3"
            blob["post_intraop_tier_at"] = discharge_at
    if or_started is not None:
        blob["or_started_at"] = or_started
    if or_ended is not None:
        blob["or_ended_at"] = or_ended
    return blob


def merge_triage_patients_into_store(
    patient_store: Dict[str, Any],
    *,
    battlecard_fn: DemoBattlecardFn,
    today: Optional[date] = None,
) -> None:
    today = today or date.today()
    rows = triage_patient_blueprint()
    for idx, row in enumerate(rows):
        patient_store[row["id"]] = build_patient_blob(
            row,
            today=today,
            hs_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
            battlecard_fn=battlecard_fn,
            idx=idx,
        )


def ensure_triage_demo_staff(team_store: Any) -> None:
    team_store.ensure_demo_health_system(
        hs_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        slug=TRIAGE_DEMO_SLUG,
        name=TRIAGE_HS_NAME,
        health_system_code=TRIAGEDM_CLINIC_CODE,
        phone="(310) 555-0200",
    )
    h = team_store.hash_team_password(TRIAGE_DEMO_SURGEON_PASSWORD)
    h_rn = team_store.hash_team_password(TRIAGE_DEMO_RN_PASSWORD)
    team_store.insert_team_member(
        ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        email=TRIAGE_DEMO_SURGEON_EMAIL,
        name=TRIAGE_DEMO_SURGEON_NAME,
        role="surgeon",
        password_hash=h,
        is_team_director=True,
    )
    team_store.insert_team_member(
        ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        email=TRIAGE_DEMO_RN_EMAIL,
        name=TRIAGE_DEMO_RN_NAME,
        role="rn_coordinator",
        password_hash=h_rn,
        is_team_director=False,
    )


def _clear_triage_sqlite(team_store: Any, patient_ids: List[str]) -> None:
    if not patient_ids:
        return
    import sqlite3

    placeholders = ",".join("?" for _ in patient_ids)
    with sqlite3.connect(team_store.db_path) as conn:
        conn.execute(f"DELETE FROM escalations WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM patient_self_flags WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM survey_responses WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM daily_checkin_responses WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM daily_checkin_sends WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM daily_checkin_misses WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM event_logs WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM preop_retier_events WHERE episode_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM postop_retier_events WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM intraop_reassessments WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM intraop_extractions WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM intraop_forms WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM pam_assessments WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM preop_intake_submissions WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM episode_snapshots WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM episodes WHERE patient_id IN ({placeholders})", patient_ids)
        conn.execute(f"DELETE FROM survey_sends WHERE patient_id IN ({placeholders})", patient_ids)


def _demo_survey_tier(answers: List[Dict[str, Any]]) -> tuple[Optional[float], str]:
    score_map = {
        "Very Clear": 100,
        "Strongly Agree": 100,
        "Somewhat Clear": 50,
        "Agree": 50,
        "Not Clear": 0,
        "Disagree": 0,
        "Strongly Disagree": 0,
    }
    applicable = []
    for ans in answers:
        response = (ans.get("response") or "").strip()
        if response == "Does not apply":
            continue
        applicable.append(score_map.get(response, 0))
    if not applicable:
        return None, "GREEN"
    score = round(sum(applicable) / len(applicable), 2)
    if score >= 80:
        tier = "GREEN"
    elif score >= 60:
        tier = "YELLOW"
    elif score >= 40:
        tier = "ORANGE"
    else:
        tier = "RED"
    return score, tier


def _seed_patricia_extras(team_store: Any, open_d: date) -> None:
    pid = "triage_patricia_alvarez"
    preop_answers = [{"question_index": i, "response": "Not Clear"} for i in range(1, 9)]
    sc, _tier = _demo_survey_tier(preop_answers)
    team_store.save_survey_response(
        patient_id=pid,
        survey_day=-96,
        answers=preop_answers,
        score=sc,
        tier="RED",
        submitted_at=_dt_combine(open_d - timedelta(days=4)),
        survey_type="preop",
    )
    team_store.save_pam_assessment(
        episode_id=pid,
        patient_id=pid,
        responses=[],
        raw_sum=8,
        items_scored=4,
        raw_average=2.0,
        activation_score=22.0,
        level="LOW",
        is_complete=True,
        model_version="triage-demo-seed",
        tuning_version=1,
        completed_at=_dt_combine(open_d - timedelta(days=3)),
    )
    team_store.save_preop_intake_submission(
        patient_id=pid,
        specialty="Orthopedics",
        form_template_name="THA_intake_demo",
        form_data={
            "bmi": 38,
            "smoking_status": "current",
            "pack_years": 10,
        },
        submitted_at=_dt_combine(open_d - timedelta(days=2)),
    )
    team_store.save_preop_retier_event(
        event_id=uuid.uuid4().hex,
        episode_id=pid,
        triggered_by="demo:triage-patricia-T96",
        inputs_snapshot={"source": "triage_demo_seed"},
        initial_tier="TIER_1",
        initial_tier_was_hard=False,
        computed_delta=1,
        computed_tier="TIER_2",
        tier_before="TIER_1",
        tier_after="TIER_2",
        changed=True,
        reasons=[
            {
                "kind": "SOFT",
                "code": "T96_READINESS_RED",
                "label": "T-96 readiness survey scored RED",
                "weight": 4,
            },
            {
                "kind": "SOFT",
                "code": "INTAKE_BMI_SMOKER",
                "label": "Intake form flagged: BMI 38, current smoker",
                "weight": 3,
            },
            {
                "kind": "SOFT",
                "code": "PAM_LEVEL_LOW",
                "label": "PAM activation level: LOW",
                "weight": 3,
            },
        ],
        model_version="triage-demo-seed",
        tuning_version=1,
    )


def _seed_sandra_reyes_engagement(team_store: Any, open_d: date) -> None:
    pid = "triage_sandra_reyes"
    vid_dx = {1, 2, 4, 7, 11}
    vid_tx = {2, 5, 9, 13}
    for d in range(1, 17):
        at = _dt_combine(open_d + timedelta(days=d - 1))
        if (hash(pid) + d) % 10 < 8:
            team_store.log_event(
                patient_id=pid,
                event_type="platform_opened",
                occurred_at=at,
                payload={"episode_day": d},
            )
        if d in vid_dx:
            team_store.log_event(
                patient_id=pid,
                event_type="diagnosis_video_watched",
                occurred_at=at,
                payload={"episode_day": d},
            )
        if d in vid_tx:
            team_store.log_event(
                patient_id=pid,
                event_type="treatment_video_watched",
                occurred_at=at,
                payload={"episode_day": d},
            )


def _seed_sandra_reyes_clinical(team_store: Any, patient_store: Dict[str, Any], open_d: date, today: date) -> None:
    pid = "triage_sandra_reyes"
    from triage.postop.patient_state import ensure_postop_patient_state

    _seed_sandra_reyes_engagement(team_store, open_d)
    ensure_postop_patient_state(patient_store[pid])

    team_store.save_intraop_reassessment(
        reassessment_id=uuid.uuid4().hex,
        patient_id=pid,
        intraop_form_id=f"demo-{pid}-intraop",
        form_snapshot={
            "unanticipated_event": "Intra-op blood pressure instability requiring vasopressors",
        },
        pre_or_current_tier="TIER_1",
        proposed_tier="TIER_2",
        final_tier="TIER_2",
        hard_upgrade_applied=False,
        upgrade_steps=1,
        reasons=[
            {
                "kind": "SOFT",
                "code": "INTRAOP_BP_VASOPRESSOR",
                "label": "Intra-op event: BP instability requiring vasopressors",
                "weight": 6,
            },
        ],
        is_conservative_default=False,
        procedure_family="HIP_FEMUR_FRACTURE",
        model_version="triage-demo-seed",
        tuning_version=1,
        triggered_by="demo:seed-intraop",
    )

    for d in range(1, 17):
        team_store.record_daily_checkin_send(
            patient_id=pid,
            episode_day=d,
            sent_at=_dt_combine(open_d + timedelta(days=d - 1)),
        )
        if d == 14:
            continue
        if d <= 5:
            team_store.save_daily_checkin_response(
                patient_id=pid,
                episode_day=d,
                submitted_at=_dt_combine(open_d + timedelta(days=d - 1)),
                answers={"pain_nrs": 3, "incision_flags": []},
                raw_total=88.0,
                tier="GREEN",
                red_flags=[],
                new_red_flag=False,
                wound_concern=False,
                pain_nrs=3,
                pain_trajectory="BETTER",
                item_scores={"pain_nrs": 80.0},
            )
        elif d == 6:
            team_store.save_daily_checkin_response(
                patient_id=pid,
                episode_day=d,
                submitted_at=_dt_combine(open_d + timedelta(days=d - 1)),
                answers={"pain_nrs": 5, "incision_flags": ["MISSING_PHOTO"]},
                raw_total=62.0,
                tier="ORANGE",
                red_flags=["INCISION_PHOTO_MISSED"],
                new_red_flag=True,
                wound_concern=True,
                pain_nrs=5,
                pain_trajectory="UNCHANGED",
                item_scores={"pain_nrs": 55.0},
            )
        elif d == 7:
            team_store.save_daily_checkin_response(
                patient_id=pid,
                episode_day=d,
                submitted_at=_dt_combine(open_d + timedelta(days=d - 1)),
                answers={"pain_nrs": 9, "incision_flags": []},
                raw_total=38.0,
                tier="RED",
                red_flags=[],
                new_red_flag=False,
                wound_concern=False,
                pain_nrs=9,
                pain_trajectory="WORSE",
                item_scores={"pain_nrs": 25.0},
            )
        elif 8 <= d <= 13:
            otier = "ORANGE" if d % 2 == 0 else "YELLOW"
            team_store.save_daily_checkin_response(
                patient_id=pid,
                episode_day=d,
                submitted_at=_dt_combine(open_d + timedelta(days=d - 1)),
                answers={"pain_nrs": 6, "incision_flags": []},
                raw_total=58.0 if otier == "ORANGE" else 65.0,
                tier=otier,
                red_flags=[],
                new_red_flag=False,
                wound_concern=False,
                pain_nrs=6,
                pain_trajectory="UNCHANGED",
                item_scores={"pain_nrs": 60.0},
            )
        else:
            team_store.save_daily_checkin_response(
                patient_id=pid,
                episode_day=d,
                submitted_at=_dt_combine(open_d + timedelta(days=d - 1)),
                answers={"pain_nrs": 4, "incision_flags": []},
                raw_total=78.0,
                tier="GREEN",
                red_flags=[],
                new_red_flag=False,
                wound_concern=False,
                pain_nrs=4,
                pain_trajectory="BETTER",
                item_scores={"pain_nrs": 75.0},
            )
        team_store.log_event(
            patient_id=pid,
            event_type="daily_checkin_response",
            occurred_at=_dt_combine(open_d + timedelta(days=d - 1)),
            payload={"episode_day": d},
        )

    day7_answers = [{"question_index": i, "response": "Not Clear"} for i in range(1, 9)]
    sc7, _ = _demo_survey_tier(day7_answers)
    team_store.mark_survey_sent(pid, 7, sent_at=_dt_combine(open_d + timedelta(days=6)))
    team_store.save_survey_response(
        patient_id=pid,
        survey_day=7,
        answers=day7_answers,
        score=sc7,
        tier="RED",
        submitted_at=_dt_combine(open_d + timedelta(days=6)),
        survey_type="postop",
    )
    team_store.mark_survey_sent(pid, 14, sent_at=_dt_combine(open_d + timedelta(days=13)))

    team_store.save_postop_retier_event(
        event_id=uuid.uuid4().hex,
        patient_id=pid,
        triggered_by="demo:day7-plus-checkins",
        inputs_snapshot={"day": 7},
        post_intraop_tier="TIER_2",
        computed_delta=1,
        computed_tier="TIER_3",
        tier_before="TIER_2",
        tier_after="TIER_3",
        changed=True,
        reasons=[
            {
                "kind": "SOFT",
                "code": "DAY7_RED_SURVEY",
                "label": "Day 7 survey scored RED (high pain, low recovery confidence)",
                "weight": 5,
            },
            {
                "kind": "SOFT",
                "code": "DAY6_WOUND_PHOTO",
                "label": "Day 6 check-in flagged: incision photo missed",
                "weight": 4,
            },
        ],
        model_version="triage-demo-seed",
        tuning_version=1,
    )


def seed_triage_demo_sqlite(
    team_store: Any,
    patient_store: Dict[str, Any],
    *,
    strategy: str,
    today: Optional[date] = None,
) -> None:
    """Populate SQLite rows for TRIAGEDM. Preserves idempotency when strategy=preserve."""
    today = today or date.today()
    rows = triage_patient_blueprint()
    ids = [r["id"] for r in rows]
    if strategy == "preserve":
        existing = [
            e
            for e in team_store.list_active_episodes()
            if (e.get("clinic_code") or "").upper() == TRIAGEDM_CLINIC_CODE
        ]
        if len(existing) >= len(rows):
            return
    if strategy == "reset":
        _clear_triage_sqlite(team_store, ids)

    for idx, row in enumerate(rows):
        pid = row["id"]
        p = patient_store.get(pid) or {}
        open_d: date
        if row["phase"] == "pre_op":
            open_d = today
        else:
            ed = int(row["episode_day"])
            open_d = today - timedelta(days=max(0, ed - 1))

        team_store.ensure_episode(
            patient_id=pid,
            open_date=_iso(open_d),
            procedure_type=row["procedure"],
            clinic_code=TRIAGEDM_CLINIC_CODE,
            resource_code=p.get("resource_code", ""),
            health_system_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        )

        init_tier = row.get("initial_tier", row["tier"])
        init_reasons = list(row.get("initial_reasons", row["reasons"]))
        hard = bool(row.get("hard"))
        post_iop = row.get("post_intraop_tier")
        if post_iop is None and row["phase"] == "post_op":
            post_iop = row.get("current_tier", row["tier"])

        team_store.upsert_episode_snapshot(
            pid,
            initial_tier_was_hard_escalator=hard,
            post_intake_tier=None,
            post_intraop_tier=post_iop if row["phase"] == "post_op" else None,
        )

        team_store.log_event(
            patient_id=pid,
            event_type="INITIAL_TIER_ASSIGNED",
            occurred_at=_dt_combine(open_d),
            payload={
                "tier": init_tier,
                "score": row.get("initial_tier_score", row.get("score")),
                "reasons": init_reasons,
                "reasonCodes": [r["code"] for r in init_reasons],
                "isHardEscalator": hard,
                "modelVersion": "triage-demo-seed",
                "tuningVersion": 1,
                "actor": "system:triage-demo-seed",
            },
        )

        if pid == "triage_patricia_alvarez":
            _seed_patricia_extras(team_store, open_d)

        if row.get("preop_retier") and row["phase"] == "pre_op" and pid != "triage_patricia_alvarez":
            team_store.log_event(
                patient_id=pid,
                event_type="PREOP_RETIER_TIER_UPDATED",
                occurred_at=_dt_combine(open_d + timedelta(days=1)),
                payload={
                    "tier_before": init_tier,
                    "tier_after": init_tier,
                    "note": "demo_seed_retier_signal",
                },
            )

        if row["phase"] == "post_op" and pid == "triage_sandra_reyes":
            _seed_sandra_reyes_clinical(team_store, patient_store, open_d, today)
            continue

        if row["phase"] == "post_op":
            from triage.postop.patient_state import ensure_postop_patient_state

            ensure_postop_patient_state(patient_store[pid])

            eday = int(row["episode_day"])
            team_store.record_daily_checkin_send(patient_id=pid, episode_day=eday)
            wound = bool(row.get("checkin_pain_wound"))
            bp_spike = bool(row.get("checkin_bp_spike"))
            red_flags: List[str] = []
            if wound:
                red_flags.append("INCISION_PHOTO_MISSED")
            if bp_spike:
                red_flags.append("BP_OUTLIER")
            team_store.save_daily_checkin_response(
                patient_id=pid,
                episode_day=eday,
                submitted_at=_dt_combine(today),
                answers={"pain_nrs": 8 if wound else (6 if bp_spike else 2), "incision_flags": ["MISSING_PHOTO"] if wound else []},
                raw_total=55.0 if wound else (68.0 if bp_spike else 92.0),
                tier="ORANGE" if wound or bp_spike else "GREEN",
                red_flags=red_flags,
                new_red_flag=bool(red_flags),
                wound_concern=wound,
                pain_nrs=8 if wound else 2,
                pain_trajectory="WORSE" if wound else ("UNCHANGED" if bp_spike else "BETTER"),
                item_scores={"pain_nrs": 40.0 if wound else 80.0},
            )

    # Demo escalations — mixed tiers so RN sees variety; surgeon API filters to tier 3.
    esc_spec: List[Tuple[str, int, str]] = [
        ("triage_helen_park", 1, "care_team_notification_demo"),
        ("triage_robert_chen", 2, "care_team_notification_demo"),
        ("triage_jamal_carter", 2, "care_team_notification_demo"),
        ("triage_patricia_alvarez", 2, "care_team_notification_demo"),
        ("triage_sandra_reyes", 3, "PATIENT_SELF_FLAG_ACTIVE"),
        ("triage_linda_whitfield", 3, "care_team_notification_demo"),
    ]
    for i, (pid, etier, trig) in enumerate(esc_spec):
        if pid not in patient_store:
            continue
        ep_open = date.fromisoformat((team_store.get_episode(pid) or {}).get("open_date", _iso(today)))
        created = datetime.combine(ep_open + timedelta(days=1 + i), datetime.utcnow().time()).replace(microsecond=0).isoformat()
        team_store.create_escalation(
            patient_id=pid,
            tier=etier,
            trigger_type=trig,
            message=f"Triage demo escalation tier {etier}",
            conversation_snapshot=[
                {"role": "patient", "content": "I need help from my care team."},
                {"role": "assistant", "content": "I've flagged this for clinical review."},
            ],
            created_at=created,
            health_system_id=ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID,
        )


def spinal_fusion_postop_demo_resources() -> Tuple[str, str, str, str]:
    """Canned diagnosis + treatment when Anthropic is offline (PRD §7)."""
    title_dx = "Spinal Fusion — Recovery Plan (Diagnosis)"
    title_tx = "Spinal Fusion — Recovery Plan (Treatment)"
    dx_html = _battlecard_html(
        title_dx,
        [
            "Status post L4–L5 instrumented posterior spinal fusion with intra-op dural tear repair.",
            "Neuro checks and wound surveillance per discharge instructions.",
        ],
    )
    tx_html = _battlecard_html(
        title_tx,
        [
            "Lumbar brace 6 weeks",
            "Narcotic + NSAID step-down as prescribed",
            "Neuro checks q4h x 48h",
            "No driving x 2 weeks",
            "Follow-up in 10 days",
        ],
    )
    dx_script = (
        "[clear] Your spinal fusion recovery plan emphasizes brace use, "
        "medication step-down, neuro checks, and follow-up in 10 days."
    )
    tx_script = (
        "[reassuring] Call your care team for severe new weakness, fever, or wound changes."
    )
    return dx_html, tx_html, dx_script, tx_script
