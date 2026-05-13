# Receipt Bot - Complete Build Guide

## Overview
A personal expense tracker bot that runs on Telegram, using Claude API for LLM processing and Modal for serverless compute/storage.

## Architecture
- **Frontend**: Telegram (user sends messages/photos)
- **Compute**: Modal (serverless functions)
- **Storage**: SQLite on Modal Volume
- **LLM**: Anthropic Claude API (default: Claude Haiku 3.5)
- **Code**: Python 3.11

---

## Phase 1: Setup & Configuration

### 1.1 Project Structure
```
receipt-bot-free/
├── app.py                 # Modal app definition & webhook
├── worker.py              # Core business logic
├── telegram.py            # Telegram API wrapper
├── tools/
│   ├── __init__.py
│   ├── db.py              # SQLite database layer
│   ├── fx.py              # Currency conversion
│   ├── llm.py             # Claude API wrapper
│   ├── location.py        # User location/timezone
│   └── timeloc.py         # Time utilities
├── requirements.txt       # Python dependencies
└── README.md
```

### 1.2 Dependencies (requirements.txt)
```
anthropic>=0.7.0
httpx>=0.27.0
modal>=0.64.0
fastapi[standard]>=0.115.0
```

### 1.3 Environment Setup
1. Create Modal account at modal.com
2. Get Claude API key from console.anthropic.com
3. Create Telegram bot via @BotFather
4. Store secrets in Modal:
   - `anthropic-api-key`: ANTHROPIC_API_KEY
   - `telegram-bot`: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

---

## Phase 2: Core Modules

### 2.1 tools/llm.py - LLM Wrapper
**Purpose**: Abstraction layer for Claude API calls

**Functions**:
- `generate_text(prompt: str, max_tokens: int) -> str`
  - Simple text-to-text generation
  - Used for intent classification, vendor lookup, queries
  
- `generate_with_image(prompt: str, image_bytes: bytes, mime: str, max_tokens: int) -> str`
  - Receipt OCR via Claude's vision capability
  - Extract vendor, amount, date, items, etc from image
  
- `generate_with_search(prompt: str, max_tokens: int) -> str`
  - Text generation (no search grounding available, fallback to generate_text)
  - Used for vendor classification

**Key Implementation**:
- Lazily initialize and cache the `Anthropic` client with API key
- Use `claude-3-5-haiku-20241022` by default
- Allow `ANTHROPIC_MODEL` to override the model without code changes
- Handle base64 encoding for images
- Return raw text (JSON parsing happens in worker.py)

### 2.2 tools/db.py - SQLite Database
**Purpose**: Persistent storage for receipts, vendors, splits

**Tables**:
1. `receipts` - individual expenses
   - id, timestamp, date, vendor, total, currency, total_usd
   - subtotal, tax, tip, category, subcategory, tier
   - payment_method, items (JSON), notes

2. `vendors` - learned vendor data
   - vendor_key (primary key), display_name
   - category, subcategory, tier, country
   - seen_count, first_seen, last_seen, notes

