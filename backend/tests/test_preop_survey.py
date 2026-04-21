import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from team_store import TeamStore
from preop_survey import (
    compute_window_tier,
    hours_until_surgery,
    parse_surgery_datetime,
    score_preop_survey,
    survey_window_state,
)


def _surgery_dt():
    return datetime(2030, 6, 15, 7, 0, 0)


class ScorePreopSurveyTests(unittest.TestCase):
    def test_t96_green_set(self):
        surgery = _surgery_dt()
        answers = [
            {"id": "t96_anxiety_proc", "response": "2"},
            {"id": "t96_anxiety_anesthesia", "response": "2"},
            {"id": "t96_understand_proc", "response": "Strongly Agree"},
            {"id": "t96_who_to_call", "response": "Agree"},
            {"id": "t96_meds_confirmed", "response": "Yes"},
            {"id": "t96_ride", "response": "Yes"},
            {"id": "t96_caregiver_24h", "response": "Yes"},
            {"id": "t96_supplies", "response": "Yes"},
        ]
        out = score_preop_survey("t96", answers, surgery, {})
        self.assertIsNotNone(out["survey_score"])
        self.assertGreaterEqual(out["survey_score"], 80)
        self.assertEqual(out["survey_tier"], "green")
        self.assertFalse(out["red_flag_hit"])

    def test_red_flag_forces_red(self):
        surgery = _surgery_dt()
        answers = [
            {"id": "t96_anxiety_proc", "response": "1"},
            {"id": "t96_anxiety_anesthesia", "response": "1"},
            {"id": "t96_understand_proc", "response": "Strongly Agree"},
            {"id": "t96_who_to_call", "response": "Agree"},
            {"id": "t96_meds_confirmed", "response": "No"},
            {"id": "t96_ride", "response": "Yes"},
            {"id": "t96_caregiver_24h", "response": "Yes"},
            {"id": "t96_supplies", "response": "Yes"},
        ]
        out = score_preop_survey("t96", answers, surgery, {})
        self.assertTrue(out["red_flag_hit"])
        self.assertEqual(out["survey_tier"], "red")

    def test_t24_npo_violation_flags(self):
        surgery = _surgery_dt()
        bad_solid = (surgery - timedelta(hours=3)).isoformat(timespec="minutes")
        ok_clear = (surgery - timedelta(hours=4)).isoformat(timespec="minutes")
        answers = [
            {"id": "t24_last_solid", "response": bad_solid},
            {"id": "t24_last_clear", "response": ok_clear},
            {"id": "t24_morning_meds", "response": "Yes"},
            {"id": "t24_ride_phone", "response": "Yes"},
            {"id": "t24_adult_arrival", "response": "Yes"},
            {"id": "t24_anxiety_vas", "response": "3"},
            {"id": "t24_apfel_female", "response": "No"},
            {"id": "t24_apfel_ponv", "response": "No"},
            {"id": "t24_apfel_smoker", "response": "No"},
            {"id": "t24_apfel_opioids", "response": "No"},
            {"id": "t24_shower_prep", "response": "Yes"},
            {"id": "t24_readiness", "response": "8"},
        ]
        out = score_preop_survey("t24", answers, surgery, {})
        self.assertTrue(out["t24_flags"].get("npo_solid"))
        self.assertTrue(out["red_flag_hit"])


class ComputeWindowTierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = TeamStore(db_path=self.tmp.name)
        self.pid = "test_preop_pt_1"
        self.patient = {
            "structured_data": {"procedure_date": "2030-06-15"},
            "health_system_id": None,
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_pending_before_window(self):
        now = datetime(2030, 6, 10, 12, 0, 0)
        info = compute_window_tier(
            patient_id=self.pid,
            window="t96",
            team_store=self.store,
            patient_dict=self.patient,
            now=now,
        )
        self.assertEqual(info["tier"], "pending")

    def test_t96_red_without_intake_started(self):
        now = datetime(2030, 6, 12, 12, 0, 0)
        h = hours_until_surgery(parse_surgery_datetime("2030-06-15"), now)
        self.assertEqual(survey_window_state("t96", h), "open")
        info = compute_window_tier(
            patient_id=self.pid,
            window="t96",
            team_store=self.store,
            patient_dict=self.patient,
            now=now,
        )
        self.assertIn("missing_intake_started", info["flags"])
        self.assertEqual(info["tier"], "red")

    def test_t96_green_with_intake_and_survey(self):
        cut = parse_surgery_datetime("2030-06-15") - timedelta(hours=96)
        self.store.log_event(
            patient_id=self.pid,
            event_type="intake_started",
            occurred_at=cut.isoformat(),
            payload={},
        )
        now = datetime(2030, 6, 12, 12, 0, 0)
        answers = [
            {"id": "t96_anxiety_proc", "response": "2"},
            {"id": "t96_anxiety_anesthesia", "response": "2"},
            {"id": "t96_understand_proc", "response": "Strongly Agree"},
            {"id": "t96_who_to_call", "response": "Agree"},
            {"id": "t96_meds_confirmed", "response": "Yes"},
            {"id": "t96_ride", "response": "Yes"},
            {"id": "t96_caregiver_24h", "response": "Yes"},
            {"id": "t96_supplies", "response": "Yes"},
        ]
        row = {
            "answers": answers,
            "answers_json": json.dumps(answers),
        }
        info = compute_window_tier(
            patient_id=self.pid,
            window="t96",
            team_store=self.store,
            patient_dict=self.patient,
            now=now,
            survey_row=row,
        )
        self.assertTrue(info["survey_submitted"])
        self.assertEqual(info["tier"], "green")


if __name__ == "__main__":
    unittest.main()
