"""
Worker logic for receipt-bot (free / Gemini edition).

Same flows as v5 but every LLM call goes through tools/llm.py.
"""

import base64
import json
import os
from datetime import datetime, timedelta

import httpx

from tools import db, fx, location, timeloc, llm


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = """\
This is a photograph of a receipt. Extract ONLY the literal facts visible.
Return a JSON object with this exact shape, nothing else (no markdown, no fences):

{
  "vendor": "store/restaurant name as printed",
  "date": "YYYY-MM-DD (today if not visible)",
  "total": 0.00,
  "subtotal": 0.00 or null,
  "tax": 0.00 or null,
  "tip": 0.00 or null,
  "currency": "ISO code: USD, SGD, GBP, EUR, JPY, HKD, AUD, CAD, etc.",
  "country": "USA / Singapore / UK / etc, based on address or currency",
  "payment_method": "card last 4 or cash, null if unclear",
  "items": ["each line item as a short description"],
  "notes": "anything else worth noting"
}

For currency: S$ → SGD, £ → GBP, € → EUR. GST → usually SGD/AUD; VAT → GBP/EUR.
If not a receipt, return {"error": "not a receipt"}.
"""

CLASSIFICATION_PROMPT_TEMPLATE = """\
Classify this vendor for a personal expense tracker. Be decisive.

Vendor: {vendor}
Items: {items}
Total: {total} {currency}
Notes: {notes}

Taxonomy:
- food: restaurant, cafe, fast_food, bar, delivery, bakery, hawker
- groceries: supermarket, specialty, convenience
- shopping: clothing, shoes, accessories, beauty, electronics, home, books, gifts
- transport: rideshare, taxi, public_transit, flight, train, fuel, parking, rental
- lodging: hotel, airbnb, hostel
- entertainment: concert, movie, museum, sport, attraction
- services: barber, spa, laundry, repair, medical, fitness
- other

Tier (for shopping): luxury (Rick Owens, Hermes, Margiela), premium (Acne, A.P.C., Aimé Leon Dore), mainstream (Nike, Gap), value (H&M, Uniqlo, Shein)
Tier (for food): fine_dining, mid_range, casual, fast_food, hawker
Tier (for groceries): premium (Whole Foods, Erewhon), mainstream (NTUC, Tesco), discount

Also identify the country (USA, Singapore, UK, France, Japan, etc).

Return ONLY this JSON, no prose, no markdown fences:
{{"category": "...", "subcategory": "...", "tier": "...", "country": "..."}}
"""

INTENT_PROMPT_TEMPLATE = """\
The user just sent this message to their expense bot:

"{message}"

Context: today is {today}. They were {location_hint}.

Decide what they want. Reply with ONE of these JSON shapes (no prose, no fences):

1. Logging a regular personal expense:
   {{"intent": "log", "amount": 5.0, "currency": "SGD", "vendor": "YY Kafei",
     "date": "YYYY-MM-DD", "items": [], "notes": ""}}

2. Logging a SPLIT expense where user paid for a group:
   {{"intent": "split", "amount": 800, "currency": "USD",
     "vendor": "Team Dinner", "date": "YYYY-MM-DD",
     "n_people": 9, "debtors": [], "notes": ""}}
   Use this when the user mentions paying for a group or being owed back.
   If specific names given ("dinner with j, mike, sara"), put them in debtors
   (excluding the user themselves) and n_people = len(debtors) + 1.

3. Marking that someone paid the user back:
   {{"intent": "payback", "who": "Mike", "pending_id": null}}
   "everyone paid" → who = "everyone".

4. Listing open pending charges:
   {{"intent": "list_pending"}}

5. Asking a question / analytics:
   {{"intent": "query"}}

6. Correcting a vendor's classification:
   {{"intent": "correction"}}

7. Wanting to undo/delete:
   {{"intent": "undo"}}

8. Anything else:
   {{"intent": "other"}}

Rules:
- Vendor names: capitalize properly ("yy kafei" → "YY Kafei").
- If only one number is present, it's the total.
- If currency isn't stated, leave null.
- Date: parse "yesterday", "last Friday", "Mar 14" to YYYY-MM-DD; default today.

Examples:
  "5 yy kafei" → log
  "150 tatiana" → log
  "$800 team dinner 9 people" → split, n_people=9
  "120 dinner with j and mike" → split, n_people=3, debtors=["j","mike"]
  "j paid me back" → payback, who="j"
  "everyone paid" → payback, who="everyone"
  "who owes me" → list_pending
  "how much on food this month" → query

Return ONLY the JSON.
"""

