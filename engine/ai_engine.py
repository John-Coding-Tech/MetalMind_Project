"""
engine/ai_engine.py

AI evaluation module — Phase 2.

Runs an INDEPENDENT supplier evaluation using the Gemma LLM.
Never consumes any expert-system score or output.

Public API:
    call_model(prompt: str, images: list[bytes] | None = None) -> str
        Isolated HTTP caller. Optionally passes JPEG image bytes as
        multimodal inline_data parts so Gemma 3 can "see" them.
        Swap this out to change the model.
    ai_evaluate(supplier: SupplierRecord) -> dict
        Returns {score, decision, risk_score, reasons, risk_flags}.

Determinism: temperature is fixed at 0.
Robustness : any failure (HTTP / JSON / schema) returns the fallback dict.

Environment variables:
    GEMMA_API_KEY   — required; Google AI Studio API key
    GEMMA_MODEL     — optional; defaults to "gemma-3-27b-it"
                      (set to "gemma-4-..." when Gemma 4 becomes available)
    GEMMA_TIMEOUT   — optional; HTTP timeout in seconds (default 60)
"""

import base64
import json
import os
import re
import time
from typing import Any

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

from modules.cleaner import SupplierRecord


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_GEMMA_API_KEY = os.getenv("GEMMA_API_KEY", "")
_GEMMA_MODEL   = os.getenv("GEMMA_MODEL", "gemma-3-27b-it")
_GEMMA_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    f"/{_GEMMA_MODEL}:generateContent"
)
_TIMEOUT      = int(os.getenv("GEMMA_TIMEOUT", "30"))
_MAX_ATTEMPTS = int(os.getenv("GEMMA_MAX_ATTEMPTS", "3"))

# Fallback returned on ANY failure path. The `source` marker lets downstream
# code distinguish a real AI rejection from a silent evaluation failure
# (so good suppliers don't get buried when the AI is simply unavailable).
_FALLBACK: dict = {
    "score":      0.0,
    "decision":   "not_recommended",
    "risk_score": 0.5,
    "reasons":    ["AI evaluation unavailable"],
    "risk_flags": ["evaluation_unavailable"],
    "source":     "fallback",
}


# ---------------------------------------------------------------------------
# Model caller — isolated so the model can be swapped without touching
# the rest of the evaluation logic.
# ---------------------------------------------------------------------------

