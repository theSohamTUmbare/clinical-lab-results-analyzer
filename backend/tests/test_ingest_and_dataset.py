"""CSV ingestion tests, plus a regression test against the labelled Kaggle data."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from app.csv_ingest import CSVIngestError, parse_csv
from mcp_server.engine import KnowledgeBase, classify

TEST_DATA = Path(__file__).resolve().parent.parent.parent / "test_data"
KAGGLE = TEST_DATA / "kaggle_lab_test_results_public.csv"


# -- CSV ingestion -------------------------------------------------------------

def test_kaggle_schema_is_mapped():
    parsed = parse_csv(KAGGLE.read_bytes(), KAGGLE.name)
    assert parsed["mapping"]["test_name"] == "Test_Name"
    assert parsed["mapping"]["value"] == "Result"
    assert parsed["mapping"]["reference_low"] == "Min_Reference"
    assert parsed["row_count"] == 27


def test_a_different_lab_schema_is_mapped_by_synonym():
    csv_text = ("analyte,value,units,low,high\n"
                "Haemoglobin,9.1,g/dL,12,15.5\n")
    parsed = parse_csv(csv_text)
    assert parsed["mapping"]["test_name"] == "analyte"
    assert parsed["mapping"]["unit"] == "units"
    assert parsed["labs"][0]["reference_low"] == 12.0


def test_semicolon_delimiter_is_detected():
    parsed = parse_csv("Test;Result;Unit\nFerritin;28.9;ug/L\nTSH;2.1;mIU/L\n")
    assert parsed["delimiter"] == ";"
    assert parsed["labs"][0]["test_name"] == "Ferritin"


def test_turkish_headers_are_mapped():
    parsed = parse_csv("Tetkik,Sonuc,Birim\nHemoglobin,12.9,g/dL\n")
    assert parsed["labs"][0]["test_name"] == "Hemoglobin"
    assert parsed["labs"][0]["unit"] == "g/dL"


def test_unmapped_columns_are_reported_not_silently_dropped():
    parsed = parse_csv("Test_Name,Result,Ordering_Physician\nTSH,2.1,Dr Who\n")
    assert "Ordering_Physician" in parsed["unmapped_columns"]


def test_bad_rows_are_skipped_with_a_warning_not_a_failure():
    parsed = parse_csv("Test_Name,Result\nTSH,2.1\n,5.0\nFerritin,\nHemoglobin,13\n")
    assert parsed["row_count"] == 2
    assert len(parsed["warnings"]) == 2


def test_patient_sex_is_picked_up_when_consistent():
    parsed = parse_csv("Test_Name,Result,Sex\nHemoglobin,13,F\nFerritin,20,F\n")
    assert parsed["patient"]["sex"] == "female"


def test_mixed_sexes_are_refused_rather_than_guessed():
    parsed = parse_csv("Test_Name,Result,Sex\nHemoglobin,13,F\nFerritin,20,M\n")
    assert parsed["patient"] is None
    assert any("more than one sex" in w for w in parsed["warnings"])


def test_latin1_encoded_file_is_decoded():
    raw = "Test_Name,Result\nİnsülin,9.42\n".encode("cp1254")
    parsed = parse_csv(raw)
    assert parsed["labs"][0]["test_name"] == "İnsülin"


@pytest.mark.parametrize("text,message", [
    ("", "empty"),
    ("a,b,c\n1,2,3\n", "test-name column"),
    ("Test_Name,Result\n,\n", "No usable rows"),
])
def test_unusable_files_fail_with_a_useful_message(text, message):
    with pytest.raises(CSVIngestError) as exc:
        parse_csv(text)
    assert message.lower() in str(exc.value).lower()


# -- regression against the labelled dataset -----------------------------------

TURKISH_LABELS = {"Normal": "Normal", "Yüksek": "High", "Düşük": "Low"}


def test_classifier_agrees_with_the_kaggle_status_labels():
    """The dataset ships its own status per row, so it is a real held-out check.

    Scored on abnormality and direction: the engine additionally separates
    Warning from Critical, a distinction the dataset does not make.
    """
    kb = KnowledgeBase()
    rows = list(csv.DictReader(io.StringIO(KAGGLE.read_text("utf-8-sig"))))
    assert rows, "dataset is empty"

    disagreements = []
    for row in rows:
        res = classify(
            kb, row["Test_Name"], row["Result"], row["Unit"],
            row_low=row["Min_Reference"] or None, row_high=row["Max_Reference"] or None,
            row_range_text=row["Reference_Range"],
        )
        expected = TURKISH_LABELS.get(row["Status"].strip(), row["Status"])
        predicted_abnormal = res["status"] in ("Warning", "Critical")
        if (predicted_abnormal != (expected != "Normal")
                or (predicted_abnormal and res["direction"] not in (expected, "Positive"))):
            disagreements.append((row["Test_Name"], row["Result"], expected, res["status"]))

    assert not disagreements, f"disagreed on {len(disagreements)} rows: {disagreements}"


def test_every_dataset_test_name_resolves():
    """No row of the target dataset should fall back to 'Unknown test'.

    The unit is supplied, as the classifier supplies it: "PCT" alone is
    genuinely ambiguous between Plateletcrit and Procalcitonin, and resolving it
    without the unit would be a guess rather than a match.
    """
    kb = KnowledgeBase()
    rows = list(csv.DictReader(io.StringIO(KAGGLE.read_text("utf-8-sig"))))
    unresolved = [r["Test_Name"] for r in rows
                  if kb.resolve(r["Test_Name"], r["Unit"]).concept_key is None]
    assert not unresolved, f"unresolved names: {unresolved}"


def test_ambiguous_name_without_its_unit_stays_unresolved():
    """The counterpart: without the unit, 'PCT' must not be guessed at."""
    assert KnowledgeBase().resolve("PCT").concept_key is None


@pytest.mark.parametrize("name", ["panel_critical.csv", "panel_mixed.csv", "panel_edge_cases.csv"])
def test_bundled_panels_are_ingestible(name):
    path = TEST_DATA / name
    assert path.exists(), f"{name} is missing from test_data/"
    parsed = parse_csv(path.read_bytes(), name)
    assert parsed["row_count"] > 0
