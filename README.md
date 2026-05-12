# Receipt Tracker Bot — Free Edition 🆓

A personal expense bot that runs on **completely free tiers** of everything:
- 🧠 **Google Gemini API** (1,500 free requests/day — way more than personal use)
- ☁️ **Modal** ($30/month free compute — uses pennies)
- 💬 **Telegram** (always free)
- 💱 **Frankfurter** (free FX rates)
- 💾 **SQLite** on Modal's free storage

No credit card needed anywhere. No monthly fees. Just sign up for accounts.

## What it does

- **Quick text logging**: `5 yy kafei` → $5 at YY Kafei
- **Photo receipts**: snap, send, parsed
- **Multi-currency** with USD reporting (auto FX on receipt date)
- **Vendor memory** (tells H&M apart from Rick Owens)
- **Split expenses**: `$800 team dinner 9 people` tracks who owes you
- **Auto-nudges** at 3/7/14/30 days for unpaid splits
- **Natural language queries** powered by SQL underneath

---

## Setup — ~45 min the first time

### Chunk 1 — Download and unzip (2 min)

1. Unzip this folder somewhere like `~/Documents/receipt-bot-free`
2. Open Terminal (Mac) or PowerShell (Windows)
3. Navigate to the folder:
   ```bash
   cd ~/Documents/receipt-bot-free
   ```

You need Python 3.11+ installed. Check with `python3 --version`. If not, get it from [python.org](https://www.python.org/downloads/).

### Chunk 2 — Get a Google Gemini API key (5 min)

1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Sign in with **any Google account** (personal Gmail is fine)
3. Click "Get API key" (top left or via the key icon)
4. Click "Create API key" → "Create API key in new project" if asked
5. Copy the key (starts with `AIza...`). Save it somewhere safe.

**That's it.** No payment method, no credit card. The free tier gives you 1,500 requests/day on Gemini 2.0 Flash, which includes vision.

### Chunk 3 — Create a Telegram bot (10 min)

1. Install Telegram on your phone or desktop if you haven't.
2. In Telegram, search for **@BotFather** and open a chat.
3. Send `/newbot`. Answer the prompts:
   - Name: anything ("My Receipt Bot")
   - Username: must end in `bot` (like `joes_receipts_bot`). Must be globally unique — try variations.
4. **Save the bot token** it gives you (looks like `7234567890:AAH...`).
5. Click the link to your new bot, send it any message like "hi". (No reply yet — that's fine.)
6. **Get your chat ID**: open this URL in a browser, replacing `<TOKEN>`:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Look for `"chat":{"id":12345...}`. That number is your chat ID.

### Chunk 4 — Set up Modal (10 min)

1. Go to [modal.com/signup](https://modal.com/signup), sign up. Free, no card.
2. In Terminal:
   ```bash
   pip3 install modal
   modal token new
   ```
3. The second command opens a browser to authorize. Click through, close tab.
4. Verify with `modal token current` — should show your account.

### Chunk 5 — Wire up secrets (3 min)

Run these in Terminal, replacing `...` with your real values:

```bash
modal secret create gemini-api-key GEMINI_API_KEY=AIza...your-key...
```

```bash
modal secret create telegram-bot TELEGRAM_BOT_TOKEN=7234567890:AAH...your-token... TELEGRAM_CHAT_ID=12345678
```

Check [modal.com/secrets](https://modal.com/secrets) — you should see both.

### Chunk 6 — Deploy and go live (5 min)

In Terminal, still in the `receipt-bot-free` folder:

```bash
modal deploy app.py
```

Wait ~30 seconds. Then:

```bash
modal run app.py::init_db
modal run app.py::set_telegram_webhook
```

**Test it!** Open Telegram, find your bot, send `5 starbucks`. You should get a reply in a few seconds.

---

## Usage

### Logging

| You type | Bot does |
|---|---|
| `5 yy kafei` | Logs $5 at YY Kafei, infers currency from context, tags as breakfast/lunch/dinner based on local time |
| `150 tatiana` | Logs $150 at Tatiana, tagged "dinner" if it's evening NYC time |
| `£8 pret breakfast` | Logs £8 at Pret with item "breakfast" |
| `tatiana 200 last friday` | $200 at Tatiana dated last Friday |

### Splits

| You type | Bot does |
|---|---|
| `$800 team dinner 9 people` | Logs $88.89 (your share) + tracks $711.11 owed |
| `120 dinner with j, mike, sara` | 4-way split with named debtors |

### Payback

- `alice paid me back` — marks Alice paid
- `everyone paid` — settles the most recent open charge
- `mike sent $89` — same as "mike paid me back"

### Queries

- "how much did I spend on food this month"
- "biggest 5 receipts in singapore"
- "who owes me right now"
- "average dinner cost this year"

### Other

- `undo` — remove the last receipt
- `fix vendor H&M tier value` — correct a vendor's classification
- `list pending` / `who owes me` — show open splits
- `db stats` — totals overview

### Photos
Snap a receipt photo, send it. Optional caption like `category: groceries` to override.

---

## Limits to be aware of

- **15 requests per minute**, **1,500/day** on the free Gemini tier. You'd have to use this very heavily to ever hit either.
- **Photo OCR**: Gemini 2.0 Flash is genuinely good but not perfect. Expect ~90% accuracy on clean receipts. The "undo" button is your friend.
- **Web search grounding** may have regional limits — if Gemini Search isn't available in your region, the bot silently falls back to classification without search. Still works fine.

---

## Inspecting your data

```bash
# Quick SQL from your terminal
modal run app.py::sql --query "SELECT vendor, COUNT(*) FROM receipts GROUP BY vendor ORDER BY 2 DESC LIMIT 10"

# Download the whole database
modal volume get receipt-db-free receipts.db ./receipts.db
```

Open `receipts.db` in [DB Browser for SQLite](https://sqlitebrowser.org) (free, spreadsheet-like view).

---

## Troubleshooting

**Bot doesn't reply.** Check logs:
```bash
modal app logs receipt-bot-free
```

**"Module not found" errors.** Make sure you're inside the `receipt-bot-free` folder when running `modal deploy`.

**Telegram says "unauthorized".** Bot token wrong. Re-run:
```bash
modal secret delete telegram-bot
modal secret create telegram-bot TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
modal deploy app.py
```

**Gemini errors.** Check that your `GEMINI_API_KEY` secret is set correctly in Modal. Test by running `modal run app.py::sql --query "SELECT 1"` — if that works, Modal is fine and only the LLM call is failing.

**Wrong chat ID.** Bot ignores messages from any chat other than yours. Send your bot one more message, then re-check the `getUpdates` URL.

---

## Switching to Claude later (if you ever want to)

If you decide receipt accuracy matters enough to pay a few bucks a month for Claude's better vision:

1. Get an Anthropic API key
2. `pip install anthropic` (already in their lib)
3. Replace `tools/llm.py` with an Anthropic version — I can write that 30-line file in a heartbeat
4. `modal secret create anthropic-api-key ANTHROPIC_API_KEY=sk-ant-...`
5. Update the secret name in `app.py`
6. Redeploy

Nothing else changes. All your data persists.

---

## Costs (the whole truth)

- Gemini API: $0/month within free tier (1500 requests/day)
- Modal: $0/month within free tier ($30/month compute free)
- Telegram: $0/month, always
- Frankfurter FX: $0/month, always

**Total: $0.** Forever, for personal use.
