"""
LLM wrapper around Google Gemini.

We keep the surface area small: three functions the worker actually uses.
- generate_text(prompt) → str
- generate_with_image(prompt, image_bytes, mime) → str
- generate_with_search(prompt) → str   (uses Google grounding when available)

All three return raw strings; JSON parsing happens in worker.py as before.

Models:
- gemini-1.5-flash: free tier, 1500 requests/day, supports vision and
  grounding (web search). This is our workhorse.
"""

import os
import base64
import google.generativeai as genai

# Configure once on import
genai.configure(api_key=os.environ["GEMINI_API_KEY"])

# Model for everything. Flash is fast, free, and good enough for this.
MODEL_NAME = "gemini-1.5-flash"


def _client(use_search: bool = False):
    """Build a model client, optionally with Google Search grounding."""
    if use_search:
        # Grounding lets the model search the web when needed
        return genai.GenerativeModel(
            MODEL_NAME,
            tools=[genai.protos.Tool(
                google_search_retrieval=genai.protos.GoogleSearchRetrieval()
            )],
        )
    return genai.GenerativeModel(MODEL_NAME)


def generate_text(prompt: str, max_tokens: int = 1024) -> str:
    """Plain text → text."""
    model = _client()
    resp = model.generate_content(
        prompt,
        generation_config={"max_output_tokens": max_tokens, "temperature": 0.1},
    )
    return _extract_text(resp)


def generate_with_image(prompt: str, image_bytes: bytes, mime: str,
                        max_tokens: int = 1024) -> str:
    """Image + text → text. Used for receipt OCR."""
    model = _client()
    image_part = {"mime_type": mime, "data": image_bytes}
    resp = model.generate_content(
        [image_part, prompt],
        generation_config={"max_output_tokens": max_tokens, "temperature": 0.1},
    )
    return _extract_text(resp)


def generate_with_search(prompt: str, max_tokens: int = 512) -> str:
    """Text → text with web search grounding. Used for unknown vendor classification."""
    try:
        model = _client(use_search=True)
        resp = model.generate_content(
            prompt,
            generation_config={"max_output_tokens": max_tokens, "temperature": 0.1},
        )
        return _extract_text(resp)
    except Exception:
        # If grounding isn't available on this account/region, fall back to no search
        return generate_text(prompt, max_tokens)


def _extract_text(resp) -> str:
    """Gemini sometimes returns multi-part responses; concat all text parts."""
    try:
        # Most common: direct .text
        if hasattr(resp, "text") and resp.text:
            return resp.text
    except Exception:
        pass

    # Fall back: walk the candidates structure
    try:
        parts = resp.candidates[0].content.parts
        return "".join(p.text for p in parts if hasattr(p, "text") and p.text)
    except Exception:
        return ""
