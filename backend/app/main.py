"""
FastAPI application.

Endpoints:
    POST /analyze_labs      the assignment's endpoint - classify, route, explain
    POST /analyze_csv       upload a CSV and analyse it in one step
    GET  /catalog           supported tests, for the frontend picker
    GET  /evaluate          classifier accuracy against the labelled Kaggle data
    GET  /health            MCP + AI provider status
"""

from __future__ import annotations

import csv
import io
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .agent import DISCLAIMER, LabAnalysisAgent
from .csv_ingest import CSVIngestError, parse_csv
from .llm import GeminiProvider
from .mcp_client import MCPToolClient, MCPUnavailable
from .schemas import AnalyzeRequest, AnalyzeResponse, EvaluationReport

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("labs")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KAGGLE_CSV = PROJECT_ROOT / "test_data" / "kaggle_lab_test_results_public.csv"

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the MCP session once, and keep it for the life of the process."""
    mcp = MCPToolClient()
    await mcp.start()
    llm = GeminiProvider()
    state["mcp"] = mcp
    state["llm"] = llm
    state["agent"] = LabAnalysisAgent(mcp, llm)
    log.info("AI provider: %s", llm.status()["detail"])
    try:
        yield
    finally:
        await mcp.stop()


app = FastAPI(
    title="Clinical Lab Results Analyzer",
    version="1.0.0",
    description=(
        "Classifies lab results with a deterministic rule engine served over MCP, "
        "then uses an LLM only to explain them - with every explanation verified "
        "against the facts it was generated from."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in
                   os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
                   .split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(MCPUnavailable)
async def _mcp_unavailable(request, exc: MCPUnavailable):
    return JSONResponse(
        status_code=503,
        content={"detail": f"Clinical knowledge server unavailable: {exc}",
                 "hint": "Check /health for the MCP session status."},
    )


@app.get("/health")
async def health() -> dict:
    mcp: MCPToolClient = state["mcp"]
    llm: GeminiProvider = state["llm"]
    ok = mcp.connected
    return {
        "status": "ok" if ok else "degraded",
        "mcp": mcp.status(),
        "ai_provider": llm.status(),
        "mode": ("full" if ok and llm.available else
                 "rule-based only (no AI key)" if ok else "unavailable"),
        "disclaimer": DISCLAIMER,
    }


@app.get("/catalog")
async def catalog() -> dict:
    """Tests this build can classify - drives the frontend's test picker."""
    return await state["mcp"].call("list_catalog", {})


@app.post("/analyze_labs", response_model=AnalyzeResponse)
async def analyze_labs(request: AnalyzeRequest) -> AnalyzeResponse:
    """Classify, route and explain a set of lab results."""
    result = await state["agent"].analyze(
        labs=[lab.model_dump() for lab in request.labs],
        patient=request.patient.model_dump() if request.patient else None,
        options=request.options.model_dump(),
    )
    return AnalyzeResponse(**result)


@app.post("/analyze_csv")
async def analyze_csv(
    file: UploadFile = File(...),
    sex: str | None = Form(None),
    age_years: float | None = Form(None),
    explain: bool = Form(True),
) -> dict:
    """Upload a lab CSV and analyse it. Column names are mapped by synonym."""
    if not file.filename or not file.filename.lower().endswith((".csv", ".txt", ".tsv")):
        raise HTTPException(400, "Please upload a .csv, .tsv or .txt file.")

    raw = await file.read()
    try:
        parsed = parse_csv(raw, file.filename)
    except CSVIngestError as exc:
        raise HTTPException(400, str(exc)) from exc

    patient = dict(parsed["patient"] or {})
    if sex in ("male", "female"):
        patient["sex"] = sex
    if age_years is not None:
        patient["age_years"] = age_years

    result = await state["agent"].analyze(
        labs=parsed["labs"], patient=patient or None,
        options={"explain": explain, "include_evidence": True},
    )
    result["ingest"] = {
        "filename": parsed["filename"],
        "delimiter": parsed["delimiter"],
        "rows_accepted": parsed["row_count"],
        "column_mapping": parsed["mapping"],
        "unmapped_columns": parsed["unmapped_columns"],
        "warnings": parsed["warnings"],
        "patient_from_file": parsed["patient"],
    }
    return result