QUERY_SYSTEM_PROMPT = """\
You are a multi-currency personal expense assistant for a SQLite database.

Schema:
  receipts (id, timestamp, date, vendor, total, currency, total_usd,
            subtotal, tax, tip, category, subcategory, tier,
            payment_method, items, notes)
  vendors  (vendor_key, display_name, category, subcategory, tier, country,
            seen_count, first_seen, last_seen, notes)
  pending_charges (id, created, date, vendor, total, currency, total_usd,
            n_people, user_share, user_share_usd, owed_total, owed_usd,
            settled, settled_at, last_nudge, receipt_id, notes)
  debtors  (id, pending_id, name, owed, paid, paid_at)

Notes:
- Dates are TEXT YYYY-MM-DD.
- Prefer total_usd for cross-currency aggregations.
- LIKE is case-sensitive; use LOWER() or COLLATE NOCASE.
- receipts contains only the user's actual cost (their share of splits).

To answer the user's question, you may issue SQL by replying with ONLY this JSON:
  {"sql": "SELECT ..."}

After running the SQL you'll see results. Then reply with the final answer
as plain text. You can issue up to 3 SQL queries to refine.

Style: terse. Show USD by default; mention native currency when relevant.
"""


# ---------------------------------------------------------------------------
# Photo path
# ---------------------------------------------------------------------------

