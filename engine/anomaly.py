"""
engine/anomaly.py

Layer 04 — Anomaly Detection.

Detects abnormal or suspicious patterns in a supplier's data. Pure
rule-based — no AI involved. Runs after the rule engine has produced
scores so it can cross-reference score vs. data quality.

Public API:
    dataset_median(suppliers) -> float | None
    detect_anomalies(supplier, median) -> dict

Output schema:
    {
      "anomalies": [str],                          # short flag descriptions
      "severity":  "none" | "low" | "medium" | "high",
    }

Severity mapping:
    high   — at least one critical flag (suspiciously low price, etc.)
    medium — at least one moderate flag OR three+ flags total
    low    — one or two minor flags only
    none   — nothing abnormal
"""

from modules.value_scorer import ValuedSupplier


# ---------------------------------------------------------------------------
# Thresholds — conservative defaults, tunable via future config hooks.
# ---------------------------------------------------------------------------

_LOW_PRICE_RATIO   = 0.5    # price below 50% of dataset median → suspicious
_HIGH_PRICE_RATIO  = 3.0    # price above 3× median → unusually high (info)
_MIN_DATASET_N     = 3      # need >= 3 known prices for a meaningful median
_SHORT_DESC_LEN    = 60     # descriptions below this length = minimal data
_HIGH_VALUE_CUT    = 0.70   # value_score (0-1) considered "high"
_MULTI_RISK_COUNT  = 2      # >= this many rule-based risk reasons counts as "multiple"


# ---------------------------------------------------------------------------
# Dataset statistic
# ---------------------------------------------------------------------------

def dataset_median(suppliers: list[ValuedSupplier]) -> float | None:
    """
    Median of known per-supplier prices in the ranked dataset.

    Returns None when fewer than _MIN_DATASET_N suppliers have a known price —
    the median is not representative at that point and price-anomaly checks
    are skipped instead of producing noise.
    """
    prices = sorted(
        s.scored.record.price_est
        for s in suppliers
        if s.scored.record.price_est is not None
    )
    if len(prices) < _MIN_DATASET_N:
        return None
    mid = len(prices) // 2
    if len(prices) % 2:
        return prices[mid]
    return (prices[mid - 1] + prices[mid]) / 2


# ---------------------------------------------------------------------------
# Severity aggregation
# ---------------------------------------------------------------------------

def _severity(flags: list[tuple[str, str]]) -> str:
    """Roll up individual flag weights into a single severity bucket."""
    if not flags:
        return "none"
    weights = [w for _, w in flags]
    if "high" in weights:
        return "high"
    if "medium" in weights or len(weights) >= 3:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_anomalies(v: ValuedSupplier, median: float | None) -> dict:
    """
    Run all detectors against one supplier, return the aggregated result.

    `median` is the dataset-wide price median (from dataset_median()). Pass
    None to skip price-anomaly checks when the dataset is too small.
    """
    flags: list[tuple[str, str]] = []   # (description, severity_weight)
    rec = v.scored.record

    # --- Price anomalies ----------------------------------------------------
    price = rec.price_est
    if price is None:
        flags.append(("No price data extracted from supplier page.", "medium"))
    elif median is not None and median > 0:
        if price < median * _LOW_PRICE_RATIO:
            flags.append((
                f"Price is suspiciously low vs dataset median "
                f"(${price:.2f} vs ${median:.2f}/sqm).",
                "high",
            ))
        elif price > median * _HIGH_PRICE_RATIO:
            flags.append((
                f"Price is unusually high vs dataset median "
                f"(${price:.2f} vs ${median:.2f}/sqm).",
                "low",
            ))

    # --- Missing critical info ---------------------------------------------
    desc = (rec.description or "").strip()
    if len(desc) < _SHORT_DESC_LEN:
        flags.append(("Supplier description is very short — limited page content.", "low"))

    if not rec.url or rec.url.strip() in ("", "#"):
        flags.append(("No supplier URL recorded.", "medium"))

    # --- Score vs. data quality --------------------------------------------
    # High value but multiple risk signals → rule engine may have over-credited.
    if v.value_score >= _HIGH_VALUE_CUT and len(v.scored.risk_reasons) >= _MULTI_RISK_COUNT:
        flags.append((
            "High value score despite multiple risk signals — verify data quality.",
            "medium",
        ))

    return {
        "anomalies": [f for f, _ in flags],
        "severity":  _severity(flags),
    }
