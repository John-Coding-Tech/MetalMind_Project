"""
main.py — MetalMind FastAPI Backend

Serves the frontend and exposes the supplier comparison API.

Run:
    uvicorn main:app --reload
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from services.tavily_client import search_india_suppliers, search_china_suppliers
from modules.cleaner        import clean_results
from modules.risk_scorer    import score_all
from modules.value_scorer   import compute_value_scores
from modules.ranker         import rank_suppliers, get_top3, get_winner
from engine.recommendation  import generate_recommendation

load_dotenv()

app = FastAPI(title="MetalMind API")

FRONTEND = Path(__file__).parent / "frontend"

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CompareRequest(BaseModel):
    max_results: int  = 8
    priority:    str  = "India"       # "India" | "China" | "Both Equal"
    currency:    str  = "AUD"         # "AUD" | "USD"
    usd_to_aud:  float = 1.58


class SupplierOut(BaseModel):
    rank:        int
    name:        str
    country:     str
    url:         str
    description: str
    price_usd:   float | None
    price_raw:   str
    risk_level:  str
    risk_score:  float
    risk_reasons: list[str]
    value_score: float


class CompareResponse(BaseModel):
    currency:       str
    symbol:         str
    fx:             float
    winner:         SupplierOut
    top3:           list[SupplierOut]
    all_suppliers:  list[SupplierOut]
    summary:        str
    explanation:    str
    risk_note:      str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_supplier_out(rank: int, v) -> SupplierOut:
    return SupplierOut(
        rank=rank,
        name=v.scored.record.name,
        country=v.scored.record.country,
        url=v.scored.record.url,
        description=v.scored.record.description,
        price_usd=v.price_used,
        price_raw=v.scored.record.price_raw,
        risk_level=v.scored.risk_level.value,
        risk_score=v.scored.risk_score,
        risk_reasons=v.scored.risk_reasons,
        value_score=round(v.value_score * 100, 1),
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/compare", response_model=CompareResponse)
def compare(req: CompareRequest):
    if not os.environ.get("TAVILY_API_KEY"):
        raise HTTPException(status_code=400, detail="TAVILY_API_KEY not configured.")

    # Step 1 — Search
    india_raw = search_india_suppliers(max_results=req.max_results)
    china_raw = search_china_suppliers(max_results=req.max_results)

    # Step 2 — Clean
    india_clean = clean_results(india_raw, country_override="India")
    china_clean = clean_results(china_raw, country_override="China")
    all_records = india_clean + china_clean

    if not all_records:
        raise HTTPException(status_code=404, detail="No ACP suppliers found.")

    # Step 3 — Risk scoring
    scored = score_all(all_records)

    # Step 4 — Value scoring
    valued = compute_value_scores(scored)

    # Step 5 & 6 — Rank + Top 3
    ranked = rank_suppliers(valued, priority=req.priority)
    top3   = get_top3(ranked)
    winner = get_winner(top3)

    # Step 7 — Recommendation
    result = generate_recommendation(winner, top3)

    # Currency
    fx     = req.usd_to_aud if req.currency == "AUD" else 1.0
    symbol = "A$" if req.currency == "AUD" else "$"

    return CompareResponse(
        currency=req.currency,
        symbol=symbol,
        fx=fx,
        winner=_to_supplier_out(1, winner),
        top3=[_to_supplier_out(i + 1, v) for i, v in enumerate(top3)],
        all_suppliers=[_to_supplier_out(i + 1, v) for i, v in enumerate(ranked)],
        summary=result.summary,
        explanation=result.explanation,
        risk_note=result.risk_note,
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    return FileResponse(FRONTEND / "index.html")
