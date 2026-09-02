"""
Deterministic lab-result classification engine.

Design rule for the whole project: **no language model participates in
classification.** Everything in this module is pure, deterministic and unit
tested, so a given input always yields the same severity and the same audit
trail. The LLM is used later, and only to put grounded facts into words.

Pipeline implemented here:

    resolve name -> parse value -> harmonise units -> select reference range
                 -> score deviation -> apply rules -> emit evidence

Every stage records what it did in an ``evidence`` structure, which is what the
frontend renders as the "why" behind a flag.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# Severity bands applied to the deviation index (see `deviation_index`).
WARNING_BAND = 1.0          # 0 < d <= 1.0  -> Warning
BORDERLINE_FRACTION = 0.05  # inside the range but within 5% of a bound
FUZZY_THRESHOLD = 0.86      # difflib ratio required to accept a fuzzy name match

STATUS_ORDER = {"Critical": 0, "Warning": 1, "Unknown": 2, "Normal": 3}

# Turkish (and general accented) characters -> ASCII. `unicodedata` alone gets
# most of these, but dotless-i and the Turkish soft-g need explicit handling.
_TRANSLIT = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    "µ": "u",
})


def normalize_name(raw: str) -> str:
    """Fold a test name to a comparable key.

    Deliberately preserves the ``(Strip)`` / ``(Dipstick)`` qualifier as a
    token: "Lokosit" is a blood white-cell count while "Lokosit (Strip)" is a
    urine dipstick pad. Merging them would be a clinically dangerous collapse.
    """
    if not raw:
        return ""
    s = str(raw).translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    # '%' and the word 'percent' both mean the same thing and must fold to one
    # token, otherwise 'Notrofil%' and 'Neutrophil percent' index differently.
    s = s.replace("%", " percent ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\bpercentage\b", "percent", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_unit(raw: str | None) -> str:
    """Fold unit spellings that mean the same thing."""
    if raw is None:
        return ""
    u = str(raw).strip().translate(_TRANSLIT)
    u = u.replace("μ", "u").replace("µ", "u")
    u = re.sub(r"\s+", "", u)
    lookup = {
        "": "", "-": "-", "10e3/ul": "10^3/uL", "10*3/ul": "10^3/uL",
        "10^3/ul": "10^3/uL", "k/ul": "K/uL", "10^9/l": "10^9/L",
        "10e6/ul": "10^6/uL", "10*6/ul": "10^6/uL", "10^6/ul": "10^6/uL",
        "m/ul": "M/uL", "10^12/l": "10^12/L", "g/dl": "g/dL", "g/l": "g/L",
        "mg/dl": "mg/dL", "mmol/l": "mmol/L", "meq/l": "mEq/L",
        "umol/l": "umol/L", "ug/l": "ug/L", "mcg/l": "ug/L", "ng/ml": "ng/mL",
        "ug/dl": "ug/dL", "ng/dl": "ng/dL", "pg/ml": "pg/mL", "ng/l": "ng/L",
        "pmol/l": "pmol/L", "nmol/l": "nmol/L", "mu/l": "mU/L",
        "uiu/ml": "uIU/mL", "miu/l": "mIU/L", "iu/ml": "IU/mL", "ku/l": "KU/L",
        "u/l": "U/L", "iu/l": "IU/L", "mg/l": "mg/L", "fl": "fL",
        "leu/ul": "-", "ml/min/1.73m2": "mL/min/1.73m2", "ml/min": "mL/min",
        "%": "%", "l/l": "L/L", "ph": "pH", "ratio": "ratio", "/ul": "/uL",
    }
    return lookup.get(u.lower(), str(raw).strip())


# Qualitative result vocabulary, normalised to a canonical grade.
_QUALITATIVE = {
    "negatif": "negative", "negative": "negative", "neg": "negative",
    "yok": "negative", "none": "negative", "not detected": "negative",
    "pozitif": "positive", "positive": "positive", "pos": "positive",
    "var": "positive", "detected": "positive", "reaktif": "positive",
    "eser": "trace", "trace": "trace", "iz": "trace",
    "normal": "normal", "normaldir": "normal",
    "1+": "1+", "2+": "2+", "3+": "3+", "4+": "4+",
    "+": "1+", "++": "2+", "+++": "3+", "++++": "4+",
}

_GRADE_RANK = {"negative": 0, "normal": 0, "trace": 1, "positive": 2,
               "1+": 2, "2+": 3, "3+": 4, "4+": 5}


class KnowledgeError(RuntimeError):
    """Raised when the bundled knowledge files cannot be loaded."""


@dataclass
class ParsedValue:
    """Outcome of reading whatever the lab put in the result field."""
    kind: str                       # "numeric" | "qualitative" | "unparseable"
    number: float | None = None
    grade: str | None = None        # canonical qualitative grade
    operator: str | None = None     # "<" or ">" for censored results
    raw: str = ""
    note: str | None = None


def parse_value(raw: Any) -> ParsedValue:
    """Read a result cell that may be a number, a censored number or a word."""
    if raw is None:
        return ParsedValue(kind="unparseable", raw="", note="No value supplied")

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if math.isnan(raw) or math.isinf(raw):
            return ParsedValue(kind="unparseable", raw=str(raw),
                               note="Value is not a finite number")
        return ParsedValue(kind="numeric", number=float(raw), raw=str(raw))

    s = str(raw).strip()
    if not s:
        return ParsedValue(kind="unparseable", raw="", note="Empty value")

    key = s.lower().translate(_TRANSLIT)
    key = re.sub(r"\s+", " ", key).strip()
    if key in _QUALITATIVE:
        return ParsedValue(kind="qualitative", grade=_QUALITATIVE[key], raw=s)

    # Censored results such as "<0.01" or ">1000" - keep the operator, because
    # "<0.01" is not the same claim as "0.01".
    m = re.match(r"^([<>]=?)\s*(-?[\d.,]+)$", s)
    operator = None
    numeric_part = s
    if m:
        operator, numeric_part = m.group(1), m.group(2)

    cleaned = numeric_part.replace(" ", "")
    # A comma is a decimal separator in most of the world and a thousands
    # separator in some of it. "0,87" is unambiguous; "1,020" is not. The usual
    # heuristic applies: comma groups of exactly three digits are thousands,
    # anything else is a decimal comma.
    ambiguous_note = None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")            # 1,020.5 -> 1020.5
    elif re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", cleaned):
        ambiguous_note = (f"'{s}' was read as a thousands separator; if the comma was "
                          "a decimal separator the value would differ by a factor of 1000")
        cleaned = cleaned.replace(",", "")            # 1,020 -> 1020
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")           # 0,87 -> 0.87

    try:
        number = float(cleaned)
    except ValueError:
        return ParsedValue(kind="unparseable", raw=s,
                           note=f"Could not read '{s}' as a number or a known qualitative result")

    if math.isnan(number) or math.isinf(number):
        return ParsedValue(kind="unparseable", raw=s, note="Value is not finite")

    return ParsedValue(
        kind="numeric", number=number, operator=operator, raw=s,
        note=("Censored result - the true value lies beyond the reporting limit"
              if operator else ambiguous_note),
    )


def parse_range_text(text: Any) -> tuple[float | None, float | None]:
    """Read a free-text reference range such as '15-150' or '<5' or '0.4 - 4.0'."""
    if text is None:
        return None, None
    s = str(text).strip()
    if not s:
        return None, None

    m = re.match(r"^(-?[\d.,]+)\s*[-–—]\s*(-?[\d.,]+)$", s)
    if m:
        try:
            return (float(m.group(1).replace(",", ".")),
                    float(m.group(2).replace(",", ".")))
        except ValueError:
            return None, None

    m = re.match(r"^[<≤]=?\s*(-?[\d.,]+)$", s)
    if m:
        try:
            return None, float(m.group(1).replace(",", "."))
        except ValueError:
            return None, None

    m = re.match(r"^[>≥]=?\s*(-?[\d.,]+)$", s)
    if m:
        try:
            return float(m.group(1).replace(",", ".")), None
        except ValueError:
            return None, None

    return None, None


@dataclass
class Resolution:
    """How a raw test name was mapped onto a catalogue concept."""
    concept_key: str | None
    display_name: str | None
    matched_via: str            # exact | alias | unit_disambiguation | fuzzy | unresolved
    confidence: float
    candidates: list[str] = field(default_factory=list)
    note: str | None = None


class KnowledgeBase:
    """Loads and indexes the bundled clinical knowledge files."""

    def __init__(self, knowledge_dir: Path | None = None) -> None:
        d = Path(knowledge_dir or KNOWLEDGE_DIR)
        try:
            ranges = json.loads((d / "reference_ranges.json").read_text("utf-8"))
            aliases = json.loads((d / "aliases.json").read_text("utf-8"))
            pathways = json.loads((d / "care_pathways.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeError(f"Could not load knowledge base from {d}: {exc}") from exc

        self.version: str = ranges.get("_meta", {}).get("version", "unknown")
        self.concepts: dict[str, dict] = ranges["concepts"]
        self.pathways: dict[str, Any] = pathways

        # alias index: normalised alias -> list of concept keys
        self.alias_index: dict[str, list[str]] = {}
        for alias, targets in aliases["aliases"].items():
            self.alias_index.setdefault(normalize_name(alias), []).extend(targets)
        # every concept is an alias of itself, by key and by display name
        for key, c in self.concepts.items():
            for form in (key.replace("_", " "), c["display_name"]):
                self.alias_index.setdefault(normalize_name(form), []).append(key)
        for k, v in self.alias_index.items():
            self.alias_index[k] = sorted(dict.fromkeys(v))

        self._fuzzy_keys = list(self.alias_index.keys())

    # -- name resolution ---------------------------------------------------

    def resolve(self, raw_name: str, unit: str | None = None) -> Resolution:
        """Map a raw test name onto a catalogue concept, or report why not."""
        norm = normalize_name(raw_name)
        if not norm:
            return Resolution(None, None, "unresolved", 0.0,
                              note="Test name was empty")

        if norm in self.concepts:
            return Resolution(norm, self.concepts[norm]["display_name"], "exact", 1.0)

        targets = self.alias_index.get(norm)
        if targets:
            if len(targets) == 1:
                return Resolution(targets[0], self.concepts[targets[0]]["display_name"],
                                  "alias", 1.0)
            picked = self._disambiguate_by_unit(targets, unit)
            if picked:
                return Resolution(
                    picked, self.concepts[picked]["display_name"],
                    "unit_disambiguation", 0.9, candidates=targets,
                    note=(f"'{raw_name}' is ambiguous; resolved to "
                          f"{self.concepts[picked]['display_name']} because the reported "
                          f"unit '{unit}' is only valid for that test"),
                )
            return Resolution(
                None, None, "unresolved", 0.0, candidates=targets,
                note=(f"'{raw_name}' matches more than one test "
                      f"({', '.join(self.concepts[t]['display_name'] for t in targets)}) "
                      "and the reported unit does not separate them"),
            )

        # Fuzzy matching on very short strings is noise ("Ka" -> "Ca"), and a
        # wrong test identity is worse than an honest Unknown.
        close = (difflib.get_close_matches(norm, self._fuzzy_keys, n=3, cutoff=FUZZY_THRESHOLD)
                 if len(norm) >= 4 else [])
        if close:
            best = close[0]
            score = difflib.SequenceMatcher(None, norm, best).ratio()
            cands = self.alias_index[best]
            if len(cands) == 1:
                return Resolution(
                    cands[0], self.concepts[cands[0]]["display_name"], "fuzzy",
                    round(score, 3),
                    note=(f"'{raw_name}' was not an exact match; interpreted as "
                          f"{self.concepts[cands[0]]['display_name']} "
                          f"(similarity {score:.0%})"),
                )

        suggestions = difflib.get_close_matches(norm, self._fuzzy_keys, n=5, cutoff=0.5)
        return Resolution(
            None, None, "unresolved", 0.0,
            candidates=sorted({c for s in suggestions for c in self.alias_index[s]})[:5],
            note=f"'{raw_name}' is not in the reference catalogue",
        )

    def _disambiguate_by_unit(self, targets: list[str], unit: str | None) -> str | None:
        """Pick between same-name tests using the reported unit (e.g. PCT)."""
        u = normalize_unit(unit)
        if not u:
            return None
        viable = [t for t in targets if u in self.concepts[t].get("units", {})]
        return viable[0] if len(viable) == 1 else None

    # -- reference ranges --------------------------------------------------

    def select_range(self, concept_key: str, sex: str | None = None,
                     age_years: float | None = None) -> dict | None:
        """Pick the sex/age-appropriate interval, falling back to the generic one."""
        concept = self.concepts.get(concept_key)
        if not concept:
            return None
        ranges = concept.get("ranges") or []
        want = (sex or "any").strip().lower()
        want = {"f": "female", "m": "male", "w": "female"}.get(want, want)

        def age_ok(r: dict) -> bool:
            if age_years is None:
                return True
            lo, hi = r.get("age_min"), r.get("age_max")
            return (lo is None or age_years >= lo) and (hi is None or age_years <= hi)

        for r in ranges:
            if r.get("sex") == want and age_ok(r):
                return dict(r)
        for r in ranges:
            if r.get("sex") == "any" and age_ok(r):
                return dict(r)
        return dict(ranges[0]) if ranges else None

    def care_pathway(self, concept_key: str | None, status: str,
                     direction: str | None = None) -> dict:
        """Look up the protocol follow-up for a (concept, status, direction)."""
        keys = [f"{status}:{direction}", status] if direction else [status]
        concept = self.concepts.get(concept_key or "", {})

        by_concept = self.pathways.get("by_concept", {}).get(concept_key or "", {})
        for k in keys:
            if k in by_concept:
                return {**by_concept[k], "source": f"by_concept.{concept_key}.{k}"}

        by_cat = self.pathways.get("by_category", {}).get(concept.get("category", ""), {})
        for k in keys:
            if k in by_cat:
                return {**by_cat[k], "source": f"by_category.{concept.get('category')}.{k}"}

        default = self.pathways.get("default", {})
        for k in keys + ["Normal"]:
            if k in default:
                return {**default[k], "source": f"default.{k}"}
        return {"urgency": "review", "sla": "Manual review", "specialty": "Laboratory",
                "actions": ["Route to a human reviewer"], "source": "fallback"}

    def catalog(self) -> list[dict]:
        """Public catalogue used by the frontend's autocomplete."""
        return sorted(
            (
                {
                    "key": k,
                    "display_name": c["display_name"],
                    "category": c["category"],
                    "unit": c["canonical_unit"],
                    "value_type": c["value_type"],
                    "loinc": c.get("loinc"),
                }
                for k, c in self.concepts.items()
            ),
            key=lambda x: (x["category"], x["display_name"]),
        )


