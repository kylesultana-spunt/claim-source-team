"""Thin Anthropic client wrapper.

Kept deliberately small so individual modules don't depend on SDK quirks
directly. The model defaults are controlled via env vars so CI can swap
to cheaper models on scheduled runs.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

try:
    from anthropic import Anthropic
except ImportError:  # deferred — let import work so tests can stub
    Anthropic = None  # type: ignore


# Default model choices. Override with env vars in CI.
MODEL_BULK = os.environ.get("SPUNT_MODEL_BULK", "claude-haiku-4-5-20251001")
MODEL_REASONING = os.environ.get("SPUNT_MODEL_REASONING", "claude-sonnet-4-6")
MODEL_VERDICT = os.environ.get("SPUNT_MODEL_VERDICT", "claude-opus-4-6")


_client: Optional["Anthropic"] = None


def client() -> "Anthropic":
    global _client
    if _client is None:
        if Anthropic is None:
            raise RuntimeError("anthropic SDK not installed; run `pip install anthropic`")
        _client = Anthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


@retry(stop=stop_after_attempt(3),
       wait=wait_exponential(multiplier=2, min=2, max=30))
def chat_json(model: str, system: str, user: str,
              max_tokens: int = 2000,
              tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Call the model and parse its reply as JSON.

    The prompt contract: the LAST line of the system prompt must tell the
    model to reply with valid JSON only. We still defensively strip markdown
    fences because small models occasionally wrap output in ```json.
    """
    kwargs: Dict[str, Any] = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if tools:
        kwargs["tools"] = tools

    resp = client().messages.create(**kwargs)

    # Concatenate all text blocks (tool-use blocks are for verdict module).
    text_parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = "\n".join(text_parts).strip()

    # Strip ```json fences if present.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Include the first 400 chars of output so CI logs are useful.
        snippet = raw[:400].replace("\n", " ")
        raise ValueError(f"Model did not return valid JSON: {e}. Got: {snippet!r}")
