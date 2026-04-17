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
    suppliers = db.query(SavedSupplier).all()
    if not suppliers:
        return {"answer": "You haven't saved any suppliers yet. Run an analysis and save some suppliers first.", "results": []}

    supplier_data = "\n".join([
        f"- {s.supplier_name} | {s.country} | Price: {s.price_display or 'unknown'} | "
        f"Risk: {s.risk_level} (score: {s.risk_score}) | Value: {s.value_score}/100 | "
        f"Trust: {s.trust or 'N/A'} | URL: {s.url} | "
        f"Risk reasons: {', '.join(s.risk_reasons) if s.risk_reasons else 'none'} | "
        f"Notes: {s.notes or 'none'}"
        for s in suppliers
    ])

    prompt = f"""You are a supplier intelligence assistant. The user has saved these ACP suppliers:

{supplier_data}

User question: {req.query}

You MUST reply with ONLY a valid JSON object (no markdown, no extra text). Use this exact format:

{{
  "recommendation": {{
    "supplier_name": "name of top recommended supplier",
    "country": "country",
    "reasons": ["reason 1", "reason 2", "reason 3"],
    "tradeoffs": ["tradeoff 1"]
  }},
  "summary": "2-3 sentence answer to the user's question, citing specific data",
  "highlights": ["supplier_name_1", "supplier_name_2"]
}}

Rules:
- "recommendation" is the single best supplier for the user's question. If no clear winner, pick the best overall.
- "reasons" should cite specific data (price, risk score, value score).
- "tradeoffs" are honest downsides of the recommendation.
- "summary" answers the question directly with data.
- "highlights" lists supplier names that are relevant to the answer (max 3).
- Reply in the same language the user used for "summary", "reasons", and "tradeoffs".
- Output ONLY the JSON object. No markdown code blocks, no explanation."""

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