def _download_telegram_file(file_id: str) -> tuple[bytes, str]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = httpx.get(
        f"https://api.telegram.org/bot{token}/getFile",
        params={"file_id": file_id}, timeout=10,
    )
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    r = httpx.get(
        f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30,
    )
    r.raise_for_status()
    ext = file_path.rsplit(".", 1)[-1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    return r.content, mime


def _parse_json(text: str) -> dict:
    """Strip code fences if present, then parse. Gemini sometimes adds fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    # Some models add stray text before/after; grab the largest {...} block
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def _extract_facts_from_image(img_bytes: bytes, mime: str, caption: str) -> dict:
    prompt = EXTRACTION_PROMPT
    if caption:
        prompt += f"\n\nThe user added this note: {caption}"
    text = llm.generate_with_image(prompt, img_bytes, mime, max_tokens=1024)
    return _parse_json(text)


def _classify(vendor: str, items: list[str], total: float, currency: str,
              notes: str, use_web_search: bool = False) -> dict:
    prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(
        vendor=vendor,
        items=", ".join(items) if items else "(none listed)",
        total=total, currency=currency,
        notes=notes or "(none)",
    )
    if use_web_search:
        text = llm.generate_with_search(prompt, max_tokens=512)
    else:
        text = llm.generate_text(prompt, max_tokens=512)
    try:
        return _parse_json(text)
    except Exception:
        return {"category": "other", "subcategory": "", "tier": "", "country": ""}


def _persist_and_format(facts: dict, source: str = "photo") -> tuple[str, int]:
    vendor = facts.get("vendor") or ""
    facts["total_usd"] = fx.to_usd(
        amount=float(facts.get("total") or 0),
        currency=facts["currency"],
        on_date=facts["date"],
    )
    receipt_id = db.append_receipt(facts)
    db.vendor_remember(
        vendor=vendor,
        category=facts.get("category", "other"),
        subcategory=facts.get("subcategory", ""),
        tier=facts.get("tier", ""),
        country=facts.get("country", ""),
    )

    native = f"{float(facts.get('total') or 0):.2f} {facts['currency']}"
    usd_str = f" ≈ ${facts['total_usd']:.2f} USD" if facts["currency"] != "USD" else ""
    tag = facts.get("category", "?")
    if facts.get("subcategory"):
        tag += f" / {facts['subcategory']}"
    if facts.get("tier"):
        tag += f" / {facts['tier']}"
    items = facts.get("items") or []
    items_str = ("\n" + ", ".join(items[:5])) if items else ""

    reply = (
        f"✅ #{receipt_id} ({source})\n"
        f"*{vendor}* — {facts['date']}\n"
        f"{native}{usd_str}\n"
        f"_{tag}_{items_str}\n\n"
        f"_undo or 'fix vendor {vendor} tier X'_"
    )
    return reply, receipt_id


def _apply_caption_overrides(facts: dict, caption: str) -> None:
    for piece in caption.split(","):
        piece = piece.strip().lower()
        if piece.startswith("category:"):
            facts["category"] = piece.split(":", 1)[1].strip()
        elif piece.startswith("subcategory:"):
            facts["subcategory"] = piece.split(":", 1)[1].strip()
        elif piece.startswith("tier:"):
            facts["tier"] = piece.split(":", 1)[1].strip()


def handle_photo(file_id: str, caption: str = "") -> str:
    img_bytes, mime = _download_telegram_file(file_id)
    try:
        facts = _extract_facts_from_image(img_bytes, mime, caption)
    except Exception as e:
        return f"⚠️ couldn't extract: {e}"
    if facts.get("error"):
        return f"⚠️ {facts['error']}"

    facts["date"] = facts.get("date") or datetime.now().strftime("%Y-%m-%d")
    facts["currency"] = fx.normalize_code(facts.get("currency"))
    if not facts.get("country"):
        facts["country"] = timeloc.country_from_currency(facts["currency"]) or ""

    vendor = facts.get("vendor") or ""
    known = db.vendor_lookup(vendor)
    if known:
        facts["category"] = known.get("category") or "other"
        facts["subcategory"] = known.get("subcategory") or ""
        facts["tier"] = known.get("tier") or ""
        if not facts.get("country"):
            facts["country"] = known.get("country") or ""
        source = f"photo · known {known.get('seen_count')}x"
    else:
        cls = _classify(vendor, facts.get("items") or [], facts.get("total") or 0,
                        facts["currency"], facts.get("notes") or "")
        facts["category"] = cls.get("category", "other")
        facts["subcategory"] = cls.get("subcategory", "")
        facts["tier"] = cls.get("tier", "")
        if not facts.get("country"):
            facts["country"] = cls.get("country", "")
        source = "photo · new vendor"

    if caption:
        _apply_caption_overrides(facts, caption)

    reply, _ = _persist_and_format(facts, source)
    return reply


# ---------------------------------------------------------------------------
# Text path
# ---------------------------------------------------------------------------

def _classify_intent(user_text: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = INTENT_PROMPT_TEMPLATE.format(
        message=user_text,
        today=today,
        location_hint=location.location_hint(),
    )
    text = llm.generate_text(prompt, max_tokens=500)
    try:
        return _parse_json(text)
    except Exception:
        return {"intent": "other"}


def _resolve_currency_and_country(vendor: str, explicit_currency: str | None) -> tuple[str, str]:
    known = db.vendor_lookup(vendor)
    if explicit_currency:
        currency = fx.normalize_code(explicit_currency)
    elif known:
        currency = db.last_vendor_currency(vendor) or location.recent_currency() or "USD"
    else:
        currency = location.recent_currency() or "USD"

    country = (known.get("country") if known else None) or timeloc.country_from_currency(currency) or ""
    return currency, country


def _quick_log(intent: dict, original_text: str) -> str:
    vendor = (intent.get("vendor") or "").strip()
    if not vendor:
        return "⚠️ couldn't figure out the vendor."
    amount = intent.get("amount")
    if amount is None:
        return "⚠️ couldn't figure out the amount."

    facts = {
        "vendor": vendor,
        "total": float(amount),
        "date": intent.get("date") or datetime.now().strftime("%Y-%m-%d"),
        "items": list(intent.get("items") or []),
        "notes": intent.get("notes") or "",
    }
    currency, country = _resolve_currency_and_country(vendor, intent.get("currency"))
    facts["currency"] = currency
    facts["country"] = country

    known = db.vendor_lookup(vendor)
    if known:
        facts["category"] = known.get("category") or "other"
        facts["subcategory"] = known.get("subcategory") or ""
        facts["tier"] = known.get("tier") or ""
        source = f"text · known {known.get('seen_count')}x"
    else:
        cls = _classify(
            vendor=vendor, items=facts["items"], total=facts["total"],
            currency=facts["currency"],
            notes=facts["notes"] + f" (logged via text in {country or 'unknown'})",
            use_web_search=True,
        )
        facts["category"] = cls.get("category", "other")
        facts["subcategory"] = cls.get("subcategory", "")
        facts["tier"] = cls.get("tier", "")
        if not facts["country"]:
            facts["country"] = cls.get("country", "")
        source = "text · new vendor (searched)"

    if (facts["category"] in ("food", "groceries")
            and not facts["items"]
            and facts["date"] == datetime.now().strftime("%Y-%m-%d")):
        meal = timeloc.infer_meal(facts["country"])
        if meal:
            facts["items"] = [meal]

    reply, _ = _persist_and_format(facts, source)
    return reply


def _handle_split(intent: dict) -> str:
    vendor = (intent.get("vendor") or "").strip() or "Group expense"
    amount = intent.get("amount")
    n_people = intent.get("n_people")
    debtors = intent.get("debtors") or []

    if amount is None:
        return "⚠️ couldn't figure out the amount."
    if not n_people and not debtors:
        return "⚠️ how many people? Try '$800 dinner 9 people' or 'with j, mike, sara'."

    if debtors and not n_people:
        n_people = len(debtors) + 1

    amount = float(amount)
    user_share = round(amount / n_people, 2)
    owed_total = round(amount - user_share, 2)

    date = intent.get("date") or datetime.now().strftime("%Y-%m-%d")
    currency, country = _resolve_currency_and_country(vendor, intent.get("currency"))

    total_usd = fx.to_usd(amount, currency, date)
    user_share_usd = fx.to_usd(user_share, currency, date)
    owed_usd = round(total_usd - user_share_usd, 2)

    known = db.vendor_lookup(vendor)
    if known:
        category = known.get("category") or "food"
        subcategory = known.get("subcategory") or ""
        tier = known.get("tier") or ""
    else:
        category, subcategory, tier = "food", "restaurant", ""

    items = []
    if (category in ("food", "groceries")
            and date == datetime.now().strftime("%Y-%m-%d")):
        meal = timeloc.infer_meal(country)
        if meal:
            items = [meal]

    user_receipt = {
        "date": date, "vendor": vendor, "total": user_share,
        "currency": currency, "total_usd": user_share_usd,
        "category": category, "subcategory": subcategory, "tier": tier,
        "items": items,
        "notes": f"(your share of {amount:.2f} {currency} split {n_people} ways)",
    }
    receipt_id = db.append_receipt(user_receipt)
    db.vendor_remember(vendor, category, subcategory, tier, country)

    pid = db.create_pending_charge(
        date=date, vendor=vendor, total=amount, currency=currency, total_usd=total_usd,
        n_people=n_people, user_share=user_share, user_share_usd=user_share_usd,
        owed_total=owed_total, owed_usd=owed_usd,
        receipt_id=receipt_id, notes=intent.get("notes", ""),
        debtors=debtors if debtors else None,
    )

    native_fn = lambda v: f"{v:.2f} {currency}"
    usd_fn = lambda v: f" (${v:.2f})" if currency != "USD" else ""

    lines = [
        f"💸 *split #{pid}* — {vendor}",
        f"Total: {native_fn(amount)}{usd_fn(total_usd)} split {n_people} ways",
        f"Your share: {native_fn(user_share)}{usd_fn(user_share_usd)} (receipt #{receipt_id})",
        f"You're owed: {native_fn(owed_total)}{usd_fn(owed_usd)}",
    ]
    if debtors:
        per = owed_total / len(debtors)
        lines.append(f"\nWaiting on: {', '.join(debtors)} ({native_fn(per)} each)")
    lines.append(f"\n_Reply 'X paid me back' as people settle, or 'remove split {pid}'_")
    return "\n".join(lines)


def _handle_payback(intent: dict) -> str:
    who = (intent.get("who") or "").strip()
    pending_id = intent.get("pending_id")
    if not who:
        return "⚠️ who paid you back?"

    open_charges = db.list_pending(settled=False)
    if not open_charges:
        return "No open pending charges."

    if who.lower() == "everyone":
        target = next((c for c in open_charges if c["id"] == pending_id), None) or open_charges[0]
        return db.mark_pending_settled(target["id"])

    candidates = []
    for charge in open_charges:
        if pending_id is not None and charge["id"] != pending_id:
            continue
        for d in db._debtors_for(charge["id"]):
            if d["name"].lower() == who.lower() and not d["paid"]:
                candidates.append((charge, d))

    if candidates:
        return "\n".join(db.mark_debtor_paid(c["id"], d["name"]) for c, d in candidates)

    eligible = [c for c in open_charges if not db._debtors_for(c["id"])]
    if len(eligible) == 1:
        return _decrement_one_share(eligible[0], who)

    return f"⚠️ couldn't find '{who}' on any pending charge."


def _decrement_one_share(charge: dict, who: str) -> str:
    per_person = round(charge["total"] / charge["n_people"], 2)
    new_owed = max(0.0, charge["owed_total"] - per_person)
    if charge["owed_total"] > 0:
        new_owed_usd = round(charge["owed_usd"] * (new_owed / charge["owed_total"]), 2)
    else:
        new_owed_usd = 0.0

    today = datetime.now().isoformat(timespec="seconds")
    with db._conn() as c:
        c.execute("""
            UPDATE pending_charges
            SET owed_total = ?, owed_usd = ?,
                notes = COALESCE(NULLIF(notes, ''), '') ||
                        CASE WHEN COALESCE(notes, '') = '' THEN '' ELSE ' | ' END ||
                        ?
            WHERE id = ?
        """, (new_owed, new_owed_usd, f"{who} paid", charge["id"]))
        if new_owed == 0:
            c.execute(
                "UPDATE pending_charges SET settled = 1, settled_at = ? WHERE id = ?",
                (today, charge["id"]),
            )
    status = "fully settled." if new_owed == 0 else f"{new_owed:.2f} {charge['currency']} still outstanding."
    return f"✓ Logged {who} paid {per_person:.2f} {charge['currency']} on #{charge['id']}. {status}"


# ---------------------------------------------------------------------------
# Query path — small loop with Gemini and SQL
# ---------------------------------------------------------------------------

def _agent_loop(user_text: str, max_turns: int = 4) -> str:
    """Multi-step query: ask Gemini for SQL, run it, ask Gemini to interpret."""
    today = datetime.now().strftime("%Y-%m-%d")
    history = [
        f"System: {QUERY_SYSTEM_PROMPT}",
        f"User (today is {today}): {user_text}",
    ]

    for turn in range(max_turns):
        prompt = "\n\n".join(history) + "\n\nAssistant:"
        response = llm.generate_text(prompt, max_tokens=800)
        history.append(f"Assistant: {response}")

        # Look for a SQL request
        stripped = response.strip()
        if stripped.startswith("{") and '"sql"' in stripped:
            try:
                sql_req = _parse_json(stripped)
                result = db.run_sql(sql_req["sql"])
                history.append(f"SQL result:\n{result}")
                continue
            except Exception as e:
                history.append(f"SQL error: {e}")
                continue

        # Otherwise treat as final answer
        return response.strip()

    return "(stopped after max turns)"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handle_text(user_text: str) -> str:
    intent = _classify_intent(user_text)
    kind = intent.get("intent", "other")

    if kind == "log":
        return _quick_log(intent, user_text)
    if kind == "split":
        return _handle_split(intent)
    if kind == "payback":
        return _handle_payback(intent)
    if kind == "list_pending":
        return db.list_pending_formatted()
    if kind == "undo":
        return db.undo_last()
    return _agent_loop(user_text)


# ---------------------------------------------------------------------------
# Scheduled nudges
# ---------------------------------------------------------------------------

NUDGE_AGES = [3, 7, 14, 30]


def _should_nudge(age_days: int, last_nudge_str: str | None) -> bool:
    if age_days < NUDGE_AGES[0]:
        return False
    if age_days > NUDGE_AGES[-1]:
        return False
    if not last_nudge_str:
        return True
    try:
        last = datetime.fromisoformat(last_nudge_str)
    except ValueError:
        return True
    return (datetime.now() - last).days >= 4


def build_nudges() -> list[str]:
    open_charges = db.list_pending(settled=False)
    messages = []
    today = datetime.now()
    for charge in open_charges:
        try:
            created = datetime.fromisoformat(charge["created"])
        except ValueError:
            continue
        age = (today - created).days
        if not _should_nudge(age, charge.get("last_nudge")):
            continue

        debtors = db._debtors_for(charge["id"])
        unpaid_names = [d["name"] for d in debtors if not d["paid"]]
        owed = f"{charge['owed_total']:.2f} {charge['currency']}"

        msg = (
            f"⏰ pending #{charge['id']} — {charge['vendor']} ({age}d old)\n"
            f"Still owed: {owed} (~${charge['owed_usd']:.2f} USD)"
        )
        if unpaid_names:
            msg += f"\nWaiting on: {', '.join(unpaid_names)}"
        msg += f"\n_Reply 'X paid' or 'settle pending {charge['id']}'_"
        messages.append(msg)
        db.mark_nudged(charge["id"])
    return messages
