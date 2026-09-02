"""
Tests for the deterministic classifier.

These are the tests that matter most in this project: because no model
participates in classification, every severity decision is pinned to an exact
expected value here.
"""

from __future__ import annotations

import pytest

from mcp_server.engine import (
    KnowledgeBase,
    classify,
    deviation_index,
    normalize_name,
    parse_range_text,
    parse_value,
)


@pytest.fixture(scope="module")
def kb() -> KnowledgeBase:
    return KnowledgeBase()


def status_of(kb, name, value, unit=None, **kw) -> str:
    return classify(kb, name, value, unit, **kw)["status"]


# -- name normalisation and resolution ----------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Hemoglobin", "hemoglobin"),
    ("İnsülin", "insulin"),
    ("Nötrofil%", "notrofil percent"),
    ("Glikozile Hemoglobin (HbA1c)", "glikozile hemoglobin hba1c"),
    ("Ürobilinojen (Strip)", "urobilinojen strip"),
    ("  RDW-SD  ", "rdw sd"),
])
def test_normalize_name_handles_turkish_and_punctuation(raw, expected):
    assert normalize_name(raw) == expected


def test_strip_qualifier_is_not_collapsed(kb):
    """'Lokosit' is a blood count; 'Lokosit (Strip)' is a urine dipstick pad."""
    blood = kb.resolve("Lökosit")
    urine = kb.resolve("Lökosit (Strip)")
    assert blood.concept_key == "leukocytes"
    assert urine.concept_key == "urine_leukocytes"
    assert blood.concept_key != urine.concept_key


def test_turkish_names_resolve(kb):
    assert kb.resolve("Trombosit").concept_key == "platelets"
    assert kb.resolve("Serbest T4").concept_key == "free_t4"
    assert kb.resolve("Glikozile Hemoglobin (HbA1c)").concept_key == "hba1c"


def test_misspelling_resolves_by_fuzzy_match_and_is_flagged(kb):
    r = classify(kb, "Hemoglobinn", 13.1, "g/dL")
    assert r["concept_key"] == "hemoglobin"
    assert r["evidence"]["resolution"]["matched_via"] == "fuzzy"
    assert "name_inferred" in r["flags"]


def test_unknown_test_is_not_guessed(kb):
    r = classify(kb, "Zorblatt Factor", 5, "x")
    assert r["status"] == "Unknown"
    assert "needs_human_review" in r["flags"]
    assert r["reference_low"] is None


def test_short_names_are_not_fuzzy_matched(kb):
    """A two-letter unknown must not be silently matched to 'Ca' or 'Na'."""
    assert kb.resolve("Xq").concept_key is None


def test_ambiguous_alias_is_resolved_by_unit(kb):
    """PCT is both Plateletcrit and Procalcitonin; the unit separates them."""
    assert kb.resolve("PCT", "%").concept_key == "pct_plateletcrit"
    assert kb.resolve("PCT", "ng/mL").concept_key == "procalcitonin"


def test_ambiguous_alias_without_unit_is_left_unresolved(kb):
    r = kb.resolve("PCT", None)
    assert r.concept_key is None
    assert set(r.candidates) == {"pct_plateletcrit", "procalcitonin"}


# -- value parsing -------------------------------------------------------------

@pytest.mark.parametrize("raw,kind,number", [
    ("12.9", "numeric", 12.9),
    (267, "numeric", 267.0),
    ("1,020", "numeric", 1020.0),
    ("0,87", "numeric", 0.87),
    ("<0.01", "numeric", 0.01),
    ("Negatif", "qualitative", None),
    ("1+", "qualitative", None),
    ("", "unparseable", None),
    ("abc", "unparseable", None),
    (None, "unparseable", None),
])
def test_parse_value(raw, kind, number):
    p = parse_value(raw)
    assert p.kind == kind
    if number is not None:
        assert p.number == pytest.approx(number)


def test_censored_value_keeps_its_operator():
    p = parse_value("<0.01")
    assert p.operator == "<"


@pytest.mark.parametrize("text,low,high", [
    ("15-150", 15.0, 150.0),
    ("0.87-1.70", 0.87, 1.70),
    ("<5", None, 5.0),
    (">=90", 90.0, None),
    ("Negatif", None, None),
])
def test_parse_range_text(text, low, high):
    assert parse_range_text(text) == (low, high)


# -- the severity rule ---------------------------------------------------------

def test_deviation_index_is_zero_inside_the_interval():
    assert deviation_index(100.0, 70.0, 130.0) == (0.0, "Normal")


def test_deviation_index_is_normalised_by_interval_width():
    d, direction = deviation_index(160.0, 70.0, 130.0)
    assert direction == "High"
    assert d == pytest.approx(0.5)  # 30 over, on a 60-wide interval


def test_low_side_is_normalised_for_analytes_bounded_at_zero():
    """Ferritin 15-150: a plain width normalisation would never flag a low result."""
    d, direction = deviation_index(9.0, 15.0, 150.0)
    assert direction == "Low"
    assert d == pytest.approx(0.4)  # 6 below, normalised by min(135, 15)


