"""
routes/suppliers.py — Save, list, and AI-search saved suppliers.
"""

import json
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from models import SavedSupplier
from engine.ai_engine import call_model
from services.tavily_client import enrich_url, is_available as tavily_available
from services.serper_client import search as serper_search, SerperError

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["suppliers"])


class SaveSupplierRequest(BaseModel):
    supplier_name: str
    country: str | None = None
    price_display: str | None = None
    price_usd: float | None = None
    risk_level: str | None = None
    risk_score: float | None = None
    risk_reasons: list[str] | None = None
    value_score: float | None = None
    url: str | None = None
    description: str | None = None
    trust: str | None = None
    anomalies: dict | None = None
    ai_adjustment: dict | None = None


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class AiSearchRequest(BaseModel):
    query: str
    selected_ids: list[int] | None = None
    history: list[ChatTurn] | None = None


class UpdateNoteRequest(BaseModel):
    notes: str


@router.post("/save-supplier")
def save_supplier(req: SaveSupplierRequest, db: Session = Depends(get_db)):
    existing = db.query(SavedSupplier).filter(
        SavedSupplier.supplier_name == req.supplier_name,
        SavedSupplier.url == req.url,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Supplier already saved")

    supplier = SavedSupplier(
        supplier_name=req.supplier_name,
        country=req.country,
        price_display=req.price_display,
        price_usd=req.price_usd,
        risk_level=req.risk_level,
        risk_score=req.risk_score,
        risk_reasons=req.risk_reasons,
        value_score=req.value_score,
        url=req.url,
        description=req.description,
        trust=req.trust,
        anomalies=req.anomalies,
        ai_adjustment=req.ai_adjustment,
    )
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return _to_dict(supplier)


@router.get("/saved-suppliers")
def list_saved(db: Session = Depends(get_db)):
    suppliers = db.query(SavedSupplier).order_by(SavedSupplier.saved_at.desc()).all()
    return [_to_dict(s) for s in suppliers]


@router.delete("/saved-supplier/{supplier_id}")
def delete_saved(supplier_id: int, db: Session = Depends(get_db)):
    supplier = db.query(SavedSupplier).filter(SavedSupplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    db.delete(supplier)
    db.commit()
    return {"ok": True}


@router.post("/supplier-report/{supplier_id}")
def supplier_report(supplier_id: int, refresh: bool = False, db: Session = Depends(get_db)):
    """Generate a deep-dive report for one supplier using web search + AI.
    Uses cached report unless refresh=true."""
    s = db.query(SavedSupplier).filter(SavedSupplier.id == supplier_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Supplier not found")

    # Return cached report if available and not forcing refresh
    if not refresh and s.deep_report:
        cached = dict(s.deep_report)
        cached["cached"] = True
        cached["generated_at"] = s.report_generated_at.isoformat() if s.report_generated_at else None
        return cached

    # Gather web search results on multiple dimensions
    searches = {
        "overview":      f"{s.supplier_name} ACP aluminium composite panel company history background",
        "capacity":      f"{s.supplier_name} production capacity factory size employees",
        "certifications": f"{s.supplier_name} ISO certification quality standards",
        "reputation":    f"{s.supplier_name} customer reviews reputation feedback",
        "news":          f"{s.supplier_name} news 2025 2026",
    }
    search_context = {}
    for topic, q in searches.items():
        try:
            hits = serper_search(q, max_results=5)
            if hits:
                search_context[topic] = "\n".join([
                    f"- {h.get('title', '')}: {h.get('content', '')[:300]}"
                    for h in hits[:5]
                ])
        except Exception as e:
            _log.warning("Report search [%s] failed for %s: %s", topic, s.supplier_name, e)

    # Also enrich the supplier's website for direct facts
    website_content = ""
    if s.url and tavily_available():
        try:
            result = enrich_url(s.url)
            if result and result.get("content"):
                website_content = result["content"][:6000]
        except Exception as e:
            _log.warning("Website enrichment failed for %s: %s", s.supplier_name, e)

    if not search_context and not website_content:
        return {
            "supplier_name": s.supplier_name,
            "sections": {},
            "summary": "Unable to gather enough information for a deep-dive report. Web search and website fetching both failed.",
            "confidence": 0.0,
        }

    # Build the prompt
    search_block = "\n\n".join([
        f"=== {topic.upper()} SEARCH ===\n{content}"
        for topic, content in search_context.items()
    ])
    website_block = f"\n\n=== WEBSITE CONTENT ({s.url}) ===\n{website_content}" if website_content else ""

    saved_profile = (
        f"Name: {s.supplier_name}\n"
        f"Country: {s.country}\n"
        f"Price: {s.price_display}\n"
        f"Risk: {s.risk_level} (score: {s.risk_score})\n"
        f"Value Score: {s.value_score}/100\n"
        f"Trust: {s.trust}\n"
        f"URL: {s.url}\n"
        f"Risk reasons: {', '.join(s.risk_reasons) if s.risk_reasons else 'none'}\n"
        f"Notes: {s.notes or 'none'}"
    )

    prompt = f"""You are a supplier due diligence analyst. Generate a comprehensive deep-dive report.

SAVED PROFILE:
{saved_profile}

{search_block}{website_block}

Produce a JSON object ONLY (no markdown) with this exact shape:

{{
  "supplier_name": "{s.supplier_name}",
  "sections": {{
    "overview": "2-3 sentences about company history, founding, and main products. Cite specific facts from sources.",
    "capacity": "Production capacity, factory size, employee count if known. Say 'not found' if unavailable.",
    "certifications": "Specific certifications (ISO, etc.) with years if known.",
    "reputation": "Customer reviews and reputation signals. Be specific about sources.",
    "recent_news": "Any recent developments, contracts, expansions, issues (2024-2026).",
    "risk_signals": "Red flags, concerns, or notable risks for a buyer."
  }},
  "summary": "Executive summary: 3-5 sentences giving an actionable verdict for a buyer considering this supplier.",
  "confidence": 0.0-1.0
}}

Rules:
- Be specific — cite facts from the search results / website content. Never invent.
- If a section has no info, write "Not found in available sources."
- Reply in English unless the supplier name suggests another locale.
- Output ONLY the JSON object. No markdown, no prose."""

    try:
        raw = call_model(prompt)
        if not raw:
            return {"supplier_name": s.supplier_name, "error": "AI model returned empty response"}
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        report = json.loads(cleaned)
        # Cache in DB
        from datetime import datetime, timezone
        s.deep_report = report
        s.report_generated_at = datetime.now(timezone.utc)
        db.commit()
        report["cached"] = False
        report["generated_at"] = s.report_generated_at.isoformat()
        return report
    except json.JSONDecodeError:
        return {"supplier_name": s.supplier_name, "error": "Could not parse AI response", "raw": raw[:1000]}
    except Exception as e:
        _log.error("Report generation failed: %s", e)
        return {"supplier_name": s.supplier_name, "error": f"Report failed: {e}"}


@router.patch("/saved-supplier/{supplier_id}/notes")
def update_notes(supplier_id: int, req: UpdateNoteRequest, db: Session = Depends(get_db)):
    supplier = db.query(SavedSupplier).filter(SavedSupplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    supplier.notes = req.notes
    db.commit()
    db.refresh(supplier)
    return _to_dict(supplier)


@router.post("/ai-search")
def ai_search(req: AiSearchRequest, db: Session = Depends(get_db)):
    all_suppliers = db.query(SavedSupplier).all()
    if not all_suppliers:
        return {"answer": "You haven't saved any suppliers yet. Run an analysis and save some suppliers first.", "results": []}

    if req.selected_ids:
        suppliers = [s for s in all_suppliers if s.id in req.selected_ids]
        scope_note = f"The user has SELECTED {len(suppliers)} specific suppliers for comparison. Focus ONLY on these selected suppliers in your answer.\n\n"
    else:
        suppliers = all_suppliers
        scope_note = ""

    if not suppliers:
        return {"answer": "No suppliers match the selection.", "results": []}

    # Detect "info-seeking" queries that benefit from fetching the actual website
    info_keywords = ["contact", "email", "phone", "address", "联系", "电话", "邮箱", "地址",
                     "reach", "call", "location", "where", "whatsapp", "微信"]
    # Queries that benefit from live Google search (data not in our DB)
    web_search_keywords = ["moq", "minimum order", "最小起订", "lead time", "delivery time", "交期", "发货",
                           "certification", "certified", "iso", "认证", "证书",
                           "warranty", "保修", "guarantee",
                           "capacity", "产能", "production",
                           "review", "reputation", "feedback", "评价", "口碑",
                           "year", "history", "成立", "founded", "established"]
    q_lower = req.query.lower()
    needs_enrichment = any(k in q_lower for k in info_keywords)
    needs_web_search = any(k in q_lower for k in web_search_keywords)

    enriched_data = {}
    if needs_enrichment and tavily_available():
        # Pick targets to enrich: all if ≤3, else top 3 by value_score
        if len(suppliers) <= 3:
            targets = suppliers
        else:
            targets = sorted(suppliers, key=lambda s: s.value_score or 0, reverse=True)[:3]
        for s in targets:
            if not s.url:
                continue
            try:
                result = enrich_url(s.url)
                if result and result.get("content"):
                    enriched_data[s.supplier_name] = result["content"][:4000]
            except Exception as e:
                _log.warning("Enrichment failed for %s: %s", s.supplier_name, e)

    # Live web search for info not in our DB (MOQ, certs, reviews, etc.)
    web_results = {}
    if needs_web_search:
        if len(suppliers) <= 3:
            web_targets = suppliers
        else:
            web_targets = sorted(suppliers, key=lambda s: s.value_score or 0, reverse=True)[:3]
        for s in web_targets:
            try:
                q = f"{s.supplier_name} {req.query}"
                hits = serper_search(q, max_results=5)
                if hits:
                    web_results[s.supplier_name] = "\n".join([
                        f"- {h.get('title', '')}: {h.get('content', '')[:300]}"
                        for h in hits[:5]
                    ])
            except (SerperError, Exception) as e:
                _log.warning("Web search failed for %s: %s", s.supplier_name, e)

    supplier_data = "\n".join([
        f"- {s.supplier_name} | {s.country} | Price: {s.price_display or 'unknown'} | "
        f"Risk: {s.risk_level} (score: {s.risk_score}) | Value: {s.value_score}/100 | "
        f"Trust: {s.trust or 'N/A'} | URL: {s.url} | "
        f"Risk reasons: {', '.join(s.risk_reasons) if s.risk_reasons else 'none'} | "
        f"Notes: {s.notes or 'none'}"
        for s in suppliers
    ])

    enrichment_block = ""
    enrichment_status = ""
    if enriched_data:
        parts = [f"\n\n=== WEBSITE CONTENT for {name} ===\n{content}" for name, content in enriched_data.items()]
        enrichment_block = "\n\nTo help answer info questions (contact, address, etc.), here is the actual website content for relevant suppliers:" + "".join(parts)
    elif needs_enrichment:
        if not tavily_available():
            enrichment_status = "\n\nNOTE: Website content fetching is not configured. You CANNOT claim info is 'not on the website' — you never checked."

    web_block = ""
    if web_results:
        parts = [f"\n\n=== GOOGLE SEARCH RESULTS for '{name} + query' ===\n{content}" for name, content in web_results.items()]
        web_block = "\n\nAdditional context from live web searches:" + "".join(parts)

    history_block = ""
    if req.history:
        turns = []
        for t in req.history[-6:]:  # last 6 turns only
            role = "User" if t.role == "user" else "Assistant"
            turns.append(f"{role}: {t.content}")
        history_block = "\n\nCONVERSATION HISTORY (for context — the user may refer to earlier answers):\n" + "\n".join(turns)

    prompt = f"""You are a supplier intelligence assistant. {scope_note}The user has saved these ACP suppliers:

{supplier_data}{enrichment_block}{web_block}{enrichment_status}{history_block}

User question: {req.query}

First, classify the question:
- "recommendation" — user is asking which is best / to compare / to choose
- "info" — user is asking for specific information (contact, URL, price, address)
- "mixed" — user is asking BOTH: recommend AND provide info (e.g. "which is best, and give me their contact")

You MUST reply with ONLY a valid JSON object (no markdown). Choose ONE format:

FORMAT A — for "recommendation" questions:
{{
  "type": "recommendation",
  "recommendation": {{
    "supplier_name": "name of top recommended supplier",
    "country": "country",
    "reasons": ["reason 1", "reason 2", "reason 3"],
    "tradeoffs": ["tradeoff 1"]
  }},
  "summary": "2-3 sentence answer citing specific data",
  "highlights": ["supplier_name_1", "supplier_name_2"]
}}

FORMAT B — for pure "info" questions:
{{
  "type": "info",
  "answer": "PLAIN STRING ONLY. Direct answer as readable text with line breaks. List facts per supplier. Extract contact info from WEBSITE CONTENT if provided.",
  "highlights": ["supplier_name_1", "supplier_name_2"]
}}

FORMAT C — for "mixed" questions (recommendation + info):
{{
  "type": "mixed",
  "recommendation": {{
    "supplier_name": "name of top recommended supplier",
    "country": "country",
    "reasons": ["reason 1", "reason 2"],
    "tradeoffs": ["tradeoff 1"]
  }},
  "info": "PLAIN STRING ONLY. The requested information formatted as readable text with line breaks. Extract from WEBSITE CONTENT if provided.",
  "summary": "Short sentence tying it together",
  "highlights": ["supplier_name_1", "supplier_name_2"]
}}

Rules:
- Use FORMAT C when the question has BOTH a choice/comparison AND an info request.
- If WEBSITE CONTENT is provided above, extract specific facts (email, phone, address) from it.
- If GOOGLE SEARCH RESULTS are provided above, use them to answer questions about MOQ, certifications, reviews, company history, etc. Cite the source.
- If info is missing, say so honestly.
- If CONVERSATION HISTORY is provided, understand references like "that supplier", "the first one", "them" based on earlier messages.
- CRITICAL: If the user asks for info about a SPECIFIC supplier (e.g. "the lowest risk one", "the best one", "Alstrong's contact"), provide info ONLY for that specific supplier — NOT all of them. Do NOT include lists of all suppliers when the user only asked about one.
- If the user says "all suppliers" or "every supplier", then list all of them.
- CRITICAL: Before answering a ranking question (lowest/highest/best X), scan ALL suppliers and identify ties. If multiple suppliers tie on the primary criterion (e.g. 3 suppliers all have risk=0), you MUST acknowledge the tie AND break it using secondary criteria — prefer higher value_score, then lower price. Never arbitrarily pick a tied supplier with a worse value_score.
- Double-check numeric comparisons: a supplier with value 40 is NOT better than one with value 90 when all else is equal.
- Reply in the same language the user used.
- Output ONLY the JSON object. No markdown, no extra text."""

    try:
        raw = call_model(prompt)
        if not raw:
            return {"answer": "AI is currently unavailable.", "structured": None, "results": [_to_dict(s) for s in suppliers]}

        import json
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        structured = json.loads(cleaned)
        return {
            "answer": structured.get("summary", raw),
            "structured": structured,
            "results": [_to_dict(s) for s in suppliers],
        }
    except (json.JSONDecodeError, Exception) as e:
        _log.warning("AI search JSON parse failed, returning raw: %s", e)
        return {"answer": raw if raw else "AI search failed.", "structured": None, "results": [_to_dict(s) for s in suppliers]}


def _to_dict(s: SavedSupplier) -> dict:
    return {
        "id": s.id,
        "supplier_name": s.supplier_name,
        "country": s.country,
        "price_display": s.price_display,
        "price_usd": s.price_usd,
        "risk_level": s.risk_level,
        "risk_score": s.risk_score,
        "risk_reasons": s.risk_reasons,
        "value_score": s.value_score,
        "url": s.url,
        "description": s.description,
        "trust": s.trust,
        "anomalies": s.anomalies,
        "ai_adjustment": s.ai_adjustment,
        "notes": s.notes,
        "saved_at": s.saved_at.isoformat() if s.saved_at else None,
    }
