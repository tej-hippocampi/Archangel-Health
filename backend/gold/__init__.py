"""Gold Standard — clinical conversation gold-data capture (PRD GoldCapture v0.1).

In-visit capture + surgeon review workflow that turns doctor–patient
conversations into clinician-verified, de-identified, schema-valid JSONL
records sold as supervised-fine-tuning / gold-evaluation data.

This package is an independent artifact — it never writes any triage
``Episode.tier`` field. See ``backend/routers/gold.py`` for the HTTP surface.
"""
