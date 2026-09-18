"""Local Filter Floor API. Binds 127.0.0.1 only. No auth because it is localhost."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from filter_floor.models import Chain, ScanResult, Verdict
from filter_floor.pipeline import data_dir_from_env, run_scan
from filter_floor.storage.cases import list_case_summaries, read_scan
from filter_floor.storage.funnel import compute_funnel

HOST = "127.0.0.1"
PORT = 3001
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Filter Floor",
    description=(
        "Local pre-buy filter API bound to 127.0.0.1 only. "
        "No auth because it binds localhost. Do not expose this process. "
        "Verdicts: AVOID, CAUTION, PASS_FILTER."
    ),
)


class ScanRequest(BaseModel):
    chain: Chain
    token: str = Field(..., min_length=1)


class CaseSummary(BaseModel):
    case_id: str
    chain: Chain
    token: str
    score: int
    verdict: Verdict
    scanned_at: str


@app.get("/", response_class=HTMLResponse)
def get_index() -> str:
    path = STATIC_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="ui not found")
    return path.read_text(encoding="utf-8")


@app.post("/scan", response_model=ScanResult)
def post_scan(body: ScanRequest) -> ScanResult:
    try:
        return run_scan(body.chain, body.token.strip(), data_dir_from_env())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/cases", response_model=list[CaseSummary])
def get_cases(days: int = Query(1, ge=1)) -> list[CaseSummary]:
    rows = list_case_summaries(data_dir_from_env(), days=days)
    return [CaseSummary.model_validate(row) for row in rows]


@app.get("/cases/{case_id}", response_model=ScanResult)
def get_case(case_id: str) -> ScanResult:
    try:
        return read_scan(case_id, data_dir_from_env())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/funnel")
def get_funnel(days: int = Query(7, ge=1)) -> dict:
    counts = compute_funnel(data_dir_from_env(), days=days)
    return counts.as_json(datetime.now(timezone.utc))
