"""
LLM wrapper around Google Gemini.

We keep the surface area small: three functions the worker actually uses.
- generate_text(prompt) → str
- generate_with_image(prompt, image_bytes, mime) → str
- generate_with_search(prompt) → str   (uses Google grounding when available)

All three return raw strings; JSON parsing happens in worker.py as before.

Models:
- gemini-2.0-flash: free tier, 1500 requests/day, supports vision and
  grounding (web search). This is our workhorse.
"""

import os
import google.genai as genai

# Configure once on import
genai.configure(api_key=os.environ["GEMINI_API_KEY"])

# Model for everything. Flash is fast, free, and good enough for this.
MODEL_NAME = "gemini-2.0-flash"


def _client(use_search: bool = False):
    """Build a model client, optionally with Google Search grounding."""
    if use_search:
        # Grounding lets the model search the web when needed
        return genai.Client().models.generate_content(
            model=MODEL_NAME,
            contents=[],
            tools=[genai.types.Tool(
                google_search_retrieval=genai.types.GoogleSearchRetrieval()
            )],
        )
    return genai.Client().models


def generate_text(prompt: str, max_tokens: int = 1024) -> str:
    """Plain text → text."""
    client = genai.Client()
    resp = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=0.1,
        ),
    )
    return _extract_text(resp)


def generate_with_image(prompt: str, image_bytes: bytes, mime: str,
                        max_tokens: int = 1024) -> str:
    """Image + text → text. Used for receipt OCR."""
    client = genai.Client()
    resp = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            genai.types.Content(
                parts=[
                    genai.types.Part.from_blob(
                        mime_type=mime,
                        data=image_bytes,
                    ),
                    genai.types.Part.from_text(prompt),
                ]
            )
        ],
        config=genai.types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=0.1,
        ),
    )
    return _extract_text(resp)


def generate_with_search(prompt: str, max_tokens: int = 512) -> str:
    """Text → text with web search grounding. Used for unknown vendor classification."""
    try:
        client = genai.Client()
        resp = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=0.1,
            ),
            tools=[genai.types.Tool(
                google_search_retrieval=genai.types.GoogleSearchRetrieval()
            )],
        )
        return _extract_text(resp)
    except Exception:
        # If grounding isn't available on this account/region, fall back to no search
        return generate_text(prompt, max_tokens)


def _extract_text(resp) -> str:
    """Extract text from response."""
    try:
        if hasattr(resp, "text") and resp.text:
            return resp.text
    except Exception:
        pass

    try:
        return "".join(part.text for part in resp.candidates[0].content.parts if hasattr(part, "text"))
    except Exception:
        return ""
