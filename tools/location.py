"""
Light-touch location inference from recent receipts.

If the user's last few receipts were in SGD, they're probably still in
Singapore. This is the cheapest possible signal and handles the most common
case: "I just landed, I'm in country X for a few days."
"""

from tools import db


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


def recent_currency(lookback: int = 5) -> str | None:
    """Return the most-common currency in the last N receipts, or None."""
    sql = f"""
        SELECT currency, COUNT(*) as n
        FROM (SELECT currency FROM receipts ORDER BY id DESC LIMIT {lookback})
        GROUP BY currency
        ORDER BY n DESC
        LIMIT 1
    """
    # We can use the same run_sql guard
    result = db.run_sql(sql)
    # result is tab-separated; first line is header, second is data
    lines = result.strip().split("\n")
    if len(lines) < 3:  # header + at least one row + summary
        return None
    data_line = lines[1].split("\t")
    if not data_line or not data_line[0]:
        return None
    return data_line[0]


def location_hint() -> str:
    """Return a short hint string for prompts, e.g. 'recently in Singapore (SGD)'."""
    cur = recent_currency()
    if not cur:
        return "no recent receipts yet"
    country = CURRENCY_TO_COUNTRY.get(cur, cur)
    return f"recently in {country} ({cur})"
