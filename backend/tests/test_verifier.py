"""Tests for the grounding checks applied to model-authored explanations."""

from __future__ import annotations

import pytest

from app.verifier import rule_based_explanation, verify_explanation

FACT = {
    "id": 0,
    "display_name": "Potassium",
    "value": 6.9,
    "canonical_value": 6.9,
    "canonical_unit": "mmol/L",
    "unit": "mmol/L",
    "reference_low": 3.5,
    "reference_high": 5.1,
    "reference_text": "3.5-5.1 mmol/L",
    "value_display": "6.9",
    "result_text": "6.9 mmol/L",
    "converted_text": "",
    "status": "Critical",
    "direction": "High",
    "deviation_index": 1.125,
    "measures": "The main electrolyte inside cells.",
    "rationale": "6.9 mmol/L breaches the absolute action limit of 6.5 mmol/L.",
}


def explanation(**overrides) -> dict:
    base = {
        "what_it_measures": "Potassium is the main electrolyte inside cells.",
        "why_flagged": "At 6.9 mmol/L this is above the reference interval of 3.5-5.1 mmol/L.",
        "clinical_significance": "A raised potassium can affect cardiac conduction.",
        "patient_friendly": "Your potassium is higher than the usual range.",
        "suggested_next_steps": ["Repeat the sample"],
    }
    return {**base, **overrides}


def test_a_well_grounded_explanation_passes():
    report = verify_explanation(explanation(), FACT)
    assert report["passed"] is True
    assert report["action"] == "accepted"
    assert all(c["passed"] for c in report["checks"])


def test_invented_threshold_is_caught():
    """The classic hallucination: a number the model was never given."""
    report = verify_explanation(
        explanation(clinical_significance="Levels above 7.8 mmol/L risk cardiac arrest."),
        FACT)
    assert report["passed"] is False
    failed = [c for c in report["checks"] if not c["passed"]]
    assert failed[0]["id"] == "numeric_grounding"
    assert "7.8" in failed[0]["detail"]


def test_the_engines_own_numbers_are_accepted():
    """6.5 appears in the rationale, so citing it is grounded."""
    report = verify_explanation(
        explanation(why_flagged="6.9 mmol/L is past the 6.5 mmol/L action limit."), FACT)
    assert report["passed"] is True


def test_digits_inside_a_test_name_are_not_ungrounded():
    fact = {**FACT, "display_name": "Vitamin B12", "measures": "Vitamin B12 supports nerves.",
            "status": "Normal", "direction": "Normal"}
    report = verify_explanation(
        explanation(what_it_measures="Vitamin B12 supports nerve function.",
                    why_flagged="This B12 result sits inside its interval.",
                    clinical_significance="No follow-up follows from this result.",
                    patient_friendly="This one is in the usual range."),
        fact)
    assert report["passed"] is True


def test_reassuring_language_on_a_critical_result_is_rejected():
    report = verify_explanation(
        explanation(patient_friendly="This is within normal limits, nothing to worry about."),
        FACT)
    assert report["passed"] is False
    assert any(c["id"] == "status_consistency" and not c["passed"] for c in report["checks"])


def test_alarming_language_on_a_normal_result_is_rejected():
    fact = {**FACT, "status": "Normal", "direction": "Normal"}
    report = verify_explanation(
        explanation(why_flagged="This is a critical finding requiring urgent review."), fact)
    assert any(c["id"] == "status_consistency" and not c["passed"] for c in report["checks"])


def test_wrong_direction_is_rejected():
    report = verify_explanation(
        explanation(why_flagged="This value is below the reference interval."), FACT)
    assert any(c["id"] == "direction_consistency" and not c["passed"] for c in report["checks"])


@pytest.mark.parametrize("text", [
    "You have hyperkalaemia and need treatment.",
    "The patient has kidney failure.",
    "Start 40 mg of the usual agent.",
    "There is no need to see a doctor about this.",
])
def test_unsafe_content_is_rejected(text):
    report = verify_explanation(explanation(clinical_significance=text), FACT)
    assert any(c["id"] == "safety_rails" and not c["passed"] for c in report["checks"])
    assert report["action"] == "replaced_with_rule_based"


def test_empty_fields_are_rejected():
    report = verify_explanation(explanation(why_flagged="   "), FACT)
    assert any(c["id"] == "completeness" and not c["passed"] for c in report["checks"])


def test_every_check_is_reported_even_when_it_passes():
    """The UI shows what was checked, not just the verdict."""
    report = verify_explanation(explanation(), FACT)
    assert {c["id"] for c in report["checks"]} == {
        "numeric_grounding", "status_consistency", "direction_consistency",
        "safety_rails", "completeness"}


# -- the deterministic fallback ------------------------------------------------

def test_rule_based_explanation_is_available_for_every_status():
    for status in ("Normal", "Warning", "Critical", "Unknown"):
        result = {
            "status": status, "display_name": "Potassium", "measures": "An electrolyte.",
            "direction": "High", "direction_meaning": "hyperkalaemia",
            "value_display": "6.9", "canonical_unit": "mmol/L",
            "reference_low": 3.5, "reference_high": 5.1, "error": "unreadable",
        }
        exp = rule_based_explanation(result)
        assert exp["what_it_measures"] and exp["why_flagged"]
        assert exp["clinical_significance"] and exp["patient_friendly"]


def test_rule_based_explanation_passes_its_own_checks():
    """The fallback must never trip the checker it is the fallback for."""
    result = {
        "status": "Critical", "display_name": "Potassium",
        "measures": "The main electrolyte inside cells.", "direction": "High",
        "direction_meaning": "hyperkalaemia - risk of arrhythmia",
        "value_display": "6.9", "canonical_unit": "mmol/L",
        "reference_low": 3.5, "reference_high": 5.1, "error": None,
    }
    report = verify_explanation(rule_based_explanation(result), FACT)
    assert report["passed"] is True