SAMPLES = {
    "kaggle": ("kaggle_lab_test_results_public.csv", "Kaggle dataset",
               "The 27-row anonymised dataset this project targets, with Turkish test "
               "names and dipstick results."),
    "critical": ("panel_critical.csv", "Deteriorating inpatient",
                 "Acute kidney injury with sepsis and cardiac involvement. Exercises "
                 "action limits and severity routing."),
    "mixed": ("panel_mixed.csv", "Routine outpatient review",
              "Mostly normal, with iron deficiency and subclinical hypothyroidism. "
              "Turkish test names."),
    "edge": ("panel_edge_cases.csv", "Edge cases",
             "Unit conversions, ambiguous names, misspellings, censored and "
             "unparseable values."),
}


@app.get("/samples")
async def samples() -> dict:
    """Bundled panels the frontend offers as one-click demos."""
    return {"samples": [
        {"id": k, "label": label, "description": desc, "filename": fn,
         "available": (PROJECT_ROOT / "test_data" / fn).exists()}
        for k, (fn, label, desc) in SAMPLES.items()
    ]}


@app.post("/analyze_sample/{sample_id}")
async def analyze_sample(sample_id: str, explain: bool = True) -> dict:
    """Analyse one of the bundled panels without needing an upload."""
    if sample_id not in SAMPLES:
        raise HTTPException(404, f"Unknown sample '{sample_id}'.")
    filename, label, _ = SAMPLES[sample_id]
    path = PROJECT_ROOT / "test_data" / filename
    if not path.exists():
        raise HTTPException(404, f"Sample file missing: {path}")

    parsed = parse_csv(path.read_bytes(), filename)
    result = await state["agent"].analyze(
        labs=parsed["labs"], patient=parsed["patient"],
        options={"explain": explain, "include_evidence": True},
    )
    result["ingest"] = {
        "filename": filename, "delimiter": parsed["delimiter"],
        "rows_accepted": parsed["row_count"], "column_mapping": parsed["mapping"],
        "unmapped_columns": parsed["unmapped_columns"], "warnings": parsed["warnings"],
        "patient_from_file": parsed["patient"], "sample_label": label,
    }
    return result


@app.get("/evaluate", response_model=EvaluationReport)
async def evaluate() -> EvaluationReport:
    """Score the classifier against the Kaggle dataset's own Status labels.

    The bundled dataset ships a clinician-assigned status per row, which makes
    it a genuine held-out check on the rule engine rather than a self-graded
    demo. Turkish labels are mapped to the engine's vocabulary.
    """
    if not KAGGLE_CSV.exists():
        raise HTTPException(404, f"Labelled dataset not found at {KAGGLE_CSV}")

    label_map = {"normal": "Normal", "yüksek": "High", "yuksek": "High",
                 "düşük": "Low", "dusuk": "Low", "high": "High", "low": "Low"}
    rows = list(csv.DictReader(io.StringIO(KAGGLE_CSV.read_text("utf-8-sig"))))

    mcp: MCPToolClient = state["mcp"]
    matrix: dict[str, dict[str, int]] = {}
    disagreements = []
    agree_count = 0

    for row in rows:
        expected = label_map.get((row.get("Status") or "").strip().lower(), "Unmapped")
        res = await mcp.call("classify_lab_result", {
            "test_name": row.get("Test_Name", ""),
            "value": row.get("Result", ""),
            "unit": row.get("Unit"),
            "reference_low": _num(row.get("Min_Reference")),
            "reference_high": _num(row.get("Max_Reference")),
            "reference_range_text": row.get("Reference_Range"),
        })

        predicted_abnormal = res["status"] in ("Warning", "Critical")
        expected_abnormal = expected in ("High", "Low")
        # "Positive" on a dipstick is the engine's vocabulary for the dataset's
        # "Yüksek" - an abnormal qualitative pad has no numeric direction.
        direction_ok = (not predicted_abnormal or
                        res.get("direction") in (expected, "Positive"))
        agree = predicted_abnormal == expected_abnormal and direction_ok

        agree_count += agree
        matrix.setdefault(expected, {}).setdefault(res["status"], 0)
        matrix[expected][res["status"]] += 1
        if not agree:
            disagreements.append({
                "test_name": row.get("Test_Name", ""), "value": row.get("Result", ""),
                "expected": expected, "predicted": res["status"],
                "direction": res.get("direction", ""), "agree": False,
                "note": res.get("error") or "",
            })

    total = len(rows) or 1
    return EvaluationReport(
        dataset=KAGGLE_CSV.name,
        rows=len(rows),
        agreements=agree_count,
        accuracy=round(agree_count / total, 4),
        by_expected=matrix,
        disagreements=disagreements,
        note=("Agreement is scored on abnormality and its direction: the dataset labels "
              "Normal/Yüksek/Düşük, while the engine additionally separates Warning from "
              "Critical, a distinction the dataset does not make."),
    )


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
