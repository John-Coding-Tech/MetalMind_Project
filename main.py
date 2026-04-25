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
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_log = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from services.search import search_suppliers as multi_search
from services.multi_search import multi_search_and_merge
from services.serper_client import SerperError
from modules.cleaner        import clean_results
from modules.risk_scorer    import RiskLevel, score_all
from modules.value_scorer   import compute_value_scores
from modules.ranker         import rank_suppliers, get_top3, get_winner
from engine.recommendation  import generate_recommendation
from engine.ai_engine        import call_model
from engine.ai_insight       import generate_insight
from engine.ai_crosscheck    import cross_check
from engine.anomaly          import dataset_median, dataset_medians, detect_anomalies
from engine.ai_adjustment    import from_crosscheck as adj_from_crosscheck, apply as adj_apply
from engine.query_parser    import parse_search_query
from engine.price_estimator import (
    estimate_supplier_price,
    classify_price_vs_market,
    market_reference_for,
)
from modules.currency       import get_rates
from config                 import DECISION_SCORE_THRESHOLD, ANALYZE_TOTAL_BUDGET, DISPLAY_SUPPLIER_LIMIT
from db import init_db
from routes.suppliers import router as suppliers_router

app = FastAPI(title="MetalMind API")

# ---------------------------------------------------------------------------
# Middleware — security headers and CORS
# ---------------------------------------------------------------------------

_CORS_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_CORS_ORIGIN] if _CORS_ORIGIN != "*" else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp


app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    _log.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(suppliers_router)


@app.on_event("startup")
def _startup():
    try:
        init_db()
    except Exception:
        _log.exception("Database initialisation failed — check DATABASE_URL")

FRONTEND = Path(__file__).parent / "frontend"


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    max_results: int = Field(default=5, ge=1, le=20)
    # New chat-driven fields. When `query` is empty the endpoint falls back to
    # the legacy ACP India+China search for backward compatibility.
    query:  str        = Field(default="", max_length=2000)
    parsed: dict | None = None    # frontend may send an edited parse override
    debug:  bool       = False    # when true, response includes raw trace


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
    # Multi-metal / multi-unit additions
    category:          str | None = None  # acp | aluminum | steel | ...
    price_unit:        str | None = None  # sqm | ton | kg | meter | piece | ft | unknown
    price_unit_source: str | None = None  # regex | keyword | category | unknown
    price_original:    str | None = None  # e.g. "CNY 25000-28000/ton"
    angle_count:       int | None = None  # how many search angles matched this URL
    angles_matched:    list[str] | None = None
    bucket_key:        str | None = None  # "category:canonical_unit"
    bucket_size:       int | None = None
    # --- Hybrid pricing (path C) ---------------------------------------
    # Per-supplier model estimate — populated ONLY when no real price was
    # extracted. Always a range (low/high in USD per canonical unit), never
    # a single point, and always labeled `price_estimate_source="model"`
    # so the frontend can apply the "⚠ model" treatment.
    price_estimated_low_usd:  float | None = None
    price_estimated_high_usd: float | None = None
    price_estimate_source:    str   | None = None   # "model" or None

    # Classification of real scraped price vs per-country market midpoint.
    # None when no real price to compare. Values: suspicious_low | within |
    # above | far_above.
    price_range_status: str | None = None


class MarketReference(BaseModel):
    """
    Top-of-results market reference band for the "hybrid pricing" banner.
    Always in AUD (matching the response currency) for direct display.
    """
    low_aud:          float
    high_aud:         float
    unit:             str
    category:         str
    country_scope:    list[str]         # [] means global
    samples_from_search: int            # # of suppliers in this search with real prices
    samples_low_aud:  float | None = None
    samples_high_aud: float | None = None


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
    # Chat-driven additions
    parsed:        dict | None = None     # echo of the parsed query for the parse-preview UI
    partial:       bool       = False     # true if AI cross-check was skipped to honor budget
    trace:         dict | None = None     # debug-only: pipeline timings + plan summary
    clarification: str | None = None      # LLM guardrail question when query is too vague
    # Hybrid pricing (path C)
    market_reference: MarketReference | None = None


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
    Legacy ACP India + China search. Used when no chat query is supplied so
    the original landing-page button keeps working unchanged.
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
    india_clean = clean_results(india_raw, country_override="India", rates=rates, use_ai=False, category="acp")
    china_clean = clean_results(china_raw, country_override="China", rates=rates, use_ai=False, category="acp")
    all_records = india_clean + china_clean

    _log.info("[perf] search=%.1fs  clean=%.2fs  records=%d", t1 - t0, time.time() - t1, len(all_records))

    if not all_records:
        raise HTTPException(status_code=404, detail="No ACP suppliers found.")
    return all_records


