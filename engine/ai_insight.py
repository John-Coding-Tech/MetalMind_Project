"""
engine/ai_insight.py

Layer 02 — AI INSIGHT (Explanation Only).

Responsibility: produce a structured natural-language explanation for a
supplier. This module NEVER scores, ranks, or recommends. The rule engine
is the sole decision-maker; AI Insight only describes.

Public API:
    generate_insight(supplier: SupplierRecord) -> dict

Output schema:
    {
      "summary":        str,     # 2-3 sentence overview
      "key_strengths":  [str],   # positive signals
      "key_risks":      [str],   # risk signals (descriptive, NOT for scoring)
      "hidden_signals": [str],   # subtle concerns a rule engine might miss
      "confidence":     float,   # self-reported confidence in [0,1]
      "source":         "ai" | "fallback",
    }

Determinism: temperature = 0 (via call_model).
Fallback:    any network/JSON/schema failure → neutral "unavailable" dict.
"""

import json
from typing import Any

from modules.cleaner  import SupplierRecord
from engine.ai_engine import call_model, _find_first_json_object


# ---------------------------------------------------------------------------
# Fallback — used on ANY failure path. source marker lets the UI distinguish
# "AI genuinely had nothing to say" from "AI call failed silently".
# ---------------------------------------------------------------------------

_FALLBACK: dict = {
    "summary":        "AI insight unavailable for this supplier.",
    "key_strengths":  [],
    "key_risks":      [],
    "hidden_signals": [],
    "confidence":     0.0,
    "source":         "fallback",
}


# ---------------------------------------------------------------------------
# Prompt — explicitly forbids scoring / ranking / recommendation language.
# ---------------------------------------------------------------------------

_CATEGORY_DISPLAY: dict[str, str] = {
    "acp":             "ACP (aluminium composite panel)",
    "aluminum":        "aluminium",
    "steel":           "carbon steel",
    "stainless_steel": "stainless steel",
    "copper":          "copper",
    "brass":           "brass",
    "zinc":            "zinc",
    "titanium":        "titanium",
    "tube":            "metal tube",
    "pipe":            "metal pipe",
    "unknown":         "metal product",
}


def _build_prompt(supplier: SupplierRecord) -> str:
    cat        = getattr(supplier, "category",   "unknown") or "unknown"
    unit       = getattr(supplier, "price_unit", "unknown") or "unknown"
    cat_label  = _CATEGORY_DISPLAY.get(cat, "metal product")
    unit_label = unit if unit != "unknown" else "unit"
    price = (
        "unknown"
        if supplier.price_est is None
        else f"${supplier.price_est:.2f}/{unit_label} (USD)"
    )
    raw = (supplier.raw_content or "")[:2000]

    return f"""You are an expert procurement analyst producing EXPLANATION ONLY for a {cat_label} supplier.

Your job is to DESCRIBE this supplier's strengths and risks. You are not the decision-maker — a separate rule engine handles scoring and ranking.

STRICT RULES:
- Do NOT assign a score.
- Do NOT rank, recommend, reject, or compare suppliers.
- Do NOT use words like "recommended", "best", "top choice", "winner".
- Return STRICT JSON only — no markdown fences, no prose outside the JSON.

Supplier data:
- Name: {supplier.name}
- Country: {supplier.country}
- Estimated price: {price}
- Description: {supplier.description}
- Page content: {raw}

Return EXACTLY this JSON shape:
{{
  "summary":        "<2-3 sentence factual overview>",
  "key_strengths":  ["<short string>", ...],
  "key_risks":      ["<short string>", ...],
  "hidden_signals": ["<subtle concern a keyword/regex system might miss>", ...],
  "confidence":     <float in [0,1], self-reported confidence>
}}

Constraints:
- Valid JSON — no trailing commas, no comments, no markdown fences.
- Each array may be empty ([]) if nothing to report.
- Keep each string concise (under 120 characters).
"""


# ---------------------------------------------------------------------------
# Parsing & validation
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """Parse JSON from model text, tolerating markdown fences / stray prose."""
    if not text:
        raise ValueError("empty response")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        # strip ```json ... ``` fences
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


def _normalize(raw: Any) -> dict:
    """Coerce parsed JSON into the insight schema. Raises on structural errors."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")

    def _strlist(x) -> list[str]:
        if not isinstance(x, list):
            return []
        out = []
        for s in x:
            t = str(s).strip()
            if t:
                out.append(t[:240])   # hard cap per-item length
        return out

    summary = str(raw.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary missing")

    confidence = raw.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "summary":        summary[:800],
        "key_strengths":  _strlist(raw.get("key_strengths")),
        "key_risks":      _strlist(raw.get("key_risks")),
        "hidden_signals": _strlist(raw.get("hidden_signals")),
        "confidence":     round(confidence, 3),
        "source":         "ai",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_insight(supplier: SupplierRecord) -> dict:
    """
    Run one AI insight pass over a supplier.

    Returns the structured insight schema. Any failure (HTTP, JSON, schema)
    returns the neutral fallback with source="fallback", so upstream callers
    can render gracefully without branching on exceptions.
    """
    prompt = _build_prompt(supplier)
    text   = call_model(prompt)

    if not text:
        return dict(_FALLBACK)

    try:
        parsed = _extract_json(text)
        return _normalize(parsed)
    except (ValueError, json.JSONDecodeError):
        return dict(_FALLBACK)
