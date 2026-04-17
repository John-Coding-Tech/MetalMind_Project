"""
engine/ai_crosscheck.py

Layer 03 — AI CROSS-CHECK (Validation Only).

Responsibility: audit the rule-based recommendation. The AI reviews the
winner selected by the rule engine (with the top-3 alternatives as context)
and returns a structured validation verdict. AI NEVER ranks or re-scores
suppliers — it only validates the existing decision.

Public API:
    cross_check(winner: dict, alternatives: list[dict]) -> dict

Input dicts contain only descriptive fields (name, country, price, risk_level,
value_score, url, description). Risk/value scores are passed so the AI can
*audit* them, not replace them.

Output schema:
    {
      "is_valid":                bool,
      "issues":                  [str],   # concrete problems with the winner
      "risk_warnings":           [str],   # risks the rule engine may have missed
      "alternative_suggestions": [str],   # alt names + one-line rationale
      "confidence":              float,   # self-reported confidence in [0,1]
      "source":                  "ai" | "fallback",
    }

Determinism: temperature = 0 (via call_model).
Fallback:    any failure → "unable to validate" dict marked source=fallback.
"""

import json
from typing import Any

from engine.ai_engine import call_model, _find_first_json_object


# ---------------------------------------------------------------------------
# Fallback — surface as a permissive "no objection" so a silent AI outage
# does not flip a rule-approved winner into a red tier.
# ---------------------------------------------------------------------------

_FALLBACK: dict = {
    "is_valid":                True,
    "issues":                  [],
    "risk_warnings":           ["AI cross-check unavailable — rule-based result returned as-is."],
    "alternative_suggestions": [],
    "confidence":              0.0,
    "source":                  "fallback",
}


# ---------------------------------------------------------------------------
# Prompt — explicitly frames the AI as an auditor, not a decision-maker.
# ---------------------------------------------------------------------------

def _fmt_supplier_block(label: str, s: dict) -> str:
    price = s.get("price_display") or s.get("price_raw") or "unknown"
    return (
        f"{label}:\n"
        f"  Name:        {s.get('name','?')}\n"
        f"  Country:     {s.get('country','?')}\n"
        f"  Est. price:  {price}\n"
        f"  Risk level:  {s.get('risk_level','?')}\n"
        f"  Value score: {s.get('value_score','?')}/100\n"
        f"  URL:         {s.get('url','')}\n"
        f"  Description: {(s.get('description') or '')[:400]}\n"
    )


def _build_prompt(winner: dict, alternatives: list[dict]) -> str:
    alts_text = "\n".join(
        _fmt_supplier_block(f"Alternative {i + 1}", a)
        for i, a in enumerate(alternatives)
    ) or "  (no alternatives available)"

    return f"""You are an independent AUDITOR reviewing a rule-based ACP (aluminium composite panel) supplier recommendation.

A separate rule engine has already ranked suppliers and selected a winner. Your job is NOT to re-rank or re-score — it is to VALIDATE the rule engine's choice.

You must answer:
- Is the recommended winner a reasonable choice given its profile and the alternatives?
- Are there risks the rule engine may have missed?
- Is there an alternative from the top candidates that would clearly be safer or better? (Only flag this when it is clear-cut.)

STRICT RULES:
- Do NOT assign any score of your own.
- Do NOT re-rank suppliers.
- Your verdict is validation-only: is_valid true/false, plus reasons.
- Return STRICT JSON only — no markdown fences, no prose outside the JSON.

{_fmt_supplier_block("RULE-BASED WINNER", winner)}

TOP ALTERNATIVES (for context only, already ranked below the winner):
{alts_text}

Return EXACTLY this JSON shape:
{{
  "is_valid":                <true | false>,
  "issues":                  ["<concrete problem with the winner, if any>", ...],
  "risk_warnings":           ["<risk the rule engine may have missed>", ...],
  "alternative_suggestions": ["<alternative name — one-line rationale>", ...],
  "confidence":              <float in [0,1]>
}}

Constraints:
- Valid JSON — no trailing commas, no comments, no markdown fences.
- is_valid = false ONLY when the winner has a serious problem (fraud signal, high-risk country mismatch, fake contact info, etc.).
- If unsure, is_valid = true with warnings listed.
- Each array may be empty ([]).
- Keep each string concise (under 160 characters).
"""


# ---------------------------------------------------------------------------
# Parsing & validation
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


def _normalize(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("not an object")

    def _strlist(x) -> list[str]:
        if not isinstance(x, list):
            return []
        out = []
        for s in x:
            t = str(s).strip()
            if t:
                out.append(t[:300])
        return out

    is_valid = raw.get("is_valid")
    if not isinstance(is_valid, bool):
        raise ValueError("is_valid missing or not bool")

    confidence = raw.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "is_valid":                is_valid,
        "issues":                  _strlist(raw.get("issues")),
        "risk_warnings":           _strlist(raw.get("risk_warnings")),
        "alternative_suggestions": _strlist(raw.get("alternative_suggestions")),
        "confidence":              round(confidence, 3),
        "source":                  "ai",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cross_check(winner: dict, alternatives: list[dict]) -> dict:
    """
    Run one AI cross-check pass over the rule-based recommendation.

    The AI receives the winner and alternatives as descriptive data and
    returns a validation verdict. It must not produce a ranking or a score
    of its own.

    On any failure returns the permissive fallback (is_valid=True) so a
    rule-approved winner is never flipped to "red" by a silent AI outage.
    """
    prompt = _build_prompt(winner, alternatives)
    text   = call_model(prompt)

    if not text:
        return dict(_FALLBACK)

    try:
        parsed = _extract_json(text)
        return _normalize(parsed)
    except (ValueError, json.JSONDecodeError):
        return dict(_FALLBACK)
