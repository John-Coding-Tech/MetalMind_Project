"""
modules/currency.py

Multi-currency detection and conversion for ACP supplier prices.

Supported currencies: USD, AUD, INR, CNY, EUR

Pipeline role:
    cleaner.py calls detect_currency() + to_usd() so that price_est is
    always in USD before it reaches the scoring/ranking pipeline.
    main.py calls get_rates() once per request to get the AUD display rate.

Exchange rates:
    Primary  : https://api.exchangerate-api.com/v4/latest/USD  (free, no key)
    Fallback : hardcoded approximate rates below
    Cache    : 24 hours in memory (_CACHE)
"""

import re
import time

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False


# ---------------------------------------------------------------------------
# Fallback rates  (1 USD = X units of currency)
# ---------------------------------------------------------------------------

_FALLBACK: dict[str, float] = {
    "USD": 1.0,
    "AUD": 1.58,
    "INR": 83.5,
    "CNY": 7.25,
    "EUR": 0.92,
}

# ---------------------------------------------------------------------------
# ACP price sanity bounds per currency  (min/sqm, max/sqm)
# Derived from USD range $3–$100/sqm converted at approximate rates.
# ---------------------------------------------------------------------------

_BOUNDS: dict[str, tuple[float, float]] = {
    "USD": (3.0,    100.0),
    "AUD": (5.0,    160.0),
    "INR": (250.0,  8500.0),
    "CNY": (20.0,   730.0),
    "EUR": (3.0,    95.0),
}

# Display symbols for raw price text
_SYMBOL: dict[str, str] = {
    "USD": "$",
    "AUD": "A$",
    "INR": "₹",
    "CNY": "¥",
    "EUR": "€",
}

# ---------------------------------------------------------------------------
# Detection patterns  (checked in order — most specific first)
# ---------------------------------------------------------------------------

# Each entry: (regex pattern, ISO code)
# Patterns are matched case-insensitively against the text window around a price.
_DETECT: list[tuple[str, str]] = [
    (r"a\$|aud\b",                          "AUD"),
    (r"₹|rs\.?\s*(?=[\d\s])|inr\b",        "INR"),
    (r"¥|rmb\b|cny\b|yuan\b",              "CNY"),
    (r"€|eur\b",                            "EUR"),
    (r"\$|usd\b",                           "USD"),
]

# ---------------------------------------------------------------------------
# Rate cache
# ---------------------------------------------------------------------------

_CACHE: dict = {"rates": None, "fetched_at": 0.0, "last_error_at": 0.0}
_TTL             = 86_400   # 24 hours — normal cache validity
_ERROR_BACKOFF   = 3_600    # 1 hour — wait before retrying after a failed fetch
_API_URL         = "https://api.exchangerate-api.com/v4/latest/USD"


def get_rates() -> dict[str, float]:
    """
    Return current exchange rates (USD base).
    Example: {"USD": 1.0, "AUD": 1.58, "INR": 83.5, ...}

    Fetched from exchangerate-api.com; cached 24 hours.

    Failure policy:
      - If the previous fetch succeeded and is <24h old, serve from cache.
      - If the previous fetch FAILED, wait 1 hour before retrying so a
        dead endpoint doesn't hammer us on every request.
      - If nothing has ever been fetched and fetch fails, use hardcoded
        _FALLBACK so the pipeline still produces reasonable numbers.
    """
    now = time.time()

    # Valid cache hit
    if _CACHE["rates"] and now - _CACHE["fetched_at"] < _TTL:
        return _CACHE["rates"]

    # Error backoff — don't retry if we just failed
    if now - _CACHE["last_error_at"] < _ERROR_BACKOFF:
        return _CACHE["rates"] or dict(_FALLBACK)

    if _REQUESTS_OK:
        try:
            resp = _requests.get(_API_URL, timeout=5)
            if resp.ok:
                raw   = resp.json().get("rates", {})
                rates = {k: float(raw[k]) for k in _FALLBACK if k in raw}
                rates["USD"] = 1.0
                _CACHE["rates"]         = rates
                _CACHE["fetched_at"]    = now
                _CACHE["last_error_at"] = 0.0
                return rates
        except Exception:
            pass

    # Record failure so we back off for the next hour
    _CACHE["last_error_at"] = now
    return _CACHE["rates"] or dict(_FALLBACK)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def detect_currency(text: str) -> str:
    """
    Detect the currency in a text snippet (e.g. near a price).
    Returns ISO code: "USD", "INR", "CNY", "EUR", "AUD".
    Defaults to "USD" if no symbol/code found.
    """
    lower = text.lower()
    for pattern, code in _DETECT:
        if re.search(pattern, lower):
            return code
    return "USD"


def price_bounds(currency: str) -> tuple[float, float]:
    """Return (min, max) sanity range for ACP panel prices in the given currency."""
    return _BOUNDS.get(currency, _BOUNDS["USD"])


def symbol(currency: str) -> str:
    """Return the display symbol for a currency code."""
    return _SYMBOL.get(currency, "$")


def to_usd(amount: float, currency: str, rates: dict[str, float]) -> float:
    """
    Convert an amount in any supported currency to USD.
    rates must be USD-base (1 USD = X currency).
    """
    if currency == "USD":
        return round(amount, 4)
    rate = rates.get(currency) or _FALLBACK.get(currency, 1.0)
    return round(amount / rate, 4)
