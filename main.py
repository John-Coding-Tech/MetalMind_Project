"""
main.py — MetalMind FastAPI Backend

Serves the frontend and exposes the supplier comparison API.

Run:
    uvicorn main:app --reload
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from services.tavily_client import (
    TavilyError,
    search_india_suppliers,
    search_china_suppliers,
    search_suppliers,
)
from modules.cleaner        import clean_results
from modules.risk_scorer    import RiskLevel, score_all, score_to_risk_level
from modules.value_scorer   import compute_value_scores
from modules.ranker         import rank_suppliers, get_top3, get_winner
from engine.recommendation  import generate_recommendation
from engine.comparator       import evaluate_supplier
from engine.ai_engine        import ai_evaluate, call_model
from modules.currency       import get_rates
from config                 import (
    DECISION_SCORE_THRESHOLD,
    AI_RISK_ESCALATION,
    AI_COMPARE_TOP_N_DEFAULT,
    AI_ONLY_TOP_N_DEFAULT,
    AI_MAX_PARALLEL_DEFAULT,
)

load_dotenv()

app = FastAPI(title="MetalMind API")

FRONTEND = Path(__file__).parent / "frontend"

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CompareRequest(BaseModel):
    # Upper-bounded to prevent OOM / API abuse; Tavily is called twice
    # (once per country) so the real fetch count is 2 × max_results.
    max_results: int  = Field(default=8, ge=1, le=50)
    priority:    str  = "India"       # "India" | "China" | "Both Equal"


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
    # AI-Comparison mode only: "green" | "yellow" | "red" | None
    decision_tier: str | None = None


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
    decision:       str = "recommended"   # "recommended" | "not_recommended"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_suppliers(req: CompareRequest, rates: dict):
    """
    Run Tavily searches for both countries and return cleaned records.

    Raises HTTPException(503) on Tavily failures (key missing, network,
    rate-limit) so the frontend can surface a clear error instead of a
    generic 500.
    """
    try:
        india_raw = search_india_suppliers(max_results=req.max_results)
        china_raw = search_china_suppliers(max_results=req.max_results)
    except TavilyError as e:
        raise HTTPException(status_code=503, detail=f"Supplier search unavailable: {e}")

    india_clean = clean_results(india_raw, country_override="India", rates=rates)
    china_clean = clean_results(china_raw, country_override="China", rates=rates)
    all_records = india_clean + china_clean

    if not all_records:
        raise HTTPException(status_code=404, detail="No ACP suppliers found.")

    return all_records


# Cap parallel Gemma calls — too many in parallel hits rate limits and all
# 16 silently fall back to the "evaluation unavailable" stub.
_AI_MAX_PARALLEL = int(os.environ.get("AI_MAX_PARALLEL", str(AI_MAX_PARALLEL_DEFAULT)))


def _dedupe_reasons(existing: list[str], candidates: list[str]) -> list[str]:
    """
    Return candidates with entries that overlap heavily with any existing
    reason removed. Normalises to lowercase word-sets and drops a candidate
    when it shares >=2 significant (>3-char) words with any existing reason.

    Prevents duplicate-looking risk notes like expert's
    'No contact information found' + AI's 'No contact info' both listed.
    """
    def _words(s: str) -> set[str]:
        return {w for w in re.sub(r"[^\w\s]", " ", s.lower()).split() if len(w) > 3}

    existing_word_sets = [_words(e) for e in existing]
    unique: list[str] = []
    for c in candidates:
        cw = _words(c)
        if any(len(cw & ew) >= 2 for ew in existing_word_sets if ew):
            continue
        unique.append(c)
    return unique


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

    rates = get_rates()
    all_records = _fetch_suppliers(req, rates)

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

    # All prices displayed in AUD — use live rate from cache
    fx     = rates.get("AUD", 1.58)
    symbol = "A$"

    # Derive decision for the winner badge (same rule used by AI mode)
    winner_out = _to_supplier_out(1, winner)
    decision = (
        "recommended"
        if winner_out.value_score >= DECISION_SCORE_THRESHOLD * 100
        and winner_out.risk_level != RiskLevel.HIGH.value
        else "not_recommended"
    )

    return CompareResponse(
        currency="AUD",
        symbol=symbol,
        fx=fx,
        winner=winner_out,
        top3=[_to_supplier_out(i + 1, v) for i, v in enumerate(top3)],
        all_suppliers=[_to_supplier_out(i + 1, v) for i, v in enumerate(ranked)],
        summary=result.summary,
        explanation=result.explanation,
        risk_note=result.risk_note,
        decision=decision,
    )


@app.get("/api/health")
def health(check_tavily: bool = False, check_gemma: bool = False):
    """
    Smoke-test endpoint.

    Base call (no params): returns {status: ok} — just confirms the FastAPI
    process is alive.

    With ?check_tavily=true: issues a minimal Tavily query to verify the key
    works and the service is reachable. Returns {tavily: {ok, error?}}.

    With ?check_gemma=true: issues a tiny Gemma request to verify the key
    works and the service is reachable. Returns {gemma: {ok, error?}}.

    Safe to call from the browser — no analysis is performed, no results
    are persisted. Useful for the user to independently verify credentials
    before running a full analysis.
    """
    result: dict = {"status": "ok"}

    if check_tavily:
        tavily_key = os.environ.get("TAVILY_API_KEY", "")
        if not tavily_key:
            result["tavily"] = {"ok": False, "error": "TAVILY_API_KEY not set"}
        else:
            try:
                hits = search_suppliers("ACP aluminium composite panel", max_results=1)
                result["tavily"] = {"ok": True, "results": len(hits)}
            except TavilyError as e:
                result["tavily"] = {"ok": False, "error": str(e)[:300]}
            except Exception as e:
                result["tavily"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:280]}"}

    if check_gemma:
        gemma_key = os.environ.get("GEMMA_API_KEY", "")
        if not gemma_key:
            result["gemma"] = {"ok": False, "error": "GEMMA_API_KEY not set"}
        else:
            try:
                text = call_model('Return exactly this JSON: {"ping": true}')
                if text:
                    result["gemma"] = {"ok": True, "sample": text[:120]}
                else:
                    result["gemma"] = {"ok": False, "error": "Empty response after retries"}
            except Exception as e:
                result["gemma"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:280]}"}

    return result


# ---------------------------------------------------------------------------
# Phase 2 — Expert vs AI comparison  (returns CompareResponse shape +
# per-supplier decision_tier so the frontend reuses the Run Calculation UI)
# ---------------------------------------------------------------------------

_AI_TOP_N           = int(os.environ.get("AI_COMPARE_TOP_N", str(AI_COMPARE_TOP_N_DEFAULT)))
_AI_RISK_ESCALATION = AI_RISK_ESCALATION


def _decision_tier(expert: dict, ai: dict, comparison: dict) -> str:
    """
    Reduce expert + ai + comparison to one of three tiers.

    When AI is unavailable (source == "fallback"), defer to expert: a good
    expert verdict should not be penalized just because the AI couldn't
    respond. This avoids burying good suppliers on transient API failures.
    """
    if ai.get("source") == "fallback":
        return "green" if expert.get("decision") == "recommended" else "yellow"

    if ai.get("decision") == "not_recommended":
        return "red"
    both_ok      = expert.get("decision") == "recommended" and ai.get("decision") == "recommended"
    escalated    = (float(ai.get("risk_score") or 0) - float(expert.get("risk_score") or 0)) > _AI_RISK_ESCALATION
    extra_risks  = bool(comparison.get("ai_extra_risks"))
    if both_ok and not escalated and not extra_risks:
        return "green"
    return "yellow"


def _ai_explanation(tier: str, winner_name: str, ai_data: dict) -> str:
    """Short paragraph for the winner-explanation slot, keyed to the tier."""
    if ai_data.get("source") == "fallback":
        return (
            f"AI evaluation was unavailable for this run — the recommendation for "
            f"{winner_name} reflects rule-based scoring only. Retry when the AI "
            f"service is reachable to get a cross-checked verdict."
        )
    if tier == "green":
        return (
            f"This recommendation is validated by both rule-based scoring and AI analysis. "
            f"{winner_name} is a safe, high-value choice."
        )
    if tier == "yellow":
        first_flag = (ai_data.get("risk_flags") or [""])[0]
        tail = f" Key concern: {first_flag}." if first_flag else ""
        return (
            f"AI detected additional risks not captured by rule-based scoring.{tail} "
            f"Review the risk details before proceeding with {winner_name}."
        )
    return (
        f"AI analysis rejects this supplier. Consider the next ranked option "
        f"or refine the search before committing."
    )


@app.post("/api/compare-with-ai", response_model=CompareResponse)
def compare_with_ai(req: CompareRequest):
    """
    Run the full expert pipeline + independent AI evaluation on the top-N
    ranked suppliers, then return the SAME response shape as /api/compare
    so the frontend reuses the Run Calculation UI end-to-end. Each
    top-N SupplierOut carries a decision_tier ("green"/"yellow"/"red").
    """
    if not os.environ.get("TAVILY_API_KEY"):
        raise HTTPException(status_code=400, detail="TAVILY_API_KEY not configured.")
    if not os.environ.get("GEMMA_API_KEY"):
        raise HTTPException(status_code=400, detail="GEMMA_API_KEY not configured.")

    rates       = get_rates()
    all_records = _fetch_suppliers(req, rates)

    scored = score_all(all_records)
    valued = compute_value_scores(scored)
    ranked = rank_suppliers(valued, priority=req.priority)
    top3   = get_top3(ranked)
    winner = get_winner(top3)

    # Expert narrative (reused for Risk Details pre content)
    narr = generate_recommendation(winner, top3)

    # --- AI evaluation on Top-N (parallel Gemma calls, capped) ---
    top_slice = ranked[:_AI_TOP_N]
    workers   = min(_AI_MAX_PARALLEL, max(1, len(top_slice)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        bundles = list(ex.map(evaluate_supplier, top_slice))

    # Map each ValuedSupplier (by name) → tier and AI data
    tier_by_name: dict[str, str]    = {}
    ai_by_name:   dict[str, dict]   = {}
    for v, bundle in zip(top_slice, bundles):
        nm            = v.scored.record.name
        tier_by_name[nm] = _decision_tier(bundle["expert"], bundle["ai"], bundle["comparison"])
        ai_by_name[nm]   = bundle["ai"]

    def _out_with_tier(i: int, v) -> SupplierOut:
        so = _to_supplier_out(i, v)
        so.decision_tier = tier_by_name.get(v.scored.record.name)

        # Merge AI reasoning into per-supplier risk_reasons so every row
        # (not just the winner) can surface the AI's rationale in its
        # "Risk Details" panel. Applies only to suppliers that were
        # AI-evaluated (top-N) and whose AI call actually succeeded.
        ai_data = ai_by_name.get(v.scored.record.name)
        if ai_data and ai_data.get("source") == "ai":
            expert_reasons = list(so.risk_reasons or [])
            extra_reasons  = _dedupe_reasons(expert_reasons, list(ai_data.get("reasons") or []))
            extra_flags    = _dedupe_reasons(expert_reasons, list(ai_data.get("risk_flags") or []))
            merged = list(expert_reasons)
            merged += [f"AI: {r}" for r in extra_reasons]
            merged += [f"⚠ {f}" for f in extra_flags]
            if merged:
                so.risk_reasons = merged
        return so

    winner_out = _out_with_tier(1, winner)
    top3_out   = [_out_with_tier(i + 1, v) for i, v in enumerate(top3)]
    all_out    = [_out_with_tier(i + 1, v) for i, v in enumerate(ranked)]

    # Winner-level decision + explanation
    winner_tier = winner_out.decision_tier or "green"
    winner_ai   = ai_by_name.get(winner.scored.record.name, {})
    explanation = _ai_explanation(winner_tier, winner_out.name, winner_ai)

    # Collapsible Risk Details — keep expert + AI reasoning side by side,
    # dropping AI reasons/flags that effectively duplicate expert signals.
    expert_reasons = list(winner_out.risk_reasons or [])
    risk_note_lines = ["=== Expert signals ==="]
    risk_note_lines += [f"• {r}" for r in (expert_reasons or ["None"])]
    if winner_ai:
        ai_reasons = _dedupe_reasons(expert_reasons, list(winner_ai.get("reasons") or []))
        ai_flags   = _dedupe_reasons(expert_reasons, list(winner_ai.get("risk_flags") or []))
        if ai_reasons:
            risk_note_lines.append("")
            risk_note_lines.append("=== AI reasoning ===")
            risk_note_lines += [f"• {r}" for r in ai_reasons]
        if ai_flags:
            risk_note_lines.append("")
            risk_note_lines.append("=== AI risk flags ===")
            risk_note_lines += [f"⚠ {f}" for f in ai_flags]
    risk_note = "\n".join(risk_note_lines)

    # Top-level decision: driven by tier (green=rec, yellow=rec, red=not_rec)
    decision = "not_recommended" if winner_tier == "red" else "recommended"

    fx     = rates.get("AUD", 1.58)
    symbol = "A$"

    return CompareResponse(
        currency="AUD",
        symbol=symbol,
        fx=fx,
        winner=winner_out,
        top3=top3_out,
        all_suppliers=all_out,
        summary=narr.summary,
        explanation=explanation,
        risk_note=risk_note,
        decision=decision,
    )


# ---------------------------------------------------------------------------
# Phase 2.5 — AI-Only Analysis
# ---------------------------------------------------------------------------

_AI_ONLY_TOP_N = int(os.environ.get("AI_ONLY_TOP_N", str(AI_ONLY_TOP_N_DEFAULT)))


def _ai_risk_level(risk_score: float) -> str:
    """Map AI's 0-1 risk_score to Low/Medium/High using the SHARED cutoffs."""
    return score_to_risk_level(risk_score).value


