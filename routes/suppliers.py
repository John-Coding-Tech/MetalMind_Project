"""
routes/suppliers.py — Save, list, and AI-search saved suppliers.
"""

import hashlib
import json
import logging
import mimetypes
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from models import SavedSupplier, SupplierAttachment
from engine.ai_engine import call_model
from services.tavily_client import enrich_url, is_available as tavily_available
from services.serper_client import search as serper_search, SerperError


# Filesystem location for uploaded files. Resolved relative to project root
# so it works identically in dev and on Railway. Created lazily on first upload.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_UPLOAD_ROOT  = _PROJECT_ROOT / "uploads"

# Filename sanitiser — keep original extension, strip path traversal and
# control characters. Uniqueness is handled by prefixing with attachment id.
_SAFE_FILENAME = re.compile(r"[^\w.\-]+", re.UNICODE)


def _sanitize_filename(name: str) -> str:
    name = Path(name).name   # strip any path components
    name = _SAFE_FILENAME.sub("_", name).strip("._")
    return name or "file"

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


class AssessmentUpdateRequest(BaseModel):
    # Tier 1
    decision_stage: str | None = None
    rating: int | None = None
    tags: list[str] | None = None
    pros: list[str] | None = None
    cons: list[str] | None = None
    # Tier 2
    quoted_price: float | None = None
    quoted_currency: str | None = None
    quoted_unit: str | None = None
    moq: int | None = None
    lead_time_days: int | None = None
    payment_terms: str | None = None
    incoterms: str | None = None
    # Tier 3
    sample_status: str | None = None
    sample_quality: int | None = None
    factory_verified_via: list[str] | None = None
    coating_confirmed: str | None = None
    core_material_confirmed: str | None = None
    fire_rating_confirmed: str | None = None
    warranty_years: int | None = None
    next_action_date: str | None = None  # ISO "YYYY-MM-DD"
    # Free-form notes — shared with existing endpoint
    notes: str | None = None


@router.post("/save-supplier")
def save_supplier(req: SaveSupplierRequest, db: Session = Depends(get_db)):
    existing = db.query(SavedSupplier).filter(
        SavedSupplier.supplier_name == req.supplier_name,
        SavedSupplier.url == req.url,
    ).first()

    if existing:
        if existing.is_saved:
            raise HTTPException(status_code=409, detail="Supplier already saved")
        # Soft-deleted row: reactivate it, refresh the system-scored fields
        # with the latest run, but KEEP user-entered Details (rating, pros,
        # cons, quoted_price, moq, payment_terms, sample_status, etc.)
        existing.is_saved     = True
        existing.country      = req.country
        existing.price_display = req.price_display
        existing.price_usd    = req.price_usd
        existing.risk_level   = req.risk_level
        existing.risk_score   = req.risk_score
        existing.risk_reasons = req.risk_reasons
        existing.value_score  = req.value_score
        existing.description  = req.description
        existing.trust        = req.trust
        existing.anomalies    = req.anomalies
        existing.ai_adjustment = req.ai_adjustment
        db.commit()
        db.refresh(existing)
        return _to_dict(existing)

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
    # Only return currently-saved rows; soft-deleted rows stay in the DB
    # so their user-filled Details survive re-save.
    suppliers = (db.query(SavedSupplier)
                   .filter(SavedSupplier.is_saved == True)   # noqa: E712
                   .order_by(SavedSupplier.saved_at.desc())
                   .all())
    return [_to_dict(s) for s in suppliers]


