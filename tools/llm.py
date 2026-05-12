"""
LLM wrapper around Claude API.

We keep the surface area small: three functions the worker actually uses.
- generate_text(prompt) → str
- generate_with_image(prompt, image_bytes, mime) → str
- generate_with_search(prompt) → str   (doesn't use search, just calls generate_text)

All three return raw strings; JSON parsing happens in worker.py as before.
"""

import os
from anthropic import Anthropic

# Configure once on import
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Model for everything
MODEL_NAME = "claude-3-5-sonnet-20241022"


def generate_text(prompt: str, max_tokens: int = 1024) -> str:
    """Plain text → text."""
    resp = client.messages.create(
        model=MODEL_NAME,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def generate_with_image(prompt: str, image_bytes: bytes, mime: str,
                        max_tokens: int = 1024) -> str:
    """Image + text → text. Used for receipt OCR."""
    import base64
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
    resp = client.messages.create(
        model=MODEL_NAME,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    )
    return resp.content[0].text


def generate_with_search(prompt: str, max_tokens: int = 512) -> str:
    """Text → text. Search grounding not available, just use regular generation."""
    return generate_text(prompt, max_tokens)