3. `pending_charges` - group split tracking
   - id, created, date, vendor, total, currency, total_usd
   - n_people, user_share, user_share_usd
   - owed_total, owed_usd, settled, settled_at, last_nudge
   - receipt_id (link to user's receipt), notes

4. `debtors` - who owes what in a split
   - id, pending_id, name, owed, paid, paid_at

**Functions**:
- `init_db()` - Create tables if missing (idempotent)
- `append_receipt(facts: dict) -> int` - Insert receipt, return id
- `vendor_lookup(vendor: str) -> dict` - Find vendor, return category/tier/country
- `vendor_remember(vendor, category, subcategory, tier, country)` - Save vendor
- `create_pending_charge(...)` - Create split record
- `list_pending(settled: bool) -> list` - Get open/closed splits
- `mark_pending_settled(id: int)` - Mark split as done
- `mark_debtor_paid(pending_id, name)` - Mark one debtor as paid
- `undo_last()` - Delete last receipt
- `run_sql(query: str)` - Execute arbitrary SQL for queries
- `list_pending_formatted()` - Human-readable pending list
- `_debtors_for(pending_id: int)` - Get debtors for a split

**Key Implementation**:
- Use context manager for connections
- Enable WAL mode for concurrent reads
- Row factory for dict-like row access
- Auto-commit on success

### 2.3 tools/fx.py - Currency Conversion
**Purpose**: Convert amounts to USD for aggregation

**Functions**:
- `normalize_code(code: str) -> str` - Normalize currency codes (£ → GBP, € → EUR)
- `to_usd(amount: float, currency: str, on_date: str) -> float` - Convert to USD using Frankfurter API

**Key Implementation**:
- Call frankfurter.app API for historical rates
- Cache results locally if possible
- Default to USD

### 2.4 tools/location.py - Location & Context
**Purpose**: Infer user's location/timezone for context

**Functions**:
- `location_hint()` -> str - Return "in [city]" or "unknown location"
- `recent_currency()` -> str - Get currency from recent location

**Key Implementation**:
- Optional geolocation (can be stubbed out for now)
- Return generic hints to Claude

### 2.5 tools/timeloc.py - Time Utilities
**Purpose**: Timezone-aware date/time operations

**Functions**:
- `country_from_currency(code: str) -> str` - Map currency to country
- `infer_meal(country: str) -> str` - Return "breakfast"/"lunch"/"dinner" based on local time

**Key Implementation**:
- Map currencies to countries
- Use local time to infer meals

---

## Phase 3: Worker Logic (worker.py)

### 3.1 Prompts
Define 4 key prompts as constants:

1. **EXTRACTION_PROMPT** - Receipt OCR
   - Input: receipt image
   - Output: JSON with vendor, date, total, currency, items, etc.

2. **CLASSIFICATION_PROMPT** - Vendor categorization
   - Input: vendor name, items, total, currency
   - Output: JSON with category, subcategory, tier, country
   - Categories: food, groceries, shopping, transport, lodging, entertainment, services, other

3. **INTENT_PROMPT** - User message classification
   - Input: user message, date, location hint
   - Output: JSON with intent type and parameters
   - Intent types: log, split, payback, list_pending, query, correction, undo, other

4. **QUERY_SYSTEM_PROMPT** - Multi-step SQL agent
   - System message for handling analytics queries
   - Schema description + instruction to generate SQL or respond

### 3.2 Intent Classification
- `_classify_intent(user_text: str) -> dict`
  - Call Claude with INTENT_PROMPT
  - Parse JSON response
  - Return intent dict with type + parameters

### 3.3 Photo Path (handle_photo)
- `handle_photo(file_id: str, caption: str) -> str`
  1. Download image from Telegram
  2. Extract facts via Claude vision (EXTRACTION_PROMPT)
  3. Parse JSON
  4. Look up vendor in db
  5. If known: use saved category/tier
  6. If new: classify via Claude (CLASSIFICATION_PROMPT + web search)
  7. Apply caption overrides if present
  8. Persist receipt to db
  9. Return formatted reply

### 3.4 Text Path (handle_text)
- `handle_text(user_text: str) -> str`
  - Classify intent
  - Route to handler:
    - `log` → `_quick_log(intent, user_text)`
    - `split` → `_handle_split(intent)`
    - `payback` → `_handle_payback(intent)`
    - `list_pending` → `db.list_pending_formatted()`
    - `undo` → `db.undo_last()`
    - `query`/`other` → `_agent_loop(user_text)` (SQL agent)

#### 3.4.1 _quick_log
- Parse amount, vendor, currency from intent
- Resolve currency/country
- Look up vendor or classify
- Persist receipt
- Return formatted reply

#### 3.4.2 _handle_split
- Parse n_people, debtors, amount
- Calculate user's share
- Create receipt for user's share
- Create pending_charge record
- Return formatted split message

#### 3.4.3 _handle_payback
- Find matching pending charge
- Mark debtor as paid or decrement owed amount
- Update pending_charge
- Return confirmation

#### 3.4.4 _agent_loop
- Multi-turn loop with Claude
- Prompt: schema + user question
- If response is JSON with "sql" field: run SQL, append result
- Otherwise: return response as final answer
- Max 4 turns

### 3.5 Helper Functions
- `_download_telegram_file(file_id) -> (bytes, str)`
  - Fetch image from Telegram
  - Return (image_bytes, mime_type)

- `_parse_json(text: str) -> dict`
  - Strip markdown code fences if present
  - Extract largest JSON block
  - Parse and return dict

- `_extract_facts_from_image(img_bytes, mime, caption) -> dict`
  - Call Claude vision with EXTRACTION_PROMPT
  - Return parsed JSON

- `_classify(vendor, items, total, currency, notes, use_web_search) -> dict`
  - Call Claude with CLASSIFICATION_PROMPT
  - Handle gracefully if search not available
  - Return category/subcategory/tier/country

- `_persist_and_format(facts, source) -> (str, int)`
  - Convert to USD
  - Insert receipt
  - Save vendor
  - Format reply message
  - Return (message, receipt_id)

- `_apply_caption_overrides(facts, caption)`
  - Parse caption for category:/subcategory:/tier: overrides
  - Mutate facts dict

- `build_nudges() -> list[str]`
  - Find pending charges not nudged recently
  - Return list of reminder messages

---

## Phase 4: Modal App (app.py)

### 4.1 Image Definition
Create Docker image with:
- Python 3.11
- pip install: anthropic, httpx, fastapi[standard]
- Mount local source: tools, worker, telegram

### 4.2 Resources
- `volume`: Modal volume named "receipt-db-free" at /data (SQLite storage)
- `inbox`: Modal queue named "receipt-inbox-free" (message queue)
- `secrets`: anthropic-api-key, telegram-bot

### 4.3 Functions

#### telegram_webhook()
- FastAPI endpoint: POST /
- Receives Telegram webhook updates
- Extract chat_id, message type (photo/text)
- Validate chat_id matches user's
- Put message in inbox queue
- Spawn process_message() async
- Return {"ok": True}

#### process_message()
- Dequeue message from inbox (timeout 1s)
- Call handle_photo() or handle_text()
- Catch exceptions, return error message
- Commit volume
- Send reply in chunks (4000 chars max)

#### init_db()
- Call db.init_db()
- Commit volume
- Print "DB initialized."

#### daily_nudge()
- Scheduled: 10am UTC daily (cron: "0 10 * * *")
- Call build_nudges()
- Send nudge messages to user
- Commit volume

#### set_telegram_webhook(url: str)
- Get webhook URL
- POST to Telegram setWebhook API
- Accept optional url parameter to override auto-detected URL
- Print response

#### check_webhook()
- GET /getWebhookInfo from Telegram
- Print webhook status
- Useful for debugging

#### sql(query: str)
- Run arbitrary SQL query
- Print results
- Used for inspection: `modal run app.py::sql --query "SELECT ..."`

---

## Phase 5: Telegram Integration (telegram.py)

### 5.1 send_message(chat_id: str, text: str)
- POST to Telegram sendMessage API
- Parameters: chat_id, text
- Keep replies plain text so user-supplied vendor names cannot break Telegram Markdown parsing
- Handle errors gracefully
- Print for debugging

---

## Phase 6: Deployment

### 6.1 Pre-deployment Checklist
1. Create Modal account, get token
2. Get Claude API key
3. Create Telegram bot, get token and chat ID
4. Test locally if possible

### 6.2 Deploy Steps
```bash
# Create secrets
modal secret create anthropic-api-key ANTHROPIC_API_KEY=sk-ant-...
modal secret create telegram-bot TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...

# Deploy
modal deploy app.py

# Initialize database
modal run app.py::init_db

# Register webhook
modal run app.py::set_telegram_webhook
```

### 6.3 Post-deployment
- Send test message to bot on Telegram
- Check Modal logs for errors
- Query database: `modal run app.py::sql --query "SELECT * FROM receipts"`

---

## Phase 7: Key Implementation Details

### 7.1 Message Flow
1. User sends message to Telegram bot
2. Telegram forwards to webhook URL (Modal function)
3. Webhook extracts chat_id, validates, queues message
4. process_message() dequeues, processes, sends reply
5. Reply chunked into 4000-char pieces for Telegram limit

### 7.2 Intent Classification
- User text → Claude → JSON with intent + parameters
- Worker routes to appropriate handler
- Handler returns formatted string reply

### 7.3 Receipt OCR
- User sends photo → Claude vision → JSON facts
- Facts → category lookup (known vendor or Claude classification)
- Receipt persisted to DB with category/tier
- Reply formatted for Telegram

### 7.4 Split Tracking
- User sends "800 dinner 4 people"
- Classify intent as "split"
- Calculate: user_share = 200, owed_total = 600
- Create receipt for user (200)
- Create pending_charge record (tracking 600 owed)
- Send reply with split details
- Later: "alice paid me back" → mark alice as paid

### 7.5 Query Agent
- User asks "how much on food this month"
- Classify intent as "query"
- Multi-turn loop:
  - Claude sees schema, generates SQL
  - Execute SQL, get results
  - Claude interprets results
  - Return final answer

---

## Testing Checklist

- [ ] Send `5 starbucks` → logs expense
- [ ] Send `590 margiela boots` → logs and categorizes correctly
- [ ] Send photo of receipt → OCR works
- [ ] Send `800 dinner 4 people` → creates split
- [ ] Send `alice paid me back` → updates split
- [ ] Send `how much did i spend this month` → returns total
- [ ] Send `undo` → deletes last receipt
- [ ] Database queries work via `modal run app.py::sql`

---

## Optional Enhancements

1. Add more prompts for complex intents
2. Implement real geolocation lookup
3. Add backup/export functionality
4. Implement recurring expense templates
5. Add budget alerts
6. Multi-currency display options
7. CSV export of expenses
