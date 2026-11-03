"""Provider registry: account.provider key → provider instance.

In mock mode (WHATSAPP_PROVIDER_MODE=mock, the dev default) every account
resolves to the MockProvider, so no request ever leaves the service.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from app.whatsapp_layer.providers.base import WhatsAppProvider
from app.whatsapp_layer.providers.mock import MockProvider
from app.whatsapp_layer.providers.omnichannel import OmnichannelProvider

logger = logging.getLogger("app_logger")

# Shown in the provider dropdown. `available=False` = "coming soon".
PROVIDER_CATALOGUE: List[Dict] = [
    {"key": "omnichannel", "label": "Omnichannel", "available": True},
    {"key": "gupshup", "label": "Gupshup", "available": False},
]

_instances: Dict[str, WhatsAppProvider] = {}


def provider_mode() -> str:
    from app.core import settings
    mode = (getattr(settings, "WHATSAPP_PROVIDER_MODE", "") or "mock").strip().lower()
    return mode if mode in ("live", "mock") else "mock"


def _http_timeout() -> float:
    from app.core import settings
    try:
        return float(getattr(settings, "WHATSAPP_HTTP_TIMEOUT_SECONDS", 15) or 15)
    except (TypeError, ValueError):
        return 15.0


def available_keys() -> List[str]:
    return [p["key"] for p in PROVIDER_CATALOGUE if p["available"]]


def get_provider(key: Optional[str]) -> WhatsAppProvider:
    """Return the provider for an account's `provider` key."""
    if provider_mode() == "mock":
        key = "mock"
    if key not in _instances:
        if key == "mock":
            _instances[key] = MockProvider()
        elif key == "omnichannel":
            _instances[key] = OmnichannelProvider(timeout=_http_timeout())
        else:
            raise ValueError(f"Unknown WhatsApp provider '{key}'")
    return _instances[key]


def reset_cache() -> None:
    """Drop cached instances (tests / settings changes)."""
    _instances.clear()