def _fetch_suppliers_from_chat(parsed: dict, rates: dict, max_results: int):
    """
    Chat-driven multi-angle search. Calls multi_search_and_merge() with the
    parsed query to fan out across (country, angle) plans, then runs the
    same rule-based cleaner with the parsed category as the relevance filter.
    """
    t0 = time.time()
    try:
        merged = multi_search_and_merge(parsed, max_calls=8, per_query_results=max_results)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Supplier search unavailable: {e}")

    t1 = time.time()
    category = parsed.get("category") or "unknown"
    # Strict country filter only when the user actually specified countries.
    # Empty list / None = global search, no filter.
    allowed = list(parsed.get("countries") or []) or None
    cleaned  = clean_results(
        merged, rates=rates, use_ai=False,
        category=category, allowed_countries=allowed,
    )
    _log.info("[perf] chat search=%.1fs clean=%.2fs records=%d (category=%s, countries=%s)",
              t1 - t0, time.time() - t1, len(cleaned), category, allowed or "global")

    if not cleaned:
        raise HTTPException(status_code=404, detail="No suppliers found for this query.")
    return cleaned, len(merged), (t1 - t0, time.time() - t1)


def _to_supplier_out(
    rank: int,
    v,
    medians_by_bucket: dict | None = None,
    query_variant: str = "",
) -> SupplierOut:
    rec    = v.scored.record
    sigs   = rec.signals or {}
    angles = sigs.get("angles_matched") or []
    category = getattr(rec, "category", None) or "unknown"

    # Path C: hybrid pricing.
    # Only compute a model estimate if we have NO real extracted price.
    # When a real price exists, classify it against the per-country market
    # midpoint (for the ✓ Within / ⚠ Above / 🚨 badges).
    est_low = est_high = None
    est_src = None
    if rec.price_est is None:
        est = estimate_supplier_price(rec, category, query_variant)
        if est:
            est_low, est_high, est_src = est["low"], est["high"], est["source"]

    range_status = None
    if rec.price_est is not None:
        range_status = classify_price_vs_market(
            rec.price_est, category, rec.country, query_variant,
        )

    # Only surface a real price when we actually extracted one. The scorer
    # fills `price_used=0.0` as a neutral fallback for missing-price rows;
    # passing that through would mislead the frontend into thinking a 0.0
    # USD price was scraped.
    price_usd_out = v.price_used if (rec.price_est is not None and v.price_used > 0) else None

    return SupplierOut(
        rank=rank,
        name=rec.name,
        country=rec.country,
        url=rec.url,
        description=rec.description,
        price_usd=price_usd_out,
        price_raw=rec.price_raw,
        risk_level=v.scored.risk_level.value,
        risk_score=v.scored.risk_score,
        risk_reasons=v.scored.risk_reasons,
        value_score=round(v.value_score * 100, 1),
        base_score=round(v.value_score * 100, 1),
        anomalies=detect_anomalies(v, medians_by_bucket),
        category=category if category != "unknown" else None,
        price_unit=getattr(rec, "price_unit", None),
        price_unit_source=getattr(rec, "price_unit_source", None),
        price_original=getattr(rec, "price_original", None),
        angle_count=sigs.get("angle_count"),
        angles_matched=list(angles) if angles else None,
        bucket_key=getattr(v, "bucket_key", None),
        bucket_size=getattr(v, "bucket_size", None),
        price_estimated_low_usd=est_low,
        price_estimated_high_usd=est_high,
        price_estimate_source=est_src,
        price_range_status=range_status,
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
    unit = getattr(rec, "price_unit", "sqm") or "sqm"
    if unit == "unknown":
        unit = "unit"
    price = (
        f"{symbol}{(rec.price_est * fx):.2f}/{unit}"
        if rec.price_est is not None else "unknown"
    )
    return {
        "name": rec.name, "country": rec.country,
        "price_display": price, "price_raw": rec.price_raw,
        "risk_level": v.scored.risk_level.value,
        "value_score": round(v.value_score * 100, 1),
        "url": rec.url, "description": rec.description,
        "category": getattr(rec, "category", "acp"),
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

_RANKED_CACHE_MAX = 200
_last_ranked: OrderedDict[str, object] = OrderedDict()
_ranked_lock = threading.Lock()


def _set_ranked_cache(mapping: dict) -> None:
    with _ranked_lock:
        _last_ranked.clear()
        for k, v in mapping.items():
            _last_ranked[k] = v
            if len(_last_ranked) > _RANKED_CACHE_MAX:
                _last_ranked.popitem(last=False)


def _get_ranked_cache(name: str):
    with _ranked_lock:
        return _last_ranked.get(name)


# ---------------------------------------------------------------------------
# PRIMARY ENDPOINT — /api/analyze
#
# Pipeline: Rule Engine → AI Cross-Check → Trust Signal → Response
# ---------------------------------------------------------------------------

class ParseRequest(BaseModel):
    query: str = Field(max_length=2000)


class ParseResponse(BaseModel):
    parsed: dict


@app.post("/api/parse", response_model=ParseResponse)
def parse_only(req: ParseRequest):
    """
    Run the chat-query parser only — no Serper calls, no scoring. Used by
    the frontend to show an editable parse preview before committing to a
    paid multi-angle search.
    """
    parsed = parse_search_query(req.query or "")
    return ParseResponse(parsed=parsed)


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    t_start = time.time()

    if not os.environ.get("TAVILY_API_KEY"):
        raise HTTPException(status_code=400, detail="TAVILY_API_KEY not configured.")

    gemma_available = bool(os.environ.get("GEMMA_API_KEY"))

    rates = get_rates()

    # ─────────────────────────────────────────────────────────────────────
    # Branch: chat-driven flow vs legacy ACP India+China flow
    # ─────────────────────────────────────────────────────────────────────
    parsed_for_response: dict | None = None
    raw_url_count = 0
    chat_timings: tuple[float, float] | None = None
    clarification: str | None = None

    if req.query or req.parsed:
        # Chat path. Prefer caller-supplied parsed override (the user may have
        # edited it in the parse-preview UI); otherwise run the LLM/regex parser.
        if req.parsed:
            parsed = req.parsed
        else:
            parsed = parse_search_query(req.query)

        # LLM guardrail: when the query is too vague, return a clarification
        # question instead of running an expensive blind search.
        if parsed.get("needs_clarification") and parsed.get("clarification_question"):
            clarification = parsed["clarification_question"]
            raise HTTPException(
                status_code=422,
                detail={"clarification": clarification, "parsed": parsed},
            )

        all_records, raw_url_count, chat_timings = _fetch_suppliers_from_chat(
            parsed, rates, max_results=req.max_results
        )
        parsed_for_response = parsed
    else:
        all_records = _fetch_suppliers(req, rates)

    # --- Layer 1: RULE ENGINE (decision maker) ---
    scored = score_all(all_records)
    valued = compute_value_scores(scored)
    ranked = rank_suppliers(valued)
    top3   = get_top3(ranked)
    winner = get_winner(top3)
    narr             = generate_recommendation(winner, top3)
    median           = dataset_median(ranked)        # legacy global, used by ai_adjustment
    medians_by_bucket = dataset_medians(ranked)      # per-(category, canonical_unit) for anomaly

    # Cache ranked results for on-demand insight
    _set_ranked_cache({v.scored.record.name: v for v in ranked})

    fx     = rates.get("AUD", 1.58)
    symbol = "A$"

    # --- Layer 2: AI CROSS-CHECK (skipped if total budget already exhausted) ---
    validation = {"source": "fallback"}
    partial    = False
    elapsed    = time.time() - t_start
    if gemma_available and elapsed < ANALYZE_TOTAL_BUDGET:
        t_ai = time.time()
        winner_audit = _supplier_audit_dict(winner, symbol, fx)
        alt_audits   = [_supplier_audit_dict(v, symbol, fx) for v in top3 if v is not winner]
        validation   = cross_check(winner_audit, alt_audits)
        _log.info("[perf] cross-check=%.1fs", time.time() - t_ai)
    elif gemma_available:
        partial = True
        _log.warning(
            "[budget] cross-check SKIPPED: %.1fs already elapsed (>= %.1fs budget)",
            elapsed, ANALYZE_TOTAL_BUDGET,
        )

    # --- Layer 3: TRUST SIGNAL ---
    trust = _trust_from_validation(validation)

    # Apply cross-check adjustment to winner's score
    winner_adj = adj_from_crosscheck(validation)

    query_variant = (parsed_for_response or {}).get("variant") or ""

    all_out: list[SupplierOut] = []
    for v in ranked:
        so = _to_supplier_out(0, v, medians_by_bucket, query_variant=query_variant)
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

    # Cap the user-facing list to the top N. Ranking, medians and anomaly
    # baselines were computed over the full ranked set above; from here on
    # we just stop surfacing the long tail the user won't read anyway.
    all_out    = all_out[:DISPLAY_SUPPLIER_LIMIT]
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

    total = time.time() - t_start
    _log.info("[perf] /api/analyze  total=%.1fs  trust=%s  partial=%s", total, trust, partial)

    trace = None
    if req.debug:
        trace = {
            "total_seconds":   round(total, 2),
            "budget_seconds":  ANALYZE_TOTAL_BUDGET,
            "raw_url_count":   raw_url_count,
            "ranked_count":    len(ranked),
            "partial":         partial,
            "parsed":          parsed_for_response,
        }
        if chat_timings:
            trace["search_seconds"] = round(chat_timings[0], 2)
            trace["clean_seconds"]  = round(chat_timings[1], 2)

    # --- Market reference band ---------------------------------------
    # Convert the USD band from price_estimator into AUD for display, and
    # attach the real-price sample envelope (min/max of scraped prices in
    # this search) as the "blended" second line on the banner.
    market_ref = None
    if parsed_for_response:
        cat_for_banner = parsed_for_response.get("category") or "unknown"
        ref_usd = market_reference_for(
            cat_for_banner,
            parsed_for_response.get("countries") or [],
            query_variant,
        )
        if ref_usd:
            # Count only suppliers with a truly-scraped price. `price_usd`
            # can be 0.0 for no-price rows (value_scorer's neutral fallback),
            # which would falsely inflate the sample count.
            real_prices_aud = [
                s.price_usd * fx for s in all_out
                if s.price_usd and s.price_usd > 0
            ]
            market_ref = MarketReference(
                low_aud=round(ref_usd["low_usd"]  * fx, 2),
                high_aud=round(ref_usd["high_usd"] * fx, 2),
                unit=ref_usd["unit"],
                category=ref_usd["category"],
                country_scope=ref_usd["country_scope"],
                samples_from_search=len(real_prices_aud),
                samples_low_aud=round(min(real_prices_aud), 2) if real_prices_aud else None,
                samples_high_aud=round(max(real_prices_aud), 2) if real_prices_aud else None,
            )

    return AnalyzeResponse(
        currency="AUD", symbol=symbol, fx=fx,
        winner=winner_out, top3=top3_out, all_suppliers=all_out,
        summary=narr.summary, explanation=explanation,
        risk_note=risk_note, decision=decision, trust=trust,
        parsed=parsed_for_response, partial=partial, trace=trace,
        clarification=clarification,
        market_reference=market_ref,
    )


# ---------------------------------------------------------------------------
# ON-DEMAND INSIGHT — /api/insight
#
# Triggered by user click on a supplier. Not part of the primary flow.
# ---------------------------------------------------------------------------

class InsightRequest(BaseModel):
    name: str
    # Fallback fields — used only when the in-memory _last_ranked cache has
    # been cleared (server restart, different run) and the frontend is
    # rendering from a localStorage snapshot. Caller sends enough to let
    # us rebuild a minimal SupplierRecord so the insight still works.
    country:     str | None = None
    url:         str | None = None
    description: str | None = None
    price_usd:   float | None = None


@app.post("/api/insight", response_model=InsightResponse)
def get_insight(req: InsightRequest):
    if not os.environ.get("GEMMA_API_KEY"):
        raise HTTPException(status_code=400, detail="GEMMA_API_KEY not configured.")

    v = _get_ranked_cache(req.name)
    if v is not None:
        # Happy path — rich record (includes full raw_content from the scan)
        record = v.scored.record
    elif req.country or req.url or req.description:
        # Fallback — rebuild a stub SupplierRecord from what the client sent.
        # raw_content is empty here (wasn't cached); the AI will work with
        # less context but still produce a useful insight.
        from modules.cleaner import SupplierRecord
        record = SupplierRecord(
            name=req.name,
            country=req.country or "Unknown",
            url=req.url or "",
            description=req.description or "",
            price_raw="",
            price_est=req.price_usd,
            relevance_score=0.0,
            raw_content="",
        )
    else:
        raise HTTPException(
            status_code=404,
            detail=(f"Supplier '{req.name}' not found. Run an analysis first, "
                    f"or refresh the page to re-send supplier context."),
        )

    t0 = time.time()
    result = generate_insight(record)
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
                _log.warning("Health serper check failed: %s", e)
                result["serper"] = {"ok": False, "error": type(e).__name__}

    if check_tavily:
        from services.tavily_client import is_available, search_fallback, TavilyError
        if not is_available():
            result["tavily"] = {"ok": False, "error": "TAVILY_API_KEY not set (optional — Serper is primary)"}
        else:
            try:
                hits = search_fallback("ACP aluminium composite panel", max_results=1)
                result["tavily"] = {"ok": True, "results": len(hits)}
            except TavilyError as e:
                _log.warning("Health tavily check failed: %s", e)
                result["tavily"] = {"ok": False, "error": type(e).__name__}

    if check_gemma:
        gemma_key = os.environ.get("GEMMA_API_KEY", "")
        if not gemma_key:
            result["gemma"] = {"ok": False, "error": "GEMMA_API_KEY not set"}
        else:
            try:
                text = call_model('Return exactly this JSON: {"ping": true}')
                result["gemma"] = {"ok": True} if text else {"ok": False, "error": "Empty response"}
            except Exception as e:
                _log.warning("Health gemma check failed: %s", e)
                result["gemma"] = {"ok": False, "error": type(e).__name__}

    return result


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

# HTML + JS + CSS change often during development; force browsers to
# revalidate every response so users never see a stale UI after a deploy.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma":        "no-cache",
    "Expires":       "0",
}


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles subclass that stamps every response with no-cache headers.

    Prevents the "browser shows yesterday's CSS/JS after a deploy" class
    of bug. Override is O(1) — just mutates the outgoing headers.
    """
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        for k, v in _NO_CACHE_HEADERS.items():
            resp.headers[k] = v
        return resp


app.mount("/static", NoCacheStaticFiles(directory=FRONTEND), name="static")


def _no_cache_file(path: Path) -> FileResponse:
    return FileResponse(path, headers=_NO_CACHE_HEADERS)


@app.get("/my-suppliers")
def serve_my_suppliers_page():
    return _no_cache_file(FRONTEND / "my-suppliers.html")

@app.get("/supplier/{supplier_id}/edit")
def serve_supplier_assessment_page(supplier_id: int):
    return _no_cache_file(FRONTEND / "supplier-assessment.html")

@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    if f"/{full_path}".startswith("/api/"):
        raise HTTPException(status_code=404, detail="Not found")
    return _no_cache_file(FRONTEND / "index.html")
