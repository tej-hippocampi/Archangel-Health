"""Pre-operative timed survey definitions, scoring, and combined readiness tiers (T-96 / T-48 / T-24)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

WINDOW_SURVEY_DAY = {"t96": -4, "t48": -2, "t24": -1}
SURVEY_DAY_TO_WINDOW = {v: k for k, v in WINDOW_SURVEY_DAY.items()}

T96_THRESHOLDS = {"green": 80, "orange": 60}
T48_THRESHOLDS = {"green": 85, "orange": 70}
T24_THRESHOLDS = {"green": 90, "orange": 75}

SCORE_MAP = {
    "Strongly Agree": 100,
    "Agree": 75,
    "Neutral": 50,
    "Disagree": 25,
    "Strongly Disagree": 0,
    "Yes": 100,
    "Partially": 50,
    "Pending": 50,
    "Unsure": 25,
    "No": 0,
    "Not prescribed": None,
    "Does not apply": None,
    "Clear": 100,
    "Yellow-tinged": 50,
    "Still brown": 0,
}

LIKERT_AGREE_LABELS = ["Strongly Agree", "Agree", "Neutral", "Disagree", "Strongly Disagree"]

# --- Question banks (adapted short-form wording; not clinical instrument reproductions) ---

T96_QUESTIONS: List[Dict[str, Any]] = [
    {
        "id": "t96_anxiety_proc",
        "text": "How anxious are you about the procedure?",
        "type": "likert_anxiety_5",
        "options": ["1", "2", "3", "4", "5"],
        "weight": 1.0,
        "red_flag": False,
    },
    {
        "id": "t96_anxiety_anesthesia",
        "text": "How anxious are you about the anesthesia?",
        "type": "likert_anxiety_5",
        "options": ["1", "2", "3", "4", "5"],
        "weight": 1.0,
        "red_flag": False,
    },
    {
        "id": "t96_understand_proc",
        "text": "I understand what will happen during and after my surgery.",
        "type": "likert_agree_5",
        "options": LIKERT_AGREE_LABELS,
        "weight": 1.0,
        "red_flag": False,
    },
    {
        "id": "t96_who_to_call",
        "text": "I know who to call if I have questions before surgery.",
        "type": "agree_disagree",
        "options": ["Agree", "Disagree"],
        "weight": 1.0,
        "red_flag": True,
    },
    {
        "id": "t96_meds_confirmed",
        "text": "I have confirmed the current list of medications I'm taking with my care team.",
        "type": "yes_no_unsure",
        "options": ["Yes", "No", "Unsure"],
        "weight": 1.0,
        "red_flag": True,
    },
    {
        "id": "t96_ride",
        "text": "I have a confirmed ride home from surgery.",
        "type": "yes_pending_no",
        "options": ["Yes", "Pending", "No"],
        "weight": 1.0,
        "red_flag": True,
    },
    {
        "id": "t96_caregiver_24h",
        "text": "I have a responsible adult who can stay with me for 24 hours after surgery.",
        "type": "yes_no_unsure",
        "options": ["Yes", "No", "Unsure"],
        "weight": 1.0,
        "red_flag": True,
    },
    {
        "id": "t96_supplies",
        "text": "Do you have all the supplies and equipment you'll need at home for recovery?",
        "type": "yes_partially_no",
        "options": ["Yes", "Partially", "No"],
        "weight": 1.0,
        "red_flag": True,
    },
]

T48_QUESTIONS: List[Dict[str, Any]] = [
    {
        "id": "t48_carb_drink",
        "text": "Have you obtained your clear carbohydrate drink (if prescribed)?",
        "type": "choice",
        "options": ["Yes", "Not prescribed", "No"],
        "weight": 1.0,
        "red_flag": True,
    },
    {
        "id": "t48_npo_understanding",
        "text": (
            "I understand I cannot eat solid food after the time my care team gave me, "
            "and cannot drink clear liquids within 2 hours before surgery unless instructed otherwise."
        ),
        "type": "likert_agree_5",
        "options": LIKERT_AGREE_LABELS,
        "weight": 1.0,
        "red_flag": True,
    },
    {
        "id": "t48_med_hold_know",
        "text": "I know which of my medications to STOP and which to CONTINUE.",
        "type": "likert_agree_5",
        "options": LIKERT_AGREE_LABELS,
        "weight": 1.0,
        "red_flag": True,
    },
    {
        "id": "t48_symptoms",
        "text": "Since your last check-in, have you had ANY of the following that are new or worse?",
        "type": "symptom_screen",
        "options": ["fever_chills_cough", "chest_pain_sob", "rash_cut_wound", "bleeding_bruising"],
        "weight": 1.0,
        "red_flag": True,
    },
    {
        "id": "t48_bowel_prep",
        "text": "If bowel prep was ordered: how clear are your stools becoming?",
        "type": "choice",
        "options": ["Clear", "Yellow-tinged", "Still brown", "Not prescribed"],
        "weight": 1.0,
        "red_flag": True,
    },
    {
        "id": "t48_anxiety_now",
        "text": "How anxious are you about surgery now?",
        "type": "likert_anxiety_5",
        "options": ["1", "2", "3", "4", "5"],
        "weight": 1.0,
        "red_flag": False,
    },
]

T24_FRAIL_ITEMS = [
    ("t24_frail_fatigue", "Are you fatigued much of the time?"),
    ("t24_frail_resistance", "Do you have difficulty walking up one flight of stairs?"),
    ("t24_frail_ambulation", "Do you have difficulty walking one block?"),
    ("t24_frail_illness", "Do you have more than five different health conditions?"),
    ("t24_frail_weight", "Have you unintentionally lost more than 5% of your weight in the past year?"),
]

T24_APFEL_ITEMS = [
    ("t24_apfel_female", "Female sex (for anesthesia nausea risk screening)?"),
    ("t24_apfel_ponv", "History of nausea or vomiting after anesthesia, or motion sickness?"),
    ("t24_apfel_smoker", "Non-smoker?"),
    ("t24_apfel_opioids", "Expecting opioid pain medicine after surgery?"),
]


def _patient_age_years(structured_data: Dict[str, Any], ref: datetime) -> Optional[int]:
    dob_raw = (structured_data or {}).get("date_of_birth") or ""
    if not dob_raw:
        return None
    try:
        dob_s = str(dob_raw).strip()[:10]
        dob = datetime.fromisoformat(dob_s).replace(tzinfo=None)
        return max(0, ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day)))
    except Exception:
        return None


def questions_for_window(window: str, structured_data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    w = (window or "").lower()
    sd = structured_data or {}
    if w == "t96":
        return [dict(q) for q in T96_QUESTIONS]
    if w == "t48":
        return [dict(q) for q in T48_QUESTIONS]
    if w == "t24":
        ref = datetime.now(timezone.utc).replace(tzinfo=None)
        age = _patient_age_years(sd, ref)
        out: List[Dict[str, Any]] = [
            {
                "id": "t24_last_solid",
                "text": "Time of your last solid food",
                "type": "datetime",
                "options": [],
                "weight": 1.0,
                "red_flag": True,
                "npo_hours": 6,
            },
            {
                "id": "t24_last_clear",
                "text": "Time of your last clear liquid",
                "type": "datetime",
                "options": [],
                "weight": 1.0,
                "red_flag": True,
                "npo_hours": 2,
            },
            {
                "id": "t24_morning_meds",
                "text": "Did you take your morning medications exactly as instructed?",
                "type": "yes_no_unsure",
                "options": ["Yes", "No", "Unsure"],
                "weight": 1.0,
                "red_flag": True,
            },
            {
                "id": "t24_ride_phone",
                "text": "Is your ride confirmed and reachable by phone?",
                "type": "yes_no",
                "options": ["Yes", "No"],
                "weight": 1.0,
                "red_flag": True,
            },
            {
                "id": "t24_adult_arrival",
                "text": "Will a responsible adult be with you when you arrive?",
                "type": "yes_no",
                "options": ["Yes", "No"],
                "weight": 1.0,
                "red_flag": True,
            },
            {
                "id": "t24_anxiety_vas",
                "text": "Rate your anxiety right now from 0 (none) to 10 (extreme).",
                "type": "vas_anxiety",
                "options": [str(i) for i in range(11)],
                "weight": 1.0,
                "red_flag": True,
            },
        ]
        if age is not None and age >= 65:
            for qid, txt in T24_FRAIL_ITEMS:
                out.append(
                    {
                        "id": qid,
                        "text": txt,
                        "type": "yes_no",
                        "options": ["Yes", "No"],
                        "weight": 1.0,
                        "red_flag": False,
                        "group": "frail",
                    }
                )
        for qid, txt in T24_APFEL_ITEMS:
            out.append(
                {
                    "id": qid,
                    "text": txt,
                    "type": "yes_no",
                    "options": ["Yes", "No"],
                    "weight": 1.0,
                    "red_flag": False,
                    "group": "apfel",
                }
            )
        out.extend(
            [
                {
                    "id": "t24_shower_prep",
                    "text": "Did you shower with the soap or wipes provided?",
                    "type": "choice",
                    "options": ["Yes", "No", "Not provided"],
                    "weight": 1.0,
                    "red_flag": True,
                },
                {
                    "id": "t24_readiness",
                    "text": "On a scale of 1–10, how ready do you feel for surgery tomorrow?",
                    "type": "readiness_1_10",
                    "options": [str(i) for i in range(1, 11)],
                    "weight": 1.0,
                    "red_flag": True,
                },
            ]
        )
        return out
    raise ValueError("window must be t96, t48, or t24")


def parse_surgery_datetime(procedure_date_str: str) -> Optional[datetime]:
    if not procedure_date_str or not str(procedure_date_str).strip():
        return None
    raw = str(procedure_date_str).strip()
    default_h = int(os.getenv("PREOP_DEFAULT_SURGERY_HOUR", "7"))
    try:
        if "T" in raw or len(raw) > 12:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        d = datetime.fromisoformat(raw[:10]).date()
        return datetime(d.year, d.month, d.day, default_h, 0, 0)
    except Exception:
        return None


def hours_until_surgery(surgery_dt: datetime, now: datetime) -> float:
    return (surgery_dt - now).total_seconds() / 3600.0


def window_hours_bounds(window: str) -> Tuple[float, float]:
    """Return (open_h, close_h] meaning open while close_h < h <= open_h."""
    w = window.lower()
    if w == "t96":
        return 96.0, 49.0
    if w == "t48":
        return 48.0, 25.0
    if w == "t24":
        return 24.0, 1.0
    raise ValueError("bad window")


def survey_window_state(window: str, hours_until: float) -> str:
    """not_yet_open | open | closed (relative to survey availability)."""
    open_h, close_h = window_hours_bounds(window)
    if hours_until > open_h:
        return "not_yet_open"
    if hours_until > close_h:
        return "open"
    return "closed"


def action_check_ready(window: str, hours_until: float) -> bool:
    """After this moment we evaluate whether required platform actions occurred on time."""
    open_h, _ = window_hours_bounds(window)
    return hours_until <= open_h


def surgery_minus_hours(surgery_dt: datetime, hours: float) -> datetime:
    return surgery_dt - timedelta(hours=hours)


def _parse_ts_answer(val: str) -> Optional[datetime]:
    if not val or not str(val).strip():
        return None
    s = str(val).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s.replace(" ", "T"))
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _score_timestamp_npo(
    last_consume: Optional[datetime], surgery_dt: datetime, min_hours_before: int
) -> Tuple[Optional[float], bool]:
    """Return (score, red_violation)."""
    if last_consume is None:
        return None, False
    need_by = surgery_dt - timedelta(hours=min_hours_before)
    delta_sec = (need_by - last_consume).total_seconds()
    if delta_sec >= 0:
        return 100.0, False
    if abs(delta_sec) <= 30 * 60:
        return 50.0, False
    return 0.0, True


def _mean(vals: List[float]) -> Optional[float]:
    return None if not vals else round(sum(vals) / len(vals), 2)


def _survey_only_tier(score: Optional[float], red_hit: bool, window: str) -> str:
    if red_hit:
        return "red"
    if score is None:
        return "pending"
    th = T96_THRESHOLDS if window == "t96" else T48_THRESHOLDS if window == "t48" else T24_THRESHOLDS
    if score >= th["green"]:
        return "green"
    if score >= th["orange"]:
        return "orange"
    return "red"


def score_preop_survey(
    window: str,
    answers: List[Dict[str, Any]],
    surgery_dt: datetime,
    structured_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Score one submission. answers: {id or question_id, response or value}."""
    qs = questions_for_window(window, structured_data)
    by_id: Dict[str, str] = {}
    for a in answers:
        qid = str(a.get("id") or a.get("question_id") or "").strip()
        resp = str(a.get("response") or a.get("value") or "").strip()
        if qid:
            by_id[qid] = resp

    per_q: List[Dict[str, Any]] = []
    scores: List[float] = []
    red_hit = False
    t24_flags: Dict[str, bool] = {}

    frail_ids = [q["id"] for q in qs if q.get("group") == "frail"]
    apfel_ids = [q["id"] for q in qs if q.get("group") == "apfel"]

    for q in qs:
        qid = q["id"]
        if q.get("group") in ("frail", "apfel"):
            continue
        qtype = q["type"]
        raw = by_id.get(qid, "").strip()
        sc: Optional[float] = None
        violated = False

        if qtype == "likert_anxiety_5":
            try:
                v = int(raw)
                if 1 <= v <= 5:
                    sc = float((6 - v) * 25)
            except Exception:
                pass
        elif qtype == "likert_agree_5":
            if raw in SCORE_MAP and SCORE_MAP[raw] is not None:
                sc = float(SCORE_MAP[raw])
        elif qtype == "agree_disagree":
            if raw == "Agree":
                sc = 100.0
            elif raw == "Disagree":
                sc = 0.0
        elif qtype in ("yes_no_unsure", "yes_pending_no", "yes_partially_no", "choice", "yes_no"):
            if raw in SCORE_MAP:
                v = SCORE_MAP[raw]
                sc = float(v) if v is not None else None
        elif qtype == "symptom_screen":
            try:
                obj = json.loads(raw) if raw.startswith("{") else {}
            except Exception:
                obj = {}
            keys = q.get("options") or []
            any_yes = any(str(obj.get(k) or "").lower() in ("yes", "true", "1") for k in keys)
            sc = 0.0 if any_yes else 100.0
            if any_yes:
                violated = True
                red_hit = True
        elif qtype == "datetime":
            dt = _parse_ts_answer(raw)
            min_h = int(q.get("npo_hours") or (6 if "solid" in qid else 2))
            sc, violated = _score_timestamp_npo(dt, surgery_dt, min_h)
        elif qtype == "vas_anxiety":
            try:
                v = int(float(raw))
                if 0 <= v <= 10:
                    sc = float((10 - v) * 10)
                    if v >= 8:
                        violated = True
                        red_hit = True
            except Exception:
                pass
        elif qtype == "readiness_1_10":
            try:
                v = int(float(raw))
                if 1 <= v <= 10:
                    sc = float((v - 1) * (100 / 9))
                    if v <= 3:
                        violated = True
                        red_hit = True
            except Exception:
                pass

        if sc is None and raw == "":
            per_q.append({"id": qid, "score": None, "red": False, "answer": raw})
            continue
        if sc is None and raw != "":
            per_q.append({"id": qid, "score": None, "red": False, "answer": raw})
            continue

        if sc is not None and q.get("red_flag") and sc == 0:
            red_hit = True
        if violated and q.get("red_flag"):
            red_hit = True

        if sc is not None:
            scores.append(float(sc))
        if qid == "t24_last_solid" and violated:
            t24_flags["npo_solid"] = True
        if qid == "t24_last_clear" and violated:
            t24_flags["npo_clear"] = True
        if qid == "t24_ride_phone" and sc == 0:
            t24_flags["no_ride"] = True
        if qid == "t24_adult_arrival" and sc == 0:
            t24_flags["no_caregiver"] = True

        per_q.append(
            {
                "id": qid,
                "score": sc,
                "red": bool(violated or (sc == 0 and bool(q.get("red_flag")))),
                "answer": raw,
            }
        )

    if frail_ids:
        yes_count = sum(1 for fid in frail_ids if str(by_id.get(fid, "")).lower() in ("yes", "true", "1"))
        frail_score = float((5 - yes_count) * 20)
        scores.append(frail_score)
        per_q.append({"id": "frail_aggregate", "score": frail_score, "red": False, "answer": f"{yes_count}_yes"})

    if apfel_ids:
        yes_count = sum(1 for aid in apfel_ids if str(by_id.get(aid, "")).lower() in ("yes", "true", "1"))
        apfel_score = float((4 - yes_count) * 25)
        scores.append(apfel_score)
        per_q.append({"id": "apfel_aggregate", "score": apfel_score, "red": False, "answer": f"{yes_count}_yes"})

    survey_score = _mean(scores)
    tier = _survey_only_tier(survey_score, red_hit, window)
    return {
        "survey_score": survey_score,
        "survey_tier": tier,
        "red_flag_hit": red_hit,
        "per_question": per_q,
        "t24_flags": t24_flags,
    }


