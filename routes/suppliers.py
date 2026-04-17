"""
routes/suppliers.py — Save, list, and AI-search saved suppliers.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from models import SavedSupplier
from engine.ai_engine import call_model
from services.tavily_client import enrich_url, is_available as tavily_available

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


class AiSearchRequest(BaseModel):
    query: str
    selected_ids: list[int] | None = None


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
    # (contact info, email, phone, address, etc.) — enrich only when scope is small.
    info_keywords = ["contact", "email", "phone", "address", "联系", "电话", "邮箱", "地址",
                     "reach", "call", "location", "where", "whatsapp", "微信"]
    q_lower = req.query.lower()
    needs_enrichment = any(k in q_lower for k in info_keywords)

    enriched_data = {}
    if needs_enrichment and tavily_available() and len(suppliers) <= 3:
        for s in suppliers:
            if not s.url:
                continue
            try:
                result = enrich_url(s.url)
                if result and result.get("content"):
                    enriched_data[s.supplier_name] = result["content"][:4000]
            except Exception as e:
                _log.warning("Enrichment failed for %s: %s", s.supplier_name, e)

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
        elif len(suppliers) > 3:
            enrichment_status = f"\n\nNOTE: Website content was NOT fetched because the scope is too broad ({len(suppliers)} suppliers, limit 3). Tell the user to select 1-3 specific suppliers (via checkboxes) to get contact info scraped from their websites. DO NOT claim info is 'not found on website' — you never checked."

    prompt = f"""You are a supplier intelligence assistant. {scope_note}The user has saved these ACP suppliers:

{supplier_data}{enrichment_block}{enrichment_status}

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
  "answer": "Direct answer. List facts per supplier. Extract contact info from WEBSITE CONTENT if provided.",
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
  "info": "The requested information (contact, address, etc.) for each relevant supplier. Extract from WEBSITE CONTENT if provided.",
  "summary": "Short sentence tying it together",
  "highlights": ["supplier_name_1", "supplier_name_2"]
}}

Rules:
- Use FORMAT C when the question has BOTH a choice/comparison AND an info request.
- If WEBSITE CONTENT is provided above, extract specific facts (email, phone, address) from it.
- If info is missing, say so honestly.
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
