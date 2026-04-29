"""Gemini client wrapper.

Two tiers:
    pro_llm()    — main reasoning + composition (`gemini-2.5-pro`)
    flash_llm()  — cheap routing + intent classification + scrub passes

Both go through `langchain_google_genai.ChatGoogleGenerativeAI` so we get
LangChain message types, function calling, and consistent error surfaces.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Optional

from app.core import settings

logger = logging.getLogger("app_logger")


@lru_cache(maxsize=1)
def _import_chat_model():
    """Import lazily so the service starts even if `langchain_google_genai`
    isn't installed yet (handy for local dev without the AI dep)."""
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore
        return ChatGoogleGenerativeAI
    except ImportError as e:
        logger.warning("langchain_google_genai not installed: %s", e)
        return None


def _build(model_name: str, temperature: float = 0.2):
    api_key = getattr(settings, "GEMINI_API_KEY", "") or ""
    if not api_key:
        logger.warning("GEMINI_API_KEY missing — AI replies will fail")
    cls = _import_chat_model()
    if cls is None:
        return None
    return cls(
        model=model_name,
        google_api_key=api_key or None,
        temperature=temperature,
        max_retries=2,
        convert_system_message_to_human=False,
    )


@lru_cache(maxsize=2)
def pro_llm():
    return _build(settings.GEMINI_PRO_MODEL, temperature=0.2)


@lru_cache(maxsize=2)
def flash_llm():
    return _build(settings.GEMINI_FLASH_MODEL, temperature=0.0)


def estimate_tokens(text: Optional[str]) -> int:
    """Cheap, conservative tokens-from-text estimator (~4 chars/token).

    Real usage is read from Gemini response metadata when available; this
    is the fallback we use to reserve budget upfront before the call.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def usage_metadata(response: Any) -> dict:
    """Pull token counts off a LangChain AIMessage from Gemini.

    Returns `{tokens_in: int, tokens_out: int}` — zeros if not present.
    """
    if response is None:
        return {"tokens_in": 0, "tokens_out": 0}
    meta = getattr(response, "usage_metadata", None) or {}
    if isinstance(meta, dict):
        return {
            "tokens_in": int(meta.get("input_tokens", 0) or 0),
            "tokens_out": int(meta.get("output_tokens", 0) or 0),
        }
    return {"tokens_in": 0, "tokens_out": 0}
