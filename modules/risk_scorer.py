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

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from modules.cleaner import SupplierRecord
from config import RISK_LEVEL_LOW_MAX, RISK_LEVEL_MEDIUM_MAX


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
# Dataset-level statistics (computed once in score_all, passed to score_risk)
# ---------------------------------------------------------------------------

@dataclass
class _DatasetStats:
    price_median: Optional[float]
    price_std:    Optional[float]


# ---------------------------------------------------------------------------
# Signal keyword lists
# ---------------------------------------------------------------------------

# Domains that are directories / aggregators, NOT manufacturer sites
_DIRECTORY_DOMAINS = [
    "alibaba.com", "aliexpress.com", "made-in-china.com", "indiamart.com",
    "tradeindia.com", "exportersindia.com", "tradeford.com", "ec21.com",
    "globalsources.com", "thomasnet.com", "yellowpages", "justdial",
    "exportHub.com", "dhgate.com", "tradekey.com",
]

# Credibility / manufacturing trust indicators
_TRUST_KEYWORDS = [
    # Quality certifications
    "iso 9001", "iso 14001", "iso 45001", "iso", "certified", "certification",
    "ce certified", "ce marking", "astm", "en 1396", "quality management",
    # Surface coating — specific to ACP manufacturing
    "pvdf coating", "pe coating", "pvdf", "polyester coating",
    "fluorocarbon", "nano coating",
    # Manufacturing capability
    "factory", "manufacturer", "manufacturing plant", "production line",
    "production capacity", "annual capacity", "sqm per", "workshop",
    "facility", "machinery", "automated",
    # Business establishment
    "established", "founded", "since 19", "since 20",
    "years of experience", "years in business", "decade",
    # Structural business signals
    "export", "exporter", "registered", "company profile",
    "about us", "our products", "our company",
    # Product-specific technical terms
    "core material", "thickness", "aluminium skin", "aluminum skin",
    "fire rated", "a2 grade", "b1 grade",
]

# Customer / verification signals
_REVIEW_KEYWORDS = [
    "review", "rating", "customer", "testimonial", "client", "feedback",
    "verified supplier", "trusted", "quality assurance", "buyer",
    "transaction", "repeat order", "satisfied", "recommend",
    "case study", "project reference", "completed project",
]

# Contact information signals
_CONTACT_KEYWORDS = [
    "contact us", "contact:", "get in touch", "reach us",
    "tel:", "phone:", "telephone:", "mobile:", "cell:",
    "email:", "e-mail:", "mail:", "enquiry",
    "address:", "location:", "office:", "head office",
    "whatsapp", "+91", "+86", ".com/contact",
]

# Phone/email pattern detection (more reliable than keyword matching)
_PHONE_PATTERN  = re.compile(r"\+?\d[\d\s\-\(\)]{7,}\d")
_EMAIL_PATTERN  = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


# ---------------------------------------------------------------------------
# Signal check functions
# ---------------------------------------------------------------------------

def _check_url_quality(url: str) -> tuple[float, list[str]]:
    """Check URL for directory listings or invalid domains."""
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
    """
    Check for credibility and technical specificity in the supplier content.
    Improved: more precise trust signals, separate contact detection,
    and product-specific technical content check.
    """
    reasons: list[str] = []
    penalty = 0.0
    combined = (description + " " + raw_content).lower()

    # Trust indicator count (weighted: ISO/cert keywords count more)
    cert_hits = sum(1 for k in ["iso 9001", "iso 14001", "certified", "certification",
                                 "pvdf", "astm", "ce marking"] if k in combined)
    general_trust_hits = sum(1 for k in _TRUST_KEYWORDS if k in combined)

    if general_trust_hits == 0:
        reasons.append("No trust indicators found (ISO, certified, factory, established, etc.)")
        penalty += 0.30
    elif cert_hits == 0 and general_trust_hits < 3:
        reasons.append("Limited trust indicators — no certifications or manufacturing details found")
        penalty += 0.12
    elif general_trust_hits < 2:
        reasons.append("Limited trust indicators in supplier description")
        penalty += 0.08

    # Customer / review / verification signals
    review_hits = sum(1 for k in _REVIEW_KEYWORDS if k in combined)
    if review_hits == 0:
        reasons.append("No customer reviews or verification signals found")
        penalty += 0.15

    # Description length — short = low online presence
    desc_len = len(description.strip())
    if desc_len < 60:
        reasons.append("Supplier description is very short — limited online presence")
        penalty += 0.20
    elif desc_len < 120:
        reasons.append("Supplier description is brief — low information confidence")
        penalty += 0.08

    return penalty, reasons


def _check_contact_info(description: str, raw_content: str) -> tuple[float, list[str]]:
    """
    Check for verifiable contact information.
    Real manufacturers almost always publish contact details.
    """
    combined = description + " " + raw_content

    has_keyword = any(k in combined.lower() for k in _CONTACT_KEYWORDS)
    has_phone   = bool(_PHONE_PATTERN.search(combined))
    has_email   = bool(_EMAIL_PATTERN.search(combined))

    if has_phone or has_email:
        # Actual phone or email found — strongest signal
        return 0.0, []
    if has_keyword:
        # Contact page referenced but no actual details
        return 0.05, []

    return 0.15, ["No contact information found (phone, email, or address) — low verifiability"]


