import os
from functools import lru_cache

try:
    from anthropic import Anthropic
except ImportError:  # Lets local DB-only checks import the project without deps.
    Anthropic = None

"""
LLM wrapper around the Anthropic Messages API.

The worker only needs three calls:
- generate_text(prompt) -> str
- generate_with_image(prompt, image_bytes, mime) -> str
- generate_with_search(prompt) -> str

All three return raw strings; JSON parsing happens in worker.py.
"""

# Haiku 3.5 is Anthropic's current lightweight Haiku model ID. Allow an env
# override so the deployed bot can move to Sonnet/Opus without a code change.
DEFAULT_MODEL_NAME = "claude-3-5-haiku-20241022"
MODEL_NAME = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL_NAME)


@lru_cache(maxsize=1)
def _client():
    if Anthropic is None:
        raise RuntimeError(
            "The `anthropic` package is not installed. "
            "Run `pip install -r requirements.txt`."
        )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is missing. Create the Modal secret "
            "`anthropic-api-key` with ANTHROPIC_API_KEY=sk-ant-..."
        )
    return Anthropic(api_key=api_key)


def _response_text(resp) -> str:
    chunks = [getattr(block, "text", "") for block in resp.content]
    return "".join(chunks).strip()


def generate_text(prompt: str, max_tokens: int = 1024) -> str:
    """Plain text → text."""
    resp = _client().messages.create(
        model=MODEL_NAME,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return _response_text(resp)


def generate_with_image(prompt: str, image_bytes: bytes, mime: str,
                        max_tokens: int = 1024) -> str:
    """Image + text → text. Used for receipt OCR."""
    import base64
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
    resp = _client().messages.create(
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
    return _response_text(resp)


def generate_with_search(prompt: str, max_tokens: int = 512) -> str:
    """Text → text. Search grounding not available, just use regular generation."""
    return generate_text(prompt, max_tokens)