def _ai_to_supplier_out(rank: int, record, ai: dict) -> SupplierOut:
    """Map an AI evaluation to the shared SupplierOut used by the UI."""
    reasons   = list(ai.get("reasons") or [])
    flags     = [f"⚠ {f}" for f in (ai.get("risk_flags") or [])]
    risk_s    = float(ai.get("risk_score") or 0.5)
    score_pct = round(float(ai.get("score") or 0.0) * 100, 1)

    return SupplierOut(
        rank=rank,
        name=record.name,
        country=record.country,
        url=record.url,
        description=record.description,
        price_usd=record.price_est,
        price_raw=record.price_raw,
        risk_level=_ai_risk_level(risk_s),
        risk_score=round(risk_s, 3),
        risk_reasons=reasons + flags,
        value_score=score_pct,
    )


@app.post("/api/ai-only", response_model=CompareResponse)
def ai_only(req: CompareRequest):
    """
    AI-only evaluation returned in the SAME shape as /api/compare so the
    frontend can reuse renderResults() without any branching.

    Pipeline: Tavily → clean → select Top-N by Tavily relevance_score
              → ai_evaluate() in parallel → map to SupplierOut.
    """
    if not os.environ.get("TAVILY_API_KEY"):
        raise HTTPException(status_code=400, detail="TAVILY_API_KEY not configured.")
    if not os.environ.get("GEMMA_API_KEY"):
        raise HTTPException(status_code=400, detail="GEMMA_API_KEY not configured.")

    rates       = get_rates()
    all_records = _fetch_suppliers(req, rates)

    # Expert pre-filter — drop HIGH-risk suppliers before AI evaluation so
    # the AI can't accidentally recommend a supplier the rule-based system
    # has already flagged as too risky. Keeps the "Risk > Price" principle
    # intact even in the AI-only mode.
    scored_all  = score_all(all_records)
    safe_scored = [s for s in scored_all if s.risk_level != RiskLevel.HIGH]

    # If the filter leaves too few candidates, relax to keep the mode usable
    # (but still note this in the response via the decision message).
    if not safe_scored:
        safe_scored = scored_all   # fall back — surface best of a bad set

    filtered_records = [s.record for s in safe_scored]
    sorted_by_rel    = sorted(filtered_records, key=lambda r: r.relevance_score, reverse=True)
    top_slice        = sorted_by_rel[:_AI_ONLY_TOP_N]

    # Parallel AI calls — capped to avoid rate-limit-induced fallback cascade
    workers = min(_AI_MAX_PARALLEL, max(1, len(top_slice)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        ai_results = list(ex.map(ai_evaluate, top_slice))

    # If EVERY AI call fell back, the AI service is effectively unreachable
    # for this request — surface a clear 503 instead of ranking fallbacks.
    if ai_results and all(a.get("source") == "fallback" for a in ai_results):
        raise HTTPException(
            status_code=503,
            detail="AI service unavailable for all suppliers. Check GEMMA_API_KEY and network, then retry.",
        )

    # Pair records with AI results, rank by AI score descending
    paired = sorted(
        zip(top_slice, ai_results),
        key=lambda pair: float(pair[1].get("score") or 0),
        reverse=True,
    )

    supplier_outs = [
        _ai_to_supplier_out(i + 1, rec, ai)
        for i, (rec, ai) in enumerate(paired)
    ]

    winner       = supplier_outs[0]
    top3         = supplier_outs[:3]
    winner_rec, winner_ai = paired[0]

    # Short explanation (main card) — do NOT include long AI prose here
    first_reason = (winner_ai.get("reasons") or ["No reason provided."])[0]
    explanation  = (
        f"AI rated {winner.name} highest at {winner.value_score}/100 "
        f"with {winner.risk_level.lower()} risk. {first_reason}"
    )

    # Full AI reasoning — goes into the collapsible "Risk Details" section
    lines = ["=== AI Reasoning ==="]
    for r in (winner_ai.get("reasons") or []):
        lines.append(f"• {r}")
    if winner_ai.get("risk_flags"):
        lines.append("")
        lines.append("=== Risk Flags ===")
        for f in winner_ai.get("risk_flags") or []:
            lines.append(f"⚠ {f}")
    risk_note = "\n".join(lines)

    decision = winner_ai.get("decision") or "not_recommended"

    fx     = rates.get("AUD", 1.58)
    symbol = "A$"

    return CompareResponse(
        currency="AUD",
        symbol=symbol,
        fx=fx,
        winner=winner,
        top3=top3,
        all_suppliers=supplier_outs,
        summary="AI-only evaluation",
        explanation=explanation,
        risk_note=risk_note,
        decision=decision,
    )


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    return FileResponse(FRONTEND / "index.html")