def deviation_index(value: float, low: float | None, high: float | None) -> tuple[float, str]:
    """How far outside the interval a value sits, in units of interval width.

    Returning a *normalised* distance rather than a raw difference is what lets
    one severity rule work across tests whose scales differ by orders of
    magnitude (troponin in ng/L, sodium in mmol/L, platelets in thousands).

        d = 0        -> inside the interval
        0 < d <= 1   -> outside by up to one interval width  (Warning)
        d > 1        -> outside by more than one width       (Critical)
    """
    if low is not None and high is not None and high > low:
        width = high - low
    else:
        anchor = high if high is not None else low
        width = abs(anchor) if anchor else 1.0
        width = width or 1.0

    if high is not None and value > high:
        return (value - high) / width, "High"
    if low is not None and value < low:
        # Analytes bounded at zero (ferritin 15-150, B12 200-900) have an
        # interval far wider than the gap between the lower bound and zero, so
        # plain width normalisation is nearly blind to severely low results.
        # Normalising the low side by min(width, low) restores the sensitivity
        # where it actually matters clinically.
        low_width = min(width, low) if low > 0 else width
        return (low - value) / low_width, "Low"
    return 0.0, "Normal"


def classify(
    kb: KnowledgeBase,
    test_name: str,
    value: Any,
    unit: str | None = None,
    sex: str | None = None,
    age_years: float | None = None,
    row_low: Any = None,
    row_high: Any = None,
    row_range_text: Any = None,
) -> dict:
    """Classify one lab result and return the result plus its full audit trail."""
    evidence: dict[str, Any] = {"steps": [], "kb_version": kb.version}
    flags: list[str] = []

    def step(name: str, outcome: str, **extra: Any) -> None:
        evidence["steps"].append({"step": name, "outcome": outcome, **extra})

    # --- 1. resolve the test name ---------------------------------------
    res = kb.resolve(test_name, unit)
    evidence["resolution"] = {
        "input": test_name, "concept_key": res.concept_key,
        "matched_via": res.matched_via, "confidence": res.confidence,
        "candidates": res.candidates, "note": res.note,
    }
    step("resolve", res.matched_via, concept=res.concept_key, confidence=res.confidence)
    if res.matched_via in ("fuzzy", "unit_disambiguation"):
        flags.append("name_inferred")

    if not res.concept_key:
        return _unknown(kb, test_name, value, unit, evidence, flags,
                        res.note or "Test not recognised", res.candidates)

    concept = kb.concepts[res.concept_key]

    # --- 2. parse the value ---------------------------------------------
    parsed = parse_value(value)
    evidence["parsed_value"] = {
        "kind": parsed.kind, "number": parsed.number, "grade": parsed.grade,
        "operator": parsed.operator, "note": parsed.note,
    }
    step("parse_value", parsed.kind, note=parsed.note)
    if parsed.kind == "unparseable":
        return _unknown(kb, test_name, value, unit, evidence, flags,
                        parsed.note or "Value could not be interpreted", [],
                        concept_key=res.concept_key, display_name=concept["display_name"])
    if parsed.operator:
        flags.append("censored_value")

    # --- 3a. qualitative branch -----------------------------------------
    if parsed.kind == "qualitative" or concept["value_type"] == "qualitative":
        return _classify_qualitative(kb, res, concept, parsed, unit, evidence, flags,
                                     raw_name=test_name)

    # --- 3b. numeric branch: harmonise units ----------------------------
    reported_unit = normalize_unit(unit)
    canonical_unit = concept["canonical_unit"]
    units_map = concept.get("units", {})
    number = parsed.number
    assert number is not None

    row_lo, row_hi = _coerce(row_low), _coerce(row_high)
    if row_lo is None and row_hi is None:
        row_lo, row_hi = parse_range_text(row_range_text)
    has_row_range = row_lo is not None or row_hi is not None

    conversion = None
    if has_row_range:
        # The lab supplied the interval alongside the result, so both are in the
        # same unit by construction. Comparing them as reported avoids inventing
        # a conversion we do not need.
        step("harmonise_units", "skipped",
             note="Row supplied its own reference range; compared in the reported unit")
        working_unit = reported_unit or canonical_unit
    elif not reported_unit or reported_unit == canonical_unit:
        working_unit = canonical_unit
        step("harmonise_units", "already_canonical", unit=canonical_unit)
    elif reported_unit in units_map:
        factor = units_map[reported_unit]
        number = number * factor
        conversion = {"from": reported_unit, "to": canonical_unit, "factor": factor,
                      "original": parsed.number, "converted": round(number, 6)}
        working_unit = canonical_unit
        step("harmonise_units", "converted", **conversion)
    else:
        # Refusing to classify beats silently comparing mg/dL against mmol/L.
        flags.append("unit_mismatch")
        step("harmonise_units", "incompatible", reported=reported_unit,
             expected=canonical_unit)
        return _unknown(
            kb, test_name, value, unit, evidence, flags,
            f"Unit '{unit}' is not a recognised unit for {concept['display_name']} "
            f"(expected {canonical_unit}). Not classified, to avoid comparing "
            "incompatible units.",
            [], concept_key=res.concept_key, display_name=concept["display_name"],
        )
    evidence["unit_conversion"] = conversion

    # --- 4. select the reference range -----------------------------------
    if has_row_range:
        low, high = row_lo, row_hi
        range_source = "row_reference_range"
        citation = "Reference interval supplied with the result by the issuing laboratory"
        matched_on = None
    else:
        r = kb.select_range(res.concept_key, sex, age_years)
        if not r:
            return _unknown(kb, test_name, value, unit, evidence, flags,
                            "No reference interval available for this test", [],
                            concept_key=res.concept_key,
                            display_name=concept["display_name"])
        low, high = r.get("low"), r.get("high")
        range_source = "knowledge_base"
        matched_on = r.get("sex")
        citation = (f"Adult reference interval (knowledge base v{kb.version}), "
                    f"sex profile: {matched_on}")
        has_sex_specific = any(r.get("sex") in ("male", "female")
                               for r in concept.get("ranges", []))
        if matched_on == "any" and sex and has_sex_specific:
            flags.append("sex_specific_range_unavailable")

    evidence["reference_range"] = {
        "low": low, "high": high, "unit": working_unit, "source": range_source,
        "matched_on_sex": matched_on, "citation": citation,
    }
    step("select_reference_range", range_source, low=low, high=high, unit=working_unit)

    # --- 5. score the deviation ------------------------------------------
    d, direction = deviation_index(number, low, high)
    evidence["deviation"] = {
        "index": round(d, 4), "direction": direction,
        "interpretation": _deviation_words(d),
    }

    # --- 6. apply the severity rules --------------------------------------
    # Most analytes escalate at one interval width outside. A few have narrow
    # physiological intervals but a wide clinically tolerable range (fasting
    # glucose, HbA1c, TSH), so they carry an explicit band in the knowledge
    # base rather than being special-cased in code.
    critical_band = float(concept.get("critical_band", WARNING_BAND))

    panic = concept.get("panic") or {}
    panic_low, panic_high = panic.get("low"), panic.get("high")
    panic_hit = None
    if panic_low is not None and number <= panic_low:
        panic_hit = {"bound": "low", "limit": panic_low}
    elif panic_high is not None and number >= panic_high:
        panic_hit = {"bound": "high", "limit": panic_high}

    if panic_hit:
        status, rule = "Critical", "panic_limit"
        rationale = (f"{number:g} {working_unit} breaches the absolute action limit of "
                     f"{panic_hit['limit']:g} {working_unit}, which forces Critical "
                     "regardless of how wide the reference interval is.")
        direction = "High" if panic_hit["bound"] == "high" else "Low"
        flags.append("panic_limit_breached")
    elif d == 0:
        status, rule = "Normal", "within_reference_interval"
        rationale = (f"{number:g} {working_unit} lies inside the reference interval "
                     f"{_fmt_range(low, high)} {working_unit}.")
        if _is_borderline(number, low, high):
            flags.append("borderline")
            rationale += " It sits close to an interval bound, so a repeat is worth watching."
    elif d <= critical_band:
        status, rule = "Warning", "outside_interval_within_critical_band"
        rationale = (f"{number:g} {working_unit} is {direction.lower()} against the reference "
                     f"interval {_fmt_range(low, high)} {working_unit}, by {d:.2f} interval "
                     f"widths. The critical band for this test starts at {critical_band:g} "
                     "interval widths, so this is abnormal but not critical.")
    else:
        status, rule = "Critical", "deviation_beyond_critical_band"
        rationale = (f"{number:g} {working_unit} is {direction.lower()} against the reference "
                     f"interval {_fmt_range(low, high)} {working_unit}, by {d:.2f} interval "
                     f"widths, which exceeds this test's critical band of {critical_band:g}.")

    if parsed.operator and status == "Normal":
        flags.append("censored_value_normal_assumed")

    step("apply_rules", rule, status=status)
    evidence["rule"] = {"id": rule, "rationale": rationale,
                        "panic_limit": panic_hit, "bands": {
                            "normal": "d = 0", "warning": f"0 < d <= {WARNING_BAND}",
                            "critical": f"d > {WARNING_BAND} or panic limit breached"}}

    pathway = kb.care_pathway(res.concept_key, status, direction)

    return {
        "test_name": test_name,
        "concept_key": res.concept_key,
        "display_name": concept["display_name"],
        "category": concept["category"],
        "loinc": concept.get("loinc"),
        "value": parsed.number,
        "value_display": _fmt_value(parsed),
        "unit": unit or working_unit,
        "canonical_value": round(number, 6),
        "canonical_unit": working_unit,
        "unit_converted": conversion is not None,
        "status": status,
        "direction": direction,
        "deviation_index": round(d, 4),
        "reference_low": low,
        "reference_high": high,
        "flags": sorted(set(flags)),
        "measures": concept.get("measures", ""),
        "direction_meaning": (concept.get("high_meaning") if direction == "High"
                              else concept.get("low_meaning") if direction == "Low" else ""),
        "care_pathway": pathway,
        "evidence": evidence,
        "error": None,
    }


