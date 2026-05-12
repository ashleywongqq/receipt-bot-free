"""
LLM wrapper around Google Gemini.

We keep the surface area small: three functions the worker actually uses.
- generate_text(prompt) → str
- generate_with_image(prompt, image_bytes, mime) → str
- generate_with_search(prompt) → str   (uses Google grounding when available)

All three return raw strings; JSON parsing happens in worker.py as before.
"""

import os
import base64
import google.generativeai as genai

# Configure once on import
genai.configure(api_key=os.environ["GEMINI_API_KEY"])

# Use gemini-pro which is stable and works on free tier
MODEL_NAME = "gemini-pro"


def generate_text(prompt: str, max_tokens: int = 1024) -> str:
    """Plain text → text."""
    model = genai.GenerativeModel(MODEL_NAME)
    resp = model.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            max_output_tokens=max_tokens,
            temperature=0.1,
        ),
    )
    return _extract_text(resp)


def generate_with_image(prompt: str, image_bytes: bytes, mime: str,
                        max_tokens: int = 1024) -> str:
    """Image + text → text. Used for receipt OCR."""
    model = genai.GenerativeModel("gemini-pro-vision")
    image_part = {"mime_type": mime, "data": image_bytes}
    resp = model.generate_content(
        [image_part, prompt],
        generation_config=genai.types.GenerationConfig(
            max_output_tokens=max_tokens,
            temperature=0.1,
        ),
    )
    return _extract_text(resp)


def generate_with_search(prompt: str, max_tokens: int = 512) -> str:
    """Text → text with web search grounding. Used for unknown vendor classification."""
    # Search grounding not available on free tier, fall back to regular generation
    return generate_text(prompt, max_tokens)


def _extract_text(resp) -> str:
    """Extract text from response."""
    try:
        if hasattr(resp, "text") and resp.text:
            return resp.text
    except Exception:
        pass

    try:
        return "".join(
            p.text for p in resp.candidates[0].content.parts
            if hasattr(p, "text") and p.text
        )
    except Exception:
        return ""
