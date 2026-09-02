"""
Clinical Lab MCP server.

This is the *only* route by which the agent reaches clinical knowledge. The
FastAPI agent holds no reference ranges, no alias table and no care pathways of
its own - it opens a stdio MCP session and calls these tools. That boundary is
deliberate: it keeps the clinical knowledge in one auditable place, lets the
same knowledge be driven by any other MCP client (Claude Desktop, an IDE), and
makes every fact the agent uses traceable to a named tool call.

Run standalone with:   python -m mcp_server.server
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .engine import KnowledgeBase, classify, normalize_name, parse_range_text

mcp = FastMCP("clinical-lab-knowledge")

_KB: KnowledgeBase | None = None


def kb() -> KnowledgeBase:
    """Load the knowledge base once per process."""
    global _KB
    if _KB is None:
        _KB = KnowledgeBase()
    return _KB


@mcp.tool()
def list_catalog() -> str:
    """List every lab test this server can classify.

    Returns the canonical key, display name, category, canonical unit and LOINC
    code for each test. Use this to populate a picker or to check whether a test
    is supported before classifying it.
    """
    k = kb()
    return json.dumps({"knowledge_base_version": k.version, "tests": k.catalog()},
                      ensure_ascii=False)


@mcp.tool()
def resolve_test_name(test_name: str, unit: str | None = None) -> str:
    """Map a free-text lab test name onto a catalogue concept.

    Handles Turkish and English names, abbreviations, and misspellings. When a
    name is ambiguous (for example "PCT", which is both Plateletcrit and
    Procalcitonin) the reported `unit` is used to disambiguate; if it still
    cannot decide it returns matched_via="unresolved" with the candidates, so
    the caller can route the row to a human rather than guess.

    Args:
        test_name: The test name exactly as it appeared on the report.
        unit: The reported unit, if known. Used only to break ambiguity.
    """
    r = kb().resolve(test_name, unit)
    return json.dumps({
        "input": test_name,
        "normalized": normalize_name(test_name),
        "concept_key": r.concept_key,
        "display_name": r.display_name,
        "matched_via": r.matched_via,
        "confidence": r.confidence,
        "candidates": r.candidates,
        "note": r.note,
    }, ensure_ascii=False)


@mcp.tool()
def reference_range_lookup(test_name: str, sex: str | None = None,
                           age_years: float | None = None,
                           unit: str | None = None) -> str:
    """Look up the reference interval for a test.

    This is the fallback the agent calls when a test is not matched directly.
    It resolves the name first (including fuzzy matching), then returns the
    sex- and age-appropriate interval together with its provenance, so the
    caller can cite where the interval came from.

    Args:
        test_name: Test name as reported.
        sex: "male", "female", or omit for the sex-neutral interval.
        age_years: Patient age, used where age-banded intervals exist.
        unit: Reported unit, used to disambiguate same-named tests.
    """
    k = kb()
    r = k.resolve(test_name, unit)
    if not r.concept_key:
        return json.dumps({
            "found": False, "reason": r.note, "candidates": r.candidates,
            "guidance": ("No interval available. Do not invent one - route this "
                         "result to a human reviewer."),
        }, ensure_ascii=False)

    concept = k.concepts[r.concept_key]
    interval = k.select_range(r.concept_key, sex, age_years)
    return json.dumps({
        "found": True,
        "concept_key": r.concept_key,
        "display_name": concept["display_name"],
        "category": concept["category"],
        "loinc": concept.get("loinc"),
        "value_type": concept["value_type"],
        "canonical_unit": concept["canonical_unit"],
        "accepted_units": list(concept.get("units", {}).keys()),
        "reference_low": (interval or {}).get("low"),
        "reference_high": (interval or {}).get("high"),
        "matched_on_sex": (interval or {}).get("sex"),
        "panic_limits": concept.get("panic"),
        "qualitative_scale": concept.get("qualitative_map"),
        "measures": concept.get("measures"),
        "matched_via": r.matched_via,
        "confidence": r.confidence,
        "citation": f"Knowledge base v{k.version}, concept '{r.concept_key}'",
    }, ensure_ascii=False)


@mcp.tool()
def classify_lab_result(test_name: str, value: str | float | int, unit: str | None = None,
                        sex: str | None = None, age_years: float | None = None,
                        reference_low: float | None = None,
                        reference_high: float | None = None,
                        reference_range_text: str | None = None) -> str:
    """Classify one lab result as Normal, Warning, Critical or Unknown.

    Fully deterministic - no language model is involved. Returns the status plus
    a complete evidence trail: how the name resolved, how the value parsed, any
    unit conversion applied, which reference interval was chosen and why, the
    deviation index, and the exact rule that produced the status.

    A reference interval supplied by the caller (reference_low/reference_high,
    or free text like "15-150") takes precedence over the knowledge base,
    because the issuing laboratory's own interval is the authoritative one.

    Args:
        test_name: Test name as reported.
        value: The result. May be numeric (12.9 or "12.9"), censored ("<0.01"),
            or qualitative ("Negatif", "1+", "Normal"). Numeric strings are
            accepted as numbers because MCP clients may coerce them in transit.
        unit: Reported unit.
        sex: "male" or "female", for sex-specific intervals.
        age_years: Patient age in years.
        reference_low: Lower bound supplied with the result, if any.
        reference_high: Upper bound supplied with the result, if any.
        reference_range_text: Free-text interval such as "15-150" or "<5".
    """
    result = classify(
        kb(), test_name=test_name, value=value, unit=unit, sex=sex,
        age_years=age_years, row_low=reference_low, row_high=reference_high,
        row_range_text=reference_range_text,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def care_pathway_lookup(status: str, test_name: str | None = None,
                        direction: str | None = None) -> str:
    """Retrieve the protocol follow-up actions for a classified result.

    These come from a curated table, not from a model, so the UI can present
    them as protocol rather than as AI suggestions. Resolution falls back from
    the specific test, to its category, to a generic default.

    Args:
        status: "Critical", "Warning", "Normal" or "Unknown".
        test_name: Test name, for test-specific pathways (e.g. hyperkalaemia).
        direction: "High", "Low" or "Positive", where the pathway differs by
            which side of the interval the result fell.
    """
    k = kb()
    concept_key = k.resolve(test_name).concept_key if test_name else None
    return json.dumps(k.care_pathway(concept_key, status, direction),
                      ensure_ascii=False)


@mcp.tool()
def parse_reference_range(text: str) -> str:
    """Parse a free-text reference interval into numeric bounds.

    Understands "15-150", "0.4 - 4.0", "<5", ">=90". Returns nulls for text it
    cannot parse rather than guessing.

    Args:
        text: The reference range string from the lab report.
    """
    low, high = parse_range_text(text)
    return json.dumps({"input": text, "low": low, "high": high,
                       "parsed": low is not None or high is not None},
                      ensure_ascii=False)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
