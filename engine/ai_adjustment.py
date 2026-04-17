"""
engine/ai_adjustment.py

Computes bounded, RELATIVE score adjustments by comparing suppliers against
each other within the same result set.

Key design:
  - Absolute evaluation (old approach) gave everyone ~-7 because it judged
    each supplier in isolation. Relative evaluation creates SPREAD by
    comparing suppliers head-to-head on multiple dimensions.
  - Each dimension produces a score that differentiates: a real brand name
    scores higher than a generic listing; a manufacturer beats a trader;
    reasonable pricing beats a suspicious outlier.
  - The per-supplier raw score is then mapped to an adjustment that MUST
    create meaningful ranking differences (at least 30-50% different).

Public API:
    compute_relative(ranked, insights, median) -> dict[name, adj_dict]
    from_crosscheck(validation)                -> adj_dict
    apply(base_score, adj_dict)                -> float

Output schema per supplier:
    {"adjustment": int [-15..+10], "reason": str, "confidence": float}
"""

import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

_ADJ_MIN = -15
_ADJ_MAX = +10
_CONF_THRESHOLD = 0.5
_ZERO = {"adjustment": 0, "reason": "", "confidence": 0.0}


def _clamp_adj(raw: float) -> int:
    return max(_ADJ_MIN, min(_ADJ_MAX, round(raw)))


# ---------------------------------------------------------------------------
# Dimension scorers — each returns (score: float, reason: str | None)
# A positive score is good; negative is bad.
# ---------------------------------------------------------------------------

_GENERIC_NAME_RE = [
    re.compile(r"^top\s+\d+", re.I),
    re.compile(r"^best\s+\d+", re.I),
    re.compile(r"^\d+\s+best\b", re.I),
    re.compile(r"^about\b", re.I),
    re.compile(r"^quality\s+", re.I),
    re.compile(r"^china\s+acp", re.I),
    re.compile(r"^chinese\s+", re.I),
    re.compile(r"manufacturer", re.I),
    re.compile(r"supplier", re.I),
    re.compile(r"wholesale", re.I),
]

_MARKETPLACE_DOMAINS = [
    "made-in-china.com", "alibaba.com", "aliexpress.com",
    "indiamart.com", "tradeindia.com", "globalsources.com",
    "ec21.com", "tradekey.com",
]


def _dim_name_quality(name: str, url: str) -> tuple[float, str | None]:
    """Real brand name vs generic listing title."""
    if any(p.search(name) for p in _GENERIC_NAME_RE):
        return (-5.0, f"Generic listing name: \"{name[:40]}\"")
    if len(name) <= 4:
        return (-3.0, "Very short name")
    return (+3.0, "Real company brand name")


def _dim_supplier_type(supplier_type: str) -> tuple[float, str | None]:
    """Manufacturer > reseller > trader > unknown."""
    if supplier_type == "manufacturer":
        return (+5.0, "Identified as manufacturer")
    if supplier_type == "reseller":
        return (+1.0, None)
    if supplier_type == "trader":
        return (-2.0, "Trading company, not manufacturer")
    return (-3.0, "Supplier type unknown")


def _dim_marketplace(url: str) -> tuple[float, str | None]:
    """Marketplace pages vs direct manufacturer websites."""
    url_low = url.lower()
    for domain in _MARKETPLACE_DOMAINS:
        if domain in url_low:
            return (-5.0, f"Marketplace page ({domain})")
    return (0.0, None)


def _dim_price_position(price: float | None, median: float | None) -> tuple[float, str | None]:
    """Reasonable pricing vs suspicious outliers."""
    if price is None:
        return (-2.0, "No price data")
    if median is None or median <= 0:
        return (0.0, None)

    ratio = price / median
    if 0.5 <= ratio <= 2.0:
        return (+3.0, "Pricing consistent with market")
    if ratio < 0.3:
        return (-5.0, f"Price suspiciously low ({ratio:.1f}x median)")
    if ratio < 0.5:
        return (-3.0, f"Price below market ({ratio:.1f}x median)")
    if ratio > 3.0:
        return (-4.0, f"Price significantly above market ({ratio:.1f}x median)")
    return (-1.0, None)


def _dim_content_quality(signals: dict) -> tuple[float, str | None]:
    """Sum of quality signals from cleaner extraction."""
    if not signals:
        return (-2.0, "No quality signals extracted")

    score = 0.0
    if signals.get("has_certification"):
        score += 2.0
    if signals.get("has_contact_info"):
        score += 1.5
    if signals.get("is_manufacturer"):
        score += 1.5
    if signals.get("has_reviews"):
        score += 1.0
    length = signals.get("content_length", 0)
    if length > 1000:
        score += 1.0
    elif length < 200:
        score -= 2.0

    if score >= 5:
        return (score, "Strong quality signals (certs, contact, reviews)")
    if score <= 0:
        return (score, "Weak quality signals")
    return (score, None)


def _dim_ai_insight(insight: dict | None) -> tuple[float, str | None]:
    """Net balance of AI-identified strengths vs risks."""
    if not insight or insight.get("source") != "ai":
        return (0.0, None)

    strengths = len(insight.get("key_strengths") or [])
    risks     = len(insight.get("key_risks") or [])
    hidden    = len(insight.get("hidden_signals") or [])
    confidence = float(insight.get("confidence") or 0.5)

    net = (strengths * 1.5) - (risks * 2.0) - (hidden * 1.5)
    scaled = net * confidence

    if scaled >= 2.0:
        reason = (insight.get("key_strengths") or ["Strong AI assessment"])[0]
        return (min(scaled, 5.0), reason)
    if scaled <= -2.0:
        reason = (insight.get("key_risks") or insight.get("hidden_signals") or ["Weak AI assessment"])[0]
        return (max(scaled, -5.0), reason)
    return (0.0, None)