@router.delete("/saved-supplier/{supplier_id}")
def delete_saved(supplier_id: int, db: Session = Depends(get_db)):
    """Soft-delete: flip is_saved to False. The row and its attachments
    stay on disk/DB so a subsequent re-save restores the user's Details."""
    supplier = db.query(SavedSupplier).filter(SavedSupplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    supplier.is_saved = False
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


@router.patch("/saved-supplier/{supplier_id}/assessment")
def update_assessment(supplier_id: int, req: AssessmentUpdateRequest, db: Session = Depends(get_db)):
    """Update any subset of the user's primary-decision-data fields.
    Auto-save friendly: only fields present in the request body are touched."""
    supplier = db.query(SavedSupplier).filter(SavedSupplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    data = req.model_dump(exclude_unset=True)
    if "next_action_date" in data and data["next_action_date"]:
        from datetime import date as _date
        try:
            data["next_action_date"] = _date.fromisoformat(data["next_action_date"])
        except ValueError:
            raise HTTPException(status_code=400, detail="next_action_date must be YYYY-MM-DD")
    for k, v in data.items():
        setattr(supplier, k, v)
    db.commit()
    db.refresh(supplier)
    return _to_dict(supplier)


# =========================================================================
# Ask-AI — Step 2: data feeding + smart trigger
#
# The prompt exposes each supplier in up to four data layers, ordered by
# authority. User-entered data is labelled VERIFIED (not "primary") because
# LLMs weight that word as ground truth more reliably.
#
#     [VERIFIED]     user-filled Tier 1/2/3 fields         — absolute authority
#     [SECONDARY-A]  rule-based scores + anomalies         — deterministic
#     [SECONDARY-B]  AI-derived adjustments (optional)     — lowest authority
#     [LIVE-WEB]     Tavily/Serper fetched this turn       — fresh but unverified
#
# Layer-C (live web) is triggered ONLY when the VERIFIED data can't answer
# the query — avoiding wasted API calls while guaranteeing accuracy.
# =========================================================================

# Query keyword → VERIFIED field on SavedSupplier.
# Sentinel values:
#   None        — DB does not cover this dimension (always search the web).
#   "__enrich__" — trigger Tavily URL extract (contact info from the site).
_QUERY_DIMENSIONS: list[tuple[list[str], str | None]] = [
    # Commercial facts (user-filled in Edit Details)
    (["moq", "minimum order", "最小起订"],                             "moq"),
    (["lead time", "delivery time", "交期", "发货", "ship", "dispatch"],"lead_time_days"),
    (["payment", "terms", "付款", "t/t", "lc", "letter of credit"],    "payment_terms"),
    (["incoterm", "fob", "cif", "exw", "ddp"],                         "incoterms"),
    (["warranty", "保修", "guarantee"],                                 "warranty_years"),
    (["quoted price", "quote", "cost", "报价"],                         "quoted_price"),
    (["currency"],                                                      "quoted_currency"),
    # Verification / sample
    (["sample"],                                                        "sample_status"),
    (["factory visit", "verified", "audit", "验厂"],                     "factory_verified_via"),
    # User's own assessment
    (["rating", "star", "评分"],                                        "rating"),
    (["decision", "stage", "shortlist"],                                "decision_stage"),
    (["pros", "strength", "优点"],                                      "pros"),
    (["cons", "weakness", "downside", "缺点"],                          "cons"),
    (["tag"],                                                           "tags"),
    (["next action", "follow up", "deadline"],                          "next_action_date"),
    (["note", "comment", "备注"],                                       "notes"),
    # Contact info → enrich the supplier's website
    (["contact", "email", "phone", "address", "reach", "call",
      "location", "where", "whatsapp", "wechat",
      "联系", "电话", "邮箱", "地址", "微信"],                           "__enrich__"),
    # DB does not cover these — always search
    (["capacity", "产能", "production", "output"],                      None),
    (["review", "reputation", "feedback", "评价", "口碑"],              None),
    (["founded", "established", "history", "year", "成立"],             None),
    (["certification", "certified", "iso", "认证", "证书"],             None),
    (["news", "recent", "latest", "新闻"],                              None),
    (["workforce", "employee", "staff", "员工"],                        None),
]

# Override keyword lists — force web search even when VERIFIED is present.
_VERIFY_KEYWORDS    = ["verify", "confirm", "double-check", "is it true",
                       "really", "actually", "check", "核实", "确认"]
_FRESHNESS_KEYWORDS = ["latest", "current", "now", "today", "still", "recent",
                       "最新", "现在", "目前"]

# Which anomaly/adjustment reason words cast doubt on which VERIFIED field.
_DIMENSION_CONCERN_WORDS: dict[str, list[str]] = {
    "quoted_price":   ["price", "cheap", "low", "expensive", "suspicious", "outlier"],
    "moq":            ["moq", "order"],
    "lead_time_days": ["lead", "delivery"],
    "warranty_years": ["warranty"],
}


def _match_dimensions(query: str) -> list[tuple[list[str], str | None]]:
    """Return the subset of _QUERY_DIMENSIONS whose keywords appear in query."""
    ql = query.lower()
    return [(kw, field) for kw, field in _QUERY_DIMENSIONS if any(k in ql for k in kw)]


def _is_placeholder(val) -> bool:
    """True when a stored VERIFIED value is effectively empty."""
    if val is None:
        return True
    if isinstance(val, (list, tuple, dict)):
        return len(val) == 0
    if isinstance(val, str):
        return val.strip().lower() in ("", "unknown", "n/a", "na", "tbd", "?", "none", "-")
    if isinstance(val, (int, float)):
        return val == 0
    return False


def _dimension_is_flagged(s: SavedSupplier, field_name: str) -> bool:
    """True if anomalies or ai_adjustment cast doubt on this dimension."""
    words = _DIMENSION_CONCERN_WORDS.get(field_name)
    if not words:
        return False
    for a in ((s.anomalies or {}).get("anomalies") or []):
        if any(w in a.lower() for w in words):
            return True
    reason = ((s.ai_adjustment or {}).get("reason") or "").lower()
    if reason and any(w in reason for w in words):
        return True
    return False


def _primary_is_reliable(s: SavedSupplier, field_name: str, query_lower: str) -> bool:
    """
    Return True when the VERIFIED value for `field_name` on `s` is trustworthy
    enough to answer the query without a web lookup.

    Returns False (→ search this supplier) when ANY of:
      1. Value is a placeholder/empty.
      2. Query asks to verify/confirm.
      3. Query needs freshness (latest / current / now).
      4. anomalies or ai_adjustment already flagged this dimension.
    """
    if _is_placeholder(getattr(s, field_name, None)):
        return False
    if any(kw in query_lower for kw in _VERIFY_KEYWORDS):
        return False
    if any(kw in query_lower for kw in _FRESHNESS_KEYWORDS):
        return False
    if _dimension_is_flagged(s, field_name):
        return False
    return True


# ------------------------------------------------------------------
# Four-layer supplier block formatters
# ------------------------------------------------------------------

def _fmt_verified(s: SavedSupplier) -> str:
    """[VERIFIED] block — user-filled Tier 1/2/3 fields. Skips empty values."""
    lines = ["[VERIFIED — entered by user after direct verification (quote / sample / factory check)]"]

    if s.decision_stage: lines.append(f"  Decision stage: {s.decision_stage}")
    if s.rating:         lines.append(f"  Rating: {s.rating}/5")
    if s.tags:           lines.append(f"  Tags: {', '.join(s.tags)}")
    if s.pros:           lines.append(f"  Pros: {'; '.join(s.pros)}")
    if s.cons:           lines.append(f"  Cons: {'; '.join(s.cons)}")

    if s.quoted_price is not None:
        head = f"  Quoted price: {s.quoted_price} {s.quoted_currency or 'USD'}/{s.quoted_unit or 'sqm'}"
        extras = []
        if s.moq is not None:            extras.append(f"MOQ: {s.moq}")
        if s.lead_time_days is not None: extras.append(f"Lead time: {s.lead_time_days} days")
        if extras: head += "  (" + ", ".join(extras) + ")"
        lines.append(head)
    else:
        if s.moq is not None:            lines.append(f"  MOQ: {s.moq}")
        if s.lead_time_days is not None: lines.append(f"  Lead time: {s.lead_time_days} days")
    if s.payment_terms: lines.append(f"  Payment terms: {s.payment_terms}")
    if s.incoterms:     lines.append(f"  Incoterms: {s.incoterms}")

    if s.sample_status:
        sq = f"  Sample status: {s.sample_status}"
        if s.sample_quality is not None: sq += f" (quality: {s.sample_quality}/5)"
        lines.append(sq)
    if s.factory_verified_via:
        lines.append(f"  Factory verified via: {', '.join(s.factory_verified_via)}")
    if s.warranty_years is not None: lines.append(f"  Warranty: {s.warranty_years} years")

    # References (three free-text fields shown as "Reference 1/2/3" in the UI)
    refs = [r for r in (s.coating_confirmed, s.core_material_confirmed, s.fire_rating_confirmed) if r]
    if refs: lines.append(f"  References: {'; '.join(refs)}")

    if s.next_action_date: lines.append(f"  Next action: {s.next_action_date}")
    if s.notes:            lines.append(f"  Notes: {s.notes}")

    if len(lines) == 1:
        lines.append("  (no user data filled yet)")
    return "\n".join(lines)


def _fmt_secondary_a(s: SavedSupplier) -> str:
    """Rule-based block: risk, value, description, anomalies."""
    lines = ["[SECONDARY-A — rule-based, deterministic snapshot]"]
    if s.risk_level:
        rs = f"  Risk: {s.risk_level}"
        if s.risk_score is not None:
            rs += f" (score: {s.risk_score:.2f})"
        lines.append(rs)
    if s.risk_reasons:
        lines.append(f"  Risk reasons: {'; '.join(s.risk_reasons)}")
    if s.value_score is not None:
        lines.append(f"  Value score: {s.value_score}/100")
    if s.price_display:
        lines.append(f"  Displayed price: {s.price_display}")
    if s.description:
        lines.append(f"  Description: {s.description[:300]}")

    anoms    = (s.anomalies or {}).get("anomalies") or []
    severity = (s.anomalies or {}).get("severity", "none")
    if anoms:
        lines.append(f"  Anomalies [severity: {severity}]:")
        for a in anoms:
            lines.append(f"    - {a}")
    return "\n".join(lines)


def _fmt_secondary_b(s: SavedSupplier) -> str:
    """AI-derived adjustments. Returns '' when nothing meaningful to show."""
    adj = s.ai_adjustment or {}
    if not adj or adj.get("confidence", 0) < 0.5 or adj.get("adjustment", 0) == 0:
        return ""
    sign = "+" if adj["adjustment"] > 0 else ""
    line = (f"  AI adjustment: {sign}{adj['adjustment']} "
            f"(confidence {adj['confidence']:.2f}, "
            f"reason: \"{adj.get('reason', '')}\")")
    return "[SECONDARY-B — AI-derived, lower authority]\n" + line


# ── Deep-Research integration (Strategy B: facts only, no AI opinions) ──
# Deep Report is AI-synthesized from web data, so echoing its opinion sections
# back into Ask AI would create an "AI answering AI" loop. We only pipe the
# fact-heavy sections (certifications, company overview, capacity). Opinion
# sections (summary / reputation / risk_signals) are DELIBERATELY SKIPPED.

_DEEP_REPORT_SAFE_SECTIONS = ["certifications", "company_overview", "capacity"]

# Maps _QUERY_DIMENSIONS keywords (the ones with field=None) to the deep_report
# section that can satisfy them, so we can skip live web searches when the
# supplier's cached report already covers it.
_DEEP_REPORT_SECTION_MAP: dict[str, str] = {
    "certification": "certifications",
    "certified":     "certifications",
    "iso":           "certifications",
    "认证":          "certifications",
    "证书":          "certifications",
    "capacity":      "capacity",
    "production":    "capacity",
    "output":        "capacity",
    "产能":          "capacity",
    "founded":       "company_overview",
    "established":   "company_overview",
    "history":       "company_overview",
    "year":          "company_overview",
    "成立":          "company_overview",
}

# How fresh the cached report needs to be before we trust it enough to
# suppress a live web search. Beyond this, we include the report in the
# prompt as context but still search fresh web data.
_DEEP_REPORT_FRESH_DAYS = 30


def _deep_report_has_content(s: SavedSupplier, section: str) -> bool:
    """True when the supplier has a non-empty, non-'not found' entry for
    the given deep_report section."""
    dr = s.deep_report or {}
    content = (dr.get("sections", {}).get(section) or "").strip()
    if not content:
        return False
    low = content.lower()
    return not ("not found" in low or "not available" in low)


def _deep_report_is_fresh(s: SavedSupplier) -> bool:
    """True when the report was generated within _DEEP_REPORT_FRESH_DAYS."""
    ts = s.report_generated_at
    if ts is None:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        # stored_at may be naive — normalize to UTC for a safe subtract
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts) < timedelta(days=_DEEP_REPORT_FRESH_DAYS)
    except Exception:
        return False


def _deep_report_section_for_keywords(keywords: list[str]) -> str | None:
    """Return the safe section name that answers these query keywords, or None."""
    for kw in keywords:
        section = _DEEP_REPORT_SECTION_MAP.get(kw)
        if section and section in _DEEP_REPORT_SAFE_SECTIONS:
            return section
    return None


def _fmt_deep_research(s: SavedSupplier) -> str:
    """
    [DEEP-RESEARCH] block — FACTS ONLY (Strategy B).
    Returns "" when the supplier has no report or no usable fact sections.
    """
    dr = s.deep_report
    if not dr:
        return ""

    sections = dr.get("sections", {})
    gen_at = s.report_generated_at.isoformat()[:10] if s.report_generated_at else "?"

    fact_lines: list[str] = []
    for key in _DEEP_REPORT_SAFE_SECTIONS:
        val = (sections.get(key) or "").strip()
        if not val:
            continue
        if "not found" in val.lower() or "not available" in val.lower():
            continue
        label = key.replace("_", " ").title()
        fact_lines.append(f"  {label}: {val[:400]}")

    if not fact_lines:
        return ""

    header = (
        f"[DEEP-RESEARCH — AI-synthesized from web data (generated {gen_at}).\n"
        f"                 Only concrete facts shown below; AI-opinion\n"
        f"                 sections (summary, reputation, risk_signals)\n"
        f"                 deliberately omitted to avoid AI-echo.]"
    )
    return header + "\n" + "\n".join(fact_lines)


def _fmt_live_web(name: str, enriched_data: dict, web_results: dict) -> str:
    """Live-web block. Only rendered when something was actually fetched."""
    chunks = []
    if name in enriched_data:
        chunks.append(f"  Website content [source: web-fetch]:\n    {enriched_data[name][:2000]}")
    if name in web_results:
        chunks.append(f"  Google search results [source: web-search]:\n    {web_results[name][:1500]}")
    if not chunks:
        return ""
    return "[LIVE-WEB — fetched this turn, may be stale after a few minutes]\n" + "\n\n".join(chunks)


def _format_supplier_block(
    s: SavedSupplier,
    enriched_data: dict,
    web_results: dict,
    attachments: list[SupplierAttachment] | None = None,
) -> str:
    """Assemble a single supplier's four-layer block (plus attachments if any)."""
    header = (f"==============================\n"
              f"SUPPLIER: {s.supplier_name} ({s.country or 'unknown'})\n"
              f"URL: {s.url or '(none)'}\n"
              f"==============================")
    parts = [header, _fmt_verified(s), _fmt_secondary_a(s)]
    sb = _fmt_secondary_b(s)
    if sb:
        parts.append(sb)
    at = _fmt_attachments(s, attachments or [])
    if at:
        parts.append(at)
    # DEEP-RESEARCH block comes BEFORE LIVE-WEB in the prompt because
    # fresh LIVE-WEB (when present) should take priority, and the RULES
    # tell the AI so. The block itself only lists concrete facts.
    dr = _fmt_deep_research(s)
    if dr:
        parts.append(dr)
    lw = _fmt_live_web(s.supplier_name, enriched_data, web_results)
    if lw:
        parts.append(lw)
    return "\n\n".join(parts)


# =========================================================================
# Layer-C fetch infrastructure: parallel execution + TTL cache
# =========================================================================

# Separate caches per fetch type. Keys are normalized so the same underlying
# lookup from different phrasings hits the same entry.
_LAYER_C_TTL = 300   # 5 minutes
_LAYER_C_MAX_WORKERS = 8
_enrich_cache: dict[str, tuple[float, str | None]] = {}   # url → (ts, content)
_search_cache: dict[str, tuple[float, str | None]] = {}   # key → (ts, formatted block)


def _cache_get(cache: dict, key: str):
    hit = cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.time() - ts > _LAYER_C_TTL:
        cache.pop(key, None)
        return None
    return data


def _cache_set(cache: dict, key: str, data) -> None:
    cache[key] = (time.time(), data)


def _serper_cache_key(supplier_name: str, query: str) -> str:
    # Collapse whitespace + lowercase so "iso cert" and "ISO  cert" share a key.
    norm = " ".join(query.lower().split())
    digest = hashlib.sha1(f"{supplier_name}||{norm}".encode("utf-8")).hexdigest()
    return digest[:16]


def _fetch_enrich_one(s: SavedSupplier) -> tuple[str, str | None]:
    """Run (or return cached) Tavily extract for one supplier."""
    if not s.url:
        return (s.supplier_name, None)

    cached = _cache_get(_enrich_cache, s.url)
    if cached is not None:
        _log.info("[ai-search] enrich CACHE HIT %s", s.url[:60])
        return (s.supplier_name, cached)

    try:
        result = enrich_url(s.url)
        content = (result or {}).get("content") or None
        if content:
            content = content[:4000]
        _cache_set(_enrich_cache, s.url, content)
        return (s.supplier_name, content)
    except Exception as e:
        _log.warning("Enrichment failed for %s: %s", s.supplier_name, e)
        return (s.supplier_name, None)


def _fetch_serper_one(s: SavedSupplier, user_query: str) -> tuple[str, str | None]:
    """Run (or return cached) Serper search for one supplier + user query."""
    cache_key = _serper_cache_key(s.supplier_name, user_query)

    cached = _cache_get(_search_cache, cache_key)
    if cached is not None:
        _log.info("[ai-search] serper CACHE HIT %s", s.supplier_name[:40])
        return (s.supplier_name, cached)

    try:
        q = f"{s.supplier_name} {user_query}"
        hits = serper_search(q, max_results=5)
        if not hits:
            _cache_set(_search_cache, cache_key, None)
            return (s.supplier_name, None)
        block = "\n".join(
            f"- {h.get('title', '')}: {h.get('content', '')[:300]}"
            for h in hits[:5]
        )
        _cache_set(_search_cache, cache_key, block)
        return (s.supplier_name, block)
    except (SerperError, Exception) as e:
        _log.warning("Web search failed for %s: %s", s.supplier_name, e)
        return (s.supplier_name, None)


def _fetch_layer_c_parallel(
    enrich_targets: dict[int, SavedSupplier],
    search_targets: dict[int, SavedSupplier],
    user_query: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Run all Tavily enrichments and Serper searches concurrently.
    Wall-clock time drops from sum-of-calls to roughly the slowest call.
    """
    enriched_data: dict[str, str] = {}
    web_results:   dict[str, str] = {}

    jobs: list = []
    tavily_on = tavily_available()

    with ThreadPoolExecutor(max_workers=_LAYER_C_MAX_WORKERS) as ex:
        if tavily_on:
            for s in enrich_targets.values():
                jobs.append(("enrich", ex.submit(_fetch_enrich_one, s)))
        for s in search_targets.values():
            jobs.append(("search", ex.submit(_fetch_serper_one, s, user_query)))

        for kind, fut in jobs:
            name, content = fut.result()
            if not content:
                continue
            if kind == "enrich":
                enriched_data[name] = content
            else:
                web_results[name] = content

    return enriched_data, web_results


# =========================================================================
# Attachments → AI readable text
#
# Attachments are stored on disk by `POST /suppliers/{id}/attachments`.
# For Ask-AI we extract readable text from text-based formats only
# (PDF, Word, Excel, CSV, plain text). Images / archives are listed by
# name so the AI knows they exist but cannot read them.
# =========================================================================

_ATTACHMENT_TEXT_BUDGET_PER_FILE = 3000    # chars per file to keep prompt sane
_ATTACHMENT_TEXT_BUDGET_TOTAL    = 8000    # chars per supplier total

# Cache extracted text per (path, mtime) so we don't re-parse unchanged files.
_attachment_text_cache: dict[tuple[str, float], str] = {}


def _extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF; swallows errors and returns '' on failure."""
    try:
        import fitz  # pymupdf
    except ImportError:
        return ""
    try:
        out = []
        with fitz.open(str(path)) as doc:
            for page in doc:
                out.append(page.get_text("text"))
                if sum(len(x) for x in out) > _ATTACHMENT_TEXT_BUDGET_PER_FILE * 2:
                    break
        return "\n".join(out).strip()
    except Exception as e:
        _log.warning("PDF extract failed for %s: %s", path.name, e)
        return ""


def _extract_docx_text(path: Path) -> str:
    try:
        import docx
    except ImportError:
        return ""
    try:
        d = docx.Document(str(path))
        paras = [p.text for p in d.paragraphs if p.text.strip()]
        # Include table cells — many supplier quotes live in Word tables
        for table in d.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    paras.append(" | ".join(cells))
        return "\n".join(paras).strip()
    except Exception as e:
        _log.warning("DOCX extract failed for %s: %s", path.name, e)
        return ""


def _extract_xlsx_text(path: Path) -> str:
    try:
        import openpyxl
    except ImportError:
        return ""
    try:
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        out = []
        for sheet in wb.worksheets:
            out.append(f"== Sheet: {sheet.title} ==")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    out.append(" | ".join(cells))
                if sum(len(x) for x in out) > _ATTACHMENT_TEXT_BUDGET_PER_FILE * 2:
                    break
        wb.close()
        return "\n".join(out).strip()
    except Exception as e:
        _log.warning("XLSX extract failed for %s: %s", path.name, e)
        return ""


def _extract_plain_text(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception as e:
        _log.warning("Plain text read failed for %s: %s", path.name, e)
        return ""


# ── Image attachments → Gemma 3 vision ──────────────────────────────────
# Images aren't turned into text here; we hand the raw pixels to Gemma's
# multimodal endpoint. Resize + JPEG-compress first so a 4K screenshot
# doesn't blow up the request size.

_MAX_IMAGE_DIMENSION   = 1600  # px — bigger gets downscaled
_JPEG_QUALITY          = 85
_MAX_IMAGES_PER_SUPPLIER = 3   # cap vision tokens; newest first

# Cache prepared JPEG bytes per (path, mtime) so repeat queries within the
# Layer-C TTL window don't re-read + re-compress the same image.
_image_bytes_cache: dict[tuple[str, float], bytes | None] = {}


def _is_image_attachment(att: SupplierAttachment) -> bool:
    name = (att.filename or "").lower()
    mime = (att.mime_type or "").lower()
    return mime.startswith("image/") or name.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
    )


def _prepare_image_for_gemma(att: SupplierAttachment) -> bytes | None:
    """
    Load, resize (long edge ≤ 1600px), and JPEG-compress an image attachment.
    Returns JPEG bytes, or None on any failure. Cached per (path, mtime).
    """
    disk_path = _PROJECT_ROOT / att.stored_path
    if not disk_path.exists():
        return None
    try:
        mtime = disk_path.stat().st_mtime
    except OSError:
        return None

    cache_key = (str(disk_path), mtime)
    if cache_key in _image_bytes_cache:
        return _image_bytes_cache[cache_key]

    try:
        from PIL import Image
        import io
        with Image.open(disk_path) as img:
            # Strip alpha for JPEG output; paste on white to avoid black halos
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            w, h = img.size
            longest = max(w, h)
            if longest > _MAX_IMAGE_DIMENSION:
                scale = _MAX_IMAGE_DIMENSION / longest
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            data = buf.getvalue()
    except Exception as e:
        _log.warning("Image prep failed for %s: %s", att.filename, e)
        _image_bytes_cache[cache_key] = None
        return None

    _image_bytes_cache[cache_key] = data
    return data


def _collect_images_for_ai(
    atts_by_supplier: dict[int, list[SupplierAttachment]],
    max_images: int = _MAX_IMAGES_PER_SUPPLIER,
) -> tuple[list[bytes], list[tuple[str, str]]]:
    """
    Walk the attachment map and prepare up to `max_images` JPEG payloads per
    supplier. Returns:
        images:    list[bytes]  — in the order the AI will see them
        vision_index: list[(supplier_name, filename)] — parallel to images,
                                                        so the prompt can
                                                        label each thumbnail
    """
    images: list[bytes] = []
    vision_index: list[tuple[str, str]] = []

    for supplier_id, atts in atts_by_supplier.items():
        image_atts = [a for a in atts if _is_image_attachment(a)]
        if not image_atts:
            continue
        # atts are already ordered newest-first by the DB query
        for a in image_atts[:max_images]:
            data = _prepare_image_for_gemma(a)
            if not data:
                continue
            images.append(data)
            # supplier_name pulled from any att (they share supplier via FK)
            vision_index.append((a.supplier.supplier_name, a.filename))

    return images, vision_index


def _extract_attachment_text(att: SupplierAttachment) -> str:
    """Dispatch on mime_type / extension. Returns '' for unsupported formats."""
    disk_path = _PROJECT_ROOT / att.stored_path
    if not disk_path.exists():
        return ""

    # Cache by (path, mtime) to avoid re-parsing unchanged files.
    try:
        mtime = disk_path.stat().st_mtime
    except OSError:
        return ""
    cache_key = (str(disk_path), mtime)
    if cache_key in _attachment_text_cache:
        return _attachment_text_cache[cache_key]

    name = (att.filename or "").lower()
    mime = (att.mime_type or "").lower()

    if mime == "application/pdf" or name.endswith(".pdf"):
        text = _extract_pdf_text(disk_path)
    elif "word" in mime or name.endswith((".docx",)):
        text = _extract_docx_text(disk_path)
    elif "spreadsheet" in mime or name.endswith((".xlsx", ".xlsm")):
        text = _extract_xlsx_text(disk_path)
    elif mime.startswith("text/") or name.endswith((".txt", ".csv", ".md", ".log")):
        text = _extract_plain_text(disk_path)
    else:
        text = ""   # images, archives, .doc (legacy) → list by name only

    text = text[:_ATTACHMENT_TEXT_BUDGET_PER_FILE]
    _attachment_text_cache[cache_key] = text
    return text


def _fmt_attachments(s: SavedSupplier, atts: list[SupplierAttachment]) -> str:
    """
    [ATTACHMENTS] block. Text-based files get their content inlined;
    images are marked "attached to AI vision" — they're sent separately
    as multimodal inline_data, not as text.
    """
    if not atts:
        return ""

    lines = ["[ATTACHMENTS — user-uploaded files for this supplier]"]
    total = 0
    images_shown = 0
    for a in atts:
        header = f"  📎 {a.filename}"
        if a.size_bytes:
            header += f" ({a.size_bytes // 1024} KB)"

        if _is_image_attachment(a):
            # Images are fed through Gemma vision — mark them so the AI
            # knows to look at the visual content it received alongside
            # this prompt. Respect the per-supplier cap so the hint here
            # matches what was actually sent.
            if images_shown < _MAX_IMAGES_PER_SUPPLIER:
                lines.append(header + " — attached to AI vision ✓")
                images_shown += 1
            else:
                lines.append(header + " — image not sent to AI (over limit)")
            continue

        text = _extract_attachment_text(a) if total < _ATTACHMENT_TEXT_BUDGET_TOTAL else ""
        lines.append(header)
        if text:
            remaining = _ATTACHMENT_TEXT_BUDGET_TOTAL - total
            snippet = text[:remaining]
            indented = "\n".join("      " + line for line in snippet.splitlines() if line.strip())
            lines.append(indented)
            total += len(snippet)
        else:
            lines.append("      (binary or unsupported format — AI can reference the filename but cannot read contents)")

    return "\n".join(lines)


@router.post("/ai-search")
def ai_search(req: AiSearchRequest, db: Session = Depends(get_db)):
    all_suppliers = db.query(SavedSupplier).all()
    if not all_suppliers:
        return {"answer": "You haven't saved any suppliers yet. Run an analysis and save some suppliers first.", "results": []}

    # --- Scope: Compare mode (user selected N) vs Explore mode ---
    if req.selected_ids:
        suppliers = [s for s in all_suppliers if s.id in req.selected_ids]
        compare_mode = True
        scope_note = (f"COMPARE MODE — user has SELECTED {len(suppliers)} specific "
                      f"suppliers. Focus ONLY on these and structure the answer "
                      f"head-to-head across all of them (even if some have 'no data').")
    else:
        suppliers = all_suppliers
        compare_mode = False
        scope_note = ("EXPLORE MODE — user has not selected specific suppliers "
                      "in the UI. Default scope is the full saved set, BUT if "
                      "the user names a single supplier in the question itself, "
                      "narrow focus to only that supplier (see SCOPE RULE).")

    if not suppliers:
        return {"answer": "No suppliers match the selection.", "results": []}

    query_lower = req.query.lower()

    # --- Smart Layer-C trigger plan ---
    # Compare mode: all selected suppliers are candidates (user asked about them).
    # Explore mode: only top-3 by value_score to control API cost.
    if compare_mode:
        default_targets = suppliers
    else:
        default_targets = sorted(suppliers, key=lambda s: s.value_score or 0, reverse=True)[:3]

    enrich_targets: dict[int, SavedSupplier] = {}   # id → supplier (Tavily extract)
    search_targets: dict[int, SavedSupplier] = {}   # id → supplier (Serper search)

    for keywords, field in _match_dimensions(req.query):
        if field == "__enrich__":
            for s in default_targets:
                enrich_targets[s.id] = s
            continue
        if field is None:
            # VERIFIED doesn't cover this dimension. But if a fresh cached
            # deep_report already has the answer, we can skip the live web
            # search (strategy-B optimization) — deep_report is grounded
            # in web data and cheaper than a re-fetch.
            dr_section = _deep_report_section_for_keywords(keywords)
            for s in default_targets:
                if (dr_section
                        and _deep_report_is_fresh(s)
                        and _deep_report_has_content(s, dr_section)):
                    continue   # already covered
                search_targets[s.id] = s
            continue
        # VERIFIED field known → only search suppliers whose value is unreliable
        for s in default_targets:
            if not _primary_is_reliable(s, field, query_lower):
                search_targets[s.id] = s

    # --- Layer-C fetches (parallel, TTL-cached) ---
    # Runs all Tavily + Serper calls concurrently. Each call goes through
    # a 5-minute TTL cache keyed on url / (supplier_name + normalized query),
    # so rapid-fire repeat questions don't re-hit the paid APIs.
    t_layer_c = time.time()
    enriched_data, web_results = _fetch_layer_c_parallel(
        enrich_targets, search_targets, req.query
    )
    if enrich_targets or search_targets:
        _log.info(
            "[ai-search] Layer-C done in %.1fs  enrich=%d/%d  search=%d/%d",
            time.time() - t_layer_c,
            len(enriched_data), len(enrich_targets),
            len(web_results),  len(search_targets),
        )

    # --- Load all attachments for in-scope suppliers in one query ---
    supplier_ids = [s.id for s in suppliers]
    att_rows = (db.query(SupplierAttachment)
                  .filter(SupplierAttachment.supplier_id.in_(supplier_ids))
                  .order_by(SupplierAttachment.uploaded_at.desc())
                  .all()) if supplier_ids else []
    atts_by_supplier: dict[int, list[SupplierAttachment]] = {}
    for a in att_rows:
        atts_by_supplier.setdefault(a.supplier_id, []).append(a)

    # --- Collect image attachments for Gemma vision ---
    # Images are passed alongside the prompt via `call_model(... images=...)`.
    # _fmt_attachments will mark them in the text prompt so the AI knows
    # which image it's looking at when multiple are attached.
    ai_images, vision_index = _collect_images_for_ai(atts_by_supplier)
    if ai_images:
        _log.info("[ai-search] sending %d image(s) to Gemma vision", len(ai_images))

    # --- Build per-supplier four-layer blocks ---
    supplier_data = "\n\n".join(
        _format_supplier_block(s, enriched_data, web_results, atts_by_supplier.get(s.id))
        for s in suppliers
    )

    # Short, ordered index of the images the AI will see. Lets the model
    # tie each "image N" it sees to a specific supplier filename.
    vision_block = ""
    if vision_index:
        lines = ["\n\nIMAGES ATTACHED TO THIS PROMPT (in order):"]
        for i, (supplier_name, filename) in enumerate(vision_index, start=1):
            lines.append(f"  Image {i}: {filename}  →  supplier: {supplier_name}")
        vision_block = "\n".join(lines)

    # --- Conversation history ---
    history_block = ""
    if req.history:
        turns = []
        for t in req.history[-6:]:   # last 6 turns only
            role = "User" if t.role == "user" else "Assistant"
            turns.append(f"{role}: {t.content}")
        history_block = "\n\nCONVERSATION HISTORY (for context — the user may refer to earlier answers):\n" + "\n".join(turns)

    prompt = f"""You are a supplier intelligence assistant.

{scope_note}

Each supplier below is presented in up to six data layers:
  [VERIFIED]      user-entered — GROUND TRUTH.
  [SECONDARY-A]   rule-based scoring + anomaly detection.
  [SECONDARY-B]   AI-derived adjustments (optional, lower authority).
  [ATTACHMENTS]   files the user uploaded (quotes, spec sheets, photos).
                  Image attachments marked "attached to AI vision ✓" are
                  also sent to you as image inputs alongside this prompt;
                  read their contents directly when answering.
  [DEEP-RESEARCH] AI-synthesized research from a previous web investigation
                  (Serper + Tavily digest). ONLY fact-heavy sections are
                  included — subjective/opinion sections are intentionally
                  omitted so you do not re-cite a previous AI's opinions
                  as your own evidence.
  [LIVE-WEB]      fetched this turn from the supplier's website or Google.

{supplier_data}{vision_block}{history_block}

User question: {req.query}

================================================================
CRITICAL RULES — read before answering
================================================================

1. VERIFIED fields are GROUND TRUTH. The user entered them after real
   verification (received a quote, reviewed a sample, audited the factory).
   NEVER contradict a VERIFIED value with web data.

2. Source priority when facts disagree:
      VERIFIED  >  ATTACHMENTS  >  LIVE-WEB  >  DEEP-RESEARCH  >  SECONDARY-A  >  SECONDARY-B
   (Attachments are user-curated documents — trustworthy, second only to
    VERIFIED. LIVE-WEB is the freshest external data. DEEP-RESEARCH is
    cached AI-synthesis of earlier web data — useful for concrete facts
    like certification names and company info, but since it was itself
    produced by AI, treat it as second-hand evidence.)

3. NEVER hedge VERIFIED values. Do not say "approximately", "around",
   "reportedly", or "claims to be". Quote the exact user-provided value.

4. When VERIFIED and LIVE-WEB disagree, the VERIFIED value IS the answer.
   Mention the web value only as an informational note about the
   discrepancy (often just an outdated website).
   Example: "MOQ is 500 [user-verified]. Note: their public site lists
   1000 [web-search], which may be outdated."

5. Tag every fact with its source using these EXACT tags (preserve
   capitalisation and internal spaces):
      [user-verified]  [attachment]  [web-fetch]  [web-search]
      [AI Deep Report]  [rule-based]  [AI-analysis]

   When citing anything drawn from the [DEEP-RESEARCH] data block, the
   corresponding output tag is [AI Deep Report] (matching the button
   that originally produced the report).

6. If a dimension is missing across ALL layers, say "no data available" —
   do NOT invent, paraphrase, or guess.

   DEEP-RESEARCH anti-echo rule: only reference the [DEEP-RESEARCH]
   block for concrete facts (certification names, dates, numbers,
   company info). Cite them with the tag [AI Deep Report].
   Do NOT reuse the block's phrasing for subjective claims like
   "concerning complaints" or "cautious approach recommended" — those
   were AI opinions in a previous turn and must not be re-cited as
   evidence.

7. Do not move facts between suppliers. Each fact belongs only to the
   supplier under whose block it appeared.

8. SCOPE RULE (HIGHEST PRIORITY — overrides rule 9 below):

   Inspect the user's question for supplier names. If the question
   mentions ONE specific supplier by name (or by an unambiguous
   descriptor like "the Chinese one"), answer ONLY about that supplier.
   Do NOT volunteer information about the other suppliers.

   Examples of single-supplier questions:
     - "Alstrong's contact email"
     - "Best Aluminum Composite Panel Manufacturer in China 能找到图片么?"
     - "What are Reynobond's certifications?"
     - "the lowest risk one — what's their MOQ?"
   → Answer for that ONE supplier only.

   The mode-awareness rule below only applies when the question does
   NOT name a specific supplier.

9. Mode awareness (applies only when the question is NOT about a
   specific supplier per rule 8):
   - COMPARE MODE → structure the answer as head-to-head across ALL
     selected suppliers, even when a supplier has "no data" on a dimension.
   - EXPLORE MODE → pick the best match(es) from the full saved set and
     justify the shortlist.

10. Before answering a ranking question (lowest/highest/best X), scan all
    suppliers and handle ties: if multiple suppliers tie on the primary
    criterion (e.g. risk=0), break the tie by higher value_score then
    lower price. Never pick a tied supplier with a worse value_score.

11. If CONVERSATION HISTORY is provided, resolve references like
    "that supplier" or "the first one" from earlier turns.

12. LANGUAGE LOCK — reply strictly in the SAME LANGUAGE the user used in
    their question. If the question is in Chinese, every free-form string
    in your JSON (summary, reasons, tradeoffs, info, answer) MUST be in
    Chinese. Do not translate to English just because the prompt is in
    English. Source tags like [user-verified] stay literal in both languages.

================================================================
OUTPUT FORMAT — return ONLY a JSON object (no markdown fences)
================================================================

Classify the question first:
  "recommendation" — asking for ONE top pick / ranking / "which is best"
  "info"           — asking for specific facts, OR asking for an open-ended
                     opinion / assessment / overview ("what do you think",
                     "tell me about them", "你觉得怎么样", "介绍一下"). In
                     this case discuss EVERY in-scope supplier, not just one.
  "mixed"          — both recommend AND give info

FORMAT A — recommendation:
{{
  "type": "recommendation",
  "recommendation": {{
    "supplier_name": "name of top recommended supplier",
    "country": "country",
    "reasons": ["reason 1 [source-tag]", "reason 2 [source-tag]"],
    "tradeoffs": ["tradeoff 1"]
  }},
  "summary": "2-3 sentence answer citing specific data with [source-tag]s",
  "highlights": ["supplier_name_1", "supplier_name_2"]
}}

FORMAT B — info:
{{
  "type": "info",
  "answer": "PLAIN STRING with line breaks. For assessment / overview /
             '你觉得怎么样' style questions, give a short paragraph for
             EACH in-scope supplier (use supplier name as a sub-heading).
             For narrow factual questions, answer directly about just the
             supplier(s) asked. Tag every fact with its [source-tag].",
  "highlights": ["supplier_name_1", "supplier_name_2"]
}}

FORMAT C — mixed:
{{
  "type": "mixed",
  "recommendation": {{
    "supplier_name": "…",
    "country": "…",
    "reasons": ["…"],
    "tradeoffs": ["…"]
  }},
  "info": "PLAIN STRING with line breaks and source tags.",
  "summary": "Short sentence tying it together.",
  "highlights": ["supplier_name_1"]
}}

Output ONLY the JSON object. No markdown, no extra text."""

    try:
        raw = call_model(prompt, images=ai_images if ai_images else None)
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
        # User primary decision data
        "decision_stage": s.decision_stage,
        "rating": s.rating,
        "tags": s.tags,
        "pros": s.pros,
        "cons": s.cons,
        "quoted_price": s.quoted_price,
        "quoted_currency": s.quoted_currency,
        "quoted_unit": s.quoted_unit,
        "moq": s.moq,
        "lead_time_days": s.lead_time_days,
        "payment_terms": s.payment_terms,
        "incoterms": s.incoterms,
        "sample_status": s.sample_status,
        "sample_quality": s.sample_quality,
        "factory_verified_via": s.factory_verified_via,
        "coating_confirmed": s.coating_confirmed,
        "core_material_confirmed": s.core_material_confirmed,
        "fire_rating_confirmed": s.fire_rating_confirmed,
        "warranty_years": s.warranty_years,
        "next_action_date": s.next_action_date.isoformat() if s.next_action_date else None,
    }


# ---------------------------------------------------------------------------
# Attachments — user-uploaded files attached to a saved supplier
# ---------------------------------------------------------------------------

def _attachment_to_dict(a: SupplierAttachment) -> dict:
    return {
        "id":          a.id,
        "supplier_id": a.supplier_id,
        "filename":    a.filename,
        "mime_type":   a.mime_type,
        "size_bytes":  a.size_bytes,
        "uploaded_at": a.uploaded_at.isoformat() if a.uploaded_at else None,
    }


@router.get("/suppliers/{supplier_id}/attachments")
def list_attachments(supplier_id: int, db: Session = Depends(get_db)):
    """Return metadata for every attachment belonging to this supplier."""
    if not db.query(SavedSupplier).filter(SavedSupplier.id == supplier_id).first():
        raise HTTPException(status_code=404, detail="Supplier not found")

    rows = (db.query(SupplierAttachment)
              .filter(SupplierAttachment.supplier_id == supplier_id)
              .order_by(SupplierAttachment.uploaded_at.desc())
              .all())
    return [_attachment_to_dict(a) for a in rows]


@router.post("/suppliers/{supplier_id}/attachments")
async def upload_attachment(
    supplier_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Accept a multipart file upload and store it under uploads/{supplier_id}/."""
    supplier = db.query(SavedSupplier).filter(SavedSupplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")

    safe_name = _sanitize_filename(file.filename or "file")
    supplier_dir = _UPLOAD_ROOT / str(supplier_id)
    supplier_dir.mkdir(parents=True, exist_ok=True)

    # Two-phase write: reserve a unique disk name, stream the file there,
    # then persist the DB row. A random prefix avoids collisions and makes
    # the directory resistant to guessing.
    unique_prefix = uuid.uuid4().hex[:10]
    disk_name = f"{unique_prefix}_{safe_name}"
    disk_path = supplier_dir / disk_name

    size = 0
    try:
        with disk_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)  # 1 MB chunks
                if not chunk:
                    break
                out.write(chunk)
                size += len(chunk)
    except Exception as e:
        # Clean up partial file on error
        try:
            disk_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    mime, _ = mimetypes.guess_type(file.filename or safe_name)

    att = SupplierAttachment(
        supplier_id=supplier_id,
        filename=file.filename or safe_name,
        stored_path=str(disk_path.relative_to(_PROJECT_ROOT).as_posix()),
        mime_type=file.content_type or mime,
        size_bytes=size,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return _attachment_to_dict(att)


@router.get("/suppliers/{supplier_id}/attachments/{att_id}")
def download_attachment(supplier_id: int, att_id: int, db: Session = Depends(get_db)):
    """Serve the stored file back for download (inline when browser can render)."""
    att = (db.query(SupplierAttachment)
             .filter(SupplierAttachment.id == att_id,
                     SupplierAttachment.supplier_id == supplier_id)
             .first())
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")

    disk_path = _PROJECT_ROOT / att.stored_path
    if not disk_path.exists():
        raise HTTPException(status_code=410, detail="File is missing from disk")

    return FileResponse(
        disk_path,
        filename=att.filename,
        media_type=att.mime_type or "application/octet-stream",
    )


@router.delete("/suppliers/{supplier_id}/attachments/{att_id}")
def delete_attachment(supplier_id: int, att_id: int, db: Session = Depends(get_db)):
    """Remove the DB row and best-effort unlink the file on disk."""
    att = (db.query(SupplierAttachment)
             .filter(SupplierAttachment.id == att_id,
                     SupplierAttachment.supplier_id == supplier_id)
             .first())
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")

    disk_path = _PROJECT_ROOT / att.stored_path
    try:
        disk_path.unlink(missing_ok=True)
    except Exception as e:
        _log.warning("Could not unlink %s: %s", disk_path, e)

    db.delete(att)
    db.commit()
    return {"ok": True}