def call_model(prompt: str, images: list[bytes] | None = None) -> str:
    """
    Send a prompt to the configured Gemma model and return raw text.
    Returns "" on any error (caller must treat this as failure).

    If `images` is provided, each entry is JPEG-encoded bytes; they are
    base64-embedded alongside the prompt via Gemini's `inline_data` part
    schema so Gemma 3 can read them with its vision capability.

    Retries up to GEMMA_MAX_ATTEMPTS times on network errors, HTTP 429,
    and HTTP 5xx, with exponential backoff (1s, 2s, 4s).
    """
    if not _REQUESTS_OK or not _GEMMA_API_KEY:
        return ""

    parts: list[dict] = [{"text": prompt}]
    for img_bytes in (images or []):
        if not img_bytes:
            continue
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data":      base64.b64encode(img_bytes).decode("ascii"),
            }
        })

    payload = {
        "contents": [
            {"parts": parts}
        ],
        "generationConfig": {
            "temperature":     0,    # deterministic
            "maxOutputTokens": 800,
        },
    }

    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = _requests.post(
                f"{_GEMMA_API_URL}?key={_GEMMA_API_KEY}",
                json=payload,
                timeout=_TIMEOUT,
            )

            # Retry on rate limit / transient server errors
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(2 ** attempt)
                    continue
                return ""

            if not resp.ok:
                return ""   # 4xx other than 429 — permanent client error

            data       = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return ""
            return parts[0].get("text", "").strip()

        except (_requests.exceptions.Timeout,
                _requests.exceptions.ConnectionError):
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            return ""
        except Exception:
            return ""

    return ""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_prompt(supplier: SupplierRecord) -> str:
    """Build a strict-JSON-only prompt from raw supplier data only."""
    price = "unknown" if supplier.price_est is None else f"${supplier.price_est:.2f}/sqm (USD)"
    # Cap raw_content to keep prompts a reasonable size
    raw = (supplier.raw_content or "")[:2000]

    return f"""You are an expert procurement analyst evaluating an ACP (aluminium composite panel) supplier.

Evaluate this supplier INDEPENDENTLY from any prior score and return STRICT JSON ONLY.
Do NOT include markdown fences, prose, or explanations outside the JSON object.

Supplier data:
- Name: {supplier.name}
- Country: {supplier.country}
- Estimated price: {price}
- Description: {supplier.description}
- Page content: {raw}

Return EXACTLY this JSON shape:
{{
  "score": <float in [0,1], higher = better overall value considering risk>,
  "decision": "recommended" or "not_recommended",
  "risk_score": <float in [0,1], higher = riskier>,
  "reasons": [<short strings explaining the score>],
  "risk_flags": [<short strings naming specific risk concerns>]
}}

Rules:
- Output MUST be valid JSON — no trailing commas, no comments, no markdown fences.
- "decision" MUST be exactly "recommended" or "not_recommended" (lowercase).
- Apply the Risk > Price principle: do NOT recommend high-risk suppliers even if cheap.
"""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _find_first_json_object(s: str) -> str | None:
    """
    Locate the first balanced {...} object in s and return its substring.

    Walks the string tracking brace depth and string-literal state so that
    braces inside JSON string values don't confuse the scanner. Returns
    None if no complete object is found.

    Replaces the old greedy `\\{.*\\}` regex which could match nested braces
    incorrectly and pick up fragments when the model emitted multiple JSON
    objects in its response.
    """
    depth     = 0
    start     = -1
    in_string = False
    escape    = False
    for i, c in enumerate(s):
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return s[start : i + 1]
            if depth < 0:
                # Unbalanced — reset scanner
                depth = 0
                start = -1
    return None


def _extract_json(text: str) -> dict:
    """
    Parse JSON from model output. Tolerates markdown fences and prose
    wrappers by extracting the first balanced {...} block as a fallback.
    Raises ValueError / JSONDecodeError on failure.
    """
    if not text:
        raise ValueError("empty response")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    obj = _find_first_json_object(cleaned)
    if not obj:
        raise ValueError("no JSON object found")
    return json.loads(obj)


def _validate_and_normalize(raw: Any) -> dict:
    """
    Coerce parsed JSON into the required schema; clamp numerics to [0,1].
    Raises ValueError if required fields are missing or wrong type.
    """
    if not isinstance(raw, dict):
        raise ValueError("not an object")

    score      = raw.get("score")
    risk_score = raw.get("risk_score")
    decision   = (raw.get("decision") or "").strip().lower()
    reasons    = raw.get("reasons", [])
    risk_flags = raw.get("risk_flags", [])

    if not isinstance(score, (int, float)):
        raise ValueError("score missing or not numeric")
    if not isinstance(risk_score, (int, float)):
        raise ValueError("risk_score missing or not numeric")
    if decision not in ("recommended", "not_recommended"):
        raise ValueError(f"decision invalid: {decision!r}")

    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    if not isinstance(risk_flags, list):
        risk_flags = [str(risk_flags)]

    return {
        "score":      round(max(0.0, min(1.0, float(score))),      4),
        "decision":   decision,
        "risk_score": round(max(0.0, min(1.0, float(risk_score))), 4),
        "reasons":    [str(r) for r in reasons],
        "risk_flags": [str(f) for f in risk_flags],
        "source":     "ai",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ai_evaluate(supplier: SupplierRecord) -> dict:
    """
    Evaluate a single supplier INDEPENDENTLY via the AI model.

    Input:  SupplierRecord (raw fields only — no expert scores used)
    Output: {score, decision, risk_score, reasons, risk_flags}

    On ANY failure (API, JSON, schema) returns the fallback dict.
    """
    prompt   = _build_prompt(supplier)
    response = call_model(prompt)

    if not response:
        return dict(_FALLBACK)

    try:
        parsed = _extract_json(response)
        return _validate_and_normalize(parsed)
    except (ValueError, json.JSONDecodeError):
        return dict(_FALLBACK)
