"""Unit tests for intake section chat parsing, tool extraction, and section-5 med buckets."""

import json
import unittest
from types import SimpleNamespace
from typing import Any

import intake_section_chat as isc
from intake_section5_normalize import normalize_section5_field_updates


class IntakeModelJsonParseTests(unittest.TestCase):
    def test_strips_fences(self):
        raw = '```json\n{"assistantReply": "Hi", "fieldUpdates": {}, "sectionComplete": false}\n```'
        d = isc._parse_intake_model_json(raw)
        self.assertEqual(d.get("assistantReply"), "Hi")

    def test_substring_braces(self):
        raw = "Here you go: {\"assistantReply\": \"x\", \"fieldUpdates\": {}, \"sectionComplete\": true} trailing"
        d = isc._parse_intake_model_json(raw)
        self.assertEqual(d.get("assistantReply"), "x")
        self.assertTrue(d.get("sectionComplete"))


class CoerceParsedTests(unittest.TestCase):
    def test_requires_assistant_reply(self):
        self.assertIsNone(isc._coerce_parsed({"fieldUpdates": {}}))

    def test_field_updates_string_to_dict(self):
        c = isc._coerce_parsed(
            {
                "assistantReply": "ok",
                "fieldUpdates": json.dumps({"a": 1}),
                "sectionComplete": True,
            }
        )
        self.assertIsNotNone(c)
        self.assertEqual(c["fieldUpdates"], {"a": 1})


class ToolExtractionTests(unittest.TestCase):
    def test_extracts_tool_input(self):
        def block(name, inp):
            return SimpleNamespace(
                type="tool_use", name=name, id="t1", input=inp
            )

        resp = SimpleNamespace(
            content=[block(isc.INTAKE_TURN_TOOL_NAME, {"assistantReply": "A", "fieldUpdates": {"x": 1}, "sectionComplete": False})]
        )
        p = isc._extract_tool_parsed(resp)
        self.assertIsNotNone(p)
        self.assertEqual(p["assistantReply"], "A")
        self.assertEqual(p["fieldUpdates"], {"x": 1})


class Section5NormalizeTests(unittest.TestCase):
    def test_moves_melatonin_fish_oil(self):
        upd: Any = {
            "currentMedications": {
                "value": [
                    "Lexapro 10mg",
                    "melatonin 3mg",
                    "fish oil 1000mg",
                ],
                "source": "interview",
            }
        }
        normalize_section5_field_updates(upd)
        cur = upd.get("currentMedications", {})
        otc = upd.get("herbalSupplementsOTC", {})
        self.assertEqual(
            cur.get("value", []),
            ["Lexapro 10mg"],
        )
        self.assertCountEqual(
            otc.get("value", []),
            ["melatonin 3mg", "fish oil 1000mg"],
        )

    def test_idempotent_on_rx_only(self):
        upd: Any = {
            "currentMedications": {
                "value": ["Aspirin 81mg (prescribed by cardiology)"],
                "source": "interview",
            }
        }
        before = json.dumps(upd, sort_keys=True)
        normalize_section5_field_updates(upd)
        self.assertEqual(before, json.dumps(upd, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
