"""
Country → timezone → meal-time inference.

When the user types '150 tatiana' we want to know:
- What country is Tatiana in? (NYC, so USA)
- What time is it there right now?
- Given the local time, what meal is this likely to be?

This stays simple — one canonical timezone per country (we don't need to be
precise about which US timezone for meal labels).
"""

from datetime import datetime, timezone, timedelta

# Country → fixed UTC offset (hours). Not DST-aware, but close enough for
# "is it lunch or dinner" decisions. Pick the major business-center offset.
COUNTRY_OFFSETS = {
    "USA": -5,           # ET; close enough for meal heuristics
    "Singapore": 8,
    "UK": 0,
    "Eurozone": 1,
    "France": 1,
    "Germany": 1,
    "Italy": 1,
    "Spain": 1,
    "Japan": 9,
    "Hong Kong": 8,
    "Australia": 10,
    "Canada": -5,
}

CURRENCY_TO_COUNTRY = {
    "USD": "USA",
    "SGD": "Singapore",
    "GBP": "UK",
    "EUR": "Eurozone",
    "JPY": "Japan",
    "HKD": "Hong Kong",
    "AUD": "Australia",
    "CAD": "Canada",
}


def country_from_currency(currency: str) -> str | None:
    return CURRENCY_TO_COUNTRY.get(currency.upper())


def local_hour(country: str) -> int | None:
    """Current hour (0-23) at the country's canonical timezone."""
    offset = COUNTRY_OFFSETS.get(country)
    if offset is None:
        return None
    now = datetime.now(timezone.utc) + timedelta(hours=offset)
    return now.hour


def meal_for_hour(hour: int) -> str:
    """Map an hour to a meal label."""
    if 5 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 15:
        return "lunch"
    if 15 <= hour < 17:
        return "snack"
    if 17 <= hour < 22:
        return "dinner"
    return "late night"


def infer_meal(country: str | None) -> str | None:
    """End-to-end: given a country, return the most likely meal happening now."""
    if not country:
        return None
    hour = local_hour(country)
    if hour is None:
        return None
    return meal_for_hour(hour)
