"""
FX conversion via Frankfurter (https://www.frankfurter.dev).

- Free, no auth needed
- ECB reference rates
- Supports historical dates — so a receipt from March uses March's rate,
  not today's. This matters for accurate reporting.

We cache rates in-memory per process; for a personal bot this is plenty.
Modal containers are short-lived so the cache resets often, which is fine.
"""

import httpx
from datetime import datetime, date

BASE_URL = "https://api.frankfurter.dev/v1"

# Map of common currency symbols/codes the AI might output → ISO 4217
NORMALIZE = {
    "$": "USD", "US$": "USD", "USD": "USD",
    "S$": "SGD", "SG$": "SGD", "SGD": "SGD",
    "£": "GBP", "GBP": "GBP",
    "€": "EUR", "EUR": "EUR",
    "¥": "JPY", "JPY": "JPY",
    "HK$": "HKD", "HKD": "HKD",
    "A$": "AUD", "AU$": "AUD", "AUD": "AUD",
    "C$": "CAD", "CA$": "CAD", "CAD": "CAD",
}

_cache: dict[tuple[str, str, str], float] = {}


def normalize_code(currency_raw: str | None) -> str:
    """Turn whatever the AI extracted into a clean ISO code."""
    if not currency_raw:
        return "USD"
    code = NORMALIZE.get(currency_raw.strip().upper()) or \
           NORMALIZE.get(currency_raw.strip()) or \
           currency_raw.strip().upper()
    # If it's not 3 letters by now, fall back to USD
    return code if len(code) == 3 and code.isalpha() else "USD"


def get_rate(from_currency: str, to_currency: str, on_date: str) -> float:
    """Get FX rate for a date. Returns 1.0 if same currency."""
    from_currency = normalize_code(from_currency)
    to_currency = normalize_code(to_currency)
    if from_currency == to_currency:
        return 1.0

    key = (from_currency, to_currency, on_date)
    if key in _cache:
        return _cache[key]

    # Frankfurter doesn't have data for future dates or today (publishes next day)
    # Clamp to yesterday at latest
    today_str = date.today().isoformat()
    fetch_date = on_date if on_date < today_str else today_str

    r = httpx.get(
        f"{BASE_URL}/{fetch_date}",
        params={"base": from_currency, "symbols": to_currency},
        timeout=10,
    )
    r.raise_for_status()
    rate = float(r.json()["rates"][to_currency])
    _cache[key] = rate
    return rate


def to_usd(amount: float, currency: str, on_date: str) -> float:
    """Convenience: convert any amount to USD on a given date."""
    if not amount:
        return 0.0
    try:
        return round(amount * get_rate(currency, "USD", on_date), 2)
    except Exception:
        # If FX lookup fails, fall back to no conversion. Better than crashing.
        return amount if normalize_code(currency) == "USD" else 0.0
