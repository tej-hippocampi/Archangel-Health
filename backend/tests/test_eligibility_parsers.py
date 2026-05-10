"""Unit tests for X12 + CSV + format detection (PRD §12.4)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eligibility.format_detect import detect_format  # noqa: E402
from eligibility.parse_csv import parse_csv, split_by_mbi  # noqa: E402
from eligibility.parse_x12 import format_for_llm, parse_x12_271  # noqa: E402


X12_SAMPLE = (
    "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
    "*230101*0000*^*00501*000000001*0*T*:~"
    "GS*HB*SENDER*RECEIVER*20230101*0000*1*X*005010X279A1~"
    "ST*271*0001*005010X279A1~"
    "NM1*IL*1*DOE*JANE****MI*1EG4TE5MK73~"
    "EB*1*FAM*MA*****~"
    "DTP*346*D8*20200101~"
    "DTP*347*D8*20991231~"
    "EB*1*FAM*MB*****~"
    "DTP*346*D8*20200101~"
    "EB*6*FAM*30*****~"
    "NM1*PR*2*HUMANA GOLD PLUS~"
    "REF*18*H1036~"
    "MSG*Member is enrolled in Medicare Advantage~"
    "SE*13*0001~GE*1*1~IEA*1*000000001~"
)


def test_detect_format_pdf_magic():
    assert detect_format("anything.txt", b"%PDF-1.4 ...") == "PDF"


def test_detect_format_x12_envelope():
    assert detect_format("eligibility.txt", X12_SAMPLE.encode()) == "X12_271"


def test_detect_format_csv_extension():
    assert detect_format("roster.csv", b"a,b,c\n1,2,3\n") == "CSV"


def test_x12_subscriber_and_benefits():
    ast = parse_x12_271(X12_SAMPLE)
    assert ast.subscriber["last_name"] == "DOE"
    assert ast.subscriber["first_name"] == "JANE"
    assert ast.subscriber["id_qualifier"] == "MI"
    assert ast.subscriber["id_code"] == "1EG4TE5MK73"
    # 3 benefits: MA, MB, 30 (MA Plan)
    assert len(ast.benefits) == 3
    assert ast.benefits[0].eb03 == "MA"
    assert ast.benefits[1].eb03 == "MB"
    assert ast.benefits[2].eb03 == "30"
    # Plan/Contract Number on the MA Plan benefit
    assert ast.benefits[2].contract_id == "H1036"
    assert ast.benefits[2].payer_name == "HUMANA GOLD PLUS"
    text = format_for_llm(ast)
    assert "EB03=MA" in text
    assert "Humana Gold Plus".upper() in text.upper()


def test_csv_alias_resolution_and_split():
    csv_text = (
        "first_name,last_name,DOB,medicare_id,part_a_effective,part_b_effective,ma_plan,scheduled_surgery_date\n"
        "Jane,Doe,1958-04-12,1EG4TE5MK73,2020-01-01,2020-01-01,,2026-06-01\n"
        "John,Smith,1955-12-30,2AB1CD2EF34,2018-06-01,2018-06-01,H1234,2026-07-15\n"
    )
    res = parse_csv(csv_text)
    assert res.row_count == 2
    assert res.resolved.get("mbi") == "medicare_id"
    assert res.resolved.get("partA_eff") == "part_a_effective"
    assert res.resolved.get("ma_plan_id") == "ma_plan"
    assert res.needs_llm is False
    groups = split_by_mbi(res)
    assert len(groups) == 2  # one row per MBI


def test_csv_unknown_headers_fall_back_to_llm():
    csv_text = "name,policy,issued\nJane,foo,bar\n"
    res = parse_csv(csv_text)
    assert res.needs_llm is True


# ─── Format-detect edge cases ──────────────────────────────────────────────
def test_format_detect_isabel_not_mistaken_for_x12():
    """Reject anything starting with the literal 'ISA' but no element delimiter."""
    assert detect_format("note.txt", b"ISABEL was here\n") == "OTHER"
    assert detect_format("note.txt", b"ISAAC is a name\n") == "OTHER"


def test_format_detect_x12_with_pipe_delimiter():
    """X12 spec allows non-'*' element delimiters — accept pipe/^/etc."""
    assert detect_format("anon.txt", b"ISA|00|          |00|...") == "X12_271"
    assert detect_format("anon.txt", b"ISA^00^          ^00^...") == "X12_271"


def test_format_detect_pdf_magic_overrides_extension():
    assert detect_format("eligibility.csv", b"%PDF-1.7 ...") == "PDF"


def test_format_detect_csv_with_only_tabs():
    assert detect_format("data.tsv", b"a\tb\tc\n1\t2\t3\n") == "CSV"


def test_format_detect_no_extension_returns_other():
    assert detect_format("attachment", b"random text\n") == "OTHER"


def test_format_detect_empty_bytes():
    assert detect_format("anything.csv", b"") == "OTHER"


# ─── X12 edge cases ────────────────────────────────────────────────────────
def test_x12_aaa_error_segment():
    """When the payer rejects, AAA segments appear with a reject_reason code."""
    payload = (
        "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
        "*230101*0000*^*00501*000000001*0*T*:~"
        "ST*271*0001*005010X279A1~"
        "AAA*Y**42*N~"  # 42 = "Unable to Respond at Current Time"
        "NM1*IL*1*DOE*JANE****MI*1EG4TE5MK73~"
        "SE*4*0001~"
    )
    ast = parse_x12_271(payload)
    assert len(ast.errors) >= 1
    assert ast.errors[0].reject_reason == "42"
    text = format_for_llm(ast)
    assert "AAA" in text


def test_x12_invalid_envelope_raises():
    from eligibility.parse_x12 import InvalidX12Error
    import pytest as _pytest
    with _pytest.raises(InvalidX12Error):
        parse_x12_271("not valid x12 at all")


# ─── CSV edge cases ────────────────────────────────────────────────────────
def test_csv_split_by_mbi_groups_dupes():
    """Two rows with the same MBI should be grouped under one patient."""
    csv_text = (
        "first_name,last_name,medicare_id\n"
        "Jane,Doe,1EG4TE5MK73\n"
        "Jane,Doe,1EG4TE5MK73\n"
        "John,Smith,2AB1CD2EF34\n"
    )
    res = parse_csv(csv_text)
    groups = split_by_mbi(res)
    # One group per distinct MBI
    assert len(groups) == 2
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 2]


def test_csv_empty_returns_zero_rows():
    res = parse_csv("first_name,last_name\n")
    assert res.row_count == 0


def test_csv_strips_whitespace_in_cells():
    res = parse_csv("first_name,last_name,medicare_id\n  Jane  ,Doe ,1EG4TE5MK73\n")
    assert res.rows[0].get("first_name", "").strip() == "Jane"
