"""
modules/risk_scorer.py

Risk scoring module.

Criteria (from rules/risk_rules.md):
- Risk increases if:
    * No website / suspicious URL (directory-only listing)
    * No clear supplier description or contact information
    * No reviews or online presence indicators
    * Unusually low or inconsistent pricing
- Risk levels: Low | Medium | High
- High-risk suppliers rank lower even if their price is low.

Risk > Price — this is the most critical rule.
"""

from dataclasses import dataclass
from enum import Enum

from modules.cleaner import SupplierRecord


# ---------------------------------------------------------------------------
# Risk level enum
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW    = "Low"
    MEDIUM = "Medium"
    HIGH   = "High"


# ---------------------------------------------------------------------------
# Scored supplier model
# ---------------------------------------------------------------------------

@dataclass
class ScoredSupplier:
    record:        SupplierRecord
    risk_level:    RiskLevel
    risk_score:    float          # 0.0 (no risk) → 1.0 (maximum risk)
    risk_reasons:  list[str]      # human-readable reasons


# ---------------------------------------------------------------------------
# Risk signal checks
# ---------------------------------------------------------------------------

# Domains that are directories / aggregators, NOT manufacturer sites
_DIRECTORY_DOMAINS = [
    "alibaba.com", "aliexpress.com", "made-in-china.com", "indiamart.com",
    "tradeindia.com", "exportersindia.com", "tradeford.com", "ec21.com",
    "globalsources.com", "thomasnet.com", "yellowpages", "justdial",
]

_TRUST_KEYWORDS = [
    "iso", "certified", "established", "founded", "factory", "manufacturer",
    "export", "sqm", "panel", "about us", "contact", "our products",
]

_REVIEW_KEYWORDS = [
    "review", "rating", "customer", "testimonial", "client", "feedback",
    "verified", "trusted", "quality assurance",
]

_SUSPICIOUS_PRICE_THRESHOLD = 6.0   # below this per sqm is suspiciously cheap


def _check_url_quality(url: str) -> tuple[float, list[str]]:
    """Check URL for directory listings or missing domains."""
    reasons: list[str] = []
    penalty = 0.0

    if not url or url == "":
        reasons.append("No URL provided")
        penalty += 0.4
        return penalty, reasons

    lower = url.lower()
    for d in _DIRECTORY_DOMAINS:
        if d in lower:
            reasons.append(f"Listed on aggregator ({d}), not a direct manufacturer site")
            penalty += 0.25
            break

    if not any(lower.startswith(p) for p in ["http://", "https://"]):
        reasons.append("URL does not appear valid")
        penalty += 0.2

    return penalty, reasons


def _check_description_quality(description: str, raw_content: str) -> tuple[float, list[str]]:
    """Check if the description contains enough credible supplier information."""
    reasons: list[str] = []
    penalty = 0.0
    combined = (description + " " + raw_content).lower()

    # Check for trust indicators
    trust_hits = sum(1 for k in _TRUST_KEYWORDS if k in combined)
    if trust_hits == 0:
        reasons.append("No trust indicators found (ISO, certified, established, etc.)")
        penalty += 0.3
    elif trust_hits < 2:
        reasons.append("Limited trust indicators in supplier description")
        penalty += 0.1

    # Check for review/rating signals
    review_hits = sum(1 for k in _REVIEW_KEYWORDS if k in combined)
    if review_hits == 0:
        reasons.append("No customer reviews or verification signals found")
        penalty += 0.15

    # Check description length — very short means low online presence
    if len(description.strip()) < 80:
        reasons.append("Supplier description is very short — limited online presence")
        penalty += 0.2

    return penalty, reasons


def _check_price_reasonableness(price_est: float | None) -> tuple[float, list[str]]:
    """Flag suspiciously low prices."""
    if price_est is None:
        return 0.15, ["No price found — cannot verify pricing"]
    if price_est < _SUSPICIOUS_PRICE_THRESHOLD:
        return 0.25, [f"Price ${price_est}/sqm is unusually low — verify authenticity"]
    return 0.0, []


def _penalty_to_risk_level(penalty: float) -> RiskLevel:
    if penalty < 0.25:
        return RiskLevel.LOW
    if penalty < 0.55:
        return RiskLevel.MEDIUM
    return RiskLevel.HIGH


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_risk(record: SupplierRecord) -> ScoredSupplier:
    """
    Assign a risk level and numeric risk score to a single supplier.

    Returns a ScoredSupplier with risk_level, risk_score (0–1), and reasons.
    """
    all_reasons: list[str] = []
    total_penalty = 0.0

    # 1. URL quality
    p, r = _check_url_quality(record.url)
    total_penalty += p
    all_reasons.extend(r)

    # 2. Description quality
    p, r = _check_description_quality(record.description, record.raw_content)
    total_penalty += p
    all_reasons.extend(r)

    # 3. Price reasonableness
    p, r = _check_price_reasonableness(record.price_est)
    total_penalty += p
    all_reasons.extend(r)

    # Cap at 1.0
    risk_score = min(round(total_penalty, 3), 1.0)
    risk_level = _penalty_to_risk_level(risk_score)

    return ScoredSupplier(
        record=record,
        risk_level=risk_level,
        risk_score=risk_score,
        risk_reasons=all_reasons if all_reasons else ["No risk signals detected"],
    )


def score_all(records: list[SupplierRecord]) -> list[ScoredSupplier]:
    """Score risk for every supplier in the list."""
    return [score_risk(r) for r in records]