def _classify_qualitative(kb, res, concept, parsed, unit, evidence, flags,
                          raw_name: str = "") -> dict:
    """Severity for dipstick-style results (Negatif / Trace / 1+ / 2+ ...)."""
    qmap = concept.get("qualitative_map") or {}
    grade = parsed.grade

    if grade is None and parsed.number is not None:
        # A numeric value on a qualitative pad (rare) - treat 0 as negative.
        grade = "negative" if parsed.number == 0 else "positive"
        flags.append("numeric_value_on_qualitative_test")

    status = qmap.get(grade or "")
    if status is None:
        return _unknown(
            kb, raw_name or res.display_name or "", parsed.raw, unit, evidence, flags,
            f"'{parsed.raw}' is not a recognised result for {concept['display_name']}. "
            f"Expected one of: {', '.join(sorted(qmap))}.",
            [], concept_key=res.concept_key, display_name=concept["display_name"],
        )

    direction = "Normal" if status == "Normal" else "Positive"
    rank = _GRADE_RANK.get(grade, 0)
    rationale = (f"Dipstick result '{parsed.raw}' maps to grade '{grade}' on the "
                 f"{concept['display_name']} scale, which the protocol classifies as "
                 f"{status}.")
    evidence["reference_range"] = {
        "low": None, "high": None, "unit": "-", "source": "qualitative_scale",
        "matched_on_sex": None,
        "citation": f"Qualitative grading scale for {concept['display_name']} "
                    f"(knowledge base v{kb.version})",
        "scale": qmap,
    }
    evidence["deviation"] = {"index": None, "direction": direction,
                             "interpretation": f"grade rank {rank} on the dipstick scale"}
    evidence["rule"] = {"id": "qualitative_scale", "rationale": rationale,
                        "panic_limit": None, "bands": qmap}
    evidence["steps"].append({"step": "apply_rules", "outcome": "qualitative_scale",
                              "status": status})

    return {
        "test_name": raw_name or concept["display_name"],
        "concept_key": res.concept_key,
        "display_name": concept["display_name"],
        "category": concept["category"],
        "loinc": concept.get("loinc"),
        "value": None,
        "value_display": parsed.raw,
        "unit": unit or "-",
        "canonical_value": None,
        "canonical_unit": "-",
        "status": status,
        "direction": direction,
        "deviation_index": None,
        "reference_low": None,
        "reference_high": None,
        "flags": sorted(set(flags)),
        "measures": concept.get("measures", ""),
        "direction_meaning": concept.get("high_meaning", "") if status != "Normal" else "",
        "care_pathway": kb.care_pathway(res.concept_key, status, direction),
        "evidence": evidence,
        "error": None,
    }


