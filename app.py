"""
Receipt bot — free edition, runs on Google Gemini's free tier + Modal's free tier.
"""

import modal

app = modal.App("receipt-bot-free")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "google-generativeai>=0.8.0",
        "httpx>=0.27.0",
        "fastapi[standard]>=0.115.0",
    )
    .add_local_python_source("tools", "worker", "telegram")
)

volume = modal.Volume.from_name("receipt-db-free", create_if_missing=True)
VOLUME_MOUNTS = {"/data": volume}

inbox = modal.Queue.from_name("receipt-inbox-free", create_if_missing=True)

secrets = [
    modal.Secret.from_name("gemini-api-key"),    # GEMINI_API_KEY
    modal.Secret.from_name("telegram-bot"),      # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
]


@app.function(image=image, secrets=secrets, volumes=VOLUME_MOUNTS)
@modal.fastapi_endpoint(method="POST", label="telegram")
def telegram_webhook(update: dict):
    import os
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat_id = str(message["chat"]["id"])
    if chat_id != os.environ["TELEGRAM_CHAT_ID"]:
        return {"ok": True}

    payload = {"chat_id": chat_id}

    if "photo" in message:
        largest = max(message["photo"], key=lambda p: p["file_size"])
        payload["type"] = "photo"
        payload["file_id"] = largest["file_id"]
        payload["caption"] = message.get("caption", "")
    elif "document" in message and message["document"].get("mime_type", "").startswith("image/"):
        payload["type"] = "photo"
        payload["file_id"] = message["document"]["file_id"]
        payload["caption"] = message.get("caption", "")
    elif "text" in message:
        payload["type"] = "text"
        payload["text"] = message["text"]
    else:
        return {"ok": True}

    inbox.put(payload)
    process_message.spawn()
    return {"ok": True}


@app.function(image=image, secrets=secrets, volumes=VOLUME_MOUNTS, timeout=120)
def process_message():
    from worker import handle_photo, handle_text
    from telegram import send_message
    from tools import db

    db.init_db()

    try:
        msg = inbox.get(timeout=1)
    except Exception:
        return

    chat_id = msg["chat_id"]
    try:
        if msg["type"] == "photo":
            send_message(chat_id, "📸 reading receipt…")
            reply = handle_photo(msg["file_id"], msg.get("caption", ""))
        else:
            reply = handle_text(msg["text"])
    except Exception as e:
        reply = f"⚠️ error: {type(e).__name__}: {e}"

    volume.commit()

    for chunk in [reply[i:i + 4000] for i in range(0, len(reply), 4000)]:
        send_message(chat_id, chunk)


@app.function(image=image, secrets=secrets, volumes=VOLUME_MOUNTS)
def init_db():
    from tools import db
    db.init_db()
    volume.commit()
    print("DB initialized.")


@app.function(
    image=image, secrets=secrets, volumes=VOLUME_MOUNTS,
    schedule=modal.Cron("0 10 * * *"),
)
def daily_nudge():
    import os
    from worker import build_nudges
    from telegram import send_message
    from tools import db

    db.init_db()
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    messages = build_nudges()
    if not messages:
        return
    bundle = "Reminder — outstanding balances:\n\n" + "\n\n".join(messages)
    for chunk in [bundle[i:i + 4000] for i in range(0, len(bundle), 4000)]:
        send_message(chat_id, chunk)
    volume.commit()


@app.function(image=image, secrets=secrets)
def set_telegram_webhook():
    import os, httpx
    webhook_url = telegram_webhook.get_web_url()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={"url": webhook_url},
    )
    print(r.json())


@app.function(image=image, secrets=secrets, volumes=VOLUME_MOUNTS)
def sql(query: str):
    from tools import db
    print(db.run_sql(query))
