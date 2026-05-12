"""Telegram sendMessage wrapper."""

import os
import httpx


def send_message(chat_id: str, text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    print(f"[SEND] chat_id={chat_id}, text={text[:50]}")
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        print(f"[SEND] response: {r.status_code}")
    except Exception as e:
        print(f"[SEND] error: {e}")