@pytest.mark.parametrize("value,expected", [
    (13.0, "Normal"),
    (11.5, "Warning"),
    (6.5, "Critical"),   # below the 7.0 g/dL action limit
])
def test_hemoglobin_bands(kb, value, expected):
    assert status_of(kb, "Hemoglobin", value, "g/dL", sex="female") == expected


def test_panic_limit_overrides_a_narrow_deviation(kb):
    """Potassium 6.9 is only 1.1 interval widths out, but it is an action limit."""
    r = classify(kb, "Potassium", 6.9, "mmol/L")
    assert r["status"] == "Critical"
    assert r["evidence"]["rule"]["id"] == "panic_limit"
    assert "panic_limit_breached" in r["flags"]


def test_critical_band_override_prevents_over_escalation(kb):
    """Fasting glucose 130 is abnormal, not critical, despite a narrow interval."""
    r = classify(kb, "Glucose", 130, "mg/dL")
    assert r["status"] == "Warning"
    assert r["deviation_index"] > 1.0


def test_borderline_normal_is_flagged_but_stays_normal(kb):
    r = classify(kb, "Platelets", 155, "10^3/uL")
    assert r["status"] == "Normal"
    assert "borderline" in r["flags"]


# -- units ---------------------------------------------------------------------

def test_unit_conversion_changes_the_verdict(kb):
    """13 mmol/L of haemoglobin is 20.9 g/dL - critical, not normal."""
    r = classify(kb, "Hemoglobin", 13.0, "mmol/L", sex="female")
    assert r["canonical_value"] == pytest.approx(20.943)
    assert r["status"] == "Critical"
    assert r["unit_converted"] is True


def test_incompatible_unit_refuses_to_classify(kb):
    """Comparing mg/dL against g/dL silently would be a patient-safety bug."""
    r = classify(kb, "Hemoglobin", 13.0, "furlongs")
    assert r["status"] == "Unknown"
    assert "unit_mismatch" in r["flags"]
    assert "furlongs" in r["error"]


def test_missing_unit_assumes_the_canonical_unit(kb):
    r = classify(kb, "Hemoglobin", 13.0, None, sex="female")
    assert r["status"] == "Normal"


# -- reference range provenance ------------------------------------------------

def test_row_supplied_range_takes_precedence(kb):
    """The issuing laboratory's own interval outranks our knowledge base."""
    r = classify(kb, "Hemoglobin", 11.0, "g/dL", sex="female",
                 row_low=10.0, row_high=14.0)
    assert r["status"] == "Normal"
    assert r["evidence"]["reference_range"]["source"] == "row_reference_range"


def test_free_text_range_is_used_when_bounds_are_absent(kb):
    r = classify(kb, "Ferritin", 20, "ug/L", row_range_text="25-200")
    assert r["reference_low"] == 25.0
    assert r["status"] == "Warning"


def test_sex_specific_interval_is_selected(kb):
    """Hb 13.0 is normal for a woman and low for a man."""
    assert status_of(kb, "Hemoglobin", 13.0, "g/dL", sex="female") == "Normal"
    assert status_of(kb, "Hemoglobin", 13.0, "g/dL", sex="male") == "Warning"


# -- qualitative results -------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("Negatif", "Normal"),
    ("Eser", "Warning"),
    ("1+", "Warning"),
    ("3+", "Critical"),
])
def test_dipstick_grades(kb, value, expected):
    assert status_of(kb, "Protein (Strip)", value) == expected


def test_unrecognised_dipstick_grade_is_not_guessed(kb):
    r = classify(kb, "Nitrit (Strip)", "maybe")
    assert r["status"] == "Unknown"
    assert "needs_human_review" in r["flags"]


def test_qualitative_direction_is_positive_not_high(kb):
    r = classify(kb, "Nitrit (Strip)", "Pozitif")
    assert r["status"] == "Warning"
    assert r["direction"] == "Positive"


# -- evidence trail ------------------------------------------------------------

def test_every_result_carries_a_complete_audit_trail(kb):
    r = classify(kb, "Potasyum", 6.9, "mmol/L")
    ev = r["evidence"]
    assert {s["step"] for s in ev["steps"]} >= {
        "resolve", "parse_value", "harmonise_units", "select_reference_range", "apply_rules"}
    assert ev["reference_range"]["citation"]
    assert ev["rule"]["rationale"]
    assert ev["resolution"]["matched_via"] == "alias"


def test_care_pathway_is_specific_where_one_exists(kb):
    r = classify(kb, "Potassium", 6.9, "mmol/L")
    assert r["care_pathway"]["source"] == "by_concept.potassium.Critical:High"
    assert r["care_pathway"]["urgency"] == "emergency"


def test_care_pathway_falls_back_to_category(kb):
    r = classify(kb, "Platelets", 15, "10^3/uL")
    assert r["status"] == "Critical"
    assert r["care_pathway"]["source"].startswith("by_concept.platelets")