def _check_country_consistency(url: str, claimed_country: str) -> tuple[float, list[str]]:
    """
    Flag mismatches between the claimed supplier country and the domain TLD.

    Examples:
      - claims India but URL is *.cn  → likely Chinese reseller
      - claims China but URL is *.in  → likely Indian reseller / intermediary

    A generic .com TLD is not penalised (too common for legitimate businesses).
    """
    if not url or not claimed_country:
        return 0.0, []

    lower = url.lower()
    # Extract TLD of the hostname only (ignore paths)
    host_end = lower.find("/", 8)          # skip past "https://"
    host     = lower[:host_end] if host_end > 0 else lower

    country_tld = {
        "India": ".in",
        "China": ".cn",
    }
    wrong_tld = {
        "India": ".cn",
        "China": ".in",
    }

    wrong = wrong_tld.get(claimed_country)
    right = country_tld.get(claimed_country)

    if wrong and host.endswith(wrong):
        return 0.15, [
            f"Domain ends in {wrong} but supplier is listed as {claimed_country} "
            f"— possible reseller or intermediary"
        ]

    # Positive signal (no penalty, just recorded implicitly via no reason added)
    if right and host.endswith(right):
        return 0.0, []

    return 0.0, []


def _check_price_reasonableness(
    price_est: Optional[float],
    stats: Optional[_DatasetStats],
) -> tuple[float, list[str]]:
    """
    Flag suspicious prices.

    Primary method: dataset-relative outlier detection.
      A price more than 2 standard deviations below the dataset median
      is flagged as suspiciously low.

    Fallback (no dataset stats available): absolute floor of $6/sqm USD.
    """
    if price_est is None:
        return 0.15, ["No price found — cannot verify pricing"]

    if stats and stats.price_median is not None and stats.price_std is not None:
        # Dataset-relative check
        lower_bound = stats.price_median - 2.0 * stats.price_std
        floor = max(lower_bound, 3.0)   # never flag anything above $3 absolute minimum
        if price_est < floor:
            return 0.25, [
                f"Price ${price_est:.2f}/sqm is significantly below dataset average "
                f"(median ${stats.price_median:.2f}/sqm) — verify authenticity"
            ]
    else:
        # Fallback: absolute threshold
        if price_est < 6.0:
            return 0.25, [f"Price ${price_est:.2f}/sqm is unusually low — verify authenticity"]

    return 0.0, []


def score_to_risk_level(risk_score: float) -> RiskLevel:
    """
    Map a 0-1 risk_score to a RiskLevel using shared cutoffs from config.

    Used by rule-based scoring here AND by the AI-only pipeline so that
    the same numeric risk score always produces the same label regardless
    of which pipeline generated it.
    """
    if risk_score < RISK_LEVEL_LOW_MAX:
        return RiskLevel.LOW
    if risk_score < RISK_LEVEL_MEDIUM_MAX:
        return RiskLevel.MEDIUM
    return RiskLevel.HIGH


# Legacy alias for backward compatibility
_penalty_to_risk_level = score_to_risk_level


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_risk(
    record: SupplierRecord,
    stats: Optional[_DatasetStats] = None,
) -> ScoredSupplier:
    """
    Assign a risk level and numeric risk score to a single supplier.

    Args:
        record: Cleaned supplier record from cleaner.py
        stats:  Optional dataset-level price statistics for relative outlier detection.
                Computed and passed by score_all(); None in standalone use.

    Returns:
        ScoredSupplier with risk_level, risk_score (0–1), and human-readable reasons.
    """
    all_reasons: list[str] = []
    total_penalty = 0.0

    # 1. URL quality
    p, r = _check_url_quality(record.url)
    total_penalty += p
    all_reasons.extend(r)

    # 2. Description quality (trust indicators, length, reviews)
    p, r = _check_description_quality(record.description, record.raw_content)
    total_penalty += p
    all_reasons.extend(r)

    # 3. Contact information
    p, r = _check_contact_info(record.description, record.raw_content)
    total_penalty += p
    all_reasons.extend(r)

    # 4. Price reasonableness (dataset-relative when stats available)
    p, r = _check_price_reasonableness(record.price_est, stats)
    total_penalty += p
    all_reasons.extend(r)

    # 5. Country / domain consistency — mismatch suggests reseller/intermediary
    p, r = _check_country_consistency(record.url, record.country)
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
    """
    Score risk for every supplier in the list.

    Computes dataset-level price statistics first, then passes them to
    each individual score_risk() call for relative outlier detection.
    """
    # Compute dataset price stats (only from suppliers that have a price)
    known_prices = [r.price_est for r in records if r.price_est is not None]

    if len(known_prices) >= 3:
        sorted_p = sorted(known_prices)
        n        = len(sorted_p)
        # True median: mean of two middle values when n is even
        median   = sorted_p[n // 2] if n % 2 else (sorted_p[n // 2 - 1] + sorted_p[n // 2]) / 2
        mean     = sum(known_prices) / n
        std      = math.sqrt(sum((p - mean) ** 2 for p in known_prices) / n)
        stats    = _DatasetStats(price_median=median, price_std=std)
    else:
        stats = _DatasetStats(price_median=None, price_std=None)

    return [score_risk(r, stats) for r in records]
