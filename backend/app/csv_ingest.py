"""
CSV ingestion.

Real lab exports do not agree on column names. The Kaggle dataset this project
targets uses ``Test_Name / Result / Unit / Min_Reference / Max_Reference``;
another lab will send ``analyte, value, units, low, high``; a Turkish export
will send ``Tetkik, Sonuc, Birim``. Rather than hardcode one schema, the loader
maps columns by synonym and reports the mapping it chose, so a user can see how
their file was read instead of wondering why a column was ignored.
"""

from __future__ import annotations

import csv
import io
from typing import Any

# Column synonyms, in priority order. Matching is on the lowercased header with
# separators stripped, so "Test_Name", "test name" and "TESTNAME" all match.
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "test_name": ["testname", "test", "analyte", "parameter", "labtest", "name",
                  "examination", "tetkik", "testadi", "component"],
    "value": ["result", "value", "resultvalue", "measurement", "observedvalue",
              "sonuc", "deger", "obsvalue"],
    "unit": ["unit", "units", "uom", "birim", "resultunit"],
    "reference_range": ["referencerange", "refrange", "range", "normalrange",
                        "referans", "referansaralik", "referenceinterval"],
    "reference_low": ["minreference", "reflow", "referencelow", "low", "min",
                      "lowerlimit", "minvalue", "altsinir"],
    "reference_high": ["maxreference", "refhigh", "referencehigh", "high", "max",
                       "upperlimit", "maxvalue", "ustsinir"],
    "sex": ["sex", "gender", "cinsiyet", "patientsex"],
    "age_years": ["age", "ageyears", "yas", "patientage"],
    "collected_on": ["date", "collectedon", "collectiondate", "resultdate",
                     "tarih", "specimendate", "observationdate"],
}

MAX_ROWS = 300
MAX_BYTES = 2_000_000

_SEX_WORDS = {
    "m": "male", "male": "male", "erkek": "male", "e": "male",
    "f": "female", "female": "female", "kadin": "female", "k": "female",
    "w": "female", "woman": "female", "man": "male",
}


class CSVIngestError(ValueError):
    """Raised when a file cannot be read as lab results at all."""


def _key(header: str) -> str:
    return "".join(ch for ch in str(header).lower() if ch.isalnum())


def parse_csv(raw: bytes | str, filename: str = "upload.csv") -> dict[str, Any]:
    """Parse a CSV upload into lab inputs plus a report of how it was read.

    Returns ``{labs, patient, mapping, unmapped_columns, warnings, row_count}``.
    Rows that cannot be read are skipped with a warning rather than failing the
    whole upload - a single malformed row should not cost the user their file.
    """
    if isinstance(raw, bytes):
        if len(raw) > MAX_BYTES:
            raise CSVIngestError(
                f"File is {len(raw) // 1024} KB; the limit is {MAX_BYTES // 1024} KB.")
        text = _decode(raw)
    else:
        text = raw

    text = text.lstrip("﻿").strip()
    if not text:
        raise CSVIngestError("The file is empty.")

    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # a single-column file sniffs as an error; comma is a safe default

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise CSVIngestError("Could not read a header row from the file.")

    headers = [h for h in reader.fieldnames if h is not None]
    mapping = _map_columns(headers)

    if "test_name" not in mapping or "value" not in mapping:
        raise CSVIngestError(
            "Could not find a test-name column and a result column. "
            f"Columns found: {', '.join(headers) or '(none)'}. "
            "Expected something like 'Test_Name' and 'Result'."
        )

    warnings: list[str] = []
    labs: list[dict[str, Any]] = []
    sexes: set[str] = set()
    ages: set[float] = set()
    row_count = 0

    for lineno, row in enumerate(reader, start=2):
        row_count += 1
        if row_count > MAX_ROWS:
            warnings.append(f"Only the first {MAX_ROWS} rows were analysed.")
            break
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank line

        name = (row.get(mapping["test_name"]) or "").strip()
        value = (row.get(mapping["value"]) or "").strip()
        if not name:
            warnings.append(f"Row {lineno}: skipped, no test name.")
            continue
        if not value:
            warnings.append(f"Row {lineno}: '{name}' skipped, no result value.")
            continue

        labs.append({
            "test_name": name,
            "value": value,
            "unit": _cell(row, mapping, "unit"),
            "reference_range": _cell(row, mapping, "reference_range"),
            "reference_low": _number(_cell(row, mapping, "reference_low")),
            "reference_high": _number(_cell(row, mapping, "reference_high")),
            "collected_on": _cell(row, mapping, "collected_on"),
        })

        if s := _cell(row, mapping, "sex"):
            if resolved := _SEX_WORDS.get(s.strip().lower()):
                sexes.add(resolved)
        if a := _number(_cell(row, mapping, "age_years")):
            ages.add(a)

    if not labs:
        raise CSVIngestError(
            "No usable rows found. Every row was missing a test name or a result value.")

    patient: dict[str, Any] = {}
    if len(sexes) == 1:
        patient["sex"] = sexes.pop()
    elif len(sexes) > 1:
        warnings.append("The file contains more than one sex; sex-specific "
                        "reference intervals were not applied.")
    if len(ages) == 1:
        patient["age_years"] = ages.pop()

    unmapped = [h for h in headers if h not in mapping.values()]
    return {
        "labs": labs,
        "patient": patient or None,
        "mapping": {k: v for k, v in mapping.items()},
        "unmapped_columns": unmapped,
        "warnings": warnings,
        "row_count": len(labs),
        "filename": filename,
        "delimiter": getattr(dialect, "delimiter", ","),
    }


def _decode(raw: bytes) -> str:
    """Lab exports are frequently Windows-1254 or Latin-1, not UTF-8."""
    for encoding in ("utf-8-sig", "utf-8", "cp1254", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _map_columns(headers: list[str]) -> dict[str, str]:
    """Match each logical field to the best available header."""
    normalised = {_key(h): h for h in headers}
    mapping: dict[str, str] = {}
    for field, synonyms in COLUMN_SYNONYMS.items():
        for syn in synonyms:
            if syn in normalised and normalised[syn] not in mapping.values():
                mapping[field] = normalised[syn]
                break
        else:
            # Fall back to a header that contains a synonym, e.g. "Result_Value".
            for syn in synonyms:
                for norm, original in normalised.items():
                    if syn in norm and original not in mapping.values():
                        mapping[field] = original
                        break
                if field in mapping:
                    break
    return mapping


def _cell(row: dict, mapping: dict, field: str) -> str | None:
    col = mapping.get(field)
    if not col:
        return None
    v = row.get(col)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _number(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
