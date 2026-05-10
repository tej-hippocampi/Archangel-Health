# TEAM Eligibility validation fixtures

Synthetic eligibility documents covering the 6 TEAM checks. Used by `test_eligibility_validation_set.py`
to exercise the parsers + evaluator without calling the real Anthropic API.

Each fixture is a dict in `validation_cases.py`:

```python
{
  "id": "case-001",
  "name": "Original Medicare, all clean",
  "x12_271": "...envelope text...",   # optional
  "csv": "...csv text...",             # optional
  "pdf_text": "...rendered text...",   # optional
  "surgery_date": "2026-06-01",
  "expected_extraction": { ...the LLM-tool output we expect... },
  "expected_overall": "ELIGIBLE" | "INELIGIBLE" | "BLOCKED_UNKNOWN",
}
```

The validation test:
1. Routes each fixture's source bytes through the appropriate parser (X12/CSV).
2. Substitutes the canonical LLM extraction (no live model call).
3. Asserts the deterministic evaluator returns the expected verdict.

This guarantees regressions in:
- X12 parser shape changes
- CSV alias resolution drift
- evaluator field semantics

Real first-pass field-accuracy benchmarking against live Claude is tracked separately
(see PRD §12.18). It is intentionally out of scope for the in-repo test suite.