# ---------------------------------------------------------------------------
# RELATIVE adjustment — compares suppliers against each other
# ---------------------------------------------------------------------------

def compute_relative(
    ranked: list,
    insights: dict[str, dict],
    median: float | None,
) -> dict[str, dict]:
    """
    Compute per-supplier adjustments by RELATIVE comparison across the dataset.

    Each supplier is scored on 6 dimensions, producing a raw differentiation
    score. The scores are then spread-mapped: the best supplier gets up to
    +10, the worst gets down to -15, and the middle gets small or zero.

    This guarantees ranking spread — suppliers with strong signals rise,
    weak ones fall, regardless of absolute AI sentiment.

    Args:
        ranked:   list[ValuedSupplier] from the rule engine
        insights: {supplier_name: ai_insight_dict}
        median:   dataset price median (from anomaly.dataset_median)

    Returns:
        {supplier_name: {"adjustment": int, "reason": str, "confidence": float}}
    """
    if not ranked:
        return {}

    # --- Phase 1: score each supplier on all dimensions ---
    entries: list[dict] = []
    for v in ranked:
        rec = v.scored.record
        nm  = rec.name
        ins = insights.get(nm)

        dims: list[tuple[float, str | None]] = [
            _dim_name_quality(nm, rec.url),
            _dim_supplier_type(rec.supplier_type),
            _dim_marketplace(rec.url),
            _dim_price_position(rec.price_est, median),
            _dim_content_quality(rec.signals),
            _dim_ai_insight(ins),
        ]

        raw_score  = sum(d[0] for d in dims)
        top_reason = next((d[1] for d in dims if d[1] and d[0] == max(d[0] for d in dims if d[1])), None)
        neg_reason = next((d[1] for d in dims if d[1] and d[0] == min(d[0] for d in dims if d[1])), None)
        confidence = float((ins or {}).get("confidence", 0.5))

        entries.append({
            "name":       nm,
            "raw":        raw_score,
            "top_reason": top_reason,
            "neg_reason": neg_reason,
            "confidence": confidence,
        })

    # --- Phase 2: spread-map raw scores to adjustment range ---
    raws   = [e["raw"] for e in entries]
    lo, hi = min(raws), max(raws)
    spread = hi - lo if hi != lo else 1.0

    result: dict[str, dict] = {}
    for e in entries:
        # Normalise to [0, 1] within the dataset
        t = (e["raw"] - lo) / spread

        # Map: 0.0 → _ADJ_MIN, 0.5 → 0, 1.0 → _ADJ_MAX
        if t >= 0.5:
            adj_raw = (t - 0.5) * 2 * _ADJ_MAX
        else:
            adj_raw = (0.5 - t) * 2 * _ADJ_MIN

        adj = _clamp_adj(adj_raw)

        # Pick reason: positive adjustments use top_reason, negative use neg_reason
        if adj > 0:
            reason = e["top_reason"] or "Stronger relative signals"
        elif adj < 0:
            reason = e["neg_reason"] or "Weaker relative signals"
        else:
            reason = ""

        if adj == 0 or e["confidence"] < _CONF_THRESHOLD:
            result[e["name"]] = dict(_ZERO)
        else:
            result[e["name"]] = {
                "adjustment": adj,
                "reason":     reason,
                "confidence": e["confidence"],
            }

        _log.debug(
            "relative adj: %-40s raw=%+.1f t=%.2f adj=%+d  %s",
            e["name"][:40], e["raw"], t, adj, reason[:60],
        )

    return result


# ---------------------------------------------------------------------------
# Cross-check adjustment (Mode 03) — unchanged, operates on single winner
# ---------------------------------------------------------------------------

def from_crosscheck(validation: dict | None) -> dict:
    """
    Derive a score adjustment from an AI Cross-Check (validation) payload.

    This is NOT relative — it applies only to the cross-checked winner.
    """
    if not validation or validation.get("source") != "ai":
        return dict(_ZERO)

    confidence = float(validation.get("confidence") or 0.0)
    if confidence < _CONF_THRESHOLD:
        return dict(_ZERO)

    is_valid = validation.get("is_valid")
    issues   = validation.get("issues") or []
    warnings = validation.get("risk_warnings") or []

    if is_valid is False:
        reason = issues[0] if issues else "AI auditor rejects this supplier"
        return {"adjustment": _ADJ_MIN, "reason": reason, "confidence": confidence}

    raw = 0.0
    raw -= min(len(issues) * 5, 15)
    raw -= min(len(warnings) * 3, 9)

    if is_valid and not issues and not warnings:
        raw += 5

    adj = _clamp_adj(raw * confidence)
    if adj == 0:
        return dict(_ZERO)

    if adj < 0:
        reason = (issues or warnings or [""])[0]
    else:
        reason = "AI auditor fully validates this supplier"

    return {"adjustment": adj, "reason": reason, "confidence": confidence}


def apply(base_score: float, adj: dict) -> float:
    """final_score = clamp(base_score + adjustment, 0, 100)."""
    return max(0.0, min(100.0, round(base_score + adj.get("adjustment", 0), 1)))
