"""
SQLite storage on a Modal Volume.

Two tables:
  receipts — one row per scanned receipt
  vendors  — one row per unique shop we've seen, with learned tags

The Volume is mounted at /data inside the container; the DB lives at
/data/receipts.db.

Design notes:
- We open a fresh connection per call. SQLite handles this fine, and Modal
  containers can come and go.
- WAL mode for safer concurrent reads (we still only have one writer).
- The agent can run arbitrary SELECT queries via `run_sql` — much more
  powerful than the filter-by-filter approach we had with Sheets.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

DB_PATH = os.environ.get("RECEIPT_DB_PATH", "/data/receipts.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Idempotent — safe to run on every deploy."""
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS receipts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            date            TEXT NOT NULL,
            vendor          TEXT NOT NULL,
            total           REAL NOT NULL,
            currency        TEXT NOT NULL,
            total_usd       REAL NOT NULL,
            subtotal        REAL,
            tax             REAL,
            tip             REAL,
            category        TEXT,
            subcategory     TEXT,
            tier            TEXT,
            payment_method  TEXT,
            items           TEXT,
            notes           TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_receipts_date ON receipts(date);
        CREATE INDEX IF NOT EXISTS idx_receipts_vendor ON receipts(vendor);
        CREATE INDEX IF NOT EXISTS idx_receipts_category ON receipts(category);

        CREATE TABLE IF NOT EXISTS vendors (
            vendor_key      TEXT PRIMARY KEY,
            display_name    TEXT NOT NULL,
            category        TEXT,
            subcategory     TEXT,
            tier            TEXT,
            country         TEXT,
            seen_count      INTEGER NOT NULL DEFAULT 0,
            first_seen      TEXT,
            last_seen       TEXT,
            notes           TEXT
        );

        -- Group expenses where you paid up front and others owe you back.
        -- The user's own share is ALSO logged as a regular receipt — this
        -- table only tracks what's owed back, not the user's slice.
        CREATE TABLE IF NOT EXISTS pending_charges (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created         TEXT NOT NULL,
            date            TEXT NOT NULL,
            vendor          TEXT NOT NULL,
            total           REAL NOT NULL,
            currency        TEXT NOT NULL,
            total_usd       REAL NOT NULL,
            n_people        INTEGER NOT NULL,
            user_share      REAL NOT NULL,
            user_share_usd  REAL NOT NULL,
            owed_total      REAL NOT NULL,   -- what others owe us
            owed_usd        REAL NOT NULL,
            settled         INTEGER NOT NULL DEFAULT 0,
            settled_at      TEXT,
            last_nudge      TEXT,            -- last time we nudged the user
            receipt_id      INTEGER,         -- link to the user's-share receipt row
            notes           TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_pending_settled ON pending_charges(settled);

        -- Per-person breakdown, optional. Only used if user named names.
        CREATE TABLE IF NOT EXISTS debtors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            pending_id      INTEGER NOT NULL,
            name            TEXT NOT NULL,
            owed            REAL NOT NULL,         -- in pending_charges.currency
            paid            INTEGER NOT NULL DEFAULT 0,
            paid_at         TEXT,
            FOREIGN KEY (pending_id) REFERENCES pending_charges(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_debtors_pending ON debtors(pending_id);
        CREATE INDEX IF NOT EXISTS idx_debtors_paid ON debtors(paid);
        """)


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def append_receipt(data: dict) -> int:
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO receipts (
                timestamp, date, vendor, total, currency, total_usd,
                subtotal, tax, tip, category, subcategory, tier,
                payment_method, items, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(timespec="seconds"),
            data.get("date") or "",
            data.get("vendor") or "",
            float(data.get("total") or 0),
            data.get("currency") or "USD",
            float(data.get("total_usd") or 0),
            data.get("subtotal"),
            data.get("tax"),
            data.get("tip"),
            data.get("category") or "other",
            data.get("subcategory") or "",
            data.get("tier") or "",
            data.get("payment_method") or "",
            " | ".join(data.get("items") or []),
            data.get("notes") or "",
        ))
        return cur.lastrowid


def undo_last() -> str:
    with _conn() as c:
        row = c.execute(
            "SELECT id, date, vendor, total, currency FROM receipts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "Nothing to undo."
        c.execute("DELETE FROM receipts WHERE id = ?", (row["id"],))
        return f"Undone: {row['vendor']} {row['total']} {row['currency']} on {row['date']}"


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------

def _vendor_key(vendor: str) -> str:
    return "".join(c for c in vendor.lower() if c.isalnum())


def vendor_lookup(vendor: str) -> dict | None:
    if not vendor:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM vendors WHERE vendor_key = ?",
            (_vendor_key(vendor),),
        ).fetchone()
        return dict(row) if row else None


def vendor_remember(vendor: str, category: str = "", subcategory: str = "",
                    tier: str = "", country: str = "", notes: str = "") -> None:
    if not vendor:
        return
    key = _vendor_key(vendor)
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        existing = c.execute(
            "SELECT seen_count, first_seen, country FROM vendors WHERE vendor_key = ?",
            (key,),
        ).fetchone()
        if existing:
            # Only update country if we have new info and old was empty
            c.execute("""
                UPDATE vendors
                SET seen_count = seen_count + 1,
                    last_seen = ?,
                    country = COALESCE(NULLIF(country, ''), ?)
                WHERE vendor_key = ?
            """, (today, country, key))
        else:
            c.execute("""
                INSERT INTO vendors (
                    vendor_key, display_name, category, subcategory, tier,
                    country, seen_count, first_seen, last_seen, notes
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """, (key, vendor, category, subcategory, tier, country, today, today, notes))


def vendor_country(vendor: str) -> str | None:
    """Return the stored country for a vendor, or None."""
    if not vendor:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT country FROM vendors WHERE vendor_key = ?",
            (_vendor_key(vendor),),
        ).fetchone()
        return row["country"] if row and row["country"] else None


def vendor_correct(vendor: str, category: str = "", subcategory: str = "",
                   tier: str = "") -> str:
    if not vendor:
        return "No vendor specified."
    key = _vendor_key(vendor)
    with _conn() as c:
        existing = c.execute(
            "SELECT * FROM vendors WHERE vendor_key = ?", (key,)
        ).fetchone()
        if existing:
            c.execute("""
                UPDATE vendors
                SET category = COALESCE(NULLIF(?, ''), category),
                    subcategory = COALESCE(NULLIF(?, ''), subcategory),
                    tier = COALESCE(NULLIF(?, ''), tier)
                WHERE vendor_key = ?
            """, (category, subcategory, tier, key))

            # Also retroactively fix receipts from this vendor
            c.execute("""
                UPDATE receipts
                SET category = COALESCE(NULLIF(?, ''), category),
                    subcategory = COALESCE(NULLIF(?, ''), subcategory),
                    tier = COALESCE(NULLIF(?, ''), tier)
                WHERE LOWER(REPLACE(REPLACE(REPLACE(vendor, ' ', ''), '-', ''), '_', '')) = ?
            """, (category, subcategory, tier, key))
            return f"Updated {existing['display_name']} (and matching past receipts)"
        else:
            vendor_remember(vendor, category, subcategory, tier)
            return f"Added new vendor: {vendor}"


def vendor_list() -> str:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM vendors ORDER BY seen_count DESC"
        ).fetchall()
    if not rows:
        return "No vendors learned yet."
    lines = [f"{len(rows)} known vendors:"]
    for r in rows[:50]:
        tag = r["category"] or "?"
        if r["subcategory"]:
            tag += f"/{r['subcategory']}"
        if r["tier"]:
            tag += f"/{r['tier']}"
        lines.append(f"  {r['display_name']} — {tag} ({r['seen_count']}x)")
    if len(rows) > 50:
        lines.append(f"  …and {len(rows) - 50} more")
    return "\n".join(lines)


def last_vendor_currency(vendor: str) -> str | None:
    """Return the currency of the most recent receipt from this vendor, or None."""
    if not vendor:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT currency FROM receipts WHERE LOWER(vendor) = LOWER(?) "
            "ORDER BY id DESC LIMIT 1",
            (vendor,),
        ).fetchone()
        return row["currency"] if row else None


# ---------------------------------------------------------------------------
# Generic query — gives the agent real power
# ---------------------------------------------------------------------------

# Safe column list, used to validate column references where helpful.
RECEIPT_COLUMNS = {
    "id", "timestamp", "date", "vendor", "total", "currency", "total_usd",
    "subtotal", "tax", "tip", "category", "subcategory", "tier",
    "payment_method", "items", "notes",
}


def run_sql(query: str, limit: int = 100) -> str:
    """
    Run a SELECT query against the database.
    Only SELECT is allowed — we strip and reject anything else.
    """
    q = query.strip().rstrip(";").strip()
    lowered = q.lower()

    # Hard guard: only SELECT and only one statement
    if not lowered.startswith("select") and not lowered.startswith("with"):
        return "ERROR: only SELECT (or WITH...SELECT) queries are allowed."
    forbidden = ["insert", "update", "delete", "drop", "alter", "create",
                 "attach", "pragma ", "vacuum", "replace"]
    for f in forbidden:
        if f in lowered:
            return f"ERROR: forbidden keyword '{f.strip()}' in query."
    if ";" in q:
        return "ERROR: only one statement allowed (no semicolons)."

    try:
        with _conn() as c:
            rows = c.execute(q).fetchmany(limit)
            if not rows:
                return "(no rows)"
            cols = rows[0].keys()
            # Tab-separated table that fits in a Telegram message
            header = "\t".join(cols)
            body = "\n".join("\t".join(_fmt(r[col]) for col in cols) for r in rows)
            return f"{header}\n{body}\n({len(rows)} row{'s' if len(rows) != 1 else ''})"
    except sqlite3.Error as e:
        return f"SQL error: {e}"


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


# ---------------------------------------------------------------------------
# Backup helper
# ---------------------------------------------------------------------------

def db_info() -> str:
    """Quick stats for the user."""
    with _conn() as c:
        receipts = c.execute("SELECT COUNT(*) as n FROM receipts").fetchone()["n"]
        vendors = c.execute("SELECT COUNT(*) as n FROM vendors").fetchone()["n"]
        first = c.execute("SELECT MIN(date) as d FROM receipts").fetchone()["d"]
        last = c.execute("SELECT MAX(date) as d FROM receipts").fetchone()["d"]
        total_usd = c.execute(
            "SELECT COALESCE(SUM(total_usd), 0) as s FROM receipts"
        ).fetchone()["s"]
        open_pending = c.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(owed_usd), 0) as s "
            "FROM pending_charges WHERE settled = 0"
        ).fetchone()
    out = (
        f"📊 *DB stats*\n"
        f"Receipts: {receipts}\n"
        f"Vendors: {vendors}\n"
        f"Date range: {first or '—'} to {last or '—'}\n"
        f"Total tracked: ${total_usd:,.2f} USD"
    )
    if open_pending["n"]:
        out += f"\n\n💰 Pending: {open_pending['n']} open, ${open_pending['s']:,.2f} USD owed to you"
    return out


# ---------------------------------------------------------------------------
# Pending charges (split expenses)
# ---------------------------------------------------------------------------

def create_pending_charge(
    *, date: str, vendor: str, total: float, currency: str, total_usd: float,
    n_people: int, user_share: float, user_share_usd: float,
    owed_total: float, owed_usd: float,
    receipt_id: int | None = None, notes: str = "",
    debtors: list[str] | None = None,
) -> int:
    """Create a pending charge plus optional per-person debtor rows."""
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO pending_charges (
                created, date, vendor, total, currency, total_usd,
                n_people, user_share, user_share_usd,
                owed_total, owed_usd, receipt_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(timespec="seconds"),
            date, vendor, total, currency, total_usd,
            n_people, user_share, user_share_usd,
            owed_total, owed_usd, receipt_id, notes,
        ))
        pid = cur.lastrowid
        if debtors:
            per_person = owed_total / len(debtors)
            for name in debtors:
                c.execute(
                    "INSERT INTO debtors (pending_id, name, owed) VALUES (?, ?, ?)",
                    (pid, name.strip(), per_person),
                )
        return pid


def list_pending(settled: bool = False) -> list[dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM pending_charges
            WHERE settled = ?
            ORDER BY date DESC
        """, (1 if settled else 0,)).fetchall()
        return [dict(r) for r in rows]


def list_pending_formatted() -> str:
    """User-friendly summary of all open pending charges."""
    open_charges = list_pending(settled=False)
    if not open_charges:
        return "No pending charges. 🎉"

    total_owed_usd = sum(c["owed_usd"] for c in open_charges)
    lines = [f"💰 *{len(open_charges)} pending* — ${total_owed_usd:,.2f} USD owed to you:\n"]

    for c in open_charges:
        debtors = _debtors_for(c["id"])
        line = (
            f"#{c['id']} — {c['vendor']} on {c['date']}\n"
            f"  Total: {c['total']:.2f} {c['currency']} "
            f"({c['n_people']} people, your share {c['user_share']:.2f})\n"
            f"  Outstanding: {c['owed_total']:.2f} {c['currency']}"
        )
        if debtors:
            paid_n = sum(1 for d in debtors if d["paid"])
            line += f" — {paid_n}/{len(debtors)} paid back"
            unpaid_names = [d["name"] for d in debtors if not d["paid"]]
            if unpaid_names:
                line += f"\n  Waiting on: {', '.join(unpaid_names)}"
        lines.append(line)
    return "\n\n".join(lines)


def _debtors_for(pending_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM debtors WHERE pending_id = ? ORDER BY paid, name",
            (pending_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending(pending_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM pending_charges WHERE id = ?",
            (pending_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["debtors"] = _debtors_for(pending_id)
        return result


def mark_debtor_paid(pending_id: int, name: str) -> str:
    """Mark one named debtor as paid. Returns a status string."""
    today = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        # Try exact match first, then case-insensitive
        row = c.execute(
            "SELECT id, owed, paid FROM debtors WHERE pending_id = ? AND LOWER(name) = LOWER(?)",
            (pending_id, name.strip()),
        ).fetchone()
        if not row:
            return f"No debtor named '{name}' on pending #{pending_id}"
        if row["paid"]:
            return f"{name} was already marked paid on #{pending_id}"
        c.execute(
            "UPDATE debtors SET paid = 1, paid_at = ? WHERE id = ?",
            (today, row["id"]),
        )
        # Decrement the pending charge's owed totals
        pending = c.execute(
            "SELECT currency, total, owed_total, owed_usd FROM pending_charges WHERE id = ?",
            (pending_id,),
        ).fetchone()
        share = row["owed"]
        share_ratio = share / pending["total"] if pending["total"] else 0
        usd_share = pending["owed_usd"] * (share / pending["owed_total"]) if pending["owed_total"] else 0
        c.execute("""
            UPDATE pending_charges
            SET owed_total = MAX(0, owed_total - ?),
                owed_usd = MAX(0, owed_usd - ?)
            WHERE id = ?
        """, (share, usd_share, pending_id))
        # Auto-close if everyone has paid
        any_unpaid = c.execute(
            "SELECT COUNT(*) as n FROM debtors WHERE pending_id = ? AND paid = 0",
            (pending_id,),
        ).fetchone()["n"]
        if any_unpaid == 0:
            c.execute(
                "UPDATE pending_charges SET settled = 1, settled_at = ? WHERE id = ?",
                (today, pending_id),
            )
            return f"✓ {name} paid. #{pending_id} fully settled."
        return f"✓ {name} paid. {any_unpaid} still owe on #{pending_id}."


def mark_pending_settled(pending_id: int) -> str:
    """Mark the whole pending charge as settled (everyone paid, or close it manually)."""
    today = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        row = c.execute(
            "SELECT vendor, settled FROM pending_charges WHERE id = ?",
            (pending_id,),
        ).fetchone()
        if not row:
            return f"No pending #{pending_id}"
        if row["settled"]:
            return f"#{pending_id} ({row['vendor']}) already settled"
        c.execute("""
            UPDATE pending_charges
            SET settled = 1, settled_at = ?, owed_total = 0, owed_usd = 0
            WHERE id = ?
        """, (today, pending_id))
        c.execute(
            "UPDATE debtors SET paid = 1, paid_at = ? WHERE pending_id = ? AND paid = 0",
            (today, pending_id),
        )
        return f"✓ #{pending_id} ({row['vendor']}) settled in full"


def delete_pending(pending_id: int) -> str:
    """Remove a pending charge entirely (e.g. created in error). Also removes linked debtors."""
    with _conn() as c:
        row = c.execute(
            "SELECT vendor, receipt_id FROM pending_charges WHERE id = ?",
            (pending_id,),
        ).fetchone()
        if not row:
            return f"No pending #{pending_id}"
        c.execute("DELETE FROM debtors WHERE pending_id = ?", (pending_id,))
        c.execute("DELETE FROM pending_charges WHERE id = ?", (pending_id,))
        # Also remove the linked user-share receipt if present
        if row["receipt_id"]:
            c.execute("DELETE FROM receipts WHERE id = ?", (row["receipt_id"],))
        return f"Removed pending #{pending_id} ({row['vendor']})"


def stale_pendings(days_since_nudge: int = 3) -> list[dict]:
    """Return pending charges that haven't been nudged in `days_since_nudge` days."""
    cutoff = (datetime.now() - timedelta(days=days_since_nudge)).isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM pending_charges
            WHERE settled = 0
              AND (last_nudge IS NULL OR last_nudge < ?)
            ORDER BY created ASC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]


def mark_nudged(pending_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE pending_charges SET last_nudge = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), pending_id),
        )


def age_days(pending_id: int) -> int:
    """Days since the pending charge was created."""
    with _conn() as c:
        row = c.execute(
            "SELECT created FROM pending_charges WHERE id = ?",
            (pending_id,),
        ).fetchone()
        if not row:
            return 0
        created = datetime.fromisoformat(row["created"])
        return (datetime.now() - created).days
