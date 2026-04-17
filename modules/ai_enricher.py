"""
modules/ai_enricher.py

Data Intelligence Layer — AI Enhancement (conditional).

Called ONLY when rule-based extraction produces ambiguous results:
  - Company name is invalid ("About Us", "Top 10...", generic)
  - Page type is unclear (directory? article? real supplier?)
  - Rule-based confidence is low

This module NEVER runs on every supplier. The calling code in cleaner.py
decides when to invoke it based on deterministic confidence thresholds.

Public API:
    enhance_with_ai(title, url, content) -> dict | None

Output schema (strict JSON from Gemma):
    {
      "company_name":  str,
      "page_type":     "supplier" | "directory" | "article" | "invalid",
      "supplier_type": "manufacturer" | "reseller" | "trader" | "unknown",
      "confidence":    float in [0,1],
    }

Returns None on any failure (network, parse, schema), so rule-based data
is always the fallback.
"""

import json
from typing import Any

from engine.ai_engine import call_model, _find_first_json_object


# ---------------------------------------------------------------------------
# Prompt — minimal, structured, forbids explanations.
# ---------------------------------------------------------------------------

def _build_prompt(title: str, url: str, content: str) -> str:
    content_trunc = (content or "")[:1500]

    return f"""You are a data classifier for an ACP (aluminium composite panel) supplier database.

Analyze this web page and return STRICT JSON ONLY. No explanation, no markdown fences.

Page data:
- Title: {title}
- URL: {url}
- Content: {content_trunc}

Return EXACTLY this JSON:
{{
  "company_name": "<the actual company name, NOT the page title>",
  "page_type": "<supplier | directory | article | invalid>",
  "supplier_type": "<manufacturer | reseller | trader | unknown>",
  "confidence": <float 0.0 to 1.0>
}}

Classification rules:
- "supplier": a specific company's own website selling or manufacturing ACP.
- "directory": a page listing multiple suppliers (e.g. "Top 10 ACP Manufacturers").
- "article": a news article, blog post, guide, or informational page.
- "invalid": an about page, contact page, login page, privacy policy, or other non-product page.

For company_name:
- Extract the REAL company/brand name, not the page title.
- If the page is a directory or article, use "N/A".
- If the page title says "About Us" but the content reveals a company name, extract that name.

Constraints:
- Valid JSON only — no trailing commas, no comments, no extra text.
- confidence reflects how certain you are about page_type classification.
"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    if not text:
        raise ValueError("empty response")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 3:
            cleaned = parts[1]
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    obj = _find_first_json_object(cleaned)
    if not obj:
        raise ValueError("no JSON object found")
    return json.loads(obj)


_VALID_PAGE_TYPES    = {"supplier", "directory", "article", "invalid"}
_VALID_SUPPLIER_TYPES = {"manufacturer", "reseller", "trader", "unknown"}


def _normalize(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("not an object")

    company_name = str(raw.get("company_name") or "").strip()
    page_type    = str(raw.get("page_type") or "").strip().lower()
    supplier_type = str(raw.get("supplier_type") or "").strip().lower()

    if page_type not in _VALID_PAGE_TYPES:
        raise ValueError(f"invalid page_type: {page_type}")
    if supplier_type not in _VALID_SUPPLIER_TYPES:
        supplier_type = "unknown"

    confidence = raw.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "company_name":  company_name[:200] or "N/A",
        "page_type":     page_type,
        "supplier_type": supplier_type,
        "confidence":    round(confidence, 3),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enhance_with_ai(title: str, url: str, content: str) -> dict | None:
    """
    Ask the AI to classify a page and extract its real company name.

    Returns the normalized dict on success, or None on any failure.
    None signals the caller to keep rule-based data unchanged.
    """
    prompt = _build_prompt(title, url, content)
    text   = call_model(prompt)

    if not text:
        return None

    try:
        parsed = _extract_json(text)
        return _normalize(parsed)
    except (ValueError, json.JSONDecodeError):
        return None
