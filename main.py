"""
main.py — MetalMind FastAPI Backend (Unified Architecture)

Single analysis flow:
  Rule Engine → AI Cross-Check → Trust Signal → UI

AI Insight is on-demand (user clicks a supplier to load it).

Run:
    uvicorn main:app --reload
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_log = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from services.search import search_suppliers as multi_search
from services.serper_client import SerperError
from modules.cleaner        import clean_results
from modules.risk_scorer    import RiskLevel, score_all
from modules.value_scorer   import compute_value_scores
from modules.ranker         import rank_suppliers, get_top3, get_winner
from engine.recommendation  import generate_recommendation
from engine.ai_engine        import call_model
from engine.ai_insight       import generate_insight
from engine.ai_crosscheck    import cross_check
from engine.anomaly          import dataset_median, detect_anomalies
from engine.ai_adjustment    import from_crosscheck as adj_from_crosscheck, apply as adj_apply
from modules.currency       import get_rates
from config                 import DECISION_SCORE_THRESHOLD
from db import init_db
from routes.suppliers import router as suppliers_router

app = FastAPI(title="MetalMind API")

app.include_router(suppliers_router)
init_db()

FRONTEND = Path(__file__).parent / "frontend"


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    max_results: int = Field(default=5, ge=1, le=20)
    priority:    str = "Both Equal"


class SupplierOut(BaseModel):
    rank:         int
    name:         str
    country:      str
    url:          str
    description:  str
    price_usd:    float | None
    price_raw:    str
    risk_level:   str
    risk_score:   float
    risk_reasons: list[str]
    value_score:  float
    base_score:   float | None = None
    ai_adjustment: dict | None = None
    trust:        str | None = None       # "safe" | "warning" | "risk" | None
    anomalies:    dict | None = None


class AnalyzeResponse(BaseModel):
    currency:      str
    symbol:        str
    fx:            float
    winner:        SupplierOut
    top3:          list[SupplierOut]
    all_suppliers: list[SupplierOut]
    summary:       str
    explanation:   str
    risk_note:     str
    decision:      str = "recommended"
    trust:         str = "safe"           # top-level trust signal for the winner


class InsightResponse(BaseModel):
    name:       str
    summary:    str
    key_strengths:  list[str]
    key_risks:      list[str]
    hidden_signals: list[str]
    confidence: float
    source:     str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_suppliers(req: AnalyzeRequest, rates: dict):
    """
    Multi-source search (Serper primary, Tavily fallback/enrichment)
    for India + China in parallel, then rule-based cleaning.
    """
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_india = ex.submit(multi_search, "India", max_results=req.max_results)
            f_china = ex.submit(multi_search, "China", max_results=req.max_results)
            india_raw = f_india.result()
            china_raw = f_china.result()
    except (SerperError, Exception) as e:
        raise HTTPException(status_code=503, detail=f"Supplier search unavailable: {e}")

    t1 = time.time()
    india_clean = clean_results(india_raw, country_override="India", rates=rates, use_ai=False)
    china_clean = clean_results(china_raw, country_override="China", rates=rates, use_ai=False)
    all_records = india_clean + china_clean

    _log.info("[perf] search=%.1fs  clean=%.2fs  records=%d", t1 - t0, time.time() - t1, len(all_records))

    if not all_records:
        raise HTTPException(status_code=404, detail="No ACP suppliers found.")
    return all_records


def _to_supplier_out(rank: int, v, median: float | None) -> SupplierOut:
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
        base_score=round(v.value_score * 100, 1),
        anomalies=detect_anomalies(v, median),
    )


def _trust_from_validation(validation: dict) -> str:
    """Map cross-check validation to simple trust signal."""
    if validation.get("source") == "fallback":
        return "safe"
    if validation.get("is_valid") is False:
        return "risk"
    if validation.get("issues") or validation.get("risk_warnings"):
        return "warning"
    return "safe"


def _supplier_audit_dict(v, symbol: str, fx: float) -> dict:
    rec = v.scored.record
    price = f"{symbol}{(rec.price_est * fx):.2f}/sqm" if rec.price_est is not None else "unknown"
    return {
        "name": rec.name, "country": rec.country,
        "price_display": price, "price_raw": rec.price_raw,
        "risk_level": v.scored.risk_level.value,
        "value_score": round(v.value_score * 100, 1),
        "url": rec.url, "description": rec.description,
    }


def _build_explanation(trust: str, winner_name: str, validation: dict, expert_fallback: str) -> str:
    if validation.get("source") == "fallback":
        return expert_fallback
    if trust == "risk":
        issue = (validation.get("issues") or [""])[0]
        tail = f" Concern: {issue}" if issue else ""
        return f"AI flagged a serious problem with {winner_name}.{tail} Consider an alternative."
    if trust == "warning":
        warn = (validation.get("risk_warnings") or validation.get("issues") or [""])[0]
        tail = f" Note: {warn}" if warn else ""
        return f"{winner_name} is the top pick, but AI raised additional warnings.{tail}"
    return f"{winner_name} is validated — rules and AI agree this is a strong choice."


def _build_risk_note(winner_out: SupplierOut, validation: dict) -> str:
    lines = ["=== Rule-based signals ==="]
    lines += [f"  {r}" for r in (winner_out.risk_reasons or ["None"])]

    if validation.get("source") == "ai":
        verdict = "APPROVED" if validation.get("is_valid") else "REJECTED"
        lines += ["", f"=== AI Auditor: {verdict} ==="]
        for i in (validation.get("issues") or []):
            lines.append(f"  {i}")
        for w in (validation.get("risk_warnings") or []):
            lines.append(f"  {w}")
        for a in (validation.get("alternative_suggestions") or []):
            lines.append(f"  Consider: {a}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Server-side cache for on-demand insight lookups
# ---------------------------------------------------------------------------

_last_ranked: dict[str, object] = {}   # name → ValuedSupplier


# ---------------------------------------------------------------------------
# PRIMARY ENDPOINT — /api/analyze
#
# Pipeline: Rule Engine → AI Cross-Check → Trust Signal → Response
# ---------------------------------------------------------------------------

@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    t_start = time.time()

    if not os.environ.get("TAVILY_API_KEY"):
        raise HTTPException(status_code=400, detail="TAVILY_API_KEY not configured.")

    gemma_available = bool(os.environ.get("GEMMA_API_KEY"))

    rates       = get_rates()
    all_records = _fetch_suppliers(req, rates)

    # --- Layer 1: RULE ENGINE (decision maker) ---
    scored = score_all(all_records)
    valued = compute_value_scores(scored)
    ranked = rank_suppliers(valued, priority=req.priority)
    top3   = get_top3(ranked)
    winner = get_winner(top3)
    narr   = generate_recommendation(winner, top3)
    median = dataset_median(ranked)

    # Cache ranked results for on-demand insight
    global _last_ranked
    _last_ranked = {v.scored.record.name: v for v in ranked}

    fx     = rates.get("AUD", 1.58)
    symbol = "A$"

    # --- Layer 2: AI CROSS-CHECK (automatic, single call) ---
    validation = {"source": "fallback"}
    if gemma_available:
        t_ai = time.time()
        winner_audit = _supplier_audit_dict(winner, symbol, fx)
        alt_audits   = [_supplier_audit_dict(v, symbol, fx) for v in top3 if v is not winner]
        validation   = cross_check(winner_audit, alt_audits)
        _log.info("[perf] cross-check=%.1fs", time.time() - t_ai)

    # --- Layer 3: TRUST SIGNAL ---
    trust = _trust_from_validation(validation)

    # Apply cross-check adjustment to winner's score
    winner_adj = adj_from_crosscheck(validation)

    all_out: list[SupplierOut] = []
    for v in ranked:
        so = _to_supplier_out(0, v, median)
        if v is winner:
            so.trust = trust
            if winner_adj.get("adjustment"):
                so.value_score = adj_apply(so.base_score, winner_adj)
                so.ai_adjustment = winner_adj
        all_out.append(so)

    # Re-rank by final score (cross-check adjustment may shift winner)
    all_out.sort(key=lambda s: s.value_score, reverse=True)
    for i, s in enumerate(all_out):
        s.rank = i + 1

    top3_out   = all_out[:3]
    winner_out = all_out[0]

    # If re-ranking promoted a different supplier to #1 (because the original
    # winner got a negative adjustment), the new winner inherits "safe" trust
    # — it passed rules and AI didn't flag it specifically.
    if not winner_out.trust:
        winner_out.trust = "safe"
    trust = winner_out.trust

    explanation = _build_explanation(trust, winner_out.name, validation, narr.explanation)
    risk_note   = _build_risk_note(winner_out, validation)

    decision = (
        "recommended"
        if winner_out.value_score >= DECISION_SCORE_THRESHOLD * 100
        and winner_out.risk_level != RiskLevel.HIGH.value
        else "not_recommended"
    )

    _log.info("[perf] /api/analyze  total=%.1fs  trust=%s", time.time() - t_start, trust)

    return AnalyzeResponse(
        currency="AUD", symbol=symbol, fx=fx,
        winner=winner_out, top3=top3_out, all_suppliers=all_out,
        summary=narr.summary, explanation=explanation,
        risk_note=risk_note, decision=decision, trust=trust,
    )


# ---------------------------------------------------------------------------
# ON-DEMAND INSIGHT — /api/insight
#
# Triggered by user click on a supplier. Not part of the primary flow.
# ---------------------------------------------------------------------------

class InsightRequest(BaseModel):
    name: str


@app.post("/api/insight", response_model=InsightResponse)
def get_insight(req: InsightRequest):
    if not os.environ.get("GEMMA_API_KEY"):
        raise HTTPException(status_code=400, detail="GEMMA_API_KEY not configured.")

    v = _last_ranked.get(req.name)
    if v is None:
        raise HTTPException(status_code=404, detail=f"Supplier '{req.name}' not found. Run an analysis first.")

    t0 = time.time()
    result = generate_insight(v.scored.record)
    _log.info("[perf] /api/insight '%s' %.1fs", req.name[:30], time.time() - t0)

    return InsightResponse(
        name=req.name,
        summary=result.get("summary", ""),
        key_strengths=result.get("key_strengths", []),
        key_risks=result.get("key_risks", []),
        hidden_signals=result.get("hidden_signals", []),
        confidence=result.get("confidence", 0.0),
        source=result.get("source", "fallback"),
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(check_serper: bool = False, check_tavily: bool = False, check_gemma: bool = False):
    result: dict = {"status": "ok"}

    if check_serper:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        if not serper_key:
            result["serper"] = {"ok": False, "error": "SERPER_API_KEY not set"}
        else:
            try:
                from services.serper_client import search as serper_search_fn
                hits = serper_search_fn("ACP aluminium composite panel", max_results=1)
                result["serper"] = {"ok": True, "results": len(hits)}
            except Exception as e:
                result["serper"] = {"ok": False, "error": str(e)[:300]}

    if check_tavily:
        from services.tavily_client import is_available, search_fallback, TavilyError
        if not is_available():
            result["tavily"] = {"ok": False, "error": "TAVILY_API_KEY not set (optional — Serper is primary)"}
        else:
            try:
                hits = search_fallback("ACP aluminium composite panel", max_results=1)
                result["tavily"] = {"ok": True, "results": len(hits)}
            except TavilyError as e:
                result["tavily"] = {"ok": False, "error": str(e)[:300]}

    if check_gemma:
        gemma_key = os.environ.get("GEMMA_API_KEY", "")
        if not gemma_key:
            result["gemma"] = {"ok": False, "error": "GEMMA_API_KEY not set"}
        else:
            try:
                text = call_model('Return exactly this JSON: {"ping": true}')
                result["gemma"] = {"ok": True, "sample": text[:120]} if text else {"ok": False, "error": "Empty response"}
            except Exception as e:
                result["gemma"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:280]}"}

    return result


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

@app.get("/my-suppliers")
def serve_my_suppliers_page():
    return FileResponse(FRONTEND / "my-suppliers.html")

@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    if f"/{full_path}".startswith("/api/"):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(FRONTEND / "index.html")