def has_event_before(events: List[Dict[str, Any]], event_type: str, cutoff: datetime) -> bool:
    cutoff_naive = cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff
    for ev in events:
        if ev.get("event_type") != event_type:
            continue
        try:
            ts = datetime.fromisoformat(str(ev.get("occurred_at", "")).replace("Z", "+00:00"))
            if ts.tzinfo:
                ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                ts = ts.replace(tzinfo=None)
            if ts <= cutoff_naive:
                return True
        except Exception:
            continue
    return False


def first_event_ts(events: List[Dict[str, Any]], event_type: str) -> Optional[str]:
    for ev in events:
        if ev.get("event_type") == event_type:
            return str(ev.get("occurred_at") or "")
    return None


def compute_window_tier(
    *,
    patient_id: str,
    window: str,
    team_store: Any,
    patient_dict: Dict[str, Any],
    now: Optional[datetime] = None,
    survey_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Combined tier for dashboard + modal."""
    w = window.lower()
    now_dt = now or datetime.utcnow()
    if now_dt.tzinfo:
        now_dt = now_dt.astimezone(timezone.utc).replace(tzinfo=None)
    sd = patient_dict.get("structured_data") or {}
    surgery = parse_surgery_datetime(sd.get("procedure_date") or "")
    flags: List[str] = []

    if surgery is None:
        return {
            "tier": "pending",
            "score": None,
            "survey_score": None,
            "action_score": None,
            "flags": ["no_surgery_date"],
            "window": w,
            "survey_submitted": False,
            "survey_window": "unknown",
        }

    h = hours_until_surgery(surgery, now_dt)
    sw_state = survey_window_state(w, h)
    open_h, close_h = window_hours_bounds(w)

    events = team_store.get_events(patient_id)
    day = WINDOW_SURVEY_DAY[w]
    row = survey_row if survey_row is not None else team_store.get_survey_response(patient_id, day, survey_type="preop")
    ans = row.get("answers") if row else None
    if ans is None and row:
        try:
            ans = json.loads(row.get("answers_json") or "[]")
        except Exception:
            ans = []
    survey_submitted = bool(ans)

    action_ready = action_check_ready(w, h)

    # --- Action scoring & overrides ---
    t96_cut = surgery_minus_hours(surgery, 96)
    t48_cut = surgery_minus_hours(surgery, 48)
    t24_cut = surgery_minus_hours(surgery, 24)

    action_score: Optional[float] = None
    t96_ok = has_event_before(events, "intake_started", t96_cut)
    t48_intake_ok = has_event_before(events, "intake_completed", t48_cut)
    t48_vid_ok = has_event_before(events, "preop_video_watched", t48_cut)
    t24_intake_ok = has_event_before(events, "intake_completed", t24_cut)
    t24_vid_ok = has_event_before(events, "preop_video_watched", t24_cut)

    if w == "t96":
        action_score = 100.0 if t96_ok else 0.0
        if action_ready and not t96_ok:
            flags.append("missing_intake_started")
    elif w == "t48":
        parts = [100.0 if t48_intake_ok else 0.0, 100.0 if t48_vid_ok else 0.0]
        action_score = round(sum(parts) / len(parts), 2)
        if action_ready:
            if not t48_intake_ok:
                flags.append("missing_intake_completed")
            if not t48_vid_ok:
                flags.append("missing_preop_video")
    else:
        parts = [100.0 if t24_intake_ok else 0.0, 100.0 if t24_vid_ok else 0.0]
        action_score = round(sum(parts) / len(parts), 2)
        if action_ready:
            if not t24_intake_ok:
                flags.append("missing_intake_completed")
            if not t24_vid_ok:
                flags.append("missing_preop_video")

    # --- Survey branch ---
    survey_score: Optional[float] = None
    combined: Optional[float] = None
    red_survey = False
    t24_hard = False
    survey_missed = False

    if survey_submitted and row and ans:
        scored = score_preop_survey(w, list(ans), surgery, sd)
        survey_score = scored.get("survey_score")
        red_survey = bool(scored.get("red_flag_hit"))
        tf = scored.get("t24_flags") or {}
        if w == "t24" and (tf.get("npo_solid") or tf.get("npo_clear") or tf.get("no_ride") or tf.get("no_caregiver")):
            t24_hard = True
            if tf.get("npo_solid"):
                flags.append("npo_solid_violation")
            if tf.get("npo_clear"):
                flags.append("npo_clear_violation")
            if tf.get("no_ride"):
                flags.append("no_ride")
            if tf.get("no_caregiver"):
                flags.append("no_caregiver")
    elif sw_state == "closed":
        flags.append("survey_missed")
        survey_missed = True
        survey_score = 0.0
        red_survey = True

    survey_for_combine = survey_submitted or survey_missed

    # --- Combined (60/40 when both survey outcome and actions evaluable) ---
    s_val = float(survey_score) if survey_score is not None else None
    a_val = float(action_score) if action_score is not None else None

    if survey_for_combine and s_val is not None and action_ready and a_val is not None:
        combined = round(0.6 * s_val + 0.4 * a_val, 2)
    elif survey_for_combine and s_val is not None:
        combined = s_val
    elif action_ready and a_val is not None and not survey_for_combine:
        combined = a_val
    else:
        combined = None

    th = T96_THRESHOLDS if w == "t96" else T48_THRESHOLDS if w == "t48" else T24_THRESHOLDS

    tier = "pending"
    if combined is not None:
        if combined >= th["green"]:
            tier = "green"
        elif combined >= th["orange"]:
            tier = "orange"
        else:
            tier = "red"

    # Overrides → red
    if w == "t96" and action_ready and not t96_ok:
        tier = "red"
        flags.append("action_override_t96")
    if w == "t48" and action_ready and (not t48_intake_ok or not t48_vid_ok):
        tier = "red"
        flags.append("action_override_t48")
    if red_survey:
        tier = "red"
    if w == "t24" and t24_hard:
        tier = "red"

    if sw_state == "not_yet_open" and not survey_submitted:
        tier = "pending"
        combined = None

    return {
        "tier": tier,
        "score": combined,
        "survey_score": survey_score,
        "action_score": action_score if action_ready else None,
        "flags": list(dict.fromkeys(flags)),
        "window": w,
        "survey_submitted": survey_submitted,
        "survey_missed": survey_missed,
        "survey_window": sw_state,
        "hours_until_surgery": round(h, 2),
        "opens_in_hours": max(0.0, h - open_h) if sw_state == "not_yet_open" else None,
        "action_events": {
            "intake_started_ts": first_event_ts(events, "intake_started"),
            "intake_completed_ts": first_event_ts(events, "intake_completed"),
            "preop_video_watched_ts": first_event_ts(events, "preop_video_watched"),
        },
    }


def preop_escalation_trigger(window: str) -> str:
    return f"preop_window_red:{window.lower()}"
