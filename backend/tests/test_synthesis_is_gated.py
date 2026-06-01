from __future__ import annotations

import pathlib

EXEMPT_MARKER = "# gated-synth-exempt:"


def test_no_ungated_script_synthesis():
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(root))
        if rel.startswith("tests/") or rel == "pipeline/gated_synthesis.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines()):
            if "ElevenLabsClient().synthesize(" in line and EXEMPT_MARKER not in line:
                offenders.append(f"{rel}:{i + 1}")
    assert not offenders, (
        "Direct ElevenLabs synthesis outside the chokepoint. Route through "
        "pipeline/gated_synthesis.synthesize_script, or annotate with "
        f"'{EXEMPT_MARKER} <reason>' for non patient-education calls: {offenders}"
    )
