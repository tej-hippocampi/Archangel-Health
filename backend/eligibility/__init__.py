"""TEAM eligibility determination — parse, extract, evaluate, orchestrate.

Modules:
- format_detect: magic-byte + extension routing
- parse_x12: X12 271 envelope → AST
- parse_pdf: pdfminer.six + pytesseract OCR fallback
- parse_csv: header-alias resolver with stdlib csv
- extract: Anthropic tool-use call for the 6 checks
- evaluate: deterministic verdict logic
- pipeline: async orchestrator (parse → extract → evaluate) with SSE queue
- store: in-memory check/document/audit stores + rate limiter
"""
