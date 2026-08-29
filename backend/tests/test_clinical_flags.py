"""Tests for ``asclepius/clinical_flags.py`` — the clinical knowledge salvaged
out of the retired peri-op triage engine.

The module is not wired into the case pipeline, so these tests are its only
consumer and therefore its whole specification. They cover the three things the
salvage could plausibly have broken: alias resolution against analyte spellings a
real lab actually emits, the sex-specific thresholds, and ICD-10 prefix matching
against dotted and dotless codes. Plus the property that makes it safe to point
at a partner export at all: a row it cannot parse yields no flags rather than an
exception.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from asclepius.clinical_flags import (
    ICD10_PREFIX_FLAGS,
    LAB_THRESHOLDS,
    derive_lab_flags,
    derive_med_flags,
    derive_problem_flags,
)


def _lab(name: str, value, **extra) -> dict:
    """A lab row in the asclepius/partner-export spelling."""
    return {"analyte": name, "value": value, **extra}


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Alias resolution
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spelling", ["Hgb", "HAEMOGLOBIN", "hemoglobin", "HB", "Hemoglobin (B)"])
def test_hemoglobin_aliases_all_resolve_to_the_same_flag(spelling):
    assert "ANEMIA_SEVERE" in derive_lab_flags([_lab(spelling, 8.1)])


@pytest.mark.parametrize("spelling", ["HbA1c", "A1C", "hemoglobin a1c", "Haemoglobin A1c"])
def test_hba1c_aliases_all_resolve_to_the_same_flag(spelling):
    assert "GLYCEMIC_DYSCONTROL_SEVERE" in derive_lab_flags([_lab(spelling, 11.2)])


def test_an_a1c_is_not_read_as_a_hemoglobin():
    """"hb" is a substring of "hba1c" and "hemoglobin" of "hemoglobin a1c".

    A naive substring match reads an A1c of 9.8 as a hemoglobin of 9.8 and fires
    ANEMIA_SEVERE on a patient whose blood count was never drawn. That is the
    single most consequential thing the alias table can get wrong.
    """
    flags = derive_lab_flags([_lab("Hemoglobin A1c", 9.8)])
    assert "ANEMIA_SEVERE" not in flags
    assert "ANEMIA_PREOP" not in flags
    assert "GLYCEMIC_DYSCONTROL_SEVERE" in flags


def test_nt_probnp_is_not_read_as_a_bnp():
    """"bnp" is a substring of "nt-probnp". An NT-proBNP of 900 is BELOW its own
    threshold (1800) but well above BNP's (400), so the collision turns a normal
    result into an elevated one."""
    assert derive_lab_flags([_lab("NT-proBNP", 900)]) == set()
    assert "BNP_ELEVATED" in derive_lab_flags([_lab("NT-proBNP", 2400)])
    assert "BNP_ELEVATED" in derive_lab_flags([_lab("BNP", 600)])


def test_the_latest_draw_wins():
    flags = derive_lab_flags([
        _lab("Hemoglobin", 8.0, drawn_at="2026-01-01"),
        _lab("Hemoglobin", 14.0, drawn_at="2026-06-01"),
    ])
    assert flags == set()


def test_an_undated_row_never_displaces_a_dated_one():
    flags = derive_lab_flags([
        _lab("Hemoglobin", 14.0, drawn_at="2026-06-01"),
        _lab("Hemoglobin", 8.0),
    ])
    assert flags == set()


def test_the_intake_spelling_still_works():
    """``name``/``type`` is the structured-intake spelling the original consumed."""
    flags = derive_lab_flags(
        [{"name": "Hemoglobin", "value": 8.1, "drawn_at": "2026-06-01"}],
        [{"type": "ECHO", "ejection_fraction": 25}],
    )
    assert "ANEMIA_SEVERE" in flags
    assert "LOW_EJECTION_FRACTION_SEVERE" in flags


# ─────────────────────────────────────────────────────────────────────────────
# Sex-specific thresholds — both sides
# ─────────────────────────────────────────────────────────────────────────────

def test_anemia_threshold_is_sex_specific_on_both_sides():
    """12.5 g/dL is anemia in a man (<13.0) and not in a woman (>=12.0)."""
    row = [_lab("Hemoglobin", 12.5)]
    assert "ANEMIA_PREOP" in derive_lab_flags(row, sex="M")
    assert "ANEMIA_PREOP" not in derive_lab_flags(row, sex="F")

    # 11.5 is below BOTH thresholds, so sex cannot rescue it.
    low = [_lab("Hemoglobin", 11.5)]
    assert "ANEMIA_PREOP" in derive_lab_flags(low, sex="M")
    assert "ANEMIA_PREOP" in derive_lab_flags(low, sex="F")


def test_severe_anemia_is_sex_independent():
    row = [_lab("Hgb", 9.2)]
    for sex in ("M", "F", "female", None):
        flags = derive_lab_flags(row, sex=sex)
        assert {"ANEMIA_SEVERE", "ANEMIA_PREOP"} <= flags


def test_unknown_sex_uses_the_male_threshold():
    """Documented carry-over from the original: unknown sex under-flags mild
    anemia in women rather than over-flagging it in men."""
    assert "ANEMIA_PREOP" in derive_lab_flags([_lab("Hgb", 12.5)], sex=None)


# ─────────────────────────────────────────────────────────────────────────────
# Remaining lab thresholds
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "analyte,value,expected",
    [
        ("Albumin", 2.4, {"MALNUTRITION_SEVERE", "HYPOALBUMINEMIA"}),
        ("Albumin", 3.2, {"HYPOALBUMINEMIA"}),
        ("Albumin", 4.1, set()),
        ("eGFR", 22, {"RENAL_IMPAIRMENT_SEVERE", "RENAL_IMPAIRMENT"}),
        ("eGFR", 48, {"RENAL_IMPAIRMENT"}),
        ("Creatinine", 2.6, {"RENAL_IMPAIRMENT_SEVERE", "RENAL_IMPAIRMENT"}),
        ("INR", 2.3, {"COAGULOPATHY"}),
        ("INR", 1.1, set()),
        ("Lactate", 3.4, {"LACTATE_ELEVATED"}),
    ],
)
def test_single_analyte_thresholds(analyte, value, expected):
    assert derive_lab_flags([_lab(analyte, value)]) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (42, {"THROMBOCYTOPENIA_SEVERE", "THROMBOCYTOPENIA"}),   # 10³/µL units
        (42000, {"THROMBOCYTOPENIA_SEVERE", "THROMBOCYTOPENIA"}),  # /µL units
        (78, {"THROMBOCYTOPENIA"}),
        (78000, {"THROMBOCYTOPENIA"}),
        (250, set()),
        (250000, set()),
    ],
)
def test_platelet_units_are_normalized_either_way(value, expected):
    assert derive_lab_flags([_lab("Platelets", value)]) == expected


@pytest.mark.parametrize(
    "ef,expected",
    [(22, {"LOW_EJECTION_FRACTION_SEVERE"}), (35, {"LOW_EJECTION_FRACTION"}), (58, set())],
)
def test_ejection_fraction_bands(ef, expected):
    assert derive_lab_flags([], [{"modality": "ECHO", "ejection_fraction": ef}]) == expected


def test_the_lowest_ejection_fraction_wins():
    flags = derive_lab_flags([], [
        {"modality": "ECHO", "ejection_fraction": 55},
        {"modality": "ECHO", "ejection_fraction": 28},
    ])
    assert flags == {"LOW_EJECTION_FRACTION_SEVERE"}


def test_a_non_echo_study_contributes_no_ejection_fraction():
    assert derive_lab_flags([], [{"modality": "CT CHEST", "ejection_fraction": 20}]) == set()


# ─────────────────────────────────────────────────────────────────────────────
# Medications — at least one per drug class
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "drug,expected_flag",
    [
        ("Apixaban 5 mg PO BID", "ANTICOAGULANT_THERAPEUTIC"),
        ("warfarin", "ANTICOAGULANT_THERAPEUTIC"),
        ("Insulin glargine 20 units", "MED_INSULIN"),
        ("Lantus", "MED_INSULIN"),
        ("Metformin 1000 mg", "MED_ORAL_DM"),
        ("Ozempic 0.5 mg weekly", "MED_ORAL_DM"),
        ("Tacrolimus 1 mg", "IMMUNOSUPPRESSANTS"),
        ("Humira", "IMMUNOSUPPRESSANTS"),
        ("Lisinopril 10 mg", "MED_HTN_AGENT"),
        ("Amlodipine 5 mg", "MED_HTN_AGENT"),
        ("Metoprolol succinate 50 mg", "BETA_BLOCKER_ON_BOARD"),
        ("Furosemide 40 mg", "DIURETIC_LOOP"),
        ("Lasix", "DIURETIC_LOOP"),
    ],
)
def test_one_medication_per_drug_class(drug, expected_flag):
    assert expected_flag in derive_med_flags([{"drug": drug}])


def test_a_beta_blocker_is_also_an_htn_agent():
    """Metoprolol appears in both keyword tables; both flags must fire."""
    flags = derive_med_flags([{"drug": "Carvedilol 6.25 mg"}])
    assert {"BETA_BLOCKER_ON_BOARD", "MED_HTN_AGENT"} <= flags


def test_steroids_are_chronic_only_when_started_over_thirty_days_ago():
    assert derive_med_flags([{"name": "Prednisone", "start_date": _days_ago(120)}]) == {
        "CHRONIC_STEROIDS"
    }
    assert derive_med_flags([{"name": "Prednisone", "start_date": _days_ago(5)}]) == set()
    # An explicit chronic indication substitutes for the date.
    assert "CHRONIC_STEROIDS" in derive_med_flags(
        [{"name": "Prednisone", "indication": "Chronic asthma"}]
    )
    # A steroid with no date and no indication is not assumed chronic.
    assert derive_med_flags([{"name": "Dexamethasone"}]) == set()


def test_opioids_are_chronic_only_beyond_ninety_days():
    assert derive_med_flags([{"name": "Oxycodone", "start_date": _days_ago(200)}]) == {
        "CHRONIC_OPIOIDS"
    }
    assert derive_med_flags([{"name": "Oxycodone", "start_date": _days_ago(40)}]) == set()


def test_dual_antiplatelet_needs_two_distinct_agents():
    assert "DUAL_ANTIPLATELET" in derive_med_flags(
        [{"drug": "Aspirin 81 mg"}, {"drug": "Clopidogrel 75 mg"}]
    )
    # Two orders for the SAME drug are not dual therapy.
    assert "DUAL_ANTIPLATELET" not in derive_med_flags(
        [{"drug": "Aspirin 81 mg"}, {"drug": "aspirin 325 mg PRN"}]
    )
    # Nor is the same drug arriving under its brand and generic names, which a
    # merged multi-format export produces routinely.
    assert "DUAL_ANTIPLATELET" not in derive_med_flags(
        [{"drug": "Plavix 75 mg"}, {"drug": "clopidogrel 75 mg"}]
    )


def test_asa_is_matched_as_a_token_not_a_substring():
    """A bare "asa" substring match makes mesalamine ("Asacol") an antiplatelet."""
    assert derive_med_flags([{"drug": "Asacol HD 800 mg"}]) == set()
    assert "DUAL_ANTIPLATELET" not in derive_med_flags(
        [{"drug": "Asacol 800 mg"}, {"drug": "Aspirin 81 mg"}]
    )
    # The real abbreviation still matches as its own word.
    assert "DUAL_ANTIPLATELET" in derive_med_flags(
        [{"drug": "ASA 81 mg PO daily"}, {"drug": "Ticagrelor 90 mg"}]
    )


def test_polypharmacy_at_ten_named_medications():
    nine = [{"drug": f"Drug{i} 10 mg"} for i in range(9)]
    assert "POLYPHARMACY_HIGH" not in derive_med_flags(nine)
    assert "POLYPHARMACY_HIGH" in derive_med_flags(nine + [{"drug": "Drug9 10 mg"}])
    # Blank rows are not drugs and must not pad the count to ten.
    assert "POLYPHARMACY_HIGH" not in derive_med_flags(nine + [{"drug": ""}, {}])


# ─────────────────────────────────────────────────────────────────────────────
# ICD-10 — dotted and dotless
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "code,expected",
    [
        ("I10", "HTN_PRESENT"),
        ("i10", "HTN_PRESENT"),
        ("I1 0.", "HTN_PRESENT"),          # OCR spacing + trailing dot
        ("I10.9", "HTN_PRESENT"),
        ("I25.10", "CAD"),
        ("I2510", "CAD"),                  # dotless
        ("J44.9", "SEVERE_COPD"),
        ("E11.65", "DIABETES_TYPE_2"),
        ("N18.5", "DIALYSIS_DEPENDENT"),
        ("N185", "DIALYSIS_DEPENDENT"),    # dotless form of a dotted prefix
        ("C79.51", "DISSEMINATED_CANCER"),
        ("G47.33", "OBSTRUCTIVE_SLEEP_APNEA"),
        ("A41.9", "SEPSIS_48H"),
        ("Z99.11", "VENTILATOR_DEPENDENT"),
    ],
)
def test_icd10_prefix_matching_dotted_and_dotless(code, expected):
    assert expected in derive_problem_flags([{"icd10": code}])


def test_a_dotted_prefix_does_not_match_a_different_subcode():
    """N18.5 is dialysis-dependent ESRD; N18.3 (stage 3 CKD) is not."""
    assert "DIALYSIS_DEPENDENT" not in derive_problem_flags([{"icd10": "N18.3"}])


def test_a_resolved_problem_contributes_nothing():
    assert derive_problem_flags([{"icd10": "I50.9", "status": "RESOLVED"}]) == set()


def test_chf_branches_on_the_severity_note_of_its_own_row():
    recent = derive_problem_flags(
        [{"icd10": "I50.21", "severity_note": "Acute decompensated CHF"}]
    )
    assert {"CHF_PRESENT", "CHF_RECENT"} <= recent
    assert "CHF_HISTORY_NOT_RECENT" not in recent

    historical = derive_problem_flags([{"icd10": "I50.9"}])
    assert {"CHF_PRESENT", "CHF_HISTORY_NOT_RECENT"} <= historical
    assert "CHF_RECENT" not in historical


def test_an_unrelated_rows_severity_note_cannot_make_chf_recent():
    """The sharp edge of the cross-row leak the salvage closes.

    The original re-ran the CHF branch for every problem once ANY row had set
    CHF_PRESENT, reading that row's severity note. So a compensated, chronic heart
    failure followed by an unrelated acute pancreatitis yielded CHF_RECENT — an
    acute-decompensation claim derived from a note about the pancreas.
    """
    flags = derive_problem_flags([
        {"icd10": "I50.9", "severity_note": "stable, compensated"},
        {"icd10": "K85.9", "severity_note": "acute pancreatitis"},
    ])
    assert "CHF_RECENT" not in flags
    assert {"CHF_PRESENT", "CHF_HISTORY_NOT_RECENT"} <= flags


def test_a_later_problem_row_cannot_downgrade_an_earned_chf_recent():
    """The same leak in the other direction: a genuinely recent decompensation
    must survive the rows listed after it."""
    flags = derive_problem_flags([
        {"icd10": "I50.21", "severity_note": "acute decompensation"},
        {"icd10": "E11.9"},
        {"icd10": "I10"},
    ])
    assert "CHF_RECENT" in flags
    assert "CHF_HISTORY_NOT_RECENT" not in flags


def test_functional_status_and_bmi_are_keyword_arguments():
    assert derive_problem_flags([], functional_status="TOTALLY_DEPENDENT") == {
        "FUNCTIONAL_TOTALLY_DEPENDENT"
    }
    assert derive_problem_flags([], functional_status="PARTIALLY_DEPENDENT") == {
        "FUNCTIONAL_PARTIALLY_DEPENDENT"
    }
    assert derive_problem_flags([], functional_status="INDEPENDENT") == set()
    assert derive_problem_flags([], bmi=44.1) == {"BMI_OVER_40"}
    assert derive_problem_flags([], bmi=17.2) == {"BMI_UNDER_18_5"}
    assert derive_problem_flags([], bmi=26.0) == set()


def test_free_text_conditions_are_not_string_matched_into_codes():
    """The asclepius export carries ``condition`` prose and no ICD-10. Guessing a
    code from prose is a clinical claim this module does not make."""
    assert derive_problem_flags([{"condition": "Congestive heart failure"}]) == set()


# ─────────────────────────────────────────────────────────────────────────────
# Unknown input returns no flags rather than raising
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("garbage", [None, [], {}, "not a list", 42, [None, "x", 7], [{}]])
def test_unknown_input_yields_no_flags_and_does_not_raise(garbage):
    assert derive_lab_flags(garbage, garbage) == set()
    assert derive_med_flags(garbage) == set()
    assert derive_problem_flags(garbage) == set()


def test_a_qualitative_lab_value_is_skipped_not_crashed_on():
    """Real exports put ``"POSITIVE"`` and ``"GRANULAR CAST (02) /HPF"`` in the
    same value column as numbers."""
    flags = derive_lab_flags([
        _lab("Hemoglobin", "POSITIVE"),
        _lab("Urine Casts", "GRANULAR CAST (02) /HPF"),
        _lab("Albumin", "2.4"),   # numeric-as-string still counts
    ])
    assert flags == {"MALNUTRITION_SEVERE", "HYPOALBUMINEMIA"}


def test_a_qualitative_row_does_not_shadow_a_numeric_one_of_the_same_analyte():
    """The unparseable row is the LATEST draw. It must be skipped, not allowed to
    hide the numeric result behind it."""
    flags = derive_lab_flags([
        _lab("Hemoglobin", 8.1, drawn_at="2026-01-01"),
        _lab("Hemoglobin", "HEMOLYZED", drawn_at="2026-06-01"),
    ])
    assert "ANEMIA_SEVERE" in flags


def test_a_boolean_is_not_a_lab_value():
    """``True`` is an ``int`` in Python; without a guard it reads as 1.0 g/dL."""
    assert derive_lab_flags([_lab("Hemoglobin", True)]) == set()


def test_an_unparseable_start_date_does_not_raise():
    assert derive_med_flags([{"name": "Prednisone", "start_date": "last spring"}]) == set()
    assert derive_med_flags([{"name": "Prednisone", "start_date": 20240101}]) == set()


# ─────────────────────────────────────────────────────────────────────────────
# Table integrity
# ─────────────────────────────────────────────────────────────────────────────

def test_the_threshold_table_carries_every_key_the_derivers_read():
    expected = {
        "anemia_preop_hb_women", "anemia_preop_hb_men", "anemia_severe_hb",
        "albumin_low", "albumin_malnutrition", "egfr_low", "egfr_severe",
        "creatinine_severe", "hba1c_elevated", "hba1c_severe", "inr_coagulopathy",
        "platelets_low", "platelets_severe", "bnp", "nt_pro_bnp", "lactate",
        "ef_low", "ef_severe",
    }
    assert expected == set(LAB_THRESHOLDS)


def test_severe_thresholds_are_stricter_than_their_non_severe_twins():
    assert LAB_THRESHOLDS["anemia_severe_hb"] < LAB_THRESHOLDS["anemia_preop_hb_women"]
    assert LAB_THRESHOLDS["albumin_malnutrition"] < LAB_THRESHOLDS["albumin_low"]
    assert LAB_THRESHOLDS["egfr_severe"] < LAB_THRESHOLDS["egfr_low"]
    assert LAB_THRESHOLDS["hba1c_severe"] > LAB_THRESHOLDS["hba1c_elevated"]
    assert LAB_THRESHOLDS["platelets_severe"] < LAB_THRESHOLDS["platelets_low"]
    assert LAB_THRESHOLDS["ef_severe"] < LAB_THRESHOLDS["ef_low"]


def test_icd10_prefixes_are_uppercase_and_unique_per_pair():
    assert len(ICD10_PREFIX_FLAGS) == len(set(ICD10_PREFIX_FLAGS))
    for prefix, flag in ICD10_PREFIX_FLAGS:
        assert prefix == prefix.upper()
        assert flag == flag.upper()


def test_the_module_imports_nothing_from_the_retired_peri_op_packages():
    """The whole point of the salvage: this module must survive ``git rm triage/``.

    Checked against the parsed import statements rather than the source text —
    the docstrings name ``triage.tuning`` deliberately, to say where the numbers
    came from, and a grep-shaped test would fail on that prose while missing a
    real import written as ``importlib.import_module("triage")``.
    """
    import ast
    import inspect

    from asclepius import clinical_flags

    retired = {"triage", "pipeline", "prompts", "eligibility", "telehealth"}
    imported: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(clinical_flags))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert not (imported & retired), f"imports a retired package: {imported & retired}"
    # Nothing dynamic either — the module is pure stdlib.
    assert imported <= {"__future__", "datetime", "typing"}


def test_every_antiplatelet_keyword_has_an_agent_mapping():
    """``derive_med_flags`` indexes ``_ANTIPLATELET_AGENTS`` by matched keyword.

    A keyword added to the match tuple without a mapping entry would KeyError on
    the first real chart containing that drug — a crash in a deriver whose whole
    contract is that it never raises on clinical input. Asserted here so the gap
    fails in CI rather than in front of a physician.
    """
    from asclepius.clinical_flags import _ANTIPLATELET_AGENTS, _ANTIPLATELET_KEYWORDS

    missing = set(_ANTIPLATELET_KEYWORDS) - set(_ANTIPLATELET_AGENTS)
    assert not missing, f"antiplatelet keywords with no agent mapping: {missing}"


def test_every_antiplatelet_keyword_actually_fires_dual_therapy():
    """The mapping is right end-to-end: each keyword pairs with a different agent
    to make DAPT, and never pairs with its own synonym to make it."""
    from asclepius.clinical_flags import _ANTIPLATELET_AGENTS, _ANTIPLATELET_KEYWORDS

    for keyword in _ANTIPLATELET_KEYWORDS:
        other = next(k for k in _ANTIPLATELET_KEYWORDS
                     if _ANTIPLATELET_AGENTS[k] != _ANTIPLATELET_AGENTS[keyword])
        pair = derive_med_flags([{"drug": f"{keyword} 10 mg"}, {"drug": f"{other} 10 mg"}])
        assert "DUAL_ANTIPLATELET" in pair, (keyword, other)

        synonym = next((k for k in _ANTIPLATELET_KEYWORDS
                        if k != keyword
                        and _ANTIPLATELET_AGENTS[k] == _ANTIPLATELET_AGENTS[keyword]), None)
        if synonym:
            same = derive_med_flags(
                [{"drug": f"{keyword} 10 mg"}, {"drug": f"{synonym} 10 mg"}])
            assert "DUAL_ANTIPLATELET" not in same, (keyword, synonym)