def _unknown(kb, test_name, value, unit, evidence, flags, reason,
             candidates, concept_key=None, display_name=None) -> dict:
    """Build an Unknown result: routed to a human rather than guessed at."""
    flags = sorted(set(flags + ["needs_human_review"]))
    evidence["rule"] = {
        "id": "unclassifiable", "rationale": reason, "panic_limit": None,
        "bands": None,
    }
    evidence["steps"].append({"step": "apply_rules", "outcome": "unclassifiable",
                              "status": "Unknown"})
    pathway = kb.care_pathway(concept_key, "Unknown", None)
    if candidates:
        names = [kb.concepts[c]["display_name"] for c in candidates if c in kb.concepts]
        if names:
            pathway = {**pathway, "actions": pathway["actions"] +
                       [f"Did you mean: {', '.join(names)}?"]}
    return {
        "test_name": test_name,
        "concept_key": concept_key,
        "display_name": display_name or test_name,
        "category": kb.concepts.get(concept_key or "", {}).get("category", "Unclassified"),
        "loinc": None,
        "value": None,
        "value_display": "" if value is None else str(value),
        "unit": unit or "",
        "canonical_value": None,
        "canonical_unit": "",
        "status": "Unknown",
        "direction": "Unknown",
        "deviation_index": None,
        "reference_low": None,
        "reference_high": None,
        "flags": flags,
        "measures": kb.concepts.get(concept_key or "", {}).get("measures", ""),
        "direction_meaning": "",
        "care_pathway": pathway,
        "evidence": evidence,
        "error": reason,
    }


def _coerce(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _is_borderline(value: float, low: float | None, high: float | None) -> bool:
    if low is None or high is None or high <= low:
        return False
    margin = (high - low) * BORDERLINE_FRACTION
    return value <= low + margin or value >= high - margin


def _deviation_words(d: float) -> str:
    if d == 0:
        return "inside the reference interval"
    if d <= 0.25:
        return "just outside the reference interval"
    if d <= WARNING_BAND:
        return "clearly outside the reference interval"
    if d <= 3:
        return "well outside the reference interval"
    return "extremely far outside the reference interval"


def _fmt_range(low: float | None, high: float | None) -> str:
    if low is not None and high is not None:
        return f"{low:g}-{high:g}"
    if high is not None:
        return f"<={high:g}"
    if low is not None:
        return f">={low:g}"
    return "not defined"


def _fmt_value(p: ParsedValue) -> str:
    if p.kind == "numeric" and p.number is not None:
        return f"{p.operator or ''}{p.number:g}"
    return p.raw
