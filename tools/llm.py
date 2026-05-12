"""
LLM wrapper around Google Gemini using the new google.genai package.

We keep the surface area small: three functions the worker actually uses.
- generate_text(prompt) → str
- generate_with_image(prompt, image_bytes, mime) → str
- generate_with_search(prompt) → str   (uses Google grounding when available)

All three return raw strings; JSON parsing happens in worker.py as before.
"""

import os
import google.genai as genai

# Configure once on import
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# Model for everything. gemini-1.5-flash has a higher free quota.
MODEL_NAME = "gemini-1.5-flash"


def generate_text(prompt: str, max_tokens: int = 1024) -> str:
    """Plain text → text."""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=0.1,
        ),
    )
    return _extract_text(response)


def generate_with_image(prompt: str, image_bytes: bytes, mime: str,
                        max_tokens: int = 1024) -> str:
    """Image + text → text. Used for receipt OCR."""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            genai.types.Content(
                parts=[
                    genai.types.Part.from_blob(mime_type=mime, data=image_bytes),
                    genai.types.Part.from_text(prompt),
                ]
            )
        ],
        config=genai.types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=0.1,
        ),
    )
    return _extract_text(response)


def generate_with_search(prompt: str, max_tokens: int = 512) -> str:
    """Text → text with web search grounding. Used for unknown vendor classification."""
    try:
        response = client.models.generate_content(
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
        return _extract_text(response)
    except Exception:
        # If grounding isn't available, fall back to no search
        return generate_text(prompt, max_tokens)


def _extract_text(response) -> str:
    """Extract text from response."""
    try:
        if hasattr(response, "text") and response.text:
            return response.text
    except Exception:
        pass

    try:
        return "".join(
            part.text for part in response.candidates[0].content.parts
            if hasattr(part, "text") and part.text
        )
    except Exception:
        return ""
