"""
Receipt bot — Claude + Modal edition.
"""

import modal

app = modal.App("receipt-bot-free")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "anthropic>=0.7.0",
        "httpx>=0.27.0",
        "fastapi[standard]>=0.115.0",
    )
    .add_local_python_source("tools", "worker", "telegram")
)

volume = modal.Volume.from_name("receipt-db-free", create_if_missing=True)
VOLUME_MOUNTS = {"/data": volume}

inbox = modal.Queue.from_name("receipt-inbox-free", create_if_missing=True)

secrets = [
    modal.Secret.from_name("anthropic-api-key"),    # ANTHROPIC_API_KEY
    modal.Secret.from_name("telegram-bot"),         # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
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
        largest = max(
            message["photo"],
            key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0),
        )
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
        print("[process_message] No message in queue")
        return

    chat_id = msg["chat_id"]
    print(f"[process_message] Processing message from {chat_id}: {msg}")
    try:
        if msg["type"] == "photo":
            send_message(chat_id, "📸 reading receipt…")
            reply = handle_photo(msg["file_id"], msg.get("caption", ""))
        else:
            reply = handle_text(msg["text"])
    except Exception as e:
        reply = f"⚠️ error: {type(e).__name__}: {e}"
        print(f"[process_message] Error: {reply}")

    volume.commit()

    print(f"[process_message] Sending reply: {reply[:100]}")
    for chunk in [reply[i:i + 4000] for i in range(0, len(reply), 4000)]:
        print(f"[process_message] Sending chunk to {chat_id}")
        send_message(chat_id, chunk)
    print(f"[process_message] Done")


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
def set_telegram_webhook(url: str = ""):
    import os, httpx
    webhook_url = url or telegram_webhook.get_web_url()
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


@app.function(image=image, secrets=secrets)
def check_webhook():
    import os, httpx, json
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = httpx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
    print(json.dumps(r.json(), indent=2))


@app.function(image=image, secrets=secrets)
def check_anthropic_models():
    import os, httpx

    r = httpx.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
        timeout=20,
    )
    print(f"status={r.status_code}")
    try:
        data = r.json()
    except Exception:
        print(r.text[:1000])
        return
    if r.status_code != 200:
        print(data)
        return
    for model in data.get("data", []):
        print(model.get("id"))


@app.function(image=image, secrets=secrets)
def check_anthropic_text(model: str = ""):
    import os
    from anthropic import Anthropic

    model = model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=model,
        max_tokens=16,
        messages=[{"role": "user", "content": "Reply with OK."}],
    )
    print(f"model={model}")
    print(resp.content[0].text)
