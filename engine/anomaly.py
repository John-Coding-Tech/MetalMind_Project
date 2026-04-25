"""
engine/anomaly.py

Layer 04 — Anomaly Detection (multi-metal aware).

Detects abnormal or suspicious patterns in a supplier's data. Pure
rule-based — no AI involved. Runs after the rule engine has produced
scores so it can cross-reference score vs. data quality.

Multi-metal change:
    Price-anomaly thresholds are now applied *per (category, canonical_unit)
    bucket* — the same buckets used by value_scorer. A $5/sqm ACP and a
    $3000/ton steel are no longer compared against a single global median
    (which would always look one of them looks "suspiciously low").

Public API:
    dataset_medians(suppliers) -> dict[(category, canonical_unit), float]
    dataset_median(suppliers)  -> float | None     # legacy global median
    detect_anomalies(supplier, medians_by_bucket)  -> dict

Output schema:
    {
      "anomalies": [str],                           # short flag descriptions
      "severity":  "none" | "low" | "medium" | "high",
    }
"""

from modules.value_scorer import ValuedSupplier, _bucket_for, _convert_price


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_LOW_PRICE_RATIO   = 0.5    # price below 50% of bucket median -> suspicious
_HIGH_PRICE_RATIO  = 3.0    # price above 3x bucket median -> unusually high (info)
_MIN_BUCKET_N      = 3      # need >= 3 known prices in a bucket for a meaningful median
_SHORT_DESC_LEN    = 60
_HIGH_VALUE_CUT    = 0.70
_MULTI_RISK_COUNT  = 2


# ---------------------------------------------------------------------------
# Dataset statistics
# ---------------------------------------------------------------------------

def _median(values: list[float]) -> float:
    s   = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def dataset_medians(suppliers: list[ValuedSupplier]) -> dict[tuple[str, str], float]:
    """
    Compute the price median for every (category, canonical_unit) bucket
    that has at least _MIN_BUCKET_N priced suppliers. Buckets with fewer
    samples are omitted — the price-anomaly check is skipped for those
    rather than flagging spurious "low/high" against a thin sample.

    Prices are converted into the bucket's canonical unit before computing
    the median (so /ton and /kg quotes for the same metal are pooled).
    """
    by_bucket: dict[tuple[str, str], list[float]] = {}

    for v in suppliers:
        rec = v.scored.record
        if rec.price_est is None:
            continue
        cat  = getattr(rec, "category",   "unknown") or "unknown"
        unit = getattr(rec, "price_unit", "unknown") or "unknown"
        key  = _bucket_for(cat, unit)

        canon_unit = key[1]
        converted  = _convert_price(rec.price_est, unit, canon_unit)
        price      = converted if converted is not None else rec.price_est
        by_bucket.setdefault(key, []).append(price)

    return {k: _median(vs) for k, vs in by_bucket.items() if len(vs) >= _MIN_BUCKET_N}


def dataset_median(suppliers: list[ValuedSupplier]) -> float | None:
    """
    Legacy global price median across the dataset, kept for callers (e.g.
    ai_adjustment) that score on a single dataset-wide ratio rather than
    per-bucket. Returns None when fewer than _MIN_BUCKET_N priced suppliers
    exist in total.
    """
    prices = [
        s.scored.record.price_est
        for s in suppliers
        if s.scored.record.price_est is not None
    ]
    if len(prices) < _MIN_BUCKET_N:
        return None
    return _median(prices)


# ---------------------------------------------------------------------------
# Severity aggregation
# ---------------------------------------------------------------------------

def _severity(flags: list[tuple[str, str]]) -> str:
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

def detect_anomalies(
    v: ValuedSupplier,
    medians_by_bucket: dict[tuple[str, str], float] | None = None,
) -> dict:
    """
    Run all detectors against one supplier, return the aggregated result.

    `medians_by_bucket` is the per-bucket median map from dataset_medians().
    Pass None or {} to skip price-anomaly checks (e.g. when no bucket reached
    _MIN_BUCKET_N samples).
    """
    flags: list[tuple[str, str]] = []
    rec = v.scored.record

    # --- Price anomalies (per-bucket) --------------------------------------
    price = rec.price_est
    if price is None:
        flags.append(("No price data extracted from supplier page.", "medium"))
    elif medians_by_bucket:
        cat  = getattr(rec, "category",   "unknown") or "unknown"
        unit = getattr(rec, "price_unit", "unknown") or "unknown"
        key  = _bucket_for(cat, unit)
        median = medians_by_bucket.get(key)
        if median is not None and median > 0:
            # Compare in canonical-unit space to match how the median was computed.
            canon_unit = key[1]
            price_cmp  = _convert_price(price, unit, canon_unit)
            if price_cmp is None:
                price_cmp = price
            ratio = price_cmp / median
            if ratio < _LOW_PRICE_RATIO:
                flags.append((
                    f"Price is suspiciously low vs {cat}/{canon_unit} median "
                    f"(${price_cmp:.2f} vs ${median:.2f}/{canon_unit}).",
                    "high",
                ))
            elif ratio > _HIGH_PRICE_RATIO:
                flags.append((
                    f"Price is unusually high vs {cat}/{canon_unit} median "
                    f"(${price_cmp:.2f} vs ${median:.2f}/{canon_unit}).",
                    "low",
                ))

    # --- Missing critical info ---------------------------------------------
    desc = (rec.description or "").strip()
    if len(desc) < _SHORT_DESC_LEN:
        flags.append(("Supplier description is very short — limited page content.", "low"))

    if not rec.url or rec.url.strip() in ("", "#"):
        flags.append(("No supplier URL recorded.", "medium"))

    # --- Score vs. data quality --------------------------------------------
    if v.value_score >= _HIGH_VALUE_CUT and len(v.scored.risk_reasons) >= _MULTI_RISK_COUNT:
        flags.append((
            "High value score despite multiple risk signals — verify data quality.",
            "medium",
        ))

    return {
        "anomalies": [f for f, _ in flags],
        "severity":  _severity(flags),
    }
